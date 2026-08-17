#!/usr/bin/env bash
# Build Tether.app, sign it, notarize it, and wrap it in a stapled DMG.
#
# Signing / notarization inputs (all optional — without them you get the
# previous ad-hoc, local-testing-only build):
#
#   APPLE_SIGNING_IDENTITY   "Developer ID Application: Name (TEAMID)".
#                            Auto-detected from the login keychain when unset.
#   TETHER_NOTARY_PROFILE    notarytool keychain profile name (default:
#                            "tether-notary"). Create it once with:
#                              xcrun notarytool store-credentials tether-notary \
#                                --apple-id you@example.com --team-id TEAMID \
#                                --password <app-specific-password>
#                            (or --key/--key-id/--issuer for an App Store
#                            Connect API key). Credentials never touch this
#                            script or the repo.
#   TETHER_SKIP_NOTARIZE=1   sign with Developer ID but do not notarize.
#
# The DMG lands in dist/macos/Tether-<version>.dmg.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DESKTOP_DIR/.." && pwd)"
SOURCE_APP="$DESKTOP_DIR/src-tauri/target/release/bundle/macos/Tether.app"
ENTITLEMENTS="$DESKTOP_DIR/src-tauri/entitlements.plist"
OUTPUT_DIR="$REPO_ROOT/dist/macos"
STAGING_DIR="$OUTPUT_DIR/.tether-dmg-staging"
NOTARY_PROFILE="${TETHER_NOTARY_PROFILE:-tether-notary}"

cleanup() {
    rm -rf "$STAGING_DIR"
}
trap cleanup EXIT

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# Resolve the signing identity.
# ---------------------------------------------------------------------------
IDENTITY="${APPLE_SIGNING_IDENTITY:-}"
if [[ -z "$IDENTITY" ]]; then
    # First "Developer ID Application" certificate in the keychain, if any.
    IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
        | sed -nE 's/^ *[0-9]+\) [0-9A-F]+ "(Developer ID Application: [^"]+)"$/\1/p' \
        | head -n 1 || true)"
fi

NOTARIZE=1
if [[ -z "$IDENTITY" ]]; then
    warn "No Developer ID Application identity found; producing an AD-HOC signed build."
    warn "This DMG is for local testing only and will be blocked by Gatekeeper elsewhere."
    NOTARIZE=0
elif [[ "${TETHER_SKIP_NOTARIZE:-0}" == "1" ]]; then
    warn "TETHER_SKIP_NOTARIZE=1: signing with '$IDENTITY' but skipping notarization."
    NOTARIZE=0
else
    # Fail fast if the notary profile is missing, before a long build.
    if ! xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
        echo "error: notarytool keychain profile '$NOTARY_PROFILE' is not usable." >&2
        echo "       Create it with: xcrun notarytool store-credentials $NOTARY_PROFILE \\" >&2
        echo "         --apple-id <apple-id> --team-id <TEAMID> --password <app-specific-password>" >&2
        echo "       or set TETHER_SKIP_NOTARIZE=1 to sign without notarizing." >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Build the app bundle.
# ---------------------------------------------------------------------------
cd "$DESKTOP_DIR"
VERSION="$(node -p "require('./package.json').version")"
OUTPUT_DMG="$OUTPUT_DIR/Tether-$VERSION.dmg"

log "Building Tether.app $VERSION"
if [[ -n "$IDENTITY" ]]; then
    # Let the Tauri bundler sign with the real identity (hardened runtime on
    # by default). We re-sign below anyway so the result is deterministic
    # regardless of bundler version.
    export APPLE_SIGNING_IDENTITY="$IDENTITY"
fi
npm run desktop:build:macos

# ---------------------------------------------------------------------------
# Sign.
# ---------------------------------------------------------------------------
if [[ -n "$IDENTITY" ]]; then
    log "Signing with: $IDENTITY"
    # Sign nested code first (frameworks/helpers), then the bundle. --deep is
    # acceptable for this bundle shape (a single Tauri binary + WebKit is
    # provided by the OS), but nested Mach-O binaries are signed explicitly
    # so a future helper binary is not missed.
    while IFS= read -r -d '' nested; do
        codesign --force --options runtime --timestamp \
            --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$nested"
    done < <(find "$SOURCE_APP/Contents" -type f -perm -u+x \
                ! -path "$SOURCE_APP/Contents/MacOS/*" -print0 2>/dev/null)
    codesign --force --options runtime --timestamp \
        --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$SOURCE_APP"
else
    codesign --force --deep --sign - "$SOURCE_APP"
fi
codesign --verify --deep --strict --verbose=2 "$SOURCE_APP"

# ---------------------------------------------------------------------------
# Notarize + staple the app itself so it is valid even when dragged out of
# the DMG on an offline machine.
# ---------------------------------------------------------------------------
notarize_path() {
    local path="$1"
    log "Notarizing $(basename "$path") (this waits for Apple; typically 1–10 min)"
    local submit_out
    submit_out="$(xcrun notarytool submit "$path" \
        --keychain-profile "$NOTARY_PROFILE" --wait --output-format json)"
    local status id
    status="$(printf '%s' "$submit_out" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')"
    id="$(printf '%s' "$submit_out" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')"
    if [[ "$status" != "Accepted" ]]; then
        echo "error: notarization of $(basename "$path") ended with status '$status' (submission $id)." >&2
        echo "       Details:" >&2
        xcrun notarytool log "$id" --keychain-profile "$NOTARY_PROFILE" >&2 || true
        exit 1
    fi
    echo "notarization accepted (submission $id)"
}

mkdir -p "$OUTPUT_DIR"
if [[ "$NOTARIZE" == "1" ]]; then
    APP_ZIP="$OUTPUT_DIR/.Tether-$VERSION-notarize.zip"
    rm -f "$APP_ZIP"
    ditto -c -k --keepParent "$SOURCE_APP" "$APP_ZIP"
    notarize_path "$APP_ZIP"
    rm -f "$APP_ZIP"
    xcrun stapler staple "$SOURCE_APP"
fi

# ---------------------------------------------------------------------------
# Package the DMG.
# ---------------------------------------------------------------------------
log "Packaging DMG"
find "$OUTPUT_DIR" -maxdepth 1 -type f -name 'Tether-*.dmg' ! -name "Tether-$VERSION.dmg" -delete
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"
ditto "$SOURCE_APP" "$STAGING_DIR/Tether.app"
ln -s /Applications "$STAGING_DIR/Applications"

hdiutil create \
    -volname "Tether" \
    -srcfolder "$STAGING_DIR" \
    -ov \
    -format UDZO \
    "$OUTPUT_DMG"

if [[ -n "$IDENTITY" ]]; then
    codesign --force --timestamp --sign "$IDENTITY" "$OUTPUT_DMG"
    codesign --verify --verbose=2 "$OUTPUT_DMG"
fi

if [[ "$NOTARIZE" == "1" ]]; then
    notarize_path "$OUTPUT_DMG"
    xcrun stapler staple "$OUTPUT_DMG"
    log "Gatekeeper assessment"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$OUTPUT_DMG"
    spctl --assess --type execute --verbose=2 "$SOURCE_APP"
fi

log "Done"
echo "$OUTPUT_DMG"
if [[ "$NOTARIZE" != "1" ]]; then
    echo "(not notarized)"
fi
