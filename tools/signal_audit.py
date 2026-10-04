"""Replay the auto-trader's decision over stored candles and count WHY.

The `signals` table only gets a row when a signal fires, so "the strategy never
triggered" and "the loop never ran" look identical in the database. This script
re-runs the exact same decision path offline and reports the rejection reason
for every bar, which is the only way to tell those two apart.

Usage:
    python tools/signal_audit.py                      # auto symbol from meta
    python tools/signal_audit.py --inst ZEC-USDT-SWAP
    python tools/signal_audit.py --adx-min 15         # what-if on thresholds
    python tools/signal_audit.py --top 20             # show the near-misses
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "platform" / "backend"))

import sqlite3  # noqa: E402
import yaml  # noqa: E402

from okx_ema_trader.indicators import indicator_frame  # noqa: E402
from okx_ema_trader.strategy import classify  # noqa: E402

DB = PROJECT_ROOT / "data" / "platform.db"
FIVE_MIN = 5 * 60_000


def load(inst: str, bar: str) -> list[list[float]]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT ts, open, high, low, close, vol FROM candles "
        "WHERE inst_id=? AND bar=? ORDER BY ts", (inst, bar)).fetchall()
    con.close()
    return [[float(x) for x in r] for r in rows]


def audit(inst: str, adx_min: float, deviation_max: float,
          ema_fast: int, ema_slow: int, adx_period: int) -> dict:
    c5, c15 = load(inst, "5m"), load(inst, "15m")
    if len(c5) < ema_slow + 2 or len(c15) < adx_period * 2:
        return {"error": f"数据不足：5m={len(c5)} 15m={len(c15)}"}

    f5 = indicator_frame(c5, ema_fast, ema_slow, adx_period)
    f15 = indicator_frame(c15, ema_fast, ema_slow, adx_period)

    reasons: Counter[str] = Counter()
    signals: list[dict] = []
    adx_values: list[float] = []
    dev_values: list[float] = []

    # Same cutoff arithmetic as autotrader.maybe_trade: the 15m row must have
    # fully closed before the 5m bar we are trading on.
    ts15 = [int(t) for t in f15["ts"]]
    for i in range(1, len(f5)):
        last_ts = int(f5["ts"].iloc[i])
        cutoff = last_ts + FIVE_MIN - 15 * 60_000
        j = len(ts15) - 1
        while j >= 0 and ts15[j] > cutoff:
            j -= 1
        if j < 1:
            reasons["no closed 15m bar"] += 1
            continue

        r5, p5, r15 = f5.iloc[i], f5.iloc[i - 1], f15.iloc[j]
        ind5 = {"close": float(r5["close"]), "ema_fast": float(r5["ema_fast"]),
                "ema_slow": float(r5["ema_slow"]),
                "prev_ema_fast": float(p5["ema_fast"]),
                "prev_ema_slow": float(p5["ema_slow"])}
        ind15 = {"close": float(r15["close"]), "ema_fast": float(r15["ema_fast"]),
                 "ema_slow": float(r15["ema_slow"]), "adx": float(r15["adx"])}

        if any(v != v for v in (ind5["ema_fast"], ind5["ema_slow"],
                                ind5["prev_ema_fast"], ind5["prev_ema_slow"],
                                ind15["adx"])):  # NaN != NaN
            reasons["indicator warmup"] += 1
            continue

        adx_values.append(ind15["adx"])
        if ind15["ema_fast"] > 0:
            dev_values.append(abs(ind15["close"] - ind15["ema_fast"])
                              / ind15["ema_fast"])

        sig, why = classify(ind5, ind15, adx_min, deviation_max)
        if sig is None:
            reasons[why] += 1
        else:
            reasons["SIGNAL " + sig.side] += 1
            signals.append({"ts": last_ts, "side": sig.side,
                            "reason": sig.reason, "adx": round(ind15["adx"], 1)})

    return {"inst": inst, "bars_5m": len(f5) - 1, "reasons": dict(reasons),
            "signals": signals,
            "adx_max": round(max(adx_values), 1) if adx_values else None,
            "adx_median": round(sorted(adx_values)[len(adx_values) // 2], 1)
            if adx_values else None,
            "dev_median": round(sorted(dev_values)[len(dev_values) // 2], 4)
            if dev_values else None}


def main() -> int:
    cfg = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml")
                         .read_text(encoding="utf-8"))
    st = cfg["strategy"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--inst", default=None)
    ap.add_argument("--adx-min", type=float, default=st["adx_min"])
    ap.add_argument("--deviation-max", type=float, default=st["deviation_max"])
    ap.add_argument("--ema-fast", type=int, default=st["ema_fast"])
    ap.add_argument("--ema-slow", type=int, default=st["ema_slow"])
    ap.add_argument("--adx-period", type=int, default=st["adx_period"])
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args()

    inst = a.inst
    if inst is None:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        inst = con.execute("SELECT value FROM meta WHERE key='auto_symbol'") \
                   .fetchone()
        con.close()
        inst = inst[0] if inst else cfg["symbol"]

    r = audit(inst, a.adx_min, a.deviation_max,
              a.ema_fast, a.ema_slow, a.adx_period)
    if "error" in r:
        print(r["error"])
        return 1

    print(f"标的 {r['inst']}   5m 根数 {r['bars_5m']}")
    print(f"参数  ADX>{a.adx_min}  偏离<={a.deviation_max}  "
          f"EMA {a.ema_fast}/{a.ema_slow}  ADX周期 {a.adx_period}")
    print(f"实测  ADX 中位数 {r['adx_median']}  最大值 {r['adx_max']}   "
          f"偏离中位数 {r['dev_median']}")
    print()
    print("每根 5m K 线的判定结果：")
    for k, v in sorted(r["reasons"].items(), key=lambda kv: -kv[1]):
        bar = "#" * max(1, round(v / max(1, r["bars_5m"]) * 40))
        print(f"  {k:<22} {v:>5}  {v/r['bars_5m']:>6.1%}  {bar}")
    print()
    print(f"信号总数：{len(r['signals'])}")
    for s in r["signals"][:a.top]:
        print(f"  {s['ts']}  {s['side']:<5} ADX={s['adx']}  {s['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
