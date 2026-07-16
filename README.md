# Ollama Agentic Workspace

Ollama Agentic Workspace is a local-first agentic coding environment for working with multiple projects on your machine. It provides a dedicated Projects dashboard, project and session archives, a three-pane coding workspace, Git-aware editing, and an agent that can inspect and modify the selected project through tool calls.

The app is built as a small Python HTTP server with a custom HTML/CSS/JavaScript frontend.

## Download The App

If you do not want to work with the source code or install Python, download the Windows executable from the GitHub Releases page:

[Download from Releases](https://github.com/mohamadreza1368/coderAI/releases)

Use the portable executable if you want the simplest option. The portable version is a single `.exe` file, so you can download it, run it, and start using the app without setting up a Python environment.

The folder-based package may start faster and is usually better for future installer-style distribution, but it must be kept together with its included support files. For most users, the portable version is easier.

## Features

- Local Ollama model support with automatic model discovery.
- OpenAI-compatible Custom API mode.
- Project workspace browser with file preview.
- Dedicated Projects dashboard with workspace cards, Git state, file statistics, session counts, recent activity, and agent status.
- Project Home views with Overview, Sessions, Files, Memory, Skills, and Settings tabs.
- Git-backed agent checkpoints with reviewable diffs, automatic scoped commits, history, and safe revert commits.
- Remote Git connection with streamed cloning, system/SSH/PAT authentication, push previews, and explicit approval before publishing changes.
- Native folder picker for selecting a local project directory.
- Agent tool calling for file reads, writes, replacement, search, project scans, shell commands, and Python snippets.
- Optional Tavily web search tools, controlled from the Settings UI.
- System prompt manager backed by Markdown files in `system_prompts/`.
- Skill manager backed by `skills/**/SKILL.md`.
- LangChain-backed model runtime for Ollama and OpenAI-compatible APIs, with a local HTTP fallback.
- Streaming chat responses and live generated-code preview.
- Context window usage meter with token estimates and percentage warnings.
- Download button for generated or selected code.
- Context compaction and LiteLLM-based token counting when available.
- Central persistent archive with project-scoped conversations, searchable semantic facts, and user preferences.

## Project Structure

```text
.
├── web_app.py                  # Local HTTP server and agent loop
├── agent_runtime.py            # LangChain model runtime adapter
├── launcher.py                 # EXE-friendly launcher that opens the browser
├── tools.py                    # Tool schemas and tool execution handlers
├── git_manager.py              # Git clone, diff, checkpoint, commit, push, and revert layer
├── memory_manager.py           # SQLite episodic, semantic, and procedural memory
├── config.py                   # Runtime configuration and environment defaults
├── prompt_manager.py           # System prompt discovery and loading
├── skills_manager.py           # Skill discovery, selection, and usage parsing
├── web_ui/                     # Frontend HTML, CSS, and JavaScript
├── system_prompts/             # Markdown system prompts
├── skills/                     # Skill definitions
├── tests/                      # Runtime and Git integration tests
└── agent_workspace/            # Default workspace when no project is selected
```

## Requirements

For the portable release:

- Windows
- Ollama installed and running for Local Ollama mode, unless you use Custom API mode

For source development:

- Windows
- Python 3.12+
- Ollama installed and running for Local Ollama mode
- Python packages from `requirements.txt`

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Running From Source

```powershell
python web_app.py
```

Then open:

```text
http://127.0.0.1:7864/
```

If `python` is not on PATH, run the script with the full path to your Python executable.

## Local Ollama

Local mode expects Ollama to be reachable at:

```text
http://127.0.0.1:11434
```

The app reads installed Ollama models from `/api/tags` and sends chat requests to `/api/chat`.

## Custom API Mode

Use `Custom API` in the top bar when you want an OpenAI-compatible endpoint. The UI provides fields for:

- API Base URL
- API Key
- Model selection

## Tavily Web Search

Tavily is disabled by default. To enable web search:

1. Open the `Settings` tab.
2. Turn on `Tavily web search`.
3. Enter your Tavily API key.
4. Save settings.

When Tavily is disabled or no key is configured, the `web_search` and `extract_url` tools are not exposed to the model.

## Git Integration

Open `Git History` in the editor panel to work with a local or remote repository.

To clone a repository:

1. Enter its HTTPS or SSH URL.
2. Select a parent folder. The repository name is added automatically when the selected folder already contains files.
3. Choose an authentication method: system Git credentials, SSH key, or username and personal access token.
4. Select `Clone repository` and follow the live command output.

Agent file changes are shown as a diff before they are written when approval mode is enabled. Approved changes are committed as scoped checkpoints. Before publishing, `Push changes` shows the branch and commits that will be sent to `origin`; the push runs only after explicit confirmation.

Personal access tokens are used only for the active clone or push process. They are not stored in the remote URL, repository configuration, or application state.

## Persistent Memory

CoderAI keeps one central archive beside the application so every workspace and chat session is available from the Projects dashboard and Memory view:

```text
coderai_data/
├── memory.db                   # SQLite conversations, facts, preferences, and session summaries
├── vectors/                    # Reserved embedded vector-index storage
└── facts.jsonl                 # Append-only semantic-memory audit log
```

Memory is separated into three layers:

- Episodic memory stores chronological conversation turns and tool-call metadata between sessions.
- Semantic memory stores durable project facts and retrieves up to five relevant items with SQLite FTS5/BM25.
- Procedural memory stores small user preferences that are always included when memory is enabled.

The agent never injects the entire database into the model context. It combines bounded recent turns, older session summaries, preferences, and only the project facts relevant to the current request. The chat stream reports how many facts were used.

Open `Projects` from the left activity rail to browse workspace cards and enter a Project Home. Each Project Home provides separate tabs for overview statistics, resumable sessions, source files, persistent memory, skills, and settings. The standalone `Memory` view remains available for editing semantic facts and preferences. Project data stays isolated by workspace even though all projects share one central database. `Forget project` removes only the active project's conversations, facts, and preferences after explicit confirmation.

In source mode, `coderai_data/` is created beside the application source. In packaged builds it is created beside the executable. Set `CODERAI_DATA_DIR` to store the archive elsewhere.

## TODO

- Add per-tool and per-workspace approval policies.
- Add pull, fetch, branch switching, and merge-conflict assistance to Git History.
- Improve incremental workspace indexing for large repositories.
- Add richer skill validation, diagnostics, and execution traces.
- Add optional local embedding providers and `sqlite-vec` retrieval for deeper semantic matching.
- Add opt-in background fact extraction with review before facts become durable memory.
- Add end-to-end browser tests for clone, diff approval, checkpoint, revert, and push flows.
- Automate signed Windows release builds and GitHub Release publishing.

## License

This project is licensed under the Apache License 2.0.

Please retain the attribution in the `NOTICE` file when redistributing this project or derivative works.

## Development Notes

- `launcher.py` finds a free local port starting at `7864` and opens the browser automatically.
- LangChain is used as the preferred model runtime for Local Ollama and OpenAI-compatible Custom API mode.
- If LangChain provider packages are unavailable, the app falls back to the built-in HTTP runtime.
- Set `AGENT_USE_LANGCHAIN=false` to force the built-in HTTP runtime during debugging.
- Streaming uses the built-in HTTP runtime by default for stability; set `AGENT_USE_LANGCHAIN_STREAMING=true` to test LangChain streaming.
- LiteLLM is used only for token counting. If it is unavailable, the app falls back to conservative character-based estimates.
- `LITELLM_LOCAL_MODEL_COST_MAP=True` is set to prevent LiteLLM from trying to refresh its model-cost map from the internet.
- Tool access is constrained to the selected workspace where file operations are involved.
- Git integration is isolated in `git_manager.py`. Agent file writes produce a dry-run diff, optionally wait for UI approval, then commit only the files changed by that tool call.
- Git commands run without a console window on Windows. Non-Git workspaces are never initialized automatically; the UI asks first.
- PAT credentials are passed to Git only through the child-process environment for the current clone or push operation. They are not stored in the repository URL or application state.

## Security Notes

- Tavily and Custom API keys are user-provided runtime settings.
- Shell and Python execution tools run on the local machine inside the selected workspace. Only use this app with projects and prompts you trust.
