# -*- coding: utf-8 -*-

"""Public, read-only status page for the Quick account pool.

This is a **separate ASGI app on a separate port** from the gateway, and that
separation is the point: publishing the gateway's own port would expose
``/quick/v1/messages`` — i.e. hand the account pool's inference quota to the
internet. This app serves two GET routes and nothing else.

Nothing it renders is a credential: no tokens, no tenant URL, no user ARN, no
account e-mail — only the labels derived from credential *filenames*, plus the
quota numbers Quick reports. Name the files neutrally (``b``, ``c``) if the page
is public and the humans behind the accounts should not be.

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
</style>
</head>
<body>
<div class="wrap">
  <h1>Quick 账号池</h1>
  <div class="sub">各账号剩余额度 · 每 20 秒自动刷新</div>
  <div class="sum">
    <div><b id="s-ready">–</b><span>可用账号</span></div>
    <div><b id="s-avg">–</b><span>平均剩余会话额度</span></div>
    <div><b id="s-inflight">–</b><span>进行中请求</span></div>
  </div>
  <div id="list"></div>
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
    return '<div class="card' + (a.status === "disabled" ? " dim" : "") + '">'
      + '<div class="row"><span class="name">' + esc(a.name) + "</span>"
      + '<span class="pill ' + a.status + '">' + (label[a.status] || a.status) + "</span></div>"
      + '<div class="big" style="color:' + color(v) + '">' + pct(v)
      + "<small>会话额度剩余</small></div>"
      + '<div class="bar"><i style="width:' + (v === null || v === undefined ? 0 : Math.max(0, Math.min(100, v)))
      + '%;background:' + color(v) + '"></i></div>'
      + '<div class="meta"><span>' + monthly + "</span>"
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

async function tick() {
  try {
    let base = location.pathname;                 // works under any status path
    while (base.endsWith("/")) base = base.slice(0, -1);
    const r = await fetch(base + "/api/pool" + location.search, {cache: "no-store"});
    if (!r.ok) throw new Error(r.status);
    render(await r.json());
  } catch (e) {
    document.getElementById("foot").textContent = "刷新失败（" + e.message + "），重试中…";
  }
}
tick();
setInterval(tick, 20000);
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

    # Both with and without the trailing slash; the page's fetch() derives the API
    # URL from its own pathname, so either entry point works.
    app.add_api_route(prefix or "/", _index, methods=["GET"], response_class=HTMLResponse)
    if prefix:
        app.add_api_route(f"{prefix}/", _index, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route(f"{prefix}/api/pool", _api_pool, methods=["GET"])

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
