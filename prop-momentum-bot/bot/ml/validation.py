"""Purged walk-forward validation.

Standard k-fold cross-validation is invalid on trading data, in two separate
ways, and both of them inflate scores dramatically:

1. **Random folds train on the future.** A model that has seen next month
   scores brilliantly and earns nothing. Only walk-forward — train on the past,
   test on the future — answers the question you actually care about.

2. **Labels overlap the test set.** A signal at 10:00 may not resolve until
   10:40. If the test fold starts at 10:20, that training label was partly
   determined by bars inside the test fold. Dropping those samples is
   *purging*; skipping a further gap afterwards is an *embargo*. Without both,
   information leaks backwards across the fold boundary.

The economic metrics matter more than the statistical ones. An AUC of 0.55 that
lifts expectancy from -0.05R to +0.12R is worth having; an AUC of 0.65 that
leaves expectancy unchanged is noise wearing a lab coat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .dataset import Dataset
from .models import Classifier


def roc_auc(y: np.ndarray, scores: np.ndarray) -> float:
    """AUC via rank statistic. 0.5 is a coin flip."""
    pos, neg = y == 1, y == 0
    n_pos, n_neg = int(pos.sum()), int(neg.sum())
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1)
    # Average ranks within ties, otherwise identical scores bias the statistic.
    _, inverse, counts = np.unique(scores, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inverse, ranks)
    ranks = (sums / counts)[inverse]
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def brier_score(y: np.ndarray, p: np.ndarray) -> float:
    """Mean squared error of the probabilities — measures calibration, which
    matters because the filter thresholds on the probability itself."""
    return float(np.mean((p - y) ** 2))


@dataclass
class FoldResult:
    fold: int
    n_train: int
    n_test: int
    n_purged: int
    auc: float
    accuracy: float
    brier: float
    base_rate: float
    probabilities: np.ndarray
    y_true: np.ndarray
    r_values: np.ndarray


@dataclass
class WalkForwardResult:
    folds: List[FoldResult] = field(default_factory=list)

    @property
    def n_samples(self) -> int:
        return sum(f.n_test for f in self.folds)

    @property
    def mean_auc(self) -> float:
        return float(np.mean([f.auc for f in self.folds])) if self.folds else 0.5

    @property
    def total_purged(self) -> int:
        return sum(f.n_purged for f in self.folds)

    def pooled(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """All out-of-sample predictions concatenated across folds."""
        if not self.folds:
            return np.zeros(0), np.zeros(0), np.zeros(0)
        return (
            np.concatenate([f.probabilities for f in self.folds]),
            np.concatenate([f.y_true for f in self.folds]),
            np.concatenate([f.r_values for f in self.folds]),
        )

    def pooled_auc(self) -> float:
        p, y, _ = self.pooled()
        return roc_auc(y, p) if len(y) else 0.5


def walk_forward(
    dataset: Dataset,
    make_model: Callable[[], Classifier],
    n_folds: int = 5,
    embargo_minutes: int = 60,
    min_train: int = 80,
) -> WalkForwardResult:
    """Train on the past, test on the next block, purging label overlap."""
    n = len(dataset)
    result = WalkForwardResult()
    if n < min_train + n_folds:
        return result

    ts = np.array([t.timestamp() for t in dataset.timestamps])
    resolve = np.array([t.timestamp() for t in dataset.resolve_ts])
    embargo = embargo_minutes * 60

    # Equal-sized test blocks over the back portion of the timeline.
    first_test = max(min_train, n // (n_folds + 1))
    edges = np.linspace(first_test, n, n_folds + 1).astype(int)

    for k in range(n_folds):
        lo, hi = edges[k], edges[k + 1]
        if hi - lo < 5:
            continue
        test_idx = np.arange(lo, hi)
        test_start = ts[lo]

        # Purge: a training label that resolved at or after the test fold began
        # was partly determined by test-fold bars. Embargo: skip a gap before
        # the boundary as well.
        candidate = np.arange(0, lo)
        keep = resolve[candidate] < (test_start - embargo)
        train_idx = candidate[keep]
        purged = int((~keep).sum())

        if len(train_idx) < min_train:
            continue
        y_train = dataset.y[train_idx]
        if len(np.unique(y_train)) < 2:
            continue  # a single-class training fold teaches nothing

        model = make_model()
        model.fit(dataset.X[train_idx], y_train)
        p = model.predict_proba(dataset.X[test_idx])
        y_test = dataset.y[test_idx]

        result.folds.append(
            FoldResult(
                fold=k,
                n_train=len(train_idx),
                n_test=len(test_idx),
                n_purged=purged,
                auc=roc_auc(y_test, p),
                accuracy=float(((p > 0.5) == y_test).mean()),
                brier=brier_score(y_test, p),
                base_rate=float(y_test.mean()),
                probabilities=p,
                y_true=y_test,
                r_values=dataset.r[test_idx],
            )
        )

    return result


@dataclass
class ThresholdRow:
    threshold: float
    trades_kept: int
    kept_fraction: float
    win_rate: float
    mean_r: float
    total_r: float
    lift_vs_baseline: float


def threshold_sweep(
    probabilities: np.ndarray,
    y: np.ndarray,
    r: np.ndarray,
    thresholds: Optional[Sequence[float]] = None,
) -> List[ThresholdRow]:
    """What filtering at each probability threshold does to the economics.

    This is the table that decides whether the model is worth using. A filter
    that keeps 15% of trades at a slightly better win rate is usually worse
    than no filter, because it trades so rarely that variance dominates.
    """
    if thresholds is None:
        thresholds = [0.0, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
    baseline = float(r.mean()) if len(r) else 0.0
    rows: List[ThresholdRow] = []
    for t in thresholds:
        mask = probabilities >= t
        kept = int(mask.sum())
        if kept == 0:
            rows.append(ThresholdRow(t, 0, 0.0, 0.0, 0.0, 0.0, -baseline))
            continue
        rows.append(
            ThresholdRow(
                threshold=t,
                trades_kept=kept,
                kept_fraction=kept / len(y),
                win_rate=float(y[mask].mean()),
                mean_r=float(r[mask].mean()),
                total_r=float(r[mask].sum()),
                lift_vs_baseline=float(r[mask].mean()) - baseline,
            )
        )
    return rows


def permutation_test(
    dataset: Dataset,
    make_model: Callable[[], Classifier],
    n_folds: int = 5,
    n_permutations: int = 20,
    embargo_minutes: int = 60,
    seed: int = 3,
) -> Tuple[float, float, float]:
    """Is the measured AUC better than the same pipeline on shuffled labels?

    Returns (real_auc, mean_shuffled_auc, p_value). With a few hundred noisy
    samples and twenty-odd features, a pipeline can manufacture AUC ~0.55 from
    pure noise. Shuffling the labels and re-running the identical procedure is
    the cheapest way to find out whether the number means anything.

    A p-value above ~0.1 means: do not trade this model.
    """
    rng = np.random.default_rng(seed)
    real = walk_forward(dataset, make_model, n_folds, embargo_minutes).pooled_auc()

    shuffled: List[float] = []
    for _ in range(n_permutations):
        permuted = dataset.subset(range(len(dataset)))
        permuted.y = rng.permutation(dataset.y)
        shuffled.append(walk_forward(permuted, make_model, n_folds, embargo_minutes).pooled_auc())

    shuffled_arr = np.array(shuffled) if shuffled else np.array([0.5])
    p_value = float((shuffled_arr >= real).sum() + 1) / (len(shuffled_arr) + 1)
    return real, float(shuffled_arr.mean()), p_value
