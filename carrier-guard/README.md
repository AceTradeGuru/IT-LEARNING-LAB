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

## Run the demo

```bash
python3 app.py
```

Open http://localhost:5002

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
seen that pattern. Check the [Network](http://localhost:5002/network) page to see the four
checks that produced it.

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

One broker's bad afternoon protects every other subscriber within a second. That is the
whole business in one click.

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

## Stack

- Python 3 / Flask, no dependencies beyond Flask
- SQLite (`schema.sql` — carriers, brokers, checks, check_signals, contact_observations,
  fraud_reports, phone_intel)
- `risk_engine.py` — the scoring rules. This file is the product; everything else is
  plumbing around it.
- `guard_db.py` — schema setup and the demo seed. Seven carriers covering every way a
  carrier goes bad, plus twelve historical checks generated by *actually running the
  engine* so the network memory is real rather than faked.

```bash
python3 guard_db.py     # rebuild + print every carrier's verdict from the CLI
```

Delete `carrierguard.db` to reset the demo to fresh seed data. All seed dates are relative
to today, so the demo never goes stale.

## What's stubbed, and what replaces it

| Prototype | Production |
|---|---|
| `carriers` table, hand-seeded | Nightly FMCSA SAFER + Company Snapshot pull |
| `phone_intel` table, hand-seeded | Twilio Lookup / Ekata — line type and provisioning age |
| `address_type` column | USPS CMRA database + address verification |
| Insurance columns | FMCSA L&I insurance filings, plus direct certificate parsing |
| Broker selector in the header | Real accounts, SSO, API keys |
| `verified` flag set by hand | Ops review queue for incoming fraud reports |

## Roadmap

- **API + TMS integration.** The check has to fire automatically at tender time inside
  McLeod, Turvo, or Aljex. Nobody will remember to visit a website at 4:50pm on a Friday.
- **Monitoring, not just checking.** Alert when a carrier you've *already* dispatched has
  its authority revoked or insurance lapse mid-haul.
- **Certificate-of-insurance parsing.** Upload the COI they emailed; verify it against the
  filing on record and flag alterations.
- **Watchlist sharing groups.** Let brokers form private trust circles on top of the
  public network.
- **Feed FreightFlip.** The verification layer the broker side of `../freight-board`
  already needs — same company, one carrier-trust database serving both products.

## Revenue

Per-seat subscription with a check allowance, because the value scales with tender volume:

| | Starter | Pro | Enterprise |
|---|---|---|---|
| Price | $199/mo | $499/mo | $1,499/mo |
| Checks | 250/mo | 1,500/mo | unlimited |
| | 1 seat | 10 seats | SSO, API, TMS integration |

The sales conversation is the easiest in software: **one prevented double-brokered load
pays for four years of Pro.** The confirmed losses on the seeded network alone total
$60,600 across two incidents.
