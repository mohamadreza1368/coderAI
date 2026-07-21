"""Incremental hybrid Codebase RAG index for a local workspace."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from vector_store import LocalEmbeddingProvider, SharedVectorStore, cosine_similarity
from workspace_filter import is_ignored_workspace_path, iter_workspace_files
from project_intelligence import (
    DependencyGraphBuilder, FileNode, HierarchicalSummarizer, ProjectGraph, QueryRouter, QueryType,
)


DEFAULT_EXCLUDE = [
    ".git/", ".agent_memory/", "node_modules/", "__pycache__/", ".huggingface/", "venv/", ".venv/",
    "dist/", "build/", ".next/", "*.pyc", "*.jpg", "*.jpeg", "*.png", "*.gif", "*.bin", "*.safetensors",
]
INDEXABLE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".cs", ".php", ".rb", ".sh", ".ps1", ".md", ".json", ".yaml", ".yml", ".txt", ".toml", ".html", ".css", ".xml"}
MAX_FILE_BYTES = int(os.getenv("CODE_INDEX_MAX_FILE_BYTES", str(2 * 1024 * 1024)))


@dataclass
class CodeChunk:
    id: str
    file_path: str
    symbol_name: str | None
    symbol_type: str
    start_line: int
    end_line: int
    content: str
    docstring: str | None
    embedding: list[float]
    last_modified: float
    content_hash: str


class CodeChunker:
    def chunk(self, file_path: str, content: str, modified: float) -> list[CodeChunk]:
        if Path(file_path).suffix.lower() == ".py":
            try:
                return self.chunk_python_file(file_path, content, modified)
            except SyntaxError:
                pass
        return self.chunk_generic_file(file_path, content, modified)

    def chunk_python_file(self, file_path: str, content: str, modified: float) -> list[CodeChunk]:
        tree = ast.parse(content)
        lines = content.splitlines()
        chunks = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            start, end = int(node.lineno), int(node.end_lineno or node.lineno)
            value = "\n".join(lines[start - 1:end])
            symbol_type = "class" if isinstance(node, ast.ClassDef) else "function"
            chunks.append(self._make(file_path, node.name, symbol_type, start, end, value, ast.get_docstring(node), modified))
        module_lines = []
        for index, line in enumerate(lines[:300], 1):
            if line.startswith(("import ", "from ", "__", "#")) or not line.strip():
                module_lines.append((index, line))
        if module_lines:
            chunks.insert(0, self._make(file_path, Path(file_path).stem, "module", module_lines[0][0], module_lines[-1][0], "\n".join(line for _, line in module_lines), ast.get_docstring(tree), modified))
        return chunks or self.chunk_generic_file(file_path, content, modified)

    def chunk_generic_file(self, file_path: str, content: str, modified: float, max_tokens: int = 300) -> list[CodeChunk]:
        lines = content.splitlines()
        chunks, current, start = [], [], 1
        max_chars = max_tokens * 4
        for line_no, line in enumerate(lines, 1):
            boundary = not line.strip() and current and sum(len(item) + 1 for item in current) >= max_chars // 2
            overflow = current and sum(len(item) + 1 for item in current) + len(line) > max_chars
            if boundary or overflow:
                chunks.append(self._make(file_path, None, "doc", start, line_no - 1, "\n".join(current).strip(), None, modified))
                current, start = [], line_no + (1 if boundary else 0)
            if line.strip() or current:
                current.append(line)
        if current:
            chunks.append(self._make(file_path, None, "doc", start, len(lines), "\n".join(current).strip(), None, modified))
        return [chunk for chunk in chunks if chunk.content]

    @staticmethod
    def _make(file_path, symbol_name, symbol_type, start, end, content, docstring, modified):
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return CodeChunk(hashlib.sha256(f"{file_path}:{start}:{end}:{digest}".encode()).hexdigest(), file_path, symbol_name, symbol_type, start, end, content, docstring, [], modified, digest)


class CodebaseIndex:
    def __init__(self, workspace_path: str | Path):
        self.workspace = Path(workspace_path).resolve()
        self.storage_dir = self.workspace / ".agent_memory"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.storage_dir / "code_index.db"
        self.vector_store = SharedVectorStore(self.storage_dir / "code_vectors")
        self.embedding_provider = LocalEmbeddingProvider()
        self.chunker = CodeChunker()
        self.graph_builder = DependencyGraphBuilder()
        self.summarizer = HierarchicalSummarizer()
        self.query_router = QueryRouter()
        self.collection = "code_chunks"
        self.embedding_error = ""
        self._embedding_disabled = False
        self._init_schema()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _init_schema(self):
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS indexed_files (
                    file_path TEXT PRIMARY KEY, content_hash TEXT NOT NULL, last_modified REAL NOT NULL, size INTEGER NOT NULL, indexed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS code_chunks (
                    id TEXT PRIMARY KEY, file_path TEXT NOT NULL, symbol_name TEXT, symbol_type TEXT NOT NULL,
                    start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, content TEXT NOT NULL, docstring TEXT,
                    embedding TEXT, last_modified REAL NOT NULL, content_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_code_chunks_file ON code_chunks(file_path);
                CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS file_nodes (
                    file_path TEXT PRIMARY KEY, imports TEXT NOT NULL, exported_symbols TEXT NOT NULL,
                    internal_calls TEXT NOT NULL, module_docstring TEXT NOT NULL, has_main_guard INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dependency_edges (
                    from_file TEXT NOT NULL, to_file TEXT NOT NULL, relation_type TEXT NOT NULL, detail TEXT NOT NULL,
                    PRIMARY KEY (from_file, to_file, relation_type, detail)
                );
                CREATE TABLE IF NOT EXISTS file_summaries (
                    file_path TEXT PRIMARY KEY, summary TEXT NOT NULL, content_hash TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS folder_summaries (
                    folder_path TEXT PRIMARY KEY, summary TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_summary (
                    id INTEGER PRIMARY KEY CHECK (id = 1), summary TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(chunk_id UNINDEXED, file_path, symbol_name, content, docstring);
                """
            )

    def discover_files(self, respect_gitignore: bool = True) -> list[Path]:
        patterns = list(DEFAULT_EXCLUDE)
        if respect_gitignore and (self.workspace / ".gitignore").is_file():
            patterns.extend(self._gitignore_patterns((self.workspace / ".gitignore").read_text(encoding="utf-8", errors="replace")))
        files = []
        for path in iter_workspace_files(self.workspace):
            if path.suffix.lower() not in INDEXABLE_EXTENSIONS:
                continue
            rel = path.relative_to(self.workspace).as_posix()
            if path.stat().st_size > MAX_FILE_BYTES or self._excluded(rel, patterns):
                continue
            files.append(path)
        return sorted(files)

    def rebuild(self) -> dict:
        files = self.discover_files()
        with self._connect() as db:
            old_ids = [row[0] for row in db.execute("SELECT id FROM code_chunks")]
            db.execute("DELETE FROM code_fts")
            db.execute("DELETE FROM code_chunks")
            db.execute("DELETE FROM indexed_files")
        self.vector_store.delete(self.collection, ids=old_ids)
        indexed, chunks = 0, 0
        for path in files:
            result = self.index_file(path, refresh_intelligence=False)
            indexed += int(result["indexed"])
            chunks += result["chunks"]
        self._set_meta("last_full_index", str(time.time()))
        self._refresh_project_intelligence()
        return {**self.status(check_freshness=True), "indexed_now": indexed, "chunks_now": chunks}

    def sync_incremental(self) -> dict:
        discovered = {path.relative_to(self.workspace).as_posix(): path for path in self.discover_files()}
        with self._connect() as db:
            existing = {row["file_path"]: row["content_hash"] for row in db.execute("SELECT file_path, content_hash FROM indexed_files")}
        removed = set(existing) - set(discovered)
        for rel in removed:
            self.remove_file(rel)
        updated = 0
        for rel, path in discovered.items():
            digest = self._file_hash(path)
            if existing.get(rel) != digest:
                updated += int(self.index_file(path, known_hash=digest, refresh_intelligence=False)["indexed"])
        self._refresh_project_intelligence()
        return {**self.status(check_freshness=True), "updated_files": updated, "removed_files": len(removed)}

    def index_file(self, file_path: str | Path, known_hash: str | None = None, refresh_intelligence: bool = True) -> dict:
        path = Path(file_path)
        path = path if path.is_absolute() else self.workspace / path
        if not path.exists() or path.suffix.lower() not in INDEXABLE_EXTENSIONS:
            return {"indexed": False, "chunks": 0}
        if is_ignored_workspace_path(self.workspace, path):
            return {"indexed": False, "chunks": 0}
        rel = path.resolve().relative_to(self.workspace).as_posix()
        if self._excluded(rel, DEFAULT_EXCLUDE) or path.stat().st_size > MAX_FILE_BYTES:
            self.remove_file(rel)
            return {"indexed": False, "chunks": 0}
        digest = known_hash or self._file_hash(path)
        with self._connect() as db:
            row = db.execute("SELECT content_hash FROM indexed_files WHERE file_path=?", (rel,)).fetchone()
        if row and row[0] == digest:
            return {"indexed": False, "chunks": 0}
        content = path.read_text(encoding="utf-8", errors="replace")
        chunks = self.chunker.chunk(rel, content, path.stat().st_mtime)
        self._embed_chunks(chunks)
        self.remove_file(rel)
        with self._connect() as db:
            for chunk in chunks:
                db.execute(
                    "INSERT INTO code_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (chunk.id, chunk.file_path, chunk.symbol_name, chunk.symbol_type, chunk.start_line, chunk.end_line,
                     chunk.content, chunk.docstring, json.dumps(chunk.embedding) if chunk.embedding else None, chunk.last_modified, chunk.content_hash),
                )
                db.execute("INSERT INTO code_fts VALUES (?, ?, ?, ?, ?)", (chunk.id, chunk.file_path, chunk.symbol_name or "", chunk.content, chunk.docstring or ""))
            db.execute("INSERT OR REPLACE INTO indexed_files VALUES (?, ?, ?, ?, ?)", (rel, digest, path.stat().st_mtime, path.stat().st_size, time.time()))
        embedded = [chunk for chunk in chunks if chunk.embedding]
        self.vector_store.upsert(
            self.collection, [chunk.id for chunk in embedded], [chunk.content for chunk in embedded],
            [{"file_path": chunk.file_path, "symbol_name": chunk.symbol_name or "", "start_line": chunk.start_line, "end_line": chunk.end_line} for chunk in embedded],
            [chunk.embedding for chunk in embedded],
        )
        if refresh_intelligence:
            self._refresh_project_intelligence({rel})
        return {"indexed": True, "chunks": len(chunks)}

    def remove_file(self, file_path: str) -> None:
        rel = Path(file_path).as_posix()
        with self._connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM code_chunks WHERE file_path=?", (rel,))]
            for chunk_id in ids:
                db.execute("DELETE FROM code_fts WHERE chunk_id=?", (chunk_id,))
            db.execute("DELETE FROM code_chunks WHERE file_path=?", (rel,))
            db.execute("DELETE FROM indexed_files WHERE file_path=?", (rel,))
        self.vector_store.delete(self.collection, ids=ids)
        with self._connect() as db:
            db.execute("DELETE FROM file_nodes WHERE file_path=?", (rel,))
            db.execute("DELETE FROM file_summaries WHERE file_path=?", (rel,))
            db.execute("DELETE FROM dependency_edges WHERE from_file=? OR to_file=?", (rel, rel))

    def retrieve_relevant_code(self, query: str, top_k: int = 5) -> list[dict]:
        limit = max(1, min(int(top_k), 20))
        keyword = self._fts_search(query, limit * 2)
        semantic = self._semantic_search(query, limit * 2)
        scores: dict[str, float] = {}
        for rank, item in enumerate(keyword):
            scores[item["id"]] = scores.get(item["id"], 0.0) + 1.0 / (60 + rank)
        for rank, item in enumerate(semantic):
            scores[item["id"]] = scores.get(item["id"], 0.0) + 1.0 / (60 + rank)
        ids = sorted(scores, key=scores.get, reverse=True)[:limit]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM code_chunks WHERE id IN ({placeholders})", ids).fetchall()
        by_id = {row["id"]: dict(row) for row in rows}
        result = []
        for chunk_id in ids:
            item = by_id[chunk_id]
            item.pop("embedding", None)
            item["score"] = scores[chunk_id]
            result.append(item)
        return result

    def status(self, check_freshness: bool = False) -> dict:
        with self._connect() as db:
            files = int(db.execute("SELECT count(*) FROM indexed_files").fetchone()[0])
            chunks = int(db.execute("SELECT count(*) FROM code_chunks").fetchone()[0])
            symbols = int(db.execute("SELECT count(*) FROM code_chunks WHERE symbol_name IS NOT NULL").fetchone()[0])
            last = db.execute("SELECT max(indexed_at) FROM indexed_files").fetchone()[0]
            graph_nodes = int(db.execute("SELECT count(*) FROM file_nodes").fetchone()[0])
            graph_edges = int(db.execute("SELECT count(*) FROM dependency_edges").fetchone()[0])
            summary_row = db.execute("SELECT summary, updated_at FROM project_summary WHERE id=1").fetchone()
        return {
            "files": files, "chunks": chunks, "symbols": symbols, "last_indexed": last,
            "up_to_date": self._is_up_to_date() if check_freshness else None, "database": str(self.db_path),
            "vector_backend": "chromadb" if self.vector_store.available else "sqlite-embedding-fallback",
            "vector_error": self.vector_store.error, "embedding_model": self.embedding_provider.model,
            "embedding_error": self.embedding_error,
            "graph_nodes": graph_nodes, "graph_edges": graph_edges,
            "project_summary": summary_row["summary"] if summary_row else "",
            "summary_updated_at": summary_row["updated_at"] if summary_row else None,
        }

    def get_graph(self) -> ProjectGraph:
        with self._connect() as db:
            node_rows = db.execute("SELECT * FROM file_nodes").fetchall()
            edges = [dict(row) for row in db.execute("SELECT * FROM dependency_edges ORDER BY from_file, to_file")]
        nodes = {
            row["file_path"]: FileNode(
                row["file_path"], json.loads(row["imports"]), json.loads(row["exported_symbols"]),
                json.loads(row["internal_calls"]), row["module_docstring"], bool(row["has_main_guard"]),
            ) for row in node_rows
        }
        return ProjectGraph(nodes, edges)

    def get_project_overview(self) -> dict:
        graph = self.get_graph()
        with self._connect() as db:
            summary = db.execute("SELECT summary, updated_at FROM project_summary WHERE id=1").fetchone()
            file_summaries = {row["file_path"]: row["summary"] for row in db.execute("SELECT file_path, summary FROM file_summaries")}
        entry_points = graph.get_entry_points()[:12]
        return {
            "summary": summary["summary"] if summary else "Codebase index has no project overview yet.",
            "updated_at": summary["updated_at"] if summary else None,
            "entry_points": entry_points,
            "key_files": [{"file_path": path, "summary": file_summaries.get(path, "")} for path in entry_points],
            "nodes": len(graph.nodes), "edges": len(graph.edges),
        }

    def get_related_files(self, file_path: str, depth: int = 1) -> list[dict]:
        graph = self.get_graph()
        related = graph.get_related_files(Path(file_path).as_posix(), depth)
        with self._connect() as db:
            summaries = {row["file_path"]: row["summary"] for row in db.execute("SELECT file_path, summary FROM file_summaries")}
        result = []
        for path in related:
            relations = [edge for edge in graph.edges if {edge["from_file"], edge["to_file"]} == {Path(file_path).as_posix(), path}]
            result.append({"file_path": path, "summary": summaries.get(path, ""), "relations": relations})
        return result

    def dependency_tree(self, limit: int = 80) -> list[dict]:
        graph = self.get_graph()
        entry_points = graph.get_entry_points()
        paths = sorted(
            graph.nodes,
            key=lambda path: (path not in entry_points, -sum(1 for edge in graph.edges if edge["from_file"] == path), path),
        )
        return [
            {"file_path": path, "related": [edge for edge in graph.edges if edge["from_file"] == path][:8]}
            for path in paths[:limit] if any(edge["from_file"] == path for edge in graph.edges)
        ]

    def retrieve_context(self, query: str, top_k: int = 5) -> dict:
        query_type = self.query_router.classify(query)
        if query_type == QueryType.PROJECT_LEVEL:
            return {"query_type": query_type.value, "overview": self.get_project_overview(), "chunks": [], "related_files": []}
        chunks = self.retrieve_relevant_code(query, top_k)
        related_files = []
        seen = {chunk["file_path"] for chunk in chunks}
        for file_path in list(seen)[:3]:
            for related in self.get_related_files(file_path, depth=1):
                if related["file_path"] not in seen:
                    related_files.append(related)
                    seen.add(related["file_path"])
        return {"query_type": query_type.value, "overview": None, "chunks": chunks, "related_files": related_files[:8]}

    def regenerate_summaries(self, model: str | None = None) -> dict:
        self._refresh_project_intelligence()
        overview = self.get_project_overview()
        if model and overview["nodes"]:
            prompt = (
                "Write a concise project architecture overview from this structural analysis. "
                "Explain the purpose, major modules, entry points, and how files collaborate. Do not invent details.\n\n"
                + overview["summary"]
            )
            improved = self.summarizer.improve_with_local_model(prompt, model)
            if improved:
                with self._connect() as db:
                    db.execute("INSERT OR REPLACE INTO project_summary VALUES (1, ?, ?)", (improved, time.time()))
        return self.get_project_overview()

    def _refresh_project_intelligence(self, changed_files: set[str] | None = None) -> None:
        with self._connect() as db:
            indexed = [dict(row) for row in db.execute("SELECT file_path, content_hash FROM indexed_files")]
            chunks_by_file: dict[str, list[dict]] = {}
            for row in db.execute("SELECT file_path, symbol_name, symbol_type, start_line, end_line FROM code_chunks"):
                chunks_by_file.setdefault(row["file_path"], []).append(dict(row))
            cached_hashes = {row["file_path"]: row["content_hash"] for row in db.execute("SELECT file_path, content_hash FROM file_summaries")}

        nodes: dict[str, FileNode] = {}
        for item in indexed:
            path = self.workspace / item["file_path"]
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace")
                nodes[item["file_path"]] = self.graph_builder.extract(item["file_path"], content)

        edges = self._resolve_dependency_edges(nodes)
        graph = ProjectGraph(nodes, edges)
        now = time.time()
        with self._connect() as db:
            db.execute("DELETE FROM file_nodes")
            db.execute("DELETE FROM dependency_edges")
            for node in nodes.values():
                db.execute(
                    "INSERT INTO file_nodes VALUES (?, ?, ?, ?, ?, ?)",
                    (node.file_path, json.dumps(node.imports), json.dumps(node.exported_symbols),
                     json.dumps(node.internal_calls), node.module_docstring, int(node.has_main_guard)),
                )
            for edge in edges:
                db.execute(
                    "INSERT OR IGNORE INTO dependency_edges VALUES (?, ?, ?, ?)",
                    (edge["from_file"], edge["to_file"], edge["relation_type"], edge["detail"]),
                )
            active_paths = set(nodes)
            for stale in set(cached_hashes) - active_paths:
                db.execute("DELETE FROM file_summaries WHERE file_path=?", (stale,))
            indexed_hashes = {item["file_path"]: item["content_hash"] for item in indexed}
            for path, node in nodes.items():
                if cached_hashes.get(path) == indexed_hashes[path] and (not changed_files or path not in changed_files):
                    continue
                summary = self.summarizer.summarize_file(node, chunks_by_file.get(path, []))
                db.execute("INSERT OR REPLACE INTO file_summaries VALUES (?, ?, ?, ?)", (path, summary, indexed_hashes[path], now))

            summaries = {row["file_path"]: row["summary"] for row in db.execute("SELECT file_path, summary FROM file_summaries")}
            folders: dict[str, dict[str, str]] = {}
            for path, summary in summaries.items():
                folders.setdefault(Path(path).parent.as_posix() if Path(path).parent.as_posix() != "." else "", {})[path] = summary
            db.execute("DELETE FROM folder_summaries")
            for folder, values in folders.items():
                db.execute("INSERT INTO folder_summaries VALUES (?, ?, ?)", (folder, self.summarizer.summarize_folder(folder, values), now))
            project_summary = self.summarizer.summarize_project(summaries, graph, self._readme_excerpt())
            db.execute("INSERT OR REPLACE INTO project_summary VALUES (1, ?, ?)", (project_summary, now))

    def _resolve_dependency_edges(self, nodes: dict[str, FileNode]) -> list[dict]:
        paths = set(nodes)
        symbol_owners: dict[str, set[str]] = {}
        for path, node in nodes.items():
            for symbol in node.exported_symbols:
                symbol_owners.setdefault(symbol, set()).add(path)
        edges: list[dict] = []
        imported_targets: dict[str, set[str]] = {}
        for source, node in nodes.items():
            for imported in node.imports:
                target = self._resolve_import(source, imported, paths)
                if target and target != source:
                    edges.append({"from_file": source, "to_file": target, "relation_type": "imports", "detail": imported})
                    imported_targets.setdefault(source, set()).add(target)
        for source, node in nodes.items():
            for call in node.internal_calls:
                owners = (symbol_owners.get(call, set()) - {source}) & imported_targets.get(source, set())
                if len(owners) == 1:
                    edges.append({"from_file": source, "to_file": next(iter(owners)), "relation_type": "calls", "detail": call})
        unique = {(edge["from_file"], edge["to_file"], edge["relation_type"], edge["detail"]): edge for edge in edges}
        return list(unique.values())

    @staticmethod
    def _resolve_import(source: str, imported: str, paths: set[str]) -> str | None:
        source_parent = Path(source).parent
        module = imported
        base = Path()
        if imported.startswith("."):
            level = len(imported) - len(imported.lstrip("."))
            module = imported[level:]
            base = source_parent
            for _ in range(max(0, level - 1)):
                base = base.parent
        module_path = Path(*[part for part in module.split(".") if part]) if module else Path()
        candidates = [base / module_path]
        if not imported.startswith("."):
            candidates.append(source_parent / module_path)
        for candidate in candidates:
            for suffix in (".py", ".js", ".ts", ".tsx", ".jsx"):
                value = candidate.as_posix() + suffix
                if value in paths:
                    return value
            init = (candidate / "__init__.py").as_posix()
            if init in paths:
                return init
            if candidate.as_posix() in paths:
                return candidate.as_posix()
        return None

    def _readme_excerpt(self) -> str:
        for name in ("README.md", "readme.md", "README.txt", "readme.txt"):
            path = self.workspace / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip() and not part.lstrip().startswith(("#", "```", "[!"))]
            if paragraphs:
                return paragraphs[0][:1800]
        return ""

    def _fts_search(self, query: str, limit: int) -> list[dict]:
        terms = re.findall(r"[A-Za-z_][\w.-]*|[^\W_]{2,}", query, flags=re.UNICODE)
        if not terms:
            return []
        expression = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:12])
        try:
            with self._connect() as db:
                rows = db.execute("SELECT chunk_id AS id, bm25(code_fts) AS rank FROM code_fts WHERE code_fts MATCH ? ORDER BY rank LIMIT ?", (expression, limit)).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            return []

    def _semantic_search(self, query: str, limit: int) -> list[dict]:
        try:
            vector = self.embedding_provider.embed(query)[0]
            self.embedding_error = ""
        except Exception as exc:
            self.embedding_error = str(exc)
            return []
        chroma = self.vector_store.query(self.collection, vector, limit)
        if chroma:
            return chroma
        with self._connect() as db:
            rows = db.execute("SELECT id, embedding FROM code_chunks WHERE embedding IS NOT NULL").fetchall()
        scored = [{"id": row["id"], "score": cosine_similarity(vector, json.loads(row["embedding"]))} for row in rows]
        return sorted(scored, key=lambda item: -item["score"])[:limit]

    def _embed_chunks(self, chunks: list[CodeChunk]) -> None:
        if not chunks or self._embedding_disabled:
            return
        try:
            vectors = self.embedding_provider.embed([self._embedding_text(chunk) for chunk in chunks])
            for chunk, vector in zip(chunks, vectors):
                chunk.embedding = vector
            self.embedding_error = ""
        except Exception as exc:
            self.embedding_error = str(exc)
            self._embedding_disabled = True

    def _is_up_to_date(self) -> bool:
        with self._connect() as db:
            indexed = {row["file_path"]: row["content_hash"] for row in db.execute("SELECT file_path, content_hash FROM indexed_files")}
        discovered = self.discover_files()
        if len(indexed) != len(discovered):
            return False
        return all(indexed.get(path.relative_to(self.workspace).as_posix()) == self._file_hash(path) for path in discovered)

    def _set_meta(self, key: str, value: str):
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO index_meta VALUES (?, ?)", (key, value))

    @staticmethod
    def _file_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _embedding_text(chunk: CodeChunk) -> str:
        return f"File: {chunk.file_path}\nSymbol: {chunk.symbol_name or ''} ({chunk.symbol_type})\nDocstring: {chunk.docstring or ''}\n{chunk.content}"

    @staticmethod
    def _gitignore_patterns(text: str) -> list[str]:
        return [line.strip().lstrip("/") for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", "!"))]

    @staticmethod
    def _excluded(rel: str, patterns: list[str]) -> bool:
        parts = rel.split("/")
        for pattern in patterns:
            clean = pattern.strip().lstrip("/")
            if clean.endswith("/") and (clean.rstrip("/") in parts or rel.startswith(clean)):
                return True
            if fnmatch.fnmatch(rel, clean) or fnmatch.fnmatch(Path(rel).name, clean) or fnmatch.fnmatch(rel, f"*/{clean}"):
                return True
        return False


class IncrementalIndexer:
    def __init__(self, index: CodebaseIndex):
        self.index = index
        self.observer = None

    def on_file_changed(self, file_path: str | Path) -> dict:
        path = Path(file_path)
        path = path if path.is_absolute() else self.index.workspace / path
        if path.exists():
            return self.index.index_file(path)
        self.index.remove_file(path.resolve().relative_to(self.index.workspace).as_posix())
        self.index._refresh_project_intelligence()
        return {"indexed": False, "removed": True, "chunks": 0}

    def start_watcher(self) -> bool:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception:
            return False
        owner = self
        class Handler(FileSystemEventHandler):
            def on_modified(self, event):
                if not event.is_directory:
                    owner.on_file_changed(event.src_path)
            on_created = on_modified
            def on_deleted(self, event):
                if not event.is_directory:
                    owner.on_file_changed(event.src_path)
        self.observer = Observer()
        self.observer.schedule(Handler(), str(self.index.workspace), recursive=True)
        self.observer.start()
        return True

    def stop_watcher(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=3)
            self.observer = None
