#!/usr/bin/env bash
# Build dist/Salvage.app: PyInstaller one-dir bundle + bundled photorec/
# libimobiledevice CLI tools with their Homebrew dylibs relinked in.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYINSTALLER=".venv/bin/pyinstaller"
PYTHON=".venv/bin/python"

if [ ! -x "$PYINSTALLER" ]; then
    echo "error: $PYINSTALLER not found. Install packaging deps first:" >&2
    echo "  uv pip install --python .venv/bin/python -e '.[dev]'" >&2
    exit 1
fi

echo "==> Running PyInstaller"
"$PYINSTALLER" --noconfirm --clean packaging/salvage.spec

APP="dist/Salvage.app"
BIN_DEST="$APP/Contents/Frameworks/salvage/bin/macos"
mkdir -p "$BIN_DEST"

echo "==> Bundling photorec + libimobiledevice CLI tools"
TOOLS=(photorec idevice_id ideviceinfo idevicebackup2 afcclient)
BUNDLED_PATHS=()
for name in "${TOOLS[@]}"; do
    src="/opt/homebrew/bin/$name"
    if [ ! -e "$src" ]; then
        echo "warning: $src not found on this machine; $name will be missing from the bundle" >&2
        continue
    fi
    real_src="$(readlink -f "$src")"
    dest="$BIN_DEST/$name"
    cp "$real_src" "$dest"
    chmod +x "$dest"
    BUNDLED_PATHS+=("$dest")
done

echo "==> Rewriting install names for bundled dylib dependencies"
# photorec only links system libs (libncurses/libz/libiconv/libSystem, all under
# /usr/lib) so it needs no relinking; only pass the libimobiledevice tools.
RELINK_TARGETS=()
for p in "${BUNDLED_PATHS[@]}"; do
    [[ "$p" == *"/photorec" ]] && continue
    RELINK_TARGETS+=("$p")
done
if [ "${#RELINK_TARGETS[@]}" -gt 0 ]; then
    "$PYTHON" packaging/relink_macho.py "$APP" "${RELINK_TARGETS[@]}"
fi

echo "==> Verifying no bundled binary still references Homebrew"
offenders="$(find "$APP" -type f -perm -u+x -exec otool -L {} \; 2>/dev/null \
    | grep -E '/opt/homebrew|/usr/local/(opt|Cellar)' || true)"
if [ -n "$offenders" ]; then
    echo "error: found Homebrew references still in the bundle:" >&2
    echo "$offenders" >&2
    exit 1
fi
echo "OK: no Homebrew references remain in $APP"

# A stable identity keeps the app's code requirement constant across rebuilds, so a
# Full Disk Access grant made in System Settings survives; ad-hoc signatures change
# every build and silently invalidate it.
IDENTITY="${SALVAGE_SIGN_IDENTITY:-Salvage Dev}"

# The SMAppService root helper (helper/DESIGN.md) is a proven prototype but is not
# wired into scanning: the kernel refuses raw reads of mounted volumes regardless of
# privilege, so it buys nothing for the internal disk. Opt in with SALVAGE_BUILD_HELPER=1.
if [ "${SALVAGE_BUILD_HELPER:-0}" = "1" ] && command -v swiftc >/dev/null 2>&1; then
    echo "==> Building SalvageHelper (SMAppService daemon) + salvage-register CLI"
    mkdir -p helper/build
    swiftc -O helper/SalvageHelper/main.swift -o helper/build/SalvageHelper
    swiftc -O helper/salvage-register/main.swift -o helper/build/salvage-register
    mkdir -p "$APP/Contents/Library/LaunchDaemons"
    cp helper/build/SalvageHelper "$APP/Contents/MacOS/SalvageHelper"
    cp helper/build/salvage-register "$APP/Contents/MacOS/salvage-register"
    cp helper/com.salvage.helper.plist "$APP/Contents/Library/LaunchDaemons/com.salvage.helper.plist"
    chmod +x "$APP/Contents/MacOS/SalvageHelper" "$APP/Contents/MacOS/salvage-register"
    # The daemon and CLI must carry the same signing identity as the app itself
    # (SMAppService checks this); sign them now, then the --deep sign below reseals
    # the whole bundle including these new files.
    if security find-identity -p codesigning 2>/dev/null | grep -q "\"$IDENTITY\""; then
        codesign --force --sign "$IDENTITY" "$APP/Contents/MacOS/SalvageHelper"
        codesign --force --sign "$IDENTITY" "$APP/Contents/MacOS/salvage-register"
    else
        codesign --force --sign - "$APP/Contents/MacOS/SalvageHelper"
        codesign --force --sign - "$APP/Contents/MacOS/salvage-register"
    fi
fi

if security find-identity -p codesigning 2>/dev/null | grep -q "\"$IDENTITY\""; then
    echo "==> Code signing with identity: $IDENTITY"
    codesign --force --deep --sign "$IDENTITY" "$APP"
else
    echo "==> Ad-hoc code signing (no '$IDENTITY' identity in keychain)"
    codesign --force --deep --sign - "$APP"
fi

echo "==> Bundle size"
du -sh "$APP"
