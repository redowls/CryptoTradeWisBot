"""Phase 11 (DB-free): daily P&L aggregation math."""

from crypto_bot.reporting.daily import SymbolAgg


def test_realized_pnl_pct():
    a = SymbolAgg(realized_pnl=50.0, cost_basis=1000.0)
    assert a.realized_pnl_pct == 5.0


def test_realized_pnl_pct_negative():
    a = SymbolAgg(realized_pnl=-30.0, cost_basis=600.0)
    assert a.realized_pnl_pct == -5.0


def test_realized_pnl_pct_zero_cost_basis():
    assert SymbolAgg().realized_pnl_pct == 0.0
    assert SymbolAgg(realized_pnl=10.0, cost_basis=0.0).realized_pnl_pct == 0.0


def test_defaults_zeroed():
    a = SymbolAgg()
    assert (a.num_buys, a.num_sells, a.win_count, a.loss_count) == (0, 0, 0, 0)
    assert a.realized_pnl == 0.0 and a.unrealized_pnl == 0.0
