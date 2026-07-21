from pathlib import Path

from skill_router import SkillRouter, normalize_persian
from skills_manager import Skill


def make_skill(tmp_path: Path, name: str, description: str, triggers=None):
    return Skill(name, description, "instructions", "test", tmp_path / f"{name}.md", triggers or [])


def test_normalize_persian_handles_arabic_letters_spacing_and_digits():
    assert normalize_persian("گزارش\u200cهای كاربری ۱۲۳") == "گزارش های کاربری 123"


def test_fast_filter_uses_explicit_persian_triggers(tmp_path: Path):
    router = SkillRouter(tmp_path / "data")
    skill = make_skill(tmp_path, "pdf-writer", "Create documents", ["پی دی اف", "گزارش خروجی"])

    hits = router.fast_filter("لطفا یک گزارش\u200cخروجی بساز", [skill])

    assert hits[0][0].name == "pdf-writer"
    assert hits[0][1] == ["گزارش خروجی"]


def test_funnel_combines_modes_and_enforces_context_limit(monkeypatch, tmp_path: Path):
    router = SkillRouter(tmp_path / "data")
    skills = [make_skill(tmp_path, f"skill-{index}", f"topic {index}", [f"trigger{index}"]) for index in range(5)]
    monkeypatch.setattr(router, "semantic_rank", lambda prompt, values: ([(skill, 0.8 - index / 10) for index, skill in enumerate(values)], False))
    modes = {"skill-0": "pinned", "skill-4": "off"}

    result = router.route("trigger1 trigger2 trigger3 trigger4", skills, modes, max_active=3)

    assert result.selected[0].skill.name == "skill-0"
    assert [item.skill.name for item in result.selected[1:]] == ["skill-1", "skill-2"]
    assert "skill-3" in [item.skill.name for item in result.skipped]
    assert "skill-4" not in [item.skill.name for item in result.selected + result.skipped]


def test_llm_confirmation_runs_only_for_ambiguous_candidates(monkeypatch, tmp_path: Path):
    router = SkillRouter(tmp_path / "data")
    first = make_skill(tmp_path, "first", "Generate reports", ["report"])
    second = make_skill(tmp_path, "second", "Review reports", ["report"])
    monkeypatch.setattr(router, "semantic_rank", lambda prompt, values: ([(first, 0.8), (second, 0.75)], True))
    calls = []

    result = router.route("report", [first, second], {}, smart_confirmation=True, confirmer=lambda prompt, candidates: calls.append(candidates) or ["second"])

    assert result.confirmation_used is True
    assert [item.skill.name for item in result.selected] == ["second"]
    assert len(calls) == 1
