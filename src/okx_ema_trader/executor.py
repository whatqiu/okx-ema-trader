"""Auto-trading loop: consume strategy signals, place demo orders.

Builds on the existing read-only pieces (`history.fetch_candles`,
`indicators.calculate_indicators`, `strategy.evaluate_signal`) and adds the
write path via `broker.Broker`, with `state.Ledger` as the durable record of
what we hold.

Design decisions worth stating out loud:

  * The ledger, not memory, is the source of truth for "what am I holding". The
    in-memory `last_side` pattern used by `monitor.py` re-fires on restart; for
    orders that means a duplicate position.
  * `classify` (in strategy.py) returns an EVENT, not a state: a signal appears
    on the one bar where 5m EMA20 crosses EMA60, and is `None` on every other
    bar. `_classify` recovers WHY it is None so that "still warming up" is never
    mistaken for "the trend flipped".
  * A position is only closed when an OPPOSITE signal arrives. A signal going
    to None leaves the position alone (the user's chosen behaviour).
  * If the ledger and the exchange disagree, we stop. Auto-correcting an
    unknown state is how accounts get liquidated.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import http
from .broker import Broker, BrokerError
from .config import Config, TradingConfig, load_config
from .credentials import CredentialsError, load_demo_credentials
from .history import fetch_candles
from .indicators import calculate_indicators
from .state import Ledger, Position, StateError
from .strategy import (REASON_ADX, REASON_DEVIATION, REASON_NO_CROSS,
                       REASON_NO_ENV, REASON_WARMUP, Signal, classify)

ROOT = Path(__file__).parents[2]
CONFIG_PATH = ROOT / "config" / "config.yaml"
STATE_PATH = ROOT / "state.yaml"
LOG_PATH = ROOT / "logs" / "executor.log"

BAR_SECONDS = 300

# The REASON_* names come from strategy.py and are re-exported here so callers
# that import them from this module keep working. There is one source of truth.


def _classify(ind_5m: dict, ind_15m: dict, adx_min: float,
              deviation_max: float = 1.0) -> tuple[Signal | None, str]:
    """Evaluate the signal and, when there is none, say precisely why.

    Thin wrapper: the decision itself lives in `strategy.classify` so that the
    executor and the backtest cannot drift apart.
    """
    return classify(ind_5m, ind_15m, adx_min, deviation_max)


def _closed(candles: list[list[float]]) -> list[list[float]]:
    """Drop the still-forming candle, exactly as monitor.Monitor.closed does."""
    return candles[:-1] if len(candles) > 1 else candles


def seconds_until_next_bar() -> float:
    return (BAR_SECONDS - time.time() % BAR_SECONDS) + 8


def exposure_for(trading: TradingConfig, equity: float) -> float:
    """Position value in quote currency, before lot rounding.

    Leverage scales the exposure in BOTH modes, so `fixed: 100 @ 10x` risks 1000
    USDT of notional and `equity: 100% @ 10x` risks the entire account ten times
    over. That is the definition the CLI flags advertise.
    """
    if trading.sizing_mode == "equity":
        return equity * (trading.equity_pct / 100.0) * trading.leverage
    return trading.notional_usdt * trading.leverage


class Executor:
    def __init__(self, config: Config, broker: Broker, ledger: Ledger, *,
                 dry_run: bool = False, proxy: str | None = None) -> None:
        self.config = config
        self.trading = config.trading
        self.broker = broker
        self.ledger = ledger
        self.dry_run = dry_run
        self.proxy = proxy
        self._equity = 0.0

    # ------------------------------------------------------------ startup gate
    def reconcile(self) -> None:
        """Compare the ledger against the exchange. Any mismatch is fatal."""
        self._equity = self.broker.verify_demo()
        problems: list[str] = []

        for symbol in self.trading.symbols:
            booked = self.ledger.get(symbol)
            try:
                actual = self.broker.get_position(symbol)
            except BrokerError as exc:
                problems.append(f"{symbol}: could not read exchange position ({exc})")
                continue

            live_side = "flat"
            if actual is not None:
                raw_side = (actual.get("side") or "").lower()
                live_side = "long" if raw_side == "long" else "short"

            if live_side != booked.side:
                problems.append(
                    f"{symbol}: exchange says {live_side!r}, ledger says {booked.side!r}"
                )

        if problems:
            logging.critical("RECONCILIATION FAILED — refusing to trade:")
            for problem in problems:
                logging.critical("  %s", problem)
            logging.critical(
                "Resolve by hand: check `okx account positions`, then either flatten "
                "the position or edit %s to match. Nothing was changed automatically.",
                self.ledger.path,
            )
            raise SystemExit(2)

        logging.info("reconciled %d symbol(s) | equity=%.2f USDT",
                     len(self.trading.symbols), self._equity)

    # --------------------------------------------------------------- one cycle
    def poll(self) -> None:
        for symbol in self.trading.symbols:
            try:
                self._poll_symbol(symbol)
            except (BrokerError, StateError) as exc:
                # One bad symbol must not take down the other. Never place an
                # order we are unsure about; log and move on.
                logging.error("%s: %s", symbol, exc)

    def _poll_symbol(self, symbol: str) -> None:
        # Fetch once per bar: each REST call costs seconds through the proxy.
        series_5m = _closed(fetch_candles(symbol, self.config.bar_5m,
                                          self.config.candle_limit, self.proxy))
        series_15m = _closed(fetch_candles(symbol, self.config.bar_15m,
                                           self.config.candle_limit, self.proxy))
        ind_5m = calculate_indicators(series_5m, self.config.ema_fast,
                                      self.config.ema_slow, self.config.adx_period)
        ind_15m = calculate_indicators(series_15m, self.config.ema_fast,
                                       self.config.ema_slow, self.config.adx_period)

        signal, reason = _classify(ind_5m, ind_15m, self.config.adx_min,
                                   self.config.deviation_max)
        booked = self.ledger.get(symbol)

        if reason == REASON_WARMUP:
            logging.info("%s: warming up (%d/%d and %d/%d candles) — no action",
                         symbol, len(series_5m), self.config.candle_limit,
                         len(series_15m), self.config.candle_limit)
            return

        detail = ""
        if ind_5m and ind_15m:
            detail = ("5m ema20=%.4f ema60=%.4f adx=%.2f | 15m ema20=%.4f ema60=%.4f adx=%.2f"
                      % (ind_5m["ema_fast"], ind_5m["ema_slow"], ind_5m["adx"],
                         ind_15m["ema_fast"], ind_15m["ema_slow"], ind_15m["adx"]))
        logging.info("%s: %s | held=%s", symbol, detail or "no indicators", booked.side)

        if signal is None:
            if reason == REASON_NO_CROSS:
                # The ordinary case: this bar simply had no crossover. Since the
                # entry is an event, "no event" is not news — do not log it as a
                # state change and do not touch the position.
                logging.debug("%s: no 5m crossover — holding %s", symbol, booked.side)
                return
            # Signal decayed to None. The user's choice is to HOLD, not to exit.
            if booked.is_open:
                logging.info("%s: signal gone (%s) — holding %s, waiting for a reversal",
                             symbol, reason, booked.side)
            else:
                logging.info("%s: no signal (%s) — flat", symbol, reason)
            return

        if signal.side == booked.side:
            logging.debug("%s: %s signal unchanged — no action", symbol, signal.side)
            return

        logging.info("%s: %s -> %s (%s)", symbol, booked.side, signal.side, signal.reason)
        self._flip(symbol, signal, booked)

    # ----------------------------------------------------------------- actions
    def _flip(self, symbol: str, signal: Signal, booked: Position) -> None:
        """Close the old side (if any) and open the new one with its stop."""
        if booked.is_open:
            closed = self.broker.close_position(symbol)
            if not closed and not self.dry_run:
                # The exchange had nothing, but the ledger thought otherwise.
                # Stop rather than stack a new position on an unknown state.
                raise BrokerError(
                    f"{symbol}: ledger holds {booked.side} but the exchange shows nothing; "
                    f"refusing to open a new position until {self.ledger.path} is corrected"
                )
            self.broker.cancel_stops(symbol)
            self.ledger.clear(symbol)
            self.ledger.save()

        equity = self.broker.verify_demo() if not self.dry_run else self._equity
        exposure = exposure_for(self.trading, equity)

        projected = self.ledger.total_notional({}) + exposure
        if projected > self.trading.max_total_notional:
            logging.warning(
                "%s: skipping entry — %.2f USDT exposure would exceed the %.2f cap",
                symbol, projected, self.trading.max_total_notional,
            )
            return

        # Margin check. At full equity and 10x, one position consumes the entire
        # balance, so a second symbol would be rejected by the exchange. Catching
        # it here turns a confusing order rejection into a clear log line.
        required_margin = exposure / max(self.trading.leverage, 1)
        held_margin = self.ledger.total_notional({}) / max(self.trading.leverage, 1)
        free_margin = max(equity - held_margin, 0.0)
        if required_margin > free_margin:
            logging.warning(
                "%s: skipping entry — needs %.2f USDT margin but only %.2f is free "
                "(%.2f of %.2f already committed across %s)",
                symbol, required_margin, free_margin, held_margin, equity,
                ",".join(self.ledger.open_symbols()) or "nothing",
            )
            return

        # set_leverage returns what was actually applied: the configured value
        # can exceed an instrument's ceiling and gets clamped. Sizing off the
        # requested number instead would ask for a position the account cannot
        # support at the lower leverage.
        effective = self.broker.set_leverage(symbol, self.trading.leverage)
        fill = self.broker.market_entry(
            symbol, signal.side, exposure / max(self.trading.leverage, 1),
            effective, self.trading.stop_loss_pct,
            client_id=f"{symbol.split('-')[0]}{int(time.time())}",
        )
        self.ledger.set(symbol, Position(
            side=signal.side,
            entry_ts=int(time.time() * 1000),
            entry_price=fill.price,
            size=fill.size,
            entry_order_id=fill.order_id,
            stop_order_id=fill.stop_order_id,
            stop_price=fill.stop_price,
        ))
        self.ledger.save()
        logging.info("%s: holding %s | entry=%.6f size=%s stop=%.6f",
                     symbol, signal.side, fill.price, fill.size, fill.stop_price)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--state", default=str(STATE_PATH))
    parser.add_argument("--profile", default=None, help="OKX config.toml profile (must be demo)")
    parser.add_argument("--proxy", default=None,
                        help="HTTP proxy for both REST and ccxt, e.g. http://127.0.0.1:7890. "
                             "Default: the system proxy from Windows Internet Options. "
                             "Pass '' for a direct connection. "
                             "(The old default 10888 was a guess and is not this machine's port.)")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the full decision loop but place no orders")
    parser.add_argument("--once", action="store_true", help="one cycle, then exit")
    args = parser.parse_args()

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )

    config = load_config(args.config)
    trading = config.trading

    if not trading.enabled and not args.dry_run:
        logging.error(
            "trading is disabled. Set `trading.enabled: true` in %s, or use --dry-run "
            "to watch the decisions without placing orders.", args.config,
        )
        raise SystemExit(1)

    if args.dry_run:
        logging.warning("DRY RUN — no orders will be placed")

    logging.info("sizing: %s | leverage=%dx | stop=%.1f%% | symbols=%s",
                 f"{trading.notional_usdt} USDT fixed" if trading.sizing_mode == "fixed"
                 else f"{trading.equity_pct}% of equity",
                 trading.leverage, trading.stop_loss_pct, ",".join(trading.symbols))
    if trading.leverage > 1:
        logging.warning("leverage %dx: a ~%.1f%% adverse move liquidates a full-equity position",
                        trading.leverage, 100.0 / trading.leverage)

    try:
        credentials = load_demo_credentials(args.profile)
    except CredentialsError as exc:
        logging.error("credentials: %s", exc)
        raise SystemExit(1)

    # Resolve once: the REST candle calls and ccxt have to agree, and "" means
    # "direct" while None means "not decided yet".
    proxy = args.proxy if args.proxy else (http.system_proxy() if args.proxy is None else None)
    if proxy:
        logging.info("using proxy %s", proxy)

    broker = Broker(credentials, proxy=proxy, dry_run=args.dry_run)
    try:
        ledger = Ledger.load(Path(args.state))
    except StateError as exc:
        logging.error("state: %s", exc)
        raise SystemExit(1)

    executor = Executor(config, broker, ledger, dry_run=args.dry_run, proxy=proxy)
    try:
        executor.reconcile()
    except CredentialsError as exc:
        logging.error("credentials: %s", exc)
        raise SystemExit(1)

    while True:
        try:
            executor.poll()
        except Exception:
            logging.exception("cycle failed; will retry")
        if args.once:
            return
        try:
            time.sleep(seconds_until_next_bar())
        except KeyboardInterrupt:
            logging.info("stopped")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("stopped")
