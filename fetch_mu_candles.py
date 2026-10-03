# -*- coding: utf-8 -*-
"""导出某个标的某一天的 OKX 永续 K 线，保存 CSV 并画 K 线图。

默认 = MU-USDT-SWAP 的「昨天」5m（本地时区 Asia/Shanghai）。

用法：
    python fetch_mu_candles.py                          # 昨天 MU 5m
    python fetch_mu_candles.py --date 2026-10-02        # 指定日期
    python fetch_mu_candles.py --inst BTC-USDT-SWAP --bar 15m
    python fetch_mu_candles.py --proxy http://127.0.0.1:7890   # 强制走某个代理

代理自动探测顺序：--proxy > 环境变量 > .mcp.json > 常见本地端口 > 直连。
哪个能通 OKX 用哪个，都通不了就报错让你手动指定。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

API_HOST = "https://www.okx.com"
TIME_URL = f"{API_HOST}/api/v5/public/time"
HIST_URL = f"{API_HOST}/api/v5/market/history-candles"
# OKX 对默认 urllib/requests UA 偶发 403，用浏览器 UA 更稳。
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) okx-ema-trader/1.0",
    "Accept": "application/json",
}
PAGE_LIMIT = 100
# 常见本地代理端口，配合 fake-ip 环境逐个试。
COMMON_PROXY_PORTS = (7890, 7897, 7891, 1080, 10808, 10809, 10810, 8888, 8118, 10888, 8080)


def _mcp_proxy(script_dir: str) -> str | None:
    """从 .mcp.json 的 env 里抠 HTTP(S)_PROXY。"""
    path = os.path.join(script_dir, ".mcp.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None
    for server in (cfg.get("mcpServers") or {}).values():
        env = server.get("env") or {}
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            if env.get(key):
                return env[key]
    return None


def build_session(script_dir: str, proxy_arg: str | None) -> requests.Session:
    """按优先级逐个候选探测，返回一个能连通 OKX 的 session。"""
    candidates: list[tuple[str, str | None]] = []
    if proxy_arg:
        candidates.append((f"--proxy {proxy_arg}", proxy_arg))
    env_p = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    if env_p:
        candidates.append((f"环境变量 {env_p}", env_p))
    mcp_p = _mcp_proxy(script_dir)
    if mcp_p:
        candidates.append((f".mcp.json {mcp_p}", mcp_p))
    for port in COMMON_PROXY_PORTS:
        candidates.append((f"端口 {port}", f"http://127.0.0.1:{port}"))
    candidates.append(("直连", None))

    for name, px in candidates:
        s = requests.Session()
        s.trust_env = False
        s.headers.update(HEADERS)
        if px:
            s.proxies = {"http": px, "https": px}
        try:
            r = s.get(TIME_URL, timeout=8)
            if r.status_code == 200 and r.json().get("code") == "0":
                print(f"[连接] 使用 {name}")
                return s
        except Exception:
            continue
    raise RuntimeError(
        "连不上 OKX：直连和所有候选代理都失败。\n"
        "  请确认你的代理(Clash/V2Ray)已开启，并用 --proxy http://127.0.0.1:端口 指定。"
    )


def day_range_ms(date_str: str | None, tz_name: str) -> tuple[int, int, str]:
    """返回 (start_ms, end_ms, 标签)。end 为开区间。默认=该时区昨天。"""
    tz = ZoneInfo(tz_name)
    if date_str:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    else:
        d = (datetime.now(tz) - timedelta(days=1)).date()
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), d.isoformat()


def fetch_day(session: requests.Session, inst: str, bar: str,
              start_ms: int, end_ms: int) -> list[list[str]]:
    """用 after 游标向前翻页，凑齐 [start_ms, end_ms) 的全部 K 线。"""
    rows: dict[int, list[str]] = {}
    cursor = end_ms
    for _ in range(60):  # 安全上限
        params = {"instId": inst, "bar": bar, "limit": str(PAGE_LIMIT), "after": str(cursor)}
        r = session.get(HIST_URL, params=params, timeout=15)
        payload = r.json()
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX 返回错误 {payload.get('code')}: {payload.get('msg')}"
                               f"（instId={inst} 可能不存在或参数有误）")
        data = payload.get("data") or []
        if not data:
            break
        for c in data:
            rows[int(c[0])] = c
        oldest = min(int(c[0]) for c in data)
        if oldest <= start_ms or len(data) < PAGE_LIMIT:
            break
        if oldest >= cursor:  # 没有前进，防死循环
            break
        cursor = oldest
        time.sleep(0.12)  # 限速
    out = [rows[ts] for ts in sorted(rows) if start_ms <= ts < end_ms]
    return out


def main() -> int:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="导出 OKX 某标的某一天的 K 线并画图")
    ap.add_argument("--inst", default="MU-USDT-SWAP")
    ap.add_argument("--bar", default="5m")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，默认昨天（按 --tz 时区）")
    ap.add_argument("--tz", default="Asia/Shanghai")
    ap.add_argument("--proxy", default=None, help="如 http://127.0.0.1:7890")
    ap.add_argument("--outdir", default=os.path.join(script_dir, "data"))
    ap.add_argument("--no-open", action="store_true", help="生成后不自动打开图片")
    args = ap.parse_args()

    start_ms, end_ms, label = day_range_ms(args.date, args.tz)
    session = build_session(script_dir, args.proxy)
    candles = fetch_day(session, args.inst, args.bar, start_ms, end_ms)
    if not candles:
        print(f"没取到数据：{args.inst} {args.bar} 在 {label} ({args.tz}) 没有任何 K 线。")
        return 2

    tz = ZoneInfo(args.tz)
    recs = []
    for c in candles:  # [ts,o,h,l,vol,volCcy,volCcyQuote,confirm]
        ts = int(c[0])
        recs.append({
            "ts": ts,
            "datetime": datetime.fromtimestamp(ts / 1000, tz).strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
            "close": float(c[4]), "vol": float(c[5]),
            "volCcy": float(c[6]) if len(c) > 6 and c[6] else 0.0,
        })

    os.makedirs(args.outdir, exist_ok=True)
    base = f"{args.inst.replace('-', '_')}_{args.bar}_{label}"
    csv_path = os.path.join(args.outdir, base + ".csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(recs[0].keys()))
        w.writeheader()
        w.writerows(recs)

    o, h, l, cl = recs[0]["open"], max(r["high"] for r in recs), min(r["low"] for r in recs), recs[-1]["close"]
    chg = (cl - o) / o * 100
    print(f"\n[数据] {args.inst} {args.bar} {label} ({args.tz}) 共 {len(recs)} 根")
    print(f"[概览] 开 {o}  高 {h}  低 {l}  收 {cl}  涨跌 {chg:+.2f}%")
    print(f"[CSV]  {csv_path}")

    # 画 K 线（红涨绿跌，A 股习惯）
    try:
        import matplotlib
        matplotlib.use("Agg")
        matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "PingFang SC", "Arial"]
        matplotlib.rcParams["axes.unicode_minus"] = False
        import pandas as pd
        import mplfinance as mpf

        df = pd.DataFrame(recs)
        df["Date"] = pd.to_datetime(df["datetime"])
        df = df.set_index("Date").rename(
            columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "vol": "Volume"})
        mc = mpf.make_marketcolors(up="r", down="g", edge="inherit",
                                   wick="inherit", volume={"up": "r", "down": "g"})
        style = mpf.make_mpf_style(base_mpf_style="yahoo", marketcolors=mc,
                                   gridcolor="#e6e6e6", facecolor="white")
        png_path = os.path.join(args.outdir, base + ".png")
        width = max(14, min(28, len(recs) / 12))
        mpf.plot(df, type="candle", style=style, volume=True, figsize=(width, 8),
                 title=f"\n{args.inst}  {args.bar}  {label} ({args.tz})   "
                       f"O {o}  H {h}  L {l}  C {cl}  {chg:+.2f}%",
                 ylabel="Price", ylabel_lower="Volume",
                 savefig=dict(fname=png_path, dpi=130, bbox_inches="tight"))
        print(f"[图片] {png_path}")
        if not args.no_open and sys.platform.startswith("win"):
            try:
                os.startfile(png_path)  # noqa: S606
            except Exception:
                pass
    except ImportError as e:
        print(f"[提示] 没装绘图库({e})，只导出了 CSV。装一下再跑：")
        print(r'       .venv\Scripts\python.exe -m pip install matplotlib mplfinance')

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] {exc}")
        raise SystemExit(1)
