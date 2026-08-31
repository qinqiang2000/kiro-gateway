# -*- coding: utf-8 -*-

"""
Convert Anthropic Messages API requests into Amazon Bedrock ``Converse`` input.

The Quick DataPlane proxies Bedrock ``Converse`` / ``ConverseStream``, whose
request schema is public and stable. This module maps the Anthropic
``/v1/messages`` body onto that schema. The only Quick-specific unknown (the RPC
envelope / ``X-Amz-Target``) lives in :mod:`quick.client`, not here.

Anthropic content block  ->  Bedrock Converse content block
    text                 ->  {"text": ...}
    image (base64)       ->  {"image": {"format", "source": {"bytes"}}}
    tool_use             ->  {"toolUse": {"toolUseId", "name", "input"}}
    tool_result          ->  {"toolResult": {"toolUseId", "content", "status"}}
    thinking             ->  {"reasoningContent": {"reasoningText": {"text", "signature"}}}
"""

import base64
from typing import Any, Dict, List, Optional, Union

from quick.config import resolve_model

JsonDict = Dict[str, Any]


# Bedrock's equivalent of an Anthropic ``cache_control`` breakpoint. Anthropic marks
# the block *up to and including which* the prefix should be cached; Bedrock wants a
# separator right after it — so a marked block becomes "block, cachePoint".
CACHE_POINT: JsonDict = {"cachePoint": {"type": "default"}}

# Bedrock rejects a request with more than this many cache points outright
# (ValidationException 400, verified live). Anthropic's own limit is the same, so a
# well-behaved client never trips it — but a 400 here would take down a real request,
# so the converter caps instead of trusting the client.
MAX_CACHE_POINTS: int = 4


def _is_cached(block: object) -> bool:
    """True when an Anthropic block carries a ``cache_control`` breakpoint."""
    return isinstance(block, dict) and isinstance(block.get("cache_control"), dict)


def _cap_cache_points(result: JsonDict, limit: int = MAX_CACHE_POINTS) -> None:
    """Drop the earliest cache points until at most ``limit`` remain, in place.

    The prefix order Bedrock caches in is tools → system → messages, so the *last*
    breakpoints cover the longest prefixes and are worth the most; an earlier one is
    already contained in every later prefix. Hence extras are dropped from the front.
    """
    lists: List[List[JsonDict]] = []
    tool_config = result.get("toolConfig")
    if isinstance(tool_config, dict) and isinstance(tool_config.get("tools"), list):
        lists.append(tool_config["tools"])
    if isinstance(result.get("system"), list):
        lists.append(result["system"])
    for message in result.get("messages") or []:
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            lists.append(message["content"])

    total = sum(1 for blocks in lists for b in blocks if "cachePoint" in b)
    excess = total - limit
    if excess <= 0:
        return
    for blocks in lists:
        i = 0
        while i < len(blocks) and excess > 0:
            if "cachePoint" in blocks[i]:
                del blocks[i]
                excess -= 1
                continue
            i += 1
        if excess <= 0:
            break


def _system_to_converse(system: Union[None, str, List[JsonDict]]) -> List[JsonDict]:
    """Convert an Anthropic ``system`` field into Converse ``system`` blocks."""
    if not system:
        return []
    if isinstance(system, str):
        return [{"text": system}] if system.strip() else []
    blocks: List[JsonDict] = []
    for item in system:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if text:
                blocks.append({"text": text})
                if _is_cached(item):
                    blocks.append(dict(CACHE_POINT))
        elif isinstance(item, str) and item:
            blocks.append({"text": item})
    return blocks


def _image_block(source: JsonDict) -> Optional[JsonDict]:
    """Convert an Anthropic image source into a Converse image block.

    The DataPlane request is serialized as JSON (see :func:`quick.client._envelope`),
    so ``source.bytes`` must be a base64 *string*, not raw ``bytes`` (json.dumps
    cannot serialize bytes). Anthropic already sends base64, so we validate and pass
    the string straight through rather than decoding and re-encoding.
    """
    if source.get("type") != "base64":
        # URL images are not supported by Converse's inline bytes model.
        return None
    media_type = source.get("media_type", "")
    fmt = media_type.split("/")[-1].lower() if "/" in media_type else media_type.lower()
    # Converse accepts: png | jpeg | gif | webp
    if fmt == "jpg":
        fmt = "jpeg"
    data = source.get("data", "")
    if not data or not isinstance(data, str):
        return None
    # Validate it is real base64 (reject garbage) but keep the string form for JSON.
    try:
        base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        return None
    return {"image": {"format": fmt, "source": {"bytes": data}}}


# Bedrock rejects an empty text block outright: "messages: text content blocks must be
# non-empty" (HTTP 400, seen live 2026-08-31 — and, arriving in-stream, it cost two
# accounts a cooldown before routes.py learned to read it as a client error). A block
# with no text carries nothing, so dropping it is an API-level fix, not a content edit
# — but a message with no blocks at all is equally invalid, so that one needs a stand-in.
# Deliberately NOT "Continue": kiro/converters_core.py learned the hard way that a
# placeholder readable as a user instruction makes models answer it (upstream #171).
EMPTY_PLACEHOLDER: str = "(empty placeholder)"


def _has_text(block: JsonDict) -> bool:
    """True when a converted block carries text Bedrock will accept."""
    return bool(str(block.get("text", "")).strip())


def _tool_result_content(content: Union[str, List[JsonDict], None]) -> List[JsonDict]:
    """Convert Anthropic tool_result content into Converse toolResult content."""
    if content is None:
        return [{"text": EMPTY_PLACEHOLDER}]
    if isinstance(content, str):
        return [{"text": content}] if content.strip() else [{"text": EMPTY_PLACEHOLDER}]
    out: List[JsonDict] = []
    for block in content:
        if not isinstance(block, dict):
            out.append({"text": str(block)})
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if str(text).strip():
                out.append({"text": text})
        elif btype == "image":
            img = _image_block(block.get("source", {}))
            if img:
                out.append(img)
        else:
            # Fall back to a JSON blob for anything structured.
            out.append({"json": block})
    return out or [{"text": EMPTY_PLACEHOLDER}]


def _content_to_converse(content: Union[str, List[JsonDict]]) -> List[JsonDict]:
    """Convert an Anthropic message ``content`` into Converse content blocks."""
    if isinstance(content, str):
        return [{"text": content}] if content.strip() else [{"text": EMPTY_PLACEHOLDER}]

    blocks: List[JsonDict] = []
    for block in content:
        if not isinstance(block, dict):
            blocks.append({"text": str(block)})
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if str(text).strip():
                blocks.append({"text": text})
        elif btype == "image":
            img = _image_block(block.get("source", {}))
            if img:
                blocks.append(img)
        elif btype == "tool_use":
            blocks.append(
                {
                    "toolUse": {
                        "toolUseId": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": block.get("input", {}),
                    }
                }
            )
        elif btype == "tool_result":
            tr: JsonDict = {
                "toolUseId": block.get("tool_use_id", ""),
                "content": _tool_result_content(block.get("content")),
            }
            if block.get("is_error"):
                tr["status"] = "error"
            blocks.append({"toolResult": tr})
        elif btype == "thinking":
            blocks.append(
                {
                    "reasoningContent": {
                        "reasoningText": {
                            "text": block.get("thinking", ""),
                            **({"signature": block["signature"]} if block.get("signature") else {}),
                        }
                    }
                }
            )
        elif btype == "redacted_thinking":
            blocks.append({"reasoningContent": {"redactedContent": block.get("data", "").encode()}})
        # Unknown block types are dropped (transparent-proxy: don't invent content).
        if _is_cached(block) and blocks and "cachePoint" not in blocks[-1]:
            blocks.append(dict(CACHE_POINT))
    return blocks or [{"text": EMPTY_PLACEHOLDER}]


def _messages_to_converse(messages: List[JsonDict]) -> List[JsonDict]:
    """Convert Anthropic messages into Converse messages, merging consecutive same-role turns."""
    converse: List[JsonDict] = []
    for msg in messages:
        role = msg.get("role", "user")
        if role not in ("user", "assistant"):
            role = "user"
        blocks = _content_to_converse(msg.get("content", ""))
        if converse and converse[-1]["role"] == role:
            # Converse rejects consecutive messages with the same role; merge them.
            converse[-1]["content"].extend(blocks)
        else:
            converse.append({"role": role, "content": blocks})
    return converse


def _tools_to_converse(
    tools: Optional[List[JsonDict]], tool_choice: Optional[JsonDict]
) -> Optional[JsonDict]:
    """Convert Anthropic tools + tool_choice into a Converse ``toolConfig``.

    Native server-side tools (identified by a ``type`` field, e.g.
    ``web_search_20250305``) carry no ``input_schema``. For ``web_search`` the
    gateway executes the search itself (see :mod:`quick.websearch`), so we give it
    a concrete ``{query}`` schema here — otherwise the model gets an empty object
    schema and may not emit a usable query.
    """
    if not tools:
        return None
    spec_tools: List[JsonDict] = []
    for tool in tools:
        name = tool.get("name")
        if not name:
            continue
        # Bedrock Converse requires toolSpec.description length >= 1, but the
        # Anthropic protocol allows an empty/omitted description. Fall back to the
        # tool name (semantic, non-misleading) so an empty description doesn't get
        # the whole request rejected with a Bedrock validation error.
        description = (tool.get("description") or "").strip() or name
        input_schema = tool.get("input_schema")
        is_native = bool(tool.get("type"))  # server-side tools carry a `type`
        if name == "web_search" and (is_native or not input_schema):
            # Gateway-executed web_search: give the model a real query schema.
            description = description if description != name else "Search the web for current information."
            input_schema = {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The search query."}},
                "required": ["query"],
            }
        spec_tools.append(
            {
                "toolSpec": {
                    "name": name,
                    "description": description,
                    "inputSchema": {"json": input_schema or {"type": "object"}},
                }
            }
        )
    if not spec_tools:
        return None
    # A cache_control on any tool caches the whole tool block (Bedrock has one
    # cache point for the tool list, at its end).
    if any(_is_cached(tool) for tool in tools):
        spec_tools.append(dict(CACHE_POINT))
    tool_config: JsonDict = {"tools": spec_tools}
    if tool_choice:
        ctype = tool_choice.get("type")
        if ctype == "auto":
            tool_config["toolChoice"] = {"auto": {}}
        elif ctype == "any":
            tool_config["toolChoice"] = {"any": {}}
        elif ctype == "tool" and tool_choice.get("name"):
            tool_config["toolChoice"] = {"tool": {"name": tool_choice["name"]}}
    return tool_config


def anthropic_to_converse(payload: JsonDict) -> JsonDict:
    """Build a Bedrock ``Converse`` input dict from an Anthropic Messages request.

    Args:
        payload: The parsed Anthropic ``/v1/messages`` request body.

    Returns:
        A dict with ``modelId``, ``messages`` and optional ``system``,
        ``inferenceConfig``, ``toolConfig`` and ``additionalModelRequestFields``
        (Bedrock ``Converse`` shape). :mod:`quick.client` decides the final RPC
        envelope around this. Client ``cache_control`` breakpoints are passed
        through as Bedrock ``cachePoint`` separators (capped at
        :data:`MAX_CACHE_POINTS`).
    """
    model = resolve_model(payload.get("model"))
    result: JsonDict = {
        "modelId": model,
        "messages": _messages_to_converse(payload.get("messages", [])),
    }

    system_blocks = _system_to_converse(payload.get("system"))
    if system_blocks:
        result["system"] = system_blocks

    inference: JsonDict = {}
    if payload.get("max_tokens") is not None:
        inference["maxTokens"] = payload["max_tokens"]
    if payload.get("temperature") is not None:
        inference["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        inference["topP"] = payload["top_p"]
    if payload.get("stop_sequences"):
        inference["stopSequences"] = payload["stop_sequences"]
    if inference:
        result["inferenceConfig"] = inference

    tool_config = _tools_to_converse(payload.get("tools"), payload.get("tool_choice"))
    if tool_config:
        result["toolConfig"] = tool_config

    additional: JsonDict = {}
    if payload.get("top_k") is not None:
        additional["top_k"] = payload["top_k"]
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        # Quick's Bedrock models (e.g. opus-4-8) reject Anthropic's
        # ``thinking.type=enabled`` + ``budget_tokens``. They use adaptive
        # thinking with ``output_config.effort`` (the app's Low/Med/High). Map
        # the client's budget onto an effort level.
        additional["thinking"] = {"type": "adaptive"}
        result["_quick_output_config"] = {"effort": _budget_to_effort(thinking.get("budget_tokens"))}
    if additional:
        result["additionalModelRequestFields"] = additional

    # Cache points are passed through from the client's cache_control markers; more
    # than four is a hard 400 from Bedrock, so trim before it can reach the wire.
    _cap_cache_points(result)

    return result


def _budget_to_effort(budget: Optional[int]) -> str:
    """Map an Anthropic thinking ``budget_tokens`` onto Quick's effort level.

    Quick exposes three thinking levels (Low / Med / High). Clients (e.g. Claude
    Code) instead send a token budget, so bucket it:

    * ``<= 4096``  -> ``low``
    * ``<= 16384`` -> ``medium``
    * else         -> ``high``
    """
    if not budget:
        return "medium"
    if budget <= 4096:
        return "low"
    if budget <= 16384:
        return "medium"
    return "high"
