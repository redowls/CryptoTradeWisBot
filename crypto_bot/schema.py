"""Idempotent schema creation + seeding (Phase 1).

Run as a module to (re)create all tables and seed defaults:

    python -m crypto_bot.schema

Safe to re-run: tables are created if-not-exists; config keys and watchlist
symbols are inserted only when absent, so real credentials and manual edits are
never clobbered. NO real secrets live in this file — credential keys are seeded
with the placeholder ``REPLACE_ME`` and filled in later via ``config_store.set``.
"""

from __future__ import annotations

from sqlalchemy import select

from crypto_bot import config_store
from crypto_bot.db import get_engine, get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import Base, Watchlist

log = get_logger(__name__)

# (value, is_secret) — credentials are placeholders; params from summary.md §7.
DEFAULT_CONFIG: dict[str, tuple[str, bool]] = {
    # --- Credentials & endpoints (real values injected post-seed) ---
    "ALPACA_API_KEY": ("REPLACE_ME", True),
    "ALPACA_SECRET": ("REPLACE_ME", True),
    "ALPACA_BASE_URL": ("https://paper-api.alpaca.markets", False),  # PAPER until Phase 14
    "TELEGRAM_BOT_TOKEN": ("REPLACE_ME", True),
    "TELEGRAM_CHAT_ID": ("REPLACE_ME", False),
    "LIVE_TRADING_ENABLED": ("false", False),  # Phase 9 guardrail; never live before Phase 14/15
    # --- Tunable parameters (summary.md §7) ---
    "TRIGGER_TF": ("1H", False),
    "TREND_TF": ("4H", False),
    "PIVOT_STRENGTH": ("5", False),
    "CLUSTER_TOLERANCE_PCT": ("0.4", False),
    "BREAKOUT_BUFFER_PCT": ("0.5", False),
    "VOLUME_MULT": ("1.5", False),
    "RSI_PERIOD": ("14", False),
    "RSI_OVERBOUGHT": ("70", False),
    "ATR_PERIOD": ("14", False),
    "ATR_STOP_MULT": ("2.5", False),
    "MIN_RR": ("2.0", False),
    "EMA_SHORT": ("8,10,20", False),
    "EMA_LONG": ("21,34,55", False),
    "ADX_MIN": ("20", False),
    "CONFIDENCE_THRESHOLD": ("70", False),
    "BASE_RISK_PCT": ("1.0", False),
    "MAX_RISK_PCT": ("2.0", False),
    "MAX_PORTFOLIO_HEAT_PCT": ("6.0", False),
    "MAX_CONCURRENT_POSITIONS": ("5", False),
    "SIZING_MODE": ("tiered", False),
    "BREAKOUT_MODE": ("aggressive", False),
    "CHANDELIER": ("22,3.0", False),
    "DRAWDOWN_BRAKE_PCT": ("10", False),
    "TIME_STOP_BARS": ("24", False),
}

STARTER_WATCHLIST: list[str] = ["BTC/USD", "ETH/USD", "SOL/USD"]


def create_schema() -> None:
    """Create all tables if they do not already exist (idempotent)."""
    engine = get_engine()
    Base.metadata.create_all(engine, checkfirst=True)
    log.info("Schema ensured (%d tables).", len(Base.metadata.tables))


def seed_config() -> int:
    """Insert any missing default config keys. Returns count inserted."""
    inserted = 0
    for key, (value, is_secret) in DEFAULT_CONFIG.items():
        if not config_store.exists(key):
            config_store.set(key, value, is_secret=is_secret)
            inserted += 1
    log.info("Seeded config: %d new key(s), %d already present.",
             inserted, len(DEFAULT_CONFIG) - inserted)
    return inserted


def seed_watchlist() -> int:
    """Insert any missing starter symbols. Returns count inserted."""
    inserted = 0
    with get_session() as session:
        existing = set(session.scalars(select(Watchlist.symbol)).all())
        for symbol in STARTER_WATCHLIST:
            if symbol not in existing:
                session.add(Watchlist(symbol=symbol, asset_class="crypto"))
                inserted += 1
        session.commit()
    log.info("Seeded watchlist: %d new symbol(s).", inserted)
    return inserted


def init_db() -> None:
    """Full Phase 1 initialization: schema + seeds."""
    create_schema()
    seed_config()
    seed_watchlist()


if __name__ == "__main__":
    init_db()
    tables = ", ".join(sorted(Base.metadata.tables))
    print(f"OK: schema + seeds applied.\nTables: {tables}")
    print(f"Config keys: {len(config_store.all_keys())}")
    with get_session() as s:
        symbols = s.scalars(select(Watchlist.symbol)).all()
    print(f"Watchlist: {', '.join(symbols)}")
