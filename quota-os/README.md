# QuotaOS — the operating system for a sales org

**Your CRM tells you a rep is at 41% of quota. It does not tell you whether that's a problem.**

QuotaOS is one application covering the three systems a sales organization
normally buys separately — the CRM, the LMS, and the ATS — on the argument that
the value is in the joins between them, not the boxes themselves.

- A rep gets flagged as at-risk, and the course that addresses that exact signal
  is one click away on their page.
- A candidate clears a scored interview loop and lands on the org chart with a
  ramp window and every mandatory certification already enrolled.
- A certification lapses and it shows up as **compliance exposure on the VP's
  dashboard**, not as an unread email from the LMS.

## Run it

```bash
python3 quota_db.py   # build and seed the database (safe to re-run)
python3 app.py
```

Open http://localhost:5003

Requires Python 3, Flask, and Werkzeug. Sign in with a demo login (printed by
`quota_db.py`). The AI features work with no API key — see
[The AI layer](#the-ai-layer) below.

## The 3-minute walkthrough

**1. Dashboard** (`/`) — Q3 attainment against a pace marker, pipeline coverage,
the band distribution across the org, and the number that has no business being
buried in an LMS: how many reps are selling on a lapsed mandatory certification.
Sign in as the owner, a manager, and a member in turn — the same page rescopes to
what each role is allowed to see.

**2. Greg Nowak** (`/rep/8`) — the reason this exists. Greg was President's Club
last year. His attainment column doesn't look catastrophic. His health score is
**47 — Watch**, and three signals say why: 28 points behind pace, 1.09x coverage
against $356K remaining, and 78% of his entire pipeline sitting in one deal. Next
to those signals are the three courses that address them, each with an **Assign**
button that writes the reason onto his plan.

**3. Sana Malik** (`/rep/6`) — the control. 7% attained at 39% quarter pace, and
correctly *not* flagged. She's 45 days into a 120-day ramp, so the engine grades
her against a quarter of pace rather than all of it. A health score that flags
every new hire is a health score managers learn to ignore.

**4. Training & certs** (`/training`) — per-track compliance across the org,
what lapses in the next 60 days, the instructor-led session calendar, and a
catalog where every course declares which health signal it remediates.

**5. AI resume search** (`/search`) — describe who you want in plain English
("enterprise closer who's carried $1M+ into healthcare with long security-review
cycles"). Every resume is scored on fit against *that brief*, with the resume
lines that earned the score and the gaps it didn't cover. Add a match straight
into a requisition's funnel.

**6. The interview loop** (`/application/1`) — generate a structured interview
and you get questions **plus the rubric they'll be scored against, written before
any answer exists**. That's what makes scores comparable between candidates.
Conduct it, and every answer is scored 0–100 against its own rubric line with the
quoted evidence that drove it, rolled into one recommendation: advance, hold, or
reject.

**7. Onboard** (`/application/1/onboard`) — move a candidate to offer and hire
them. They become a row on the org chart with a ramp window and every mandatory
certification track enrolled on day one. Go back to the dashboard and the new
seat is there, graded on a ramp curve.

## Import from Salesforce or HubSpot

`/import` (admin only) takes a CSV export from either platform and lands it in
QuotaOS. It reads the header row to work out which system it came from, maps their
pipeline stages onto our six, matches deal owners to reps by email then name, and
shows a full preview — what it will create, update, and skip, with a reason on
every skipped row — **before it writes anything**.

Unrecognized stages and unknown owners are surfaced rather than guessed at. You can
opt to create the missing reps, in which case they arrive with zero quota so they
don't distort org attainment until you set one.

**Re-importing is the point, not a hazard.** Deals are keyed on the Salesforce
opportunity id or HubSpot record id, so applying the same export twice updates in
place. And because no CRM export contains the date a deal was *originally* forecast
to close, the first import sets `close_date_original = close_date` — then every
later import moves `close_date` and leaves the original alone. That is how a deal
pushed three times shows up as slippage in QuotaOS when the CRM it came from could
never show it.

A worked example, straight from the test run: import a Salesforce export, then push
one deal a month and discount it, then re-import. Result — `0 created, 1 updated,
5 unchanged, 0 duplicates`, and Greg Nowak's health score drops with a new signal:

> **Close dates are moving** — 1 of 3 deals have slipped, the worst by 30 days.
> Repeated pushes usually mean the buyer's timeline was never real.

## The health engine

`health_engine.py` scores each rep 0–100 by asking the six questions a good
manager asks in a 1:1, and every point of the score is attributable to a signal:

| Question | Signals |
|---|---|
| Behind the calendar, or just behind the number? | `behind_pace`, `ahead_pace`, `ramping` |
| Enough pipeline left to catch up? | `thin_pipeline`, `low_coverage`, `empty_pipeline` |
| Are those deals actually being worked? | `stale_deals`, `some_stale` |
| Does every deal have a next step? | `no_next_step` |
| Are close dates holding? | `slippage`, `concentration` |
| Are they cleared to sell what they're selling? | `cert_expired`, `cert_missing`, `cert_expiring` |

Two decisions worth calling out:

- **Ramping reps are graded on a curve, not excused.** Expected attainment is
  quarter pace scaled by how far into ramp they are.
- **Signals carry a `fixes` key that matches `courses.fixes_signal`.** That's the
  join between "this rep is in trouble" and "here is the course for it" — the
  reason training and performance are in the same product.

## The AI layer

`ai_engine.py` has four capabilities, each with two implementations:

| | |
|---|---|
| `write_posting()` | req knobs → a finished job posting |
| `search_resumes()` | plain-English brief → ranked candidates with evidence and gaps |
| `build_interview()` | req + interview type → structured questions and a rubric |
| `score_interview()` | questions + answers → per-answer scores and a recommendation |

**With credentials**, these call Claude (`claude-opus-5`) — reading a resume the
way a recruiter does, and scoring an answer against a rubric rather than counting
keywords. Structured outputs are used everywhere the result feeds the database, so
the shape is validated at the API layer instead of being parsed hopefully.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 app.py
```

An `ant auth login` profile works too — credentials resolve the normal way and
nothing is hardcoded.

**Without credentials**, a deterministic local engine runs instead: template-based
postings, keyword-plus-synonym resume ranking, a question bank per interview type,
and answer scoring that rewards specificity and penalizes hedging. It's crude next
to the Claude path and it's labeled as such everywhere it appears — but it means
the repo clones and demos on a laptop with no key, no network, and no cost, and a
rate limit degrades a feature instead of breaking a page.

The sidebar always shows which engine is live, and every generated artifact —
posting, ranking, scorecard — is tagged with the engine that produced it. A
scorecard whose provenance is ambiguous is a scorecard nobody should act on.

## The seed data

Twelve reps, each an archetype that exercises a different path through the engine,
because a demo where everyone is fine proves nothing:

| Rep | The story |
|---|---|
| Alicia Trent | Crushing it — the control |
| Ben Okafor | Boring and on track — the other control |
| Sana Malik | 45 days into a 120-day ramp — must *not* read as at-risk |
| Greg Nowak | Good last year, pipeline gone this year, one whale deal left |
| Dev Patel | 5x coverage, none of it touched in a month — happy ears |
| Rachel Kim | On track, clean hygiene |
| Omar Haddad | Selling well on a lapsed mandatory certification |
| Lena Fischer | Decent pipeline, half the team's activity |
| Chris Boone | SMB volume machine, quota already met |
| Maya Lindqvist | Behind, and no next step on most of the pipeline |
| Tyler Grant | Eight months in, fully ramped, zero open pipeline |
| Nadia Rahman | Steady, but never started a mandatory certification |

Greg and Dev matter most. Both have a green-looking number somewhere on their
record, and both are in trouble for reasons a quota-attainment column cannot show
you.

Everything is dated relative to today, so the demo never goes stale.

## Files

```
app.py            Flask routes — the floor, the bench, the funnel
health_engine.py  Rep and org scoring. No AI: a manager assigning remediation
                  wants the same answer every time.
ai_engine.py      Claude-backed hiring intelligence + offline fallbacks
importer.py       Salesforce / HubSpot CSV import — sniffing, stage mapping,
                  dry-run planning, idempotent apply
auth.py           Login, roles, tenant scoping, CSRF, audit trail
db.py             SQLite locally, Postgres in production
config.py         Environment-driven settings with a fail-fast production guard
quota_db.py       Schema build and demo seed
schema.sql        Twelve tables across the three surfaces
templates/        Jinja templates, one inline design system in base.html
```

## Security and tenancy

Multi-tenant with real authentication. Every tenant-owned table carries `org_id`
and every query filters on it, including where a join would make it redundant —
one forgotten filter should leak nothing. Four roles gate routes (`member` →
`manager` → `admin` → `owner`) and row access below that: a member sees only their
own seat, a manager only their team.

CSRF on every POST, scrypt-hashed passwords, `HttpOnly`/`SameSite`/`Secure`
cookies, config entirely from environment, and an `audit_log` recording every
AI-generated artifact and score for employment-decision compliance.

## Status

Runs, and the security model is tested — but it is not yet in production. SQLite
needs to become Postgres and the rate limiter needs to leave process memory before
real customer data lands in it. See [DEPLOY.md](DEPLOY.md) for exactly what is
done, what is left, and the AI-hiring regulation you need counsel on.
