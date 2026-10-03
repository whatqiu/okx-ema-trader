"""Adversarial tests for the smart MarketPoller scheduling.

Pinned invariants:
  - first poll always fetches candles (cold state)
  - same bar bucket + confirmed bar -> NO candle re-fetch (rate-budget saver)
  - unconfirmed close -> keeps retrying on the next passes
  - ticker fetched every pass but PERSISTED at most once per 60s
  - latest_ticker prefers memory, falls back to DB
  - stop() flushes the hot ticker cache to disk
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "platform" / "backend"))

import market  # noqa: E402
from okx_ema_trader.storage import Store  # noqa: E402

INST = "BTC-USDT-SWAP"
BAR = "5m"
STEP = 300_000
_RESULTS = []


def check(name, condition):
    _RESULTS.append((name, bool(condition)))


def make_fetch(calls, candle_confirm):
    """Fake OKX: records calls, serves 3 candles around the CURRENT bucket
    (test data must be built against real time, since the poller buckets by
    wall clock)."""

    def fetch(path, params):
        calls.append(path)
        now = int(time.time() * 1000)
        bucket = now // STEP
        if "candles" in path:
            return [
                [str(bucket * STEP), "100", "101", "99", "100.5", "1", "1", "1", "0"],
                [str((bucket - 1) * STEP), "99", "100", "98", "100", "1", "1", "1",
                 "1" if candle_confirm else "0"],
                [str((bucket - 2) * STEP), "98", "99", "97", "99", "1", "1", "1", "1"],
            ]
        return [{"instId": INST, "last": "100.5", "open24h": "99",
                 "high24h": "101", "low24h": "98", "vol24h": "1",
                 "volCcy24h": "1", "bidPx": "100.4", "askPx": "100.6",
                 "ts": str(now)}]

    return fetch


class SpyStore:
    """Counts save_tick calls; everything else delegates to the real Store."""

    def __init__(self, store):
        self._s = store
        self.saved_ticks = 0

    def save_tick(self, *args):
        self.saved_ticks += 1
        return self._s.save_tick(*args)

    def __getattr__(self, name):
        return getattr(self._s, name)


def current_bucket():
    return int(time.time() * 1000) // STEP


def test_first_poll_fetches_then_quiet():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        try:
            calls = []
            poller = market.MarketPoller(
                store, fetch=make_fetch(calls, candle_confirm=True))
            poller.watch(INST, BAR)
            poller.poll_once()
            candle_calls_1 = calls.count("/market/candles")
            check("cold state fetches candles", candle_calls_1 == 1)
            poller.poll_once()  # same bucket, now confirmed
            check("confirmed bar -> no candle re-fetch",
                  calls.count("/market/candles") == candle_calls_1)
            check("ticker still fetched every pass",
                  calls.count("/market/ticker") == 2)
        finally:
            store.close()


def test_unconfirmed_close_retries():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        try:
            calls = []
            poller = market.MarketPoller(
                store, fetch=make_fetch(calls, candle_confirm=False))
            poller.watch(INST, BAR)
            poller.poll_once()
            poller.poll_once()
            check("unconfirmed close retries every pass",
                  calls.count("/market/candles") == 2)
        finally:
            store.close()


def test_bucket_boundary_triggers_refetch():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        try:
            calls = []
            poller = market.MarketPoller(
                store, fetch=make_fetch(calls, candle_confirm=True))
            poller.watch(INST, BAR)
            poller.poll_once()
            # Simulate time having crossed into the next bar.
            poller._candle_state[(INST, BAR)]["bucket"] -= 1
            poller.poll_once()
            check("new bar bucket -> candle fetch",
                  calls.count("/market/candles") == 2)
        finally:
            store.close()


def test_ticker_persist_throttled():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        spy = SpyStore(store)
        try:
            calls = []
            poller = market.MarketPoller(
                spy, fetch=make_fetch(calls, candle_confirm=True))
            poller.watch(INST, BAR)
            poller.poll_once()
            poller.poll_once()
            poller.poll_once()
            check("ticker persisted once in 3 passes", spy.saved_ticks == 1)
            check("memory cache has the fresh tick",
                  poller.latest_ticker(INST)["last"] == 100.5)
        finally:
            store.close()


def test_latest_ticker_db_fallback_and_stop_flush():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        spy = SpyStore(store)
        try:
            calls = []
            poller = market.MarketPoller(
                spy, fetch=make_fetch(calls, candle_confirm=True))
            poller.watch(INST, BAR)
            poller.poll_once()
            # Memory hit, DB still empty of this pass's tick? It persisted
            # once (first pass). Clear memory to force the DB path.
            with poller._lock:
                poller._ticker_cache.clear()
            check("db fallback after memory cleared",
                  poller.latest_ticker(INST)["last"] == 100.5)
            # Fresh tick into memory only (persist just happened), stop()
            # must flush it.
            with poller._lock:
                poller._ticker_cache[INST] = dict(
                    poller._ticker_cache.get(INST)
                    or {"inst_id": INST, "last": 101.0})
                poller._ticker_cache[INST]["last"] = 101.0
            before = spy.saved_ticks
            poller.stop()
            check("stop() flushes hot cache", spy.saved_ticks == before + 1)
            check("flushed tick readable from db",
                  store.tick(INST)["last"] == 101.0)
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
