"""Symbol parsing rules.

Kept as its own file because these rules are load-bearing for the WATCH LIST:
a symbol that silently normalises to something else would make the auto-trader
trade an instrument the user never picked, and one that fails to normalise would
drop it from the scan with no visible error.

Migrated here from the old `test_console.py` when the single-page console was
removed — the parsing itself outlived the UI it was written for.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from okx_ema_trader.symbols import SymbolError, normalise_symbol


def test_symbol_normalisation() -> int:
    """`MU` / `MUUSDT` / `mu-usdt-swap` must all land on the same instId."""
    failures = 0
    for raw, expected in [("MU", "MU-USDT-SWAP"), ("mu", "MU-USDT-SWAP"),
                          ("MUUSDT", "MU-USDT-SWAP"), ("MU-USDT-SWAP", "MU-USDT-SWAP"),
                          (" mu-usdt-swap ", "MU-USDT-SWAP"),
                          ("BTC-USDT-SWAP", "BTC-USDT-SWAP")]:
        got = normalise_symbol(raw)
        if got != expected:
            print(f"  FAIL {raw!r} -> {got!r}, expected {expected!r}")
            failures += 1
    print(f"  accepted forms: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} accepted-form mismatch(es)"
    return failures


def test_rejects_anything_that_is_not_a_usdt_swap() -> int:
    """Reject, never guess: a wrong instId is a position in the wrong market."""
    failures = 0
    for bad in ("", "   ", "策略@@", "BTC-USD-SWAP", "ETHUSDT-SPOT"):
        try:
            normalise_symbol(bad)
            print(f"  FAIL {bad!r} should have been rejected")
            failures += 1
        except SymbolError:
            pass
    print(f"  rejected forms: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, f"{failures} bad symbol(s) were accepted"
    return failures


def test_symbol_error_is_a_value_error() -> int:
    """The trading path catches `(ValueError, KeyError)`; a symbol error has to
    be catchable by that handler or one bad instrument kills the whole scan."""
    failures = 0
    if not issubclass(SymbolError, ValueError):
        print("  FAIL SymbolError must subclass ValueError")
        failures += 1
    try:
        raise SymbolError("boom")
    except ValueError:
        pass  # caught by the same handler as a bad notional
    except Exception as exc:
        print(f"  FAIL not catchable as ValueError: {exc!r}")
        failures += 1
    print(f"  catchable as ValueError: {'ok' if not failures else 'FAILED'}")
    assert failures == 0, "SymbolError must be catchable as ValueError"
    return failures


def main() -> int:
    print("symbols")
    total = 0
    for test in (test_symbol_normalisation,
                 test_rejects_anything_that_is_not_a_usdt_swap,
                 test_symbol_error_is_a_value_error):
        total += test()
    print("all groups passed" if not total else f"{total} failure(s)")
    return total


if __name__ == "__main__":
    raise SystemExit(main())
