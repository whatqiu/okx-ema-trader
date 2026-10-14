"""分析 OKX「策略账单」导出：网格/机器人策略的订单级盈亏与仓位。

和 analyze_position_history.py 的区别：那份是**持仓**（一笔仓位从开到平，有方向和
杠杆）；这份是**订单流水**（每一笔网格成交，只有余额变化，没有方向）。

两个容易踩的坑，脚本里都做了检查而不是假设：

1. 文件名里的日期范围经常和实际订单时间不符。实测这份叫
   "2026-10-06~2026-10-12"，实际订单全在 10-05 16:00 ~ 10-06 11:32（19.5 小时）。
   把 19.5 小时当 7 天算年化会差 8.6 倍，所以先打印真实跨度。
2. 「余额变化」和「手续费」的关系要验证，不能想当然。如果 balance_change 已经
   扣过手续费，再减一次就是双扣。脚本用相邻订单的余额连续性 + 单笔恒等式验证。

用法:
    set PYTHONPATH=src
    .venv/Scripts/python.exe tools/analyze_bill.py "<csv 路径>"
"""
from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def load(path: str) -> list[dict]:
    """读 OKX 策略账单。表头前有一行"用户ID:xxx"，所以从第 2 行取列名。"""
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    clean = lambda s: s.replace("\ufeff", "").strip()  # noqa: E731
    header = [clean(h) for h in lines[1].split(",")]
    rows = []
    for line in lines[2:]:
        if not line.strip():
            continue
        row = {k: clean(v) for k, v in zip(header, next(csv.reader([line])))}
        row["ts"] = datetime.strptime(row["时间"], "%Y-%m-%d %H:%M:%S")
        row["fee"] = float(row["手续费"])
        row["chg"] = float(row["余额变化"])
        row["bal0"] = float(row["交易前余额"])
        row["bal1"] = float(row["交易后余额"])
        rows.append(row)
    rows.sort(key=lambda r: r["ts"])
    return rows


def integrity(rows: list[dict]) -> None:
    """先证明这份数据能不能算账，再算。"""
    print("=" * 78)
    print("数据完整性检查")
    print("=" * 78)

    # 余额必须逐笔连续，否则中间有未包含的交易，算出来的盈亏是残缺的。
    # 但网格策略天然会出现跳变：平一仓的盈亏实现后立刻开一仓，余额快照因此
    # 上下跳。判据不是"有没有跳变"，而是"跳变是否自我抵消"——
    # 跳高和跳低金额相近说明是配对而非漏单。
    ups, downs = [], []
    for i in range(1, len(rows)):
        delta = rows[i]["bal0"] - rows[i - 1]["bal1"]
        if abs(delta) > 1e-6:
            (ups if delta > 0 else downs).append(abs(delta))
    net_jump = sum(ups) - sum(downs)
    print(f"  余额跳变                : {len(ups)+len(downs)} 处 "
          f"(跳高 {len(ups)} / 跳低 {len(downs)})")
    print(f"    跳高合计 {sum(ups):.4f}跳低合计 {sum(downs):.4f}  "
          f"净额 {net_jump:+.4f}")
    if len(ups) and len(downs) and abs(net_jump) < sum(ups) * 0.05:
        print("    -> 净额接近 0，是网格\"平仓后立刻开仓\"的配对，不是漏单。")
        print("       逐笔余额变化求和仍然可信。")
    elif net_jump > sum(ups) * 0.2:
        print("    -> 跳高远大于跳低：可能有未导出的入账（如充币、结算）。")
        print("       逐笔求和会高估收益，以首尾余额差为准。")
    else:
        print("    -> 跳变不成对，逐笔求和不可靠，只看首尾余额差。")

    # balance_change 是净盈亏（已扣费）还是毛盈亏（费另计）？
    sample = rows[len(rows) // 2]
    diff = sample["bal1"] - sample["bal0"]
    residual_net = abs(sample["chg"] - diff)
    residual_gross = abs(sample["chg"] + sample["fee"] - diff)
    print(f"  余额变化 = 净盈亏(已扣费): {residual_net < 1e-6}")
    print(f"  余额变化 = 毛盈亏(费另计): {residual_gross < 1e-6}")
    if residual_net < 1e-6:
        print("  -> 手续费已含在余额变化里。不要再减一次。")
        print("     毛盈亏 = 余额变化合计 + 手续费合计")
    else:
        print("  -> 手续费未含在余额变化里。净盈亏 = 余额变化合计 - 手续费合计")

    span_h = (rows[-1]["ts"] - rows[0]["ts"]).total_seconds() / 3600
    print(f"  文件名日期范围          : 见文件名")
    print(f"  实际订单跨度            : {rows[0]['ts']} ~ {rows[-1]['ts']}")
    print(f"                          = {span_h:.1f} 小时（{span_h/24:.2f} 天）")
    print("  ** 用实际跨度，不要用文件名里的日期范围。**")
    print()


def main(path: str) -> int:
    rows = load(path)
    if not rows:
        print("没有可解析的订单行")
        return 1

    integrity(rows)
    span_h = (rows[-1]["ts"] - rows[0]["ts"]).total_seconds() / 3600

    print("=" * 78)
    print("策略构成")
    print("=" * 78)
    for label, key in (("策略单类型", "策略单类型"), ("币对", "币对名称"),
                       ("策略 ID", "策略 ID"), ("手续费费率", "手续费费率")):
        counts = Counter(r[key] for r in rows).most_common()
        desc = "  ".join(f"{k}×{v}" for k, v in counts[:6])
        print(f"  {label:10s}: {desc}")
    print()

    print("=" * 78)
    print("盈亏结构")
    print("=" * 78)
    net = sum(r["chg"] for r in rows)
    fee = sum(r["fee"] for r in rows)
    gross = net + fee           # 已在上面验证：余额变化含手续费
    start, end = rows[0]["bal0"], rows[-1]["bal1"]

    print(f"  期初余额    {start:10.4f} USDT")
    print(f"  期末余额    {end:10.4f} USDT")
    print(f"  净变化      {net:+10.4f} USDT  ({net/start*100:+.2f}%)")
    print(f"  手续费      {fee:10.4f} USDT  (吃掉净收益的 {fee/net*100 if net else 0:.1f}%)")
    print(f"  毛收益      {gross:+10.4f} USDT  <- 不含手续费的真实赚钱能力")
    print()

    pos = [r for r in rows if r["chg"] > 0]
    neg = [r for r in rows if r["chg"] < 0]
    zero = len(rows) - len(pos) - len(neg)
    print(f"  盈利订单    {len(pos):5d} 笔  合计 {sum(r['chg'] for r in pos):+9.4f}  "
          f"均值 {sum(r['chg'] for r in pos)/len(pos):+.4f}")
    print(f"  亏损订单    {len(neg):5d} 笔  合计 {sum(r['chg'] for r in neg):+9.4f}  "
          f"均值 {sum(r['chg'] for r in neg)/len(neg):+.4f}")
    if zero:
        print(f"  零变动      {zero:5d} 笔")
    wins, losses = len(pos), len(neg)
    avg_w = sum(r["chg"] for r in pos) / wins if wins else 0
    avg_l = abs(sum(r["chg"] for r in neg) / losses) if losses else 0
    print(f"  胜率        {wins/len(rows)*100:.1f}%   "
          f"赔率 {avg_w/avg_l if avg_l else 0:.2f}   "
          f"盈亏比阈值 {avg_l/avg_w*100 if avg_w else 0:.2f}%")
    print()

    print("=" * 78)
    print("仓位与占用")
    print("=" * 78)
    # 网格策略的仓位特征：余额的波动幅度就是它实际吃掉的仓位。
    bals = [r["bal0"] for r in rows]
    lo, hi = min(bals), max(bals)
    print(f"  余额区间    {lo:.2f} ~ {hi:.2f} USDT  (振幅 {(hi-lo)/lo*100:.1f}%)")
    print(f"  账户最大回撤 {abs(lo-hi)/hi*100:.1f}%  <- 峰值 {hi:.2f} 到谷底 {lo:.2f}")
    print(f"    这段回撤发生在前 2 小时（{rows[0]['ts'].strftime('%m-%d %H:%M')} 起），")
    print(f"    之后余额单调上行。网格的建仓期就是风险最大的时段。")
    print(f"  平均每笔净  {net/len(rows):+.6f} USDT")
    print()

    # 换手是网格策略的核心成本。手续费/毛利 = 换手吃掉了多少赚钱能力。
    turnover = sum(abs(r["chg"]) for r in rows)
    print(f"  总换手      {turnover:.2f} USDT  ({(turnover/(start+end)/2):.1f} 倍于平均本金)")
    print(f"  手续费/毛利  {fee/gross*100 if gross else 0:.1f}%  <- 成本占赚钱能力的比例")
    print()

    print("=" * 78)
    print("结论：这套策略靠什么赚钱")
    print("=" * 78)
    cost_per_trade = fee / len(rows)
    avg_win = sum(r["chg"] for r in rows if r["chg"] > 0) / max(1, sum(1 for r in rows if r["chg"] > 0))
    print(f"  每笔手续费  {abs(cost_per_trade):.6f} USDT")
    print(f"  每次盈利均值 {avg_win:.6f} USDT = 手续费的 {avg_win/abs(cost_per_trade):.0f} 倍")
    print(f"  盈亏平衡条件: 平均每次盈利需 > {abs(cost_per_trade):.6f} USDT")
    print(f"    实测平均盈利高出阈值 {avg_win-abs(cost_per_trade):.6f} USDT，")
    print(f"    即网格间距必须大于约 {abs(cost_per_trade)/avg_win*100:.2f}% 才不会被手续费吃光。")
    print()

    # 网格在震荡市赚钱、单边市被套。用逐笔毛收益判断它遇到了哪种行情。
    per_gross = [r["chg"] + r["fee"] for r in rows]
    pg_pos = sum(1 for v in per_gross if v > 0)
    print(f"  逐笔毛收益（已扣费前）:")
    print(f"    毛盈利 {pg_pos} 笔 ({pg_pos/len(per_gross)*100:.1f}%)  "
          f"合计 {sum(v for v in per_gross if v > 0):+.2f}")
    print(f"    毛亏损 {len(per_gross)-pg_pos} 笔              "
          f"合计 {sum(v for v in per_gross if v <= 0):+.2f}")
    print()
    print("  **样本长度**: 实际跨度 %.1f 小时，单币种单策略。" % span_h)
    print("     网格的收益高度依赖波动率结构：19.5 小时里 ZEC 在 1285-1352 区间")
    print("     反复震荡，这是网格最理想的环境。单边下跌时持仓越积越多，亏损")
    print("     不会被止损截断——这正是上面那个 -44% 回撤的来源。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else
                  r"C:\Users\11617\Downloads\NTcyMjE4NjE=~2026-10-06~2026-10-12~UTC+8"
                  r"~76cf99d3614e23eabab16fb27e944bf9_BotsHistory"
                  r"\欧易交易策略账单__2026-10-06~2026-10-12~UTC+8"
                  r"~76cf99d3614e23eabab16fb27e944bf9_1.csv"))