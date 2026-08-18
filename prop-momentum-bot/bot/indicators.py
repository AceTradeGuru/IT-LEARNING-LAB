"""Technical indicators.

Two rules hold everywhere in this module:

1. **Causality.** Value at index i uses only bars 0..i. A backtest that peeks
   one bar ahead prints a beautiful equity curve and loses real money, so the
   tests assert this property directly (see tests/test_indicators.py).
2. **Warmup is NaN, not zero.** A zero ADX reads as "no trend" and silently
   changes behaviour; NaN forces callers to handle the warmup window.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np


def _as_array(values: Sequence[float]) -> np.ndarray:
    return np.asarray(values, dtype=float)


def sma(values: Sequence[float], period: int) -> np.ndarray:
    v = _as_array(values)
    out = np.full(v.shape, np.nan)
    if period <= 0 or len(v) < period:
        return out
    cumsum = np.cumsum(np.insert(v, 0, 0.0))
    out[period - 1:] = (cumsum[period:] - cumsum[:-period]) / period
    return out


def ema(values: Sequence[float], period: int) -> np.ndarray:
    """Exponential MA seeded with an SMA of the first `period` values."""
    v = _as_array(values)
    out = np.full(v.shape, np.nan)
    if period <= 0 or len(v) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = v[:period].mean()
    for i in range(period, len(v)):
        out[i] = alpha * v[i] + (1 - alpha) * out[i - 1]
    return out


def rma(values: Sequence[float], period: int) -> np.ndarray:
    """Wilder's smoothing — the average used by ATR, RSI and ADX."""
    v = _as_array(values)
    out = np.full(v.shape, np.nan)
    if period <= 0 or len(v) < period:
        return out
    out[period - 1] = v[:period].mean()
    for i in range(period, len(v)):
        out[i] = (out[i - 1] * (period - 1) + v[i]) / period
    return out


def true_range(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> np.ndarray:
    h, l, c = _as_array(high), _as_array(low), _as_array(close)
    prev_close = np.roll(c, 1)
    prev_close[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_close), np.abs(l - prev_close)))
    return tr


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int = 14) -> np.ndarray:
    return rma(true_range(high, low, close), period)


def rsi(values: Sequence[float], period: int = 14) -> np.ndarray:
    v = _as_array(values)
    out = np.full(v.shape, np.nan)
    if len(v) <= period:
        return out
    delta = np.diff(v, prepend=v[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # Skip the synthetic first delta so warmup matches Wilder's definition.
    avg_gain = rma(gain[1:], period)
    avg_loss = rma(loss[1:], period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss == 0, np.inf, avg_gain / np.where(avg_loss == 0, 1.0, avg_loss))
        vals = 100.0 - (100.0 / (1.0 + rs))
    vals = np.where(np.isinf(rs), 100.0, vals)
    out[1:] = vals
    return out


def adx(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    period: int = 14,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Average Directional Index. Returns (adx, plus_di, minus_di).

    ADX measures trend STRENGTH, not direction — that is exactly what a
    momentum system needs as a chop filter.
    """
    h, l, c = _as_array(high), _as_array(low), _as_array(close)
    n = len(h)
    nan = np.full(n, np.nan)
    if n < period * 2:
        return nan, nan.copy(), nan.copy()

    up_move = np.diff(h, prepend=h[0])
    down_move = -np.diff(l, prepend=l[0])
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm[0] = minus_dm[0] = 0.0

    tr = true_range(h, l, c)
    atr_ = rma(tr, period)
    plus_sm = rma(plus_dm, period)
    minus_sm = rma(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * plus_sm / atr_
        minus_di = 100.0 * minus_sm / atr_
        denom = plus_di + minus_di
        dx = 100.0 * np.abs(plus_di - minus_di) / np.where(denom == 0, np.nan, denom)

    # ADX is Wilder's average of DX, but DX itself only starts at `period-1`.
    adx_out = np.full(n, np.nan)
    valid = ~np.isnan(dx)
    if valid.sum() >= period:
        first = int(np.argmax(valid))
        dx_valid = dx[first:]
        smoothed = rma(np.nan_to_num(dx_valid, nan=0.0), period)
        adx_out[first:] = smoothed
    return adx_out, plus_di, minus_di


def donchian(
    high: Sequence[float], low: Sequence[float], period: int
) -> tuple[np.ndarray, np.ndarray]:
    """Rolling channel of the PRIOR `period` bars, excluding the current bar.

    Excluding the current bar matters: including it makes every new high a
    "breakout" of a channel it just defined, which is a lookahead bug that
    turns into a strategy that cannot be traded.
    """
    h, l = _as_array(high), _as_array(low)
    n = len(h)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    if period <= 0 or n <= period:
        return upper, lower
    for i in range(period, n):
        window_h = h[i - period:i]
        window_l = l[i - period:i]
        upper[i] = window_h.max()
        lower[i] = window_l.min()
    return upper, lower


def rolling_mean_volume(volume: Sequence[float], period: int) -> np.ndarray:
    """Average volume of the PRIOR `period` bars (current bar excluded)."""
    v = _as_array(volume)
    n = len(v)
    out = np.full(n, np.nan)
    if period <= 0 or n <= period:
        return out
    base = sma(v, period)
    out[1:] = base[:-1]
    return out


def session_vwap(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    volume: Sequence[float],
    session_ids: Sequence[int],
) -> np.ndarray:
    """Volume-weighted average price, reset at each session boundary.

    `session_ids` is any array that changes value when a new session starts;
    the caller owns the timezone logic so this stays pure arithmetic.
    """
    h, l, c, v = _as_array(high), _as_array(low), _as_array(close), _as_array(volume)
    ids = np.asarray(session_ids)
    typical = (h + l + c) / 3.0
    out = np.full(len(c), np.nan)
    cum_pv = 0.0
    cum_v = 0.0
    current = None
    for i in range(len(c)):
        if current is None or ids[i] != current:
            current = ids[i]
            cum_pv = 0.0
            cum_v = 0.0
        vol = v[i] if v[i] > 0 else 1.0
        cum_pv += typical[i] * vol
        cum_v += vol
        out[i] = cum_pv / cum_v if cum_v > 0 else np.nan
    return out


def slope(values: Sequence[float], period: int = 5) -> np.ndarray:
    """Least-squares slope over a trailing window, in price units per bar."""
    v = _as_array(values)
    n = len(v)
    out = np.full(n, np.nan)
    if period < 2 or n < period:
        return out
    x = np.arange(period, dtype=float)
    x_mean = x.mean()
    denom = ((x - x_mean) ** 2).sum()
    for i in range(period - 1, n):
        window = v[i - period + 1:i + 1]
        if np.isnan(window).any():
            continue
        out[i] = ((x - x_mean) * (window - window.mean())).sum() / denom
    return out


def realized_volatility(close: Sequence[float], period: int = 20) -> np.ndarray:
    """Stdev of log returns over a trailing window, as a fraction of price."""
    c = _as_array(close)
    n = len(c)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(np.log(np.where(c > 0, c, np.nan)), prepend=np.nan)
    for i in range(period, n):
        window = rets[i - period + 1:i + 1]
        if np.isnan(window).any():
            continue
        out[i] = float(np.std(window, ddof=1))
    return out


def crossed_above(series: np.ndarray, level: np.ndarray | float, index: int) -> bool:
    """True when `series` crosses up through `level` exactly at `index`."""
    if index < 1:
        return False
    lvl_now = level[index] if isinstance(level, np.ndarray) else level
    lvl_prev = level[index - 1] if isinstance(level, np.ndarray) else level
    a, b = series[index - 1], series[index]
    if any(np.isnan(x) for x in (a, b, lvl_now, lvl_prev)):
        return False
    return a <= lvl_prev and b > lvl_now


def crossed_below(series: np.ndarray, level: np.ndarray | float, index: int) -> bool:
    if index < 1:
        return False
    lvl_now = level[index] if isinstance(level, np.ndarray) else level
    lvl_prev = level[index - 1] if isinstance(level, np.ndarray) else level
    a, b = series[index - 1], series[index]
    if any(np.isnan(x) for x in (a, b, lvl_now, lvl_prev)):
        return False
    return a >= lvl_prev and b < lvl_now
