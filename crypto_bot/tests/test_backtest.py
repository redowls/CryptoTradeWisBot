"""Phase 8 (DB-free): backtest metrics, bucketing, config parsing."""

from crypto_bot.backtest.engine import (
    BTCfg, BTTrade, bucket_of, bucket_report, compute_metrics,
)


def _trade(pnl, r, score=75, exit_time="2026-01-01T00:00:00"):
    return BTTrade(
        symbol="X", entry_time="2026-01-01", entry_price=100, qty=1, stop=95, target=110,
        score=score, source="BOTH", exit_time=exit_time, exit_price=100 + pnl,
        exit_reason="target" if pnl > 0 else "stop", pnl=pnl, r_multiple=r, bars_held=5,
    )


def test_bucket_of_boundaries():
    assert bucket_of(95) == "90+"
    assert bucket_of(85) == "80-89"
    assert bucket_of(70) == "70-79"
    assert bucket_of(69) == "below70"


def test_metrics_empty():
    assert compute_metrics([], 10000) == {"trade_count": 0}


def test_metrics_basic_win_rate_and_profit_factor():
    trades = [
        _trade(200, 2.0, exit_time="2026-01-01T01:00:00"),
        _trade(200, 2.0, exit_time="2026-01-01T02:00:00"),
        _trade(-100, -1.0, exit_time="2026-01-01T03:00:00"),
    ]
    m = compute_metrics(trades, 10000)
    assert m["trade_count"] == 3
    assert m["wins"] == 2 and m["losses"] == 1
    assert m["win_rate_pct"] == round(2 / 3 * 100, 2)
    # gross profit 400, gross loss 100 -> PF 4.0
    assert m["profit_factor"] == 4.0
    assert m["total_pnl"] == 300
    assert m["avg_r"] == round((2.0 + 2.0 - 1.0) / 3, 4)


def test_metrics_max_drawdown():
    # +300 (eq 10300, peak), then -500 (eq 9800) -> dd from 10300 = 4.85%
    trades = [
        _trade(300, 3.0, exit_time="2026-01-01T01:00:00"),
        _trade(-500, -5.0, exit_time="2026-01-01T02:00:00"),
    ]
    m = compute_metrics(trades, 10000)
    assert m["max_drawdown_pct"] == round((10300 - 9800) / 10300 * 100, 2)


def test_bucket_report_groups_by_score():
    trades = [
        _trade(100, 1.0, score=72),
        _trade(-100, -1.0, score=78),
        _trade(200, 2.0, score=95),
    ]
    rep = bucket_report(trades)
    assert rep["70-79"]["count"] == 2
    assert rep["70-79"]["win_rate_pct"] == 50.0
    assert rep["90+"]["count"] == 1
    assert rep["80-89"]["count"] == 0


def test_btcfg_parses_chandelier_mult():
    cfg = BTCfg(chandelier_mult=3.0)
    assert cfg.chandelier_mult == 3.0
    assert 0 < cfg.fee < 1 and 0 < cfg.slippage < 1
