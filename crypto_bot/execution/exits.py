"""Exit management (Phase 10, summary.md §5.10).

A stateful, no-lookahead exit engine that manages an open long to completion:

  - hard stop-loss at the initial stop;
  - partial scale-outs: 50% at 1R -> move stop to breakeven; 25% at 2R; the final
    25% trails;
  - Chandelier trailing stop (highest_high - mult x ATR), ratchet-only;
  - structural take-profit: exit the remainder at the target (always >= 2R since
    sizing requires R:R >= 2);
  - time stop: exit if price hasn't moved ~1% in favor within TIME_STOP_BARS.

`manage_bar()` advances one bar and mutates a `PositionState`; it's pure and
unit-testable. `run_position()` replays a bar sequence until close. The DB side
(`manage_open_trades` / `close_trade_from_state`) closes the `trades` row with an
effective blended exit price and accurate realized P&L.

To avoid same-bar self-triggering, the trailing stop is checked against the level
set by *prior* bars, then ratcheted up after this bar's high is incorporated.

CLI:  python -m crypto_bot.execution.exits     # manage open trades vs latest bars
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import and_, select

from crypto_bot import config_store
from crypto_bot.analysis.signal_engine import load_enriched
from crypto_bot.db import get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import Trade

log = get_logger(__name__)

PARTIAL_1R_FRAC = 0.50   # scale out at 1R
PARTIAL_2R_FRAC = 0.25   # scale out at 2R (final 25% trails)
TIME_STOP_MIN_MOVE_PCT = 1.0


@dataclass
class ExitConfig:
    chandelier_mult: float = 3.0
    time_stop_bars: int = 24
    use_partials: bool = True

    @classmethod
    def from_store(cls) -> "ExitConfig":
        chand = config_store.get_list("CHANDELIER", "22,3.0", cast=float)
        return cls(
            chandelier_mult=chand[1] if len(chand) > 1 else 3.0,
            time_stop_bars=config_store.get_int("TIME_STOP_BARS", 24),
        )


@dataclass
class ExitAction:
    kind: str          # SCALE_OUT | EXIT | MOVE_STOP | HOLD
    qty: float
    price: float
    reason: str


@dataclass
class PositionState:
    entry: float
    initial_stop: float
    target: float | None
    qty: float                       # original quantity
    qty_remaining: float = None      # set in __post_init__
    current_stop: float = None
    highest_high: float = None
    stop_reason: str = "initial"     # initial | breakeven | chandelier
    scaled_1r: bool = False
    scaled_2r: bool = False
    bars_held: int = 0
    realized_pnl: float = 0.0        # gross (excl. fees), accumulated across exits
    closed: bool = False
    exit_reason: str | None = None

    def __post_init__(self):
        if self.qty_remaining is None:
            self.qty_remaining = self.qty
        if self.current_stop is None:
            self.current_stop = self.initial_stop
        if self.highest_high is None:
            self.highest_high = self.entry

    @property
    def risk_per_unit(self) -> float:
        return self.entry - self.initial_stop

    def r_price(self, r: float) -> float:
        return self.entry + r * self.risk_per_unit

    @property
    def avg_exit_price(self) -> float | None:
        """Effective blended exit once closed (entry + realized/qty)."""
        if self.qty <= 0:
            return None
        return self.entry + self.realized_pnl / self.qty


def _scale(state: PositionState, qty: float, price: float, reason: str) -> ExitAction:
    qty = min(qty, state.qty_remaining)
    state.qty_remaining -= qty
    state.realized_pnl += qty * (price - state.entry)
    if state.qty_remaining <= 1e-12:
        state.closed = True
        state.exit_reason = reason
    return ExitAction("SCALE_OUT", round(qty, 10), price, reason)


def _exit_all(state: PositionState, price: float, reason: str) -> ExitAction:
    qty = state.qty_remaining
    state.realized_pnl += qty * (price - state.entry)
    state.qty_remaining = 0.0
    state.closed = True
    state.exit_reason = reason
    return ExitAction("EXIT", round(qty, 10), price, reason)


def manage_bar(state: PositionState, *, high: float, low: float, close: float,
               atr: float, cfg: ExitConfig) -> list[ExitAction]:
    """Advance one bar; mutate state; return the actions taken."""
    if state.closed:
        return []
    state.bars_held += 1
    actions: list[ExitAction] = []

    # 1) Stop / trailing stop — checked against the level set by prior bars.
    if low <= state.current_stop:
        reason = {"initial": "stop_loss", "breakeven": "breakeven_stop",
                  "chandelier": "trailing_stop"}[state.stop_reason]
        actions.append(_exit_all(state, state.current_stop, reason))
        return actions

    # 2) Partial scale-outs at R multiples (use the bar high to detect the level).
    if cfg.use_partials:
        if not state.scaled_1r and high >= state.r_price(1):
            actions.append(_scale(state, state.qty * PARTIAL_1R_FRAC, state.r_price(1), "scale_1R"))
            state.scaled_1r = True
            if state.current_stop < state.entry:      # move stop to breakeven
                state.current_stop = state.entry
                state.stop_reason = "breakeven"
                actions.append(ExitAction("MOVE_STOP", 0.0, state.entry, "breakeven_after_1R"))
        if not state.closed and not state.scaled_2r and high >= state.r_price(2):
            actions.append(_scale(state, state.qty * PARTIAL_2R_FRAC, state.r_price(2), "scale_2R"))
            state.scaled_2r = True

    # 3) Structural take-profit: exit the remainder at the target (>= 2R).
    if not state.closed and state.target is not None and high >= state.target:
        actions.append(_exit_all(state, state.target, "target"))
        return actions

    # 4) Time stop.
    if not state.closed and state.bars_held >= cfg.time_stop_bars:
        if (close - state.entry) / state.entry * 100.0 < TIME_STOP_MIN_MOVE_PCT:
            actions.append(_exit_all(state, close, "time"))
            return actions

    # 5) Ratchet the Chandelier trail up for the next bar (after this bar's high).
    state.highest_high = max(state.highest_high, high)
    chand = state.highest_high - cfg.chandelier_mult * atr
    if chand > state.current_stop:
        state.current_stop = chand
        state.stop_reason = "chandelier"
        actions.append(ExitAction("MOVE_STOP", 0.0, round(chand, 8), "chandelier_trail"))

    if not actions:
        actions.append(ExitAction("HOLD", 0.0, close, "hold"))
    return actions


def run_position(state: PositionState, bars: list[dict], cfg: ExitConfig) -> list[tuple[int, list[ExitAction]]]:
    """Replay bars (each: high/low/close/atr) until the position closes."""
    log_out = []
    for i, b in enumerate(bars):
        acts = manage_bar(state, high=b["high"], low=b["low"], close=b["close"],
                          atr=b["atr"], cfg=cfg)
        log_out.append((i, acts))
        if state.closed:
            break
    return log_out


# --- DB integration ---------------------------------------------------------
def state_from_trade(trade: Trade) -> PositionState:
    return PositionState(
        entry=float(trade.entry_price), initial_stop=float(trade.stop_price),
        target=float(trade.target_price) if trade.target_price is not None else None,
        qty=float(trade.qty),
    )


def close_trade_from_state(session, trade: Trade, state: PositionState,
                           exit_time: datetime | None = None) -> Trade:
    """Write the blended exit + realized P&L to the trade row and mark CLOSED."""
    avg_exit = state.avg_exit_price
    trade.exit_price = round(avg_exit, 8) if avg_exit is not None else None
    trade.exit_time = exit_time or datetime.now(timezone.utc)
    trade.realized_pnl = round(state.realized_pnl, 8)
    trade.realized_pnl_pct = round(state.realized_pnl / (state.qty * state.entry) * 100.0, 4) \
        if state.qty and state.entry else None
    trade.status = "CLOSED"
    session.commit()
    return trade


def manage_open_trades(session, timeframe: str | None = None,
                       cfg: ExitConfig | None = None) -> dict:
    """For each filled OPEN trade, replay bars since entry and close if exited.

    Stateless: replays from entry each cycle (idempotent), so it recovers from
    restarts without persisting per-position exit state.
    """
    cfg = cfg or ExitConfig.from_store()
    timeframe = timeframe or config_store.get("TRIGGER_TF", "1H")
    open_rows = session.scalars(
        select(Trade).where(and_(
            Trade.status == "OPEN", Trade.entry_price.isnot(None),
            Trade.side == "BUY",
        ))
    ).all()

    closed = 0
    for trade in open_rows:
        enriched = load_enriched(session, trade.symbol, timeframe, limit=2000)
        if enriched.empty or trade.entry_time is None:
            continue
        entry_ts = pd.Timestamp(trade.entry_time).tz_localize(None)
        after = enriched[enriched["ts"] > entry_ts]
        bars = [{"high": float(r.high), "low": float(r.low), "close": float(r.close),
                 "atr": float(r.atr)} for r in after.itertuples()]
        if not bars:
            continue
        state = state_from_trade(trade)
        run_position(state, bars, cfg)
        if state.closed:
            close_trade_from_state(session, trade, state)
            closed += 1
            log.info("Closed trade #%d %s: exit=%s pnl=%.2f (%.2f%%)",
                     trade.trade_id, trade.symbol, state.exit_reason,
                     trade.realized_pnl, trade.realized_pnl_pct or 0.0)
    return {"open_evaluated": len(open_rows), "closed": closed}


if __name__ == "__main__":
    with get_session() as s:
        print("manage_open_trades:", manage_open_trades(s))
