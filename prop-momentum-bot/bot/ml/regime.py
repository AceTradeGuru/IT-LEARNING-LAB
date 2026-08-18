"""Unsupervised market regime detection.

This answers "what kind of market is this, right now?" rather than "where does
price go next". It is the more tractable question, and the more useful one for
a momentum system: momentum strategies do not fail randomly, they fail in
ranges. Knowing which regime you are in tells you when to stand down.

Method: k-means over a small set of scale-free regime features, fit on a
training window and then applied forward. Cluster labels are meaningless
integers, so `describe_regimes` interprets the centroids into names like
"trending / expanding" by where each sits relative to the others.

Two properties that make this usable rather than merely interesting:

* **Causal.** Features at bar i use bars <= i. The model is fit on a training
  window and applied forward; it is never re-fit on the data it labels.
* **Persistent.** Raw k-means labels flicker bar to bar, which would make a
  regime filter thrash. A dwell filter suppresses flips shorter than
  `min_dwell_bars`, so a regime change has to persist to count.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .. import indicators as ind
from ..data.types import Bar

REGIME_FEATURE_NAMES = [
    "adx",              # trend strength
    "atr_pct",          # volatility level
    "atr_expansion",    # volatility relative to its own recent norm
    "trend_extension",  # |fast EMA - trend EMA| / ATR
    "directional_persistence",  # fraction of last 20 bars closing with the trend
    "efficiency_ratio",  # net move / summed move: 1.0 = straight line, 0 = chop
]


def regime_features(bars: Sequence[Bar], atr_period: int = 14) -> np.ndarray:
    """Build the (n_bars, n_features) regime matrix. Rows before warmup are NaN."""
    h = np.array([b.high for b in bars], dtype=float)
    l = np.array([b.low for b in bars], dtype=float)
    c = np.array([b.close for b in bars], dtype=float)
    n = len(c)
    out = np.full((n, len(REGIME_FEATURE_NAMES)), np.nan)
    if n < 60:
        return out

    atr = ind.atr(h, l, c, atr_period)
    adx = ind.adx(h, l, c, 14)[0]
    ema_fast = ind.ema(c, 9)
    ema_trend = ind.ema(c, 50)

    atr_mean = np.full(n, np.nan)
    for i in range(50, n):
        window = atr[max(0, i - 49): i + 1]
        atr_mean[i] = np.nanmean(window)

    for i in range(50, n):
        a = atr[i]
        if a != a or a <= 0 or c[i] <= 0:
            continue
        # Directional persistence: how one-sided the recent closes have been.
        diffs = np.diff(c[max(0, i - 20): i + 1])
        if len(diffs) == 0:
            continue
        ups = float((diffs > 0).mean())
        persistence = abs(ups - 0.5) * 2.0

        # Efficiency ratio: net displacement over summed absolute movement.
        net = abs(c[i] - c[max(0, i - 20)])
        gross = float(np.abs(diffs).sum())
        efficiency = net / gross if gross > 0 else 0.0

        out[i] = [
            adx[i] if adx[i] == adx[i] else 0.0,
            a / c[i],
            a / atr_mean[i] if atr_mean[i] and atr_mean[i] == atr_mean[i] else 1.0,
            abs(ema_fast[i] - ema_trend[i]) / a if ema_fast[i] == ema_fast[i] and ema_trend[i] == ema_trend[i] else 0.0,
            persistence,
            efficiency,
        ]
    return out


@dataclass
class RegimeModel:
    """k-means over regime features, with named clusters."""

    k: int = 4
    seed: int = 11
    min_dwell_bars: int = 10
    centroids: Optional[np.ndarray] = None
    mean: Optional[np.ndarray] = None
    scale: Optional[np.ndarray] = None
    names: Dict[int, str] = field(default_factory=dict)

    # -- fitting --------------------------------------------------------

    def fit(self, features: np.ndarray, max_iter: int = 100) -> "RegimeModel":
        X = features[~np.isnan(features).any(axis=1)]
        if len(X) < self.k * 10:
            raise ValueError(
                f"need at least {self.k * 10} valid rows to fit {self.k} regimes, got {len(X)}"
            )
        self.mean = X.mean(axis=0)
        scale = X.std(axis=0)
        self.scale = np.where(scale < 1e-12, 1.0, scale)
        Xs = (X - self.mean) / self.scale

        rng = np.random.default_rng(self.seed)
        # k-means++ seeding: random init on skewed trading features regularly
        # collapses two centroids onto the same cluster.
        centroids = [Xs[rng.integers(len(Xs))]]
        for _ in range(1, self.k):
            d2 = np.min(
                np.stack([((Xs - c) ** 2).sum(axis=1) for c in centroids]), axis=0
            )
            total = d2.sum()
            probs = d2 / total if total > 0 else np.full(len(Xs), 1 / len(Xs))
            centroids.append(Xs[rng.choice(len(Xs), p=probs)])
        C = np.vstack(centroids)

        for _ in range(max_iter):
            dists = np.stack([((Xs - c) ** 2).sum(axis=1) for c in C], axis=1)
            assign = dists.argmin(axis=1)
            new_C = np.array([
                Xs[assign == j].mean(axis=0) if (assign == j).any() else C[j]
                for j in range(self.k)
            ])
            if np.allclose(new_C, C, atol=1e-8):
                C = new_C
                break
            C = new_C

        self.centroids = C
        self._name_clusters()
        return self

    def _name_clusters(self) -> None:
        """Interpret centroids into readable names.

        Cluster indices are arbitrary, so names come from where each centroid
        sits relative to the others on the two axes that matter for a momentum
        system: how trending it is, and how volatile.
        """
        if self.centroids is None:
            return
        real = self.centroids * self.scale + self.mean
        trend_score = real[:, 0] / 50.0 + real[:, 5]      # adx + efficiency
        vol_score = real[:, 2]                            # atr expansion

        trend_median = float(np.median(trend_score))
        vol_median = float(np.median(vol_score))
        for j in range(self.k):
            trending = trend_score[j] >= trend_median
            volatile = vol_score[j] >= vol_median
            if trending and volatile:
                base = "trending / expanding"
            elif trending:
                base = "trending / quiet"
            elif volatile:
                base = "choppy / volatile"
            else:
                base = "range / quiet"
            self.names[j] = base
        # Disambiguate duplicates so two clusters never share a name.
        seen: Dict[str, int] = {}
        for j in sorted(self.names):
            base = self.names[j]
            seen[base] = seen.get(base, 0) + 1
            if seen[base] > 1:
                self.names[j] = f"{base} #{seen[base]}"

    # -- prediction -----------------------------------------------------

    def predict(self, features: np.ndarray, smooth: bool = True) -> np.ndarray:
        """Regime index per bar; -1 where features are unavailable."""
        if self.centroids is None:
            raise RuntimeError("regime model is not fitted")
        out = np.full(len(features), -1, dtype=int)
        valid = ~np.isnan(features).any(axis=1)
        if not valid.any():
            return out
        Xs = (features[valid] - self.mean) / self.scale
        dists = np.stack([((Xs - c) ** 2).sum(axis=1) for c in self.centroids], axis=1)
        out[valid] = dists.argmin(axis=1)
        return self._apply_dwell(out) if smooth else out

    def _apply_dwell(self, labels: np.ndarray) -> np.ndarray:
        """Suppress regime flips shorter than `min_dwell_bars`.

        Without this the labels flicker between adjacent clusters on almost
        every bar, and a filter keyed to them would thrash.
        """
        if self.min_dwell_bars <= 1:
            return labels
        out = labels.copy()
        current = -1
        run_start = 0
        for i in range(len(out)):
            if out[i] == -1:
                continue
            if current == -1:
                current, run_start = out[i], i
                continue
            if out[i] != current:
                # Look ahead: does the new label actually persist?
                j = i
                while j < len(out) and out[j] == out[i]:
                    j += 1
                if j - i < self.min_dwell_bars:
                    out[i:j] = current       # too brief — treat as noise
                    continue
                current, run_start = out[i], i
        return out

    # -- reporting ------------------------------------------------------

    def describe_regimes(self, features: np.ndarray, labels: np.ndarray) -> str:
        lines = [f"{'regime':<26}{'bars':>8}{'share':>8}{'ADX':>7}{'ATRexp':>8}{'effic':>7}"]
        lines.append("-" * 64)
        total = int((labels >= 0).sum())
        for j in range(self.k):
            mask = labels == j
            count = int(mask.sum())
            if count == 0:
                continue
            rows = features[mask]
            rows = rows[~np.isnan(rows).any(axis=1)]
            if len(rows) == 0:
                continue
            lines.append(
                f"{self.names.get(j, str(j)):<26}{count:>8}"
                f"{100 * count / max(total, 1):>7.1f}%"
                f"{rows[:, 0].mean():>7.1f}{rows[:, 2].mean():>8.2f}{rows[:, 5].mean():>7.2f}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "k": self.k, "seed": self.seed, "min_dwell_bars": self.min_dwell_bars,
            "centroids": self.centroids.tolist() if self.centroids is not None else None,
            "mean": self.mean.tolist() if self.mean is not None else None,
            "scale": self.scale.tolist() if self.scale is not None else None,
            "names": {str(k): v for k, v in self.names.items()},
            "feature_names": REGIME_FEATURE_NAMES,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RegimeModel":
        m = cls(k=d.get("k", 4), seed=d.get("seed", 11), min_dwell_bars=d.get("min_dwell_bars", 10))
        m.centroids = np.array(d["centroids"]) if d.get("centroids") else None
        m.mean = np.array(d["mean"]) if d.get("mean") else None
        m.scale = np.array(d["scale"]) if d.get("scale") else None
        m.names = {int(k): v for k, v in d.get("names", {}).items()}
        return m
