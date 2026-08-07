-- ============================================================
-- CarrierGuard 004 — audit trail and rate limiting
--
-- The audit table is not routine hygiene here. This product lets one company
-- publish a fraud accusation against another company by name. When a carrier
-- disputes a report — and one eventually will — the questions are: who filed
-- it, from where, and when. Without a record, the answer is a shrug, and a
-- shrug is not a defense.
-- ============================================================

CREATE TABLE audit_events (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id     BIGINT REFERENCES organizations(id) ON DELETE SET NULL,
    user_id    BIGINT REFERENCES users(id) ON DELETE SET NULL,
    action     TEXT NOT NULL,          -- 'login', 'fraud_report.create', …
    target     TEXT,                   -- what it acted on, e.g. an MC number
    outcome    TEXT NOT NULL DEFAULT 'ok'
                 CHECK (outcome IN ('ok','denied','failed')),
    ip         INET,
    user_agent TEXT,
    detail     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Nullable org/user with ON DELETE SET NULL, because a failed login has no
-- user yet and a closed account must not take its history with it — the
-- record of what happened has to outlive the account that did it.
CREATE INDEX idx_audit_org    ON audit_events (org_id, created_at DESC);
CREATE INDEX idx_audit_action ON audit_events (action, created_at DESC);
CREATE INDEX idx_audit_target ON audit_events (target, created_at DESC)
    WHERE target IS NOT NULL;


-- Fixed-window rate limiting, in Postgres rather than in memory.
--
-- In-process counters are worthless here: gunicorn runs several workers and
-- production runs several instances, so an in-memory limit of 5 is really a
-- limit of 5 x workers x instances, and it resets on every deploy. The
-- database is the only thing all of them agree on, and at this traffic level
-- a single indexed upsert per request is not worth adding Redis for.
CREATE TABLE rate_limits (
    bucket       TEXT PRIMARY KEY,     -- 'login:ip:203.0.113.4'
    window_start TIMESTAMPTZ NOT NULL DEFAULT now(),
    count        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_rate_limits_window ON rate_limits (window_start);
