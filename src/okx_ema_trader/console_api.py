"""Service layer behind the local web console.

Everything the UI can do exists here as a plain function, so the HTTP layer
stays a thin adapter and every operation is testable without a browser.

One rule is inherited unchanged from the rest of the project and is NOT
weakened anywhere in this module: **DEMO ONLY**. `load_demo_credentials`
refuses any profile that does not declare `demo = true`, and `Broker` forces
ccxt into sandbox mode and re-asserts the `x-simulated-trading` header. There
is no code path from this console to a funded account.

Nothing here changes the strategy. Entry decisions still come from
`strategy.classify`; the backtest still calls the same function.
"""
from __future__ import annotations

import json
import math
import os
import re
import tomllib
from pathlib import Path

from . import backtest as backtest_module
from . import http
from . import instruments as instruments_module
from .http import OkxError
from .backtest import fmt_ts, simulate
from .broker import Broker, BrokerError
from .config import Config, load_config
from .credentials import CredentialsError, default_config_path, load_demo_credentials
from .history import fetch_candles
from .indicators import calculate_indicators, indicator_frame
from .strategy import classify

ROOT = Path(__file__).parents[2]
CONFIG_PATH = ROOT / "config" / "config.yaml"
CONSOLE_STATE_PATH = ROOT / "config" / "console.json"
LEDGER_PATH = ROOT / "state.yaml"

# Re-exported: `http` owns these now, older call sites still import them here.
API = http.API
UA = http.UA
NETWORK_HINT = "连不上 OKX。检查代理设置（控制台右上角）或本机网络。"


class ConsoleError(RuntimeError):
    """Something the user can act on: bad symbol, no key, unreachable API."""


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def load_settings(path: Path | None = None) -> dict:
    """Console-local preferences. Missing file = defaults, never an error.

    `proxy` defaults to None (= connect directly). A proxy is only used when
    the user or the detector supplies one, so nobody is silently routed through
    a guessed port.
    """
    settings = {"proxy": None, "profile": None, "symbol": None}
    target = Path(path) if path else CONSOLE_STATE_PATH
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                settings.update({k: v for k, v in raw.items() if k in settings})
        except (json.JSONDecodeError, OSError):
            pass  # a garbled prefs file must not stop the console from starting
    return settings


def default_proxy() -> str | None:
    """What to use when the request itself does not say.

    "Never configured" and "chose to connect directly" are different states and
    have to read differently, otherwise a first run — before anybody has opened
    the settings page — connects directly, fails, and reports "cannot reach OKX"
    on a machine whose system proxy works fine.

    So: an explicit `null` or `""` in console.json means direct, and a MISSING
    key falls back to the machine's own proxy.
    """
    if CONSOLE_STATE_PATH.exists():
        try:
            raw = json.loads(CONSOLE_STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = None
        if isinstance(raw, dict) and "proxy" in raw:
            stored = raw["proxy"]
            if stored is None:
                return None                      # user picked direct
            return str(stored).strip() or None   # "" is direct too
    return http.system_proxy()


def save_settings(patch: dict, path: Path | None = None) -> dict:
    """Only the three known keys are ever written; unknown ones are dropped."""
    target = Path(path) if path else CONSOLE_STATE_PATH
    settings = load_settings(target)
    settings.update({k: v for k, v in patch.items() if k in settings})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def update_config_symbols(symbol: str) -> list[str]:
    """Add `symbol` to `trading.symbols` by editing that ONE LINE.

    The config file carries explanatory comments that a yaml round-trip would
    destroy, so this is a targeted text replacement rather than safe_dump.
    """
    symbol = _normalise_symbol(symbol)
    text = CONFIG_PATH.read_text(encoding="utf-8")
    updated = _replace_symbols_line(text, symbol)
    CONFIG_PATH.write_text(updated, encoding="utf-8")
    return list(load_config(CONFIG_PATH).trading.symbols)


def _replace_symbols_line(text: str, symbol: str) -> str:
    lines = text.splitlines(keepends=True)
    in_trading = False
    for index, line in enumerate(lines):
        if re.match(r"^trading\s*:", line):
            in_trading = True
            continue
        if in_trading and re.match(r"^\s+symbols\s*:", line):
            # Pull the current list out so we never silently drop an entry.
            match = re.search(r"\[(.*)\]", line)
            current: list[str] = []
            if match:
                current = [item.strip().strip("'\"")
                           for item in match.group(1).split(",") if item.strip()]
            if symbol not in current:
                current.append(symbol)
            indent = line[:len(line) - len(line.lstrip())]
            lines[index] = f"{indent}symbols: [{', '.join(current)}]\n"
            return "".join(lines)
    raise ConsoleError("config.yaml 里没找到 trading.symbols 这一行，无法自动写入")


def _finite(value: float) -> float | None:
    """`inf` and `nan` are not valid JSON; the UI renders None as "-".

    `ruin_exposure()` is `inf` whenever the run never dipped below zero, and a
    single bad candle turns every downstream statistic into `nan`. Both have to
    be caught here rather than at the HTTP boundary, because `run_backtest` is
    also used directly by tests and scripts.
    """
    return value if math.isfinite(value) else None


def _normalise_symbol(symbol: str) -> str:
    """Accept `MU`, `MUUSDT`, `mu-usdt-swap` and give back `MU-USDT-SWAP`.

    Anything that is not a USDT-margined swap is rejected: the broker slices on
    "-" to build the ccxt market, and the strategy's sizing assumes USDT quote.
    """
    raw = (symbol or "").strip().upper()
    if not raw:
        raise ConsoleError("币种不能为空")
    if raw.endswith("-USDT-SWAP"):
        return raw
    if raw.endswith("USDT"):
        return f"{raw[:-len('USDT')]}-USDT-SWAP"
    if re.fullmatch(r"[A-Z0-9]{1,20}", raw):
        return f"{raw}-USDT-SWAP"
    raise ConsoleError(f"无法识别的币种格式：{symbol!r}（示例：MU-USDT-SWAP 或 MU）")


# --------------------------------------------------------------------------
# Proxy discovery
#
# Whether a proxy is needed at all depends entirely on the network: OKX is
# reachable directly from some countries and filtered at the TLS/SNI layer from
# others. Rather than guessing one port, we walk the routes a user might already
# have configured and report which one actually works.
# --------------------------------------------------------------------------
COMMON_PROXIES = ("http://127.0.0.1:7890", "http://127.0.0.1:10888",
                  "http://127.0.0.1:10809", "http://127.0.0.1:10808",
                  "http://127.0.0.1:20171")


# Proxy discovery lives in `http` (it is a network concern and `history.py`
# needs it too). These names are kept so existing call sites and tests keep
# working unchanged.
system_proxy = http.system_proxy
_parse_proxy_server = http.parse_proxy_server


def profile_proxy(profile: str | None = None) -> str | None:
    """OKX's own `proxy_url` field on the chosen profile.

    This is the convention the OKX CLI/agent docs define, so honouring it means
    a user who already configured access for the official tooling does not have
    to configure it a second time here.
    """
    path = default_config_path()
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        return None
    name = profile or raw.get("default_profile")
    body = (raw.get("profiles") or {}).get(name) or {}
    value = body.get("proxy_url")
    return str(value).strip() if value else None


def _probe(proxy: str | None, timeout: float = 6.0) -> tuple[bool, str | None]:
    """Can we actually reach OKX's clock through this route?"""
    try:
        _get_json("/public/time", proxy=proxy, timeout=timeout)
        return True, None
    except Exception as exc:
        return False, str(exc)


def detect_proxy(profile: str | None = None) -> dict:
    """Try every plausible route in order; return the first that reaches OKX.

    Order matters: an explicit per-profile setting beats the OS setting, which
    beats a bare direct attempt, which beats guessing local ports.
    """
    candidates: list[tuple[str, str | None]] = []
    explicit = profile_proxy(profile)
    if explicit:
        candidates.append((f"config.toml 的 proxy_url（profile {profile or '默认'}）", explicit))
    system = system_proxy()
    if system:
        candidates.append(("Windows 系统代理", system))
    env = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if env:
        candidates.append(("环境变量 https_proxy", env))
    candidates.append(("直连（不用代理）", None))
    for url in COMMON_PROXIES:
        candidates.append((f"本地常见端口 {url}", url))

    tried: list[dict] = []
    for label, value in candidates:
        ok, error = _probe(value)
        tried.append({"label": label, "proxy": value, "ok": ok,
                      "error": None if ok else (error or "")[:160]})
        if ok:
            return {"found": value, "via": label, "tried": tried}
    return {"found": None, "via": None, "tried": tried}


# --------------------------------------------------------------------------
# OKX public data
# --------------------------------------------------------------------------
def _get_json(path: str, params: dict | None = None, proxy: str | None = None,
              timeout: float = 15.0):
    """One public OKX GET, shared with `instruments.py` via `http.get_json`.

    The proxy and the rate limit live in `http` rather than here so that the
    console and the backtester draw on the same per-IP budget — two callers
    each sleeping politely still add up to a throttle.

    The error is re-raised with the caller's wording, but only CONNECTION
    failures get the "check your proxy" hint. Reporting a 404 (wrong symbol) or
    a 429 (rate limited) as a network problem sends people off to debug a VPN
    that was never broken.
    """
    try:
        return http.get_json(path, params, proxy=proxy, timeout=timeout)
    except OkxError as exc:
        if exc.kind == "network":
            raise ConsoleError(f"{NETWORK_HINT}（{exc}）") from exc
        raise ConsoleError(str(exc)) from exc


def ticker(symbol: str, proxy: str | None = None) -> dict:
    """Latest price and 24h stats. Public, no API key needed."""
    data = _get_json("/market/ticker", {"instId": _normalise_symbol(symbol)}, proxy)
    if not data:
        raise ConsoleError(f"{symbol} 没有行情数据")
    row = data[0]
    last = float(row.get("last") or 0.0)
    open24 = float(row.get("open24h") or 0.0)
    change = (last - open24) / open24 * 100.0 if open24 > 0 else 0.0
    return {
        "symbol": _normalise_symbol(symbol),
        "last": last,
        "open24h": open24,
        "high24h": float(row.get("high24h") or 0.0),
        "low24h": float(row.get("low24h") or 0.0),
        "vol24h": float(row.get("vol24h") or 0.0),
        "change_pct": change,
        "ts": int(row.get("ts") or 0),
    }


def instruments(proxy: str | None = None, quote: str = "USDT",
                limit: int = 250, extra: list[str] | None = None) -> list[dict]:
    """Live `quote`-margined swaps with their contract specs, ranked by turnover.

    A bare alphabetical list of instIds is not a usable picker: most of them
    barely trade, and nothing tells you the contract face value or the leverage
    ceiling before you commit money. Ranking by turnover is what the official
    CLI does with `okx market filter --sortBy volUsd24h`.

    Two API facts shape this function:

      * `/public/instruments` caps its response at 500 rows. There are more
        swaps than that, so a symbol you already configured can be absent from
        the list — MU-USDT-SWAP is. Those are fetched by name and merged in.
      * SWAP tickers carry `vol24h` (contracts) and `volCcy24h` (base coin),
        but NOT a quote-currency turnover. USDT turnover is
        `volCcy24h x last`. Ranking on the raw `volCcy24h` would put SATS and
        PEPE, where a unit is worth a rounding error, above BTC.

    Turnover is a nicety, not a gate — if the tickers call fails we still return
    the full list, just unsorted.
    """
    if extra is None:
        try:
            configured = load_config(CONFIG_PATH)
            extra = [configured.symbol, *configured.trading.symbols]
        except Exception:
            extra = []
    # Normalise once: "MU" and "MU-USDT-SWAP" must not become two entries.
    wanted: list[str] = []
    for symbol in extra:
        try:
            name = _normalise_symbol(symbol) if symbol else ""
        except ConsoleError:
            continue
        if name and name not in wanted:
            wanted.append(name)

    specs = {spec.inst_id: spec for spec in instruments_module.fetch_all(proxy=proxy)}
    for name in wanted:
        if name not in specs:
            try:
                found = instruments_module.fetch(name, proxy=proxy)
            except OkxError:
                found = None
            if found:
                specs[name] = found

    turnover: dict[str, float] = {}

    def _turnover(row: dict) -> None:
        inst_id = row.get("instId")
        if not inst_id:
            return
        base_volume = float(row.get("volCcy24h") or 0.0)
        last = float(row.get("last") or 0.0)
        turnover[inst_id] = base_volume * last

    try:
        for row in _get_json("/market/tickers", {"instType": "SWAP"}, proxy, timeout=20.0):
            _turnover(row)
        # Only the configured symbols get an individual lookup. Backfilling all
        # 500 would be 500 rate-limited calls (~50s at 20 req/2s) to rank
        # instruments nobody is going to pick.
        for name in wanted:
            if name not in turnover:  # beyond the 500-row cap
                for row in _get_json("/market/ticker", {"instId": name}, proxy):
                    _turnover(row)
    except ConsoleError:
        pass

    suffix = f"-{quote}-SWAP"
    rows = [{
        "inst_id": spec.inst_id,
        "category": spec.category,
        "contract_value": spec.contract_value,
        "contract_ccy": spec.contract_ccy,
        "lot_size": spec.lot_size,
        "min_size": spec.min_size,
        "max_leverage": spec.max_leverage,
        "state": spec.state,
        "turnover_24h": turnover.get(spec.inst_id, 0.0),
    } for inst_id, spec in specs.items()
        if inst_id.endswith(suffix) and spec.is_live]
    rows.sort(key=lambda row: -row["turnover_24h"])
    return rows[:limit]


def spec(symbol: str, proxy: str | None = None) -> dict:
    """Contract specs for one symbol, for the trade panel.

    Refuses instruments that are not `live` — placing an order into a suspended
    market fails in a way that looks like a bug in our code.
    """
    try:
        found = instruments_module.require(_normalise_symbol(symbol), proxy=proxy)
    except (instruments_module.SpecError, OkxError) as exc:
        raise ConsoleError(str(exc)) from exc
    return {
        "inst_id": found.inst_id,
        "category": found.category,
        "contract_value": found.contract_value,
        "contract_ccy": found.contract_ccy,
        "lot_size": found.lot_size,
        "min_size": found.min_size,
        "tick_size": found.tick_size,
        "max_leverage": found.max_leverage,
        "max_market_size": found.max_market_size,
        "state": found.state,
        "describe": instruments_module.describe(found),
        "is_stock_token": found.is_stock_token,
    }


# --------------------------------------------------------------------------
# Signal
# --------------------------------------------------------------------------
def summarise(ind_5m: dict, ind_15m: dict, config: Config) -> dict:
    """Turn two indicator snapshots into what the UI shows. Pure: no network.

    The decision itself is still `classify`. This only formats, and derives the
    two display numbers the gates use (deviation, 5m gap) so the UI can show
    *why* a gate passed or failed without re-implementing anything.
    """
    signal, reason = classify(ind_5m, ind_15m, config.adx_min, config.deviation_max)

    fast_15m = float(ind_15m.get("ema_fast") or 0.0)
    close_15m = float(ind_15m.get("close") or 0.0)
    deviation = abs(close_15m - fast_15m) / fast_15m if fast_15m > 0 else None
    env = ("long" if fast_15m > float(ind_15m.get("ema_slow") or 0.0)
           else "short" if fast_15m < float(ind_15m.get("ema_slow") or 0.0) else "flat")

    fast_5m = float(ind_5m.get("ema_fast") or 0.0)
    slow_5m = float(ind_5m.get("ema_slow") or 0.0)
    prev_fast = ind_5m.get("prev_ema_fast")
    prev_slow = ind_5m.get("prev_ema_slow")
    crossed = None
    if prev_fast is not None and prev_slow is not None:
        if prev_fast <= prev_slow and fast_5m > slow_5m:
            crossed = "golden"
        elif prev_fast >= prev_slow and fast_5m < slow_5m:
            crossed = "death"

    return {
        "signal": signal.side if signal else None,
        "reason": reason,
        "reason_text": _reason_text(reason),
        "m15": {
            "ts": int(ind_15m.get("ts") or 0),
            "close": close_15m,
            "ema_fast": fast_15m,
            "ema_slow": float(ind_15m.get("ema_slow") or 0.0),
            "adx": float(ind_15m.get("adx") or 0.0),
            "deviation": deviation,
            "env": env,
        },
        "m5": {
            "ts": int(ind_5m.get("ts") or 0),
            "close": float(ind_5m.get("close") or 0.0),
            "ema_fast": fast_5m,
            "ema_slow": slow_5m,
            "gap": fast_5m - slow_5m,
            "crossed": crossed,
        },
        "gates": {
            "adx_min": config.adx_min,
            "adx_ok": float(ind_15m.get("adx") or 0.0) > config.adx_min,
            "deviation_max": config.deviation_max,
            "deviation_ok": deviation is not None and deviation <= config.deviation_max,
            "env_ok": env != "flat",
            "cross_ok": crossed is not None,
        },
    }


def _reason_text(reason: str) -> str:
    return {
        "warmup": "指标还在预热，K 线不够",
        "adx_too_low": "15m ADX 不够强，趋势不成立",
        "deviation_too_large": "15m 价格离 EMA20 太远，不追",
        "no_trend_env": "15m EMA20 与 EMA60 粘合，没有方向",
        "no_cross_event": "5m 本根没有发生交叉（这是常态，不是故障）",
        "long": "15m 多头 + 5m 金叉",
        "short": "15m 空头 + 5m 死叉",
    }.get(reason, reason)


def current_signal(symbol: str, config: Config, proxy: str | None = None) -> dict:
    """Evaluate the strategy right now, on closed candles only."""
    symbol = _normalise_symbol(symbol)
    closed_5m = _closed(fetch_candles(symbol, config.bar_5m, config.candle_limit, proxy))
    closed_15m = _closed(fetch_candles(symbol, config.bar_15m, config.candle_limit, proxy))
    ind_5m = calculate_indicators(closed_5m, config.ema_fast, config.ema_slow, config.adx_period)
    ind_15m = calculate_indicators(closed_15m, config.ema_fast, config.ema_slow, config.adx_period)
    if not ind_5m or not ind_15m:
        raise ConsoleError(f"{symbol} 指标还没预热完（K 线不足），请稍后再试")
    payload = summarise(ind_5m, ind_15m, config)
    payload["symbol"] = symbol
    payload["bars"] = {"m5": len(closed_5m), "m15": len(closed_15m)}
    return payload


def _closed(candles: list) -> list:
    """Drop the still-forming candle, exactly as the executor does."""
    return candles[:-1] if len(candles) > 1 else candles


# --------------------------------------------------------------------------
# Account / trading (demo only)
# --------------------------------------------------------------------------
def make_broker(profile: str | None, proxy: str | None, dry_run: bool = False) -> Broker:
    try:
        credentials = load_demo_credentials(profile, default_config_path())
    except CredentialsError as exc:
        raise ConsoleError(f"凭证不可用：{exc}") from exc
    return Broker(credentials, proxy=proxy, dry_run=dry_run)


def profiles() -> list[dict]:
    """Profiles in ~/.okx/config.toml, flagged demo or not."""
    path = default_config_path()
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        return []
    default = raw.get("default_profile")
    out = []
    for name, body in sorted((raw.get("profiles") or {}).items()):
        out.append({"name": name, "demo": bool(body.get("demo") is True),
                    "is_default": name == default})
    return out


def account(profile: str | None, proxy: str | None) -> dict:
    broker = make_broker(profile, proxy)
    try:
        equity = broker.verify_demo()
    except BrokerError as exc:
        raise ConsoleError(f"连接失败：{exc}") from exc
    config = load_config(CONFIG_PATH)
    positions = []
    for symbol in config.trading.symbols:
        try:
            raw = broker.get_position(symbol)
        except BrokerError as exc:
            positions.append({"symbol": symbol, "error": str(exc)})
            continue
        if raw:
            positions.append(_position_view(symbol, raw, broker))
    return {"profile": profile, "demo": True, "equity_usdt": equity, "positions": positions,
            "ledger": _ledger_view()}


def _position_view(symbol: str, raw: dict, broker: Broker) -> dict:
    contracts = float(raw.get("contracts") or 0.0)
    entry = float(raw.get("entryPrice") or 0.0)
    mark = float(raw.get("markPrice") or 0.0)
    side = (raw.get("side") or "").lower()
    sign = 1.0 if side == "long" else -1.0
    pnl_ratio = (mark - entry) / entry * sign if entry > 0 else 0.0
    return {
        "symbol": symbol,
        "side": side,
        "contracts": contracts,
        "entry_price": entry,
        "mark_price": mark,
        "pnl_ratio": pnl_ratio,
        "unrealised": float(raw.get("unrealizedPnl") or 0.0),
        "leverage": raw.get("leverage"),
        "liquidation": float(raw.get("liquidationPrice") or 0.0),
        "notional": broker.notional_for_contracts(symbol, contracts, mark) if mark > 0 else 0.0,
    }


def _ledger_view() -> list[dict]:
    """What `state.yaml` thinks we hold. A restart guard, not a second truth."""
    from .state import Ledger
    if not LEDGER_PATH.exists():
        return []
    try:
        ledger = Ledger.load(LEDGER_PATH)
    except Exception:
        return []
    return [{"symbol": symbol, "side": pos.side, "entry_price": pos.entry_price,
             "stop_price": pos.stop_price}
            for symbol, pos in ledger.positions.items() if pos.is_open]


def place_order(symbol: str, side: str, notional: float, leverage: int, stop_pct: float,
                profile: str | None, proxy: str | None, dry_run: bool = False) -> dict:
    """Open a demo position with its stop attached in the same request."""
    symbol = _normalise_symbol(symbol)
    if side not in ("long", "short"):
        raise ConsoleError("方向只能是 long 或 short")
    if notional <= 0:
        raise ConsoleError("名义金额必须大于 0")
    broker = make_broker(profile, proxy, dry_run=dry_run)
    try:
        broker.verify_demo()
        broker.set_leverage(symbol, leverage)
        fill = broker.market_entry(symbol, side, notional, leverage, stop_pct)
    except BrokerError as exc:
        raise ConsoleError(_broker_hint(str(exc))) from exc
    return {"ok": True, "dry_run": dry_run, "symbol": symbol, "side": fill.side,
            "price": fill.price, "size": fill.size, "notional": fill.notional,
            "stop_price": fill.stop_price, "order_id": fill.order_id}


def close_position(symbol: str, profile: str | None, proxy: str | None,
                   dry_run: bool = False) -> dict:
    symbol = _normalise_symbol(symbol)
    broker = make_broker(profile, proxy, dry_run=dry_run)
    try:
        broker.verify_demo()
        closed = broker.close_position(symbol)
        broker.cancel_stops(symbol)
    except BrokerError as exc:
        raise ConsoleError(_broker_hint(str(exc))) from exc
    return {"ok": True, "dry_run": dry_run, "symbol": symbol, "closed": bool(closed)}


def _broker_hint(message: str) -> str:
    """A ccxt failure is almost always the proxy; say so instead of the raw URL."""
    lowered = message.lower()
    if any(token in lowered for token in ("timeout", "timed out", "connection", "10061",
                                          "proxy", "dns", "getaddrinfo", "refused")):
        return f"{message}\n\n{NETWORK_HINT}"
    return message


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------
def run_backtest(symbol: str, days: int, fee_bps: float, slippage_bps: float,
                 proxy: str | None = None, show: int = 20) -> dict:
    """Replay the live strategy on confirmed candles; same function, same gates."""
    symbol = _normalise_symbol(symbol)
    config = load_config(CONFIG_PATH)
    bars_per_day = 24 * 60 // backtest_module.MINUTES_PER_BAR[config.bar_5m]
    bars_needed = max(days * bars_per_day, config.candle_limit)

    try:
        candles_5m = backtest_module.fetch_history(symbol, config.bar_5m, bars_needed, proxy=proxy)
        candles_15m = backtest_module.fetch_history(
            symbol, config.bar_15m, bars_needed // 3 + 400, proxy=proxy)
    except Exception as exc:
        raise ConsoleError(f"{NETWORK_HINT}（{type(exc).__name__}: {exc}）") from exc
    if len(candles_5m) < max(config.ema_slow, config.adx_period * 2):
        raise ConsoleError(f"{symbol} 历史不足（只取到 {len(candles_5m)} 根 5m K 线），换个币种或减少天数")

    frame_5m = indicator_frame(candles_5m, config.ema_fast, config.ema_slow, config.adx_period)
    frame_15m = indicator_frame(candles_15m, config.ema_fast, config.ema_slow, config.adx_period)
    lookahead_ms = backtest_module.MINUTES_PER_BAR[config.bar_5m] * 60_000

    report = simulate(frame_5m, frame_15m, config.adx_min, config.deviation_max,
                      fee_bps + slippage_bps, lookahead_ms, config.trading.stop_loss_pct)

    wins = [t for t in report.trades if t.gross_return > 0]
    trading = config.trading
    exposure = (trading.equity_pct / 100.0 * trading.leverage
                if trading.sizing_mode == "equity" else None)
    rows = [{
        "side": t.side,
        "entry_signal_time": fmt_ts(t.entry_signal_ts),
        "entry_execution_time": fmt_ts(t.entry_exec_ts),
        "entry_price": t.entry_price,
        "exit_signal_time": fmt_ts(t.exit_signal_ts),
        "exit_execution_time": fmt_ts(t.exit_exec_ts),
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "gross_return": t.gross_return,
    } for t in report.trades]

    return {
        "symbol": symbol,
        "days": days,
        "bars": {"m5": len(candles_5m), "m15": len(candles_15m)},
        "summary": {
            "trades": len(report.trades),
            "win_rate": (len(wins) / len(report.trades)) if report.trades else 0.0,
            "gross_return": report.gross_return,
            "net_return": report.net_return,
            "turnover": report.total_turnover,
            "cost_bps": fee_bps + slippage_bps,
            "stopped_out": report.stopped_out,
            "unfilled_signals": report.unfilled_signals,
            "expectancy_bps": (report.net_return / len(report.trades) * 10_000)
                              if report.trades else 0.0,
            # Two drawdowns on purpose. `notional` is the signal's own swing
            # (1 unit of exposure against 1 unit of capital); `account` applies
            # the configured equity% and leverage and is capped at 100%, because
            # that is the number that decides whether the account survives.
            "max_drawdown": report.max_drawdown(1.0),
            "max_drawdown_account": (report.max_drawdown(exposure)
                                     if exposure else None),
            "account_exposure": exposure,
            "wiped_out": bool(report.wiped_out(exposure)) if exposure else False,
            # `inf` when the run never went below zero, `nan` if any candle was
            # bad. Neither is valid JSON, so the API never emits them.
            "ruin_leverage": _finite(report.ruin_exposure()),
            "breakeven_bps": report.breakeven_bps,
        },
        "trades": rows if show <= 0 else rows[-show:],
        "trades_total": len(rows),
    }


def default_symbol() -> str:
    """The instrument the console starts on: config symbol, else first tradable."""
    config = load_config(CONFIG_PATH)
    return config.symbol or (config.trading.symbols[0] if config.trading.symbols else "")


def overview() -> dict:
    """Everything the UI needs on first paint, without touching the network
    except for the symbol list (which degrades to the configured ones)."""
    config = load_config(CONFIG_PATH)
    settings = load_settings()
    return {
        "config": {
            "symbol": config.symbol,
            "bar_5m": config.bar_5m,
            "bar_15m": config.bar_15m,
            "adx_min": config.adx_min,
            "deviation_max": config.deviation_max,
            "ema_fast": config.ema_fast,
            "ema_slow": config.ema_slow,
            "adx_period": config.adx_period,
            "trading": {
                "enabled": config.trading.enabled,
                "symbols": list(config.trading.symbols),
                "sizing_mode": config.trading.sizing_mode,
                "notional_usdt": config.trading.notional_usdt,
                "equity_pct": config.trading.equity_pct,
                "leverage": config.trading.leverage,
                "stop_loss_pct": config.trading.stop_loss_pct,
                "max_total_notional": config.trading.max_total_notional,
            },
        },
        "settings": settings,
        "profiles": profiles(),
        "demo_only": True,
    }
