# -*- coding: utf-8 -*-

"""Unit tests for the Quick gateway conversion + streaming layers.

Network-isolated (no DataPlane / Keychain access): these exercise the pure
transform functions only. Follows the repo's Arrange-Act-Assert + Test*Success
conventions.
"""

import json
import struct

import pytest

import quick.config
from quick.config import resolve_model
from quick.converters import anthropic_to_converse
from quick.streaming import (
    ConverseAggregator,
    ConverseToAnthropicSSE,
    EventStreamDecoder,
    converse_response_to_anthropic,
)


def _frame(event_type: str, payload: dict) -> bytes:
    """Build one AWS event-stream frame (CRCs zeroed; the decoder ignores them)."""
    hdrs = b""
    for name, val in [(":event-type", event_type), (":message-type", "event")]:
        nb = name.encode()
        vb = val.encode()
        hdrs += bytes([len(nb)]) + nb + b"\x07" + struct.pack(">H", len(vb)) + vb
    pb = json.dumps(payload).encode()
    total = 12 + len(hdrs) + len(pb) + 4
    return struct.pack(">II", total, len(hdrs)) + struct.pack(">I", 0) + hdrs + pb + struct.pack(">I", 0)


def _quick_frame(event_type: str, payload: dict) -> bytes:
    """Build a Quick-wrapped frame: header ``bedrockStreamEvent``, payload {eventType,payload}."""
    return _frame("bedrockStreamEvent", {"eventType": event_type, "payload": payload})


@pytest.fixture
def no_force(monkeypatch):
    """Disable the global force-model override so mapping logic can be tested."""
    monkeypatch.setattr(quick.config, "QUICK_FORCE_MODEL", "")


class TestForceModel:
    def test_force_overrides_everything(self, monkeypatch):
        monkeypatch.setattr(quick.config, "QUICK_FORCE_MODEL", "us.anthropic.claude-opus-4-8")
        assert resolve_model("claude-sonnet-4-6") == "us.anthropic.claude-opus-4-8"
        assert resolve_model("anything") == "us.anthropic.claude-opus-4-8"
        assert resolve_model(None) == "us.anthropic.claude-opus-4-8"

    def test_default_force_is_opus_4_8(self):
        # By default the gateway forces every request to Opus 4.8 (opus-5 is IAM-denied).
        assert resolve_model("claude-sonnet-4-6") == "us.anthropic.claude-opus-4-8"


@pytest.mark.usefixtures("no_force")
class TestModelResolutionSuccess:
    def test_alias_and_mode_resolution(self):
        assert resolve_model("balanced") == "us.anthropic.claude-sonnet-4-6"
        assert resolve_model("smart") == "us.anthropic.claude-opus-4-6-v1"
        assert resolve_model("fast").startswith("us.anthropic.claude-haiku-4-5")

    def test_family_fallback_for_client_picked_names(self):
        # Claude Code sends its own names (e.g. "claude-opus-5"); map by family.
        assert resolve_model("claude-opus-5") == "us.anthropic.claude-opus-4-8"  # opus family -> 4-8
        assert resolve_model("Opus") == "us.anthropic.claude-opus-4-8"
        assert resolve_model("claude-sonnet-5") == "us.anthropic.claude-sonnet-4-6"
        assert resolve_model("claude-haiku-9000").startswith("us.anthropic.claude-haiku-4-5")

    def test_exact_alias_wins_over_family(self):
        assert resolve_model("claude-opus-4-6") == "us.anthropic.claude-opus-4-6-v1"

    def test_passthrough_non_claude_model(self):
        assert resolve_model("some.future.model") == "some.future.model"


@pytest.mark.usefixtures("no_force")
class TestConverterSuccess:
    def test_system_inference_and_tools(self):
        req = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 100,
            "temperature": 0.7,
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "calc", "description": "c", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "auto"},
        }
        out = anthropic_to_converse(req)
        assert out["modelId"] == "us.anthropic.claude-sonnet-4-6"
        assert out["system"] == [{"text": "You are helpful."}]
        assert out["inferenceConfig"] == {"maxTokens": 100, "temperature": 0.7}
        assert out["toolConfig"]["tools"][0]["toolSpec"]["name"] == "calc"
        assert out["toolConfig"]["toolChoice"] == {"auto": {}}

    def test_tool_use_and_result_blocks(self):
        req = {
            "model": "x",
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "calc", "input": {"x": 1}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "42", "is_error": False}]},
            ],
        }
        out = anthropic_to_converse(req)
        assert out["messages"][0]["content"][0]["toolUse"]["toolUseId"] == "t1"
        tr = out["messages"][1]["content"][0]["toolResult"]
        assert tr["toolUseId"] == "t1"
        assert tr["content"] == [{"text": "42"}]

    def test_thinking_maps_to_adaptive_plus_effort(self):
        # Anthropic enabled+budget_tokens -> Quick adaptive + output_config.effort.
        out = anthropic_to_converse({
            "model": "x", "max_tokens": 3000,
            "thinking": {"type": "enabled", "budget_tokens": 2048},
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert out["additionalModelRequestFields"]["thinking"] == {"type": "adaptive"}
        assert out["_quick_output_config"] == {"effort": "low"}

    def test_thinking_budget_buckets(self):
        from quick.converters import _budget_to_effort
        assert _budget_to_effort(1024) == "low"
        assert _budget_to_effort(8000) == "medium"
        assert _budget_to_effort(32000) == "high"
        assert _budget_to_effort(None) == "medium"

    def test_consecutive_same_role_merged(self):
        req = {"model": "x", "messages": [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
        ]}
        out = anthropic_to_converse(req)
        assert len(out["messages"]) == 1
        assert out["messages"][0]["content"] == [{"text": "a"}, {"text": "b"}]


class TestStreamingSuccess:
    def test_decode_and_translate_across_split_chunks(self):
        stream = b"".join([
            _frame("messageStart", {"role": "assistant"}),
            _frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Hello"}}),
            _frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": " world"}}),
            _frame("contentBlockStop", {"contentBlockIndex": 0}),
            _frame("messageStop", {"stopReason": "end_turn"}),
            _frame("metadata", {"usage": {"inputTokens": 5, "outputTokens": 2}}),
        ])
        dec = EventStreamDecoder()
        tr = ConverseToAnthropicSSE("msg_x", "m")
        out = [tr.start()]
        for i in range(0, len(stream), 3):  # awkward 3-byte chunks
            for ev in dec.feed(stream[i:i + 3]):
                out += tr.handle(ev)
        out += tr.finish()
        joined = "".join(out)
        assert "text_delta" in joined and "Hello" in joined and "world" in joined
        assert "message_stop" in joined and "end_turn" in joined
        assert '"output_tokens": 2' in joined

    def test_tool_use_streaming(self):
        stream = b"".join([
            _frame("contentBlockStart", {"contentBlockIndex": 0,
                                         "start": {"toolUse": {"toolUseId": "t1", "name": "calc"}}}),
            _frame("contentBlockDelta", {"contentBlockIndex": 0,
                                         "delta": {"toolUse": {"input": '{"x":'}}}),
            _frame("contentBlockDelta", {"contentBlockIndex": 0,
                                         "delta": {"toolUse": {"input": "1}"}}}),
            _frame("contentBlockStop", {"contentBlockIndex": 0}),
            _frame("messageStop", {"stopReason": "tool_use"}),
        ])
        dec = EventStreamDecoder()
        tr = ConverseToAnthropicSSE("msg_x", "m")
        out = [tr.start()]
        for ev in dec.feed(stream):
            out += tr.handle(ev)
        out += tr.finish()
        joined = "".join(out)
        assert '"type": "tool_use"' in joined and "calc" in joined
        assert "input_json_delta" in joined
        assert "tool_use" in joined  # stop_reason


class TestQuickWrappedStreamSuccess:
    """The real Quick wire format: frames typed ``bedrockStreamEvent``."""

    def test_wrapped_events_translate_to_anthropic_sse(self):
        stream = b"".join([
            _quick_frame("messageStart", {"role": "assistant"}),
            _quick_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "hello"}}),
            _quick_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": " world"}}),
            _quick_frame("contentBlockStop", {"contentBlockIndex": 0}),
            _quick_frame("messageStop", {"stopReason": "end_turn"}),
            _quick_frame("metadata", {"usage": {"inputTokens": 13, "outputTokens": 5}}),
        ])
        dec = EventStreamDecoder()
        tr = ConverseToAnthropicSSE("msg_x", "m")
        out = [tr.start()]
        for ev in dec.feed(stream):
            out += tr.handle(ev)
        out += tr.finish()
        joined = "".join(out)
        assert "text_delta" in joined and "hello" in joined and "world" in joined
        assert '"output_tokens": 5' in joined

    def test_aggregator_builds_message_from_wrapped_stream(self):
        stream = b"".join([
            _quick_frame("messageStart", {"role": "assistant"}),
            _quick_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "hi there"}}),
            _quick_frame("contentBlockStop", {"contentBlockIndex": 0}),
            _quick_frame("messageStop", {"stopReason": "end_turn"}),
            _quick_frame("metadata", {"usage": {"inputTokens": 14, "outputTokens": 6}}),
        ])
        agg = ConverseAggregator("msg_1", "m")
        dec = EventStreamDecoder()
        for ev in dec.feed(stream):
            agg.add(ev)
        msg = agg.result()
        assert msg["content"] == [{"type": "text", "text": "hi there"}]
        assert msg["stop_reason"] == "end_turn"
        assert msg["usage"] == {"input_tokens": 14, "output_tokens": 6}

    def test_aggregator_assembles_tool_use(self):
        stream = b"".join([
            _quick_frame("contentBlockStart", {"contentBlockIndex": 0,
                                               "start": {"toolUse": {"toolUseId": "t1", "name": "get_weather"}}}),
            _quick_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"city":'}}}),
            _quick_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '"Paris"}'}}}),
            _quick_frame("contentBlockStop", {"contentBlockIndex": 0}),
            _quick_frame("messageStop", {"stopReason": "tool_use"}),
        ])
        agg = ConverseAggregator("msg_1", "m")
        dec = EventStreamDecoder()
        for ev in dec.feed(stream):
            agg.add(ev)
        msg = agg.result()
        assert msg["stop_reason"] == "tool_use"
        assert msg["content"][0] == {"type": "tool_use", "id": "t1", "name": "get_weather",
                                     "input": {"city": "Paris"}}


class TestNonStreamingSuccess:
    def test_converse_response_mapping(self):
        resp = {
            "output": {"message": {"role": "assistant", "content": [
                {"text": "hi"},
                {"toolUse": {"toolUseId": "t1", "name": "calc", "input": {"x": 1}}},
            ]}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": 3, "outputTokens": 4},
        }
        out = converse_response_to_anthropic(resp, "msg_1", "m")
        assert out["content"][0] == {"type": "text", "text": "hi"}
        assert out["content"][1]["type"] == "tool_use"
        assert out["stop_reason"] == "tool_use"
        assert out["usage"] == {"input_tokens": 3, "output_tokens": 4}


class _FakeResponse:
    """Minimal httpx-like response for mocking Keycloak refresh."""

    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def _sample_blob() -> dict:
    """A credential blob shaped like the real Keychain value (far-future expiries)."""
    return {
        "access_token": "AT0",
        "id_token": "ID0",
        "refresh_token": "RT0",
        "access_token_expiry": 9_999_999_999.0,
        "id_token_expiry": 9_999_999_999.0,
        "token_endpoint": "https://quick.example/realms/quick/protocol/openid-connect/token",
        "client_id": "quick-desktop",
        "tenant_url": "https://qbs-abc.dp.appintegrations.us-east-1.prod.plato.ai.aws.dev",
        "region": "us-east-1",
        "user_arn": "arn:aws:quicksight:us-east-1:1:user/x",
    }


@pytest.fixture
def creds_file(tmp_path, monkeypatch):
    """Redirect QUICK_CREDS_FILE (in config AND already-imported auth) to a tmp path."""
    import quick.auth as qa
    p = tmp_path / "gateway-creds.json"
    monkeypatch.setattr(quick.config, "QUICK_CREDS_FILE", p)
    monkeypatch.setattr(qa, "QUICK_CREDS_FILE", p)
    # Neutralize the tenant-url env override so blob values round-trip verbatim.
    monkeypatch.setattr(qa, "QUICK_TENANT_URL", "")
    return p


class TestAuthLoadSuccess:
    def test_cache_file_present_skips_keychain(self, creds_file, monkeypatch):
        # Arrange: a valid cache file exists.
        creds_file.write_text(json.dumps(_sample_blob()), encoding="utf-8")
        import quick.auth as qa
        called = {"security": False}

        def _boom(*a, **k):
            called["security"] = True
            raise AssertionError("Keychain must not be read when cache file exists")

        monkeypatch.setattr(qa.subprocess, "check_output", _boom)
        # Act
        mgr = qa.QuickAuthManager()
        creds = mgr._load()
        # Assert
        assert creds.id_token == "ID0"
        assert creds.tenant_url.startswith("https://qbs-abc")
        assert called["security"] is False

    def test_keychain_bootstrap_writes_file_0600(self, creds_file, monkeypatch):
        # Arrange: no cache file; security CLI "present"; keychain returns the blob.
        assert not creds_file.exists()
        import quick.auth as qa
        monkeypatch.setattr(qa.shutil, "which", lambda _n: "/usr/bin/security")
        monkeypatch.setattr(qa, "_resolve_profile_id", lambda: "enterprise-x")
        reads = {"n": 0}

        def _fake_check_output(*a, **k):
            reads["n"] += 1
            return json.dumps(_sample_blob()).encode()

        monkeypatch.setattr(qa.subprocess, "check_output", _fake_check_output)
        # Act
        mgr = qa.QuickAuthManager()
        creds = mgr._load()
        # Assert: keychain read exactly once, file written with 0600 + correct content.
        assert reads["n"] == 1
        assert creds_file.exists()
        assert (creds_file.stat().st_mode & 0o777) == 0o600
        on_disk = json.loads(creds_file.read_text(encoding="utf-8"))
        assert on_disk["refresh_token"] == "RT0"
        assert on_disk["tenant_url"].startswith("https://qbs-abc")


class TestAuthLoadErrors:
    def test_no_file_no_security_raises_actionable(self, creds_file, monkeypatch):
        # Arrange: simulate Linux — no cache file, no `security` CLI.
        import quick.auth as qa
        monkeypatch.setattr(qa.shutil, "which", lambda _n: None)
        # Act / Assert
        with pytest.raises(qa.QuickAuthError) as exc:
            qa.QuickAuthManager()._load()
        assert str(creds_file) in str(exc.value)

    def test_corrupt_cache_file_raises(self, creds_file):
        creds_file.write_text("{not json", encoding="utf-8")
        import quick.auth as qa
        with pytest.raises(qa.QuickAuthError):
            qa.QuickAuthManager()._load()


class TestAuthRefresh:
    @pytest.mark.asyncio
    async def test_refresh_persists_rotated_tokens(self, creds_file, monkeypatch):
        # Arrange: seed cache with an EXPIRED id_token so get_credentials refreshes.
        blob = _sample_blob()
        blob["id_token_expiry"] = 0.0  # force is_expired() True
        creds_file.write_text(json.dumps(blob), encoding="utf-8")
        import quick.auth as qa

        rotated = {
            "access_token": "AT1",
            "id_token": "ID1",
            "refresh_token": "RT1",
            "expires_in": 300,
        }

        class _FakeAsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _FakeResponse(200, rotated)

        monkeypatch.setattr(qa.httpx, "AsyncClient", _FakeAsyncClient)
        # Act
        mgr = qa.QuickAuthManager()
        creds = await mgr.get_credentials()
        # Assert: in-memory + on-disk both hold the rotated values.
        assert creds.id_token == "ID1"
        assert creds.refresh_token == "RT1"
        on_disk = json.loads(creds_file.read_text(encoding="utf-8"))
        assert on_disk["refresh_token"] == "RT1"
        assert on_disk["id_token"] == "ID1"


class TestKeepalive:
    @pytest.mark.asyncio
    async def test_keepalive_forces_refresh_even_when_token_valid(self, creds_file, monkeypatch):
        # Arrange: seed cache with a NON-expired id_token (get_credentials would NOT refresh).
        creds_file.write_text(json.dumps(_sample_blob()), encoding="utf-8")
        import quick.auth as qa

        rotated = {"access_token": "AT2", "id_token": "ID2", "refresh_token": "RT2", "expires_in": 300}

        class _FakeAsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _FakeResponse(200, rotated)

        monkeypatch.setattr(qa.httpx, "AsyncClient", _FakeAsyncClient)
        # Act: keepalive must refresh unconditionally (unlike get_credentials).
        mgr = qa.QuickAuthManager()
        await mgr.keepalive()
        # Assert: rotated + persisted despite the seed token being valid.
        on_disk = json.loads(creds_file.read_text(encoding="utf-8"))
        assert on_disk["refresh_token"] == "RT2"
        assert on_disk["id_token"] == "ID2"

    @pytest.mark.asyncio
    async def test_keepalive_loop_disabled_returns_immediately(self, monkeypatch):
        # Arrange: interval 0 disables the loop; it must return without sleeping/refreshing.
        import quick.auth as qa
        monkeypatch.setattr(qa, "QUICK_KEEPALIVE_INTERVAL", 0)

        async def _boom():
            raise AssertionError("keepalive must not run when disabled")

        monkeypatch.setattr(qa.quick_auth_manager, "keepalive", _boom)
        # Act / Assert: returns promptly, no exception.
        await qa.keepalive_loop()


class TestToolDescriptionFallback:
    def test_empty_tool_description_falls_back_to_name(self):
        # Bedrock rejects toolSpec.description length 0; Anthropic allows empty.
        payload = {
            "model": "claude-opus-4-8",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"name": "web_search", "description": "", "input_schema": {"type": "object"}},
                {"name": "no_desc_key", "input_schema": {"type": "object"}},
                {"name": "spaces_only", "description": "   ", "input_schema": {"type": "object"}},
                {"name": "kept", "description": "real description", "input_schema": {"type": "object"}},
            ],
        }
        out = anthropic_to_converse(payload)
        specs = {t["toolSpec"]["name"]: t["toolSpec"]["description"] for t in out["toolConfig"]["tools"]}
        # Empty / missing / whitespace-only -> tool name (non-empty, satisfies Bedrock).
        assert specs["web_search"] == "web_search"
        assert specs["no_desc_key"] == "no_desc_key"
        assert specs["spaces_only"] == "spaces_only"
        # Real descriptions are preserved verbatim.
        assert specs["kept"] == "real description"
        # No description is ever the empty string.
        assert all(len(d) >= 1 for d in specs.values())


class TestWebSearchConversion:
    def test_native_web_search_gets_query_schema(self):
        # Native server tool: has `type`, no input_schema. Must get a {query} schema.
        payload = {
            "model": "claude-opus-4-8", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }
        out = anthropic_to_converse(payload)
        spec = out["toolConfig"]["tools"][0]["toolSpec"]
        assert spec["name"] == "web_search"
        schema = spec["inputSchema"]["json"]
        assert schema["properties"]["query"]["type"] == "string"
        assert schema["required"] == ["query"]
        assert len(spec["description"]) >= 1


class TestWebSearchLoop:
    @pytest.mark.asyncio
    async def test_loop_executes_search_and_feeds_back(self, monkeypatch):
        import quick.agent_loop as al

        # Round 1: model asks for web_search. Round 2: model answers with text.
        rounds = [
            {"content": [{"type": "tool_use", "id": "t1", "name": "web_search",
                          "input": {"query": "Anthropic news"}}], "stop_reason": "tool_use"},
            {"content": [{"type": "text", "text": "Anthropic released X."}], "stop_reason": "end_turn"},
        ]
        calls = {"n": 0, "searched": []}

        class _FakeAgg:
            def __init__(self, *a): self.error = None; self._i = calls["n"]
            def result(self): return rounds[self._i]

        async def _fake_round(ci, mid, model):
            agg = _FakeAgg()
            calls["n"] += 1
            return agg

        async def _fake_search(query, max_results=6):
            calls["searched"].append(query)
            return [{"title": "T", "url": "https://x", "snippet": "S"}]

        monkeypatch.setattr(al, "_run_one_round", _fake_round)
        monkeypatch.setattr(al, "search_web", _fake_search)

        ci = {"messages": [{"role": "user", "content": [{"text": "what's new?"}]}]}
        final, err = await al.run_websearch_loop(ci, "msg_1", "m")
        assert err is None
        assert final["content"][0]["text"] == "Anthropic released X."
        assert calls["searched"] == ["Anthropic news"]        # search actually ran
        # transcript grew by assistant(tool_use)+user(toolResult)
        assert any(m["role"] == "user" and "toolResult" in m["content"][0] for m in ci["messages"])

    @pytest.mark.asyncio
    async def test_loop_returns_non_websearch_tool_use_immediately(self, monkeypatch):
        import quick.agent_loop as al
        # Model calls a DIFFERENT tool → loop must return it as-is (client handles it).
        msg = {"content": [{"type": "tool_use", "id": "t9", "name": "get_weather",
                            "input": {"city": "SF"}}], "stop_reason": "tool_use"}

        class _FakeAgg:
            error = None
            def result(self): return msg

        async def _fake_round(ci, mid, model): return _FakeAgg()
        searched = []
        async def _fake_search(q, max_results=6): searched.append(q); return []
        monkeypatch.setattr(al, "_run_one_round", _fake_round)
        monkeypatch.setattr(al, "search_web", _fake_search)

        final, err = await al.run_websearch_loop({"messages": []}, "msg_1", "m")
        assert err is None
        assert final["content"][0]["name"] == "get_weather"
        assert searched == []   # never executed a search
