"""
skills_manager.py - load skills, support slash commands, and parse skill usage.

The auto-select prompt tells the model which skills are available. Responses can
declare used skills with an HTML comment such as:
<!-- skills: tdd, diagnosing-bugs -->
"""

from __future__ import annotations

import re
import sys
import ast
from pathlib import Path
from dataclasses import dataclass


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)).resolve()
SKILLS_DIR = ROOT / "skills"


@dataclass
class Skill:
    name:        str
    description: str
    content:     str
    category:    str
    path:        Path
    triggers:    list[str] | None = None
    disable_model_invocation: bool = False

    @property
    def slash_command(self) -> str:
        return f"/{self.name}"

    @property
    def system_injection(self) -> str:
        """Inject full skill instructions when a skill is explicitly selected."""
        return (
            f"\n\n---\n"
            f"## Active Skill: `/{self.name}`\n\n"
            f"{self.content}\n"
            f"---\n"
        )


@dataclass
class SkillSelection:
    skill: Skill
    triggered_by: str
    matched_keywords: list[str]

    @property
    def reason(self) -> str:
        if self.triggered_by == "slash_command":
            return f"explicit /{self.skill.name} command"
        if self.triggered_by == "pinned":
            return "selected in the project skills panel"
        return "matched " + ", ".join(f'"{word}"' for word in self.matched_keywords)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# The model can declare used skills with this response tag.
_SKILL_TAG_RE   = re.compile(r"<!--\s*skills:\s*([^>]+?)-->", re.IGNORECASE)


def _parse_skill_file(path: Path, category: str) -> "Skill | None":
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None

    fm_match = _FRONTMATTER_RE.match(raw)
    if not fm_match:
        return None

    fm_text = fm_match.group(1)
    content = raw[fm_match.end():]

    meta: dict[str, str] = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"')

    name        = meta.get("name", path.parent.name)
    description = meta.get("description", "")
    disable     = meta.get("disable-model-invocation", "false").lower() == "true"
    try:
        parsed_triggers = ast.literal_eval(meta.get("triggers", "[]"))
        triggers = [str(item).strip() for item in parsed_triggers] if isinstance(parsed_triggers, (list, tuple)) else []
    except (ValueError, SyntaxError):
        triggers = [item.strip() for item in meta.get("triggers", "").strip("[]").split(",") if item.strip()]

    return Skill(
        name=name,
        description=description,
        content=content.strip(),
        category=category,
        path=path,
        triggers=triggers,
        disable_model_invocation=disable,
    )


class SkillsManager:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not SKILLS_DIR.exists():
            return
        for skill_file in sorted(SKILLS_DIR.rglob("SKILL.md")):
            parts    = skill_file.relative_to(SKILLS_DIR).parts
            category = parts[0] if len(parts) >= 2 else "misc"
            skill    = _parse_skill_file(skill_file, category)
            if skill:
                self._skills[skill.name] = skill

    def reload(self) -> None:
        self._skills.clear()
        self._load_all()

    def all(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: (s.category, s.name))

    def by_category(self) -> dict[str, list[Skill]]:
        result: dict[str, list[Skill]] = {}
        for skill in self.all():
            result.setdefault(skill.category, []).append(skill)
        return result

    def get(self, name: str) -> "Skill | None":
        return self._skills.get(name)

    def count(self) -> int:
        return len(self._skills)

    # Slash command detection.
    _SLASH_RE = re.compile(r"^/([a-zA-Z0-9_\-]+)")

    def detect_skill_command(self, message: str) -> "Skill | None":
        m = self._SLASH_RE.match(message.strip())
        if not m:
            return None
        return self._skills.get(m.group(1))

    def strip_command(self, message: str) -> str:
        return self._SLASH_RE.sub("", message).strip()

    def detect_skill_commands(self, message: str) -> list[Skill]:
        """
        Extract all known slash commands from the beginning of a message.
        Example: /tdd /diagnosing-bugs fix the login flow.
        """
        found: list[Skill] = []
        for token in message.strip().split():
            if not token.startswith("/"):
                break
            name = token[1:]
            skill = self._skills.get(name)
            if skill and skill not in found:
                found.append(skill)
            if not skill:
                break
        return found

    def strip_commands(self, message: str) -> str:
        """Remove all known slash commands from a message."""
        cleaned = message
        for skill in self.detect_skill_commands(message):
            cleaned = re.sub(rf"(^|\s)/{re.escape(skill.name)}(?=\s|$)", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def select_relevant_skills(self, message: str, pinned: list[str] | None = None, limit: int = 3) -> list[SkillSelection]:
        """Select skills deterministically and expose why each one matched."""
        selections: list[SkillSelection] = []
        seen: set[str] = set()
        for skill in self.detect_skill_commands(message):
            selections.append(SkillSelection(skill, "slash_command", [f"/{skill.name}"]))
            seen.add(skill.name)
        for name in pinned or []:
            skill = self.get(name)
            if skill and skill.name not in seen:
                selections.append(SkillSelection(skill, "pinned", []))
                seen.add(skill.name)

        query_words = set(_meaningful_words(message))
        ranked: list[tuple[int, Skill, list[str]]] = []
        for skill in self.all():
            if skill.name in seen or skill.disable_model_invocation:
                continue
            name_words = set(_meaningful_words(skill.name.replace("-", " ")))
            description_words = set(_meaningful_words(skill.description))
            matches = sorted(query_words & (name_words | description_words))
            if not matches:
                continue
            score = sum(3 if word in name_words else 1 for word in matches)
            ranked.append((score, skill, matches[:6]))
        ranked.sort(key=lambda item: (-item[0], item[1].name))
        for _, skill, matches in ranked[:max(0, int(limit) - len(selections))]:
            selections.append(SkillSelection(skill, "keyword_match", matches))
        return selections[:limit]

    # Auto-select prompt.
    def build_auto_select_prompt(self) -> str:
        """
        Build the system prompt section that teaches the model:
          1. which skills exist
          2. how to choose relevant skills
          3. how to declare which skills were used
        """
        available = [s for s in self.all() if not s.disable_model_invocation]
        if not available:
            return ""

        skill_list = "\n".join(
            f"- `/{s.name}` ({s.category}): {s.description}"
            for s in available
        )

        return f"""

---
## Available Skills

You have access to the following skills. **Automatically select the most relevant skill name(s)** based on the user's request. Full instructions are injected only for pinned/slash-selected skills to keep the context window healthy:

{skill_list}

### How to use skills:
1. **Identify** which skill(s) fit the user's request
2. If a skill was pinned or selected with slash commands, apply its injected instructions
3. **Declare** which skills you used by adding this tag at the END of your response:
   `<!-- skills: skill-name-1, skill-name-2 -->`
   If no skill was relevant, write: `<!-- skills: none -->`
---
"""

    # Parse used skills from model responses.
    def parse_used_skills(self, response_text: str) -> list[Skill]:
        """
        Find the <!-- skills: ... --> response tag and return matching skills.
        """
        m = _SKILL_TAG_RE.search(response_text)
        if not m:
            return []
        names = [n.strip().lstrip("/").strip("` ") for n in m.group(1).split(",")]
        found = []
        for name in names:
            if name.lower() == "none":
                continue
            skill = self._skills.get(name)
            if skill:
                found.append(skill)
        return found

    def strip_skill_tag(self, response_text: str) -> str:
        """Remove the <!-- skills: ... --> tag before rendering the response."""
        return _SKILL_TAG_RE.sub("", response_text).rstrip()


_instance: "SkillsManager | None" = None


_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "code", "for", "from", "in", "is", "it",
    "of", "on", "or", "the", "this", "to", "tool", "use", "using", "with", "you", "your",
}


def _meaningful_words(text: str) -> list[str]:
    words = re.findall(r"[^\W_]{2,}", str(text or "").lower(), flags=re.UNICODE)
    return [word for word in words if word not in _STOP_WORDS]


def get_skills_manager() -> SkillsManager:
    global _instance
    if _instance is None:
        _instance = SkillsManager()
    return _instance
