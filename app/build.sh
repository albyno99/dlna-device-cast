#!/bin/bash
# Builds "Device Cast.app" in the project root from app/DeviceCast.swift.
# Requires swiftc (Command Line Tools: xcode-select --install).
#
# The app does not bundle the scripts: it runs sender/devicecast.py from the project folder and
# writes config and log to data/. Editing the scripts therefore leaves the app signature unchanged
# and macOS keeps the Screen Recording and Local Network permissions.
#
# The bundle is rebuilt only when DeviceCast.swift or the icons in app/assets change. Every rebuild
# changes the ad-hoc signature, so macOS asks for Screen Recording permission again.
#
# Usage:  app/build.sh           rebuild only if needed
#         app/build.sh --force   always rebuild
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
APP="$ROOT/Device Cast.app"
SRC="$HERE/DeviceCast.swift"
ASSETS=("$HERE/assets/AppIcon.icns" "$HERE/assets/MenuIcon.png" "$HERE/assets/MenuIcon@2x.png")
SRC_HASH=$(cat "$SRC" "${ASSETS[@]}" | shasum -a 256 | cut -c1-16)

if [ "${1:-}" != "--force" ] && [ -f "$APP/Contents/Resources/source.hash" ] \
   && [ "$(cat "$APP/Contents/Resources/source.hash")" = "$SRC_HASH" ]; then
  echo "App unchanged, not rebuilding (permissions stay valid)."
  exit 0
fi
command -v swiftc >/dev/null || { echo "swiftc not found: install the Command Line Tools with  xcode-select --install"; exit 1; }

osascript -e 'quit app "Device Cast"' >/dev/null 2>&1 || true
BUILD="$(mktemp -d)"
NEW="$BUILD/Device Cast.app"
mkdir -p "$NEW/Contents/MacOS" "$NEW/Contents/Resources" "$ROOT/data"
swiftc -O -swift-version 5 -framework Cocoa -o "$NEW/Contents/MacOS/Device Cast" "$SRC"
echo "$SRC_HASH" > "$NEW/Contents/Resources/source.hash"
cp "${ASSETS[@]}" "$NEW/Contents/Resources/"

cat > "$NEW/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Device Cast</string>
  <key>CFBundleDisplayName</key><string>Device Cast</string>
  <key>CFBundleIdentifier</key><string>local.devicecast</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Device Cast</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>DeviceCastProjectDir</key><string>$ROOT</string>
  <key>NSScreenCaptureUsageDescription</key><string>Device Cast captures the screen to send it to the TV.</string>
  <key>NSMicrophoneUsageDescription</key><string>Only needed if you choose to cast an audio device.</string>
  <key>NSLocalNetworkUsageDescription</key><string>Device Cast searches for the TV on the local network.</string>
</dict></plist>
PLIST

# Ad-hoc signature: a stable identity for the permissions as long as the source does not change.
xattr -cr "$NEW"
codesign --force --sign - "$NEW"
rm -rf "$APP"
mv "$NEW" "$APP"
rmdir "$BUILD" 2>/dev/null || true
echo "Built: $APP"
