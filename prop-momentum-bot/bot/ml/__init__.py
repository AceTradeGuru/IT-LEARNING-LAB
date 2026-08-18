"""Machine learning layer.

Scope, deliberately narrow: this does NOT predict price. It does two things
that are tractable on a few hundred noisy samples —

  1. **Trade filtering (meta-labelling).** Given that a rule-based strategy has
     fired, estimate P(this one reaches target before stop) and drop the weak
     ones. The strategy still decides where setups exist.
  2. **Regime detection.** Cluster market conditions over time so the bot can
     stand down in the conditions where momentum does not work.

Everything is validated with purged walk-forward splits and a label-shuffling
permutation test, because on this much data a pipeline can manufacture a
convincing AUC out of pure noise.
"""

from .dataset import Dataset, build_dataset
from .features import FEATURE_NAMES, extract
from .filter import FilteredStrategy, MLSignalFilter
from .labeling import Label, label_signal
from .models import (
    Classifier, GradientBoostedTrees, LogisticRegression, load_model, save_model,
)
from .regime import RegimeModel, regime_features
from .validation import (
    ThresholdRow, WalkForwardResult, permutation_test, roc_auc,
    threshold_sweep, walk_forward,
)

__all__ = [
    "Dataset", "build_dataset", "FEATURE_NAMES", "extract",
    "MLSignalFilter", "FilteredStrategy", "Label", "label_signal",
    "Classifier", "LogisticRegression", "GradientBoostedTrees", "save_model", "load_model",
    "RegimeModel", "regime_features",
    "walk_forward", "WalkForwardResult", "threshold_sweep", "ThresholdRow",
    "permutation_test", "roc_auc",
]
