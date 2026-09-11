# Royal Grind — AI sales intelligence and execution

**Your prospecting database tells you a company exists. It does not tell you to
call them this morning.**

Royal Grind is one application covering the loop a salesperson actually runs —
**find → understand → call → close → automate** — on the argument that the value
is in the joins between those steps, not in owning the biggest contact list.

- A prospect reaches the top of today's list, and the **reason** is on the card:
  a $42M facility announcement eleven days ago, and a VP of Supply Chain who
  started nine weeks ago.
- Next to the number is **how much to trust it** — verified by a connect
  twenty-one days ago, or constructed from a dialing pattern and never confirmed.
- The call is placed, logged, transcribed and summarized; the follow-up email is
  drafted and the next action lands on the rep's list with a date on it.
- And the outcome is attributed back to the signals that caused the call, so
  next quarter the list is ranked by **what actually converted here**, not by
  what a product manager guessed.

## Run it

```bash
python3 grind_db.py   # build and seed the demo brokerage (safe to re-run)
python3 app.py
```

Open http://localhost:5005. Requires Python 3, Flask and Werkzeug.

Sign in as `owner@royalgrind.example`, `manager@royalgrind.example`, or
`rep@royalgrind.example` — password `grind-demo-2026`. The same pages rescope to
what each role is allowed to see.

**No API key and no phone account are needed.** The AI layer falls back to a
deterministic offline engine and the phone layer to a call simulator, both
labelled as such on every screen they touch. See
[Three engines](#three-engines-and-what-happens-without-them).

## The 4-minute walkthrough

**1. Today** (`/`) — the product in one screen. Twenty-five people ranked by
opportunity, each with the buying signal that put them there, the number to dial
rendered in the colour of how much it can be trusted, and a Call button. Above
it, the hours *your own* calls actually connect — from your call history, not an
industry study.

**2. Vantage Aerospace** (top of the list) — the record. Score 95, and the right
rail breaks down every point of it: `+19.3` a new facility announced eleven days
ago, `+16` a verified direct dial, `+14` a decision maker sixty-three days into
the role, `+10` three prior conversations. Nothing in that column is a model
output. A rep who cannot see the inputs will not trust the output.

**3. Press Call.** The compliance gate runs first — suppression list, the
prospect's local time, the recording rules for both jurisdictions — and writes
the basis onto the call row. Then the console: your opening, the discovery
questions in order, the objections you will actually hear and what to say back,
and the voicemail if nobody picks up.

**4. Log it.** One form. Behind it, five things happen: the disposition is
stored, the outcome is attributed to the signals that were live when you dialled,
the number's trust level is updated (a connect *is* a verification), the cadence
starts or stops, and the debrief writes a summary, a dated next action, and a
drafted follow-up email.

**5. Signal performance** (`/intelligence`) — the point of all of it. Every
signal against what it actually produced. In the seeded demo, *public service
complaint* runs about 1.2x the average and gets scored up; *new supply-chain
leadership* runs about 0.7x and gets scored down. **Nobody typed those numbers
in** — see [The learning loop](#the-learning-loop).

**6. Data quality** (`/data`) — not a coverage percentage. The count of records a
rep will dial this quarter that cannot reach anybody, what that costs in hours,
and the specific contacts to fix first with a button next to each.

**7. Executive view** (`/exec`) — pipeline sourced by the platform, hours per
meeting, win rate, and the model bill, with the arithmetic shown.

## Why not "a better ZoomInfo"

The strategy this was built from rates the parts honestly: a modern dashboard is
a 3 out of 10, phone integration a 4, and *accurate contact information* a 9 —
with *keeping hundreds of millions of contacts continuously updated* a 10.

So this does not try to win on database size. It wins on the layer above it:
**turning intelligence into a prioritized action plan, giving the rep the right
message, connecting the call, learning from the outcome, and driving the next
step.** Three design consequences run through the whole codebase.

### 1. Every field is a claim, not a string

`contact_points` is a table, not three columns on `contacts`. A phone number
carries its source, the method it was last checked by, the date, and a
confidence that **decays** — anything verified more than ninety days ago reads as
*probable* today whatever it said when it was written. The UI renders that
difference in colour, always:

| | |
|---|---|
| `verified` | checked against the authoritative system and it answered |
| `probable` | a provider or pattern match, never independently confirmed |
| `guessed` | built from a naming convention; a coin flip with a story |
| `catch_all` | the domain accepts everything, so acceptance proves nothing |
| `invalid` | checked and rejected |

A platform that renders a verified direct dial and a pattern guess in the same
black text is lying by omission, and the rep finds out one dial at a time.

### 2. The dialer is the best verification service in the building

When a call comes back `bad_number`, that is a harder signal than any vendor's
API — it is first-party, it is four minutes old, and it cost nothing because the
call was being made anyway. `enrichment.record_dial_result()` writes it back:
three hard failures retire a number, and a connect promotes it to `verified`
with `source = connected_call`.

In the seeded demo that is already **270 fields verified by a connect and 25
numbers retired by failure**. Over a quarter of real usage, on the accounts this
org actually sells to, it is the data asset a competitor cannot license.

### 3. Reachability is scored, not filtered

The instinct is to hide every prospect without a verified direct dial. That
empties the list. Instead a guessed number costs points and *says so on the
card*, and the rep decides whether a 90-fit company is worth dialing a
switchboard for.

## The scoring engine

`signal_engine.py` scores every target 0–100 by asking four questions in the
order a good salesperson asks them, and attaches the reasons — so the score is
never the answer, it is the index into the explanation.

| Question | What it reads |
|---|---|
| **Why now?** | Live buying signals, decayed on a half-life and weighted by whether this broker can haul the freight they imply |
| **Are they a fit?** | Mode overlap, load volume, revenue band, home-market density |
| **Can I reach them?** | Verification status and age of each number and address, prior hard failures |
| **Right person, burned yet?** | Role type, tenure, prior conversations, recent attempts, fatigue |

Twelve signal types ship, each with a half-life and an implied freight need:

`new_facility` `expansion` `logistics_hire` `leadership_change` `carrier_trouble`
`contract_window` `seasonal_peak` `new_product` `funding` `compliance_event`
`facility_closure` `tariff_shift`

Four decisions worth calling out:

- **Timing decays on a half-life, not a cliff.** A facility announcement is worth
  most in week one and something in week six. A hard 30-day expiry makes the list
  lurch; exponential decay makes it drift, which is how the underlying reality
  behaves.
- **New in role is worth 14 points.** The most under-used field in B2B sales.
  Somebody 30–180 days into a logistics role is rewriting the vendor list, has
  budget authority they have not spent, and owes the incumbent nothing.
- **The top of the range is compressed, not clamped.** A hard `min(raw, 100)`
  throws away ordering exactly where a rep needs it — four different records all
  read "100" and the list stops being ranked. The curve approaches 100 without
  reaching it.
- **Nothing in here calls a model.** A rep who gets a different list each time
  they refresh stops believing the list.

## The learning loop

Everything above is a *prior* — an experienced broker's opinion about what a
facility announcement is worth. What it is actually worth is an empirical
question, and every call this platform places is one observation of it.

Each call freezes the signals that were live onto the call row. When it resolves,
`record_outcome()` credits every one of them. `learned_weights()` then computes
each signal's value per attempt against the org-wide average and turns it into a
multiplier on the base points.

Two things stop that being a feedback loop instead of learning:

- **Shrinkage.** The observed rate is blended toward 1.00 in proportion to sample
  size; at 40 attempts the evidence and the prior get equal say. A signal with
  four attempts and one meeting has a 25% meeting rate and means nothing, and an
  engine that chases it reorders the whole floor's morning on noise. The UI marks
  low-volume signals **"too few to judge"** rather than drawing a lift number
  somebody will quote.
- **A clamp at 0.55x–1.60x.** The sample was drawn by a scoring function that
  already favoured high-scoring signals. Uncapped, a lucky signal gets scored up,
  so it gets called more, so it accumulates more outcomes, so it gets scored up
  again.

**An org on its first day gets every weight at 1.00 and the engine behaves
exactly as the priors say.** That is the correct behavior, not a limitation to
apologize for. The alternative is a cold-start system that pretends to know
something.

The demo's numbers are not hand-authored. The seed runs sixty business days of
calls through the real simulator — whose outcome distribution shifts with data
quality, contact role and hour of day — and feeds them through the same
`record_outcome()` path a live call takes. Rebuild with a different RNG seed and
the lift table changes.

## Three engines, and what happens without them

| Layer | With credentials | Without |
|---|---|---|
| **Intelligence** (`ai_engine.py`) | Claude (`claude-opus-5`) writes research briefs, call plans, call analyses, follow-up emails, and parses plain-English search | Deterministic templates and regex extraction |
| **Telephony** (`telephony.py`) | Vonage Voice API — JWT auth, NCCO, two-channel recording, signed event webhooks | A call simulator that produces a realistic outcome *and transcript* from the record's data quality, the contact's role and the hour |
| **Data** (`enrichment.py`) | A licensed verification vendor behind two methods | Deterministic pseudo-verification, every field stamped `offline simulation` |

The fallbacks are crude next to the real paths and **they are labelled as such
everywhere they appear** — in the sidebar, on every generated artifact, and in a
banner across any simulated call. A call script whose provenance is ambiguous is
a script a rep should not read aloud to a customer.

They exist for three reasons: the repo clones and demos on a laptop with no key,
no network and no cost; a rate limit degrades a feature instead of breaking a
page mid-call; and CI can exercise the whole product without spending money to
tell you a template renders.

Structured outputs are used everywhere a model result reaches the database. A
call analysis writes a disposition and a follow-up date onto real rows; parsing
that out of prose hopefully is how a CRM ends up with a task due on the 31st of
February.

## Compliance is a brake, not a banner

`compliance.py` runs **before** the dialer, and `app.py` has no path around it.

- **Suppression.** Numbers and addresses nobody in this org may contact, checked
  on every dial and every send, normalized so `+1 (816) 555-0143` and
  `8165550143` are one entry. There is no override flag — an override flag on a
  do-not-call list is a do-not-call suggestion.
- **Quiet hours** in the *prospect's* timezone. A broker in Kansas City dialing
  California at 7:40am local is the easiest violation to commit by accident, and
  a list sorted by opportunity score will commit it every morning unless
  something stops it. An unresolvable timezone warns rather than failing open.
- **Recording consent.** Fourteen all-party-consent states in one reviewable
  constant. The stricter of the two jurisdictions governs, an unknown prospect
  location is treated as strict, and the basis is written onto the call row *at
  the time* — it cannot be reconstructed afterwards.
- **Subject access and erasure.** One click exports everything held about a
  person as JSON, *including the AI-generated material* — the brief written about
  them, the plan built to persuade them, the analysis of what they said. Somebody
  asking what a system holds about them is entitled to the parts that were
  inferred, not just the parts that were typed in. Erasure suppresses their
  numbers **before** deleting them, so the next enrichment run cannot resurrect
  the person who just asked to be forgotten, and keeps a redacted commercial
  record so the audit trail survives.
- **Retention.** A clock on personal data, with `python3 compliance.py --purge`
  for cron and `--dry-run` by default.

**None of this is legal advice and installing it does not make anyone
compliant.** It is the mechanism to enforce a policy your counsel sets, and the
record of what the system did. Read [DEPLOY.md](DEPLOY.md) before dialing anyone.

## Automation that knows when to stop

A prospect who does not answer is the majority of the list, and the majority is
where the money is. `sequences.py` runs cadences under three rules:

1. **A reply ends the sequence.** The moment somebody connects, books, or asks to
   be left alone, every cadence they are in exits and the tasks it queued are
   cancelled. Automation that keeps firing after a human conversation started is
   how a domain gets blocked. An exit is a success metric here, not a failure one.
2. **Steps generate drafts, never sends.** The gap between "the system wrote it"
   and "the customer received it" is a person, deliberately.
3. **Weekends do not count.** A three-day follow-up scheduled on a Thursday lands
   on Sunday, and a task due Sunday is a task done Tuesday.

## The seed

A Kansas City brokerage: 6 reps, 205 companies, 347 contacts, 822 contact points,
154 live signals, and ~3,500 simulated calls. Nine records carry the demo,
because one where every record is clean proves nothing about a product whose
thesis is that records are not clean:

| Record | What it proves |
|---|---|
| **Vantage Aerospace** | The hero. New facility, new VP, verified direct dial — and every point of the 95 is attributable |
| **Cedar Ridge Foods** | High fit, no direct line. The list does not silently hide what it cannot dial |
| **Ironwood Building Products** | The contact left four months ago and the record still says Director |
| **Halcyon Plastics** | Verified — in June. Reads as probable today |
| **Kestrel Chemical** | On the suppression list. Never reaches a rep |
| **Sandpiper Produce** | California. Blocked before 8am Pacific whatever the score says |
| **Longview Steel** | Flatbed-only shipper, LTL broker. High volume, negative fit, correctly buried |
| **Meridian Industries** | $4.2B enterprise. Real freight, wrong sales motion |
| **Thistle Distribution** | Six attempts, zero connects — and the engine blames the number, not the person |

Everything is dated relative to today, so the demo never goes stale.

## Files

```
app.py            Flask routes — the floor, the database, the learning loop
signal_engine.py  Opportunity scoring and the outcome-attribution loop. No AI:
                  a rep working a list wants the same answer twice.
ai_engine.py      Claude-backed research, call plans, call analysis, email,
                  plain-English search — each with an offline fallback
telephony.py      Vonage Voice API + a call simulator, behind one interface
enrichment.py     Provenance, verification, decay, and dial-result feedback
compliance.py     Suppression, quiet hours, recording consent, subject rights
sequences.py      Cadence automation that exits the moment a human replies
auth.py           Login, roles, tenant scoping, CSRF, audit trail
db.py             SQLite locally, Postgres in production
config.py         Environment-driven settings with a fail-fast production guard
grind_db.py       Schema build and demo seed
schema.sql        Nineteen tables across five surfaces
smoke_test.py     160 end-to-end checks, including the ones that must fail
templates/        Jinja templates, one inline design system in base.html
```

## Security and tenancy

Multi-tenant with real authentication. Every tenant-owned table carries `org_id`
and **every query filters on it**, including where a join would make it
redundant — one forgotten filter should leak nothing. Four roles gate routes
(`member` → `manager` → `admin` → `owner`) and row access below that: a member
works their own list, a manager their team's.

CSRF on every POST, scrypt-hashed passwords, `HttpOnly`/`SameSite`/`Secure`
cookies, config entirely from environment. The call webhook — the only
unauthenticated route — verifies an HMAC of the call id in constant time before
writing anything; without it, it is an open endpoint that writes transcripts into
a multi-tenant database and whose URL lives in a third party's config screen.

`smoke_test.py` asserts the failures: a member gets 403 on another rep's
prospect, one tenant gets 404 on another's records by id, an unsigned webhook is
refused, a POST without a CSRF token is rejected, and a production config missing
a `SECRET_KEY` refuses to boot.

## Status

Runs, and the security and compliance models are tested — but it has not placed a
real phone call. The Vonage and enrichment-vendor paths are written against
documented APIs and have not been exercised against live accounts, and neither
has the Postgres path. See [DEPLOY.md](DEPLOY.md) for exactly what is done, what
is stubbed, what is not wired up, and what needs counsel before you dial anybody.
