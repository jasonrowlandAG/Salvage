#!/usr/bin/env bash
# packaging/make_dmg.sh — package dist/Salvage.app into a compressed,
# read-only Salvage-<version>.dmg with an /Applications shortcut, a simple
# icon-view layout, and a generated background image. Signed with whatever
# identity packaging/build_mac.sh would use (defaults to the local "Salvage
# Dev" identity).
#
# This produces an UNNOTARIZED dmg: opening the app from it still needs
# right-click -> Open on a Mac other than the one that built it, exactly like
# dist/Salvage.app itself today. See packaging/notarize.sh for the (currently
# untested, credential-gated) next step.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP="dist/Salvage.app"
if [ ! -d "$APP" ]; then
    echo "error: $APP not found. Run packaging/build_mac.sh first." >&2
    exit 1
fi

VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")"
VOL_NAME="Salvage"
DMG_PATH="dist/Salvage-${VERSION}.dmg"

STAGING="$(mktemp -d)"
RW_DMG="$(mktemp -u).dmg"
cleanup() {
    if [ -n "${DEVICE:-}" ]; then
        hdiutil detach "$DEVICE" -quiet 2>/dev/null || true
    fi
    rm -rf "$STAGING" "$RW_DMG"
}
trap cleanup EXIT

echo "==> Staging DMG contents for version $VERSION"
rm -f "$DMG_PATH"
mkdir -p dist
cp -R "$APP" "$STAGING/Salvage.app"
ln -s /Applications "$STAGING/Applications"

echo "==> Generating background image"
mkdir -p "$STAGING/.background"
HAVE_BACKGROUND=0
if [ -x ".venv/bin/python" ] && .venv/bin/python - "$STAGING/.background/background.png" <<'PYEOF'
import sys

try:
    from PIL import Image, ImageDraw, ImageFont

    W, H = 660, 400
    img = Image.new("RGB", (W, H), "#f4f4f6")
    draw = ImageDraw.Draw(img)

    # Arrow from the app icon position to the Applications alias position
    # (icon positions set below via Finder AppleScript are chosen to line up
    # with this).
    y = 195
    draw.line([(235, y), (420, y)], fill="#a0a0a8", width=3)
    draw.polygon([(420, y - 9), (420, y + 9), (441, y)], fill="#a0a0a8")

    font = None
    for path in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNSText.ttf"):
        try:
            font = ImageFont.truetype(path, 18)
            break
        except OSError:
            pass

    text = "Drag Salvage into Applications"
    if font is not None:
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text(((W - (bbox[2] - bbox[0])) / 2, 55), text, fill="#3a3a3c", font=font)

    img.save(sys.argv[1])
except Exception as exc:  # cosmetic only -- never fail the DMG build over this
    print(f"warning: background generation skipped ({exc})", file=sys.stderr)
    raise SystemExit(1)
PYEOF
then
    HAVE_BACKGROUND=1
else
    echo "warning: could not generate DMG background image (Pillow unavailable?); continuing without one" >&2
    rm -rf "$STAGING/.background"
fi

echo "==> Building read-write staging image"
hdiutil create -volname "$VOL_NAME" -srcfolder "$STAGING" -fs HFS+ -format UDRW -ov "$RW_DMG" >/dev/null

echo "==> Mounting staging image to arrange the Finder window"
ATTACH_INFO="$(hdiutil attach -readwrite -noverify -noautoopen -plist "$RW_DMG" | /usr/bin/python3 -c '
import sys, plistlib
d = plistlib.loads(sys.stdin.buffer.read())
for e in d["system-entities"]:
    if "mount-point" in e:
        print(e["dev-entry"])
        print(e["mount-point"])
        break
')"
DEVICE="$(echo "$ATTACH_INFO" | sed -n 1p)"
MOUNT_DIR="$(echo "$ATTACH_INFO" | sed -n 2p)"

if [ "$HAVE_BACKGROUND" = "1" ] && [ -n "$MOUNT_DIR" ]; then
    if ! osascript <<OSAEOF
tell application "Finder"
    tell disk "${VOL_NAME}"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {200, 120, 860, 520}
        set theViewOptions to the icon view options of container window
        set arrangement of theViewOptions to not arranged
        set icon size of theViewOptions to 96
        set background picture of theViewOptions to file ".background:background.png"
        set position of item "Salvage.app" of container window to {140, 190}
        set position of item "Applications" of container window to {480, 190}
        close
        open
        update without registering applications
        delay 1
    end tell
end tell
OSAEOF
    then
        echo "warning: Finder window layout script failed (no Automation permission for this process?); DMG will still work, just with the default icon layout." >&2
    fi
fi

echo "==> Detaching staging image"
hdiutil detach "$DEVICE" -quiet
DEVICE=""  # already detached; skip the cleanup trap's detach attempt

echo "==> Converting to compressed read-only DMG"
hdiutil convert "$RW_DMG" -format UDZO -imagekey zlib-level=9 -ov -o "$DMG_PATH" >/dev/null

IDENTITY="${SALVAGE_SIGN_IDENTITY:-Salvage Dev}"
if security find-identity -p codesigning 2>/dev/null | grep -q "\"$IDENTITY\""; then
    echo "==> Code signing DMG with identity: $IDENTITY"
    codesign --force --sign "$IDENTITY" "$DMG_PATH"
else
    echo "==> Ad-hoc signing DMG (no '$IDENTITY' identity in keychain)"
    codesign --force --sign - "$DMG_PATH"
fi

echo "==> Done: $DMG_PATH"
du -h "$DMG_PATH"
echo
echo "Note: this DMG is signed but NOT notarized -- spctl will reject it on"
echo "any Mac (including this one) until packaging/notarize.sh has been run"
echo "with real Apple Developer ID credentials. That rejection is expected;"
echo "see docs/release-checklist.md."
