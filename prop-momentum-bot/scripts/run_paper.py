#!/usr/bin/env python3
"""Paper-trade the bot against simulated live bars.

Feeds bars into `LiveTrader` one at a time through `PaperBroker`, exactly as a
real feed would. Use this to watch the decision sequence, the risk guard's
vetoes, and the reconciliation loop behave before pointing anything at a broker.

    python scripts/run_paper.py --firm futures_50k_trailing --symbols MES --days 10 --verbose
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import BotConfig
from bot.brokers import PaperBroker
from bot.data.feed import CsvFeed, align_feeds
from bot.data.synthetic import generate_bars
from bot.engine.live import LiveTrader
from bot.reporting import compute, trade_log


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--firm", default="futures_50k_trailing")
    p.add_argument("--symbols", nargs="+", default=["MES"])
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--seed", type=int, default=21)
    p.add_argument("--csv", nargs="*", default=[], metavar="SYM=PATH")
    p.add_argument("--verbose", action="store_true", help="log every open/close")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    config = BotConfig.load(args.firm)
    csv_map = dict(spec.split("=", 1) for spec in args.csv if "=" in spec)
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)

    data = {}
    for symbol in args.symbols:
        if symbol in csv_map:
            data[symbol] = CsvFeed(symbol, 1, csv_map[symbol]).to_list()
        else:
            data[symbol] = generate_bars(config.instruments[symbol], start, args.days, 1, seed=args.seed)

    broker = PaperBroker(config.instruments, config.firm.starting_balance)
    broker.connect()
    trader = LiveTrader(config, broker, args.symbols)

    print(f"paper trading {args.symbols} on {config.firm.display_name}")
    print(f"{sum(len(v) for v in data.values())} bars to replay\n")

    for ts, group in align_feeds(data):
        for symbol, bar in group.items():
            broker.push_bar(bar)
            trader.on_bar(bar)
        if trader.state.halted:
            print(f"\nHALTED: {trader.state.halt_reason}")
            break

    trader.account.finalize()
    print()
    print(trader.status_line())
    print()
    trades = trader.portfolio.closed
    if trades:
        m = compute(trades)
        print(f"{m.trades} trades | win rate {m.win_rate:.1f}% | "
              f"expectancy {m.expectancy_r:+.3f}R | net ${m.net_pnl:,.2f}")
        print()
        print(trade_log(trades, limit=20))
    else:
        print("no trades taken")
    if trader.state.rejections:
        print()
        print("risk guard vetoes:")
        for reason, count in sorted(trader.state.rejections.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  {count:>5}  {reason}")
    if trader.state.reconcile_errors:
        print(f"\nreconciliation errors: {trader.state.reconcile_errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
