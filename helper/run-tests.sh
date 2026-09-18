#!/usr/bin/env bash
# Run the helper's unit tests (auth policy: peer requirement, device allowlist,
# read-length bounds). Plain `swift test` works when Xcode is installed; with
# Command Line Tools only — which is all DESIGN.md's build needs — swift-testing
# is present but not on the default search paths, so point at it explicitly.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

DEV_DIR="$(xcode-select -p)"
FRAMEWORKS="$DEV_DIR/Library/Developer/Frameworks"
LIBS="$DEV_DIR/Library/Developer/usr/lib"

if [ ! -d "$FRAMEWORKS/Testing.framework" ]; then
    exec swift test "$@"
fi

DYLD_FRAMEWORK_PATH="$FRAMEWORKS" DYLD_LIBRARY_PATH="$LIBS" \
    exec swift test \
        -Xswiftc -F -Xswiftc "$FRAMEWORKS" \
        -Xlinker -F -Xlinker "$FRAMEWORKS" \
        -Xlinker -rpath -Xlinker "$FRAMEWORKS" \
        -Xlinker -rpath -Xlinker "$LIBS" \
        "$@"
