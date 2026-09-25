"""
The Mini's archive: the permanent history the relay does not keep.

The relay appends every accepted write to a numbered outbox. This module pulls
that outbox in order, stores each entry in moto.db, and only then acknowledges
it, so nothing can be lost by acknowledging early. Entries are idempotent: ids
come from the relay and are seeded above anything already archived here, so
replaying an entry twice changes nothing.

It also writes the per-trip log files behind the pane's "Open logs" button, and
computes the previous-trip statistics the idle panes show.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Columns the relay-2 incident model added; a moto.db from the Mini's old server
# predates all of them.
INCIDENT_COLUMNS = {
    "rider_closed_at": "TEXT", "rider_resolution": "TEXT",
    "observer_closed_at": "TEXT", "observer_closed_by": "TEXT",
    "silenced_at": "TEXT", "silenced_by": "TEXT",
    "closed_at": "TEXT", "forced": "INTEGER NOT NULL DEFAULT 0",
}
POSITION_COLUMNS = {
    "state": "TEXT", "min_g": "REAL", "mean_g": "REAL", "rms_g": "REAL",
    "peak_rot": "REAL", "mean_rot": "REAL", "accel_n": "INTEGER", "gyro_n": "INTEGER",
    "decel": "REAL",
    # Braking and leaning, measured from 2026-09-16 (Jack's brake tests were invisible
    # in everything recorded before it).
    "peak_horiz_g": "REAL", "mean_horiz_g": "REAL",
}
MOVING_KMH = 3          # at or above this the rider counts as moving
# Positions are kept for ever (Jack, 2026-09-16). A full 84-minute ride is about
# 130 KB, so a hundred rides a year costs some 13 MB — and deleting them would make
# a ride unreplayable, which is the one thing the archive is for.
RETAIN_DAYS = 0


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(path: Path) -> sqlite3.Connection:
    # Several threads share one connection, serialised by the monitor's lock.
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def migrate(path: Path) -> None:
    """Brings an archive written by the Mini's old server up to the relay-2 shape.
    Only ever adds: no archived column or row is changed or dropped."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS riders (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'sleep');
                CREATE TABLE IF NOT EXISTS trips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, rider_id TEXT NOT NULL REFERENCES riders(id),
                    started_at TEXT NOT NULL, ended_at TEXT, state TEXT NOT NULL DEFAULT 'riding');
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id INTEGER REFERENCES trips(id),
                    rider_id TEXT NOT NULL, ts TEXT NOT NULL, received_at TEXT NOT NULL,
                    lat REAL, lon REAL, accuracy REAL, speed REAL, battery INTEGER,
                    peak_g REAL, decel REAL);
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id INTEGER REFERENCES trips(id),
                    rider_id TEXT NOT NULL, raised_at TEXT NOT NULL, kind TEXT NOT NULL,
                    state TEXT NOT NULL, escalate_at TEXT, resolved_at TEXT, resolved_by TEXT,
                    resolution TEXT, evidence TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,
                    tag TEXT NOT NULL, rider_id TEXT, message TEXT NOT NULL);
                -- Where the relay's outbox has been read up to. One row.
                CREATE TABLE IF NOT EXISTS sync_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1), last_seq INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT);
                INSERT OR IGNORE INTO sync_state (id, last_seq) VALUES (1, 0);
                INSERT OR IGNORE INTO riders (id, name) VALUES ('jack','Jack'), ('dana','Dana');
                CREATE INDEX IF NOT EXISTS idx_pos_trip ON positions(trip_id, ts);
                CREATE INDEX IF NOT EXISTS idx_pos_recv ON positions(received_at);
                CREATE INDEX IF NOT EXISTS idx_trips_rider ON trips(rider_id, started_at DESC);
            """)
            for table, columns in (("incidents", INCIDENT_COLUMNS), ("positions", POSITION_COLUMNS)):
                have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                for col, typ in columns.items():
                    if col not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    finally:
        conn.close()


def cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT last_seq FROM sync_state WHERE id=1").fetchone()
    return row["last_seq"] if row else 0


def _insert(conn: sqlite3.Connection, table: str, payload: dict, mode: str = "OR REPLACE") -> None:
    """Stores a relay row snapshot, ignoring any column this archive lacks."""
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    cols = [c for c in payload if c in have]
    sql = (f"INSERT {mode} INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})")
    conn.execute(sql, [payload[c] for c in cols])


def apply(conn: sqlite3.Connection, entries: list) -> dict:
    """Stores a batch of outbox entries and advances the cursor, in ONE transaction.
    Returns counts per kind plus the phone logs that arrived, for the caller to write."""
    counts: dict = {}
    logs = []
    closed_trips = set()
    with conn:
        for e in entries:
            kind, p = e["kind"], e["payload"]
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "trip":
                _insert(conn, "trips", p)
                if p.get("ended_at"):
                    closed_trips.add(p["id"])
            elif kind == "rider":
                conn.execute("INSERT INTO riders (id, name, state) VALUES (?,?,?)"
                             " ON CONFLICT(id) DO UPDATE SET name=excluded.name, state=excluded.state",
                             (p["id"], p["name"], p.get("state", "sleep")))
            elif kind == "position":
                _insert(conn, "positions", p, mode="OR IGNORE")
            elif kind == "incident":
                _insert(conn, "incidents", p)
            elif kind == "event":
                _insert(conn, "events", p, mode="OR IGNORE")
            elif kind == "spike_log":
                logs.append(p)
            # An unknown kind is still acknowledged: it came from a newer relay,
            # and holding the outbox open for it would stall the archive.
        conn.execute("UPDATE sync_state SET last_seq=?, updated_at=? WHERE id=1",
                     (entries[-1]["seq"], now()) if entries else (cursor(conn), now()))
    return {"counts": counts, "logs": logs, "closed_trips": sorted(closed_trips)}


# ---------------------------------------------------------------- trip statistics

def _dt(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def trip_stats(conn: sqlite3.Connection, trip_id: int) -> Optional[dict]:
    t = conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not t:
        return None
    rows = conn.execute("SELECT ts, received_at, speed, state, peak_g, peak_rot FROM positions"
                        " WHERE trip_id=? ORDER BY id", (trip_id,)).fetchall()
    started, ended = _dt(t["started_at"]), _dt(t["ended_at"])
    duration = (ended - started).total_seconds() if started and ended else None
    # Average speed counts riding time only: a coffee stop is not a slow ride.
    riding = [r for r in rows if (r["state"] or "riding") == "riding"]
    riding_s, prev = 0.0, None
    for r in riding:
        at = _dt(r["received_at"])
        if prev and at and 0 < (at - prev).total_seconds() <= 30:
            riding_s += (at - prev).total_seconds()
        prev = at
    speeds = [r["speed"] for r in riding if r["speed"] is not None]
    gs = [r["peak_g"] for r in rows if r["peak_g"] is not None]
    rots = [r["peak_rot"] for r in rows if r["peak_rot"] is not None]
    return {
        "trip_id": trip_id, "rider": t["rider_id"],
        "started_at": t["started_at"], "ended_at": t["ended_at"],
        "duration_s": duration, "riding_s": round(riding_s, 1),
        "max_speed": max(speeds) if speeds else None,
        "avg_speed": (sum(speeds) / len(speeds)) if speeds else None,
        "max_g": max(gs) if gs else None,
        "max_rot": max(rots) if rots else None,
        "points": len(rows),
    }


def last_trip(conn: sqlite3.Connection, rider: str) -> Optional[dict]:
    row = conn.execute("SELECT id FROM trips WHERE rider_id=? AND ended_at IS NOT NULL"
                       " ORDER BY started_at DESC LIMIT 1", (rider,)).fetchone()
    return trip_stats(conn, row["id"]) if row else None


# The calendar day a ride belongs to, in the timezone the Mac is sitting in.
LOCAL_DAY = "substr(datetime(started_at, 'localtime'), 1, 10)"


def days_with_trips(conn: sqlite3.Connection, rider: Optional[str] = None) -> list:
    """Dates that have a finished trip, newest first, for the replay picker. One
    rider's when asked: the button belongs to a rider's pane, so what it opens is
    theirs and nobody else's (Jack, 2026-09-20)."""
    mine = " AND rider_id=?" if rider else ""
    # The day a ride belongs to is the day it was ridden, here, not in UTC: an
    # evening ride in El Salvador starts after midnight UTC and was being filed
    # under tomorrow. And a day only counts rides that can actually be opened —
    # a trip with no positions was making a day that then showed nothing.
    rows = conn.execute(
        f"SELECT {LOCAL_DAY} AS day, COUNT(*) AS trips FROM trips t"
        f" WHERE ended_at IS NOT NULL{mine} AND EXISTS"
        "  (SELECT 1 FROM positions p WHERE p.trip_id = t.id)"
        " GROUP BY day ORDER BY day DESC",
        (rider,) if rider else ()).fetchall()
    return [{"day": r["day"], "trips": r["trips"]} for r in rows]


def trips_on(conn: sqlite3.Connection, day: str, rider: Optional[str] = None) -> list:
    """Every finished trip that started on a given day, with enough to choose by.
    One rider's when asked."""
    mine = " AND rider_id=?" if rider else ""
    rows = conn.execute(
        f"SELECT id FROM trips t WHERE ended_at IS NOT NULL AND {LOCAL_DAY}=?"
        f"{mine} ORDER BY started_at", (day, rider) if rider else (day,)).fetchall()
    out = []
    for r in rows:
        stats = trip_stats(conn, r["id"])
        if stats and stats["points"]:
            out.append(stats)
    return out


def replay(conn: sqlite3.Connection, trip_id: int) -> Optional[dict]:
    """Everything needed to watch a trip again: every position, and the events that
    happened while it was open."""
    stats = trip_stats(conn, trip_id)
    if not stats:
        return None
    frames = [dict(r) for r in conn.execute(
        "SELECT received_at, ts, lat, lon, speed, accuracy, battery, state,"
        " peak_g, mean_g, peak_rot, accel_n, peak_horiz_g"
        " FROM positions WHERE trip_id=? ORDER BY id", (trip_id,))]
    events = [dict(r) for r in conn.execute(
        "SELECT ts, level, tag, rider_id AS rider, message FROM events"
        " WHERE ts BETWEEN ? AND ? ORDER BY id",
        (stats["started_at"], stats["ended_at"] or now()))]
    incidents = [dict(r) for r in conn.execute(
        "SELECT * FROM incidents WHERE trip_id=? ORDER BY id", (trip_id,))]
    return {"trip": stats, "frames": frames, "events": events, "incidents": incidents}


def trail(conn: sqlite3.Connection, trip_id: int, limit: int = 3000) -> list:
    """The trip's track, for the map. Newest last."""
    rows = conn.execute("SELECT lat, lon, speed, received_at FROM positions WHERE trip_id=?"
                        " AND lat IS NOT NULL ORDER BY id DESC LIMIT ?", (trip_id, limit)).fetchall()
    return [{"lat": r["lat"], "lon": r["lon"], "speed": r["speed"], "at": r["received_at"]}
            for r in reversed(rows)]


# ---------------------------------------------------------------- log files

def _stamp(value: Optional[str]) -> str:
    dt = _dt(value)
    return dt.astimezone().strftime("%H:%M:%S") if dt else "--:--:--"


def write_trip_log(conn: sqlite3.Connection, trip_id: int, logs_dir: Path) -> Optional[Path]:
    """Regenerates one trip's log file from the archive. Safe to call repeatedly."""
    stats = trip_stats(conn, trip_id)
    if not stats:
        return None
    started = _dt(stats["started_at"])
    day = started.astimezone().strftime("%Y-%m-%d") if started else "unknown"
    out = logs_dir / stats["rider"] / f"{day}-trip-{trip_id}.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"moto-tracker trip #{trip_id} — {stats['rider']}",
        f"started  {stats['started_at']}",
        f"ended    {stats['ended_at'] or '(open)'}",
        f"duration {fmt_duration(stats['duration_s'])}   riding {fmt_duration(stats['riding_s'])}",
        f"max {stats['max_speed'] or 0:.0f} km/h   avg {stats['avg_speed'] or 0:.0f} km/h"
        f"   peak {stats['max_g'] or 0:.2f} g   rot {stats['max_rot'] or 0:.2f} rad/s",
        f"{stats['points']} positions",
        "",
    ]
    for r in conn.execute("SELECT * FROM positions WHERE trip_id=? ORDER BY id", (trip_id,)):
        sens = ""
        if r["peak_g"] is not None:
            sens = (f"  g {r['peak_g']:.2f}/{r['mean_g'] or 0:.2f}"
                    f"  rot {r['peak_rot'] or 0:.2f}  n {r['accel_n'] or 0}/{r['gyro_n'] or 0}")
        lines.append(
            f"{_stamp(r['received_at'])}  {r['state'] or 'riding':8}"
            f"  {r['lat'] or 0:.5f},{r['lon'] or 0:.5f}"
            f"  {r['speed'] or 0:5.1f} km/h  ±{r['accuracy'] or 0:3.0f} m"
            f"  batt {r['battery'] if r['battery'] is not None else '?'}%{sens}")
    incidents = conn.execute("SELECT * FROM incidents WHERE trip_id=? ORDER BY id", (trip_id,)).fetchall()
    if incidents:
        lines += ["", "incidents:"]
        for i in incidents:
            lines.append(
                f"  #{i['id']} {i['kind']} {i['state']} raised {i['raised_at']}"
                + (f" rider {i['rider_resolution']} at {i['rider_closed_at']}" if i["rider_closed_at"] else "")
                + (f" observer {i['observer_closed_by']} at {i['observer_closed_at']}"
                   if i["observer_closed_at"] else "")
                + (" FORCED" if i["forced"] else ""))
    events = conn.execute("SELECT * FROM events WHERE rider_id=? AND ts BETWEEN ? AND ? ORDER BY id",
                          (stats["rider"], stats["started_at"], stats["ended_at"] or now())).fetchall()
    if events:
        lines += ["", "events:"]
        for e in events:
            lines.append(f"  {_stamp(e['ts'])} {e['level']:5} {e['tag']:9} {e['message']}")
    out.write_text("\n".join(lines) + "\n")
    return out


def write_phone_log(payload: dict, logs_dir: Path) -> Path:
    """A log uploaded from a phone, kept beside that rider's trip logs."""
    rider = "".join(c for c in (payload.get("rider") or "unknown") if c.isalnum() or c in "_-") or "unknown"
    name = Path(payload.get("name") or "phone.log").name
    out = logs_dir / rider / "phone" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload.get("text") or "")
    return out


def prune(conn: sqlite3.Connection, days: int = RETAIN_DAYS) -> int:
    """Nothing is pruned while [RETAIN_DAYS] is 0, which is the setting Jack chose on
    2026-09-16: the archive is small and a deleted position is a ride that can never
    be replayed. Kept as a switch rather than deleted, in case that ever changes."""
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="milliseconds")
    with conn:
        return conn.execute("DELETE FROM positions WHERE received_at < ?"
                            " AND trip_id IN (SELECT id FROM trips WHERE ended_at IS NOT NULL)",
                            (cutoff_iso,)).rowcount


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
