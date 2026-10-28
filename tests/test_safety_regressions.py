"""Regression tests for accounting, account brakes, and multi-symbol execution."""
from dataclasses import replace
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "platform" / "backend"))

import health
import autotrader
import main as api_main
import paper
from okx_ema_trader import executor as executor_module
from okx_ema_trader.broker import Broker, BrokerError
from okx_ema_trader.config import load_config
from okx_ema_trader.executor import Executor
from okx_ema_trader.state import Ledger, Position
from okx_ema_trader.storage import Store
from test_exit_rules import FakeBroker, _Pinned
from test_platform_autotrader_multi import seed

SYMBOL = "BTC-USDT-SWAP"
OTHER = "AAVE-USDT-SWAP"


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "account.db")
    yield result
    result.close()


def test_lifetime_balance_survives_pruning_restart_and_reset(store):
    with store._connect() as conn:
        conn.executemany(
            "INSERT INTO orders(inst_id,side,status,pnl,created_at) VALUES(?,?,?,?,?)",
            [(SYMBOL, "long", "closed", 1.0, i) for i in range(1001)])
    assert paper.account_summary(store)["balance"] == 11001
    store.prune(keep_orders=3)
    assert paper.account_summary(store)["balance"] == 11001
    store.prune(keep_orders=0)
    assert paper.account_summary(store)["balance"] == 11001
    store.prune(keep_orders=0)
    store.close()
    assert paper.account_summary(store)["balance"] == 11001
    paper.reset_account(store, 5000)
    assert paper.account_summary(store)["balance"] == 5000


def test_account_drawdown_closes_healthy_and_losing_positions(store):
    paper.reset_account(store, 1000)
    health.risk_baseline(store, 1000)
    paper.open_position(store, SYMBOL, "long", 500, 5, 100)
    paper.open_position(store, OTHER, "long", 100, 5, 100)
    report = health.enforce_risk(
        store, limits=health.RiskLimits(max_drawdown_pct=25),
        marks={SYMBOL: 40, OTHER: 100},
        exit_prices={SYMBOL: (40, 41), OTHER: (100, 101)})
    assert report["halted"]
    assert len(report["closed"]) == 2
    assert not store.open_orders()


@pytest.mark.parametrize("side,mark", [("long", 98), ("short", 102)])
def test_paper_price_stop_uses_configured_distance(store, side, mark):
    paper.open_position(store, SYMBOL, side, 100, 5, 100, stop_loss_pct=2)
    report = health.enforce_risk(
        store, limits=health.RiskLimits(), marks={SYMBOL: mark},
        exit_prices={SYMBOL: (mark, mark)})
    assert len(report["closed"]) == 1
    assert not store.open_orders()


def make_executor(tmp_path, broker, symbols=(SYMBOL,)):
    cfg = load_config(ROOT / "config" / "config.yaml")
    cfg = replace(cfg, trading=replace(cfg.trading, symbols=symbols,
                                      exit_mode="fixed", stop_loss_pct=2.0))
    ledger = Ledger(path=tmp_path / "state.yaml")
    return Executor(cfg, broker, ledger)


def test_remote_stop_is_counted_once_through_the_full_poll(tmp_path):
    broker = FakeBroker(price=98)
    ex = make_executor(tmp_path, broker)
    ex.ledger.set(SYMBOL, Position(side="long", entry_price=100, size=1, stop_price=98))
    with _Pinned("long", 1000):
        ex.poll()
        ex.poll()
    assert not ex.ledger.get(SYMBOL).is_open
    assert ex.ledger.risk.consecutive_losses == 1
    assert not broker.entered


def test_halt_during_first_reversal_blocks_later_symbols(tmp_path):
    broker = FakeBroker(price=98)
    ex = make_executor(tmp_path, broker, (SYMBOL, OTHER))
    ex.guard.limits = replace(ex.guard.limits, max_consecutive_losses=1)
    ex.ledger.set(SYMBOL, Position(side="long", entry_price=100, size=1, stop_price=98))
    broker.live[SYMBOL] = {"side": "long"}
    with _Pinned("short", 1000):
        ex.poll()
    assert ex.ledger.risk.halted
    assert not broker.entered


def test_pullback_latches_are_independent_per_symbol(tmp_path, monkeypatch):
    broker = FakeBroker()
    ex = make_executor(tmp_path, broker, (SYMBOL, OTHER))
    ex.config = replace(ex.config, entry_mode="pullback")
    monkeypatch.setattr(executor_module, "fetch_candles", lambda *a, **k: [])
    monkeypatch.setattr(executor_module, "calculate_indicators", lambda *a, **k: {
        "ts": 1000, "close": 100, "low": 100, "high": 100,
        "ema_fast": 100, "ema_slow": 99, "adx": 30})
    ex.poll()
    assert {item[0] for item in broker.entered} == {SYMBOL, OTHER}


def test_stale_quotes_cannot_value_positions(store, monkeypatch):
    tick = {"last": 100, "bid": 99, "ask": 101,
            "ts": int(time.time() * 1000) - 120_000}
    store.save_tick(SYMBOL, tick)
    monkeypatch.setattr(api_main, "_poller", None)
    monkeypatch.setattr(api_main.market, "fetch_ticker", lambda *a: tick)
    with pytest.raises(api_main.HTTPException) as error:
        api_main._risk_ticker(store, SYMBOL)
    assert error.value.status_code == 503
    tick["ts"] = int(time.time() * 1000)
    assert api_main._risk_ticker(store, SYMBOL)["last"] == 100


def test_stop_must_belong_to_the_entry(monkeypatch):
    class Exchange:
        def fetch_order(self, *args):
            return {"info": {"attachAlgoOrds": [{"attachAlgoId": "new"}]}}

        def fetch_open_orders(self, *args, **kwargs):
            return [{"id": "old", "info": {"slTriggerPx": "98"}}]

    probe = Broker.__new__(Broker)
    probe.exchange = Exchange()
    monkeypatch.setattr("okx_ema_trader.broker.time.sleep", lambda _: None)
    assert probe._find_stop_id(SYMBOL, "entry") is None
    monkeypatch.setattr(probe.exchange, "fetch_open_orders", lambda *a, **k: [
        {"id": "new", "info": {"slTriggerPx": "98"}}])
    assert probe._find_stop_id(SYMBOL, "entry") == "new"


@pytest.mark.parametrize("price", [0, -1, float("inf"), float("nan")])
def test_invalid_fill_prices_are_rejected(store, price):
    with pytest.raises(ValueError):
        paper.open_position(store, SYMBOL, "long", 100, 5, price)
    assert not store.open_orders()


def test_manual_open_cannot_bypass_a_halt(store):
    health.set_risk_halt(store, "test")
    with pytest.raises(ValueError, match="熔断"):
        paper.open_position(store, SYMBOL, "long", 100, 5, 100)


def test_zero_pnl_does_not_erase_a_losing_streak(tmp_path):
    ex = make_executor(tmp_path, FakeBroker())
    ex.guard.record_close(-1)
    ex.guard.record_close(0)
    assert ex.ledger.risk.consecutive_losses == 1


def test_settlement_uses_actual_fills_and_fees_not_latest_price():
    position = Position(side="long", size=2, entry_price=100,
                        entry_ts=100_000, entry_order_id="entry")
    fills = [
        {"order": "entry", "timestamp": 99_000, "side": "buy", "amount": 2,
         "price": 100, "fee": {"cost": 0.1, "currency": "USDT"}},
        {"order": "exit", "timestamp": 101_000, "side": "sell", "amount": 1,
         "price": 99, "fee": {"cost": .05, "currency": "USDT"}},
        {"order": "exit", "timestamp": 101_001, "side": "sell", "amount": 1,
         "price": 98, "fee": {"cost": .05, "currency": "USDT"}},
    ]
    class Exchange:
        def fetch_my_trades(self, *a, **k):
            return fills
    probe = Broker.__new__(Broker)
    probe.exchange = Exchange()
    probe.notional_for_contracts = lambda s, amount, price: amount * price
    assert probe.closed_pnl(SYMBOL, position) == pytest.approx(-3.2)
    fills.pop()
    with pytest.raises(BrokerError, match="记录不完整"):
        probe.closed_pnl(SYMBOL, position)


def test_account_halt_retries_all_positions_after_partial_failure(store, monkeypatch):
    paper.open_position(store, SYMBOL, "long", 100, 5, 100)
    health.set_risk_halt(store, "account-drawdown")
    close = paper.close_position
    def fail(*a, **k):
        raise ValueError("temporary failure")
    monkeypatch.setattr(paper, "close_position", fail)
    assert health.enforce_risk(store, limits=health.RiskLimits(),
                               marks={SYMBOL: 100})["failed"]
    monkeypatch.setattr(paper, "close_position", close)
    report = health.enforce_risk(store, limits=health.RiskLimits(), marks={SYMBOL: 100})
    assert len(report["closed"]) == 1
    assert not store.open_orders()


def test_paper_reversal_cannot_reopen_after_fifth_loss(store):
    for _ in range(4):
        oid = store.add_order(SYMBOL, "long", status="filled")
        store.close_order(oid, 100, pnl=-1)
    paper.open_position(store, SYMBOL, "short", 100, 5, 100)
    for inst in (SYMBOL, OTHER):
        seed(store, inst)
    autotrader.set_state(store, enabled=True, symbols=[SYMBOL, OTHER])
    def check_risk(s):
        return health.enforce_risk(
            s, limits=health.RiskLimits(max_consecutive_losses=5),
            marks={SYMBOL: 101, OTHER: 101})
    result = autotrader.maybe_trade_all(
        store, top_up=False,
        signal_fn=lambda *a: executor_module.Signal("long", "test"),
        ticker_fn=lambda *a: {"last": 101, "bid": 100, "ask": 101},
        risk_check_fn=check_risk)
    assert health.risk_halted(store)
    assert not store.open_orders()
    assert not any(r.get("opened") for r in result["results"])


def test_concurrent_orders_cannot_spend_the_same_margin(store):
    from concurrent.futures import ThreadPoolExecutor
    paper.reset_account(store, 100)
    def attempt(inst):
        try:
            paper.open_position(store, inst, "long", 80, 1, 100)
            return True
        except ValueError:
            return False
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [SYMBOL, OTHER]))
    assert sum(outcomes) == 1
    assert len(store.open_orders()) == 1


def test_exposure_cap_uses_contract_face_value(tmp_path):
    broker = FakeBroker(contract_value=.01)
    ex = make_executor(tmp_path, broker, (SYMBOL, OTHER))
    ex.trading = replace(ex.trading, sizing_mode="fixed", leverage=1,
                         notional_usdt=1, max_total_notional=3)
    ex.ledger.set(OTHER, Position(side="long", entry_price=100, size=1))
    ex._flip(SYMBOL, executor_module.Signal("long", "test"), Position.flat())
    assert len(broker.entered) == 1
