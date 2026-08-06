-- ============================================================
-- CarrierGuard 001 — initial schema (PostgreSQL)
--
--   carriers             -> what the government record says (FMCSA snapshot)
--   checks               -> what the carrier claimed when they contacted YOU
--   check_signals        -> every risk signal that fired, and what it cost
--   contact_observations -> the network's memory of who contacted whom
--   fraud_reports        -> what other brokers learned the hard way
--
-- Fraud almost never shows up inside one record. It shows up as a
-- DISAGREEMENT between the government record, the inbound contact,
-- and the network's memory. That disagreement is the product.
--
-- Ported from the SQLite prototype. Dates are real DATE/TIMESTAMPTZ
-- columns rather than ISO text, so date arithmetic happens in the
-- database instead of being re-parsed in Python on every check.
-- ============================================================

-- The FMCSA/enrichment snapshot for a motor carrier, refreshed nightly
-- by the ingest job.
CREATE TABLE carriers (
    mc_number             TEXT PRIMARY KEY,
    dot_number            TEXT,
    legal_name            TEXT NOT NULL,
    dba_name              TEXT,

    -- Contact of record. Everything inbound gets compared against this.
    registered_domain     TEXT,
    registered_email      TEXT,
    registered_phone      TEXT,               -- digits only, e.g. '3135550188'
    registered_address    TEXT,
    address_type          TEXT NOT NULL DEFAULT 'commercial'
                            CHECK (address_type IN
                              ('commercial','residential','cmra','unknown')),
    registered_phone_type TEXT NOT NULL DEFAULT 'unknown'
                            CHECK (registered_phone_type IN
                              ('landline','mobile','voip','unknown')),

    -- Operating authority.
    authority_status      TEXT NOT NULL DEFAULT 'active'
                            CHECK (authority_status IN
                              ('active','revoked','pending','inactive')),
    authority_granted     DATE,
    reinstatement_count   INTEGER NOT NULL DEFAULT 0
                            CHECK (reinstatement_count >= 0),

    -- Insurance.
    liability_expires     DATE,
    cargo_expires         DATE,
    cargo_coverage_usd    INTEGER NOT NULL DEFAULT 100000
                            CHECK (cargo_coverage_usd >= 0),

    -- Safety / activity.
    power_units           INTEGER NOT NULL DEFAULT 1 CHECK (power_units >= 0),
    oos_rate              NUMERIC(4,3) NOT NULL DEFAULT 0
                            CHECK (oos_rate >= 0 AND oos_rate <= 1),
    last_inspection       DATE,

    -- Ingest provenance: which run last touched this row, so the app can
    -- surface "this record is 9 days stale" instead of quietly serving it.
    source                TEXT NOT NULL DEFAULT 'seed',
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Brokers and shippers who subscribe to CarrierGuard.
-- Superseded by organizations in a later migration; kept minimal here so the
-- data layer port stays reviewable on its own.
CREATE TABLE brokers (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_name  TEXT NOT NULL,
    mc_number     TEXT,
    contact_email TEXT NOT NULL UNIQUE,
    plan          TEXT NOT NULL DEFAULT 'starter'
                    CHECK (plan IN ('starter','pro','enterprise')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row every time a broker vets a carrier before tendering.
CREATE TABLE checks (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    broker_id      BIGINT NOT NULL REFERENCES brokers(id),
    mc_number      TEXT NOT NULL,          -- as typed; may not exist in carriers

    -- What the person on the other end of the phone/email actually used.
    contact_name   TEXT,
    contact_email  TEXT,
    contact_phone  TEXT,                   -- digits only
    load_value_usd INTEGER CHECK (load_value_usd IS NULL OR load_value_usd >= 0),

    risk_score     INTEGER NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
    verdict        TEXT NOT NULL CHECK (verdict IN ('clear','review','block')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The explanation. A verdict a broker can't defend to their boss is worthless,
-- so every point of risk score is itemized and cited back to its source.
CREATE TABLE check_signals (
    id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    check_id BIGINT NOT NULL REFERENCES checks(id) ON DELETE CASCADE,
    code     TEXT NOT NULL,
    label    TEXT NOT NULL,
    detail   TEXT,
    weight   INTEGER NOT NULL,             -- negative = mitigating
    severity TEXT NOT NULL
               CHECK (severity IN ('critical','high','medium','low','good')),
    source   TEXT NOT NULL DEFAULT 'fmcsa'
               CHECK (source IN ('fmcsa','identity','insurance','safety','network'))
);

-- The network's memory: which contact details have been used to represent
-- which MC number, to which broker, and how often.
-- A legitimate carrier's dispatch contact is boring and stable.
-- A hijacked MC number sprouts new contacts across multiple brokers at once.
--
-- contact_email/phone are NOT NULL DEFAULT '' rather than nullable: SQL treats
-- NULLs as distinct in unique constraints, which would let the same blank
-- contact insert unboundedly instead of incrementing times_seen.
CREATE TABLE contact_observations (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mc_number     TEXT NOT NULL,
    contact_email TEXT NOT NULL DEFAULT '',
    contact_phone TEXT NOT NULL DEFAULT '',
    broker_id     BIGINT NOT NULL REFERENCES brokers(id),
    times_seen    INTEGER NOT NULL DEFAULT 1 CHECK (times_seen > 0),
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (mc_number, contact_email, contact_phone, broker_id)
);

-- What other brokers learned the hard way. This is the moat: it is the only
-- data in the system that cannot be bought from a vendor.
CREATE TABLE fraud_reports (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    broker_id       BIGINT NOT NULL REFERENCES brokers(id),
    mc_number       TEXT NOT NULL,
    reported_email  TEXT,
    reported_phone  TEXT,
    incident_type   TEXT NOT NULL CHECK (incident_type IN
                      ('double_brokered','identity_theft','payment_redirect',
                       'cargo_theft','no_show','fictitious_pickup')),
    amount_lost_usd INTEGER NOT NULL DEFAULT 0 CHECK (amount_lost_usd >= 0),
    notes           TEXT,
    verified        BOOLEAN NOT NULL DEFAULT FALSE,  -- confirmed by ops review
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cached phone-line intelligence (Twilio Lookup / Ekata cache).
-- VOIP and freshly-provisioned numbers are the fraudster's default tool
-- because they are disposable.
CREATE TABLE phone_intel (
    phone        TEXT PRIMARY KEY,          -- digits only
    line_type    TEXT NOT NULL
                   CHECK (line_type IN ('landline','mobile','voip','unknown')),
    carrier_name TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_checks_mc     ON checks (mc_number, created_at DESC);
CREATE INDEX idx_checks_broker ON checks (broker_id, created_at DESC);
CREATE INDEX idx_signals_check ON check_signals (check_id);
CREATE INDEX idx_reports_mc    ON fraud_reports (mc_number, verified);
CREATE INDEX idx_obs_mc        ON contact_observations (mc_number, last_seen DESC);
