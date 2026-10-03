from dataclasses import dataclass, field
from pathlib import Path
import yaml


@dataclass(frozen=True)
class TradingConfig:
    """Order-placement settings. Disabled unless explicitly switched on."""

    enabled: bool = False
    symbols: tuple[str, ...] = ("BTC-USDT-SWAP", "AAVE-USDT-SWAP")
    # "fixed" = a flat USDT notional per entry; "equity" = a share of account equity.
    sizing_mode: str = "fixed"
    notional_usdt: float = 100.0
    equity_pct: float = 0.0
    leverage: int = 1
    stop_loss_pct: float = 3.0
    max_total_notional: float = 300.0
    position_mode: str = "cross"


@dataclass(frozen=True)
class Config:
    symbol: str
    bar_5m: str
    bar_15m: str
    candle_limit: int
    ema_fast: int
    ema_slow: int
    adx_period: int
    adx_min: float
    # 15m 偏离度上限：abs(close - EMA20) / EMA20，超过则不允许新开仓。
    deviation_max: float
    reconnect_seconds: int
    log_level: str
    trading: TradingConfig = field(default_factory=TradingConfig)


def load_config(path: str | Path) -> Config:
    with Path(path).open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    strategy = raw["strategy"]
    runtime = raw["runtime"]
    trading = raw.get("trading") or {}

    # Distinguish "key absent" (use defaults) from "key present but empty"
    # (explicitly no symbols -> an error, not a silent fallback to defaults).
    if "symbols" in trading:
        symbols = trading["symbols"]
        if symbols is None:
            raise ValueError("trading.symbols is null; either omit it or list instruments")
        if isinstance(symbols, str):
            raise ValueError(f"trading.symbols must be a list, got the string {symbols!r}")
        symbols = tuple(str(s) for s in symbols)
    else:
        symbols = TradingConfig.symbols

    trading_config = TradingConfig(
        enabled=bool(trading.get("enabled", False)),
        symbols=symbols,
        sizing_mode=str(trading.get("sizing_mode", "fixed")),
        notional_usdt=float(trading.get("notional_usdt", 100.0)),
        equity_pct=float(trading.get("equity_pct", 0.0)),
        leverage=int(trading.get("leverage", 1)),
        stop_loss_pct=float(trading.get("stop_loss_pct", 3.0)),
        max_total_notional=float(trading.get("max_total_notional", 300.0)),
        position_mode=str(trading.get("position_mode", "cross")),
    )

    config = Config(
        symbol=raw["symbol"], bar_5m=raw["bar_5m"], bar_15m=raw["bar_15m"],
        candle_limit=int(raw["candle_limit"]), ema_fast=int(strategy["ema_fast"]),
        ema_slow=int(strategy["ema_slow"]), adx_period=int(strategy["adx_period"]),
        adx_min=float(strategy["adx_min"]),
        deviation_max=float(strategy.get("deviation_max", 0.02)),
        reconnect_seconds=int(runtime["reconnect_seconds"]),
        log_level=runtime["log_level"], trading=trading_config,
    )
    validate(config)
    return config


def validate(config: Config) -> None:
    """Reject settings that would silently produce no signal or an endless restart loop."""
    if config.ema_fast < 1 or config.ema_slow < 1:
        raise ValueError("ema_fast and ema_slow must be >= 1")
    if config.ema_fast >= config.ema_slow:
        raise ValueError(f"ema_fast ({config.ema_fast}) must be < ema_slow ({config.ema_slow})")
    if config.adx_period < 2:
        raise ValueError(f"adx_period must be >= 2, got {config.adx_period}")
    if config.deviation_max <= 0:
        raise ValueError(f"deviation_max must be > 0, got {config.deviation_max}")
    if config.candle_limit < max(config.ema_slow, config.adx_period * 2):
        raise ValueError(
            f"candle_limit ({config.candle_limit}) is below the indicator warm-up "
            f"requirement ({max(config.ema_slow, config.adx_period * 2)})"
        )
    if config.bar_5m == config.bar_15m:
        raise ValueError(f"bar_5m and bar_15m must differ, both are {config.bar_5m!r}")
    if config.reconnect_seconds < 1:
        raise ValueError(f"reconnect_seconds must be >= 1, got {config.reconnect_seconds}")

    trading = config.trading
    if trading.sizing_mode not in ("fixed", "equity"):
        raise ValueError(f"sizing_mode must be 'fixed' or 'equity', got {trading.sizing_mode!r}")
    if trading.sizing_mode == "fixed" and trading.notional_usdt <= 0:
        raise ValueError(f"notional_usdt must be > 0, got {trading.notional_usdt}")
    if trading.sizing_mode == "equity" and not 0 < trading.equity_pct <= 100:
        raise ValueError(f"equity_pct must be in (0, 100], got {trading.equity_pct}")
    if not 1 <= trading.leverage <= 125:
        raise ValueError(f"leverage must be between 1 and 125, got {trading.leverage}")
    if not 0 < trading.stop_loss_pct < 100:
        raise ValueError(f"stop_loss_pct must be in (0, 100), got {trading.stop_loss_pct}")
    if trading.max_total_notional <= 0:
        raise ValueError(f"max_total_notional must be > 0, got {trading.max_total_notional}")
    if trading.position_mode not in ("cross", "isolated"):
        raise ValueError(f"position_mode must be 'cross' or 'isolated', got {trading.position_mode!r}")
    if not trading.symbols:
        raise ValueError("trading.symbols must list at least one instrument")
