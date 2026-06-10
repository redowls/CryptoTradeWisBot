"""Phase 12 (DB-free): formatters, throttle, and never-crash guarantee."""

from crypto_bot.alerts import telegram as tg


# --- Formatters -------------------------------------------------------------
def test_format_signal_has_key_fields():
    s = tg.format_signal(symbol="BTC/USD", signal="BUY", source="BOTH", score=85.0,
                         entry=60000, stop=58000, target=66000, rr=3.0)
    assert "BUY" in s and "BTC/USD" in s and "85.0" in s and "BOTH" in s
    assert "60,000" in s and "3.00" in s


def test_format_exit_sign_and_emoji():
    win = tg.format_exit(symbol="ETH/USD", exit_price=2100, pnl=100.0, pnl_pct=10.0, reason="target")
    loss = tg.format_exit(symbol="ETH/USD", exit_price=1900, pnl=-50.0, pnl_pct=-5.0, reason="stop_loss")
    assert "✅" in win and "+100.00" in win and "+10.00%" in win
    assert "🔴" in loss and "-50.00" in loss and "stop_loss" in loss


def test_format_daily_summary():
    s = tg.format_daily_summary({"trade_date": "2026-06-10", "equity": 10140.0,
        "drawdown_pct": 0.0, "portfolio": {"num_buys": 4, "num_sells": 3,
        "realized_pnl": 140.0, "realized_pnl_pct": 1.08, "unrealized_pnl": 24.5}})
    assert "2026-06-10" in s and "140" in s and "1.08" in s


def test_format_error_escapes_html():
    s = tg.format_error("ingest", "boom <script>")
    assert "&lt;script&gt;" in s and "<script>" not in s


# --- Throttle ---------------------------------------------------------------
def test_dedup_throttle_suppresses_repeat():
    tg._last_sent.clear()
    assert tg._should_send("k", now=1000.0) is True
    assert tg._should_send("k", now=1000.0 + 5) is False         # within window
    assert tg._should_send("k", now=1000.0 + tg.DEDUP_WINDOW_SEC + 1) is True  # after window


def test_dedup_none_key_always_sends():
    assert tg._should_send(None) is True
    assert tg._should_send(None) is True


# --- Never crashes ----------------------------------------------------------
def test_send_message_swallows_transport_errors(monkeypatch):
    monkeypatch.setattr(tg, "is_configured", lambda: True)
    monkeypatch.setattr(tg, "get_chat_id", lambda: "123")
    monkeypatch.setattr(tg, "get_token", lambda: "tok")

    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(tg, "_post", boom)
    tg._last_sent.clear()
    assert tg.send_message("hi") is False  # returns False, does not raise


def test_send_message_returns_false_when_unconfigured(monkeypatch):
    monkeypatch.setattr(tg, "is_configured", lambda: False)
    assert tg.send_message("hi") is False
