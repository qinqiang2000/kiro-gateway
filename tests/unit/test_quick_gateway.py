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

        async def _fake_round(ci, mid, model, account=None):
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

        async def _fake_round(ci, mid, model, account=None): return _FakeAgg()
        searched = []
        async def _fake_search(q, max_results=6): searched.append(q); return []
        monkeypatch.setattr(al, "_run_one_round", _fake_round)
        monkeypatch.setattr(al, "search_web", _fake_search)

        final, err = await al.run_websearch_loop({"messages": []}, "msg_1", "m")
        assert err is None
        assert final["content"][0]["name"] == "get_weather"
        assert searched == []   # never executed a search


class TestImageBlock:
    def test_image_source_is_json_serializable_base64_string(self):
        import base64 as _b64, json as _json
        # 1x1 transparent PNG
        png_b64 = _b64.b64encode(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f8b0000000049454e44ae426082"
        )).decode()
        payload = {
            "model": "claude-opus-4-8", "max_tokens": 16,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image", "source": {"type": "base64",
                                              "media_type": "image/png", "data": png_b64}},
            ]}],
        }
        out = anthropic_to_converse(payload)
        # Must serialize without a bytes TypeError (the original bug).
        blob = _json.dumps({"modelId": out["modelId"], "requestBody": out})
        assert "image" in blob
        # Find the image block; its source.bytes must be the base64 STRING.
        content = out["messages"][0]["content"]
        img = next(b for b in content if "image" in b)["image"]
        assert img["format"] == "png"
        assert img["source"]["bytes"] == png_b64
        assert isinstance(img["source"]["bytes"], str)

    def test_jpg_normalized_and_bad_base64_dropped(self):
        # jpg -> jpeg; invalid base64 -> block dropped (no crash).
        payload = {
            "model": "m", "max_tokens": 8,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpg",
                                             "data": "not!!base64"}},
            ]}],
        }
        out = anthropic_to_converse(payload)
        content = out["messages"][0]["content"]
        assert not any("image" in b for b in content)  # bad base64 dropped


# ==================================================================================================
# Model watch (public remote-config registry)
# ==================================================================================================

_SAMPLE_CONFIG = {
    "config_version": 0,
    "models": {
        "default": {
            "fast": {"model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0"},
            "smart": {"model_id": "us.anthropic.claude-opus-4-6-v1"},
        }
    },
    "config-priority-rule-list": [
        {
            "condition": {"regions": ["us-west-2"]},
            "configs": [
                {"percentage": 50, "controlVariant": True,
                 "value": {"models": {"default": {"smart": {"model_id": "us.anthropic.claude-opus-4-8"}}}}},
                {"percentage": 50,
                 "value": {"models": {"default": {"smart": {"model_id": "us.anthropic.claude-opus-5"}}}}},
            ],
        },
        {"condition": {}, "configs": [{"percentage": 100, "value": {"quotas": {"a": 1}}}]},
    ],
}


class TestModelWatchSuccess:
    """extract_mapping / diff_mapping over the public Quick remote config."""

    def test_extract_mapping_covers_base_and_rollout_variants(self):
        from quick.model_watch import extract_mapping

        mapping = extract_mapping(_SAMPLE_CONFIG)

        assert mapping["base/default/smart"] == "us.anthropic.claude-opus-4-6-v1"
        # Each rollout bucket is its own scope, so a canary stays visible.
        assert "us.anthropic.claude-opus-5" in mapping.values()
        assert sum(1 for v in mapping.values() if v == "us.anthropic.claude-opus-4-8") == 1

    def test_extract_mapping_ignores_configs_without_models(self):
        from quick.model_watch import extract_mapping

        mapping = extract_mapping(_SAMPLE_CONFIG)

        assert not any(k.startswith("rule1") for k in mapping)

    def test_diff_reports_new_model_ids_and_changed_modes(self):
        from quick.model_watch import diff_mapping

        previous = {"base/default/smart": "us.anthropic.claude-opus-4-6-v1"}
        current = {"base/default/smart": "us.anthropic.claude-opus-5"}

        report = diff_mapping(previous, current)

        assert report["new_models"] == ["us.anthropic.claude-opus-5"]
        assert report["changed"]["base/default/smart"] == [
            "us.anthropic.claude-opus-4-6-v1",
            "us.anthropic.claude-opus-5",
        ]
        assert "us.anthropic.claude-opus-5" in report["unknown_to_gateway"]

    def test_diff_of_identical_mappings_is_empty(self):
        from quick.model_watch import diff_mapping

        mapping = {"base/default/smart": "us.anthropic.claude-opus-4-8"}

        report = diff_mapping(mapping, dict(mapping))

        assert not report["new_models"]
        assert not report["changed"]
        assert not report["added"] and not report["removed"]
        # opus-4-8 is a known gateway model, so nothing is flagged as unknown.
        assert report["unknown_to_gateway"] == []


class TestModelRankSuccess:
    """Version/family ranking that decides what counts as an upgrade."""

    @pytest.mark.parametrize("model_id,expected", [
        ("us.anthropic.claude-opus-4-8", (2, (4, 8))),
        ("us.anthropic.claude-opus-5", (2, (5, 0))),
        ("global.anthropic.claude-haiku-4-5-20251001-v1:0", (0, (4, 5))),
        ("us.anthropic.claude-sonnet-4-6", (1, (4, 6))),
        ("us.anthropic.claude-opus-5-1", (2, (5, 1))),
        ("us.anthropic.claude-opus-5-v1", (2, (5, 0))),
        # A trailing date stamp is not a minor version.
        ("us.anthropic.claude-opus-5-20260901-v1:0", (2, (5, 0))),
    ])
    def test_rank_parses_family_and_version(self, model_id, expected):
        from quick.model_watch import model_rank

        assert model_rank(model_id) == expected

    def test_rank_of_non_claude_id_is_none(self):
        from quick.model_watch import model_rank

        assert model_rank("us.amazon.nova-lite-v1:0") is None

    @pytest.mark.parametrize("candidate,expected", [
        ("us.anthropic.claude-opus-5", True),         # newer Opus wins
        ("us.anthropic.claude-opus-4-10", True),      # minor compared numerically, not as text
        ("us.anthropic.claude-opus-4-6-v1", False),   # older Opus
        ("us.anthropic.claude-sonnet-5", False),      # newer but weaker family
        ("us.amazon.nova-lite-v1:0", False),          # unparsable
    ])
    def test_is_upgrade_against_opus_4_8(self, candidate, expected):
        from quick.model_watch import is_upgrade

        assert is_upgrade(candidate, "us.anthropic.claude-opus-4-8") is expected

    @pytest.mark.parametrize("baseline,candidate,expected", [
        ("us.anthropic.claude-opus-5", "us.anthropic.claude-opus-5-1", True),
        ("us.anthropic.claude-opus-5-1", "us.anthropic.claude-opus-5-2", True),
        ("us.anthropic.claude-opus-5-2", "us.anthropic.claude-opus-5-10", True),
        ("us.anthropic.claude-opus-5-2", "us.anthropic.claude-opus-5-1", False),
        # A dated baseline must not swallow a real minor bump.
        ("us.anthropic.claude-opus-5-20260901-v1:0", "us.anthropic.claude-opus-5-1", True),
    ])
    def test_minor_versions_chain_forward(self, baseline, candidate, expected):
        from quick.model_watch import is_upgrade

        assert is_upgrade(candidate, baseline) is expected


class TestAlertFormatting:
    """What gets pushed to chat, and how short it is."""

    def test_upgrade_message_is_three_short_lines(self):
        from quick.model_watch import format_alert

        report = {"upgrades": ["us.anthropic.claude-opus-5"],
                  "new_models": ["us.anthropic.claude-opus-5"],
                  "baseline": "us.anthropic.claude-opus-4-8",
                  "all_models": ["us.anthropic.claude-opus-5"]}
        mapping = {"rule0[regions=us-west-2]/cfg0(50%)/default/smart": "us.anthropic.claude-opus-5",
                   "rule0[regions=us-west-2]/cfg0(50%)/default/smart.thinking": "us.anthropic.claude-opus-5"}

        text = format_alert(report, mapping)

        assert text.startswith("🚀")
        assert len(text.splitlines()) == 3  # one location only — the rest is noise
        assert "--probe us.anthropic.claude-opus-5" in text

    def test_only_an_opus_class_upgrade_is_pushed(self):
        from quick.model_watch import should_alert

        # A newer Sonnet while the baseline is Opus: logged, not pushed.
        assert should_alert({"upgrades": [], "new_models": ["us.anthropic.claude-sonnet-5"]}) is False
        assert should_alert({"upgrades": [], "changed": {"base/default/smart": ["a", "b"]}}) is False
        assert should_alert({"upgrades": ["us.anthropic.claude-opus-5"]}) is True

    def test_long_region_lists_are_compressed_in_chat_only(self):
        from quick.model_watch import _short_scope

        scope = "rule1[regions=us-west-2,eu-west-1,eu-central-1,eu-west-2]/cfg1(50%)/default/smart"

        assert _short_scope(scope) == "rule1[regions=us-west-2+3]/cfg1(50%)/default/smart"


class TestAlertDeliveryErrors:
    """A 2xx that the robot actually rejected must not count as delivered."""

    def _resp(self, body: str, content_type: str = "application/json"):
        import httpx

        return httpx.Response(200, text=body, headers={"content-type": content_type})

    def test_yunzhijia_failure_body_is_not_success(self):
        from quick.model_watch import _accepted

        body = '{"data":null,"error":"invalid token","errorCode":10001,"success":false}'

        assert _accepted(self._resp(body)) is False

    def test_yunzhijia_success_body_is_success(self):
        from quick.model_watch import _accepted

        body = '{"data":{"msgId":"BOT-1"},"error":null,"errorCode":0,"success":true}'

        assert _accepted(self._resp(body)) is True

    def test_non_json_body_is_taken_at_face_value(self):
        from quick.model_watch import _accepted

        assert _accepted(self._resp("ok", "text/plain")) is True


# ==================================================================================================
# Session-usage watch (quick/usage_watch.py)
# ==================================================================================================

def _usage_summary(session_used, monthly_used=100, resume=0, status="ALLOWED"):
    """Build a usageSummary exactly as the live DataPlane returns it."""
    return {
        "entitlementStatus": status,
        "overageEnabled": True,
        "sessionUsage": {"resumeInMinutes": resume, "usedPercentage": session_used},
        "monthlyUsage": {"availableUnits": 0, "provisionedUnits": 720,
                         "resetsAt": 1788220800, "usedPercentage": monthly_used},
    }


@pytest.fixture
def usage_state(tmp_path, monkeypatch):
    """Point the watch at a temp state file and clear the in-process reading."""
    import quick.usage_watch as uw

    monkeypatch.setattr(uw, "QUICK_SESSION_WATCH_STATE", tmp_path / "session-usage-state.json")
    monkeypatch.setattr(uw, "_latest", None)
    return uw


class TestUsageParsingSuccess:
    def test_used_percentage_becomes_remaining(self):
        from quick.usage_watch import parse_usage_summary

        snap = parse_usage_summary(_usage_summary(38))

        assert snap.session_used_pct == 38
        assert snap.session_remaining_pct == 62
        assert snap.monthly_used_pct == 100
        assert snap.overage_enabled is True

    def test_stream_metadata_frame_updates_the_snapshot(self, usage_state):
        payload = {"usage": {"inputTokens": 8, "outputTokens": 1}}
        frame = _frame("bedrockStreamEvent",
                       {"eventType": "metadata", "payload": payload,
                        "usageSummary": _usage_summary(91)})
        translator = ConverseToAnthropicSSE("msg_1", "m")

        for event in EventStreamDecoder().feed(frame):
            translator.handle(event)

        assert usage_state.latest_snapshot().session_remaining_pct == 9

    def test_aggregator_path_also_records(self, usage_state):
        frame = _frame("bedrockStreamEvent",
                       {"eventType": "metadata", "payload": {"usage": {"inputTokens": 3}},
                        "usageSummary": _usage_summary(50)})
        aggregator = ConverseAggregator("msg_1", "m")

        for event in EventStreamDecoder().feed(frame):
            aggregator.add(event)

        assert usage_state.latest_snapshot().session_used_pct == 50


class TestUsageParsingEdgeCases:
    def test_missing_or_junk_summary_is_ignored(self, usage_state):
        from quick.usage_watch import parse_usage_summary, record_usage_summary

        assert parse_usage_summary(None) is None
        assert parse_usage_summary({"sessionUsage": {}}) is None
        assert parse_usage_summary("nope") is None
        record_usage_summary({"sessionUsage": {"usedPercentage": None}})

        assert usage_state.latest_snapshot() is None

    def test_plain_bedrock_frames_carry_no_summary(self, usage_state):
        translator = ConverseToAnthropicSSE("msg_1", "m")

        for event in EventStreamDecoder().feed(_frame("metadata", {"usage": {"inputTokens": 1}})):
            translator.handle(event)

        assert usage_state.latest_snapshot() is None

    def test_over_100_percent_used_clamps_remaining_at_zero(self):
        from quick.usage_watch import parse_usage_summary

        assert parse_usage_summary(_usage_summary(140)).session_remaining_pct == 0


class TestUsageAlertHysteresis:
    """One alert per crossing: silent below the threshold until it recovers."""

    async def _cycle(self, uw, used):
        """Feed one reading through a full watch cycle."""
        uw.record_usage_summary(_usage_summary(used))
        return await uw.watch_once(notify=True, threshold=10)

    @pytest.mark.asyncio
    async def test_alerts_once_then_stays_quiet_until_recovery(self, usage_state, monkeypatch):
        uw = usage_state
        sent = []

        async def _fake_send(content, webhook=""):
            sent.append(content)
            return True

        monkeypatch.setattr(uw, "send_alert", _fake_send)

        _, fired_ok = await self._cycle(uw, 50)          # 50% left - quiet
        _, fired_low = await self._cycle(uw, 91)         # 9% left  - alert
        _, fired_lower = await self._cycle(uw, 97)       # 3% left  - already alerted
        _, fired_recovered = await self._cycle(uw, 20)   # 80% left - re-arms
        _, fired_again = await self._cycle(uw, 95)       # 5% left  - alert again

        assert [fired_ok, fired_low, fired_lower, fired_recovered, fired_again] == \
            [False, True, False, False, True]
        assert len(sent) == 2
        assert "剩余 9%" in sent[0]

    @pytest.mark.asyncio
    async def test_exactly_at_the_threshold_does_not_alert(self, usage_state, monkeypatch):
        uw = usage_state
        monkeypatch.setattr(uw, "send_alert", lambda *a, **k: None)
        uw.record_usage_summary(_usage_summary(90))  # exactly 10% left

        report, fired = await uw.watch_once(notify=True, threshold=10)

        assert fired is False
        assert report["below_threshold"] is False

    @pytest.mark.asyncio
    async def test_failed_push_stays_armed_to_retry(self, usage_state, monkeypatch):
        uw = usage_state

        async def _fail(content, webhook=""):
            return False

        monkeypatch.setattr(uw, "send_alert", _fail)
        uw.record_usage_summary(_usage_summary(95))

        report, fired = await uw.watch_once(notify=True, threshold=10)

        assert fired is False
        assert report["armed"] is True
        assert uw.load_state()["armed"] is True

    @pytest.mark.asyncio
    async def test_armed_state_survives_a_restart(self, usage_state, monkeypatch):
        uw = usage_state
        sent = []

        async def _fake_send(content, webhook=""):
            sent.append(content)
            return True

        monkeypatch.setattr(uw, "send_alert", _fake_send)
        uw.record_usage_summary(_usage_summary(95))
        await uw.watch_once(notify=True, threshold=10)

        # A restart loses the in-process reading but not the state file.
        monkeypatch.setattr(uw, "_latest", None)
        uw.record_usage_summary(_usage_summary(96))
        _, fired = await uw.watch_once(notify=True, threshold=10)

        assert fired is False
        assert len(sent) == 1


class TestUsageAlertMessage:
    def test_alert_names_both_numbers_and_the_rule(self):
        from quick.usage_watch import format_alert, parse_usage_summary

        text = format_alert(parse_usage_summary(_usage_summary(93, resume=42)), threshold=10)

        assert "剩余 7%" in text
        assert "已用 93%" in text
        assert "42 分钟" in text
        assert "月度：已用 100%" in text

    def test_non_allowed_entitlement_is_surfaced(self):
        from quick.usage_watch import format_alert, parse_usage_summary

        text = format_alert(parse_usage_summary(_usage_summary(99, status="THROTTLED")))

        assert "THROTTLED" in text


# ==================================================================================================
# Account pool (quick/pool.py) — selection, cooldown, failover
# ==================================================================================================

@pytest.fixture
def pool_env(tmp_path, monkeypatch):
    """A pool of two accounts backed by temp credential files, with clean state."""
    import quick.pool as qp
    import quick.usage_watch as uw

    for name in ("gateway-creds.json", "gateway-creds-b.json"):
        (tmp_path / name).write_text(json.dumps(_sample_blob()), encoding="utf-8")
    monkeypatch.setattr(qp, "QUICK_CREDS_DIR", tmp_path)
    monkeypatch.setattr(qp, "QUICK_CREDS_FILE", tmp_path / "gateway-creds.json")
    monkeypatch.setattr(qp, "QUICK_ACCOUNTS", "")
    monkeypatch.setattr(qp, "QUICK_POOL_AVOID_OVERAGE", False)
    uw._snapshots.clear()
    monkeypatch.setattr(uw, "_latest", None)
    qp.pool._accounts = {}
    qp.pool.discover()
    yield qp.pool
    qp.pool._accounts = {}
    uw._snapshots.clear()


def _reading(account: str, session_used: float, **kw):
    """Record a usageSummary for one account."""
    import quick.usage_watch as uw

    uw.record_usage_summary(_usage_summary(session_used, **kw), account=account)


class TestPoolDiscoverySuccess:
    def test_filenames_become_account_names(self, pool_env):
        assert [a.name for a in pool_env.accounts()] == ["default", "b"]

    def test_each_account_owns_its_own_creds_file(self, pool_env):
        files = {a.name: a.auth.creds_file.name for a in pool_env.accounts()}
        assert files == {"default": "gateway-creds.json", "b": "gateway-creds-b.json"}

    def test_explicit_account_list_wins(self, pool_env, monkeypatch, tmp_path):
        import quick.pool as qp

        monkeypatch.setattr(qp, "QUICK_ACCOUNTS", "b")
        assert [a.name for a in pool_env.discover(force=True)] == ["b"]

    def test_state_files_are_not_mistaken_for_credentials(self, pool_env, tmp_path):
        (tmp_path / "session-usage-state.json").write_text("{}", encoding="utf-8")
        (tmp_path / "model-watch-state.json").write_text("{}", encoding="utf-8")

        assert [a.name for a in pool_env.discover(force=True)] == ["default", "b"]


class TestPoolSelectionSuccess:
    def test_picks_the_account_with_more_session_allowance(self, pool_env):
        _reading("default", 80)   # 20% left
        _reading("b", 10)         # 90% left

        assert pool_env.select().name == "b"

    def test_unmeasured_account_is_assumed_fresh(self, pool_env):
        # 'default' measured low, 'b' never measured -> 'b' should get the traffic.
        _reading("default", 95)

        assert pool_env.select().name == "b"

    def test_same_bucket_alternates_instead_of_ping_ponging(self, pool_env):
        _reading("default", 41)   # 59% left -> bucket 5
        _reading("b", 45)         # 55% left -> bucket 5
        picks = []
        for _ in range(4):
            account = pool_env.select()
            picks.append(account.name)
            pool_env.begin(account)
            pool_env.end(account)

        assert picks == ["default", "b", "default", "b"]  # alternates, never sticks

    def test_exclude_skips_an_already_tried_account(self, pool_env):
        _reading("default", 10)

        assert pool_env.select(exclude={"default"}).name == "b"

    def test_inflight_breaks_a_bucket_tie(self, pool_env):
        _reading("default", 10)
        _reading("b", 10)
        first = pool_env.select()
        pool_env.begin(first)   # leave it in flight

        assert pool_env.select().name != first.name


class TestPoolFailureHandling:
    def test_throttling_cools_the_account_down(self, pool_env):
        account = pool_env.get("default")
        pool_env.note_failure(account, 429, "Too many requests")

        assert account.status() == "cooling"
        assert pool_env.select().name == "b"

    def test_invalid_grant_disables_permanently(self, pool_env):
        account = pool_env.get("b")
        pool_env.note_failure(account, 400, "Keycloak refresh failed (400): invalid_grant")

        assert account.status() == "disabled"
        assert "invalid_grant" in account.disabled_reason or account.disabled_reason
        assert pool_env.select().name == "default"

    def test_exhausted_allowance_cools_down_until_the_window_resets(self, pool_env):
        import time as _time

        _reading("default", 100, resume=42)   # 0% left, window resets in 42 min
        account = pool_env.get("default")

        assert account.status() == "cooling"
        assert 41 * 60 <= account.cooldown_until - _time.time() <= 42 * 60

    def test_a_low_but_usable_account_is_not_benched(self, pool_env):
        """resumeInMinutes is a window-reset countdown, not a lockout (verified live)."""
        _reading("default", 79, resume=55)    # 21% left, still ALLOWED

        assert pool_env.get("default").status() == "ready"

    def test_a_low_account_still_ranks_below_a_fresh_one(self, pool_env):
        _reading("default", 79, resume=55)    # 21% left
        _reading("b", 0)                      # 100% left

        assert pool_env.select().name == "b"

    def test_revoked_entitlement_takes_the_account_out(self, pool_env):
        _reading("b", 50, status="BLOCKED")

        assert pool_env.get("b").status() == "cooling"
        assert pool_env.select().name == "default"

    def test_everything_down_yields_no_candidate(self, pool_env):
        for account in pool_env.accounts():
            pool_env.note_failure(account, 429, "throttled")

        assert pool_env.select() is None

    def test_a_recovered_account_comes_back_on_its_own(self, pool_env, monkeypatch):
        import time as _time

        account = pool_env.get("default")
        pool_env.note_failure(account, 429, "throttled")
        monkeypatch.setattr(_time, "time", lambda: account.cooldown_until + 1)

        assert account.status() == "ready"


class TestPoolSnapshotSafety:
    def test_snapshot_carries_no_credential_material(self, pool_env):
        _reading("default", 30)
        blob = json.dumps(pool_env.snapshot())

        for secret in ("token", "tenant", "arn", "refresh", "qbs-", "Bearer"):
            assert secret not in blob, f"status payload leaked {secret!r}"

    def test_snapshot_reports_readiness_and_quota(self, pool_env):
        _reading("default", 30)
        snapshot = pool_env.snapshot()

        assert snapshot["total"] == 2 and snapshot["ready"] == 2
        entry = next(a for a in snapshot["accounts"] if a["name"] == "default")
        assert entry["session_remaining_pct"] == 70 and entry["status"] == "ready"


class TestRequestFailoverSuccess:
    """A dead account must cost a log line, not a user-visible error."""

    def _ok_stream(self):
        frames = [
            _quick_frame("messageStart", {"role": "assistant"}),
            _quick_frame("contentBlockDelta", {"contentBlockIndex": 0,
                                               "delta": {"text": "hi"}}),
            _quick_frame("messageStop", {"stopReason": "end_turn"}),
        ]
        return frames

    def _fake_converse(self, failing: str, error):
        """converse_stream double: `failing` account raises, the other streams fine."""
        frames = self._ok_stream()

        async def _gen(converse_input, account=None):
            if account is not None and account.name == failing:
                raise error
            for frame in frames:
                yield frame

        return _gen

    @pytest.mark.asyncio
    async def test_non_streaming_retries_on_the_next_account(self, pool_env, monkeypatch):
        import quick.routes as qr
        from quick.client import QuickAPIError

        _reading("default", 10)   # 90% left -> tried first
        _reading("b", 60)         # 40% left -> the fallback
        monkeypatch.setattr(qr, "converse_stream",
                            self._fake_converse("default", QuickAPIError(429, "throttled")))

        final, error = await qr._serve_aggregated({"modelId": "m"}, "msg_1", "m",
                                                  pinned="", websearch=False)

        assert error is None
        assert final["content"][0]["text"] == "hi"
        assert pool_env.get("default").status() == "cooling"

    @pytest.mark.asyncio
    async def test_a_bad_request_does_not_burn_a_second_account(self, pool_env, monkeypatch):
        import quick.routes as qr
        from quick.client import QuickAPIError

        async def _always_400(converse_input, account=None):
            raise QuickAPIError(400, "Improperly formed request")
            yield b""  # pragma: no cover - generator marker

        monkeypatch.setattr(qr, "converse_stream", _always_400)

        final, error = await qr._serve_aggregated({"modelId": "m"}, "msg_1", "m",
                                                  pinned="", websearch=False)

        assert final is None and error.status_code == 400
        # Nothing was blamed on the accounts: both stay ready.
        assert [a.status() for a in pool_env.accounts()] == ["ready", "ready"]

    @pytest.mark.asyncio
    async def test_streaming_fails_over_before_the_first_token(self, pool_env, monkeypatch):
        import quick.routes as qr
        from quick.client import QuickAPIError

        _reading("default", 10)   # 90% left -> tried first
        _reading("b", 60)         # 40% left -> the fallback
        monkeypatch.setattr(qr, "converse_stream",
                            self._fake_converse("default", QuickAPIError(403, "not authorized")))

        chunks = [c async for c in qr._stream({"modelId": "m"}, "msg_1", "m", "")]
        body = "".join(chunks)

        assert "event: error" not in body           # the client never saw the failure
        assert "message_start" in body and "hi" in body
        assert body.count("event: message_start") == 1   # not restarted mid-stream

    @pytest.mark.asyncio
    async def test_in_stream_error_frame_also_fails_over(self, pool_env, monkeypatch):
        import quick.routes as qr

        ok_frames = self._ok_stream()

        async def _gen(converse_input, account=None):
            if account.name == "default":
                # HTTP 200 with an error frame — Quick's IAM-deny shape.
                yield _quick_frame("error", {"message": "User is not authorized"})
                return
            for frame in ok_frames:
                yield frame

        _reading("default", 10)   # 90% left -> tried first
        _reading("b", 60)         # 40% left -> the fallback
        monkeypatch.setattr(qr, "converse_stream", _gen)

        body = "".join([c async for c in qr._stream({"modelId": "m"}, "msg_1", "m", "")])

        assert "event: error" not in body and "hi" in body

    @pytest.mark.asyncio
    async def test_pinned_account_never_fails_over(self, pool_env, monkeypatch):
        import quick.routes as qr
        from quick.client import QuickAPIError

        monkeypatch.setattr(qr, "converse_stream",
                            self._fake_converse("b", QuickAPIError(429, "throttled")))

        final, error = await qr._serve_aggregated({"modelId": "m"}, "msg_1", "m",
                                                  pinned="b", websearch=False)

        assert final is None and error.status_code == 429

    @pytest.mark.asyncio
    async def test_empty_pool_is_a_503_not_a_crash(self, pool_env, monkeypatch):
        import quick.routes as qr

        for account in pool_env.accounts():
            pool_env.note_failure(account, 429, "throttled")

        final, error = await qr._serve_aggregated({"modelId": "m"}, "msg_1", "m",
                                                  pinned="", websearch=False)

        assert final is None and error.status_code == 503


class TestStatusPageSafety:
    def test_token_gate_is_open_when_unset(self, monkeypatch):
        import quick.status_app as sa

        monkeypatch.setattr(sa, "QUICK_STATUS_TOKEN", "")

        class _Req:
            query_params = {}
            headers = {}

        assert sa._authorized(_Req()) is True

    def test_token_gate_rejects_a_wrong_token(self, monkeypatch):
        import quick.status_app as sa

        monkeypatch.setattr(sa, "QUICK_STATUS_TOKEN", "s3cret")

        class _Req:
            query_params = {"t": "nope"}
            headers = {}

        assert sa._authorized(_Req()) is False

    def test_page_has_no_inference_route(self):
        from quick.status_app import create_status_app

        paths = {r.path for r in create_status_app().routes}

        assert paths == {"/", "/api/pool", "/health"}
