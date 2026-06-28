"""
web_app.py - HTML/CSS UI server for Ollama Agentic Workspace.

This replaces the Streamlit surface with a small stdlib HTTP server while
reusing the existing workspace, tools, skills, and agent loop modules.
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import DEFAULT_SYSTEM_PROMPT, MODE_CUSTOM, MODE_LOCAL
from prompt_manager import get_prompt_manager
from skills_manager import get_skills_manager
from tools import TOOL_SCHEMAS, execute_tool, get_workspace, set_tavily_config, set_workspace, tool_scan_project


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)).resolve()
STATIC_DIR = ROOT / "web_ui"
HOST = os.getenv("WEB_APP_HOST", "127.0.0.1")
PORT = int(os.getenv("WEB_APP_PORT", "7864"))
DEFAULT_PROMPT_NAME = "custom_prompt"
DEFAULT_ACTIVE_PROMPT = (ROOT / "system_prompts" / f"{DEFAULT_PROMPT_NAME}.md").read_text(
    encoding="utf-8",
    errors="replace",
) if (ROOT / "system_prompts" / f"{DEFAULT_PROMPT_NAME}.md").exists() else DEFAULT_SYSTEM_PROMPT

MAX_CONTEXT_CHARS = 32_000
MAX_HISTORY_MESSAGE_CHARS = 6_000
MAX_ASSISTANT_HISTORY_CHARS = 3_500
MAX_KEPT_HISTORY_MESSAGES = 12
MAX_ITERATIONS = 10
REQUEST_TIMEOUT = int(os.getenv("AGENT_REQUEST_TIMEOUT", "1800"))
DEFAULT_CONTEXT_TOKEN_BUDGET = int(os.getenv("AGENT_CONTEXT_TOKENS", "24000"))
DEFAULT_RESPONSE_TOKEN_BUDGET = int(os.getenv("AGENT_RESPONSE_TOKENS", "8192"))
MAX_AUTO_CONTINUES = int(os.getenv("AGENT_AUTO_CONTINUES", "2"))

try:
    from litellm import token_counter as _litellm_token_counter
    _litellm_import_error = ""
except Exception as exc:
    _litellm_token_counter = None
    _litellm_import_error = repr(exc)

STATE = {
    "messages": [],
    "tools_log": [],
    "used_skills_log": [],
    "selected_skills": [],
    "conn_mode": MODE_LOCAL,
    "model": os.getenv("OLLAMA_MODEL", "llama3"),
    "temperature": 0.4,
    "enable_thinking": False,
    "custom_api_url": "https://api.openai.com/v1",
    "custom_api_key": "",
    "selected_prompt": DEFAULT_PROMPT_NAME,
    "system_prompt": DEFAULT_ACTIVE_PROMPT,
    "model_user_selected": False,
    "memory_enabled": True,
    "memory_summary": "",
    "memory_summarized_count": 0,
    "context_token_budget": DEFAULT_CONTEXT_TOKEN_BUDGET,
    "response_token_budget": DEFAULT_RESPONSE_TOKEN_BUDGET,
    "auto_continue": True,
    "tavily_enabled": os.getenv("TAVILY_ENABLED", "false").lower() == "true",
    "tavily_api_key": os.getenv("TAVILY_API_KEY", ""),
}

sm = get_skills_manager()
pm = get_prompt_manager()


AGENT_WORKFLOW_PROMPT = """

---
## Agent Workspace Workflow

When the user asks for project work:
1. Inspect the project first with `scan_project` or `list_files`.
2. Read the relevant files before changing anything.
3. Make the requested code changes with the available tools.
4. Use `search_files`, `read_many_files`, `replace_in_file`, and `project_tree` when they make codebase work faster and more precise.
5. If Tavily web tools are enabled, use `web_search` for current internet information and `extract_url` when the user gives a URL or asks for web-backed research. If they are not available, explain that Tavily must be enabled in Settings.
6. For large code changes, write/edit files with tools instead of printing entire files in chat.
7. In the final answer, clearly report:
   - what changed
   - which files were changed
   - any command/test result you ran
If you did not change files, say that explicitly.
---
"""


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def _active_tool_schemas() -> list[dict]:
    tavily_names = {"web_search", "extract_url"}
    if STATE.get("tavily_enabled") and STATE.get("tavily_api_key"):
        return TOOL_SCHEMAS
    return [
        schema for schema in TOOL_SCHEMAS
        if schema.get("function", {}).get("name") not in tavily_names
    ]


def _sync_tool_settings() -> None:
    set_tavily_config(bool(STATE.get("tavily_enabled")), STATE.get("tavily_api_key", ""))


def _send_json(handler: BaseHTTPRequestHandler, data, status: int = 200) -> None:
    payload = json.dumps(data, ensure_ascii=False, default=_json_default).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _write_stream_event(handler: BaseHTTPRequestHandler, event: dict) -> None:
    payload = (json.dumps(event, ensure_ascii=False, default=_json_default) + "\n").encode("utf-8")
    handler.wfile.write(payload)
    handler.wfile.flush()


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8") or "{}")


def _clip_for_context(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(0, limit // 2)
    tail = max(0, limit - head - 120)
    return (
        text[:head]
        + f"\n\n...[context clipped: {len(text) - head - tail:,} chars omitted]...\n\n"
        + text[-tail:]
    )


def _estimate_tokens_for_messages(messages: list[dict]) -> int:
    if _litellm_token_counter:
        try:
            return int(_litellm_token_counter(model=STATE.get("model") or "gpt-4o-mini", messages=messages))
        except Exception:
            pass
    chars = sum(len(str(message.get("content", ""))) + 24 for message in messages)
    return max(1, chars // 4)


def _estimate_tokens_for_text(text: str) -> int:
    return max(1, len(text or "") // 4)


def _message_summary_line(message: dict) -> str:
    role = message.get("role", "user")
    content = " ".join(str(message.get("content", "")).split())
    return f"- {role}: {_clip_for_context(content, 700)}"


def _compact_memory_if_needed() -> None:
    if not STATE.get("memory_enabled"):
        return

    messages = STATE["messages"]
    target_count = max(MAX_KEPT_HISTORY_MESSAGES + 2, 18)
    if len(messages) <= target_count:
        return

    keep_tail = MAX_KEPT_HISTORY_MESSAGES
    to_summarize = messages[:-keep_tail]
    tail = messages[-keep_tail:]
    removed_assistants = sum(1 for message in to_summarize if message.get("role") == "assistant")
    existing = STATE.get("memory_summary", "").strip()
    lines = []
    if existing:
        lines.append(existing)
    lines.append(f"\n## Memory compacted at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.extend(_message_summary_line(message) for message in to_summarize)
    summary = "\n".join(lines).strip()
    STATE["memory_summary"] = _clip_for_context(summary, 12_000)
    STATE["memory_summarized_count"] = int(STATE.get("memory_summarized_count", 0)) + len(to_summarize)
    STATE["messages"] = tail
    if removed_assistants:
        STATE["tools_log"] = STATE["tools_log"][removed_assistants:]
        STATE["used_skills_log"] = STATE["used_skills_log"][removed_assistants:]


def _message_for_context(message: dict) -> dict:
    role = message.get("role", "user")
    content = message.get("content", "")
    limit = MAX_ASSISTANT_HISTORY_CHARS if role == "assistant" else MAX_HISTORY_MESSAGE_CHARS
    return {"role": role, "content": _clip_for_context(content, limit)}


def _build_workspace_context(active_context: dict | None = None) -> str:
    ws = get_workspace()
    snapshot = _workspace_snapshot()
    lines = [
        "[Workspace context]",
        f"Current workspace path: {ws}",
        f"Workspace stats: {snapshot['stats']['files']} files, {snapshot['stats']['kb']}KB, {snapshot['stats']['types']} file types.",
        "When the user asks to change code, inspect/read/write files using tools. Do not ask the user to paste code that is already in the workspace.",
    ]

    if active_context:
        path = str(active_context.get("path") or "").strip()
        content = str(active_context.get("content") or "")
        if path and content:
            lines += [
                "",
                "[Active file selected in UI]",
                f"Path: {path}",
                "The user's next request is about this file unless they explicitly say otherwise.",
                "Use `write_file` or `replace_in_file` to apply requested changes to this path when appropriate.",
                "",
                "```",
                _clip_for_context(content, 18_000),
                "```",
            ]
    return "\n".join(lines)


def _build_final_system_prompt() -> str:
    final = STATE.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
    for active_name in STATE["selected_skills"]:
        skill = sm.get(active_name)
        if skill:
            final += skill.system_injection
    final += AGENT_WORKFLOW_PROMPT
    final += sm.build_auto_select_prompt()
    return final


def _build_api_messages(final_system: str) -> list[dict]:
    _compact_memory_if_needed()
    target_tokens = max(4_000, int(STATE.get("context_token_budget") or DEFAULT_CONTEXT_TOKEN_BUDGET))
    system_tokens = _estimate_tokens_for_text(final_system)
    budget = max(2_000, (target_tokens - system_tokens) * 4)
    selected_reversed: list[dict] = []
    used = 0

    recent = STATE["messages"][-MAX_KEPT_HISTORY_MESSAGES:]
    for message in reversed(recent):
        compact = _message_for_context(message)
        size = len(compact["content"]) + 32
        if selected_reversed and used + size > budget:
            break
        selected_reversed.append(compact)
        used += size

    selected = list(reversed(selected_reversed))
    omitted = len(STATE["messages"]) - len(selected) + int(STATE.get("memory_summarized_count", 0))
    memory_summary = STATE.get("memory_summary", "").strip()
    if memory_summary:
        selected.insert(0, {
            "role": "system",
            "content": (
                "[Long-term conversation memory]\n"
                f"{memory_summary}\n\n"
                "Use this as compressed context. Recent messages below are more authoritative."
            ),
        })
    elif omitted > 0:
        selected.insert(0, {
            "role": "system",
            "content": f"[Context note: {omitted} older chat message(s) were omitted to stay within the model context window.]",
        })
    return [{"role": "system", "content": final_system}] + selected


def _workspace_snapshot() -> dict:
    ws = get_workspace()
    ignore = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode", "dist", "build", ".next"}
    files = []
    total_size = 0
    ext_set = set()
    try:
        for p in sorted(ws.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ws)
            if any(part in ignore for part in rel.parts):
                continue
            size = p.stat().st_size
            total_size += size
            if p.suffix:
                ext_set.add(p.suffix.lower())
            files.append({"path": str(rel), "size": size, "ext": p.suffix.lower()})
    except Exception:
        files = []
    return {
        "path": str(ws),
        "files": files[:1000],
        "stats": {"files": len(files), "kb": total_size // 1024, "types": len(ext_set)},
    }


def _read_file(rel_path: str) -> dict:
    ws = get_workspace()
    target = (ws / rel_path).resolve()
    if not str(target).startswith(str(ws.resolve())):
        raise PermissionError("Path outside workspace is not allowed")
    text = target.read_text(encoding="utf-8", errors="replace")
    return {"path": rel_path, "content": text, "size": target.stat().st_size, "ext": target.suffix.lstrip(".") or "text"}


def _browse_local_folder(initial_dir: str = "") -> str | None:
    """Open a native folder picker on the machine running this local web server."""
    start_dir = initial_dir if initial_dir and Path(initial_dir).exists() else str(Path.home())
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=start_dir,
            title="Select project folder",
        )
        root.destroy()
        if selected:
            return selected
    except Exception:
        pass

    if os.name != "nt":
        return None

    powershell = _windows_powershell_path()
    if not powershell:
        return None

    script = r"""
$initial = $args[0]
$shell = New-Object -ComObject Shell.Application
$folder = $shell.BrowseForFolder(0, "Select project folder", 0, $initial)
if ($folder -and $folder.Self -and $folder.Self.Path) {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Write-Output $folder.Self.Path
}
"""
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
                start_dir,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        selected = result.stdout.strip().splitlines()
        return selected[-1].strip() if selected else None
    except Exception:
        return None


def _windows_powershell_path() -> str | None:
    """Return a stable PowerShell path for packaged Windows builds."""
    candidates = []
    for root in (os.environ.get("SystemRoot"), os.environ.get("WINDIR"), r"C:\Windows"):
        if root:
            candidates.append(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
            candidates.append(Path(root) / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json_stream(url: str, payload: dict, headers: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            yield json.loads(line)


def _format_agent_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout) or "timed out" in str(exc).lower():
        return (
            "Agent runtime error. The selected model did not return a response before the timeout.\n\n"
            f"Current timeout: {REQUEST_TIMEOUT} seconds.\n"
            "Try a smaller/faster local model, reduce the prompt/context, or wait and retry if Ollama is still loading the model."
        )
    return (
        "Agent runtime error. Make sure Ollama or the selected API endpoint is reachable.\n\n"
        f"{exc}"
    )


def _get_json(url: str, headers: dict | None = None, timeout: int = 8) -> dict:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _available_models() -> dict:
    conn_mode = STATE["conn_mode"]
    try:
        if conn_mode == MODE_LOCAL:
            data = _get_json("http://127.0.0.1:11434/api/tags")
            raw_models = data.get("models", [])
            names = [m.get("model") or m.get("name") for m in raw_models if isinstance(m, dict)]
        else:
            headers = {}
            if STATE["custom_api_key"]:
                headers["Authorization"] = f"Bearer {STATE['custom_api_key']}"
            data = _get_json(f"{STATE['custom_api_url'].rstrip('/')}/models", headers=headers)
            raw_models = data.get("data", []) if isinstance(data, dict) else []
            names = [m.get("id") or m.get("name") for m in raw_models if isinstance(m, dict)]
        names = [name for name in names if name]
        preferred = next((name for name in names if not name.endswith(":cloud")), names[0] if names else STATE["model"])
        if names and (STATE["model"] not in names or (not STATE["model_user_selected"] and STATE["model"].endswith(":cloud"))):
            STATE["model"] = preferred
        return {"models": names, "selected_model": STATE["model"], "error": None}
    except Exception as exc:
        return {"models": [], "selected_model": STATE["model"], "error": str(exc)}


def _prompt_payload() -> dict:
    return {
        "prompts": [
            {
                "name": prompt.name,
                "category": prompt.category,
                "preview": prompt.preview,
                "size": prompt.size,
            }
            for prompt in pm.all()
        ],
        "selected_prompt": STATE["selected_prompt"],
        "system_prompt": STATE["system_prompt"],
    }


def _skills_diagnostics() -> dict:
    skills = sm.all()
    names = [skill.name for skill in skills]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    missing_description = [skill.name for skill in skills if not skill.description.strip()]
    selected_missing = [name for name in STATE["selected_skills"] if not sm.get(name)]
    slash_tests = []
    for skill in skills[:50]:
        detected = sm.detect_skill_commands(f"/{skill.name} test")
        slash_tests.append({
            "name": skill.name,
            "ok": bool(detected and detected[0].name == skill.name),
        })
    failed_slash = [item["name"] for item in slash_tests if not item["ok"]]
    categories: dict[str, int] = {}
    for skill in skills:
        categories[skill.category] = categories.get(skill.category, 0) + 1
    return {
        "ok": not duplicates and not missing_description and not selected_missing and not failed_slash,
        "count": len(skills),
        "categories": categories,
        "duplicates": duplicates,
        "missing_description": missing_description,
        "selected_missing": selected_missing,
        "failed_slash_detection": failed_slash,
        "selected_skills": STATE["selected_skills"],
        "auto_select_prompt_chars": len(sm.build_auto_select_prompt()),
    }


def _extract_tool_calls_ollama(message: dict) -> list[dict]:
    calls = []
    for tc in message.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append({"name": fn.get("name", ""), "arguments": args})
    return calls


def _extract_tool_calls_openai(message: dict) -> list[dict]:
    calls = []
    for tc in message.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        args = fn.get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append({"id": tc.get("id"), "type": tc.get("type", "function"), "name": fn.get("name", ""), "arguments": args})
    return calls


def _format_tool_calls_for_history(conn_mode: str, tool_calls: list[dict]) -> list[dict]:
    formatted = []
    for index, tc in enumerate(tool_calls):
        name = tc.get("name", "")
        arguments = tc.get("arguments", {}) or {}
        if conn_mode == MODE_LOCAL:
            formatted.append({"function": {"name": name, "arguments": arguments}})
        else:
            formatted.append({
                "id": tc.get("id") or f"call_{index}",
                "type": tc.get("type", "function"),
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            })
    return formatted


def _call_model(history: list[dict]) -> dict:
    response_budget = max(512, int(STATE.get("response_token_budget") or DEFAULT_RESPONSE_TOKEN_BUDGET))
    conn_mode = STATE["conn_mode"]
    if conn_mode == MODE_LOCAL:
        data = _post_json(
            "http://127.0.0.1:11434/api/chat",
            {
                "model": STATE["model"],
                "messages": history,
                "tools": _active_tool_schemas(),
                "think": bool(STATE["enable_thinking"]),
                "stream": False,
                "options": {"temperature": float(STATE["temperature"]), "num_predict": response_budget},
            },
        )
        msg = data.get("message", {})
        return {
            "content": msg.get("content", "") or "",
            "thinking": msg.get("thinking", "") or "",
            "tool_calls": _extract_tool_calls_ollama(msg),
            "finish_reason": data.get("done_reason") or "",
        }

    headers = {}
    if STATE["custom_api_key"]:
        headers["Authorization"] = f"Bearer {STATE['custom_api_key']}"
    data = _post_json(
        f"{STATE['custom_api_url'].rstrip('/')}/chat/completions",
        {
            "model": STATE["model"],
            "messages": history,
            "tools": _active_tool_schemas(),
            "temperature": float(STATE["temperature"]),
            "stream": False,
            "max_tokens": response_budget,
        },
        headers=headers,
    )
    choices = data.get("choices", [])
    msg = choices[0].get("message", {}) if choices else {}
    finish_reason = choices[0].get("finish_reason", "") if choices else ""
    return {
        "content": msg.get("content", "") or "",
        "thinking": msg.get("reasoning_content", "") or "",
        "tool_calls": _extract_tool_calls_openai(msg),
        "finish_reason": finish_reason or "",
    }


def _merge_custom_tool_delta(tool_calls: dict, delta_calls: list[dict]) -> None:
    for delta in delta_calls or []:
        index = int(delta.get("index", len(tool_calls)))
        current = tool_calls.setdefault(index, {
            "id": delta.get("id"),
            "type": delta.get("type", "function"),
            "function": {"name": "", "arguments": ""},
        })
        if delta.get("id"):
            current["id"] = delta["id"]
        if delta.get("type"):
            current["type"] = delta["type"]
        fn = delta.get("function") or {}
        if fn.get("name"):
            current["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            current["function"]["arguments"] += fn["arguments"]


def _call_model_stream(history: list[dict], write_event) -> dict:
    response_budget = max(512, int(STATE.get("response_token_budget") or DEFAULT_RESPONSE_TOKEN_BUDGET))
    conn_mode = STATE["conn_mode"]
    content_parts: list[str] = []
    thinking_parts: list[str] = []

    if conn_mode == MODE_LOCAL:
        tool_calls_raw: list[dict] = []
        finish_reason = ""
        for event in _post_json_stream(
            "http://127.0.0.1:11434/api/chat",
            {
                "model": STATE["model"],
                "messages": history,
                "tools": _active_tool_schemas(),
                "think": bool(STATE["enable_thinking"]),
                "stream": True,
                "options": {"temperature": float(STATE["temperature"]), "num_predict": response_budget},
            },
        ):
            if event.get("done_reason"):
                finish_reason = event.get("done_reason") or ""
            msg = event.get("message", {}) if isinstance(event, dict) else {}
            thinking = msg.get("thinking", "") or ""
            content = msg.get("content", "") or ""
            if thinking:
                thinking_parts.append(thinking)
            if content:
                content_parts.append(content)
                write_event({"type": "token", "content": content})
            if msg.get("tool_calls"):
                tool_calls_raw.extend(msg.get("tool_calls") or [])
        return {
            "content": "".join(content_parts),
            "thinking": "".join(thinking_parts),
            "tool_calls": _extract_tool_calls_ollama({"tool_calls": tool_calls_raw}),
            "finish_reason": finish_reason,
        }

    headers = {}
    if STATE["custom_api_key"]:
        headers["Authorization"] = f"Bearer {STATE['custom_api_key']}"
    tool_call_deltas: dict[int, dict] = {}
    finish_reason = ""
    for event in _post_json_stream(
        f"{STATE['custom_api_url'].rstrip('/')}/chat/completions",
        {
            "model": STATE["model"],
            "messages": history,
            "tools": _active_tool_schemas(),
            "temperature": float(STATE["temperature"]),
            "stream": True,
            "max_tokens": response_budget,
        },
        headers=headers,
    ):
        choices = event.get("choices", []) if isinstance(event, dict) else []
        if not choices:
            continue
        finish_reason = choices[0].get("finish_reason") or finish_reason
        delta = choices[0].get("delta", {}) or {}
        content = delta.get("content") or ""
        thinking = delta.get("reasoning_content") or ""
        if thinking:
            thinking_parts.append(thinking)
        if content:
            content_parts.append(content)
            write_event({"type": "token", "content": content})
        _merge_custom_tool_delta(tool_call_deltas, delta.get("tool_calls") or [])

    return {
        "content": "".join(content_parts),
        "thinking": "".join(thinking_parts),
        "tool_calls": _extract_tool_calls_openai({"tool_calls": [tool_call_deltas[i] for i in sorted(tool_call_deltas)]}),
        "finish_reason": finish_reason,
    }


def _hit_generation_limit(result: dict) -> bool:
    reason = str(result.get("finish_reason", "") or "").lower()
    return reason in {"length", "max_tokens"} or "length" in reason or "limit" in reason


def _continuation_prompt() -> str:
    return (
        "Continue exactly from where you stopped. Do not restart, do not summarize, "
        "and do not repeat previous text. If you were writing a code block, continue the same code block."
    )


def _run_agent_loop(api_messages: list[dict]) -> tuple[str, str, list[dict]]:
    history = list(api_messages)
    response_text = ""
    thinking_text = ""
    tools_done: list[dict] = []

    auto_continues = 0
    for _ in range(MAX_ITERATIONS):
        result = _call_model(history)
        thinking_text += result.get("thinking", "") or ""
        if result["tool_calls"]:
            if result["content"]:
                response_text += result["content"]
            history.append({
                "role": "assistant",
                "content": result["content"] or "",
                "tool_calls": _format_tool_calls_for_history(STATE["conn_mode"], result["tool_calls"]),
            })
            for index, tc in enumerate(result["tool_calls"]):
                name = tc["name"]
                args = tc["arguments"]
                tool_output = execute_tool(name, args)
                tools_done.append({"name": name, "args": args, "result": tool_output})
                tool_message = {"role": "tool", "content": tool_output, "name": name}
                if STATE["conn_mode"] == MODE_CUSTOM:
                    tool_message["tool_call_id"] = tc.get("id") or f"call_{index}"
                history.append(tool_message)
            continue
        response_text += result["content"]
        if STATE.get("auto_continue") and _hit_generation_limit(result) and auto_continues < MAX_AUTO_CONTINUES:
            auto_continues += 1
            history.append({"role": "assistant", "content": result["content"] or ""})
            history.append({"role": "user", "content": _continuation_prompt()})
            continue
        return response_text, thinking_text, tools_done

    response_text += f"\n\nAgent stopped after {MAX_ITERATIONS} tool iterations."
    return response_text, thinking_text, tools_done


def _run_agent(prompt: str, active_context: dict | None = None) -> dict:
    triggered = sm.detect_skill_commands(prompt)
    if triggered:
        existing = list(STATE["selected_skills"])
        for skill in triggered:
            if skill.name not in existing:
                existing.append(skill.name)
        STATE["selected_skills"] = existing
        clean_prompt = sm.strip_commands(prompt) or "Use these skills: " + ", ".join(f"/{s.name}" for s in triggered)
    else:
        clean_prompt = prompt

    _sync_tool_settings()
    model_prompt = f"{_build_workspace_context(active_context)}\n\n[User request]\n{clean_prompt}"
    STATE["messages"].append({"role": "user", "content": clean_prompt})
    final_system = _build_final_system_prompt()
    api_messages = _build_api_messages(final_system)
    api_messages[-1] = {"role": "user", "content": model_prompt}

    try:
        response_text, thinking_text, tools_done = _run_agent_loop(api_messages)
    except Exception as exc:
        response_text = _format_agent_error(exc)
        thinking_text = ""
        tools_done = []

    used_skills = sm.parse_used_skills(response_text)
    clean_response = sm.strip_skill_tag(response_text)
    STATE["messages"].append({"role": "assistant", "content": clean_response})
    STATE["tools_log"].append(tools_done)
    STATE["used_skills_log"].append([s.name for s in used_skills])
    return {
        "message": {"role": "assistant", "content": clean_response},
        "thinking": thinking_text,
        "tools": tools_done,
        "used_skills": [s.name for s in used_skills],
        "state": _client_state(),
    }


def _chunk_text(text: str, size: int = 90):
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _run_agent_stream(prompt: str, write_event, active_context: dict | None = None) -> None:
    triggered = sm.detect_skill_commands(prompt)
    if triggered:
        existing = list(STATE["selected_skills"])
        for skill in triggered:
            if skill.name not in existing:
                existing.append(skill.name)
        STATE["selected_skills"] = existing
        clean_prompt = sm.strip_commands(prompt) or "Use these skills: " + ", ".join(f"/{s.name}" for s in triggered)
    else:
        clean_prompt = prompt

    _sync_tool_settings()
    model_prompt = f"{_build_workspace_context(active_context)}\n\n[User request]\n{clean_prompt}"
    STATE["messages"].append({"role": "user", "content": clean_prompt})
    write_event({"type": "state", "state": _client_state()})
    write_event({"type": "status", "message": "Preparing context..."})

    final_system = _build_final_system_prompt()
    history = _build_api_messages(final_system)
    history[-1] = {"role": "user", "content": model_prompt}
    response_text = ""
    thinking_text = ""
    tools_done: list[dict] = []
    auto_continues = 0

    try:
        for iteration in range(MAX_ITERATIONS):
            write_event({
                "type": "status",
                "message": f"Waiting for {STATE['model']} ({iteration + 1}/{MAX_ITERATIONS}, timeout {REQUEST_TIMEOUT}s)...",
            })
            result = _call_model_stream(history, write_event)
            thinking_text += result.get("thinking", "") or ""

            if result["tool_calls"]:
                if result["content"]:
                    response_text += result["content"]

                history.append({
                    "role": "assistant",
                    "content": result["content"] or "",
                    "tool_calls": _format_tool_calls_for_history(STATE["conn_mode"], result["tool_calls"]),
                })

                for index, tc in enumerate(result["tool_calls"]):
                    name = tc["name"]
                    args = tc["arguments"]
                    write_event({"type": "tool_call", "name": name, "args": args})
                    tool_output = execute_tool(name, args)
                    tools_done.append({"name": name, "args": args, "result": tool_output})
                    write_event({"type": "tool_result", "name": name, "result": tool_output})
                    tool_message = {"role": "tool", "content": tool_output, "name": name}
                    if STATE["conn_mode"] == MODE_CUSTOM:
                        tool_message["tool_call_id"] = tc.get("id") or f"call_{index}"
                    history.append(tool_message)
                continue

            response_text += result["content"]
            if STATE.get("auto_continue") and _hit_generation_limit(result) and auto_continues < MAX_AUTO_CONTINUES:
                auto_continues += 1
                write_event({
                    "type": "status",
                    "message": f"Output hit token limit; continuing automatically ({auto_continues}/{MAX_AUTO_CONTINUES})...",
                })
                history.append({"role": "assistant", "content": result["content"] or ""})
                history.append({"role": "user", "content": _continuation_prompt()})
                continue
            break
        else:
            response_text += f"\n\nAgent stopped after {MAX_ITERATIONS} tool iterations."
            write_event({"type": "token", "content": f"\n\nAgent stopped after {MAX_ITERATIONS} tool iterations."})
    except Exception as exc:
        response_text = _format_agent_error(exc)
        tools_done = []
        thinking_text = ""
        write_event({"type": "error", "message": response_text})

    used_skills = sm.parse_used_skills(response_text)
    clean_response = sm.strip_skill_tag(response_text)
    STATE["messages"].append({"role": "assistant", "content": clean_response})
    STATE["tools_log"].append(tools_done)
    STATE["used_skills_log"].append([s.name for s in used_skills])
    write_event({
        "type": "done",
        "thinking": thinking_text,
        "tools": tools_done,
        "used_skills": [s.name for s in used_skills],
        "state": _client_state(),
    })


def _client_state() -> dict:
    models_payload = _available_models()
    return {
        "workspace": _workspace_snapshot(),
        "messages": STATE["messages"],
        "tools_log": STATE["tools_log"],
        "selected_skills": STATE["selected_skills"],
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "category": s.category,
                "disabled": s.disable_model_invocation,
            }
            for s in sm.all()
        ],
        "settings": {
            "conn_mode": STATE["conn_mode"],
            "model": STATE["model"],
            "temperature": STATE["temperature"],
            "enable_thinking": STATE["enable_thinking"],
            "custom_api_url": STATE["custom_api_url"],
            "selected_prompt": STATE["selected_prompt"],
            "system_prompt": STATE["system_prompt"],
            "memory_enabled": STATE["memory_enabled"],
            "context_token_budget": STATE["context_token_budget"],
            "response_token_budget": STATE["response_token_budget"],
            "auto_continue": STATE["auto_continue"],
            "tavily_enabled": STATE["tavily_enabled"],
            "tavily_key_set": bool(STATE.get("tavily_api_key")),
        },
        "memory": {
            "enabled": STATE["memory_enabled"],
            "summary_chars": len(STATE.get("memory_summary", "")),
            "summarized_messages": STATE.get("memory_summarized_count", 0),
            "token_counter": "litellm" if _litellm_token_counter else "estimated",
            "token_counter_error": _litellm_import_error,
            "visible_messages": len(STATE["messages"]),
        },
        "prompts": _prompt_payload(),
        "models": models_payload,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            _send_json(self, _client_state())
            return
        if path == "/api/models":
            _send_json(self, _available_models())
            return
        if path == "/api/prompts":
            _send_json(self, _prompt_payload())
            return
        if path == "/api/skills/diagnostics":
            _send_json(self, _skills_diagnostics())
            return
        if path == "/api/prompt":
            name = parse_qs(parsed.query).get("name", [""])[0]
            prompt = pm.get(name)
            if not prompt:
                _send_json(self, {"error": "Prompt not found"}, 404)
                return
            _send_json(self, {
                "name": prompt.name,
                "category": prompt.category,
                "content": prompt.content,
                "preview": prompt.preview,
                "size": prompt.size,
            })
            return
        if path == "/api/file":
            rel = parse_qs(parsed.query).get("path", [""])[0]
            try:
                _send_json(self, _read_file(rel))
            except Exception as exc:
                _send_json(self, {"error": str(exc)}, 400)
            return
        if path == "/api/browse":
            picked = _browse_local_folder(str(get_workspace()))
            if picked:
                set_workspace(picked)
            body = (
                "<!doctype html><meta charset='utf-8'>"
                "<script>location.replace('/');</script>"
                "<body>Returning to workspace...</body>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/":
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists() or not target.is_file():
            self.send_error(404)
            return
        content = target.read_bytes()
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = _read_json(self)
            if path == "/api/workspace":
                ok, msg = set_workspace(data.get("path", ""))
                _send_json(self, {"ok": ok, "message": msg, "workspace": _workspace_snapshot()}, 200 if ok else 400)
                return
            if path == "/api/browse":
                picked = _browse_local_folder(data.get("initial_dir") or str(get_workspace()))
                if not picked:
                    _send_json(self, {"ok": False, "cancelled": True, "message": "Folder selection was cancelled"})
                    return
                ok, msg = set_workspace(picked)
                _send_json(
                    self,
                    {"ok": ok, "message": msg, "workspace": _workspace_snapshot(), "path": picked},
                    200 if ok else 400,
                )
                return
            if path == "/api/settings":
                for key in (
                    "conn_mode", "model", "temperature", "enable_thinking",
                    "custom_api_url", "custom_api_key", "memory_enabled",
                    "context_token_budget", "response_token_budget", "auto_continue",
                    "tavily_enabled", "tavily_api_key",
                ):
                    if key in data:
                        STATE[key] = data[key]
                if "tavily_api_key" in data:
                    STATE["tavily_api_key"] = str(data.get("tavily_api_key") or "").strip()
                if not STATE.get("tavily_enabled"):
                    STATE["tavily_api_key"] = ""
                _sync_tool_settings()
                for key in ("context_token_budget", "response_token_budget"):
                    if key in STATE:
                        STATE[key] = max(512, int(STATE[key]))
                if "model" in data:
                    STATE["model_user_selected"] = True
                _send_json(self, _client_state())
                return
            if path == "/api/prompt":
                if data.get("selected_prompt"):
                    prompt = pm.get(data["selected_prompt"])
                    if not prompt:
                        _send_json(self, {"error": "Prompt not found"}, 404)
                        return
                    STATE["selected_prompt"] = prompt.name
                    STATE["system_prompt"] = prompt.content
                elif "system_prompt" in data:
                    STATE["selected_prompt"] = None
                    STATE["system_prompt"] = data.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
                _send_json(self, _client_state())
                return
            if path == "/api/skills":
                names = data.get("selected_skills", [])
                STATE["selected_skills"] = [name for name in names if sm.get(name)]
                _send_json(self, _client_state())
                return
            if path == "/api/memory/compact":
                _compact_memory_if_needed()
                _send_json(self, _client_state())
                return
            if path == "/api/scan":
                result = tool_scan_project(int(data.get("max_files", 200)))
                STATE["messages"].append({"role": "user", "content": "Scan & Report"})
                STATE["messages"].append({"role": "assistant", "content": result})
                STATE["tools_log"].append([{"name": "scan_project", "args": {"max_files": data.get("max_files", 200)}, "result": result}])
                _send_json(self, {"result": result, "state": _client_state()})
                return
            if path == "/api/chat":
                _send_json(self, _run_agent(data.get("prompt", ""), data.get("active_context")))
                return
            if path == "/api/chat_stream":
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                def write_event(event: dict) -> None:
                    _write_stream_event(self, event)

                _run_agent_stream(data.get("prompt", ""), write_event, data.get("active_context"))
                return
            if path == "/api/clear":
                STATE["messages"].clear()
                STATE["tools_log"].clear()
                STATE["used_skills_log"].clear()
                STATE["memory_summary"] = ""
                STATE["memory_summarized_count"] = 0
                _send_json(self, _client_state())
                return
            _send_json(self, {"error": "Not found"}, 404)
        except Exception as exc:
            _send_json(self, {"error": str(exc)}, 500)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[web_app] {self.address_string()} - {fmt % args}")


def main() -> None:
    STATIC_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Web UI running at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
