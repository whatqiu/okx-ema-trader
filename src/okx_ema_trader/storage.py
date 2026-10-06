"""Local persistence for the trading platform.

Everything the platform shows is served from this store; OKX is only asked for
what is missing. That matters because the market-data limit is 20 requests per
2 seconds per IP: pulling 30 days of 5m candles is ~29 paginated calls, so
re-downloading them on every page load would burn the whole budget in one
refresh and get us rate-limited.

SQLite via stdlib `sqlite3` — no new dependency, one file, survives restarts,
and UPSERT makes backfills idempotent.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parents[2] / "data" / "platform.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    inst_id TEXT    NOT NULL,
    bar     TEXT    NOT NULL,
    ts      INTEGER NOT NULL,
    open    REAL    NOT NULL,
    high    REAL    NOT NULL,
    low     REAL    NOT NULL,
    close   REAL    NOT NULL,
    vol     REAL    NOT NULL DEFAULT 0,
    vol_ccy REAL,
    confirm INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (inst_id, bar, ts)
);
CREATE INDEX IF NOT EXISTS candles_scan ON candles (inst_id, bar, ts);

CREATE TABLE IF NOT EXISTS ticks (
    inst_id    TEXT PRIMARY KEY,
    last       REAL,
    open24h    REAL,
    high24h    REAL,
    low24h     REAL,
    vol24h     REAL,
    vol_ccy24h REAL,
    bid        REAL,
    ask        REAL,
    ts         INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id       TEXT NOT NULL,
    side          TEXT NOT NULL,
    contracts     REAL,
    entry_price   REAL,
    exit_price    REAL,
    notional      REAL,
    leverage      REAL,
    stop_price    REAL,
    order_id      TEXT,
    stop_order_id TEXT,
    status        TEXT NOT NULL,
    pnl           REAL,
    pnl_pct       REAL,
    reason        TEXT,
    opened_at     INTEGER,
    closed_at     INTEGER,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_inst ON orders (inst_id, created_at);

CREATE TABLE IF NOT EXISTS signals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    inst_id   TEXT NOT NULL,
    side      TEXT,
    reason    TEXT,
    price     REAL,
    ema_fast  REAL,
    ema_slow  REAL,
    adx       REAL,
    deviation REAL,
    acted     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS signals_ts ON signals (ts);

CREATE TABLE IF NOT EXISTS backtests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  INTEGER NOT NULL,
    inst_id     TEXT NOT NULL,
    days        INTEGER,
    params_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);

-- ts is NOT the primary key: two snapshots inside the same millisecond would
-- overwrite each other, silently shortening the curve.
CREATE TABLE IF NOT EXISTS equity (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    equity REAL NOT NULL,
    note   TEXT
);
CREATE INDEX IF NOT EXISTS equity_ts ON equity (ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    """Thread-safe SQLite store.

    One connection per thread: sqlite3 objects default to
    `check_same_thread=True`, and the platform serves requests from uvicorn's
    thread pool while a background poller writes at the same time. WAL lets the
    reader and the writer run without blocking each other.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ---------------------------------------------------------------- plumbing
    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=20.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=20000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ----------------------------------------------------------------- candles
    def upsert_candles(self, inst_id: str, bar: str, rows) -> int:
        """Insert or refresh candle rows. Returns how many were written.

        `rows` are OKX-shaped:
        [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm].
        `confirm` is index 8, NOT 7 — reading index 7 picks up volCcyQuote,
        a string like "12345.6" that never equals "1", which would mark every
        confirmed bar as unconfirmed and empty any only_confirmed query.
        Re-fetching an overlapping window is safe — the primary key makes it an
        upsert, so a half-formed newest bar gets corrected on the next poll
        instead of being duplicated.
        """
        payload = []
        for row in rows:
            if len(row) < 5:
                continue
            confirm = row[8] if len(row) > 8 else "1"
            payload.append((
                inst_id, bar, int(row[0]), float(row[1]), float(row[2]),
                float(row[3]), float(row[4]),
                float(row[5]) if len(row) > 5 and row[5] else 0.0,
                float(row[6]) if len(row) > 6 and row[6] else None,
                1 if str(confirm) == "1" else 0,
            ))
        if not payload:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO candles "
                "(inst_id, bar, ts, open, high, low, close, vol, vol_ccy, confirm) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", payload)
        return len(payload)

    def candles(self, inst_id: str, bar: str, limit: int = 500,
                end_ts: int | None = None, only_confirmed: bool = False) -> list[dict]:
        """Most recent `limit` candles, oldest first."""
        where = ["inst_id = ?", "bar = ?"]
        params: list = [inst_id, bar]
        if end_ts is not None:
            where.append("ts <= ?")
            params.append(int(end_ts))
        if only_confirmed:
            where.append("confirm = 1")
        sql = ("SELECT ts, open, high, low, close, vol, vol_ccy, confirm FROM candles "
               f"WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?")
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in reversed(rows)]

    def candle_extent(self, inst_id: str, bar: str) -> tuple[int, int, int]:
        """(oldest_ts, newest_ts, count) — empty range reads as (0, 0, 0)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(*) FROM candles WHERE inst_id=? AND bar=?",
                (inst_id, bar)).fetchone()
        if not row or not row[2]:
            return 0, 0, 0
        return int(row[0]), int(row[1]), int(row[2])

    def candle_gaps(self, inst_id: str, bar: str, step_ms: int,
                    limit: int = 20) -> list[tuple[int, int]]:
        """Interior holes in the stored series, as (last_bar_before, first_bar_after).

        A hole is a jump between two STORED bars wider than one bar — the
        signature of "the process was off" or "OKX was unreachable for a while".

        Why this exists when `candle_extent` already reports oldest/newest/count:
        extent cannot see a hole. The moment the newest bar is refreshed to
        "now" after a reconnect, the series looks complete from the outside
        (oldest is old, newest is fresh, count is high) while the middle is
        missing — and neither the forward fill (newest is current) nor the
        backward fill (it pages away from `oldest`) will ever touch it.

        Newest-first and capped: the job is to heal a recent outage, not to
        reconstruct the whole listing history on every chart load.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts FROM candles WHERE inst_id=? AND bar=? ORDER BY ts",
                (inst_id, bar)).fetchall()
        gaps: list[tuple[int, int]] = []
        previous: int | None = None
        for (raw_ts,) in rows:
            ts = int(raw_ts)
            if previous is not None and ts - previous > step_ms:
                gaps.append((previous, ts))
            previous = ts
        gaps.reverse()
        return gaps[:limit]

    def candle_rows(self, inst_id: str, bar: str, limit: int = 5000,
                    only_confirmed: bool = False) -> list[list[float]]:
        """Candles shaped exactly like `backtest.fetch_history` returns:
        `[ts, open, high, low, close, vol]`.

        Matching that shape is the point — `indicators.indicator_frame` takes a
        list of rows, so stored candles and freshly fetched ones are drop-in
        replacements and a backtest cannot quietly behave differently depending
        on whether the data came from disk or from OKX.
        """
        return [[float(row["ts"]), float(row["open"]), float(row["high"]),
                 float(row["low"]), float(row["close"]), float(row["vol"])]
                for row in self.candles(inst_id, bar, limit,
                                        only_confirmed=only_confirmed)]

    # ------------------------------------------------------------------- ticks
    def save_tick(self, inst_id: str, tick: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ticks "
                "(inst_id, last, open24h, high24h, low24h, vol24h, vol_ccy24h, "
                " bid, ask, ts, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (inst_id,
                 tick.get("last"), tick.get("open24h"), tick.get("high24h"),
                 tick.get("low24h"), tick.get("vol24h"), tick.get("vol_ccy24h"),
                 tick.get("bid"), tick.get("ask"), tick.get("ts"), _now_ms()))

    def tick(self, inst_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ticks WHERE inst_id=?", (inst_id,)).fetchone()
        return dict(row) if row else None

    def ticks(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM ticks").fetchall()]

    # ------------------------------------------------------------------ orders
    def add_order(self, inst_id: str, side: str, *, contracts: float | None = None,
                  entry_price: float | None = None, notional: float | None = None,
                  leverage: float | None = None, stop_price: float | None = None,
                  order_id: str | None = None, stop_order_id: str | None = None,
                  status: str = "filled", reason: str | None = None,
                  opened_at: int | None = None) -> int:
        stamp = opened_at or _now_ms()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO orders (inst_id, side, contracts, entry_price, notional, "
                "leverage, stop_price, order_id, stop_order_id, status, reason, "
                "opened_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (inst_id, side, contracts, entry_price, notional, leverage,
                 stop_price, order_id, stop_order_id, status, reason, stamp, _now_ms()))
            return int(cur.lastrowid)

    def close_order(self, order_id: int, exit_price: float, *,
                    pnl: float | None = None, pnl_pct: float | None = None,
                    reason: str | None = None, status: str = "closed") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE orders SET exit_price=?, pnl=?, pnl_pct=?, reason=?, "
                "status=?, closed_at=? WHERE id=?",
                (exit_price, pnl, pnl_pct, reason, status, _now_ms(), order_id))

    def open_orders(self, inst_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM orders WHERE status='filled'"
        params: list = []
        if inst_id:
            sql += " AND inst_id=?"
            params.append(inst_id)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY opened_at", params)]

    def orders(self, limit: int = 100, inst_id: str | None = None,
               status: str | None = None) -> list[dict]:
        """`status`: "open" (= stored 'filled'), "closed", or None for both."""
        where: list[str] = []
        params: list = []
        if inst_id:
            where.append("inst_id=?")
            params.append(inst_id)
        if status == "open":
            where.append("status='filled'")
        elif status == "closed":
            where.append("status='closed'")
        sql = "SELECT * FROM orders"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                sql + " ORDER BY created_at DESC LIMIT ?", [*params, int(limit)])]

    def known_symbols(self) -> list[str]:
        """Every symbol we hold any local data for.

        Offline fallback for the symbol search: a dead proxy should shrink the
        list to what we already track, not blank it out entirely.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT inst_id FROM ticks UNION SELECT inst_id FROM candles "
                "UNION SELECT inst_id FROM orders ORDER BY 1").fetchall()
        return [r["inst_id"] for r in rows]

    # ----------------------------------------------------------------- signals
    def add_signal(self, inst_id: str, side: str | None, reason: str, *,
                   price: float | None = None, ema_fast: float | None = None,
                   ema_slow: float | None = None, adx: float | None = None,
                   deviation: float | None = None, ts: int | None = None,
                   acted: bool = False) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO signals (ts, inst_id, side, reason, price, ema_fast, "
                "ema_slow, adx, deviation, acted) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts or _now_ms(), inst_id, side, reason, price, ema_fast,
                 ema_slow, adx, deviation, 1 if acted else 0))
            return int(cur.lastrowid)

    def signals(self, limit: int = 100, inst_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM signals"
        params: list = []
        if inst_id:
            sql += " WHERE inst_id=?"
            params.append(inst_id)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                sql + " ORDER BY ts DESC LIMIT ?", [*params, int(limit)])]

    # --------------------------------------------------------------- backtests
    def save_backtest(self, inst_id: str, params: dict, result: dict) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO backtests (created_at, inst_id, days, params_json, result_json) "
                "VALUES (?,?,?,?,?)",
                (_now_ms(), inst_id, params.get("days"),
                 json.dumps(params, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False)))
            return int(cur.lastrowid)

    def backtests(self, limit: int = 30, inst_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM backtests"
        params: list = []
        if inst_id:
            sql += " WHERE inst_id=?"
            params.append(inst_id)
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?",
                                [*params, int(limit)]).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["params"] = json.loads(item.pop("params_json"))
            item["result"] = json.loads(item.pop("result_json"))
            out.append(item)
        return out

    # ------------------------------------------------------------------ equity
    def add_equity(self, equity: float, note: str | None = None,
                   ts: int | None = None) -> int:
        with self._connect() as conn:
            cur = conn.execute("INSERT INTO equity (ts, equity, note) VALUES (?,?,?)",
                               (ts or _now_ms(), float(equity), note))
            return int(cur.lastrowid)

    def equity_series(self, limit: int = 500) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT ts, equity, note FROM equity ORDER BY ts DESC, id DESC LIMIT ?",
                (int(limit),)).fetchall()][::-1]

    # -------------------------------------------------------------------- meta
    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                         (key, value))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ------------------------------------------------------------------- admin
    def prune(self, candle_days: int = 45, keep_signals: int = 500,
              keep_orders: int = 1000, keep_equity: int = 2000,
              keep_backtests: int = 50) -> dict:
        """Retention sweep + disk reclaim. Returns per-table delete counts.

        Why 45 days for candles and not fewer: the 30-day backtest is a core
        feature, and deleting data it needs just forces a re-download through
        the 20req/2s rate limit — the cleanup would cost more than it saves.
        45d of 5m bars ≈ 13k rows ≈ a few MB, which is nothing.

        Open positions (status='filled') are NEVER deleted regardless of age:
        losing track of a live position is the one unforgivable cleanup bug.

        DELETE only marks pages free — without wal_checkpoint(TRUNCATE) the
        -wal file keeps growing, and without VACUUM the main file never
        shrinks. Both are done here, which is exactly why this is a
        maintenance routine and not something to run per request.
        """
        cutoff = _now_ms() - candle_days * 86_400_000
        deleted: dict[str, int] = {}
        with self._connect() as conn:
            deleted["candles"] = conn.execute(
                "DELETE FROM candles WHERE ts < ?", (cutoff,)).rowcount
            deleted["signals"] = conn.execute(
                "DELETE FROM signals WHERE id NOT IN "
                "(SELECT id FROM signals ORDER BY ts DESC, id DESC LIMIT ?)",
                (keep_signals,)).rowcount
            deleted["orders"] = conn.execute(
                "DELETE FROM orders WHERE status != 'filled' AND id NOT IN "
                "(SELECT id FROM orders WHERE status != 'filled' "
                " ORDER BY created_at DESC, id DESC LIMIT ?)",
                (keep_orders,)).rowcount
            deleted["equity"] = conn.execute(
                "DELETE FROM equity WHERE id NOT IN "
                "(SELECT id FROM equity ORDER BY ts DESC, id DESC LIMIT ?)",
                (keep_equity,)).rowcount
            deleted["backtests"] = conn.execute(
                "DELETE FROM backtests WHERE id NOT IN "
                "(SELECT id FROM backtests ORDER BY created_at DESC, id DESC LIMIT ?)",
                (keep_backtests,)).rowcount
        # The with-block just committed; only NOW can checkpoint/VACUUM run.
        # Inside the block they deadlock against our own uncommitted write
        # transaction ("database table is locked" — caught by the tests).
        conn = self._connect()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        return deleted

    def file_sizes(self) -> dict:
        """Sizes of the db + WAL sidecars, for the admin UI."""
        out = {"db": 0, "wal": 0, "shm": 0}
        for suffix, key in (("", "db"), ("-wal", "wal"), ("-shm", "shm")):
            p = Path(str(self.path) + suffix)
            if p.exists():
                out[key] = p.stat().st_size
        return out

    def stats(self) -> dict:
        with self._connect() as conn:
            counts = {}
            for table in ("candles", "ticks", "orders", "signals", "backtests", "equity"):
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        sizes = self.file_sizes()
        return {"path": str(self.path), "size_bytes": sum(sizes.values()),
                "files": sizes, "counts": counts}
