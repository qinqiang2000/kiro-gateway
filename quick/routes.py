# -*- coding: utf-8 -*-

"""
FastAPI routes for the Amazon Quick gateway.

Exposes an Anthropic-compatible ``/quick/v1/messages`` endpoint (namespaced so it
coexists with Kiro's ``/v1/messages``). The client → gateway auth is intentionally
permissive: point any Anthropic client at it with a dummy token, e.g.::

    export ANTHROPIC_BASE_URL="http://localhost:8000/quick"
    export ANTHROPIC_AUTH_TOKEN="123"

The gateway ignores that token and authenticates to the Quick backend itself
using the Keycloak credentials from the macOS Keychain.
"""

import json
import uuid
from typing import Any, AsyncGenerator, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from quick.auth import QuickAuthError
from quick.client import QuickAPIError, converse_stream
from quick.config import resolve_model
from quick.converters import anthropic_to_converse
from quick.streaming import (
    ConverseAggregator,
    ConverseToAnthropicSSE,
    EventStreamDecoder,
    _sse,
)

router = APIRouter(tags=["Quick API"])


def _message_id() -> str:
    """Generate an Anthropic-style message id."""
    return f"msg_{uuid.uuid4().hex[:24]}"


def _wants_websearch(payload: Dict[str, Any]) -> bool:
    """True if the request offers a ``web_search`` tool the gateway must execute."""
    for tool in payload.get("tools") or []:
        if isinstance(tool, dict) and tool.get("name") == "web_search":
            return True
    return False


async def _replay_message_as_sse(
    message: Dict[str, Any], message_id: str, model: str
) -> AsyncGenerator[str, None]:
    """Re-emit a fully-assembled Anthropic message as an Anthropic SSE stream.

    Used after the web_search loop (which is internally non-streaming) when the
    client asked for ``stream: true``. Text blocks are chunked; tool_use blocks are
    emitted whole with their input as a single ``input_json_delta``.
    """
    usage = message.get("usage", {}) or {}
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0},
        },
    })
    yield "event: ping\ndata: {\"type\": \"ping\"}\n\n"

    for idx, block in enumerate(message.get("content", [])):
        btype = block.get("type")
        if btype == "text":
            yield _sse("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "text", "text": ""}})
            text = block.get("text", "")
            for i in range(0, len(text), 100):
                yield _sse("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "text_delta", "text": text[i:i + 100]}})
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        elif btype == "tool_use":
            yield _sse("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "tool_use", "id": block.get("id", ""),
                                  "name": block.get("name", ""), "input": {}}})
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "input_json_delta",
                          "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False)}})
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
        elif btype == "thinking":
            yield _sse("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "thinking_delta", "thinking": block.get("thinking", "")}})
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": message.get("stop_reason", "end_turn"), "stop_sequence": None},
        "usage": {"output_tokens": usage.get("output_tokens", 0)}})
    yield _sse("message_stop", {"type": "message_stop"})


def _error_response(status: int, message: str, err_type: str = "api_error") -> JSONResponse:
    """Build an Anthropic-shaped error response."""
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": err_type, "message": message}},
    )


@router.post("/quick/v1/messages")
async def quick_messages(request: Request) -> Any:
    """Anthropic Messages endpoint backed by Amazon Quick."""
    try:
        payload: Dict[str, Any] = await request.json()
    except json.JSONDecodeError:
        return _error_response(400, "Invalid JSON body", "invalid_request_error")

    model = resolve_model(payload.get("model"))
    stream = bool(payload.get("stream", False))
    logger.info("Request to /quick/v1/messages (model={}, stream={})", model, stream)

    try:
        converse_input = anthropic_to_converse(payload)
    except Exception as exc:  # noqa: BLE001 - surface conversion issues to client
        logger.exception("Quick request conversion failed")
        return _error_response(400, f"Request conversion failed: {exc}", "invalid_request_error")

    message_id = _message_id()

    # Gateway-executed web_search (Path B): if the client offered a web_search tool,
    # the Quick/Bedrock backend won't run it, so we drive the model↔search loop here
    # and return the model's final answer. web_fetch and other tools are unaffected.
    if _wants_websearch(payload):
        from quick.agent_loop import run_websearch_loop

        try:
            final, err = await run_websearch_loop(converse_input, message_id, model)
        except QuickAuthError as exc:
            return _error_response(401, str(exc), "authentication_error")
        except QuickAPIError as exc:
            return _error_response(exc.status_code if exc.status_code >= 400 else 502, exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Quick web_search loop failed")
            return _error_response(502, str(exc))
        if err:
            status = 403 if "not authorized" in err.lower() else 502
            logger.warning("Quick backend error (web_search loop): {}", err[:200])
            return _error_response(status, err)
        if stream:
            return StreamingResponse(
                _replay_message_as_sse(final, message_id, model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
        return JSONResponse(content=final)

    if stream:
        return StreamingResponse(
            _stream(converse_input, message_id, model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # Non-streaming: drive the stream endpoint and fold events into one message.
    try:
        aggregator = ConverseAggregator(message_id, model)
        decoder = EventStreamDecoder()
        async for chunk in converse_stream(converse_input):
            for event in decoder.feed(chunk):
                aggregator.add(event)
    except QuickAuthError as exc:
        return _error_response(401, str(exc), "authentication_error")
    except QuickAPIError as exc:
        return _error_response(exc.status_code if exc.status_code >= 400 else 502, exc.message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Quick converse failed")
        return _error_response(502, str(exc))

    if aggregator.error:
        # Quick returns HTTP 200 with an in-stream error frame (e.g. IAM deny).
        status = 403 if "not authorized" in aggregator.error.lower() else 502
        logger.warning("Quick backend error: {}", aggregator.error[:200])
        return _error_response(status, aggregator.error)

    return JSONResponse(content=aggregator.result())


async def _stream(
    converse_input: Dict[str, Any], message_id: str, model: str
) -> AsyncGenerator[str, None]:
    """Drive a streaming Converse call and yield Anthropic SSE strings."""
    translator = ConverseToAnthropicSSE(message_id, model)
    yield translator.start()
    # Anthropic clients expect an early ping.
    yield "event: ping\ndata: {\"type\": \"ping\"}\n\n"

    decoder = EventStreamDecoder()
    try:
        async for chunk in converse_stream(converse_input):
            for event in decoder.feed(chunk):
                for sse in translator.handle(event):
                    yield sse
        for sse in translator.finish():
            yield sse
    except QuickAuthError as exc:
        yield _sse_error(str(exc), "authentication_error")
    except QuickAPIError as exc:
        yield _sse_error(exc.message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Quick streaming failed")
        yield _sse_error(str(exc))


def _sse_error(message: str, err_type: str = "api_error") -> str:
    """Format an SSE error event."""
    return "event: error\ndata: " + json.dumps(
        {"type": "error", "error": {"type": err_type, "message": message}}, ensure_ascii=False
    ) + "\n\n"
