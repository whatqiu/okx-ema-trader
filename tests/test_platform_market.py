"""Tests for the platform backend's market-data layer (offline).

Adversarial by design: the failure modes that matter here are not "does the
happy path work" but "does it loop forever", "does it leave a hole", and
"does the forming bar corrupt confirmed-bar queries".

Run:
    .venv/Scripts/python.exe tests\\test_platform_market.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "platform" / "backend"))

import market  # noqa: E402
from okx_ema_trader.http import OkxError  # noqa: E402
from okx_ema_trader.storage import Store  # noqa: E402

INST = "MU-USDT-SWAP"
BAR = "5m"
STEP = market.BAR_MS[BAR]
_checks = 0


def check(condition: bool, message: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(message)


def make_row(ts: int, price: float = 100.0, confirm: str = "1"):
    """OKX-shaped: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]."""
    return [str(ts), str(price), str(price + 1), str(price - 1), str(price),
            "10", "1000", "1000", confirm]


def page_ending_at(newest_ts: int, n: int):
    """A newest-first OKX page of n bars ending at newest_ts."""
    return [make_row(newest_ts - i * STEP) for i in range(n)]


def fresh_store():
    tmp = tempfile.TemporaryDirectory()
    return Store(Path(tmp.name) / "test.db"), tmp


# --------------------------------------------------------------------------
# Regression: confirm flag lives at index 8, not 7
# --------------------------------------------------------------------------
def test_confirm_flag_read_from_index_8():
    store, tmp = fresh_store()
    store.upsert_candles(INST, BAR, [make_row(1_000, confirm="1")])
    rows = store.candle_rows(INST, BAR, only_confirmed=True)
    check(len(rows) == 1, "index-7 bug: volCcyQuote was mistaken for confirm")
    store.close(); tmp.cleanup()


def test_forming_bar_excluded_from_confirmed_queries():
    store, tmp = fresh_store()
    store.upsert_candles(INST, BAR, [make_row(1_000, confirm="0")])
    check(store.candle_rows(INST, BAR, only_confirmed=True) == [],
          "forming bar leaked into confirmed-only query")
    check(len(store.candle_rows(INST, BAR)) == 1,
          "chart query must still see the forming bar")
    store.close(); tmp.cleanup()


def test_forming_bar_is_corrected_not_duplicated():
    store, tmp = fresh_store()
    ts = int(time.time() * 1000) // STEP * STEP
    store.upsert_candles(INST, BAR, [make_row(ts, price=100.0, confirm="0")])
    store.upsert_candles(INST, BAR, [make_row(ts, price=105.0, confirm="1")])
    rows = store.candles(INST, BAR)
    check(len(rows) == 1, f"expected 1 row after re-upsert, got {len(rows)}")
    check(rows[0]["close"] == 105.0, "forming bar not corrected in place")
    check(rows[0]["confirm"] == 1, "confirm flag not updated")
    store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# bar validation
# --------------------------------------------------------------------------
def test_bar_ms_rejects_unknown_bar():
    try:
        market.bar_ms("7m")
    except ValueError:
        return
    raise AssertionError("bar_ms accepted an unsupported bar")


# --------------------------------------------------------------------------
# Backward fill
# --------------------------------------------------------------------------
def test_backward_fill_pages_until_target():
    store, tmp = fresh_store()
    calls = []

    def fetch(path, params):
        calls.append(dict(params))
        if path == "/market/candles":
            return page_ending_at(int(time.time() * 1000), 1)
        after = int(params["after"]) if "after" in params else int(time.time() * 1000)
        return page_ending_at(after - STEP, 300)

    result = market.ensure_candles(store, INST, BAR, target_bars=700, fetch=fetch)
    check(result["count"] >= 700, f"count {result['count']} < 700")
    check(result["fetched"] >= 700, "fetched should cover the whole backfill")
    history_calls = [c for c in calls if "before" not in c]
    check(len(history_calls) <= 4,
          f"700 bars should take ~3 pages, took {len(history_calls)}")
    store.close(); tmp.cleanup()


def test_second_ensure_fetches_almost_nothing():
    store, tmp = fresh_store()
    now = int(time.time() * 1000)

    def fetch(path, params):
        if path == "/market/candles":
            return page_ending_at(now, 1)
        after = int(params["after"]) if "after" in params else now + STEP
        return page_ending_at(after - STEP, 300)

    market.ensure_candles(store, INST, BAR, target_bars=600, fetch=fetch)
    calls = []

    def counting_fetch(path, params):
        calls.append(path)
        return fetch(path, params)

    market.ensure_candles(store, INST, BAR, target_bars=600, fetch=counting_fetch)
    check(calls.count("/market/history-candles") == 0,
          "fresh + sufficient data must not trigger history paging")
    store.close(); tmp.cleanup()


def test_no_progress_guard_stops_loop():
    """A pathological endpoint that always returns the same page must not spin."""
    store, tmp = fresh_store()
    fixed = page_ending_at(1_000_000, 300)

    def fetch(path, params):
        return fixed

    result = market.ensure_candles(store, INST, BAR, target_bars=99999, fetch=fetch)
    check(result["count"] == 300,
          f"guard failed: count {result['count']} (loop did not stop)")
    store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# Forward fill (stale database)
# --------------------------------------------------------------------------
def test_forward_fill_closes_gap():
    store, tmp = fresh_store()
    now = int(time.time() * 1000)
    gap_bars = 50
    stale_newest = now - (gap_bars + 1) * STEP
    store.upsert_candles(INST, BAR, page_ending_at(stale_newest, 10))
    before_seen = []

    def fetch(path, params):
        if path == "/market/candles":
            return page_ending_at(now, 1)
        if "before" in params:
            before_seen.append(int(params["before"]))
            rows = [make_row(stale_newest + i * STEP) for i in range(1, gap_bars + 1)]
            return list(reversed(rows))  # newest-first, like OKX
        return []

    market.ensure_candles(store, INST, BAR, target_bars=5, fetch=fetch)
    check(bool(before_seen) and before_seen[0] == stale_newest,
          "forward fill must start strictly after the stale newest ts")
    oldest, newest, count = store.candle_extent(INST, BAR)
    check(count >= 60, f"hole left behind: count {count} < 60")
    check(newest >= now - 2 * STEP, "forward fill did not catch up to now")
    store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# Ticker
# --------------------------------------------------------------------------
def test_fetch_ticker_maps_okx_fields():
    store, tmp = fresh_store()

    def fetch(path, params):
        check(path == "/market/ticker", "ticker hit the wrong endpoint")
        return [{"last": "1.23", "open24h": "1.20", "high24h": "1.30",
                 "low24h": "1.10", "vol24h": "999", "volCcy24h": "1234",
                 "bidPx": "1.22", "askPx": "1.24", "ts": "1728000000000"}]

    tick = market.fetch_ticker(store, INST, fetch=fetch)
    check(tick["last"] == 1.23, "last not parsed")
    check(tick["bid"] == 1.22 and tick["ask"] == 1.24, "bid/ask not mapped from bidPx/askPx")
    stored = store.tick(INST)
    check(stored is not None and stored["last"] == 1.23, "ticker not persisted")
    store.close(); tmp.cleanup()


def test_fetch_ticker_empty_data_raises():
    store, tmp = fresh_store()

    def fetch(path, params):
        return []

    try:
        market.fetch_ticker(store, INST, fetch=fetch)
    except OkxError:
        pass
    else:
        raise AssertionError("empty ticker payload must raise OkxError")
    store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# Poller
# --------------------------------------------------------------------------
def test_poller_survives_network_error():
    store, tmp = fresh_store()

    def broken(path, params):
        raise OkxError("连不上 OKX", "network")

    poller = market.MarketPoller(store, fetch=broken)
    poller.watch(INST, BAR)
    poller.poll_once()  # must not raise
    check(bool(poller.last_error) and "连不上" in poller.last_error,
          "poller must record the error for the UI instead of dying")
    store.close(); tmp.cleanup()


def test_poller_updates_candles_and_ticker():
    store, tmp = fresh_store()
    now = int(time.time() * 1000)

    def fetch(path, params):
        if path == "/market/ticker":
            return [{"last": "5", "ts": str(now)}]
        return page_ending_at(now, 2)

    poller = market.MarketPoller(store, fetch=fetch)
    poller.watch(INST, BAR)
    poller.poll_once()
    check(poller.last_error is None, "unexpected poller error")
    check(store.tick(INST)["last"] == 5.0, "poller did not persist ticker")
    check(store.candle_extent(INST, BAR)[2] >= 2, "poller did not persist candles")
    store.close(); tmp.cleanup()


TESTS = [value for name, value in sorted(globals().items())
         if name.startswith("test_") and callable(value)]

if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(TESTS)} tests, {_checks} checks — all green")
