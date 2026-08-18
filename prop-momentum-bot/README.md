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
python scripts/train_model.py  --firm futures_50k_trailing --symbols MES --days 250
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

## Your trading window

The bot only trades **08:00–12:00 America/Chicago**, configured in
`config/risk.yaml`. Three times, not two, because "stop trading at noon" and
"be flat at noon" are different rules:

```yaml
trading_window:
  timezone: "America/Chicago"
  start:      "08:00"   # first moment a position may be opened
  last_entry: "11:00"   # after this, manage only — no new positions
  end:        "12:00"   # everything flattened, unconditionally
```

The window is a hard constraint layered *on top of* each instrument's session —
the bot trades only where the two overlap. For MES (08:30–15:00 CT) that leaves
08:30–11:00 for entries, which is the highest-momentum stretch of the equity
session. For FX it captures the London/NY overlap.

`end` overrides the firm's overnight policy: even on a firm that permits
overnight holds, nothing stays live past noon, because you are not at the desk
to manage it.

The timezone is an IANA name, not "CST", so CST/CDT transitions are handled
automatically. Writing "CST" would silently shift your window by an hour twice
a year.

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

## Machine learning

Two things, both chosen because they are tractable on a few hundred noisy
samples. Neither of them predicts price.

### 1. Trade filtering (meta-labelling)

The strategy still decides *where* a setup exists. The model only estimates
**P(this particular signal reaches its target before its stop)** and drops the
weak ones. That is a far easier problem than generating entries from scratch,
where a model must be right about direction, timing and geometry at once.

22 scale-free features per signal — ADX, ATR expansion, EMA structure in ATR
units, VWAP distance, volume ratio, bar shape, momentum over two horizons,
time of day, minutes left in your window. Everything is divided by ATR or by
price so a model trained on MES can say something about EURUSD.

Labels come from **triple-barrier** forward simulation with the *same
pessimism as the execution engine*: when one bar spans both barriers, the stop
is assumed first. If labelling were optimistic while execution is pessimistic,
the model would be trained to prefer exactly the trades that lose in reality.

The filter sits **before** the risk guard, never instead of it. It can only
remove trades, so a model bug costs opportunity, never risk-limit compliance.
No model loaded means the bot degrades to the rule-based strategy; a scoring
error means that signal is rejected, because an unscored signal is unknown and
unknown must not count as good.

### 2. Regime detection

k-means over six regime features — ADX, ATR level, ATR expansion, trend
extension, directional persistence, and an efficiency ratio (net move ÷ summed
move; 1.0 is a straight line, 0 is chop). Fitted on a training window and
applied forward, never re-fit on the bars it labels. A dwell filter suppresses
flips shorter than 10 bars so the labels do not thrash.

Clusters get interpreted names from where their centroids sit:

```
regime                        bars   share    ADX  ATRexp  effic
range / quiet                 6642   42.7%   20.2    0.83   0.22
trending / quiet              3262   21.0%   38.1    1.01   0.42
choppy / volatile             2550   16.4%   19.9    1.15   0.16
trending / expanding          3096   19.9%   24.8    1.33   0.21
```

And then the actionable part — where the strategy actually works:

```
MOMENTUM BREAKOUT PERFORMANCE BY REGIME (R per trade, costs excluded)
  trending / quiet               110     0.258     43%
  trending / expanding            22     0.046     36%
  range / quiet                   68    -0.072     31%
```

Momentum makes money in trends and loses it in ranges, which is what theory
says it should do. That is the kind of finding worth acting on — after you have
confirmed it holds out of sample on your own data.

### How this is kept honest

Trading ML fails in specific, well-known ways, and each has a countermeasure
wired into `scripts/train_model.py`:

| Failure mode | Countermeasure |
|---|---|
| Training on the future | **Walk-forward only.** Train on the past, test on the next block. Never random k-fold. |
| Labels leaking backwards | **Purging.** A signal at 10:00 may not resolve until 10:40; if the test fold starts at 10:20 that training label was partly set by test-fold bars, so it is dropped. Plus a 60-minute **embargo**. |
| Finding patterns in noise | **Permutation test.** Re-run the identical pipeline on shuffled labels 20 times. If shuffled labels reproduce your AUC, it is noise. |
| Good AUC, no money | **Threshold sweep** showing what each cutoff does to win rate, expectancy, and how many trades survive. |
| Overfitting a tiny sample | Refuses to model fewer than 200 samples. |
| Silent lookahead in features | Tests truncate the tape and require identical feature values. |

The script has to clear three gates before recommending a model — AUC ≥ 0.55,
permutation p < 0.10, and a threshold that lifts expectancy while keeping ≥25%
of trades. On synthetic data it correctly refuses:

```
PERMUTATION TEST (same pipeline, shuffled labels)
  real AUC 0.519 | shuffled mean AUC 0.497 | p = 0.190
  -> shuffled labels reproduce this AUC 19% of the time. NOISE.

VERDICT: DO NOT TRADE THIS MODEL — AUC 0.519 < 0.55; p = 0.190 > 0.1.
```

Note the trap it caught: the threshold sweep showed a tempting **+0.116R lift
at threshold 0.45**, keeping 23% of trades. Taken alone that looks like a
discovery. The permutation test says it is noise — and that is exactly the
number that would have gotten traded without one.

The pipeline is verified in both directions. `test_pipeline_detects_an_injected_
edge` puts a real relationship in the data and requires the validator to find
it; `test_pipeline_reports_noise_as_noise` requires it not to find one that
isn't there:

```
NO edge (pure noise)     AUC 0.514  p=0.250  -> DO NOT TRADE
REAL injected edge       AUC 0.797  p=0.062  -> TRADE IT
```

A validator that only ever says "no edge" is not a validator.

### Using a trained model

```bash
python scripts/train_model.py --firm ftmo_100k --symbols EURUSD \
    --csv EURUSD=data/eurusd_1m.csv --model gbt --out models/eurusd.json

python scripts/run_backtest.py --firm ftmo_100k --symbols EURUSD \
    --csv EURUSD=data/eurusd_1m.csv --model models/eurusd.json
```

Models store their walk-forward AUC, permutation p-value and a `usable` flag in
their metadata. Loading one that failed validation prints a warning and keeps
going — your call, loudly flagged.

Two model types: `logistic` (interpretable coefficients — start here; if the
linear model finds nothing, be very suspicious of a nonlinear one that does)
and `gbt` (gradient-boosted trees, captures interactions, overfits more
eagerly). Both are implemented in NumPy, so the dependency surface stays at
numpy + PyYAML.

**Realistically:** ~1–3 trades a day in a 3-hour window is roughly 400 trades a
year. That is a thin sample for ML. Expect to need 2+ years of real 1-minute
data before the filter says anything trustworthy, and expect the honest answer
to often be "no usable edge". The pipeline is built to tell you that rather
than to sell you a model.

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
  ml/            features, triple-barrier labelling, models, purged
                 walk-forward validation, regime detection, signal filter
  reporting/     metrics and reports
scripts/         run_backtest.py, run_paper.py, monte_carlo.py, train_model.py
tests/           105 tests
```

## Tests

```bash
pytest -q                    # 105 tests
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
- **The ML layer needs far more data than a few months provides.** At 1–3
  trades a day it takes years to accumulate a sample worth modelling.
- **Regime labels are unsupervised**, so the cluster count `k` is a choice, not
  a discovery. Different `k` gives different regimes.
- **The model does not adapt online.** It is trained offline and loaded; there
  is no incremental refit as new trades close.

## Disclaimer

For research and education. Trading futures, forex, and crypto involves
substantial risk of loss. Prop firm evaluations have fees you can lose. Nothing
here is financial advice, and past or simulated performance does not predict
future results. You are responsible for anything you run with real money.
