# -*- coding: utf-8 -*-

"""
Configuration for the Quick gateway (Amazon Quick backend).

Amazon Quick (com.amazon.QuickWork.mac) is an Electron + frozen-Python desktop
agent that talks to a per-tenant "AppIntegrations DataPlane" (codename *plato*)
which proxies Amazon Bedrock ``Converse`` / ``ConverseStream``.

Unlike Kiro, Quick does not keep its credentials in ``~/.aws/sso/cache/``. It
stores a single JSON blob in the **macOS Keychain** under the generic-password
service ``quickwork-enterprise-<profileId>`` (account ``session``) and refreshes
it against a Keycloak OIDC endpoint.

Everything in this module is either confirmed from the running app (Keychain
layout, Keycloak refresh, tenant URL, model IDs, operations) or a clearly marked
TODO that still needs a live capture (the ``X-Amz-Target`` RPC operation name and
the exact request envelope).
"""

import os
from pathlib import Path
from typing import Dict, Optional

# ==================================================================================================
# Keychain / profile discovery
# ==================================================================================================

# macOS Keychain generic-password service prefix. The full service name is
# ``quickwork-enterprise-<profileId>`` and the account is ``session``.
KEYCHAIN_SERVICE_PREFIX: str = "quickwork-enterprise-"
KEYCHAIN_ACCOUNT: str = "session"

# Quick's on-disk profile registry. ``last_active`` names the profile whose
# credential blob we should read from the Keychain.
QUICKWORK_HOME: Path = Path(os.path.expanduser("~/.quickwork"))
PROFILES_JSON: Path = QUICKWORK_HOME / "profiles.json"

# Override the auto-detected profile id (service = PREFIX + this value).
QUICK_PROFILE_ID: str = os.getenv("QUICK_PROFILE_ID", "")

# Portable credential cache. On macOS the Keychain is read at most ONCE (when this
# file is absent) and the blob is persisted here; every later start — and every host
# without a Keychain (e.g. a headless Linux box) — loads straight from this file and
# never invokes `security`. Refresh writes the rotated refresh_token back here, so the
# file stays self-bootstrapping. scp it to another host to run the gateway there.
QUICK_CREDS_FILE: Path = Path(
    os.getenv("QUICK_CREDS_FILE", "~/.quickwork/gateway-creds.json")
).expanduser()

# ==================================================================================================
# Account pool (multi-account)
# ==================================================================================================

# Directory scanned for credential files, one per Quick account. Each match becomes a
# pool member; the account NAME is derived from the filename suffix:
#   gateway-creds.json      -> "default"
#   gateway-creds-b.json    -> "b"
# Adding an account is therefore "drop one more file in here and restart" — no nginx
# upstream to edit, no extra container (contrast: the Kiro pool's per-account containers).
QUICK_CREDS_DIR: Path = Path(
    os.getenv("QUICK_CREDS_DIR", str(QUICK_CREDS_FILE.parent))
).expanduser()

# Glob applied inside QUICK_CREDS_DIR. Keep it narrow: state files (model-watch-state,
# session-usage-state) live in the same directory and must NOT be mistaken for creds.
QUICK_CREDS_GLOB: str = os.getenv("QUICK_CREDS_GLOB", "gateway-creds*.json")

# Explicit account list ("default,b"), overriding auto-discovery. Each name N maps to
# QUICK_CREDS_DIR/gateway-creds-N.json (N="default" -> gateway-creds.json).
QUICK_ACCOUNTS: str = os.getenv("QUICK_ACCOUNTS", "")

# How long an account stays out of rotation after a failure the backend gave no
# resume hint for. When Quick DOES report ``resumeInMinutes``, that wins.
QUICK_POOL_COOLDOWN_SECONDS: int = int(os.getenv("QUICK_POOL_COOLDOWN_SECONDS", "900"))

# Remaining-percentage bucket width used to rank accounts. Ranking on the raw
# percentage would ping-pong traffic between two accounts on every reading; bucketing
# to 10 % keeps the choice stable and lets the in-flight count break the tie.
QUICK_POOL_QUOTA_BUCKET: int = int(os.getenv("QUICK_POOL_QUOTA_BUCKET", "10"))

# DEPRECATED alias for QUICK_POOL_OVERAGE_POLICY="avoid". Kept so an existing
# deployment that sets it keeps its behaviour.
QUICK_POOL_AVOID_OVERAGE: bool = os.getenv("QUICK_POOL_AVOID_OVERAGE", "0").lower() in (
    "1", "true", "yes",
)

# How the pool ORDERS an account whose monthly entitlement is spent:
#   avoid — pick it last, while any account still has monthly headroom   (default)
#   allow — ignore the distinction and rank purely on the session allowance
# This is a preference, never an eviction: an account leaves the pool only when it
# actually stops working. A spent account that Quick still serves keeps serving.
QUICK_POOL_OVERAGE_POLICY: str = os.getenv("QUICK_POOL_OVERAGE_POLICY", "avoid").strip().lower()

# Ceiling for the exponential back-off applied to an account that keeps failing, so a
# persistently broken one is retried every hour rather than every minute forever.
QUICK_POOL_MAX_COOLDOWN_SECONDS: int = int(
    os.getenv("QUICK_POOL_MAX_COOLDOWN_SECONDS", "3600")
)

# How many accounts one request may try before giving up (1 = no failover).
QUICK_POOL_MAX_ATTEMPTS: int = int(os.getenv("QUICK_POOL_MAX_ATTEMPTS", "2"))

# ==================================================================================================
# Public status page (read-only quota dashboard)
# ==================================================================================================

# Second HTTP server, serving ONLY the pool's quota page — never inference. It is a
# separate app on a separate port precisely so that publishing it cannot expose
# /quick/v1/messages (that would hand the account pool to the internet). 0 disables.
QUICK_STATUS_PORT: int = int(os.getenv("QUICK_STATUS_PORT", "9090"))
QUICK_STATUS_HOST: str = os.getenv("QUICK_STATUS_HOST", "0.0.0.0")

# Path the page is served under. Everything outside it 404s, so a scanner sweeping
# ``/`` finds nothing — cheap obscurity, not a security boundary (use
# QUICK_STATUS_TOKEN for that). Empty string serves at the root.
QUICK_STATUS_PATH: str = os.getenv("QUICK_STATUS_PATH", "/quick")

# Optional shared secret. Empty (default) = the page is public. When set, requests
# must carry ?t=<token> or the X-Status-Token header.
QUICK_STATUS_TOKEN: str = os.getenv("QUICK_STATUS_TOKEN", "")

# ==================================================================================================
# Token refresh (Keycloak OIDC)
# ==================================================================================================

# Refresh a bit before the (~5 min) access-token expiry to avoid races.
TOKEN_REFRESH_THRESHOLD_SECONDS: int = 60

# Keep-alive interval (seconds) for the background Quick refresh task. The id_token
# lives only ~5 min and is refreshed lazily on the request path, so this loop exists
# ONLY to stop the long-lived (~90-day) refresh_token from silently lapsing when the
# gateway sees zero traffic for a long time. Once a day is ample for a 90-day window
# and keeps the logs quiet. Set QUICK_KEEPALIVE_INTERVAL=0 to disable the task.
QUICK_KEEPALIVE_INTERVAL: int = int(os.getenv("QUICK_KEEPALIVE_INTERVAL", "86400"))

# The token_endpoint / client_id normally come straight from the Keychain blob
# (keys ``token_endpoint`` and ``client_id``); these are only fallbacks.
DEFAULT_CLIENT_ID: str = "quick-desktop"

# ==================================================================================================
# Tenant DataPlane (Bedrock proxy)
# ==================================================================================================

# The tenant base URL is read from the Keychain blob (key ``tenant_url``), e.g.
#   https://qbs-<uuid>.dp.appintegrations.us-east-1.prod.plato.ai.aws.dev
# Inference goes to REST paths under it (see BEDROCK_PROXY_*_PATH). Override if needed:
QUICK_TENANT_URL: str = os.getenv("QUICK_TENANT_URL", "")

# DataPlane REST paths (confirmed by decrypting the agent's bedrock client).
# The streaming path speaks Bedrock ``ConverseStream``; the request body is
# ``{"modelId": <id>, "requestBody": {<Converse params>}}`` and the response is an
# AWS event stream whose frames are typed ``bedrockStreamEvent`` (see
# quick/streaming.py). The non-stream path uses a different (InvokeModel) schema,
# so the gateway routes everything through the stream path and aggregates.
BEDROCK_PROXY_STREAM_PATH: str = "/integration/quick-work/bedrock-proxy-stream"
BEDROCK_PROXY_PATH: str = "/integration/quick-work/bedrock-proxy"

EVENTSTREAM_ACCEPT: str = "application/vnd.amazon.eventstream"
# ==================================================================================================
# Model mapping: Anthropic client model name -> Quick Bedrock inference-profile id
# ==================================================================================================

# Confirmed in the running app's logs (create_bedrock_model model_id=...).
# Both ``us.`` and ``global.`` inference-profile prefixes appear for the newer ids.
QUICK_MODELS: Dict[str, str] = {
    # canonical Quick ids (pass-through). NOTE: claude-opus-5 exists on the backend
    # but this account is IAM-denied (explicit deny on InvokeModelWithResponseStream),
    # so it is intentionally NOT listed. opus-4-8 is the best authorized Opus.
    "us.anthropic.claude-opus-4-8": "us.anthropic.claude-opus-4-8",
    "global.anthropic.claude-opus-4-8": "global.anthropic.claude-opus-4-8",
    "us.anthropic.claude-opus-4-6-v1": "us.anthropic.claude-opus-4-6-v1",
    "us.anthropic.claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
}

# Friendly aliases -> Quick id. Client can also send a Quick id directly.
QUICK_MODEL_ALIASES: Dict[str, str] = {
    "claude-opus-4-8": "us.anthropic.claude-opus-4-8",
    "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
    "claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
    "claude-haiku-4-5": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    # Quick UI "modes" (mode=smart/balanced/fast in the app logs).
    "smart": "us.anthropic.claude-opus-4-6-v1",       # Smart -> Opus
    "balanced": "us.anthropic.claude-sonnet-4-6",     # Balanced -> Sonnet
    "fast": "us.anthropic.claude-haiku-4-5-20251001-v1:0",  # Fast -> Haiku
}

# Family fallback: any opus*/sonnet*/haiku* name (including client-picked names
# like Claude Code's "claude-opus-5") maps to the best Quick model in that family.
# Override the Opus target with QUICK_OPUS_MODEL if you prefer opus-4-6-v1.
QUICK_MODEL_FAMILY: Dict[str, str] = {
    "opus": os.getenv("QUICK_OPUS_MODEL", "us.anthropic.claude-opus-4-8"),
    "sonnet": os.getenv("QUICK_SONNET_MODEL", "us.anthropic.claude-sonnet-4-6"),
    "haiku": os.getenv("QUICK_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
}

# Default model when the client name cannot be resolved.
DEFAULT_QUICK_MODEL: str = os.getenv(
    "QUICK_DEFAULT_MODEL", "us.anthropic.claude-sonnet-4-6"
)

# Force EVERY request to this Quick model, ignoring whatever the client picked.
# Set to the empty string (``QUICK_FORCE_MODEL=`` in .env) to disable and fall
# back to per-request family mapping. Change the default here to switch the
# forced model (e.g. to us.anthropic.claude-opus-5 if/when it becomes available).
QUICK_FORCE_MODEL: str = os.getenv("QUICK_FORCE_MODEL", "us.anthropic.claude-opus-4-8")


def resolve_model(name: Optional[str]) -> str:
    """Resolve an incoming Anthropic model name to a Quick Bedrock model id.

    Args:
        name: The ``model`` field from the client request (may be ``None``).

    Returns:
        A Quick Bedrock inference-profile id. When :data:`QUICK_FORCE_MODEL` is
        set, always returns it (ignoring ``name``). Otherwise resolves by exact
        id/alias, then by family, falling back to :data:`DEFAULT_QUICK_MODEL` —
        matching this repo's "let the backend be the final arbiter" philosophy.
    """
    if QUICK_FORCE_MODEL:
        return QUICK_FORCE_MODEL
    if not name:
        return DEFAULT_QUICK_MODEL
    key = name.strip()
    if key in QUICK_MODELS:
        return key
    low = key.lower()
    if low in QUICK_MODEL_ALIASES:
        return QUICK_MODEL_ALIASES[low]
    # Normalize dashed Anthropic-style names: claude-sonnet-4-6-2026... -> alias
    for alias, target in QUICK_MODEL_ALIASES.items():
        if low.startswith(alias):
            return target
    # Family fallback: map any Claude family name (incl. names the *client* picks,
    # e.g. Claude Code's "claude-opus-5") to the best matching Quick model. Quick
    # only serves the ids in QUICK_MODELS, so an unmapped passthrough would 400.
    if "opus" in low:
        return QUICK_MODEL_FAMILY["opus"]
    if "sonnet" in low:
        return QUICK_MODEL_FAMILY["sonnet"]
    if "haiku" in low:
        return QUICK_MODEL_FAMILY["haiku"]
    # Unknown non-Claude name: pass through and let the DataPlane decide.
    return key
