"""Tests for the leverage/stop/position-size safety rules.

The 2026-10 account history made the reason for this file concrete: at 50x the
liquidation distance (~2%) equals the stop width (2%), so the stop can never
fill before the exchange force-closes. Both positions that ended in 全部强平
were exactly that case. These tests pin the arithmetic that catches it, so the
combination cannot be re-introduced by editing config.yaml alone.

Three properties, each with a test that fails if the rule is removed:
  1. a stop beyond liquidation distance is rejected;
  2. one stop may not risk more than 25% of equity;
  3. sizing_mode="risk" sizes the position from the stop distance, not from
     leverage — the same account risk at 10x and 50x must give the same size.

Run:
    set PYTHONPATH=src
    .venv/Scripts/python.exe -m pytest tests/test_risk_sizing.py -q
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from okx_ema_trader.config import (Config, TradingConfig, account_risk_per_trade_pct,
                                   liquidation_distance_pct, validate)
from okx_ema_trader.executor import exposure_for

LEVERAGED = 10.0


def _config(**trading) -> Config:
    """A valid baseline; override only what the test is about."""
    base = TradingConfig(
        enabled=True,
        symbols=("BTC-USDT-SWAP",),
        sizing_mode="equity",
        equity_pct=20.0,
        leverage=10,
        stop_loss_pct=3.0,
        take_profit_pct=0.0,
        max_total_notional=500_000.0,
    )
    return Config(
        symbol="BTC-USDT-SWAP", bar_5m="5m", bar_15m="15m", candle_limit=200,
        ema_fast=20, ema_slow=50, adx_period=14, adx_min=20.0,
        deviation_max=0.02, entry_mode="crossover", pullback_tolerance=0.002,
        reconnect_seconds=5, log_level="INFO",
        trading=replace(base, **trading) if trading else base,
    )


# --------------------------------------------------------------- 1. 止损够不到强平价
def test_stop_at_or_beyond_liquidation_is_rejected() -> None:
    """50x + 2% stop is the account's real configuration; it must not load.

    At 50x the liquidation distance is ~2% and the stop sat at 2% — the stop
    and the force-close raced, and both 强平 positions lost 200%+ on margin.
    This is the arithmetic that turns "1% margin used" into "no stop exists".
    """
    for leverage, stop in [(50, 2.0), (75, 2.0), (100, 2.0), (50, 3.0)]:
        with pytest.raises(ValueError, match="liquidation"):
            validate(_config(leverage=leverage, stop_loss_pct=stop))


def test_stop_inside_liquidation_distance_is_accepted() -> None:
    """The guard must not fire on safe settings, or it is just noise."""
    validate(_config(leverage=10, stop_loss_pct=3.0))   # 10% distance, 3% stop
    validate(_config(leverage=20, stop_loss_pct=2.0))   # 5% distance, 2% stop
    validate(_config(leverage=10, stop_loss_pct=9.5))   # 10% distance, tight fit


def test_liquidation_distance_is_the_hard_ceiling_not_the_stop() -> None:
    """A stop exactly AT the distance is still rejected — it is a coin flip."""
    assert liquidation_distance_pct(50) == 2.0
    with pytest.raises(ValueError, match="liquidation"):
        validate(_config(leverage=50, stop_loss_pct=2.0))


# ----------------------------------------------------------------- 2. 单笔风险上限
def test_single_stop_risk_above_ceiling_is_rejected() -> None:
    """25x x 2% x 100% equity = 50% of the account per trade. Rejected.

    Two such stops in a row leave 25% of the equity, and max_drawdown_pct=60
    would not fire until the account is already unrecoverable.
    """
    with pytest.raises(ValueError, match="25%"):
        validate(_config(sizing_mode="equity", equity_pct=100.0,
                         leverage=25, stop_loss_pct=2.0))


def test_account_risk_is_computed_the_same_way_the_guard_uses() -> None:
    """account_risk_per_trade_pct must equal what GuardLimits derives.

    If these disagree, the single-position circuit breaker is set from a
    different number than validate() approved — the guard would then halt on
    a perfectly normal loss.
    """
    from okx_ema_trader.risk_guard import GuardLimits
    trading = _config(sizing_mode="equity", equity_pct=20.0,
                      leverage=10, stop_loss_pct=3.0).trading
    expected = 3.0 * 10 * 0.20  # 6%
    assert account_risk_per_trade_pct(trading) == pytest.approx(expected)
    assert GuardLimits.for_trading(trading).max_position_loss_pct == pytest.approx(expected)


# ----------------------------------------------------------------- 3. risk 模式仓位
def test_risk_sizing_ignores_leverage() -> None:
    """The same account risk must give the same position at any leverage.

    This is what makes leverage safe to use for margin efficiency: sizing from
    the stop distance (rather than from equity x leverage) keeps a 1% stop-loss
    budget at 1% regardless of whether the account runs 10x or 50x.
    """
    def size(leverage: int) -> float:
        trading = _config(sizing_mode="risk", risk_per_trade_pct=1.0,
                          stop_loss_pct=2.0, leverage=leverage).trading
        return exposure_for(trading, 10_000.0)

    assert size(10) == pytest.approx(size(50))
    # 1% risk, 2% stop -> position worth half the account
    assert size(10) == pytest.approx(5_000.0)


def test_risk_sizing_actually_bounds_the_loss_at_the_stop() -> None:
    """Verify the invariant the mode promises: hitting the stop costs exactly
    risk_per_trade_pct of the account, at every leverage.

    This is the property worth pinning — not the arithmetic (which the test
    above covers) but the promise the config comment makes.
    """
    for leverage in (10, 25, 50):
        trading = _config(sizing_mode="risk", risk_per_trade_pct=2.0,
                          stop_loss_pct=3.0, leverage=leverage).trading
        equity = 10_000.0
        exposure = exposure_for(trading, equity)
        loss_pct = exposure * (trading.stop_loss_pct / 100.0) / equity * 100.0
        assert loss_pct == pytest.approx(2.0), f"leverage {leverage} gave {loss_pct}%"


def test_risk_sizing_rejects_an_unbounded_budget() -> None:
    for bad in (0.0, -1.0, 101.0):
        with pytest.raises(ValueError, match="risk_per_trade_pct"):
            validate(_config(sizing_mode="risk", risk_per_trade_pct=bad))


def test_unknown_sizing_mode_is_rejected() -> None:
    """A typo must fail loudly, not silently fall back to a different mode."""
    with pytest.raises(ValueError, match="sizing_mode"):
        validate(_config(sizing_mode="risky"))