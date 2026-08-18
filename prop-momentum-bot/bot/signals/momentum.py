"""Momentum breakout — the 5-minute engine.

Thesis: markets spend most of the day going nowhere and occasionally expand.
The money is in the expansion, and the cost of being wrong is small if you only
take breakouts that arrive with confirmation. So this strategy stacks four
independent filters and requires all of them:

    1. Trend bias   — EMA 9/21/50 stacked in one direction. No counter-trend.
    2. Trend quality— ADX above a floor. Below it, breakouts are noise that
                      revert, and this is the filter that does the most work.
    3. Range break  — close beyond the prior 20-bar Donchian channel. The
                      channel excludes the current bar (see indicators.donchian)
                      because otherwise every new high "breaks out" of itself.
    4. Participation— bar volume above its 20-bar average. A break on thin
                      volume is the classic trap.

Entry is on the CLOSE of the confirming bar, not intrabar, so the signal can
never use information the bar had not yet produced.
"""

from __future__ import annotations

from typing import Optional

from ..data.types import Side, Signal
from .base import IndicatorCache, Strategy

REQUIRED = ("ema_fast", "ema_slow", "ema_trend", "atr", "adx", "donchian_up", "donchian_dn", "avg_volume")


class MomentumBreakout(Strategy):
    name = "momentum_breakout"

    def on_bar(self, cache: IndicatorCache, i: int) -> Optional[Signal]:
        if not cache.ready(i, REQUIRED):
            return None
        if not self._volatility_ok(cache, i) or not self._adx_ok(cache, i):
            return None

        bias = self._trend_bias(cache, i)
        if bias is None:
            return None

        close = float(cache.close[i])
        atr = cache.at("atr", i)
        up = cache.at("donchian_up", i)
        dn = cache.at("donchian_dn", i)

        # Participation filter.
        avg_vol = cache.at("avg_volume", i)
        vol = float(cache.volume[i])
        surge = self.params.getf("volume_surge_mult", 1.0)
        if avg_vol > 0 and vol < avg_vol * surge:
            return None

        # Directional momentum agreement: DI must confirm the break direction.
        plus_di, minus_di = cache.at("plus_di", i), cache.at("minus_di", i)

        if bias is Side.LONG and close > up and plus_di > minus_di:
            return self._make_signal(
                cache, i, Side.LONG, close, atr,
                reason=f"break {up:.5f} on {vol / avg_vol:.1f}x vol, ADX {cache.at('adx', i):.0f}",
                channel=up, volume_ratio=vol / avg_vol if avg_vol else 0.0,
            )

        if bias is Side.SHORT and close < dn and minus_di > plus_di:
            return self._make_signal(
                cache, i, Side.SHORT, close, atr,
                reason=f"break {dn:.5f} on {vol / avg_vol:.1f}x vol, ADX {cache.at('adx', i):.0f}",
                channel=dn, volume_ratio=vol / avg_vol if avg_vol else 0.0,
            )

        return None
