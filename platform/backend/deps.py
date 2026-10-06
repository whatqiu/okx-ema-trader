"""Shared dependencies for the backend.

Everything here is resolved once and shared, because two things must not happen
twice: opening a second SQLite handle per request, and letting two modules each
hold their own idea of "the proxy" (they would disagree, and only one of them
would be able to reach OKX).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parents[1]
SRC_DIR = PROJECT_ROOT / "src"

for _path in (str(BACKEND_DIR), str(SRC_DIR), str(PROJECT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from okx_ema_trader import http as okx_http  # noqa: E402
from okx_ema_trader.config import Config, load_config  # noqa: E402
from okx_ema_trader.storage import Store  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
CONSOLE_STATE_PATH = PROJECT_ROOT / "config" / "console.json"
DB_PATH = PROJECT_ROOT / "data" / "platform.db"
FRONTEND_DIST = PROJECT_ROOT / "platform" / "frontend" / "dist"

_store: Store | None = None
_proxy_override: str | None | bool = False  # False = not resolved yet


def project_root() -> Path:
    return PROJECT_ROOT


def store() -> Store:
    """Process-wide Store. SQLite handles are per-thread inside, but the object
    and its schema setup are shared."""
    global _store
    if _store is None:
        _store = Store(DB_PATH)
    return _store


def load_settings() -> dict:
    try:
        raw = json.loads(CONSOLE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_settings(patch: dict) -> dict:
    current = load_settings()
    for key in ("proxy", "profile", "symbol"):
        if key in patch:
            current[key] = patch[key] or None
    CONSOLE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONSOLE_STATE_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    global _proxy_override
    _proxy_override = False  # force re-resolution
    return current


def proxy() -> str | None:
    """The proxy every outbound OKX call uses.

    Precedence: explicit setting ("" means direct) -> Windows system proxy ->
    None. Resolved once and cached: `winreg` on every request is wasteful, and
    if the user changes the setting we clear the cache in `save_settings`.
    """
    global _proxy_override
    if _proxy_override is False:
        settings = load_settings()
        if "proxy" in settings:
            # An explicit "" is a deliberate choice to go direct, which is
            # different from never having configured it.
            _proxy_override = settings.get("proxy") or None
        else:
            _proxy_override = okx_http.system_proxy()
    return _proxy_override  # type: ignore[return-value]


def config() -> Config:
    return load_config(CONFIG_PATH)


def normalise_symbol(symbol: str) -> str:
    """`MU` / `MUUSDT` / `mu-usdt-swap` -> `MU-USDT-SWAP`."""
    from okx_ema_trader.symbols import normalise_symbol as _normalise

    return _normalise(symbol or "")


def okx_get(path: str, params: dict | None = None, timeout: float = 15.0):
    return okx_http.get_json(path, params, proxy=proxy(), timeout=timeout)
