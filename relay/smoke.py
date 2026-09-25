#!/usr/bin/env python3
"""
Smoke test for the relay, run from the Mini over HTTPS by deploy.sh.

Trusts only our own CA. Uses only the smoke key and the __smoketest__ rider,
removes every live row it wrote, and proves that none of its writes entered the
outbox — so a deploy check can never leak into the Mini's archive, and can never
touch a real rider's data (the relay refuses a smoke key acting for anyone else,
which is also checked).

Standard library only, so it runs under the Mini's /usr/bin/python3.
Exit status 0 only if every check passes.
"""
import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SMOKE = "__smoketest__"


def seconds_until(iso) -> float:
    """How long the relay says it will wait before escalating."""
    try:
        return (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, TypeError):
        return -1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://203.0.113.10")
    ap.add_argument("--ca", required=True, help="our CA certificate")
    ap.add_argument("--token-file", required=True, help="file holding the smoke key")
    args = ap.parse_args()

    ctx = ssl.create_default_context(cafile=args.ca)
    with open(args.token_file) as f:
        token = f.read().strip()
    failures = []

    def call(method, path, body=None, key=True, timeout=15):
        headers, data = {}, None
        if isinstance(body, str):
            data, headers["Content-Type"] = body.encode(), "text/plain"
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        if key:
            headers["Authorization"] = "Bearer " + (token if key is True else key)
        req = urllib.request.Request(args.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
                text = r.read().decode()
                status = r.status
        except urllib.error.HTTPError as e:
            text, status = e.read().decode(), e.code
        try:
            return status, json.loads(text) if text else None
        except ValueError:
            return status, text

    def check(name, ok, detail=""):
        print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"   ({detail})"))
        if not ok:
            failures.append(name)
        return ok

    print(f"smoke test against {args.url}")

    s, b = call("GET", "/livez", key=False)
    check("liveness answers without a key", s == 200 and b == "ok", (s, b))
    s, b = call("GET", "/health", key=False)
    check("health refuses a request with no key", s == 401, (s, b))
    s, b = call("GET", "/health", key="mt_not-a-real-key")
    check("health refuses an unknown key", s == 401, (s, b))
    s, b = call("GET", "/health")
    if s == 401:
        print("  ✗ the relay does not know this smoke key (exit 3: deploy.sh rotates it)")
        return 3
    check("health accepts the smoke key", s == 200 and b.get("device") == "smoke", (s, b))

    s, b = call("POST", "/trip/start", {"rider": "jack"})
    check("smoke key cannot act for a real rider", s == 403, (s, b))
    s, b = call("POST", "/incident/resolve", {"rider": "dana", "incident_id": 1, "resolution": "ok"})
    check("smoke key cannot resolve for a real rider", s == 403, (s, b))

    # Start trip opens the trip off the bike, and Start ride is what gets on it
    # (Jack, 2026-09-20).
    s, b = call("POST", "/trip/start", {"rider": SMOKE, "riding": False})
    trip = b.get("trip_id") if isinstance(b, dict) else None
    check("trip starts", s == 200 and b.get("trip_opened") is True, (s, b))
    check("and it starts off the bike", b.get("state") == "offbike", (s, b))
    check("trip ids start above the Mini archive", isinstance(trip, int) and trip >= 100, trip)
    s, b = call("POST", "/ride/start", {"rider": SMOKE})
    check("Start ride gets on the bike without opening a second trip",
          s == 200 and b.get("state") == "riding" and b.get("trip_opened") is False
          and b.get("trip_id") == trip, (s, b))
    s, b = call("POST", "/position", {"rider": SMOKE, "lat": 13.0, "lon": -89.0, "speed": 12.0,
                                      "accuracy": 5.0, "battery": 90, "peak_g": 1.1, "mean_g": 1.0})
    check("position accepted", s == 200 and b == {"ok": True}, (s, b))

    s, b = call("GET", "/state")
    me = b.get(SMOKE, {}) if isinstance(b, dict) else {}
    check("state shows the riders", s == 200 and "jack" in b and "dana" in b, (s, b))
    check("state reflects the trip", me.get("state") == "riding" and me.get("trip_id") == trip, me)

    s, b = call("POST", "/ride/offbike", {"rider": SMOKE})
    check("off the bike", s == 200 and b.get("state") == "offbike", (s, b))
    s, b = call("POST", "/stop-answer", {"rider": SMOKE, "answer": "arrived"})
    check("stop answer", s == 200, (s, b))
    s, b = call("POST", "/ride/start", {"rider": SMOKE})
    check("riding resumes in the same trip", s == 200 and b.get("trip_opened") is False
          and b.get("trip_id") == trip, (s, b))

    # ---- baselines and the silence watchdog (relay-5)
    s, b = call("POST", "/baseline", {"rider": SMOKE, "impact_g": 11.0, "impact_rot": 8.0,
                                      "decel_kmh_s": 6.5, "moving_h": 3.1, "source": "smoke"})
    check("the Mini may push a baseline", s == 403, f"monitor only, got {s}")
    s, b = call("GET", f"/baseline?rider={SMOKE}")
    check("a rider may read its own baseline", s in (200, 404), f"got {s}")
    s, b = call("GET", "/baseline?rider=jack")
    check("but never another rider's", s == 403, f"got {s}")
    s, b = call("GET", "/map-key")
    check("the smoke key gets no map key", s == 403, f"got {s}")

    s, b = call("POST", "/incident/candidate", {"rider": SMOKE, "evidence": {"smoke": True}})
    inc = b.get("incident_id") if isinstance(b, dict) else None
    check("candidate raised, id above the archive", s == 200 and isinstance(inc, int) and inc >= 1000, (s, b))
    s, b = call("POST", "/incident/retract", {"rider": SMOKE, "incident_id": inc})
    check("candidate retracted", s == 200 and b.get("ok") is True, (s, b))

    # Escalation and the two-sided close (design.md 2026-09-15).
    def open_incident():
        st, body = call("GET", "/state")
        return ((body or {}).get(SMOKE) or {}).get("incident") or {} if st == 200 else {}

    s, b = call("POST", "/incident/candidate",
                {"rider": SMOKE, "confirm_window_s": 1, "evidence": {"peak_g": 11.5, "peak_rot": 7.2}})
    inc2 = b.get("incident_id") if isinstance(b, dict) else None
    check("a candidate shows as pending first", open_incident().get("display") == "pending", open_incident())
    # Asked for one second and given thirty: every candidate waits for the ride to
    # prove itself ordinary before anyone is woken (Jack, 2026-09-16). This is the
    # real relay, so this is the real floor — worth the wait once per deploy.
    waited = seconds_until(b.get("escalate_at"))
    check("a one-second confirm window is raised to the thirty-second floor",
          waited >= 25, f"escalates in {waited:.0f}s")
    time.sleep(max(2.6, waited + 1.5))
    view = open_incident()
    check("an unretracted candidate escalates to an open incident", view.get("id") == inc2
          and view.get("state") == "sos" and view.get("display") == "red", view)
    check("the incident view carries the recorded impact and a position",
          view.get("g") == 11.5 and view.get("rot") == 7.2 and view.get("last_position"), view)
    s, b = call("POST", "/incident/silence", {"incident_id": inc2})
    view = open_incident()
    check("silencing is shared and does not close it", s == 200 and view.get("silenced_at")
          and view.get("state") == "sos", (s, b, view))
    s, b = call("POST", "/incident/resolve", {"rider": SMOKE, "incident_id": inc2, "resolution": "ok"})
    view = open_incident()
    check("the rider's I'm OK leaves it open, shown yellow", s == 200 and b.get("waiting_for") == "observer"
          and view.get("state") == "sos" and view.get("display") == "yellow", (s, b, view))
    s, b = call("POST", "/incident/observer-close", {"incident_id": inc2})
    check("the Observer's close after the rider's closes it", s == 200 and b.get("state") == "closed"
          and not open_incident(), (s, b, open_incident()))

    s, b = call("POST", "/stop-answer", {"rider": SMOKE, "answer": "help"})
    view = open_incident()
    inc3 = view.get("id")
    check("I need help opens an incident", s == 200 and view.get("kind") == "help"
          and view.get("display") == "red", (s, b, view))
    # One open incident per rider (2026-09-18): pressed again, or a crash reported
    # during it, it is the same incident — not another one to find and close.
    s, b = call("POST", "/stop-answer", {"rider": SMOKE, "answer": "help"})
    check("I need help pressed again is the same incident", s == 200 and b.get("incident_id") == inc3
          and open_incident().get("id") == inc3, (s, b, open_incident()))
    s, b = call("POST", "/incident/candidate", {"rider": SMOKE, "evidence": {"peak_g": 12.0}})
    view = open_incident()
    check("a crash report during it joins it", s == 200 and b.get("incident_id") == inc3
          and b.get("joined") is True and view.get("id") == inc3 and view.get("kind") == "help", (s, b, view))
    s, b = call("POST", "/incident/observer-close", {"incident_id": inc3})
    view = open_incident()
    check("the Observer's close alone leaves it open, red, waiting for the rider",
          s == 200 and b.get("waiting_for") == "rider" and view.get("display") == "red"
          and view.get("observer_closed_at"), (s, b, view))
    s, b = call("POST", "/incident/force-close", {"incident_id": inc3, "confirm": "yes"})
    check("force-close refuses without the exact confirmation", s == 400, (s, b))
    s, b = call("POST", "/incident/force-close", {"incident_id": inc3, "confirm": "close without rider confirmation"})
    check("force-close with the confirmation closes it, marked forced",
          s == 200 and b.get("forced") is True and not open_incident(), (s, b, open_incident()))
    # Once somebody has silenced it, I need help is a fresh alarm — never a quiet one.
    s, b = call("POST", "/stop-answer", {"rider": SMOKE, "answer": "help"})
    inc4 = b.get("incident_id") if isinstance(b, dict) else None
    call("POST", "/incident/silence", {"incident_id": inc4})
    s, b = call("POST", "/stop-answer", {"rider": SMOKE, "answer": "help"})
    inc5 = b.get("incident_id") if isinstance(b, dict) else None
    view = open_incident()
    check("after a silence, I need help replaces it with a fresh alarm",
          s == 200 and isinstance(inc5, int) and inc5 != inc4 and view.get("id") == inc5
          and not view.get("silenced_at") and view.get("display") == "red", (s, b, view))
    s, b = call("POST", "/incident/force-close", {"incident_id": inc5, "confirm": "close without rider confirmation"})
    check("and the one it replaced is not left open behind it", s == 200 and not open_incident(),
          (s, b, open_incident()))

    # The live stream: the first line of the backlog must arrive promptly.
    req = urllib.request.Request(args.url + "/events", headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            first = ""
            for _ in range(20):
                first = r.readline().decode()
                if first.startswith("data:"):
                    break
        check("event stream delivers", first.startswith("data:"), first[:80])
    except Exception as e:
        check("event stream delivers", False, repr(e))

    s, b = call("GET", "/sync")
    check("only the monitor key may read the outbox", s == 403, (s, b))
    s, b = call("POST", "/sync/ack", {"upto": 1})
    check("only the monitor key may acknowledge", s == 403, (s, b))
    s, b = call("POST", "/pair", {"code": "ZZZZ-ZZZZ"}, key=False)
    check("an invalid pairing code is refused", s == 403, (s, b))

    s, b = call("POST", f"/spike/log?rider={SMOKE}&build=smoke", "smoke log line\n")
    check("log upload accepted", s == 200, (s, b))
    # Only the phone in a rider's hand ends that rider's trip; only the Mac may force it
    # (Jack, 2026-09-24).
    s, b = call("POST", "/trip/force-end", {"rider": SMOKE})
    check("no device key but the Mac's may force-end a trip", s == 403, (s, b))
    s, b = call("POST", "/trip/end", {"rider": SMOKE})
    check("trip ends", s == 200 and b.get("trip_id") == trip, (s, b))

    s, b = call("GET", "/health")
    check("no smoke write reached the outbox", s == 200 and b.get("outbox_smoke") == 0, (s, b))

    s, b = call("POST", "/smoke/cleanup")
    check("cleanup", s == 200 and b.get("ok") is True, (s, b))
    s, b = call("GET", "/state")
    me = b.get(SMOKE, {}) if isinstance(b, dict) else {}
    check("cleanup left nothing behind", me.get("trip_id") is None and me.get("last_seen_s") is None, me)

    print("PASS" if not failures else f"FAIL: {len(failures)} check(s) failed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
