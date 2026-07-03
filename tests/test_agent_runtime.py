"""
Unit tests for agent_runtime.LangChainRuntime.

All LangChain internals are mocked so no network access or real
Ollama/OpenAI dependency is required.
"""

from __future__ import annotations

import sys
import types
import pytest


# ---------------------------------------------------------------------------
# Minimal stubs for langchain packages so agent_runtime imports without
# the real packages installed.
# ---------------------------------------------------------------------------

def _make_message_stub(role: str):
    class _Msg:
        def __init__(self, content="", tool_calls=None, **kwargs):
            self.content = content
            self.tool_calls = tool_calls or []
            self.additional_kwargs = kwargs.get("additional_kwargs", {})
            self.response_metadata = kwargs.get("response_metadata", {})

        def __add__(self, other):
            merged = _Msg(
                content=self.content + (other.content if isinstance(other.content, str) else ""),
                tool_calls=self.tool_calls + other.tool_calls,
            )
            merged.additional_kwargs = {**self.additional_kwargs, **other.additional_kwargs}
            merged.response_metadata = {**self.response_metadata, **other.response_metadata}
            return merged
    _Msg.__name__ = role
    return _Msg


HumanMessage  = _make_message_stub("HumanMessage")
AIMessage     = _make_message_stub("AIMessage")
SystemMessage = _make_message_stub("SystemMessage")
ToolMessage   = _make_message_stub("ToolMessage")


def _install_langchain_stubs():
    """Insert minimal stub modules into sys.modules before importing agent_runtime."""
    # langchain_core.messages
    lc_core = types.ModuleType("langchain_core")
    lc_core_messages = types.ModuleType("langchain_core.messages")
    lc_core_messages.HumanMessage  = HumanMessage
    lc_core_messages.AIMessage     = AIMessage
    lc_core_messages.SystemMessage = SystemMessage
    lc_core_messages.ToolMessage   = ToolMessage
    sys.modules.setdefault("langchain_core", lc_core)
    sys.modules["langchain_core.messages"] = lc_core_messages

    # langchain_ollama
    lc_ollama = types.ModuleType("langchain_ollama")
    lc_ollama.ChatOllama = object  # placeholder; replaced per test
    sys.modules.setdefault("langchain_ollama", lc_ollama)

    # langchain_openai
    lc_openai = types.ModuleType("langchain_openai")
    lc_openai.ChatOpenAI = object  # placeholder; replaced per test
    sys.modules.setdefault("langchain_openai", lc_openai)

    # langchain (top-level, some code may import it)
    sys.modules.setdefault("langchain", types.ModuleType("langchain"))


_install_langchain_stubs()

# Now safe to import
from agent_runtime import LangChainRuntime, RuntimeSettings  # noqa: E402
from config import MODE_LOCAL, MODE_CUSTOM  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(**overrides) -> RuntimeSettings:
    defaults = dict(
        conn_mode=MODE_LOCAL,
        model="llama3",
        temperature=0.4,
        enable_thinking=False,
        custom_api_url="https://api.openai.com/v1",
        custom_api_key="",
        response_token_budget=4096,
        request_timeout=30,
    )
    defaults.update(overrides)
    return RuntimeSettings(**defaults)


def _fake_response(content="hello", tool_calls=None, finish_reason="stop", thinking=None):
    msg = AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        response_metadata={"finish_reason": finish_reason},
    )
    if thinking:
        msg.additional_kwargs["thinking"] = thinking
    return msg


# ---------------------------------------------------------------------------
# availability & supports()
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_available_when_stubs_installed(self):
        rt = LangChainRuntime()
        assert rt.available is True
        assert rt.error == ""

    def test_supports_local_mode(self):
        rt = LangChainRuntime()
        assert rt.supports(MODE_LOCAL) is True

    def test_supports_custom_mode(self):
        rt = LangChainRuntime()
        assert rt.supports(MODE_CUSTOM) is True

    def test_does_not_support_unknown_mode(self):
        rt = LangChainRuntime()
        assert rt.supports("🌐 Remote Ollama") is False

    def test_supports_returns_false_when_unavailable(self, monkeypatch):
        rt = LangChainRuntime()
        monkeypatch.setattr(rt, "available", False)
        assert rt.supports(MODE_LOCAL) is False


# ---------------------------------------------------------------------------
# _build_model()
# ---------------------------------------------------------------------------

class TestBuildModel:
    def test_build_local_returns_chat_ollama(self, monkeypatch):
        import langchain_ollama as _lo
        calls = []

        class FakeOllama:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(_lo, "ChatOllama", FakeOllama)
        rt = LangChainRuntime()
        model = rt._build_model(_settings(conn_mode=MODE_LOCAL), streaming=False)
        assert isinstance(model, FakeOllama)
        assert calls[0]["model"] == "llama3"

    def test_build_custom_returns_chat_openai(self, monkeypatch):
        import langchain_openai as _lop
        calls = []

        class FakeOpenAI:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(_lop, "ChatOpenAI", FakeOpenAI)
        rt = LangChainRuntime()
        model = rt._build_model(_settings(conn_mode=MODE_CUSTOM), streaming=False)
        assert isinstance(model, FakeOpenAI)

    def test_build_unknown_raises_value_error(self):
        rt = LangChainRuntime()
        with pytest.raises(ValueError, match="Unsupported"):
            rt._build_model(_settings(conn_mode="bad_mode"), streaming=False)


# ---------------------------------------------------------------------------
# _string_content()
# ---------------------------------------------------------------------------

class TestStringContent:
    def test_string_passthrough(self):
        assert LangChainRuntime._string_content("hello") == "hello"

    def test_none_returns_empty(self):
        assert LangChainRuntime._string_content(None) == ""

    def test_list_of_strings(self):
        assert LangChainRuntime._string_content(["a", "b"]) == "ab"

    def test_list_of_dicts_text_key(self):
        result = LangChainRuntime._string_content([{"text": "x"}, {"text": "y"}])
        assert result == "xy"

    def test_list_of_dicts_content_key(self):
        result = LangChainRuntime._string_content([{"content": "foo"}])
        assert result == "foo"

    def test_empty_list(self):
        assert LangChainRuntime._string_content([]) == ""

    def test_integer_coerced(self):
        assert LangChainRuntime._string_content(42) == "42"


# ---------------------------------------------------------------------------
# _extract_thinking()
# ---------------------------------------------------------------------------

class TestExtractThinking:
    def test_from_additional_kwargs(self):
        msg = AIMessage(additional_kwargs={"thinking": "deep thought"})
        assert LangChainRuntime._extract_thinking(msg) == "deep thought"

    def test_from_response_metadata(self):
        msg = AIMessage(response_metadata={"reasoning_content": "reasoned"})
        assert LangChainRuntime._extract_thinking(msg) == "reasoned"

    def test_no_thinking_returns_empty(self):
        msg = AIMessage()
        assert LangChainRuntime._extract_thinking(msg) == ""

    def test_reasoning_key(self):
        msg = AIMessage(additional_kwargs={"reasoning": "some reasoning"})
        assert LangChainRuntime._extract_thinking(msg) == "some reasoning"


# ---------------------------------------------------------------------------
# _normalize_response()
# ---------------------------------------------------------------------------

class TestNormalizeResponse:
    def test_none_input(self):
        rt = LangChainRuntime()
        result = rt._normalize_response(None)
        assert result == {"content": "", "thinking": "", "tool_calls": [], "finish_reason": ""}

    def test_basic_response(self):
        rt = LangChainRuntime()
        msg = _fake_response(content="hi", finish_reason="stop")
        result = rt._normalize_response(msg)
        assert result["content"] == "hi"
        assert result["finish_reason"] == "stop"
        assert result["tool_calls"] == []

    def test_tool_calls_extracted(self):
        rt = LangChainRuntime()
        msg = _fake_response()
        msg.tool_calls = [{"id": "c1", "name": "run_bash", "args": {"command": "ls"}}]
        result = rt._normalize_response(msg)
        assert len(result["tool_calls"]) == 1
        tc = result["tool_calls"][0]
        assert tc["name"] == "run_bash"
        assert tc["arguments"] == {"command": "ls"}

    def test_thinking_extracted(self):
        rt = LangChainRuntime()
        msg = _fake_response(thinking="thought")
        result = rt._normalize_response(msg)
        assert result["thinking"] == "thought"


# ---------------------------------------------------------------------------
# invoke()
# ---------------------------------------------------------------------------

class TestInvoke:
    def test_invoke_calls_bind_tools_and_returns_normalized(self, monkeypatch):
        import langchain_ollama as _lo

        fake_response = _fake_response(content="done")

        class FakeBound:
            def invoke(self, messages):
                return fake_response

        class FakeOllama:
            def __init__(self, **kwargs): pass
            def bind_tools(self, tools):
                return FakeBound()

        monkeypatch.setattr(_lo, "ChatOllama", FakeOllama)
        rt = LangChainRuntime()
        result = rt.invoke([{"role": "user", "content": "hello"}], [], _settings())
        assert result["content"] == "done"


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------

class TestStream:
    def test_stream_aggregates_tokens(self, monkeypatch):
        import langchain_ollama as _lo

        chunk1 = AIMessage(content="hel")
        chunk2 = AIMessage(content="lo")

        class FakeBound:
            def stream(self, messages):
                yield chunk1
                yield chunk2

        class FakeOllama:
            def __init__(self, **kwargs): pass
            def bind_tools(self, tools):
                return FakeBound()

        monkeypatch.setattr(_lo, "ChatOllama", FakeOllama)
        rt = LangChainRuntime()
        events = []
        result = rt.stream(
            [{"role": "user", "content": "hi"}],
            [],
            _settings(),
            write_event=lambda e: events.append(e),
        )
        assert result["content"] == "hello"
        token_events = [e for e in events if e.get("type") == "token"]
        assert len(token_events) == 2
        assert token_events[0]["content"] == "hel"
        assert token_events[1]["content"] == "lo"

    def test_stream_write_event_callback_called(self, monkeypatch):
        import langchain_ollama as _lo

        chunk = AIMessage(content="x")

        class FakeBound:
            def stream(self, messages):
                yield chunk

        class FakeOllama:
            def __init__(self, **kwargs): pass
            def bind_tools(self, tools):
                return FakeBound()

        monkeypatch.setattr(_lo, "ChatOllama", FakeOllama)
        rt = LangChainRuntime()
        called_with = []
        rt.stream(
            [{"role": "user", "content": "test"}],
            [],
            _settings(),
            write_event=lambda e: called_with.append(e),
        )
        assert any(e.get("type") == "token" for e in called_with)
