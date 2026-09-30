"""
config.py - environment settings, constants, and local proxy cleanup.
"""
import os

# Keep local Ollama traffic away from VPN/proxy settings, especially in Docker.
_no_proxy = os.getenv("NO_PROXY", "localhost,127.0.0.1,::1,host.docker.internal")
os.environ["NO_PROXY"] = _no_proxy
os.environ["no_proxy"] = _no_proxy

def _auto_discover_proxy():
    if os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY"):
        return  # user explicitly set it
    
    import socket
    proxy_ports = [10808, 10809, 2080, 2081, 10811, 7890, 7891]
    host = "host.docker.internal"
    
    try:
        socket.gethostbyname(host)
    except Exception:
        host = "127.0.0.1"
        
    for port in proxy_ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            try:
                if s.connect_ex((host, port)) == 0:
                    proxy_url = f"http://{host}:{port}"
                    os.environ["HTTP_PROXY"] = proxy_url
                    os.environ["HTTPS_PROXY"] = proxy_url
                    os.environ["http_proxy"] = proxy_url
                    os.environ["https_proxy"] = proxy_url
                    print(f"[CoderAI] Auto-detected proxy on {proxy_url}")
                    break
            except Exception:
                pass

_auto_discover_proxy()

os.environ["OLLAMA_HOST"] = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

# Keep LiteLLM fully local for token counting.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_LOG", "ERROR")

# Remote URL config. Override via OLLAMA_URL environment variable.
# Default points to a local Ollama instance consistent with Local Ollama mode.
OLLAMA_URL = os.getenv("OLLAMA_URL", f"{os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')}/api/generate")
_BASE      = OLLAMA_URL.rsplit("/api/", 1)[0]
TAGS_URL   = f"{_BASE}/api/tags"
CHAT_URL   = f"{_BASE}/api/chat"

# Connection modes.
MODE_LOCAL  = "🖥️ Local Ollama"
MODE_REMOTE = "🌐 Remote Ollama"
MODE_CUSTOM = "🔑 Custom API"
ALL_MODES   = [MODE_LOCAL, MODE_REMOTE, MODE_CUSTOM]

# Default system prompt loaded from a Markdown file.
_PROMPT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "system_prompts", "custom_prompt.md")

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
