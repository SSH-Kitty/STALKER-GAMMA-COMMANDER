"""Auto-repair helpers for GAMMA mod integrity verification.

Given the failures reported by an MD5 scan (``integrity.scan_mods_md5``),
these helpers figure out which mods can be re-downloaded and re-extracted
through the official modpack list, and prepare the install for repair:
the corrupt mod folder and its cached archive are deleted so a subsequent
``full install --skip-extract-on-hash-match`` re-downloads (MD5-verified
against the official archive checksum) and re-extracts just those mods.
"""

from __future__ import annotations

import re
import shutil
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .integrity import Md5ScanResult
from .launcher import (
    Runner,
    _same_file_contents,
    host_wine_lib_dirs,
    runner_build_dir,
)
from .network import read_response_bytes
from .network import urlopen_with_retry as urlopen

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@dataclass
class ModPackRecord:
    """One entry of the modpack maker list (line ``Counter`` of the TSV)."""

    counter: int
    addon_name: str
    patch: str
    dl_link: str
    mod_db_url: str
    zip_name: str
    md5_mod_db: str
    instructions: str

    @property
    def folder_name(self) -> str:
        name = f"{self.counter}- {self.addon_name}"
        if self.patch:
            name += f" {self.patch}"
        return name

    def archive_names(self) -> list[str]:
        """The file name(s) this addon's archive has in the download cache.

        Must match what the CLI actually writes, or Verify's repair moves the
        mod folder aside but not its archive - the reinstall then finds a
        hash-matching archive, skips extracting it, and the mod is gone.

        * GitHub downloads are cached as ``<repo>.zip`` (github.com/<owner>/
          <repo>/archive/...) whatever the URL ends in - taking the URL's
          last segment gave "latest.zip.zip" / "main.zip.zip", and the
          list's ZipName column is unreliable for them (it can be a stale
          release name, or carry a checksum after the name).
        * Everything else uses ZipName, cut at the first whitespace for the
          same reason.
        """
        repo = _github_repo(self.dl_link)
        if repo:
            return [f"{repo}.zip"]
        name = self.zip_name.split()[0] if self.zip_name.strip() else ""
        return [name] if name else []


def _github_repo(url: str) -> str:
    """``repo`` from a github.com/<owner>/<repo>/... URL, else ""."""
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return ""
    if not parsed.netloc.lower().endswith("github.com"):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    return parts[1] if len(parts) >= 2 else ""


def parse_modpack_records(text: str) -> dict[str, ModPackRecord]:
    """Parse the tab-separated modpack maker list into records by folder name.

    The list is a tab-separated file whose per-line fields are
    ``DlLink, Instructions, Patch, AddonName, ModDbUrl, ZipName, Md5ModDb``.
    Folder names are ``{line}- {AddonName} {Patch}`` (same convention the
    installer uses). Lines without an addon name (category headers) are skipped.
    """
    records: dict[str, ModPackRecord] = {}
    for counter, line in enumerate(text.splitlines(), start=1):
        parts = line.split("\t")
        addon = parts[3].strip() if len(parts) > 3 else ""
        if not addon:
            continue
        record = ModPackRecord(
            counter=counter,
            addon_name=addon,
            patch=parts[2].strip() if len(parts) > 2 else "",
            dl_link=parts[0].strip() if parts else "",
            mod_db_url=parts[4].strip() if len(parts) > 4 else "",
            zip_name=parts[5].strip() if len(parts) > 5 else "",
            md5_mod_db=parts[6].strip() if len(parts) > 6 else "",
            instructions=parts[1].strip() if len(parts) > 1 else "",
        )
        records[record.folder_name] = record
    return records


def fetch_modpack_records(url: str, timeout: float = 20) -> dict[str, ModPackRecord]:
    """Download the modpack maker list and map folder names to records.

    The list is a tab-separated file whose per-line fields are
    ``DlLink, Instructions, Patch, AddonName, ModDbUrl, ZipName, Md5ModDb``.
    Folder names are ``{line}- {AddonName} {Patch}`` (same convention the
    installer uses). Returns an empty dict if the list cannot be fetched.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            text = read_response_bytes(resp, 32 * 1024 * 1024).decode(
                "utf-8", errors="replace"
            )
    except (OSError, ValueError, UnicodeError):
        return {}
    return parse_modpack_records(text)


_FOLDER_NAME_PREFIX_RE = re.compile(r"^\d+- (.*)$")


def _name_only(folder_name: str) -> str:
    """Strip the leading "<counter>- " that both a record's folder_name and
    the on-disk mod directory share, leaving just the addon name + patch.

    The counter is the record's line number in the official modpack list,
    which shifts whenever that list is reordered (an addon added earlier in
    the list bumps every counter after it). An install made under an older
    list has mod folders named after the *old* counters, so matching on the
    full folder_name against a freshly-fetched records dict misses them even
    though the addon is still present - it just moved to a different line.
    """
    match = _FOLDER_NAME_PREFIX_RE.match(folder_name)
    return match.group(1) if match else folder_name


def find_record_for_folder(
    folder: str, records: dict[str, ModPackRecord]
) -> ModPackRecord | None:
    """Find the record for an on-disk mod folder, tolerating a counter shift.

    Tries an exact folder_name match first (cheap, and correct whenever the
    list has not been reordered since this mod was installed), then falls
    back to matching on the addon name + patch alone.

    Returns None (unrepairable) if the fallback name-only match is
    ambiguous - i.e. more than one record shares that stripped name (a
    renamed/relisted/forked mod, or a duplicate entry). Repairing against
    a guessed record with no way to confirm it is actually the same mod
    would delete/redownload against the wrong archive and checksum, which
    is worse than leaving it unrepaired.
    """
    record = records.get(folder)
    if record is not None:
        return record
    name = _name_only(folder)
    matches = [c for c in records.values() if _name_only(c.folder_name) == name]
    return matches[0] if len(matches) == 1 else None


@dataclass
class RepairPlan:
    """How the problems found by an MD5 scan should be handled."""

    repairable: list[str] = field(default_factory=list)
    unrepairable: list[str] = field(default_factory=list)
    added_only: list[str] = field(default_factory=list)
    #: folder -> the record it was actually matched against (possibly via
    #: the name-only fallback), for every entry in ``repairable``.
    matched_records: dict[str, ModPackRecord] = field(default_factory=dict)
    #: folder -> (changed files, removed files) the scan found in it.
    file_counts: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def has_repairable(self) -> bool:
        return bool(self.repairable)


def classify_problems(
    scan: Md5ScanResult,
    records: dict[str, ModPackRecord],
    extra_broken_folders: Iterable[str] = (),
) -> RepairPlan:
    """Split the scan problems into repairable / unrepairable / added.

    Changed and removed files are repairable when their mod folder maps to a
    record in the official modpack list. Files that only appeared (added) are
    never repaired - they are usually the user's own edits - and mods without
    a record (extras, special repo mods) are reported but left alone.

    ``extra_broken_folders`` lets a caller fold in mod folders known to be
    broken by some other check (e.g. ``verify_gamma``'s presence check
    reporting a mod missing/empty) - a folder that never made it into the
    MD5 baseline in the first place produces no changed/removed entries of
    its own, so without this it would be reported forever but never
    actually offered for repair.
    """
    plan = RepairPlan()

    def folder_of(rel: str) -> str | None:
        parts = rel.split("/")
        if len(parts) >= 2 and parts[0] == "mods":
            return parts[1]
        return None

    for key, rels in (("changed", scan.changed), ("removed", scan.removed)):
        for rel in set(rels):
            folder = folder_of(rel)
            if folder is None:
                continue
            changed, removed = plan.file_counts.get(folder, (0, 0))
            plan.file_counts[folder] = (
                (changed + 1, removed) if key == "changed" else (changed, removed + 1)
            )
    broken_folders = {folder_of(rel) for rel in set(scan.changed) | set(scan.removed)}
    broken_folders.discard(None)
    broken_folders.update(extra_broken_folders)
    unrepairable_rels = [
        rel
        for rel in sorted(set(scan.changed) | set(scan.removed))
        if folder_of(rel) is None
    ]

    for folder in sorted(broken_folders):
        record = find_record_for_folder(folder, records)
        if record is not None:
            if folder not in plan.repairable:
                plan.repairable.append(folder)
                plan.matched_records[folder] = record
        elif folder not in plan.unrepairable:
            plan.unrepairable.append(folder)
    plan.unrepairable.extend(unrepairable_rels)
    for rel in sorted(scan.added):
        folder = folder_of(rel)
        if folder is not None and folder not in plan.added_only:
            plan.added_only.append(folder)
    return plan


def repair_preview(plan: RepairPlan | None, anomaly_problems: Iterable[str] = ()) -> str:
    """The full, plain-text list of what a repair will do - shown behind
    "Show Details..." in the repair prompt, so nothing it touches is hidden
    behind a "... and N more"."""
    out: list[str] = []
    anomaly_problems = list(anomaly_problems)
    if anomaly_problems:
        out.append(f"Anomaly files to re-install ({len(anomaly_problems)}):")
        out.extend(f"  {line}" for line in anomaly_problems)
        out.append("")
    if plan is not None and plan.repairable:
        out.append(f"GAMMA mods to re-install ({len(plan.repairable)}):")
        for folder in plan.repairable:
            changed, removed = plan.file_counts.get(folder, (0, 0))
            parts = []
            if changed:
                parts.append(f"{changed} changed")
            if removed:
                parts.append(f"{removed} missing")
            detail = ", ".join(parts) or "missing or empty"
            record = plan.matched_records.get(folder)
            archives = ", ".join(record.archive_names()) if record is not None else ""
            out.append(f"  {folder}  ({detail})" + (f"  <- {archives}" if archives else ""))
        out.append("")
    if plan is not None and plan.unrepairable:
        out.append(f"Left as they are - no download source ({len(plan.unrepairable)}):")
        out.extend(f"  {name}" for name in plan.unrepairable)
        out.append("")
    if plan is not None and plan.added_only:
        out.append(
            f"Mods with extra files you added - not touched ({len(plan.added_only)}):"
        )
        out.extend(f"  {name}" for name in plan.added_only)
        out.append("")
    out.append("Also kept safe:")
    out.append("  - your modlist.txt (order, enabled/disabled, your own mods) is restored afterwards")
    out.append("  - user.ltx and MCM settings are backed up first (a repair never touches saves)")
    out.append("  - mod folders are set aside, not deleted, until their reinstall succeeds")
    return "\n".join(out).rstrip()


_QUARANTINE_DIRNAME = ".verify-quarantine"


@dataclass
class QuarantineItem:
    """One file/folder moved aside by a quarantine operation."""

    original: Path
    quarantined: Path


@dataclass
class QuarantineRecord:
    """Everything moved aside for one mod folder, so it can be restored."""

    folder: str
    items: list[QuarantineItem] = field(default_factory=list)


def _quarantine_dest(quarantine_dir: Path, name: str) -> Path:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{int(time.time())}.{uuid.uuid4().hex[:8]}"
    return quarantine_dir / f"{name}.{suffix}"


def quarantine_mod_and_archive(
    gamma_dir: str,
    folder: str,
    record: ModPackRecord | None = None,
) -> QuarantineRecord:
    """Move a broken mod folder and its cached archive(s) aside, not delete them.

    Verify Integrity's repair reinstalls whatever gets moved out of the
    way here - if that reinstall then fails or is cancelled, the caller
    can restore everything via ``restore_from_quarantine`` instead of the
    mod simply being gone. Nothing is permanently removed until
    ``purge_quarantine`` is called, which only happens after a repair is
    confirmed to have actually succeeded.
    """
    result = QuarantineRecord(folder=folder)
    base = Path(gamma_dir)
    mods_root = base / "mods"
    mod_path = mods_root / folder
    # This moves user data, so never act on a name that escapes mods/.
    if not _is_direct_child(mods_root, mod_path):
        raise ValueError(f"Refusing to touch mod folder outside mods/: {folder!r}")
    try:
        if mod_path.is_dir():
            dest = _quarantine_dest(mods_root / _QUARANTINE_DIRNAME, folder)
            mod_path.rename(dest)
            result.items.append(QuarantineItem(original=mod_path, quarantined=dest))
        if mod_path.exists():
            raise OSError(f"Mod folder still exists after quarantine: {mod_path}")
        if record is not None:
            # gamma/downloads is commonly a symlink to the cache directory;
            # resolve it first or the child guard below rejects every archive.
            downloads = (base / "downloads").resolve()
            for name in record.archive_names():
                archive = downloads / name
                if not _is_direct_child(downloads, archive):
                    continue
                if archive.is_file():
                    dest = _quarantine_dest(downloads / _QUARANTINE_DIRNAME, name)
                    archive.rename(dest)
                    result.items.append(
                        QuarantineItem(original=archive, quarantined=dest)
                    )
                if archive.exists():
                    raise OSError(f"Archive still exists after quarantine: {archive}")
    except (OSError, ValueError):
        # All-or-nothing: the returned QuarantineRecord is the caller's ONLY
        # handle on what was moved, and raising means the caller never gets
        # it (install_page logs the folder as "could not be set aside" and
        # carries on). A mod folder left half-quarantined that way would
        # never be restored on cancel/failure, and the next successful
        # repair's purge_quarantine() would then permanently delete the
        # user's only copy of it. Put anything already moved back first.
        restore_from_quarantine(result)
        raise
    return result


def restore_from_quarantine(record: QuarantineRecord) -> list[str]:
    """Move everything in ``record`` back to where it came from.

    Best-effort per item, matching the rest of this module's "one
    stubborn item must not stop the others" philosophy - returns a list
    of human-readable failure messages (empty on full success) instead of
    raising, since this itself typically runs as part of reporting a
    different failure (the repair install that made restoring necessary
    in the first place).
    """
    failures: list[str] = []
    for item in record.items:
        try:
            if item.quarantined.exists() and not item.original.exists():
                item.original.parent.mkdir(parents=True, exist_ok=True)
                item.quarantined.rename(item.original)
        except OSError as exc:
            failures.append(f"{item.quarantined} -> {item.original}: {exc}")
    return failures


@dataclass
class SettleResult:
    """What ``settle_quarantine`` did with each set-aside mod."""

    reinstalled: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _mod_item(record: QuarantineRecord) -> QuarantineItem | None:
    for item in record.items:
        if item.original.parent.name == "mods":
            return item
    return None


def _has_content(folder: Path) -> bool:
    try:
        return folder.is_dir() and any(folder.iterdir())
    except OSError:
        return False


def settle_quarantine(records: Iterable[QuarantineRecord]) -> SettleResult:
    """After a repair install, keep what was reinstalled, undo what wasn't.

    The installer exiting successfully does not prove every set-aside mod
    came back: when a mod's cached archive was not set aside with it (a
    name mismatch, an archive shared with another mod), the installer finds
    a hash-matching archive, skips extracting it, and the mod folder is
    simply never recreated. Deleting everything set aside at that point
    (what the repair used to do) left those mods gone for good - a "repair"
    that broke the game. So each mod is checked: a non-empty folder means
    its old copy (and archive) can go; anything else is moved back.
    """
    result = SettleResult()
    for record in records:
        mod_item = _mod_item(record)
        if mod_item is not None and _has_content(mod_item.original):
            for item in record.items:
                try:
                    if item.quarantined.is_dir() and not item.quarantined.is_symlink():
                        shutil.rmtree(item.quarantined)
                    elif item.quarantined.exists() or item.quarantined.is_symlink():
                        item.quarantined.unlink()
                except OSError as exc:
                    # Harmless leftover; the reinstalled copy is in place.
                    result.failures.append(f"could not remove {item.quarantined}: {exc}")
            result.reinstalled.append(record.folder)
            continue
        # Not reinstalled: an empty folder the installer may have created
        # would block the restore, so clear it first.
        if mod_item is not None and mod_item.original.is_dir():
            try:
                mod_item.original.rmdir()
            except OSError:
                pass
        failures = restore_from_quarantine(record)
        result.failures.extend(failures)
        if not failures:
            result.restored.append(record.folder)
    return result


def purge_quarantine(gamma_dir: str) -> None:
    """Permanently delete anything left in the quarantine folders.

    Only call this once a repair is confirmed successful - this is the
    actual point of no return that ``quarantine_mod_and_archive`` defers.
    Best-effort: a failure here just leaves the (otherwise harmless,
    already-replaced) quarantined copies on disk for manual cleanup.
    """
    base = Path(gamma_dir)
    try:
        downloads = (base / "downloads").resolve()
    except OSError:
        downloads = base / "downloads"
    for quarantine_dir in (
        base / "mods" / _QUARANTINE_DIRNAME,
        downloads / _QUARANTINE_DIRNAME,
    ):
        try:
            if quarantine_dir.is_dir():
                shutil.rmtree(quarantine_dir)
        except OSError:
            pass


def _is_direct_child(parent: Path, child: Path) -> bool:
    """True when ``child`` is exactly one level inside ``parent``."""
    if parent.is_symlink() or child.is_symlink():
        return False
    if not child.name or child.name in (".", ".."):
        return False
    try:
        return child.parent == parent and child.resolve().parent == parent.resolve()
    except OSError:
        return False


# --------------------------------------------------------- Wine prefixes
#: Proton's placeholders for DLLs it does not copy are a few hundred bytes;
#: a real Windows DLL never is.
_PLACEHOLDER_DLL_BYTES = 8 * 1024

_ARCH_DIRS = (
    ("system32", "x86_64-windows"),
    ("syswow64", "i386-windows"),
)


def foreign_prefix_dlls(prefix: str | Path, runner: Runner) -> list[tuple[Path, Path]]:
    """DLLs in the prefix that some *other* Wine on this host wrote there.

    A file is foreign when it is a real DLL (not one of Proton's tiny
    placeholders) and is byte-identical to a builtin shipped by a Wine
    installed on the host that is not the runner's own build. Proton never
    puts another build's files in its prefix, so that identity is proof of
    who wrote it - and it is deliberately the only test used, because
    "differs from Proton's copy" is not enough: DXVK's ``d3d11.dll`` and a
    genuine Microsoft ``msvcp140.dll`` from a winetricks verb both differ
    from Proton's builtins and both belong exactly where they are.

    Returns ``(prefix_file, runner_replacement)`` pairs; the replacement is
    the same-named DLL from the runner's ``files/lib/wine/<arch>-windows``,
    which is precisely what Proton would have placed there itself.
    """
    build = runner_build_dir(runner)
    if build is None:
        return []
    host_dirs = [d for d in host_wine_lib_dirs() if build not in d.parents and d != build]
    if not host_dirs:
        return []
    root = Path(prefix).expanduser()
    if runner.kind == "proton":
        root = root / "pfx"
    windows = root / "drive_c" / "windows"
    foreign: list[tuple[Path, Path]] = []
    for sub, arch in _ARCH_DIRS:
        folder = windows / sub
        if not folder.is_dir():
            continue
        replacement_dir = build / "files" / "lib" / "wine" / arch
        for actual in sorted(folder.glob("*.dll")):
            try:
                if actual.is_symlink() or actual.stat().st_size < _PLACEHOLDER_DLL_BYTES:
                    continue
            except OSError:
                continue
            written_by_host_wine = any(
                _same_file_contents(actual, host / arch / actual.name)
                for host in host_dirs
                if (host / arch / actual.name).is_file()
            )
            if written_by_host_wine:
                foreign.append((actual, replacement_dir / actual.name))
    return foreign


def repair_prefix_foreign_dlls(prefix: str | Path, runner: Runner) -> list[str]:
    """Put the runner's own DLLs back over ones another Wine wrote.

    Proton copies a set of its DLLs into ``system32``/``syswow64`` as real
    files. If a different Wine build has run in the prefix since, its own
    versions of those files are sitting there instead, and Proton - which
    tracks them by name, not content - loads them and crashes on the first
    thread of every process.

    Every DLL :func:`foreign_prefix_dlls` identifies is replaced with the
    runner's copy; one the runner does not ship is removed, so Proton's own
    placeholder logic takes over on the next launch. Nothing else in the
    prefix is touched: not the registry, not ``winetricks.log``, not any
    third-party DLL.

    One consequence the caller must relay: runtimes a winetricks verb had
    installed (``vcrun2022``'s ``msvcp140.dll`` among them) were overwritten
    by the other Wine too, so they come back as Proton's builtins and the
    dependencies need reinstalling afterwards - through the runner's own
    Wine this time.

    Returns the relative paths repaired.
    """
    root = Path(prefix).expanduser()
    if runner.kind == "proton":
        root = root / "pfx"
    windows = root / "drive_c" / "windows"
    repaired: list[str] = []
    for actual, replacement in foreign_prefix_dlls(prefix, runner):
        relative = str(actual.relative_to(windows))
        if replacement.is_file():
            # Write beside the target and swap in, so an interrupted repair
            # cannot leave a half-written DLL where a working one was.
            temporary = actual.with_name(f".{actual.name}.commander-repair")
            try:
                shutil.copyfile(replacement, temporary)
                temporary.replace(actual)
            except OSError:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
        else:
            actual.unlink()
        repaired.append(relative)
    return repaired


#: Where the profile's modlist.txt is kept while a repair's installer runs.
PRE_REPAIR_SUFFIX = ".pre-repair.bak"


def snapshot_modlist(modlist_path: Path | None) -> Path | None:
    """Copy the profile's modlist.txt aside before a repair's installer runs.

    The repair runs the CLI's ``full-install``, which writes the official
    modlist.txt over the profile's own - dropping mods the user added and
    resetting every enable/disable choice. Returns the snapshot path, or
    None when there was nothing to snapshot.
    """
    if modlist_path is None or not modlist_path.is_file():
        return None
    snapshot = modlist_path.with_name(modlist_path.name + PRE_REPAIR_SUFFIX)
    try:
        shutil.copy2(modlist_path, snapshot)
    except OSError:
        return None
    return snapshot


def restore_modlist_after_repair(modlist_path: Path, snapshot: Path | None) -> str:
    """Put the user's modlist.txt back after a repair, when that's safe.

    Safe when the installer's list only contains mods the user's list already
    had - the repair changed files, not the modpack - so the user's own
    list (extra mods, order, enabled/disabled) is restored exactly. If the
    installer's list has new mods, the repair also applied a GAMMA update;
    the new list is kept (its new mods must stay listed) and the old one is
    left as the snapshot for the user. Returns a line for the repair log.
    """
    from .modlist import entries, read_lines, save_lines

    if snapshot is None or not snapshot.is_file():
        return ""
    try:
        before = read_lines(snapshot)
        after = read_lines(modlist_path) if modlist_path.is_file() else []
        before_names = {name for _status, name in entries(before)}
        after_names = {name for _status, name in entries(after)}
    except (OSError, ValueError) as exc:
        return f"Could not compare modlist.txt with its pre-repair copy ({exc}); kept {snapshot}."
    if after == before:
        snapshot.unlink(missing_ok=True)
        return ""
    if after_names <= before_names:
        try:
            save_lines(modlist_path, before)
        except OSError as exc:
            return f"Could not restore your modlist.txt ({exc}); your previous copy is {snapshot}."
        snapshot.unlink(missing_ok=True)
        return "Restored your own modlist.txt (mod order, extra mods, enabled/disabled)."
    return (
        "The repair also brought in newer GAMMA mods, so the installer's "
        f"modlist.txt was kept. Your previous one is saved as {snapshot.name}."
    )
