# moto-tracker server — Phase 2

## Run it

**Click `moto-tracker` in your Applications folder.** It starts the server if it
is not already running and opens the dashboard. That is the whole workflow — no
terminal.

The app only touches the internal disk, so it works whether or not this project
volume is mounted. On first run, if the launchd job is missing, it deploys from
here automatically.

`./install-app.sh` rebuilds and reinstalls it. The launcher, icon and its
generator live in `app/`.

Terminal equivalents, if you ever want them — note `motoctl` only resolves from
this directory, which is why the app exists:

```
./motoctl status | open | logs | stop | start | restart
./deploy.sh          # after editing app.py, dashboard.html or schema.sql
```

## Two locations, on purpose

**Source of truth** is here in the project. **Runtime** is
`~/Library/Application Support/moto-tracker`.

launchd-spawned processes cannot read `/Volumes/2TB` — macOS TCC denies them with
`EPERM`, and the job dies before it can log why. Beyond that, a service that must
survive a reboot should not wait on an external volume to mount. Edit here, run
`./deploy.sh`.

## Binding

Two explicit sockets: `100.64.0.10` (Meshnet) and `127.0.0.1`. Not `0.0.0.0`,
and explicitly not the LAN address — design.md relies on the mesh for access
control. Localhost is required because the Mini cannot reach its own Meshnet IP,
so the dashboard would otherwise be unreachable from the Mini itself.

## Interpreter

A venv built on Apple's `/usr/bin/python3`, which symlinks to the signed
`com.apple.python3` framework binary. Homebrew and miniconda pythons are ad-hoc
signed, and the macOS firewall silently ignores allow-entries for those — that is
what blocked all inbound Meshnet traffic on 2026-09-08.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/ride/start` | Rider taps Start Ride |
| POST | `/ride/end` | Off the bike; sharing continues, monitoring stops |
| POST | `/ride/sleep` | Back to start screen — refused unless already offbike |
| POST | `/position` | 5-second packet |
| POST | `/stop-answer` | `arrived` / `traffic` / `help` |
| POST | `/incident/candidate` | High-G candidate, sent immediately |
| POST | `/incident/retract` | Phone talks the candidate down |
| POST | `/incident/resolve` | Closed by rider or observer |
| GET | `/state` | Both riders |
| GET | `/health` | Uptime, counts, db size |
| GET | `/events` | SSE log stream |
| GET | `/` | Dashboard |
| POST | `/admin/shutdown` | Stop button — localhost only |

## Not built yet

`Stale` detection and the escalation of unretracted candidates are Phase 5 and 6.
The schema carries `escalate_at` so the rule has somewhere to live, but nothing
runs it yet.
