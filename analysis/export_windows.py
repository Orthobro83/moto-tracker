#!/usr/bin/env python3
"""
Exports the archive's real rides as CSV fixtures, so the detector can be replayed
over them in a unit test. One row per 5-second window, in order, per trip.

    /usr/bin/python3 analysis/export_windows.py [--db PATH] [--out DIR]

Only trips with real movement are exported; walks and stationary tests are not
riding and would flatter any detector.
"""
import argparse
import csv
import sqlite3
from datetime import datetime
from pathlib import Path

MOVING_KMH = 20
MIN_MOVING = 20
DEFAULT_DB = Path.home() / "Library/Application Support/moto-tracker/moto.db"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "android/app/src/test/resources"


def ms(iso):
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    written = []
    for t in conn.execute("SELECT id, rider_id, started_at FROM trips ORDER BY id"):
        rows = conn.execute(
            "SELECT received_at, speed, peak_g, mean_g, peak_rot, accel_n FROM positions"
            " WHERE trip_id=? ORDER BY id", (t["id"],)).fetchall()
        if sum(1 for r in rows if (r["speed"] or 0) >= MOVING_KMH) < MIN_MOVING:
            continue
        name = f"ride-{t['id']}-{t['rider_id']}.csv"
        with (out / name).open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["at_ms", "speed_kmh", "peak_g", "mean_g", "peak_rot", "accel_n"])
            for r in rows:
                w.writerow([ms(r["received_at"]), round(r["speed"] or 0, 2),
                            round(r["peak_g"] or 0, 4), round(r["mean_g"] or 0, 4),
                            round(r["peak_rot"] or 0, 4), r["accel_n"] or 0])
        written.append((name, len(rows)))
    for name, n in written:
        print(f"{name}  {n} windows")
    print(f"{len(written)} rides -> {out}")


if __name__ == "__main__":
    main()
