#!/bin/sh
# Install the standalone Tether CLI. No Python, pip, sudo, or clone required.
set -eu

install_dir=${TETHER_INSTALL_DIR:-"$HOME/.local/bin"}
version=${TETHER_VERSION:-latest}
local_binary=
modify_path=true
while [ "$#" -gt 0 ]; do
    case "$1" in
        --binary) [ "$#" -ge 2 ] || { echo '--binary requires a path' >&2; exit 1; }; local_binary=$2; shift 2 ;;
        --no-modify-path) modify_path=false; shift ;;
        --help|-h)
            echo 'Usage: sh install.sh [--binary ./dist/tether] [--no-modify-path]'
            echo 'TETHER_INSTALL_DIR sets the destination (default: ~/.local/bin).'
            echo 'TETHER_VERSION selects a release tag (default: latest).'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

case "$install_dir" in
    /*) ;;
    *) echo 'TETHER_INSTALL_DIR must be an absolute path.' >&2; exit 1 ;;
esac

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/tether-install.XXXXXX")
trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM

if [ -n "$local_binary" ]; then
    [ -f "$local_binary" ] || { echo "Binary not found: $local_binary" >&2; exit 1; }
    cp "$local_binary" "$tmp_dir/tether"
else
    case "$(uname -s)" in
        Darwin) os=darwin ;;
        Linux) os=linux ;;
        *) echo 'This installer supports macOS and Linux.' >&2; exit 1 ;;
    esac
    case "$(uname -m)" in
        arm64|aarch64) arch=arm64 ;;
        x86_64|amd64) arch=x86_64 ;;
        *) echo 'Supported architectures: arm64 and x86_64.' >&2; exit 1 ;;
    esac
    command -v curl >/dev/null 2>&1 || { echo 'curl is required.' >&2; exit 1; }
    asset="tether-$os-$arch.tar.gz"
    base='https://github.com/agentrebench/Tether/releases'
    if [ "$version" = latest ]; then
        base="$base/latest/download"
    else
        case "$version" in *[!a-zA-Z0-9._-]*) echo 'Invalid release tag.' >&2; exit 1 ;; esac
        base="$base/download/$version"
    fi
    echo "Downloading Tether ($os/$arch)…"
    curl -fLSs --retry 3 "$base/$asset" -o "$tmp_dir/$asset" || {
        echo 'No binary available for this release. Build from source with scripts/build_binary.py.' >&2
        exit 1
    }
    curl -fLSs --retry 3 "$base/$asset.sha256" -o "$tmp_dir/$asset.sha256"
    if command -v sha256sum >/dev/null 2>&1; then
        (cd "$tmp_dir" && sha256sum -c "$asset.sha256")
    elif command -v shasum >/dev/null 2>&1; then
        (cd "$tmp_dir" && shasum -a 256 -c "$asset.sha256")
    else
        echo 'A SHA-256 verifier (sha256sum or shasum) is required.' >&2
        exit 1
    fi
    tar -xzf "$tmp_dir/$asset" -C "$tmp_dir" tether
fi

chmod 755 "$tmp_dir/tether"
"$tmp_dir/tether" --version
mkdir -p "$install_dir"
# Rename a fresh file rather than following an existing pipx/venv symlink.
staged=$(mktemp "$install_dir/.tether.XXXXXX")
trap 'rm -rf "$tmp_dir"; rm -f "$staged"' EXIT HUP INT TERM
cp "$tmp_dir/tether" "$staged"
chmod 755 "$staged"
mv -f "$staged" "$install_dir/tether"
echo "Installed $install_dir/tether"

case ":$PATH:" in
    *":$install_dir:"*) echo 'Run tether from any project directory.' ;;
    *)
        # Single-quote the path for shell startup files, including spaces/quotes.
        quoted_dir=$(printf '%s' "$install_dir" | sed "s/'/'\\\\''/g")
        path_line="export PATH='$quoted_dir':\"\$PATH\""
        rc=
        case "${SHELL:-}" in
            */zsh) rc="${ZDOTDIR:-$HOME}/.zshrc" ;;
            */bash) rc="$HOME/.bashrc" ;;
        esac
        if [ "$modify_path" = true ] && [ -n "$rc" ]; then
            if ! grep -Fqx "$path_line" "$rc" 2>/dev/null; then
                printf '\n# Tether CLI\n%s\n' "$path_line" >> "$rc"
            fi
            # Login bash shells use .bash_profile; non-login shells use .bashrc.
            if [ "${SHELL##*/}" = bash ]; then
                if ! grep -Fqx "$path_line" "$HOME/.bash_profile" 2>/dev/null; then
                    printf '\n# Tether CLI\n%s\n' "$path_line" >> "$HOME/.bash_profile"
                fi
            fi
            echo "Added Tether to $rc. Open a new terminal, then type tether."
        else
            echo 'Add the install directory to your shell PATH, then type tether.'
        fi
        printf 'For this terminal: %s\n' "$path_line"
        ;;
esac
