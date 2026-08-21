"""XDG autostart integration.

Creates or removes a ``.desktop`` file in the user's XDG autostart directory
so the application starts automatically at login.

When running inside an AppImage the ``APPIMAGE`` environment variable provides
the absolute path; otherwise ``sys.executable -m commander_gui`` is used.
"""

from __future__ import annotations

import os
import shlex
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
    # Frozen build (PyInstaller, etc.): use the executable directly.
    if getattr(sys, "frozen", False):
        return sys.executable
    # Source / venv install: invoke the package via the current interpreter.
    python = sys.executable
    if python and Path(python).is_file():
        return f"{python} -m commander_gui"
    return None


def _project_root() -> Path | None:
    """Return the project root for source installs, or None."""
    if os.environ.get("APPIMAGE") or getattr(sys, "frozen", False):
        return None
    return Path(__file__).resolve().parent.parent


def _desktop_exec(command: str) -> str:
    """Quote executable arguments using the desktop-entry syntax."""
    parts = shlex.split(command)
    escaped: list[str] = []
    for part in parts:
        escaped_part = part.replace("%", "%%")
        if any(char.isspace() for char in escaped_part) or any(
            char in escaped_part for char in '"\\'
        ):
            escaped_part = escaped_part.replace("\\", "\\\\").replace('"', '\\"')
            escaped.append(f'"{escaped_part}"')
        else:
            escaped.append(escaped_part)
    return " ".join(escaped)


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
        "Comment=Install, update and launch the STALKER Anomaly + GAMMA Modpack\n"
        f"Exec={_desktop_exec(cmd)}\n"
        "Icon=stalker-gamma-commander\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    root = _project_root()
    if root is not None:
        content += f"Path={root}\n"
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
