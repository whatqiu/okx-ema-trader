"""Contract specifications: how many coins a "contract" actually is.

The single most expensive assumption in this codebase would be that 1 contract
= 1 unit of the base asset. It is not, and the ratio differs per instrument:

    BTC-USDT-SWAP    ctVal = 0.01   -> 1 contract = 0.01 BTC
    AAVE-USDT-SWAP   ctVal = 0.1    -> 1 contract = 0.1 AAVE
    MU-USDT-SWAP     ctVal = 1      -> 1 contract = 1 MU

Get it wrong by 100x and either the exchange rejects the order or you open a
position a hundred times the size you intended. Everything that converts between
contracts and money goes through this module so there is one place to get it
right.

Also carries the other per-instrument limits the exchange enforces and we used
not to check at all: `lotSz` (the size increment), `minSz` (the smallest
order), `tickSz`, `lever` (the maximum leverage — NOT a global constant), and
`maxMktSz` (the largest market order). `instCategory` tells us whether we are
looking at crypto (1), a stock token (3), metals (4), commodities (5), forex (6)
or bonds (7) — they do not all behave the same way.

Source: OKX `/api/v5/public/instruments`, documented in the official
agent-skills repo (`okx-cex-market` -> `instrument-commands.md`).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from .http import get_json

# OKX's `instCategory` field. Kept as strings because that is what the API sends.
CATEGORIES = {
    "1": "crypto",
    "2": "crypto",
    "3": "stock token",
    "4": "metals",
    "5": "commodities",
    "6": "forex",
    "7": "bonds",
}


class SpecError(RuntimeError):
    """Raised when a requested size cannot be expressed for this instrument."""


@dataclass(frozen=True)
class Spec:
    """Everything the exchange enforces about one instrument."""

    inst_id: str
    category: str          # CATEGORIES value, e.g. "stock token"
    raw_category: str      # the numeric string OKX sent, e.g. "3"
    contract_value: float  # ctVal: base units per contract
    contract_ccy: str      # ctValCcy
    lot_size: float        # lotSz: size increment
    min_size: float        # minSz: smallest order
    tick_size: float       # tickSz: price increment
    state: str             # "live" | "suspend" | "preopen" | ...
    max_leverage: float    # lever: the instrument's ceiling, NOT a global 10
    max_market_size: float  # maxMktSz, 0 when the field is absent

    @property
    def is_live(self) -> bool:
        return self.state == "live"

    @property
    def is_stock_token(self) -> bool:
        return self.raw_category == "3"


def parse(row: dict) -> Spec:
    """Build a `Spec` from one `/public/instruments` row."""

    def num(key: str, default: float = 0.0) -> float:
        try:
            return float(row.get(key) or default)
        except (TypeError, ValueError):
            return default

    raw_category = str(row.get("instCategory") or "1")
    return Spec(
        inst_id=str(row.get("instId") or ""),
        category=CATEGORIES.get(raw_category, f"category {raw_category}"),
        raw_category=raw_category,
        contract_value=num("ctVal"),
        contract_ccy=str(row.get("ctValCcy") or ""),
        lot_size=num("lotSz"),
        min_size=num("minSz"),
        tick_size=num("tickSz"),
        state=str(row.get("state") or ""),
        max_leverage=num("lever"),
        max_market_size=num("maxMktSz"),
    )


def fetch(symbol: str, proxy: str | None = None) -> Spec | None:
    """Specs for one instrument, or None when OKX does not list it."""
    rows = get_json("/public/instruments", {"instType": "SWAP", "instId": symbol},
                    proxy=proxy, timeout=20.0)
    return parse(rows[0]) if rows else None


def fetch_all(proxy: str | None = None, inst_type: str = "SWAP") -> list[Spec]:
    rows = get_json("/public/instruments", {"instType": inst_type},
                    proxy=proxy, timeout=20.0)
    return [parse(row) for row in rows if row.get("instId")]


def require(symbol: str, proxy: str | None = None) -> Spec:
    """Like `fetch` but fails loudly. Use before placing an order."""
    spec = fetch(symbol, proxy=proxy)
    if spec is None:
        raise SpecError(f"OKX 没有这个合约：{symbol}")
    if not spec.is_live:
        raise SpecError(f"{symbol} 当前状态是 {spec.state!r}，不接受下单")
    if spec.contract_value <= 0:
        raise SpecError(f"{symbol} 的 ctVal 是 {spec.contract_value}，无法换算张数")
    return spec


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------
def _floor_to(value: float, step: float) -> float:
    """Floor `value` to a multiple of `step` without float drift.

    `(raw // step) * step` in binary floats turns 0.3 into
    0.30000000000000004, which OKX rejects. Decimal with an explicit FLOOR
    keeps it exact — and FLOOR, not ROUND, because rounding up would silently
    increase the position beyond the requested notional.
    """
    if step <= 0:
        return float(value)
    units = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(ROUND_FLOOR)
    return float(units * Decimal(str(step)))


def round_contracts(spec: Spec, raw: float) -> float:
    """Clamp a desired contract count to what this instrument actually accepts."""
    size = _floor_to(raw, spec.lot_size)
    if size < spec.min_size:
        raise SpecError(
            f"{spec.inst_id}: 需要 {raw:.6f} 张，取整后 {size:.6f} 张，"
            f"低于最小下单量 {spec.min_size} 张。加大名义金额或换一个合约。"
        )
    return size


def contracts_for_notional(spec: Spec, notional: float, price: float) -> float:
    """Contracts that give `notional` quote-currency of exposure at `price`."""
    if price <= 0:
        raise SpecError(f"{spec.inst_id}: 价格 {price} 无效")
    per_contract = spec.contract_value * price
    if per_contract <= 0:
        raise SpecError(f"{spec.inst_id}: ctVal×price = {per_contract}，无法换算")
    return round_contracts(spec, notional / per_contract)


def notional_for_contracts(spec: Spec, contracts: float, price: float) -> float:
    """Quote-currency value of `contracts`. NOT `contracts * price`."""
    return contracts * spec.contract_value * price


def round_price(spec: Spec, price: float) -> float:
    return _floor_to(price, spec.tick_size) if spec.tick_size > 0 else float(price)


# --------------------------------------------------------------------------
# Pre-trade checks
# --------------------------------------------------------------------------
def check_leverage(spec: Spec, wanted: int) -> tuple[int, str | None]:
    """Clamp a requested leverage to this instrument's ceiling.

    Returns `(effective, warning)`. Refusing outright would be safer but the
    ceiling is genuinely per-instrument (BTC 100x, MU 50x) and OKX rejects
    anything above it, so a silent clamp with a loud warning beats a 400 nobody
    can act on. The caller decides whether to proceed.
    """
    if spec.max_leverage <= 0:
        return int(wanted), None
    if wanted <= spec.max_leverage:
        return int(wanted), None
    return int(spec.max_leverage), (
        f"{spec.inst_id} 最大杠杆 {spec.max_leverage:g}x，配置里的 {wanted}x 会被交易所拒绝；"
        f"本次按 {spec.max_leverage:g}x 下单。改 config.yaml 的 trading.leverage 消除这个警告。"
    )


def check_order_size(spec: Spec, contracts: float) -> str | None:
    """Warn when a market order exceeds the instrument's max market size."""
    if spec.max_market_size <= 0 or contracts <= spec.max_market_size:
        return None
    return (f"{spec.inst_id}: {contracts:.6f} 张超过市价单上限 {spec.max_market_size:g} 张，"
            f"交易所会拒绝。减小仓位或改用限价单。")


def describe(spec: Spec) -> str:
    """One line of Chinese for the UI, so the size maths is never invisible."""
    return (f"{spec.inst_id} · {spec.category} · 1 张 = {spec.contract_value:g} "
            f"{spec.contract_ccy} · 最小 {spec.min_size:g} 张 / 步长 {spec.lot_size:g} · "
            f"最大杠杆 {spec.max_leverage:g}x · {spec.state}")
