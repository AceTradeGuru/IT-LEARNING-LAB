"""Pullback scalp — the 1-minute engine.

This is still a momentum strategy: it never fades a move. It waits for a trend
to establish, lets price retrace to the fast EMA, and enters when the retrace
FAILS and price resumes. The distinction that matters:

    a shallow pullback that resumes  -> continuation, tradeable
    a deep pullback                  -> the trend is breaking, stand aside

`pullback_max_atr` enforces that distinction. A retrace deeper than 1 ATR from
the swing extreme is not a pullback, it is a reversal, and taking it is how
scalpers turn a good day into a bad one.

Because scalps are small and frequent, they are also where commission and
spread do the most damage — the backtest charges both on every round turn, and
the report shows gross vs net so the cost drag is impossible to miss.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from ..data.types import Side, Signal
from .base import IndicatorCache, Strategy

REQUIRED = ("ema_fast", "ema_slow", "ema_trend", "atr", "adx", "rsi", "vwap")


class ScalpPullback(Strategy):
    name = "scalp_pullback"

    def on_bar(self, cache: IndicatorCache, i: int) -> Optional[Signal]:
        if i < 3 or not cache.ready(i, REQUIRED):
            return None
        if not self._volatility_ok(cache, i) or not self._adx_ok(cache, i):
            return None

        bias = self._trend_bias(cache, i)
        if bias is None:
            return None

        atr = cache.at("atr", i)
        close = float(cache.close[i])
        ema_fast = cache.at("ema_fast", i)
        vwap = cache.at("vwap", i)
        rsi = cache.at("rsi", i)

        # VWAP side filter: intraday institutional flow reference. Longs only
        # above it, shorts only below.
        if self.params.get("vwap_side_filter", True) and vwap == vwap:
            if bias is Side.LONG and close < vwap:
                return None
            if bias is Side.SHORT and close > vwap:
                return None

        if bias is Side.LONG and rsi < self.params.getf("rsi_long_min", 50.0):
            return None
        if bias is Side.SHORT and rsi > self.params.getf("rsi_short_max", 50.0):
            return None

        prev, prev2 = cache.bars[i - 1], cache.bars[i - 2]
        bar = cache.bars[i]
        max_pullback = self.params.getf("pullback_max_atr", 1.0) * atr

        if bias is Side.LONG:
            # A pullback happened if either of the last two bars traded down
            # into the fast EMA...
            touched = min(prev.low, prev2.low) <= ema_fast + 0.25 * atr
            # ...and it stayed shallow relative to the recent swing high.
            swing_high = float(np.max(cache.high[max(0, i - 10):i]))
            depth = swing_high - min(prev.low, prev2.low)
            shallow = depth <= max_pullback
            # Resumption: this bar takes out the pullback bar's high and closes strong.
            resumed = bar.close > max(prev.high, prev2.high) and bar.close > bar.open
            if touched and shallow and resumed:
                return self._make_signal(
                    cache, i, Side.LONG, bar.close, atr,
                    reason=f"pullback {depth / atr:.2f} ATR held, resumed above {prev.high:.5f}",
                    pullback_atr=depth / atr if atr else 0.0,
                )

        else:
            touched = max(prev.high, prev2.high) >= ema_fast - 0.25 * atr
            swing_low = float(np.min(cache.low[max(0, i - 10):i]))
            depth = max(prev.high, prev2.high) - swing_low
            shallow = depth <= max_pullback
            resumed = bar.close < min(prev.low, prev2.low) and bar.close < bar.open
            if touched and shallow and resumed:
                return self._make_signal(
                    cache, i, Side.SHORT, bar.close, atr,
                    reason=f"pullback {depth / atr:.2f} ATR held, resumed below {prev.low:.5f}",
                    pullback_atr=depth / atr if atr else 0.0,
                )

        return None
