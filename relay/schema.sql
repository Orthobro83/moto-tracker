-- moto-tracker relay schema (runs on the VPS). design.md 2026-09-15.
--
-- The relay keeps LIVE STATE ONLY. The history lives on the Mini, which receives
-- every accepted write through the outbox below. The live tables are pruned on a
-- timer; the outbox is pruned only by the Mini's acknowledgement.

PRAGMA journal_mode = WAL;
-- Deleted rows must not linger on disk: live state is pruned within hours.
PRAGMA secure_delete = ON;

CREATE TABLE IF NOT EXISTS riders (
    id          TEXT PRIMARY KEY,          -- 'jack' | 'dana' | '__smoketest__'
    name        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'sleep'   -- riding | offbike | sleep
);

-- A TRIP opens at Start trip and closes at End trip. Riding and off-bike
-- stretches inside it are all part of the same trip. Ids are seeded above the
-- Mini archive's highest id, so archived and relayed trips never collide.
CREATE TABLE IF NOT EXISTS trips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rider_id    TEXT NOT NULL REFERENCES riders(id),
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    state       TEXT NOT NULL DEFAULT 'riding'   -- riding | offbike | sleep
);
CREATE INDEX IF NOT EXISTS idx_trips_rider ON trips(rider_id, started_at DESC);

CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id     INTEGER REFERENCES trips(id),
    rider_id    TEXT NOT NULL,
    state       TEXT,                      -- riding | offbike when recorded
    ts          TEXT NOT NULL,             -- phone's own timestamp
    received_at TEXT NOT NULL,             -- relay arrival, for gap analysis
    lat         REAL, lon REAL,
    accuracy    REAL, speed REAL,
    battery     INTEGER,
    peak_g      REAL,                      -- resultant acceleration / 9.81
    min_g       REAL,
    mean_g      REAL,
    rms_g       REAL,
    peak_rot    REAL,                      -- gyroscope magnitude, rad/s
    mean_rot    REAL,
    accel_n     INTEGER,                   -- samples in the window
    gyro_n      INTEGER,
    decel       REAL,
    -- Horizontal acceleration with gravity taken out: braking, accelerating and
    -- leaning. The only one of these that can see a hard stop (2026-09-16).
    peak_horiz_g REAL,
    mean_horiz_g REAL
);
CREATE INDEX IF NOT EXISTS idx_pos_trip ON positions(trip_id, ts);
CREATE INDEX IF NOT EXISTS idx_pos_recv ON positions(received_at);
CREATE INDEX IF NOT EXISTS idx_pos_rider ON positions(rider_id, id DESC);

CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id      INTEGER REFERENCES trips(id),
    rider_id     TEXT NOT NULL,
    raised_at    TEXT NOT NULL,
    kind         TEXT NOT NULL,            -- candidate | help | crash
    state        TEXT NOT NULL,            -- pending | retracted | sos | closed
    escalate_at  TEXT,
    resolved_at  TEXT,                     -- legacy (rider-only resolution, before 2026-09-15)
    resolved_by  TEXT,
    resolution   TEXT,
    evidence     TEXT,                     -- JSON: full pre-crash buffer
    -- Closing needs BOTH halves (design.md 2026-09-15): the rider's I'm OK or False
    -- alarm, and an Observer's Close incident — from the Mini or the Observer phone.
    rider_closed_at    TEXT,
    rider_resolution   TEXT,               -- ok | false_alarm
    observer_closed_at TEXT,
    observer_closed_by TEXT,               -- device name
    silenced_at        TEXT,               -- one silence quiets every Observer
    silenced_by        TEXT,
    closed_at          TEXT,
    forced             INTEGER NOT NULL DEFAULT 0   -- closed without rider confirmation
);
CREATE INDEX IF NOT EXISTS idx_inc_open ON incidents(state, escalate_at);

-- What normal riding looks like for each rider, computed by the Mini from the whole
-- archive and pushed here. The phone detects crashes against these numbers; the relay
-- uses them to judge whether a silence followed something violent.
CREATE TABLE IF NOT EXISTS baselines (
    rider_id    TEXT PRIMARY KEY REFERENCES riders(id),
    impact_g    REAL NOT NULL,
    impact_rot  REAL NOT NULL,
    -- Below riding speed only a blow this hard counts: the archive's wildest
    -- ordinary readings come from driveways and filtering (Jack, 2026-09-16).
    rearend_g   REAL,
    decel_kmh_s REAL,
    moving_h    REAL,
    source      TEXT,
    updated_at  TEXT NOT NULL
);

-- The relay's own account of what it saw and decided, streamed to monitors.
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL,               -- info | warn | alert
    tag       TEXT NOT NULL,
    rider_id  TEXT,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- Every accepted write, in order, for the Mini. Deleted ONLY when the Mini
-- acknowledges it. Payloads are full row snapshots, so the Mini can rebuild its
-- archive from the outbox alone.
CREATE TABLE IF NOT EXISTS outbox (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT NOT NULL,                -- trip | rider | position | incident | event | spike_log
    ts       TEXT NOT NULL,
    payload  TEXT NOT NULL                 -- JSON
);

-- One row per device. Keys are stored only as SHA-256 hashes; the key itself is
-- shown once, at pairing, and never again.
CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL,           -- rider | observer | monitor | smoke
    rider_id      TEXT REFERENCES riders(id),   -- set for rider (and smoke) keys
    key_hash      TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT,
    revoked_at    TEXT,
    -- Exactly one monitor consumes the outbox and keeps the history. Any other Mac
    -- is a viewer: it reads live state and acts as an Observer, but never acks,
    -- because an ack deletes what the archivist has not stored yet.
    archivist     INTEGER NOT NULL DEFAULT 0
);

-- Short single-use codes, typed once on a phone and exchanged for a device key.
CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash   TEXT PRIMARY KEY,
    role        TEXT NOT NULL,
    rider_id    TEXT,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

INSERT OR IGNORE INTO riders (id, name) VALUES
    ('jack',    'Jack'),
    ('dana', 'Dana');
