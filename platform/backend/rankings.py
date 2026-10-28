"""OKX USDT swap rankings; calendar days start at midnight UTC+8.

History covers the current listed universe, including low-volume swaps. A
single background worker fetches seven confirmed daily bars per instrument;
partial coverage is always exposed. No trading or account state is touched.
"""
from __future__ import annotations

import copy
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone

import deps

TZ = timezone(timedelta(hours=8))
KEY = "market_rankings_history_v1"


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def day_key(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, TZ).date().isoformat()


def leaders(rows, field, limit=10):
    valid = [r for r in rows if number(r.get(field)) is not None]
    up = sorted((r for r in valid if float(r[field]) > 0),
                key=lambda r: (-float(r[field]), r["inst_id"]))[:limit]
    down = sorted((r for r in valid if float(r[field]) < 0),
                  key=lambda r: (float(r[field]), r["inst_id"]))[:limit]
    return {"gainers": up, "losers": down, "covered": len(valid)}


def daily_rows(inst, candles, today):
    cutoff = (datetime.fromisoformat(today) - timedelta(days=7)).date().isoformat()
    rows = {}
    for candle in candles:
        try:
            if str(candle[8]) != "1":
                continue
            date = day_key(int(candle[0]) / 1000)
            opened, closed = number(candle[1]), number(candle[4])
            if not cutoff <= date < today or not opened or opened <= 0 or not closed or closed <= 0:
                continue
            change = (closed - opened) / opened
            if not math.isfinite(change):
                continue
            rows[date] = {"inst_id": inst, "base": inst.split("-")[0],
                          "last": closed, "change": change}
        except (IndexError, ValueError, TypeError, OverflowError, OSError):
            continue
    return rows


class HistoryRankings:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = None
        self.worker = None
        self.stop = threading.Event()

    def snapshot(self, store, instruments, *, fetch=None, now=None):
        now = time.time() if now is None else now
        today = day_key(now)
        with self.lock:
            if self.state is None:
                try:
                    saved = json.loads(store.get_meta(KEY, "null"))
                    if (isinstance(saved, dict) and saved.get("today") == today
                            and isinstance(saved.get("days"), dict)
                            and all(isinstance(rows, list) for rows in saved["days"].values())
                            and isinstance(saved.get("updated_at"), (int, float))
                            and {"loading", "processed", "total", "failed", "error"} <= saved.keys()
                            and not saved["loading"]):
                        self.state = saved
                except (TypeError, ValueError):
                    pass
            running = self.worker is not None and self.worker.is_alive()
            ttl = 300 if self.state and self.state.get("failed") else 21600
            if not running and (not self.state or self.state.get("today") != today
                                or now - self.state.get("updated_at", 0) >= ttl):
                self.stop.clear()
                dates = [(datetime.fromisoformat(today) - timedelta(days=i)).date().isoformat()
                         for i in range(1, 8)]
                self.state = {"today": today, "loading": True, "processed": 0,
                              "total": len(instruments), "failed": 0, "updated_at": now,
                              "days": {d: [] for d in dates}, "error": None}
                self.worker = threading.Thread(target=self.collect,
                    args=(store, list(instruments), fetch or deps.okx_get, today),
                    daemon=True, name="market-rankings")
                self.worker.start()
            return self._view(today)

    def _view(self, today):
        result = copy.deepcopy(self.state)
        result["stale"] = result["today"] != today
        result["days"] = [{"date": date, **leaders(rows, "change")}
                          for date, rows in sorted(result["days"].items(), reverse=True)]
        return result

    def collect(self, store, instruments, fetch, today):
        consecutive_errors = 0
        try:
            for instrument in instruments:
                if self.stop.is_set():
                    break
                inst = instrument["inst_id"]
                try:
                    candles = fetch("/market/candles", {"instId": inst, "bar": "1D", "limit": "8"})
                    rows = daily_rows(inst, candles, today)
                    if not rows:
                        raise ValueError("没有可用的已收盘日线")
                    with self.lock:
                        for date, row in rows.items():
                            self.state["days"][date].append(row)
                    consecutive_errors = 0
                except Exception:
                    consecutive_errors += 1
                    with self.lock:
                        self.state["failed"] += 1
                with self.lock:
                    self.state["processed"] += 1
                if consecutive_errors >= 5:
                    with self.lock:
                        self.state["error"] = "连续获取日线失败，已暂停；5 分钟后自动重试"
                    break
                # Leave the shared public API budget available to charts/risk.
                if self.stop.wait(0.25):
                    break
        finally:
            with self.lock:
                self.state["loading"] = False
                self.state["updated_at"] = time.time()
                payload = json.dumps(self.state, allow_nan=False)
            if not self.stop.is_set():
                try:
                    store.set_meta(KEY, payload)
                except Exception:
                    with self.lock:
                        self.state["error"] = "历史榜单缓存保存失败，重启后需要重新加载"


history = HistoryRankings()
