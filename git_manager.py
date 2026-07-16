"""Independent Git operations for agent workspaces."""

from __future__ import annotations

import difflib
import base64
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable


class GitError(RuntimeError):
    """Raised when a Git operation cannot be completed."""


class GitManager:
    def __init__(self, workspace_path: str | Path):
        self.repo_path = Path(workspace_path).resolve()
        self._ensure_repo()

    @staticmethod
    def _startupinfo():
        if os.name != "nt":
            return None
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
        return info

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                startupinfo=self._startupinfo(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"Git is unavailable: {exc}") from exc
        if check and result.returncode:
            raise GitError((result.stderr or result.stdout or "Git command failed").strip())
        return result

    @classmethod
    def clone_repository(
        cls,
        remote_url: str,
        destination: str | Path,
        on_output: Callable[[str], None] | None = None,
        username: str = "",
        token: str = "",
    ) -> "GitManager":
        destination = Path(destination).resolve()
        if destination.exists() and any(destination.iterdir()):
            raise GitError(f"Clone destination is not empty: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        env = cls._credential_env(username, token)
        process = subprocess.Popen(
            ["git", "clone", "--progress", remote_url, str(destination)],
            cwd=str(destination.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            startupinfo=cls._startupinfo(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert process.stdout is not None
        for line in process.stdout:
            clean = line.rstrip()
            if clean and on_output:
                on_output(clean)
        return_code = process.wait()
        if return_code:
            raise GitError(f"git clone failed with exit code {return_code}")
        return cls(destination)

    def _ensure_repo(self) -> bool:
        return self.is_repo()

    def is_repo(self) -> bool:
        if not self.repo_path.is_dir():
            return False
        try:
            result = self._run("rev-parse", "--is-inside-work-tree", check=False)
        except GitError:
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def init_repo(self) -> None:
        self.repo_path.mkdir(parents=True, exist_ok=True)
        self._run("init")
        gitignore = self.repo_path / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "__pycache__/\n*.py[cod]\n.venv/\nvenv/\nnode_modules/\ndist/\nbuild/\n.env\n",
                encoding="utf-8",
            )

    def get_diff(self, staged: bool = False) -> str:
        if not self.is_repo():
            return ""
        args = ["diff"]
        if staged:
            args.append("--cached")
        args.extend(["--no-ext-diff", "--no-color"])
        return self._run(*args).stdout

    def get_diff_preview(self, path: str, new_content: str) -> str:
        target = self._safe_file(path)
        old_content = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        return "".join(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        ))

    def get_status(self) -> dict:
        if not self.is_repo():
            return {"is_repo": False, "branch": None, "files": []}
        branch = self._run("branch", "--show-current", check=False).stdout.strip() or "HEAD"
        lines = self._run("status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()
        remote = self.get_remote()
        ahead, behind = self.get_ahead_behind()
        return {
            "is_repo": True,
            "branch": branch,
            "clean": not lines,
            "files": [{"status": line[:2], "path": line[3:]} for line in lines if len(line) >= 4],
            "remote": remote,
            "ahead": ahead,
            "behind": behind,
        }

    def get_remote(self, name: str = "origin") -> str:
        if not self.is_repo():
            return ""
        return self._run("remote", "get-url", name, check=False).stdout.strip()

    def get_ahead_behind(self) -> tuple[int, int]:
        if not self.is_repo():
            return 0, 0
        result = self._run("rev-list", "--left-right", "--count", "@{upstream}...HEAD", check=False)
        if result.returncode or not result.stdout.strip():
            return 0, 0
        behind, ahead = (int(value) for value in result.stdout.split()[:2])
        return ahead, behind

    def get_push_preview(self) -> dict:
        status = self.get_status()
        branch = status.get("branch") or "HEAD"
        remote = status.get("remote") or ""
        commits = []
        result = self._run("log", "--format=%h%x1f%s", "@{upstream}..HEAD", check=False)
        if result.returncode and remote:
            result = self._run("log", "--format=%h%x1f%s", f"origin/{branch}..HEAD", check=False)
        for line in result.stdout.splitlines():
            parts = line.split("\x1f", 1)
            if len(parts) == 2:
                commits.append({"hash": parts[0], "message": parts[1]})
        return {"remote": remote, "branch": branch, "commits": commits, "ahead": len(commits) or status.get("ahead", 0)}

    def push(self, username: str = "", token: str = "", remote: str = "origin") -> str:
        if not self.is_repo():
            raise GitError("Workspace is not a Git repository")
        branch = self._run("branch", "--show-current").stdout.strip()
        if not branch:
            raise GitError("Cannot push from a detached HEAD")
        env = self._credential_env(username, token)
        result = subprocess.run(
            ["git", "push", "--set-upstream", remote, branch],
            cwd=str(self.repo_path), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300, env=env,
            startupinfo=self._startupinfo(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode:
            raise GitError((result.stderr or result.stdout or "Git push failed").strip())
        return (result.stdout or result.stderr or "Push completed").strip()

    def stage_files(self, files: list[str]) -> None:
        if not files:
            return
        safe = [str(self._safe_file(path).relative_to(self.repo_path)) for path in files]
        self._run("add", "--", *safe)

    def commit(self, message: str, files: list[str] | None = None) -> str:
        safe_files: list[str] = []
        if files:
            safe_files = [str(self._safe_file(path).relative_to(self.repo_path)) for path in files]
            self.stage_files(files)
        diff_args = ["diff", "--cached", "--no-ext-diff", "--no-color"]
        if safe_files:
            diff_args.extend(["--", *safe_files])
        if not self._run(*diff_args).stdout.strip():
            return ""
        self._ensure_identity()
        commit_args = ["commit", "-m", message]
        if safe_files:
            commit_args.extend(["--only", "--", *safe_files])
        self._run(*commit_args)
        return self._run("rev-parse", "HEAD").stdout.strip()

    def stage_and_commit(self, files: list[str], message: str) -> str:
        return self.commit(message, files)

    def create_checkpoint_branch(self) -> str:
        if not self.is_repo():
            raise GitError("Workspace is not a Git repository")
        base = datetime.now().strftime("coderai-checkpoint-%Y%m%d-%H%M%S")
        name = base
        suffix = 1
        while self._run("show-ref", "--verify", f"refs/heads/{name}", check=False).returncode == 0:
            suffix += 1
            name = f"{base}-{suffix}"
        self._run("branch", name)
        return name

    def revert_to(self, commit_hash: str) -> str:
        if not self.is_repo():
            raise GitError("Workspace is not a Git repository")
        self._run("rev-parse", "--verify", f"{commit_hash}^{{commit}}")
        result = self._run("revert", "--no-edit", commit_hash)
        return self._run("rev-parse", "HEAD").stdout.strip() if result.returncode == 0 else ""

    def get_log(self, limit: int = 20) -> list[dict]:
        if not self.is_repo():
            return []
        limit = max(1, min(int(limit), 100))
        fmt = "%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1e"
        result = self._run("log", f"--max-count={limit}", f"--pretty=format:{fmt}", check=False)
        entries = []
        for record in result.stdout.strip("\x1e\n").split("\x1e"):
            fields = record.strip().split("\x1f")
            if len(fields) == 5:
                entries.append(dict(zip(("hash", "short_hash", "author", "date", "message"), fields)))
        return entries

    def _safe_file(self, path: str) -> Path:
        target = (self.repo_path / path).resolve()
        try:
            target.relative_to(self.repo_path)
        except ValueError as exc:
            raise GitError(f"Path is outside workspace: {path}") from exc
        return target

    def _ensure_identity(self) -> None:
        if self._run("config", "user.name", check=False).returncode:
            self._run("config", "user.name", "CoderAI Agent")
        if self._run("config", "user.email", check=False).returncode:
            self._run("config", "user.email", "coderai@localhost")

    @staticmethod
    def _credential_env(username: str = "", token: str = "") -> dict[str, str]:
        env = os.environ.copy()
        if not token:
            return env
        identity = username.strip() or "x-access-token"
        encoded = base64.b64encode(f"{identity}:{token}".encode("utf-8")).decode("ascii")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {encoded}"
        return env
