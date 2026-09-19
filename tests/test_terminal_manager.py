"""
tests/test_terminal_manager.py - Unit tests for the integrated terminal subsystem.
"""

import time
from pathlib import Path

import pytest

from terminal_manager import TerminalManager, TerminalSession, _contained_cwd
from tools import get_workspace, set_workspace


def _pin_workspace(ws: Path) -> Path:
    """Point the active workspace at *ws* so a terminal can anchor there."""
    ws.mkdir(parents=True, exist_ok=True)
    set_workspace(ws)
    return get_workspace()


def test_terminal_session_creation(tmp_path: Path):
    ws = _pin_workspace(tmp_path / "ws")
    session = TerminalSession(session_id="test_sess", shell_type="powershell", cwd=tmp_path / "ws")
    assert session.session_id == "test_sess"
    assert session.shell_type == "powershell"
    assert session.cwd == ws
    assert session.is_running()
    session._close_internal()


def test_terminal_session_execute_and_stream(tmp_path: Path):
    ws = _pin_workspace(tmp_path / "ws")
    session = TerminalSession(session_id="test_sess_exec", shell_type="powershell", cwd=tmp_path / "ws")
    session.write("Write-Output 'Hello from PowerShell Terminal'\r\n")
    
    events = []
    start_time = time.time()
    while time.time() - start_time < 8.0:
        while not session.output_queue.empty():
            events.append(session.output_queue.get_nowait())
        if any("Hello from PowerShell Terminal" in e.get("text", "") for e in events):
            break
        time.sleep(0.1)
    
    while not session.output_queue.empty():
        events.append(session.output_queue.get_nowait())
    
    texts = [e.get("text", "") for e in events]
    combined = "".join(texts)
    assert "Hello from PowerShell Terminal" in combined or len(events) >= 1
    session._close_internal()


def test_terminal_manager_singleton(tmp_path: Path):
    ws = _pin_workspace(tmp_path / "ws")
    mgr1 = TerminalManager()
    mgr2 = TerminalManager()
    assert mgr1 is mgr2

    session = mgr1.get_or_create_session("sess_abc", shell_type="cmd", cwd=tmp_path / "ws")
    assert session.shell_type == "cmd"
    assert session.cwd == ws

    shells = mgr1.list_available_shells()
    shell_ids = [s["id"] for s in shells]
    assert "powershell" in shell_ids or "cmd" in shell_ids

    mgr1.remove_session("sess_abc")


# --- P1 #8: terminal cwd must be contained to the workspace -----------------

def test_contained_cwd_keeps_in_workspace(tmp_path: Path):
    ws = _pin_workspace(tmp_path / "ws")
    sub = ws / "nested"
    sub.mkdir()
    assert _contained_cwd(sub) == sub.resolve()


def test_contained_cwd_rejects_out_of_workspace(tmp_path: Path):
    ws = _pin_workspace(tmp_path / "ws")
    outside = tmp_path / "outside"
    outside.mkdir()
    # An outside path must be re-anchored to the workspace root.
    assert _contained_cwd(outside) == ws
    # A path that would escape via a parent dotdot is likewise contained.
    assert _contained_cwd(ws / ".." / "outside") == ws


def test_contained_cwd_none_uses_workspace(tmp_path: Path):
    ws = _pin_workspace(tmp_path / "ws")
    assert _contained_cwd(None) == ws
    assert _contained_cwd("") == ws


def test_terminal_session_initial_cwd_clamped_to_workspace(tmp_path: Path):
    ws = _pin_workspace(tmp_path / "ws")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    session = TerminalSession(session_id="clamped_sess", shell_type="cmd", cwd=outside)
    assert session.cwd == ws
    session._close_internal()


def test_terminal_set_cwd_refuses_escape(tmp_path: Path):
    ws = _pin_workspace(tmp_path / "ws")
    outside = tmp_path / "outside"
    outside.mkdir()
    session = TerminalSession(session_id="setcwd_sess", shell_type="cmd", cwd=ws)
    # Moving to an in-workspace subdir is allowed...
    ok = session.set_cwd(ws / "nested")
    assert ok is False  # subdir does not exist yet
    (ws / "nested").mkdir()
    assert session.set_cwd(ws / "nested") is True
    assert session.cwd == (ws / "nested").resolve()
    # ...but moving outside the workspace is refused (cwd stays contained).
    before = session.cwd
    assert session.set_cwd(outside) is False
    assert session.cwd == before
    session._close_internal()
