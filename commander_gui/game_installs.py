"""Find GAMMA installs on disk, and fix one that was copied from Windows.

An install is three folders: Anomaly, GAMMA (the Mod Organizer instance)
and the cache. COMMANDER's own layout puts all three in one folder;
GAMMA's Windows launcher keeps its archives in ``gamma/downloads``.

A copy from Windows still works once ModOrganizer.ini's drive paths
(``C:\\Games\\Anomaly``) point at the folders through Wine's ``Z:`` drive.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .atomic import write_text
from .integrity import GAMMA_MARKERS

#: A Windows drive path in ModOrganizer.ini. Z: is Wine's view of /, so it
#: already works on Linux.
_DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Ya-y]:(?:\\\\|\\|/)[^\r\n\"]*")
WINDOWS_BACKUP_SUFFIX = ".windows.bak"


def is_anomaly(folder: Path) -> bool:
    return any((folder / "bin").glob("Anomaly*.exe"))


def is_gamma(folder: Path) -> bool:
    return all((folder / name).is_file() for name in GAMMA_MARKERS)


def find_install(root: str | Path) -> tuple[str, str, str] | None:
    """(anomaly, gamma, cache) found in *root* or one level below it.

    *root* may also be the Anomaly or GAMMA folder itself; the other one is
    then looked for beside it.
    """
    # Resolved, so a symlinked /home (Fedora Atomic, Bazzite) is stored the
    # same way as the folders the rest of COMMANDER saves.
    root = Path(root).expanduser().resolve()
    try:
        if is_anomaly(root) or is_gamma(root):
            root = root.parent
        folders = [root, *sorted(p for p in root.iterdir() if p.is_dir())] if root.is_dir() else []
    except OSError:  # unreadable folder or a stale mount
        return None
    anomaly = next((p for p in folders if is_anomaly(p)), None)
    gamma = next((p for p in folders if is_gamma(p)), None)
    if anomaly is None or gamma is None:
        return None
    cache = next(
        (p for p in (root / "cache", gamma / "downloads") if p.is_dir()),
        root / "cache",
    )
    return str(anomaly), str(gamma), str(cache)


def mo2_profile_for(gamma: str | Path, wanted: str) -> str:
    """*wanted* if *gamma* has that Mod Organizer profile, else GAMMA's
    stock one, else the first there is. Another install may not have the
    profile a COMMANDER profile was using."""
    profiles = Path(gamma) / "profiles"
    names = sorted(p.name for p in profiles.iterdir() if p.is_dir()) if profiles.is_dir() else []
    for name in (wanted, "G.A.M.M.A"):
        if name in names:
            return name
    return names[0] if names else wanted


def _plain(path: str) -> str:
    """A Windows path with ``/`` separators and no trailing one."""
    return re.sub(r"[\\/]+", "/", path).rstrip("/")


def _drive_paths(text: str) -> list[str]:
    """Windows drive paths in *text*, ``/``-separated, without the closing
    paren of an ``@ByteArray(...)`` value (a folder's own ``(1)`` stays)."""
    found = []
    for match in _DRIVE_PATH.finditer(text):
        path = match.group()
        if text[: match.start()].endswith("@ByteArray(") and path.endswith(")"):
            path = path[:-1]
        found.append(_plain(path))
    return list(dict.fromkeys(found))


def windows_paths(gamma: str | Path) -> list[str]:
    """Windows drive paths left in ModOrganizer.ini, ``/``-separated."""
    ini = Path(gamma) / "ModOrganizer.ini"
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _drive_paths(text)


def _old_root(path: str, new_root: Path) -> str | None:
    """The Windows folder *path* was under, if the rest of it exists in
    *new_root* (``C:/GAMMA/downloads`` with ``new/downloads`` -> ``C:/GAMMA``)."""
    parts = path.split("/")
    # The folder itself, under the same name ("C:/Games/GAMMA (copy)").
    if len(parts) > 1 and parts[-1].lower() == new_root.name.lower():
        return path
    for cut in range(1, len(parts)):
        if new_root.joinpath(*parts[cut:]).exists():
            return "/".join(parts[:cut])
    return None


def _game_path(text: str) -> str | None:
    match = re.search(r"^gamePath=(.*?)\s*$", text, re.MULTILINE)
    if not match:
        return None
    paths = _drive_paths(match.group(1))
    return paths[0] if paths else None


def _windows_roots(text: str, anomaly: Path, gamma: Path) -> dict[str, Path]:
    """Lower-cased Windows folders in *text* mapped to the Linux folder that
    holds the same files."""
    roots: dict[str, Path] = {}
    if old := _game_path(text):
        roots[old.lower()] = anomaly
    # Longest first: a bare "C:/GAMMA" only maps once a longer path found it.
    for path in sorted(_drive_paths(text), key=len, reverse=True):
        if any(path.lower().startswith(old + "/") or path.lower() == old for old in roots):
            continue
        for new_root in (anomaly, gamma):
            if (old := _old_root(path, new_root)) is not None:
                roots[old.lower()] = new_root
                break
    return roots


def needs_fix(anomaly: str | Path, gamma: str | Path) -> bool:
    """Whether ModOrganizer.ini still has Windows paths COMMANDER can point
    at *anomaly* or *gamma*. Paths on other drives don't count: they are
    left alone, so asking again on every switch would never end."""
    try:
        text = (Path(gamma) / "ModOrganizer.ini").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_windows_roots(text, Path(anomaly), Path(gamma)))


def fix_windows_paths(anomaly: str | Path, gamma: str | Path) -> list[str]:
    """Point ModOrganizer.ini's Windows paths at *anomaly* and *gamma*.

    Keeps the original as ``ModOrganizer.ini.windows.bak``. Returns the
    Windows paths that are outside both folders and so were left alone.
    """
    anomaly, gamma = Path(anomaly), Path(gamma)
    ini = gamma / "ModOrganizer.ini"
    text = ini.read_text(encoding="utf-8", errors="replace")
    roots = _windows_roots(text, anomaly, gamma)
    updated = text
    # Longest first, so "C:/G/anomaly-2" is not rewritten as "C:/G/anomaly".
    for old, new_root in sorted(roots.items(), key=lambda item: len(item[0]), reverse=True):
        new = "Z:" + str(new_root)
        for sep in ("\\\\", "\\", "/"):
            pattern = re.escape(old.replace("/", sep)) + r"(?=[\\/\")]|$)"
            updated = re.sub(
                pattern, lambda _m, sep=sep, new=new: new.replace("/", sep), updated,
                flags=re.IGNORECASE | re.MULTILINE,
            )
    if updated != text:
        backup = ini.with_name(ini.name + WINDOWS_BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(ini, backup)
        write_text(ini, updated)
    return windows_paths(gamma)
