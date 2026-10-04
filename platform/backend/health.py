"""Liveness and survival: is the system still talking to OKX, and does it have
a brake that does not depend on the strategy's opinion?

Two independent concerns live here because they are the two ways this platform
can lie to you:

1.  HEALTH — the background loops must never fail *silently*. The original
    `except Exception: continue` kept the process alive but discarded the reason,
    so a three-hour proxy outage looked exactly like a healthy idle system. A
    loop that cannot be observed is a loop that cannot be trusted.

2.  RISK — the strategy's exit is a *signal* (EMA cross the other way). A signal
    is a forecast; it can be wrong, and while it is wrong the position is
    unprotected. A stop-loss is not a signal, it is a rule. The only thing that
    should be allowed to veto a stop-loss is the exchange going down, and even
    then we must keep trying.

Design rules this module follows:
  * no import of paper/market/autotrader — they import health, so importing them
    here would close the cycle. Risk decisions are handed in as plain numbers.
  * failure to enforce a stop is an error worth surfacing, never a `pass`.
  * every transition is timestamped from an injectable clock so tests do not
    have to sleep.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# A failure shorter than this is noise (one proxy hiccup). Only a sustained
# outage is worth turning the badge red for.
DEFAULT_STALE_AFTER_S = 60.0


# --------------------------------------------------------------------------
# Connection health
# --------------------------------------------------------------------------
@dataclass
class HealthState:
    """Rolling record of the last time each background loop succeeded.

    `last_ok_at` is the only field the UI trusts: it is the newest moment we
    know for a fact that OKX answered. `last_error` is kept for the tooltip —
    a bare "offline" badge does not tell the user whether to fix the proxy or
    the network cable.
    """

    last_ok_at: int = 0
    last_error: str = ""
    error_since: int = 0          # 0 while connected
    failures: int = 0             # consecutive failures
    polls: int = 0
    now_ms: int = 0
    # True when the loop is alive but did NOT touch the network this pass
    # (auto-trading switched off). It must never be reported as connectivity.
    idle: bool = False

    @property
    def connected(self) -> bool:
        """We have positive proof that OKX answered, and nothing says otherwise.

        An idle loop is excluded on purpose: it made no request, so it has no
        evidence either way. Reporting `True` there would resurrect an old
        timestamp and paint a green badge over a live outage.
        """
        return self.last_ok_at > 0 and self.error_since == 0 and not self.idle

    @property
    def loop_alive(self) -> bool:
        """The loop itself is healthy — idle counts, an error does not.

        This, not `connected`, is what a mixed-health badge should AND together:
        a loop that is parked by design must not drag the light red, but a loop
        that is throwing must.
        """
        return self.error_since == 0

    def offline_for_s(self, now_ms: int | None = None) -> int:
        if not self.error_since:
            return 0
        now = now_ms if now_ms is not None else self.now_ms
        return max(0, (now - self.error_since) // 1000)

    def as_dict(self, now_ms: int | None = None) -> dict:
        now = now_ms if now_ms is not None else self.now_ms
        return {
            "connected": self.connected,
            "last_ok_at": self.last_ok_at,
            "stale_for_s": max(0, (now - self.last_ok_at) // 1000) if self.last_ok_at else -1,
            "offline_for_s": self.offline_for_s(now),
            "last_error": self.last_error,
            "failures": self.failures,
            "polls": self.polls,
            "idle": self.idle,
            "loop_alive": self.loop_alive,
        }


class HealthMonitor:
    """Thread-safe success/failure recorder.

    One instance per named loop. `ok()` clears the error and keeps the older of
    the two timestamps when several loops report into one monitor, so "last
    successful contact" means the newest moment *every* loop was alive.
    """

    def __init__(self, clock=None) -> None:
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._state = HealthState()

    def ok(self) -> None:
        with self._lock:
            now = self._clock()
            s = self._state
            s.failures = 0
            s.last_error = ""
            s.error_since = 0
            s.polls += 1
            s.idle = False
            # The newest moment we know for a fact that OKX answered. Freshness
            # is what the badge promises, so a success always moves it forward.
            s.last_ok_at = now
            s.now_ms = now

    def idle(self) -> None:
        """The loop ran, but made no request — it has nothing to report.

        Used when auto-trading is switched off. Crucially this does NOT touch
        `last_ok_at`: stamping "OKX answered at <now>" for a pass that never
        dialled out would make the badge green during an outage.

        It DOES clear the error state, though — switching auto-trading off is a
        legitimate way to stop this loop from being the one that is red. The
        watchdog still probes unconditionally, so a dead proxy stays visible.
        """
        with self._lock:
            now = self._clock()
            s = self._state
            s.failures = 0
            s.last_error = ""
            s.error_since = 0
            s.polls += 1
            s.idle = True
            s.now_ms = now

    def fail(self, reason: str) -> None:
        with self._lock:
            now = self._clock()
            s = self._state
            s.polls += 1
            s.failures += 1
            s.idle = False
            s.last_error = str(reason)[:300]
            if not s.error_since:
                s.error_since = now
                # Entering an outage: stamp the last known-good moment so the UI
                # can say "data is as of HH:MM" rather than only "offline".
                s.last_ok_at = s.last_ok_at or now
            s.now_ms = now

    def snapshot(self) -> dict:
        with self._lock:
            return self._state.as_dict(self._clock())

    # Convenience for the badge: connectivity is a single boolean.
    @property
    def connected(self) -> bool:
        return self.snapshot()["connected"]


# --------------------------------------------------------------------------
# Account-level circuit breaker
# --------------------------------------------------------------------------
@dataclass
class RiskLimits:
    """Circuit-breaker thresholds. Separate from strategy config on purpose.

    `max_loss_pct` is a percentage of the *position's margin*, not of notional:
    with 5x leverage a 20% adverse move wipes out all the margin in the position,
    so a notional-based threshold would fire far too late to be a brake.
    """

    enabled: bool = True
    max_loss_pct: float = 60.0      # of position margin
    max_daily_loss_pct: float = 0.0  # of account equity; 0 disables
    max_consecutive_losses: int = 0  # 0 disables
    # Close when the mark is within this many PERCENT of the liquidation price.
    # 5x puts liq 20% away, so 3.0 means "exit once a third of the way there".
    liq_buffer_pct: float = 3.0


@dataclass
class RiskDecision:
    should_close: bool = False
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"should_close": self.should_close, "reason": self.reason,
                **self.detail}


def evaluate_risk(positions: list[dict], *, equity: float, initial: float,
                  limits: RiskLimits, consecutive_losses: int = 0) -> RiskDecision:
    """Should any open position be closed right now, ignoring the strategy?

    `positions` are the dicts from `paper.account_summary()['positions']`, which
    already carry `margin`, `unrealised_pnl`, `liq_price` and `mark_price`.
    Every returned decision names the single worst reason so the UI can show one
    clear cause instead of a list of near-misses.
    """
    if not limits.enabled:
        return RiskDecision()

    # 0. Losing streak. A strategy that has just lost N times in a row is the
    #    exact moment to stop letting it drive: the edge (if any) is not showing
    #    up, and the next entry is a coin flip with fees attached.
    if (limits.max_consecutive_losses > 0
            and consecutive_losses >= limits.max_consecutive_losses):
        return RiskDecision(
            should_close=True, reason="losing-streak",
            detail={"streak": consecutive_losses,
                    "limit": limits.max_consecutive_losses},
        )

    # 1. Per-position checks. Collect EVERY condemned position, not just the
    #    worst one: two positions can bleed past the margin cap at the same time,
    #    and stopping only the worst leaves the other one bleeding. A test caught
    #    exactly this (a -40% margin loss tripped before a -50% account drawdown,
    #    so the account-level flatten never ran).
    condemned: dict[int, str] = {}
    for p in positions:
        margin = float(p.get("margin") or 0.0)
        if margin <= 0:
            continue
        loss_pct = -float(p.get("unrealised_pnl") or 0.0) / margin * 100.0
        if loss_pct > limits.max_loss_pct:
            condemned[p["id"]] = f"position-stop:{p.get('inst_id')}"
        # 2. Liquidation proximity. At 5x the liq price is 20% away; a mark
        #    within a few percent of it means the position is nearly dead.
        liq = p.get("liq_price")
        mark = p.get("mark_price")
        if liq and mark:
            distance = abs(float(mark) - float(liq)) / abs(float(liq)) * 100.0
            if distance <= limits.liq_buffer_pct:
                condemned[p["id"]] = f"liquidation-risk:{p.get('inst_id')}"

    # 3. Daily account loss. Counted from `initial` because `equity` already
    #    includes unrealised PnL, which is what we want: the brake should react
    #    to open positions bleeding, not only to closed ones.
    account_level = ""
    if limits.max_daily_loss_pct > 0 and initial > 0:
        drawdown = (initial - equity) / initial * 100.0
        if drawdown >= limits.max_daily_loss_pct:
            account_level = "account-drawdown"

    if account_level:
        # An account-level breach condemns EVERYTHING. Any per-position stop we
        # already found is irrelevant next to "the account is down a quarter" —
        # and leaving the survivors open is how a bad hour becomes a bad day.
        return RiskDecision(
            should_close=True, reason=account_level,
            detail={"drawdown_pct": round(
                        (initial - equity) / initial * 100.0, 2),
                    "limit_pct": limits.max_daily_loss_pct,
                    "condemned": sorted(condemned) or "ALL"},
        )
    if condemned:
        # Report the first condemned position, but the caller closes them all.
        first = positions[0]
        for p in positions:
            if p["id"] in condemned:
                first = p
                break
        return RiskDecision(
            should_close=True, reason=condemned[first["id"]],
            detail={"order_id": first["id"], "inst_id": first.get("inst_id"),
                    "condemned": sorted(condemned)},
        )
    return RiskDecision()


def consecutive_losses(store) -> int:
    """How many closed trades in a row have lost money, counting back from now.

    A winning trade resets the count to zero. `pnl IS NULL` trades are skipped
    rather than counted as wins: an unpriced close is not good news.
    """
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT pnl FROM orders WHERE status='closed' AND pnl IS NOT NULL "
            "ORDER BY COALESCE(closed_at, created_at) DESC, id DESC"
        ).fetchall()
    streak = 0
    for r in rows:
        if float(r["pnl"]) < 0:
            streak += 1
        else:
            break
    return streak


def enforce_risk(store, *, limits: RiskLimits, marks: dict | None = None,
                 exit_prices: dict | None = None) -> dict:
    """Close every position the circuit breaker condemns.

    `marks` / `exit_prices` come from the caller's price cache so that judging
    and filling use the same price instant — a stop computed off one tick and
    filled at another is not a stop.

    Returns a report for the UI. Deliberately keeps going after one failure: a
    broker hiccup on position A must not leave position B unprotected. Only when
    EVERY close failed do we report the error loudly.
    """
    import paper  # local import: paper must not depend on this module

    report: dict = {"closed": [], "failed": [], "checked": 0, "reason": "",
                    "error": ""}
    try:
        account = paper.account_summary(store, marks, exit_prices)
    except Exception as exc:                      # store unreadable
        report["error"] = f"无法读取账户：{exc}"
        return report

    positions = account.get("positions") or []
    report["checked"] = len(positions)
    if not positions:
        return report

    equity = float(account.get("equity") or 0.0)
    initial = float(account.get("initial") or 0.0)
    decision = evaluate_risk(positions, equity=equity, initial=initial,
                             limits=limits,
                             consecutive_losses=consecutive_losses(store))
    if not decision.should_close:
        return report
    report["reason"] = decision.reason

    # `condemned` is computed by the decision itself, so the caller cannot
    # disagree with the rule about who gets closed. An account-level reason
    # condemns every position; a per-position reason condemns the ones named.
    listed = decision.detail.get("condemned")
    if listed == "ALL" or decision.reason == "losing-streak":
        condemned = {p["id"] for p in positions}
    elif listed:
        condemned = set(listed)
    else:
        named = decision.detail.get("order_id")
        condemned = {named} if named is not None else set()

    for p in positions:
        if p.get("id") not in condemned:
            continue
        price = _exit_price(p)
        if price is None:
            report["failed"].append(
                {"id": p.get("id"), "error": f"{p.get('inst_id')} 没有可用价格，无法平仓"})
            continue
        try:
            out = paper.close_position(store, p["id"], price)
            report["closed"].append({"id": p["id"], "pnl": out["pnl"],
                                     "reason": decision.reason})
        except Exception as exc:
            report["failed"].append({"id": p.get("id"), "error": str(exc)})

    if report["failed"] and not report["closed"]:
        report["error"] = (
            f"风控触发（{decision.reason}）但平仓失败：{report['failed'][0]['error']}")
    else:
        report["reason"] = decision.reason
    return report


def _exit_price(position: dict):
    """Price to close at, crossing the spread the honest way.

    A stop-loss that fills at the last mark price is a lie: exiting a long needs
    the bid (what a buyer will actually pay), not the mid. Falls back to the
    mark only when the ticker is unavailable — a slightly wrong price still beats
    leaving a condemned position open.
    """
    side = position.get("side")
    preferred = "bid" if side == "long" else "ask"
    for key in (preferred, "mark_price"):
        value = position.get(key)
        if value:
            return float(value)
    return None
