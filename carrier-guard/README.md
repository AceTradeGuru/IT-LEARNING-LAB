# CarrierGuard — the carrier vetting & fraud firewall

**Run the carrier before you tender the load.**

A broker is about to hand $78,000 of someone else's freight to a stranger who called
twenty minutes ago. The stranger gave an MC number. The MC number checks out in SAFER —
active authority, valid insurance, clean safety score — because **the MC number belongs
to a real, clean carrier.** It just doesn't belong to the person on the phone.

Every existing tool answers "is this MC number good?" That is the wrong question, and it
is why brokers who do their homework still get hit.

CarrierGuard asks three questions at once and scores the **disagreement** between them:

| | Source | What it tells you |
|---|---|---|
| 1 | **The government record** | Is this authority real, active, insured, and safe? |
| 2 | **The inbound identity** | Do the email and phone they contacted you from match the carrier of record? |
| 3 | **The network** | What has this MC number done to every other broker this month? |

A hijacked authority can forge a certificate of insurance. It cannot forge a boring,
stable dispatch contact history across four brokerages.

## Local development

Requires Docker (for Postgres) and Python 3.13+.

```bash
cd carrier-guard
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
docker compose up -d db
.venv/bin/python seed.py --reset
.venv/bin/python app.py
```

Open http://localhost:5002

`seed.py` prints four demo logins when it runs (password `carrierguard-demo`). It also
migrates, loads the demo data, then **re-scores every scenario and fails loudly if any
verdict has moved.** If you change a rule weight, that's your regression test — run it
before you commit. It refuses to run at all with `APP_ENV=production`, since those demo
logins share one well-known password.

```bash
.venv/bin/python seed.py --reset   # drop, migrate, reseed, verify
.venv/bin/python db.py             # migrate only
.venv/bin/python db.py --reset     # drop and migrate, no seed
```

## The 2-minute pitch demo

The home page has four one-click scenarios. Run them in this order.

**1. "The carrier you've hauled with for years"** → `CLEAR`, risk **0**

Great Lakes Freight. Everything agrees: 11 years of unbroken authority, contact details
match the record, and this exact dispatcher has a clean history across two brokers on the
network. Two signals fire and both are *green* — good history subtracts from the score.

**2. "The same MC number — but not the same company"** → `BLOCK`, risk **60**

Identical MC number. Identical spotless SAFER record. One difference: the email domain is
`greatlakesfreight-sys.com` instead of `greatlakesfreightsys.com`, and the callback number
is a VOIP line. The result page puts the record and the inbound details side by side, and
the mismatched fields turn red.

> This is the slide. Every competitor's tool returns "clean" on this carrier.

**3. "Clean paperwork, four different faces"** → `BLOCK`, risk **60**

Bayou Express. Nothing wrong on the government record — active authority, valid insurance,
7 years, 12 trucks. But the network has watched this MC number present **four different
dispatch emails to four different brokers in nineteen days.** No single broker could have
seen that pattern.

**4. "Not a fraudster — just genuinely risky"** → `REVIEW`, risk **58**

Summit Haul. Legitimate, probably fine — but a 90-day-old authority, insurance lapsing in
18 days, and a "terminal" that resolves to a mailbox store. The product's job here isn't
to block; it's to make you call before you tender.

**5. Then demonstrate the network effect live.**

From that Summit Haul result, click **Report fraud on this MC**, file a report, and you're
sent straight back to a fresh check on the same carrier:

```
before        58   REVIEW
after         80   BLOCK      +22  unconfirmed fraud report from another broker
```

One broker's bad afternoon protects every other subscriber within a second.

## How the score works

Every rule carries an explicit point weight, and the result page itemizes all of them —
a verdict a broker can't defend to their boss is worthless.

| Band | Score | Meaning |
|---|---|---|
| `CLEAR` | 0–29 | Tender it. Save the check ID on the load file as your due-diligence record. |
| `REVIEW` | 30–59 | Don't tender yet. Call the number **on file** — never the one you were given. |
| `BLOCK` | 60–100 | Walk away and report it. |

Weights are deliberately asymmetric: a false `REVIEW` costs a broker five minutes, a false
`CLEAR` costs them a truckload. Mitigating history (long clean tenure, an established
contact) subtracts points, but any single **critical** finding forces a `BLOCK` regardless
of how good the track record looks.

The signals, by source:

- **FMCSA** — authority not active, authority under 6 months old, revocation churn, no
  authority found at all
- **Insurance** — cargo or liability expired, expiring within 30 days, coverage below the
  value of this load
- **Safety** — out-of-service rate above the national average, dormant authority suddenly
  booking freight
- **Identity** — lookalike email domain, domain mismatch, free consumer inbox, callback
  number mismatch, VOIP line, mailbox-store or residential principal address
- **Network** — verified fraud reports, unconfirmed reports, and contact churn across
  multiple brokers

## Architecture

| File | Role |
|---|---|
| `risk_engine.py` | The scoring rules. **This file is the product**; everything else is plumbing. |
| `db.py` | Pooled Postgres connections and the migration runner. |
| `migrations/` | Versioned SQL. Applied in filename order, each in its own transaction. |
| `config.py` | Environment-driven config. Fails fast in production on missing secrets. |
| `auth.py` | Passwords, sessions, API keys, tenancy, plan metering. |
| `api.py` | The JSON API. |
| `queries.py` | Read queries shared across routes. |
| `app.py` | Flask routes, input validation, error handling. |
| `seed.py` | Development data — and the verdict regression test. |

Four decisions worth knowing before you change things:

**Transactions belong to the caller.** `run_check()` never commits; the `db.connection()`
block that wraps it does. That's what guarantees a failed check leaves no orphaned row
behind, and it lets the engine be tested inside a transaction that gets rolled back.

**Migrations take an advisory lock.** A rolling deploy starts several workers at once.
Without the lock they would each read the same empty `schema_migrations` and race to apply
the same DDL — one wins, the rest crash-loop.

**Check history is private; fraud reports are shared.** This is the central tenancy
decision and it cuts both ways deliberately. A brokerage's check history is a list of
exactly which carriers it is about to do business with — publishing that to competitors
would be worse than publishing nothing. But a fraud warning nobody can see protects
nobody. So `/network` shows network-wide aggregates and network-wide fraud reports, and
only *your* per-check feed. Reading a check by id is scoped by org, and an out-of-tenant
id returns 404 rather than 403 — telling a caller that a check exists but belongs to
someone else leaks the very thing being protected.

**API keys are stored hashed, never in a URL.** Only a SHA-256 is kept, so the table is
useless if it leaks. The plaintext key is shown once, carried to that page in the session
rather than a query string — a key in a URL ends up in browser history, proxy logs, and
the `Referer` header of the next request.

## Accounts and the API

Signup creates an organization and its first owner user. Every check, observation, and
fraud report is attributed to the org, and plan usage is metered per org:

| Plan | Checks / period |
|---|---|
| Starter | 250 |
| Pro | 1,500 |
| Enterprise | unmetered |

Exceeding the allowance returns `429 quota_exceeded` from the API and redirects to
settings in the UI.

The API is how the product actually gets used — a TMS fires a check at tender time,
because nobody remembers to open a website at 4:50pm on a Friday.

```bash
curl -X POST https://…/api/v1/checks \
  -H "Authorization: Bearer cg_live_…" \
  -H "Content-Type: application/json" \
  -d '{"mc_number": "MC-471902",
       "contact_email": "dispatch@greatlakesfreight-sys.com",
       "contact_phone": "872-555-0417",
       "load_value_usd": 78000}'
```

Returns the verdict, the risk score, the carrier record, and every signal that fired.
`GET /api/v1/checks/<id>` retrieves a past check (scoped to the key's org);
`GET /api/v1/usage` reports the current allowance.

## Deployment

The image migrates on boot, then serves under gunicorn:

```bash
docker build -t carrierguard .
docker run -p 8000:8000 \
  -e APP_ENV=production \
  -e SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
  -e DATABASE_URL="postgresql://..." \
  carrierguard
```

`render.yaml` provisions the web service and its Postgres together — connect the repo and
Render reads it. `SECRET_KEY` is generated by the platform, `DATABASE_URL` is injected,
and API credentials are marked `sync: false` so they are set in the dashboard and never
written back to git.

In production, a missing `SECRET_KEY` or `DATABASE_URL` is a **hard startup failure**, not
a silent default. A generated fallback would either log every user out on each deploy or,
worse, let anyone forge a session cookie.

`/healthz` performs a real database round trip and reports carrier-record staleness. A
fraud tool serving month-old authority data is worse than no tool, because the broker
trusts it — so a silent ingest failure surfaces here instead of becoming a wrong `CLEAR`.

Config reference: `.env.example`.

## What's stubbed, and what replaces it

| Prototype | Production |
|---|---|
| Seeded `carriers` table | Nightly FMCSA SAFER + Company Snapshot ingest |
| Seeded `phone_intel` table | Twilio Lookup — line type and provisioning age |
| `address_type` column | USPS CMRA database + address verification |
| Insurance columns | FMCSA L&I insurance filings, plus certificate parsing |
| `verified` flag set by hand | Ops review queue for incoming fraud reports |
| No billing | Stripe subscription against the existing per-org usage meter |

## Roadmap

- **API + TMS integration.** The check has to fire automatically at tender time inside
  McLeod, Turvo, or Aljex. Nobody will remember to visit a website at 4:50pm on a Friday.
- **Monitoring, not just checking.** Alert when a carrier you've *already* dispatched has
  its authority revoked or insurance lapse mid-haul.
- **Certificate-of-insurance parsing.** Upload the COI they emailed; verify it against the
  filing on record and flag alterations.
- **Watchlist sharing groups.** Private trust circles layered on the public network.
- **Feed FreightFlip.** The verification layer the broker side of `../freight-board`
  already needs — one carrier-trust database serving both products.

## Revenue

Per-seat subscription with a check allowance, because the value scales with tender volume:

| | Starter | Pro | Enterprise |
|---|---|---|---|
| Price | $199/mo | $499/mo | $1,499/mo |
| Checks | 250/mo | 1,500/mo | unlimited |
| | 1 seat | 10 seats | SSO, API, TMS integration |

The sales conversation is the easiest in software: **one prevented double-brokered load
pays for four years of Pro.**

## Before this takes public signups

Not engineering, and not optional. This publishes risk verdicts about named real companies
and lets subscribers accuse them of fraud. Get a lawyer on terms of service, a carrier
dispute/appeal process, and E&O coverage. The product is built to support that — every
verdict is itemized and cited, and `verified` is deliberately separate from `reported` —
but the paperwork is yours to get reviewed.
