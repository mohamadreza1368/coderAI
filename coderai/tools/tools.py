"""
tools.py - tool definitions and execution helpers.

The workspace path is mutable. Each tool resolves the current path through
get_workspace() before reading or writing files.
"""

import os
import sys
import subprocess
from coderai.tools import advanced_tools
import json
import uuid
import re
import hashlib
import shlex
import threading
import urllib.request
import urllib.error
import urllib.parse
import ipaddress
import socket
from pathlib import Path
from datetime import datetime
from typing import Callable

from coderai.codebase.git_manager import GitManager
from coderai.codebase.codebase_index import CodebaseIndex, IncrementalIndexer, get_codebase_index
from coderai.codebase.workspace_filter import iter_workspace_files, walk_workspace
from coderai.tools.sandbox_runner import SandboxRunner
from coderai.utils.approval_policy import policy_manager, is_dangerous_bash

MAX_OUTPUT_CHARS = 8_000
# Hard ceiling on how many bytes of a network response body we read into
# memory (P1-14). Bodies are truncated downstream anyway (fetch caps at a few
# KB of text, tavily/endpoint tests snippet a few KB), so an uncapped
# resp.read() could OOM on a large/malicious response.
MAX_RESPONSE_BYTES = int(os.getenv("CODERAI_MAX_RESPONSE_BYTES", str(4 * 1024 * 1024)))
EXEC_TIMEOUT     = 15
TAVILY_ENABLED = os.getenv("TAVILY_ENABLED", "false").lower() == "true"
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
SANDBOX_MODE = os.getenv("SANDBOX_MODE", "auto")
SANDBOX_DOCKER_IMAGE = os.getenv("SANDBOX_DOCKER_IMAGE", "python:3.11-slim")

# Default workspace used before the user selects a project folder.
_DEFAULT_WORKSPACE = Path(os.getenv("AGENT_WORKSPACE", "./agent_workspace")).resolve()
_DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)

# Updated whenever the user changes the active workspace.
WORKSPACE_DIR: Path = _DEFAULT_WORKSPACE
GIT_APPROVAL_MODE = True
# When True, write_file / replace_in_file auto-commit each change to the
# workspace git repo. Off by default: auto-committing silently mutates the
# branch the user is on (and made the test suite pollute it on every run), so
# it is an explicit opt-in, not a side effect of editing a file.
AUTO_COMMIT = os.getenv("CODERAI_AUTO_COMMIT", "false").lower() == "true"
_tool_event_sink: Callable[[dict], None] | None = None


def set_sandbox_config(mode: str = "auto", docker_image: str = "python:3.11-slim") -> None:
    global SANDBOX_MODE, SANDBOX_DOCKER_IMAGE
    SANDBOX_MODE = str(mode or "auto").lower()
    SANDBOX_DOCKER_IMAGE = str(docker_image or "python:3.11-slim")


def set_git_config(approval_mode: bool = True) -> None:
    global GIT_APPROVAL_MODE
    GIT_APPROVAL_MODE = bool(approval_mode)


def set_auto_commit(enabled: bool = False) -> None:
    global AUTO_COMMIT
    AUTO_COMMIT = bool(enabled)


def set_tool_event_sink(sink: Callable[[dict], None] | None) -> None:
    global _tool_event_sink
    _tool_event_sink = sink


def _emit_tool_event(event: dict) -> None:
    if _tool_event_sink:
        _tool_event_sink(event)


_active_processes: set[subprocess.Popen] = set()
_process_lock = threading.Lock()
_cancel_event = threading.Event()


def is_execution_cancelled() -> bool:
    return _cancel_event.is_set()


def reset_cancel_flag() -> None:
    _cancel_event.clear()


def cancel_current_execution() -> bool:
    _cancel_event.set()
    with _process_lock:
        killed_any = False
        for proc in list(_active_processes):
            try:
                _kill_proc_tree(proc)
                killed_any = True
            except Exception:
                pass
        _active_processes.clear()
        return killed_any


def _register_process(proc: subprocess.Popen) -> None:
    with _process_lock:
        _active_processes.add(proc)


def _unregister_process(proc: subprocess.Popen) -> None:
    with _process_lock:
        _active_processes.discard(proc)


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _update_code_index(path: str, deleted: bool = False) -> None:
    # Write/index path: uses a fresh instance (not the shared read cache) so a
    # transient embedding outage sets the sticky _embedding_disabled flag on a
    # throwaway instance, never the one read paths use.
    try:
        indexer = IncrementalIndexer(CodebaseIndex(get_workspace()))
        result = indexer.on_file_changed(path)
        _emit_tool_event({"type": "code_index_updated", "path": path, "deleted": deleted, "chunks": result.get("chunks", 0), "result": result})
    except Exception as exc:
        _emit_tool_event({"type": "code_index_error", "path": path, "message": str(exc)})


# ══════════════════════════════════════════════════════════════════════════════
# ── Execution Approval State
# ══════════════════════════════════════════════════════════════════════════════

class ToolApprovalRequired(Exception):
    """Raised when a tool call requires explicit user approval before execution.

    Carries the per-request approval ``token`` (a hash of the tool name and its
    arguments) so the UI/backend can correlate an approve/reject decision with the
    exact call that requested it, rather than keying on the tool name alone.
    """

    def __init__(self, tool_name: str, arguments: dict, preview: str, token: str = "") -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.preview = preview
        self.token = token
        super().__init__(f"Approval required for {tool_name}")


def _compute_approval_token(tool_name: str, arguments: dict) -> str:
    """Return a deterministic approval token for a tool call.

    The token is the SHA-256 of the tool name and its arguments (key-ordered, so it
    is independent of argument insertion order). Two calls of the same tool with
    identical arguments share a token; a different argument or a different tool yields
    a different token.
    """
    payload = json.dumps({"tool": tool_name, "args": arguments}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _ApprovalStore:
    """Token-keyed approval state, safe for concurrent agent loops.

    Replaces the old single-slot, name-keyed global. Each pending request is stored
    under its own token, a granted decision is single-use (consumed on re-execution,
    so a token cannot be replayed), and the per-session "always allow" set is keyed by
    *tool name* so approving one tool never auto-approves another.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}
        self._latest_pending_token: str | None = None
        self._session_allow: set[str] = set()

    @staticmethod
    def _new_record(token: str, tool_name: str, arguments: dict, preview: str) -> dict:
        return {
            "token": token,
            "tool_name": tool_name,
            "arguments": arguments,
            "preview": preview,
            "pending": True,
            "approved": False,
            "rejected": False,
            "rejection_reason": "",
            "always_allow": False,
        }

    def request(self, tool_name: str, arguments: dict, preview: str) -> str:
        """Register a new pending request; return its token."""
        token = _compute_approval_token(tool_name, arguments)
        with self._lock:
            self._records[token] = self._new_record(token, tool_name, arguments, preview)
            self._latest_pending_token = token
        return token

    def get_record(self, token: str) -> dict | None:
        with self._lock:
            record = self._records.get(token)
            return dict(record) if record is not None else None

    def resolve(
        self,
        token: str | None,
        approved: bool,
        always_allow: bool = False,
        reason: str = "",
    ) -> bool:
        """Record an approve/reject decision for a token.

        Returns True if a matching pending record was decided. A ``None``/empty token
        falls back to the most recently requested pending request (back-compat for
        clients that predate tokens). An ``always_allow`` approval opens the session for
        that token's *tool name only*.
        """
        with self._lock:
            tok = token or self._latest_pending_token
            record = self._records.get(tok) if tok else None
            if record is None or not record["pending"]:
                return False
            record["pending"] = False
            record["approved"] = bool(approved)
            record["rejected"] = not approved
            record["rejection_reason"] = reason or ""
            record["always_allow"] = bool(always_allow) and approved
            if approved and always_allow:
                self._session_allow.add(record["tool_name"])
            return True

    def consume(self, token: str) -> None:
        """Drop a decided record after it has been honored (single-use)."""
        with self._lock:
            self._records.pop(token, None)

    def decision(self, token: str) -> dict | None:
        """Return the decision for a token, or None while it is still pending/unknown."""
        with self._lock:
            record = self._records.get(token)
        if record is None or record["pending"]:
            return None
        return {
            "approved": record["approved"],
            "rejected": record["rejected"],
            "rejection_reason": record["rejection_reason"],
            "always_allow": record["always_allow"],
        }

    def allow_tool_for_session(self, tool_name: str) -> None:
        with self._lock:
            self._session_allow.add(tool_name)

    def is_tool_allowed(self, tool_name: str) -> bool:
        with self._lock:
            return tool_name in self._session_allow

    def latest_pending(self) -> dict | None:
        """Return the most recently requested *pending* record, if any."""
        with self._lock:
            token = self._latest_pending_token
            record = self._records.get(token) if token else None
            if record is not None and record["pending"]:
                return dict(record)
            for tok, rec in reversed(list(self._records.items())):
                if rec["pending"]:
                    return dict(rec)
        return None

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._latest_pending_token = None
            self._session_allow.clear()


_approval_store = _ApprovalStore()

_EMPTY_APPROVAL_STATE: dict = {
    "pending": False,
    "token": "",
    "tool_name": "",
    "arguments": {},
    "preview": "",
    "approved": False,
    "rejected": False,
    "rejection_reason": "",
    "always_allow": False,
}


def get_approval_state() -> dict:
    """Return a flat, UI-compatible snapshot of the latest pending approval request.

    This is a superset of the legacy shape (it also carries ``token``) so the
    ``/api/approval`` endpoint, the poll loop, and the UI keep working unchanged.
    When no request is pending it returns an empty record with ``pending`` False.
    """
    record = _approval_store.latest_pending()
    if record is None:
        return dict(_EMPTY_APPROVAL_STATE)
    return dict(record)


def resolve_approval(
    token: str,
    approved: bool,
    always_allow: bool = False,
    reason: str = "",
) -> bool:
    """Record an approve/reject decision for a specific token.

    Args:
        token: The approval token returned in the ``approval_required`` payload.
        approved: True to approve, False to reject.
        always_allow: If approving, allow this tool for the rest of the session.
        reason: Optional rejection reason.

    Returns:
        True if a matching pending request was decided, False otherwise.
    """
    return _approval_store.resolve(token, approved, always_allow, reason)


def get_approval_decision(token: str) -> dict | None:
    """Return the decision for a token, or None while it is still pending.

    The decision dict has keys ``approved``, ``rejected``, ``rejection_reason`` and
    ``always_allow``. A ``None`` return means the poll loop should keep waiting.
    """
    return _approval_store.decision(token)


def allow_tool_for_session(tool_name: str) -> None:
    """Allow a tool to run without prompting for the rest of the session."""
    _approval_store.allow_tool_for_session(tool_name)


def approve_pending(always_allow_for_session: bool = False, token: str | None = None) -> None:
    """Mark a pending approval request as approved.

    Defaults to the most recently requested request when no token is supplied, so
    legacy callers (and tests) keep working.
    """
    _approval_store.resolve(token, True, always_allow_for_session, "")


def reject_pending(reason: str = "", token: str | None = None) -> None:
    """Mark a pending approval request as rejected (back-compat wrapper)."""
    _approval_store.resolve(token, False, False, reason or "")


def clear_approval_state() -> None:
    """Reset all approval state and the per-session allow set (call on chat reset)."""
    _approval_store.clear()


def _request_approval(tool_name: str, arguments: dict, preview: str) -> None:
    """Register a pending approval request under its token and raise ToolApprovalRequired."""
    token = _approval_store.request(tool_name, arguments, preview)
    raise ToolApprovalRequired(tool_name, arguments, preview, token)


def _gate_approval(tool_name: str, arguments: dict, preview: str, preview_type: str = "generic") -> None:
    """Central approval gate every mutating tool routes through.

    Raises ``ToolApprovalRequired`` when the call needs approval and it has not yet
    been granted. It is a no-op when the policy does not require approval, when the
    tool has been session-allowed, when git approval mode is off, or when a decision
    has already been given for this exact token (which is then consumed so the
    token is single-use). ``preview_type`` is retained for interface symmetry with
    ``should_require_approval``.
    """
    del preview_type
    ws = get_workspace()
    req, _reason, _ptype = policy_manager.should_require_approval(tool_name, arguments, ws)
    if not GIT_APPROVAL_MODE:
        req = False
    if not req:
        return
    if _approval_store.is_tool_allowed(tool_name):
        return
    token = _compute_approval_token(tool_name, arguments)
    decision = get_approval_decision(token)
    if decision is not None and decision["approved"]:
        _approval_store.consume(token)
        return
    _request_approval(tool_name, arguments, preview)


def set_workspace(path: str | Path) -> tuple[bool, str]:
    """
    Change the active workspace.
    Returns: (success, message)
    """
    global WORKSPACE_DIR
    p = Path(path).resolve()
    if not p.exists():
        return False, f"Path does not exist: {p}"
    if not p.is_dir():
        return False, f"Path is not a folder: {p}"
    WORKSPACE_DIR = p
    return True, str(p)


def get_workspace() -> Path:
    return WORKSPACE_DIR


def set_tavily_config(enabled: bool, api_key: str = "") -> None:
    global TAVILY_ENABLED, TAVILY_API_KEY
    TAVILY_ENABLED = bool(enabled)
    TAVILY_API_KEY = api_key.strip()


def tavily_configured() -> bool:
    return bool(TAVILY_ENABLED and TAVILY_API_KEY)


# ══════════════════════════════════════════════════════════════════════════════
# ── Tool Schemas
# ══════════════════════════════════════════════════════════════════════════════

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command (e.g., npm test, python -m unittest, etc.) in the workspace to verify code changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash/shell command to execute"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30, max 120)"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_file_diagnostics",
            "description": "Check a file for syntax errors and warnings (LSP-like diagnostics). Always run this after editing a file to ensure your code is correct.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to check"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_database_schema",
            "description": "Read the schema structure of a SQL database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "connection_string": {"type": "string", "description": "SQLAlchemy connection string"}
                },
                "required": ["connection_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sql_query",
            "description": "Execute a SQL query against a database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "connection_string": {"type": "string", "description": "SQLAlchemy connection string"},
                    "query": {"type": "string", "description": "SQL query"}
                },
                "required": ["connection_string", "query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "navigate_web",
            "description": "Navigate to a URL using a headless browser (Playwright) and return the page content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Take a full-page screenshot of a website using Playwright.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Website URL"},
                    "output_path": {"type": "string", "description": "Path to save screenshot (e.g., screenshot.png)"}
                },
                "required": ["url", "output_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_docker_container",
            "description": "Run a Docker container and return its output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "Docker image"},
                    "command": {"type": "string", "description": "Command to run"}
                },
                "required": ["image"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_container_logs",
            "description": "Fetch logs of a Docker container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "container_name_or_id": {"type": "string", "description": "Container ID"}
                },
                "required": ["container_name_or_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_linter",
            "description": "Run a linter command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command (e.g., flake8 .)", "default": "flake8 ."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run test suite.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command (e.g., pytest)", "default": "pytest"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_kubectl",
            "description": "Run Kubernetes command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "kubectl args"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_terraform",
            "description": "Run Terraform command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "terraform args"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "test_api_endpoint",
            "description": "Test REST API.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL"},
                    "method": {"type": "string", "description": "HTTP Method", "default": "GET"},
                    "headers": {"type": "object", "description": "Headers dict"},
                    "json_body": {"type": "object", "description": "JSON payload dict"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_npm_script",
            "description": "Run npm script.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Script name"},
                    "package_manager": {"type": "string", "description": "npm/yarn", "default": "npm"}
                },
                "required": ["script_name"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Get current Git repository status including branch name and changed files.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Get recent Git commit history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of commits to return", "default": 10}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Get Git differences for uncommitted or staged changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean", "description": "If true, diff staged changes, else diff working tree", "default": False}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage and commit changes to the Git repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message"},
                    "files": {"type": "array", "items": {"type": "string"}, "description": "List of files to commit. Leave empty to commit all changed files."}
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_checkout",
            "description": "Switch to an existing Git branch or create a new one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {"type": "string", "description": "Branch name to checkout or create"},
                    "create": {"type": "boolean", "description": "Create the branch if it does not exist", "default": False}
                },
                "required": ["branch"]
            }
        }
    },    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the active workspace. Supports LeanCTX surgical windowing (start_line, end_line) and compression modes ('raw', 'clean', 'outline') to conserve tokens.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the workspace"},
                    "start_line": {"type": "integer", "description": "Optional starting line number (1-indexed)"},
                    "end_line": {"type": "integer", "description": "Optional ending line number (1-indexed)"},
                    "mode": {"type": "string", "enum": ["raw", "clean", "outline"], "description": "Reading mode: 'raw' (default), 'clean' (strips comments/blank lines), or 'outline' (structural class/function signatures with line numbers)"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite a text file in the active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "File path relative to the workspace"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders in the active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob filter. Default: **/*",
                        "default": "**/*",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": f"Run a shell command in the active workspace. Timeout: {EXEC_TIMEOUT}s",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Run a Python code snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code"}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch text content from a public URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url":       {"type": "string",  "description": "URL to fetch"},
                    "max_chars": {"type": "integer", "description": "Maximum characters to return. Default: 4000", "default": 4000},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web with Tavily and return summarized results, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Number of results. Default: 5", "default": 5},
                    "search_depth": {
                        "type": "string",
                        "description": "Search depth: basic or advanced",
                        "enum": ["basic", "advanced"],
                        "default": "basic",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_url",
            "description": "Extract clean content from one or more URLs with Tavily.",
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "URLs to extract",
                    },
                    "max_chars": {"type": "integer", "description": "Maximum output characters", "default": 8000},
                },
                "required": ["urls"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search workspace files for text or a regex pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text or regex to search for"},
                    "pattern": {"type": "string", "description": "File glob. Default: **/*", "default": "**/*"},
                    "regex": {"type": "boolean", "description": "Treat query as a regex when true", "default": False},
                    "max_matches": {"type": "integer", "description": "Maximum number of matches", "default": 80},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_many_files",
            "description": "Read multiple text files from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relative file paths",
                    },
                    "max_chars_each": {"type": "integer", "description": "Maximum characters per file", "default": 6000},
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Replace exact text or regex matches in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the workspace"},
                    "old": {"type": "string", "description": "Existing text or regex"},
                    "new": {"type": "string", "description": "Replacement text"},
                    "regex": {"type": "boolean", "description": "Treat old as a regex when true", "default": False},
                    "count": {"type": "integer", "description": "Replacement count. 0 means all matches", "default": 0},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Append text to a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Content to append"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "Create a directory inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to the workspace"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_tree",
            "description": "Return a compact project tree with configurable depth.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_depth": {"type": "integer", "description": "Tree depth", "default": 3},
                    "max_entries": {"type": "integer", "description": "Maximum entries", "default": 300},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "current_time",
            "description": "Return the backend system time.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_codebase",
            "description": "Hybrid semantic and exact-keyword search over indexed code chunks. Use for a specific function, class, file, behavior, or implementation question. Use get_project_overview for whole-project purpose or architecture questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Code symbol, filename, behavior, or implementation concept to find"},
                    "top_k": {"type": "integer", "description": "Maximum relevant code chunks", "default": 5}
                },
                "required": ["query"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_overview",
            "description": "Return the indexed project purpose, architecture summary, likely entry points, key file summaries, and dependency graph statistics. Use this for questions about the whole project, its structure, or how major modules collaborate.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_related_files",
            "description": "Return files connected to a specified file through resolved imports or function calls, including relation details and file summaries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative source file path"},
                    "depth": {"type": "integer", "description": "Graph traversal depth, usually 1 or 2", "default": 1}
                },
                "required": ["path"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_project",
            "description": (
                "Build a project scan report with file lists, detected languages, "
                "dependency/config files such as package.json or requirements.txt, "
                "and high-level statistics. Use this when starting work on a new project."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_files": {
                        "type": "integer",
                        "description": "Maximum files to scan. Default: 200",
                        "default": 200,
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "Store an important fact, architectural decision, user preference, or bug solution into long-term project memory (Hindsight).",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The information, observation, or lesson to retain.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context or category (e.g. 'architecture', 'user_preference', 'bug_fix').",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Search long-term project memories, previous decisions, and past experiences using hybrid retrieval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query or concept to recall.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of memories to recall (default 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reflect_memory",
            "description": "Synthesize patterns, risks, or deep conclusions from the project's cumulative memory (Hindsight reflection).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The question or topic to reflect upon (e.g. 'What are the main architectural patterns in this project?').",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_architecture",
            "description": "Get high-level module architecture, community clusters, and key entry points from the code review graph. Ideal for large projects to understand structural organization without token waste.",
            "parameters": {
                "type": "object",
                "properties": {
                    "detail_level": {
                        "type": "string",
                        "enum": ["minimal", "standard", "detailed"],
                        "description": "Detail level of the architecture overview (default: 'minimal').",
                        "default": "minimal",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_impact_radius",
            "description": "Calculate the blast radius of proposed or actual file changes. Traces all callers, dependents, and affected unit tests across folders before editing code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of relative file paths to check for blast radius.",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum call-chain traversal depth (default 2).",
                        "default": 2,
                    },
                },
                "required": ["files"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_code_graph",
            "description": "Query AST structural relationships in the codebase graph across all directories. Pattern options: 'calls' (what does target call), 'callers' (who calls target), 'imports', 'dependencies', or 'extended_by'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "enum": ["calls", "callers", "imports", "dependencies", "extended_by"],
                        "description": "Query pattern: 'calls', 'callers', 'imports', 'dependencies', or 'extended_by'.",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Target function name, class name, or file path to inspect.",
                    },
                },
                "required": ["pattern", "symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_code_review_context",
            "description": "Extract a token-optimized focused subgraph slice for a review task or set of changed files, providing caller context without reading whole files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Description of the task or review goal.",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of changed file paths.",
                    },
                },
                "required": [],
            },
        },
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# ── Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _safe_path(rel_path: str) -> Path:
    ws = get_workspace()
    target = (ws / rel_path).resolve()
    try:
        target.relative_to(ws.resolve())
    except ValueError:
        raise PermissionError(f"Access outside the workspace is not allowed: {rel_path}")
    return target


def _is_probably_text(path: Path, sample_size: int = 2048) -> bool:
    try:
        sample = path.read_bytes()[:sample_size]
        if b"\x00" in sample:
            return False
        sample.decode("utf-8", errors="strict")
        return True
    except Exception:
        return False


def _tavily_post(endpoint: str, payload: dict) -> dict:
    if not TAVILY_ENABLED:
        return {"error": "Tavily web search is disabled in Settings."}
    if not TAVILY_API_KEY:
        return {"error": "Tavily API key is empty. Enable Tavily and add an API key in Settings."}
    errors = []
    auth_attempts = [
        (payload, {"Authorization": f"Bearer {TAVILY_API_KEY}"}),
        ({"api_key": TAVILY_API_KEY, **payload}, {}),
    ]
    for body_payload, extra_headers in auth_attempts:
        body = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.tavily.com/{endpoint}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Ollama-Agentic-Workspace/1.0",
                **extra_headers,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(_read_response_bounded(resp).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
            errors.append(f"HTTP {exc.code}: {detail[:600]}")
        except Exception as exc:
            errors.append(str(exc))
    return {"error": "Tavily request failed. " + " | ".join(errors)}


# ══════════════════════════════════════════════════════════════════════════════
# ── Tool Handlers
# ══════════════════════════════════════════════════════════════════════════════

def tool_read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    mode: str = "raw",
) -> str:
    try:
        p = _safe_path(path)
        if not p.exists():
            return f"File does not exist: {path}"
        content = p.read_text(encoding="utf-8", errors="replace")
        
        _emit_tool_event({"type": "file_opened", "path": path, "content": content})

        # LeanCTX outline/map mode
        mode_str = str(mode or "raw").lower()
        if mode_str in {"outline", "map"}:
            from coderai.core.context_builder import extract_code_outline
            return extract_code_outline(content, file_path=path)

        # LeanCTX clean/aggressive mode (strips comments and redundant whitespace)
        if mode_str in {"clean", "aggressive"}:
            from coderai.core.context_builder import compress_source_code
            content = compress_source_code(content, mode="clean")

        lines = content.splitlines()
        total_lines = len(lines)

        # Surgical line windowing
        if start_line is not None or end_line is not None:
            s = max(1, int(start_line or 1))
            e = min(total_lines, int(end_line or total_lines))
            if s > total_lines:
                return f"Requested start_line {s} exceeds total lines ({total_lines}) in {path}."
            selected = lines[s - 1:e]
            numbered = [f"{i}: {line}" for i, line in enumerate(selected, s)]
            result = f"--- {path} (lines {s}-{e} of {total_lines}) ---\n" + "\n".join(numbered)
            if len(result) > MAX_OUTPUT_CHARS:
                result = result[:MAX_OUTPUT_CHARS] + f"\n\n... [truncated - {len(result)} characters]"
            return result

        if len(content) > MAX_OUTPUT_CHARS:
            content = content[:MAX_OUTPUT_CHARS] + f"\n\n... [truncated - {len(content)} total characters. Use start_line/end_line for surgical reading]"
        return content
    except Exception as e:
        return f"Error: {e}"


def tool_write_file(path: str, content: str) -> str:
    try:
        p = _safe_path(path)
        manager = GitManager(get_workspace())
        arguments = {"path": path, "content": content}
        preview = manager.get_diff_preview(path, content) if manager.is_repo() else f"Create/overwrite file: {path} ({len(content)} bytes)"
        _gate_approval("write_file", arguments, preview, "diff")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _update_code_index(path)
        
        _emit_tool_event({"type": "file_diff", "path": path, "diff": preview})
        
        commit_hash = ""
        if manager.is_repo() and AUTO_COMMIT:
            commit_hash = manager.stage_and_commit([path], f"Update {path} with CoderAI")
            if commit_hash:
                _emit_tool_event({"type": "git_commit_created", "commit": commit_hash, "message": f"Update {path} with CoderAI", "files": [path]})
        suffix = f"; committed as {commit_hash[:8]}" if commit_hash else ""
        return f"File written: {path} ({p.stat().st_size:,} bytes){suffix}"
    except ToolApprovalRequired:
        raise
    except Exception as e:
        return f"Error: {e}"




def tool_check_file_diagnostics(path: str) -> str:
    try:
        p = _safe_path(path)
        if not p.exists():
            return f"File does not exist: {path}"
            
        ext = p.suffix.lower()
        if ext == '.py':
            # 1. Native AST syntax check (super fast, catches syntax/indentation errors)
            import ast
            try:
                ast.parse(p.read_text("utf-8"))
            except SyntaxError as e:
                return f"SyntaxError in {path} at line {e.lineno}, column {e.offset}: {e.msg}\nLine: {e.text}"
            except Exception as e:
                return f"Parse Error: {e}"
                
            # 2. Try running flake8 or pylint if installed
            import subprocess
            try:
                res = subprocess.run(["flake8", str(p)], capture_output=True, text=True, timeout=5)
                if res.returncode != 0 and res.stdout:
                    return f"Linting Warnings/Errors:\n{res.stdout[:2000]}"
            except FileNotFoundError:
                pass # flake8 not installed
                
            return f"No syntax errors found in {path}. Code is syntactically valid."
            
        elif ext in ['.js', '.ts', '.jsx', '.tsx']:
            import subprocess
            try:
                # Try node-based syntax check or eslint
                res = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True, timeout=5)
                if res.returncode != 0:
                    return f"Syntax Error:\n{res.stderr[:2000]}"
                return f"No syntax errors found in {path}."
            except FileNotFoundError:
                return "Node.js not installed, cannot verify JS/TS syntax."
                
        else:
            return f"Diagnostics not supported natively for extension {ext}. Use tool_run_command with an appropriate linter/compiler."
            
    except Exception as e:
        return f"Error running diagnostics: {e}"

def tool_run_command(command: str, timeout: int = 30) -> str:
    """Execute a shell command via the same runner as run_bash.

    run_command is the "verify my changes" tool, but it is the same capability
    class as run_bash — a host shell execution tool. It previously ran
    ``subprocess.run(shell=True)`` directly on the host with no approval and no
    sandbox (its approval gate was a no-op because it was absent from the
    policy), leaving an unprompted RCE surface. It is now hard-stopped against
    destructive commands, gated by the same "always" approval rule, and executed
    through :class:`SandboxRunner` (Docker when available, otherwise a local
    ``sh -c`` with a sanitized environment) exactly like run_bash.
    """
    if is_execution_cancelled():
        return "Execution cancelled by user."
    ws = get_workspace()
    timeout = max(5, min(int(timeout or 30), 120))

    # Hard stop for destructive commands, mirroring run_bash.
    is_dangerous, reason = _is_destructive_command(command)
    if is_dangerous:
        return f"Security Error: Command blocked due to potentially destructive system operation ({reason})."

    # Gated identically to run_bash (default policy "always").
    arguments = {"command": command, "timeout": timeout}
    _gate_approval("run_command", arguments, f"Execute command in {ws}:\n{command}", "command")

    runner = SandboxRunner(
        workspace_path=ws,
        mode=SANDBOX_MODE,
        docker_image=SANDBOX_DOCKER_IMAGE,
        timeout_seconds=timeout,
    )
    result = runner.run_bash_command(
        command=command,
        env=_get_sanitized_env(),
        process_register_cb=_register_process,
    )

    output = result.stdout
    if result.stderr:
        output += "\n[STDERR]:\n" + result.stderr
    if not output.strip():
        output = "Command executed successfully with no output."
    if result.exit_code != 0:
        output = f"Command exited with code {result.exit_code}\n{output}"
    if len(output) > 8000:
        output = output[:8000] + "\n... [truncated]"
    return output


def tool_list_files(pattern: str = "**/*") -> str:
    try:
        ws = get_workspace()
        IGNORE = {'.git', '.agent_memory', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode', 'dist', 'build', '.next'}
        matches = []
        for p in sorted(ws.glob(pattern)):
            parts = p.relative_to(ws).parts
            if any(part in IGNORE for part in parts):
                continue
            matches.append(p)

        if not matches:
            return "Workspace is empty."

        lines = []
        for p in matches[:300]:
            rel = p.relative_to(ws)
            if p.is_dir():
                lines.append(f"📁 {rel}/")
            else:
                size  = p.stat().st_size
                mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m/%d %H:%M")
                lines.append(f"📄 {rel}  ({size:,}b, {mtime})")
        if len(matches) > 300:
            lines.append(f"\n... and {len(matches)-300} more files")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


# Commands worth a precise, specific hard-stop reason, checked BEFORE the
# shared DANGEROUS_BASH_PATTERNS policy list so their specific reason strings
# are preserved (e.g. "Root / home directory deletion"). Anything not matched
# here falls through to is_dangerous_bash — the single 11-pattern detector that
# is_dangerous_bash and should_require_approval both use — so this hard-stop
# can no longer silently drift out of sync with the policy list (it used to
# miss sudo, curl|bash, dd, mkfs, and the C:\\ "rd" alias).
_DESTRUCTIVE_OVERRIDE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[rfRF]{1,4}\s+)?(/\s*$|/\*|~\s*$|\$HOME\b)", re.IGNORECASE), "Root / home directory deletion"),
    (re.compile(r"\b(rd|rmdir)\s+/[sS]\s+/[qQ]\s+[cC]:\\?", re.IGNORECASE), "C:\\ drive root directory wipe"),
    (re.compile(r"\bdel\s+/[fF]\s+/[sS]\s+/[qQ]\s+[cC]:\\?", re.IGNORECASE), "C:\\ drive root file wipe"),
]

_SENSITIVE_ENV_KEYS = {
    "CUSTOM_API_KEY",
    "TAVILY_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN",
    "GIT_PASSWORD",
    "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES",
}


def _is_destructive_command(command: str) -> tuple[bool, str]:
    """Hard-stop detector for run_bash / run_command, run BEFORE the approval
    gate so a truly destructive command is refused even if approval is set to
    "auto".

    A small set of patterns is checked first to keep their specific reason
    strings; everything else delegates to the shared ``is_dangerous_bash``
    policy list, so the hard-stop stays a strict superset of the policy
    detector and the two can no longer drift apart.
    """
    cmd = command.strip()
    for pattern, description in _DESTRUCTIVE_OVERRIDE_PATTERNS:
        if pattern.search(cmd):
            return True, description
    return is_dangerous_bash(cmd)


def _get_sanitized_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _SENSITIVE_ENV_KEYS:
        env.pop(key, None)
    env["PYTHONPATH"] = str(get_workspace().resolve())
    return env


def tool_run_bash(command: str) -> str:
    if is_execution_cancelled():
        return "Execution cancelled by user."
    is_dangerous, danger_reason = _is_destructive_command(command)
    if is_dangerous:
        return f"Security Error: Command blocked due to potentially destructive system operation ({danger_reason})."
    arguments = {"command": command}
    _gate_approval("run_bash", arguments, command, "command")

    runner = SandboxRunner(
        workspace_path=get_workspace(),
        mode=SANDBOX_MODE,
        docker_image=SANDBOX_DOCKER_IMAGE,
        timeout_seconds=EXEC_TIMEOUT,
    )
    result = runner.run_bash_command(
        command=command,
        env=_get_sanitized_env(),
        process_register_cb=_register_process,
    )
    parts = []
    if result.stdout and result.stdout.strip():
        parts.append(f"STDOUT:\n{result.stdout.strip()}")
    if result.stderr and result.stderr.strip():
        parts.append(f"STDERR:\n{result.stderr.strip()}")
    parts.append(f"exit code: {result.exit_code}")
    if result.used_sandbox == "docker":
        parts.append("(Executed inside Docker container sandbox)")
    output = "\n\n".join(parts)
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n... [truncated]"
    return output or "(empty output)"


def tool_run_python(code: str) -> str:
    if is_execution_cancelled():
        return "Execution cancelled by user."
    arguments = {"code": code}
    _gate_approval("run_python", arguments, code, "code")

    runner = SandboxRunner(
        workspace_path=get_workspace(),
        mode=SANDBOX_MODE,
        docker_image=SANDBOX_DOCKER_IMAGE,
        timeout_seconds=EXEC_TIMEOUT,
    )
    result = runner.run_python_code(
        code=code,
        env=_get_sanitized_env(),
        process_register_cb=_register_process,
    )
    parts = []
    if result.stdout and result.stdout.strip():
        parts.append(f"OUTPUT:\n{result.stdout.strip()}")
    if result.stderr and result.stderr.strip():
        parts.append(f"STDERR:\n{result.stderr.strip()}")
    parts.append(f"exit code: {result.exit_code}")
    if result.used_sandbox == "docker":
        parts.append("(Executed inside Docker container sandbox)")
    output = "\n\n".join(parts)
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n... [truncated]"
    return output or "(empty output)"


def _blocked_ip_reason(ip: "ipaddress._BaseAddress") -> str | None:
    """Return a human-readable reason if *ip* is not a safe, public target.

    Only globally-routable public addresses are allowed. Anything that is
    loopback, private, link-local (which includes the cloud metadata address
    169.254.169.254), unspecified, reserved, or carrier-grade-NAT is blocked.
    ``is_global`` is the authoritative gate; the specific branches only shape
    the message.
    """
    if ip.is_global:
        return None
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (includes cloud metadata)"
    if ip.is_private:
        return "private"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_reserved:
        return "reserved"
    return "non-global"


def _resolve_public_target(url: str) -> tuple[str | None, str]:
    """Resolve *url* and return the first public, globally-routable IP to
    connect to. Returns ``(ip, "")`` on success or ``(None, reason)`` on
    failure. The returned IP is one the caller should *pin the socket to*:
    validating DNS and then letting the connect re-resolve independently
    leaves a DNS-rebinding window (the A record flips to a private IP between
    our check and the connect). Pinning closes it.

    Every resolved address is checked — one public and one private fails the
    whole lookup, so a rebinding-style response with a private record cannot
    slip past because a public one was listed first. Non-http schemes and
    unresolvable hosts are rejected outright.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return None, f"Unsupported URL scheme: {parsed.scheme}"
        hostname = parsed.hostname
        if not hostname:
            return None, "Invalid URL hostname"

        try:
            infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            return None, f"Could not resolve hostname: {exc}"

        if not infos:
            return None, "Hostname resolved to no addresses"

        # Any single private/link-local/loopback record fails the whole lookup
        # (a rebinding response with one public and one private must not pass),
        # so scan every address before returning a pin target.
        first_public: str | None = None
        for info in infos:
            ip_str = info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            reason = _blocked_ip_reason(ip)
            if reason:
                return None, f"Requests to {reason} addresses are blocked ({ip_str})"
            if first_public is None:
                first_public = ip_str
        if first_public is None:
            return None, "Hostname resolved to no usable addresses"
        return first_public, ""
    except Exception as exc:
        return None, f"URL parse error: {exc}"


def _is_url_safe(url: str) -> tuple[bool, str]:
    """Return ``(True, "")`` only if *url* is an http(s) URL whose hostname
    resolves exclusively to public, globally-routable addresses. Kept as the
    boolean predicate (used by tests and the redirect validator); the connect
    path uses :func:`_resolve_public_target` so it can pin the socket to the
    validated IP.
    """
    _ip, reason = _resolve_public_target(url)
    if reason:
        return False, reason
    return True, ""


class _SSRFPolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """SSRF policy check for a single redirect hop.

    The main fetch path (:func:`_ssrf_safe_fetch`) applies this same rule
    inline in its redirect loop, so a redirect to a private/metadata address
    is refused *before* the connection is made. This class is kept as the
    standalone predicate (and for unit tests) — it raises when the target
    fails the policy and otherwise delegates to urllib's default behaviour.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe, reason = _is_url_safe(newurl)
        if not safe:
            raise urllib.error.HTTPError(
                newurl, code, f"Redirect to blocked address: {reason}", headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_pinned_http(url: str, method: str, data: bytes | None,
                      headers: dict, timeout: int):
    """Open one HTTP(S) request, pinning the socket to a validated public IP.

    Resolves the hostname, checks *every* address, then connects directly to
    the first public IP (so a DNS-rebinding flip between check and connect
    cannot steer the socket to a private address). The original hostname is
    kept for the ``Host`` header and HTTPS SNI/certificate, so virtual-host
    routing and TLS validation still work. Returns an ``addinfourl``-like
    response; the caller closes it.
    """
    import http.client
    import ssl

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")
    host = parsed.hostname
    if not host:
        raise ValueError("Invalid URL hostname")

    ip, reason = _resolve_public_target(url)
    if reason:
        raise ValueError(reason)
    connect_host = ip or host

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    if parsed.scheme == "https":
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(connect_host, port, context=ctx, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(connect_host, port, timeout=timeout)
    try:
        hdrs = dict(headers or {})
        hdrs["Host"] = host
        conn.request(method.upper(), path, body=data, headers=hdrs)
        resp = conn.getresponse()
        return urllib.response.addinfourl(resp, resp.msg, url, resp.status)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise


def _ssrf_safe_fetch(url: str, method: str = "GET", data: bytes | None = None,
                     headers: dict | None = None, timeout: int = 30,
                     max_redirects: int = 5):
    """Open *url* with the SSRF policy enforced end-to-end.

    For *every* hop (initial request and each redirect):
      1. the target is checked against :func:`_is_url_safe` — non-http schemes
         and private/metadata/loopback destinations are refused before any
         connection;
      2. the socket is pinned to the validated public IP
         (:func:`_open_pinned_http`), closing DNS rebinding.

    Together these close both the "public page → private target" redirect
    bounce and the "A record flips after our check" rebinding window.
    Returns an open ``addinfourl``-like response — use it as a context manager
    so the underlying socket is closed. Raises ``ValueError`` on a policy
    violation; callers surface that as an error string.
    """
    current_url = url
    for _ in range(max_redirects + 1):
        ok, reason = _is_url_safe(current_url)
        if not ok:
            raise ValueError(reason)
        resp = _open_pinned_http(current_url, method, data, headers, timeout)
        status = getattr(resp, "status", None) or getattr(resp, "code", 200)
        if 300 <= status < 400:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise ValueError(f"Redirect with no Location header (status {status})")
            # Resolve relative Location against the current URL.
            current_url = urllib.parse.urljoin(current_url, location)
            method = "GET"
            data = None
            continue
        return resp
    raise ValueError(f"Too many redirects (>{max_redirects}) for {url}")


def _read_response_bounded(resp, max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """Read a response body, never buffering more than *max_bytes* into memory
    (P1-14). Reads in 64 KiB chunks and stops once the ceiling is reached, so a
    large or malicious response cannot exhaust memory before it is truncated.
    The returned bytes are a prefix of the body (at most *max_bytes*)."""
    limit = max(1, int(max_bytes))
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = resp.read(min(64 * 1024, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def tool_fetch_url(url: str, max_chars: int = 4000) -> str:
    try:
        default_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        with _ssrf_safe_fetch(url, headers=default_headers, timeout=10) as resp:
            raw = _read_response_bounded(resp)
            enc = resp.headers.get_content_charset() or "utf-8"
        text = raw.decode(enc, errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [truncated]"
        return text
    except Exception as e:
        return f"Error: {e}"


def tool_web_search(query: str, max_results: int = 5, search_depth: str = "basic") -> str:
    max_results = max(1, min(int(max_results or 5), 10))
    data = _tavily_post("search", {
        "query": query,
        "search_depth": search_depth if search_depth in {"basic", "advanced"} else "basic",
        "max_results": max_results,
        "include_answer": True,
        "include_raw_content": False,
    })
    if data.get("error"):
        return f"❌ {data['error']}"

    lines = [f"# Tavily search: {query}"]
    if data.get("answer"):
        lines += ["", "## Answer", str(data["answer"])]
    results = data.get("results", []) or []
    if results:
        lines += ["", "## Results"]
        for index, item in enumerate(results, 1):
            title = item.get("title") or "(untitled)"
            url = item.get("url") or ""
            content = (item.get("content") or "").strip()
            score = item.get("score")
            lines.append(f"{index}. {title}")
            if url:
                lines.append(f"   URL: {url}")
            if score is not None:
                lines.append(f"   Score: {score}")
            if content:
                lines.append(f"   Snippet: {content[:900]}")
    return "\n".join(lines)[:MAX_OUTPUT_CHARS]


def tool_extract_url(urls: list[str], max_chars: int = 8000) -> str:
    if isinstance(urls, str):
        urls = [urls]
    data = _tavily_post("extract", {
        "urls": urls,
        "extract_depth": "basic",
        "include_images": False,
    })
    if data.get("error"):
        return f"❌ {data['error']}"
    lines = ["# Tavily extract"]
    for item in data.get("results", []) or []:
        url = item.get("url") or ""
        content = item.get("raw_content") or item.get("content") or ""
        lines.append(f"\n## {url}\n{content[:max_chars]}")
    failed = data.get("failed_results", []) or []
    if failed:
        lines.append("\n## Failed")
        for item in failed:
            lines.append(json.dumps(item, ensure_ascii=False)[:1000])
    return "\n".join(lines)[:max_chars]


def tool_search_files(query: str, pattern: str = "**/*", regex: bool = False, max_matches: int = 80) -> str:
    try:
        ws = get_workspace()
        max_matches = max(1, min(int(max_matches or 80), 500))
        flags = re.IGNORECASE
        compiled = re.compile(query, flags) if regex else None
        ignore = {'.git', '.agent_memory', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode', 'dist', 'build', '.next'}
        matches = []
        for path in sorted(ws.glob(pattern or "**/*")):
            if not path.is_file():
                continue
            rel = path.relative_to(ws)
            if any(part in ignore for part in rel.parts) or not _is_probably_text(path):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), 1):
                hit = compiled.search(line) if compiled else query.lower() in line.lower()
                if hit:
                    matches.append(f"{rel}:{line_no}: {line[:260]}")
                    if len(matches) >= max_matches:
                        return "\n".join(matches)
        return "\n".join(matches) if matches else "No matches found."
    except Exception as e:
        return f"Error: {e}"


def tool_read_many_files(paths: list[str], max_chars_each: int = 6000) -> str:
    if isinstance(paths, str):
        paths = [paths]
    max_chars_each = max(500, min(int(max_chars_each or 6000), 20000))
    chunks = []
    for path in paths[:20]:
        chunks.append(f"\n\n--- FILE: {path} ---\n{tool_read_file(path)[:max_chars_each]}")
    return "".join(chunks).strip() or "No files were provided."



def _fuzzy_replace(text: str, old: str, new: str, count: int) -> tuple[str, int]:
    if old in text:
        limit = -1 if int(count or 0) == 0 else int(count)
        changed = text.count(old) if limit < 0 else min(text.count(old), limit)
        return text.replace(old, new, limit), changed
        
    import re
    tokens = old.split()
    if not tokens:
        return text, 0
        
    escaped_tokens = [re.escape(t) for t in tokens]
    pattern_str = r'\s+'.join(escaped_tokens)
    
    try:
        pattern = re.compile(pattern_str)
    except Exception:
        return text, 0
        
    matches = list(pattern.finditer(text))
    if len(matches) == 0:
        return text, 0
        
    limit = len(matches) if int(count or 0) == 0 else int(count)
    changed = min(len(matches), limit)
    
    res = text
    # Replace from back to front to preserve indices
    for match in reversed(matches[:changed]):
        start, end = match.start(), match.end()
        res = res[:start] + new + res[end:]
        
    return res, changed

def tool_replace_in_file(path: str, old: str, new: str, regex: bool = False, count: int = 0) -> str:
    try:
        p = _safe_path(path)
        if not p.exists():
            return f"File does not exist: {path}"
        text = p.read_text(encoding="utf-8", errors="replace")
        if regex:
            updated, changed = re.subn(old, new, text, count=max(0, int(count or 0)))
        else:
            updated, changed = _fuzzy_replace(text, old, new, int(count or 0))
        if updated == text:
            return f"No replacements were made: {path}"
        ws = get_workspace()
        manager = GitManager(ws)
        arguments = {"path": path, "old": old, "new": new, "regex": regex, "count": count}
        preview = manager.get_diff_preview(path, updated) if manager.is_repo() else f"Replace occurrences in {path}"
        _gate_approval("replace_in_file", arguments, preview, "diff")
        p.write_text(updated, encoding="utf-8")
        _update_code_index(path)
        
        _emit_tool_event({"type": "file_diff", "path": path, "diff": preview})
        
        commit_hash = ""
        if manager.is_repo() and AUTO_COMMIT:
            commit_hash = manager.stage_and_commit([path], f"Update {path} with CoderAI")
            if commit_hash:
                _emit_tool_event({"type": "git_commit_created", "commit": commit_hash, "message": f"Update {path} with CoderAI", "files": [path]})
        suffix = f" Committed as {commit_hash[:8]}." if commit_hash else ""
        return f"Replaced {changed} occurrence(s) in {path}.{suffix}"
    except ToolApprovalRequired:
        raise
    except Exception as e:
        return f"Error: {e}"


def tool_append_file(path: str, content: str) -> str:
    try:
        p = _safe_path(path)
        arguments = {"path": path, "content": content}
        manager = GitManager(get_workspace())
        preview = manager.get_diff_preview(path, content) if manager.is_repo() else f"Append {len(content)} bytes to: {path}"
        _gate_approval("append_file", arguments, preview, "diff")
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        _update_code_index(path)
        return f"Appended to file: {path} ({p.stat().st_size:,} bytes)"
    except ToolApprovalRequired:
        raise
    except Exception as e:
        return f"Error: {e}"


def tool_create_directory(path: str) -> str:
    try:
        p = _safe_path(path)
        p.mkdir(parents=True, exist_ok=True)
        return f"Directory is ready: {path}"
    except Exception as e:
        return f"Error: {e}"


def tool_project_tree(max_depth: int = 3, max_entries: int = 300) -> str:
    try:
        ws = get_workspace()
        max_depth = max(1, min(int(max_depth or 3), 8))
        max_entries = max(20, min(int(max_entries or 300), 1000))
        ignore = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode', 'dist', 'build', '.next'}
        lines = [f"{ws.name}/"]
        count = 0
        for current, directories, files in walk_workspace(ws, ignore):
            rel_dir = current.relative_to(ws)
            if len(rel_dir.parts) >= max_depth:
                directories[:] = []
            entries = [(current / name, True) for name in directories] + [(current / name, False) for name in files]
            for path, is_dir in entries:
                rel = path.relative_to(ws)
                if len(rel.parts) > max_depth:
                    continue
                count += 1
                if count > max_entries:
                    lines.append("... more entries omitted")
                    return "\n".join(lines)
                indent = "  " * (len(rel.parts) - 1)
                lines.append(f"{indent}- {rel.name}{'/' if is_dir else ''}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def tool_current_time() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def tool_delete_file(path: str) -> str:
    try:
        p = _safe_path(path)
        if not p.exists():
            return f"File does not exist: {path}"
        arguments = {"path": path}
        manager = GitManager(get_workspace())
        preview = manager.get_diff_preview(path, "") if manager.is_repo() else f"Delete file: {path} ({p.stat().st_size:,} bytes)"
        _gate_approval("delete_file", arguments, preview, "diff")
        p.unlink()
        _update_code_index(path, deleted=True)
        _emit_tool_event({"type": "file_diff", "path": path, "diff": preview})
        return f"Deleted: {path}"
    except ToolApprovalRequired:
        raise
    except Exception as e:
        return f"Error: {e}"


def tool_search_codebase(query: str, top_k: int = 5) -> str:
    try:
        index = get_codebase_index(get_workspace())
        hits = index.retrieve_relevant_code(query, top_k)
        if not hits:
            status = index.status()
            if not status["files"]:
                return "Codebase index is empty. Build it from Files > Index before using search_codebase."
            return f"No indexed code matched: {query}"
        sections = [f"# Codebase search: {query}"]
        for number, hit in enumerate(hits, 1):
            symbol = f" · {hit['symbol_type']} `{hit['symbol_name']}`" if hit.get("symbol_name") else ""
            sections.append(
                f"\n## {number}. `{hit['file_path']}:{hit['start_line']}-{hit['end_line']}`{symbol}\n"
                f"```\n{hit['content'][:3500]}\n```"
            )
        return "\n".join(sections)[:MAX_OUTPUT_CHARS]
    except Exception as e:
        return f"Error: {e}"


def tool_get_project_overview() -> str:
    try:
        overview = get_codebase_index(get_workspace()).get_project_overview()
        
        summary_text = overview.get("summary", "")
        # If the summary is JSON, format it nicely so the agent doesn't regurgitate raw JSON
        if summary_text.strip().startswith("{") and summary_text.strip().endswith("}"):
            try:
                import json
                parsed = json.loads(summary_text)
                if isinstance(parsed, dict):
                    lines = []
                    for k, v in parsed.items():
                        lines.append(f"### {str(k).replace('_', ' ').title()}")
                        if isinstance(v, list):
                            for item in v:
                                if isinstance(item, dict) and "path" in item and "summary" in item:
                                    lines.append(f"- **{item['path']}**: {item['summary']}")
                                else:
                                    lines.append(f"- {item}")
                        elif isinstance(v, dict):
                            for sub_k, sub_v in v.items():
                                lines.append(f"- **{sub_k}**: {sub_v}")
                        else:
                            lines.append(str(v))
                    summary_text = "\n".join(lines)
            except Exception:
                pass
                
        sections = ["# Project overview", summary_text]
        if overview["key_files"]:
            sections.append("\n## Entry points and key files")
            sections.extend(f"- `{item['file_path']}`: {item['summary']}" for item in overview["key_files"])
        sections.append(f"\nGraph: {overview['nodes']} files, {overview['edges']} resolved dependency edges")
        return "\n".join(sections)[:MAX_OUTPUT_CHARS]
    except Exception as e:
        return f"Error: {e}"


def tool_get_related_files(path: str, depth: int = 1) -> str:
    try:
        related = get_codebase_index(get_workspace()).get_related_files(path, max(1, min(int(depth), 3)))
        if not related:
            return f"No resolved import or call relations found for: {path}"
        sections = [f"# Files related to `{path}`"]
        for item in related:
            relations = ", ".join(
                f"{edge['relation_type']}:{edge['detail']} ({edge['from_file']} -> {edge['to_file']})"
                for edge in item["relations"]
            )
            sections.append(f"- `{item['file_path']}` — {relations}\n  {item['summary']}")
        return "\n".join(sections)[:MAX_OUTPUT_CHARS]
    except Exception as e:
        return f"Error: {e}"


def tool_scan_project(max_files: int = 200) -> str:
    """Build an initial project structure report."""
    ws = get_workspace()
    IGNORE = {'.git', '.agent_memory', '__pycache__', 'node_modules', '.venv', 'venv',
              '.idea', '.vscode', 'dist', 'build', '.next', '.mypy_cache'}

    all_files: list[Path] = []
    for p in sorted(iter_workspace_files(ws, IGNORE)):
        all_files.append(p)

    if not all_files:
        return "The project is empty."

    ext_count: dict[str, int] = {}
    for f in all_files:
        ext = f.suffix.lower() or "(no extension)"
        ext_count[ext] = ext_count.get(ext, 0) + 1

    CONFIG_FILES = {
        "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
        "package.json", "package-lock.json", "yarn.lock",
        "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        ".env.example", "Makefile", "README.md", "README.rst",
    }
    found_configs = [f for f in all_files if f.name in CONFIG_FILES]

    top_level: dict[str, list] = {}
    for f in all_files[:max_files]:
        rel   = f.relative_to(ws)
        parts = rel.parts
        key   = parts[0] if len(parts) > 1 else "."
        top_level.setdefault(key, []).append(rel)

    lines = [
        "# Project scan report",
        f"**Path:** `{ws}`",
        f"**Total files:** {len(all_files)}",
        "",
        "## Languages / file types",
    ]
    for ext, cnt in sorted(ext_count.items(), key=lambda x: -x[1])[:15]:
        lines.append(f"- `{ext}`: {cnt} file(s)")

    if found_configs:
        lines += ["", "## Detected configuration files"]
        for cf in found_configs:
            lines.append(f"- `{cf.relative_to(ws)}`")

    lines += ["", "## Main structure"]
    for folder, files in sorted(top_level.items()):
        lines.append(f"**{folder}/** - {len(files)} item(s)")

    if len(all_files) > max_files:
        lines.append(f"\n_(Showing only the first {max_files} files out of {len(all_files)} total.)_")

    try:
        from coderai.codebase.code_graph_service import code_graph_service
        if code_graph_service.is_available:
            threading.Thread(target=code_graph_service.build_or_update, args=(ws,), daemon=True).start()
    except Exception:
        pass

    return "\n".join(lines)


def tool_remember_fact(content: str, context: str = "") -> str:
    """Retain a key observation, rule, or architectural fact into long-term Hindsight memory."""
    try:
        from coderai.memory.hindsight_manager import get_hindsight_manager
        hm = get_hindsight_manager()
        res = hm.retain(content=content, context=context)
        status = res.get("status", "")
        if status == "error":
            return f"Failed to store memory: {res.get('error', 'unknown error')}"
        if status == "skipped":
            return f"Skipped storing memory: {res.get('reason', 'empty content')}"
        bank_id = res.get("bank_id", "default")
        if res.get("engine") == "hindsight":
            return f"Retained in Hindsight memory bank '{bank_id}': {content}"
        return f"Retained in local memory store (bank '{bank_id}'): {content}"
    except Exception as e:
        return f"Failed to store memory: {e}"


def tool_recall_memory(query: str, limit: int = 5) -> str:
    """Recall relevant project memories, facts, and lessons using Hindsight hybrid search."""
    try:
        from coderai.memory.hindsight_manager import get_hindsight_manager
        hm = get_hindsight_manager()
        res = hm.recall(query=query, max_tokens=2048)
        p_str = res.get("prompt_string", "")
        if p_str:
            return f"--- Recalled Memories ({res.get('engine')}, {res.get('count')} item(s)) ---\n{p_str}"
        return f"No memories found for query: '{query}'."
    except Exception as e:
        return f"Error recalling memory: {e}"


def tool_reflect_memory(query: str) -> str:
    """Synthesize deep lessons and project patterns using Hindsight reflection."""
    try:
        from coderai.memory.hindsight_manager import get_hindsight_manager
        hm = get_hindsight_manager()
        return hm.reflect(query=query)
    except Exception as e:
        return f"Error reflecting on memory: {e}"


def tool_get_project_architecture(detail_level: str = "minimal") -> str:
    """Get high-level module architecture and community clusters using code-review-graph."""
    try:
        from coderai.codebase.code_graph_service import code_graph_service
        res = code_graph_service.get_architecture_overview(get_workspace(), detail_level=detail_level)
        if not res.get("ok"):
            return f"Architecture overview unavailable: {res.get('error', 'unknown error')}"
        return json.dumps(res.get("data", {}), indent=2)
    except Exception as exc:
        return f"Error getting project architecture: {exc}"


def tool_get_impact_radius(files: list[str], max_depth: int = 2) -> str:
    """Calculate blast radius across folders for changed or targeted files."""
    try:
        from coderai.codebase.code_graph_service import code_graph_service
        res = code_graph_service.get_impact_radius(get_workspace(), changed_files=files, max_depth=max_depth)
        if not res.get("ok"):
            return f"Impact radius unavailable: {res.get('error', 'unknown error')}"
        return json.dumps(res.get("data", {}), indent=2)
    except Exception as exc:
        return f"Error calculating impact radius: {exc}"


def tool_query_code_graph(pattern: str, symbol: str) -> str:
    """Query AST call graphs, callers, imports, or dependencies."""
    try:
        from coderai.codebase.code_graph_service import code_graph_service
        res = code_graph_service.query_graph(pattern=pattern, target=symbol, workspace_path=get_workspace())
        if not res.get("ok"):
            return f"Graph query failed: {res.get('error', 'unknown error')}"
        return json.dumps(res.get("data", {}), indent=2)
    except Exception as exc:
        return f"Error querying code graph: {exc}"


def tool_get_code_review_context(task: str = "", files: list[str] | None = None) -> str:
    """Extract a token-optimized focused subgraph slice for a review task."""
    try:
        from coderai.codebase.code_graph_service import code_graph_service
        res = code_graph_service.get_minimal_context(get_workspace(), task=task, changed_files=files)
        if not res.get("ok"):
            return f"Code review context unavailable: {res.get('error', 'unknown error')}"
        return json.dumps(res.get("data", {}), indent=2)
    except Exception as exc:
        return f"Error getting code review context: {exc}"


# ── Advanced shell tool gating ─────────────────────────────────────────────────
# The advanced shell tools used to run model-controlled strings via shell=True with
# no approval. They are now gated behind the per-request approval token (see
# _gate_approval) and, where they wrap a fixed binary, executed with argv lists or
# through the sandboxed runner so shell metacharacters are not a second, ungated,
# injection surface. All approval knowledge stays in tools.py so advanced_tools
# remains a dumb executor (tools.py imports advanced_tools, not the other way).


def _run_via_sandbox(command: str, timeout_seconds: int = 60) -> str:
    """Run a shell command via SandboxRunner and format the result like tool_run_bash.

    Keeps shell semantics (pipes, flags) while honoring the sandbox mode: Docker when
    available, otherwise a local ``sh -c``. Returns a string with STDOUT/STDERR/exit
    code so callers get a consistent, reviewable shape.
    """
    runner = SandboxRunner(
        workspace_path=get_workspace(),
        mode=SANDBOX_MODE,
        docker_image=SANDBOX_DOCKER_IMAGE,
        timeout_seconds=timeout_seconds,
    )
    result = runner.run_bash_command(
        command=command,
        env=_get_sanitized_env(),
        process_register_cb=_register_process,
    )
    parts = []
    if result.stdout and result.stdout.strip():
        parts.append(f"STDOUT:\n{result.stdout.strip()}")
    if result.stderr and result.stderr.strip():
        parts.append(f"STDERR:\n{result.stderr.strip()}")
    parts.append(f"exit code: {result.exit_code}")
    if result.used_sandbox == "docker":
        parts.append("(Executed inside Docker container sandbox)")
    output = "\n\n".join(parts)
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n... [truncated]"
    return output or "(empty output)"


def _run_host_subprocess(argv: list[str], timeout_seconds: int = 60) -> str:
    """Run a fixed-binary command as an argv list on the host (no shell).

    Used for the Docker CLI tools, which must not be nested inside the Docker sandbox.
    Arguments are passed as a list so shell metacharacters in the model input cannot
    spawn extra processes.
    """
    try:
        res = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(get_workspace()),
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout_seconds}s: {argv[0]}"
    except FileNotFoundError as exc:
        return f"Error: {exc}"
    parts = []
    if res.stdout and res.stdout.strip():
        parts.append(f"STDOUT:\n{res.stdout.strip()}")
    if res.stderr and res.stderr.strip():
        parts.append(f"STDERR:\n{res.stderr.strip()}")
    parts.append(f"exit code: {res.returncode}")
    output = "\n\n".join(parts)
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n... [truncated]"
    return output or "(empty output)"


def _guarded_command(tool_name: str, arguments: dict, preview: str, run_fn) -> str:
    """Shared seam for the advanced shell tools: approve first, then execute.

    ``run_fn(arguments)`` is invoked only after the approval gate passes, so a
    prompt-injected model cannot run these commands without an explicit user approval.
    """
    _gate_approval(tool_name, arguments, preview, "command")
    return run_fn(arguments)


# ── SQL tool gating (local SQLite only) ───────────────────────────────────────
# execute_sql_query / get_database_schema used to accept a model-supplied connection
# string (including remote databases) and commit arbitrary statements. They are now
# restricted to a workspace-local SQLite file, and write statements require an
# explicit approval (read-only SELECT/PRAGMA/EXPLAIN/WITH do not).


def _resolve_sqlite_in_workspace(connection_string: str) -> tuple[Path | None, str | None]:
    """Resolve a SQLite connection string to a workspace-local file.

    Returns ``(path, None)`` on success or ``(None, error_message)`` on failure.
    Only local SQLite files reachable by a workspace-relative path are accepted.
    Any other scheme (postgresql://, mysql://, ...) or any path that escapes the
    workspace is rejected, so a prompt cannot point the tool at a remote database.
    """
    cs = (connection_string or "").strip()
    if not cs:
        return None, "Error: empty connection string."

    if "://" in cs:
        scheme = cs.split("://", 1)[0].strip().lower()
        if scheme != "sqlite":
            return None, (f"Error: only local SQLite databases inside the workspace are "
                          f"allowed. Got scheme '{scheme}'.")
        candidate = cs.split("://", 1)[1].lstrip("/")
    else:
        candidate = cs[len("sqlite:"):] if cs.lower().startswith("sqlite:") else cs
    candidate = candidate.strip()
    if not candidate:
        return None, "Error: no database path in connection string."

    ws = get_workspace()
    try:
        target = (ws / candidate).resolve()
        target.relative_to(ws.resolve())
    except (ValueError, PermissionError):
        return None, f"Error: database path must be inside the workspace: {cs}"
    return target, None


def _run_sql(path: Path, query: str) -> str:
    """Run a single SQL statement against a local SQLite file and return its rows."""
    import sqlite3
    try:
        conn = sqlite3.connect(str(path))
    except Exception as e:
        return f"Error connecting to database: {e}"
    try:
        cur = conn.execute(query)
        if cur.description is not None:
            rows = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
            return json.dumps(rows, indent=2, default=str)
        conn.commit()
        return "Query executed successfully. (No rows returned)"
    except Exception as e:
        return f"Error executing query: {e}"
    finally:
        conn.close()


def _get_schema_sqlite(path: Path) -> str:
    """Return the schema (tables + columns) of a local SQLite file."""
    import sqlite3
    try:
        conn = sqlite3.connect(str(path))
    except Exception as e:
        return f"Error connecting to database: {e}"
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        if not tables:
            return "No tables found."
        info = []
        for table in tables:
            cols = [f"{c[1]} ({c[2]})" for c in conn.execute(f'PRAGMA table_info("{table}")')]
            info.append(f"Table: {table}\n  Columns: {', '.join(cols)}")
        return "\n".join(info)
    except Exception as e:
        return f"Error reading schema: {e}"
    finally:
        conn.close()


def _guarded_sql_query(arguments: dict) -> str:
    """Gated execute_sql_query: local-SQLite-only, write statements require approval."""
    path, err = _resolve_sqlite_in_workspace(str(arguments.get("connection_string", "")))
    if err:
        return err
    query = str(arguments.get("query", ""))
    preview = query if len(query) <= 400 else query[:400] + "…"
    _gate_approval("execute_sql_query", arguments, preview, "command")
    return _run_sql(path, query)


def _guarded_sql_schema(arguments: dict) -> str:
    """Gated get_database_schema: local-SQLite-only, read-only reflection."""
    path, err = _resolve_sqlite_in_workspace(str(arguments.get("connection_string", "")))
    if err:
        return err
    return _get_schema_sqlite(path)


# ── Dispatcher ─────────────────────────────────────────────────────────────────
_HANDLERS: dict = {
    "read_file":    lambda a: tool_read_file(a["path"], a.get("start_line"), a.get("end_line"), a.get("mode", "raw")),
    "write_file":   lambda a: tool_write_file(a["path"], a["content"]),
    "run_command":  lambda a: tool_run_command(a["command"], a.get("timeout", 30)),
    "check_file_diagnostics": lambda a: tool_check_file_diagnostics(a["path"]),
    "list_files":   lambda a: tool_list_files(a.get("pattern", "**/*")),
    "run_bash":     lambda a: tool_run_bash(a["command"]),
    "run_python":   lambda a: tool_run_python(a["code"]),
    "fetch_url":    lambda a: tool_fetch_url(a["url"], a.get("max_chars", 4000)),
    "web_search":   lambda a: tool_web_search(a["query"], a.get("max_results", 5), a.get("search_depth", "basic")),
    "extract_url":  lambda a: tool_extract_url(a["urls"], a.get("max_chars", 8000)),
    "search_files": lambda a: tool_search_files(a["query"], a.get("pattern", "**/*"), a.get("regex", False), a.get("max_matches", 80)),
    "read_many_files": lambda a: tool_read_many_files(a["paths"], a.get("max_chars_each", 6000)),
    "replace_in_file": lambda a: tool_replace_in_file(a["path"], a["old"], a["new"], a.get("regex", False), a.get("count", 0)),
    "append_file":  lambda a: tool_append_file(a["path"], a["content"]),
    "create_directory": lambda a: tool_create_directory(a["path"]),
    "project_tree": lambda a: tool_project_tree(a.get("max_depth", 3), a.get("max_entries", 300)),
    "current_time": lambda a: tool_current_time(),
    "delete_file":  lambda a: tool_delete_file(a["path"]),
    "search_codebase": lambda a: tool_search_codebase(a["query"], a.get("top_k", 5)),
    "get_project_overview": lambda a: tool_get_project_overview(),
    "get_related_files": lambda a: tool_get_related_files(a["path"], a.get("depth", 1)),
    "scan_project": lambda a: tool_scan_project(a.get("max_files", 200)),
    "remember_fact":  lambda a: tool_remember_fact(a["content"], a.get("context", "")),
    "recall_memory":  lambda a: tool_recall_memory(a["query"], a.get("limit", 5)),
    "reflect_memory": lambda a: tool_reflect_memory(a["query"]),
    "get_project_architecture": lambda a: tool_get_project_architecture(a.get("detail_level", "minimal")),
    "get_impact_radius": lambda a: tool_get_impact_radius(a.get("files", []), a.get("max_depth", 2)),
    "query_code_graph": lambda a: tool_query_code_graph(a["pattern"], a["symbol"]),
    "get_code_review_context": lambda a: tool_get_code_review_context(a.get("task", ""), a.get("files")),
    "git_status": lambda a: tool_git_status(),
    "git_log": lambda a: tool_git_log(a.get("limit", 10)),
    "git_diff": lambda a: tool_git_diff(a.get("staged", False)),
    "git_commit": lambda a: tool_git_commit(a.get("message"), a.get("files")),
    "git_checkout": lambda a: tool_git_checkout(a.get("branch"), a.get("create", False)),
    "get_database_schema": lambda a: _guarded_sql_schema(a),
    "execute_sql_query": lambda a: _guarded_sql_query(a),
    "navigate_web": lambda a: advanced_tools.tool_navigate_web(a.get("url", "")),
    "take_screenshot": lambda a: advanced_tools.tool_take_screenshot(a.get("url", ""), a.get("output_path", "")),
    "run_docker_container": lambda a: _guarded_command(
        "run_docker_container", a,
        f"docker run --rm {a.get('image', '')}",
        lambda a: _run_host_subprocess(
            ["docker", "run", "--rm", *shlex.split((a.get("image", "") + " " + a.get("command", "")).strip())],
            timeout_seconds=120,
        ),
    ),
    "get_container_logs": lambda a: _guarded_command(
        "get_container_logs", a,
        f"docker logs {a.get('container_name_or_id', '')}",
        lambda a: _run_host_subprocess(["docker", "logs", a.get("container_name_or_id", "")]),
    ),
    "run_linter": lambda a: _guarded_command(
        "run_linter", a, a.get("command", "flake8 ."),
        lambda a: _run_via_sandbox(a.get("command", "flake8 ."), timeout_seconds=60),
    ),
    "run_tests": lambda a: _guarded_command(
        "run_tests", a, a.get("command", "pytest"),
        lambda a: _run_via_sandbox(a.get("command", "pytest"), timeout_seconds=120),
    ),
    "run_kubectl": lambda a: _guarded_command(
        "run_kubectl", a, f"kubectl {a.get('command', '')}",
        lambda a: _run_via_sandbox(f"kubectl {a.get('command', '')}", timeout_seconds=60),
    ),
    "run_terraform": lambda a: _guarded_command(
        "run_terraform", a, f"terraform {a.get('command', '')}",
        lambda a: _run_via_sandbox(f"terraform {a.get('command', '')}", timeout_seconds=120),
    ),
    "test_api_endpoint": lambda a: advanced_tools.tool_test_api_endpoint(a.get("url", ""), a.get("method", "GET"), a.get("headers"), a.get("json_body")),
    "run_npm_script": lambda a: _guarded_command(
        "run_npm_script", a,
        f"{a.get('package_manager', 'npm')} run {a.get('script_name', '')}",
        lambda a: _run_via_sandbox(
            f"{a.get('package_manager', 'npm')} run {a.get('script_name', '')}", timeout_seconds=120
        ),
    ),
}



def tool_git_status() -> str:
    from coderai.codebase.git_manager import GitManager
    try:
        mgr = GitManager(get_workspace())
        if not mgr.is_repo(): return "Not a git repository."
        import json
        return json.dumps(mgr.get_status(), indent=2)
    except Exception as e:
        return f"Error: {e}"

def tool_git_log(limit: int = 10) -> str:
    from coderai.codebase.git_manager import GitManager
    try:
        mgr = GitManager(get_workspace())
        if not mgr.is_repo(): return "Not a git repository."
        import json
        return json.dumps(mgr.get_log(limit=limit), indent=2)
    except Exception as e:
        return f"Error: {e}"

def tool_git_diff(staged: bool = False) -> str:
    from coderai.codebase.git_manager import GitManager
    try:
        mgr = GitManager(get_workspace())
        if not mgr.is_repo(): return "Not a git repository."
        diff = mgr.get_diff(staged=staged)
        return diff if diff.strip() else "No differences found."
    except Exception as e:
        return f"Error: {e}"

def tool_git_commit(message: str, files: list = None) -> str:
    from coderai.codebase.git_manager import GitManager
    try:
        mgr = GitManager(get_workspace())
        if not mgr.is_repo(): return "Not a git repository."
        status = mgr.get_status()
        if not files:
            files = [f["path"] for f in status.get("files", [])]
        if not files:
            return "No files specified or found to commit."
        # Committing mutates history on the workspace repo, so it is gated
        # behind the same approval policy as git_push / git_revert.
        arguments = {"message": message, "files": files}
        _gate_approval("git_commit", arguments, f"git commit {files}: {message}", "generic")
        commit_hash = mgr.stage_and_commit(files, message)
        if commit_hash:
            _emit_tool_event({"type": "git_commit_created", "commit": commit_hash, "message": message, "files": files})
            return f"Committed successfully. Hash: {commit_hash}"
        return "Nothing to commit."
    except ToolApprovalRequired:
        raise
    except Exception as e:
        return f"Error: {e}"

def tool_git_checkout(branch: str, create: bool = False) -> str:
    from coderai.codebase.git_manager import GitManager
    try:
        mgr = GitManager(get_workspace())
        if not mgr.is_repo(): return "Not a git repository."
        # Switching branches can discard uncommitted work, so it is gated the
        # same way as git_push / git_revert / git_commit.
        arguments = {"branch": branch, "create": create}
        _gate_approval("git_checkout", arguments, f"git checkout {branch}{' --create' if create else ''}", "generic")
        res = mgr.switch_branch(name=branch, create=create)
        import json
        return json.dumps(res, indent=2)
    except ToolApprovalRequired:
        raise
    except Exception as e:
        return f"Error: {e}"

def execute_tool(name: str, arguments: dict | str) -> str | dict:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return f"Could not parse tool arguments: {arguments}"
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool: {name}"
    try:
        return handler(arguments)
    except ToolApprovalRequired as exc:
        return json.dumps({
            "status": "approval_required",
            "token": exc.token,
            "tool_name": exc.tool_name,
            "arguments": exc.arguments,
            "preview": exc.preview,
        })
    except KeyError as e:
        return f"Missing required argument: {e}"
    except Exception as e:
        return f"Error in '{name}': {e}"
