"""ML layer tests.

The load-bearing ones are the leakage tests and `test_pipeline_detects_an_
injected_edge`. A validation pipeline that reports encouraging numbers on noise
is worse than no pipeline at all, so it has to be shown doing both halves of
its job: finding a pattern that is really there, and refusing to find one that
is not.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from bot.config import BotConfig
from bot.data.feed import resample
from bot.data.synthetic import generate_bars
from bot.data.types import Bar, Side, Signal
from bot.ml.dataset import Dataset, build_dataset
from bot.ml.features import FEATURE_NAMES, extract
from bot.ml.filter import MLSignalFilter
from bot.ml.labeling import label_signal
from bot.ml.models import (
    GradientBoostedTrees, LogisticRegression, load_model, save_model, sigmoid,
)
from bot.ml.regime import RegimeModel, regime_features
from bot.ml.validation import (
    permutation_test, roc_auc, threshold_sweep, walk_forward,
)
from bot.signals import make_strategy

UTC = timezone.utc


@pytest.fixture(scope="module")
def config():
    return BotConfig.load("futures_50k_trailing")


@pytest.fixture(scope="module")
def bars(config):
    return generate_bars(config.instruments["MES"], datetime(2026, 1, 5, tzinfo=UTC), 45, 1, seed=31)


# ----------------------------------------------------------------------
# features
# ----------------------------------------------------------------------

def test_features_are_causal(config, bars):
    """Truncating the tape must not change features already computed.

    A single lookahead feature produces excellent validation scores and no
    live edge, and is nearly impossible to spot afterwards.
    """
    mes = config.instruments["MES"]
    params = config.strategies["momentum_breakout"]
    tf_bars = resample(bars, params.timeframe_minutes)
    strat = make_strategy("momentum_breakout", params, mes)

    full = strat.build_cache(tf_bars)
    cut = len(tf_bars) // 2
    partial = strat.build_cache(tf_bars[:cut])

    compared = 0
    for i in range(cut):
        sig = strat.on_bar(full, i)
        if sig is None:
            continue
        a = extract(full, i, sig, mes)
        b = extract(partial, i, sig, mes)
        np.testing.assert_allclose(a.values, b.values, rtol=1e-9, atol=1e-9)
        compared += 1
    assert compared > 0, "no signals to compare — the test would be vacuous"


def test_features_are_finite_and_named(config, bars):
    mes = config.instruments["MES"]
    params = config.strategies["momentum_breakout"]
    tf_bars = resample(bars, params.timeframe_minutes)
    strat = make_strategy("momentum_breakout", params, mes)
    cache = strat.build_cache(tf_bars)
    for i in range(len(tf_bars)):
        sig = strat.on_bar(cache, i)
        if sig is None:
            continue
        row = extract(cache, i, sig, mes)
        assert len(row.values) == len(FEATURE_NAMES)
        assert np.isfinite(row.values).all(), f"non-finite feature at bar {i}"


def test_direction_features_are_oriented_to_the_trade(config):
    """A long and a short in mirror-image conditions must produce the same
    feature values, so one model can serve both sides."""
    mes = config.instruments["MES"]
    params = config.strategies["momentum_breakout"]
    strat = make_strategy("momentum_breakout", params, mes)

    ts = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    up = [Bar(ts + timedelta(minutes=5 * i), 100 + i, 101 + i, 99 + i, 100.5 + i, 1000, "MES")
          for i in range(120)]
    down = [Bar(b.ts, 200 - (b.open - 100), 200 - (b.low - 100),
                200 - (b.high - 100), 200 - (b.close - 100), b.volume, "MES") for b in up]

    cache_up, cache_down = strat.build_cache(up), strat.build_cache(down)
    i = 110
    long_sig = Signal(up[i].ts, "MES", Side.LONG, "m", up[i].close,
                      up[i].close - 3, up[i].close + 6, 2.0)
    short_sig = Signal(down[i].ts, "MES", Side.SHORT, "m", down[i].close,
                       down[i].close + 3, down[i].close - 6, 2.0)

    a = extract(cache_up, i, long_sig, mes).as_dict()
    b = extract(cache_down, i, short_sig, mes).as_dict()
    for key in ("rsi_directional", "ret_5_atr", "ema_fast_slow_atr", "donchian_position"):
        assert a[key] == pytest.approx(b[key], abs=1e-6), f"{key} is not direction-symmetric"


# ----------------------------------------------------------------------
# labelling
# ----------------------------------------------------------------------

def _bars_from_closes(closes, symbol="MES"):
    base = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    return [Bar(base + timedelta(minutes=i), c, c + 0.25, c - 0.25, c, 1000, symbol)
            for i, c in enumerate(closes)]


def test_label_target_hit():
    b = _bars_from_closes([100, 101, 103, 106])
    sig = Signal(b[0].ts, "MES", Side.LONG, "m", 100.0, 98.0, 104.0, 2.0)
    label = label_signal(sig, np.array([x.high for x in b]), np.array([x.low for x in b]),
                         np.array([x.close for x in b]), 0, time_stop_bars=10)
    assert label is not None and label.is_win and label.exit_kind == "target"


def test_label_stop_hit():
    b = _bars_from_closes([100, 99, 97, 95])
    sig = Signal(b[0].ts, "MES", Side.LONG, "m", 100.0, 98.0, 104.0, 2.0)
    label = label_signal(sig, np.array([x.high for x in b]), np.array([x.low for x in b]),
                         np.array([x.close for x in b]), 0, time_stop_bars=10)
    assert label is not None and not label.is_win and label.exit_kind == "stop"


def test_label_stop_wins_ties_matching_execution():
    """Labelling must use the same pessimism as the fill engine. If labels were
    optimistic while execution is pessimistic, the model would be trained to
    prefer exactly the trades that lose in reality."""
    base = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    b = [Bar(base, 100, 100, 100, 100, 1000, "MES"),
         Bar(base + timedelta(minutes=1), 100, 105, 97, 100, 1000, "MES")]
    sig = Signal(base, "MES", Side.LONG, "m", 100.0, 98.0, 104.0, 2.0)
    label = label_signal(sig, np.array([x.high for x in b]), np.array([x.low for x in b]),
                         np.array([x.close for x in b]), 0, time_stop_bars=10)
    assert label is not None and label.exit_kind == "stop"


def test_unresolved_signal_is_dropped_not_labelled_a_loss():
    """'We don't know' and 'it lost' are different, and conflating them biases
    the model toward pessimism at the end of every training window."""
    b = _bars_from_closes([100, 100.1, 100.2])
    sig = Signal(b[0].ts, "MES", Side.LONG, "m", 100.0, 98.0, 104.0, 2.0)
    label = label_signal(sig, np.array([x.high for x in b]), np.array([x.low for x in b]),
                         np.array([x.close for x in b]), 0, time_stop_bars=50)
    assert label is None


def test_label_records_a_horizon_for_purging():
    b = _bars_from_closes([100, 101, 103, 106])
    sig = Signal(b[0].ts, "MES", Side.LONG, "m", 100.0, 98.0, 104.0, 2.0)
    label = label_signal(sig, np.array([x.high for x in b]), np.array([x.low for x in b]),
                         np.array([x.close for x in b]), 0, time_stop_bars=10)
    assert label.resolve_index > 0 and label.bars_to_resolve > 0


# ----------------------------------------------------------------------
# models
# ----------------------------------------------------------------------

def test_sigmoid_is_numerically_stable():
    z = np.array([-800.0, -1.0, 0.0, 1.0, 800.0])
    p = sigmoid(z)
    assert np.isfinite(p).all()
    assert p[0] == pytest.approx(0.0, abs=1e-12)
    assert p[-1] == pytest.approx(1.0, abs=1e-12)
    assert p[2] == pytest.approx(0.5)


@pytest.mark.parametrize("model_cls", [LogisticRegression, GradientBoostedTrees])
def test_models_learn_a_separable_problem(model_cls):
    rng = np.random.default_rng(5)
    X = rng.normal(size=(800, 5))
    y = (X[:, 0] + 0.5 * X[:, 1] > 0).astype(float)
    model = model_cls(feature_names=[f"f{i}" for i in range(5)])
    model.fit(X[:600], y[:600])
    acc = ((model.predict_proba(X[600:]) > 0.5) == y[600:]).mean()
    assert acc > 0.85, f"{model_cls.__name__} failed a separable problem: {acc:.3f}"


@pytest.mark.parametrize("model_cls", [LogisticRegression, GradientBoostedTrees])
def test_model_round_trips_through_disk(tmp_path, model_cls):
    rng = np.random.default_rng(6)
    X = rng.normal(size=(300, 5))
    y = (X[:, 0] > 0).astype(float)
    model = model_cls(feature_names=[f"f{i}" for i in range(5)])
    model.fit(X, y)
    before = model.predict_proba(X[:20])

    path = tmp_path / "m.json"
    save_model(model, str(path), {"note": "test"})
    restored, meta = load_model(str(path))
    np.testing.assert_allclose(before, restored.predict_proba(X[:20]), rtol=1e-9)
    assert meta["note"] == "test"


def test_standardizer_survives_a_constant_feature():
    """A constant column has zero variance; dividing by it yields NaN and
    poisons every weight in the model."""
    X = np.column_stack([np.random.default_rng(1).normal(size=200), np.ones(200)])
    y = (X[:, 0] > 0).astype(float)
    model = LogisticRegression().fit(X, y)
    assert np.isfinite(model.predict_proba(X)).all()


# ----------------------------------------------------------------------
# validation — the leakage tests
# ----------------------------------------------------------------------

def _synthetic_dataset(n=600, signal_strength=0.0, seed=4, horizon_minutes=30):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, len(FEATURE_NAMES)))
    logits = signal_strength * X[:, 1]
    y = (rng.random(n) < sigmoid(logits)).astype(float)
    base = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    ts = [base + timedelta(minutes=10 * i) for i in range(n)]
    resolve = [t + timedelta(minutes=horizon_minutes) for t in ts]
    return Dataset(
        X=X, y=y, r=np.where(y > 0, 2.0, -1.0),
        timestamps=ts, resolve_ts=resolve,
        symbols=["MES"] * n, strategies=["momentum_breakout"] * n,
    )


def test_walk_forward_never_trains_on_the_future():
    ds = _synthetic_dataset()
    result = walk_forward(ds, lambda: LogisticRegression(), n_folds=4, embargo_minutes=0)
    assert result.folds
    for fold in result.folds:
        # Train sets are built only from indices before the test block, so a
        # growing train size across folds is the signature of walk-forward.
        assert fold.n_train > 0
    sizes = [f.n_train for f in result.folds]
    assert sizes == sorted(sizes), f"training window must grow forward: {sizes}"


def test_purging_removes_overlapping_labels():
    """A label that resolved after the test fold began was partly determined by
    test-fold bars. With a long horizon, many more samples must be purged."""
    short = walk_forward(_synthetic_dataset(horizon_minutes=5),
                         lambda: LogisticRegression(), n_folds=4, embargo_minutes=0)
    long = walk_forward(_synthetic_dataset(horizon_minutes=600),
                        lambda: LogisticRegression(), n_folds=4, embargo_minutes=0)
    assert long.total_purged > short.total_purged, (
        f"longer label horizon must purge more: {long.total_purged} vs {short.total_purged}"
    )


def test_embargo_purges_additional_samples():
    ds = _synthetic_dataset(horizon_minutes=5)
    none = walk_forward(ds, lambda: LogisticRegression(), n_folds=4, embargo_minutes=0)
    heavy = walk_forward(ds, lambda: LogisticRegression(), n_folds=4, embargo_minutes=600)
    assert heavy.total_purged > none.total_purged


def test_pipeline_reports_noise_as_noise():
    """The control that matters most: with no relationship in the data, the
    permutation test must NOT declare significance."""
    ds = _synthetic_dataset(signal_strength=0.0, seed=9)
    real, shuffled, p = permutation_test(
        ds, lambda: LogisticRegression(), n_folds=4, n_permutations=15, embargo_minutes=0
    )
    assert p > 0.05, f"pipeline found significance in pure noise (AUC {real:.3f}, p={p:.3f})"


def test_pipeline_detects_an_injected_edge():
    """The other half: when a real relationship exists, the pipeline must find
    it. A validator that only ever reports 'no edge' is not a validator.
    """
    ds = _synthetic_dataset(signal_strength=2.0, seed=9, n=800)
    result = walk_forward(ds, lambda: LogisticRegression(), n_folds=4, embargo_minutes=0)
    auc = result.pooled_auc()
    assert auc > 0.65, f"failed to detect a strong injected edge (AUC {auc:.3f})"

    _, _, p = permutation_test(
        ds, lambda: LogisticRegression(), n_folds=4, n_permutations=15, embargo_minutes=0
    )
    assert p < 0.10, f"real edge not distinguished from shuffled labels (p={p:.3f})"


def test_threshold_sweep_reports_the_economics():
    p = np.array([0.1, 0.3, 0.6, 0.8, 0.9])
    y = np.array([0.0, 0.0, 1.0, 1.0, 1.0])
    r = np.array([-1.0, -1.0, 2.0, 2.0, 2.0])
    rows = threshold_sweep(p, y, r, thresholds=[0.0, 0.5])
    assert rows[0].trades_kept == 5 and rows[0].mean_r == pytest.approx(0.8)
    assert rows[1].trades_kept == 3 and rows[1].mean_r == pytest.approx(2.0)
    assert rows[1].lift_vs_baseline > 0


# ----------------------------------------------------------------------
# regimes
# ----------------------------------------------------------------------

def test_regime_model_separates_trend_from_range():
    base = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    rng = np.random.default_rng(2)
    closes = []
    closes += list(100 + np.cumsum(rng.normal(0.25, 0.15, 400)))   # trend
    closes += list(closes[-1] + rng.normal(0, 0.3, 400))           # range
    bars = [Bar(base + timedelta(minutes=5 * i), c, c + 0.3, c - 0.3, c, 1000, "MES")
            for i, c in enumerate(closes)]

    feats = regime_features(bars)
    model = RegimeModel(k=2, min_dwell_bars=5).fit(feats)
    labels = model.predict(feats)

    trend_labels = labels[100:380]
    range_labels = labels[450:780]
    trend_mode = np.bincount(trend_labels[trend_labels >= 0]).argmax()
    range_mode = np.bincount(range_labels[range_labels >= 0]).argmax()
    assert trend_mode != range_mode, "regime model failed to separate trend from range"


def test_regime_dwell_filter_suppresses_flicker():
    model = RegimeModel(k=2, min_dwell_bars=5)
    noisy = np.array([0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0])
    smoothed = model._apply_dwell(noisy)
    assert (smoothed == 0).all(), f"one-bar flip should be suppressed: {smoothed}"


def test_regime_model_round_trips():
    base = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    rng = np.random.default_rng(8)
    closes = 100 + np.cumsum(rng.normal(0, 0.4, 600))
    bars = [Bar(base + timedelta(minutes=5 * i), c, c + 0.3, c - 0.3, c, 1000, "MES")
            for i, c in enumerate(closes)]
    feats = regime_features(bars)
    model = RegimeModel(k=3).fit(feats)
    restored = RegimeModel.from_dict(model.to_dict())
    np.testing.assert_array_equal(model.predict(feats), restored.predict(feats))


def test_regime_fit_refuses_too_little_data():
    with pytest.raises(ValueError, match="at least"):
        RegimeModel(k=4).fit(np.full((10, 6), 1.0))


# ----------------------------------------------------------------------
# filter
# ----------------------------------------------------------------------

def test_filter_without_a_model_passes_everything(config, bars):
    """Degrade to the rule-based strategy rather than refusing to trade."""
    f = MLSignalFilter(model=None)
    assert not f.active


def test_filter_rejects_when_the_feature_set_mismatches():
    """A model trained on a different feature order would score confident
    nonsense, so construction must fail loudly."""
    model = LogisticRegression(feature_names=["a", "b"])
    with pytest.raises(ValueError, match="different feature set"):
        MLSignalFilter(model=model, threshold=0.5)


def test_filter_blocks_low_scores_and_passes_high_ones(config, bars):
    mes = config.instruments["MES"]
    params = config.strategies["momentum_breakout"]
    tf_bars = resample(bars, params.timeframe_minutes)
    strat = make_strategy("momentum_breakout", params, mes)
    cache = strat.build_cache(tf_bars)

    found = [(i, s) for i in range(len(tf_bars)) if (s := strat.on_bar(cache, i))]
    assert found, "need signals for this test to mean anything"

    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, len(FEATURE_NAMES)))
    model = LogisticRegression(feature_names=FEATURE_NAMES).fit(X, (X[:, 0] > 0).astype(float))

    permissive = MLSignalFilter(model=model, threshold=0.0)
    strict = MLSignalFilter(model=model, threshold=1.01)
    i, sig = found[0]
    assert permissive.allows(cache, i, sig, mes)[0]
    assert not strict.allows(cache, i, sig, mes)[0]


def test_filter_fails_closed_on_a_scoring_error(config, bars):
    """An unscored signal is unknown, and unknown must not count as good."""
    class Broken(LogisticRegression):
        def predict_proba(self, X):
            raise RuntimeError("boom")

    mes = config.instruments["MES"]
    params = config.strategies["momentum_breakout"]
    tf_bars = resample(bars, params.timeframe_minutes)
    strat = make_strategy("momentum_breakout", params, mes)
    cache = strat.build_cache(tf_bars)
    found = [(i, s) for i in range(len(tf_bars)) if (s := strat.on_bar(cache, i))]

    model = Broken(feature_names=FEATURE_NAMES)
    model.weights = np.zeros(len(FEATURE_NAMES))
    f = MLSignalFilter(model=model, threshold=0.5)
    allowed, reason = f.allows(cache, found[0][0], found[0][1], mes)
    assert not allowed and "failed" in reason
    assert f.stats.errors == 1


# ----------------------------------------------------------------------
# dataset
# ----------------------------------------------------------------------

def test_build_dataset_is_time_ordered_and_labelled(config, bars):
    ds = build_dataset(config, {"MES": bars})
    assert len(ds) > 20
    assert ds.timestamps == sorted(ds.timestamps), "samples must be chronological"
    assert set(np.unique(ds.y)) <= {0.0, 1.0}
    assert all(rt >= t for rt, t in zip(ds.resolve_ts, ds.timestamps)), (
        "a label cannot resolve before its signal"
    )
    assert ds.X.shape == (len(ds), len(FEATURE_NAMES))


def test_dataset_subset_keeps_everything_aligned(config, bars):
    ds = build_dataset(config, {"MES": bars})
    sub = ds.subset([0, 2, 4])
    assert len(sub) == 3
    np.testing.assert_allclose(sub.X[1], ds.X[2])
    assert sub.timestamps[1] == ds.timestamps[2]
