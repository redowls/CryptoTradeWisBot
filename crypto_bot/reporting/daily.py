"""Daily summary & P&L (Phase 11, summary.md §6.8).

Aggregates the `trades` table into `daily_summary` for a UTC trade date:
  - one row per symbol that had activity (entry or exit) or an open position,
  - plus a portfolio-total row (symbol = NULL).
Counts buys (entries that day) and sells (exits that day), realized P&L + %
(return on capital deployed in the day's closed trades), unrealized P&L on open
positions (marked to the latest close), and win/loss counts. Upserts on the
unique (trade_date, symbol) key so re-running is idempotent.

Also writes an `account_snapshots` row (equity, cash, open positions, portfolio
heat, drawdown vs the running equity peak).

CLI:  python -m crypto_bot.reporting.daily
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select

from crypto_bot import config_store
from crypto_bot.db import get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import AccountSnapshot, DailySummary, MarketBar, Trade
from crypto_bot.risk.sizing import portfolio_heat_pct

log = get_logger(__name__)


@dataclass
class SymbolAgg:
    num_buys: int = 0
    num_sells: int = 0
    realized_pnl: float = 0.0
    cost_basis: float = 0.0       # sum(qty*entry) of the day's closed trades
    unrealized_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0

    @property
    def realized_pnl_pct(self) -> float:
        return round(self.realized_pnl / self.cost_basis * 100.0, 4) if self.cost_basis > 0 else 0.0


def _latest_close(session, symbol: str) -> float | None:
    c = session.scalar(
        select(MarketBar.close).where(MarketBar.symbol == symbol)
        .order_by(MarketBar.ts.desc()).limit(1)
    )
    return float(c) if c is not None else None


def aggregate(session, trade_date: date) -> dict[str | None, SymbolAgg]:
    """Build per-symbol aggregates for the date (key None = portfolio total)."""
    trades = list(session.scalars(select(Trade)).all())
    by_symbol: dict[str, SymbolAgg] = defaultdict(SymbolAgg)

    for t in trades:
        entry_d = t.entry_time.date() if t.entry_time else None
        exit_d = t.exit_time.date() if t.exit_time else None
        agg = by_symbol[t.symbol]
        if t.side == "BUY" and entry_d == trade_date:
            agg.num_buys += 1
        if t.status == "CLOSED" and exit_d == trade_date:
            agg.num_sells += 1
            pnl = float(t.realized_pnl) if t.realized_pnl is not None else 0.0
            agg.realized_pnl += pnl
            if t.entry_price is not None and t.qty is not None:
                agg.cost_basis += float(t.qty) * float(t.entry_price)
            if pnl > 0:
                agg.win_count += 1
            else:
                agg.loss_count += 1

    # Unrealized P&L on currently-open positions, marked to the latest close.
    for t in trades:
        if t.status == "OPEN" and t.entry_price is not None and t.qty is not None:
            px = _latest_close(session, t.symbol)
            if px is not None:
                by_symbol[t.symbol].unrealized_pnl += float(t.qty) * (px - float(t.entry_price))

    # Portfolio total.
    total = SymbolAgg()
    for agg in by_symbol.values():
        total.num_buys += agg.num_buys
        total.num_sells += agg.num_sells
        total.realized_pnl += agg.realized_pnl
        total.cost_basis += agg.cost_basis
        total.unrealized_pnl += agg.unrealized_pnl
        total.win_count += agg.win_count
        total.loss_count += agg.loss_count

    result: dict[str | None, SymbolAgg] = dict(by_symbol)
    result[None] = total
    return result


def _get_summary_row(session, trade_date: date, symbol: str | None) -> DailySummary | None:
    stmt = select(DailySummary).where(DailySummary.trade_date == trade_date)
    stmt = stmt.where(DailySummary.symbol.is_(None) if symbol is None
                      else DailySummary.symbol == symbol)
    return session.scalar(stmt)


def upsert_summary(session, trade_date: date, symbol: str | None, agg: SymbolAgg) -> DailySummary:
    row = _get_summary_row(session, trade_date, symbol)
    if row is None:
        row = DailySummary(trade_date=trade_date, symbol=symbol)
        session.add(row)
    row.num_buys = agg.num_buys
    row.num_sells = agg.num_sells
    row.realized_pnl = round(agg.realized_pnl, 8)
    row.realized_pnl_pct = agg.realized_pnl_pct
    row.unrealized_pnl = round(agg.unrealized_pnl, 8)
    row.win_count = agg.win_count
    row.loss_count = agg.loss_count
    session.commit()
    return row


def _equity_and_cash(session) -> tuple[float, float]:
    """Equity/cash from Alpaca if reachable, else STARTING_EQUITY + realized P&L."""
    starting = config_store.get_float("STARTING_EQUITY", 10000.0)
    realized = sum(float(t.realized_pnl) for t in session.scalars(
        select(Trade).where(Trade.status == "CLOSED")).all() if t.realized_pnl is not None)
    fallback = starting + realized
    try:
        from crypto_bot.execution.broker import get_account
        acct = get_account()
        return acct["equity"], acct["cash"]
    except Exception as e:  # noqa: BLE001 — reporting must never crash on broker/network
        log.warning("Account fetch failed, using STARTING_EQUITY+realized: %s", e)
        return fallback, fallback


def write_account_snapshot(session, equity: float, cash: float) -> AccountSnapshot:
    open_rows = list(session.scalars(select(Trade).where(Trade.status == "OPEN")).all())
    heat = portfolio_heat_pct(open_rows, equity)
    prior_peak = session.scalar(select(AccountSnapshot.equity)
                                .order_by(AccountSnapshot.equity.desc()).limit(1))
    peak = max(float(prior_peak) if prior_peak is not None else equity, equity)
    drawdown = round((peak - equity) / peak * 100.0, 4) if peak > 0 else 0.0
    snap = AccountSnapshot(
        equity=round(equity, 2), cash=round(cash, 2), open_positions=len(open_rows),
        portfolio_heat_pct=heat, drawdown_pct=drawdown,
    )
    session.add(snap)
    session.commit()
    return snap


def generate_daily_summary(session, trade_date: date | None = None) -> dict:
    """Build daily_summary rows + an account snapshot for the date (default: today UTC)."""
    trade_date = trade_date or datetime.now(timezone.utc).date()
    aggs = aggregate(session, trade_date)
    rows = 0
    for symbol, agg in aggs.items():
        # Skip empty per-symbol rows (no activity, no open exposure); always write total.
        if symbol is not None and agg.num_buys == 0 and agg.num_sells == 0 and agg.unrealized_pnl == 0:
            continue
        upsert_summary(session, trade_date, symbol, agg)
        rows += 1
    equity, cash = _equity_and_cash(session)
    snap = write_account_snapshot(session, equity, cash)
    total = aggs[None]
    log.info("Daily summary %s: %d rows; buys=%d sells=%d realized=%.2f (%.2f%%) equity=%.2f",
             trade_date, rows, total.num_buys, total.num_sells,
             total.realized_pnl, total.realized_pnl_pct, equity)
    return {"trade_date": str(trade_date), "rows_written": rows,
            "portfolio": {"num_buys": total.num_buys, "num_sells": total.num_sells,
                          "realized_pnl": round(total.realized_pnl, 2),
                          "realized_pnl_pct": total.realized_pnl_pct,
                          "unrealized_pnl": round(total.unrealized_pnl, 2)},
            "equity": round(equity, 2), "drawdown_pct": snap.drawdown_pct}


if __name__ == "__main__":
    with get_session() as s:
        print(generate_daily_summary(s))
