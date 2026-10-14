"""Analyse a real OKX position-history export.

This reads the account's OWN fills, so it is the one measurement here that
cannot be argued with the way a backtest can. Everything else in this project
is a simulation of this.

The export has a UTF-8 BOM that survives `.lstrip()`, so values are cleaned
per-cell rather than per-line.
"""
import csv
import glob
import io
import sys
from collections import defaultdict
from datetime import datetime

path = glob.glob(".workbuddy/tmp_okx/**/*.csv", recursive=True)[0]
lines = open(path, encoding="utf-8-sig").read().splitlines()


def clean(s):
    return s.replace("﻿", "").replace("", "").strip()


hdr = [clean(h) for h in lines[1].split(",")]
rows = []
for line in lines[2:]:
    if not line.strip():
        continue
    rows.append({k: clean(v) for k, v in zip(hdr, next(csv.reader([line])))})

recs = []
for r in rows:
    t0 = datetime.strptime(r["仓位创建时间"], "%Y-%m-%d %H:%M:%S")
    t1 = datetime.strptime(r["仓位更新时间"], "%Y-%m-%d %H:%M:%S")
    recs.append({
        "t1": t1,
        "sym": r["交易产品"].replace("-USDT-SWAP", ""),
        "side": "多" if r["持仓方向"] == "做多" else "空",
        "lev": int(r["杠杆倍数"]),
        "ret": float(r["收益率"]),
        "pnl": float(r["收益额"]),
        "fee": float(r["累计手续费"]) + float(r["累计资金费用"]),
        "mins": int((t1 - t0).total_seconds() / 60),
        "ctype": r["平仓类型"],
        "notional": float(r["最大持仓量"]) * float(r["开仓均价"]),
        "close_px": float(r["平仓均价"]),
        "open_px": float(r["开仓均价"]),
        "qty": float(r["累计平仓量"]),
    })

recs.sort(key=lambda x: x["t1"], reverse=True)

print("=" * 104)
print("  真实账户持仓明细（%d 笔，%s ~ %s）" % (len(recs), recs[-1]["t1"], recs[0]["t1"]))
print("=" * 104)
print("%-12s %-9s %-4s %-6s %-9s %-10s %-8s %-7s %s"
      % ("平仓时间", "标的", "方向", "杠杆", "收益率", "收益额", "费用", "持仓", "平仓类型"))
print("-" * 104)
for r in recs:
    print("%-12s %-9s %-4s %-6s %+8.2f%% %+10.4f %-8.4f %-7s %s"
          % (r["t1"].strftime("%m-%d %H:%M"), r["sym"], r["side"],
             "%dx" % r["lev"], r["ret"] * 100, r["pnl"], r["fee"],
             "%dm" % r["mins"], r["ctype"]))

pnl = sum(r["pnl"] for r in recs)
fee = sum(r["fee"] for r in recs)
wins = [r for r in recs if r["pnl"] > 0]
losses = [r for r in recs if r["pnl"] <= 0]

print("=" * 104)
print("  总收益额     : %+.4f USDT" % pnl)
print("  总费用       :  %.4f USDT (手续费+资金费)" % fee)
print("  净收益       : %+.4f USDT" % (pnl - fee))
print("  笔数         : %d  (盈 %d / 亏 %d, 胜率 %.1f%%)"
      % (len(recs), len(wins), len(losses), len(wins) / len(recs) * 100))
if wins and losses:
    aw = sum(r["pnl"] for r in wins) / len(wins)
    al = sum(r["pnl"] for r in losses) / len(losses)
    print("  平均盈利     : %+.4f  平均亏损 %+.4f  赔率 %.2f" % (aw, al, abs(aw / al)))

print()
print("  --- 按杠杆分组 ---")
bylev = defaultdict(list)
for r in recs:
    bylev[r["lev"]].append(r)
for lev in sorted(bylev):
    g = bylev[lev]
    print("  %2dx: %2d 笔  收益 %+9.4f  胜率 %5.1f%%  平均 %+.4f"
          % (lev, len(g), sum(x["pnl"] for x in g),
             len([x for x in g if x["pnl"] > 0]) / len(g) * 100,
             sum(x["pnl"] for x in g) / len(g)))

print()
print("  --- 按标的分组 ---")
bysym = defaultdict(list)
for r in recs:
    bysym[r["sym"]].append(r)
for s in sorted(bysym, key=lambda x: sum(y["pnl"] for y in bysym[x]), reverse=True):
    g = bysym[s]
    print("  %-9s %2d 笔  收益 %+9.4f  胜率 %5.1f%%  杠杆 %s"
          % (s, len(g), sum(x["pnl"] for x in g),
             len([x for x in g if x["pnl"] > 0]) / len(g) * 100,
             "/".join(str(sorted({x["lev"] for x in g})))))

print()
print("  --- 多空 ---")
for side in ("多", "空"):
    g = [r for r in recs if r["side"] == side]
    if not g:
        continue
    print("  %s: %2d 笔  收益 %+9.4f  胜率 %5.1f%%"
          % (side, len(g), sum(x["pnl"] for x in g),
             len([x for x in g if x["pnl"] > 0]) / len(g) * 100))

print()
print("  --- 持仓时长 ---")
buckets = {"<15m": [], "15-60m": [], "1-4h": [], ">4h": []}
for r in recs:
    m = r["mins"]
    if m < 15:
        buckets["<15m"].append(r)
    elif m < 60:
        buckets["15-60m"].append(r)
    elif m < 240:
        buckets["1-4h"].append(r)
    else:
        buckets[">4h"].append(r)
for k in ("<15m", "15-60m", "1-4h", ">4h"):
    g = buckets[k]
    if not g:
        continue
    print("  %-8s %2d 笔  收益 %+9.4f  胜率 %5.1f%%"
          % (k, len(g), sum(x["pnl"] for x in g),
             len([x for x in g if x["pnl"] > 0]) / len(g) * 100))
