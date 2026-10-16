"""Integrity verification for Anomaly and the GAMMA install.

The Anomaly side is handled by the CLI's ``anomaly check`` command (hash
verification against ``anomaly/tools/checksums.md5``). The GAMMA side has no
CLI command, so it is verified here: every enabled mod in the profile's
``modlist.txt`` must have a non-empty folder under ``gamma/mods``, and the
core Mod Organizer files must be present.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .atomic import write_text
from .i18n import tr
from .modlist import entries, read_lines
from .network import read_response_bytes
from .network import urlopen_with_retry as urlopen

GAMMA_MARKERS = ("ModOrganizer.exe", "ModOrganizer.ini")
MANIFEST_FILENAME = "gamma-md5.txt"

_ANOMALY_STATUS_RE = re.compile(r"\|\s*(OK|CORRUPT|NOT FOUND)\s*$")
# Greedy path (callers strip it): the lazy "(.*?)\s*\|" form retried the
# whitespace run from every position - quadratic on a long blank line, and
# this runs on every line of every CLI log the Assistant reads.
_ANOMALY_LINE_RE = re.compile(r"^(?P<path>.*)\|\s*(?P<status>OK|CORRUPT|NOT FOUND)\s*$")

#: Files GAMMA's own install deliberately overwrites with patched
#: versions (its replacement engine executables, plus its own
#: fsgame.ltx) - confirmed against a real GAMMA install: every one of
#: these, and only these, mismatches ``anomaly check``'s baseline
#: (``anomaly/tools/checksums.md5``, which only ever knows vanilla
#: Anomaly's own hashes - it has no concept of "GAMMA-patched"). A
#: CORRUPT verdict on exactly one of these is expected, not a defect.
GAMMA_OVERLAY_FILES = frozenset(
    {
        "fsgame.ltx",
        "bin/anomalydx8.exe",
        "bin/anomalydx8avx.exe",
        "bin/anomalydx9.exe",
        "bin/anomalydx9avx.exe",
        "bin/anomalydx10.exe",
        "bin/anomalydx10avx.exe",
        "bin/anomalydx11.exe",
        "bin/anomalydx11avx.exe",
    }
)


def anomaly_status(line: str) -> str | None:
    """Extract the status from an ``anomaly check`` output line, if any."""
    match = _ANOMALY_STATUS_RE.search(line.strip())
    return match.group(1) if match else None


def is_expected_gamma_overlay_corrupt(line: str, anomaly_path: str) -> bool:
    """True if ``line`` is a CORRUPT verdict for one of GAMMA's own

    overlay files (see GAMMA_OVERLAY_FILES) - expected to mismatch
    ``anomaly check``'s vanilla-only baseline by design, not a real
    problem. False for OK/NOT FOUND on the same path: a missing overlay
    file means GAMMA's own overwrite never happened, which is a real
    problem worth flagging.
    """
    if not anomaly_path:
        return False
    match = _ANOMALY_LINE_RE.match(line.strip())
    if match is None or match.group("status") != "CORRUPT":
        return False
    root = str(Path(anomaly_path).expanduser()).replace("\\", "/").rstrip("/") + "/"
    path_text = match.group("path").strip().replace("\\", "/")
    if not path_text.lower().startswith(root.lower()):
        return False
    rel = path_text[len(root) :]
    return rel.lower() in GAMMA_OVERLAY_FILES


def is_gamma_overlay_corrupt_line(line: str) -> bool:
    """Like is_expected_gamma_overlay_corrupt, but without an anomaly root.

    Log dumps don't reliably carry the profile's Anomaly path, so this
    matches the overlay file by path suffix instead.
    """
    match = _ANOMALY_LINE_RE.match(line.strip())
    if match is None or match.group("status") != "CORRUPT":
        return False
    path = match.group("path").strip().replace("\\", "/").lower()
    return any(path.endswith("/" + rel) for rel in GAMMA_OVERLAY_FILES)


def format_size(num_bytes: int) -> str:
    """Render a byte count as a human-readable size (canonical implementation)."""
    if num_bytes <= 0:
        return "0 B"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


@dataclass
class GammaVerifyResult:
    """Result of a GAMMA folder integrity check."""

    marker_missing: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    ok_mods: int = 0
    disabled_mods: int = 0
    notes: list[str] = field(default_factory=list)
    mods_dir: str = ""
    official: list[str] = field(default_factory=list)
    official_missing: list[str] = field(default_factory=list)
    official_empty: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    extra_missing: list[str] = field(default_factory=list)
    extra_empty: list[str] = field(default_factory=list)
    separators: int = 0
    used_official_list: bool = False

    @property
    def problems(self) -> int:
        return len(self.marker_missing) + len(self.missing) + len(self.empty)

    def lines(self) -> list[str]:
        """The technical log for this check (plain English, pasteable)."""
        out: list[str] = [f"Not found: {name}" for name in self.marker_missing]
        if self.used_official_list:
            out.extend(
                self._section(
                    "GAMMA mods", self.official, self.official_missing, self.official_empty
                )
            )
            if self.extra or self.extra_missing or self.extra_empty:
                out.extend(
                    self._section(
                        "Your own mods (not part of GAMMA)",
                        self.extra,
                        self.extra_missing,
                        self.extra_empty,
                        list_present=True,
                    )
                )
        else:
            out.extend(self._section("Mods", [], self.missing, self.empty))
        out.append(self.summary)
        out.extend(self.notes)
        return out

    @staticmethod
    def _section(
        label: str,
        present: list[str],
        missing: list[str],
        empty: list[str],
        *,
        list_present: bool = False,
    ) -> list[str]:
        out = [f"{label}: {len(present)} present"] if present or list_present else []
        out.extend(f"  {name}" for name in present if list_present)
        out.extend(f"  Folder missing: {name}" for name in missing)
        out.extend(f"  Folder empty: {name}" for name in empty)
        return out

    @property
    def summary(self) -> str:
        parts = [f"{self.ok_mods} mods present"]
        if self.missing:
            parts.append(f"{len(self.missing)} folders missing")
        if self.empty:
            parts.append(f"{len(self.empty)} folders empty")
        parts.append(f"{self.disabled_mods} optional mods turned off")
        return "GAMMA: " + ", ".join(parts)


def fetch_official_mod_names(url: str, timeout: float = 10) -> set[str] | None:
    """Download the official GAMMA modlist and return its mod names.

    Returns ``None`` if the list cannot be fetched or parsed (the caller can
    then fall back to a single combined section).
    """
    try:
        with urlopen(url, timeout=timeout) as resp:
            text = read_response_bytes(resp, 32 * 1024 * 1024).decode(
                "utf-8", errors="replace"
            )
        return {name for _, name in entries(text.splitlines())}
    except (OSError, ValueError):
        # ValueError: malformed/unsupported URL, or a malformed +/- line in
        # the fetched list (entries() rejects those) - either way, callers
        # already treat None as "fall back to a single combined section".
        return None


def verify_gamma(
    gamma_dir: str,
    profile: str | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    official_mods: set[str] | None = None,
) -> GammaVerifyResult:
    """Check that ``gamma_dir`` looks like a complete GAMMA install.

    Verifies the core Mod Organizer files exist, the profile ``modlist.txt``
    is readable, and every enabled mod has a non-empty folder under ``mods``.
    ``on_progress`` (if given) is called as ``on_progress(done, total, name)``
    for each enabled mod as it is checked.

    When ``official_mods`` is given, enabled mods whose name is in that set
    are reported in the "official" section and any other enabled mods (except
    GAMMA category separators) are reported in the "extra mods on top"
    section below it.
    """
    result = GammaVerifyResult()
    result.used_official_list = official_mods is not None
    base = Path(gamma_dir)

    if not base.is_dir():
        result.marker_missing.append(f"(gamma directory not found: {gamma_dir})")
        return result

    result.mods_dir = str(base / "mods")
    for marker in GAMMA_MARKERS:
        if not (base / marker).is_file():
            result.marker_missing.append(marker)

    modlist_path: Path | None = None
    profiles_dir = base / "profiles"
    if profiles_dir.is_dir():
        candidates = [p for p in profiles_dir.iterdir() if p.is_dir()]
        match = None
        if profile:
            match = next(
                (p for p in candidates if p.name.upper() == profile.upper()),
                None,
            )
            if match is None:
                result.marker_missing.append(f"profiles/{profile}/modlist.txt")
                return result
        else:
            match = next(
                (p for p in candidates if (p / "modlist.txt").is_file()),
                None,
            )
        if match is not None and (match / "modlist.txt").is_file():
            modlist_path = match / "modlist.txt"
    if modlist_path is None:
        marker = (
            f"profiles/{profile}/modlist.txt"
            if profile
            else "profiles/G.A.M.M.A/modlist.txt"
        )
        result.marker_missing.append(marker)
        return result

    try:
        mod_pairs = entries(read_lines(modlist_path))
    except (OSError, ValueError) as exc:
        # ValueError covers both a non-UTF-8 modlist.txt (read_lines) and a
        # malformed +/- line (entries/_line_info) - neither is an OSError,
        # and this must degrade to the same reported-missing state as an
        # unreadable file rather than crash the integrity check.
        result.marker_missing.append(f"cannot read modlist.txt: {exc}")
        return result

    mods_dir = base / "mods"
    if not mods_dir.is_dir():
        result.marker_missing.append("mods/ (directory missing)")
        return result

    enabled = [name for status, name in mod_pairs if status == "Enabled"]
    # Category _separator headers are conventionally written disabled; count
    # them the same way the enabled loop below does (as separators, not
    # mods) so "N disabled" isn't inflated by every category header.
    result.disabled_mods = sum(
        1
        for status, name in mod_pairs
        if status == "Disabled" and not name.endswith("_separator")
    )

    total = len(enabled)
    for index, name in enumerate(enabled, start=1):
        if on_progress is not None:
            on_progress(index, total, name)
        if name.endswith("_separator"):
            result.separators += 1
            continue
        is_official = official_mods is not None and name in official_mods
        mod_path = mods_dir / name
        try:
            safe_mod_path = not mod_path.is_symlink() and mod_path.resolve().is_relative_to(
                mods_dir.resolve()
            )
        except (OSError, RuntimeError):
            safe_mod_path = False
        if not safe_mod_path or not mod_path.is_dir():
            result.missing.append(name)
            if is_official:
                result.official_missing.append(name)
            else:
                result.extra_missing.append(name)
        else:
            try:
                non_empty = any(mod_path.iterdir())
            except OSError:
                non_empty = False
            if non_empty:
                result.ok_mods += 1
                if is_official:
                    result.official.append(name)
                else:
                    result.extra.append(name)
            else:
                result.empty.append(name)
                if is_official:
                    result.official_empty.append(name)
                else:
                    result.extra_empty.append(name)

    return result


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def _md5_file(path: Path) -> tuple[str, int] | None:
    """Return (md5, size) for a file, or None if it cannot be read."""
    digest = hashlib.md5(usedforsecurity=False)
    try:
        # O_NONBLOCK: opening a FIFO left in mods/ would otherwise wait for
        # a writer forever, past any Cancel. Regular files ignore it.
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        with os.fdopen(fd, "rb") as f:
            size = 0
            for chunk in iter(lambda: f.read(1 << 16), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size
    except OSError:
        return None


@dataclass
class CacheArchiveVerifyResult:
    """Result of checking expected GAMMA download archives."""

    cache_dir: str = ""
    verified: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def problems(self) -> int:
        return len(self.missing) + len(self.mismatched) + len(self.unreadable)

    def lines(self) -> list[str]:
        if self.cancelled:
            return ["GAMMA cache verification cancelled"]
        lines = [
            (
                "Download cache: "
                f"{len(self.verified)} archives ready to reuse, "
                f"{len(self.missing)} not downloaded, "
                f"{len(self.mismatched)} older versions, "
                f"{len(self.unreadable)} unreadable"
            )
        ]
        if self.missing or self.mismatched:
            lines.append(
                "  Archives that are not downloaded or are an older version "
                "do not affect the installed game. They are downloaded "
                "automatically the next time an install or repair needs them."
            )
        for label, values in (
            ("Not downloaded", self.missing),
            ("Older version", self.mismatched),
            ("Unreadable", self.unreadable),
        ):
            if values:
                lines.append(f"  {label}: {', '.join(values)}")
        return lines


def verify_cache_archives(
    cache_dir: str,
    expected: Mapping[str, str],
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel=None,
) -> CacheArchiveVerifyResult:
    """Check cached archives against the current official MD5 mapping."""
    result = CacheArchiveVerifyResult(cache_dir=cache_dir)
    cache = Path(cache_dir)
    items = sorted((name, digest.lower()) for name, digest in expected.items())
    total = len(items)
    for index, (name, expected_digest) in enumerate(items, start=1):
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            break
        # Path(name).name alone does not reject ".." (Path("..").name == "..")
        # or ".", so a manifest entry of exactly ".." would otherwise resolve
        # `cache / name` to the cache directory's own parent.
        if not name or name in (".", "..") or Path(name).name != name:
            result.unreadable.append(name or "(empty archive name)")
            if on_progress is not None:
                on_progress(index, total, name)
            continue
        archive = cache / name
        if not archive.is_file() or archive.is_symlink():
            result.missing.append(name)
        elif not re.fullmatch(r"[0-9a-f]{32}", expected_digest):
            result.unreadable.append(name)
        else:
            actual = _md5_file(archive)
            if actual is None:
                result.unreadable.append(name)
            elif actual[0] == expected_digest:
                result.verified.append(name)
            else:
                result.mismatched.append(name)
        if on_progress is not None:
            on_progress(index, total, name)
    return result


def _read_manifest(path: Path) -> dict[str, str] | None:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        if len(line) < 34 or line[32:34] != "  ":
            return None
        digest = line[:32]
        relative_path = line[34:]
        if not re.fullmatch(r"[0-9a-fA-F]{32}", digest) or not relative_path:
            return None
        out[relative_path] = digest.lower()
    return out


def _write_manifest(path: Path, mapping: dict[str, str]) -> None:
    lines = [f"{mapping[rel]}  {rel}" for rel in sorted(mapping)]
    write_text(path, "\n".join(lines) + "\n")


def invalidate_baseline(gamma_dir: str) -> None:
    """Discard the MD5 baseline after a legitimate change to ``gamma/mods``.

    Anything that adds/changes/removes mod files outside Verify
    Integrity's own repair pipeline (a GAMMA update, a Fresh/GAMMA
    Reset, a Mod Manager install/delete) must call this - otherwise the
    next Verify Integrity run compares against a now-stale baseline and
    reports every legitimately-changed file as "corrupt". Re-establishing
    truth is Verify Integrity's own job (it already knows how to create a
    fresh baseline on a "missing manifest" run and tells the user to run
    it again to detect changes), so this deliberately just removes the
    file rather than trying to recompute it here.
    """
    try:
        (Path(gamma_dir) / MANIFEST_FILENAME).unlink(missing_ok=True)
    except OSError:
        pass


@dataclass
class Md5ScanResult:
    """Result of a full MD5 scan of ``gamma/mods`` against a baseline."""

    manifest_path: str = ""
    created: bool = False
    #: The baseline predated a GAMMA update and was replaced, not compared.
    stale: bool = False
    files_scanned: int = 0
    bytes_scanned: int = 0
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    elapsed: float = 0.0
    #: relpath -> md5 of every file hashed, so a caller can record exactly
    #: this state as the baseline without hashing the whole tree again.
    digests: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def problems(self) -> int:
        return (
            len(self.changed) + len(self.added) + len(self.removed) + len(self.errors)
        )

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.cancelled:
            out.append(f"GAMMA MD5 scan cancelled after {self.files_scanned} files")
            return out
        out.append(
            f"GAMMA MD5 scan: {self.files_scanned} files "
            f"({format_size(self.bytes_scanned)}) in "
            f"{_format_duration(self.elapsed)}"
        )
        if self.created:
            if self.stale:
                out.append(
                    "  GAMMA was updated since the last snapshot (e.g. from "
                    "the terminal), so the old snapshot was replaced"
                )
            out.append(
                f"  Snapshot saved to {Path(self.manifest_path).name}. This run "
                "had nothing to compare mod files against; later runs will "
                "report any file that changes from this snapshot"
            )
            return out
        if self.problems == 0:
            out.append("  Every mod file matches the last snapshot")
            return out
        for label, items in (
            ("Changed since snapshot", self.changed),
            ("New since snapshot", self.added),
            ("Deleted since snapshot", self.removed),
            ("Could not be read", self.errors),
        ):
            if not items:
                continue
            out.append(f"{label}: {len(items)}")
            out.extend(f"  {rel}" for rel in items)
        return out

    @property
    def summary(self) -> str:
        if self.created:
            if self.stale:
                return f"Mod file snapshot replaced after a GAMMA update ({self.files_scanned} files)"
            return f"Mod file snapshot saved ({self.files_scanned} files)"
        if self.cancelled:
            return "MD5 scan cancelled"
        if self.problems == 0:
            return f"MD5: {self.files_scanned} files match the snapshot"
        return f"MD5: {self.problems} file changes since the snapshot"


# ------------------------------------------------- plain-language results


@dataclass
class Finding:
    """One plain-language result row on the Verify Integrity card.

    ``level`` is "ok", "info", "warn" or "problem". Only "problem" means
    something is broken and a repair would help; "info" is never shown in
    an error colour.
    """

    level: str
    title: str
    detail: str = ""
    items: list[str] = field(default_factory=list)
    count: int = 0


_LEVEL_ORDER = {"ok": 0, "problem": 1, "warn": 2, "info": 3}


def _anomaly_paths(lines: Iterable[str], status: str, anomaly_root: str) -> list[str]:
    root = ""
    if anomaly_root:
        root = str(Path(anomaly_root).expanduser()).replace("\\", "/").rstrip("/") + "/"
    out: list[str] = []
    for line in lines:
        match = _ANOMALY_LINE_RE.match(line.strip())
        if match is None or match.group("status") != status:
            continue
        path = match.group("path").strip().replace("\\", "/")
        if root and path.lower().startswith(root.lower()):
            path = path[len(root) :]
        out.append(path)
    return out


def build_findings(
    *,
    anomaly_checked: bool,
    extra: Iterable[Finding] = (),
    anomaly_problem_lines: Iterable[str] = (),
    anomaly_root: str = "",
    presence: GammaVerifyResult | None = None,
    scan: Md5ScanResult | None = None,
    cache: CacheArchiveVerifyResult | None = None,
    plan=None,
    official_missing: bool = False,
    gamma_skipped: bool = False,
) -> list[Finding]:
    """Turn the raw check results into plain-language rows, worst first.

    Nothing here is checked again: this only explains what the Anomaly
    check, ``verify_gamma``, ``scan_mods_md5``, ``verify_cache_archives``
    and ``classify_problems`` (``plan``) already found.
    """
    found: list[Finding] = list(extra)

    def add(level, title, detail="", items=()):
        items = list(items)
        found.append(Finding(level, title, detail, items, len(items)))

    lines = list(anomaly_problem_lines)
    if not anomaly_checked:
        add(
            "problem",
            tr("Anomaly could not be checked"),
            tr("The Anomaly checker returned no results. Make sure the Anomaly folder in Step 3 is correct and Anomaly is installed. The technical log has the details."),
        )
    add_missing = _anomaly_paths(lines, "NOT FOUND", anomaly_root)
    if add_missing:
        add(
            "problem",
            tr("Anomaly files are missing"),
            tr("Repair re-installs Anomaly, then puts GAMMA's own files back on top."),
            add_missing,
        )
    differ = _anomaly_paths(lines, "CORRUPT", anomaly_root)
    if differ:
        add(
            "problem",
            tr("Anomaly files don't match the originals"),
            tr("These files are damaged or were replaced. Repair re-installs Anomaly, then puts GAMMA's own files back on top."),
            differ,
        )

    if gamma_skipped:
        add("info", tr("GAMMA isn't installed"), tr("Only Anomaly was checked."))

    broken: set[str] = set()
    official: set[str] = set()
    if presence is not None:
        broken = set(presence.missing) | set(presence.empty)
        official = (
            set(presence.official)
            | set(presence.official_missing)
            | set(presence.official_empty)
        )
        if presence.marker_missing:
            add(
                "problem",
                tr("GAMMA's core files weren't found"),
                tr("Mod Organizer or the GAMMA profile is missing from the GAMMA folder. Check the folder in Step 4, or run Install GAMMA."),
                presence.marker_missing,
            )
        gamma_broken = (
            presence.official_missing + presence.official_empty
            if presence.used_official_list
            else presence.missing + presence.empty
        )
        if gamma_broken:
            add(
                "problem",
                tr("GAMMA mods are missing or empty"),
                tr("Their mod folders are gone or have no files in them. Repair downloads them again."),
                gamma_broken,
            )
        own_broken = presence.extra_missing + presence.extra_empty
        if presence.used_official_list and own_broken:
            add(
                "warn",
                tr("Some of your own mods have no files"),
                tr("These mods aren't part of GAMMA, so COMMANDER can't download them again. Reinstall them yourself, or turn them off in the Mod Manager."),
                own_broken,
            )

    if plan is not None:
        changed = []
        for folder in plan.repairable:
            if folder in broken:
                continue
            edited, deleted = plan.file_counts.get(folder, (0, 0))
            parts = [tr("{count} changed", count=edited)] if edited else []
            if deleted:
                parts.append(tr("{count} deleted", count=deleted))
            changed.append(f"{folder} ({', '.join(parts)})" if parts else folder)
        if changed:
            add(
                "problem",
                tr("GAMMA mods changed since the last check"),
                tr("Files in these mods were changed or deleted since the snapshot. Repair downloads fresh copies. If you edited them on purpose, you don't need to repair."),
                changed,
            )
        stuck = [name for name in plan.unrepairable if name not in broken]
        no_source = [name for name in stuck if name in official]
        if no_source:
            add(
                "info",
                tr("Changed GAMMA mods with no download source"),
                tr("These ship with GAMMA itself rather than as a download, so there's no fresh copy to fetch. If you changed them on purpose (MCM values, for example), there's nothing to do. Otherwise, Install GAMMA restores them."),
                no_source,
            )
        own = [name for name in stuck if name not in official]
        if own:
            add(
                "info",
                tr("Changed files in your own mods"),
                tr("These aren't part of GAMMA, so there's no official copy to compare with. They're left exactly as they are."),
                own,
            )
        if plan.added_only:
            add(
                "info",
                tr("Mods with new files since the last check"),
                tr("Usually your own tweaks or files the game created. Nothing is removed."),
                plan.added_only,
            )

    if scan is not None:
        if scan.created:
            add(
                "info",
                tr("GAMMA was updated - new snapshot of your mod files saved")
                if scan.stale
                else tr("Snapshot of your mod files saved"),
                tr("This was the first check since your last install or change, so there was nothing to compare mod file contents against yet. Mod folders and Anomaly's files were checked against the originals. From now on, any change to a mod file is caught by the next check."),
            )
        unreadable = [rel for rel in scan.errors if rel not in scan.removed]
        if unreadable:
            add(
                "warn",
                tr("Some mod files couldn't be read"),
                tr("Usually a permissions problem. These files were skipped, not changed."),
                unreadable,
            )

    if official_missing and not gamma_skipped:
        add(
            "warn",
            tr("Couldn't download GAMMA's mod list"),
            tr("Mods were only checked for being present, and your own mods couldn't be told apart from GAMMA's. Check your internet connection and try again."),
        )

    if cache is not None and not cache.cancelled:
        if cache.missing:
            add(
                "info",
                tr("Archives not in the download cache"),
                tr("Your game is not affected. They're downloaded automatically the next time an install or repair needs them."),
                cache.missing,
            )
        if cache.mismatched:
            add(
                "info",
                tr("Older archive versions in the download cache"),
                tr("GAMMA's list has a newer version. Your game is not affected; the new one is downloaded when needed."),
                cache.mismatched,
            )
        if cache.unreadable:
            add(
                "warn",
                tr("Cached archives that couldn't be read"),
                tr("They'll be downloaded again when needed."),
                cache.unreadable,
            )

    if not any(f.level in ("problem", "warn") for f in found) and not (
        scan is not None and scan.created
    ):
        add(
            "ok",
            tr("Everything checks out"),
            tr("Anomaly's files match the originals.")
            if scan is None
            else tr("Anomaly's files match the originals and every GAMMA mod is in place.")
            if scan.changed or scan.removed or scan.added
            else tr("Anomaly's files match the originals, every GAMMA mod is in place, and no mod file changed since the last check."),
        )
    found.sort(key=lambda finding: _LEVEL_ORDER.get(finding.level, 9))
    return found


def overall_verdict(findings: Iterable[Finding], scan: Md5ScanResult | None) -> str:
    """"attention", "snapshot" or "healthy" for the card's status pill.

    A run that only saved the snapshot never counts as "healthy": it had
    nothing to compare mod file contents against.
    """
    if any(finding.level == "problem" for finding in findings):
        return "attention"
    if scan is not None and scan.created:
        return "snapshot"
    return "healthy"


#: Must match repair.py's _QUARANTINE_DIRNAME - not imported directly to
#: avoid a circular import (repair.py already imports from this module).
_QUARANTINE_DIRNAME = ".verify-quarantine"


def _iter_mod_files(mods: Path):
    """Yield safe mod files in stable order without materializing the tree.

    os.walk never descends into a symlinked folder and every symlinked file
    is skipped below, so what is yielded is inside ``mods`` - without a
    resolve() (an lstat per path component) for each of ~100k files.
    """
    for root, dirs, names in os.walk(mods):
        if root == str(mods) and _QUARANTINE_DIRNAME in dirs:
            # repair.py's own quarantine holding area, sitting directly
            # under mods/ - never real mod content, and if left behind by
            # a failed purge/restore it must never be hashed into the
            # baseline (that would bake orphaned duplicate files in, and
            # later cleanup would then look like mass "removed" files).
            dirs.remove(_QUARANTINE_DIRNAME)
        dirs.sort()
        names.sort()
        for name in names:
            candidate = Path(root) / name
            try:
                if candidate.is_symlink():
                    continue
            except OSError:
                continue
            yield candidate


def _updated_since(base: Path, manifest: Path) -> bool:
    """Whether a GAMMA install or update ran after *manifest* was written.

    Every ``full-install``/``update apply`` rewrites the CLI's modpack list
    in the MO2 profile. COMMANDER drops the baseline itself after the runs
    it starts (``invalidate_baseline``), but an update run from the
    terminal does not, and the old baseline then flags every updated or
    renumbered mod as broken.
    """
    try:
        written = manifest.stat().st_mtime
        for profile in (base / "profiles").iterdir():
            for name in ("modpack_maker_list.txt", "modpack_maker_list.json"):
                path = profile / name
                if path.is_file() and path.stat().st_mtime > written:
                    return True
    except OSError:
        return False
    return False


def scan_mods_md5(
    gamma_dir: str,
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel=None,
    rebaseline: bool = False,
    trust_baseline: bool = False,
) -> Md5ScanResult:
    """Hash every file under ``gamma/mods`` and compare against a baseline.

    The baseline is stored next to the install (``{gamma_dir}/gamma-md5.txt``).
    On the first run it is created and no comparison is made; on later runs
    every file is re-hashed and any changed, added, removed or unreadable file
    is reported. ``cancel`` (a ``threading.Event``) aborts the scan between
    files if set. ``rebaseline`` re-records the current state as the new
    baseline (used after a repair so fixed mods do not show as changed).
    ``trust_baseline`` compares against the baseline even when a GAMMA
    install ran after it: the repair's own ``full-install`` rewrites the
    modpack list, and its re-check must still compare, not start over.
    """
    result = Md5ScanResult()
    base = Path(gamma_dir)
    mods = base / "mods"
    manifest_path = base / MANIFEST_FILENAME
    result.manifest_path = str(manifest_path)

    if not mods.is_dir():
        result.errors.append("mods/ (directory missing)")
        return result

    started = time.monotonic()
    # Materialize once: walking and stat-ing a 100k-file tree twice doubles
    # the pre-scan latency on slow disks.
    files = list(_iter_mod_files(mods))
    total = len(files)

    current: dict[str, str] = {}
    bytes_total = 0
    for index, path in enumerate(files, start=1):
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            break
        rel = path.relative_to(base).as_posix()
        if "\n" in rel or "\r" in rel:
            # The baseline manifest is a plain "<md5>  <relpath>\n" text
            # file (one entry per line): a relative path carrying an
            # embedded newline/CR would split into extra lines on write,
            # either making _read_manifest reject the whole baseline as
            # corrupt or - worse - desyncing a digest from a truncated
            # path. repair.py's classify_problems() then derives the mod
            # folder to quarantine straight from these same relative
            # paths, so a desynced entry could point repair at the wrong
            # folder. Such filenames are legal on Linux and can arrive via
            # a third-party archive (mod_install.py only rejects symlinks
            # and path traversal, not control characters in entry names),
            # so this is reachable, not just hypothetical - report it as
            # unreadable rather than ever writing it into the baseline.
            result.errors.append(rel)
            continue
        digest = _md5_file(path)
        if digest is None:
            result.errors.append(rel)
        else:
            current[rel] = digest[0]
            bytes_total += digest[1]
        if on_progress is not None and (index % 5000 == 0 or index == total):
            on_progress(index, total, format_size(bytes_total))

    result.files_scanned = len(current)
    result.bytes_scanned = bytes_total
    result.digests = current
    result.elapsed = time.monotonic() - started

    if result.cancelled:
        return result
    if rebaseline:
        try:
            _write_manifest(manifest_path, current)
        except OSError as exc:
            result.errors.append(f"Failed to write manifest: {exc}")
        return result
    if (
        not trust_baseline
        and manifest_path.is_file()
        and _updated_since(base, manifest_path)
    ):
        result.stale = True
    if manifest_path.is_file() and not result.stale:
        baseline = _read_manifest(manifest_path)
        if baseline is None:
            result.errors.append(
                f"Snapshot file '{manifest_path.name}' is empty or unreadable"
            )
        else:
            for rel, digest in current.items():
                if rel in baseline:
                    if baseline[rel] != digest:
                        result.changed.append(rel)
                else:
                    result.added.append(rel)
            # A file that exists but couldn't be read is an error, not a
            # removal: counted as removed it made repair quarantine and
            # re-download the whole mod over one permission problem.
            result.removed = sorted(set(baseline) - set(current) - set(result.errors))
            result.changed.sort()
            result.added.sort()
    else:
        result.created = True
        try:
            _write_manifest(manifest_path, current)
        except OSError as exc:
            result.errors.append(f"Failed to write manifest: {exc}")

    return result


def write_baseline(gamma_dir: str, scan: Md5ScanResult) -> None:
    """Record a finished scan's hashes as the new baseline (raises OSError).

    The same file ``scan_mods_md5(..., rebaseline=True)`` writes, without
    hashing tens of GB a second time.
    """
    _write_manifest(Path(gamma_dir) / MANIFEST_FILENAME, scan.digests)


def reverted_gamma_overlay(anomaly_dir: str) -> list[str]:
    """GAMMA-patched Anomaly files that are currently back to vanilla.

    GAMMA replaces Anomaly's engine executables and fsgame.ltx (see
    GAMMA_OVERLAY_FILES). An Anomaly repair re-extracts vanilla Anomaly
    over them; if GAMMA's copies are not put back afterwards, the game
    starts with the wrong engine and crashes - while every file "verifies
    OK", because vanilla is exactly what ``anomaly check`` expects. So the
    test is the reverse: an overlay file whose hash equals Anomaly's own
    vanilla checksum has been reverted.
    """
    root = Path(anomaly_dir).expanduser()
    reverted: list[str] = []
    for key, (name, digest) in _vanilla_md5s(root).items():
        if key not in GAMMA_OVERLAY_FILES:
            continue
        result = _md5_file(root / name)
        if result is not None and result[0].lower() == digest:
            reverted.append(name)
    return reverted


def _vanilla_md5s(anomaly_root: Path) -> dict[str, tuple[str, str]]:
    """Anomaly's ``tools/checksums.md5``: lowercase path -> (path, md5)."""
    try:
        lines = (anomaly_root / "tools" / "checksums.md5").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return {}
    vanilla: dict[str, tuple[str, str]] = {}
    for line in lines:
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            name = parts[1].lstrip("*").replace("\\", "/")
            vanilla[name.lower()] = (name, parts[0].lower())
    return vanilla


#: Where GAMMA keeps the files it lays over Anomaly, inside the cached
#: ``Stalker_GAMMA`` git repository the CLI clones into the download cache.
_GAMMA_REPO = "Stalker_GAMMA.git"
_GAMMA_PATCH_ROOT = "G.A.M.M.A/modpack_patches/"


@dataclass
class OverlayRestore:
    restored: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    reason: str = ""


def _gamma_repo(cache_dir: str | Path) -> Path | None:
    repo = Path(cache_dir).expanduser() / _GAMMA_REPO
    return repo if (repo / "HEAD").is_file() and (repo / "objects").is_dir() else None


def restore_gamma_overlay(
    anomaly_dir: str | Path, cache_dir: str | Path, names: list[str]
) -> OverlayRestore:
    """Put GAMMA's own copies of reverted overlay files back.

    The source is the GAMMA repository already in the download cache (the
    same files ``full-install`` copies over Anomaly), read with ``git show``
    so nothing is downloaded. Each file is written atomically, and only
    after checking it is really GAMMA's: a Windows executable (for the
    ``.exe`` files) that differs from Anomaly's vanilla copy. Files the
    repository does not carry (``fsgame.ltx`` is written by the installer,
    not copied) are reported as failed, never guessed.
    """
    import shutil
    import subprocess

    from .atomic import write_bytes
    from .config import child_environment

    result = OverlayRestore()
    if not names:
        return result
    git = shutil.which("git")
    repo = _gamma_repo(cache_dir)
    if git is None or repo is None:
        result.failed = list(names)
        result.reason = (
            "git is not installed" if git is None else f"no {_GAMMA_REPO} in the download cache"
        )
        return result
    env = child_environment()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        listing = subprocess.run(
            [git, "-c", f"safe.directory={repo}", "--git-dir", str(repo),
             "ls-tree", "-r", "--name-only", "HEAD", "--", _GAMMA_PATCH_ROOT],
            capture_output=True, text=True, timeout=60, env=env, check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        result.failed = list(names)
        result.reason = f"could not read {_GAMMA_REPO}: {exc}"
        return result
    by_name = {
        item[len(_GAMMA_PATCH_ROOT):].lower(): item
        for item in listing
        if item.startswith(_GAMMA_PATCH_ROOT)
    }
    root = Path(anomaly_dir).expanduser()
    vanilla = {key: digest for key, (_name, digest) in _vanilla_md5s(root).items()}
    for name in names:
        key = name.replace("\\", "/").lower()
        source = by_name.get(key)
        if key not in GAMMA_OVERLAY_FILES or source is None:
            result.failed.append(name)
            continue
        try:
            data = subprocess.run(
                [git, "-c", f"safe.directory={repo}", "--git-dir", str(repo),
                 "show", f"HEAD:{source}"],
                capture_output=True, timeout=120, env=env, check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            result.failed.append(name)
            continue
        if (key.endswith(".exe") and not data.startswith(b"MZ")) or (
            hashlib.md5(data, usedforsecurity=False).hexdigest() == vanilla.get(key)
        ):
            result.failed.append(name)
            continue
        try:
            # Keeps the replaced file's permissions (an executable stays one).
            write_bytes(root / name, data, lock=False)
        except OSError:
            result.failed.append(name)
            continue
        result.restored.append(name)
    return result
