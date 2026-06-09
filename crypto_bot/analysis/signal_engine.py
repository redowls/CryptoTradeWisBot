"""Signal combiner (Phase 5): fuse triple-MA + breakout + overextension into a
single BUY / HOLD / AVOID decision and tag signal_source (BREAKOUT | MA | BOTH).

Combine rule (summary.md §5.7): a BUY requires
    long-set trend filter bullish
    AND (confirmed breakout OR short-set bullish trigger)
    AND overextension filter passed (which includes R:R >= MIN_RR).

`combine_signal(...)` is a pure function (no DB) so it is unit-testable and
reusable by the backtester. `generate_signal(...)` loads the latest enriched
bars/levels from the DB and calls it. Writing to the `signals` table + the
0-100 confidence score is Phase 6.

CLI:  python -m crypto_bot.analysis.signal_engine
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import and_, select

from crypto_bot import config_store
from crypto_bot.analysis.breakout import check_breakout, overextension_filter
from crypto_bot.analysis.moving_avg import (
    evaluate_trend, evaluate_trigger, triple_ma_signal,
)
from crypto_bot.db import get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import Indicator, MarketBar, SRLevel, Watchlist

log = get_logger(__name__)

_ENRICH_COLS = [
    "ts", "open", "high", "low", "close", "volume",
    "ema8", "ema10", "ema20", "ema21", "ema34", "ema55",
    "atr", "rsi", "adx", "vol_avg",
]


@dataclass
class Cfg:
    trigger_tf: str = "1H"
    trend_tf: str = "4H"
    buffer_pct: float = 0.5
    volume_mult: float = 1.5
    rsi_overbought: float = 70.0
    atr_stop_mult: float = 2.5
    min_rr: float = 2.0
    adx_min: float = 20.0
    breakout_mode: str = "aggressive"

    @classmethod
    def from_store(cls) -> "Cfg":
        g = config_store
        return cls(
            trigger_tf=g.get("TRIGGER_TF", "1H"),
            trend_tf=g.get("TREND_TF", "4H"),
            buffer_pct=g.get_float("BREAKOUT_BUFFER_PCT", 0.5),
            volume_mult=g.get_float("VOLUME_MULT", 1.5),
            rsi_overbought=g.get_float("RSI_OVERBOUGHT", 70.0),
            atr_stop_mult=g.get_float("ATR_STOP_MULT", 2.5),
            min_rr=g.get_float("MIN_RR", 2.0),
            adx_min=g.get_float("ADX_MIN", 20.0),
            breakout_mode=g.get("BREAKOUT_MODE", "aggressive"),
        )


@dataclass
class SignalDecision:
    symbol: str
    ts: object
    signal: str            # BUY | HOLD | AVOID
    signal_source: str     # BREAKOUT | MA | BOTH
    entry_price: float
    stop_price: float
    target_price: float | None
    rr_ratio: float | None
    reason: str
    ma: object = None
    breakout: object = None
    overext: object = None
    indicators: dict = field(default_factory=dict)


def _source(breakout_fired: bool, ma_trigger: bool) -> str:
    if breakout_fired and ma_trigger:
        return "BOTH"
    if breakout_fired:
        return "BREAKOUT"
    return "MA"  # MA-trigger-only, or baseline framework when nothing fired


def combine_signal(
    *, symbol: str, ts, open_: float, high: float, low: float, close: float,
    volume: float, prev_close: float,
    atr: float, rsi: float, adx: float, vol_avg: float,
    ema8: float, ema10: float, ema20: float, prev_short_stacked: bool | None,
    trend_ema21: float, trend_ema34: float, trend_ema55: float, trend_close: float,
    resistances: list[float], recent_closes: list[float], recent_lows: list[float],
    cfg: Cfg,
) -> SignalDecision:
    """Pure decision logic. All indicator values are point-in-time scalars."""
    trend = evaluate_trend(trend_ema21, trend_ema34, trend_ema55, trend_close)
    trigger = evaluate_trigger(ema8, ema10, ema20, close, prev_short_stacked)
    ma = triple_ma_signal(trend, trigger, adx, cfg.adx_min)

    bo = check_breakout(
        prev_close=prev_close, open_=open_, high=high, low=low, close=close,
        atr=atr, volume=volume, vol_avg=vol_avg, resistance_levels=resistances,
        buffer_pct=cfg.buffer_pct, volume_mult=cfg.volume_mult, mode=cfg.breakout_mode,
        recent_closes=recent_closes, recent_lows=recent_lows,
    )

    # Stop: ATR-based, never inside the broken range (push below the broken level).
    stop = close - atr * cfg.atr_stop_mult
    if bo.level is not None:
        stop = min(stop, bo.level - bo.buffer)

    # Target: next resistance beyond the broken level (breakout) or nearest above close.
    if bo.level is not None:
        above = sorted(p for p in resistances if p > bo.level + bo.buffer)
    else:
        above = sorted(p for p in resistances if p > close)
    target = above[0] if above else None

    ovx = overextension_filter(
        close=close, level=bo.level, atr=atr, rsi=rsi, ref_ema=ema20,
        next_resistance=target, stop=stop, min_rr=cfg.min_rr,
        rsi_overbought=cfg.rsi_overbought,
    )

    breakout_fired = bo.is_breakout
    ma_trigger = trigger.bullish
    buy = trend.bullish and (breakout_fired or ma_trigger) and ovx.passed

    if buy:
        signal, reason = "BUY", "trend+entry+filters ok"
    elif not trend.bullish:
        signal, reason = "AVOID", "trend filter not bullish"
    elif not (breakout_fired or ma_trigger):
        signal, reason = "HOLD", "trend ok, no breakout/MA trigger"
    else:
        signal, reason = "HOLD", "blocked by overextension: " + ",".join(ovx.reasons)

    return SignalDecision(
        symbol=symbol, ts=ts, signal=signal,
        signal_source=_source(breakout_fired, ma_trigger),
        entry_price=round(close, 8), stop_price=round(stop, 8),
        target_price=round(target, 8) if target is not None else None,
        rr_ratio=ovx.rr_ratio, reason=reason, ma=ma, breakout=bo, overext=ovx,
        indicators=dict(close=close, atr=atr, rsi=rsi, adx=adx, vol_avg=vol_avg,
                        ema8=ema8, ema10=ema10, ema20=ema20),
    )


# --- DB loading -------------------------------------------------------------
def load_enriched(session, symbol: str, timeframe: str, limit: int = 12) -> pd.DataFrame:
    """Last `limit` bars joined with their cached indicators, ascending by ts."""
    j = MarketBar.__table__.join(
        Indicator.__table__,
        and_(MarketBar.symbol == Indicator.symbol,
             MarketBar.timeframe == Indicator.timeframe,
             MarketBar.ts == Indicator.ts),
    )
    rows = session.execute(
        select(
            MarketBar.ts, MarketBar.open, MarketBar.high, MarketBar.low,
            MarketBar.close, MarketBar.volume,
            Indicator.ema8, Indicator.ema10, Indicator.ema20,
            Indicator.ema21, Indicator.ema34, Indicator.ema55,
            Indicator.atr, Indicator.rsi, Indicator.adx, Indicator.vol_avg,
        ).select_from(j)
        .where(and_(MarketBar.symbol == symbol, MarketBar.timeframe == timeframe))
        .order_by(MarketBar.ts.desc()).limit(limit)
    ).all()
    df = pd.DataFrame(rows, columns=_ENRICH_COLS)
    if not df.empty:
        df = df.iloc[::-1].reset_index(drop=True)  # back to ascending
        for c in _ENRICH_COLS[1:]:
            df[c] = df[c].astype("float64")
    return df


def active_resistances(session, symbol: str, timeframe: str) -> list[float]:
    rows = session.scalars(
        select(SRLevel.level_price).where(and_(
            SRLevel.symbol == symbol, SRLevel.timeframe == timeframe,
            SRLevel.level_type == "resistance", SRLevel.is_active == True,  # noqa: E712
        ))
    ).all()
    return [float(p) for p in rows]


def generate_signal(session, symbol: str, cfg: Cfg | None = None) -> SignalDecision | None:
    cfg = cfg or Cfg.from_store()
    trig = load_enriched(session, symbol, cfg.trigger_tf, limit=12)
    trend = load_enriched(session, symbol, cfg.trend_tf, limit=2)
    if len(trig) < 2 or trend.empty:
        log.warning("Insufficient enriched data for %s — skipping signal.", symbol)
        return None

    t, t_prev = trig.iloc[-1], trig.iloc[-2]
    tr = trend.iloc[-1]
    prev_short_stacked = bool(t_prev.ema8 > t_prev.ema10 > t_prev.ema20)
    resistances = active_resistances(session, symbol, cfg.trigger_tf)

    return combine_signal(
        symbol=symbol, ts=t.ts, open_=t.open, high=t.high, low=t.low, close=t.close,
        volume=t.volume, prev_close=t_prev.close,
        atr=t.atr, rsi=t.rsi, adx=t.adx, vol_avg=t.vol_avg,
        ema8=t.ema8, ema10=t.ema10, ema20=t.ema20, prev_short_stacked=prev_short_stacked,
        trend_ema21=tr.ema21, trend_ema34=tr.ema34, trend_ema55=tr.ema55, trend_close=tr.close,
        resistances=resistances,
        recent_closes=list(trig["close"].iloc[:-1]), recent_lows=list(trig["low"].iloc[:-1]),
        cfg=cfg,
    )


def generate_all() -> list[SignalDecision]:
    cfg = Cfg.from_store()
    out = []
    with get_session() as session:
        symbols = list(
            session.scalars(select(Watchlist.symbol).where(Watchlist.is_active == True)).all()  # noqa: E712
        )
        for symbol in symbols:
            d = generate_signal(session, symbol, cfg)
            if d:
                out.append(d)
    return out


if __name__ == "__main__":
    for d in generate_all():
        rr = f"{d.rr_ratio:.2f}" if d.rr_ratio is not None else "n/a"
        tgt = f"{d.target_price:.2f}" if d.target_price is not None else "n/a"
        print(f"{d.symbol:9} {d.signal:6} src={d.signal_source:8} "
              f"entry={d.entry_price:.2f} stop={d.stop_price:.2f} target={tgt} "
              f"RR={rr}  [{d.reason}]")
