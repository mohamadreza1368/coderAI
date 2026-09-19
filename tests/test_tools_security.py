import json
import os
from pathlib import Path

import pytest

import tools
from approval_policy import DEFAULT_GLOBAL_POLICY
from tools import (
    _is_destructive_command,
    _get_sanitized_env,
    clear_approval_state,
    execute_tool,
    resolve_approval,
    set_workspace,
    tool_run_bash,
)


def test_destructive_command_blocking():
    is_danger, reason = _is_destructive_command("rm -rf /")
    assert is_danger
    assert "Root" in reason

    is_danger, reason = _is_destructive_command("del /f /s /q C:\\")
    assert is_danger
    assert "C:\\" in reason

    is_danger, reason = _is_destructive_command("format D:")
    assert is_danger

    is_danger, reason = _is_destructive_command("echo hello world")
    assert not is_danger


def test_destructive_command_in_run_bash():
    from tools import reset_cancel_flag
    reset_cancel_flag()
    result = tool_run_bash("rm -rf /")
    assert "Security Error" in result
    assert "Command blocked" in result


def test_benign_run_bash_requires_approval(tmp_path):
    """P1 #9/#12: even a non-destructive command prompts (default policy 'always').

    With the sandbox falling back to a local shell (no Docker) and the
    destructive blocklist trivially bypassed, 'dangerous_only' left an
    unprompted RCE surface. Any run_bash must now be gated.
    """
    ws = _fresh_ws(tmp_path)
    # "echo" matches no destructive pattern, but still requires approval.
    payload = _approval_payload(execute_tool("run_bash", {"command": "echo hello"}))
    assert payload["status"] == "approval_required", f"run_bash not gated: {payload}"
    assert payload["tool_name"] == "run_bash"
    assert payload.get("token")


def test_sanitized_env():
    os.environ["CUSTOM_API_KEY"] = "secret_12345"
    os.environ["TAVILY_API_KEY"] = "tvly_secret"
    sanitized = _get_sanitized_env()
    assert "CUSTOM_API_KEY" not in sanitized
    assert "TAVILY_API_KEY" not in sanitized
    assert "PYTHONPATH" in sanitized


def _fresh_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    set_workspace(ws)
    clear_approval_state()
    tools.reset_cancel_flag()
    return ws


def _approval_payload(out) -> dict:
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return {"status": "not-approval-required", "raw": out}
    if isinstance(data, dict) and data.get("status") == "approval_required":
        return data
    return {"status": "not-approval-required", "raw": out}


def test_delete_append_in_default_policy():
    assert DEFAULT_GLOBAL_POLICY.get("delete_file") == "always"
    assert DEFAULT_GLOBAL_POLICY.get("append_file") == "always"


def test_delete_file_requires_approval(tmp_path):
    ws = _fresh_ws(tmp_path)
    target = ws / "victim.txt"
    target.write_text("important", encoding="utf-8")
    payload = _approval_payload(execute_tool("delete_file", {"path": "victim.txt"}))
    assert payload["status"] == "approval_required", f"delete_file not gated: {payload}"
    assert payload["tool_name"] == "delete_file"
    assert payload.get("token")
    # Not approved -> file untouched.
    assert target.exists()


def test_delete_file_deletes_after_approval(tmp_path):
    ws = _fresh_ws(tmp_path)
    target = ws / "victim.txt"
    target.write_text("important", encoding="utf-8")
    args = {"path": "victim.txt"}
    payload = _approval_payload(execute_tool("delete_file", args))
    assert payload["status"] == "approval_required"
    resolve_approval(payload["token"], True)
    out = execute_tool("delete_file", args)  # identical args -> same token -> approved
    assert "approval_required" not in out
    assert not target.exists()


def test_append_file_requires_approval(tmp_path):
    ws = _fresh_ws(tmp_path)
    target = ws / "notes.txt"
    target.write_text("line1\n", encoding="utf-8")
    payload = _approval_payload(execute_tool("append_file", {
        "path": "notes.txt", "content": "line2\n",
    }))
    assert payload["status"] == "approval_required", f"append_file not gated: {payload}"
    assert payload["tool_name"] == "append_file"
    assert payload.get("token")
    # Not approved -> file unchanged.
    assert target.read_text(encoding="utf-8") == "line1\n"


def test_append_file_appends_after_approval(tmp_path):
    ws = _fresh_ws(tmp_path)
    target = ws / "notes.txt"
    target.write_text("line1\n", encoding="utf-8")
    args = {"path": "notes.txt", "content": "line2\n"}
    payload = _approval_payload(execute_tool("append_file", args))
    assert payload["status"] == "approval_required"
    resolve_approval(payload["token"], True)
    out = execute_tool("append_file", args)  # identical args -> same token -> approved
    assert "approval_required" not in out
    assert target.read_text(encoding="utf-8") == "line1\nline2\n"
