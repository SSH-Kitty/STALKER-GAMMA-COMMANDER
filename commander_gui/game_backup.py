"""Back up and restore saves, user.ltx and MCM settings.

Three things a player cannot get back from a reinstall live inside the
install folders, so a Fresh Reset, GAMMA Reset or Full Uninstall deletes
them along with everything else:

* saves - ``<anomaly>/appdata/savedgames``;
* game options, controls and keybinds - ``<anomaly>/appdata/user.ltx``;
* MCM (Mod Configuration Menu) settings - ``axr_options.ltx``, which MO2
  keeps inside GAMMA's "MCM values" mod (or a renamed copy of it) and, when
  a mod writes one for the first time, in ``<gamma>/overwrite``.

A backup is one zip file under ``$XDG_DATA_HOME/stalker-gamma-commander/
backups/<profile>/`` - outside every install folder, so the wipes above
can never reach it. It holds a ``manifest.json`` plus ``saves/``,
``user.ltx`` and ``mcm/<path inside the GAMMA folder>``.

Restoring never deletes anything: files are copied over the current ones,
and every current file that would be replaced is first packed into a
"before restore" backup, so a restore can itself be undone.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

#: Why a backup was made. ``manual`` backups are never pruned; the others
#: are made automatically and only the newest few are kept.
REASON_LABELS = {
    "manual": "Manual backup",
    "fresh-reset": "Before Fresh Reset",
    "gamma-reset": "Before GAMMA Reset",
    "uninstall": "Before Full Uninstall",
    "restore": "Before restore",
    "update": "Before GAMMA update",
    "repair": "Before Verify & Repair",
    "reinstall": "Before GAMMA reinstall",
}
#: Automatic backups kept per profile *and per reason* (manual ones are
#: never removed). Per reason, so frequent settings-only backups (before
#: every update or repair) can never push out the one backup holding the
#: saves from before a reset.
KEEP_AUTOMATIC = 5
MANIFEST = "manifest.json"
MCM_FILE = "axr_options.ltx"

Report = Callable[[str], None]


class BackupError(Exception):
    """A backup or restore could not be completed; nothing was deleted."""


@dataclass
class BackupInfo:
    path: Path
    profile: str
    created: float
    reason: str
    saves: int = 0
    user_ltx: bool = False
    mcm: list[str] = field(default_factory=list)
    size: int = 0
    #: The player's own name for a manual backup ("" when none was given).
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or REASON_LABELS.get(self.reason, self.reason)

    @property
    def has_settings(self) -> bool:
        return self.user_ltx or bool(self.mcm)


def backups_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
    return Path(base) / "stalker-gamma-commander" / "backups"


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return slug or "profile"


#: Longest name a player can give a backup.
NAME_MAX = 60


def clean_name(name: str) -> str:
    """A backup name as stored: one line, no control characters, trimmed."""
    text = "".join(ch if ch.isprintable() else " " for ch in (name or ""))
    return " ".join(text.split())[:NAME_MAX].strip()


def profile_backup_dir(profile_name: str) -> Path:
    return backups_root() / _slug(profile_name)


# -- collecting ---------------------------------------------------------------
def saves_dir(anomaly: str | Path) -> Path:
    return Path(anomaly).expanduser() / "appdata" / "savedgames"


def user_ltx_path(anomaly: str | Path) -> Path:
    return Path(anomaly).expanduser() / "appdata" / "user.ltx"


def mcm_files(gamma: str | Path) -> list[tuple[str, Path]]:
    """Every axr_options.ltx MO2 can load, as ``(path in GAMMA, file)``."""
    root = Path(gamma).expanduser()
    found: list[tuple[str, Path]] = []
    mods = root / "mods"
    if mods.is_dir():
        for candidate in sorted(mods.glob(f"*/gamedata/configs/{MCM_FILE}")):
            if candidate.is_file():
                found.append((candidate.relative_to(root).as_posix(), candidate))
    overwrite = root / "overwrite" / "gamedata" / "configs" / MCM_FILE
    if overwrite.is_file():
        found.append((overwrite.relative_to(root).as_posix(), overwrite))
    return found


def collect(
    anomaly: str | Path,
    gamma: str | Path,
    *,
    saves: bool = True,
    settings: bool = True,
) -> list[tuple[str, Path]]:
    """The ``(name in the zip, file on disk)`` pairs a backup would hold."""
    files: list[tuple[str, Path]] = []
    if saves:
        folder = saves_dir(anomaly)
        if folder.is_dir():
            for item in sorted(folder.rglob("*")):
                if item.is_file() and not item.is_symlink():
                    files.append(("saves/" + item.relative_to(folder).as_posix(), item))
    if settings:
        ltx = user_ltx_path(anomaly)
        if ltx.is_file():
            files.append(("user.ltx", ltx))
        files.extend(("mcm/" + rel, path) for rel, path in mcm_files(gamma))
    return files


def _count_saves(names) -> int:
    """Saves, not files: each save is a .scop plus its .scoc and thumbnail."""
    return sum(1 for name in names if name.startswith("saves/") and name.lower().endswith(".scop"))


# -- writing ------------------------------------------------------------------
def _write_zip(
    dest: Path,
    files: list[tuple[str, Path]],
    manifest: dict,
    report: Report,
) -> None:
    total = sum(path.stat().st_size for _name, path in files)
    dest.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest.parent).free
    if total * 1.05 + 1_000_000 > free:
        raise BackupError(
            f"Not enough free space for the backup: it needs about "
            f"{total / 1e6:.0f} MB, {free / 1e6:.0f} MB is free in {dest.parent}."
        )
    fd, tmp = tempfile.mkstemp(prefix=".partial-", suffix=".zip", dir=dest.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
            for index, (name, path) in enumerate(files, 1):
                archive.write(path, name)
                if index % 50 == 0 or index == len(files):
                    report(f"Backed up {index}/{len(files)} files...")
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def create_backup(
    profile_name: str,
    anomaly: str | Path,
    gamma: str | Path,
    *,
    reason: str = "manual",
    name: str = "",
    saves: bool = True,
    settings: bool = True,
    report: Report = lambda _line: None,
) -> BackupInfo | None:
    """Write a backup; ``None`` when there was nothing to back up.

    Raises :class:`BackupError` (or ``OSError``) when it cannot be written -
    callers about to delete the install must then stop.
    """
    try:
        files = collect(anomaly, gamma, saves=saves, settings=settings)
    except OSError as exc:
        raise BackupError(f"Could not read the files to back up: {exc}") from exc
    if not files:
        report("No saves, user.ltx or MCM settings found - nothing to back up.")
        return None
    created = time.time()
    label = clean_name(name)
    save_count = _count_saves(member for member, _path in files)
    manifest = {
        "format": 1,
        "profile": profile_name,
        "created": created,
        "reason": reason,
        "name": label,
        "saves": save_count,
        "user_ltx": any(member == "user.ltx" for member, _path in files),
        "mcm": [member[4:] for member, _path in files if member.startswith("mcm/")],
    }
    folder = profile_backup_dir(profile_name)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(created))
    # The name goes into the file name too, so the backup folder reads
    # sensibly in a file manager.
    tag = _slug(label)[:40] if label else reason
    dest = folder / f"{stamp}-{tag}.zip"
    suffix = 1
    while dest.exists():
        suffix += 1
        dest = folder / f"{stamp}-{tag}-{suffix}.zip"
    report(f"Backing up {len(files)} files to {dest} ...")
    try:
        _write_zip(dest, files, manifest, report)
    except OSError as exc:
        raise BackupError(f"Could not write the backup: {exc}") from exc
    report("Backup complete.")
    if reason != "manual":
        prune_automatic(profile_name)
    info = read_info(dest)
    if info is None:
        raise BackupError(f"The backup could not be read back: {dest}")
    return info


# -- listing ------------------------------------------------------------------
def read_info(path: Path) -> BackupInfo | None:
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.getinfo(MANIFEST).file_size > 1_000_000:
                return None
            data = json.loads(archive.read(MANIFEST).decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    if not isinstance(data, dict):
        return None
    try:
        created = float(data.get("created", 0))
        if not math.isfinite(created):
            created = 0.0
        mcm = [str(item) for item in data.get("mcm", []) if isinstance(item, str)]
        return BackupInfo(
            path=path,
            profile=str(data.get("profile", "")),
            created=created,
            reason=str(data.get("reason", "manual")),
            name=clean_name(str(data.get("name", "") or "")),
            saves=int(data.get("saves", 0)),
            user_ltx=bool(data.get("user_ltx", False)),
            mcm=mcm,
            size=path.stat().st_size,
        )
    except (OSError, TypeError, ValueError, OverflowError):
        return None


def list_backups(profile_name: str) -> list[BackupInfo]:
    """This profile's backups, newest first."""
    folder = profile_backup_dir(profile_name)
    if not folder.is_dir():
        return []
    infos = [
        info
        for path in folder.glob("*.zip")
        if not path.name.startswith(".") and (info := read_info(path)) is not None
    ]
    return sorted(infos, key=lambda info: info.created, reverse=True)


def prune_automatic(profile_name: str, keep: int = KEEP_AUTOMATIC) -> list[Path]:
    removed: list[Path] = []
    by_reason: dict[str, list[BackupInfo]] = {}
    for info in list_backups(profile_name):  # newest first
        if info.reason != "manual":
            by_reason.setdefault(info.reason, []).append(info)
    for infos in by_reason.values():
        for info in infos[keep:]:
            try:
                info.path.unlink()
                removed.append(info.path)
            except OSError:
                pass
    return removed


def _prune_folder(folder: Path) -> None:
    """prune_automatic() for a folder rather than a profile name."""
    infos = sorted(
        (i for p in folder.glob("*.zip") if (i := read_info(p)) is not None),
        key=lambda i: i.created,
        reverse=True,
    )
    by_reason: dict[str, list[BackupInfo]] = {}
    for info in infos:
        if info.reason != "manual":
            by_reason.setdefault(info.reason, []).append(info)
    for group in by_reason.values():
        for info in group[KEEP_AUTOMATIC:]:
            try:
                info.path.unlink()
            except OSError:
                pass


def rename_profile_backups(old_name: str, new_name: str) -> None:
    """Keep a profile's backups with it when the profile is renamed."""
    old, new = profile_backup_dir(old_name), profile_backup_dir(new_name)
    if old == new or not old.is_dir() or new.exists():
        return
    try:
        old.rename(new)
    except OSError:
        pass


def delete_backup(info: BackupInfo) -> None:
    root = backups_root().resolve()
    target = info.path.resolve()
    if root not in target.parents or target.suffix != ".zip":
        raise BackupError(f"Refusing to delete a file outside the backup folder: {target}")
    target.unlink()


# -- restoring ----------------------------------------------------------------
def _safe_member(name: str) -> PurePosixPath | None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or "\\" in name:
        return None
    return path


def _mcm_target(gamma: Path, rel: str) -> Path:
    """Where a backed-up axr_options.ltx goes back to.

    Its own mod folder when that still exists; otherwise (the mod was
    renamed, or the reinstall named it differently) MO2's overwrite
    folder, which every mod loads under, so the values still apply.
    """
    parts = PurePosixPath(rel).parts
    if len(parts) >= 2 and parts[0] == "mods" and (gamma / parts[0] / parts[1]).is_dir():
        return gamma.joinpath(*parts)
    return gamma / "overwrite" / "gamedata" / "configs" / MCM_FILE


def restore_plan(
    info: BackupInfo,
    anomaly: str | Path,
    gamma: str | Path,
    *,
    saves: bool = True,
    settings: bool = True,
    user_ltx: bool = True,
    mcm: bool = True,
) -> list[tuple[str, Path]]:
    """``(member, destination)`` for every file a restore would write.

    ``settings`` covers both user.ltx and MCM; ``user_ltx`` / ``mcm`` narrow
    it to one of them.
    """
    anomaly_dir = Path(anomaly).expanduser()
    gamma_dir = Path(gamma).expanduser()
    plan: list[tuple[str, Path]] = []
    with zipfile.ZipFile(info.path) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
    mcm_members: list[tuple[str, Path]] = []
    for name in names:
        member = _safe_member(name)
        if member is None:
            continue
        top = member.parts[0]
        if saves and top == "saves" and len(member.parts) > 1:
            plan.append((name, saves_dir(anomaly_dir).joinpath(*member.parts[1:])))
        elif settings and user_ltx and name == "user.ltx":
            plan.append((name, user_ltx_path(anomaly_dir)))
        elif settings and mcm and top == "mcm" and member.name == MCM_FILE:
            rel = PurePosixPath(*member.parts[1:]).as_posix()
            mcm_members.append((name, _mcm_target(gamma_dir, rel)))
    # Overwrite-folder copies last: MO2 gives that folder the final say.
    mcm_members.sort(key=lambda item: item[0].startswith("mcm/overwrite/"))
    plan.extend(mcm_members)
    return plan


def restore_backup(
    info: BackupInfo,
    anomaly: str | Path,
    gamma: str | Path,
    *,
    saves: bool = True,
    settings: bool = True,
    user_ltx: bool = True,
    mcm: bool = True,
    report: Report = lambda _line: None,
) -> int:
    """Copy a backup back into place. Returns the number of files written."""
    anomaly_dir = Path(anomaly).expanduser()
    if not anomaly_dir.is_dir():
        raise BackupError(f"The Anomaly folder does not exist: {anomaly_dir}")
    plan = restore_plan(
        info, anomaly, gamma, saves=saves, settings=settings, user_ltx=user_ltx, mcm=mcm
    )
    if not plan:
        raise BackupError("That backup holds nothing of the selected kind.")
    replaced: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for _name, dest in plan:
        if dest.is_file() and dest not in seen:
            seen.add(dest)
            label = (
                "saves/" + dest.relative_to(saves_dir(anomaly_dir)).as_posix()
                if saves_dir(anomaly_dir) in dest.parents
                else "user.ltx"
                if dest == user_ltx_path(anomaly_dir)
                else "mcm/" + dest.relative_to(Path(gamma).expanduser()).as_posix()
            )
            replaced.append((label, dest))
    if replaced:
        report(f"Keeping a copy of the {len(replaced)} files this restore replaces...")
        create_backup_of_files(info.profile, replaced, report, folder=info.path.parent)
    written = 0
    with zipfile.ZipFile(info.path) as archive:
        for name, dest in plan:
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".restore-", dir=dest.parent)
            try:
                with os.fdopen(fd, "wb") as out, archive.open(name) as src:
                    shutil.copyfileobj(src, out)
                os.replace(tmp, dest)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            written += 1
    report(f"Restored {written} files.")
    return written


def create_backup_of_files(
    profile_name: str,
    files: list[tuple[str, Path]],
    report: Report,
    *,
    folder: Path | None = None,
) -> BackupInfo | None:
    """A "before restore" backup of exactly the given files.

    Written into ``folder`` (the restored backup's own folder) so it shows
    up in the same list, whatever the profile is called now.
    """
    created = time.time()
    manifest = {
        "format": 1,
        "profile": profile_name,
        "created": created,
        "reason": "restore",
        "saves": _count_saves(name for name, _p in files),
        "user_ltx": any(name == "user.ltx" for name, _p in files),
        "mcm": [name[4:] for name, _p in files if name.startswith("mcm/")],
    }
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(created))
    folder = folder if folder is not None else profile_backup_dir(profile_name)
    dest = folder / f"{stamp}-restore.zip"
    suffix = 1
    while dest.exists():
        suffix += 1
        dest = dest.with_name(f"{stamp}-restore-{suffix}.zip")
    try:
        _write_zip(dest, files, manifest, report)
    except OSError as exc:
        raise BackupError(f"Could not keep a copy of the current files: {exc}") from exc
    _prune_folder(folder)
    return read_info(dest)


def backup_before_wipe(
    profile, wipe_labels: set[str], reason: str, report: Report
) -> BackupInfo | None:
    """The automatic backup that runs before a reset or uninstall.

    Saves and user.ltx are at risk when the Anomaly folder is wiped; MCM
    settings when the GAMMA folder is. user.ltx is always included - it is
    tiny, and a GAMMA reinstall without "preserve" resets it too.
    """
    anomaly_wiped = "Anomaly" in wipe_labels
    gamma_wiped = "GAMMA" in wipe_labels
    if not (anomaly_wiped or gamma_wiped):
        return None
    report("Backing up saves, user.ltx and MCM settings before deleting anything...")
    return create_backup(
        profile.profile_name,
        profile.anomaly,
        profile.gamma,
        reason=reason,
        saves=anomaly_wiped,
        settings=True,
        report=report,
    )


def backup_settings_before(profile, reason: str, report: Report = lambda _line: None) -> str | None:
    """Back up user.ltx and MCM settings before a job that can reset them.

    Updates, repairs and reinstalls do not touch saves, but with "preserve"
    unticked (or a CLI bug) they rewrite user.ltx and axr_options.ltx. The
    two files are a few KB, so this runs in place, before the job starts.
    Returns an error message, or ``None`` - a failure is reported, never
    allowed to block the job itself.
    """
    if profile is None:
        return None
    try:
        create_backup(
            profile.profile_name,
            profile.anomaly,
            profile.gamma,
            reason=reason,
            saves=False,
            settings=True,
            report=report,
        )
    except (BackupError, OSError) as exc:
        return str(exc)
    return None


# -- restoring settings after a GAMMA Reset -------------------------------------
#: gui-settings key: profile name -> {"path", "user", "mcm"}. A GAMMA Reset
#: deletes the GAMMA folder before reinstalling, so the CLI's own "preserve
#: MCM settings" has nothing left to preserve; the pre-reset backup is put
#: back instead once the reinstall has succeeded. Stored on disk so a
#: reinstall that fails and is resumed later still gets it.
_PENDING_KEY = "pending_settings_restore"


def mark_settings_restore(profile_name: str, info: BackupInfo, *, user_ltx: bool, mcm: bool) -> None:
    from . import gui_settings

    if not (user_ltx or mcm) or not info.has_settings:
        return
    pending = dict(gui_settings.load_gui_settings().get(_PENDING_KEY) or {})
    pending[profile_name] = {"path": str(info.path), "user": bool(user_ltx), "mcm": bool(mcm)}
    gui_settings.save_gui_settings(**{_PENDING_KEY: pending})


def apply_pending_settings_restore(profile) -> str | None:
    """After a successful GAMMA install: put back the settings a reset kept.

    Returns a line for the install log, or ``None`` when nothing was pending.
    """
    from . import gui_settings

    if profile is None:
        return None
    pending = dict(gui_settings.load_gui_settings().get(_PENDING_KEY) or {})
    entry = pending.pop(profile.profile_name, None)
    if not isinstance(entry, dict):
        return None
    gui_settings.save_gui_settings(**{_PENDING_KEY: pending})
    path = Path(str(entry.get("path", "")))
    root = backups_root().resolve()
    try:
        if root not in path.resolve().parents:
            return None
    except OSError:
        return None
    info = read_info(path)
    if info is None:
        return f"Could not restore your user.ltx / MCM settings: backup not found ({path})."
    try:
        count = restore_backup(
            info,
            profile.anomaly,
            profile.gamma,
            saves=False,
            settings=True,
            user_ltx=bool(entry.get("user", True)),
            mcm=bool(entry.get("mcm", True)),
        )
    except (BackupError, OSError, KeyError, zipfile.BadZipFile) as exc:
        return f"Could not restore your user.ltx / MCM settings: {exc}"
    return f"Restored your user.ltx / MCM settings from before the reset ({count} file(s))."


def describe(info: BackupInfo) -> str:
    """One line: what is in the backup."""
    parts = []
    if info.saves:
        parts.append(f"{info.saves} saves")
    if info.user_ltx:
        parts.append("user.ltx")
    if info.mcm:
        parts.append("MCM settings")
    return ", ".join(parts) or "empty"
