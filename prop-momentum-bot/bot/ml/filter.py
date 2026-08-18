"""ML signal filter: the integration point.

Wraps a strategy. The strategy still decides WHERE a setup exists; the model
decides whether this particular instance is worth taking. That separation is
deliberate — a model that generates entries from scratch has to be right about
direction, timing and geometry all at once, while a model that filters an
existing rule only has to rank quality, which is a far easier problem on a few
hundred noisy samples.

The filter fails SAFE in both directions:

* No model loaded -> pass everything through. The bot degrades to the rule-based
  strategy rather than refusing to trade.
* Model present but the feature vector is malformed -> reject that signal and
  record why. A scoring error must never be silently treated as a high score.

It sits BEFORE the risk guard, never instead of it. The guard is still the only
thing that authorises an order, so a bug here can cost opportunity but cannot
cost risk-limit compliance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

from ..config import Instrument, TradingWindow
from ..data.types import Signal
from ..signals.base import IndicatorCache, Strategy
from .features import FEATURE_NAMES, extract
from .models import Classifier
from .regime import RegimeModel, regime_features


@dataclass
class FilterStats:
    seen: int = 0
    passed: int = 0
    blocked_by_model: int = 0
    blocked_by_regime: int = 0
    errors: int = 0
    scores: List[float] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.seen if self.seen else 0.0

    def summary(self) -> str:
        if not self.seen:
            return "filter saw no signals"
        mean_score = float(np.mean(self.scores)) if self.scores else 0.0
        return (
            f"{self.seen} signals -> {self.passed} passed ({self.pass_rate:.0%}); "
            f"model blocked {self.blocked_by_model}, regime blocked "
            f"{self.blocked_by_regime}, errors {self.errors}; mean score {mean_score:.3f}"
        )


class MLSignalFilter:
    """Scores signals and drops the ones below threshold."""

    def __init__(
        self,
        model: Optional[Classifier] = None,
        threshold: float = 0.5,
        regime_model: Optional[RegimeModel] = None,
        allowed_regimes: Optional[Sequence[int]] = None,
        window: Optional[TradingWindow] = None,
        feature_names: Optional[Sequence[str]] = None,
    ) -> None:
        self.model = model
        self.threshold = threshold
        self.regime_model = regime_model
        self.allowed_regimes = set(allowed_regimes) if allowed_regimes is not None else None
        self.window = window
        self.stats = FilterStats()

        expected = list(feature_names or FEATURE_NAMES)
        if model is not None and getattr(model, "feature_names", None):
            if list(model.feature_names) != expected:
                raise ValueError(
                    "model was trained on a different feature set than this build "
                    f"produces ({len(model.feature_names)} vs {len(expected)} features). "
                    "Retrain the model — scoring across mismatched feature orders "
                    "produces confident nonsense."
                )
        self._regime_cache: Dict[int, np.ndarray] = {}

    @property
    def active(self) -> bool:
        return self.model is not None or self.regime_model is not None

    def score(
        self,
        cache: IndicatorCache,
        i: int,
        signal: Signal,
        instrument: Instrument,
    ) -> Optional[float]:
        """P(this signal reaches target before stop), or None if no model."""
        if self.model is None:
            return None
        window_end = None
        if self.window is not None and self.window.enabled:
            window_end = (self.window.timezone, self.window.end.hour * 60 + self.window.end.minute)
        row = extract(cache, i, signal, instrument, window_end)
        return float(self.model.predict_proba(row.values.reshape(1, -1))[0])

    def allows(
        self,
        cache: IndicatorCache,
        i: int,
        signal: Signal,
        instrument: Instrument,
    ) -> tuple[bool, str]:
        """Decide whether this signal survives. Returns (allowed, reason)."""
        self.stats.seen += 1

        if self.regime_model is not None and self.allowed_regimes is not None:
            try:
                regimes = self._regimes_for(cache)
                current = int(regimes[i]) if i < len(regimes) else -1
                if current >= 0 and current not in self.allowed_regimes:
                    self.stats.blocked_by_regime += 1
                    name = self.regime_model.names.get(current, str(current))
                    return False, f"regime '{name}' not in the allowed set"
            except Exception as exc:  # noqa: BLE001 - never let scoring crash the loop
                self.stats.errors += 1
                return False, f"regime scoring failed: {exc}"

        if self.model is None:
            self.stats.passed += 1
            return True, "no model — rule-based signal passed through"

        try:
            p = self.score(cache, i, signal, instrument)
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            # Fail closed on a scoring error: an unscored signal is unknown,
            # and unknown must not be treated as good.
            return False, f"model scoring failed: {exc}"

        self.stats.scores.append(p)
        if p < self.threshold:
            self.stats.blocked_by_model += 1
            return False, f"model score {p:.3f} below threshold {self.threshold:.2f}"

        self.stats.passed += 1
        return True, f"model score {p:.3f}"

    def _regimes_for(self, cache: IndicatorCache) -> np.ndarray:
        key = id(cache)
        if key not in self._regime_cache:
            feats = regime_features(cache.bars)
            self._regime_cache[key] = self.regime_model.predict(feats)
        return self._regime_cache[key]


class FilteredStrategy(Strategy):
    """A strategy with an ML filter attached.

    Presents the same `on_bar` interface, so the backtest and live loop need no
    knowledge of whether a model is in play.
    """

    def __init__(self, inner: Strategy, signal_filter: MLSignalFilter) -> None:
        # Deliberately not calling super().__init__: this wrapper borrows the
        # inner strategy's already-resolved params and instrument.
        self.inner = inner
        self.filter = signal_filter
        self.params = inner.params
        self.instrument = inner.instrument
        self.name = inner.name
        self.rejected: List[tuple[Signal, str]] = []

    def build_cache(self, bars) -> IndicatorCache:
        return self.inner.build_cache(bars)

    def on_bar(self, cache: IndicatorCache, i: int) -> Optional[Signal]:
        signal = self.inner.on_bar(cache, i)
        if signal is None:
            return None
        if not self.filter.active:
            return signal
        allowed, reason = self.filter.allows(cache, i, signal, self.instrument)
        if not allowed:
            self.rejected.append((signal, reason))
            return None
        signal.meta["ml_reason"] = reason
        return signal
