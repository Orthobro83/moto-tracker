"""
What normal riding looks like, per rider, computed from the Mini's archive and
pushed to the relay.

One source of truth, deliberately. The phone judges a ride against these numbers,
and the relay uses the same ones to decide whether a silence followed something
violent — if the two disagreed, a crash could be a crash on one machine and
ordinary riding on the other.

The Mini is the only machine that can compute them: it is the only one that holds
the history. They are recomputed as that history grows, so a new bike, a new mount
or a different riding style moves the thresholds with it.

Mirrors analysis/baseline.py, which is the readable version for looking at the data
by hand.
"""
import math
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

MOVING_KMH = 20          # below this a rider is filtering, parking or stopped
# design.md, "Baseline epoch": everything before this date was recorded while the
# pipeline itself was being debugged and does not describe riding.
EPOCH = "2026-09-11"
# A real incident is not ordinary riding. If its windows were counted as normal, the
# bar would rise by exactly the amount of the crash, and the next one would have to
# be worse to be noticed. Retracted candidates stay in: those WERE ordinary riding.
INCIDENT_MARGIN_S = 120
MIN_MOVING_SAMPLES = 200  # ~17 minutes of riding before a rider's own numbers are used
IMPACT_G_FLOOR = 11.0    # the app's floors, repeated here so both ends agree
IMPACT_ROT_FLOOR = 8.0
MARGIN = 1.5             # how far outside their own worst riding an impact must sit
# Below riding speed, only a vehicle-sized blow counts (Jack, 2026-09-16): the
# wildest ordinary readings in the archive are driveway bumps and filtering, not
# riding. Dana's worst window of all, 11.93 g, is her own driveway.
REAR_END_MARGIN = 1.8
REAR_END_G_FLOOR = 20.0

# Until a rider has ridden enough, they are judged by the harder of the two known
# riders' baselines: better to ask someone who is fine than to stay quiet for
# someone who is not, without being so tight that an ordinary ride sets it off.
UNKNOWN = {"impact_g": 13.2, "impact_rot": 9.1, "rearend_g": 23.8, "decel_kmh_s": 6.7,
           "moving_h": 0.0, "source": "default (not enough riding yet)"}


def _pct(values, p):
    if not values:
        return None
    k = max(0, min(len(values) - 1, int(math.ceil(p / 100 * len(values))) - 1))
    return values[k]


def _excluded(conn: sqlite3.Connection, rider: str) -> list:
    """Time ranges around confirmed incidents, which are not ordinary riding."""
    out = []
    for r in conn.execute(
            "SELECT raised_at, COALESCE(closed_at, resolved_at, raised_at) AS ended"
            " FROM incidents WHERE rider_id=? AND state NOT IN ('retracted')", (rider,)):
        start = _shift(r["raised_at"], -INCIDENT_MARGIN_S)
        end = _shift(r["ended"], INCIDENT_MARGIN_S)
        if start and end:
            out.append((start, end))
    return out


def _shift(iso: str, seconds: int):
    try:
        return (datetime.fromisoformat(iso) + timedelta(seconds=seconds)).isoformat(
            timespec="milliseconds")
    except (ValueError, TypeError):
        return None


def compute(conn: sqlite3.Connection, rider: str) -> dict:
    """This rider's thresholds, from every archived ride of theirs since the epoch,
    with confirmed incidents left out."""
    rows = conn.execute(
        "SELECT p.speed, p.peak_g, p.peak_rot, p.received_at FROM positions p"
        " WHERE p.rider_id=? AND p.speed >= ? AND p.received_at >= ?",
        (rider, MOVING_KMH, EPOCH)).fetchall()
    skip = _excluded(conn, rider)
    if skip:
        rows = [r for r in rows
                if not any(a <= (r["received_at"] or "") <= b for a, b in skip)]
    gs = sorted(r["peak_g"] for r in rows if r["peak_g"] is not None)
    rots = sorted(r["peak_rot"] for r in rows if r["peak_rot"] is not None)
    if len(gs) < MIN_MOVING_SAMPLES or len(rots) < MIN_MOVING_SAMPLES:
        return dict(UNKNOWN)

    worst_g = max(gs[-1], _pct(gs, 99.9) or 0)
    worst_rot = max(rots[-1], _pct(rots, 99.9) or 0)
    # Hardest ordinary braking, km/h lost per second, over consecutive windows.
    decel = []
    trips = [r["id"] for r in conn.execute(
        "SELECT DISTINCT trip_id AS id FROM positions WHERE rider_id=? AND trip_id IS NOT NULL",
        (rider,)).fetchall()]
    for trip in trips:
        speeds = [r["speed"] or 0 for r in conn.execute(
            "SELECT speed FROM positions WHERE trip_id=? ORDER BY id", (trip,)).fetchall()]
        for a, b in zip(speeds, speeds[1:]):
            if a >= MOVING_KMH and a > b:
                decel.append((a - b) / 5.0)
    decel.sort()
    impact_g = max(worst_g * MARGIN, IMPACT_G_FLOOR)
    return {
        "impact_g": round(impact_g, 2),
        "impact_rot": round(max(worst_rot * MARGIN, IMPACT_ROT_FLOOR), 2),
        "rearend_g": round(max(impact_g * REAR_END_MARGIN, REAR_END_G_FLOOR), 2),
        "decel_kmh_s": round(decel[-1], 2) if decel else None,
        "moving_h": round(len(gs) * 5 / 3600, 2),
        "source": f"{len(gs)} moving windows since {EPOCH}"
                  + (f", {len(skip)} incident(s) left out" if skip else ""),
    }


def push(conn: sqlite3.Connection, relay, riders=("jack", "dana")) -> dict:
    """Computes and sends. Returns what was sent, for the log."""
    sent = {}
    for rider in riders:
        b = compute(conn, rider)
        status, _, _ = relay.call("POST", "/baseline", dict(b, rider=rider), timeout=15)
        sent[rider] = dict(b, ok=(status == 200), status=status)
    return sent


def describe(b: dict) -> str:
    return (f"impact {b['impact_g']:.1f} g / {b['impact_rot']:.1f} rad/s,"
            f" {b.get('rearend_g', 0):.1f} g below riding speed,"
            f" from {b.get('moving_h') or 0:.1f} h of riding")
