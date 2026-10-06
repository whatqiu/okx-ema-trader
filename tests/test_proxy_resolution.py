"""Proxy resolution in deps.proxy().

Pinned because of a real incident (2026-10-06): the machine has TWO proxy
clients on different ports, and proxy() used to resolve once per process and
cache the answer. When the cached client was not the one running, every OKX
call died with WinError 10061 and — before logging existed — nothing on screen
said which address had been tried. The user restarted the platform over and
over; only editing config/console.json helped.

Two semantics must hold:
  * an explicit setting (config/console.json "proxy" key) is authoritative and
    cached — "" deliberately means direct;
  * WITHOUT an explicit setting the answer must track the Windows system proxy
    LIVE, because the running client rewrites the registry when it starts and
    the user switches clients.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).parents[1] / "platform" / "backend"
for p in (str(BACKEND), str(BACKEND.parents[1] / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import deps  # noqa: E402


def test_no_explicit_setting_follows_the_system_proxy_live(monkeypatch):
    """No "proxy" key -> whatever the registry says *at call time*, not boot time."""
    monkeypatch.setattr(deps, "_proxy_override", False)
    monkeypatch.setattr(deps, "load_settings", lambda: {})
    monkeypatch.setattr(deps.okx_http, "system_proxy", lambda: "http://127.0.0.1:10888")
    assert deps.proxy() == "http://127.0.0.1:10888"

    # The user switches proxy clients: the registry now points elsewhere. A
    # cached implementation keeps answering with the dead port; this one must
    # follow within the same process, no restart needed.
    monkeypatch.setattr(deps.okx_http, "system_proxy", lambda: "http://127.0.0.1:6088")
    assert deps.proxy() == "http://127.0.0.1:6088"


def test_explicit_setting_is_authoritative_and_cached(monkeypatch):
    """A "proxy" key overrides the registry, and stays put if the registry moves."""
    monkeypatch.setattr(deps, "_proxy_override", False)
    monkeypatch.setattr(deps, "load_settings", lambda: {"proxy": "http://127.0.0.1:6088"})
    monkeypatch.setattr(deps.okx_http, "system_proxy", lambda: "http://127.0.0.1:10888")

    assert deps.proxy() == "http://127.0.0.1:6088"
    # Now cached: even a registry change must not silently override what the
    # user explicitly asked for.
    monkeypatch.setattr(deps.okx_http, "system_proxy", lambda: "http://127.0.0.1:9999")
    assert deps.proxy() == "http://127.0.0.1:6088"


def test_explicit_empty_means_direct_not_system(monkeypatch):
    '"" in the settings file is a deliberate "go direct", not "unset".'
    monkeypatch.setattr(deps, "_proxy_override", False)
    monkeypatch.setattr(deps, "load_settings", lambda: {"proxy": ""})
    monkeypatch.setattr(deps.okx_http, "system_proxy", lambda: "http://127.0.0.1:10888")

    assert deps.proxy() is None


def test_save_settings_invalidates_the_cached_value(monkeypatch, tmp_path):
    """Changing the setting through the API must take effect without a restart."""
    monkeypatch.setattr(deps, "_proxy_override", False)
    monkeypatch.setattr(deps, "CONSOLE_STATE_PATH", tmp_path / "console.json")
    monkeypatch.setattr(deps.okx_http, "system_proxy", lambda: "http://127.0.0.1:10888")

    deps.save_settings({"proxy": "http://127.0.0.1:6088"})
    assert deps.proxy() == "http://127.0.0.1:6088"

    deps.save_settings({"proxy": ""})
    assert deps.proxy() is None
