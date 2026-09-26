#!/bin/sh
# Launcher for a system install where commander_gui/, assistant/ and cli/
# live as siblings under /opt/stalker-gamma-commander (see config.py's
# project_root(), which resolves relative to wherever commander_gui itself
# physically lives - PYTHONPATH here is what makes that true post-install).
export PYTHONPATH="/opt/stalker-gamma-commander${PYTHONPATH:+:$PYTHONPATH}"
# -P: don't put the current directory first on sys.path, so a stray
#     commander_gui/ or shutil.py wherever this was started from can't be
#     imported instead of the real one (the AppImage's AppRun does the same).
# -s: ignore ~/.local site-packages; PySide6 comes from the system package.
exec python3 -s -P -m commander_gui "$@"
