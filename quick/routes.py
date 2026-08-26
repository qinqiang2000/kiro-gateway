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
)

router = APIRouter(tags=["Quick API"])


def _message_id() -> str:
    """Generate an Anthropic-style message id."""
    return f"msg_{uuid.uuid4().hex[:24]}"


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
