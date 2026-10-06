"""Reproducible edge evaluation over locally stored candles.

Why this file exists
--------------------
`backtest.py` answers "what happened on this window". It cannot answer
"is this reproducible", which is the only question that matters before real
money. Three things it does not do:

  1. **Run more than one symbol.** A positive result on one coin is a single
     observation, not a strategy.
  2. **Split the sample.** Every number from one contiguous window is a
     description of that window. A parameter tuned until the window goes green
     has been fitted to noise.
  3. **Separate the stop's contribution from the signal's.** A strategy whose
     gross PnL is negative has a direction problem, and no fee cut fixes it.
     `backtest.py` prints this; this file makes it a table you can sort by.

It reads `data/platform.db` (written by the platform's poller) rather than
hitting OKX, so it also works with the network down — which is how it got run
the first time.

Run:
    set PYTHONPATH=src
    .venv/Scripts/python.exe tools/evaluate_edge.py
    .venv/Scripts/python.exe tools/evaluate_edge.py --split     # in-sample vs out-of-sample
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from okx_ema_trader.backtest import simulate  # noqa: E402
from okx_ema_trader.config import load_config  # noqa: E402
from okx_ema_trader.indicators import indicator_frame  # noqa: E402

DB = ROOT / "data" / "platform.db"
CONFIG = ROOT / "config" / "config.yaml"

# OKX swap taker is 5 bps/side for VIP0; 3 bps of slippage is the number this
# project has been using. 8 bps/unit total is therefore the realistic floor,
# and 0 bps is the gross edge. Both are reported because the gap between them
# IS the finding.
COST_BPS = 8.0
LOOKAHEAD_MS = 300_000  # one 5m bar


def load_candles(conn, inst: str, bar: str) -> list[list[float]]:
    """Confirmed candles only, oldest-first, one row per ts.

    A forming bar (confirm=0) is in the table for the chart but was never
    tradable, and backtesting on it would be the same lookahead the rest of
    this project is careful to avoid.

    The column is `vol` in SQL and `volume` in `indicators.COLUMNS` — those are
    two different vocabularies and conflating them costs an hour.
    """
    rows = conn.execute(
        "SELECT ts, open, high, low, close, vol FROM candles "
        "WHERE inst_id = ? AND bar = ? AND confirm = 1 ORDER BY ts",
        (inst, bar),
    ).fetchall()
    seen: dict[int, list] = {}
    for ts, o, h, low, close, vol in rows:
        seen[int(ts)] = [float(ts), float(o), float(h), float(low),
                         float(close), float(vol or 0.0)]
    return [seen[k] for k in sorted(seen)]


def run(inst: str, conn, cfg, stop_pct: float, cost_bps: float) -> dict | None:
    c5 = load_candles(conn, inst, "5m")
    c15 = load_candles(conn, inst, "15m")
    if len(c5) < 200 or len(c15) < 200:
        return None
    f5 = indicator_frame(c5, cfg.ema_fast, cfg.ema_slow, cfg.adx_period)
    f15 = indicator_frame(c15, cfg.ema_fast, cfg.ema_slow, cfg.adx_period)
    rep = simulate(f5, f15, cfg.adx_min, cfg.deviation_max,
                   cost_bps, LOOKAHEAD_MS, stop_pct)
    return _stats(inst, rep, stop_pct, cost_bps)


def _stats(inst: str, rep, stop_pct: float, cost_bps: float) -> dict:
    n = len(rep.trades)
    wins = [t for t in rep.trades if t.gross_return > 0]
    losses = [t for t in rep.trades if t.gross_return <= 0]
    stops = [t for t in rep.trades if t.exit_reason == "stop"]
    return {
        "inst": inst,
        "n": n,
        "win_rate": len(wins) / n if n else 0.0,
        "gross": rep.gross_return,
        "net": rep.net_return,
        "breakeven": rep.breakeven_bps,
        "avg_win": sum(t.gross_return for t in wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(t.gross_return for t in losses) / len(losses) if losses else 0.0,
        "stop_share": len(stops) / n if n else 0.0,
        "maxdd_1x": rep.max_drawdown(1.0),
        "maxdd_10x": rep.max_drawdown(10.0),
        "stop_pct": stop_pct,
        "cost_bps": cost_bps,
    }


def verdict(s: dict) -> str:
    """One line, deliberately conservative.

    The order matters: sign first, then magnitude, then sample size. A positive
    net on 12 trades is a weaker statement than a negative breakeven on 200,
    and printing them in the same weight is how people talk themselves into
    funding a backtest.
    """
    if s["n"] == 0:
        return "no trades"
    if s["breakeven"] < 0:
        return "DIRECTION WRONG (gross PnL negative)"
    if s["n"] < 30:
        return f"noise ({s['n']} trades < 30)"
    if s["net"] > 0 and s["breakeven"] > s["cost_bps"] * 1.5:
        return f"survives costs ({s['n']} trades)"
    if s["net"] > 0:
        return f"edge thinner than costs ({s['n']} trades)"
    return f"costs exceed edge ({s['n']} trades)"


def print_table(rows: list[dict], title: str) -> None:
    print(f"\n{title}")
    print("  " + "-" * 100)
    print(f"  {'symbol':<16}{'trades':>7}{'win%':>7}{'gross':>10}{'net':>10}"
          f"{'breakeven':>11}{'stop%':>7}{'dd1x':>8}{'dd10x':>8}  verdict")
    for s in rows:
        print(f"  {s['inst']:<16}{s['n']:>7}{s['win_rate']:>7.0%}"
              f"{s['gross']:>+10.2%}{s['net']:>+10.2%}"
              f"{s['breakeven']:>10.1f}b{s['stop_share']:>7.0%}"
              f"{s['maxdd_1x']:>8.1%}{s['maxdd_10x']:>8.1%}  {verdict(s)}")
    tot_n = sum(s["n"] for s in rows)
    tot_net = sum(s["net"] for s in rows)
    print("  " + "-" * 100)
    print(f"  {'TOTAL':<16}{tot_n:>7}{'':>7}{'':>10}{tot_net:>+10.2%}")
    print(f"  breakeven bps is the cost per unit that would erase the edge; "
          f"assumed cost here is {COST_BPS:.0f} bps.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stop-pct", type=float, default=None,
                    help="override config stop_loss_pct (999 = no stop)")
    ap.add_argument("--split", action="store_true",
                    help="also run the first 60%% of each series as in-sample "
                         "and the remainder as out-of-sample")
    args = ap.parse_args()

    if not DB.exists():
        raise SystemExit(f"{DB} not found — start the platform once so the "
                         "poller has something in it")
    cfg = load_config(CONFIG)
    stop = args.stop_pct if args.stop_pct is not None else cfg.trading.stop_loss_pct
    conn = sqlite3.connect(DB)

    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT inst_id FROM candles WHERE bar='5m' AND confirm=1")]
    rows = [s for s in (run(i, conn, cfg, stop, COST_BPS) for i in symbols) if s]
    if not rows:
        raise SystemExit("not enough stored history to evaluate anything")
    rows.sort(key=lambda s: -s["n"])

    print(f"source: {DB}   stop={stop}%   cost={COST_BPS} bps/unit   "
          f"params: EMA{cfg.ema_fast}/{cfg.ema_slow} ADX>{cfg.adx_min} "
          f"dev<={cfg.deviation_max:.0%}")
    print_table(rows, "FULL SAMPLE (all stored history)")

    print(f"\n{'=' * 100}\n  Gross vs net: does the signal have an edge BEFORE costs?")
    # `gross_return` is a property of the trade list and does not depend on the
    # cost argument, so this table is really "what the trades did" vs "what
    # survived the bill". The cost drag is net@8bps MINUS gross@0, not gross
    # minus gross — subtracting the two gross columns yields a column of zeros
    # that looks like a finding.
    gross_only = {s["inst"]: s for s in
                  (run(i, conn, cfg, stop, 0.0) for i in symbols) if s}
    print(f"  {'symbol':<16}{'trades':>7}{'gross@0cost':>14}{'net@8cost':>12}"
          f"{'cost drag':>12}")
    # Keyed by symbol, not zipped: `rows` is sorted by trade count and
    # `gross_only` is not, and zipping two differently-ordered lists produces a
    # table that is confident and wrong.
    for f in rows:
        g = gross_only.get(f["inst"])
        if g is None:
            continue
        print(f"  {f['inst']:<16}{g['n']:>7}{g['gross']:>+14.2%}"
              f"{f['net']:>+12.2%}{f['net'] - g['gross']:>12.2%}")
    print("  A negative gross@0cost means the DIRECTION is wrong; no fee "
          "reduction fixes that.")
    negatives = [s for s in gross_only.values() if s["n"] >= 5 and s["gross"] < 0]
    if negatives:
        print(f"  -> {len(negatives)} of {sum(1 for s in gross_only.values() if s['n'] >= 5)}"
              f" symbols with >=5 trades are negative before ANY cost.")

    if args.split:
        print(f"\n{'=' * 100}\n  OUT-OF-SAMPLE CHECK (last 40% held out)")
        for inst in symbols[:4]:
            c5 = load_candles(conn, inst, "5m")
            c15 = load_candles(conn, inst, "15m")
            if len(c5) < 400 or len(c15) < 400:
                continue
            cut5, cut15 = int(len(c5) * 0.6), int(len(c15) * 0.6)
            for label, a, b in (("in-sample", c5[:cut5], c15[:cut15]),
                                ("out-sample", c5[cut5:], c15[cut15:])):
                if len(a) < 200 or len(b) < 200:
                    continue
                f5 = indicator_frame(a, cfg.ema_fast, cfg.ema_slow, cfg.adx_period)
                f15 = indicator_frame(b, cfg.ema_fast, cfg.ema_slow, cfg.adx_period)
                rep = simulate(f5, f15, cfg.adx_min, cfg.deviation_max,
                               COST_BPS, LOOKAHEAD_MS, stop)
                s = _stats(inst, rep, stop, COST_BPS)
                print(f"  {inst:<16}{label:<12}{s['n']:>5} trades  "
                      f"net={s['net']:>+8.2%}  breakeven={s['breakeven']:>7.1f}b  "
                      f"{verdict(s)}")
        print("  Parameters were NOT re-tuned on the held-out half. If in-sample "
              "is green\n  and out-of-sample is not, the parameters were fitted "
              "to the sample, not to\n  the market. That is the only reading "
              "that matters here.")

    conn.close()


if __name__ == "__main__":
    main()
