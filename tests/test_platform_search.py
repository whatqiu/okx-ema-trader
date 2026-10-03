"""Adversarial tests for symbol discovery and order filtering.

Covered invariants:
  - swap_tickers keeps only USDT swaps, sorts by 24h quote volume desc
  - the 5-minute cache actually caches (second call does NOT re-fetch)
  - reset_swap_ticker_cache forces a re-fetch
  - Store.orders status filter: open == 'filled', closed == 'closed'
  - Store.known_symbols unions ticks/candles/orders, sorted
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "platform" / "backend"))

import market  # noqa: E402
from okx_ema_trader.storage import Store  # noqa: E402

_RESULTS = []
ASSERTIONS = 0


def check(name, condition):
    _RESULTS.append((name, bool(condition)))


def expect_eq(name, actual, expected):
    global ASSERTIONS
    ASSERTIONS += 1
    _RESULTS.append((name, actual == expected))


def raw_ticker(inst, last, open24, vol_ccy):
    return {"instId": inst, "last": str(last), "open24h": str(open24),
            "volCcy24h": str(vol_ccy)}


def test_swap_tickers_filters_and_sorts():
    calls = []

    def fetch(path, params):
        calls.append((path, params))
        return [
            raw_ticker("BTC-USDT-SWAP", 100, 100, 5_000_000),
            # 50M units of a $1 coin = 50M quote — must NOT outrank BTC/ETH
            # even though its raw volCcy is the biggest number on the wire.
            raw_ticker("DOGE-USDT-SWAP", 1, 1, 50_000_000),
            raw_ticker("ETH-USDT-SWAP", 2000, 1900, 9_000_000),
            raw_ticker("BTC-USD-SWAP", 100, 100, 999_999_999),  # coin-margined: drop
            raw_ticker("BTC-USDT", 100, 100, 999_999_999),      # spot: drop
        ]

    market.reset_swap_ticker_cache()
    rows = market.swap_tickers(fetch=fetch)
    ids = [r["inst_id"] for r in rows]
    # Quote volume: ETH 9M*2000=18B > BTC 5M*100=500M > DOGE 50M*1=50M.
    check("only USDT swaps kept, ranked by QUOTE volume", ids == [
          "ETH-USDT-SWAP", "BTC-USDT-SWAP", "DOGE-USDT-SWAP"])
    check("sorted by volume desc",
          rows[0]["vol_ccy24h"] > rows[1]["vol_ccy24h"] > rows[2]["vol_ccy24h"])
    expect_eq("base extracted", rows[0]["base"], "ETH")
    expect_eq("change24h computed", round(rows[0]["change24h"], 6),
              round((2000 - 1900) / 1900, 6))


def test_swap_tickers_cache():
    calls = []

    def fetch(path, params):
        calls.append(1)
        return [raw_ticker("BTC-USDT-SWAP", 1, 1, 1)]

    market.reset_swap_ticker_cache()
    market.swap_tickers(fetch=fetch)
    market.swap_tickers(fetch=fetch)   # must hit the cache
    expect_eq("second call served from cache", len(calls), 1)
    market.reset_swap_ticker_cache()
    market.swap_tickers(fetch=fetch)
    expect_eq("reset forces re-fetch", len(calls), 2)
    market.swap_tickers(fetch=fetch, force=True)
    expect_eq("force bypasses cache", len(calls), 3)


def test_swap_tickers_bad_numbers_do_not_crash():
    def fetch(path, params):
        return [{"instId": "X-USDT-SWAP", "last": "", "open24h": "0",
                 "volCcy24h": ""}]

    market.reset_swap_ticker_cache()
    rows = market.swap_tickers(fetch=fetch)
    check("empty strings tolerated",
          rows[0]["last"] is None and rows[0]["change24h"] is None
          and rows[0]["vol_ccy24h"] == 0.0)


def _seed_store(tmp) -> Store:
    store = Store(Path(tmp) / "t.db")
    a = store.add_order("BTC-USDT-SWAP", "long", entry_price=100, notional=100,
                        leverage=2, status="filled")
    b = store.add_order("ETH-USDT-SWAP", "short", entry_price=2000,
                        notional=200, leverage=5, status="filled")
    store.close_order(b, 1900, pnl=9.0, pnl_pct=0.2)
    store.save_tick("MU-USDT-SWAP", {"last": 1.0})
    store.upsert_candles("SOL-USDT-SWAP", "5m",
                         [["1000", "1", "1", "1", "1", "1", "1", "1", "1"]])
    return store


def test_orders_status_filter():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        try:
            expect_eq("all orders", len(store.orders(limit=10)), 2)
            open_rows = store.orders(limit=10, status="open")
            expect_eq("open only", len(open_rows), 1)
            expect_eq("open row is BTC", open_rows[0]["inst_id"], "BTC-USDT-SWAP")
            closed_rows = store.orders(limit=10, status="closed")
            expect_eq("closed only", len(closed_rows), 1)
            expect_eq("closed row is ETH", closed_rows[0]["inst_id"],
                      "ETH-USDT-SWAP")
            both = store.orders(limit=10, inst_id="ETH-USDT-SWAP",
                                status="closed")
            expect_eq("symbol + status combine", len(both), 1)
            expect_eq("unknown status returns all (no silent filter)",
                      len(store.orders(limit=10, status="weird")), 2)
        finally:
            store.close()


def test_known_symbols():
    with tempfile.TemporaryDirectory() as tmp:
        store = _seed_store(tmp)
        try:
            syms = store.known_symbols()
            expect_eq("union of ticks+candles+orders",
                      syms, ["BTC-USDT-SWAP", "ETH-USDT-SWAP",
                             "MU-USDT-SWAP", "SOL-USDT-SWAP"])
        finally:
            store.close()


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            market.reset_swap_ticker_cache()
            fn()
    failed = 0
    for name, ok in _RESULTS:
        print(f"  {'ok' if ok else 'FAIL'}  {name}")
        failed += 0 if ok else 1
    total = len(_RESULTS)
    print(f"\n{total - failed}/{total} checks passed "
          f"({ASSERTIONS} strict assertions)")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
