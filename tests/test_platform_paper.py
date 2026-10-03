"""Tests for the paper-trading layer (offline).

The numbers here are money. Every test pins an exact formula: fees on both
sides, margin checks BEFORE the order is booked, and equity that does not
teleport when a position closes.

Run:
    .venv/Scripts/python.exe tests/test_platform_paper.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "platform" / "backend"))

import paper  # noqa: E402
from okx_ema_trader.storage import Store  # noqa: E402

INST = "MU-USDT-SWAP"
_checks = 0


def check(condition: bool, message: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(message)


def close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def fresh_store():
    tmp = tempfile.TemporaryDirectory()
    return Store(Path(tmp.name) / "test.db"), tmp


# --------------------------------------------------------------------------
def test_open_books_fee_and_margin():
    store, tmp = fresh_store()
    res = paper.open_position(store, INST, "long", notional=1000.0,
                              leverage=10.0, price=100.0)
    check(close(res["margin"], 100.0), "margin = notional/leverage")
    check(close(res["fee"], 0.5), "taker fee = 5bps of notional")
    acc = paper.account_summary(store, {INST: 100.0})
    # Equity at entry price = 10000 - opening fee (deducted via unrealised).
    check(close(acc["equity"], 9999.5), f"equity after open: {acc['equity']}")
    check(close(acc["margin_used"], 100.0), "margin not tracked")
    store.close(); tmp.cleanup()


def test_insufficient_margin_rejected_before_booking():
    store, tmp = fresh_store()
    try:
        paper.open_position(store, INST, "long", notional=2_000_000.0,
                            leverage=1.0, price=100.0)
    except ValueError as exc:
        check("可用余额不足" in str(exc), "wrong rejection message")
    else:
        raise AssertionError("oversized position was not rejected")
    check(store.open_orders() == [], "rejected order was still booked")
    store.close(); tmp.cleanup()


def test_bad_side_and_bad_leverage_rejected():
    store, tmp = fresh_store()
    for kwargs in ({"side": "buy"}, {"leverage": 0}, {"leverage": 126},
                   {"notional": -5}):
        args = dict(inst_id=INST, side="long", notional=100.0,
                    leverage=2.0, price=100.0)
        args.update(kwargs)
        try:
            paper.open_position(store, **args)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid {kwargs}")
    check(store.open_orders() == [], "invalid orders were booked")
    store.close(); tmp.cleanup()


def test_long_profit_includes_both_fees():
    store, tmp = fresh_store()
    res = paper.open_position(store, INST, "long", notional=1000.0,
                              leverage=10.0, price=100.0)
    out = paper.close_position(store, res["order_id"], 110.0)
    # gross = +10% of 1000 = 100; fees = 0.5 open + 0.5 close
    check(close(out["pnl"], 99.0), f"pnl {out['pnl']} != 99.0")
    check(close(out["pnl_pct"], 99.0 / 100.0), "pnl_pct must be vs margin")
    acc = paper.account_summary(store)
    check(close(acc["balance"], 10_099.0), f"balance {acc['balance']}")
    check(close(acc["equity"], acc["balance"]), "no open positions: equity == balance")
    store.close(); tmp.cleanup()


def test_short_loss_accounting():
    store, tmp = fresh_store()
    res = paper.open_position(store, INST, "short", notional=1000.0,
                              leverage=5.0, price=100.0)
    out = paper.close_position(store, res["order_id"], 104.0)
    # short loses 4% of 1000 = -40, minus 1.0 fees
    check(close(out["pnl"], -41.0), f"short pnl {out['pnl']} != -41")
    store.close(); tmp.cleanup()


def test_equity_does_not_teleport_more_than_close_fee():
    """The only equity jump at close is the close fee appearing. Anything
    bigger means mark-to-market and realised accounting disagree."""
    store, tmp = fresh_store()
    res = paper.open_position(store, INST, "long", notional=1000.0,
                              leverage=10.0, price=100.0)
    before = paper.account_summary(store, {INST: 110.0})["equity"]
    paper.close_position(store, res["order_id"], 110.0)
    after = paper.account_summary(store)["equity"]
    jump = before - after
    check(close(jump, 0.5), f"equity jumped {jump} at close (should be close fee 0.5)")
    store.close(); tmp.cleanup()


def test_close_unknown_order_rejected():
    store, tmp = fresh_store()
    try:
        paper.close_position(store, 999, 100.0)
    except ValueError:
        pass
    else:
        raise AssertionError("closed a nonexistent order")
    store.close(); tmp.cleanup()


def test_double_close_rejected():
    store, tmp = fresh_store()
    res = paper.open_position(store, INST, "long", notional=100.0,
                              leverage=1.0, price=100.0)
    paper.close_position(store, res["order_id"], 100.0)
    try:
        paper.close_position(store, res["order_id"], 100.0)
    except ValueError:
        pass
    else:
        raise AssertionError("double close was allowed")
    store.close(); tmp.cleanup()


def test_liquidation_price_sanity():
    store, tmp = fresh_store()
    res = paper.open_position(store, INST, "long", notional=1000.0,
                              leverage=10.0, price=100.0)
    pos = paper.account_summary(store, {INST: 100.0})["positions"][0]
    check(close(pos["liq_price"], 90.0), f"long liq {pos['liq_price']} != 90")
    res2 = paper.open_position(store, INST, "short", notional=100.0,
                               leverage=20.0, price=100.0)
    poss = {p["id"]: p for p in paper.account_summary(store, {INST: 100.0})["positions"]}
    check(close(poss[res2["order_id"]]["liq_price"], 105.0), "short liq wrong")
    store.close(); tmp.cleanup()


def test_reset_wipes_and_restarts():
    store, tmp = fresh_store()
    res = paper.open_position(store, INST, "long", notional=100.0,
                              leverage=1.0, price=100.0)
    paper.close_position(store, res["order_id"], 90.0)
    paper.reset_account(store, 5_000.0)
    acc = paper.account_summary(store)
    check(close(acc["balance"], 5_000.0), "reset balance wrong")
    check(acc["positions"] == [], "reset left positions behind")
    check(store.orders(limit=1000) == [], "reset left order history behind")
    store.close(); tmp.cleanup()


def test_unrealised_uses_mark_price():
    store, tmp = fresh_store()
    paper.open_position(store, INST, "long", notional=1000.0,
                        leverage=2.0, price=100.0)
    acc = paper.account_summary(store, {INST: 103.0})
    # +3% on 1000 = 30, minus 0.5 opening fee
    check(close(acc["unrealised_pnl"], 29.5), f"upl {acc['unrealised_pnl']}")
    store.close(); tmp.cleanup()


TESTS = [value for name, value in sorted(globals().items())
         if name.startswith("test_") and callable(value)]

if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(TESTS)} tests, {_checks} checks — all green")
