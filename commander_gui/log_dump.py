"""Create a timestamped zip archive of diagnostic logs for troubleshooting.

Gathers COMMANDER, Anomaly, GAMMA/MO2 and Wine-prefix logs plus crash dumps
into ``~/.config/stalker-gamma/logs/dumps/commander-log-dump-YYYYMMDD-HHMM.zip``
and shows the exact path to the user when finished.
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .config import logs_dir

#: Files larger than this are listed in the manifest but not archived.
MAX_FILE_BYTES = 64_000_000
#: Once collected bytes exceed this the walk stops adding files.
MAX_TOTAL_BYTES = 256_000_000
#: Maximum directory recursion depth below each source root.
MAX_DEPTH = 8

_ARCHIVE_STEM = "commander-log-dump"

_EXTENSIONS = {".log", ".dmp", ".mdmp", ".crash"}
_CRASH_MARKERS = ("crash", "dump", "backtrace", "stacktrace")
#: Binaries that can carry marker words in their names (e.g. .NET's
#: System.Diagnostics.StackTrace.dll) but are never diagnostics.
_BINARY_EXTENSIONS = frozenset(
    {
        ".dll",
        ".exe",
        ".so",
        ".pak",
        ".dds",
        ".thm",
        ".oggs",
        ".ogg",
        ".wav",
        ".7z",
        ".zip",
        ".rar",
    }
)
#: Asset-heavy trees that never contain diagnostics worth archiving.
_SKIP_DIRS = frozenset(
    {
        "mods",
        "downloads",
        "cache",
        "textures",
        "meshes",
        "sounds",
        "scripts",
        "gamedata",
        "db",
        ".git",
        "__pycache__",
        "node_modules",
        "licenses",
        "dumps",
    }
)


def log_dumps_dir() -> Path:
    """Directory where log-dump archives are stored."""
    return logs_dir() / "dumps"


def archive_name(now: datetime | None = None) -> str:
    """Timestamped archive filename: commander-log-dump-YYYYMMDD-HHMM.zip."""
    stamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M")
    return f"{_ARCHIVE_STEM}-{stamp}.zip"


def _is_candidate(path: Path) -> bool:
    """True for files worth archiving: log/crash extensions or crash-y names."""
    if path.suffix.lower() in _EXTENSIONS:
        return True
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return False
    lowered = path.name.lower()
    return any(marker in lowered for marker in _CRASH_MARKERS)


def _safe_archive_part(value: str) -> str:
    """Return one safe ZIP path component, rejecting traversal input."""
    normalized = value.replace("\\", "/")
    part = normalized.strip("/")
    if (
        not part
        or normalized != part
        or part in {".", ".."}
        or "/" in part
        or ".." in part
    ):
        raise ValueError(f"Unsafe archive path component: {value!r}")
    return part


def _walk_candidates(root: Path) -> list[Path]:
    """Bounded-depth, symlink-safe collection of candidate files under *root*."""
    if not root.is_dir():
        return []
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name.lower() in _SKIP_DIRS:
                        continue
                    if depth < MAX_DEPTH:
                        stack.append((entry, depth + 1))
                    continue
                if entry.is_file() and _is_candidate(entry):
                    found.append(entry)
            except OSError:
                continue
    return found


def _unique_path(directory: Path, name: str) -> Path:
    """Return *name* in *directory*, suffixing -2, -3... on collisions."""
    candidate = directory / name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def build_log_dump(
    dest_dir: Path,
    sources: dict[str, str | Path],
    extra_texts: dict[str, str] | None = None,
    now: datetime | None = None,
    per_file_cap: int = MAX_FILE_BYTES,
    total_cap: int = MAX_TOTAL_BYTES,
) -> tuple[Path, dict[str, object]]:
    """Archive candidate logs from *sources* into a timestamped zip.

    Returns ``(archive_path, stats)`` where stats reports included files,
    skipped files and total archived bytes for display in the UI.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_path(dest_dir, archive_name(now))
    manifest_lines: list[str] = [
        "COMMANDER log dump",
        f"Created: {(now or datetime.now().astimezone()).strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    included = 0
    skipped: list[str] = []
    total_bytes = 0
    temp_fd, temp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
    os.close(temp_fd)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for label in sorted(sources):
                safe_label = _safe_archive_part(label)
                root = Path(sources[label]).expanduser()
                if not root.is_dir():
                    manifest_lines.append(f"[missing] {label}: {root}")
                    continue
                for file_path in _walk_candidates(root):
                    try:
                        size = file_path.stat().st_size
                    except OSError:
                        continue
                    if size > per_file_cap:
                        skipped.append(f"{safe_label}/{file_path.name} ({size} bytes)")
                        continue
                    if total_bytes + size > total_cap:
                        skipped.append(f"{safe_label}/{file_path.name} (total cap)")
                        continue
                    relative = file_path.relative_to(root)
                    arcname = f"{safe_label}/{relative.as_posix()}"
                    try:
                        if file_path.suffix.lower() in {".log", ".crash"}:
                            from .diagnostics import _redact

                            content = file_path.read_bytes().decode(
                                "utf-8", errors="replace"
                            )
                            zf.writestr(arcname, _redact(content))
                        else:
                            zf.write(file_path, arcname)
                    except OSError:
                        continue
                    included += 1
                    total_bytes += size
                    manifest_lines.append(f"[ok] {arcname} ({size} bytes)")
            for name, text in sorted((extra_texts or {}).items()):
                safe_name = _safe_archive_part(name)
                from .diagnostics import _redact

                zf.writestr(f"report/{safe_name}", _redact(text))
                manifest_lines.append(f"[ok] report/{safe_name}")
                included += 1
            if skipped:
                manifest_lines.append("")
                manifest_lines.append("Skipped:")
                manifest_lines.extend(f"- {entry}" for entry in skipped)
            zf.writestr("MANIFEST.txt", "\n".join(manifest_lines) + "\n")
        Path(temp_path).replace(target)
    except BaseException:
        Path(temp_path).unlink(missing_ok=True)
        raise
    stats: dict[str, object] = {
        "files": included,
        "skipped": len(skipped),
        "bytes": total_bytes,
    }
    return target, stats


def gather_default_sources() -> dict[str, str | Path]:
    """Resolve the standard log locations from the active configuration."""
    from . import gui_settings
    from .settings import load_settings

    sources: dict[str, str | Path] = {"commander": logs_dir()}
    profile = load_settings().active_profile
    if profile is not None:
        if profile.anomaly:
            sources["anomaly"] = profile.anomaly
        if profile.gamma:
            sources["gamma"] = profile.gamma
    prefix = gui_settings.configured_wine_prefix()
    if prefix:
        sources["wine-prefix"] = prefix
    return sources


def create_log_dump(
    report: Callable[[str], None] | None = None,
) -> tuple[Path, dict[str, object]]:
    """Entry point for the UI: build the archive from live configuration."""
    from .diagnostics import collect_diagnostics

    say = report or (lambda _message: None)
    sources = gather_default_sources()
    for label, root in sorted(sources.items()):
        say(f"Scanning {label}: {root}")
    say("Compressing logs...")
    return build_log_dump(
        log_dumps_dir(),
        sources,
        extra_texts={"diagnostics.txt": collect_diagnostics()},
    )
