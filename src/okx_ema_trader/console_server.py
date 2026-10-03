"""Local web console: HTTP adapter over `console_api`.

Deliberately stdlib-only (`http.server`) so the console adds no dependency the
project did not already have, and deliberately bound to 127.0.0.1 so it is
reachable from this machine and nowhere else.

It is a UI over the same code the bot runs. It does not re-implement the
strategy, and it cannot reach a live account.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import console_api
from .console_api import ConsoleError
from .http import OkxError

ROOT = Path(__file__).parents[2]
WEB_DIR = ROOT / "web"
INDEX = WEB_DIR / "index.html"

log = logging.getLogger(__name__)

# Route -> handler. GET handlers take query args, POST handlers take a JSON body.
GET_ROUTES: dict[str, callable] = {}
POST_ROUTES: dict[str, callable] = {}


def route(path: str, method: str = "GET"):
    def wrap(func):
        (GET_ROUTES if method == "GET" else POST_ROUTES)[path] = func
        return func

    return wrap


def _json_safe(value):
    """Replace non-finite floats with None, recursively.

    Python's `json.dumps` happily writes `NaN` and `Infinity`, which are NOT
    valid JSON — the browser's `JSON.parse` throws and the whole panel renders
    blank. They are not exotic either: `ruin_exposure()` returns `inf` whenever
    the run never dipped below zero, and a single bad candle anywhere in the
    series turns every downstream statistic into `NaN`. Better to emit `null`
    (the UI already renders "-" for it) than to serve a response that cannot be
    parsed.
    """
    if isinstance(value, float):
        # np.float64 IS a float subclass, so numpy's nan/inf land here too.
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if type(value).__module__ == "numpy":
        # np.int64 and np.bool_ are NOT int/bool subclasses — json.dumps raises
        # "Object of type int64 is not JSON serializable" and the endpoint
        # returns 500. Checked without importing numpy to keep this module
        # stdlib-only.
        return _json_safe(value.item() if getattr(value, "ndim", 0) == 0 else value.tolist())
    return value


def _proxy(args: dict) -> str | None:
    """An EMPTY string means "connect directly", not "use whatever is saved".

    Conflating the two made it impossible to try a direct connection without
    first clearing the saved value — which is exactly what someone on an
    unfiltered network needs to do.
    """
    if "proxy" in args:
        return str(args["proxy"]).strip() or None
    # Not `load_settings().get("proxy")`: that reads None for both "never
    # configured" and "chose direct", and picks direct for the former — so a
    # fresh install fails to reach OKX even when the system proxy works.
    return console_api.default_proxy()


def _profile(args: dict):
    value = args.get("profile")
    return value or None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@route("/api/overview")
def _overview(args):
    return console_api.overview()


@route("/api/ticker")
def _ticker(args):
    return console_api.ticker(args["symbol"], _proxy(args))


@route("/api/signal")
def _signal(args):
    from .config import load_config
    config = load_config(console_api.CONFIG_PATH)
    return console_api.current_signal(args["symbol"], config, _proxy(args))


@route("/api/instruments")
def _instruments(args):
    try:
        rows = console_api.instruments(_proxy(args))
    except ConsoleError as exc:
        # A dead network must not empty the picker; fall back to the config list.
        from .config import load_config
        symbols = list(load_config(console_api.CONFIG_PATH).trading.symbols)
        return {"symbols": symbols, "rows": [], "warning": str(exc)}
    return {"symbols": [row["inst_id"] for row in rows], "rows": rows, "warning": None}


@route("/api/spec")
def _spec(args):
    return console_api.spec(args["symbol"], _proxy(args))


@route("/api/account")
def _account(args):
    return console_api.account(_profile(args), _proxy(args))


@route("/api/detect-proxy", "POST")
def _detect_proxy(args):
    """Try every route and report which one actually reaches OKX."""
    result = console_api.detect_proxy(_profile(args))
    if result["found"]:
        console_api.save_settings({"proxy": result["found"]})
    return result


@route("/api/backtest", "POST")
def _backtest(args):
    return console_api.run_backtest(
        symbol=args.get("symbol") or console_api.default_symbol(),
        days=int(args.get("days") or 60),
        fee_bps=float(args.get("fee_bps") or 5.0),
        slippage_bps=float(args.get("slippage_bps") or 3.0),
        proxy=_proxy(args),
        show=int(args.get("show") or 20),
    )


@route("/api/trade", "POST")
def _trade(args):
    return console_api.place_order(
        symbol=args["symbol"],
        side=args["side"],
        notional=float(args.get("notional") or 100.0),
        leverage=int(args.get("leverage") or 10),
        stop_pct=float(args.get("stop_pct") or 3.0),
        profile=_profile(args),
        proxy=_proxy(args),
        dry_run=bool(args.get("dry_run")),
    )


@route("/api/close", "POST")
def _close(args):
    return console_api.close_position(
        symbol=args["symbol"],
        profile=_profile(args),
        proxy=_proxy(args),
        dry_run=bool(args.get("dry_run")),
    )


@route("/api/settings", "POST")
def _settings(args):
    return console_api.save_settings(
        {k: args[k] for k in ("proxy", "profile", "symbol") if k in args})


@route("/api/symbols", "POST")
def _symbols(args):
    return {"symbols": console_api.update_config_symbols(args["symbol"])}


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------
class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "okx-ema-console/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(_json_safe(payload), ensure_ascii=False,
                                      allow_nan=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _dispatch(self, table: dict, args: dict, name: str) -> None:
        handler = table.get(name)
        if handler is None:
            self._json(404, {"ok": False, "error": f"unknown endpoint {name}"})
            return
        try:
            self._json(200, {"ok": True, "data": handler(args)})
        except ConsoleError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except OkxError as exc:
            # An OKX-level failure is something the user can act on (wrong
            # symbol, rate limited, suspended market), so it is a 400 with the
            # exchange's own wording — not a 500 that reads like our bug.
            self._json(400, {"ok": False, "error": str(exc)})
        except KeyError as exc:
            self._json(400, {"ok": False, "error": f"缺少参数 {exc}"})
        except Exception as exc:  # never leak a stack trace into the UI
            log.exception("console request failed: %s", name)
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            if not INDEX.exists():
                self._send(500, b"web/index.html is missing", "text/plain; charset=utf-8")
                return
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        if path.startswith("/api/"):
            args = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            self._dispatch(GET_ROUTES, args, path)
            return
        self._json(404, {"ok": False, "error": f"not found: {path}"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._json(404, {"ok": False, "error": f"not found: {parsed.path}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            args = json.loads(raw.decode("utf-8"))
            if not isinstance(args, dict):
                raise ValueError("body must be a JSON object")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            self._json(400, {"ok": False, "error": f"请求体不是合法 JSON: {exc}"})
            return
        self._dispatch(POST_ROUTES, args, parsed.path)

    def log_message(self, fmt: str, *args) -> None:  # keep the console readable
        log.info("%s %s", self.address_string(), fmt % args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local web console for the OKX EMA/ADX strategy.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="port (default: 8787)")
    parser.add_argument("--open", action="store_true", help="open a browser after start")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not INDEX.exists():
        raise SystemExit(f"missing {INDEX}")

    server = ThreadingHTTPServer((args.host, args.port), ConsoleHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"OKX EMA 控制台已启动: {url}")
    print("只连接 OKX Demo 模拟盘。Ctrl+C 退出。")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
