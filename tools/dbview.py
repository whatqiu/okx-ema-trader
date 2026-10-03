#!/usr/bin/env python
"""查看 SQLite 数据库的小工具（只读）。

为什么需要它：
  1. SQLite 的 `.db` / `.db-wal` / `.db-shm` 三件套必须由 sqlite3 自己打开，
     手动文本编辑器打开 `.db` 只会看到一堆二进制乱码。
  2. `-wal` 里存着尚未合并回主库的新数据，直接复制 `.db` 单个文件会丢数据。
  3. 用法：只读连接（mode=ro），不会锁库、不干扰正在运行的交易程序。

用法：
    python tools/dbview.py                      # 概览：所有表 + 行数
    python tools/dbview.py orders               # 看某张表的最近 10 行
    python tools/dbview.py orders --sql "SELECT status, COUNT(*) FROM orders GROUP BY status"
    python tools/dbview.py --sql "SELECT * FROM equity ORDER BY id DESC LIMIT 5"
    python tools/dbview.py --raw "SELECT * FROM candles WHERE bar='15m' LIMIT 3"   # 竖排显示
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "platform.db"

# 建表语句见 src/okx_ema_trader/storage.py 的 SCHEMA 常量；
# 主键推断用于给没有显式列名的查询排序。
DEFAULT_ORDER = {
    "candles": "ts",
    "ticks": "updated_at",
    "orders": "id",
    "signals": "ts",
    "backtests": "id",
    "equity": "id",
    "meta": "key",
}


def connect(path: Path, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        # uri 形式 + mode=ro：纯读，不建锁、不写 journal
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(path), timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn


def list_tables(conn: sqlite3.Connection) -> None:
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    print(f"数据库：{DB_PATH}")
    print(f"{'表名':<12}{'行数':>10}")
    print("-" * 22)
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        print(f"{t:<12}{n:>10,}")
    print(f"\n共 {len(tables)} 张表。")


def run_sql(conn: sqlite3.Connection, sql: str, *, raw: bool = False) -> None:
    cur = conn.execute(sql)
    rows = cur.fetchall()
    if not rows:
        print("(无结果)")
        return
    cols = rows[0].keys()
    if raw:
        for r in rows:
            print("-" * 40)
            for c in cols:
                print(f"{c:<12}: {r[c]}")
        return
    widths = {
        c: min(
            28,
            max(
                len(str(c)),
                max((len(str(r[c])) for r in rows[:200]), default=0),
            ),
        )
        for c in cols
    }
    print(" | ".join(str(c).ljust(widths[c]) for c in cols))
    print("-" * (sum(widths.values()) + 3 * (len(cols) - 1)))
    for r in rows:
        print(" | ".join(str(r[c])[:28].ljust(widths[c]) for c in cols))
    print(f"\n{len(rows)} 行。")


def preview_table(conn: sqlite3.Connection, table: str, limit: int) -> None:
    order = DEFAULT_ORDER.get(table, "rowid")
    run_sql(conn, f"SELECT * FROM {table} ORDER BY {order} DESC LIMIT {limit}")


def main() -> int:
    ap = argparse.ArgumentParser(description="只读查看 SQLite 数据库")
    ap.add_argument("table", nargs="?", help="表名，省略则显示概览")
    ap.add_argument("--sql", help="自定义 SQL 查询")
    ap.add_argument("--raw", action="store_true", help="竖排显示（字段多时用）")
    ap.add_argument("--limit", type=int, default=10, help="预览行数，默认 10")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="数据库路径")
    ap.add_argument(
        "-w", "--write", action="store_true", help="可写模式（默认只读，慎用）"
    )
    args = ap.parse_args()

    if not args.db.exists():
        print(f"找不到数据库：{args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db, readonly=not args.write)
    try:
        if args.sql:
            run_sql(conn, args.sql, raw=args.raw)
        elif args.table:
            preview_table(conn, args.table, args.limit)
        else:
            list_tables(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())