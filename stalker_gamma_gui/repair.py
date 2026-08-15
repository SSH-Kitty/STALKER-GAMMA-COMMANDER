"""Auto-repair helpers for GAMMA mod integrity verification.

Given the failures reported by an MD5 scan (``integrity.scan_mods_md5``),
these helpers figure out which mods can be re-downloaded and re-extracted
through the official modpack list, and prepare the install for repair:
the corrupt mod folder and its cached archive are deleted so a subsequent
``full install --skip-extract-on-hash-match`` re-downloads (MD5-verified
against the official archive checksum) and re-extracts just those mods.
"""

from __future__ import annotations

import shutil
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .integrity import Md5ScanResult

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
        names: list[str] = []
        if self.zip_name:
            names.append(self.zip_name)
        elif "github" in self.dl_link.lower():
            repo = self.dl_link.rstrip("/").split("/")[-1]
            if repo:
                names.append(f"{repo}.zip")
        return names


def fetch_modpack_records(url: str, timeout: float = 20) -> dict[str, ModPackRecord]:
    """Download the modpack maker list and map folder names to records.

    The list is a tab-separated file whose per-line fields are
    ``DlLink, Instructions, Patch, AddonName, ModDbUrl, ZipName, Md5ModDb``.
    Folder names are ``{line}- {AddonName} {Patch}`` (same convention the
    installer uses). Returns an empty dict if the list cannot be fetched.
    """
    records: dict[str, ModPackRecord] = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except OSError:
        return records
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


@dataclass
class RepairPlan:
    """How the problems found by an MD5 scan should be handled."""

    repairable: list[str] = field(default_factory=list)
    unrepairable: list[str] = field(default_factory=list)
    added_only: list[str] = field(default_factory=list)
    added_files: list[str] = field(default_factory=list)

    @property
    def has_repairable(self) -> bool:
        return bool(self.repairable)


def classify_problems(
    scan: Md5ScanResult,
    records: dict[str, ModPackRecord],
) -> RepairPlan:
    """Split the scan problems into repairable / unrepairable / added.

    Changed and removed files are repairable when their mod folder maps to a
    record in the official modpack list. Files that only appeared (added) are
    never repaired - they are usually the user's own edits - and mods without
    a record (extras, special repo mods) are reported but left alone.
    """
    plan = RepairPlan()

    def folder_of(rel: str) -> str | None:
        parts = rel.split("/")
        if len(parts) >= 2 and parts[0] == "mods":
            return parts[1]
        return None

    for rel in sorted(set(scan.changed) | set(scan.removed)):
        folder = folder_of(rel)
        if folder is not None and folder in records:
            if folder not in plan.repairable:
                plan.repairable.append(folder)
        else:
            if folder is None:
                plan.unrepairable.append(rel)
            elif folder not in plan.unrepairable:
                plan.unrepairable.append(folder)
    for rel in sorted(scan.added):
        plan.added_files.append(rel)
        folder = folder_of(rel)
        if folder is not None and folder not in plan.added_only:
            plan.added_only.append(folder)
    return plan


def delete_mod_and_archive(
    gamma_dir: str,
    folder: str,
    record: ModPackRecord | None = None,
) -> list[Path]:
    """Permanently delete a mod folder and its cached archive(s).

    Returns the paths that were removed. The archive lives in
    ``gamma/downloads`` (a symlink to the cache) under the record's archive
    name; deleting it forces the installer to re-download the mod.
    """
    removed: list[Path] = []
    base = Path(gamma_dir)
    mods_root = base / "mods"
    mod_path = mods_root / folder
    # This deletes user data, so never act on a name that escapes mods/.
    if not _is_direct_child(mods_root, mod_path):
        raise ValueError(f"Refusing to delete mod folder outside mods/: {folder!r}")
    if mod_path.is_dir():
        shutil.rmtree(mod_path, ignore_errors=True)
        removed.append(mod_path)
    if record is not None:
        downloads = base / "downloads"
        for name in record.archive_names():
            archive = downloads / name
            if not _is_direct_child(downloads, archive):
                continue
            if archive.is_file():
                archive.unlink(missing_ok=True)
                removed.append(archive)
    return removed


def _is_direct_child(parent: Path, child: Path) -> bool:
    """True when ``child`` is exactly one level inside ``parent``."""
    if not child.name or child.name in (".", ".."):
        return False
    try:
        return child.resolve().parent == parent.resolve()
    except OSError:
        return False
