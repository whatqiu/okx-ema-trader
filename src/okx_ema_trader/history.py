from __future__ import annotations

from . import http

MAX_LIMIT = 300


def fetch_candles(symbol: str, bar: str, limit: int,
                  proxy: str | None = None) -> list[list[float]]:
    """Return up to `limit` closed/forming candles, oldest first.

    `proxy` defaults to the machine's system proxy. It used to be impossible to
    pass one at all, which meant the live monitor and the executor — everything
    that is not the web console — could never reach OKX from a network where the
    direct route is filtered.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    if proxy is None:
        proxy = http.system_proxy()
    data = http.get_json("/market/candles",
                         {"instId": symbol, "bar": bar, "limit": limit},
                         proxy=proxy, timeout=15)
    # REST returns newest first; [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    candles = [[float(item[index]) for index in range(6)] for item in reversed(data)]
    return candles[-limit:]
