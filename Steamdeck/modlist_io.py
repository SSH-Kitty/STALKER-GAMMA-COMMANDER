"""Writing modlist.txt from Deck Mode, safely.

Reproduces the contract the desktop Mod Manager enforces at its single write
choke point. Two guards matter, and skipping either would lose the user's
work rather than merely inconvenience them:

* **Mod Organizer must not be running.** MO2 keeps its own copy of the load
  order in memory and rewrites modlist.txt when it exits, so any edit made
  while it is open is silently discarded later.
* **No install may be in progress.** The CLI rewrites the same file during
  an install or update.

Everything else here is about being able to undo: a one-off ``.gammagui.bak``
of the pristine file, and one timestamped snapshot per session before the
first change.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from commander_gui.modlist import BACKUP_SUFFIX, save_lines, timestamped_backup_path
from commander_gui.ui.common import mo2_running


class ModlistWriteBlocked(RuntimeError):
    """A guard refused the write. ``str()`` is shown to the user."""


def backup_path(modlist: Path) -> Path:
    return modlist.with_name(modlist.name + BACKUP_SUFFIX)


def guard_reason(window) -> str | None:
    """Why writing is currently refused, or None if it is allowed.

    Re-evaluated on every single write, never cached: Mod Organizer can be
    launched from the Play screen between one toggle and the next.
    """
    from commander_gui.i18n import tr

    if getattr(window, "install_busy", False):
        return tr("An install is already running.")
    if mo2_running(force=True):
        return tr(
            "Close Mod Organizer first - it would overwrite your changes "
            "when it exits."
        )
    return None


def write_lines(
    window,
    path: Path,
    lines: list[str],
    *,
    snapshot: bool = False,
) -> None:
    """Write ``lines`` to ``path``, or raise :class:`ModlistWriteBlocked`.

    ``snapshot`` takes an additional timestamped copy; the Mods screen asks
    for one before its first edit of a session, matching how the desktop
    page treats a batch change rather than an individual toggle.
    """
    reason = guard_reason(window)
    if reason is not None:
        raise ModlistWriteBlocked(reason)
    try:
        if path.exists():
            original = backup_path(path)
            if not original.exists():
                shutil.copy2(path, original)
            if snapshot:
                shutil.copy2(path, timestamped_backup_path(path))
        save_lines(path, lines)
    except OSError as exc:
        raise ModlistWriteBlocked(str(exc)) from exc
