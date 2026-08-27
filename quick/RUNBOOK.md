# quick-gateway RUNBOOK (for Claude Code / agents)

Cold-start guide to run/deploy **quick-gateway**. Background/protocol: `quick/README.md`.

**What it is:** a FastAPI proxy under `quick/`, wired into `main.py`. Exposes
Anthropic-compatible `POST /quick/v1/messages` → Amazon Quick (Bedrock Converse).
Coexists with the Kiro routes in the same server. Auth = portable files
`~/.quickwork/gateway-creds*.json`, **one per account** — the gateway pools them and
picks the account with the most quota left (`quick/pool.py`). Live status:
<http://43.160.157.90:9090/quick>.

## 1. Get a creds file (once per account, on a mac signed into Amazon Quick)

```bash
python -c "from quick.auth import QuickAuthManager; QuickAuthManager()._load()"  # 1 Keychain prompt, first time
ls -l ~/.quickwork/gateway-creds.json   # verify: -rw------- (0600)
```

**Adding another account** — sign it into the Amazon Quick desktop app (the app keeps
only the newest profile in `profiles.json`, and deletes the previous account's Keychain
item, so export each account right after signing it in), find its profile id, then export
it into its **own** file. The filename decides the pool account name
(`gateway-creds-b.json` → account `b`):

```bash
cat ~/.quickwork/profiles.json | python3 -c "import json,sys;print(json.load(sys.stdin)['last_active'])"
QUICK_PROFILE_ID=<that id> QUICK_CREDS_FILE=~/.quickwork/gateway-creds-b.json \
  python -c "from quick.auth import QuickAuthManager; QuickAuthManager()._load()"
```

The Keychain read pops one GUI approval dialog — click **Always Allow**. Then **quit the
Quick desktop app**: a running app keeps rotating that account's refresh token and will
invalidate the copy the server is using.

## 2. Run locally (mac)

```bash
pip install -r requirements.txt        # first time
python main.py --port 8000             # or ./run.sh restart
curl -sS localhost:8000/quick/v1/messages -H 'content-type: application/json' \
  -d '{"model":"claude-opus-4-8","max_tokens":16,"messages":[{"role":"user","content":"say ok"}]}'
```
Verify: log shows `source=file` (no prompt) and curl returns "Ok".
Client: `ANTHROPIC_BASE_URL=http://localhost:8000/quick`, token ignored.

## 3. Deploy to Linux (from the mac, after §1)

```bash
./quick/deploy.sh                     # code only + rebuild — DOES NOT touch creds
./quick/deploy.sh --creds b           # code + upload ONLY account b's creds
./quick/deploy.sh --creds-only b      # only upload account b's creds, then restart
HOST=1.2.3.4 PEM=~/keys/box.pem ./quick/deploy.sh   # new box
```

> ⚠️ **Uploading creds is opt-in and per account, on purpose.** The container refreshes
> and rewrites each file continuously; the Mac's copy of an account already running on
> the box is a rotated-away token. Overwriting it kills that account. Only upload an
> account you have just exported (§1).

Success ends with the pool table and `→ deploy ok`. The script is idempotent and handles
rsync `--checksum`, uid-999 chown, build, health-check, pool status, smoke-test.
Inference binds `127.0.0.1:8000` (not public); the status page binds `0.0.0.0:9090`
(public, read-only).

## 4. Gotchas (don't relearn)

- Container runs as **uid 999** — the mounted `quickwork/` creds must be owned by 999.
- **Account pool**: every `quickwork/gateway-creds*.json` is one account (`…-b.json` → `b`).
  Requests go to the account with the most session allowance left, with failover; a
  malformed request (400) is never retried on a second account. `/quick/pin/b/v1/messages`
  pins one account. State files in the same dir are not mistaken for creds.
- **`REFRESH_TOKEN=dummy`** in the compose only passes `main.py`'s startup check; Kiro
  routes are inert here (boot-time 401s are harmless). quick-gateway needs no Kiro creds.
- id_token = 5-min TTL → frequent `Refreshed ... 300s` logs are NORMAL. refresh_token
  ~90d, rotates, written back to the file. Daily keep-alive prevents idle lapse. Run on
  ONE host only.
- `QUICK_FORCE_MODEL` (default opus-4-8) forces every request to one model; opus-5 is
  IAM-denied. Empty = per-request mapping.
- **web_search** is gateway-executed (key-less DuckDuckGo); **web_fetch**/images pass through.
- Deploy always via `deploy.sh` (plain rsync skips edited files by mtime → stale container).

## 5. The status page (port 9090, public)

<http://43.160.157.90:9090/quick> — one card per account: session allowance left, monthly
used, ready/cooling/disabled, cooldown countdown. Auto-refreshes every 20 s. It is a
**separate app on a separate port** (`quick/status_app.py`) with no inference route, so
publishing it does not expose `/quick/v1/messages`. It is served **only** under
`QUICK_STATUS_PATH` (default `/quick`) — every other path, `/` included, returns a bare
404, so a scanner sweeping the port finds nothing. It shows no credential material —
only the labels derived from filenames, so keep filenames neutral (`b`, `c`) rather than people's names.

```bash
curl -s localhost:8000/quick/pool | python3 -m json.tool   # same data, on the box
docker compose -p quick-gateway exec quick-gateway python -m quick.usage_watch --pool
```

The path is obscurity, not a boundary: gate it with `QUICK_STATUS_TOKEN=<secret>` in
`/opt/quick-gateway/.env` (then `?t=<secret>`), or turn it off with `QUICK_STATUS_PORT=0`.

## 6. Optional: behind litellm

quick-gateway is Anthropic-compatible, and the pool is invisible to litellm — one
`api_base`, N accounts behind it. **No litellm change is needed to add an account.**
Add to litellm `config.yaml` (back it up first):
`api_base: http://quick-gateway:8000/quick`, `model: anthropic/claude-opus-quick`,
`model_name: claude-opus-quick` → `docker restart litellm`. (Container must share
litellm's docker network.)

**Cost tracking**: every Claude entry on the box is priced at Anthropic's official list
rate (as of 2026-08-27: Opus $5/$25, Sonnet 5 $2/$10, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5
per MTok). Quick is a flat-rate account, so `claude-opus-quick` is priced at **1/10 of
official Opus** — `input_cost_per_token: 5.0e-07`, `output_cost_per_token: 2.5e-06`.
Each model has two entries (`claude-x` and `us.anthropic.claude-x`) — change both.
litellm only reads these on restart; verify with
`curl -s localhost:4000/v1/model/info -H "Authorization: Bearer <master-key>"` and by
checking `spend` on the next `LiteLLM_SpendLogs` row.

## 7. Watch for a newer model

Quick's model registry is a **public CloudFront JSON** (no auth, no DataPlane traffic) —
diff it instead of probing inference. Exit code 10 = changed:

```bash
python -m quick.model_watch            # or --json for a machine-readable report
# on the box:
docker compose -p quick-gateway exec quick-gateway python -m quick.model_watch
```

**The gateway already does this on a timer** — `main.py` starts `watch_loop()` every
`QUICK_MODEL_WATCH_INTERVAL` s (default 18000 = 5 h). Every change is logged at WARNING;
only an **upgrade** (a newer Opus-class model, i.e. one beating `QUICK_UPGRADE_BASELINE`)
is pushed to the chat robot, in three lines, when `QUICK_ALERT_WEBHOOK` is set
(`QUICK_ALERT_ALL_CHANGES=1` pushes everything — noisy). The webhook is a secret and **this repo
is public**, so it lives in the box's compose `.env`, not in git — one-time setup:

```bash
ssh rocky@43.160.157.90 'cat >> /opt/quick-gateway/.env' <<'EOF'
QUICK_ALERT_WEBHOOK=https://www.yunzhijia.com/gateway/robot/webhook/send?yzjtype=0&yzjtoken=<token>
EOF
cd /opt/quick-gateway && docker compose -p quick-gateway up -d      # picks it up
docker compose -p quick-gateway exec quick-gateway python -m quick.model_watch --test-notify
```

`deploy/quick/docker-compose.yml` passes it through as `${QUICK_ALERT_WEBHOOK:-}`, and
`quick/deploy.sh` never copies `.env`, so the box's file survives redeploys. No cron
needed — run the CLI by hand only for an ad-hoc check.

When a new id shows up, confirm account access with **one** request — from the box
only (running auth on the mac rotates the refresh_token and breaks the box's copy):

```bash
docker compose -p quick-gateway exec quick-gateway \
  python -m quick.model_watch --probe us.anthropic.claude-opus-5   # available|denied|unknown_model
```

`available` → set `QUICK_FORCE_MODEL` to it in the compose and restart; also add it to
`QUICK_MODELS` in `quick/config.py`, and add a litellm entry if it is fronted there (§6).
Background: `quick/README.md` → "Detecting a new model".

## 8. Watch the session allowance

Quick meters a rolling **session** allowance plus a monthly entitlement, and attaches
both to every inference response (`usageSummary` on the Converse `metadata` frame).
`quick/usage_watch.py` reads them for free from the traffic the gateway already
serves — no extra API, no polling of the DataPlane:

```bash
docker compose -p quick-gateway exec quick-gateway python -m quick.usage_watch --json
# {"session_remaining_pct": 62, "session_used_pct": 38, "monthly_used_pct": 100, ...}
```

`main.py` runs `watch_loop()` every `QUICK_SESSION_WATCH_INTERVAL` s (default 3600).
It pushes **one** message to `QUICK_ALERT_WEBHOOK` when the remaining share first
drops below `QUICK_SESSION_ALERT_REMAINING_PCT` (default 10 %), then stays quiet until
it recovers past that mark — the armed flag lives in
`quickwork/session-usage-state.json` next to the creds, so a restart does not re-fire.
Beware the sign: the API reports `usedPercentage` (consumed); the alert is about
`100 - used`. If the gateway has been idle longer than the interval, the cycle spends
one 1-token request (haiku) to refresh the reading — so the CLI needs creds, i.e. run
it in the container, not on the mac. Background: `quick/README.md` → "Watching the
session allowance".

## 9. Trouble

- Startup "No Kiro credentials" → set `REFRESH_TOKEN` (any value) in the compose.
- "No Quick credentials: cache file absent" (Linux) → creds not copied / not owned by 999.
- Keychain keeps prompting (mac) → cache file missing/unreadable.
- 502 "validation error ... toolSpec" → a tool schema Bedrock rejects (`quick/converters.py`).
- 403 "not authorized" → account IAM-denied for that model.
- web_search empty → DuckDuckGo changed markup/rate-limited; fix regex in `quick/websearch.py`.
- Session-usage alert never fires → check `QUICK_ALERT_WEBHOOK` is set (else log-only) and
  that `"armed": true` under `accounts.<name>` in `quickwork/session-usage-state.json`.
- An account shows **disabled** on the status page → its refresh token is dead
  (`invalid_grant`); re-export it on the mac (§1) and `./quick/deploy.sh --creds-only <name>`.
- An account shows **cooling** → normal: its allowance hit 0 or entitlement was revoked;
  it returns by itself. A *low but usable* account stays `ready` (a low `resumeInMinutes`
  is a window-reset countdown, not a lockout).
- All accounts unusable → the pool pushes one alert; add an account (§1 + §3).
- Before shipping: `pytest tests/unit/test_quick_gateway.py -v`.
