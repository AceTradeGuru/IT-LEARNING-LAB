# Prop Momentum Bot

An intraday momentum trading system built to survive a prop firm evaluation on
**futures, forex, and crypto**. Two strategies — a 5-minute breakout engine and
a 1-minute pullback scalper — sit behind a risk engine that enforces each
firm's drawdown rules as hard constraints rather than guidelines.

```bash
pip install -r requirements.txt
python scripts/run_backtest.py --firm futures_50k_trailing --symbols MES MNQ --days 40
python scripts/monte_carlo.py  --firm futures_50k_trailing --symbols MES MNQ --runs 50
python scripts/run_paper.py    --firm ftmo_100k --symbols EURUSD --days 10 --verbose
pytest -q
```

---

## Read this first

**No bot passes a prop evaluation reliably, and anyone who tells you otherwise
is selling something.** What this repository does is split the problem in two:

| Half the problem | Who solves it | Status |
|---|---|---|
| *Don't blow the account* | The risk engine | **Solved, and measurably so** |
| *Make money* | The strategy + your data | **Unproven — that part is on you** |

The risk engine is the part worth having. Across 55 simulated evaluations on
synthetic data (30 futures, 25 forex), **zero accounts were blown** — every run
ended above its drawdown floor. That is a structural property of how position
size is computed, not luck, and the test suite pins it down.

The strategies, run on synthetic bars, lose money. That is the honest and
expected result: synthetic data contains no exploitable edge, so a momentum
system trading it pays costs and bleeds. **A positive backtest here would be
evidence of a bug, not of an edge.** Whether these strategies work depends
entirely on real market data, which you supply with `--csv`.

---

## Why the risk engine is the interesting part

Most trading bots size positions as a fixed percentage of equity. That is fine
until a losing streak, at which point fixed-percent sizing walks straight
through the drawdown floor and the evaluation is over.

This engine sizes each trade as the **minimum of three independent budgets**:

```
risk_per_trade = min(
    base_risk_pct × equity,                          # what a sane trader risks
    max_buffer_fraction × (equity − drawdown_floor), # ≤10% of room left
    max_daily_fraction  × remaining_daily_allowance, # ≤25% of today's allowance
)
```

The second term is the load-bearing one. As the account falls toward the floor,
the buffer shrinks, so the next trade shrinks with it — and because each loss
shrinks the *next* budget again, the sequence decays toward the floor instead of
crossing it. `tests/test_risk.py::test_a_max_loss_streak_cannot_reach_the_floor`
takes the maximum permitted loss sixty times in a row and never breaches.

Watch it collapse on a $50k futures account with a $2,000 trailing drawdown:

| Equity | Buffer to floor | Size | Risk |
|---:|---:|---:|---:|
| $50,000 | $2,000 | 1 contract | $125 |
| $49,600 | $1,600 | 1 contract | $125 |
| $49,200 | $1,200 | 0 — blocked | $0 |
| $48,400 | $400 | 0 — blocked | $0 |

On top of sizing, a single **risk guard** vetoes trades. Every order in the
system — backtest, paper, and live alike — passes through `RiskGuard.evaluate()`,
so there is no code path to a broker that skips these checks:

- stops trading at **60% of the daily loss limit**, not 100%, so an open
  position has room to move while it is being closed
- stops at **80% of total drawdown**
- refuses any trade whose worst case (this stop **plus** every open position
  stopping out) would breach the floor
- halts the session after 3 consecutive losses — evaluations die in tilt
  streaks far more often than in single bad trades
- caps concurrent, per-instrument, and *correlated* positions (long MES and
  long MNQ is one bet, not two)
- flattens before the session close where overnight holds are forbidden
- **stops trading once the target is reached** — protecting a completed pass is
  worth more than one more trade

---

## Firm rule profiles

The drawdown model is the single biggest structural difference between firms,
and it changes correct behaviour completely. All three are implemented:

| Model | Floor follows | Example | What it punishes |
|---|---|---|---|
| `static` | Nothing — fixed from day one | FTMO-style | Nothing; profit is a permanent buffer |
| `eod_trailing` | Highest **end-of-day balance** | Topstep-style | Banking gains raises tomorrow's floor |
| `intraday_trailing` | Highest **equity incl. open profit** | Apex-style | Round-tripping a winner, brutally |

Under `intraday_trailing`, a trade that runs +$800 and returns to flat has
permanently cost $800 of buffer even though nothing was booked. Configured in
`config/firms.yaml`, along with daily loss limits, profit targets, minimum
trading days, consistency rules, and trail-freeze levels. Drop your firm's exact
numbers in there — no code changes needed.

```bash
python scripts/run_backtest.py --list-firms
```

---

## The strategies

Both are momentum-family. Neither ever fades a move.

**`momentum_breakout`** (5m) — stacks four filters and requires all of them:
EMA 9/21/50 aligned for trend bias; ADX above 20 to exclude chop; close beyond
the prior 20-bar Donchian channel; and volume above its 20-bar average. Entry is
on the **close** of the confirming bar. Stop 1.5 ATR, target 3.0 ATR (2R),
breakeven at 0.75R, trails after 1R.

**`scalp_pullback`** (1m) — waits for a trend, lets price retrace to the fast
EMA, and enters when the retrace *fails*. The `pullback_max_atr` parameter
enforces the distinction that matters: a shallow pullback that resumes is
continuation and tradeable; a deep one means the trend is breaking. Filtered by
session VWAP and RSI. Stop 1.0 ATR, target 1.5 ATR, no trail — scalps take the
target.

Thresholds expressed as a fraction of price get **per-asset-class overrides**,
because index futures run ~0.17% ATR on 5-minute bars while EURUSD runs ~0.06%.
A single global threshold silently filters out every forex signal — which it did,
until the overrides went in.

---

## Honesty about the backtest

Backtests flatter you. Each of these is corrected, and every correction makes
the results worse:

- **Spread** — you buy the ask and sell the bid; mid-price fills invent money
- **Slippage** — stops are market orders in motion and fill worse than the
  trigger; limit targets get **no** positive slippage
- **Gaps** — a bar that opens beyond your stop fills at the open, not the stop
- **Stop-before-target** — when one bar covers both, the engine assumes the stop
  filled first. Assuming otherwise is how backtests manufacture win rates
- **Costs on every round turn**, reported as a separate line so the drag is
  impossible to miss
- **A cost-to-risk gate** rejects any trade whose commission exceeds 15% of the
  money risked — a stop that tight needs an absurd win rate just to break even

Three bugs found and fixed by these controls during development, all of which
would have shown up as fictional profit:

1. **Stop-update ordering.** Stops were moved to breakeven on a bar's *close*,
   then that same bar's *low* was tested against the new stop — so positions
   were stopped out by price action that happened before the stop existed. The
   scalper showed a 3.5% win rate. Fixing the order took it to 38%.
2. **Permanent tilt-halt.** The consecutive-loss circuit breaker never reset, so
   one bad afternoon halted the bot for the rest of the evaluation. It is a
   *session* rule now.
3. **Instrument volatility off by 10×.** `typical_atr_points` claimed EURUSD
   moves 9 pips a day; it moves 60–80. Every FX stop derived from it was
   unrealistically tight, and the cost gate correctly rejected all of them.

### The control experiment

A backtester that only ever loses is indistinguishable from one with a sign
error. So `tests/test_engine.py::test_engine_detects_a_real_edge` builds a tape
with a genuine persistent uptrend and asserts the engine profits from it:

```
CONTROL (genuine uptrend): 13 trades, win% 92.3, PF 1391.87,
                           expectancy +1.589R, net $3,449.35
```

It finds edge when edge exists, and reports its absence when it doesn't. That is
what makes the negative results on realistic data trustworthy.

---

## Using your own data

Synthetic bars prove the plumbing. Only your data can say anything about edge.

```bash
python scripts/run_backtest.py --firm ftmo_100k --symbols EURUSD \
    --csv EURUSD=data/eurusd_1m.csv --trades
```

CSV needs `timestamp,open,high,low,close,volume`. Timestamps may be ISO-8601 or
a unix epoch; naive values are read as UTC. Feed **1-minute** bars — coarser
timeframes are resampled internally. Non-monotonic timestamps are a hard error,
because out-of-order data silently corrupts every indicator.

A workflow that will not fool you:

1. Backtest on 6–12 months of real data, per instrument, one strategy at a time
2. Compare against `--no-costs` — if the edge lives entirely in the cost
   difference, there is no edge
3. Run `monte_carlo.py` for a pass-rate distribution with a confidence interval
4. Paper trade for a month and check it matches the backtest
5. Only then consider a funded evaluation

Step 4 is the one people skip and the one that catches everything.

---

## Live trading

`PaperBroker` is fully implemented and is what `run_paper.py` drives. The three
live adapters — `CcxtCryptoBroker` (crypto), `OandaFxBroker` (forex),
`TradovateFuturesBroker` (futures) — are written against each venue's documented
REST API but **ship disabled** and raise `AdapterDisabled` on connect.

Enabling one is deliberate: install the SDK, export the credentials the adapter
names, and construct it with `enabled=True`. Credentials are read **only** from
the environment, never from config, so a checked-in config can never leak a key.

**These adapters have not been executed against a live endpoint from this
repository.** Treat every one as unverified until you have run it against the
venue's own demo environment and reconciled fills by hand. Do not point one at a
funded evaluation before that.

The live loop reconciles its own position view against the broker's on every
cycle and halts new entries on a mismatch — an engine that has lost track of
what it owns must not size a new trade off a stale equity figure.

---

## Layout

```
config/          firms.yaml, instruments.yaml, strategies.yaml, risk.yaml
bot/
  config.py      typed config loading — a YAML typo is an import-time error
  indicators.py  EMA/ATR/ADX/RSI/VWAP/Donchian — all causal, all tested
  data/          types, feeds (CSV + synthetic generator), resampling
  signals/       strategy base + the two strategies
  risk/          rules (drawdown models), account, sizing, guard
  engine/        fill model, portfolio, backtest, live loop
  brokers/       adapter interface, paper broker, disabled live stubs
  reporting/     metrics and reports
scripts/         run_backtest.py, run_paper.py, monte_carlo.py
tests/           70 tests
```

## Tests

```bash
pytest -q                    # 70 tests
pytest tests/test_risk.py -q # the ones that matter most
```

A strategy bug costs money slowly; a risk engine bug ends the evaluation in one
afternoon. The suite reflects that — the heaviest coverage is on drawdown
models, day-boundary handling, sizing collapse, and guard vetoes.

Causality is tested directly: every indicator is checked to produce identical
values when future bars are truncated away, and both strategies are checked to
emit identical signals on a truncated tape. The whole backtest rests on that
assumption, so it is asserted rather than merely intended.

---

## Known limitations

- **Funding rates are not modelled** for crypto perpetuals. Intraday styles are
  barely affected; anything holding for hours is not.
- **No tick data**, so intrabar fill order is assumed (stop first, pessimistic).
- **Single-account only** — no copy-trading across multiple evaluations.
- **The news blackout takes a calendar you supply**; no feed is bundled.
- **Correlation is configured, not measured.** The groups in `strategies.yaml`
  are static; the engine does not compute rolling correlation.
- **Synthetic data is not a market.** It has regimes, volatility clustering,
  intraday seasonality, and fat tails, but no real structure to exploit.

## Disclaimer

For research and education. Trading futures, forex, and crypto involves
substantial risk of loss. Prop firm evaluations have fees you can lose. Nothing
here is financial advice, and past or simulated performance does not predict
future results. You are responsible for anything you run with real money.
