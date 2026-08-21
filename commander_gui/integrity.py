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
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .atomic import write_text
from .modlist import entries, read_lines
from .network import read_response_bytes

GAMMA_MARKERS = ("ModOrganizer.exe", "ModOrganizer.ini")
MANIFEST_FILENAME = "gamma-md5.txt"

_ANOMALY_STATUS_RE = re.compile(r"\|\s*(OK|CORRUPT|NOT FOUND)\s*$")


def anomaly_status(line: str) -> str | None:
    """Extract the status from an ``anomaly check`` output line, if any."""
    match = _ANOMALY_STATUS_RE.search(line.strip())
    return match.group(1) if match else None


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
        out: list[str] = []
        for name in self.marker_missing:
            out.append(f"MISSING  {name}")
        if self.used_official_list:
            out.append(
                self._section_line(
                    "Official GAMMA mods",
                    self.official,
                    self.official_missing,
                    self.official_empty,
                )
            )
            for name in self.official_missing:
                out.append(f"  MISSING  {name}")
            for name in self.official_empty:
                out.append(f"  EMPTY    {name}")
            out.append(
                self._section_line(
                    "Extra Mods",
                    self.extra,
                    self.extra_missing,
                    self.extra_empty,
                )
            )
            for name in self.extra:
                out.append(f"  EXTRA  {name}")
            for name in self.extra_missing:
                out.append(f"  MISSING  {name}")
            for name in self.extra_empty:
                out.append(f"  EMPTY    {name}")
        else:
            for name in self.missing:
                out.append(f"MISSING  mods/{name}")
            for name in self.empty:
                out.append(f"EMPTY    mods/{name}")
            if self.problems == 0:
                out.append("GAMMA: all enabled mods are present and non-empty")
        out.append(self.summary)
        out.extend(self.notes)
        return out

    @staticmethod
    def _section_line(
        label: str, ok: list[str], missing: list[str], empty: list[str]
    ) -> str:
        if not ok and not missing and not empty:
            return f"{label}: 0"
        text = f"{label}: {len(ok)} verified"
        if missing:
            text += f", {len(missing)} missing"
        if empty:
            text += f", {len(empty)} empty"
        return text

    @property
    def summary(self) -> str:
        parts = [f"{self.ok_mods} mod(s) OK"]
        if self.missing:
            parts.append(f"{len(self.missing)} missing")
        if self.empty:
            parts.append(f"{len(self.empty)} empty")
        parts.append(f"{self.disabled_mods} disabled")
        return "GAMMA: " + ", ".join(parts)


def fetch_official_mod_names(url: str, timeout: float = 10) -> set[str] | None:
    """Download the official GAMMA modlist and return its mod names.

    Returns ``None`` if the list cannot be fetched or parsed (the caller can
    then fall back to a single combined section).
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            text = read_response_bytes(resp, 32 * 1024 * 1024).decode(
                "utf-8", errors="replace"
            )
    except (OSError, ValueError):  # ValueError: malformed/unsupported URL
        return None
    return {name for _, name in entries(text.splitlines())}


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
    except OSError as exc:
        result.marker_missing.append(f"cannot read modlist.txt: {exc}")
        return result

    mods_dir = base / "mods"
    if not mods_dir.is_dir():
        result.marker_missing.append("mods/ (directory missing)")
        return result

    enabled = [name for status, name in mod_pairs if status == "Enabled"]
    result.disabled_mods = sum(1 for status, _ in mod_pairs if status == "Disabled")

    total = len(enabled)
    for index, name in enumerate(enabled, start=1):
        if on_progress is not None:
            on_progress(index, total, name)
        if name.endswith("_separator"):
            result.separators += 1
            continue
        is_official = official_mods is not None and name in official_mods
        mod_path = mods_dir / name
        if not mod_path.is_dir():
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
    digest = hashlib.md5()
    try:
        with path.open("rb") as f:
            size = 0
            for chunk in iter(lambda: f.read(1 << 16), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size
    except OSError:
        return None


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


@dataclass
class Md5ScanResult:
    """Result of a full MD5 scan of ``gamma/mods`` against a baseline."""

    manifest_path: str = ""
    created: bool = False
    files_scanned: int = 0
    bytes_scanned: int = 0
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    elapsed: float = 0.0

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
            out.append(
                f"  baseline saved to {Path(self.manifest_path).name} - "
                "run Verify Integrity again to detect changes"
            )
            return out
        if self.problems == 0:
            out.append("All mod files unchanged since baseline")
            return out
        for label, items in (
            ("Changed", self.changed),
            ("Added", self.added),
            ("Removed", self.removed),
        ):
            if not items:
                continue
            out.append(f"{label}: {len(items)}")
            for rel in items:
                out.append(f"  {rel}")
        if self.errors:
            out.append(f"Errors (unreadable): {len(self.errors)}")
            for rel in self.errors:
                out.append(f"  {rel}")
        return out

    @property
    def summary(self) -> str:
        if self.created:
            return f"MD5 baseline created ({self.files_scanned} files)"
        if self.cancelled:
            return "MD5 scan cancelled"
        if self.problems == 0:
            return f"MD5: {self.files_scanned} files unchanged"
        return f"MD5: {self.problems} change(s) since baseline"


def _iter_mod_files(mods: Path):
    """Yield safe mod files in stable order without materializing the tree."""
    root_path = mods.resolve()
    for root, dirs, names in os.walk(mods):
        dirs.sort()
        names.sort()
        for name in names:
            candidate = Path(root) / name
            try:
                if candidate.is_symlink() or not candidate.resolve().is_relative_to(
                    root_path
                ):
                    continue
            except (OSError, RuntimeError):
                continue
            yield candidate


def scan_mods_md5(
    gamma_dir: str,
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel=None,
    rebaseline: bool = False,
) -> Md5ScanResult:
    """Hash every file under ``gamma/mods`` and compare against a baseline.

    The baseline is stored next to the install (``{gamma_dir}/gamma-md5.txt``).
    On the first run it is created and no comparison is made; on later runs
    every file is re-hashed and any changed, added, removed or unreadable file
    is reported. ``cancel`` (a ``threading.Event``) aborts the scan between
    files if set. ``rebaseline`` re-records the current state as the new
    baseline (used after a repair so fixed mods do not show as changed).
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
    total = sum(1 for _ in _iter_mod_files(mods))

    current: dict[str, str] = {}
    bytes_total = 0
    for index, path in enumerate(_iter_mod_files(mods), start=1):
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            break
        digest = _md5_file(path)
        rel = path.relative_to(base).as_posix()
        if digest is None:
            result.errors.append(rel)
        else:
            current[rel] = digest[0]
            bytes_total += digest[1]
        if on_progress is not None and (index % 5000 == 0 or index == total):
            on_progress(index, total, format_size(bytes_total))

    result.files_scanned = len(current)
    result.bytes_scanned = bytes_total
    result.elapsed = time.monotonic() - started

    if result.cancelled:
        return result
    if rebaseline:
        try:
            _write_manifest(manifest_path, current)
        except OSError as exc:
            result.errors.append(f"Failed to write manifest: {exc}")
        return result
    if manifest_path.is_file():
        baseline = _read_manifest(manifest_path)
        if baseline is None:
            result.errors.append(
                f"Baseline manifest '{manifest_path.name}' is empty or corrupt"
            )
        else:
            for rel, digest in current.items():
                if rel in baseline:
                    if baseline[rel] != digest:
                        result.changed.append(rel)
                else:
                    result.added.append(rel)
            result.removed = sorted(set(baseline) - set(current))
            result.changed.sort()
            result.added.sort()
    else:
        result.created = True
        try:
            _write_manifest(manifest_path, current)
        except OSError as exc:
            result.errors.append(f"Failed to write manifest: {exc}")

    return result
