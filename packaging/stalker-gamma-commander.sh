#!/bin/sh
# Launcher for a system install where commander_gui/, assistant/ and cli/
# live as siblings under /opt/stalker-gamma-commander (see config.py's
# project_root(), which resolves relative to wherever commander_gui itself
# physically lives - PYTHONPATH here is what makes that true post-install).
export PYTHONPATH="/opt/stalker-gamma-commander${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m commander_gui "$@"
