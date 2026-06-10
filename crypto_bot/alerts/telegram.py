"""Telegram notifications (Phase 12).

Sends formatted alerts (new signal, entry, exit, daily summary, errors) to the
configured chat via the raw Bot API over `requests` — synchronous, so it slots
into the trading loop without an async runtime.

Hard rule: **alerting never crashes the trading loop.** Every send is wrapped so
any failure (network, bad token, missing chat id) is logged and swallowed,
returning False. A small dedup throttle suppresses identical messages within a
short window so the bot can't spam the chat.

Token + chat id come from `config_store` (`TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`). `resolve_chat_id()` can auto-discover the chat id from
getUpdates once the user has messaged the bot.

CLI:  python -m crypto_bot.alerts.telegram [test|resolve]
"""

from __future__ import annotations

import html
import time

import requests

from crypto_bot import config_store
from crypto_bot.logging_setup import get_logger

log = get_logger(__name__)

_API = "https://api.telegram.org"
_TIMEOUT = 10
_PLACEHOLDER = "REPLACE_ME"
DEDUP_WINDOW_SEC = 60.0

_last_sent: dict[str, float] = {}


# --- Config -----------------------------------------------------------------
def get_token() -> str | None:
    return config_store.get("TELEGRAM_BOT_TOKEN", None)


def get_chat_id() -> str | None:
    return config_store.get("TELEGRAM_CHAT_ID", None)


def _valid(v: str | None) -> bool:
    return bool(v) and v != _PLACEHOLDER


def is_configured() -> bool:
    return _valid(get_token()) and _valid(get_chat_id())


# --- Throttle ---------------------------------------------------------------
def _should_send(key: str | None, now: float | None = None) -> bool:
    """Suppress an identical message key seen within DEDUP_WINDOW_SEC."""
    if key is None:
        return True
    now = now if now is not None else time.time()
    last = _last_sent.get(key)
    if last is not None and (now - last) < DEDUP_WINDOW_SEC:
        return False
    _last_sent[key] = now
    return True


# --- Transport (mockable) ---------------------------------------------------
def _post(token: str, payload: dict) -> bool:
    r = requests.post(f"{_API}/bot{token}/sendMessage", json=payload, timeout=_TIMEOUT)
    r.raise_for_status()
    return True


def send_message(text: str, *, dedup_key: str | None = None,
                 disable_notification: bool = False) -> bool:
    """Send a message. Never raises — returns True on success, else False."""
    try:
        if not is_configured():
            log.warning("Telegram not configured (token/chat id) — skipping alert.")
            return False
        if not _should_send(dedup_key):
            log.debug("Telegram alert throttled (dedup): %s", dedup_key)
            return False
        payload = {
            "chat_id": get_chat_id(), "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True, "disable_notification": disable_notification,
        }
        return _post(get_token(), payload)
    except Exception as e:  # noqa: BLE001 — alerting must never break the loop
        log.warning("Telegram send failed (suppressed): %s", e)
        return False


# --- Formatters -------------------------------------------------------------
def _e(v) -> str:
    return html.escape(str(v))


def _fmt_price(v) -> str:
    return f"{float(v):,.2f}" if v is not None else "n/a"


def format_signal(*, symbol, signal, source, score, entry, stop, target, rr) -> str:
    rr_s = f"{float(rr):.2f}" if rr is not None else "n/a"
    return (f"📡 <b>Signal {_e(signal)}</b> {_e(symbol)}\n"
            f"score <b>{float(score):.1f}</b> · {_e(source)}\n"
            f"entry {_fmt_price(entry)} · stop {_fmt_price(stop)} · "
            f"target {_fmt_price(target)} · R:R {rr_s}")


def format_entry(*, symbol, qty, price, trade_id, paper=True) -> str:
    tag = "PAPER" if paper else "LIVE"
    return (f"🟢 <b>Entry</b> [{tag}] {_e(symbol)}\n"
            f"qty {_e(qty)} @ {_fmt_price(price)} · trade #{_e(trade_id)}")


def format_exit(*, symbol, exit_price, pnl, pnl_pct, reason) -> str:
    emoji = "✅" if (pnl is not None and pnl >= 0) else "🔴"
    pnl_s = f"{float(pnl):+,.2f}" if pnl is not None else "n/a"
    pct_s = f"{float(pnl_pct):+.2f}%" if pnl_pct is not None else "n/a"
    return (f"{emoji} <b>Exit</b> {_e(symbol)} ({_e(reason)})\n"
            f"@ {_fmt_price(exit_price)} · P&amp;L {pnl_s} ({pct_s})")


def format_daily_summary(summary: dict) -> str:
    p = summary.get("portfolio", {})
    return (f"📊 <b>Daily summary {_e(summary.get('trade_date'))}</b>\n"
            f"buys {_e(p.get('num_buys', 0))} · sells {_e(p.get('num_sells', 0))}\n"
            f"realized {_e(p.get('realized_pnl', 0))} "
            f"({_e(p.get('realized_pnl_pct', 0))}%) · "
            f"unrealized {_e(p.get('unrealized_pnl', 0))}\n"
            f"equity {_fmt_price(summary.get('equity'))} · "
            f"drawdown {_e(summary.get('drawdown_pct', 0))}%")


def format_error(context: str, error) -> str:
    return f"⚠️ <b>Error</b> {_e(context)}\n<code>{_e(error)}</code>"


# --- Notify helpers (loop-safe) ---------------------------------------------
def notify_signal(**kw) -> bool:
    key = f"signal:{kw.get('symbol')}:{kw.get('signal')}:{kw.get('score')}"
    return send_message(format_signal(**kw), dedup_key=key)


def notify_entry(**kw) -> bool:
    return send_message(format_entry(**kw), dedup_key=f"entry:{kw.get('trade_id')}")


def notify_exit(**kw) -> bool:
    return send_message(format_exit(**kw),
                        dedup_key=f"exit:{kw.get('symbol')}:{kw.get('exit_price')}")


def notify_daily_summary(summary: dict) -> bool:
    return send_message(format_daily_summary(summary),
                        dedup_key=f"daily:{summary.get('trade_date')}")


def notify_error(context: str, error) -> bool:
    return send_message(format_error(context, error), dedup_key=f"error:{context}")


# --- Chat-id discovery ------------------------------------------------------
def resolve_chat_id(store: bool = True) -> str | None:
    """Find the chat id from getUpdates (user must have messaged the bot)."""
    token = get_token()
    if not _valid(token):
        log.warning("No bot token — cannot resolve chat id.")
        return None
    try:
        r = requests.get(f"{_API}/bot{token}/getUpdates", timeout=_TIMEOUT)
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("getUpdates failed: %s", e)
        return None
    if not data.get("ok"):
        return None
    for upd in reversed(data.get("result", [])):
        msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            cid = str(chat["id"])
            if store:
                config_store.set("TELEGRAM_CHAT_ID", cid)
                log.info("Resolved & stored TELEGRAM_CHAT_ID=%s", cid)
            return cid
    log.warning("No chats found in getUpdates — message the bot first.")
    return None


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    if cmd == "resolve":
        print("resolved chat id:", resolve_chat_id())
    else:
        ok = send_message("🤖 <b>CryptoTradeWisBot</b> test alert — Phase 12 wired up.")
        print("sent:", ok, "| configured:", is_configured())
