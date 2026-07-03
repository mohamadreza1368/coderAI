"""
config.py - environment settings, constants, and local proxy cleanup.
"""
import os

# Keep local Ollama traffic away from VPN/proxy settings.
os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

_PROXY_VARS = [
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
]
for _var in _PROXY_VARS:
    os.environ.pop(_var, None)

os.environ["OLLAMA_HOST"] = "http://127.0.0.1:11434"

# Keep LiteLLM fully local for token counting.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_LOG", "ERROR")

# Remote URL config. Override via OLLAMA_URL environment variable.
# Default points to a local Ollama instance consistent with Local Ollama mode.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
_BASE      = OLLAMA_URL.rsplit("/api/", 1)[0]
TAGS_URL   = f"{_BASE}/api/tags"
CHAT_URL   = f"{_BASE}/api/chat"

# Connection modes.
MODE_LOCAL  = "🖥️ Local Ollama"
MODE_REMOTE = "🌐 Remote Ollama"
MODE_CUSTOM = "🔑 Custom API"
ALL_MODES   = [MODE_LOCAL, MODE_REMOTE, MODE_CUSTOM]

# Default system prompt loaded from a Markdown file.
_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "system_prompts", "custom_prompt.md")

def _load_system_prompt() -> str:
    try:
        with open(_PROMPT_FILE, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "You are a helpful AI assistant."

DEFAULT_SYSTEM_PROMPT: str = _load_system_prompt()

# Session state defaults.
SESSION_DEFAULTS: dict = {
    "messages":          [],
    "thinking_logs":     [],
    "rtl_flags":         [],
    "model_list":        [],
    "model_error":       None,
    "models_loaded":     False,
    "conn_mode":         MODE_LOCAL,
    "custom_api_url":    "https://api.openai.com/v1",
    "custom_api_key":    "",
    "selected_file_tab": None,
    "workspace_path":    "",
    "used_skills_log":   [],
}

# Extension map for workspace file naming.
LANG_EXT_MAP: dict[str, str] = {
    "python":     "py",
    "javascript": "js",
    "typescript": "ts",
    "html":       "html",
    "css":        "css",
    "bash":       "sh",
    "sql":        "sql",
    "json":       "json",
}
