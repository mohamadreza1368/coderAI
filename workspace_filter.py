"""Shared workspace traversal that prunes dependencies and virtual environments."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator


IGNORED_DIRECTORY_NAMES = {
    ".git", ".agent_memory", ".idea", ".vscode", ".next",
    ".venv", "venv", "env", ".env", "virtualenv",
    ".tox", ".nox", ".conda", "conda-env", "conda_env",
    "__pycache__", "node_modules", "site-packages",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}


def is_environment_directory(path: Path) -> bool:
    """Recognize Python/Conda environments even when their folder has a custom name."""
    return (path / "pyvenv.cfg").is_file() or (path / "conda-meta").is_dir()


def should_prune_directory(path: Path, extra_names: set[str] | None = None) -> bool:
    names = IGNORED_DIRECTORY_NAMES | {name.lower() for name in (extra_names or set())}
    return path.name.lower() in names or path.is_symlink() or is_environment_directory(path)


def is_ignored_workspace_path(root: str | Path, path: str | Path, extra_names: set[str] | None = None) -> bool:
    workspace = Path(root).resolve()
    target = Path(path).resolve()
    try:
        relative = target.relative_to(workspace)
    except ValueError:
        return True
    current = workspace
    for part in relative.parts[:-1]:
        current /= part
        if should_prune_directory(current, extra_names):
            return True
    return False


def walk_workspace(root: str | Path, extra_names: set[str] | None = None) -> Iterator[tuple[Path, list[str], list[str]]]:
    """Yield an os.walk traversal after removing ignored directories in place."""
    workspace = Path(root).resolve()
    for current, directories, files in os.walk(workspace, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name for name in directories
            if not should_prune_directory(current_path / name, extra_names)
        )
        yield current_path, directories, sorted(files)


def iter_workspace_files(root: str | Path, extra_names: set[str] | None = None) -> Iterator[Path]:
    for current, _, files in walk_workspace(root, extra_names):
        for name in files:
            yield current / name
