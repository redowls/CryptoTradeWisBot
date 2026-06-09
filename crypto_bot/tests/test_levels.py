"""Phase 4 (DB-free): swing pivots, clustering, volume profile, stability."""

import numpy as np
import pandas as pd
import pytest

from crypto_bot.analysis import levels as lv


def _df(highs, lows, closes=None, volume=None):
    n = len(highs)
    closes = closes if closes is not None else [(h + l) / 2 for h, l in zip(highs, lows)]
    volume = volume if volume is not None else [1000.0] * n
    idx = pd.date_range("2026-01-01", periods=n, freq="h")
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": volume},
        index=idx,
    )


# --- Swing pivots -----------------------------------------------------------
def test_detects_obvious_peak_and_trough():
    # Peak at index 5, trough at index 11; n=3.
    highs = [10, 11, 12, 13, 14, 20, 14, 13, 12, 11, 10, 9, 10, 11, 12, 13, 14]
    lows = [h - 1 for h in highs]
    lows[11] = 2  # clear trough
    df = _df(highs, lows)
    hi, lo = lv.find_swing_pivots(df, n=3)
    hi_idx = [df.index.get_loc(ts) for ts, _ in hi]
    lo_idx = [df.index.get_loc(ts) for ts, _ in lo]
    assert 5 in hi_idx
    assert 11 in lo_idx


def test_no_repaint_excludes_last_n_bars():
    # A would-be peak in the last n bars must NOT be confirmed.
    highs = [10, 11, 12, 13, 14, 15, 16, 17, 30]  # spike at last bar
    lows = [h - 1 for h in highs]
    df = _df(highs, lows)
    hi, _ = lv.find_swing_pivots(df, n=3)
    hi_idx = [df.index.get_loc(ts) for ts, _ in hi]
    assert (len(df) - 1) not in hi_idx  # last bar can't be a confirmed pivot


# --- Clustering -------------------------------------------------------------
def test_cluster_merges_within_tolerance():
    ts = pd.Timestamp("2026-01-01")
    # 100.0, 100.2 within 0.4%; 110 separate.
    pts = [(ts, 100.0), (ts, 100.2), (ts, 110.0)]
    clusters = lv.cluster_levels(pts, tol_pct=0.4)
    assert len(clusters) == 2
    merged = [c for c in clusters if c["touch_count"] == 2][0]
    assert 100.0 <= merged["price"] <= 100.2


def test_cluster_separates_beyond_tolerance():
    ts = pd.Timestamp("2026-01-01")
    pts = [(ts, 100.0), (ts, 105.0)]  # 5% apart
    clusters = lv.cluster_levels(pts, tol_pct=0.4)
    assert len(clusters) == 2


# --- Volume profile ---------------------------------------------------------
def test_volume_profile_poc_at_high_volume_price():
    n = 100
    price = np.full(n, 100.0)
    price[40:60] = 150.0  # concentrate price action at 150
    volume = np.full(n, 10.0)
    volume[40:60] = 1000.0  # and concentrate volume there -> POC ~150
    df = _df(list(price + 1), list(price - 1), list(price), list(volume))
    vp = lv.volume_profile(df)
    assert vp is not None
    assert abs(vp["poc"] - 150.0) < 5.0
    assert vp["val"] <= vp["poc"] <= vp["vah"]


def test_volume_profile_none_when_no_volume():
    df = _df([101] * 10, [99] * 10, [100] * 10, [0.0] * 10)
    assert lv.volume_profile(df) is None


# --- Classification + stability --------------------------------------------
def test_classification_relative_to_current_price():
    assert lv._classify(90.0, current_price=100.0) == "support"
    assert lv._classify(110.0, current_price=100.0) == "resistance"


def test_detect_levels_is_deterministic():
    rng = np.random.default_rng(7)
    n = 200
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    df = _df(list(close + 1), list(close - 1), list(close), list(rng.uniform(100, 500, n)))
    a = lv.detect_levels(df, float(close[-1]), pivot_strength=5, tol_pct=0.4)
    b = lv.detect_levels(df, float(close[-1]), pivot_strength=5, tol_pct=0.4)
    assert [(x.price, x.source, x.level_type) for x in a] == \
           [(x.price, x.source, x.level_type) for x in b]


def test_detect_levels_includes_poc_vah_val():
    rng = np.random.default_rng(1)
    n = 120
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    df = _df(list(close + 1), list(close - 1), list(close), list(rng.uniform(100, 500, n)))
    sources = {x.source for x in lv.detect_levels(df, float(close[-1]), 5, 0.4)}
    assert {"poc", "vah", "val"} <= sources
