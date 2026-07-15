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

    import web_app

    url = f"http://{web_app.HOST}:{web_app.PORT}/"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    web_app.main()


if __name__ == "__main__":
    main()
