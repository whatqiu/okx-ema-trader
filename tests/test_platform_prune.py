"""Adversarial tests for Store.prune (retention sweep + disk reclaim).

Pinned invariants:
  - candles older than the retention window go; newer ones stay
  - OPEN positions are never deleted, no matter how old
  - closed orders / signals / equity / backtests are truncated to keep-N
  - prune is idempotent (second run deletes nothing)
  - prune keeps every surviving row intact (no half-deleted state)
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from okx_ema_trader.storage import Store  # noqa: E402

NOW = int(time.time() * 1000)
DAY = 86_400_000
_RESULTS = []


def check(name, condition):
    _RESULTS.append((name, bool(condition)))


def candle_row(ts):
    return [str(ts), "100", "101", "99", "100", "10", "1000", "1000", "1"]


def test_prune_candles_by_age():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        try:
            old = [candle_row(NOW - 50 * DAY + i * 300_000) for i in range(10)]
            new = [candle_row(NOW - 10 * DAY + i * 300_000) for i in range(10)]
            store.upsert_candles("BTC-USDT-SWAP", "5m", old + new)
            deleted = store.prune(candle_days=45)
            check("old candles deleted", deleted["candles"] == 10)
            check("new candles kept",
                  store.candle_extent("BTC-USDT-SWAP", "5m")[2] == 10)
            check("second prune is a no-op",
                  store.prune(candle_days=45)["candles"] == 0)
        finally:
            store.close()


def test_prune_never_deletes_open_positions():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        try:
            for i in range(5):
                oid = store.add_order("BTC-USDT-SWAP", "long", entry_price=100,
                                      notional=100, leverage=2,
                                      status="filled", opened_at=NOW - 200 * DAY)
                if i:  # 4 closed, 1 still open
                    store.close_order(oid, 101, pnl=1.0)
            deleted = store.prune(keep_orders=2)
            check("closed orders truncated to keep-N",
                  len(store.orders(status="closed")) == 2)
            check("open position survives",
                  len(store.open_orders()) == 1)
            check("deleted count covers only closed rows",
                  deleted["orders"] == 2)
        finally:
            store.close()


def test_prune_truncates_log_tables():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        try:
            for i in range(10):
                store.add_signal("BTC-USDT-SWAP", "long", "t", ts=NOW + i)
                store.add_equity(10000 + i, ts=NOW + i)
                store.save_backtest("BTC-USDT-SWAP", {"days": 1}, {"x": i})
            deleted = store.prune(keep_signals=3, keep_equity=4,
                                  keep_backtests=2)
            check("signals truncated", deleted["signals"] == 7
                  and len(store.signals(limit=100)) == 3)
            check("equity truncated", deleted["equity"] == 6
                  and len(store.equity_series(limit=100)) == 4)
            check("backtests truncated", deleted["backtests"] == 8
                  and len(store.backtests(limit=100)) == 2)
            kept = store.signals(limit=100)
            check("kept signals are the NEWEST",
                  kept[0]["ts"] == NOW + 9)
        finally:
            store.close()


def test_prune_data_integrity_after_vacuum():
    """VACUUM rewrites the whole file; surviving rows must read back whole."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        try:
            rows = [candle_row(NOW - 5 * DAY + i * 300_000) for i in range(50)]
            store.upsert_candles("ETH-USDT-SWAP", "15m", rows)
            store.prune(candle_days=45)
            back = store.candle_rows("ETH-USDT-SWAP", "15m", limit=100)
            check("all 50 rows intact after VACUUM", len(back) == 50)
            check("ohlc values intact",
                  back[0][1] == 100.0 and back[0][3] == 99.0)
        finally:
            store.close()


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    failed = 0
    for name, ok in _RESULTS:
        print(f"  {'ok' if ok else 'FAIL'}  {name}")
        failed += 0 if ok else 1
    print(f"\n{len(_RESULTS) - failed}/{len(_RESULTS)} checks passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
