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

    def test_unavailable_when_import_fails(self, monkeypatch):
        """If a required package is missing, available should be False and error non-empty."""
        import builtins
        real_import = builtins.__import__

        def _fail_import(name, *args, **kwargs):
            if name == "langchain_ollama":
                raise ImportError("no module named langchain_ollama")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail_import)
        available, error = LangChainRuntime._check_available()
        assert available is False
        assert "langchain_ollama" in error


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

    def test_build_local_with_thinking_sets_reasoning(self, monkeypatch):
        """When enable_thinking=True the kwargs must include reasoning=True."""
        import langchain_ollama as _lo
        calls = []

        class FakeOllama:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(_lo, "ChatOllama", FakeOllama)
        rt = LangChainRuntime()
        rt._build_model(_settings(conn_mode=MODE_LOCAL, enable_thinking=True), streaming=False)
        assert calls[0].get("reasoning") is True

    def test_build_local_without_thinking_omits_reasoning(self, monkeypatch):
        import langchain_ollama as _lo
        calls = []

        class FakeOllama:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(_lo, "ChatOllama", FakeOllama)
        rt = LangChainRuntime()
        rt._build_model(_settings(conn_mode=MODE_LOCAL, enable_thinking=False), streaming=False)
        assert "reasoning" not in calls[0]

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

    def test_build_custom_passes_api_key(self, monkeypatch):
        import langchain_openai as _lop
        calls = []

        class FakeOpenAI:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(_lop, "ChatOpenAI", FakeOpenAI)
        rt = LangChainRuntime()
        rt._build_model(_settings(conn_mode=MODE_CUSTOM, custom_api_key="sk-test"), streaming=False)
        assert calls[0]["api_key"] == "sk-test"

    def test_build_custom_fallback_api_key_when_empty(self, monkeypatch):
        import langchain_openai as _lop
        calls = []

        class FakeOpenAI:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(_lop, "ChatOpenAI", FakeOpenAI)
        rt = LangChainRuntime()
        rt._build_model(_settings(conn_mode=MODE_CUSTOM, custom_api_key=""), streaming=False)
        assert calls[0]["api_key"] == "not-needed"

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

    def test_empty_string(self):
        assert LangChainRuntime._string_content("") == ""

    def test_list_with_empty_dict(self):
        assert LangChainRuntime._string_content([{}]) == ""


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

    def test_metadata_thinking_key(self):
        msg = AIMessage(response_metadata={"thinking": "meta thought"})
        assert LangChainRuntime._extract_thinking(msg) == "meta thought"

    def test_additional_kwargs_takes_priority_over_metadata(self):
        msg = AIMessage(
            additional_kwargs={"thinking": "from kwargs"},
            response_metadata={"thinking": "from meta"},
        )
        assert LangChainRuntime._extract_thinking(msg) == "from kwargs"

    def test_object_without_attributes(self):
        """Plain object with no additional_kwargs/response_metadata returns empty."""
        class Bare:
            pass
        assert LangChainRuntime._extract_thinking(Bare()) == ""


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

    def test_tool_calls_id_fallback(self):
        """tool_call without id gets a generated call_N id."""
        rt = LangChainRuntime()
        msg = _fake_response()
        msg.tool_calls = [{"name": "run_python", "args": {"code": "print(1)"}}]
        result = rt._normalize_response(msg)
        assert result["tool_calls"][0]["id"] == "call_0"

    def test_thinking_extracted(self):
        rt = LangChainRuntime()
        msg = _fake_response(thinking="thought")
        result = rt._normalize_response(msg)
        assert result["thinking"] == "thought"

    def test_done_reason_from_metadata(self):
        """Ollama uses done_reason; it should be returned as finish_reason."""
        rt = LangChainRuntime()
        msg = AIMessage(
            content="bye",
            response_metadata={"done_reason": "stop"},
        )
        result = rt._normalize_response(msg)
        assert result["finish_reason"] == "stop"

    def test_finish_reason_empty_when_no_metadata(self):
        rt = LangChainRuntime()
        msg = AIMessage(content="ok")
        result = rt._normalize_response(msg)
        assert result["finish_reason"] == ""

    def test_multiple_tool_calls(self):
        rt = LangChainRuntime()
        msg = _fake_response()
        msg.tool_calls = [
            {"id": "c1", "name": "tool_a", "args": {}},
            {"id": "c2", "name": "tool_b", "args": {"x": 1}},
        ]
        result = rt._normalize_response(msg)
        assert len(result["tool_calls"]) == 2
        assert result["tool_calls"][1]["name"] == "tool_b"


# ---------------------------------------------------------------------------
# _to_langchain_messages()
# ---------------------------------------------------------------------------

class TestToLangchainMessages:
    def test_system_message(self):
        rt = LangChainRuntime()
        msgs = rt._to_langchain_messages([{"role": "system", "content": "You are helpful."}])
        assert len(msgs) == 1
        assert msgs[0].__class__.__name__ == "SystemMessage"
        assert msgs[0].content == "You are helpful."

    def test_user_message(self):
        rt = LangChainRuntime()
        msgs = rt._to_langchain_messages([{"role": "user", "content": "hello"}])
        assert msgs[0].__class__.__name__ == "HumanMessage"
        assert msgs[0].content == "hello"

    def test_unknown_role_becomes_human(self):
        rt = LangChainRuntime()
        msgs = rt._to_langchain_messages([{"role": "observer", "content": "watching"}])
        assert msgs[0].__class__.__name__ == "HumanMessage"

    def test_assistant_message_no_tool_calls(self):
        rt = LangChainRuntime()
        msgs = rt._to_langchain_messages([{"role": "assistant", "content": "sure"}])
        assert msgs[0].__class__.__name__ == "AIMessage"
        assert msgs[0].content == "sure"
        assert msgs[0].tool_calls == []

    def test_assistant_message_with_tool_calls(self):
        rt = LangChainRuntime()
        history = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "c1",
                "function": {"name": "run_bash", "arguments": {"command": "ls"}},
            }],
        }]
        msgs = rt._to_langchain_messages(history)
        assert len(msgs[0].tool_calls) == 1
        tc = msgs[0].tool_calls[0]
        assert tc["name"] == "run_bash"
        assert tc["args"] == {"command": "ls"}
        assert tc["id"] == "c1"

    def test_assistant_tool_call_args_as_json_string(self):
        """Arguments passed as JSON string should be parsed to dict."""
        import json
        rt = LangChainRuntime()
        history = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {"name": "run_python", "arguments": json.dumps({"code": "1+1"})},
            }],
        }]
        msgs = rt._to_langchain_messages(history)
        assert msgs[0].tool_calls[0]["args"] == {"code": "1+1"}

    def test_assistant_tool_call_bad_json_becomes_empty_dict(self):
        rt = LangChainRuntime()
        history = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {"name": "run_bash", "arguments": "{bad json"},
            }],
        }]
        msgs = rt._to_langchain_messages(history)
        assert msgs[0].tool_calls[0]["args"] == {}

    def test_tool_role_message(self):
        rt = LangChainRuntime()
        msgs = rt._to_langchain_messages([{
            "role": "tool",
            "content": "output",
            "name": "run_bash",
            "tool_call_id": "c1",
        }])
        assert msgs[0].__class__.__name__ == "ToolMessage"
        assert msgs[0].content == "output"

    def test_tool_role_fallback_tool_call_id(self):
        """tool message without tool_call_id gets a generated id."""
        rt = LangChainRuntime()
        msgs = rt._to_langchain_messages([{"role": "tool", "content": "ok", "name": "x"}])
        assert msgs[0].tool_call_id == "call_0"

    def test_mixed_history(self):
        rt = LangChainRuntime()
        history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "tool", "content": "result", "name": "t"},
        ]
        msgs = rt._to_langchain_messages(history)
        roles = [m.__class__.__name__ for m in msgs]
        assert roles == ["SystemMessage", "HumanMessage", "AIMessage", "ToolMessage"]

    def test_empty_history(self):
        rt = LangChainRuntime()
        assert rt._to_langchain_messages([]) == []

    def test_none_content_becomes_empty_string(self):
        rt = LangChainRuntime()
        msgs = rt._to_langchain_messages([{"role": "user", "content": None}])
        assert msgs[0].content == ""


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

    def test_invoke_with_custom_mode(self, monkeypatch):
        import langchain_openai as _lop

        fake_response = _fake_response(content="custom")

        class FakeBound:
            def invoke(self, messages):
                return fake_response

        class FakeOpenAI:
            def __init__(self, **kwargs): pass
            def bind_tools(self, tools):
                return FakeBound()

        monkeypatch.setattr(_lop, "ChatOpenAI", FakeOpenAI)
        rt = LangChainRuntime()
        result = rt.invoke(
            [{"role": "user", "content": "hi"}],
            [],
            _settings(conn_mode=MODE_CUSTOM, custom_api_key="sk-x"),
        )
        assert result["content"] == "custom"

    def test_invoke_passes_tools_to_bind(self, monkeypatch):
        import langchain_ollama as _lo
        bound_tools_received = []

        class FakeBound:
            def invoke(self, messages):
                return _fake_response()

        class FakeOllama:
            def __init__(self, **kwargs): pass
            def bind_tools(self, tools):
                bound_tools_received.extend(tools)
                return FakeBound()

        monkeypatch.setattr(_lo, "ChatOllama", FakeOllama)
        rt = LangChainRuntime()
        tool_schema = [{"type": "function", "function": {"name": "run_bash"}}]
        rt.invoke([{"role": "user", "content": "go"}], tool_schema, _settings())
        assert len(bound_tools_received) == 1
        assert bound_tools_received[0]["function"]["name"] == "run_bash"

    def test_invoke_returns_tool_calls(self, monkeypatch):
        import langchain_ollama as _lo

        response = _fake_response(content="")
        response.tool_calls = [{"id": "c1", "name": "run_bash", "args": {"command": "pwd"}}]

        class FakeBound:
            def invoke(self, messages):
                return response

        class FakeOllama:
            def __init__(self, **kwargs): pass
            def bind_tools(self, tools):
                return FakeBound()

        monkeypatch.setattr(_lo, "ChatOllama", FakeOllama)
        rt = LangChainRuntime()
        result = rt.invoke([{"role": "user", "content": "run"}], [], _settings())
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == "run_bash"


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

    def test_stream_empty_chunks_no_token_events(self, monkeypatch):
        """Chunks with empty content must not emit token events."""
        import langchain_ollama as _lo

        chunk = AIMessage(content="")

        class FakeBound:
            def stream(self, messages):
                yield chunk

        class FakeOllama:
            def __init__(self, **kwargs): pass
            def bind_tools(self, tools):
                return FakeBound()

        monkeypatch.setattr(_lo, "ChatOllama", FakeOllama)
        rt = LangChainRuntime()
        events = []
        result = rt.stream(
            [{"role": "user", "content": "silent"}],
            [],
            _settings(),
            write_event=lambda e: events.append(e),
        )
        token_events = [e for e in events if e.get("type") == "token"]
        assert token_events == []
        assert result["content"] == ""

    def test_stream_thinking_extracted_from_chunks(self, monkeypatch):
        """Thinking content from chunks must be aggregated in result["thinking"]."""
        import langchain_ollama as _lo

        chunk = AIMessage(content="answer", additional_kwargs={"thinking": "deliberating"})

        class FakeBound:
            def stream(self, messages):
                yield chunk

        class FakeOllama:
            def __init__(self, **kwargs): pass
            def bind_tools(self, tools):
                return FakeBound()

        monkeypatch.setattr(_lo, "ChatOllama", FakeOllama)
        rt = LangChainRuntime()
        result = rt.stream(
            [{"role": "user", "content": "think"}],
            [],
            _settings(),
            write_event=lambda _: None,
        )
        assert result["thinking"] == "deliberating"

    def test_stream_tool_calls_in_aggregate(self, monkeypatch):
        """Tool calls appearing in the aggregate message are forwarded in the result."""
        import langchain_ollama as _lo

        chunk1 = AIMessage(content="")
        chunk1.tool_calls = [{"id": "c1", "name": "run_bash", "args": {"command": "echo hi"}}]
        chunk2 = AIMessage(content="")

        # Override __add__ to produce a combined message with tool_calls
        def _combined_add(self, other):
            merged = AIMessage(content="")
            merged.tool_calls = self.tool_calls + other.tool_calls
            merged.additional_kwargs = {}
            merged.response_metadata = {}
            return merged

        chunk1.__class__.__add__ = _combined_add

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
        result = rt.stream(
            [{"role": "user", "content": "go"}],
            [],
            _settings(),
            write_event=lambda _: None,
        )
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == "run_bash"

    def test_stream_no_chunks_returns_empty(self, monkeypatch):
        """If the model returns zero chunks, result must be a clean empty response."""
        import langchain_ollama as _lo

        class FakeBound:
            def stream(self, messages):
                return iter([])

        class FakeOllama:
            def __init__(self, **kwargs): pass
            def bind_tools(self, tools):
                return FakeBound()

        monkeypatch.setattr(_lo, "ChatOllama", FakeOllama)
        rt = LangChainRuntime()
        result = rt.stream(
            [{"role": "user", "content": "hello"}],
            [],
            _settings(),
            write_event=lambda _: None,
        )
        assert result == {"content": "", "thinking": "", "tool_calls": [], "finish_reason": ""}
