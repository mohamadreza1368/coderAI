"""
fastapi_app.py - Modern FastAPI server with non-blocking StreamingResponse, WebSocket (/ws/chat) and Abort/Cancel support.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import web_app
from codebase_index import CodebaseIndex
from code_graph_service import code_graph_service
from terminal_manager import terminal_manager
from tools import cancel_current_execution, get_workspace, set_workspace
from vector_store import EmbeddingModelManager


def create_app() -> FastAPI:
    app = FastAPI(title="CoderAI Workspace API", version="2.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_no_cache_header(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    static_dir = Path(web_app.STATIC_DIR).resolve()
    static_dir.mkdir(exist_ok=True)

    @app.get("/")
    async def index():
        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file), media_type="text/html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        return HTMLResponse("<h1>CoderAI Web UI is ready</h1>")

    @app.get("/api/state")
    @app.get("/api/settings")
    async def get_state(request: Request):
        sid = request.headers.get("x-session-id") or request.query_params.get("session_id")
        return web_app._client_state(sid)

    @app.get("/api/hindsight/status")
    async def get_hindsight_status():
        from hindsight_manager import get_hindsight_manager
        hm = get_hindsight_manager()
        return hm.get_status()

    @app.post("/api/settings")
    async def save_settings(request: Request):
        data = await request.json()
        for key in (
            "conn_mode", "temperature", "enable_thinking",
            "custom_api_url", "memory_enabled",
            "context_token_budget", "response_token_budget", "auto_continue",
            "tavily_enabled", "tavily_api_key",
            "git_approval_mode",
            "smart_skill_confirmation",
            "sandbox_mode", "sandbox_docker_image",
        ):
            if key in data:
                web_app.STATE[key] = data[key]
        if "custom_api_key" in data and str(data["custom_api_key"] or "").strip():
            web_app.STATE["custom_api_key"] = str(data["custom_api_key"]).strip()
        if "custom_api_model" in data and str(data["custom_api_model"] or "").strip():
            web_app.STATE["custom_api_model"] = str(data["custom_api_model"]).strip()

        is_custom = "custom" in str(web_app.STATE.get("conn_mode") or "").lower()
        if is_custom:
            if "model" in data and str(data["model"] or "").strip() and data["model"] != "gemma4:12b":
                web_app.STATE["custom_api_model"] = str(data["model"]).strip()
            if str(web_app.STATE.get("custom_api_model") or "").strip():
                web_app.STATE["model"] = web_app.STATE["custom_api_model"]
        else:
            if "model" in data and str(data["model"] or "").strip():
                web_app.STATE["model"] = str(data["model"]).strip()
                web_app.STATE["model_user_selected"] = True

        web_app.STATE["custom_api_model"] = str(web_app.STATE.get("custom_api_model") or "gpt-4o-mini").strip()
        if "tavily_api_key" in data:
            web_app.STATE["tavily_api_key"] = str(data.get("tavily_api_key") or "").strip()
        if not web_app.STATE.get("tavily_enabled"):
            web_app.STATE["tavily_api_key"] = ""
        web_app._sync_tool_settings()
        for key in ("context_token_budget", "response_token_budget"):
            if key in web_app.STATE:
                web_app.STATE[key] = max(512, int(web_app.STATE[key]))
        web_app._save_persisted_settings()
        return web_app._client_state()

    @app.get("/api/models")
    async def get_models(request: Request):
        force = request.query_params.get("force", "false").lower() == "true"
        return web_app._available_models(force=force)

    @app.get("/api/projects")
    async def get_projects():
        return {"projects": web_app._project_cards()}

    @app.get("/api/prompts")
    async def get_prompts():
        return web_app._prompt_payload()

    @app.get("/api/skills")
    async def get_skills():
        return {"skills": web_app._skills_payload()}

    @app.get("/api/skills/diagnostics")
    async def get_skills_diagnostics():
        return web_app._skills_diagnostics()

    @app.get("/api/skills/usage")
    async def get_skills_usage():
        return web_app._skill_usage_payload()

    @app.get("/api/index/graph")
    @app.get("/api/graph/structure")
    async def get_schematic_graph():
        index = CodebaseIndex(web_app.get_workspace())
        return index.get_schematic_graph()

    @app.get("/api/memory/graph/stats")
    async def get_graph_stats():
        return web_app._graph_memory_store().get_stats()

    @app.get("/api/memory/graph/search")
    async def search_graph(q: str = ""):
        return web_app._graph_memory_store().search(q)

    @app.post("/api/memory/graph/forget")
    async def forget_graph_entity(request: Request):
        data = await request.json()
        entity = str(data.get("entity") or data.get("entity_id") or "").strip()
        deleted = web_app._graph_memory_store().forget_entity(entity)
        return {"ok": True, "deleted": deleted, "stats": web_app._graph_memory_store().get_stats()}

    @app.post("/api/memory/graph/fact")
    async def add_graph_fact(request: Request):
        data = await request.json()
        sub = str(data.get("subject") or "").strip()
        rel = str(data.get("relation") or "").strip()
        obj = str(data.get("object") or "").strip()
        conf = float(data.get("confidence", 1.0))
        fact = web_app._graph_memory_store().add_fact(sub, rel, obj, confidence=conf)
        return {"ok": True, "fact": {"id": fact.id, "text": fact.fact_text}, "stats": web_app._graph_memory_store().get_stats()}

    @app.post("/api/memory/graph/extract")
    async def extract_graph_triples(request: Request):
        data = await request.json()
        text = str(data.get("text") or "").strip()
        triples = web_app._graph_memory_store().extract_triples_rule_based(text)
        for item in triples:
            web_app._graph_memory_store().add_fact(item["subject"], item["relation"], item["object"], confidence=item.get("confidence", 0.9))
        return {"ok": True, "extracted": len(triples), "triples": triples, "stats": web_app._graph_memory_store().get_stats()}

    @app.post("/api/memory/graph/index")
    @app.post("/api/project/index")
    async def index_project_endpoint():
        res = web_app._graph_memory_store().index_project_workspace()
        try:
            CodebaseIndex(web_app.get_workspace()).sync_incremental()
        except Exception:
            pass
        return {"ok": True, "result": res}

    @app.post("/api/cancel")
    async def cancel_execution():
        killed = cancel_current_execution()
        web_app.STATE["agent_running"] = False
        return {
            "ok": True,
            "cancelled": True,
            "process_killed": killed,
            "state": web_app._client_state(),
        }

    @app.post("/api/project/delete")
    @app.delete("/api/project")
    async def delete_project(request: Request):
        data = await request.json()
        manager = web_app._memory_manager()
        project_id = data.get("project_id")
        workspace_path = data.get("workspace_path")
        deleted = False
        if project_id is not None:
            deleted = manager.delete_project_by_id(int(project_id))
        elif workspace_path:
            deleted = manager.delete_project_by_path(str(workspace_path))
        return {
            "ok": True,
            "deleted": deleted,
            "projects": web_app._project_cards(manager),
        }

    @app.get("/api/memory")
    async def get_memory():
        return web_app._memory_payload()

    @app.post("/api/memory/archive")
    async def memory_archive(request: Request):
        data = await request.json()
        manager = web_app._memory_manager()
        project_id = data.get("project_id")
        session_id = str(data.get("session_id") or "")
        project = manager.get_project(int(project_id)) if project_id else None
        return {
            "projects": web_app._project_cards(manager),
            "sessions": manager.list_sessions(int(project_id)) if project_id else [],
            "session": manager.load_session(session_id) if session_id else None,
            "project": project,
            "files": web_app._workspace_snapshot_for(project["workspace_path"])["files"] if project and Path(project["workspace_path"]).is_dir() else [],
            "facts": manager.list_facts(project_id=int(project_id)) if project_id else [],
            "preferences": manager.get_user_preferences(int(project_id)) if project_id else {},
            "skill_usage": web_app._skill_usage_payload(project["workspace_path"]) if project else {"skills": [], "recent": []},
        }

    @app.post("/api/memory/archive/file")
    async def memory_archive_file(request: Request):
        data = await request.json()
        manager = web_app._memory_manager()
        project = manager.get_project(int(data.get("project_id")))
        if not project:
            raise HTTPException(status_code=404, detail="Archived project not found")
        return web_app._read_workspace_file(project["workspace_path"], str(data.get("path") or ""))

    @app.post("/api/memory/session/resume")
    async def memory_session_resume(request: Request):
        data = await request.json()
        session_id = str(data.get("session_id") or "").strip()
        session = web_app._memory_manager().load_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        ok, message = set_workspace(session["workspace_path"])
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        web_app.STATE["memory_session_id"] = session_id
        web_app.STATE["messages"] = [
            {"role": turn["role"], "content": turn["content"]}
            for turn in session["turns"]
        ]
        web_app.STATE["tools_log"] = []
        web_app.STATE["used_skills_log"] = []
        web_app.STATE["memory_summary"] = session.get("summary", "")
        web_app.STATE["memory_summarized_count"] = 0
        web_app.STATE["memory_retrieval_count"] = 0
        web_app.STATE["memory_retrieved_facts"] = []
        web_app.STATE["memory_context"] = ""
        sid = request.headers.get("x-session-id")
        return web_app._client_state(sid)

    @app.post("/api/memory/fact")
    async def memory_fact(request: Request):
        data = await request.json()
        manager = web_app._memory_manager()
        fact_id = data.get("id")
        if fact_id:
            manager.update_fact(int(fact_id), data.get("fact", ""), data.get("source", "manual"))
        else:
            manager.index_fact(data.get("fact", ""), data.get("source", "manual"))
        return web_app._memory_payload()

    @app.post("/api/memory/fact/delete")
    async def memory_fact_delete(request: Request):
        data = await request.json()
        web_app._memory_manager().delete_fact(int(data.get("id")))
        return web_app._memory_payload()

    @app.post("/api/memory/preference")
    async def memory_preference(request: Request):
        data = await request.json()
        web_app._memory_manager().update_preference(data.get("key", ""), data.get("value", ""))
        return web_app._memory_payload()

    @app.post("/api/memory/preference/delete")
    async def memory_preference_delete(request: Request):
        data = await request.json()
        web_app._memory_manager().delete_preference(data.get("key", ""))
        return web_app._memory_payload()

    @app.post("/api/memory/forget")
    async def memory_forget(request: Request):
        data = await request.json()
        if data.get("confirm") is not True:
            raise HTTPException(status_code=403, detail="Explicit confirmation is required")
        web_app._memory_manager().forget_project()
        web_app.STATE["messages"].clear()
        web_app.STATE["tools_log"].clear()
        web_app.STATE["used_skills_log"].clear()
        web_app.STATE["memory_session_id"] = uuid.uuid4().hex
        web_app.STATE["memory_summary"] = ""
        web_app.STATE["memory_summarized_count"] = 0
        web_app.STATE["memory_retrieval_count"] = 0
        web_app.STATE["memory_retrieved_facts"] = []
        web_app.STATE["memory_context"] = ""
        sid = request.headers.get("x-session-id")
        return web_app._client_state(sid)

    @app.post("/api/memory/compact")
    async def memory_compact(request: Request):
        web_app._compact_memory_if_needed()
        try:
            web_app._memory_manager().summarize_old_session(web_app.STATE["memory_session_id"])
        except Exception:
            pass
        sid = request.headers.get("x-session-id")
        return web_app._client_state(sid)

    @app.post("/api/clear")
    async def clear_session(request: Request):
        sid = request.headers.get("x-session-id")
        return web_app._clear_session_state(sid)

    @app.get("/api/file")
    async def get_file(request: Request):
        rel = request.query_params.get("path", "")
        try:
            return web_app._read_file(rel)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/file")
    @app.post("/api/file/write")
    async def write_file_endpoint(request: Request):
        data = await request.json()
        rel = data.get("path", "")
        content = data.get("content", "")
        web_app._write_file(rel, content)
        return {"ok": True, "path": rel}

    @app.get("/api/git")
    async def get_git():
        return web_app._git_snapshot()

    @app.post("/api/git/init")
    async def git_init():
        web_app._git_manager().init_repo()
        return web_app._git_snapshot()
    @app.post("/api/git/clone_stream")
    async def git_clone_stream(request: Request):
        data = await request.json()
        
        async def event_generator() -> AsyncGenerator[str, None]:
            q: queue.Queue = queue.Queue()
            
            def sink(ev: dict):
                q.put(ev)
                
            def worker():
                try:
                    remote_url = web_app._validate_git_remote(data.get("remote_url", ""))
                    destination = web_app._resolve_clone_destination(remote_url, str(data.get("destination") or ""))
                    
                    sink({"type": "git_clone_status", "message": f"Cloning {remote_url}"})
                    
                    from git_manager import GitManager
                    manager = GitManager.clone_repository(
                        remote_url,
                        destination,
                        on_output=lambda line: sink({"type": "git_clone_output", "content": line}),
                        username=str(data.get("username") or ""),
                        token=str(data.get("token") or ""),
                    )
                    
                    ok, message = web_app._activate_workspace_memory(destination)
                    if not ok:
                        raise Exception(message)
                        
                    web_app.STATE["git_checkpoint_workspace"] = ""
                    sink({"type": "git_clone_done", "path": str(manager.repo_path), "state": web_app._client_state()})
                except Exception as e:
                    sink({"type": "git_clone_error", "message": str(e)})
                finally:
                    q.put(None)
                    
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            
            while True:
                try:
                    ev = q.get_nowait()
                    if ev is None:
                        break
                    yield json.dumps(ev, default=web_app._json_default, ensure_ascii=False) + "\n"
                except queue.Empty:
                    if not thread.is_alive() and q.empty():
                        break
                    await asyncio.sleep(0.01)
                    
        return StreamingResponse(event_generator(), media_type="application/x-ndjson")

    @app.post("/api/git/push-preview")
    async def git_push_preview():
        preview = web_app._git_manager().get_push_preview()
        if not preview.get("remote"):
            raise HTTPException(status_code=400, detail="This repository has no origin remote")
        return preview

    @app.post("/api/git/push")
    async def git_push(request: Request):
        data = await request.json()
        if data.get("approved") is not True:
            raise HTTPException(status_code=403, detail="Explicit user approval is required before push")
        output = web_app._git_manager().push(
            username=str(data.get("username") or ""),
            token=str(data.get("token") or ""),
        )
        return {"ok": True, "message": output, "git": web_app._git_snapshot()}

    @app.post("/api/git/fetch")
    async def git_fetch(request: Request):
        data = await request.json()
        output = web_app._git_manager().fetch(
            remote=str(data.get("remote") or "origin"),
            username=str(data.get("username") or ""),
            token=str(data.get("token") or ""),
        )
        return {"ok": True, "message": output, "git": web_app._git_snapshot()}

    @app.post("/api/git/pull")
    async def git_pull(request: Request):
        data = await request.json()
        result = web_app._git_manager().pull(
            remote=str(data.get("remote") or "origin"),
            branch=str(data.get("branch") or ""),
            username=str(data.get("username") or ""),
            token=str(data.get("token") or ""),
        )
        return {"ok": result.get("ok", False), "conflict": result.get("conflict", False), "message": result.get("message", ""), "git": web_app._git_snapshot()}

    @app.post("/api/git/branch/switch")
    async def git_branch_switch(request: Request):
        data = await request.json()
        name = str(data.get("name") or data.get("branch") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Branch name is required")
        res = web_app._git_manager().switch_branch(name)
        return {"ok": True, "branch": res.get("branch"), "message": res.get("output", ""), "git": web_app._git_snapshot()}

    @app.post("/api/git/branch/create")
    async def git_branch_create(request: Request):
        data = await request.json()
        name = str(data.get("name") or data.get("branch") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Branch name is required")
        start_point = str(data.get("start_point") or "").strip()
        res = web_app._git_manager().create_branch(name, start_point=start_point)
        return {"ok": True, "branch": res.get("branch"), "message": res.get("output", ""), "git": web_app._git_snapshot()}

    @app.post("/api/git/conflicts/resolve")
    async def git_conflict_resolve(request: Request):
        data = await request.json()
        path_str = str(data.get("path") or "").strip()
        resolution = str(data.get("resolution") or "").strip()
        custom = str(data.get("custom_content") or "")
        if not path_str or not resolution:
            raise HTTPException(status_code=400, detail="path and resolution are required")
        res = web_app._git_manager().resolve_conflict(path_str, resolution, custom_content=custom)
        return {"ok": True, "result": res, "git": web_app._git_snapshot()}

    @app.post("/api/git/merge/abort")
    async def git_merge_abort():
        out = web_app._git_manager().abort_merge()
        return {"ok": True, "message": out, "git": web_app._git_snapshot()}

    @app.post("/api/git/merge/complete")
    async def git_merge_complete(request: Request):
        data = await request.json()
        message = str(data.get("message") or "").strip()
        commit_hash = web_app._git_manager().complete_merge(message)
        return {"ok": True, "commit": commit_hash, "git": web_app._git_snapshot()}

    @app.post("/api/git/revert/{commit_hash}")
    async def git_revert(commit_hash: str):
        new_hash = web_app._git_manager().revert_to(commit_hash)
        return {"ok": True, "commit": new_hash, "git": web_app._git_snapshot()}

    @app.get("/api/approval")
    async def get_approval():
        return web_app.get_approval_state()

    @app.post("/api/approval/approve")
    @app.post("/api/git/approve")
    async def approve_action(request: Request):
        data = await request.json()
        from tools import approve_pending, get_approval_state
        approve_pending(bool(data.get("always_allow_for_session")))
        return get_approval_state()

    @app.post("/api/approval/reject")
    @app.post("/api/git/reject")
    async def reject_action(request: Request):
        data = await request.json()
        from tools import reject_pending, get_approval_state
        reject_pending(data.get("reason", ""))
        return get_approval_state()

    @app.get("/api/policies")
    async def get_policies(workspace_path: str = ""):
        return web_app._get_policies_payload(workspace_path or None)

    @app.post("/api/policies")
    async def update_policies(request: Request):
        data = await request.json()
        return web_app._update_policy_payload(data)

    @app.post("/api/policies/reset")
    async def reset_policies(request: Request):
        data = await request.json()
        return web_app._reset_policy_payload(data)

    @app.post("/api/scan")
    async def scan_project(request: Request):
        data = await request.json()
        max_files = int(data.get("max_files", 200))
        result = web_app.tool_scan_project(max_files)
        sid = request.headers.get("x-session-id")
        return {"result": result, "state": web_app._client_state(sid)}

    @app.post("/api/skills/mode")
    async def skill_mode(request: Request):
        data = await request.json()
        skill_name = str(data.get("skill_name") or "")
        if not web_app.sm.get(skill_name):
            raise HTTPException(status_code=404, detail="Skill not found")
        target_workspace = web_app._known_project_workspace(data.get("workspace_path"))
        web_app._skill_tracker(target_workspace).set_mode(skill_name, str(data.get("mode") or "auto"))
        if data.get("workspace_path"):
            return {"skill_usage": web_app._skill_usage_payload(target_workspace)}
        sid = request.headers.get("x-session-id")
        return web_app._client_state(sid)

    @app.post("/api/skills/disable")
    async def skill_disable(request: Request):
        data = await request.json()
        skill_name = str(data.get("skill_name") or "")
        if not web_app.sm.get(skill_name):
            raise HTTPException(status_code=404, detail="Skill not found")
        web_app._skill_tracker().set_disabled(skill_name, bool(data.get("disabled")))
        return web_app._skill_usage_payload()

    @app.get("/api/skill/source")
    async def get_skill_source(request: Request):
        name = request.query_params.get("name", "")
        skill = web_app.sm.get(name)
        if not skill:
            raise HTTPException(status_code=404, detail="Skill not found")
        return {"name": skill.name, "path": str(skill.path), "content": skill.path.read_text(encoding="utf-8", errors="replace")}

    @app.post("/api/skill/source")
    async def save_skill_source(request: Request):
        data = await request.json()
        skill = web_app.sm.get(str(data.get("name") or ""))
        if not skill:
            raise HTTPException(status_code=404, detail="Skill not found")
        skill.path.write_text(str(data.get("content") or ""), encoding="utf-8")
        web_app.sm.reload()
        web_app.skill_router.invalidate()
        return {"ok": True, "name": skill.name, "skill_usage": web_app._skill_usage_payload()}

    @app.get("/api/prompt")
    async def get_prompt(request: Request):
        name = request.query_params.get("name", "")
        prompt = web_app.pm.get(name)
        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt not found")
        return {
            "name": prompt.name,
            "category": prompt.category,
            "content": prompt.content,
            "preview": prompt.preview,
            "size": prompt.size,
        }

    @app.post("/api/prompt")
    async def set_prompt(request: Request):
        data = await request.json()
        if data.get("selected_prompt"):
            prompt = web_app.pm.get(data["selected_prompt"])
            if not prompt:
                raise HTTPException(status_code=404, detail="Prompt not found")
            web_app.STATE["selected_prompt"] = prompt.name
            web_app.STATE["system_prompt"] = prompt.content
        elif "system_prompt" in data:
            web_app.STATE["selected_prompt"] = None
            web_app.STATE["system_prompt"] = data.get("system_prompt") or web_app.DEFAULT_SYSTEM_PROMPT
        sid = request.headers.get("x-session-id")
        return web_app._client_state(sid)

    @app.get("/api/index")
    async def get_index():
        return web_app.CodebaseIndex(web_app.get_workspace()).status(check_freshness=True)

    @app.get("/api/index/overview")
    async def get_index_overview():
        index = web_app.CodebaseIndex(web_app.get_workspace())
        return {"overview": index.get_project_overview(), "graph": index.dependency_tree()}

    @app.post("/api/index/sync")
    async def index_sync():
        return web_app.CodebaseIndex(web_app.get_workspace()).sync_incremental()

    @app.post("/api/index/rebuild")
    async def index_rebuild():
        return web_app.CodebaseIndex(web_app.get_workspace()).rebuild()

    @app.post("/api/index/regenerate-summary")
    async def index_regenerate_summary(request: Request):
        data = await request.json()
        model = web_app._active_model() if data.get("use_model") else None
        return web_app.CodebaseIndex(web_app.get_workspace()).regenerate_summaries(model=model)

    @app.get("/api/graph/overview")
    async def get_graph_overview(request: Request):
        detail = request.query_params.get("detail", "minimal")
        ws = request.query_params.get("workspace_path") or get_workspace()
        res = code_graph_service.get_architecture_overview(ws, detail_level=detail)
        return res

    @app.post("/api/graph/impact")
    async def get_graph_impact(request: Request):
        data = await request.json() if request.headers.get("content-type") == "application/json" else {}
        files = data.get("files", []) or data.get("changed_files", [])
        depth = data.get("depth", 2) or data.get("max_depth", 2)
        ws = data.get("workspace_path") or get_workspace()
        res = code_graph_service.get_impact_radius(ws, changed_files=files, max_depth=depth)
        return res

    @app.post("/api/graph/build")
    async def build_graph(request: Request):
        data = await request.json() if request.headers.get("content-type") == "application/json" else {}
        full = data.get("full_rebuild", False)
        ws = data.get("workspace_path") or get_workspace()
        res = code_graph_service.build_or_update(ws, full_rebuild=full)
        return res

    @app.get("/api/graph/stats")
    async def get_graph_stats(request: Request):
        ws = request.query_params.get("workspace_path") or get_workspace()
        return code_graph_service.get_stats(ws)

    @app.get("/api/graph/graphify.html")
    async def get_graphify_html(request: Request):
        ws = request.query_params.get("workspace_path") or get_workspace()
        html = code_graph_service.generate_graphify_html(ws)
        return HTMLResponse(html, media_type="text/html")

    @app.get("/api/graph/graphify-data")
    async def get_graphify_data(request: Request):
        ws = request.query_params.get("workspace_path") or get_workspace()
        max_n = int(request.query_params.get("max_nodes", "250"))
        return code_graph_service.get_graphify_payload(ws, max_nodes=max_n)

    @app.get("/api/browse")
    @app.post("/api/browse")
    async def browse_folder(request: Request):
        initial = None
        if request.method == "POST":
            data = await request.json()
            initial = data.get("initial_dir")
        picked = web_app._browse_local_folder(initial or str(web_app.get_workspace()))
        if not picked:
            return {"ok": False, "cancelled": True, "message": "Folder selection was cancelled"}
        ok, msg = web_app._activate_workspace_memory(picked)
        return {"ok": ok, "message": msg, "workspace": web_app._workspace_snapshot(), "path": picked}

    @app.get("/api/ollama/embedding-status")
    async def get_embedding_status():
        status = EmbeddingModelManager.get_instance().get_status()
        status["dismissed"] = bool(web_app.STATE.get("embedding_dismissed"))
        return status

    @app.post("/api/ollama/pull-embedding")
    async def pull_embedding():
        mgr = EmbeddingModelManager.get_instance()
        started = mgr.start_pull("embeddinggemma")
        return {"ok": started, "status": mgr.get_status()}

    @app.post("/api/ollama/dismiss-embedding")
    async def dismiss_embedding():
        web_app.STATE["embedding_dismissed"] = True
        return {"ok": True}

    @app.post("/api/workspace")
    async def activate_workspace(request: Request):
        data = await request.json()
        path = data.get("path", "")
        ok, msg = web_app._activate_workspace_memory(path)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"ok": ok, "message": msg, "workspace": web_app._workspace_snapshot()}

    @app.post("/api/chat")
    async def chat(request: Request):
        data = await request.json()
        prompt = data.get("prompt", "")
        context = data.get("active_context")
        return web_app._run_agent(prompt, context)

    @app.post("/api/chat_stream")
    async def chat_stream(request: Request):
        data = await request.json()
        prompt = data.get("prompt", "")
        context = data.get("active_context")

        async def event_generator() -> AsyncGenerator[str, None]:
            web_app.STATE["agent_running"] = True
            q: queue.Queue = queue.Queue()

            def thread_sink(ev: dict):
                q.put(ev)

            def worker():
                try:
                    web_app._run_agent_stream(prompt, thread_sink, context)
                finally:
                    q.put(None)

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()

            try:
                while True:
                    try:
                        ev = q.get_nowait()
                        if ev is None:
                            break
                        yield json.dumps(ev, default=web_app._json_default) + "\n"
                    except queue.Empty:
                        if not thread.is_alive() and q.empty():
                            break
                        await asyncio.sleep(0.01)
            finally:
                web_app.STATE["agent_running"] = False

        return StreamingResponse(event_generator(), media_type="application/x-ndjson")

    # ══════════════════════════════════════════════════════════════════════════════
    # ── WebSocket Real-Time Chat & Thinking Logs
    # ══════════════════════════════════════════════════════════════════════════════
    @app.websocket("/ws/chat")
    async def websocket_chat(websocket: WebSocket):
        await websocket.accept()
        loop = asyncio.get_running_loop()

        try:
            while True:
                msg_text = await websocket.receive_text()
                data = json.loads(msg_text)
                action = data.get("type", "chat")
                sid = websocket.query_params.get("session_id") or data.get("session_id")

                if action == "cancel":
                    cancel_current_execution()
                    web_app.STATE["agent_running"] = False
                    await websocket.send_text(json.dumps({
                        "type": "cancelled",
                        "state": web_app._client_state(sid),
                    }))

                elif action == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))

                elif action == "chat":
                    prompt = data.get("prompt", "")
                    context = data.get("active_context")
                    web_app.STATE["agent_running"] = True

                    def sink(ev: dict):
                        try:
                            asyncio.run_coroutine_threadsafe(
                                websocket.send_text(json.dumps(ev, default=web_app._json_default)),
                                loop,
                            )
                        except Exception:
                            pass

                    def run_worker():
                        try:
                            web_app._run_agent_stream(prompt, sink, context)
                        finally:
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    websocket.send_text(json.dumps({"type": "done", "state": web_app._client_state(sid)}, default=web_app._json_default)),
                                    loop,
                                )
                            except Exception:
                                pass

                    await loop.run_in_executor(None, run_worker)
                    web_app.STATE["agent_running"] = False

        except (WebSocketDisconnect, Exception):
            cancel_current_execution()
            web_app.STATE["agent_running"] = False

    # ══════════════════════════════════════════════════════════════════════════════
    # ── Integrated Terminal Endpoints
    # ══════════════════════════════════════════════════════════════════════════════
    @app.get("/api/terminal/info")
    async def terminal_info(request: Request):
        session_id = request.query_params.get("session_id", "default")
        session = terminal_manager.get_or_create_session(session_id, cwd=get_workspace())
        return {
            "session_id": session.session_id,
            "shell_type": session.shell_type,
            "cwd": str(session.cwd),
            "is_running": session.is_running(),
            "available_shells": terminal_manager.list_available_shells(),
            "history": session.history[-30:],
        }

    @app.websocket("/api/terminal/ws")
    async def terminal_websocket(websocket: WebSocket):
        await websocket.accept()
        session_id = websocket.query_params.get("session_id", "default")
        shell = websocket.query_params.get("shell", "powershell")
        ws_cwd = get_workspace()
        session = terminal_manager.get_or_create_session(session_id, shell_type=shell, cwd=ws_cwd)

        loop = asyncio.get_running_loop()
        handle, sub_queue = session.subscribe_async(loop)
        ws_lock = asyncio.Lock()

        async def safe_send(text: str) -> None:
            if not text:
                return
            async with ws_lock:
                try:
                    await websocket.send_text(text)
                except Exception:
                    pass

        # Replay buffered output history only if requested (avoids duplicate prompts on reconnect)
        replay = websocket.query_params.get("replay", "1")
        if replay in {"1", "true", "yes"}:
            history = session.get_output_history()
            if history and history.strip():
                await safe_send(history)

        async def send_terminal_output():
            try:
                while True:
                    event = await sub_queue.get()
                    text = (event.get("data") or event.get("text", "")) if event else ""
                    if text:
                        await safe_send(text)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        sender_task = asyncio.create_task(send_terminal_output())

        try:
            while True:
                msg = await websocket.receive_text()
                try:
                    data = json.loads(msg)
                    if isinstance(data, dict):
                        mtype = data.get("type")
                        if mtype == "input":
                            raw_input = data.get("data", "")
                            # Protect against unsolicited escape echoes
                            if (
                                raw_input
                                and not (raw_input.startswith("\x1b[?") and raw_input.endswith("c"))
                                and "?1;2c" not in raw_input
                                and "?1;0c" not in raw_input
                            ):
                                session.write(raw_input)
                        elif mtype == "resize":
                            try:
                                rows = data.get("rows")
                                cols = data.get("cols")
                                r_val = int(rows) if rows is not None and str(rows).isdigit() else 24
                                c_val = int(cols) if cols is not None and str(cols).isdigit() else 80
                                session.resize(r_val, c_val)
                            except Exception:
                                pass
                        elif mtype == "restart":
                            session.restart(data.get("shell", session.shell_type))
                        elif mtype == "kill":
                            session.kill()
                        elif mtype == "ping":
                            await safe_send(json.dumps({"type": "pong"}))
                        continue
                except Exception:
                    pass
                if not msg.startswith("{"):
                    session.write(msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            session.unsubscribe_async(handle)
            sender_task.cancel()
            try:
                await sender_task
            except (asyncio.CancelledError, Exception):
                pass

    @app.get("/api/terminal/stream")
    async def terminal_stream(request: Request):
        session_id = request.query_params.get("session_id", "default")
        session = terminal_manager.get_or_create_session(session_id, cwd=get_workspace())

        def event_stream():
            for event in session.stream_events():
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/terminal/exec")
    async def terminal_exec(request: Request):
        data = await request.json()
        session_id = data.get("session_id", "default")
        command = data.get("command", "")
        shell_type = data.get("shell_type", "powershell")
        cwd = data.get("cwd") or str(get_workspace())
        session = terminal_manager.get_or_create_session(session_id, shell_type=shell_type, cwd=Path(cwd))
        if session.is_running():
            session.write_stdin(command)
        elif command.strip():
            session.execute(command.strip(), cwd=Path(cwd) if cwd else None)
        return {"ok": True, "is_running": session.is_running(), "cwd": str(session.cwd)}

    @app.post("/api/terminal/kill")
    async def terminal_kill(request: Request):
        data = await request.json()
        session_id = data.get("session_id", "default")
        session = terminal_manager.get_or_create_session(session_id, cwd=get_workspace())
        killed = session.kill()
        return {"ok": True, "killed": killed}

    @app.post("/api/terminal/clear")
    async def terminal_clear(request: Request):
        data = await request.json()
        session_id = data.get("session_id", "default")
        session = terminal_manager.get_or_create_session(session_id, cwd=get_workspace())
        while not session.output_queue.empty():
            try:
                session.output_queue.get_nowait()
            except Exception:
                break
        return {"ok": True}

    # Mount static assets
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()


def main():
    host = os.getenv("WEB_APP_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_APP_PORT", "7864"))
    print(f"Starting CoderAI FastAPI server on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info", ws_ping_interval=None)


if __name__ == "__main__":
    main()
