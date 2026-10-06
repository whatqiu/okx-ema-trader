"""Offline tests for the account-level circuit breaker (risk_guard.py).

Same hand-rolled harness as tests/test_executor.py: no pytest, each test returns
a failure count, main() sums them. Nothing here touches the network, needs an API
key, or places an order — the broker is faked.

Run:
    set PYTHONPATH=src
    .venv/Scripts/python.exe tests/test_risk_guard.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from okx_ema_trader import executor as executor_module
from okx_ema_trader.broker import Fill
from okx_ema_trader.config import load_config
from okx_ema_trader.executor import Executor
from okx_ema_trader.risk_guard import GuardLimits, RiskGuard
from okx_ema_trader.state import Ledger, Position, StateError
from okx_ema_trader.strategy import Signal

SYMBOL = "BTC-USDT-SWAP"
OTHER = "AAVE-USDT-SWAP"

# 这里是显式写死的测试阈值（回撤 30），跟 config.yaml 的 60 故意不同：下面
# 这批老用例钉的是熔断机制本身，"config 里写的数字有没有真的生效"由
# test_config_* 那几个用例单独覆盖。
LIMITS = GuardLimits(max_drawdown_pct=30.0, max_consecutive_losses=3,
                     max_position_loss_pct=30.0)


def _expect(ok: bool, label: str) -> int:
    if not ok:
        print(f"  FAIL {label}")
        return 1
    return 0


class FakeBroker:
    """只实现 risk_guard / executor 会用到的那几个方法。"""

    def __init__(self, equity: float = 1000.0, price: float = 100.0,
                 contract_value: float = 0.01) -> None:
        self.equity = equity
        self.price_value = price
        self.contract_value = contract_value
        self.live: dict[str, dict] = {}   # symbol -> ccxt 风格的仓位 dict
        self.closed: list[str] = []
        self.entered: list[tuple[str, str]] = []

    # --- 账户
    def verify_demo(self) -> float:
        return self.equity

    # --- 行情 / 仓位
    def price(self, symbol: str) -> float:
        return self.price_value

    def get_position(self, symbol: str) -> dict | None:
        return self.live.get(symbol)

    def notional_for_contracts(self, symbol: str, contracts: float, price: float) -> float:
        # 一张合约不等于一个币；这里用固定的面值把换算说清楚。
        return contracts * self.contract_value * price

    def contracts_for_notional(self, symbol: str, notional: float, price: float) -> float:
        return round(notional / (self.contract_value * price), 4)

    # --- 下单
    def set_leverage(self, symbol: str, leverage: int) -> int:
        return leverage

    def close_position(self, symbol: str) -> bool:
        self.closed.append(symbol)
        return self.live.pop(symbol, None) is not None

    def cancel_stops(self, symbol: str) -> None:
        pass

    def market_entry(self, symbol: str, side: str, notional_usdt: float,
                     leverage: int, stop_pct: float, client_id: str | None = None) -> Fill:
        self.entered.append((symbol, side))
        size = self.contracts_for_notional(symbol, notional_usdt * leverage, self.price_value)
        stop = self.price_value * (1 - stop_pct / 100) if side == "long" \
            else self.price_value * (1 + stop_pct / 100)
        return Fill(order_id="fake", side=side, price=self.price_value, size=size,
                    notional=self.notional_for_contracts(symbol, size, self.price_value),
                    stop_price=stop, stop_order_id="fake-stop")


def _setup(equity: float = 1000.0, *, limits: GuardLimits | None = None,
           live: dict[str, dict] | None = None,
           positions: dict[str, Position] | None = None,
           price: float = 100.0) -> tuple[RiskGuard, FakeBroker, Ledger, Path]:
    """一个 guard + 假 broker + 已落盘到账本里的持仓。"""
    path = Path(tempfile.mkdtemp()) / "state.yaml"
    ledger = Ledger.load(path)
    for symbol, position in (positions or {}).items():
        ledger.set(symbol, position)
    ledger.save()

    broker = FakeBroker(equity=equity, price=price)
    for symbol, raw in (live or {}).items():
        broker.live[symbol] = raw
    return RiskGuard(limits or LIMITS, broker, ledger), broker, ledger, path


def _long(entry_price: float = 100.0, size: float = 100.0) -> Position:
    return Position(side="long", entry_ts=1, entry_price=entry_price, size=size,
                    stop_price=entry_price * 0.97)


def _config(**risk: object):
    """拿真实 config.yaml 做底子，只动 risk 段。不带参数 = 老配置（无 risk 段）。"""
    import yaml
    base = yaml.safe_load(
        (Path(__file__).parents[1] / "config" / "config.yaml").read_text(encoding="utf-8"))
    if risk:
        base["risk"] = risk
    else:
        base.pop("risk", None)
    path = Path(tempfile.mkdtemp()) / "config.yaml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    return load_config(path)


# ---------------------------------------------------------------------------
def test_healthy_check_stays_out_of_the_way() -> int:
    """没触发时 check() 不许干预正常交易。"""
    failures = 0
    guard, broker, ledger, _ = _setup(equity=1000.0)
    decision = guard.check()
    failures += _expect(not decision.triggered, f"健康账户不该触发：{decision}")
    failures += _expect(not decision.halted, "健康账户不该 halt")
    failures += _expect(decision.close_symbols == (), "健康账户不该点名任何仓位")
    failures += _expect(guard.risk.peak_equity == 1000.0,
                        f"峰值应记下 1000，实际 {guard.risk.peak_equity}")

    # 权益不变，再跑一轮：峰值不动，回撤 0，仍然不干预。
    guard.check()
    failures += _expect(guard.risk.peak_equity == 1000.0, "峰值不该被拉低")
    failures += _expect(not guard.check().triggered, "第二轮仍不该触发")
    print(f"  未触发时不干预: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_drawdown_halts_and_flattens() -> int:
    failures = 0
    guard, broker, ledger, _ = _setup(
        equity=1000.0, live={SYMBOL: {"unrealizedPnl": 0.0}},
        positions={SYMBOL: _long()})
    guard.check()  # 建立峰值 1000

    broker.equity = 650.0  # 回撤 35% > 30%
    decision = guard.check()
    failures += _expect(decision.halted, "回撤超限必须 halt")
    failures += _expect(decision.close_symbols == (SYMBOL,),
                        f"回撤超限要平掉全部持仓，实际 {decision.close_symbols}")
    failures += _expect(ledger.risk.halted, "halt 必须落在账本里")
    failures += _expect("回撤" in decision.reason, f"原因要写清楚，实际 {decision.reason!r}")

    # 停手是粘性的：权益恢复了也不该自己解除。
    broker.equity = 1200.0
    failures += _expect(guard.check().halted, "halt 不该自动解除")
    print(f"  回撤熔断: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_drawdown_baseline_is_the_peak_and_survives_restart() -> int:
    """回撤基准是历史最高权益，且跨重启连续——重启不该白送一份亏损额度。"""
    failures = 0
    guard, broker, ledger, path = _setup(equity=1000.0)
    guard.check()
    failures += _expect(ledger.risk.peak_equity == 1000.0, "峰值应已落盘")

    # 涨过之后再跌：回撤从峰值算，不是从"这次启动"算。
    broker.equity = 1500.0
    guard.check()
    failures += _expect(ledger.risk.peak_equity == 1500.0, "峰值应随权益抬高")
    broker.equity = 1200.0
    failures += _expect(not guard.check().halted, "20% 回撤未达 30% 上限，不该 halt")

    # 重启 = 新的 guard + 重新 load 的账本。峰值必须还在。
    broker2 = FakeBroker(equity=1000.0)
    guard2 = RiskGuard(LIMITS, broker2, Ledger.load(path))
    failures += _expect(guard2.risk.peak_equity == 1500.0,
                        f"重启后基准应仍是 1500，实际 {guard2.risk.peak_equity}")
    decision = guard2.check()
    failures += _expect(decision.halted, "重启后 33% 回撤必须 halt（基准没被重置）")
    print(f"  回撤基准与持久化: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_consecutive_losses_halt() -> int:
    failures = 0
    guard, _, ledger, _ = _setup()
    for i in (1, 2):
        decision = guard.record_close(-10.0)
        failures += _expect(not decision.halted, f"第 {i} 笔亏损不该 halt")
    failures += _expect(ledger.risk.consecutive_losses == 2, "连亏计数应为 2")

    decision = guard.record_close(-10.0)
    failures += _expect(decision.halted, f"第 3 笔亏损必须 halt：{decision}")
    failures += _expect(ledger.risk.consecutive_losses == 3, "连亏计数应为 3")

    # 盈利清零：连亏不该跨过一笔盈利继续累加。
    ledger.risk.consecutive_losses = 2
    guard.record_close(+25.0)
    failures += _expect(ledger.risk.consecutive_losses == 0, "盈利必须清零连亏")
    failures += _expect(not guard.record_close(-5.0).halted, "清零后再亏一笔不该 halt")
    print(f"  连亏熔断: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_single_position_loss_closes_that_position() -> int:
    failures = 0
    guard, _, ledger, _ = _setup(
        equity=1000.0,
        live={SYMBOL: {"unrealizedPnl": -400.0}, OTHER: {"unrealizedPnl": -10.0}},
        positions={SYMBOL: _long(), OTHER: _long()})
    decision = guard.check()
    failures += _expect(decision.halted, "单仓浮亏超限要停手：止损可能根本没生效")
    failures += _expect(decision.close_symbols == (SYMBOL,),
                        f"只平超限的那一个，实际 {decision.close_symbols}")
    failures += _expect(OTHER not in decision.close_symbols, "没超限的仓位不该被点名")
    print(f"  单仓亏损: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_exchange_side_stop_is_counted_once() -> int:
    """交易所把止损打掉（账本还记着、交易所已无仓位）也要结算，且只结算一次。"""
    failures = 0
    guard, broker, ledger, _ = _setup(
        equity=970.0, live={}, positions={SYMBOL: _long(entry_price=100.0, size=100.0)},
        price=97.0)  # 100 张 × 0.01 × (97-100) = -3 USDT
    guard.check()
    failures += _expect(ledger.risk.consecutive_losses == 1,
                        f"交易所侧平仓应记为一次亏损，实际 {ledger.risk.consecutive_losses}")
    failures += _expect(ledger.get(SYMBOL).settled, "这笔应标记为已结算")
    failures += _expect(ledger.get(SYMBOL).side == "long",
                        "账本故意不改：不一致要留给人核对")

    # 结算本身踩到连亏上限时，check() 必须当场停手——否则这一轮还会去开新仓。
    strict, strict_broker, strict_ledger, _ = _setup(
        equity=970.0, live={}, positions={SYMBOL: _long(entry_price=100.0, size=100.0)},
        price=97.0, limits=GuardLimits(max_consecutive_losses=1))
    decision = strict.check()
    failures += _expect(decision.halted, "连亏上限在循环内触发时 check() 必须报 halt")
    failures += _expect(decision.close_symbols == (),
                        "这个仓位交易所已经没有了，不该再点名去平")
    print(f"  交易所侧止损计入风控: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_halt_persists_into_a_new_guard() -> int:
    """写 ledger -> 新建 RiskGuard 实例 -> 仍然是 halt。"""
    failures = 0
    guard, broker, ledger, path = _setup(
        equity=1000.0, live={SYMBOL: {"unrealizedPnl": 0.0}},
        positions={SYMBOL: _long()})
    guard.check()
    broker.equity = 600.0
    decision = guard.check()
    failures += _expect(decision.halted, "本实例应已 halt")

    reloaded = Ledger.load(path)
    broker2 = FakeBroker(equity=600.0)
    broker2.live[SYMBOL] = {"unrealizedPnl": 0.0}
    guard2 = RiskGuard(LIMITS, broker2, reloaded)
    decision2 = guard2.check()
    failures += _expect(guard2.risk.halted, "新实例读回账本后仍应是 halt")
    failures += _expect(decision2.halted, "新实例的 check() 也要报 halt")
    failures += _expect(decision2.close_symbols == (SYMBOL,), "重启后仍要平掉残留仓位")
    failures += _expect(bool(decision2.reason), "原因要一起持久化")
    print(f"  halt 持久化: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_halt_blocks_new_entries() -> int:
    """核心回归点：halt 之后 poll() 一笔都不许开。"""
    failures = 0
    config = load_config(Path(__file__).parents[1] / "config" / "config.yaml")
    candles = [[float(i), 100.0 + i * 0.1, 100.5 + i * 0.1, 99.5 + i * 0.1,
                100.0 + i * 0.1, 1.0] for i in range(200)]
    original_fetch = executor_module.fetch_candles
    original_classify = executor_module._classify
    executor_module.fetch_candles = lambda *a, **k: candles
    # 策略判定本身不在本测试范围内：钉死一个 long 信号，只看风控会不会放行。
    executor_module._classify = lambda *a, **k: (Signal("long", "test"), "long")
    try:
        # 对照：没熔断时这套假环境确实会开仓，否则下面的 0 笔没有意义。
        base_guard, base_broker, base_ledger, _ = _setup(equity=1000.0)
        Executor(config, base_broker, base_ledger, dry_run=False).poll()
        failures += _expect(len(base_broker.entered) >= 1,
                            "对照组应该开仓，否则测试本身是空的")

        _, broker, ledger, _ = _setup(equity=1000.0)
        ledger.risk.halted = True
        ledger.risk.halt_reason = "test-halt"
        ledger.save()
        Executor(config, broker, ledger, dry_run=False).poll()
        failures += _expect(broker.entered == [], f"熔断中居然开了仓：{broker.entered}")
        failures += _expect(ledger.open_symbols() == [], "熔断中不该留下持仓")
    finally:
        executor_module.fetch_candles = original_fetch
        executor_module._classify = original_classify
    print(f"  halt 后不再开新仓: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_old_state_file_without_risk_section() -> int:
    """旧版 state.yaml（没有 risk 段）必须照常加载，不许因为升级而启动即崩。"""
    failures = 0
    path = Path(tempfile.mkdtemp()) / "state.yaml"
    path.write_text(
        "version: 1\n"
        "positions:\n"
        "  BTC-USDT-SWAP:\n"
        "    side: long\n"
        "    entry_price: 100.0\n"
        "    size: 100.0\n"
        "    stop_price: 97.0\n",
        encoding="utf-8")
    ledger = Ledger.load(path)
    failures += _expect(ledger.get(SYMBOL).side == "long", "旧持仓要照常读出")
    failures += _expect(ledger.get(SYMBOL).settled is False, "新增字段缺失时应取默认值")
    failures += _expect(ledger.risk.halted is False, "risk 段缺失 => 未熔断")
    failures += _expect(ledger.risk.peak_equity == 0.0, "risk 段缺失 => 基准未建立")
    failures += _expect(ledger.risk.consecutive_losses == 0, "risk 段缺失 => 连亏 0")

    # 版本号不许变：升版本会让所有已有 state.yaml 直接 StateError。
    from okx_ema_trader.state import SCHEMA_VERSION
    failures += _expect(SCHEMA_VERSION == 1, f"SCHEMA_VERSION 不该变，现在是 {SCHEMA_VERSION}")

    # 而且 guard 能在这份旧账本上正常工作。
    decision = RiskGuard(LIMITS, FakeBroker(equity=1000.0), ledger).check()
    failures += _expect(not decision.triggered, "旧账本上不该误触发")
    print(f"  旧 state.yaml 向后兼容: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


def test_garbage_risk_section_is_refused() -> int:
    """risk 段写坏了要报错，不能当成"没熔断"悄悄跑起来。"""
    failures = 0
    for content, label in [
        ("version: 1\npositions: {}\nrisk: just-a-string\n", "risk 是字符串"),
        ("version: 1\npositions: {}\nrisk: [1, 2]\n", "risk 是列表"),
    ]:
        path = Path(tempfile.mkdtemp()) / "state.yaml"
        path.write_text(content, encoding="utf-8")
        try:
            Ledger.load(path)
            print(f"  FAIL {label}: 应该拒绝加载")
            failures += 1
        except StateError:
            pass
    print(f"  risk 段损坏时拒绝加载: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} check(s) failed"
    return failures


# ------------------------------------------------- 阈值由 config 驱动（新增）
def test_config_drawdown_limit_is_actually_used() -> None:
    """config 里写的 60 必须真的生效：45% 回撤不熔断（老默认 30 会误杀）。"""
    config = _config(max_drawdown_pct=60, max_consecutive_losses=3)
    assert config.risk.max_drawdown_pct == 60.0

    limits = GuardLimits.for_trading(config.trading, config.risk)
    assert limits.max_drawdown_pct == 60.0
    # 省略 max_position_loss_pct -> 自动推导 = stop_loss_pct × leverage = 3 × 10
    assert limits.max_position_loss_pct == 30.0
    assert limits.max_consecutive_losses == 3

    guard, broker, _, _ = _setup(equity=1000.0, limits=limits)
    guard.check()          # 峰值 1000
    broker.equity = 550.0  # 回撤 45%
    assert not guard.check().halted, "45% 回撤不该熔断：阈值是 60 不是 30"
    broker.equity = 400.0  # 回撤 60%
    assert guard.check().halted, "60% 回撤必须熔断"


def test_risk_section_is_optional() -> None:
    """没有 risk 段的老 config.yaml 照旧加载，阈值回落到代码默认 60。"""
    config = _config()  # 无 risk 段
    assert config.risk.max_drawdown_pct is None
    assert config.risk.max_consecutive_losses is None
    assert config.risk.max_position_loss_pct is None

    limits = GuardLimits.for_trading(config.trading, config.risk)
    assert limits.max_drawdown_pct == 60.0
    assert limits.max_consecutive_losses == 3
    assert limits.max_position_loss_pct == 30.0

    # 默认就是 60，不是旧的 30。
    guard, broker, _, _ = _setup(equity=1000.0, limits=limits)
    guard.check()
    broker.equity = 550.0
    assert not guard.check().halted, "默认值已是 60，45% 回撤不该熔断"


def test_zero_disables_one_rule_only() -> None:
    """0 = 明确关掉这一条（None = 没意见）。关掉回撤后 99% 回撤也不因此熔断。"""
    config = _config(max_drawdown_pct=0)
    limits = GuardLimits.for_trading(config.trading, config.risk)
    assert limits.max_drawdown_pct == 0.0
    # 另外两条没配，仍然走默认值，不受影响。
    assert limits.max_consecutive_losses == 3
    assert limits.max_position_loss_pct == 30.0

    guard, broker, _, _ = _setup(equity=1000.0, limits=limits)
    guard.check()
    broker.equity = 10.0  # 回撤 99%
    assert not guard.check().triggered, "0 应该关掉回撤熔断"


def test_config_rejects_out_of_range_risk() -> None:
    """越界阈值必须在启动前被挡住：永远触发不了的阈值等于静默失效。"""
    for bad in ({"max_drawdown_pct": 150}, {"max_drawdown_pct": -1},
                {"max_consecutive_losses": -1},
                {"max_position_loss_pct": 95}, {"max_position_loss_pct": -5}):
        try:
            _config(**bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} 应该被 load_config 拒绝")

    # 边界内合法：0（关闭）和 100 / 90（上限）都要能加载。
    for good in ({"max_drawdown_pct": 0}, {"max_drawdown_pct": 100},
                 {"max_consecutive_losses": 0}, {"max_position_loss_pct": 90}):
        _config(**good)


def test_executor_hands_config_risk_to_the_guard() -> None:
    """executor 必须把 config.risk 传给 guard，否则 yaml 里改了没用。"""
    config = _config(max_drawdown_pct=60)
    _, broker, ledger, _ = _setup(equity=1000.0)
    executor = Executor(config, broker, ledger)
    assert executor.guard.limits.max_drawdown_pct == 60.0
    assert executor.guard.limits.max_position_loss_pct == 30.0


def main() -> int:
    print("risk guard tests (offline; no network, no orders)")
    print("")
    failures = 0
    for test in (
        test_healthy_check_stays_out_of_the_way,
        test_drawdown_halts_and_flattens,
        test_drawdown_baseline_is_the_peak_and_survives_restart,
        test_consecutive_losses_halt,
        test_single_position_loss_closes_that_position,
        test_exchange_side_stop_is_counted_once,
        test_halt_persists_into_a_new_guard,
        test_halt_blocks_new_entries,
        test_old_state_file_without_risk_section,
        test_garbage_risk_section_is_refused,
        test_config_drawdown_limit_is_actually_used,
        test_risk_section_is_optional,
        test_zero_disables_one_rule_only,
        test_config_rejects_out_of_range_risk,
        test_executor_hands_config_risk_to_the_guard,
    ):
        # 新用例用 assert 写成 None；老用例仍返回失败计数，两种都收。
        failures += test() or 0
    print("")
    if failures:
        print(f"FAILED: {failures} assertion(s)")
    else:
        print("all groups passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
