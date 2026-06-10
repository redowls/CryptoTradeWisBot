"""Event-driven backtesting harness (Phase 8) — validate BEFORE any real orders.

For each watchlist symbol it walks the TRIGGER-timeframe bars in order, feeding
Phases 3-7 with only the data available up to each bar (no lookahead):

  - indicators are causal (EMA/Wilder) so the precomputed series is safe to index;
  - the TREND timeframe is attached per bar via a backward as-of join;
  - S/R levels are rebuilt periodically from the window up to the current bar;
  - a qualifying BUY (score >= threshold, sizing ok) enters at the NEXT bar's open
    (slippage + fee applied); one position per symbol at a time;
  - exits: stop / target / Chandelier trailing / time stop (full position — partial
    scale-outs are Phase 10). If a bar touches both stop and target, stop wins.

Sizing uses a fixed reference equity (STARTING_EQUITY) so R-multiples are
comparable and poolable; portfolio-level caps (heat/correlation/positions) are a
live concern (Phase 7/9) and are not applied here. Results (metrics + expectancy
by confidence bucket) are written to backtest_results/ as JSON.

CLI:  python -m crypto_bot.backtest.engine
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import pandas as pd
from sqlalchemy import select

from crypto_bot import config_store
from crypto_bot.analysis.levels import detect_levels
from crypto_bot.analysis.scorer import score_decision
from crypto_bot.analysis.signal_engine import Cfg, combine_signal
from crypto_bot.db import get_session
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import Indicator, MarketBar, Watchlist
from crypto_bot.risk.sizing import RiskCfg, conviction_risk_pct

log = get_logger(__name__)

_RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / "backtest_results"


@dataclass
class BTCfg:
    fee: float = 0.001          # per side (fraction)
    slippage: float = 0.0005    # per side (fraction)
    chandelier_mult: float = 3.0
    time_stop_bars: int = 24
    pivot_strength: int = 5
    cluster_tol_pct: float = 0.4
    confidence_threshold: float = 70.0
    rebuild_every: int = 6
    level_window: int = 400     # trailing bars used for S/R detection (bounds cost)

    @classmethod
    def from_store(cls) -> "BTCfg":
        g = config_store
        chand = g.get_list("CHANDELIER", "22,3.0", cast=float)
        return cls(
            fee=g.get_float("FEE_PCT", 0.1) / 100.0,
            slippage=g.get_float("SLIPPAGE_PCT", 0.05) / 100.0,
            chandelier_mult=chand[1] if len(chand) > 1 else 3.0,
            time_stop_bars=g.get_int("TIME_STOP_BARS", 24),
            pivot_strength=g.get_int("PIVOT_STRENGTH", 5),
            cluster_tol_pct=g.get_float("CLUSTER_TOLERANCE_PCT", 0.4),
            confidence_threshold=g.get_float("CONFIDENCE_THRESHOLD", 70.0),
        )


@dataclass
class BTTrade:
    symbol: str
    entry_time: str
    entry_price: float
    qty: float
    stop: float
    target: float
    score: float
    source: str
    exit_time: str
    exit_price: float
    exit_reason: str
    pnl: float
    r_multiple: float
    bars_held: int


def bucket_of(score: float) -> str:
    if score >= 90:
        return "90+"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    return "below70"


# --- Data loading -----------------------------------------------------------
def _load_enriched(session, symbol: str, timeframe: str) -> pd.DataFrame:
    j = MarketBar.__table__.join(
        Indicator.__table__,
        (MarketBar.symbol == Indicator.symbol)
        & (MarketBar.timeframe == Indicator.timeframe)
        & (MarketBar.ts == Indicator.ts),
    )
    cols = [
        MarketBar.ts, MarketBar.open, MarketBar.high, MarketBar.low,
        MarketBar.close, MarketBar.volume,
        Indicator.ema8, Indicator.ema10, Indicator.ema20,
        Indicator.ema21, Indicator.ema34, Indicator.ema55,
        Indicator.atr, Indicator.rsi, Indicator.adx, Indicator.vol_avg,
    ]
    rows = session.execute(
        select(*cols).select_from(j)
        .where((MarketBar.symbol == symbol) & (MarketBar.timeframe == timeframe))
        .order_by(MarketBar.ts)
    ).all()
    names = ["ts", "open", "high", "low", "close", "volume", "ema8", "ema10",
             "ema20", "ema21", "ema34", "ema55", "atr", "rsi", "adx", "vol_avg"]
    df = pd.DataFrame(rows, columns=names)
    for c in names[1:]:
        df[c] = df[c].astype("float64")
    return df


# --- Per-symbol simulation --------------------------------------------------
def run_symbol(session, symbol: str, sig_cfg: Cfg, risk_cfg: RiskCfg, bt_cfg: BTCfg) -> list[BTTrade]:
    e1 = _load_enriched(session, symbol, sig_cfg.trigger_tf)
    e4 = _load_enriched(session, symbol, sig_cfg.trend_tf)
    if len(e1) < 60 or e4.empty:
        return []

    e4t = e4[["ts", "ema21", "ema34", "ema55", "close"]].rename(
        columns={"ema21": "t21", "ema34": "t34", "ema55": "t55", "close": "t_close"})
    m = pd.merge_asof(e1.sort_values("ts"), e4t.sort_values("ts"), on="ts",
                      direction="backward").dropna(subset=["t21"]).reset_index(drop=True)
    if len(m) < 60:
        return []
    ohlc = m.set_index("ts")[["open", "high", "low", "close", "volume"]]

    trades: list[BTTrade] = []
    pos = None
    pending = None
    resistances: list[float] = []

    for i in range(1, len(m)):
        row = m.iloc[i]
        prev = m.iloc[i - 1]

        # 1) fill a pending entry at this bar's open
        if pending is not None and pos is None:
            fill = float(row.open) * (1 + bt_cfg.slippage)
            entry_fee = fill * pending["qty"] * bt_cfg.fee
            pos = {**pending, "entry_price": fill, "entry_time": row.ts,
                   "entry_index": i, "highest_high": float(row.high), "entry_fee": entry_fee}
            pending = None

        # 2) manage an open position against this bar
        if pos is not None:
            pos["highest_high"] = max(pos["highest_high"], float(row.high))
            trail = pos["highest_high"] - bt_cfg.chandelier_mult * pos["atr"]
            eff_stop = max(pos["stop"], trail)
            exit_price = exit_reason = None
            if float(row.low) <= eff_stop:
                exit_price, exit_reason = eff_stop, ("trail" if trail > pos["stop"] else "stop")
            elif float(row.high) >= pos["target"]:
                exit_price, exit_reason = pos["target"], "target"
            else:
                held = i - pos["entry_index"]
                if held >= bt_cfg.time_stop_bars and \
                        (float(row.close) - pos["entry_price"]) / pos["entry_price"] < 0.01:
                    exit_price, exit_reason = float(row.close), "time"

            if exit_price is not None:
                fill = exit_price * (1 - bt_cfg.slippage)
                exit_fee = fill * pos["qty"] * bt_cfg.fee
                pnl = pos["qty"] * (fill - pos["entry_price"]) - pos["entry_fee"] - exit_fee
                r = pnl / pos["risk_amount"] if pos["risk_amount"] > 0 else 0.0
                trades.append(BTTrade(
                    symbol=symbol, entry_time=str(pos["entry_time"]),
                    entry_price=round(pos["entry_price"], 8), qty=round(pos["qty"], 8),
                    stop=round(pos["stop"], 8), target=round(pos["target"], 8),
                    score=pos["score"], source=pos["source"], exit_time=str(row.ts),
                    exit_price=round(fill, 8), exit_reason=exit_reason,
                    pnl=round(pnl, 4), r_multiple=round(r, 4), bars_held=i - pos["entry_index"],
                ))
                pos = None

        # 3) rebuild S/R levels as-of (no lookahead)
        if not resistances or i % bt_cfg.rebuild_every == 0:
            window = ohlc.iloc[max(0, i + 1 - bt_cfg.level_window):i + 1]
            cp = float(window["close"].iloc[-1])
            levels = detect_levels(window, cp, bt_cfg.pivot_strength, bt_cfg.cluster_tol_pct)
            resistances = [l.price for l in levels if l.level_type == "resistance"]

        # 4) evaluate a new signal only when flat
        if pos is None and pending is None:
            prev_short = bool(prev.ema8 > prev.ema10 > prev.ema20)
            d = combine_signal(
                symbol=symbol, ts=row.ts, open_=float(row.open), high=float(row.high),
                low=float(row.low), close=float(row.close), volume=float(row.volume),
                prev_close=float(prev.close), atr=float(row.atr), rsi=float(row.rsi),
                adx=float(row.adx), vol_avg=float(row.vol_avg),
                ema8=float(row.ema8), ema10=float(row.ema10), ema20=float(row.ema20),
                prev_short_stacked=prev_short, trend_ema21=float(row.t21),
                trend_ema34=float(row.t34), trend_ema55=float(row.t55), trend_close=float(row.t_close),
                resistances=resistances,
                recent_closes=list(m["close"].iloc[max(0, i - 6):i]),
                recent_lows=list(m["low"].iloc[max(0, i - 6):i]),
                cfg=sig_cfg,
            )
            score, _ = score_decision(d, sig_cfg)
            if d.signal == "BUY" and score >= bt_cfg.confidence_threshold \
                    and d.target_price is not None:
                risk_pct = conviction_risk_pct(score, d.rr_ratio, risk_cfg)
                rpu = d.entry_price - d.stop_price
                if risk_pct > 0 and rpu > 0:
                    risk_amount = risk_cfg.starting_equity * risk_pct / 100.0
                    pending = {
                        "qty": risk_amount / rpu, "stop": d.stop_price,
                        "target": d.target_price, "score": score,
                        "source": d.signal_source, "atr": float(row.atr),
                        "risk_amount": risk_amount,
                    }
    return trades


# --- Metrics ----------------------------------------------------------------
def compute_metrics(trades: list[BTTrade], starting_equity: float) -> dict:
    n = len(trades)
    if n == 0:
        return {"trade_count": 0}
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    total_pnl = sum(t.pnl for t in trades)

    # Equity curve (non-compounding), ordered by exit time, for max drawdown.
    eq = starting_equity
    peak = eq
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.exit_time):
        eq += t.pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100.0 if peak > 0 else 0.0)

    return {
        "trade_count": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / n, 2),
        "avg_r": round(mean(t.r_multiple for t in trades), 4),
        "avg_win_r": round(mean(t.r_multiple for t in wins), 4) if wins else 0.0,
        "avg_loss_r": round(mean(t.r_multiple for t in losses), 4) if losses else 0.0,
        "expectancy_r": round(mean(t.r_multiple for t in trades), 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else float("inf"),
        "total_pnl": round(total_pnl, 2),
        "return_pct": round(100.0 * total_pnl / starting_equity, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "final_equity": round(starting_equity + total_pnl, 2),
    }


def bucket_report(trades: list[BTTrade]) -> dict:
    out = {}
    for b in ("70-79", "80-89", "90+"):
        grp = [t for t in trades if bucket_of(t.score) == b]
        if grp:
            wins = sum(1 for t in grp if t.pnl > 0)
            out[b] = {
                "count": len(grp),
                "win_rate_pct": round(100.0 * wins / len(grp), 2),
                "avg_r": round(mean(t.r_multiple for t in grp), 4),
            }
        else:
            out[b] = {"count": 0, "win_rate_pct": 0.0, "avg_r": 0.0}
    return out


def run_backtest(symbols: list[str] | None = None, save: bool = True) -> dict:
    sig_cfg, risk_cfg, bt_cfg = Cfg.from_store(), RiskCfg.from_store(), BTCfg.from_store()
    all_trades: list[BTTrade] = []
    per_symbol = {}
    with get_session() as session:
        symbols = symbols or list(
            session.scalars(select(Watchlist.symbol).where(Watchlist.is_active == True)).all()  # noqa: E712
        )
        for symbol in symbols:
            tr = run_symbol(session, symbol, sig_cfg, risk_cfg, bt_cfg)
            per_symbol[symbol] = len(tr)
            all_trades.extend(tr)
            log.info("%-9s backtest trades: %d", symbol, len(tr))

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "trigger_tf": sig_cfg.trigger_tf, "trend_tf": sig_cfg.trend_tf,
            "confidence_threshold": bt_cfg.confidence_threshold,
            "fee_pct": bt_cfg.fee * 100, "slippage_pct": bt_cfg.slippage * 100,
            "sizing_mode": risk_cfg.sizing_mode, "starting_equity": risk_cfg.starting_equity,
            "chandelier_mult": bt_cfg.chandelier_mult, "time_stop_bars": bt_cfg.time_stop_bars,
        },
        "per_symbol_trade_count": per_symbol,
        "metrics": compute_metrics(all_trades, risk_cfg.starting_equity),
        "by_confidence_bucket": bucket_report(all_trades),
    }
    if save:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = _RESULTS_DIR / f"backtest_{stamp}.json"
        path.write_text(json.dumps({**results, "trades": [asdict(t) for t in all_trades]}, indent=2))
        results["saved_to"] = str(path)
    return results


if __name__ == "__main__":
    r = run_backtest()
    m = r["metrics"]
    print("=== Backtest ===")
    print(f"params: {r['params']}")
    print(f"per-symbol trades: {r['per_symbol_trade_count']}")
    if m.get("trade_count", 0) == 0:
        print("No trades generated.")
    else:
        print(f"trades={m['trade_count']} win_rate={m['win_rate_pct']}% "
              f"avg_R={m['avg_r']} profit_factor={m['profit_factor']} "
              f"max_dd={m['max_drawdown_pct']}% return={m['return_pct']}%")
        print("by confidence bucket:")
        for b, s in r["by_confidence_bucket"].items():
            print(f"  {b:6} count={s['count']:4} win_rate={s['win_rate_pct']:5}% avg_R={s['avg_r']}")
    print(f"saved: {r.get('saved_to')}")
