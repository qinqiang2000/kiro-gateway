# -*- coding: utf-8 -*-

"""
Translate Amazon Bedrock ``ConverseStream`` output into Anthropic SSE.

The Quick DataPlane returns ``application/vnd.amazon.eventstream`` — the standard
AWS event-stream binary framing, each frame carrying a JSON Bedrock
``ConverseStream`` event. This module:

1. :func:`iter_eventstream_messages` — incrementally decodes the binary framing.
2. :class:`ConverseToAnthropicSSE` — a small state machine that turns the
   decoded Converse events into Anthropic ``/v1/messages`` streaming events.

Frame layout (all integers big-endian):
    total_len(4) | headers_len(4) | prelude_crc(4) | headers | payload | msg_crc(4)
Header:
    name_len(1) | name | value_type(1) | [value...]
"""

import json
import struct
from typing import Any, Dict, Generator, Iterator, List, Optional

from quick.usage_watch import observe_event

JsonDict = Dict[str, Any]


# ==================================================================================================
# AWS event-stream binary decoder
# ==================================================================================================

def _parse_headers(buf: bytes) -> Dict[str, Any]:
    """Parse the header block of one event-stream frame into a dict."""
    headers: Dict[str, Any] = {}
    i = 0
    n = len(buf)
    while i < n:
        name_len = buf[i]
        i += 1
        name = buf[i : i + name_len].decode("utf-8", "replace")
        i += name_len
        vtype = buf[i]
        i += 1
        if vtype == 7:  # string
            (vlen,) = struct.unpack_from(">H", buf, i)
            i += 2
            value: Any = buf[i : i + vlen].decode("utf-8", "replace")
            i += vlen
        elif vtype == 6:  # byte array
            (vlen,) = struct.unpack_from(">H", buf, i)
            i += 2
            value = buf[i : i + vlen]
            i += vlen
        elif vtype == 0:  # bool true
            value = True
        elif vtype == 1:  # bool false
            value = False
        elif vtype == 4:  # int32
            (value,) = struct.unpack_from(">i", buf, i)
            i += 4
        elif vtype == 5:  # int64 / timestamp
            (value,) = struct.unpack_from(">q", buf, i)
            i += 8
        else:  # pragma: no cover - other types unused by Bedrock
            break
        headers[name] = value
    return headers


class EventStreamDecoder:
    """Incremental (push-style) decoder for AWS event-stream framing.

    Feed raw byte chunks as they arrive; :meth:`feed` returns the list of
    complete Bedrock event dicts decoded so far, retaining any partial trailing
    frame in an internal buffer for the next call. This is the correct way to
    decode a chunked HTTP body where frames span chunk boundaries.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> List[JsonDict]:
        """Add ``chunk`` and return any newly-complete event dicts."""
        events: List[JsonDict] = []
        if chunk:
            self._buffer.extend(chunk)
        buffer = self._buffer
        while len(buffer) >= 12:
            total_len, headers_len = struct.unpack_from(">II", buffer, 0)
            if total_len < 16 or total_len > 16 * 1024 * 1024:
                # Desync / corrupt length: drop one byte and resync.
                del buffer[0]
                continue
            if len(buffer) < total_len:
                break  # wait for more bytes
            header_start = 12
            header_end = header_start + headers_len
            payload_end = total_len - 4
            headers = _parse_headers(bytes(buffer[header_start:header_end]))
            payload_bytes = bytes(buffer[header_end:payload_end])
            del buffer[:total_len]

            payload: JsonDict = {}
            if payload_bytes:
                try:
                    payload = json.loads(payload_bytes.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    payload = {"_raw": payload_bytes.decode("utf-8", "replace")}
            events.append(
                {
                    "event_type": headers.get(":event-type"),
                    "message_type": headers.get(":message-type"),
                    "exception_type": headers.get(":exception-type"),
                    "payload": payload,
                }
            )
        return events


def iter_eventstream_messages(
    chunks: Iterator[bytes],
) -> Generator[JsonDict, None, None]:
    """Decode a finite iterator of byte chunks into Bedrock event dicts.

    Convenience wrapper over :class:`EventStreamDecoder` for the non-async /
    testing case.

    Args:
        chunks: Iterator of raw byte chunks.

    Yields:
        Bedrock event dicts (see :class:`EventStreamDecoder`).
    """
    decoder = EventStreamDecoder()
    for chunk in chunks:
        for event in decoder.feed(chunk):
            yield event


# ==================================================================================================
# Bedrock Converse events -> Anthropic SSE
# ==================================================================================================

def _unwrap_bedrock_event(event: JsonDict) -> JsonDict:
    """Normalize a Quick ``bedrockStreamEvent`` frame to a plain Converse event.

    Quick wraps every Bedrock ConverseStream event: the AWS event-stream header
    ``:event-type`` is always ``bedrockStreamEvent`` and the JSON payload is
    ``{"eventType": "<real>", "payload": {<real event data>}}``. Direct Bedrock
    frames (used in tests) already carry the real type; both are handled.
    """
    payload = event.get("payload") or {}
    if event.get("event_type") == "bedrockStreamEvent" and isinstance(payload, dict) and "eventType" in payload:
        # The wrapper also carries the account's entitlement snapshot (``usageSummary``,
        # on ``metadata`` frames) — hand it to the session watch before dropping it.
        observe_event(event)
        inner = payload.get("payload") or {}
        etype = payload.get("eventType")
        # Surface errors and modeled exceptions carried inside the wrapper. Quick
        # returns HTTP 200 even for backend failures (e.g. an IAM "explicit deny"
        # on an unauthorized model) as an ``{"eventType":"error"}`` frame.
        is_err = isinstance(etype, str) and (etype == "error" or etype.endswith("Exception"))
        return {"event_type": etype, "message_type": "event",
                "exception_type": etype if is_err else None, "payload": inner}
    return event


def _error_message(payload: JsonDict) -> str:
    """Extract a human-readable message from an error/exception event payload."""
    if not isinstance(payload, dict):
        return str(payload)
    return str(payload.get("error") or payload.get("message") or payload.get("Message") or payload)


_STOP_REASON_MAP = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "content_filtered": "end_turn",
    "guardrail_intervened": "end_turn",
}


def _sse(event: str, data: JsonDict) -> str:
    """Format a single Anthropic SSE event."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class ConverseToAnthropicSSE:
    """Stateful translator from Bedrock Converse events to Anthropic SSE strings.

    Converse emits ``contentBlockStart`` only for tool-use blocks; text and
    reasoning blocks start implicitly at their first delta. We therefore open the
    matching Anthropic content block lazily on the first delta we see for an index.
    """

    def __init__(self, message_id: str, model: str) -> None:
        self.message_id = message_id
        self.model = model
        # Maps Converse contentBlockIndex -> Anthropic block type currently open.
        self._open_blocks: Dict[int, str] = {}
        self._input_tokens = 0
        self._output_tokens = 0
        self._stop_reason: Optional[str] = None
        self._started = False

    def start(self) -> str:
        """Emit ``message_start``."""
        self._started = True
        return _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def _open_text(self, index: int) -> str:
        self._open_blocks[index] = "text"
        return _sse(
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
        )

    def _open_thinking(self, index: int) -> str:
        self._open_blocks[index] = "thinking"
        return _sse(
            "content_block_start",
            {"type": "content_block_start", "index": index,
             "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        )

    def handle(self, event: JsonDict) -> List[str]:
        """Translate one decoded Converse event into zero or more SSE strings."""
        event = _unwrap_bedrock_event(event)
        etype = event.get("event_type")
        payload = event.get("payload") or {}
        out: List[str] = []

        if event.get("exception_type"):
            msg = _error_message(payload)
            out.append(_sse("error", {"type": "error",
                                      "error": {"type": "api_error", "message": msg}}))
            return out

        if etype == "messageStart":
            # role always assistant; message_start already emitted by caller.
            return out

        if etype == "contentBlockStart":
            index = payload.get("contentBlockIndex", 0)
            start = payload.get("start", {})
            if "toolUse" in start:
                tu = start["toolUse"]
                self._open_blocks[index] = "tool_use"
                out.append(_sse("content_block_start", {
                    "type": "content_block_start", "index": index,
                    "content_block": {"type": "tool_use", "id": tu.get("toolUseId", ""),
                                      "name": tu.get("name", ""), "input": {}},
                }))
            return out

        if etype == "contentBlockDelta":
            index = payload.get("contentBlockIndex", 0)
            delta = payload.get("delta", {})
            if "text" in delta:
                if self._open_blocks.get(index) != "text":
                    out.append(self._open_text(index))
                out.append(_sse("content_block_delta", {
                    "type": "content_block_delta", "index": index,
                    "delta": {"type": "text_delta", "text": delta["text"]},
                }))
            elif "toolUse" in delta:
                out.append(_sse("content_block_delta", {
                    "type": "content_block_delta", "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": delta["toolUse"].get("input", "")},
                }))
            elif "reasoningContent" in delta:
                rc = delta["reasoningContent"]
                if self._open_blocks.get(index) != "thinking":
                    out.append(self._open_thinking(index))
                if "text" in rc:
                    out.append(_sse("content_block_delta", {
                        "type": "content_block_delta", "index": index,
                        "delta": {"type": "thinking_delta", "thinking": rc["text"]},
                    }))
                elif "signature" in rc:
                    out.append(_sse("content_block_delta", {
                        "type": "content_block_delta", "index": index,
                        "delta": {"type": "signature_delta", "signature": rc["signature"]},
                    }))
            return out

        if etype == "contentBlockStop":
            index = payload.get("contentBlockIndex", 0)
            if index in self._open_blocks:
                del self._open_blocks[index]
                out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
            return out

        if etype == "messageStop":
            raw = payload.get("stopReason", "end_turn")
            self._stop_reason = _STOP_REASON_MAP.get(raw, "end_turn")
            return out

        if etype == "metadata":
            usage = payload.get("usage", {})
            self._input_tokens = usage.get("inputTokens", self._input_tokens)
            self._output_tokens = usage.get("outputTokens", self._output_tokens)
            return out

        return out

    def finish(self) -> List[str]:
        """Close any dangling blocks and emit ``message_delta`` + ``message_stop``."""
        out: List[str] = []
        for index in list(self._open_blocks.keys()):
            del self._open_blocks[index]
            out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
        out.append(_sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self._stop_reason or "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": self._output_tokens},
        }))
        out.append(_sse("message_stop", {"type": "message_stop"}))
        return out


class ConverseAggregator:
    """Accumulate decoded Converse stream events into one Anthropic message.

    Used for non-streaming responses: the gateway calls the stream endpoint and
    folds the events into a single Anthropic Messages response.
    """

    def __init__(self, message_id: str, model: str) -> None:
        self.message_id = message_id
        self.model = model
        self._blocks: Dict[int, JsonDict] = {}
        self._order: List[int] = []
        self._tool_json: Dict[int, str] = {}
        self._stop_reason = "end_turn"
        self._input_tokens = 0
        self._output_tokens = 0
        self.error: Optional[str] = None

    def add(self, event: JsonDict) -> None:
        """Fold one (possibly wrapped) event into the accumulating message."""
        event = _unwrap_bedrock_event(event)
        etype = event.get("event_type")
        payload = event.get("payload") or {}
        if event.get("exception_type"):
            self.error = _error_message(payload)
            return
        if etype == "contentBlockStart":
            idx = payload.get("contentBlockIndex", 0)
            start = payload.get("start", {})
            if "toolUse" in start:
                tu = start["toolUse"]
                self._blocks[idx] = {"type": "tool_use", "id": tu.get("toolUseId", ""),
                                     "name": tu.get("name", ""), "input": {}}
                self._tool_json[idx] = ""
                self._order.append(idx)
        elif etype == "contentBlockDelta":
            idx = payload.get("contentBlockIndex", 0)
            delta = payload.get("delta", {})
            if "text" in delta:
                b = self._blocks.get(idx)
                if not b or b.get("type") != "text":
                    b = {"type": "text", "text": ""}
                    self._blocks[idx] = b
                    self._order.append(idx)
                b["text"] += delta["text"]
            elif "toolUse" in delta:
                self._tool_json[idx] = self._tool_json.get(idx, "") + delta["toolUse"].get("input", "")
            elif "reasoningContent" in delta:
                rc = delta["reasoningContent"]
                b = self._blocks.get(idx)
                if not b or b.get("type") != "thinking":
                    b = {"type": "thinking", "thinking": "", "signature": ""}
                    self._blocks[idx] = b
                    self._order.append(idx)
                if "text" in rc:
                    b["thinking"] += rc["text"]
                elif "signature" in rc:
                    b["signature"] = rc["signature"]
        elif etype == "messageStop":
            self._stop_reason = _STOP_REASON_MAP.get(payload.get("stopReason", "end_turn"), "end_turn")
        elif etype == "metadata":
            usage = payload.get("usage", {})
            self._input_tokens = usage.get("inputTokens", self._input_tokens)
            self._output_tokens = usage.get("outputTokens", self._output_tokens)

    def result(self) -> JsonDict:
        """Return the assembled Anthropic Messages response."""
        content: List[JsonDict] = []
        for idx in self._order:
            b = self._blocks.get(idx)
            if not b:
                continue
            if b["type"] == "tool_use":
                raw = self._tool_json.get(idx, "")
                try:
                    b["input"] = json.loads(raw) if raw else {}
                except ValueError:
                    b["input"] = {}
            content.append(b)
        return {
            "id": self.message_id,
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": content or [{"type": "text", "text": ""}],
            "stop_reason": self._stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": self._input_tokens, "output_tokens": self._output_tokens},
        }


def converse_response_to_anthropic(resp: JsonDict, message_id: str, model: str) -> JsonDict:
    """Convert a non-streaming Bedrock ``Converse`` response to an Anthropic message.

    Args:
        resp: The parsed Converse response (``output``, ``stopReason``, ``usage``).
        message_id: The Anthropic message id to report.
        model: The model name to report.

    Returns:
        An Anthropic Messages response dict.
    """
    content: List[JsonDict] = []
    message = (resp.get("output") or {}).get("message") or {}
    for block in message.get("content", []):
        if "text" in block:
            content.append({"type": "text", "text": block["text"]})
        elif "toolUse" in block:
            tu = block["toolUse"]
            content.append({"type": "tool_use", "id": tu.get("toolUseId", ""),
                            "name": tu.get("name", ""), "input": tu.get("input", {})})
        elif "reasoningContent" in block:
            rt = block["reasoningContent"].get("reasoningText", {})
            content.append({"type": "thinking", "thinking": rt.get("text", ""),
                            "signature": rt.get("signature", "")})
    usage = resp.get("usage", {})
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": _STOP_REASON_MAP.get(resp.get("stopReason", "end_turn"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
        },
    }
