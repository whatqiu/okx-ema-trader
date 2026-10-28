"""Offline rankings: calendar boundaries, partial coverage, and shared work."""
import json
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "platform" / "backend"))

import rankings
import market
import main


class MemoryStore:
    def __init__(self):
        self.meta = {}

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value


def candle(date, opened=100, closed=120, confirm="1"):
    ts = int(datetime.fromisoformat(date).replace(tzinfo=rankings.TZ).timestamp() * 1000)
    return [str(ts), str(opened), "125", "75", str(closed), "10", "10", "1000", confirm]


def test_calendar_days_and_bad_candles():
    rows = rankings.daily_rows("BTC-USDT-SWAP", [
        candle("2026-10-27"), candle("2026-10-26", closed=80),
        candle("2026-10-28"), candle("2026-10-20"),
        candle("2026-10-25", confirm="0"), candle("2026-10-24", opened=0),
        candle("2026-10-23", closed="NaN"), [],
    ], "2026-10-28")
    assert set(rows) == {"2026-10-27", "2026-10-26"}
    assert rows["2026-10-27"]["change"] == .2
    assert rows["2026-10-26"]["change"] == -.2
    assert rankings.day_key(1793116800) == datetime.fromtimestamp(1793116800, rankings.TZ).date().isoformat()


def test_rankings_signs_invalid_numbers_and_ties():
    rows = [{"inst_id": name, "change": val} for name, val in
            [("B", .3), ("A", .3), ("C", -.5), ("D", -.1),
             ("E", 0), ("F", float("nan")), ("G", float("inf")), ("H", None)]]
    result = rankings.leaders(rows, "change", 2)
    assert [r["inst_id"] for r in result["gainers"]] == ["A", "B"]
    assert [r["inst_id"] for r in result["losers"]] == ["C", "D"]
    assert result["covered"] == 5
    assert rankings.leaders(rows[:2], "change")["losers"] == []


def test_ticker_cache_expires_at_beijing_midnight():
    market.reset_swap_ticker_cache()
    midnight = datetime(2026, 10, 28, tzinfo=rankings.TZ).timestamp()
    try:
        with patch.object(market.time, "time", side_effect=[midnight - 1, midnight]), \
                patch.object(market.time, "monotonic", return_value=100), \
                patch.object(market.deps, "okx_get", return_value=[]) as fetch:
            market.swap_tickers()
            market.swap_tickers()
            assert fetch.call_count == 2
    finally:
        market.reset_swap_ticker_cache()


def test_history_single_worker_persisted_cache_and_partial_failure():
    service, store = rankings.HistoryRankings(), MemoryStore()
    yesterday = (datetime.now(rankings.TZ) - timedelta(days=1)).date().isoformat()
    gate, entered = threading.Event(), threading.Event()
    calls = []

    def fetch(path, params):
        calls.append(params["instId"])
        entered.set()
        assert gate.wait(3)
        if params["instId"] == "BAD-USDT-SWAP":
            raise RuntimeError("offline")
        assert params["bar"] == "1D"
        return [candle(yesterday)]

    instruments = [{"inst_id": name} for name in ["BTC-USDT-SWAP", "BAD-USDT-SWAP"]]
    with patch.object(service.stop, "wait", return_value=False):
        first = service.snapshot(store, instruments, fetch=fetch)
        assert first["loading"]
        assert entered.wait(3)
        service.snapshot(store, instruments, fetch=fetch)
        assert len(calls) == 1
        gate.set()
        service.worker.join(3)
    assert not service.worker.is_alive()
    result = service.snapshot(store, instruments, fetch=fetch)
    assert not result["loading"]
    assert result["failed"] == 1 and result["processed"] == 2
    assert result["days"][0]["covered"] == 1
    assert len(result["days"]) == 7
    restored = rankings.HistoryRankings()
    assert restored.snapshot(store, instruments, fetch=fetch)["days"] == result["days"]
    assert len(calls) == 2
    json.dumps(result, allow_nan=False)


def test_outage_stops_after_five_errors():
    service, store = rankings.HistoryRankings(), MemoryStore()
    instruments = [{"inst_id": f"X{i}-USDT-SWAP"} for i in range(20)]
    with patch.object(service.stop, "wait", return_value=False):
        service.snapshot(store, instruments, fetch=lambda *args: (_ for _ in ()).throw(RuntimeError()))
        service.worker.join(3)
    result = service.snapshot(store, instruments)
    assert result["processed"] == 5
    assert result["failed"] == 5
    assert result["error"] and not result["loading"]


def test_route_separates_today_from_24h_and_quotes_volume():
    market.reset_swap_ticker_cache()
    try:
        rows = market.swap_tickers(fetch=lambda *args: [{
            "instId": "BTC-USDT-SWAP", "last": "120", "open24h": "100",
            "sodUtc8": "150", "volCcy24h": "10", "ts": "123"}])
        assert rows[0]["change24h"] == .2
        assert rows[0]["change_today"] == -.2
        assert rows[0]["vol_ccy24h"] == 1200
        with patch.object(main.deps, "store", return_value=MemoryStore()), \
                patch.object(main.rankings.history, "snapshot", return_value={"days": []}):
            result = main.market_rankings()
        assert result["rolling"]["gainers"][0]["base"] == "BTC"
        assert result["today"]["losers"][0]["base"] == "BTC"
    finally:
        market.reset_swap_ticker_cache()
