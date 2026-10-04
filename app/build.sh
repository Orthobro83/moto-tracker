#!/bin/bash
#
# Builds moto-tracker.app: the whole Mac side of moto-tracker in one app
# (design.md 2026-10-04). It talks to the relay itself; there is no service to go with it.
#
#   bash app/build.sh [destination]          # default: build/moto-tracker.app beside this script
#   bash app/build.sh --harness <file>       # test only: the same code, no window
#   bash app/build.sh --page-check <file>    # test only: the real page in real WebKit
#
# Ad-hoc signed: this Mac runs it, nothing else has to.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="moto-tracker"

# Apple's Command Line Tools are enough — they carry swiftc and the macOS SDK — but the
# compiler has to be run THROUGH xcrun, which is what puts the SDK in its hands.
# Calling swiftc directly fails with "unable to load standard library".
if ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "No Swift compiler on this Mac. Install Apple's command line tools with:" >&2
  echo "    xcode-select --install" >&2
  exit 1
fi
swiftc() {
  xcrun swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos13.0" -sdk "$(xcrun --show-sdk-path)" "$@"
}

if [ "${1:-}" = "--harness" ] || [ "${1:-}" = "--page-check" ]; then
  OUT="${2:?where should it go?}"
  mkdir -p "$(dirname "$OUT")"
  if [ "$1" = "--harness" ]; then
    swiftc -o "$OUT" "$HERE"/Core/*.swift "$HERE/Harness/main.swift"
  else
    swiftc -o "$OUT" "$HERE"/Core/*.swift "$HERE/PageHandler.swift" "$HERE/PageCheck/main.swift"
  fi
  echo "built $OUT"
  exit 0
fi

DEST="${1:-$HERE/build/moto-tracker.app}"
rm -rf "$DEST"
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources/ui"

cat > "$DEST/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>moto-tracker</string>
  <key>CFBundleDisplayName</key><string>moto-tracker</string>
  <key>CFBundleIdentifier</key><string>com.example.mototracker</string>
  <key>CFBundleExecutable</key><string>moto-tracker</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>4.0</string>
  <key>CFBundleVersion</key><string>4</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>private</string>
  <!-- Never napped: the relay loop and the alarm keep time with the window hidden. -->
  <key>NSAppSleepDisabled</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict>
    <!-- Only for app/demo.py, which points a copy of the app at a throwaway relay on
         this Mac. The installed app talks to the relay over HTTPS. -->
    <key>NSAllowsLocalNetworking</key><true/>
    <!-- The relay's certificate comes from our own CA, which the app checks itself
         (Relay.swift): the chain must end at that CA and name this address. Without
         this, macOS's app rules insist on a CA from the system's list as well. -->
    <key>NSExceptionDomains</key>
    <dict>
      <key>203.0.113.10</key>
      <dict>
        <key>NSExceptionAllowsInsecureHTTPLoads</key><true/>
      </dict>
    </dict>
  </dict>
</dict>
</plist>
PLIST

echo "compiling…"
swiftc -parse-as-library -o "$DEST/Contents/MacOS/$NAME" "$HERE/MotoTracker.swift" "$HERE/PageHandler.swift" \
  "$HERE"/Core/*.swift

# The page, inside the app: it is drawn from here, not fetched from anywhere.
cp "$HERE"/ui/* "$DEST/Contents/Resources/ui/"

# A small icon so the Dock and the About panel are not a blank page.
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
/usr/bin/python3 - "$ICONSET" <<'PY'
import struct, sys, zlib
from pathlib import Path

def png(path, size):
    """A dark rounded square with a bright dot: the rider on the map."""
    cx = cy = (size - 1) / 2
    radius, dot, corner = size * 0.46, size * 0.12, size * 0.22
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            dx, dy = x - cx, y - cy
            # rounded square mask
            inside = (abs(dx) <= radius and abs(dy) <= radius)
            ox, oy = abs(dx) - (radius - corner), abs(dy) - (radius - corner)
            if inside and ox > 0 and oy > 0 and (ox * ox + oy * oy) > corner * corner:
                inside = False
            d = (dx * dx + dy * dy) ** 0.5
            if not inside:
                row += bytes((0, 0, 0, 0))
            elif d <= dot:
                row += bytes((77, 163, 255, 255))
            elif d <= dot * 1.9:
                a = int(255 * (1 - (d - dot) / (dot * 0.9)) * 0.45)
                row += bytes((77, 163, 255, max(0, a) + 0)) if a > 0 else bytes((23, 26, 32, 255))
            else:
                row += bytes((23, 26, 32, 255))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                           + chunk(b"IDAT", raw) + chunk(b"IEND", b""))

out = Path(sys.argv[1])
for size, name in ((16, "icon_16x16"), (32, "icon_16x16@2x"), (32, "icon_32x32"),
                   (64, "icon_32x32@2x"), (128, "icon_128x128"), (256, "icon_128x128@2x"),
                   (256, "icon_256x256"), (512, "icon_256x256@2x"), (512, "icon_512x512"),
                   (1024, "icon_512x512@2x")):
    png(out / f"{name}.png", size)
PY
if ! iconutil -c icns "$ICONSET" -o "$DEST/Contents/Resources/AppIcon.icns" 2>/dev/null; then
  echo "  (no icon: iconutil unavailable — the app is fine without one)"
fi
rm -rf "$(dirname "$ICONSET")"

# How the installer recognises its own app. It must exist BEFORE signing: anything
# added to a signed bundle afterwards breaks the seal, and a broken seal is what macOS
# calls "damaged" — fatal on a Mac the app was carried to.
echo "moto-tracker app" > "$DEST/Contents/Resources/moto-tracker-app"

codesign --force --deep --sign - "$DEST" >/dev/null
codesign --verify --strict "$DEST" || { echo "the built app is not sealed" >&2; exit 1; }
echo "built $DEST"
