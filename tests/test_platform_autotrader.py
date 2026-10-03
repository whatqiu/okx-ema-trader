"""Tests for the auto-trader (offline, strategy injected).

The auto-trader's value is entirely in its plumbing: the decision belongs to
strategy.evaluate_signal (tested elsewhere). Here we pin: one evaluation per
bar, no-signal = no action, opposite position closed before opening, no
duplicate position on a repeated signal, and failures recorded not raised.

Run:
    .venv/Scripts/python.exe tests/test_platform_autotrader.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "platform" / "backend"))

import autotrader  # noqa: E402
import paper  # noqa: E402
from okx_ema_trader.storage import Store  # noqa: E402
from okx_ema_trader.strategy import Signal  # noqa: E402

INST = "MU-USDT-SWAP"
_checks = 0

STEP5 = 300_000
STEP15 = 900_000
BASE_TS = 1_790_000_000_000  # fixed past ts; only ordering matters here


def check(condition: bool, message: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(message)


def fresh_store():
    tmp = tempfile.TemporaryDirectory()
    return Store(Path(tmp.name) / "test.db"), tmp


def seed_candles(store, n5: int = 120, n15: int = 60, price: float = 100.0):
    """Enough confirmed bars to clear indicator warmup (ema_slow=60).

    Prices must MOVE: perfectly flat synthetic bars give +DM == -DM == 0,
    so Wilder's DX divides 0 by 0 and ADX is NaN forever. Real markets
    wiggle; the seed must too.
    """
    def bar(ts, p):
        return [str(ts), f"{p:.4f}", f"{p + 0.6:.4f}", f"{p - 0.6:.4f}",
                f"{p + 0.2:.4f}", "10", "1000", "1000", "1"]

    rows5 = [bar(BASE_TS + i * STEP5, price + i * 0.05 + (i % 3) * 0.15)
             for i in range(n5)]
    rows15 = [bar(BASE_TS + i * STEP15, price + i * 0.15 + (i % 3) * 0.3)
              for i in range(n15)]
    store.upsert_candles(INST, "5m", rows5)
    store.upsert_candles(INST, "15m", rows15)
    return BASE_TS + (n5 - 1) * STEP5


def fake_tick(store, inst):
    return {"bid": 99.5, "ask": 100.5, "last": 100.0}


def long_sig(*a, **kw):
    return Signal("long", "test cross")


def short_sig(*a, **kw):
    return Signal("short", "test cross")


def no_sig(*a, **kw):
    return None


# --------------------------------------------------------------------------
def test_long_signal_opens_long_and_records():
    store, tmp = fresh_store()
    seed_candles(store)
    autotrader.set_state(store, enabled=True, symbol=INST, notional=100,
                         leverage=5)
    out = autotrader.maybe_trade(store, INST, signal_fn=long_sig,
                                 ticker_fn=fake_tick)
    check(out.get("acted"), f"expected action, got {out}")
    check(out["opened"] and out["opened"]["entry_price"] == 100.5,
          "long must cross the spread: fill at ask")
    positions = store.open_orders(INST)
    check(len(positions) == 1 and positions[0]["side"] == "long",
          "no long position booked")
    sigs = store.signals(limit=5, inst_id=INST)
    check(bool(sigs) and sigs[0]["acted"] == 1, "signal not recorded as acted")
    store.close(); tmp.cleanup()


def test_same_bar_never_evaluated_twice():
    store, tmp = fresh_store()
    seed_candles(store)
    autotrader.set_state(store, enabled=True, symbol=INST, notional=100,
                         leverage=5)
    autotrader.maybe_trade(store, INST, signal_fn=no_sig, ticker_fn=fake_tick)
    out = autotrader.maybe_trade(store, INST, signal_fn=long_sig,
                                 ticker_fn=fake_tick)
    check(out.get("why") == "bar already evaluated",
          f"bar ts guard failed: {out}")
    check(store.open_orders(INST) == [], "guard failed: order booked on re-eval")
    store.close(); tmp.cleanup()


def test_no_signal_means_no_action():
    store, tmp = fresh_store()
    seed_candles(store)
    out = autotrader.maybe_trade(store, INST, signal_fn=no_sig,
                                 ticker_fn=fake_tick)
    check(not out.get("acted"), "no-signal must not trade (空仓不操作)")
    check(store.open_orders(INST) == [], "position opened without a signal")
    store.close(); tmp.cleanup()


def test_opposite_signal_flips_position():
    store, tmp = fresh_store()
    last_ts = seed_candles(store)
    autotrader.set_state(store, enabled=True, symbol=INST, notional=100,
                         leverage=5)
    autotrader.maybe_trade(store, INST, signal_fn=long_sig, ticker_fn=fake_tick)
    # A NEW bar arrives -> the short signal fires on it.
    store.upsert_candles(INST, "5m",
                         [[str(last_ts + STEP5), "100", "101", "99", "100",
                           "10", "1000", "1000", "1"]])
    out = autotrader.maybe_trade(store, INST, signal_fn=short_sig,
                                 ticker_fn=fake_tick)
    check(out.get("acted"), f"flip did not act: {out}")
    check(len(out["closed"]) == 1, "old long not closed on flip")
    opens = store.open_orders(INST)
    check(len(opens) == 1 and opens[0]["side"] == "short",
          "flip did not leave exactly one short")
    check(opens[0]["entry_price"] == 99.5, "short must fill at bid")
    store.close(); tmp.cleanup()


def test_repeated_same_side_signal_is_noop():
    store, tmp = fresh_store()
    last_ts = seed_candles(store)
    autotrader.set_state(store, enabled=True, symbol=INST, notional=100,
                         leverage=5)
    autotrader.maybe_trade(store, INST, signal_fn=long_sig, ticker_fn=fake_tick)
    store.upsert_candles(INST, "5m",
                         [[str(last_ts + STEP5), "100", "101", "99", "100",
                           "10", "1000", "1000", "1"]])
    out = autotrader.maybe_trade(store, INST, signal_fn=long_sig,
                                 ticker_fn=fake_tick)
    check(not out.get("opened"), "duplicate long opened on repeat signal")
    check(len(store.open_orders(INST)) == 1, "position count changed on repeat")
    store.close(); tmp.cleanup()


def test_margin_failure_recorded_not_raised():
    store, tmp = fresh_store()
    seed_candles(store)
    autotrader.set_state(store, enabled=True, symbol=INST,
                         notional=10_000_000, leverage=1)
    out = autotrader.maybe_trade(store, INST, signal_fn=long_sig,
                                 ticker_fn=fake_tick)
    check("error" in out, "margin failure must surface as error, not crash")
    state = autotrader.get_state(store)
    check("可用余额不足" in state["last_error"], "error not persisted for UI")
    store.close(); tmp.cleanup()


def test_missing_spread_recorded_not_raised():
    store, tmp = fresh_store()
    seed_candles(store)

    def empty_tick(s, i):
        return {"bid": None, "ask": None, "last": None}

    out = autotrader.maybe_trade(store, INST, signal_fn=long_sig,
                                 ticker_fn=empty_tick)
    check("error" in out, "missing spread must surface as error")
    store.close(); tmp.cleanup()


TESTS = [value for name, value in sorted(globals().items())
         if name.startswith("test_") and callable(value)]

if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(TESTS)} tests, {_checks} checks — all green")
