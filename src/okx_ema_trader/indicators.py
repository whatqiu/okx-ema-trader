from __future__ import annotations

import numpy as np
import pandas as pd

# The 6 OHLCV columns, in the order `storage.upsert_candles` writes them and in
# the order `backtest` feeds them. Everything downstream indexes by NAME
# (frame["close"]), so this list is the only place column order is decided.
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
    # Refuse to emit a partial frame. A truncated ADX is not "approximately
    # right", it is a different number, and a caller cannot tell the difference
    # from a valid one. Returning empty forces the caller to warm up instead.
    if len(frame) < max(ema_slow, adx_period * 2):
        return empty
    high, low, close = frame["high"], frame["low"], frame["close"]
    # adjust=False makes this the classic recursive EMA seeded with the first
    # close. adjust=True would divide by the window weight, which is a different
    # indicator entirely — backtest/live parity depends on this exact choice.
    frame["ema_fast"] = close.ewm(span=ema_fast, adjust=False).mean()
    frame["ema_slow"] = close.ewm(span=ema_slow, adjust=False).mean()
    _attach_adx(frame, adx_period)
    return frame


def _attach_adx(frame: pd.DataFrame, adx_period: int) -> None:
    """Wilder ADX, in place. Split out so backtest and live paths can't drift.

    ADX measures TREND STRENGTH, never direction. +DI and -DI carry the
    direction; the DX collapse |+DI - -DI| / (+DI + -DI) throws it away, and the
    final ewm averages that collapse into one 0-100 "how much movement" number.

    So "ADX > 20" means "this is trending", never "this is bullish". The
    direction comes from the EMA20/60 ordering on the 15m bar, separately.

    0-25 chop, 25-50 trending, >50 strongly trending (Wilder's own guide).
    """
    high, low, close = frame["high"], frame["low"], frame["close"]

    # True Range: the largest of the three standard candidates. Taking the max
    # rather than any single one is what makes gaps count as range.
    up_move = high.diff()
    down_move = -low.diff()
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    # +DM/-DM use a strict comparison and a > 0 guard. Ties resolve to 0.0 on
    # purpose: when an up and down move are equal there is no directional
    # information, and letting it accumulate inflates the smoothed DI values.
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    # NaN (not pd.NA) keeps the dtype float64; pd.NA collapses these to object and breaks ewm.
    # alpha = 1/period is Wilder's smoothing, NOT an EMA of span=period. Using
    # span=period here is a silent off-by-a-lot that still produces plausible
    # numbers, so it is spelled out rather than derived.
    alpha = 1 / adx_period
    # The replace() calls divide-by-zero guards: a flat market has TR=0 and DX=0,
    # and both must become NaN, not inf. inf would poison every downstream
    # comparison (NaN <= threshold is False, so the filter would silently pass).
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
