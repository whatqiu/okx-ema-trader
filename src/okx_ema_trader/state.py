"""Position ledger on disk, so a restart cannot open a duplicate position.

The live bots keep `last_side` in memory only (see `main.Trader` and
`monitor.Monitor`), which is harmless for an observer but dangerous for an
executor: restarting while long would re-fire the same signal as if it were
new. This module is the durable record that closes that gap.

Two rules the caller depends on:
  * writes are atomic (`tmp` + `os.replace`), so a crash mid-write cannot leave
    a truncated file;
  * a file that exists but cannot be understood raises instead of being treated
    as empty — "I don't know what I'm holding" must never look like "I hold
    nothing", because that is precisely the state that opens a second position.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

SCHEMA_VERSION = 1


class StateError(RuntimeError):
    """Raised when the ledger exists but cannot be trusted."""


@dataclass
class Position:
    """A position we believe we hold in one instrument."""

    side: str  # "long" | "short" | "flat"
    entry_ts: int = 0
    entry_price: float = 0.0
    size: float = 0.0
    entry_order_id: str = ""
    stop_order_id: str = ""
    stop_price: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.side in ("long", "short")

    @classmethod
    def flat(cls) -> "Position":
        return cls(side="flat")

    @classmethod
    def from_dict(cls, raw: dict) -> "Position":
        if not isinstance(raw, dict):
            raise StateError(f"position entry is {type(raw).__name__}, expected mapping")
        side = raw.get("side")
        if side not in ("long", "short", "flat"):
            raise StateError(f"position has invalid side {side!r}")
        return cls(
            side=side,
            entry_ts=int(raw.get("entry_ts", 0) or 0),
            entry_price=float(raw.get("entry_price", 0.0) or 0.0),
            size=float(raw.get("size", 0.0) or 0.0),
            entry_order_id=str(raw.get("entry_order_id", "") or ""),
            stop_order_id=str(raw.get("stop_order_id", "") or ""),
            stop_price=float(raw.get("stop_price", 0.0) or 0.0),
        )


@dataclass
class Ledger:
    """All tracked instruments, keyed by symbol."""

    positions: dict[str, Position] = field(default_factory=dict)
    path: Path | None = None

    def get(self, symbol: str) -> Position:
        """Ledger's view of a symbol; flat when never traded."""
        return self.positions.get(symbol) or Position.flat()

    def set(self, symbol: str, position: Position) -> None:
        self.positions[symbol] = position

    def clear(self, symbol: str) -> None:
        self.positions[symbol] = Position.flat()

    def open_symbols(self) -> list[str]:
        return sorted(sym for sym, pos in self.positions.items() if pos.is_open)

    def total_notional(self, prices: dict[str, float]) -> float:
        """Sum of |size * price| across open positions, for the exposure cap."""
        total = 0.0
        for symbol in self.open_symbols():
            position = self.positions[symbol]
            price = prices.get(symbol) or position.entry_price
            total += abs(position.size * price)
        return total

    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "positions": {sym: asdict(pos) for sym, pos in self.positions.items()},
        }

    def save(self) -> None:
        """Atomically replace the ledger file."""
        if self.path is None:
            raise StateError("ledger has no path; construct it with Ledger.load(path)")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(self.to_dict(), sort_keys=True, allow_unicode=True)

        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent,
            prefix=self.path.name + ".", suffix=".tmp", delete=False,
        )
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)  # atomic on Windows and POSIX
        except Exception:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        """Read the ledger. A missing file is a fresh start; a corrupt one is fatal."""
        if not path.exists():
            return cls(positions={}, path=path)

        text = path.read_text(encoding="utf-8")
        if not text.strip():
            # A zero-byte file is what a failed first write leaves behind. We
            # cannot tell "nothing held" from "record lost", so refuse.
            raise StateError(f"{path} is empty; delete it only if you are certain nothing is held")

        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise StateError(f"{path} is not valid YAML: {exc}") from exc

        if not isinstance(raw, dict):
            raise StateError(f"{path} should contain a mapping, got {type(raw).__name__}")

        version = raw.get("version")
        if version != SCHEMA_VERSION:
            raise StateError(
                f"{path} has schema version {version!r}, expected {SCHEMA_VERSION}; migrate or remove it"
            )

        positions_raw = raw.get("positions") or {}
        if not isinstance(positions_raw, dict):
            raise StateError(f"{path} 'positions' should be a mapping, got {type(positions_raw).__name__}")

        positions = {str(sym): Position.from_dict(entry) for sym, entry in positions_raw.items()}
        return cls(positions=positions, path=path)
