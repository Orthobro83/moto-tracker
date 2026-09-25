"""
moto-tracker monitor — the Mac Mini's GUI for the relay (design.md 2026-09-15).

Runs in the background whether or not the window is open: it holds the archive,
sounds the alarm and keeps the menu-bar icon informed. The window is a WKWebView
in moto-tracker.app pointed at this server.

Everything it serves is on 127.0.0.1 — nothing on the Mini listens on the network.

Threads:
  state    /state from the relay every second (also the ping and the Live light)
  events   /events as a stream: the running log and packets-per-minute
  sync     /sync -> archive -> /sync/ack every five seconds
  weather  Open-Meteo near the active rider every ten minutes
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import alarm as alarm_mod          # noqa: E402
import archive                     # noqa: E402
import baselines as baselines_mod  # noqa: E402
import config                      # noqa: E402
import tiles as tiles_mod          # noqa: E402
import weather as weather_mod      # noqa: E402
from devices import Devices        # noqa: E402
from relayclient import Relay      # noqa: E402

VERSION = "monitor-5"
LOG_LINES = 600            # the running log the UI can scroll back through
# How many polls must fail before the relay counts as unreachable. Polls are a second
# apart, so this is a three-second grace — long enough to ride out the source-address
# rotation that breaks every connection in flight, short enough to notice a real outage.
MISSES_BEFORE_DOWN = 3
# An alarm nobody is there to silence should not sound all afternoon. After this it
# goes quiet HERE only — the incident stays open, the icon keeps flashing, and the
# relay still shows it unsilenced, because nobody has actually acknowledged it.
ALARM_MAX_S = 900
# Weather this old is no longer worth showing. On 2026-09-20 the pane still had
# a reading from eleven hours earlier: every fetch since had failed, and nothing
# said so (Open-Meteo throttles, and this Mac goes out through a shared address).
WEATHER_STALE_S = 2 * 3600
DRILL_CONFIRM_S = 6        # how long a drill shows as "checking" before it escalates
DRILL_MAX_S = 600          # a drill can never be left running longer than this
ALLOWED_ORIGINS = {f"http://127.0.0.1:{config.PORT}", f"http://localhost:{config.PORT}"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse(iso: Optional[str]):
    try:
        return datetime.fromisoformat(iso) if iso else None
    except (ValueError, TypeError):
        return None


class Monitor:
    def __init__(self):
        self.relay = Relay(config.RELAY_URL, config.KEY_FILE, config.CA_FILE)
        self.devices = Devices(config.SSH_KEY, config.SSH_TARGET, config.DEVICES_ENABLED)
        self.tiles = tiles_mod.Tiles(config.TOMTOM_KEY_FILE, config.TILE_CACHE)
        archive.migrate(config.ARCHIVE)
        self.db = archive.connect(config.ARCHIVE)
        self.db_lock = threading.Lock()
        self.alarm = alarm_mod.Alarm(
            alarm_mod.build_sound(config.SUPPORT / "monitor-runtime/alarm.wav"),
            mute=config.ALARM_MUTE,
            open_app=config.APP_PATH if config.OPEN_APP else None)

        self.lock = threading.Lock()
        self.state: dict = {}
        self.server = {"up": False, "ping_ms": None, "checked_at": None, "error": None}
        self.log = deque(maxlen=LOG_LINES)
        self.log_seq = 0
        self.ticks = deque(maxlen=600)          # position arrivals, for packets/minute
        self.stream_up = False
        self.weather: Optional[dict] = None
        # Each rider's own weather: with both out at once, each pane shows what its
        # rider is riding in (Jack, 2026-09-24).
        self.weather_by: dict = {}
        self.sync_status = {"last_seq": 0, "pending": None, "at": None, "error": None}
        # Pairing and role. A second Mac (Dana's) pairs with a code like a phone
        # does, and is a viewer: it sees everything and can act as an Observer, but
        # exactly one Mac consumes the outbox and keeps the history.
        self.paired = self.relay.has_key()
        self.key_rejected = False
        self.archivist: Optional[bool] = None
        self.archivist_checked = 0.0
        self.seen_incidents: dict = {}
        self.drill: Optional[dict] = None
        self.baselines: dict = {}
        self.baselines_at = 0.0
        self.attention: Optional[str] = None
        self.misses = 0
        self.down_since: Optional[float] = None
        self.last_trip_cache: dict = {}
        self.stop = threading.Event()

    # ---------------------------------------------------------------- helpers

    def add_log(self, entry: dict) -> None:
        with self.lock:
            self.log_seq += 1
            entry = dict(entry)
            entry["seq"] = self.log_seq
            self.log.append(entry)
            if entry.get("tag") == "position":
                self.ticks.append(time.time())

    def note(self, level: str, message: str, rider: Optional[str] = None, tag: str = "monitor") -> None:
        self.add_log({"ts": now(), "level": level, "tag": tag, "rider": rider, "message": message})

    def packets_per_minute(self) -> int:
        cutoff = time.time() - 60
        with self.lock:
            return sum(1 for t in self.ticks if t >= cutoff)

    # ---------------------------------------------------------------- threads

    def poll_state(self) -> None:
        while not self.stop.is_set():
            if not self.relay.has_key():
                self.paired = False
                self.stop.wait(2.0)
                continue
            status, body, ms = self.relay.call("GET", "/state", timeout=8)
            if status == 401:
                # The key was revoked, or this Mac was never paired. Say so plainly.
                self.paired, self.key_rejected = True, True
                with self.lock:
                    self.server = {"up": False, "ping_ms": None, "checked_at": now(),
                                   "error": "this Mac's key was rejected"}
                self.stop.wait(5.0)
                continue
            self.paired, self.key_rejected = True, False
            if status == 200 and isinstance(body, dict):
                if self.down_since is not None:
                    self.note("info", f"relay back after {time.time() - self.down_since:.0f} s",
                              tag="server")
                    self.down_since = None
                self.misses = 0
                body = self.apply_drill(body)
                with self.lock:
                    self.state = body
                    self.server = {"up": True, "ping_ms": round(ms), "checked_at": now(), "error": None}
                self.follow_incidents(body)
            else:
                # One failed poll is not an outage. The Mini's route to the VPS is
                # carried by a tunnel whose source address rotates through a pool —
                # 14 different addresses in a day — and every rotation kills the
                # connections in flight. That costs one poll and heals itself, so it
                # is not worth a red light or a line in the log (2026-09-16).
                self.misses += 1
                if self.misses < MISSES_BEFORE_DOWN:
                    self.stop.wait(config.STATE_EVERY_S)
                    continue
                with self.lock:
                    was_up = self.server["up"]
                    self.server = {"up": False, "ping_ms": None, "checked_at": now(),
                                   "error": f"HTTP {status}" if status else "unreachable"}
                if was_up:
                    self.down_since = time.time()
                    self.note("warn", f"relay unreachable — {self.misses} polls in a row",
                              tag="server")
            self.stop.wait(config.STATE_EVERY_S)

    def follow_incidents(self, state: dict) -> None:
        """The alarm follows the relay, never a local guess: silence is shared, so a
        silence on the Observer phone stops the Mini's alarm on the next poll."""
        sounding, attention = False, None
        for rider, s in state.items():
            inc = s.get("incident")
            if not inc:
                continue
            # Fifteen minutes is long enough for anyone in the house to have heard it.
            raised = _parse(inc.get("raised_at"))
            if raised and (datetime.now(timezone.utc) - raised).total_seconds() > ALARM_MAX_S:
                if self.alarm.sounding and self.alarm.reason == "incident":
                    self.note("warn", f"alarm quieted here after {ALARM_MAX_S // 60} minutes —"
                                      f" incident #{inc['id']} is still open and unsilenced",
                              rider, tag="alarm")
                self.alarm.set(False)
                if inc["display"] == "red":
                    attention = "red"
                elif attention != "red":
                    attention = inc["display"]
                continue
            key = inc["id"]
            before = self.seen_incidents.get(key)
            self.seen_incidents[key] = inc
            if inc["state"] == "sos" and not inc.get("silenced_at"):
                sounding = True
            if inc["display"] == "red":
                attention = "red"
            elif inc["display"] == "yellow" and attention != "red":
                attention = "yellow"
            elif inc["display"] == "pending" and attention is None:
                attention = "amber"
            # A new incident, or one that just escalated, brings the Mini to life.
            if inc["state"] == "sos" and (before is None or before["state"] != "sos"):
                self.alarm.attention()
        # This follower owns the INCIDENT alarm and nothing else. Calling set(False)
        # unconditionally here is what made a test alarm stop after about a second:
        # the next poll silenced it. A test stops when it is stopped.
        if sounding:
            self.alarm.set(True, "incident")
        elif self.alarm.reason == "incident":
            self.alarm.set(False)
        with self.lock:
            self.attention = attention

    # ---------------------------------------------------------------- the drill

    def start_drill(self, rider: str) -> dict:
        """A rehearsal: a fabricated incident fed into this Mac exactly where a real
        one arrives, so the alarm, the flashing icon, the red pane, the box and its
        counters all behave for real. Nothing is sent to the relay, nothing is
        archived, and no rider is ever told anything."""
        with self.lock:
            state = dict(self.state)
        if rider not in state:
            raise HTTPException(404, f"no rider {rider!r}")
        if any(r.get("incident") for r in state.values()):
            raise HTTPException(409, "there is a real incident open — not now")
        if any(r.get("trip_id") for r in state.values()):
            raise HTTPException(409, "someone is riding — a drill would hide the real screen")
        if self.drill:
            return self.drill_view()
        seen = state.get(rider) or {}
        started = datetime.now(timezone.utc)
        self.drill = {
            "id": -int(time.time()),            # negative: it can never be a relay id
            "rider": rider,
            "started": started,
            "raised_at": started.isoformat(timespec="milliseconds"),
            "escalate_at": (started + timedelta(seconds=DRILL_CONFIRM_S)).isoformat(timespec="milliseconds"),
            "silenced_at": None, "silenced_by": None,
            "lat": seen.get("lat") if seen.get("lat") is not None else 13.6929,
            "lon": seen.get("lon") if seen.get("lon") is not None else -89.2182,
            "speed_before": seen.get("speed") or 78.0,
            "g": 9.8, "rot": 6.4,
        }
        self.note("warn", f"DRILL — a practice incident was raised for {seen.get('name', rider)}; "
                          f"nothing was sent to the relay", rider, tag="drill")
        return self.drill_view()

    def stop_drill(self, why: str = "ended") -> None:
        drill = self.drill
        self.drill = None
        if not drill:
            return
        self.seen_incidents.pop(drill["id"], None)
        # Stop the menu-bar icon flashing now, not on the next poll. The last poll
        # left the drill in the stored state, so take it out of there too.
        with self.lock:
            for rider, seen in list(self.state.items()):
                inc = seen.get("incident")
                if inc and inc.get("id", 0) < 0:
                    self.state[rider] = dict(seen, incident=None)
            if not any(r.get("incident") for r in self.state.values()):
                self.attention = None
        self.note("info", f"DRILL {why}", drill["rider"], tag="drill")

    def drill_view(self) -> Optional[dict]:
        """The same shape the relay gives for a real incident, so every screen, counter
        and button works on it unchanged."""
        d = self.drill
        if not d:
            return None
        age = (datetime.now(timezone.utc) - d["started"]).total_seconds()
        if age > DRILL_MAX_S:
            self.stop_drill("ended by itself after ten minutes")
            return None
        escalated = age >= DRILL_CONFIRM_S
        return {
            "id": d["id"], "kind": "candidate", "drill": True,
            "state": "sos" if escalated else "pending",
            "display": "red" if escalated else "pending",
            "raised_at": d["raised_at"], "escalate_at": d["escalate_at"],
            "rider_closed_at": None, "rider_resolution": None,
            "observer_closed_at": None, "observer_closed_by": None,
            "silenced_at": d["silenced_at"], "silenced_by": d["silenced_by"],
            "last_position": {"lat": d["lat"], "lon": d["lon"], "accuracy": 6.0, "at": d["raised_at"]},
            "speed_before": d["speed_before"], "speed_after": 0.0,
            "stationary_since": d["raised_at"],
            "g": d["g"], "rot": d["rot"],
        }

    def apply_drill(self, state: dict) -> dict:
        """Puts the drill where a real incident would be, before anything reads it."""
        view = self.drill_view()
        if view:
            rider = self.drill["rider"]
            if rider in state:
                state[rider] = dict(state[rider], incident=view)
        return state

    def consume_events(self) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            try:
                for payload in self.relay.stream("/events"):
                    if self.stop.is_set():
                        return
                    self.stream_up = True
                    backoff = 1.0
                    self.add_log(payload)
            except Exception:
                pass
            self.stream_up = False
            if self.stop.wait(backoff):
                return
            backoff = min(backoff * 2, 15)

    def sync_once(self) -> dict:
        status, body, _ = self.relay.call("GET", "/sync?after=%d&limit=500" % self.cursor(), timeout=30)
        if status == 403:
            # Another Mac keeps the history. Checked again now and then, in case it moves.
            if self.archivist is not False:
                self.note("info", "this Mac is a viewer — another Mac keeps the history", tag="archive")
            self.archivist = False
            self.archivist_checked = time.time()
            self.sync_status.update({"error": None, "at": now(), "pending": 0})
            return {"ok": True, "stored": 0}
        if status == 200:
            self.archivist = True
        if status != 200 or not isinstance(body, dict):
            self.sync_status["error"] = f"HTTP {status}" if status else "unreachable"
            return {"ok": False}
        entries = body.get("entries") or []
        if not entries:
            self.sync_status.update({"error": None, "at": now(), "pending": 0})
            return {"ok": True, "stored": 0}
        with self.db_lock:
            result = archive.apply(self.db, entries)
            for payload in result["logs"]:
                path = archive.write_phone_log(payload, config.LOGS)
                self.note("info", f"phone log stored: {path.name}", payload.get("rider"), tag="archive")
            for trip_id in result["closed_trips"]:
                archive.write_trip_log(self.db, trip_id, config.LOGS)
        # Only now may the relay drop them.
        top = entries[-1]["seq"]
        status, _, _ = self.relay.call("POST", "/sync/ack", {"upto": top}, timeout=15)
        if status != 200:
            self.sync_status["error"] = f"ack failed: HTTP {status}"
            return {"ok": False, "stored": len(entries)}
        self.sync_status.update({"last_seq": top, "error": None, "at": now(),
                                 "pending": 1 if body.get("more") else 0})
        if "trip" in result["counts"]:
            # A trip that just ended changes what the idle pane must show.
            self.last_trip_cache.clear()
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(result["counts"].items()))
        if any(k != "position" for k in result["counts"]):
            self.note("info", f"archived {len(entries)} ({kinds})", tag="archive")
        return {"ok": True, "stored": len(entries), "more": bool(body.get("more"))}

    def sync_forever(self) -> None:
        last_prune = 0.0
        while not self.stop.is_set():
            try:
                if not self.paired or self.key_rejected:
                    self.stop.wait(config.SYNC_EVERY_S)
                    continue
                if self.archivist is False and time.time() - self.archivist_checked < 300:
                    self.stop.wait(config.SYNC_EVERY_S)
                    continue
                result = self.sync_once()
                while result.get("more") and not self.stop.is_set():
                    result = self.sync_once()
            except Exception as e:
                self.sync_status["error"] = repr(e)
            if self.archivist is not False and time.time() - self.baselines_at > 3600:
                self.baselines_at = time.time()
                try:
                    with self.db_lock:
                        sent = baselines_mod.push(self.db, self.relay)
                    self.baselines = sent
                    for rider, b in sent.items():
                        if b.get("ok"):
                            self.note("info", f"baseline for {rider}: {baselines_mod.describe(b)}",
                                      rider, tag="baseline")
                except Exception as e:
                    self.note("warn", f"could not publish baselines: {e!r}", tag="baseline")
            if time.time() - last_prune > 3600:
                last_prune = time.time()
                try:
                    with self.db_lock:
                        archive.prune(self.db)
                    self.tiles.sweep()
                except Exception:
                    pass
            self.stop.wait(config.SYNC_EVERY_S)

    def weather_forever(self) -> None:
        """Weather where each rider is. Riders on a trip first; with nobody out, the
        last known position of each, so an idle pane is not left without it."""
        while not self.stop.is_set():
            with self.lock:
                spots = {rider: (s["lat"], s["lon"]) for rider, s in self.state.items()
                         if s.get("lat") is not None and s.get("trip_id")}
                if not spots:
                    spots = {rider: (s["lat"], s["lon"]) for rider, s in self.state.items()
                             if s.get("lat") is not None}
            for rider, (lat, lon) in spots.items():
                w = weather_mod.fetch(lat, lon, config.WINDY_KMH)
                with self.lock:
                    if w:
                        self.weather_by[rider] = w
                    elif self.weather_by.get(rider):
                        taken = _parse(self.weather_by[rider].get("fetched_at"))
                        old = not taken or (datetime.now(timezone.utc) - taken).total_seconds() > WEATHER_STALE_S
                        if old:
                            del self.weather_by[rider]
                            self.note("info", "no weather — the service has not answered for hours",
                                      rider, tag="weather")
            with self.lock:
                # The single reading older screens and tests read: the first rider out.
                self.weather = next((self.weather_by[r] for r in spots if r in self.weather_by), None)
            self.stop.wait(config.WEATHER_EVERY_S if self.weather else 60)

    def cursor(self) -> int:
        with self.db_lock:
            return archive.cursor(self.db)

    # ---------------------------------------------------------------- archive reads

    def previous_trip(self, rider: str) -> Optional[dict]:
        with self.db_lock:
            return archive.last_trip(self.db, rider)

    def start(self) -> None:
        self.sync_status["last_seq"] = self.cursor()      # where the archive already stands
        for fn in (self.poll_state, self.consume_events, self.sync_forever, self.weather_forever):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
        self.note("info", f"monitor {VERSION} started", tag="monitor")


M = Monitor()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    M.start()
    yield
    M.stop.set()
    M.alarm.set(False)


app = FastAPI(title="moto-tracker monitor", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


# ---------------------------------------------------------------- local-only guard

def guard(request: Request, x_moto: Optional[str] = Header(default=None)) -> None:
    """Loopback is not a security boundary by itself: any page in any browser can
    POST to 127.0.0.1. A custom header cannot be set cross-site without a preflight,
    and this server answers no preflight, so these two checks together mean only our
    own page can act."""
    if request.client and request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "local only")
    if request.method != "GET":
        if x_moto != "1":
            raise HTTPException(403, "not from this monitor")
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            raise HTTPException(403, "bad origin")


@app.middleware("http")
async def local_only(request: Request, call_next):
    try:
        guard(request, request.headers.get("x-moto"))
    except HTTPException as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    response = await call_next(request)
    response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------- the page

@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "ui/index.html").read_text()


@app.get("/ui/{name}")
def ui_asset(name: str):
    path = (HERE / "ui" / name).resolve()
    if path.parent != (HERE / "ui").resolve() or not path.exists():
        raise HTTPException(404, "no such file")
    types = {".js": "text/javascript", ".css": "text/css", ".png": "image/png",
             ".svg": "image/svg+xml", ".wav": "audio/wav"}
    return FileResponse(path, media_type=types.get(path.suffix, "application/octet-stream"))


# ---------------------------------------------------------------- state

@app.get("/api/status")
def api_status():
    with M.lock:
        state = json.loads(json.dumps(M.state))
        server = dict(M.server)
        weather = dict(M.weather) if M.weather else None
        weather_by = json.loads(json.dumps(M.weather_by))
        attention = M.attention
    # A drill is shown the moment it is raised, not on the next poll.
    state = M.apply_drill(state)
    if M.drill and attention is None:
        attention = (state.get(M.drill["rider"], {}).get("incident") or {}).get("display")
    riders = {}
    for rider, s in state.items():
        s = dict(s)
        # The archivist has the whole history; a viewer keeps what the relay sends.
        if not s.get("trip_id") and M.archivist is not False:
            if rider not in M.last_trip_cache or time.time() - M.last_trip_cache[rider][0] > 10:
                M.last_trip_cache[rider] = (time.time(), M.previous_trip(rider))
            s["previous_trip"] = M.last_trip_cache[rider][1] or s.get("previous_trip")
        riders[rider] = s
    return {
        "version": VERSION,
        "server": server,
        "stream_up": M.stream_up,
        "packets_per_minute": M.packets_per_minute(),
        "riders": riders,
        "weather": weather,
        "weather_by": weather_by,
        "attention": attention,
        "alarm": {"sounding": M.alarm.sounding, "muted": M.alarm.mute, "reason": M.alarm.reason},
        "drill": bool(M.drill),
        "baselines": M.baselines,
        "sync": M.sync_status,
        "tiles": M.tiles.available(),
        "paired": M.paired and not M.key_rejected,
        "key_rejected": M.key_rejected,
        "archivist": M.archivist,
        "devices_available": config.DEVICES_ENABLED and config.SSH_KEY.exists(),
        "hostname": socket.gethostname().replace(".local", ""),
        "relay": config.RELAY_URL,
        "now": now(),
    }


@app.post("/api/pair")
def api_pair(payload: dict = Body(...)):
    """Pairs this Mac with the relay using a code from the other Mac's Devices panel.
    Nobody needs the VPS: the code is exchanged for this Mac's own device key."""
    code = "".join(c for c in str(payload.get("code", "")).upper() if c.isalnum())
    name = (payload.get("name") or socket.gethostname()).strip()[:80]
    if len(code) != 8:
        raise HTTPException(400, "a pairing code is eight characters")
    status, body = M.relay.pair(code, name)
    if status != 200 or not isinstance(body, dict) or "key" not in body:
        detail = body.get("detail") if isinstance(body, dict) else body
        raise HTTPException(status or 502, str(detail or "pairing failed"))
    if body.get("role") != "monitor":
        raise HTTPException(400, f"that code is for a {body.get('role')} device, not a Mac")
    M.relay.save_key(body["key"])
    # The relay has just said which kind of Mac this is; no need to discover it.
    M.paired, M.key_rejected = True, False
    M.archivist = bool(body.get("archivist"))
    M.archivist_checked = time.time()
    M.note("info", f"this Mac paired as {body.get('name')}"
                   + (" — it keeps the history" if body.get("archivist") else " — viewer"), tag="device")
    return {"ok": True, "name": body.get("name"), "archivist": bool(body.get("archivist"))}


@app.get("/api/events")
def api_events(after: int = 0, limit: int = 400):
    with M.lock:
        rows = [e for e in M.log if e["seq"] > after][-max(1, min(limit, LOG_LINES)):]
        return {"entries": rows, "seq": M.log_seq}


# ---------------------------------------------------------------- replay

@app.get("/api/trip-days")
def api_trip_days(rider: str = ""):
    """Which days have a ride to watch. The picker offers these rather than asking
    the user to guess a date (2026-09-16). Scoped to one rider when asked."""
    with M.db_lock:
        return {"rider": rider, "days": archive.days_with_trips(M.db, rider or None)}


@app.get("/api/trips")
def api_trips(day: str, rider: str = ""):
    """The finished trips that started on one day, for one rider when asked."""
    if len(day) != 10:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    with M.db_lock:
        return {"day": day, "rider": rider, "trips": archive.trips_on(M.db, day, rider or None)}


@app.get("/api/replay")
def api_replay(trip_id: int):
    """One whole trip: every position, and everything logged while it was open."""
    with M.db_lock:
        data = archive.replay(M.db, trip_id)
    if not data:
        raise HTTPException(404, f"no trip #{trip_id} in the archive")
    return data


@app.get("/api/trail")
def api_trail(trip_id: int, limit: int = 3000):
    with M.db_lock:
        return {"trail": archive.trail(M.db, trip_id, limit)}


@app.get("/api/attention")
def api_attention():
    """What the menu-bar icon needs, and nothing else: it polls this every second."""
    with M.lock:
        return {"attention": M.attention, "sounding": M.alarm.sounding, "server_up": M.server["up"]}


# ---------------------------------------------------------------- acting on an incident

@app.post("/api/incident/{incident_id}/silence")
def api_silence(incident_id: int):
    if M.drill and incident_id == M.drill["id"]:
        M.drill["silenced_at"] = now()
        M.drill["silenced_by"] = "Mac Mini monitor"
        M.alarm.set(False)
        M.note("info", "DRILL — alarm silenced; the practice incident is still open",
               M.drill["rider"], tag="drill")
        return {"ok": True, "silenced": True, "drill": True}
    status, body, _ = M.relay.call("POST", "/incident/silence", {"incident_id": incident_id})
    if status != 200:
        raise HTTPException(status or 502, str(body))
    M.alarm.set(False)
    return body


@app.post("/api/incident/{incident_id}/close")
def api_close(incident_id: int):
    """The Observer's half. The incident stays open until the rider closes it too."""
    if M.drill and incident_id == M.drill["id"]:
        M.alarm.set(False)
        M.stop_drill("closed")
        return {"ok": True, "state": "closed", "drill": True}
    status, body, _ = M.relay.call("POST", "/incident/observer-close", {"incident_id": incident_id})
    if status != 200:
        raise HTTPException(status or 502, str(body))
    M.alarm.set(False)
    return body


@app.post("/api/drill")
def api_drill(payload: dict = Body(default={})):
    """Rehearse an incident on this Mac. Refused while anyone is riding or a real
    incident is open, and it never reaches the relay or the archive."""
    rider = payload.get("rider")
    if not rider:
        with M.lock:
            rider = next(iter(M.state), None)
    if not rider:
        raise HTTPException(503, "no rider state yet")
    return {"ok": True, "incident": M.start_drill(rider)}


@app.post("/api/drill/stop")
def api_drill_stop():
    M.alarm.set(False)
    M.stop_drill("stopped")
    return {"ok": True}


@app.post("/api/incident/{incident_id}/force-close")
def api_force_close(incident_id: int, payload: dict = Body(default={})):
    """Deliberately awkward: the exact phrase must be typed, and the close is
    recorded as made without the rider's confirmation."""
    if M.drill and incident_id == M.drill["id"]:
        raise HTTPException(400, "this is a drill; close it with Close incident")
    status, body, _ = M.relay.call("POST", "/incident/force-close",
                                   {"incident_id": incident_id, "confirm": payload.get("confirm", "")})
    if status != 200:
        raise HTTPException(status or 502, str(body))
    M.alarm.set(False)
    M.note("warn", f"incident #{incident_id} closed without rider confirmation", tag="incident")
    return body


@app.post("/api/trip/force-end")
def api_force_end(payload: dict = Body(default={})):
    """Ends a trip whose phone can no longer end it. An Observer's decision, made
    deliberately behind a confirmation (Jack, 2026-09-16)."""
    rider = payload.get("rider")
    if not rider:
        raise HTTPException(400, "which rider?")
    status, body, _ = M.relay.call("POST", "/trip/force-end", {"rider": rider})
    if status != 200:
        raise HTTPException(status or 502, str(body))
    M.note("warn", f"force-ended {rider}'s trip — their phone was not going to", rider, tag="trip")
    return body


@app.post("/api/alarm/test")
def api_alarm_test():
    """Starts or stops the test alarm. Refuses while a real incident is sounding —
    that one is stopped by silencing the incident, which the Observer phone shares."""
    if M.alarm.sounding and M.alarm.reason != "test":
        raise HTTPException(409, "an incident is sounding — silence it in the incident box")
    sounding = M.alarm.test()
    M.note("info", "test alarm started" if sounding else "test alarm stopped", tag="alarm")
    return {"ok": True, "sounding": sounding}


@app.post("/api/alarm/stop")
def api_alarm_stop():
    if M.alarm.sounding and M.alarm.reason != "test":
        raise HTTPException(409, "an incident is sounding — silence it in the incident box")
    M.alarm.set(False)
    return {"ok": True}


# ---------------------------------------------------------------- devices and logs

@app.get("/api/devices")
def api_devices():
    return M.devices.list()


@app.post("/api/devices/pair")
def api_pair(payload: dict = Body(...)):
    return M.devices.pair(payload.get("role", ""), payload.get("name", ""), payload.get("rider"))


@app.post("/api/devices/revoke")
def api_revoke(payload: dict = Body(...)):
    try:
        device_id = int(payload.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "which device?")
    return M.devices.revoke(device_id)


@app.post("/api/open-logs")
def api_open_logs(payload: dict = Body(default={})):
    rider = payload.get("rider") or ""
    rider = "".join(c for c in rider if c.isalnum() or c in "_-")
    folder = (config.LOGS / rider) if rider else config.LOGS
    folder.mkdir(parents=True, exist_ok=True)
    # Make sure what Finder shows is current before it opens.
    with M.db_lock:
        rows = M.db.execute("SELECT id FROM trips WHERE ended_at IS NOT NULL"
                            + (" AND rider_id=?" if rider else "")
                            + " ORDER BY started_at DESC LIMIT 20",
                            (rider,) if rider else ()).fetchall()
        for row in rows:
            archive.write_trip_log(M.db, row["id"], config.LOGS)
    try:
        subprocess.Popen(["/usr/bin/open", str(folder)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        raise HTTPException(500, repr(e))
    return {"ok": True, "folder": str(folder), "logs": len(rows)}


# ---------------------------------------------------------------- map tiles

@app.get("/tiles/{layer}/{z}/{x}/{y}.png")
def api_tile(layer: str, z: int, x: int, y: int, style: str = "main"):
    if layer not in ("map", "traffic"):
        raise HTTPException(404, "no such layer")
    data, ttl = M.tiles.get(layer, z, x, y, style)
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": f"private, max-age={ttl}"})


def main() -> None:
    import uvicorn
    os.umask(0o077)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning",
                access_log=False, server_header=False)


if __name__ == "__main__":
    main()
