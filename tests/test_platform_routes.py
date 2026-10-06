"""Tests for the backend route layer.

The failure mode pinned here: a route that normalises a symbol must answer 400
for a malformed one, not 500. `deps.normalise_symbol` signals "bad symbol" by
RAISING, so calling it directly in a handler turns a typo in the query string
into a traceback — and a 500 reads as "the server is broken" when the truth is
"you typed the symbol wrong". `/api/backtests` did exactly that.

These call the handler functions directly instead of going through TestClient:
the thing under test is the error mapping, and an ASGI transport would need
`httpx` for no extra signal.

Run:
    .venv/Scripts/python.exe tests/test_platform_routes.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "platform" / "backend"))

from fastapi import HTTPException  # noqa: E402

import deps  # noqa: E402
import main as app  # noqa: E402
from okx_ema_trader.storage import Store  # noqa: E402

BAD = "NOT-A-SYMBOL!!"
GOOD = "MU-USDT-SWAP"
_checks = 0


def check(condition: bool, message: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(message)


class _TempStore:
    """Swap the process-wide store for a throwaway one."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "t.db")
        self._saved = deps.store

    def __enter__(self):
        deps.store = lambda: self.store
        return self.store

    def __exit__(self, *exc):
        deps.store = self._saved
        self.store.close()
        self._tmp.cleanup()


def status_of(fn, *args, **kwargs) -> int | None:
    """The HTTP status the handler raises, or None if it returned normally."""
    try:
        fn(*args, **kwargs)
    except HTTPException as exc:
        return exc.status_code
    return None


def test_backtests_bad_symbol_is_400_not_500():
    got = status_of(app.backtests, BAD)
    check(got == 400, f"a bad symbol must be 400, got {got!r} (None means it returned)")


def test_other_symbol_routes_agree():
    with _TempStore():
        for name, fn in (("orders", app.orders), ("signals", app.signals)):
            got = status_of(fn, BAD)
            check(got == 400, f"/api/{name} must 400 on a bad symbol, got {got!r}")


def test_backtests_accepts_a_good_symbol():
    with _TempStore():
        rows = app.backtests(GOOD, 30)
        check(rows["rows"] == [], "a fresh store has no backtest rows")


def test_no_route_calls_normalise_symbol_raw():
    """Guard the class of bug, not just the one instance found.

    `_symbol` is the only wrapper that turns SymbolError into a 400. A new route
    reaching for `deps.normalise_symbol` reintroduces the 500 and nothing else
    in the suite would notice — this one does.
    """
    source = PROJECT_ROOT / "platform" / "backend" / "main.py"
    lines = source.read_text(encoding="utf-8").splitlines()
    inside_helper = False
    for lineno, line in enumerate(lines, 1):
        if line.startswith("def _symbol("):
            inside_helper = True
            continue
        if inside_helper:
            # The helper's own body is the one legitimate caller.
            if line.startswith("def ") or line.startswith("@"):
                inside_helper = False
            else:
                continue
        if "deps.normalise_symbol" in line:
            raise AssertionError(
                f"main.py:{lineno} calls deps.normalise_symbol directly — "
                "use _symbol() so a bad symbol answers 400, not 500")


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print(f"test_platform_routes: {_checks} checks passed")
