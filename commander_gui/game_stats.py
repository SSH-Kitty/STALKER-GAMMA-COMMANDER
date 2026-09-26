"""Read the player's game statistics out of an Anomaly save.

Anomaly keeps script state beside each save in a ``<name>.scoc`` file - the
marshalled ``alife_storage_manager`` data, uncompressed. ``game_statistics``
stores the PDA's statistics there as ``actor_statistics``: a flat table of
counters (``deaths``, ``killed_monsters``, ``killed_stalkers``,
``artefacts_found``, ...).

The same file holds ``actor_achievements`` - each achievement key with an
unlocked flag - read the same way.

Only the part of the marshal format those tables use is decoded here::

    0x04 <u32 length> <bytes>   string (every key)
    0x03 <f64>                  number
    0x01 <u8>                   boolean
    0x05 <u8> <u32>             table header

Values are read in order until anything else turns up (the nested
``actor_articles`` table ends the flat counters). Everything is read-only
and best-effort: a missing, truncated or unfamiliar save gives ``None``,
never an exception.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .game_backup import saves_dir

#: .scoc files run to a few hundred KB; anything far bigger is not one.
_MAX_BYTES = 32 * 1024 * 1024
#: No real key comes close; a larger length means we lost our place.
_MAX_KEY_LEN = 256


@dataclass(frozen=True)
class SaveStats:
    save_name: str
    mtime: float
    stats: dict[str, float]
    #: Achievement key -> unlocked, from ``actor_achievements``.
    achievements: dict[str, bool] = field(default_factory=dict)

    def get(self, key: str) -> int:
        return int(self.stats.get(key, 0) or 0)


#: The counters both UIs show, in display order: ``actor_statistics`` keys
#: and their captions (``total_kills`` is monsters + stalkers).
STAT_FIELDS: tuple[tuple[str, str], ...] = (
    ("total_kills", "Total kills"),
    ("killed_monsters", "Mutants killed"),
    ("killed_stalkers", "Stalkers killed"),
    ("boxes_smashed", "Boxes smashed"),
    ("artefacts_found", "Artefacts found"),
    ("stashes_found", "Stashes found"),
    ("tasks_completed", "Tasks completed"),
    ("emissions", "Emissions survived"),
    ("items_crafted", "Items crafted"),
)


def stat_value(save: SaveStats, key: str) -> int:
    """One of :data:`STAT_FIELDS`' counters from ``save``."""
    if key == "total_kills":
        return save.get("killed_monsters") + save.get("killed_stalkers")
    return save.get(key)


def _key_record(key: str) -> bytes:
    """``key`` as the marshal writer stores it: string tag, length, bytes."""
    raw = key.encode("utf-8")
    return b"\x04" + struct.pack("<I", len(raw)) + raw


def _read_table(data: bytes, key: str) -> dict[str, float] | None:
    """The flat table stored under ``key``: string keys, number/bool values.

    Reading stops at the first value of any other kind (a nested table).
    None when the key is missing or not followed by a table.
    """
    start = data.find(_key_record(key))
    if start < 0:
        return None
    i = start + len(_key_record(key))
    # 0x05 <u8> <u32>: the table header.
    if i + 6 > len(data) or data[i] != 0x05:
        return None
    i += 6
    table: dict[str, float] = {}
    try:
        while i < len(data) and data[i] == 0x04:
            (length,) = struct.unpack_from("<I", data, i + 1)
            if length > _MAX_KEY_LEN:
                break
            name = data[i + 5 : i + 5 + length].decode("utf-8", "replace")
            i += 5 + length
            kind = data[i]
            if kind == 0x03:
                (value,) = struct.unpack_from("<d", data, i + 1)
                i += 9
            elif kind == 0x01:
                value = float(data[i + 1])
                i += 2
            else:
                break
            table[name] = value
    except (struct.error, IndexError):
        pass
    return table


def _read_save(path: str | Path) -> bytes | None:
    try:
        path = Path(path)
        if path.stat().st_size > _MAX_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def read_actor_statistics(path: str | Path) -> dict[str, float] | None:
    """The ``actor_statistics`` counters from one ``.scoc`` file."""
    data = _read_save(path)
    if data is None:
        return None
    return _read_table(data, "actor_statistics") or None


def save_folders(
    anomaly: str | None, gamma: str | None = None, mo2_profile: str | None = None
) -> list[Path]:
    """Every folder an Anomaly save for this profile can end up in.

    * ``<anomaly>/appdata/savedgames`` - the normal place;
    * ``<gamma>/profiles/<mo2 profile>/saves`` - MO2's per-profile
      "local saves" (``LocalSaves=true`` in the profile's settings.ini);
    * ``<gamma>/overwrite/appdata/savedgames`` - where MO2's virtual file
      system puts a save it created itself rather than in the real folder.
    """
    folders: list[Path] = []
    if anomaly:
        folders.append(saves_dir(anomaly))
    if gamma:
        root = Path(gamma).expanduser()
        if mo2_profile:
            folders.append(root / "profiles" / mo2_profile / "saves")
        folders.append(root / "overwrite" / "appdata" / "savedgames")
    return folders


def latest_save_stats(
    anomaly: str | None, gamma: str | None = None, mo2_profile: str | None = None
) -> SaveStats | None:
    """Statistics from the most recently written save, or None."""
    saves: list[tuple[float, Path]] = []
    for folder in save_folders(anomaly, gamma, mo2_profile):
        try:
            for save in folder.glob("*.scoc"):
                saves.append((save.stat().st_mtime, save))
        except OSError:
            continue
    saves.sort(key=lambda item: item[0], reverse=True)
    # Newest first; skip a save whose file is unreadable or unfamiliar.
    for mtime, save in saves[:5]:
        data = _read_save(save)
        stats = _read_table(data, "actor_statistics") if data is not None else None
        if stats:
            achievements = _read_table(data, "actor_achievements") or {}
            return SaveStats(
                save.stem,
                mtime,
                stats,
                {key: bool(value) for key, value in achievements.items()},
            )
    return None
