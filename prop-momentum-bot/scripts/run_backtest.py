#!/usr/bin/env python3
"""Run one evaluation attempt and print a report.

Examples
--------
    # synthetic data, futures firm, both strategies
    python scripts/run_backtest.py --firm futures_50k_trailing --symbols MES MNQ --days 40

    # your own historical bars (1-minute CSVs)
    python scripts/run_backtest.py --firm ftmo_100k --symbols EURUSD \
        --csv EURUSD=data/eurusd_1m.csv

    # one strategy at a time, to see which one carries the result
    python scripts/run_backtest.py --firm futures_50k_trailing --symbols MES \
        --strategies momentum_breakout --days 60
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import BotConfig, ConfigError, load_firms
from bot.data.feed import CsvFeed
from bot.data.synthetic import generate_bars
from bot.engine.backtest import Backtester
from bot.engine.broker_sim import FillModel
from bot.reporting import backtest_report, trade_log


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--firm", default="futures_50k_trailing", help="firm profile key from config/firms.yaml")
    p.add_argument("--symbols", nargs="+", default=["MES"], help="instruments to trade")
    p.add_argument("--strategies", nargs="*", default=None, help="strategies to run (default: all enabled)")
    p.add_argument("--days", type=int, default=40, help="sessions of synthetic data to generate")
    p.add_argument("--seed", type=int, default=7, help="synthetic data seed")
    p.add_argument("--start", default="2026-01-05", help="synthetic data start date (YYYY-MM-DD)")
    p.add_argument("--csv", nargs="*", default=[], metavar="SYM=PATH",
                   help="use real 1-minute CSV data for a symbol instead of synthetic")
    p.add_argument("--stop-slippage-atr", type=float, default=0.10,
                   help="slippage on stop exits, in ATR units")
    p.add_argument("--no-costs", action="store_true",
                   help="disable spread and slippage (shows how much of the result is cost)")
    p.add_argument("--trades", action="store_true", help="print the trade log")
    p.add_argument("--model", default=None, metavar="PATH",
                   help="trained model JSON from train_model.py; filters signals below --threshold")
    p.add_argument("--threshold", type=float, default=None,
                   help="minimum model score to take a trade (default: the model's recommended threshold)")
    p.add_argument("--list-firms", action="store_true", help="list available firm profiles and exit")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_firms:
        for key, firm in load_firms().items():
            print(f"{key:32s} {firm.display_name}")
            print(f"{'':32s}   balance ${firm.starting_balance:,.0f}  "
                  f"target ${firm.profit_target:,.0f}  "
                  f"max DD ${firm.max_total_drawdown:,.0f} ({firm.drawdown_model})")
        return 0

    try:
        config = BotConfig.load(args.firm)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    permitted = config.instruments_for_firm()
    unknown = [s for s in args.symbols if s not in permitted]
    if unknown:
        print(f"error: {unknown} not permitted by {args.firm} "
              f"(allows {config.firm.asset_classes}); available: {sorted(permitted)}",
              file=sys.stderr)
        return 2

    csv_map = {}
    for spec in args.csv:
        if "=" not in spec:
            print(f"error: --csv expects SYMBOL=PATH, got {spec!r}", file=sys.stderr)
            return 2
        sym, path = spec.split("=", 1)
        csv_map[sym] = path

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    data = {}
    for symbol in args.symbols:
        instrument = config.instruments[symbol]
        if symbol in csv_map:
            data[symbol] = CsvFeed(symbol, 1, csv_map[symbol]).to_list()
            print(f"loaded {len(data[symbol])} bars for {symbol} from {csv_map[symbol]}")
        else:
            data[symbol] = generate_bars(instrument, start, args.days, 1, seed=args.seed + hash(symbol) % 1000)

    signal_filter = None
    if args.model:
        from bot.ml import MLSignalFilter, load_model

        model, meta = load_model(args.model)
        threshold = args.threshold
        if threshold is None:
            threshold = meta.get("recommended_threshold") or 0.5
        if not meta.get("usable", False):
            print(f"WARNING: {args.model} is marked usable=false in its metadata "
                  f"(walk-forward AUC {meta.get('walk_forward_auc')}, "
                  f"permutation p={meta.get('permutation_p_value')}).")
            print("         Filtering with it is not expected to help. Continuing anyway.")
        if meta.get("trained_on") == "synthetic":
            print("WARNING: this model was trained on synthetic data and cannot "
                  "generalise to real markets.")
        signal_filter = MLSignalFilter(
            model=model, threshold=threshold, window=config.risk.trading_window
        )
        print(f"ML filter active: {os.path.basename(args.model)} at threshold {threshold:.2f}")

    fills = FillModel(
        stop_slippage_atr=0.0 if args.no_costs else args.stop_slippage_atr,
        entry_slippage_atr=0.0 if args.no_costs else 0.03,
        use_spread=not args.no_costs,
    )
    result = Backtester(config, fills, args.strategies, signal_filter).run(data)

    title = "BACKTEST (SYNTHETIC DATA)" if not csv_map else "BACKTEST"
    print(backtest_report(result, config, title))
    if args.trades and result.trades:
        print()
        print(trade_log(result.trades))
    if not csv_map:
        print()
        print("NOTE: this ran on synthetic bars. It proves the engine works end to end;")
        print("      it says nothing about whether the strategy has an edge in real")
        print("      markets. Re-run with --csv on your own data before drawing any")
        print("      conclusion about profitability.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
