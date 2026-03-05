# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Kiro Gateway is a Python FastAPI proxy that exposes OpenAI-compatible (`/v1/chat/completions`) and Anthropic-compatible (`/v1/messages`) APIs, translating requests to the Kiro API (AWS CodeWhisperer). It handles auth, streaming, model resolution, and format conversion.

## Commands

```bash
# Run server
python main.py                    # default 0.0.0.0:8000
python main.py --port 9000        # custom port

# Install deps
pip install -r requirements.txt

# Run all tests
pytest -v

# Run unit or integration tests
pytest tests/unit/ -v
pytest tests/integration/ -v

# Run a single test
pytest tests/unit/test_config.py::TestClassName::test_name -v

# Stop on first failure
pytest -x

# Coverage
pytest --cov=kiro --cov-report=html
```

## Architecture

Request flow: Client → Routes → Converters → Model Resolver → HTTP Client → Kiro Auth → Kiro API → Streaming → Client

The codebase is split into parallel OpenAI/Anthropic pipelines that share core logic:

- **Routes** (`routes_openai.py`, `routes_anthropic.py`): FastAPI endpoints, auth validation, dispatch to converters/streaming
- **Converters** (`converters_openai.py`, `converters_anthropic.py`, `converters_core.py`): Transform client requests into Kiro API format. Core module defines `UnifiedMessage` as the shared intermediate representation
- **Streaming** (`streaming_openai.py`, `streaming_anthropic.py`, `streaming_core.py`): Parse AWS event streams back into OpenAI SSE or Anthropic SSE format
- **Model Resolver** (`model_resolver.py`): 4-layer pipeline — normalize name → check dynamic cache → check hidden models → pass-through. Gateway philosophy: let Kiro be the final arbiter for unknown models
- **Auth** (`auth.py`): `KiroAuthManager` with auto-detection across 4 auth methods (JSON creds file, env var refresh token, SQLite DB, AWS SSO OIDC). Thread-safe token refresh via asyncio.Lock
- **HTTP Client** (`http_client.py`): Retry logic with auto token refresh on 403, exponential backoff on 429/5xx
- **Thinking Parser** (`thinking_parser.py`): FSM-based parser that extracts `<thinking>` blocks from streamed responses

Entry point is `main.py` which wires up FastAPI, routes, middleware, and loguru logging.

## Key Conventions

- **Commits**: Conventional Commits format — `feat(scope):`, `fix(scope):`, `refactor(scope):`, etc.
- **Type hints mandatory** on all function parameters and return values
- **Docstrings**: Google style with Args/Returns/Raises
- **Logging**: loguru (`from loguru import logger`), not stdlib logging
- **Async**: All I/O operations use async/await
- **Error handling**: Catch specific exceptions, never bare `except:`. Errors should be user-friendly and actionable
- **Config**: Centralized in `kiro/config.py`, loaded from `.env` via python-dotenv

## Testing

- Framework: pytest + pytest-asyncio + hypothesis
- **All tests are network-isolated**: `block_all_network_calls` fixture in `tests/conftest.py` blocks all real httpx requests. Every external call must be mocked
- Tests follow Arrange-Act-Assert pattern
- Test classes named `Test*Success`, `Test*Errors`, `Test*EdgeCases`
- Test functions named `test_<what>_<expected_result>`

## Gotchas

- **Streaming requires per-request HTTP clients** — reusing a shared `httpx.AsyncClient` for streaming causes CLOSE_WAIT socket leaks. Always use `async with httpx.AsyncClient() as client:` for streaming requests
- **Kiro's "Improperly formed request" error** is vague and can mean many different validation issues (role order, tool schemas, content format, etc.). Requires systematic debugging
- **Model name normalization** converts client formats to Kiro format: dashes to dots for minor versions, strips date suffixes (e.g., `claude-haiku-4-5-20251001` → `claude-haiku-4.5`)
- **Tool calls from Kiro** may arrive in bracket format `[{...}]` instead of proper JSON — the parser handles this
- **Debug logging** has three modes configured via `DEBUG_MODE` env var: `off` (default), `errors` (4xx/5xx only), `all`

## Project Philosophy

This is a transparent proxy with minimal intervention. Fix API-level quirks, don't modify user content. Build systems over patches. See `AGENTS.md` for full guidelines.
