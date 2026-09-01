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
import re
import uuid
from typing import Any, AsyncGenerator, Dict, Optional, Set, Tuple

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from quick.auth import QuickAuthError
from quick.client import QuickAPIError, converse_stream
from quick.config import QUICK_POOL_MAX_ATTEMPTS, QUICK_STREAM_MAX_RESUMES, resolve_model
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


# A response with no events at all is not an answer. Serving it would hand the client
# a well-formed blank message — the failure mode that hid Quick's entitlement block for
# six minutes of live traffic — so it is treated as a backend error and failed over.
EMPTY_STREAM_ERROR = "Quick returned an empty stream (no events)"

# Why a bench gets withdrawn: the next account answered the same request with the same
# error, so the first account was never what was wrong. Benching it (and the failure
# streak that doubles the next cooldown) was a misdiagnosis — and this is the general
# form of the empty-text-block incident, which needed a keyword to catch it in advance.
_SAME_FAILURE = "the same failure on account '{account}' — the request, not the account"


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


def _failure_fingerprint(status: Optional[int], message: str) -> str:
    """A comparable shape for one failure, ignoring what differs between two of them.

    Request ids, timings and counters make two copies of the *same* failure look
    different, so they are stripped: what is left is the status plus the wording, which
    is what tells "this account is in trouble" apart from "this request is".

    Args:
        status: HTTP status, when the failure had one.
        message: The error text.

    Returns:
        A short key that equal failures share.
    """
    text = (message or "").lower()
    text = re.sub(r"\b[0-9a-f]{6,}(?:-[0-9a-f]{4,})*\b", "", text)   # ids, uuids
    text = re.sub(r"\d+", "", text)                                   # counts, timings
    text = re.sub(r"[^a-z ]+", " ", text)
    return f"{status}|{' '.join(text.split())[:160]}"


def _continuation_input(
    converse_input: Dict[str, Any], text_so_far: str
) -> Optional[Dict[str, Any]]:
    """Build the request that continues an answer the client has already half-received.

    A dropped stream leaves the client holding real text, so the retry must not start
    the answer over: the delivered text goes back as the final *assistant* turn, which
    is Converse's prefill — the model continues it instead of restarting. The output
    budget shrinks by what was already spent, so a resumed answer cannot exceed the
    ``max_tokens`` the client asked for.

    Args:
        converse_input: The request as first sent.
        text_so_far: Every text delta already written to the client.

    Returns:
        The continuation request, or ``None`` when there is no output budget left to
        continue into (finishing is then the backend's business, not ours).
    """
    prefill = text_so_far.rstrip()   # Bedrock rejects a trailing-whitespace prefill
    resumed = dict(converse_input)

    messages = list(resumed.get("messages") or [])
    if prefill:
        if messages and messages[-1].get("role") == "assistant":
            # Client-sent prefill already occupies the last turn; extend it rather than
            # adding a second assistant message, which Converse would reject.
            last = dict(messages[-1])
            last["content"] = list(last.get("content") or []) + [{"text": prefill}]
            messages[-1] = last
        else:
            messages.append({"role": "assistant", "content": [{"text": prefill}]})
        resumed["messages"] = messages

    config = resumed.get("inferenceConfig")
    if isinstance(config, dict) and config.get("maxTokens"):
        remaining = int(config["maxTokens"]) - _approx_tokens(prefill)
        if remaining <= 0:
            return None
        resumed["inferenceConfig"] = {**config, "maxTokens": remaining}
    return resumed


def _approx_tokens(text: str) -> int:
    """Rough token count (~4 chars each) for text whose real count never arrived."""
    return max(1, len(text) // 4) if text else 0


def _backend_error_status(message: str) -> int:
    """Map an in-stream backend error to an HTTP status (Quick answers 200 for these)."""
    text = (message or "").lower()
    if "not authorized" in text:
        return 403
    # An entitlement block is a quota refusal, not a gateway fault: 429 is what a
    # client (and litellm in front of it) already knows how to read.
    if "usage limit" in text or "entitlement" in text:
        return 429
    # Bedrock reports a malformed request the same way it reports a real fault — an
    # in-stream error frame under HTTP 200 — but names its own status inside the text.
    # Reading that matters: a request Bedrock refuses fails identically everywhere, so
    # calling it a backend fault made one bad client request fail over onto a second
    # account and cool BOTH (seen live 2026-08-31, "text content blocks must be
    # non-empty" took the whole pool down for minutes). 400 stops the retry.
    if "status code: 400" in text or "validationexception" in text:
        return 400
    return 502


def _pool_exhausted_message(pinned: str) -> str:
    """Explain, actionably, why no account could serve the request."""
    if pinned:
        known = ", ".join(a.name for a in pool.accounts()) or "(none)"
        return f"Quick 账号 '{pinned}' 不存在或不可用。已知账号：{known}"
    snapshot = pool.snapshot()
    detail = "；".join(
        f"{a['name']}={a['status']}" + _account_reason(a, snapshot)
        for a in snapshot["accounts"]
    ) or "账号池为空"
    return f"Quick 账号池无可用账号：{detail}"


def _account_reason(account: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
    """Parenthesised reason for one account's state, or "" when it needs none."""
    reason = account.get("cooldown_reason") or account.get("disabled_reason") or ""
    return f"({reason})" if reason else ""


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
    events = 0
    async for chunk in converse_stream(converse_input, account):
        for event in decoder.feed(chunk):
            events += 1
            aggregator.add(event)
    if aggregator.error:
        return None, aggregator.error
    if not events:
        return None, EMPTY_STREAM_ERROR
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
    blamed: Optional[Tuple[Account, str]] = None
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
            return None, _error_response(
                status, message,
                "invalid_request_error" if status == 400 else "api_error")
        fingerprint = _failure_fingerprint(status, message)
        if blamed is not None and blamed[1] == fingerprint:
            # Two accounts, one request, the same error: that is about the request.
            pool.absolve(blamed[0], _SAME_FAILURE.format(account=account.name))
            return None, _error_response(status, message)
        pool.note_failure(account, status, message)
        blamed = (account, fingerprint)
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

    Past that point a *dropped connection* is still recoverable: the answer so far goes
    back to the model as a prefill and the rest is streamed into the same message
    (:func:`_continuation_input`). The alternative — the error the client sees today —
    costs a whole turn, and a turn here means re-reading a 200k-500k-token prefix.
    """
    excluded: Set[str] = set()
    status, message = 503, ""
    blamed: Optional[Tuple[Account, str]] = None
    translator = ConverseToAnthropicSSE(message_id, model)
    emitted = False
    attempts, resumes = 0, 0
    while attempts < max(1, QUICK_POOL_MAX_ATTEMPTS):
        account = _pick(pinned, excluded)
        if account is None:
            break
        attempts += 1
        current_account.set(account.name)
        decoder = EventStreamDecoder()
        failure = ""
        dropped = False
        first_event = True
        pool.begin(account)
        try:
            async for chunk in converse_stream(converse_input, account):
                for event in decoder.feed(chunk):
                    if first_event:
                        # Quick reports a refusal as an in-stream error frame under
                        # HTTP 200, and that frame is the whole stream. Checked on every
                        # attempt, not just the first: a continuation that runs into an
                        # entitlement block must not read as a finished answer.
                        first_event = False
                        err = backend_error(event)
                        if err:
                            failure = err
                            break
                    if not emitted:
                        yield translator.start()
                        yield "event: ping\ndata: {\"type\": \"ping\"}\n\n"
                        emitted = True
                    for sse in translator.handle(event):
                        yield sse
                if failure:
                    break
            if not failure and not emitted:
                failure = EMPTY_STREAM_ERROR
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
        except httpx.TransportError as exc:
            # The connection died under us (typically "peer closed connection without
            # sending complete message body"). Nothing was wrong with the request, so
            # mid-stream this is the one failure worth continuing rather than reporting.
            logger.warning("Quick stream dropped on account '{}': {}", account.name, exc)
            status, failure, dropped = 502, f"{type(exc).__name__}: {exc}", True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Quick streaming failed on account '{}'", account.name)
            status, failure = 502, str(exc)
        finally:
            pool.end(account)

        message = failure
        if emitted:
            resumed = (
                _continuation_input(converse_input, translator.text_so_far)
                if dropped and resumes < QUICK_STREAM_MAX_RESUMES and translator.can_resume()
                else None
            )
            if resumed is not None:
                resumes += 1
                # A resume is not a failover: it gets its own budget of accounts. The
                # account that dropped is passed over but not benched — a dead
                # connection is no verdict on it — and it is taken back rather than
                # dead-ending the stream when it is the only account there is.
                attempts, excluded = 0, {account.name}
                if _pick(pinned, excluded) is None:
                    excluded = set()
                converse_input = resumed
                translator.resume_here()
                logger.warning(
                    "Quick stream dropped after {} chars on '{}'; continuing on another "
                    "account (resume {}/{}).",
                    len(translator.text_so_far), account.name, resumes, QUICK_STREAM_MAX_RESUMES,
                )
                continue
            # Too late to switch accounts — the client is mid-stream.
            yield _sse_error(failure)
            return
        if not _should_failover(status, failure):
            yield _sse_error(failure, "invalid_request_error" if status == 400 else "api_error")
            return
        fingerprint = _failure_fingerprint(status, failure)
        if blamed is not None and blamed[1] == fingerprint:
            pool.absolve(blamed[0], _SAME_FAILURE.format(account=account.name))
            yield _sse_error(failure)
            return
        pool.note_failure(account, status, failure)
        blamed = (account, fingerprint)
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
