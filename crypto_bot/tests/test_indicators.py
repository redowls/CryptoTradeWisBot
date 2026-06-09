"""Phase 3 (DB-free): indicators vs independent reference loops + edge fixtures."""

import numpy as np
import pandas as pd
import pytest

from crypto_bot.indicators import engine as ind


# --- Independent (pure-Python) reference implementations --------------------
def ref_ema(vals, span):
    k = 2.0 / (span + 1)
    out, prev = [], None
    for v in vals:
        prev = v if prev is None else k * v + (1 - k) * prev
        out.append(prev)
    return out


def ref_rma(vals, period):
    a = 1.0 / period
    out, prev = [], None
    for v in vals:
        prev = v if prev is None else a * v + (1 - a) * prev
        out.append(prev)
    return out


def ref_true_range(h, l, c):
    out = [h[0] - l[0]]
    for i in range(1, len(c)):
        out.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return out


# --- Deterministic OHLCV fixture --------------------------------------------
@pytest.fixture
def ohlcv():
    n = 80
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 1.0, n))
    high = close + rng.uniform(0.2, 1.0, n)
    low = close - rng.uniform(0.2, 1.0, n)
    open_ = close + rng.uniform(-0.5, 0.5, n)
    volume = rng.uniform(1000, 5000, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="h")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def _bars(highs, lows, closes):
    n = len(closes)
    idx = pd.date_range("2026-01-01", periods=n, freq="h")
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes,
         "volume": [1000.0] * n},
        index=idx,
    )


# --- EMA --------------------------------------------------------------------
def test_ema_matches_reference(ohlcv):
    for span in ind.EMA_PERIODS:
        got = ind.ema(ohlcv["close"], span).to_numpy()
        exp = np.array(ref_ema(ohlcv["close"].tolist(), span))
        np.testing.assert_allclose(got, exp, rtol=1e-9, atol=1e-9)


def test_ema_constant_series():
    s = pd.Series([50.0] * 30)
    assert np.allclose(ind.ema(s, 8).to_numpy(), 50.0)


# --- RSI --------------------------------------------------------------------
def test_rsi_matches_reference(ohlcv):
    close = ohlcv["close"].tolist()
    deltas = [0.0] + [close[i] - close[i - 1] for i in range(1, len(close))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    ag, al = ref_rma(gains, ind.RSI_PERIOD), ref_rma(losses, ind.RSI_PERIOD)
    exp = [100 - 100 / (1 + g / l) if l != 0 else 100.0 for g, l in zip(ag, al)]

    got = ind.rsi(ohlcv["close"]).to_numpy()
    p = ind.RSI_PERIOD
    np.testing.assert_allclose(got[p:], np.array(exp)[p:], rtol=1e-9, atol=1e-9)


def test_rsi_all_gains_is_100():
    s = pd.Series(np.arange(100, 140, 1.0))  # strictly increasing
    out = ind.rsi(s).dropna()
    assert np.allclose(out.to_numpy(), 100.0)


def test_rsi_all_losses_is_0():
    s = pd.Series(np.arange(140, 100, -1.0))  # strictly decreasing
    out = ind.rsi(s).dropna()
    assert np.allclose(out.to_numpy(), 0.0)


# --- ATR --------------------------------------------------------------------
def test_atr_matches_reference(ohlcv):
    h, l, c = ohlcv["high"].tolist(), ohlcv["low"].tolist(), ohlcv["close"].tolist()
    exp = ref_rma(ref_true_range(h, l, c), ind.ATR_PERIOD)
    got = ind.atr(ohlcv["high"], ohlcv["low"], ohlcv["close"]).to_numpy()
    p = ind.ATR_PERIOD
    np.testing.assert_allclose(got[p:], np.array(exp)[p:], rtol=1e-9, atol=1e-9)


def test_atr_constant_true_range():
    # Identical bars: high-low = R, prev_close in the middle -> TR == R every bar.
    R = 4.0
    highs = [102.0] * 40
    lows = [98.0] * 40
    closes = [100.0] * 40
    out = ind.atr(pd.Series(highs), pd.Series(lows), pd.Series(closes)).dropna()
    np.testing.assert_allclose(out.to_numpy(), R, rtol=1e-9)


# --- ADX --------------------------------------------------------------------
def test_adx_trending_higher_than_ranging():
    n = 120
    # Strong uptrend.
    base = np.arange(100, 100 + n, 1.0)
    trend = ind.adx(pd.Series(base + 1), pd.Series(base - 1), pd.Series(base)).dropna()
    # Choppy range.
    osc = 100 + np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
    rng = ind.adx(pd.Series(osc + 1), pd.Series(osc - 1), pd.Series(osc)).dropna()

    assert trend.iloc[-1] > 25, f"trend ADX too low: {trend.iloc[-1]}"
    assert trend.iloc[-1] > rng.iloc[-1], "trend ADX should exceed ranging ADX"
    assert 0.0 <= rng.iloc[-1] <= 100.0


# --- Engine wiring ----------------------------------------------------------
def test_compute_indicators_adds_all_columns(ohlcv):
    out = ind.compute_indicators(ohlcv)
    for col in ind.INDICATOR_COLUMNS:
        assert col in out.columns
    # Original OHLCV preserved.
    assert len(out) == len(ohlcv)
    # Last row fully warmed (no NaN in indicators).
    assert out.iloc[-1][ind.INDICATOR_COLUMNS].notna().all()
