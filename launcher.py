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
    threading.Timer(1.2, lambda: _open_browser(url)).start()
    web_app.main()


def _open_browser(url: str) -> None:
    if os.name == "nt" and hasattr(os, "startfile"):
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return
        except OSError:
            pass
    webbrowser.open(url)


if __name__ == "__main__":
    main()
