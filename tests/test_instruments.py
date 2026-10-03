"""Contract-spec and risk-metric tests (offline).

Two things here protect money, not code style:

  * **ctVal.** "1 contract = 1 coin" is false and the ratio differs per
    instrument (BTC 0.01, AAVE 0.1, MU 1). A 100x sizing error is not a
    rounding issue, it is a liquidation. The conversion and the lot-step maths
    are pinned here against the numbers OKX actually publishes.
  * **max drawdown.** The old metric divided a P&L swing by the P&L *peak*,
    which reported "284% drawdown" for a run whose account fell ~20%. The
    denominator has to be the account. These tests pin the unit, not just the
    formula.

Run:
    .venv/Scripts/python.exe tests\\test_instruments.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import numpy as np

from okx_ema_trader import http, instruments
from okx_ema_trader.backtest import Report, Trade
from okx_ema_trader.broker import to_ccxt_symbol

FAILURES = 0


def _check(label: str, ok: bool, detail: str = "") -> None:
    global FAILURES
    if ok:
        print(f"  {label}: ok")
    else:
        FAILURES += 1
        print(f"  {label}: FAIL {detail}")


def _spec(inst_id: str, ct_val: float, lot: float = 0.01, min_sz: float = 0.01,
          lever: float = 100.0, category: str = "1", max_mkt: float = 0.0) -> instruments.Spec:
    return instruments.Spec(
        inst_id=inst_id, category=instruments.CATEGORIES.get(category, "crypto"),
        raw_category=category, contract_value=ct_val, contract_ccy=inst_id.split("-")[0],
        lot_size=lot, min_size=min_sz, tick_size=0.01, state="live",
        max_leverage=lever, max_market_size=max_mkt,
    )


# --------------------------------------------------------------------------
def test_contract_value_maths() -> None:
    """BTC is 0.01 per contract, MU is 1. The same 1000 USDT is not the same size."""
    btc = _spec("BTC-USDT-SWAP", 0.01)
    mu = _spec("MU-USDT-SWAP", 1.0, lever=50.0)

    btc_contracts = instruments.contracts_for_notional(btc, 1000.0, 100_000.0)
    # 1000 / (0.01 * 100000) = 1 contract
    _check("btc 1000 USDT @100k = 1 contract", abs(btc_contracts - 1.0) < 1e-9,
           f"got {btc_contracts}")
    # 1 contract of BTC at 100k is 1000 USDT, NOT 100000.
    _check("btc 1 contract = 1000 USDT not 100000",
           abs(instruments.notional_for_contracts(btc, 1.0, 100_000.0) - 1000.0) < 1e-9)

    mu_contracts = instruments.contracts_for_notional(mu, 1070.0, 1070.0)
    _check("mu 1070 USDT @1070 = 1 contract", abs(mu_contracts - 1.0) < 1e-9,
           f"got {mu_contracts}")
    _check("mu 1 contract = 1070 USDT",
           abs(instruments.notional_for_contracts(mu, 1.0, 1070.0) - 1070.0) < 1e-9)

    # The bug this module exists to prevent: contracts * price overstates BTC 100x.
    naive = 1.0 * 100_000.0
    real = instruments.notional_for_contracts(btc, 1.0, 100_000.0)
    _check("naive contracts*price is 100x wrong on BTC", abs(naive / real - 100.0) < 1e-9)


def test_lot_step_is_not_min_size() -> None:
    """lotSz and minSz are different fields; the old code used minSz as the step.

    An instrument with minSz=1 and lotSz=5: flooring to a multiple of minSz
    yields sizes the exchange rejects (3, 7, 12 ...), which is exactly the
    mistake `broker.contracts_for_notional` used to make.
    """
    spec = _spec("X-USDT-SWAP", 1.0, lot=5.0, min_sz=1.0)
    got = instruments.contracts_for_notional(spec, 17.0, 1.0)  # 17 coins -> 15 contracts
    _check("floors to lotSz=5, not minSz=1", got == 15.0, f"got {got}")

    # Below minSz must be a loud refusal, not a silent 0.
    tiny = _spec("Y-USDT-SWAP", 1.0, lot=1.0, min_sz=10.0)
    try:
        instruments.contracts_for_notional(tiny, 5.0, 1.0)
        _check("below minSz raises", False, "no exception")
    except instruments.SpecError:
        _check("below minSz raises", True)


def test_no_float_drift() -> None:
    """(raw // step) * step gives 0.30000000000000004; OKX rejects that string."""
    spec = _spec("Z-USDT-SWAP", 1.0, lot=0.1, min_sz=0.1)
    got = instruments.contracts_for_notional(spec, 0.3, 1.0)
    _check("0.3 stays 0.3", got == 0.3 and repr(got) == "0.3", f"got {got!r}")


def test_leverage_ceiling_is_per_instrument() -> None:
    mu = _spec("MU-USDT-SWAP", 1.0, lever=50.0, category="3")
    effective, warning = instruments.check_leverage(mu, 10)
    _check("10x allowed on a 50x instrument", effective == 10 and warning is None)

    effective, warning = instruments.check_leverage(mu, 100)
    _check("100x clamped to 50x with a warning",
           effective == 50 and warning is not None and "50" in warning, f"{effective} {warning}")

    _check("stock token flagged", mu.is_stock_token)
    _check("category named", mu.category == "stock token", mu.category)


def test_market_size_ceiling() -> None:
    mu = _spec("MU-USDT-SWAP", 1.0, lever=50.0, category="3", max_mkt=580.0)
    _check("580 contracts allowed", instruments.check_order_size(mu, 580.0) is None)
    _check("581 contracts refused", instruments.check_order_size(mu, 581.0) is not None)


def test_spec_rejects_untradeable() -> None:
    suspended = instruments.Spec("A-USDT-SWAP", "crypto", "1", 1.0, "A", 0.01, 0.01,
                                 0.01, "suspend", 10.0, 0.0)
    _check("suspended is not live", not suspended.is_live)
    _check("live is live", _spec("B-USDT-SWAP", 1.0).is_live)


def test_ccxt_symbol_mapping() -> None:
    cases = {
        "BTC-USDT-SWAP": "BTC/USDT:USDT",
        "MU-USDT-SWAP": "MU/USDT:USDT",
        "BTC-USD-SWAP": "BTC/USD:BTC",      # inverse settles in the base
        "XAUUSDT-USDT-SWAP": "XAUUSDT/USDT:USDT",  # base contains "USDT"
        "BTC/USDT:USDT": "BTC/USDT:USDT",   # already converted
    }
    failures = 0
    for raw, expected in cases.items():
        got = to_ccxt_symbol(raw)
        if got != expected:
            failures += 1
            print(f"    FAIL {raw} -> {got!r}, expected {expected!r}")
    _check("ccxt symbol mapping", failures == 0)

    try:
        to_ccxt_symbol("BTCUSDT")
        _check("malformed instId rejected", False, "no exception")
    except Exception:
        _check("malformed instId rejected", True)


# --------------------------------------------------------------------------
def _report(equity) -> Report:
    trade = Trade("long", 0, 0, 1.0, 0, 0, 1.0, 0.0, "signal")
    return Report([trade], np.array(equity, dtype=float), 0.0, 1.0)


def test_drawdown_is_measured_against_the_account() -> None:
    """The regression: peak +8.2%, trough -15.1% used to print as a 284% drawdown.

    equity is cumulative P&L in units of notional, so its peak is a profit
    figure. Dividing by it is a unit error. At 1x the account goes
    1.082 -> 0.849, a 21.5% fall, and that is the answer.
    """
    report = _report([0.01, 0.0822, 0.02, -0.05, -0.1512, -0.09])
    dd = report.max_drawdown(1.0)
    _check("1x drawdown is a sane percentage", 0.21 < dd < 0.22, f"got {dd:.4f}")
    _check("not the old 284% nonsense", dd < 1.0, f"got {dd}")

    # Unleveraged the account survives; that is what "1x" means.
    _check("1x does not wipe out", not report.wiped_out(1.0))


def test_drawdown_scales_with_leverage() -> None:
    """Same P&L stream, 10x full-equity: the account is gone."""
    report = _report([0.01, 0.0822, 0.02, -0.05, -0.1512, -0.09])
    _check("10x wipes the account", report.wiped_out(10.0))
    _check("10x drawdown capped at 100%", report.max_drawdown(10.0) == 1.0,
           f"got {report.max_drawdown(10.0)}")

    # 2x is survivable here and must be strictly worse than 1x.
    _check("2x survives", not report.wiped_out(2.0))
    _check("more leverage = more drawdown",
           report.max_drawdown(2.0) > report.max_drawdown(1.0))


def test_ruin_leverage() -> None:
    """1 / |trough| is the exposure that empties the account."""
    report = _report([0.05, -0.20, -0.10])
    ruin = report.ruin_exposure()
    _check("ruin exposure = 1/0.20 = 5x", abs(ruin - 5.0) < 1e-9, f"got {ruin}")
    _check("just above ruin survives", not report.wiped_out(4.9))
    _check("at ruin it is gone", report.wiped_out(5.0))

    profitable = _report([0.01, 0.02, 0.03])
    _check("no trough -> infinite ruin leverage",
           profitable.ruin_exposure() == float("inf"))


def test_drawdown_bounds() -> None:
    _check("empty report has no drawdown", _report([]).max_drawdown(1.0) == 0.0)
    flat = _report([0.0, 0.0, 0.0])
    _check("flat equity has no drawdown", flat.max_drawdown(1.0) == 0.0)
    monotone = _report([0.01, 0.02, 0.03])
    _check("monotone gains have no drawdown", monotone.max_drawdown(1.0) == 0.0)


# --------------------------------------------------------------------------
def test_rate_limiter() -> None:
    """20 calls inside the window must not all be free."""
    from okx_ema_trader import http

    http._calls.clear()
    for _ in range(5):
        http.throttle(limit=5, window=2.0)
    _check("5 calls recorded", len(http._calls) == 5, f"{len(http._calls)}")

    # The 6th would have to wait; verify the bookkeeping rather than sleeping.
    import time
    now = time.monotonic()
    while http._calls and now - http._calls[0] >= 2.0:
        http._calls.popleft()
    _check("window eviction works", len(http._calls) == 5)
    http._calls.clear()


# --------------------------------------------------------------------------
def test_proxy_string_shapes():
    """Windows writes `host:port`, a URL, or `scheme=host:port;...`.

    They cannot be told apart by looking for ";": `socks=1.1.1.1:1080` has none,
    and the old test turned it into `http://socks=1.1.1.1:1080` — not a host,
    not a port, and it fails in a way that looks like a dead network.
    """
    print("\nproxy string shapes")
    cases = {
        "": None,
        "   ": None,
        "127.0.0.1:6088": "http://127.0.0.1:6088",
        "http://user:pw@h:1": "http://user:pw@h:1",
        # A password containing "=" must not be mistaken for scheme form.
        "http://user:pa=ss@h:1": "http://user:pa=ss@h:1",
        "socks=1.1.1.1:1080": "http://1.1.1.1:1080",
        "ftp=1.2.3.4:21;http=5.6.7.8:80": "http://5.6.7.8:80",
        "https=a:1;http=b:2": "http://a:1",
        "socks=127.0.0.1:1080;http=127.0.0.1:7890": "http://127.0.0.1:7890",
        "h:1;": "http://h:1",
    }
    for text, expected in cases.items():
        got = http.parse_proxy_server(text)
        _check(f"{text!r:42}", got == expected, f"-> {got!r}, want {expected!r}")


def test_json_is_browser_parseable():
    """`json.dumps` writes NaN/Infinity, which `JSON.parse` refuses.

    `ruin_exposure()` is inf whenever the run never dipped below zero, and one
    bad candle turns every downstream stat into NaN. Either one would make the
    whole backtest panel render blank.
    """
    print("\njson never emits NaN/Infinity")
    from okx_ema_trader.console_server import _json_safe

    payload = {"ruin": float("inf"), "nan": float("nan"),
               "nested": [1.0, float("-inf"), {"x": float("nan")}],
               "ok": 3.5, "text": "keep", "none": None}
    cleaned = _json_safe(payload)
    _check("inf -> None", cleaned["ruin"] is None)
    _check("nan -> None", cleaned["nan"] is None)
    _check("nested cleaned", cleaned["nested"][1] is None and cleaned["nested"][2]["x"] is None)
    _check("finite floats survive", cleaned["ok"] == 3.5)
    _check("other types survive", cleaned["text"] == "keep" and cleaned["none"] is None)
    try:
        text = json.dumps(cleaned, allow_nan=False)
        json.loads(text)
        _check("round-trips through strict JSON", True)
    except ValueError as exc:
        _check("round-trips through strict JSON", False, str(exc))

    from okx_ema_trader import console_api
    _check("console_api._finite(inf)", console_api._finite(float("inf")) is None)
    _check("console_api._finite(nan)", console_api._finite(float("nan")) is None)
    _check("console_api._finite(6.6)", console_api._finite(6.6) == 6.6)

    # np.int64 / np.bool_ are not int subclasses: json.dumps rejects them and
    # the endpoint 500s. numpy floats are caught by the float branch instead.
    mixed = {"i": np.int64(7), "b": np.bool_(True), "f": np.float64(1.5),
             "bad": np.float64("inf"), "arr": np.array([1.0, np.nan])}
    try:
        text = json.dumps(_json_safe(mixed), allow_nan=False)
        back = json.loads(text)
        _check("np.int64 -> 7", back["i"] == 7)
        _check("np.bool_ -> true", back["b"] is True)
        _check("np.float64 kept", back["f"] == 1.5)
        _check("np.inf -> null", back["bad"] is None)
        _check("ndarray -> list with null", back["arr"] == [1.0, None])
    except (TypeError, ValueError) as exc:
        _check("numpy payload serialises", False, str(exc))


def test_stop_lookup_waits_for_registration():
    """`market_entry` closes the position when the stop cannot be found.

    An algo order is not always visible the instant the entry fills, so an empty
    first read means "not yet", not "never". Returning None from inside the loop
    — as the old code did — meant it only retried after an exception, and a
    registration delay became an unnecessary market close.
    """
    print("\nstop lookup retries before declaring failure")
    from okx_ema_trader import broker as broker_module

    class FakeExchange:
        def __init__(self, pages):
            self.pages = list(pages)
            self.calls = 0

        def fetch_open_orders(self, market, params=None):
            self.calls += 1
            return self.pages.pop(0) if self.pages else []

    real_sleep = broker_module.time.sleep
    broker_module.time.sleep = lambda _seconds: None
    try:
        # Appears on the third read -> must be found, not given up on.
        probe = broker_module.Broker.__new__(broker_module.Broker)
        probe.exchange = FakeExchange([[], [], [{"info": {"slTriggerPx": "900"}, "id": "algo1"}]])
        found = probe._find_stop_id("BTC-USDT-SWAP", "o1")
        _check("late stop is found", found == "algo1", f"got {found!r}")
        _check("all three attempts used", probe.exchange.calls == 3,
               f"calls={probe.exchange.calls}")

        # Genuinely absent -> None, but only after exhausting the retries.
        probe2 = broker_module.Broker.__new__(broker_module.Broker)
        probe2.exchange = FakeExchange([[], [], []])
        _check("absent stop -> None", probe2._find_stop_id("BTC-USDT-SWAP", "o1") is None)
        _check("absent stop still retried", probe2.exchange.calls == 3,
               f"calls={probe2.exchange.calls}")

        # An exception is retried like an empty page.
        class Boom:
            def __init__(self):
                self.calls = 0

            def fetch_open_orders(self, market, params=None):
                self.calls += 1
                raise RuntimeError("timeout")

        probe3 = broker_module.Broker.__new__(broker_module.Broker)
        probe3.exchange = Boom()
        _check("exception path -> None", probe3._find_stop_id("BTC-USDT-SWAP", "o1") is None)
        _check("exception path retried", probe3.exchange.calls == 3,
               f"calls={probe3.exchange.calls}")
    finally:
        broker_module.time.sleep = real_sleep


# --------------------------------------------------------------------------
def main() -> int:
    print("instruments & risk metric tests")
    for test in (test_contract_value_maths, test_lot_step_is_not_min_size,
                 test_no_float_drift, test_leverage_ceiling_is_per_instrument,
                 test_market_size_ceiling, test_spec_rejects_untradeable,
                 test_ccxt_symbol_mapping,
                 test_drawdown_is_measured_against_the_account,
                 test_drawdown_scales_with_leverage, test_ruin_leverage,
                 test_drawdown_bounds, test_rate_limiter,
                 test_proxy_string_shapes, test_json_is_browser_parseable,
                 test_stop_lookup_waits_for_registration):
        test()
    print("")
    if FAILURES:
        print(f"FAILURES: {FAILURES}")
    else:
        print("all groups passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
