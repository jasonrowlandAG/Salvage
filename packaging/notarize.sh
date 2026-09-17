#!/usr/bin/env bash
# packaging/notarize.sh — submit a signed DMG to Apple's notary service and
# staple the resulting ticket, so Gatekeeper accepts it on other Macs without
# a right-click-Open workaround.
#
# ============================================================================
# STATUS: UNTESTED. Do not run this yet.
#
# This machine currently has only a self-signed "Salvage Dev" code-signing
# identity (`security find-identity -p codesigning` shows just that one, and
# it is untrusted outside this Mac). Notarization requires a real Apple
# Developer ID, which this project does not have yet. Running this script
# today will fail at the `notarytool submit` step for lack of credentials.
# ============================================================================
#
# --- Prerequisites (the owner must complete these BEFORE this script works) ---
#
# 1. Enrol in the paid Apple Developer Program (US$99/year, individual or
#    organisation): https://developer.apple.com/programs/enroll/
#    Notarization is unavailable without this membership, even for a free app.
#
# 2. Create a "Developer ID Application" certificate and install it in this
#    Mac's login keychain:
#      Xcode -> Settings -> Accounts -> Manage Certificates -> "+" ->
#      "Developer ID Application"
#    or via https://developer.apple.com/account/resources/certificates/list
#    Confirm it is present and shows as valid with:
#      security find-identity -p codesigning
#    You are looking for a line such as:
#      1) ABCD1234... "Developer ID Application: Jay Rowland (TEAMID1234)"
#    IMPORTANT: dist/Salvage.app and the DMG must then be BUILT AND SIGNED
#    with this identity, not "Salvage Dev":
#      SALVAGE_SIGN_IDENTITY="Developer ID Application: Jay Rowland (TEAMID1234)" \
#        packaging/build_mac.sh
#      SALVAGE_SIGN_IDENTITY="Developer ID Application: Jay Rowland (TEAMID1234)" \
#        packaging/make_dmg.sh
#    Apple's notary service rejects submissions signed with a self-signed or
#    ad-hoc identity outright, before it even looks at the contents.
#
# 3. Generate an app-specific password for your Apple ID at
#    https://appleid.apple.com -> Sign-In and Security -> App-Specific
#    Passwords. This is NOT your normal Apple ID password and NOT your
#    Developer Program password.
#
# 4. Store those credentials once, locally, as a named keychain profile so
#    this script (and you) never have to type or store the password again:
#      xcrun notarytool store-credentials "salvage-notary" \
#        --apple-id "jay@assemblygrowth.com" \
#        --team-id "TEAMID1234" \
#        --password "the-app-specific-password"
#    This writes to this Mac's keychain, not to any file in this repo.
#    Find your Team ID at https://developer.apple.com/account -> Membership.
#
# --- Usage once all four steps above are done -------------------------------
#   packaging/build_mac.sh      # sign with the real Developer ID identity
#   packaging/make_dmg.sh       # produces dist/Salvage-<version>.dmg, also
#                                # signed with the real Developer ID identity
#   packaging/notarize.sh dist/Salvage-<version>.dmg
#
# notarytool only accepts .dmg, .pkg or .zip submissions (never a raw .app
# directory), which is why this script takes the DMG make_dmg.sh already
# built rather than dist/Salvage.app directly.
set -euo pipefail

PROFILE="${SALVAGE_NOTARY_PROFILE:-salvage-notary}"

if [ $# -ne 1 ]; then
    echo "Usage: packaging/notarize.sh <path-to-Salvage-VERSION.dmg>" >&2
    exit 1
fi
TARGET="$1"

if [ ! -e "$TARGET" ]; then
    echo "error: $TARGET not found. Run packaging/make_dmg.sh first." >&2
    exit 1
fi

if ! xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
    echo "error: no stored notarytool credentials under profile '$PROFILE'." >&2
    echo "See the prerequisites block at the top of this script (step 4) —" >&2
    echo "you must run 'xcrun notarytool store-credentials $PROFILE ...' once first." >&2
    exit 1
fi

echo "==> Submitting $TARGET to Apple's notary service (profile: $PROFILE)"
echo "    This uploads the DMG to Apple and can take anywhere from under a"
echo "    minute to ~30 minutes depending on their queue."
xcrun notarytool submit "$TARGET" --keychain-profile "$PROFILE" --wait

echo "==> Stapling the notarization ticket to $TARGET"
xcrun stapler staple "$TARGET"

echo "==> Validating the staple"
xcrun stapler validate "$TARGET"

echo "==> Gatekeeper assessment (should now say 'accepted', source=Notarized Developer ID)"
spctl --assess --type open --context context:primary-signature -vv "$TARGET"

echo "==> Done. $TARGET is notarized and ready to distribute."
