"""Application configuration and path resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def project_root() -> Path:
    """Return the project root (parent of the package directory)."""
    return Path(__file__).resolve().parent.parent


def cli_binary_path() -> Path:
    """Locate the bundled stalker-gamma CLI binary.

    Resolution order:
      1. STALKER_GAMMA_CLI environment variable
      2. bundled: <project>/cli/usr/bin/stalker-gamma
      3. cli/usr/bin/stalker-gamma relative to the executable dir (frozen apps)
      4. system PATH
    """
    env = os.environ.get("STALKER_GAMMA_CLI")
    if env:
        return Path(env)

    candidates = [
        project_root() / "cli" / "usr" / "bin" / "stalker-gamma",
    ]
    if getattr(sys, "frozen", False):
        candidates.insert(
            0, Path(sys.executable).resolve().parent / "cli" / "usr" / "bin" / "stalker-gamma"
        )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    from shutil import which

    found = which("stalker-gamma")
    if found:
        return Path(found)

    return candidates[0]


def settings_dir() -> Path:
    """Directory where the CLI stores its settings (%APPDATA%/stalker-gamma)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base:
        base = os.path.join(Path.home(), ".config")
    return Path(base) / "stalker-gamma"


def settings_path() -> Path:
    return settings_dir() / "settings.json"


def gui_settings_path() -> Path:
    return settings_dir() / "gui-settings.json"


def logs_dir() -> Path:
    return settings_dir() / "logs"
