"""Phase 1 (DB-free): verify the seed config covers the spec and is well-formed."""

from crypto_bot.schema import DEFAULT_CONFIG, STARTER_WATCHLIST

# Every tunable parameter from summary.md §7 must be present.
SPEC_PARAM_KEYS = {
    "TRIGGER_TF", "TREND_TF", "PIVOT_STRENGTH", "CLUSTER_TOLERANCE_PCT",
    "BREAKOUT_BUFFER_PCT", "VOLUME_MULT", "RSI_PERIOD", "RSI_OVERBOUGHT",
    "ATR_PERIOD", "ATR_STOP_MULT", "MIN_RR", "EMA_SHORT", "EMA_LONG",
    "ADX_MIN", "CONFIDENCE_THRESHOLD", "BASE_RISK_PCT", "MAX_RISK_PCT",
    "MAX_PORTFOLIO_HEAT_PCT", "MAX_CONCURRENT_POSITIONS", "SIZING_MODE",
    "BREAKOUT_MODE", "CHANDELIER", "DRAWDOWN_BRAKE_PCT", "TIME_STOP_BARS",
}
CREDENTIAL_KEYS = {
    "ALPACA_API_KEY", "ALPACA_SECRET", "ALPACA_BASE_URL",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
}


def test_all_spec_params_present():
    missing = SPEC_PARAM_KEYS - set(DEFAULT_CONFIG)
    assert not missing, f"missing §7 params: {missing}"


def test_credential_keys_present_and_flagged_secret():
    for key in CREDENTIAL_KEYS:
        assert key in DEFAULT_CONFIG, f"missing credential key: {key}"
    # The sensitive ones must be flagged is_secret for Phase 15 encryption.
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET", "TELEGRAM_BOT_TOKEN"):
        assert DEFAULT_CONFIG[key][1] is True, f"{key} should be is_secret=True"


def test_paper_endpoint_default():
    assert DEFAULT_CONFIG["ALPACA_BASE_URL"][0] == "https://paper-api.alpaca.markets"
    assert DEFAULT_CONFIG["LIVE_TRADING_ENABLED"][0] == "false"


def test_no_real_secrets_in_source():
    """Credential placeholders must not contain real-looking values."""
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET", "TELEGRAM_BOT_TOKEN"):
        assert DEFAULT_CONFIG[key][0] == "REPLACE_ME"


def test_starter_watchlist():
    assert STARTER_WATCHLIST == ["BTC/USD", "ETH/USD", "SOL/USD"]
