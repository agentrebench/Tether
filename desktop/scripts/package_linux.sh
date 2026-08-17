#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "error: Linux packages must be built on Linux" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DESKTOP_DIR/.." && pwd)"
BUNDLE_DIR="$DESKTOP_DIR/src-tauri/target/release/bundle"
OUTPUT_DIR="$REPO_ROOT/dist/linux"

cd "$DESKTOP_DIR"
npm run desktop:build:linux

mkdir -p "$OUTPUT_DIR"
find "$OUTPUT_DIR" -maxdepth 1 -type f \
    \( -name '*.AppImage' -o -name '*.deb' -o -name '*.rpm' -o -name 'SHA256SUMS' \) \
    -delete
find "$BUNDLE_DIR/appimage" "$BUNDLE_DIR/deb" "$BUNDLE_DIR/rpm" \
    -maxdepth 1 -type f \
    \( -name '*.AppImage' -o -name '*.deb' -o -name '*.rpm' \) \
    -exec cp {} "$OUTPUT_DIR/" \;

if ! find "$OUTPUT_DIR" -maxdepth 1 -type f \
    \( -name '*.AppImage' -o -name '*.deb' -o -name '*.rpm' \) \
    -print -quit | grep -q .; then
    echo "error: Tauri produced no Linux packages" >&2
    exit 1
fi

cd "$OUTPUT_DIR"
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum ./*.AppImage ./*.deb ./*.rpm > SHA256SUMS
else
    shasum -a 256 ./*.AppImage ./*.deb ./*.rpm > SHA256SUMS
fi

echo "$OUTPUT_DIR"
