"""出场规则：止盈挂单、交易所远程平仓后的结算、出场后观望。

单独一个文件是因为这三件事是同一条链路：止盈/止损在交易所成交之后，机器人
必须能自己回到 flat 并等下一个信号——否则"止盈后观望"根本跑不起来。

每个用例都对着真实 Executor.poll() 跑，broker 是假的。不联网、不下单。

Run:
    set PYTHONPATH=src
    .venv/Scripts/python.exe tests/test_exit_rules.py
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from okx_ema_trader import executor as executor_module
from okx_ema_trader.broker import BrokerError, Fill
from okx_ema_trader.config import load_config
from okx_ema_trader.executor import Executor
from okx_ema_trader.state import Ledger, Position
from okx_ema_trader.strategy import Signal

SYMBOL = "BTC-USDT-SWAP"
CONFIG = Path(__file__).parents[1] / "config" / "config.yaml"


def _expect(ok: bool, label: str) -> int:
    if not ok:
        print(f"  FAIL {label}")
        return 1
    return 0


class FakeBroker:
    """只实现 executor 会用到的那几个方法。"""

    def __init__(self, equity: float = 1000.0, price: float = 100.0,
                 contract_value: float = 0.01) -> None:
        self.equity = equity
        self.price_value = price
        self.contract_value = contract_value
        self.live: dict[str, dict] = {}
        self.closed: list[str] = []
        self.entered: list[tuple[str, str, float, float]] = []
        self.position_error: Exception | None = None

    def verify_demo(self) -> float:
        return self.equity

    def price(self, symbol: str) -> float:
        return self.price_value

    def get_position(self, symbol: str) -> dict | None:
        if self.position_error is not None:
            raise self.position_error
        return self.live.get(symbol)

    def notional_for_contracts(self, symbol: str, contracts: float, price: float) -> float:
        return contracts * self.contract_value * price

    def contracts_for_notional(self, symbol: str, notional: float, price: float) -> float:
        return round(notional / (self.contract_value * price), 4)

    def set_leverage(self, symbol: str, leverage: int) -> int:
        return leverage

    def close_position(self, symbol: str) -> bool:
        self.closed.append(symbol)
        return self.live.pop(symbol, None) is not None

    def cancel_stops(self, symbol: str) -> None:
        pass

    def market_entry(self, symbol: str, side: str, notional_usdt: float,
                     leverage: int, stop_pct: float, client_id: str | None = None,
                     take_profit_pct: float = 0.0) -> Fill:
        price = self.price_value
        size = self.contracts_for_notional(symbol, notional_usdt * leverage, price)
        stop = price * (1 - stop_pct / 100) if side == "long" \
            else price * (1 + stop_pct / 100)
        # 复算一遍真实 broker 的算法：多单止盈在上方，空单在下方。
        tp = 0.0
        if take_profit_pct > 0:
            tp = price * (1 + take_profit_pct / 100) if side == "long" \
                else price * (1 - take_profit_pct / 100)
        self.entered.append((symbol, side, take_profit_pct, tp))
        self.live[symbol] = {"side": side}
        return Fill(order_id="fake", side=side, price=price, size=size,
                    notional=self.notional_for_contracts(symbol, size, price),
                    stop_price=stop, stop_order_id="fake-stop", take_profit_price=tp)


def _candles(last_ts: int) -> list[list[float]]:
    """200 根够指标热身的假 K 线，最后一根的 ts 就是"当前 15m bar"的身份。

    价格必须真的在动：全平的 K 线会让 TR 恒为非零而 +DM/-DM 恒为 0，DX 变成
    0/0 = NaN，ADX 跟着 NaN，`calculate_indicators` 于是返回空字典——连 ts 都
    没有。用常量 K 线写测试会静默地测到一个假的状态。
    """
    rows = []
    for i in range(200):
        base = 100.0 + i * 0.05          # 温和单调上行，ADX 会很高
        rows.append([float(i), base, base + 0.5, base - 0.5, base + 0.25, 1.0])
    # 倒数第二根：executor 的 `_closed()` 会丢掉最后一根（未成形的那根），
    # 所以真正被读到的"最后一根收盘 K 线"是它。
    rows[-2][0] = float(last_ts)
    return rows


class _Pinned:
    """把一个钉死的信号塞进 executor，用完必须还原。"""

    def __init__(self, side: str, ts15: int) -> None:
        self._fetch = executor_module.fetch_candles
        self._classify = executor_module._classify
        executor_module.fetch_candles = lambda *a, **k: _candles(ts15)
        executor_module._classify = lambda *a, **k: (Signal(side, "test"), side)

    def retarget(self, side: str, ts15: int) -> None:
        executor_module.fetch_candles = lambda *a, **k: _candles(ts15)
        executor_module._classify = lambda *a, **k: (Signal(side, "test"), side)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        executor_module.fetch_candles = self._fetch
        executor_module._classify = self._classify
        return False


def _new_executor(broker: FakeBroker, ledger: Ledger):
    config = load_config(CONFIG)
    config = replace(config, trading=replace(config.trading, symbols=(SYMBOL,)))
    return Executor(config, broker, ledger)


def _ledger() -> Ledger:
    return Ledger(positions={}, path=Path(tempfile.mkdtemp()) / "state.yaml")


# --------------------------------------------------------------- 止盈挂单
def test_take_profit_reaches_the_broker() -> int:
    """config 里写了 take_profit_pct，executor 就必须把它交给 broker。"""
    failures = 0
    broker, ledger = FakeBroker(), _ledger()
    with _Pinned("long", 1000) as pin:
        _new_executor(broker, ledger).poll()
    failures += _expect(len(broker.entered) == 1, f"应开 1 笔，实际 {broker.entered}")
    if broker.entered:
        _, side, tp_pct, tp_price = broker.entered[0]
        failures += _expect(tp_pct == 2.0, f"take_profit_pct 应为 2.0，实际 {tp_pct}")
        failures += _expect(tp_price > broker.price_value,
                            f"多单止盈必须在入场价上方，实际 {tp_price}")
        failures += _expect(abs(tp_price - broker.price_value * 1.02) < 1e-9,
                            f"止盈价应为入场价×1.02，实际 {tp_price}")

    # 空单方向相反。
    broker2, ledger2 = FakeBroker(), _ledger()
    with _Pinned("short", 1000):
        _new_executor(broker2, ledger2).poll()
    if broker2.entered:
        _, _, _, tp_price = broker2.entered[0]
        failures += _expect(tp_price < broker2.price_value,
                            f"空单止盈必须在入场价下方，实际 {tp_price}")
    else:
        failures += _expect(False, "空单没有开仓")
    print(f"  止盈挂单: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


# -------------------------------------------- 交易所已平仓：结算 + 回到 flat
def test_remote_close_settles_and_returns_to_flat() -> int:
    """止盈/止损在交易所成交后，账本必须自己恢复 flat，否则这个 symbol 卡死。"""
    failures = 0
    broker, ledger = FakeBroker(), _ledger()
    ledger.set(SYMBOL, Position(side="long", entry_price=100.0, size=1.0,
                                stop_price=98.0, entry_ts=1))
    ledger.save()
    broker.live = {}  # 交易所已经没有仓位了

    with _Pinned("long", 1000):
        _new_executor(broker, ledger).poll()

    failures += _expect(ledger.get(SYMBOL).side == "flat",
                        f"账本应恢复 flat，实际 {ledger.get(SYMBOL).side}")
    failures += _expect(broker.entered == [], f"结算这一轮不该开新仓，实际 {broker.entered}")
    print(f"  远程平仓结算: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


# ---------------------------------------------------------------- 出场后观望
def test_exit_then_wait_for_a_new_signal() -> int:
    """同一根 15m bar 发出的同方向信号不允许把刚平掉的仓位买回来。"""
    failures = 0
    broker, ledger = FakeBroker(), _ledger()
    ledger.set(SYMBOL, Position(side="long", entry_price=100.0, size=1.0,
                                stop_price=98.0, entry_ts=1))
    ledger.save()
    broker.live = {}

    with _Pinned("long", 1000) as pin:
        ex = _new_executor(broker, ledger)
        ex.poll()                                   # 结算，恢复 flat
        failures += _expect(ledger.get(SYMBOL).side == "flat", "第一轮应恢复 flat")

        ex.poll()                                   # 同一信号再来一次
        failures += _expect(broker.entered == [],
                            f"同一信号不该重进，实际 {broker.entered}")

        pin.retarget("long", 2000)                  # 换一根 15m bar = 新信号
        ex.poll()
        failures += _expect(len(broker.entered) == 1,
                            f"新信号应开仓，实际 {broker.entered}")

    print(f"  出场后观望: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


# ------------------------------------------------ "读不到"不等于"已经平了"
def test_unreadable_position_leaves_ledger_alone() -> int:
    """get_position 抛错时绝不能把账本清成 flat——那等于在未知状态上开新仓。"""
    failures = 0
    broker, ledger = FakeBroker(), _ledger()
    broker.live[SYMBOL] = {"side": "long"}          # 交易所确实有仓
    broker.position_error = BrokerError("proxy down")
    ledger.set(SYMBOL, Position(side="long", entry_price=100.0, size=1.0,
                                stop_price=98.0, entry_ts=1))
    ledger.save()

    with _Pinned("long", 1000):
        _new_executor(broker, ledger).poll()

    failures += _expect(ledger.get(SYMBOL).is_open, "读不到持仓时账本必须原样保留")
    print(f"  读不到持仓不动账本: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def main() -> int:
    print("exit rules tests (offline; no network, no orders)")
    print("")
    failures = 0
    for test in (test_take_profit_reaches_the_broker,
                 test_remote_close_settles_and_returns_to_flat,
                 test_exit_then_wait_for_a_new_signal,
                 test_unreadable_position_leaves_ledger_alone):
        failures += test()
    print("")
    print(f"exit rules: {'ALL OK' if not failures else str(failures) + ' FAILED'}")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
