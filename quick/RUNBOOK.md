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

## 6. Trouble

- Startup "No Kiro credentials" → set `REFRESH_TOKEN` (any value) in the compose.
- "No Quick credentials: cache file absent" (Linux) → creds not copied / not owned by 999.
- Keychain keeps prompting (mac) → cache file missing/unreadable.
- 502 "validation error ... toolSpec" → a tool schema Bedrock rejects (`quick/converters.py`).
- 403 "not authorized" → account IAM-denied for that model.
- web_search empty → DuckDuckGo changed markup/rate-limited; fix regex in `quick/websearch.py`.
- Before shipping: `pytest tests/unit/test_quick_gateway.py -v`.
