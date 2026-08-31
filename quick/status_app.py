# -*- coding: utf-8 -*-

"""Public, read-only status page for the Quick account pool.

This is a **separate ASGI app on a separate port** from the gateway, and that
separation is the point: publishing the gateway's own port would expose
``/quick/v1/messages`` — i.e. hand the account pool's inference quota to the
internet. This app serves two GET routes and nothing else.

Nothing it renders is a credential: no tokens, no tenant URL, no user ARN — only the
labels derived from credential *filenames*, the quota numbers Quick reports, and (on
the spend tab) litellm key **aliases**, never key material.

Those labels do identify people, though, once the credential files are named after
their owners and the aliases are colleagues' names. So this deployment sets
:data:`quick.config.QUICK_STATUS_TOKEN`: the port is published to the internet, and
the path-based 404 is obscurity, not a boundary. Name the files neutrally (``b``,
``c``) instead if the page must stay open.

The page lives under :data:`quick.config.QUICK_STATUS_PATH` (default ``/quick``) and
everything else 404s, so a scanner sweeping ``/`` finds nothing. That is obscurity,
not a boundary — set :data:`quick.config.QUICK_STATUS_TOKEN` to require
``?t=<token>`` (or an ``X-Status-Token`` header) if the page should be private.
"""

from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from loguru import logger

from quick import config as quick_config
from quick.config import (
    QUICK_STATUS_HOST,
    QUICK_STATUS_PATH,
    QUICK_STATUS_PORT,
    QUICK_STATUS_TOKEN,
)
from quick.litellm_usage import monthly_spend
from quick.pool import pool

PAGE = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Quick 账号池</title>
<style>
  :root {
    --bg:#f6f7f9; --card:#fff; --fg:#14171a; --muted:#6b7280; --line:#e5e7eb;
    --ok:#16a34a; --warn:#d97706; --bad:#dc2626; --track:#e5e7eb; --shadow:0 1px 2px rgba(0,0,0,.06);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#0f1115; --card:#171a21; --fg:#e8eaed; --muted:#9aa1ac; --line:#262b35;
      --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444; --track:#262b35; --shadow:none;
    }
  }
  * { box-sizing:border-box; }
  body {
    margin:0; padding:28px 20px 48px; background:var(--bg); color:var(--fg);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",
         "Microsoft YaHei",sans-serif;
  }
  .wrap { max-width:860px; margin:0 auto; }
  h1 { font-size:19px; margin:0 0 2px; letter-spacing:.2px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:22px; }
  .sum { display:flex; gap:26px; flex-wrap:wrap; padding:16px 18px; background:var(--card);
         border:1px solid var(--line); border-radius:12px; box-shadow:var(--shadow); margin-bottom:16px; }
  .sum div { min-width:96px; }
  .sum b { display:block; font-size:24px; font-variant-numeric:tabular-nums; font-weight:650; }
  .sum span { color:var(--muted); font-size:12px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:16px 18px; margin-bottom:12px; box-shadow:var(--shadow); }
  .row { display:flex; align-items:baseline; justify-content:space-between; gap:12px; }
  .name { font-weight:650; font-size:15px; }
  .pill { font-size:11px; padding:2px 9px; border-radius:999px; border:1px solid currentColor;
          font-weight:600; letter-spacing:.3px; }
  .ready { color:var(--ok); } .cooling { color:var(--warn); } .disabled { color:var(--bad); }
  .big { font-size:26px; font-weight:650; font-variant-numeric:tabular-nums; }
  .big small { font-size:12px; font-weight:500; color:var(--muted); margin-left:4px; }
  .bar { height:7px; border-radius:999px; background:var(--track); overflow:hidden; margin:10px 0 8px; }
  .bar i { display:block; height:100%; border-radius:999px; transition:width .4s ease; }
  .meta { color:var(--muted); font-size:12.5px; display:flex; gap:16px; flex-wrap:wrap; }
  .note { margin-top:8px; font-size:12.5px; color:var(--warn); }
  .note.bad { color:var(--bad); }
  footer { color:var(--muted); font-size:12px; margin-top:22px; text-align:center; }
  .dim { opacity:.55; }
  .tabs { display:flex; gap:6px; margin-bottom:16px; }
  .tabs button { font:inherit; font-size:13.5px; font-weight:600; color:var(--muted);
                 background:transparent; border:1px solid var(--line); border-radius:999px;
                 padding:6px 15px; cursor:pointer; transition:all .15s ease; }
  .tabs button:hover { color:var(--fg); }
  .tabs button[aria-selected="true"] { background:var(--card); color:var(--fg);
                                       box-shadow:var(--shadow); }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th, td { text-align:right; padding:9px 8px; border-bottom:1px solid var(--line); }
  th:first-child, td:first-child { text-align:left; }
  th { color:var(--muted); font-size:12px; font-weight:600; white-space:nowrap; }
  tbody tr:last-child td { border-bottom:none; }
  td.num { font-variant-numeric:tabular-nums; }
  td.who { font-weight:600; }
  .rank { display:inline-block; min-width:22px; color:var(--muted); font-weight:600;
          font-variant-numeric:tabular-nums; }
  .scroll { overflow-x:auto; }
  .hint { color:var(--muted); font-size:12px; margin-top:10px; line-height:1.6; }
  .bar.mini { height:5px; max-width:200px; margin:7px 0 1px; }
  .split { font-weight:500; color:var(--muted); font-size:12px; margin-top:3px; }
  .cap { color:var(--muted); font-size:12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Quick 账号池</h1>
  <div class="sub" id="sub">各账号剩余额度 · 每 20 秒自动刷新</div>
  <div class="tabs" role="tablist">
    <button id="tab-pool" role="tab" aria-selected="true" onclick="show('pool')">账号额度</button>
    <button id="tab-spend" role="tab" aria-selected="false" onclick="show('spend')">本月花费排名</button>
  </div>

  <div id="pane-pool">
    <div class="sum">
      <div><b id="s-ready">–</b><span>可用账号</span></div>
      <div><b id="s-avg">–</b><span>平均剩余额度（取更紧的）</span></div>
      <div><b id="s-inflight">–</b><span>进行中请求</span></div>
    </div>
    <div id="list"></div>
  </div>

  <div id="pane-spend" hidden>
    <div class="sum">
      <div><b id="p-spend">–</b><span id="p-month">本月花费</span></div>
      <div><b id="p-tokens">–</b><span>tokens</span></div>
      <div><b id="p-reqs">–</b><span>请求数</span></div>
    </div>
    <div class="card">
      <div class="row"><span class="name">按模型</span>
        <span class="cap">同一个账号池、同一份配额，只是模型不同</span></div>
      <div class="scroll"><table>
        <thead><tr><th>通道</th><th>花费</th><th>占比</th><th>tokens</th><th>缓存读取</th><th>请求</th></tr></thead>
        <tbody id="chan-rows"><tr><td colspan="6" class="dim">加载中…</td></tr></tbody>
      </table></div>
    </div>
    <div class="card">
      <div class="row"><span class="name">按虚拟 key</span>
        <span class="cap">用了不止一个模型的 key，下面一行是它的构成</span></div>
      <div class="scroll"><table>
        <thead><tr><th>虚拟 key</th><th>花费</th><th>tokens</th><th>缓存读取</th><th>请求</th></tr></thead>
        <tbody id="spend-rows"><tr><td colspan="5" class="dim">加载中…</td></tr></tbody>
      </table></div>
      <div class="hint" id="spend-hint"></div>
    </div>
  </div>

  <footer id="foot">加载中…</footer>
</div>
<script>
const pct = v => (v === null || v === undefined) ? "–" : Math.round(v) + "%";
const color = v => (v === null || v === undefined) ? "var(--muted)"
                 : v >= 40 ? "var(--ok)" : v >= 10 ? "var(--warn)" : "var(--bad)";
const ago = s => {
  if (s === null || s === undefined) return "从未使用";
  if (s < 90) return Math.round(s) + " 秒前";
  if (s < 5400) return Math.round(s / 60) + " 分钟前";
  return Math.round(s / 3600) + " 小时前";
};
const left = s => s >= 3600 ? Math.round(s / 3600) + " 小时" : Math.max(1, Math.round(s / 60)) + " 分钟";
const label = {ready: "可用", cooling: "冷却中", disabled: "已停用"};

function render(d) {
  document.getElementById("s-ready").textContent = d.ready + " / " + d.total;
  document.getElementById("s-avg").textContent = pct(d.pool_remaining_pct);
  document.getElementById("s-inflight").textContent =
    d.accounts.reduce((n, a) => n + a.inflight, 0);

  document.getElementById("list").innerHTML = d.accounts.map(a => {
    const v = a.session_remaining_pct;
    const note = a.disabled_reason
      ? '<div class="note bad">凭证失效：' + esc(a.disabled_reason) + "</div>"
      : (a.cooldown_seconds_left
          ? '<div class="note">冷却中，约 ' + left(a.cooldown_seconds_left) + "后恢复"
            + (a.cooldown_reason ? "（" + esc(a.cooldown_reason) + "）" : "") + "</div>"
          : "");
    // overageEnabled is a standing setting, not a state — only say "超额" past 100%.
    const units = (a.monthly_available_units === null || a.monthly_available_units === undefined
                   || !a.monthly_provisioned_units)
      ? "" : " · 剩 " + Math.round(a.monthly_available_units) + "/"
             + Math.round(a.monthly_provisioned_units) + " 单位";
    const over = (a.monthly_used_pct >= 100)
      ? (a.overage_enabled ? "，已超额（overage 兜底，仍可用）" : "，已用尽") : "";
    const monthly = a.monthly_used_pct === null || a.monthly_used_pct === undefined
      ? "" : "月度已用 " + pct(a.monthly_used_pct) + units + over;
    // Which number the pool actually ranks this account on, so a card showing "100%
    // 会话额度剩余" while sitting at the back of the queue explains itself.
    const rank = a.headroom_pct === null || a.headroom_pct === undefined ? ""
      : "排序依据 " + pct(a.headroom_pct)
        + (a.binding_allowance === "monthly" ? "（月度更紧）" : "（会话更紧）");
    return '<div class="card' + (a.status === "disabled" ? " dim" : "") + '">'
      + '<div class="row"><span class="name">' + esc(a.name) + "</span>"
      + '<span class="pill ' + a.status + '">' + (label[a.status] || a.status) + "</span></div>"
      + '<div class="big" style="color:' + color(v) + '">' + pct(v)
      + "<small>会话额度剩余</small></div>"
      + '<div class="bar"><i style="width:' + (v === null || v === undefined ? 0 : Math.max(0, Math.min(100, v)))
      + '%;background:' + color(v) + '"></i></div>'
      + '<div class="meta"><span>' + monthly + "</span>"
      + (rank ? "<span>" + rank + "</span>" : "")
      + "<span>最后使用 " + ago(a.last_used_ago_seconds) + "</span>"
      + "<span>已服务 " + a.served + " 次</span>"
      + (a.inflight ? "<span>进行中 " + a.inflight + "</span>" : "")
      + "</div>" + note + "</div>";
  }).join("") || '<div class="card">账号池为空：没有找到任何凭证文件。</div>';

  document.getElementById("foot").textContent =
    "更新于 " + new Date().toLocaleTimeString("zh-CN");
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

const money = v => "$" + (v >= 1 ? v.toFixed(2) : v >= 0.01 ? v.toFixed(3) : v.toFixed(5));
const big = n => {
  if (n === null || n === undefined) return "–";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
};

function renderSpend(d) {
  const t = d.totals || {};
  document.getElementById("p-spend").textContent = money(t.spend || 0);
  document.getElementById("p-month").textContent = (d.month || "") + " 花费";
  document.getElementById("p-tokens").textContent = big(t.total_tokens || 0);
  document.getElementById("p-reqs").textContent = big(t.requests || 0);

  // Per model. The share is of spend, which is what the tiles above are counting;
  // tokens sit next to it because a Quick unit is charged by tokens, not by model.
  const chans = d.channels || [];
  document.getElementById("chan-rows").innerHTML = chans.length
    ? chans.map(c => {
        const share = t.spend ? 100 * c.spend / t.spend : 0;
        return "<tr><td class='who'>" + esc(c.channel)
          + "<div class='bar mini'><i style='width:" + share.toFixed(1)
          + "%;background:var(--ok)'></i></div></td>"
          + "<td class='num'>" + money(c.spend) + "</td>"
          + "<td class='num dim'>" + share.toFixed(0) + "%</td>"
          + "<td class='num'>" + big(c.total_tokens) + "</td>"
          + "<td class='num dim'>" + big(c.cache_read_tokens) + "</td>"
          + "<td class='num'>" + big(c.requests)
          + (c.failed ? " <span style='color:var(--bad)'>(" + c.failed + " 失败)</span>" : "")
          + "</td></tr>";
      }).join("")
    : "<tr><td colspan='6' class='dim'>本月还没有任何通道的调用记录。</td></tr>";

  // A key's split is only worth a line when it actually used more than one model.
  const short = Object.fromEntries(chans.map(c => [c.channel, c.label || c.channel]));
  const split = k => {
    const used = Object.entries(k.channels || {}).filter(c => c[1].requests || c[1].spend);
    return used.length < 2 ? ""
      : "<div class='split'>" + used.sort((a, b) => b[1].spend - a[1].spend)
          .map(c => esc(short[c[0]] || c[0]) + " " + money(c[1].spend)).join(" · ") + "</div>";
  };

  const rows = d.keys || [];
  document.getElementById("spend-rows").innerHTML = rows.length
    ? rows.map((k, i) =>
        "<tr><td class='who'><span class='rank'>" + (i + 1) + "</span>" + esc(k.alias)
        + split(k) + "</td>"
        + "<td class='num'>" + money(k.spend) + "</td>"
        + "<td class='num'>" + big(k.total_tokens) + "</td>"
        + "<td class='num dim'>" + big(k.cache_read_tokens) + "</td>"
        + "<td class='num'>" + big(k.requests)
        + (k.failed ? " <span style='color:var(--bad)'>(" + k.failed + " 失败)</span>" : "")
        + "</td></tr>").join("")
    : "<tr><td colspan='5' class='dim'>本月还没有这个网关的调用记录。</td></tr>";

  const note = d.error
    ? "<span style='color:var(--bad)'>" + esc(d.error) + "</span><br>"
    : "";
  document.getElementById("spend-hint").innerHTML = note
    + "统计口径：" + esc(chans.map(c => c.channel).join(" / ") || (d.models || []).join(" / "))
    + "（成功记解析后的名字、失败记请求时的名字，已合并），"
    + esc(d.start || "") + " ~ " + esc(d.end || "") + "（UTC）。"
    + "金额是 litellm 按配置单价的记账（每条通道 = 各自官方 list 的 1/10），"
    + "不是 AWS 账单——Quick 席位是包月配额制，而且 unit 只按 token 算、不分模型，"
    + "所以美元占比不等于配额占比。"
    + (d.cache_age_seconds ? "缓存 " + Math.round(d.cache_age_seconds) + " 秒前。" : "");
}

let active = "pool", spendLoaded = false;

function show(which) {
  active = which;
  document.getElementById("pane-pool").hidden = which !== "pool";
  document.getElementById("pane-spend").hidden = which !== "spend";
  document.getElementById("tab-pool").setAttribute("aria-selected", which === "pool");
  document.getElementById("tab-spend").setAttribute("aria-selected", which === "spend");
  document.getElementById("sub").textContent = which === "pool"
    ? "各账号剩余额度 · 每 20 秒自动刷新"
    : "本通道各虚拟 key 的当月消耗排名 · 每 5 分钟刷新一次";
  if (which === "spend" && !spendLoaded) { spendLoaded = true; tickSpend(); }
}

function apiBase() {
  let base = location.pathname;                   // works under any status path
  while (base.endsWith("/")) base = base.slice(0, -1);
  return base;
}

async function tick() {
  try {
    const r = await fetch(apiBase() + "/api/pool" + location.search, {cache: "no-store"});
    if (!r.ok) throw new Error(r.status);
    render(await r.json());
  } catch (e) {
    document.getElementById("foot").textContent = "刷新失败（" + e.message + "），重试中…";
  }
}

async function tickSpend() {
  try {
    const r = await fetch(apiBase() + "/api/litellm" + location.search, {cache: "no-store"});
    if (!r.ok) throw new Error(r.status);
    renderSpend(await r.json());
  } catch (e) {
    document.getElementById("spend-hint").textContent = "读取花费失败（" + e.message + "）。";
  }
}

tick();
setInterval(tick, 20000);
// The spend query walks a month of rows server-side; it is cached there, and the tab
// only refreshes while it is the one being looked at.
setInterval(() => { if (active === "spend") tickSpend(); }, 300000);
</script>
</body>
</html>
"""


def _authorized(request: Request) -> bool:
    """True when the request may see the page (always, unless a token is configured)."""
    if not QUICK_STATUS_TOKEN:
        return True
    supplied = request.query_params.get("t") or request.headers.get("x-status-token", "")
    return supplied == QUICK_STATUS_TOKEN


def status_prefix(raw: Optional[str] = None) -> str:
    """Normalize the configured status path to ``/thing`` (or ``""`` for the root)."""
    value = (QUICK_STATUS_PATH if raw is None else raw).strip().strip("/")
    return f"/{value}" if value else ""


def create_status_app(path: Optional[str] = None) -> FastAPI:
    """Build the status-only ASGI app (no inference routes, by construction).

    Args:
        path: Serve under this path instead of :data:`quick.config.QUICK_STATUS_PATH`.
    """
    app = FastAPI(title="Quick Pool Status", docs_url=None, redoc_url=None, openapi_url=None)
    prefix = status_prefix(path)

    async def _index(request: Request) -> Any:
        """The page itself."""
        if not _authorized(request):
            return HTMLResponse("Not Found", status_code=404)
        return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})

    async def _api_pool(request: Request) -> Any:
        """Per-account quota, as JSON. Carries no credential material."""
        if not _authorized(request):
            return JSONResponse({"error": "not found"}, status_code=404)
        pool.discover()
        return JSONResponse(pool.snapshot(), headers={"Cache-Control": "no-store"})

    async def _api_litellm(request: Request) -> Any:
        """This channel's per-key spend for the month, as litellm accounts for it.

        Key *aliases* only — the report never carries a key, and the query never
        leaves the docker network.
        """
        if not _authorized(request):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(await monthly_spend(), headers={"Cache-Control": "no-store"})

    # Both with and without the trailing slash; the page's fetch() derives the API
    # URL from its own pathname, so either entry point works.
    app.add_api_route(prefix or "/", _index, methods=["GET"], response_class=HTMLResponse)
    if prefix:
        app.add_api_route(f"{prefix}/", _index, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route(f"{prefix}/api/pool", _api_pool, methods=["GET"])
    app.add_api_route(f"{prefix}/api/litellm", _api_litellm, methods=["GET"])

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: Any) -> Any:
        """Answer every other path with a bare 404 — nothing to fingerprint."""
        return PlainTextResponse("Not Found", status_code=404)

    return app


async def serve_status_page(
    host: str = "", port: int = 0, stop_on_error: bool = False
) -> Optional[None]:
    """Run the status page server until cancelled.

    Args:
        host: Bind address; defaults to :data:`quick.config.QUICK_STATUS_HOST`.
        port: Bind port; defaults to :data:`quick.config.QUICK_STATUS_PORT`. ``0``
            disables the server.
        stop_on_error: Re-raise a bind failure instead of logging it (the gateway
            must keep serving inference even if the page cannot bind its port).
    """
    import asyncio

    import uvicorn

    # Read the port from the module (not the import-time constant) so a test or an
    # embedder can switch the server off without re-importing.
    bind_port = port or quick_config.QUICK_STATUS_PORT
    if bind_port <= 0:
        logger.info("Quick status page disabled (QUICK_STATUS_PORT=0).")
        return None
    config = uvicorn.Config(
        create_status_app(),
        host=host or quick_config.QUICK_STATUS_HOST,
        port=bind_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    # This server is a task inside the gateway's own loop, not the process's main
    # server: it must not take over SIGINT/SIGTERM (which would break shutdown, and
    # raises outright when the loop is not on the main thread).
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    logger.info(
        "Quick pool status page on http://{}:{}{} ({}).",
        config.host, bind_port, status_prefix() or "/",
        "token required" if QUICK_STATUS_TOKEN else "public, read-only",
    )
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise
    except OSError as exc:
        logger.warning("Quick status page could not bind {}:{} — {}.", config.host, bind_port, exc)
        if stop_on_error:
            raise
    return None


if __name__ == "__main__":  # pragma: no cover - manual run
    import asyncio

    asyncio.run(serve_status_page())
