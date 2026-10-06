"""FastAPI backend for the trading platform.

Everything the UI shows comes from the local SQLite store; OKX is contacted
only to fill gaps (see market.py for why). One process-wide Store and one
MarketPoller are created at startup and shared by all routes — two SQLite
handles per request or two pollers would each be a way to disagree with
ourselves about what "the data" is.

Run from this directory:
    uvicorn main:app --host 127.0.0.1 --port 8788
"""
from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import deps
import market
import paper
import autotrader
import health
from okx_ema_trader.backtest import MINUTES_PER_BAR, simulate
from okx_ema_trader.jsonio import json_safe as _json_safe
from okx_ema_trader.symbols import SymbolError
from okx_ema_trader.http import OkxError
from okx_ema_trader.indicators import indicator_frame

_poller: market.MarketPoller | None = None
_auto_stop = threading.Event()
_janitor_stop = threading.Event()
_prune_lock = threading.Lock()
JANITOR_INTERVAL_S = 6 * 3600

# Liveness + circuit breaker. One monitor per loop so a dead poller cannot hide
# behind a healthy trader, and one shared risk report for the UI.
_market_health = health.HealthMonitor()
_auto_health = health.HealthMonitor()
_risk_lock = threading.Lock()          # only one thread may close a position
_last_risk: dict = {}                  # most recent enforce_risk() report

RISK_LIMITS = health.RiskLimits(
    enabled=True,
    # 60% of the position's own margin. At 5x that is a ~12% adverse move, well
    # before the 20% liq distance; a tighter cap would turn ordinary noise into
    # forced exits, which is how a strategy loses money on its own brake.
    max_loss_pct=60.0,
    # 25% of the account. A day that erases a quarter of the balance means
    # something is structurally wrong, not that the market is hard.
    max_daily_loss_pct=25.0,
    # Five losses in a row: stop letting the strategy open new risk.
    max_consecutive_losses=5,
    # Exit when the mark is within 3% of the liq price. At 5x that is well
    # outside the 20% liq distance, so this is a brake, not a coin flip.
    liq_buffer_pct=3.0,
)


def _prune_safe() -> dict | None:
    """Run a retention sweep, never raising into a background loop."""
    with _prune_lock:
        try:
            return deps.store().prune()
        except Exception:
            return None


def _janitor_loop() -> None:
    """Every 6h: prune old rows and reclaim disk.

    SQLite's DELETE does not return space to the OS and the WAL sidecar
    grows until checkpointed — without a janitor the data dir inflates
    forever even though the row counts look small.
    """
    while not _janitor_stop.wait(JANITOR_INTERVAL_S):
        _prune_safe()


def _run_risk_check(store) -> dict:
    """Enforce the circuit breaker. Runs on EVERY auto-loop pass.

    Order matters: the breaker goes first, before the strategy gets to open new
    risk. A strategy that opens first and stops second spends one more bar
    exposed on every loop that needs rescuing.

    The lock is what makes this safe to call from both the background loop and
    the manual /api/risk/check route: two threads closing the same order would
    race on the balance check and one would raise "position does not exist".
    """
    global _last_risk
    with _risk_lock:
        report = health.enforce_risk(store, limits=RISK_LIMITS,
                                     marks=_marks(store),
                                     exit_prices=_exit_prices(store))
        _last_risk = report
    return report


def _auto_pass() -> None:
    """One iteration of the auto loop, split out so a test can run exactly one.

    The ORDER IS THE FIX. The brake used to sit after the `continue` taken when
    auto-trading was off or the watch list was empty — the two states where it
    matters most: a position opened by hand from /api/trade/open, or one left
    running overnight with the switch off, had NO circuit breaker at all while
    the badge stayed green. The comment there claimed otherwise; the code now
    matches the comment.
    """
    try:
        store = deps.store()
        # 1. The brake, first and unconditionally. Its own try because step 2
        #    can fail on its own: a corrupt `auto_symbols` row must not cost the
        #    account its only protection. Fails CLOSED — a pass that could not
        #    run the brake must not let the strategy add exposure. The next pass
        #    retries in 15s.
        try:
            _run_risk_check(store)
        except Exception as exc:
            _auto_health.fail(f"风控检查失败: {type(exc).__name__}: {exc}")
            return
        # 2. The strategy. Reading the watch list is a separate concern and a
        #    separate failure, deliberately — see above.
        state = autotrader.get_state(store)
        symbols = state["symbols"]
        if not state["enabled"] or not symbols:
            # Nothing to trade, so this pass dialled out to nobody. Record
            # the loop as alive but do NOT claim a successful contact:
            # `idle()` leaves `last_ok_at` alone so a real outage still
            # shows up in the badge while auto-trading is off.
            _auto_health.idle()
            return
        autotrader.maybe_trade_all(store)
        _auto_health.ok()
    except Exception as exc:
        # Recorded, not swallowed. The previous `except Exception: continue`
        # kept the loop alive but erased the reason, which made an outage
        # indistinguishable from a quiet market.
        _auto_health.fail(f"{type(exc).__name__}: {exc}")


def _auto_loop() -> None:
    """Every 15s: enforce risk, then scan every watched symbol for a signal.

    The loop never dies: a proxy outage or a bad tick must not end auto-trading
    for the session. But it is no longer silent — every pass records success or
    the reason it failed, so a three-hour outage shows up in the UI instead of
    looking like an idle market.
    """
    while not _auto_stop.wait(15.0):
        _auto_pass()


def _watchdog_loop() -> None:
    """Every 5s: prove OKX is still reachable, and mirror that into the ticker.

    The poller already retries, but it only touches watched symbols; if the
    watch list is empty (nobody opened the chart) it would report "healthy" all
    day while the network was down. This asks OKX directly.
    """
    while not _auto_stop.wait(5.0):
        try:
            deps.okx_get("/public/time")
            _market_health.ok()
        except Exception as exc:
            _market_health.fail(f"{type(exc).__name__}: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poller
    _poller = market.MarketPoller(deps.store(), interval=5.0)
    _poller.start()
    # Re-subscribe on boot. The watch list lives in the database; the poller's
    # watch set does not. Without this a restart resurrects auto-trading with a
    # watch list nobody is refreshing, and the scan quietly trades on candles
    # that stop moving.
    _boot_cfg = deps.config()
    for _inst in autotrader.get_state(deps.store())["symbols"]:
        _poller.watch(_inst, _boot_cfg.bar_5m)
        _poller.watch(_inst, _boot_cfg.bar_15m)
    _auto_stop.clear()
    _janitor_stop.clear()
    auto_thread = threading.Thread(target=_auto_loop, daemon=True,
                                   name="auto-trader")
    auto_thread.start()
    watchdog = threading.Thread(target=_watchdog_loop, daemon=True,
                                name="okx-watchdog")
    watchdog.start()
    janitor_thread = threading.Thread(target=_janitor_loop, daemon=True,
                                      name="db-janitor")
    janitor_thread.start()
    _prune_safe()  # startup sweep: a laptop that was off for days has stale rows
    yield
    _auto_stop.set()
    _janitor_stop.set()
    _poller.stop()


app = FastAPI(title="okx-ema-trader platform", lifespan=lifespan)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _symbol(raw: str | None) -> str:
    """Normalise or 400 — never let a typo'd symbol become an OKX round-trip.

    `normalise_symbol` signals a bad symbol by RAISING, never by returning "".
    The `if not symbol` guard that used to sit here was therefore dead code: the
    exception escaped and a typo came back as a 500 with a traceback. Catch it
    and answer 400, which is what a malformed query parameter actually is.
    """
    try:
        return deps.normalise_symbol(raw or "")
    except SymbolError as exc:
        raise HTTPException(400, str(exc)) from exc


def _bar(raw: str | None) -> str:
    bar = (raw or "5m").strip()
    try:
        market.bar_ms(bar)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return bar


def _okx(call):
    """Translate the three OkxError kinds into three different HTTP answers,
    because 'wrong symbol' and 'proxy dead' must not look identical to the UI."""
    try:
        return call()
    except OkxError as exc:
        status = {"network": 502, "http": 502, "api": 400}.get(exc.kind, 502)
        raise HTTPException(status, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _watch(symbol: str, bar: str) -> None:
    if _poller is not None:
        _poller.watch(symbol, bar)


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------
@app.get("/api/health")
def health_liveness():
    """Liveness only: "this process is serving". Never reflects upstream state.

    Named `_liveness` rather than `health` on purpose: a module-level
    `def health()` shadows the `health` MODULE for the whole file, and the
    failure mode is an AttributeError on `health.enforce_risk` that no offline
    test can see because they import `health` directly, never `main`.

    desktop.py uses a 200 here to decide whether an instance already exists.
    If this endpoint turned red on a proxy outage, closing the window would make
    it spawn a second server on top of a healthy one. Upstream connectivity
    lives at /api/status.
    """
    return {
        "ok": True,
        "time": int(time.time() * 1000),
        "poller": {
            "last_error": _poller.last_error if _poller else None,
            "last_poll_at": _poller.last_poll_at if _poller else 0,
        },
    }


@app.get("/api/candles")
def candles(symbol: str = Query(...), bar: str = Query("5m"),
            limit: int = Query(500, ge=1, le=5000),
            sync: bool = Query(True)):
    """Local-first candles, oldest first.

    `sync=true` (default) fills gaps from OKX before reading; `sync=false`
    serves exactly what is on disk — useful for checking what the poller has
    actually fetched without triggering more traffic.
    """
    inst = _symbol(symbol)
    tf = _bar(bar)
    store = deps.store()
    extent = None
    if sync:
        extent = _okx(lambda: market.ensure_candles(store, inst, tf,
                                                    target_bars=max(limit, 500)))
        _watch(inst, tf)
    rows = store.candles(inst, tf, limit=limit)
    return _json_safe({
        "symbol": inst, "bar": tf, "count": len(rows), "extent": extent,
        "rows": [[r["ts"], r["open"], r["high"], r["low"], r["close"],
                  r["vol"], r["confirm"]] for r in rows],
    })


@app.get("/api/ticker")
def ticker(symbol: str = Query(...)):
    inst = _symbol(symbol)
    store = deps.store()
    _watch(inst, "5m")
    # The browser polls this every 3s; if each of those became an OKX call +
    # DB write, the frontend alone would undo the poller's throttling. Serve
    # the poller's memory-hot cache while it is fresh (<15s), and only touch
    # OKX when the cache is cold (first load, or the poller is stuck).
    if _poller is not None:
        cached = _poller.latest_ticker(inst)
        if cached and cached.get("ts"):
            age_ms = int(time.time() * 1000) - int(cached["ts"])
            if age_ms < 15_000:
                return _json_safe(cached)
    tick = _okx(lambda: market.fetch_ticker(store, inst))
    return _json_safe(tick)


@app.get("/api/instruments")
def instruments(q: str = "", limit: int = Query(30, ge=1, le=100)):
    """Symbol search. Empty q = top USDT swaps by 24h volume (quick picks).

    Falls back to locally-known symbols when OKX is unreachable — a dead
    proxy should degrade the list, not blank it.
    """
    store = deps.store()
    try:
        rows = market.swap_tickers()
        source = "okx"
    except OkxError:
        rows = [{"inst_id": s, "base": s.split("-")[0], "last": None,
                 "vol_ccy24h": 0.0, "change24h": None}
                for s in store.known_symbols()]
        source = "local"
    query = q.strip().upper().replace("-USDT-SWAP", "")
    if query:
        rows = [r for r in rows
                if query in r["base"] or query in r["inst_id"]]
        # Prefix matches rank above substring matches: typing SOL should put
        # SOL-USDT-SWAP above every token that merely contains those letters.
        rows.sort(key=lambda r: (not r["base"].startswith(query),
                                 -r["vol_ccy24h"]))
    return _json_safe({"source": source, "rows": rows[:limit]})


# --------------------------------------------------------------------------
# Trading records (local store only)
# --------------------------------------------------------------------------
@app.get("/api/orders")
def orders(symbol: str | None = None, status: str | None = None,
           limit: int = Query(100, ge=1, le=1000)):
    inst = _symbol(symbol) if symbol else None
    if status not in (None, "open", "closed"):
        raise HTTPException(400, "status 只能是 open / closed")
    rows = deps.store().orders(limit=limit, inst_id=inst, status=status)
    return _json_safe({"rows": rows})


@app.get("/api/signals")
def signals(symbol: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    inst = _symbol(symbol) if symbol else None
    return _json_safe({"rows": deps.store().signals(limit=limit, inst_id=inst)})


@app.get("/api/equity")
def equity(limit: int = Query(500, ge=1, le=5000)):
    return _json_safe({"rows": deps.store().equity_series(limit=limit)})


@app.get("/api/stats")
def stats():
    return _json_safe(deps.store().stats())


@app.post("/api/admin/prune")
def admin_prune():
    """Manual retention sweep: old candles/signals/orders out, disk reclaimed."""
    before = deps.store().file_sizes()
    deleted = _prune_safe()
    after = deps.store().file_sizes()
    if deleted is None:
        raise HTTPException(500, "清理执行失败（数据库可能被占用），稍后再试")
    return _json_safe({"deleted": deleted, "before": before, "after": after})


# --------------------------------------------------------------------------
# Auto-trading — the live strategy driving the paper account
# --------------------------------------------------------------------------
class AutoTradePatch(BaseModel):
    enabled: bool | None = None
    symbol: str | None = None
    # The watch list. A single `symbol` is still accepted and replaces it, so an
    # older client keeps working — it just trades one thing.
    symbols: list[str] | None = None
    notional: float | None = Field(None, gt=0)
    leverage: float | None = Field(None, ge=1.0, le=20.0)
    max_positions: int | None = Field(None, ge=1, le=autotrader.MAX_SYMBOLS)


@app.get("/api/autotrade")
def autotrade_state():
    return _json_safe(autotrader.get_state(deps.store()))


@app.post("/api/autotrade")
def autotrade_set(patch: AutoTradePatch):
    store = deps.store()
    fields = patch.model_dump(exclude_none=True)
    if "symbol" in fields:
        fields["symbol"] = _symbol(fields["symbol"])
    if "symbols" in fields:
        fields["symbols"] = [_symbol(s) for s in fields["symbols"] or []]
    try:
        state = autotrader.set_state(store, **fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Turning it on must not wait 15s for the first evaluation: scan now.
    if patch.enabled and state["symbols"]:
        cfg = deps.config()
        for inst in state["symbols"]:
            # Backfill once, then let the poller keep it fresh at bar
            # boundaries. Watching is what stops the scan from re-fetching
            # history every 15 seconds for every symbol.
            _okx(lambda i=inst: market.ensure_candles(store, i, cfg.bar_5m, 400))
            _okx(lambda i=inst: market.ensure_candles(store, i, cfg.bar_15m, 200))
            _watch(inst, cfg.bar_5m)
            _watch(inst, cfg.bar_15m)
        autotrader.maybe_trade_all(store)
        state = autotrader.get_state(store)
    return _json_safe(state)


# --------------------------------------------------------------------------
# Paper trading — fills at REAL bid/ask, taker fee on both sides
# --------------------------------------------------------------------------
def _marks(store) -> dict[str, float]:
    """inst_id -> last price for every symbol with an open position.

    Reads the poller's memory-hot ticker cache first (5s fresh), then the
    persisted tick (up to 60s old), and only hits OKX when neither exists —
    a position you cannot price is worse than a slow request.
    """
    marks = {}
    for o in store.open_orders():
        inst = o["inst_id"]
        if inst in marks:
            continue
        tick = _poller.latest_ticker(inst) if _poller else store.tick(inst)
        if tick and tick.get("last"):
            marks[inst] = tick["last"]
        else:
            try:
                marks[inst] = market.fetch_ticker(store, inst)["last"]
            except OkxError:
                pass  # entry-price fallback inside account_summary
    return marks


def _exit_prices(store) -> dict[str, tuple]:
    """inst_id -> (bid, ask) for every symbol with an open position.

    Same lookup order as `_marks` (hot cache -> persisted tick -> OKX), but keeps
    both sides so a risk close can cross the spread instead of filling at the
    mid. Without this, an emergency stop would realise a worse price than the
    one the stop was computed from.
    """
    prices: dict[str, tuple] = {}
    for o in store.open_orders():
        inst = o["inst_id"]
        if inst in prices:
            continue
        tick = _poller.latest_ticker(inst) if _poller else store.tick(inst)
        if not tick or not (tick.get("bid") and tick.get("ask")):
            try:
                tick = market.fetch_ticker(store, inst)
            except OkxError:
                tick = None
        prices[inst] = ((tick or {}).get("bid"), (tick or {}).get("ask"))
    return prices


@app.get("/api/status")
def health_status():
    """Is the system actually alive? Two independent monitors plus the brake.

    `connected` = the watchdog proves OKX answers AND the auto loop is not in
    error. It is deliberately NOT `market.connected and auto.connected`: the
    auto loop goes idle (no request at all) whenever auto-trading is off, and an
    idle loop has no `last_ok_at` to offer. Using `loop_alive` there keeps the
    light green when the switch is simply off, and red when the loop is broken.

    Deliberately NOT /api/health: that endpoint means "this process is serving"
    and is what desktop.py probes for a live port. Folding upstream connectivity
    in would make the desktop shell relaunch a perfectly healthy server just
    because the proxy is down.
    """
    store = deps.store()
    market_h = _market_health.snapshot()
    auto_h = _auto_health.snapshot()
    connected = bool(market_h["connected"] and auto_h["loop_alive"])
    halt = health.risk_halt_state(store)
    return _json_safe({
        "connected": connected,
        "market": market_h,
        "auto": auto_h,
        "risk": {
            "limits": {
                "enabled": RISK_LIMITS.enabled,
                "max_loss_pct": RISK_LIMITS.max_loss_pct,
                "max_daily_loss_pct": RISK_LIMITS.max_daily_loss_pct,
                "max_consecutive_losses": RISK_LIMITS.max_consecutive_losses,
                "liq_buffer_pct": RISK_LIMITS.liq_buffer_pct,
            },
            "last": _last_risk,
            # The streak the brake actually judges (counted from the last manual
            # reset), not the raw one — see health.losing_streak.
            "losing_streak": health.losing_streak(store),
            # Latched "stop opening". Sits beside `last` rather than inside it
            # because it is true ACROSS passes, not the result of one pass.
            "halted": halt["halted"],
            "halt_reason": halt["reason"],
            "halt_at": halt["at"],
        },
    })


@app.post("/api/risk/check")
def risk_check():
    """Run the circuit breaker now, on demand. Same path the loop uses."""
    report = _run_risk_check(deps.store())
    return _json_safe(report)


@app.post("/api/risk/reset")
def risk_reset():
    """Lift a latched halt by hand.

    Without this the halt is a lockout: the brake trips once, silently refuses
    every future entry, and the user is left asking why the bot "broke". Clearing
    is a deliberate human act and it re-bases the losing-streak counter — see
    `health.clear_risk_halt` for why that re-base is not optional.
    """
    global _last_risk
    store = deps.store()
    state = health.clear_risk_halt(store)
    # The cached report is what the badge and the banner render. Leaving
    # `halted: true` in it would keep the UI accusing a brake that was just
    # cleared, until the next pass overwrote it.
    with _risk_lock:
        _last_risk = {**(_last_risk or {}), "halted": state["halted"],
                      "halt_reason": state["reason"], "halt_at": state["at"]}
    return _json_safe({"ok": True, **state})


@app.get("/api/account")
def account():
    store = deps.store()
    return _json_safe(paper.account_summary(store, _marks(store),
                                             _exit_prices(store)))


class TradeOpenRequest(BaseModel):
    symbol: str
    side: str                       # "long" | "short"
    notional: float = Field(..., gt=0)
    leverage: float = Field(1.0, ge=1.0, le=125.0)


@app.post("/api/trade/open")
def trade_open(req: TradeOpenRequest):
    inst = _symbol(req.symbol)
    store = deps.store()
    tick = _okx(lambda: market.fetch_ticker(store, inst))
    # Cross the spread like a real market order: long pays ask, short hits bid.
    side = req.side.lower()
    price = tick["ask"] if side == "long" else tick["bid"]
    if not price:
        raise HTTPException(502, f"{inst} 没有可用的{'卖一' if side == 'long' else '买一'}价")
    try:
        result = paper.open_position(store, inst, side, req.notional,
                                     req.leverage, price)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _watch(inst, "5m")
    return _json_safe(result)


class TradeCloseRequest(BaseModel):
    order_id: int


@app.post("/api/trade/close")
def trade_close(req: TradeCloseRequest):
    store = deps.store()
    order = next((o for o in store.open_orders() if o["id"] == req.order_id), None)
    if order is None:
        raise HTTPException(404, f"持仓 #{req.order_id} 不存在或已平仓")
    tick = _okx(lambda: market.fetch_ticker(store, order["inst_id"]))
    # Exit crosses the spread the other way: long sells at bid, short buys at ask.
    price = tick["bid"] if order["side"] == "long" else tick["ask"]
    if not price:
        raise HTTPException(502, "没有可用的平仓价格")
    try:
        result = paper.close_position(store, req.order_id, price)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _json_safe(result)


class AccountResetRequest(BaseModel):
    balance: float = Field(paper.DEFAULT_BALANCE, gt=0)


@app.post("/api/account/reset")
def account_reset(req: AccountResetRequest):
    store = deps.store()
    paper.reset_account(store, req.balance)
    # A fresh balance must get a fresh drawdown baseline. Keeping the old one
    # would open the new account already deep in a "drawdown" against money
    # that never existed, and the brake would latch on the first pass.
    health.clear_risk_baseline(store)
    # Same reasoning as the baseline: the losing streak that sank the old
    # account is not something the new one did.
    health.clear_risk_halt(store)
    return {"ok": True, "balance": req.balance}


# --------------------------------------------------------------------------
# Backtest — run against STORED candles, never re-download what we have
# --------------------------------------------------------------------------
class BacktestRequest(BaseModel):
    symbol: str
    days: int = Field(30, ge=1, le=365)
    fee_bps: float = Field(5.0, ge=0.0)
    slippage_bps: float = Field(3.0, ge=0.0)


@app.post("/api/backtest")
def run_backtest(req: BacktestRequest):
    inst = _symbol(req.symbol)
    store = deps.store()
    config = deps.config()
    step_5m = MINUTES_PER_BAR[config.bar_5m]
    bars_needed = req.days * (24 * 60 // step_5m)

    _okx(lambda: market.ensure_candles(store, inst, config.bar_5m, bars_needed))
    _okx(lambda: market.ensure_candles(store, inst, config.bar_15m,
                                       bars_needed // 3 + 400))
    _watch(inst, config.bar_5m)

    # Decisions must only see confirmed bars — the forming bar is in the store
    # for the chart, not for the strategy.
    rows_5m = store.candle_rows(inst, config.bar_5m, bars_needed,
                                only_confirmed=True)
    rows_15m = store.candle_rows(inst, config.bar_15m, bars_needed // 3 + 400,
                                 only_confirmed=True)
    if len(rows_5m) < max(config.ema_slow, config.adx_period * 2):
        raise HTTPException(400, f"本地K线不足以预热指标（{len(rows_5m)} 根），"
                                 "请调大 days 或先让行情同步跑一会儿")

    frame_5m = indicator_frame(rows_5m, config.ema_fast, config.ema_slow,
                               config.adx_period)
    frame_15m = indicator_frame(rows_15m, config.ema_fast, config.ema_slow,
                                config.adx_period)
    report = simulate(frame_5m, frame_15m, config.adx_min, config.deviation_max,
                      req.fee_bps + req.slippage_bps, step_5m * 60_000,
                      config.trading.stop_loss_pct)

    trading = config.trading
    exposure = (trading.equity_pct / 100.0 * trading.leverage
                if trading.sizing_mode == "equity" else 1.0)
    wins = sum(1 for t in report.trades if t.gross_return > 0)
    # report.equity has a point per fill EVENT (entries too), so it cannot be
    # zipped with trades. Rebuild the curve per round trip: each trade pays
    # cost on entry and exit (2 units of notional, matching turnover accounting).
    cost_per_unit = (req.fee_bps + req.slippage_bps) / 10_000.0
    equity_curve = []
    cumulative = 0.0
    for t in report.trades:
        cumulative += t.gross_return - 2 * cost_per_unit
        equity_curve.append({"ts": t.exit_exec_ts, "equity": cumulative})
    result = {
        "symbol": inst,
        "days": req.days,
        "bars_5m": len(rows_5m),
        "trades": len(report.trades),
        "win_rate": wins / len(report.trades) if report.trades else None,
        "gross_return": report.gross_return,
        "net_return": report.net_return,
        "turnover": report.total_turnover,
        "cost_bps": req.fee_bps + req.slippage_bps,
        "breakeven_bps": report.breakeven_bps,
        "stopped_out": report.stopped_out,
        "unfilled_signals": report.unfilled_signals,
        "max_drawdown_1x": report.max_drawdown(1.0),
        "max_drawdown_account": report.max_drawdown(exposure),
        "wiped_out": report.wiped_out(exposure),
        "ruin_exposure": report.ruin_exposure(),
        "exposure": exposure,
        "verdict": "edge survives realistic costs" if report.net_return > 0
                   else "NO EDGE — costs eat everything",
        "equity_curve": equity_curve,
        "trade_list": [
            {"side": t.side, "entry_price": t.entry_price,
             "exit_price": t.exit_price, "gross_return": t.gross_return,
             "entry_exec_ts": t.entry_exec_ts, "exit_exec_ts": t.exit_exec_ts,
             "exit_reason": t.exit_reason}
            for t in report.trades
        ],
    }
    run_id = store.save_backtest(inst, req.model_dump(), result)
    return _json_safe({"id": run_id, **result})


@app.get("/api/backtests")
def backtests(symbol: str | None = None, limit: int = Query(30, ge=1, le=200)):
    inst = deps.normalise_symbol(symbol) if symbol else None
    rows = deps.store().backtests(limit=limit, inst_id=inst)
    # The list view does not need full trade lists — keep the payload small.
    for row in rows:
        row["result"] = {k: v for k, v in row["result"].items()
                         if k not in ("equity_curve", "trade_list")}
    return _json_safe({"rows": rows})


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
class SettingsPatch(BaseModel):
    proxy: str | None = None
    symbol: str | None = None


@app.get("/api/settings")
def get_settings():
    settings = deps.load_settings()
    settings["resolved_proxy"] = deps.proxy()
    return settings


@app.post("/api/settings")
def post_settings(patch: SettingsPatch):
    return deps.save_settings(patch.model_dump(exclude_none=True))


# --------------------------------------------------------------------------
# Frontend static files (production build). Mounted LAST so /api wins.
# StaticFiles itself guards against ../ path traversal.
# --------------------------------------------------------------------------
if deps.FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(deps.FRONTEND_DIST), html=True),
              name="frontend")
else:
    @app.get("/")
    def no_frontend():
        return JSONResponse({
            "detail": "前端尚未构建。开发模式：cd platform/frontend && npm run dev；"
                      "生产模式：npm run build 后由本服务直接托管。",
            "docs": "/docs",
        }, status_code=503)
