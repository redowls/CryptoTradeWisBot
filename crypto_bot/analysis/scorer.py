"""Confluence scorer (Phase 6, summary.md §5.8).

Turns a Phase-5 SignalDecision into a 0-100 confidence score from six
*independent* factors, and writes the signal (with the per-factor breakdown as
JSON) to the `signals` table.

    Factor                                   Max pts
    Trend alignment (21/34/55 stacked+above)   30
    Breakout quality                           20
    Volume confirmation                        15
    Short-set trigger (8/10/20)                15
    Momentum (RSI healthy 50-70, not >70)      10
    Volatility regime (ADX > min; ATR band)    10
    --------------------------------------------------
    Total                                     100

Trade only if score >= CONFIDENCE_THRESHOLD (default 70). Below-threshold
signals are still recorded (acted=0) for later analysis.

Note: the ATR-percentile refinement of the volatility factor (§5.11) is deferred;
ADX trend-strength stands in for it here, which is adequate for the gate.

CLI:  python -m crypto_bot.analysis.scorer
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import and_, select

from crypto_bot import config_store
from crypto_bot.analysis.signal_engine import Cfg, generate_signal
from crypto_bot.db import get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import Signal, Watchlist

log = get_logger(__name__)

MAX_POINTS = {
    "trend": 30, "breakout": 20, "volume": 15,
    "trigger": 15, "momentum": 10, "volatility": 10,
}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --- Per-factor scoring -----------------------------------------------------
def _score_trend(trend) -> float:
    if not trend.stacked:
        return 0.0
    if not trend.price_above:
        return 12.0  # aligned but price not yet above all three
    # Bullish: base 20, +10 scaled by stacking width (gap_pct, full at >=3%).
    return _clamp(20.0 + 10.0 * _clamp(trend.gap_pct / 3.0, 0.0, 1.0), 0, 30)


def _score_breakout(bo) -> float:
    if not bo.close_beyond:
        return 0.0
    if not bo.is_breakout:
        return 8.0  # closed beyond but unconfirmed (no participation / weak body)
    # Confirmed: base 14, +6 scaled by body strength.
    return _clamp(14.0 + 6.0 * _clamp(bo.body_frac, 0.0, 1.0), 0, 20)


def _score_volume(bo, volume_mult: float) -> float:
    pts = 15.0 * _clamp(bo.volume_ratio / volume_mult, 0.0, 1.0) if volume_mult else 0.0
    if bo.atr_expansion:  # crypto volume is fragmented — ATR expansion is a proxy
        pts = max(pts, 7.5)
    return _clamp(pts, 0, 15)


def _score_trigger(trigger) -> float:
    if not trigger.stacked:
        return 0.0
    if not trigger.price_above:
        return 6.0
    return 15.0 if trigger.fresh_cross else 10.0


def _score_momentum(rsi: float, rsi_overbought: float) -> float:
    if rsi is None:
        return 0.0
    if rsi > rsi_overbought:
        return _clamp(10.0 - (rsi - rsi_overbought), 0, 10)  # overbought penalty
    if rsi >= 50:
        return 10.0
    if rsi >= 40:
        return _clamp(10.0 * (rsi - 40) / 10.0, 0, 10)
    return 0.0


def _score_volatility(adx: float, adx_ok: bool, adx_min: float) -> float:
    if not adx_ok or adx is None:
        return 0.0
    return _clamp(6.0 + 4.0 * _clamp((adx - adx_min) / 20.0, 0.0, 1.0), 0, 10)


def score_decision(decision, cfg: Cfg | None = None) -> tuple[float, dict]:
    """Return (score 0-100, breakdown dict with per-factor pts + total)."""
    cfg = cfg or Cfg.from_store()
    trend = decision.ma.trend
    trigger = decision.ma.trigger
    bo = decision.breakout
    rsi = decision.indicators.get("rsi")

    breakdown = {
        "trend": round(float(_score_trend(trend)), 2),
        "breakout": round(float(_score_breakout(bo)), 2),
        "volume": round(float(_score_volume(bo, cfg.volume_mult)), 2),
        "trigger": round(float(_score_trigger(trigger)), 2),
        "momentum": round(float(_score_momentum(rsi, cfg.rsi_overbought)), 2),
        "volatility": round(float(_score_volatility(decision.ma.adx, decision.ma.adx_ok, cfg.adx_min)), 2),
    }
    total = round(float(sum(breakdown.values())), 2)
    breakdown["total"] = total
    return total, breakdown


# --- Persistence ------------------------------------------------------------
def _as_datetime(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(str(ts))


def persist_signal(session, decision, score: float, breakdown: dict, threshold: float) -> Signal:
    """Upsert a signal row on (symbol, ts). acted=1 only for a BUY at/above threshold."""
    ts = _as_datetime(decision.ts)
    acted = bool(decision.signal == "BUY" and score >= threshold)

    row = session.scalar(
        select(Signal).where(and_(Signal.symbol == decision.symbol, Signal.ts == ts))
    )
    if row is None:
        row = Signal(symbol=decision.symbol, ts=ts)
        session.add(row)

    row.signal = decision.signal
    row.signal_source = decision.signal_source
    row.confidence_score = score
    row.score_breakdown = json.dumps(breakdown)
    row.entry_price = decision.entry_price
    row.stop_price = decision.stop_price
    row.target_price = decision.target_price
    row.rr_ratio = decision.rr_ratio
    row.acted = acted
    row.notes = decision.reason[:500]
    session.commit()
    return row


def score_and_store_all() -> list[dict]:
    """Generate, score, and persist signals for all active watchlist symbols."""
    cfg = Cfg.from_store()
    threshold = config_store.get_float("CONFIDENCE_THRESHOLD", 70.0)
    results = []
    with get_session() as session:
        symbols = list(
            session.scalars(select(Watchlist.symbol).where(Watchlist.is_active == True)).all()  # noqa: E712
        )
        for symbol in symbols:
            decision = generate_signal(session, symbol, cfg)
            if decision is None:
                continue
            score, breakdown = score_decision(decision, cfg)
            row = persist_signal(session, decision, score, breakdown, threshold)
            results.append({
                "symbol": symbol, "signal": decision.signal, "source": decision.signal_source,
                "score": score, "acted": row.acted, "breakdown": breakdown,
            })
            log.info("%-9s %-6s score=%.1f acted=%s %s",
                     symbol, decision.signal, score, row.acted, breakdown)
    return results


if __name__ == "__main__":
    for r in score_and_store_all():
        b = r["breakdown"]
        parts = " ".join(f"{k}={b[k]}" for k in MAX_POINTS)
        print(f"{r['symbol']:9} {r['signal']:6} src={r['source']:8} "
              f"score={r['score']:5.1f} acted={int(r['acted'])}  [{parts}]")
