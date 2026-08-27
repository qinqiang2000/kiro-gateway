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

import json
from typing import TYPE_CHECKING, AsyncGenerator, Dict, Optional, Tuple

import httpx
from loguru import logger

from quick.auth import quick_auth_manager
from quick.config import BEDROCK_PROXY_STREAM_PATH, EVENTSTREAM_ACCEPT

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
        # Per repo gotcha: streaming needs a dedicated client per request.
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
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
