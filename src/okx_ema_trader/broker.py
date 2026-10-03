"""Thin ccxt wrapper for placing demo swap orders with an attached stop.

Deliberately small: this module knows how to size, enter, protect, and exit one
position. It does NOT decide when to trade — `executor.py` owns that.

Three things here are load-bearing:

  1. `proxies` must be passed explicitly. ccxt does not honour HTTP_PROXY from
     the environment, and without this every call times out against okx.com.
  2. sandbox is forced on in `__init__` and re-asserted in `verify_demo()`. The
     credentials layer already refuses non-demo profiles; this is the second
     lock, so a bug in the first cannot reach a funded account.
  3. every entry places its stop in the SAME order request, then reads the order
     back to confirm the stop exists. A position that opened without a stop is
     closed immediately — holding an unprotected position is worse than not
     holding one.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import ccxt

from . import instruments
from .credentials import Credentials

log = logging.getLogger(__name__)

# There is deliberately no default proxy port. It used to be 127.0.0.1:10888,
# which is a guess: right for some machines, silently unreachable on others, and
# it made every failure look like a network outage. Callers must decide — the
# executor and the console both resolve it from the system proxy or from a saved
# setting. Passing None means "connect directly" and is a real choice, not a
# fallback.
DEFAULT_PROXY = None


class BrokerError(RuntimeError):
    """Raised when an exchange interaction cannot be completed safely."""


@dataclass(frozen=True)
class Fill:
    order_id: str
    side: str  # "long" | "short"
    price: float
    size: float  # contracts
    notional: float  # quote currency
    stop_price: float
    stop_order_id: str


def to_ccxt_symbol(symbol: str) -> str:
    """`BTC-USDT-SWAP` -> `BTC/USDT:USDT`; `BTC-USD-SWAP` -> `BTC/USD:BTC`.

    Splitting properly rather than taking `split("-")[0]`: the quote currency is
    not always USDT, and for an inverse contract the settlement currency is the
    base (`BTC/USD:BTC`), not the quote. `XAUUSDT-USDT-SWAP` also breaks the
    naive version — its base genuinely contains "USDT".
    """
    if "/" in symbol:
        return symbol
    parts = symbol.split("-")
    if len(parts) < 3:
        raise BrokerError(f"{symbol!r} is not an OKX instId like BTC-USDT-SWAP")
    base, quote = parts[0], parts[1]
    # Linear (USDT-margined) settles in the quote; inverse (*-USD-*) settles in
    # the base currency.
    settle = base if quote == "USD" else quote
    return f"{base}/{quote}:{settle}"


class Broker:
    def __init__(self, credentials: Credentials, proxy: str | None = DEFAULT_PROXY,
                 dry_run: bool = False) -> None:
        config: dict = {
            "apiKey": credentials.api_key,
            "secret": credentials.secret_key,
            "password": credentials.passphrase,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
        # ccxt ignores HTTP_PROXY/HTTPS_PROXY env vars; passing these is required.
        if proxy:
            config["proxies"] = {"http": proxy, "https": proxy}

        self.exchange = ccxt.okx(config)
        self.exchange.set_sandbox_mode(True)
        self.dry_run = dry_run
        self.proxy = proxy
        self._markets_loaded = False
        self._specs: dict[str, instruments.Spec] = {}

    # ------------------------------------------------------------------ setup
    def load_markets(self) -> None:
        if not self._markets_loaded:
            self.exchange.load_markets()
            self._markets_loaded = True

    # ---------------------------------------------------------- contract specs
    def _spec_from_ccxt(self, symbol: str) -> instruments.Spec:
        """Fallback specs when the OKX public endpoint is unreachable."""
        self.load_markets()
        market = self.exchange.market(to_ccxt_symbol(symbol))
        limits = market.get("limits") or {}

        def num(*path, default: float = 0.0) -> float:
            node: object = market
            for key in path:
                node = (node or {}).get(key) if isinstance(node, dict) else None
            try:
                return float(node)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default

        return instruments.Spec(
            inst_id=symbol,
            category="crypto",
            raw_category="1",
            contract_value=float(market.get("contractSize") or 0.0),
            contract_ccy=str(market.get("base") or ""),
            lot_size=num("precision", "amount"),
            min_size=num("limits", "amount", "min"),
            tick_size=num("precision", "price"),
            state="live" if market.get("active") else "suspended",
            max_leverage=num("limits", "leverage", "max"),
            max_market_size=num("limits", "amount", "max"),
        )

    def spec(self, symbol: str) -> instruments.Spec:
        """Contract face value, size step, and limits for `symbol`.

        Straight from OKX's `/public/instruments` — one authoritative source, the
        same one the web console displays, rather than a second opinion from
        ccxt's market cache. Falls back to ccxt only if that call fails, because
        a missing ctVal must not silently become "1 contract = 1 coin".
        """
        if symbol not in self._specs:
            try:
                self._specs[symbol] = instruments.require(symbol, proxy=self.proxy)
            except Exception as exc:
                log.warning("%s: 读不到合约规格（%s），改用 ccxt 缓存", symbol, exc)
                self._specs[symbol] = self._spec_from_ccxt(symbol)
            log.info("%s", instruments.describe(self._specs[symbol]))
        return self._specs[symbol]

    def verify_demo(self) -> float:
        """Confirm we are talking to the simulated environment. Returns USDT equity.

        The header check catches a build where sandbox was silently reset; the
        balance call proves the API key actually works against it.
        """
        if self.exchange.headers.get("x-simulated-trading") != "1":
            raise BrokerError(
                "sandbox header is not set — refusing to continue against a live endpoint"
            )
        try:
            balance = self.exchange.fetch_balance()
        except Exception as exc:
            raise BrokerError(f"could not read balance: {exc}") from exc

        if "USDT" not in balance:
            raise BrokerError("balance response has no USDT entry; unexpected for a swap account")
        total = float(balance["USDT"].get("total") or 0.0)
        log.info("demo account verified | USDT equity=%.2f", total)
        return total

    # ------------------------------------------------------------- market data
    def price(self, symbol: str) -> float:
        self.load_markets()
        ticker = self.exchange.fetch_ticker(to_ccxt_symbol(symbol))
        price = float(ticker.get("last") or 0.0)
        if price <= 0:
            raise BrokerError(f"no usable last price for {symbol}: {ticker.get('last')!r}")
        return price

    def set_leverage(self, symbol: str, leverage: int) -> int:
        """Set cross leverage for both sides. Returns the leverage actually set.

        The ceiling is per instrument, not a global constant — BTC-USDT-SWAP
        allows 100x, MU-USDT-SWAP 50x — and OKX rejects anything above it. We
        clamp down and shout rather than raise, because a crash-loop in the
        executor is strictly worse than trading smaller than planned.
        """
        self.load_markets()
        market = to_ccxt_symbol(symbol)
        leverage, warning = instruments.check_leverage(self.spec(symbol), leverage)
        if warning:
            log.warning(warning)
        for side in ("long", "short"):
            try:
                self.exchange.set_leverage(leverage, market, params={"mgnMode": "cross", "posSide": side})
            except Exception as exc:
                # Setting one side can fail when the account is in net mode, where
                # posSide is meaningless. Retry once without it before giving up.
                try:
                    self.exchange.set_leverage(leverage, market, params={"mgnMode": "cross"})
                    log.info("%s leverage set to %dx (net mode)", symbol, leverage)
                    return leverage
                except Exception:
                    raise BrokerError(f"could not set {leverage}x on {symbol}: {exc}") from exc
        log.info("%s leverage set to %dx", symbol, leverage)
        return leverage

    # ------------------------------------------------------------------ sizing
    def contracts_for_notional(self, symbol: str, notional_usdt: float, price: float) -> float:
        """Contracts needed for `notional_usdt` of exposure, floored to `lotSz`.

        The old version used `limits.amount.min` as the rounding step. That is
        `minSz`, the *smallest allowed order* — a different number from `lotSz`,
        the *increment*. On any instrument where they differ, the position came
        out a multiple of the wrong quantity and got rejected.
        """
        try:
            return instruments.contracts_for_notional(self.spec(symbol), notional_usdt, price)
        except instruments.SpecError as exc:
            raise BrokerError(str(exc)) from exc

    def notional_for_contracts(self, symbol: str, contracts: float, price: float) -> float:
        """Quote-currency value of `contracts`.

        One contract is NOT one unit: BTC-USDT-SWAP is 0.01 BTC and
        AAVE-USDT-SWAP is 0.1 AAVE, so `contracts * price` overstates the
        position by 100x and 10x respectively.
        """
        try:
            return instruments.notional_for_contracts(self.spec(symbol), contracts, price)
        except instruments.SpecError as exc:
            raise BrokerError(str(exc)) from exc

    # ----------------------------------------------------------------- entries
    def market_entry(self, symbol: str, side: str, notional_usdt: float,
                     leverage: int, stop_pct: float, client_id: str | None = None) -> Fill:
        """Open a position with a hard stop attached in the same request."""
        if side not in ("long", "short"):
            raise BrokerError(f"side must be long/short, got {side!r}")

        price = self.price(symbol)
        size = self.contracts_for_notional(symbol, notional_usdt * leverage, price)

        # maxMktSz is a real ceiling (MU-USDT-SWAP: 580 contracts). Finding out
        # from a rejection costs a fill; finding out here costs one log line.
        too_big = instruments.check_order_size(self.spec(symbol), size)
        if too_big:
            raise BrokerError(too_big)

        ccxt_side = "buy" if side == "long" else "sell"
        # Stop sits below entry for a long, above for a short.
        stop_price = price * (1 - stop_pct / 100) if side == "long" else price * (1 + stop_pct / 100)
        stop_price = self.exchange.price_to_precision(to_ccxt_symbol(symbol), stop_price)

        params: dict = {
            "tdMode": "cross",
            "posSide": "net",
            "stopLoss": {"triggerPrice": stop_price, "type": "market"},
        }
        if client_id:
            params["clOrdId"] = client_id

        if self.dry_run:
            notional = self.notional_for_contracts(symbol, size, price)
            log.info("[dry-run] would open %s %s size=%s contracts (%.2f USDT) @~%.6f stop=%s",
                     side, symbol, size, notional, price, stop_price)
            return Fill(order_id="dry-run", side=side, price=price, size=size,
                        notional=notional, stop_price=float(stop_price), stop_order_id="dry-run")

        try:
            order = self.exchange.create_order(
                to_ccxt_symbol(symbol), "market", ccxt_side, size, None, params
            )
        except Exception as exc:
            raise BrokerError(f"entry order rejected for {symbol} {side}: {exc}") from exc

        fill_price = float(order.get("average") or order.get("price") or price)
        filled = float(order.get("filled") or size)
        stop_id = self._find_stop_id(symbol, order.get("id"))
        if not stop_id:
            # No stop means no protection. Undo the entry rather than keep it.
            log.error("stop did not attach to %s order %s — closing to avoid a naked position",
                      symbol, order.get("id"))
            try:
                self.close_position(symbol)
            finally:
                raise BrokerError(f"entry for {symbol} filled without a stop; position was closed")

        # `filled * fill_price` was wrong here and nowhere else: it treats one
        # contract as one coin. On BTC-USDT-SWAP that overstates the position
        # 100x, and the ledger recorded the inflated number as truth.
        notional = self.notional_for_contracts(symbol, filled, fill_price)
        log.info("opened %s %s size=%s filled@%.6f notional=%.2f stop=%.2f (%s)",
                 side, symbol, filled, fill_price, notional, float(stop_price), stop_id)
        return Fill(
            order_id=str(order.get("id") or ""),
            side=side,
            price=fill_price,
            size=filled,
            notional=notional,
            stop_price=float(stop_price),
            stop_order_id=str(stop_id),
        )

    def _find_stop_id(self, symbol: str, order_id: str | None) -> str | None:
        """Read algo (conditional) orders back and find the stop for this position.

        Retrying matters because `market_entry` closes the position when this
        returns None. An algo order is not always visible the instant the entry
        fills, so "not found yet" and "does not exist" are different answers —
        the old code returned None from inside the loop, meaning it only ever
        retried after an exception and gave up immediately on an empty list.
        That turns a registration delay into an unnecessary market close.
        """
        market = to_ccxt_symbol(symbol)
        for attempt in range(3):
            try:
                algos = self.exchange.fetch_open_orders(market, params={"ordType": "conditional"})
            except Exception as exc:
                log.warning("could not read back algo orders (%s); retry %d/3", exc, attempt + 1)
                time.sleep(1.0)
                continue
            for algo in algos:
                info = algo.get("info") or {}
                if info.get("slTriggerPx"):
                    return str(algo.get("id") or info.get("algoId") or "")
                if algo.get("stopLossPrice"):
                    return str(algo.get("id") or "")
            if attempt < 2:  # visible yet? give the exchange a moment
                time.sleep(1.0)
        return None

    # ------------------------------------------------------------------- exits
    def get_position(self, symbol: str) -> dict | None:
        """Current exchange position for `symbol`, or None when flat."""
        self.load_markets()
        try:
            positions = self.exchange.fetch_positions([to_ccxt_symbol(symbol)])
        except Exception as exc:
            raise BrokerError(f"could not fetch position for {symbol}: {exc}") from exc
        for position in positions:
            contracts = float(position.get("contracts") or 0.0)
            if contracts > 0:
                return position
        return None

    def close_position(self, symbol: str) -> bool:
        """Market-close whatever is open. Returns True if something was closed."""
        self.load_markets()
        market = to_ccxt_symbol(symbol)
        position = self.get_position(symbol)
        if position is None:
            log.info("no open position on %s; nothing to close", symbol)
            return False

        contracts = float(position.get("contracts") or 0.0)
        side = (position.get("side") or "").lower()
        close_side = "sell" if side == "long" else "buy"
        params = {"tdMode": "cross", "posSide": "net", "reduceOnly": True}

        if self.dry_run:
            log.info("[dry-run] would close %s: %s %s contracts", symbol, close_side, contracts)
            return True

        try:
            self.exchange.create_order(market, "market", close_side, contracts, None, params)
        except Exception as exc:
            raise BrokerError(f"failed to close {symbol}: {exc}") from exc
        log.info("closed %s (%s %s contracts)", symbol, close_side, contracts)
        return True

    def cancel_stops(self, symbol: str) -> None:
        """Best-effort cancel of leftover conditional orders for a symbol."""
        if self.dry_run:
            return
        try:
            algos = self.exchange.fetch_open_orders(
                to_ccxt_symbol(symbol), params={"ordType": "conditional"}
            )
        except Exception as exc:
            log.warning("could not list algo orders for %s: %s", symbol, exc)
            return
        for algo in algos:
            try:
                self.exchange.cancel_order(str(algo.get("id")), to_ccxt_symbol(symbol))
            except Exception as exc:
                log.warning("could not cancel algo %s: %s", algo.get("id"), exc)
