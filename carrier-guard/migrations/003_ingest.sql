-- ============================================================
-- CarrierGuard 003 — carrier data ingest
--
-- Every ingest run is recorded, successful or not.
--
-- The failure mode this protects against is specific and bad: the ingest job
-- quietly stops working, the carriers table goes stale, and the app keeps
-- serving CLEAR verdicts based on authority data from three weeks ago. Nobody
-- notices, because a stale CLEAR looks exactly like a fresh CLEAR. A run log
-- plus the fetched_at column on carriers is what makes that visible.
-- ============================================================

CREATE TABLE ingest_runs (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source         TEXT NOT NULL,
    mode           TEXT NOT NULL,           -- 'all' | 'stale' | 'single' | 'on_demand'
    status         TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','ok','partial','failed')),
    carriers_seen     INTEGER NOT NULL DEFAULT 0,
    carriers_updated  INTEGER NOT NULL DEFAULT 0,
    errors            INTEGER NOT NULL DEFAULT 0,
    message        TEXT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ
);

CREATE INDEX idx_ingest_runs_time ON ingest_runs (started_at DESC);

-- Refresh scheduling: a carrier someone actually checks is worth refreshing
-- nightly; the other two million in the FMCSA census are not. This index backs
-- the "stale and recently checked" query the nightly job runs.
CREATE INDEX idx_carriers_fetched ON carriers (fetched_at);

-- Distinguishes "we asked FMCSA and this authority does not exist" from
-- "we have never successfully looked this one up", which are very different
-- facts to show a broker who is about to tender a load.
ALTER TABLE carriers
    ADD COLUMN last_verified_at TIMESTAMPTZ;

UPDATE carriers SET last_verified_at = fetched_at;
