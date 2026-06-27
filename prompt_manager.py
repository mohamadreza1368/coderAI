"""
prompt_manager.py - load, categorize, and search bundled system prompts.

Every Markdown file in system_prompts/ is treated as a selectable system prompt.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from dataclasses import dataclass

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)).resolve()
PROMPTS_DIR = ROOT / "system_prompts"

# Categories inferred from prompt file names.
_CATEGORY_MAP: dict[str, str] = {
    "Claude":   "🟣 Anthropic",
    "ChatGPT":  "🟢 OpenAI",
    "Gemini":   "🔵 Google",
    "Grok":     "⚫ xAI",
    "Cursor":   "🔧 Coding IDEs",
    "GitHub":   "🔧 Coding IDEs",
    "Codex":    "🔧 Coding IDEs",
    "V0":       "🎨 Web Builders",
    "Bolt":     "🎨 Web Builders",
    "Cascade":  "🎨 Web Builders",
    "Qodo":     "🧪 Dev Tools",
    "Proton":   "📧 Other",
    "Perplexity": "🔍 Search",
    "ChatGPT Deep Research": "🔍 Search",
}


def _get_category(name: str) -> str:
    for prefix, cat in _CATEGORY_MAP.items():
        if name.startswith(prefix):
            return cat
    return "🔧 Other"


def _extract_preview(content: str, max_chars: int = 180) -> str:
    """Return a compact preview from the first meaningful prompt lines."""
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    preview = " ".join(lines[:3])
    if len(preview) > max_chars:
        preview = preview[:max_chars] + "..."
    return preview


@dataclass
class SystemPrompt:
    name:     str       # File stem without .md
    content:  str       # Full prompt content
    category: str       # UI category
    path:     Path
    size:     int       # Character count

    @property
    def preview(self) -> str:
        return _extract_preview(self.content)

    @property
    def label(self) -> str:
        return f"{self.name}  ({self.size//1000}k chars)"


class PromptManager:
    """Load and provide access to bundled system prompts."""

    def __init__(self) -> None:
        self._prompts: dict[str, SystemPrompt] = {}
        self._load()

    def _load(self) -> None:
        if not PROMPTS_DIR.exists():
            return
        for f in sorted(PROMPTS_DIR.glob("*.md")):
            name = f.stem
            content = f.read_text(encoding="utf-8", errors="replace")
            self._prompts[name] = SystemPrompt(
                name=name,
                content=content,
                category=_get_category(name),
                path=f,
                size=len(content),
            )

    # Accessors.

    def all(self) -> list[SystemPrompt]:
        return sorted(self._prompts.values(), key=lambda p: (p.category, p.name))

    def by_category(self) -> dict[str, list[SystemPrompt]]:
        result: dict[str, list[SystemPrompt]] = {}
        for p in self.all():
            result.setdefault(p.category, []).append(p)
        return result

    def get(self, name: str) -> SystemPrompt | None:
        return self._prompts.get(name)

    def count(self) -> int:
        return len(self._prompts)

    def names(self) -> list[str]:
        return [p.name for p in self.all()]

    def search(self, query: str) -> list[SystemPrompt]:
        q = query.lower()
        return [p for p in self.all()
                if q in p.name.lower() or q in p.content.lower()[:500]]


_instance: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    global _instance
    if _instance is None:
        _instance = PromptManager()
    return _instance
