# quick-gateway RUNBOOK (for Claude Code / agents)

Cold-start guide to run/deploy **quick-gateway**. Background/protocol: `quick/README.md`.

**What it is:** a FastAPI proxy under `quick/`, wired into `main.py`. Exposes
Anthropic-compatible `POST /quick/v1/messages` → Amazon Quick (Bedrock Converse).
Coexists with the Kiro routes in the same server. Auth = portable file
`~/.quickwork/gateway-creds.json`.

## 1. Get the creds file (once, on a mac signed into Amazon Quick)

```bash
python -c "from quick.auth import QuickAuthManager; QuickAuthManager()._load()"  # 1 Keychain prompt, first time
ls -l ~/.quickwork/gateway-creds.json   # verify: -rw------- (0600)
```
Already exists → done (loads from file, no prompt). New account → sign that account
into Amazon Quick desktop first, then run the above.

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
./quick/deploy.sh                              # defaults: HOST=43.160.157.90, PEM=~/tools/pem/rocky_test.pem
HOST=1.2.3.4 PEM=~/keys/box.pem ./quick/deploy.sh   # new box
# flags: --no-creds (code only) | --creds-only (rotate creds)
```
Success ends with `→ deploy ok`. The script is idempotent and handles rsync
`--checksum`, uid-999 chown, build, health-check, smoke-test. Container binds
`127.0.0.1:8000` only (not public).

## 4. Gotchas (don't relearn)

- Container runs as **uid 999** — the mounted `quickwork/` creds must be owned by 999.
- **`REFRESH_TOKEN=dummy`** in the compose only passes `main.py`'s startup check; Kiro
  routes are inert here (boot-time 401s are harmless). quick-gateway needs no Kiro creds.
- id_token = 5-min TTL → frequent `Refreshed ... 300s` logs are NORMAL. refresh_token
  ~90d, rotates, written back to the file. Daily keep-alive prevents idle lapse. Run on
  ONE host only.
- `QUICK_FORCE_MODEL` (default opus-4-8) forces every request to one model; opus-5 is
  IAM-denied. Empty = per-request mapping.
- **web_search** is gateway-executed (key-less DuckDuckGo); **web_fetch**/images pass through.
- Deploy always via `deploy.sh` (plain rsync skips edited files by mtime → stale container).

## 5. Optional: behind litellm

quick-gateway is Anthropic-compatible. Add to litellm `config.yaml` (back it up first):
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

## 6. Watch for a newer model

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
`QUICK_MODELS` in `quick/config.py`, and add a litellm entry if it is fronted there (§5).
Background: `quick/README.md` → "Detecting a new model".

## 7. Watch the session allowance

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

## 8. Trouble

- Startup "No Kiro credentials" → set `REFRESH_TOKEN` (any value) in the compose.
- "No Quick credentials: cache file absent" (Linux) → creds not copied / not owned by 999.
- Keychain keeps prompting (mac) → cache file missing/unreadable.
- 502 "validation error ... toolSpec" → a tool schema Bedrock rejects (`quick/converters.py`).
- 403 "not authorized" → account IAM-denied for that model.
- web_search empty → DuckDuckGo changed markup/rate-limited; fix regex in `quick/websearch.py`.
- Session-usage alert never fires → check `QUICK_ALERT_WEBHOOK` is set (else log-only) and
  that `"armed": true` in `quickwork/session-usage-state.json`.
- Before shipping: `pytest tests/unit/test_quick_gateway.py -v`.
