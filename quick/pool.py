# -*- coding: utf-8 -*-

"""Multi-account pool for the Amazon Quick backend.

Quick meters a **rolling session allowance plus a monthly entitlement per account**,
so the constraint a pool has to schedule against is quota, not concurrency. Two
common designs are wrong here:

* **Pinning a client key to one account** — the heavy key burns its own account to
  zero while the others idle, and when that account trips, the key is simply down.
* **Blind round-robin** (what an nginx ``least_conn`` upstream can do) — it spreads
  evenly, which is the wrong split when one account is fresh and another is at 3 %,
  and the load balancer cannot see either number. That is how a pool degrades into
  a file of hand-commented upstreams.

This pool schedules on the number Quick hands us for free: every inference response
carries a ``usageSummary`` (:mod:`quick.usage_watch`), so the gateway always knows
each account's remaining share. Selection is therefore:

1. drop accounts that are disabled or cooling down,
2. rank by remaining session share, bucketed (raw percentages would ping-pong the
   choice between two accounts on every reading),
3. break ties by in-flight requests, then by requests served (round-robin within a
   bucket, which also spreads the very first requests before any reading exists).

A request that fails on quota/auth grounds cools that account down and is retried on
the next candidate, so one dead account is a log line rather than a user-visible error.

Accounts are discovered from credential files (``gateway-creds*.json`` in
:data:`quick.config.QUICK_CREDS_DIR`), one file per account, each owned by exactly one
:class:`quick.auth.QuickAuthManager` — Keycloak rotates the refresh token on every
refresh, so a file must have a single writer.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from loguru import logger

from quick.auth import QuickAuthManager
from quick.config import (
    QUICK_ACCOUNTS,
    QUICK_CREDS_DIR,
    QUICK_CREDS_FILE,
    QUICK_CREDS_GLOB,
    QUICK_POOL_AVOID_OVERAGE,
    QUICK_POOL_COOLDOWN_SECONDS,
    QUICK_POOL_QUOTA_BUCKET,
)
from quick.usage_watch import UsageSnapshot, snapshot_for

DEFAULT_ACCOUNT: str = "default"
_CREDS_STEM: str = "gateway-creds"

# Cooldowns per failure kind, in seconds. A backend-reported ``resumeInMinutes``
# always wins over these defaults.
_COOLDOWN_BY_KIND: Dict[str, int] = {
    "quota": QUICK_POOL_COOLDOWN_SECONDS,   # session/monthly allowance exhausted
    "throttled": 300,                       # 429
    "denied": 300,                          # IAM deny (may be model-specific)
    "error": 60,                            # transient backend failure
    "auth": 120,                            # token refresh hiccup (not invalid_grant)
}


def account_name_for(path: Path) -> str:
    """Derive an account name from a credential filename.

    ``gateway-creds.json`` → ``default``; ``gateway-creds-b.json`` → ``b``. Any other
    filename keeps its stem, so a hand-named file still yields a usable label.

    Args:
        path: The credential file.

    Returns:
        The account name.
    """
    stem = path.stem
    if stem == _CREDS_STEM:
        return DEFAULT_ACCOUNT
    if stem.startswith(_CREDS_STEM + "-"):
        return stem[len(_CREDS_STEM) + 1:] or DEFAULT_ACCOUNT
    return stem


def creds_path_for(name: str) -> Path:
    """Return the credential file an account name maps to.

    Args:
        name: Account name (``default`` maps to the plain ``gateway-creds.json``).

    Returns:
        The path inside :data:`quick.config.QUICK_CREDS_DIR`.
    """
    if name == DEFAULT_ACCOUNT:
        return QUICK_CREDS_DIR / f"{_CREDS_STEM}.json"
    return QUICK_CREDS_DIR / f"{_CREDS_STEM}-{name}.json"


def classify_failure(status: Optional[int], message: str) -> Tuple[str, bool]:
    """Classify a request failure into a cooldown kind.

    Args:
        status: HTTP status, if the failure had one.
        message: Error text from the backend or the auth layer.

    Returns:
        ``(kind, disable)`` — ``kind`` indexes :data:`_COOLDOWN_BY_KIND`; ``disable``
        is ``True`` only for a credential that can no longer be refreshed at all
        (Keycloak ``invalid_grant``), which no amount of waiting fixes.
    """
    text = (message or "").lower()
    if "invalid_grant" in text or "token is not active" in text:
        return "auth", True
    if status == 429 or "throttl" in text or "too many requests" in text:
        return "throttled", False
    if "entitlement" in text or "quota" in text or "allowance" in text or "exhaust" in text:
        return "quota", False
    if status == 403 or "not authorized" in text or "explicit deny" in text:
        return "denied", False
    if status == 401 or "credential" in text or "refresh failed" in text:
        return "auth", False
    return "error", False


@dataclass
class Account:
    """One Quick account in the pool.

    Attributes:
        name: Label derived from the credential filename.
        creds_file: The credential cache this account owns.
        auth: The manager that refreshes (and rewrites) that file.
        inflight: Requests currently in flight on this account.
        served: Requests completed on this account since start (round-robin tie-break).
        failures: Consecutive failures since the last success.
        cooldown_until: Epoch seconds until which the account is out of rotation.
        cooldown_reason: Why it is cooling down (shown on the status page).
        disabled_reason: Non-empty when the credential is permanently unusable.
        last_error: Most recent error text (truncated).
        last_used: Epoch seconds of the last request served.
    """

    name: str
    creds_file: Path
    auth: QuickAuthManager
    inflight: int = 0
    served: int = 0
    failures: int = 0
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    disabled_reason: str = ""
    last_error: str = ""
    last_used: float = 0.0

    @property
    def usage(self) -> Optional[UsageSnapshot]:
        """The newest entitlement reading for this account, if one has been seen."""
        return snapshot_for(self.name)

    def cooling(self, now: Optional[float] = None) -> bool:
        """True while the account is temporarily out of rotation."""
        return (now or time.time()) < self.cooldown_until

    def available(self, now: Optional[float] = None) -> bool:
        """True when the account may serve a request right now."""
        return not self.disabled_reason and not self.cooling(now)

    def status(self, now: Optional[float] = None) -> str:
        """One-word state for logs and the status page."""
        if self.disabled_reason:
            return "disabled"
        if self.cooling(now):
            return "cooling"
        return "ready"

    def session_remaining(self) -> Optional[float]:
        """Remaining share of the rolling session allowance, if known."""
        usage = self.usage
        return usage.session_remaining_pct if usage else None

    def monthly_remaining(self) -> Optional[float]:
        """Remaining share of the monthly entitlement, if known."""
        usage = self.usage
        if usage is None or usage.monthly_used_pct is None:
            return None
        return max(0.0, 100.0 - usage.monthly_used_pct)


def _bucket(value: Optional[float], width: int) -> int:
    """Bucket a percentage, treating an unknown reading as a full allowance.

    An account nobody has measured yet is assumed fresh so it gets its first request
    (which produces the reading that then ranks it honestly).
    """
    if value is None:
        return 100 // max(1, width)
    return int(max(0.0, value) // max(1, width))


class QuickPool:
    """The account pool: discovery, selection, and failure bookkeeping."""

    def __init__(self) -> None:
        self._accounts: Dict[str, Account] = {}

    # ------------------------------------------------------------------ discovery

    def discover(self, force: bool = False) -> List[Account]:
        """Build the account list from credential files (idempotent).

        Explicit :data:`quick.config.QUICK_ACCOUNTS` wins; otherwise every file
        matching :data:`quick.config.QUICK_CREDS_GLOB` in
        :data:`quick.config.QUICK_CREDS_DIR` becomes an account, plus
        :data:`quick.config.QUICK_CREDS_FILE` itself when it lives elsewhere.

        Args:
            force: Rebuild from scratch, dropping runtime state (cooldowns, counters).

        Returns:
            The accounts, ordered by name with ``default`` first.
        """
        if force:
            self._accounts = {}

        paths: List[Path] = []
        if QUICK_ACCOUNTS.strip():
            paths = [creds_path_for(n.strip()) for n in QUICK_ACCOUNTS.split(",") if n.strip()]
        else:
            try:
                paths = sorted(QUICK_CREDS_DIR.glob(QUICK_CREDS_GLOB))
            except OSError as exc:
                logger.warning("Quick pool: cannot scan {}: {}", QUICK_CREDS_DIR, exc)
            if QUICK_CREDS_FILE not in paths and QUICK_CREDS_FILE.exists():
                paths.append(QUICK_CREDS_FILE)

        seen: Set[str] = set()
        for path in paths:
            name = account_name_for(path)
            if name in seen:
                continue
            seen.add(name)
            existing = self._accounts.get(name)
            if existing is not None:
                existing.creds_file = path
                continue
            self._accounts[name] = Account(
                name=name, creds_file=path, auth=QuickAuthManager(path, name=name)
            )

        # A file that disappeared stops being a candidate, but never mid-flight.
        for name in list(self._accounts):
            if name not in seen and self._accounts[name].inflight == 0:
                logger.info("Quick pool: account '{}' removed (credential file gone).", name)
                del self._accounts[name]

        if not self._accounts:
            logger.warning(
                "Quick pool: no credential files found (dir={}, glob={}).",
                QUICK_CREDS_DIR, QUICK_CREDS_GLOB,
            )
        return self.accounts()

    def accounts(self) -> List[Account]:
        """All known accounts, ``default`` first then alphabetical."""
        return sorted(
            self._accounts.values(), key=lambda a: (a.name != DEFAULT_ACCOUNT, a.name)
        )

    def get(self, name: str) -> Optional[Account]:
        """Look up one account by name (``None`` if unknown)."""
        if not self._accounts:
            self.discover()
        return self._accounts.get(name)

    # ------------------------------------------------------------------ selection

    def select(self, exclude: Iterable[str] = ()) -> Optional[Account]:
        """Pick the account that should serve the next request.

        Args:
            exclude: Account names already tried for this request.

        Returns:
            The best candidate, or ``None`` when every account is disabled, cooling
            down, or excluded.
        """
        if not self._accounts:
            self.discover()
        now = time.time()
        skip = set(exclude)
        width = QUICK_POOL_QUOTA_BUCKET
        candidates = [
            a for a in self._accounts.values() if a.name not in skip and a.available(now)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda a: (
                -_bucket(a.session_remaining(), width),
                -_bucket(a.monthly_remaining(), width) if QUICK_POOL_AVOID_OVERAGE else 0,
                a.inflight,
                a.served,
                a.name != DEFAULT_ACCOUNT,   # perfect tie: same order as accounts()
                a.name,
            )
        )
        return candidates[0]

    def begin(self, account: Account) -> None:
        """Mark a request as started on ``account``."""
        account.inflight += 1
        account.last_used = time.time()

    def end(self, account: Account) -> None:
        """Mark a request as finished on ``account`` (success or not)."""
        account.inflight = max(0, account.inflight - 1)
        account.served += 1

    def note_success(self, account: Account) -> None:
        """Clear the failure streak after a request completes normally."""
        account.failures = 0
        account.last_error = ""

    def note_failure(
        self,
        account: Account,
        status: Optional[int] = None,
        message: str = "",
        cooldown_seconds: Optional[float] = None,
    ) -> str:
        """Record a failed request and take the account out of rotation.

        Args:
            account: The account that failed.
            status: HTTP status, when the failure had one.
            message: Error text (also shown, truncated, on the status page).
            cooldown_seconds: Explicit cooldown; otherwise derived from the failure kind.

        Returns:
            The failure kind (see :func:`classify_failure`).
        """
        kind, disable = classify_failure(status, message)
        account.failures += 1
        account.last_error = (message or "")[:300]
        if disable:
            account.disabled_reason = "credential rejected (invalid_grant) — re-upload this account's creds file"
            logger.error(
                "Quick pool: account '{}' DISABLED — {}", account.name, account.last_error
            )
            return kind
        seconds = cooldown_seconds if cooldown_seconds is not None else _COOLDOWN_BY_KIND.get(kind, 60)
        self.cool_down(account, seconds, kind)
        return kind

    def cool_down(self, account: Account, seconds: float, reason: str) -> None:
        """Put an account out of rotation for ``seconds`` (never shortens a longer rest)."""
        until = time.time() + max(0.0, seconds)
        if until <= account.cooldown_until:
            return
        account.cooldown_until = until
        account.cooldown_reason = reason
        logger.warning(
            "Quick pool: account '{}' cooling down {}s ({}).",
            account.name, int(seconds), reason,
        )

    def revive(self, account: Account) -> None:
        """Clear a disable/cooldown (used after a credential file is replaced)."""
        account.disabled_reason = ""
        account.cooldown_until = 0.0
        account.cooldown_reason = ""
        account.failures = 0

    def observe_usage(self, name: str, snapshot: UsageSnapshot) -> None:
        """React to a fresh entitlement reading for ``name``.

        Only a *hard* block benches an account: entitlement revoked, or the session
        allowance actually down to zero. A merely low allowance needs no special
        case — :meth:`select` already ranks it below its healthier siblings.

        ``resumeInMinutes`` is **not** a lockout timer: it is how long until the
        rolling session window resets, and it is populated while the account is still
        perfectly usable (verified live: 21 % left, ``resumeInMinutes`` 55,
        ``entitlementStatus`` ALLOWED). It is only used here to size the cooldown
        once the allowance really is exhausted.
        """
        account = self._accounts.get(name)
        if account is None:
            return
        if snapshot.entitlement_status and snapshot.entitlement_status != "ALLOWED":
            self.cool_down(
                account, _COOLDOWN_BY_KIND["quota"], f"entitlement {snapshot.entitlement_status}"
            )
        elif snapshot.session_remaining_pct is not None and snapshot.session_remaining_pct <= 0:
            seconds = (snapshot.resume_in_minutes * 60) or _COOLDOWN_BY_KIND["quota"]
            self.cool_down(account, seconds, "session allowance exhausted")

    # ------------------------------------------------------------------ reporting

    def snapshot(self) -> Dict[str, object]:
        """Render the pool state for the status page and the CLI.

        Returns:
            A JSON-serializable dict. It deliberately carries no credential material:
            no tokens, no tenant URL, no user ARN — the page it feeds is public.
        """
        now = time.time()
        accounts: List[Dict[str, object]] = []
        for account in self.accounts():
            usage = account.usage
            accounts.append({
                "name": account.name,
                "status": account.status(now),
                "session_remaining_pct": account.session_remaining(),
                "session_used_pct": usage.session_used_pct if usage else None,
                "monthly_used_pct": usage.monthly_used_pct if usage else None,
                "monthly_resets_at": usage.monthly_resets_at if usage else None,
                "overage_enabled": bool(usage.overage_enabled) if usage else False,
                "entitlement_status": usage.entitlement_status if usage else "",
                "reading_age_seconds": round(usage.age_seconds(), 1) if usage else None,
                "inflight": account.inflight,
                "served": account.served,
                "cooldown_seconds_left": max(0, int(account.cooldown_until - now)),
                "cooldown_reason": account.cooldown_reason if account.cooling(now) else "",
                "disabled_reason": account.disabled_reason,
                "last_error": account.last_error,
                "last_used_ago_seconds": round(now - account.last_used, 1) if account.last_used else None,
            })
        ready = [a for a in accounts if a["status"] == "ready"]
        known = [a["session_remaining_pct"] for a in ready if a["session_remaining_pct"] is not None]
        return {
            "accounts": accounts,
            "total": len(accounts),
            "ready": len(ready),
            "pool_remaining_pct": round(sum(known) / len(known), 1) if known else None,
            "generated_at": now,
        }


# Module-level singleton, mirroring quick.auth's manager.
pool = QuickPool()


def selectable_names(accounts: Sequence[Account]) -> List[str]:
    """Names of the accounts currently able to serve traffic (for logs)."""
    now = time.time()
    return [a.name for a in accounts if a.available(now)]
