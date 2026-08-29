"""Auto-repair helpers for GAMMA mod integrity verification.

Given the failures reported by an MD5 scan (``integrity.scan_mods_md5``),
these helpers figure out which mods can be re-downloaded and re-extracted
through the official modpack list, and prepare the install for repair:
the corrupt mod folder and its cached archive are deleted so a subsequent
``full install --skip-extract-on-hash-match`` re-downloads (MD5-verified
against the official archive checksum) and re-extracts just those mods.
"""

from __future__ import annotations

import enum
import shutil
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .integrity import Md5ScanResult
from .network import read_response_bytes

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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = read_response_bytes(resp, 32 * 1024 * 1024).decode(
                "utf-8", errors="replace"
            )
    except (OSError, ValueError, UnicodeError):
        return {}
    return parse_modpack_records(text)


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
        shutil.rmtree(mod_path)
        removed.append(mod_path)
    if mod_path.exists():
        raise OSError(f"Mod folder still exists after deletion: {mod_path}")
    if record is not None:
        downloads = base / "downloads"
        for name in record.archive_names():
            archive = downloads / name
            if not _is_direct_child(downloads, archive):
                continue
            if archive.is_file():
                archive.unlink(missing_ok=True)
                removed.append(archive)
            if archive.exists():
                raise OSError(f"Archive still exists after deletion: {archive}")
    return removed


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


# ── verify / repair state machine (pure logic, testable without UI) ────────


class VerifyPhase(enum.Enum):
    """High-level phase of a verify + optional repair run."""

    IDLE = "idle"
    CHECKING_ANOMALY = "checking_anomaly"
    CHECKING_GAMMA = "checking_gamma"
    SCANNING_MD5 = "scanning_md5"
    LOOKING_UP_RECORDS = "looking_up_records"
    PROMPTING_REPAIR = "prompting_repair"
    REPAIRING_ANOMALY = "repairing_anomaly"
    REPAIRING_GAMMA = "repairing_gamma"
    RECHECKING_ANOMALY = "rechecking_anomaly"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass
class VerifyRepairState:
    """Snapshot of the verify / repair pipeline at a point in time.

    The *pure* helper :func:`decide_next_phase` takes a ``VerifyRepairState``
    and returns the next phase + any commands the caller should execute.  The
    caller (UI layer) runs those commands asynchronously and feeds the results
    back into a new ``VerifyRepairState``.  This keeps all decision logic in
    ``repair.py`` where it can be unit-tested without PySide6.
    """

    phase: VerifyPhase = VerifyPhase.IDLE

    # Anomaly check results
    anomaly_ok: int = 0
    anomaly_corrupt: int = 0
    anomaly_not_found: int = 0
    anomaly_recheck_done: bool = False

    # GAMMA presence check results
    gamma_missing: int = 0
    gamma_empty: int = 0
    gamma_ok: int = 0
    gamma_disabled: int = 0
    gamma_not_installed: bool = False

    # GAMMA MD5 scan results
    scan: Md5ScanResult | None = None
    scan_cancelled: bool = False

    # Repair plan
    plan: RepairPlan | None = None
    records: dict[str, ModPackRecord] = field(default_factory=dict)

    # Repair orchestration
    anomaly_needs_repair: bool = False
    gamma_repairable: bool = False
    repair_anomaly_pending: bool = False
    gamma_repair_pending: bool = False

    # Final verdict
    final_ok: bool | None = None
    final_message: str = ""

    @property
    def anomaly_clean(self) -> bool:
        return self.anomaly_corrupt == 0 and self.anomaly_not_found == 0

    @property
    def gamma_clean(self) -> bool:
        return self.gamma_missing == 0 and self.gamma_empty == 0

    @property
    def scan_clean(self) -> bool:
        return self.scan is not None and self.scan.problems == 0


@dataclass
class PhaseCommand:
    """A command the caller should execute, returned by :func:`decide_next_phase`.

    ``kind`` is a stable string tag (``"anomaly_check"``, ``"gamma_verify"``,
    ``"md5_scan"``, ``"fetch_records"``, ``"repair_anomaly"``,
    ``"repair_gamma_delete"``, ``"none"``).  Extra data is carried in ``args``.
    """

    kind: str
    args: dict = field(default_factory=dict)


def decide_next_phase(state: VerifyRepairState) -> PhaseCommand:
    """Pure function: given the current state, return the next command.

    This is the single source of truth for the verify / repair pipeline.
    The caller never makes sequencing decisions — it always asks this
    function.
    """
    if state.phase in (VerifyPhase.COMPLETE, VerifyPhase.CANCELLED):
        return PhaseCommand("none")

    # ── initial: start anomaly check ──
    if state.phase == VerifyPhase.IDLE:
        return PhaseCommand("anomaly_check")

    # ── anomaly check done → start GAMMA check ──
    if state.phase == VerifyPhase.CHECKING_ANOMALY:
        return PhaseCommand("gamma_verify")

    # ── GAMMA presence check done → decide whether to scan ──
    if state.phase == VerifyPhase.CHECKING_GAMMA:
        if state.gamma_not_installed:
            return PhaseCommand(
                "complete",
                args={"ok": state.anomaly_clean, "message": "GAMMA not installed"},
            )
        if state.anomaly_clean and state.gamma_clean:
            # Still need MD5 scan for mod file integrity.
            return PhaseCommand("md5_scan")
        # Issues exist — still scan to build repair plan.
        return PhaseCommand("md5_scan")

    # ── MD5 scan done → look up records if problems found ──
    if state.phase == VerifyPhase.SCANNING_MD5:
        if state.scan_cancelled:
            return PhaseCommand(
                "complete",
                args={"ok": False, "message": "MD5 scan cancelled"},
            )
        if state.scan_clean and state.anomaly_clean and state.gamma_clean:
            return PhaseCommand(
                "complete",
                args={"ok": True, "message": "All checks passed"},
            )
        if state.scan is not None and state.scan.problems > 0:
            return PhaseCommand("fetch_records")
        # Presence issues but no scan problems → report without repair.
        return PhaseCommand(
            "complete",
            args={"ok": False, "message": "Issues found but not repairable via MD5"},
        )

    # ── records fetched (or skipped) → decide repair ──
    if state.phase == VerifyPhase.LOOKING_UP_RECORDS:
        needs_anomaly = state.anomaly_corrupt > 0 or state.anomaly_not_found > 0
        needs_gamma = state.plan is not None and state.plan.has_repairable
        if not needs_anomaly and not needs_gamma:
            return PhaseCommand(
                "complete",
                args={"ok": False, "message": "Issues found but not auto-repairable"},
            )
        return PhaseCommand(
            "prompt_repair",
            args={"anomaly": needs_anomaly, "gamma": needs_gamma},
        )

    # ── user accepted repair → advance pipeline ──
    if state.phase == VerifyPhase.PROMPTING_REPAIR:
        if state.repair_anomaly_pending:
            return PhaseCommand("repair_anomaly")
        if state.gamma_repair_pending:
            return PhaseCommand("repair_gamma_delete")
        # Both done → conclude.
        return PhaseCommand("conclude_after_repairs")

    # ── anomaly repair done → recheck, then move to gamma or conclude ──
    if state.phase == VerifyPhase.REPAIRING_ANOMALY:
        if not state.anomaly_recheck_done:
            return PhaseCommand("recheck_anomaly")
        # After recheck: advance to gamma repair or conclude.
        if state.gamma_repair_pending:
            return PhaseCommand("repair_gamma_delete")
        return PhaseCommand("conclude_after_repairs")

    # ── rechecking anomaly → back to repair flow ──
    if state.phase == VerifyPhase.RECHECKING_ANOMALY:
        return PhaseCommand("repair_anomaly")

    # ── gamma repair done → conclude ──
    if state.phase == VerifyPhase.REPAIRING_GAMMA:
        return PhaseCommand("conclude_after_repairs")

    return PhaseCommand("none")
