#!/usr/bin/env python3
"""
Dev only: a copy of the Mac app running against a fake ride, for looking at the
interface.

Starts a throwaway relay in a temporary directory, opens a second copy of the app
pointed at it (its window says "demo"; it keeps its own archive there too), then
rides a loop around San Salvador. Nothing here touches the VPS, the real archive,
the phones or the real app, and the alarm is muted unless --sound is given.

    /usr/bin/python3 app/demo.py                 # idle panes, then a ride
    /usr/bin/python3 app/demo.py --incident 25   # crash 25 s into the ride
    /usr/bin/python3 app/demo.py --idle          # previous-trip panes only
    /usr/bin/python3 app/demo.py --both          # Dana rides too (Hybrid)
    /usr/bin/python3 app/demo.py --both --incident 25 --who dana
    /usr/bin/python3 app/demo.py --both --drop 20   # Dana's data runs out 20 s in

It uses app/build/moto-tracker.app, building it first if it is not there.
"""
import argparse
import json
import math
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VENV = Path.home() / "Library/Application Support/moto-tracker/venv/bin/python"
SUPPORT = Path.home() / "Library/Application Support/moto-tracker"
CENTRE = (13.6989, -89.2200)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def call(base, method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text and text[:1] in "[{" else text)
    except Exception as e:
        return 0, repr(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--incident", type=float, default=None, help="seconds into the ride to crash")
    ap.add_argument("--idle", action="store_true", help="archive a finished trip and stop riding")
    ap.add_argument("--sound", action="store_true", help="let the alarm actually sound")
    ap.add_argument("--both", action="store_true", help="Dana rides too, on her own loop")
    ap.add_argument("--drop", type=float, default=None,
                    help="seconds into her ride that Dana stops reporting (out of data)")
    ap.add_argument("--who", default="jack", choices=["jack", "dana"], help="whose crash --incident raises")
    ap.add_argument("--app", default=str(HERE / "build/moto-tracker.app"), help="the built app to run")
    args = ap.parse_args()

    app = Path(args.app)
    if not (app / "Contents/MacOS/moto-tracker").exists():
        subprocess.run(["bash", str(HERE / "build.sh"), str(app)], check=True)

    python = str(VENV) if VENV.exists() else sys.executable
    work = Path(tempfile.mkdtemp(prefix="moto-demo-"))
    relay_port = free_port()
    relay_url = f"http://127.0.0.1:{relay_port}"
    procs = []

    relay_env = dict(os.environ, RELAY_DB=str(work / "relay.db"), RELAY_PORT=str(relay_port))
    procs.append(subprocess.Popen([python, str(ROOT / "relay/relay.py")], env=relay_env,
                                  stdout=subprocess.DEVNULL))
    for _ in range(60):
        if call(relay_url, "GET", "/livez")[0] == 200:
            break
        time.sleep(0.25)

    keys = {}
    for role, rider in (("monitor", None), ("rider", "jack"), ("rider", "dana")):
        cmd = [python, str(ROOT / "relay/relayctl.py"), "pair", "--role", role,
               "--name", f"demo {rider or role}", "--json"]
        if rider:
            cmd += ["--rider", rider]
        out = subprocess.run(cmd, env=dict(os.environ, RELAY_DB=str(work / "relay.db")),
                             capture_output=True, text=True)
        code = json.loads(out.stdout)["code"]
        keys[rider or role] = call(relay_url, "POST", "/pair", {"code": code, "name": f"demo {rider or role}"})[1]["key"]
    (work / "monitor.key").write_text(keys["monitor"])

    env = dict(os.environ, MONITOR_SUPPORT=str(work / "support"), MONITOR_RELAY_URL=relay_url,
               MONITOR_KEY=str(work / "monitor.key"), MONITOR_CA=str(work / "none.crt"),
               MONITOR_ARCHIVE=str(work / "moto.db"), MONITOR_LOGS=str(work / "logs"),
               MONITOR_TILE_CACHE=str(work / "tiles"), MONITOR_STATUS_FILE=str(work / "app-status.json"),
               MONITOR_TOMTOM_KEY=str(SUPPORT / "monitor-secrets/tomtom.key"),
               MONITOR_ALARM_MUTE="0" if args.sound else "1", MONITOR_OPEN_APP="0",
               MONITOR_DEVICES="0")
    procs.append(subprocess.Popen([str(app / "Contents/MacOS/moto-tracker")], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    print(f"app:      {app} (window titled \"moto-tracker — demo\")\nrelay:    {relay_url}\n"
          f"status:   {work / 'app-status.json'}\nworkspace {work}", flush=True)
    stop = threading.Event()

    def ride():
        rk = keys["jack"]
        # A finished trip first, so the idle panes have something to show.
        call(relay_url, "POST", "/trip/start", {"rider": "jack"}, token=rk)
        for i in range(12):
            angle = i / 12 * math.tau
            call(relay_url, "POST", "/position",
                 {"rider": "jack", "lat": CENTRE[0] + 0.01 * math.sin(angle),
                  "lon": CENTRE[1] + 0.01 * math.cos(angle), "speed": 55 + 12 * math.sin(angle * 3),
                  "accuracy": 4 + i % 3, "battery": 96 - i // 3, "peak_g": 1.05 + 0.4 * abs(math.sin(angle * 2)),
                  "mean_g": 1.0, "peak_rot": 0.3 + 0.5 * abs(math.cos(angle)), "accel_n": 250,
                  "gyro_n": 250}, token=rk)
        call(relay_url, "POST", "/trip/end", {"rider": "jack"}, token=rk)
        time.sleep(7)                       # let the Mini archive it
        if args.idle:
            return
        call(relay_url, "POST", "/trip/start", {"rider": "jack"}, token=rk)
        started, i = time.time(), 0
        crashed = False
        while not stop.is_set():
            elapsed = time.time() - started
            angle = elapsed / 90 * math.tau
            speed = 0 if crashed else max(0, 58 + 14 * math.sin(elapsed / 7))
            call(relay_url, "POST", "/position",
                 {"rider": "jack", "lat": CENTRE[0] + 0.012 * math.sin(angle),
                  "lon": CENTRE[1] + 0.012 * math.cos(angle), "speed": speed,
                  "accuracy": 4 + (i % 4), "battery": max(20, 95 - int(elapsed // 60)),
                  "peak_g": 1.0 if crashed else 1.05 + 0.5 * abs(math.sin(elapsed / 3)),
                  "mean_g": 1.0, "peak_rot": 0.1 if crashed else 0.4 + 0.6 * abs(math.cos(elapsed / 4)),
                  "accel_n": 250, "gyro_n": 250}, token=rk)
            if args.who == "jack" and args.incident and not crashed and elapsed >= args.incident:
                crashed = True
                call(relay_url, "POST", "/incident/candidate",
                     {"rider": "jack", "confirm_window_s": 8,
                      "evidence": {"peak_g": 11.3, "peak_rot": 7.8}}, token=rk)
                print("candidate raised; it escalates in 8 s unless retracted", flush=True)
            i += 1
            stop.wait(2.0)

    def ride_dana():
        # Both out at once (Jack, 2026-09-24): her own loop, east of his, a little slower.
        jk = keys["dana"]
        time.sleep(9)
        call(relay_url, "POST", "/trip/start", {"rider": "dana"}, token=jk)
        started, crashed = time.time(), False
        while not stop.is_set():
            elapsed = time.time() - started
            if args.drop is not None and elapsed >= args.drop:
                stop.wait(2.0)                 # still riding, but nothing gets through
                continue
            angle = -elapsed / 110 * math.tau
            call(relay_url, "POST", "/position",
                 {"rider": "dana", "lat": 13.6770 + 0.015 * math.sin(angle),
                  "lon": -89.1850 + 0.015 * math.cos(angle),
                  "speed": 0 if crashed else max(0, 44 + 10 * math.sin(elapsed / 9)),
                  "accuracy": 5, "battery": max(20, 88 - int(elapsed // 60)),
                  "peak_g": 1.0 if crashed else 1.04 + 0.3 * abs(math.sin(elapsed / 5)),
                  "mean_g": 1.0, "peak_rot": 0.1 if crashed else 0.3 + 0.4 * abs(math.cos(elapsed / 6)),
                  "accel_n": 250, "gyro_n": 250}, token=jk)
            if args.who == "dana" and args.incident and not crashed and elapsed >= args.incident:
                crashed = True
                call(relay_url, "POST", "/incident/candidate",
                     {"rider": "dana", "confirm_window_s": 8,
                      "evidence": {"peak_g": 12.1, "peak_rot": 6.9}}, token=jk)
                print("Dana's candidate raised; it escalates in 8 s unless retracted", flush=True)
            stop.wait(2.0)

    threading.Thread(target=ride, daemon=True).start()
    if args.both and not args.idle:
        threading.Thread(target=ride_dana, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for p in procs:
            p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
        print(f"\nstopped. workspace left at {work}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
