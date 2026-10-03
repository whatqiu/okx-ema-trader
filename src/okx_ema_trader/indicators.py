from __future__ import annotations

import numpy as np
import pandas as pd

COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def indicator_frame(candles: list[list[float]], ema_fast: int, ema_slow: int, adx_period: int) -> pd.DataFrame:
    """Return the full indicator series (ts, close, ema_fast, ema_slow, adx).

    Row-level `calculate_indicators` is a thin wrapper over this for the live
    bot. Backtests use the frame directly: computing once over the whole series
    is ~1000x faster than recomputing a rolling window per bar, and EMA/ADX are
    recursive-but-forgetful so the two agree to well below float noise.
    """
    frame = pd.DataFrame(candles, columns=COLUMNS)
    empty = pd.DataFrame(columns=[*COLUMNS, "ema_fast", "ema_slow", "adx"])
    if len(frame) < max(ema_slow, adx_period * 2):
        return empty
    high, low, close = frame["high"], frame["low"], frame["close"]
    frame["ema_fast"] = close.ewm(span=ema_fast, adjust=False).mean()
    frame["ema_slow"] = close.ewm(span=ema_slow, adjust=False).mean()
    _attach_adx(frame, adx_period)
    return frame


def _attach_adx(frame: pd.DataFrame, adx_period: int) -> None:
    """Wilder ADX, in place. Split out so backtest and live paths can't drift."""
    high, low, close = frame["high"], frame["low"], frame["close"]

    up_move = high.diff()
    down_move = -low.diff()
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    # NaN (not pd.NA) keeps the dtype float64; pd.NA collapses these to object and breaks ewm.
    alpha = 1 / adx_period
    atr = tr.ewm(alpha=alpha, adjust=False).mean().replace(0.0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    frame["adx"] = dx.ewm(alpha=alpha, adjust=False).mean()


def calculate_indicators(candles: list[list[float]], ema_fast: int, ema_slow: int, adx_period: int) -> dict:
    """Last closed bar's indicators, plus the bar before it.

    `prev_ema_fast` / `prev_ema_slow` exist because the entry trigger is a
    CROSSOVER, which by definition compares two consecutive bars. Returning only
    the current bar makes that impossible to express.
    """
    if len(candles) < max(ema_slow, adx_period * 2):
        return {}
    frame = indicator_frame(candles, ema_fast, ema_slow, adx_period)
    if len(frame) < 2:
        return {}  # a crossover needs a previous bar to compare against
    row = frame.iloc[-1]
    prev = frame.iloc[-2]
    if pd.isna(row[["ema_fast", "ema_slow", "adx"]]).any():
        return {}
    if pd.isna(prev[["ema_fast", "ema_slow"]]).any():
        return {}
    return {
        "ts": int(row["ts"]),
        "close": float(row["close"]),
        "ema_fast": float(row["ema_fast"]),
        "ema_slow": float(row["ema_slow"]),
        "adx": float(row["adx"]),
        "prev_ema_fast": float(prev["ema_fast"]),
        "prev_ema_slow": float(prev["ema_slow"]),
    }
