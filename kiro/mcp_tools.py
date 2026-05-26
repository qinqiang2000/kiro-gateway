# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
MCP Tools Support (WebSearch via Kiro MCP API).

Handles server-side tools that execute on Kiro infrastructure via MCP API.
This module provides:
- MCP API calls for web_search
- SSE response emulation in Anthropic/OpenAI formats
- Path A: Native Anthropic server-side tools (early return from routes)
- Path B: MCP tool emulation (streaming interception)
"""

import json
import time
import uuid
import random
import string
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

import httpx
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from kiro.tokenizer import count_message_tokens, count_tokens

try:
    from kiro.debug_logger import debug_logger
except ImportError:
    debug_logger = None


# ==================================================================================================
# ID Generation
# ==================================================================================================

def generate_random_id(length: int) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


# ==================================================================================================
# MCP API
# ==================================================================================================

async def call_kiro_mcp_api(
    query: str,
    auth_manager,
) -> Tuple[Optional[str], Optional[Dict]]:
    """
    Call Kiro MCP API for web_search.

    Args:
        query: Search query string
        auth_manager: KiroAuthManager instance

    Returns:
        Tuple of (tool_use_id, results_dict) or (None, None) on error.
        results_dict has shape {"results": [...], "totalResults": N, "query": "..."}
    """
    random_22 = generate_random_id(22)
    timestamp = int(time.time() * 1000)
    random_8 = generate_random_id(8)
    request_id = f"web_search_tooluse_{random_22}_{timestamp}_{random_8}"
    tool_use_id = f"srvtoolu_{uuid.uuid4().hex[:32]}"

    mcp_request: Dict[str, Any] = {
        "id": request_id,
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "web_search",
            "arguments": {"query": query},
        },
    }

    # profileArn required by runtime.kiro.dev/mcp
    profile_arn = auth_manager.profile_arn or ""
    if profile_arn:
        mcp_request["profileArn"] = profile_arn

    try:
        if debug_logger:
            debug_logger.log_raw_chunk(
                b"[MCP REQUEST]\n" + json.dumps(mcp_request, ensure_ascii=False, indent=2).encode()
            )
    except Exception:
        pass

    try:
        token = await auth_manager.get_access_token()

        headers: Dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "x-amzn-codewhisperer-optout": "false",
            "Content-Type": "application/json",
        }
        if profile_arn:
            headers["x-amzn-codewhisperer-profile-arn"] = profile_arn

        mcp_url = f"{auth_manager.q_host}/mcp"
        logger.debug(f"Calling MCP API: {mcp_url}")

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(mcp_url, json=mcp_request, headers=headers)

            if response.status_code != 200:
                body_preview = response.text[:200]
                logger.error(f"MCP API error: {response.status_code} — {body_preview}")
                return None, None

            mcp_response = response.json()

            try:
                if debug_logger:
                    debug_logger.log_raw_chunk(
                        b"[MCP RESPONSE]\n"
                        + json.dumps(mcp_response, ensure_ascii=False, indent=2).encode()
                    )
            except Exception:
                pass

            if mcp_response.get("error"):
                logger.error(f"MCP API returned error: {mcp_response['error']}")
                return None, None

            # result.content[0].text is a JSON STRING — must parse
            result_text = (
                mcp_response.get("result", {})
                .get("content", [{}])[0]
                .get("text", "{}")
            )
            results = json.loads(result_text)
            logger.debug(f"MCP API returned {results.get('totalResults', 0)} results")
            return tool_use_id, results

    except httpx.TimeoutException as e:
        logger.error(f"MCP API timeout: {e}")
    except httpx.RequestError as e:
        logger.error(f"MCP API request error: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"MCP API response JSON parse error: {e}")
    except Exception as e:
        logger.error(f"MCP API unexpected error: {e}", exc_info=True)

    return None, None


def generate_search_summary(query: str, results: Dict) -> str:
    """
    Format search results as human-readable text wrapped in <web_search> tags.

    Returns full snippets without truncation so the model has complete context.
    """
    summary = f'\n<web_search>\nSearch results for "{query}":\n\n'

    if results and "results" in results:
        for i, result in enumerate(results["results"], 1):
            title = result.get("title", "Untitled")
            url = result.get("url", "")
            snippet = result.get("snippet", "")
            published_date_ms = result.get("publishedDate")

            summary += f"{i}. Title: **{title}**\n"

            if published_date_ms:
                try:
                    dt = datetime.fromtimestamp(published_date_ms / 1000)
                    summary += f"   Published: {dt.strftime('%d %b %Y %H:%M:%S')}\n"
                except (ValueError, OSError):
                    pass

            if url:
                summary += f"   URL: {url}\n"
            if snippet:
                summary += f"   {snippet}\n"
            summary += "\n"
    else:
        summary += "No results found.\n"

    summary += "</web_search>\n"
    return summary


# ==================================================================================================
# SSE Emulation — Anthropic format
# ==================================================================================================

async def generate_anthropic_web_search_sse(
    model: str,
    query: str,
    tool_use_id: str,
    results: Dict,
    input_tokens: int,
):
    """
    Emit Anthropic SSE events for a completed web_search (Path A).

    Sequence: message_start → server_tool_use block → web_search_tool_result block
              → text block (summary) → message_delta → message_stop
    """
    from kiro.streaming_anthropic import format_sse_event

    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    summary = generate_search_summary(query, results)
    output_tokens = count_tokens(summary, apply_claude_correction=False)

    yield format_sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    })

    yield format_sse_event("content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "id": tool_use_id,
            "type": "server_tool_use",
            "name": "web_search",
            "input": {},
        },
    })

    yield format_sse_event("content_block_delta", {
        "type": "content_block_delta",
        "index": 0,
        "delta": {
            "type": "input_json_delta",
            "partial_json": json.dumps({"query": query}),
        },
    })

    yield format_sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})

    search_content = [
        {
            "type": "web_search_result",
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "encrypted_content": r.get("snippet", ""),
            "page_age": None,
        }
        for r in results.get("results", [])
    ]

    yield format_sse_event("content_block_start", {
        "type": "content_block_start",
        "index": 1,
        "content_block": {
            "type": "web_search_tool_result",
            "tool_use_id": tool_use_id,
            "content": search_content,
        },
    })

    yield format_sse_event("content_block_stop", {"type": "content_block_stop", "index": 1})

    yield format_sse_event("content_block_start", {
        "type": "content_block_start",
        "index": 2,
        "content_block": {"type": "text", "text": ""},
    })

    chunk_size = 100
    for i in range(0, len(summary), chunk_size):
        yield format_sse_event("content_block_delta", {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "text_delta", "text": summary[i:i + chunk_size]},
        })

    yield format_sse_event("content_block_stop", {"type": "content_block_stop", "index": 2})

    yield format_sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })

    yield format_sse_event("message_stop", {"type": "message_stop"})


# ==================================================================================================
# Path A: Native Anthropic server-side tool handler
# ==================================================================================================

def extract_query_from_messages(messages) -> Optional[str]:
    """
    Extract the search query from the first user message.

    Works with both Pydantic model instances and plain dicts.
    """
    if not messages:
        return None

    first_msg = messages[0]
    content = getattr(first_msg, "content", None) if not isinstance(first_msg, dict) else first_msg.get("content")

    if content is None:
        return None

    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if hasattr(block, "type") and getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", ""))
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = "".join(parts)
    else:
        return None

    prefix = "Perform a web search for the query: "
    query = text[len(prefix):] if text.startswith(prefix) else text
    return query.strip() or None


async def handle_native_web_search(
    request,
    request_data,
    auth_manager,
) -> "JSONResponse | StreamingResponse":
    """
    Handle a native Anthropic web_search server-side tool (Path A).

    Bypasses /generateAssistantResponse entirely — calls Kiro MCP API
    directly and emulates the Anthropic SSE response.

    Args:
        request: FastAPI Request
        request_data: Validated AnthropicMessagesRequest
        auth_manager: KiroAuthManager instance

    Returns:
        StreamingResponse (stream=True) or JSONResponse (stream=False)
    """
    query = extract_query_from_messages(request_data.messages)
    if not query:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Cannot extract search query from messages",
                },
            },
        )

    logger.info(f"WebSearch Path A — query: {query!r}")

    tool_use_id, results = await call_kiro_mcp_api(query, auth_manager)
    if results is None:
        return JSONResponse(
            status_code=500,
            content={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "Web search failed. Please try again.",
                },
            },
        )

    input_tokens = count_message_tokens(
        [msg.model_dump() for msg in request_data.messages],
        apply_claude_correction=False,
    )

    if request_data.stream:
        return StreamingResponse(
            generate_anthropic_web_search_sse(
                request_data.model, query, tool_use_id, results, input_tokens
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # Non-streaming: return full JSON
    summary = generate_search_summary(query, results)
    output_tokens = count_tokens(summary, apply_claude_correction=False)
    message_id = f"msg_{uuid.uuid4().hex[:24]}"

    search_content = [
        {
            "type": "web_search_result",
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "encrypted_content": r.get("snippet", ""),
            "page_age": None,
        }
        for r in results.get("results", [])
    ]

    return JSONResponse(content={
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "server_tool_use",
                "id": tool_use_id,
                "name": "web_search",
                "input": {"query": query},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": tool_use_id,
                "content": search_content,
            },
            {"type": "text", "text": summary},
        ],
        "model": request_data.model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })
