#!/bin/bash
# Recompile the app icon from its Icon Composer source (macapp/Visionary.icon) into the two files
# build.sh ships: Assets.car (the live glass icon macOS 26 renders) and Visionary.icns (the flat
# fallback for older systems). Needs full Xcode (26+) for actool — the Command Line Tools do not
# carry it. DEVELOPER_DIR points just this script at Xcode; the system's xcode-select is untouched.
# Both outputs are committed, so building the app never needs Xcode.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
xcrun actool --compile "$OUT" --platform macosx --minimum-deployment-target 13.0 \
  --app-icon Visionary --output-partial-info-plist "$OUT/partial.plist" \
  "$ROOT/macapp/Visionary.icon" >/dev/null
cp "$OUT/Assets.car" "$ROOT/macapp/Assets.car"
cp "$OUT/Visionary.icns" "$ROOT/macapp/Visionary.icns"
echo "compiled: macapp/Assets.car + macapp/Visionary.icns (icon name: Visionary)"
