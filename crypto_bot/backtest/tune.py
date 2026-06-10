"""Strategy tuning harness (Phase 14 follow-up).

Runs the Phase-8 backtest with arbitrary in-memory configs (no DB mutation) so we
can diagnose losers and sweep parameters to find an edge. Reuses run_symbol() and
the metrics from backtest.engine.

CLI:
  python -m crypto_bot.backtest.tune diag      # baseline + exit-reason breakdown
  python -m crypto_bot.backtest.tune sweep      # OFAT parameter sweep
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import replace
from statistics import mean

from crypto_bot.analysis.signal_engine import Cfg
from crypto_bot.backtest.engine import (BTCfg, bucket_report, compute_metrics, run_symbol)
from crypto_bot.db import get_session
from crypto_bot.risk.sizing import RiskCfg

# A representative, liquid subset for fast iteration.
TUNE_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD",
                "LINK/USD", "LTC/USD", "DOT/USD", "XRP/USD"]
START_EQUITY = 10000.0


def run_config(sig_cfg: Cfg, risk_cfg: RiskCfg, bt_cfg: BTCfg, symbols=None):
    """Run all symbols for one config; return (trades, metrics)."""
    symbols = symbols or TUNE_SYMBOLS
    trades = []
    with get_session() as s:
        for sym in symbols:
            trades.extend(run_symbol(s, sym, sig_cfg, risk_cfg, bt_cfg))
    return trades, compute_metrics(trades, START_EQUITY)


def exit_breakdown(trades) -> dict:
    """Per exit reason: (count, avg R)."""
    by = defaultdict(list)
    for t in trades:
        by[t.exit_reason].append(t.r_multiple)
    return {k: (len(v), round(mean(v), 3)) for k, v in sorted(by.items())}


def _summary(tag, metrics, trades=None):
    m = metrics
    line = (f"{tag:34} trades={m.get('trade_count',0):3} "
            f"win={m.get('win_rate_pct',0):5.1f}% avgR={m.get('avg_r',0):+.3f} "
            f"PF={m.get('profit_factor',0):.2f} maxDD={m.get('max_drawdown_pct',0):4.1f}% "
            f"ret={m.get('return_pct',0):+.1f}%")
    print(line)
    return m


def diag():
    base_sig, base_risk, base_bt = Cfg.from_store(), RiskCfg.from_store(), BTCfg.from_store()
    print(f"symbols={TUNE_SYMBOLS}\nbaseline: time_stop={base_bt.time_stop_bars} "
          f"chand={base_bt.chandelier_mult} thr={base_bt.confidence_threshold} "
          f"adx_min={base_sig.adx_min} min_rr={base_sig.min_rr} atr_stop={base_sig.atr_stop_mult}\n")
    trades, m = run_config(base_sig, base_risk, base_bt)
    _summary("BASELINE", m)
    if trades:
        print("\nexit-reason breakdown (count, avg R):")
        for reason, (n, r) in exit_breakdown(trades).items():
            print(f"  {reason:16} n={n:3} avgR={r:+.3f}")
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        print(f"\nwinners avgR={mean(t.r_multiple for t in wins):+.3f} (n={len(wins)})" if wins else "no winners")
        print(f"losers  avgR={mean(t.r_multiple for t in losses):+.3f} (n={len(losses)})" if losses else "no losers")
        print(f"avg bars held: {mean(t.bars_held for t in trades):.1f}")


def sweep():
    base_sig, base_risk, base_bt = Cfg.from_store(), RiskCfg.from_store(), BTCfg.from_store()
    print("=== OFAT sweep (one factor at a time from baseline) ===\n")
    _summary("BASELINE", run_config(base_sig, base_risk, base_bt)[1])
    print()

    grids = {
        "time_stop_bars": ([48, 72, 120, 100000], "bt"),
        "chandelier_mult": ([4.0, 5.0, 6.0], "bt"),
        "confidence_threshold": ([75.0, 80.0], "bt"),
        "adx_min": ([25.0, 30.0], "sig"),
        "min_rr": ([2.5, 3.0], "sig"),
        "atr_stop_mult": ([2.0, 3.0], "sig"),
    }
    for field, (values, which) in grids.items():
        for v in values:
            sig, risk, bt = base_sig, base_risk, base_bt
            if which == "bt":
                bt = replace(base_bt, **{field: v})
            else:
                sig = replace(base_sig, **{field: v})
            _, m = run_config(sig, risk, bt)
            _summary(f"{field}={v}", m)
        print()


def compare():
    """Baseline vs a few combined candidate configs (diagnosis-driven)."""
    bs, br, bb = Cfg.from_store(), RiskCfg.from_store(), BTCfg.from_store()
    # Smaller symbol set for speed.
    syms = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD"]

    candidates = {
        "BASELINE": (bs, bb),
        # The trail kills winners -> widen it.
        "wide_trail(ch5)": (bs, replace(bb, chandelier_mult=5.0)),
        # Widen trail + let winners develop (time stop 24h -> 72h).
        "ch5+time72": (bs, replace(bb, chandelier_mult=5.0, time_stop_bars=72)),
        # + wider initial stop for crypto vol.
        "ch5+t72+stop3": (replace(bs, atr_stop_mult=3.0),
                          replace(bb, chandelier_mult=5.0, time_stop_bars=72)),
        # Stricter entries (less chop) on top.
        "strict+ch5+t72": (replace(bs, atr_stop_mult=3.0, adx_min=25.0),
                           replace(bb, chandelier_mult=5.0, time_stop_bars=72,
                                   confidence_threshold=75.0)),
        # Let it ride: very wide trail, long time stop, higher RR.
        "ride(ch6,t120,rr2.5)": (replace(bs, atr_stop_mult=3.0, min_rr=2.5),
                                 replace(bb, chandelier_mult=6.0, time_stop_bars=120)),
    }
    print(f"symbols={syms}\n", flush=True)
    for tag, (sig, bt) in candidates.items():
        _, m = run_config(sig, br, bt, symbols=syms)
        _summary(tag, m)
        sys.stdout.flush()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "diag"
    {"diag": diag, "sweep": sweep, "compare": compare}.get(cmd, diag)()
