#!/usr/bin/env python3
"""Simulate many independent evaluation attempts and report the pass rate.

One backtest tells you what happened on one sequence of bars. That is a sample
of size one, and prop evaluations are decided by variance as much as by edge.
Running N independent attempts and reporting the distribution — with a
confidence interval — is the only honest way to answer "will this pass?"

Examples
--------
    python scripts/monte_carlo.py --firm futures_50k_trailing --symbols MES MNQ --runs 100
    python scripts/monte_carlo.py --firm ftmo_100k --symbols EURUSD GBPUSD --runs 200 --days 60
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import BotConfig
from bot.data.synthetic import generate_bars
from bot.engine.backtest import Backtester
from bot.reporting import aggregate, monte_carlo_report


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--firm", default="futures_50k_trailing")
    p.add_argument("--symbols", nargs="+", default=["MES"])
    p.add_argument("--strategies", nargs="*", default=None)
    p.add_argument("--runs", type=int, default=50, help="independent evaluation attempts")
    p.add_argument("--days", type=int, default=45, help="sessions per attempt")
    p.add_argument("--start-seed", type=int, default=1000)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    config = BotConfig.load(args.firm)
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    results = []

    for run in range(args.runs):
        seed = args.start_seed + run * 17
        data = {
            s: generate_bars(config.instruments[s], start, args.days, 1, seed=seed + i * 101)
            for i, s in enumerate(args.symbols)
        }
        # A fresh config per run keeps account state from leaking between attempts.
        result = Backtester(BotConfig.load(args.firm), strategies=args.strategies).run(data)
        results.append(result)
        if not args.quiet:
            marker = {"PASSED": "P", "FAILED": "X", "INCOMPLETE": "."}[result.outcome]
            print(marker, end="", flush=True)
            if (run + 1) % 50 == 0:
                print(f"  {run + 1}/{args.runs}")
    if not args.quiet:
        print()
        print()

    print(monte_carlo_report(aggregate(results), config, args.runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
