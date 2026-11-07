#!/bin/sh
# Velune CLI installer for macOS / Linux / WSL.
#
#   curl -fsSL https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/scripts/install.sh | sh
#
# Installs Velune into its own isolated environment with uv
# (https://docs.astral.sh/uv/), so it never fights the packages of any other
# tool and works even when the system Python is missing, too old, or
# "externally managed" (PEP 668 — Debian/Ubuntu/Homebrew). uv downloads a
# private Python if needed. Re-run the same command to upgrade.
#
# Before downloading anything the installer checks the operating system and
# CPU architecture, and stops with an explanation on a platform Velune does not
# support (see "Platform support" in the README).
#
# Environment overrides:
#   VELUNE_PACKAGE         what to install (default: velune-cli; may be a version
#                          spec like "velune-cli==0.9.9" or a local wheel path)
#   VELUNE_PYTHON          Python version for the environment (default: 3.12)
#   VELUNE_NO_MODIFY_PATH  set to 1 to leave shell profiles untouched

set -eu

PACKAGE="${VELUNE_PACKAGE:-velune-cli}"
PYTHON_VERSION="${VELUNE_PYTHON:-3.12}"
SUPPORT_URL="https://github.com/Surya-Hariharan/Velune-CLI#platform-support"

say() { printf '%s\n' "$*"; }
die() { printf 'velune-install: error: %s\n' "$*" >&2; exit 1; }

# unsupported_platform_reason OS ARCH
#   OS and ARCH are the values of `uname -s` and `uname -m`.
#   Prints why the platform is unsupported, or nothing when it is supported.
unsupported_platform_reason() {
    _os="$1"
    _arch="$2"
    case "$_os" in
        Linux|Darwin) ;;
        MINGW*|MSYS*|CYGWIN*)
            printf 'this is a Windows shell (%s); use the PowerShell installer instead' "$_os"
            return 0 ;;
        *)
            printf 'unsupported operating system: %s' "$_os"
            return 0 ;;
    esac
    case "$_arch" in
        x86_64|amd64|aarch64|arm64) ;;
        *)
            printf 'unsupported CPU architecture: %s (on %s)' "$_arch" "$_os"
            return 0 ;;
    esac
}

is_musl() {
    for _loader in /lib/ld-musl-*.so.1 /usr/lib/ld-musl-*.so.1; do
        if [ -e "$_loader" ]; then return 0; fi
    done
    return 1
}

# platform_notes OS ARCH
#   Non-fatal heads-up about platforms where the base CLI installs but an
#   optional extra does not. Prints nothing otherwise.
platform_notes() {
    _os="$1"
    _arch="$2"
    if [ "$_os" = "Darwin" ] && { [ "$_arch" = "x86_64" ] || [ "$_arch" = "amd64" ]; }; then
        say "note: Intel Macs are unverified: the cryptography release in our lock file has no macOS x86-64 wheel, so installing the base CLI may need a Rust toolchain. Installation will continue and may work. The optional [rag] extra is unavailable on this platform. See $SUPPORT_URL"
    elif [ "$_os" = "Linux" ] && is_musl; then
        say "note: musl Linux (e.g. Alpine) is not tested. The optional [rag] extra is unavailable on this platform."
    fi
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
    for candidate in "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then printf '%s\n' "$candidate"; return; fi
    done
}

main() {
    OS="$(uname -s 2>/dev/null || printf 'unknown')"
    ARCH="$(uname -m 2>/dev/null || printf 'unknown')"

    # Fail before any download: a known-unsupported platform must never fall
    # through to a long source build that ends in a compiler error.
    REASON="$(unsupported_platform_reason "$OS" "$ARCH")"
    if [ -n "$REASON" ]; then
        die "$REASON.
Velune does not support this platform. Supported platforms and manual
install options: $SUPPORT_URL
Nothing was downloaded or installed."
    fi
    platform_notes "$OS" "$ARCH"

    UV="$(find_uv || true)"
    if [ -z "$UV" ]; then
        say "==> Installing uv (isolated Python package manager)..."
        if command -v curl >/dev/null 2>&1; then
            curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
        else
            die "need curl or wget to download uv"
        fi
        UV="$(find_uv || true)"
        [ -n "$UV" ] || die "uv installation failed; see https://docs.astral.sh/uv/getting-started/installation/"
    fi

    say "==> Installing $PACKAGE (isolated environment, Python $PYTHON_VERSION)..."
    "$UV" tool install --force --upgrade --python "$PYTHON_VERSION" "$PACKAGE" \
        || die "installing $PACKAGE failed (output above)"

    BIN_DIR="$("$UV" tool dir --bin)"
    "$BIN_DIR/velune" --version || die "velune was installed but does not start"

    if [ "${VELUNE_NO_MODIFY_PATH:-0}" != "1" ]; then
        "$UV" tool update-shell >/dev/null 2>&1 || true
    fi

    case ":$PATH:" in
        *":$BIN_DIR:"*)
            say "==> Done. Run: velune" ;;
        *)
            say "==> Done. Open a new terminal (or run: export PATH=\"$BIN_DIR:\$PATH\"), then run: velune" ;;
    esac
}

# Tests source this file to exercise the functions above without installing.
if [ "${VELUNE_INSTALLER_SOURCE_ONLY:-0}" != "1" ]; then
    main
fi
