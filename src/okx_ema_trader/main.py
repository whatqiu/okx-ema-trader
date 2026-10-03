from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import websockets

from .config import load_config
from .history import fetch_candles
from .indicators import calculate_indicators
from .strategy import Signal, evaluate_signal

# Candle channels live on the business endpoint; the public one rejects them with code 60018.
WS_URL = "wss://ws.okx.com:8443/ws/v5/business"
CONFIG_PATH = Path(__file__).parents[2] / "config" / "config.yaml"


class Trader:
    def __init__(self, config) -> None:
        self.config = config
        self.candles: dict[str, list[list[float]]] = {config.bar_5m: [], config.bar_15m: []}
        self.last_side: str | None = None

    def warm_up(self) -> None:
        """Seed each series from REST so indicators are valid immediately."""
        for bar in self.candles:
            try:
                series = fetch_candles(self.config.symbol, bar, self.config.candle_limit)
            except Exception as exc:
                logging.warning("history warm-up failed for %s: %s", bar, exc)
                continue
            self.candles[bar] = series
            logging.info("warmed up %s with %d candles", bar, len(series))

    def apply_candle(self, bar: str, candle: list[float]) -> None:
        series = self.candles[bar]
        if series and series[-1][0] == candle[0]:
            series[-1] = candle
        else:
            series.append(candle)
        del series[:-self.config.candle_limit]

    def evaluate(self, close: float) -> None:
        ind_5m = calculate_indicators(self.candles[self.config.bar_5m], self.config.ema_fast,
                                      self.config.ema_slow, self.config.adx_period)
        ind_15m = calculate_indicators(self.candles[self.config.bar_15m], self.config.ema_fast,
                                       self.config.ema_slow, self.config.adx_period)
        if ind_5m and ind_15m:
            logging.debug("state 5m adx=%.1f fast=%.1f slow=%.1f | 15m adx=%.1f fast=%.1f slow=%.1f",
                          ind_5m["adx"], ind_5m["ema_fast"], ind_5m["ema_slow"],
                          ind_15m["adx"], ind_15m["ema_fast"], ind_15m["ema_slow"])
        else:
            logging.debug("state pending: 5m=%d 15m=%d candles",
                          len(self.candles[self.config.bar_5m]), len(self.candles[self.config.bar_15m]))
        signal: Signal | None = evaluate_signal(ind_5m, ind_15m, self.config.adx_min,
                                                self.config.deviation_max)
        # Edge-triggered: one log line per direction change, not one per bar.
        if signal is None or signal.side == self.last_side:
            return
        self.last_side = signal.side
        logging.info("SIGNAL %s close=%.2f reason=%s", signal.side, close, signal.reason)


async def run() -> None:
    config = load_config(CONFIG_PATH)
    logging.basicConfig(level=getattr(logging, config.log_level), format="%(asctime)s %(levelname)s %(message)s")

    trader = Trader(config)
    args = [{"channel": "candle" + bar, "instId": config.symbol} for bar in (config.bar_5m, config.bar_15m)]

    while True:
        try:
            # Re-warm every connection: candles that closed while we were offline are gone otherwise.
            trader.warm_up()
            logging.info("connecting to %s", WS_URL)
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": args}))
                logging.info("subscribed: %s", args)
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("event") == "error":
                        # Not fatal on its own, but nothing will arrive on this socket.
                        logging.error("OKX error: %s", message)
                        break
                    if "data" not in message:
                        continue
                    bar = message["arg"]["channel"].removeprefix("candle")
                    if bar not in trader.candles:
                        continue
                    for item in message["data"]:
                        # OKX candle row: ts, open, high, low, close, volume, ..., confirm
                        if len(item) < 9 or item[-1] != "1":
                            continue
                        candle = [float(item[i]) for i in range(6)]
                        trader.apply_candle(bar, candle)
                        trader.evaluate(candle[4])
        except (OSError, websockets.WebSocketException, json.JSONDecodeError) as exc:
            logging.error("websocket stopped: %s", exc)
        except Exception:
            logging.exception("unexpected error; reconnecting")
        await asyncio.sleep(config.reconnect_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.info("stopped")
