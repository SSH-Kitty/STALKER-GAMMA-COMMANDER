#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
REQUIREMENTS_HASH_FILE="$VENV_DIR/.requirements.sha256"
PYTHON_BIN="${PYTHON:-python3}"

# Prevent two launchers from replacing or installing into the same environment.
exec 9>"$SCRIPT_DIR/.venv-setup.lock"
flock -x 9

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python 3.10 or newer is required (set PYTHON=/path/to/python3.10+ to override)." >&2
    exit 1
fi

requested_version="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
venv_version=""
if [ -x "$VENV_DIR/bin/python" ]; then
    venv_version="$("$VENV_DIR/bin/python" -c \
        'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || true)"
fi
if [ ! -x "$VENV_DIR/bin/python" ] || [ "$venv_version" != "$requested_version" ]; then
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        echo "Creating virtual environment..."
        "$PYTHON_BIN" -m venv "$VENV_DIR"
        "$VENV_DIR/bin/pip" install --upgrade pip
    else
        echo "Warning: existing virtual environment uses Python $venv_version; requested $requested_version. Using the existing environment." >&2
    fi
fi

requirements_hash="$(sha256sum "$REQUIREMENTS" | awk '{print $1}')"
saved_hash=""
if [ -f "$REQUIREMENTS_HASH_FILE" ]; then
    read -r saved_hash < "$REQUIREMENTS_HASH_FILE" || true
fi
if [ "$requirements_hash" != "$saved_hash" ]; then
    echo "Installing or updating Python dependencies..."
    "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS"
    printf '%s\n' "$requirements_hash" > "$REQUIREMENTS_HASH_FILE"
fi

cd "$SCRIPT_DIR"
exec "$VENV_DIR/bin/python" -m commander_gui "$@"
