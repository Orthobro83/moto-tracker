#!/usr/bin/python3
"""
Sensor baseline report.

design.md makes the crash discriminator the *shape* of an event — spike, continued
chaos, stillness — and shape is meaningless without knowing what normal looks
like. This summarises what the sensors actually report, bucketed by speed, so
thresholds can be chosen from data rather than guessed.

Run it after a ride:

    ./baseline.py              every ride together
    ./baseline.py --list       what rides exist
    ./baseline.py --trip 4     just that one
    ./baseline.py --latest     the most recent trip
    --riding     on-bike telemetry only (recommended for thresholds)

Trips are separated in the database by trip_id, so there is never a reason to
wipe data to get a clean read — filter instead. Retention stays the rolling 30
days decided on 2026-09-08.
"""
import os
import sqlite3
import statistics
import sys

DB = os.path.expanduser("~/Library/Application Support/moto-tracker/moto.db")


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))]


def main():
    if not os.path.exists(DB):
        sys.exit(f"no database at {DB}")
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    args = sys.argv[1:]
    if "--list" in args:
        print(f"{'trip':>5}  {'rider':<9}{'started':<21}{'riding':>8}{'offbike':>9}{'sensors':>9}")
        for r in c.execute("SELECT * FROM trips ORDER BY id"):
            def cnt(extra="", p=()):
                return c.execute("SELECT COUNT(*) n FROM positions WHERE trip_id=?" + extra,
                                 (r["id"],) + p).fetchone()["n"]
            print(f"{r['id']:>5}  {r['rider_id']:<9}{r['started_at'][:19]:<21}"
                  f"{cnt(' AND state=?', ('riding',)):>8}"
                  f"{cnt(' AND state=?', ('offbike',)):>9}"
                  f"{cnt(' AND peak_g IS NOT NULL'):>9}")
        return

    where, params, scope = "peak_g IS NOT NULL", (), "all trips"
    if "--latest" in args:
        r = c.execute("SELECT id FROM trips ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            where += " AND trip_id = ?"; params = (r["id"],); scope = f"trip #{r['id']}"
    elif "--trip" in args:
        tid = int(args[args.index("--trip") + 1])
        where += " AND trip_id = ?"; params = (tid,); scope = f"trip #{tid}"
    if "--riding" in args:
        # A phone jostled in a bag at a cafe is trip data, but it says nothing
        # about riding and would distort any threshold derived from it.
        where += " AND state = 'riding'"; scope += ", on-bike only"

    rows = c.execute(
        "SELECT speed, peak_g, min_g, mean_g, rms_g, peak_rot, mean_rot,"
        f" accel_n, gyro_n, rider_id FROM positions WHERE {where}", params
    ).fetchall()
    print(f"scope: {scope}")

    if not rows:
        sys.exit("no sensor data yet — ride with spike-8 or later, then re-run")

    print(f"{len(rows)} ticks carrying sensor data\n")

    # sample-rate health first: everything below is worthless if the OS throttled
    ns = [r["accel_n"] for r in rows if r["accel_n"] is not None]
    gs = [r["gyro_n"] for r in rows if r["gyro_n"] is not None]
    if ns:
        # Derived, not assumed. The S21 FE delivers ~104 Hz accel and ~52 Hz gyro
        # at SENSOR_DELAY_GAME — roughly double what SENSOR_DELAY_GAME nominally
        # promises, and the two sensors run at different rates.
        med_n = statistics.median(ns)
        print(f"SAMPLE RATE (window is nominally 5 s)")
        print(f"  accel: median {med_n:.0f} per window  ~= {med_n/5:.0f} Hz"
              f"   min {min(ns)}   max {max(ns)}")
        if gs:
            med_g = statistics.median(gs)
            print(f"  gyro:  median {med_g:.0f} per window  ~= {med_g/5:.0f} Hz")
        floor = med_n * 0.4
        starved = sum(1 for n in ns if n < floor)
        print(f"  windows under 40% of median ({floor:.0f}): {starved} "
              f"({100*starved/len(ns):.1f}%)"
              f"{'   <-- sensors were throttled' if starved else ''}\n")

    buckets = [("stopped", 0, 1), ("walking/creep", 1, 15),
               ("town", 15, 50), ("highway", 50, 999)]
    print(f"{'speed band':<16}{'n':>6}{'peak g p50':>12}{'p95':>8}{'max':>8}"
          f"{'pk/mean p95':>13}{'rot p95':>10}{'rot max':>10}")
    for name, lo, hi in buckets:
        b = [r for r in rows if r["speed"] is not None and lo <= r["speed"] < hi]
        if not b:
            continue
        pg = [r["peak_g"] for r in b if r["peak_g"] is not None]
        pr = [r["peak_rot"] for r in b if r["peak_rot"] is not None]
        ratio = [r["peak_g"] / r["mean_g"] for r in b
                 if r["peak_g"] and r["mean_g"] and r["mean_g"] > 0.1]
        print(f"{name:<16}{len(b):>6}{pct(pg,50):>12.2f}{pct(pg,95):>8.2f}"
              f"{max(pg) if pg else 0:>8.2f}{pct(ratio,95):>13.2f}{pct(pr,95):>10.2f}"
              f"{max(pr) if pr else 0:>10.2f}")

    allpg = [r["peak_g"] for r in rows if r["peak_g"] is not None]
    print(f"\nOVERALL peak g:  p50 {pct(allpg,50):.2f}   p95 {pct(allpg,95):.2f}   "
          f"p99 {pct(allpg,99):.2f}   max {max(allpg):.2f}")
    print("\nAt rest a phone reads ~1.00 g, so these are resultant magnitudes, not\n"
          "deviations. A candidate threshold belongs well above the p99 of normal\n"
          "riding — pick it once a simulated crash has been recorded for comparison.")
    print("\npeak/mean is the shape proxy this per-window summary still carries:\n"
          "  high peak, mean near 1.0  -> one sharp spike. A pothole.\n"
          "  high peak, mean well above 1.0 -> sustained violence. A crash.\n"
          "Reference, 2026-09-10 hand-shake test: peak 10.8, mean 3.7, ratio 2.9,\n"
          "rotation 22 rad/s. At rest the same phone reads 1.0 / 1.0 / 1.0 / 0.0.")


if __name__ == "__main__":
    main()
