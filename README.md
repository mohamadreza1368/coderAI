# Ollama Agentic Workspace

Ollama Agentic Workspace is a local, browser-based coding agent UI for working with projects on your machine. It provides a three-pane workspace for browsing files, previewing or downloading generated code, and chatting with an agent that can inspect and edit the selected project through tool calls.

The app is built as a small Python HTTP server with a custom HTML/CSS/JavaScript frontend. It can run directly from source or be packaged as a Windows executable with PyInstaller.

## Features

- Local Ollama model support with automatic model discovery.
- OpenAI-compatible Custom API mode.
- Project workspace browser with file preview.
- Native folder picker for selecting a local project directory.
- Agent tool calling for file reads, writes, replacement, search, project scans, shell commands, and Python snippets.
- Optional Tavily web search tools, controlled from the Settings UI.
- System prompt manager backed by Markdown files in `system_prompts/`.
- Skill manager backed by `skills/**/SKILL.md`.
- Streaming chat responses and live generated-code preview.
- Download button for generated or selected code.
- Context compaction and LiteLLM-based token counting when available.
- Portable Windows executable builds.

## Project Structure

```text
.
├── web_app.py                  # Local HTTP server and agent runtime
├── launcher.py                 # EXE-friendly launcher that opens the browser
├── tools.py                    # Tool schemas and tool execution handlers
├── config.py                   # Runtime configuration and environment defaults
├── prompt_manager.py           # System prompt discovery and loading
├── skills_manager.py           # Skill discovery, selection, and usage parsing
├── web_ui/                     # Frontend HTML, CSS, and JavaScript
├── system_prompts/             # Markdown system prompts
├── skills/                     # Skill definitions
├── agent_workspace/            # Default workspace when no project is selected
└── dist/                       # Generated executable builds
```

## Requirements

For source development:

- Windows
- Python 3.12+
- Ollama installed and running for Local Ollama mode
- Optional: LiteLLM for more accurate token counting
- Optional: PyInstaller for building executables

The packaged executable does not require users to install Python.

## Initial Release

For the first public version, publish the generated Windows executable files as GitHub Release assets instead of committing them to the repository.

Recommended initial release assets:

```text
OllamaAgentWorkspace.exe
OllamaAgentWorkspacePortable.exe
```

The source repository ignores `dist/`, `release/`, `releases/`, and `*.exe` files so build artifacts stay out of normal commits. After building, upload the executable files to a release such as `v0.1.0`.

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

## Building Windows Executables

Install PyInstaller:

```powershell
python -m pip install pyinstaller
```

Build the folder-based app:

```powershell
python -m PyInstaller --noconfirm --clean OllamaAgentWorkspace.spec
```

Build the single-file portable app:

```powershell
python -m PyInstaller --noconfirm --clean OllamaAgentWorkspacePortable.spec
```

Outputs:

```text
dist/OllamaAgentWorkspace/OllamaAgentWorkspace.exe
dist/OllamaAgentWorkspacePortable.exe
```

The folder-based build is usually better as a base for a proper installer. The portable build is easier to copy, but starts more slowly because it extracts bundled files at launch.

## Development Notes

- `web_app.py` supports PyInstaller by reading bundled assets from `sys._MEIPASS`.
- `launcher.py` finds a free local port starting at `7864` and opens the browser automatically.
- LiteLLM is used only for token counting. If it is unavailable, the app falls back to conservative character-based estimates.
- `LITELLM_LOCAL_MODEL_COST_MAP=True` is set to prevent LiteLLM from trying to refresh its model-cost map from the internet.
- Tool access is constrained to the selected workspace where file operations are involved.

## Security Notes

- Do not commit real API keys.
- Tavily and Custom API keys are user-provided runtime settings.
- Shell and Python execution tools run on the local machine inside the selected workspace. Only use this app with projects and prompts you trust.
