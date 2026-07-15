"""
agent_runtime.py - optional LangChain-backed model runtime.

The app keeps a small HTTP fallback for portability. When LangChain provider
packages are installed, this module lets the agent use ChatOllama or ChatOpenAI
behind the same response shape used by the existing agent loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from config import MODE_CUSTOM, MODE_LOCAL


@dataclass
class RuntimeSettings:
    conn_mode: str
    model: str
    temperature: float
    enable_thinking: bool
    custom_api_url: str
    custom_api_key: str
    response_token_budget: int
    request_timeout: int


class LangChainRuntime:
    """Thin adapter around LangChain chat models."""

    def __init__(self) -> None:
        self.available, self.error = self._check_available()

    @staticmethod
    def _check_available() -> tuple[bool, str]:
        try:
            import langchain_core  # noqa: F401
            import langchain_ollama  # noqa: F401
            import langchain_openai  # noqa: F401
            return True, ""
        except Exception as exc:
            return False, repr(exc)

    def supports(self, conn_mode: str) -> bool:
        return self.available and conn_mode in {MODE_LOCAL, MODE_CUSTOM}

    def invoke(self, history: list[dict], tools: list[dict], settings: RuntimeSettings) -> dict:
        model = self._build_model(settings, streaming=False)
        messages = self._to_langchain_messages(history)
        response = model.bind_tools(tools).invoke(messages)
        return self._normalize_response(response)

    def stream(
        self,
        history: list[dict],
        tools: list[dict],
        settings: RuntimeSettings,
        write_event: Callable[[dict], None],
    ) -> dict:
        model = self._build_model(settings, streaming=True)
        messages = self._to_langchain_messages(history)
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        aggregate = None

        for chunk in model.bind_tools(tools).stream(messages):
            aggregate = chunk if aggregate is None else aggregate + chunk
            content = self._string_content(getattr(chunk, "content", ""))
            if content:
                content_parts.append(content)
                write_event({"type": "token", "content": content})
            thinking = self._extract_thinking(chunk)
            if thinking:
                thinking_parts.append(thinking)

        normalized = self._normalize_response(aggregate)
        if content_parts:
            normalized["content"] = "".join(content_parts)
        if thinking_parts:
            normalized["thinking"] = "".join(thinking_parts)
        return normalized

    def _build_model(self, settings: RuntimeSettings, streaming: bool):
        if settings.conn_mode == MODE_LOCAL:
            from langchain_ollama import ChatOllama

            kwargs = {
                "model": settings.model,
                "base_url": "http://127.0.0.1:11434",
                "temperature": settings.temperature,
                "num_predict": settings.response_token_budget,
                "streaming": streaming,
                "timeout": settings.request_timeout,
            }
            if settings.enable_thinking:
                kwargs["reasoning"] = True
            return ChatOllama(**kwargs)

        if settings.conn_mode == MODE_CUSTOM:
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=settings.model,
                base_url=settings.custom_api_url.rstrip("/"),
                api_key=settings.custom_api_key or "not-needed",
                temperature=settings.temperature,
                max_tokens=settings.response_token_budget,
                streaming=streaming,
                timeout=settings.request_timeout,
            )

        raise ValueError(f"Unsupported connection mode for LangChain: {settings.conn_mode}")

    @staticmethod
    def _to_langchain_messages(history: list[dict]) -> list[Any]:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

        messages = []
        for index, item in enumerate(history):
            role = item.get("role")
            content = item.get("content") or ""
            if role == "system":
                messages.append(SystemMessage(content=content))
            elif role == "assistant":
                tool_calls = []
                for tc_index, call in enumerate(item.get("tool_calls") or []):
                    fn = call.get("function") or {}
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    tool_calls.append({
                        "name": fn.get("name", ""),
                        "args": args,
                        "id": call.get("id") or f"call_{index}_{tc_index}",
                    })
                messages.append(AIMessage(content=content, tool_calls=tool_calls))
            elif role == "tool":
                tool_call_id = item.get("tool_call_id") or f"call_{index}"
                message = ToolMessage(
                    content=content,
                    name=item.get("name"),
                    tool_call_id=tool_call_id,
                )
                if not hasattr(message, "tool_call_id"):
                    object.__setattr__(message, "tool_call_id", tool_call_id)
                messages.append(message)
            else:
                messages.append(HumanMessage(content=content))
        return messages

    def _normalize_response(self, response: Any) -> dict:
        if response is None:
            return {"content": "", "thinking": "", "tool_calls": [], "finish_reason": ""}

        tool_calls = []
        for index, call in enumerate(getattr(response, "tool_calls", None) or []):
            tool_calls.append({
                "id": call.get("id") or f"call_{index}",
                "type": "function",
                "name": call.get("name", ""),
                "arguments": call.get("args") or {},
            })

        metadata = getattr(response, "response_metadata", None) or {}
        finish_reason = (
            metadata.get("finish_reason")
            or metadata.get("done_reason")
            or getattr(response, "finish_reason", "")
            or ""
        )
        return {
            "content": self._string_content(getattr(response, "content", "")),
            "thinking": self._extract_thinking(response),
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
        }

    @staticmethod
    def _string_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
            return "".join(parts)
        return str(content or "")

    @staticmethod
    def _extract_thinking(message: Any) -> str:
        additional = getattr(message, "additional_kwargs", None) or {}
        metadata = getattr(message, "response_metadata", None) or {}
        for key in ("thinking", "reasoning_content", "reasoning"):
            value = additional.get(key) or metadata.get(key)
            if value:
                return str(value)
        return ""
