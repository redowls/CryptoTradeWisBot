"""Triple-MA system (Phase 5, summary.md §5.6).

Two roles:
  - Long set (21/34/55) on the TREND timeframe = trend filter / gate. Only allow
    longs when it is bullishly stacked (fast > mid > slow) and price is above it.
  - Short set (8/10/20) on the TRIGGER timeframe = entry trigger. Fires only in
    the direction the long set permits; a fresh bullish stack is the strongest.

ADX (on the trigger timeframe) must exceed ADX_MIN to avoid range-bound chop.
Output: BUY / HOLD / AVOID, plus the components the scorer (Phase 6) reuses.

These are pure functions over indicator values so they are unit-testable and can
be reused by the backtester (Phase 8).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TrendState:
    bullish: bool          # long set stacked bullish AND price above it
    stacked: bool          # 21 > 34 > 55
    price_above: bool      # close above all three
    gap_pct: float         # (ema21 - ema55) / close — wider = stronger momentum


@dataclass
class TriggerState:
    bullish: bool          # short set stacked bullish AND close > ema8
    stacked: bool          # 8 > 10 > 20
    price_above: bool      # close > ema8
    fresh_cross: bool      # short set was not stacked on the prior bar, is now


@dataclass
class MAResult:
    signal: str            # BUY | HOLD | AVOID
    trend: TrendState
    trigger: TriggerState
    adx: float
    adx_ok: bool
    details: dict = field(default_factory=dict)


def evaluate_trend(ema21: float, ema34: float, ema55: float, close: float) -> TrendState:
    stacked = ema21 > ema34 > ema55
    price_above = close > ema21 and close > ema34 and close > ema55
    gap_pct = (ema21 - ema55) / close * 100.0 if close else 0.0
    return TrendState(bullish=stacked and price_above, stacked=stacked,
                      price_above=price_above, gap_pct=round(gap_pct, 4))


def evaluate_trigger(ema8: float, ema10: float, ema20: float, close: float,
                     prev_short_stacked: bool | None = None) -> TriggerState:
    stacked = ema8 > ema10 > ema20
    price_above = close > ema8
    fresh = bool(stacked and prev_short_stacked is False)
    return TriggerState(bullish=stacked and price_above, stacked=stacked,
                        price_above=price_above, fresh_cross=fresh)


def triple_ma_signal(trend: TrendState, trigger: TriggerState,
                     adx: float, adx_min: float) -> MAResult:
    """Combine the two MA sets + ADX gate into BUY / HOLD / AVOID."""
    adx_ok = adx is not None and adx > adx_min
    if not trend.bullish:
        signal = "AVOID"           # intertwined or bearishly stacked -> stand aside
    elif not adx_ok:
        signal = "HOLD"            # trend aligned but chop (weak ADX)
    elif trigger.bullish:
        signal = "BUY"             # trend permits + short-set trigger fired
    else:
        signal = "HOLD"            # trend intact, no fresh trigger / price not above ema8
    return MAResult(signal=signal, trend=trend, trigger=trigger,
                    adx=round(adx, 4) if adx is not None else None, adx_ok=adx_ok)
