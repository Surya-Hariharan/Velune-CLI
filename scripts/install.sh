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
# Environment overrides:
#   VELUNE_PACKAGE         what to install (default: velune-cli; may be a version
#                          spec like "velune-cli==0.9.8" or a local wheel path)
#   VELUNE_PYTHON          Python version for the environment (default: 3.12)
#   VELUNE_NO_MODIFY_PATH  set to 1 to leave shell profiles untouched

set -eu

PACKAGE="${VELUNE_PACKAGE:-velune-cli}"
PYTHON_VERSION="${VELUNE_PYTHON:-3.12}"

say() { printf '%s\n' "$*"; }
die() { printf 'velune-install: error: %s\n' "$*" >&2; exit 1; }

find_uv() {
    if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
    for candidate in "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then printf '%s\n' "$candidate"; return; fi
    done
}

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
