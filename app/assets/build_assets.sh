#!/bin/bash
# Regenerates the PNG/ICNS icons from icon.svg, menubar.svg and tv-icon.svg in this folder.
# Edit the SVGs, run this script, then app/build.sh.
set -euo pipefail
cd "$(dirname "$0")"
TMP="$(mktemp -d)"
swiftc -O -framework AppKit -o "$TMP/render_svg" render_svg.swift
R="$TMP/render_svg"
"$R" icon.svg AppIcon-1024.png 1024 1024
mkdir -p "$TMP/AppIcon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s AppIcon-1024.png --out "$TMP/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) AppIcon-1024.png --out "$TMP/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/AppIcon.iconset" -o AppIcon.icns
"$R" menubar.svg MenuIcon.png 30 18
"$R" menubar.svg MenuIcon@2x.png 60 36
"$R" tv-icon.svg ../../receiver/tizen/icon.png 512 423
rm -rf "$TMP"
echo "Icons regenerated: AppIcon.icns, MenuIcon*.png, receiver/tizen/icon.png"
