"""Application configuration and path resolution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

#: Set by ``run.sh`` to let a from-source launch run alongside another
#: COMMANDER. Deliberately opt-in: two instances share every file under
#: ``settings_dir()``, and their settings writes are last-write-wins.
ALLOW_MULTIPLE_ENV = "COMMANDER_ALLOW_MULTIPLE"

_FALSEY = {"", "0", "false", "no", "off"}

#: True once this process has found another COMMANDER already holding the
#: instance lock and chosen to run anyway.
_SECONDARY_INSTANCE = False


def multiple_instances_allowed(env: Mapping[str, str] | None = None) -> bool:
    """True when this launch may run alongside another COMMANDER.

    Only ``run.sh`` opts in, so the AppImage and the AUR package keep the
    single-instance rule they have always had. The AppImage is excluded
    explicitly rather than merely not opting in: its ``AppRun`` deliberately
    avoids ``-E`` so ``PYTHONPATH`` survives, which means it inherits the
    whole environment - and a variable exported in the shell that launched it
    would otherwise unlock a shipped build the user never meant to unlock.

    ``APPIMAGE`` is the right discriminator, not ``APPDIR``: the AppImage
    runtime sets ``APPIMAGE`` only for a real .AppImage, while ``APPDIR`` is
    also present when an AppDir is run extracted. This matches how
    ``self_update.commander_appimage_path()`` and ``autostart`` already
    decide the same question.
    """
    environ = os.environ if env is None else env
    if environ.get("APPIMAGE"):
        return False
    value = environ.get(ALLOW_MULTIPLE_ENV)
    if value is None:
        return False
    return value.strip().lower() not in _FALSEY


def mark_secondary_instance() -> None:
    """Record that another COMMANDER already holds the instance lock."""
    global _SECONDARY_INSTANCE

    _SECONDARY_INSTANCE = True


def is_secondary_instance() -> bool:
    """True when this window is an extra instance, not the first one.

    The windows use this to say so in their title bar: with several
    COMMANDERs open on the same settings, knowing which one is the spare is
    the difference between an intentional second window and a confusing one.
    """
    return _SECONDARY_INSTANCE


def project_root() -> Path:
    """Return the project root (parent of the package directory)."""
    return Path(__file__).resolve().parent.parent


def cli_binary_path() -> Path:
    """Locate the bundled stalker-gamma CLI binary.

    Resolution order:
      1. STALKER_GAMMA_CLI environment variable
      2. bundled: <project>/cli/usr/bin/stalker-gamma
      3. system PATH
    """
    env = os.environ.get("STALKER_GAMMA_CLI")
    if env:
        return Path(env).expanduser()

    candidates = [
        project_root() / "cli" / "usr" / "bin" / "stalker-gamma",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    from shutil import which

    found = which("stalker-gamma")
    if found:
        return Path(found)

    return candidates[0]


def settings_dir() -> Path:
    """Directory where the CLI stores its settings (~/.config/stalker-gamma)."""
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


#: Variables the AppImage's AppRun sets for COMMANDER's *own* interpreter
#: (see build-appimage.sh). They point into the AppImage's temporary
#: /tmp/.mount_* directory, which disappears when COMMANDER exits.
_APPIMAGE_ONLY_VARS = ("APPDIR", "APPIMAGE", "ARGV0", "OWD", "PYTHONNOUSERSITE")
#: Set by AppRun too, but only stripped when they really point into the
#: mount - a user's own PYTHONPATH / SSL_CERT_FILE must survive.
_APPIMAGE_PATH_VARS = ("PYTHONPATH", "PYTHONHOME", "SSL_CERT_FILE", "SSL_CERT_DIR")


def child_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """``base`` (default: this process's environment) minus AppImage internals.

    For every program COMMANDER starts that is not COMMANDER itself: Wine and
    the game, umu-run and protontricks (both Python programs, which would
    otherwise import from COMMANDER's bundled payload), the Steam client it
    restarts. Detached ones outlive COMMANDER, and inherited paths into the
    vanished AppImage mount then break them - an SSL_CERT_FILE that no
    longer exists fails every TLS connection Steam makes.
    Outside an AppImage this returns ``base`` unchanged.
    """
    environ = dict(os.environ if base is None else base)
    appdir = environ.get("APPDIR", "")
    if not appdir or not environ.get("APPIMAGE"):
        return environ
    for key in _APPIMAGE_ONLY_VARS:
        environ.pop(key, None)
    prefix = appdir.rstrip("/") + "/"
    for key in _APPIMAGE_PATH_VARS:
        value = environ.get(key)
        if value and any(part.startswith(prefix) for part in value.split(os.pathsep)):
            environ.pop(key, None)
    return environ
