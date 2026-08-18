"""Classifiers, implemented in NumPy.

No scikit-learn dependency: the engine's whole dependency surface is numpy and
PyYAML, and a trading bot is a bad place to add a large transitive tree. Both
models here are small enough to read end to end, which also means their
behaviour on a tiny, noisy, imbalanced trading dataset is inspectable rather
than assumed.

Two models, deliberately:

* `LogisticRegression` — linear, L2-regularised. Its coefficients are directly
  interpretable, so it answers "which features actually matter" in a way a tree
  ensemble does not. Use it first; if it finds nothing, be very suspicious of a
  nonlinear model that does.
* `GradientBoostedTrees` — shallow regression trees on the logistic gradient.
  Captures interactions (e.g. "high ADX only helps in the first hour"), at the
  cost of overfitting far more eagerly on small samples.

Both standardise features internally, because trading features differ by orders
of magnitude (ADX ~25, atr_pct ~0.002) and unscaled gradient descent on that
converges to nonsense.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np


def sigmoid(z: np.ndarray) -> np.ndarray:
    # Split by sign to avoid overflow in exp for large |z|.
    out = np.empty_like(z, dtype=float)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


class Classifier(ABC):
    """Predicts P(win) for a feature matrix."""

    feature_names: List[str]

    @abstractmethod
    def fit(self, X: np.ndarray, y: np.ndarray, sample_weight: Optional[np.ndarray] = None) -> "Classifier":
        ...

    @abstractmethod
    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        ...

    @abstractmethod
    def to_dict(self) -> dict:
        ...

    def importances(self) -> Dict[str, float]:
        return {}


@dataclass
class Standardizer:
    mean: Optional[np.ndarray] = None
    scale: Optional[np.ndarray] = None

    def fit(self, X: np.ndarray) -> "Standardizer":
        self.mean = X.mean(axis=0)
        scale = X.std(axis=0)
        # A constant feature has zero variance; dividing by it produces NaN and
        # poisons every downstream weight.
        self.scale = np.where(scale < 1e-12, 1.0, scale)
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        if self.mean is None or self.scale is None:
            return X
        return (X - self.mean) / self.scale

    def to_dict(self) -> dict:
        return {
            "mean": self.mean.tolist() if self.mean is not None else None,
            "scale": self.scale.tolist() if self.scale is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Standardizer":
        s = cls()
        s.mean = np.array(d["mean"]) if d.get("mean") is not None else None
        s.scale = np.array(d["scale"]) if d.get("scale") is not None else None
        return s


class LogisticRegression(Classifier):
    """L2-regularised logistic regression, fit by gradient descent."""

    def __init__(
        self,
        learning_rate: float = 0.1,
        epochs: int = 600,
        l2: float = 1.0,
        feature_names: Optional[Sequence[str]] = None,
    ) -> None:
        self.learning_rate = learning_rate
        self.epochs = epochs
        self.l2 = l2
        self.feature_names = list(feature_names or [])
        self.weights: Optional[np.ndarray] = None
        self.bias = 0.0
        self.scaler = Standardizer()

    def fit(self, X: np.ndarray, y: np.ndarray, sample_weight: Optional[np.ndarray] = None) -> "LogisticRegression":
        Xs = self.scaler.fit(X).transform(X)
        n, d = Xs.shape
        w = np.zeros(d)
        b = 0.0
        sw = np.ones(n) if sample_weight is None else np.asarray(sample_weight, dtype=float)
        sw = sw / sw.sum() * n

        for _ in range(self.epochs):
            p = sigmoid(Xs @ w + b)
            err = (p - y) * sw
            grad_w = Xs.T @ err / n + (self.l2 / n) * w
            grad_b = err.sum() / n
            w -= self.learning_rate * grad_w
            b -= self.learning_rate * grad_b

        self.weights, self.bias = w, float(b)
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self.weights is None:
            raise RuntimeError("model is not fitted")
        return sigmoid(self.scaler.transform(X) @ self.weights + self.bias)

    def importances(self) -> Dict[str, float]:
        """Absolute standardised coefficients — comparable across features
        precisely because the inputs were standardised first."""
        if self.weights is None:
            return {}
        names = self.feature_names or [f"f{i}" for i in range(len(self.weights))]
        total = np.abs(self.weights).sum() or 1.0
        return {n: float(abs(w) / total) for n, w in zip(names, self.weights)}

    def signed_coefficients(self) -> Dict[str, float]:
        """Direction matters as much as magnitude: a negative weight on
        `volume_ratio` would say the volume filter is backwards."""
        if self.weights is None:
            return {}
        names = self.feature_names or [f"f{i}" for i in range(len(self.weights))]
        return {n: float(w) for n, w in zip(names, self.weights)}

    def to_dict(self) -> dict:
        return {
            "type": "logistic",
            "weights": self.weights.tolist() if self.weights is not None else None,
            "bias": self.bias,
            "scaler": self.scaler.to_dict(),
            "feature_names": self.feature_names,
            "hyper": {"learning_rate": self.learning_rate, "epochs": self.epochs, "l2": self.l2},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LogisticRegression":
        h = d.get("hyper", {})
        m = cls(h.get("learning_rate", 0.1), h.get("epochs", 600), h.get("l2", 1.0), d.get("feature_names"))
        m.weights = np.array(d["weights"]) if d.get("weights") is not None else None
        m.bias = float(d.get("bias", 0.0))
        m.scaler = Standardizer.from_dict(d.get("scaler", {}))
        return m


@dataclass
class _Node:
    feature: int = -1
    threshold: float = 0.0
    value: float = 0.0
    left: Optional["_Node"] = None
    right: Optional["_Node"] = None

    @property
    def is_leaf(self) -> bool:
        return self.left is None

    def to_dict(self) -> dict:
        if self.is_leaf:
            return {"v": self.value}
        return {
            "f": self.feature, "t": self.threshold,
            "l": self.left.to_dict(), "r": self.right.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_Node":
        if "v" in d:
            return cls(value=float(d["v"]))
        return cls(
            feature=int(d["f"]), threshold=float(d["t"]),
            left=cls.from_dict(d["l"]), right=cls.from_dict(d["r"]),
        )


class GradientBoostedTrees(Classifier):
    """Gradient boosting with shallow regression trees on the logistic loss."""

    def __init__(
        self,
        n_estimators: int = 60,
        max_depth: int = 3,
        learning_rate: float = 0.08,
        min_samples_leaf: int = 12,
        subsample: float = 0.85,
        seed: int = 7,
        feature_names: Optional[Sequence[str]] = None,
    ) -> None:
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.min_samples_leaf = min_samples_leaf
        self.subsample = subsample
        self.seed = seed
        self.feature_names = list(feature_names or [])
        self.trees: List[_Node] = []
        self.base_score = 0.0
        self._importances: Dict[int, float] = {}

    # -- tree building --------------------------------------------------

    def _build(self, X: np.ndarray, g: np.ndarray, depth: int) -> _Node:
        n, d = X.shape
        if depth >= self.max_depth or n < 2 * self.min_samples_leaf:
            return _Node(value=float(g.mean()) if n else 0.0)

        best_gain, best_f, best_t = 0.0, -1, 0.0
        parent_sse = float(((g - g.mean()) ** 2).sum())

        for f in range(d):
            col = X[:, f]
            # Quantile candidates rather than every unique value: far cheaper
            # and less prone to splitting on a single outlier.
            qs = np.quantile(col, [0.15, 0.3, 0.45, 0.6, 0.75, 0.9])
            for t in np.unique(qs):
                mask = col <= t
                nl = int(mask.sum())
                if nl < self.min_samples_leaf or n - nl < self.min_samples_leaf:
                    continue
                gl, gr = g[mask], g[~mask]
                sse = float(((gl - gl.mean()) ** 2).sum() + ((gr - gr.mean()) ** 2).sum())
                gain = parent_sse - sse
                if gain > best_gain:
                    best_gain, best_f, best_t = gain, f, float(t)

        if best_f < 0:
            return _Node(value=float(g.mean()))

        self._importances[best_f] = self._importances.get(best_f, 0.0) + best_gain
        mask = X[:, best_f] <= best_t
        return _Node(
            feature=best_f,
            threshold=best_t,
            left=self._build(X[mask], g[mask], depth + 1),
            right=self._build(X[~mask], g[~mask], depth + 1),
        )

    @staticmethod
    def _predict_tree(node: _Node, X: np.ndarray) -> np.ndarray:
        out = np.empty(len(X))
        for i, row in enumerate(X):
            cur = node
            while not cur.is_leaf:
                cur = cur.left if row[cur.feature] <= cur.threshold else cur.right
            out[i] = cur.value
        return out

    # -- API ------------------------------------------------------------

    def fit(self, X: np.ndarray, y: np.ndarray, sample_weight: Optional[np.ndarray] = None) -> "GradientBoostedTrees":
        rng = np.random.default_rng(self.seed)
        n = len(y)
        p0 = float(np.clip(y.mean(), 1e-6, 1 - 1e-6))
        self.base_score = float(np.log(p0 / (1 - p0)))
        f = np.full(n, self.base_score)
        self.trees = []
        self._importances = {}

        for _ in range(self.n_estimators):
            residual = y - sigmoid(f)          # negative gradient of logloss
            if self.subsample < 1.0:
                idx = rng.choice(n, size=max(int(n * self.subsample), self.min_samples_leaf * 2), replace=False)
            else:
                idx = np.arange(n)
            tree = self._build(X[idx], residual[idx], 0)
            self.trees.append(tree)
            f += self.learning_rate * self._predict_tree(tree, X)

        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if not self.trees:
            raise RuntimeError("model is not fitted")
        f = np.full(len(X), self.base_score)
        for tree in self.trees:
            f += self.learning_rate * self._predict_tree(tree, X)
        return sigmoid(f)

    def importances(self) -> Dict[str, float]:
        if not self._importances:
            return {}
        names = self.feature_names or [f"f{i}" for i in range(max(self._importances) + 1)]
        total = sum(self._importances.values()) or 1.0
        out = {names[f]: g / total for f, g in self._importances.items() if f < len(names)}
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def to_dict(self) -> dict:
        return {
            "type": "gbt",
            "base_score": self.base_score,
            "trees": [t.to_dict() for t in self.trees],
            "feature_names": self.feature_names,
            "hyper": {
                "n_estimators": self.n_estimators, "max_depth": self.max_depth,
                "learning_rate": self.learning_rate, "min_samples_leaf": self.min_samples_leaf,
                "subsample": self.subsample, "seed": self.seed,
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GradientBoostedTrees":
        h = d.get("hyper", {})
        m = cls(feature_names=d.get("feature_names"), **h)
        m.base_score = float(d.get("base_score", 0.0))
        m.trees = [_Node.from_dict(t) for t in d.get("trees", [])]
        return m


MODEL_TYPES = {"logistic": LogisticRegression, "gbt": GradientBoostedTrees}


def save_model(model: Classifier, path: str, metadata: Optional[dict] = None) -> None:
    payload = {"model": model.to_dict(), "metadata": metadata or {}}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def load_model(path: str) -> tuple[Classifier, dict]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    d = payload["model"]
    kind = d.get("type")
    if kind not in MODEL_TYPES:
        raise ValueError(f"unknown model type {kind!r}")
    return MODEL_TYPES[kind].from_dict(d), payload.get("metadata", {})
