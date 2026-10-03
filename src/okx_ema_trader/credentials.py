"""Load OKX demo credentials from the existing CLI config file.

Reads `~/.okx/config.toml` — the same file the `okx` CLI and `okx-trade-mcp`
use — rather than duplicating secrets into this project's own config. Python
3.12 ships `tomllib`, so this adds no dependency.

There is exactly one job here: hand back a demo profile, or refuse. A profile
without `demo = true` is rejected outright so that a typo in a profile name can
never reach a live account.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".okx" / "config.toml"


class CredentialsError(RuntimeError):
    """Raised when usable demo credentials cannot be produced."""


@dataclass(frozen=True)
class Credentials:
    profile: str
    api_key: str
    secret_key: str
    passphrase: str

    def __repr__(self) -> str:  # keep secrets out of tracebacks and logs
        return f"Credentials(profile={self.profile!r}, api_key='***', secret_key='***', passphrase='***')"


# The placeholder text shipped in the template config. Treated as "not filled in".
_PLACEHOLDERS = {"your-live-api-key", "your-live-secret-key", "your-live-passphrase", ""}


def _read_config(path: Path) -> dict:
    if not path.exists():
        raise CredentialsError(
            f"OKX config not found at {path}. Create it with the `okx config` CLI, "
            f"or pass --config <path>."
        )
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise CredentialsError(f"{path} is not valid TOML: {exc}") from exc


def load_demo_credentials(profile: str | None = None, path: Path | None = None) -> Credentials:
    """Return credentials for a demo profile, or raise.

    `profile=None` uses the file's `default_profile`. The chosen profile MUST
    declare `demo = true`; anything else is refused rather than demoted, so a
    misconfigured name fails loudly instead of trading something real.
    """
    config_path = path or DEFAULT_CONFIG
    raw = _read_config(config_path)

    profiles = raw.get("profiles") or {}
    if not profiles:
        raise CredentialsError(f"{config_path} defines no [profiles.*] sections")

    name = profile or raw.get("default_profile")
    if not name:
        raise CredentialsError(f"{config_path} has no `default_profile` and none was given")
    if name not in profiles:
        raise CredentialsError(
            f"profile {name!r} not found in {config_path}; available: {sorted(profiles)}"
        )

    chosen = profiles[name]
    if chosen.get("demo") is not True:
        raise CredentialsError(
            f"profile {name!r} is not a demo profile (needs `demo = true`). "
            f"Refusing to trade a non-demo account."
        )

    api_key = str(chosen.get("api_key", "")).strip()
    secret_key = str(chosen.get("secret_key", "")).strip()
    passphrase = str(chosen.get("passphrase", "")).strip()
    missing = [
        field
        for field, value in (("api_key", api_key), ("secret_key", secret_key), ("passphrase", passphrase))
        if value in _PLACEHOLDERS
    ]
    if missing:
        raise CredentialsError(
            f"profile {name!r} has placeholder or empty {', '.join(missing)}; fill them in first"
        )

    return Credentials(profile=name, api_key=api_key, secret_key=secret_key, passphrase=passphrase)


def default_config_path() -> Path:
    """Allow tests and the CLI to override the location via env."""
    override = os.environ.get("OKX_CONFIG_TOML")
    return Path(override) if override else DEFAULT_CONFIG
