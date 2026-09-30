"""
web_app.py - HTML/CSS UI server for Ollama Agentic Workspace.

This replaces the Streamlit surface with a small stdlib HTTP server while
reusing the existing workspace, tools, skills, and agent loop modules.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from coderai.core.agent_runtime import LangChainRuntime, RuntimeSettings
from coderai.core.config import DEFAULT_SYSTEM_PROMPT, MODE_CUSTOM, MODE_LOCAL
from coderai.core.prompt_manager import get_prompt_manager
from coderai.skills.skills_manager import get_skills_manager
from coderai.codebase.git_manager import GitManager, GitError
from coderai.memory.memory_manager import MemoryManager
from coderai.memory.memory_graph import GraphMemoryStore
from coderai.skills.skill_tracker import SkillTracker
from coderai.skills.skill_router import SkillRouter
from coderai.codebase.codebase_index import CodebaseIndex, get_codebase_index
from coderai.codebase.workspace_filter import iter_workspace_files
from coderai.tools.terminal_manager import terminal_manager
from coderai.memory.vector_store import EmbeddingModelManager
from coderai.tools.tools import (
    TOOL_SCHEMAS, execute_tool, get_workspace, set_tavily_config, set_workspace,
    tool_scan_project, get_approval_state, approve_pending, reject_pending, clear_approval_state,
    get_approval_decision,
    set_git_config, set_tool_event_sink, set_sandbox_config,
    _gate_approval, ToolApprovalRequired, _is_destructive_command,
)
from coderai.core.context_builder import (
    clip_for_context, estimate_tokens_for_messages, estimate_tokens_for_text,
    fast_tokens_for_messages, get_model_context_window, message_summary_line,
    adaptive_compact_messages, compact_tool_output,
    extract_code_outline, compact_history_assistant_turns,
)
from coderai.tools.tool_parser import repair_json_tool_arguments, extract_fallback_tool_calls_from_text
from coderai.core.session_manager import SessionStore, build_project_cards
from plan_mode.http_api import dispatch as plan_dispatch
from plan_mode.adapters import build_default_service

APPROVAL_POLL_INTERVAL = float(os.getenv("AGENT_APPROVAL_POLL_INTERVAL", "1.0"))
APPROVAL_TIMEOUT = int(os.getenv("AGENT_APPROVAL_TIMEOUT", "600"))

# The server has no auth layer, so it is only safe when bound to loopback
# (the default WEB_APP_HOST=127.0.0.1). Make that explicit: by default, refuse
# any client that is not local. An operator who sets WEB_APP_HOST=0.0.0.0 must
# also set WEB_APP_LOOPBACK_ONLY=false to accept remote clients.
_LOOPBACK_ONLY = os.getenv("WEB_APP_LOOPBACK_ONLY", "true").lower() == "true"


def _is_loopback_client(host: str) -> bool:
    if not host:
        return True  # unknown client (e.g. in tests) — fail open to not break local tooling
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def _gate_terminal_exec(command: str) -> dict | None:
    """Return a payload for /api/terminal/exec if *command* is not allowed to
    run right now, else None (meaning: proceed).

    Reuses the same run_bash policy and single-use token round-trip as the
    agent tools: destructive commands are refused outright (no prompt); any
    other command requires an explicit approval, which is issued as a token the
    client must present via /api/approval/approve before re-POSTing. Each
    approval is single-use, so re-executing the same command re-prompts.
    """
    dangerous, reason = _is_destructive_command(command)
    if dangerous:
        return {"ok": False, "status": "blocked", "reason": f"Blocked: {reason}"}
    try:
        _gate_approval("run_bash", {"command": command}, command, "command")
    except ToolApprovalRequired as exc:
        return {"ok": False, "status": "approval_required", "token": exc.token,
                "tool_name": exc.tool_name, "arguments": exc.arguments,
                "preview": exc.preview}
    return None  # not destructive and (policy auto OR token already approved)


def _terminal_exec_allowed(client_host: str) -> bool:
    return not _LOOPBACK_ONLY or _is_loopback_client(client_host)


# --- Access-token auth layer ------------------------------------------------
#
# The loopback guard above only helps while the server is bound to 127.0.0.1.
# This adds a second, independent boundary: a single access token. Loopback
# (and test) clients are trusted without one, so the local browser keeps
# working with no UX change; any non-loopback client must present the token or
# be refused — making an opened bind actually safe, not just "safe by default
# bind". The token is a process secret: read from CODERAI_AUTH_TOKEN, else
# generated once at boot and printed once (never persisted, never logged).
#
# Deliberate scope: this guards HTTP + WS at the server boundary. It does NOT
# make the loopback-only terminal endpoints remote-safe — those keep their own
# tighter guard (see /api/terminal/* below).
_auth_token = os.getenv("CODERAI_AUTH_TOKEN", "")
_auth_lock = threading.Lock()


def _is_trusted_client(host: str) -> bool:
    """A client needs no token if it is local (loopback) or a test client."""
    if not host:
        return True  # unknown (e.g. tests) — fail open so local tooling keeps working
    return _is_loopback_client(host) or host in ("testclient", "testserver")


def get_auth_token() -> str:
    """Return the active access token, generating + printing one on first use
    if CODERAI_AUTH_TOKEN was not set. Idempotent; thread-safe (FastAPI
    dispatches in a thread pool)."""
    global _auth_token
    if _auth_token:
        return _auth_token
    with _auth_lock:
        if not _auth_token:
            _auth_token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
            print("\n*** CoderAI access token (shown once — save it. Remote "
                  "clients must send 'Authorization: Bearer <token>' or a "
                  "?token=<token> query param): ***\n    " + _auth_token +
                  "\n    Local (loopback) clients do not need it.\n***\n",
                  flush=True)
    return _auth_token


def _presented_token(headers, query) -> str:
    """Extract the presented token from an Authorization header or query."""
    auth = (headers.get("Authorization") or headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth:
        return auth
    if query is not None:
        token = query.get("token") if isinstance(query, dict) else None
        if token:
            if isinstance(token, list):
                token = token[0]
            return str(token)
    return ""


def check_auth(host: str, headers, query) -> bool:
    """True if *host*/*headers*/*query* are allowed through.

    Loopback and test clients are trusted. Everyone else must present the
    access token (constant-time compare).
    """
    if os.getenv("CODERAI_DISABLE_AUTH", "false").lower() in ("true", "1", "yes"):
        return True
    if _is_trusted_client(host):
        return True
    from hmac import compare_digest
    return compare_digest(_presented_token(headers, query), get_auth_token())


def auth_status_payload() -> dict:
    """Describe the auth posture for /api/auth/status (never leaks the token)."""
    return {
        "auth_enabled": True,
        "loopback_only": _LOOPBACK_ONLY,
        "token_configured": bool(os.getenv("CODERAI_AUTH_TOKEN", "")),
    }


def _is_approval_required(tool_output) -> dict | None:
    if not isinstance(tool_output, str):
        return None
    try:
        parsed = json.loads(tool_output)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict) and parsed.get("status") == "approval_required":
        return parsed
    return None


def _execute_tool_with_approval(name: str, args: dict, write_event=None) -> str:
    set_tool_event_sink(write_event)
    try:
        output = execute_tool(name, args)
    finally:
        set_tool_event_sink(None)
    approval_info = _is_approval_required(output)
    if not approval_info:
        return output

    token = approval_info.get("token", "")

    if write_event:
        event_type = "git_diff_preview" if name in {
            "write_file", "replace_in_file", "append_file", "delete_file"
        } else "approval_required"
        write_event({
            "type": event_type,
            "name": approval_info.get("tool_name", name),
            "args": approval_info.get("arguments", args),
            "preview": approval_info.get("preview", ""),
            "token": token,
        })

    def _poll_decision() -> dict | None:
        # Prefer the token-scoped decision; fall back to the flat snapshot for
        # (legacy) payloads that carried no token.
        if token:
            return get_approval_decision(token)
        state = get_approval_state()
        if not state.get("pending") and state.get("approved"):
            return {"approved": True, "rejected": False,
                    "rejection_reason": "", "always_allow": state.get("always_allow", False)}
        if state.get("pending") or state.get("approved"):
            return None
        return {"approved": False, "rejected": state.get("rejected", False),
                "rejection_reason": state.get("rejection_reason", ""),
                "always_allow": False}

    waited = 0.0
    while waited < APPROVAL_TIMEOUT:
        time.sleep(APPROVAL_POLL_INTERVAL)
        waited += APPROVAL_POLL_INTERVAL
        decision = _poll_decision()
        if decision and decision["rejected"]:
            return f"Execution rejected: {decision['rejection_reason'] or 'User rejected execution.'}"
        if decision and decision["approved"]:
            set_tool_event_sink(write_event)
            try:
                return execute_tool(name, args)
            finally:
                set_tool_event_sink(None)
        if decision is None:
            # Cleared/reset elsewhere (e.g. chat cleared) without explicit reject.
            return f"Tool execution cancelled: {name}"

    return f"Approval timed out after {APPROVAL_TIMEOUT}s for tool: {name}"

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent.parent)).resolve()
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
MAX_KEPT_HISTORY_MESSAGES = 6
MAX_ITERATIONS = 20
REQUEST_TIMEOUT = int(os.getenv("AGENT_REQUEST_TIMEOUT", "1800"))
DEFAULT_CONTEXT_TOKEN_BUDGET = int(os.getenv("AGENT_CONTEXT_TOKENS", "24000"))
DEFAULT_RESPONSE_TOKEN_BUDGET = int(os.getenv("AGENT_RESPONSE_TOKENS", "8192"))
MAX_AUTO_CONTINUES = int(os.getenv("AGENT_AUTO_CONTINUES", "8"))

# litellm pulls in the openai/azure/anthropic SDKs and takes ~100s to import —
# a cost that used to run at module import time, slowing every app boot and
# every pytest collection. It is now warmed up in a background daemon thread
# after the server starts; until it finishes, callers fall back to the fast
# char-based estimator (they guard on the counter being truthy).
_litellm_state = (None, {}, "")
_LITELLM_LOCK = threading.Lock()


def _get_litellm():
    """Return the current ``(token_counter, model_cost, error)`` triple.

    Never imports — the import happens once in :func:`warmup_litellm`, off the
    request path, so a not-yet-warmed server returns ``(None, {}, "")`` and
    callers use the estimator.
    """
    return _litellm_state


def warmup_litellm() -> None:
    """Import litellm (once) and publish the result. Run from a daemon thread."""
    global _litellm_state
    if _litellm_state[0] is not None:
        return
    with _LITELLM_LOCK:
        if _litellm_state[0] is not None:
            return
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        os.environ.setdefault("LITELLM_LOG", "ERROR")
        try:
            from litellm import model_cost, token_counter
            _litellm_state = (token_counter, model_cost, "")
        except Exception as exc:
            _litellm_state = (None, {}, repr(exc))

STATE_LOCK = threading.RLock()

STATE = {
    "messages": [],
    "tools_log": [],
    "used_skills_log": [],
    "selected_skills": [],
    "conn_mode": MODE_LOCAL,
    "model": os.getenv("OLLAMA_MODEL", "gemma4:12b"),
    "temperature": 0.4,
    "enable_thinking": False,
    "custom_api_url": "https://api.openai.com/v1",
    "custom_api_key": "",
    "custom_api_model": os.getenv("CUSTOM_API_MODEL", "gpt-4o-mini"),
    "selected_prompt": DEFAULT_PROMPT_NAME,
    "system_prompt": DEFAULT_ACTIVE_PROMPT,
    "model_user_selected": False,
    "memory_enabled": True,
    "memory_summary": "",
    "memory_summarized_count": 0,
    "memory_session_id": uuid.uuid4().hex,
    "memory_retrieval_count": 0,
    "memory_retrieved_facts": [],
    "memory_context": "",
    "context_token_budget": DEFAULT_CONTEXT_TOKEN_BUDGET,
    "response_token_budget": DEFAULT_RESPONSE_TOKEN_BUDGET,
    "auto_continue": True,
    "tavily_enabled": os.getenv("TAVILY_ENABLED", "false").lower() == "true",
    "tavily_api_key": os.getenv("TAVILY_API_KEY", ""),
    "git_approval_mode": True,
    "sandbox_mode": os.getenv("SANDBOX_MODE", "auto"),
    "sandbox_docker_image": os.getenv("SANDBOX_DOCKER_IMAGE", "python:3.11-slim"),
    "generated_artifact": None,
    "git_checkpoint_workspace": "",
    "git_checkpoint_branch": "",
    "smart_skill_confirmation": False,
    "skill_routing": {},
    "code_rag_hits": [],
    "code_rag_type": "",
}


def get_state_snapshot() -> dict:
    """Thread-safe snapshot of global state."""
    with STATE_LOCK:
        return dict(STATE)


def update_state(updates: dict) -> None:
    """Thread-safe batch update of global state."""
    with STATE_LOCK:
        STATE.update(updates)


SETTINGS_PATH = ROOT / "coderai_data" / "settings.json"


def _load_persisted_settings() -> None:
    if not SETTINGS_PATH.exists():
        return
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        for key in (
            "conn_mode", "temperature", "enable_thinking",
            "custom_api_url", "custom_api_key", "custom_api_model", "fallback_model", "memory_enabled",
            "context_token_budget", "response_token_budget", "auto_continue",
            "tavily_enabled", "tavily_api_key", "git_approval_mode", "smart_skill_confirmation",
            "sandbox_mode", "sandbox_docker_image",
            "model",
        ):
            if key in data and data[key] is not None:
                STATE[key] = data[key]
    except Exception:
        pass


def _save_persisted_settings() -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        persisted = {
            "conn_mode": STATE.get("conn_mode"),
            "model": STATE.get("model"),
            "temperature": STATE.get("temperature"),
            "enable_thinking": STATE.get("enable_thinking"),
            "custom_api_url": STATE.get("custom_api_url"),
            "custom_api_key": STATE.get("custom_api_key"),
            "custom_api_model": STATE.get("custom_api_model"),
            "memory_enabled": STATE.get("memory_enabled"),
            "context_token_budget": STATE.get("context_token_budget"),
            "response_token_budget": STATE.get("response_token_budget"),
            "auto_continue": STATE.get("auto_continue"),
            "tavily_enabled": STATE.get("tavily_enabled"),
            "tavily_api_key": STATE.get("tavily_api_key"),
            "git_approval_mode": STATE.get("git_approval_mode"),
            "smart_skill_confirmation": STATE.get("smart_skill_confirmation"),
            "sandbox_mode": STATE.get("sandbox_mode", "auto"),
            "sandbox_docker_image": STATE.get("sandbox_docker_image", "python:3.11-slim"),
        }
        SETTINGS_PATH.write_text(json.dumps(persisted, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


_load_persisted_settings()

SESSION_STORE = SessionStore(STATE)


def get_session_state(session_id: str | None = None) -> dict:
    if not session_id or session_id == "default":
        return STATE
    _, sess = SESSION_STORE.get_or_create(session_id)
    return sess


sm = get_skills_manager()
pm = get_prompt_manager()
lc_runtime = LangChainRuntime()
skill_router = SkillRouter()


def _normalize_conn_mode(mode: str | None) -> str:
    m = str(mode or "").strip().lower()
    if "custom" in m:
        return MODE_CUSTOM
    return MODE_LOCAL


def _active_model() -> str:
    if "custom" in str(STATE.get("conn_mode") or "").lower():
        return str(STATE.get("custom_api_model") or "gpt-4o-mini").strip()
    return str(STATE.get("model") or "gemma4:12b").strip()


def _active_model_display() -> str:
    if "custom" in str(STATE.get("conn_mode") or "").lower():
        model = str(STATE.get("custom_api_model") or "gpt-4o-mini").strip()
        return f"Custom API ({model})"
    model = str(STATE.get("model") or "gemma4:12b").strip()
    return f"Ollama ({model})"


def _skill_tracker(workspace: str | Path | None = None) -> SkillTracker:
    return SkillTracker(workspace or get_workspace())


def _known_project_workspace(workspace: str | Path | None) -> Path:
    if not workspace:
        return get_workspace().resolve()
    requested = Path(workspace).resolve()
    known = {Path(project["workspace_path"]).resolve() for project in _memory_manager().list_projects()}
    if requested not in known:
        raise PermissionError("Unknown project workspace")
    return requested


def _skill_usage_payload(workspace: str | Path | None = None) -> dict:
    tracker = _skill_tracker(workspace)
    report = tracker.report([skill.name for skill in sm.all()])
    definitions = {skill.name: skill for skill in sm.all()}
    for item in report["skills"]:
        skill = definitions.get(item["name"])
        item.update({
            "description": skill.description if skill else "",
            "category": skill.category if skill else "misc",
            "path": str(skill.path) if skill else "",
        })
    report["routing"] = STATE.get("skill_routing", {}) if workspace is None or Path(workspace).resolve() == get_workspace().resolve() else {}
    return report


def _confirm_skill_candidates(prompt: str, candidates: list) -> list[str]:
    if not candidates:
        return []
    candidate_text = "\n".join(f"- {item.skill.name}: {item.skill.description}" for item in candidates)
    history = [{
        "role": "user",
        "content": (
            "Select only the skills genuinely required for this request. Return only a JSON array of skill names.\n\n"
            f"User request:\n{prompt}\n\nCandidate skills:\n{candidate_text}"
        ),
    }]
    try:
        result = _call_model(history)
        content = str(result.get("content") or "")
        match = re.search(r"\[[\s\S]*?\]", content)
        names = json.loads(match.group(0)) if match else []
        allowed = {item.skill.name for item in candidates}
        return [str(name) for name in names if str(name) in allowed]
    except Exception:
        return [item.skill.name for item in candidates]


def _prepare_skill_turn(prompt: str, write_event=None) -> tuple[str, list, str, int]:
    tracker = _skill_tracker()
    triggered = sm.detect_skill_commands(prompt)
    route = skill_router.route(
        prompt, sm.all(), tracker.skill_modes(), [skill.name for skill in triggered], max_active=3,
        smart_confirmation=bool(STATE.get("smart_skill_confirmation")), confirmer=_confirm_skill_candidates,
    )
    selections = route.selected
    STATE["selected_skills"] = [item.skill.name for item in selections if item.triggered_by == "pinned"]
    STATE["skill_routing"] = {
        "selected": [item.skill.name for item in selections],
        "skipped": [item.skill.name for item in route.skipped],
        "confirmation_used": route.confirmation_used,
        "semantic_available": route.semantic_available,
        "limit": 3,
    }
    if triggered:
        clean_prompt = sm.strip_commands(prompt) or "Use these skills: " + ", ".join(f"/{skill.name}" for skill in triggered)
    else:
        clean_prompt = prompt
    turn_index = 1 + sum(1 for message in STATE["messages"] if message.get("role") == "user")
    auto_injection = []
    pinned = set(STATE.get("selected_skills", []))
    for selection in selections:
        tracker.log_usage(
            STATE["memory_session_id"], selection.skill.name, selection.triggered_by,
            selection.matched_keywords, "selected", turn_index,
        )
        tracker.log_usage(
            STATE["memory_session_id"], selection.skill.name, selection.triggered_by,
            selection.matched_keywords, "loaded", turn_index,
        )
        if selection.skill.name not in pinned:
            auto_injection.append(selection.skill.system_injection)
    return clean_prompt, selections, "".join(auto_injection), turn_index


def _emit_skill_selections(selections: list, write_event) -> None:
    for selection in selections:
        write_event({
            "type": "skill_selected", "skill": selection.skill.name,
            "reason": selection.reason, "matched_keywords": selection.matched_keywords,
        })


def _finish_skill_turn(selections: list, used_skills: list, turn_index: int, failed: bool = False, write_event=None) -> None:
    tracker = _skill_tracker()
    selected = {selection.skill.name: selection for selection in selections}
    if failed:
        outcomes = [(selection, "failed") for selection in selections]
    else:
        outcomes = [(selected[skill.name], "applied") for skill in used_skills if skill.name in selected]
    for selection, status in outcomes:
        event = tracker.log_usage(
            STATE["memory_session_id"], selection.skill.name, selection.triggered_by,
            selection.matched_keywords, status, turn_index,
        )
        if write_event:
            write_event({
                "type": f"skill_{status}", "skill": selection.skill.name,
                "reason": selection.reason, "matched_keywords": selection.matched_keywords,
                "event": event,
            })


AGENT_WORKFLOW_PROMPT = """

---
## Agent Workspace Workflow

When the user asks for project work:
1. For whole-project purpose, architecture, structure, or module-collaboration questions, use `get_project_overview`; do not answer from one retrieved file.
2. For a specific implementation question, use `search_codebase`, then `get_related_files` when imports or callers matter.
3. Use `scan_project` or `list_files` for a raw inventory when the index is unavailable.
4. Read the relevant files before changing anything.
5. Make the requested code changes with the available tools.
6. Use `search_files`, `read_many_files`, `replace_in_file`, and `project_tree` when they make codebase work faster and more precise.
7. If Tavily web tools are enabled, use `web_search` for current internet information and `extract_url` when the user gives a URL or asks for web-backed research. If they are not available, explain that Tavily must be enabled in Settings.
8. For large code changes, write/edit files with tools instead of printing entire files in chat.
9. In the final answer, clearly report:
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
    set_git_config(bool(STATE.get("git_approval_mode", True)))
    set_sandbox_config(str(STATE.get("sandbox_mode", "auto")), str(STATE.get("sandbox_docker_image", "python:3.11-slim")))


def _git_manager() -> GitManager:
    return GitManager(get_workspace())


def _git_snapshot() -> dict:
    try:
        manager = _git_manager()
        status = manager.get_status()
        status["history"] = manager.get_log(30) if status.get("is_repo") else []
        status["approval_mode"] = bool(STATE.get("git_approval_mode", True))
        if status.get("is_repo"):
            status["branches"] = manager.list_branches()
            status["in_merge"] = manager.is_in_merge()
            status["conflicts"] = manager.get_conflicts() if status["in_merge"] else []
        else:
            status["branches"] = {"current": "", "local": [], "remote": [], "ahead": 0, "behind": 0}
            status["in_merge"] = False
            status["conflicts"] = []
        return status
    except Exception as exc:
        return {
            "is_repo": False,
            "branch": None,
            "files": [],
            "history": [],
            "branches": {"current": "", "local": [], "remote": [], "ahead": 0, "behind": 0},
            "in_merge": False,
            "conflicts": [],
            "error": str(exc),
            "approval_mode": bool(STATE.get("git_approval_mode", True)),
        }


def _memory_manager() -> MemoryManager:
    return MemoryManager(get_workspace())


def _graph_memory_store(workspace_path: str | Path | None = None) -> GraphMemoryStore:
    ws = Path(workspace_path or STATE.get("workspace_path") or get_workspace()).resolve()
    return GraphMemoryStore(ws)


def _memory_payload() -> dict:
    try:
        manager = _memory_manager()
        return {
            "stats": manager.stats(),
            "facts": manager.list_facts(),
            "preferences": manager.get_user_preferences(),
            "retrieval_count": int(STATE.get("memory_retrieval_count", 0)),
            "retrieved_facts": STATE.get("memory_retrieved_facts", []),
            "session_id": STATE.get("memory_session_id", ""),
            "projects": _project_cards(manager),
            "sessions": manager.list_sessions(),
            "graph": _graph_memory_store().get_stats(),
        }
    except Exception as exc:
        return {"stats": {}, "facts": [], "preferences": {}, "retrieval_count": 0, "retrieved_facts": [], "graph": {}, "error": str(exc)}


def _project_cards(manager: MemoryManager | None = None) -> list[dict]:
    return build_project_cards(
        workspace_path=get_workspace(),
        manager=manager or _memory_manager(),
        is_agent_running=bool(STATE.get("agent_running")),
        snapshot_fn=_workspace_snapshot_for,
    )


def _persistent_memory_context(query: str) -> str:
    if not STATE.get("memory_enabled"):
        STATE["memory_retrieval_count"] = 0
        STATE["memory_retrieved_facts"] = []
        STATE["memory_context"] = ""
        return ""
    try:
        sections = []
        # 1. Knowledge Graph (KG-RAG) Context
        try:
            kg_context = _graph_memory_store().retrieve_context(query, token_budget=1500)
            if kg_context:
                sections.append(kg_context.strip())
        except Exception:
            pass

        # 2. Semantic Facts & Preferences
        manager = _memory_manager()
        preferences = manager.get_user_preferences()
        facts = manager.retrieve_relevant(query, top_k=5)
        summaries = manager.load_recent_summaries(STATE.get("memory_session_id", ""), limit=2)
        STATE["memory_retrieval_count"] = len(facts)
        STATE["memory_retrieved_facts"] = facts
        
        mem_sections = ["[Persistent workspace memory]"]
        if preferences:
            mem_sections.append("User preferences:\n" + "\n".join(f"- {key}: {value}" for key, value in preferences.items()))
        if facts:
            mem_sections.append("Relevant project facts:\n" + "\n".join(f"- {item['fact']} (source: {item['source'] or 'memory'})" for item in facts))
        if summaries:
            summary_text = "\n\n".join(item["summary"] for item in summaries)
            mem_sections.append("Older session summaries:\n" + _clip_for_context(summary_text, 3000))
        if len(mem_sections) > 1:
            sections.append("\n\n".join(mem_sections))

        if not sections:
            STATE["memory_context"] = ""
            return ""
        sections.append("Treat current workspace files and the current user request as more authoritative than memory.")
        context = _clip_for_context("\n\n".join(sections), 7000)
        STATE["memory_context"] = context
        return context
    except Exception:
        STATE["memory_retrieval_count"] = 0
        STATE["memory_retrieved_facts"] = []
        STATE["memory_context"] = ""
        return ""


def _codebase_rag_context(query: str) -> str:
    # Per user request (Suggestion 2), automatic RAG injection is turned off.
    # The agent is expected to use the `search_codebase` tool when it needs code context.
    try:
        # We still run a lightweight metadata-only or fast retrieval if we just want UI highlights, 
        # but for prompt context, we return an empty string to save tokens.
        retrieval = get_codebase_index(get_workspace()).retrieve_context(query, top_k=5)
        hits = retrieval["chunks"]
        STATE["code_rag_type"] = retrieval["query_type"]
        STATE["code_rag_hits"] = [
            {key: hit.get(key) for key in ("file_path", "symbol_name", "symbol_type", "start_line", "end_line", "score")}
            for hit in hits
        ]
        return ""
    except Exception:
        STATE["code_rag_hits"] = []
        STATE["code_rag_type"] = ""
        return ""


def _save_memory_turn(role: str, content: str, tool_calls: list | None = None) -> None:
    if not STATE.get("memory_enabled"):
        return
    try:
        _memory_manager().save_turn(STATE["memory_session_id"], role, content, tool_calls)
    except Exception:
        pass
    try:
        _graph_memory_store().ingest_turn_async(role, content, source_ref=STATE.get("memory_session_id", "default"))
    except Exception:
        pass


def _index_tool_memory(tools_done: list[dict]) -> None:
    if not STATE.get("memory_enabled"):
        return
    labels = {
        "write_file": "Agent created or updated {path}.",
        "replace_in_file": "Agent modified existing code in {path}.",
        "append_file": "Agent appended content to {path}.",
        "delete_file": "Agent deleted {path}.",
    }
    try:
        manager = _memory_manager()
        for item in tools_done:
            name = item.get("name")
            path = str((item.get("args") or {}).get("path") or "").strip()
            result = str(item.get("result") or "")
            if name in labels and path and not result.lower().startswith(("error", "execution rejected", "no replacements")):
                manager.index_fact(labels[name].format(path=path), source=path)
    except Exception:
        pass
    try:
        kg = _graph_memory_store()
        for item in tools_done:
            name = item.get("name")
            path = str((item.get("args") or {}).get("path") or "").strip()
            result = str(item.get("result") or "")
            if path and not result.lower().startswith(("error", "execution rejected", "no replacements")):
                if name in ("write_file", "replace_in_file", "append_file"):
                    kg.add_fact("Agent", "modified_file", path, confidence=1.0, subject_type="agent", object_type="file")
                elif name == "delete_file":
                    kg.add_fact("Agent", "deleted_file", path, confidence=1.0, subject_type="agent", object_type="file")
    except Exception:
        pass


def _auto_index_workspace_background(workspace_path: str | Path) -> None:
    ws = Path(workspace_path).resolve()
    def _worker():
        try:
            CodebaseIndex(ws).sync_incremental()
            res = GraphMemoryStore(ws).index_project_workspace()
            
            try:
                from coderai.codebase.code_graph_service import code_graph_service
                code_graph_service.build_or_update(ws, full_rebuild=False)
            except Exception:
                pass

            try:
                mm = MemoryManager(ws)
                if res.get("project_name"):
                    p_name = res["project_name"]
                    langs = ", ".join(res.get("languages", []))
                    if langs:
                        mm.index_fact(f"Project '{p_name}' primary languages: {langs}", source="auto_index")
                    eps = ", ".join(res.get("entrypoints", []))
                    if eps:
                        mm.index_fact(f"Project '{p_name}' entrypoint files: {eps}", source="auto_index")
            except Exception:
                pass
        except Exception:
            pass

    t = threading.Thread(target=_worker, daemon=True, name="AutoProjectIndex")
    t.start()



def _restore_session_state(session: dict) -> None:
    STATE["memory_session_id"] = session.get("id") or session.get("session_id")
    STATE["messages"] = []
    STATE["tools_log"] = []
    for turn in session["turns"]:
        role = turn["role"]
        content = turn["content"]
        tools_done = turn.get("tool_calls") or []
        
        if role == "user":
            STATE["messages"].append({"role": "user", "content": content})
        elif role == "assistant":
            if tools_done:
                STATE["tools_log"].append(tools_done)
                t_calls = []
                for i, t in enumerate(tools_done):
                    t_calls.append({"name": t["name"], "arguments": t.get("args", {}), "id": f"call_{i}"})
                
                STATE["messages"].append({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": _format_tool_calls_for_history(STATE["conn_mode"], t_calls)
                })
                
                for i, t in enumerate(tools_done):
                    tm = {"role": "tool", "content": str(t.get("result", "")), "name": t["name"]}
                    if STATE["conn_mode"] == MODE_CUSTOM:
                        tm["tool_call_id"] = f"call_{i}"
                    STATE["messages"].append(tm)
            else:
                STATE["messages"].append({"role": "assistant", "content": content})
                
    STATE["memory_summary"] = session.get("summary", "")
    STATE["memory_summarized_count"] = 0
    STATE["memory_retrieval_count"] = 0
    STATE["memory_retrieved_facts"] = []
    STATE["memory_context"] = ""
    STATE["used_skills_log"] = []


def _activate_workspace_memory(path: str | Path) -> tuple[bool, str]:
    old_workspace = get_workspace()
    try:
        MemoryManager(old_workspace).summarize_old_session(STATE.get("memory_session_id", ""))
    except Exception:
        pass
    ok, message = set_workspace(path)
    if not ok:
        return ok, message
    
    mm = MemoryManager(path)
    sessions = mm.list_sessions(limit=1)
    
    if sessions:
        latest = sessions[0]
        session = mm.load_session(latest["id"] if "id" in latest else latest.get("session_id", ""))
        if session:
            _restore_session_state(session)
        else:
            STATE["memory_session_id"] = uuid.uuid4().hex
            STATE["messages"] = []
            STATE["tools_log"] = []
            STATE["memory_summary"] = ""
    else:
        STATE["memory_session_id"] = uuid.uuid4().hex
        STATE["messages"] = []
        STATE["tools_log"] = []
        STATE["memory_summary"] = ""
        
    STATE["memory_summarized_count"] = 0
    STATE["memory_retrieval_count"] = 0
    STATE["memory_retrieved_facts"] = []
    STATE["memory_context"] = ""
    STATE["used_skills_log"] = []
    _auto_index_workspace_background(path)
    return True, message


def _validate_git_remote(remote_url: str) -> str:
    remote_url = str(remote_url or "").strip()
    if not remote_url:
        raise ValueError("Repository URL is required")
    parsed = urlparse(remote_url)
    is_scp_style = remote_url.startswith("git@") and ":" in remote_url
    if parsed.scheme not in {"http", "https", "ssh", "git"} and not is_scp_style:
        raise ValueError("Use an HTTPS or SSH Git repository URL")
    if parsed.username or parsed.password:
        raise ValueError("Do not put credentials in the repository URL; use the authentication fields")
    return remote_url


def _default_clone_destination(remote_url: str) -> Path:
    return get_workspace().parent / _git_repository_name(remote_url)


def _git_repository_name(remote_url: str) -> str:
    repo_name = remote_url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if repo_name.lower().endswith(".git"):
        repo_name = repo_name[:-4]
    repo_name = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_name).strip(".-") or "repository"
    return repo_name


def _resolve_clone_destination(remote_url: str, requested: str = "") -> Path:
    if not requested:
        return _default_clone_destination(remote_url).resolve()
    destination = Path(requested).expanduser().resolve()
    # A selected existing folder is treated as the parent directory, matching
    # common clone dialogs. Empty/non-existent paths remain valid exact targets.
    if destination.is_dir() and any(destination.iterdir()):
        destination = destination / _git_repository_name(remote_url)
    return destination


def _ensure_session_checkpoint(write_event=None) -> str:
    workspace = str(get_workspace())
    if STATE.get("git_checkpoint_workspace") == workspace:
        return STATE.get("git_checkpoint_branch", "")
    manager = GitManager(workspace)
    if not manager.is_repo() or not manager.get_log(1):
        return ""
    branch = manager.create_checkpoint_branch()
    STATE["git_checkpoint_workspace"] = workspace
    STATE["git_checkpoint_branch"] = branch
    if write_event:
        write_event({"type": "git_checkpoint_created", "branch": branch})
    return branch


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


# --- Plan Mode ------------------------------------------------------------
#
# Plan Mode is a modular, cleanly-architected service (see the plan_mode package).
# The web app is only a thin adapter: it forwards /api/plans* requests to the
# pure dispatch() handler, which returns an HTTP status + JSON payload. Plans are
# persisted to coderai_data/plans so they survive a restart. A module-level lock
# guards the (cheap) service construction and serializes dispatches.
_PLAN_SERVICE_LOCK = threading.Lock()
_PLAN_SERVICE = None


def _get_plan_service():
    """Return the process-wide PlanModeService, building it on first use.

    Returns:
        A wired :class:`plan_mode.application.service.PlanModeService`.
    """
    global _PLAN_SERVICE
    if _PLAN_SERVICE is None:
        with _PLAN_SERVICE_LOCK:
            if _PLAN_SERVICE is None:
                _PLAN_SERVICE = build_default_service(directory="coderai_data/plans")
    return _PLAN_SERVICE


def dispatch_plan_request(method: str, path: str, body: dict | None) -> tuple[int, dict]:
    """Route a /api/plans* request to the Plan Mode service.

    This is the single seam between the HTTP handler and the plan_mode package.
    It keeps web_app free of Plan Mode logic so the service stays unit-testable.

    Args:
        method: the HTTP method.
        path: the request path.
        body: the decoded JSON body (may be empty).

    Returns:
        ``(status_code, payload)`` — payload is JSON-serializable.
    """
    return plan_dispatch(method, path, body, _get_plan_service())


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
    counter, _cost, _err = _get_litellm()
    if counter:
        try:
            return int(counter(model=_active_model(), messages=messages))
        except Exception:
            pass
    chars = sum(len(str(message.get("content", ""))) + 24 for message in messages)
    return max(1, chars // 4)


def _estimate_tokens_for_text(text: str) -> int:
    return max(1, len(text or "") // 4)


def _fast_tokens_for_messages(messages: list[dict]) -> int:
    chars = sum(len(str(message.get("content", ""))) + 24 for message in messages)
    return max(1, chars // 4)


def _model_context_window(model: str) -> tuple[int, str]:
    cleaned = (model or "").strip()
    budget = max(4_000, int(STATE.get("context_token_budget") or DEFAULT_CONTEXT_TOKEN_BUDGET))

    counter, model_cost, _err = _get_litellm()
    if counter:
        candidates = [cleaned]
        if STATE.get("conn_mode") == MODE_LOCAL and not cleaned.startswith("ollama/"):
            candidates.append(f"ollama/{cleaned}")
        for candidate in candidates:
            try:
                info = model_cost.get(candidate, {}) or {}
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


def _context_usage_snapshot() -> dict:
    final_system = _build_final_system_prompt()
    if STATE.get("memory_context"):
        final_system += f"\n\n{STATE['memory_context']}"
    messages = _build_api_messages(final_system, compact=False)
    input_tokens = _fast_tokens_for_messages(messages)
    configured_budget = max(4_000, int(STATE.get("context_token_budget") or DEFAULT_CONTEXT_TOKEN_BUDGET))
    context_window, source = _model_context_window(_active_model())
    effective_window = max(1, min(context_window, configured_budget))
    response_budget = max(512, int(STATE.get("response_token_budget") or DEFAULT_RESPONSE_TOKEN_BUDGET))
    remaining = max(0, effective_window - input_tokens)
    pct = min(999, round((input_tokens / effective_window) * 100)) if effective_window else 0
    return {
        "input_tokens": input_tokens,
        "context_window": context_window,
        "configured_budget": configured_budget,
        "effective_window": effective_window,
        "response_budget": response_budget,
        "remaining_tokens": remaining,
        "percent": pct,
        "window_source": source,
        "message_count": len(messages),
        "counter": "fast estimate",
    }


def _message_summary_line(message: dict) -> str:
    role = message.get("role", "user")
    content = " ".join(str(message.get("content", "")).split())
    return f"- {role}: {_clip_for_context(content, 700)}"


def _compact_memory_if_needed() -> None:
    if not STATE.get("memory_enabled"):
        return

    messages = STATE["messages"]
    # Estimate total token/char usage
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    
    # We trigger auto-compaction if messages get too large OR if we exceed MAX_KEPT_HISTORY_MESSAGES by a large margin
    context_window, _ = _model_context_window(_active_model())
    if not context_window or context_window < 4000:
        context_window = int(STATE.get("context_token_budget", DEFAULT_CONTEXT_TOKEN_BUDGET))
        
    # Auto-compact at 80% capacity (assuming roughly 3.5 chars per token)
    char_limit = int(context_window * 0.8 * 3.5)
    
    if len(messages) <= max(MAX_KEPT_HISTORY_MESSAGES + 2, 18) and total_chars < char_limit:
        return

    keep_tail = MAX_KEPT_HISTORY_MESSAGES
    to_summarize = messages[:-keep_tail]
    tail = messages[-keep_tail:]
    # tools_log has one entry per *final* assistant message per turn; the
    # assistant tool_calls messages (persisted for model context) are not
    # turns, so they must not be counted here or the logs drift.
    removed_assistants = sum(
        1 for message in to_summarize
        if message.get("role") == "assistant" and not message.get("tool_calls")
    )
    existing = STATE.get("memory_summary", "").strip()
    
    print(f"Auto-compacting {len(to_summarize)} messages... (Total chars: {total_chars})")
    
    # Prepare LLM request for summarization
    system_prompt = "You are a memory compaction module. Summarize the provided conversation history. Extract and retain ALL important technical details, file paths, code structures, user preferences, and unresolved plans. Be extremely concise but comprehensive."
    user_prompt = "CONVERSATION HISTORY TO SUMMARIZE:\n"
    if existing:
        user_prompt += f"\n--- PREVIOUS SUMMARY ---\n{existing}\n"
    user_prompt += "\n--- NEW MESSAGES ---\n"
    for message in to_summarize:
        role = message.get("role", "unknown")
        content = _clip_for_context(message.get("content", ""), 2000) # Clip individual messages to avoid blowing up the summarization prompt
        user_prompt += f"{role}: {content}\n\n"
    
    try:
        old_tools = STATE.get("active_tools", [])
        STATE["active_tools"] = []
        old_budget = STATE.get("response_token_budget", DEFAULT_RESPONSE_TOKEN_BUDGET)
        STATE["response_token_budget"] = 1500  # limit summary length
        
        summary_history = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        # We need a way to notify the user if it's called inside a streaming context
        # But since we don't have write_event here, we just let it take its time.
        response = _call_model(summary_history)
        
        STATE["active_tools"] = old_tools
        STATE["response_token_budget"] = old_budget

        new_summary = response.get("content", "").strip()
        if not new_summary:
            raise Exception("LLM returned empty summary")
            
        summary = f"## Memory compacted at {time.strftime('%Y-%m-%d %H:%M:%S')}\n{new_summary}"
    except Exception as e:
        print(f"LLM compaction failed: {e}. Falling back to basic clipping.")
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
    if role == "tool":
        out = {"role": "tool", "name": message.get("name"), "content": compact_tool_output(content, max_chars=3500)}
        if message.get("tool_call_id"):
            out["tool_call_id"] = message["tool_call_id"]
        return out
    limit = MAX_ASSISTANT_HISTORY_CHARS if role == "assistant" else MAX_HISTORY_MESSAGE_CHARS
    msg = {"role": role, "content": _clip_for_context(content, limit)}
    if "tool_calls" in message:
        msg["tool_calls"] = message["tool_calls"]
    return msg


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
            is_generated = path == "Generated Code"
            heading = "[Active generated artifact in Editor]" if is_generated else "[Active file selected in UI]"
            lines += [
                "",
                heading,
                f"Path: {path}",
            ]
            if is_generated:
                lines += [
                    "The user's next request is about this generated code artifact unless they explicitly say otherwise.",
                    "Apply the requested change to this artifact and return the complete updated artifact in one fenced code block.",
                    "Do not return only a patch, excerpt, or explanation when the user asks for a code change.",
                    "Keep any prose short and separate from the fenced code block.",
                ]
                body = _clip_for_context(content, 18_000)
                lines += [
                    "",
                    f"```{active_context.get('info') or ''}".rstrip(),
                    body,
                    "```",
                ]
            else:
                lines += [
                    "The user's next request is about this file unless they explicitly say otherwise.",
                    "Use `write_file` or `replace_in_file` to apply requested changes to this path when appropriate.",
                ]
                
                body = _clip_for_context(content, 18_000)
                lines += [
                    "",
                    f"```{active_context.get('info') or ''}".rstrip(),
                    body,
                    "```",
                ]
                
                try:
                    from coderai.codebase.code_graph_service import code_graph_service
                    if code_graph_service.is_available():
                        res = code_graph_service.get_minimal_context(changed_files=[path])
                        if res.get("ok") and res.get("data", {}).get("status") == "ready":
                            graph_summary = res["data"].get("context_text", "")
                            if graph_summary:
                                lines += [
                                    "",
                                    "=== File Dependency Graph Context ===",
                                    "Use the following knowledge graph summary to understand the architecture and dependencies of the file, without needing to `read_file` blindly.",
                                    graph_summary,
                                    "=====================================",
                                ]
                except Exception as e:
                    pass
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


def _build_api_messages(final_system: str, compact: bool = True) -> list[dict]:
    if compact:
        _compact_memory_if_needed()
    target_tokens = max(4_000, int(STATE.get("context_token_budget") or DEFAULT_CONTEXT_TOKEN_BUDGET))
    system_tokens = _estimate_tokens_for_text(final_system)
    budget = max(2_000, (target_tokens - system_tokens) * 4)
    selected_reversed: list[dict] = []
    used = 0

    recent = STATE["messages"][-MAX_KEPT_HISTORY_MESSAGES:]
    # The window may begin mid-exchange (an assistant tool_calls message and
    # its tool results are now persisted as separate messages). Tool messages
    # without their assistant tool_calls message are invalid for strict APIs,
    # so drop leading orphans.
    while recent and recent[0].get("role") == "tool":
        recent = recent[1:]
    compacted_recent = compact_history_assistant_turns(recent, keep_recent_assistant_code=1)
    # Group each assistant tool_calls message with the tool results that
    # follow it, so the budget loop never selects half an exchange (a call
    # without its response, or a response without its call) — strict APIs
    # reject those.
    units: list[list[dict]] = []
    in_exchange = False
    for message in compacted_recent:
        if in_exchange and message.get("role") == "tool":
            units[-1].append(message)
            continue
        in_exchange = message.get("role") == "assistant" and bool(message.get("tool_calls"))
        units.append([message])
    for unit in reversed(units):
        unit_compacted = [_message_for_context(message) for message in unit]
        size = sum(len(message["content"]) + 32 for message in unit_compacted)
        if selected_reversed and used + size > budget:
            break
        # The final list is reversed once, so add the unit's messages in
        # reverse order to keep them in their original relative order.
        selected_reversed.extend(reversed(unit_compacted))
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

    # Proactive Hindsight Memory Recall for current project
    try:
        from coderai.memory.hindsight_manager import get_hindsight_manager
        hm = get_hindsight_manager()
        if hm.is_available():
            last_user_msg = next((m.get("content", "") for m in reversed(STATE["messages"]) if m.get("role") == "user"), "")
            if last_user_msg:
                h_rec = hm.recall(query=last_user_msg[:250], max_tokens=600)
                p_str = h_rec.get("prompt_string", "").strip()
                if p_str:
                    selected.insert(0, {
                        "role": "system",
                        "content": (
                            f"[Hindsight Long-Term Memory (bank: {h_rec.get('bank_id')})]\n"
                            f"{p_str}\n\n"
                            "Use these learned project facts and lessons to inform your actions."
                        ),
                    })
    except Exception:
        pass

    return [{"role": "system", "content": final_system}] + selected


def _workspace_snapshot() -> dict:
    return _workspace_snapshot_for(get_workspace())


def _workspace_snapshot_for(workspace: str | Path) -> dict:
    ws = Path(workspace).resolve()
    ignore = {".git", ".agent_memory", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode", "dist", "build", ".next"}
    files = []
    total_size = 0
    ext_set = set()
    try:
        for p in sorted(iter_workspace_files(ws)):
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


def _read_workspace_file(workspace: str | Path, rel_path: str) -> dict:
    ws = Path(workspace).resolve()
    target = (ws / rel_path).resolve()
    try:
        target.relative_to(ws)
    except ValueError as exc:
        raise PermissionError("Path outside workspace is not allowed") from exc
    if not target.is_file():
        raise FileNotFoundError("Archived project file no longer exists")
    text = target.read_text(encoding="utf-8", errors="replace")
    return {"path": rel_path, "content": text, "size": target.stat().st_size, "ext": target.suffix.lstrip(".") or "text"}


def _read_file(rel_path: str) -> dict:
    return _read_workspace_file(get_workspace(), rel_path)


def _browse_local_folder(initial_dir: str = "") -> str | None:
    """Open a native folder picker on the machine running this local web server."""
    start_dir = initial_dir if initial_dir and Path(initial_dir).exists() else str(Path.home())
    if os.name != "nt":
        root = None
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
            return selected or None
        except Exception:
            return None
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass

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
    url = _fix_docker_localhost_url(url)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "CoderAI/1.0",
        **(headers or {}),
    }
    req = urllib.request.Request(
        url,
        data=body,
        headers=req_headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as err:
            raise ValueError(f"Failed to parse JSON response from {url}: {raw[:120]}") from err


def _post_json_stream(url: str, payload: dict, headers: dict | None = None):
    url = _fix_docker_localhost_url(url)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/x-ndjson, application/json",
        "User-Agent": "CoderAI/1.0",
        **(headers or {}),
    }
    req = urllib.request.Request(
        url,
        data=body,
        headers=req_headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            # Ignore SSE comment and keep-alive lines (e.g. ": ping", ": keepalive")
            if line.startswith(":"):
                continue
            if line.startswith("event:") or line.startswith("id:") or line.startswith("retry:"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line:
                continue
            if line == "[DONE]":
                break
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


def _format_agent_error(exc: Exception) -> str:
    error_detail = str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
            if body:
                try:
                    parsed = json.loads(body)
                    error_detail = parsed.get("error") or parsed.get("message") or body
                except Exception:
                    error_detail = body
        except Exception:
            pass

    if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout) or "timed out" in str(exc).lower():
        return (
            "Agent runtime error. The selected model did not return a response before the timeout.\n\n"
            f"Current timeout: {REQUEST_TIMEOUT} seconds.\n"
            "Try a smaller/faster local model, reduce the prompt/context, or wait and retry if Ollama is still loading the model."
        )
    return (
        "Agent runtime error. Make sure Ollama or the selected API endpoint is reachable.\n\n"
        f"{error_detail}"
    )



def _fix_docker_localhost_url(url: str) -> str:
    import os, re
    if url and os.path.exists('/.dockerenv'):
        return re.sub(r'://(localhost|127\.0\.0\.1)(:|/)', r'://host.docker.internal\2', url)
    return url
def _get_json(url: str, headers: dict | None = None, timeout: int = 15) -> dict:
    url = _fix_docker_localhost_url(url)
    req_headers = {
        "Accept": "application/json",
        "User-Agent": "CoderAI/1.0",
        **(headers or {}),
    }
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as err:
            raise ValueError(f"Failed to parse JSON response from {url}: {raw[:120]}") from err



_MODELS_CACHE = {"local": [], "custom": []}
def _available_models(force: bool = False) -> dict:
    global _MODELS_CACHE
    conn_mode = STATE.get("conn_mode", "local")
    try:
        if conn_mode == MODE_LOCAL:
            if not force and _MODELS_CACHE["local"]:
                names = _MODELS_CACHE["local"]
            else:
                data = _get_json(f'{os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")}/api/tags', timeout=2)
                raw_models = data.get("models", [])
                names = [m.get("model") or m.get("name") for m in raw_models if isinstance(m, dict)]
                names = [name for name in names if name]
                if names: _MODELS_CACHE["local"] = names
            preferred = next((name for name in names if not name.endswith(":cloud")), names[0] if names else STATE.get("model", "gemma4:12b"))
            if names and (STATE.get("model") not in names or (not STATE.get("model_user_selected") and str(STATE.get("model", "")).endswith(":cloud"))):
                STATE["model"] = preferred
            return {"models": names, "selected_model": STATE.get("model"), "error": None}
        else:
            selected = _active_model()
            names = []
            if not force and _MODELS_CACHE["custom"]:
                names = _MODELS_CACHE["custom"]
            else:
                try:
                    headers = {}
                    if STATE.get("custom_api_key"):
                        headers["Authorization"] = f"Bearer {STATE['custom_api_key']}"
                    models_url = f"{STATE.get('custom_api_url', '').rstrip('/')}/models"
                    data = _get_json(models_url, headers=headers, timeout=3)
                    raw_models = data.get("data", []) if isinstance(data, dict) else []
                    names = [m.get("id") or m.get("name") for m in raw_models if isinstance(m, dict)]
                    names = [name for name in names if name]
                    if names: _MODELS_CACHE["custom"] = names
                except Exception:
                    pass
            if not names:
                # Listing is unavailable (offline / endpoint error). Show the
                # configured custom model rather than a hardcoded list of
                # generic names that likely don't exist on this vendor.
                names = [selected] if selected else ["gpt-4o-mini", "gpt-4o", "claude-3-5-sonnet", "deepseek-chat"]
            return {"models": names, "selected_model": selected, "error": None}
    except Exception as e:
        return {"models": [_active_model()], "selected_model": _active_model(), "error": str(e)}

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
        args = repair_json_tool_arguments(fn.get("arguments", {}))
        
        name = str(fn.get("name", "")).strip()
        import re
        name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
        
        calls.append({"name": name, "arguments": args})
    if not calls and message.get("content"):
        available_names = [s.get("function", {}).get("name") for s in TOOL_SCHEMAS if s.get("function", {}).get("name")]
        calls = extract_fallback_tool_calls_from_text(message.get("content", ""), available_names)
    return calls


def _extract_tool_calls_openai(message: dict) -> list[dict]:
    calls = []
    for tc in message.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        args = repair_json_tool_arguments(fn.get("arguments", "{}"))
        
        # Sanitize tool name to prevent 400 Bad Request on Custom APIs (OpenAI format requires ^[a-zA-Z0-9_-]{1,64}$)
        name = str(fn.get("name", "")).strip()
        import re
        name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
        
        calls.append({"id": tc.get("id"), "type": tc.get("type", "function"), "name": name, "arguments": args})
    if not calls and message.get("content"):
        available_names = [s.get("function", {}).get("name") for s in TOOL_SCHEMAS if s.get("function", {}).get("name")]
        for idx, fc in enumerate(extract_fallback_tool_calls_from_text(message.get("content", ""), available_names)):
            calls.append({"id": f"call_fb_{idx}", "type": "function", "name": fc["name"], "arguments": fc["arguments"]})
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


def _use_langchain_runtime() -> bool:
    return os.getenv("AGENT_USE_LANGCHAIN", "true").lower() not in {"0", "false", "no"}


def _use_langchain_streaming_runtime() -> bool:
    if "custom" in str(STATE.get("conn_mode", "")).lower():
        return True
    return os.getenv("AGENT_USE_LANGCHAIN_STREAMING", "false").lower() in {"1", "true", "yes"}


def _runtime_settings() -> RuntimeSettings:
    return RuntimeSettings(
        conn_mode=STATE["conn_mode"],
        model=_active_model(),
        temperature=float(STATE["temperature"]),
        enable_thinking=bool(STATE["enable_thinking"]),
        custom_api_url=_fix_docker_localhost_url(STATE["custom_api_url"]),
        custom_api_key=STATE["custom_api_key"],
        response_token_budget=max(512, int(STATE.get("response_token_budget") or DEFAULT_RESPONSE_TOKEN_BUDGET)),
        request_timeout=REQUEST_TIMEOUT,
    )


def _call_model(history: list[dict]) -> dict:
    response_budget = max(512, int(STATE.get("response_token_budget") or DEFAULT_RESPONSE_TOKEN_BUDGET))
    conn_mode = STATE["conn_mode"]
    is_custom = "custom" in str(conn_mode).lower()
    if _use_langchain_runtime() and lc_runtime.supports(conn_mode):
        return lc_runtime.invoke(history, _active_tool_schemas(), _runtime_settings())

    if not is_custom:
        data = _post_json(
            f'{os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")}/api/chat',
            {
                "model": _active_model(),
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
            "model": _active_model(),
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
    content = msg.get("content", "") or ""
    thinking = msg.get("reasoning_content", "") or msg.get("reasoning", "") or ""
    if not content and thinking:
        content = thinking
    return {
        "content": content,
        "thinking": thinking,
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
    is_custom = "custom" in str(conn_mode).lower()
    if _use_langchain_runtime() and _use_langchain_streaming_runtime() and lc_runtime.supports(conn_mode):
        return lc_runtime.stream(history, _active_tool_schemas(), _runtime_settings(), write_event)

    content_parts: list[str] = []
    thinking_parts: list[str] = []

    if not is_custom:
        tool_calls_raw: list[dict] = []
        finish_reason = ""
        for event in _post_json_stream(
            f'{os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")}/api/chat',
            {
                "model": _active_model(),
                "messages": history,
                "tools": _active_tool_schemas(),
                "think": bool(STATE["enable_thinking"]),
                "stream": True,
                "options": {"temperature": float(STATE["temperature"]), "num_predict": response_budget},
            },
        ):
            from coderai.tools.tools import is_execution_cancelled
            if is_execution_cancelled():
                write_event({"type": "cancelled", "message": "Execution cancelled by user."})
                break
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
            "model": _active_model(),
            "messages": history,
            "tools": _active_tool_schemas(),
            "temperature": float(STATE["temperature"]),
            "stream": True,
            "max_tokens": response_budget,
        },
        headers=headers,
    ):
        from coderai.tools.tools import is_execution_cancelled
        if is_execution_cancelled():
            write_event({"type": "cancelled", "message": "Execution cancelled by user."})
            break
        choices = event.get("choices", []) if isinstance(event, dict) else []
        if not choices:
            if isinstance(event, dict) and "error" in event:
                raise ValueError(f"API Error: {event['error']}")
            continue
        finish_reason = choices[0].get("finish_reason") or finish_reason
        delta = choices[0].get("delta", {}) or {}
        content = delta.get("content") or ""
        thinking = delta.get("reasoning_content") or delta.get("reasoning") or ""
        if thinking:
            thinking_parts.append(thinking)
        if content:
            content_parts.append(content)
            write_event({"type": "token", "content": content})
        _merge_custom_tool_delta(tool_call_deltas, delta.get("tool_calls") or [])

    final_content = "".join(content_parts)
    if not final_content and thinking_parts:
        final_content = "".join(thinking_parts)
        write_event({"type": "token", "content": final_content})

    return {
        "content": final_content,
        "thinking": "".join(thinking_parts),
        "tool_calls": _extract_tool_calls_openai({"tool_calls": [tool_call_deltas[i] for i in sorted(tool_call_deltas)]}),
        "finish_reason": finish_reason,
    }


def _hit_generation_limit(result: dict) -> bool:
    reason = str(result.get("finish_reason", "") or "").lower()
    return reason in {"length", "max_tokens"} or "length" in reason or "limit" in reason


def _looks_incomplete_generation(text: str) -> bool:
    stripped = (text or "").rstrip()
    if not stripped:
        return False
    if stripped.count("```") % 2 == 1:
        return True
    tail = stripped[-240:].lower()
    unfinished_markers = (
        "continue",
        "continued",
        "the rest",
        "remaining code",
        "next part",
        "rest of the code",
        "rest of code",
        "to be continued",
        "omitted for brevity",
        "truncated",
        "partial",
    )
    if any(marker in tail for marker in unfinished_markers):
        return True
    if stripped.endswith("..."):
        return True

    code_blocks = _extract_markdown_code_blocks(stripped)
    if code_blocks:
        return _looks_like_incomplete_code(code_blocks[-1])
    if _contains_code_markers(stripped):
        return _looks_like_incomplete_code(stripped)
    return False


def _extract_markdown_code_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    marker = "```"
    parts = text.split(marker)
    for index in range(1, len(parts), 2):
        block = parts[index]
        if "\n" in block:
            block = block.split("\n", 1)[1]
        if block.strip():
            blocks.append(block)
    return blocks


def _strip_code_noise(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    if line.startswith(("#", "//", "<!--", "*")):
        return ""
    return line


def _contains_code_markers(text: str) -> bool:
    value = text or ""
    markers = (
        "function ",
        "const ",
        "let ",
        "var ",
        "return ",
        "import ",
        "from ",
        "def ",
        "class ",
        "public class ",
        "private ",
        "protected ",
        "#include",
        "</",
        "<div",
        "<section",
        "{",
        "};",
    )
    lower = value.lower()
    return any(marker in lower for marker in markers)


def _looks_like_incomplete_code(code: str) -> bool:
    value = (code or "").rstrip()
    if not value:
        return False
    lines = [_strip_code_noise(line) for line in value.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return False

    last = lines[-1]
    if last.endswith(("...", "{", "(", "[", ",", "\\", "=", "+", "-", "*", "/", ":", ".", "&&", "||")):
        return True
    if last in {"else", "try", "finally", "do"}:
        return True
    if last.lower() in {"else:", "try:", "finally:", "except:", "elif:"}:
        return True

    brackets = _bracket_balance(value)
    if brackets["open"] > brackets["close"]:
        return True

    if _looks_like_html(value) and _has_unclosed_html_tag(value):
        return True

    return False


def _bracket_balance(code: str) -> dict[str, int]:
    pairs = {"(": ")", "[": "]", "{": "}"}
    opening = set(pairs)
    closing = set(pairs.values())
    stack: list[str] = []
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    open_count = 0
    close_count = 0

    for index, char in enumerate(code):
        next_char = code[index + 1] if index + 1 < len(code) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            continue
        if char in opening:
            stack.append(pairs[char])
            open_count += 1
        elif char in closing:
            close_count += 1
            if stack and stack[-1] == char:
                stack.pop()
    return {"open": open_count, "close": close_count, "unclosed": len(stack)}


def _looks_like_html(code: str) -> bool:
    return "<" in code and ">" in code and any(tag in code.lower() for tag in ("<div", "<section", "<html", "<body", "<script", "<style", "<template"))


def _has_unclosed_html_tag(code: str) -> bool:
    import re

    void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    stack: list[str] = []
    for match in re.finditer(r"<\s*(/)?\s*([a-zA-Z][\w:-]*)(?:\s[^<>]*)?>", code):
        closing, tag = match.group(1), match.group(2).lower()
        raw = match.group(0)
        if tag in void_tags or raw.endswith("/>") or raw.startswith("<!--"):
            continue
        if closing:
            if tag in stack:
                while stack:
                    current = stack.pop()
                    if current == tag:
                        break
        else:
            stack.append(tag)
    return bool(stack)


def _continuation_prompt() -> str:
    return (
        "Continue exactly from where you stopped. Do not restart, do not summarize, "
        "and do not repeat previous text. If you were writing a code block, continue the same code block."
    )


def _extract_code_blocks_with_info(text: str, include_open_block: bool = False) -> list[dict]:
    import re

    blocks: list[dict] = []
    last_end = 0
    for match in re.finditer(r"```([^\n`]*)\n([\s\S]*?)```", text or ""):
        info = (match.group(1) or "txt").strip() or "txt"
        code = (match.group(2) or "").rstrip()
        last_end = match.end()
        if code.strip():
            blocks.append({"info": info, "code": code})

    if include_open_block:
        remaining = str(text or "")[last_end:]
        open_match = re.search(r"```([^\n`]*)\n([\s\S]*)$", remaining)
        if open_match:
            info = (open_match.group(1) or "txt").strip() or "txt"
            code = (open_match.group(2) or "").rstrip()
            if code.strip():
                blocks.append({"info": info, "code": code})
    return blocks


def _infer_artifact_info(code: str, fallback: str = "txt") -> str:
    value = (code or "").strip()
    if not value:
        return fallback or "txt"
    lower = value.lower()
    if lower.startswith(("<!doctype", "<html")) or ("<body" in lower and "</" in lower):
        return "html"
    if "<" in value and ">" in value and any(tag in lower for tag in ("<div", "<section", "<script", "<style", "<template")):
        return "html"
    if any(marker in value for marker in ("def ", "import ", "from ", "class ")) and ":" in value:
        return "python"
    if any(marker in value for marker in ("function ", "const ", "let ", "var ", "export ", "=>")):
        return "javascript"
    if "public class " in lower or "private class " in lower:
        return "java"
    if "{" in value and any(marker in lower for marker in ("color:", "display:", "padding:", "margin:", "background:")):
        return "css"
    return fallback or "txt"


def _looks_like_artifact_code(text: str) -> bool:
    value = (text or "").strip()
    if len(value) < 20:
        return False
    if "\n" not in value:
        return False
    return _contains_code_markers(value) or _looks_like_html(value)


def _artifact_from_response(response_text: str, active_context: dict | None = None) -> dict | None:
    blocks = _extract_code_blocks_with_info(response_text, include_open_block=True)
    active_info = str((active_context or {}).get("info") or "txt")
    if blocks:
        block = max(blocks, key=lambda item: len(item.get("code", "")))
        return {
            "title": "Generated Code",
            "info": block.get("info") or active_info or "txt",
            "content": block.get("code", ""),
            "source": "assistant_code_block",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    if (active_context or {}).get("path") == "Generated Code" and _looks_like_artifact_code(response_text):
        content = response_text.strip()
        return {
            "title": "Generated Code",
            "info": _infer_artifact_info(content, active_info),
            "content": content,
            "source": "assistant_plain_code",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return None


def _update_generated_artifact(response_text: str, active_context: dict | None = None) -> dict | None:
    artifact = _artifact_from_response(response_text, active_context)
    if artifact:
        STATE["generated_artifact"] = artifact
    return artifact


def _persist_tool_history(messages: list[dict], response_text: str, turn_tool_calls: list[dict], turn_tools: list[dict]) -> None:
    """Append this iteration's assistant tool-call message and each tool
    result to the persistent transcript, so the next turn keeps the tool
    context instead of re-reading everything (P1-13). Skipped when the
    exchange is incomplete (cancelled mid-iteration) — a partial
    assistant/tool_calls block would break strict APIs."""
    if len(turn_tools) != len(turn_tool_calls):
        return
    messages.append({
        "role": "assistant",
        "content": response_text,
        "tool_calls": _format_tool_calls_for_history(STATE["conn_mode"], turn_tool_calls),
    })
    for index, tc in enumerate(turn_tool_calls):
        messages.append({
            "role": "tool",
            "name": tc["name"],
            "content": turn_tools[index]["result"],
            "tool_call_id": tc.get("id") or f"call_{index}",
        })


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
            turn_start = len(tools_done)
            history.append({
                "role": "assistant",
                "content": result["content"] or "",
                "tool_calls": _format_tool_calls_for_history(STATE["conn_mode"], result["tool_calls"]),
            })
            for index, tc in enumerate(result["tool_calls"]):
                name = tc["name"]
                args = tc["arguments"]
                tool_output = _execute_tool_with_approval(name, args)
                tools_done.append({"name": name, "args": args, "result": tool_output})
                tool_message = {"role": "tool", "content": tool_output, "name": name}
                if STATE["conn_mode"] == MODE_CUSTOM:
                    tool_message["tool_call_id"] = tc.get("id") or f"call_{index}"
                history.append(tool_message)
            _persist_tool_history(
                STATE["messages"], response_text,
                result["tool_calls"], tools_done[turn_start:],
            )
            continue
        response_text += result["content"]
        needs_continue = _hit_generation_limit(result) or _looks_incomplete_generation(response_text)
        if STATE.get("auto_continue") and needs_continue and auto_continues < MAX_AUTO_CONTINUES:
            auto_continues += 1
            history.append({"role": "assistant", "content": result["content"] or ""})
            history.append({"role": "user", "content": _continuation_prompt()})
            continue
        return response_text, thinking_text, tools_done

    response_text += f"\n\nAgent stopped after {MAX_ITERATIONS} tool iterations."
    return response_text, thinking_text, tools_done


def _run_agent(prompt: str, active_context: dict | None = None) -> dict:
    clean_prompt, skill_selections, skill_injection, turn_index = _prepare_skill_turn(prompt)

    _sync_tool_settings()
    memory_context = _persistent_memory_context(clean_prompt)
    code_context = _codebase_rag_context(clean_prompt)
    model_prompt = f"{_build_workspace_context(active_context)}\n\n{code_context}\n\n[User request]\n{clean_prompt}"
    STATE["messages"].append({"role": "user", "content": clean_prompt})
    _save_memory_turn("user", clean_prompt)
    final_system = _build_final_system_prompt() + skill_injection + (f"\n\n{memory_context}" if memory_context else "")
    api_messages = _build_api_messages(final_system)
    api_messages[-1] = {"role": "user", "content": model_prompt}

    failed = False
    try:
        response_text, thinking_text, tools_done = _run_agent_loop(api_messages)
    except Exception as exc:
        failed = True
        response_text = _format_agent_error(exc)
        thinking_text = ""
        tools_done = []

    used_skills = sm.parse_used_skills(response_text)
    _finish_skill_turn(skill_selections, used_skills, turn_index, failed=failed)
    clean_response = sm.strip_skill_tag(response_text)
    _update_generated_artifact(clean_response, active_context)
    STATE["messages"].append({"role": "assistant", "content": clean_response})
    _save_memory_turn("assistant", clean_response, tools_done)
    _index_tool_memory(tools_done)
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
    from coderai.tools.tools import reset_cancel_flag, is_execution_cancelled
    reset_cancel_flag()
    clean_prompt, skill_selections, skill_injection, turn_index = _prepare_skill_turn(prompt)

    _sync_tool_settings()
    try:
        _ensure_session_checkpoint(write_event)
    except Exception as exc:
        write_event({"type": "status", "message": f"Git checkpoint unavailable: {exc}"})
    memory_context = _persistent_memory_context(clean_prompt)
    code_context = _codebase_rag_context(clean_prompt)
    model_prompt = f"{_build_workspace_context(active_context)}\n\n{code_context}\n\n[User request]\n{clean_prompt}"
    STATE["messages"].append({"role": "user", "content": clean_prompt})
    _save_memory_turn("user", clean_prompt)
    write_event({"type": "state", "state": _client_state()})
    _emit_skill_selections(skill_selections, write_event)
    if STATE.get("memory_retrieval_count"):
        write_event({"type": "memory_used", "count": STATE["memory_retrieval_count"]})
    if STATE.get("code_rag_type"):
        write_event({"type": "code_rag_used", "query_type": STATE["code_rag_type"], "count": len(STATE["code_rag_hits"]), "chunks": STATE["code_rag_hits"]})
    write_event({"type": "status", "message": "Preparing context..."})
    
    # Check if memory compaction will be triggered
    if STATE.get("memory_enabled") and len(STATE.get("messages", [])) > max(MAX_KEPT_HISTORY_MESSAGES + 2, 18):
        write_event({"type": "status", "message": "Auto-compacting memory... (This may take a minute to summarize history)"})


    final_system = _build_final_system_prompt() + skill_injection + (f"\n\n{memory_context}" if memory_context else "")
    history = _build_api_messages(final_system)
    history[-1] = {"role": "user", "content": model_prompt}
    response_text = ""
    thinking_text = ""
    tools_done: list[dict] = []
    auto_continues = 0

    failed = False
    try:
        for iteration in range(MAX_ITERATIONS):
            if is_execution_cancelled():
                write_event({"type": "cancelled", "message": "Execution cancelled by user."})
                break
            write_event({
                "type": "status",
                "message": f"Waiting for {_active_model_display()} ({iteration + 1}/{MAX_ITERATIONS}, timeout {REQUEST_TIMEOUT}s)...",
            })
            
            try:
                result = _call_model_stream(history, write_event)
            except Exception as e:
                fallback = str(STATE.get("fallback_model", "")).strip()
                if fallback and fallback != STATE.get("custom_api_model") and fallback != STATE.get("model"):
                    write_event({"type": "status", "message": f"Primary model failed ({e}). Falling back to {fallback}..."})
                    print(f"Model failed: {e}. Falling back to {fallback}")
                    
                    old_conn_mode = STATE["conn_mode"]
                    old_custom = STATE.get("custom_api_model")
                    
                    STATE["conn_mode"] = MODE_CUSTOM
                    STATE["custom_api_model"] = fallback
                    try:
                        result = _call_model_stream(history, write_event)
                    finally:
                        STATE["conn_mode"] = old_conn_mode
                        STATE["custom_api_model"] = old_custom
                else:
                    write_event({"type": "status", "message": f"Agent loop aborted: {e}"})
                    write_event({"type": "token", "content": f"\n\n**Agent Execution Failed:**\n`{e}`\n\nPlease check your model or API settings and try again."})
                    break

            if is_execution_cancelled():
                write_event({"type": "cancelled", "message": "Execution cancelled by user."})
                break

            if result["tool_calls"]:
                if result["content"]:
                    response_text += result["content"]
                turn_start = len(tools_done)
                history.append({
                    "role": "assistant",
                    "content": result["content"] or "",
                    "tool_calls": _format_tool_calls_for_history(STATE["conn_mode"], result["tool_calls"]),
                })

                for index, tc in enumerate(result["tool_calls"]):
                    if is_execution_cancelled():
                        write_event({"type": "cancelled", "message": "Execution cancelled by user."})
                        break
                    name = tc["name"]
                    args = tc["arguments"]
                    write_event({"type": "tool_call", "name": name, "args": args})
                    tool_output = _execute_tool_with_approval(name, args, write_event)
                    tools_done.append({"name": name, "args": args, "result": tool_output})
                    write_event({"type": "tool_result", "name": name, "result": tool_output})
                    tool_message = {"role": "tool", "content": tool_output, "name": name}
                    if STATE["conn_mode"] == MODE_CUSTOM:
                        tool_message["tool_call_id"] = tc.get("id") or f"call_{index}"
                    history.append(tool_message)
                _persist_tool_history(
                    STATE["messages"], response_text,
                    result["tool_calls"], tools_done[turn_start:],
                )
                continue

            response_text += result["content"]
            hit_limit = _hit_generation_limit(result)
            looks_incomplete = _looks_incomplete_generation(response_text)
            if STATE.get("auto_continue") and (hit_limit or looks_incomplete) and auto_continues < MAX_AUTO_CONTINUES:
                auto_continues += 1
                reason = "token limit" if hit_limit else "incomplete output"
                write_event({
                    "type": "status",
                    "message": f"Output hit {reason}; continuing automatically ({auto_continues}/{MAX_AUTO_CONTINUES})...",
                })
                history.append({"role": "assistant", "content": result["content"] or ""})
                history.append({"role": "user", "content": _continuation_prompt()})
                continue
            break
        else:
            response_text += f"\n\nAgent stopped after {MAX_ITERATIONS} tool iterations."
            write_event({"type": "token", "content": f"\n\nAgent stopped after {MAX_ITERATIONS} tool iterations."})
    except Exception as exc:
        failed = True
        response_text = _format_agent_error(exc)
        tools_done = []
        thinking_text = ""
        write_event({"type": "error", "message": response_text})

    used_skills = sm.parse_used_skills(response_text)
    _finish_skill_turn(skill_selections, used_skills, turn_index, failed=failed, write_event=write_event)
    clean_response = sm.strip_skill_tag(response_text)
    _update_generated_artifact(clean_response, active_context)
    STATE["messages"].append({"role": "assistant", "content": clean_response})
    _save_memory_turn("assistant", clean_response, tools_done)
    _index_tool_memory(tools_done)
    STATE["tools_log"].append(tools_done)
    STATE["used_skills_log"].append([s.name for s in used_skills])
    write_event({
        "type": "done",
        "thinking": thinking_text,
        "tools": tools_done,
        "used_skills": [s.name for s in used_skills],
        "state": _client_state(),
    })


def _skills_payload() -> list[dict]:
    return [
        {
            "name": s.name,
            "slash_command": f"/{s.name}",
            "description": s.description,
            "category": s.category,
            "triggers": s.triggers or [],
            "disabled": s.disable_model_invocation,
            "content": s.content,
        }
        for s in sm.all()
    ]


def _get_policies_payload(target_workspace: str | None = None) -> dict:
    from coderai.utils.approval_policy import policy_manager
    ws = target_workspace or str(get_workspace())
    return {
        "workspace_path": ws,
        "global": policy_manager.get_global_policy(),
        "workspace": policy_manager.get_workspace_policy(ws),
        "effective": policy_manager.get_effective_policy(ws),
    }


def _update_policy_payload(data: dict) -> dict:
    from coderai.utils.approval_policy import policy_manager
    scope = data.get("scope", "workspace")
    policy = data.get("policy", {})
    mode = data.get("mode", "custom")
    ws = data.get("workspace_path") or str(get_workspace())
    if scope == "global":
        policy_manager.save_global_policy(policy)
    else:
        policy_manager.save_workspace_policy(ws, mode=mode, policy=policy)
    return _get_policies_payload(ws)


def _reset_policy_payload(data: dict) -> dict:
    from coderai.utils.approval_policy import policy_manager
    ws = data.get("workspace_path") or str(get_workspace())
    policy_manager.reset_workspace_policy(ws)
    return _get_policies_payload(ws)


def _client_state(session_id: str | None = None) -> dict:
    _litellm_counter, _litellm_cost, _litellm_error = _get_litellm()
    st = get_session_state(session_id)
    models_payload = _available_models()
    return {
        "session_id": st.get("memory_session_id", "default"),
        "workspace": _workspace_snapshot(),
        "messages": st["messages"],
        "tools_log": st["tools_log"],
        "generated_artifact": st.get("generated_artifact"),
        "selected_skills": st["selected_skills"],
        "skills": _skills_payload(),
        "skill_usage": _skill_usage_payload(),
        "settings": {
            "conn_mode": st["conn_mode"],
            "model": _active_model(),
            "ollama_model": st["model"],
            "custom_api_model": st["custom_api_model"],
            "fallback_model": st.get("fallback_model", ""),
            "temperature": st["temperature"],
            "enable_thinking": st["enable_thinking"],
            "custom_api_url": st["custom_api_url"],
            "selected_prompt": st["selected_prompt"],
            "system_prompt": st["system_prompt"],
            "memory_enabled": st["memory_enabled"],
            "context_token_budget": st["context_token_budget"],
            "response_token_budget": st["response_token_budget"],
            "auto_continue": st["auto_continue"],
            "tavily_enabled": st["tavily_enabled"],
            "tavily_key_set": bool(st.get("tavily_api_key")),
            "git_approval_mode": st["git_approval_mode"],
            "smart_skill_confirmation": st["smart_skill_confirmation"],
            "sandbox_mode": st.get("sandbox_mode", "auto"),
            "sandbox_docker_image": st.get("sandbox_docker_image", "python:3.11-slim"),
            "policies": _get_policies_payload(),
        },
        "memory": {
            "enabled": st["memory_enabled"],
            "summary_chars": len(st.get("memory_summary", "")),
            "summarized_messages": st.get("memory_summarized_count", 0),
            "token_counter": "litellm" if _litellm_counter else "estimated",
            "token_counter_error": _litellm_error,
            "visible_messages": len(st["messages"]),
            "persistent": _memory_payload(),
        },
        "runtime": {
            "langchain_available": lc_runtime.available,
            "langchain_enabled": _use_langchain_runtime(),
            "langchain_streaming_enabled": _use_langchain_streaming_runtime(),
            "langchain_error": lc_runtime.error,
        },
        "git": _git_snapshot(),
        "code_index": get_codebase_index(get_workspace()).status(),
        "code_rag_hits": st.get("code_rag_hits", []),
        "code_rag_type": st.get("code_rag_type", ""),
        "context_usage": _context_usage_snapshot(),
        "prompts": _prompt_payload(),
        "models": models_payload,
    }


def _clear_session_state(session_id: str | None = None) -> dict:
    st = get_session_state(session_id)
    try:
        _memory_manager().summarize_old_session(st.get("memory_session_id", ""))
    except Exception:
        pass
    st["messages"].clear()
    st["tools_log"].clear()
    st["used_skills_log"].clear()
    st["memory_summary"] = ""
    st["memory_summarized_count"] = 0
    st["memory_session_id"] = uuid.uuid4().hex
    st["memory_retrieval_count"] = 0
    st["memory_retrieved_facts"] = []
    st["memory_context"] = ""
    if st is not STATE:
        STATE["messages"].clear()
        STATE["tools_log"].clear()
        STATE["used_skills_log"].clear()
        STATE["memory_summary"] = ""
        STATE["memory_summarized_count"] = 0
        STATE["memory_session_id"] = st["memory_session_id"]
        STATE["memory_retrieval_count"] = 0
        STATE["memory_retrieved_facts"] = []
        STATE["memory_context"] = ""
    return _client_state(session_id)


class Handler(BaseHTTPRequestHandler):
    def _auth_check(self) -> bool:
        """Enforce the access-token boundary. Loopback/test clients pass; any
        other client needs the token (header or ?token=). Returns True if the
        request may proceed, else sends 401 and returns False."""
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if check_auth(self.client_address[0], self.headers, query):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Type", "application/json")
        body = json.dumps({"error": "unauthorized",
                           "message": "remote access requires the CoderAI token"}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/") and not self._auth_check():
            return
        if path == "/api/auth/status":
            _send_json(self, auth_status_payload())
            return
        if path == "/api/plans" or path.startswith("/api/plans/"):
            status, payload = dispatch_plan_request("GET", path, {})
            _send_json(self, payload, status)
            return
        if path == "/api/approval":
            _send_json(self, get_approval_state())
            return
        if path == "/api/policies":
            query = parse_qs(parsed.query)
            ws = query.get("workspace_path", [None])[0]
            _send_json(self, _get_policies_payload(ws))
            return
        if path == "/api/ollama/embedding-status":
            status = EmbeddingModelManager.get_instance().get_status()
            status["dismissed"] = bool(STATE.get("embedding_dismissed"))
            _send_json(self, status)
            return
        if path == "/api/git":
            _send_json(self, _git_snapshot())
            return
        if path == "/api/memory":
            _send_json(self, _memory_payload())
            return
        if path == "/api/memory/graph/stats":
            _send_json(self, _graph_memory_store().get_stats())
            return
        if path == "/api/memory/graph/search":
            q = parse_qs(parsed.query).get("q", [""])[0]
            _send_json(self, _graph_memory_store().search(q))
            return
        if path == "/api/index":
            _send_json(self, get_codebase_index(get_workspace()).status(check_freshness=True))
            return
        if path == "/api/index/overview":
            index = get_codebase_index(get_workspace())
            _send_json(self, {"overview": index.get_project_overview(), "graph": index.dependency_tree()})
            return
        if path in ("/api/index/graph", "/api/graph/structure"):
            index = get_codebase_index(get_workspace())
            _send_json(self, index.get_schematic_graph())
            return
        if path == "/api/graph/graphify.html":
            from coderai.codebase.code_graph_service import code_graph_service
            html = code_graph_service.generate_graphify_html(get_workspace())
            encoded = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if path == "/api/graph/graphify-data":
            from coderai.codebase.code_graph_service import code_graph_service
            _send_json(self, code_graph_service.get_graphify_payload(get_workspace()))
            return
        if path == "/api/projects":
            _send_json(self, {"projects": _project_cards()})
            return
        if path == "/api/state" or path == "/api/settings":
            _send_json(self, _client_state())
            return
        if path == "/api/models":
            _send_json(self, _available_models())
            return
        if path == "/api/prompts":
            _send_json(self, _prompt_payload())
            return
        if path == "/api/skills":
            _send_json(self, {"skills": _skills_payload()})
            return
        if path == "/api/skills/diagnostics":
            _send_json(self, _skills_diagnostics())
            return
        if path == "/api/skills/usage":
            _send_json(self, _skill_usage_payload())
            return
        if path == "/api/skill/source":
            name = parse_qs(parsed.query).get("name", [""])[0]
            skill = sm.get(name)
            if not skill:
                _send_json(self, {"error": "Skill not found"}, 404)
                return
            _send_json(self, {"name": skill.name, "path": str(skill.path), "content": skill.path.read_text(encoding="utf-8", errors="replace")})
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
        if path == "/api/terminal/info":
            session_id = parse_qs(parsed.query).get("session_id", ["default"])[0]
            session = terminal_manager.get_or_create_session(session_id, cwd=get_workspace())
            _send_json(self, {
                "session_id": session.session_id,
                "shell_type": session.shell_type,
                "cwd": str(session.cwd),
                "is_running": session.is_running(),
                "available_shells": terminal_manager.list_available_shells(),
                "history": session.history[-30:],
            })
            return
        if path == "/api/terminal/stream":
            session_id = parse_qs(parsed.query).get("session_id", ["default"])[0]
            session = terminal_manager.get_or_create_session(session_id, cwd=get_workspace())
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            for event in session.stream_events():
                payload = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
                try:
                    self.wfile.write(payload)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            return
        if path == "/api/browse":
            picked = _browse_local_folder(str(get_workspace()))
            if picked:
                _activate_workspace_memory(picked)
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
        if target.suffix.lower() in {".js", ".css", ".html"}:
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self._auth_check():
            return
        try:
            data = _read_json(self)
            if path == "/api/plans" or path.startswith("/api/plans/"):
                status, payload = dispatch_plan_request("POST", path, data)
                _send_json(self, payload, status)
                return
            if path == "/api/workspace":
                ok, msg = _activate_workspace_memory(data.get("path", ""))
                _send_json(self, {"ok": ok, "message": msg, "workspace": _workspace_snapshot()}, 200 if ok else 400)
                return
            if path == "/api/index/rebuild":
                _send_json(self, CodebaseIndex(get_workspace()).rebuild())
                return
            if path == "/api/index/sync":
                _send_json(self, CodebaseIndex(get_workspace()).sync_incremental())
                return
            if path == "/api/index/regenerate-summary":
                model = _active_model() if data.get("use_model") else None
                _send_json(self, CodebaseIndex(get_workspace()).regenerate_summaries(model=model))
                return
            if path == "/api/ollama/pull-embedding":
                mgr = EmbeddingModelManager.get_instance()
                started = mgr.start_pull("embeddinggemma")
                _send_json(self, {"ok": started, "status": mgr.get_status()})
                return
            if path == "/api/ollama/dismiss-embedding":
                STATE["embedding_dismissed"] = True
                _send_json(self, {"ok": True})
                return
            if path == "/api/browse":
                picked = _browse_local_folder(data.get("initial_dir") or str(get_workspace()))
                if not picked:
                    _send_json(self, {"ok": False, "cancelled": True, "message": "Folder selection was cancelled"})
                    return
                ok, msg = _activate_workspace_memory(picked)
                _send_json(
                    self,
                    {"ok": ok, "message": msg, "workspace": _workspace_snapshot(), "path": picked},
                    200 if ok else 400,
                )
                return
            if path == "/api/settings":
                for key in (
                    "conn_mode", "temperature", "enable_thinking",
                    "custom_api_url", "memory_enabled",
                    "context_token_budget", "response_token_budget", "auto_continue",
                    "tavily_enabled", "tavily_api_key",
                    "git_approval_mode",
                    "smart_skill_confirmation",
                    "sandbox_mode", "sandbox_docker_image",
                ):
                    if key in data:
                        STATE[key] = data[key]
                if "custom_api_key" in data and str(data["custom_api_key"] or "").strip():
                    STATE["custom_api_key"] = str(data["custom_api_key"]).strip()
                if "custom_api_model" in data and str(data["custom_api_model"] or "").strip():
                    STATE["custom_api_model"] = str(data["custom_api_model"]).strip()
                if "fallback_model" in data:
                    STATE["fallback_model"] = str(data["fallback_model"]).strip()

                is_custom = "custom" in str(STATE.get("conn_mode") or "").lower()
                if is_custom:
                    if "model" in data and str(data["model"] or "").strip() and data["model"] != "gemma4:12b":
                        STATE["custom_api_model"] = str(data["model"]).strip()
                    if str(STATE.get("custom_api_model") or "").strip():
                        STATE["model"] = STATE["custom_api_model"]
                else:
                    if "model" in data and str(data["model"] or "").strip():
                        STATE["model"] = str(data["model"]).strip()
                        STATE["model_user_selected"] = True

                STATE["custom_api_model"] = str(STATE.get("custom_api_model") or "gpt-4o-mini").strip()
                if "tavily_api_key" in data:
                    STATE["tavily_api_key"] = str(data.get("tavily_api_key") or "").strip()
                if not STATE.get("tavily_enabled"):
                    STATE["tavily_api_key"] = ""
                _sync_tool_settings()
                for key in ("context_token_budget", "response_token_budget"):
                    if key in STATE:
                        STATE[key] = max(512, int(STATE[key]))
                _save_persisted_settings()
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
                tracker = _skill_tracker()
                valid_names = {name for name in names if sm.get(name)}
                for skill in sm.all():
                    current = tracker.skill_modes().get(skill.name, "auto")
                    if skill.name in valid_names:
                        tracker.set_mode(skill.name, "pinned")
                    elif current == "pinned":
                        tracker.set_mode(skill.name, "auto")
                STATE["selected_skills"] = sorted(valid_names)
                _send_json(self, _client_state())
                return
            if path == "/api/skills/mode":
                skill_name = str(data.get("skill_name") or "")
                if not sm.get(skill_name):
                    _send_json(self, {"error": "Skill not found"}, 404)
                    return
                target_workspace = _known_project_workspace(data.get("workspace_path"))
                _skill_tracker(target_workspace).set_mode(skill_name, str(data.get("mode") or "auto"))
                if data.get("workspace_path"):
                    _send_json(self, {"skill_usage": _skill_usage_payload(target_workspace)})
                else:
                    _send_json(self, _client_state())
                return
            if path == "/api/memory/compact":
                _compact_memory_if_needed()
                try:
                    _memory_manager().summarize_old_session(STATE["memory_session_id"])
                except Exception:
                    pass
                _send_json(self, _client_state())
                return
            if path == "/api/skills/disable":
                skill_name = str(data.get("skill_name") or "")
                if not sm.get(skill_name):
                    _send_json(self, {"error": "Skill not found"}, 404)
                    return
                _skill_tracker().set_disabled(skill_name, bool(data.get("disabled")))
                _send_json(self, _skill_usage_payload())
                return
            if path == "/api/skill/source":
                skill = sm.get(str(data.get("name") or ""))
                if not skill:
                    _send_json(self, {"error": "Skill not found"}, 404)
                    return
                skill.path.write_text(str(data.get("content") or ""), encoding="utf-8")
                sm.reload()
                skill_router.invalidate()
                _send_json(self, {"ok": True, "name": skill.name, "skill_usage": _skill_usage_payload()})
                return
            if path in ("/api/project/delete", "/api/projects/delete"):
                manager = _memory_manager()
                project_id = data.get("project_id")
                workspace_path = data.get("workspace_path")
                deleted = False
                if project_id is not None:
                    deleted = manager.delete_project_by_id(int(project_id))
                elif workspace_path:
                    deleted = manager.delete_project_by_path(str(workspace_path))
                _send_json(self, {
                    "ok": True,
                    "deleted": deleted,
                    "projects": _project_cards(manager),
                })
                return
            if path == "/api/memory/graph/forget":
                entity = str(data.get("entity") or data.get("entity_id") or "").strip()
                deleted = _graph_memory_store().forget_entity(entity)
                _send_json(self, {"ok": True, "deleted": deleted, "stats": _graph_memory_store().get_stats()})
                return
            if path == "/api/memory/graph/fact":
                sub = str(data.get("subject") or "").strip()
                rel = str(data.get("relation") or "").strip()
                obj = str(data.get("object") or "").strip()
                conf = float(data.get("confidence", 1.0))
                fact = _graph_memory_store().add_fact(sub, rel, obj, confidence=conf)
                _send_json(self, {"ok": True, "fact": {"id": fact.id, "text": fact.fact_text}, "stats": _graph_memory_store().get_stats()})
                return
            if path in ("/api/memory/graph/index", "/api/project/index"):
                res = _graph_memory_store().index_project_workspace()
                try:
                    CodebaseIndex(get_workspace()).sync_incremental()
                except Exception:
                    pass
                _send_json(self, {"ok": True, "result": res})
                return
            if path == "/api/memory/graph/extract":
                text = str(data.get("text") or "").strip()
                triples = _graph_memory_store().extract_triples_rule_based(text)
                for item in triples:
                    _graph_memory_store().add_fact(item["subject"], item["relation"], item["object"], confidence=item.get("confidence", 0.9))
                _send_json(self, {"ok": True, "extracted": len(triples), "triples": triples, "stats": _graph_memory_store().get_stats()})
                return
            if path == "/api/memory/archive":
                manager = _memory_manager()
                project_id = data.get("project_id")
                session_id = str(data.get("session_id") or "")
                project = manager.get_project(int(project_id)) if project_id else None
                _send_json(self, {
                    "projects": _project_cards(manager),
                    "sessions": manager.list_sessions(int(project_id)) if project_id else [],
                    "session": manager.load_session(session_id) if session_id else None,
                    "project": project,
                    "files": _workspace_snapshot_for(project["workspace_path"])["files"] if project and Path(project["workspace_path"]).is_dir() else [],
                    "facts": manager.list_facts(project_id=int(project_id)) if project_id else [],
                    "preferences": manager.get_user_preferences(int(project_id)) if project_id else {},
                    "skill_usage": _skill_usage_payload(project["workspace_path"]) if project else {"skills": [], "recent": []},
                })
                return
            if path == "/api/memory/archive/file":
                manager = _memory_manager()
                project = manager.get_project(int(data.get("project_id")))
                if not project:
                    _send_json(self, {"error": "Archived project not found"}, 404)
                    return
                _send_json(self, _read_workspace_file(project["workspace_path"], str(data.get("path") or "")))
                return
            if path == "/api/memory/session/resume":
                session_id = str(data.get("session_id") or "").strip()
                session = _memory_manager().load_session(session_id)
                if not session:
                    _send_json(self, {"error": "Session not found"}, 404)
                    return
                ok, message = set_workspace(session["workspace_path"])
                if not ok:
                    _send_json(self, {"error": message}, 400)
                    return
                _restore_session_state(session)
                _send_json(self, _client_state())
                return
            if path == "/api/memory/fact":
                manager = _memory_manager()
                fact_id = data.get("id")
                if fact_id:
                    manager.update_fact(int(fact_id), data.get("fact", ""), data.get("source", "manual"))
                else:
                    manager.index_fact(data.get("fact", ""), data.get("source", "manual"))
                _send_json(self, _memory_payload())
                return
            if path == "/api/memory/fact/delete":
                _memory_manager().delete_fact(int(data.get("id")))
                _send_json(self, _memory_payload())
                return
            if path == "/api/memory/preference":
                _memory_manager().update_preference(data.get("key", ""), data.get("value", ""))
                _send_json(self, _memory_payload())
                return
            if path == "/api/memory/preference/delete":
                _memory_manager().delete_preference(data.get("key", ""))
                _send_json(self, _memory_payload())
                return
            if path == "/api/memory/forget":
                if data.get("confirm") is not True:
                    _send_json(self, {"error": "Explicit confirmation is required"}, 403)
                    return
                _memory_manager().forget_project()
                STATE["messages"].clear()
                STATE["tools_log"].clear()
                STATE["used_skills_log"].clear()
                STATE["memory_session_id"] = uuid.uuid4().hex
                STATE["memory_summary"] = ""
                STATE["memory_summarized_count"] = 0
                STATE["memory_retrieval_count"] = 0
                STATE["memory_retrieved_facts"] = []
                STATE["memory_context"] = ""
                _send_json(self, _client_state())
                return
            if path == "/api/scan":
                result = tool_scan_project(int(data.get("max_files", 200)))
                STATE["messages"].append({"role": "user", "content": "Scan & Report"})
                STATE["messages"].append({"role": "assistant", "content": result})
                STATE["tools_log"].append([{"name": "scan_project", "args": {"max_files": data.get("max_files", 200)}, "result": result}])
                _send_json(self, {"result": result, "state": _client_state()})
                return
            if path == "/api/cancel":
                from coderai.tools.tools import cancel_current_execution
                proc_killed = cancel_current_execution()
                STATE["agent_running"] = False
                _send_json(self, {"ok": True, "cancelled": True, "process_killed": proc_killed, "state": _client_state()})
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

                STATE["agent_running"] = True
                try:
                    _run_agent_stream(data.get("prompt", ""), write_event, data.get("active_context"))
                finally:
                    STATE["agent_running"] = False
                return
            if path == "/api/clear":
                try:
                    _memory_manager().summarize_old_session(STATE["memory_session_id"])
                except Exception:
                    pass
                STATE["messages"].clear()
                STATE["tools_log"].clear()
                STATE["used_skills_log"].clear()
                STATE["memory_summary"] = ""
                STATE["memory_summarized_count"] = 0
                STATE["memory_session_id"] = uuid.uuid4().hex
                STATE["memory_retrieval_count"] = 0
                STATE["memory_retrieved_facts"] = []
                STATE["memory_context"] = ""
                STATE["git_checkpoint_workspace"] = ""
                STATE["git_checkpoint_branch"] = ""
                clear_approval_state()
                _send_json(self, _client_state())
                return
            if path == "/api/approval/approve":
                approve_pending(bool(data.get("always_allow_for_session")), data.get("token"))
                _send_json(self, get_approval_state())
                return
            if path == "/api/approval/reject":
                reject_pending(data.get("reason", ""), data.get("token"))
                _send_json(self, get_approval_state())
                return
            if path == "/api/policies":
                _send_json(self, _update_policy_payload(data))
                return
            if path == "/api/policies/reset":
                _send_json(self, _reset_policy_payload(data))
                return
            if path == "/api/git/approve":
                approve_pending(bool(data.get("always_allow_for_session")), data.get("token"))
                _send_json(self, get_approval_state())
                return
            if path == "/api/git/reject":
                reject_pending(data.get("reason", ""), data.get("token"))
                _send_json(self, get_approval_state())
                return
            if path == "/api/git/init":
                manager = GitManager(get_workspace())
                manager.init_repo()
                _send_json(self, _git_snapshot())
                return
            if path == "/api/git/clone_stream":
                remote_url = _validate_git_remote(data.get("remote_url", ""))
                destination = _resolve_clone_destination(remote_url, str(data.get("destination") or ""))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    _write_stream_event(self, {"type": "git_clone_status", "message": f"Cloning {remote_url}"})
                    manager = GitManager.clone_repository(
                        remote_url,
                        destination,
                        on_output=lambda line: _write_stream_event(self, {"type": "git_clone_output", "content": line}),
                        username=str(data.get("username") or ""),
                        token=str(data.get("token") or ""),
                    )
                    ok, message = _activate_workspace_memory(destination)
                    if not ok:
                        raise GitError(message)
                    STATE["git_checkpoint_workspace"] = ""
                    STATE["git_checkpoint_branch"] = ""
                    _write_stream_event(self, {"type": "git_clone_done", "path": str(destination), "git": manager.get_status(), "state": _client_state()})
                except Exception as exc:
                    _write_stream_event(self, {"type": "git_clone_error", "message": str(exc)})
                return
            if path == "/api/git/push-preview":
                preview = GitManager(get_workspace()).get_push_preview()
                if not preview.get("remote"):
                    _send_json(self, {"error": "This repository has no origin remote"}, 400)
                    return
                _send_json(self, preview)
                return
            if path == "/api/git/push":
                if data.get("approved") is not True:
                    _send_json(self, {"error": "Explicit user approval is required before push"}, 403)
                    return
                output = GitManager(get_workspace()).push(
                    username=str(data.get("username") or ""),
                    token=str(data.get("token") or ""),
                )
                _send_json(self, {"ok": True, "message": output, "git": _git_snapshot()})
                return
            if path.startswith("/api/git/revert/"):
                commit_hash = path.rsplit("/", 1)[-1]
                new_hash = GitManager(get_workspace()).revert_to(commit_hash)
                _send_json(self, {"ok": True, "commit": new_hash, "git": _git_snapshot()})
                return
            if path == "/api/git/fetch":
                manager = _git_manager()
                out = manager.fetch(
                    remote=str(data.get("remote") or "origin"),
                    username=str(data.get("username") or ""),
                    token=str(data.get("token") or ""),
                )
                _send_json(self, {"ok": True, "message": out, "git": _git_snapshot()})
                return
            if path == "/api/git/pull":
                manager = _git_manager()
                result = manager.pull(
                    remote=str(data.get("remote") or "origin"),
                    branch=str(data.get("branch") or ""),
                    username=str(data.get("username") or ""),
                    token=str(data.get("token") or ""),
                )
                _send_json(self, {"ok": result.get("ok", False), "conflict": result.get("conflict", False), "message": result.get("message", ""), "git": _git_snapshot()})
                return
            if path == "/api/git/branch/switch":
                branch_name = str(data.get("name") or data.get("branch") or "").strip()
                if not branch_name:
                    _send_json(self, {"error": "Branch name is required"}, 400)
                    return
                manager = _git_manager()
                res = manager.switch_branch(branch_name)
                _send_json(self, {"ok": True, "branch": res.get("branch"), "message": res.get("output", ""), "git": _git_snapshot()})
                return
            if path == "/api/git/branch/create":
                branch_name = str(data.get("name") or data.get("branch") or "").strip()
                if not branch_name:
                    _send_json(self, {"error": "Branch name is required"}, 400)
                    return
                start_point = str(data.get("start_point") or "").strip()
                manager = _git_manager()
                res = manager.create_branch(branch_name, start_point=start_point)
                _send_json(self, {"ok": True, "branch": res.get("branch"), "message": res.get("output", ""), "git": _git_snapshot()})
                return
            if path == "/api/git/conflicts/resolve":
                target_path = str(data.get("path") or "").strip()
                resolution = str(data.get("resolution") or "").strip()
                custom_content = str(data.get("custom_content") or "")
                if not target_path or not resolution:
                    _send_json(self, {"error": "path and resolution are required"}, 400)
                    return
                manager = _git_manager()
                res = manager.resolve_conflict(target_path, resolution, custom_content=custom_content)
                _send_json(self, {"ok": True, "result": res, "git": _git_snapshot()})
                return
            if path == "/api/git/merge/abort":
                manager = _git_manager()
                out = manager.abort_merge()
                _send_json(self, {"ok": True, "message": out, "git": _git_snapshot()})
                return
            if path == "/api/git/merge/complete":
                message = str(data.get("message") or "").strip()
                manager = _git_manager()
                commit_hash = manager.complete_merge(message)
                _send_json(self, {"ok": True, "commit": commit_hash, "git": _git_snapshot()})
                return
            if path == "/api/terminal/exec":
                # This endpoint runs an arbitrary command on a live PTY. It has
                # no auth, so: (1) refuse non-loopback clients by default, and
                # (2) route through the same run_bash approval policy as the
                # agent tools. The interactive browser terminal uses the
                # WebSocket path instead and is unaffected.
                if not _terminal_exec_allowed(self.client_address[0]):
                    _send_json(self, {"ok": False, "status": "forbidden",
                                      "error": "terminal exec is loopback-only"}, 403)
                    return
                command = data.get("command", "")
                if command and (gate := _gate_terminal_exec(command)) is not None:
                    status = 403 if gate.get("status") == "blocked" else 202
                    _send_json(self, gate, status)
                    return
                session_id = data.get("session_id", "default")
                shell_type = data.get("shell_type", "powershell")
                cwd = data.get("cwd") or str(get_workspace())
                session = terminal_manager.get_or_create_session(session_id, shell_type=shell_type, cwd=Path(cwd))
                if session.is_running():
                    session.write_stdin(command)
                elif command.strip():
                    session.execute(command.strip(), cwd=Path(cwd) if cwd else None)
                _send_json(self, {"ok": True, "is_running": session.is_running(), "cwd": str(session.cwd)})
                return
            if path == "/api/terminal/kill":
                session_id = data.get("session_id", "default")
                session = terminal_manager.get_or_create_session(session_id, cwd=get_workspace())
                killed = session.kill()
                _send_json(self, {"ok": True, "killed": killed})
                return
            if path == "/api/terminal/clear":
                session_id = data.get("session_id", "default")
                session = terminal_manager.get_or_create_session(session_id, cwd=get_workspace())
                while not session.output_queue.empty():
                    try:
                        session.output_queue.get_nowait()
                    except Exception:
                        break
                _send_json(self, {"ok": True})
                return
            _send_json(self, {"error": "Not found"}, 404)
        except Exception as exc:
            _send_json(self, {"error": str(exc)}, 500)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[web_app] {self.address_string()} - {fmt % args}")


def main() -> None:
    _activate_workspace_memory(get_workspace())
    STATIC_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=warmup_litellm, name="litellm-warmup", daemon=True).start()
    print(f"Web UI running at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
