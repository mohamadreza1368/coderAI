const state = {
  data: null,
  activeFile: null,
  fileContent: "",
  generatedInfo: "txt",
  editorDirty: false,
  contextUsage: {},
  workspaceLocked: false,
  pendingApprovalType: null,
  pendingPush: null,
  projectCards: [],
  projectArchive: null,
};

const $ = (id) => document.getElementById(id);

function installEditorMetricStyles() {
  if (document.getElementById("editorMetricStyles")) return;
  const style = document.createElement("style");
  style.id = "editorMetricStyles";
  style.textContent = `
    .code-highlight,
    .code-highlight code,
    .code-editor {
      font-family: Consolas, "SFMono-Regular", "Cascadia Mono", "IBM Plex Mono", ui-monospace, monospace !important;
      font-size: 13px !important;
      font-weight: 400 !important;
      line-height: 20px !important;
      letter-spacing: 0 !important;
      word-spacing: 0 !important;
      tab-size: 2 !important;
      font-variant-ligatures: none !important;
      font-kerning: none !important;
      white-space: pre !important;
      overflow-wrap: normal !important;
      word-break: normal !important;
    }
    .code-highlight,
    .code-editor {
      padding: 16px 16px 16px 62px !important;
      overflow: auto !important;
    }
    .code-highlight code {
      display: block !important;
      min-width: max-content !important;
    }
    .code-editor {
      -webkit-text-size-adjust: 100%;
      text-size-adjust: 100%;
    }
  `;
  document.head.appendChild(style);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || data.message || "Request failed");
  return data;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes}b`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

function formatTokens(value) {
  const num = Number(value || 0);
  if (num >= 1000000) return `${(num / 1000000).toFixed(num >= 10000000 ? 0 : 1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(num >= 10000 ? 0 : 1)}K`;
  return String(Math.max(0, Math.round(num)));
}

function extensionForInfo(info) {
  const lang = String(info || "txt").trim().split(/\s+/)[0].toLowerCase();
  const map = {
    javascript: "js",
    js: "js",
    typescript: "ts",
    ts: "ts",
    tsx: "tsx",
    jsx: "jsx",
    python: "py",
    py: "py",
    html: "html",
    css: "css",
    json: "json",
    markdown: "md",
    md: "md",
    shell: "sh",
    bash: "sh",
    powershell: "ps1",
    ps1: "ps1",
    yaml: "yml",
    yml: "yml",
    sql: "sql",
  };
  return map[lang] || lang.replace(/[^a-z0-9]/g, "") || "txt";
}

function languageForInfo(info) {
  const lang = String(info || "text").trim().split(/\s+/)[0].toLowerCase();
  const map = {
    py: "python",
    python: "python",
    js: "javascript",
    javascript: "javascript",
    jsx: "javascript",
    ts: "typescript",
    tsx: "typescript",
    typescript: "typescript",
    java: "java",
    html: "html",
    htm: "html",
    css: "css",
    json: "json",
    md: "markdown",
    markdown: "markdown",
    sh: "shell",
    bash: "shell",
    shell: "shell",
    ps1: "powershell",
    powershell: "powershell",
    sql: "sql",
    yml: "yaml",
    yaml: "yaml",
  };
  return map[lang] || "text";
}

function tokenClass(token, language) {
  const keywordSets = {
    python: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self",
    javascript: "await async break case catch class const continue debugger default delete do else export extends false finally for from function if import in instanceof let new null return static super switch this throw true try typeof undefined var void while yield",
    typescript: "abstract any as async await boolean break case catch class const constructor continue declare default delete do else enum export extends false finally for from function if implements import in instanceof interface keyof let module namespace never new null number private protected public readonly return static string super switch this throw true try type typeof undefined unknown var void while yield",
    java: "abstract assert boolean break byte case catch char class const continue default do double else enum extends false final finally float for if implements import instanceof int interface long native new null package private protected public return short static strictfp super switch synchronized this throw throws transient true try void volatile while",
    css: "align-items animation background border bottom color content display flex font grid height justify-content left margin max-width min-height opacity overflow padding place-items position right text top transform transition width z-index",
    shell: "case do done elif else esac fi for function if in local return then while",
    powershell: "begin break catch class continue data do dynamicparam else elseif end exit filter finally for foreach from function if in param process return switch throw trap try until using var while",
    sql: "alter and as by case create delete desc distinct drop else end from group having in insert into is join left like limit not null on or order outer right select set table then update values when where",
  };
  const keywords = new Set((keywordSets[language] || "").split(/\s+/).filter(Boolean));
  if (/^\/\*[\s\S]*\*\/$/.test(token) || /^\/\/.*/.test(token) || /^#.*/.test(token) || /^<!--[\s\S]*-->$/.test(token)) return "tok-comment";
  if (/^["'`]/.test(token) || /^"""[\s\S]*"""$/.test(token) || /^'''[\s\S]*'''$/.test(token)) return "tok-string";
  if (/^\b\d/.test(token)) return "tok-number";
  if (language === "html" && /^<\/?[\w:-]+/.test(token)) return "tok-tag";
  if (language === "css" && /^[.#]?[-_a-zA-Z][-_a-zA-Z0-9]*(?=\s*:|\s*\{)/.test(token)) return "tok-selector";
  if (keywords.has(token)) return "tok-keyword";
  if (/^[A-Z][A-Za-z0-9_]*$/.test(token)) return "tok-type";
  return "";
}

function highlightCode(code, info = "text") {
  const language = languageForInfo(info);
  if (!code) return "";
  if (language === "json") {
    return escapeHtml(code).replace(
      /(&quot;(?:\\.|[^&])*?&quot;)(\s*:)?|\b(true|false|null)\b|-?\b\d+(?:\.\d+)?(?:e[+-]?\d+)?\b/gi,
      (match, stringToken, colon, boolToken) => {
        if (stringToken) return `<span class="${colon ? "tok-property" : "tok-string"}">${stringToken}</span>${colon || ""}`;
        if (boolToken) return `<span class="tok-keyword">${match}</span>`;
        return `<span class="tok-number">${match}</span>`;
      }
    );
  }

  const commentPrefix = ["python", "shell", "powershell", "yaml"].includes(language) ? "#.*" : "\\/\\/.*";
  const tokenPattern = new RegExp(
    "\\/\\*[\\s\\S]*?\\*\\/|<!--([\\s\\S]*?)-->|" +
      commentPrefix +
      "|\"\"\"[\\s\\S]*?\"\"\"|'''[\\s\\S]*?'''|\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'|`(?:\\\\.|[^`\\\\])*`|<\\/?[\\w:-]+(?:\\s+[^<>]*)?>|\\b\\d+(?:\\.\\d+)?\\b|\\b[A-Za-z_$][\\w$]*\\b",
    "g"
  );
  let html = "";
  let last = 0;
  for (const match of code.matchAll(tokenPattern)) {
    const token = match[0];
    html += escapeHtml(code.slice(last, match.index));
    const cls = tokenClass(token, language);
    html += cls ? `<span class="${cls}">${escapeHtml(token)}</span>` : escapeHtml(token);
    last = match.index + token.length;
  }
  html += escapeHtml(code.slice(last));
  return html;
}

function setCodeEditorContent(title, meta, content, info = "txt") {
  state.activeFile = title;
  state.fileContent = content || "";
  state.generatedInfo = info || "txt";
  state.editorDirty = false;
  const language = languageForInfo(state.generatedInfo);
  $("activeFile").textContent = title;
  $("fileMeta").textContent = meta;
  $("editorLanguage").textContent = language;
  $("codeEditor").value = state.fileContent;
  $("codeEditor").disabled = false;
  $("codeEditorWrap").classList.toggle("empty", !state.fileContent);
  renderCodeHighlight();
  $("attachFile").disabled = !state.fileContent;
  $("downloadCode").disabled = !state.fileContent;
  updateTokenUsage();
}

function renderCodeHighlight() {
  const code = $("codeEditor").value;
  const visibleCode = code || "Select or generate code to edit it here.";
  const highlight = $("codeHighlight");
  if (highlight) {
    highlight.innerHTML = `<code>${highlightCode(visibleCode, state.generatedInfo)}</code>`;
  }
  syncEditorHighlightScroll();
}

function setCodePreview(title, meta, content, info = "txt") {
  setCodeEditorContent(title, meta, content, info);
}

function syncEditorHighlightScroll() {
  const highlight = $("codeHighlight");
  const editor = $("codeEditor");
  if (!highlight || !editor) return;
  highlight.scrollTop = editor.scrollTop;
  highlight.scrollLeft = editor.scrollLeft;
}

function activateTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-page").forEach((p) => p.classList.remove("active"));
  $(`${name}Tab`)?.classList.add("active");
}

function setActiveActivity(label) {
  document.querySelectorAll(".activity-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("aria-label") === label);
  });
}

function renderState(data) {
  state.data = data;
  const ws = data.workspace;
  $("workspacePath").textContent = ws.path;
  if ($("statusModel")) $("statusModel").textContent = data.settings.model || "Model ready";
  $("workspaceInput").value = ws.path;
  $("fileCount").textContent = ws.stats.files;
  $("totalKb").textContent = `${ws.stats.kb}KB`;
  $("typeCount").textContent = ws.stats.types;
  $("connMode").value = data.settings.conn_mode;
  renderModels(data.models || { models: [], error: null }, data.settings.model);
  $("temperature").value = data.settings.temperature;
  $("thinking").checked = !!data.settings.enable_thinking;
  $("autoContinue").checked = !!data.settings.auto_continue;
  $("memoryEnabled").checked = !!data.settings.memory_enabled;
  $("contextTokenBudget").value = data.settings.context_token_budget || 24000;
  $("responseTokenBudget").value = data.settings.response_token_budget || 8192;
  $("tavilyEnabled").checked = !!data.settings.tavily_enabled;
  $("gitApprovalMode").checked = data.settings.git_approval_mode !== false;
  $("tavilyApiKey").value = "";
  updateTavilyPanel(data.settings);
  const memory = data.memory || {};
  const persistentStats = memory.persistent?.stats || {};
  $("memoryStats").textContent = `Memory: ${persistentStats.turns || 0} turns · ${persistentStats.facts || 0} facts · ${persistentStats.preferences || 0} preferences`;
  $("memoryUsageIndicator").textContent = memory.persistent?.retrieval_count
    ? `${memory.persistent.retrieval_count} memory fact(s) used`
    : "";
  state.contextUsage = data.context_usage || {};
  updateTokenUsage();
  $("customApiUrl").value = data.settings.custom_api_url || "https://api.openai.com/v1";
  $("topCustomApiUrl").value = data.settings.custom_api_url || "https://api.openai.com/v1";
  $("customApiKey").value = "";
  $("topCustomApiKey").value = "";
  updateCustomApiPanel();
  $("systemPromptEditor").value = data.settings.system_prompt || "";
  $("selectedPromptName").textContent = data.settings.selected_prompt || "Custom system prompt";
  renderFiles();
  renderSkills();
  renderPrompts();
  renderMemory(data.memory?.persistent || {});
  renderGit(data.git || {});
  renderMessages();
  if (!renderGeneratedArtifactFromState()) {
    renderGeneratedCodeFromMessages();
  }
  if (state.workspaceLocked) setWorkspaceLocked(true);
}

function formatRelativeTime(timestamp) {
  if (!timestamp) return "No activity";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(timestamp)));
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(Number(timestamp) * 1000).toLocaleDateString();
}

function showWorkbench() {
  document.querySelector(".layout").classList.remove("hidden");
  $("projectsView").classList.remove("active");
}

async function showProjects() {
  document.querySelector(".layout").classList.add("hidden");
  $("projectsView").classList.add("active");
  $("projectsIndex").classList.remove("hidden");
  $("projectHome").classList.remove("active");
  const payload = await api("/api/projects");
  state.projectCards = payload.projects || [];
  renderProjectCards();
}

function renderProjectCards() {
  $("projectCards").innerHTML = state.projectCards.map((project) => {
    const dirty = Number(project.git?.files?.length || 0);
    const gitClass = dirty ? "git-dirty" : "git-clean";
    const gitText = project.git?.is_repo ? `${dirty ? `${dirty} changes` : "Git clean"} · ${project.git.commits || 0} commits` : "Git disabled";
    return `<button type="button" class="project-card" data-project-id="${Number(project.id)}">
      <div class="project-card-head"><span class="project-card-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg></span><span class="project-card-title"><strong>${escapeHtml(project.name)}</strong><code>${escapeHtml(project.workspace_path)}</code></span></div>
      <div class="project-card-badges"><span class="project-badge ${gitClass}">${escapeHtml(gitText)}</span><span class="project-badge ${project.agent_status === "running" ? "running" : ""}">${escapeHtml(project.agent_status)}</span></div>
      <div class="project-card-footer"><span>${project.sessions || 0} sessions · ${project.stats?.files || 0} files · ${formatSize(Number(project.stats?.kb || 0) * 1024)}</span><span>${formatRelativeTime(project.last_session_at || project.last_opened_at)}</span></div>
    </button>`;
  }).join("") || `<div class="memory-item-source">No projects yet. Use + New to add a workspace.</div>`;
  document.querySelectorAll(".project-card").forEach((button) => button.addEventListener("click", () => openProjectHome(Number(button.dataset.projectId))));
}

async function openProjectHome(projectId) {
  const archive = await api("/api/memory/archive", { method: "POST", body: JSON.stringify({ project_id: projectId }) });
  const card = state.projectCards.find((item) => Number(item.id) === projectId) || archive.project || {};
  state.projectArchive = { ...archive, card };
  $("projectsIndex").classList.add("hidden");
  $("projectHome").classList.add("active");
  $("projectHomeName").textContent = card.name || "Project";
  $("projectHomePath").textContent = card.workspace_path || "";
  $("projectSettingsPath").textContent = card.workspace_path || "";
  $("projectSettingsGit").textContent = card.git?.is_repo ? `${card.git.branch || "Git"} · ${card.git.files?.length || 0} changes` : "Disabled";
  renderProjectHome();
  activateProjectTab("overview");
}

function renderProjectHome() {
  const { card, sessions = [], files = [], facts = [], preferences = {} } = state.projectArchive || {};
  const dirty = Number(card?.git?.files?.length || 0);
  $("projectQuickStats").innerHTML = [
    [card?.stats?.files || 0, "Files"], [formatSize(Number(card?.stats?.kb || 0) * 1024), "Workspace size"],
    [sessions.length, "Sessions"], [facts.length, "Saved facts"],
  ].map(([value, label]) => `<div class="quick-stat"><strong>${escapeHtml(value)}</strong><span>${label}</span></div>`).join("");
  const recent = sessions.slice(0, 5).map((session) => `<div class="activity-row"><i class="activity-dot"></i><div><strong>${escapeHtml(session.title)}</strong><span>${session.turns} turns · session</span></div><span>${formatRelativeTime(session.updated_at)}</span></div>`);
  if (card?.git?.is_repo) recent.unshift(`<div class="activity-row"><i class="activity-dot"></i><div><strong>${dirty ? `${dirty} uncommitted changes` : "Working tree clean"}</strong><span>${escapeHtml(card.git.branch || "Git")}</span></div><span>${card.git.commits || 0} commits</span></div>`);
  $("projectRecentActivity").innerHTML = recent.join("") || `<div class="memory-item-source">No recent activity.</div>`;
  const pct = Number(state.contextUsage?.percent || 0);
  $("projectContextFill").style.width = `${Math.min(100, pct)}%`;
  $("projectContextText").textContent = `${pct}%`;
  $("projectSessionList").innerHTML = sessions.map((session) => `<button type="button" class="project-session-row" data-session-id="${escapeHtml(session.id)}"><i class="activity-dot"></i><div><strong>${escapeHtml(session.title)}</strong><span>${session.turns} turns · ${escapeHtml(state.data?.settings?.model || "model")}</span></div><span>${formatRelativeTime(session.updated_at)}</span></button>`).join("") || `<div class="memory-item-source">No sessions for this project.</div>`;
  $("projectFileGrid").innerHTML = files.map((file) => `<button type="button" class="project-file-row" data-path="${escapeHtml(file.path)}"><span>${escapeHtml(file.path)}</span><small>${formatSize(file.size)}</small></button>`).join("") || `<div class="memory-item-source">No files found.</div>`;
  $("projectMemoryContent").innerHTML = `<section class="project-memory-group"><h2>Semantic Facts</h2><ul>${facts.map((fact) => `<li>${escapeHtml(fact.fact)}</li>`).join("") || "<li>No saved facts</li>"}</ul></section><section class="project-memory-group"><h2>Preferences</h2><ul>${Object.entries(preferences).map(([key, value]) => `<li><strong>${escapeHtml(key)}</strong>: ${escapeHtml(value)}</li>`).join("") || "<li>No saved preferences</li>"}</ul></section>`;
  $("projectSkillsContent").innerHTML = (state.data?.skills || []).map((skill) => `<div class="project-skill"><strong>/${escapeHtml(skill.name)}</strong><span>${escapeHtml(skill.description || skill.category)}</span></div>`).join("");
  document.querySelectorAll(".project-session-row").forEach((button) => button.addEventListener("click", () => resumeProjectSession(button.dataset.sessionId)));
  document.querySelectorAll(".project-file-row").forEach((button) => button.addEventListener("click", async () => { await openProjectInEditor(false); await loadFile(button.dataset.path); }));
}

function activateProjectTab(name) {
  document.querySelectorAll(".project-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.projectTab === name));
  document.querySelectorAll(".project-page").forEach((page) => page.classList.remove("active"));
  $(`project${name[0].toUpperCase()}${name.slice(1)}Panel`)?.classList.add("active");
}

async function openProjectInEditor(startSession = false) {
  const path = state.projectArchive?.card?.workspace_path;
  if (!path) return;
  const result = await api("/api/workspace", { method: "POST", body: JSON.stringify({ path }) });
  if (!result.ok) throw new Error(result.message || "Could not open project");
  renderState(await api("/api/state"));
  if (startSession) renderState(await api("/api/clear", { method: "POST", body: JSON.stringify({}) }));
  showWorkbench(); setActiveActivity("Agent"); $("promptInput").focus();
}

async function resumeProjectSession(sessionId) {
  renderState(await api("/api/memory/session/resume", { method: "POST", body: JSON.stringify({ session_id: sessionId }) }));
  showWorkbench(); setActiveActivity("Agent");
}

function renderMemory(memory) {
  const stats = memory.stats || {};
  $("persistentMemoryStats").textContent = `${stats.sessions || 0} sessions · ${stats.turns || 0} turns · ${stats.facts || 0} facts · ${stats.preferences || 0} preferences`;
  renderMemoryProjects(memory.projects || []);
  $("memoryFactsList").innerHTML = (memory.facts || []).map((item) => `
    <article class="memory-item" data-memory-id="${Number(item.id)}">
      <textarea class="memory-fact-input" spellcheck="false">${escapeHtml(item.fact)}</textarea>
      <div class="memory-item-actions">
        <button type="button" class="ghost memory-fact-save">Save</button>
        <button type="button" class="ghost memory-fact-delete">Delete</button>
      </div>
      <span class="memory-item-source">${escapeHtml(item.source || "manual")}</span>
    </article>
  `).join("") || `<div class="memory-item-source">No project facts have been saved.</div>`;
  $("memoryPreferencesList").innerHTML = Object.entries(memory.preferences || {}).map(([key, value]) => `
    <article class="memory-item" data-preference-key="${encodeURIComponent(key)}">
      <div><strong>${escapeHtml(key)}</strong><div class="memory-pref-value">${escapeHtml(value)}</div></div>
      <div class="memory-item-actions"><button type="button" class="ghost memory-preference-delete">Delete</button></div>
    </article>
  `).join("") || `<div class="memory-item-source">No preferences have been saved.</div>`;

  document.querySelectorAll(".memory-fact-save").forEach((button) => button.addEventListener("click", async () => {
    const item = button.closest(".memory-item");
    const id = Number(item.dataset.memoryId);
    const existing = (state.data?.memory?.persistent?.facts || []).find((fact) => Number(fact.id) === id);
    const memoryData = await api("/api/memory/fact", { method: "POST", body: JSON.stringify({ id, fact: item.querySelector("textarea").value, source: existing?.source || "manual" }) });
    state.data.memory.persistent = memoryData;
    renderMemory(memoryData);
  }));
  document.querySelectorAll(".memory-fact-delete").forEach((button) => button.addEventListener("click", async () => {
    const id = Number(button.closest(".memory-item").dataset.memoryId);
    const memoryData = await api("/api/memory/fact/delete", { method: "POST", body: JSON.stringify({ id }) });
    state.data.memory.persistent = memoryData;
    renderMemory(memoryData);
  }));
  document.querySelectorAll(".memory-preference-delete").forEach((button) => button.addEventListener("click", async () => {
    const key = decodeURIComponent(button.closest(".memory-item").dataset.preferenceKey);
    const memoryData = await api("/api/memory/preference/delete", { method: "POST", body: JSON.stringify({ key }) });
    state.data.memory.persistent = memoryData;
    renderMemory(memoryData);
  }));
}

function renderMemoryProjects(projects) {
  $("memoryProjectsList").innerHTML = projects.map((project) => `
    <button type="button" class="archive-entry memory-project" data-project-id="${Number(project.id)}">
      <strong>${escapeHtml(project.name)}</strong><span>${Number(project.sessions || 0)} sessions</span>
      <small>${escapeHtml(project.workspace_path)}</small>
    </button>
  `).join("") || `<div class="memory-item-source">No archived projects.</div>`;
  document.querySelectorAll(".memory-project").forEach((button) => button.addEventListener("click", async () => {
    document.querySelectorAll(".memory-project").forEach((item) => item.classList.toggle("selected", item === button));
    const archive = await api("/api/memory/archive", { method: "POST", body: JSON.stringify({ project_id: Number(button.dataset.projectId) }) });
    state.memoryArchiveProjectId = Number(button.dataset.projectId);
    renderMemorySessions(archive.sessions || []);
    renderMemoryArchiveProject(archive);
  }));
}

function renderMemoryArchiveProject(archive) {
  const project = archive.project || {};
  $("memoryArchiveProjectMeta").innerHTML = `<strong>${escapeHtml(project.name || "Unknown project")}</strong><span>${escapeHtml(project.workspace_path || "")}</span>`;
  $("memoryArchiveFiles").innerHTML = (archive.files || []).map((file) => `
    <button type="button" class="archive-file" data-path="${escapeHtml(file.path)}">
      <span>${escapeHtml(file.path)}</span><small>${formatSize(file.size || 0)}</small>
    </button>
  `).join("") || `<div class="memory-item-source">No readable files found at the archived path.</div>`;
  const facts = (archive.facts || []).map((item) => `<li>${escapeHtml(item.fact)}<small>${escapeHtml(item.source || "memory")}</small></li>`).join("");
  const preferences = Object.entries(archive.preferences || {}).map(([key, value]) => `<li><strong>${escapeHtml(key)}</strong>: ${escapeHtml(value)}</li>`).join("");
  $("memoryArchiveKnowledge").innerHTML = `
    <h4>Semantic facts</h4><ul>${facts || "<li>No saved facts</li>"}</ul>
    <h4>Preferences</h4><ul>${preferences || "<li>No saved preferences</li>"}</ul>
  `;
  document.querySelectorAll(".archive-file").forEach((button) => button.addEventListener("click", () => loadArchivedFile(button.dataset.path)));
}

async function loadArchivedFile(path) {
  if (!state.memoryArchiveProjectId) return;
  const data = await api("/api/memory/archive/file", {
    method: "POST",
    body: JSON.stringify({ project_id: state.memoryArchiveProjectId, path }),
  });
  setCodeEditorContent(
    data.path,
    `${data.ext} · archived project · ${formatSize(data.size)} · read-only preview`,
    data.content,
    data.ext || "txt"
  );
}

function renderMemorySessions(sessions) {
  $("memorySessionsList").innerHTML = sessions.map((session) => `
    <button type="button" class="archive-entry memory-session" data-session-id="${escapeHtml(session.id)}">
      <strong>${escapeHtml(session.title || "New session")}</strong><span>${Number(session.turns || 0)} turns</span>
    </button>
  `).join("") || `<div class="memory-item-source">No sessions in this project.</div>`;
  $("memorySessionPreview").textContent = "Select a session to preview its conversation.";
  $("resumeMemorySession").disabled = true;
  document.querySelectorAll(".memory-session").forEach((button) => button.addEventListener("click", async () => {
    document.querySelectorAll(".memory-session").forEach((item) => item.classList.toggle("selected", item === button));
    const archive = await api("/api/memory/archive", { method: "POST", body: JSON.stringify({ session_id: button.dataset.sessionId }) });
    const session = archive.session;
    $("memorySessionPreview").innerHTML = (session?.turns || []).map((turn) => `<div><strong>${escapeHtml(turn.role)}</strong><p>${escapeHtml(turn.content)}</p></div>`).join("") || "This session is empty.";
    $("resumeMemorySession").dataset.sessionId = button.dataset.sessionId;
    $("resumeMemorySession").disabled = false;
  }));
}

async function resumeMemorySession() {
  const sessionId = $("resumeMemorySession").dataset.sessionId;
  if (!sessionId) return;
  renderState(await api("/api/memory/session/resume", { method: "POST", body: JSON.stringify({ session_id: sessionId }) }));
}

async function addMemoryFact() {
  const fact = $("newMemoryFact").value.trim();
  if (!fact) return;
  const memory = await api("/api/memory/fact", { method: "POST", body: JSON.stringify({ fact, source: "manual" }) });
  $("newMemoryFact").value = "";
  state.data.memory.persistent = memory;
  renderMemory(memory);
}

async function addMemoryPreference() {
  const key = $("newPreferenceKey").value.trim();
  const value = $("newPreferenceValue").value.trim();
  if (!key || !value) return;
  const memory = await api("/api/memory/preference", { method: "POST", body: JSON.stringify({ key, value }) });
  $("newPreferenceKey").value = "";
  $("newPreferenceValue").value = "";
  state.data.memory.persistent = memory;
  renderMemory(memory);
}

async function forgetProjectMemory() {
  if (!window.confirm("Forget all conversations, project facts, and preferences stored for this workspace?")) return;
  renderState(await api("/api/memory/forget", { method: "POST", body: JSON.stringify({ confirm: true }) }));
}

function renderGit(git) {
  const notice = $("gitRepoNotice");
  const status = $("gitStatusFiles");
  const history = $("gitHistoryList");
  if (!git.is_repo) {
    notice.innerHTML = `<span>This workspace is not a Git repository.</span><button id="initGitRepo" type="button" class="primary">Initialize Git</button>`;
    status.innerHTML = "";
    history.innerHTML = "";
    $("initGitRepo").addEventListener("click", initGitRepo);
    $("pushGitChanges").disabled = true;
    return;
  }
  if (git.remote) $("gitRemoteUrl").value = git.remote;
  $("pushGitChanges").disabled = !git.remote;
  notice.innerHTML = `<span>Branch: <code>${escapeHtml(git.branch || "HEAD")}</code>${git.clean ? " · clean" : " · uncommitted changes"}</span>`;
  status.textContent = (git.files || []).map((file) => `${file.status}  ${file.path}`).join("\n");
  history.innerHTML = (git.history || []).map((commit) => `
    <article class="git-commit">
      <strong>${escapeHtml(commit.message)}</strong>
      <button type="button" class="ghost git-revert" data-hash="${escapeHtml(commit.hash)}">Revert</button>
      <span>${escapeHtml(commit.short_hash)} · ${escapeHtml(commit.author)} · ${escapeHtml(commit.date)}</span>
    </article>
  `).join("") || `<div class="git-repo-notice">No commits yet.</div>`;
  document.querySelectorAll(".git-revert").forEach((button) => {
    button.addEventListener("click", () => revertGitCommit(button.dataset.hash));
  });
}

function gitCredentials() {
  const usePat = $("gitAuthMode").value === "pat";
  return {
    username: usePat ? $("gitUsername").value.trim() : "",
    token: usePat ? $("gitToken").value : "",
  };
}

function updateGitAuthPanel() {
  $("gitAuthMode").parentElement.parentElement.classList.toggle("show-pat", $("gitAuthMode").value === "pat");
}

function appendGitTerminal(line) {
  const terminal = $("gitCloneTerminal");
  terminal.classList.add("active");
  terminal.textContent += `${line}\n`;
  terminal.scrollTop = terminal.scrollHeight;
}

async function cloneGitRepository() {
  const remoteUrl = $("gitRemoteUrl").value.trim();
  if (!remoteUrl) {
    $("gitRemoteUrl").focus();
    return;
  }
  const terminal = $("gitCloneTerminal");
  terminal.textContent = "";
  setLoading(true, "Cloning repository...", true);
  $("cloneGitRepo").disabled = true;
  try {
    const response = await fetch("/api/git/clone_stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        remote_url: remoteUrl,
        destination: $("gitCloneDestination").value.trim(),
        ...gitCredentials(),
      }),
    });
    if (!response.ok || !response.body) {
      const error = await response.json().catch(() => ({ error: "Clone request failed" }));
      throw new Error(error.error || "Clone request failed");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "git_clone_output") appendGitTerminal(event.content);
        else if (event.type === "git_clone_status") appendGitTerminal(event.message);
        else if (event.type === "git_clone_error") throw new Error(event.message);
        else if (event.type === "git_clone_done") {
          appendGitTerminal(`Clone completed: ${event.path}`);
          renderState(event.state);
          showEditorView("git");
        }
      }
    }
  } catch (error) {
    appendGitTerminal(`ERROR: ${error.message}`);
  } finally {
    $("gitToken").value = "";
    $("cloneGitRepo").disabled = false;
    setLoading(false);
  }
}

async function previewGitPush() {
  const preview = await api("/api/git/push-preview", { method: "POST", body: JSON.stringify({}) });
  const commitLines = (preview.commits || []).map((commit) => `${commit.hash}  ${commit.message}`);
  state.pendingPush = { ...gitCredentials(), preview };
  state.pendingApprovalType = "git-push";
  $("approvalTitle").textContent = "Push Changes to Remote";
  $("approvalToolName").textContent = `git push origin ${preview.branch}`;
  $("approvalWorkspace").textContent = preview.remote;
  $("approvalPreviewLabel").textContent = `${preview.ahead || commitLines.length} commit(s) will be pushed`;
  $("approvalPreview").classList.remove("diff-preview");
  $("approvalPreview").textContent = commitLines.join("\n") || "The current branch will be pushed to its remote.";
  $("approvalAlways").parentElement.style.display = "none";
  $("approvalApprove").textContent = "Approve and push";
  $("approvalModal").style.display = "grid";
}

async function initGitRepo() {
  if (!window.confirm("Initialize a Git repository in this workspace?")) return;
  const git = await api("/api/git/init", { method: "POST", body: JSON.stringify({}) });
  state.data.git = git;
  renderGit(git);
}

async function revertGitCommit(hash) {
  if (!window.confirm(`Create a revert commit for ${hash.slice(0, 8)}?`)) return;
  const result = await api(`/api/git/revert/${encodeURIComponent(hash)}`, { method: "POST", body: JSON.stringify({}) });
  state.data.git = result.git;
  renderGit(result.git);
  await refresh();
}

function showEditorView(name) {
  const isGit = name === "git";
  $("codeEditorWrap").classList.toggle("active", !isGit);
  $("gitHistoryView").classList.toggle("active", isGit);
  $("showCodeTab").classList.toggle("active", !isGit);
  $("showGitTab").classList.toggle("active", isGit);
}

function renderDiffPreview(diff) {
  return String(diff || "").split("\n").map((line) => {
    let kind = "";
    if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@")) kind = "meta";
    else if (line.startsWith("+")) kind = "add";
    else if (line.startsWith("-")) kind = "remove";
    return `<span class="diff-line ${kind}">${escapeHtml(line) || " "}</span>`;
  }).join("");
}

function showApproval(event, isGitDiff = false) {
  state.pendingApprovalType = isGitDiff ? "git" : "tool";
  $("approvalTitle").textContent = isGitDiff ? "Review File Change" : "Execution Approval Required";
  $("approvalToolName").textContent = event.name || "tool";
  $("approvalWorkspace").textContent = state.data?.workspace?.path || "";
  $("approvalPreviewLabel").textContent = isGitDiff ? "Proposed file diff" : "Command / Code preview";
  $("approvalPreview").classList.toggle("diff-preview", isGitDiff);
  if (isGitDiff) $("approvalPreview").innerHTML = renderDiffPreview(event.preview);
  else $("approvalPreview").textContent = event.preview || "";
  $("approvalAlways").checked = false;
  $("approvalAlways").parentElement.style.display = "flex";
  $("approvalApprove").textContent = "Approve and run";
  $("approvalModal").style.display = "grid";
}

async function resolveApproval(approved) {
  if (state.pendingApprovalType === "git-push") {
    try {
      if (approved) {
        const result = await api("/api/git/push", {
          method: "POST",
          body: JSON.stringify({ approved: true, username: state.pendingPush?.username || "", token: state.pendingPush?.token || "" }),
        });
        state.data.git = result.git;
        renderGit(result.git);
        $("gitConnectionStatus").textContent = result.message || "Push completed";
      }
    } finally {
      $("gitToken").value = "";
      state.pendingPush = null;
      state.pendingApprovalType = null;
      $("approvalModal").style.display = "none";
    }
    return;
  }
  const prefix = state.pendingApprovalType === "git" ? "/api/git" : "/api/approval";
  const endpoint = approved ? "approve" : "reject";
  await api(`${prefix}/${endpoint}`, {
    method: "POST",
    body: JSON.stringify(approved ? { always_allow_for_session: $("approvalAlways").checked } : { reason: "Rejected by user" }),
  });
  $("approvalModal").style.display = "none";
  state.pendingApprovalType = null;
}

function estimateTextTokens(text) {
  return Math.max(0, Math.ceil(String(text || "").length / 4));
}

function updateTokenUsage() {
  renderTokenUsage(state.contextUsage || {});
}

function renderTokenUsage(usage) {
  const activeContextTokens = state.activeFile && state.fileContent ? estimateTextTokens(state.fileContent) : 0;
  const draftPromptTokens = $("promptInput") ? estimateTextTokens($("promptInput").value) : 0;
  const extraTokens = activeContextTokens + draftPromptTokens;
  const inputTokens = Number(usage.input_tokens || 0) + extraTokens;
  const settingsBudget = Number(state.data?.settings?.context_token_budget || 0);
  const effectiveWindow = Number(usage.effective_window || usage.context_window || settingsBudget || 0);
  const contextWindow = Number(usage.context_window || effectiveWindow);
  const configuredBudget = Number(usage.configured_budget || settingsBudget || effectiveWindow);
  const percent = effectiveWindow ? Math.min(999, Math.round((inputTokens / effectiveWindow) * 100)) : 0;
  const fillPercent = Math.max(0, Math.min(100, percent));
  const meter = $("tokenMeter");
  meter.classList.toggle("warn", percent >= 70 && percent < 90);
  meter.classList.toggle("danger", percent >= 90);
  $("tokenPercent").textContent = `${percent}%`;
  $("tokenBarFill").style.width = `${fillPercent}%`;
  $("tokenDetails").textContent = `${formatTokens(inputTokens)} / ${formatTokens(effectiveWindow)} tokens`;

  const source = usage.window_source || "estimated";
  const counter = usage.counter || "estimated";
  const remaining = Math.max(0, effectiveWindow - inputTokens);
  meter.title = [
    `Input tokens: ${inputTokens.toLocaleString()}`,
    `Base context tokens: ${Number(usage.input_tokens || 0).toLocaleString()}`,
    `Active editor estimate: ${activeContextTokens.toLocaleString()}`,
    `Draft prompt estimate: ${draftPromptTokens.toLocaleString()}`,
    `Effective context budget: ${effectiveWindow.toLocaleString()}`,
    `Model context window: ${contextWindow.toLocaleString()} (${source})`,
    `Configured context budget: ${configuredBudget.toLocaleString()}`,
    `Reserved response tokens: ${Number(usage.response_budget || 0).toLocaleString()}`,
    `Remaining input tokens: ${remaining.toLocaleString()}`,
    `Counter: ${counter}`,
  ].join("\n");
}

function renderModels(modelPayload, activeModel) {
  const models = modelPayload.models || [];
  const selectedModel = modelPayload.selected_model || activeModel;
  const select = $("modelSelect");
  const options = models.length ? models : [selectedModel || "llama3"];
  select.innerHTML = options.map((name) => `
    <option value="${escapeHtml(name)}" ${name === selectedModel ? "selected" : ""}>${escapeHtml(name)}</option>
  `).join("");
  select.title = modelPayload.error ? `Model refresh error: ${modelPayload.error}` : "Model";
}

function renderFiles() {
  const query = $("fileSearch").value.toLowerCase();
  const files = (state.data?.workspace.files || []).filter((f) =>
    f.path.toLowerCase().includes(query)
  );
  $("fileList").innerHTML = files.map((file) => `
    <button class="file-item ${state.activeFile === file.path ? "active" : ""}" data-path="${escapeHtml(file.path)}">
      ${escapeHtml(file.path)}
    </button>
  `).join("");
  document.querySelectorAll(".file-item").forEach((btn) => {
    btn.addEventListener("click", () => loadFile(btn.dataset.path));
  });
}

function renderSkills() {
  const selected = new Set(state.data?.selected_skills || []);
  $("skillsList").innerHTML = (state.data?.skills || []).map((skill) => `
    <button class="skill-item ${selected.has(skill.name) ? "selected" : ""}" data-skill="${escapeHtml(skill.name)}" ${skill.disabled ? "disabled" : ""}>
      /${escapeHtml(skill.name)}
      <span class="skill-desc">${escapeHtml(skill.description || skill.category)}</span>
    </button>
  `).join("");
  document.querySelectorAll(".skill-item").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const next = new Set(state.data.selected_skills || []);
      if (next.has(btn.dataset.skill)) next.delete(btn.dataset.skill);
      else next.add(btn.dataset.skill);
      renderState(await api("/api/skills", {
        method: "POST",
        body: JSON.stringify({ selected_skills: Array.from(next) }),
      }));
    });
  });
}

function renderPrompts() {
  const query = $("promptSearch").value.toLowerCase();
  const selected = state.data?.settings.selected_prompt;
  const prompts = (state.data?.prompts?.prompts || []).filter((prompt) =>
    prompt.name.toLowerCase().includes(query) ||
    prompt.category.toLowerCase().includes(query) ||
    prompt.preview.toLowerCase().includes(query)
  );
  $("promptsList").innerHTML = prompts.map((prompt) => `
    <button class="prompt-item ${selected === prompt.name ? "selected" : ""}" data-prompt="${escapeHtml(prompt.name)}">
      ${escapeHtml(prompt.name)}
      <span class="prompt-meta">${escapeHtml(prompt.category)} · ${Math.floor(prompt.size / 1000)}k · ${escapeHtml(prompt.preview)}</span>
    </button>
  `).join("");
  document.querySelectorAll(".prompt-item").forEach((btn) => {
    btn.addEventListener("click", async () => selectPrompt(btn.dataset.prompt));
  });
}

function renderMessages() {
  const messages = state.data?.messages || [];
  const toolsLog = state.data?.tools_log || [];
  $("messages").innerHTML = messages.map((msg, index) => {
    const assistantIndex = messages.slice(0, index + 1).filter((m) => m.role === "assistant").length - 1;
    const tools = msg.role === "assistant" && toolsLog[assistantIndex] ? toolsLog[assistantIndex] : [];
    const toolHtml = tools.length ? `<div class="tool-box">${escapeHtml(tools.map((t) =>
      `${t.name}(${JSON.stringify(t.args || {})})\n${String(t.result || "").slice(0, 1200)}`
    ).join("\n\n"))}</div>` : "";
    return `
      <article class="message ${msg.role}">
        <span class="role">${msg.role}</span>
        ${renderMessageContent(msg)}
        ${toolHtml}
      </article>
    `;
  }).join("");
  $("messages").scrollTop = $("messages").scrollHeight;
}

function renderMessageContent(msg) {
  const content = msg.content || "";
  if (msg.role !== "assistant") return escapeHtml(content);
  const blocks = extractCodeBlocks(content);
  if (!blocks.length) return escapeHtml(content);
  const prose = content.replace(/```([^\n`]*)\n([\s\S]*?)```/g, "").trim();
  const note = `<div class="tool-box">Code artifact updated in Editor · ${blocks.length} block${blocks.length > 1 ? "s" : ""}</div>`;
  return `${escapeHtml(prose)}${prose ? "\n" : ""}${note}`;
}

function extractCodeBlocks(text, includeOpenBlock = false) {
  const blocks = [];
  const re = /```([^\n`]*)\n([\s\S]*?)```/g;
  let match;
  let lastEnd = 0;
  while ((match = re.exec(text || "")) !== null) {
    const info = (match[1] || "text").trim();
    const code = (match[2] || "").trimEnd();
    lastEnd = re.lastIndex;
    if (code.trim()) {
      blocks.push({ info, code });
    }
  }
  if (includeOpenBlock) {
    const remaining = String(text || "").slice(lastEnd);
    const open = remaining.match(/```([^\n`]*)\n([\s\S]*)$/);
    if (open) {
      const info = (open[1] || "text").trim();
      const code = (open[2] || "").trimEnd();
      if (code.trim()) {
        blocks.push({ info, code });
      }
    }
  }
  return blocks;
}

function pickBestCodeBlock(blocks) {
  if (!blocks.length) return null;
  return blocks.reduce((best, block) => (
    block.code.length >= best.code.length ? block : best
  ), blocks[0]);
}

function looksLikeCode(text) {
  const value = String(text || "").trim();
  if (!value) return false;
  const codeMarkers = [
    /(^|\n)\s*(import|from|def|class|function|const|let|var|return|async|await)\b/,
    /(^|\n)\s*(if|for|while|try|catch|switch)\s*[\(\w]/,
    /<\/?[a-z][\s\S]*>/i,
    /[{;}]\s*$/,
    /(^|\n)\s*#include\b/,
    /(^|\n)\s*(public|private|protected)\s+(class|static|void|int|string)\b/i,
  ];
  return codeMarkers.some((pattern) => pattern.test(value));
}

function inferCodeBlockFromText(text) {
  const value = String(text || "").trim();
  if (!looksLikeCode(value)) return null;
  let info = "txt";
  if (/(^|\n)\s*(from|import|def|class)\b/.test(value)) info = "python";
  else if (/<\/?[a-z][\s\S]*>/i.test(value)) info = "html";
  else if (/(^|\n)\s*(function|const|let|var|export|import)\b/.test(value)) info = "javascript";
  else if (/(^|\n)\s*(public|private|protected)\s+(class|static|void|int|string)\b/i.test(value)) info = "java";
  else if (/(^|\n)\s*[.#]?[a-z0-9_-]+\s*\{/.test(value)) info = "css";
  return { info, code: value };
}

function setGeneratedCodeFromText(text, metaPrefix = "extracted from latest agent response", force = false) {
  if (!force && state.activeFile && state.activeFile !== "Generated Code") return false;
  if (state.editorDirty) return false;
  const blocks = extractCodeBlocks(text, true);
  const block = blocks.length ? pickBestCodeBlock(blocks) : (force ? inferCodeBlockFromText(text) : null);
  if (!block) return false;
  setCodePreview(
    "Generated Code",
    `${block.info || "text"} · ${block.code.length.toLocaleString()} chars · ${metaPrefix}`,
    block.code,
    block.info || "txt"
  );
  return true;
}

function renderGeneratedArtifactFromState(force = false) {
  const artifact = state.data?.generated_artifact;
  if (!artifact?.content) return false;
  if (!force && state.activeFile && state.activeFile !== "Generated Code") return false;
  if (state.editorDirty) return false;
  setCodePreview(
    artifact.title || "Generated Code",
    `${artifact.info || "text"} · ${artifact.content.length.toLocaleString()} chars · ${artifact.source || "agent artifact"}`,
    artifact.content,
    artifact.info || "txt"
  );
  return true;
}

function renderGeneratedCodeFromMessages(force = false) {
  if (!force && state.activeFile && state.activeFile !== "Generated Code") return;
  if (state.editorDirty) return;
  const messages = state.data?.messages || [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role !== "assistant") continue;
    if (setGeneratedCodeFromText(messages[i].content, "extracted from latest agent response", force)) return;
  }
}

function setWorkspaceLocked(isLocked) {
  state.workspaceLocked = Boolean(isLocked);
  document.body.classList.toggle("workspace-locked", state.workspaceLocked);
  document.querySelectorAll(".workspace-panel button, .workspace-panel input, .workspace-panel select, .workspace-panel textarea")
    .forEach((control) => {
      if (state.workspaceLocked) {
        if (!("lockPrevDisabled" in control.dataset)) {
          control.dataset.lockPrevDisabled = control.disabled ? "1" : "0";
        }
        control.disabled = true;
      } else {
        control.disabled = control.dataset.lockPrevDisabled === "1";
        delete control.dataset.lockPrevDisabled;
      }
    });
}

function setLoading(isLoading, text = "Working...", lockWorkspace = state.workspaceLocked) {
  document.body.classList.toggle("loading", isLoading);
  $("loadingText").textContent = text;
  setWorkspaceLocked(isLoading ? lockWorkspace : false);
}

function appendStreamingAssistant() {
  const article = document.createElement("article");
  article.className = "message assistant streaming";
  article.innerHTML = `<span class="role">assistant</span><span class="stream-content"></span>`;
  $("messages").appendChild(article);
  $("messages").scrollTop = $("messages").scrollHeight;
  return article.querySelector(".stream-content");
}

function appendToolStatus(name, text) {
  const article = document.createElement("article");
  article.className = "message assistant";
  article.innerHTML = `<span class="role">tool</span><div class="tool-box">${escapeHtml(`${name}\n${text}`)}</div>`;
  $("messages").appendChild(article);
  $("messages").scrollTop = $("messages").scrollHeight;
}

function updateGeneratedCodeFromStreaming(text) {
  setGeneratedCodeFromText(text, "live stream", true);
}

async function loadFile(path) {
  const data = await api(`/api/file?path=${encodeURIComponent(path)}`);
  setCodeEditorContent(
    path,
    `${data.ext} · ${formatSize(data.size)} · ${data.content.length.toLocaleString()} chars · editable`,
    data.content,
    data.ext || "txt"
  );
  renderFiles();
}

function downloadCurrentCode() {
  if (!state.fileContent) return;
  const ext = extensionForInfo(state.generatedInfo);
  const baseName = state.activeFile && state.activeFile !== "Generated Code"
    ? state.activeFile.split(/[\\/]/).pop()
    : `generated-code.${ext}`;
  const filename = baseName.includes(".") ? baseName : `${baseName}.${ext}`;
  const blob = new Blob([state.fileContent], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function currentActiveContext() {
  if (!state.activeFile || !state.fileContent) return null;
  return {
    path: state.activeFile,
    content: state.fileContent,
    info: state.generatedInfo || "txt",
  };
}

async function refresh() {
  renderState(await api("/api/state"));
  await showProjects();
}

async function openWorkspace() {
  const data = await api("/api/workspace", {
    method: "POST",
    body: JSON.stringify({ path: $("workspaceInput").value.trim() }),
  });
  renderState(data);
}

async function browseWorkspace() {
  setLoading(true, "Opening folder picker...");
  try {
    const data = await api("/api/browse", {
      method: "POST",
      body: JSON.stringify({ initial_dir: $("workspaceInput").value.trim() }),
    });
    if (data.cancelled) return;
    $("workspaceInput").value = data.path || data.workspace?.path || $("workspaceInput").value;
    renderState(await api("/api/state"));
  } catch (err) {
    alert(err.message || "Could not open the folder picker");
  } finally {
    setLoading(false);
  }
}

function updateCustomApiPanel() {
  $("customApiPanel").classList.toggle("open", $("connMode").value === "🔑 Custom API");
}

function updateTavilyPanel(settings = state.data?.settings || {}) {
  const enabled = $("tavilyEnabled").checked;
  $("tavilyKeyRow").classList.toggle("disabled-row", !enabled);
  $("tavilyApiKey").disabled = !enabled;
  $("tavilyStatus").textContent = enabled
    ? (settings.tavily_key_set ? "Tavily is enabled · key saved" : "Tavily is enabled · add API key")
    : "Tavily is disabled";
}

async function saveSettings() {
  const apiUrl = $("topCustomApiUrl").value.trim() || $("customApiUrl").value.trim() || "https://api.openai.com/v1";
  const apiKey = $("topCustomApiKey").value || $("customApiKey").value;
  renderState(await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      conn_mode: $("connMode").value,
      model: $("modelSelect").value,
      temperature: Number($("temperature").value),
      enable_thinking: $("thinking").checked,
      auto_continue: $("autoContinue").checked,
      memory_enabled: $("memoryEnabled").checked,
      context_token_budget: Number($("contextTokenBudget").value),
      response_token_budget: Number($("responseTokenBudget").value),
      tavily_enabled: $("tavilyEnabled").checked,
      tavily_api_key: $("tavilyApiKey").value,
      git_approval_mode: $("gitApprovalMode").checked,
      custom_api_url: apiUrl,
      custom_api_key: apiKey,
    }),
  }));
}

async function compactMemory() {
  renderState(await api("/api/memory/compact", {
    method: "POST",
    body: JSON.stringify({}),
  }));
}

async function testSkills() {
  $("skillsStatus").textContent = "Testing...";
  const result = await api("/api/skills/diagnostics");
  const problems = []
    .concat(result.duplicates?.map((name) => `duplicate:${name}`) || [])
    .concat(result.missing_description?.map((name) => `missing desc:${name}`) || [])
    .concat(result.failed_slash_detection?.map((name) => `slash failed:${name}`) || [])
    .concat(result.selected_missing?.map((name) => `missing selected:${name}`) || []);
  $("skillsStatus").textContent = result.ok
    ? `OK · ${result.count} skills · auto prompt ${result.auto_select_prompt_chars} chars`
    : `Issues · ${problems.slice(0, 3).join(", ")}${problems.length > 3 ? "..." : ""}`;
}

async function refreshModels() {
  await saveSettings();
  renderState(await api("/api/state"));
}

async function selectPrompt(name) {
  const prompt = await api(`/api/prompt?name=${encodeURIComponent(name)}`);
  $("systemPromptEditor").value = prompt.content;
  $("selectedPromptName").textContent = prompt.name;
  renderState(await api("/api/prompt", {
    method: "POST",
    body: JSON.stringify({ selected_prompt: prompt.name }),
  }));
}

async function savePrompt() {
  renderState(await api("/api/prompt", {
    method: "POST",
    body: JSON.stringify({ system_prompt: $("systemPromptEditor").value }),
  }));
}

async function scanProject() {
  setLoading(true, "Scanning project...", true);
  try {
    const data = await api("/api/scan", { method: "POST", body: JSON.stringify({ max_files: 250 }) });
    renderState(data.state);
  } finally {
    setLoading(false);
  }
}

async function sendPrompt(prompt) {
  setLoading(true, "Sending prompt...", true);
  let streamTarget = appendStreamingAssistant();
  streamTarget.textContent = "Preparing request...";
  let streamText = "";
  let sawDone = false;
  const activeContext = currentActiveContext();
  state.editorDirty = false;
  const ensureStreamTarget = (placeholder = "Working...") => {
    if (!streamTarget || !streamTarget.isConnected) {
      streamTarget = appendStreamingAssistant();
    }
    if (!streamText && placeholder) {
      streamTarget.textContent = placeholder;
    }
    return streamTarget;
  };
  const showStreamNotice = (message) => {
    ensureStreamTarget("");
    if (streamText) {
      streamText += `\n\n${message}`;
      streamTarget.textContent = streamText;
    } else {
      streamTarget.textContent = message;
    }
    $("messages").scrollTop = $("messages").scrollHeight;
  };
  const refreshEditorAfterDone = async () => {
    if (activeContext?.path && activeContext.path !== "Generated Code") {
      try {
        await loadFile(activeContext.path);
      } catch {
        renderGeneratedArtifactFromState(true);
      }
      return;
    }
    if (!renderGeneratedArtifactFromState(true) && !setGeneratedCodeFromText(streamText, "final stream output", true)) {
      renderGeneratedCodeFromMessages(true);
    }
  };
  try {
    const res = await fetch("/api/chat_stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, active_context: activeContext }),
    });
    if (!res.ok || !res.body) {
      const fallback = await res.json().catch(() => ({ error: "Streaming request failed" }));
      throw new Error(fallback.error || "Streaming request failed");
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        let event;
        try {
          event = JSON.parse(line);
        } catch (err) {
          showStreamNotice(`[stream parse error] ${err.message}`);
          continue;
        }
        if (event.type === "state") {
          renderState(event.state);
          ensureStreamTarget("Preparing context...");
        } else if (event.type === "status") {
          setLoading(true, event.message);
          ensureStreamTarget(event.message || "Working...");
        } else if (event.type === "token") {
          ensureStreamTarget("");
          streamText += event.content || "";
          streamTarget.textContent = streamText;
          updateGeneratedCodeFromStreaming(streamText);
          $("messages").scrollTop = $("messages").scrollHeight;
        } else if (event.type === "tool_call") {
          setLoading(true, `Running ${event.name}...`);
          appendToolStatus(event.name, JSON.stringify(event.args || {}, null, 2));
        } else if (event.type === "tool_result") {
          appendToolStatus(`${event.name} result`, String(event.result || "").slice(0, 1200));
        } else if (event.type === "approval_required") {
          setLoading(true, `Waiting for approval: ${event.name}`);
          showApproval(event, false);
        } else if (event.type === "git_diff_preview") {
          setLoading(true, `Reviewing changes to ${event.args?.path || "file"}`);
          showApproval(event, true);
          showEditorView("git");
        } else if (event.type === "git_commit_created") {
          appendToolStatus("Git checkpoint", `${event.commit.slice(0, 8)} ${event.message}`);
        } else if (event.type === "git_checkpoint_created") {
          appendToolStatus("Session checkpoint", `Created branch ${event.branch}`);
        } else if (event.type === "memory_used") {
          $("memoryUsageIndicator").textContent = `${event.count} memory fact(s) used`;
        } else if (event.type === "error") {
          showStreamNotice(event.message || "Error");
        } else if (event.type === "done") {
          sawDone = true;
          renderState(event.state);
          await refreshEditorAfterDone();
        }
      }
    }
    if (buffer.trim()) {
      try {
        const event = JSON.parse(buffer.trim());
        if (event.type === "done") {
          sawDone = true;
          renderState(event.state);
          await refreshEditorAfterDone();
        } else if (event.type === "error") {
          showStreamNotice(event.message || "Error");
        }
      } catch (err) {
        showStreamNotice(`[stream ended with partial data] ${err.message}`);
      }
    }
    if (!sawDone) {
      showStreamNotice("[stream ended before the agent sent a final state]");
    }
  } catch (err) {
    showStreamNotice(`Agent stream error: ${err.message}`);
  } finally {
    setLoading(false);
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    activateTab(tab.dataset.tab);
    setActiveActivity(tab.dataset.tab === "settings" ? "Settings" : "Search");
  });
});

document.querySelectorAll(".activity-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const label = btn.getAttribute("aria-label");
    setActiveActivity(label);
    if (label === "Projects") {
      showProjects();
    } else if (label === "Search") {
      showWorkbench();
      activateTab("files");
      $("fileSearch").focus();
    } else if (label === "Agent") {
      showWorkbench();
      $("promptInput").focus();
    } else if (label === "Settings") {
      showWorkbench();
      activateTab("settings");
    } else if (label === "Memory") {
      showWorkbench();
      activateTab("memory");
    }
  });
});

document.querySelectorAll(".project-tab").forEach((tab) => tab.addEventListener("click", () => activateProjectTab(tab.dataset.projectTab)));
$("backToProjects").addEventListener("click", () => { $("projectHome").classList.remove("active"); $("projectsIndex").classList.remove("hidden"); });
$("openProjectInEditor").addEventListener("click", () => openProjectInEditor(false));
$("startProjectSession").addEventListener("click", () => openProjectInEditor(true));
$("newProjectSession").addEventListener("click", () => openProjectInEditor(true));
$("newProject").addEventListener("click", async () => { showWorkbench(); setActiveActivity("Search"); await browseWorkspace(); });
$("openRuntimeSettings").addEventListener("click", () => { showWorkbench(); setActiveActivity("Settings"); activateTab("settings"); });

$("fileSearch").addEventListener("input", renderFiles);
$("promptSearch").addEventListener("input", renderPrompts);
$("openWorkspace").addEventListener("click", openWorkspace);
$("browseWorkspace").addEventListener("click", browseWorkspace);
$("saveSettings").addEventListener("click", saveSettings);
$("compactMemory").addEventListener("click", compactMemory);
$("addMemoryFact").addEventListener("click", addMemoryFact);
$("addMemoryPreference").addEventListener("click", addMemoryPreference);
$("forgetProjectMemory").addEventListener("click", forgetProjectMemory);
$("resumeMemorySession").addEventListener("click", resumeMemorySession);
$("testSkills").addEventListener("click", testSkills);
$("tavilyEnabled").addEventListener("change", () => updateTavilyPanel());
$("saveTopCustomApi").addEventListener("click", refreshModels);
$("refreshModels").addEventListener("click", refreshModels);
$("modelSelect").addEventListener("change", saveSettings);
$("connMode").addEventListener("change", () => {
  updateCustomApiPanel();
  refreshModels();
});
$("savePrompt").addEventListener("click", savePrompt);
$("scanBtn").addEventListener("click", scanProject);
$("clearChat").addEventListener("click", async () => renderState(await api("/api/clear", { method: "POST", body: JSON.stringify({}) })));
$("attachFile").addEventListener("click", () => {
  if (!state.activeFile) return;
  $("promptInput").value = `Apply this change to ${state.activeFile}: `;
  $("promptInput").focus();
});
$("downloadCode").addEventListener("click", downloadCurrentCode);
$("showCodeTab").addEventListener("click", () => showEditorView("code"));
$("showGitTab").addEventListener("click", () => showEditorView("git"));
$("gitAuthMode").addEventListener("change", updateGitAuthPanel);
$("cloneGitRepo").addEventListener("click", cloneGitRepository);
$("pushGitChanges").addEventListener("click", () => previewGitPush().catch((error) => alert(error.message)));
$("approvalApprove").addEventListener("click", () => resolveApproval(true));
$("approvalReject").addEventListener("click", () => resolveApproval(false));
$("codeEditor").addEventListener("input", () => {
  state.fileContent = $("codeEditor").value;
  state.editorDirty = true;
  $("downloadCode").disabled = !state.fileContent;
  $("attachFile").disabled = !state.fileContent;
  $("codeEditorWrap").classList.toggle("empty", !state.fileContent);
  renderCodeHighlight();
  updateTokenUsage();
});
$("codeEditor").addEventListener("scroll", () => {
  syncEditorHighlightScroll();
});
$("promptInput").addEventListener("input", updateTokenUsage);
$("chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = $("promptInput").value.trim();
  if (!prompt) return;
  $("promptInput").value = "";
  updateTokenUsage();
  await sendPrompt(prompt);
});

installEditorMetricStyles();
updateGitAuthPanel();
refresh().catch((err) => {
  $("messages").innerHTML = `<article class="message assistant"><span class="role">error</span>${escapeHtml(err.message)}</article>`;
});
