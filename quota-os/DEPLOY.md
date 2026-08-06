# Deploying QuotaOS

Status: **the blocking security work is done.** Authentication, role-based access,
multi-tenant isolation, CSRF, secure cookies, env-based config, and an AI decision
log are all in and tested. What remains before real customer data is listed under
[Still required](#still-required).

---

## What you need to provide

| # | Thing | Status | Cost |
|---|---|---|---|
| 1 | A host | **Decided: Render.** `render.yaml` is in this directory — deploy via New → Blueprint. | ~$7/mo |
| 2 | Managed Postgres | **Done in code.** `render.yaml` provisions it and wires `DATABASE_URL`. | ~$7/mo |
| 3 | A domain + TLS | Yours to buy. Render issues the certificate. | ~$15/yr |
| 4 | `SECRET_KEY` | **You must set this in the Render dashboard.** `python3 -c "import secrets; print(secrets.token_hex(32))"` | free |
| 5 | `ANTHROPIC_API_KEY` | **You must set this in the Render dashboard.** Without it the offline engine runs. | usage |
| 6 | Monthly AI ceiling | **Decided: $90/org/month.** Enforced from real token usage, blocks before the call. | — |
| 7 | A tested backup restore | Render gives you snapshots. Doing the restore drill once is on you. | included |
| 8 | **Legal review of the AI hiring features** | Still open. The item most likely to actually block launch — see below. | varies |
| 9 | CRM strategy | **Decided: replace Salesforce/HubSpot.** That is a much larger build than the rest of this list — see [Becoming the system of record](#becoming-the-system-of-record). | — |

### Generate the secret key

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### Environment

Copy `.env.example` and fill it in. The app **refuses to boot** in production if
`SECRET_KEY` is missing, debug is on, or secure cookies are off — a misconfigured
deploy fails loudly instead of quietly serving an insecure app.

```bash
QUOTAOS_ENV=production
QUOTAOS_DEBUG=0
SECRET_KEY=<from the command above>
SESSION_COOKIE_SECURE=1
ANTHROPIC_API_KEY=<optional; offline engine runs without it>
```

### Run

```bash
pip install -r requirements.txt
gunicorn --workers 4 --bind 0.0.0.0:$PORT app:app     # or just use the Procfile
```

**Do not run `quota_db.py` in production.** It seeds the Northwind demo tenant and
five demo logins with a published password. Real tenants come from `/signup`.

---

## What is already done

**Authentication and access control**
- Session login, scrypt-hashed passwords, 12-character minimum, session rotated on
  login to prevent fixation
- Four roles — `member` → `manager` → `admin` → `owner` — enforced by decorator on
  every route, and by row filter below that
- The demo "Viewing as" impersonation switcher is deleted

**Multi-tenancy**
- `org_id` on every tenant-owned table, including child tables where a join would
  make it redundant — so one forgotten filter leaks nothing
- Every query in `app.py` filters on `org_id`; `auth.scoped_one()` is the only way
  a route loads a record by id
- Verified: a second tenant signing up gets **404 on every one of the first
  tenant's record ids** and sees zero rows anywhere

**Verified access matrix**

| | `/` | `/org` | `/pipeline` | `/training` | `/hiring` | `/settings/users` | `/settings/audit` |
|---|---|---|---|---|---|---|---|
| anonymous | → login | → login | → login | → login | → login | → login | → login |
| member | 200 | 200 (self only) | 200 | 200 | 403 | 403 | 403 |
| manager | 200 | 200 (own team) | 200 | 200 | 200 | 403 | 403 |
| admin | 200 | 200 (whole org) | 200 | 200 | 200 | 403 | 200 |
| owner | 200 | 200 | 200 | 200 | 200 | 200 | 200 |

A member reading another rep's page gets 403; a manager reading an out-of-team rep
gets 403.

**Hardening**
- CSRF token on all 16 POST forms, validated constant-time; a POST without one is
  rejected with 400
- `HttpOnly` + `SameSite=Lax` cookies, `Secure` in production, 12-hour lifetime
- Request body capped, debug off, gunicorn instead of the dev server
- Config entirely from environment; no secret in the repo
- Login says "email or password is incorrect" for both cases, so it can't be used
  to enumerate accounts
- Post-login redirects are restricted to same-site paths (no open redirect)
- Per-org hourly rate limit on the four AI endpoints, and a cap on how many resumes
  one search sends to the model

**Compliance groundwork**
- `audit_log` records every AI generation and score — action, entity, engine, model
  id, the full per-question output, the acting user, IP, and timestamp
- Visible to admins at `/settings/audit`
- `candidates.purge_after` column exists for a retention job

---

## Still required

### Before real customer data
1. **Verify the Postgres path on the first deploy.** The code is written (`db.py`
   translates placeholders, `RETURNING id`, and `INSERT OR IGNORE` automatically)
   but there is no Postgres on the development machine, so **it has never run
   against a live server.** On the first Render deploy, open a shell and run
   `python3 db.py --check` — it inserts, reads back an id, and deletes. Do that
   before you trust it with anything.
2. **Rate limiter in Redis.** The hourly call counter is in-process, so with 4
   workers the effective ceiling is 4× the configured number. The *dollar* cap is
   already correct across workers — it reads from the `ai_spend` table.
3. **Error monitoring** (Sentry or equivalent) and a staging environment.
4. **Backup restore drill.** Once, before you need it.
5. **Connection pooling.** Every request currently opens and closes a connection.
   Fine on SQLite, wasteful on Postgres — use `psycopg_pool` or Render's pgbouncer.

### Before real candidates

I am not a lawyer, and you should get one for this. QuotaOS generates scores and
advance/hold/reject recommendations on job applicants, which puts it squarely
inside a body of regulation aimed at automated employment decision tools:

- **NYC Local Law 144** — annual independent bias audit, published summary of
  results, and advance notice to candidates, if you hire for NYC roles.
- **Illinois AI Video Interview Act**, **Colorado AI Act**, and the **EU AI Act**,
  which classifies employment screening as high-risk.
- Candidate resumes are personal data under **GDPR/CCPA** — retention limits,
  deletion on request, and disclosure of automated processing.

The product is designed to help here rather than hinder: the rubric is written
before any answer exists and is shown to candidates, every score quotes the
evidence that produced it, a human moves every stage, and the decision log is
built in. Those are defensible choices. They are not a legal opinion.

### Before selling to other companies
- Billing and plan enforcement (`orgs.plan` and `orgs.ai_cents_cap` are stubs)
- Password reset and email verification — there is no mail transport yet
- SSO/SAML, which enterprise buyers will ask for
- SOC 2, which they will also ask for

---

## Rough timeline

| Phase | Work | Effort |
|---|---|---|
| ~~Blocking security~~ | ~~auth, tenancy, CSRF, config, audit log~~ | **done** |
| Production infrastructure | ~~Postgres~~ (written, unverified), Redis, pooling, monitoring, staging | ~3 days |
| Sellable | billing, password reset, email, onboarding flow | ~2 weeks |
| Enterprise-ready | SSO, SOC 2 evidence, DPA, bias audit | months, mostly not code |

---

## Becoming the system of record

The goal is to replace Salesforce/HubSpot rather than sync from them. That is a
strategy decision, not a configuration one, and it changes the roadmap more than
anything else on this page.

**What exists today.** QuotaOS is a *management layer*. It reads deal data and
interprets it — health scoring, coverage, slippage, hygiene, the training join, the
hiring loop. All of that is genuinely differentiated; neither Salesforce nor HubSpot
does it.

**What does not exist.** There is no way to create a deal. No route, no form. Every
deal in the database was written by the seed script. Specifically missing:

| Gap | Why it blocks "system of record" |
|---|---|
| Deal create / edit / delete | A rep cannot record a deal at all today |
| Accounts and contacts as tables | `deals.account` is a text column; there is no company or person entity |
| Activity logging | `activities` holds weekly rollups only, and nothing writes them |
| Notes, tasks, attachments | The daily surface a rep actually lives in |
| Email and calendar capture | The reason reps tolerate a CRM at all |
| Salesforce / HubSpot import | Without it nobody can switch to you |
| Custom fields | The first question every CRM buyer asks |
| Reporting and forecast rollups | What the VP signs the contract for |

Realistically that is 3–6 months of focused work before it is a credible CRM
replacement, and the CRM half is table stakes — it wins no deals on its own,
because Salesforce already does it.

**The sequencing that actually gets you their customers.**

The differentiated part is already built. The commodity part is not. Leading with
the commodity part means competing head-on with a twenty-year-old product on its
strongest ground, with none of your advantage visible.

1. **Land as a layer.** Import from Salesforce/HubSpot (CSV first, API later) and
   sell the health engine, certification compliance, and the hiring loop — the
   things they cannot get from their CRM. Low switching cost, so the deal is easy.
2. **Own the daily workflow.** Add deal editing, notes, and activity capture *in
   QuotaOS*. Once reps update deals here rather than in Salesforce, you are the
   system of record in practice regardless of which one is nominally authoritative.
3. **Then displace.** The Salesforce renewal becomes a line item nobody is
   defending, because the work already moved.

That is the path Gong, Clari, and People.ai all took into accounts Salesforce
owned. Step 1 needs the import path, which is the highest-leverage thing to build
next and is worth doing even if you go straight at full replacement — nobody can
switch to you without it.

**If you want the direct route anyway,** the order is: accounts and contacts →
deal CRUD → activity logging → import → custom fields → forecasting. Say the word
and I'll start on accounts and contacts, which everything else hangs off.
