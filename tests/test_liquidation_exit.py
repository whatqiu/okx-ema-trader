"""Liquidation-only protection: direction, buffer boundaries and order behavior."""
from dataclasses import replace
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "platform" / "backend"))

import health
import paper
from okx_ema_trader.config import load_config, validate, sizing_exit_distance_pct
from okx_ema_trader.executor import Executor, exposure_for
from okx_ema_trader.state import Ledger, Position
from okx_ema_trader.broker import Broker, BrokerError
from okx_ema_trader.risk_guard import GuardLimits, RiskGuard
from okx_ema_trader.liquidation import distance_pct, buffer_price
from okx_ema_trader.storage import Store
from test_exit_rules import FakeBroker

SYMBOL = "BTC-USDT-SWAP"


@pytest.mark.parametrize("side,liq,mark", [("long", 80, 82.4), ("short", 120, 116.4),
                                          ("long", 80, 70), ("short", 120, 130)])
def test_at_buffer_or_beyond_liquidation_always_exits(side, liq, mark):
    assert distance_pct(side, mark, liq) <= 3 + 1e-10
    limits = health.RiskLimits(max_loss_pct=0, price_stop_enabled=False, liq_buffer_pct=3)
    pos = dict(id=1, inst_id=SYMBOL, side=side, margin=100,
               unrealised_pnl=-1, liq_price=liq, mark_price=mark)
    assert health.evaluate_risk([pos], equity=1000, baseline=1000,
                                limits=limits).should_close


def test_old_fixed_stop_and_margin_loss_do_not_exit_early():
    pos = dict(id=1, inst_id=SYMBOL, side="long", margin=100,
               unrealised_pnl=-70, liq_price=80, mark_price=90, stop_price=98)
    decision = health.evaluate_risk(
        [pos], equity=1000, baseline=1000,
        limits=health.RiskLimits(max_loss_pct=0, price_stop_enabled=False))
    assert not decision.should_close


def test_executor_uses_exchange_liquidation_not_leverage_approximation(tmp_path):
    broker = FakeBroker()
    ledger = Ledger(path=tmp_path / "state.yaml")
    ledger.set(SYMBOL, Position(side="long", size=1, entry_price=100))
    broker.live[SYMBOL] = dict(markPrice=90, liquidationPrice=80, unrealizedPnl=-100)
    cfg = load_config(ROOT / "config" / "config.yaml")
    guard = RiskGuard(GuardLimits.for_trading(cfg.trading, cfg.risk), broker, ledger)
    assert not guard.check().triggered
    broker.live[SYMBOL]["markPrice"] = 82.4
    assert SYMBOL in guard.check().close_symbols


def test_missing_exchange_liquidation_blocks_risk_pass(tmp_path):
    broker = FakeBroker()
    ledger = Ledger(path=tmp_path / "state.yaml")
    ledger.set(SYMBOL, Position(side="long", size=1, entry_price=100))
    broker.live[SYMBOL] = dict(markPrice=100)
    guard = RiskGuard(GuardLimits(max_position_loss_pct=0, liquidation_buffer_pct=3),
                      broker, ledger)
    with pytest.raises(BrokerError, match="强平价"):
        guard.check()


def test_risk_checks_can_run_without_evaluating_entry_signals(tmp_path, monkeypatch):
    cfg = load_config(ROOT / "config" / "config.yaml")
    ex = Executor(cfg, FakeBroker(), Ledger(path=tmp_path / "state.yaml"))
    calls = []
    monkeypatch.setattr(ex, "_poll_symbol", lambda s: calls.append(s))
    ex.poll(evaluate_signals=False)
    assert not calls
    assert ex.ledger.risk.peak_equity == 1000


def test_current_config_and_risk_sizing_do_not_depend_on_fixed_stop():
    cfg = load_config(ROOT / "config" / "config.yaml")
    assert cfg.trading.exit_mode == "liquidation"
    assert cfg.trading.stop_loss_pct == 0
    assert cfg.trading.liquidation_buffer_pct == 3
    assert sizing_exit_distance_pct(cfg.trading) == pytest.approx(2.15)
    assert exposure_for(cfg.trading, 1000) == pytest.approx(10 / .0215)
    assert GuardLimits.for_trading(cfg.trading, cfg.risk).max_position_loss_pct == 0
    with pytest.raises(ValueError, match="buffer"):
        validate(replace(cfg, trading=replace(cfg.trading, leverage=50)))


def test_no_fixed_stop_is_attached_or_verified():
    class Exchange:
        def create_order(self, *args):
            assert "stopLoss" not in args[-1]
            return {"id": "entry", "average": 100, "filled": 1}
    broker = Broker.__new__(Broker)
    broker.exchange = Exchange()
    broker.dry_run = False
    broker.price = lambda s: 100
    broker.contracts_for_notional = lambda *a: 1
    broker.spec = lambda s: None
    broker.notional_for_contracts = lambda s, amount, price: amount * price
    from unittest.mock import patch
    with patch("okx_ema_trader.broker.instruments.check_order_size", return_value=None):
        fill = broker.market_entry(SYMBOL, "long", 10, 10, 0)
    assert fill.stop_price == 0
    assert not fill.stop_order_id


def test_paper_rejects_entry_already_inside_buffer(tmp_path):
    store = Store(tmp_path / "test.db")
    try:
        with pytest.raises(ValueError, match="缓冲"):
            paper.open_position(store, SYMBOL, "long", 100, 50, 100,
                                liquidation_buffer_pct=3)
        assert not store.open_orders()
    finally:
        store.close()


def test_restart_migrates_only_the_recorded_fixed_protection(tmp_path):
    class MigratingBroker(FakeBroker):
        cancelled = []
        def cancel_fixed_stop(self, symbol, stop_id):
            self.cancelled.append((symbol, stop_id))
    broker = MigratingBroker()
    broker.live[SYMBOL] = dict(side="long", markPrice=100, liquidationPrice=80)
    ledger = Ledger(path=tmp_path / "state.yaml")
    ledger.set(SYMBOL, Position(side="long", entry_price=100, size=1,
                                stop_price=98, stop_order_id="old-stop"))
    ex = Executor(load_config(ROOT / "config" / "config.yaml"), broker, ledger)
    ex.reconcile()
    assert broker.cancelled == [(SYMBOL, "old-stop")]
    saved = Ledger.load(ledger.path).get(SYMBOL)
    assert saved.stop_price == 0
    assert not saved.stop_order_id


def test_migration_cancels_algo_order_with_trigger_flag():
    class Exchange:
        orders = [{"id": "old"}, {"id": "unrelated"}]
        def fetch_open_orders(self, *a, **k):
            return self.orders
        def cancel_order(self, order_id, symbol, params):
            assert params == {"trigger": True}
            self.orders = [o for o in self.orders if o["id"] != order_id]
    broker = Broker.__new__(Broker)
    broker.dry_run = False
    broker.exchange = Exchange()
    broker.cancel_fixed_stop(SYMBOL, "old")
    assert broker.exchange.orders == [{"id": "unrelated"}]


def test_backtest_uses_side_specific_liquidation_boundary(monkeypatch):
    import pandas as pd
    from okx_ema_trader import backtest
    from okx_ema_trader.strategy import Signal
    rows = []
    for i in range(6):
        rows.append(dict(ts=i * 300_000, open=100, high=101, low=99 if i < 4 else 97,
                         close=100, ema_fast=100, ema_slow=99, adx=30))
    f5 = pd.DataFrame(rows)
    f15 = pd.DataFrame([dict(ts=ts, close=100, ema_fast=100, ema_slow=99, adx=30)
                        for ts in (-1_800_000, -900_000)])
    monkeypatch.setattr(backtest, "evaluate_signal", lambda *a: Signal("long", "test"))
    report = backtest.simulate(f5, f15, 20, .02, 0, 300_000, 0,
                               liquidation_buffer_pct=3, leverage=20)
    stops = [t for t in report.trades if t.exit_reason == "stop"]
    assert stops
    assert stops[0].exit_price == pytest.approx(buffer_price(100, "long", 20, 3))
