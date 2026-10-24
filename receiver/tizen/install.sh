#!/bin/bash
# Build, sign and install the Device Cast receiver on a Samsung TV in Developer Mode.
#
# Usage: receiver/tizen/install.sh <tv-ip> [security-profile]
#
# TIZEN_HOME     Tizen Studio location (default: <project>/tools.noindex/tizen-studio)
# TIZEN_PROFILE  signing profile created with `tizen security-profiles add` (default: second argument)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
TV="${1:?usage: $0 <tv-ip> [security-profile]}"
PROFILE="${2:-${TIZEN_PROFILE:?set TIZEN_PROFILE or pass the signing profile as second argument}}"
TIZEN_HOME="${TIZEN_HOME:-$ROOT/tools.noindex/tizen-studio}"
TIZEN="$TIZEN_HOME/tools/ide/bin/tizen"
SDB="$TIZEN_HOME/tools/sdb"
PKG="DevCast001"
REMOTE="/home/owner/share/tmp/sdk_tools/tmp/DeviceCast.wgt"

[ -x "$TIZEN" ] || { echo "Tizen CLI not found in $TIZEN_HOME" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Package only the app files (not this script).
mkdir -p "$WORK/app" "$WORK/out"
cp -R "$HERE/config.xml" "$HERE/index.html" "$HERE/icon.png" "$HERE/fonts" "$WORK/app/"
"$TIZEN" package -t wgt -s "$PROFILE" -o "$WORK/out" -- "$WORK/app" >/dev/null
WGT="$(ls "$WORK"/out/*.wgt)"

"$SDB" connect "$TV" >/dev/null
"$SDB" -s "$TV:26101" push "$WGT" "$REMOTE" >/dev/null 2>&1
RESULT="$("$SDB" -s "$TV:26101" shell 0 vd_appinstall "$PKG" "$REMOTE" 2>&1)"
if grep -q "install completed" <<<"$RESULT"; then
  echo "Installed Device Cast on $TV"
else
  echo "$RESULT" | grep -E "install|fail" >&2
  exit 1
fi
