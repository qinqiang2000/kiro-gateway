# Quick Gateway (Amazon Quick backend)

An Anthropic-compatible gateway for **Amazon Quick** (`com.amazon.QuickWork.mac`),
built alongside the Kiro gateway and reusing its conventions. Exposes
`POST /quick/v1/messages` and translates to Quick's tenant DataPlane, which
proxies Amazon Bedrock `Converse` / `ConverseStream`.

**Status: fully working** — text, streaming, and tool-use verified end-to-end
against the live backend.

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
each other. Re-copy only if the offline `refresh_token` fully lapses (~90 days) or you
re-sign-in to Quick on the mac.

### Docker (isolated deploy — the `deploy/quick/` compose)

`deploy/quick/docker-compose.yml` runs quick-gateway as its own compose project on host
port 8000, mounting `./quickwork` → `/home/kiro/.quickwork`. The image runs as uid 999
(`kiro`), so the creds dir/file on the host must be owned by 999 for refresh to persist:

```bash
mkdir -p quickwork && cp ~/.quickwork/gateway-creds.json quickwork/ && chown -R 999:999 quickwork
docker compose -p quick-gateway up -d --build
```

`REFRESH_TOKEN=dummy` in that compose only satisfies `main.py`'s startup credential check
(the Kiro `/v1/*` routes are inert in this container); quick-gateway needs no Kiro creds.

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
"payload": {…}}` (unwrapped in `streaming.py`). The `metadata` event also carries
a `usageSummary` (entitlement / monthly quota) which the gateway ignores.

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
| `routes.py` | FastAPI `/quick/v1/messages` |

## Models

Authorized for this account (verified live — real completions):

- Opus: `us.anthropic.claude-opus-4-8`, `us.anthropic.claude-opus-4-6-v1`
- Sonnet: `us.anthropic.claude-sonnet-4-6`
- Haiku: `us.anthropic.claude-haiku-4-5-20251001-v1:0`

`us.anthropic.claude-opus-5` **exists on the backend but this account is
IAM-denied** (explicit deny on `InvokeModelWithResponseStream`) — it returns HTTP
200 with an in-stream `error` frame, not a completion. So opus-4-8 is the best
usable Opus.

**Forcing** — by default the gateway forces *every* request to a single model
(`QUICK_FORCE_MODEL`, default `us.anthropic.claude-opus-4-8`), ignoring whatever
the client picked. Set `QUICK_FORCE_MODEL=` (empty) in `.env` to disable and use
per-request mapping instead: `resolve_model()` then maps any Claude name to a
Quick id by exact id/alias, else by family (Opus→opus-4-8, Sonnet→sonnet-4-6,
Haiku→haiku-4-5). The client's `/model` menu (Opus 5, Sonnet 5, …) is its own
list and is *not* served by this gateway.

Backend failures arrive as an in-stream `{"eventType":"error"}` frame under HTTP
200; the gateway surfaces these as real errors (403 for IAM denies, else 502).

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
