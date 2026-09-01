# -*- coding: utf-8 -*-

"""
HTTP client for the Amazon Quick tenant DataPlane (Bedrock ``Converse`` proxy).

Confirmed protocol (by decrypting the agent's bedrock client + live verification):

* **Endpoint**: ``POST {tenant_url}/integration/quick-work/bedrock-proxy-stream``
* **Auth**: ``Authorization: Bearer <id_token>`` — the Keycloak *id_token*
  (aud=quick-desktop), NOT the access_token.
* **Body**: ``{"modelId": "<id>", "requestBody": {<Bedrock Converse params>}}``
* **Response**: an AWS event stream whose frames are typed ``bedrockStreamEvent``;
  each JSON payload is ``{"eventType": "<Converse event>", "payload": {…}}``
  (unwrapped in :mod:`quick.streaming`).

The non-stream ``/bedrock-proxy`` path uses a different (InvokeModel) schema, so
the gateway routes everything through the stream path and aggregates for
non-streaming responses.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncGenerator, Dict, Optional, Tuple

import httpx
from loguru import logger

from quick.auth import quick_auth_manager
from quick.config import (
    BEDROCK_PROXY_STREAM_PATH,
    EVENTSTREAM_ACCEPT,
    QUICK_HTTP_KEEPALIVE_EXPIRY,
    QUICK_HTTP_MAX_CONNECTIONS,
    QUICK_HTTP_MAX_KEEPALIVE,
    QUICK_HTTP_REUSE_CONNECTIONS,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from quick.pool import Account

JsonDict = Dict[str, object]


class QuickAPIError(Exception):
    """Raised when the DataPlane returns a non-success response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Quick DataPlane error {status_code}: {message}")
        self.status_code = status_code
        self.message = message


async def _auth(account: Optional["Account"] = None) -> Tuple[Dict[str, str], str]:
    """Return (headers, tenant_base_url) for a DataPlane request.

    Args:
        account: Pool account whose credentials to use. ``None`` falls back to the
            process-wide manager (single-account deployments, CLI probes, tests).
    """
    manager = account.auth if account is not None else quick_auth_manager
    creds = await manager.get_credentials()
    headers = {
        "Authorization": f"Bearer {creds.id_token}",
        "Content-Type": "application/json",
        "Accept": EVENTSTREAM_ACCEPT,
        "User-Agent": "quick-gateway/1.0",
    }
    return headers, creds.tenant_url


_TIMEOUT = httpx.Timeout(300.0, connect=30.0)
_LIMITS = httpx.Limits(
    max_connections=QUICK_HTTP_MAX_CONNECTIONS,
    max_keepalive_connections=QUICK_HTTP_MAX_KEEPALIVE,
    keepalive_expiry=QUICK_HTTP_KEEPALIVE_EXPIRY,
)

# One warm client per event loop. Keyed by loop because the CLIs (usage_watch,
# model_watch --probe) each run their own asyncio.run, and a client built on a loop
# that has since closed cannot be used on the next one.
_clients: Dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}


def _shared_client() -> httpx.AsyncClient:
    """Return this loop's keep-alive client, building it on first use."""
    loop = asyncio.get_running_loop()
    for dead in [lp for lp in _clients if lp.is_closed()]:
        _clients.pop(dead, None)
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=_TIMEOUT, limits=_LIMITS)
        _clients[loop] = client
    return client


@asynccontextmanager
async def _http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Yield the client for one DataPlane call.

    Shared (and therefore warm) by default; ``QUICK_HTTP_REUSE_CONNECTIONS=0`` falls
    back to a client per request, which closes its connection on the way out.
    """
    if QUICK_HTTP_REUSE_CONNECTIONS:
        yield _shared_client()
        return
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        yield client


async def aclose_http_clients() -> None:
    """Close the shared client(s) — called from the app's shutdown path."""
    for loop, client in list(_clients.items()):
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.debug("Quick: closing shared HTTP client failed: {}", exc)
        _clients.pop(loop, None)


def _envelope(converse_input: JsonDict) -> bytes:
    """Wrap Converse params into the DataPlane request envelope.

    ``converse_input`` carries ``modelId`` and (optionally) ``_quick_output_config``
    inline (from :func:`quick.converters.anthropic_to_converse`); the DataPlane
    wants ``modelId`` and ``output_config`` as siblings of ``requestBody``.
    """
    body = dict(converse_input)
    model_id = body.pop("modelId", None)
    output_config = body.pop("_quick_output_config", None)
    envelope: JsonDict = {"modelId": model_id, "requestBody": body}
    if output_config:
        envelope["output_config"] = output_config
    return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


async def converse_stream(
    converse_input: JsonDict, account: Optional["Account"] = None
) -> AsyncGenerator[bytes, None]:
    """POST a ConverseStream request and yield raw event-stream bytes.

    Args:
        converse_input: Bedrock ``Converse`` input incl. ``modelId`` (from
            :func:`quick.converters.anthropic_to_converse`).
        account: Pool account to authenticate as; ``None`` uses the process-wide
            credentials.

    Yields:
        Raw ``application/vnd.amazon.eventstream`` byte chunks.

    Raises:
        QuickAPIError: On a non-2xx response.
        quick.auth.QuickAuthError: If credentials cannot be obtained.
    """
    body = _envelope(converse_input)
    manager = account.auth if account is not None else quick_auth_manager
    for attempt in range(2):
        headers, base = await _auth(account)
        async with _http_client() as client:
            async with client.stream(
                "POST", f"{base}{BEDROCK_PROXY_STREAM_PATH}", headers=headers, content=body
            ) as resp:
                if resp.status_code in (401, 403) and attempt == 0:
                    logger.warning("Quick DataPlane {} — refreshing token and retrying.", resp.status_code)
                    await manager.invalidate()
                    await resp.aread()
                    continue
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                    raise QuickAPIError(resp.status_code, detail)
                async for chunk in resp.aiter_bytes():
                    yield chunk
                return
    raise QuickAPIError(500, "ConverseStream retry exhausted")
