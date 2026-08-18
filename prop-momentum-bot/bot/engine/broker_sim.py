"""Fill simulation.

Backtests flatter you in three specific ways, and each one is corrected here:

1. **Spread.** You buy the ask and sell the bid. Mid-price fills invent money
   that does not exist, and on a 1-minute scalper that error alone can flip a
   losing system into a winner on paper.
2. **Slippage.** Stops are market orders triggered in motion, so they fill
   worse than the trigger price — modelled as a fraction of ATR, scaled up for
   stop exits versus limit exits. Limit targets do NOT get positive slippage.
3. **Gaps.** If a bar opens beyond your stop, you fill at the open, not at the
   stop price. This is the difference that makes overnight risk real.

Every one of these makes results worse, which is the correct direction for a
model whose errors you cannot fully know.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import Instrument
from ..data.types import Bar, ExitReason, Side


@dataclass
class FillModel:
    """Cost assumptions. Defaults are deliberately pessimistic."""

    stop_slippage_atr: float = 0.10     # market-order slippage on stop exits
    entry_slippage_atr: float = 0.03    # crossing the spread on entry
    use_spread: bool = True
    gap_fills_at_open: bool = True

    def entry_fill(self, instrument: Instrument, side: Side, price: float, atr: float) -> float:
        """Price actually paid to open, worse than the signal price."""
        adverse = self.entry_slippage_atr * atr
        if self.use_spread:
            adverse += instrument.spread_points / 2.0
        return instrument.round_price(price + side.sign * adverse)

    def exit_fill(
        self,
        instrument: Instrument,
        side: Side,
        intended: float,
        bar: Bar,
        atr: float,
        reason: ExitReason,
    ) -> float:
        """Price actually received to close."""
        if reason in (ExitReason.STOP, ExitReason.TRAIL, ExitReason.RISK_HALT):
            # Gap through the stop: you get the open, not the stop.
            if self.gap_fills_at_open:
                if side is Side.LONG and bar.open < intended:
                    return instrument.round_price(bar.open)
                if side is Side.SHORT and bar.open > intended:
                    return instrument.round_price(bar.open)
            adverse = self.stop_slippage_atr * atr
            if self.use_spread:
                adverse += instrument.spread_points / 2.0
            # Exiting a long means selling: fill BELOW the stop.
            return instrument.round_price(intended - side.sign * adverse)

        if reason is ExitReason.TARGET:
            # A resting limit order fills at its price, no better.
            return instrument.round_price(intended)

        # Time stop / session close: market order at the close, pay the spread.
        adverse = instrument.spread_points / 2.0 if self.use_spread else 0.0
        return instrument.round_price(bar.close - side.sign * adverse)


def costs_for(instrument: Instrument, size: float, price: float) -> float:
    """Round-turn commission and fees for a position of `size`."""
    return instrument.cost_for(size, price)
