-- ============================================================
-- FreightFlip: a reverse load board
-- Drivers post their capacity; brokers compete for it.
--
-- Core idea of the schema:
--   drivers  -> own trucks and post availability (capacity_posts)
--   brokers  -> post loads
--   bids     -> a broker offers a load to a driver's capacity post
-- ============================================================

-- Drivers are the first-class citizens of this marketplace.
CREATE TABLE IF NOT EXISTS drivers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    phone           TEXT,
    cdl_number      TEXT NOT NULL,
    mc_number       TEXT,               -- FMCSA authority (owner-operators)
    home_city       TEXT NOT NULL,
    home_state      TEXT NOT NULL,      -- 2-letter code, e.g. 'GA'
    rating          REAL DEFAULT 5.0,   -- 1.0 to 5.0
    created_at      TEXT DEFAULT (datetime('now'))
);

-- A driver can own more than one truck.
CREATE TABLE IF NOT EXISTS trucks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id       INTEGER NOT NULL REFERENCES drivers(id),
    equipment_type  TEXT NOT NULL CHECK (equipment_type IN
                      ('dry_van', 'reefer', 'flatbed', 'step_deck',
                       'power_only', 'box_truck', 'hotshot', 'tanker')),
    max_weight_lbs  INTEGER NOT NULL,
    length_ft       INTEGER,
    year            INTEGER,
    make_model      TEXT
);

-- The heart of the reversal: a driver posting WHERE they are,
-- WHEN they're free, and WHAT they want to earn.
CREATE TABLE IF NOT EXISTS capacity_posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id       INTEGER NOT NULL REFERENCES drivers(id),
    truck_id        INTEGER NOT NULL REFERENCES trucks(id),
    origin_city     TEXT NOT NULL,      -- where the truck is/will be empty
    origin_state    TEXT NOT NULL,
    available_date  TEXT NOT NULL,      -- ISO date 'YYYY-MM-DD'
    dest_pref_state TEXT,               -- preferred destination state (NULL = anywhere)
    min_rate_per_mile REAL NOT NULL,    -- the driver's rate floor, e.g. 2.50
    max_deadhead_mi INTEGER DEFAULT 100,-- how far they'll drive empty to pick up
    notes           TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open', 'booked', 'expired')),
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Freight brokers — verified by MC number and bond.
CREATE TABLE IF NOT EXISTS brokers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name    TEXT NOT NULL,
    contact_name    TEXT,
    email           TEXT NOT NULL UNIQUE,
    phone           TEXT,
    mc_number       TEXT NOT NULL UNIQUE,
    bond_verified   INTEGER NOT NULL DEFAULT 0,  -- 0/1, via FMCSA lookup
    rating          REAL DEFAULT 5.0,
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Loads a broker needs moved.
CREATE TABLE IF NOT EXISTS loads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_id       INTEGER NOT NULL REFERENCES brokers(id),
    origin_city     TEXT NOT NULL,
    origin_state    TEXT NOT NULL,
    dest_city       TEXT NOT NULL,
    dest_state      TEXT NOT NULL,
    pickup_date     TEXT NOT NULL,
    equipment_type  TEXT NOT NULL,
    weight_lbs      INTEGER NOT NULL,
    distance_miles  INTEGER NOT NULL,
    offered_rate    REAL NOT NULL,      -- total $ for the load
    commodity       TEXT,
    status          TEXT NOT NULL DEFAULT 'available'
                      CHECK (status IN ('available', 'bid_out', 'booked', 'delivered', 'cancelled')),
    created_at      TEXT DEFAULT (datetime('now'))
);

-- The reversal in action: brokers BID a load onto a driver's capacity post.
CREATE TABLE IF NOT EXISTS bids (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    load_id          INTEGER NOT NULL REFERENCES loads(id),
    capacity_post_id INTEGER NOT NULL REFERENCES capacity_posts(id),
    broker_id        INTEGER NOT NULL REFERENCES brokers(id),
    offered_rate     REAL NOT NULL,     -- can be higher than the load's list rate
    message          TEXT,
    status           TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'accepted', 'declined', 'withdrawn')),
    created_at       TEXT DEFAULT (datetime('now')),
    UNIQUE (load_id, capacity_post_id)  -- one bid per load per post
);

-- Indexes for the queries drivers and brokers will actually run.
CREATE INDEX IF NOT EXISTS idx_capacity_origin  ON capacity_posts(origin_state, available_date, status);
CREATE INDEX IF NOT EXISTS idx_loads_origin     ON loads(origin_state, pickup_date, status);
CREATE INDEX IF NOT EXISTS idx_bids_post        ON bids(capacity_post_id, status);
