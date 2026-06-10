"""Phase 10 (DB-free): exit engine — each exit type + partial scale-out P&L."""

import pytest

from crypto_bot.execution.exits import (
    ExitConfig, PositionState, manage_bar, run_position,
)

# entry 100, stop 90 -> risk_per_unit = 10; 1R=110, 2R=120. target 130 (3R).
CFG = ExitConfig(chandelier_mult=3.0, time_stop_bars=24, use_partials=True)


def _state(qty=1.0, target=130.0):
    return PositionState(entry=100.0, initial_stop=90.0, target=target, qty=qty)


def test_hard_stop_loss():
    st = _state()
    acts = manage_bar(st, high=101, low=89, close=92, atr=5, cfg=CFG)  # low pierces stop 90
    assert st.closed and st.exit_reason == "stop_loss"
    assert acts[-1].kind == "EXIT" and acts[-1].price == 90.0
    # full loss: 1 unit * (90-100) = -10
    assert st.realized_pnl == pytest.approx(-10.0)


def test_partial_scale_at_1R_moves_to_breakeven():
    st = _state(qty=1.0)
    manage_bar(st, high=111, low=100, close=110, atr=50, cfg=CFG)  # reach 1R=110 (atr huge so no trail)
    assert st.scaled_1r
    assert st.qty_remaining == pytest.approx(0.5)        # sold 50%
    assert st.current_stop == 100.0 and st.stop_reason == "breakeven"
    assert st.realized_pnl == pytest.approx(0.5 * (110 - 100))  # +5


def test_scale_2R_then_target_exit():
    st = _state(qty=1.0)
    # One bar that spans up to the target (130): 1R, 2R, then target on remainder.
    manage_bar(st, high=131, low=100, close=130, atr=50, cfg=CFG)
    assert st.closed and st.exit_reason == "target"
    # 0.5@110 (+5) + 0.25@120 (+5) + 0.25@130 (+7.5) = +17.5
    assert st.realized_pnl == pytest.approx(17.5)
    assert st.realized_pnl / (st.qty * st.entry) * 100 == pytest.approx(17.5)


def test_breakeven_stop_after_1R():
    st = _state(qty=1.0)
    manage_bar(st, high=111, low=100, close=110, atr=50, cfg=CFG)   # scale 1R, stop->BE(100)
    acts = manage_bar(st, high=108, low=99, close=101, atr=50, cfg=CFG)  # dips to 99 <= 100
    assert st.closed and st.exit_reason == "breakeven_stop"
    assert acts[-1].price == 100.0
    # +5 from 1R partial, then 0.5*(100-100)=0 -> net +5
    assert st.realized_pnl == pytest.approx(5.0)


def test_trailing_stop_exit():
    st = _state(qty=1.0, target=10_000.0)  # target far away so trail is what triggers
    cfg = ExitConfig(chandelier_mult=3.0, time_stop_bars=999, use_partials=False)
    # Bar 1: rallies to 200 with atr 10 -> trail ratchets to 200-30=170.
    manage_bar(st, high=200, low=100, close=199, atr=10, cfg=cfg)
    assert st.current_stop == pytest.approx(170.0) and st.stop_reason == "chandelier"
    # Bar 2: pulls back through 170.
    acts = manage_bar(st, high=201, low=165, close=168, atr=10, cfg=cfg)
    assert st.closed and st.exit_reason == "trailing_stop"
    assert acts[-1].price == pytest.approx(170.0)
    assert st.realized_pnl == pytest.approx(70.0)  # 1*(170-100)


def test_time_stop_exit():
    st = _state(qty=1.0, target=10_000.0)
    cfg = ExitConfig(chandelier_mult=100.0, time_stop_bars=3, use_partials=False)
    # Flat bars near entry; atr huge so no trail; never reaches target.
    acts = None
    for _ in range(3):
        acts = manage_bar(st, high=100.4, low=99.6, close=100.2, atr=1000, cfg=cfg)
    assert st.closed and st.exit_reason == "time"
    assert acts[-1].price == pytest.approx(100.2)


def test_run_position_stops_at_close():
    st = _state(qty=1.0)
    bars = [{"high": 101, "low": 89, "close": 90, "atr": 5}] * 5  # first bar stops out
    logs = run_position(st, bars, CFG)
    assert len(logs) == 1 and st.closed


def test_no_lookahead_partials_persist_across_bars():
    st = _state(qty=1.0)
    manage_bar(st, high=111, low=100, close=110, atr=50, cfg=CFG)  # 1R
    assert st.scaled_1r and not st.scaled_2r
    manage_bar(st, high=121, low=109, close=120, atr=50, cfg=CFG)  # 2R later bar
    assert st.scaled_2r
    assert st.qty_remaining == pytest.approx(0.25)
