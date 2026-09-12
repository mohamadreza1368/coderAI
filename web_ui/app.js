/* ══════════════════════════════════════════════════════════════════════════
 * CoderAI Web Client - Modular Architecture
 * ══════════════════════════════════════════════════════════════════════════ */

/* ── 1. GLOBAL STATE & SELECTOR HELPERS ─────────────────────────── */
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
  initialProjectsShown: false,
  commandSelection: 0,
};

const $ = (id) => document.getElementById(id);

function installEditorMetricStyles() {
  if (document.getElementById("editorMetricStyles")) return;
  const style = document.createElement("style");
  style.id = "editorMetricStyles";
  style.textContent = `
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
      padding: 16px 16px 16px 58px !important;
      overflow: auto !important;
      color: #e4e4e7 !important;
      background: #181818 !important;
      caret-color: #38bdf8 !important;
      -webkit-text-size-adjust: 100%;
      text-size-adjust: 100%;
    }
    .code-editor::selection {
      background: #264f78 !important;
      color: #ffffff !important;
    }
  `;
  document.head.appendChild(style);
}

function getActiveSessionId() {
  let sid = sessionStorage.getItem("coderai_session_id");
  if (!sid) {
    sid = "sess_" + Math.random().toString(36).slice(2, 10);
    sessionStorage.setItem("coderai_session_id", sid);
  }
  return sid;
}

async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    "X-Session-ID": getActiveSessionId(),
    ...(options.headers || {}),
  };
  const res = await fetch(path, {
    ...options,
    headers,
  });
  let data = {};
  try {
    data = await res.json();
  } catch (_) {}
  if (!res.ok) {
    const errorMsg = data.detail || data.error || data.message || `Request failed (${res.status} ${res.statusText})`;
    throw new Error(errorMsg);
  }
  return data;
}

async function postJson(path, body = {}) {
  return api(path, {
    method: "POST",
    body: JSON.stringify(body),
  });
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

function updateEditorLineNumbers() {
  const editor = $("codeEditor");
  const gutter = $("editorGutter");
  if (!editor || !gutter) return;
  const lines = editor.value ? editor.value.split("\n").length : 1;
  let nums = "";
  for (let i = 1; i <= Math.max(lines, 1); i++) {
    nums += i + "\n";
  }
  gutter.textContent = nums;
  gutter.scrollTop = editor.scrollTop;
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
  updateEditorLineNumbers();
  $("attachFile").disabled = !state.fileContent;
  $("downloadCode").disabled = !state.fileContent;
  updateTokenUsage();
}

function renderCodeHighlight() {
  updateEditorLineNumbers();
}

function setCodePreview(title, meta, content, info = "txt") {
  setCodeEditorContent(title, meta, content, info);
}

function syncEditorHighlightScroll() {
  const gutter = $("editorGutter");
  const editor = $("codeEditor");
  if (!gutter || !editor) return;
  gutter.scrollTop = editor.scrollTop;
}

function activateTab(name) {
  document.body.classList.toggle("utility-mode", name !== "files");
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-page").forEach((p) => p.classList.remove("active"));
  $(`${name}Tab`)?.classList.add("active");
}

function setActiveActivity(label) {
  document.querySelectorAll(".activity-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("aria-label") === label);
  });
}

function renderState(data, updateChat = true) {
  state.data = data;
  const ws = data.workspace;
  $("workspacePath").textContent = ws.path;
  if ($("statusModel")) $("statusModel").textContent = data.settings.model || "Model ready";
  const gitBranch = data.git?.status?.branch || data.git?.branch || "not connected";
  $("statusBranch").textContent = `Git: ${gitBranch}`;
  const index = data.code_index || {};
  $("statusIndex").textContent = !index.files ? "Index: not built" : index.up_to_date === false ? "Index: changes detected" : `Index: ${index.files} files`;
  $("statusModel").textContent = `Model: ${data.settings.model || "not selected"}`;
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
  if (data.settings.policies) {
    renderApprovalPolicies(data.settings.policies);
  }
  $("smartSkillConfirmation").checked = !!data.settings.smart_skill_confirmation;
  $("tavilyApiKey").value = "";
  updateTavilyPanel(data.settings);
  const memory = data.memory || {};
  const persistentStats = memory.persistent?.stats || {};
  $("memoryStats").textContent = `Memory: ${persistentStats.turns || 0} turns · ${persistentStats.facts || 0} facts · ${persistentStats.preferences || 0} preferences`;
  $("memoryUsageIndicator").textContent = memory.persistent?.retrieval_count
    ? `${memory.persistent.retrieval_count} memory fact(s) used`
    : "";
  state.contextUsage = data.context_usage || {};
  $("statusContext").textContent = `Context: ${Number(state.contextUsage.percent || 0)}%`;
  $("customApiUrl").value = data.settings.custom_api_url || "https://api.openai.com/v1";
  $("customApiModel").value = data.settings.custom_api_model || "gpt-4o-mini";
  $("customApiKey").value = "";
  if ($("modalCustomApiUrl") && !$("modalCustomApiUrl").value) $("modalCustomApiUrl").value = data.settings.custom_api_url || "https://api.openai.com/v1";
  if ($("modalCustomApiModel") && !$("modalCustomApiModel").value) $("modalCustomApiModel").value = data.settings.custom_api_model || "gpt-4o-mini";
  if ($("sandboxMode")) $("sandboxMode").value = data.settings.sandbox_mode || "auto";
  if ($("sandboxDockerImage")) $("sandboxDockerImage").value = data.settings.sandbox_docker_image || "python:3.11-slim";
  $("systemPromptEditor").value = data.settings.system_prompt || "";
  $("selectedPromptName").textContent = data.settings.selected_prompt || "Custom system prompt";
  renderFiles();
  renderCodeIndex(data.code_index || {});
  renderSkills();
  renderPrompts();
  renderMemory(data.memory?.persistent || {});
  renderGit(data.git || {});
  if (updateChat) {
    renderMessages();
    if (!renderGeneratedArtifactFromState()) {
      renderGeneratedCodeFromMessages();
    }
  }
  if (state.workspaceLocked) setWorkspaceLocked(true);
}

function friendlyErrorMessage(message) {
  const text = String(message || "");
  const lower = text.toLowerCase();
  if (lower.includes("chromadb") || lower.includes("chroma")) {
    return "Advanced semantic memory is unavailable, so the app is using the local fallback search mode.";
  }
  if (lower.includes("sqlite") && lower.includes("fallback")) {
    return "Local fallback search is active. You can keep working normally; semantic search may be less precise.";
  }
  if (lower.includes("tavily")) {
    return "Web search is not ready. Enable it from Settings when you need web results.";
  }
  if (lower.includes("module not found") || lower.includes("modulenotfounderror")) {
    return "An optional advanced component is missing. The app switched to a simpler local mode.";
  }
  if (lower.includes("timed out") || lower.includes("timeout")) {
    return "The selected model took too long to respond. Check that it is running, then try again.";
  }
  if (lower.includes("connection refused") || lower.includes("failed to fetch") || lower.includes("not reachable")) {
    return "The selected model service is unavailable. Check the connection in Settings and try again.";
  }
  if (lower.includes("traceback") || lower.includes("error:") || lower.includes("exception")) {
    return "The operation could not be completed. Check Advanced Settings or the application logs for technical details.";
  }
  return text || "The operation could not be completed.";
}

const commandDefinitions = [
  { id: "projects", icon: "P", title: "Projects and sessions", detail: "Browse local workspaces and resume previous sessions", keywords: "/session archive history", shortcut: "" },
  { id: "files", icon: "F", title: "Project files", detail: "Open the workspace file browser", keywords: "/file source tree", shortcut: "" },
  { id: "index", icon: "I", title: "Codebase intelligence", detail: "View project overview, index status, and dependency graph", keywords: "/index rag graph architecture", shortcut: "" },
  { id: "agent", icon: "A", title: "Agent chat", detail: "Focus the request composer", keywords: "/agent chat prompt", shortcut: "" },
  { id: "skills", icon: "S", title: "Skills", detail: "Choose skills and inspect usage", keywords: "/skill capability", shortcut: "" },
  { id: "prompts", icon: ">_", title: "System prompts", detail: "Select or edit the active system prompt", keywords: "/prompt system instruction", shortcut: "" },
  { id: "memory", icon: "M", title: "Memory", detail: "Browse sessions, project facts, and preferences", keywords: "/memory fact preference", shortcut: "" },
  { id: "git", icon: "G", title: "Git history", detail: "Review repository status, commits, clone, and push", keywords: "/git diff commit history", shortcut: "" },
  { id: "graph", icon: "🕸️", title: "Code & Architecture Graph", detail: "Interactive Graphify knowledge graph with AST call trees and cluster analysis", keywords: "/graph architecture visualizer code-review-graph ast", shortcut: "" },
  { id: "settings", icon: "CFG", title: "Settings", detail: "Model, runtime, and web search controls", keywords: "/settings model tavily", shortcut: "" },
  { id: "advanced", icon: "...", title: "Advanced settings", detail: "Context, memory, Git review, skills, and Custom API", keywords: "/advanced context api embedding", shortcut: "" },
];

function filteredCommands() {
  const query = $("commandSearch").value.trim().toLowerCase();
  if (!query) return commandDefinitions;
  return commandDefinitions.filter((command) => `${command.title} ${command.detail} ${command.keywords}`.toLowerCase().includes(query));
}

function renderCommandPalette() {
  const commands = filteredCommands();
  state.commandSelection = Math.max(0, Math.min(state.commandSelection, commands.length - 1));
  $("commandList").innerHTML = commands.map((command, index) => `
    <button type="button" class="command-item ${index === state.commandSelection ? "selected" : ""}" data-command-id="${command.id}" role="option" aria-selected="${index === state.commandSelection}">
      <span class="command-item-icon">${escapeHtml(command.icon)}</span>
      <span><strong>${escapeHtml(command.title)}</strong><span>${escapeHtml(command.detail)}</span></span>
      ${command.shortcut ? `<kbd>${escapeHtml(command.shortcut)}</kbd>` : ""}
    </button>
  `).join("") || `<div class="command-empty">No matching commands</div>`;
  document.querySelectorAll(".command-item").forEach((button) => button.addEventListener("click", () => runCommand(button.dataset.commandId)));
}

function openCommandPalette() {
  $("commandPalette").hidden = false;
  $("commandSearch").value = "";
  state.commandSelection = 0;
  renderCommandPalette();
  $("commandSearch").focus();
}

function closeCommandPalette() {
  $("commandPalette").hidden = true;
}

async function runCommand(id) {
  closeCommandPalette();
  if (id === "projects") return showProjects();
  if (id === "graph") return showCodeGraphView();
  showWorkbench();
  if (id === "files") { setActiveActivity("Files"); activateTab("files"); return $("fileSearch").focus(); }
  if (id === "index") { setActiveActivity("Files"); activateTab("files"); return activateFileView("index"); }
  if (id === "agent") { activateTab("files"); return $("promptInput").focus(); }
  if (id === "skills") return activateTab("skills");
  if (id === "prompts") return activateTab("prompts");
  if (id === "memory") return activateTab("memory");
  if (id === "git") { activateTab("files"); setActiveActivity("Git"); return showEditorView("git"); }
  if (id === "settings" || id === "advanced") {
    setActiveActivity("Settings");
    activateTab("settings");
    const advanced = document.querySelector(".advanced-settings");
    if (id === "advanced") advanced.open = true;
  }
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
  const layout = $("mainLayout") || document.querySelector(".layout");
  if (layout) layout.classList.remove("hidden");
  $("projectsView")?.classList.remove("active");
  $("codeGraphView")?.classList.remove("active");
}

async function showProjects() {
  document.body.classList.remove("utility-mode");
  const layout = $("mainLayout") || document.querySelector(".layout");
  if (layout) layout.classList.add("hidden");
  $("projectsView")?.classList.add("active");
  $("projectsIndex")?.classList.remove("hidden");
  $("projectHome")?.classList.remove("active");
  $("codeGraphView")?.classList.remove("active");
  const payload = await api("/api/projects");
  state.projectCards = payload.projects || [];
  renderProjectCards();
}

function showCodeGraphView() {
  document.body.classList.remove("utility-mode");
  const layout = $("mainLayout") || document.querySelector(".layout");
  if (layout) layout.classList.add("hidden");
  $("projectsView")?.classList.remove("active");
  $("codeGraphView")?.classList.add("active");
  setActiveActivity("Graph");

  refreshSchematicGraph();
  setTimeout(() => {
    if (graphifyState.network) {
      graphifyState.network.fit({ animation: false });
    }
  }, 100);
}

async function deleteProject(projectId, projectName) {
  const confirmed = confirm(`Are you sure you want to remove project "${projectName || 'this project'}" from the workspace list?`);
  if (!confirmed) return;
  try {
    const res = await api("/api/project/delete", {
      method: "POST",
      body: JSON.stringify({ project_id: projectId }),
    });
    state.projectCards = res.projects || [];
    renderProjectCards();
    if ($("projectHome").classList.contains("active")) {
      $("projectHome").classList.remove("active");
      $("projectsIndex").classList.remove("hidden");
    }
  } catch (err) {
    alert(`Failed to delete project: ${err.message}`);
  }
}

function renderProjectCards() {
  $("projectCards").innerHTML = state.projectCards.map((project) => {
    const dirty = Number(project.git?.files?.length || 0);
    const gitClass = dirty ? "git-dirty" : "git-clean";
    const gitText = project.git?.is_repo ? `${dirty ? `${dirty} changes` : "Git clean"} · ${project.git.commits || 0} commits` : "Git disabled";
    return `<div class="project-card" data-project-id="${Number(project.id)}" role="button" tabindex="0">
      <div class="project-card-actions">
        <button type="button" class="project-card-delete-btn" data-project-id="${Number(project.id)}" data-project-name="${escapeHtml(project.name)}" title="Remove project">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
        </button>
      </div>
      <div class="project-card-head"><span class="project-card-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg></span><span class="project-card-title"><strong>${escapeHtml(project.name)}</strong><code>${escapeHtml(project.workspace_path)}</code></span></div>
      <div class="project-card-badges"><span class="project-badge ${gitClass}">${escapeHtml(gitText)}</span><span class="project-badge ${project.agent_status === "running" ? "running" : ""}">${escapeHtml(project.agent_status)}</span></div>
      <div class="project-card-footer"><span>${project.sessions || 0} sessions · ${project.stats?.files || 0} files · ${formatSize(Number(project.stats?.kb || 0) * 1024)}</span><span>${formatRelativeTime(project.last_session_at || project.last_opened_at)}</span></div>
    </div>`;
  }).join("") || `<div class="memory-item-source">No projects yet. Use + New to add a workspace.</div>`;

  document.querySelectorAll(".project-card").forEach((card) => {
    card.addEventListener("click", async (e) => {
      if (e.target.closest(".project-card-delete-btn")) return;
      document.querySelectorAll(".project-card").forEach((c) => c.classList.remove("selected"));
      card.classList.add("selected");
      await openProjectHome(Number(card.dataset.projectId));
    });
  });

  document.querySelectorAll(".project-card-delete-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteProject(Number(btn.dataset.projectId), btn.dataset.projectName);
    });
  });
}

async function openProjectHome(projectId) {
  try {
    const archive = await api("/api/memory/archive", { method: "POST", body: JSON.stringify({ project_id: projectId }) });
    const card = state.projectCards.find((item) => Number(item.id) === projectId) || archive.project || {};
    state.projectArchive = { ...archive, card };
    $("projectsIndex").classList.add("hidden");
    $("projectHome").classList.add("active");
    $("projectHomeName").textContent = card.name || "Project";
    $("projectHomePath").textContent = card.workspace_path || "";
    $("projectSettingsPath").textContent = card.workspace_path || "";
    $("projectSettingsGit").textContent = card.git?.is_repo ? `${card.git.branch || "Git"} · ${card.git.files?.length || 0} changes` : "Disabled";
    if ($("deleteProjectBtn")) {
      $("deleteProjectBtn").onclick = () => deleteProject(projectId, card.name);
    }
    renderProjectHome();
    activateProjectTab("overview");
  } catch (err) {
    console.error("Failed to open project home:", err);
    alert(`Could not open project: ${err.message}`);
  }
}

function renderProjectHome() {
  const { card, sessions = [], files = [], facts = [], preferences = {}, skill_usage = {} } = state.projectArchive || {};
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
  $("projectSkillsContent").innerHTML = (skill_usage.skills || []).map((skill) => `<button type="button" class="project-skill ${skill.disabled ? "disabled" : ""}" data-skill-name="${escapeHtml(skill.name)}"><strong>/${escapeHtml(skill.name)}</strong><span>${skill.project_uses} project uses · ${skill.global_uses} global · ${skill.success_rate == null ? "no outcomes" : `${skill.success_rate}% success`}</span><span>${skill.top_keywords?.length ? `Top triggers: ${skill.top_keywords.map(([word]) => escapeHtml(word)).join(", ")}` : "No trigger keywords yet"}</span></button>`).join("");
  renderRoutingNotice("projectSkillsRoutingNotice", skill_usage.routing || {});
  document.querySelectorAll(".project-session-row").forEach((button) => button.addEventListener("click", () => resumeProjectSession(button.dataset.sessionId)));
  document.querySelectorAll(".project-file-row").forEach((button) => button.addEventListener("click", async () => { await openProjectInEditor(false); await loadFile(button.dataset.path); }));
  document.querySelectorAll(".project-skill").forEach((button) => button.addEventListener("click", () => showProjectSkillDetail(button.dataset.skillName)));
}

async function showProjectSkillDetail(name) {
  const source = await api(`/api/skill/source?name=${encodeURIComponent(name)}`);
  const stats = (state.projectArchive?.skill_usage?.skills || []).find((skill) => skill.name === name) || {};
  $("projectSkillDetail").innerHTML = `<div class="project-skill-detail-head"><div><h2>/${escapeHtml(name)}</h2><span>${stats.project_uses || 0} project uses · ${stats.global_uses || 0} global · ${stats.success_rate == null ? "No success data" : `${stats.success_rate}% success`}</span></div><label>Mode <select id="projectSkillMode"><option value="auto">Auto</option><option value="pinned">Pinned</option><option value="off">Off</option></select></label></div><textarea id="projectSkillSource" spellcheck="false"></textarea><div class="project-skill-detail-actions"><code>${escapeHtml(source.path)}</code><button id="saveProjectSkill" type="button" class="primary">Save SKILL.md</button></div>`;
  $("projectSkillSource").value = source.content;
  $("projectSkillMode").value = stats.mode || "auto";
  $("projectSkillMode").addEventListener("change", async () => {
    const updated = await api("/api/skills/mode", { method: "POST", body: JSON.stringify({ skill_name: name, mode: $("projectSkillMode").value, workspace_path: state.projectArchive?.card?.workspace_path }) });
    state.projectArchive.skill_usage = updated.skill_usage;
    renderProjectHome(); showProjectSkillDetail(name);
  });
  $("saveProjectSkill").addEventListener("click", async () => {
    await api("/api/skill/source", { method: "POST", body: JSON.stringify({ name, content: $("projectSkillSource").value }) });
    $("saveProjectSkill").textContent = "Saved";
  });
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
  checkEmbeddingModelStatus();
  activateTab("files");
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
  const toolbar = $("gitToolbar");
  const conflictPanel = $("gitConflictPanel");

  if (!git.is_repo) {
    if (toolbar) toolbar.style.display = "none";
    if (conflictPanel) conflictPanel.style.display = "none";
    notice.innerHTML = `<span>This workspace is not a Git repository.</span><button id="initGitRepo" type="button" class="primary">Initialize Git</button>`;
    status.innerHTML = "";
    history.innerHTML = "";
    $("initGitRepo").addEventListener("click", initGitRepo);
    $("pushGitChanges").disabled = true;
    if ($("gitFetchBtn")) $("gitFetchBtn").disabled = true;
    if ($("gitPullBtn")) $("gitPullBtn").disabled = true;
    return;
  }

  if (toolbar) toolbar.style.display = "flex";
  if (git.remote) $("gitRemoteUrl").value = git.remote;
  $("pushGitChanges").disabled = !git.remote;
  if ($("gitFetchBtn")) $("gitFetchBtn").disabled = !git.remote;
  if ($("gitPullBtn")) $("gitPullBtn").disabled = !git.remote;

  renderGitBranches(git);
  renderGitConflicts(git);

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

function renderGitBranches(git) {
  const select = $("gitBranchSelect");
  if (!select) return;
  const currentBranch = git.branch || git.branches?.current || "HEAD";
  const local = git.branches?.local || [currentBranch];
  const remote = git.branches?.remote || [];

  let optionsHtml = local.map((b) => `<option value="${escapeHtml(b)}"${b === currentBranch ? " selected" : ""}>${escapeHtml(b)}</option>`).join("");
  if (remote.length > 0) {
    optionsHtml += `<optgroup label="Remote Branches">` +
      remote.map((b) => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`).join("") +
      `</optgroup>`;
  }
  select.innerHTML = optionsHtml;

  const badge = $("gitSyncBadge");
  if (badge) {
    const ahead = git.branches?.ahead ?? git.ahead ?? 0;
    const behind = git.branches?.behind ?? git.behind ?? 0;
    if (ahead > 0 || behind > 0) {
      badge.textContent = `↑ ${ahead} · ↓ ${behind}`;
      badge.title = `${ahead} commit(s) ahead, ${behind} commit(s) behind remote`;
      badge.style.display = "inline-block";
    } else if (git.remote) {
      badge.textContent = "✓ Synced";
      badge.title = "Up to date with remote";
      badge.style.display = "inline-block";
    } else {
      badge.style.display = "none";
    }
  }
}

function renderGitConflicts(git) {
  const panel = $("gitConflictPanel");
  if (!panel) return;
  const inMerge = Boolean(git.in_merge);
  const conflicts = git.conflicts || [];

  if (!inMerge) {
    panel.style.display = "none";
    return;
  }

  panel.style.display = "grid";
  const summaryBadge = $("conflictSummaryBadge");
  if (summaryBadge) {
    summaryBadge.textContent = `${conflicts.length} unmerged file${conflicts.length === 1 ? "" : "s"}`;
  }

  const completeBtn = $("gitCompleteMergeBtn");
  if (completeBtn) {
    completeBtn.disabled = conflicts.length > 0;
  }

  const list = $("gitConflictList");
  if (!list) return;

  if (conflicts.length === 0) {
    list.innerHTML = `<div style="padding:8px 12px;background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.3);border-radius:6px;color:#34d399;font-size:12px;">All conflicts resolved! Click <strong>Complete Merge</strong> above to finalize the merge commit.</div>`;
    return;
  }

  list.innerHTML = conflicts.map((c) => {
    const hunks = c.hunks || [];
    const hunksHtml = hunks.map((h, i) => `
      <div class="conflict-hunk-box">
        <div class="conflict-hunk-header ours">
          <span>Ours: ${escapeHtml(h.ours_label || "Current HEAD")}</span>
          <span>Hunk #${i + 1}</span>
        </div>
        <pre class="conflict-hunk-content">${escapeHtml(h.ours || "(empty)")}</pre>
        <div class="conflict-hunk-header theirs">
          <span>Theirs: ${escapeHtml(h.theirs_label || "Incoming change")}</span>
        </div>
        <pre class="conflict-hunk-content">${escapeHtml(h.theirs || "(empty)")}</pre>
      </div>
    `).join("");

    return `
      <div class="conflict-card" data-path="${escapeHtml(c.path)}">
        <div class="conflict-card-head">
          <div class="conflict-file-path">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px;"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/></svg>
            ${escapeHtml(c.path)}
            <span class="conflict-hunks-summary">(${c.count || hunks.length} conflict hunk${(c.count || hunks.length) === 1 ? "" : "s"})</span>
          </div>
          <div class="conflict-card-actions">
            <button type="button" class="ghost small-btn conflict-btn-ours" data-path="${escapeHtml(c.path)}" title="Keep our current changes">Accept Ours</button>
            <button type="button" class="ghost small-btn conflict-btn-theirs" data-path="${escapeHtml(c.path)}" title="Accept incoming remote changes">Accept Theirs</button>
            <button type="button" class="ghost small-btn conflict-btn-edit" data-path="${escapeHtml(c.path)}" title="Open file in editor to resolve manually">Edit in Editor</button>
            <button type="button" class="primary small-btn conflict-btn-resolved" data-path="${escapeHtml(c.path)}" title="Mark as resolved (git add)">Mark Resolved</button>
          </div>
        </div>
        ${hunksHtml}
      </div>
    `;
  }).join("");

  list.querySelectorAll(".conflict-btn-ours").forEach((btn) => {
    btn.addEventListener("click", () => resolveGitConflict(btn.dataset.path, "ours"));
  });
  list.querySelectorAll(".conflict-btn-theirs").forEach((btn) => {
    btn.addEventListener("click", () => resolveGitConflict(btn.dataset.path, "theirs"));
  });
  list.querySelectorAll(".conflict-btn-edit").forEach((btn) => {
    btn.addEventListener("click", () => openConflictInEditor(btn.dataset.path));
  });
  list.querySelectorAll(".conflict-btn-resolved").forEach((btn) => {
    btn.addEventListener("click", () => resolveGitConflict(btn.dataset.path, "mark_resolved"));
  });
}

async function switchGitBranch(branchName) {
  if (!branchName) return;
  setLoading(true, `Switching to branch ${branchName}...`);
  try {
    const res = await api("/api/git/branch/switch", { method: "POST", body: JSON.stringify({ name: branchName }) });
    state.data.git = res.git;
    renderGit(res.git);
    await refresh();
  } catch (err) {
    alert(`Failed to switch branch: ${err.message}`);
    if (state.data.git) renderGit(state.data.git);
  } finally {
    setLoading(false);
  }
}

function openNewBranchModal() {
  $("newBranchNameInput").value = "";
  $("newBranchStartPointInput").value = "";
  $("newBranchModal").style.display = "grid";
  $("newBranchNameInput").focus();
}

function closeNewBranchModal() {
  $("newBranchModal").style.display = "none";
}

async function submitNewBranch() {
  const name = $("newBranchNameInput").value.trim();
  if (!name) {
    $("newBranchNameInput").focus();
    return;
  }
  const startPoint = $("newBranchStartPointInput").value.trim();
  closeNewBranchModal();
  setLoading(true, `Creating branch ${name}...`);
  try {
    const res = await api("/api/git/branch/create", {
      method: "POST",
      body: JSON.stringify({ name, start_point: startPoint }),
    });
    state.data.git = res.git;
    renderGit(res.git);
    await refresh();
  } catch (err) {
    alert(`Failed to create branch: ${err.message}`);
  } finally {
    setLoading(false);
  }
}

async function fetchGitRepo() {
  setLoading(true, "Fetching from remote...");
  try {
    const res = await api("/api/git/fetch", {
      method: "POST",
      body: JSON.stringify({ ...gitCredentials() }),
    });
    state.data.git = res.git;
    renderGit(res.git);
  } catch (err) {
    alert(`Fetch failed: ${err.message}`);
  } finally {
    setLoading(false);
  }
}

async function pullGitRepo() {
  setLoading(true, "Pulling from remote...");
  try {
    const res = await api("/api/git/pull", {
      method: "POST",
      body: JSON.stringify({ ...gitCredentials() }),
    });
    state.data.git = res.git;
    renderGit(res.git);
    await refresh();
    if (res.conflict) {
      alert("Merge conflict detected! Please use the Merge Conflict Assistant to resolve conflicts.");
    }
  } catch (err) {
    alert(`Pull failed: ${err.message}`);
    await refresh();
  } finally {
    setLoading(false);
  }
}

async function resolveGitConflict(filePath, resolution) {
  setLoading(true, `Resolving ${filePath} (${resolution})...`);
  try {
    const res = await api("/api/git/conflicts/resolve", {
      method: "POST",
      body: JSON.stringify({ path: filePath, resolution }),
    });
    state.data.git = res.git;
    renderGit(res.git);
    await refresh();
  } catch (err) {
    alert(`Failed to resolve conflict: ${err.message}`);
  } finally {
    setLoading(false);
  }
}

async function openConflictInEditor(filePath) {
  showEditorView("code");
  await loadFile(filePath);
}

async function abortGitMerge() {
  if (!window.confirm("Are you sure you want to abort the current merge? All unresolved changes will be reset.")) return;
  setLoading(true, "Aborting merge...");
  try {
    const res = await api("/api/git/merge/abort", { method: "POST", body: JSON.stringify({}) });
    state.data.git = res.git;
    renderGit(res.git);
    await refresh();
  } catch (err) {
    alert(`Failed to abort merge: ${err.message}`);
  } finally {
    setLoading(false);
  }
}

async function completeGitMerge() {
  const message = window.prompt("Merge commit message:", "Merge resolved conflicts");
  if (message === null) return;
  setLoading(true, "Completing merge commit...");
  try {
    const res = await api("/api/git/merge/complete", { method: "POST", body: JSON.stringify({ message }) });
    state.data.git = res.git;
    renderGit(res.git);
    await refresh();
    alert(`Merge completed! Commit: ${res.commit ? res.commit.slice(0, 8) : "done"}`);
  } catch (err) {
    alert(`Failed to complete merge: ${err.message}`);
  } finally {
    setLoading(false);
  }
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
  setLoading(true, "Cloning repository...");
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
  if ($("showCodeTab")) $("showCodeTab").classList.toggle("active", !isGit);
  if ($("showGitTab")) $("showGitTab").classList.toggle("active", isGit);
  setActiveActivity(isGit ? "Git" : "Files");
  if (isGit) {
    api("/api/git").then((git) => {
      if (git && state.data) {
        state.data.git = git;
        renderGit(git);
      }
    }).catch(() => {});
  }
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
  const policyRow = $("approvalPolicyRow");
  if (policyRow) {
    if (event.reason) {
      policyRow.style.display = "flex";
      $("approvalPolicyReason").textContent = event.reason;
    } else {
      policyRow.style.display = "none";
    }
  }
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

// --- Approval Policies Management ---
state.policyScope = "workspace"; // "workspace" or "global"
state.policiesData = null;

function renderApprovalPolicies(policiesData) {
  if (!policiesData) return;
  state.policiesData = policiesData;

  const wsName = $("policyWorkspaceName");
  if (wsName) {
    const wsPath = policiesData.workspace_path || state.data?.workspace?.path || "";
    const parts = wsPath.split(/[\\/]/).filter(Boolean);
    wsName.textContent = parts[parts.length - 1] || wsPath || "Current";
    wsName.title = wsPath;
  }

  const isWorkspace = state.policyScope === "workspace";
  const wsMode = policiesData.workspace?.mode || "inherit";
  const inheritToggle = $("policyInheritToggle");
  if (inheritToggle) {
    inheritToggle.checked = wsMode === "inherit";
  }

  const wsBanner = $("policyWorkspaceBanner");
  if (wsBanner) {
    wsBanner.style.display = isWorkspace ? "flex" : "none";
  }

  const resetBtn = $("policyResetBtn");
  if (resetBtn) {
    resetBtn.style.display = (isWorkspace && wsMode === "custom") ? "inline-block" : "none";
  }

  const activePolicy = isWorkspace
    ? (wsMode === "custom" ? (policiesData.workspace?.policy || {}) : policiesData.effective)
    : policiesData.global;

  const isInherited = isWorkspace && wsMode === "inherit";

  const selects = {
    policyWriteFileSelect: activePolicy?.write_file || "always",
    policyRunBashSelect: activePolicy?.run_bash || "dangerous_only",
    policyRunPythonSelect: activePolicy?.run_python || "always",
    policyGitOpsSelect: activePolicy?.git_push || "always",
  };

  Object.entries(selects).forEach(([id, val]) => {
    const el = $(id);
    if (el) {
      el.value = val;
      el.disabled = isInherited;
    }
  });

  // Keep legacy gitApprovalMode checkbox in sync
  const gitApproval = $("gitApprovalMode");
  if (gitApproval) {
    gitApproval.checked = (selects.policyWriteFileSelect !== "auto");
  }
}

async function saveCurrentPolicy() {
  if (!state.policiesData) return;
  const isWorkspace = state.policyScope === "workspace";
  const inheritToggle = $("policyInheritToggle");
  const mode = (inheritToggle && inheritToggle.checked) ? "inherit" : "custom";

  const policy = {
    write_file: $("policyWriteFileSelect").value,
    replace_in_file: $("policyWriteFileSelect").value,
    run_bash: $("policyRunBashSelect").value,
    run_python: $("policyRunPythonSelect").value,
    git_push: $("policyGitOpsSelect").value,
    git_revert: $("policyGitOpsSelect").value,
  };

  const payload = {
    scope: isWorkspace ? "workspace" : "global",
    mode: mode,
    policy: policy,
    workspace_path: state.policiesData.workspace_path || state.data?.workspace?.path || "",
  };

  const statusEl = $("policyStatusMsg");
  try {
    if (statusEl) { statusEl.textContent = "Saving..."; statusEl.className = "policy-status"; }
    const updated = await api("/api/policies", { method: "POST", body: JSON.stringify(payload) });
    renderApprovalPolicies(updated);
    if (statusEl) {
      statusEl.textContent = "Policy saved ✓";
      statusEl.className = "policy-status saved";
      setTimeout(() => { if (statusEl) statusEl.textContent = ""; }, 2500);
    }
  } catch (err) {
    if (statusEl) { statusEl.textContent = `Error: ${err.message}`; statusEl.className = "policy-status"; }
  }
}

function initApprovalPoliciesUI() {
  const wsBtn = $("policyScopeWorkspaceBtn");
  const globalBtn = $("policyScopeGlobalBtn");

  if (wsBtn && globalBtn) {
    wsBtn.addEventListener("click", () => {
      state.policyScope = "workspace";
      wsBtn.classList.add("active");
      globalBtn.classList.remove("active");
      if (state.policiesData) renderApprovalPolicies(state.policiesData);
    });

    globalBtn.addEventListener("click", () => {
      state.policyScope = "global";
      globalBtn.classList.add("active");
      wsBtn.classList.remove("active");
      if (state.policiesData) renderApprovalPolicies(state.policiesData);
    });
  }

  const inheritToggle = $("policyInheritToggle");
  if (inheritToggle) {
    inheritToggle.addEventListener("change", async () => {
      if (inheritToggle.checked) {
        await api("/api/policies/reset", {
          method: "POST",
          body: JSON.stringify({ workspace_path: state.policiesData?.workspace_path || "" })
        }).then(renderApprovalPolicies);
      } else {
        await saveCurrentPolicy();
      }
    });
  }

  ["policyWriteFileSelect", "policyRunBashSelect", "policyRunPythonSelect", "policyGitOpsSelect"].forEach((id) => {
    const el = $(id);
    if (el) {
      el.addEventListener("change", () => {
        const inheritToggle = $("policyInheritToggle");
        if (state.policyScope === "workspace" && inheritToggle && inheritToggle.checked) {
          inheritToggle.checked = false;
        }
        saveCurrentPolicy();
      });
    }
  });

  const resetBtn = $("policyResetBtn");
  if (resetBtn) {
    resetBtn.addEventListener("click", async () => {
      const res = await api("/api/policies/reset", {
        method: "POST",
        body: JSON.stringify({ workspace_path: state.policiesData?.workspace_path || "" })
      });
      renderApprovalPolicies(res);
    });
  }
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
  const isCustom = $("connMode")?.value?.includes("Custom") || String(state.data?.settings?.conn_mode || "").toLowerCase().includes("custom");
  const models = modelPayload.models || [];
  const selectedModel = modelPayload.selected_model || activeModel;

  // Update hidden select for state compatibility
  const select = $("modelSelect");
  if (select) {
    const options = models.length ? models : [selectedModel || (isCustom ? "gpt-4o-mini" : "llama3")];
    select.innerHTML = options.map((name) => `
      <option value="${escapeHtml(name)}" ${name === selectedModel ? "selected" : ""}>${escapeHtml(name)}</option>
    `).join("");
  }

  // Update modal Ollama select
  const modalOllamaSelect = $("modalOllamaModelSelect");
  if (modalOllamaSelect) {
    const ollamaModels = models.length ? models : [selectedModel || "gemma4:12b"];
    modalOllamaSelect.innerHTML = ollamaModels.map((name) => `
      <option value="${escapeHtml(name)}" ${name === selectedModel ? "selected" : ""}>${escapeHtml(name)}</option>
    `).join("");
  }

  // Update header chooseModelBtn label & icon
  const customModel = $("modalCustomApiModel")?.value?.trim() || state.data?.settings?.custom_api_model || "gpt-4o-mini";
  const ollamaModel = selectedModel || "gemma4:12b";
  if ($("chooseModelIcon")) {
    $("chooseModelIcon").textContent = isCustom ? "🔑" : "🖥️";
  }
  if ($("chooseModelLabel")) {
    $("chooseModelLabel").textContent = isCustom ? `Custom: ${customModel}` : `Ollama: ${ollamaModel}`;
  }
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
  refreshSchematicGraph();
}

function renderSkills() {
  const usage = new Map((state.data?.skill_usage?.skills || []).map((item) => [item.name, item]));
  $("skillsList").innerHTML = (state.data?.skills || []).map((skill) => `
    <button class="skill-item skill-mode-${usage.get(skill.name)?.mode || "auto"}" data-skill="${escapeHtml(skill.name)}" ${skill.disabled ? "disabled" : ""}>
      /${escapeHtml(skill.name)}
      <span class="skill-mode-label">${usage.get(skill.name)?.mode === "pinned" ? "Pinned" : usage.get(skill.name)?.mode === "off" ? "Off" : "Auto"}</span>
      <span class="skill-desc">${escapeHtml(skill.description || skill.category)} · ${usage.get(skill.name)?.project_uses || 0} uses${usage.get(skill.name)?.project_uses ? "" : " ⚠"}</span>
    </button>
  `).join("");
  renderRoutingNotice("skillsRoutingNotice", state.data?.skill_usage?.routing || {});
  $("skillsRecentUsage").innerHTML = (state.data?.skill_usage?.recent || []).slice(0, 20).map((event) => `<div class="skill-usage-row"><strong>/${escapeHtml(event.skill_name)}</strong><span>${escapeHtml(event.status)} · ${formatRelativeTime(event.timestamp)} · session ${escapeHtml(event.session_id.slice(0, 6))}</span></div>`).join("") || `<div class="memory-item-source">No skill usage recorded for this project.</div>`;
  document.querySelectorAll(".skill-item").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const current = usage.get(btn.dataset.skill)?.mode || "auto";
      const next = current === "auto" ? "pinned" : current === "pinned" ? "off" : "auto";
      renderState(await api("/api/skills/mode", {
        method: "POST",
        body: JSON.stringify({ skill_name: btn.dataset.skill, mode: next }),
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

function isRTL(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return false;
  const clean = trimmed.replace(/[\d\s.,!?:;"'()\[\]{}<>\/\\@#$%^&*_+=~`|-]/g, "");
  if (!clean) return false;
  const rtlChars = clean.match(/[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]/g) || [];
  return (rtlChars.length / clean.length) > 0.2;
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
    const isMsgRTL = isRTL(msg.content);
    const dirAttr = isMsgRTL ? "rtl" : "ltr";
    const roleLabel = msg.role === "user" ? "You" : "Coder AI";
    return `
      <article class="message ${msg.role} ${dirAttr}" dir="${dirAttr}">
        <div class="message-meta"><span class="role">${roleLabel}</span></div>
        <div class="message-text">${renderMessageContent(msg)}</div>
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
  state.workspaceLocked = false;
  document.body.classList.remove("workspace-locked");
}

function setLoading(isLoading, text = "Working...") {
  document.body.classList.toggle("loading", isLoading);
  $("loadingText").textContent = text;
  const agentLabel = document.querySelector(".signal.is-agent .signal-label");
  if (agentLabel) agentLabel.textContent = isLoading ? "Agent working" : "Agent idle";
}

function appendStreamingAssistant() {
  const article = document.createElement("article");
  article.className = "message assistant streaming ltr";
  article.innerHTML = `<div class="message-meta"><span class="role">Coder AI</span></div><div class="message-text"><span class="stream-content"></span></div>`;
  $("messages").appendChild(article);
  $("messages").scrollTop = $("messages").scrollHeight;
  return article.querySelector(".stream-content");
}

function appendToolStatus(name, text) {
  const article = document.createElement("article");
  article.className = "message assistant";
  article.innerHTML = `<div class="message-meta"><span class="role">System Action</span></div><div class="tool-box">${escapeHtml(`${name}\n${text}`)}</div>`;
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
}

function renderCodeIndex(index = {}) {
  $("indexFileCount").textContent = Number(index.files || 0).toLocaleString();
  $("indexSymbolCount").textContent = Number(index.symbols || 0).toLocaleString();
  $("indexChunkCount").textContent = Number(index.chunks || 0).toLocaleString();
  const hasIndex = Number(index.files || 0) > 0;
  const fresh = index.up_to_date;
  $("indexFreshness").textContent = !hasIndex ? "Not indexed" : fresh === false ? "Changes detected" : fresh === true ? "Up to date" : "Ready";
  $("indexLastRun").textContent = index.last_indexed ? formatRelativeTime(index.last_indexed) : "Never";
  $("indexEmbeddingModel").textContent = index.embedding_model || "nomic-embed-text";
  $("indexVectorBackend").textContent = index.vector_backend === "chromadb"
    ? "Advanced semantic search"
    : index.vector_backend === "sqlite-vec"
    ? "sqlite-vec accelerated search"
    : "Local fallback search";
  $("indexStatusDot").className = hasIndex && fresh !== false ? "ready" : hasIndex ? "stale" : "";
  const embeddingError = index.embedding_error || "";
  const notice = embeddingError
    ? friendlyErrorMessage(embeddingError)
    : "";
  $("indexError").textContent = notice;
  $("indexError").classList.toggle("visible", Boolean(notice));
  if (index.project_summary) $("indexProjectSummary").textContent = index.project_summary;
  $("indexGraphStats").textContent = `${Number(index.graph_nodes || 0)} files · ${Number(index.graph_edges || 0)} relations`;
}

function renderProjectIntelligence(payload = {}) {
  const overview = payload.overview || {};
  $("indexProjectSummary").textContent = overview.summary || "Build the index to generate a project-level architecture summary.";
  $("indexGraphStats").textContent = `${Number(overview.nodes || 0)} files · ${Number(overview.edges || 0)} relations`;
  $("indexEntryPoints").innerHTML = (overview.entry_points || []).map((path) => `<code title="${escapeHtml(path)}">${escapeHtml(path)}</code>`).join("");
  const lines = [];
  (payload.graph || []).forEach((node) => {
    lines.push(`▾ ${node.file_path}`);
    (node.related || []).forEach((edge) => lines.push(`   ${edge.relation_type} → ${edge.to_file}  [${edge.detail}]`));
  });
  $("indexDependencyTree").textContent = lines.join("\n") || "No resolved project dependencies yet.";
}

async function refreshCodeIndex() {
  const [index, intelligence] = await Promise.all([api("/api/index"), api("/api/index/overview")]);
  if (state.data) state.data.code_index = index;
  renderCodeIndex(index);
  renderProjectIntelligence(intelligence);
}

async function runCodeIndex(path, label) {
  setLoading(true, label, false);
  $("syncCodeIndex").disabled = true;
  $("rebuildCodeIndex").disabled = true;
  try {
    const index = await api(path, { method: "POST", body: "{}" });
    if (state.data) state.data.code_index = index;
    renderCodeIndex(index);
    await refreshCodeIndex();
  } catch (err) {
    $("indexError").textContent = friendlyErrorMessage(err.message);
    $("indexError").classList.add("visible");
  } finally {
    $("syncCodeIndex").disabled = false;
    $("rebuildCodeIndex").disabled = false;
    setLoading(false);
  }
}

function activateFileView(name) {
  document.querySelectorAll(".file-view-tab").forEach((button) => button.classList.toggle("active", button.dataset.fileView === name));
  $("fileBrowserView").classList.toggle("active", name === "browser");
  $("codeIndexView").classList.toggle("active", name === "index");
  if (name === "index") refreshCodeIndex().catch((err) => {
    $("indexError").textContent = friendlyErrorMessage(err.message);
    $("indexError").classList.add("visible");
  });
}

function renderRoutingNotice(id, routing) {
  const skipped = routing.skipped || [];
  const details = [];
  if (skipped.length) details.push(`${skipped.length} matched skill(s) skipped by the ${routing.limit || 3}-skill context limit: ${skipped.join(", ")}`);
  if (routing.confirmation_used) details.push("Smart LLM confirmation was used");
  if (routing.selected?.length) details.push(`Active: ${routing.selected.join(", ")}`);
  $(id).textContent = details.join(" · ");
  $(id).classList.toggle("visible", details.length > 0);
}

function appendSkillStatus(event) {
  // Skills are hidden from chat feed per user request (kept in Skills tab / status bar)
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

function openChooseModelModal() {
  const modal = $("chooseModelModal");
  if (!modal) return;
  const isCustom = $("connMode")?.value?.includes("Custom") || String(state.data?.settings?.conn_mode || "").toLowerCase().includes("custom");
  switchModelModeTab(isCustom ? "custom" : "ollama");

  const settings = state.data?.settings || {};
  if ($("modalCustomApiUrl")) $("modalCustomApiUrl").value = settings.custom_api_url || "https://api.openai.com/v1";
  if ($("modalCustomApiKey")) $("modalCustomApiKey").value = "";
  if ($("modalCustomApiModel")) $("modalCustomApiModel").value = settings.custom_api_model || "gpt-4o-mini";

  modal.style.display = "flex";
}

function closeChooseModelModal() {
  const modal = $("chooseModelModal");
  if (modal) modal.style.display = "none";
}

function switchModelModeTab(tab) {
  const isOllama = tab === "ollama";
  const tabOllama = $("tabLocalOllama");
  const tabCustom = $("tabCustomApi");
  const secOllama = $("sectionLocalOllama");
  const secCustom = $("sectionCustomApi");

  if (tabOllama) {
    tabOllama.classList.toggle("active", isOllama);
    tabOllama.style.background = "";
    tabOllama.style.color = "";
  }
  if (tabCustom) {
    tabCustom.classList.toggle("active", !isOllama);
    tabCustom.style.background = "";
    tabCustom.style.color = "";
  }
  if (secOllama) secOllama.style.display = isOllama ? "grid" : "none";
  if (secCustom) secCustom.style.display = !isOllama ? "grid" : "none";
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
  const isCustom = $("connMode").value.includes("Custom") || String(state.data?.settings?.conn_mode || "").toLowerCase().includes("custom");
  const apiUrl = ($("modalCustomApiUrl")?.value?.trim() || $("customApiUrl")?.value?.trim() || state.data?.settings?.custom_api_url || "https://api.openai.com/v1");
  const apiKey = ($("modalCustomApiKey")?.value?.trim() || $("customApiKey")?.value?.trim() || "");
  const customApiModel = ($("modalCustomApiModel")?.value?.trim() || $("customApiModel")?.value?.trim() || state.data?.settings?.custom_api_model || "gpt-4o-mini");

  const payload = {
    conn_mode: $("connMode").value,
    model: $("modelSelect")?.value || $("modalOllamaModelSelect")?.value || "gemma4:12b",
    temperature: Number($("temperature").value),
    enable_thinking: $("thinking").checked,
    auto_continue: $("autoContinue").checked,
    memory_enabled: $("memoryEnabled").checked,
    context_token_budget: Number($("contextTokenBudget").value),
    response_token_budget: Number($("responseTokenBudget").value),
    tavily_enabled: $("tavilyEnabled").checked,
    tavily_api_key: $("tavilyApiKey").value,
    git_approval_mode: $("gitApprovalMode").checked,
    smart_skill_confirmation: $("smartSkillConfirmation").checked,
    sandbox_mode: $("sandboxMode") ? $("sandboxMode").value : "auto",
    sandbox_docker_image: $("sandboxDockerImage") ? $("sandboxDockerImage").value.trim() : "python:3.11-slim",
    custom_api_url: apiUrl,
    custom_api_model: customApiModel,
  };
  if (apiKey) {
    payload.custom_api_key = apiKey;
  }
  renderState(await api("/api/settings", {
    method: "POST",
    body: JSON.stringify(payload),
  }), false);
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
  renderState(await api("/api/state"), false);
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
  setLoading(true, "Scanning project...");
  try {
    const data = await api("/api/scan", { method: "POST", body: JSON.stringify({ max_files: 250 }) });
    renderState(data.state);
  } finally {
    setLoading(false);
  }
}

function dispatchStreamEvent(event, ctx) {
  if (!event || typeof event !== "object") return;
  if (event.type === "state") {
    renderState(event.state);
    ctx.ensureStreamTarget("Preparing context...");
  } else if (event.type === "status") {
    setLoading(true, event.message);
    ctx.ensureStreamTarget(event.message || "Working...");
  } else if (event.type === "token") {
    ctx.ensureStreamTarget("");
    ctx.appendToken(event.content || "");
  } else if (event.type === "tool_call") {
    setLoading(true, `Running ${event.name}...`);
    appendToolStatus(event.name, JSON.stringify(event.args || {}, null, 2));
  } else if (event.type === "tool_result") {
    appendToolStatus(`${event.name} result`, String(event.result || "").slice(0, 1200));
  } else if (event.type === "skill_selected" || event.type === "skill_applied" || event.type === "skill_failed") {
    // Hidden from chat feed per user request (skills are maintained in the Skills tab)
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
  } else if (event.type === "code_rag_used") {
    appendToolStatus("Codebase context", event.query_type === "project_level" ? "Project overview and dependency graph loaded" : `${event.count} relevant chunk(s) plus graph relations loaded`);
  } else if (event.type === "code_index_updated") {
    appendToolStatus("Code index", `${event.path || "File"} updated · ${event.chunks || 0} chunk(s)`);
  } else if (event.type === "code_index_error") {
    appendToolStatus("Code index warning", event.message || "Index update failed");
  } else if (event.type === "cancelled") {
    ctx.showStreamNotice("⛔ Generation stopped by user.");
    ctx.setSawDone(true);
  } else if (event.type === "error") {
    ctx.showStreamNotice(event.message || "Error");
  } else if (event.type === "done") {
    ctx.setSawDone(true);
    renderState(event.state);
  }
}

async function streamViaWebSocket(prompt, activeContext, ctx, controller) {
  if (typeof WebSocket === "undefined") throw new Error("WebSocket not supported");
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const sid = getActiveSessionId();
  const wsUrl = `${protocol}//${location.host}/ws/chat?session_id=${encodeURIComponent(sid)}`;

  return new Promise((resolve, reject) => {
    let ws;
    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      return reject(e);
    }

    let opened = false;
    let completed = false;

    const cleanup = () => {
      if (ws) {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        try { ws.close(); } catch (_) {}
      }
    };

    const abortListener = () => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: "cancel", session_id: sid })); } catch (_) {}
      }
      cleanup();
      resolve(false);
    };
    controller.signal.addEventListener("abort", abortListener, { once: true });

    ws.onopen = () => {
      opened = true;
      try {
        ws.send(JSON.stringify({ type: "chat", session_id: sid, prompt, active_context: activeContext }));
      } catch (err) {
        cleanup();
        reject(err);
      }
    };

    ws.onmessage = async (event) => {
      try {
        const payload = JSON.parse(event.data);
        dispatchStreamEvent(payload, ctx);
        if (payload.type === "done" || payload.type === "cancelled") {
          completed = true;
          controller.signal.removeEventListener("abort", abortListener);
          cleanup();
          resolve(true);
        }
      } catch (err) {
        ctx.showStreamNotice(`[ws parse error] ${err.message}`);
      }
    };

    ws.onerror = (err) => {
      if (!opened) {
        cleanup();
        reject(new Error("WebSocket connection failed"));
      } else {
        ctx.showStreamNotice("[ws connection error]");
      }
    };

    ws.onclose = () => {
      controller.signal.removeEventListener("abort", abortListener);
      if (!completed && !opened) {
        reject(new Error("WebSocket closed before opening"));
      } else {
        resolve(completed);
      }
    };
  });
}

async function streamViaHttp(prompt, activeContext, ctx, controller) {
  const sid = getActiveSessionId();
  const res = await fetch("/api/chat_stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Session-ID": sid },
    body: JSON.stringify({ session_id: sid, prompt, active_context: activeContext }),
    signal: controller.signal,
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
      try {
        const event = JSON.parse(line);
        dispatchStreamEvent(event, ctx);
      } catch (err) {
        ctx.showStreamNotice(`[stream parse error] ${err.message}`);
      }
    }
  }
  if (buffer.trim()) {
    try {
      const event = JSON.parse(buffer.trim());
      dispatchStreamEvent(event, ctx);
    } catch (_) {}
  }
}

async function sendPrompt(prompt) {
  const isCustom = $("connMode")?.value?.includes("Custom");
  const customModel = ($("topCustomApiModel")?.value?.trim() || $("customApiModel")?.value?.trim() || "gpt-4o-mini");
  const localModel = ($("modelSelect")?.value || "gemma4:12b");
  const targetLabel = isCustom ? `Custom API (${customModel})` : `Ollama (${localModel})`;
  const waitingMsg = `Waiting for ${targetLabel}...`;

  setLoading(true, waitingMsg);
  $("sendBtn").style.display = "none";
  $("stopBtn").style.display = "inline-flex";
  $("stopBtn").disabled = false;
  $("stopBtn").innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg> Stop`;

  let streamTarget = appendStreamingAssistant();
  streamTarget.textContent = waitingMsg;
  let streamText = "";
  let sawDone = false;
  const activeContext = currentActiveContext();
  state.editorDirty = false;
  const controller = new AbortController();

  const stopHandler = async (e) => {
    e?.preventDefault?.();
    $("stopBtn").disabled = true;
    $("stopBtn").innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg> Stopping...`;
    controller.abort();
    ctx.showStreamNotice("⛔ Stopping generation...");
    try {
      await fetch("/api/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
    } catch (_) {}
  };
  $("stopBtn").addEventListener("click", stopHandler, { once: true });

  const ctx = {
    ensureStreamTarget: (placeholder = "Working...") => {
      if (!streamTarget || !streamTarget.isConnected) {
        streamTarget = appendStreamingAssistant();
      }
      if (!streamText && placeholder) {
        streamTarget.textContent = placeholder;
      }
      return streamTarget;
    },
    showStreamNotice: (message) => {
      ctx.ensureStreamTarget("");
      if (streamText) {
        streamText += `\n\n${message}`;
        streamTarget.textContent = streamText;
      } else {
        streamTarget.textContent = message;
      }
      $("messages").scrollTop = $("messages").scrollHeight;
    },
    appendToken: (chunk) => {
      streamText += chunk;
      streamTarget.textContent = streamText;
      const isPersian = isRTL(streamText);
      const article = streamTarget.closest("article");
      if (article) {
        article.setAttribute("dir", isPersian ? "rtl" : "ltr");
        article.classList.toggle("rtl", isPersian);
        article.classList.toggle("ltr", !isPersian);
      }
      updateGeneratedCodeFromStreaming(streamText);
      $("messages").scrollTop = $("messages").scrollHeight;
    },
    setSawDone: (val) => {
      sawDone = val;
    },
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
    await streamViaHttp(prompt, activeContext, ctx, controller);

    if (sawDone) {
      await refreshEditorAfterDone();
    } else if (!controller.signal.aborted) {
      ctx.showStreamNotice("[stream completed]");
      await refreshEditorAfterDone();
    }
  } catch (err) {
    if (err.name === "AbortError") {
      ctx.showStreamNotice("⛔ Generation stopped by user.");
    } else {
      ctx.showStreamNotice(`Agent stream error: ${err.message}`);
    }
  } finally {
    $("stopBtn").removeEventListener("click", stopHandler);
    $("stopBtn").disabled = false;
    $("stopBtn").innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg> Stop`;
    $("sendBtn").style.display = "";
    $("stopBtn").style.display = "none";
    setLoading(false);
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    activateTab(tab.dataset.tab);
    setActiveActivity(tab.dataset.tab === "settings" ? "Settings" : "Files");
  });
});

document.querySelectorAll(".activity-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.initialProjectsShown = true;
    state.userNavigated = true;
    const label = btn.getAttribute("aria-label");
    setActiveActivity(label);
    if (label === "Projects") {
      showProjects();
    } else if (label === "Files") {
      showWorkbench();
      activateTab("files");
      showEditorView("code");
      $("fileSearch").focus();
    } else if (label === "Graph") {
      showCodeGraphView();
    } else if (label === "Git") {
      showWorkbench();
      activateTab("files");
      showEditorView("git");
    } else if (label === "Settings") {
      showWorkbench();
      activateTab("settings");
    }
  });
});

document.querySelectorAll(".project-tab").forEach((tab) => tab.addEventListener("click", () => activateProjectTab(tab.dataset.projectTab)));
$("openCommandPalette").addEventListener("click", openCommandPalette);
$("commandSearch").addEventListener("input", () => { state.commandSelection = 0; renderCommandPalette(); });
document.querySelectorAll("[data-command-close]").forEach((element) => element.addEventListener("click", closeCommandPalette));
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    return $("commandPalette").hidden ? openCommandPalette() : closeCommandPalette();
  }
  if ($("commandPalette").hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    return closeCommandPalette();
  }
  const commands = filteredCommands();
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if (!commands.length) return;
    state.commandSelection = (state.commandSelection + (event.key === "ArrowDown" ? 1 : -1) + commands.length) % commands.length;
    return renderCommandPalette();
  }
  if (event.key === "Enter" && document.activeElement === $("commandSearch") && commands[state.commandSelection]) {
    event.preventDefault();
    return runCommand(commands[state.commandSelection].id);
  }
});
$("backToProjects").addEventListener("click", () => { $("projectHome").classList.remove("active"); $("projectsIndex").classList.remove("hidden"); });
$("openProjectInEditor").addEventListener("click", () => openProjectInEditor(false));
$("startProjectSession").addEventListener("click", () => openProjectInEditor(true));
$("newProjectSession").addEventListener("click", () => openProjectInEditor(true));
$("newProject").addEventListener("click", async () => { showWorkbench(); setActiveActivity("Files"); await browseWorkspace(); });
$("openRuntimeSettings").addEventListener("click", () => { showWorkbench(); setActiveActivity("Settings"); activateTab("settings"); });

$("fileSearch").addEventListener("input", renderFiles);
document.querySelectorAll(".file-view-tab").forEach((button) => button.addEventListener("click", () => activateFileView(button.dataset.fileView)));
$("syncCodeIndex").addEventListener("click", () => runCodeIndex("/api/index/sync", "Syncing codebase index..."));
$("rebuildCodeIndex").addEventListener("click", () => runCodeIndex("/api/index/rebuild", "Rebuilding codebase index..."));
$("regenerateProjectSummary").addEventListener("click", async () => {
  setLoading(true, "Regenerating project overview with the local model...", false);
  $("regenerateProjectSummary").disabled = true;
  try {
    await api("/api/index/regenerate-summary", {method: "POST", body: JSON.stringify({use_model: true})});
    await refreshCodeIndex();
  } catch (err) {
    $("indexError").textContent = friendlyErrorMessage(err.message);
    $("indexError").classList.add("visible");
  } finally {
    $("regenerateProjectSummary").disabled = false;
    setLoading(false);
  }
});
$("promptSearch").addEventListener("input", renderPrompts);
$("openWorkspace").addEventListener("click", openWorkspace);
$("browseWorkspace").addEventListener("click", browseWorkspace);
$("saveSettings").addEventListener("click", saveSettings);
$("compactMemory").addEventListener("click", compactMemory);
$("addMemoryFact").addEventListener("click", addMemoryFact);
$("addMemoryPreference").addEventListener("click", addMemoryPreference);
$("forgetProjectMemory").addEventListener("click", forgetProjectMemory);
$("resumeMemorySession").addEventListener("click", resumeMemorySession);
$("testSkills")?.addEventListener("click", testSkills);
$("tavilyEnabled").addEventListener("change", () => updateTavilyPanel());

// Choose Model Button & Modal
$("chooseModelBtn")?.addEventListener("click", openChooseModelModal);
$("chooseModelCloseX")?.addEventListener("click", closeChooseModelModal);
$("chooseModelCancel")?.addEventListener("click", closeChooseModelModal);
$("chooseCustomCancel")?.addEventListener("click", closeChooseModelModal);

// Tabs
$("tabLocalOllama")?.addEventListener("click", () => switchModelModeTab("ollama"));
$("tabCustomApi")?.addEventListener("click", () => switchModelModeTab("custom"));

// Refresh Ollama in modal
  $("modalRefreshOllama")?.addEventListener("click", async () => {
    const modelsPayload = await api("/api/models?force=true");
    renderModels(modelsPayload, state.data?.settings?.model);
  });

// Apply Local Ollama
$("applyLocalOllama")?.addEventListener("click", async () => {
  const selectedModel = $("modalOllamaModelSelect")?.value || "gemma4:12b";
  if ($("connMode")) $("connMode").value = "🖥️ Local Ollama";
  if ($("modelSelect")) $("modelSelect").value = selectedModel;

  closeChooseModelModal();

  const payload = {
    conn_mode: "🖥️ Local Ollama",
    model: selectedModel,
    temperature: Number($("temperature").value),
    enable_thinking: $("thinking").checked,
    auto_continue: $("autoContinue").checked,
    memory_enabled: $("memoryEnabled").checked,
    context_token_budget: Number($("contextTokenBudget").value),
    response_token_budget: Number($("responseTokenBudget").value),
    tavily_enabled: $("tavilyEnabled").checked,
    tavily_api_key: $("tavilyApiKey").value,
    git_approval_mode: $("gitApprovalMode").checked,
    smart_skill_confirmation: $("smartSkillConfirmation").checked,
    sandbox_mode: $("sandboxMode") ? $("sandboxMode").value : "auto",
    sandbox_docker_image: $("sandboxDockerImage") ? $("sandboxDockerImage").value.trim() : "python:3.11-slim",
  };
  renderState(await api("/api/settings", {
    method: "POST",
    body: JSON.stringify(payload),
  }), false);
  await refreshModels();
});

// Apply Custom API
$("applyCustomApi")?.addEventListener("click", async () => {
  const url = $("modalCustomApiUrl")?.value?.trim() || "https://api.openai.com/v1";
  const key = $("modalCustomApiKey")?.value?.trim() || "";
  const model = $("modalCustomApiModel")?.value?.trim() || "gpt-4o-mini";

  if ($("connMode")) $("connMode").value = "🔑 Custom API";
  if ($("customApiUrl")) $("customApiUrl").value = url;
  if ($("customApiModel")) $("customApiModel").value = model;
  if ($("modelSelect")) $("modelSelect").value = model;

  closeChooseModelModal();

  const payload = {
    conn_mode: "🔑 Custom API",
    custom_api_url: url,
    custom_api_model: model,
    model: model,
    temperature: Number($("temperature").value),
    enable_thinking: $("thinking").checked,
    auto_continue: $("autoContinue").checked,
    memory_enabled: $("memoryEnabled").checked,
    context_token_budget: Number($("contextTokenBudget").value),
    response_token_budget: Number($("responseTokenBudget").value),
    tavily_enabled: $("tavilyEnabled").checked,
    tavily_api_key: $("tavilyApiKey").value,
    git_approval_mode: $("gitApprovalMode").checked,
    smart_skill_confirmation: $("smartSkillConfirmation").checked,
    sandbox_mode: $("sandboxMode") ? $("sandboxMode").value : "auto",
    sandbox_docker_image: $("sandboxDockerImage") ? $("sandboxDockerImage").value.trim() : "python:3.11-slim",
  };
  if (key) {
    payload.custom_api_key = key;
  }
  renderState(await api("/api/settings", {
    method: "POST",
    body: JSON.stringify(payload),
  }), false);
  await refreshModels();
});

$("refreshModels")?.addEventListener("click", refreshModels);
$("modelSelect")?.addEventListener("change", saveSettings);
$("connMode")?.addEventListener("change", async () => {
  const isCustom = $("connMode").value.includes("Custom");
  if (isCustom) {
    openChooseModelModal();
  }
  await saveSettings();
  await refreshModels();
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
$("showCodeTab")?.addEventListener("click", () => showEditorView("code"));
$("showGitTab")?.addEventListener("click", () => showEditorView("git"));
$("gitAuthMode").addEventListener("change", updateGitAuthPanel);
$("cloneGitRepo").addEventListener("click", cloneGitRepository);
$("pushGitChanges").addEventListener("click", () => previewGitPush().catch((error) => alert(error.message)));
$("gitBranchSelect")?.addEventListener("change", (e) => switchGitBranch(e.target.value));
$("gitNewBranchBtn")?.addEventListener("click", openNewBranchModal);
$("newBranchCancelBtn")?.addEventListener("click", closeNewBranchModal);
$("newBranchSubmitBtn")?.addEventListener("click", submitNewBranch);
$("gitFetchBtn")?.addEventListener("click", fetchGitRepo);
$("gitPullBtn")?.addEventListener("click", pullGitRepo);
$("gitAbortMergeBtn")?.addEventListener("click", abortGitMerge);
$("gitCompleteMergeBtn")?.addEventListener("click", completeGitMerge);
$("approvalApprove").addEventListener("click", () => resolveApproval(true));
$("approvalReject").addEventListener("click", () => resolveApproval(false));
$("codeEditor").addEventListener("input", () => {
  state.fileContent = $("codeEditor").value;
  state.editorDirty = true;
  if ($("saveEditorCode")) $("saveEditorCode").style.display = "inline-flex";
  if (!state.activeFile || state.activeFile === "No file selected") {
    $("activeFile").textContent = "Untitled";
    $("fileMeta").textContent = `${state.fileContent.length.toLocaleString()} chars • editable`;
  }
  $("downloadCode").disabled = !state.fileContent;
  $("attachFile").disabled = !state.fileContent;
  $("codeEditorWrap").classList.toggle("empty", !state.fileContent);
  updateEditorLineNumbers();
  updateTokenUsage();
});

$("saveEditorCode")?.addEventListener("click", async () => {
  if (!state.activeFile || state.activeFile === "Untitled") return;
  const btn = $("saveEditorCode");
  const orig = btn.innerHTML;
  try {
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Saving...`;
    await api("/api/file/write", {
      method: "POST",
      body: JSON.stringify({ path: state.activeFile, content: state.fileContent })
    });
    state.editorDirty = false;
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="20 6 9 17 4 12"/></svg> Saved!`;
    setTimeout(() => { btn.style.display = "none"; btn.innerHTML = orig; }, 1500);
  } catch (err) {
    btn.innerHTML = `<span style="color:var(--red);">Error!</span>`;
    setTimeout(() => { btn.innerHTML = orig; }, 2000);
  }
});

$("codeEditor").addEventListener("scroll", () => {
  syncEditorHighlightScroll();
});

$("codeEditor").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if ($("saveEditorCode") && $("saveEditorCode").style.display !== "none") {
      $("saveEditorCode").click();
    }
    return;
  }
  if (e.key === "Tab") {
    e.preventDefault();
    const editor = $("codeEditor");
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    const val = editor.value;
    editor.value = val.substring(0, start) + "  " + val.substring(end);
    editor.selectionStart = editor.selectionEnd = start + 2;
    state.fileContent = editor.value;
    state.editorDirty = true;
    $("downloadCode").disabled = !state.fileContent;
    $("attachFile").disabled = !state.fileContent;
    updateEditorLineNumbers();
  }
});

$("copyEditorCode")?.addEventListener("click", async () => {
  const text = $("codeEditor")?.value;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    const btn = $("copyEditorCode");
    const orig = btn.innerHTML;
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="20 6 9 17 4 12"/></svg> Copied!`;
    setTimeout(() => { btn.innerHTML = orig; }, 1500);
  } catch (_) {
    $("codeEditor")?.select();
    document.execCommand("copy");
  }
});

$("pasteEditorCode")?.addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      const editor = $("codeEditor");
      editor.focus();
      const start = editor.selectionStart || 0;
      const end = editor.selectionEnd || 0;
      const val = editor.value;
      editor.value = val.substring(0, start) + text + val.substring(end);
      editor.selectionStart = editor.selectionEnd = start + text.length;
      state.fileContent = editor.value;
      state.editorDirty = true;
      if (!state.activeFile || state.activeFile === "No file selected") {
        $("activeFile").textContent = "Untitled";
        $("fileMeta").textContent = `${state.fileContent.length.toLocaleString()} chars · editable`;
      }
      $("downloadCode").disabled = !state.fileContent;
      $("attachFile").disabled = !state.fileContent;
      updateEditorLineNumbers();
      updateTokenUsage();
    }
  } catch (_) {
    $("codeEditor")?.focus();
  }
});
$("promptInput").addEventListener("input", (e) => {
  updateTokenUsage();
  const isPersian = isRTL(e.target.value);
  e.target.setAttribute("dir", isPersian ? "rtl" : "ltr");
  e.target.classList.toggle("rtl", isPersian);
  e.target.classList.toggle("ltr", !isPersian);
});
$("chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = $("promptInput").value.trim();
  if (!prompt) return;
  $("promptInput").value = "";
  $("promptInput").setAttribute("dir", "auto");
  $("promptInput").classList.remove("rtl");
  $("promptInput").classList.add("ltr");
  updateTokenUsage();
  await sendPrompt(prompt);
});

let embeddingPullPollTimer = null;

async function checkEmbeddingModelStatus() {
  try {
    const status = await api("/api/ollama/embedding-status");
    updateEmbeddingUI(status);
    if (!status.installed && !status.dismissed && !status.is_pulling) {
      showEmbeddingModal(status);
    }
  } catch (err) {
    console.warn("Could not check embedding status:", err);
  }
}

function updateEmbeddingUI(status) {
  const modelEl = $("indexEmbeddingModel");
  if (modelEl) {
    modelEl.textContent = status.installed ? "embeddinggemma" : (status.active_model || "nomic-embed-text");
    if (status.installed) {
      modelEl.style.color = "var(--teal)";
      modelEl.title = "Using Google embeddinggemma (768d dense vectors)";
    }
  }
}

function showEmbeddingModal(status) {
  const modal = $("embeddingModal");
  if (!modal) return;
  modal.style.display = "flex";
}

function hideEmbeddingModal() {
  const modal = $("embeddingModal");
  if (!modal) return;
  modal.style.display = "none";
}

async function dismissEmbeddingModal() {
  hideEmbeddingModal();
  try {
    await api("/api/ollama/dismiss-embedding", { method: "POST", body: JSON.stringify({}) });
  } catch (_) {}
}

async function startPullingEmbedding() {
  const btnPull = $("btnPullEmbedding");
  const btnDismiss = $("btnDismissEmbedding");
  const progressWrap = $("embeddingPullProgressWrap");
  const statusText = $("embeddingPullStatusText");
  const statusPct = $("embeddingPullStatusPct");
  const progressBar = $("embeddingPullProgressBar");

  if (btnPull) btnPull.disabled = true;
  if (btnDismiss) btnDismiss.disabled = true;
  if (progressWrap) progressWrap.style.display = "block";

  try {
    await api("/api/ollama/pull-embedding", { method: "POST", body: JSON.stringify({}) });

    if (embeddingPullPollTimer) clearInterval(embeddingPullPollTimer);
    embeddingPullPollTimer = setInterval(async () => {
      try {
        const status = await api("/api/ollama/embedding-status");
        if (status.progress_text && statusText) {
          statusText.textContent = status.progress_text;
          const match = status.progress_text.match(/(\d+)%/);
          if (match && statusPct && progressBar) {
            statusPct.textContent = `${match[1]}%`;
            progressBar.style.width = `${match[1]}%`;
          }
        }
        if (status.completed || status.installed) {
          clearInterval(embeddingPullPollTimer);
          embeddingPullPollTimer = null;
          if (statusText) statusText.textContent = "مدل با موفقیت دانلود و آماده شد!";
          if (progressBar) progressBar.style.width = "100%";
          if (statusPct) statusPct.textContent = "100%";
          updateEmbeddingUI(status);
          setTimeout(() => {
            hideEmbeddingModal();
            if (btnPull) btnPull.disabled = false;
            if (btnDismiss) btnDismiss.disabled = false;
            api("/api/index/sync", { method: "POST", body: JSON.stringify({}) }).catch(() => {});
          }, 1200);
        } else if (status.error) {
          clearInterval(embeddingPullPollTimer);
          embeddingPullPollTimer = null;
          if (statusText) statusText.textContent = `خطا در دانلود: ${status.error}`;
          if (btnPull) btnPull.disabled = false;
          if (btnDismiss) btnDismiss.disabled = false;
        }
      } catch (err) {
        console.error("Polling error:", err);
      }
    }, 1500);
  } catch (err) {
    if (statusText) statusText.textContent = `خطا در ارسال درخواست: ${err.message}`;
    if (btnPull) btnPull.disabled = false;
    if (btnDismiss) btnDismiss.disabled = false;
  }
}

$("btnPullEmbedding")?.addEventListener("click", startPullingEmbedding);
$("btnDismissEmbedding")?.addEventListener("click", dismissEmbeddingModal);


function initPanelResizersAndToggles() {
  const layout = $("mainLayout") || document.querySelector(".layout");
  const leftPanel = $("workspacePanel") || document.querySelector(".workspace-panel");
  const rightPanel = $("agentPanel") || document.querySelector(".agent-panel");
  const resizerLeft = $("resizerLeft");
  const resizerRight = $("resizerRight");

  const toggleLeftBtn = $("toggleLeftSidebar");
  const toggleRightBtn = $("toggleRightSidebar");

  // Restore saved widths from localStorage
  try {
    const savedWidths = JSON.parse(localStorage.getItem("coderai_panel_widths") || "{}");
    if (savedWidths.left) {
      document.documentElement.style.setProperty("--left-panel-width", `${savedWidths.left}px`);
    }
    if (savedWidths.right) {
      document.documentElement.style.setProperty("--right-panel-width", `${savedWidths.right}px`);
    }
  } catch (_) {}

  function setLeftPanelVisible(visible) {
    if (leftPanel) leftPanel.classList.toggle("collapsed", !visible);
    if (resizerLeft) resizerLeft.classList.toggle("hidden", !visible);
    if (toggleLeftBtn) toggleLeftBtn.classList.toggle("active", visible);
    savePanelVisibility();
  }

  function setRightPanelVisible(visible) {
    if (rightPanel) rightPanel.classList.toggle("collapsed", !visible);
    if (resizerRight) resizerRight.classList.toggle("hidden", !visible);
    if (toggleRightBtn) toggleRightBtn.classList.toggle("active", visible);
    savePanelVisibility();
  }

  function savePanelVisibility() {
    try {
      const vis = {
        left: !leftPanel?.classList.contains("collapsed"),
        right: !rightPanel?.classList.contains("collapsed"),
      };
      localStorage.setItem("coderai_panel_visibility", JSON.stringify(vis));
    } catch (_) {}
  }

  // Restore saved visibility
  try {
    const savedVis = JSON.parse(localStorage.getItem("coderai_panel_visibility") || "{}");
    if (savedVis.left === false) {
      setLeftPanelVisible(false);
    }
    if (savedVis.right === false) {
      setRightPanelVisible(false);
    }
  } catch (_) {}

  // The TWO dedicated toggle button listeners
  if (toggleLeftBtn) {
    toggleLeftBtn.addEventListener("click", () => {
      const isCollapsed = leftPanel?.classList.contains("collapsed");
      setLeftPanelVisible(isCollapsed);
    });
  }

  if (toggleRightBtn) {
    toggleRightBtn.addEventListener("click", () => {
      const isCollapsed = rightPanel?.classList.contains("collapsed");
      setRightPanelVisible(isCollapsed);
    });
  }

  // Pointer-based draggable resizers
  function setupDraggableSplitter(resizer, onDrag) {
    if (!resizer) return;

    const onPointerMove = (e) => {
      onDrag(e);
    };

    const onPointerUp = () => {
      resizer.classList.remove("is-dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
    };

    resizer.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      resizer.classList.add("is-dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", onPointerUp);
    });
  }

  if (resizerLeft && layout) {
    setupDraggableSplitter(resizerLeft, (e) => {
      const layoutRect = layout.getBoundingClientRect();
      const newWidth = Math.max(220, Math.min(650, e.clientX - layoutRect.left));
      document.documentElement.style.setProperty("--left-panel-width", `${newWidth}px`);
      try {
        const widths = JSON.parse(localStorage.getItem("coderai_panel_widths") || "{}");
        widths.left = newWidth;
        localStorage.setItem("coderai_panel_widths", JSON.stringify(widths));
      } catch (_) {}
    });
  }

  if (resizerRight && layout) {
    setupDraggableSplitter(resizerRight, (e) => {
      const layoutRect = layout.getBoundingClientRect();
      const newWidth = Math.max(280, Math.min(850, layoutRect.right - e.clientX));
      document.documentElement.style.setProperty("--right-panel-width", `${newWidth}px`);
      try {
        const widths = JSON.parse(localStorage.getItem("coderai_panel_widths") || "{}");
        widths.right = newWidth;
        localStorage.setItem("coderai_panel_widths", JSON.stringify(widths));
      } catch (_) {}
    });
  }

  // Keyboard shortcuts (Ctrl+B for left panel, Ctrl+J for chat panel)
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b" && !e.shiftKey && !e.altKey) {
      if (document.activeElement?.tagName === "INPUT" || document.activeElement?.tagName === "TEXTAREA") {
        return;
      }
      e.preventDefault();
      const isCollapsed = leftPanel?.classList.contains("collapsed");
      setLeftPanelVisible(isCollapsed);
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "j" && !e.shiftKey && !e.altKey) {
      if (document.activeElement?.tagName === "INPUT" || document.activeElement?.tagName === "TEXTAREA") {
        return;
      }
      e.preventDefault();
      const isCollapsed = rightPanel?.classList.contains("collapsed");
      setRightPanelVisible(isCollapsed);
    }
  });
}

function setupSkillSlashAutocomplete() {
  const input = $("promptInput");
  const popover = $("skillAutocomplete");
  if (!input || !popover) return;

  let selectedIndex = 0;
  let activeMatches = [];
  let slashRange = null; // { start, end, query }

  function getAvailableSkills() {
    return (state.data?.skills || []).filter((s) => !s.disabled);
  }

  function getSlashContext() {
    const text = input.value;
    const caret = input.selectionStart;
    if (caret === undefined || caret === null) return null;

    const beforeCaret = text.slice(0, caret);
    const match = beforeCaret.match(/(?:^|\s)\/([a-zA-Z0-9_\-]*)$/);
    if (!match) return null;

    const slashIndex = beforeCaret.lastIndexOf("/");
    return {
      start: slashIndex,
      end: caret,
      query: match[1].toLowerCase(),
    };
  }

  function renderAutocompleteList() {
    const ctx = getSlashContext();
    if (!ctx) {
      closeAutocomplete();
      return;
    }

    slashRange = ctx;
    const allSkills = getAvailableSkills();
    activeMatches = allSkills.filter((s) => {
      if (!ctx.query) return true;
      return (
        s.name.toLowerCase().includes(ctx.query) ||
        (s.description && s.description.toLowerCase().includes(ctx.query)) ||
        (s.category && s.category.toLowerCase().includes(ctx.query))
      );
    });

    if (!activeMatches.length) {
      popover.innerHTML = `
        <div class="skill-autocomplete-header">Skills (0)</div>
        <div class="skill-autocomplete-empty">No skills matching "/${escapeHtml(ctx.query)}"</div>
      `;
      popover.hidden = false;
      return;
    }

    if (selectedIndex >= activeMatches.length) {
      selectedIndex = 0;
    } else if (selectedIndex < 0) {
      selectedIndex = activeMatches.length - 1;
    }

    popover.innerHTML = `
      <div class="skill-autocomplete-header">
        <span>Skills (${activeMatches.length})</span>
        <span>↑↓ Navigate · Enter / Click to select</span>
      </div>
      ${activeMatches.map((skill, index) => `
        <div class="skill-autocomplete-item ${index === selectedIndex ? "active" : ""}" data-index="${index}" data-skill="${escapeHtml(skill.name)}">
          <span class="skill-autocomplete-name">/${escapeHtml(skill.name)}</span>
          <span class="skill-autocomplete-desc">${escapeHtml(skill.description || "")}</span>
          <span class="skill-autocomplete-badge">${escapeHtml(skill.category || "skill")}</span>
        </div>
      `).join("")}
    `;
    popover.hidden = false;

    const activeEl = popover.querySelector(`.skill-autocomplete-item[data-index="${selectedIndex}"]`);
    if (activeEl) {
      activeEl.scrollIntoView({ block: "nearest" });
    }

    popover.querySelectorAll(".skill-autocomplete-item").forEach((item) => {
      const idx = parseInt(item.dataset.index, 10);
      item.addEventListener("mouseenter", () => {
        selectedIndex = idx;
        popover.querySelectorAll(".skill-autocomplete-item").forEach((el, i) => {
          el.classList.toggle("active", i === selectedIndex);
        });
      });
      item.addEventListener("mousedown", (e) => {
        e.preventDefault();
        selectSkill(activeMatches[idx]);
      });
    });
  }

  function selectSkill(skill) {
    if (!skill || !slashRange) return;
    const text = input.value;
    const before = text.slice(0, slashRange.start);
    const after = text.slice(slashRange.end);
    const insertion = `/${skill.name} `;
    input.value = before + insertion + after;
    const newCaret = before.length + insertion.length;
    input.focus();
    input.setSelectionRange(newCaret, newCaret);
    closeAutocomplete();
    updateTokenUsage();
  }

  function closeAutocomplete() {
    popover.hidden = true;
    activeMatches = [];
    slashRange = null;
    selectedIndex = 0;
  }

  input.addEventListener("input", () => {
    renderAutocompleteList();
  });

  input.addEventListener("click", () => {
    renderAutocompleteList();
  });

  input.addEventListener("blur", () => {
    setTimeout(() => {
      if (!popover.matches(":hover")) {
        closeAutocomplete();
      }
    }, 150);
  });

  input.addEventListener("keydown", (e) => {
    if (popover.hidden || !activeMatches.length) {
      return;
    }

    if (e.key === "ArrowDown") {
      e.preventDefault();
      selectedIndex = (selectedIndex + 1) % activeMatches.length;
      renderAutocompleteList();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      selectedIndex = (selectedIndex - 1 + activeMatches.length) % activeMatches.length;
      renderAutocompleteList();
    } else if (e.key === "Enter" || e.key === "Tab") {
      if (activeMatches[selectedIndex]) {
        e.preventDefault();
        selectSkill(activeMatches[selectedIndex]);
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeAutocomplete();
    }
  });
}

/// ── Interactive Graphify Knowledge Graph Subsystem (Vis-Network Physics Engine) ──

let graphifyState = {
  network: null,
  nodesDS: null,
  edgesDS: null,
  rawNodes: [],
  rawEdges: [],
  legend: [],
  stats: {},
  physicsEnabled: true,
  isFullscreen: false,
  lastWorkspace: null,
  hiddenCommunities: new Set(),
};

async function refreshSchematicGraph(force = false) {
  const currentWs = state.data?.workspace?.path;
  if (!currentWs) return;
  if (!force && graphifyState.lastWorkspace === currentWs && graphifyState.rawNodes.length > 0) {
    return;
  }
  graphifyState.lastWorkspace = currentWs;

  try {
    let data = null;
    try {
      data = await api("/api/graph/graphify-data");
    } catch (e) {
      console.warn("Graphify API error, falling back to /api/index/graph", e);
    }
    if (!data || !data.nodes || data.nodes.length === 0) {
      data = await api("/api/index/graph");
    }
    renderGraphifyNetwork(data);
  } catch (err) {
    console.error("Failed to load graphify graph:", err);
    const emptyEl = $("schematicGraphEmpty");
    if (emptyEl) {
      emptyEl.style.display = "flex";
      emptyEl.textContent = "Could not load knowledge graph";
    }
  }
}

function renderGraphifyNetwork(data) {
  const container = $("graphifyNetwork");
  const emptyEl = $("schematicGraphEmpty");
  if (!container) return;

  if (typeof vis === "undefined") {
    if (emptyEl) {
      emptyEl.style.display = "flex";
      emptyEl.textContent = "Graph visualizer library (vis-network) loading...";
    }
    setTimeout(() => {
      if (typeof vis !== "undefined") renderGraphifyNetwork(data);
    }, 500);
    return;
  }

  if (!data || !data.nodes || !data.nodes.length) {
    if (emptyEl) {
      emptyEl.style.display = "flex";
      emptyEl.textContent = "No graph relationships found";
    }
    if (graphifyState.network) {
      graphifyState.network.destroy();
      graphifyState.network = null;
    }
    return;
  }

  if (emptyEl) emptyEl.style.display = "none";

  // Normalize nodes if from legacy format
  const rawNodes = data.nodes.map((n) => {
    if (n.community !== undefined && n.size !== undefined) {
      return n;
    }
    const isFolder = n.type === "folder";
    const isFile = n.type === "file";
    const color = isFolder ? "#38bdf8" : isFile ? "#3b82f6" : "#10b981";
    return {
      id: n.id,
      label: n.label || n.id,
      title: `${n.label || n.id} (${n.type})`,
      color: {
        background: color,
        border: n.is_entry ? "#f43f5e" : "#ffffff",
        highlight: { background: color, border: "#38bdf8" },
      },
      size: isFolder ? 24 : isFile ? 20 : 14,
      font: { color: "#e0e0e0", size: 12, face: "Segoe UI, sans-serif" },
      community: 0,
      community_name: "Core",
      source_file: n.full_path || n.file_path || n.id,
      file_type: n.type,
      degree: 1,
      start_line: n.start_line || 1,
      end_line: n.end_line || 1,
    };
  });

  // Normalize edges if from legacy format
  const rawEdges = (data.edges || []).map((e, idx) => {
    if (e.from !== undefined && e.to !== undefined) {
      return e;
    }
    const relType = e.type || "imports";
    const edgeColor = relType === "calls" ? "#a855f7" : relType === "inherits" ? "#f59e0b" : "#3b82f6";
    return {
      from: e.source,
      to: e.target,
      label: "",
      title: `${relType} [${e.source} -> ${e.target}]`,
      dashes: relType === "defines" || relType === "contains",
      width: relType === "calls" ? 2 : 1.2,
      color: { color: edgeColor, opacity: 0.75, highlight: "#f43f5e" },
      arrows: { to: { enabled: true, scaleFactor: 0.5 } },
    };
  });

  graphifyState.rawNodes = rawNodes;
  graphifyState.rawEdges = rawEdges;
  graphifyState.legend = data.legend || [];
  graphifyState.stats = data.stats || {};
  graphifyState.hiddenCommunities.clear();

  // Create Vis-Network datasets
  const nodesDS = new vis.DataSet(
    rawNodes.map((n) => ({
      id: n.id,
      label: n.label,
      color: n.color,
      size: n.size || 18,
      font: n.font || { color: "#e0e0e0", size: 12 },
      title: n.title || n.label,
      shape: n.shape || "dot",
      borderWidth: 1.5,
      _raw: n,
    }))
  );

  const edgesDS = new vis.DataSet(
    rawEdges.map((e, i) => ({
      id: i,
      from: e.from,
      to: e.to,
      label: "",
      title: e.title || "",
      dashes: !!e.dashes,
      width: e.width || 1.2,
      color: e.color || { color: "#38bdf8", opacity: 0.7 },
      arrows: e.arrows || { to: { enabled: true, scaleFactor: 0.5 } },
    }))
  );

  graphifyState.nodesDS = nodesDS;
  graphifyState.edgesDS = edgesDS;

  const options = {
    physics: {
      enabled: true,
      solver: "forceAtlas2Based",
      forceAtlas2Based: {
        gravitationalConstant: -70,
        centralGravity: 0.008,
        springLength: 100,
        springConstant: 0.08,
        damping: 0.45,
        avoidOverlap: 0.85,
      },
      stabilization: {
        enabled: true,
        iterations: 180,
        fit: true,
      },
    },
    interaction: {
      hover: true,
      tooltipDelay: 100,
      hideEdgesOnDrag: true,
      navigationButtons: false,
      keyboard: false,
      zoomView: true,
      dragView: true,
    },
    nodes: {
      shape: "dot",
      borderWidth: 1.5,
    },
    edges: {
      smooth: {
        type: "continuous",
        roundness: 0.2,
      },
    },
  };

  if (graphifyState.network) {
    graphifyState.network.destroy();
  }

  const network = new vis.Network(container, { nodes: nodesDS, edges: edgesDS }, options);
  graphifyState.network = network;
  graphifyState.physicsEnabled = true;

  network.once("stabilizationIterationsDone", () => {
    network.setOptions({ physics: { enabled: false } });
    graphifyState.physicsEnabled = false;
    const pText = $("graphifyPhysicsText");
    if (pText) pText.textContent = "Physics";
    const pBtn = $("toggleGraphifyPhysics");
    if (pBtn) pBtn.classList.remove("active");
  });

  network.on("click", (params) => {
    if (params.nodes.length > 0) {
      showGraphifyNodeInfo(params.nodes[0]);
    }
  });

  network.on("doubleClick", (params) => {
    if (params.nodes.length > 0) {
      const nodeId = params.nodes[0];
      const nodeItem = nodesDS.get(nodeId);
      if (nodeItem && nodeItem._raw) {
        openNodeInEditor(nodeItem._raw);
      }
    } else {
      toggleSchematicFullscreen();
    }
  });

  renderGraphifyLegend(graphifyState.legend);
  updateGraphifyStatsFooter();
}

function showGraphifyNodeInfo(nodeId) {
  const contentEl = $("graphifyInspectorContent");
  if (!contentEl || !graphifyState.nodesDS || !graphifyState.network) return;

  const item = graphifyState.nodesDS.get(nodeId);
  if (!item) return;
  const n = item._raw || item;

  const neighborIds = graphifyState.network.getConnectedNodes(nodeId);
  const neighborItems = neighborIds.slice(0, 20).map((nid) => {
    const nb = graphifyState.nodesDS.get(nid);
    const label = nb ? nb.label : nid;
    return `<button class="neighbor-link" data-nid="${escapeHtml(String(nid))}">⤿ ${escapeHtml(label)}</button>`;
  }).join("");

  const typeName = n.file_type || n.type || "symbol";
  const commName = n.community_name || (n.community !== undefined ? `Cluster ${n.community}` : "Core");
  const sourcePath = n.source_file || n.full_path || n.file_path || n.id || "";
  const degree = n.degree || neighborIds.length || 0;

  contentEl.innerHTML = `
    <div class="inspector-node-title">${escapeHtml(n.label || n.id)}</div>
    <div class="inspector-badge-row">
      <span class="inspector-badge">${escapeHtml(typeName)}</span>
      <span class="inspector-badge" style="color:#a78bfa">${escapeHtml(commName)}</span>
      <span class="inspector-badge" style="color:#38bdf8">${degree} links</span>
    </div>
    <div class="inspector-path" title="Click to open file in editor" id="inspectorOpenPath">
      📄 ${escapeHtml(sourcePath)}
    </div>
    ${neighborIds.length > 0 ? `
      <div class="inspector-connections-title">Connections (${neighborIds.length}):</div>
      <div class="inspector-neighbor-list">${neighborItems}</div>
    ` : ""}
  `;

  const pathBtn = $("inspectorOpenPath");
  if (pathBtn) {
    pathBtn.addEventListener("click", () => {
      openNodeInEditor(n);
    });
  }

  contentEl.querySelectorAll(".neighbor-link").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const nid = btn.getAttribute("data-nid");
      if (nid) focusGraphifyNode(nid);
    });
  });
}

function focusGraphifyNode(nodeId) {
  if (!graphifyState.network || !graphifyState.nodesDS) return;
  graphifyState.network.focus(nodeId, {
    scale: 1.4,
    animation: {
      duration: 600,
      easingFunction: "easeInOutQuad",
    },
  });
  graphifyState.network.selectNodes([nodeId]);
  showGraphifyNodeInfo(nodeId);
}

function renderGraphifyLegend(legend) {
  const legendEl = $("graphifyCommunityLegend");
  if (!legendEl) return;

  if (!legend || !legend.length) {
    legendEl.innerHTML = `<span class="inspector-empty">No clusters detected</span>`;
    return;
  }

  legendEl.innerHTML = legend.map((c) => `
    <label class="community-legend-item" data-cid="${c.cid}">
      <input type="checkbox" checked data-cid="${c.cid}">
      <span class="community-swatch" style="background:${c.color}"></span>
      <span class="community-label" title="${escapeHtml(c.label)}">${escapeHtml(c.label)}</span>
      <span class="community-count">(${c.count})</span>
    </label>
  `).join("");

  legendEl.querySelectorAll("input[type='checkbox']").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      const cid = parseInt(cb.getAttribute("data-cid"), 10);
      const isChecked = cb.checked;
      if (isChecked) {
        graphifyState.hiddenCommunities.delete(cid);
      } else {
        graphifyState.hiddenCommunities.add(cid);
      }
      if (graphifyState.nodesDS && graphifyState.rawNodes) {
        const updates = graphifyState.rawNodes
          .filter((n) => n.community === cid)
          .map((n) => ({ id: n.id, hidden: !isChecked }));
        graphifyState.nodesDS.update(updates);
      }
    });
  });
}

function updateGraphifyStatsFooter() {
  const footer = $("graphifyStatsFooter");
  const nodesCount = graphifyState.rawNodes.length;
  const edgesCount = graphifyState.rawEdges.length;
  const commsCount = graphifyState.legend.length;
  if (footer) footer.textContent = `${nodesCount} nodes · ${edgesCount} edges · ${commsCount} clusters`;
  const nodeBadge = $("graphNodeCountBadge");
  const edgeBadge = $("graphEdgeCountBadge");
  const clusterBadge = $("graphClusterCountBadge");
  if (nodeBadge) nodeBadge.textContent = `${nodesCount} nodes`;
  if (edgeBadge) edgeBadge.textContent = `${edgesCount} edges`;
  if (clusterBadge) clusterBadge.textContent = `${commsCount} clusters`;
}

function toggleSchematicFullscreen() {
  const box = $("codeGraphView") || $("schematicGraphBox");
  if (!box) return;
  graphifyState.isFullscreen = !graphifyState.isFullscreen;
  box.classList.toggle("fullscreen", graphifyState.isFullscreen);

  let backdrop = document.querySelector(".schematic-backdrop");
  if (graphifyState.isFullscreen) {
    if (!backdrop) {
      backdrop = document.createElement("div");
      backdrop.className = "schematic-backdrop";
      backdrop.addEventListener("click", toggleSchematicFullscreen);
      document.body.appendChild(backdrop);
    }
    const exp = $("schematicExpandIcon");
    const cmp = $("schematicCompressIcon");
    if (exp) exp.style.display = "none";
    if (cmp) cmp.style.display = "block";
  } else {
    if (backdrop) backdrop.remove();
    const exp = $("schematicExpandIcon");
    const cmp = $("schematicCompressIcon");
    if (exp) exp.style.display = "block";
    if (cmp) cmp.style.display = "none";
  }

  setTimeout(() => {
    if (graphifyState.network) {
      graphifyState.network.fit({ animation: true });
    }
  }, 100);
}

async function openNodeInEditor(node) {
  if (!node) return;
  if (node.file_type === "folder" || node.type === "folder") {
    showWorkbench();
    setActiveActivity("Files");
    activateTab("files");
    return;
  }
  const filePath = node.source_file || node.full_path || (node.file_type === "file" ? node.id : node.file_path) || node.id;
  if (!filePath) return;

  const startLine = node.start_line || 1;
  const symbol = (node.file_type !== "file" && node.type !== "file") ? node.label : "";

  showWorkbench();
  setActiveActivity("Files");
  showEditorView("code");
  activateTab("files");

  await loadFile(filePath);

  setTimeout(() => {
    scrollEditorToLine(startLine, symbol);
  }, 100);
}

function initSchematicArchitectureGraph() {
  const searchInput = $("graphifySearchInput");
  const searchDropdown = $("graphifySearchResults");
  const physicsBtn = $("toggleGraphifyPhysics");
  const fitBtn = $("fitGraphifyGraph");
  const standaloneBtn = $("openGraphifyStandalone");
  const refreshBtn = $("refreshSchematicGraph");
  const fullscreenBtn = $("toggleSchematicFullscreen");

  if (physicsBtn) {
    physicsBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (!graphifyState.network) return;
      graphifyState.physicsEnabled = !graphifyState.physicsEnabled;
      graphifyState.network.setOptions({ physics: { enabled: graphifyState.physicsEnabled } });
      const pText = $("graphifyPhysicsText");
      if (pText) {
        pText.textContent = graphifyState.physicsEnabled ? "Physics (Live)" : "Physics";
      }
      physicsBtn.classList.toggle("active", graphifyState.physicsEnabled);
    });
  }

  if (fitBtn) {
    fitBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (graphifyState.network) {
        graphifyState.network.fit({ animation: { duration: 500, easingFunction: "easeInOutQuad" } });
      }
    });
  }

  if (standaloneBtn) {
    standaloneBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      window.open("/api/graph/graphify.html", "_blank");
    });
  }

  if (refreshBtn) {
    refreshBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      refreshSchematicGraph(true);
    });
  }

  if (fullscreenBtn) {
    fullscreenBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleSchematicFullscreen();
    });
  }

  const closeBtn = $("closeGraphViewBtn");
  if (closeBtn) {
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      showWorkbench();
      setActiveActivity("Files");
      activateTab("files");
    });
  }

  if (searchInput && searchDropdown) {
    searchInput.addEventListener("input", () => {
      const q = searchInput.value.trim().toLowerCase();
      if (!q) {
        searchDropdown.style.display = "none";
        searchDropdown.innerHTML = "";
        return;
      }
      const matches = (graphifyState.rawNodes || [])
        .filter((n) => (n.label || "").toLowerCase().includes(q) || (n.id || "").toLowerCase().includes(q))
        .slice(0, 15);

      if (!matches.length) {
        searchDropdown.style.display = "block";
        searchDropdown.innerHTML = `<div style="padding:6px;color:var(--muted);font-size:11px;text-align:center">No matching symbols</div>`;
        return;
      }

      searchDropdown.style.display = "block";
      searchDropdown.innerHTML = matches.map((m) => `
        <div class="graphify-search-item" data-nid="${escapeHtml(String(m.id))}">
          <span class="graphify-search-item-label">${escapeHtml(m.label || m.id)}</span>
          <span class="graphify-search-item-type">${escapeHtml(m.file_type || m.type || "file")}</span>
        </div>
      `).join("");
    });

    searchDropdown.addEventListener("click", (e) => {
      const item = e.target.closest(".graphify-search-item");
      if (item && item.dataset.nid) {
        focusGraphifyNode(item.dataset.nid);
        searchDropdown.style.display = "none";
      }
    });

    document.addEventListener("click", (e) => {
      if (!e.target.closest(".graphify-search-wrap")) {
        searchDropdown.style.display = "none";
      }
    });
  }

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && graphifyState.isFullscreen) {
      toggleSchematicFullscreen();
    }
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// ── Real Integrated xterm.js Terminal Controller (VS Code Engine)
// ══════════════════════════════════════════════════════════════════════════════
const terminalState = {
  sessionId: "default",
  shellType: "powershell",
  isOpen: false,
  isMaximized: false,
  hasRenderedOutput: false,
  lastCols: 0,
  lastRows: 0,
  xterm: null,
  fitAddon: null,
  webLinksAddon: null,
  socket: null,
  reconnectTimer: null,
  heartbeatTimer: null,
};

function initXtermTerminal() {
  const container = $("xtermContainer");
  if (!container) return;

  if (typeof Terminal === "undefined") {
    console.error("xterm.js is not loaded yet.");
    return;
  }

  if (terminalState.xterm) {
    try { terminalState.xterm.dispose(); } catch (_) {}
    terminalState.xterm = null;
  }

  // Create real xterm.js instance with VS Code theme
  terminalState.xterm = new Terminal({
    cursorBlink: true,
    cursorStyle: "block",
    fontSize: 13,
    fontFamily: 'Consolas, "Cascadia Code", "Courier New", monospace',
    lineHeight: 1.25,
    scrollback: 5000,
    theme: {
      background: "#181818",
      foreground: "#cccccc",
      cursor: "#aeafad",
      cursorAccent: "#181818",
      selectionBackground: "#264f78",
      black: "#000000",
      red: "#cd3131",
      green: "#0dbc79",
      yellow: "#e5e510",
      blue: "#2472c8",
      magenta: "#bc3fbc",
      cyan: "#11a8cd",
      white: "#e5e5e5",
      brightBlack: "#666666",
      brightRed: "#f14c4c",
      brightGreen: "#23d18b",
      brightYellow: "#f5f543",
      brightBlue: "#3b8eea",
      brightMagenta: "#d670d6",
      brightCyan: "#29b8db",
      brightWhite: "#e5e5e5",
    },
  });

  if (typeof FitAddon !== "undefined" && FitAddon.FitAddon) {
    terminalState.fitAddon = new FitAddon.FitAddon();
    terminalState.xterm.loadAddon(terminalState.fitAddon);
  }

  if (typeof WebLinksAddon !== "undefined" && WebLinksAddon.WebLinksAddon) {
    terminalState.webLinksAddon = new WebLinksAddon.WebLinksAddon();
    terminalState.xterm.loadAddon(terminalState.webLinksAddon);
  }

  terminalState.xterm.open(container);

  // Send keystrokes directly over WebSocket
  terminalState.xterm.onData((data) => {
    if (!data) return;
    // Filter out escape responses automatically generated by xterm (DA queries, focus reporting)
    if (data.startsWith("\x1b[?") && data.endsWith("c")) return;
    if (data.includes("?1;2c") || data.includes("?1;0c") || data === "\x1b[c") return;
    if (data === "\x1b[I" || data === "\x1b[O") return;

    if (terminalState.socket && terminalState.socket.readyState === WebSocket.OPEN) {
      terminalState.socket.send(JSON.stringify({ type: "input", data: data }));
    }
  });

  // Resize handler using FitAddon with debouncing
  let fitDebounceTimer = null;
  const triggerFit = () => {
    if (fitDebounceTimer) clearTimeout(fitDebounceTimer);
    fitDebounceTimer = setTimeout(() => {
      if (terminalState.isOpen && terminalState.fitAddon && terminalState.xterm && container.clientHeight > 0 && container.clientWidth > 0) {
        try {
          terminalState.fitAddon.fit();
          const cols = (terminalState.xterm.cols && terminalState.xterm.cols > 0) ? terminalState.xterm.cols : 80;
          const rows = (terminalState.xterm.rows && terminalState.xterm.rows > 0) ? terminalState.xterm.rows : 24;
          if (cols === terminalState.lastCols && rows === terminalState.lastRows) {
            return;
          }
          terminalState.lastCols = cols;
          terminalState.lastRows = rows;
          if (terminalState.socket && terminalState.socket.readyState === WebSocket.OPEN) {
            terminalState.socket.send(JSON.stringify({
              type: "resize",
              cols: cols,
              rows: rows,
            }));
          }
        } catch (_) {}
      }
    }, 120);
  };

  if (!terminalState.hasResizeListener) {
    terminalState.hasResizeListener = true;
    window.addEventListener("resize", triggerFit);
  }

  if (window.ResizeObserver && !terminalState.hasResizeObserver) {
    terminalState.hasResizeObserver = true;
    const ro = new ResizeObserver(triggerFit);
    ro.observe(container);
  }
}

function disconnectTerminalWebSocket() {
  if (terminalState.reconnectTimer) {
    clearTimeout(terminalState.reconnectTimer);
    terminalState.reconnectTimer = null;
  }
  if (terminalState.heartbeatTimer) {
    clearInterval(terminalState.heartbeatTimer);
    terminalState.heartbeatTimer = null;
  }
  if (terminalState.socket) {
    try {
      terminalState.socket.onopen = null;
      terminalState.socket.onmessage = null;
      terminalState.socket.onerror = null;
      terminalState.socket.onclose = null;
      terminalState.socket.close(1000, "Client closed");
    } catch (_) {}
    terminalState.socket = null;
  }
}

function connectTerminalWebSocket() {
  if (!terminalState.isOpen) {
    return;
  }

  if (terminalState.reconnectTimer) {
    clearTimeout(terminalState.reconnectTimer);
    terminalState.reconnectTimer = null;
  }

  if (terminalState.socket) {
    if (terminalState.socket.readyState === WebSocket.OPEN || terminalState.socket.readyState === WebSocket.CONNECTING) {
      return;
    }
    disconnectTerminalWebSocket();
  }

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const replayParam = terminalState.hasRenderedOutput ? "&replay=0" : "&replay=1";
  const wsUrl = `${protocol}//${window.location.host}/api/terminal/ws?session_id=${terminalState.sessionId}&shell=${encodeURIComponent(terminalState.shellType)}${replayParam}`;

  const ws = new WebSocket(wsUrl);
  terminalState.socket = ws;

  ws.onopen = () => {
    if (terminalState.socket !== ws) return;

    // Start keepalive heartbeat every 10s to prevent idle disconnect
    if (terminalState.heartbeatTimer) clearInterval(terminalState.heartbeatTimer);
    terminalState.heartbeatTimer = setInterval(() => {
      if (terminalState.socket === ws && ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify({ type: "ping" }));
        } catch (_) {}
      }
    }, 10000);

    if (terminalState.fitAddon && terminalState.xterm && terminalState.isOpen) {
      try {
        terminalState.fitAddon.fit();
        const cols = (terminalState.xterm.cols && terminalState.xterm.cols > 0) ? terminalState.xterm.cols : 80;
        const rows = (terminalState.xterm.rows && terminalState.xterm.rows > 0) ? terminalState.xterm.rows : 24;
        terminalState.lastCols = cols;
        terminalState.lastRows = rows;
        ws.send(JSON.stringify({
          type: "resize",
          cols: cols,
          rows: rows,
        }));
      } catch (_) {}
    }
  };

  ws.onmessage = (event) => {
    if (terminalState.socket !== ws) return;
    if (!event.data) return;
    // Filter out internal keepalive pong
    if (typeof event.data === "string" && event.data.startsWith("{") && event.data.includes('"pong"')) {
      return;
    }
    if (terminalState.xterm) {
      terminalState.hasRenderedOutput = true;
      terminalState.xterm.write(event.data);
    }
  };

  ws.onerror = (err) => {
    console.warn("Terminal WebSocket error:", err);
  };

  ws.onclose = (event) => {
    if (terminalState.heartbeatTimer) {
      clearInterval(terminalState.heartbeatTimer);
      terminalState.heartbeatTimer = null;
    }
    if (terminalState.socket === ws) {
      terminalState.socket = null;
    }
    // Only reconnect if terminal remains open and connection was not closed intentionally
    if (terminalState.isOpen && event && event.code !== 1000) {
      terminalState.reconnectTimer = setTimeout(() => {
        if (terminalState.isOpen && (!terminalState.socket || terminalState.socket.readyState === WebSocket.CLOSED)) {
          connectTerminalWebSocket();
        }
      }, 3000);
    }
  };
}

function toggleTerminal(forceOpen = null) {
  const panel = $("terminalPanel");
  const splitter = $("terminalSplitter");
  if (!panel) return;

  const isCurrentlyOpen = !panel.classList.contains("collapsed");
  const shouldOpen = forceOpen !== null ? forceOpen : !isCurrentlyOpen;

  panel.classList.toggle("collapsed", !shouldOpen);
  if (splitter) splitter.classList.toggle("collapsed", !shouldOpen);
  terminalState.isOpen = shouldOpen;

  if (shouldOpen) {
    if (!terminalState.xterm) {
      initXtermTerminal();
    }
    connectTerminalWebSocket();
    if (terminalState.fitAddon) {
      setTimeout(() => {
        try {
          terminalState.fitAddon.fit();
          terminalState.xterm?.focus();
        } catch (_) {}
      }, 50);
    }
  }
}

function initIntegratedTerminal() {
  const panel = $("terminalPanel");
  const splitter = $("terminalSplitter");
  const shellSelect = $("terminalShellSelect");
  const toggleBtn = $("toggleTerminalBtn");
  const clearBtn = $("terminalClearBtn");
  const killBtn = $("terminalKillBtn");
  const maxBtn = $("terminalMaximizeBtn");
  const closeBtn = $("terminalCloseBtn");

  if (!panel || !splitter) return;

  // Toggle button in topbar
  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => toggleTerminal());
  }

  // Shell selector
  if (shellSelect) {
    shellSelect.addEventListener("change", (e) => {
      terminalState.shellType = e.target.value;
      terminalState.hasRenderedOutput = false;
      terminalState.xterm?.reset();
      if (terminalState.socket && terminalState.socket.readyState === WebSocket.OPEN) {
        terminalState.socket.send(JSON.stringify({ type: "restart", shell: terminalState.shellType }));
      } else {
        connectTerminalWebSocket();
      }
      terminalState.xterm?.focus();
    });
  }

  // Clear button (Ctrl+L)
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      terminalState.hasRenderedOutput = false;
      if (terminalState.xterm) {
        terminalState.xterm.clear();
        terminalState.xterm.focus();
      }
    });
  }

  // Kill button (Ctrl+C / 0x03)
  if (killBtn) {
    killBtn.addEventListener("click", () => {
      if (terminalState.socket && terminalState.socket.readyState === WebSocket.OPEN) {
        terminalState.socket.send(JSON.stringify({ type: "kill" }));
        terminalState.socket.send(JSON.stringify({ type: "input", data: "\x03" }));
        terminalState.xterm?.focus();
      }
    });
  }

  // Maximize / Restore button
  if (maxBtn) {
    maxBtn.addEventListener("click", () => {
      terminalState.isMaximized = !terminalState.isMaximized;
      panel.classList.toggle("maximized", terminalState.isMaximized);
      if (terminalState.isMaximized) {
        panel.style.height = "";
      } else {
        const savedHeight = getComputedStyle(panel).getPropertyValue("--terminal-height").trim() || "220px";
        panel.style.height = savedHeight;
      }
      if (terminalState.fitAddon) {
        setTimeout(() => {
          try { terminalState.fitAddon.fit(); } catch (_) {}
        }, 60);
      }
    });
  }

  // Close button
  if (closeBtn) closeBtn.addEventListener("click", () => toggleTerminal(false));

  // Global shortcut: Ctrl+` (backtick) toggles terminal
  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "`" || e.key === "~")) {
      e.preventDefault();
      toggleTerminal();
    }
  });

  // Vertical pointer-based splitter drag for terminal height
  splitter.addEventListener("dblclick", (e) => {
    e.preventDefault();
    terminalState.isMaximized = !terminalState.isMaximized;
    panel.classList.toggle("maximized", terminalState.isMaximized);
    if (terminalState.isMaximized) {
      panel.style.height = "";
    } else {
      const savedHeight = getComputedStyle(panel).getPropertyValue("--terminal-height").trim() || "220px";
      panel.style.height = savedHeight;
    }
    if (terminalState.fitAddon) {
      setTimeout(() => {
        try { terminalState.fitAddon.fit(); } catch (_) {}
      }, 60);
    }
  });

  let isDraggingSplitter = false;
  let startY = 0;
  let startHeight = 220;

  const onPointerMove = (e) => {
    if (!isDraggingSplitter) return;
    const deltaY = startY - e.clientY; // dragging up increases terminal height
    const containerHeight = panel.parentElement ? panel.parentElement.getBoundingClientRect().height : window.innerHeight;
    const minHeight = 60;
    const maxHeight = Math.max(minHeight, containerHeight - 120);
    const newHeight = Math.max(minHeight, Math.min(maxHeight, startHeight + deltaY));
    panel.style.setProperty("--terminal-height", `${newHeight}px`);
    panel.style.height = `${newHeight}px`;
    terminalState.isMaximized = false;
    panel.classList.remove("maximized");
    if (terminalState.fitAddon) {
      try { terminalState.fitAddon.fit(); } catch (_) {}
    }
  };

  const onPointerUp = (e) => {
    if (isDraggingSplitter) {
      isDraggingSplitter = false;
      splitter.classList.remove("is-dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      try {
        if (splitter.hasPointerCapture && splitter.hasPointerCapture(e.pointerId)) {
          splitter.releasePointerCapture(e.pointerId);
        }
      } catch (_) {}
      if (terminalState.fitAddon) {
        try { terminalState.fitAddon.fit(); } catch (_) {}
      }
    }
  };

  splitter.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    isDraggingSplitter = true;
    startY = e.clientY;
    const rect = panel.getBoundingClientRect();
    startHeight = rect.height > 0 ? rect.height : 220;
    splitter.classList.add("is-dragging");
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    try {
      splitter.setPointerCapture(e.pointerId);
    } catch (_) {}
    e.preventDefault();
  });

  splitter.addEventListener("pointermove", onPointerMove);
  splitter.addEventListener("pointerup", onPointerUp);
  splitter.addEventListener("pointercancel", onPointerUp);
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);

  // Initialize xterm instance
  initXtermTerminal();

  // If terminal panel is open in the DOM, connect and focus
  const isPanelOpen = !panel.classList.contains("collapsed");
  terminalState.isOpen = isPanelOpen;
  if (isPanelOpen) {
    connectTerminalWebSocket();
    setTimeout(() => {
      try {
        terminalState.fitAddon?.fit();
        terminalState.xterm?.focus();
      } catch (_) {}
    }, 150);
  }

  // Click anywhere on terminal body or container to focus xterm
  $("terminalBody")?.addEventListener("click", () => {
    try { terminalState.xterm?.focus(); } catch (_) {}
  });
}

initPanelResizersAndToggles();
setupSkillSlashAutocomplete();
initSchematicArchitectureGraph();
initIntegratedTerminal();
initApprovalPoliciesUI();
installEditorMetricStyles();
updateGitAuthPanel();
refresh().catch((err) => {
  $("messages").innerHTML = `<article class="message assistant"><span class="role">error</span>${escapeHtml(err.message)}</article>`;
}).then(() => {
  checkEmbeddingModelStatus();
  const currentActivity = document.querySelector(".activity-btn.active")?.getAttribute("aria-label");
  if (currentActivity && currentActivity !== "Projects") {
    state.initialProjectsShown = true;
  }
  if (!state.initialProjectsShown && !state.userNavigated) {
    state.initialProjectsShown = true;
    const hasWorkspace = Boolean(state.data?.workspace?.path);
    if (!hasWorkspace) {
      setActiveActivity("Projects");
      showProjects().catch(() => showWorkbench());
    } else {
      showWorkbench();
    }
  }
});
