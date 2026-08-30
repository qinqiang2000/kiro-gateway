# -*- coding: utf-8 -*-

"""Who is spending this channel's quota, as litellm accounts for it.

The pool page answers "how much Quick allowance is left"; this answers the other
half — **which virtual key burned it**. litellm is the only place that knows: it
owns the per-key attribution, and quick-gateway sees an undifferentiated stream of
requests (litellm is the auth boundary, so the gateway is not even given a key).

Source: ``GET /user/daily/activity``, the endpoint litellm's own Usage UI calls. It
is the *only* one that gives per-key **and** per-model numbers on a community
licence — ``/global/spend/report`` is enterprise-gated (403 with a licence pitch),
and ``/global/spend/keys`` is per-key but all-models, with no token counts.

Two subtleties, both found by reading real rows:

* the row's model is the **resolved** name (``anthropic/claude-opus-quick``) on a
  successful call but the **requested** one (``claude-opus-quick``) on a failure, so
  both are queried and merged — otherwise a month of 429s during a quota block would
  silently vanish from the ranking;
* ``breakdown.api_keys`` is scoped by whatever filter the query carried, so the
  per-key numbers are only *this* channel's because ``model=`` was passed. Reading
  the same field off an unfiltered query would hand back each key's whole spend
  across every model in the proxy.

The dollar figures are litellm's own accounting at the rates in its config (Quick is
priced at 1/10 of official Opus list), **not** an AWS invoice: the Quick seats are
flat-rate with a unit quota, so this is what the channel *would* have cost, which is
the number worth ranking people by.
"""

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from quick.config import (
    LITELLM_BASE_URL,
    LITELLM_MASTER_KEY,
    LITELLM_QUICK_MODELS,
    LITELLM_USAGE_CACHE_SECONDS,
    LITELLM_USAGE_TIMEOUT,
)

JsonDict = Dict[str, Any]

_ACTIVITY_PATH: str = "/user/daily/activity"
_PAGE_SIZE: int = 1000
_MAX_PAGES: int = 50


@dataclass
class KeySpend:
    """One virtual key's consumption of this channel, for the month so far."""

    key_hash: str
    alias: str = ""
    spend: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0
    successful: int = 0
    failed: int = 0

    def label(self) -> str:
        """Display name: the key alias, else something safe to print.

        litellm's per-key id is normally a hex digest, but its own service accounts
        appear under a literal name (``litellm_proxy_admin``) — worth showing as-is,
        while anything digest-shaped (or a stray raw key) is cut to 8 characters.
        """
        if self.alias:
            return self.alias
        opaque = self.key_hash.startswith("sk-") or (
            len(self.key_hash) >= 12
            and all(c in "0123456789abcdefABCDEF" for c in self.key_hash)
        )
        return f"key {self.key_hash[:8]}" if opaque else self.key_hash

    def to_dict(self) -> JsonDict:
        """Render for the status page. Carries no key material — alias and hash only."""
        return {
            "key": self.key_hash[:8],
            "alias": self.label(),
            "spend": round(self.spend, 6),
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "requests": self.requests,
            "successful": self.successful,
            "failed": self.failed,
        }


@dataclass
class _Cache:
    """Newest good report, so the page's 20 s polling does not re-walk the month."""

    payload: Optional[JsonDict] = None
    fetched_at: float = 0.0

    def fresh(self, ttl: float) -> bool:
        """True while the cached report may still be served as-is."""
        return self.payload is not None and (time.time() - self.fetched_at) < ttl


_cache: _Cache = _Cache()


def quick_model_names() -> List[str]:
    """The litellm model names that count as this channel (config, de-duplicated)."""
    seen: List[str] = []
    for raw in LITELLM_QUICK_MODELS.split(","):
        name = raw.strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def month_window(today: Optional[date] = None) -> Tuple[str, str, str]:
    """The current calendar month so far, in the format litellm expects.

    Quick's own monthly entitlement resets at the UTC month boundary, so the window
    is anchored to UTC rather than the box's local time — the two tabs would
    otherwise disagree for a few hours around the turn of the month.

    Args:
        today: Override for "now" (tests).

    Returns:
        ``(start_date, end_date, month_label)`` as ``YYYY-MM-DD`` / ``YYYY-MM``.
    """
    now = today or datetime.now(timezone.utc).date()
    start = now.replace(day=1)
    return start.isoformat(), now.isoformat(), now.strftime("%Y-%m")


def _merge(into: Dict[str, KeySpend], breakdown: JsonDict) -> None:
    """Fold one day's per-key metrics into the running totals.

    Args:
        into: Accumulator keyed by the key's hash.
        breakdown: A day's ``breakdown`` object from the activity endpoint.
    """
    for key_hash, entry in (breakdown.get("api_keys") or {}).items():
        if not isinstance(entry, dict):
            continue
        metrics = entry.get("metrics") or {}
        meta = entry.get("metadata") or {}
        row = into.setdefault(key_hash, KeySpend(key_hash=key_hash))
        row.alias = row.alias or (meta.get("key_alias") or "")
        row.spend += float(metrics.get("spend") or 0.0)
        row.total_tokens += int(metrics.get("total_tokens") or 0)
        row.prompt_tokens += int(metrics.get("prompt_tokens") or 0)
        row.completion_tokens += int(metrics.get("completion_tokens") or 0)
        row.cache_read_tokens += int(metrics.get("cache_read_input_tokens") or 0)
        row.cache_write_tokens += int(metrics.get("cache_creation_input_tokens") or 0)
        row.requests += int(metrics.get("api_requests") or 0)
        row.successful += int(metrics.get("successful_requests") or 0)
        row.failed += int(metrics.get("failed_requests") or 0)


async def _fetch_model(
    client: httpx.AsyncClient, model: str, start: str, end: str, into: Dict[str, KeySpend]
) -> None:
    """Walk every page of one model's daily activity into ``into``.

    Args:
        client: Client carrying the master-key header.
        model: litellm model name to filter on.
        start: Window start (``YYYY-MM-DD``).
        end: Window end (``YYYY-MM-DD``).
        into: Accumulator shared across models.

    Raises:
        httpx.HTTPError: On transport failure or a non-2xx status.
    """
    page = 1
    while page <= _MAX_PAGES:
        resp = await client.get(
            f"{LITELLM_BASE_URL}{_ACTIVITY_PATH}",
            params={"start_date": start, "end_date": end, "model": model,
                    "page": page, "page_size": _PAGE_SIZE},
        )
        resp.raise_for_status()
        body = resp.json()
        for day in body.get("results") or []:
            _merge(into, day.get("breakdown") or {})
        meta = body.get("metadata") or {}
        total_pages = int(meta.get("total_pages") or 1)
        if not meta.get("has_more") and page >= total_pages:
            return
        page += 1
    logger.warning("litellm usage: stopped at {} pages for model {}.", _MAX_PAGES, model)


async def fetch_monthly_spend(today: Optional[date] = None) -> JsonDict:
    """Rank this channel's virtual keys by spend for the month so far.

    Args:
        today: Override for "now" (tests).

    Returns:
        A JSON-friendly report. ``error`` is non-empty when the numbers could not be
        fetched; the other fields are then empty rather than absent, so the page can
        render the same shape either way.

    Raises:
        httpx.HTTPError: Propagated to :func:`monthly_spend`, which turns it into an
            ``error`` field instead of an exception.
    """
    start, end, month = month_window(today)
    report: JsonDict = {
        "month": month, "start": start, "end": end,
        "models": quick_model_names(), "keys": [],
        "totals": {"spend": 0.0, "total_tokens": 0, "prompt_tokens": 0,
                   "completion_tokens": 0, "cache_read_tokens": 0,
                   "requests": 0, "successful": 0, "failed": 0},
        "generated_at": time.time(), "error": "",
    }
    if not LITELLM_MASTER_KEY:
        report["error"] = "LITELLM_MASTER_KEY 未配置"
        return report

    rows: Dict[str, KeySpend] = {}
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    timeout = httpx.Timeout(LITELLM_USAGE_TIMEOUT, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for model in quick_model_names():
            await _fetch_model(client, model, start, end, rows)

    ranked = sorted(rows.values(), key=lambda r: (-r.spend, -r.total_tokens, r.label()))
    report["keys"] = [row.to_dict() for row in ranked]
    totals = report["totals"]
    for row in ranked:
        totals["spend"] += row.spend
        totals["total_tokens"] += row.total_tokens
        totals["prompt_tokens"] += row.prompt_tokens
        totals["completion_tokens"] += row.completion_tokens
        totals["cache_read_tokens"] += row.cache_read_tokens
        totals["requests"] += row.requests
        totals["successful"] += row.successful
        totals["failed"] += row.failed
    totals["spend"] = round(totals["spend"], 6)
    return report


async def monthly_spend(force: bool = False) -> JsonDict:
    """Cached :func:`fetch_monthly_spend`, which never raises at the page.

    A failed refresh serves the last good report with ``error`` set and ``stale``
    true — a stale ranking with a visible warning beats an empty tab, and the page
    polls often enough that a blip would otherwise blank it.

    Args:
        force: Ignore the cache and re-query litellm.

    Returns:
        The report dict (see :func:`fetch_monthly_spend`).
    """
    if not force and _cache.fresh(LITELLM_USAGE_CACHE_SECONDS):
        payload = dict(_cache.payload or {})
        payload["cache_age_seconds"] = round(time.time() - _cache.fetched_at, 1)
        return payload
    try:
        report = await fetch_monthly_spend()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("litellm usage: refresh failed ({}).", exc)
        if _cache.payload is not None:
            stale = dict(_cache.payload)
            stale["error"] = f"刷新失败：{exc}"
            stale["stale"] = True
            stale["cache_age_seconds"] = round(time.time() - _cache.fetched_at, 1)
            return stale
        start, end, month = month_window()
        return {"month": month, "start": start, "end": end, "models": quick_model_names(),
                "keys": [], "totals": {}, "generated_at": time.time(),
                "error": f"读取 litellm 失败：{exc}"}
    if not report.get("error"):
        _cache.payload = report
        _cache.fetched_at = time.time()
    report["cache_age_seconds"] = 0.0
    return report


def reset_cache() -> None:
    """Drop the cached report (tests, and after a config change)."""
    _cache.payload = None
    _cache.fetched_at = 0.0


if __name__ == "__main__":  # pragma: no cover - manual run
    import asyncio
    import json

    print(json.dumps(asyncio.run(monthly_spend(force=True)), ensure_ascii=False, indent=2))
