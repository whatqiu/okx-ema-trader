"""Standalone signal monitor with Windows desktop alerts.

Polls OKX public candles for one instrument, evaluates the same EMA20/EMA50 +
ADX rule as the main strategy, and raises a Windows toast when the direction
changes. Read-only: public market data only, no API key, no orders, no account
access.

Run from the project root:

    set PYTHONPATH=src
    python -m okx_ema_trader.monitor --symbol AAVE-USDT-SWAP
"""
from __future__ import annotations

import argparse
import base64
import logging
import subprocess
import sys
import time
from pathlib import Path

from .config import load_config
from .history import fetch_candles
from .indicators import calculate_indicators
from .strategy import (REASON_ADX, REASON_DEVIATION, REASON_NO_CROSS,
                       REASON_NO_ENV, classify)

ROOT = Path(__file__).parents[2]
CONFIG_PATH = ROOT / "config" / "config.yaml"
LOG_PATH = ROOT / "logs" / "signals.log"

TOAST_APP = "OKX Signal Monitor"
BAR_SECONDS = 300  # wake on 5m boundaries; 15m boundaries are a subset of these


def _run_powershell(script: str) -> subprocess.CompletedProcess:
    """Run PowerShell via -EncodedCommand so non-ASCII text survives the code page."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _ps_literal(text: str) -> str:
    """Single-quoted PowerShell string literal."""
    return "'" + text.replace("'", "''") + "'"


def toast(title: str, body: str) -> None:
    script = f"""
try {{
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  $t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $x = $t.GetElementsByTagName('text')
  $x.Item(0).AppendChild($t.CreateTextNode({_ps_literal(title)})) | Out-Null
  $x.Item(1).AppendChild($t.CreateTextNode({_ps_literal(body)})) | Out-Null
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({_ps_literal(TOAST_APP)}).Show([Windows.UI.Notifications.ToastNotification]::new($t))
}} catch {{ }}
"""
    try:
        _run_powershell(script)
    except Exception as exc:
        logging.warning("toast failed: %s", exc)


def beep() -> None:
    try:
        import winsound

        for _ in range(2):
            winsound.Beep(880, 220)
    except Exception:
        pass


def seconds_until_next_bar() -> float:
    """Seconds to the next 5m boundary plus a buffer, so the closed candle is available."""
    return (BAR_SECONDS - time.time() % BAR_SECONDS) + 8


class Monitor:
    def __init__(self, config, symbol: str) -> None:
        self.config = config
        self.symbol = symbol
        self.last_side: str | None = None

    def closed(self, bar: str) -> list[list[float]]:
        """Candles excluding the still-forming one, so signals only fire on closes."""
        series = fetch_candles(self.symbol, bar, self.config.candle_limit)
        return series[:-1] if len(series) > 1 else series

    def _message(self, signal, i5: dict, i15: dict, first: bool,
                 reason: str = "") -> tuple[str, str]:
        if signal:
            label = "做多 LONG" if signal.side == "long" else "做空 SHORT"
            title = f"OKX {'初始状态' if first else '信号翻转'}：{label}"
            trend = "多头" if i15["ema_fast"] > i15["ema_slow"] else "空头"
            body = (
                f"{self.symbol}\n"
                f"价格 {i5['close']:.4f}\n"
                f"15m {trend} | ADX {i15['adx']:.1f}\n"
                f"{signal.reason}"
            )
            return title, body

        if reason == REASON_ADX:
            why = f"ADX {i15['adx']:.1f} 未超过阈值 {self.config.adx_min:.0f}（震荡）"
        elif reason == REASON_DEVIATION:
            why = (f"15m 价格偏离 EMA{self.config.ema_fast} 超过 "
                   f"{self.config.deviation_max:.2%}（不追）")
        elif reason == REASON_NO_CROSS:
            why = (f"5m 未发生 EMA{self.config.ema_fast}/EMA{self.config.ema_slow} 交叉"
                   "（趋势延续中，不是入场点）")
        elif reason == REASON_NO_ENV:
            why = (f"15m EMA{self.config.ema_fast} 与 EMA{self.config.ema_slow} 黏合，"
                   "无方向")
        else:
            why = "指标尚未就绪"
        title = "OKX 信号消失"
        body = f"{self.symbol}\n价格 {i5['close']:.4f}\n{why}\n建议观望/离场"
        return title, body

    def poll(self, toast_on: bool, sound_on: bool) -> None:
        # Fetch once per bar: each REST call costs seconds through the proxy.
        series_5m = self.closed(self.config.bar_5m)
        series_15m = self.closed(self.config.bar_15m)
        i5 = calculate_indicators(series_5m, self.config.ema_fast,
                                  self.config.ema_slow, self.config.adx_period)
        i15 = calculate_indicators(series_15m, self.config.ema_fast,
                                   self.config.ema_slow, self.config.adx_period)
        if not i5 or not i15:
            logging.info("waiting for warm-up (%d/%d and %d/%d candles)",
                         len(series_5m), self.config.candle_limit,
                         len(series_15m), self.config.candle_limit)
            return

        logging.info("5m close=%.4f ema20=%.4f ema60=%.4f adx=%.2f | 15m ema20=%.4f ema60=%.4f adx=%.2f",
                     i5["close"], i5["ema_fast"], i5["ema_slow"], i5["adx"],
                     i15["ema_fast"], i15["ema_slow"], i15["adx"])

        signal, reason = classify(i5, i15, self.config.adx_min, self.config.deviation_max)
        side = signal.side if signal else None
        if side == self.last_side:
            logging.debug("no change (side=%s)", side)
            return

        first = self.last_side is None
        self.last_side = side
        title, body = self._message(signal, i5, i15, first, reason)
        logging.info("ALERT | %s | %s", title.replace("\n", " "), body.replace("\n", " | "))
        if toast_on:
            toast(title, body)
        if sound_on:
            beep()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="AAVE-USDT-SWAP", help="instrument to watch")
    parser.add_argument("--interval", type=int, default=0,
                        help="poll every N seconds (0 = align to the 5m candle close)")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--no-toast", action="store_true", help="log only, no desktop popup")
    parser.add_argument("--no-sound", action="store_true", help="no beep")
    args = parser.parse_args()

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )

    config = load_config(CONFIG_PATH)
    monitor = Monitor(config, args.symbol)
    # Strictly greater than: adx == adx_min produces no signal.
    logging.info("watching %s | rule: 15m EMA%d/EMA%d with ADX>%.0f, entry on a 5m cross",
                 args.symbol, config.ema_fast, config.ema_slow, config.adx_min)
    logging.info("alerts: toast=%s sound=%s | log file: %s",
                 not args.no_toast, not args.no_sound, LOG_PATH)

    while True:
        try:
            monitor.poll(toast_on=not args.no_toast, sound_on=not args.no_sound)
        except Exception:
            logging.exception("poll failed; will retry")
        if args.once:
            return
        delay = args.interval or seconds_until_next_bar()
        logging.debug("next check in %ds", delay)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            logging.info("stopped")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("stopped")
