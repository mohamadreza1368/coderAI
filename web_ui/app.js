const state = {
  data: null,
  activeFile: null,
  fileContent: "",
  generatedInfo: "txt",
};

const $ = (id) => document.getElementById(id);

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

function setCodePreview(title, meta, content, info = "txt") {
  state.activeFile = title;
  state.fileContent = content || "";
  state.generatedInfo = info || "txt";
  $("activeFile").textContent = title;
  $("fileMeta").textContent = meta;
  $("codePreview").innerHTML = `<code>${escapeHtml(state.fileContent.slice(0, 80000))}${state.fileContent.length > 80000 ? "\n... [truncated]" : ""}</code>`;
  $("attachFile").disabled = !state.fileContent;
  $("downloadCode").disabled = !state.fileContent;
}

function renderState(data) {
  state.data = data;
  const ws = data.workspace;
  $("workspacePath").textContent = ws.path;
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
  $("tavilyApiKey").value = "";
  updateTavilyPanel(data.settings);
  const memory = data.memory || {};
  $("memoryStats").textContent = `Memory: ${memory.summarized_messages || 0} compacted · ${memory.summary_chars || 0} chars · ${memory.token_counter || "estimated"}`;
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
  renderMessages();
  renderGeneratedCodeFromMessages();
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
        ${escapeHtml(msg.content || "")}
        ${toolHtml}
      </article>
    `;
  }).join("");
  $("messages").scrollTop = $("messages").scrollHeight;
}

function extractCodeBlocks(text) {
  const blocks = [];
  const re = /```([^\n`]*)\n([\s\S]*?)```/g;
  let match;
  while ((match = re.exec(text || "")) !== null) {
    const info = (match[1] || "text").trim();
    const code = (match[2] || "").trimEnd();
    if (code.trim()) {
      blocks.push({ info, code });
    }
  }
  return blocks;
}

function renderGeneratedCodeFromMessages() {
  const messages = state.data?.messages || [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role !== "assistant") continue;
    const blocks = extractCodeBlocks(messages[i].content);
    if (!blocks.length) continue;
    const block = blocks[blocks.length - 1];
    setCodePreview(
      "Generated Code",
      `${block.info || "text"} · ${block.code.length.toLocaleString()} chars · extracted from latest agent response`,
      block.code,
      block.info || "txt"
    );
    return;
  }
}

function setLoading(isLoading, text = "Working...") {
  document.body.classList.toggle("loading", isLoading);
  $("loadingText").textContent = text;
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
  const blocks = extractCodeBlocks(text);
  if (!blocks.length) return;
  const block = blocks[blocks.length - 1];
  setCodePreview(
    "Generated Code",
    `${block.info || "text"} · live stream · ${block.code.length.toLocaleString()} chars`,
    block.code,
    block.info || "txt"
  );
}

async function loadFile(path) {
  const data = await api(`/api/file?path=${encodeURIComponent(path)}`);
  state.activeFile = path;
  state.fileContent = data.content;
  state.generatedInfo = data.ext || "txt";
  $("activeFile").textContent = path;
  $("fileMeta").textContent = `${data.ext} · ${formatSize(data.size)} · ${data.content.length.toLocaleString()} chars`;
  $("codePreview").innerHTML = `<code>${escapeHtml(data.content.slice(0, 50000))}${data.content.length > 50000 ? "\n... [truncated]" : ""}</code>`;
  $("attachFile").disabled = false;
  $("downloadCode").disabled = false;
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
    $("workspaceInput").value = data.path || data.workspace?.path || $("workspaceInput").value;
    renderState(await api("/api/state"));
  } catch (err) {
    alert(err.message || "No folder selected");
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
  setLoading(true, "Scanning project...");
  try {
    const data = await api("/api/scan", { method: "POST", body: JSON.stringify({ max_files: 250 }) });
    renderState(data.state);
  } finally {
    setLoading(false);
  }
}

async function sendPrompt(prompt) {
  setLoading(true, "Sending prompt...");
  let streamTarget = null;
  let streamText = "";
  const activeContext = currentActiveContext();
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
        const event = JSON.parse(line);
        if (event.type === "state") {
          renderState(event.state);
        } else if (event.type === "status") {
          setLoading(true, event.message);
        } else if (event.type === "token") {
          if (!streamTarget) streamTarget = appendStreamingAssistant();
          streamText += event.content || "";
          streamTarget.textContent = streamText;
          updateGeneratedCodeFromStreaming(streamText);
          $("messages").scrollTop = $("messages").scrollHeight;
        } else if (event.type === "tool_call") {
          setLoading(true, `Running ${event.name}...`);
          appendToolStatus(event.name, JSON.stringify(event.args || {}, null, 2));
        } else if (event.type === "tool_result") {
          appendToolStatus(`${event.name} result`, String(event.result || "").slice(0, 1200));
        } else if (event.type === "error") {
          if (!streamTarget) streamTarget = appendStreamingAssistant();
          streamTarget.textContent = event.message || "Error";
        } else if (event.type === "done") {
          renderState(event.state);
        }
      }
    }
  } finally {
    setLoading(false);
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tab-page").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $(`${tab.dataset.tab}Tab`).classList.add("active");
  });
});

$("fileSearch").addEventListener("input", renderFiles);
$("promptSearch").addEventListener("input", renderPrompts);
$("openWorkspace").addEventListener("click", openWorkspace);
$("browseWorkspace").addEventListener("click", browseWorkspace);
$("saveSettings").addEventListener("click", saveSettings);
$("compactMemory").addEventListener("click", compactMemory);
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
$("chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = $("promptInput").value.trim();
  if (!prompt) return;
  $("promptInput").value = "";
  await sendPrompt(prompt);
});

refresh().catch((err) => {
  $("messages").innerHTML = `<article class="message assistant"><span class="role">error</span>${escapeHtml(err.message)}</article>`;
});
