"""Breakout confirmation + overextension filter (Phase 5, summary.md §5.4–5.5).

Breakout confirmation (don't trust a bare line cross):
  - a candle CLOSE beyond resistance + buffer = max(BREAKOUT_BUFFER_PCT%, 1xATR)
    (an intrabar wick through the level does not count),
  - participation: volume >= VOLUME_MULT x 20-bar avg, OR ATR-expansion proxy
    (this bar's range exceeds ATR) — crypto volume is fragmented,
  - a strong body (> 70% of range, or > 0.8 x ATR).
  Modes: `aggressive` (enter on the confirmed close) or `retest` (a prior break
  then a pullback that holds the level as support).

Overextension filter ("nice entry vs already overpriced") — reject if any of:
  - distance beyond the broken level > CHASE_ATR_MULT x ATR (chasing),
  - extension above the reference EMA > EXT_PCT_MAX,
  - RSI > RSI_OVERBOUGHT,
  - R:R to the next resistance < MIN_RR  (the decisive filter).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Defaults for knobs not in summary.md §7 (sensible, tune via backtest).
CHASE_ATR_MULT = 1.5     # max distance beyond the broken level, in ATR
EXT_PCT_MAX = 8.0        # max % extension above the reference EMA
BODY_RANGE_FRAC = 0.70   # strong-body: body > 70% of candle range
BODY_ATR_FRAC = 0.80     # ...or body > 0.8 x ATR
RETEST_LOOKBACK = 6      # bars to look back for a prior break (retest mode)


@dataclass
class BreakoutResult:
    is_breakout: bool
    mode: str
    level: float | None = None          # the resistance level broken
    buffer: float = 0.0
    close_beyond: bool = False
    volume_ratio: float = 0.0           # volume / vol_avg
    volume_ok: bool = False
    atr_expansion: bool = False
    participation: bool = False
    strong_body: bool = False
    body_frac: float = 0.0              # body / range
    retest_ok: bool | None = None
    details: dict = field(default_factory=dict)


@dataclass
class OverextensionResult:
    passed: bool
    rr_ratio: float | None
    distance_atr: float
    ext_pct: float
    rsi: float
    reasons: list[str] = field(default_factory=list)  # which checks failed


def _buffer(level: float, atr: float, buffer_pct: float) -> float:
    return max(buffer_pct / 100.0 * level, atr)


def check_breakout(
    *, prev_close: float, open_: float, high: float, low: float, close: float,
    atr: float, volume: float, vol_avg: float,
    resistance_levels: list[float], buffer_pct: float, volume_mult: float,
    mode: str = "aggressive",
    recent_closes: list[float] | None = None, recent_lows: list[float] | None = None,
) -> BreakoutResult:
    """Detect a confirmed long breakout of the nearest crossed resistance level."""
    candle_range = max(high - low, 1e-12)
    body = abs(close - open_)
    body_frac = body / candle_range
    strong_body = body_frac > BODY_RANGE_FRAC or (atr > 0 and body > BODY_ATR_FRAC * atr)
    volume_ratio = (volume / vol_avg) if vol_avg and vol_avg > 0 else 0.0
    volume_ok = volume_ratio >= volume_mult
    atr_expansion = candle_range > atr if atr > 0 else False
    participation = volume_ok or atr_expansion

    result = BreakoutResult(
        is_breakout=False, mode=mode, volume_ratio=round(volume_ratio, 4),
        volume_ok=volume_ok, atr_expansion=atr_expansion, participation=participation,
        strong_body=strong_body, body_frac=round(body_frac, 4),
    )

    # Find the nearest resistance freshly crossed this bar (close beyond + buffer,
    # while the previous close was at/below the buffered level).
    crossed = None
    for L in sorted(resistance_levels):
        buf = _buffer(L, atr, buffer_pct)
        if prev_close <= L + buf and close > L + buf:
            crossed = (L, buf)
            break
    if crossed is None:
        return result

    level, buf = crossed
    result.level, result.buffer, result.close_beyond = level, round(buf, 8), True

    if mode == "retest":
        # A prior break of `level` within lookback, then this bar pulled back to the
        # level (low within a buffer) and closed back above it with a bullish body.
        rc = recent_closes or []
        prior_break = any(c > level + buf for c in rc[-RETEST_LOOKBACK:])
        pulled_back = (low <= level + buf) and (close > level) and (close > open_)
        result.retest_ok = bool(prior_break and pulled_back)
        result.is_breakout = result.retest_ok and participation
    else:  # aggressive
        result.is_breakout = participation and strong_body

    return result


def overextension_filter(
    *, close: float, level: float | None, atr: float, rsi: float,
    ref_ema: float, next_resistance: float | None, stop: float,
    min_rr: float, rsi_overbought: float,
) -> OverextensionResult:
    """Reject overpriced/chasing entries. R:R below `min_rr` is the decisive fail."""
    reasons: list[str] = []

    distance_atr = ((close - level) / atr) if (level is not None and atr > 0) else 0.0
    if level is not None and distance_atr > CHASE_ATR_MULT:
        reasons.append("chasing")

    ext_pct = ((close - ref_ema) / ref_ema * 100.0) if ref_ema else 0.0
    if ext_pct > EXT_PCT_MAX:
        reasons.append("overextended_ema")

    if rsi is not None and rsi > rsi_overbought:
        reasons.append("rsi_overbought")

    risk = close - stop
    if next_resistance is not None and risk > 0:
        rr_ratio = (next_resistance - close) / risk
    else:
        rr_ratio = None
    if rr_ratio is None or rr_ratio < min_rr:
        reasons.append("rr_below_min")

    return OverextensionResult(
        passed=not reasons, rr_ratio=round(rr_ratio, 4) if rr_ratio is not None else None,
        distance_atr=round(distance_atr, 4), ext_pct=round(ext_pct, 4),
        rsi=round(rsi, 4) if rsi is not None else None, reasons=reasons,
    )
