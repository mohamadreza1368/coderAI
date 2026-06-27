# Ollama Agentic Workspace

Ollama Agentic Workspace is a local, browser-based coding agent UI for working with projects on your machine. It provides a three-pane workspace for browsing files, previewing or downloading generated code, and chatting with an agent that can inspect and edit the selected project through tool calls.

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
- Native folder picker for selecting a local project directory.
- Agent tool calling for file reads, writes, replacement, search, project scans, shell commands, and Python snippets.
- Optional Tavily web search tools, controlled from the Settings UI.
- System prompt manager backed by Markdown files in `system_prompts/`.
- Skill manager backed by `skills/**/SKILL.md`.
- Streaming chat responses and live generated-code preview.
- Download button for generated or selected code.
- Context compaction and LiteLLM-based token counting when available.

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

## TODO

- Add automated tests for the agent runtime and tool execution layer.
- Add persistent conversation memory with configurable limits.
- Improve workspace indexing for larger projects.
- Add richer skill validation and status reporting.
- Add a safer approval flow for shell and Python execution tools.
- Improve release packaging and distribution workflow.

## Development Notes

- `launcher.py` finds a free local port starting at `7864` and opens the browser automatically.
- LiteLLM is used only for token counting. If it is unavailable, the app falls back to conservative character-based estimates.
- `LITELLM_LOCAL_MODEL_COST_MAP=True` is set to prevent LiteLLM from trying to refresh its model-cost map from the internet.
- Tool access is constrained to the selected workspace where file operations are involved.

## Security Notes

- Tavily and Custom API keys are user-provided runtime settings.
- Shell and Python execution tools run on the local machine inside the selected workspace. Only use this app with projects and prompts you trust.
