#!/bin/bash
set -e
# Repo root: derived from this script's location; override with VISIONARY_ROOT if needed.
ROOT="${VISIONARY_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
APP="$ROOT/Visionary.app"
TMP="$(mktemp -d)"
BIN="$TMP/Visionary"
# Local overrides (gitignored): e.g. BUNDLE_ID for an existing install's TCC continuity.
[ -f "$ROOT/.env" ] && . "$ROOT/.env"
# Bundle id: env-overridable. TCC (Screen Recording + Accessibility) keys on the bundle id +
# the signing cert, so KEEP whatever id you first granted with — changing it resets grants.
# (The maintainer exports BUNDLE_ID=com.adambritsch.overnightupscaler locally for that reason.)
BUNDLE_ID="${BUNDLE_ID:-com.visionary.upscaler}"

# 1. COMPILE FIRST — to a temp path, so a build error never wrecks the working app.
swiftc \
  "$ROOT/macapp/Models.swift" \
  "$ROOT/macapp/Store.swift" \
  "$ROOT/macapp/Views.swift" \
  "$ROOT/macapp/main.swift" \
  -o "$BIN" -framework AppKit -framework SwiftUI

# 2. Assemble the bundle. The bundle id is UNCHANGED across the Overnight Upscaler ->
#    Visionary rename ON PURPOSE: TCC (Screen Recording + Accessibility) keys on the
#    bundle id + the cert-based designated requirement, so keeping it makes the existing
#    grants survive the rename.
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/engine"

# Icon: prefer the compiled Icon Composer .icon (Assets.car -> the live glass icon the
# system renders dynamically) when it's present; otherwise fall back to the flattened
# AppIcon.icns. Emit the matching Info.plist keys for whichever we have.
ICON_KEYS='  <key>CFBundleIconFile</key><string>UpscalerDV</string>'
if [ -f "$ROOT/macapp/Assets.car" ]; then
  cp "$ROOT/macapp/Assets.car" "$APP/Contents/Resources/Assets.car"
  ICON_KEYS='  <key>CFBundleIconName</key><string>UpscalerDV</string>
  <key>CFBundleIconFile</key><string>UpscalerDV</string>'
fi
[ -f "$ROOT/macapp/UpscalerDV.icns" ] && cp "$ROOT/macapp/UpscalerDV.icns" "$APP/Contents/Resources/UpscalerDV.icns"
# Header mark: the recolored Dolby Vision logo (NSImage renders the SVG with its gradient
# + transparent Ds at runtime).
[ -f "$ROOT/macapp/DolbyVision.svg" ] && cp "$ROOT/macapp/DolbyVision.svg" "$APP/Contents/Resources/DolbyVision.svg"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Visionary</string>
  <key>CFBundleDisplayName</key><string>Visionary</string>
  <key>CFBundleExecutable</key><string>Visionary</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>0.3</string>
  <key>CFBundleShortVersionString</key><string>0.3</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
$ICON_KEYS
</dict></plist>
PLIST

rsync -a --exclude __pycache__ --exclude '*.pyc' --exclude manifests "$ROOT/engine/" "$APP/Contents/Resources/engine/"
cp "$BIN" "$APP/Contents/MacOS/Visionary"
# Sign with the STABLE self-signed identity (see setup-signing-cert.sh) so TCC keys on
# the cert-based designated requirement, not the binary hash — Screen Recording +
# Accessibility grants then survive rebuilds. Falls back to ad-hoc if the cert is gone.
IDENTITY="Overnight Upscaler Local Signing"
if codesign --force --deep -s "$IDENTITY" --timestamp=none "$APP" 2>/dev/null; then
  echo "signed with stable identity ($IDENTITY) — grants survive rebuilds"
else
  echo "WARN: '$IDENTITY' not found — ad-hoc fallback (grants WILL reset). Run macapp/setup-signing-cert.sh"
  codesign --force --deep -s - "$APP" 2>/dev/null || true
fi
rm -rf "$TMP"
echo "built: $APP"
