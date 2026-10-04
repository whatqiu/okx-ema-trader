"""Tests for liveness tracking and the account-level circuit breaker.

The failure modes that matter here are not "does the happy path work" but:
  * a single proxy hiccup must NOT paint the badge red (alarm fatigue kills
    the signal you actually need);
  * after an outage, `last_ok_at` must still say WHEN we last had data — "offline"
    alone cannot tell you whether the price on screen is 5 seconds or 5 hours old;
  * the brake must fire on margin loss, not notional loss (at 5x a notional
    threshold fires an order of magnitude too late);
  * the brake must NOT fire on a profitable or barely-touched position — a
    strategy that gets stopped out by its own risk layer loses money on the
    brake instead of the market;
  * closing one position must not close the others, and an account-level reason
    must close ALL of them;
  * if every close fails, the report must say so loudly rather than returning an
    empty success.

Run:
    .venv/Scripts/python.exe tests/test_platform_health.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "platform" / "backend"))

import health  # noqa: E402
import paper  # noqa: E402
from okx_ema_trader.storage import Store  # noqa: E402

INST = "MU-USDT-SWAP"
_checks = 0


def check(condition: bool, message: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(message)


def fresh_store():
    tmp = tempfile.TemporaryDirectory()
    return Store(Path(tmp.name) / "test.db"), tmp


def fake_clock(start_ms: int = 1_700_000_000_000):
    """A clock the test advances by hand — no sleeping in a unit test."""
    box = {"now": start_ms}

    def clock() -> int:
        return box["now"]

    clock.advance = lambda s: box.__setitem__("now", box["now"] + int(s * 1000))
    return clock


# --------------------------------------------------------------------------
# Health monitor
# --------------------------------------------------------------------------
def test_health_starts_disconnected_but_not_broken():
    clock = fake_clock()
    mon = health.HealthMonitor(clock)
    snap = mon.snapshot()
    # Never contacted OKX yet. Must NOT claim "connected" on an empty record.
    check(not snap["connected"], "a monitor with no success must not claim connected")
    check(snap["last_ok_at"] == 0, "no success yet -> last_ok_at stays 0")
    check(snap["offline_for_s"] == 0, "not in an outage -> offline_for_s is 0")


def test_single_failure_keeps_previous_success_time():
    clock = fake_clock()
    mon = health.HealthMonitor(clock)
    mon.ok()
    good_at = mon.snapshot()["last_ok_at"]
    clock.advance(30)
    mon.fail("ConnectionResetError: proxy died")
    snap = mon.snapshot()
    check(snap["last_ok_at"] == good_at,
          "a failure must not erase when we last had data")
    check(snap["offline_for_s"] == 0,
          "offline_for_s measures from error_since, not from the first error")
    check(snap["failures"] == 1, "failure counted")
    check("proxy died" in snap["last_error"], "reason kept for the tooltip")


def test_offline_duration_accumulates_then_resets():
    clock = fake_clock()
    mon = health.HealthMonitor(clock)
    mon.ok()
    clock.advance(60)
    mon.fail("timeout")
    check(mon.snapshot()["offline_for_s"] == 0, "outage just started -> 0s")
    clock.advance(185)
    check(mon.snapshot()["offline_for_s"] == 185, "185s into the outage")
    clock.advance(5)
    mon.ok()
    snap = mon.snapshot()
    check(snap["connected"], "recovered -> connected")
    check(snap["offline_for_s"] == 0, "recovery clears the outage timer")
    check(snap["failures"] == 0, "recovery resets the failure counter")
    check(snap["last_error"] == "", "recovery clears the stale error")


def test_repeated_failures_keep_first_error_time():
    clock = fake_clock()
    mon = health.HealthMonitor(clock)
    mon.fail("first")
    clock.advance(10)
    mon.fail("second")
    check(mon.snapshot()["failures"] == 2, "consecutive failures counted")
    check(mon.snapshot()["offline_for_s"] == 10,
          "offline_for_s must not restart on every failure")


def test_health_reason_is_preserved_not_swallowed():
    """The whole point: the old code did `except Exception: continue`."""
    clock = fake_clock()
    mon = health.HealthMonitor(clock)
    try:
        raise ValueError("缺少买一/卖一价，无法成交")
    except Exception as exc:
        mon.fail(f"{type(exc).__name__}: {exc}")
    snap = mon.snapshot()
    check(not snap["connected"], "failure means disconnected")
    check("缺少买一" in snap["last_error"], "the actual reason survives")


def test_idle_does_not_claim_a_successful_contact():
    """Regression: auto-trading OFF used to call `ok()`, faking a green badge.

    An idle pass never dialled out, so it must not move `last_ok_at` — otherwise
    the UI shows "已连接" during a real outage whenever the switch is off.
    """
    clock = fake_clock()
    mon = health.HealthMonitor(clock)
    mon.ok()
    good_at = mon.snapshot()["last_ok_at"]
    clock.advance(30)
    mon.idle()
    snap = mon.snapshot()
    check(snap["idle"] is True, "an idle pass is flagged as idle")
    check(snap["last_ok_at"] == good_at,
          "idle must not advance last_ok_at — no request was made")
    check(snap["loop_alive"] is True, "the loop itself is fine")
    check(not snap["connected"],
          "an idle loop must not claim connectivity off a stale timestamp")


def test_idle_from_cold_start_is_not_connected():
    """Idle with no prior success proves nothing, so it must not say 'connected'."""
    clock = fake_clock()
    mon = health.HealthMonitor(clock)
    mon.idle()
    snap = mon.snapshot()
    check(snap["loop_alive"] is True, "idle is not an error")
    check(not snap["connected"],
          "idle alone must never be reported as connectivity")
    check(snap["last_ok_at"] == 0, "no contact was ever made")


def test_idle_resets_the_error_streak_without_claiming_connectivity():
    """Switching auto-trading off is not a fix for a broken proxy.

    `idle()` does clear `error_since` — a parked loop must not stay red forever
    — but it still refuses to report `connected`, because no request was made.
    The watchdog is what decides the badge during an outage.
    """
    clock = fake_clock()
    mon = health.HealthMonitor(clock)
    mon.ok()
    mon.fail("WinError 10061")
    clock.advance(20)
    mon.idle()
    snap = mon.snapshot()
    check(snap["failures"] == 0, "idle is a clean pass, so the streak resets")
    check(snap["last_error"] == "", "no error to show while idle")
    check(not snap["connected"],
          "going idle must not silently turn a red badge green")


def test_ok_and_fail_clear_the_idle_flag():
    clock = fake_clock()
    mon = health.HealthMonitor(clock)
    mon.idle()
    check(mon.snapshot()["idle"] is True, "idle set")
    mon.ok()
    check(mon.snapshot()["idle"] is False, "a real success clears idle")
    mon.idle()
    mon.fail("boom")
    check(mon.snapshot()["idle"] is False, "a failure clears idle")


# --------------------------------------------------------------------------
# Circuit breaker — pure decision logic
# --------------------------------------------------------------------------
def pos(**kw) -> dict:
    base = {"id": 1, "inst_id": INST, "side": "long", "margin": 100.0,
            "unrealised_pnl": 0.0, "liq_price": 80.0, "mark_price": 100.0,
            "bid": 99.9, "ask": 100.1}
    base.update(kw)
    return base


LIMITS = health.RiskLimits(enabled=True, max_loss_pct=60.0,
                           max_daily_loss_pct=25.0, max_consecutive_losses=5,
                           liq_buffer_pct=3.0)


def test_flat_position_never_triggers():
    d = health.evaluate_risk([pos()], equity=10000, initial=10000, limits=LIMITS)
    check(not d.should_close, "a fresh position must be left alone")


def test_small_loss_is_within_tolerance():
    # -30 on 100 margin = -30%, under the 60% cap: noise, not a stop.
    d = health.evaluate_risk([pos(unrealised_pnl=-30.0)], equity=9970,
                             initial=10000, limits=LIMITS)
    check(not d.should_close, "-30% of margin must not trip a 60% brake")


def test_position_stop_fires_on_margin_not_notional():
    """5x, 100 margin, 200 notional. -61 on margin is -30.5% of notional.

    A notional-based threshold of, say, 20% would NOT have fired yet, while the
    margin is already 61% gone. The margin measure is the one that means
    'this position is dead'.
    """
    d = health.evaluate_risk([pos(unrealised_pnl=-61.0)], equity=9939,
                             initial=10000, limits=LIMITS)
    check(d.should_close, "61% of margin gone -> must close")
    check(d.reason.startswith("position-stop"), f"reason: {d.reason}")
    check(d.detail["order_id"] == 1, "the offending order is named")
    check(d.detail["condemned"] == [1], "the caller closes exactly this one")


def test_liquidation_proximity_fires_early():
    # mark 82 vs liq 80 -> 2.5% away, inside the 50%-of-liq-distance buffer.
    d = health.evaluate_risk([pos(mark_price=82.0, unrealised_pnl=-20.0)],
                             equity=9980, initial=10000, limits=LIMITS)
    check(d.should_close, "close to the liq price must force an exit")
    check(d.reason.startswith("liquidation-risk"), f"reason: {d.reason}")


def test_account_drawdown_closes_without_an_order_id():
    d = health.evaluate_risk([pos()], equity=7000, initial=10000, limits=LIMITS)
    check(d.should_close, "30% account drawdown trips a 25% limit")
    check(d.reason == "account-drawdown", f"reason: {d.reason}")
    check(d.detail.get("order_id") is None,
          "account-level reason names no single order")


def test_losing_streak_closes():
    d = health.evaluate_risk([pos()], equity=10000, initial=10000,
                             limits=LIMITS, consecutive_losses=5)
    check(d.should_close, "5 losses in a row must stop the strategy")
    check(d.reason == "losing-streak", f"reason: {d.reason}")


def test_one_loss_short_of_streak_does_not_fire():
    d = health.evaluate_risk([pos()], equity=10000, initial=10000,
                             limits=LIMITS, consecutive_losses=4)
    check(not d.should_close, "4 < 5 must keep trading")


def test_disabled_breaker_never_fires():
    off = health.RiskLimits(enabled=False, max_loss_pct=1.0,
                            max_daily_loss_pct=1.0, max_consecutive_losses=1,
                            liq_buffer_pct=99.0)
    d = health.evaluate_risk([pos(unrealised_pnl=-9999.0, mark_price=1.0)],
                             equity=1, initial=10000, limits=off,
                             consecutive_losses=99)
    check(not d.should_close, "a disabled breaker must not close anything")


def test_worst_position_is_the_one_reported():
    positions = [pos(id=1, unrealised_pnl=-20.0),
                 pos(id=2, unrealised_pnl=-80.0),
                 pos(id=3, unrealised_pnl=-30.0)]
    d = health.evaluate_risk(positions, equity=9870, initial=10000, limits=LIMITS)
    check(d.detail["order_id"] == 2, "the worst loss decides the reason")


def test_two_positions_past_the_cap_are_both_condemned():
    """Regression: only the worst offender used to be closed.

    A single bleeding position does not make the other one safe. If the brake
    picks one victim, the other keeps running toward its own liquidation while
    the UI says the risk is handled.
    """
    positions = [pos(id=1, unrealised_pnl=-70.0),
                 pos(id=2, unrealised_pnl=-75.0)]
    d = health.evaluate_risk(positions, equity=9855, initial=10000, limits=LIMITS)
    check(d.should_close, "both are past the cap")
    check(d.detail["condemned"] == [1, 2],
          f"both must be condemned, got {d.detail.get('condemned')}")


def test_one_failing_position_does_not_condemn_the_healthy_one():
    positions = [pos(id=1, unrealised_pnl=-70.0), pos(id=2, unrealised_pnl=-1.0)]
    d = health.evaluate_risk(positions, equity=9929, initial=10000, limits=LIMITS)
    check(d.detail["condemned"] == [1], "only the offender is condemned")


# --------------------------------------------------------------------------
# Exit price selection
# --------------------------------------------------------------------------
def test_exit_price_uses_bid_for_long_and_ask_for_short():
    check(health._exit_price(pos(side="long", bid=99.9, ask=100.1)) == 99.9,
          "exiting a long sells into the bid")
    check(health._exit_price(pos(side="short", bid=99.9, ask=100.1)) == 100.1,
          "covering a short buys at the ask")


def test_exit_price_falls_back_to_mark_then_none():
    check(health._exit_price(pos(bid=None, ask=None, mark_price=100.0)) == 100.0,
          "no spread -> use the mark rather than abandoning the stop")
    check(health._exit_price(pos(bid=None, ask=None, mark_price=None)) is None,
          "no price at all -> refuse honestly")


# --------------------------------------------------------------------------
# enforce_risk against a real store
# --------------------------------------------------------------------------
def test_enforce_risk_closes_a_bled_position():
    store, tmp = fresh_store()
    try:
        paper.open_position(store, INST, "long", notional=1000.0, leverage=10.0,
                            price=100.0)
        # Drop the mark to 90: 10% adverse on 1000 notional = -100 gross, -100.1
        # after the opening fee already deducted -> beyond 60% of the 100 margin.
        report = health.enforce_risk(
            store, limits=LIMITS, marks={INST: 90.0},
            exit_prices={INST: (89.9, 90.1)})
        check(len(report["closed"]) == 1, f"one close expected: {report}")
        check(report["error"] == "", f"no error expected: {report}")
        check(len(store.open_orders()) == 0, "the position is gone from the store")
    finally:
        store.close(); tmp.cleanup()


def test_enforce_risk_leaves_healthy_position_alone():
    store, tmp = fresh_store()
    try:
        paper.open_position(store, INST, "long", notional=1000.0, leverage=10.0,
                            price=100.0)
        report = health.enforce_risk(
            store, limits=LIMITS, marks={INST: 101.0},
            exit_prices={INST: (100.9, 101.1)})
        check(report["closed"] == [], "a winning position stays open")
        check(report["checked"] == 1, "but it WAS checked")
        check(len(store.open_orders()) == 1, "still holding")
    finally:
        store.close(); tmp.cleanup()


def test_account_drawdown_closes_every_position():
    store, tmp = fresh_store()
    other = "ZEC-USDT-SWAP"
    try:
        paper.open_position(store, INST, "long", notional=5000.0, leverage=5.0,
                            price=100.0)
        paper.open_position(store, other, "long", notional=5000.0, leverage=5.0,
                            price=50.0)
        # Both at ~40% down: equity 10000 -> ~4000, a 60% account drawdown.
        report = health.enforce_risk(
            store, limits=LIMITS,
            marks={INST: 60.0, other: 30.0},
            exit_prices={INST: (59.9, 60.1), other: (29.9, 30.1)})
        check(len(report["closed"]) == 2,
              f"account-level reason must flatten the book: {report}")
        check(len(store.open_orders()) == 0, "nothing left open")
        check(report["reason"] == "account-drawdown", f"reason: {report}")
    finally:
        store.close(); tmp.cleanup()


def test_enforce_risk_on_empty_book_is_a_noop():
    store, tmp = fresh_store()
    try:
        report = health.enforce_risk(store, limits=LIMITS)
        check(report["closed"] == [], "nothing to close")
        check(report["checked"] == 0, "nothing to check")
        check(report["error"] == "", "not an error")
    finally:
        store.close(); tmp.cleanup()


def test_stop_fills_at_the_mark_when_the_spread_is_unavailable():
    """No bid/ask (ticker down) must NOT abandon a condemned position.

    Falling back to the mark realises a slightly different price. Leaving the
    position open instead would let it run to liquidation. The fallback is the
    lesser evil and is deliberate.
    """
    store, tmp = fresh_store()
    try:
        paper.open_position(store, INST, "long", notional=1000.0, leverage=10.0,
                            price=100.0)
        report = health.enforce_risk(
            store, limits=LIMITS, marks={INST: 80.0}, exit_prices={})
        check(len(report["closed"]) == 1,
              f"the brake must still fire without a spread: {report}")
        check(len(store.open_orders()) == 0, "the position is closed")
    finally:
        store.close(); tmp.cleanup()


def test_enforce_risk_reports_loudly_when_close_fails():
    """A brake that cannot pull the trigger must SAY so, not return empty success."""
    store, tmp = fresh_store()
    original = paper.close_position
    try:
        paper.open_position(store, INST, "long", notional=1000.0, leverage=10.0,
                            price=100.0)

        def boom(store_, order_id, price):
            raise RuntimeError("模拟盘接口超时")

        paper.close_position = boom
        try:
            report = health.enforce_risk(
                store, limits=LIMITS, marks={INST: 80.0},
                exit_prices={INST: (79.9, 80.1)})
        finally:
            paper.close_position = original
        check(report["reason"], f"the brake must have fired: {report}")
        check(report["closed"] == [], "nothing actually closed")
        check(len(report["failed"]) == 1, f"the failure must be kept: {report}")
        check("模拟盘接口超时" in report["error"],
              f"the error must reach the UI: {report}")
    finally:
        store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# Losing streak accounting
# --------------------------------------------------------------------------
def _book_closed(store, pnl, reason="t"):
    """Add one already-closed trade with a known PnL (paper.close_order path)."""
    oid = store.add_order(INST, "long", contracts=1, entry_price=100.0,
                          notional=100.0, leverage=1.0, status="filled")
    store.close_order(oid, 100.0, pnl=pnl, reason=reason)
    return oid


def test_consecutive_losses_counts_back_from_now():
    store, tmp = fresh_store()
    try:
        # 3 losses then a win then 2 losses -> streak is 2, not 5.
        for i, pnl in enumerate([-1.0, -2.0, -3.0, 4.0, -1.0, -2.0]):
            _book_closed(store, pnl, f"t{i}")
        check(health.consecutive_losses(store) == 2,
              f"streak should stop at the win: {health.consecutive_losses(store)}")
    finally:
        store.close(); tmp.cleanup()


def test_consecutive_losses_zero_after_a_win():
    store, tmp = fresh_store()
    try:
        for i, pnl in enumerate([1.0, 1.0, 1.0]):
            _book_closed(store, pnl, f"t{i}")
        check(health.consecutive_losses(store) == 0, "winning streak is not a risk")
    finally:
        store.close(); tmp.cleanup()


def test_consecutive_losses_ignores_unpriced_closes():
    """pnl IS NULL is an unpriced close, not good news."""
    store, tmp = fresh_store()
    try:
        _book_closed(store, None, "unpriced")
        _book_closed(store, -5.0, "loss")
        check(health.consecutive_losses(store) == 1,
              "an unpriced close must not break the streak")
    finally:
        store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# account_summary now carries the exit side
# --------------------------------------------------------------------------
def test_account_summary_exposes_bid_and_ask():
    store, tmp = fresh_store()
    try:
        paper.open_position(store, INST, "long", notional=1000.0, leverage=10.0,
                            price=100.0)
        p = paper.account_summary(store, {INST: 100.0},
                                  {INST: (99.9, 100.1)})["positions"][0]
        check(p["bid"] == 99.9, f"bid missing: {p}")
        check(p["ask"] == 100.1, f"ask missing: {p}")
    finally:
        store.close(); tmp.cleanup()


def test_account_summary_tolerates_missing_spread():
    store, tmp = fresh_store()
    try:
        paper.open_position(store, INST, "long", notional=1000.0, leverage=10.0,
                            price=100.0)
        p = paper.account_summary(store, {INST: 100.0})["positions"][0]
        check(p["bid"] is None, "a missing ticker must not crash the summary")
        check(p["mark_price"] == 100.0, "the mark is still there for display")
    finally:
        store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# Wiring — catches what unit tests on `health` alone cannot
# --------------------------------------------------------------------------
def test_main_module_does_not_shadow_the_health_module():
    """Regression: `def health()` at module level shadowed `import health`.

    Every other test here imports `health` directly, so they all passed while
    the real app 500'd on `health.enforce_risk`. The only way to see this is to
    import the module that wires everything together.
    """
    import main
    check(main.health.__name__ == "health",
          "main.health must still be the MODULE, not a route function")
    check(callable(main.health.enforce_risk),
          "main.health.enforce_risk must be reachable from the route")
    check(hasattr(main, "RISK_LIMITS"), "the risk limits must be configured")
    check(main.RISK_LIMITS.enabled, "the circuit breaker must be armed")


def test_risk_check_endpoint_is_wired():
    import main
    paths = {r.path for r in main.app.routes}
    check("/api/status" in paths, "the liveness endpoint must exist")
    check("/api/risk/check" in paths, "the manual risk check must exist")
    check("/api/health" in paths,
          "desktop.py probes /api/health for a live port; do not remove it")


def test_liveness_endpoint_never_reports_upstream_failure():
    """A red /api/health would make desktop.py spawn a second server."""
    import main
    body = main.health_liveness()
    check(body["ok"] is True, "liveness is about the process, not the network")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} tests, {_checks} assertions — all green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
