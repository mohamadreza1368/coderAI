"""Persistent skill selection and usage telemetry."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from memory_manager import default_storage_root


class SkillTracker:
    STATUSES = {"selected", "loaded", "applied", "failed"}

    def __init__(self, workspace_path: str | Path, storage_root: str | Path | None = None):
        self.workspace_path = str(Path(workspace_path).resolve())
        self.storage_dir = Path(storage_root).resolve() if storage_root else default_storage_root()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.storage_dir / "skill_usage.db"
        self._init_schema()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS skill_usage (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    skill_name TEXT NOT NULL,
                    triggered_by TEXT NOT NULL,
                    matched_keywords TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    turn_index INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_skill_usage_project ON skill_usage(project_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_skill_usage_skill ON skill_usage(skill_name, timestamp DESC);
                CREATE TABLE IF NOT EXISTS project_skill_settings (
                    project_id TEXT NOT NULL,
                    skill_name TEXT NOT NULL,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'auto',
                    PRIMARY KEY(project_id, skill_name)
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(project_skill_settings)").fetchall()}
            if "mode" not in columns:
                db.execute("ALTER TABLE project_skill_settings ADD COLUMN mode TEXT NOT NULL DEFAULT 'auto'")
                db.execute("UPDATE project_skill_settings SET mode=CASE WHEN disabled=1 THEN 'off' ELSE 'auto' END")

    def log_usage(self, session_id: str, skill_name: str, triggered_by: str, matched_keywords: list[str], status: str, turn_index: int) -> dict:
        if status not in self.STATUSES:
            raise ValueError(f"Unknown skill usage status: {status}")
        event = {
            "id": uuid.uuid4().hex,
            "session_id": str(session_id),
            "project_id": self.workspace_path,
            "skill_name": str(skill_name),
            "triggered_by": str(triggered_by),
            "matched_keywords": list(matched_keywords or []),
            "status": status,
            "timestamp": time.time(),
            "turn_index": int(turn_index),
        }
        with self._connect() as db:
            db.execute(
                "INSERT INTO skill_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event["id"], event["session_id"], event["project_id"], event["skill_name"], event["triggered_by"],
                 json.dumps(event["matched_keywords"], ensure_ascii=False), event["status"], event["timestamp"], event["turn_index"]),
            )
        return event

    def disabled_skills(self) -> set[str]:
        return {name for name, mode in self.skill_modes().items() if mode == "off"}

    def skill_modes(self) -> dict[str, str]:
        with self._connect() as db:
            rows = db.execute("SELECT skill_name, mode, disabled FROM project_skill_settings WHERE project_id=?", (self.workspace_path,)).fetchall()
        return {row["skill_name"]: ("off" if row["disabled"] else row["mode"] or "auto") for row in rows}

    def set_mode(self, skill_name: str, mode: str) -> None:
        if mode not in {"auto", "pinned", "off"}:
            raise ValueError("Skill mode must be auto, pinned, or off")
        with self._connect() as db:
            db.execute(
                "INSERT INTO project_skill_settings(project_id, skill_name, disabled, mode) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id, skill_name) DO UPDATE SET disabled=excluded.disabled, mode=excluded.mode",
                (self.workspace_path, str(skill_name), int(mode == "off"), mode),
            )

    def set_disabled(self, skill_name: str, disabled: bool) -> None:
        self.set_mode(skill_name, "off" if disabled else "auto")

    def recent_usage(self, limit: int = 50) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM skill_usage WHERE project_id=? ORDER BY timestamp DESC LIMIT ?",
                (self.workspace_path, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._event(row) for row in rows]

    def report(self, skill_names: list[str]) -> dict:
        disabled = self.disabled_skills()
        modes = self.skill_modes()
        with self._connect() as db:
            project_rows = db.execute(
                "SELECT skill_name, count(*) AS uses, max(timestamp) AS last_used, "
                "sum(status='applied') AS applied, sum(status='failed') AS failed "
                "FROM skill_usage WHERE project_id=? AND status IN ('applied','failed') GROUP BY skill_name",
                (self.workspace_path,),
            ).fetchall()
            global_rows = db.execute(
                "SELECT skill_name, count(*) AS uses FROM skill_usage WHERE status IN ('applied','failed') GROUP BY skill_name"
            ).fetchall()
            keyword_rows = db.execute(
                "SELECT skill_name, matched_keywords FROM skill_usage WHERE project_id=? AND status='selected'",
                (self.workspace_path,),
            ).fetchall()
        project = {row["skill_name"]: dict(row) for row in project_rows}
        global_uses = {row["skill_name"]: int(row["uses"]) for row in global_rows}
        keywords: dict[str, dict[str, int]] = {}
        for row in keyword_rows:
            bucket = keywords.setdefault(row["skill_name"], {})
            for word in json.loads(row["matched_keywords"] or "[]"):
                bucket[word] = bucket.get(word, 0) + 1
        skills = []
        for name in skill_names:
            stats = project.get(name, {})
            applied, failed = int(stats.get("applied") or 0), int(stats.get("failed") or 0)
            outcomes = applied + failed
            skills.append({
                "name": name, "project_uses": int(stats.get("uses") or 0), "global_uses": global_uses.get(name, 0),
                "last_used": stats.get("last_used"), "success_rate": round(applied * 100 / outcomes) if outcomes else None,
                "applied": applied, "failed": failed, "disabled": name in disabled, "mode": modes.get(name, "auto"),
                "top_keywords": sorted(keywords.get(name, {}).items(), key=lambda item: (-item[1], item[0]))[:5],
            })
        return {"skills": skills, "recent": self.recent_usage(), "database": str(self.db_path)}

    @staticmethod
    def _event(row: sqlite3.Row) -> dict:
        event = dict(row)
        event["matched_keywords"] = json.loads(event.get("matched_keywords") or "[]")
        return event
