# -*- coding: utf-8 -*-

"""
FastAPI routes for the Amazon Quick gateway.

Exposes an Anthropic-compatible ``/quick/v1/messages`` endpoint (namespaced so it
coexists with Kiro's ``/v1/messages``). The client → gateway auth is intentionally
permissive: point any Anthropic client at it with a dummy token, e.g.::

    export ANTHROPIC_BASE_URL="http://localhost:8000/quick"
    export ANTHROPIC_AUTH_TOKEN="123"

The gateway ignores that token and authenticates to the Quick backend itself,
picking an account from the pool (:mod:`quick.pool`) per request: the account with
the most session allowance left, with automatic failover to the next one when a
request is refused on quota or credential grounds. ``/quick/pin/{account}/v1/messages``
serves the same API pinned to one account, for reserved capacity or debugging.
"""

import json
import uuid
from typing import Any, AsyncGenerator, Dict, Optional, Set, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from quick.auth import QuickAuthError
from quick.client import QuickAPIError, converse_stream
from quick.config import QUICK_POOL_MAX_ATTEMPTS, resolve_model
from quick.converters import anthropic_to_converse
from quick.pool import Account, pool
from quick.streaming import (
    ConverseAggregator,
    ConverseToAnthropicSSE,
    EventStreamDecoder,
    _sse,
    backend_error,
)
from quick.usage_watch import current_account

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


# ==================================================================================================
# Account selection + failover
# ==================================================================================================

def _pick(pinned: str, exclude: Set[str]) -> Optional[Account]:
    """Choose the account for this attempt.

    Args:
        pinned: A specific account name (``/quick/pin/{account}/…``), or empty for
            normal pool selection.
        exclude: Accounts already tried for this request.

    Returns:
        The account to try, or ``None`` when there is nothing left to try.
    """
    if pinned:
        account = pool.get(pinned)
        return account if account is not None and account.name not in exclude else None
    return pool.select(exclude=exclude)


def _should_failover(status: Optional[int], message: str) -> bool:
    """Whether a failure is worth retrying on a different account.

    A malformed request fails identically everywhere — retrying it just burns a
    second account's quota and doubles the latency of an error the client has to fix.
    Quota, throttling, credential and backend failures are account-specific and do
    fail over.
    """
    text = (message or "").lower()
    if status == 400 or "validation" in text or "improperly formed" in text:
        return False
    return True


def _backend_error_status(message: str) -> int:
    """Map an in-stream backend error to an HTTP status (Quick answers 200 for these)."""
    return 403 if "not authorized" in (message or "").lower() else 502


def _pool_exhausted_message(pinned: str) -> str:
    """Explain, actionably, why no account could serve the request."""
    if pinned:
        known = ", ".join(a.name for a in pool.accounts()) or "(none)"
        return f"Quick 账号 '{pinned}' 不存在或不可用。已知账号：{known}"
    snapshot = pool.snapshot()
    detail = "；".join(
        f"{a['name']}={a['status']}" + (f"({a['cooldown_reason'] or a['disabled_reason']})"
                                        if (a["cooldown_reason"] or a["disabled_reason"]) else "")
        for a in snapshot["accounts"]
    ) or "账号池为空"
    return f"Quick 账号池无可用账号：{detail}"


async def _run_aggregated(
    converse_input: Dict[str, Any],
    message_id: str,
    model: str,
    account: Account,
    websearch: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Run one non-streaming attempt on ``account``; return (message, backend_error)."""
    if websearch:
        from quick.agent_loop import run_websearch_loop

        return await run_websearch_loop(converse_input, message_id, model, account)

    aggregator = ConverseAggregator(message_id, model)
    decoder = EventStreamDecoder()
    async for chunk in converse_stream(converse_input, account):
        for event in decoder.feed(chunk):
            aggregator.add(event)
    if aggregator.error:
        return None, aggregator.error
    return aggregator.result(), None


async def _serve_aggregated(
    converse_input: Dict[str, Any],
    message_id: str,
    model: str,
    pinned: str,
    websearch: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[JSONResponse]]:
    """Drive the non-streaming path with pool failover.

    Returns:
        ``(message, None)`` on success or ``(None, error_response)`` once every
        eligible account has been tried.
    """
    excluded: Set[str] = set()
    status, message = 503, ""
    for _attempt in range(max(1, QUICK_POOL_MAX_ATTEMPTS)):
        account = _pick(pinned, excluded)
        if account is None:
            break
        current_account.set(account.name)
        pool.begin(account)
        try:
            final, err = await _run_aggregated(converse_input, message_id, model,
                                               account, websearch)
            if err is None:
                pool.note_success(account)
                return final, None
            status, message = _backend_error_status(err), err
        except QuickAuthError as exc:
            status, message = 401, str(exc)
        except QuickAPIError as exc:
            status, message = (exc.status_code if exc.status_code >= 400 else 502), exc.message
        except Exception as exc:  # noqa: BLE001 - surface as a gateway error
            logger.exception("Quick request failed on account '{}'", account.name)
            status, message = 502, str(exc)
        finally:
            pool.end(account)

        if not _should_failover(status, message):
            return None, _error_response(status, message)
        pool.note_failure(account, status, message)
        excluded.add(account.name)
        logger.warning("Quick account '{}' failed ({}); trying the next account.",
                       account.name, message[:160])

    if not message:
        return None, _error_response(503, _pool_exhausted_message(pinned), "overloaded_error")
    err_type = "authentication_error" if status == 401 else "api_error"
    return None, _error_response(status, message, err_type)


# ==================================================================================================
# Endpoints
# ==================================================================================================

@router.post("/quick/v1/messages")
async def quick_messages(request: Request) -> Any:
    """Anthropic Messages endpoint backed by the Amazon Quick account pool."""
    return await _handle(request, pinned="")


@router.post("/quick/pin/{account}/v1/messages")
async def quick_messages_pinned(account: str, request: Request) -> Any:
    """Same endpoint, pinned to one pool account (reserved capacity / debugging)."""
    return await _handle(request, pinned=account)


@router.get("/quick/pool")
async def quick_pool_status() -> Any:
    """Per-account quota and health, as JSON (no credential material)."""
    pool.discover()
    return JSONResponse(content=pool.snapshot())


async def _handle(request: Request, pinned: str) -> Any:
    """Shared request path for the pooled and pinned endpoints."""
    try:
        payload: Dict[str, Any] = await request.json()
    except json.JSONDecodeError:
        return _error_response(400, "Invalid JSON body", "invalid_request_error")

    model = resolve_model(payload.get("model"))
    stream = bool(payload.get("stream", False))
    logger.info("Request to /quick/v1/messages (model={}, stream={}, account={})",
                model, stream, pinned or "pool")

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
        final, error = await _serve_aggregated(converse_input, message_id, model,
                                               pinned, websearch=True)
        if error is not None:
            return error
        if stream:
            return StreamingResponse(
                _replay_message_as_sse(final, message_id, model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
        return JSONResponse(content=final)

    if stream:
        return StreamingResponse(
            _stream(converse_input, message_id, model, pinned),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    final, error = await _serve_aggregated(converse_input, message_id, model,
                                           pinned, websearch=False)
    return error if error is not None else JSONResponse(content=final)


async def _stream(
    converse_input: Dict[str, Any], message_id: str, model: str, pinned: str = ""
) -> AsyncGenerator[str, None]:
    """Drive a streaming Converse call with pool failover and yield Anthropic SSE.

    ``message_start`` is held back until the first real event arrives: once a byte of
    SSE has been written the client owns the stream and a switch of account would be
    visible as an error. Failing over *before* that point keeps a cooling-down account
    invisible to the caller.
    """
    excluded: Set[str] = set()
    status, message = 503, ""
    for _attempt in range(max(1, QUICK_POOL_MAX_ATTEMPTS)):
        account = _pick(pinned, excluded)
        if account is None:
            break
        current_account.set(account.name)
        translator = ConverseToAnthropicSSE(message_id, model)
        decoder = EventStreamDecoder()
        emitted = False
        failure = ""
        pool.begin(account)
        try:
            async for chunk in converse_stream(converse_input, account):
                for event in decoder.feed(chunk):
                    if not emitted:
                        # Quick reports backend failures as an in-stream error frame
                        # under HTTP 200 — catch it while failing over is still free.
                        err = backend_error(event)
                        if err:
                            failure = err
                            break
                        yield translator.start()
                        yield "event: ping\ndata: {\"type\": \"ping\"}\n\n"
                        emitted = True
                    for sse in translator.handle(event):
                        yield sse
                if failure:
                    break
            if not failure:
                for sse in translator.finish():
                    yield sse
                pool.note_success(account)
                return
            status = _backend_error_status(failure)
        except QuickAuthError as exc:
            status, failure = 401, str(exc)
        except QuickAPIError as exc:
            status, failure = (exc.status_code if exc.status_code >= 400 else 502), exc.message
        except Exception as exc:  # noqa: BLE001
            logger.exception("Quick streaming failed on account '{}'", account.name)
            status, failure = 502, str(exc)
        finally:
            pool.end(account)

        message = failure
        if emitted:
            # Too late to switch accounts — the client is mid-stream.
            yield _sse_error(failure)
            return
        if not _should_failover(status, failure):
            yield _sse_error(failure, "invalid_request_error" if status == 400 else "api_error")
            return
        pool.note_failure(account, status, failure)
        excluded.add(account.name)
        logger.warning("Quick account '{}' failed before first token ({}); "
                       "failing over.", account.name, failure[:160])

    yield _sse_error(message or _pool_exhausted_message(pinned),
                     "authentication_error" if status == 401 else "overloaded_error")


def _sse_error(message: str, err_type: str = "api_error") -> str:
    """Format an SSE error event."""
    return "event: error\ndata: " + json.dumps(
        {"type": "error", "error": {"type": err_type, "message": message}}, ensure_ascii=False
    ) + "\n\n"
