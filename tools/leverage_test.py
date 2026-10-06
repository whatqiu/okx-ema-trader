"""Test the "high leverage + ride the move" claim against stored candles.

The question this answers is NOT "does the backtest make money". It is:

    after a signal fires, does price reach +TARGET% in my favour BEFORE it
    reaches -LIQ% against me, where LIQ is the liquidation distance at the
    leverage I am actually using?

That is the whole bet behind "30x, let it swing 3-4 points, I win most of the
time". It is a race between two barriers, and it is measurable.

Liquidation distance (OKX, cross/isolated, maintenance margin ignored only in
the `mmr` term): init margin = 1/leverage, so the account is gone at roughly

    liq_distance = 1/leverage - maintenance_margin_rate

With mmr ~= 0.5% (typical for the small tiers) that is 2.83% at 30x — which is
INSIDE a 3% stop. That is the single most important number in this file: at 30x
you are liquidated before a 3% stop-loss order can even fill.

Same entry convention as the backtest: decided on bar N's close, filled at bar
N+1's open. Within a single bar the ORDER of high/low is unknown, so if a bar's
range spans both barriers we assume the adverse one came first — identical to
the conservative assumption backtest.py already makes, and what a real
liquidation engine does.

Usage:
    python tools/leverage_test.py --inst ZEC-USDT-SWAP --leverage 30 --target 3
    python tools/leverage_test.py --sweep          # 10/20/30/50x side by side
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "platform" / "backend"))

from okx_ema_trader.indicators import indicator_frame  # noqa: E402
from okx_ema_trader.strategy import classify  # noqa: E402

DB = PROJECT_ROOT / "data" / "platform.db"
FIVE_MIN = 5 * 60_000
MMR = 0.005  # maintenance margin rate assumption, ~0.5% for small tiers


def load(inst: str, bar: str) -> list[list[float]]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT ts, open, high, low, close, vol FROM candles "
        "WHERE inst_id=? AND bar=? ORDER BY ts", (inst, bar)).fetchall()
    con.close()
    return [[float(x) for x in r] for r in rows]


def find_signals(inst: str, adx_min: float, deviation_max: float,
                 ema_fast: int, ema_slow: int, adx_period: int):
    """Replay the auto-trader's decision path, same cutoff arithmetic."""
    c5, c15 = load(inst, "5m"), load(inst, "15m")
    if len(c5) < ema_slow + 2 or len(c15) < adx_period * 2:
        return [], 0

    f5 = indicator_frame(c5, ema_fast, ema_slow, adx_period)
    f15 = indicator_frame(c15, ema_fast, ema_slow, adx_period)
    ts15 = [int(t) for t in f15["ts"]]

    opens = f5["open"].to_numpy()
    highs = f5["high"].to_numpy()
    lows = f5["low"].to_numpy()
    closes = f5["close"].to_numpy()

    out = []
    for i in range(1, len(f5) - 1):
        last_ts = int(f5["ts"].iloc[i])
        cutoff = last_ts + FIVE_MIN - 15 * 60_000
        j = len(ts15) - 1
        while j >= 0 and ts15[j] > cutoff:
            j -= 1
        if j < 1:
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
                                ind15["adx"])):
            continue

        sig, _ = classify(ind5, ind15, adx_min, deviation_max)
        if sig is None:
            continue
        out.append({"i": i, "ts": last_ts, "side": sig.side,
                    "adx": float(ind15["adx"])})
    return out, (opens, highs, lows, closes, len(f5))


def race(signals, ohlc, leverage: float, target_pct: float, horizon: int):
    """For each signal: which barrier is touched first, and by how much."""
    opens, highs, lows, closes, n = ohlc
    liq_pct = (1.0 / leverage - MMR) * 100.0
    rows = []
    for s in signals:
        i = s["i"] + 1                      # fill on the NEXT bar's open
        if i >= n:
            continue
        entry = float(opens[i])
        side = 1 if s["side"] == "long" else -1
        end = min(n, i + horizon)

        mfe = 0.0        # best favourable move seen
        mae = 0.0        # worst adverse move seen (positive number)
        outcome = "timeout"
        bars = None
        exit_pct = None
        for k in range(i, end):
            high, low = float(highs[k]), float(lows[k])
            fav = ((high - entry) / entry) if side == 1 else ((entry - low) / entry)
            adv = ((entry - low) / entry) if side == 1 else ((high - entry) / entry)
            fav *= 100.0
            adv *= 100.0
            mfe = max(mfe, fav)
            mae = max(mae, adv)
            # Same bar spans both barriers -> assume the bad one came first.
            if adv >= liq_pct:
                outcome, bars, exit_pct = "liquidated", k - i + 1, -liq_pct
                break
            if fav >= target_pct:
                outcome, bars, exit_pct = "target", k - i + 1, target_pct
                break
        if exit_pct is None:
            # Ran out of horizon: mark to the last close we actually saw.
            last = end - 1
            c = float(closes[last])
            exit_pct = ((c - entry) / entry if side == 1
                        else (entry - c) / entry) * 100.0
        # Return on MARGIN: the price move, multiplied by leverage, and a
        # liquidation costs 100% of the margin, not the price move.
        if outcome == "liquidated":
            margin_pct = -100.0
        else:
            margin_pct = exit_pct * leverage
        rows.append({"ts": s["ts"], "side": s["side"], "adx": s["adx"],
                     "entry": entry, "mfe": mfe, "mae": mae,
                     "outcome": outcome, "bars": bars,
                     "margin_pct": margin_pct})
    return rows, liq_pct


def main() -> int:
    cfg = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml")
                         .read_text(encoding="utf-8"))
    st = cfg["strategy"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--inst", default="ZEC-USDT-SWAP")
    ap.add_argument("--leverage", type=float, default=30)
    ap.add_argument("--target", type=float, default=3.0)
    ap.add_argument("--horizon", type=int, default=288, help="bars held (288=24h)")
    ap.add_argument("--adx-min", type=float, default=st["adx_min"])
    ap.add_argument("--deviation-max", type=float, default=st["deviation_max"])
    ap.add_argument("--ema-fast", type=int, default=st["ema_fast"])
    ap.add_argument("--ema-slow", type=int, default=st["ema_slow"])
    ap.add_argument("--adx-period", type=int, default=st["adx_period"])
    ap.add_argument("--sweep", action="store_true",
                    help="run 10/20/30/50x instead of a single leverage")
    a = ap.parse_args()

    signals, ohlc = find_signals(a.inst, a.adx_min, a.deviation_max,
                                 a.ema_fast, a.ema_slow, a.adx_period)
    if not signals:
        print("no signals in stored data for", a.inst)
        return 1

    print(f"\n{a.inst}   EMA{a.ema_fast}/{a.ema_slow}  ADX>{a.adx_min}  "
          f"持仓上限 {a.horizon} 根 5m ({a.horizon*5/60:.0f}h)")
    print(f"信号数 {len(signals)}\n")

    levs = [10, 20, 30, 50] if a.sweep else [a.leverage]
    for lev in levs:
        rows, liq = race(signals, ohlc, lev, a.target, a.horizon)
        tgt = sum(1 for r in rows if r["outcome"] == "target")
        liqd = sum(1 for r in rows if r["outcome"] == "liquidated")
        to = sum(1 for r in rows if r["outcome"] == "timeout")
        med_mae = sorted(r["mae"] for r in rows)[len(rows) // 2]
        med_mfe = sorted(r["mfe"] for r in rows)[len(rows) // 2]
        avg_margin = sum(r["margin_pct"] for r in rows) / len(rows)
        print(f"  {lev:>2.0f}x  强平距离 {liq:.2f}%   "
              f"先到 +{a.target}%: {tgt}/{len(rows)}   先被强平: {liqd}/{len(rows)}   "
              f"都没到: {to}/{len(rows)}")
        print(f"        中位最大反向波动 MAE {med_mae:.2f}%  "
              f"中位最大有利波动 MFE {med_mfe:.2f}%  "
              f"每笔保证金平均 {avg_margin:+.1f}%")

    rows, liq = race(signals, ohlc, a.leverage, a.target, a.horizon)
    print(f"\n  逐笔明细（{a.leverage:.0f}x，强平距离 {liq:.2f}%，目标 +{a.target}%）")
    print("  时间            方向   进场价      MFE       MAE     结果")
    print("  " + "-" * 68)
    import datetime as dt
    tz = dt.timezone(dt.timedelta(hours=8))
    for r in rows:
        t = dt.datetime.fromtimestamp(r["ts"] / 1000, tz).strftime("%m-%d %H:%M")
        extra = f"{r['bars']}根" if r["bars"] else "-"
        print(f"  {t}   {r['side']:<5}  {r['entry']:<10.4f}  "
              f"{r['mfe']:>+6.2f}%  {-r['mae']:>+6.2f}%   {r['outcome']:<10} {extra}")
    print("\n  注：同一根 K 线内 high/low 先后顺序不可知，按先碰到不利一侧处理")
    print("      （与 backtest.py 的保守假设一致，也是真实强平引擎的行为）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
