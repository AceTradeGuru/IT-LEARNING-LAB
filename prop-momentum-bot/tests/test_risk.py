"""Risk engine tests.

These are the tests that matter most. A strategy bug costs money slowly; a risk
engine bug ends the evaluation in one afternoon.
"""

from datetime import datetime, timedelta, timezone

import pytest

from bot.config import BotConfig, load_firm
from bot.data.types import ExitReason, Side, Signal, Trade
from bot.risk.account import Account, trading_day_of
from bot.risk.guard import RiskGuard, session_window
from bot.risk.rules import (
    EodTrailingFloor, FloorState, IntradayTrailingFloor, StaticFloor,
    make_floor, required_profit_for_consistency,
)
from bot.risk.sizing import size_position

UTC = timezone.utc


def make_trade(pnl: float, ts: datetime, symbol="MES", strategy="momentum_breakout") -> Trade:
    return Trade(
        symbol=symbol, side=Side.LONG, strategy=strategy, size=1,
        entry_ts=ts, entry_price=100.0, exit_ts=ts, exit_price=100.0,
        gross_pnl=pnl, costs=0.0, net_pnl=pnl,
        r_multiple=pnl / 100.0, exit_reason=ExitReason.TARGET, bars_held=5,
    )


# ----------------------------------------------------------------------
# drawdown models
# ----------------------------------------------------------------------

def test_static_floor_never_moves():
    rules = load_firm("ftmo_100k")
    floor = make_floor(rules)
    assert isinstance(floor, StaticFloor)
    at_start = FloorState(100000, 100000, 100000)
    after_profit = FloorState(100000, 150000, 150000)
    assert floor.floor(at_start) == floor.floor(after_profit) == 90000


def test_eod_trailing_floor_ignores_intraday_peaks():
    """The distinction that defines Topstep-style rules: intraday profit does
    not raise the floor, but a settled daily balance does."""
    rules = load_firm("futures_50k_trailing")
    floor = EodTrailingFloor(rules)
    intraday_spike = FloorState(50000, 51500, 50000)   # peak equity up, EOD flat
    settled = FloorState(50000, 51500, 51500)          # the day closed up
    assert floor.floor(intraday_spike) == 48000
    assert floor.floor(settled) == 49500


def test_intraday_trailing_floor_follows_unrealized_peaks():
    """Apex-style: a trade that runs +$800 and round-trips permanently costs
    $800 of buffer, even though no profit was ever booked."""
    rules = load_firm("futures_50k_intraday_trail")
    floor = IntradayTrailingFloor(rules)
    before = FloorState(50000, 50000, 50000)
    after_spike = FloorState(50000, 50800, 50000)
    assert floor.floor(before) == 47500
    assert floor.floor(after_spike) == 48300
    assert floor.floor(after_spike) > floor.floor(before)


def test_trail_freeze_caps_the_floor():
    rules = load_firm("futures_50k_trailing")  # freezes at 52000
    floor = make_floor(rules)
    runaway = FloorState(50000, 80000, 80000)
    assert floor.floor(runaway) == 50000, "frozen floor must stop trailing upward"


def test_all_three_models_start_below_starting_balance():
    for key in ("ftmo_100k", "futures_50k_trailing", "futures_50k_intraday_trail"):
        rules = load_firm(key)
        floor = make_floor(rules)
        state = FloorState(rules.starting_balance, rules.starting_balance, rules.starting_balance)
        assert floor.floor(state) < rules.starting_balance


# ----------------------------------------------------------------------
# account bookkeeping
# ----------------------------------------------------------------------

def test_trading_day_rolls_at_the_firms_hour():
    # CME rolls at 17:00 ET, so 18:00 ET Monday is already Tuesday's session.
    monday_evening = datetime(2026, 3, 2, 23, 0, tzinfo=UTC)  # 18:00 ET
    assert trading_day_of(monday_evening, "America/New_York", 17).day == 3
    # An FX firm rolling at midnight UTC keeps it on Monday.
    assert trading_day_of(monday_evening, "UTC", 0).day == 2


def test_daily_loss_resets_on_a_new_day():
    rules = load_firm("futures_50k_trailing")
    acct = Account(rules)
    day1 = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    acct.mark(day1, 0.0)
    acct.on_trade_closed(make_trade(-500, day1))
    acct.mark(day1, 0.0)
    assert acct.daily_loss_used == pytest.approx(500)

    day2 = day1 + timedelta(days=1)
    acct.mark(day2, 0.0)
    assert acct.daily_loss_used == 0.0, "the daily allowance must reset each session"


def test_session_loss_streak_resets_daily_but_running_streak_does_not():
    """The tilt-stop is a session circuit breaker. Carrying it across days
    would halt the bot permanently after one bad afternoon."""
    rules = load_firm("futures_50k_trailing")
    acct = Account(rules)
    day1 = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    acct.mark(day1, 0.0)
    for _ in range(3):
        acct.on_trade_closed(make_trade(-100, day1))
    assert acct.day_consecutive_losses == 3
    assert acct.consecutive_losses == 3

    day2 = day1 + timedelta(days=1)
    acct.mark(day2, 0.0)
    assert acct.day_consecutive_losses == 0
    assert acct.consecutive_losses == 3, "running streak still informs sizing"


def test_trading_days_counts_only_days_with_trades():
    rules = load_firm("futures_50k_trailing")
    acct = Account(rules)
    base = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    acct.mark(base, 0.0)
    acct.on_trade_closed(make_trade(100, base))
    acct.mark(base + timedelta(days=1), 0.0)   # a day with no trades
    acct.mark(base + timedelta(days=2), 0.0)
    acct.on_trade_closed(make_trade(100, base + timedelta(days=2)))
    assert acct.trading_days == 2


def test_failure_is_detected_when_equity_touches_the_floor():
    rules = load_firm("futures_50k_trailing")
    acct = Account(rules)
    ts = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    acct.mark(ts, 0.0)
    acct.mark(ts, -2000.0)   # unrealized loss straight to the floor
    assert acct.status.failed
    assert "drawdown" in acct.failed_reason.lower()


def test_unrealized_loss_can_breach_the_daily_limit():
    """Firms that count open positions against the daily limit will fail you
    on an unrealized drawdown — the account must model that."""
    rules = load_firm("futures_50k_trailing")
    assert rules.daily_loss_includes_unrealized
    acct = Account(rules)
    ts = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    acct.mark(ts, 0.0)
    status = acct.mark(ts, -1000.0)
    assert status.failed and "daily" in status.reason.lower()


def test_pass_requires_minimum_trading_days():
    rules = load_firm("futures_50k_trailing")   # min 5 days
    acct = Account(rules)
    ts = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    acct.mark(ts, 0.0)
    acct.on_trade_closed(make_trade(3500, ts))  # target cleared on day one
    status = acct.mark(ts, 0.0)
    assert not status.passed
    assert "minimum trading days" in status.reason


def test_consistency_rule_blocks_a_one_big_day_pass():
    rules = load_firm("futures_50k_trailing")   # best day <= 50% of profit
    acct = Account(rules)
    base = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    for i in range(5):
        ts = base + timedelta(days=i)
        acct.mark(ts, 0.0)
        acct.on_trade_closed(make_trade(3100 if i == 0 else 25, ts))
        acct.mark(ts, 0.0)
    assert acct.trading_days >= rules.min_trading_days
    assert acct.total_profit >= rules.profit_target
    assert not acct.status.passed
    assert "consistency" in acct.status.reason.lower()


def test_required_profit_for_consistency():
    rules = load_firm("futures_50k_trailing")   # 50%
    assert required_profit_for_consistency(rules, 2000) == 4000


# ----------------------------------------------------------------------
# sizing
# ----------------------------------------------------------------------

def make_signal(entry=5000.0, stop=4975.0, atr=25.0, symbol="MES") -> Signal:
    return Signal(
        ts=datetime(2026, 3, 3, 15, 0, tzinfo=UTC), symbol=symbol, side=Side.LONG,
        strategy="momentum_breakout", entry_price=entry, stop_price=stop,
        target_price=entry + 2 * (entry - stop), atr=atr,
    )


def test_size_shrinks_as_the_buffer_shrinks():
    """The core safety property: as the account approaches the floor, size
    falls monotonically and risk stays a bounded fraction of what is left.

    Note size approaches zero asymptotically rather than hitting it — risking
    10% of a $500 buffer is still a legitimate $50 trade. What must never
    happen is size holding steady, or growing, as the floor gets closer.
    """
    config = BotConfig.load("ftmo_100k")
    acct = Account(config.firm)
    ts = datetime(2026, 3, 3, 10, 0, tzinfo=UTC)
    signal = make_signal(entry=1.0850, stop=1.0800, atr=0.005, symbol="EURUSD")
    eurusd = config.instruments["EURUSD"]

    sizes, risks = [], []
    for loss in (0, 3000, 6000, 8000, 9500):
        acct.balance = config.firm.starting_balance - loss
        # Fresh day each time so the daily limit is not the binding constraint.
        acct.mark(ts + timedelta(days=len(sizes)), 0.0)
        result = size_position(signal, eurusd, acct, config.risk)
        sizes.append(result.size)
        risks.append(result.risk_amount)
        cap = acct.buffer * config.risk.max_buffer_fraction_per_trade
        assert result.risk_amount <= cap + 1e-6, (
            f"risked ${result.risk_amount} against a ${acct.buffer} buffer"
        )

    assert sizes[0] > 0
    assert all(a >= b for a, b in zip(sizes, sizes[1:])), f"size must not grow as buffer shrinks: {sizes}"
    assert sizes[-1] <= sizes[0] / 10, f"size must collapse near the floor: {sizes}"


def test_a_max_loss_streak_cannot_reach_the_floor():
    """The property the whole sizing model exists to produce: take the maximum
    permitted loss over and over and the floor is never reached, because each
    loss shrinks the next trade's budget."""
    config = BotConfig.load("ftmo_100k")
    acct = Account(config.firm)
    eurusd = config.instruments["EURUSD"]
    signal = make_signal(entry=1.0850, stop=1.0800, atr=0.005, symbol="EURUSD")
    base = datetime(2026, 3, 3, 10, 0, tzinfo=UTC)

    for day in range(60):
        ts = base + timedelta(days=day)
        acct.mark(ts, 0.0)
        result = size_position(signal, eurusd, acct, config.risk)
        if result.size <= 0:
            break
        acct.on_trade_closed(make_trade(-result.risk_amount, ts, symbol="EURUSD"))
        acct.mark(ts, 0.0)
        assert not acct.status.failed, (
            f"floor breached on loss {day + 1}: equity {acct.equity} floor {acct.floor}"
        )

    assert acct.equity > acct.floor


def test_size_never_risks_more_than_the_buffer_fraction():
    config = BotConfig.load("ftmo_100k")
    acct = Account(config.firm)
    acct.mark(datetime(2026, 3, 3, 10, 0, tzinfo=UTC), 0.0)
    result = size_position(
        make_signal(1.0850, 1.0800, 0.005, "EURUSD"),
        config.instruments["EURUSD"], acct, config.risk,
    )
    cap = acct.buffer * config.risk.max_buffer_fraction_per_trade
    assert result.risk_amount <= cap + 1e-6


def test_rejects_a_stop_tighter_than_the_minimum():
    config = BotConfig.load("futures_50k_trailing")
    acct = Account(config.firm)
    acct.mark(datetime(2026, 3, 3, 15, 0, tzinfo=UTC), 0.0)
    result = size_position(
        make_signal(entry=5000.0, stop=4999.75, atr=25.0),
        config.instruments["MES"], acct, config.risk,
    )
    assert not result.ok and "ATR" in (result.rejected_reason or "")


def test_rejects_when_costs_dominate_the_risk():
    """A stop so tight that commission is a third of the risk is uneconomic
    before the market does anything."""
    config = BotConfig.load("futures_50k_trailing")
    acct = Account(config.firm)
    acct.mark(datetime(2026, 3, 3, 15, 0, tzinfo=UTC), 0.0)
    # 0.6 point stop on MES = $3 risk/contract vs $1.24 round-turn commission.
    result = size_position(
        make_signal(entry=5000.0, stop=4999.4, atr=1.0),
        config.instruments["MES"], acct, config.risk,
    )
    assert not result.ok
    assert "cost" in (result.rejected_reason or "").lower()


def test_size_rounds_down_never_up():
    config = BotConfig.load("ftmo_100k")
    acct = Account(config.firm)
    acct.mark(datetime(2026, 3, 3, 10, 0, tzinfo=UTC), 0.0)
    eurusd = config.instruments["EURUSD"]
    result = size_position(make_signal(1.0850, 1.0800, 0.005, "EURUSD"), eurusd, acct, config.risk)
    steps = result.size / eurusd.size_step
    assert abs(steps - round(steps)) < 1e-6, "size must land on the lot grid"
    assert result.risk_amount <= acct.buffer * config.risk.max_buffer_fraction_per_trade + 1e-6


# ----------------------------------------------------------------------
# guard
# ----------------------------------------------------------------------

@pytest.fixture
def guard_setup():
    config = BotConfig.load("futures_50k_trailing")
    acct = Account(config.firm)
    ts = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)   # 10:00 ET, mid-session
    acct.mark(ts, 0.0)
    return config, acct, RiskGuard(config, acct), ts


def test_guard_approves_a_clean_signal(guard_setup):
    config, acct, guard, ts = guard_setup
    decision = guard.evaluate(make_signal(), {}, ts)
    assert decision.approved and decision.size > 0


def test_guard_blocks_outside_the_session(guard_setup):
    config, acct, guard, ts = guard_setup
    overnight = datetime(2026, 3, 3, 3, 0, tzinfo=UTC)  # 22:00 ET previous day
    acct.mark(overnight, 0.0)
    assert not guard.evaluate(make_signal(), {}, overnight).approved


def test_guard_blocks_near_the_close(guard_setup):
    config, acct, guard, ts = guard_setup
    # MES closes 16:00 ET = 21:00 UTC in March (EST); 20:55 UTC is 5m before.
    near_close = datetime(2026, 3, 3, 20, 55, tzinfo=UTC)
    decision = guard.evaluate(make_signal(), {}, near_close)
    assert not decision.approved and "close" in decision.reason


def test_guard_blocks_disallowed_asset_class(guard_setup):
    config, acct, guard, ts = guard_setup   # futures firm
    decision = guard.evaluate(make_signal(symbol="EURUSD"), {}, ts)
    assert not decision.approved and "not permitted" in decision.reason


def test_guard_enforces_the_daily_stop_fraction(guard_setup):
    config, acct, guard, ts = guard_setup
    acct.on_trade_closed(make_trade(-620, ts))   # 62% of the $1,000 daily limit
    acct.mark(ts, 0.0)
    decision = guard.evaluate(make_signal(), {}, ts)
    assert not decision.approved and "daily loss" in decision.reason


def test_guard_stops_before_the_actual_limit_not_at_it(guard_setup):
    """Stopping at 100% of the daily limit leaves no room for an open trade to
    move while it is being closed. The cushion is the point."""
    config, acct, guard, ts = guard_setup
    assert config.risk.daily_loss_stop_fraction < 1.0
    acct.on_trade_closed(make_trade(-601, ts))
    acct.mark(ts, 0.0)
    assert not acct.status.failed, "must not have actually failed yet"
    assert guard.check_halt() is not None, "but must already have stopped trading"


def test_guard_enforces_the_tilt_stop(guard_setup):
    config, acct, guard, ts = guard_setup
    for _ in range(config.portfolio.max_consecutive_losses):
        acct.on_trade_closed(make_trade(-10, ts))
    acct.mark(ts, 0.0)
    decision = guard.evaluate(make_signal(), {}, ts)
    assert not decision.approved and "consecutive losses" in decision.reason


def test_guard_enforces_the_correlation_cap(guard_setup):
    from bot.data.types import Position
    config, acct, guard, ts = guard_setup
    open_positions = {
        "MES:s": Position("MES", Side.LONG, 1, 5000, ts, 4975, 5050, "s", 25, 25),
        "MNQ:s": Position("MNQ", Side.LONG, 1, 18000, ts, 17900, 18200, "s", 100, 100),
    }
    # MGC is $10/point, so give it a stop its own tick scale supports.
    mgc_signal = Signal(
        ts=ts, symbol="MGC", side=Side.LONG, strategy="momentum_breakout",
        entry_price=2350.0, stop_price=2340.0, target_price=2370.0, atr=10.0,
    )
    # MGC is not in the equity_index group, so the correlation cap must not
    # block it — only an unrelated limit could.
    decision = guard.evaluate(mgc_signal, open_positions, ts)
    assert "correlation" not in decision.reason

    # Once MGC counts as an equity-index instrument, the cap of 2 must bite.
    config.portfolio.correlation_groups["equity_index"].append("MGC")
    decision = guard.evaluate(mgc_signal, open_positions, ts)
    assert not decision.approved and "correlation" in decision.reason


def test_guard_rejects_a_trade_whose_worst_case_breaches_the_floor(guard_setup):
    config, acct, guard, ts = guard_setup
    acct.balance = 48150   # $150 above the $48,000 floor
    acct.mark(ts, 0.0)
    decision = guard.evaluate(make_signal(), {}, ts)
    assert not decision.approved


def test_guard_halts_once_the_evaluation_is_passed(guard_setup):
    """Protecting a completed pass is worth more than another trade."""
    config, acct, guard, ts = guard_setup
    base = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    for i in range(6):
        day = base + timedelta(days=i)
        acct.mark(day, 0.0)
        acct.on_trade_closed(make_trade(550, day))
        acct.mark(day, 0.0)
    assert acct.total_profit >= config.firm.profit_target
    assert "target" in (guard.check_halt() or "")


def test_crypto_is_tradeable_at_the_weekend_but_futures_are_not():
    config = BotConfig.load("ftmo_100k")   # allows forex and crypto
    acct = Account(config.firm)
    saturday = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    acct.mark(saturday, 0.0)
    guard = RiskGuard(config, acct)

    crypto = guard.evaluate(
        Signal(saturday, "BTCUSDT", Side.LONG, "momentum_breakout",
               68000.0, 67100.0, 69800.0, 900.0), {}, saturday)
    forex = guard.evaluate(
        Signal(saturday, "EURUSD", Side.LONG, "momentum_breakout",
               1.0850, 1.0800, 1.0950, 0.005), {}, saturday)

    assert crypto.approved, f"crypto trades 24/7: {crypto.reason}"
    assert not forex.approved and "weekend" in forex.reason
