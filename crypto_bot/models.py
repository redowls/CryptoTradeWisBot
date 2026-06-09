"""SQLAlchemy ORM models / table definitions (summary.md §6).

Microsoft SQL Server conventions:
  - prices/quantities  -> DECIMAL(18,8) (crypto precision); volume DECIMAL(28,8)
  - percentages        -> DECIMAL(9,4)
  - timestamps         -> DATETIME2 (UTC), defaulting to SYSUTCDATETIME()
  - booleans           -> BIT (SQLAlchemy Boolean)
  - text               -> NVARCHAR(n) (Unicode) / NVARCHAR(MAX) (UnicodeText)
  - identity PKs       -> BIGINT IDENTITY (BigInteger autoincrement)
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    Unicode,
    UnicodeText,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Reusable column types ------------------------------------------------------
_PRICE = Numeric(18, 8)
_VOLUME = Numeric(28, 8)
_PCT = Numeric(9, 4)
_SCORE = Numeric(5, 2)
_MONEY = Numeric(18, 2)
_UTCNOW = text("SYSUTCDATETIME()")


def _id_col() -> Mapped[int]:
    """BIGINT IDENTITY primary key."""
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


class Base(DeclarativeBase):
    pass


# 6.1 Config & credentials ---------------------------------------------------
class AppConfig(Base):
    __tablename__ = "app_config"

    config_key: Mapped[str] = mapped_column(Unicode(100), primary_key=True)
    config_value: Mapped[str] = mapped_column(UnicodeText, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    updated_at: Mapped[object] = mapped_column(DATETIME2, server_default=_UTCNOW)


# 6.2 Analyzable symbols -----------------------------------------------------
class Watchlist(Base):
    __tablename__ = "watchlist"

    symbol_id: Mapped[int] = _id_col()
    symbol: Mapped[str] = mapped_column(Unicode(20), unique=True, nullable=False)
    asset_class: Mapped[str] = mapped_column(Unicode(20), server_default=text("'crypto'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    added_at: Mapped[object] = mapped_column(DATETIME2, server_default=_UTCNOW)
    notes: Mapped[str | None] = mapped_column(Unicode(255), nullable=True)


# 6.3 OHLCV cache ------------------------------------------------------------
class MarketBar(Base):
    __tablename__ = "market_bars"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "ts", name="uq_bar"),)

    bar_id: Mapped[int] = _id_col()
    symbol: Mapped[str] = mapped_column(Unicode(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(Unicode(10), nullable=False)
    ts: Mapped[object] = mapped_column(DATETIME2, nullable=False)
    open: Mapped[float | None] = mapped_column(_PRICE)
    high: Mapped[float | None] = mapped_column(_PRICE)
    low: Mapped[float | None] = mapped_column(_PRICE)
    close: Mapped[float | None] = mapped_column(_PRICE)
    volume: Mapped[float | None] = mapped_column(_VOLUME)


# 6.4 Indicator cache --------------------------------------------------------
class Indicator(Base):
    __tablename__ = "indicators"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "ts", name="uq_ind"),)

    ind_id: Mapped[int] = _id_col()
    symbol: Mapped[str] = mapped_column(Unicode(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(Unicode(10), nullable=False)
    ts: Mapped[object] = mapped_column(DATETIME2, nullable=False)
    ema8: Mapped[float | None] = mapped_column(_PRICE)
    ema10: Mapped[float | None] = mapped_column(_PRICE)
    ema20: Mapped[float | None] = mapped_column(_PRICE)
    ema21: Mapped[float | None] = mapped_column(_PRICE)
    ema34: Mapped[float | None] = mapped_column(_PRICE)
    ema55: Mapped[float | None] = mapped_column(_PRICE)
    atr: Mapped[float | None] = mapped_column(_PRICE)
    rsi: Mapped[float | None] = mapped_column(_PCT)
    adx: Mapped[float | None] = mapped_column(_PCT)
    vol_avg: Mapped[float | None] = mapped_column(_VOLUME)


# 6.5 Detected support/resistance levels -------------------------------------
class SRLevel(Base):
    __tablename__ = "sr_levels"

    level_id: Mapped[int] = _id_col()
    symbol: Mapped[str] = mapped_column(Unicode(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(Unicode(10), nullable=False)
    level_price: Mapped[float] = mapped_column(_PRICE, nullable=False)
    level_type: Mapped[str] = mapped_column(Unicode(12), nullable=False)  # support|resistance
    source: Mapped[str] = mapped_column(Unicode(20), nullable=False)  # pivot|cluster|poc|vah|val
    touch_count: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    strength: Mapped[float | None] = mapped_column(_PCT)
    detected_at: Mapped[object] = mapped_column(DATETIME2, server_default=_UTCNOW)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))


# 6.6 Signals ----------------------------------------------------------------
class Signal(Base):
    __tablename__ = "signals"

    signal_id: Mapped[int] = _id_col()
    symbol: Mapped[str] = mapped_column(Unicode(20), nullable=False)
    ts: Mapped[object] = mapped_column(DATETIME2, nullable=False)
    signal: Mapped[str] = mapped_column(Unicode(8), nullable=False)  # BUY|HOLD|AVOID
    signal_source: Mapped[str] = mapped_column(Unicode(10), nullable=False)  # BREAKOUT|MA|BOTH
    confidence_score: Mapped[float] = mapped_column(_SCORE, nullable=False)
    score_breakdown: Mapped[str | None] = mapped_column(UnicodeText)  # JSON
    entry_price: Mapped[float | None] = mapped_column(_PRICE)
    stop_price: Mapped[float | None] = mapped_column(_PRICE)
    target_price: Mapped[float | None] = mapped_column(_PRICE)
    rr_ratio: Mapped[float | None] = mapped_column(_PCT)
    acted: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    notes: Mapped[str | None] = mapped_column(Unicode(500))


# 6.7 Trades / orders --------------------------------------------------------
class Trade(Base):
    __tablename__ = "trades"

    trade_id: Mapped[int] = _id_col()
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.signal_id"), nullable=True)
    symbol: Mapped[str] = mapped_column(Unicode(20), nullable=False)
    side: Mapped[str] = mapped_column(Unicode(4), nullable=False)  # BUY|SELL
    qty: Mapped[float] = mapped_column(_PRICE, nullable=False)
    entry_price: Mapped[float | None] = mapped_column(_PRICE)
    entry_time: Mapped[object | None] = mapped_column(DATETIME2)
    stop_price: Mapped[float | None] = mapped_column(_PRICE)
    target_price: Mapped[float | None] = mapped_column(_PRICE)
    exit_price: Mapped[float | None] = mapped_column(_PRICE)
    exit_time: Mapped[object | None] = mapped_column(DATETIME2)
    realized_pnl: Mapped[float | None] = mapped_column(_PRICE)
    realized_pnl_pct: Mapped[float | None] = mapped_column(_PCT)
    status: Mapped[str] = mapped_column(Unicode(10), server_default=text("'OPEN'"))  # OPEN|CLOSED|CANCELLED
    confidence_score: Mapped[float | None] = mapped_column(_SCORE)
    signal_source: Mapped[str | None] = mapped_column(Unicode(10))
    risk_pct: Mapped[float | None] = mapped_column(_PCT)
    fees: Mapped[float | None] = mapped_column(_PRICE)
    alpaca_order_id: Mapped[str | None] = mapped_column(Unicode(64))
    is_paper: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))


# 6.8 Daily summary ----------------------------------------------------------
class DailySummary(Base):
    __tablename__ = "daily_summary"
    __table_args__ = (UniqueConstraint("trade_date", "symbol", name="uq_daily"),)

    summary_id: Mapped[int] = _id_col()
    trade_date: Mapped[object] = mapped_column(Date, nullable=False)
    symbol: Mapped[str | None] = mapped_column(Unicode(20), nullable=True)  # NULL = portfolio total
    num_buys: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    num_sells: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    realized_pnl: Mapped[float] = mapped_column(_PRICE, server_default=text("0"))
    realized_pnl_pct: Mapped[float] = mapped_column(_PCT, server_default=text("0"))
    unrealized_pnl: Mapped[float] = mapped_column(_PRICE, server_default=text("0"))
    win_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    loss_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    notes: Mapped[str | None] = mapped_column(Unicode(500))


# 6.9 Account snapshots ------------------------------------------------------
class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    snapshot_id: Mapped[int] = _id_col()
    ts: Mapped[object] = mapped_column(DATETIME2, server_default=_UTCNOW)
    equity: Mapped[float | None] = mapped_column(_MONEY)
    cash: Mapped[float | None] = mapped_column(_MONEY)
    open_positions: Mapped[int | None] = mapped_column(Integer)
    portfolio_heat_pct: Mapped[float | None] = mapped_column(_PCT)
    drawdown_pct: Mapped[float | None] = mapped_column(_PCT)


# 6.10 Run log ---------------------------------------------------------------
class BotRun(Base):
    __tablename__ = "bot_runs"

    run_id: Mapped[int] = _id_col()
    started_at: Mapped[object | None] = mapped_column(DATETIME2)
    finished_at: Mapped[object | None] = mapped_column(DATETIME2)
    status: Mapped[str | None] = mapped_column(Unicode(20))
    symbols_scanned: Mapped[int | None] = mapped_column(Integer)
    signals_generated: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(UnicodeText)
