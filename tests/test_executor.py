"""Offline tests for the executor's decision logic.

Same hand-rolled harness style as tests/test_indicators.py: no pytest, each test
returns a failure count, main() sums them. Nothing here touches the network or
places an order — the broker is faked.

Run:
    set PYTHONPATH=src
    .venv/Scripts/python.exe tests/test_executor.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from okx_ema_trader.config import TradingConfig, load_config
from okx_ema_trader.executor import (REASON_ADX, REASON_DEVIATION, REASON_NO_CROSS,
                                     REASON_WARMUP, _classify, _closed, exposure_for)
from okx_ema_trader.state import Ledger, Position, StateError

ADX_MIN = 20.0
DEV_MAX = 0.02


def _inds(ema_fast_5m, ema_slow_5m, adx_5m, ema_fast_15m, ema_slow_15m, adx_15m,
          prev_fast_5m=None, prev_slow_5m=None, close_15m=None) -> tuple[dict, dict]:
    """Build indicator dicts.

    prev_* default to the CURRENT 5m values, i.e. no crossover happened — the
    ordinary state of the world. Callers that want a crossover pass them.
    `close_15m` defaults to EMA20 so the deviation gate reads 0 and stays out of
    the way unless a test is deliberately exercising it.
    """
    return (
        {
            "ema_fast": ema_fast_5m, "ema_slow": ema_slow_5m, "adx": adx_5m,
            "close": 100.0, "ts": 0,
            "prev_ema_fast": ema_fast_5m if prev_fast_5m is None else prev_fast_5m,
            "prev_ema_slow": ema_slow_5m if prev_slow_5m is None else prev_slow_5m,
        },
        {
            "ema_fast": ema_fast_15m, "ema_slow": ema_slow_15m, "adx": adx_15m,
            "close": ema_fast_15m if close_15m is None else close_15m, "ts": 0,
        },
    )


def test_warmup_is_not_signal_loss() -> int:
    """An unfinished warm-up must never look like 'the signal went away'.

    This is the bug the REASON_* split exists to prevent: acting on a warm-up
    gap would close a healthy position.
    """
    failures = 0
    for i5, i15, label in [
        ({}, {}, "both empty"),
        (_inds(1, 2, 30, 1, 2, 30)[0], {}, "15m empty"),
        ({}, _inds(1, 2, 30, 1, 2, 30)[1], "5m empty"),
    ]:
        signal, reason = _classify(i5, i15, ADX_MIN, DEV_MAX)
        if signal is not None or reason != REASON_WARMUP:
            print(f"  FAIL {label}: got signal={signal} reason={reason}")
            failures += 1
    print(f"  warm-up separation: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_no_signal_reasons_are_distinguished() -> int:
    """None must not be one undifferentiated blob."""
    failures = 0

    # 15m ADX below the gate.
    i5, i15 = _inds(2, 1, 30, 2, 1, 10)
    _, reason = _classify(i5, i15, ADX_MIN, DEV_MAX)
    if reason != REASON_ADX:
        print(f"  FAIL adx gate: expected {REASON_ADX}, got {reason}")
        failures += 1

    # 15m bullish, but this 5m bar had no crossover (EMA20 already on top).
    i5, i15 = _inds(2, 1, 30, 2, 1, 30)
    _, reason = _classify(i5, i15, ADX_MIN, DEV_MAX)
    if reason != REASON_NO_CROSS:
        print(f"  FAIL no-cross: expected {REASON_NO_CROSS}, got {reason}")
        failures += 1

    # Same for the bearish environment with EMA20 already below.
    i5, i15 = _inds(1, 2, 30, 1, 2, 30)
    _, reason = _classify(i5, i15, ADX_MIN, DEV_MAX)
    if reason != REASON_NO_CROSS:
        print(f"  FAIL no-cross (short env): expected {REASON_NO_CROSS}, got {reason}")
        failures += 1

    # 15m deviation too large -> entry blocked, and it says why.
    i5, i15 = _inds(2, 1, 30, 2, 1, 30, prev_fast_5m=1.0, prev_slow_5m=2.0, close_15m=2.5)
    signal, reason = _classify(i5, i15, ADX_MIN, DEV_MAX)
    if signal is not None or reason != REASON_DEVIATION:
        print(f"  FAIL deviation: expected {REASON_DEVIATION}, got signal={signal} reason={reason}")
        failures += 1

    print(f"  no-signal reasons: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_adx_is_strictly_greater() -> int:
    """ADX must be > adx_min. Exactly equal is NOT enough."""
    failures = 0
    cross = {"prev_fast_5m": 1.0, "prev_slow_5m": 2.0}  # a genuine golden cross

    i5, i15 = _inds(2, 1, 30, 2, 1, 20.0, **cross)
    signal, reason = _classify(i5, i15, ADX_MIN, DEV_MAX)
    if signal is not None or reason != REASON_ADX:
        print(f"  FAIL ADX==20 must not pass: got signal={signal} reason={reason}")
        failures += 1

    i5, i15 = _inds(2, 1, 30, 2, 1, 20.01, **cross)
    signal, _ = _classify(i5, i15, ADX_MIN, DEV_MAX)
    if signal is None or signal.side != "long":
        print(f"  FAIL ADX just above 20 must pass: got {signal}")
        failures += 1

    print(f"  ADX strictly greater: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_both_directions_need_a_real_cross() -> int:
    failures = 0
    # long: prev EMA20 <= EMA60, now EMA20 > EMA60
    i5, i15 = _inds(2, 1, 30, 2, 1, 30, prev_fast_5m=1.0, prev_slow_5m=2.0)
    signal, _ = _classify(i5, i15, ADX_MIN, DEV_MAX)
    if not signal or signal.side != "long":
        print(f"  FAIL long golden cross: {signal}")
        failures += 1

    # short: prev EMA20 >= EMA60, now EMA20 < EMA60
    i5, i15 = _inds(1, 2, 30, 1, 2, 30, prev_fast_5m=2.0, prev_slow_5m=1.0)
    signal, _ = _classify(i5, i15, ADX_MIN, DEV_MAX)
    if not signal or signal.side != "short":
        print(f"  FAIL short death cross: {signal}")
        failures += 1

    # A bullish environment must ignore a death cross.
    i5, i15 = _inds(1, 2, 30, 2, 1, 30, prev_fast_5m=2.0, prev_slow_5m=1.0)
    signal, _ = _classify(i5, i15, ADX_MIN, DEV_MAX)
    if signal is not None:
        print(f"  FAIL bullish env must ignore a death cross: {signal}")
        failures += 1

    print(f"  direction detection: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_closed_drops_forming_candle() -> int:
    """The forming bar must be excluded, or signals flicker mid-bar."""
    failures = 0
    candles = [[float(i)] + [1.0] * 5 for i in range(5)]
    result = _closed(candles)
    if len(result) != 4 or result[-1][0] != 3.0:
        print(f"  FAIL: expected 4 candles ending at ts=3, got {len(result)}")
        failures += 1
    if _closed([[1.0] * 6]) != [[1.0] * 6]:
        print("  FAIL: single candle should pass through unchanged")
        failures += 1
    print(f"  closed-candle filter: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_exposure_semantics() -> int:
    """Leverage multiplies exposure in both sizing modes."""
    failures = 0
    fixed = TradingConfig(sizing_mode="fixed", notional_usdt=100.0, leverage=10)
    if exposure_for(fixed, 5000.0) != 1000.0:
        print(f"  FAIL fixed 100@10x should be 1000, got {exposure_for(fixed, 5000.0)}")
        failures += 1

    equity = TradingConfig(sizing_mode="equity", equity_pct=50.0, leverage=2)
    if exposure_for(equity, 1000.0) != 1000.0:
        print(f"  FAIL equity 50%@2x on 1000 should be 1000, got {exposure_for(equity, 1000.0)}")
        failures += 1
    print(f"  exposure semantics: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_ledger_roundtrip() -> int:
    failures = 0
    path = Path(tempfile.mkdtemp()) / "state.yaml"
    ledger = Ledger.load(path)  # missing file -> fresh start
    if ledger.get("BTC-USDT-SWAP").side != "flat":
        print("  FAIL: unknown symbol should read as flat")
        failures += 1

    ledger.set("BTC-USDT-SWAP", Position(side="long", entry_price=100.0, size=0.5,
                                         entry_ts=1, stop_price=97.0))
    ledger.save()

    reloaded = Ledger.load(path)
    position = reloaded.get("BTC-USDT-SWAP")
    if position.side != "long" or position.size != 0.5 or position.stop_price != 97.0:
        print(f"  FAIL roundtrip: {position}")
        failures += 1
    if reloaded.open_symbols() != ["BTC-USDT-SWAP"]:
        print(f"  FAIL open_symbols: {reloaded.open_symbols()}")
        failures += 1
    print(f"  ledger roundtrip: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_ledger_refuses_corrupt_state() -> int:
    """'I can't read what I hold' must never be silently treated as 'I hold nothing'."""
    failures = 0
    for content, label in [
        ("", "empty file"),
        ("not: [valid: yaml", "malformed yaml"),
        ("- a\n- b\n", "not a mapping"),
        ("version: 99\npositions: {}\n", "wrong schema version"),
        ("version: 1\npositions: {X: {side: sideways}}\n", "invalid side"),
    ]:
        path = Path(tempfile.mkdtemp()) / "state.yaml"
        path.write_text(content, encoding="utf-8")
        try:
            Ledger.load(path)
            print(f"  FAIL {label}: should have raised")
            failures += 1
        except StateError:
            pass
    print(f"  corrupt-state refusal: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_config_values() -> int:
    """The shipped config must carry the agreed strategy numbers."""
    failures = 0
    config = load_config(Path(__file__).parents[1] / "config" / "config.yaml")
    if config.adx_min != 20.0:
        print(f"  FAIL adx_min should be 20, got {config.adx_min}")
        failures += 1
    if abs(config.deviation_max - 0.02) > 1e-12:
        print(f"  FAIL deviation_max should be 0.02, got {config.deviation_max}")
        failures += 1
    # MU must be the OKX instId, never the bare MUUSDT form broker cannot split.
    bad = [s for s in config.trading.symbols if "-" not in s]
    if bad:
        print(f"  FAIL symbols not in instId form: {bad}")
        failures += 1
    # Risk parameters must be untouched.
    if config.trading.leverage != 10 or config.trading.equity_pct != 100.0 \
            or config.trading.stop_loss_pct != 3.0:
        print("  FAIL risk parameters changed: "
              f"lev={config.trading.leverage} eq={config.trading.equity_pct} "
              f"stop={config.trading.stop_loss_pct}")
        failures += 1
    # The circuit-breaker limits live in config now, so they are just as easy to
    # change by accident as leverage is — and a stray edit here silently moves
    # the point at which the bot stops trading. Pin them the same way.
    if config.risk.max_drawdown_pct != 60.0:
        print(f"  FAIL risk.max_drawdown_pct should be 60, "
              f"got {config.risk.max_drawdown_pct}")
        failures += 1
    if config.risk.max_consecutive_losses != 3:
        print(f"  FAIL risk.max_consecutive_losses should be 3, "
              f"got {config.risk.max_consecutive_losses}")
        failures += 1
    # Left unset on purpose: one stop-out costs stop_loss_pct x leverage = 30%,
    # so the per-position cap is derived rather than spelled out twice.
    if config.risk.max_position_loss_pct is not None:
        print("  FAIL risk.max_position_loss_pct must stay unset so it is "
              f"derived from stop x leverage, got {config.risk.max_position_loss_pct}")
        failures += 1
    print(f"  config values: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_config_backwards_compatible() -> int:
    """A config.yaml predating the trading section must still load."""
    failures = 0
    import yaml
    base = yaml.safe_load((Path(__file__).parents[1] / "config" / "config.yaml").read_text(encoding="utf-8"))
    base.pop("trading", None)
    path = Path(tempfile.mkdtemp()) / "config.yaml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    config = load_config(path)
    if config.trading.enabled is not False:
        print("  FAIL: trading should default to disabled")
        failures += 1
    if config.trading.symbols != ("BTC-USDT-SWAP", "AAVE-USDT-SWAP"):
        print(f"  FAIL default symbols: {config.trading.symbols}")
        failures += 1
    print(f"  config backwards compatibility: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_config_rejects_bad_trading() -> int:
    failures = 0
    import yaml
    base = yaml.safe_load((Path(__file__).parents[1] / "config" / "config.yaml").read_text(encoding="utf-8"))
    for bad in [
        {"leverage": 0},
        {"leverage": 200},
        {"stop_loss_pct": 0},
        {"stop_loss_pct": 150},
        {"notional_usdt": -5},
        {"sizing_mode": "yolo"},
        {"position_mode": "nonsense"},
        {"symbols": []},
        {"symbols": None},
        {"symbols": "BTC-USDT-SWAP"},
    ]:
        raw = dict(base)
        raw["trading"] = bad
        path = Path(tempfile.mkdtemp()) / "config.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        try:
            load_config(path)
            print(f"  FAIL {bad}: should have been rejected")
            failures += 1
        except ValueError:
            pass
    print(f"  config rejection: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def main() -> int:
    print("executor tests (offline; no network, no orders)")
    print("")
    failures = 0
    for test in (
        test_warmup_is_not_signal_loss,
        test_no_signal_reasons_are_distinguished,
        test_adx_is_strictly_greater,
        test_both_directions_need_a_real_cross,
        test_closed_drops_forming_candle,
        test_exposure_semantics,
        test_ledger_roundtrip,
        test_ledger_refuses_corrupt_state,
        test_config_values,
        test_config_backwards_compatible,
        test_config_rejects_bad_trading,
    ):
        failures += test()
    print("")
    if failures:
        print(f"FAILED: {failures} assertion(s)")
    else:
        print("all groups passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
