"""Strategy semantics: crossover events, gates, and backtest/live agreement.

The point of this file is the things that are easy to get silently wrong:

  * an entry is an EVENT (the 5m cross), not a state (EMA20 > EMA60);
  * nothing about a bar may depend on bars that had not closed yet;
  * the backtest must be running the very same decision function as the live bot;
  * the stop loss the live bot places must actually exist in the backtest.

Run:
    set PYTHONPATH=src
    .venv/Scripts/python.exe tests/test_strategy.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import okx_ema_trader.backtest as backtest
from okx_ema_trader.backtest import simulate
from okx_ema_trader.indicators import indicator_frame
from okx_ema_trader.strategy import evaluate_signal

ADX_MIN = 20.0
DEV_MAX = 0.02
LOOKAHEAD_MS = 5 * 60_000


def _i5(fast, slow, prev_fast, prev_slow, close=100.0) -> dict:
    return {"close": close, "ema_fast": fast, "ema_slow": slow,
            "prev_ema_fast": prev_fast, "prev_ema_slow": prev_slow}


def _i15(fast, slow, adx, close) -> dict:
    return {"close": close, "ema_fast": fast, "ema_slow": slow, "adx": adx}


# --------------------------------------------------------------------------
# Entry is an event
# --------------------------------------------------------------------------
def test_entry_fires_once_not_every_bar() -> int:
    """EMA20 above EMA60 for many bars must produce ONE entry, not one per bar."""
    failures = 0
    i15 = _i15(102.0, 100.0, 30.0, close=102.0)  # bullish, ADX ok, zero deviation

    first = evaluate_signal(_i5(101.0, 100.0, 99.0, 100.0), i15, ADX_MIN, DEV_MAX)
    if first is None or first.side != "long":
        print(f"  FAIL the crossing bar must fire: {first}")
        failures += 1

    # Every bar after the cross: EMA20 stays above EMA60, prev also above -> none.
    for bar in range(10):
        signal = evaluate_signal(_i5(101.5 + bar, 100.0, 101.0 + bar, 100.0), i15,
                                 ADX_MIN, DEV_MAX)
        if signal is not None:
            print(f"  FAIL bar +{bar} re-fired without a new cross: {signal}")
            failures += 1

    print(f"  entry fires once: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_death_cross_is_the_mirror_case() -> int:
    failures = 0
    i15 = _i15(98.0, 100.0, 30.0, close=98.0)  # bearish

    signal = evaluate_signal(_i5(99.0, 100.0, 101.0, 100.0), i15, ADX_MIN, DEV_MAX)
    if signal is None or signal.side != "short":
        print(f"  FAIL death cross must fire short: {signal}")
        failures += 1

    # Already below for several bars -> no repeat.
    for bar in range(5):
        signal = evaluate_signal(_i5(98.0 - bar, 100.0, 98.5 - bar, 100.0), i15,
                                 ADX_MIN, DEV_MAX)
        if signal is not None:
            print(f"  FAIL bar +{bar} re-fired: {signal}")
            failures += 1

    print(f"  death cross mirror: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_15m_is_a_state_not_an_event() -> int:
    """15m must not need a crossover — it stays 'allowed' for as long as it holds."""
    failures = 0
    # prev and current 15m EMA are both already bullish (no cross anywhere).
    i15 = _i15(102.0, 100.0, 30.0, close=102.0)
    i15_prev_same = dict(i15)
    # Nothing in the strategy reads a previous 15m bar; a bullish 15m that has
    # been bullish for ages still permits a 5m golden cross.
    signal = evaluate_signal(_i5(101.0, 100.0, 99.0, 100.0), i15_prev_same, ADX_MIN, DEV_MAX)
    if signal is None or signal.side != "long":
        print(f"  FAIL 15m state should permit the long: {signal}")
        failures += 1
    print(f"  15m is a state: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------
def test_deviation_boundary() -> int:
    """deviation = abs(close - EMA20)/EMA20, gate is <= max."""
    failures = 0
    ema20 = 100.0  # the reference the formula divides by: 15m EMA20 itself
    for close, should_pass, label in [
        (102.0, True, "exactly +2%"),
        (98.0, True, "exactly -2%"),
        (102.001, False, "just over +2%"),
        (97.999, False, "just under -2%"),
    ]:
        # fast=ema20, slow=ema20-1 -> bullish 15m, and the gate divides by ema20.
        i15 = _i15(ema20, ema20 - 1, 30.0, close=close)
        signal = evaluate_signal(_i5(101.0, 100.0, 99.0, 100.0), i15, ADX_MIN, DEV_MAX)
        passed = signal is not None
        if passed != should_pass:
            print(f"  FAIL {label}: expected pass={should_pass}, got {signal}")
            failures += 1
    print(f"  deviation boundary: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_deviation_is_15m_only() -> int:
    """A wildly stretched 5m price must not block the entry; only 15m is checked."""
    failures = 0
    i15 = _i15(101.0, 100.0, 30.0, close=101.0)          # 15m deviation 0
    i5 = _i5(101.0, 100.0, 99.0, 100.0, close=5000.0)    # 5m price far away
    signal = evaluate_signal(i5, i15, ADX_MIN, DEV_MAX)
    if signal is None or signal.side != "long":
        print(f"  FAIL 5m deviation must be irrelevant: {signal}")
        failures += 1
    print(f"  deviation is 15m only: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_adx_gate_is_strict() -> int:
    failures = 0
    i5 = _i5(101.0, 100.0, 99.0, 100.0)
    for adx, should_pass in [(20.0, False), (19.99, False), (20.01, True), (45.0, True)]:
        signal = evaluate_signal(i5, _i15(101.0, 100.0, adx, close=101.0), ADX_MIN, DEV_MAX)
        if (signal is not None) != should_pass:
            print(f"  FAIL ADX={adx}: expected pass={should_pass}, got {signal}")
            failures += 1
    print(f"  ADX strict gate: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


# --------------------------------------------------------------------------
# Backtest data
# --------------------------------------------------------------------------
def make_frames(n5: int = 1500, seed: int = 3, drift: float = 0.0):
    """Aligned 5m / 15m synthetic candles, oldest first."""
    rng = random.Random(seed)
    price = 100.0
    ts0 = 1_700_000_000_000
    c5 = []
    for i in range(n5):
        # A real market gaps: bar N+1's open is not bar N's close. Without this
        # the one-bar execution delay is invisible in the results and a
        # close-price fill would pass every test undetected.
        open_ = max(1.0, price * (1 + rng.gauss(0, 0.0008)))
        close = max(1.0, open_ * (1 + rng.gauss(drift, 0.006)))
        high = max(open_, close) * (1 + abs(rng.gauss(0, 0.002)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, 0.002)))
        c5.append([float(ts0 + i * 300_000), open_, high, low, close, 10.0])
        price = close
    c15 = []
    for k in range(n5 // 3):
        chunk = c5[k * 3:(k + 1) * 3]
        c15.append([float(ts0 + k * 900_000), chunk[0][1],
                    max(x[2] for x in chunk), min(x[3] for x in chunk),
                    chunk[-1][4], 30.0])
    return c5, c15


def _decide_at(f5, f15, i: int):
    """Re-run the shared decision for bar i on its own, the way simulate does."""
    ts_15m = f15["ts"].to_numpy()
    cutoff = int(f5["ts"].iloc[i]) + LOOKAHEAD_MS - 15 * 60_000
    j = int(np.searchsorted(ts_15m, cutoff, side="right")) - 1
    if j < 1:
        return None
    row, prev, r15 = f5.iloc[i], f5.iloc[i - 1], f15.iloc[j]
    return evaluate_signal(
        {"close": float(row["close"]), "ema_fast": float(row["ema_fast"]),
         "ema_slow": float(row["ema_slow"]), "prev_ema_fast": float(prev["ema_fast"]),
         "prev_ema_slow": float(prev["ema_slow"])},
        {"close": float(r15["close"]), "ema_fast": float(r15["ema_fast"]),
         "ema_slow": float(r15["ema_slow"]), "adx": float(r15["adx"])},
        ADX_MIN, DEV_MAX,
    )


def test_backtest_calls_the_same_function() -> int:
    """Structural proof: simulate really is wired to strategy.evaluate_signal."""
    failures = 0
    from okx_ema_trader import strategy
    if backtest.evaluate_signal is not strategy.evaluate_signal:
        print("  FAIL: backtest imported a different function object")
        failures += 1
    print(f"  backtest binds the shared function: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_every_backtest_entry_is_a_strategy_signal() -> int:
    """No entry may appear in the backtest that the shared strategy disagrees with."""
    failures = 0
    c5, c15 = make_frames(1500, seed=29, drift=0.0006)
    f5 = indicator_frame(c5, 20, 60, 14)
    f15 = indicator_frame(c15, 20, 60, 14)
    report = simulate(f5, f15, ADX_MIN, DEV_MAX, 8.0, LOOKAHEAD_MS, 3.0)
    if not report.trades:
        print("  FAIL: no trades produced; cannot verify parity")
        return 1

    index_by_ts = {int(f5["ts"].iloc[i]): i for i in range(len(f5))}
    entry_ts = {t.entry_signal_ts: t.side for t in report.trades}

    for ts, side in entry_ts.items():
        i = index_by_ts.get(ts)
        if i is None:
            print(f"  FAIL entry signal ts {ts} is not a real bar")
            failures += 1
            continue
        signal = _decide_at(f5, f15, i)
        if signal is None or signal.side != side:
            print(f"  FAIL entry at bar {i} not justified by evaluate_signal "
                  f"(trade={side}, signal={signal})")
            failures += 1
        # The entry must carry BOTH times, exactly one bar apart, and the fill
        # price must be the open of that next bar.
        trade = next(t for t in report.trades if t.entry_signal_ts == ts)
        j = index_by_ts.get(trade.entry_exec_ts)
        if j != i + 1:
            print(f"  FAIL entry filled on bar {j}, expected bar {i + 1}")
            failures += 1
            continue
        if abs(trade.entry_price - float(f5["open"].iloc[j])) > 1e-9:
            print(f"  FAIL fill {trade.entry_price} is not bar {j}'s open "
                  f"{float(f5['open'].iloc[j])}")
            failures += 1
        if abs(trade.entry_price - float(f5["close"].iloc[i])) < 1e-12:
            print(f"  FAIL fill equals the signal bar's close "
                  f"({trade.entry_price}) — that is a look-ahead fill")
            failures += 1

    # And no bar without a signal may have produced an entry.
    for i in range(1, len(f5)):
        ts = int(f5["ts"].iloc[i])
        if ts in entry_ts and _decide_at(f5, f15, i) is None:
            print(f"  FAIL bar {i} had no signal but an entry was booked")
            failures += 1

    print(f"  {len(entry_ts)} entries all justified: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_entry_uses_next_bar_open() -> int:
    """A cross on bar N must fill at bar N+1's OPEN, never at bar N's close.

    The test re-prices bar N+1's open 1% away from bar N's close. Re-pricing
    the open cannot change any EMA (they are computed from closes), so the very
    same signal must still fire — only the fill price may move.
    """
    failures = 0
    c5, c15 = make_frames(1500, seed=3, drift=0.0004)
    f5 = indicator_frame(c5, 20, 60, 14)
    f15 = indicator_frame(c15, 20, 60, 14)
    base = simulate(f5, f15, ADX_MIN, DEV_MAX, 8.0, LOOKAHEAD_MS, 3.0)
    if not base.trades:
        print("  FAIL: no trades produced; cannot verify the fill price")
        return 1

    trade = base.trades[0]
    index_by_ts = {int(f5["ts"].iloc[i]): i for i in range(len(f5))}
    i = index_by_ts[trade.entry_signal_ts]
    if i + 1 >= len(c5):
        print("  FAIL: signal fired on the last bar; nothing to fill")
        return 1

    close_signal_bar = float(f5["close"].iloc[i])
    gap_open = close_signal_bar * 1.01          # 1% away: clearly not the close
    c5[i + 1][1] = gap_open                     # row = [ts, open, high, low, close, vol]
    c5[i + 1][2] = max(c5[i + 1][2], gap_open)  # keep high >= open

    again = simulate(indicator_frame(c5, 20, 60, 14), f15, ADX_MIN, DEV_MAX,
                     8.0, LOOKAHEAD_MS, 3.0)
    matched = [t for t in again.trades if t.entry_signal_ts == trade.entry_signal_ts]
    if not matched:
        print(f"  FAIL: the signal at bar {i} vanished after re-pricing the open")
        return 1
    filled = matched[0]

    if abs(filled.entry_price - gap_open) > 1e-9:
        print(f"  FAIL fill is not bar N+1's open: {filled.entry_price} != {gap_open}")
        failures += 1
    if abs(filled.entry_price - close_signal_bar) < 1e-9:
        print(f"  FAIL fill equals bar N's close ({close_signal_bar}) — look-ahead fill")
        failures += 1
    if filled.entry_exec_ts != int(f5["ts"].iloc[i + 1]):
        print(f"  FAIL execution ts is not bar N+1: {filled.entry_exec_ts}")
        failures += 1
    if filled.entry_signal_ts != int(f5["ts"].iloc[i]):
        print(f"  FAIL signal ts is not bar N: {filled.entry_signal_ts}")
        failures += 1

    print(f"  next-bar open fill (signal {close_signal_bar:.2f} -> fill {gap_open:.2f}): "
          f"{'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_signal_on_last_bar_is_never_filled() -> int:
    """Requirement: if bar N+1 does not exist, no trade is booked at all."""
    failures = 0
    c5, c15 = make_frames(1500, seed=3, drift=0.0004)
    f5 = indicator_frame(c5, 20, 60, 14)
    f15 = indicator_frame(c15, 20, 60, 14)
    base = simulate(f5, f15, ADX_MIN, DEV_MAX, 8.0, LOOKAHEAD_MS, 3.0)
    if not base.trades:
        print("  FAIL: no trades produced; cannot test the last-bar case")
        return 1

    index_by_ts = {int(f5["ts"].iloc[i]): i for i in range(len(f5))}
    i = index_by_ts[base.trades[0].entry_signal_ts]

    # Cut the series so bar i IS the final bar: EMAs are causal so the same
    # signal still fires, but there is no bar i+1 to fill it on.
    f5_cut = indicator_frame(c5[:i + 1], 20, 60, 14)
    f15_cut = indicator_frame(c15[:(i + 1) // 3], 20, 60, 14)
    cut = simulate(f5_cut, f15_cut, ADX_MIN, DEV_MAX, 8.0, LOOKAHEAD_MS, 3.0)

    leaked = [t for t in cut.trades if t.entry_signal_ts == int(f5["ts"].iloc[i])]
    if leaked:
        print(f"  FAIL: a signal on the final bar was filled anyway ({leaked})")
        failures += 1
    if cut.unfilled_signals != 1:
        print(f"  FAIL: the dropped signal was not reported "
              f"(unfilled_signals={cut.unfilled_signals})")
        failures += 1

    print(f"  last-bar signal not filled: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_no_lookahead_bias() -> int:
    """Decisions up to bar N must not change when later bars are revealed.

    EMA/ADX are causal, so appending future candles cannot alter what happened
    earlier. If it does, something is reading the future.
    """
    failures = 0
    c5, c15 = make_frames(1500, seed=5, drift=0.0006)
    cutoff = 900  # decide on the first 900 bars only

    f5_full = indicator_frame(c5, 20, 60, 14)
    f15_full = indicator_frame(c15, 20, 60, 14)
    full = simulate(f5_full, f15_full, ADX_MIN, DEV_MAX, 8.0, LOOKAHEAD_MS, 3.0)

    f5_cut = indicator_frame(c5[:cutoff], 20, 60, 14)
    f15_cut = indicator_frame(c15[:cutoff // 3], 20, 60, 14)
    cut = simulate(f5_cut, f15_cut, ADX_MIN, DEV_MAX, 8.0, LOOKAHEAD_MS, 3.0)

    last_ts = int(f5_cut["ts"].iloc[-1])
    entries_full = [(t.entry_signal_ts, t.side) for t in full.trades
                    if t.entry_signal_ts <= last_ts]
    # The truncated run force-closes whatever it still holds; that is not an entry.
    entries_cut = [(t.entry_signal_ts, t.side) for t in cut.trades
                   if t.entry_signal_ts <= last_ts]

    if entries_full != entries_cut:
        print(f"  FAIL future bars changed past decisions")
        print(f"    full    : {entries_full}")
        print(f"    truncated: {entries_cut}")
        failures += 1
    print(f"  no look-ahead ({len(entries_cut)} entries stable): "
          f"{'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


# --------------------------------------------------------------------------
# Stop loss
# --------------------------------------------------------------------------
def test_stop_loss_is_actually_simulated() -> int:
    """A tight stop must produce stop exits; an unreachable one must produce none."""
    failures = 0
    c5, c15 = make_frames(1500, seed=29, drift=0.0006)
    f5 = indicator_frame(c5, 20, 60, 14)
    f15 = indicator_frame(c15, 20, 60, 14)

    tight = simulate(f5, f15, ADX_MIN, DEV_MAX, 8.0, LOOKAHEAD_MS, 0.05)
    if tight.stopped_out == 0:
        print("  FAIL: a 0.05% stop never triggered — it is not being simulated")
        failures += 1
    if not any(t.exit_reason == "stop" for t in tight.trades):
        print("  FAIL: no trade recorded a stop exit")
        failures += 1

    # 100% away is unreachable in this data, so nothing should ever be stopped.
    wide = simulate(f5, f15, ADX_MIN, DEV_MAX, 8.0, LOOKAHEAD_MS, 100.0)
    if wide.stopped_out != 0:
        print(f"  FAIL: an unreachable stop fired {wide.stopped_out} times")
        failures += 1

    print(f"  stop simulated (tight={tight.stopped_out}, wide={wide.stopped_out}): "
          f"{'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_stop_exits_are_conservative() -> int:
    """A stop exit may never beat the stop level — no favourable intrabar order."""
    failures = 0
    pct = 0.5
    c5, c15 = make_frames(1500, seed=29, drift=0.0006)
    f5 = indicator_frame(c5, 20, 60, 14)
    f15 = indicator_frame(c15, 20, 60, 14)
    report = simulate(f5, f15, ADX_MIN, DEV_MAX, 8.0, LOOKAHEAD_MS, pct)

    stops = [t for t in report.trades if t.exit_reason == "stop"]
    if not stops:
        print("  FAIL: no stop exits to inspect")
        return 1
    for t in stops:
        if t.side == "long":
            limit = t.entry_price * (1 - pct / 100.0)
            if t.exit_price > limit * (1 + 1e-9):
                print(f"  FAIL long stop filled above the stop: {t.exit_price} > {limit}")
                failures += 1
        else:
            limit = t.entry_price * (1 + pct / 100.0)
            if t.exit_price < limit * (1 - 1e-9):
                print(f"  FAIL short stop filled below the stop: {t.exit_price} < {limit}")
                failures += 1
    print(f"  {len(stops)} stop exits conservative: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def main() -> int:
    print("strategy semantics tests (offline)")
    print("")
    failures = 0
    for test in (
        test_entry_fires_once_not_every_bar,
        test_death_cross_is_the_mirror_case,
        test_15m_is_a_state_not_an_event,
        test_deviation_boundary,
        test_deviation_is_15m_only,
        test_adx_gate_is_strict,
        test_backtest_calls_the_same_function,
        test_every_backtest_entry_is_a_strategy_signal,
        test_entry_uses_next_bar_open,
        test_signal_on_last_bar_is_never_filled,
        test_no_lookahead_bias,
        test_stop_loss_is_actually_simulated,
        test_stop_exits_are_conservative,
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
