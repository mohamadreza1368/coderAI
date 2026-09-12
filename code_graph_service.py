"""
code_graph_service.py - Code Review Graph (CRG) integration service.

Provides local-first AST code intelligence, blast-radius impact analysis,
hierarchical community architecture overviews, and token-optimized subgraphs
for large-scale projects and monorepos.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("code_graph_service")

try:
    import code_review_graph
    from code_review_graph.tools.build import build_or_update_graph
    from code_review_graph.tools.community_tools import get_architecture_overview_func
    from code_review_graph.tools.context import get_minimal_context
    from code_review_graph.tools.query import (
        get_impact_radius,
        list_graph_stats,
        query_graph,
    )
    CRG_AVAILABLE = True
except Exception as _crg_err:
    CRG_AVAILABLE = False
    build_or_update_graph = None  # type: ignore
    get_architecture_overview_func = None  # type: ignore
    get_minimal_context = None  # type: ignore
    get_impact_radius = None  # type: ignore
    list_graph_stats = None  # type: ignore
    query_graph = None  # type: ignore
    logger.warning("code-review-graph not available: %s", _crg_err)

COMMUNITY_COLORS = [
    "#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
    "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#BAB0AC",
]


class CodeGraphService:
    """Manages AST knowledge graph indexing, queries, and blast-radius analysis."""

    def __init__(self) -> None:
        self._available = CRG_AVAILABLE

    @property
    def is_available(self) -> bool:
        return self._available

    def _resolve_repo(self, workspace_path: str | Path | None) -> str:
        if workspace_path is None:
            p = Path.cwd().resolve()
        else:
            p = Path(workspace_path).resolve()
        # Ensure project root marker exists so CRG accepts non-git projects gracefully
        crg_dir = p / ".code-review-graph"
        if not (p / ".git").exists() and not (p / ".svn").exists() and not crg_dir.exists():
            try:
                crg_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        return str(p)

    def build_or_update(
        self,
        workspace_path: str | Path | None = None,
        full_rebuild: bool = False,
    ) -> dict[str, Any]:
        """Build or incrementally update the AST code graph for the workspace."""
        if not self._available:
            return {
                "ok": False,
                "available": False,
                "error": "code-review-graph engine is not installed or unavailable.",
            }
        repo = self._resolve_repo(workspace_path)
        try:
            res = build_or_update_graph(
                full_rebuild=full_rebuild,
                repo_root=repo,
                postprocess="standard",
            )
            return {
                "ok": True,
                "available": True,
                "repo": repo,
                "data": res,
            }
        except Exception as exc:
            logger.exception("build_or_update_graph failed for %s", repo)
            return {
                "ok": False,
                "available": True,
                "repo": repo,
                "error": str(exc),
            }

    def get_impact_radius(
        self,
        workspace_path: str | Path | None = None,
        changed_files: list[str] | None = None,
        max_depth: int = 2,
    ) -> dict[str, Any]:
        """Calculate the blast radius (affected callers, dependents, and tests)."""
        if not self._available:
            return {
                "ok": False,
                "available": False,
                "error": "code-review-graph engine is not installed or unavailable.",
            }
        repo = self._resolve_repo(workspace_path)
        try:
            res = get_impact_radius(
                changed_files=changed_files,
                max_depth=max_depth,
                repo_root=repo,
                detail_level="standard",
            )
            return {
                "ok": True,
                "available": True,
                "repo": repo,
                "data": res,
            }
        except Exception as exc:
            logger.exception("get_impact_radius failed for %s", repo)
            return {
                "ok": False,
                "available": True,
                "repo": repo,
                "error": str(exc),
            }

    def get_architecture_overview(
        self,
        workspace_path: str | Path | None = None,
        detail_level: str = "minimal",
    ) -> dict[str, Any]:
        """Get high-level module architecture and community clusters without token waste."""
        if not self._available:
            return {
                "ok": False,
                "available": False,
                "error": "code-review-graph engine is not installed or unavailable.",
            }
        repo = self._resolve_repo(workspace_path)
        try:
            res = get_architecture_overview_func(
                repo_root=repo,
                detail_level=detail_level,
                max_results=50,
                max_members=8,
            )
            return {
                "ok": True,
                "available": True,
                "repo": repo,
                "data": res,
            }
        except Exception as exc:
            logger.exception("get_architecture_overview failed for %s", repo)
            return {
                "ok": False,
                "available": True,
                "repo": repo,
                "error": str(exc),
            }

    def get_minimal_context(
        self,
        workspace_path: str | Path | None = None,
        task: str = "",
        changed_files: list[str] | None = None,
    ) -> dict[str, Any]:
        """Extract a token-optimized subgraph context slice for a task or change."""
        if not self._available:
            return {
                "ok": False,
                "available": False,
                "error": "code-review-graph engine is not installed or unavailable.",
            }
        repo = self._resolve_repo(workspace_path)
        try:
            res = get_minimal_context(
                task=task,
                changed_files=changed_files,
                repo_root=repo,
            )
            return {
                "ok": True,
                "available": True,
                "repo": repo,
                "data": res,
            }
        except Exception as exc:
            logger.exception("get_minimal_context failed for %s", repo)
            return {
                "ok": False,
                "available": True,
                "repo": repo,
                "error": str(exc),
            }

    def query_graph(
        self,
        pattern: str,
        target: str,
        workspace_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Query graph edges and nodes (e.g. calls, callers, imports, extends)."""
        if not self._available:
            return {
                "ok": False,
                "available": False,
                "error": "code-review-graph engine is not installed or unavailable.",
            }
        repo = self._resolve_repo(workspace_path)
        try:
            res = query_graph(
                pattern=pattern,
                target=target,
                repo_root=repo,
                detail_level="standard",
                max_results=100,
            )
            return {
                "ok": True,
                "available": True,
                "repo": repo,
                "data": res,
            }
        except Exception as exc:
            logger.exception("query_graph failed for %s", repo)
            return {
                "ok": False,
                "available": True,
                "repo": repo,
                "error": str(exc),
            }

    def get_stats(self, workspace_path: str | Path | None = None) -> dict[str, Any]:
        """Get graph statistics (node counts, edge counts, density)."""
        if not self._available:
            return {
                "ok": False,
                "available": False,
                "error": "code-review-graph engine is not installed or unavailable.",
            }
        repo = self._resolve_repo(workspace_path)
        try:
            res = list_graph_stats(repo_root=repo)
            return {
                "ok": True,
                "available": True,
                "repo": repo,
                "data": res,
            }
        except Exception as exc:
            logger.exception("list_graph_stats failed for %s", repo)
            return {
                "ok": False,
                "available": True,
                "repo": repo,
                "error": str(exc),
            }

    def get_schematic_graph(
        self,
        workspace_path: str | Path | None = None,
        max_nodes: int = 180,
    ) -> dict[str, Any]:
        """Build an interactive UI schematic graph (nodes & edges) using Tree-sitter AST & SQLite knowledge graph."""
        if not self._available:
            return {"nodes": [], "edges": [], "source": "unavailable"}

        repo_str = self._resolve_repo(workspace_path)
        repo = Path(repo_str)

        try:
            from code_review_graph.tools._common import _get_store
            store, _ = _get_store(repo_str)
        except Exception as exc:
            logger.debug("Failed to get CRG store for %s: %s", repo_str, exc)
            return {"nodes": [], "edges": [], "source": "unavailable"}

        try:
            all_files = store.get_all_files()
            if not all_files:
                self.build_or_update(repo_str)
                all_files = store.get_all_files()

            if not all_files:
                return {"nodes": [], "edges": [], "source": "empty"}

            all_nodes = store.get_all_nodes()
            all_edges = store.get_all_edges()

            entry_names = {
                "main.py", "app.py", "web_app.py", "fastapi_app.py", "launcher.py",
                "index.js", "server.js", "index.ts", "server.ts", "main.go", "main.rs"
            }
            nodes: list[dict[str, Any]] = []
            edges: list[dict[str, Any]] = []
            folder_nodes: dict[str, dict[str, Any]] = {}
            node_ids: set[str] = set()
            file_symbol_counts: dict[str, int] = {}

            for n in all_nodes:
                if n.file_path:
                    file_symbol_counts[n.file_path] = file_symbol_counts.get(n.file_path, 0) + 1

            # 1. File nodes & folder containment
            for fpath in all_files:
                p = Path(fpath)
                try:
                    rel = p.relative_to(repo).as_posix()
                except Exception:
                    rel = p.as_posix()

                is_entry = p.name.lower() in entry_names or rel in entry_names
                sym_count = file_symbol_counts.get(fpath, 0)

                nodes.append({
                    "id": rel,
                    "label": p.name,
                    "full_path": rel,
                    "type": "file",
                    "shape": "dot",
                    "is_entry": is_entry,
                    "symbols_count": sym_count,
                    "start_line": 1,
                    "end_line": 1,
                })
                node_ids.add(rel)

                parent = Path(rel).parent
                while parent and parent.as_posix() != ".":
                    p_str = parent.as_posix()
                    f_id = f"folder::{p_str}"
                    if f_id not in folder_nodes:
                        folder_nodes[f_id] = {
                            "id": f_id,
                            "label": parent.name,
                            "full_path": p_str,
                            "type": "folder",
                            "start_line": 1,
                            "end_line": 1,
                        }
                        node_ids.add(f_id)
                    parent = parent.parent if parent.parent != parent and parent.parent.as_posix() != "." else None

                parent_dir = Path(rel).parent.as_posix()
                if parent_dir and parent_dir != ".":
                    edges.append({
                        "source": f"folder::{parent_dir}",
                        "target": rel,
                        "type": "contains",
                        "label": "contains",
                    })

            nodes.extend(folder_nodes.values())

            # 2. Key Classes & Functions
            sorted_nodes = sorted(
                all_nodes,
                key=lambda x: (0 if x.kind == "Class" else 1 if x.kind == "Function" else 2)
            )

            file_added_symbols: dict[str, int] = {}
            for n in sorted_nodes:
                if len(nodes) >= max_nodes:
                    break
                if n.kind not in ("Class", "Function") or not n.file_path:
                    continue

                p = Path(n.file_path)
                try:
                    rel_file = p.relative_to(repo).as_posix()
                except Exception:
                    rel_file = p.as_posix()

                if file_added_symbols.get(rel_file, 0) >= 6:
                    continue

                sym_id = f"{rel_file}::{n.name}"
                if sym_id in node_ids:
                    continue

                file_added_symbols[rel_file] = file_added_symbols.get(rel_file, 0) + 1
                nodes.append({
                    "id": sym_id,
                    "label": n.name,
                    "file_path": rel_file,
                    "type": "class" if n.kind == "Class" else "function",
                    "start_line": n.line_start or 1,
                    "end_line": n.line_end or 1,
                })
                node_ids.add(sym_id)

                edges.append({
                    "source": rel_file,
                    "target": sym_id,
                    "type": "defines",
                    "label": "defines",
                })

            # 3. Call and Import Edges
            seen_edges: set[tuple[str, str, str]] = set()
            for e in all_edges:
                src_file = e.file_path
                if not src_file:
                    continue
                try:
                    src_rel = Path(src_file).relative_to(repo).as_posix()
                except Exception:
                    src_rel = Path(src_file).as_posix()

                target_qn = e.target_qualified or ""
                target_name = Path(target_qn).name if target_qn else None

                target_id = None
                if target_qn in node_ids:
                    target_id = target_qn
                elif target_name and target_name in node_ids:
                    target_id = target_name

                if target_id and src_rel in node_ids and src_rel != target_id:
                    edge_type = "calls" if e.kind == "CALLS" else "inherits" if e.kind == "INHERITS" else "imports"
                    edge_key = (src_rel, target_id, edge_type)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append({
                            "source": src_rel,
                            "target": target_id,
                            "type": edge_type,
                            "label": edge_type,
                        })

            file_count = len(all_files)
            symbol_count = len([n for n in all_nodes if n.kind in ("Class", "Function", "Test")])
            return {
                "nodes": nodes,
                "edges": edges,
                "file_count": file_count,
                "symbol_count": symbol_count,
                "source": "code-review-graph",
                "stats": {
                    "total_files": file_count,
                    "total_nodes": len(all_nodes),
                    "total_edges": len(all_edges),
                },
            }
        except Exception as exc:
            logger.exception("Error building schematic graph from code-review-graph: %s", exc)
            return {"nodes": [], "edges": [], "file_count": 0, "symbol_count": 0, "source": "error", "error": str(exc)}
        finally:
            try:
                store.close()
            except Exception:
                pass

    def get_graphify_payload(
        self,
        workspace_path: str | Path | None = None,
        max_nodes: int = 250,
    ) -> dict[str, Any]:
        """
        Generate a Graphify-compliant (vis-network) payload with force-directed physics,
        Leiden community clustering, degree-based node sizing, and relationship edges.
        """
        if not self._available:
            return {"nodes": [], "edges": [], "legend": [], "stats": {}, "source": "unavailable"}

        repo_str = self._resolve_repo(workspace_path)
        repo = Path(repo_str)

        try:
            from code_review_graph.tools._common import _get_store
            store, _ = _get_store(repo_str)
        except Exception as exc:
            logger.debug("Failed to get CRG store for %s: %s", repo_str, exc)
            return {"nodes": [], "edges": [], "legend": [], "stats": {}, "source": "unavailable"}

        try:
            all_files = store.get_all_files()
            if not all_files:
                self.build_or_update(repo_str)
                all_files = store.get_all_files()

            if not all_files:
                return {"nodes": [], "edges": [], "legend": [], "stats": {}, "source": "empty"}

            all_nodes = store.get_all_nodes()
            all_edges = store.get_all_edges()

            # 1. Parse communities
            comm_list = []
            try:
                for c in store.get_communities_list():
                    c_dict = dict(c) if hasattr(c, "keys") else {}
                    if c_dict.get("id"):
                        comm_list.append(c_dict)
            except Exception:
                pass

            cid_to_name: dict[int, str] = {}
            cid_to_color: dict[int, str] = {}
            for i, c in enumerate(comm_list):
                cid = c["id"]
                cname = c.get("name") or f"Community {cid}"
                cid_to_name[cid] = cname
                cid_to_color[cid] = COMMUNITY_COLORS[i % len(COMMUNITY_COLORS)]

            default_cid = 0
            cid_to_name[0] = "Core"
            cid_to_color[0] = "#4E79A7"

            # 2. Compute degrees and file associations
            degree_map: dict[str, int] = {}
            file_symbol_counts: dict[str, int] = {}
            for n in all_nodes:
                if n.file_path:
                    file_symbol_counts[n.file_path] = file_symbol_counts.get(n.file_path, 0) + 1

            for e in all_edges:
                s = e.file_path or e.source_qualified or ""
                t = e.target_qualified or ""
                if s:
                    degree_map[s] = degree_map.get(s, 0) + 1
                if t:
                    degree_map[t] = degree_map.get(t, 0) + 1

            nodes: list[dict[str, Any]] = []
            edges: list[dict[str, Any]] = []
            node_ids: set[str] = set()
            community_member_counts: dict[int, int] = {}

            # 3. Add File Nodes
            entry_names = {
                "main.py", "app.py", "web_app.py", "fastapi_app.py", "launcher.py",
                "index.js", "server.js", "index.ts", "server.ts", "main.go", "main.rs"
            }

            for fpath in all_files:
                p = Path(fpath)
                try:
                    rel = p.relative_to(repo).as_posix()
                except Exception:
                    rel = p.as_posix()

                is_entry = p.name.lower() in entry_names or rel in entry_names
                deg = degree_map.get(fpath, 0) + degree_map.get(rel, 0)
                sym_count = file_symbol_counts.get(fpath, 0)

                # Assign community based on folder or hash
                parent_folder = Path(rel).parts[0] if len(Path(rel).parts) > 1 else "root"
                cid = abs(hash(parent_folder)) % max(1, len(comm_list) or 1) + 1 if comm_list else 0
                if cid not in cid_to_name:
                    cid_to_name[cid] = parent_folder.capitalize()
                    cid_to_color[cid] = COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)]

                color = cid_to_color.get(cid, "#4E79A7")
                community_member_counts[cid] = community_member_counts.get(cid, 0) + 1

                node_size = max(14, min(34, 14 + deg * 2 + (5 if is_entry else 0)))
                nodes.append({
                    "id": rel,
                    "label": f"File {p.name}",
                    "title": f"File: {rel}\nType: file\nSymbols: {sym_count}\nDegree: {deg}\nCommunity: {cid_to_name[cid]}",
                    "color": {
                        "background": color,
                        "border": "#f43f5e" if is_entry else "#ffffff",
                        "highlight": {"background": color, "border": "#38bdf8"},
                    },
                    "size": node_size,
                    "font": {"color": "#e0e0e0", "size": 13, "face": "Segoe UI, sans-serif"},
                    "community": cid,
                    "community_name": cid_to_name[cid],
                    "source_file": rel,
                    "file_type": "file",
                    "shape": "dot",
                    "degree": deg,
                    "is_entry": is_entry,
                    "start_line": 1,
                    "end_line": 1,
                })
                node_ids.add(rel)

            # 4. Add Class and Function Nodes (prioritized by degree)
            sorted_nodes = sorted(
                all_nodes,
                key=lambda x: (
                    0 if x.kind == "Class" else 1,
                    -(degree_map.get(x.qualified_name or "", 0)),
                )
            )

            file_added_symbols: dict[str, int] = {}
            for n in sorted_nodes:
                if len(nodes) >= max_nodes:
                    break
                if n.kind not in ("Class", "Function") or not n.file_path:
                    continue

                p = Path(n.file_path)
                try:
                    rel_file = p.relative_to(repo).as_posix()
                except Exception:
                    rel_file = p.as_posix()

                if file_added_symbols.get(rel_file, 0) >= 6:
                    continue

                sym_id = f"{rel_file}::{n.name}"
                if sym_id in node_ids:
                    continue

                file_added_symbols[rel_file] = file_added_symbols.get(rel_file, 0) + 1
                deg = degree_map.get(n.qualified_name or "", 0) + degree_map.get(sym_id, 0)

                # Inherit file's community
                parent_folder = Path(rel_file).parts[0] if len(Path(rel_file).parts) > 1 else "root"
                cid = abs(hash(parent_folder)) % max(1, len(comm_list) or 1) + 1 if comm_list else 0
                cname = cid_to_name.get(cid, "Module")
                color = cid_to_color.get(cid, "#4E79A7")
                community_member_counts[cid] = community_member_counts.get(cid, 0) + 1

                is_class = n.kind == "Class"
                node_size = max(8, min(22, 9 + deg * 1.5 + (3 if is_class else 0)))

                nodes.append({
                    "id": sym_id,
                    "label": f"{n.kind} {n.name}",
                    "title": f"{n.kind}: {n.name}\nFile: {rel_file}:{n.line_start}\nDegree: {deg}\nCommunity: {cname}",
                    "color": {
                        "background": color,
                        "border": "#fbbf24" if is_class else "#10b981",
                        "highlight": {"background": color, "border": "#38bdf8"},
                    },
                    "size": node_size,
                    "font": {"color": "#cbd5e1", "size": 11, "face": "Segoe UI, sans-serif"},
                    "community": cid,
                    "community_name": cname,
                    "source_file": rel_file,
                    "file_type": n.kind.lower(),
                    "shape": "diamond" if is_class else "square",
                    "degree": deg,
                    "start_line": n.line_start or 1,
                    "end_line": n.line_end or 1,
                })
                node_ids.add(sym_id)

                edges.append({
                    "from": rel_file,
                    "to": sym_id,
                    "label": "defines",
                    "title": f"defines [{rel_file} -> {n.name}]",
                    "dashes": True,
                    "width": 1,
                    "color": {"color": "rgba(255, 255, 255, 0.15)", "highlight": "#38bdf8"},
                    "arrows": {"to": {"enabled": True, "scaleFactor": 0.4}},
                })

            # 5. Add Relationship Edges (CALLS, IMPORTS, INHERITS)
            seen_edges: set[tuple[str, str, str]] = set()
            for e in all_edges:
                src_file = e.file_path
                if not src_file:
                    continue
                try:
                    src_rel = Path(src_file).relative_to(repo).as_posix()
                except Exception:
                    src_rel = Path(src_file).as_posix()

                target_qn = e.target_qualified or ""
                target_name = Path(target_qn).name if target_qn else None

                target_id = None
                if target_qn in node_ids:
                    target_id = target_qn
                elif target_name and target_name in node_ids:
                    target_id = target_name

                if target_id and src_rel in node_ids and src_rel != target_id:
                    rel_type = "calls" if e.kind == "CALLS" else "inherits" if e.kind == "INHERITS" else "imports"
                    edge_key = (src_rel, target_id, rel_type)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edge_color = "#a855f7" if rel_type == "calls" else "#f59e0b" if rel_type == "inherits" else "#3b82f6"
                        edges.append({
                            "from": src_rel,
                            "to": target_id,
                            "label": rel_type,
                            "title": f"{rel_type} [{src_rel} -> {target_id}]",
                            "dashes": False,
                            "width": 2 if rel_type in ("calls", "inherits") else 1.2,
                            "color": {"color": edge_color, "opacity": 0.75, "highlight": "#f43f5e"},
                            "arrows": {"to": {"enabled": True, "scaleFactor": 0.5}},
                        })

            # 6. Build Legend
            legend = []
            for cid, cname in sorted(cid_to_name.items()):
                cnt = community_member_counts.get(cid, 0)
                if cnt > 0:
                    legend.append({
                        "cid": cid,
                        "color": cid_to_color.get(cid, "#4E79A7"),
                        "label": cname,
                        "count": cnt,
                    })

            return {
                "nodes": nodes,
                "edges": edges,
                "legend": legend,
                "stats": {
                    "total_files": len(all_files),
                    "total_nodes": len(nodes),
                    "total_edges": len(edges),
                    "communities_count": len(legend),
                },
                "source": "graphify",
            }
        except Exception as exc:
            logger.exception("Error generating Graphify payload: %s", exc)
            return {"nodes": [], "edges": [], "legend": [], "stats": {}, "source": "error", "error": str(exc)}
        finally:
            try:
                store.close()
            except Exception:
                pass

    def generate_graphify_html(self, workspace_path: str | Path | None = None) -> str:
        """Generate a complete, standalone Graphify HTML visualization page."""
        import json
        payload = self.get_graphify_payload(workspace_path)
        nodes_json = json.dumps(payload.get("nodes", [])).replace("</", "<\\/")
        edges_json = json.dumps(payload.get("edges", [])).replace("</", "<\\/")
        legend_json = json.dumps(payload.get("legend", [])).replace("</", "<\\/")
        stats = payload.get("stats", {})
        stats_str = f"{stats.get('total_nodes', 0)} nodes · {stats.get('total_edges', 0)} edges · {stats.get('communities_count', 0)} communities"

        repo_name = Path(self._resolve_repo(workspace_path)).name

        html_template = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Graphify - __REPO_NAME__</title>
<script src="/vis-network.min.js"></script>
<script>
if (!window.vis) {
  document.write('<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"><\\/script>');
}
</script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0b0f17; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; height: 100vh; overflow: hidden; }
#graph { flex: 1; height: 100%; position: relative; }
#sidebar { width: 310px; background: #111827; border-left: 1px solid #1f2937; display: flex; flex-direction: column; overflow: hidden; box-shadow: -4px 0 20px rgba(0,0,0,0.4); }
#search-wrap { padding: 14px; border-bottom: 1px solid #1f2937; background: #0f172a; }
#search { width: 100%; background: #1e293b; border: 1px solid #334155; color: #f8fafc; padding: 9px 12px; border-radius: 8px; font-size: 13px; outline: none; transition: border-color 0.2s; }
#search:focus { border-color: #38bdf8; }
#search-results { max-height: 160px; overflow-y: auto; padding: 6px 12px; border-bottom: 1px solid #1f2937; display: none; background: #172033; }
.search-item { padding: 6px 8px; cursor: pointer; border-radius: 6px; font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #94a3b8; }
.search-item:hover { background: #334155; color: #fff; }
#info-panel { padding: 16px; border-bottom: 1px solid #1f2937; min-height: 160px; }
#info-panel h3 { font-size: 11px; color: #64748b; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700; }
#info-content { font-size: 13px; color: #cbd5e1; line-height: 1.6; }
#info-content .field { margin-bottom: 6px; }
#info-content .field b { color: #f8fafc; font-size: 14px; }
#info-content .empty { color: #64748b; font-style: italic; }
.neighbor-link { display: block; padding: 4px 8px; margin: 3px 0; border-radius: 4px; cursor: pointer; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; border-left: 3px solid #38bdf8; background: #1e293b; color: #cbd5e1; }
.neighbor-link:hover { background: #334155; color: #fff; }
#neighbors-list { max-height: 160px; overflow-y: auto; margin-top: 6px; }
#legend-wrap { flex: 1; overflow-y: auto; padding: 14px; }
#legend-wrap h3 { font-size: 11px; color: #64748b; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700; }
.legend-item { display: flex; align-items: center; gap: 8px; padding: 5px 6px; cursor: pointer; border-radius: 6px; font-size: 12px; transition: background 0.15s; }
.legend-item:hover { background: #1e293b; }
.legend-item.dimmed { opacity: 0.35; }
.legend-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.legend-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.legend-count { color: #64748b; font-size: 11px; }
#stats { padding: 12px 14px; border-top: 1px solid #1f2937; font-size: 11.5px; color: #64748b; background: #0f172a; }
.graph-toolbar { position: absolute; top: 14px; left: 14px; display: flex; gap: 8px; z-index: 10; }
.tool-btn { background: rgba(17, 24, 39, 0.85); backdrop-filter: blur(8px); border: 1px solid #334155; color: #e2e8f0; padding: 6px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; display: flex; align-items: center; gap: 6px; }
.tool-btn:hover { background: #1e293b; border-color: #38bdf8; }
</style>
</head>
<body>
<div id="graph">
  <div class="graph-toolbar">
    <button class="tool-btn" onclick="network.fit({animation: true})">Fit View</button>
    <button class="tool-btn" id="physicsBtn" onclick="togglePhysics()">Pause Physics</button>
  </div>
</div>
<div id="sidebar">
  <div id="search-wrap">
    <input id="search" type="text" placeholder="Search functions, classes, files..." autocomplete="off">
    <div id="search-results"></div>
  </div>
  <div id="info-panel">
    <h3>Node Inspector</h3>
    <div id="info-content"><span class="empty">Click any node to inspect relationships</span></div>
  </div>
  <div id="legend-wrap">
    <h3>Communities (Leiden Clusters)</h3>
    <div id="legend"></div>
  </div>
  <div id="stats">__STATS__</div>
</div>

<script>
const RAW_NODES = __NODES_JSON__;
const RAW_EDGES = __EDGES_JSON__;
const LEGEND = __LEGEND_JSON__;

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

const nodesDS = new vis.DataSet(RAW_NODES.map(n => ({
  id: n.id, label: n.label, color: n.color, size: n.size,
  font: n.font, title: n.title,
  _community: n.community, _community_name: n.community_name,
  _source_file: n.source_file, _file_type: n.file_type, _degree: n.degree,
  _line: n.start_line
})));

const edgesDS = new vis.DataSet(RAW_EDGES.map((e, i) => ({
  id: i, from: e.from, to: e.to,
  label: '',
  title: e.title,
  dashes: e.dashes,
  width: e.width,
  color: e.color,
  arrows: e.arrows,
})));

const container = document.getElementById('graph');
const network = new vis.Network(container, { nodes: nodesDS, edges: edgesDS }, {
  physics: {
    enabled: true,
    solver: 'forceAtlas2Based',
    forceAtlas2Based: {
      gravitationalConstant: -70,
      centralGravity: 0.008,
      springLength: 100,
      springConstant: 0.08,
      damping: 0.45,
      avoidOverlap: 0.85,
    },
    stabilization: { iterations: 180, fit: true },
  },
  interaction: {
    hover: true,
    tooltipDelay: 80,
    hideEdgesOnDrag: true,
    navigationButtons: false,
    keyboard: false,
  },
  nodes: { shape: 'dot', borderWidth: 1.5 },
  edges: { smooth: { type: 'continuous', roundness: 0.2 } },
});

network.once('stabilizationIterationsDone', () => {
  network.setOptions({ physics: { enabled: false } });
  const btn = document.getElementById('physicsBtn');
  if (btn) btn.textContent = 'Enable Physics';
});

let physicsEnabled = false;
function togglePhysics() {
  physicsEnabled = !physicsEnabled;
  network.setOptions({ physics: { enabled: physicsEnabled } });
  const btn = document.getElementById('physicsBtn');
  if (btn) btn.textContent = physicsEnabled ? 'Pause Physics' : 'Enable Physics';
}

function showInfo(nodeId) {
  const n = nodesDS.get(nodeId);
  if (!n) return;
  const neighborIds = network.getConnectedNodes(nodeId);
  const neighborItems = neighborIds.map(nid => {
    const nb = nodesDS.get(nid);
    const color = nb && nb.color ? (nb.color.background || '#38bdf8') : '#38bdf8';
    return `<span class="neighbor-link" style="border-left-color:${color}" data-nid="${esc(nid)}">${esc(nb ? nb.label : nid)}</span>`;
  }).join('');

  document.getElementById('info-content').innerHTML = `
    <div class="field"><b>${esc(n.label)}</b></div>
    <div class="field"><span style="color:#94a3b8">Type:</span> ${esc(n._file_type || 'file')}</div>
    <div class="field"><span style="color:#94a3b8">Community:</span> ${esc(n._community_name || 'Core')}</div>
    <div class="field"><span style="color:#94a3b8">File:</span> ${esc(n._source_file || '-')}</div>
    <div class="field"><span style="color:#94a3b8">Connections:</span> ${n._degree || 0}</div>
    ${neighborIds.length ? `<div class="field" style="margin-top:10px;color:#94a3b8;font-size:11px">Connected Symbols (${neighborIds.length}):</div><div id="neighbors-list">${neighborItems}</div>` : ''}
  `;
}

function focusNode(nodeId) {
  network.focus(nodeId, { scale: 1.4, animation: true });
  network.selectNodes([nodeId]);
  showInfo(nodeId);
}

document.addEventListener('click', e => {
  const el = e.target.closest('.neighbor-link');
  if (el && el.dataset.nid !== undefined) focusNode(el.dataset.nid);
});

network.on('click', params => {
  if (params.nodes.length > 0) {
    showInfo(params.nodes[0]);
  }
});

// Live search
const searchInput = document.getElementById('search');
const searchResults = document.getElementById('search-results');
searchInput.addEventListener('input', () => {
  const q = searchInput.value.trim().toLowerCase();
  if (!q) {
    searchResults.style.display = 'none';
    searchResults.innerHTML = '';
    return;
  }
  const matches = RAW_NODES.filter(n => (n.label || '').toLowerCase().includes(q) || (n.id || '').toLowerCase().includes(q)).slice(0, 10);
  if (!matches.length) {
    searchResults.style.display = 'block';
    searchResults.innerHTML = '<div style="padding:4px;color:#64748b;font-size:12px">No matching symbols</div>';
    return;
  }
  searchResults.style.display = 'block';
  searchResults.innerHTML = matches.map(m => `<div class="search-item" data-nid="${esc(m.id)}">${esc(m.label)} <span style="font-size:10px;color:#64748b">(${m.file_type || 'file'})</span></div>`).join('');
});

searchResults.addEventListener('click', e => {
  const item = e.target.closest('.search-item');
  if (item && item.dataset.nid) {
    focusNode(item.dataset.nid);
    searchResults.style.display = 'none';
  }
});

// Legend items
const legendEl = document.getElementById('legend');
const hiddenCommunities = new Set();
LEGEND.forEach(c => {
  const item = document.createElement('div');
  item.className = 'legend-item';
  item.innerHTML = `<div class="legend-dot" style="background:${c.color}"></div>
    <span class="legend-label">${c.label}</span>
    <span class="legend-count">${c.count}</span>`;
  item.onclick = () => {
    if (hiddenCommunities.has(c.cid)) {
      hiddenCommunities.delete(c.cid);
      item.classList.remove('dimmed');
    } else {
      hiddenCommunities.add(c.cid);
      item.classList.add('dimmed');
    }
    const updates = RAW_NODES
      .filter(n => n.community === c.cid)
      .map(n => ({ id: n.id, hidden: hiddenCommunities.has(c.cid) }));
    nodesDS.update(updates);
  };
  legendEl.appendChild(item);
});
</script>
</body>
</html>"""

        return (
            html_template
            .replace("__REPO_NAME__", repo_name)
            .replace("__STATS__", stats_str)
            .replace("__NODES_JSON__", nodes_json)
            .replace("__EDGES_JSON__", edges_json)
            .replace("__LEGEND_JSON__", legend_json)
        )


# Global singleton instance
code_graph_service = CodeGraphService()
