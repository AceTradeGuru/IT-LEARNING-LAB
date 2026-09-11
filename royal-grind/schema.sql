-- Royal Grind — AI sales intelligence and execution platform.
--
-- Nineteen tables across five surfaces: the tenant, the data layer, the
-- execution layer (calls, tasks, sequences), the pipeline, and the learning
-- loop that makes the first four worth more next quarter than this one.
--
-- Two shapes in here are unusual for a CRM and are the point of the product:
--
--   * `contact_points` is a table, not three columns on `contacts`. A phone
--     number is not a string — it is a claim, with a source, a verification
--     method, a date it was last checked, and a confidence that decays. A
--     platform that stores it as a bare string cannot tell a rep whether to
--     trust it, and "the number was wrong" is the single fastest way to lose
--     a sales team.
--
--   * `signal_outcomes` accumulates what actually happened after each kind of
--     buying signal. It starts empty and is worthless on day one. After a
--     quarter of calls it is the only asset here a competitor cannot license.

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS signal_outcomes;
DROP TABLE IF EXISTS emails;
DROP TABLE IF EXISTS enrollments;
DROP TABLE IF EXISTS sequence_steps;
DROP TABLE IF EXISTS sequences;
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS activities;
DROP TABLE IF EXISTS call_events;
DROP TABLE IF EXISTS calls;
DROP TABLE IF EXISTS deals;
DROP TABLE IF EXISTS targets;
DROP TABLE IF EXISTS triggers;
DROP TABLE IF EXISTS contact_points;
DROP TABLE IF EXISTS contacts;
DROP TABLE IF EXISTS companies;
DROP TABLE IF EXISTS suppression;
DROP TABLE IF EXISTS imports;
DROP TABLE IF EXISTS saved_searches;
DROP TABLE IF EXISTS ai_spend;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS reps;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS orgs;

-- ========================================================== the tenant ==

CREATE TABLE orgs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    slug                TEXT NOT NULL UNIQUE,
    plan                TEXT NOT NULL DEFAULT 'trial',
    status              TEXT NOT NULL DEFAULT 'active',

    -- What this org sells, in one line. Feeds every AI prompt so the call plan
    -- pitches the customer's actual service rather than a generic one.
    pitch               TEXT DEFAULT '',
    -- Freight modes they can actually cover. A signal that predicts flatbed
    -- volume is worth nothing to a broker with no flatbed capacity.
    modes               TEXT DEFAULT 'ltl,ftl',
    home_markets        TEXT DEFAULT '',

    -- Monthly ceiling on model spend, in US cents. Authoritative, shared across
    -- workers, checked before the call rather than reported after it.
    ai_cents_cap        INTEGER NOT NULL DEFAULT 9000,

    -- Call recording posture. 'off' never records. 'one_party' records where one
    -- party's consent suffices. 'all_party' records only after both parties are
    -- on the line having heard the announcement. See compliance.py.
    recording_policy    TEXT NOT NULL DEFAULT 'off',
    -- Local-time window in which dialing is permitted, in the PROSPECT's zone.
    quiet_start         INTEGER NOT NULL DEFAULT 8,
    quiet_end           INTEGER NOT NULL DEFAULT 21,

    retention_days      INTEGER NOT NULL DEFAULT 730,
    created_at          TEXT NOT NULL
);

CREATE TABLE users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    email           TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    name            TEXT NOT NULL,
    -- member -> manager -> admin -> owner. See auth.ROLES.
    role            TEXT NOT NULL DEFAULT 'member',
    status          TEXT NOT NULL DEFAULT 'active',
    rep_id          INTEGER,
    last_login_at   TEXT,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_users_email ON users (org_id, email);

CREATE TABLE reps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    email           TEXT,
    manager_id      INTEGER,
    territory       TEXT DEFAULT '',
    -- The number on the seat, per quarter, in dollars of gross margin or revenue
    -- depending on how the org counts. Used for coverage math on /exec.
    quota_quarter   REAL NOT NULL DEFAULT 0,
    -- Caller ID the phone platform presents. A local presence number connects
    -- materially better than an 800 number, which is why it is per-rep.
    caller_id       TEXT DEFAULT '',
    daily_call_goal INTEGER NOT NULL DEFAULT 40,
    status          TEXT NOT NULL DEFAULT 'active',
    started_at      TEXT,
    created_at      TEXT NOT NULL
);

-- ======================================================= the data layer ==

CREATE TABLE companies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    domain          TEXT DEFAULT '',
    industry        TEXT DEFAULT '',
    naics           TEXT DEFAULT '',
    city            TEXT DEFAULT '',
    state           TEXT DEFAULT '',
    timezone        TEXT DEFAULT 'America/Chicago',
    employees       INTEGER,
    revenue_est     REAL,

    -- The freight profile. This is the vertical knowledge a general-purpose
    -- contact database does not hold, and the reason a broker would switch.
    modes           TEXT DEFAULT '',      -- ltl,ftl,flatbed,reefer,expedited,intermodal
    loads_per_month INTEGER,
    dock_count      INTEGER,
    lanes           TEXT DEFAULT '',      -- "Kansas City, MO -> Wichita, KS" per line
    incumbents      TEXT DEFAULT '',      -- brokerages they use today, if known
    shipper_type    TEXT DEFAULT '',      -- manufacturer / distributor / 3pl / retailer

    -- Provenance, carried on the row rather than in a side table because every
    -- consumer of a company needs it. See enrichment.py for the vocabulary.
    source          TEXT NOT NULL DEFAULT 'manual',
    confidence      INTEGER NOT NULL DEFAULT 50,
    last_verified   TEXT,
    external_id     TEXT DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_companies_org ON companies (org_id, state, industry);
CREATE UNIQUE INDEX idx_companies_ext ON companies (org_id, external_id);

CREATE TABLE contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    title           TEXT DEFAULT '',
    -- Who they are in the deal, not what their business card says.
    -- decision_maker | influencer | gatekeeper | user | unknown
    role_type       TEXT NOT NULL DEFAULT 'unknown',
    seniority       TEXT DEFAULT '',      -- c_level | vp | director | manager | ic
    department      TEXT DEFAULT '',
    linkedin        TEXT DEFAULT '',

    -- The field that quietly decides whether a record is worth anything. A
    -- director who started three weeks ago is a different call than one who has
    -- been there nine years, and a contact whose role ended is dead weight the
    -- rep should never see.
    role_started    TEXT,
    role_ended      TEXT,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | left_company | do_not_contact

    source          TEXT NOT NULL DEFAULT 'manual',
    confidence      INTEGER NOT NULL DEFAULT 50,
    last_verified   TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_contacts_company ON contacts (org_id, company_id);

-- A phone number or address is a claim with an expiry, not a string.
CREATE TABLE contact_points (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    contact_id      INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,   -- direct_dial | mobile | switchboard | email
    value           TEXT NOT NULL,
    -- verified   checked against the authoritative system and it answered
    -- probable   pattern or provider match, never independently confirmed
    -- guessed    constructed from a naming convention; a coin flip with a story
    -- catch_all  the domain accepts everything, so acceptance proves nothing
    -- invalid    checked and rejected
    status          TEXT NOT NULL DEFAULT 'probable',
    confidence      INTEGER NOT NULL DEFAULT 50,
    source          TEXT NOT NULL DEFAULT 'manual',
    method          TEXT DEFAULT '',     -- how it was last checked
    last_verified   TEXT,
    -- Incremented by the dialer and the mailer. Three hard bounces or three
    -- disconnected tones is data, and it should cost the record its confidence.
    fail_count      INTEGER NOT NULL DEFAULT 0,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_points_contact ON contact_points (org_id, contact_id, kind);

-- The "why now". A company with no live trigger is a name on a list.
CREATE TABLE triggers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    -- Matches a key in signal_engine.TRIGGER_TYPES. The join between "something
    -- happened" and "here is what it is worth" is that key.
    kind            TEXT NOT NULL,
    headline        TEXT NOT NULL,
    detail          TEXT DEFAULT '',
    source_name     TEXT DEFAULT '',
    source_url      TEXT DEFAULT '',
    observed_at     TEXT NOT NULL,
    -- 0-100 confidence that the event is real and correctly attributed.
    strength        INTEGER NOT NULL DEFAULT 60,
    -- After this date the event is history, not news. Decay is computed from
    -- observed_at; this is the hard floor.
    expires_at      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_triggers_company ON triggers (org_id, company_id, observed_at);

-- ================================================== the execution layer ==

-- A company+contact assigned to a rep to be worked. The unit of the daily list.
CREATE TABLE targets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    contact_id      INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    rep_id          INTEGER REFERENCES reps(id) ON DELETE SET NULL,
    -- new | working | meeting_set | opportunity | won | lost | disqualified
    status          TEXT NOT NULL DEFAULT 'new',
    dq_reason       TEXT DEFAULT '',

    -- Cached score so the daily list is one query rather than one per row.
    -- signal_engine writes it; nothing else may.
    score           INTEGER NOT NULL DEFAULT 0,
    score_at        TEXT,
    signals_json    TEXT DEFAULT '',

    attempts        INTEGER NOT NULL DEFAULT 0,
    connects        INTEGER NOT NULL DEFAULT 0,
    last_touch_at   TEXT,
    last_connect_at TEXT,
    next_action_at  TEXT,
    snoozed_until   TEXT,

    -- AI research, cached. Regenerating a brief on every page load would be
    -- both slow and a bill.
    research_json   TEXT DEFAULT '',
    research_engine TEXT DEFAULT '',
    plan_json       TEXT DEFAULT '',
    plan_engine     TEXT DEFAULT '',

    created_at      TEXT NOT NULL
);
CREATE INDEX idx_targets_rep ON targets (org_id, rep_id, status, score);
CREATE UNIQUE INDEX idx_targets_pair ON targets (org_id, company_id, contact_id);

CREATE TABLE calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    target_id       INTEGER REFERENCES targets(id) ON DELETE CASCADE,
    rep_id          INTEGER REFERENCES reps(id) ON DELETE SET NULL,
    contact_id      INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    direction       TEXT NOT NULL DEFAULT 'outbound',
    to_number       TEXT DEFAULT '',
    from_number     TEXT DEFAULT '',
    provider        TEXT DEFAULT 'simulated',
    provider_call_id TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued|ringing|answered|completed|failed
    -- connected | voicemail | no_answer | gatekeeper | wrong_number | bad_number
    -- | not_interested | callback | meeting_set | do_not_call
    disposition     TEXT DEFAULT '',
    started_at      TEXT,
    ended_at        TEXT,
    duration_sec    INTEGER NOT NULL DEFAULT 0,

    recorded        INTEGER NOT NULL DEFAULT 0,
    recording_url   TEXT DEFAULT '',
    -- Why we were allowed to record: the policy and jurisdiction that permitted
    -- it, written at the time. Reconstructing this later is not possible.
    consent_basis   TEXT DEFAULT '',
    transcript      TEXT DEFAULT '',

    notes           TEXT DEFAULT '',
    analysis_json   TEXT DEFAULT '',
    analysis_engine TEXT DEFAULT '',
    -- The signals that were live on the target when the call was placed, frozen.
    -- Without this the learning loop cannot attribute an outcome to a cause.
    signals_at_call TEXT DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_calls_org ON calls (org_id, rep_id, created_at);

CREATE TABLE call_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    call_id         INTEGER REFERENCES calls(id) ON DELETE CASCADE,
    provider_call_id TEXT DEFAULT '',
    kind            TEXT NOT NULL,
    payload         TEXT DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_call_events ON call_events (org_id, call_id);

CREATE TABLE activities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    target_id       INTEGER REFERENCES targets(id) ON DELETE CASCADE,
    rep_id          INTEGER REFERENCES reps(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL,   -- call | email | note | task | stage | system
    summary         TEXT NOT NULL,
    detail          TEXT DEFAULT '',
    ref_id          INTEGER,
    -- 'rep' or 'system'. A timeline that cannot distinguish what a person did
    -- from what the platform did is a timeline nobody trusts.
    actor           TEXT NOT NULL DEFAULT 'rep',
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_activities_target ON activities (org_id, target_id, created_at);

CREATE TABLE tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    target_id       INTEGER REFERENCES targets(id) ON DELETE CASCADE,
    rep_id          INTEGER REFERENCES reps(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL DEFAULT 'follow_up',  -- call | email | follow_up | meeting
    title           TEXT NOT NULL,
    detail          TEXT DEFAULT '',
    due_at          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',       -- open | done | cancelled
    created_by      TEXT NOT NULL DEFAULT 'rep',
    completed_at    TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_tasks_rep ON tasks (org_id, rep_id, status, due_at);

CREATE TABLE emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    target_id       INTEGER REFERENCES targets(id) ON DELETE CASCADE,
    rep_id          INTEGER REFERENCES reps(id) ON DELETE SET NULL,
    to_address      TEXT DEFAULT '',
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft',   -- draft | queued | sent | failed
    engine          TEXT DEFAULT '',
    sequence_step   INTEGER,
    sent_at         TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE sequences (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    -- Which disposition drops a target in here automatically, if any.
    entry_trigger   TEXT DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE sequence_steps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    sequence_id     INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    step_no         INTEGER NOT NULL,
    day_offset      INTEGER NOT NULL,
    channel         TEXT NOT NULL,      -- call | email | linkedin | task
    title           TEXT NOT NULL,
    guidance        TEXT DEFAULT ''
);

CREATE TABLE enrollments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    sequence_id     INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    target_id       INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    step_no         INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',  -- active | completed | exited
    exit_reason     TEXT DEFAULT '',
    started_at      TEXT NOT NULL,
    next_due_at     TEXT
);
CREATE UNIQUE INDEX idx_enroll_pair ON enrollments (org_id, sequence_id, target_id);

-- ========================================================== the pipeline ==

CREATE TABLE deals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    target_id       INTEGER REFERENCES targets(id) ON DELETE SET NULL,
    rep_id          INTEGER REFERENCES reps(id) ON DELETE SET NULL,
    name            TEXT NOT NULL,
    -- discovery | pricing | pilot | contract | won | lost
    stage           TEXT NOT NULL DEFAULT 'discovery',
    value_annual    REAL NOT NULL DEFAULT 0,
    loads_per_month INTEGER,
    close_date      TEXT,
    -- Set once, on creation, and never moved. The difference between this and
    -- close_date is slippage, and no CRM export contains it.
    close_date_original TEXT,
    next_step       TEXT DEFAULT '',
    -- Which trigger started this. The learning loop reads it to answer "what
    -- kind of news actually turns into revenue".
    origin_signal   TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT
);
CREATE INDEX idx_deals_org ON deals (org_id, rep_id, stage);

-- ====================================================== the learning loop ==

-- One row per (signal, outcome-window). Written by signal_engine.record_outcome
-- as calls resolve, read by signal_engine.learned_weights to bend the scoring
-- toward whatever is actually working for THIS org on THIS list.
CREATE TABLE signal_outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    signal_key      TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    connects        INTEGER NOT NULL DEFAULT 0,
    conversations   INTEGER NOT NULL DEFAULT 0,
    meetings        INTEGER NOT NULL DEFAULT 0,
    opportunities   INTEGER NOT NULL DEFAULT 0,
    won             INTEGER NOT NULL DEFAULT 0,
    dollars         REAL NOT NULL DEFAULT 0,
    updated_at      TEXT
);
CREATE UNIQUE INDEX idx_outcomes_key ON signal_outcomes (org_id, signal_key);

-- ========================================================== housekeeping ==

-- Numbers and addresses nobody in this org may contact, for any reason.
-- Checked before every dial and every send. A suppression list that is advisory
-- is not a suppression list.
CREATE TABLE suppression (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,       -- phone | email | domain
    value           TEXT NOT NULL,
    reason          TEXT DEFAULT '',
    source          TEXT DEFAULT 'manual',  -- manual | dnc_registry | request | bounce
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_suppression ON suppression (org_id, kind, value);

-- One row per uploaded prospect list. The raw CSV is kept so that applying an
-- import re-plans from the same bytes rather than from a serialized plan: an
-- import approved an hour after it was previewed then sees the database as it
-- is now, and a contact somebody added in between is an update, not a duplicate.
CREATE TABLE imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id     INTEGER,
    source      TEXT NOT NULL,          -- zoominfo | apollo | salesforce | hubspot | generic
    filename    TEXT,
    raw_csv     TEXT NOT NULL,
    options_json TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'preview',   -- preview | applied | discarded
    report_json TEXT,
    created_at  TEXT NOT NULL,
    applied_at  TEXT
);
CREATE INDEX idx_imports_org ON imports (org_id, created_at);

CREATE TABLE saved_searches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    rep_id          INTEGER REFERENCES reps(id) ON DELETE SET NULL,
    name            TEXT NOT NULL,
    query_text      TEXT NOT NULL,
    filters_json    TEXT DEFAULT '',
    engine          TEXT DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER,
    user_id     INTEGER,
    action      TEXT NOT NULL,
    entity      TEXT,
    engine      TEXT,
    detail      TEXT,
    ip          TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_audit_org ON audit_log (org_id, created_at);

CREATE TABLE ai_spend (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    month       TEXT NOT NULL,
    cents       REAL NOT NULL,
    feature     TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_spend ON ai_spend (org_id, month);
