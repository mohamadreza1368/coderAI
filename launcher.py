from __future__ import annotations

import os
import socket
import threading
import webbrowser


def _find_free_port(start: int = 7864, attempts: int = 20) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def main() -> None:
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    os.environ.setdefault("AGENT_REQUEST_TIMEOUT", "1800")
    os.environ.setdefault("WEB_APP_HOST", "127.0.0.1")
    os.environ.setdefault("WEB_APP_PORT", str(_find_free_port()))

    host = os.environ["WEB_APP_HOST"]
    port = int(os.environ["WEB_APP_PORT"])
    url = f"http://{host}:{port}/"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    framework = os.getenv("SERVER_FRAMEWORK", "fastapi").lower()
    if framework == "fastapi":
        try:
            import uvicorn
            print(f"Starting CoderAI (FastAPI + Uvicorn + WebSocket) on {url}")
            uvicorn.run("fastapi_app:app", host=host, port=port, log_level="info", ws_ping_interval=None)
            return
        except Exception as exc:
            print(f"FastAPI start failed ({exc}); falling back to standard web_app...")

    import web_app
    web_app.main()


if __name__ == "__main__":
    main()
