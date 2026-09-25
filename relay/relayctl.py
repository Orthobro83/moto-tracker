#!/usr/bin/env python3
"""
Admin for the moto-tracker relay. Runs on the VPS as the relay's service account,
through the /usr/local/bin/relayctl wrapper that deploy.sh installs:

    sudo relayctl pair --role rider --rider jack --name "Jack's S21 FE"
    sudo relayctl pair --role monitor --name "Mac Mini"
    sudo relayctl devices
    sudo relayctl archivist 2                      # which Mac keeps the history
    sudo relayctl revoke 3
    sudo relayctl status
    sudo relayctl export-logs /tmp/spike-logs     # read-only: nothing is acknowledged
    sudo relayctl smoke-key                        # prints ONLY the new key, for deploy.sh

A pairing code is shown once, lives 10 minutes, and is typed on the phone, which
exchanges it at POST /pair for its device key. The key itself is never shown here.
"""
import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path(os.environ.get("RELAY_DB", "/var/lib/moto-relay/relay.db"))
SMOKE_RIDER = "__smoketest__"
# No 0/O, 1/I/L: a code read off one screen and typed into another.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_TTL = timedelta(minutes=10)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def connect() -> sqlite3.Connection:
    if not DB.exists():
        sys.exit(f"no relay database at {DB} — has the relay started once?")
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def cmd_pair(args) -> None:
    if args.role == "rider" and not args.rider:
        sys.exit("a rider key needs --rider")
    if args.role != "rider" and args.rider:
        sys.exit("only rider keys are bound to a rider")
    code = "".join(secrets.choice(ALPHABET) for _ in range(8))
    expires = (datetime.now(timezone.utc) + CODE_TTL).isoformat(timespec="milliseconds")
    with connect() as conn:
        if args.rider and not conn.execute("SELECT 1 FROM riders WHERE id=?", (args.rider,)).fetchone():
            sys.exit(f"unknown rider {args.rider!r}")
        conn.execute("INSERT INTO pairing_codes (code_hash, role, rider_id, name, created_at, expires_at)"
                     " VALUES (?,?,?,?,?,?)",
                     (hash_secret(code), args.role, args.rider, args.name, now(), expires))
    if args.json:
        print(json.dumps({"code": f"{code[:4]}-{code[4:]}", "role": args.role, "rider": args.rider,
                          "name": args.name, "expires_at": expires}))
        return
    print(f"Pairing code for {args.name} ({args.role}{', ' + args.rider if args.rider else ''}):")
    print(f"    {code[:4]}-{code[4:]}")
    print("Single use, valid for 10 minutes.")


def cmd_devices(args) -> None:
    with connect() as conn:
        rows = conn.execute("SELECT id, name, role, rider_id, created_at, last_seen_at, revoked_at,"
                            " archivist FROM devices ORDER BY id").fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows]))
        return
    if not rows:
        print("no devices")
    for r in rows:
        status = f"revoked {r['revoked_at'][:16]}" if r["revoked_at"] else f"last seen {(r['last_seen_at'] or 'never')[:16]}"
        keeps = "  [keeps the history]" if r["archivist"] else ""
        print(f"#{r['id']:<3} {r['role']:8} {r['rider_id'] or '-':14} {r['name']:32} {status}{keeps}")


def cmd_revoke(args) -> None:
    with connect() as conn:
        n = conn.execute("UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                         (now(), args.id)).rowcount
    print(f"revoked device #{args.id}" if n else f"no active device #{args.id}")


def cmd_archivist(args) -> None:
    """Names the one monitor that consumes the outbox and keeps the history."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id=? AND revoked_at IS NULL", (args.id,)).fetchone()
        if not row or row["role"] != "monitor":
            sys.exit(f"#{args.id} is not an active monitor device")
        conn.execute("UPDATE devices SET archivist=0 WHERE role='monitor'")
        conn.execute("UPDATE devices SET archivist=1 WHERE id=?", (args.id,))
    print(f"#{args.id} ({row['name']}) now keeps the history; every other Mac is a viewer")


def cmd_smoke_key(_args) -> None:
    """Rotates the smoke-test key. Prints the key and nothing else."""
    key = "mt_" + secrets.token_urlsafe(32)
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO riders (id, name) VALUES (?, 'Smoke test')", (SMOKE_RIDER,))
        conn.execute("UPDATE devices SET revoked_at=? WHERE role='smoke' AND revoked_at IS NULL", (now(),))
        conn.execute("INSERT INTO devices (name, role, rider_id, key_hash, created_at) VALUES (?,?,?,?,?)",
                     ("deploy smoke test", "smoke", SMOKE_RIDER, hash_secret(key), now()))
    print(key)


def cmd_status(_args) -> None:
    with connect() as conn:
        for t in ("trips", "positions", "incidents", "events", "outbox"):
            print(f"{t:10} {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
        o = conn.execute("SELECT MIN(seq) lo, MAX(seq) hi, MIN(ts) oldest, COALESCE(SUM(LENGTH(payload)),0) b"
                         " FROM outbox").fetchone()
        print(f"outbox     seq {o['lo']}..{o['hi']}, oldest {o['oldest']}, {o['b'] / 1024:.0f} KB waiting for the Mini")
        for r in conn.execute("SELECT kind, COUNT(*) c FROM outbox GROUP BY kind ORDER BY kind"):
            print(f"  {r['kind']:10} {r['c']}")
        opent = conn.execute("SELECT id, rider_id, started_at, state FROM trips WHERE ended_at IS NULL").fetchall()
        print("open trips " + (", ".join(f"#{t['id']} {t['rider_id']} ({t['state']})" for t in opent) or "none"))


def cmd_export_logs(args) -> None:
    """Writes uploaded phone logs out of the outbox. Acknowledges nothing."""
    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        rows = conn.execute("SELECT seq, payload FROM outbox WHERE kind='spike_log' ORDER BY seq").fetchall()
    for r in rows:
        p = json.loads(r["payload"])
        (out / p["name"]).write_text(p["text"])
        print(f"seq {r['seq']}: {out / p['name']} ({len(p['text'])} bytes)")
    if not rows:
        print("no uploaded logs waiting")


def main() -> None:
    ap = argparse.ArgumentParser(description="moto-tracker relay admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair", help="create a single-use pairing code")
    # Two roles (design.md 2026-09-15): a phone is paired to its rider and becomes
    # the other rider's Observer by itself. 'observer' keys still work if any exist.
    p.add_argument("--role", required=True, choices=["rider", "monitor"])
    p.add_argument("--rider", choices=["jack", "dana"])
    p.add_argument("--name", required=True)
    p.add_argument("--json", action="store_true", help="machine-readable, for the Mini's Devices panel")
    p.set_defaults(fn=cmd_pair)
    d = sub.add_parser("devices", help="list device keys")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_devices)
    r = sub.add_parser("revoke", help="revoke a device key")
    r.add_argument("id", type=int)
    r.set_defaults(fn=cmd_revoke)
    a = sub.add_parser("archivist", help="name the monitor that keeps the history")
    a.add_argument("id", type=int)
    a.set_defaults(fn=cmd_archivist)
    sub.add_parser("smoke-key", help="rotate the deploy smoke-test key").set_defaults(fn=cmd_smoke_key)
    sub.add_parser("status", help="live state and outbox backlog").set_defaults(fn=cmd_status)
    e = sub.add_parser("export-logs", help="write uploaded phone logs to a directory")
    e.add_argument("dir")
    e.set_defaults(fn=cmd_export_logs)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
