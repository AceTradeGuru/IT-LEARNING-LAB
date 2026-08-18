"""Indicator correctness — especially causality.

The backtest computes indicators over the whole series and indexes into them.
That is only valid if every indicator is causal. These tests are what makes
that assumption safe to rely on rather than merely intended.
"""

import numpy as np
import pytest

from bot import indicators as ind


@pytest.fixture
def ohlc():
    rng = np.random.default_rng(1234)
    n = 300
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + rng.uniform(0.05, 0.8, n)
    low = close - rng.uniform(0.05, 0.8, n)
    volume = rng.uniform(500, 2500, n)
    return high, low, close, volume


ALL_TRUNCATION_SAFE = [
    ("sma", lambda h, l, c, v: ind.sma(c, 20)),
    ("ema", lambda h, l, c, v: ind.ema(c, 20)),
    ("rma", lambda h, l, c, v: ind.rma(c, 14)),
    ("atr", lambda h, l, c, v: ind.atr(h, l, c, 14)),
    ("rsi", lambda h, l, c, v: ind.rsi(c, 14)),
    ("donchian_up", lambda h, l, c, v: ind.donchian(h, l, 20)[0]),
    ("donchian_dn", lambda h, l, c, v: ind.donchian(h, l, 20)[1]),
    ("avg_volume", lambda h, l, c, v: ind.rolling_mean_volume(v, 20)),
    ("slope", lambda h, l, c, v: ind.slope(c, 5)),
    ("realized_vol", lambda h, l, c, v: ind.realized_volatility(c, 20)),
    ("adx", lambda h, l, c, v: ind.adx(h, l, c, 14)[0]),
]


@pytest.mark.parametrize("name,fn", ALL_TRUNCATION_SAFE, ids=[n for n, _ in ALL_TRUNCATION_SAFE])
def test_indicator_is_causal(ohlc, name, fn):
    """Truncating future bars must not change any past value.

    If this fails, the indicator peeks ahead and every backtest using it is
    reporting profits that could not have been earned.
    """
    h, l, c, v = ohlc
    cut = 200
    full = fn(h, l, c, v)
    partial = fn(h[:cut], l[:cut], c[:cut], v[:cut])
    np.testing.assert_allclose(full[:cut], partial, equal_nan=True, rtol=1e-9, atol=1e-9)


def test_sma_known_values():
    values = [1, 2, 3, 4, 5, 6]
    out = ind.sma(values, 3)
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], [2.0, 3.0, 4.0, 5.0])


def test_ema_seeds_with_sma():
    values = [10.0] * 10
    out = ind.ema(values, 5)
    # A flat series has a flat EMA equal to the level.
    np.testing.assert_allclose(out[4:], [10.0] * 6)


def test_atr_on_constant_range():
    n = 50
    high = np.full(n, 101.0)
    low = np.full(n, 99.0)
    close = np.full(n, 100.0)
    out = ind.atr(high, low, close, 14)
    np.testing.assert_allclose(out[14:], 2.0)


def test_rsi_bounds_and_extremes():
    rising = np.arange(1, 60, dtype=float)
    out = ind.rsi(rising, 14)
    valid = out[~np.isnan(out)]
    assert (valid >= 0).all() and (valid <= 100).all()
    assert valid[-1] > 99, "a monotonic rise should pin RSI near 100"


def test_donchian_excludes_current_bar():
    """The channel must not include the bar being tested against it.

    Otherwise every new high 'breaks out' of a channel it just set, which
    generates signals that cannot be traded.
    """
    high = np.array([1, 2, 3, 4, 5, 100.0])
    low = np.array([1, 2, 3, 4, 5, 100.0])
    up, _ = ind.donchian(high, low, 5)
    assert up[5] == 5.0, "channel at bar 5 must come from bars 0..4 only"
    assert high[5] > up[5], "the spike bar should read as a breakout"


def test_rolling_mean_volume_excludes_current_bar():
    volume = np.array([10.0] * 20 + [1000.0])
    avg = ind.rolling_mean_volume(volume, 20)
    assert avg[20] == pytest.approx(10.0), "current bar's volume must not be in its own average"


def test_session_vwap_resets_per_session():
    high = np.array([10.0, 10, 10, 100, 100, 100])
    low = high.copy()
    close = high.copy()
    volume = np.ones(6)
    sessions = np.array([1, 1, 1, 2, 2, 2])
    out = ind.session_vwap(high, low, close, volume, sessions)
    assert out[2] == pytest.approx(10.0)
    assert out[3] == pytest.approx(100.0), "VWAP must reset at the session boundary"


def test_adx_rises_in_a_trend_and_falls_in_chop():
    n = 200
    trend_close = np.arange(n, dtype=float) * 0.5 + 100
    trend = ind.adx(trend_close + 0.3, trend_close - 0.3, trend_close, 14)[0]

    rng = np.random.default_rng(9)
    chop_close = 100 + rng.normal(0, 0.4, n)
    chop = ind.adx(chop_close + 0.3, chop_close - 0.3, chop_close, 14)[0]

    assert np.nanmean(trend[50:]) > np.nanmean(chop[50:]) + 15, (
        "ADX must separate trend from chop — it is the strategy's regime filter"
    )


def test_crossings():
    series = np.array([1.0, 2.0, 3.0, 2.0, 1.0])
    assert ind.crossed_above(series, 2.5, 2)
    assert not ind.crossed_above(series, 2.5, 1)
    assert ind.crossed_below(series, 2.5, 3)


def test_short_series_returns_nan_not_garbage():
    """Warmup must be NaN. A zero ADX reads as 'no trend' and silently
    changes behaviour instead of being caught."""
    assert np.isnan(ind.atr([1, 2], [1, 2], [1, 2], 14)).all()
    assert np.isnan(ind.adx([1, 2], [1, 2], [1, 2], 14)[0]).all()
    assert np.isnan(ind.sma([1, 2], 14)).all()
