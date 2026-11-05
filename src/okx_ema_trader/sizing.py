from __future__ import annotations

import math

from .config import TradingConfig


def bounded_exposure(trading: TradingConfig, equity: float, requested: float,
                     side: str, positions: list[dict]) -> float:
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError('Cannot size entry without positive finite equity')
    if not math.isfinite(requested) or requested <= 0 or side not in ('long', 'short'):
        raise ValueError('Invalid requested exposure or side')
    total_notional = sum(position['notional'] for position in positions)
    available = max(0.0, trading.max_total_notional - total_notional)
    exposure = min(requested, available)
    if not (trading.max_portfolio_risk_pct or trading.max_same_side_risk_pct):
        return exposure
    unit_risk = trading.stop_loss_pct / 100 + trading.execution_cost_bps / 10000
    total_risk = same_side_risk = 0.0
    for position in positions:
        entry = position['entry_price']
        stop = position.get('stop_price')
        notional = position['notional']
        if (not stop or not all(math.isfinite(value) and value > 0
                               for value in (entry, stop, notional))
                or position['side'] not in ('long', 'short')):
            raise ValueError('Existing position has no valid protective stop; entry blocked')
        distance = (1 - stop / entry) if position['side'] == 'long' else (stop / entry - 1)
        risk = notional * (max(distance, 0) + trading.execution_cost_bps / 10000)
        total_risk += risk
        if position['side'] == side:
            same_side_risk += risk
    for budget, used in ((trading.max_portfolio_risk_pct, total_risk),
                         (trading.max_same_side_risk_pct, same_side_risk)):
        if budget:
            exposure = min(exposure, max(0.0, equity * budget / 100 - used) / unit_risk)
    return exposure
