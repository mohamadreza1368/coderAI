"""
context_builder.py - RAG context, prompt building, and token estimation helpers.
"""

from __future__ import annotations

import ast
import json
import os
import re
from typing import Any, Callable


DEFAULT_CONTEXT_TOKEN_BUDGET = 32_000
DEFAULT_RESPONSE_TOKEN_BUDGET = 4_096
MAX_HISTORY_MESSAGE_CHARS = 12_000
MAX_ASSISTANT_HISTORY_CHARS = 3_500
MAX_KEPT_HISTORY_MESSAGES = 6


def clip_for_context(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(0, limit // 2)
    tail = max(0, limit - head - 120)
    tail_text = text[-tail:] if tail > 0 else ""
    return (
        text[:head]
        + f"\n\n...[context clipped: {len(text) - head - tail:,} chars omitted]...\n\n"
        + tail_text
    )


def compact_tool_output(content: str, max_chars: int = 1500, tool_name: str = "") -> str:
    """Compacts voluminous tool output (e.g. large file reads or command logs) while preserving key facts."""
    if not content or len(content) <= max_chars:
        return content

    # Try parsing structured JSON
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            # For file read operations
            if "content" in data and isinstance(data["content"], str) and len(data["content"]) > max_chars:
                lines = data["content"].splitlines()
                if len(lines) > 30:
                    preview = "\n".join(lines[:15]) + f"\n\n... [{len(lines) - 30} lines hidden for context economy] ...\n\n" + "\n".join(lines[-15:])
                else:
                    head = data["content"][:max_chars // 2]
                    tail = data["content"][-(max_chars // 2):]
                    preview = head + f"\n\n... [{len(data['content']) - max_chars} chars omitted] ...\n\n" + tail
                data["content"] = preview
                data["_compacted"] = True
                return json.dumps(data, ensure_ascii=False)
            # For bash command stdout
            if "stdout" in data and isinstance(data["stdout"], str) and len(data["stdout"]) > max_chars:
                out = data["stdout"]
                head = out[:max_chars // 2]
                tail = out[-(max_chars // 2):]
                data["stdout"] = head + f"\n\n... [{len(out) - max_chars} chars truncated] ...\n\n" + tail
                data["_compacted"] = True
                return json.dumps(data, ensure_ascii=False)
    except Exception:
        pass

    # Generic string clipping
    return clip_for_context(content, max_chars)


def extract_code_outline(code: str, file_path: str = "") -> str:
    """Extracts a structural symbol outline (classes, functions, methods, line numbers) using Tree-sitter AST."""
    if not code:
        return "[Empty file]"

    lines = code.splitlines()
    total_lines = len(lines)
    symbols: list[str] = []

    try:
        import tempfile
        from pathlib import Path
        from code_graph_service import code_graph_service
        
        # Only attempt if code-review-graph is actually available
        if code_graph_service.is_available():
            import code_review_graph.parser
            
            # Write to a temporary file with the correct extension so Tree-sitter detects language
            ext = os.path.splitext(file_path)[1] if file_path else ".py"
            with tempfile.NamedTemporaryFile(suffix=ext, mode='w', encoding='utf-8', delete=False) as f:
                f.write(code)
                temp_name = f.name
                
            try:
                parser = code_review_graph.parser.CodeParser()
                nodes, _ = parser.parse_file(Path(temp_name))
                for n in nodes:
                    if n.kind in ("Class", "Function", "Method", "Interface", "TypeAlias"):
                        indent = "  " if n.parent_name else ""
                        params = n.params or ""
                        symbols.append(f"{indent}Line {n.line_start}-{n.line_end}: {n.kind.lower()} {n.name}{params}")
            finally:
                os.unlink(temp_name)
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"CodeParser outline failed: {e}")

    # Fallback to simple regex if code_review_graph failed or found nothing
    if not symbols:
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            m = re.match(r"^(class\s+[a-zA-Z0-9_]+(?:\([^)]*\))?):", stripped)
            if m:
                symbols.append(f"Line {i}: {m.group(1)}")
                continue
            m = re.match(r"^((?:async\s+)?def\s+[a-zA-Z0-9_]+\([^)]*\)):", stripped)
            if m:
                indent = "  " if line.startswith(("    ", "\t")) else ""
                symbols.append(f"{indent}Line {i}: {m.group(1)}")
                continue
            m = re.match(r"^(?:export\s+)?(?:default\s+)?(?:class|function|interface|type)\s+([a-zA-Z0-9_]+)", stripped)
            if m:
                symbols.append(f"Line {i}: {stripped[:100]}...")
                continue
            m = re.match(r"^(?:export\s+)?(?:async\s+)?function\s+([a-zA-Z0-9_]+)\s*(\([^)]*\))", stripped)
            if m:
                symbols.append(f"Line {i}: function {m.group(1)}{m.group(2)}")
                continue
            m = re.match(r"^(?:export\s+)?(?:const|let|var)\s+([a-zA-Z0-9_]+)\s*=\s*(?:async\s*)?(\([^)]*\)|[a-zA-Z0-9_]+)\s*=>", stripped)
            if m:
                symbols.append(f"Line {i}: const {m.group(1)} = (...) =>")
                continue
            # JS / TS method
            m = re.match(r"^(?:async\s+)?([a-zA-Z0-9_]+)\s*(\([^)]*\))\s*\{?", stripped)
            if m and m.group(1) not in {"if", "for", "while", "switch", "catch", "return"}:
                prefix = "async " if stripped.startswith("async ") else ""
                symbols.append(f"Line {i}: {prefix}{m.group(1)}{m.group(2)}")
                continue
            # Rust / Go
            m = re.match(r"^(?:pub\s+)?(?:fn|func)\s+([a-zA-Z0-9_]+)", stripped)
            if m:
                symbols.append(f"Line {i}: {stripped[:60]}")
                continue

    header = f"[Structural Outline: {file_path or 'Source Code'} ({total_lines} lines)]"
    if not symbols:
        preview = "\n".join(lines[:15])
        return f"{header}\n(No major class/function declarations detected. Head preview):\n{preview}"

    out = header + "\n" + "\n".join(symbols)
    if len(out) > 8000:
        return out[:8000] + "\n... [outline truncated]"
    return out


def compress_source_code(code: str, mode: str = "clean") -> str:
    """
    Compresses source code to reduce token consumption inspired by LeanCTX:
    - 'outline' / 'map': returns structural symbol signatures and line numbers.
    - 'clean' / 'aggressive': strips comments, redundant whitespace, and blank lines.
    - 'lightweight': collapses consecutive blank lines.
    - 'raw': unchanged.
    """
    if not code:
        return code
    mode_str = str(mode or "clean").lower()
    if mode_str in {"outline", "map"}:
        return extract_code_outline(code)
    if mode_str in {"clean", "aggressive"}:
        lines = []
        in_multiline_comment = False
        for raw_line in code.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            # C-style multiline comments
            if in_multiline_comment:
                if "*/" in stripped:
                    in_multiline_comment = False
                continue
            if stripped.startswith("/*"):
                if "*/" not in stripped:
                    in_multiline_comment = True
                continue
            # Single-line comments (# or //)
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            # Keep non-empty lines
            if stripped:
                lines.append(line)
            elif lines and lines[-1] != "":
                lines.append("")
        return "\n".join(lines)
    if mode_str == "lightweight":
        lines = []
        for line in code.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(line.rstrip())
            elif lines and lines[-1] != "":
                lines.append("")
        return "\n".join(lines)
    return code


def compact_history_assistant_turns(
    messages: list[dict],
    keep_recent_assistant_code: int = 1,
    min_lines_to_collapse: int = 6,
) -> list[dict]:
    """
    In older assistant turns, collapses voluminous code blocks into compact stubs.
    Preserves full code in the most recent assistant turns.
    Prevents exponential token growth across multiple turns.
    """
    if not messages:
        return []

    # Find indices of assistant messages
    assistant_indices = [idx for idx, msg in enumerate(messages) if msg.get("role") == "assistant"]
    keep_set = set(assistant_indices[-keep_recent_assistant_code:]) if assistant_indices else set()

    result = []
    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant" or idx in keep_set:
            result.append(msg)
            continue

        content = str(msg.get("content") or "")

        def _collapse_code_block(match: re.Match) -> str:
            lang = match.group(1) or ""
            body = match.group(2)
            lines = body.strip().splitlines()
            if len(lines) <= min_lines_to_collapse:
                return match.group(0)
            first_two = "\n".join(lines[:2])
            return (
                f"```{lang}\n{first_two}\n"
                f"... [{len(lines) - 2} lines of {lang or 'code'} collapsed to save tokens. Current state is in workspace.]\n```"
            )

        compacted_content = re.sub(
            r"```([a-zA-Z0-9_\-\.]*)\n([\s\S]*?)```",
            _collapse_code_block,
            content,
        )

        copy_msg = dict(msg)
        copy_msg["content"] = compacted_content
        result.append(copy_msg)

    return result


def adaptive_compact_messages(
    messages: list[dict],
    token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
    model: str = "",
    keep_recent_full_turns: int = 2,
) -> list[dict]:
    """Adaptively compacts tool outputs and older turns to stay well within token_budget."""
    if not messages:
        return []

    # First collapse older assistant code blocks to reclaim massive token overhead
    messages = compact_history_assistant_turns(messages, keep_recent_assistant_code=1)

    compacted: list[dict] = []
    total_messages = len(messages)

    for idx, msg in enumerate(messages):
        is_recent = idx >= total_messages - (keep_recent_full_turns * 2)
        role = msg.get("role", "")
        content = msg.get("content", "")

        msg_copy = dict(msg)

        if role == "tool" or ("tool_calls" in msg and not is_recent):
            char_limit = 3500 if is_recent else 800
            msg_copy["content"] = compact_tool_output(content, max_chars=char_limit)
        elif role == "assistant" and not is_recent:
            msg_copy["content"] = clip_for_context(content, limit=MAX_ASSISTANT_HISTORY_CHARS // 2)
        elif role == "user" and not is_recent:
            msg_copy["content"] = clip_for_context(content, limit=MAX_HISTORY_MESSAGE_CHARS // 2)

        compacted.append(msg_copy)

    return compacted


_TIKTOKEN_CACHE: dict[str, Any] = {}


def _get_tiktoken_encoding(model: str = "") -> Any:
    global _TIKTOKEN_CACHE
    key = (model or "").lower().strip()
    if key in _TIKTOKEN_CACHE:
        return _TIKTOKEN_CACHE[key]
    try:
        import tiktoken
        if key:
            try:
                enc = tiktoken.encoding_for_model(key)
                _TIKTOKEN_CACHE[key] = enc
                return enc
            except Exception:
                pass
        if "cl100k_base" not in _TIKTOKEN_CACHE:
            _TIKTOKEN_CACHE["cl100k_base"] = tiktoken.get_encoding("cl100k_base")
        _TIKTOKEN_CACHE[key] = _TIKTOKEN_CACHE["cl100k_base"]
        return _TIKTOKEN_CACHE[key]
    except Exception:
        return None


def estimate_tokens_for_messages(messages: list[dict], model: str = "", litellm_counter: Callable | None = None) -> int:
    if litellm_counter:
        try:
            return int(litellm_counter(model=model, messages=messages))
        except Exception:
            pass
    enc = _get_tiktoken_encoding(model)
    if enc is not None:
        try:
            num_tokens = 3
            for message in messages:
                num_tokens += 3
                content = str(message.get("content", "") or "")
                num_tokens += len(enc.encode(content, disallowed_special=()))
                if "name" in message:
                    num_tokens += len(enc.encode(str(message["name"]), disallowed_special=()))
                if "tool_calls" in message:
                    num_tokens += len(enc.encode(str(message["tool_calls"]), disallowed_special=()))
            return max(1, num_tokens)
        except Exception:
            pass
    chars = sum(len(str(message.get("content", ""))) + 24 for message in messages)
    return max(1, chars // 4)


def estimate_tokens_for_text(text: str, model: str = "") -> int:
    if not text:
        return 0
    enc = _get_tiktoken_encoding(model)
    if enc is not None:
        try:
            return max(1, len(enc.encode(text, disallowed_special=())))
        except Exception:
            pass
    return max(1, len(text or "") // 4)


def fast_tokens_for_messages(messages: list[dict], model: str = "") -> int:
    return estimate_tokens_for_messages(messages, model=model)


def get_model_context_window(
    model: str,
    configured_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
    conn_mode: str = "",
    litellm_counter: Callable | None = None,
    litellm_cost_map: dict | None = None,
) -> tuple[int, str]:
    cleaned = (model or "").strip()
    budget = max(4_000, int(configured_budget or DEFAULT_CONTEXT_TOKEN_BUDGET))

    if litellm_counter and litellm_cost_map:
        candidates = [cleaned]
        if conn_mode == "🖥️ Local Ollama" and not cleaned.startswith("ollama/"):
            candidates.append(f"ollama/{cleaned}")
        for candidate in candidates:
            try:
                info = litellm_cost_map.get(candidate, {}) or {}
                max_input = int(info.get("max_input_tokens") or 0)
                if max_input > 0:
                    return max_input, "litellm"
            except Exception:
                pass

    lower = cleaned.lower()
    known_windows = {
        "gpt-4o": 128_000,
        "gpt-4o-mini": 128_000,
        "gpt-4.1": 1_000_000,
        "gpt-4.1-mini": 1_000_000,
        "gpt-4.1-nano": 1_000_000,
        "gpt-5": 400_000,
        "gpt-5-mini": 400_000,
        "gpt-5-nano": 400_000,
        "claude-3.5": 200_000,
        "claude-3-5": 200_000,
        "claude-3.7": 200_000,
        "claude-3-7": 200_000,
        "claude-sonnet-4": 200_000,
        "claude-opus-4": 200_000,
        "gemini-1.5": 1_000_000,
        "gemini-2.5": 1_000_000,
        "llama3.1": 128_000,
        "llama3.2": 128_000,
        "llama3.3": 128_000,
        "qwen2.5": 128_000,
        "qwen3": 128_000,
        "gemma4": 128_000,
        "gemma3": 128_000,
        "gpt-oss": 128_000,
    }
    for marker, window in known_windows.items():
        if marker in lower:
            return window, "estimated"
    return budget, "configured"


def message_summary_line(message: dict) -> str:
    role = message.get("role", "user")
    content = " ".join(str(message.get("content", "")).split())
    return f"- {role}: {clip_for_context(content, 700)}"
