"""Find GAMMA installs on disk, and fix one that was copied from Windows.

An install is three folders: Anomaly, GAMMA (the Mod Organizer instance)
and the cache. COMMANDER's own layout puts all three in one folder;
GAMMA's Windows launcher keeps its archives in ``gamma/downloads``.

A copy from Windows still works once ModOrganizer.ini's drive paths
(``C:\\Games\\Anomaly``) point at the folders through Wine's ``Z:`` drive.
"""

from __future__ import annotations

import os
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


def _replace_roots(text: str, roots: dict[str, Path]) -> str:
    """*text* with each old folder in *roots* (``/``-separated, any case)
    swapped for ``Z:`` plus its new Linux folder, in the separator it had."""
    # Longest first, so "C:/G/anomaly-2" is not rewritten as "C:/G/anomaly".
    for old, new_root in sorted(roots.items(), key=lambda item: len(item[0]), reverse=True):
        new = "Z:" + str(new_root)
        for sep in ("\\\\", "\\", "/"):
            pattern = re.escape(old.replace("/", sep)) + r"(?=[\\/\")]|$)"
            text = re.sub(
                pattern, lambda _m, sep=sep, new=new: new.replace("/", sep), text,
                flags=re.IGNORECASE | re.MULTILINE,
            )
    return text


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
    updated = _replace_roots(text, _windows_roots(text, anomaly, gamma))
    if updated != text:
        backup = ini.with_name(ini.name + WINDOWS_BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(ini, backup)
        write_text(ini, updated)
    return windows_paths(gamma)


#: Filesystems a Windows drive is mounted with. ntfs-3g shows as fuseblk.
_WINDOWS_FILESYSTEMS = {"ntfs", "ntfs3", "fuseblk", "fuse.ntfs-3g", "exfat", "vfat", "msdos"}


def _mount_types(mountinfo: str) -> list[tuple[str, str]]:
    """``(mount point, filesystem)`` pairs from /proc/self/mountinfo."""
    try:
        lines = Path(mountinfo).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    mounts = []
    for line in lines:
        fields, _sep, rest = line.partition(" - ")
        fields, rest = fields.split(), rest.split()
        if len(fields) < 5 or not rest:
            continue
        # Spaces and other odd characters come octal-escaped ("\040").
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
        mounts.append((point, rest[0]))
    return mounts


def windows_drive_folder(*paths: str, mountinfo: str = "/proc/self/mountinfo") -> str | None:
    """First of *paths* that sits on a Windows drive (NTFS, exFAT, FAT),
    else None. A folder that doesn't exist yet counts by the drive it would
    be made on. None too when the mounts can't be read: never blocks."""
    mounts = _mount_types(mountinfo)
    for path in paths:
        if not path:
            continue
        full = str(Path(path).expanduser().resolve())
        # The deepest mount holding the folder is the one it is on.
        holding = [(point, kind) for point, kind in mounts if point == "/" or full == point or full.startswith(point + "/")]
        if holding and max(holding, key=lambda m: len(m[0]))[1] in _WINDOWS_FILESYSTEMS:
            return path
    return None


class CopyCancelled(Exception):
    """The copy was stopped; nothing of it is left behind."""


def _copy_tree(src: Path, dst: Path, copied: list[int], on_file) -> None:
    """Copy *src* into *dst* (made here) file by file. Contents only, not
    the Windows drive's mount permissions, so everything stays writable."""
    for folder, dirs, files in os.walk(src):
        rel = Path(folder).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in [*files, *(d for d in dirs if (Path(folder) / d).is_symlink())]:
            source, target = Path(folder) / name, dst / rel / name
            if source.is_symlink():
                os.symlink(os.readlink(source), target)
                continue
            shutil.copyfile(source, target)
            copied[0] += source.stat().st_size
            on_file()


def copy_install(install, target: str | Path, total: int, report, cancelled) -> tuple[str, str, str]:
    """Copy *install*'s Anomaly and GAMMA folders into *target*; returns the
    copy's ``(anomaly, gamma, cache)``.

    *report(done, total)* gets the bytes copied so far, *cancelled()* stops
    it. A separate cache folder is left out: it downloads again. A partial
    copy is deleted again on cancel or error. ModOrganizer.ini's Linux
    paths to the old folders are pointed at the copy; Windows paths are
    left for :func:`fix_windows_paths`.
    """
    anomaly, gamma, cache = (Path(p) for p in install)
    target = Path(target).expanduser().resolve()
    new_anomaly, new_gamma = target / anomaly.name, target / gamma.name
    for folder in (new_anomaly, new_gamma):
        if folder.exists():
            raise FileExistsError(folder)
    copied = [0]
    last = [-1]

    def on_file() -> None:
        if cancelled():
            raise CopyCancelled
        # One update per MB at most: GAMMA has 100k+ small files.
        if copied[0] >> 20 != last[0]:
            last[0] = copied[0] >> 20
            report(copied[0], total)

    try:
        _copy_tree(anomaly, new_anomaly, copied, on_file)
        _copy_tree(gamma, new_gamma, copied, on_file)
    except BaseException:
        for folder in (new_anomaly, new_gamma):
            shutil.rmtree(folder, ignore_errors=True)
        raise
    ini = new_gamma / "ModOrganizer.ini"
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = None
    if text is not None:
        updated = _replace_roots(text, {"z:" + str(gamma).lower(): new_gamma, "z:" + str(anomaly).lower(): new_anomaly})
        if updated != text:
            write_text(ini, updated)
    new_cache = new_gamma / cache.relative_to(gamma) if cache.is_relative_to(gamma) else target / "cache"
    return str(new_anomaly), str(new_gamma), str(new_cache)
