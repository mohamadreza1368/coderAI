"""
tools.py - tool definitions and execution helpers.

The workspace path is mutable. Each tool resolves the current path through
get_workspace() before reading or writing files.
"""

import os
import subprocess
import json
import re
import difflib
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

MAX_OUTPUT_CHARS = 8_000
EXEC_TIMEOUT     = 15
TAVILY_ENABLED = os.getenv("TAVILY_ENABLED", "false").lower() == "true"
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# Default workspace used before the user selects a project folder.
_DEFAULT_WORKSPACE = Path(os.getenv("AGENT_WORKSPACE", "./agent_workspace")).resolve()
_DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)

# Updated whenever the user changes the active workspace.
WORKSPACE_DIR: Path = _DEFAULT_WORKSPACE


# ══════════════════════════════════════════════════════════════════════════════
# ── Execution Approval State
# ══════════════════════════════════════════════════════════════════════════════

class ToolApprovalRequired(Exception):
    """Raised when a tool call requires explicit user approval before execution."""
    def __init__(self, tool_name: str, arguments: dict, preview: str):
        self.tool_name = tool_name
        self.arguments = arguments
        self.preview = preview
        super().__init__(f"Approval required for {tool_name}")


_approval_state: dict = {
    "pending": False,
    "tool_name": "",
    "arguments": {},
    "preview": "",
    "approved": False,
    "rejected": False,
    "rejection_reason": "",
    "always_allow": False,
}


def get_approval_state() -> dict:
    """Return a copy of the current approval state."""
    return dict(_approval_state)


def approve_pending(always_allow_for_session: bool = False) -> None:
    """Mark the pending approval request as approved."""
    global _approval_state
    _approval_state["approved"] = True
    _approval_state["rejected"] = False
    _approval_state["pending"] = False
    if always_allow_for_session:
        _approval_state["always_allow"] = True


def reject_pending(reason: str = "") -> None:
    """Mark the pending approval request as rejected."""
    global _approval_state
    _approval_state["rejected"] = True
    _approval_state["approved"] = False
    _approval_state["pending"] = False
    _approval_state["rejection_reason"] = reason or ""


def clear_approval_state() -> None:
    """Reset approval state (call on chat reset)."""
    global _approval_state
    _approval_state = {
        "pending": False,
        "tool_name": "",
        "arguments": {},
        "preview": "",
        "approved": False,
        "rejected": False,
        "rejection_reason": "",
        "always_allow": False,
    }


def _request_approval(tool_name: str, arguments: dict, preview: str) -> None:
    """Set pending approval state and raise ToolApprovalRequired."""
    global _approval_state
    _approval_state["pending"] = True
    _approval_state["tool_name"] = tool_name
    _approval_state["arguments"] = arguments
    _approval_state["preview"] = preview
    _approval_state["approved"] = False
    _approval_state["rejected"] = False
    _approval_state["rejection_reason"] = ""
    raise ToolApprovalRequired(tool_name, arguments, preview)


def _consume_tool_approval(tool_name: str) -> bool:
    if _approval_state["always_allow"]:
        return True
    if _approval_state["approved"] and _approval_state["tool_name"] == tool_name:
        _approval_state["approved"] = False
        _approval_state["tool_name"] = ""
        return True
    return False


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
            "name": "read_file",
            "description": "Read a text file from the active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the workspace"}
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
            "name": "git_status",
            "description": "Return the current Git branch and porcelain status for the active workspace.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Return a Git diff for the active workspace. Use this before reporting changed files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional file path to diff", "default": ""},
                    "staged": {"type": "boolean", "description": "Show staged diff when true", "default": False},
                    "max_chars": {"type": "integer", "description": "Maximum output characters", "default": 8000},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Return recent Git commits for the active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of commits. Default: 8", "default": 8}
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
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            errors.append(f"HTTP {exc.code}: {detail[:600]}")
        except Exception as exc:
            errors.append(str(exc))
    return {"error": "Tavily request failed. " + " | ".join(errors)}


def _run_git(args: list[str], timeout: int = 10, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(get_workspace()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return "Git is not installed or is not available on PATH."
    except subprocess.TimeoutExpired:
        return f"Git command timed out after {timeout}s."
    except Exception as exc:
        return f"Git error: {exc}"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    output = "\n".join(part for part in (stdout, stderr) if part)
    if result.returncode != 0:
        return output or f"Git exited with code {result.returncode}."
    if len(output) > max_chars:
        return output[:max_chars] + "\n... [truncated]"
    return output or "(empty output)"


def _is_git_repo() -> bool:
    return not _run_git(["rev-parse", "--is-inside-work-tree"], timeout=5, max_chars=100).lower().startswith(("fatal", "git exited", "git is not"))


class GitManager:
    """Small Git integration layer used by write tools."""

    @staticmethod
    def is_repo() -> bool:
        return _is_git_repo()

    @staticmethod
    def diff_preview(path: str, new_content: str, max_chars: int = 20_000) -> str:
        try:
            target = _safe_path(path)
            old_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            old_lines = old_text.splitlines()
            new_lines = str(new_content or "").splitlines()
            diff = "\n".join(difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
            ))
            if not diff:
                return f"No content changes for {path}."
            if len(diff) > max_chars:
                return diff[:max_chars] + "\n... [diff truncated]"
            return diff
        except Exception as exc:
            return f"Could not build diff preview for {path}: {exc}"

    @staticmethod
    def commit(path: str, message: str | None = None) -> dict:
        if not GitManager.is_repo():
            return {"ok": False, "skipped": True, "message": "Git commit skipped: workspace is not a Git repository."}

        try:
            safe = _safe_path(path)
            rel = str(safe.relative_to(get_workspace().resolve()))
        except Exception as exc:
            return {"ok": False, "skipped": False, "message": f"Git commit failed: {exc}"}

        add_output = _run_git(["add", "--", rel], timeout=10, max_chars=4000)
        if add_output.lower().startswith(("fatal", "git exited", "git error", "git command timed out")):
            return {"ok": False, "skipped": False, "message": f"Git add failed: {add_output}"}

        status = _run_git(["diff", "--cached", "--quiet", "--", rel], timeout=10, max_chars=1000)
        if status and not status.startswith("(empty output)"):
            # `git diff --quiet` exits 1 when there are changes; _run_git returns an empty-ish message only on 0.
            pass

        commit_message = message or f"Agent: update {rel}"
        commit_output = _run_git(["commit", "-m", commit_message, "--", rel], timeout=30, max_chars=8000)
        if "nothing to commit" in commit_output.lower():
            return {"ok": False, "skipped": True, "message": "Git commit skipped: nothing to commit."}
        if commit_output.lower().startswith(("fatal", "git exited", "git error", "git command timed out")):
            return {"ok": False, "skipped": False, "message": f"Git commit failed: {commit_output}"}

        commit_hash = _run_git(["rev-parse", "--short", "HEAD"], timeout=10, max_chars=200).strip()
        return {"ok": True, "skipped": False, "hash": commit_hash, "message": commit_message, "output": commit_output}


# ══════════════════════════════════════════════════════════════════════════════
# ── Tool Handlers
# ══════════════════════════════════════════════════════════════════════════════

def tool_read_file(path: str) -> str:
    try:
        p = _safe_path(path)
        if not p.exists():
            return f"File does not exist: {path}"
        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > MAX_OUTPUT_CHARS:
            content = content[:MAX_OUTPUT_CHARS] + f"\n\n... [truncated - {len(content)} total characters]"
        return content
    except Exception as e:
        return f"Error: {e}"


def tool_write_file(path: str, content: str) -> str:
    try:
        if not _consume_tool_approval("write_file"):
            preview = GitManager.diff_preview(path, content)
            _request_approval("write_file", {"path": path, "content": content}, preview)

        p = _safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        lines = [f"File written: {path} ({p.stat().st_size:,} bytes)"]
        commit = GitManager.commit(path)
        if commit.get("ok"):
            lines.append(f"Git commit: {commit.get('hash')} {commit.get('message')}")
        else:
            lines.append(commit.get("message", "Git commit was not created."))
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def tool_list_files(pattern: str = "**/*") -> str:
    try:
        ws = get_workspace()
        IGNORE = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode', 'dist', 'build', '.next'}
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


def tool_run_bash(command: str) -> str:
    if not _consume_tool_approval("run_bash"):
        _request_approval("run_bash", {"command": command}, command)
    if _approval_state.get("rejected"):
        reason = _approval_state.get("rejection_reason") or "User rejected execution."
        _approval_state["rejected"] = False
        return f"Execution rejected: {reason}"
    try:
        result = subprocess.run(
            command, shell=True, cwd=str(get_workspace()),
            capture_output=True, text=True, timeout=EXEC_TIMEOUT,
        )
        parts = []
        if result.stdout.strip(): parts.append(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr.strip(): parts.append(f"STDERR:\n{result.stderr.strip()}")
        parts.append(f"exit code: {result.returncode}")
        output = "\n\n".join(parts)
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return output or "(empty output)"
    except subprocess.TimeoutExpired:
        return f"Timeout: exceeded {EXEC_TIMEOUT} seconds"
    except Exception as e:
        return f"Error: {e}"


def tool_run_python(code: str) -> str:
    if not _consume_tool_approval("run_python"):
        _request_approval("run_python", {"code": code}, code)
    if _approval_state.get("rejected"):
        reason = _approval_state.get("rejection_reason") or "User rejected execution."
        _approval_state["rejected"] = False
        return f"Execution rejected: {reason}"
    tmp = get_workspace() / "__tmp_agent__.py"
    try:
        tmp.write_text(code, encoding="utf-8")
        result = subprocess.run(
            ["python3", str(tmp)], cwd=str(get_workspace()),
            capture_output=True, text=True, timeout=EXEC_TIMEOUT,
        )
        parts = []
        if result.stdout.strip(): parts.append(f"OUTPUT:\n{result.stdout.strip()}")
        if result.stderr.strip(): parts.append(f"STDERR:\n{result.stderr.strip()}")
        parts.append(f"exit code: {result.returncode}")
        output = "\n\n".join(parts)
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return output or "(empty output)"
    except subprocess.TimeoutExpired:
        return f"Timeout: exceeded {EXEC_TIMEOUT} seconds"
    except Exception as e:
        return f"Error: {e}"
    finally:
        tmp.unlink(missing_ok=True)


def tool_fetch_url(url: str, max_chars: int = 4000) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        enc  = resp.headers.get_content_charset() or "utf-8"
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
        ignore = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode', 'dist', 'build', '.next'}
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


def tool_replace_in_file(path: str, old: str, new: str, regex: bool = False, count: int = 0) -> str:
    try:
        p = _safe_path(path)
        if not p.exists():
            return f"File does not exist: {path}"
        text = p.read_text(encoding="utf-8", errors="replace")
        if regex:
            updated, changed = re.subn(old, new, text, count=max(0, int(count or 0)))
        else:
            changed = text.count(old) if int(count or 0) == 0 else min(text.count(old), int(count))
            updated = text.replace(old, new, int(count or 0))
        if updated == text:
            return f"No replacements were made: {path}"
        p.write_text(updated, encoding="utf-8")
        return f"Replaced {changed} occurrence(s) in {path}."
    except Exception as e:
        return f"Error: {e}"


def tool_append_file(path: str, content: str) -> str:
    try:
        p = _safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended to file: {path} ({p.stat().st_size:,} bytes)"
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
        for path in sorted(ws.rglob("*")):
            rel = path.relative_to(ws)
            if any(part in ignore for part in rel.parts) or len(rel.parts) > max_depth:
                continue
            count += 1
            if count > max_entries:
                lines.append(f"... more entries omitted")
                break
            indent = "  " * (len(rel.parts) - 1)
            suffix = "/" if path.is_dir() else ""
            lines.append(f"{indent}- {rel.name}{suffix}")
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
        p.unlink()
        return f"Deleted: {path}"
    except Exception as e:
        return f"Error: {e}"


def tool_git_status() -> str:
    if not _is_git_repo():
        return "The active workspace is not inside a Git repository."
    branch = _run_git(["branch", "--show-current"], max_chars=200)
    upstream = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], max_chars=200)
    short = _run_git(["status", "--short", "--branch"], max_chars=MAX_OUTPUT_CHARS)
    return "\n".join([
        f"Branch: {branch or '(detached)'}",
        f"Upstream: {upstream if not upstream.startswith('fatal') else '(none)'}",
        "",
        short,
    ]).strip()


def tool_git_diff(path: str = "", staged: bool = False, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    if not _is_git_repo():
        return "The active workspace is not inside a Git repository."
    max_chars = max(500, min(int(max_chars or MAX_OUTPUT_CHARS), 30_000))
    args = ["diff"]
    if staged:
        args.append("--staged")
    if path:
        safe = _safe_path(path)
        rel = safe.relative_to(get_workspace().resolve())
        args += ["--", str(rel)]
    return _run_git(args, max_chars=max_chars)


def tool_git_log(limit: int = 8) -> str:
    if not _is_git_repo():
        return "The active workspace is not inside a Git repository."
    limit = max(1, min(int(limit or 8), 30))
    return _run_git(["log", f"-{limit}", "--oneline", "--decorate"], max_chars=MAX_OUTPUT_CHARS)


def tool_scan_project(max_files: int = 200) -> str:
    """Build an initial project structure report."""
    ws = get_workspace()
    IGNORE = {'.git', '__pycache__', 'node_modules', '.venv', 'venv',
              '.idea', '.vscode', 'dist', 'build', '.next', '.mypy_cache'}

    all_files: list[Path] = []
    for p in sorted(ws.rglob("*")):
        if p.is_file():
            parts = p.relative_to(ws).parts
            if not any(part in IGNORE for part in parts):
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

    return "\n".join(lines)


# ── Dispatcher ─────────────────────────────────────────────────────────────────
_HANDLERS: dict = {
    "read_file":    lambda a: tool_read_file(a["path"]),
    "write_file":   lambda a: tool_write_file(a["path"], a["content"]),
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
    "scan_project": lambda a: tool_scan_project(a.get("max_files", 200)),
    "git_status":   lambda a: tool_git_status(),
    "git_diff":     lambda a: tool_git_diff(a.get("path", ""), a.get("staged", False), a.get("max_chars", MAX_OUTPUT_CHARS)),
    "git_log":      lambda a: tool_git_log(a.get("limit", 8)),
}


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
            "tool_name": exc.tool_name,
            "arguments": exc.arguments,
            "preview": exc.preview,
        })
    except KeyError as e:
        return f"Missing required argument: {e}"
    except Exception as e:
        return f"Error in '{name}': {e}"
