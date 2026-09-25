"""
moto-tracker server — Phase 2 skeleton.

Runs under launchd so it survives a reboot (design.md: the Mini is on a UPS
precisely so this stays up). The dashboard it serves is a control panel, not the
host — stopping the process is done through launchctl via ./motoctl, so an
unattended restart brings the server back on its own.

Binding: two explicit sockets, the Meshnet address and localhost. Not 0.0.0.0 and
explicitly not the LAN address, per design.md — anything on the home network could
otherwise reach it, and the mesh already provides the access control the design
relies on. Localhost is needed on top of the mesh address because the Mini cannot
connect to its own Meshnet IP (confirmed 2026-09-08), so without it the dashboard
would be unreachable from the very machine it runs on.
"""
import asyncio
import json
from contextlib import asynccontextmanager
import os
import threading
import socket
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

HERE = Path(__file__).parent
DB = HERE / "moto.db"
MESH_IP = "100.64.0.10"
# Sustained speed above this while OffBike resumes riding automatically, so a
# rider pulling away from a stop without pressing Start Ride is not left
# unmonitored. Corrected from 20 to 10 on 2026-09-10.
AUTO_RESUME_KMH = 10
# How much recent telemetry a newly-opened dashboard replays. This is a *view*
# window, not a retention policy — nothing is deleted. Retention is Phase 7.
TELEMETRY_WINDOW_MIN = 60
PORT = 8088

STARTED = time.time()
_subscribers: "list[asyncio.Queue]" = []


def now() -> str:
    """
    Stored timestamps stay UTC — unambiguous, and the phone sends offset-aware ISO
    so the two can be compared without guesswork. Anything a human reads is
    rendered in local time instead: see local() below and the dashboard.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def local(iso: str) -> str:
    """UTC ISO string -> local wall-clock, for logs and the dashboard."""
    try:
        return (datetime.fromisoformat(iso).astimezone()
                .strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
    except ValueError:
        return iso


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


RIDER_COLUMNS = {"state": "TEXT NOT NULL DEFAULT 'sleep'"}

SENSOR_COLUMNS = {
    "min_g": "REAL", "mean_g": "REAL", "rms_g": "REAL",
    "peak_rot": "REAL", "mean_rot": "REAL",
    "accel_n": "INTEGER", "gyro_n": "INTEGER",
}


def migrate(conn) -> None:
    """rides -> trips, ride_id -> trip_id, positions.state. Renames in place so
    existing rows survive; schema.sql only creates what is missing."""
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "rides" in tables and "trips" not in tables:
        conn.execute("ALTER TABLE rides RENAME TO trips")
        print("migrated: rides -> trips", flush=True)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(positions)")}
    if "ride_id" in cols and "trip_id" not in cols:
        conn.execute("ALTER TABLE positions RENAME COLUMN ride_id TO trip_id")
        print("migrated: positions.ride_id -> trip_id", flush=True)
    if "state" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN state TEXT")
        print("migrated: positions.state", flush=True)
    icols = {r["name"] for r in conn.execute("PRAGMA table_info(incidents)")}
    if "ride_id" in icols and "trip_id" not in icols:
        conn.execute("ALTER TABLE incidents RENAME COLUMN ride_id TO trip_id")
        print("migrated: incidents.ride_id -> trip_id", flush=True)


def init_db() -> None:
    with db() as conn:
        migrate(conn)
        conn.executescript((HERE / "schema.sql").read_text())
        # Additive migration: schema.sql is CREATE TABLE IF NOT EXISTS, so it
        # cannot widen an existing positions table. Add what is missing.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(positions)")}
        for col, typ in SENSOR_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {typ}")
                print(f"migrated: positions.{col}", flush=True)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(riders)")}
        for col, typ in RIDER_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE riders ADD COLUMN {col} {typ}")
                print(f"migrated: riders.{col}", flush=True)


def _push(ts: str, level: str, tag: str, message: str, rider_id: Optional[str]) -> None:
    payload = json.dumps({"ts": ts, "level": level, "tag": tag,
                          "rider": rider_id, "message": message})
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def log(level: str, tag: str, message: str, rider_id: Optional[str] = None) -> None:
    """A notable event: stored, streamed, and replayed to a dashboard on connect."""
    ts = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO events (ts, level, tag, rider_id, message) VALUES (?,?,?,?,?)",
            (ts, level, tag, rider_id, message),
        )
    _push(ts, level, tag, message, rider_id)
    print(f"{local(ts)} {level:5} {tag:10} {rider_id or '-':8} {message}", flush=True)


def telemetry(message: str, rider_id: str) -> None:
    """
    A position tick: streamed live, and replayed from the positions table for the
    last TELEMETRY_WINDOW_MIN minutes when a dashboard connects.

    Not written to the events table — the positions row already is the record, and
    duplicating 17k rows a day would bury every real event in heartbeat noise. The
    dashboard can mute these; it cannot mute an incident.
    """
    _push(now(), "tick", "position", message, rider_id)


app = FastAPI(title="moto-tracker", docs_url=None, redoc_url=None)
# lifespan assigned below, once it is defined


# ---------------------------------------------------------------- models

class RiderRef(BaseModel):
    rider: str


class Position(BaseModel):
    rider: str
    ts: Optional[str] = None
    lat: float
    lon: float
    accuracy: Optional[float] = None
    speed: Optional[float] = None
    battery: Optional[int] = None
    # Sensor summary for the window since the previous tick. g is resultant
    # acceleration / 9.81, so ~1.0 at rest. rot is gyroscope magnitude in rad/s.
    peak_g: Optional[float] = None
    min_g: Optional[float] = None
    mean_g: Optional[float] = None
    rms_g: Optional[float] = None
    peak_rot: Optional[float] = None
    mean_rot: Optional[float] = None
    # Sample counts. A window that should hold ~250 samples arriving with 20 means
    # the OS throttled the sensor, which the g values alone would never reveal.
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


class Retract(BaseModel):
    rider: str
    incident_id: int


class Resolve(BaseModel):
    # No `by` field: only the rider can resolve, so there is nothing to choose.
    # The Observer can silence the alarm and nothing more (design.md 2026-09-11).
    incident_id: int
    rider: str
    resolution: str      # ok | false_alarm


# ---------------------------------------------------------------- helpers

def active_trip(conn, rider: str):
    """The open trip. Spans every riding and off-bike stretch until End trip."""
    return conn.execute(
        "SELECT * FROM trips WHERE rider_id=? AND ended_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (rider,)
    ).fetchone()


def last_trip(conn, rider: str):
    return conn.execute(
        "SELECT * FROM trips WHERE rider_id=? ORDER BY id DESC LIMIT 1", (rider,)
    ).fetchone()


def rider_state(conn, rider: str) -> str:
    r = conn.execute("SELECT state FROM riders WHERE id=?", (rider,)).fetchone()
    return r["state"] if r else "sleep"


def set_state(conn, rider: str, state: str) -> None:
    conn.execute("UPDATE riders SET state=? WHERE id=?", (state, rider))


def require_rider(conn, rider: str):
    if not conn.execute("SELECT 1 FROM riders WHERE id=?", (rider,)).fetchone():
        raise HTTPException(404, f"unknown rider {rider!r}")


# ---------------------------------------------------------------- ride control

@app.post("/trip/start")
@app.post("/ride/start")
def ride_start(r: RiderRef, auto: bool = False):
    """
    Riding begins. Opens a trip if none is open; otherwise resumes riding inside
    the trip already running. Coming back from a stop does **not** start a new
    trip — the stop was part of it.

    Two ways in: the rider taps Start, or the phone sees sustained motion above
    AUTO_RESUME_KMH while off the bike and calls this with auto=true.
    """
    with db() as conn:
        require_rider(conn, r.rider)
        trip = active_trip(conn, r.rider)
        if trip:
            conn.execute("UPDATE trips SET state='riding' WHERE id=?", (trip["id"],))
            set_state(conn, r.rider, "riding")
            tid, opened = trip["id"], False
        else:
            cur = conn.execute(
                "INSERT INTO trips (rider_id, started_at, state) VALUES (?,?,'riding')",
                (r.rider, now()),
            )
            tid, opened = cur.lastrowid, True
            set_state(conn, r.rider, "riding")
    log("info", "trip",
        (f"TRIP #{tid} started" if opened else f"riding resumed in trip #{tid}")
        + (" (auto-resume)" if auto else ""), r.rider)
    return {"trip_id": tid, "state": "riding", "trip_opened": opened}


@app.post("/ride/offbike")
@app.post("/ride/end")
def ride_offbike(r: RiderRef):
    """
    Arrived, or making a stop. The **trip stays open** and telemetry keeps
    reporting — an hour at a destination is part of the trip, not the end of it.
    Only End trip closes a trip.
    """
    with db() as conn:
        require_rider(conn, r.rider)
        trip = active_trip(conn, r.rider)
        if trip:
            conn.execute("UPDATE trips SET state='offbike' WHERE id=?", (trip["id"],))
        set_state(conn, r.rider, "offbike")
    log("info", "trip", "off the bike — trip continues, telemetry still reporting", r.rider)
    return {"trip_id": trip["id"] if trip else None, "state": "offbike"}


@app.post("/trip/end")
@app.post("/ride/sleep")
def trip_end(r: RiderRef):
    """
    End trip. Pressed on reaching home: closes the trip *and* puts the app to
    sleep, which is the same event. Sleep halts telemetry and background usage
    until the next trip begins.
    """
    with db() as conn:
        require_rider(conn, r.rider)
        was = rider_state(conn, r.rider)
        trip = active_trip(conn, r.rider)
        if trip:
            conn.execute("UPDATE trips SET ended_at=?, state='sleep' WHERE id=?",
                         (now(), trip["id"]))
        set_state(conn, r.rider, "sleep")
    if trip and was == "riding":
        # Allowed — riding into the driveway and pressing End trip is the normal
        # case, and refusing it would force a pointless Arrived tap first. Logged
        # at warn because ending a trip straight from riding is also what an
        # accidental press looks like, and monitoring stops either way.
        log("warn", "trip", f"TRIP #{trip['id']} ended directly from RIDING — "
                            f"telemetry stops now", r.rider)
    else:
        log("info", "trip",
            f"TRIP #{trip['id']} ended — asleep" if trip else "sleep (no trip open)",
            r.rider)
    return {"trip_id": trip["id"] if trip else None, "state": "sleep"}


# ---------------------------------------------------------------- telemetry

@app.post("/position")
def position(p: Position):
    """
    Every packet belongs to the open trip, on-bike or off. Off-bike telemetry is
    trip data — an hour at a destination is part of the trip — and is tagged with
    the state at the time so the two can still be told apart.
    """
    with db() as conn:
        require_rider(conn, p.rider)
        trip = active_trip(conn, p.rider)
        st = rider_state(conn, p.rider)
        conn.execute(
            "INSERT INTO positions (trip_id, rider_id, state, ts, received_at,"
            " lat, lon, accuracy, speed, battery, peak_g, decel, min_g, mean_g,"
            " rms_g, peak_rot, mean_rot, accel_n, gyro_n)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trip["id"] if trip else None, p.rider, st, p.ts or now(), now(),
             p.lat, p.lon, p.accuracy, p.speed, p.battery, p.peak_g, p.decel,
             p.min_g, p.mean_g, p.rms_g, p.peak_rot, p.mean_rot, p.accel_n, p.gyro_n),
        )
    # A packet whose phone timestamp lags well behind its arrival was held in the
    # phone's queue through a signal gap and pushed when the mesh came back.
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
        sens = (f"  g {p.peak_g:.2f}/{p.mean_g or 0:.2f}"
                f"  rot {p.peak_rot or 0:.2f}"
                f"  n {p.accel_n or 0}/{p.gyro_n or 0}")
    telemetry(
        f"{p.lat:.5f}, {p.lon:.5f}  {p.speed or 0:.0f} km/h  "
        f"±{p.accuracy or 0:.0f} m  batt {p.battery if p.battery is not None else '?'}%"
        f"{sens}{late}",
        p.rider,
    )
    return {"ok": True}


@app.post("/stop-answer")
def stop_answer(a: StopAnswer):
    """
    Answers to the stop prompt. Two of the three are state transitions and one is
    a suppression — see design.md.
    """
    if a.answer not in {"arrived", "traffic", "help"}:
        raise HTTPException(400, "answer must be arrived | traffic | help")
    with db() as conn:
        require_rider(conn, a.rider)
        trip = active_trip(conn, a.rider)
        if not trip:
            raise HTTPException(409, "no trip in progress")
        if a.answer == "arrived":
            # Off the bike. The trip continues — only End trip closes one.
            conn.execute("UPDATE trips SET state='offbike' WHERE id=?", (trip["id"],))
            set_state(conn, a.rider, "offbike")
        elif a.answer == "help":
            conn.execute(
                "INSERT INTO incidents (trip_id, rider_id, raised_at, kind, state)"
                " VALUES (?,?,?,'help','sos')", (trip["id"], a.rider, now()),
            )
    if a.answer == "help":
        log("alert", "incident", "RIDER PRESSED I NEED HELP", a.rider)
    else:
        log("info", "stop", f"answered: {a.answer}", a.rider)
    return {"ok": True, "answer": a.answer}


# ---------------------------------------------------------------- incidents

@app.post("/incident/candidate")
def candidate(c: Candidate):
    """
    A high-G candidate, sent the instant it is detected. Not an alarm yet — but
    the server escalates it unless a retraction arrives, so the default is alarm
    and the phone must actively talk it down.
    """
    escalate_at = datetime.fromtimestamp(
        time.time() + c.confirm_window_s, timezone.utc
    ).isoformat(timespec="milliseconds")
    with db() as conn:
        require_rider(conn, c.rider)
        trip = active_trip(conn, c.rider)
        cur = conn.execute(
            "INSERT INTO incidents (trip_id, rider_id, raised_at, kind, state,"
            " escalate_at, evidence) VALUES (?,?,?,'candidate','pending',?,?)",
            (trip["id"] if trip else None, c.rider, now(), escalate_at,
             json.dumps(c.evidence or {})),
        )
        iid = cur.lastrowid
    log("warn", "incident", f"candidate #{iid} raised, escalates in {c.confirm_window_s}s", c.rider)
    return {"incident_id": iid, "escalate_at": escalate_at}


@app.post("/incident/retract")
def retract(r: Retract):
    """Retraction must be acknowledged — the phone retries until it is."""
    with db() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id=?", (r.incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such incident")
        if row["state"] == "sos":
            log("warn", "incident", f"retraction for #{r.incident_id} arrived too late", r.rider)
            return {"ok": False, "state": "sos", "note": "already escalated"}
        conn.execute("UPDATE incidents SET state='retracted' WHERE id=?", (r.incident_id,))
    log("info", "incident", f"candidate #{r.incident_id} retracted", r.rider)
    return {"ok": True, "state": "retracted"}


@app.post("/incident/resolve")
def resolve(r: Resolve):
    """
    Only the rider of that incident may close it — enforced here and not merely
    in the UI, because it is the design and not a convenience. An Observer who
    could resolve after a reassuring phone call would turn *non-response is the
    alarm* into *someone else's response*, and a reassuring call is exactly what
    a concussed rider is most likely to give.
    """
    if r.resolution not in {"ok", "false_alarm"}:
        raise HTTPException(400, "resolution must be ok | false_alarm")
    with db() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id=?", (r.incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such incident")
        if row["rider_id"] != r.rider:
            log("warn", "incident",
                f"#{r.incident_id} resolve REFUSED — {r.rider} is not the rider", row["rider_id"])
            raise HTTPException(403, "only the rider can resolve their own incident")
        conn.execute(
            "UPDATE incidents SET state='resolved', resolved_at=?, resolved_by='rider',"
            " resolution=? WHERE id=?", (now(), r.resolution, r.incident_id),
        )
    log("info", "incident", f"#{r.incident_id} resolved by rider: {r.resolution}", row["rider_id"])
    return {"ok": True}


# ---------------------------------------------------------------- read

@app.get("/state")
def state():
    out = {}
    with db() as conn:
        for rider in conn.execute("SELECT * FROM riders").fetchall():
            trip = active_trip(conn, rider["id"])
            last = conn.execute(
                "SELECT * FROM positions WHERE rider_id=? ORDER BY id DESC LIMIT 1",
                (rider["id"],),
            ).fetchone()
            openinc = conn.execute(
                "SELECT id, kind, state FROM incidents WHERE rider_id=?"
                " AND state IN ('pending','sos') ORDER BY id DESC LIMIT 1",
                (rider["id"],),
            ).fetchone()
            # Instantaneous values alone are useless for spotting an event: by the
            # time anyone looks at the pane the spike is several ticks in the past.
            # Carry the ride's high-water marks alongside them.
            # Scope to a ride, never to a rolling clock window: a high-water mark
            # belongs to the ride that produced it. With nothing open, report the
            # last completed ride and say so, so a new ride starts clean.
            prev = last_trip(conn, rider["id"])
            scope_trip, scope_label = (
                (trip["id"], "trip") if trip
                else (prev["id"] if prev else -1, "last trip")
            )
            scope = ("trip_id = ?", (scope_trip,))
            peaks = conn.execute(
                f"SELECT MAX(peak_g) mg, MAX(peak_rot) mr, MAX(mean_g) mmg"
                f" FROM positions WHERE {scope[0]}", scope[1]
            ).fetchone()
            # On-bike only: a phone jostled in a bag at a cafe is trip data but
            # says nothing about riding, and would otherwise distort the peak.
            onbike = conn.execute(
                f"SELECT MAX(peak_g) mg, MAX(peak_rot) mr"
                f" FROM positions WHERE {scope[0]} AND state='riding'", scope[1]
            ).fetchone()
            when = None
            if peaks and peaks["mg"] is not None:
                w = conn.execute(
                    f"SELECT received_at FROM positions WHERE {scope[0]}"
                    f" AND peak_g = ? ORDER BY id DESC LIMIT 1",
                    scope[1] + (peaks["mg"],)
                ).fetchone()
                when = w["received_at"] if w else None

            age = None
            if last:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(last["received_at"])).total_seconds()
            out[rider["id"]] = {
                "name": rider["name"],
                "mesh_ip": rider["mesh_ip"],
                "state": rider_state(conn, rider["id"]),
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
                "max_g_riding": onbike["mg"] if onbike else None,
                "max_rot_riding": onbike["mr"] if onbike else None,
                "peaks_scope": scope_label,
                "lat": last["lat"] if last else None,
                "lon": last["lon"] if last else None,
                "incident": dict(openinc) if openinc else None,
            }
    return out


@app.get("/health")
def health():
    with db() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("trips", "positions", "incidents", "events")}
    return {
        "ok": True,
        "uptime_s": round(time.time() - STARTED, 1),
        "db_bytes": DB.stat().st_size if DB.exists() else 0,
        "python": sys.version.split()[0],
        "counts": counts,
    }


@app.get("/events")
async def events():
    """Server-sent events: the live log the dashboard reads."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.append(q)

    async def stream():
        try:
            # Backlog is events AND ticks merged by timestamp. They used to be
            # replayed as two separate blocks — every event first, then every
            # tick — which made a trip that ended at 14:14 appear above the
            # telemetry from 14:08 and read as if the clock had gone backwards.
            backlog = []
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(minutes=TELEMETRY_WINDOW_MIN)).isoformat(timespec="milliseconds")
            with db() as conn:
                for row in conn.execute(
                    "SELECT ts, level, tag, rider_id, message FROM events"
                    " ORDER BY id DESC LIMIT 50"
                ).fetchall():
                    backlog.append((row["ts"], {
                        "ts": row["ts"], "level": row["level"], "tag": row["tag"],
                        "rider": row["rider_id"], "message": row["message"]}))
                for t in conn.execute(
                    "SELECT received_at, rider_id, lat, lon, speed, accuracy, battery,"
                    " peak_g, mean_g, peak_rot, accel_n, gyro_n"
                    " FROM positions WHERE received_at >= ? ORDER BY id", (cutoff,)
                ).fetchall():
                    sens = ""
                    if t["peak_g"] is not None:
                        sens = (f"  g {t['peak_g']:.2f}/{t['mean_g'] or 0:.2f}"
                                f"  rot {t['peak_rot'] or 0:.2f}"
                                f"  n {t['accel_n'] or 0}/{t['gyro_n'] or 0}")
                    backlog.append((t["received_at"], {
                        "ts": t["received_at"], "level": "tick", "tag": "position",
                        "rider": t["rider_id"],
                        "message": (f"{t['lat']:.5f}, {t['lon']:.5f}  "
                                    f"{t['speed'] or 0:.0f} km/h  "
                                    f"\u00b1{t['accuracy'] or 0:.0f} m  "
                                    f"batt {t['battery'] if t['battery'] is not None else '?'}%"
                                    f"{sens}")}))
            backlog.sort(key=lambda x: x[0])
            for _, payload in backlog:
                yield "data: " + json.dumps(payload) + "\n\n"

            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {item}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if q in _subscribers:
                _subscribers.remove(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.post("/spike/log")
async def spike_log(request: Request, build: str = "?", rider: str = "?"):
    """
    Phase 1 support: the endurance spike uploading its on-device log.

    Temporary — it goes away with the spike once the gate is passed. It exists
    because the on-device log is the ground truth for throttling, and it needs
    somewhere to land that is not the phone.
    """
    body = (await request.body()).decode("utf-8", "replace")
    outdir = HERE / "spike-logs"
    outdir.mkdir(exist_ok=True)
    name = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{rider}-{build}.log"
    (outdir / name).write_text(body)
    log("info", "spike", f"log uploaded: {name} ({len(body)} bytes)", rider)
    return {"ok": True, "saved": name}


@app.post("/admin/shutdown")
def shutdown(request: Request):
    """
    The dashboard's Stop button. Localhost only — a rider's phone must never be
    able to kill the server it depends on, and the mesh socket is what the phones
    reach us on.
    """
    client = request.client.host if request.client else "?"
    if client not in ("127.0.0.1", "::1"):
        log("warn", "admin", f"shutdown refused from {client}")
        raise HTTPException(403, "shutdown is localhost only")
    log("warn", "server", "shutdown requested from dashboard")

    def die():
        time.sleep(0.4)
        os._exit(0)

    threading.Thread(target=die, daemon=True).start()
    return {"ok": True, "note": "stopping; run ./motoctl start to bring it back"}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return (HERE / "dashboard.html").read_text()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    log("info", "server", f"started on {MESH_IP}:{PORT} and 127.0.0.1:{PORT}")
    yield
    log("info", "server", "stopped")


app.router.lifespan_context = lifespan


def main() -> None:
    import uvicorn

    socks = []
    for host in (MESH_IP, "127.0.0.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, PORT))
        except OSError as e:
            print(f"could not bind {host}:{PORT} — {e}", file=sys.stderr)
            s.close()
            continue
        s.listen(128)
        s.setblocking(False)
        socks.append(s)
    if not socks:
        sys.exit("no sockets bound, refusing to start")

    config = uvicorn.Config(app, log_level="warning")
    uvicorn.Server(config).run(sockets=socks)


if __name__ == "__main__":
    main()
