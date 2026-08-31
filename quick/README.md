# Quick Gateway (Amazon Quick backend)

An Anthropic-compatible gateway for **Amazon Quick** (`com.amazon.QuickWork.mac`),
built alongside the Kiro gateway and reusing its conventions. Exposes
`POST /quick/v1/messages` and translates to Quick's tenant DataPlane, which
proxies Amazon Bedrock `Converse` / `ConverseStream`.

**Status: fully working** — text, streaming, and tool-use verified end-to-end
against the live backend, over a **pool of accounts** (quota-aware selection with
failover) fronted by a public read-only quota page.

```
Anthropic client ─▶ /quick/v1/messages ─▶ converters ─▶ quick.client ─▶ tenant DataPlane ─▶ Bedrock
                        (routes.py)        (Converse)    (REST proxy)    (*.plato.ai.aws.dev)
```

## Usage

Point any Anthropic client at the gateway (the client token is ignored — the
gateway authenticates to Quick itself):

```bash
python main.py --port 8000
export ANTHROPIC_BASE_URL="http://localhost:8000/quick"
export ANTHROPIC_AUTH_TOKEN="123"
```

## Credentials & headless / Linux deploy

Auth is portable via a single JSON cache file (`QUICK_CREDS_FILE`, default
`~/.quickwork/gateway-creds.json`):

- **macOS**: on first start the Keychain is read **once** (one login-password prompt),
  and the blob is written to the cache file at `0600`. Every later start loads from the
  file — no prompt. Delete the file to re-bootstrap from the Keychain.
- **Linux / headless** (no Keychain, no `security` CLI): copy the cache file over once and
  the gateway bootstraps from it. Keycloak refresh (`quick.piaozone.net`, public internet)
  keeps tokens fresh and rewrites the rotated `refresh_token` back into the file.

```bash
# one-time: seed the file on a signed-in mac (any local start does this), then copy it
scp ~/.quickwork/gateway-creds.json  user@linux-box:~/.quickwork/gateway-creds.json
```

**Run the gateway on exactly one host.** Keycloak rotates the `refresh_token` on every
refresh, so two hosts refreshing from copies of the same file will eventually invalidate
each other. This is per *file*, so a multi-account pool on one host is fine — what is not
fine is the same account's file living in two running places (two hosts, or the box plus
a signed-in desktop app). Re-copy only if the offline `refresh_token` fully lapses
(~90 days) or you re-sign-in to Quick on the mac.

For a second account, export it into its own file (the pool picks it up by filename):

```bash
QUICK_PROFILE_ID=<new-profile-id> QUICK_CREDS_FILE=~/.quickwork/gateway-creds-b.json \
  python -c "from quick.auth import QuickAuthManager; QuickAuthManager()._load()"
```

### Docker (isolated deploy — the `deploy/quick/` compose)

`deploy/quick/docker-compose.yml` runs quick-gateway as its own compose project on host
port 8000, mounting `./quickwork` → `/home/kiro/.quickwork`. The image runs as uid 999
(`kiro`), so the creds dir/file on the host must be owned by 999 for refresh to persist:

```bash
mkdir -p quickwork && cp ~/.quickwork/gateway-creds*.json quickwork/ && chown -R 999:999 quickwork
docker compose -p quick-gateway up -d --build
```

Port `8000` (inference) stays bound to `127.0.0.1`; port `9090` (the status page)
is published publicly on purpose.

`REFRESH_TOKEN=dummy` in that compose only satisfies `main.py`'s startup credential check
(the Kiro `/v1/*` routes are inert in this container); quick-gateway needs no Kiro creds.

## Account pool (`pool.py`)

Quick meters a **rolling session allowance plus a monthly entitlement per account**
(per *user*, not per tenant — verified: two users on one tenant have independent
buckets). So capacity scales by adding accounts, and the thing to schedule against
is quota, not concurrency.

Every credential file in `QUICK_CREDS_DIR` matching `gateway-creds*.json` is one
pool member; the account name comes from the filename:

| file | account |
|------|---------|
| `gateway-creds.json` | `default` |
| `gateway-creds-b.json` | `b` |

**Adding an account is: drop one more file in.** No extra container, no
load-balancer config, and no restart either — `discover()` re-scans the directory on
every pass (the hourly `usage_watch` cycle, and every status-page poll), so a new
file joins the pool by itself and a removed one leaves it. (Contrast the Kiro pool
next door: one container per account behind an nginx `least_conn` upstream, where a
dead account is discovered by a 502 and fixed by hand-commenting a line.)

### How an account is chosen

Selection is **per request** — Converse is stateless, the gateway sends the full
transcript every time, and nothing here uses prompt caching, so switching accounts
mid-conversation is invisible to the client. There is deliberately **no key→account
pinning by default**: pinning starves one account while the others idle, and takes
the key down with it.

1. drop accounts that are disabled or cooling down;
2. apply the **overage preference** (below) — while some account still has monthly
   units, a spent one is ordered last (it is never dropped);
3. rank by the **tighter of the session and monthly shares**, bucketed to 10 %
   (`QUICK_POOL_QUOTA_BUCKET`) — ranking on the raw percentage would ping-pong the
   choice on every reading;
4. then by the session share alone, which still separates accounts once every
   monthly bucket is spent and overage is carrying the pool;
5. break ties by in-flight requests, then by requests served — round-robin within a
   bucket, which also spreads the first requests before any reading exists.

An unmeasured account counts as full, so a freshly added one gets the first request
(which produces the reading that then ranks it honestly).

> **Rank on what actually stops the account** (learned live 2026-08-31). Selection
> used to read the session share only. The session window is what throttles an account
> minute to minute, but the *monthly* entitlement is what hard-blocks it — with overage
> off, spending the last unit earns `BLOCKED_MONTHLY` until the reset, not a bill. An
> account reading a fresh session window and 4 % of its month left was ranked top and
> walked straight into that. Live, the same rule produced the mirror image: one account
> read 0 % session used and took **every** request while the other, holding a 28 % older
> session reading, served none — and readings only refresh from real traffic, so the
> account that is not chosen keeps its stale number and stays unchosen. Taking the
> tighter of the two also spends the pool evenly by *fraction* consumed, which is the
> fair split when the accounts belong to different people and their limit profiles
> differ in size. `snapshot()` reports `headroom_pct` and `binding_allowance`, and the
> status page prints them, so a card showing "100 % 会话额度剩余" at the back of the
> queue explains itself.

> **A reading expires by its own statement.** Every monthly reading carries `resetsAt`;
> past that moment "0 units left" is last month's fact. Since the pool only re-reads an
> account it *selects*, ranking on an expired reading would keep a spent account at the
> bottom straight through the reset that refilled it. So past `resetsAt` the monthly
> figures count as unknown — which also means the account is no longer treated as
> exhausted for the overage preference.

### Failover

A request that fails on quota, throttling, credential or backend grounds cools that
account down and is **retried on the next candidate** (`QUICK_POOL_MAX_ATTEMPTS`,
default 2). A malformed request (HTTP 400 / validation) is *not* retried — it would
fail identically everywhere and just burn a second account's quota.

Streaming holds back `message_start` until the first real event arrives, so a
failover before the first token is invisible to the client; Quick's HTTP-200
in-stream `error` frame is caught there too. Once bytes are on the wire the stream
belongs to the client and the error is surfaced as-is.

### What benches an account

| condition | effect |
|-----------|--------|
| `entitlementStatus != ALLOWED` | cooldown (`QUICK_POOL_COOLDOWN_SECONDS`, default 15 min) — but a **monthly** block waits for the reading's own `resetsAt` |
| in-stream `usageLimitExceeded` frame | the request fails over; the account cools until `resetsAt` |
| session allowance at 0 % | cooldown until the window resets |
| 429 / IAM deny / backend error | cooldown 5 min / 5 min / 1 min, **doubling per consecutive failure** up to `QUICK_POOL_MAX_COOLDOWN_SECONDS` (1 h) |
| a **real failure** while the monthly bucket is spent | re-classified as a quota block → cooldown until `resetsAt` |
| Keycloak `invalid_grant` on refresh | **disabled** + one alert — waiting never fixes it, only a re-uploaded creds file (which the pool then picks up by itself, below) |

Every quota bench is capped at `QUICK_POOL_MAX_QUOTA_COOLDOWN_SECONDS` (default 2 h), and
a reading that reports real headroom **retracts** one:

| condition | effect |
|-----------|--------|
| a quota deadline (`resetsAt`) further out than the cap | shortened to the cap, so the wait is a repeating half-open trial |
| reading says ALLOWED **and** the buckets it names have room | the quota bench is lifted immediately (the failure streak is not) |
| the same reading, but the monthly bucket is still empty (overage on) | left benched — that reading agrees with the bench |
| a bench from a 429 / IAM deny / 5xx | left benched — quota headroom is no evidence about those |

> `resumeInMinutes` is **not** a lockout timer — it counts down to the rolling
> session window's reset and is populated while the account is perfectly usable
> (verified live: 21 % left, `resumeInMinutes` 55, `entitlementStatus` ALLOWED). It
> only sizes the cooldown once the allowance really is exhausted. A merely *low*
> account needs no special case: selection already ranks it below its siblings.

**Only a real failure evicts. Detection is by real traffic — never by a health probe,
and never by predicting a rejection we have not seen.** A prober would spend the very
quota this protects, and it exercises a different path than real requests (a haiku
probe passing says nothing about Opus). A prediction is worse still: benching a
working account on the guess that Quick *would* block it costs capacity for nothing.

* **the trigger is the backend** — a request that fails, or a reading in which Quick
  itself says the account is blocked (`entitlementStatus != ALLOWED`, session at 0 %).
  A spent monthly bucket is **not** a trigger, with or without overage: Quick has
  served every request we have ever sent at 0 units;
* **the reading sizes the rest** — once something really fails, the free
  `usageSummary` decides how long to wait (`resetsAt`, `resumeInMinutes`) instead of
  a made-up constant, and the request itself fails over to the next candidate;
* **coming back** — the cooldown is an absolute deadline and the first request after
  it expires *is* the half-open trial: it either succeeds (streak cleared) or re-cools
  with the next backoff step. The hourly `usage_watch` cycle skips its probe for a
  benched account precisely so it does not spend a failing request per hour. This is
  why a quota deadline is **capped** rather than obeyed literally: a `resetsAt` three
  weeks out would mean three weeks with no trial and no probe, i.e. no way at all to
  notice the block had lifted.

* **the one thing a reading cannot fix** — a refresh token Keycloak rejects outright
  (`invalid_grant`) never recovers on its own, so that account is *disabled* rather
  than cooled, and only a new credential file helps. `discover()` therefore watches
  for exactly that: when an account's file no longer matches what its own manager
  last wrote, the upload is treated as the operator saying "try again" — the disable,
  the cooldown and the failure streak are cleared and the dead in-memory token is
  dropped. The trigger is **whose write was last**, never mtime alone: the gateway
  rewrites every file on every token rotation, so mtime alone would revive a cooling
  account several times an hour. Discovery runs on the hourly `usage_watch` cycle and
  on every status-page poll, so an `scp` heals the pool without a restart.

> **A bench is a claim, and a reading can refute it** (learned live 2026-08-30). The
> tenant admin raised the limit profile from 480 to 1080 units/user; `default`, benched
> the day before until `resetsAt`, was in fact `ALLOWED` with 360 units — and stayed out
> of rotation, because `observe_usage` only ever benched and never released (`revive()`
> existed with no caller), while `usage_watch` skips benched accounts by design. The
> account slept on 6 usable agent-hours that units-don't-roll-over would have destroyed
> at the monthly reset. Symmetry is the fix: whatever a reading may bench an account
> for, that same reading may un-bench it for — and only for that. A healthy
> `usageSummary` refutes "no quota"; it says nothing about a 429 or an IAM deny, so
> those benches stand.

> **Trust the reading over the error text — but only after something failed.** When an
> account's monthly bucket is spent, *any* failure is that block however the backend
> words it. Keyword-matching alone would retry a month-long block every 60 s.

### How Quick refuses a blocked account (seen live 2026-08-28)

The tenant admin turned overage off. Both accounts immediately read
`entitlementStatus: BLOCKED_MONTHLY` with `provisionedUnits` zeroed, and every request
came back as **HTTP 200 carrying exactly one frame** — no `messageStart`, no content,
no `metadata`:

```json
{"eventType": "usageLimitExceeded", "payload": {},
 "usageSummary": {"entitlementStatus": "BLOCKED_MONTHLY", "overageEnabled": false,
                  "monthlyUsage": {"availableUnits": 0, "provisionedUnits": 480,
                                   "resetsAt": 1788220800, "usedPercentage": 100}}}
```

It is **not** the `{"eventType":"error"}` shape an IAM deny uses, and its own payload is
empty — the reason lives in the sibling `usageSummary`. Before this was recognised, the
translator turned that stream into a well-formed *empty answer*: `content: []`,
0 input/0 output tokens, `stop_reason: end_turn` — a blank reply to the client and a
recorded success for the pool. Now `streaming.py` treats it as a backend error, which
buys the whole existing chain: fail over before the first token, surface **429** with
the entitlement, units and reset time in the text, and (via `note_failure`) cool the
account until `resetsAt` instead of releasing it every 15 minutes to be refused again.

The same lesson generalised: **a stream with no events at all is never served as an
answer.** An empty body fails over like any other backend error.

### Overage preference

`QUICK_POOL_OVERAGE_POLICY` decides the **order**, never the membership, for an
account whose monthly entitlement is spent (so Quick serves it on overage):

| value | behaviour |
|-------|-----------|
| `avoid` *(default)* | pick it last, while any account still has monthly headroom |
| `allow` | no demotion — it is ranked like any other account |

Note this is now a *demotion* switch only: the monthly share enters the ranking above
either way, so `allow` no longer means "rank on the session alone".

An unrecognised value falls back to `avoid`. The deprecated
`QUICK_POOL_AVOID_OVERAGE=1` still forces `avoid`.

A spent account that is the only one left **still serves the request** — the pool
never answers 503 over money. If you want a hard stop, turn overage off on the
account itself: Quick then enforces it, and the pool reacts to the real rejection
like any other failure. `snapshot()` reports `on_overage` per account so the status
page can show who is spending, without that ever reading as "broken".

### Pinning (the escape hatch)

`POST /quick/pin/{account}/v1/messages` serves the same API against one named
account, with no failover — for reserved capacity or for debugging one account. In
litellm, give it its own `model_name` (`api_base: …/quick/pin/b`) and grant that
model only to the key that should be isolated.

```bash
curl localhost:8000/quick/pool                    # per-account quota, as JSON
python -m quick.usage_watch --pool                # same, as a table
python -m quick.usage_watch --account b --json    # one account
```

| env | meaning |
|-----|---------|
| `QUICK_CREDS_DIR` | directory scanned for credential files (default: `QUICK_CREDS_FILE`'s dir) |
| `QUICK_ACCOUNTS` | explicit account list (`default,b`), overriding discovery |
| `QUICK_POOL_MAX_ATTEMPTS` | accounts one request may try (default 2; 1 = no failover) |
| `QUICK_POOL_COOLDOWN_SECONDS` | default cooldown when the backend gives no resume hint (900) |
| `QUICK_POOL_MAX_QUOTA_COOLDOWN_SECONDS` | cap on a quota bench, so a far-off `resetsAt` still gets a half-open trial (7200 = 2 h; `0` = obey `resetsAt` exactly) |
| `QUICK_POOL_QUOTA_BUCKET` | ranking bucket width in percent (10) |
| `QUICK_POOL_AVOID_OVERAGE` | `1` to prefer accounts with monthly headroom over overage |

> **One writer per credential file.** Keycloak rotates the refresh token on every
> refresh, so the same file must never be refreshed from two places — that is why
> `deploy.sh` only uploads accounts you name explicitly, and why the desktop app
> should be signed out after a creds export.

## Status page (`status_app.py`)

A read-only page with two tabs, served on `QUICK_STATUS_PORT` (default 9090) under
`QUICK_STATUS_PATH` (default `/quick`) — **a separate ASGI app on a separate port**,
and that is the point: publishing the gateway's own port would expose
`/quick/v1/messages`, i.e. hand the pool's quota to the internet. This app has three
routes (the page and its two JSON endpoints) and no inference path; **everything else
answers a bare 404**, so a scanner sweeping `/` finds nothing. That is obscurity, not
a boundary — `QUICK_STATUS_TOKEN` is the boundary.

| tab | question it answers | source |
|-----|---------------------|--------|
| 账号额度 | how much Quick allowance is left, per account | the pool's own readings |
| 本月花费排名 | **which model, and which virtual key**, this month | litellm (`litellm_usage.py`) |

It renders no credential material — no token, tenant URL, user ARN, and never a key,
only labels: account names come from *filenames*, spender names are litellm **key
aliases**. Both identify people once you name them after their owners, which is why
this deployment sets `QUICK_STATUS_TOKEN` (require `?t=<token>` or an
`X-Status-Token` header). Name things neutrally (`b`, `c`) instead if the page must
stay open. `QUICK_STATUS_PORT=0` disables the server.

```
http://<box>:9090/quick?t=<token>              # the page
http://<box>:9090/quick/api/pool?t=<token>     # account quota, as JSON
http://<box>:9090/quick/api/litellm?t=<token>  # this month's spend ranking, as JSON
```

### The spend tab (`litellm_usage.py`)

quick-gateway cannot attribute a request to a person — litellm is the auth boundary,
so the gateway never sees a client key. litellm can, and the tab reads it from
`GET /user/daily/activity`, the endpoint litellm's own Usage UI calls. It is the only
one that gives per-key **and** per-model numbers on a community licence:
`/global/spend/report` is enterprise-gated (403 with a licence pitch) and
`/global/spend/keys` is per-key but all-models with no token counts.

It reports **per channel and per key**, from one walk of the month: each row is
counted into the key's total and into the channel its query was filtered to. So the
tab shows what `claude-opus-quick` and `claude-sonnet-quick` each cost (with the
share of the month's spend), and a key that used both gets its split under its name.
Channels the model watch has never published simply do not appear.

The dollars are not the quota, and the page says so: a Quick unit is charged by
tokens and does not care which model produced them, while each channel is priced at
1/10 of *its own* official list — so a dollar share is not a unit share. Tokens sit
in the next column for exactly that reason.

Two things had to be read off real rows to get it right:

* litellm records the **resolved** model (`anthropic/claude-opus-quick`) on a success
  and the **requested** one (`claude-opus-quick`) on a failure, so both names are
  queried and merged (`LITELLM_QUICK_MODELS`) — otherwise a month of 429s during a
  quota block would vanish from the ranking;
* `breakdown.api_keys` is scoped by whatever filter the query carried. The per-key
  numbers are only this channel's *because* `model=` was passed; reading the same
  field off an unfiltered query would report each key's spend across every model in
  the proxy.

The dollar figures are litellm's accounting at its configured rates (each channel at
1/10 of its own model's official list), **not** an AWS invoice — the Quick seats are flat-rate
with a unit quota. The page says so, because a ranking in dollars invites the opposite
reading. The report is cached for `LITELLM_USAGE_CACHE_SECONDS` (the page polls every
20 s and the query walks a month of daily rows), and a failed refresh serves the last
good ranking marked stale rather than blanking the tab.

| env | meaning |
|-----|---------|
| `LITELLM_MASTER_KEY` | litellm admin key. **Secret — `.env` only, this repo is public.** Empty = tab off |
| `LITELLM_BASE_URL` | where litellm lives (default `http://litellm:4000`, over the shared docker network) |
| `LITELLM_QUICK_MODELS` | model names that count as this channel — **every** published channel, resolved *and* requested spelling (default `anthropic/claude-opus-quick,claude-opus-quick,anthropic/claude-sonnet-quick,claude-sonnet-quick`) |
| `LITELLM_USAGE_CACHE_SECONDS` | how long a report is served before re-querying (300) |

## Confirmed protocol

**Auth**
1. Cache file `~/.quickwork/gateway-creds.json` if present (the only path on Linux);
   otherwise, on macOS, `~/.quickwork/profiles.json` → `last_active` profile id.
2. macOS Keychain (one-time bootstrap only):
   `security find-generic-password -s quickwork-enterprise-<id> -a session -w`
   → JSON blob (`refresh_token`, `id_token`, `token_endpoint`, `client_id`, `tenant_url`, …),
   which is then persisted to the cache file.
3. Keycloak refresh (`quick.piaozone.net`, client `quick-desktop`) → fresh tokens
   (rotated `refresh_token` written back to the cache file).
4. **The DataPlane bearer is the `id_token`** (aud=`quick-desktop`), *not* the
   access_token (aud=`account`). Sending the access_token returns
   `401 {"Message":"Token verification failed"}`.

**Inference** — `POST {tenant_url}/integration/quick-work/bedrock-proxy-stream`

```
Authorization: Bearer <id_token>
Content-Type: application/json

{"modelId": "us.anthropic.claude-sonnet-4-6",
 "requestBody": { <Bedrock Converse params: messages, system, inferenceConfig, toolConfig, …> }}
```

Response is an AWS event stream. Every frame's `:event-type` header is
`bedrockStreamEvent`; the JSON payload is `{"eventType": "<Converse event>",
"payload": {…}}` (unwrapped in `streaming.py`). The `metadata` frame carries one
extra sibling — `usageSummary`, the account's session / monthly entitlement —
which `usage_watch.py` reads (see "Watching the session allowance").

The sibling non-stream path `/integration/quick-work/bedrock-proxy` uses a
different (InvokeModel) schema, so the gateway routes **everything** through the
stream path and aggregates for non-streaming responses.

## Modules

| File | Role |
|------|------|
| `config.py` | Keychain/profile discovery, model map, DataPlane paths |
| `auth.py` | `QuickAuthManager`: Keychain read + Keycloak refresh (exposes `id_token`) |
| `converters.py` | Anthropic Messages → Bedrock `Converse` input |
| `streaming.py` | event-stream decoder, `bedrockStreamEvent` unwrap, SSE translator + aggregator |
| `client.py` | DataPlane HTTP client (envelope + id_token bearer + retry) |
| `websearch.py` | key-less DuckDuckGo web search (executes `web_search` gateway-side) |
| `model_watch.py` | watch the public model registry, alert on a newer Opus |
| `usage_watch.py` | per-account allowance readings, alert once when one runs low |
| `pool.py` | the account pool: discovery, quota-aware selection, cooldown/failover |
| `status_app.py` | read-only status page: quota + spend tabs (own port, no inference route) |
| `litellm_usage.py` | this channel's per-key monthly spend, read from litellm's usage API |
| `agent_loop.py` | model↔web_search loop (Path B): run, search, feed back, repeat |
| `routes.py` | FastAPI `/quick/v1/messages`, `/quick/pin/{account}/…`, `/quick/pool` |

## Tools

**Function tools** pass through: the client's tools become a Converse `toolConfig`,
the model emits `tool_use`, and the gateway returns it for the client to execute
(empty tool descriptions fall back to the tool name — Bedrock requires length ≥ 1).

**`web_search`** — Amazon Quick's Bedrock backend does **not** run Anthropic's
native server-side `web_search`. The gateway executes it itself (Path B, like
kiro-gateway): when the model calls `web_search`, `agent_loop.py` runs a key-less
DuckDuckGo search (`websearch.py`, via the bundled httpx — no API key, no extra
dependency), feeds the results back as a Converse `toolResult`, and re-invokes the
model, looping until it answers (bounded by `MAX_SEARCH_ROUNDS`). Any *other*
tool the model calls is returned to the client untouched.

> Caveat: DuckDuckGo's HTML endpoint has no official API contract — if results
> suddenly come back empty, DDG likely changed its markup or rate-limited us;
> update the regex in `websearch.py` or swap the source.

**`web_fetch`** — needs no gateway handling; it already works as a pass-through.

**Images** — `image` blocks pass through as base64 *strings* (the DataPlane request
is JSON, so `source.bytes` is a base64 string, not raw bytes — verified live).
`jpg`→`jpeg`; invalid base64 is dropped rather than crashing.

## Models

Authorized for this account (verified live — real completions):

- Opus: `us.anthropic.claude-opus-4-8`, `us.anthropic.claude-opus-4-6-v1`
- Sonnet: `us.anthropic.claude-sonnet-5` (probed 2026-08-31), `us.anthropic.claude-sonnet-4-6`
- Haiku: `us.anthropic.claude-haiku-4-5-20251001-v1:0`

`us.anthropic.claude-opus-5` **exists on the backend but this account is
IAM-denied** (explicit deny on `InvokeModelWithResponseStream`) — it returns HTTP
200 with an in-stream `error` frame, not a completion. So opus-4-8 is the best
usable Opus. Sonnet 5 is the counter-example — the same two-stage check
(registry, then `--probe`) came back `available`, so the family target for Sonnet
*is* sonnet-5; entitlement is per model, never a family-wide guess.

**Forcing** — by default the gateway forces *every* request to a single model
(`QUICK_FORCE_MODEL`, default `us.anthropic.claude-opus-4-8`), ignoring whatever
the client picked. Set `QUICK_FORCE_MODEL=` (empty) in `.env` to disable and use
per-request mapping instead: `resolve_model()` then maps any Claude name to a
Quick id by exact id/alias, else by family (Opus→opus-4-8, Sonnet→sonnet-5,
Haiku→haiku-4-5). The client's `/model` menu (Opus 5, Sonnet 5, …) is its own
list and is *not* served by this gateway.

### Channels — publishing more than one model from one gateway

A client sending `claude-opus-5` picked that name off *its own* menu and knows
nothing about Quick; the force exists to overrule it. A name ending in **`-quick`**
is the opposite: only this gateway answers to those names, so one is a routing
instruction from whoever configured the channel — and it is the one thing
`QUICK_FORCE_MODEL` does not override.

| channel (litellm `model_name`) | Quick model |
|--------------------------------|-------------|
| `claude-opus-quick` | `us.anthropic.claude-opus-4-8` |
| `claude-sonnet-quick` | `us.anthropic.claude-sonnet-5` |
| `claude-haiku-quick` *(unpublished; works if you add it)* | `us.anthropic.claude-haiku-4-5-…` |

The suffix names a **family**, not an id, so the table is derived rather than
maintained (`QUICK_OPUS_MODEL` / `QUICK_SONNET_MODEL` / `QUICK_HAIKU_MODEL` move a
whole family, channel included). Within its own family the forced model wins, so
pointing `QUICK_FORCE_MODEL` at opus-5 the day it is entitled carries
`claude-opus-quick` along instead of silently leaving it on 4.8.

Both channels share one gateway, one account pool and one quota: the split is about
which model answers, never about capacity. Add the channel to `LITELLM_QUICK_MODELS`
too, or the spend tab will rank the pool's quota as if that traffic did not exist.

Backend failures arrive in-stream under HTTP 200: `{"eventType":"error"}` for an IAM
deny, `{"eventType":"usageLimitExceeded"}` for a blocked entitlement. The gateway
surfaces these as real errors (403 for IAM denies, 429 for an entitlement block, else
502) rather than as empty completions.

### Detecting a new model (`model_watch.py`)

Quick publishes its **model registry in a public, unauthenticated CloudFront config**
that the desktop app polls every ~60 s:

```
https://d2tws6r933zatt.cloudfront.net/quickwork/prod/feature_flag_config.json
```

It holds the mode→model map (`fast` / `balanced` / `smart`, each with a `.thinking`
variant) for the base config *and* every staged-rollout override — per region, per
percentage bucket. A new Claude model appears here (typically as a regional
percentage canary) before it is worth spending a single inference request. It carried
`us.anthropic.claude-sonnet-5` in a 50 % bucket for us-west-2 / eu-* / ap-southeast-2
on 2026-08-27 and at **99 %** four days later — a rollout watched end to end, and the
reason `claude-sonnet-quick` exists. `smart` is `opus-4-8` there and `opus-4-6-v1` in
the base config; our tenant is us-east-1, where `balanced` is still sonnet-4-6 — the
registry's mode map is the *app's*, and says nothing about what the account may call
directly (sonnet-5 probes `available` from us-east-1).

So detection is two-stage, and stage 1 never touches the tenant DataPlane:

```bash
python -m quick.model_watch            # conditional GET + diff vs saved state
python -m quick.model_watch --json     # machine-readable report
```

Exit code `10` = something changed (cron-friendly); state and ETag live in
`~/.quickwork/model-watch-state.json` (`QUICK_MODEL_WATCH_STATE`). Polling every 5 h is
~1/300 of the app's own rate against a CDN object with no credentials attached, so it
cannot trip inference-side rate limiting.

The gateway also runs this on a timer itself: `main.py`'s lifespan starts
`watch_loop()` every `QUICK_MODEL_WATCH_INTERVAL` seconds (default 18000 = 5 h,
`0` disables).
Every change is logged at WARNING, but **only an upgrade is pushed** to the chat robot
(`POST {"content": "…"}`) — three lines, no config dump:

```
🚀 Quick 新模型：us.anthropic.claude-opus-5（基线 us.anthropic.claude-opus-4-8）
位置：rule1[regions=us-west-2+4]/cfg1(50%)/default/smart
确认权限：python -m quick.model_watch --probe us.anthropic.claude-opus-5
```

An id counts as an **upgrade** when its family is at least as strong *as the baseline*
and its version is newer — i.e. "a new Opus-class version" when the baseline is Opus.
`opus-5` beats `opus-4-8`; `sonnet-5` does not, so it is logged and not pushed. Mode
remaps and retired ids are log-only too. That is the intended trade and it did cost
something: sonnet-5 went from canary to 99 % with only WARNING lines to show for it.
A weaker-family arrival is worth a *look* (is it entitled? worth its own channel?),
not a page — `--json` on the next cycle is where to find it.

| env | meaning |
|-----|---------|
| `QUICK_MODEL_WATCH_INTERVAL` | poll interval in seconds; `0` = off (default 18000 = 5 h) |
| `QUICK_ALERT_WEBHOOK` | chat webhook URL; empty = log only. **Secret — `.env` only, this repo is public** |
| `QUICK_UPGRADE_BASELINE` | the model an alert must beat; defaults to `QUICK_FORCE_MODEL` |
| `QUICK_ALERT_ALL_CHANGES` | `1` to also push non-upgrade changes (noisy; off by default) |

If a push fails the state is not saved, so the next cycle re-detects and retries rather
than losing the alert.

```bash
python -m quick.model_watch --test-notify   # verify the webhook
```

Stage 2 confirms whether *this account* may use a newly seen id — one 1-token
`ConverseStream`, run manually, not on a timer:

```bash
python -m quick.model_watch --probe us.anthropic.claude-opus-5
# → available | denied | unknown_model | error
```

`denied` (IAM explicit deny) means the model exists but the account is not entitled —
recheck occasionally; `unknown_model` (validation error) means the id is not live at
all — stop trying it.

> **Run `--probe` on the host that owns the credentials** (i.e. inside the deployed
> container), never on the mac while the Linux gateway is live: any auth path here
> refreshes Keycloak and rotates the `refresh_token`, which invalidates the copy the
> other host is using. `--probe` needs creds; the plain watch does not.


## Watching the session allowance (`usage_watch.py`)

Amazon Quick meters two buckets — a rolling **session** allowance and a **monthly**
entitlement. The desktop app shows both in its profile flyout ("Session usage 27 %",
the bar turning red at 90 %). Those numbers come from `GET /profile/usage` on the
app's *local* HTTP server, which a headless gateway does not have — and the tenant
DataPlane does not serve that path (verified: 404 `UnknownOperationException`).

They do ride along on **every inference response**, though: the Converse `metadata`
frame carries a `usageSummary` sibling of the event payload (verified live):

```json
{"entitlementStatus": "ALLOWED",
 "overageEnabled": true,
 "sessionUsage": {"resumeInMinutes": 0, "usedPercentage": 38},
 "monthlyUsage": {"availableUnits": 0, "provisionedUnits": 720,
                  "resetsAt": 1788220800, "usedPercentage": 100}}
```

`usedPercentage` is the share **consumed**, so what matters for an alert is
`100 - usedPercentage` — the share still available. `resumeInMinutes` counts down to
the session window's reset, *not* to the end of a lockout: it is set while the
account is still usable (21 % left, `resumeInMinutes` 55, ALLOWED — verified live).

So the watch is nearly free: `streaming.py` hands every decoded frame to
`observe_event()`, which keeps the newest reading in memory **per pool account**
(the request path stamps the account it selected on a `ContextVar`, since the
response itself never says which account it came from). `main.py`'s lifespan
runs `watch_loop()` every `QUICK_SESSION_WATCH_INTERVAL` seconds (default 3600,
`0` disables); a cycle spends **one 1-token request only if** the gateway has been
idle longer than that interval and has nothing fresh to read.

The alert is **edge-triggered with hysteresis** — one message per crossing:

```
⚠️ Quick 会话额度告警：剩余 9%（已用 91%，阈值 剩余 <10%）
月度：已用 100%（overage 开启），2026-09-01 00:00 UTC 重置
恢复到 剩余 ≥10% 后才会再次告警
```

It fires when the remaining share first drops below
`QUICK_SESSION_ALERT_REMAINING_PCT` (default 10 — where the app itself turns its bar
red), then stays silent all the way down to 0 %. It re-arms only once the remaining
share is back at or above the threshold (a new session window), so a long stretch at
3 % costs one message, not one per hour. The armed flag lives in
`~/.quickwork/session-usage-state.json` (`QUICK_SESSION_WATCH_STATE`), so a restart
does not re-fire. A failed push leaves the state armed and the next cycle retries.

Each account gets its own edge-triggered alert, plus two pool-level ones: a
credential that died (`invalid_grant` — re-upload the file and the pool re-enables it
itself), and **every
account unusable**, which is the only alert that means *go add an account*.

```bash
python -m quick.usage_watch              # every account (probes the stale ones)
python -m quick.usage_watch --pool       # the pool table only, no probing
python -m quick.usage_watch --account b  # one account
python -m quick.usage_watch --json       # machine-readable
python -m quick.usage_watch --probe      # force a fresh 1-token probe
# on the box:
docker compose -p quick-gateway exec quick-gateway python -m quick.usage_watch --json
```

| env | meaning |
|-----|---------|
| `QUICK_SESSION_WATCH_INTERVAL` | check interval in seconds; `0` = off (default 3600) |
| `QUICK_SESSION_ALERT_REMAINING_PCT` | alert below this much remaining (default 10) |
| `QUICK_SESSION_PROBE_MODEL` | model for the idle-fallback probe (default haiku-4.5) |
| `QUICK_SESSION_WATCH_STATE` | armed-flag state file |
| `QUICK_ALERT_WEBHOOK` | same webhook as the model watch; empty = log only |

> The CLI needs credentials when it has to probe, so run it **on the host that owns
> them** (the deployed container), like `model_watch --probe`.


## Prompt caching

Client `cache_control` breakpoints are **passed through** as Bedrock `cachePoint`
separators — in `system`, in message content, and after the tool list. Anthropic marks
the block *up to and including which* to cache; Bedrock wants a separator right after
it, so a marked block becomes `block, cachePoint`. The gateway never invents a cache
point the client did not ask for.

Two boundaries, both probed live:

* **At most 4 cache points**, or Bedrock rejects the whole request:
  `ValidationException: A maximum of 4 blocks with cache_control may be provided.
  Found 5.` The converter therefore caps at `MAX_CACHE_POINTS`, dropping the
  *earliest* points — those cover the shortest prefixes and are contained in every
  later one.
* **A prefix below the cacheable minimum is silently ignored** (no error, `write=0`),
  so no size guard is needed.

The response carries `cache_read_input_tokens` / `cache_creation_input_tokens` back to
the client (Bedrock excludes the cached share from `inputTokens`), which is the only
way the win is visible downstream.

### The cache is shared across pool accounts

Measured: a cold request on account `b` wrote 13264 tokens, the same prefix on account
**`default`** then read all 13264 from cache. Both accounts sit on the same tenant
DataPlane and the same AWS account, and the id_token identifies only the Quick *user*,
which the Bedrock-side cache key does not include. **So the pool's per-request account
switching costs nothing in cache hits** — no session affinity is needed.

### What it saves

Measured on 12 requests × 204k tokens each (idle control window confirmed the
concurrent traffic contributed 0 units):

| | units consumed | wall time |
|---|---|---|
| no cache points | **10** (~0.83/request) | 84 s |
| with cache points | **5**, including the initial write | 55 s |

So Quick's monthly *units* track tokens (~245k tokens per unit uncached), a cached read
is billed at roughly 40 % of a full read, and the same workload runs ~35 % faster.
Caching is worth real quota here, not just latency.

## Thinking (extended reasoning)

Quick's models use **adaptive** thinking with an effort level (the app's
Low/Med/High), *not* Anthropic's `enabled` + `budget_tokens` (which errors:
`"thinking.type.enabled" is not supported … Use "thinking.type.adaptive"`). The
converter translates an incoming `thinking: {type: enabled, budget_tokens: N}`
into `additionalModelRequestFields.thinking = {type: adaptive}` plus a top-level
`output_config: {effort: …}`, bucketing the budget: `≤4096` → low, `≤16384` →
medium, else high. The backend returns a signed `thinking` block (reasoning text
is not streamed) followed by the answer — verified in both stream and non-stream.

## How the protocol was recovered

The agent is PyInstaller + PyArmor (machine-bound to this Mac). The bedrock-client
constants were recovered by extracting the archive (`pyinstxtractor-ng`) and
importing the modules under the app's own Python 3.12.8 so PyArmor self-decrypted
them, then reading the code objects' string constants — no proxy or app changes.
