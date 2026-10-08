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
import threading
import time
from collections.abc import Callable
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


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Wait for a daemon worker without baking a machine-speed sleep into tests."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


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


def test_bars_for_days_uses_calendar_math_and_daily_floor():
    check(market.bars_for_days("5m", 30) == 8640,
          "30 days of 5m must be exactly 8,640 bars")
    check(market.bars_for_days("1D", 30) == 30,
          "30 daily bars must both satisfy the math and the chart floor")
    check(market.bars_for_days("1D", 1) == 30,
          "short daily requests still need the 30-bar presentation floor")


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


def test_default_target_backfills_thirty_days():
    store, tmp = fresh_store()
    now = int(time.time() * 1000) // STEP * STEP
    calls = []

    def fetch(path, params):
        calls.append((path, dict(params)))
        if path == "/market/candles":
            return page_ending_at(now, 1)
        after = int(params["after"]) if "after" in params else now + STEP
        return page_ending_at(after - STEP, market.MAX_LIMIT)

    report = market.ensure_candles(store, INST, BAR, fetch=fetch)
    target = market.bars_for_days(BAR)
    history_calls = [call for call in calls
                     if call[0] == "/market/history-candles"]
    expected_pages = (target + market.MAX_LIMIT - 1) // market.MAX_LIMIT
    check(report["count"] >= target,
          f"default target stopped at {report['count']} of {target} bars")
    check(len(history_calls) == expected_pages,
          f"expected {expected_pages} history pages, got {len(history_calls)}")
    store.close(); tmp.cleanup()


def test_default_target_is_idempotent_after_thirty_day_fill():
    store, tmp = fresh_store()
    now = int(time.time() * 1000) // STEP * STEP

    def fetch(path, params):
        if path == "/market/candles":
            return page_ending_at(now, 1)
        after = int(params["after"]) if "after" in params else now + STEP
        return page_ending_at(after - STEP, market.MAX_LIMIT)

    market.ensure_candles(store, INST, BAR, fetch=fetch)
    calls = []

    def counting_fetch(path, params):
        calls.append(path)
        return fetch(path, params)

    report = market.ensure_candles(store, INST, BAR, fetch=counting_fetch)
    check(report["count"] >= market.bars_for_days(BAR),
          "the idempotent pass lost already-stored history")
    check(calls == [],
          f"a warm default target must make zero OKX calls, got {calls}")
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
# Background history warmer
# --------------------------------------------------------------------------
def test_history_warmer_schedule_is_nonblocking_and_deduplicates():
    store, tmp = fresh_store()
    entered_fetch = threading.Event()
    release_fetch = threading.Event()
    history_calls = []

    def fetch(path, params):
        if path == "/market/history-candles":
            history_calls.append(dict(params))
            entered_fetch.set()
            release_fetch.wait(timeout=2.0)
        return []

    warmer = market.HistoryWarmer(fetch=fetch)
    try:
        started_at = time.monotonic()
        first_added = warmer.schedule(store, INST, BAR)
        elapsed = time.monotonic() - started_at
        check(first_added and elapsed < 0.1,
              f"schedule blocked the request path for {elapsed:.3f}s")
        check(entered_fetch.wait(timeout=1.0), "warmer never started queued work")
        duplicate_results = [warmer.schedule(store, INST, BAR) for _ in range(5)]
        check(not any(duplicate_results),
              "a key already being processed must not be queued again")
        release_fetch.set()
        check(wait_until(lambda: not warmer._pending),
              "warmer did not finish the queued key")
        check(len(history_calls) == 1,
              f"duplicate schedules triggered {len(history_calls)} history calls")
    finally:
        release_fetch.set()
        warmer.stop()
        store.close(); tmp.cleanup()


def test_history_warmer_survives_worker_exception():
    store, tmp = fresh_store()
    attempted = threading.Event()

    def broken(path, params):
        attempted.set()
        raise OkxError("预热测试断网", "network")

    warmer = market.HistoryWarmer(fetch=broken)
    try:
        check(warmer.schedule(store, INST, BAR), "failed key was not scheduled")
        check(attempted.wait(timeout=1.0), "worker never called the fetcher")
        check(wait_until(lambda: not warmer._pending),
              "failed work remained permanently pending")
        check(warmer._thread.is_alive(),
              "an OkxError escaped and killed the history worker")
    finally:
        warmer.stop()
        store.close(); tmp.cleanup()


def test_history_warmer_reaches_thirty_day_depth():
    store, tmp = fresh_store()
    now = int(time.time() * 1000) // STEP * STEP
    history_calls = []

    def fetch(path, params):
        if path == "/market/candles":
            return page_ending_at(now, 1)
        history_calls.append(dict(params))
        after = int(params["after"]) if "after" in params else now + STEP
        return page_ending_at(after - STEP, market.MAX_LIMIT)

    warmer = market.HistoryWarmer(fetch=fetch)
    try:
        check(warmer.schedule(store, INST, BAR), "cold history was not scheduled")
        target = market.bars_for_days(BAR)
        check(wait_until(lambda: store.candle_extent(INST, BAR)[2] >= target),
              "background warmer did not reach the 30-day target")
        check(wait_until(lambda: (INST, BAR) in warmer._complete),
              "completed history was not marked for future deduplication")
        expected_pages = (target + market.MAX_LIMIT - 1) // market.MAX_LIMIT
        check(len(history_calls) == expected_pages,
              f"warmer used {len(history_calls)} pages, expected {expected_pages}")
        check(not warmer.schedule(store, INST, BAR),
              "an already-complete key must not be queued again")
    finally:
        warmer.stop()
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


def _store_with_a_hole(now: int, hole_bars: int = 12, old_bars: int = 5):
    """Old block, then a hole, then the newest 3 bars — the reconnect shape.

    This is what a disconnection leaves behind: the poller's very next
    successful pass writes the newest three bars, so `newest` jumps to now
    while everything that closed in between is simply absent.
    """
    store, tmp = fresh_store()
    T0 = now - (hole_bars + 3) * STEP        # last bar of the old block
    store.upsert_candles(INST, BAR,
                         [make_row(T0 - i * STEP) for i in range(old_bars)])
    store.upsert_candles(INST, BAR, [make_row(now - i * STEP) for i in range(3)])
    return store, tmp, T0


def _hole_aware_fetch(now: int, T0: int):
    """Serves /market/candles and fills the hole, refuses anything else."""
    def fetch(path, params):
        if path == "/market/candles":
            return page_ending_at(now, 3)
        if path == "/market/history-candles" and "after" in params:
            cursor = int(params["after"])
            rows, ts = [], cursor - STEP
            while ts > T0:                   # strictly inside the hole
                rows.append(make_row(ts))
                ts -= STEP
            return rows
        return []
    return fetch


def test_candle_gaps_detects_an_interior_hole():
    """Extent says "complete"; only walking the series finds the hole."""
    now = int(time.time() * 1000) // STEP * STEP
    store, tmp, T0 = _store_with_a_hole(now)

    gaps = store.candle_gaps(INST, BAR, STEP)
    check(len(gaps) == 1, f"expected exactly one hole, got {gaps}")
    check(gaps[0] == (T0, now - 2 * STEP), f"wrong hole bounds: {gaps[0]}")

    # The whole point: oldest/newest/count cannot see it, so any gap logic
    # built on the extent would decide there is nothing to do.
    oldest, newest, count = store.candle_extent(INST, BAR)
    check(count == 8 and newest == now,
          "fixture should look complete from the outside")
    store.close(); tmp.cleanup()


def test_ensure_candles_heals_a_hole_after_reconnect():
    now = int(time.time() * 1000) // STEP * STEP
    store, tmp, T0 = _store_with_a_hole(now)

    # target_bars below the current count: proves the hole is healed by the
    # gap phase, not by the backward fill happening to cover it.
    report = market.ensure_candles(store, INST, BAR, target_bars=5,
                                   fetch=_hole_aware_fetch(now, T0))
    check(report["holes_filled"] == 1, f"no hole reported: {report}")
    check(store.candle_gaps(INST, BAR, STEP) == [], "hole survived the sync")
    _, _, count = store.candle_extent(INST, BAR)
    check(count == 20, f"expected 5 + 12 + 3 bars, got {count}")
    store.close(); tmp.cleanup()


def test_poller_heals_the_hole_on_the_first_pass_after_reconnect():
    """Recovering must fix the chart by itself — no manual reload."""
    now = int(time.time() * 1000) // STEP * STEP
    store, tmp, T0 = _store_with_a_hole(now)
    calls = {"n": 0}

    def fetch(path, params):
        calls["n"] += 1
        if calls["n"] == 1:                  # the offline pass
            raise OkxError("连不上 OKX", "network")
        if path == "/market/ticker":
            return [{"last": "5", "ts": str(now)}]
        return _hole_aware_fetch(now, T0)(path, params)

    poller = market.MarketPoller(store, fetch=fetch)
    poller.watch(INST, BAR)
    poller.poll_once()
    check(poller.last_error is not None, "offline pass must record the error")
    poller.poll_once()
    check(poller.last_error is None, "online pass must clear the error")
    check(store.candle_gaps(INST, BAR, STEP) == [],
          "hole still there after the poller came back")
    store.close(); tmp.cleanup()


def test_gap_fill_stops_on_an_unfillable_hole():
    """A hole OKX has no bars for must not become an infinite page loop."""
    now = int(time.time() * 1000) // STEP * STEP
    store, tmp, T0 = _store_with_a_hole(now)
    calls = {"n": 0}

    def fetch(path, params):
        calls["n"] += 1
        if path == "/market/candles":
            return page_ending_at(now, 3)
        return []                            # OKX has nothing for the hole

    market.ensure_candles(store, INST, BAR, target_bars=5, fetch=fetch)
    check(calls["n"] <= 3, f"unfillable hole caused {calls['n']} requests")
    check(len(store.candle_gaps(INST, BAR, STEP)) == 1,
          "hole should still be reported, just not looped over")
    store.close(); tmp.cleanup()


TESTS = [value for name, value in sorted(globals().items())
         if name.startswith("test_") and callable(value)]

if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(TESTS)} tests, {_checks} checks — all green")
