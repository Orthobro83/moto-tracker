#!/usr/bin/python3
"""
Exercise every endpoint against the running server.

Exists because on 2026-09-10 a table rename broke /health while leaving /state
working. The trip lifecycle had been tested thoroughly; /health had not been
touched, so it was not re-checked — and the dashboard fetched both together, so
one dead endpoint made a healthy server look entirely down.

Run after every deploy. Uses rider 'dana' and cleans up after itself.
"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

DB = os.path.expanduser("~/Library/Application Support/moto-tracker/moto.db")
RIDER = "__smoketest__"

BASE = "http://127.0.0.1:8088"
fails = []


def call(method, path, body=None, expect=200):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            code, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    except Exception as e:
        fails.append(f"{method} {path}: {e}")
        print(f"  FAIL  {method:5} {path:24} {e}")
        return None
    ok = code == expect
    if not ok:
        fails.append(f"{method} {path}: got {code}, want {expect}")
    print(f"  {'ok  ' if ok else 'FAIL'}  {method:5} {path:24} {code}")
    try:
        out = json.loads(payload)
    except Exception:
        return None
    tid = out.get("trip_id") if isinstance(out, dict) else None
    if tid is not None and tid not in created["trips"]:
        created["trips"].append(tid)
    return out


def ensure_rider():
    """A rider that exists only for this test, so cleanup can never touch a real one."""
    c = sqlite3.connect(DB)
    c.execute("INSERT OR IGNORE INTO riders (id, name, mesh_ip, state)"
              " VALUES (?,?,NULL,'sleep')", (RIDER, "smoke test"))
    c.commit()
    c.close()


def cleanup():
    """
    Remove exactly what this run created, addressed by id.

    On 2026-09-11 this function deleted a real recorded ride. It ran as rider
    'dana' and cleaned up with "DELETE ... WHERE rider_id = dana", which
    took her actual trip with it — and deploy.sh runs this automatically, so a
    routine deploy was enough to destroy the data.

    Two rules came out of that, and both matter more than the convenience they
    cost: the test never borrows a real identity, and it never deletes by
    anything except the ids it created itself.
    """
    if not os.path.exists(DB) or not created["trips"]:
        return
    c = sqlite3.connect(DB)
    for t in created["trips"]:
        c.execute("DELETE FROM positions WHERE trip_id=?", (t,))
        c.execute("DELETE FROM incidents WHERE trip_id=?", (t,))
        c.execute("DELETE FROM trips WHERE id=?", (t,))
    c.execute("DELETE FROM positions WHERE rider_id=?", (RIDER,))
    c.execute("DELETE FROM incidents WHERE rider_id=?", (RIDER,))
    c.execute("DELETE FROM riders WHERE id=?", (RIDER,))
    c.commit()
    c.close()
    logs = os.path.dirname(DB) + "/spike-logs"
    for f in os.listdir(logs):
        if "smoke" in f:
            os.remove(logs + "/" + f)
    print(f"  cleaned up trips {created['trips']} (by id, rider {RIDER})")


created = {"trips": []}
ensure_rider()
R = {"rider": RIDER}
print("reads")
call("GET", "/health")
call("GET", "/state")
call("GET", "/")

print("\ntrip lifecycle")
call("POST", "/trip/start", R)
call("POST", "/position", {**R, "lat": 13.1, "lon": -89.1, "speed": 40,
                           "peak_g": 2.2, "mean_g": 1.1, "peak_rot": 1.0,
                           "accel_n": 550, "gyro_n": 275})
call("POST", "/stop-answer", {**R, "answer": "traffic"})
call("POST", "/ride/offbike", R)
call("POST", "/position", {**R, "lat": 13.1, "lon": -89.1, "speed": 1})
call("POST", "/trip/start", R)

print("\nincidents")
c = call("POST", "/incident/candidate", {**R, "confirm_window_s": 20,
                                         "evidence": {"peak_g": 9.9}})
if c:
    call("POST", "/incident/retract", {**R, "incident_id": c["incident_id"]})
h = call("POST", "/stop-answer", {**R, "answer": "help"})
# resolve is rider-only and enforced server-side
inc = call("GET", "/state")


print("\nguards (these SHOULD reject)")
call("POST", "/trip/start", {"rider": "nobody"}, expect=404)
call("POST", "/stop-answer", {**R, "answer": "banana"}, expect=400)
# End trip from riding is ALLOWED — you ride into the driveway and press it.
# Kept here so a future change back to refusing is noticed rather than assumed.
call("POST", "/ride/sleep", R)
call("POST", "/trip/start", R)   # reopen for the remaining checks

print("\nlegacy aliases the spike still calls")
call("POST", "/ride/start", R)
call("POST", "/ride/end", R)

print("\nspike support")
call("POST", "/spike/log?build=smoke&rider=dana", None)

call("POST", "/trip/end", R)

print()
cleanup()
print()
if fails:
    print(f"{len(fails)} FAILURES:")
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("all endpoints OK")
