"""Compare the crossover entry against a pullback entry, on real candles.

The question this answers is NOT "does pullback look nicer" but "does it have a
positive gross edge, and does that edge survive out of sample". Everything here
reuses `backtest.simulate` — the only thing that changes is the entry function,
so a difference in the numbers is a difference in the ENTRY, not in the harness.

A pullback entry, stated precisely:
  * 15m still decides DIRECTION (unchanged: ADX > adx_min, EMA separation,
    deviation gate). This is the part that is known to work as a filter.
  * 5m no longer waits for a crossover EVENT. It waits for price to pull back
    INTO the 5m EMA fast line (within `tolerance` of it) while the fast line is
    on the correct side of the slow line — i.e. buy a dip inside a trend
    instead of buying the breakout.

That is a STATE, not an event, so it needs its own "only fire once" latch or
every bar of a trend re-enters. The latch is per (env, bar-bucket) and is
tested by only allowing a re-arm after price closes back beyond the EMA.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

import okx_ema_trader.backtest as bt
import okx_ema_trader.strategy as st
from okx_ema_trader.backtest import simulate
from okx_ema_trader.config import load_config
from okx_ema_trader.indicators import indicator_frame
from okx_ema_trader.strategy import PullbackEntry

COST_BPS = 8.0
LOOKAHEAD_MS = 300_000


def load(conn, inst: str, bar: str) -> list[list[float]]:
    rows = conn.execute(
        "SELECT ts, open, high, low, close, vol FROM candles "
        "WHERE inst_id = ? AND bar = ? AND confirm = 1 ORDER BY ts",
        (inst, bar)).fetchall()
    seen: dict[int, list] = {}
    for r in rows:
        seen[int(r[0])] = [float(v) for v in r]
    return [seen[k] for k in sorted(seen)]


# PullbackEntry comes from strategy.py — the ONE implementation the executor and
# the platform also run. An earlier version of this file defined its own copy,
# and the two drifted: the copy still used the one-sided `low <= EMA` test that
# the real rule had already fixed, so the "measurement" silently measured a
# strategy nobody would ever trade. Never re-describe a rule here; import it.


def run(f5, f15, adx_min, dev, stop, entry=st.evaluate_signal) -> bt.Report:
    original = bt.evaluate_signal
    bt.evaluate_signal = entry
    try:
        return simulate(f5, f15, adx_min, dev, COST_BPS, LOOKAHEAD_MS, stop)
    finally:
        bt.evaluate_signal = original


def stats(rep: bt.Report) -> tuple[int, float, float, float]:
    wins = [t for t in rep.trades if t.gross_return > 0]
    return (len(rep.trades), rep.gross_return, rep.net_return,
            len(wins) / len(rep.trades) if rep.trades else 0.0)


def main() -> None:
    import sqlite3

    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "config.yaml")
    conn = sqlite3.connect(Path(__file__).resolve().parents[1] / "data" / "platform.db")
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT inst_id FROM candles WHERE bar='5m' GROUP BY inst_id "
        "HAVING COUNT(*) > 8000")]
    cache: dict = {}

    def frames(inst):
        if inst not in cache:
            c5, c15 = load(conn, inst, "5m"), load(conn, inst, "15m")
            cache[inst] = (indicator_frame(c5, cfg.ema_fast, cfg.ema_slow, cfg.adx_period),
                           indicator_frame(c15, cfg.ema_fast, cfg.ema_slow, cfg.adx_period),
                           len(c5))
        return cache[inst]

    print(f"symbols with enough 5m history: {symbols}\n")

    for label, stop in (("stop=3%", 3.0), ("stop=5%", 5.0)):
        print(f"=== {label} ===")
        print(f"{'symbol':<16}{'CROSS n':>9}{'gross':>9}{'net':>9}   "
              f"{'PULL n':>8}{'gross':>9}{'net':>9}   {'d_gross':>9}")
        tot = {"cross": [0, 0.0], "pull": [0, 0.0]}
        for inst in symbols:
            f5, f15, _ = frames(inst)
            c = stats(run(f5, f15, cfg.adx_min, cfg.deviation_max, stop))
            # ONE rule per symbol. The latch is per-instrument state: sharing a
            # single instance across symbols would let ZEC's dip disarm NVDA's
            # entry, and the trade count would silently depend on list order.
            p = stats(run(f5, f15, cfg.adx_min, cfg.deviation_max, stop,
                          PullbackEntry()))
            tot["cross"][0] += c[0]; tot["cross"][1] += c[1]
            tot["pull"][0] += p[0]; tot["pull"][1] += p[1]
            print(f"{inst:<16}{c[0]:>9}{c[1]:>+9.2%}{c[2]:>+9.2%}   "
                  f"{p[0]:>8}{p[1]:>+9.2%}{p[2]:>+9.2%}   {p[1]-c[1]:>+9.2%}")
        print(f"{'TOTAL':<16}{tot['cross'][0]:>9}{tot['cross'][1]:>+9.2%}"
              f"{'':>9}   {tot['pull'][0]:>8}{tot['pull'][1]:>+9.2%}"
              f"{'':>9}   {tot['pull'][1]-tot['cross'][1]:>+9.2%}\n")

    # The only number that matters: is the pullback edge positive BEFORE costs,
    # and does it hold in the second half of the sample?
    print("=== OUT-OF-SAMPLE CHECK (pullback, stop=3%) ===")
    print(f"{'symbol':<16}{'1st half':>22}{'2nd half':>22}")
    for inst in symbols:
        f5, f15, n5 = frames(inst)
        h5, h15 = n5 // 2, len(f15) // 2
        a = stats(run(f5.iloc[:h5].reset_index(drop=True),
                      f15.iloc[:h15].reset_index(drop=True),
                      cfg.adx_min, cfg.deviation_max, 3.0, PullbackEntry()))
        b = stats(run(f5.iloc[h5:].reset_index(drop=True),
                      f15.iloc[h15:].reset_index(drop=True),
                      cfg.adx_min, cfg.deviation_max, 3.0, PullbackEntry()))
        print(f"{inst:<16}  n={a[0]:<3} gross={a[1]:>+8.2%}       "
              f"n={b[0]:<3} gross={b[1]:>+8.2%}")


if __name__ == "__main__":
    main()