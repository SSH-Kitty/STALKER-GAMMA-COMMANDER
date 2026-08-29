"""Read COMMANDER log-dump zip archives.

A dump is a zip created by GAMMA COMMANDER's "Create Log Dump" action with
entries grouped under source prefixes (``commander/``, ``anomaly/``,
``gamma/``, ``wine-prefix/``, ``report/``) plus a ``MANIFEST.txt``.
"""

from __future__ import annotations

import os
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

#: ANSI colour escapes appear throughout Wine/Proton output; strip them once
#: at read time so titles, excerpts and matching stay clean.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

#: Friendly names for the top-level folders inside a dump.
SOURCE_LABELS: dict[str, str] = {
    "commander": "COMMANDER",
    "anomaly": "Anomaly",
    "gamma": "GAMMA / MO2",
    "wine-prefix": "Wine prefix",
    "report": "Report",
    "other": "Other files",
}

#: Suffixes that are decoded and scanned. Everything else is listed but
#: treated as binary.
TEXT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".log",
        ".txt",
        ".json",
        ".ini",
        ".cfg",
        ".ltx",
        ".md",
        ".xml",
        ".bat",
        ".crash",
    }
)

#: Refuse to decode absurdly large entries into memory.
MAX_TEXT_BYTES = 32_000_000
#: Bound archive metadata and aggregate text allocations as well as entries.
MAX_ENTRIES = 10_000
MAX_TOTAL_TEXT_BYTES = 256_000_000

_ZIP_READ_ERRORS = (
    OSError,
    EOFError,
    KeyError,
    RuntimeError,
    ValueError,
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
)


class DumpError(ValueError):
    """Raised when a file cannot be opened as a log-dump archive."""


def default_dumps_dir() -> Path:
    """Where COMMANDER stores log dumps on this machine."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base:
        base = os.path.join(Path.home(), ".config")
    base = Path(base) / "stalker-gamma"
    return base / "logs" / "dumps"


def _mtime_of(path: Path) -> float:
    """Modification time, or ``0.0`` when the file vanished mid-listing."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def recent_dumps(limit: int = 10) -> list[Path]:
    """Newest dump archives in the default dumps folder.

    Files deleted between listing and sorting are skipped instead of
    raising.
    """
    directory = default_dumps_dir()
    if not directory.is_dir():
        return []
    entries: list[tuple[float, str]] = []
    try:
        candidates = directory.glob("*.zip")
        for candidate in candidates:
            try:
                if candidate.is_file():
                    entries.append((_mtime_of(candidate), str(candidate)))
            except OSError:
                continue
    except OSError:
        return []
    entries.sort(key=lambda entry: entry[0], reverse=True)
    return [Path(name) for _mtime, name in entries[:limit]]


def source_of(arcname: str) -> str:
    """Top-level group for an archive path (``commander/...`` → ``commander``)."""
    first = arcname.split("/", 1)[0]
    return first if first in SOURCE_LABELS else "other"


@dataclass
class DumpFile:
    """One entry inside the archive."""

    arcname: str
    source: str
    short_name: str
    size: int
    is_text: bool
    lines: list[str] = field(default_factory=list)
    text: str | None = None


@dataclass
class DumpArchive:
    """An opened log-dump archive with decoded text files ready to scan."""

    path: Path
    size: int
    files: list[DumpFile] = field(default_factory=list)
    manifest_text: str | None = None

    @classmethod
    def open(cls, path: str | Path) -> DumpArchive:
        """Open *path*, decoding every text entry. Raises :class:`DumpError`."""
        resolved = Path(path)
        if not resolved.is_file():
            raise DumpError(f"File not found: {resolved}")
        try:
            zf = zipfile.ZipFile(resolved)
        except _ZIP_READ_ERRORS as exc:
            raise DumpError(f"{resolved.name} is not a readable zip archive.") from exc
        try:
            archive = cls(path=resolved, size=resolved.stat().st_size)
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                raise DumpError(
                    f"{resolved.name} contains too many archive entries."
                )
            declared_text_bytes = 0
            decoded_text_bytes = 0
            for info in infos:
                if info.is_dir():
                    continue
                arcname = _safe_name(info.filename)
                if arcname is None:
                    continue
                suffix = Path(arcname).suffix.lower()
                is_text = (
                    suffix in TEXT_SUFFIXES
                    or not suffix  # extensionless files like MANIFEST
                ) and info.file_size <= MAX_TEXT_BYTES
                is_text_candidate = suffix in TEXT_SUFFIXES or not suffix
                if is_text_candidate:
                    declared_text_bytes += info.file_size
                    if declared_text_bytes > MAX_TOTAL_TEXT_BYTES:
                        raise DumpError(
                            f"{resolved.name} contains too much declared text."
                        )
                entry = DumpFile(
                    arcname=arcname,
                    source=source_of(arcname),
                    short_name=arcname.split("/", 1)[-1],
                    size=info.file_size,
                    is_text=is_text,
                )
                if is_text:
                    try:
                        raw = zf.read(info)
                    except _ZIP_READ_ERRORS as exc:
                        raise DumpError(
                            f"Could not read {resolved.name}: {exc}"
                        ) from exc
                    decoded_text_bytes += len(raw)
                    if decoded_text_bytes > MAX_TOTAL_TEXT_BYTES:
                        raise DumpError(
                            f"{resolved.name} contains too much decoded text."
                        )
                    entry.text = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace"))
                    entry.lines = entry.text.splitlines()
                if arcname == "MANIFEST.txt":
                    archive.manifest_text = entry.text
                archive.files.append(entry)
            archive.files.sort(key=lambda f: f.arcname)
            return archive
        except DumpError:
            raise
        except _ZIP_READ_ERRORS as exc:
            raise DumpError(f"Could not read {resolved.name}: {exc}") from exc
        finally:
            zf.close()

    def looks_like_dump(self) -> bool:
        """True when the layout matches COMMANDER's dump format."""
        if self.manifest_text is not None:
            return True
        return any(f.source != "other" for f in self.files)

    def file_by_arcname(self, arcname: str) -> DumpFile | None:
        for entry in self.files:
            if entry.arcname == arcname:
                return entry
        return None


def _safe_name(filename: str) -> str | None:
    """Normalise safe separators; ``None`` for unsafe archive paths."""
    name = filename.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def human_size(num_bytes: int) -> str:
    """Compact byte size for display."""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
