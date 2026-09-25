#!/usr/bin/env python3
"""
Both riders on a trip at once (Jack, 2026-09-24): Hybrid mode, as the relay sees it.

Until now one phone rode and the other observed. The relay was always per rider,
so this proves that it holds when both trips are open together: neither rider's
trip, telemetry, silence or incident leaks into the other's, and each rider's phone
remains the other's Observer while riding itself.

Starts a throwaway relay (plain HTTP, its own database, a 3-second confirm window)
and touches nothing real. The deploy smoke test cannot do this: it is confined to
one test rider on the VPS.

    /usr/bin/python3 relay/both_riding_test.py      # uses the Mini's venv for the relay
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = Path.home() / "Library/Application Support/moto-tracker/venv/bin/python"
PASS, FAIL = [], []


def check(label: str, ok: bool, detail="") -> bool:
    (PASS if ok else FAIL).append(label)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + str(detail)[:300]) if detail and not ok else ''}")
    return ok


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    python = str(VENV) if VENV.exists() else sys.executable
    work = Path(tempfile.mkdtemp(prefix="moto-both-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, RELAY_DB=str(work / "relay.db"), RELAY_HOST="127.0.0.1",
               RELAY_PORT=str(port), RELAY_MIN_CONFIRM_S="3")
    relay = subprocess.Popen([python, str(HERE / "relay.py")], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def call(method, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        head = {"Content-Type": "application/json"} if data else {}
        if token:
            head["Authorization"] = "Bearer " + token
        req = urllib.request.Request(base + path, data=data, method=method, headers=head)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                text = r.read().decode()
                return r.status, (json.loads(text) if text[:1] in "[{" else text)
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            try:
                return e.code, json.loads(text)
            except ValueError:
                return e.code, text
        except Exception as e:
            return 0, repr(e)

    try:
        for _ in range(80):
            if call("GET", "/livez")[0] == 200:
                break
            time.sleep(0.25)
        if not check("the test relay starts", call("GET", "/livez")[0] == 200):
            return report()

        keys = {}
        for role, rider, name in (("monitor", None, "test monitor"), ("rider", "jack", "jack phone"),
                                  ("rider", "dana", "dana phone")):
            args = [python, str(HERE / "relayctl.py"), "pair", "--role", role, "--name", name, "--json"]
            if rider:
                args += ["--rider", rider]
            out = subprocess.run(args, env=env, capture_output=True, text=True)
            _, body = call("POST", "/pair", {"code": json.loads(out.stdout)["code"], "name": name})
            keys[rider or role] = body["key"]
        rk, jk, mk = keys["jack"], keys["dana"], keys["monitor"]

        def state(token=mk):
            return call("GET", "/state", token=token)[1]

        def pos(rider, token, lat, speed, **extra):
            return call("POST", "/position", dict({"rider": rider, "lat": lat, "lon": -89.2,
                                                   "speed": speed, "accuracy": 5, "battery": 80,
                                                   "peak_g": 1.1, "mean_g": 1.0, "peak_rot": 0.3},
                                                  **extra), token)

        # ---------------------------------------------------------------- into Hybrid
        print("Observer, then Hybrid")
        s, j_trip = call("POST", "/trip/start", {"rider": "dana", "riding": True}, jk)
        st = state(rk)
        check("Dana rides; Jack's phone sees her trip and has none of its own (Observer)",
              st["dana"]["trip_id"] == j_trip["trip_id"] and st["jack"]["trip_id"] is None, st)

        # Jack opens a trip from the Observer drawer, off the bike.
        s, r_trip = call("POST", "/trip/start", {"rider": "jack", "riding": False}, rk)
        check("Jack can open a trip while Dana's is open", s == 200 and r_trip["trip_opened"] is True,
              (s, r_trip))
        check("the two trips are separate", r_trip["trip_id"] != j_trip["trip_id"], (r_trip, j_trip))
        for who, token in (("jack", rk), ("dana", jk), ("monitor", mk)):
            st = state(token)
            check(f"{who} sees both trips open (Hybrid)",
                  st["jack"]["trip_id"] == r_trip["trip_id"] and st["dana"]["trip_id"] == j_trip["trip_id"]
                  and st["jack"]["state"] == "offbike" and st["dana"]["state"] == "riding", st)
        s, b = call("POST", "/ride/start", {"rider": "jack"}, rk)
        check("Start ride from the drawer gets Jack on the bike in the same trip",
              b.get("state") == "riding" and b.get("trip_id") == r_trip["trip_id"], b)

        # ---------------------------------------------------------------- telemetry
        print("telemetry")
        for i in range(3):
            pos("jack", rk, 13.70 + i * 0.001, 60 + i)
            pos("dana", jk, 13.50 + i * 0.001, 40 + i)
        st = state()
        check("each rider's live position and speed are their own",
              round(st["jack"]["lat"], 3) == 13.702 and st["jack"]["speed"] == 62
              and round(st["dana"]["lat"], 3) == 13.502 and st["dana"]["speed"] == 42, st)
        s, b = pos("dana", rk, 13.0, 10)
        check("Jack's key still cannot post for Dana", s == 403, (s, b))

        s, feed = call("GET", "/sync", token=mk)
        positions = [e["payload"] for e in feed["entries"] if e["kind"] == "position"]
        wrong = [p for p in positions
                 if p["trip_id"] != {"jack": r_trip["trip_id"], "dana": j_trip["trip_id"]}[p["rider_id"]]]
        check("every position the Mini archives belongs to its own rider's trip",
              len(positions) == 6 and not wrong, wrong or len(positions))

        # Pausing one ride leaves the other alone.
        call("POST", "/ride/offbike", {"rider": "jack"}, rk)
        st = state()
        check("Jack pauses; Dana is still riding",
              st["jack"]["state"] == "offbike" and st["dana"]["state"] == "riding", st)
        call("POST", "/ride/start", {"rider": "jack"}, rk)

        # ---------------------------------------------------------------- incidents
        print("an incident while both ride")
        s, b = call("POST", "/incident/candidate",
                    {"rider": "jack", "confirm_window_s": 3, "evidence": {"peak_g": 14.0}}, rk)
        r_inc = b.get("incident_id")
        pos("dana", jk, 13.51, 45)
        time.sleep(5)
        st = state(jk)
        check("Jack's crash escalates and Dana's riding phone sees it",
              (st["jack"]["incident"] or {}).get("id") == r_inc
              and st["jack"]["incident"]["state"] == "sos", st["jack"]["incident"])
        check("Dana has no incident of her own", st["dana"]["incident"] is None, st["dana"]["incident"])
        s, b = call("POST", "/incident/silence", {"incident_id": r_inc}, jk)
        check("Dana, riding, can silence Jack's alarm", s == 200, (s, b))
        s, b = call("POST", "/incident/observer-close", {"incident_id": r_inc}, rk)
        check("Jack still cannot be his own Observer", s == 403, (s, b))
        s, b = call("POST", "/incident/observer-close", {"incident_id": r_inc}, jk)
        check("Dana, riding, closes her half", s == 200 and b.get("waiting_for") == "rider", (s, b))
        s, b = call("POST", "/incident/resolve", {"rider": "jack", "incident_id": r_inc, "resolution": "ok"}, rk)
        check("Jack's I'm OK then closes it", s == 200 and b.get("state") == "closed", (s, b))
        check("and both trips carry on", all(state()[r]["trip_id"] for r in ("jack", "dana")), state())

        print("an incident on each at once")
        s, b = call("POST", "/stop-answer", {"rider": "dana", "answer": "help"}, jk)
        j_help = b.get("incident_id")
        s, b = call("POST", "/incident/candidate",
                    {"rider": "jack", "confirm_window_s": 3, "evidence": {"peak_g": 13.0}}, rk)
        r_inc2 = b.get("incident_id")
        time.sleep(5)
        st = state()
        check("both incidents are open together, each on its own rider",
              (st["dana"]["incident"] or {}).get("id") == j_help
              and (st["jack"]["incident"] or {}).get("id") == r_inc2
              and st["dana"]["incident"]["kind"] == "help", (st["jack"]["incident"], st["dana"]["incident"]))
        call("POST", "/incident/silence", {"incident_id": j_help}, rk)
        st = state()
        check("silencing hers leaves his ringing",
              st["dana"]["incident"]["silenced_at"] and not st["jack"]["incident"]["silenced_at"], st)
        for inc, rider, token, other in ((j_help, "dana", jk, rk), (r_inc2, "jack", rk, jk)):
            call("POST", "/incident/observer-close", {"incident_id": inc}, other)
            call("POST", "/incident/resolve", {"rider": rider, "incident_id": inc, "resolution": "ok"}, token)
        st = state()
        check("each closes with the other as Observer",
              st["jack"]["incident"] is None and st["dana"]["incident"] is None, st)

        # ---------------------------------------------------------------- silence
        print("one goes quiet while the other rides on (about 20 s)")
        s, b = call("POST", "/incident/candidate",
                    {"rider": "dana", "confirm_window_s": 90, "evidence": {"peak_g": 12.5}}, jk)
        j_inc = b.get("incident_id")
        deadline = time.time() + 26
        escalated = False
        while time.time() < deadline:
            pos("jack", rk, 13.71, 55)              # Jack keeps reporting; Dana is silent
            inc = state()["dana"]["incident"] or {}
            if inc.get("id") == j_inc and inc.get("state") == "sos":
                escalated = True
                break
            time.sleep(2)
        check("Dana's silence cuts her 90 s window short, despite Jack's steady telemetry", escalated,
              state()["dana"]["incident"])
        check("Jack's steady telemetry raises nothing for him", state()["jack"]["incident"] is None,
              state()["jack"]["incident"])
        call("POST", "/incident/observer-close", {"incident_id": j_inc}, rk)
        call("POST", "/incident/resolve", {"rider": "dana", "incident_id": j_inc, "resolution": "false_alarm"}, jk)

        # ---------------------------------------------------------------- a crash that silences the phone
        # Losing the signal right after a violent reading, or right after a collapse in
        # speed, is a crash that took the phone with it: it must still end in an alarm,
        # not in a grey "Signal lost" (Jack, 2026-09-24). Each with the other rider
        # riding on normally beside it.
        def silent_crash(label, victim, vtoken, other, otoken, packets):
            print(f"{label} (about 35 s)")
            for p in packets:
                call("POST", "/position", dict({"rider": victim, "lat": 13.60, "lon": -89.2,
                                                "accuracy": 5, "battery": 70, "mean_g": 1.0},
                                               **p), vtoken)
            deadline = time.time() + 50
            seen_pending, inc = False, {}
            while time.time() < deadline:
                pos(other, otoken, 13.72, 55)       # the other rider keeps riding and reporting
                inc = state()[victim]["incident"] or {}
                seen_pending = seen_pending or inc.get("display") == "pending"
                if inc.get("state") == "sos":
                    break
                time.sleep(2)
            check(f"{label}: a candidate is raised for the silence",
                  seen_pending or inc.get("kind") == "silence", inc)
            check(f"{label}: it escalates to a crash alarm",
                  inc.get("state") == "sos" and inc.get("display") == "red" and inc.get("kind") == "silence", inc)
            check(f"{label}: the other rider has nothing raised", state()[other]["incident"] is None,
                  state()[other]["incident"])
            call("POST", "/incident/observer-close", {"incident_id": inc.get("id")}, otoken)
            call("POST", "/incident/resolve", {"rider": victim, "incident_id": inc.get("id"),
                                               "resolution": "ok"}, vtoken)
            return inc

        inc = silent_crash("a hard impact, then silence", "dana", jk, "jack", rk, [
            {"speed": 62, "peak_g": 1.3, "peak_rot": 0.4},
            {"speed": 61, "peak_g": 1.2, "peak_rot": 0.5},
            {"speed": 58, "peak_g": 17.5, "peak_rot": 11.0},     # out of bounds
        ])
        check("the alarm says what it saw: the impact",
              inc.get("g") == 17.5 and inc.get("rot") == 11.0 and inc.get("speed_before") == 62, inc)
        inc = silent_crash("a collapse in speed, then silence", "jack", rk, "dana", jk, [
            {"speed": 72, "peak_g": 1.3, "peak_rot": 0.4},
            {"speed": 70, "peak_g": 1.3, "peak_rot": 0.4},
            {"speed": 38, "peak_g": 1.3, "peak_rot": 0.4},       # nothing violent recorded,
            {"speed": 9, "peak_g": 1.3, "peak_rot": 0.4},        # just the bike stopping
            {"speed": 3, "peak_g": 1.3, "peak_rot": 0.4},        # hard, then nothing
        ])
        check("the alarm says what it saw: the deceleration",
              inc.get("speed_before") == 72 and (inc.get("speed_after") or 0) <= 8, inc)

        # ---------------------------------------------------------------- back out of Hybrid
        print("back out of Hybrid")
        s, b = call("POST", "/trip/end", {"rider": "dana"}, jk)
        st = state(rk)
        check("Dana ends her trip; Jack's carries on (Riding)",
              st["dana"]["trip_id"] is None and st["jack"]["trip_id"] == r_trip["trip_id"], st)
        # Only the phone in a rider's hand ends that rider's trip (Jack, 2026-09-24).
        call("POST", "/trip/start", {"rider": "dana", "riding": False}, jk)
        s, b = call("POST", "/trip/force-end", {"rider": "dana"}, rk)
        check("a riding phone cannot end the other's trip", s == 403, (s, b))
        s, b = call("POST", "/trip/end", {"rider": "dana"}, rk)
        check("not even with the ordinary End trip", s == 403, (s, b))
        s, b = call("POST", "/trip/force-end", {"rider": "jack"}, rk)
        check("nor force-end its own", s == 403, (s, b))
        check("so Dana's trip is still open", state()["dana"]["trip_id"] is not None, state()["dana"])
        s, b = call("POST", "/trip/force-end", {"rider": "dana"}, mk)
        check("only the Mac can end it for her", s == 200 and b.get("forced") is True, (s, b))
        call("POST", "/trip/end", {"rider": "jack"}, rk)
        st = state()
        check("both at home (Idle)", st["jack"]["trip_id"] is None and st["dana"]["trip_id"] is None, st)
    finally:
        relay.terminate()
        try:
            relay.wait(5)
        except subprocess.TimeoutExpired:
            relay.kill()
        shutil.rmtree(work, ignore_errors=True)
    return report()


def report() -> int:
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED: " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
