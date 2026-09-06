"""Safe local archive installation for MO2-style GAMMA mods."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from threading import Event

from .config import cli_binary_path


class ModInstallError(RuntimeError):
    """Raised when an archive cannot be safely installed."""


def find_archiver() -> Path:
    """Locate the bundled 7zz helper, falling back to a system 7-Zip."""
    bundled = cli_binary_path().parent / "resources" / "7zz"
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return bundled
    for name in ("7zz", "7z", "7za"):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise ModInstallError("The bundled 7zz archive helper could not be found")


def sanitize_name(name: str) -> str:
    """Return a safe single directory name for an installed mod."""
    if not isinstance(name, str) or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ModInstallError("Mod names cannot contain control characters")
    cleaned = re.sub(r"[\\/:]+", " ", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or cleaned in {".", ".."}:
        raise ModInstallError("The archive does not have a usable mod name")
    return cleaned[:180]


def default_mod_name(archive: Path) -> str:
    """Use the archive filename as the initial MO2 mod name."""
    name = archive.name
    for suffix in (".tar.gz", ".tar.xz", ".tar.bz2", ".zip", ".7z", ".rar", ".fomod"):
        if name.casefold().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return sanitize_name(name)


def _validate_tree(root: Path) -> None:
    # os.walk(followlinks=False) never descends into a symlinked directory,
    # unlike Path.rglob() which follows them while walking - a symlink loop
    # (or a link to a huge unrelated tree) inside a malicious archive could
    # otherwise make this scan hang or run away before ever reaching the
    # is_symlink() check below.
    resolved_root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in (*dirnames, *filenames):
            path = current / name
            if path.is_symlink():
                raise ModInstallError("Archive contains an unsupported symlink")
            try:
                path.resolve().relative_to(resolved_root)
            except ValueError as exc:
                raise ModInstallError(
                    "Archive contains a path outside its staging folder"
                ) from exc


def _list_archive_paths(archiver: Path, archive: Path) -> list[str]:
    """Return every member path the archiver reports for ``archive``."""
    try:
        result = subprocess.run(
            [str(archiver), "l", "-slt", str(archive)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ModInstallError(f"Could not list archive contents: {exc}") from exc
    if result.returncode != 0:
        detail = "\n".join(result.stdout.splitlines()[-8:])
        raise ModInstallError(
            f"Could not list archive contents (exit code {result.returncode})\n{detail}"
        )
    paths: list[str] = []
    for block in result.stdout.split("\n\n"):
        # The archive-level info block (before the per-entry list starts)
        # also has a "Path = " line - for the archive file itself - but is
        # the only block carrying this field, so it's how we skip it.
        if "Physical Size =" in block:
            continue
        for line in block.splitlines():
            if line.startswith("Path = "):
                paths.append(line[len("Path = ") :])
                break
    return paths


def _validate_archive_entries(archiver: Path, archive: Path) -> None:
    """Reject an archive containing an absolute or ``..``-escaping entry.

    ``_validate_tree()`` below only inspects what actually landed inside
    ``staging`` after extraction - a member the extractor wrote *outside*
    staging (e.g. via a ``../`` path) would never be visited by that walk.
    Listing entries first and rejecting anything that looks like a
    path-traversal attempt closes that gap without depending on the
    external 7-Zip binary refusing such paths on its own.
    """
    for entry in _list_archive_paths(archiver, archive):
        normalized = entry.replace("\\", "/")
        first_segment = normalized.split("/", 1)[0]
        if normalized.startswith("/") or ":" in first_segment:
            raise ModInstallError("Archive contains an absolute path entry")
        if ".." in normalized.split("/"):
            raise ModInstallError("Archive contains a path-traversal entry")


def extract_archive(
    archive: Path,
    staging: Path,
    cancel_event: Event | None = None,
    progress=None,
) -> None:
    """Extract an archive with bundled 7zz into ``staging``."""
    if not archive.is_file():
        raise ModInstallError(f"Archive not found: {archive}")
    if cancel_event is not None and cancel_event.is_set():
        raise ModInstallError("Mod installation cancelled")
    archiver = find_archiver()
    _validate_archive_entries(archiver, archive)
    if cancel_event is not None and cancel_event.is_set():
        raise ModInstallError("Mod installation cancelled")
    staging.mkdir(parents=True, exist_ok=False)
    command = [str(archiver), "x", str(archive), f"-o{staging}", "-y", "-bsp1"]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except (OSError, ValueError) as exc:
        raise ModInstallError(f"Could not start archive extractor: {exc}") from exc
    try:
        output: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            clean = line.strip()
            if clean:
                output.append(clean)
                if progress is not None:
                    match = re.search(r"(\d{1,3})%", clean)
                    progress(int(match.group(1)) if match else None, clean)
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise ModInstallError("Mod installation cancelled")
        process.stdout.close()
        rc = process.wait()
        if rc != 0:
            detail = "\n".join(output[-8:])
            raise ModInstallError(
                f"Archive extraction failed (exit code {rc})\n{detail}"
            )
        _validate_tree(staging)
    except Exception:
        if process.poll() is None:
            try:
                process.kill()
                process.wait()
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise


def payload_root(staging: Path) -> Path:
    """Return the archive payload, unwrapping one harmless root directory."""
    children = list(staging.iterdir())
    if len(children) == 1 and children[0].is_dir() and children[0].name != "fomod":
        return children[0]
    return staging


def move_payload(
    staging: Path, destination: Path, cancel_event: Event | None = None
) -> None:
    """Move staged files into a new mod directory without overwriting anything."""
    if destination.is_symlink() or destination.exists():
        raise ModInstallError(f"Mod directory already exists: {destination.name}")
    if destination.parent.is_symlink():
        raise ModInstallError("Mod directory parent cannot be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = payload_root(staging)
    if not any(payload.iterdir()):
        raise ModInstallError("The archive contains no installable files")
    destination.mkdir()
    try:
        for child in list(payload.iterdir()):
            if cancel_event is not None and cancel_event.is_set():
                raise ModInstallError("Mod installation cancelled")
            shutil.move(str(child), destination / child.name)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def install_archive(
    archive: Path,
    mods_dir: Path,
    name: str | None = None,
    cancel_event: Event | None = None,
    progress=None,
) -> str:
    """Extract and install one local archive, returning its MO2 folder name."""
    mod_name = sanitize_name(name or default_mod_name(archive))
    mods_dir = mods_dir.resolve()
    mods_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gamma-mod-") as temp:
        staging = Path(temp) / "payload"
        extract_archive(archive, staging, cancel_event, progress)
        move_payload(staging, mods_dir / mod_name)
    return mod_name
