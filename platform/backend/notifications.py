"""Durable signal email outbox; SMTP runs outside the trading thread."""
from __future__ import annotations

import json
import logging
import smtplib
import sqlite3
import ssl
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "email.local.json"
DATABASE = ROOT / "data" / "email-outbox.db"
log = logging.getLogger(__name__)


def settings():
    if not CONFIG.exists():
        return None
    cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    if not cfg.get("enabled"):
        return None
    for key in ("host", "username", "password", "recipient"):
        if not cfg.get(key):
            raise ValueError(f"邮件配置缺少 {key}")
    if cfg.get("security", "ssl") not in ("ssl", "starttls"):
        raise ValueError("邮件 security 必须为 ssl 或 starttls")
    return cfg


@contextmanager
def connect():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, inst TEXT, side TEXT, bar_ts INTEGER,
                reason TEXT, sent INTEGER NOT NULL DEFAULT 0,
                UNIQUE(inst, side, bar_ts))""")
            yield conn
    finally:
        conn.close()


def enqueue(inst, side, bar_ts, reason):
    try:
        if settings() is None:
            return
        with connect() as conn:
            conn.execute("INSERT OR IGNORE INTO outbox(inst,side,bar_ts,reason) VALUES(?,?,?,?)",
                         (inst, side, int(bar_ts), reason))
    except Exception:
        log.exception("信号邮件入队失败；交易扫描继续")


def send(cfg, row, *, test=False):
    direction = "做多" if row["side"] == "long" else "做空"
    stamp = datetime.fromtimestamp(row["bar_ts"] / 1000, timezone.utc).astimezone()
    msg = EmailMessage()
    msg["Subject"] = "[OKX 邮件测试] 发送配置验证" if test else f"[OKX 策略信号] {row['inst']} {direction}"
    msg["From"] = cfg.get("sender") or cfg["username"]
    msg["To"] = cfg["recipient"]
    msg["Message-ID"] = f"<okx-signal-{row['inst']}-{row['side']}-{row['bar_ts']}@localhost>"
    msg.set_content(f"币种：{row['inst']}\n方向：{direction}\n信号K线时间：{stamp:%Y-%m-%d %H:%M:%S %z}\n"
                    f"原因：{row['reason']}\n\n这是策略信号提醒，实际成交请查看平台。\nhttp://127.0.0.1:8788")
    if test:
        msg.set_content("这是一封由你主动要求发送的邮件测试，用于验证 SMTP 配置。\n"
                        "本邮件不代表交易信号，也未触发任何交易。\n"
                        "日常提醒只在策略产生做多或做空信号时发送。")
    secure = cfg.get("security", "ssl")
    cls = smtplib.SMTP_SSL if secure == "ssl" else smtplib.SMTP
    kwargs = {"timeout": 15}
    if secure == "ssl":
        kwargs["context"] = ssl.create_default_context()
    with cls(cfg["host"], int(cfg.get("port", 465 if secure == "ssl" else 587)), **kwargs) as smtp:
        if secure == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        smtp.login(cfg["username"], cfg["password"])
        smtp.send_message(msg)


def deliver_pending():
    cfg = settings()
    if cfg is None:
        return
    with connect() as conn:
        rows = conn.execute("SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT 20").fetchall()
        for row in rows:
            send(cfg, row)
            conn.execute("UPDATE outbox SET sent=1 WHERE id=?", (row["id"],))
            conn.commit()
            log.info("信号邮件已发送：%s %s", row["inst"], row["side"])


def run(stop: threading.Event):
    while not stop.is_set():
        try:
            deliver_pending()
        except Exception:
            log.exception("邮件发送失败，60 秒后重试；请检查本地邮件配置")
            if stop.wait(60):
                return
        stop.wait(5)
