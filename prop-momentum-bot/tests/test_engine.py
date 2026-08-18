"""Engine tests: fills, exit precedence, cost accounting, and lookahead.

The most important test in this file is `test_engine_detects_a_real_edge`. A
backtester that only ever loses money is indistinguishable from one with a sign
error, so the engine has to be shown finding a profit that genuinely exists
before its losses on other data mean anything.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from bot.config import BotConfig
from bot.data.feed import ListFeed, resample
from bot.data.synthetic import generate_bars
from bot.data.types import Bar, ExitReason, Position, Side, Signal
from bot.engine.backtest import Backtester
from bot.engine.broker_sim import FillModel
from bot.engine.portfolio import Portfolio
from bot.reporting import compute

UTC = timezone.utc


@pytest.fixture
def config():
    return BotConfig.load("futures_50k_trailing")


def bar(ts, o, h, l, c, v=1000.0, symbol="MES"):
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v, symbol=symbol)


# ----------------------------------------------------------------------
# fills
# ----------------------------------------------------------------------

def test_entry_fill_is_never_better_than_the_signal_price(config):
    fills = FillModel()
    mes = config.instruments["MES"]
    long_fill = fills.entry_fill(mes, Side.LONG, 5000.0, 25.0)
    short_fill = fills.entry_fill(mes, Side.SHORT, 5000.0, 25.0)
    assert long_fill > 5000.0, "buying must pay up"
    assert short_fill < 5000.0, "selling must give up the spread"


def test_stop_exit_fills_worse_than_the_stop(config):
    fills = FillModel()
    mes = config.instruments["MES"]
    b = bar(datetime(2026, 3, 3, 15, 0, tzinfo=UTC), 5000, 5001, 4970, 4975)
    fill = fills.exit_fill(mes, Side.LONG, 4975.0, b, 25.0, ExitReason.STOP)
    assert fill < 4975.0, "a stop is a market order in motion; it slips"


def test_a_gap_through_the_stop_fills_at_the_open(config):
    """The difference that makes overnight risk real."""
    fills = FillModel()
    mes = config.instruments["MES"]
    gapped = bar(datetime(2026, 3, 3, 15, 0, tzinfo=UTC), 4900, 4910, 4890, 4905)
    fill = fills.exit_fill(mes, Side.LONG, 4975.0, gapped, 25.0, ExitReason.STOP)
    assert fill == pytest.approx(4900.0), "you get the open, not your stop price"


def test_target_fills_at_its_price_with_no_positive_slippage(config):
    fills = FillModel()
    mes = config.instruments["MES"]
    b = bar(datetime(2026, 3, 3, 15, 0, tzinfo=UTC), 5000, 5060, 4995, 5055)
    fill = fills.exit_fill(mes, Side.LONG, 5050.0, b, 25.0, ExitReason.TARGET)
    assert fill == pytest.approx(5050.0), "a resting limit never fills better than its price"


# ----------------------------------------------------------------------
# exits
# ----------------------------------------------------------------------

def make_position(config, side=Side.LONG, entry=5000.0, stop=4975.0, target=5050.0):
    return Position(
        symbol="MES", side=side, size=1, entry_price=entry,
        entry_ts=datetime(2026, 3, 3, 15, 0, tzinfo=UTC),
        stop_price=stop, target_price=target, strategy="momentum_breakout",
        atr_at_entry=25.0, initial_risk_per_unit=abs(entry - stop),
    )


def test_stop_wins_when_a_bar_covers_both_stop_and_target(config):
    """Without tick data, assuming the target filled first is how backtests
    manufacture fictional win rates. Pessimism is the only honest default."""
    pf = Portfolio(config)
    pos = make_position(config)
    params = config.strategies["momentum_breakout"]
    engulfing = bar(datetime(2026, 3, 3, 15, 5, tzinfo=UTC), 5000, 5060, 4970, 5010)
    assert pf.check_exit(pos, engulfing, params) is ExitReason.STOP


def test_time_stop_closes_a_dead_trade(config):
    pf = Portfolio(config)
    pos = make_position(config)
    params = config.strategies["momentum_breakout"]
    pos.bars_held = params.geti("time_stop_bars") + 1
    quiet = bar(datetime(2026, 3, 3, 15, 5, tzinfo=UTC), 5000, 5005, 4995, 5000)
    assert pf.check_exit(pos, quiet, params) is ExitReason.TIME_STOP


def test_stops_only_ever_move_favourably(config):
    """A stop that can loosen is not a stop."""
    pf = Portfolio(config)
    pos = make_position(config)
    params = config.strategies["momentum_breakout"]
    original = pos.stop_price
    for _ in range(5):
        pf.update_stops(pos, bar(datetime(2026, 3, 3, 15, 5, tzinfo=UTC), 5000, 5002, 4998, 5001), params)
    assert pos.stop_price >= original


def test_breakeven_stop_covers_costs(config):
    """Scratching a trade must actually be flat, not flat-minus-commission."""
    pf = Portfolio(config)
    pos = make_position(config)
    params = config.strategies["momentum_breakout"]
    r_needed = params.getf("breakeven_at_r")
    price = pos.entry_price + r_needed * pos.initial_risk_per_unit
    pf.update_stops(pos, bar(datetime(2026, 3, 3, 15, 5, tzinfo=UTC), price, price, price, price), params)
    assert pos.moved_to_breakeven
    assert pos.stop_price > pos.entry_price, "breakeven must clear the round-turn cost"


def test_costs_are_charged_on_every_round_turn(config):
    pf = Portfolio(config)
    pos = make_position(config)
    b = bar(datetime(2026, 3, 3, 15, 30, tzinfo=UTC), 5050, 5055, 5045, 5050)
    trade = pf.close_position(pos, b, ExitReason.TARGET)
    assert trade.costs > 0
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.costs)


def test_short_pnl_has_the_right_sign(config):
    pf = Portfolio(config)
    pos = make_position(config, side=Side.SHORT, entry=5000.0, stop=5025.0, target=4950.0)
    b = bar(datetime(2026, 3, 3, 15, 30, tzinfo=UTC), 4950, 4955, 4945, 4950)
    trade = pf.close_position(pos, b, ExitReason.TARGET)
    assert trade.gross_pnl > 0, "a short that fell must profit"


# ----------------------------------------------------------------------
# feed
# ----------------------------------------------------------------------

def test_resample_aggregates_ohlcv_correctly():
    base = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    bars = [
        bar(base + timedelta(minutes=i), 100 + i, 105 + i, 95 + i, 101 + i, 10)
        for i in range(5)
    ]
    out = resample(bars, 5)
    assert len(out) == 1
    assert out[0].open == bars[0].open
    assert out[0].close == bars[-1].close
    assert out[0].high == max(b.high for b in bars)
    assert out[0].low == min(b.low for b in bars)
    assert out[0].volume == 50


def test_resample_buckets_align_to_the_clock():
    """Buckets must land on :00, :05, :10 rather than drifting with wherever
    the data happens to start."""
    base = datetime(2026, 3, 3, 15, 3, tzinfo=UTC)
    bars = [bar(base + timedelta(minutes=i), 100, 101, 99, 100) for i in range(10)]
    out = resample(bars, 5)
    assert all(b.ts.minute % 5 == 0 for b in out)


# ----------------------------------------------------------------------
# the whole engine
# ----------------------------------------------------------------------

def test_backtest_never_breaches_the_floor(config):
    """Across many random tapes the account may lose, but it must never end
    below the floor without the engine recording a failure."""
    for seed in range(8):
        cfg = BotConfig.load("futures_50k_trailing")
        data = {"MES": generate_bars(cfg.instruments["MES"],
                                     datetime(2026, 1, 5, tzinfo=UTC), 25, 1, seed=seed)}
        result = Backtester(cfg).run(data)
        if not result.failed:
            assert result.account.equity > result.account.floor, (
                f"seed {seed}: ended below the floor without recording a failure"
            )


def test_backtest_is_deterministic(config):
    data = {"MES": generate_bars(config.instruments["MES"],
                                 datetime(2026, 1, 5, tzinfo=UTC), 20, 1, seed=99)}
    a = Backtester(BotConfig.load("futures_50k_trailing")).run(data)
    b = Backtester(BotConfig.load("futures_50k_trailing")).run(data)
    assert len(a.trades) == len(b.trades)
    assert a.account.balance == pytest.approx(b.account.balance)


def test_every_trade_respects_the_session_rules(config):
    """A futures firm forbids overnight holds; no trade may survive the close."""
    data = {"MES": generate_bars(config.instruments["MES"],
                                 datetime(2026, 1, 5, tzinfo=UTC), 25, 1, seed=4)}
    result = Backtester(config).run(data)
    assert not config.firm.allow_overnight
    for trade in result.trades:
        assert trade.entry_ts.date() == trade.exit_ts.date() or trade.exit_reason in (
            ExitReason.SESSION_CLOSE, ExitReason.RISK_HALT,
        ), f"{trade.symbol} held overnight: {trade.entry_ts} -> {trade.exit_ts}"


def test_no_trade_is_opened_after_the_account_fails(config):
    """A bar that breaches the floor must not also open a position."""
    for seed in range(15):
        cfg = BotConfig.load("futures_50k_trailing")
        data = {"MES": generate_bars(cfg.instruments["MES"],
                                     datetime(2026, 1, 5, tzinfo=UTC), 30, 1, seed=seed)}
        result = Backtester(cfg).run(data)
        if result.failed:
            fail_ts = result.equity_curve[-1].ts
            later = [t for t in result.trades if t.entry_ts > fail_ts]
            assert not later, f"seed {seed}: {len(later)} trades opened after failure"


def test_engine_detects_a_real_edge():
    """Validation: on a tape with a genuine persistent trend, the momentum
    strategy must make money.

    This is the control experiment. A backtester that loses on every input is
    indistinguishable from one with an inverted sign or double-counted costs,
    so the engine has to be caught finding a profit that really is there before
    its losses on realistic data can be trusted.
    """
    cfg = BotConfig.load("futures_50k_trailing")
    mes = cfg.instruments["MES"]

    # A steady uptrend with modest noise — the regime momentum is built for.
    rng = np.random.default_rng(3)
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    bars, price = [], 5000.0
    for day in range(30):
        day_start = start + timedelta(days=day)
        if day_start.weekday() >= 5:
            continue
        for minute in range(390):
            price += rng.normal(0.35, 1.1)      # persistent upward drift
            o = price - rng.normal(0, 0.4)
            h = max(o, price) + abs(rng.normal(0, 0.5))
            l = min(o, price) - abs(rng.normal(0, 0.5))
            # Volume must vary: the breakout filter requires a surge above
            # the 20-bar average, so a constant-volume tape produces no trades
            # at all and would make this test silently vacuous.
            volume = float(rng.lognormal(mean=7.3, sigma=0.5))
            bars.append(bar(day_start + timedelta(minutes=minute),
                            mes.round_price(o), mes.round_price(h),
                            mes.round_price(l), mes.round_price(price), volume))

    result = Backtester(cfg, strategies=["momentum_breakout"]).run({"MES": bars})
    m = compute(result.trades, result.equity_curve)

    assert m.trades >= 5, f"strategy did not engage with a clear trend ({m.trades} trades)"
    assert m.net_pnl > 0, (
        f"engine failed to profit from an unambiguous uptrend: "
        f"{m.trades} trades, net ${m.net_pnl:,.2f}, expectancy {m.expectancy_r:.3f}R. "
        f"This points at a sign error or cost double-count, not a weak strategy."
    )
    assert m.expectancy_r > 0


def test_strategies_only_read_past_bars():
    """Feeding the strategy a truncated tape must not change the signals it
    already produced. This is the end-to-end lookahead check."""
    from bot.signals import make_strategy

    cfg = BotConfig.load("futures_50k_trailing")
    mes = cfg.instruments["MES"]
    bars = generate_bars(mes, datetime(2026, 1, 5, tzinfo=UTC), 15, 1, seed=77)

    for name in ("momentum_breakout", "scalp_pullback"):
        params = cfg.strategies[name]
        tf_bars = resample(bars, params.timeframe_minutes) if params.timeframe_minutes > 1 else bars
        cut = len(tf_bars) // 2

        strat = make_strategy(name, params, mes)
        full = strat.build_cache(tf_bars)
        partial = strat.build_cache(tf_bars[:cut])

        for i in range(cut):
            a = strat.on_bar(full, i)
            b = strat.on_bar(partial, i)
            assert (a is None) == (b is None), f"{name}: signal at bar {i} depends on future bars"
            if a and b:
                assert a.side == b.side and a.entry_price == pytest.approx(b.entry_price)
