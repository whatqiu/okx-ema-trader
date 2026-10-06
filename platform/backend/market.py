"""Server-side market data: local-first candles with gap backfill from OKX.

The rule the whole platform obeys: OKX is asked only for what SQLite does not
already have. 20 requests per 2 seconds per IP is the public market-data
budget, and a single chart refresh wanting 30 days of 5m bars is ~29 paginated
calls — re-downloading on every page load would burn the budget in one F5.

Two gap shapes are handled differently because they are different problems:

  * **Backward gap** (database has too little history): page backwards with
    `after` = strictly older than the oldest stored ts. Idempotent because
    `upsert_candles` keys on (inst_id, bar, ts).
  * **Forward gap** (server was off, data is stale): page forwards with
    `before` = strictly newer than the newest stored ts, until we catch up
    with "now". Without this a stale DB would otherwise only ever refresh
    its newest 300 bars and silently keep a hole in the middle.

The forming bar (confirm=0) is stored too — the chart wants it — but every
consumer that makes decisions reads with `only_confirmed=True`.
"""
from __future__ import annotations

import logging
import threading
import time

import deps
from okx_ema_trader.http import OkxError

log = logging.getLogger("platform")   # same logger as main.py -> one log file

# Mirrors backtest.MINUTES_PER_BAR, in milliseconds; upper-case H/D included
# because OKX spells them that way.
BAR_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1H": 3_600_000, "4H": 14_400_000,
    "1D": 86_400_000,
}
MAX_LIMIT = 300
# Hard cap on pages per ensure_candles call: a runaway loop here is a rate-limit
# ban, not just wasted time. 40 pages x 300 bars = 12,000 bars = 41 days of 5m.
MAX_PAGES = 40
# Budget for the automatic heal when the poller recovers from an outage: 10
# pages x 300 bars = 3000 bars = ~10 days of 5m. Enough for a laptop that was
# asleep, small enough that four symbols x two bars coming back at once cannot
# turn into a burst that eats the whole rate-limit budget.
HEAL_PAGES = 10


def bar_ms(bar: str) -> int:
    if bar not in BAR_MS:
        raise ValueError(f"unsupported bar {bar!r}; valid: {sorted(BAR_MS)}")
    return BAR_MS[bar]


def _f(value):
    """OKX sends numbers as strings and "" for missing; both must not crash."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def refresh_latest(store, inst_id: str, bar: str, fetch=None, limit: int = 3) -> int:
    """Re-write the newest bars, including the still-forming one.

    The forming bar's OHLC moves until it closes; UPSERT corrects it in place
    on the next poll instead of appending phantom duplicates.
    """
    fetch = fetch or deps.okx_get
    rows = fetch("/market/candles",
                 {"instId": inst_id, "bar": bar, "limit": str(limit)})
    return store.upsert_candles(inst_id, bar, rows)


def _fill_gap(store, inst_id: str, bar: str, gap_start: int, gap_end: int,
              fetch, budget: int) -> tuple[int, int]:
    """Fill the bars strictly between `gap_start` and `gap_end` (both stored).

    `after` pages newest-first going back in time, so each page starts just
    below `gap_end`; the next cursor is the oldest bar the page returned. Only
    the part of a page that falls inside the hole is written — the rest is
    already stored and re-writing it would just spend rows.

    Returns (bars written, pages used). A page that adds nothing ends the
    attempt: some holes are genuinely unfillable (the instrument did not trade,
    or the bar predates listing) and paging forever at those is how a budget
    becomes a rate-limit ban.
    """
    written = 0
    used = 0
    cursor = gap_end
    for _ in range(budget):
        page = fetch("/market/history-candles", {
            "instId": inst_id, "bar": bar, "limit": str(MAX_LIMIT),
            "after": str(cursor),
        })
        used += 1
        if not page:
            break
        inside = [row for row in page
                  if gap_start < int(row[0]) < gap_end]
        if not inside:
            break
        written += store.upsert_candles(inst_id, bar, inside)
        page_oldest = int(page[-1][0])
        if page_oldest <= gap_start or len(page) < MAX_LIMIT:
            break
        cursor = page_oldest
    return written, used


def ensure_candles(store, inst_id: str, bar: str, target_bars: int = 1500,
                   fetch=None, max_pages: int = MAX_PAGES) -> dict:
    """Make the store hold ~`target_bars` recent bars, fetching only the gaps.

    Returns the post-sync extent {oldest, newest, count, fetched}.

    Three phases, sharing ONE page budget (`max_pages`), because the budget is
    a rate-limit concern, not a per-phase concern: a chart load that spends 40
    pages on history and then discovers a hole has nothing left to heal it with.
    """
    fetch = fetch or deps.okx_get
    step = bar_ms(bar)
    now = int(time.time() * 1000)
    oldest, newest, count = store.candle_extent(inst_id, bar)
    fetched = 0
    budget = max_pages

    # ---- 1. forward fill: local data exists but is stale ------------------
    if count and newest < now - 2 * step:
        before = newest
        for _ in range(budget):
            page = fetch("/market/history-candles", {
                "instId": inst_id, "bar": bar, "limit": str(MAX_LIMIT),
                "before": str(before),
            })
            budget -= 1
            if not page:
                break
            fetched += store.upsert_candles(inst_id, bar, page)
            page_newest = int(page[0][0])  # OKX pages are newest-first
            if page_newest <= before:
                break  # no forward progress -> stop rather than spin
            before = page_newest
            if page_newest >= now - step or len(page) < MAX_LIMIT:
                break
        oldest, newest, count = store.candle_extent(inst_id, bar)

    # ---- 2. backward fill: not enough history ------------------------------
    while count < target_bars and budget > 0:
        params = {"instId": inst_id, "bar": bar, "limit": str(MAX_LIMIT)}
        if count:
            params["after"] = str(oldest)
        page = fetch("/market/history-candles", params)
        budget -= 1
        if not page:
            break
        fetched += store.upsert_candles(inst_id, bar, page)
        page_oldest = int(page[-1][0])
        if len(page) < MAX_LIMIT or (count and page_oldest >= oldest):
            break  # OKX ran out, or we are not moving backwards anymore
        oldest = page_oldest
        count += len(page)

    # ---- 2b. interior gaps: the outage case --------------------------------
    # This is the one the other two phases cannot reach. After a disconnection
    # the poller's refresh_latest jumps `newest` to "now" on the first
    # successful pass, so phase 1 sees fresh data and stops, while phase 2
    # pages AWAY from `oldest` — the hole in the middle is invisible to both
    # and stayed there permanently until this existed.
    holes = 0
    for gap_start, gap_end in store.candle_gaps(inst_id, bar, step):
        if budget <= 0:
            log.warning("%s %s: 还有缺口未补（本轮页预算已用尽）", inst_id, bar)
            break
        missing = (gap_end - gap_start) // step - 1
        written, used = _fill_gap(store, inst_id, bar, gap_start, gap_end,
                                  fetch, budget)
        budget -= used
        fetched += written
        if written:
            holes += 1
            log.info("%s %s: 补洞 %d/%d 根（%s -> %s）", inst_id, bar,
                     written, missing, gap_start, gap_end)

    # ---- 3. always refresh the newest few bars (forming bar included) -----
    fetched += refresh_latest(store, inst_id, bar, fetch)

    oldest, newest, count = store.candle_extent(inst_id, bar)
    return {"oldest": oldest, "newest": newest, "count": count,
            "fetched": fetched, "holes_filled": holes}


def fetch_ticker(store, inst_id: str, fetch=None) -> dict:
    """Pull the 24h ticker, persist it, return a cleaned dict."""
    fetch = fetch or deps.okx_get
    data = fetch("/market/ticker", {"instId": inst_id})
    if not data:
        raise OkxError(f"OKX 没有返回 {inst_id} 的 ticker", "api")
    raw = data[0]
    tick = {
        "inst_id": inst_id,
        "last": _f(raw.get("last")),
        "open24h": _f(raw.get("open24h")),
        "high24h": _f(raw.get("high24h")),
        "low24h": _f(raw.get("low24h")),
        "vol24h": _f(raw.get("vol24h")),
        "vol_ccy24h": _f(raw.get("volCcy24h")),
        "bid": _f(raw.get("bidPx")),
        "ask": _f(raw.get("askPx")),
        "ts": int(raw.get("ts") or 0),
    }
    store.save_tick(inst_id, tick)
    return tick


# --------------------------------------------------------------------------
# Instrument discovery — what the symbol search combobox queries
# --------------------------------------------------------------------------
_TICKERS_TTL = 300.0  # seconds
_tickers_cache: tuple[float, list[dict]] | None = None


def swap_tickers(fetch=None, force: bool = False) -> list[dict]:
    """All USDT-margined SWAP tickers, sorted by 24h quote volume (desc).

    Cached for 5 minutes: the full SWAP ticker list is one of the biggest
    public payloads OKX serves, and the search box fires on every debounced
    keystroke — without a cache, typing "BTC" would mean three full
    downloads of every swap on the exchange inside our 20req/2s budget.
    """
    global _tickers_cache
    now = time.monotonic()
    if not force and _tickers_cache and now - _tickers_cache[0] < _TICKERS_TTL:
        return _tickers_cache[1]
    fetch = fetch or deps.okx_get
    data = fetch("/market/tickers", {"instType": "SWAP"})
    rows = []
    for raw in data:
        inst = raw.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        last, open24 = _f(raw.get("last")), _f(raw.get("open24h"))
        vol_ccy = _f(raw.get("volCcy24h")) or 0.0
        # volCcy24h is denominated in the COIN, not in USDT. Ranking by it
        # directly lets ultra-cheap tokens (SATS, PEPE — 45 trillion units of
        # something worth 1e-8) bury BTC/ETH. Convert to quote volume.
        rows.append({
            "inst_id": inst,
            "base": inst.split("-")[0],
            "last": last,
            "vol_ccy24h": vol_ccy * last if last else 0.0,
            "change24h": (last - open24) / open24 if last and open24 else None,
        })
    rows.sort(key=lambda r: -r["vol_ccy24h"])
    _tickers_cache = (now, rows)
    return rows


def reset_swap_ticker_cache() -> None:
    """Test hook — module-level cache must not leak between test cases."""
    global _tickers_cache
    _tickers_cache = None


class MarketPoller:
    """Background thread keeping candles + tickers fresh — WITHOUT hammering.

    Two scheduling rules replace the naive "fetch everything every 5s":

    * **Candles are fetched on bar boundaries only.** A 5m bar closes every
      300s; re-downloading it 12 times a minute is 11 wasted calls. The
      poller fetches when a new bar bucket begins, retries until OKX marks
      the just-closed bar confirm=1, then sleeps until the next boundary.
      The forming bar's live shape is the frontend's job (it merges the
      ticker into the last candle client-side).

    * **Tickers live in memory, persisted once a minute.** The ticks table
      exists so a restart has *a* price — it never needed 5s granularity.
      Trading fills bypass this cache entirely (`fetch_ticker` hits OKX
      directly), so stale persistence can never become a stale fill.
    """

    TICKER_PERSIST_S = 60.0
    # Hard cap on the watch list. `/api/candles` calls `watch()` on every
    # request, so browsing the instrument list grows this without bound, and
    # every entry costs a ticker request per pass against a 20-req/2s budget.
    # Oldest non-pinned entries are dropped.
    MAX_WATCH = 40

    def __init__(self, store, interval: float = 5.0, fetch=None) -> None:
        self._store = store
        self._interval = interval
        self._fetch = fetch  # test hook; None -> deps.okx_get
        self._watch: list[tuple[str, str]] = []  # oldest first, newest last
        # Entries that must never be evicted; see `watch(pin=True)`.
        self._pinned: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.last_poll_at: int = 0
        # (inst, bar) -> {"bucket": current bar bucket, "confirmed": bool}
        self._candle_state: dict[tuple[str, str], dict] = {}
        # inst -> freshest tick (memory-hot; DB gets a copy once a minute)
        self._ticker_cache: dict[str, dict] = {}
        self._ticker_persisted: dict[str, float] = {}
        # (inst, bar) pairs that failed at least once and owe a gap-fill on the
        # first pass that succeeds again.
        self._to_heal: set[tuple[str, str]] = set()

    def watch(self, inst_id: str, bar: str, *, pin: bool = False) -> None:
        """Start polling `inst_id`/`bar`, or move it to the front if it is
        already watched.

        `pin` makes the entry unevictable. Auto-trade pins its symbols, because
        a scan that quietly stopped refreshing one of them is far worse than
        paying for an extra request; everything else is LRU and can fall off the
        end of the list.

        The LRU works only because `/api/candles` re-watches on every request:
        a chart you have open gets pushed back to the front every few seconds,
        so the entries that reach the end really are the ones nobody is looking
        at.
        """
        key = (inst_id, bar)
        with self._lock:
            if pin:
                self._pinned.add(key)
            if key in self._watch:
                self._watch.remove(key)
            self._watch.append(key)
            self._evict()

    def unwatch(self, inst_id: str, bar: str) -> None:
        key = (inst_id, bar)
        with self._lock:
            self._watch = [k for k in self._watch if k != key]
            self._pinned.discard(key)
            # Without these two the per-bar cursor and any pending gap-heal
            # outlive the entry they belong to and leak for the life of the
            # process — and a stale `confirmed` cursor would make a re-watched
            # symbol skip its first bar.
            self._candle_state.pop(key, None)
            self._to_heal.discard(key)

    def _evict(self) -> None:
        """Drop the oldest non-pinned entries over MAX_WATCH. Caller holds lock."""
        while len(self._watch) > self.MAX_WATCH:
            victim = next((k for k in self._watch if k not in self._pinned), None)
            if victim is None:
                return  # everything left is pinned: over budget, but correct
            self._watch.remove(victim)
            self._candle_state.pop(victim, None)
            self._to_heal.discard(victim)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="market-poller")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        # Flush the hot ticker cache: after a restart the ticks table is the
        # only price source until the first successful poll.
        with self._lock:
            cache = dict(self._ticker_cache)
        for inst_id, tick in cache.items():
            try:
                self._store.save_tick(inst_id, tick)
            except Exception:
                pass

    def latest_ticker(self, inst_id: str) -> dict | None:
        """Freshest known tick: memory first, DB as fallback."""
        with self._lock:
            tick = self._ticker_cache.get(inst_id)
        return tick if tick is not None else self._store.tick(inst_id)

    def _poll_ticker(self, inst_id: str) -> None:
        fetch = self._fetch or deps.okx_get
        data = fetch("/market/ticker", {"instId": inst_id})
        if not data:
            raise OkxError(f"OKX 没有返回 {inst_id} 的 ticker", "api")
        raw = data[0]
        tick = {
            "inst_id": inst_id,
            "last": _f(raw.get("last")),
            "open24h": _f(raw.get("open24h")),
            "high24h": _f(raw.get("high24h")),
            "low24h": _f(raw.get("low24h")),
            "vol24h": _f(raw.get("vol24h")),
            "vol_ccy24h": _f(raw.get("volCcy24h")),
            "bid": _f(raw.get("bidPx")),
            "ask": _f(raw.get("askPx")),
            "ts": int(raw.get("ts") or 0),
        }
        with self._lock:
            self._ticker_cache[inst_id] = tick
        now = time.monotonic()
        if now - self._ticker_persisted.get(inst_id, 0.0) >= self.TICKER_PERSIST_S:
            self._store.save_tick(inst_id, tick)
            self._ticker_persisted[inst_id] = now

    def _poll_candles(self, inst_id: str, bar: str) -> None:
        step = bar_ms(bar)
        now_ms = int(time.time() * 1000)
        bucket = now_ms // step
        key = (inst_id, bar)
        state = self._candle_state.get(key)
        if state and bucket == state["bucket"] and state["confirmed"]:
            return  # closed bar stored & confirmed; nothing to do this bucket
        refresh_latest(self._store, inst_id, bar, fetch=self._fetch)
        # Did OKX mark the bar that just closed (bucket-1) as confirmed?
        want_ts = (bucket - 1) * step
        recent = self._store.candles(inst_id, bar, limit=3)
        confirmed = any(int(r["ts"]) == want_ts and r["confirm"] == 1
                        for r in recent)
        self._candle_state[key] = {"bucket": bucket, "confirmed": confirmed}

    def poll_once(self) -> None:
        """One pass over the watch list. Exposed for tests."""
        with self._lock:
            targets = list(self._watch)
        # One ticker per SYMBOL per pass, not one per (symbol, bar): a symbol
        # watched on both 5m and 15m is still one instrument, so the old loop
        # spent two of the 20-req/2s budget to learn one price.
        tickers_done: set[str] = set()
        for inst_id, bar in targets:
            try:
                self._poll_candles(inst_id, bar)
                if inst_id not in tickers_done:
                    tickers_done.add(inst_id)
                    self._poll_ticker(inst_id)
                self.last_error = None
                if (inst_id, bar) in self._to_heal:
                    # We just came back from a failure. The bars that closed
                    # while we were offline are missing, and refreshing the
                    # newest 3 does not bring them back — without this the
                    # chart keeps a hole until someone reloads it by hand.
                    self._to_heal.discard((inst_id, bar))
                    report = ensure_candles(self._store, inst_id, bar,
                                            fetch=self._fetch,
                                            max_pages=HEAL_PAGES)
                    if report["holes_filled"]:
                        log.info("%s %s: 恢复连接后补上了 %d 段缺口",
                                 inst_id, bar, report["holes_filled"])
            except OkxError as exc:
                # A dead proxy must not kill the thread — the next pass may work,
                # and the UI reads last_error to explain stale data.
                self.last_error = str(exc)
                self._to_heal.add((inst_id, bar))
            self.last_poll_at = int(time.time() * 1000)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.poll_once()
