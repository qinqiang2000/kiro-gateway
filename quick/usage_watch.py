# -*- coding: utf-8 -*-

"""Watch the Amazon Quick account's session allowance and alert before it runs out.

Amazon Quick meters two buckets — a rolling **session** allowance and a **monthly**
entitlement. The desktop app renders both in its profile flyout ("Session usage 27 %",
the bar turning red at 90 %). Those numbers come from ``GET /profile/usage`` on the
app's *local* HTTP server — a headless gateway has no such server, and the tenant
DataPlane does not serve that path (verified: 404 ``UnknownOperationException``).

The same numbers ride along on **every inference response**: the Converse
``metadata`` event carries a ``usageSummary`` sibling of the event payload::

    {"entitlementStatus": "ALLOWED",
     "overageEnabled": true,
     "sessionUsage": {"resumeInMinutes": 0, "usedPercentage": 38},
     "monthlyUsage": {"availableUnits": 0, "provisionedUnits": 720,
                      "resetsAt": 1788220800, "usedPercentage": 100}}

``usedPercentage`` is the share **consumed**, so what the alert cares about is
``100 - usedPercentage`` — the share still available.

That makes the watch nearly free: every response the gateway already streams updates
the snapshot (:func:`observe_event`, called from :mod:`quick.streaming`). A cycle
spends one 1-token request only when the gateway has been idle longer than the watch
interval and has nothing fresh to read.

The alert is **edge-triggered with hysteresis**: it fires once when the remaining
share drops below :data:`QUICK_SESSION_ALERT_REMAINING_PCT`, then stays silent until
the share recovers to at or above that threshold (a new session window, typically) —
so a long stretch at 3 % costs one message, not one per hour.

Usage::

    python -m quick.usage_watch              # read the current usage (probes if stale)
    python -m quick.usage_watch --json       # machine-readable report
    python -m quick.usage_watch --notify     # also push an alert to the webhook
    python -m quick.usage_watch --probe      # force a fresh 1-token probe

Exit codes: ``0`` above the threshold, ``10`` below it, ``1`` usage unreadable.
"""

import argparse
import asyncio
import contextvars
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from loguru import logger

from quick.config import QUICKWORK_HOME
from quick.model_watch import QUICK_ALERT_WEBHOOK, send_alert

# Mirrors quick/model_watch.py: the CLI runs without importing main.py, so the .env
# has to be loaded here too.
load_dotenv()

if TYPE_CHECKING:  # pragma: no cover - import cycle: quick.pool imports this module
    from quick.pool import Account

JsonDict = Dict[str, object]

# Background-task interval when the watch runs inside the gateway (0 disables it).
QUICK_SESSION_WATCH_INTERVAL: int = int(os.getenv("QUICK_SESSION_WATCH_INTERVAL", "3600"))

# Alert when the *remaining* session share drops below this many percent. The app's
# own progress bar turns red at 90 % used, i.e. 10 % remaining.
QUICK_SESSION_ALERT_REMAINING_PCT: float = float(
    os.getenv("QUICK_SESSION_ALERT_REMAINING_PCT", "10")
)

# Where the armed/disarmed flag and the last reading are kept, so a restart does not
# re-fire an alert that was already sent.
QUICK_SESSION_WATCH_STATE: Path = Path(
    os.getenv("QUICK_SESSION_WATCH_STATE", str(QUICKWORK_HOME / "session-usage-state.json"))
).expanduser()

# Model used for the fallback probe — the cheapest authorized one; the usageSummary
# is account-wide, so the model only decides what the probe costs.
QUICK_SESSION_PROBE_MODEL: str = os.getenv(
    "QUICK_SESSION_PROBE_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

PROBE_TIMEOUT_SECONDS: float = 60.0


# ==================================================================================================
# Snapshot
# ==================================================================================================

@dataclass(frozen=True)
class UsageSnapshot:
    """One reading of the account's entitlement, as reported by Quick.

    The monthly bucket is metered in **units** (``availableUnits`` of
    ``provisionedUnits``), which is the number worth watching: a percentage hides
    how much headroom is actually left. Observed live: ~0.65 units per streaming
    Opus request, on a 720-unit monthly allowance.
    """

    session_used_pct: Optional[float]
    session_remaining_pct: Optional[float]
    resume_in_minutes: int = 0
    monthly_used_pct: Optional[float] = None
    monthly_available_units: Optional[float] = None
    monthly_provisioned_units: Optional[float] = None
    monthly_resets_at: Optional[int] = None
    entitlement_status: str = ""
    overage_enabled: bool = False
    observed_at: float = field(default_factory=time.time)

    def age_seconds(self) -> float:
        """Seconds since this reading was taken."""
        return max(0.0, time.time() - self.observed_at)

    def to_dict(self) -> JsonDict:
        """Render the snapshot as a JSON-friendly dict (for reports and state)."""
        return {
            "session_used_pct": self.session_used_pct,
            "session_remaining_pct": self.session_remaining_pct,
            "resume_in_minutes": self.resume_in_minutes,
            "monthly_used_pct": self.monthly_used_pct,
            "monthly_available_units": self.monthly_available_units,
            "monthly_provisioned_units": self.monthly_provisioned_units,
            "monthly_resets_at": self.monthly_resets_at,
            "entitlement_status": self.entitlement_status,
            "overage_enabled": self.overage_enabled,
            "observed_at": self.observed_at,
            "age_seconds": round(self.age_seconds(), 1),
        }


def _num(value: object) -> Optional[float]:
    """Coerce a JSON scalar to ``float``, or ``None`` if it is not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def parse_usage_summary(summary: object) -> Optional[UsageSnapshot]:
    """Parse Quick's ``usageSummary`` object into a :class:`UsageSnapshot`.

    Args:
        summary: The ``usageSummary`` value from a Converse ``metadata`` frame.

    Returns:
        The parsed snapshot, or ``None`` if the object is missing or carries no
        session percentage (nothing to alert on).
    """
    if not isinstance(summary, dict):
        return None
    session = summary.get("sessionUsage")
    session = session if isinstance(session, dict) else {}
    used = _num(session.get("usedPercentage"))
    if used is None:
        return None
    monthly = summary.get("monthlyUsage")
    monthly = monthly if isinstance(monthly, dict) else {}
    resets_at = _num(monthly.get("resetsAt"))
    resume = _num(session.get("resumeInMinutes")) or 0.0
    return UsageSnapshot(
        session_used_pct=used,
        session_remaining_pct=max(0.0, 100.0 - used),
        resume_in_minutes=int(resume),
        monthly_used_pct=_num(monthly.get("usedPercentage")),
        monthly_available_units=_num(monthly.get("availableUnits")),
        monthly_provisioned_units=_num(monthly.get("provisionedUnits")),
        monthly_resets_at=int(resets_at) if resets_at else None,
        entitlement_status=str(summary.get("entitlementStatus") or ""),
        overage_enabled=bool(summary.get("overageEnabled")),
    )


# ==================================================================================================
# Passive observation (free — every response the gateway serves carries the numbers)
# ==================================================================================================

_latest: Optional[UsageSnapshot] = None

# Newest reading per pool account. A response only says what an account's allowance
# is, never which account it came from, so the request path stamps the account it
# selected on this context variable and every frame decoded underneath is attributed
# to it. (A ContextVar rather than a parameter because the summary is read deep in
# the stream translator, four call layers below the route that picks the account.)
_snapshots: Dict[str, UsageSnapshot] = {}

current_account: contextvars.ContextVar[str] = contextvars.ContextVar(
    "quick_current_account", default=""
)


def record_usage_summary(summary: object, account: Optional[str] = None) -> None:
    """Record a ``usageSummary`` as the newest reading. Never raises.

    Args:
        summary: The ``usageSummary`` object from a Converse ``metadata`` frame.
        account: Account the reading belongs to; defaults to :data:`current_account`.
    """
    global _latest
    snapshot = parse_usage_summary(summary)
    if snapshot is None:
        return
    previous = _latest
    _latest = snapshot
    name = account if account is not None else current_account.get()
    if name:
        _snapshots[name] = snapshot
        # Let the pool react to a window that just closed (resumeInMinutes) or an
        # entitlement that was revoked, without waiting for a request to fail.
        try:
            from quick.pool import pool

            pool.observe_usage(name, snapshot)
        except Exception as exc:  # noqa: BLE001 - observation must never break a response
            logger.debug("Quick pool could not observe usage for {}: {}", name, exc)
    if previous is None or previous.session_used_pct != snapshot.session_used_pct:
        logger.debug(
            "Quick session usage[{}]: {}% used ({}% left), monthly {}% used.",
            name or "?",
            _pct(snapshot.session_used_pct),
            _pct(snapshot.session_remaining_pct),
            _pct(snapshot.monthly_used_pct),
        )


def observe_event(event: JsonDict, account: Optional[str] = None) -> None:
    """Record the ``usageSummary`` carried by one decoded Quick stream frame.

    Quick hangs the entitlement snapshot off the ``bedrockStreamEvent`` *wrapper*
    (a sibling of ``eventType``/``payload``), so this takes the raw decoded frame —
    before :func:`quick.streaming._unwrap_bedrock_event` drops the wrapper. Frames
    without a summary (i.e. all but ``metadata``) are ignored.

    Args:
        event: A decoded event dict from :class:`quick.streaming.EventStreamDecoder`.
        account: Account the frame belongs to; defaults to :data:`current_account`.
    """
    payload = event.get("payload")
    if isinstance(payload, dict):
        record_usage_summary(payload.get("usageSummary"), account)


def latest_snapshot() -> Optional[UsageSnapshot]:
    """The newest reading seen in this process, on any account."""
    return _latest


_restored = False


def restore_snapshots() -> int:
    """Seed the in-memory readings from the persisted state, once per process.

    Without this the pool is blind for the first requests after every restart: with
    no reading, an account cannot be known to be on overage or out of units, so the
    money guard and the quota bench are both inert exactly when a redeploy has just
    reset them. The restored readings keep their original ``observed_at``, so they
    still count as stale and the next cycle refreshes them — stale is strictly better
    than absent here, because "unknown" is treated as a *full* allowance.

    Returns:
        How many account readings were restored.
    """
    global _restored
    if _restored:
        return 0
    _restored = True
    fields = {f for f in UsageSnapshot.__dataclass_fields__}
    restored = 0
    for name, entry in (load_all_state().get("accounts") or {}).items():
        last = entry.get("last") if isinstance(entry, dict) else None
        if not isinstance(last, dict):
            continue
        try:
            _snapshots[name] = UsageSnapshot(**{k: v for k, v in last.items() if k in fields})
            restored += 1
        except TypeError as exc:  # a state file from an older field layout
            logger.debug("Could not restore the {} usage reading: {}", name, exc)
    if restored:
        logger.info("Restored {} persisted Quick usage reading(s).", restored)
    return restored


def snapshot_for(account: str) -> Optional[UsageSnapshot]:
    """The newest reading for one account, if it has been measured.

    Args:
        account: Account name.

    Returns:
        Its latest :class:`UsageSnapshot`, or ``None`` when nothing has been read
        from that account yet (the pool treats that as a full allowance).
    """
    return _snapshots.get(account)


def all_snapshots() -> Dict[str, UsageSnapshot]:
    """Every per-account reading held in this process."""
    return dict(_snapshots)


# ==================================================================================================
# Active probe (only when the passive reading is stale)
# ==================================================================================================

async def probe_usage(
    model_id: str = "", account: Optional["Account"] = None
) -> Optional[UsageSnapshot]:
    """Spend one 1-token request to refresh the usage snapshot.

    Args:
        model_id: Model to probe with; defaults to :data:`QUICK_SESSION_PROBE_MODEL`.
        account: Pool account to probe; ``None`` uses the process-wide credentials.

    Returns:
        The fresh snapshot, or ``None`` if the request failed or returned no
        ``usageSummary``.
    """
    # Imported here so the passive path works on a host without Quick credentials,
    # and to keep quick.streaming's import of this module acyclic.
    from quick.client import QuickAPIError, converse_stream
    from quick.streaming import EventStreamDecoder

    converse_input: JsonDict = {
        "modelId": model_id or QUICK_SESSION_PROBE_MODEL,
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        "inferenceConfig": {"maxTokens": 1},
    }
    decoder = EventStreamDecoder()
    name = account.name if account is not None else current_account.get()
    before = _snapshots.get(name) if name else _latest
    if name:
        current_account.set(name)

    async def _drive() -> None:
        async for chunk in converse_stream(converse_input, account):
            for event in decoder.feed(chunk):
                observe_event(event, name or None)

    try:
        await asyncio.wait_for(_drive(), PROBE_TIMEOUT_SECONDS)
    except QuickAPIError as exc:
        logger.warning("Session-usage probe failed: HTTP {} {}", exc.status_code, exc.message[:200])
        if account is not None:
            from quick.pool import pool

            pool.note_failure(account, exc.status_code, exc.message)
        return None
    except asyncio.TimeoutError:
        logger.warning("Session-usage probe timed out after {}s.", PROBE_TIMEOUT_SECONDS)
        return None
    except Exception as exc:  # noqa: BLE001 - a probe must never take the caller down
        logger.warning("Session-usage probe failed: {}: {}", type(exc).__name__, exc)
        return None

    fresh = _snapshots.get(name) if name else _latest
    if fresh is None or fresh is before:
        logger.warning("Session-usage probe returned no usageSummary (account={}).",
                       name or "default")
        return None
    return fresh


async def read_usage(
    max_age: float, allow_probe: bool = True, account: Optional["Account"] = None
) -> Optional[UsageSnapshot]:
    """Return a usage reading, probing only if the passive one is too old.

    Args:
        max_age: How old (seconds) the passive reading may be and still count as
            current. A busy gateway refreshes it on every response.
        allow_probe: Whether a stale reading may be refreshed with one request.
        account: Pool account to read; ``None`` reads the newest reading overall.

    Returns:
        The freshest snapshot available, or ``None`` if there is none.
    """
    snapshot = _snapshots.get(account.name) if account is not None else _latest
    if snapshot is not None and snapshot.age_seconds() <= max_age:
        return snapshot
    if not allow_probe:
        return snapshot
    return await probe_usage(account=account) or snapshot


# ==================================================================================================
# Alert state (edge-triggered, so a long stretch below the threshold alerts once)
# ==================================================================================================

DEFAULT_STATE_KEY: str = "default"
POOL_STATE_KEY: str = "pool"


def load_all_state() -> JsonDict:
    """Load the whole state file, migrating the pre-pool single-account shape.

    The file used to be one flat ``{"armed": …, "last": …}`` object. With a pool it
    is ``{"accounts": {"<name>": {…}}, "pool": {…}}``; a legacy file is read as the
    ``default`` account's state so an upgrade does not re-fire an alert that was
    already sent.
    """
    try:
        raw = json.loads(QUICK_SESSION_WATCH_STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"accounts": {}}
    except (OSError, ValueError) as exc:
        logger.warning("Session-usage state unreadable ({}); starting fresh.", exc)
        return {"accounts": {}}
    if not isinstance(raw, dict):
        return {"accounts": {}}
    if "accounts" not in raw:
        legacy = {k: v for k, v in raw.items() if k != "accounts"}
        return {"accounts": {DEFAULT_STATE_KEY: legacy}} if legacy else {"accounts": {}}
    accounts = raw.get("accounts")
    raw["accounts"] = accounts if isinstance(accounts, dict) else {}
    return raw


def load_state(account: str = "") -> JsonDict:
    """Load one account's alert state (empty when it has none yet).

    Args:
        account: Account name; empty means the ``default`` slot, which is also where
            a pre-pool state file lands.
    """
    entry = load_all_state()["accounts"].get(account or DEFAULT_STATE_KEY)
    return entry if isinstance(entry, dict) else {}


def save_state(state: JsonDict, account: str = "") -> None:
    """Persist one account's alert state (best effort — a failure costs a repeat alert).

    Args:
        state: The account's state object.
        account: Account name; empty means the ``default`` slot.
    """
    full = load_all_state()
    full["accounts"][account or DEFAULT_STATE_KEY] = state
    _write_state(full)


def _write_state(full: JsonDict) -> None:
    """Write the whole state file."""
    try:
        QUICK_SESSION_WATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
        QUICK_SESSION_WATCH_STATE.write_text(
            json.dumps(full, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Could not write session-usage state: {}", exc)


def _pct(value: Optional[float]) -> str:
    """Render a percentage without a trailing ``.0``."""
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _reset_time(epoch: Optional[int]) -> str:
    """Render a reset timestamp as ``YYYY-MM-DD HH:MM UTC``."""
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return ""


def format_alert(
    snapshot: UsageSnapshot,
    threshold: float = QUICK_SESSION_ALERT_REMAINING_PCT,
    account: str = "",
) -> str:
    """Render a low-session-allowance alert as a short chat message.

    Args:
        snapshot: The reading that tripped the threshold.
        threshold: The remaining-percentage threshold that was crossed.
        account: Pool account the reading belongs to (omitted when single-account).

    Returns:
        Three or four lines: how much is left, when it comes back, the monthly
        bucket for context, and the rule for the next alert.
    """
    who = f"（账号 {account}）" if account else ""
    lines = [
        f"⚠️ Quick 会话额度告警{who}：剩余 {_pct(snapshot.session_remaining_pct)}%"
        f"（已用 {_pct(snapshot.session_used_pct)}%，阈值 剩余 <{_pct(threshold)}%）"
    ]
    if snapshot.resume_in_minutes:
        # resumeInMinutes = 距离滚动会话窗口重置的时间（额度未耗尽时也会有值）。
        lines.append(f"本轮会话窗口约 {snapshot.resume_in_minutes} 分钟后重置")
    if snapshot.monthly_used_pct is not None:
        monthly = f"月度：已用 {_pct(snapshot.monthly_used_pct)}%"
        if snapshot.overage_enabled:
            monthly += "（overage 开启）"
        reset = _reset_time(snapshot.monthly_resets_at)
        if reset:
            monthly += f"，{reset} 重置"
        lines.append(monthly)
    if snapshot.entitlement_status and snapshot.entitlement_status != "ALLOWED":
        lines.append(f"entitlementStatus: {snapshot.entitlement_status}")
    lines.append(f"恢复到 剩余 ≥{_pct(threshold)}% 后才会再次告警")
    return "\n".join(lines)


# ==================================================================================================
# Watch cycle
# ==================================================================================================

async def watch_once(
    notify: bool = False,
    save: bool = True,
    allow_probe: bool = True,
    threshold: float = QUICK_SESSION_ALERT_REMAINING_PCT,
    account: str = "",
) -> Tuple[JsonDict, bool]:
    """Run one cycle: read the usage, alert if it just crossed the threshold, persist.

    The alert is edge-triggered: it fires only while the state is *armed*, and the
    state re-arms once the remaining share is back at or above ``threshold``. A push
    that fails leaves the state armed, so the next cycle retries instead of losing
    the alert.

    Args:
        notify: Whether to push an alert to the webhook (log-only when ``False``).
        save: Whether to write the state back (``False`` for a dry run).
        allow_probe: Whether a stale reading may be refreshed with one request.
        threshold: Remaining-percentage threshold to alert below.
        account: Pool account to check; empty means "whatever this process last read"
            (the single-account behaviour), keyed under the ``default`` state slot.

    Returns:
        ``(report, alerted)`` — ``alerted`` is ``True`` only when a push succeeded.
    """
    acct = None
    if account:
        from quick.pool import pool

        acct = pool.get(account)
    state = load_state(account)
    armed = state.get("armed") is not False  # unknown / first run = armed
    snapshot = await read_usage(max_age=float(QUICK_SESSION_WATCH_INTERVAL or 3600),
                                allow_probe=allow_probe, account=acct)
    if snapshot is None:
        logger.warning("Quick session usage unavailable for account '{}' "
                       "(no response seen and no probe result).", account or "default")
        return {"available": False, "armed": armed, "threshold": threshold,
                "account": account or DEFAULT_STATE_KEY}, False

    remaining = snapshot.session_remaining_pct
    low = remaining is not None and remaining < threshold
    fired = False

    if low and armed:
        if notify:
            fired = await send_alert(format_alert(snapshot, threshold, account))
            if not fired:
                logger.warning("Session-usage alert push failed; staying armed to retry "
                               "on the next cycle.")
        else:
            logger.warning("Quick session allowance低于阈值：剩余 {}% (< {}%) — 告警未推送"
                           "（未开启 notify / 未配置 webhook）。",
                           _pct(remaining), _pct(threshold))
        if fired:
            armed = False
    elif not low:
        armed = True  # recovered (or a new session window) — re-arm for the next drop

    report: JsonDict = dict(snapshot.to_dict())
    report.update({
        "available": True,
        "account": account or DEFAULT_STATE_KEY,
        "threshold": threshold,
        "below_threshold": low,
        "armed": armed,
        "alert_pushed": fired,
    })
    if acct is not None:
        report["status"] = acct.status()
    if save:
        new_state: JsonDict = {"armed": armed, "last": snapshot.to_dict(),
                               "updated_at": time.time()}
        if acct is not None:
            new_state["disabled"] = bool(acct.disabled_reason)
        if fired:
            new_state["alerted_at"] = time.time()
        elif state.get("alerted_at") is not None and not armed:
            new_state["alerted_at"] = state["alerted_at"]
        save_state(new_state, account)
    return report, fired


# ==================================================================================================
# Pool cycle (every account, plus the pool-level "we are out of accounts" alert)
# ==================================================================================================

def format_pool_alert(report: JsonDict) -> str:
    """Render the pool-exhausted alert — the "go add an account" signal."""
    total = report.get("total", 0)
    lines = [f"🛑 Quick 账号池告警：{total} 个账号全部不可用（冷却/停用/额度耗尽）"]
    for entry in report.get("accounts", []):
        if not isinstance(entry, dict):
            continue
        detail = entry.get("disabled_reason") or entry.get("cooldown_reason") or "?"
        left = entry.get("session_remaining_pct")
        lines.append(
            f"· {entry.get('name')}：{entry.get('status')}"
            f"（剩余 {_pct(_num(left))}%，{detail}）"
        )
    lines.append("恢复任一账号后才会再次告警")
    return "\n".join(lines)


def format_disabled_alert(name: str, reason: str) -> str:
    """Render the per-account "this credential is dead" alert."""
    return (f"🛑 Quick 账号 {name} 凭证失效：{reason}\n"
            f"处理：在已登录该账号的 Mac 上重新导出 creds，"
            f"再 ./quick/deploy.sh --creds-only {name}")


async def watch_pool_once(
    notify: bool = False,
    save: bool = True,
    allow_probe: bool = True,
    threshold: float = QUICK_SESSION_ALERT_REMAINING_PCT,
) -> Tuple[JsonDict, bool]:
    """Run one watch cycle across every pool account.

    Each account gets its own edge-triggered low-allowance alert (so two accounts
    running dry are two messages, not one ambiguous one), plus two pool-level ones:
    a credential that died, and "every account is unusable" — the only alert that
    actually means *go add an account*.

    Args:
        notify: Whether to push alerts to the webhook.
        save: Whether to persist alert state.
        allow_probe: Whether a stale reading may be refreshed with one request.
        threshold: Remaining-percentage threshold to alert below.

    Returns:
        ``(report, alerted)`` where ``report`` carries the pool snapshot plus each
        account's report, and ``alerted`` is True if any push succeeded.
    """
    from quick.pool import pool

    accounts = pool.discover()
    if not accounts:
        return await watch_once(notify=notify, save=save, allow_probe=allow_probe,
                                threshold=threshold)

    fired_any = False
    reports: List[JsonDict] = []
    for account in accounts:
        # An account benched until a known deadline (monthly reset, session window,
        # dead credential) needs no probe — it would just fail, once per cycle, until
        # the deadline. Its reading resumes the moment real traffic returns to it.
        probe = allow_probe and not account.cooling() and not account.disabled_reason
        report, fired = await watch_once(notify=notify, save=save, allow_probe=probe,
                                         threshold=threshold, account=account.name)
        reports.append(report)
        fired_any = fired_any or fired

        # A credential that can no longer be refreshed never recovers on its own —
        # alert on the transition, once.
        if account.disabled_reason:
            state = load_state(account.name)
            if not state.get("disabled_alerted"):
                pushed = True
                if notify:
                    pushed = await send_alert(
                        format_disabled_alert(account.name, account.disabled_reason)
                    )
                else:
                    logger.warning("Quick 账号 {} 凭证失效：{}（未推送）",
                                   account.name, account.disabled_reason)
                if pushed and save:
                    state["disabled_alerted"] = True
                    save_state(state, account.name)
                fired_any = fired_any or (pushed and notify)

    snapshot = pool.snapshot()
    pool_report: JsonDict = dict(snapshot)
    pool_report["reports"] = reports
    pool_report["threshold"] = threshold
    pool_report["available"] = any(r.get("available") for r in reports)

    full = load_all_state()
    pool_state = full.get(POOL_STATE_KEY) if isinstance(full.get(POOL_STATE_KEY), dict) else {}
    armed = pool_state.get("armed") is not False
    exhausted = int(snapshot.get("ready") or 0) == 0
    if exhausted and armed:
        pushed = False
        if notify:
            pushed = await send_alert(format_pool_alert(snapshot))
        else:
            logger.error("Quick 账号池已全部不可用（未推送告警）。")
        if pushed:
            armed = False
            fired_any = True
    elif not exhausted:
        armed = True
    pool_report["pool_armed"] = armed
    pool_report["pool_exhausted"] = exhausted
    if save:
        full[POOL_STATE_KEY] = {"armed": armed, "updated_at": time.time()}
        _write_state(full)
    return pool_report, fired_any


async def watch_loop() -> None:
    """Background task: check the session allowance forever and alert when it runs low.

    Runs on :data:`QUICK_SESSION_WATCH_INTERVAL` (0 disables it). While the gateway
    is serving traffic this costs nothing — the reading comes from responses it
    already streams; only an idle interval spends one 1-token request. Failures are
    logged and retried; the task never crashes the app, and cancellation on shutdown
    is clean.
    """
    if QUICK_SESSION_WATCH_INTERVAL <= 0:
        logger.info("Quick session-usage watch disabled (QUICK_SESSION_WATCH_INTERVAL=0).")
        return
    logger.info(
        "Quick session-usage watch started (interval: {}s, alert below {}% remaining, "
        "alerts: {}).",
        QUICK_SESSION_WATCH_INTERVAL,
        _pct(QUICK_SESSION_ALERT_REMAINING_PCT),
        "on" if QUICK_ALERT_WEBHOOK else "log-only",
    )
    while True:
        try:
            report, fired = await watch_pool_once(notify=bool(QUICK_ALERT_WEBHOOK))
            for entry in report.get("reports", [report]):
                if isinstance(entry, dict) and entry.get("available"):
                    logger.info("Quick session usage[{}]: {}% left ({}% used).",
                                entry.get("account", "default"),
                                _num_pct(entry, "session_remaining_pct"),
                                _num_pct(entry, "session_used_pct"))
            if report.get("ready") is not None:
                logger.info("Quick pool: {}/{} accounts ready.",
                            report.get("ready"), report.get("total"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the watch must never crash the app
            logger.warning("Quick session-usage watch cycle failed ({}); retry in {}s.",
                           exc, QUICK_SESSION_WATCH_INTERVAL)
        await asyncio.sleep(QUICK_SESSION_WATCH_INTERVAL)


# ==================================================================================================
# CLI
# ==================================================================================================

def _num_pct(report: JsonDict, key: str) -> str:
    """Render one percentage field of a report dict."""
    return _pct(_num(report.get(key)))


def _log_pool(snapshot: JsonDict) -> None:
    """Render the pool table as human-readable log lines."""
    logger.info("Quick pool: {}/{} accounts ready (avg {}% session left).",
                snapshot.get("ready"), snapshot.get("total"),
                _num_pct(snapshot, "pool_remaining_pct"))
    for entry in snapshot.get("accounts", []):
        if not isinstance(entry, dict):
            continue
        detail = entry.get("disabled_reason") or entry.get("cooldown_reason") or ""
        logger.info("  {:<10} {:<9} session {}% left, monthly {}% used{}",
                    entry.get("name"), entry.get("status"),
                    _num_pct(entry, "session_remaining_pct"),
                    _num_pct(entry, "monthly_used_pct"),
                    f" — {detail}" if detail else "")


def _log_report(report: JsonDict) -> None:
    """Render a watch report as human-readable log lines."""
    if not report.get("available"):
        logger.error("Session usage unavailable for account '{}' — no response seen yet "
                     "and the probe failed.", report.get("account", "default"))
        return
    line = (f"[{report.get('account', 'default')}] "
            f"Session: {_num_pct(report, 'session_remaining_pct')}% left "
            f"({_num_pct(report, 'session_used_pct')}% used), "
            f"monthly {_num_pct(report, 'monthly_used_pct')}% used")
    resets_at = _num(report.get("monthly_resets_at"))
    reset = _reset_time(int(resets_at) if resets_at else None)
    if reset:
        line += f" (resets {reset})"
    line += f", reading {report.get('age_seconds')}s old"
    if report.get("below_threshold"):
        logger.warning("{} — BELOW the {}% threshold (alert {}).", line,
                       _num_pct(report, "threshold"),
                       "pushed" if report.get("alert_pushed") else "not pushed")
    else:
        logger.info("{} — above the {}% threshold.", line, _num_pct(report, "threshold"))


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 = above threshold, 10 = below it, 1 = unreadable.
    """
    parser = argparse.ArgumentParser(
        description="Watch the Amazon Quick account's session allowance."
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--no-save", action="store_true", help="do not update the state file")
    parser.add_argument("--notify", action="store_true",
                        help="push an alert to QUICK_ALERT_WEBHOOK when below the threshold")
    parser.add_argument("--probe", action="store_true",
                        help="force a fresh 1-token probe instead of any cached reading")
    parser.add_argument("--account", default="",
                        help="check only this pool account (default: every account)")
    parser.add_argument("--pool", action="store_true",
                        help="print the pool table (accounts, remaining, state) and exit")
    parser.add_argument("--threshold", type=float, default=QUICK_SESSION_ALERT_REMAINING_PCT,
                        help="remaining-percentage threshold to alert below "
                             f"(default {QUICK_SESSION_ALERT_REMAINING_PCT})")
    args = parser.parse_args(argv)

    from quick.pool import pool

    if args.pool:
        snapshot = pool.snapshot()
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        else:
            _log_pool(snapshot)
        return 0 if snapshot.get("ready") else 10

    if args.probe:
        asyncio.run(probe_usage(account=pool.get(args.account) if args.account else None))

    if args.account:
        report, _ = asyncio.run(watch_once(notify=args.notify, save=not args.no_save,
                                           threshold=args.threshold, account=args.account))
    else:
        report, _ = asyncio.run(watch_pool_once(notify=args.notify, save=not args.no_save,
                                                threshold=args.threshold))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif "reports" in report:
        for entry in report["reports"]:
            _log_report(entry)
        _log_pool(report)
    else:
        _log_report(report)
    if not report.get("available"):
        return 1
    if "reports" in report:
        below = [r for r in report["reports"] if r.get("below_threshold")]
        return 10 if len(below) == len(report["reports"]) else 0
    return 10 if report.get("below_threshold") else 0


if __name__ == "__main__":
    sys.exit(main())
