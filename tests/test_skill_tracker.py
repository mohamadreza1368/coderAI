from pathlib import Path

from skill_tracker import SkillTracker
from skills_manager import Skill, SkillsManager


def test_relevant_skill_selection_returns_reason(monkeypatch, tmp_path: Path):
    manager = SkillsManager()
    pdf_skill = Skill("pdf-writer", "Create PDF reports and documents", "instructions", "documents", tmp_path / "SKILL.md")
    monkeypatch.setattr(manager, "_skills", {pdf_skill.name: pdf_skill})

    selections = manager.select_relevant_skills("Create a PDF report")

    assert [item.skill.name for item in selections] == ["pdf-writer"]
    assert "pdf" in selections[0].matched_keywords
    assert selections[0].triggered_by == "keyword_match"


def test_slash_and_pinned_selection_are_distinct(monkeypatch, tmp_path: Path):
    manager = SkillsManager()
    first = Skill("tdd", "Test driven development", "tdd", "engineering", tmp_path / "tdd.md")
    second = Skill("review", "Review source changes", "review", "engineering", tmp_path / "review.md")
    monkeypatch.setattr(manager, "_skills", {first.name: first, second.name: second})

    selections = manager.select_relevant_skills("/tdd implement feature", pinned=["review"])

    assert [(item.skill.name, item.triggered_by) for item in selections] == [("tdd", "slash_command"), ("review", "pinned")]


def test_tracker_separates_loaded_from_applied_and_reports_global_usage(tmp_path: Path):
    storage = tmp_path / "data"
    first = SkillTracker(tmp_path / "project-a", storage)
    second = SkillTracker(tmp_path / "project-b", storage)
    first.log_usage("s1", "pdf-writer", "keyword_match", ["pdf"], "selected", 1)
    first.log_usage("s1", "pdf-writer", "keyword_match", ["pdf"], "loaded", 1)

    before = first.report(["pdf-writer"])["skills"][0]
    assert before["project_uses"] == 0
    assert before["success_rate"] is None

    first.log_usage("s1", "pdf-writer", "keyword_match", ["pdf"], "applied", 1)
    second.log_usage("s2", "pdf-writer", "slash_command", ["/pdf-writer"], "failed", 1)
    after = first.report(["pdf-writer"])["skills"][0]

    assert after["project_uses"] == 1
    assert after["global_uses"] == 2
    assert after["success_rate"] == 100
    assert after["top_keywords"][0] == ("pdf", 1)


def test_disable_is_scoped_to_project(tmp_path: Path):
    storage = tmp_path / "data"
    first = SkillTracker(tmp_path / "project-a", storage)
    second = SkillTracker(tmp_path / "project-b", storage)

    first.set_disabled("tdd", True)

    assert first.disabled_skills() == {"tdd"}
    assert first.skill_modes()["tdd"] == "off"
    assert second.disabled_skills() == set()


def test_three_state_mode_is_persistent(tmp_path: Path):
    storage = tmp_path / "data"
    tracker = SkillTracker(tmp_path / "project", storage)

    tracker.set_mode("review", "pinned")

    reopened = SkillTracker(tmp_path / "project", storage)
    assert reopened.skill_modes()["review"] == "pinned"
    assert reopened.report(["review"])["skills"][0]["mode"] == "pinned"
