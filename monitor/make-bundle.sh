#!/bin/bash
#
# Builds the folder to hand to the second Mac (Dana's), so it can run the same
# monitor without this project, Xcode, or anyone touching the VPS.
#
#   bash monitor/make-bundle.sh                     # no secrets: the map draws blank
#   bash monitor/make-bundle.sh --with-map-key      # include the TomTom key as well
#
# What goes in: the built app, the monitor's code, and the CA certificate, which is
# public — it only says which certificate to trust. What never goes in: any device
# key, the CA's private key, or secrets.env. The second Mac gets its own device key
# by typing a pairing code into its own window.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/moto-tracker"
OUT="$PROJECT/dist/moto-tracker-for-her-mac"
WITH_MAP_KEY=0
[ "${1:-}" = "--with-map-key" ] && WITH_MAP_KEY=1

rm -rf "$OUT"
mkdir -p "$OUT/monitor/ui" "$OUT/app" "$OUT/pki"

echo "building the app (shipped as a fallback; the other Mac rebuilds it locally)"
bash "$PROJECT/app/build.sh" "$OUT/app/moto-tracker.app" >/dev/null
# The source travels too: building the app on her Mac is what keeps macOS from
# refusing it as "damaged", which is what happens to an app carried between Macs.
cp "$PROJECT/app/MotoTracker.swift" "$PROJECT/app/build.sh" "$OUT/app/"

# The service and every module it has, less the test rig and the demo, which need the
# relay source and have no business on her Mac. Everything else goes, so a new module
# cannot be left behind: a hand-kept list once missed baselines.py and her monitor
# would not start (2026-09-24).
for f in "$HERE"/*.py; do
  case "$(basename "$f")" in
    e2e_test.py|demo.py) ;;
    *) cp "$f" "$OUT/monitor/" ;;
  esac
done
# Proof before shipping: the monitor imports from the bundle alone, with the Python
# her Mac will run it under.
BUNDLE_PY="$SUPPORT/venv/bin/python"
[ -x "$BUNDLE_PY" ] || BUNDLE_PY=/usr/bin/python3
if ! (cd "$OUT/monitor" && "$BUNDLE_PY" -c "import monitor" >/dev/null 2>"$OUT/.import-check"); then
  echo "REFUSED: the bundled monitor does not import:" >&2
  cat "$OUT/.import-check" >&2
  exit 1
fi
rm -f "$OUT/.import-check"
echo "the bundled monitor imports cleanly"
cp "$HERE"/install.sh "$OUT/monitor/"
cp "$HERE"/ui/* "$OUT/monitor/ui/"
rm -f "$OUT/monitor/make-bundle.sh"

if [ -f "$SUPPORT/pki/ca.crt" ]; then
  cp "$SUPPORT/pki/ca.crt" "$OUT/pki/ca.crt"
else
  echo "WARNING: no CA certificate at $SUPPORT/pki/ca.crt — the other Mac will not trust the relay"
fi

if [ "$WITH_MAP_KEY" = 1 ] && [ -f "$SUPPORT/monitor-secrets/tomtom.key" ]; then
  mkdir -p "$OUT/monitor-secrets"
  ( umask 077; cp "$SUPPORT/monitor-secrets/tomtom.key" "$OUT/monitor-secrets/tomtom.key" )
  chmod 600 "$OUT/monitor-secrets/tomtom.key"
  echo "including the map key — treat this folder as a secret from here on"
else
  echo "no map key included: her map will have no tiles until one is placed at"
  echo "  ~/Library/Application Support/moto-tracker/monitor-secrets/tomtom.key"
  echo "Re-run with --with-map-key to include it."
fi

cat > "$OUT/Install moto-tracker.command" <<'CMD'
#!/bin/bash
# Double-click this. It installs the monitor and the app on this Mac.
set -e
cd "$(dirname "$0")"
xattr -dr com.apple.quarantine . 2>/dev/null || true
if ! /usr/bin/python3 -V >/dev/null 2>&1; then
  echo "This Mac needs Apple's command line tools first."
  echo "A dialog will open: click Install, wait for it to finish, then run this again."
  xcode-select --install || true
  exit 1
fi
bash monitor/install.sh --bundle
echo
echo "Press return to close this window."
read -r _
CMD
chmod +x "$OUT/Install moto-tracker.command"

cp "$HERE/bundle-README.md" "$OUT/README.md"

# A zip, ready to send as one file. Ditto keeps the app's signature intact;
# the Finder's own Compress would do the same, but this saves a step.
ZIP="$PROJECT/dist/moto-tracker-for-her-mac.zip"
rm -f "$ZIP"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$OUT" "$ZIP"

echo
echo "bundle ready:"
echo "  folder  $OUT"
echo "  zip     $ZIP  ($(du -h "$ZIP" | cut -f1))"
echo
echo "Send the zip. On her Mac: unzip, then in Terminal run"
echo "    bash <drag 'Install moto-tracker.command' here>"
echo "Double-clicking is blocked by macOS for anything carried from another Mac."
echo "Then make her a code here: gear -> Devices -> Monitor (this Mac) -> Create pairing code."
