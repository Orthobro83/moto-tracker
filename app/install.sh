#!/bin/bash
#
# Installs moto-tracker on this Mac: one app, which talks to the relay itself
# (design.md 2026-10-04). It also retires the old background service
# (com.example.moto-monitor on 127.0.0.1:8089) if this Mac still has it.
#
#   bash app/install.sh              # install or update
#   bash app/install.sh --force      # even while a trip is open
#   bash app/install.sh --bundle     # on another Mac, from the hand-over folder: take the
#                                    # CA (and the map key, if included) from the bundle
#   bash app/install.sh --uninstall  # stop and remove, leaving the archive and keys alone
#
# The archive, the keys and the logs under ~/Library/Application Support/moto-tracker
# are never touched by an install: the app carries on from exactly where the old
# service stopped.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/moto-tracker"
AGENTS="$HOME/Library/LaunchAgents"
OLD_SERVICE="com.example.moto-monitor"
APP_AGENT="com.example.moto-tracker-app"
APP="/Applications/moto-tracker.app"
STATUS="$SUPPORT/app-status.json"
LOGS="$HOME/Library/Logs"
FORCE=0
UNINSTALL=0
BUNDLE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --bundle) BUNDLE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n%s\n' "$1"; }
STAMP="$(date +%Y%m%d-%H%M%S)"

# launchd needs the old job fully gone before the new one is bootstrapped; asking too
# soon gives "Input/output error".
unload_agent() {
  local label="$1"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  for _ in $(seq 1 40); do
    launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || break
    sleep 0.25
  done
}
load_agent() {
  local label="$1" plist="$2"
  unload_agent "$label"
  launchctl enable "gui/$(id -u)/$label" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    if launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "  could not load $label" >&2
  launchctl bootstrap "gui/$(id -u)" "$plist"
}
quit_app() {
  osascript -e 'tell application "moto-tracker" to quit' >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    pgrep -f "$APP/Contents/MacOS/moto-tracker" >/dev/null || return 0
    sleep 0.25
  done
  pkill -f "$APP/Contents/MacOS/moto-tracker" 2>/dev/null || true
}

# ---------------------------------------------------------------- uninstall

if [ "$UNINSTALL" = 1 ]; then
  say "stopping moto-tracker"
  unload_agent "$APP_AGENT"
  rm -f "$AGENTS/$APP_AGENT.plist"
  quit_app
  echo "removed the login item and quit the app."
  echo "left alone: $APP, and everything in $SUPPORT (archive, keys, logs)."
  exit 0
fi

# ---------------------------------------------------------------- 1/5  not during a ride

INSTALLING="$(sed -n 's/^let VERSION = "\(.*\)"/\1/p' "$HERE/Core/Monitor.swift")"
echo "installing ${INSTALLING:-an unknown version} from $HERE"

say "1/5  checking that nobody is riding"
KEY="$SUPPORT/relay/monitor.key"
CA="$SUPPORT/pki/ca.crt"
[ -r "$CA" ] || CA="$PROJECT/pki/ca.crt"
if [ -r "$KEY" ] && [ -r "$CA" ]; then
  STATE="$(curl -sS --max-time 12 --cacert "$CA" -H "Authorization: Bearer $(cat "$KEY")" \
           https://203.0.113.10/state || true)"
  OPEN="$(printf '%s' "$STATE" | /usr/bin/python3 -c '
import json, sys
try:
    state = json.load(sys.stdin)
except Exception:
    print("")            # unreachable: not a reason to refuse
    raise SystemExit
print(",".join(r for r, s in state.items() if s.get("trip_id")))' 2>/dev/null || true)"
  if [ -n "$OPEN" ] && [ "$FORCE" != 1 ]; then
    echo "REFUSED: a trip is open ($OPEN). Installing restarts the app that keeps the alarm."
    echo "Run again with --force if you really mean to."
    exit 2
  fi
  [ -n "$OPEN" ] && echo "  a trip is open ($OPEN) — continuing because --force was given"
  echo "  nobody is riding"
else
  echo "  no key for this Mac yet; skipping the check"
fi

# ---------------------------------------------------------------- 2/5  build

say "2/5  building the app on this Mac"
# An app built on another Mac arrives ad-hoc signed and quarantined, and macOS refuses
# those outright ("damaged"). Building it here avoids the problem: the Command Line
# Tools carry the Swift compiler.
if ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "REFUSED: no Swift compiler — install Apple's command line tools with"
  echo "  xcode-select --install"
  exit 1
fi
mkdir -p "$SUPPORT/build"
bash "$HERE/build.sh" "$SUPPORT/build/moto-tracker.app" >/dev/null
echo "  built"

# ---------------------------------------------------------------- 3/5  the certificate and the map key

say "3/5  the relay's certificate and the map key"
mkdir -p "$SUPPORT/pki" "$SUPPORT/monitor-secrets" "$SUPPORT/logs" "$LOGS"
chmod 700 "$SUPPORT/monitor-secrets"
if [ -f "$SUPPORT/pki/ca.crt" ]; then
  echo "  the CA certificate is already here"
elif [ -f "$PROJECT/pki/ca.crt" ]; then
  cp "$PROJECT/pki/ca.crt" "$SUPPORT/pki/ca.crt"
  echo "  installed the CA certificate from the bundle"
else
  echo "  NO CA CERTIFICATE. This Mac cannot verify the relay until"
  echo "  $SUPPORT/pki/ca.crt exists."
fi
if [ ! -f "$SUPPORT/monitor-secrets/tomtom.key" ] && [ -f "$PROJECT/monitor-secrets/tomtom.key" ]; then
  ( umask 077; cp "$PROJECT/monitor-secrets/tomtom.key" "$SUPPORT/monitor-secrets/tomtom.key" )
  chmod 600 "$SUPPORT/monitor-secrets/tomtom.key"
  echo "  installed the map key from the bundle (never printed)"
fi
if [ -f "$SUPPORT/monitor-secrets/tomtom.key" ]; then
  echo "  the map key is installed (not shown, not re-read)"
elif [ "$BUNDLE" = 0 ] && [ -r "$PROJECT/secrets.env" ]; then
  ( umask 077
    awk -F= '/^TOMTOM_API_KEY=/{sub(/^TOMTOM_API_KEY=/,""); gsub(/^["'"'"']|["'"'"']$/,""); print; exit}' \
      "$PROJECT/secrets.env" > "$SUPPORT/monitor-secrets/tomtom.key" )
  chmod 600 "$SUPPORT/monitor-secrets/tomtom.key"
  echo "  copied from secrets.env (never printed)"
else
  echo "  no map key — the map will draw without tiles until the key is placed at"
  echo "  $SUPPORT/monitor-secrets/tomtom.key"
fi

# ---------------------------------------------------------------- 4/5  the old service out, the app in

say "4/5  replacing the old background service with the app"
if [ -f "$AGENTS/$OLD_SERVICE.plist" ] || launchctl print "gui/$(id -u)/$OLD_SERVICE" >/dev/null 2>&1; then
  unload_agent "$OLD_SERVICE"
  mkdir -p "$SUPPORT/backups"
  [ -f "$AGENTS/$OLD_SERVICE.plist" ] && mv "$AGENTS/$OLD_SERVICE.plist" "$SUPPORT/backups/$OLD_SERVICE.plist.retired-$STAMP"
  [ -d "$SUPPORT/monitor-runtime" ] && mv "$SUPPORT/monitor-runtime" "$SUPPORT/backups/monitor-runtime-retired-$STAMP"
  echo "  retired $OLD_SERVICE (127.0.0.1:8089); its files are in backups/, nothing deleted"
else
  echo "  no old service on this Mac"
fi
if lsof -nP -iTCP:8089 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  WARNING: something still listens on 8089" >&2
fi

quit_app
unload_agent "$APP_AGENT"
if [ -d "$APP" ] && [ ! -f "$APP/Contents/Resources/moto-tracker-app" ] && [ ! -f "$APP/Contents/Resources/moto-monitor" ]; then
  mkdir -p "$SUPPORT/backups"
  cp -R "$APP" "$SUPPORT/backups/moto-tracker-$STAMP.app"
  echo "  backed up an unfamiliar $APP to backups/"
fi
rm -rf "$APP"
cp -R "$SUPPORT/build/moto-tracker.app" "$APP"
# The installed copy must carry no quarantine flag — and nothing may be written inside
# it afterwards, or macOS calls the app damaged.
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
codesign --verify --strict "$APP" 2>/dev/null || {
  echo "REFUSED: $APP is not sealed; macOS would call it damaged." >&2
  exit 1
}
echo "  installed $APP"
# Kept beside the data so --uninstall works after the hand-over folder is gone.
cp "$HERE/install.sh" "$SUPPORT/install.sh"

# The app is the alarm, so launchd keeps it running: opened at login, and opened
# again at once if it crashes or its watchdog ends it. A deliberate Quit is left alone.
cat > "$AGENTS/$APP_AGENT.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$APP_AGENT</string>
  <key>ProgramArguments</key>
  <array><string>$APP/Contents/MacOS/moto-tracker</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key><false/>
  </dict>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$LOGS/moto-tracker-app.stderr.log</string>
</dict>
</plist>
PLIST
STARTED="$(date -u +%Y-%m-%dT%H:%M:%S)"
load_agent "$APP_AGENT" "$AGENTS/$APP_AGENT.plist"
echo "  the app is running, opens at login, and comes back by itself after a crash"

# ---------------------------------------------------------------- 5/5  check

say "5/5  checking it talks to the relay"
/usr/bin/python3 - "$STATUS" "$STARTED" <<'PY'
import json, sys, time
path, started = sys.argv[1], sys.argv[2]
s = {}
for _ in range(60):
    try:
        s = json.load(open(path))
    except Exception:
        s = {}
    if s.get("written_at", "") >= started and (s.get("relay_up") or s.get("key_rejected")
                                               or not s.get("paired", True)):
        break
    time.sleep(0.5)
if s.get("written_at", "") < started:
    print("  the app has not said anything yet — look at the window")
    sys.exit(1)
if not s.get("paired"):
    print(f"  {s['version']}: not paired yet — the window offers a pairing code box")
elif s.get("key_rejected"):
    print(f"  {s['version']}: the relay REJECTED this Mac's key")
    sys.exit(1)
elif not s.get("relay_up"):
    print(f"  {s['version']}: relay UNREACHABLE ({s.get('relay_why') or s.get('relay_error')})")
    sys.exit(1)
else:
    role = {True: "keeps the history", False: "viewer"}.get(s.get("archivist"), "role not known yet")
    print(f"  {s['version']}: relay live, ping {s.get('ping_ms')} ms, {role}")
PY
open -a "$APP"
if [ "$BUNDLE" = 1 ]; then
  say "done. If the window asks to be paired, get a code on the other Mac:"
  echo "  gear → Devices → Monitor (this Mac) → Create pairing code, then type it here."
else
  say "done. The window is open; the menu-bar icon flashes if an incident is raised."
fi
