"""Support/resistance detection (Phase 4).

Pipeline (summary.md §5.3):
  1. Confirmed swing pivots (strength PIVOT_STRENGTH, no repaint — a pivot needs
     `n` bars on *both* sides, so the last `n` bars never form pivots).
  2. Cluster pivots into price zones within CLUSTER_TOLERANCE_PCT; each zone
     carries a touch_count and a time span.
  3. Volume Profile: Point of Control (POC) and 70% Value Area (VAH/VAL).
  4. Rank by strength (touches + span); classify relative to current price;
     persist the strongest supports/resistances + POC/VAH/VAL to `sr_levels`.

Re-running for a symbol/timeframe deactivates the previous active levels and
writes a fresh set, so `sr_levels` always reflects current structure and stale
levels are marked inactive (kept for history).

CLI:  python -m crypto_bot.analysis.levels
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import and_, select, update

from crypto_bot import config_store
from crypto_bot.db import get_session
from crypto_bot.indicators.engine import load_bars
from crypto_bot.logging_setup import get_logger
from crypto_bot.models import SRLevel, Watchlist

log = get_logger(__name__)

VALUE_AREA_PCT = 0.70  # standard 70% value area
VP_BINS = 50
MAX_LEVELS_PER_SIDE = 6  # cap persisted cluster levels above/below price


@dataclass
class Level:
    price: float
    level_type: str  # 'support' | 'resistance'
    source: str      # 'cluster' | 'poc' | 'vah' | 'val'
    touch_count: int
    strength: float


# --- 1. Swing pivots --------------------------------------------------------
def find_swing_pivots(df: pd.DataFrame, n: int) -> tuple[list, list]:
    """Return (highs, lows) as lists of (ts, price). Confirmed only (no repaint)."""
    highs, lows = [], []
    H, L = df["high"].to_numpy(), df["low"].to_numpy()
    idx = df.index
    for i in range(n, len(df) - n):
        left_h, right_h = H[i - n:i], H[i + 1:i + n + 1]
        if H[i] > left_h.max() and H[i] > right_h.max():
            highs.append((idx[i], float(H[i])))
        left_l, right_l = L[i - n:i], L[i + 1:i + n + 1]
        if L[i] < left_l.min() and L[i] < right_l.min():
            lows.append((idx[i], float(L[i])))
    return highs, lows


# --- 2. Clustering ----------------------------------------------------------
def cluster_levels(points: list, tol_pct: float) -> list[dict]:
    """Merge nearby pivot prices into zones within `tol_pct` of the running mean.

    Each zone: {price (mean), touch_count, first_ts, last_ts}.
    """
    if not points:
        return []
    pts = sorted(points, key=lambda p: p[1])
    clusters: list[dict] = []
    for ts, price in pts:
        if clusters and abs(price - clusters[-1]["price"]) / clusters[-1]["price"] <= tol_pct / 100.0:
            cl = clusters[-1]
            cl["prices"].append(price)
            cl["tss"].append(ts)
            cl["price"] = float(np.mean(cl["prices"]))
        else:
            clusters.append({"prices": [price], "tss": [ts], "price": float(price)})
    for cl in clusters:
        cl["touch_count"] = len(cl["prices"])
        cl["first_ts"] = min(cl["tss"])
        cl["last_ts"] = max(cl["tss"])
    return clusters


def _cluster_strength(cl: dict, total_span: pd.Timedelta) -> float:
    """More touches + longer span = stronger."""
    if total_span and total_span.total_seconds() > 0:
        span_frac = (cl["last_ts"] - cl["first_ts"]).total_seconds() / total_span.total_seconds()
    else:
        span_frac = 0.0
    return round(cl["touch_count"] * (1.0 + span_frac), 4)


# --- 3. Volume profile ------------------------------------------------------
def volume_profile(df: pd.DataFrame, bins: int = VP_BINS) -> dict | None:
    """Return {'poc','vah','val','poc_strength'} or None if volume is unusable."""
    vol = df["volume"].to_numpy(dtype=float)
    if vol.sum() <= 0:
        return None
    typical = ((df["high"] + df["low"] + df["close"]) / 3.0).to_numpy(dtype=float)
    bins = min(bins, max(2, len(df)))
    hist, edges = np.histogram(typical, bins=bins, weights=vol)
    total = hist.sum()
    if total <= 0:
        return None

    poc_idx = int(np.argmax(hist))
    centers = (edges[:-1] + edges[1:]) / 2.0
    poc = float(centers[poc_idx])

    # Expand outward from POC until cumulative volume reaches the value-area target.
    target = VALUE_AREA_PCT * total
    lo = hi = poc_idx
    acc = hist[poc_idx]
    last = len(hist) - 1
    while acc < target and (lo > 0 or hi < last):
        left = hist[lo - 1] if lo > 0 else -np.inf
        right = hist[hi + 1] if hi < last else -np.inf
        if right >= left:
            hi += 1
            acc += hist[hi]
        else:
            lo -= 1
            acc += hist[lo]
    return {
        "poc": poc,
        "val": float(edges[lo]),
        "vah": float(edges[hi + 1]),
        "poc_strength": round(100.0 * hist[poc_idx] / total, 4),
    }


# --- 4. Detection orchestration --------------------------------------------
def _classify(price: float, current_price: float) -> str:
    return "support" if price < current_price else "resistance"


def detect_levels(df: pd.DataFrame, current_price: float,
                  pivot_strength: int, tol_pct: float) -> list[Level]:
    """Pure detection: pivots -> clusters -> ranked levels + POC/VAH/VAL.

    Deterministic for a given input (stable run-to-run).
    """
    highs, lows = find_swing_pivots(df, pivot_strength)
    clusters = cluster_levels(highs + lows, tol_pct)

    total_span = (df.index[-1] - df.index[0]) if len(df) > 1 else pd.Timedelta(0)
    cluster_levels_list = [
        Level(
            price=round(cl["price"], 8),
            level_type=_classify(cl["price"], current_price),
            source="cluster",
            touch_count=cl["touch_count"],
            strength=_cluster_strength(cl, total_span),
        )
        for cl in clusters
    ]

    # Keep the strongest N supports and resistances.
    supports = sorted((l for l in cluster_levels_list if l.level_type == "support"),
                      key=lambda l: l.strength, reverse=True)[:MAX_LEVELS_PER_SIDE]
    resistances = sorted((l for l in cluster_levels_list if l.level_type == "resistance"),
                         key=lambda l: l.strength, reverse=True)[:MAX_LEVELS_PER_SIDE]
    levels = supports + resistances

    vp = volume_profile(df)
    if vp:
        for src in ("poc", "vah", "val"):
            price = vp[src]
            levels.append(Level(
                price=round(price, 8),
                level_type=_classify(price, current_price),
                source=src,
                touch_count=1,
                strength=vp["poc_strength"],
            ))
    return levels


def persist_levels(session, symbol: str, timeframe: str, levels: list[Level]) -> int:
    """Deactivate prior active levels, insert the fresh set as active. Returns count."""
    session.execute(
        update(SRLevel)
        .where(and_(SRLevel.symbol == symbol, SRLevel.timeframe == timeframe,
                    SRLevel.is_active == True))  # noqa: E712
        .values(is_active=False)
    )
    for lv in levels:
        session.add(SRLevel(
            symbol=symbol, timeframe=timeframe, level_price=lv.price,
            level_type=lv.level_type, source=lv.source,
            touch_count=lv.touch_count, strength=lv.strength, is_active=True,
        ))
    session.commit()
    return len(levels)


def detect_for_symbol(session, symbol: str, timeframe: str, persist: bool = False) -> list[Level]:
    pivot_strength = config_store.get_int("PIVOT_STRENGTH", 5)
    tol_pct = config_store.get_float("CLUSTER_TOLERANCE_PCT", 0.4)
    df = load_bars(session, symbol, timeframe)
    if df.empty or len(df) < 2 * pivot_strength + 1:
        log.warning("Insufficient bars for %s %s — skipping S/R.", symbol, timeframe)
        return []
    current_price = float(df["close"].iloc[-1])
    levels = detect_levels(df, current_price, pivot_strength, tol_pct)
    if persist:
        n = persist_levels(session, symbol, timeframe, levels)
        log.info("%-9s %-3s: %d S/R levels persisted (px=%.2f).", symbol, timeframe, n, current_price)
    return levels


def detect_all(timeframes: list[str] | None = None, persist: bool = True) -> dict:
    timeframes = timeframes or ["1H", "4H", "1D"]
    totals = {"series": 0, "levels": 0}
    with get_session() as session:
        symbols = list(
            session.scalars(select(Watchlist.symbol).where(Watchlist.is_active == True)).all()  # noqa: E712
        )
        for symbol in symbols:
            for tf in timeframes:
                levels = detect_for_symbol(session, symbol, tf, persist=persist)
                if levels:
                    totals["series"] += 1
                    totals["levels"] += len(levels)
    return totals


if __name__ == "__main__":
    summary = detect_all(persist=True)
    print(f"OK: detected S/R for {summary['series']} symbol/timeframe series, "
          f"{summary['levels']} active levels persisted.")
