#!/usr/bin/env python3
"""
What normal riding looks like, per rider, from the archive.

This is the input to every crash-detection threshold. It exists as a script rather
than a one-off answer because the baseline is not a calibration done once: the real
app re-runs this over the growing archive and moves with each rider (design.md).

    /usr/bin/python3 analysis/baseline.py [--db PATH] [--since 2026-09-01]

There are no crash samples in here and there never will be — nobody is going to
crash on purpose to collect data. So thresholds can never be "learned" from
examples of the thing being detected. What this establishes is the opposite: how
violent NORMAL riding gets for each rider, so a candidate can be required to stand
outside their own noise rather than outside some universal number.
"""
import argparse
import math
import sqlite3
from pathlib import Path

MOVING_KMH = 20          # below this a rider is filtering, parking or stopped
DEFAULT_DB = Path.home() / "Library/Application Support/moto-tracker/moto.db"


def pct(values, p):
    """Percentile by nearest rank; values must be sorted."""
    if not values:
        return None
    k = max(0, min(len(values) - 1, int(math.ceil(p / 100 * len(values))) - 1))
    return values[k]


def summarise(name, values, unit="", digits=2):
    if not values:
        print(f"  {name:22} no samples")
        return
    values = sorted(values)
    line = "  ".join(
        f"p{p}={pct(values, p):.{digits}f}" for p in (50, 90, 99, 99.9))
    print(f"  {name:22} n={len(values):<6} {line}  max={values[-1]:.{digits}f}{unit}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--since", default="", help="only trips started on/after this date")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    trips = conn.execute(
        "SELECT id, rider_id, started_at FROM trips WHERE started_at >= ? ORDER BY id",
        (args.since,)).fetchall()

    riders = {}
    for t in trips:
        rows = conn.execute(
            "SELECT ts, received_at, speed, peak_g, mean_g, peak_rot, mean_rot, accel_n"
            " FROM positions WHERE trip_id=? ORDER BY id", (t["id"],)).fetchall()
        moving = [r for r in rows if (r["speed"] or 0) >= MOVING_KMH]
        if len(moving) < 20:
            continue                      # a walk, a test, or a trip that never left
        b = riders.setdefault(t["rider_id"], {
            "trips": [], "g": [], "rot": [], "mean_g": [], "decel": [], "seconds": 0.0,
            "top": 0.0})
        b["trips"].append(t["id"])
        b["seconds"] += len(moving) * 5
        b["top"] = max(b["top"], max(r["speed"] for r in moving))
        for r in moving:
            if r["peak_g"] is not None:
                b["g"].append(r["peak_g"])
            if r["peak_rot"] is not None:
                b["rot"].append(r["peak_rot"])
            if r["mean_g"] is not None:
                b["mean_g"].append(r["mean_g"])
        # Deceleration between consecutive samples, in km/h per second. The crash
        # signature is a speed collapse; this is how hard normal braking gets.
        for a, c in zip(rows, rows[1:]):
            if (a["speed"] or 0) < MOVING_KMH:
                continue
            gap = 5.0
            drop = (a["speed"] or 0) - (c["speed"] or 0)
            if drop > 0:
                b["decel"].append(drop / gap)

    print(f"archive: {args.db}")
    print(f"moving means speed >= {MOVING_KMH} km/h; windows are the phone's 5-second packets\n")
    for rider, b in sorted(riders.items()):
        print(f"{rider.upper()}  — trips {b['trips']}, "
              f"{b['seconds'] / 3600:.1f} h moving, top {b['top']:.0f} km/h")
        summarise("peak g per window", b["g"], " g")
        summarise("mean g per window", b["mean_g"], " g")
        summarise("peak rotation", b["rot"], " rad/s")
        summarise("deceleration", b["decel"], " km/h per s", digits=1)
        print()

    if len(riders) > 1:
        print("The two riders are not comparable, and that is the whole point:")
        for rider, b in sorted(riders.items()):
            g99 = pct(sorted(b["g"]), 99.9)
            r99 = pct(sorted(b["rot"]), 99.9)
            print(f"  {rider:8} normal riding reaches {g99:.1f} g and {r99:.1f} rad/s "
                  f"at its 99.9th percentile")
        print("  A single fixed threshold would either miss one rider's crash or "
              "fire constantly on the other's ordinary ride.")


if __name__ == "__main__":
    main()
