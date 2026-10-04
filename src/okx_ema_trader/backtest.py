from __future__ import annotations

"""Signal statistics harness — the step that must come before any live order.

It answers exactly one question: does this EMA/ADX crossover make enough GROSS
money to pay OKX fees and slippage?

Reads public candles only. Never touches account endpoints, never needs an API
key. If the answer here is negative, no amount of AI wiring will save it.

Two assumptions are load-bearing and are surfaced as CLI flags rather than
hidden constants:
  * a signal is decided on bar N's CLOSE and filled at bar N+1's OPEN. Bar N's
    close is never used as a fill price — you cannot trade a price you only
    learned after it printed. If bar N+1 does not exist, the signal is dropped
    and no trade is booked;
  * every signal flips a fully invested position, so each flip pays costs on
    2 units of notional (exit the old side, enter the new one).

Anything relying on the future would make this look better than reality, so the
15m filter value used at bar N is the last 15m candle CLOSED at that moment,
never the forming one.
"""

import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import http
from .config import load_config
from .indicators import indicator_frame
from .strategy import evaluate_signal

# `/market/candles` only looks back ~1440 bars and returns an empty page the
# moment you pass `after`, which silently truncated a 60-day request to 5 days.
# `/market/history-candles` is the paginated endpoint that goes back years.
MAX_LIMIT = 300

MINUTES_PER_BAR = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240}
DEFAULT_CONFIG_PATH = Path(__file__).parents[2] / "config" / "config.yaml"


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------
def fetch_history(symbol: str, bar: str, target: int, pause: float = 0.15,
                  proxy: str | None = None) -> list[list[float]]:
    """Fetch up to `target` CONFIRMED candles oldest-first, paging backwards.

    `proxy` is accepted because ccxt-style env vars are not honoured by urllib
    on every platform; the console passes the proxy the user configured.

    The live bot only ever holds `candle_limit` bars, which is far too little to
    backtest on: 200 x 5m is under 17 hours. OKX returns newest-first and caps a
    page at MAX_LIMIT, so we walk `after` (strictly older than this timestamp)
    until we have enough.
    """
    raw: list[list] = []
    after: str | None = None
    while len(raw) < target:
        want = min(MAX_LIMIT, target - len(raw))
        params: dict[str, str] = {"instId": symbol, "bar": bar, "limit": str(want)}
        if after is not None:
            params["after"] = after
        # `http.get_json` owns the proxy and the shared 20-req/2s budget. The old
        # fixed `pause` was ~13 req/s on its own — over the limit — and did not
        # know about the console's requests at all.
        page = http.get_json("/market/history-candles", params, proxy=proxy, timeout=15)
        if pause > 0:
            time.sleep(pause)
        if not page:
            break
        raw = page + raw
        after = str(page[-1][0])  # oldest row of this page -> next page goes further back
        if len(page) < want:
            break

    seen: dict[int, list] = {}
    for row in raw:
        seen.setdefault(int(row[0]), row)
    rows = [seen[key] for key in sorted(seen)]
    # index 8 is `confirm`; an unfinished bar is not something you could have traded.
    confirmed = [row for row in rows if len(row) >= 9 and row[8] == "1"]
    return [[float(row[index]) for index in range(6)] for row in confirmed]


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Trade:
    """One round trip, with the signal time and the fill time kept APART.

    entry_signal_ts  : the 5m bar whose close produced the signal
    entry_exec_ts    : the 5m bar whose open filled it (signal bar + 1)
    entry_price      : that open. Never the signal bar's close.

    For a stop exit there is no separate signal bar — the stop is detected on
    the bar that touched it, so signal_ts == exec_ts there.
    """
    side: str
    entry_signal_ts: int
    entry_exec_ts: int
    entry_price: float
    exit_signal_ts: int
    exit_exec_ts: int
    exit_price: float
    gross_return: float  # fraction of notional, before costs
    exit_reason: str = "signal"  # "signal" | "stop" | "forced"


@dataclass(frozen=True)
class Report:
    trades: list[Trade]
    equity: np.ndarray
    cost_per_unit_bps: float
    turnover: float
    stopped_out: int = 0
    unfilled_signals: int = 0  # signals dropped because no next bar existed

    @property
    def gross_return(self) -> float:
        return float(sum(trade.gross_return for trade in self.trades))

    @property
    def total_turnover(self) -> float:
        """Units of notional traded: first entry 1, each flip 2, forced exit 1."""
        return self.turnover

    @property
    def total_cost(self) -> float:
        return self.total_turnover * self.cost_per_unit_bps / 10_000.0

    @property
    def net_return(self) -> float:
        return self.gross_return - self.total_cost

    @property
    def breakeven_bps(self) -> float:
        """Cost per unit (fee + slippage) that would erase the entire edge."""
        if self.total_turnover == 0:
            return 0.0
        return self.gross_return / self.total_turnover * 10_000.0

    def equity_index(self, exposure: float = 1.0) -> np.ndarray:
        """The account, expressed as a multiple of its starting value.

        `equity` is cumulative P&L in units of NOTIONAL — it starts at 0 and
        its peak is a profit figure, not capital. It is a P&L ledger, not an
        equity curve, and treating it as one is what produced a "284% maximum
        drawdown": a 23% swing divided by an 8% profit peak. To talk about
        drawdown we need the account: `1 + P&L x exposure`, where `exposure`
        is how many units of notional one unit of account equity is risking.
        """
        return 1.0 + self.equity * exposure

    def max_drawdown(self, exposure: float = 1.0) -> float:
        """Largest peak-to-trough fall of the account, in [0, 1].

        `exposure` = notional risked per unit of account equity. At
        `equity_pct=100` and `leverage=10` that is 10, so a 10% adverse move on
        the position erases the account. The default 1.0 is the unleveraged
        reading: one unit of notional against one unit of capital.
        """
        index = self.equity_index(exposure)
        if index.size == 0:
            return 0.0
        peak = np.maximum.accumulate(index)
        # An index at or below zero means the account is gone. That is a 100%
        # drawdown, not an unbounded one — the leverage is already in `exposure`
        # and must not be applied a second time here.
        drawdowns = np.where(index > 0, (peak - index) / np.maximum(peak, 1e-12), 1.0)
        return float(min(drawdowns.max(), 1.0))

    def wiped_out(self, exposure: float) -> bool:
        """Did the account hit zero anywhere in the run at this exposure?"""
        return bool((self.equity_index(exposure) <= 0).any())

    def ruin_exposure(self) -> float:
        """The smallest exposure that would have emptied the account.

        Useful because it is a property of the P&L stream alone: `1 / |trough|`.
        At 100% equity it maps directly to "the leverage that kills you".
        """
        trough = float(self.equity.min()) if self.equity.size else 0.0
        if trough >= 0:
            return float("inf")
        return 1.0 / abs(trough)


def simulate(
    frame_5m,
    frame_15m,
    adx_min: float,
    deviation_max: float,
    cost_per_unit_bps: float,
    lookahead_ms: int,
    stop_loss_pct: float,
) -> Report:
    """Replay the live strategy by CALLING it, not by re-describing it.

    Every entry decision comes from `strategy.evaluate_signal` — the same
    function the executor runs. The only things added here are what a backtest
    must model and a live fill does not: costs, the stop loss, and the one-bar
    delay between seeing a signal and being able to trade it.

    Per bar N, in this order:

      1. fill whatever bar N-1 decided, at bar N's OPEN;
      2. check the stop against bar N's high/low (conservative);
      3. decide on bar N's CLOSE -> an order that can only fill on bar N+1.

    A signal produced on the final bar therefore never fills.
    """
    ts_5m = frame_5m["ts"].to_numpy()
    ts_15m = frame_15m["ts"].to_numpy()
    opens = frame_5m["open"].to_numpy()
    highs = frame_5m["high"].to_numpy()
    lows = frame_5m["low"].to_numpy()
    # For a 5m bar starting at S we decide at its close S+5m, so a 15m bar is
    # only usable if it finished by then: ts_15m + 15m <= S + 5m.
    usable_until = ts_5m + lookahead_ms

    cost = cost_per_unit_bps / 10_000.0
    trades: list[Trade] = []
    equity_curve: list[float] = []
    equity = 0.0
    turnover = 0.0
    position = 0  # -1 short, 0 flat, +1 long
    entry_price = 0.0
    entry_signal_ts = 0
    entry_exec_ts = 0
    stop_price = 0.0
    stopped_out = 0
    unfilled = 0
    pending: tuple[str, int] | None = None  # (side, bar index that signalled it)

    # Start at 1: a crossover needs the previous bar, so bar 0 can never enter.
    for index in range(1, len(frame_5m)):
        # --- 1) Fill the previous bar's decision at THIS bar's open. The signal
        # bar's close is information you only had after it printed; it is not a
        # price you could have been filled at.
        if pending is not None:
            side_name, signal_index = pending
            pending = None
            side = 1 if side_name == "long" else -1
            fill = float(opens[index])

            if position != 0 and side != position:
                # Reverse the book: exit the old side, enter the new one.
                gross = (fill - entry_price) / entry_price * position
                equity += gross - cost
                turnover += 1.0
                trades.append(Trade(
                    "long" if position == 1 else "short",
                    int(entry_signal_ts), int(entry_exec_ts), float(entry_price),
                    int(ts_5m[signal_index]), int(ts_5m[index]), fill,
                    float(gross), "signal"))
                equity_curve.append(equity)
                position = 0

            if position == 0:
                position = side
                entry_price = fill
                entry_signal_ts = int(ts_5m[signal_index])
                entry_exec_ts = int(ts_5m[index])
                stop_price = (entry_price * (1 - stop_loss_pct / 100.0) if side == 1
                              else entry_price * (1 + stop_loss_pct / 100.0))
                equity -= cost
                turnover += 1.0
                equity_curve.append(equity)
            # side == position: the order is redundant, drop it (no repeat entry).

        # --- 2) Stop loss. Conservative by construction: if this bar's range
        # spans the stop we assume the stop was hit, even though the bar closed
        # somewhere else. We never assume a favourable intrabar order.
        if position != 0:
            low, high = float(lows[index]), float(highs[index])
            open_ = float(opens[index])
            hit = low <= stop_price if position == 1 else high >= stop_price
            if hit:
                # A gap through the stop fills at the open, not at the stop price.
                fill = min(stop_price, open_) if position == 1 else max(stop_price, open_)
                gross = (fill - entry_price) / entry_price * position
                equity += gross - cost
                turnover += 1.0
                # A stop has no separate signal bar: it is detected on the bar
                # that touched it, so signal time and execution time coincide.
                trades.append(Trade(
                    "long" if position == 1 else "short",
                    int(entry_signal_ts), int(entry_exec_ts), float(entry_price),
                    int(ts_5m[index]), int(ts_5m[index]), fill, float(gross), "stop"))
                equity_curve.append(equity)
                position = 0
                stopped_out += 1

        # --- 3) Entry decision. Closed 5m bar, and the 15m bar closed by then.
        cutoff = usable_until[index] - 15 * 60_000
        j = int(np.searchsorted(ts_15m, cutoff, side="right")) - 1
        if j < 1:
            continue  # not enough 15m history yet

        row_5m = frame_5m.iloc[index]
        prev_5m = frame_5m.iloc[index - 1]
        row_15m = frame_15m.iloc[j]
        if np.isnan(row_15m["adx"]):
            continue
        if np.isnan(row_5m["ema_fast"]) or np.isnan(row_5m["ema_slow"]):
            continue
        if np.isnan(prev_5m["ema_fast"]) or np.isnan(prev_5m["ema_slow"]):
            continue

        signal = evaluate_signal(
            {
                "close": float(row_5m["close"]),
                "ema_fast": float(row_5m["ema_fast"]),
                "ema_slow": float(row_5m["ema_slow"]),
                "prev_ema_fast": float(prev_5m["ema_fast"]),
                "prev_ema_slow": float(prev_5m["ema_slow"]),
            },
            {
                "close": float(row_15m["close"]),
                "ema_fast": float(row_15m["ema_fast"]),
                "ema_slow": float(row_15m["ema_slow"]),
                "adx": float(row_15m["adx"]),
            },
            adx_min,
            deviation_max,
        )
        if signal is None:
            continue

        side = 1 if signal.side == "long" else -1
        if side == position:
            continue  # already holding it; a repeat is not an entry

        # Queue it. It can only be filled on the NEXT bar's open; if there is no
        # next bar it is simply never booked.
        pending = (signal.side, index)

    if pending is not None:
        unfilled = 1  # signal on the final bar: no bar N+1 to fill it on

    # A position still open at the last bar has never been realised. Leaving it
    # out would flatter or sink the result depending on luck, so force-liquidate
    # at the last close (clearly labelled "forced", it is not a signal exit).
    if position != 0:
        last_close = float(frame_5m["close"].iloc[-1])
        last_ts = int(ts_5m[-1])
        gross = (last_close - entry_price) / entry_price * position
        equity += gross - cost
        turnover += 1.0
        trades.append(Trade("long" if position == 1 else "short",
                            int(entry_signal_ts), int(entry_exec_ts), float(entry_price),
                            last_ts, last_ts, last_close, float(gross), "forced"))
        equity_curve.append(equity)

    if not equity_curve:
        return Report(trades, np.array([0.0]), cost_per_unit_bps, turnover,
                      stopped_out, unfilled)
    return Report(trades, np.array(equity_curve), cost_per_unit_bps, turnover,
                  stopped_out, unfilled)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
# Timestamps are printed in UTC+8 so they line up with what the exchange UI and
# the exported CSV show.
TZ = timezone(timedelta(hours=8))


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, TZ).strftime("%m-%d %H:%M")


def print_trades(trades: list[Trade], limit: int) -> None:
    """The per-trade ledger: signal time and fill time are never collapsed."""
    shown = trades[-limit:] if limit > 0 else trades
    print("")
    print("  entry_signal_time  entry_execution_time  entry_price   "
          "exit_signal_time  exit_execution_time  exit_price   exit_reason  ret")
    print("  " + "-" * 112)
    for n, trade in enumerate(shown, start=1):
        print(f"  {fmt_ts(trade.entry_signal_ts):<18}  {fmt_ts(trade.entry_exec_ts):<20}"
              f"  {trade.entry_price:<12.4f}  {fmt_ts(trade.exit_signal_ts):<16}"
              f"  {fmt_ts(trade.exit_exec_ts):<19}  {trade.exit_price:<11.4f}"
              f"  {trade.exit_reason:<11}  {trade.gross_return:+.2%}"
              f"   [{trade.side} #{n}]")
    if limit > 0 and len(trades) > limit:
        print(f"  (showing the last {limit} of {len(trades)}; --show-trades 0 for all)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure whether the EMA/ADX signal beats its own costs.")
    parser.add_argument("--days", type=int, default=60, help="how much 5m history to pull (default: 60)")
    parser.add_argument("--symbol", default=None, help="override symbol from config.yaml")
    parser.add_argument(
        "--proxy",
        default=None,
        help="HTTP proxy, e.g. http://127.0.0.1:7890. Default: the system proxy "
             "from Windows Internet Options, or a direct connection if none is set.",
    )
    parser.add_argument(
        "--fee-bps",
        type=float,
        default=5.0,
        help="taker fee per side in bps (default: 5.0 ~ OKX swap VIP0 taker; set 0 to see gross edge)",
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=3.0,
        help="assumed slippage per side in bps (default: 3.0)",
    )
    parser.add_argument(
        "--show-trades",
        type=int,
        default=20,
        help="how many round trips to list in detail (default: 20, 0 = all)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(DEFAULT_CONFIG_PATH)
    symbol = args.symbol or config.symbol
    cost_per_unit_bps = args.fee_bps + args.slippage_bps
    proxy = args.proxy or http.system_proxy()
    if proxy:
        print(f"using proxy {proxy}")

    bars_needed = args.days * (24 * 60 // MINUTES_PER_BAR[config.bar_5m])
    print(f"fetching {bars_needed} {config.bar_5m} candles for {symbol} ...")
    candles_5m = fetch_history(symbol, config.bar_5m, bars_needed, proxy=proxy)
    candles_15m = fetch_history(symbol, config.bar_15m, bars_needed // 3 + 400, proxy=proxy)
    print(f"got {len(candles_5m)} x {config.bar_5m}, {len(candles_15m)} x {config.bar_15m} (confirmed only)")

    if len(candles_5m) < max(config.ema_slow, config.adx_period * 2):
        raise SystemExit("not enough history to warm up indicators")

    frame_5m = indicator_frame(candles_5m, config.ema_fast, config.ema_slow, config.adx_period)
    frame_15m = indicator_frame(candles_15m, config.ema_fast, config.ema_slow, config.adx_period)
    lookahead_ms = MINUTES_PER_BAR[config.bar_5m] * 60_000

    report = simulate(frame_5m, frame_15m, config.adx_min, config.deviation_max,
                      cost_per_unit_bps, lookahead_ms, config.trading.stop_loss_pct)

    print("")
    print("=" * 58)
    print(f"  {symbol}  EMA{config.ema_fast}/{config.ema_slow}  ADX>{config.adx_min}  last {args.days}d")
    print(f"  deviation<={config.deviation_max:.2%} on 15m   stop={config.trading.stop_loss_pct:.1f}%")
    print("=" * 58)
    if not report.trades:
        print("  no completed round trips — strategy stayed flat the whole window")
        return

    wins = [t for t in report.trades if t.gross_return > 0]
    print(f"  trades            : {len(report.trades)}")
    print(f"  win rate          : {len(wins) / len(report.trades):.1%}")
    print(f"  gross return      : {report.gross_return:+.2%}  (before costs)")
    print(f"  turnover          : {report.total_turnover:.0f} units")
    print(f"  assumed cost      : {cost_per_unit_bps:.1f} bps per unit (fee {args.fee_bps} + slip {args.slippage_bps})")
    print(f"  net return        : {report.net_return:+.2%}")
    print(f"  stopped out       : {report.stopped_out} of {len(report.trades)} exits")
    print(f"  unfilled signals  : {report.unfilled_signals} (no next bar to fill on)")
    print(f"  expectancy/trade  : {report.net_return / len(report.trades) * 10_000:+.1f} bps")
    print(f"  max drawdown (1x) : {report.max_drawdown(1.0):.2%}  (名义本金口径，未计杠杆)")
    print(f"  BREAKEVEN cost    : {report.breakeven_bps:.1f} bps per unit")
    print("=" * 58)

    # Drawdown at the leverage and sizing the live bot would actually use. This
    # is the number that matters: the 1x figure above describes the signal, this
    # one describes what it does to the account.
    trading = config.trading
    if trading.sizing_mode == "equity":
        exposure = trading.equity_pct / 100.0 * trading.leverage
        ruined = report.wiped_out(exposure)
        print(f"  account exposure  : {exposure:g}x  "
              f"({trading.equity_pct:g}% equity @ {trading.leverage}x)")
        print(f"  max drawdown      : {report.max_drawdown(exposure):.2%}  (账户口径)")
        print(f"  account wiped out : {'YES — 这轮会爆仓' if ruined else 'no'}")
        print(f"  ruin leverage     : {report.ruin_exposure():.2f}x  "
              f"(满仓时超过这个杠杆就会归零)")
    else:
        print("  max drawdown      : 需要 sizing_mode: equity 才能换算到账户口径；"
              "fixed 模式下不知道账户权益。")
    print("=" * 58)
    verdict = "edge survives realistic costs" if report.net_return > 0 else "NO EDGE — costs eat everything"
    print(f"  verdict           : {verdict}")

    # ---- READ THE VERDICT WITH CARE ------------------------------------
    # `net_return > 0` is a SIGN test, not a proof. It answers "did this exact
    # 30-day window happen to come out positive", which is a much weaker claim
    # than "this strategy has an edge".
    #
    # Sample size is the thing that is quietly missing from the verdict above.
    # Rules of thumb for a directional intraday strategy:
    #     < 30 trades  : noise. A profitable run is luck, full stop.
    #    30-100 trades : suggestive, not conclusive.
    #   100-300 trades: worth acting on.
    #        300+     : the only number you can size real money with.
    #
    # The 30-day MU run produced 14 trades, 8 of them stops. Whatever that
    # window says, it cannot establish the absence of an edge — only that
    # 14 trades were not enough to demonstrate one. And note the asymmetry
    # that makes this dangerous: with 14 trades you can ALSO tune a parameter
    # until the backtest goes green. That is overfitting, not discovery.
    #
    # breakeven_bps is the more honest number, because it is independent of
    # the sample size. Read it in three tiers:
    #     strongly positive  -> real edge, comfortably above costs
    #     small positive     -> edge exists but is thinner than your costs
    #     NEGATIVE           -> the gross PnL is itself negative. The direction
    #                           is wrong. No fee reduction can fix this; only
    #                           changing the logic can.
    n = len(report.trades)
    if report.net_return > 0 and n < 30:
        print(f"  ⚠ WARNING        : 只有 {n} 笔交易（<30）。正收益在这个样本量下")
        print(f"                     可能是运气而非 edge，不能据此判断策略有效。")
    if report.breakeven_bps < 0:
        print(f"  ⚠ 诊断           : breakeven 为负 ({report.breakeven_bps:.1f} bps)，")
        print(f"                     说明毛利本身是负的 —— 不是被手续费吃掉，是方向错了。")
        print(f"                     降费用救不了，只能改逻辑。")
    elif 0 <= report.breakeven_bps < 20:
        print(f"  ⚠ 诊断           : breakeven 仅 {report.breakeven_bps:.1f} bps，edge 很薄，")
        print(f"                     略高的手续费或滑点就会把它吃掉。")

    # ---- WHERE THE LOSSES ACTUALLY COME FROM ----------------------------
    # "NO EDGE" is a summary, and summaries hide structure. Count the exit
    # reasons before concluding the DIRECTION is wrong.
    #
    # The MU 30-day run (14 trades, 3 wins / 11 losses) is the example to read
    # carefully, because the aggregate verdict is misleading:
    #
    #     average WIN   +6.14%      <- substantial; the signal finds real trends
    #     average LOSS  -2.31%      <- small; capped by a 3% stop
    #     payoff ratio   0.72       <- 3/14 wins is too low to pay for that
    #
    # 8 of the 11 losses printed exactly -3.00%: they did not lose because the
    # thesis was wrong, they were taken out at the stop while the thesis was
    # still working. A 3% stop on a 5m EMA crossover sits INSIDE normal
    # intrabar noise — the question is not "is the stop profitable" but "is the
    # stop wider than the noise the entry needs to clear".
    #
    # So the first thing to test is the stop, not the signal. Widening the stop
    # trades one known cost (more losses) against an unmeasured one (whether the
    # moves that stop currently eats would have continued in your favour).
    # Counting the exits is how you tell those two apart; net PnL cannot.
    if report.trades:
        stops = [t for t in report.trades if t.exit_reason == "stop"]
        if stops:
            share = len(stops) / len(report.trades)
            tag = "  <- 多数亏损来自止损，方向未必是主因" if share > 0.4 else ""
            print(f"  止损占比         : {len(stops)}/{len(report.trades)} ({share:.0%}){tag}")
            if share > 0.4:
                print(f"                     先怀疑止损宽度，再怀疑信号方向。")

    print_trades(report.trades, args.show_trades)
    print("")
    print("  Note: a signal is decided on bar N's close and filled at bar N+1's open.")
    print("  entry_signal_time / exit_signal_time = the bar that produced the decision;")
    print("  entry_execution_time / exit_execution_time = the bar that filled it.")


if __name__ == "__main__":
    main()
