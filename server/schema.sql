-- moto-tracker Phase 2 schema.
-- States follow the 2026-09-08 state machine: sleep / riding / offbike are
-- phone-authoritative; stale / sos are server-derived and never stored as a
-- phone state.

PRAGMA journal_mode = WAL;
-- design.md: deleted rows must not linger on disk, so the 30-day purge in
-- Phase 7 actually purges. Set from the start rather than retrofitted.
PRAGMA secure_delete = ON;

CREATE TABLE IF NOT EXISTS riders (
    id          TEXT PRIMARY KEY,          -- 'jack' | 'dana'
    name        TEXT NOT NULL,
    mesh_ip     TEXT
);

-- A TRIP is the unit: it opens at Start trip and closes at End trip, which the
-- rider presses on reaching home. Inside one trip they move between riding and
-- off-bike any number of times — a fuel stop, lunch, an hour at a destination —
-- and all of it, on-bike and off, is part of the same trip.
CREATE TABLE IF NOT EXISTS trips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rider_id    TEXT NOT NULL REFERENCES riders(id),
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    state       TEXT NOT NULL DEFAULT 'riding'   -- riding | offbike
);
CREATE INDEX IF NOT EXISTS idx_trips_rider ON trips(rider_id, started_at DESC);

CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id     INTEGER REFERENCES trips(id),
    rider_id    TEXT NOT NULL,
    -- riding | offbike at the moment this was recorded. Off-bike telemetry is
    -- part of the trip and must be kept; it just has to stay distinguishable.
    state       TEXT,
    ts          TEXT NOT NULL,             -- phone's own timestamp
    received_at TEXT NOT NULL,             -- server arrival, for gap analysis
    lat         REAL, lon REAL,
    accuracy    REAL, speed REAL,
    battery     INTEGER,
    -- Sensor summary for the window since the previous tick.
    -- g is resultant acceleration / 9.81, so ~1.0 at rest, not 0.
    peak_g      REAL,
    min_g       REAL,
    mean_g      REAL,
    rms_g       REAL,
    peak_rot    REAL,                      -- gyroscope magnitude, rad/s
    mean_rot    REAL,
    accel_n     INTEGER,                   -- samples in the window; a collapse
    gyro_n      INTEGER,                   -- here means the OS throttled sensors
    decel       REAL
);
CREATE INDEX IF NOT EXISTS idx_pos_trip ON positions(trip_id, ts);
CREATE INDEX IF NOT EXISTS idx_pos_recv ON positions(received_at);

CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id      INTEGER REFERENCES trips(id),
    rider_id     TEXT NOT NULL,
    raised_at    TEXT NOT NULL,
    kind         TEXT NOT NULL,            -- candidate | help | crash
    state        TEXT NOT NULL,            -- pending | retracted | sos | resolved
    -- design.md: the server escalates any candidate never retracted. This is
    -- the deadline that rule is enforced against.
    escalate_at  TEXT,
    resolved_at  TEXT,
    resolved_by  TEXT,                     -- rider | observer
    resolution   TEXT,                     -- ok | false_alarm | reached_by_other_means
    evidence     TEXT                      -- JSON: full pre-crash buffer
);
CREATE INDEX IF NOT EXISTS idx_inc_open ON incidents(state, escalate_at);

-- The log the dashboard streams. Not the app's diagnostics: this is the
-- server's own account of what it saw and decided.
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL,               -- info | warn | alert
    tag       TEXT NOT NULL,
    rider_id  TEXT,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(id DESC);

INSERT OR IGNORE INTO riders (id, name, mesh_ip) VALUES
    ('jack',    'Jack',    '100.64.0.11'),
    ('dana', 'Dana', '100.64.0.12');
