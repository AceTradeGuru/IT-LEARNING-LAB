-- ============================================================
-- CarrierGuard: the carrier vetting & fraud firewall
-- Run the carrier before you tender the load.
--
-- Core idea of the schema:
--   carriers             -> what the government record says (FMCSA snapshot)
--   checks               -> what the carrier claimed when they contacted YOU
--   check_signals        -> every risk signal that fired, and what it cost
--   contact_observations -> the network's memory of who contacted whom
--   fraud_reports        -> what other brokers learned the hard way
--
-- Fraud almost never shows up inside one record. It shows up as a
-- DISAGREEMENT between the government record, the inbound contact,
-- and the network's memory. That disagreement is the product.
-- ============================================================

-- The FMCSA/enrichment snapshot for a motor carrier.
-- In production this is refreshed nightly from SAFER + insurance +
-- phone/address intelligence providers. Here it is seeded.
CREATE TABLE IF NOT EXISTS carriers (
    mc_number           TEXT PRIMARY KEY,           -- 'MC-284561'
    dot_number          TEXT,
    legal_name          TEXT NOT NULL,
    dba_name            TEXT,

    -- Contact of record. Everything inbound gets compared against this.
    registered_domain   TEXT,                       -- 'redlinecarriers.com'
    registered_email    TEXT,
    registered_phone    TEXT,                       -- digits only, e.g. '3135550188'
    registered_address  TEXT,
    address_type        TEXT DEFAULT 'commercial'
                          CHECK (address_type IN ('commercial','residential','cmra','unknown')),
    registered_phone_type TEXT DEFAULT 'landline'
                          CHECK (registered_phone_type IN ('landline','mobile','voip','unknown')),

    -- Operating authority.
    authority_status    TEXT NOT NULL DEFAULT 'active'
                          CHECK (authority_status IN ('active','revoked','pending','inactive')),
    authority_granted   TEXT,                       -- ISO date
    reinstatement_count INTEGER NOT NULL DEFAULT 0, -- revoked-then-reinstated churn

    -- Insurance.
    liability_expires   TEXT,                       -- ISO date
    cargo_expires       TEXT,                       -- ISO date
    cargo_coverage_usd  INTEGER DEFAULT 100000,

    -- Safety / activity.
    power_units         INTEGER DEFAULT 1,
    oos_rate            REAL DEFAULT 0.0,           -- vehicle out-of-service rate, 0.0-1.0
    last_inspection     TEXT,                       -- ISO date; NULL/old = dormant shell
    updated_at          TEXT DEFAULT (datetime('now'))
);

-- Brokers and shippers who subscribe to CarrierGuard.
CREATE TABLE IF NOT EXISTS brokers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name    TEXT NOT NULL,
    mc_number       TEXT,
    contact_email   TEXT NOT NULL UNIQUE,
    plan            TEXT NOT NULL DEFAULT 'starter'
                      CHECK (plan IN ('starter','pro','enterprise')),
    created_at      TEXT DEFAULT (datetime('now'))
);

-- One row every time a broker vets a carrier before tendering.
CREATE TABLE IF NOT EXISTS checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_id       INTEGER NOT NULL REFERENCES brokers(id),
    mc_number       TEXT NOT NULL,          -- as typed; may not exist in carriers

    -- What the person on the other end of the phone/email actually used.
    contact_name    TEXT,
    contact_email   TEXT,
    contact_phone   TEXT,                   -- digits only
    load_value_usd  INTEGER,                -- cargo value at risk

    risk_score      INTEGER NOT NULL,       -- 0-100
    verdict         TEXT NOT NULL CHECK (verdict IN ('clear','review','block')),
    created_at      TEXT DEFAULT (datetime('now'))
);

-- The explanation. A verdict a broker can't defend to their boss is worthless,
-- so every point of risk score is itemized and cited back to its source.
CREATE TABLE IF NOT EXISTS check_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id    INTEGER NOT NULL REFERENCES checks(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,              -- 'LOOKALIKE_DOMAIN'
    label       TEXT NOT NULL,              -- human-readable headline
    detail      TEXT,                       -- the specific evidence
    weight      INTEGER NOT NULL,           -- points added (negative = mitigating)
    severity    TEXT NOT NULL
                  CHECK (severity IN ('critical','high','medium','low','good')),
    source      TEXT NOT NULL DEFAULT 'fmcsa'
                  CHECK (source IN ('fmcsa','identity','insurance','safety','network'))
);

-- The network's memory: which contact details have been used to represent
-- which MC number, to which broker, and how often.
-- A legitimate carrier's dispatch contact is boring and stable.
-- A hijacked MC number sprouts new contacts across multiple brokers at once.
CREATE TABLE IF NOT EXISTS contact_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mc_number       TEXT NOT NULL,
    contact_email   TEXT,
    contact_phone   TEXT,
    broker_id       INTEGER NOT NULL REFERENCES brokers(id),
    times_seen      INTEGER NOT NULL DEFAULT 1,
    first_seen      TEXT DEFAULT (datetime('now')),
    last_seen       TEXT DEFAULT (datetime('now')),
    UNIQUE (mc_number, contact_email, contact_phone, broker_id)
);

-- What other brokers learned the hard way. This is the moat: it is the only
-- data in the system that cannot be bought from a vendor.
CREATE TABLE IF NOT EXISTS fraud_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_id       INTEGER NOT NULL REFERENCES brokers(id),
    mc_number       TEXT NOT NULL,
    reported_email  TEXT,
    reported_phone  TEXT,
    incident_type   TEXT NOT NULL CHECK (incident_type IN
                      ('double_brokered','identity_theft','payment_redirect',
                       'cargo_theft','no_show','fictitious_pickup')),
    amount_lost_usd INTEGER DEFAULT 0,
    notes           TEXT,
    verified        INTEGER NOT NULL DEFAULT 0,   -- 0/1, confirmed by CarrierGuard ops
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Cached phone-line intelligence (stub for a Twilio Lookup / Ekata cache).
-- VOIP and freshly-provisioned numbers are the fraudster's default tool
-- because they are disposable.
CREATE TABLE IF NOT EXISTS phone_intel (
    phone       TEXT PRIMARY KEY,           -- digits only
    line_type   TEXT NOT NULL
                  CHECK (line_type IN ('landline','mobile','voip','unknown')),
    carrier_name TEXT
);

CREATE INDEX IF NOT EXISTS idx_checks_mc      ON checks(mc_number, created_at);
CREATE INDEX IF NOT EXISTS idx_checks_broker  ON checks(broker_id, created_at);
CREATE INDEX IF NOT EXISTS idx_signals_check  ON check_signals(check_id);
CREATE INDEX IF NOT EXISTS idx_reports_mc     ON fraud_reports(mc_number, verified);
CREATE INDEX IF NOT EXISTS idx_obs_mc         ON contact_observations(mc_number, last_seen);
