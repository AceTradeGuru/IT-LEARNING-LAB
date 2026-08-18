"""Feature extraction at signal time.

Every feature here is computed from bars at or before the signal bar. That is
not a stylistic preference — a single feature that peeks one bar ahead produces
a model with excellent validation scores and no live edge, and it is nearly
impossible to spot after the fact. `tests/test_ml.py` asserts the property by
truncating the tape and requiring identical features.

Features are also **scale-free** wherever possible: spreads are divided by ATR,
prices by price, volume by its own average. A model trained on MES must be able
to say something about EURUSD, and raw point values would make that impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from ..config import Instrument
from ..data.types import Side, Signal
from ..signals.base import IndicatorCache

# Order matters and is part of the model contract: a saved model stores this
# list and refuses to score a vector built from a different one.
FEATURE_NAMES: List[str] = [
    "side",                 # +1 long, -1 short — lets one model serve both
    "adx",                  # trend strength
    "di_spread",            # (+DI - -DI) * side: directional agreement
    "atr_pct",              # ATR / price: volatility regime
    "atr_expansion",        # ATR vs its own 50-bar average
    "ema_fast_slow_atr",    # (fast - slow) / ATR * side
    "ema_slow_trend_atr",   # (slow - trend) / ATR * side
    "close_vs_trend_atr",   # (close - trend EMA) / ATR * side
    "rsi_directional",      # RSI oriented to trade direction
    "vwap_distance_atr",    # (close - VWAP) / ATR * side
    "volume_ratio",         # bar volume / 20-bar average
    "body_fraction",        # |close-open| / range: conviction of the bar
    "upper_wick_frac",      # rejection above
    "lower_wick_frac",      # rejection below
    "ret_5_atr",            # 5-bar return in ATR units * side
    "ret_20_atr",           # 20-bar return in ATR units * side
    "donchian_position",    # where price sits in its 20-bar range
    "consec_bars",          # run length of same-direction closes * side
    "minutes_into_session",
    "minutes_to_window_end",
    "reward_risk",          # the signal's own R:R
    "stop_atr",             # stop distance in ATR units
]


@dataclass
class FeatureRow:
    """One training example: features, plus the metadata needed to label and
    validate it without leaking the future into the past."""

    ts: datetime
    symbol: str
    strategy: str
    side: Side
    bar_index: int
    values: np.ndarray
    signal: Optional[Signal] = None

    def as_dict(self) -> Dict[str, float]:
        return dict(zip(FEATURE_NAMES, self.values.tolist()))


def _safe(x: float, default: float = 0.0) -> float:
    return default if x is None or x != x or np.isinf(x) else float(x)


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def extract(
    cache: IndicatorCache,
    i: int,
    signal: Signal,
    instrument: Instrument,
    window_end_local: Optional[tuple[str, int]] = None,
) -> FeatureRow:
    """Build the feature vector for `signal`, which fired on bar `i`.

    Only cache values at indices <= i are read.
    """
    sign = float(signal.side.sign)
    close = float(cache.close[i])
    atr = _safe(cache.at("atr", i), 0.0)
    atr = atr if atr > 0 else max(instrument.tick_size, 1e-9)

    # --- trend and direction ---
    adx = _safe(cache.at("adx", i))
    di_spread = (_safe(cache.at("plus_di", i)) - _safe(cache.at("minus_di", i))) * sign

    # --- volatility ---
    atr_pct = atr / close if close > 0 else 0.0
    atr_series = cache.series.get("atr")
    lookback = atr_series[max(0, i - 49): i + 1] if atr_series is not None else np.array([atr])
    atr_mean = float(np.nanmean(lookback)) if len(lookback) else atr
    atr_expansion = atr / atr_mean if atr_mean > 0 else 1.0

    # --- moving average structure, in ATR units so it is scale-free ---
    ema_fast = _safe(cache.at("ema_fast", i), close)
    ema_slow = _safe(cache.at("ema_slow", i), close)
    ema_trend = _safe(cache.at("ema_trend", i), close)
    ema_fast_slow = (ema_fast - ema_slow) / atr * sign
    ema_slow_trend = (ema_slow - ema_trend) / atr * sign
    close_vs_trend = (close - ema_trend) / atr * sign

    # --- oscillator, oriented so "good for this trade" is always high ---
    rsi = _safe(cache.at("rsi", i), 50.0)
    rsi_directional = rsi if signal.side is Side.LONG else 100.0 - rsi

    vwap = _safe(cache.at("vwap", i), close)
    vwap_distance = (close - vwap) / atr * sign

    # --- participation and bar shape ---
    avg_vol = _safe(cache.at("avg_volume", i), 0.0)
    volume = float(cache.volume[i])
    volume_ratio = volume / avg_vol if avg_vol > 0 else 1.0

    high, low, open_ = float(cache.high[i]), float(cache.low[i]), float(cache.open[i])
    bar_range = high - low
    if bar_range > 0:
        body_fraction = abs(close - open_) / bar_range
        upper_wick = (high - max(close, open_)) / bar_range
        lower_wick = (min(close, open_) - low) / bar_range
    else:
        body_fraction = upper_wick = lower_wick = 0.0
    # Orient wicks to the trade: "rejection against me" is what matters.
    if signal.side is Side.SHORT:
        upper_wick, lower_wick = lower_wick, upper_wick

    # --- momentum over two horizons ---
    ret_5 = (close - float(cache.close[max(0, i - 5)])) / atr * sign
    ret_20 = (close - float(cache.close[max(0, i - 20)])) / atr * sign

    # --- position within the recent range ---
    hi_20 = float(np.max(cache.high[max(0, i - 19): i + 1]))
    lo_20 = float(np.min(cache.low[max(0, i - 19): i + 1]))
    span = hi_20 - lo_20
    donchian_position = ((close - lo_20) / span if span > 0 else 0.5)
    if signal.side is Side.SHORT:
        donchian_position = 1.0 - donchian_position

    # --- run length of same-direction closes ---
    consec = 0
    for k in range(i, max(0, i - 10), -1):
        if k == 0:
            break
        direction = 1 if cache.close[k] > cache.close[k - 1] else -1
        if consec == 0 or (consec > 0) == (direction > 0):
            consec += direction
        else:
            break
    consec_bars = float(consec) * sign

    # --- clock ---
    tz = ZoneInfo(instrument.session_tz)
    local = signal.ts.astimezone(tz)
    session_open_min = instrument.session_open.hour * 60 + instrument.session_open.minute
    minutes_into = (local.hour * 60 + local.minute) - session_open_min
    if minutes_into < 0:
        minutes_into += 24 * 60

    minutes_to_end = 0.0
    if window_end_local is not None:
        wtz, end_minutes = window_end_local
        wlocal = signal.ts.astimezone(ZoneInfo(wtz))
        minutes_to_end = end_minutes - (wlocal.hour * 60 + wlocal.minute)

    values = np.array(
        [
            sign,
            _clip(adx, 0, 100),
            _clip(di_spread, -100, 100),
            _clip(atr_pct, 0, 1),
            _clip(atr_expansion, 0, 10),
            _clip(ema_fast_slow, -20, 20),
            _clip(ema_slow_trend, -20, 20),
            _clip(close_vs_trend, -20, 20),
            _clip(rsi_directional, 0, 100),
            _clip(vwap_distance, -20, 20),
            _clip(volume_ratio, 0, 20),
            _clip(body_fraction, 0, 1),
            _clip(upper_wick, 0, 1),
            _clip(lower_wick, 0, 1),
            _clip(ret_5, -30, 30),
            _clip(ret_20, -30, 30),
            _clip(donchian_position, 0, 1),
            _clip(consec_bars, -10, 10),
            _clip(minutes_into, 0, 1440),
            _clip(minutes_to_end, -1440, 1440),
            _clip(signal.reward_risk, 0, 20),
            _clip(signal.stop_distance / atr, 0, 20),
        ],
        dtype=float,
    )
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)

    return FeatureRow(
        ts=signal.ts,
        symbol=signal.symbol,
        strategy=signal.strategy,
        side=signal.side,
        bar_index=i,
        values=values,
        signal=signal,
    )
