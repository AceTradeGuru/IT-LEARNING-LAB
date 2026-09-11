# Deploying Royal Grind

What is done, what is stubbed, what is deliberately not wired up, and what needs
a lawyer before you dial anybody.

Read the last section before you set `TELEPHONY_PROVIDER=vonage`. That one
environment variable is the difference between a demo and a system that calls
real people at work.

---

## What is production-ready

- **Authentication and tenancy.** scrypt password hashing, session rotation on
  login, `HttpOnly`/`SameSite`/`Secure` cookies, CSRF on every POST. Every
  tenant-owned query filters on `org_id`; cross-tenant reads by id return 404 and
  the smoke test asserts it against a real second tenant.
- **Roles and row access.** `member` → `manager` → `admin` → `owner` gate routes;
  `visible_rep_ids()` narrows rows below that.
- **Config.** Everything from the environment. `config.validate()` refuses to
  boot a production process with no `SECRET_KEY`, with the Werkzeug debugger on,
  with insecure cookies, or with Vonage half-configured.
- **The compliance gate.** Suppression, quiet hours in the prospect's timezone,
  and recording-consent evaluation run before every dial, from one function, with
  no override parameter.
- **The scoring and learning engines.** Pure functions over rows. No network, no
  model, deterministic.
- **Spend control.** A per-hour call ceiling per org, and an authoritative
  per-month dollar cap in the `ai_spend` table computed from reported token
  usage, checked *before* the request.
- **The audit trail.** Every AI artifact, blocked call, erasure and suppression
  entry, with the engine that produced it.

## What is stubbed, and how to un-stub it

### The phone — `telephony.py`

Ships with `SimulatedProvider`, which places no calls and generates a realistic
outcome and transcript from the record's data quality, the contact's role and the
hour. It is what makes the product demonstrable with no account, and it is what
CI runs against.

`VonageProvider` is real code written against the documented Voice API —
RS256 JWT auth, an NCCO that optionally speaks the recording announcement before
connecting, two-channel recording so a transcript is attributable to a speaker,
and authenticated recording fetches. **It has never been run against a live
Vonage account**, because there is no account attached to this repository. Treat
your first real call as the test of it.

To switch it on:

1. Create a Vonage application with the Voice capability. Note the application id
   and download the private key.
2. Buy or assign a number and link it to the application.
3. Set `VONAGE_APPLICATION_ID`, `VONAGE_PRIVATE_KEY` (the whole PEM; `\n`
   escapes are handled), `VONAGE_NUMBER`, and `PUBLIC_BASE_URL` — the publicly
   reachable HTTPS origin Vonage will post events to.
4. Generate `TELEPHONY_WEBHOOK_SECRET`
   (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`). The app
   refuses to boot in production without it.
5. Set `TELEPHONY_PROVIDER=vonage`.
6. Point the application's event URL at `/webhook/call`. The app builds the full
   signed URL per call, so configure the answer/event URLs to whatever Vonage
   requires and let the per-call `event_url` carry the signature.

**The webhook is the only unauthenticated route.** It verifies an HMAC of the
call id, in constant time, before it writes anything. Do not remove that check
to make an integration work; it is the thing standing between a third party's
configuration screen and your transcript table.

Per-rep caller id lives on `reps.caller_id`. A local presence number connects
materially better than an 800 number, which is why it is per-rep rather than
per-org.

### The data layer — `enrichment.py`

Ships with `OfflineProvider`, which does not contact anything. It hashes the
value so verification is deterministic and a demo is reproducible, and stamps
every field it touches `offline simulation` — which the UI renders. A fake
verification presented as a real one is the single most damaging thing this
module could do.

`HttpProvider` is the shape a licensed vendor plugs into: `POST
{base}/verify/{phone|email}` with a bearer token. Real vendors differ; `_map()`
is the one function to change and nothing downstream cares, because every
consumer reads `status` and `confidence` rather than the vendor's name.

Before wiring one up, read its licence terms on **what you may store and for how
long**. Most B2B data contracts restrict retention and redistribution, and those
terms interact directly with `orgs.retention_days`.

There is no company/contact *discovery* provider. Companies arrive by seed, by
`/signup` plus manual entry, or by whatever import you build. A CSV importer is
the obvious next module and is not written.

### The model — `ai_engine.py`

Works as shipped with `ANTHROPIC_API_KEY`, or an `ant auth login` profile. Without
credentials the offline engine runs and says so everywhere.

## What is deliberately not wired up

- **Email sending.** Drafts are created; "Send" marks them sent and writes the
  timeline entry. Nothing is handed to an SMTP server or an ESP. This is a
  decision, not an omission: outbound email at volume brings CAN-SPAM and
  GDPR obligations, domain authentication (SPF/DKIM/DMARC), warm-up, bounce and
  complaint handling, and an unsubscribe mechanism that has to actually work.
  Wiring a transport without those is how a company's domain gets blocked in a
  week. When you add one, route it through `compliance.is_suppressed` — the
  route already does for the "sent" transition.
- **Call recording storage.** `calls.recording_url` holds the provider's URL.
  Recordings are not copied into your own storage, transcoded, or lifecycled. If
  you record, you need a retention policy for the audio, and it needs to match
  what you told people on the announcement.
- **Transcription.** The simulator produces transcripts. With Vonage you get an
  audio recording; a speech-to-text step has to sit between the recording webhook
  and `analyze_call()`. Two-channel recording is already requested so speakers
  stay separable, which is the part that is annoying to retrofit.
- **Password reset and email invites.** An owner sets a password directly on
  `/settings/users`. There is no token flow.
- **A background worker.** `sequences.py --run` and `compliance.py --purge` are
  cron entry points. Run them from Render Cron Jobs or equivalent, daily.
- **Rate limiting across workers.** The per-hour AI ceiling is per-process, so
  with four gunicorn workers the real ceiling is 4x. The per-month dollar cap is
  in the database and is authoritative. Move the hourly limiter to Redis before
  it matters.

## Database

SQLite locally; Postgres in production via `DATABASE_URL`. `db.py` translates the
three constructs this codebase uses — placeholders, `lastrowid`, and
`INSERT OR IGNORE` — and derives the Postgres DDL from the same `schema.sql`
rather than maintaining a second copy that silently drifts.

**The Postgres path has not been run against a live server.** On first deploy,
open a shell and run:

```bash
python3 db.py --check                                  # proves the PG path works
python3 -c "from grind_db import create_tables; create_tables()"
```

Do **not** run `python3 grind_db.py` in production. It seeds the demo brokerage,
with a published password, into your database. Real tenants come from `/signup`.

Royal Grind gets its own Postgres instance. QuotaOS and CarrierGuard in this same
repository also define a `users` table; pointing two apps at one instance would
have them fighting over it.

## Before you dial anybody

This section is not legal advice and does not make anyone compliant. It is the
list of things to put in front of someone who can give legal advice.

1. **Telemarketing law.** US outbound B2B calling touches the TCPA, state
   mini-TCPAs (Florida and Oklahoma are notably stricter), and the national and
   state do-not-call registries. The platform enforces a calling window and a
   suppression list; **it does not scrub against the national DNC registry**, and
   it cannot tell a business line from a personal wireless number. Both of those
   are subscriptions and integrations you have to add. `suppression.source`
   already has a `dnc_registry` value for exactly this.
2. **Recording consent.** `ALL_PARTY_STATES` in `compliance.py` is compiled from
   the commonly cited list. Jurisdictions amend these statutes and courts read
   them differently — have counsel review that constant, and review the
   announcement text in the same file. The default recording policy is **off**,
   and it should stay off until somebody has signed off on it.
3. **Data protection.** If you hold data on people in the EU or UK, you need a
   lawful basis (legitimate interest, in practice, with the balancing test
   documented), an Article 14 notice to people whose data you did not collect
   from them, and an Article 30 record. The platform gives you subject access,
   erasure, and a retention clock; it does not give you a lawful basis. California
   (CCPA/CPRA) and the other US state privacy laws have their own notice and
   opt-out mechanics.
4. **AI disclosure.** Call analysis and generated call scripts are increasingly
   in scope for AI transparency rules. The audit log records every artifact and
   the engine that produced it, which is the evidence such a rule asks for; the
   disclosure itself is a policy decision.
5. **Your data vendor's contract.** Retention limits, permitted use, and
   redistribution terms. These are the terms most often breached by accident,
   usually by a backup nobody thought about.

## Deploying to Render

`render.yaml` is a blueprint. New → Blueprint, point it at this repo. It creates
the web service and the Postgres instance and wires `DATABASE_URL`.

Set by hand after the first deploy (they are `sync: false` so they are never read
from the repo): `SECRET_KEY`, `ANTHROPIC_API_KEY`, `TELEPHONY_WEBHOOK_SECRET`,
`PUBLIC_BASE_URL`, and the three `VONAGE_*` values.

`TELEPHONY_PROVIDER` is pinned to `simulated` in the blueprint on purpose. Change
it in the dashboard, deliberately, after reading the section above.

Health check is `/healthz` — no auth, no tenant data, one cheap query.

Add two cron jobs:

```
daily 06:00   python3 sequences.py --run
weekly        python3 compliance.py --purge --apply
```

## Running the tests

```bash
python3 smoke_test.py
```

160 checks against a scratch database: every route, every role boundary, the
whole call loop from dial to logged debrief, the learning loop's clamps, the data
layer's decay and dial feedback, cadence entry and exit, tenant isolation, webhook
signing, CSRF, subject access and erasure, and the production config guard.

It needs no API key and places no calls. CI runs it on every push.
