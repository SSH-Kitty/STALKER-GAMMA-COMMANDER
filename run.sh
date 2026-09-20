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
    if [ -x "$VENV_DIR/bin/python" ] && [ -n "$venv_version" ]; then
        echo "Recreating virtual environment (Python $venv_version -> $requested_version)..."
        rm -rf "$VENV_DIR"
    else
        echo "Creating virtual environment..."
    fi
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
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

# The venv is ready, so drop the setup lock before handing off. `exec N>file`
# does not set FD_CLOEXEC and a flock lives on the open file description, so
# without this the GUI would inherit fd 9 and hold the lock for as long as it
# runs - blocking the next ./run.sh at the `flock -x 9` above, however the
# app's own instance rule is set.
exec 9>&-

# Opt this launch out of the single-instance rule, so several source builds
# can run side by side while developing (see multiple_instances_allowed() in
# commander_gui/config.py). The shipped AppImage and AUR package never set
# this and are unaffected. An existing value wins, so
# `COMMANDER_ALLOW_MULTIPLE=0 ./run.sh` still behaves like a release build.
export COMMANDER_ALLOW_MULTIPLE="${COMMANDER_ALLOW_MULTIPLE:-1}"

cd "$SCRIPT_DIR"
exec "$VENV_DIR/bin/python" -m commander_gui "$@"
