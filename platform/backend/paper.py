"""Paper trading: a simulated account on top of the local store.

One rule makes this honest rather than a toy: fills use the REAL bid/ask from
the last ticker poll, and every open and close pays the taker fee. A paper
account that fills at mid-price with no costs teaches bad habits — the user
would go live expecting fills that do not exist.

Positions reuse the `orders` table (status 'filled' = open, 'closed' = done).
The account balance is derived, not stored: initial balance (meta key) plus
the sum of realised PnL. Derived state can never disagree with itself; stored
state can. The equity table gets a snapshot after every trade so the equity
curve in the UI has something to draw.
"""
from __future__ import annotations

DEFAULT_BALANCE = 10_000.0
TAKER_FEE_BPS = 5.0        # OKX swap VIP0 taker, per side
_META_KEY = "paper_initial_balance"


def initial_balance(store) -> float:
    raw = store.get_meta(_META_KEY)
    if raw is None:
        store.set_meta(_META_KEY, str(DEFAULT_BALANCE))
        return DEFAULT_BALANCE
    return float(raw)


def reset_account(store, balance: float = DEFAULT_BALANCE) -> None:
    """Wipe every order and restart the account. There is no partial reset —
    a half-reset account with orphaned positions is worse than none."""
    with store._connect() as conn:
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM equity")
    store.set_meta(_META_KEY, str(float(balance)))
    store.add_equity(float(balance), note="reset")


def _fee(notional: float) -> float:
    return notional * TAKER_FEE_BPS / 10_000.0


def _f(value) -> float | None:
    """OKX sends numbers as strings; tolerate a missing/blank one."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def open_position(store, inst_id: str, side: str, notional: float,
                  leverage: float, price: float) -> dict:
    """Open a paper position at `price` (caller passes real bid/ask).

    Raises ValueError on anything that would be rejected by a real exchange's
    risk checks — wrong side, non-positive size, margin the account cannot
    cover. Better to learn "insufficient margin" here than on real money.
    """
    if side not in ("long", "short"):
        raise ValueError(f"side 必须是 long 或 short，收到 {side!r}")
    if notional <= 0:
        raise ValueError("名义价值必须大于 0")
    if not (1 <= leverage <= 125):
        raise ValueError("杠杆必须在 1–125 之间")

    margin = notional / leverage
    account = account_summary(store)
    if margin + _fee(notional) > account["available"]:
        raise ValueError(
            f"可用余额不足：需要保证金 {margin:.2f} + 手续费 {_fee(notional):.2f}，"
            f"可用 {account['available']:.2f}")

    fee = _fee(notional)
    order_id = store.add_order(
        inst_id, side,
        contracts=notional / price, entry_price=price, notional=notional,
        leverage=leverage, status="filled", reason="paper")
    # Equity already reflects the opening fee: position_unrealised deducts it.
    # Subtracting it again here would double-count.
    store.add_equity(account_summary(store)["equity"], note=f"open {side} {inst_id}")
    return {"order_id": order_id, "entry_price": price, "notional": notional,
            "leverage": leverage, "margin": margin, "fee": fee}


def close_position(store, order_id: int, price: float) -> dict:
    """Close an open paper position at `price`. Returns realised PnL details."""
    opens = {o["id"]: o for o in store.open_orders()}
    order = opens.get(order_id)
    if order is None:
        raise ValueError(f"持仓 #{order_id} 不存在或已平仓")

    direction = 1.0 if order["side"] == "long" else -1.0
    notional = order["notional"]
    gross = (price - order["entry_price"]) / order["entry_price"] * direction
    fees = _fee(notional) * 2          # open + close
    pnl = gross * notional - fees
    margin = notional / order["leverage"]
    pnl_pct = pnl / margin if margin else 0.0

    store.close_order(order_id, price, pnl=pnl, pnl_pct=pnl_pct, reason="paper-close")
    account_after = account_summary(store)
    store.add_equity(account_after["equity"], note=f"close #{order_id}")
    return {"order_id": order_id, "exit_price": price, "gross_return": gross,
            "fees": fees, "pnl": pnl, "pnl_pct": pnl_pct}


def position_unrealised(order: dict, mark: float) -> float:
    """Mark-to-market PnL of one open position, close fee NOT yet deducted."""
    direction = 1.0 if order["side"] == "long" else -1.0
    return ((mark - order["entry_price"]) / order["entry_price"]
            * direction * order["notional"]) - _fee(order["notional"])


def liquidation_price(order: dict) -> float:
    """Approximate isolated-margin liq price: margin gone when price moves
    1/leverage against you (ignores maintenance margin — labelled as approx)."""
    lev = order["leverage"] or 1
    if order["side"] == "long":
        return order["entry_price"] * (1 - 1 / lev)
    return order["entry_price"] * (1 + 1 / lev)


def account_summary(store, marks: dict[str, float] | None = None,
                    exit_prices: dict[str, tuple] | None = None) -> dict:
    """balance / equity / margin / available, plus open positions marked to
    `marks` (inst_id -> last price). `exit_prices` supplies (bid, ask) so a risk
    close can cross the spread instead of filling at the mark.

    Missing marks use the entry price, which understates nothing and overstates
    nothing — it just means "no price yet".
    """
    marks = marks or {}
    exit_prices = exit_prices or {}
    orders = store.orders(limit=1000)
    realised = sum(o["pnl"] for o in orders
                   if o["status"] == "closed" and o["pnl"] is not None)
    balance = initial_balance(store) + realised

    margin_used = 0.0
    unrealised = 0.0
    positions = []
    for o in store.open_orders():
        margin = o["notional"] / (o["leverage"] or 1)
        mark = marks.get(o["inst_id"], o["entry_price"])
        upl = position_unrealised(o, mark)
        margin_used += margin
        unrealised += upl
        positions.append({
            **o,
            "mark_price": mark,
            "unrealised_pnl": upl,
            "margin": margin,
            "liq_price": liquidation_price(o),
            # The exit side, so a risk close can cross the spread honestly
            # instead of pretending the mark price is fillable.
            "bid": _f(exit_prices.get(o["inst_id"], (None, None))[0]),
            "ask": _f(exit_prices.get(o["inst_id"], (None, None))[1]),
        })
    equity = balance + unrealised
    return {
        "initial": initial_balance(store),
        "balance": balance,
        "equity": equity,
        "realised_pnl": realised,
        "unrealised_pnl": unrealised,
        "margin_used": margin_used,
        "available": equity - margin_used,
        "positions": positions,
    }
