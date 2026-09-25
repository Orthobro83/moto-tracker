#!/bin/bash
#
# Installs the Mini's monitor: the background service, the app, and the login items.
#
#   bash monitor/install.sh              # install or update
#   bash monitor/install.sh --force      # even while a trip is open
#   bash monitor/install.sh --bundle     # from a bundle on another Mac: use the app as
#                                        # shipped, and take the CA from the bundle
#   bash monitor/install.sh --uninstall  # stop and remove, leaving the archive alone
#
# The runtime lives on the internal disk under ~/Library/Application Support, because
# a launchd job cannot read /Volumes/2TB. The archive, the keys and the logs there are
# never touched by an install.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/moto-tracker"
RUNTIME="$SUPPORT/monitor-runtime"
VENV="$SUPPORT/venv"
AGENTS="$HOME/Library/LaunchAgents"
SERVICE="com.example.moto-monitor"
APP_AGENT="com.example.moto-tracker-app"
APP="/Applications/moto-tracker.app"
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

# launchd needs the old job fully gone before the new one is bootstrapped; asking
# too soon gives "Input/output error".
reload_agent() {
  local label="$1" plist="$2"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  for _ in $(seq 1 40); do
    launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || break
    sleep 0.25
  done
  launchctl enable "gui/$(id -u)/$label" 2>/dev/null || true
  for attempt in 1 2 3 4 5; do
    if launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "  could not load $label" >&2
  launchctl bootstrap "gui/$(id -u)" "$plist"
}

# ---------------------------------------------------------------- uninstall

if [ "$UNINSTALL" = 1 ]; then
  say "stopping the monitor"
  launchctl bootout "gui/$(id -u)/$SERVICE" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$APP_AGENT" 2>/dev/null || true
  rm -f "$AGENTS/$SERVICE.plist" "$AGENTS/$APP_AGENT.plist"
  osascript -e 'tell application "moto-tracker" to quit' 2>/dev/null || true
  echo "removed the service and the login item."
  echo "left alone: $APP, and everything in $SUPPORT (archive, keys, logs)."
  exit 0
fi

# ---------------------------------------------------------------- 1/6  not during a ride

# Say what is being installed, and from where, before anything else: an old folder
# left beside a new one looks identical, and on 2026-09-24 one was installed by
# mistake and printed a stale error.
INSTALLING="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$HERE/monitor.py")"
echo "installing ${INSTALLING:-an unknown version} from $HERE"
# Where this install's own lines begin in the log, so a failure shows only those.
LOG_START=$(wc -c < "$LOGS/moto-monitor.log" 2>/dev/null || echo 0)

say "1/6  checking that nobody is riding"
KEY="$SUPPORT/relay/monitor.key"
CA="$SUPPORT/pki/ca.crt"
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
    echo "REFUSED: a trip is open ($OPEN). Installing restarts the archiver."
    echo "Run again with --force if you really mean to."
    exit 2
  fi
  [ -n "$OPEN" ] && echo "  a trip is open ($OPEN) — continuing because --force was given"
  echo "  nobody is riding"
else
  echo "  no monitor key yet; skipping the check"
fi

# ---------------------------------------------------------------- 2/6  runtime

say "2/6  copying the runtime to the internal disk"
mkdir -p "$RUNTIME" "$SUPPORT/monitor-secrets" "$SUPPORT/logs" "$LOGS"
chmod 700 "$SUPPORT/monitor-secrets"
rm -rf "$RUNTIME.new"
mkdir -p "$RUNTIME.new"
cp "$HERE"/*.py "$RUNTIME.new/"
# The uninstaller travels with what it uninstalls, so removing this needs nothing else.
cp "$HERE"/install.sh "$RUNTIME.new/"
mkdir -p "$RUNTIME.new/ui"
cp "$HERE"/ui/* "$RUNTIME.new/ui/"
# Keep anything the running service made (the alarm sound), then swap atomically.
[ -f "$RUNTIME/alarm.wav" ] && cp "$RUNTIME/alarm.wav" "$RUNTIME.new/" || true
if [ ! -x "$VENV/bin/python" ]; then
  echo "  building the virtual environment"
  /usr/bin/python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet fastapi uvicorn pydantic
fi
# The new code must load before it replaces the old. A bundle missing a module once
# replaced a working monitor with one that could not start (2026-09-24); now the
# running monitor is left exactly as it was.
if ! (cd "$RUNTIME.new" && "$VENV/bin/python" -c "import monitor" >/dev/null 2>"$SUPPORT/.import-check"); then
  echo "REFUSED: the new monitor does not load, so the running one was left alone:" >&2
  tail -n 3 "$SUPPORT/.import-check" >&2
  rm -rf "$RUNTIME.new" "$SUPPORT/.import-check"
  exit 1
fi
rm -f "$SUPPORT/.import-check"
rm -rf "$RUNTIME.old"
[ -d "$RUNTIME" ] && mv "$RUNTIME" "$RUNTIME.old"
mv "$RUNTIME.new" "$RUNTIME"
rm -rf "$RUNTIME.old"
echo "  $RUNTIME"
"$VENV/bin/python" - <<'PY'
import fastapi, uvicorn
print(f"  python packages: fastapi {fastapi.__version__}, uvicorn {uvicorn.__version__}")
PY

# ---------------------------------------------------------------- 3/6  the certificate and the map key

say "3/6  the relay's certificate and the map key"
if [ -f "$SUPPORT/pki/ca.crt" ]; then
  echo "  the CA certificate is already here"
elif [ -f "$PROJECT/pki/ca.crt" ]; then
  mkdir -p "$SUPPORT/pki"
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
  echo "  the map key is already installed (not shown, not re-read)"
elif [ -r "$PROJECT/secrets.env" ]; then
  ( umask 077
    awk -F= '/^TOMTOM_API_KEY=/{sub(/^TOMTOM_API_KEY=/,""); gsub(/^["'"'"']|["'"'"']$/,""); print; exit}' \
      "$PROJECT/secrets.env" > "$SUPPORT/monitor-secrets/tomtom.key" )
  chmod 600 "$SUPPORT/monitor-secrets/tomtom.key"
  echo "  copied from secrets.env (never printed)"
else
  echo "  no secrets.env here — the map will draw without tiles until the key is placed at"
  echo "  $SUPPORT/monitor-secrets/tomtom.key"
fi

# ---------------------------------------------------------------- 4/6  the service

say "4/6  the background monitor"
cat > "$AGENTS/$SERVICE.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$SERVICE</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>$RUNTIME/monitor.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MONITOR_RUNTIME</key><string>$RUNTIME</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGS/moto-monitor.log</string>
  <key>StandardErrorPath</key><string>$LOGS/moto-monitor.log</string>
  <key>WorkingDirectory</key><string>$RUNTIME</string>
</dict>
</plist>
PLIST
reload_agent "$SERVICE" "$AGENTS/$SERVICE.plist"
echo "  $SERVICE loaded"

# ---------------------------------------------------------------- 5/6  the app

say "5/6  the app"
if [ -d "$APP" ] && [ ! -f "$APP/Contents/Resources/moto-monitor" ] && [ ! -f "$APP/.moto-monitor" ]; then
  STAMP="$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$SUPPORT/backups"
  cp -R "$APP" "$SUPPORT/backups/moto-tracker-$STAMP.app"
  echo "  backed up the previous app to $SUPPORT/backups/moto-tracker-$STAMP.app"
fi
# An app built on another Mac arrives ad-hoc signed and quarantined, and macOS
# refuses those outright ("damaged"). Building it here avoids the problem entirely:
# the Command Line Tools this installer already needs also carry the Swift compiler.
if [ -f "$PROJECT/app/MotoTracker.swift" ] && \
   { xcrun --find swiftc >/dev/null 2>&1 || [ -x /Library/Developer/CommandLineTools/usr/bin/swiftc ]; }; then
  echo "  building the app on this Mac"
  bash "$PROJECT/app/build.sh" "$SUPPORT/build/moto-tracker.app" >/dev/null
elif [ "$BUNDLE" = 1 ] && [ -d "$PROJECT/app/moto-tracker.app" ]; then
  echo "  no Swift compiler here — using the app as shipped"
  rm -rf "$SUPPORT/build"
  mkdir -p "$SUPPORT/build"
  cp -R "$PROJECT/app/moto-tracker.app" "$SUPPORT/build/moto-tracker.app"
  xattr -dr com.apple.quarantine "$SUPPORT/build/moto-tracker.app" 2>/dev/null || true
  codesign --force --deep --sign - "$SUPPORT/build/moto-tracker.app" >/dev/null 2>&1 || true
else
  echo "REFUSED: no way to produce the app — install Apple's command line tools with"
  echo "  xcode-select --install"
  exit 1
fi
osascript -e 'tell application "moto-tracker" to quit' 2>/dev/null || true
sleep 1
rm -rf "$APP"
cp -R "$SUPPORT/build/moto-tracker.app" "$APP"
# Whatever produced it, the installed copy must carry no quarantine flag — and
# nothing may be written inside it afterwards, or macOS calls the app damaged.
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
if ! codesign --verify --strict "$APP" 2>/dev/null; then
  echo "  the app's signature is not intact — re-signing"
  codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
fi
codesign --verify --strict "$APP" 2>/dev/null || {
  echo "REFUSED: $APP is not sealed; macOS would call it damaged." >&2
  exit 1
}
echo "  installed $APP"

cat > "$AGENTS/$APP_AGENT.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$APP_AGENT</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/open</string><string>-a</string><string>$APP</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
PLIST
reload_agent "$APP_AGENT" "$AGENTS/$APP_AGENT.plist"
echo "  the app opens at login"

# ---------------------------------------------------------------- 6/6  check

say "6/6  checking it answers"
for i in $(seq 1 40); do
  if curl -fsS --max-time 3 http://127.0.0.1:8089/api/status >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
if ! curl -fsS --max-time 5 http://127.0.0.1:8089/api/status > /tmp/moto-monitor-status.json 2>/dev/null; then
  echo "  the monitor did not answer. Its log since this install began:"
  NEW_LOG="$( (tail -c +$((LOG_START + 1)) "$LOGS/moto-monitor.log" 2>/dev/null | tail -n 30) || true)"
  echo "${NEW_LOG:-  (nothing — it wrote no log at all)}"
  exit 1
fi
/usr/bin/python3 - <<'PY'
import json
s = json.load(open("/tmp/moto-monitor-status.json"))
print(f"  {s['version']}: relay {'live' if s['server']['up'] else 'UNREACHABLE'}"
      f", ping {s['server']['ping_ms']} ms, tiles {'on' if s['tiles'] else 'off'}")
for rider, r in s["riders"].items():
    print(f"  {r['name']:8} {r['state']:8} trip {r['trip_id'] or '—'}")
PY
rm -f /tmp/moto-monitor-status.json
open -a "$APP"
if [ "$BUNDLE" = 1 ]; then
  say "done. The window is open. If it asks to be paired, get a code on the other Mac:"
  echo "  Devices → Monitor (this Mac) → Create pairing code, then type it here."
else
  say "done. The window is open; the menu-bar icon flashes if an incident is raised."
fi
