"""Phase 7 (DB-free): risk sizing math, conviction scaling, caps, brake."""

import pytest

from crypto_bot.risk import sizing as sz

CFG = sz.RiskCfg()  # defaults: base 1%, max 2%, heat 6%, 5 positions, tiered


# --- Conviction scaling -----------------------------------------------------
def test_tiered_risk_tiers():
    assert sz.tiered_risk_pct(72) == 0.75
    assert sz.tiered_risk_pct(85) == 1.0
    assert sz.tiered_risk_pct(95) == 2.0
    assert sz.tiered_risk_pct(60) == 0.0  # below threshold -> no allocation


def test_conviction_capped_at_max_risk():
    cfg = sz.RiskCfg(max_risk_pct=1.5)
    assert sz.conviction_risk_pct(95, rr=3.0, cfg=cfg) == 1.5  # 2.0 capped to 1.5


def test_kelly_positive_edge_and_no_edge():
    cfg = sz.RiskCfg(sizing_mode="kelly", kelly_win_rate=0.6)
    # b=3, p=0.6, q=0.4 -> f*=(1.8-0.4)/3=0.467; x0.25 -> ~11.67% capped to max 2%.
    assert sz.conviction_risk_pct(75, rr=3.0, cfg=cfg) == 2.0
    # No edge: p=0.3, b=2 -> f*=(0.6-0.7)/2 <0 -> 0
    cfg2 = sz.RiskCfg(sizing_mode="kelly", kelly_win_rate=0.3)
    assert sz.conviction_risk_pct(75, rr=2.0, cfg=cfg2) == 0.0


# --- qty math ---------------------------------------------------------------
def test_compute_qty_van_tharp():
    # 10000 * 1% = 100 risk; stop 5 away -> 20 units.
    assert sz.compute_qty(10000, 1.0, entry=100.0, stop=95.0) == 20.0


def test_compute_qty_zero_when_stop_invalid():
    assert sz.compute_qty(10000, 1.0, entry=100.0, stop=100.0) == 0.0
    assert sz.compute_qty(10000, 1.0, entry=100.0, stop=105.0) == 0.0


# --- Drawdown brake ---------------------------------------------------------
def test_drawdown_brake_halves_when_beyond_threshold():
    assert sz.apply_drawdown_brake(2.0, drawdown_pct=12.0, brake_pct=10.0) == 1.0
    assert sz.apply_drawdown_brake(2.0, drawdown_pct=5.0, brake_pct=10.0) == 2.0


# --- Caps -------------------------------------------------------------------
def test_caps_all_clear():
    assert sz.check_caps(n_open=2, existing_heat_pct=2.0, risk_pct=1.0,
                         correlated_count=0, already_open=False, cfg=CFG) == []


def test_cap_max_positions():
    r = sz.check_caps(n_open=5, existing_heat_pct=1.0, risk_pct=1.0,
                      correlated_count=0, already_open=False, cfg=CFG)
    assert "max_positions" in r


def test_cap_portfolio_heat():
    # existing 5.5 + 1.0 = 6.5 > 6.0 cap.
    r = sz.check_caps(n_open=3, existing_heat_pct=5.5, risk_pct=1.0,
                      correlated_count=0, already_open=False, cfg=CFG)
    assert "portfolio_heat" in r


def test_cap_correlation():
    r = sz.check_caps(n_open=2, existing_heat_pct=1.0, risk_pct=1.0,
                      correlated_count=3, already_open=False, cfg=CFG)
    assert "correlation_cap" in r


def test_cap_already_open():
    r = sz.check_caps(n_open=1, existing_heat_pct=1.0, risk_pct=1.0,
                      correlated_count=0, already_open=True, cfg=CFG)
    assert "already_open" in r


# --- Portfolio heat from positions ------------------------------------------
class _Pos:
    def __init__(self, qty, entry, stop, symbol="X"):
        self.qty, self.entry_price, self.stop_price, self.symbol = qty, entry, stop, symbol
        self.status = "OPEN"


def test_portfolio_heat_from_open_positions():
    # Two positions each risking $100 on $10000 -> 2% heat.
    positions = [_Pos(20, 100, 95), _Pos(10, 50, 40)]
    # risks: 20*5=100, 10*10=100 -> 200/10000 = 2%
    assert sz.portfolio_heat_pct(positions, 10000) == pytest.approx(2.0)
