"""Auto-trading: drive the paper account with the LIVE strategy's signals.

The one guarantee that matters: the decision comes from
`strategy.evaluate_signal` — the same function the backtest measures and the
live bot would trade. If the backtest says NO EDGE, this auto-trader will
faithfully lose the same way in paper, which is exactly what a simulator is
for.

Timing discipline mirrors `backtest.simulate`:
  * evaluate only on a CONFIRMED 5m close (the forming bar is invisible);
  * the 15m filter uses the last 15m bar CLOSED at that moment;
  * one evaluation per bar — a bar ts is processed once, recorded in meta,
    so a restart or a double poll cannot re-fire yesterday's crossover.

Action rule (user's spec): long signal -> be long, short signal -> be short,
no signal -> do nothing. Opposite positions are closed first; a signal in the
direction already held is a no-op (a STATE is not an EVENT).
"""
from __future__ import annotations

import time

import deps
import market
import paper
from okx_ema_trader.backtest import MINUTES_PER_BAR
from okx_ema_trader.indicators import indicator_frame
from okx_ema_trader.strategy import evaluate_signal

_DEFAULTS = {"enabled": "0", "symbol": "", "notional": "100", "leverage": "5"}


def get_state(store) -> dict:
    return {
        "enabled": store.get_meta("auto_enabled", _DEFAULTS["enabled"]) == "1",
        "symbol": store.get_meta("auto_symbol", _DEFAULTS["symbol"]),
        "notional": float(store.get_meta("auto_notional", _DEFAULTS["notional"])),
        "leverage": float(store.get_meta("auto_leverage", _DEFAULTS["leverage"])),
        "last_bar_ts": int(store.get_meta("auto_last_bar_ts", "0")),
        "last_action": store.get_meta("auto_last_action", ""),
        "last_error": store.get_meta("auto_last_error", ""),
    }


def set_state(store, *, enabled=None, symbol=None, notional=None,
              leverage=None) -> dict:
    if enabled is not None:
        store.set_meta("auto_enabled", "1" if enabled else "0")
    if symbol is not None:
        store.set_meta("auto_symbol", symbol)
    if notional is not None:
        if notional <= 0:
            raise ValueError("自动交易名义价值必须大于 0")
        store.set_meta("auto_notional", str(float(notional)))
    if leverage is not None:
        if not (1 <= leverage <= 20):
            raise ValueError("自动交易杠杆限制在 1–20x（模拟盘也不练坏习惯）")
        store.set_meta("auto_leverage", str(float(leverage)))
    return get_state(store)


def _record(store, key: str, value: str) -> None:
    store.set_meta(key, value)


def maybe_trade(store, inst_id: str, *, signal_fn=None, price_fn=None,
                ticker_fn=None) -> dict:
    """Evaluate the newest confirmed bar once; act on a fresh signal.

    Injectable seams (`signal_fn`, `price_fn`, `ticker_fn`) keep this testable
    offline: the decision path and the accounting are what we pin down, not
    OKX's mood.
    """
    signal_fn = signal_fn or evaluate_signal
    cfg = deps.config()
    bar5 = cfg.bar_5m
    bar15 = cfg.bar_15m

    rows5 = store.candle_rows(inst_id, bar5, 300, only_confirmed=True)
    rows15 = store.candle_rows(inst_id, bar15, 200, only_confirmed=True)
    warmup = max(cfg.ema_slow, cfg.adx_period * 2) + 2
    if len(rows5) < warmup or len(rows15) < warmup // 3:
        return {"acted": False, "why": "warmup"}

    last_ts = int(rows5[-1][0])
    last_done = int(store.get_meta("auto_last_bar_ts", "0"))
    if last_ts <= last_done:
        return {"acted": False, "why": "bar already evaluated", "bar_ts": last_ts}
    _record(store, "auto_last_bar_ts", str(last_ts))

    frame5 = indicator_frame(rows5, cfg.ema_fast, cfg.ema_slow, cfg.adx_period)
    frame15 = indicator_frame(rows15, cfg.ema_fast, cfg.ema_slow, cfg.adx_period)

    # 15m filter: the last 15m bar CLOSED at the 5m close, never the forming
    # one. Same cutoff arithmetic as backtest.simulate.
    cutoff = last_ts + MINUTES_PER_BAR[bar5] * 60_000 - 15 * 60_000
    eligible = [r for r in rows15 if int(r[0]) <= cutoff]
    if len(eligible) < 2:
        return {"acted": False, "why": "no closed 15m bar yet", "bar_ts": last_ts}

    r5 = frame5.iloc[-1]
    p5 = frame5.iloc[-2]
    # Rebuild the 15m frame row for the eligible bar: locate by ts.
    j = len(frame15) - 1
    while j >= 0 and int(frame15["ts"].iloc[j]) > cutoff:
        j -= 1
    if j < 0:
        return {"acted": False, "why": "15m frame lacks the closed bar",
                "bar_ts": last_ts}
    r15 = frame15.iloc[j]

    import math
    if any(math.isnan(float(v)) for v in
           (r5["ema_fast"], r5["ema_slow"], p5["ema_fast"], p5["ema_slow"],
            r15["adx"])):
        return {"acted": False, "why": "indicator warmup", "bar_ts": last_ts}

    signal = signal_fn(
        {"close": float(r5["close"]), "ema_fast": float(r5["ema_fast"]),
         "ema_slow": float(r5["ema_slow"]),
         "prev_ema_fast": float(p5["ema_fast"]),
         "prev_ema_slow": float(p5["ema_slow"])},
        {"close": float(r15["close"]), "ema_fast": float(r15["ema_fast"]),
         "ema_slow": float(r15["ema_slow"]), "adx": float(r15["adx"])},
        cfg.adx_min, cfg.deviation_max,
    )

    if signal is None:
        return {"acted": False, "why": "no signal — 空仓不操作", "bar_ts": last_ts}

    side = signal.side
    price_fn = price_fn or _spread_prices
    state = get_state(store)

    opens = store.open_orders(inst_id)
    same = [o for o in opens if o["side"] == side]
    opposite = [o for o in opens if o["side"] != side]

    closed, opened = [], None
    try:
        # Price fetch is INSIDE the try: a dead proxy or a missing bid/ask
        # must be recorded for the UI, not thrown through the caller.
        prices = price_fn(store, inst_id, ticker_fn)
        for o in opposite:
            exit_price = prices["bid"] if o["side"] == "long" else prices["ask"]
            out = paper.close_position(store, o["id"], exit_price)
            closed.append({"id": o["id"], "pnl": out["pnl"]})
        if not same:
            entry = prices["ask"] if side == "long" else prices["bid"]
            opened = paper.open_position(store, inst_id, side,
                                         state["notional"], state["leverage"],
                                         entry)
        acted = opened is not None or bool(closed)
        store.add_signal(inst_id, side, f"auto: {signal.reason}",
                         price=prices.get("last"), ema_fast=float(r5["ema_fast"]),
                         ema_slow=float(r5["ema_slow"]), adx=float(r15["adx"]),
                         ts=last_ts, acted=acted)
        stamp = time.strftime("%m-%d %H:%M")
        _record(store, "auto_last_action",
                f"{stamp} {side} 信号: 平{len(closed)} 开{1 if opened else 0}")
        _record(store, "auto_last_error", "")
        return {"acted": acted, "signal": side, "reason": signal.reason,
                "closed": closed, "opened": opened, "bar_ts": last_ts}
    except (ValueError, KeyError) as exc:
        # e.g. insufficient margin, or a missing bid/ask — record it, do not
        # kill the loop. The NEXT bar's signal gets another chance.
        store.add_signal(inst_id, side, f"auto FAILED: {exc}",
                         ts=last_ts, acted=False)
        _record(store, "auto_last_error", str(exc))
        return {"acted": False, "signal": side, "error": str(exc),
                "bar_ts": last_ts}


def _spread_prices(store, inst_id: str, ticker_fn=None) -> dict:
    """Real bid/ask for fills — the auto-trader crosses the spread exactly
    like the manual ticket does."""
    if ticker_fn is not None:
        tick = ticker_fn(store, inst_id)
    else:
        tick = market.fetch_ticker(store, inst_id)
    if not tick.get("bid") or not tick.get("ask"):
        raise KeyError(f"{inst_id} 缺少买一/卖一价，无法成交")
    return tick
