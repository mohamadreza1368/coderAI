"""Project dependency graph, hierarchical summaries, and query routing."""

from __future__ import annotations

import ast
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


@dataclass
class FileNode:
    file_path: str
    imports: list[str] = field(default_factory=list)
    exported_symbols: list[str] = field(default_factory=list)
    internal_calls: list[str] = field(default_factory=list)
    module_docstring: str = ""
    has_main_guard: bool = False


class DependencyGraphBuilder:
    _JS_IMPORT_RE = re.compile(
        r"(?:import\s+(?:[^;]+?\s+from\s+)?|require\s*\()\s*['\"]([^'\"]+)['\"]"
    )
    _JS_EXPORT_RE = re.compile(r"(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)")
    _GENERIC_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")

    def extract(self, file_path: str, content: str) -> FileNode:
        if Path(file_path).suffix.lower() == ".py":
            try:
                return self._extract_python(file_path, content)
            except SyntaxError:
                pass
        return self._extract_generic(file_path, content)

    def _extract_python(self, file_path: str, content: str) -> FileNode:
        tree = ast.parse(content)
        imports: list[str] = []
        calls: list[str] = []
        exports: list[str] = []
        has_main_guard = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = "." * int(node.level or 0)
                module = node.module or ""
                imports.append(prefix + module)
            elif isinstance(node, ast.Call):
                name = self._call_name(node.func)
                if name:
                    calls.append(name)
            elif isinstance(node, ast.If) and self._is_main_guard(node.test):
                has_main_guard = True
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                exports.append(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        exports.append(target.id)
        return FileNode(
            file_path=file_path,
            imports=self._unique(imports),
            exported_symbols=self._unique(exports),
            internal_calls=self._unique(calls),
            module_docstring=ast.get_docstring(tree) or "",
            has_main_guard=has_main_guard,
        )

    def _extract_generic(self, file_path: str, content: str) -> FileNode:
        if Path(file_path).suffix.lower() not in {".js", ".jsx", ".ts", ".tsx"}:
            return FileNode(file_path=file_path)
        return FileNode(
            file_path=file_path,
            imports=self._unique(self._JS_IMPORT_RE.findall(content)),
            exported_symbols=self._unique(self._JS_EXPORT_RE.findall(content)),
            internal_calls=self._unique(self._GENERIC_CALL_RE.findall(content)),
        )

    @staticmethod
    def _call_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    @staticmethod
    def _is_main_guard(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "__name__"
            and any(isinstance(value, ast.Constant) and value.value == "__main__" for value in node.comparators)
        )

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))


class ProjectGraph:
    SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".cs", ".php", ".rb", ".sh", ".ps1"}
    NON_RUNTIME_PARTS = {"test", "tests", "testing", "skills", "docs", "examples", "fixtures", "templates"}
    def __init__(self, nodes: dict[str, FileNode], edges: list[dict]):
        self.nodes = nodes
        self.edges = edges

    def get_related_files(self, file_path: str, depth: int = 1) -> list[str]:
        visited = {file_path}
        frontier = {file_path}
        for _ in range(max(1, depth)):
            next_frontier = set()
            for edge in self.edges:
                source, target = edge["from_file"], edge["to_file"]
                if source in frontier and target not in visited:
                    next_frontier.add(target)
                if target in frontier and source not in visited:
                    next_frontier.add(source)
            visited.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break
        visited.discard(file_path)
        return sorted(visited)

    def get_entry_points(self) -> list[str]:
        imported = {edge["to_file"] for edge in self.edges if edge["relation_type"] == "imports"}
        source_nodes = {path: node for path, node in self.nodes.items() if Path(path).suffix.lower() in self.SOURCE_EXTENSIONS}
        runtime_nodes = {
            path: node for path, node in source_nodes.items()
            if not any(part.lower() in self.NON_RUNTIME_PARTS for part in Path(path).parts)
            and ".template." not in Path(path).name.lower()
        }
        candidates = runtime_nodes or source_nodes or self.nodes
        roots = [path for path, node in candidates.items() if path not in imported or node.has_main_guard]
        preferred = ("main.py", "app.py", "server.py", "web_app.py", "cli.py", "index.js", "index.ts")
        return sorted(roots, key=lambda path: (Path(path).name not in preferred, not self.nodes[path].has_main_guard, path))


class HierarchicalSummarizer:
    def summarize_file(self, node: FileNode, chunks: list[dict]) -> str:
        if node.module_docstring:
            purpose = node.module_docstring.split("\n\n", 1)[0].strip()
        else:
            purpose = f"Defines {', '.join(node.exported_symbols[:8])}." if node.exported_symbols else "Contains project source or configuration."
        relations = []
        if node.imports:
            relations.append(f"imports {', '.join(node.imports[:6])}")
        if node.internal_calls:
            relations.append(f"calls {', '.join(node.internal_calls[:6])}")
        detail = f" It {' and '.join(relations)}." if relations else ""
        return f"{node.file_path}: {purpose}{detail} ({len(chunks)} indexed chunks)"

    def summarize_folder(self, folder: str, summaries: dict[str, str]) -> str:
        names = [Path(path).name for path in sorted(summaries)]
        label = folder or "project root"
        return f"{label} contains {len(names)} indexed files: {', '.join(names[:12])}."

    def summarize_project(self, file_summaries: dict[str, str], graph: ProjectGraph, readme_excerpt: str = "") -> str:
        entry_points = graph.get_entry_points()[:8]
        imports = sum(1 for edge in graph.edges if edge["relation_type"] == "imports")
        calls = sum(1 for edge in graph.edges if edge["relation_type"] == "calls")
        opening = readme_excerpt.strip() or f"This project contains {len(file_summaries)} indexed source and documentation files."
        details = [opening, f"The dependency graph contains {imports} import relations and {calls} resolved call relations."]
        if entry_points:
            details.append(f"Likely entry points: {', '.join(entry_points)}.")
        key = [file_summaries[path] for path in entry_points if path in file_summaries][:5]
        if key:
            details.append("Key files:\n" + "\n".join(f"- {item}" for item in key))
        return "\n\n".join(details)

    def improve_with_local_model(self, prompt: str, model: str, base_url: str | None = None, timeout: int = 120) -> str:
        url = (base_url or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/") + "/api/generate"
        body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return str(json.loads(response.read().decode("utf-8")).get("response") or "").strip()


class QueryType(str, Enum):
    PROJECT_LEVEL = "project_level"
    CODE_LEVEL = "code_level"


class QueryRouter:
    PROJECT_SIGNALS = (
        "این پروژه", "کل پروژه", "کلاً", "معماری", "ساختار پروژه", "چیکار می‌کنه", "چیکار میکنه",
        "پروژه رو", "پروژه را", "توضیح پروژه", "نمای کلی پروژه",
        "project overview", "whole project", "architecture", "codebase", "how does this project", "what does this project",
    )

    def classify(self, query: str) -> QueryType:
        normalized = " ".join(query.lower().split())
        return QueryType.PROJECT_LEVEL if any(signal in normalized for signal in self.PROJECT_SIGNALS) else QueryType.CODE_LEVEL
