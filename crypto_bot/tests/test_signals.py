"""Phase 5 (DB-free): triple-MA, breakout, overextension, and combined decision."""

from crypto_bot.analysis.moving_avg import (
    evaluate_trend, evaluate_trigger, triple_ma_signal,
)
from crypto_bot.analysis.breakout import check_breakout, overextension_filter
from crypto_bot.analysis.signal_engine import Cfg, combine_signal


CFG = Cfg()  # defaults


# --- Triple MA --------------------------------------------------------------
def test_trend_bullish_when_stacked_and_price_above():
    ts = evaluate_trend(ema21=110, ema34=105, ema55=100, close=112)
    assert ts.bullish and ts.stacked and ts.price_above and ts.gap_pct > 0


def test_trend_not_bullish_when_intertwined():
    assert not evaluate_trend(ema21=100, ema34=105, ema55=102, close=110).bullish


def test_trigger_fresh_cross_detected():
    tr = evaluate_trigger(ema8=110, ema10=108, ema20=105, close=111, prev_short_stacked=False)
    assert tr.bullish and tr.fresh_cross


def test_ma_signal_buy_hold_avoid():
    bull_trend = evaluate_trend(110, 105, 100, 112)
    bull_trig = evaluate_trigger(110, 108, 105, 111, prev_short_stacked=False)
    assert triple_ma_signal(bull_trend, bull_trig, adx=30, adx_min=20).signal == "BUY"
    # Trend ok but weak ADX -> HOLD
    assert triple_ma_signal(bull_trend, bull_trig, adx=10, adx_min=20).signal == "HOLD"
    # Trend not bullish -> AVOID
    bear_trend = evaluate_trend(100, 105, 110, 95)
    assert triple_ma_signal(bear_trend, bull_trig, adx=30, adx_min=20).signal == "AVOID"


# --- Breakout ---------------------------------------------------------------
def test_breakout_confirmed_close_beyond_with_volume_and_body():
    r = check_breakout(
        prev_close=99.0, open_=99.2, high=103.0, low=99.0, close=102.8,
        atr=1.0, volume=300.0, vol_avg=100.0, resistance_levels=[100.0],
        buffer_pct=0.5, volume_mult=1.5, mode="aggressive",
    )
    assert r.is_breakout and r.close_beyond and r.volume_ok and r.strong_body
    assert r.level == 100.0


def test_wick_through_level_is_not_a_breakout():
    # High pierces the level but the close is back below it -> no breakout.
    r = check_breakout(
        prev_close=99.0, open_=99.0, high=103.0, low=98.5, close=99.5,
        atr=1.0, volume=300.0, vol_avg=100.0, resistance_levels=[100.0],
        buffer_pct=0.5, volume_mult=1.5, mode="aggressive",
    )
    assert not r.is_breakout and not r.close_beyond


def test_breakout_needs_participation():
    # Close beyond (buffer = max(0.5%, 1xATR) = 2) + strong body, but no volume
    # and no ATR expansion (range 1.7 < ATR 2) -> rejected.
    r = check_breakout(
        prev_close=99.0, open_=101.2, high=102.7, low=101.0, close=102.5,
        atr=2.0, volume=50.0, vol_avg=100.0, resistance_levels=[100.0],
        buffer_pct=0.5, volume_mult=1.5, mode="aggressive",
    )
    assert r.close_beyond and not r.volume_ok and not r.atr_expansion
    assert not r.is_breakout


# --- Overextension ----------------------------------------------------------
def test_overextension_passes_with_good_rr():
    r = overextension_filter(
        close=101.0, level=100.0, atr=1.0, rsi=55.0, ref_ema=99.0,
        next_resistance=110.0, stop=97.0, min_rr=2.0, rsi_overbought=70.0,
    )
    assert r.passed and r.rr_ratio >= 2.0 and not r.reasons


def test_overextension_fails_on_low_rr():
    # Target too close relative to risk -> R:R below min, decisive fail.
    r = overextension_filter(
        close=109.0, level=100.0, atr=1.0, rsi=55.0, ref_ema=108.5,
        next_resistance=110.0, stop=99.0, min_rr=2.0, rsi_overbought=70.0,
    )
    assert not r.passed and "rr_below_min" in r.reasons


def test_overextension_fails_on_overbought_rsi():
    r = overextension_filter(
        close=101.0, level=100.0, atr=1.0, rsi=80.0, ref_ema=100.5,
        next_resistance=120.0, stop=97.0, min_rr=2.0, rsi_overbought=70.0,
    )
    assert not r.passed and "rsi_overbought" in r.reasons


# --- Combined decision ------------------------------------------------------
def _uptrend_kwargs(**over):
    """A clean confirmed-uptrend breakout setup."""
    # Buffer = max(0.5%*100, 1xATR=1) = 1, so close must exceed 101; and the
    # entry must stay within CHASE_ATR_MULT (1.5 ATR) of the level (not chasing).
    base = dict(
        symbol="BTC/USD", ts="2026-06-09T00:00:00",
        open_=100.2, high=101.6, low=100.1, close=101.4, volume=300.0, prev_close=100.0,
        atr=1.0, rsi=58.0, adx=30.0, vol_avg=100.0,
        ema8=101.0, ema10=100.5, ema20=100.0, prev_short_stacked=False,
        trend_ema21=100.0, trend_ema34=98.0, trend_ema55=96.0, trend_close=101.4,
        resistances=[100.0, 115.0], recent_closes=[97, 98, 98.5, 99], recent_lows=[96, 97, 97.5, 98],
        cfg=CFG,
    )
    base.update(over)
    return base


def test_combine_buy_in_confirmed_uptrend_tags_both():
    d = combine_signal(**_uptrend_kwargs())
    assert d.signal == "BUY"
    # Both a confirmed breakout and the short-set trigger fired.
    assert d.signal_source == "BOTH"
    assert d.rr_ratio is not None and d.rr_ratio >= CFG.min_rr
    assert d.stop_price < d.entry_price < d.target_price


def test_combine_avoid_in_downtrend():
    d = combine_signal(**_uptrend_kwargs(
        trend_ema21=96.0, trend_ema34=98.0, trend_ema55=100.0,  # bearishly stacked
    ))
    assert d.signal == "AVOID"


def test_combine_ma_only_source_when_no_breakout():
    # No resistance crossed -> no breakout; trend + trigger still bullish -> BUY via MA.
    d = combine_signal(**_uptrend_kwargs(
        resistances=[130.0],  # far above, nothing crossed
        prev_close=102.0,
    ))
    assert d.signal == "BUY"
    assert d.signal_source == "MA"


def test_combine_hold_when_trend_ok_but_overextended():
    # Trend + trigger bullish, but RSI overbought -> blocked -> HOLD (not BUY).
    d = combine_signal(**_uptrend_kwargs(rsi=85.0))
    assert d.signal == "HOLD"
    assert "rsi_overbought" in d.overext.reasons
