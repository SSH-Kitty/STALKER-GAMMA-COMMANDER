"""Desktop-environment helpers shared by the Assistant UI."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#: File managers tried (in order) before falling back to xdg-open.
_OPENERS = ("nautilus", "nemo", "thunar", "pcmanfm")


def open_in_file_manager(path: str | Path) -> bool:
    """Open *path* in the user's file manager.

    Tries the KDE/Plasma default (dolphin), then common GTK managers,
    ``xdg-open``, and finally Windows Explorer for WSL setups. Returns
    True when something was launched successfully.
    """
    target = str(Path(path))
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    commands: list[list[str]] = []

    dolphin = shutil.which("dolphin")
    if dolphin and ("kde" in desktop or "plasma" in desktop):
        commands.append([dolphin, "--new-window", target])
    for opener in _OPENERS:
        exe = shutil.which(opener)
        if exe:
            commands.append([exe, target])
    xdg_open = shutil.which("xdg-open")
    if xdg_open:
        commands.append([xdg_open, target])

    explorer = shutil.which("explorer.exe")
    if explorer:
        converted = target.replace("/", "\\")
        wslpath = shutil.which("wslpath")
        if wslpath:
            try:
                result = subprocess.run(
                    [wslpath, "-w", target],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                converted = (
                    result.stdout.strip() if result.returncode == 0 else ""
                )
            except (OSError, subprocess.TimeoutExpired):
                converted = ""
        if converted:
            commands.append([explorer, converted])

    for command in commands:
        try:
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            continue
    return False
