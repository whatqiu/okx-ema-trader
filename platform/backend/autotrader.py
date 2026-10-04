"""Auto-trading: drive the paper account with the LIVE strategy's signals.

The one guarantee that matters: the decision comes from
`strategy.evaluate_signal` — the same function the backtest measures and the
live bot would trade. If the backtest says NO EDGE, this auto-trader will
faithfully lose the same way in paper, which is exactly what a simulator is
for.

Timing discipline mirrors `backtest.simulate`:
  * evaluate only on a CONFIRMED 5m close (the forming bar is invisible);
  * the 15m filter uses the last 15m bar CLOSED at that moment;
  * one evaluation per bar PER SYMBOL — a bar ts is processed once, recorded
    in meta, so a restart or a double poll cannot re-fire yesterday's
    crossover. The dedup key carries the instrument, otherwise scanning N
    symbols would let one symbol's bar stamp swallow every other symbol's.

Action rule (user's spec): long signal -> be long, short signal -> be short,
no signal -> do nothing. Opposite positions are closed first; a signal in the
direction already held is a no-op (a STATE is not an EVENT).

Scanning many symbols changes one thing that matters: a signal is now a RARE
event per symbol but the aggregate is not, so capacity has to be a first-class
rule rather than an afterthought. `maybe_trade_all` owns that: how many
positions may be open at once, and whether this pass is allowed to open at
all. Individual symbol failures are contained — one bad instrument must never
take the whole scan down.
"""
from __future__ import annotations

import json
import math
import time

import deps
import market
import paper
from okx_ema_trader.backtest import MINUTES_PER_BAR
from okx_ema_trader.indicators import indicator_frame
from okx_ema_trader.strategy import evaluate_signal

_DEFAULTS = {"enabled": "0", "symbol": "", "notional": "100", "leverage": "5"}

# A watch list is not a portfolio: beyond this the scan spends more on API
# calls and candle backfill than it gains from extra signal opportunities.
MAX_SYMBOLS = 10
DEFAULT_MAX_POSITIONS = 3


def _bar_key(inst_id: str) -> str:
    return f"auto_bar_ts:{inst_id}"


def _scan_key(inst_id: str) -> str:
    return f"auto_scan:{inst_id}"


def _load_symbols(store) -> list[str]:
    """Read the watch list, migrating the old single-symbol setting.

    `auto_symbol` predates scanning. It is still honoured so an existing
    installation keeps trading the one thing it was already trading, instead of
    silently going flat on upgrade.
    """
    raw = store.get_meta("auto_symbols")
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            out = []
            for s in parsed:
                inst = deps.normalise_symbol(str(s))
                if inst and inst not in out:
                    out.append(inst)
            return out[:MAX_SYMBOLS]
    legacy = store.get_meta("auto_symbol", _DEFAULTS["symbol"]) or ""
    # Guarded: `normalise_symbol` rejects an empty string, and "never
    # configured" is a legitimate state that must read back as an empty list
    # rather than raising.
    if not legacy.strip():
        return []
    return [deps.normalise_symbol(legacy)]


def get_state(store) -> dict:
    symbols = _load_symbols(store)
    scan = {}
    for inst in symbols:
        raw = store.get_meta(_scan_key(inst))
        if raw:
            try:
                scan[inst] = json.loads(raw)
            except (TypeError, ValueError):
                pass
    return {
        "enabled": store.get_meta("auto_enabled", _DEFAULTS["enabled"]) == "1",
        # `symbols` is the truth; `symbol` stays for the UI's single-symbol
        # affordances (chart header, manual ticket) and for old clients.
        "symbols": symbols,
        "symbol": symbols[0] if symbols else "",
        "notional": float(store.get_meta("auto_notional", _DEFAULTS["notional"])),
        "leverage": float(store.get_meta("auto_leverage", _DEFAULTS["leverage"])),
        "max_positions": int(store.get_meta("auto_max_positions",
                                            str(DEFAULT_MAX_POSITIONS))),
        "last_action": store.get_meta("auto_last_action", ""),
        "last_error": store.get_meta("auto_last_error", ""),
        # Per-symbol last evaluation, including WHY there was no signal. Without
        # this, an empty `signals` table cannot distinguish "the strategy never
        # triggers" from "the loop never ran".
        "scan": scan,
    }


def set_state(store, *, enabled=None, symbol=None, symbols=None, notional=None,
              leverage=None, max_positions=None) -> dict:
    if enabled is not None:
        store.set_meta("auto_enabled", "1" if enabled else "0")
    if symbols is not None:
        raw = symbols
        if isinstance(raw, str):
            raw = [s for s in raw.replace(",", " ").split() if s]
        cleaned = []
        for s in raw or []:
            inst = deps.normalise_symbol(str(s))
            if inst and inst not in cleaned:
                cleaned.append(inst)
        if len(cleaned) > MAX_SYMBOLS:
            raise ValueError(f"最多监控 {MAX_SYMBOLS} 个币种，收到 {len(cleaned)} 个")
        store.set_meta("auto_symbols", json.dumps(cleaned, ensure_ascii=False))
        # Keep the legacy key pointing at the head of the list so anything
        # still reading it does not see a symbol that is no longer watched.
        store.set_meta("auto_symbol", cleaned[0] if cleaned else "")
    elif symbol is not None:
        # Backwards compatibility: a single symbol replaces the watch list.
        # An empty string clears it rather than raising — "watch nothing" has
        # to be expressible.
        inst = deps.normalise_symbol(symbol) if (symbol or "").strip() else ""
        store.set_meta("auto_symbols", json.dumps([inst] if inst else [],
                                                  ensure_ascii=False))
        store.set_meta("auto_symbol", inst)
    if notional is not None:
        if notional <= 0:
            raise ValueError("自动交易名义价值必须大于 0")
        store.set_meta("auto_notional", str(float(notional)))
    if leverage is not None:
        if not (1 <= leverage <= 20):
            raise ValueError("自动交易杠杆限制在 1–20x（模拟盘也不练坏习惯）")
        store.set_meta("auto_leverage", str(float(leverage)))
    if max_positions is not None:
        if not (1 <= int(max_positions) <= MAX_SYMBOLS):
            raise ValueError(f"最大持仓数必须在 1–{MAX_SYMBOLS} 之间")
        store.set_meta("auto_max_positions", str(int(max_positions)))
    return get_state(store)


def _record(store, key: str, value: str) -> None:
    store.set_meta(key, value)


def _record_scan(store, inst_id: str, **fields) -> None:
    store.set_meta(_scan_key(inst_id),
                   json.dumps(fields, ensure_ascii=False))


def ensure_data(store, inst_id: str) -> bool:
    """Top up candles for one symbol, but ONLY when they are missing or stale.

    Scanning N symbols means N times the temptation to call `ensure_candles`
    every pass, which would quietly undo the bar-boundary polling the rest of
    the platform was tuned for. So the common case is a local extent query and
    no HTTP at all; the network is touched only when the candles are too short
    to compute indicators, or have fallen more than two bars behind.
    """
    cfg = deps.config()
    warmup = max(cfg.ema_slow, cfg.adx_period * 2) + 2
    now = int(time.time() * 1000)
    need = False
    for bar, target in ((cfg.bar_5m, 400), (cfg.bar_15m, 200)):
        _oldest, newest, count = store.candle_extent(inst_id, bar)
        step = market.bar_ms(bar)
        if count < warmup or not count or newest < now - 2 * step:
            market.ensure_candles(store, inst_id, bar, target)
            need = True
    return need


def maybe_trade(store, inst_id: str, *, signal_fn=None, price_fn=None,
                ticker_fn=None, allow_open: bool = True) -> dict:
    """Evaluate the newest confirmed bar once for ONE symbol; act on a signal.

    Injectable seams (`signal_fn`, `price_fn`, `ticker_fn`) keep this testable
    offline: the decision path and the accounting are what we pin down, not
    OKX's mood.

    `allow_open` is the capacity gate owned by `maybe_trade_all`. Closing an
    opposite position is always allowed — reducing risk never needs permission.
    """
    signal_fn = signal_fn or evaluate_signal
    cfg = deps.config()
    bar5 = cfg.bar_5m
    bar15 = cfg.bar_15m

    rows5 = store.candle_rows(inst_id, bar5, 300, only_confirmed=True)
    rows15 = store.candle_rows(inst_id, bar15, 200, only_confirmed=True)
    warmup = max(cfg.ema_slow, cfg.adx_period * 2) + 2
    if len(rows5) < warmup or len(rows15) < warmup // 3:
        return {"acted": False, "why": "warmup", "inst_id": inst_id}

    last_ts = int(rows5[-1][0])
    last_done = int(store.get_meta(_bar_key(inst_id), "0"))
    # Exactly-once evaluation per bar PER SYMBOL. The key carries the
    # instrument: with a shared key, whichever symbol the scan touched first
    # would stamp the bar and every other symbol would be skipped for that bar.
    #
    # Stamped BEFORE the trade, so a crash between the trade and the next poll
    # cannot re-fire the same crossover. That is the trade-off — a crash can
    # skip a bar, but the far worse failure is opening the same position twice.
    if last_ts <= last_done:
        return {"acted": False, "why": "bar already evaluated",
                "bar_ts": last_ts, "inst_id": inst_id}
    _record(store, _bar_key(inst_id), str(last_ts))

    frame5 = indicator_frame(rows5, cfg.ema_fast, cfg.ema_slow, cfg.adx_period)
    frame15 = indicator_frame(rows15, cfg.ema_fast, cfg.ema_slow, cfg.adx_period)

    # 15m filter: the last 15m bar CLOSED at the 5m close, never the forming
    # one. Same cutoff arithmetic as backtest.simulate.
    #
    # The arithmetic, spelled out because this is where look-ahead bugs live:
    #   last_ts  is the OPEN time of the just-closed 5m bar. At 10:15:00 close,
    #            the bar that closed is the one that opened at 10:10:00.
    #   +5m      converts that to the moment the 5m bar actually closed (10:15).
    #   -15m     walks back to 10:00 — the OPEN of the last 15m bar that has
    #            fully closed by 10:15. Any 15m bar opening at 10:00 or later is
    #            still forming and must NOT be read.
    #
    # Getting this wrong by even one bar means trading on a 15m candle that had
    # not finished forming, and the backtest would then be measuring a strategy
    # the live bot never runs.
    cutoff = last_ts + MINUTES_PER_BAR[bar5] * 60_000 - 15 * 60_000
    eligible = [r for r in rows15 if int(r[0]) <= cutoff]
    if len(eligible) < 2:
        _record_scan(store, inst_id, ts=last_ts, side=None, acted=False,
                     why="no closed 15m bar yet")
        return {"acted": False, "why": "no closed 15m bar yet",
                "bar_ts": last_ts, "inst_id": inst_id}

    r5 = frame5.iloc[-1]
    p5 = frame5.iloc[-2]
    # Rebuild the 15m frame row for the eligible bar: locate by ts.
    j = len(frame15) - 1
    while j >= 0 and int(frame15["ts"].iloc[j]) > cutoff:
        j -= 1
    if j < 0:
        return {"acted": False, "why": "15m frame lacks the closed bar",
                "bar_ts": last_ts, "inst_id": inst_id}
    r15 = frame15.iloc[j]

    if any(math.isnan(float(v)) for v in
           (r5["ema_fast"], r5["ema_slow"], p5["ema_fast"], p5["ema_slow"],
            r15["adx"])):
        _record_scan(store, inst_id, ts=last_ts, side=None, acted=False,
                     why="indicator warmup")
        return {"acted": False, "why": "indicator warmup",
                "bar_ts": last_ts, "inst_id": inst_id}

    adx = float(r15["adx"])
    signal = signal_fn(
        {"close": float(r5["close"]), "ema_fast": float(r5["ema_fast"]),
         "ema_slow": float(r5["ema_slow"]),
         "prev_ema_fast": float(p5["ema_fast"]),
         "prev_ema_slow": float(p5["ema_slow"])},
        {"close": float(r15["close"]), "ema_fast": float(r15["ema_fast"]),
         "ema_slow": float(r15["ema_slow"]), "adx": adx},
        cfg.adx_min, cfg.deviation_max,
    )

    if signal is None:
        # Recorded, not swallowed. This is what turns an empty `signals` table
        # from "who knows" into "the strategy looked and said no, N times".
        _record_scan(store, inst_id, ts=last_ts, side=None, acted=False, adx=adx,
                     why="no signal — 空仓不操作")
        return {"acted": False, "why": "no signal — 空仓不操作",
                "bar_ts": last_ts, "inst_id": inst_id, "adx": adx}

    side = signal.side
    price_fn = price_fn or _spread_prices
    state = get_state(store)

    opens = store.open_orders(inst_id)
    same = [o for o in opens if o["side"] == side]
    opposite = [o for o in opens if o["side"] != side]

    # Closing an opposite position always frees the slot it occupies, so it is
    # allowed even at the cap: reducing risk never needs permission, only
    # adding exposure does. Blocking the close here would strand a position the
    # strategy has explicitly said to reverse.
    can_open = allow_open or bool(opposite)
    if not same and not can_open:
        _record_scan(store, inst_id, ts=last_ts, side=side, acted=False, adx=adx,
                     why=f"持仓已达上限 {state['max_positions']}，跳过开仓")
        return {"acted": False, "why": "at position cap", "signal": side,
                "bar_ts": last_ts, "inst_id": inst_id}

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
                         ema_slow=float(r5["ema_slow"]), adx=adx,
                         ts=last_ts, acted=acted)
        _record_scan(store, inst_id, ts=last_ts, side=side, acted=acted, adx=adx,
                     why=signal.reason,
                     opened=bool(opened), closed=len(closed))
        stamp = time.strftime("%m-%d %H:%M")
        _record(store, "auto_last_action",
                f"{stamp} {inst_id} {side} 信号: 平{len(closed)} 开{1 if opened else 0}")
        # Deliberately NOT clearing `auto_last_error` here. It is one global
        # slot, and with a watch list a healthy symbol scanned after a broken
        # one would wipe the only record that the broken one failed. The slot
        # is cleared once per scan pass instead — see `maybe_trade_all`.
        return {"acted": acted, "signal": side, "reason": signal.reason,
                "closed": closed, "opened": opened, "bar_ts": last_ts,
                "inst_id": inst_id}
    except (ValueError, KeyError) as exc:
        # e.g. insufficient margin, or a missing bid/ask — record it, do not
        # kill the loop. The NEXT bar's signal gets another chance.
        store.add_signal(inst_id, side, f"auto FAILED: {exc}",
                         ts=last_ts, acted=False)
        _record_scan(store, inst_id, ts=last_ts, side=side, acted=False, adx=adx,
                     why=f"FAILED: {exc}")
        _record(store, "auto_last_error", str(exc))
        return {"acted": False, "signal": side, "error": str(exc),
                "bar_ts": last_ts, "inst_id": inst_id}


def maybe_trade_all(store, *, signal_fn=None, price_fn=None,
                    ticker_fn=None, top_up: bool = True) -> dict:
    """Scan every watched symbol and act on whichever ones have a signal.

    `top_up=False` skips the candle freshness check, for callers (and tests)
    that have already seeded the store and must not touch the network.

    One symbol's failure is contained on purpose: with a watch list, a single
    instrument that 404s, has no bid/ask, or has thin candles would otherwise
    abort the scan and silently stop trading every OTHER symbol too.
    """
    """Scan every watched symbol and act on whichever ones have a signal.

    One symbol's failure is contained on purpose: with a watch list, a single
    instrument that 404s, has no bid/ask, or has thin candles would otherwise
    abort the scan and silently stop trading every OTHER symbol too.
    """
    state = get_state(store)
    symbols = state["symbols"]
    if not state["enabled"] or not symbols:
        return {"scanned": 0, "results": [], "why": "disabled or no symbols",
                "enabled": state["enabled"], "symbols": symbols}

    # Cleared once per pass, never per symbol: see the note in `maybe_trade`.
    # This way `last_error` means "something in THIS scan failed", and a later
    # healthy symbol cannot erase an earlier broken one.
    _record(store, "auto_last_error", "")

    open_count = len(store.open_orders())
    cap = state["max_positions"]
    results = []
    for inst in symbols:
        # Capacity is re-evaluated per symbol: an earlier symbol in the list
        # may have opened something, so "allowed" can flip mid-scan.
        allow_open = open_count < cap
        try:
            # Self-healing: a symbol added to the watch list five seconds ago
            # has no candles yet, and one whose poller entry was lost has stale
            # ones. Fresh symbols cost nothing here.
            if top_up:
                ensure_data(store, inst)
            res = maybe_trade(store, inst, signal_fn=signal_fn,
                              price_fn=price_fn, ticker_fn=ticker_fn,
                              allow_open=allow_open)
        except Exception as exc:  # noqa: BLE001 - one bad symbol, not the scan
            _record_scan(store, inst, ts=0, side=None, acted=False,
                         why=f"scan error: {type(exc).__name__}: {exc}")
            _record(store, "auto_last_error", f"{inst}: {exc}")
            res = {"acted": False, "error": str(exc), "inst_id": inst}
        if res.get("opened"):
            open_count += 1
        results.append(res)
    acted = [r for r in results if r.get("acted")]
    return {"scanned": len(symbols), "results": results, "acted": len(acted),
            "enabled": True, "symbols": symbols}


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
