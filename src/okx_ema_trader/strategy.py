"""The one and only entry decision. Live and backtest both call this.

Deliberately the only place the strategy exists. `backtest.simulate` and
`executor._classify` must never re-implement it: two copies of a strategy is how
a backtest ends up measuring something the bot never trades.

Shape of the decision:

    15m (closed bar) : direction + ADX + deviation  -> which side is ALLOWED
    5m  (closed bar) : EMA20/EMA60 CROSSOVER EVENT  -> when to actually enter

Nothing else. 15m is a state (it stays true for many bars); 5m is an event (it is
true on exactly one bar). That asymmetry is the whole point: without it, every
bar of an established trend re-fires an entry.
"""
from __future__ import annotations

from dataclasses import dataclass

# Why no signal was produced. Kept distinct on purpose: acting on a warm-up gap
# or on "this bar simply had no crossover" as if the trade were over would close
# healthy positions.
REASON_WARMUP = "warmup"                         # not enough history (or no previous 5m bar)
REASON_ADX = "adx_too_low"                       # 15m ADX <= adx_min
REASON_DEVIATION = "deviation_too_large"         # 15m price too far from EMA20
REASON_NO_ENV = "no_trend_env"                   # 15m EMA20 == EMA60, no direction
REASON_NO_CROSS = "no_cross_event"               # normal case: no 5m crossover on this bar


@dataclass(frozen=True)
class Signal:
    side: str
    reason: str


def classify(ind_5m: dict, ind_15m: dict, adx_min: float,
             deviation_max: float = 1.0) -> tuple[Signal | None, str]:
    """Return (signal, reason). `reason` is why there is no signal, else the side.

    `deviation_max` defaults to 1.0 (= 100%, effectively off) so a caller that
    has not been given a threshold still works, but config always supplies the
    real one.
    """
    if not ind_5m or not ind_15m:
        return None, REASON_WARMUP

    # --- 15m environment. A STATE, not an event: no crossover required here.
    adx = ind_15m.get("adx")
    if adx is None or adx <= adx_min:          # strict: ADX must be > adx_min
        return None, REASON_ADX

    fast_15m = ind_15m["ema_fast"]
    slow_15m = ind_15m["ema_slow"]
    if fast_15m > slow_15m:
        env = "long"
    elif fast_15m < slow_15m:
        env = "short"
    else:
        return None, REASON_NO_ENV

    # Deviation gate: abs(close - EMA20) / EMA20, on the 15m bar only.
    # Entry filter only - it says nothing about a position already open.
    if fast_15m > 0:
        deviation = abs(ind_15m["close"] - fast_15m) / fast_15m
        if deviation > deviation_max:
            return None, REASON_DEVIATION

    # --- 5m entry trigger. Must be a crossover that just happened.
    prev_fast = ind_5m.get("prev_ema_fast")
    prev_slow = ind_5m.get("prev_ema_slow")
    if prev_fast is None or prev_slow is None:
        return None, REASON_WARMUP

    fast_5m = ind_5m["ema_fast"]
    slow_5m = ind_5m["ema_slow"]

    if env == "long" and prev_fast <= prev_slow and fast_5m > slow_5m:
        return Signal("long", "15m bullish + 5m golden cross"), "long"
    if env == "short" and prev_fast >= prev_slow and fast_5m < slow_5m:
        return Signal("short", "15m bearish + 5m death cross"), "short"

    # EMA20 sitting above EMA60 for many bars is NOT a signal. Only the flip is.
    return None, REASON_NO_CROSS


def evaluate_signal(ind_5m: dict, ind_15m: dict, adx_min: float,
                    deviation_max: float = 1.0) -> Signal | None:
    signal, _ = classify(ind_5m, ind_15m, adx_min, deviation_max)
    return signal
