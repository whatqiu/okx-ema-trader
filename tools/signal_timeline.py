"""列出每一次 5m 交叉事件，以及那一刻 15m 环境站在哪一边。

用来验证：反向交叉发生时，15m 环境是否还没跟上（= 信号被 AND 掉）。
"""
import bisect
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "src")
from okx_ema_trader.indicators import indicator_frame  # noqa: E402
from okx_ema_trader.strategy import classify  # noqa: E402

CST = timezone(timedelta(hours=8))
EMA_FAST, EMA_SLOW, ADX_P = 20, 50, 14
ADX_MIN, DEV_MAX = 20.0, 0.02


def fmt(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, CST).strftime("%m-%d %H:%M")


def rows_of(frame):
    return [dict(zip(frame.columns, r)) for r in frame.itertuples(index=False)]


def load(sym, bar):
    conn = sqlite3.connect("data/platform.db")
    r = conn.execute(
        "select ts,open,high,low,close,vol from candles "
        "where inst_id=? and bar=? and confirm=1 order by ts", (sym, bar)
    ).fetchall()
    return [list(x) for x in r]


def report(symbol):
    r5 = rows_of(indicator_frame(load(symbol, "5m"), EMA_FAST, EMA_SLOW, ADX_P))
    r15 = rows_of(indicator_frame(load(symbol, "15m"), EMA_FAST, EMA_SLOW, ADX_P))
    ts15 = [r["ts"] for r in r15]

    print(f"\n{'='*88}\n{symbol}   15m 环境翻转 vs 5m 交叉事件\n{'='*88}")

    # --- 1. 15m 环境翻转时刻
    print("\n[15m 环境翻转]")
    for i in range(1, len(r15)):
        p, c = r15[i - 1], r15[i]
        if p["ema_fast"] > p["ema_slow"] and c["ema_fast"] <= c["ema_slow"]:
            print(f"  {fmt(c['ts'])}  long -> short   (15m EMA20 跌破 EMA50, ADX={c['adx']:.1f})")
        elif p["ema_fast"] < p["ema_slow"] and c["ema_fast"] >= c["ema_slow"]:
            print(f"  {fmt(c['ts'])}  short -> long   (15m EMA20 上穿 EMA50, ADX={c['adx']:.1f})")

    # --- 2. 每次 5m 交叉事件 + 当时 15m env
    print("\n[5m 交叉事件 与 当时 15m 环境]")
    print(f"  {'5m bar':>12} {'方向':>6} {'15m env':>8} {'15m ADX':>8} {'一致?':>6}")
    print("  " + "-" * 50)
    for i in range(1, len(r5)):
        p, b = r5[i - 1], r5[i]
        kind = None
        if p["ema_fast"] <= p["ema_slow"] and b["ema_fast"] > b["ema_slow"]:
            kind = "金叉"
        elif p["ema_fast"] >= p["ema_slow"] and b["ema_fast"] < b["ema_slow"]:
            kind = "死叉"
        if not kind:
            continue
        j = bisect.bisect_right(ts15, b["ts"] + 300 - 900) - 1
        if j < 1:
            continue
        e = r15[j]
        env = "long" if e["ema_fast"] > e["ema_slow"] else ("short" if e["ema_fast"] < e["ema_slow"] else "flat")
        ok = (kind == "金叉" and env == "long") or (kind == "死叉" and env == "short")
        print(f"  {fmt(b['ts']):>12} {kind:>6} {env:>8} {e['adx']:>8.1f} {'YES 可交易' if ok else 'NO  环境相反 → 丢弃':>6}")

    # --- 3. 当前状态
    last15, last5 = r15[-1], r5[-1]
    env = "long" if last15["ema_fast"] > last15["ema_slow"] else "short"
    print(f"\n[最新] 15m: env={env} ADX={last15['adx']:.1f} EMA20={last15['ema_fast']:.2f} EMA50={last15['ema_slow']:.2f}")
    print(f"       5m : EMA20={last5['ema_fast']:.2f} EMA50={last5['ema_slow']:.2f} "
          f"({'EMA20 已在下方 = 熊市排列，但交叉早就发生过了' if last5['ema_fast'] < last5['ema_slow'] else 'EMA20 在上方'})")

    # --- 4. 实际产生的信号：逐 bar 跑一遍当前的 classify
    print("\n[策略实际会发出的信号]  (逐 bar 调 strategy.classify)")
    for i in range(1, len(r5)):
        bar, prev = r5[i], r5[i - 1]
        j = bisect.bisect_right(ts15, bar["ts"] + 300 - 900) - 1
        if j < 1:
            continue
        e, pe = r15[j], r15[j - 1]
        sig, _ = classify(
            {"close": bar["close"], "ema_fast": bar["ema_fast"], "ema_slow": bar["ema_slow"],
             "adx": bar["adx"], "prev_ema_fast": prev["ema_fast"], "prev_ema_slow": prev["ema_slow"]},
            {"close": e["close"], "ema_fast": e["ema_fast"], "ema_slow": e["ema_slow"], "adx": e["adx"],
             "prev_ema_fast": pe["ema_fast"], "prev_ema_slow": pe["ema_slow"]},
            ADX_MIN, DEV_MAX,
        )
        if sig:
            print(f"  {fmt(bar['ts'])}  {sig.side.upper():<6} @ {bar['close']:.4f}   {sig.reason}")


if __name__ == "__main__":
    for s in (sys.argv[1:] or ["SPCX-USDT-SWAP"]):
        report(s)
