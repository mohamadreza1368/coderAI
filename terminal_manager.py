"""
terminal_manager.py - Manages native pseudoterminal (PTY) and shell sessions
powered by ConPTY/WinPTY and xterm.js (the technology behind VS Code's integrated terminal).
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
import re
from typing import Generator

try:
    import winpty
    HAS_WINPTY = True
except Exception:
    HAS_WINPTY = False

# Strip VT device attribute queries and unsolicited capability probes
# that trigger xterm to send stdin keystrokes (like ?1;2c) back to the shell.
VT_QUERY_FILTER = re.compile(r"\x1b\[c|\x1b\[\?1004[hl]|\x1b\[\?9001[hl]|\x1b\[[12]t|\x1b\[\?1;[0-9]+c|\?1;[0-9]+c")


def _contained_cwd(cwd: str | Path | None) -> Path:
    """Return *cwd* if it is inside the active workspace, else the workspace.

    A terminal session anchored outside the workspace (e.g. ``C:\\Windows``,
    the user profile) becomes a user-RCE surface once ``execute`` runs a
    command there. The requested cwd is resolved; if it escapes the active
    workspace the session is re-anchored to the workspace root instead.
    """
    from tools import get_workspace

    ws = Path(get_workspace()).resolve()
    if cwd:
        try:
            requested = Path(cwd).resolve()
            requested.relative_to(ws)
            return requested
        except (ValueError, OSError):
            pass  # escapes the workspace (or unresolvable)
    return ws if ws.exists() else Path.cwd().resolve()


class TerminalSession:
    """Represents an interactive terminal execution session."""

    def __init__(
        self,
        session_id: str,
        shell_type: str = "powershell",
        cwd: Path | None = None,
        rows: int = 24,
        cols: int = 80,
    ):
        self.session_id = session_id
        self.shell_type = shell_type.lower()
        self.cwd = _contained_cwd(cwd)
        self.rows = max(5, rows)
        self.cols = max(10, cols)
        self.pty_proc: winpty.PtyProcess | None = None
        self.active_process: subprocess.Popen | None = None
        self.output_queue: queue.Queue[dict] = queue.Queue()
        self._subscribers: set[Any] = set()
        self._subscribers_lock = threading.Lock()
        self.output_buffer: list[str] = []
        self._buffer_lock = threading.Lock()
        self.history: list[str] = []
        self._lock = threading.Lock()
        self._is_running = False
        self._reader_thread: threading.Thread | None = None

        self.start_shell()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._subscribers_lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subscribers_lock:
            self._subscribers.discard(q)

    def subscribe_async(self, loop: Any) -> tuple[tuple[Any, Any], Any]:
        import asyncio
        q: asyncio.Queue = asyncio.Queue()
        handle = (loop, q)
        with self._subscribers_lock:
            self._subscribers.add(handle)
        return handle, q

    def unsubscribe_async(self, handle: tuple[Any, Any]) -> None:
        with self._subscribers_lock:
            self._subscribers.discard(handle)

    def _broadcast(self, item: dict) -> None:
        self.output_queue.put(item)
        with self._subscribers_lock:
            dead = []
            for sub in self._subscribers:
                try:
                    if isinstance(sub, tuple):
                        sub_loop, async_q = sub
                        if not sub_loop.is_closed():
                            sub_loop.call_soon_threadsafe(async_q.put_nowait, item)
                        else:
                            dead.append(sub)
                    else:
                        sub.put_nowait(item)
                except Exception:
                    dead.append(sub)
            for d in dead:
                self._subscribers.discard(d)

    def get_shell_command_args(self) -> list[str]:
        if self.shell_type in {"cmd", "command prompt", "cmd.exe"}:
            return ["cmd.exe", "/k"]
        if self.shell_type in {"bash", "git bash", "git-bash"}:
            bash_path = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
            if os.path.exists(bash_path):
                return [bash_path, "-i"]
        pwsh = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        return [pwsh, "-NoLogo"]

    def start_shell(self) -> None:
        with self._lock:
            self._close_internal()
            with self._buffer_lock:
                self.output_buffer.clear()
            if not self.cwd.exists():
                self.cwd = Path.cwd().resolve()

            shell_args = self.get_shell_command_args()

            # For powershell and bash, winpty provides genuine pseudoterminal handling
            if HAS_WINPTY and self.shell_type not in {"cmd", "command prompt", "cmd.exe"}:
                try:
                    self.pty_proc = winpty.PtyProcess.spawn(
                        shell_args,
                        cwd=str(self.cwd),
                        dimensions=(self.rows, self.cols),
                    )
                    self._is_running = True
                    self._reader_thread = threading.Thread(target=self._pty_reader, daemon=True)
                    self._reader_thread.start()
                    return
                except Exception as exc:
                    self.output_queue.put({"type": "output", "text": f"\r\n[WinPTY error: {exc}]\r\n", "data": f"\r\n[WinPTY error: {exc}]\r\n"})

            # Fallback to standard subprocess
            try:
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                env["TERM"] = "xterm-256color"

                self.active_process = subprocess.Popen(
                    shell_args,
                    cwd=str(self.cwd),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=0,
                    universal_newlines=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )
                self._is_running = True
                self._reader_thread = threading.Thread(target=self._proc_reader, daemon=True)
                self._reader_thread.start()
            except Exception as exc:
                self._is_running = False
                self.output_queue.put({"type": "output", "text": f"\r\n[Process error: {exc}]\r\n", "data": f"\r\n[Process error: {exc}]\r\n"})

    def _pty_reader(self) -> None:
        while True:
            proc = self.pty_proc
            if not proc or not proc.isalive():
                break
            try:
                data = proc.read()
                if data:
                    clean = VT_QUERY_FILTER.sub("", data)
                    if clean:
                        self._broadcast({"type": "raw", "data": clean, "text": clean})
                        with self._buffer_lock:
                            self.output_buffer.append(clean)
                            if len(self.output_buffer) > 1000:
                                self.output_buffer.pop(0)
                else:
                    time.sleep(0.01)
            except EOFError:
                break
            except Exception:
                time.sleep(0.01)
        with self._lock:
            self._is_running = False

    def _proc_reader(self) -> None:
        proc = self.active_process
        if not proc or not proc.stdout:
            return
        try:
            while True:
                char = proc.stdout.read(1)
                if not char:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.02)
                    continue
                buf = [char]
                while True:
                    try:
                        next_char = proc.stdout.read(1)
                        if next_char:
                            buf.append(next_char)
                            if next_char == "\n" or len(buf) >= 512:
                                break
                        else:
                            break
                    except Exception:
                        break
                data = "".join(buf)
                clean = VT_QUERY_FILTER.sub("", data)
                if clean:
                    self._broadcast({"type": "raw", "data": clean, "text": clean})
                    with self._buffer_lock:
                        self.output_buffer.append(clean)
                        if len(self.output_buffer) > 1000:
                            self.output_buffer.pop(0)
        except Exception as exc:
            self._broadcast({"type": "output", "text": f"\r\n[Read error: {exc}]\r\n", "data": f"\r\n[Read error: {exc}]\r\n"})
        finally:
            with self._lock:
                self._is_running = False

    def get_output_history(self) -> str:
        with self._buffer_lock:
            return "".join(self.output_buffer)

    def write(self, data: str) -> None:
        with self._lock:
            if self.pty_proc and self.pty_proc.isalive():
                try:
                    self.pty_proc.write(data)
                except Exception:
                    pass
            elif self.active_process and self.active_process.poll() is None and self.active_process.stdin:
                try:
                    self.active_process.stdin.write(data)
                    self.active_process.stdin.flush()
                except Exception:
                    pass

    def write_stdin(self, text: str) -> bool:
        self.write(text + "\r\n")
        return True

    def execute(self, command: str, cwd: Path | None = None) -> None:
        if cwd and cwd != self.cwd:
            self.set_cwd(cwd)
        if not self.is_running():
            self.start_shell()
        self.history.append(command)
        self.write(command + "\r\n")

    def resize(self, rows: int, cols: int) -> None:
        self.rows = max(5, rows)
        self.cols = max(10, cols)
        with self._lock:
            if self.pty_proc and self.pty_proc.isalive():
                try:
                    self.pty_proc.setwinsize(self.rows, self.cols)
                except Exception:
                    pass

    def restart(self, shell_type: str | None = None) -> None:
        if shell_type:
            self.shell_type = shell_type.lower()
        self.start_shell()

    def _close_internal(self) -> None:
        if self.pty_proc and self.pty_proc.isalive():
            try:
                self.pty_proc.terminate()
            except Exception:
                pass
            self.pty_proc = None
        if self.active_process and self.active_process.poll() is None:
            try:
                self.active_process.kill()
            except Exception:
                pass
            self.active_process = None
        self._is_running = False

    def kill(self) -> bool:
        with self._lock:
            if self.pty_proc and self.pty_proc.isalive():
                # Send Ctrl+C (0x03)
                try:
                    self.pty_proc.write("\x03")
                    return True
                except Exception:
                    pass
            elif self.active_process and self.active_process.poll() is None:
                try:
                    self.active_process.kill()
                    return True
                except Exception:
                    pass
            return False

    def is_running(self) -> bool:
        with self._lock:
            if self.pty_proc:
                return self.pty_proc.isalive()
            if self.active_process:
                return self.active_process.poll() is None
            return False

    def set_cwd(self, new_cwd: str | Path) -> bool:
        # Refuse to re-anchor outside the workspace; keep the current cwd if the
        # requested path escapes (an escape attempt stays contained).
        try:
            requested = Path(new_cwd).resolve()
        except (OSError, ValueError):
            return False
        contained = _contained_cwd(new_cwd)
        if contained != requested:
            return False
        if contained.exists() and contained.is_dir():
            self.cwd = contained
            if not self.is_running():
                self.start_shell()
            return True
        return False

    def stream_events(self) -> Generator[dict, None, None]:
        while True:
            try:
                event = self.output_queue.get(timeout=1.0)
                yield event
            except queue.Empty:
                yield {"type": "ping"}


class TerminalManager:
    _instance: TerminalManager | None = None

    def __new__(cls) -> TerminalManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._sessions = {}
            cls._instance._lock = threading.Lock()
        return cls._instance

    def get_or_create_session(
        self,
        session_id: str = "default",
        shell_type: str = "powershell",
        cwd: Path | None = None,
        rows: int = 24,
        cols: int = 80,
    ) -> TerminalSession:
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = TerminalSession(
                    session_id,
                    shell_type=shell_type,
                    cwd=cwd,
                    rows=rows,
                    cols=cols,
                )
            else:
                session = self._sessions[session_id]
                if shell_type and session.shell_type != shell_type.lower():
                    session.restart(shell_type)
                elif not session.is_running():
                    session.start_shell()
            return self._sessions[session_id]

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                session._close_internal()

    def list_available_shells(self) -> list[dict]:
        shells = [
            {"id": "powershell", "name": "PowerShell", "available": bool(shutil.which("powershell") or shutil.which("pwsh"))},
            {"id": "cmd", "name": "Command Prompt", "available": bool(shutil.which("cmd.exe") or os.name == "nt")},
        ]
        bash = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
        if os.path.exists(bash):
            shells.append({"id": "bash", "name": "Git Bash", "available": True})
        return shells


terminal_manager = TerminalManager()
