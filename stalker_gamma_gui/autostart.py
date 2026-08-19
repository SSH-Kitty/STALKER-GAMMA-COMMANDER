"""XDG autostart integration.

Creates or removes a ``.desktop`` file in the user's XDG autostart directory
so the application starts automatically at login.

When running inside an AppImage the ``APPIMAGE`` environment variable provides
the absolute path; otherwise ``sys.executable -m stalker_gamma_gui`` is used.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_DESKTOP_NAME = "stalker-gamma-commander.desktop"


def _autostart_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart"


def _exec_command() -> str | None:
    """Return the Exec= value for the .desktop file, or None if undetermined."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return appimage
    # Source / venv install: invoke the package via the current interpreter.
    python = sys.executable
    if python and Path(python).is_file():
        return f"{python} -m stalker_gamma_gui"
    return None


def autostart_desktop_path() -> Path:
    return _autostart_dir() / _DESKTOP_NAME


def is_autostart_enabled() -> bool:
    return autostart_desktop_path().is_file()


def enable_autostart() -> bool:
    cmd = _exec_command()
    if not cmd:
        return False
    path = autostart_desktop_path()
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=STALKER GAMMA Commander\n"
        "Comment=Install, update and launch the S.T.A.L.K.E.R. Anomaly + GAMMA mod pack\n"
        f"Exec={cmd}\n"
        "Icon=stalker-gamma-commander\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        return False


def disable_autostart() -> bool:
    path = autostart_desktop_path()
    if not path.exists():
        return True
    try:
        path.unlink()
        return True
    except OSError:
        return False
