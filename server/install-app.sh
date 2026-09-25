#!/bin/sh
# Build and install /Applications/moto-tracker.app — the clickable launcher.
#
# The app depends only on the internal disk (runtime lives in
# ~/Library/Application Support), so it works whether or not this volume is
# mounted. It regenerates the icon only if missing, since that takes a minute.
set -e
cd "$(dirname "$0")"
APP=/Applications/moto-tracker.app

[ -f app/moto-tracker.icns ] || {
    echo "generating icon…"
    /usr/bin/python3 app/mkicon.py
    rm -rf app/icon.iconset && mkdir app/icon.iconset
    for s in 16 32 128 256 512; do
        sips -z $s $s icon.png --out app/icon.iconset/icon_${s}x${s}.png >/dev/null
        sips -z $((s*2)) $((s*2)) icon.png --out app/icon.iconset/icon_${s}x${s}@2x.png >/dev/null
    done
    iconutil -c icns app/icon.iconset -o app/moto-tracker.icns
    rm -rf app/icon.iconset icon.png
}

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp app/Info.plist "$APP/Contents/Info.plist"
cp app/moto-tracker.icns "$APP/Contents/Resources/"
cp app/launcher.sh "$APP/Contents/MacOS/moto-tracker"
chmod +x "$APP/Contents/MacOS/moto-tracker"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true
echo "installed $APP"
