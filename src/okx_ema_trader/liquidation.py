"""Shared liquidation-buffer arithmetic; prices are estimates, not guarantees."""
import math


def distance_pct(side: str, mark: float, liquidation: float) -> float:
    if side not in ("long", "short") or not all(
            math.isfinite(v) and v > 0 for v in (mark, liquidation)):
        raise ValueError("需要有效的方向、标记价格和强平价格")
    return round(((mark - liquidation) if side == "long" else
                  (liquidation - mark)) / liquidation * 100, 10)


def estimated_liquidation(entry: float, side: str, leverage: float) -> float:
    return entry * (1 - 1 / leverage if side == "long" else 1 + 1 / leverage)


def buffer_price(entry: float, side: str, leverage: float, buffer_pct: float) -> float:
    liq = estimated_liquidation(entry, side, leverage)
    return liq * (1 + buffer_pct / 100 if side == "long" else 1 - buffer_pct / 100)


def estimated_exit_distance_pct(leverage: float, buffer_pct: float) -> float:
    """Larger side's estimated adverse move, used only for risk sizing."""
    return max(abs(buffer_price(1, side, leverage, buffer_pct) - 1) * 100
               for side in ("long", "short"))
