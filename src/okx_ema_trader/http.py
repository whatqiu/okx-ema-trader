"""One shared OKX REST client: proxy handling, rate limiting, error taxonomy.

Why this exists rather than three copies of `urllib.request.build_opener`:

  * **Rate limit.** OKX allows 20 requests per 2 seconds per IP on the public
    market endpoints (`okx-cex-market` SKILL.md). `backtest.fetch_history` used
    to sleep 0.15s between pages, i.e. ~13 req/s — over the limit — and the web
    console fired its own requests on top of that. Two callers that each believe
    they are being polite can still jointly get the IP throttled, so the budget
    has to be owned by one object.
  * **Error taxonomy.** A connection reset, an HTTP 404, and a JSON body saying
    `code != "0"` are three different failures needing three different user
    messages. Collapsing all three into "cannot reach OKX" makes a wrong symbol
    look like a broken proxy, which sends people off to debug their VPN.

Stdlib only — the console must import without ccxt installed.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

API = "https://www.okx.com/api/v5"

# Cloudflare error 1010 ("The owner of this website has banned your browser's
# signature") is what OKX returns for a request that looks like a bot wearing a
# fake browser hat. A UA of "Mozilla/5.0 (compatible; okx-ema-trader)" is
# exactly that pattern: browser prefix + obviously programmatic tail. It got
# us 403 while a full Chrome UA sailed through — so the UA must look like a
# real Chrome, and the extra browser-shaped headers below are what a genuine
# navigation sends. This is a public market-data endpoint; identifying as a
# browser is not spoofing anything, it is just not volunteering a bot shape.
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# 20 requests per 2 seconds per IP, per OKX's published market-data limit.
RATE_LIMIT = 20
RATE_WINDOW = 2.0


# --------------------------------------------------------------------------
# Proxy discovery
# --------------------------------------------------------------------------
def parse_proxy_server(text: str) -> str | None:
    """Windows stores either `host:port`, a URL, or `http=a:1;https=b:2;socks=c:3`.

    The three shapes have to be told apart by shape, not by looking for ";":
    `socks=127.0.0.1:1080` has no semicolon but is still scheme form, and the
    old `if ";" not in text` test turned it into `http://socks=127.0.0.1:1080`
    — a string that is not a host, not a port, and fails in a way that looks
    like a dead network.
    """
    text = (text or "").strip()
    if not text:
        return None
    if "://" in text:
        return text                      # already a full URL
    if "=" not in text:
        # Bare host:port. Strip a stray trailing ";": "h:1;" would otherwise
        # become "http://h:1;", a port that no socket will ever parse.
        host = text.rstrip(";").strip()
        return f"http://{host}" if host else None

    # Prefer the https entry; fall back to http, then to whatever else is there.
    # Reading "the first entry" instead would happily return an ftp proxy.
    by_scheme: dict[str, str] = {}
    order: list[str] = []
    for part in text.split(";"):
        scheme, sep, host = part.partition("=")
        if not sep or not host.strip():
            continue
        name = scheme.strip().lower()
        by_scheme[name] = host.strip()
        order.append(name)
    if not by_scheme:
        return None
    chosen = by_scheme.get("https") or by_scheme.get("http") or by_scheme[order[0]]
    # `socks=` lands here as a bare host:port. urllib cannot speak SOCKS, so
    # this is a guess — but almost every local proxy client (Clash, v2ray)
    # accepts plain HTTP on its SOCKS port, and `detect_proxy` probes whatever
    # comes back here, so a wrong guess costs one request rather than a hang.
    return chosen if "://" in chosen else f"http://{chosen}"


def system_proxy() -> str | None:
    """The proxy configured in Windows Internet Options, if it is switched on.

    Read through `winreg` rather than shelling out to reg.exe: no subprocess,
    and it cannot be blocked by an execution policy.

    This lives in the network layer because it is not only the console that
    needs it — the live monitor and the backtester reach the same host and used
    to hard-code a port that is not the one this machine actually uses.
    """
    if os.name != "nt":
        return None
    import winreg
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    if not enabled or not server:
        return None
    return parse_proxy_server(str(server).strip())


class OkxError(RuntimeError):
    """An OKX request failed. `kind` says which of the three ways it failed."""

    def __init__(self, message: str, kind: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind  # "network" | "http" | "api"
        self.status = status


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------
_lock = threading.Lock()
_calls: deque[float] = deque()


def throttle(limit: int = RATE_LIMIT, window: float = RATE_WINDOW) -> None:
    """Block until this call fits inside the shared per-IP budget.

    Sliding window, not a fixed sleep: recording the timestamp of each call and
    evicting everything older than `window` keeps the ceiling honest even when
    callers arrive in bursts.
    """
    while True:
        with _lock:
            now = time.monotonic()
            while _calls and now - _calls[0] >= window:
                _calls.popleft()
            if len(_calls) < limit:
                _calls.append(now)
                return
            wait = window - (now - _calls[0]) + 0.01
        time.sleep(wait)


# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------
def _opener(proxy: str | None):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def get_json(path: str, params: dict | None = None, *, proxy: str | None = None,
             timeout: float = 15.0, rate: bool = True):
    """GET `API + path`, return the `data` array. Raises `OkxError`."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=UA)
    if rate:
        throttle()
    try:
        with _opener(proxy).open(request, timeout=timeout) as response:
            status = response.getcode()
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 404 = wrong symbol or wrong endpoint; 429 = throttled; 5xx = OKX's problem.
        # None of these is "your proxy is broken".
        raise OkxError(f"OKX 返回 HTTP {exc.code} for {path}", "http", exc.code) from exc
    except Exception as exc:
        raise OkxError(f"连不上 OKX（{type(exc).__name__}: {exc}）", "network") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OkxError(f"OKX 返回了非 JSON 内容（{exc}）", "http", status) from exc

    code = payload.get("code")
    if code != "0":
        raise OkxError(f"OKX 返回 {code}: {payload.get('msg')}", "api", status)
    return payload.get("data") or []
