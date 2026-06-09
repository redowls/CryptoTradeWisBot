"""Risk & position sizing (Phase 7, summary.md §5.9).

Van Tharp risk-based sizing:
    qty = (equity x risk_pct) / (entry - stop)
sized off *current* equity so it shrinks after losses and grows after wins.

Conviction scaling (capped at MAX_RISK_PCT):
  - tiered (default): score 70-79 -> 0.75% ; 80-89 -> 1.0% ; 90+ -> 2.0%
  - kelly: fractional Kelly (x0.25 normal, x0.5 for score>=90) using the trade's
    R:R as payoff b and KELLY_WIN_RATE as p (until the backtest supplies real stats)

Hard caps / guards (a breach rejects the trade):
  - MAX_CONCURRENT_POSITIONS
  - MAX_PORTFOLIO_HEAT_PCT (sum of open risk + this trade's risk)
  - drawdown brake: halve risk while drawdown > DRAWDOWN_BRAKE_PCT
  - correlation guard: reject if >= MAX_CORRELATED_POSITIONS open positions are
    correlated (|corr| > CORRELATION_THRESHOLD) with the candidate symbol
  - one position per symbol (no pyramiding here)

The math is in pure functions (testable); DB state (equity, open positions,
correlations) is read by `size_signal`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import and_, func, select

from crypto_bot import config_store
from crypto_bot.db import get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import AccountSnapshot, MarketBar, Trade

log = get_logger(__name__)


@dataclass
class RiskCfg:
    base_risk_pct: float = 1.0
    max_risk_pct: float = 2.0
    max_heat_pct: float = 6.0
    max_positions: int = 5
    sizing_mode: str = "tiered"
    drawdown_brake_pct: float = 10.0
    confidence_threshold: float = 70.0
    starting_equity: float = 10000.0
    kelly_win_rate: float = 0.5
    min_rr: float = 2.0
    correlation_threshold: float = 0.7
    max_correlated: int = 3

    @classmethod
    def from_store(cls) -> "RiskCfg":
        g = config_store
        return cls(
            base_risk_pct=g.get_float("BASE_RISK_PCT", 1.0),
            max_risk_pct=g.get_float("MAX_RISK_PCT", 2.0),
            max_heat_pct=g.get_float("MAX_PORTFOLIO_HEAT_PCT", 6.0),
            max_positions=g.get_int("MAX_CONCURRENT_POSITIONS", 5),
            sizing_mode=g.get("SIZING_MODE", "tiered"),
            drawdown_brake_pct=g.get_float("DRAWDOWN_BRAKE_PCT", 10.0),
            confidence_threshold=g.get_float("CONFIDENCE_THRESHOLD", 70.0),
            starting_equity=g.get_float("STARTING_EQUITY", 10000.0),
            kelly_win_rate=g.get_float("KELLY_WIN_RATE", 0.5),
            min_rr=g.get_float("MIN_RR", 2.0),
            correlation_threshold=g.get_float("CORRELATION_THRESHOLD", 0.7),
            max_correlated=g.get_int("MAX_CORRELATED_POSITIONS", 3),
        )


@dataclass
class SizingResult:
    approved: bool
    qty: float
    risk_pct: float
    risk_amount: float
    entry: float
    stop: float
    target: float | None
    position_value: float
    existing_heat_pct: float
    new_heat_pct: float
    equity: float
    reasons: list[str] = field(default_factory=list)  # rejection reasons (empty = approved)


# --- Pure math --------------------------------------------------------------
def tiered_risk_pct(score: float) -> float:
    if score >= 90:
        return 2.0
    if score >= 80:
        return 1.0
    if score >= 70:
        return 0.75
    return 0.0


def kelly_risk_pct(score: float, rr: float | None, win_rate: float, min_rr: float) -> float:
    """Fractional Kelly as a risk %; 0 if no positive edge."""
    b = rr if (rr and rr > 0) else min_rr
    p = win_rate
    q = 1.0 - p
    f_star = (b * p - q) / b
    if f_star <= 0:
        return 0.0
    fraction = 0.5 if score >= 90 else 0.25
    return f_star * fraction * 100.0


def conviction_risk_pct(score: float, rr: float | None, cfg: RiskCfg) -> float:
    """Risk % for the trade's conviction, capped at MAX_RISK_PCT."""
    if cfg.sizing_mode == "kelly":
        rp = kelly_risk_pct(score, rr, cfg.kelly_win_rate, cfg.min_rr)
    else:
        rp = tiered_risk_pct(score)
    return max(0.0, min(rp, cfg.max_risk_pct))


def apply_drawdown_brake(risk_pct: float, drawdown_pct: float, brake_pct: float) -> float:
    """Halve risk while in a drawdown beyond the brake threshold."""
    return risk_pct * 0.5 if drawdown_pct > brake_pct else risk_pct


def compute_qty(equity: float, risk_pct: float, entry: float, stop: float) -> float:
    risk_per_unit = entry - stop
    if risk_per_unit <= 0:
        return 0.0
    return round((equity * risk_pct / 100.0) / risk_per_unit, 8)


def check_caps(*, n_open: int, existing_heat_pct: float, risk_pct: float,
               correlated_count: int, already_open: bool, cfg: RiskCfg) -> list[str]:
    """Return the list of breached caps (empty = all clear)."""
    reasons = []
    if already_open:
        reasons.append("already_open")
    if n_open >= cfg.max_positions:
        reasons.append("max_positions")
    if existing_heat_pct + risk_pct > cfg.max_heat_pct + 1e-9:
        reasons.append("portfolio_heat")
    if correlated_count >= cfg.max_correlated:
        reasons.append("correlation_cap")
    return reasons


# --- DB state ---------------------------------------------------------------
def current_equity(session, cfg: RiskCfg) -> float:
    eq = session.scalar(select(AccountSnapshot.equity).order_by(AccountSnapshot.ts.desc()).limit(1))
    return float(eq) if eq is not None else cfg.starting_equity


def current_drawdown_pct(session) -> float:
    dd = session.scalar(select(AccountSnapshot.drawdown_pct).order_by(AccountSnapshot.ts.desc()).limit(1))
    return float(dd) if dd is not None else 0.0


def open_trades(session) -> list[Trade]:
    return list(session.scalars(select(Trade).where(Trade.status == "OPEN")).all())


def portfolio_heat_pct(open_positions: list[Trade], equity: float) -> float:
    if equity <= 0:
        return 0.0
    risk = 0.0
    for t in open_positions:
        if t.entry_price is not None and t.stop_price is not None and t.qty is not None:
            risk += float(t.qty) * max(0.0, float(t.entry_price) - float(t.stop_price))
    return round(risk / equity * 100.0, 4)


def _daily_returns(session, symbol: str, lookback: int = 90) -> pd.Series:
    rows = session.execute(
        select(MarketBar.ts, MarketBar.close)
        .where(and_(MarketBar.symbol == symbol, MarketBar.timeframe == "1D"))
        .order_by(MarketBar.ts.desc()).limit(lookback)
    ).all()
    if not rows:
        return pd.Series(dtype="float64")
    df = pd.DataFrame(rows, columns=["ts", "close"]).iloc[::-1]
    return df.set_index("ts")["close"].astype("float64").pct_change().dropna()


def correlated_count(session, symbol: str, open_positions: list[Trade], threshold: float) -> int:
    """Count open positions whose 1D returns correlate with `symbol` above threshold."""
    base = _daily_returns(session, symbol)
    if base.empty:
        return 0
    count = 0
    for t in open_positions:
        if t.symbol == symbol:
            continue
        other = _daily_returns(session, t.symbol)
        joined = pd.concat([base, other], axis=1, join="inner").dropna()
        if len(joined) >= 10:
            corr = joined.iloc[:, 0].corr(joined.iloc[:, 1])
            if pd.notna(corr) and abs(corr) > threshold:
                count += 1
    return count


# --- Orchestration ----------------------------------------------------------
def size_signal(session, decision, score: float, cfg: RiskCfg | None = None) -> SizingResult:
    cfg = cfg or RiskCfg.from_store()
    equity = current_equity(session, cfg)
    entry, stop, target = decision.entry_price, decision.stop_price, decision.target_price

    def _reject(reasons, risk_pct=0.0, qty=0.0, existing=0.0):
        return SizingResult(approved=False, qty=qty, risk_pct=risk_pct, risk_amount=0.0,
                            entry=entry, stop=stop, target=target, position_value=0.0,
                            existing_heat_pct=existing, new_heat_pct=existing, equity=equity,
                            reasons=reasons)

    if not (decision.signal == "BUY" and score >= cfg.confidence_threshold):
        return _reject(["not_actionable"])
    if entry is None or stop is None or entry - stop <= 0:
        return _reject(["invalid_stop"])

    risk_pct = conviction_risk_pct(score, decision.rr_ratio, cfg)
    risk_pct = apply_drawdown_brake(risk_pct, current_drawdown_pct(session), cfg.drawdown_brake_pct)
    if risk_pct <= 0:
        return _reject(["no_risk_allocation"])

    opens = open_trades(session)
    existing_heat = portfolio_heat_pct(opens, equity)
    already_open = any(t.symbol == decision.symbol for t in opens)
    corr_count = correlated_count(session, decision.symbol, opens, cfg.correlation_threshold)

    reasons = check_caps(n_open=len(opens), existing_heat_pct=existing_heat, risk_pct=risk_pct,
                         correlated_count=corr_count, already_open=already_open, cfg=cfg)

    qty = compute_qty(equity, risk_pct, entry, stop)
    risk_amount = round(equity * risk_pct / 100.0, 2)
    return SizingResult(
        approved=not reasons, qty=qty, risk_pct=round(risk_pct, 4), risk_amount=risk_amount,
        entry=entry, stop=stop, target=target, position_value=round(qty * entry, 2),
        existing_heat_pct=existing_heat, new_heat_pct=round(existing_heat + risk_pct, 4),
        equity=equity, reasons=reasons,
    )


if __name__ == "__main__":
    from crypto_bot.analysis.scorer import score_decision
    from crypto_bot.analysis.signal_engine import Cfg, generate_signal
    from crypto_bot.models import Watchlist

    sig_cfg, risk_cfg = Cfg.from_store(), RiskCfg.from_store()
    with get_session() as session:
        print(f"equity={current_equity(session, risk_cfg):.2f} "
              f"drawdown={current_drawdown_pct(session):.2f}% "
              f"open={len(open_trades(session))}")
        symbols = list(session.scalars(select(Watchlist.symbol).where(Watchlist.is_active == True)).all())  # noqa: E712
        for symbol in symbols:
            d = generate_signal(session, symbol, sig_cfg)
            if not d:
                continue
            score, _ = score_decision(d, sig_cfg)
            r = size_signal(session, d, score, risk_cfg)
            status = "APPROVED" if r.approved else "REJECTED:" + ",".join(r.reasons)
            print(f"{symbol:9} {d.signal:6} score={score:5.1f} risk%={r.risk_pct:>4} "
                  f"qty={r.qty:.6f} value={r.position_value:.2f}  {status}")
