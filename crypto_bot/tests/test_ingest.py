"""Phase 2 (DB-free): timeframe mapping and timestamp normalization."""

from datetime import datetime, timezone, timedelta

from alpaca.data.timeframe import TimeFrameUnit

from crypto_bot.data import ingest


def test_timeframe_map_complete():
    assert set(ingest.TIMEFRAMES) == {"1H", "4H", "1D"}
    assert ingest.TIMEFRAMES["4H"].amount_value == 4
    assert ingest.TIMEFRAMES["4H"].unit_value == TimeFrameUnit.Hour
    assert ingest.TIMEFRAMES["1D"].unit_value == TimeFrameUnit.Day


def test_backfill_days_cover_all_timeframes():
    assert set(ingest.BACKFILL_DAYS) >= set(ingest.TIMEFRAMES)


def test_to_naive_utc_strips_tz_and_converts():
    # 09:30 in UTC+2 -> 07:30 naive UTC
    aware = datetime(2026, 6, 9, 9, 30, tzinfo=timezone(timedelta(hours=2)))
    out = ingest._to_naive_utc(aware)
    assert out.tzinfo is None
    assert out == datetime(2026, 6, 9, 7, 30)


def test_to_naive_utc_passthrough_for_naive():
    naive = datetime(2026, 6, 9, 7, 30)
    assert ingest._to_naive_utc(naive) == naive


def test_placeholder_detection():
    assert ingest._placeholder(None) is True
    assert ingest._placeholder("REPLACE_ME") is True
    assert ingest._placeholder("realkey123") is False
