# -*- coding: utf-8 -*-

"""Detect new Amazon Quick models without hammering the inference backend.

Amazon Quick ships its **model registry in a public, unauthenticated CloudFront
config** that the desktop app polls every ~60 s:

    https://d2tws6r933zatt.cloudfront.net/quickwork/prod/feature_flag_config.json

It carries the mode → model mapping (``fast`` / ``balanced`` / ``smart``, each
with a ``.thinking`` variant) for the base config plus every staged-rollout
override (per region, per percentage bucket). A new Claude model shows up here —
usually as a percentage canary in a subset of regions — *before* it is worth
sending a single inference request.

So detection is two-stage:

1. **Watch** (zero risk): conditional ``GET`` of that JSON (ETag / If-None-Match).
   It is a CDN object, needs no credentials, never touches the tenant DataPlane,
   and one poll per hour is 1/60 of what the desktop app itself does. Changes are
   logged and (optionally) pushed to a chat webhook.
2. **Probe** (one request, only when stage 1 reports something new): a single
   1-token ``ConverseStream`` against the new id to see whether *this account* is
   authorized, classifying the answer as available / denied / unknown-model.

Usage::

    python -m quick.model_watch                 # diff against the saved state
    python -m quick.model_watch --json          # machine-readable report
    python -m quick.model_watch --notify        # also push changes to the webhook
    python -m quick.model_watch --test-notify   # verify the webhook works
    python -m quick.model_watch --probe us.anthropic.claude-opus-5

Exit codes: ``0`` no change, ``10`` change detected, ``1`` fetch/push failed.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from loguru import logger

from quick.config import QUICK_FORCE_MODEL, QUICK_MODELS, QUICKWORK_HOME

# Mirrors kiro/config.py: settings come from .env. Needed here too because the CLI
# (`python -m quick.model_watch`) runs without importing main.py.
load_dotenv()

JsonDict = Dict[str, object]

# Public CloudFront config the desktop app polls (recovered from the app's own
# ``aws_quick_work.qw_core.remote_config.service`` log lines).
QUICK_REMOTE_CONFIG_URL: str = os.getenv(
    "QUICK_REMOTE_CONFIG_URL",
    "https://d2tws6r933zatt.cloudfront.net/quickwork/prod/feature_flag_config.json",
)

# Where the last-seen mapping + ETag are kept, so a run only reports *changes*.
QUICK_MODEL_WATCH_STATE: Path = Path(
    os.getenv("QUICK_MODEL_WATCH_STATE", str(QUICKWORK_HOME / "model-watch-state.json"))
).expanduser()

# Background-task interval when the watch runs inside the gateway (0 disables it).
QUICK_MODEL_WATCH_INTERVAL: int = int(os.getenv("QUICK_MODEL_WATCH_INTERVAL", "3600"))

# Chat webhook for alerts (Yunzhijia robot: POST {"content": "…"}). Empty = log only.
# Keep the token in .env — never commit it.
QUICK_ALERT_WEBHOOK: str = os.getenv("QUICK_ALERT_WEBHOOK", "")

# By default only an upgrade (a newer model at least as strong as the baseline) is
# pushed; mode remaps / weaker-family additions stay in the log. Set to "1" to push
# every change instead.
QUICK_ALERT_ALL_CHANGES: bool = os.getenv("QUICK_ALERT_ALL_CHANGES", "").lower() in ("1", "true", "yes")

# "Better than this" is what earns an upgrade alert; defaults to the forced model.
QUICK_UPGRADE_BASELINE: str = (
    os.getenv("QUICK_UPGRADE_BASELINE", "")
    or QUICK_FORCE_MODEL
    or "us.anthropic.claude-opus-4-8"
)

PROBE_TIMEOUT_SECONDS: float = 60.0

_FAMILY_RANK: Dict[str, int] = {"haiku": 0, "sonnet": 1, "opus": 2}
_MODEL_RE = re.compile(r"claude-(opus|sonnet|haiku)-(\d+)(?:-(\d+))?")


# ==================================================================================================
# Stage 1 — watch the public config
# ==================================================================================================

async def fetch_config(etag: Optional[str] = None) -> Tuple[Optional[JsonDict], Optional[str]]:
    """Fetch the public Quick remote config, honouring the cached ETag.

    Args:
        etag: ETag from the previous fetch; sent as ``If-None-Match`` so an
            unchanged config costs a 304 with no body.

    Returns:
        ``(config, etag)``. ``config`` is ``None`` when the server answered 304.

    Raises:
        httpx.HTTPError: On a transport failure or non-200/304 status.
    """
    headers = {"Accept": "application/json", "User-Agent": "quick-gateway/1.0"}
    if etag:
        headers["If-None-Match"] = etag
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        resp = await client.get(QUICK_REMOTE_CONFIG_URL, headers=headers)
    if resp.status_code == 304:
        return None, etag
    resp.raise_for_status()
    return resp.json(), resp.headers.get("etag", etag)


def _condition_label(condition: JsonDict) -> str:
    """Render a rule condition (regions / accounts / groups) as a short label."""
    if not condition:
        return "*"
    parts: List[str] = []
    for key, value in condition.items():
        if isinstance(value, list):
            parts.append(f"{key}={','.join(str(v) for v in value)}" if len(value) <= 6
                         else f"{key}=<{len(value)} items>")
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def _collect(models: JsonDict, scope: str, out: Dict[str, str]) -> None:
    """Flatten a ``models`` object into ``{scope/agent/mode: model_id}``."""
    for agent, modes in models.items():
        if not isinstance(modes, dict):
            continue
        for mode, info in modes.items():
            if isinstance(info, dict) and info.get("model_id"):
                out[f"{scope}/{agent}/{mode}"] = str(info["model_id"])


def extract_mapping(config: JsonDict) -> Dict[str, str]:
    """Flatten every mode → model mapping in the config, overrides included.

    Args:
        config: The parsed remote config JSON.

    Returns:
        ``{"<scope>/<agent>/<mode>": "<model id>"}`` covering the base ``models``
        block and every ``config-priority-rule-list`` variant (each rollout
        bucket is its own scope, so a canary is visible as a distinct entry).
    """
    mapping: Dict[str, str] = {}
    base = config.get("models")
    if isinstance(base, dict):
        _collect(base, "base", mapping)
    rules = config.get("config-priority-rule-list")
    if isinstance(rules, list):
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            label = _condition_label(rule.get("condition") or {})
            for j, cfg in enumerate(rule.get("configs") or []):
                if not isinstance(cfg, dict):
                    continue
                value = cfg.get("value")
                if not isinstance(value, dict) or not isinstance(value.get("models"), dict):
                    continue
                pct = cfg.get("percentage")
                scope = f"rule{i}[{label}]/cfg{j}({pct}%)"
                _collect(value["models"], scope, mapping)
    return mapping


def model_rank(model_id: str) -> Optional[Tuple[int, Tuple[int, int]]]:
    """Rank a Claude model id for comparison.

    Args:
        model_id: e.g. ``us.anthropic.claude-opus-4-8``.

    Returns:
        ``(family_rank, (major, minor))`` — e.g. ``(2, (4, 8))`` for opus-4-8 and
        ``(2, (5, 0))`` for opus-5 — or ``None`` for a non-Claude / unparsable id.
    """
    match = _MODEL_RE.search(model_id)
    if not match:
        return None
    family, major, minor = match.group(1), match.group(2), match.group(3)
    # A 4+ digit trailing group is a date stamp (claude-opus-5-20260901-v1:0), not a
    # minor version — reading it as one would rank a dated x.0 above a real x.1.
    if minor and len(minor) >= 4:
        minor = None
    return _FAMILY_RANK[family], (int(major), int(minor or 0))


def is_upgrade(model_id: str, baseline: str = QUICK_UPGRADE_BASELINE) -> bool:
    """Whether ``model_id`` beats ``baseline`` (same-or-better family, newer version).

    ``opus-5`` beats ``opus-4-8``; ``sonnet-5`` does not (weaker family), though it
    is still reported as a new model id.

    Args:
        model_id: Candidate model id.
        baseline: The model to beat (defaults to :data:`QUICK_UPGRADE_BASELINE`).

    Returns:
        ``True`` if the candidate is a strict upgrade.
    """
    cand, base = model_rank(model_id), model_rank(baseline)
    if cand is None or base is None:
        return False
    return cand[0] >= base[0] and cand[1] > base[1]


def load_state() -> JsonDict:
    """Load the saved watch state, or an empty state if there is none."""
    try:
        return json.loads(QUICK_MODEL_WATCH_STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Model-watch state unreadable ({}); starting fresh.", exc)
        return {}


def save_state(state: JsonDict) -> None:
    """Persist the watch state (best effort — a failure only costs a re-report)."""
    try:
        QUICK_MODEL_WATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
        QUICK_MODEL_WATCH_STATE.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Could not write model-watch state: {}", exc)


def diff_mapping(previous: Dict[str, str], current: Dict[str, str]) -> JsonDict:
    """Compare two flattened mappings.

    Args:
        previous: Mapping from the last run (empty on first run).
        current: Mapping just fetched.

    Returns:
        A report dict with ``new_models`` (model ids never seen before, the
        headline signal), ``upgrades`` (those that beat the baseline),
        ``changed`` / ``added`` / ``removed`` mapping entries, and
        ``unknown_to_gateway`` (ids the config serves that
        :data:`quick.config.QUICK_MODELS` does not list).
    """
    prev_ids = set(previous.values())
    cur_ids = set(current.values())
    new_models = sorted(cur_ids - prev_ids)
    changed = {k: [previous[k], current[k]] for k in previous.keys() & current.keys()
               if previous[k] != current[k]}
    return {
        "new_models": new_models,
        "upgrades": [m for m in new_models if is_upgrade(m)],
        "baseline": QUICK_UPGRADE_BASELINE,
        "gone_models": sorted(prev_ids - cur_ids),
        "changed": changed,
        "added": {k: current[k] for k in current.keys() - previous.keys()},
        "removed": {k: previous[k] for k in previous.keys() - current.keys()},
        "unknown_to_gateway": sorted(cur_ids - set(QUICK_MODELS)),
        "all_models": sorted(cur_ids),
    }


# ==================================================================================================
# Alerting (Yunzhijia robot webhook)
# ==================================================================================================

async def send_alert(content: str, webhook: str = "") -> bool:
    """Push one plain-text message to the chat webhook.

    Args:
        content: Message body.
        webhook: Webhook URL; defaults to :data:`QUICK_ALERT_WEBHOOK`.

    Returns:
        ``True`` on a 2xx response, ``False`` on any failure (including "no
        webhook configured") — alerting must never break the caller.
    """
    url = webhook or QUICK_ALERT_WEBHOOK
    if not url:
        logger.debug("No QUICK_ALERT_WEBHOOK configured; alert not pushed.")
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.post(url, json={"content": content})
        if resp.status_code // 100 == 2:
            return _accepted(resp)
        logger.warning("Alert push returned HTTP {}: {}", resp.status_code, resp.text[:200])
    except httpx.HTTPError as exc:
        logger.warning("Alert push failed: {}", exc)
    return False


_REGION_LIST_RE = re.compile(r"regions=([a-z0-9-]+(?:,[a-z0-9-]+)+)")


def _short_scope(scope: str) -> str:
    """Compress a scope label for chat: ``regions=a,b,c,d`` -> ``regions=a+3``.

    Only the rendering is shortened — the mapping keys kept in the state file stay
    verbatim, so shortening never fabricates a diff.
    """
    return _REGION_LIST_RE.sub(
        lambda m: f"regions={m.group(1).split(',')[0]}+{len(m.group(1).split(',')) - 1}", scope
    )


def _accepted(resp: httpx.Response) -> bool:
    """Whether a 2xx webhook response actually accepted the message.

    Yunzhijia robots answer a rejected message (bad token, rate limit, …) with
    HTTP 200 and ``{"success": false, "errorCode": N, "error": "…"}``, so a bare
    status check would record a lost alert as delivered. Non-JSON bodies (other
    robot flavours answer ``ok``) are taken at face value.

    Args:
        resp: The webhook response.

    Returns:
        ``True`` if the robot accepted the message.
    """
    try:
        body = resp.json()
    except ValueError:
        return True
    if not isinstance(body, dict):
        return True
    code = body.get("errorCode")
    if body.get("success") is False or (code is not None and str(code) not in ("0", "")):
        logger.warning("Alert push rejected by the robot: {}", resp.text[:200])
        return False
    return True


def _where(mapping: Dict[str, str], model_id: str, limit: int = 3) -> List[str]:
    """Scopes in which a model id appears (which region / rollout bucket serves it)."""
    return [_short_scope(k) for k, v in mapping.items() if v == model_id][:limit]


def should_alert(report: JsonDict) -> bool:
    """Whether a change is worth pushing to chat.

    Only an upgrade — a newer model whose family is at least as strong as the
    baseline, i.e. "a new Opus-class version" — earns a push. Mode remaps and
    weaker-family additions (e.g. a new Sonnet while the baseline is Opus) are
    logged and not pushed, unless :data:`QUICK_ALERT_ALL_CHANGES` is set.

    Args:
        report: Output of :func:`diff_mapping`.

    Returns:
        ``True`` if the report should be pushed.
    """
    return bool(report.get("upgrades")) or QUICK_ALERT_ALL_CHANGES


def format_alert(report: JsonDict, mapping: Dict[str, str]) -> str:
    """Render a change report as a short chat message.

    Args:
        report: Output of :func:`diff_mapping`.
        mapping: The current flattened mapping (used to show where an id appears).

    Returns:
        Three lines for an upgrade: what appeared, where, and how to confirm
        access. Falls back to a one-line summary for a non-upgrade change (only
        reachable with :data:`QUICK_ALERT_ALL_CHANGES`).
    """
    upgrades = report.get("upgrades") or []
    if not upgrades:
        news = report.get("new_models") or []
        detail = f"新增 {', '.join(news)}" if news else f"映射变化 {len(report.get('changed') or {})} 条"
        return f"⚠️ Quick 模型配置有变化（非升级）：{detail}"
    lines = [f"🚀 Quick 新模型：{', '.join(upgrades)}（基线 {report.get('baseline')}）"]
    for scope in _where(mapping, upgrades[0], limit=1):
        lines.append(f"位置：{scope}")
    lines.append(f"确认权限：python -m quick.model_watch --probe {upgrades[0]}")
    return "\n".join(lines)


# ==================================================================================================
# Watch cycle
# ==================================================================================================

async def watch_once(save: bool = True, notify: bool = False) -> Tuple[JsonDict, bool]:
    """Run one watch cycle: fetch, diff against saved state, alert, persist.

    Args:
        save: Whether to write the new state back (``False`` for a dry run).
        notify: Whether to push a change to the webhook. When a push is attempted
            and fails, the state is *not* saved, so the next cycle retries the
            alert instead of losing it.

    Returns:
        ``(report, changed)`` — ``changed`` is ``True`` when anything moved.

    Raises:
        httpx.HTTPError: If the config could not be fetched.
    """
    state = load_state()
    etag = state.get("etag") if isinstance(state.get("etag"), str) else None
    config, new_etag = await fetch_config(etag)
    if config is None:
        logger.info("Quick remote config unchanged (304).")
        known = sorted(set((state.get("mapping") or {}).values()))
        return {"unchanged": True, "all_models": known}, False

    current = extract_mapping(config)
    previous = state.get("mapping") if isinstance(state.get("mapping"), dict) else {}
    report = diff_mapping(previous, current)
    # A first run has nothing to compare against — record the baseline quietly
    # instead of reporting every mapping as "new".
    report["first_run"] = not previous
    changed = bool(previous) and bool(
        report["new_models"] or report["gone_models"]
        or report["changed"] or report["added"] or report["removed"]
    )

    pushed = True
    alerting = changed and notify and should_alert(report)
    if alerting:
        pushed = await send_alert(format_alert(report, current))
        if not pushed:
            logger.warning("Change detected but alert push failed; state not saved, "
                           "the next cycle will retry.")
    report["alert_pushed"] = alerting and pushed
    if save and pushed:
        save_state({
            "etag": new_etag,
            "config_version": config.get("config_version"),
            "mapping": current,
        })
    return report, changed


async def watch_loop() -> None:
    """Background task: poll the public config forever and alert on changes.

    Runs on :data:`QUICK_MODEL_WATCH_INTERVAL` (0 disables it). One poll per hour
    is 1/60 of the desktop app's own rate against the same CDN object, and it
    never touches the tenant DataPlane. Failures are logged and retried; the task
    never crashes the app, and cancellation on shutdown is clean.
    """
    if QUICK_MODEL_WATCH_INTERVAL <= 0:
        logger.info("Quick model watch disabled (QUICK_MODEL_WATCH_INTERVAL=0).")
        return
    logger.info(
        "Quick model watch started (interval: {}s, alerts: {}, baseline: {}).",
        QUICK_MODEL_WATCH_INTERVAL,
        "on" if QUICK_ALERT_WEBHOOK else "log-only",
        QUICK_UPGRADE_BASELINE,
    )
    while True:
        try:
            report, changed = await watch_once(notify=bool(QUICK_ALERT_WEBHOOK))
            if changed:
                _log_report(report, changed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the watch must never crash the app
            logger.warning("Quick model watch cycle failed ({}); retry in {}s.",
                           exc, QUICK_MODEL_WATCH_INTERVAL)
        await asyncio.sleep(QUICK_MODEL_WATCH_INTERVAL)


# ==================================================================================================
# Stage 2 — one minimal inference probe (only for an id stage 1 flagged)
# ==================================================================================================

async def probe_model(model_id: str) -> JsonDict:
    """Send one 1-token request to check whether this account may use a model.

    This is the *only* call that touches the tenant DataPlane, so run it once
    per candidate — not on a timer. Quick answers an unauthorized model with
    HTTP 200 plus an in-stream ``error`` frame, so the outcome is classified
    from the frame text rather than the status code.

    Run it on the host that owns the credentials: any auth path here refreshes
    Keycloak and rotates the ``refresh_token``, invalidating another host's copy.

    Args:
        model_id: A Quick Bedrock inference-profile id, e.g.
            ``us.anthropic.claude-opus-5``.

    Returns:
        ``{"model_id": …, "status": "available"|"denied"|"unknown_model"|"error",
        "detail": …}``.
    """
    # Imported here so watching works on a host without Quick credentials.
    from quick.client import QuickAPIError, converse_stream
    from quick.streaming import ConverseAggregator, EventStreamDecoder

    converse_input: JsonDict = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        "inferenceConfig": {"maxTokens": 1},
    }
    aggregator = ConverseAggregator("probe", model_id)
    decoder = EventStreamDecoder()

    async def _drive() -> None:
        async for chunk in converse_stream(converse_input):
            for event in decoder.feed(chunk):
                aggregator.add(event)

    try:
        await asyncio.wait_for(_drive(), PROBE_TIMEOUT_SECONDS)
    except QuickAPIError as exc:
        return {"model_id": model_id, "status": "error",
                "detail": f"HTTP {exc.status_code}: {exc.message[:300]}"}
    except asyncio.TimeoutError:
        return {"model_id": model_id, "status": "error", "detail": "probe timed out"}
    except Exception as exc:  # noqa: BLE001 - probe must never take the caller down
        return {"model_id": model_id, "status": "error", "detail": f"{type(exc).__name__}: {exc}"}

    if aggregator.error:
        low = aggregator.error.lower()
        if "not authorized" in low or "accessdenied" in low or "explicit deny" in low:
            status = "denied"
        elif "validation" in low or "not found" in low or "invalid" in low:
            status = "unknown_model"
        else:
            status = "error"
        return {"model_id": model_id, "status": status, "detail": aggregator.error[:300]}
    return {"model_id": model_id, "status": "available", "detail": "completion returned"}


# ==================================================================================================
# CLI
# ==================================================================================================

def _log_report(report: JsonDict, changed: bool) -> None:
    """Render a watch report as human-readable log lines."""
    if report.get("unchanged"):
        logger.info("No change. Models in play: {}", ", ".join(report.get("all_models") or []))
        return
    if report.get("first_run"):
        logger.info("Baseline recorded. Models in play: {}", ", ".join(report.get("all_models") or []))
        if report.get("unknown_to_gateway"):
            logger.warning("Served by Quick but not in quick/config.py QUICK_MODELS: {}",
                           ", ".join(report["unknown_to_gateway"]))
        return
    if report.get("upgrades"):
        logger.warning("UPGRADE AVAILABLE — Quick now serves {} (baseline {}).",
                       ", ".join(report["upgrades"]), report.get("baseline"))
    if report.get("new_models"):
        logger.warning("NEW MODEL ID(S) in Quick's config: {}", ", ".join(report["new_models"]))
    for key, (old, new) in (report.get("changed") or {}).items():
        logger.warning("mapping changed: {}: {} -> {}", key, old, new)
    for key, val in (report.get("added") or {}).items():
        logger.info("mapping added:   {} = {}", key, val)
    for key, val in (report.get("removed") or {}).items():
        logger.info("mapping removed: {} = {}", key, val)
    if report.get("unknown_to_gateway"):
        logger.warning("Served by Quick but not in quick/config.py QUICK_MODELS: {}",
                       ", ".join(report["unknown_to_gateway"]))
    if not changed:
        logger.info("No change. Models in play: {}", ", ".join(report.get("all_models") or []))


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 = no change, 10 = change detected, 1 = failure.
    """
    parser = argparse.ArgumentParser(description="Watch Amazon Quick's public model registry.")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--no-save", action="store_true", help="do not update the state file")
    parser.add_argument("--notify", action="store_true",
                        help="push changes to QUICK_ALERT_WEBHOOK")
    parser.add_argument("--test-notify", action="store_true",
                        help="send one test message to the webhook and exit")
    parser.add_argument("--probe", metavar="MODEL_ID",
                        help="send ONE 1-token request to test account access to a model id")
    args = parser.parse_args(argv)

    if args.test_notify:
        ok = asyncio.run(send_alert("✅ quick-gateway 模型监控：webhook 连通性测试"))
        logger.info("Test alert {}", "sent." if ok else "FAILED (check QUICK_ALERT_WEBHOOK).")
        return 0 if ok else 1

    if args.probe:
        result = asyncio.run(probe_model(args.probe))
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
              else f"{result['model_id']}: {result['status']} — {result['detail']}")
        return 0 if result["status"] == "available" else 10

    try:
        report, changed = asyncio.run(watch_once(save=not args.no_save, notify=args.notify))
    except httpx.HTTPError as exc:
        logger.error("Quick remote config fetch failed: {}", exc)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _log_report(report, changed)
    return 10 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
