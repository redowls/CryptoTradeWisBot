"""Indicator engine (Phase 3): EMA / ATR / RSI / ADX / volume average.

Hand-rolled with standard definitions (summary.md §5.2) for full control and
deterministic, fixture-testable output:

  - EMA: recursive, k = 2/(N+1), seeded at the first bar (``ewm(adjust=False)``).
  - Wilder RMA: ``ewm(alpha=1/period, adjust=False)`` — the smoothing behind
    ATR, RSI and ADX. Output is NaN until ``period`` bars exist.
  - RSI(14), ATR(14), ADX(14) via Wilder; 20-bar SMA of volume.

``compute_indicators(df)`` adds indicator columns to an OHLCV frame indexed by
``ts`` (ascending). ``compute_all()`` runs every active symbol/timeframe and
optionally caches results to the ``indicators`` table.

CLI:  python -m crypto_bot.indicators.engine
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import and_, select

from crypto_bot.db import get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import Indicator, MarketBar, Watchlist

log = get_logger(__name__)

EMA_PERIODS = (8, 10, 20, 21, 34, 55)
ATR_PERIOD = 14
RSI_PERIOD = 14
ADX_PERIOD = 14
VOL_AVG_PERIOD = 20

# Columns the engine produces (besides OHLCV), in DB order.
INDICATOR_COLUMNS = [
    "ema8", "ema10", "ema20", "ema21", "ema34", "ema55",
    "atr", "rsi", "adx", "vol_avg",
]


# --- Primitive indicators ---------------------------------------------------
def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential MA, k = 2/(span+1), seeded at the first value."""
    return series.ewm(span=span, adjust=False).mean()


def wilder_rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA): ewm(alpha=1/period). NaN until `period` bars."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr


def atr(high, low, close, period: int = ATR_PERIOD) -> pd.Series:
    return wilder_rma(true_range(high, low, close), period)


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff().fillna(0.0)
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = wilder_rma(gain, period)
    avg_loss = wilder_rma(loss, period)
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    # Edge cases: no losses -> 100; no gains -> 0; perfectly flat -> 50.
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_gain == 0) & (avg_loss > 0), 0.0)
    out = out.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return out


def adx(high, low, close, period: int = ADX_PERIOD) -> pd.Series:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move.fillna(0)
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move.fillna(0)

    atr_ = wilder_rma(true_range(high, low, close), period)
    plus_di = 100.0 * wilder_rma(plus_dm, period) / atr_
    minus_di = 100.0 * wilder_rma(minus_dm, period) / atr_

    di_sum = (plus_di + minus_di).replace(0.0, pd.NA)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return wilder_rma(dx.astype(float), period)


def volume_sma(volume: pd.Series, period: int = VOL_AVG_PERIOD) -> pd.Series:
    return volume.rolling(period, min_periods=period).mean()


# --- Engine -----------------------------------------------------------------
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of an OHLCV frame (index=ts asc) with indicator columns added."""
    out = df.copy()
    close, high, low, vol = out["close"], out["high"], out["low"], out["volume"]
    for p in EMA_PERIODS:
        out[f"ema{p}"] = ema(close, p)
    out["atr"] = atr(high, low, close)
    out["rsi"] = rsi(close)
    out["adx"] = adx(high, low, close)
    out["vol_avg"] = volume_sma(vol)
    return out


def load_bars(session, symbol: str, timeframe: str) -> pd.DataFrame:
    """Load OHLCV for one symbol/timeframe from market_bars, ascending by ts."""
    rows = session.execute(
        select(
            MarketBar.ts, MarketBar.open, MarketBar.high,
            MarketBar.low, MarketBar.close, MarketBar.volume,
        )
        .where(and_(MarketBar.symbol == symbol, MarketBar.timeframe == timeframe))
        .order_by(MarketBar.ts)
    ).all()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    if not df.empty:
        df = df.set_index("ts")
        df = df.astype({c: "float64" for c in ["open", "high", "low", "close", "volume"]})
    return df


def _cache(session, symbol: str, timeframe: str, df: pd.DataFrame) -> int:
    """Upsert indicator rows (only fully-warmed rows, i.e. adx not NaN). Returns count."""
    ready = df.dropna(subset=["adx", "atr", "rsi", "vol_avg"])
    if ready.empty:
        return 0
    timestamps = [ts.to_pydatetime() for ts in ready.index]
    existing = {
        r.ts: r
        for r in session.scalars(
            select(Indicator).where(
                and_(
                    Indicator.symbol == symbol,
                    Indicator.timeframe == timeframe,
                    Indicator.ts.in_(timestamps),
                )
            )
        )
    }
    n = 0
    for ts, row in zip(timestamps, ready.itertuples()):
        vals = {col: float(getattr(row, col)) for col in INDICATOR_COLUMNS}
        rec = existing.get(ts)
        if rec is None:
            session.add(Indicator(symbol=symbol, timeframe=timeframe, ts=ts, **vals))
        else:
            for col, v in vals.items():
                setattr(rec, col, v)
        n += 1
    session.commit()
    return n


def compute_for_symbol(session, symbol: str, timeframe: str, persist: bool = False) -> pd.DataFrame:
    """Compute indicators for one symbol/timeframe; optionally cache to DB."""
    df = load_bars(session, symbol, timeframe)
    if df.empty:
        log.warning("No bars for %s %s — skipping indicators.", symbol, timeframe)
        return df
    out = compute_indicators(df)
    if persist:
        cached = _cache(session, symbol, timeframe, out)
        log.info("%-9s %-3s: indicators computed on %d bars, %d cached.",
                 symbol, timeframe, len(out), cached)
    return out


def compute_all(timeframes: list[str] | None = None, persist: bool = True) -> pd.DataFrame:
    """Compute (and optionally cache) indicators for all active symbols/timeframes.

    Returns a tidy frame keyed by (symbol, timeframe, ts).
    """
    timeframes = timeframes or ["1H", "4H", "1D"]
    tidy_parts = []
    with get_session() as session:
        symbols = list(
            session.scalars(select(Watchlist.symbol).where(Watchlist.is_active == True)).all()  # noqa: E712
        )
        for symbol in symbols:
            for tf in timeframes:
                out = compute_for_symbol(session, symbol, tf, persist=persist)
                if out.empty:
                    continue
                part = out.reset_index()
                part.insert(0, "timeframe", tf)
                part.insert(0, "symbol", symbol)
                tidy_parts.append(part)
    if not tidy_parts:
        return pd.DataFrame()
    return pd.concat(tidy_parts, ignore_index=True).set_index(["symbol", "timeframe", "ts"])


if __name__ == "__main__":
    tidy = compute_all(persist=True)
    print(f"OK: computed indicators for {tidy.index.droplevel('ts').nunique()} "
          f"symbol/timeframe series, {len(tidy)} total rows.")
