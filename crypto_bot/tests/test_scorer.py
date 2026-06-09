"""Phase 6 (DB-free): confidence scoring breakdown, caps, and gating."""

from crypto_bot.analysis.scorer import MAX_POINTS, score_decision
from crypto_bot.analysis.signal_engine import Cfg, combine_signal

CFG = Cfg()


def _kwargs(**over):
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


def test_breakdown_sums_to_total():
    d = combine_signal(**_kwargs())
    score, b = score_decision(d, CFG)
    assert abs(sum(b[k] for k in MAX_POINTS) - b["total"]) < 1e-6
    assert abs(b["total"] - score) < 1e-6


def test_each_factor_within_cap():
    for over in ({}, dict(rsi=85.0), dict(adx=10.0), dict(volume=1e9),
                 dict(trend_ema21=96.0, trend_ema34=98.0, trend_ema55=100.0)):
        d = combine_signal(**_kwargs(**over))
        _, b = score_decision(d, CFG)
        for k, cap in MAX_POINTS.items():
            assert 0.0 <= b[k] <= cap, f"{k}={b[k]} out of [0,{cap}]"


def test_score_in_range():
    d = combine_signal(**_kwargs())
    score, _ = score_decision(d, CFG)
    assert 0.0 <= score <= 100.0


def test_confirmed_uptrend_scores_high():
    d = combine_signal(**_kwargs())
    score, b = score_decision(d, CFG)
    assert d.signal == "BUY"
    assert score >= 70.0, f"expected prime setup >=70, got {score} ({b})"


def test_downtrend_scores_low():
    d = combine_signal(**_kwargs(
        trend_ema21=96.0, trend_ema34=98.0, trend_ema55=100.0,  # bearish stack
        ema8=100.0, ema10=100.5, ema20=101.0,                    # short set bearish
    ))
    score, b = score_decision(d, CFG)
    assert d.signal == "AVOID"
    assert score < 70.0
    assert b["trend"] == 0.0  # no trend points when not stacked


def test_overbought_momentum_penalized():
    healthy, _ = score_decision(combine_signal(**_kwargs(rsi=60.0)), CFG)
    over, _ = score_decision(combine_signal(**_kwargs(rsi=85.0)), CFG)
    assert over < healthy  # RSI 85 should score below RSI 60
