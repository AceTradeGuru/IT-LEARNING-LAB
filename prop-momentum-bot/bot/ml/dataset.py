"""Turning a strategy run into a supervised dataset.

Replays each strategy over its own timeframe, collects every signal it would
have produced (whether or not the risk guard would have taken it), extracts
features, and labels the outcome.

Signals the guard would have vetoed are deliberately INCLUDED. The model's job
is to rank setup quality, and training only on trades that survived the guard
would restrict it to an already-filtered slice — it would never learn what a
bad setup looks like.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..config import BotConfig, Instrument
from ..data.feed import resample
from ..data.types import Bar, Signal
from ..signals import make_strategy
from .features import FEATURE_NAMES, FeatureRow, extract
from .labeling import Label, label_all


@dataclass
class Dataset:
    """Feature matrix, labels, and the metadata validation needs."""

    X: np.ndarray                    # (n_samples, n_features)
    y: np.ndarray                    # (n_samples,) 0/1
    r: np.ndarray                    # realised R per sample
    timestamps: List[datetime]
    resolve_ts: List[datetime]       # when each label became known
    symbols: List[str]
    strategies: List[str]
    feature_names: List[str] = field(default_factory=lambda: list(FEATURE_NAMES))

    def __len__(self) -> int:
        return len(self.y)

    @property
    def win_rate(self) -> float:
        return float(self.y.mean()) if len(self.y) else 0.0

    def subset(self, idx: Sequence[int]) -> "Dataset":
        idx = list(idx)
        return Dataset(
            X=self.X[idx],
            y=self.y[idx],
            r=self.r[idx],
            timestamps=[self.timestamps[i] for i in idx],
            resolve_ts=[self.resolve_ts[i] for i in idx],
            symbols=[self.symbols[i] for i in idx],
            strategies=[self.strategies[i] for i in idx],
            feature_names=list(self.feature_names),
        )

    def filter_strategy(self, name: str) -> "Dataset":
        return self.subset([i for i, s in enumerate(self.strategies) if s == name])

    def describe(self) -> str:
        if not len(self):
            return "empty dataset"
        by_strat: Dict[str, int] = {}
        for s in self.strategies:
            by_strat[s] = by_strat.get(s, 0) + 1
        return (
            f"{len(self)} samples, {len(self.feature_names)} features, "
            f"win rate {self.win_rate:.1%}, "
            f"{self.timestamps[0]:%Y-%m-%d} to {self.timestamps[-1]:%Y-%m-%d}, "
            f"by strategy: {by_strat}"
        )


def build_dataset(
    config: BotConfig,
    data: Dict[str, List[Bar]],
    strategies: Optional[Sequence[str]] = None,
) -> Dataset:
    """Replay strategies over `data` and produce a labelled dataset."""
    names = list(strategies) if strategies else [
        n for n, p in config.strategies.items() if p.enabled
    ]
    window = config.risk.trading_window
    window_end = None
    if window is not None and window.enabled:
        window_end = (window.timezone, window.end.hour * 60 + window.end.minute)

    rows: List[FeatureRow] = []
    labels: List[Label] = []
    resolve_times: List[datetime] = []

    for symbol, base_bars in data.items():
        if symbol not in config.instruments:
            continue
        instrument = config.instruments[symbol]
        for name in names:
            params = config.strategies[name]
            bars = resample(base_bars, params.timeframe_minutes) if params.timeframe_minutes > 1 else base_bars
            strat = make_strategy(name, params, instrument)
            cache = strat.build_cache(bars)

            found: List[Tuple[int, Signal]] = []
            for i in range(len(bars)):
                sig = strat.on_bar(cache, i)
                if sig is not None:
                    found.append((i, sig))
            if not found:
                continue

            time_stop = strat.params.geti("time_stop_bars", 20)
            outcomes = label_all(found, bars, time_stop)

            for (i, sig), label in zip(found, outcomes):
                if label is None:
                    continue  # unresolved on this tape — cannot be labelled
                rows.append(extract(cache, i, sig, instrument, window_end))
                labels.append(label)
                resolve_times.append(bars[label.resolve_index].ts)

    if not rows:
        return Dataset(
            X=np.zeros((0, len(FEATURE_NAMES))), y=np.zeros(0), r=np.zeros(0),
            timestamps=[], resolve_ts=[], symbols=[], strategies=[],
        )

    order = np.argsort([r.ts.timestamp() for r in rows])
    rows = [rows[i] for i in order]
    labels = [labels[i] for i in order]
    resolve_times = [resolve_times[i] for i in order]

    return Dataset(
        X=np.vstack([r.values for r in rows]),
        y=np.array([l.win for l in labels], dtype=float),
        r=np.array([l.r_multiple for l in labels], dtype=float),
        timestamps=[r.ts for r in rows],
        resolve_ts=resolve_times,
        symbols=[r.symbol for r in rows],
        strategies=[r.strategy for r in rows],
    )
