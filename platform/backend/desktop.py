"""Desktop launcher: one double-click, no console, no port to remember.

How it works:
  1. If an instance is already serving, just open a window onto it —
     two servers on one database is how SQLite files get locked.
  2. Otherwise bind a FREE port (not a hard-coded one), serve uvicorn in a
     daemon thread, and point a native window at it.

The port still exists — a browser cannot read SQLite, something has to — but
it is picked automatically and never shown to the user.

Run:  python desktop.py        (or double-click start-platform-desktop.bat)
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.request

import uvicorn
import webview

import main as backend  # noqa: F401  (registers the FastAPI app)


def _serving(port: int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    preferred = 8788
    if _serving(preferred):
        port = preferred            # reuse the running instance
    else:
        port = _free_port()
        config = uvicorn.Config(backend.app, host="127.0.0.1", port=port,
                                log_level="warning")
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):        # wait until the app answers, max ~10s
            if _serving(port):
                break
            time.sleep(0.1)
        else:
            raise SystemExit("后端没能在 10 秒内启动——请改用 start-platform.bat 看报错")

    webview.create_window("OKX EMA Trader", f"http://127.0.0.1:{port}",
                          width=1440, height=900, min_size=(1100, 700))
    webview.start()


if __name__ == "__main__":
    main()
