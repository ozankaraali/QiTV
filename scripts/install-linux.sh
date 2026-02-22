#!/bin/bash
# QiTV Linux installer
# Creates a desktop shortcut and installs the icon so QiTV appears
# in your application menu and survives reboots.
#
# Usage:
#   For release binary:  ./scripts/install-linux.sh /path/to/qitv-linux
#   For dev (venv) mode: ./scripts/install-linux.sh
#
# The script installs to ~/.local (no root required).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

ICON_SRC="$REPO_DIR/assets/qitv.png"
DESKTOP_SRC="$REPO_DIR/assets/qitv.desktop"

ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
DESKTOP_DIR="$HOME/.local/share/applications"
BIN_DIR="$HOME/.local/bin"

# --- helpers ---------------------------------------------------------------
die() { echo "Error: $*" >&2; exit 1; }

install_icon() {
    mkdir -p "$ICON_DIR"
    cp "$ICON_SRC" "$ICON_DIR/qitv.png"
    echo "Installed icon to $ICON_DIR/qitv.png"
}

install_desktop_entry() {
    local exec_line="$1"
    mkdir -p "$DESKTOP_DIR"

    # Write desktop file with the correct Exec line
    sed "s|^Exec=.*|Exec=$exec_line|" "$DESKTOP_SRC" > "$DESKTOP_DIR/qitv.desktop"
    chmod +x "$DESKTOP_DIR/qitv.desktop"
    echo "Installed desktop entry to $DESKTOP_DIR/qitv.desktop"

    # Update desktop database if available
    if command -v update-desktop-database &>/dev/null; then
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    fi
}

# --- main ------------------------------------------------------------------
if [ $# -ge 1 ]; then
    # ---- Release binary mode ----
    BINARY="$(realpath "$1")"
    [ -f "$BINARY" ] || die "File not found: $1"
    [ -x "$BINARY" ] || chmod +x "$BINARY"

    # Copy to ~/.local/bin for PATH-based launching
    mkdir -p "$BIN_DIR"
    cp "$BINARY" "$BIN_DIR/qitv-linux"
    chmod +x "$BIN_DIR/qitv-linux"
    echo "Installed binary to $BIN_DIR/qitv-linux"

    install_icon
    install_desktop_entry "$BIN_DIR/qitv-linux"
else
    # ---- Development / venv mode ----
    VENV_PYTHON="$REPO_DIR/venv/bin/python"
    MAIN_PY="$REPO_DIR/main.py"

    [ -f "$VENV_PYTHON" ] || die "Virtual environment not found at $REPO_DIR/venv/. Run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    [ -f "$MAIN_PY" ] || die "main.py not found at $REPO_DIR"

    install_icon
    install_desktop_entry "$VENV_PYTHON $MAIN_PY"
fi

echo ""
echo "QiTV has been installed. You should now find it in your application menu."
echo "If it does not appear immediately, log out and log back in."
echo ""
echo "Tip: make sure ~/.local/bin is in your PATH. Add this to your ~/.bashrc:"
echo '  export PATH="$HOME/.local/bin:$PATH"'
