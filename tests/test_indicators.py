"""Regression guard for indicators.py — runnable without pytest.

    cd F:\\Claude\\okx-ema-trader
    set PYTHONPATH=src
    .venv\\Scripts\\python.exe tests\\test_indicators.py

Why this file exists: indicators.py was refactored so backtests could reuse the
indicator series instead of recomputing a rolling window per bar. That touched
the code path the live bot actually trades on. `REFERENCE_BEFORE_REFACTOR` is a
verbatim copy of the original implementation, kept here forever, and this test
proves the current one is numerically identical to it.
"""

from __future__ import annotations

import random
import sys

import numpy as np
import pandas as pd

from okx_ema_trader.indicators import calculate_indicators, indicator_frame

COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
# The values the frozen reference produced. Still the regression contract.
ORIGINAL_KEYS = ("ts", "close", "ema_fast", "ema_slow", "adx")


def REFERENCE_BEFORE_REFACTOR(candles, ema_fast, ema_slow, adx_period):  # noqa: N802
    """Verbatim copy of the pre-refactor implementation. Do not 'clean this up'."""
    if len(candles) < max(ema_slow, adx_period * 2):
        return {}
    frame = pd.DataFrame(candles, columns=COLUMNS)
    high, low, close = frame["high"], frame["low"], frame["close"]
    frame["ema_fast"] = close.ewm(span=ema_fast, adjust=False).mean()
    frame["ema_slow"] = close.ewm(span=ema_slow, adjust=False).mean()

    up_move = high.diff()
    down_move = -low.diff()
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    alpha = 1 / adx_period
    atr = tr.ewm(alpha=alpha, adjust=False).mean().replace(0.0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    frame["adx"] = dx.ewm(alpha=alpha, adjust=False).mean()

    row = frame.iloc[-1]
    if pd.isna(row[["ema_fast", "ema_slow", "adx"]]).any():
        return {}
    return {
        "ts": int(row["ts"]),
        "close": float(row["close"]),
        "ema_fast": float(row["ema_fast"]),
        "ema_slow": float(row["ema_slow"]),
        "adx": float(row["adx"]),
    }


def synthetic_candles(count: int, seed: int) -> list[list[float]]:
    """Random walk with realistic OHLC relationships. Deterministic per seed."""
    rng = random.Random(seed)
    price = 60000.0
    ts = 1_700_000_000_000
    candles = []
    for index in range(count):
        open_price = price
        price = max(1.0, price * (1 + rng.gauss(0, 0.004)))
        high = max(open_price, price) * (1 + abs(rng.gauss(0, 0.001)))
        low = min(open_price, price) * (1 - abs(rng.gauss(0, 0.001)))
        candles.append([float(ts + index * 300_000), open_price, high, low, price, rng.random() * 10])
    return candles


def test_matches_pre_refactor_implementation() -> int:
    failures = 0
    checks = 0
    for seed in range(5):
        candles = synthetic_candles(400, seed)
        for ema_fast, ema_slow, adx_period in [(20, 60, 14), (5, 13, 7), (50, 200, 21)]:
            checks += 1
            current = calculate_indicators(candles, ema_fast, ema_slow, adx_period)
            expected = REFERENCE_BEFORE_REFACTOR(candles, ema_fast, ema_slow, adx_period)
            # Compare the ORIGINAL five values only. calculate_indicators now also
            # returns prev_ema_fast/prev_ema_slow for crossover detection, which the
            # frozen reference never produced; those are covered separately below.
            if any(current[key] != expected[key] for key in ORIGINAL_KEYS):
                failures += 1
                print(f"  FAIL seed={seed} ema={ema_fast}/{ema_slow} adx={adx_period}")
                print(f"    current ={current}")
                print(f"    expected={expected}")
    print(f"  calculate_indicators vs frozen reference: {checks - failures}/{checks} identical")
    return failures


def test_prev_row_is_the_previous_bar() -> int:
    """Crossover detection reads the bar before; make sure that is what it gets."""
    failures = 0
    checks = 0
    for seed in range(3):
        candles = synthetic_candles(300, seed)
        frame = indicator_frame(candles, 20, 60, 14)
        row = calculate_indicators(candles, 20, 60, 14)
        checks += 1
        prev = frame.iloc[-2]
        if abs(row["prev_ema_fast"] - float(prev["ema_fast"])) > 1e-12:
            failures += 1
            print(f"  FAIL seed={seed}: prev_ema_fast is not frame.iloc[-2]")
        if abs(row["prev_ema_slow"] - float(prev["ema_slow"])) > 1e-12:
            failures += 1
            print(f"  FAIL seed={seed}: prev_ema_slow is not frame.iloc[-2]")
    print(f"  prev bar equals frame.iloc[-2]: {checks - failures}/{checks} agree")
    return failures


def test_short_series_returns_empty() -> int:
    failures = 0
    short = synthetic_candles(10, 1)
    if calculate_indicators(short, 20, 60, 14) != {}:
        failures += 1
        print("  FAIL: short series should return {} instead of garbage indicators")
    if indicator_frame(short, 20, 60, 14).empty:
        pass
    else:
        failures += 1
        print("  FAIL: indicator_frame should return an empty frame for short series")
    print("  short-series guard: ok" if failures == 0 else "  short-series guard: FAILED")
    return failures


def test_frame_last_row_equals_row_api() -> int:
    """The two public entry points must agree, or live and backtest diverge."""
    failures = 0
    checks = 0
    for seed in range(3):
        candles = synthetic_candles(300, seed)
        frame = indicator_frame(candles, 20, 60, 14)
        checks += 1
        row = calculate_indicators(candles, 20, 60, 14)
        tail = frame.iloc[-1]
        if abs(float(tail["ema_fast"]) - row["ema_fast"]) > 1e-12 or abs(float(tail["adx"]) - row["adx"]) > 1e-12:
            failures += 1
            print(f"  FAIL seed={seed}: frame tail disagrees with row API")
    print(f"  indicator_frame tail vs calculate_indicators: {checks - failures}/{checks} agree")
    return failures


def main() -> int:
    print("indicators regression tests")
    failures = 0
    failures += test_matches_pre_refactor_implementation()
    failures += test_prev_row_is_the_previous_bar()
    failures += test_short_series_returns_empty()
    failures += test_frame_last_row_equals_row_api()
    print("")
    print("PASS" if failures == 0 else f"FAIL ({failures} checks failed)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
