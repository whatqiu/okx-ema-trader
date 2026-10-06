"""Instrument-id normalisation — `MU` / `MUUSDT` / `mu-usdt-swap` -> `MU-USDT-SWAP`.

Lives on its own because TWO callers need it and neither owns it: the platform
backend (`deps.normalise_symbol`, used by the watch list and every endpoint that
takes a symbol) and the tests that pin the parsing rules. It used to sit inside
the old single-page console, which meant deleting that console would have taken
the platform down with it.

`SymbolError` subclasses `ValueError` deliberately: `autotrader.set_state`
validates notional/leverage with plain `ValueError`, and callers such as
`maybe_trade` catch `(ValueError, KeyError)` around the trading path. A symbol
error has to be catchable by the same handler or one bad instrument aborts the
scan instead of being recorded and skipped.
"""
from __future__ import annotations

import re


class SymbolError(ValueError):
    """A symbol the user can fix: empty, or not a USDT-margined swap."""


def normalise_symbol(symbol: str) -> str:
    """Accept `MU`, `MUUSDT`, `mu-usdt-swap` and give back `MU-USDT-SWAP`.

    Anything that is not a USDT-margined swap is rejected: the broker slices on
    "-" to build the ccxt market, and the strategy's sizing assumes USDT quote.
    """
    raw = (symbol or "").strip().upper()
    if not raw:
        raise SymbolError("币种不能为空")
    if raw.endswith("-USDT-SWAP"):
        return raw
    if raw.endswith("USDT"):
        return f"{raw[:-len('USDT')]}-USDT-SWAP"
    if re.fullmatch(r"[A-Z0-9]{1,20}", raw):
        return f"{raw}-USDT-SWAP"
    raise SymbolError(f"无法识别的币种格式：{symbol!r}（示例：MU-USDT-SWAP 或 MU）")
