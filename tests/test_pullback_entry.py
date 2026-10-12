"""PullbackEntry semantics: a state needs a latch, and the latch needs tests.

The failure this file exists to prevent is specific and quiet: a pullback entry
is a STATE ("price is touching the fast EMA"), while the crossover entry is an
EVENT ("the cross happened on this bar"). Drop the latch and the strategy
re-enters on every bar of a trend — which looks like a working strategy right up
until the turnover shows up in the costs.

So the assertions here are mostly about FIRING DISCIPLINE, not about whether the
rule makes money. Whether it makes money is measured by tools/test_pullback.py
and written up in README; a test that asserted profitability would be asserting
a fact about the market that no test can know.

Run:
    set PYTHONPATH=src
    .venv/Scripts/python.exe tests/test_pullback_entry.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from okx_ema_trader.strategy import PullbackEntry, classify_env, evaluate_env

ADX_MIN = 20.0
DEV_MAX = 1.0          # deviation gate off, so these tests are about the entry


def bullish(**over) -> dict:
    """A 5m bar in a 15m bullish environment, sitting slightly above the EMA."""
    bar = {"close": 100.0, "high": 100.2, "low": 99.8, "ema_fast": 100.0,
           "ema_slow": 99.0, "prev_ema_fast": 99.5, "prev_ema_slow": 99.2}
    bar.update(over)
    return bar


def bearish(**over) -> dict:
    bar = {"close": 100.0, "high": 100.2, "low": 99.8, "ema_fast": 100.0,
           "ema_slow": 101.0, "prev_ema_fast": 100.5, "prev_ema_slow": 100.8}
    bar.update(over)
    return bar


def env_bull(**over) -> dict:
    e = {"close": 100.0, "ema_fast": 101.0, "ema_slow": 99.0, "adx": 30.0,
         "prev_ema_fast": 100.5, "prev_ema_slow": 99.5}
    e.update(over)
    return e


def env_bear(**over) -> dict:
    e = {"close": 100.0, "ema_fast": 99.0, "ema_slow": 101.0, "adx": 30.0,
         "prev_ema_fast": 99.5, "prev_ema_slow": 100.5}
    e.update(over)
    return e


def main() -> int:
    failures = 0

    def check(cond: bool, label: str) -> None:
        nonlocal failures
        if not cond:
            failures += 1
            print(f"FAIL: {label}")

    # ---- 1. a long fires when a shallow dip reaches the fast EMA ---------
    rule = PullbackEntry()
    bar = bullish(low=99.95, close=100.0)    # dipped 0.05%, EMA is 100.0
    got = rule(bar, env_bull(), ADX_MIN, DEV_MAX)
    check(got is not None and got.side == "long", "a shallow long dip fires")
    check(got is not None and "pullback" in got.reason,
          f"the reason names the pullback, got {got!r}")

    # ---- 2. THE LATCH: the same dip must not fire twice -------------------
    got2 = rule(bar, env_bull(), ADX_MIN, DEV_MAX)
    check(got2 is None, "the same dip must not fire again (this is the latch)")

    # ---- 3. re-arm requires closing back beyond the EMA ------------------
    rule2 = PullbackEntry()
    rule2(bullish(low=99.95), env_bull(), ADX_MIN, DEV_MAX)      # fires
    # Still touching the line: no re-arm even though bars have passed.
    check(rule2(bullish(low=99.95, close=100.01), env_bull(), ADX_MIN,
                DEV_MAX) is None,
          "touching the line repeatedly must not re-arm")
    # A close clearly back above the band re-arms it.
    check(rule2(bullish(low=100.2, close=100.5), env_bull(), ADX_MIN,
                DEV_MAX) is None,
          "a close beyond the EMA re-arms without firing")
    # ...and the next dip is allowed to fire again.
    got3 = rule2(bullish(low=99.9, close=100.0), env_bull(), ADX_MIN, DEV_MAX)
    check(got3 is not None, "after a re-arm the next dip fires")

    # ---- 4. shorts mirror longs ------------------------------------------
    rule3 = PullbackEntry()
    got = rule3(bearish(high=100.1, close=100.0), env_bear(), ADX_MIN, DEV_MAX)
    check(got is not None and got.side == "short",
          "a rally into the fast EMA fires a short")
    check(rule3(bearish(high=100.05), env_bear(), ADX_MIN, DEV_MAX) is None,
          "the same rally must not fire a short twice")

    # ---- 5. the environment is NOT the entry rule's to override ----------
    rule4 = PullbackEntry()
    check(rule4(bullish(low=99.95), env_bull(adx=10.0), ADX_MIN, DEV_MAX) is None,
          "weak ADX must block a pullback exactly as it blocks a crossover")
    rule5 = PullbackEntry()
    check(rule5(bullish(low=99.95), {"close": 130.0, "ema_fast": 100.0,
                                    "ema_slow": 99.0, "adx": 30.0},
                ADX_MIN, 0.02) is None,
          "the deviation gate must still apply")
    rule6 = PullbackEntry()
    check(rule6(bullish(low=99.95), env_bull(ema_fast=99.0, ema_slow=99.0),
                ADX_MIN, DEV_MAX) is None,
          "EMA20 == EMA50 has no direction and must not trade")

    # ---- 6. a rejected environment re-arms the latch ----------------------
    # Otherwise a rule that fired during a dip could stay silent for the rest
    # of a trend just because one bar was briefly out of tolerance.
    rule7 = PullbackEntry()
    rule7(bullish(low=99.95), env_bull(), ADX_MIN, DEV_MAX)      # fires
    rule7(bullish(low=99.95), env_bull(adx=5.0), ADX_MIN, DEV_MAX)  # rejected
    got = rule7(bullish(low=99.95), env_bull(), ADX_MIN, DEV_MAX)
    check(got is not None, "an environment rejection must re-arm the latch")

    # ---- 7. tolerance actually widens the band ---------------------------
    # The band is two-sided: price must come DOWN TO the fast EMA from above,
    # not merely be somewhere below it. Without the lower edge a long's
    # `low <= fast` is true on almost every bar and `tolerance` does nothing.
    tight = PullbackEntry(tolerance=0.0005)
    wide = PullbackEntry(tolerance=0.02)
    far_bar = bullish(low=99.0, close=100.0)      # 1% below the EMA
    check(tight(far_bar, env_bull(), ADX_MIN, DEV_MAX) is None,
          "0.05% tolerance must reject a 1% dip — it is a crash, not a dip")
    check(wide(far_bar, env_bull(), ADX_MIN, DEV_MAX) is not None,
          "2% tolerance must accept a 1% dip")
    crashed = bullish(low=90.0, close=100.0)      # 10% below the EMA
    check(wide(crashed, env_bull(), ADX_MIN, DEV_MAX) is None,
          "even a 2% tolerance must reject a 10% collapse")
    short_far = bearish(high=101.0, close=100.0)  # 1% above the EMA
    check(PullbackEntry(tolerance=0.0005)(
        short_far, env_bear(), ADX_MIN, DEV_MAX) is None,
        "the short side must be symmetric: a 0.05% band rejects a 1% rally")

    # ---- 8. the shared environment helper agrees with classify -----------
    # If these two disagree, the two entries are trading different markets.
    for ind5, ind15, label in ((bullish(), env_bull(), "bullish"),
                                (bearish(), env_bear(), "bearish"),
                                (bullish(), env_bull(adx=1.0), "weak adx")):
        allowed, _ = classify_env(ind5, ind15, ADX_MIN, DEV_MAX)
        helper = evaluate_env(ind5, ind15, ADX_MIN, DEV_MAX)
        check((allowed is None) == (helper is None),
              f"evaluate_env and classify_env agree on {label}")
        if allowed is not None:
            check(allowed.side == helper.side,
                  f"evaluate_env returns the same side for {label}")

    print(f"{'FAILED: ' + str(failures) if failures else 'OK'}"
          f"  ({failures} check(s) failed)")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)


def test_pullback_entry_checks_all_pass():
    """pytest entry point.

    Without this, `pytest tests/test_pullback_entry.py` reports "no tests ran"
    and a green suite means nothing: the file's checks only executed when run
    by hand. `main()` still returns a failure count for the direct-run style
    used elsewhere in this directory.
    """
    assert main() == 0