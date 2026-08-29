"""Persistent record of mods installed by the Commander.

modlist.txt is the only authoritative source of what is in the list, but
GAMMA/MO2 can rewrite it independently of the Commander (reordering, dropping
unknown separators, relocating entries).  To honor the invariant that mods
installed here stay grouped under the ``Extra Mods`` category, we keep a small
sidecar file next to the modlist recording which entries the Commander
installed.  The manager reparents those entries back into ``Extra Mods`` when
the list is loaded.
"""

from __future__ import annotations

import json
from pathlib import Path

from .atomic import write_text

TRACKER_SUFFIX = ".gammagui.mods.json"


def tracker_path(modlist: str | Path) -> Path:
    """Return the path of the tracker sidecar for a modlist file."""
    return Path(f"{modlist}{TRACKER_SUFFIX}")


def read_user_mods(modlist: str | Path) -> list[str]:
    """Return the recorded Commander-installed mod names (deduplicated)."""
    path = tracker_path(modlist)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names = data.get("user_mods") if isinstance(data, dict) else data
        if isinstance(names, list):
            return [name for name in names if isinstance(name, str)]
    except (OSError, ValueError):
        return []
    return []


def write_user_mods(modlist: str | Path, names: list[str]) -> None:
    """Persist the set of Commander-installed mod names."""
    ordered = list(dict.fromkeys(name for name in names if isinstance(name, str)))
    payload = json.dumps({"user_mods": ordered}, indent=2)
    write_text(tracker_path(modlist), payload + "\n")


def add_user_mod(modlist: str | Path, name: str) -> None:
    """Record ``name`` as Commander-installed (idempotent)."""
    names = read_user_mods(modlist)
    if name in names:
        return
    write_user_mods(modlist, names + [name])


def remove_user_mods(modlist: str | Path, names: list[str]) -> None:
    """Drop ``names`` from the tracker (missing names are ignored)."""
    dropped = set(names)
    current = read_user_mods(modlist)
    remaining = [name for name in current if name not in dropped]
    if remaining == current:
        return
    write_user_mods(modlist, remaining)


def rename_user_mod(modlist: str | Path, old_name: str, new_name: str) -> None:
    """Update a tracked name after an in-app display rename."""
    names = read_user_mods(modlist)
    changed = False
    for index, name in enumerate(names):
        if name == old_name:
            names[index] = new_name
            changed = True
    if changed:
        write_user_mods(modlist, names)