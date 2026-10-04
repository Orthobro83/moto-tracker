"""
moto-tracker relay — runs on the VPS (design.md 2026-09-15).

Strictly a relay. The rider phone reports here; the Observer phones and the
Mini's monitor read from here. There is no dashboard, no GUI and no admin
endpoint on this machine — the Mini's interface is the GUI.

History lives on the Mini, not here. Every write this relay accepts is also
appended to a numbered outbox; the Mini pulls the outbox in order, stores it,
and acknowledges the highest number it holds, and only then is it deleted.
Nothing unacknowledged is ever deleted. The relay's own tables hold live state
only — rider states, open trips and their trail, open incidents, recent events —
and are pruned on a timer.

Every request needs a device key except /livez and /pair. Keys have roles:
rider (acts for its own rider only), observer (reads), monitor (the Mini: reads,
syncs, acknowledges), smoke (deploy checks, confined to the __smoketest__ rider).

Listens on 127.0.0.1 only. Caddy terminates HTTPS on 443 with our own CA's
certificate and forwards here.

The phone-facing API is the same one the Mini's server exposed, so a spike needs
only a new address, the CA and a key.
"""
import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

HERE = Path(__file__).parent
DB = Path(os.environ.get("RELAY_DB", "/var/lib/moto-relay/relay.db"))
HOST = os.environ.get("RELAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("RELAY_PORT", "8088"))
VERSION = "relay-16"   # relay-16 (2026-10-04): off the bike, signal loss after 2 min

# How much recent telemetry a newly-connected monitor replays.
TELEMETRY_WINDOW_MIN = 60
# Live state for a closed trip is kept this long, so an Observer opening the app
# after arrival still sees the ride. The Mini holds it permanently either way.
CLOSED_TRIP_KEEP_H = 6
EVENT_KEEP_H = 24
PRUNE_EVERY_S = 300
MAX_LOG_BYTES = 5 * 1024 * 1024
PAIR_CODE_TTL_S = 600
ESCALATE_EVERY_S = 1
# A phone cannot report its own silence, so the relay watches for it. Signal loss is
# never on its own a reason to open an incident (Jack, 2026-09-29): g-force or
# rotation before the silence is. Rural gaps and storms are ordinary — on 2026-09-29
# Dana went unheard for 3.5 minutes after 46 km/h, perfectly well, and the relay's
# old "silence at speed" rule called it a crash. Neither silence after riding speed
# nor a drop in speed before it opens anything any more; they are reported as what
# they are, a signal loss (SIGNAL_LOST_S, SIGNAL_ALERT_S).
SILENCE_WITH_CONTEXT_S = 30     # violent window, then this long unheard: an alarm
SILENCE_CONTEXT_WINDOW_S = 60   # how far back to look for that violence
PENDING_SILENCE_S = 18          # design.md: the stale timeout collapses while a
                                # candidate is pending — a rider who has gone quiet
                                # mid-candidate is the one least able to retract.
                                # Eighteen, not eight: packets come every five
                                # seconds, so eight was one dropped packet.
# A signal loss with nothing violent before it is shown, never raised (Jack,
# 2026-09-29; two minutes off the bike, see OFFBIKE_SIGNAL_LOST_S). After a minute every screen says "Signal lost for m:ss" over the rider's
# last known position, and the log says what they were last doing — a minute, not two
# (Jack, 2026-09-16): a frozen dot with no explanation is itself alarming, and the
# longest legitimate gap measured on the walk tests was 38.8 s. After two minutes the
# Macs and the Observer phones alert: a bar over the map and a single chime, and a
# notification on the phones. An alert, not a crash, and it clears itself when the
# signal comes back. Settable only so the tests need not wait minutes.
SIGNAL_LOST_S = int(os.environ.get("RELAY_SIGNAL_LOST_S", "60"))
SIGNAL_ALERT_S = int(os.environ.get("RELAY_SIGNAL_ALERT_S", "120"))
# Off the bike the phone reports every 30 s instead of every 5 (app 1.0.14), and the
# risk is low, so a loss is shown only after two minutes, the same moment it alerts
# (Jack, 2026-10-04). Settable so the tests need not wait minutes.
OFFBIKE_SIGNAL_LOST_S = int(os.environ.get("RELAY_OFFBIKE_SIGNAL_LOST_S", "120"))


def signal_thresholds(state: Optional[str]) -> tuple:
    """(shown after, alerted after) seconds unheard, for a rider in this state."""
    if state == "offbike":
        return OFFBIKE_SIGNAL_LOST_S, max(SIGNAL_ALERT_S, OFFBIKE_SIGNAL_LOST_S)
    return SIGNAL_LOST_S, SIGNAL_ALERT_S

# Every candidate the phone raises gets this long for the ride to prove itself
# ordinary before anyone is woken (Jack, 2026-09-16). Nothing is ever asked of the
# rider: the evidence is the telemetry itself, and the relay retracts on its own.
MIN_CONFIRM_S = int(os.environ.get("RELAY_MIN_CONFIRM_S", "30"))
RESUMED_RIDING_KMH = 25.0       # back up to this, with sensors back inside baseline
# One open incident per rider (2026-09-18). A report this close to the open
# incident is the same event and joins it; anything later is its own.
JOIN_WINDOW_S = 120
RECENT_MOTION_S = 180           # a low-speed blow only counts if the bike moved lately
FORCE_CLOSE_PHRASE = "close without rider confirmation"
DEVICE_COLUMNS = {"archivist": "INTEGER NOT NULL DEFAULT 0"}
BASELINE_COLUMNS = {"rearend_g": "REAL"}
POSITION_COLUMNS = {"peak_horiz_g": "REAL", "mean_horiz_g": "REAL"}
# Below this the bike is manoeuvring, not riding, and only a vehicle-sized blow
# counts. The same two numbers the phone's detector uses.
RIDING_KMH = 25.0
INCIDENT_COLUMNS = {
    "rider_closed_at": "TEXT", "rider_resolution": "TEXT",
    "observer_closed_at": "TEXT", "observer_closed_by": "TEXT",
    "silenced_at": "TEXT", "silenced_by": "TEXT",
    "closed_at": "TEXT", "forced": "INTEGER NOT NULL DEFAULT 0",
}

# The map key lives here, readable only by the relay's own service account. The
# phones ask for it with their device key and then talk to TomTom directly, so no
# tile byte ever crosses this machine and the key is not inside the APK where
# anyone holding the file could read it out (Jack, 2026-09-16).
MAP_KEY_FILE = Path(os.environ.get("RELAY_MAP_KEY", "/var/lib/moto-relay/tomtom.key"))

SMOKE_RIDER = "__smoketest__"
# A rider's phone is the other rider's Observer (Jack, 2026-09-15): it asks the relay
# whether a trip is open and switches modes by itself, so its key must read too.
READERS = {"rider", "observer", "monitor", "smoke"}

# Ids start far above anything in the Mini archive (35 trips, 0 incidents,
# ~7.2k positions, ~110 events at the time of writing), so rows relayed from here
# can be stored on the Mini under the same ids without ever colliding.
ID_FLOORS = {"trips": 100, "incidents": 1000, "positions": 1_000_000, "events": 1_000_000}

STARTED = time.time()
_subscribers: "list[asyncio.Queue]" = []
_loop: Optional[asyncio.AbstractEventLoop] = None
_pair_attempts: "dict[str, deque]" = {}


# ---------------------------------------------------------------- basics

def now() -> str:
    """Stored timestamps are UTC ISO with milliseconds; they sort as strings."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="milliseconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def tx():
    """One transaction on its own connection, always closed afterwards."""
    conn = db()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    with tx() as conn:
        conn.executescript((HERE / "schema.sql").read_text())
        # Additive migration: a database from relay-1 predates the two-sided close.
        for table, columns in (("incidents", INCIDENT_COLUMNS), ("devices", DEVICE_COLUMNS),
                               ("baselines", BASELINE_COLUMNS), ("positions", POSITION_COLUMNS)):
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in columns.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        # The first monitor ever paired keeps the history, unless one is already named.
        if not conn.execute("SELECT 1 FROM devices WHERE role='monitor' AND archivist=1"
                            " AND revoked_at IS NULL").fetchone():
            first = conn.execute("SELECT id FROM devices WHERE role='monitor' AND revoked_at IS NULL"
                                 " ORDER BY id LIMIT 1").fetchone()
            if first:
                conn.execute("UPDATE devices SET archivist=1 WHERE id=?", (first["id"],))
        for table, floor in ID_FLOORS.items():
            have = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
            top = conn.execute(f"SELECT COALESCE(MAX(id), 0) m FROM {table}").fetchone()["m"]
            current = max(have["seq"] if have else 0, top)
            if current < floor - 1:
                if have:
                    conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (floor - 1, table))
                else:
                    conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?,?)", (table, floor - 1))


def snapshot(conn, table: str, row_id) -> dict:
    return dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone())


def emit(conn, kind: str, payload: dict, rider: Optional[str]) -> None:
    """Append to the outbox, in the same transaction as the write it describes.
    Smoke-test writes never enter it, so they can never reach the Mini archive."""
    if rider == SMOKE_RIDER:
        return
    conn.execute("INSERT INTO outbox (kind, ts, payload) VALUES (?,?,?)",
                 (kind, now(), json.dumps(payload)))


def _offer(q: asyncio.Queue, payload: str) -> None:
    try:
        q.put_nowait(payload)
    except asyncio.QueueFull:
        pass


def _push(ts: str, level: str, tag: str, message: str, rider: Optional[str]) -> None:
    """Hand a line to every live stream. Endpoints run in a thread pool, and an
    asyncio.Queue is not thread-safe, so delivery goes through the event loop."""
    loop = _loop
    if loop is None:
        return
    payload = json.dumps({"ts": ts, "level": level, "tag": tag, "rider": rider, "message": message})
    for q in list(_subscribers):
        loop.call_soon_threadsafe(_offer, q, payload)


def log(level: str, tag: str, message: str, rider: Optional[str] = None) -> None:
    """A notable event: stored, sent to the Mini through the outbox, and streamed."""
    ts = now()
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO events (ts, level, tag, rider_id, message) VALUES (?,?,?,?,?)",
            (ts, level, tag, rider, message))
        emit(conn, "event", snapshot(conn, "events", cur.lastrowid), rider)
    _push(ts, level, tag, message, rider)
    print(f"{ts} {level:5} {tag:10} {rider or '-':13} {message}", flush=True)


def telemetry(message: str, rider: str) -> None:
    """A position tick: streamed live only. The positions row is the record."""
    _push(now(), "tick", "position", message, rider)


# ---------------------------------------------------------------- device keys

def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def device(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "a device key is required", headers={"WWW-Authenticate": "Bearer"})
    key_hash = hash_secret(authorization[7:].strip())
    with tx() as conn:
        dev = conn.execute("SELECT * FROM devices WHERE key_hash=? AND revoked_at IS NULL",
                           (key_hash,)).fetchone()
        # Throttled, so a phone posting every 5 s is not an extra write each time.
        if dev and (dev["last_seen_at"] is None or dev["last_seen_at"] < ago(seconds=60)):
            conn.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (now(), dev["id"]))
    if not dev:
        raise HTTPException(401, "unknown or revoked device key", headers={"WWW-Authenticate": "Bearer"})
    return dict(dev)


def act_for(dev: dict, rider: str) -> None:
    """A rider key posts for its own rider and nobody else's."""
    if dev["role"] in ("rider", "smoke") and dev["rider_id"] == rider:
        return
    raise HTTPException(403, f"this device key cannot act for rider {rider!r}")


def must_read(dev: dict) -> None:
    if dev["role"] not in READERS:
        raise HTTPException(403, "this device key cannot read rider data")


def may_observe(dev: dict, incident_rider: str) -> None:
    """Observer actions — silence and close. The Mini, a dedicated Observer key, or
    the OTHER rider's phone, which is an Observer whenever it is not the one riding.
    A rider is never their own Observer: their half of a close is /incident/resolve,
    and an incident needs both halves."""
    if dev["role"] in ("observer", "monitor"):
        return
    if dev["role"] == "rider" and dev["rider_id"] != incident_rider:
        return
    if dev["role"] == "smoke" and incident_rider == SMOKE_RIDER:
        return
    raise HTTPException(403, "only an Observer device can do that")


def must_archive(dev: dict) -> None:
    """The outbox has exactly one reader. A second Mac is a viewer: it sees everything
    live and can act as an Observer, but acknowledging is what deletes relay rows, and
    two acknowledgers would each archive half the history."""
    if dev["role"] == "smoke":
        raise HTTPException(403, "the smoke key cannot read the archive feed")
    if dev["role"] != "monitor" or not dev["archivist"]:
        raise HTTPException(403, "this monitor is a viewer; another Mac keeps the history")


def must_be_monitor(dev: dict) -> None:
    if dev["role"] != "monitor":
        raise HTTPException(403, "only the monitor key can sync")


# ---------------------------------------------------------------- models

class RiderRef(BaseModel):
    rider: str
    # Start trip opens the trip OFF THE BIKE (Jack, 2026-09-20): getting ready,
    # loading up and setting off are all part of a trip, and none of them are
    # riding. A phone from before that says nothing here and means riding.
    riding: bool = True


class Position(BaseModel):
    rider: str
    ts: Optional[str] = None
    lat: float
    lon: float
    accuracy: Optional[float] = None
    speed: Optional[float] = None
    battery: Optional[int] = None
    peak_g: Optional[float] = None
    min_g: Optional[float] = None
    mean_g: Optional[float] = None
    rms_g: Optional[float] = None
    peak_rot: Optional[float] = None
    peak_horiz_g: Optional[float] = None
    mean_horiz_g: Optional[float] = None
    mean_rot: Optional[float] = None
    accel_n: Optional[int] = None
    gyro_n: Optional[int] = None
    decel: Optional[float] = None


class StopAnswer(BaseModel):
    rider: str
    answer: str          # arrived | traffic | help


class Candidate(BaseModel):
    rider: str
    evidence: Optional[dict] = None
    confirm_window_s: int = 20
    # When the phone concluded it, if that is not now. A phone that detects a crash
    # in a dead spot keeps the candidate and delivers it when the signal returns
    # (Jack, 2026-09-16), and it must not then look like it has only just happened.
    at: Optional[str] = None


class Retract(BaseModel):
    rider: str
    incident_id: int


class Resolve(BaseModel):
    incident_id: int
    rider: str
    resolution: str      # ok | false_alarm


class Ack(BaseModel):
    upto: int


class IncidentRef(BaseModel):
    incident_id: int


class ForceClose(BaseModel):
    incident_id: int
    confirm: str


class BaselineIn(BaseModel):
    rider: str
    impact_g: float
    impact_rot: float
    rearend_g: Optional[float] = None
    decel_kmh_s: Optional[float] = None
    moving_h: Optional[float] = None
    source: Optional[str] = None


class PairRequest(BaseModel):
    code: str
    name: Optional[str] = None


# ---------------------------------------------------------------- helpers

def active_trip(conn, rider: str):
    return conn.execute("SELECT * FROM trips WHERE rider_id=? AND ended_at IS NULL "
                        "ORDER BY id DESC LIMIT 1", (rider,)).fetchone()


def last_trip(conn, rider: str):
    return conn.execute("SELECT * FROM trips WHERE rider_id=? ORDER BY id DESC LIMIT 1",
                        (rider,)).fetchone()


def trip_summary(conn, trip) -> Optional[dict]:
    """What an idle pane shows about the last trip. The archivist's own archive is
    better (it keeps everything); this is for a viewer Mac, and covers the six hours
    the relay keeps a closed trip."""
    if not trip or not trip["ended_at"]:
        return None
    stats = conn.execute(
        "SELECT MAX(speed) top, COUNT(*) n, MAX(peak_g) g, MAX(peak_rot) r FROM positions"
        " WHERE trip_id=?", (trip["id"],)).fetchone()
    riding = conn.execute(
        "SELECT AVG(speed) mean FROM positions WHERE trip_id=? AND COALESCE(state,'riding')='riding'",
        (trip["id"],)).fetchone()
    started, ended = datetime.fromisoformat(trip["started_at"]), datetime.fromisoformat(trip["ended_at"])
    return {"trip_id": trip["id"], "rider": trip["rider_id"],
            "started_at": trip["started_at"], "ended_at": trip["ended_at"],
            "duration_s": (ended - started).total_seconds(),
            "max_speed": stats["top"], "avg_speed": riding["mean"],
            "max_g": stats["g"], "max_rot": stats["r"], "points": stats["n"]}


def rider_state(conn, rider: str) -> str:
    r = conn.execute("SELECT state FROM riders WHERE id=?", (rider,)).fetchone()
    return r["state"] if r else "sleep"


def last_heard(conn, trip) -> str:
    """When this trip last heard from its rider: its newest packet, or the moment it
    opened if none has arrived yet. A packet from an earlier trip says nothing about
    this one — measured from yesterday's last packet, a trip opened this morning
    would begin its life hours "unheard"."""
    row = conn.execute("SELECT received_at FROM positions WHERE rider_id=? AND trip_id=?"
                       " ORDER BY id DESC LIMIT 1", (trip["rider_id"], trip["id"])).fetchone()
    return row["received_at"] if row else trip["started_at"]


def clock(seconds: float) -> str:
    """A duration the way every screen writes one: m:ss, or h:mm:ss past the hour."""
    s = int(seconds)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def set_state(conn, rider: str, state: str) -> None:
    conn.execute("UPDATE riders SET state=? WHERE id=?", (state, rider))
    emit(conn, "rider", snapshot(conn, "riders", rider), rider)


def require_rider(conn, rider: str) -> None:
    if not conn.execute("SELECT 1 FROM riders WHERE id=?", (rider,)).fetchone():
        raise HTTPException(404, f"unknown rider {rider!r}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()
    init_db()
    task = asyncio.create_task(prune_forever())
    escalator = asyncio.create_task(escalate_forever())
    log("info", "relay", f"{VERSION} started on {HOST}:{PORT}")
    try:
        yield
    finally:
        task.cancel()
        escalator.cancel()
        log("info", "relay", "stopped")


app = FastAPI(title="moto-tracker relay", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=lifespan)


# ---------------------------------------------------------------- ride control

@app.post("/trip/start")
@app.post("/ride/start")
def ride_start(r: RiderRef, auto: bool = False, dev: dict = Depends(device)):
    """Opens a trip if none is open, otherwise resumes riding inside it. A trip can
    be opened off the bike, which is how Start trip opens one."""
    act_for(dev, r.rider)
    state = "riding" if r.riding else "offbike"
    with tx() as conn:
        require_rider(conn, r.rider)
        trip = active_trip(conn, r.rider)
        if trip:
            conn.execute("UPDATE trips SET state=? WHERE id=?", (state, trip["id"]))
            tid, opened = trip["id"], False
        else:
            cur = conn.execute("INSERT INTO trips (rider_id, started_at, state) VALUES (?,?,?)",
                               (r.rider, now(), state))
            tid, opened = cur.lastrowid, True
        emit(conn, "trip", snapshot(conn, "trips", tid), r.rider)
        set_state(conn, r.rider, state)
    if opened:
        said = f"TRIP #{tid} started" + ("" if r.riding else " — off the bike until they set off")
    else:
        said = f"riding resumed in trip #{tid}" if r.riding else f"off the bike in trip #{tid}"
    log("info", "trip", said + (" (auto-resume)" if auto else ""), r.rider)
    return {"trip_id": tid, "state": state, "trip_opened": opened}


@app.post("/ride/offbike")
@app.post("/ride/end")
def ride_offbike(r: RiderRef, dev: dict = Depends(device)):
    """Arrived or making a stop. The trip stays open; telemetry keeps reporting."""
    act_for(dev, r.rider)
    with tx() as conn:
        require_rider(conn, r.rider)
        trip = active_trip(conn, r.rider)
        if trip:
            conn.execute("UPDATE trips SET state='offbike' WHERE id=?", (trip["id"],))
            emit(conn, "trip", snapshot(conn, "trips", trip["id"]), r.rider)
        set_state(conn, r.rider, "offbike")
    log("info", "trip", "off the bike — trip continues, telemetry still reporting", r.rider)
    return {"trip_id": trip["id"] if trip else None, "state": "offbike"}


@app.post("/trip/end")
@app.post("/ride/sleep")
def trip_end(r: RiderRef, dev: dict = Depends(device)):
    """End trip: closes the trip and sleeps, which is the same event."""
    act_for(dev, r.rider)
    with tx() as conn:
        require_rider(conn, r.rider)
        was = rider_state(conn, r.rider)
        trip = active_trip(conn, r.rider)
        if trip:
            conn.execute("UPDATE trips SET ended_at=?, state='sleep' WHERE id=?", (now(), trip["id"]))
            emit(conn, "trip", snapshot(conn, "trips", trip["id"]), r.rider)
        set_state(conn, r.rider, "sleep")
    if trip and was == "riding":
        log("warn", "trip", f"TRIP #{trip['id']} ended directly from RIDING — telemetry stops now", r.rider)
    else:
        log("info", "trip", f"TRIP #{trip['id']} ended — asleep" if trip else "sleep (no trip open)", r.rider)
    return {"trip_id": trip["id"] if trip else None, "state": "sleep"}


@app.post("/trip/force-end")
def trip_force_end(r: RiderRef, dev: dict = Depends(device)):
    """Ends somebody else's trip, for when their phone can no longer end it itself
    (Jack, 2026-09-16): a dead battery, a destroyed phone, a rider who got a lift
    home. Without this the trip stays open for ever and the relay keeps expecting
    telemetry that is never coming.

    The Mac's decision, not the rider's, so it is recorded as one. It does not
    touch an open incident: ending a trip is not the same as saying everyone is fine.

    Never a phone's (Jack, 2026-09-24): only the phone in the rider's hand ends that
    rider's trip. With both riders able to ride at once, the other phone is as likely
    to be riding as watching, and it has no business ending a trip it is not on.
    """
    if dev["role"] != "monitor":
        raise HTTPException(403, "only the Mac can end someone else's trip")
    with tx() as conn:
        require_rider(conn, r.rider)
        trip = active_trip(conn, r.rider)
        if not trip:
            raise HTTPException(409, f"{r.rider} has no trip open")
        conn.execute("UPDATE trips SET ended_at=?, state='sleep' WHERE id=?", (now(), trip["id"]))
        emit(conn, "trip", snapshot(conn, "trips", trip["id"]), r.rider)
        set_state(conn, r.rider, "sleep")
    log("warn", "trip", f"TRIP #{trip['id']} FORCE-ENDED by {dev['name']} — {r.rider}'s phone"
                        f" was not going to end it", r.rider)
    return {"trip_id": trip["id"], "state": "sleep", "forced": True}


# ---------------------------------------------------------------- telemetry

@app.post("/position")
def position(p: Position, dev: dict = Depends(device)):
    """Every packet belongs to the open trip, tagged with the state at the time."""
    act_for(dev, p.rider)
    stamp = now()
    with tx() as conn:
        require_rider(conn, p.rider)
        trip = active_trip(conn, p.rider)
        st = rider_state(conn, p.rider)
        heard = last_heard(conn, trip) if trip else None
        cur = conn.execute(
            "INSERT INTO positions (trip_id, rider_id, state, ts, received_at,"
            " lat, lon, accuracy, speed, battery, peak_g, decel, min_g, mean_g,"
            " rms_g, peak_rot, mean_rot, accel_n, gyro_n, peak_horiz_g, mean_horiz_g)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trip["id"] if trip else None, p.rider, st, p.ts or stamp, stamp,
             p.lat, p.lon, p.accuracy, p.speed, p.battery, p.peak_g, p.decel,
             p.min_g, p.mean_g, p.rms_g, p.peak_rot, p.mean_rot, p.accel_n, p.gyro_n,
             p.peak_horiz_g, p.mean_horiz_g))
        emit(conn, "position", snapshot(conn, "positions", cur.lastrowid), p.rider)
        # Normal riding is the evidence. A pending candidate is retracted by the relay
        # itself the moment the telemetry says the rider is riding on with everything
        # back inside their baseline. A crashed rider does not ride away; nobody is
        # ever asked, and no prompt is ever put in front of someone who may be moving
        # (Jack, 2026-09-16).
        returned = []
        for row in conn.execute("SELECT id FROM incidents WHERE rider_id=?"
                                " AND state='pending'", (p.rider,)).fetchall():
            if not normal_riding(conn, p):
                continue
            why = "normal riding resumed"
            conn.execute("UPDATE incidents SET state='retracted', resolved_at=?,"
                         " resolved_by='relay', resolution=? WHERE id=?", (now(), why, row["id"]))
            emit(conn, "incident", snapshot(conn, "incidents", row["id"]), p.rider)
            returned.append((row["id"], why))
    for iid, why in returned:
        log("info", "incident", f"#{iid} cleared itself — {why} for {p.rider}", p.rider)
    # The end of a signal loss is said as plainly as its start, so the log shows how
    # long the rider went unheard (Jack, 2026-09-29).
    gap = seconds_between(heard, stamp) if heard else 0.0
    if signal_thresholds(st)[0] <= gap < float("inf"):
        # Its own tag: a `signal` line after the last packet would read, to the watchdog,
        # as the next signal loss already having been reported.
        log("info", "signal-ok", f"signal back after {clock(gap)} — now at {p.speed or 0:.0f} km/h", p.rider)
    late = ""
    if p.ts:
        try:
            lag = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(p.ts.replace("Z", "+00:00"))).total_seconds()
            if lag > 30:
                late = f"  [backfilled, {lag / 60:.0f} min old]"
        except ValueError:
            pass
    sens = ""
    if p.peak_g is not None:
        sens = (f"  g {p.peak_g:.2f}/{p.mean_g or 0:.2f}  rot {p.peak_rot or 0:.2f}"
                f"  n {p.accel_n or 0}/{p.gyro_n or 0}")
    telemetry(f"{p.lat:.5f}, {p.lon:.5f}  {p.speed or 0:.0f} km/h  ±{p.accuracy or 0:.0f} m  "
              f"batt {p.battery if p.battery is not None else '?'}%{sens}{late}", p.rider)
    return {"ok": True}


@app.post("/stop-answer")
def stop_answer(a: StopAnswer, dev: dict = Depends(device)):
    act_for(dev, a.rider)
    if a.answer not in {"arrived", "traffic", "help"}:
        raise HTTPException(400, "answer must be arrived | traffic | help")
    with tx() as conn:
        require_rider(conn, a.rider)
        trip = active_trip(conn, a.rider)
        if not trip:
            raise HTTPException(409, "no trip in progress")
        if a.answer == "arrived":
            conn.execute("UPDATE trips SET state='offbike' WHERE id=?", (trip["id"],))
            emit(conn, "trip", snapshot(conn, "trips", trip["id"]), a.rider)
            set_state(conn, a.rider, "offbike")
        elif a.answer == "help":
            helped = ask_for_help(conn, a.rider, trip["id"])
    if a.answer == "help":
        log_help(a.rider, *helped)
        return {"ok": True, "answer": a.answer, "incident_id": helped[0]}
    log("info", "stop", f"answered: {a.answer}", a.rider)
    return {"ok": True, "answer": a.answer}


# ---------------------------------------------------------------- incidents

# One open incident per rider (2026-09-18). A screen left on in a pocket pressed I need
# help five times in ten seconds, and each press opened an incident of its own; every
# screen shows only the newest, so four of them had to be found and closed one by one.
#
# A report that belongs to the open incident now joins it. What must never happen is
# the opposite mistake — a new crash, or a new call for help, swallowed quietly by an
# old incident that everyone has already silenced or answered. So:
#   - I need help, while an incident is live (raised in the last two minutes, and
#     nobody has silenced or closed any of it), escalates that incident. Pressed
#     again, it changes nothing. Otherwise it replaces the open incident with a fresh
#     alarm — a new id, which every Mac and phone already treats as new.
#   - A crash report joins the open incident when it is the same event: within two
#     minutes of it, and not after the rider has said they are OK. A later crash is
#     raised as its own candidate, with its own check and its own alarm, as before.
# The silence watchdog never raises a second incident at all (watch_for_silence).

def open_incident(conn, rider: str):
    """The rider's open incident, newest first."""
    return conn.execute("SELECT * FROM incidents WHERE rider_id=? AND state IN ('pending','sos')"
                        " ORDER BY id DESC LIMIT 1", (rider,)).fetchone()


def seconds_between(earlier: Optional[str], later: Optional[str]) -> float:
    try:
        return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def ask_for_help(conn, rider: str, trip_id, row=None) -> tuple:
    """The rider asking for help, from either button. Returns (incident id, what
    happened, the incident it replaced) for log_help() to say after the transaction:
    raised | escalated | again | replaced."""
    row = row or open_incident(conn, rider)
    stamp = now()
    if row is not None:
        live = (row["silenced_at"] is None and row["rider_closed_at"] is None
                and row["observer_closed_at"] is None
                and seconds_between(row["raised_at"], stamp) <= JOIN_WINDOW_S)
        if live:
            if row["state"] == "sos" and row["kind"] == "help":
                return row["id"], "again", None
            conn.execute("UPDATE incidents SET state='sos', kind='help', escalate_at=?,"
                         " rider_closed_at=NULL, rider_resolution=NULL WHERE id=?", (stamp, row["id"]))
            emit(conn, "incident", snapshot(conn, "incidents", row["id"]), rider)
            return row["id"], "escalated", None
    evidence = {}
    if row is not None:
        try:
            evidence = json.loads(row["evidence"] or "{}") or {}
        except ValueError:
            evidence = {}
        evidence["replaces"] = row["id"]
    cur = conn.execute("INSERT INTO incidents (trip_id, rider_id, raised_at, kind, state, escalate_at,"
                       " evidence) VALUES (?,?,?,'help','sos',?,?)",
                       (row["trip_id"] if row is not None and row["trip_id"] else trip_id, rider,
                        stamp, stamp, json.dumps(evidence) if evidence else None))
    iid = cur.lastrowid
    emit(conn, "incident", snapshot(conn, "incidents", iid), rider)
    if row is None:
        return iid, "raised", None
    # The old one is folded into the new: closed by the relay, saying why. Everything
    # it still needed — the rider's answer, an Observer's close — the new one needs.
    conn.execute("UPDATE incidents SET state='closed', closed_at=?, resolved_at=?, resolved_by='relay',"
                 " resolution=? WHERE id=?",
                 (stamp, stamp, f"replaced by #{iid}: the rider asked for help", row["id"]))
    emit(conn, "incident", snapshot(conn, "incidents", row["id"]), rider)
    return iid, "replaced", row["id"]


def log_help(rider: str, iid: int, what: str, replaced: Optional[int]) -> None:
    if what == "again":
        log("warn", "incident", f"#{iid}: I need help pressed again — the same incident, still open", rider)
    elif what == "replaced":
        log("alert", "incident", f"#{iid}: {rider.upper()} PRESSED I NEED HELP — a fresh alarm,"
                                 f" replacing #{replaced}", rider)
    else:
        log("alert", "incident", f"#{iid}: {rider.upper()} PRESSED I NEED HELP", rider)


@app.post("/incident/candidate")
def candidate(c: Candidate, dev: dict = Depends(device)):
    act_for(dev, c.rider)
    # Never less than the evidence window: the ride itself has to be given time to
    # prove ordinary before anyone is woken (Jack, 2026-09-16).
    window = max(c.confirm_window_s, MIN_CONFIRM_S)
    escalate_at = datetime.fromtimestamp(time.time() + window, timezone.utc
                                         ).isoformat(timespec="milliseconds")
    raised_at, late = now(), 0.0
    if c.at:
        try:
            when = datetime.fromisoformat(c.at.replace("Z", "+00:00"))
            late = (datetime.now(timezone.utc) - when).total_seconds()
            # Anything up to an hour old is believable; anything else is a bad clock.
            if 0 <= late <= 3600:
                raised_at = when.isoformat(timespec="milliseconds")
            else:
                late = 0.0
        except ValueError:
            late = 0.0
    fresh = dict(c.evidence or {}, **({"delivered_late_s": round(late)} if late > 10 else {}))
    with tx() as conn:
        require_rider(conn, c.rider)
        trip = active_trip(conn, c.rider)
        row = open_incident(conn, c.rider)
        same_event = (row is not None
                      and abs(seconds_between(row["raised_at"], raised_at)) <= JOIN_WINDOW_S
                      and not (row["rider_closed_at"]
                               and seconds_between(row["rider_closed_at"], raised_at) > 0))
        if same_event:
            joined = join_candidate(conn, row, fresh, escalate_at)
        else:
            cur = conn.execute(
                "INSERT INTO incidents (trip_id, rider_id, raised_at, kind, state, escalate_at, evidence)"
                " VALUES (?,?,?,'candidate','pending',?,?)",
                (trip["id"] if trip else None, c.rider, raised_at, escalate_at, json.dumps(fresh)))
            iid = cur.lastrowid
            emit(conn, "incident", snapshot(conn, "incidents", iid), c.rider)
    if same_event:
        iid, state, due = joined
        log("warn", "incident",
            f"#{iid}: another possible crash reported — added to this candidate" if state == "pending"
            else f"#{iid}: another crash report from the phone, added to this open incident", c.rider)
        return {"incident_id": iid, "escalate_at": due, "joined": True}
    log("warn", "incident",
        f"POSSIBLE CRASH — candidate #{iid}, watching {window}s for the ride to carry on as normal"
        + (f" (the phone held this for {late:.0f}s with no signal)" if late > 10 else ""), c.rider)
    return {"incident_id": iid, "escalate_at": escalate_at}


def join_candidate(conn, row, fresh: dict, escalate_at: str) -> tuple:
    """A crash report for the event the open incident already is. Nothing is lost:
    the report goes into the incident's evidence. Returns (id, state, escalate_at)."""
    try:
        evidence = json.loads(row["evidence"] or "{}") or {}
    except ValueError:
        evidence = {}
    kind, due = row["kind"], row["escalate_at"]
    if row["kind"] == "silence":
        # The phone's own account replaces the relay's inference, and makes it a
        # candidate like any other.
        evidence = dict(fresh, relay=evidence)
        if row["state"] == "pending":
            kind = "candidate"
    else:
        evidence["also"] = (evidence.get("also") or [])[-9:] + [fresh]
    if row["state"] == "pending":
        # Never later than either would have escalated on its own.
        due = escalate_at if not due or seconds_between(escalate_at, due) > 0 else due
    conn.execute("UPDATE incidents SET kind=?, escalate_at=?, evidence=? WHERE id=?",
                 (kind, due, json.dumps(evidence), row["id"]))
    emit(conn, "incident", snapshot(conn, "incidents", row["id"]), row["rider_id"])
    return row["id"], row["state"], due


@app.post("/incident/retract")
def retract(r: Retract, dev: dict = Depends(device)):
    act_for(dev, r.rider)
    with tx() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id=?", (r.incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such incident")
        if row["rider_id"] != r.rider:
            raise HTTPException(403, "only the rider can retract their own candidate")
        outcome = row["state"]
        if outcome == "pending":
            conn.execute("UPDATE incidents SET state='retracted' WHERE id=?", (r.incident_id,))
            emit(conn, "incident", snapshot(conn, "incidents", r.incident_id), r.rider)
            outcome = "retracted"
    if outcome == "retracted":
        log("info", "incident", f"candidate #{r.incident_id} retracted — no crash", r.rider)
        return {"ok": True, "state": "retracted"}
    if outcome == "sos":
        log("warn", "incident", f"retraction for #{r.incident_id} arrived too late — it is an open incident", r.rider)
        return {"ok": False, "state": "sos", "note": "already escalated"}
    return {"ok": False, "state": outcome, "note": "nothing to retract"}


@app.post("/incident/resolve")
def resolve(r: Resolve, dev: dict = Depends(device)):
    """
    The rider's half of closing: I'm OK, or False alarm (design.md 2026-09-15).

    It never closes an incident alone. A concussed rider can sincerely believe they
    are fine, so until an Observer also closes it the incident stays open and every
    screen shows it yellow. A candidate still inside its confirmation window is
    simply retracted.
    """
    if r.resolution not in {"ok", "false_alarm"}:
        raise HTTPException(400, "resolution must be ok | false_alarm")
    act_for(dev, r.rider)
    with tx() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id=?", (r.incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such incident")
        if row["rider_id"] != r.rider:
            refused = True
        else:
            refused, stamp, outcome = False, now(), row["state"]
            if outcome == "pending":
                conn.execute("UPDATE incidents SET state='retracted', rider_closed_at=?, rider_resolution=?"
                             " WHERE id=?", (stamp, r.resolution, r.incident_id))
                outcome = "retracted"
            elif outcome == "sos":
                closes = row["observer_closed_at"] is not None
                conn.execute("UPDATE incidents SET rider_closed_at=COALESCE(rider_closed_at, ?),"
                             " rider_resolution=?, state=?, closed_at=? WHERE id=?",
                             (stamp, r.resolution, "closed" if closes else "sos",
                              stamp if closes else None, r.incident_id))
                outcome = "closed" if closes else "rider_ok"
            if outcome in ("retracted", "closed", "rider_ok"):
                emit(conn, "incident", snapshot(conn, "incidents", r.incident_id), r.rider)
    if refused:
        log("warn", "incident", f"#{r.incident_id} close REFUSED — {r.rider} is not the rider", row["rider_id"])
        raise HTTPException(403, "only the rider can give the rider's close")
    said = "I'm OK" if r.resolution == "ok" else "False alarm"
    if outcome == "rider_ok":
        log("warn", "incident", f"#{r.incident_id}: rider says {said} — still open until an Observer closes it", r.rider)
    elif outcome == "closed":
        log("info", "incident", f"#{r.incident_id} CLOSED — rider ({said}) and Observer both closed it", r.rider)
    elif outcome == "retracted":
        log("info", "incident", f"candidate #{r.incident_id} retracted by the rider ({said})", r.rider)
    return {"ok": True, "state": "sos" if outcome == "rider_ok" else outcome,
            "waiting_for": "observer" if outcome == "rider_ok" else None}


@app.post("/incident/escalate")
def escalate_now(r: Retract, dev: dict = Depends(device)):
    """The rider pressing **I need help** on an open incident.

    This overrides everything else: no confirm window, no waiting for the telemetry
    to prove itself ordinary, no automatic retraction. The rider has said they need
    help, and that is the end of the argument (Jack, 2026-09-16)."""
    act_for(dev, r.rider)
    with tx() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id=? AND rider_id=?",
                           (r.incident_id, r.rider)).fetchone()
        if not row:
            raise HTTPException(404, "no such incident")
        if row["state"] not in ("pending", "sos"):
            raise HTTPException(409, f"incident #{r.incident_id} is {row['state']}")
        helped = ask_for_help(conn, r.rider, row["trip_id"], row)
    log_help(r.rider, *helped)
    return {"ok": True, "state": "sos", "incident_id": helped[0]}


@app.post("/incident/silence")
def silence(r: IncidentRef, dev: dict = Depends(device)):
    """Silences the alarm on every Observer at once. Never closes anything."""
    with tx() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id=?", (r.incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such incident")
        may_observe(dev, row["rider_id"])
        first = row["silenced_at"] is None and row["state"] in ("pending", "sos")
        if first:
            conn.execute("UPDATE incidents SET silenced_at=?, silenced_by=? WHERE id=?",
                         (now(), dev["name"], r.incident_id))
            emit(conn, "incident", snapshot(conn, "incidents", r.incident_id), row["rider_id"])
    if first:
        log("info", "incident", f"#{r.incident_id}: alarm silenced by {dev['name']} — incident still open", row["rider_id"])
    return {"ok": True, "silenced": True}


@app.post("/incident/observer-close")
def observer_close(r: IncidentRef, dev: dict = Depends(device)):
    """The Observer's half of closing, from the Mini or the Observer phone. It also
    silences, since pressing it means someone is dealing with the incident."""
    with tx() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id=?", (r.incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such incident")
        may_observe(dev, row["rider_id"])
        if row["state"] != "sos":
            raise HTTPException(409, f"incident #{r.incident_id} is {row['state']}, not open")
        stamp, closes = now(), row["rider_closed_at"] is not None
        conn.execute("UPDATE incidents SET observer_closed_at=COALESCE(observer_closed_at, ?),"
                     " observer_closed_by=COALESCE(observer_closed_by, ?),"
                     " silenced_at=COALESCE(silenced_at, ?), silenced_by=COALESCE(silenced_by, ?),"
                     " state=?, closed_at=? WHERE id=?",
                     (stamp, dev["name"], stamp, dev["name"], "closed" if closes else "sos",
                      stamp if closes else None, r.incident_id))
        emit(conn, "incident", snapshot(conn, "incidents", r.incident_id), row["rider_id"])
    if closes:
        log("info", "incident", f"#{r.incident_id} CLOSED — {dev['name']} and the rider both closed it", row["rider_id"])
    else:
        log("warn", "incident", f"#{r.incident_id}: {dev['name']} closed it — waiting for the rider to confirm", row["rider_id"])
    return {"ok": True, "state": "closed" if closes else "sos", "waiting_for": None if closes else "rider"}


@app.post("/incident/force-close")
def force_close(r: ForceClose, dev: dict = Depends(device)):
    """
    Closes without the rider's confirmation — for a rider who can never answer
    (phone destroyed, rider in hospital). The Mini only, and only with the exact
    confirmation phrase its two-step dialog sends. Recorded as forced.
    """
    if dev["role"] not in ("monitor", "smoke"):
        raise HTTPException(403, "only the Mini can close an incident without the rider")
    if r.confirm.strip().lower() != FORCE_CLOSE_PHRASE:
        raise HTTPException(400, f"confirm must be exactly: {FORCE_CLOSE_PHRASE}")
    with tx() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id=?", (r.incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such incident")
        if dev["role"] == "smoke" and row["rider_id"] != SMOKE_RIDER:
            raise HTTPException(403, "the smoke key is confined to its test rider")
        if row["state"] not in ("pending", "sos"):
            raise HTTPException(409, f"incident #{r.incident_id} is {row['state']}, not open")
        stamp = now()
        conn.execute("UPDATE incidents SET state='closed', forced=1, closed_at=?,"
                     " observer_closed_at=COALESCE(observer_closed_at, ?), observer_closed_by=COALESCE(observer_closed_by, ?),"
                     " silenced_at=COALESCE(silenced_at, ?), silenced_by=COALESCE(silenced_by, ?) WHERE id=?",
                     (stamp, stamp, dev["name"], stamp, dev["name"], r.incident_id))
        emit(conn, "incident", snapshot(conn, "incidents", r.incident_id), row["rider_id"])
    log("warn", "incident", f"#{r.incident_id} CLOSED WITHOUT RIDER CONFIRMATION by {dev['name']}", row["rider_id"])
    return {"ok": True, "state": "closed", "forced": True}


def incident_view(conn, row) -> dict:
    """Everything a screen needs to show one open incident, in relay time."""
    inc = dict(row)
    rider, raised = inc["rider_id"], inc["raised_at"]
    at = datetime.fromisoformat(raised)
    iso = lambda dt: dt.isoformat(timespec="milliseconds")
    try:
        evidence = json.loads(inc.get("evidence") or "{}") or {}
    except ValueError:
        evidence = {}
    before = conn.execute("SELECT MAX(speed) s FROM positions WHERE rider_id=? AND received_at BETWEEN ? AND ?",
                          (rider, iso(at - timedelta(seconds=30)), raised)).fetchone()["s"]
    after = conn.execute("SELECT speed FROM positions WHERE rider_id=? AND received_at >= ? ORDER BY id LIMIT 1",
                         (rider, raised)).fetchone()
    last = conn.execute("SELECT lat, lon, accuracy, speed, received_at FROM positions WHERE rider_id=?"
                        " ORDER BY id DESC LIMIT 1", (rider,)).fetchone()
    moved = conn.execute("SELECT MAX(received_at) t FROM positions WHERE rider_id=? AND speed >= 3",
                         (rider,)).fetchone()["t"]
    if last and (last["speed"] or 0) >= 3:
        still_since = None
    else:
        still_since = max(t for t in (moved, raised) if t)
    near = conn.execute("SELECT MAX(peak_g) g, MAX(peak_rot) r FROM positions WHERE rider_id=?"
                        " AND received_at BETWEEN ? AND ?",
                        (rider, iso(at - timedelta(seconds=15)), iso(at + timedelta(seconds=15)))).fetchone()
    if inc["state"] == "pending":
        display = "pending"
    elif inc["rider_closed_at"]:
        display = "yellow"
    else:
        display = "red"
    return {
        "id": inc["id"], "kind": inc["kind"], "state": inc["state"], "display": display,
        "raised_at": raised, "escalate_at": inc["escalate_at"],
        "rider_closed_at": inc["rider_closed_at"], "rider_resolution": inc["rider_resolution"],
        "observer_closed_at": inc["observer_closed_at"], "observer_closed_by": inc["observer_closed_by"],
        "silenced_at": inc["silenced_at"], "silenced_by": inc["silenced_by"],
        "last_position": ({"lat": last["lat"], "lon": last["lon"], "accuracy": last["accuracy"],
                           "at": last["received_at"]} if last else None),
        # What the relay recorded when it raised it comes first. A silence is raised
        # half a minute after the last packet, so the windows around raised_at miss
        # the crash itself and would show the Observer no deceleration at all
        # (2026-09-24).
        "speed_before": evidence.get("speed_before", before),
        "speed_after": evidence.get("speed_after",
                                    after["speed"] if after else (last["speed"] if last else None)),
        "stationary_since": still_since,
        "g": evidence.get("peak_g", evidence.get("g")) or near["g"],
        "rot": evidence.get("peak_rot", evidence.get("rot")) or near["r"],
    }


def normal_riding(conn, p) -> bool:
    """Is this packet a rider carrying on as usual? Riding speed, and both sensors
    back inside that rider's own baseline. This is the evidence that retracts a
    candidate — the only thing that does, short of the rider saying so later."""
    if (p.speed or 0) < RESUMED_RIDING_KMH:
        return False
    base = conn.execute("SELECT * FROM baselines WHERE rider_id=?", (p.rider,)).fetchone()
    impact_g = base["impact_g"] if base else 11.0
    impact_rot = base["impact_rot"] if base else 8.0
    return (p.peak_g or 0) < impact_g and (p.peak_rot or 0) < impact_rot


def silence_context(conn, rider: str, last) -> Optional[dict]:
    """Was the last minute before the silence violent? Returns what made it so, or
    None. That is the difference between a valley and a crash — design.md keeps them
    as different events, and only one of them is an alarm.

    Violent means g-force or rotation outside the rider's baseline (Jack, 2026-09-29).
    A drop in speed is not violence: a rider who pulls up at a junction in a dead spot
    has stopped, not crashed, and until 2026-09-29 that alone raised an alarm here.

    Mirrors the phone's detector, including its speed bands: the hardest window is
    judged by how fast the bike was going in the twenty seconds before it, so a
    driveway bump on the way to parking is never read the way a highway impact is.
    """
    base = conn.execute("SELECT * FROM baselines WHERE rider_id=?", (rider,)).fetchone()
    impact_g = base["impact_g"] if base else 11.0
    impact_rot = base["impact_rot"] if base else 8.0
    rearend_g = (base["rearend_g"] if base and base["rearend_g"] else max(impact_g * 1.8, 20.0))
    since = (datetime.fromisoformat(last["received_at"])
             - timedelta(seconds=SILENCE_CONTEXT_WINDOW_S)).isoformat(timespec="milliseconds")
    # Violence that has already been answered for does not count twice. If a
    # candidate was raised over it and then cleared — by the rider, or by the relay
    # seeing the ride carry on as normal — the window starts after that.
    settled = conn.execute(
        "SELECT MAX(COALESCE(resolved_at, closed_at)) t FROM incidents WHERE rider_id=?"
        " AND state IN ('retracted','closed')", (rider,)).fetchone()["t"]
    if settled and settled > since:
        since = settled
    # This trip's packets only: the end of the last one is not this one's violence.
    rows = conn.execute("SELECT * FROM positions WHERE rider_id=? AND trip_id IS ? AND received_at >= ?"
                        " ORDER BY id", (rider, last["trip_id"], since)).fetchall()
    if not rows:
        return None

    def trailing_speed(i: int) -> float:
        """The fastest the bike was going in the four windows up to and including i."""
        return max((rows[j]["speed"] or 0) for j in range(max(0, i - 3), i + 1))

    worst_i = max(range(len(rows)), key=lambda i: rows[i]["peak_g"] or 0)
    hardest = rows[worst_i]["peak_g"] or 0
    riding_at_impact = trailing_speed(worst_i)
    spun = max((r["peak_rot"] or 0) for r in rows)
    top = max((r["speed"] or 0) for r in rows)
    ended = last["speed"] or 0
    common = {"peak_g": hardest, "peak_rot": spun, "speed_before": top, "speed_after": ended}

    if riding_at_impact < RIDING_KMH:
        # Manoeuvring, filtering or parking. Rotation says nothing here, and only a
        # vehicle-sized blow counts (Jack, 2026-09-16) — and only if the bike was
        # actually moving in the last few minutes. A bike knocked off its stand
        # outside a café, with the trip still open, is not a rider being hit.
        moved = conn.execute(
            "SELECT MAX(speed) s FROM positions WHERE rider_id=? AND trip_id IS ? AND received_at >= ?",
            (rider, last["trip_id"], (datetime.fromisoformat(last["received_at"])
                                      - timedelta(seconds=RECENT_MOTION_S)).isoformat(timespec="milliseconds"))
        ).fetchone()["s"] or 0
        if moved < 15:
            return None
        if hardest >= rearend_g:
            return dict(common, why=f"hit at {riding_at_impact:.0f} km/h: {hardest:.1f} g"
                                    f" (low-speed threshold {rearend_g:.1f})")
        return None
    if hardest >= impact_g:
        return dict(common, why=f"impact {hardest:.1f} g (threshold {impact_g:.1f})")
    if spun >= impact_rot:
        return dict(common, why=f"rotation {spun:.1f} rad/s (threshold {impact_rot:.1f})")
    return None


def watch_for_silence() -> None:
    """A phone cannot report that it has stopped reporting. This does it for them.

    Violence, then silence, is a crash that may have taken the phone with it, and it
    is an alarm. Silence with nothing violent before it is a signal loss, and it is
    never an incident (Jack, 2026-09-29): it is said in the log after a minute and
    again after two, when the screens show it and then alert (signal_view).

    Every write happens inside the one transaction; every log line is written after
    it closes. `log()` opens its own connection, and calling it while this one holds
    the write lock deadlocks until the busy timeout expires."""
    notes = []
    with tx() as conn:
        for rider in conn.execute("SELECT * FROM riders WHERE state IN ('riding','offbike')").fetchall():
            if rider["id"] == SMOKE_RIDER:
                continue
            trip = active_trip(conn, rider["id"])
            if not trip:
                continue
            heard = last_heard(conn, trip)
            quiet = seconds_between(heard, now())
            last = conn.execute("SELECT * FROM positions WHERE rider_id=? AND trip_id=?"
                                " ORDER BY id DESC LIMIT 1", (rider["id"], trip["id"])).fetchone()
            open_inc = open_incident(conn, rider["id"])

            # The phone raised a candidate and has gone quiet since, mid-ride: the stale
            # timeout collapses (design.md). Whoever cannot answer is who this is for.
            if (rider["state"] == "riding" and open_inc and open_inc["state"] == "pending"
                    and quiet >= PENDING_SILENCE_S):
                if open_inc["escalate_at"] and open_inc["escalate_at"] > now():
                    conn.execute("UPDATE incidents SET escalate_at=? WHERE id=?", (now(), open_inc["id"]))
                    emit(conn, "incident", snapshot(conn, "incidents", open_inc["id"]), rider["id"])
                    notes.append(("warn", "incident",
                                  f"#{open_inc['id']}: the phone went quiet with a candidate pending"
                                  f" — not waiting out the window", rider["id"]))
                continue
            if open_inc:
                continue

            # Already asked about this silence, and the rider said it was nothing.
            # Their answer stands until fresh telemetry arrives: raising it again
            # thirty seconds later would be crying wolf at someone who has answered.
            answered = conn.execute(
                "SELECT state, raised_at FROM incidents WHERE rider_id=?"
                " ORDER BY id DESC LIMIT 1", (rider["id"],)).fetchone()
            already_answered = (answered and answered["state"] == "retracted"
                                and answered["raised_at"] > heard)

            if (rider["state"] == "riding" and last and not already_answered
                    and quiet >= SILENCE_WITH_CONTEXT_S):
                context = silence_context(conn, rider["id"], last)
                if context:
                    # Due at once. Its confirm window was the silence itself: the ride
                    # had half a minute to carry on, and nobody who can retract it is
                    # there to. So violence then silence alarms at about 31 s (Jack,
                    # 2026-09-29) — it always did; until then it was dressed up as a
                    # thirty-second window that the rule above cut short a second later.
                    cur = conn.execute(
                        "INSERT INTO incidents (trip_id, rider_id, raised_at, kind, state, escalate_at,"
                        " evidence) VALUES (?,?,?,?,?,?,?)",
                        (trip["id"], rider["id"], now(), "silence", "pending", now(),
                         json.dumps(dict(context, quiet_s=round(quiet)))))
                    emit(conn, "incident", snapshot(conn, "incidents", cur.lastrowid), rider["id"])
                    notes.append(("alert", "incident",
                                  f"SIGNAL LOST after {context['why']} — candidate #{cur.lastrowid},"
                                  f" quiet {quiet:.0f}s", rider["id"]))
                    continue

            # A signal loss: shown, said, never raised. Once each, so the log gives a
            # reason rather than a frozen dot, and records when the screens alerted.
            lost_s, alert_s = signal_thresholds(rider["state"])
            if quiet < lost_s:
                continue
            if last is None:
                seen = "nothing heard since the trip opened"
            elif rider["state"] == "offbike":
                seen = "last seen off the bike"
            else:
                seen = f"last seen at {last['speed'] or 0:.0f} km/h"
            for tag, due, message in (
                    ("signal", lost_s,
                     f"signal lost {quiet:.0f}s ago — {seen}, nothing violent before it"),
                    ("no-signal", alert_s,
                     f"signal lost for {clock(quiet)} — {seen}, nothing violent before it."
                     f" An alert, not an incident")):
                said = conn.execute("SELECT 1 FROM events WHERE rider_id=? AND tag=? AND ts >= ?",
                                    (rider["id"], tag, heard)).fetchone()
                if quiet >= due and not said:
                    notes.append(("warn", tag, message, rider["id"]))
    for level, tag, message, rider in notes:
        log(level, tag, message, rider)


def signal_view(conn, trip, open_inc) -> Optional[dict]:
    """A rider on a trip who has not been heard from for SIGNAL_LOST_S: since when
    (relay time), and whether it has gone on long enough to alert (Jack, 2026-09-29).
    None while they are being heard, and whenever no trip is open.

    The screens show this rather than each working it out, so both Macs and both
    phones say the same thing at the same moment — and a screen that has lost the
    relay itself cannot mistake its own silence for the rider's. An open incident
    outranks the alert: its own alarm is already sounding."""
    if not trip:
        return None
    heard = last_heard(conn, trip)
    quiet = seconds_between(heard, now())
    lost_s, alert_s = signal_thresholds(rider_state(conn, trip["rider_id"]))
    if quiet < lost_s:
        return None
    return {"since": heard, "lost_s": round(quiet, 1),
            "alert": quiet >= alert_s and open_inc is None}


def escalate_due() -> int:
    """The default is alarm: a candidate not retracted by its deadline becomes an incident."""
    stamp, escalated = now(), []
    with tx() as conn:
        for row in conn.execute("SELECT id, rider_id, kind, evidence FROM incidents WHERE state='pending'"
                                " AND escalate_at IS NOT NULL AND escalate_at <= ?", (stamp,)).fetchall():
            conn.execute("UPDATE incidents SET state='sos' WHERE id=? AND state='pending'", (row["id"],))
            emit(conn, "incident", snapshot(conn, "incidents", row["id"]), row["rider_id"])
            escalated.append((row["id"], row["rider_id"], row["kind"], row["evidence"]))
    for iid, rider, kind, evidence in escalated:
        if kind == "silence":
            try:
                ev = json.loads(evidence or "{}") or {}
            except ValueError:
                ev = {}
            said = (f"CRASH DETECTED — #{iid}: {ev.get('why', 'something violent')},"
                    f" then no signal for {ev.get('quiet_s', '?')}s")
        else:
            said = f"CRASH DETECTED — candidate #{iid} was not retracted in time"
        log("alert", "incident", said, rider)
    return len(escalated)


async def escalate_forever() -> None:
    while True:
        try:
            await asyncio.to_thread(escalate_due)
            await asyncio.to_thread(watch_for_silence)
        except Exception as e:  # never let escalation take the relay down
            print(f"{now()} warn  escalate   -             failed: {e!r}", flush=True)
        await asyncio.sleep(ESCALATE_EVERY_S)


# ---------------------------------------------------------------- spike logs

@app.post("/spike/log")
async def spike_log(request: Request, build: str = "?", rider: str = "?", dev: dict = Depends(device)):
    """A phone's on-device log. Relayed to the Mini through the outbox, not kept here."""
    act_for(dev, rider)
    body = await request.body()
    if len(body) > MAX_LOG_BYTES:
        raise HTTPException(413, f"log larger than {MAX_LOG_BYTES} bytes")
    text = body.decode("utf-8", "replace")
    name = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{rider}-{build}.log"
    with tx() as conn:
        emit(conn, "spike_log", {"name": name, "rider": rider, "build": build,
                                 "received_at": now(), "text": text}, rider)
    log("info", "spike", f"log uploaded: {name} ({len(body)} bytes) — waiting for the Mini", rider)
    return {"ok": True, "saved": name}


# ---------------------------------------------------------------- baselines

@app.post("/baseline")
def put_baseline(b: BaselineIn, dev: dict = Depends(device)):
    """The Mini pushes what it has computed from the whole archive. Only the Mini
    has the history to compute it, and only one set of numbers may exist, so both
    the phone and this relay judge a ride against exactly the same thresholds."""
    must_be_monitor(dev)
    with tx() as conn:
        if not conn.execute("SELECT 1 FROM riders WHERE id=?", (b.rider,)).fetchone():
            raise HTTPException(404, f"unknown rider {b.rider!r}")
        before = conn.execute("SELECT impact_g FROM baselines WHERE rider_id=?", (b.rider,)).fetchone()
        conn.execute(
            "INSERT INTO baselines (rider_id, impact_g, impact_rot, rearend_g, decel_kmh_s,"
            " moving_h, source, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(rider_id) DO UPDATE SET"
            " impact_g=excluded.impact_g, impact_rot=excluded.impact_rot,"
            " rearend_g=excluded.rearend_g, decel_kmh_s=excluded.decel_kmh_s,"
            " moving_h=excluded.moving_h, source=excluded.source, updated_at=excluded.updated_at",
            (b.rider, b.impact_g, b.impact_rot, b.rearend_g or max(b.impact_g * 1.8, 20.0),
             b.decel_kmh_s, b.moving_h, b.source, now()))
    if not before or abs(before["impact_g"] - b.impact_g) > 0.05:
        log("info", "baseline", f"{b.rider}: impact {b.impact_g:.1f} g / {b.impact_rot:.1f} rad/s"
                                f" from {b.moving_h or 0:.1f} h of riding", b.rider)
    return {"ok": True}


@app.get("/baseline")
def get_baseline(rider: str, dev: dict = Depends(device)):
    """The phone asks at the start of a trip. A rider key may only ask about itself."""
    if dev["role"] in ("rider", "smoke") and dev["rider_id"] != rider:
        raise HTTPException(403, "a rider key may only read its own baseline")
    if dev["role"] not in ("rider", "smoke") and dev["role"] not in READERS:
        raise HTTPException(403, "not allowed")
    with tx() as conn:
        row = conn.execute("SELECT * FROM baselines WHERE rider_id=?", (rider,)).fetchone()
    if not row:
        raise HTTPException(404, f"no baseline for {rider!r} yet")
    return dict(row)


@app.get("/map-key")
def map_key(dev: dict = Depends(device)):
    """The map key, for any paired device. Revoking a device revokes its maps with it.

    Deliberately not cached anywhere public and never logged: the answer is the
    secret. A phone keeps it in its own private storage and refreshes it when the
    relay is reachable."""
    if dev["role"] == "smoke":
        raise HTTPException(403, "the smoke key gets no map key")
    try:
        key = MAP_KEY_FILE.read_text().strip()
    except OSError:
        raise HTTPException(503, "no map key on this relay yet")
    if not key:
        raise HTTPException(503, "no map key on this relay yet")
    return {"key": key, "tiles": "https://api.tomtom.com"}


# ---------------------------------------------------------------- read

@app.get("/livez", response_class=PlainTextResponse)
def livez():
    """Bare liveness for health checks. Says nothing else."""
    return "ok"


@app.get("/health")
def health(dev: dict = Depends(device)):
    """Any valid key. The phones use this for their reachability light."""
    with tx() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("trips", "positions", "incidents", "events")}
        pending = conn.execute("SELECT COUNT(*) c, MIN(ts) oldest FROM outbox").fetchone()
        out = {"ok": True, "version": VERSION, "uptime_s": round(time.time() - STARTED, 1),
               "counts": counts, "outbox_pending": pending["c"], "outbox_oldest": pending["oldest"],
               "device": dev["role"], "archivist": bool(dev["archivist"]) if dev["role"] == "monitor" else None}
        # Only for the deploy check: proves smoke writes never entered the outbox.
        # Not computed for the phones, which call this every few seconds.
        if dev["role"] == "smoke":
            out["outbox_smoke"] = conn.execute(
                "SELECT COUNT(*) c FROM outbox WHERE payload LIKE ?", (f"%{SMOKE_RIDER}%",)).fetchone()["c"]
    return out


@app.get("/state")
def state(dev: dict = Depends(device)):
    must_read(dev)
    out = {}
    with tx() as conn:
        for rider in conn.execute("SELECT * FROM riders").fetchall():
            if rider["id"] == SMOKE_RIDER and dev["role"] != "smoke":
                continue
            trip = active_trip(conn, rider["id"])
            last = conn.execute("SELECT * FROM positions WHERE rider_id=? ORDER BY id DESC LIMIT 1",
                                (rider["id"],)).fetchone()
            openinc = conn.execute(
                "SELECT * FROM incidents WHERE rider_id=? AND state IN ('pending','sos')"
                " ORDER BY id DESC LIMIT 1", (rider["id"],)).fetchone()
            prev = last_trip(conn, rider["id"])
            scope_trip, scope_label = ((trip["id"], "trip") if trip
                                       else (prev["id"] if prev else -1, "last trip"))
            peaks = conn.execute("SELECT MAX(peak_g) mg, MAX(peak_rot) mr, MAX(mean_g) mmg"
                                 " FROM positions WHERE trip_id=?", (scope_trip,)).fetchone()
            onbike = conn.execute("SELECT MAX(peak_g) mg, MAX(peak_rot) mr FROM positions"
                                  " WHERE trip_id=? AND state='riding'", (scope_trip,)).fetchone()
            when = None
            if peaks and peaks["mg"] is not None:
                w = conn.execute("SELECT received_at FROM positions WHERE trip_id=? AND peak_g=?"
                                 " ORDER BY id DESC LIMIT 1", (scope_trip, peaks["mg"])).fetchone()
                when = w["received_at"] if w else None
            when_rot = None
            if peaks and peaks["mr"] is not None:
                w = conn.execute("SELECT received_at FROM positions WHERE trip_id=? AND peak_rot=?"
                                 " ORDER BY id DESC LIMIT 1", (scope_trip, peaks["mr"])).fetchone()
                when_rot = w["received_at"] if w else None
            age = None
            if last:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(last["received_at"])).total_seconds()
            out[rider["id"]] = {
                "name": rider["name"],
                "state": rider["state"],
                "trip_id": trip["id"] if trip else None,
                "trip_started": trip["started_at"] if trip else None,
                "last_seen_s": round(age, 1) if age is not None else None,
                "speed": last["speed"] if last else None,
                "battery": last["battery"] if last else None,
                "peak_g": last["peak_g"] if last else None,
                "mean_g": last["mean_g"] if last else None,
                "peak_rot": last["peak_rot"] if last else None,
                "accel_n": last["accel_n"] if last else None,
                "max_g": peaks["mg"] if peaks else None,
                "max_mean_g": peaks["mmg"] if peaks else None,
                "max_rot": peaks["mr"] if peaks else None,
                "max_g_at": when,
                "max_rot_at": when_rot,
                "accuracy": last["accuracy"] if last else None,
                "last_received_at": last["received_at"] if last else None,
                # Relay time at the moment of this answer, so a screen can run its
                # counters in real time without trusting its own clock.
                "as_of": now(),
                "max_g_riding": onbike["mg"] if onbike else None,
                "max_rot_riding": onbike["mr"] if onbike else None,
                "peaks_scope": scope_label,
                "lat": last["lat"] if last else None,
                "lon": last["lon"] if last else None,
                "incident": incident_view(conn, openinc) if openinc else None,
                # Unheard for a minute with a trip open, and whether it is past the
                # two-minute alert (relay-15). Every screen draws its signal loss from this.
                "signal": signal_view(conn, trip, openinc),
                # For a viewer Mac with no archive of its own; the archivist prefers its own.
                "previous_trip": None if trip else trip_summary(conn, prev),
            }
    return out


@app.get("/events")
async def events(dev: dict = Depends(device)):
    """Server-sent events: recent events and telemetry, then everything live."""
    must_read(dev)
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subscribers.append(q)

    async def stream():
        try:
            backlog = []
            with tx() as conn:
                for row in conn.execute("SELECT ts, level, tag, rider_id, message FROM events"
                                        " ORDER BY id DESC LIMIT 50").fetchall():
                    if row["rider_id"] == SMOKE_RIDER and dev["role"] != "smoke":
                        continue
                    backlog.append((row["ts"], {"ts": row["ts"], "level": row["level"], "tag": row["tag"],
                                                "rider": row["rider_id"], "message": row["message"]}))
                for t in conn.execute(
                        "SELECT received_at, rider_id, lat, lon, speed, accuracy, battery,"
                        " peak_g, mean_g, peak_rot, accel_n, gyro_n FROM positions"
                        " WHERE received_at >= ? ORDER BY id", (ago(minutes=TELEMETRY_WINDOW_MIN),)).fetchall():
                    if t["rider_id"] == SMOKE_RIDER and dev["role"] != "smoke":
                        continue
                    sens = ""
                    if t["peak_g"] is not None:
                        sens = (f"  g {t['peak_g']:.2f}/{t['mean_g'] or 0:.2f}  rot {t['peak_rot'] or 0:.2f}"
                                f"  n {t['accel_n'] or 0}/{t['gyro_n'] or 0}")
                    backlog.append((t["received_at"], {
                        "ts": t["received_at"], "level": "tick", "tag": "position", "rider": t["rider_id"],
                        "message": (f"{t['lat']:.5f}, {t['lon']:.5f}  {t['speed'] or 0:.0f} km/h  "
                                    f"±{t['accuracy'] or 0:.0f} m  "
                                    f"batt {t['battery'] if t['battery'] is not None else '?'}%{sens}")}))
            backlog.sort(key=lambda x: x[0])
            for _, payload in backlog:
                yield "data: " + json.dumps(payload) + "\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                    if dev["role"] != "smoke" and json.loads(item).get("rider") == SMOKE_RIDER:
                        continue
                    yield f"data: {item}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if q in _subscribers:
                _subscribers.remove(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- the Mini's archive feed

@app.get("/sync")
def sync(after: int = 0, limit: int = 500, dev: dict = Depends(device)):
    """Outbox entries after `after`, oldest first. The Mini stores them, then acks."""
    must_archive(dev)
    limit = max(1, min(limit, 1000))
    with tx() as conn:
        rows = conn.execute("SELECT seq, kind, ts, payload FROM outbox WHERE seq > ? ORDER BY seq LIMIT ?",
                            (after, limit)).fetchall()
        pending = conn.execute("SELECT COUNT(*) c FROM outbox WHERE seq > ?", (after,)).fetchone()["c"]
    return {"entries": [{"seq": r["seq"], "kind": r["kind"], "ts": r["ts"],
                         "payload": json.loads(r["payload"])} for r in rows],
            "more": pending > len(rows)}


@app.post("/sync/ack")
def sync_ack(a: Ack, dev: dict = Depends(device)):
    """The Mini holds everything up to `upto`. Only now may it leave the relay."""
    must_archive(dev)
    with tx() as conn:
        top = conn.execute("SELECT COALESCE(MAX(seq), 0) m FROM outbox").fetchone()["m"]
        if a.upto > top:
            raise HTTPException(409, f"cannot acknowledge past the last entry ({top})")
        deleted = conn.execute("DELETE FROM outbox WHERE seq <= ?", (a.upto,)).rowcount
    return {"ok": True, "deleted": deleted}


# ---------------------------------------------------------------- pairing

def _pair_allowed(client: str) -> bool:
    """At most 10 attempts per address per 10 minutes. Codes are 8 characters from
    a 31-symbol alphabet and live 10 minutes, so guessing is hopeless anyway."""
    window = _pair_attempts.setdefault(client, deque())
    cutoff = time.time() - 600
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= 10:
        return False
    window.append(time.time())
    return True


@app.post("/pair")
def pair(p: PairRequest, request: Request):
    """Exchange a single-use pairing code for a device key. The key is shown once."""
    client = request.client.host if request.client else "?"
    if not _pair_allowed(client):
        raise HTTPException(429, "too many pairing attempts; wait ten minutes")
    code = "".join(ch for ch in p.code.upper() if ch.isalnum())
    with tx() as conn:
        row = conn.execute("SELECT * FROM pairing_codes WHERE code_hash=?", (hash_secret(code),)).fetchone()
        if not row or row["used_at"] or row["expires_at"] < now():
            raise HTTPException(403, "that pairing code is not valid")
        key = "mt_" + secrets.token_urlsafe(32)
        name = (p.name or row["name"]).strip()[:80] or row["name"]
        conn.execute("UPDATE pairing_codes SET used_at=? WHERE code_hash=?", (now(), row["code_hash"]))
        archivist = 0
        if row["role"] == "monitor" and not conn.execute(
                "SELECT 1 FROM devices WHERE role='monitor' AND archivist=1 AND revoked_at IS NULL").fetchone():
            archivist = 1
        conn.execute("INSERT INTO devices (name, role, rider_id, key_hash, created_at, archivist)"
                     " VALUES (?,?,?,?,?,?)",
                     (name, row["role"], row["rider_id"], hash_secret(key), now(), archivist))
    log("info", "device", f"paired: {name} ({row['role']}{', ' + row['rider_id'] if row['rider_id'] else ''})",
        row["rider_id"])
    return {"key": key, "role": row["role"], "rider": row["rider_id"], "name": name,
            "archivist": bool(archivist)}


# ---------------------------------------------------------------- smoke test

@app.post("/smoke/cleanup")
def smoke_cleanup(dev: dict = Depends(device)):
    """Removes every live row the smoke test wrote. None of it reached the outbox."""
    if dev["role"] != "smoke":
        raise HTTPException(403, "smoke key only")
    with tx() as conn:
        n = 0
        n += conn.execute("DELETE FROM positions WHERE rider_id=?", (SMOKE_RIDER,)).rowcount
        n += conn.execute("DELETE FROM incidents WHERE rider_id=?", (SMOKE_RIDER,)).rowcount
        n += conn.execute("DELETE FROM trips WHERE rider_id=?", (SMOKE_RIDER,)).rowcount
        n += conn.execute("DELETE FROM events WHERE rider_id=?", (SMOKE_RIDER,)).rowcount
        conn.execute("UPDATE riders SET state='sleep' WHERE id=?", (SMOKE_RIDER,))
    return {"ok": True, "deleted": n}


# ---------------------------------------------------------------- pruning live state

def prune() -> dict:
    """Live state only. Never touches the outbox: that leaves only on the Mini's ack."""
    closed_cutoff = ago(hours=CLOSED_TRIP_KEEP_H)
    stats = {}
    with tx() as conn:
        old = [r["id"] for r in conn.execute(
            "SELECT id FROM trips WHERE ended_at IS NOT NULL AND ended_at < ?"
            " AND id NOT IN (SELECT trip_id FROM incidents WHERE trip_id IS NOT NULL"
            " AND state IN ('pending','sos'))", (closed_cutoff,)).fetchall()]
        stats["trips"] = len(old)
        stats["positions"] = 0
        for tid in old:
            stats["positions"] += conn.execute("DELETE FROM positions WHERE trip_id=?", (tid,)).rowcount
            conn.execute("DELETE FROM incidents WHERE trip_id=?", (tid,))
            conn.execute("DELETE FROM trips WHERE id=?", (tid,))
        stats["positions"] += conn.execute(
            "DELETE FROM positions WHERE trip_id IS NULL AND received_at < ?",
            (ago(minutes=TELEMETRY_WINDOW_MIN),)).rowcount
        conn.execute("DELETE FROM incidents WHERE trip_id IS NULL AND state IN ('retracted','resolved','closed')"
                     " AND raised_at < ?", (closed_cutoff,))
        stats["events"] = conn.execute("DELETE FROM events WHERE ts < ?", (ago(hours=EVENT_KEEP_H),)).rowcount
        conn.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (ago(days=1),))
    return stats


async def prune_forever() -> None:
    while True:
        try:
            stats = await asyncio.to_thread(prune)
            if any(stats.values()):
                print(f"{now()} info  prune      -             {stats}", flush=True)
        except Exception as e:  # never let pruning take the relay down
            print(f"{now()} warn  prune      -             failed: {e!r}", flush=True)
        await asyncio.sleep(PRUNE_EVERY_S)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", server_header=False)


if __name__ == "__main__":
    main()
