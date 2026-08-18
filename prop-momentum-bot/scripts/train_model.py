#!/usr/bin/env python3
"""Train and honestly evaluate the ML trade filter and regime detector.

The output is designed to talk you OUT of using a model that does not work.
Three gates have to pass before a model is worth trading:

    1. Out-of-sample AUC meaningfully above 0.5 on purged walk-forward folds
    2. A permutation test showing that AUC is not reproducible from shuffled
       labels (p < 0.10)
    3. A threshold that improves expectancy while still keeping enough trades
       to matter

Failing any of them, the script says so and refuses to recommend the model.

Examples
--------
    python scripts/train_model.py --firm futures_50k_trailing --symbols MES MNQ --days 250
    python scripts/train_model.py --firm ftmo_100k --symbols EURUSD \
        --csv EURUSD=data/eurusd_1m.csv --model gbt --out models/eurusd.json
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import BotConfig
from bot.data.feed import CsvFeed, resample
from bot.data.synthetic import generate_bars
from bot.ml import (
    GradientBoostedTrees, LogisticRegression, RegimeModel, build_dataset,
    permutation_test, regime_features, save_model, threshold_sweep, walk_forward,
)

MIN_SAMPLES = 200
GOOD_AUC = 0.55
MAX_P_VALUE = 0.10


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--firm", default="futures_50k_trailing")
    p.add_argument("--symbols", nargs="+", default=["MES"])
    p.add_argument("--strategies", nargs="*", default=None)
    p.add_argument("--days", type=int, default=250, help="sessions of synthetic data")
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--csv", nargs="*", default=[], metavar="SYM=PATH")
    p.add_argument("--model", choices=["logistic", "gbt"], default="logistic")
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--embargo-minutes", type=int, default=60)
    p.add_argument("--permutations", type=int, default=20)
    p.add_argument("--regimes", type=int, default=4)
    p.add_argument("--out", default=None, help="path to save the trained model JSON")
    p.add_argument("--per-strategy", action="store_true",
                   help="train a separate model per strategy instead of one pooled model")
    return p.parse_args()


def make_model_factory(kind: str, feature_names):
    if kind == "gbt":
        return lambda: GradientBoostedTrees(feature_names=feature_names)
    return lambda: LogisticRegression(feature_names=feature_names)


def evaluate(dataset, kind: str, args, label: str) -> dict:
    """Run the full honesty pipeline on one dataset and print the verdict."""
    print()
    print("=" * 78)
    print(f"  {label}")
    print("=" * 78)
    print(f"  {dataset.describe()}")

    if len(dataset) < MIN_SAMPLES:
        print()
        print(f"  NOT ENOUGH DATA — {len(dataset)} samples, want at least {MIN_SAMPLES}.")
        print("  A model fitted here would memorise noise. Collect more history.")
        return {"usable": False, "reason": "insufficient samples"}

    factory = make_model_factory(kind, dataset.feature_names)
    wf = walk_forward(dataset, factory, n_folds=args.folds, embargo_minutes=args.embargo_minutes)

    if not wf.folds:
        print("\n  No usable folds — too few samples per fold after purging.")
        return {"usable": False, "reason": "no usable folds"}

    print()
    print("  PURGED WALK-FORWARD (train on past, test on future)")
    print(f"    {'fold':<6}{'train':>8}{'purged':>8}{'test':>7}{'AUC':>8}{'acc':>8}{'base':>8}")
    for f in wf.folds:
        print(f"    {f.fold:<6}{f.n_train:>8}{f.n_purged:>8}{f.n_test:>7}"
              f"{f.auc:>8.3f}{f.accuracy:>8.3f}{f.base_rate:>8.3f}")
    pooled_auc = wf.pooled_auc()
    print(f"    {'pooled':<6}{'':>8}{wf.total_purged:>8}{wf.n_samples:>7}{pooled_auc:>8.3f}")
    print(f"    ({wf.total_purged} training samples purged for overlapping the test folds)")

    # --- permutation test ---
    print()
    print("  PERMUTATION TEST (same pipeline, shuffled labels)")
    real, shuffled_mean, p_value = permutation_test(
        dataset, factory, n_folds=args.folds,
        n_permutations=args.permutations, embargo_minutes=args.embargo_minutes,
    )
    print(f"    real AUC {real:.3f} | shuffled mean AUC {shuffled_mean:.3f} | p = {p_value:.3f}")
    if p_value > MAX_P_VALUE:
        print(f"    -> shuffled labels reproduce this AUC {p_value:.0%} of the time. NOISE.")
    else:
        print(f"    -> unlikely under shuffled labels (p < {MAX_P_VALUE})")

    # --- economics ---
    probs, y, r = wf.pooled()
    print()
    print("  WHAT FILTERING WOULD DO (out-of-sample, costs excluded)")
    print(f"    {'thresh':>8}{'kept':>8}{'%kept':>8}{'win%':>8}{'mean R':>9}{'lift':>9}")
    rows = threshold_sweep(probs, y, r)
    best = None
    for row in rows:
        marker = ""
        if row.kept_fraction >= 0.25 and row.lift_vs_baseline > 0:
            if best is None or row.lift_vs_baseline > best.lift_vs_baseline:
                best = row
                marker = ""
        print(f"    {row.threshold:>8.2f}{row.trades_kept:>8}{100 * row.kept_fraction:>7.0f}%"
              f"{100 * row.win_rate:>7.0f}%{row.mean_r:>9.3f}{row.lift_vs_baseline:>+9.3f}{marker}")
    if best:
        print(f"    best usable threshold: {best.threshold:.2f} "
              f"(keeps {100 * best.kept_fraction:.0f}% of trades, "
              f"expectancy {best.mean_r:+.3f}R vs {rows[0].mean_r:+.3f}R unfiltered)")

    # --- what the model learned ---
    model = factory()
    model.fit(dataset.X, dataset.y)
    imps = model.importances()
    if imps:
        print()
        print("  TOP FEATURES (fitted on all data — for interpretation only)")
        for name, w in sorted(imps.items(), key=lambda kv: -kv[1])[:8]:
            bar = "█" * max(1, int(w * 60))
            print(f"    {name:<24}{w:>6.3f}  {bar}")
    if isinstance(model, LogisticRegression):
        coefs = model.signed_coefficients()
        strongest = sorted(coefs.items(), key=lambda kv: -abs(kv[1]))[:5]
        print("    direction: " + ", ".join(
            f"{n} {'+' if c > 0 else '-'}{abs(c):.2f}" for n, c in strongest
        ))

    # --- verdict ---
    usable = pooled_auc >= GOOD_AUC and p_value <= MAX_P_VALUE and best is not None
    print()
    if usable:
        print(f"  VERDICT: USABLE — AUC {pooled_auc:.3f}, p={p_value:.3f}, "
              f"threshold {best.threshold:.2f} lifts expectancy {best.lift_vs_baseline:+.3f}R.")
        print("           Forward-test on paper before trusting it with an evaluation.")
    else:
        reasons = []
        if pooled_auc < GOOD_AUC:
            reasons.append(f"AUC {pooled_auc:.3f} < {GOOD_AUC}")
        if p_value > MAX_P_VALUE:
            reasons.append(f"p = {p_value:.3f} > {MAX_P_VALUE} (indistinguishable from noise)")
        if best is None:
            reasons.append("no threshold both improves expectancy and keeps >=25% of trades")
        print(f"  VERDICT: DO NOT TRADE THIS MODEL — {'; '.join(reasons)}.")
        print("           This is the expected result on data without a real edge.")

    return {
        "usable": usable, "auc": pooled_auc, "p_value": p_value,
        "threshold": best.threshold if best else None,
        "model": model, "n_samples": len(dataset),
    }


def analyse_regimes(config, data, args) -> None:
    """Fit the regime detector and report where each strategy actually works."""
    print()
    print("=" * 78)
    print("  REGIME DETECTION")
    print("=" * 78)

    symbol = args.symbols[0]
    bars = resample(data[symbol], 5)
    feats = regime_features(bars)
    split = int(len(bars) * 0.6)
    try:
        model = RegimeModel(k=args.regimes).fit(feats[:split])
    except ValueError as exc:
        print(f"  could not fit regimes: {exc}")
        return

    labels = model.predict(feats)
    print(f"  fitted on the first 60% of {symbol} 5m bars, applied forward")
    print()
    print("  " + model.describe_regimes(feats, labels).replace("\n", "\n  "))

    changes = int(np.sum(np.diff(labels[labels >= 0]) != 0))
    valid = int((labels >= 0).sum())
    print()
    print(f"  {changes} regime changes over {valid} bars "
          f"(average dwell {valid / max(changes, 1):.0f} bars)")

    # Per-regime trade performance: the actionable part.
    ds = build_dataset(config, {symbol: data[symbol]}, ["momentum_breakout"])
    if len(ds) < 30:
        return
    ts_to_regime = {b.ts: labels[i] for i, b in enumerate(bars) if labels[i] >= 0}
    buckets: dict[int, list[float]] = {}
    for t, r_val in zip(ds.timestamps, ds.r):
        reg = ts_to_regime.get(t)
        if reg is not None:
            buckets.setdefault(int(reg), []).append(float(r_val))

    if buckets:
        print()
        print("  MOMENTUM BREAKOUT PERFORMANCE BY REGIME (R per trade, costs excluded)")
        print(f"    {'regime':<26}{'trades':>8}{'mean R':>10}{'win%':>8}")
        for reg, rs in sorted(buckets.items(), key=lambda kv: -np.mean(kv[1])):
            arr = np.array(rs)
            print(f"    {model.names.get(reg, str(reg)):<26}{len(arr):>8}"
                  f"{arr.mean():>10.3f}{100 * (arr > 0).mean():>7.0f}%")
        print()
        print("  Regimes with negative mean R are where this strategy should stand down.")
        print("  Confirm the ranking holds out of sample before acting on it.")


def main() -> int:
    args = parse_args()
    config = BotConfig.load(args.firm)
    csv_map = dict(spec.split("=", 1) for spec in args.csv if "=" in spec)
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)

    data = {}
    for i, symbol in enumerate(args.symbols):
        if symbol in csv_map:
            data[symbol] = CsvFeed(symbol, 1, csv_map[symbol]).to_list()
            print(f"loaded {len(data[symbol])} bars for {symbol}")
        else:
            data[symbol] = generate_bars(
                config.instruments[symbol], start, args.days, 1, seed=args.seed + i * 101
            )

    if not csv_map:
        print()
        print("*" * 78)
        print("SYNTHETIC DATA. Synthetic bars contain no exploitable edge, so the")
        print("correct outcome below is DO NOT TRADE THIS MODEL. Seeing that verdict")
        print("means the validation pipeline is working. Re-run with --csv on real")
        print("data before drawing any conclusion.")
        print("*" * 78)

    dataset = build_dataset(config, data, args.strategies)
    if len(dataset) == 0:
        print("no signals produced — nothing to train on")
        return 1

    results = []
    if args.per_strategy:
        for name in sorted(set(dataset.strategies)):
            sub = dataset.filter_strategy(name)
            results.append((name, evaluate(sub, args.model, args, f"MODEL: {name}")))
    else:
        results.append(("pooled", evaluate(dataset, args.model, args, "MODEL: all strategies pooled")))

    analyse_regimes(config, data, args)

    if args.out:
        usable = [(n, r) for n, r in results if r.get("usable")]
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        name, res = (usable or results)[0]
        save_model(
            res["model"], args.out,
            metadata={
                "strategy": name, "firm": args.firm, "symbols": args.symbols,
                "model_type": args.model, "n_samples": res.get("n_samples", 0),
                "walk_forward_auc": res.get("auc"), "permutation_p_value": res.get("p_value"),
                "recommended_threshold": res.get("threshold"),
                "usable": res.get("usable", False),
                "trained_on": "csv" if csv_map else "synthetic",
            },
        )
        print()
        print(f"saved model to {args.out}"
              + ("" if res.get("usable") else "  (marked usable=false in metadata)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
