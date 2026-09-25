#!/bin/sh
# moto-tracker launcher.
#
# Starts the server if it is not already up, then opens the dashboard. Depends
# only on the internal disk: the runtime lives in ~/Library/Application Support,
# so this works whether or not the project volume is mounted.
LABEL=com.example.mototracker
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME="$HOME/Library/Application Support/moto-tracker"
LOG="$HOME/Library/Logs/moto-tracker.log"
SOURCE="/Volumes/2TB/claude-vault/projects/moto-tracker/server"
URL="http://127.0.0.1:8088/"
U=$(id -u)

note() { osascript -e "display notification \"$1\" with title \"moto-tracker\"" >/dev/null 2>&1; }
fail() {
    osascript -e "display dialog \"$1\" with title \"moto-tracker\" buttons {\"OK\"} default button 1 with icon caution" >/dev/null 2>&1
    exit 1
}

up() { curl -s --max-time 2 "$URL/health" >/dev/null 2>&1; }

# Already running? Just show it.
if up; then open "$URL"; exit 0; fi

# First run, or the launchd job was removed.
if [ ! -f "$PLIST" ]; then
    if [ -x "$SOURCE/deploy.sh" ]; then
        note "First run — setting up…"
        "$SOURCE/deploy.sh" >/dev/null 2>&1 || fail "Setup failed. See $LOG"
    else
        fail "Not installed yet, and the project volume is not mounted.\n\nMount /Volumes/2TB and open this again, or run deploy.sh by hand."
    fi
fi

[ -d "$RUNTIME" ] || fail "Runtime missing at:\n$RUNTIME\n\nRun deploy.sh from the project."

launchctl kickstart -k "gui/$U/$LABEL" >/dev/null 2>&1 \
    || launchctl bootstrap "gui/$U" "$PLIST" >/dev/null 2>&1

# Give it a moment; the venv import of fastapi is the slow part.
i=0
while [ $i -lt 20 ]; do
    if up; then open "$URL"; note "Server running"; exit 0; fi
    sleep 0.5
    i=$((i + 1))
done

fail "The server did not come up within 10 seconds.\n\nLast log lines:\n$(tail -n 6 "$LOG" 2>/dev/null)"
