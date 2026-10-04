#!/usr/bin/env python3
"""
End-to-end test for the Mac app (design.md 2026-10-04).

Builds the app's own code without its window (app/Harness, the "harness"), starts a
throwaway relay (plain HTTP, its own database) and rides a whole trip through them:
pairing, positions, a crash candidate that escalates, the alarm, the two-sided
close, the archive, the trip log file and the map tiles.

The harness is spoken to over stdin and stdout, one JSON line each way — the same
requests the page makes inside the app — so nothing on the Mac side listens on a
port, in the test or out of it.

Touches nothing real: not the VPS, not ~/Library/Application Support, not the
speakers. Run it before every install:

    /usr/bin/python3 app/e2e_test.py          # uses the Mini's venv for the relay
"""
import base64
import json
import os
import queue
import shutil
import signal
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VENV = Path.home() / "Library/Application Support/moto-tracker/venv/bin/python"
HARNESS = None          # the built harness, set in main()
# Tiles are the one thing worth testing against the real service: everything else
# runs entirely inside this test. Without the key the proxy must still serve a
# blank tile rather than break the map, and that is what gets checked instead.
REAL_TOMTOM_KEY = Path.home() / "Library/Application Support/moto-tracker/monitor-secrets/tomtom.key"
PASS, FAIL = [], []


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(label)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail and not ok else ''}")
    return ok


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Mac:
    """The app's code without its window, driven the way the page drives it."""

    def __init__(self, env: dict):
        self.proc = subprocess.Popen([str(HARNESS)], env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, bufsize=1)
        self.lines = queue.Queue()
        self.n = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(line)

    def request(self, method, path, body=None, headers=None, timeout=15):
        """(status, bytes). Status 0 means no answer."""
        with self.lock:
            self.n += 1
            head = dict(headers or {})
            if body is not None:
                head.setdefault("Content-Type", "application/json")
            msg = {"id": self.n, "method": method, "path": path, "headers": head,
                   "body": json.dumps(body) if body is not None else None}
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                return 0, repr(e).encode()
            deadline = time.time() + timeout
            while True:
                try:
                    reply = json.loads(self.lines.get(timeout=max(0.1, deadline - time.time())))
                except queue.Empty:
                    return 0, b"no answer"
                if reply.get("id") == self.n:       # an answer that came too late is skipped
                    return reply["status"], base64.b64decode(reply["body_b64"])


def call(base, method: str, path: str, body=None, token=None, headers=None, timeout=15):
    if isinstance(base, Mac):
        status, raw = base.request(method, path, body, headers, timeout)
        text = raw.decode("utf-8", "replace")
        if status == 0:
            return 0, text
        try:
            return status, (json.loads(text) if text and (text[:1] in "[{" or status >= 400) else text)
        except ValueError:
            return status, text
    data = json.dumps(body).encode() if body is not None else None
    head = {"Content-Type": "application/json"} if data else {}
    if token:
        head["Authorization"] = "Bearer " + token
    head.update(headers or {})
    req = urllib.request.Request(base + path, data=data, method=method, headers=head)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text and text[:1] in "[{" else text)
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, text
    except Exception as e:
        return 0, repr(e)


def fetch_bytes(base, path: str, timeout=20):
    """For binary answers (tiles): the text helper above would choke on PNG bytes."""
    if isinstance(base, Mac):
        return base.request("GET", path, timeout=timeout)
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, repr(e).encode()


def until(predicate, seconds=20, step=0.5):
    """Waits for something the monitor does on its own timers."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def wait_for(base: str, path: str, token=None, tries=80) -> bool:
    for _ in range(tries):
        status, _ = call(base, "GET", path, token=token, timeout=3)
        if status in (200, 401, 403):
            return True
        time.sleep(0.25)
    return False


def check_weather_labels() -> None:
    """The pane may only claim rain when the model has rain in it (Jack, 2026-09-20).

    Twice the screen said rain over a dry ride — "Thunderstorm", then "Light
    drizzle" — on 0.0 and 0.1 mm. The phone carries the same rule in Weather.kt,
    with its own test.
    """
    for code, day, precip, cloud, expected in (
        (51, True, 0.1, 98, "Overcast"),           # the real one, 2026-09-20
        (95, True, 0.0, 40, "Partly cloudy"),      # the real one, 2026-09-19
        (51, True, 0.4, 98, "Light drizzle"),      # rain it actually has
        (63, True, 2.0, 100, "Rain"),
        (0, True, 0.0, 5, "Sunny"),
        (0, False, 0.0, 5, "Clear"),
        (45, True, 0.0, 100, "Fog"),               # seeing, not wetness
        (3, True, 0.0, -1, "—"),                   # no cloud figure: say nothing
    ):
        got = probe(["describe", str(code), "1" if day else "0", str(precip), str(cloud)]).get("label")
        check(f"weather code {code} with {precip} mm and {cloud}% cloud reads as {expected}",
              got == expected, f"got {got}")


def probe(args: list, env: dict = None) -> dict:
    """One of the harness's single-shot checks, answered as a JSON line."""
    try:
        out = subprocess.run([str(HARNESS), "probe", *args], env=env, capture_output=True,
                             text=True, timeout=60)
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, ValueError, IndexError) as e:
        return {"error": repr(e)}


def check_stale_weather_does_not_hang(env: dict) -> None:
    """Weather unanswered for hours must not hang the app (2026-10-02).

    In the old background service, dropping a stale reading logged its line from
    inside a lock that the log takes too. The weather thread waited on itself, and
    everything behind that lock waited with it: the relay polls, the alarm, every
    page request. It stayed that way for four hours with the process up the whole
    time, and the window sat at "Waiting for the monitor…". The app's shared state
    can be re-entered, so this must come back at once.
    """
    got = probe(["stale-weather"], env)
    check("weather unanswered for hours is dropped without hanging the app",
          got.get("free") is True and got.get("dropped") is True, json.dumps(got))
    check("and the log says there is no weather", got.get("logged") is True, json.dumps(got))


def check_log_colours() -> None:
    """The running log's colour rule, checked against the relay's real messages.

    It lives in the page's JavaScript, so this reads the two word lists straight out
    of it rather than keeping a second copy that could drift (Jack, 2026-09-16:
    blue for a change of state, yellow for a possible incident, red for a real one).
    """
    import re
    js = (HERE / "ui/app.js").read_text()
    red = re.search(r"const LOG_RED = \[(.*?)\];", js, re.S)
    yellow = re.search(r"const LOG_YELLOW = \[(.*?)\];", js, re.S)
    if not red or not yellow:
        check("the log colour rule is where the test expects it", False, "LOG_RED/LOG_YELLOW")
        return
    RED = [w.strip().strip("',") for w in red.group(1).split(",") if w.strip().strip("',")]
    YELLOW = [w.strip().strip("',") for w in yellow.group(1).split(",") if w.strip().strip("',")]

    def colour(tag, level, message):
        t = message.lower()
        if tag in ("incident", "drill", "signal"):
            if any(w in t for w in RED):
                return "red"
            if any(w in t for w in YELLOW):
                return "yellow"
            return "red"
        if tag in ("trip", "rider", "stop", "signal-ok"):
            return "blue"
        if any(w in t for w in YELLOW):
            return "yellow"
        return {"alert": "red", "warn": "yellow"}.get(level, "white")

    cases = [
        ("trip", "info", "TRIP #109 started", "blue"),
        ("trip", "info", "off the bike — trip continues, telemetry still reporting", "blue"),
        ("trip", "info", "TRIP #109 ended — asleep", "blue"),
        ("stop", "info", "answered: arrived", "blue"),
        ("incident", "warn", "POSSIBLE CRASH — candidate #1000, watching 30s", "yellow"),
        ("incident", "alert", "SIGNAL LOST after impact 17.5 g — candidate #1004, quiet 31s", "yellow"),
        ("incident", "alert", "CRASH DETECTED — candidate #1000 was not retracted in time", "red"),
        ("incident", "info", "#1000 CLOSED — rider (ok) and Observer both closed it", "red"),
        ("incident", "info", "candidate #1000 retracted by the rider (ok)", "yellow"),
        ("incident", "info", "#1001 cleared itself — normal riding resumed for jack", "yellow"),
        ("incident", "alert", "#1002: JACK PRESSED I NEED HELP", "red"),
        ("signal", "warn", "signal lost 60s ago — last seen at 60 km/h", "yellow"),
        ("no-signal", "warn", "signal lost for 2:00 — last seen at 46 km/h, nothing violent before it."
                              " An alert, not an incident", "yellow"),
        ("signal-ok", "info", "signal back after 3:39 — now at 46 km/h", "blue"),
        ("incident", "alert", "CRASH DETECTED — #1004: impact 17.5 g (threshold 13.2),"
                              " then no signal for 31s", "red"),
        ("position", "tick", "13.48785, -89.35408  1 km/h", "white"),
        ("baseline", "info", "jack: impact 11.0 g / 8.0 rad/s", "white"),
    ]
    wrong = [f"{t}/{lv}: {m[:40]} -> {colour(t, lv, m)} not {want}"
             for t, lv, m, want in cases if colour(t, lv, m) != want]
    check(f"the running log colours all {len(cases)} kinds of line correctly",
          not wrong, "; ".join(wrong)[:200])


def main() -> int:
    global HARNESS
    python = str(VENV) if VENV.exists() else sys.executable
    work = Path(tempfile.mkdtemp(prefix="moto-e2e-"))
    relay_port = free_port()
    relay_url = f"http://127.0.0.1:{relay_port}"
    procs = []
    print(f"workspace {work}")

    try:
        # ---------------------------------------------------------------- the app's code
        HARNESS = work / "moto-harness"
        built = subprocess.run(["bash", str(HERE / "build.sh"), "--harness", str(HARNESS)],
                               capture_output=True, text=True)
        if not check("the app's code compiles", built.returncode == 0 and HARNESS.exists(),
                     (built.stderr or built.stdout)[-1500:]):
            return report()

        # ---------------------------------------------------------------- relay
        relay_env = dict(os.environ, RELAY_DB=str(work / "relay.db"),
                         RELAY_HOST="127.0.0.1", RELAY_PORT=str(relay_port),
                         # Production waits 30 s for the ride to prove itself ordinary.
                         # Here it is 3, so the suite does not spend minutes asleep;
                         # the deploy smoke test checks the real floor on the VPS.
                         RELAY_MIN_CONFIRM_S="3",
                         # A signal loss is shown after a minute and alerted after two.
                         # Here 35 s and 45 s: shorter, but still both past the 30 s at
                         # which violence-then-silence alarms, as in production.
                         RELAY_SIGNAL_LOST_S="35", RELAY_SIGNAL_ALERT_S="45")
        procs.append(subprocess.Popen([python, str(ROOT / "relay/relay.py")], env=relay_env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        if not check("the test relay starts", wait_for(relay_url, "/livez")):
            return report()

        # Keys: one monitor, one rider, made the way relayctl makes them.
        ctl_env = dict(os.environ, RELAY_DB=str(work / "relay.db"))
        keys = {}
        for role, rider, name in (("monitor", None, "test monitor"), ("rider", "jack", "test phone"),
                                  ("rider", "dana", "her test phone")):
            args = [python, str(ROOT / "relay/relayctl.py"), "pair", "--role", role, "--name", name, "--json"]
            if rider:
                args += ["--rider", rider]
            out = subprocess.run(args, env=ctl_env, capture_output=True, text=True)
            code = json.loads(out.stdout)["code"]
            status, body = call(relay_url, "POST", "/pair", {"code": code, "name": name})
            keys[rider or role] = body["key"]
        check("pairing codes exchange for device keys", len(keys) == 3)

        key_file = work / "monitor.key"
        key_file.write_text(keys["monitor"])
        rk, jk = keys["jack"], keys["dana"]

        # ---------------------------------------------------------------- the Mac
        mon_env = dict(os.environ,
                       MONITOR_SUPPORT=str(work / "support"),
                       MONITOR_RELAY_URL=relay_url,
                       MONITOR_KEY=str(key_file),
                       MONITOR_CA=str(work / "unused-ca.crt"),
                       MONITOR_ARCHIVE=str(work / "moto.db"),
                       MONITOR_LOGS=str(work / "logs"),
                       MONITOR_TILE_CACHE=str(work / "tiles"),
                       MONITOR_TOMTOM_KEY=str(REAL_TOMTOM_KEY),
                       MONITOR_UI=str(HERE / "ui"),
                       MONITOR_ALARM_MUTE="1",
                       MONITOR_OPEN_APP="0",
                       MONITOR_DEVICES="0")
        mac = Mac(mon_env)
        procs.append(mac.proc)
        if not check("the Mac's code starts", wait_for(mac, "/api/status")):
            return report()

        check_log_colours()
        check_weather_labels()
        # The test runs muted, so this is the proof the sounds themselves are sound: both
        # decode in the player the alarm uses, at their designed lengths.
        sounds = probe(["sounds"])
        check("the alarm and the chime are playable, at their designed lengths",
              sounds.get("ready") is True and sounds.get("alarm_s") == 6.0 and sounds.get("chime_s") == 1.9,
              json.dumps(sounds))
        check_stale_weather_does_not_hang(dict(
            mon_env, MONITOR_SUPPORT=str(work / "weather-support"),
            MONITOR_ARCHIVE=str(work / "weather.db"), MONITOR_LOGS=str(work / "weather-logs"),
            MONITOR_KEY=str(work / "weather.key"), MONITOR_TOMTOM_KEY=str(work / "no-tomtom.key")))
        body = until(lambda: (lambda b: b if (b or {}).get("server", {}).get("up") else None)(
            call(mac, "GET", "/api/status")[1]), seconds=10) or call(mac, "GET", "/api/status")[1]
        check("the Mac reaches the relay itself", body["server"]["up"] is True, json.dumps(body["server"]))
        listening = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(mac.proc.pid), "-iTCP",
                                    "-sTCP:LISTEN"], capture_output=True, text=True).stdout.strip()
        check("and listens on no port at all", listening == "", listening[:200])
        # Each pane shows its own rider's weather now that both can be out (2026-09-24).
        check("the status carries weather per rider", isinstance(body.get("weather_by"), dict),
              str(body.get("weather_by"))[:80])
        check("the page is served", call(mac, "GET", "/")[0] == 200)
        check("Leaflet is served locally, not from a CDN",
              call(mac, "GET", "/ui/leaflet.js")[0] == 200)

        # ---------------------------------------------------------------- CSRF guard
        status, _ = call(mac, "POST", "/api/alarm/test", {})
        check("a POST without the monitor's header is refused", status == 403, f"got {status}")
        status, _ = call(mac, "POST", "/api/alarm/test", {},
                         headers={"X-Moto": "1", "Origin": "https://evil.example"})
        check("a POST from another origin is refused", status == 403, f"got {status}")
        status, body = call(mac, "POST", "/api/alarm/test", {}, headers={"X-Moto": "1"})
        check("the monitor's own POST is accepted", status == 200, f"got {status}")
        check("Test alarm sounds until it is stopped, not once",
              body.get("sounding") is True, json.dumps(body))
        time.sleep(4)          # several state polls go by; none of them may silence it
        status, body = call(mac, "GET", "/api/status")
        check("a test alarm keeps sounding across state polls",
              body["alarm"]["sounding"] is True and body["alarm"]["reason"] == "test",
              json.dumps(body["alarm"]))
        status, body = call(mac, "POST", "/api/alarm/test", {}, headers={"X-Moto": "1"})
        check("pressing it again stops it", body.get("sounding") is False, json.dumps(body))
        status, body = call(mac, "GET", "/api/status")
        check("and it stays stopped", body["alarm"]["sounding"] is False, json.dumps(body["alarm"]))

        # ---------------------------------------------------------------- a trip
        # Start trip opens the trip off the bike; Start ride gets on it. The other
        # phone becomes the Observer on the trip being open, not on anyone riding
        # (Jack, 2026-09-20).
        status, trip = call(relay_url, "POST", "/trip/start",
                            {"rider": "jack", "riding": False}, token=rk)
        trip_id = trip["trip_id"]
        check("Start trip opens the trip off the bike",
              trip.get("trip_opened") is True and trip.get("state") == "offbike", json.dumps(trip))
        status, seen = call(relay_url, "GET", "/state", token=jk)
        theirs = seen.get("jack") or {}
        check("and the other phone already has a trip to watch",
              theirs.get("trip_id") == trip_id and theirs.get("state") == "offbike",
              json.dumps(theirs)[:140])
        status, body = call(relay_url, "POST", "/ride/start", {"rider": "jack"}, token=rk)
        check("Start ride gets on the bike inside the same trip",
              body.get("trip_opened") is False and body.get("state") == "riding"
              and body.get("trip_id") == trip_id, json.dumps(body))
        lat, lon = 13.6929, -89.2182
        for i in range(6):
            call(relay_url, "POST", "/position",
                 {"rider": "jack", "lat": lat + i * 0.001, "lon": lon + i * 0.001, "speed": 60 + i,
                  "accuracy": 5, "battery": 90 - i, "peak_g": 1.1 + i * 0.05, "mean_g": 1.0,
                  "peak_rot": 0.4, "accel_n": 250, "gyro_n": 250}, token=rk)
        time.sleep(4.5)                                    # a poll and a sync go by
        status, body = call(mac, "GET", "/api/status")
        jack = body["riders"]["jack"]
        check("the monitor shows the rider riding", jack["state"] == "riding" and jack["trip_id"] == trip_id)
        check("the monitor shows a live position", jack["lat"] is not None and jack["speed"] is not None)
        check("packets per minute are counted", body["packets_per_minute"] >= 6,
              str(body["packets_per_minute"]))
        check("the relay's clock is published for the counters", bool(body["now"]))

        status, body = call(mac, "GET", "/api/events?after=0")
        check("the running log has the ticks",
              any(e["tag"] == "position" for e in body["entries"]), str(len(body["entries"])))

        trail = until(lambda: (call(mac, "GET", f"/api/trail?trip_id={trip_id}")[1] or {}).get("trail"))
        check("the map trail comes from the archive", len(trail or []) >= 5, str(len(trail or [])))

        # ---------------------------------------------------------------- an incident
        status, body = call(relay_url, "POST", "/incident/candidate",
                            {"rider": "jack", "confirm_window_s": 1,
                             "evidence": {"peak_g": 9.4, "peak_rot": 6.1}}, token=rk)
        incident_id = body["incident_id"]
        time.sleep(1.5)
        status, body = call(mac, "GET", "/api/status")
        inc = body["riders"]["jack"]["incident"]
        check("a candidate shows as checking, with no alarm",
              inc and inc["display"] == "pending" and body["alarm"]["sounding"] is False,
              json.dumps(inc))

        time.sleep(4.5)                                    # the relay escalates it
        status, body = call(mac, "GET", "/api/status")
        inc = body["riders"]["jack"]["incident"]
        check("an unretracted candidate becomes a red incident", inc["display"] == "red", json.dumps(inc))
        check("the alarm is sounding", body["alarm"]["sounding"] is True)
        check("the menu-bar icon is told to flash", body["attention"] == "red")
        check("the box has the crash evidence",
              inc["g"] == 9.4 and inc["rot"] == 6.1 and inc["speed_before"] is not None, json.dumps(inc))
        check("the box has a last known position", inc["last_position"] is not None)
        check("the box can run a stationary counter", "stationary_since" in inc)

        status, body = call(mac, "POST", f"/api/incident/{incident_id}/silence", {},
                            headers={"X-Moto": "1"})
        check("silencing works from the Mini", status == 200, json.dumps(body))
        time.sleep(1.5)
        status, body = call(mac, "GET", "/api/status")
        check("the alarm stops but the incident stays open",
              body["alarm"]["sounding"] is False
              and body["riders"]["jack"]["incident"]["state"] == "sos")

        status, body = call(mac, "POST", f"/api/incident/{incident_id}/close", {},
                            headers={"X-Moto": "1"})
        check("the Observer's close waits for the rider", body.get("waiting_for") == "rider", json.dumps(body))
        time.sleep(1.5)
        status, body = call(mac, "GET", "/api/status")
        check("it is still red while the rider has not answered",
              body["riders"]["jack"]["incident"]["display"] == "red")

        call(relay_url, "POST", "/incident/resolve",
             {"incident_id": incident_id, "rider": "jack", "resolution": "ok"}, token=rk)
        time.sleep(1.5)
        status, body = call(mac, "GET", "/api/status")
        check("both halves close the incident", body["riders"]["jack"]["incident"] is None)
        check("nothing is left flashing", body["attention"] is None)

        # ---------------------------------------------------------------- force close
        status, body = call(relay_url, "POST", "/incident/candidate",
                            {"rider": "jack", "confirm_window_s": 1}, token=rk)
        second = body["incident_id"]
        time.sleep(4.5)
        status, body = call(mac, "POST", f"/api/incident/{second}/force-close",
                            {"confirm": "yes"}, headers={"X-Moto": "1"})
        check("a force close needs the exact phrase", status == 400, f"got {status}")
        status, body = call(mac, "POST", f"/api/incident/{second}/force-close",
                            {"confirm": "close without rider confirmation"}, headers={"X-Moto": "1"})
        check("a force close with the phrase is accepted", status == 200 and body.get("forced") is True,
              json.dumps(body))

        # ------------------------------------------------- a rider phone as Observer
        # Jack, 2026-09-15: there is no separate Observer key. The other rider's phone
        # asks the relay whether a trip is open and observes until it ends.
        status, body = call(relay_url, "GET", "/state", token=jk)
        check("the other rider's phone can read the ride", status == 200 and "jack" in (body or {}),
              f"got {status}")
        # I need help overrides every other rule: no window, no auto-retraction.
        status, urgent = call(relay_url, "POST", "/incident/candidate",
                              {"rider": "jack", "confirm_window_s": 300}, token=rk)
        status, body = call(relay_url, "POST", "/incident/escalate",
                            {"rider": "jack", "incident_id": urgent["incident_id"]}, token=rk)
        check("I need help escalates an open incident at once",
              status == 200 and body.get("state") == "sos", f"got {status} {body}")
        status, body = call(relay_url, "GET", "/state", token=keys["monitor"])
        inc = body["jack"]["incident"]
        check("and it is red, marked as the rider asking for help",
              inc and inc["display"] == "red" and inc["kind"] == "help", json.dumps(inc))
        call(relay_url, "POST", "/incident/observer-close",
             {"incident_id": urgent["incident_id"]}, token=jk)
        call(relay_url, "POST", "/incident/resolve",
             {"incident_id": urgent["incident_id"], "rider": "jack", "resolution": "ok"}, token=rk)

        status, third = call(relay_url, "POST", "/incident/candidate",
                             {"rider": "jack", "confirm_window_s": 1}, token=rk)
        third_id = third["incident_id"]
        time.sleep(4.5)
        status, body = call(relay_url, "POST", "/incident/silence", {"incident_id": third_id}, token=jk)
        check("the other rider's phone can silence the alarm", status == 200, f"got {status} {body}")
        status, body = call(relay_url, "POST", "/incident/observer-close", {"incident_id": third_id}, token=rk)
        check("a rider cannot be their own Observer", status == 403, f"got {status}")
        status, body = call(relay_url, "POST", "/incident/observer-close", {"incident_id": third_id}, token=jk)
        check("the other rider's phone closes the Observer half",
              status == 200 and body.get("waiting_for") == "rider", f"got {status} {body}")
        status, body = call(relay_url, "POST", "/incident/resolve",
                            {"incident_id": third_id, "rider": "jack", "resolution": "false_alarm"}, token=rk)
        check("and the rider's own half closes it", status == 200 and body.get("state") == "closed",
              f"got {status} {body}")
        status, _ = call(relay_url, "GET", "/sync", token=rk)
        check("a rider phone still cannot read the archive feed", status == 403, f"got {status}")

        # ------------------------------------------------- one open incident per rider
        # 2026-09-18: a screen left on in a pocket pressed I need help five times in ten
        # seconds, and each press opened an incident of its own.
        rdb = sqlite3.connect(work / "relay.db")

        def open_count():
            return rdb.execute("SELECT COUNT(*) FROM incidents WHERE rider_id='jack'"
                               " AND state IN ('pending','sos')").fetchone()[0]

        presses = [call(relay_url, "POST", "/stop-answer", {"rider": "jack", "answer": "help"},
                        token=rk)[1] for _ in range(5)]
        help_id = presses[0].get("incident_id")
        check("I need help pressed five times opens one incident",
              isinstance(help_id, int) and all(p.get("incident_id") == help_id for p in presses)
              and open_count() == 1, json.dumps(presses))
        status, body = call(relay_url, "POST", "/incident/candidate",
                            {"rider": "jack", "confirm_window_s": 1, "evidence": {"peak_g": 12.5}},
                            token=rk)
        check("a crash report during it joins it",
              body.get("incident_id") == help_id and body.get("joined") is True and open_count() == 1,
              json.dumps(body))
        # Once somebody has silenced it, pressing it again is a fresh alarm, never a quiet one.
        call(relay_url, "POST", "/incident/silence", {"incident_id": help_id}, token=jk)
        status, body = call(relay_url, "POST", "/stop-answer", {"rider": "jack", "answer": "help"},
                            token=rk)
        fresh_id = body.get("incident_id")
        time.sleep(1.5)
        status, body = call(mac, "GET", "/api/status")
        inc = body["riders"]["jack"]["incident"]
        check("after a silence, I need help is a fresh alarm with a new id",
              isinstance(fresh_id, int) and fresh_id != help_id and inc and inc["id"] == fresh_id
              and not inc.get("silenced_at") and body["alarm"]["sounding"] is True, json.dumps(inc))
        old = rdb.execute("SELECT state, resolution FROM incidents WHERE id=?",
                          (help_id,)).fetchone() or (None, None)
        check("and the incident it replaced is closed, saying why",
              old[0] == "closed" and f"#{fresh_id}" in (old[1] or "") and open_count() == 1, str(old))
        call(relay_url, "POST", "/incident/observer-close", {"incident_id": fresh_id}, token=jk)
        call(relay_url, "POST", "/incident/resolve",
             {"incident_id": fresh_id, "rider": "jack", "resolution": "false_alarm"}, token=rk)
        check("closing the fresh one leaves nothing open", open_count() == 0, str(open_count()))

        rdb.close()
        time.sleep(1.5)                                    # the monitor sees it all closed

        # ---------------------------------------------------------------- the archive
        req = urllib.request.Request(relay_url + "/spike/log?rider=jack&build=e2e",
                                     data=b"phone log from the test\n", method="POST",
                                     headers={"Authorization": "Bearer " + rk})
        urllib.request.urlopen(req, timeout=10).read()
        call(relay_url, "POST", "/trip/end", {"rider": "jack"}, token=rk)
        time.sleep(7)                                      # one sync cycle, with room to spare

        db = sqlite3.connect(work / "moto.db")
        db.row_factory = sqlite3.Row
        trips = db.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
        positions = db.execute("SELECT COUNT(*) c FROM positions WHERE trip_id=?", (trip_id,)).fetchone()["c"]
        incidents = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        check("the trip is archived on the Mini", trips is not None and trips["ended_at"] is not None)
        check("its positions are archived", positions >= 6, str(positions))
        check("the incident is archived with both closes",
              incidents is not None and incidents["rider_closed_at"] and incidents["observer_closed_at"])
        check("the forced close is recorded as forced",
              db.execute("SELECT forced FROM incidents WHERE id=?", (second,)).fetchone()["forced"] == 1)

        status, body = call(relay_url, "GET", "/health", token=keys["monitor"])
        check("the relay's outbox is emptied by the Mini's acks", body["outbox_pending"] == 0,
              str(body["outbox_pending"]))

        logs = sorted((work / "logs").rglob("*.log"))
        trip_log = [p for p in logs if f"trip-{trip_id}" in p.name]
        check("a per-trip log file is written", bool(trip_log), str([p.name for p in logs]))
        if trip_log:
            text = trip_log[0].read_text()
            check("the trip log has the ride in it",
                  "positions" in text and "incidents:" in text and str(incident_id) in text)
        check("the phone's uploaded log is stored beside it",
              any(p.parent.name == "phone" for p in logs), str([str(p) for p in logs]))

        prev = until(lambda: (call(mac, "GET", "/api/status")[1] or {})
                     .get("riders", {}).get("jack", {}).get("previous_trip"), seconds=20)
        check("the idle pane gets the previous trip's statistics",
              prev is not None and prev["max_speed"] >= 60, json.dumps(prev))

        # ------------------------------------------------- cessation of signal
        # A phone cannot report its own silence, so the relay watches for it. Violence
        # then silence is a crash that took the phone with it: an alarm. Silence with
        # nothing violent before it is a signal loss and never an incident (Jack,
        # 2026-09-29): every screen shows it after a minute, and after two a bar over the
        # map and a single chime alert. Both riders go quiet here; only one raises anything.
        call(relay_url, "POST", "/baseline", {"rider": "dana", "impact_g": 13.2,
                                              "impact_rot": 9.1, "moving_h": 1.9,
                                              "source": "e2e"}, token=keys["monitor"])
        status, body = call(relay_url, "GET", "/baseline?rider=dana", token=jk)
        check("a rider can read the thresholds it will be judged by",
              status == 200 and body.get("impact_g") == 13.2, f"got {status} {body}")

        call(relay_url, "POST", "/trip/start", {"rider": "dana"}, token=jk)
        call(relay_url, "POST", "/trip/start", {"rider": "jack"}, token=rk)
        for i in range(4):
            for who, token in (("dana", jk), ("jack", rk)):
                call(relay_url, "POST", "/position",
                     {"rider": who, "lat": lat, "lon": lon, "speed": 62.0, "accuracy": 5,
                      "battery": 80, "peak_g": 2.0, "mean_g": 1.0, "peak_rot": 0.5,
                      "accel_n": 250, "gyro_n": 250}, token=token)
        # She hits something hard and the phone stops reporting.
        call(relay_url, "POST", "/position",
             {"rider": "dana", "lat": lat, "lon": lon, "speed": 9.0, "accuracy": 5,
              "battery": 80, "peak_g": 17.5, "mean_g": 2.2, "peak_rot": 11.0,
              "accel_n": 250, "gyro_n": 250}, token=jk)
        # He parks: slows right down, takes the bump at the end of the driveway, and
        # his phone goes quiet too. Jack's rule (2026-09-16) — at walking pace only a
        # vehicle-sized blow counts, so this must raise nothing at all.
        for speed in (40.0, 18.0, 6.0, 3.0, 2.0, 2.0):
            call(relay_url, "POST", "/position",
                 {"rider": "jack", "lat": lat, "lon": lon, "speed": speed, "accuracy": 5,
                  "battery": 80, "peak_g": 2.1, "mean_g": 1.0, "peak_rot": 0.6,
                  "accel_n": 250, "gyro_n": 250}, token=rk)
        call(relay_url, "POST", "/position",
             {"rider": "jack", "lat": lat, "lon": lon, "speed": 2.0, "accuracy": 5,
              "battery": 80, "peak_g": 15.0, "mean_g": 1.6, "peak_rot": 4.0,
              "accel_n": 250, "gyro_n": 250}, token=rk)
        went = time.time()
        chimes = (call(mac, "GET", "/api/status")[1] or {}).get("alarm", {}).get("chimes")

        silent = until(lambda: ((call(relay_url, "GET", "/state", token=keys["monitor"])[1] or {})
                                .get("dana", {}).get("incident") or {}).get("state") == "sos",
                       seconds=60, step=1)
        took = time.time() - went
        status, body = call(relay_url, "GET", "/state", token=keys["monitor"])
        inc = body["dana"]["incident"] or {}
        check("silence after a violent window is a crash alarm, at about 31 s",
              silent is True and inc.get("kind") == "silence" and took < 40, f"{took:.0f}s {json.dumps(inc)}")
        check("a driveway bump at walking pace, then silence, raises nothing",
              body["jack"]["incident"] is None, json.dumps(body["jack"].get("incident")))
        check("and the quiet rider is reported as quiet, not as crashed",
              body["jack"]["last_seen_s"] > 20, str(body["jack"]["last_seen_s"]))

        # His signal loss, as the Mac sees it: shown after a minute, alerted after two.
        shown = until(lambda: ((call(mac, "GET", "/api/status")[1] or {})
                               .get("riders", {}).get("jack", {}).get("signal")), seconds=15, step=1)
        check("the Mac is told of his signal loss after a minute (here 35 s), since his last packet",
              bool(shown) and shown.get("alert") is False
              and shown.get("since") == body["jack"]["last_received_at"], json.dumps(shown))
        alerted = until(lambda: ((call(mac, "GET", "/api/status")[1] or {})
                                 .get("riders", {}).get("jack", {}).get("signal") or {}).get("alert"),
                        seconds=15, step=1)
        check("and alerted after two (here 45 s)", alerted is True)
        time.sleep(3)                                      # several polls go by
        status, body = call(mac, "GET", "/api/status")
        check("the Mac chimes once for it, however many polls go by",
              body["alarm"]["chimes"] == (chimes or 0) + 1, f"{chimes} -> {body['alarm']['chimes']}")
        sig = body["riders"]["dana"].get("signal") or {}
        check("her signal loss gives way to her crash alarm: no alert, no chime of its own",
              sig.get("lost_s", 0) >= 45 and sig.get("alert") is False, json.dumps(sig))
        check("still nothing raised for him", body["riders"]["jack"]["incident"] is None)

        # A minute of nothing has to say why, rather than leaving a frozen dot; two
        # minutes of it says it is an alert and not an incident.
        entries = (call(mac, "GET", "/api/events?after=0")[1] or {}).get("entries", [])
        check("a minute of silence is reported, with what he was last doing",
              any(e["tag"] == "signal" and e.get("rider") == "jack" and "last seen at 2 km/h" in e["message"]
                  for e in entries))
        check("and two minutes of it, once, as an alert and not an incident",
              sum(1 for e in entries if e["tag"] == "no-signal" and e.get("rider") == "jack"
                  and "not an incident" in e["message"]) == 1)
        check("nothing of the kind is said for her: hers is a crash",
              not any(e["tag"] in ("signal", "no-signal") and e.get("rider") == "dana"
                      and e["ts"] >= datetime.fromtimestamp(went, timezone.utc).isoformat() for e in entries))

        # He comes back.
        call(relay_url, "POST", "/position",
             {"rider": "jack", "lat": lat, "lon": lon, "speed": 20.0, "accuracy": 5,
              "battery": 79, "peak_g": 1.5, "mean_g": 1.0, "peak_rot": 0.4,
              "accel_n": 250, "gyro_n": 250}, token=rk)
        cleared = until(lambda: "signal" in ((call(mac, "GET", "/api/status")[1] or {})
                                              .get("riders", {}).get("jack", {}))
                        and (call(mac, "GET", "/api/status")[1] or {})["riders"]["jack"]["signal"] is None,
                        seconds=6, step=0.5)
        check("his first packet clears his signal loss", cleared is True)
        back = until(lambda: [e for e in (call(mac, "GET", "/api/events?after=0")[1] or {})
                              .get("entries", []) if e["tag"] == "signal-ok" and e.get("rider") == "jack"],
                     seconds=6, step=0.5)
        check("and the log says how long he went unheard",
              bool(back) and back[-1]["message"].startswith("signal back after 0:"), json.dumps(back)[:160])

        # Her phone comes back too — but a crash alarm is not cleared by a packet. It
        # needs both halves, the rider's and an Observer's (design.md 2026-09-15).
        call(relay_url, "POST", "/position",
             {"rider": "dana", "lat": lat, "lon": lon, "speed": 58.0, "accuracy": 5,
              "battery": 79, "peak_g": 2.0, "mean_g": 1.0, "peak_rot": 0.5,
              "accel_n": 250, "gyro_n": 250}, token=jk)
        status, body = call(relay_url, "GET", "/state", token=keys["monitor"])
        check("her phone coming back does not clear a crash alarm",
              (body["dana"]["incident"] or {}).get("state") == "sos", json.dumps(body["dana"]["incident"]))
        call(relay_url, "POST", "/incident/observer-close", {"incident_id": inc.get("id")}, token=rk)
        call(relay_url, "POST", "/incident/resolve",
             {"incident_id": inc.get("id"), "rider": "dana", "resolution": "ok"}, token=jk)
        status, body = call(relay_url, "GET", "/state", token=keys["monitor"])
        check("both halves close it", body["dana"]["incident"] is None, json.dumps(body["dana"]["incident"]))

        call(relay_url, "POST", "/trip/end", {"rider": "dana"}, token=jk)
        call(relay_url, "POST", "/trip/end", {"rider": "jack"}, token=rk)
        time.sleep(1)

        # ---------------------------------------------------------------- the drill
        # A rehearsal must behave like the real thing on this Mac and reach nothing else.
        status, body = call(mac, "POST", "/api/drill", {}, headers={"X-Moto": "1"})
        blocking = {r: (s or {}).get("incident") for r, s in
                    ((call(relay_url, "GET", "/state", token=keys["monitor"])[1]) or {}).items()}
        check("a drill can be raised from the app",
              status == 200 and (body.get("incident") or {}).get("drill") is True,
              f"got {status} {body} | open: {json.dumps(blocking)[:300]}")
        drill_id = body["incident"]["id"]
        check("a drill id can never collide with a relay id", drill_id < 0, str(drill_id))
        status, body = call(mac, "GET", "/api/status")
        inc = body["riders"]["jack"]["incident"]
        check("the drill shows as checking first, with no alarm",
              inc["display"] == "pending" and body["alarm"]["sounding"] is False, json.dumps(inc))
        sounding = until(lambda: (call(mac, "GET", "/api/status")[1] or {})
                         .get("alarm", {}).get("sounding"), seconds=20)
        check("the drill escalates and the alarm sounds", sounding is True)
        reason = until(lambda: (call(mac, "GET", "/api/status")[1] or {})
                       .get("alarm", {}).get("reason") == "incident", seconds=10)
        check("the incident owns the alarm, whatever started it", reason is True)
        status, body = call(mac, "GET", "/api/status")
        check("the drill turns the pane red and flashes the icon",
              body["riders"]["jack"]["incident"]["display"] == "red" and body["attention"] == "red")
        check("the drill box has a position and forces to show",
              body["riders"]["jack"]["incident"]["last_position"] is not None
              and body["riders"]["jack"]["incident"]["g"] > 0)
        status, body = call(mac, "POST", f"/api/incident/{drill_id}/force-close",
                            {"confirm": "close without rider confirmation"}, headers={"X-Moto": "1"})
        check("a drill cannot be force-closed like a real incident", status == 400, f"got {status}")
        # The test-alarm button must never be able to quiet a real alarm: only a
        # silence can, and a silence is recorded and shared with the Observer phone.
        status, body = call(mac, "POST", "/api/alarm/test", {}, headers={"X-Moto": "1"})
        check("Test alarm is refused while an incident is sounding", status == 409, f"got {status} {body}")
        status, body = call(mac, "GET", "/api/status")
        check("the incident alarm is still sounding after that",
              body["alarm"]["sounding"] is True and body["alarm"]["reason"] == "incident",
              json.dumps(body["alarm"]))
        status, body = call(mac, "POST", f"/api/incident/{drill_id}/silence", {},
                            headers={"X-Moto": "1"})
        check("silencing a drill stops the sound", status == 200 and body.get("drill") is True)
        status, body = call(mac, "GET", "/api/status")
        check("but the drill stays open until it is closed",
              body["alarm"]["sounding"] is False and body["riders"]["jack"]["incident"] is not None)
        status, body = call(mac, "POST", f"/api/incident/{drill_id}/close", {},
                            headers={"X-Moto": "1"})
        check("closing the drill ends it", status == 200 and body.get("drill") is True)
        status, body = call(mac, "GET", "/api/status")
        check("nothing is left on screen or flashing",
              body["riders"]["jack"]["incident"] is None and body["attention"] is None
              and body["drill"] is False)
        status, body = call(relay_url, "GET", "/state", token=keys["monitor"])
        check("the relay never heard about the drill",
              all(r.get("incident") is None for r in body.values()), json.dumps(body)[:200])
        with sqlite3.connect(work / "moto.db") as db_probe:
            left = db_probe.execute("SELECT COUNT(*) FROM incidents WHERE id < 0").fetchone()[0]
        check("and nothing about it was archived", left == 0, str(left))

        # a drill must never cover a real ride
        call(relay_url, "POST", "/trip/start", {"rider": "jack"}, token=rk)
        time.sleep(1.5)
        status, body = call(mac, "POST", "/api/drill", {}, headers={"X-Moto": "1"})
        check("a drill is refused while someone is riding", status == 409, f"got {status} {body}")
        call(relay_url, "POST", "/trip/end", {"rider": "jack"}, token=rk)
        time.sleep(1.5)

        # ------------------------------------------------- replay
        # A ride has to be watchable again afterwards, which means the archive keeps
        # every position for ever (Jack, 2026-09-16) and can hand a whole trip back.
        status, body = call(mac, "GET", "/api/trip-days")
        days = body.get("days") or []
        check("the archive knows which days have a ride", bool(days), json.dumps(body)[:120])
        day = days[0]["day"] if days else ""
        status, body = call(mac, "GET", f"/api/trips?day={day}")
        trips = body.get("trips") or []
        check("and which rides were on one of them", bool(trips), json.dumps(body)[:120])
        # Each pane's Open logs button belongs to that rider: their days, their
        # rides, their folder, and nobody else's (Jack, 2026-09-20).
        status, mine = call(mac, "GET", f"/api/trips?day={day}&rider=jack")
        status, hers = call(mac, "GET", f"/api/trips?day={day}&rider=dana")
        jack_rides = mine.get("trips") or []
        her_rides = hers.get("trips") or []
        check("Open logs under a rider lists only that rider's rides",
              bool(jack_rides) and bool(her_rides)
              and all(t["rider"] == "jack" for t in jack_rides)
              and all(t["rider"] == "dana" for t in her_rides)
              and len(jack_rides) + len(her_rides) == len(trips),
              json.dumps({"jack": len(jack_rides), "dana": len(her_rides), "both": len(trips)}))
        status, her_days = call(mac, "GET", "/api/trip-days?rider=dana")
        her_day_list = her_days.get("days") or []
        check("and only the days that rider rode",
              bool(her_day_list) and {d["day"] for d in her_day_list} <= {d["day"] for d in days}
              and next((d["trips"] for d in her_day_list if d["day"] == day), 0) == len(her_rides),
              json.dumps(her_day_list)[:160])
        status, body = call(mac, "GET", f"/api/replay?trip_id={trips[0]['trip_id']}"
                            if trips else "/api/replay?trip_id=0")
        check("a whole trip comes back for replay",
              status == 200 and len(body.get("frames") or []) >= 5, f"got {status}")
        check("with the positions carrying what the map and pane need",
              all(k in (body.get("frames") or [{}])[0] for k in ("lat", "lon", "speed", "received_at")),
              json.dumps((body.get("frames") or [{}])[0])[:160])
        status, _ = call(mac, "GET", "/api/replay?trip_id=999999")
        check("asking for a trip that is not there says so", status == 404, f"got {status}")

        with sqlite3.connect(work / "moto.db") as db_probe:
            kept = db_probe.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        removed = probe(["prune"], mon_env).get("removed")
        with sqlite3.connect(work / "moto.db") as db_probe:
            still = db_probe.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        check("pruning no longer throws rides away", removed == 0 and still == kept,
              f"removed {removed}, {kept} -> {still}")

        # ------------------------------------------------- force-ending a dead trip
        # A phone that can never send "end trip" — flat battery, broken screen, a lift
        # home — would otherwise leave the trip open for ever (Jack, 2026-09-16).
        call(relay_url, "POST", "/trip/start", {"rider": "dana"}, token=jk)
        status, body = call(relay_url, "POST", "/trip/force-end", {"rider": "dana"}, token=jk)
        check("a rider cannot force-end their own trip", status == 403, f"got {status}")
        # Nor anyone else's: only the phone in a rider's hand ends that rider's trip
        # (Jack, 2026-09-24).
        status, body = call(relay_url, "POST", "/trip/force-end", {"rider": "dana"}, token=rk)
        check("the other rider's phone cannot force-end it either", status == 403, f"got {status}")
        status, body = call(relay_url, "POST", "/trip/force-end", {"rider": "dana"},
                            token=keys["monitor"])
        check("the Mac can end a trip the phone cannot", status == 200 and body.get("forced") is True,
              f"got {status} {body}")
        status, body = call(relay_url, "GET", "/state", token=keys["monitor"])
        check("and the rider goes back to asleep with no trip",
              body["dana"]["trip_id"] is None and body["dana"]["state"] == "sleep",
              json.dumps(body["dana"])[:120])
        status, body = call(relay_url, "POST", "/trip/force-end", {"rider": "dana"},
                            token=keys["monitor"])
        check("ending a trip that is already over says so", status == 409, f"got {status}")

        # ------------------------------------------------- a second Mac (Dana's)
        # It pairs itself with a code, and is a viewer: no Mac but the archivist may
        # acknowledge the outbox, or the history would be split between them.
        second_env = dict(mon_env, MONITOR_SUPPORT=str(work / "second-support"),
                          MONITOR_KEY=str(work / "second.key"),
                          MONITOR_ARCHIVE=str(work / "second.db"),
                          MONITOR_LOGS=str(work / "second-logs"))
        her_mac = Mac(second_env)
        procs.append(her_mac.proc)
        wait_for(her_mac, "/api/status")
        status, body = call(her_mac, "GET", "/api/status")
        check("a Mac with no key says it is not paired", body["paired"] is False, json.dumps(body["paired"]))

        out = subprocess.run([python, str(ROOT / "relay/relayctl.py"), "pair", "--role", "monitor",
                              "--name", "her Mac", "--json"], env=ctl_env, capture_output=True, text=True)
        code = json.loads(out.stdout)["code"]
        status, body = call(her_mac, "POST", "/api/pair", {"code": code, "name": "her Mac"},
                            headers={"X-Moto": "1"})
        check("the second Mac pairs itself with the code", status == 200 and body.get("ok") is True,
              f"got {status} {body}")
        check("and it is told it is a viewer", body.get("archivist") is False, json.dumps(body))
        second_key = (work / "second.key").read_text().strip()
        check("its key is stored for it", second_key.startswith("mt_"))
        check("the key file is private", oct((work / "second.key").stat().st_mode)[-3:] == "600")

        status, _ = call(relay_url, "GET", "/sync", token=second_key)
        check("a viewer Mac cannot read the archive feed", status == 403, f"got {status}")
        status, body = call(relay_url, "POST", "/sync/ack", {"upto": 1}, token=second_key)
        check("a viewer Mac cannot acknowledge anything", status == 403, f"got {status}")
        status, body = call(relay_url, "GET", "/state", token=second_key)
        check("but it reads the ride like any Observer", status == 200 and "jack" in (body or {}))
        check("and the relay gives it the last trip for its idle pane",
              (body or {}).get("jack", {}).get("previous_trip") is not None,
              json.dumps((body or {}).get("jack", {}).get("previous_trip")))
        status, body = call(her_mac, "GET", "/api/status")
        check("the second Mac shows itself as a viewer", body.get("archivist") is False, json.dumps(body.get("archivist")))
        status, body = call(mac, "GET", "/api/status")
        check("the first Mac still keeps the history", body.get("archivist") is True)

        # ------------------------------------------------- the page itself, in WebKit
        # The window's page, loaded by real WebKit from inside the app through its moto://
        # handler — no server anywhere. Run as the viewer Mac, so it draws the map
        # without taking the history from the Mac that keeps it.
        page_check = work / "page-check"
        built = subprocess.run(["bash", str(HERE / "build.sh"), "--page-check", str(page_check)],
                               capture_output=True, text=True)
        page = {"error": (built.stderr or built.stdout)[-400:]}
        # Someone out on a ride, so the page draws a map: an idle pane shows the last trip.
        call(relay_url, "POST", "/trip/start", {"rider": "jack"}, token=rk)
        call(relay_url, "POST", "/position",
             {"rider": "jack", "lat": lat, "lon": lon, "speed": 40.0, "accuracy": 5, "battery": 80,
              "peak_g": 1.2, "mean_g": 1.0, "peak_rot": 0.3, "accel_n": 250, "gyro_n": 250}, token=rk)
        if built.returncode == 0:
            try:
                out = subprocess.run([str(page_check)], env=dict(
                    second_env, MONITOR_SUPPORT=str(work / "page-support"),
                    MONITOR_ARCHIVE=str(work / "page.db"), MONITOR_LOGS=str(work / "page-logs")),
                    capture_output=True, text=True, timeout=90)
                page = json.loads(out.stdout.strip().splitlines()[-1])
            except (subprocess.TimeoutExpired, ValueError, IndexError) as e:
                page = {"error": repr(e)}
        call(relay_url, "POST", "/trip/end", {"rider": "jack"}, token=rk)
        shown = json.dumps(page)[:300]
        check("the page loads in WebKit from inside the app, with no script errors",
              page.get("status") == 200 and page.get("title") == "moto-tracker" and page.get("errors") == []
              and page.get("leaflet") is True, shown)
        check("its POSTs reach the app with their body",
              page.get("post") == 404 and (page.get("postBody") or {}).get("detail") == "no rider 'nobody'", shown)
        check("a POST without the page's own header is refused", page.get("unsigned") == 403, shown)
        check("the map draws, its tiles fetched by the app",
              (page.get("map") or 0) >= 1 and page.get("tile") == 200 and page.get("tileType") == "image/png", shown)
        check("and the page shows the relay live", page.get("srvtext") == "Server live", shown)

        # ---------------------------------------------------------------- tiles
        status, data = fetch_bytes(mac, "/tiles/map/12/1032/1890.png?style=night")
        check("map tiles are proxied", status == 200 and data[:8] == b"\x89PNG\r\n\x1a\n",
              f"got {status} {data[:40]!r}")
        status, data = fetch_bytes(mac, "/tiles/traffic/12/1032/1890.png?style=relative0-dark")
        check("traffic tiles are proxied", status == 200 and data[:8] == b"\x89PNG\r\n\x1a\n",
              f"got {status} {data[:40]!r}")
        cached = list((work / "tiles").rglob("*.png"))
        if REAL_TOMTOM_KEY.exists():
            check("tiles come from TomTom and are cached on disk", len(cached) >= 2, str(len(cached)))
            check("a cached tile is a real map tile, not a placeholder",
                  bool(cached) and max(p.stat().st_size for p in cached) > 1000,
                  str([p.stat().st_size for p in cached]))
        else:
            check("without a TomTom key the map still gets a tile", len(data) < 200 and not cached)

        return report()
    finally:
        for p in procs:
            p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
            err = p.stderr.read() if p.stderr else ""
            err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
            if err.strip() and ("Traceback" in err or "Error" in err or "Fatal" in err):
                print("\nstderr:\n" + err[-3000:])
        if not FAIL:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"\nworkspace kept for inspection: {work}")


def report() -> int:
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED: " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
