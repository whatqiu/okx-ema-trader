from dataclasses import replace
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from okx_ema_trader.config import TradingConfig, load_config, validate
from okx_ema_trader.executor import exposure_for
from okx_ema_trader.sizing import bounded_exposure


def settings():
    return TradingConfig(sizing_mode='risk', risk_per_trade_pct=0.25,
                         stop_loss_pct=1.5, leverage=3, execution_cost_bps=20,
                         max_total_notional=10000, max_portfolio_risk_pct=0.75,
                         max_same_side_risk_pct=0.5)


def position(side='long', notional=2000, stop=98.5):
    return dict(side=side, notional=notional, entry_price=100, stop_price=stop)


def test_costs_fit_inside_single_trade_budget():
    trading = settings()
    exposure = exposure_for(trading, 10000)
    assert exposure * (0.015 + 0.002) == pytest.approx(25)


def test_same_side_budget_includes_existing_stop_and_costs():
    exposure = bounded_exposure(settings(), 10000, 5000, 'long', [position()])
    assert exposure * 0.017 + 2000 * 0.017 == pytest.approx(50)


def test_opposite_positions_still_consume_portfolio_budget():
    exposure = bounded_exposure(settings(), 10000, 5000, 'long',
                                [position('short', 4000, 101.5)])
    assert exposure * 0.017 + 4000 * 0.017 == pytest.approx(75)


def test_exhausted_budget_blocks_entry():
    assert bounded_exposure(settings(), 10000, 100, 'long', [position(notional=4000)]) == 0


def test_unprotected_existing_position_blocks_entry():
    with pytest.raises(ValueError, match='protective stop'):
        bounded_exposure(settings(), 10000, 100, 'long', [position(stop=None)])


def test_notional_cap_remains_binding():
    trading = replace(settings(), max_total_notional=2050)
    assert bounded_exposure(trading, 10000, 100, 'short', [position()]) == 50


def test_pilot_is_disabled_and_validated():
    config = load_config(Path(__file__).parents[1] / 'config/pilot.yaml')
    assert not config.trading.enabled
    assert config.trading.exit_mode == 'fixed'
    assert config.trading.leverage == 3
    for value in (float('nan'), float('inf'), -1):
        with pytest.raises(ValueError):
            validate(replace(config, trading=replace(config.trading, max_same_side_risk_pct=value)))
    with pytest.raises(ValueError, match='fixed stops'):
        validate(replace(config, trading=replace(config.trading, exit_mode='liquidation')))
