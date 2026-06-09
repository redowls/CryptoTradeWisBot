"""Market-data ingestion: Alpaca crypto OHLCV -> ``market_bars`` (Phase 2).

- Builds an Alpaca crypto data client (keys optional; historical crypto bars are
  a public endpoint, but keys are used when present and not placeholders).
- Fetches 1H / 4H / 1D bars for each active watchlist symbol.
- Upserts into ``market_bars`` keyed on (symbol, timeframe, ts) so re-running
  never creates duplicates; the most recent (possibly partial) bar is updated.
- Incremental: only fetches from the latest stored bar forward.
- Retries with backoff on transient errors; tolerates empty responses.

CLI:  python -m crypto_bot.data.ingest
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from sqlalchemy import and_, func, select

from crypto_bot import config_store
from crypto_bot.db import get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import MarketBar, Watchlist

log = get_logger(__name__)

# Our timeframe label -> Alpaca TimeFrame.
TIMEFRAMES: dict[str, TimeFrame] = {
    "1H": TimeFrame(1, TimeFrameUnit.Hour),
    "4H": TimeFrame(4, TimeFrameUnit.Hour),
    "1D": TimeFrame(1, TimeFrameUnit.Day),
}

# How far back to backfill on the first run per timeframe (enough history for
# 55-EMA / ADX / volume-profile warmup with margin).
BACKFILL_DAYS: dict[str, int] = {"1H": 45, "4H": 180, "1D": 400}

_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0  # seconds: 2, 4, 8 ...


def _placeholder(v: str | None) -> bool:
    return not v or v == "REPLACE_ME"


def get_data_client() -> CryptoHistoricalDataClient:
    """Crypto historical client. Uses keys if real, else the keyless public client."""
    key = config_store.get("ALPACA_API_KEY", None)
    secret = config_store.get("ALPACA_SECRET", None)
    if _placeholder(key) or _placeholder(secret):
        log.info("Alpaca keys absent/placeholder — using keyless public crypto data client.")
        return CryptoHistoricalDataClient()
    return CryptoHistoricalDataClient(api_key=key, secret_key=secret)


def _to_naive_utc(ts: datetime) -> datetime:
    """Normalize a (possibly tz-aware) timestamp to naive UTC for DATETIME2."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def latest_ts(session, symbol: str, timeframe: str) -> datetime | None:
    """Most recent stored bar timestamp for (symbol, timeframe), or None."""
    return session.scalar(
        select(func.max(MarketBar.ts)).where(
            and_(MarketBar.symbol == symbol, MarketBar.timeframe == timeframe)
        )
    )


def _fetch_with_retry(client, request: CryptoBarsRequest):
    """Call Alpaca with bounded retries + exponential backoff."""
    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return client.get_crypto_bars(request)
        except Exception as e:  # noqa: BLE001 — Alpaca raises APIError/network errors
            last_err = e
            if attempt == _MAX_RETRIES:
                break
            sleep = _BACKOFF_BASE ** attempt
            log.warning("Alpaca fetch failed (attempt %d/%d): %s — retrying in %.0fs",
                        attempt, _MAX_RETRIES, e, sleep)
            time.sleep(sleep)
    raise RuntimeError(f"Alpaca fetch failed after {_MAX_RETRIES} attempts: {last_err}")


def _upsert(session, symbol: str, timeframe: str, df) -> tuple[int, int]:
    """Upsert a bars DataFrame for one symbol/timeframe. Returns (inserted, updated)."""
    if df is None or df.empty:
        return (0, 0)

    # df is a multi-index (symbol, timestamp) frame; slice to this symbol.
    if symbol in df.index.get_level_values(0):
        sub = df.xs(symbol, level=0)
    else:
        sub = df  # single-symbol frame

    rows = {}  # naive-utc ts -> ohlcv dict
    for ts, r in sub.iterrows():
        rows[_to_naive_utc(ts.to_pydatetime())] = dict(
            open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
            close=float(r["close"]), volume=float(r["volume"]),
        )
    if not rows:
        return (0, 0)

    existing = {
        b.ts: b
        for b in session.scalars(
            select(MarketBar).where(
                and_(
                    MarketBar.symbol == symbol,
                    MarketBar.timeframe == timeframe,
                    MarketBar.ts.in_(list(rows.keys())),
                )
            )
        )
    }

    inserted = updated = 0
    for ts, vals in rows.items():
        bar = existing.get(ts)
        if bar is None:
            session.add(MarketBar(symbol=symbol, timeframe=timeframe, ts=ts, **vals))
            inserted += 1
        else:
            bar.open, bar.high, bar.low = vals["open"], vals["high"], vals["low"]
            bar.close, bar.volume = vals["close"], vals["volume"]
            updated += 1
    session.commit()
    return (inserted, updated)


def ingest_symbol(client, session, symbol: str, timeframe: str) -> tuple[int, int]:
    """Fetch and upsert one symbol/timeframe incrementally. Returns (inserted, updated)."""
    last = latest_ts(session, symbol, timeframe)
    if last is not None:
        # Re-fetch from the last stored bar so a previously-partial bar is refreshed.
        start = last.replace(tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS[timeframe])

    request = CryptoBarsRequest(
        symbol_or_symbols=[symbol], timeframe=TIMEFRAMES[timeframe], start=start
    )
    barset = _fetch_with_retry(client, request)
    ins, upd = _upsert(session, symbol, timeframe, getattr(barset, "df", None))
    log.info("%-9s %-3s: +%d new, ~%d updated (since %s)",
             symbol, timeframe, ins, upd, start.date())
    return (ins, upd)


def ingest_all(timeframes: list[str] | None = None) -> dict:
    """Ingest all active watchlist symbols across the given timeframes."""
    timeframes = timeframes or list(TIMEFRAMES)
    client = get_data_client()
    totals = {"inserted": 0, "updated": 0, "symbols": 0}

    with get_session() as session:
        symbols = list(
            session.scalars(select(Watchlist.symbol).where(Watchlist.is_active == True)).all()  # noqa: E712 — SQL Server BIT needs '= 1', not 'IS 1'
        )
        for symbol in symbols:
            totals["symbols"] += 1
            for tf in timeframes:
                try:
                    ins, upd = ingest_symbol(client, session, symbol, tf)
                    totals["inserted"] += ins
                    totals["updated"] += upd
                except Exception as e:  # noqa: BLE001 — one bad symbol/tf must not abort the run
                    log.error("Ingest failed for %s %s: %s", symbol, tf, e)
    return totals


if __name__ == "__main__":
    summary = ingest_all()
    print(f"OK: ingested {summary['symbols']} symbol(s) -> "
          f"+{summary['inserted']} new bars, ~{summary['updated']} updated")
