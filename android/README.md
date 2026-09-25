# moto-tracker — Phase 1 endurance spike

Throwaway. It answers two platform questions and nothing else:

1. Does a foreground service keep ticking every 5 seconds through a multi-hour
   ride on One UI, or does Samsung throttle it?
2. Can a sideloaded app fire a full-screen intent over Waze?

Both are go/no-go for the real design. See `../checklist.md` Phase 1.

## Build

Needs Temurin 25 (`brew install --cask temurin@25`) — the system JDK 26 is ahead
of what AGP 9.3.2 supports, and `../design.md` forbids borrowing Android Studio's.

```
./gradlew assembleDebug
```

Debug signing is fine here. The spike is thrown away, so it never needs the real
upgrade path the release keystore exists to protect.

## Install — no ADB

Developer Options stay off; the riders' banking apps refuse to run with USB
debugging enabled (`../design.md`, 2026-09-08).

```
./serve-apk.sh
```

Then on the phone open `http://100.64.0.10:8000/moto-tracker.apk`, tap the
download, and allow Chrome to install unknown apps when asked.

## The server

From `spike-4` the spike talks to the **real Phase 2 server** on port 8088, not
the throwaway listener that used to run on 8099 — that has been retired.

Start it by clicking **moto-tracker** in Applications. The spike calls
`/ride/start` when tracking starts and `/ride/end` when it stops, posts each tick
to `/position`, and uploads its log to `/spike/log`. So the dashboard shows the
ride live, and the endurance data lands in SQLite.

Set which rider this phone is with **Switch rider** — otherwise both phones
report as Jack and their tracks interleave.

Network failures never stop the local log. That is deliberate: the on-device file
is the ground truth for the gate, and an app that stopped ticking because the mesh
blipped would destroy the very evidence being collected.

## On the phone, once

Neither of these is in Developer Options:

- Settings → Apps → moto-tracker → Battery → **Unrestricted**
- Settings → Battery and device care → Background usage limits → remove from
  **Sleeping apps** and **Deep sleeping apps**

Then in the app: Start tracking, and grant background location when asked.

## Reading the result

The **on-device log is the ground truth**. A gap in the server log alone cannot
separate One UI throttling from a cellular dropout, and separating those is the
whole point. Use "Upload log to Mini" after the ride, or compare `logs/`
side by side.

Regular 5-second `TICK` lines throughout = the design holds. Long silences with
no `SVC onDestroy` before them = Samsung killed it, and the architecture changes
before anything else gets built.

`CRASH` lines are the uncaught-exception handler. With no logcat, they are the
only crash visibility there is.

## Testing the takeover

Tap "Test full-screen intent", switch to Waze within 20 seconds, and wait. If a
red screen appears over Waze, the five-minute stop escalation is viable on this
phone. If only a notification appears, it is not, and the design needs rethinking
before Phase 3.
