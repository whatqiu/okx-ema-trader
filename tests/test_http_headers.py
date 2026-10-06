"""Regression test: OKX's WAF rejects a bot-shaped User-Agent (error 1010).

Symptom this prevents: every outbound call returns HTTP 403 with body
`error code: 1010` while the browser and curl reach the same URL fine. The
cause is the old header `"Mozilla/5.0 (compatible; okx-ema-trader)"` — a
browser prefix wrapped around an obviously programmatic tail, which is the
exact signature Cloudflare error 1010 exists to catch.

These checks are offline: they pin the SHAPE of the headers, not OKX's
current mood. The live verification was done by hand (403 -> 200 with a real
Chrome UA through the same proxy).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from okx_ema_trader import http  # noqa: E402

_RESULTS = []


def check(name, condition):
    _RESULTS.append((name, bool(condition)))


def test_ua_looks_like_a_real_browser():
    ua = http.UA["User-Agent"]
    check("UA starts with the real Chrome prefix",
          ua.startswith("Mozilla/5.0 (Windows NT 10.0; Win64; x64)"))
    check("UA contains a Chrome version token", bool(re.search(r"Chrome/\d+", ua)))
    check("UA contains a Safari token", "Safari/" in ua)
    check("UA does not leak the project name", "okx-ema-trader" not in ua)
    # The specific shape Cloudflare 1010 dislikes: browser prefix + bot tail.
    check("no '(compatible; <project>)' bot tail",
          not re.search(r"\(compatible;\s*\w", ua))


def test_browser_shaped_headers_present():
    for header in ("Accept", "Accept-Language", "Accept-Encoding",
                   "Sec-Fetch-Mode"):
        check(f"header {header} present", header in http.UA)
    check("Accept accepts JSON and wildcard",
          "application/json" in http.UA["Accept"] and "*/*" in http.UA["Accept"])


def test_accept_encoding_is_identity():
    """`Accept-Encoding: identity` is deliberate.

    urllib does not transparently decode gzip, and a WAF is far more willing
    to inspect a bodyless request than a compressed one. Asking for identity
    keeps responses readable instead of failing at the JSON parse step.
    """
    check("Accept-Encoding is identity (urllib cannot gunzip)",
          http.UA["Accept-Encoding"] == "identity")


def test_api_base_unchanged():
    check("API base still points at OKX v5",
          http.API == "https://www.okx.com/api/v5")


def main():
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    failed = 0
    for name, ok in _RESULTS:
        print(f"  {'ok' if ok else 'FAIL'}  {name}")
        failed += 0 if ok else 1
    print("")
    if failed:
        print(f"FAILED: {failed} check(s)")
    else:
        print("all groups passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
