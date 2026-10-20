#!/usr/bin/env bash
# Build the native Tether.app and package a verified DMG. --legacy selects Tauri.
# --adhoc skips signing-identity discovery and notarization for a local build.
# --fetch permits the native builder to download missing pinned assets.
# TETHER_BUILD_PYTHON selects Python; defaults to the repo venv, then python3.
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
ENTITLEMENTS="$DESKTOP_DIR/src-tauri/entitlements.plist"
OUTPUT_DIR="$REPO_ROOT/dist/macos"
NOTARY_PROFILE="${TETHER_NOTARY_PROFILE:-tether-notary}"
TARGET=native
ADHOC=0
FETCH=0
WORK_DIR=""
MOUNTED=0
for arg in "$@"; do
    case "$arg" in
        --legacy) TARGET=legacy ;;
        --adhoc) ADHOC=1 ;;
        --fetch) FETCH=1 ;;
        --help|-h)
            echo "Usage: $0 [--legacy] [--adhoc] [--fetch]"
            echo "Build the native terminal DMG; --legacy builds the Tauri desktop instead."
            exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done
if [[ "$TARGET" == legacy && "$FETCH" == 1 ]]; then
    echo "--fetch is only supported for the native app." >&2
    exit 2
fi

cleanup() {
    if [[ "$MOUNTED" == 1 ]]; then
        if ! hdiutil detach "$MOUNT_DIR"; then
            echo "Could not detach $MOUNT_DIR; preserving $WORK_DIR for cleanup." >&2
            return
        fi
    fi
    if [[ -n "$WORK_DIR" ]]; then rm -rf "$WORK_DIR"; fi
}
trap cleanup EXIT

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# Resolve the signing identity.
# ---------------------------------------------------------------------------
IDENTITY="${APPLE_SIGNING_IDENTITY:-}"
if [[ "$ADHOC" == 1 ]]; then
    IDENTITY=""
    unset APPLE_SIGNING_IDENTITY
elif [[ -z "$IDENTITY" ]]; then
    # First "Developer ID Application" certificate in the keychain, if any.
    IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
        | sed -nE 's/^ *[0-9]+\) [0-9A-F]+ "(Developer ID Application: [^"]+)"$/\1/p' \
        | head -n 1 || true)"
fi

NOTARIZE=1
if [[ -z "$IDENTITY" ]]; then
    warn "Producing an ad-hoc signed local build; it is not notarized for distribution."
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
log "Building Tether.app ($TARGET)"
if [[ -n "$IDENTITY" ]]; then
    # Let the Tauri bundler sign with the real identity (hardened runtime on
    # by default). We re-sign below anyway so the result is deterministic
    # regardless of bundler version.
    export APPLE_SIGNING_IDENTITY="$IDENTITY"
fi
if [[ "$TARGET" == native ]]; then
    SOURCE_APP="$OUTPUT_DIR/Tether.app"
    PYTHON_BIN="${TETHER_BUILD_PYTHON:-$REPO_ROOT/.venv/bin/python}"
    if [[ -z "${TETHER_BUILD_PYTHON:-}" && ! -x "$PYTHON_BIN" ]]; then PYTHON_BIN=python3; fi
    if [[ "$FETCH" == 1 ]]; then
        "$PYTHON_BIN" "$REPO_ROOT/scripts/build_macos_app.py" --fetch
    else
        "$PYTHON_BIN" "$REPO_ROOT/scripts/build_macos_app.py"
    fi
else
    SOURCE_APP="$DESKTOP_DIR/src-tauri/target/release/bundle/macos/Tether.app"
    (cd "$DESKTOP_DIR" && npm run desktop:build:macos)
fi
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$SOURCE_APP/Contents/Info.plist")"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid app version for DMG filename: $VERSION" >&2
    exit 1
fi
OUTPUT_DMG="$OUTPUT_DIR/Tether-$VERSION.dmg"
mkdir -p "$OUTPUT_DIR"
WORK_DIR="$(mktemp -d "$OUTPUT_DIR/.tether-dmg.XXXXXX")"
STAGING_DIR="$WORK_DIR/staging"
MOUNT_DIR="$WORK_DIR/mounted"
BUILD_DMG="$WORK_DIR/Tether-$VERSION.dmg"

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

if [[ "$NOTARIZE" == "1" ]]; then
    APP_ZIP="$WORK_DIR/Tether-$VERSION-notarize.zip"
    ditto -c -k --keepParent "$SOURCE_APP" "$APP_ZIP"
    notarize_path "$APP_ZIP"
    rm -f "$APP_ZIP"
    xcrun stapler staple "$SOURCE_APP"
fi

# ---------------------------------------------------------------------------
# Package the DMG.
# ---------------------------------------------------------------------------
log "Packaging DMG"
mkdir -p "$STAGING_DIR"
ditto "$SOURCE_APP" "$STAGING_DIR/Tether.app"
ln -s /Applications "$STAGING_DIR/Applications"

hdiutil create \
    -volname "Tether" \
    -srcfolder "$STAGING_DIR" \
    -ov \
    -format UDZO \
    "$BUILD_DMG"

if [[ -n "$IDENTITY" ]]; then
    codesign --force --timestamp --sign "$IDENTITY" "$BUILD_DMG"
    codesign --verify --verbose=2 "$BUILD_DMG"
fi

if [[ "$NOTARIZE" == "1" ]]; then
    notarize_path "$BUILD_DMG"
    xcrun stapler staple "$BUILD_DMG"
    log "Gatekeeper assessment"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$BUILD_DMG"
    spctl --assess --type execute --verbose=2 "$SOURCE_APP"
fi

log "Verifying disk image and packaged app"
hdiutil verify "$BUILD_DMG"
mkdir -p "$MOUNT_DIR"
hdiutil attach -readonly -nobrowse -mountpoint "$MOUNT_DIR" "$BUILD_DMG"
MOUNTED=1
PACKAGED_APP="$MOUNT_DIR/Tether.app"
codesign --verify --deep --strict --verbose=2 "$PACKAGED_APP"
test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$PACKAGED_APP/Contents/Info.plist")" = "$VERSION"
test "$(readlink "$MOUNT_DIR/Applications")" = /Applications
EXECUTABLE="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$PACKAGED_APP/Contents/Info.plist")"
cmp "$SOURCE_APP/Contents/MacOS/$EXECUTABLE" "$PACKAGED_APP/Contents/MacOS/$EXECUTABLE"
if [[ "$TARGET" == native ]]; then
    for helper in tether btop; do
        cmp "$SOURCE_APP/Contents/Resources/bin/$helper" "$PACKAGED_APP/Contents/Resources/bin/$helper"
        TETHER_CONFIG_DIR="$WORK_DIR/config" "$PACKAGED_APP/Contents/Resources/bin/$helper" --version
    done
fi
hdiutil detach "$MOUNT_DIR"
MOUNTED=0
(cd "$WORK_DIR" && shasum -a 256 "Tether-$VERSION.dmg" > "Tether-$VERSION.dmg.sha256")
mv -f "$BUILD_DMG" "$OUTPUT_DMG"
mv -f "$WORK_DIR/Tether-$VERSION.dmg.sha256" "$OUTPUT_DMG.sha256"

log "Done"
echo "$OUTPUT_DMG"
echo "$OUTPUT_DMG.sha256"
if [[ "$NOTARIZE" != "1" ]]; then
    echo "(not notarized)"
fi
