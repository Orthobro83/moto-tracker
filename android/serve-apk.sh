#!/bin/sh
# Delivery mechanism for a phone with no ADB (design.md "Installing without ADB").
#
# Binds all interfaces by default. Inbound over Meshnet is currently blocked on
# the Mini (2026-09-08) while outbound works, so the mesh URL may time out and the
# LAN URL will not. This is the transient APK server, not the FastAPI service, so
# the mesh-only binding rule in Phase 2 does not apply to it.
#
#   ./serve-apk.sh                  both LAN and mesh
#   ./serve-apk.sh 100.64.0.10    mesh only, once inbound is fixed
set -e
BIND="${1:-0.0.0.0}"
APK=app/build/outputs/apk/debug/app-debug.apk
[ -f "$APK" ] || { echo "no APK at $APK — run ./gradlew assembleDebug first"; exit 1; }
mkdir -p .serve && cp "$APK" .serve/moto-tracker.apk
echo "On the phone, open whichever reaches:"
echo "  LAN (home wifi):  http://192.168.1.254:8000/moto-tracker.apk"
echo "  Meshnet:          http://100.64.0.10:8000/moto-tracker.apk"
cd .serve && exec /usr/bin/python3 -m http.server 8000 --bind "$BIND"
