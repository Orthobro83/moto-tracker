#!/bin/sh
# Copy the server from the project directory to its runtime home, then restart.
#
# Why two locations: this project lives on /Volumes/2TB, and launchd-spawned
# processes cannot read that volume — macOS TCC denies them with EPERM, and the
# job dies before it can log why (confirmed 2026-09-08). Granting Full Disk
# Access to a python interpreter would be a fragile fix.
#
# The better reason: a service that must survive a reboot should not depend on an
# external volume being mounted in time. The Mini is on a UPS so this stays up;
# waiting on /Volumes to appear undercuts that.
#
# Source of truth stays here in the project. Runtime lives on the internal disk.
set -e
cd "$(dirname "$0")"
RUNTIME="$HOME/Library/Application Support/moto-tracker"
mkdir -p "$RUNTIME"
cp app.py schema.sql dashboard.html "$RUNTIME/"

if [ ! -x "$RUNTIME/venv/bin/python3" ]; then
    echo "creating venv on the internal disk…"
    /usr/bin/python3 -m venv "$RUNTIME/venv"
    "$RUNTIME/venv/bin/pip" install --quiet --upgrade pip
    "$RUNTIME/venv/bin/pip" install --quiet fastapi uvicorn
fi

LOG="$HOME/Library/Logs/moto-tracker.log"
mkdir -p "$HOME/Library/Logs" "$HOME/Library/LaunchAgents"
cat > "$HOME/Library/LaunchAgents/com.example.mototracker.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.example.mototracker</string>
    <key>ProgramArguments</key>
    <array>
        <string>$RUNTIME/venv/bin/python3</string>
        <string>$RUNTIME/app.py</string>
    </array>
    <key>WorkingDirectory</key><string>$RUNTIME</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

echo "deployed to $RUNTIME"
# Must be a real restart: `install` returns success when the job already exists
# and leaves the OLD code running, which silently shipped nothing (2026-09-10).
if launchctl print "gui/$(id -u)/com.example.mototracker" >/dev/null 2>&1; then
    ./motoctl restart
else
    ./motoctl install
fi

# Always smoke-test after deploying. A table rename once broke /health while
# /state kept working, and nothing noticed until it was seen on a phone.
sleep 4
./smoke.py || echo "!! SMOKE TEST FAILED — the server is not healthy"
