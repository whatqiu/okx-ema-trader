"""Tests for multi-symbol scanning (offline, strategy injected).

Scanning a watch list is not "the same loop N times". It introduces two
failure modes that a single-symbol trader cannot have:

  1. Dedup leakage — if the per-bar stamp is not keyed by instrument, the
     first symbol scanned consumes the bar and every other symbol is skipped
     for that bar. Silent, and it looks exactly like "no signals today".
  2. Error blast radius — one instrument with no bid/ask, a 404, or thin
     candles aborts the whole scan, so every OTHER symbol stops trading too.

Both are pinned here, along with capacity and the scan-state record that makes
an empty `signals` table interpretable.

Run:
    .venv/Scripts/python.exe tests/test_platform_autotrader_multi.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "platform" / "backend"))

import autotrader  # noqa: E402
from okx_ema_trader.storage import Store  # noqa: E402
from okx_ema_trader.strategy import Signal  # noqa: E402

A = "MU-USDT-SWAP"
B = "ZEC-USDT-SWAP"
C = "BTC-USDT-SWAP"

STEP5 = 300_000
STEP15 = 900_000
BASE_TS = 1_790_000_000_000  # fixed past ts; only ordering matters here

_checks = 0


def check(condition: bool, message: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(message)


def fresh_store():
    tmp = tempfile.TemporaryDirectory()
    return Store(Path(tmp.name) / "test.db"), tmp


def seed(store, inst: str, n5: int = 120, n15: int = 60):
    """Enough confirmed bars to clear warmup (ema_slow=50). Prices must MOVE:
    flat bars make +DM == -DM == 0, so Wilder's DX is 0/0 and ADX stays NaN.
    """
    def bar(ts, p):
        return [str(ts), f"{p:.4f}", f"{p + 0.6:.4f}", f"{p - 0.6:.4f}",
                f"{p + 0.2:.4f}", "10", "1000", "1000", "1"]

    store.upsert_candles(inst, "5m",
                         [bar(BASE_TS + i * STEP5, 100 + i * 0.05 + (i % 3) * 0.15)
                          for i in range(n5)])
    store.upsert_candles(inst, "15m",
                         [bar(BASE_TS + i * STEP15, 100 + i * 0.15 + (i % 3) * 0.3)
                          for i in range(n15)])


def fake_tick(store, inst):
    return {"bid": 99.5, "ask": 100.5, "last": 100.0}


def long_sig(*a, **kw):
    return Signal("long", "test cross")


def no_sig(*a, **kw):
    return None


def watch(store, *symbols, notional=100, leverage=5, max_positions=3):
    autotrader.set_state(store, enabled=True, symbols=list(symbols),
                         notional=notional, leverage=leverage,
                         max_positions=max_positions)


# --------------------------------------------------------------------------
# Watch-list plumbing
# --------------------------------------------------------------------------
def test_symbols_round_trip_and_symbol_mirrors_the_head():
    store, tmp = fresh_store()
    autotrader.set_state(store, symbols=[A, B])
    state = autotrader.get_state(store)
    check(state["symbols"] == [A, B], f"watch list lost: {state['symbols']}")
    check(state["symbol"] == A, "legacy `symbol` must mirror the head")
    store.close(); tmp.cleanup()


def test_legacy_single_symbol_migrates_into_the_watch_list():
    """An install that predates scanning must keep trading what it traded."""
    store, tmp = fresh_store()
    store.set_meta("auto_symbol", A)
    state = autotrader.get_state(store)
    check(state["symbols"] == [A], f"legacy symbol not migrated: {state['symbols']}")
    store.close(); tmp.cleanup()


def test_setting_one_symbol_replaces_the_list():
    store, tmp = fresh_store()
    autotrader.set_state(store, symbols=[A, B])
    autotrader.set_state(store, symbol=C)
    check(autotrader.get_state(store)["symbols"] == [C],
          "a single symbol must replace, not append to, the watch list")
    store.close(); tmp.cleanup()


def test_watch_list_is_capped():
    store, tmp = fresh_store()
    too_many = [f"S{i}-USDT-SWAP" for i in range(autotrader.MAX_SYMBOLS + 3)]
    try:
        autotrader.set_state(store, symbols=too_many)
        raise AssertionError(f"expected a cap at {autotrader.MAX_SYMBOLS}")
    except ValueError as exc:
        check("最多监控" in str(exc), f"unexpected refusal: {exc}")
    store.close(); tmp.cleanup()


def test_disabled_or_empty_list_scans_nothing():
    store, tmp = fresh_store()
    seed(store, A)
    autotrader.set_state(store, enabled=True, symbols=[])
    out = autotrader.maybe_trade_all(store, signal_fn=long_sig,
                                     ticker_fn=fake_tick, top_up=False)
    check(out["scanned"] == 0, f"empty watch list must scan nothing: {out}")
    check(out["results"] == [], "no results expected")

    autotrader.set_state(store, enabled=False, symbols=[A])
    out = autotrader.maybe_trade_all(store, signal_fn=long_sig,
                                     ticker_fn=fake_tick, top_up=False)
    check(out["scanned"] == 0, "disabled must scan nothing")
    store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# The two failure modes that only exist once you scan many symbols
# --------------------------------------------------------------------------
def test_dedup_is_per_symbol_not_global():
    """Regression: a shared bar stamp let one symbol swallow the whole bar."""
    store, tmp = fresh_store()
    seed(store, A)
    seed(store, B)
    watch(store, A, B)
    out = autotrader.maybe_trade_all(store, signal_fn=long_sig,
                                     ticker_fn=fake_tick, top_up=False)
    check(out["scanned"] == 2, f"both symbols must be scanned: {out}")
    opened = {r.get("inst_id") for r in out["results"] if r.get("opened")}
    check(opened == {A, B},
          f"dedup leaked — only {opened} traded; the second symbol was skipped")
    check(len(store.open_orders()) == 2, "expected one position per symbol")
    store.close(); tmp.cleanup()


def test_re_evaluating_the_same_bar_is_still_refused_per_symbol():
    store, tmp = fresh_store()
    seed(store, A)
    seed(store, B)
    watch(store, A, B)
    autotrader.maybe_trade_all(store, signal_fn=long_sig, ticker_fn=fake_tick,
                               top_up=False)
    again = autotrader.maybe_trade_all(store, signal_fn=long_sig,
                                       ticker_fn=fake_tick, top_up=False)
    for res in again["results"]:
        check(res.get("why") == "bar already evaluated",
              f"re-evaluation not refused: {res}")
    check(len(store.open_orders()) == 2, "a second scan must not open more")
    store.close(); tmp.cleanup()


def test_one_symbol_failing_does_not_stop_the_others():
    """The scan must not die because one instrument has no spread."""
    store, tmp = fresh_store()
    seed(store, A)
    seed(store, B)
    watch(store, A, B)

    def broken_tick(s, inst):
        if inst == A:
            raise KeyError(f"{inst} 缺少买一/卖一价，无法成交")
        return fake_tick(s, inst)

    out = autotrader.maybe_trade_all(store, signal_fn=long_sig,
                                     ticker_fn=broken_tick, top_up=False)
    check(out["scanned"] == 2, "a failing symbol must not shorten the scan")
    by_inst = {r.get("inst_id"): r for r in out["results"]}
    check("error" in by_inst[A], f"{A} failure not reported: {by_inst[A]}")
    check(by_inst[B].get("acted"), f"{B} should still have traded: {by_inst[B]}")
    check(len(store.open_orders()) == 1, "exactly the healthy symbol trades")
    store.close(); tmp.cleanup()


def test_a_symbol_that_raises_is_contained_not_propagated():
    store, tmp = fresh_store()
    seed(store, A)
    seed(store, B)
    watch(store, A, B)

    def exploding_tick(s, inst):
        if inst == A:
            raise RuntimeError("unexpected shape from OKX")
        return fake_tick(s, inst)

    # Must not raise out of the scan.
    out = autotrader.maybe_trade_all(store, signal_fn=long_sig,
                                     ticker_fn=exploding_tick, top_up=False)
    check(out["scanned"] == 2, "scan must complete despite one exploded symbol")
    state = autotrader.get_state(store)
    check("unexpected shape" in (state["last_error"] or ""),
          f"the reason must reach the UI: {state['last_error']}")
    check(len(store.open_orders()) == 1, "the healthy symbol still traded")
    store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# Capacity
# --------------------------------------------------------------------------
def test_position_cap_blocks_new_opens():
    store, tmp = fresh_store()
    seed(store, A)
    seed(store, B)
    seed(store, C)
    watch(store, A, B, C, max_positions=1)
    out = autotrader.maybe_trade_all(store, signal_fn=long_sig,
                                     ticker_fn=fake_tick, top_up=False)
    check(len(store.open_orders()) == 1,
          f"cap of 1 breached: {len(store.open_orders())} positions")
    capped = [r for r in out["results"] if r.get("why") == "at position cap"]
    check(len(capped) == 2, f"the skipped symbols must say why: {out['results']}")
    store.close(); tmp.cleanup()


def test_cap_still_allows_closing_an_opposite_position():
    """Reducing risk never needs permission; only opening does."""
    store, tmp = fresh_store()
    seed(store, A)
    watch(store, A, max_positions=1)
    autotrader.maybe_trade_all(store, signal_fn=lambda *a, **k: Signal(
        "long", "seed"), ticker_fn=fake_tick, top_up=False)
    check(len(store.open_orders()) == 1, "seed position missing")

    # Advance a bar so the dedup guard releases, then signal the other way.
    seed(store, A, n5=121, n15=61)
    out = autotrader.maybe_trade_all(
        store, signal_fn=lambda *a, **k: Signal("short", "flip"),
        ticker_fn=fake_tick, top_up=False)
    res = out["results"][0]
    check(res.get("closed"), f"opposite must be closed even at the cap: {res}")
    check(len(store.open_orders()) == 1, "one position, now short")
    check(store.open_orders()[0]["side"] == "short", "should have flipped")
    store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# Scan state: making an empty signals table interpretable
# --------------------------------------------------------------------------
def test_scan_records_why_there_was_no_signal():
    store, tmp = fresh_store()
    seed(store, A)
    seed(store, B)
    watch(store, A, B)
    autotrader.maybe_trade_all(store, signal_fn=no_sig, ticker_fn=fake_tick,
                               top_up=False)
    state = autotrader.get_state(store)
    for inst in (A, B):
        scan = state["scan"].get(inst)
        check(scan is not None, f"{inst} has no scan record")
        check(scan.get("why") == "no signal — 空仓不操作",
              f"{inst} reason missing: {scan}")
        check(scan.get("side") is None, "no signal means no side")
    check(store.signals(limit=10) == [],
          "a non-signal must not pollute the signals table")
    store.close(); tmp.cleanup()


def test_scan_records_a_fired_signal_with_its_adx():
    store, tmp = fresh_store()
    seed(store, A)
    watch(store, A)
    autotrader.maybe_trade_all(store, signal_fn=long_sig, ticker_fn=fake_tick,
                               top_up=False)
    scan = autotrader.get_state(store)["scan"][A]
    check(scan["side"] == "long", f"scan lost the side: {scan}")
    check(scan.get("acted"), f"scan should report it acted: {scan}")
    check("adx" in scan, "ADX belongs in the record — it is the filter that fired")
    store.close(); tmp.cleanup()


TESTS = [value for name, value in sorted(globals().items())
         if name.startswith("test_") and callable(value)]

if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(TESTS)} tests, {_checks} checks — all green")
