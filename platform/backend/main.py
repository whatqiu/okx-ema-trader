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
from okx_ema_trader.backtest import MINUTES_PER_BAR, simulate
from okx_ema_trader.console_server import _json_safe
from okx_ema_trader.http import OkxError
from okx_ema_trader.indicators import indicator_frame

_poller: market.MarketPoller | None = None
_auto_stop = threading.Event()
_janitor_stop = threading.Event()
_prune_lock = threading.Lock()
JANITOR_INTERVAL_S = 6 * 3600


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


def _auto_loop() -> None:
    """Every 15s: if auto-trading is on, evaluate the newest confirmed bar.

    The loop never raises: a proxy outage or a bad tick must not kill
    auto-trading for the rest of the session — the next pass may succeed.
    """
    while not _auto_stop.wait(15.0):
        try:
            store = deps.store()
            state = autotrader.get_state(store)
            if not state["enabled"] or not state["symbol"]:
                continue
            inst = deps.normalise_symbol(state["symbol"])
            if not inst:
                continue
            cfg = deps.config()
            market.ensure_candles(store, inst, cfg.bar_5m, 400)
            market.ensure_candles(store, inst, cfg.bar_15m, 200)
            autotrader.maybe_trade(store, inst)
        except Exception:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poller
    _poller = market.MarketPoller(deps.store(), interval=5.0)
    _poller.start()
    _auto_stop.clear()
    _janitor_stop.clear()
    auto_thread = threading.Thread(target=_auto_loop, daemon=True,
                                   name="auto-trader")
    auto_thread.start()
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
    """Normalise or 400 — never let a typo'd symbol become an OKX round-trip."""
    symbol = deps.normalise_symbol(raw or "")
    if not symbol:
        raise HTTPException(400, f"无法识别的交易对：{raw!r}（示例：MU 或 MU-USDT-SWAP）")
    return symbol


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
def health():
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
    inst = deps.normalise_symbol(symbol) if symbol else None
    if status not in (None, "open", "closed"):
        raise HTTPException(400, "status 只能是 open / closed")
    rows = deps.store().orders(limit=limit, inst_id=inst, status=status)
    return _json_safe({"rows": rows})


@app.get("/api/signals")
def signals(symbol: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    inst = deps.normalise_symbol(symbol) if symbol else None
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
    notional: float | None = Field(None, gt=0)
    leverage: float | None = Field(None, ge=1.0, le=20.0)


@app.get("/api/autotrade")
def autotrade_state():
    return _json_safe(autotrader.get_state(deps.store()))


@app.post("/api/autotrade")
def autotrade_set(patch: AutoTradePatch):
    store = deps.store()
    fields = patch.model_dump(exclude_none=True)
    if "symbol" in fields:
        fields["symbol"] = _symbol(fields["symbol"])
    try:
        state = autotrader.set_state(store, **fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Turning it on must not wait 15s for the first evaluation: trade now.
    if patch.enabled and state["symbol"]:
        inst = deps.normalise_symbol(state["symbol"])
        cfg = deps.config()
        _okx(lambda: market.ensure_candles(store, inst, cfg.bar_5m, 400))
        _okx(lambda: market.ensure_candles(store, inst, cfg.bar_15m, 200))
        _watch(inst, cfg.bar_5m)
        autotrader.maybe_trade(store, inst)
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


@app.get("/api/account")
def account():
    store = deps.store()
    return _json_safe(paper.account_summary(store, _marks(store)))


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
    paper.reset_account(deps.store(), req.balance)
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
