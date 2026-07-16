"""Central persistent memory archive for projects and chat sessions."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def default_storage_root() -> Path:
    configured = os.getenv("CODERAI_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return app_dir / "coderai_data"


class MemoryManager:
    """Project-scoped facade over one application-level SQLite database."""

    def __init__(self, workspace_path: str | Path, storage_backend: str = "sqlite", storage_root: str | Path | None = None):
        if storage_backend != "sqlite":
            raise ValueError("Only the sqlite storage backend is currently supported")
        self.workspace_path = Path(workspace_path).resolve()
        self.storage_dir = Path(storage_root).resolve() if storage_root else default_storage_root()
        self.db_path = self.storage_dir / "memory.db"
        self.vectors_dir = self.storage_dir / "vectors"
        self.audit_path = self.storage_dir / "facts.jsonl"
        self._write_lock = threading.RLock()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.vectors_dir.mkdir(parents=True, exist_ok=True)
        self.vector_store = "sqlite-fts5"
        self._init_schema()
        self.project_id = self._register_project()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_path TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_opened_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    title TEXT NOT NULL DEFAULT 'New session',
                    summary TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_calls TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, id);
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    fact TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    embedding TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(project_id, fact, source)
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(project_id, key)
                );
                """
            )
            try:
                db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(fact, source, fact_id UNINDEXED, project_id UNINDEXED)"
                )
            except sqlite3.OperationalError:
                self.vector_store = "sqlite-like-search"

    def _register_project(self) -> int:
        now = time.time()
        workspace = str(self.workspace_path)
        with self._write_lock, self._connect() as db:
            db.execute(
                "INSERT INTO projects(workspace_path, name, created_at, last_opened_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(workspace_path) DO UPDATE SET name=excluded.name, last_opened_at=excluded.last_opened_at",
                (workspace, self.workspace_path.name or workspace, now, now),
            )
            return int(db.execute("SELECT id FROM projects WHERE workspace_path=?", (workspace,)).fetchone()[0])

    # --- Project and session archive -------------------------------------

    def ensure_session(self, session_id: str, title: str = "New session") -> None:
        now = time.time()
        with self._write_lock, self._connect() as db:
            db.execute(
                "INSERT INTO sessions(id, project_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
                (session_id, self.project_id, title or "New session", now, now),
            )

    def list_projects(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT p.id, p.name, p.workspace_path, p.created_at, p.last_opened_at, "
                "count(DISTINCT s.id) AS sessions, count(DISTINCT f.id) AS facts, max(s.updated_at) AS last_session_at "
                "FROM projects p LEFT JOIN sessions s ON s.project_id=p.id LEFT JOIN facts f ON f.project_id=p.id "
                "GROUP BY p.id ORDER BY coalesce(max(s.updated_at), p.last_opened_at) DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_project(self, project_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, name, workspace_path, created_at, last_opened_at FROM projects WHERE id=?",
                (int(project_id),),
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, project_id: int | None = None, limit: int = 100) -> list[dict]:
        selected_project = int(project_id or self.project_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT s.id, s.project_id, s.title, s.summary, s.created_at, s.updated_at, count(t.id) AS turns "
                "FROM sessions s LEFT JOIN turns t ON t.session_id=s.id "
                "WHERE s.project_id=? GROUP BY s.id ORDER BY s.updated_at DESC LIMIT ?",
                (selected_project, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_session(self, session_id: str) -> dict | None:
        with self._connect() as db:
            session = db.execute(
                "SELECT s.*, p.name AS project_name, p.workspace_path FROM sessions s "
                "JOIN projects p ON p.id=s.project_id WHERE s.id=?", (session_id,)
            ).fetchone()
            if not session:
                return None
            turns = db.execute(
                "SELECT role, content, tool_calls, created_at FROM turns WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
        payload = dict(session)
        payload["turns"] = [self._turn_dict(row) for row in turns]
        return payload

    def rename_session(self, session_id: str, title: str) -> None:
        title = " ".join(str(title or "").split()).strip()[:120]
        if not title:
            raise ValueError("Session title cannot be empty")
        with self._write_lock, self._connect() as db:
            db.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?", (title, time.time(), session_id))

    # --- Episodic memory -------------------------------------------------

    def save_turn(self, session_id: str, role: str, content: str, tool_calls: list | None = None) -> int:
        self.ensure_session(session_id, self._title_from_content(content) if role == "user" else "New session")
        now = time.time()
        with self._write_lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO turns(project_id, session_id, role, content, tool_calls, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (self.project_id, session_id, role, content, json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None, now),
            )
            if role == "user":
                turn_count = int(db.execute("SELECT count(*) FROM turns WHERE session_id=?", (session_id,)).fetchone()[0])
                if turn_count == 1:
                    db.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?", (self._title_from_content(content), now, session_id))
                else:
                    db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            else:
                db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            return int(cursor.lastrowid)

    def load_recent_turns(self, session_id: str, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT role, content, tool_calls, created_at FROM turns WHERE session_id=? AND project_id=? "
                "ORDER BY id DESC LIMIT ?", (session_id, self.project_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._turn_dict(row) for row in reversed(rows)]

    def summarize_old_session(self, session_id: str) -> str:
        turns = self.load_recent_turns(session_id, 200)
        if not turns:
            return ""
        lines = []
        for turn in turns[-40:]:
            compact = " ".join(turn["content"].split())
            lines.append(f"- {turn['role']}: {compact[:497] + '...' if len(compact) > 500 else compact}")
        summary = "\n".join(lines)
        with self._write_lock, self._connect() as db:
            db.execute("UPDATE sessions SET summary=?, updated_at=? WHERE id=?", (summary, time.time(), session_id))
        return summary

    def load_recent_summaries(self, exclude_session_id: str = "", limit: int = 3) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id AS session_id, summary, updated_at FROM sessions "
                "WHERE project_id=? AND id != ? AND summary != '' ORDER BY updated_at DESC LIMIT ?",
                (self.project_id, exclude_session_id, max(1, min(int(limit), 20))),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Semantic memory -------------------------------------------------

    def index_fact(self, fact: str, source: str = "", embedding: list[float] | None = None) -> int:
        fact = " ".join(str(fact or "").split()).strip()
        source = str(source or "").strip()
        if not fact:
            raise ValueError("Fact cannot be empty")
        now = time.time()
        with self._write_lock, self._connect() as db:
            db.execute(
                "INSERT INTO facts(project_id, fact, source, embedding, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id, fact, source) DO UPDATE SET embedding=excluded.embedding, updated_at=excluded.updated_at",
                (self.project_id, fact, source, json.dumps(embedding) if embedding else None, now, now),
            )
            fact_id = int(db.execute(
                "SELECT id FROM facts WHERE project_id=? AND fact=? AND source=?", (self.project_id, fact, source)
            ).fetchone()[0])
            if self.vector_store == "sqlite-fts5":
                db.execute("DELETE FROM facts_fts WHERE fact_id=?", (fact_id,))
                db.execute(
                    "INSERT INTO facts_fts(fact, source, fact_id, project_id) VALUES (?, ?, ?, ?)",
                    (fact, source, fact_id, self.project_id),
                )
        self._audit("index", {"fact_id": fact_id, "fact": fact, "source": source})
        return fact_id

    def retrieve_relevant(self, query: str, top_k: int = 5) -> list[dict]:
        top_k = max(1, min(int(top_k), 20))
        terms = [term for term in self._search_terms(query) if len(term) > 1]
        with self._connect() as db:
            if self.vector_store == "sqlite-fts5" and terms:
                match = " OR ".join(f'"{term}"' for term in terms[:12])
                try:
                    rows = db.execute(
                        "SELECT f.id, f.fact, f.source, f.created_at, bm25(facts_fts) AS score FROM facts_fts "
                        "JOIN facts f ON f.id=CAST(facts_fts.fact_id AS INTEGER) "
                        "WHERE facts_fts MATCH ? AND facts_fts.project_id=? ORDER BY score LIMIT ?",
                        (match, str(self.project_id), top_k),
                    ).fetchall()
                    if rows:
                        return [dict(row) for row in rows]
                except sqlite3.OperationalError:
                    pass
            if terms:
                clauses = " OR ".join("lower(fact) LIKE ? OR lower(source) LIKE ?" for _ in terms[:8])
                params = [value for term in terms[:8] for value in (f"%{term.lower()}%", f"%{term.lower()}%")]
                rows = db.execute(
                    f"SELECT id, fact, source, created_at, 0 AS score FROM facts WHERE project_id=? AND ({clauses}) "
                    "ORDER BY updated_at DESC LIMIT ?", (self.project_id, *params, top_k),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id, fact, source, created_at, 0 AS score FROM facts WHERE project_id=? ORDER BY updated_at DESC LIMIT ?",
                    (self.project_id, top_k),
                ).fetchall()
        return [dict(row) for row in rows]

    def list_facts(self, limit: int = 200, project_id: int | None = None) -> list[dict]:
        selected_project = int(project_id or self.project_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, fact, source, created_at, updated_at FROM facts WHERE project_id=? ORDER BY updated_at DESC LIMIT ?",
                (selected_project, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_fact(self, fact_id: int, fact: str, source: str = "") -> None:
        with self._write_lock, self._connect() as db:
            if self.vector_store == "sqlite-fts5":
                db.execute("DELETE FROM facts_fts WHERE fact_id=?", (int(fact_id),))
            db.execute("DELETE FROM facts WHERE id=? AND project_id=?", (int(fact_id), self.project_id))
        self.index_fact(fact, source)
        self._audit("update", {"fact_id": int(fact_id), "fact": fact, "source": source})

    def delete_fact(self, fact_id: int) -> None:
        with self._write_lock, self._connect() as db:
            if self.vector_store == "sqlite-fts5":
                db.execute("DELETE FROM facts_fts WHERE fact_id=?", (int(fact_id),))
            db.execute("DELETE FROM facts WHERE id=? AND project_id=?", (int(fact_id), self.project_id))
        self._audit("delete", {"fact_id": int(fact_id)})

    # --- Procedural memory -----------------------------------------------

    def get_user_preferences(self, project_id: int | None = None) -> dict:
        selected_project = int(project_id or self.project_id)
        with self._connect() as db:
            rows = db.execute("SELECT key, value FROM preferences WHERE project_id=? ORDER BY key", (selected_project,)).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def update_preference(self, key: str, value: str) -> None:
        key, value = str(key or "").strip(), str(value or "").strip()
        if not key or not value:
            raise ValueError("Preference key and value are required")
        with self._write_lock, self._connect() as db:
            db.execute(
                "INSERT INTO preferences(project_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (self.project_id, key, value, time.time()),
            )

    def delete_preference(self, key: str) -> None:
        with self._write_lock, self._connect() as db:
            db.execute("DELETE FROM preferences WHERE project_id=? AND key=?", (self.project_id, str(key)))

    def stats(self) -> dict:
        with self._connect() as db:
            turns = int(db.execute("SELECT count(*) FROM turns WHERE project_id=?", (self.project_id,)).fetchone()[0])
            facts = int(db.execute("SELECT count(*) FROM facts WHERE project_id=?", (self.project_id,)).fetchone()[0])
            preferences = int(db.execute("SELECT count(*) FROM preferences WHERE project_id=?", (self.project_id,)).fetchone()[0])
            sessions = int(db.execute("SELECT count(*) FROM sessions WHERE project_id=?", (self.project_id,)).fetchone()[0])
        return {
            "turns": turns, "facts": facts, "preferences": preferences, "sessions": sessions,
            "backend": self.vector_store, "database": str(self.db_path), "project_id": self.project_id,
        }

    def forget_project(self) -> None:
        with self._write_lock, self._connect() as db:
            if self.vector_store == "sqlite-fts5":
                db.execute("DELETE FROM facts_fts WHERE project_id=?", (str(self.project_id),))
            db.execute("DELETE FROM projects WHERE id=?", (self.project_id,))
        self._audit("forget_project", {})

    @staticmethod
    def _turn_dict(row: sqlite3.Row) -> dict:
        return {
            "role": row["role"], "content": row["content"],
            "tool_calls": json.loads(row["tool_calls"]) if row["tool_calls"] else None,
            "created_at": row["created_at"],
        }

    @staticmethod
    def _search_terms(query: str) -> list[str]:
        cleaned = "".join(char if char.isalnum() or char in "_-" else " " for char in str(query or ""))
        seen = set()
        return [term for term in cleaned.split() if not (term.lower() in seen or seen.add(term.lower()))]

    @staticmethod
    def _title_from_content(content: str) -> str:
        title = " ".join(str(content or "").split()).strip()
        return (title[:77] + "...") if len(title) > 80 else (title or "New session")

    def _audit(self, action: str, payload: dict) -> None:
        record = {"timestamp": time.time(), "action": action, "project_id": self.project_id, **payload}
        with self._write_lock, self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
