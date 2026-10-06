"""账户级熔断：让 executor（唯一会真下单的那条路径）在亏到一定程度时停手。

为什么需要单独一层：

  * 交易所侧止损（`broker.market_entry(..., stop_loss_pct=3.0)`）保护的是
    "这一笔交易"，不是"这个账户"。3% 止损 × 10x 满仓 = 单笔亏掉 30% 权益，
    而止损成交后的下一根 K 线，策略完全可以再开一笔——交易所不会拦你。
  * `max_total_notional` 和保证金检查保护的是"单笔开得太大"，它们同样不看
    累计盈亏。

三条线，任意一条触发即停手（halt）：

  1. 账户回撤：从历史最高权益起算，达到 `max_drawdown_pct` 就平掉全部持仓
     并停手。
  2. 连亏：连续 N 笔平仓亏损。
  3. 单仓浮亏：单个仓位的浮亏达到权益的 `max_position_loss_pct`。这一条通常
     意味着止损没挂上或者跳空穿了——止损本身失效，继续跑没有意义。

停手而不是只平仓：只平仓不停手会让机器人平完立刻按同一套逻辑再开一笔，反
复摩擦手续费（platform 那边已经踩过）。halt 之后不再开新仓，直到人工解除。

命名上的教训（来自 platform/backend/health.py）：那边这个阈值原本叫
`max_daily_loss_pct`，但代码里没有任何按日复位的逻辑，名字在骗人——2026-10
已改名为 `max_drawdown_pct`。这里同样叫 `max_drawdown_pct`，并且基准
（peak_equity）是持久化的——它真的不会按天或按重启复位。

两边的阈值数字故意不一样，不是漏改：platform 自动交易是 5x + 固定 100 USDT
名义，一次止损约 3 USDT，账户回撤 25% 就已经是事故；这里是 10x + 100% 权益，
一次止损就是 30% 权益，25% 会在第一次正常止损时误触发。同一个数字放进两套
敞口里，必然有一边是在噪声上刹车。见 platform/backend/main.py 的 RISK_LIMITS。

盈亏基准怎么定的：equity 一律从 `broker.verify_demo()` 拿（`fetch_balance`
的 USDT total，含未实现盈亏和已扣的手续费），而不是自己拿仓位和价格去算。
理由：自己算要处理合约面值换算和资金费，多一处算错就是多一处假信号；而
demo 盘的 verify_demo() 本来每轮都会被调用，多一次调用的成本低于一个错误
的基准。
"""
from __future__ import annotations

import logging

from dataclasses import dataclass

from .broker import BrokerError
from .config import RiskConfig, TradingConfig
from .state import Ledger, Position, RiskState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuardLimits:
    """熔断阈值。都是相对账户权益的百分比／笔数；<= 0 表示这一条不启用。"""

    # 自历史最高权益起算的累计回撤上限（%）。注意：不是"单日"，没有任何按
    # 天或按重启复位的逻辑。
    #
    # 为什么默认 60：满仓 10x + 3% 止损下，一次止损就是 30% 权益。60% 等于
    # 允许吃两次，在第三次之前停手——一次止损就停手对这套参数太紧，而三次
    # 之后账户只剩 0.7³ ≈ 34%，已经没什么可救的了。想要更紧就在 config.yaml
    # 的 risk 段写 30，不用改代码。
    max_drawdown_pct: float = 60.0
    # 连续亏损笔数上限。平仓时才知道盈亏，所以这个计数由 record_close 维护。
    max_consecutive_losses: int = 3
    # 单个仓位浮亏占权益的上限（%）。
    max_position_loss_pct: float = 30.0

    @classmethod
    def for_trading(cls, trading: TradingConfig,
                    risk: RiskConfig | None = None) -> "GuardLimits":
        """按实际暴露推导阈值；config 里显式配了就以 config 为准。

        单仓阈值默认跟着配置走：一次止损亏掉的权益 = stop_loss_pct × leverage
        （3% × 10x = 30%），写死 30 在 1% × 2x 的配置下等于十五次止损都拦不
        住。上限压到 90：一个仓位最多亏掉全部权益，超过 100% 的阈值永远触发
        不了，那是静默失效，不是宽松。

        None（没配）和 0（配了但关掉）必须区分，所以只在 is not None 时覆盖
        ——写 0 得真的能把这一条关掉。
        """
        defaults = cls()
        one_stop = min(trading.stop_loss_pct * trading.leverage, 90.0)
        return cls(
            max_drawdown_pct=defaults.max_drawdown_pct
            if risk is None or risk.max_drawdown_pct is None else risk.max_drawdown_pct,
            max_consecutive_losses=defaults.max_consecutive_losses
            if risk is None or risk.max_consecutive_losses is None else risk.max_consecutive_losses,
            max_position_loss_pct=one_stop
            if risk is None or risk.max_position_loss_pct is None else risk.max_position_loss_pct,
        )


@dataclass(frozen=True)
class RiskDecision:
    """check() / record_close() 的回答：平哪些仓、停不停手、为什么。"""

    halted: bool = False
    close_symbols: tuple[str, ...] = ()
    reason: str = ""

    @property
    def triggered(self) -> bool:
        return bool(self.reason)


def drawdown_pct(equity: float, peak: float) -> float:
    """自峰值起算的回撤百分比。峰值还没建立时是 0，不是 100。"""
    return (peak - equity) / peak * 100.0 if peak > 0 else 0.0


def realized_pnl(broker, symbol: str, position: Position,
                 exit_price: float) -> float | None:
    """一笔仓位的已实现盈亏（计价货币；含价格变动，不含手续费与资金费）。

    走 `notional_for_contracts` 而不是 `size * (exit - entry)`：一张合约不等
    于一个币（BTC-USDT-SWAP 是 0.01 BTC），直接相乘会高估 100 倍。

    算不出来时返回 None 而不是 0.0：0 会被当成"不赚不赔"从而把连亏计数清
    零，那比干脆不记账更糟。
    """
    if not position.is_open or position.size <= 0:
        return None
    if position.entry_price <= 0 or exit_price <= 0:
        return None
    try:
        entered = broker.notional_for_contracts(symbol, position.size, position.entry_price)
        exited = broker.notional_for_contracts(symbol, position.size, exit_price)
    except BrokerError as exc:
        log.warning("%s: 换算盈亏失败（%s）——本次不记账", symbol, exc)
        return None
    return exited - entered if position.side == "long" else entered - exited


class RiskGuard:
    """账户级熔断。每轮 poll 开头 check() 一次，平仓后 record_close() 一次。"""

    def __init__(self, limits: GuardLimits, broker, ledger: Ledger) -> None:
        self.limits = limits
        self.broker = broker
        self.ledger = ledger

    @property
    def risk(self) -> RiskState:
        return self.ledger.risk

    # ---------------------------------------------------------------- 每轮检查
    def check(self) -> RiskDecision:
        """这一轮要不要平仓、要不要停手。

        读不到权益时（网络／交易所出错）这里把异常抛出去，由 Executor.poll
        外层兜住，结果是这一轮一笔都不下。宁可错过一根 K 线，也不要在不知
        道自己亏了多少的时候继续下单。
        """
        # 熔断是粘性的：重启不解除，只有人能解除。
        if self.risk.halted:
            return RiskDecision(
                halted=True,
                reason=self.risk.halt_reason or "halted",
                close_symbols=tuple(self.ledger.open_symbols()),
            )

        equity = self.broker.verify_demo()
        self._raise_peak(equity)

        peak = self.risk.peak_equity
        drawdown = drawdown_pct(equity, peak)
        if self.limits.max_drawdown_pct > 0 and drawdown >= self.limits.max_drawdown_pct:
            return self._halt(
                f"账户回撤 {drawdown:.1f}% >= 上限 {self.limits.max_drawdown_pct:.1f}%"
                f"（峰值 {peak:.2f} -> 现在 {equity:.2f} USDT）", flatten=True)

        streak = self.risk.consecutive_losses
        if self.limits.max_consecutive_losses > 0 and streak >= self.limits.max_consecutive_losses:
            return self._halt(
                f"连亏 {streak} 笔 >= 上限 {self.limits.max_consecutive_losses} 笔", flatten=True)

        condemned: list[str] = []
        for symbol in self.ledger.open_symbols():
            position = self.ledger.get(symbol)
            try:
                live = self.broker.get_position(symbol)
            except BrokerError as exc:
                # 读不到就跳过这一个，别把它当成"交易所说没有"。
                log.warning("%s: 读不到交易所仓位，本轮跳过该仓的风控检查（%s）", symbol, exc)
                continue

            if live is None:
                booked = self._book_remote_close(symbol, position)
                if booked is not None and booked.halted:
                    # 记账本身就踩到了连亏上限。这里返回空 close_symbols：交易所
                    # 已经没有这个仓位了，其余持仓留给下一轮——那时 check() 走
                    # halted 分支，会一次性平掉。
                    return RiskDecision(halted=True, reason=booked.reason)
                continue

            unrealized = float(live.get("unrealizedPnl") or 0.0)
            loss_pct = -unrealized / equity * 100.0 if equity > 0 else 0.0
            if self.limits.max_position_loss_pct > 0 and loss_pct >= self.limits.max_position_loss_pct:
                condemned.append(symbol)
                log.error("%s: 单仓浮亏 %.1f%% >= 上限 %.1f%%（止损可能没生效）",
                          symbol, loss_pct, self.limits.max_position_loss_pct)

        if condemned:
            # 只平点名的那几笔；但既然是"止损失效"级别的事件，同时停手——
            # 让人看一眼比让它继续按同一套逻辑下单重要。
            return self._halt(f"单仓浮亏超限：{','.join(condemned)}",
                              flatten=False, close_symbols=tuple(condemned))

        return RiskDecision()

    # ------------------------------------------------------------------ 平仓后
    def record_close(self, pnl: float) -> RiskDecision:
        """一笔平仓已实现，记账并判断是否立刻停手。

        必须在"平仓之后、开新仓之前"调用：_flip 是平掉反手再开，顺序反了，
        熔断挡住的就永远是下一笔而不是这一笔。
        """
        if pnl < 0:
            self.risk.consecutive_losses += 1
        else:
            # 与 platform/backend/health.py 的 consecutive_losses 保持一致：
            # 只有真正的盈利才清零，不赚不赔的一笔不算赢（它同样交了手续费）。
            self.risk.consecutive_losses = 0
        self.ledger.save()

        streak = self.risk.consecutive_losses
        log.info("平仓结算 pnl=%.2f USDT | 连亏 %d/%d", pnl, streak,
                 self.limits.max_consecutive_losses)
        if self.limits.max_consecutive_losses > 0 and streak >= self.limits.max_consecutive_losses:
            return self._halt(
                f"连亏 {streak} 笔 >= 上限 {self.limits.max_consecutive_losses} 笔", flatten=True)
        return RiskDecision()

    # -------------------------------------------------------------------- 内部
    def _raise_peak(self, equity: float) -> None:
        """回撤基准只增不减，并且立刻落盘。

        立刻落盘是为了让"回撤"跨重启连续：进程崩掉之后重新启动，不该因为基
        准丢了而重新获得一整份亏损额度。
        """
        if equity <= self.risk.peak_equity:
            return
        self.risk.peak_equity = equity
        self.ledger.save()
        log.info("回撤基准（历史最高权益）更新为 %.2f USDT", equity)

    def _book_remote_close(self, symbol: str, position: Position) -> RiskDecision | None:
        """账本还记着、交易所已经没了 —— 把这笔的盈亏结算进计数。

        返回 record_close 的结果（已结算时）或 None（跳过时）。调用方必须看
        这个返回值：结算本身就可能踩到连亏上限，不看等于把熔断吞掉。

        为什么必须处理：3% × 10x 满仓时，绝大多数出场是交易所把止损打掉，
        而不是策略反向信号触发 _flip。只统计 _flip 里的平仓，连亏计数会几乎
        永远是 0，熔断等于没有。

        为什么不动账本：executor 的原则是"交易所与账本不一致就停下来等人"。
        交易所说没有仓位，可能是止损成交，也可能是我们读错了；自动把账本清
        成 flat 会让下一次信号再开一笔，在真有仓位的情况下变成叠加持仓。所
        以只记账（settled 保证同一笔只结算一次），不平账，不一致留给人工
        —— 启动时 reconcile() 本来就会报这个错。
        """
        if position.settled:
            return None
        try:
            exit_price = self.broker.price(symbol)
        except BrokerError as exc:
            # 止损价是我们对出场价唯一的先验，而且挂出去的就是这个价。
            log.warning("%s: 读不到平仓价（%s），改用止损价估算", symbol, exc)
            exit_price = position.stop_price

        pnl = realized_pnl(self.broker, symbol, position, exit_price)
        if pnl is None:
            return None

        position.settled = True
        self.ledger.set(symbol, position)
        log.warning("%s: 交易所已无仓位（账本仍记 %s）——按 %.6f 结算盈亏 %.2f USDT；"
                    "账本未自动修改，请人工核对 %s",
                    symbol, position.side, exit_price, pnl, self.ledger.path)
        return self.record_close(pnl)

    def _halt(self, reason: str, *, flatten: bool,
              close_symbols: tuple[str, ...] = ()) -> RiskDecision:
        self.risk.halted = True
        self.risk.halt_reason = reason
        # 落盘是熔断的一部分：写不进去就等于没熔断。
        self.ledger.save()
        log.error("RISK HALT — %s", reason)
        symbols = tuple(self.ledger.open_symbols()) if flatten else close_symbols
        return RiskDecision(halted=True, reason=reason, close_symbols=symbols)
