-- QuotaOS — the operating system for a sales organization.
--
-- Three surfaces, one database, on purpose:
--   1. The floor      — reps, deals, activity, attainment. What a CRM shows you.
--   2. The bench      — courses, certification tracks, scheduled sessions. What an
--                       LMS shows you, except here it is joined to performance.
--   3. The funnel     — reqs, candidates, applications, AI interviews. What an ATS
--                       shows you, except here a hire lands on the org chart with a
--                       ramp plan already attached.
--
-- The joins between those three are the product. A rep who is at risk gets a course
-- assigned. A candidate who passes an interview loop becomes a rep with a
-- certification track. Nothing here is a separate system.
--
-- ---------------------------------------------------------------------------
-- MULTI-TENANCY
--
-- Every tenant-owned table carries `org_id`. Not just the top-level ones — child
-- tables carry it too, even though it could be derived through a join. That
-- redundancy is deliberate: it means a scoping bug in one query leaks nothing,
-- because the WHERE clause on the child table is independently correct. Retrofitting
-- tenant isolation is how SaaS products leak customer data, so it goes in first.
--
-- The rule for every query in app.py: filter on org_id, always, even when a join
-- would make it redundant.
-- ---------------------------------------------------------------------------

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS ai_spend;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS interviews;
DROP TABLE IF EXISTS stage_events;
DROP TABLE IF EXISTS applications;
DROP TABLE IF EXISTS candidates;
DROP TABLE IF EXISTS reqs;
DROP TABLE IF EXISTS session_signups;
DROP TABLE IF EXISTS class_sessions;
DROP TABLE IF EXISTS certifications;
DROP TABLE IF EXISTS enrollments;
DROP TABLE IF EXISTS track_courses;
DROP TABLE IF EXISTS cert_tracks;
DROP TABLE IF EXISTS courses;
DROP TABLE IF EXISTS activities;
DROP TABLE IF EXISTS deals;
DROP TABLE IF EXISTS reps;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS orgs;

-- ----------------------------------------------------------- tenancy/auth --

-- One row per customer company.
CREATE TABLE orgs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    plan        TEXT NOT NULL DEFAULT 'trial',   -- trial | starter | growth
    status      TEXT NOT NULL DEFAULT 'active',  -- active | suspended
    -- Per-tenant monthly ceiling on AI spend, in cents. Enforced before every
    -- model call so one customer's runaway loop can't bill the whole account.
    -- Default $90/month.
    ai_cents_cap INTEGER NOT NULL DEFAULT 9000,
    created_at  TEXT NOT NULL
);

-- A person who can log in. Distinct from `reps`: a sales ops admin has a login and
-- no quota, and a rep who has left keeps their history without keeping their access.
CREATE TABLE users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id),
    email           TEXT NOT NULL UNIQUE,       -- globally unique, so login needs no org picker
    password_hash   TEXT NOT NULL,
    name            TEXT NOT NULL,
    -- owner   — billing and user management, plus everything below
    -- admin   — the whole app for their org
    -- manager — their own team, and hiring
    -- member  — their own rep page only
    role            TEXT NOT NULL DEFAULT 'member',
    rep_id          INTEGER REFERENCES reps(id), -- the seat this login belongs to, if any
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);
CREATE INDEX idx_users_org ON users(org_id);

-- Every AI-assisted employment decision, recorded. NYC Local Law 144 and the EU AI
-- Act both expect an organization to be able to answer "what did the tool output
-- for this candidate, when, on what input, and who acted on it".
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id),
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,          -- ai.posting | ai.search | ai.interview | ai.score | stage.move | hire
    entity      TEXT,                   -- e.g. application:42
    engine      TEXT,                   -- claude | offline
    detail      TEXT,                   -- JSON blob: inputs, outputs, score
    ip          TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_audit_org ON audit_log(org_id, created_at);

-- Append-only ledger of model spend. A table rather than a counter column so the
-- monthly cap is correct across gunicorn workers, and so an org can be shown where
-- its money went rather than just that it ran out.
CREATE TABLE ai_spend (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id),
    month       TEXT NOT NULL,          -- YYYY-MM
    cents       REAL NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_ai_spend_org ON ai_spend(org_id, month);

-- ---------------------------------------------------------------- the floor --

-- One row per human on the sales org chart, VPs and managers included. `manager_id`
-- is a self-reference, which is what lets the whole org roll up from one query.
CREATE TABLE reps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id),
    name            TEXT NOT NULL,
    email           TEXT NOT NULL,
    title           TEXT NOT NULL,
    role            TEXT NOT NULL,          -- vp | manager | rep
    manager_id      INTEGER REFERENCES reps(id),
    team            TEXT NOT NULL,
    region          TEXT NOT NULL,
    segment         TEXT NOT NULL,          -- SMB | Mid-Market | Enterprise
    hire_date       TEXT NOT NULL,
    ramp_end_date   TEXT,                   -- NULL once fully ramped
    quota_annual    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',
    notes           TEXT,
    UNIQUE (org_id, email)
);
CREATE INDEX idx_reps_org ON reps(org_id, status);

-- Open and closed opportunities. `close_date_original` is the field that makes
-- slippage visible: a deal pushed three times has a story a snapshot can't tell.
CREATE TABLE deals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id              INTEGER NOT NULL REFERENCES orgs(id),
    rep_id              INTEGER NOT NULL REFERENCES reps(id),
    account             TEXT NOT NULL,
    amount              INTEGER NOT NULL,
    stage               TEXT NOT NULL,      -- discovery | demo | proposal | negotiation | closed_won | closed_lost
    probability         INTEGER NOT NULL DEFAULT 0,
    created_date        TEXT NOT NULL,
    close_date          TEXT NOT NULL,
    close_date_original TEXT NOT NULL,
    last_activity_date  TEXT,
    next_step           TEXT,
    source              TEXT
);
CREATE INDEX idx_deals_org_rep ON deals(org_id, rep_id, stage);

-- Weekly activity rollup. Raw call logs are noise; the week is the unit a manager
-- actually coaches against.
CREATE TABLE activities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id),
    rep_id      INTEGER NOT NULL REFERENCES reps(id),
    week_start  TEXT NOT NULL,
    calls       INTEGER NOT NULL DEFAULT 0,
    emails      INTEGER NOT NULL DEFAULT 0,
    meetings    INTEGER NOT NULL DEFAULT 0,
    demos       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_activities_org_rep ON activities(org_id, rep_id, week_start);

-- ---------------------------------------------------------------- the bench --

CREATE TABLE courses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id),
    code        TEXT NOT NULL,
    title       TEXT NOT NULL,
    category    TEXT NOT NULL,              -- Product | Method | Compliance | Industry
    hours       REAL NOT NULL,
    summary     TEXT NOT NULL,
    -- The health signal this course is the answer to. This column is the hinge
    -- between the performance side and the training side of the product.
    fixes_signal TEXT,
    UNIQUE (org_id, code)
);

-- A certification is a bundle of courses plus an expiry. Security and compliance
-- certs expire; that is the whole reason this table has `valid_months`.
CREATE TABLE cert_tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id),
    name            TEXT NOT NULL,
    summary         TEXT NOT NULL,
    required_for    TEXT NOT NULL,          -- all | rep | manager | Enterprise | Mid-Market
    valid_months    INTEGER NOT NULL DEFAULT 12,
    is_mandatory    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (org_id, name)
);

CREATE TABLE track_courses (
    org_id      INTEGER NOT NULL REFERENCES orgs(id),
    track_id    INTEGER NOT NULL REFERENCES cert_tracks(id),
    course_id   INTEGER NOT NULL REFERENCES courses(id),
    sequence    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (track_id, course_id)
);

CREATE TABLE enrollments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id),
    rep_id          INTEGER NOT NULL REFERENCES reps(id),
    course_id       INTEGER NOT NULL REFERENCES courses(id),
    status          TEXT NOT NULL DEFAULT 'not_started',  -- not_started | in_progress | complete
    score           INTEGER,
    assigned_on     TEXT NOT NULL,
    completed_on    TEXT,
    assigned_reason TEXT,                   -- why this landed on their plate
    UNIQUE (rep_id, course_id)
);
CREATE INDEX idx_enrollments_org ON enrollments(org_id, rep_id);

CREATE TABLE certifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id),
    rep_id          INTEGER NOT NULL REFERENCES reps(id),
    track_id        INTEGER NOT NULL REFERENCES cert_tracks(id),
    status          TEXT NOT NULL DEFAULT 'not_started',  -- not_started | in_progress | certified | expired
    certified_on    TEXT,
    expires_on      TEXT,
    UNIQUE (rep_id, track_id)
);
CREATE INDEX idx_certifications_org ON certifications(org_id, rep_id);

-- Scheduled, seated, instructor-led sessions — the calendar half of training.
CREATE TABLE class_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id),
    course_id   INTEGER NOT NULL REFERENCES courses(id),
    starts_at   TEXT NOT NULL,
    duration_h  REAL NOT NULL DEFAULT 2,
    location    TEXT NOT NULL,
    instructor  TEXT NOT NULL,
    seats       INTEGER NOT NULL DEFAULT 20
);

CREATE TABLE session_signups (
    org_id      INTEGER NOT NULL REFERENCES orgs(id),
    session_id  INTEGER NOT NULL REFERENCES class_sessions(id),
    rep_id      INTEGER NOT NULL REFERENCES reps(id),
    signed_up_on TEXT NOT NULL,
    PRIMARY KEY (session_id, rep_id)
);

-- --------------------------------------------------------------- the funnel --

CREATE TABLE reqs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id              INTEGER NOT NULL REFERENCES orgs(id),
    title               TEXT NOT NULL,
    level               TEXT NOT NULL,      -- SDR | AE | Senior AE | Manager
    team                TEXT NOT NULL,
    region              TEXT NOT NULL,
    segment             TEXT NOT NULL,
    quota_annual        INTEGER NOT NULL,
    comp_base           INTEGER NOT NULL,
    comp_ote            INTEGER NOT NULL,
    must_haves          TEXT NOT NULL,
    nice_haves          TEXT,
    hiring_manager_id   INTEGER REFERENCES reps(id),
    status              TEXT NOT NULL DEFAULT 'open',   -- draft | open | closed
    posting_md          TEXT,               -- the AI-written job posting
    posting_source      TEXT,               -- claude | offline
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_reqs_org ON reqs(org_id, status);

CREATE TABLE candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id),
    name            TEXT NOT NULL,
    email           TEXT NOT NULL,
    phone           TEXT,
    location        TEXT,
    years_exp       INTEGER NOT NULL DEFAULT 0,
    current_title   TEXT,
    current_company TEXT,
    resume_text     TEXT NOT NULL,
    source          TEXT,
    created_at      TEXT NOT NULL,
    -- Candidate records are personal data. `purge_after` is the retention clock a
    -- deletion job reads; it is not decoration.
    purge_after     TEXT,
    UNIQUE (org_id, email)
);
CREATE INDEX idx_candidates_org ON candidates(org_id);

-- One candidate against one req. The stage column is the spine of the hiring loop.
CREATE TABLE applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id),
    req_id          INTEGER NOT NULL REFERENCES reqs(id),
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id),
    -- sourced | screened | ai_interview | panel | offer | hired | rejected
    stage           TEXT NOT NULL DEFAULT 'sourced',
    match_score     INTEGER,                -- from the resume search, 0-100
    match_reason    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    hired_rep_id    INTEGER REFERENCES reps(id),
    UNIQUE (req_id, candidate_id)
);
CREATE INDEX idx_applications_org ON applications(org_id, stage);

-- Every stage transition, so the loop has an audit trail rather than a status field.
CREATE TABLE stage_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id),
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    from_stage      TEXT,
    to_stage        TEXT NOT NULL,
    note            TEXT,
    actor_user_id   INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL
);

-- A structured AI interview: generated questions, candidate answers, per-answer
-- scores against a rubric, and one recommendation. JSON columns because the shape
-- is the model's output, not something to normalize into forty rows.
CREATE TABLE interviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL REFERENCES orgs(id),
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    kind            TEXT NOT NULL DEFAULT 'screen',   -- screen | role_play | technical
    questions_json  TEXT NOT NULL,
    answers_json    TEXT,
    scores_json     TEXT,
    overall_score   INTEGER,
    recommendation  TEXT,                   -- advance | hold | reject
    summary         TEXT,
    engine          TEXT,                   -- claude | offline
    model           TEXT,                   -- exact model id, for the audit trail
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);
CREATE INDEX idx_interviews_org ON interviews(org_id, application_id);
