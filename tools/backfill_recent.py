"""One-off: backfill candles from the last stored ts to now, then report.

Reads the system proxy live (the port keeps changing: 6088 -> 10888 -> 6088),
pages OKX /market/candles oldest-first, and upserts through Store so a
half-formed newest bar is corrected rather than duplicated.
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from okx_ema_trader import http
from okx_ema_trader.storage import Store

SYMBOLS = ["ZEC-USDT-SWAP", "ETH-USDT-SWAP", "MU-USDT-SWAP",
           "NVDA-USDT-SWAP", "SPCX-USDT-SWAP", "BTC-USDT-SWAP"]
BARS = {"5m": 5 * 60_000, "15m": 15 * 60_000}
PROXY = http.system_proxy()


def fetch_backfill(store: Store, inst: str, bar: str, step_ms: int) -> tuple[int, int]:
    extent = store.candle_extent(inst, bar)
    last_ts = extent[1] if extent else 0
    now_ms = int(time.time() * 1000)
    if last_ts and now_ms - last_ts < step_ms:
        return 0, 0
    written, pages = 0, 0
    after = None  # newest-first paging: `after` returns records older than ts
    while pages < 40:
        params = {"instId": inst, "bar": bar, "limit": "300"}
        if after:
            params["after"] = str(after)
        data = http.get_json("/market/candles", params=params, proxy=PROXY, timeout=15)
        if not data:
            break
        store.upsert_candles(inst, bar, data)
        written += len(data)
        pages += 1
        oldest = int(data[-1][0])
        if last_ts and oldest <= last_ts:
            break
        after = oldest
        if not last_ts:  # fresh symbol: keep walking back at most 40 pages
            pass
        time.sleep(0.15)
    return written, pages


def main() -> None:
    store = Store(Path(__file__).resolve().parents[1] / "data" / "platform.db")
    for inst in SYMBOLS:
        for bar, step in BARS.items():
            try:
                n, pages = fetch_backfill(store, inst, bar, step)
                extent = store.candle_extent(inst, bar)
                print(f"{inst:<18} {bar:<4} +{n:>5} rows ({pages} pages) "
                      f"extent now {extent[0]} -> {extent[1]}")
            except Exception as e:
                print(f"{inst:<18} {bar:<4} FAIL {type(e).__name__}: {str(e)[:100]}")


if __name__ == "__main__":
    main()
