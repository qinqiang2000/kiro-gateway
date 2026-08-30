# -*- coding: utf-8 -*-

"""
Authentication manager for the Amazon Quick backend.

Credential lifecycle (confirmed against the running app):

1. **Cache file first** — ``~/.quickwork/gateway-creds.json`` (:data:`QUICK_CREDS_FILE`).
   If present, load straight from it and never touch the Keychain. This is the only path
   that runs on a host without a macOS Keychain (e.g. a headless Linux box): copy the file
   over once and the gateway bootstraps from it.
2. **Keychain (macOS bootstrap)** — only when the cache file is absent. Locate the profile
   from ``~/.quickwork/profiles.json`` (``last_active``), then read the credential blob:
   ``security find-generic-password -s quickwork-enterprise-<profileId> -a session -w``.
   The blob is JSON with ``refresh_token``, ``access_token``, ``id_token``, ``token_endpoint``,
   ``client_id``, ``tenant_url``, ``region``, ``user_arn`` and expiries. On success it is
   written to the cache file, so the macOS login-password prompt happens at most once.
3. **Refresh** the short-lived (~5 min) tokens against Keycloak:
   ``POST {token_endpoint}`` with form
   ``grant_type=refresh_token&client_id={client_id}&refresh_token={refresh_token}``.
   Keycloak rotates the refresh token; the newest is persisted back to the cache file.

Thread-safe refresh via ``asyncio.Lock`` (mirrors :class:`kiro.auth.KiroAuthManager`).
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from quick.config import (
    DEFAULT_CLIENT_ID,
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE_PREFIX,
    PROFILES_JSON,
    QUICK_CREDS_FILE,
    QUICK_KEEPALIVE_INTERVAL,
    QUICK_PROFILE_ID,
    QUICK_TENANT_URL,
    TOKEN_REFRESH_THRESHOLD_SECONDS,
)


class QuickAuthError(Exception):
    """Raised when Quick credentials cannot be loaded or refreshed."""


class QuickCredentials:
    """In-memory view of the Quick credential blob.

    Attributes:
        access_token: Current bearer token for the tenant DataPlane.
        refresh_token: Latest (rotated) Keycloak refresh token.
        access_token_expiry: Unix epoch seconds when ``access_token`` expires.
        token_endpoint: Keycloak token endpoint.
        client_id: OIDC client id (``quick-desktop``).
        tenant_url: Per-tenant DataPlane base URL.
        region: AWS region string (e.g. ``us-east-1``).
        user_arn: Federated QuickSight user ARN (informational).
    """

    def __init__(self, blob: dict) -> None:
        self.access_token: Optional[str] = blob.get("access_token")
        # The DataPlane authenticates with the OIDC *id_token* (aud=quick-desktop),
        # not the access_token (aud=account). This is the bearer used for inference.
        self.id_token: Optional[str] = blob.get("id_token")
        self.refresh_token: Optional[str] = blob.get("refresh_token")
        self.access_token_expiry: float = float(blob.get("access_token_expiry") or 0.0)
        self.id_token_expiry: float = float(blob.get("id_token_expiry") or 0.0)
        self.token_endpoint: str = blob.get("token_endpoint") or ""
        self.client_id: str = blob.get("client_id") or DEFAULT_CLIENT_ID
        self.tenant_url: str = (QUICK_TENANT_URL or blob.get("tenant_url") or "").rstrip("/")
        self.region: str = blob.get("region") or "us-east-1"
        self.user_arn: str = blob.get("user_arn") or ""

    def is_expired(self, threshold: int = TOKEN_REFRESH_THRESHOLD_SECONDS) -> bool:
        """Return True if the id token (DataPlane bearer) is missing or near expiry."""
        if not self.id_token:
            return True
        return time.time() >= (self.id_token_expiry - threshold)

    def to_dict(self) -> dict:
        """Serialize to a portable credential blob (inverse of ``__init__``).

        Returns:
            A JSON-serializable dict with the same keys the constructor reads, so a
            round-trip through :meth:`QuickAuthManager._save_creds_file` and back
            reconstructs an equivalent :class:`QuickCredentials`. ``tenant_url`` is
            written as the resolved value so the file is self-contained on any host.
        """
        return {
            "access_token": self.access_token,
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "access_token_expiry": self.access_token_expiry,
            "id_token_expiry": self.id_token_expiry,
            "token_endpoint": self.token_endpoint,
            "client_id": self.client_id,
            "tenant_url": self.tenant_url,
            "region": self.region,
            "user_arn": self.user_arn,
        }


def _resolve_profile_id() -> str:
    """Determine the active Quick profile id.

    Returns:
        The profile id (Keychain service suffix).

    Raises:
        QuickAuthError: If no profile can be determined.
    """
    if QUICK_PROFILE_ID:
        return QUICK_PROFILE_ID
    try:
        data = json.loads(PROFILES_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise QuickAuthError(
            f"Quick profile registry not found at {PROFILES_JSON}. Is Amazon Quick installed and signed in?"
        ) from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise QuickAuthError(f"Failed to read {PROFILES_JSON}: {exc}") from exc

    profile_id = data.get("last_active")
    if not profile_id:
        entries = data.get("entries") or []
        if entries:
            profile_id = entries[0].get("id")
    if not profile_id:
        raise QuickAuthError("No active Quick profile found in profiles.json")
    return profile_id


def _read_creds_file(path: Optional[Path] = None) -> Optional[dict]:
    """Read the portable credential cache file, if present.

    Args:
        path: The cache file to read; defaults to :data:`QUICK_CREDS_FILE` (read at
            call time so tests and the pool can redirect it).

    Returns:
        The parsed JSON blob, or ``None`` if the file does not exist.

    Raises:
        QuickAuthError: If the file exists but cannot be read or parsed (a corrupt
            cache should surface loudly, not silently fall back to the Keychain).
    """
    path = path or QUICK_CREDS_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise QuickAuthError(
            f"Quick credential cache {path} is unreadable/corrupt: {exc}. "
            f"Delete it to re-bootstrap from the Keychain (macOS), or re-copy it."
        ) from exc


def _read_keychain_blob(profile_id: str) -> dict:
    """Read and parse the Quick credential blob from the macOS Keychain.

    Args:
        profile_id: The active profile id.

    Returns:
        The parsed JSON credential blob.

    Raises:
        QuickAuthError: If the Keychain item is missing or unparseable.
    """
    service = f"{KEYCHAIN_SERVICE_PREFIX}{profile_id}"
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-a", KEYCHAIN_ACCOUNT, "-w"],
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:  # `security` only exists on macOS
        raise QuickAuthError("`security` CLI not found; Quick auth requires macOS.") from exc
    except subprocess.CalledProcessError as exc:
        raise QuickAuthError(
            f"Keychain item '{service}' (account '{KEYCHAIN_ACCOUNT}') not found. "
            f"Sign in to Amazon Quick first."
        ) from exc
    try:
        return json.loads(raw.decode("utf-8").strip())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise QuickAuthError(f"Quick Keychain blob is not valid JSON: {exc}") from exc


class QuickAuthManager:
    """Manages Quick access tokens with automatic, thread-safe refresh.

    One instance owns exactly one credential file. In a multi-account deployment
    (:mod:`quick.pool`) there is one manager per account, and each file is written
    by that one manager only — Keycloak rotates the refresh token on every refresh,
    so two writers on one file eventually invalidate each other.

    Args:
        creds_file: Credential cache to own. ``None`` (the default) resolves to
            :data:`quick.config.QUICK_CREDS_FILE` at call time.
        name: Account label used in logs; defaults to the file's derived name.
    """

    def __init__(self, creds_file: Optional[Path] = None, name: str = "") -> None:
        self._creds: Optional[QuickCredentials] = None
        self._lock = asyncio.Lock()
        self._profile_id: Optional[str] = None
        self._creds_file: Optional[Path] = creds_file
        self.name: str = name
        # mtime of the last file content THIS manager put there (or read). Anything
        # else on disk is somebody replacing the credential — see
        # :meth:`file_replaced_externally`.
        self._own_mtime: float = 0.0
        self._reload_requested: bool = False

    @property
    def creds_file(self) -> Path:
        """The credential file this manager reads and writes."""
        # Resolved lazily so monkeypatching quick.auth.QUICK_CREDS_FILE still works.
        return self._creds_file or QUICK_CREDS_FILE

    def _stamp_mtime(self) -> None:
        """Remember the file's mtime as *ours* (after we read or wrote it)."""
        try:
            self._own_mtime = self.creds_file.stat().st_mtime
        except OSError:
            self._own_mtime = 0.0

    def file_replaced_externally(self) -> bool:
        """True when the credential file on disk is not the content we last handled.

        Keycloak rotates the refresh token on every refresh and we write the rotation
        back, so mtime alone means nothing — the gateway churns it constantly. What
        matters is whether the *newest* write was ours. When it was not, somebody
        uploaded a fresh credential, which is the only thing that fixes an
        ``invalid_grant`` account (:meth:`QuickPool.discover`).
        """
        if not self._own_mtime:
            return False           # never loaded: nothing has been replaced yet
        try:
            return self.creds_file.stat().st_mtime != self._own_mtime
        except OSError:
            return False           # gone or unreadable: discovery handles that

    def mark_stale(self) -> None:
        """Drop the in-memory credential at the next lock acquisition.

        Deliberately a flag rather than ``self._creds = None``: this is called from
        synchronous discovery, and clearing the field outright could land between a
        refresh and its return inside :meth:`get_credentials`.

        The file we are about to read becomes *ours* right away, so the caller that
        polls :meth:`file_replaced_externally` sees the replacement once instead of
        on every pass until the reload actually happens.
        """
        self._reload_requested = True
        self._stamp_mtime()

    def _load(self) -> QuickCredentials:
        """Load (or reload) credentials, file-first then Keychain.

        Source precedence:

        1. **Cache file** (:data:`QUICK_CREDS_FILE`) — the only path that runs on a
           host without a macOS Keychain (e.g. a headless Linux box). Never invokes
           ``security``.
        2. **macOS Keychain** — only when the cache file is absent *and* the
           ``security`` CLI exists. On success the blob is written to the cache file
           so the login-password prompt never recurs.

        Raises:
            QuickAuthError: If neither source yields usable credentials.
        """
        blob = _read_creds_file(self.creds_file)
        source = "file"
        bootstrap_from_keychain = False
        if blob is None:
            if shutil.which("security") is None:
                raise QuickAuthError(
                    f"No Quick credentials: cache file {self.creds_file} is absent and the "
                    f"macOS `security` CLI is not available (are we on Linux?). Copy the file "
                    f"from a signed-in mac: scp <mac>:{self.creds_file} {self.creds_file}"
                )
            self._profile_id = _resolve_profile_id()
            blob = _read_keychain_blob(self._profile_id)
            source = "keychain"
            bootstrap_from_keychain = True

        creds = QuickCredentials(blob)
        if not creds.refresh_token or not creds.token_endpoint:
            raise QuickAuthError("Quick credential blob missing refresh_token/token_endpoint.")
        if not creds.tenant_url:
            raise QuickAuthError("Quick credential blob missing tenant_url.")

        # Persist immediately on the one-time Keychain bootstrap so future starts
        # (and other hosts) load from the file and never prompt again.
        if bootstrap_from_keychain:
            self._save_creds_file(creds)

        self._stamp_mtime()
        logger.info(
            "Loaded Quick credentials (account={}, source={}, profile={}, user={}, tenant={})",
            self.name or "default",
            source,
            self._profile_id or "?",
            creds.user_arn or "?",
            creds.tenant_url,
        )
        return creds

    def _save_creds_file(self, creds: QuickCredentials) -> None:
        """Persist credentials to :attr:`creds_file` atomically, mode 0600.

        Args:
            creds: The credentials to serialize.

        A failure to write is logged but never raised — a working in-memory token
        must not be defeated by a disk problem.
        """
        target = self.creds_file
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(target.parent), prefix=".gateway-creds-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(creds.to_dict(), fh, ensure_ascii=False, indent=2)
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, target)
                # Claim this write, so a LATER mtime means somebody else's file.
                self._stamp_mtime()
            except BaseException:
                # Clean up the temp file on any failure before re-raising.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.warning("Failed to persist Quick creds to {}: {}", target, exc)

    async def _refresh(self, creds: QuickCredentials) -> None:
        """Refresh the access token against Keycloak, in place."""
        data = {
            "grant_type": "refresh_token",
            "client_id": creds.client_id,
            "refresh_token": creds.refresh_token,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                creds.token_endpoint,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            raise QuickAuthError(
                f"Keycloak refresh failed ({resp.status_code}): {resp.text[:300]}"
            )
        tok = resp.json()
        now = time.time()
        creds.access_token = tok["access_token"]
        creds.access_token_expiry = now + float(tok.get("expires_in", 300))
        # id_token is the DataPlane bearer; Keycloak returns a fresh one on refresh.
        if tok.get("id_token"):
            creds.id_token = tok["id_token"]
            creds.id_token_expiry = now + float(tok.get("expires_in", 300))
        # Keycloak rotates the refresh token; keep the newest.
        if tok.get("refresh_token"):
            creds.refresh_token = tok["refresh_token"]
        logger.debug("Refreshed Quick tokens (expires in {}s).", int(tok.get("expires_in", 0)))
        # Persist the rotated refresh_token + fresh id_token so the on-disk cache stays
        # self-bootstrapping across restarts and hosts.
        self._save_creds_file(creds)

    async def get_id_token(self) -> str:
        """Return a valid id token (the DataPlane bearer), refreshing if necessary."""
        creds = await self.get_credentials()
        return creds.id_token or ""

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        creds = await self.get_credentials()
        return creds.access_token or ""

    async def get_credentials(self) -> QuickCredentials:
        """Return current credentials, (re)loading and refreshing as needed."""
        async with self._lock:
            self._honour_reload()
            if self._creds is None:
                self._creds = self._load()
            if self._creds.is_expired():
                await self._refresh(self._creds)
            return self._creds

    def _honour_reload(self) -> None:
        """Act on a pending :meth:`mark_stale` — call only while holding the lock."""
        if self._reload_requested:
            self._reload_requested = False
            self._creds = None
            logger.info("Quick auth: reloading '{}' from a replaced credential file.",
                        self.name or "default")

    async def invalidate(self) -> None:
        """Force a full reload + refresh on the next call (e.g. after a 401/403)."""
        async with self._lock:
            self._creds = None

    async def keepalive(self) -> None:
        """Unconditionally refresh once to keep the ~90-day refresh_token alive.

        The request path already refreshes the short-lived id_token on demand, so this
        is only useful during long idle periods (no requests) to prevent the offline
        refresh_token from silently lapsing. Loads credentials first if needed. Errors
        propagate to the caller (the background loop logs and retries).
        """
        async with self._lock:
            self._honour_reload()
            if self._creds is None:
                self._creds = self._load()
            await self._refresh(self._creds)


# Module-level singleton, mirroring kiro.auth usage.
quick_auth_manager = QuickAuthManager()


async def keepalive_loop() -> None:
    """Background task: periodically refresh Quick creds so the offline token survives.

    Runs forever on :data:`QUICK_KEEPALIVE_INTERVAL`. Does nothing (returns immediately)
    when the interval is 0. Each failure is logged and retried on the next tick; a
    failure never crashes the task or the app. Cancellation (on shutdown) is clean.
    """
    if QUICK_KEEPALIVE_INTERVAL <= 0:
        logger.info("Quick keep-alive disabled (QUICK_KEEPALIVE_INTERVAL=0).")
        return
    logger.info(
        "Quick keep-alive task started (interval: {}s).", QUICK_KEEPALIVE_INTERVAL
    )
    while True:
        await asyncio.sleep(QUICK_KEEPALIVE_INTERVAL)
        try:
            from quick.pool import pool

            accounts = pool.discover()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to the single-account path
            logger.debug("Quick keep-alive: pool unavailable ({}); using process creds.", exc)
            accounts = []

        if not accounts:
            try:
                await quick_auth_manager.keepalive()
                logger.debug("Quick keep-alive refresh ok.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep-alive must never crash the app
                logger.warning(
                    "Quick keep-alive refresh failed ({}); retry in {}s.",
                    exc,
                    QUICK_KEEPALIVE_INTERVAL,
                )
            continue

        for account in accounts:
            if account.disabled_reason:
                continue
            try:
                await account.auth.keepalive()
                logger.debug("Quick keep-alive refresh ok (account={}).", account.name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad account must not stop the rest
                # A refresh token that Keycloak rejects outright never recovers on its
                # own; note_failure() turns that into a disabled account (and an alert
                # on the next watch cycle) instead of a silent hole in the pool.
                logger.warning(
                    "Quick keep-alive refresh failed for account '{}' ({}); retry in {}s.",
                    account.name, exc, QUICK_KEEPALIVE_INTERVAL,
                )
                from quick.pool import pool as _pool

                _pool.note_failure(account, None, str(exc))
