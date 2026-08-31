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
2. rank by the **tighter of the session and monthly shares** — whichever allowance
   runs out first is the one that will stop the account — bucketed (raw percentages
   would ping-pong the choice between two accounts on every reading), then by the
   session share alone, which still discriminates once every monthly bucket is spent,
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
    QUICK_POOL_SOFT_INFLIGHT,
    QUICK_CREDS_DIR,
    QUICK_CREDS_FILE,
    QUICK_CREDS_GLOB,
    QUICK_POOL_AVOID_OVERAGE,
    QUICK_POOL_COOLDOWN_SECONDS,
    QUICK_POOL_MAX_COOLDOWN_SECONDS,
    QUICK_POOL_MAX_QUOTA_COOLDOWN_SECONDS,
    QUICK_POOL_OVERAGE_POLICY,
    QUICK_POOL_QUOTA_BUCKET,
)
from quick.usage_watch import UsageSnapshot, restore_snapshots, snapshot_for

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


_OVERAGE_POLICIES: Tuple[str, ...] = ("allow", "avoid")


def overage_policy() -> str:
    """The active overage *preference* (validated, honouring the legacy switch).

    It is a preference, never an eviction: ``avoid`` reorders selection so a spent
    account is picked last, but an account is only taken out of the pool when it
    genuinely stops working.

    Returns:
        ``avoid`` (default) or ``allow``.
    """
    policy = QUICK_POOL_OVERAGE_POLICY
    if policy not in _OVERAGE_POLICIES:
        return "avoid"
    if policy == "allow" and QUICK_POOL_AVOID_OVERAGE:
        return "avoid"
    return policy


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
    # Quota before throttling: an entitlement block is surfaced to the client as 429
    # (the honest code for "no capacity"), so the status alone would read as
    # throttling and earn a 5-minute cooldown instead of waiting for the reset.
    if "entitlement" in text or "quota" in text or "allowance" in text or "exhaust" in text:
        return "quota", False
    if status == 429 or "throttl" in text or "too many requests" in text:
        return "throttled", False
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
        cooldown_kind: Failure kind that started the current cooldown, so a later
            reading can retract a *quota* bench it contradicts without touching a
            bench a healthy usageSummary says nothing about (429, IAM deny, 5xx).
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
    cooldown_kind: str = ""
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
        """One-word state for logs and the status page.

        Being on overage is **not** a state: an account leaves the pool only when it
        actually stops working (a real failure, or the backend saying so). Overage
        only reorders :meth:`QuickPool.select`.
        """
        if self.disabled_reason:
            return "disabled"
        if self.cooling(now):
            return "cooling"
        return "ready"

    def monthly_reading_expired(self, now: Optional[float] = None) -> bool:
        """True once a reading's own ``resetsAt`` has passed.

        A reading states when its month ends; past that moment "0 units left" is not a
        fact about the account any more, it is last month's fact. This matters because
        the pool only re-reads an account it *selects*: ranking a spent account last on
        an expired reading would keep it there straight through the reset that refilled
        it. Unknown reset time means the reading never expires on its own.
        """
        usage = self.usage
        resets = usage.monthly_resets_at if usage else None
        return bool(resets) and (now or time.time()) >= resets

    def monthly_exhausted(self) -> bool:
        """True when this account's monthly entitlement is spent.

        Unknown (no reading yet, or one that has outlived its own reset) counts as
        *not* exhausted — an unmeasured account must still get its first request,
        which is what produces the reading.
        """
        usage = self.usage
        if usage is None or self.monthly_reading_expired():
            return False
        if usage.monthly_available_units is not None:
            return usage.monthly_available_units <= 0
        return usage.monthly_used_pct is not None and usage.monthly_used_pct >= 100

    def on_overage(self) -> bool:
        """True when serving here spends overage instead of the subscription.

        Requires overage to be *enabled*: with it off, a spent account is blocked by
        Quick rather than billed, and :meth:`QuickPool.observe_usage` benches it.
        """
        usage = self.usage
        return bool(usage and usage.overage_enabled) and self.monthly_exhausted()

    def eligible(self, now: Optional[float] = None) -> bool:
        """Whether the account may serve traffic (alias of :meth:`available`).

        Overage deliberately does not enter here: an account that still works stays
        selectable, however it is billed.
        """
        return self.available(now)

    def session_remaining(self) -> Optional[float]:
        """Remaining share of the rolling session allowance, if known."""
        usage = self.usage
        return usage.session_remaining_pct if usage else None

    def monthly_remaining(self) -> Optional[float]:
        """Remaining share of the monthly entitlement, if the reading still applies."""
        usage = self.usage
        if usage is None or usage.monthly_used_pct is None or self.monthly_reading_expired():
            return None
        return max(0.0, 100.0 - usage.monthly_used_pct)

    def headroom(self) -> Optional[float]:
        """Remaining share of whichever allowance runs out first, if any is known.

        Selection ranks on this rather than on the session share alone. The session
        window is what throttles an account minute to minute, but the monthly
        entitlement is what *hard-blocks* it — with overage off, spending the last unit
        earns a BLOCKED_MONTHLY refusal until the reset, not a bill. An account with a
        full session window and 4 % of its month left is one request from being benched
        for days, and ranking it top (as the session share alone did) walked straight
        into that. Ranking on the tighter of the two also spends the pool evenly by
        *fraction* consumed, which is the fair split when the accounts belong to
        different people and their limit profiles differ in size.

        Unknown counts as full: a freshly added account must still get its first
        request, which is what produces the reading that then ranks it honestly.
        """
        known = [v for v in (self.session_remaining(), self.monthly_remaining())
                 if v is not None]
        return min(known) if known else None

    def binding_allowance(self) -> str:
        """Which allowance :meth:`headroom` is reporting — for the status page."""
        session, monthly = self.session_remaining(), self.monthly_remaining()
        if session is None and monthly is None:
            return ""
        if monthly is None or (session is not None and session <= monthly):
            return "session"
        return "monthly"


def _bucket(value: Optional[float], width: int) -> int:
    """Bucket a percentage, treating an unknown reading as a full allowance.

    An account nobody has measured yet is assumed fresh so it gets its first request
    (which produces the reading that then ranks it honestly).
    """
    if value is None:
        return 100 // max(1, width)
    return int(max(0.0, value) // max(1, width))


def _reading_is_healthy(snapshot: UsageSnapshot) -> bool:
    """True when a reading says the account has quota to serve with, right now.

    Deliberately stricter than "Quick would answer": an account whose monthly bucket is
    spent reads ALLOWED while overage is on, and that is *not* grounds to retract a
    bench — the reading agrees with it. Only headroom the backend positively reports
    counts; anything it leaves unsaid is read as healthy, which matches the rest of the
    pool (an unmeasured account is treated as full).

    Args:
        snapshot: The freshest entitlement reading.

    Returns:
        Whether every bucket the reading mentions still has room.
    """
    if snapshot.entitlement_status and snapshot.entitlement_status != "ALLOWED":
        return False
    if snapshot.session_remaining_pct is not None and snapshot.session_remaining_pct <= 0:
        return False
    if snapshot.monthly_available_units is not None:
        return snapshot.monthly_available_units > 0
    if snapshot.monthly_used_pct is not None:
        return snapshot.monthly_used_pct < 100
    return True


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
        # Readings survive a restart (the pool would otherwise be blind to overage and
        # spent units for the first requests after every deploy).
        restore_snapshots()

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
                self._note_replaced_credential(existing)
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
            return self._early_trial(skip, now)

        # Overage decides the ORDER, never the membership: while some account still
        # has subscription headroom, a spent one is picked last — but if it is the
        # only one left it still serves. (As a mere sort key it only won inside the
        # same session bucket, which is exactly the case where both look equally
        # good and only the money differs.)
        if overage_policy() != "allow":
            with_headroom = [a for a in candidates if not a.on_overage()]
            if with_headroom:
                candidates = with_headroom

        # Spread concurrency before ranking on quota. In-flight only ever broke a tie
        # inside one bucket, so an account a single bucket ahead absorbed every
        # simultaneous request while its siblings idled — the shape that finds a
        # per-account rate limit. A demotion, never a drop: if every account is at the
        # cap this changes nothing and the quota ranking decides as before.
        if QUICK_POOL_SOFT_INFLIGHT > 0:
            spare = [a for a in candidates if a.inflight < QUICK_POOL_SOFT_INFLIGHT]
            if spare:
                candidates = spare

        candidates.sort(
            key=lambda a: (
                -_bucket(a.headroom(), width),
                # Then the session share. Once every account's monthly bucket is spent
                # (overage carrying the pool), the binding number is 0 everywhere and
                # would stop discriminating — but session throttling is still real, and
                # it is the dimension that moves minute to minute.
                -_bucket(a.session_remaining(), width),
                a.inflight,
                a.served,
                a.name != DEFAULT_ACCOUNT,   # perfect tie: same order as accounts()
                a.name,
            )
        )
        return candidates[0]

    def _early_trial(self, skip: Set[str], now: float) -> Optional[Account]:
        """The soonest-expiring benched account, when every account is benched.

        A bench is a *claim* that an account would fail; a 503 is the certainty that
        this request does. Once the whole pool is cooling there is nothing left to
        protect, so the account closest to its deadline gets its half-open trial early
        rather than the client getting a guaranteed error — and if the bench was a
        misdiagnosis (the failure was never the account's), this is what finds out.
        The bounded cost is one attempt against an account that may refuse; the bound
        is :data:`QUICK_POOL_MAX_ATTEMPTS`.

        A *disabled* account is still not tried: a credential Keycloak rejects fails
        deterministically until someone re-uploads the file, so trying it would spend a
        round-trip per request forever, and that failure already raised an alert.

        Args:
            skip: Accounts already tried for this request.
            now: Current time.

        Returns:
            The account to try early, or ``None`` when there is genuinely nothing left.
        """
        benched = [a for a in self._accounts.values()
                   if a.name not in skip and not a.disabled_reason]
        if not benched:
            return None
        account = min(benched, key=lambda a: a.cooldown_until)
        logger.warning(
            "Quick pool: every account is benched; trying '{}' early ({}s of its "
            "cooldown left, {}).",
            account.name, max(0, int(account.cooldown_until - now)),
            account.cooldown_reason or "no reason recorded",
        )
        return account

    def absolve(self, account: Account, reason: str) -> None:
        """Release a bench that later evidence disproved.

        The pool benches an account when a request fails on it — a diagnosis made from
        one data point, and wrong whenever the failure belonged to the *request*
        instead. When the very next account answers the same request with the same
        error, that is the experiment: the account was never at fault, so its bench is
        withdrawn along with the failure it counted. This is :meth:`observe_usage`'s
        rule (a reading may retract a bench it could have caused) applied to the other
        kind of evidence, and it needs no list of error strings to recognise.

        A disabled account is left alone: that state is not a cooldown and is cleared
        only by a replaced credential file.

        Args:
            account: The account whose bench is being withdrawn.
            reason: What disproved it, for the log.
        """
        if account.disabled_reason or not account.cooldown_until:
            return
        logger.info("Quick pool: releasing account '{}' — {}.", account.name, reason)
        account.cooldown_until = 0.0
        account.cooldown_reason = ""
        account.cooldown_kind = ""
        account.failures = max(0, account.failures - 1)

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
        # Cross-reference the free entitlement reading rather than trusting the error
        # text. When the monthly bucket is spent, ANY failure is that block however the
        # backend worded it — and we have never seen its wording, because these accounts
        # have always had overage on. Keyword-matching alone would retry a month-long
        # block every 60 s, and each retry costs a real request its first attempt.
        if kind != "quota" and account.monthly_exhausted():
            logger.info(
                "Quick pool: treating '{}' failure on account '{}' as a quota block "
                "(monthly entitlement is spent).", kind, account.name,
            )
            kind = "quota"
        if kind == "quota" and account.monthly_exhausted():
            usage = account.usage
            self.cool_down_until(
                account, usage.monthly_resets_at if usage else None,
                "monthly entitlement spent",
            )
            return kind

        seconds = (
            cooldown_seconds if cooldown_seconds is not None
            else self._backoff(kind, account.failures)
        )
        self.cool_down(account, seconds, kind, kind)
        return kind

    @staticmethod
    def _backoff(kind: str, failures: int) -> float:
        """Cooldown for a failure kind, doubled per consecutive failure and capped.

        Without this a permanently broken account is retried at a fixed interval
        forever, and every retry spends a real request's first attempt on it.

        Args:
            kind: Failure kind from :func:`classify_failure`.
            failures: Consecutive failures on the account (reset by a success).

        Returns:
            Seconds to stay out of rotation.
        """
        base = _COOLDOWN_BY_KIND.get(kind, 60)
        return float(min(base * 2 ** max(0, failures - 1), QUICK_POOL_MAX_COOLDOWN_SECONDS))

    def cool_down(
        self, account: Account, seconds: float, reason: str, kind: str = ""
    ) -> None:
        """Put an account out of rotation for ``seconds`` (never shortens a longer rest).

        Args:
            account: The account to bench.
            seconds: How long to stay out of rotation.
            reason: Shown on the status page.
            kind: Failure kind behind it (see :func:`classify_failure`), remembered so
                :meth:`observe_usage` knows whether a healthy reading contradicts it.
        """
        until = time.time() + max(0.0, seconds)
        if until <= account.cooldown_until:
            return
        account.cooldown_until = until
        account.cooldown_reason = reason
        account.cooldown_kind = kind or reason
        logger.warning(
            "Quick pool: account '{}' cooling down {}s ({}).",
            account.name, int(seconds), reason,
        )

    def cool_down_until(self, account: Account, epoch: Optional[int], reason: str) -> None:
        """Bench an account until an absolute deadline (e.g. the monthly reset).

        The deadline is capped by :data:`QUICK_POOL_MAX_QUOTA_COOLDOWN_SECONDS`: a
        monthly ``resetsAt`` can be three weeks out, and sleeping that long assumes the
        entitlement cannot change before then — which is wrong, because an admin can
        raise the limit profile at any moment and a benched account is deliberately
        never probed. The cap makes the wait a repeating half-open trial instead: the
        first request after it either succeeds, or fails over and re-cools.

        Args:
            account: The account to bench.
            epoch: Unix timestamp to sleep until; falsy falls back to the quota default.
            reason: Shown on the status page.
        """
        if not epoch:
            self.cool_down(account, _COOLDOWN_BY_KIND["quota"], reason, "quota")
            return
        seconds = max(0.0, epoch - time.time())
        if QUICK_POOL_MAX_QUOTA_COOLDOWN_SECONDS > 0:
            seconds = min(seconds, float(QUICK_POOL_MAX_QUOTA_COOLDOWN_SECONDS))
        self.cool_down(account, seconds, reason, "quota")

    def _note_replaced_credential(self, account: Account) -> None:
        """Re-enable an account whose credential file somebody just replaced.

        This is the recovery path for the one failure nothing else can undo: a
        refresh token Keycloak rejects outright (``invalid_grant``) disables the
        account, and no amount of waiting fixes it — only a new file does. Uploading
        one is therefore the operator saying "try again", so it clears the disable,
        the cooldown and the failure streak, and drops the dead in-memory token.

        The trigger is the file no longer matching what this account's own manager
        last wrote (:meth:`quick.auth.QuickAuthManager.file_replaced_externally`),
        never mtime alone: the gateway rewrites every file on every token rotation,
        so mtime alone would revive a cooling account several times an hour.

        Args:
            account: The account whose credential file to check.
        """
        if not account.auth.file_replaced_externally():
            return
        was = account.disabled_reason or account.cooldown_reason
        account.auth.mark_stale()
        self.revive(account)
        logger.info(
            "Quick pool: account '{}' credential file replaced — back in rotation{}.",
            account.name, f" (was: {was})" if was else "",
        )

    def revive(self, account: Account) -> None:
        """Clear a disable/cooldown (used after a credential file is replaced)."""
        account.disabled_reason = ""
        account.cooldown_until = 0.0
        account.cooldown_reason = ""
        account.cooldown_kind = ""
        account.failures = 0

    def release(self, account: Account, reason: str) -> None:
        """Put a cooling account back in rotation (the cooldown's premise no longer holds).

        Only the cooldown is lifted — the failure streak is left alone, so if the
        account fails again the back-off resumes where it was instead of restarting
        at the shortest step.

        Args:
            account: The account to return to rotation.
            reason: Why the bench was retracted (logged).
        """
        if not account.cooling():
            return
        account.cooldown_until = 0.0
        account.cooldown_reason = ""
        account.cooldown_kind = ""
        logger.info("Quick pool: account '{}' back in rotation ({}).", account.name, reason)

    def observe_usage(self, name: str, snapshot: UsageSnapshot) -> None:
        """React to a fresh entitlement reading for ``name``.

        Only a *hard* block benches an account: entitlement revoked, or the session
        allowance actually down to zero. A merely low allowance needs no special
        case — :meth:`select` already ranks it below its healthier siblings.

        It works in **both** directions: a reading that reports real headroom retracts a
        quota bench, because that bench's whole premise was "this account has no quota"
        and the backend just said otherwise. It retracts nothing else — a healthy
        usageSummary is no evidence about a 429, an IAM deny or a 5xx.

        ``resumeInMinutes`` is **not** a lockout timer: it is how long until the
        rolling session window resets, and it is populated while the account is still
        perfectly usable (verified live: 21 % left, ``resumeInMinutes`` 55,
        ``entitlementStatus`` ALLOWED). It is only used here to size the cooldown
        once the allowance really is exhausted.
        """
        account = self._accounts.get(name)
        if account is None:
            return
        if _reading_is_healthy(snapshot):
            # The bench said "no quota"; the backend now says there is. Retract it —
            # otherwise a raised limit profile (480 -> 1080 units/user, seen live) can
            # only be discovered when the cooldown finally lapses, and the headroom in
            # between expires unused at the monthly reset.
            if account.cooldown_kind == "quota":
                self.release(account, "entitlement reading is healthy again")
            return
        if snapshot.entitlement_status and snapshot.entitlement_status != "ALLOWED":
            reason = f"entitlement {snapshot.entitlement_status}"
            # A monthly block lasts until the entitlement resets, so the reading's own
            # ``resetsAt`` is the honest deadline. A flat cooldown would instead release
            # the account every 15 minutes to spend one more rejected request — and,
            # before the block was recognised as a failure at all, to hand one client an
            # empty answer each time it came back.
            if account.monthly_exhausted() or "MONTHLY" in snapshot.entitlement_status.upper():
                self.cool_down_until(account, snapshot.monthly_resets_at, reason)
            else:
                self.cool_down(account, _COOLDOWN_BY_KIND["quota"], reason)
        elif snapshot.session_remaining_pct is not None and snapshot.session_remaining_pct <= 0:
            seconds = (snapshot.resume_in_minutes * 60) or _COOLDOWN_BY_KIND["quota"]
            self.cool_down(account, seconds, "session allowance exhausted")
        # A spent monthly bucket is deliberately NOT benched here, even with overage
        # off. "It will surely be blocked" is a guess about a rejection we have never
        # observed, and acting on it would take a working account out of the pool.
        # Let the backend be the arbiter: if the next request really is refused,
        # note_failure benches it — and uses this reading to size the cooldown.

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
                "monthly_available_units": usage.monthly_available_units if usage else None,
                "monthly_provisioned_units": usage.monthly_provisioned_units if usage else None,
                "monthly_resets_at": usage.monthly_resets_at if usage else None,
                "headroom_pct": account.headroom(),
                "binding_allowance": account.binding_allowance(),
                "overage_enabled": bool(usage.overage_enabled) if usage else False,
                "on_overage": account.on_overage(),
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
        # Averaged over the *binding* allowance, not the session one: an average that
        # reads 100% while every account is out of monthly units is worse than no
        # number at all — it is the pool's headline, and it should mean "capacity".
        known = [a["headroom_pct"] for a in ready if a["headroom_pct"] is not None]
        return {
            "accounts": accounts,
            "total": len(accounts),
            "ready": len(ready),
            "pool_remaining_pct": round(sum(known) / len(known), 1) if known else None,
            "overage_policy": overage_policy(),
            "generated_at": now,
        }


# Module-level singleton, mirroring quick.auth's manager.
pool = QuickPool()


def selectable_names(accounts: Sequence[Account]) -> List[str]:
    """Names of the accounts currently able to serve traffic (for logs)."""
    now = time.time()
    return [a.name for a in accounts if a.eligible(now)]
