"""Triple-barrier labelling.

Each signal is followed forward until one of three barriers is touched:

    upper  the strategy's target      -> win
    lower  the strategy's stop        -> loss
    time   the strategy's time stop   -> resolved at whatever price it sits on

The label is what the trade WOULD have done had it been taken, which is the
right question for a filter: the model is not predicting price, it is predicting
whether this particular setup resolves in the strategy's favour.

Two details that decide whether this is honest:

* **Same pessimism as the backtest.** When one bar spans both barriers the stop
  is assumed first. If labelling were optimistic while execution is pessimistic,
  the model would be trained to prefer exactly the trades that lose in reality.
* **The horizon is recorded.** A label depends on bars after the signal, so a
  training example is not "known" until its horizon passes. Validation uses this
  to purge overlapping samples — without it, tomorrow's label leaks into today's
  training set and the model scores brilliantly and earns nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

from ..data.types import Bar, Side, Signal


@dataclass
class Label:
    """Outcome of one signal, plus how far into the future it took to resolve."""

    win: int                 # 1 target first, 0 otherwise
    r_multiple: float        # realised R, ignoring costs
    bars_to_resolve: int
    resolve_index: int       # bar index where the outcome became known
    exit_kind: str           # "target" | "stop" | "time"

    @property
    def is_win(self) -> bool:
        return self.win == 1


def label_signal(
    signal: Signal,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    entry_index: int,
    time_stop_bars: int,
) -> Optional[Label]:
    """Walk forward from `entry_index` until a barrier is touched.

    Returns None when the tape runs out before resolution — an unresolved
    sample must be dropped, never labelled as a loss, because "we don't know"
    and "it lost" are different things and conflating them biases the model.
    """
    n = len(closes)
    entry = signal.entry_price
    stop = signal.stop_price
    target = signal.target_price
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    horizon = min(n - 1, entry_index + max(time_stop_bars, 1))

    for j in range(entry_index + 1, horizon + 1):
        hi, lo = float(highs[j]), float(lows[j])
        if signal.side is Side.LONG:
            hit_stop = lo <= stop
            hit_target = hi >= target
        else:
            hit_stop = hi >= stop
            hit_target = lo <= target

        # Stop wins ties, matching the execution engine's assumption.
        if hit_stop:
            return Label(0, -1.0, j - entry_index, j, "stop")
        if hit_target:
            r = abs(target - entry) / risk
            return Label(1, r, j - entry_index, j, "target")

    if horizon <= entry_index or horizon >= n:
        return None
    if entry_index + time_stop_bars > n - 1:
        return None  # the tape ended before the time stop; outcome unknown

    exit_price = float(closes[horizon])
    r = ((exit_price - entry) * signal.side.sign) / risk
    return Label(1 if r > 0 else 0, r, horizon - entry_index, horizon, "time")


def label_all(
    signals: Sequence[tuple[int, Signal]],
    bars: Sequence[Bar],
    time_stop_bars: int,
) -> List[Optional[Label]]:
    highs = np.array([b.high for b in bars], dtype=float)
    lows = np.array([b.low for b in bars], dtype=float)
    closes = np.array([b.close for b in bars], dtype=float)
    return [
        label_signal(sig, highs, lows, closes, idx, time_stop_bars)
        for idx, sig in signals
    ]


def class_balance(labels: Sequence[Label]) -> tuple[int, int, float]:
    """(wins, losses, win_rate). A filter trained on a wildly imbalanced set
    will happily predict the majority class and look accurate while being
    useless, so the caller needs to see this before trusting anything."""
    wins = sum(1 for l in labels if l.is_win)
    total = len(labels)
    return wins, total - wins, (wins / total if total else 0.0)
