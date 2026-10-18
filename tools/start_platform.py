"""Build changed frontend assets and open the combined platform."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import threading
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "platform" / "frontend"
URL = "http://127.0.0.1:8788"


def running():
    try:
        with urllib.request.urlopen(URL + "/openapi.json", timeout=2) as response:
            return json.load(response).get("info", {}).get("title") == "okx-ema-trader platform"
    except Exception:
        return False


def build_frontend():
    index = FRONTEND / "dist" / "index.html"
    sources = [*(FRONTEND / "src").rglob("*"), FRONTEND / "package.json",
               FRONTEND / "package-lock.json", FRONTEND / "index.html", FRONTEND / "vite.config.js"]
    if index.exists() and all(p.stat().st_mtime <= index.stat().st_mtime for p in sources if p.is_file()):
        return
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise RuntimeError("Frontend update needs Node.js / npm. Install Node.js and retry.")
    if not (FRONTEND / "node_modules" / ".bin" / "vite.cmd").exists():
        subprocess.run([npm, "ci"], cwd=FRONTEND, check=True)
    subprocess.run([npm, "run", "build"], cwd=FRONTEND, check=True)


def open_when_ready(done):
    for _ in range(60):
        if done.is_set():
            return
        if running():
            webbrowser.open(URL)
            return
        done.wait(1)
    print("Browser did not open: inspect logs/platform.log")


def main():
    if running():
        webbrowser.open(URL)
        return 0
    build_frontend()
    print(f"Starting {URL} - keep this window open. Ctrl+C stops the platform.")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app",
                                "--host", "127.0.0.1", "--port", "8788"],
                               cwd=ROOT / "platform" / "backend")
    done = threading.Event()
    threading.Thread(target=open_when_ready, args=(done,), daemon=True).start()
    try:
        return process.wait()
    except KeyboardInterrupt:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait()
        return 0
    finally:
        done.set()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Startup failed: {exc}")
        sys.exit(1)
