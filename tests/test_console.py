"""Console tests (offline).

These cover the parts of the web console that can be wrong without any network:
symbol normalisation, the one-line config edit, the demo-only credential gate,
the signal summary, and the HTTP routing. Nothing here contacts OKX.

Run:
    set PYTHONPATH=src
    .venv/Scripts/python.exe tests/test_console.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from okx_ema_trader import backtest, console_api, console_server
from okx_ema_trader.config import load_config
from okx_ema_trader.console_api import ConsoleError, _normalise_symbol, _replace_symbols_line
from okx_ema_trader.credentials import CredentialsError, load_demo_credentials

CONFIG_PATH = console_api.CONFIG_PATH


def _config(**overrides):
    """The real config with a few fields swapped (Config is frozen)."""
    return replace(load_config(CONFIG_PATH), **overrides)


# --------------------------------------------------------------------------
def test_symbol_normalisation() -> int:
    failures = 0
    for raw, expected in [("MU", "MU-USDT-SWAP"), ("mu", "MU-USDT-SWAP"),
                          ("MUUSDT", "MU-USDT-SWAP"), ("MU-USDT-SWAP", "MU-USDT-SWAP"),
                          (" mu-usdt-swap ", "MU-USDT-SWAP"),
                          ("BTC-USDT-SWAP", "BTC-USDT-SWAP")]:
        got = _normalise_symbol(raw)
        if got != expected:
            print(f"  FAIL {raw!r} -> {got!r}, expected {expected!r}")
            failures += 1
    for bad in ("", "   ", "策略@@"):
        try:
            _normalise_symbol(bad)
            print(f"  FAIL {bad!r} should have been rejected")
            failures += 1
        except ConsoleError:
            pass
    print(f"  symbol normalisation: {'ok' if not failures else 'FAILED'}")
    return failures


def test_config_symbol_edit_keeps_comments() -> int:
    """The config file is commented; a yaml round-trip would destroy it."""
    failures = 0
    original = """# top comment
symbol: BTC-USDT-SWAP

strategy:
  ema_fast: 20   # fast line
  adx_min: 20

trading:
  enabled: true
  # 统一使用 OKX instId 格式
  symbols: [BTC-USDT-SWAP, AAVE-USDT-SWAP]
  leverage: 10   # do not touch
"""
    edited = _replace_symbols_line(original, "MU-USDT-SWAP")
    for line in ("# top comment", "# fast line", "# 统一使用 OKX instId 格式",
                 "# do not touch", "leverage: 10"):
        if line not in edited:
            print(f"  FAIL comment/line lost: {line!r}")
            failures += 1
    if "symbols: [BTC-USDT-SWAP, AAVE-USDT-SWAP, MU-USDT-SWAP]" not in edited:
        print(f"  FAIL symbol not appended correctly:\n{edited}")
        failures += 1

    # Adding it twice must not duplicate it.
    twice = _replace_symbols_line(edited, "MU-USDT-SWAP")
    if twice.count("MU-USDT-SWAP") != 1:
        print(f"  FAIL adding twice duplicated the entry:\n{twice}")
        failures += 1

    print(f"  config symbol edit: {'ok' if not failures else 'FAILED'}")
    return failures


def test_demo_only_credential_gate() -> int:
    """A non-demo profile must be refused, not silently demoted."""
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(
            '[profiles.demo]\ndemo = true\napi_key = "k"\nsecret_key = "s"\n'
            'passphrase = "p"\n\n[profiles.live]\ndemo = false\napi_key = "k"\n'
            'secret_key = "s"\npassphrase = "p"\n', encoding="utf-8")
        os.environ["OKX_CONFIG_TOML"] = str(path)
        try:
            creds = load_demo_credentials("demo", path)
            if creds.profile != "demo":
                print(f"  FAIL demo profile not loaded: {creds.profile!r}")
                failures += 1
            try:
                load_demo_credentials("live", path)
                print("  FAIL a non-demo profile was accepted")
                failures += 1
            except CredentialsError:
                pass
        finally:
            os.environ.pop("OKX_CONFIG_TOML", None)
    print(f"  demo-only gate: {'ok' if not failures else 'FAILED'}")
    return failures


# --------------------------------------------------------------------------
def _i5(fast, slow, prev_fast, prev_slow, close=100.0) -> dict:
    return {"close": close, "ema_fast": fast, "ema_slow": slow,
            "prev_ema_fast": prev_fast, "prev_ema_slow": prev_slow}


def _i15(fast, slow, adx, close) -> dict:
    return {"close": close, "ema_fast": fast, "ema_slow": slow, "adx": adx}


def test_summarise_matches_the_strategy() -> int:
    """The UI summary must agree with `classify`, and never decide on its own."""
    failures = 0
    config = _config(adx_min=20.0, deviation_max=0.02)

    # Golden cross inside a bullish 15m -> long.
    out = console_api.summarise(_i5(101.0, 100.0, 99.0, 100.0),
                                _i15(100.0, 99.0, 30.0, 100.0), config)
    if out["signal"] != "long":
        print(f"  FAIL expected long, got {out['signal']} ({out['reason']})")
        failures += 1
    if not all(out["gates"].values()):
        print(f"  FAIL all gates should pass: {out['gates']}")
        failures += 1

    # EMA20 already above EMA60 for many bars -> no cross -> no signal.
    out = console_api.summarise(_i5(102.0, 100.0, 101.5, 100.0),
                                _i15(100.0, 99.0, 30.0, 100.0), config)
    if out["signal"] is not None or out["reason"] != "no_cross_event":
        print(f"  FAIL stale state must not fire: {out['signal']} {out['reason']}")
        failures += 1

    # ADX exactly at the threshold -> blocked (strict >).
    out = console_api.summarise(_i5(101.0, 100.0, 99.0, 100.0),
                                _i15(100.0, 99.0, 20.0, 100.0), config)
    if out["signal"] is not None or out["reason"] != "adx_too_low":
        print(f"  FAIL ADX==min must block: {out['signal']} {out['reason']}")
        failures += 1

    # Deviation gate: close 3% above EMA20 blocks even with a perfect cross.
    out = console_api.summarise(_i5(101.0, 100.0, 99.0, 100.0),
                                _i15(100.0, 99.0, 30.0, 103.0), config)
    if out["signal"] is not None or out["reason"] != "deviation_too_large":
        print(f"  FAIL deviation gate did not block: {out['signal']} {out['reason']}")
        failures += 1
    if not out["gates"]["cross_ok"]:
        print("  FAIL the 5m cross itself was still true; only the gate should fail")
        failures += 1

    print(f"  summarise agrees with classify: {'ok' if not failures else 'FAILED'}")
    return failures


def test_console_backtest_uses_the_shared_simulate() -> int:
    """The console must not be a third copy of the strategy."""
    failures = 0
    if console_api.simulate is not backtest.simulate:
        print("  FAIL console imported a different simulate")
        failures += 1
    print(f"  console reuses backtest.simulate: {'ok' if not failures else 'FAILED'}")
    return failures


# --------------------------------------------------------------------------
def test_proxy_precedence_and_empty_means_direct() -> int:
    """Empty must mean "no proxy". Conflating empty with "unset" made direct
    connections impossible to try without editing the saved config."""
    failures = 0
    from okx_ema_trader.console_server import _proxy
    if _proxy({"proxy": ""}) is not None:
        print("  FAIL empty proxy must mean direct, not fall back to saved")
        failures += 1
    if _proxy({"proxy": "http://127.0.0.1:7890"}) != "http://127.0.0.1:7890":
        print("  FAIL explicit proxy not honoured")
        failures += 1
    print(f"  proxy resolution: {'ok' if not failures else 'FAILED'}")
    return failures


def test_default_proxy_distinguishes_unset_from_direct() -> int:
    """A MISSING key must fall back to the system proxy; an explicit null/""
    means "connect directly".

    Both used to read as None, so a fresh install — before anyone opened the
    settings page — connected directly and reported "cannot reach OKX" on a
    machine whose system proxy works.
    """
    failures = 0
    real_path = console_api.CONSOLE_STATE_PATH
    real_system = console_api.http.system_proxy
    console_api.http.system_proxy = lambda: "http://127.0.0.1:6088"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "console.json"
            console_api.CONSOLE_STATE_PATH = path

            if console_api.default_proxy() != "http://127.0.0.1:6088":
                print("  FAIL unset proxy should fall back to the system proxy")
                failures += 1

            for raw, label in (('{"proxy": null}', "explicit null"),
                               ('{"proxy": ""}', "explicit empty string")):
                path.write_text(raw, encoding="utf-8")
                if console_api.default_proxy() is not None:
                    print(f"  FAIL {label} should mean direct")
                    failures += 1

            path.write_text('{"proxy": "http://127.0.0.1:7890"}', encoding="utf-8")
            if console_api.default_proxy() != "http://127.0.0.1:7890":
                print("  FAIL stored proxy not honoured")
                failures += 1

            # Garbled: must not crash, and must not silently go direct either.
            path.write_text("{not json", encoding="utf-8")
            if console_api.default_proxy() != "http://127.0.0.1:6088":
                print("  FAIL garbled prefs should fall back to the system proxy")
                failures += 1
    finally:
        console_api.CONSOLE_STATE_PATH = real_path
        console_api.http.system_proxy = real_system
    print(f"  unset vs direct: {'ok' if not failures else 'FAILED'}")
    return failures


def test_windows_proxy_string_parsing() -> int:
    failures = 0
    parse = console_api._parse_proxy_server
    for raw, expected in [
        ("127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("http=127.0.0.1:7890;https=127.0.0.1:7891", "http://127.0.0.1:7891"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("ftp=1.2.3.4:21;http=5.6.7.8:80", "http://5.6.7.8:80"),
        ("", None), ("   ", None),
    ]:
        got = parse(raw)
        if got != expected:
            print(f"  FAIL {raw!r} -> {got!r}, expected {expected!r}")
            failures += 1
    print(f"  proxy string parsing: {'ok' if not failures else 'FAILED'}")
    return failures


def test_profile_proxy_url_is_honoured() -> int:
    """OKX's own convention: `proxy_url` inside the chosen profile."""
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(
            '[profiles.demo]\ndemo = true\napi_key = "k"\nsecret_key = "s"\n'
            'passphrase = "p"\nproxy_url = "http://127.0.0.1:7890"\n\n'
            '[profiles.plain]\ndemo = true\napi_key = "k"\nsecret_key = "s"\n'
            'passphrase = "p"\n', encoding="utf-8")
        os.environ["OKX_CONFIG_TOML"] = str(path)
        try:
            if console_api.profile_proxy("demo") != "http://127.0.0.1:7890":
                print("  FAIL proxy_url not read from the profile")
                failures += 1
            if console_api.profile_proxy("plain") is not None:
                print("  FAIL a profile without proxy_url returned one")
                failures += 1
        finally:
            os.environ.pop("OKX_CONFIG_TOML", None)
    print(f"  profile proxy_url: {'ok' if not failures else 'FAILED'}")
    return failures


def test_detect_proxy_survives_a_blocked_network() -> int:
    """With no reachable route it must report cleanly, not raise."""
    failures = 0
    result = console_api.detect_proxy()
    tried = result["tried"]
    if not tried:
        print("  FAIL detection returned no attempts")
        failures += 1
    elif result["found"] is not None:
        # It short-circuits on the first working route, so that one must be
        # both last in the list and marked ok.
        if not tried[-1]["ok"]:
            print(f"  FAIL returned a route that did not work: {tried[-1]}")
            failures += 1
    else:
        # Nothing worked: the direct attempt must at least have been tried.
        if not any("直连" in t["label"] for t in tried):
            print("  FAIL direct connection was never tried")
            failures += 1
        if any(t["ok"] for t in tried):
            print("  FAIL found=None but some route succeeded")
            failures += 1
    print(f"  detect proxy (tried {len(tried)} routes, found="
          f"{result['found'] or 'none'}): {'ok' if not failures else 'FAILED'}")
    return failures


def test_settings_roundtrip() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "console.json"
        saved = console_api.save_settings({"proxy": "http://127.0.0.1:9999",
                                           "symbol": "MU-USDT-SWAP"}, path)
        if saved["proxy"] != "http://127.0.0.1:9999":
            print(f"  FAIL save did not stick: {saved}")
            failures += 1
        if console_api.load_settings(path)["symbol"] != "MU-USDT-SWAP":
            print("  FAIL load did not read back the symbol")
            failures += 1
        # Unknown keys must not be persisted.
        console_api.save_settings({"evil": "x"}, path)
        if "evil" in json.loads(path.read_text(encoding="utf-8")):
            print("  FAIL an unknown key was written")
            failures += 1
        # A garbled file must fall back to defaults (direct), not crash.
        path.write_text("{not json", encoding="utf-8")
        if console_api.load_settings(path)["proxy"] is not None:
            print("  FAIL garbled prefs file should fall back to direct")
            failures += 1
    print(f"  settings roundtrip: {'ok' if not failures else 'FAILED'}")
    return failures


def test_http_routes_without_network() -> int:
    """Start the real handler on a free port and hit the offline endpoints."""
    failures = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), console_server.ConsoleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/", timeout=10) as response:
            html = response.read().decode("utf-8")
        if "OKX EMA 策略控制台" not in html:
            print("  FAIL index.html was not served")
            failures += 1

        with urllib.request.urlopen(base + "/api/overview", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload["ok"] or payload["data"]["demo_only"] is not True:
            print(f"  FAIL overview wrong: {payload.get('data')}")
            failures += 1

        request = urllib.request.Request(
            base + "/api/nope", method="GET")
        try:
            urllib.request.urlopen(request, timeout=10)
            print("  FAIL unknown endpoint did not 404")
            failures += 1
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                print(f"  FAIL expected 404, got {exc.code}")
                failures += 1
    finally:
        server.shutdown()
        server.server_close()
    print(f"  http routing offline: {'ok' if not failures else 'FAILED'}")
    return failures


def main() -> int:
    print("console tests (offline)")
    print("")
    failures = 0
    for test in (
        test_symbol_normalisation,
        test_config_symbol_edit_keeps_comments,
        test_demo_only_credential_gate,
        test_summarise_matches_the_strategy,
        test_console_backtest_uses_the_shared_simulate,
        test_proxy_precedence_and_empty_means_direct,
        test_default_proxy_distinguishes_unset_from_direct,
        test_windows_proxy_string_parsing,
        test_profile_proxy_url_is_honoured,
        test_detect_proxy_survives_a_blocked_network,
        test_settings_roundtrip,
        test_http_routes_without_network,
    ):
        failures += test()
    print("")
    if failures:
        print(f"FAILED: {failures} assertion(s)")
    else:
        print("all groups passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
