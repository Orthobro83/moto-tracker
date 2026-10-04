#!/bin/bash
#
# Builds the folder to hand to the second Mac (Dana's), so it can run the same app
# without this project, Xcode, or anyone touching the VPS.
#
#   bash app/make-bundle.sh                     # no secrets: the map draws blank
#   bash app/make-bundle.sh --with-map-key      # include the TomTom key as well
#
# What goes in: the app's source (her Mac builds it — an app carried between Macs is
# refused as "damaged"), the page, the installer, and the CA certificate, which is
# public: it only says which certificate to trust. What never goes in: any device key,
# the CA's private key, or secrets.env. The second Mac gets its own device key by
# typing a pairing code into its own window.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/moto-tracker"
OUT="$PROJECT/dist/moto-tracker-for-her-mac"
WITH_MAP_KEY=0
[ "${1:-}" = "--with-map-key" ] && WITH_MAP_KEY=1

rm -rf "$OUT"
mkdir -p "$OUT/app/Core" "$OUT/app/ui" "$OUT/pki"
cp "$HERE/MotoTracker.swift" "$HERE/PageHandler.swift" "$HERE/build.sh" "$HERE/install.sh" "$OUT/app/"
cp "$HERE"/Core/*.swift "$OUT/app/Core/"
cp "$HERE"/ui/* "$OUT/app/ui/"

# Proof before shipping: the app builds from what is in the bundle alone.
CHECK="$(mktemp -d)"
if ! bash "$OUT/app/build.sh" "$CHECK/moto-tracker.app" >/dev/null 2>"$CHECK/err"; then
  echo "REFUSED: the bundled app does not build:" >&2
  tail -n 20 "$CHECK/err" >&2
  rm -rf "$CHECK"
  exit 1
fi
rm -rf "$CHECK"
echo "the bundled app builds cleanly"

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
# Run this from Terminal (see README.md). It builds and installs the moto-tracker app.
set -e
cd "$(dirname "$0")"
xattr -dr com.apple.quarantine . 2>/dev/null || true
if ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "This Mac needs Apple's command line tools first."
  echo "A dialog will open: click Install, wait for it to finish, then run this again."
  xcode-select --install || true
  exit 1
fi
bash app/install.sh --bundle
echo
echo "Press return to close this window."
read -r _
CMD
chmod +x "$OUT/Install moto-tracker.command"

cp "$HERE/bundle-README.md" "$OUT/README.md"

# A zip, ready to send as one file.
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
echo "Her Mac is already paired; an update keeps its key."
