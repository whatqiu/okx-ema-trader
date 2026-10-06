"""Tests for the three holes in the account-level circuit breaker.

What is pinned down here, and why each one matters:

1.  The brake runs on EVERY auto-loop pass, including the ones that do no
    trading. It used to sit after the `continue` taken when auto-trading was off
    or the watch list was empty — the two states where an open position is most
    unattended. A position opened by hand from the UI had NO brake at all while
    the badge stayed green.

2.  The brake LATCHES. Closing everything is not the same as stopping: the old
    behaviour flattened the book and then let the next crossover re-open it,
    paying the spread on both sides forever. The halt survives passes, restarts,
    and is cleared only by a human.

3.  Clearing actually works. A reset that leaves the five losses in place
    re-latches on the very next pass, so the button would be decoration — the
    losing-streak counter is re-based at reset, on purpose.

4.  The drawdown baseline is NOT the account's opening balance. This brake was
    installed long after most of these accounts were opened, so charging them
    for losses booked before it existed latches a halt on the first pass after
    an upgrade — and the operator cannot clear their way out of it. The baseline
    is pinned to the equity at install time and then left alone.

Run:
    .venv/Scripts/python.exe tests/test_platform_risk_halt.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "platform" / "backend"))

import autotrader  # noqa: E402
import deps  # noqa: E402
import health  # noqa: E402
import main  # noqa: E402
import paper  # noqa: E402
from okx_ema_trader.storage import Store  # noqa: E402
from okx_ema_trader.strategy import Signal  # noqa: E402

INST = "MU-USDT-SWAP"
STEP5 = 300_000
STEP15 = 900_000
BASE_TS = 1_790_000_000_000  # fixed past ts; only ordering matters here

_checks = 0


def check(condition: bool, message: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(message)


def fresh_store():
    tmp = tempfile.TemporaryDirectory()
    return Store(Path(tmp.name) / "test.db"), tmp


def seed_candles(store, n5: int = 120, n15: int = 60, price: float = 100.0):
    """Enough confirmed bars to clear indicator warmup (ema_slow=50).

    Prices must MOVE: perfectly flat synthetic bars give +DM == -DM == 0, so
    Wilder's DX divides 0 by 0 and ADX is NaN forever.
    """
    def bar(ts, p):
        return [str(ts), f"{p:.4f}", f"{p + 0.6:.4f}", f"{p - 0.6:.4f}",
                f"{p + 0.2:.4f}", "10", "1000", "1000", "1"]

    rows5 = [bar(BASE_TS + i * STEP5, price + i * 0.05 + (i % 3) * 0.15)
             for i in range(n5)]
    rows15 = [bar(BASE_TS + i * STEP15, price + i * 0.15 + (i % 3) * 0.3)
              for i in range(n15)]
    store.upsert_candles(INST, "5m", rows5)
    store.upsert_candles(INST, "15m", rows15)
    return BASE_TS + (n5 - 1) * STEP5


def fake_tick(store, inst):
    return {"bid": 99.5, "ask": 100.5, "last": 100.0}


def long_sig(*a, **kw):
    return Signal("long", "test cross")


def short_sig(*a, **kw):
    return Signal("short", "test cross")


LIMITS = health.RiskLimits(enabled=True, max_loss_pct=60.0,
                           max_drawdown_pct=25.0, max_consecutive_losses=5,
                           liq_buffer_pct=3.0)


def _book_closed(store, pnl, reason="t"):
    """Add one already-closed trade with a known PnL."""
    oid = store.add_order(INST, "long", contracts=1, entry_price=100.0,
                          notional=100.0, leverage=1.0, status="filled")
    store.close_order(oid, 100.0, pnl=pnl, reason=reason)
    return oid


def _bleed(store, price: float = 90.0):
    """A long that must be stopped: -100 on 100 margin at 10x."""
    paper.open_position(store, INST, "long", notional=1000.0, leverage=10.0,
                        price=100.0)
    return health.enforce_risk(store, limits=LIMITS, marks={INST: price},
                               exit_prices={INST: (price - 0.1, price + 0.1)})


def _run_one_pass(store, marks=None, exit_prices=None):
    """Run exactly one background auto-loop pass against a temp store.

    `deps.store()` is redirected rather than the loop being started: the loop
    sleeps 15s between passes and never returns, which is unusable in a test.
    `_marks` / `_exit_prices` are stubbed so the pass cannot reach OKX.
    """
    saved = (deps.store, main._marks, main._exit_prices)
    deps.store = lambda: store
    main._marks = lambda s: marks or {}
    main._exit_prices = lambda s: exit_prices or {}
    try:
        main._auto_pass()
    finally:
        deps.store, main._marks, main._exit_prices = saved


# --------------------------------------------------------------------------
# Hole 1: the brake ran only when the strategy was switched on
# --------------------------------------------------------------------------
def test_risk_runs_when_auto_trading_is_off():
    """Regression: the brake used to sit AFTER the "disabled" continue.

    This is the case the brake exists for — a position opened by hand from the
    UI, or one left running overnight with auto-trading off — and it had no
    protection at all while the badge stayed green.
    """
    store, tmp = fresh_store()
    before = main._auto_health.snapshot()["last_ok_at"]
    try:
        paper.open_position(store, INST, "long", notional=1000.0,
                            leverage=10.0, price=100.0)
        autotrader.set_state(store, enabled=False, symbol=INST)
        _run_one_pass(store, marks={INST: 90.0},
                      exit_prices={INST: (89.9, 90.1)})
        check(len(store.open_orders()) == 0,
              f"自动交易关闭时风控必须仍然平仓: {store.open_orders()}")
        check(len(main._last_risk.get("closed") or []) == 1,
              f"报告必须记录这次平仓: {main._last_risk}")
        snap = main._auto_health.snapshot()
        check(snap["last_error"] == "", f"这一轮不该有错误: {snap}")
        # `idle()` semantics are preserved: auto-trading off is not proof that
        # OKX answered, so an idle pass must not move `last_ok_at`.
        check(snap["idle"] is True, "没有交易的一轮应记为 idle")
        check(snap["last_ok_at"] == before,
              "idle 不得推进 last_ok_at —— 这一轮根本没联网")
    finally:
        store.close(); tmp.cleanup()


def test_risk_runs_with_an_empty_watch_list():
    """Same hole from the other side: `symbols == []` used to skip the brake too."""
    store, tmp = fresh_store()
    try:
        paper.open_position(store, INST, "long", notional=1000.0,
                            leverage=10.0, price=100.0)
        autotrader.set_state(store, enabled=True, symbols=[])
        check(autotrader.get_state(store)["symbols"] == [],
              "前置条件：监控列表为空")
        _run_one_pass(store, marks={INST: 90.0},
                      exit_prices={INST: (89.9, 90.1)})
        check(len(store.open_orders()) == 0,
              "空监控列表但仍有持仓时，风控必须运行")
    finally:
        store.close(); tmp.cleanup()


def test_a_broken_watch_list_does_not_disable_the_brake():
    """Reading the watch list can fail (corrupt meta) — it must not take the
    account's only protection down with it."""
    store, tmp = fresh_store()
    try:
        paper.open_position(store, INST, "long", notional=1000.0,
                            leverage=10.0, price=100.0)
        autotrader.set_state(store, enabled=True, symbol=INST)
        store.set_meta("auto_symbols", "{not json at all")
        _run_one_pass(store, marks={INST: 90.0},
                      exit_prices={INST: (89.9, 90.1)})
        check(len(store.open_orders()) == 0,
              "监控列表读不出来也要先跑风控（有持仓就要检查）")
    finally:
        store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# Hole 2: the brake closed, but never stopped opening
# --------------------------------------------------------------------------
def test_account_drawdown_latches_the_halt():
    store, tmp = fresh_store()
    other = "ZEC-USDT-SWAP"
    try:
        paper.open_position(store, INST, "long", notional=5000.0, leverage=5.0,
                            price=100.0)
        paper.open_position(store, other, "long", notional=5000.0, leverage=5.0,
                            price=50.0)
        # Pins the drawdown baseline while the account is still healthy. Without
        # this pass the baseline would be taken AFTER the crash and the brake
        # would measure a drawdown of zero against it — see `risk_baseline`.
        health.enforce_risk(store, limits=LIMITS,
                            marks={INST: 100.0, other: 50.0})
        report = health.enforce_risk(
            store, limits=LIMITS, marks={INST: 60.0, other: 30.0},
            exit_prices={INST: (59.9, 60.1), other: (29.9, 30.1)})
        check(len(report["closed"]) == 2, f"账户级回撤必须清仓: {report}")
        check(report["halted"] is True, f"账户级回撤必须熔断: {report}")
        check(report["halt_reason"] == "account-drawdown", f"{report}")
        # Next pass, book already flat: a latched halt is not a per-pass result.
        again = health.enforce_risk(store, limits=LIMITS)
        check(again["halted"] is True, "熔断不会自己解除")
    finally:
        store.close(); tmp.cleanup()


def test_a_single_position_stop_does_not_latch_the_halt():
    """One bleeding position is a position problem, not an account problem."""
    store, tmp = fresh_store()
    try:
        report = _bleed(store)
        check(len(report["closed"]) == 1, f"止损必须平掉这个仓位: {report}")
        check(report["halted"] is False, "单仓位止损不该熔断整个账户")
        check(health.risk_halted(store) is False, "也不该留下熔断标记")
    finally:
        store.close(); tmp.cleanup()


def test_losing_streak_halts_even_with_a_flat_book():
    """The streak is a fact about CLOSED trades, so it must be judged before
    the next entry — not discovered on the position it just opened."""
    store, tmp = fresh_store()
    try:
        for i in range(5):
            _book_closed(store, -1.0, f"t{i}")
        report = health.enforce_risk(store, limits=LIMITS)
        check(report["halted"] is True, f"连亏5笔必须熔断: {report}")
        check(report["closed"] == [], "空仓时没有东西可平")
        check(health.risk_halt_state(store)["reason"] == "losing-streak",
              "熔断原因要留给 UI")
    finally:
        store.close(); tmp.cleanup()


def test_halt_blocks_a_plain_entry():
    store, tmp = fresh_store()
    seed_candles(store)
    autotrader.set_state(store, enabled=True, symbol=INST, notional=100,
                         leverage=5)
    health.set_risk_halt(store, "losing-streak")
    out = autotrader.maybe_trade_all(store, signal_fn=long_sig,
                                     ticker_fn=fake_tick, top_up=False)
    check(out["halted"] is True, "扫描结果必须带出熔断状态")
    check(store.open_orders() == [], f"熔断中不得开新仓: {store.open_orders()}")
    check(out["results"][0].get("why") == "risk halt",
          f"跳过原因要能显示在扫描表里: {out['results'][0]}")
    store.close(); tmp.cleanup()


def test_halt_still_lets_a_reversal_close():
    """Reducing risk never needs permission — even under a halt.

    But the OPEN half of the same reversal does: flipping short to long would be
    adding exposure through the back door.
    """
    store, tmp = fresh_store()
    seed_candles(store)
    autotrader.set_state(store, enabled=True, symbol=INST, notional=100,
                         leverage=5)
    paper.open_position(store, INST, "short", notional=100.0, leverage=5.0,
                        price=100.0)
    health.set_risk_halt(store, "losing-streak")
    out = autotrader.maybe_trade_all(store, signal_fn=long_sig,
                                     ticker_fn=fake_tick, top_up=False)
    first = out["results"][0]
    check(len(first.get("closed") or []) == 1,
          f"反向仓必须被平掉: {first}")
    check(first.get("acted") is True, f"这一轮确实行了平仓: {first}")
    check(store.open_orders() == [],
          f"平完反向仓不得再开新仓: {store.open_orders()}")
    store.close(); tmp.cleanup()


def test_without_a_halt_the_same_signal_still_flips():
    """Control for the test above: the gate must not have frozen reversals."""
    store, tmp = fresh_store()
    seed_candles(store)
    autotrader.set_state(store, enabled=True, symbol=INST, notional=100,
                         leverage=5)
    paper.open_position(store, INST, "short", notional=100.0, leverage=5.0,
                        price=100.0)
    autotrader.maybe_trade_all(store, signal_fn=long_sig, ticker_fn=fake_tick,
                               top_up=False)
    opens = store.open_orders()
    check(len(opens) == 1 and opens[0]["side"] == "long",
          f"没有熔断时反向信号应当正常翻仓: {opens}")
    store.close(); tmp.cleanup()


def test_halt_survives_a_new_store_instance():
    """Persisted in meta, not in memory: a restart must not forget it."""
    store, tmp = fresh_store()
    try:
        health.set_risk_halt(store, "account-drawdown")
        rebooted = Store(store.path)
        try:
            check(health.risk_halted(rebooted) is True,
                  "熔断必须持久化：新进程也要记得自己在熔断")
            check(health.risk_halt_state(rebooted)["reason"] ==
                  "account-drawdown", "原因也要一起持久化")
            check(health.risk_halt_state(rebooted)["at"] > 0,
                  "熔断时间要留给 UI 展示")
        finally:
            rebooted.close()
    finally:
        store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# The drawdown baseline: install time, not account-opening time
# --------------------------------------------------------------------------
def test_first_pass_forgives_losses_booked_before_the_brake_existed():
    """An account already down 30% when this version ships must NOT halt.

    Measuring from the opening balance would latch the halt on the upgrade's
    first pass, and `clear_risk_halt` does not re-base the drawdown — so the
    operator would be left with a banner they cannot dismiss.
    """
    store, tmp = fresh_store()
    try:
        paper.reset_account(store, 1000.0)
        _book_closed(store, -300.0, "亏损发生在装熔断之前")
        assert paper.account_summary(store)["equity"] == 700.0
        report = health.enforce_risk(store, limits=LIMITS)
        assert report["halted"] is False, f"历史亏损不该追溯: {report}"
        assert report["closed"] == [], f"也不该平掉任何东西: {report}"
        assert float(store.get_meta(health.BASELINE_KEY)) == 700.0, \
            "基准应定在当前权益"
    finally:
        store.close(); tmp.cleanup()


def test_drawdown_against_the_pinned_baseline_still_halts():
    """Same account, but this loss happens AFTER the baseline was pinned."""
    store, tmp = fresh_store()
    try:
        paper.reset_account(store, 1000.0)
        _book_closed(store, -300.0, "历史亏损")
        health.enforce_risk(store, limits=LIMITS)        # pins at 700
        _book_closed(store, -220.0, "新的亏损")           # 700 -> 480 = -31.4%
        assert paper.account_summary(store)["equity"] == 480.0
        report = health.enforce_risk(store, limits=LIMITS)
        assert report["halted"] is True, f"相对基准亏 31.4% 必须熔断: {report}"
        assert report["halt_reason"] == "account-drawdown", f"{report}"
    finally:
        store.close(); tmp.cleanup()


def test_a_restart_does_not_re_pin_the_baseline():
    """Otherwise every restart hands the account a fresh loss allowance."""
    store, tmp = fresh_store()
    try:
        paper.reset_account(store, 1000.0)
        health.enforce_risk(store, limits=LIMITS)        # pins at 1000
        assert float(store.get_meta(health.BASELINE_KEY)) == 1000.0
        _book_closed(store, -300.0, "亏 30%")
        assert health.enforce_risk(store, limits=LIMITS)["halted"] is True

        rebooted = Store(store.path)
        try:
            again = health.enforce_risk(rebooted, limits=LIMITS)
            assert float(rebooted.get_meta(health.BASELINE_KEY)) == 1000.0, \
                "重启不得重新定基，否则每次启动都白送一份亏损额度"
            assert again["halted"] is True, "重启也不该让熔断失效"
        finally:
            rebooted.close()
    finally:
        store.close(); tmp.cleanup()


def test_account_reset_clears_the_baseline():
    """A fresh balance must not inherit the old account's drawdown reference."""
    store, tmp = fresh_store()
    try:
        paper.reset_account(store, 1000.0)
        health.enforce_risk(store, limits=LIMITS)
        assert float(store.get_meta(health.BASELINE_KEY)) == 1000.0

        saved = deps.store
        deps.store = lambda: store
        try:
            body = main.account_reset(main.AccountResetRequest(balance=500.0))
        finally:
            deps.store = saved
        assert body["ok"] is True, f"重置接口失败: {body}"
        assert store.get_meta(health.BASELINE_KEY) == "", \
            "重置账户必须清掉旧基准"

        report = health.enforce_risk(store, limits=LIMITS)
        assert float(store.get_meta(health.BASELINE_KEY)) == 500.0, \
            "清掉之后应按新权益重新定基"
        assert report["halted"] is False, "新账户开户即熔断是荒谬的"
    finally:
        store.close(); tmp.cleanup()


def test_a_store_without_the_baseline_key_still_works():
    """Old databases predate the key entirely."""
    store, tmp = fresh_store()
    try:
        assert store.get_meta(health.BASELINE_KEY) is None, \
            "前置条件：老库没有这个键"
        report = health.enforce_risk(store, limits=LIMITS)
        assert report["error"] == "", f"老库不该报错: {report}"
        assert report["halted"] is False, f"老库不该一上来就熔断: {report}"
        assert float(store.get_meta(health.BASELINE_KEY)) == \
            paper.account_summary(store)["equity"], "按当前权益定基"
    finally:
        store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# Hole 3: the reset has to actually work
# --------------------------------------------------------------------------
def test_manual_reset_clears_the_halt_and_rebases_the_streak():
    """Re-basing is not optional.

    The five losses that caused the halt are still the five most recent closes
    one pass later, so a reset that leaves them counted re-latches instantly —
    the button would be pure decoration.
    """
    store, tmp = fresh_store()
    try:
        for i in range(5):
            _book_closed(store, -1.0, f"t{i}")
        health.enforce_risk(store, limits=LIMITS)
        check(health.risk_halted(store) is True, "前置条件：已熔断")
        health.clear_risk_halt(store)
        check(health.risk_halted(store) is False, "人工解除必须生效")
        check(health.losing_streak(store) == 0, "连亏计数从复位点重算")
        again = health.enforce_risk(store, limits=LIMITS)
        check(again["halted"] is False,
              "否则下一轮立刻重新熔断，复位按钮形同虚设")
    finally:
        store.close(); tmp.cleanup()


def test_a_win_still_counts_after_a_reset():
    """Re-basing must not disable the streak brake for good."""
    store, tmp = fresh_store()
    try:
        for i in range(5):
            _book_closed(store, -1.0, f"t{i}")
        health.set_risk_halt(store, "losing-streak")
        health.clear_risk_halt(store)
        for i in range(5):
            _book_closed(store, -1.0, f"n{i}")
        check(health.losing_streak(store) == 5, "复位后又连亏5笔，计数应为5")
        report = health.enforce_risk(store, limits=LIMITS)
        check(report["halted"] is True, "复位后重新攒够连亏仍要熔断")
    finally:
        store.close(); tmp.cleanup()


def test_risk_reset_endpoint_is_wired_and_clears_the_halt():
    store, tmp = fresh_store()
    try:
        health.set_risk_halt(store, "losing-streak")
        check("/api/risk/reset" in {r.path for r in main.app.routes},
              "解除熔断的接口必须存在")
        saved = deps.store
        deps.store = lambda: store
        try:
            body = main.risk_reset()
        finally:
            deps.store = saved
        check(body["halted"] is False, f"接口应返回已解除: {body}")
        check(health.risk_halted(store) is False, "store 里的熔断标记必须清掉")
        check(main._last_risk.get("halted") is False,
              "缓存的报告也要更新，否则 UI 还挂着旧状态")
    finally:
        store.close(); tmp.cleanup()


# --------------------------------------------------------------------------
# The UI has to be able to see it
# --------------------------------------------------------------------------
def test_status_and_autotrade_state_expose_the_halt():
    store, tmp = fresh_store()
    try:
        health.set_risk_halt(store, "account-drawdown")
        check(autotrader.get_state(store)["halted"] is True,
              "/api/autotrade 必须带出熔断状态")
        check(autotrader.get_state(store)["halt_reason"] == "account-drawdown",
              "/api/autotrade 必须带出熔断原因")
        saved = deps.store
        deps.store = lambda: store
        try:
            body = main.health_status()
        finally:
            deps.store = saved
        check(body["risk"]["halted"] is True,
              f"/api/status 必须带出熔断状态: {body['risk']}")
        check(body["risk"]["halt_reason"] == "account-drawdown", "原因要带出")
    finally:
        store.close(); tmp.cleanup()


def run() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} tests, {_checks} assertions — all green")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
