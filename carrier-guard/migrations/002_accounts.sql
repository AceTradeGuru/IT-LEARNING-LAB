-- ============================================================
-- CarrierGuard 002 — accounts, organizations, and API keys
--
-- The prototype had a `brokers` table and a dropdown that let you pretend to
-- be any of them. This turns that into real multi-tenancy.
--
-- `brokers` is RENAMED rather than replaced. Renaming preserves the primary
-- keys and the identity sequence, so every existing check, observation, and
-- fraud report keeps pointing at the right subscriber. Recreating the table
-- and copying rows would have renumbered them and silently reattributed
-- historical fraud reports to the wrong company.
-- ============================================================

ALTER TABLE brokers RENAME TO organizations;
ALTER TABLE organizations RENAME COLUMN company_name  TO name;
ALTER TABLE organizations RENAME COLUMN contact_email TO billing_email;

ALTER TABLE organizations
    ADD COLUMN slug         TEXT,
    ADD COLUMN status       TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','past_due','canceled')),
    -- Usage metering. Reset when the billing period rolls over; the check
    -- endpoint refuses once an org is over its plan allowance.
    ADD COLUMN checks_used  INTEGER NOT NULL DEFAULT 0 CHECK (checks_used >= 0),
    ADD COLUMN period_start DATE NOT NULL DEFAULT CURRENT_DATE,
    -- Populated when billing is wired up; null means "not yet a paying account".
    ADD COLUMN stripe_customer_id     TEXT,
    ADD COLUMN stripe_subscription_id TEXT;

-- Backfill slugs from existing names, then enforce uniqueness.
UPDATE organizations
   SET slug = regexp_replace(lower(name), '[^a-z0-9]+', '-', 'g') || '-' || id::text
 WHERE slug IS NULL;

ALTER TABLE organizations
    ALTER COLUMN slug SET NOT NULL,
    ADD CONSTRAINT organizations_slug_key UNIQUE (slug);

-- The foreign keys keep working; only the column names change.
ALTER TABLE checks               RENAME COLUMN broker_id TO org_id;
ALTER TABLE contact_observations RENAME COLUMN broker_id TO org_id;
ALTER TABLE fraud_reports        RENAME COLUMN broker_id TO org_id;

ALTER INDEX idx_checks_broker RENAME TO idx_checks_org;


-- People who log in. A user belongs to exactly one organization: brokerages
-- don't share staff, and one-org-per-user keeps every query's tenant scope
-- unambiguous.
CREATE TABLE users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id        BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    full_name     TEXT,
    role          TEXT NOT NULL DEFAULT 'member'
                    CHECK (role IN ('owner','member')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

-- Case-insensitive uniqueness: nobody should be able to register
-- Ops@Example.com because ops@example.com is taken.
CREATE UNIQUE INDEX users_email_key ON users (lower(email));
CREATE INDEX idx_users_org ON users (org_id);


-- API keys, for firing checks from a TMS at tender time.
--
-- Only the hash is stored. If this table leaks, the keys in it are not usable —
-- which is the entire reason to hash them. key_prefix exists so the UI can show
-- "cg_live_a1b2c3d4…" without being able to reconstruct the secret.
CREATE TABLE api_keys (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id       BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    key_prefix   TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   BIGINT REFERENCES users(id) ON DELETE SET NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);

CREATE INDEX idx_api_keys_org ON api_keys (org_id) WHERE revoked_at IS NULL;
