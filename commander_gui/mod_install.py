"""Safe local archive installation for MO2-style GAMMA mods."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from threading import Event, Thread

from . import atomic
from .config import cli_binary_path

#: Dropped into a mod folder for the duration of move_payload() and removed
#: on success. A folder killed/crashed mid-move (the in-loop rollback below
#: only runs for a raised Python exception, not a hard kill) still carries
#: this afterwards, marking it as an incomplete install rather than a
#: genuinely finished one.
INSTALLING_MARKER = ".commander-installing"


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
            # -p with no password: an encrypted archive fails instead of
            # waiting for a password on a terminal nobody is looking at.
            [str(archiver), "l", "-slt", "-p", str(archive)],
            stdin=subprocess.DEVNULL,
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
    # 7-Zip prints the archive's own header first (its "Path = " is the
    # archive file itself, an absolute path) and then a "----------" line
    # before the per-entry blocks. Only what follows that line is entries.
    # Keying off "Physical Size =" instead missed formats whose header has
    # no such field (gzip), so every .tar.gz mod was refused as having an
    # "absolute path entry".
    lines = result.stdout.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "----------")
    except StopIteration:
        start = -1
    listing = "\n".join(lines[start + 1 :])
    for block in listing.split("\n\n"):
        block_lines = block.splitlines()
        # An archive-level block (the archive itself, or a nested archive's
        # header) describes a container, not an entry to extract.
        if any(
            line.startswith(("Type = ", "Physical Size = ")) for line in block_lines
        ):
            continue
        path = None
        for line in block.splitlines():
            if line.startswith("Path = ") and path is None:
                path = line[len("Path = ") :]
            elif (
                line.startswith("Symbolic Link = ") and line.strip() != "Symbolic Link ="
            ) or (
                # 7z/zip archives mark links in the Unix mode instead:
                # "Attributes = A lrwxrwxrwx".
                line.startswith("Attributes = ")
                and line.split()[-1].startswith("l")
                and len(line.split()[-1]) == 10
            ):
                # Refused up front, not just after extraction: an older system
                # 7-Zip (p7zip 16.02, used when the bundled 7zz is missing)
                # follows a link it has just created, so "link -> ~/x" then
                # "link/file" writes outside staging before any later check.
                raise ModInstallError("Archive contains an unsupported symlink")
        if path is not None:
            paths.append(path)
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
    command = [str(archiver), "x", str(archive), f"-o{staging}", "-y", "-p", "-bsp1"]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except (OSError, ValueError) as exc:
        raise ModInstallError(f"Could not start archive extractor: {exc}") from exc
    # 7-Zip's progress uses backspaces, not newlines, so the loop below may
    # not see a line for minutes; watch the cancel flag on its own thread so
    # Cancel stops the extractor at once.
    stop_watching = Event()
    if cancel_event is not None:
        def _watch() -> None:
            while not stop_watching.is_set() and process.poll() is None:
                if cancel_event.wait(0.2):
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    return

        Thread(target=_watch, name="extract-cancel", daemon=True).start()
    try:
        output: list[str] = []
        if process.stdout is None:
            raise ModInstallError("Archive extractor produced no output stream")
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
        if cancel_event is not None and cancel_event.is_set():
            raise ModInstallError("Mod installation cancelled")
        if rc != 0:
            detail = "\n".join(output[-8:])
            raise ModInstallError(
                f"Archive extraction failed (exit code {rc})\n{detail}"
            )
        _unwrap_tarball(archiver, archive, staging, cancel_event)
        _validate_tree(staging)
        _make_owner_writable(staging)
    except Exception:
        if process.poll() is None:
            try:
                process.kill()
                process.wait()
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        stop_watching.set()


def _make_owner_writable(root: Path) -> None:
    """Give the owner write access to everything extracted under ``root``.

    Archives made on Linux can store read-only folders (``dr-xr-xr-x``) and
    7-Zip restores that mode. A read-only folder can't take the files a
    later FOMOD option copies into the same place - ``copytree`` carries
    the mode over - and can't be emptied when the staging folder, or later
    the installed mod, is deleted. Runs after ``_validate_tree``, so there
    are no symlinks to follow.
    """
    for directory, _dirs, files in os.walk(root):
        path = Path(directory)
        try:
            path.chmod(path.stat().st_mode | stat.S_IRWXU)
        except OSError:
            continue
        for name in files:
            file_path = path / name
            try:
                file_path.chmod(file_path.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass


_COMPRESSED_TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar.zst")


def _unwrap_tarball(
    archiver: Path, archive: Path, staging: Path, cancel_event: Event | None
) -> None:
    """Second pass for a compressed tarball.

    7-Zip unpacks one layer at a time: ``x mod.tar.gz`` yields ``mod.tar``,
    which used to be installed as the mod - a single tar file MO2 can't use.
    The inner tar goes through the same listing checks as any archive.
    """
    if not archive.name.lower().endswith(_COMPRESSED_TAR_SUFFIXES):
        return
    entries = list(staging.iterdir())
    if len(entries) != 1 or not entries[0].is_file() or entries[0].suffix.lower() != ".tar":
        return
    inner = entries[0]
    _validate_archive_entries(archiver, inner)
    if cancel_event is not None and cancel_event.is_set():
        raise ModInstallError("Mod installation cancelled")
    try:
        result = subprocess.run(
            [str(archiver), "x", str(inner), f"-o{staging}", "-y", "-p"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=3600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ModInstallError(f"Could not unpack the inner tar archive: {exc}") from exc
    if result.returncode != 0:
        detail = "\n".join(result.stdout.splitlines()[-8:])
        raise ModInstallError(
            f"Archive extraction failed (exit code {result.returncode})\n{detail}"
        )
    inner.unlink()


#: MO2's own STALKER Anomaly/GAMMA game-support plugin
#: (game_stalkeranomaly.py, StalkerAnomalyModDataChecker._valid_folders)
#: only recognizes a mod as valid if one of these sits at its top level -
#: anything else gets flagged INVALID (a red X in MO2's Flags column)
#: and the game's VFS never mounts it.
_GAME_DATA_FOLDER_NAMES = {"appdata", "bin", "db", "gamedata"}


def payload_root(staging: Path) -> Path:
    """Return the archive payload, unwrapping harmless root directories.

    Peels consecutive single-child wrapper directories (an archiver
    often wraps everything in a meaningless container folder). Never
    unwraps *into* a lone top-level directory that is itself one of
    MO2's recognized data folders (see _GAME_DATA_FOLDER_NAMES) - doing
    so used to strip e.g. a mod's own "gamedata" wrapper entirely,
    landing its contents with no recognized top-level folder at all and
    getting the mod flagged INVALID by MO2, even though the archive was
    perfectly valid.

    The one exception: a recognized data folder whose own single child
    is *another* directory with the exact same name (e.g.
    "gamedata/gamedata/...") is a redundant duplicate wrapper, not the
    real payload - that layer is still peeled through, landing on the
    inner one. This keeps both cases correct: "gamedata/<real files>"
    is returned as-is (so "gamedata" lands at the mod's top level), while
    "gamedata/gamedata/<real files>" collapses to a single "gamedata".
    """
    current = staging
    while True:
        children = list(current.iterdir())
        if len(children) != 1 or not children[0].is_dir():
            return current
        child = children[0]
        if child.name == "fomod":
            return current
        if child.name.lower() not in _GAME_DATA_FOLDER_NAMES:
            current = child
            continue
        grandchildren = list(child.iterdir())
        if (
            len(grandchildren) == 1
            and grandchildren[0].is_dir()
            and grandchildren[0].name.lower() == child.name.lower()
        ):
            current = child
            continue
        return current


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
    (destination / INSTALLING_MARKER).touch()
    try:
        for child in list(payload.iterdir()):
            if cancel_event is not None and cancel_event.is_set():
                raise ModInstallError("Mod installation cancelled")
            shutil.move(str(child), destination / child.name)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    (destination / INSTALLING_MARKER).unlink()


def write_basic_meta_ini(destination: Path, installation_file: str) -> None:
    """Write a minimal MO2-style meta.ini into a freshly installed mod.

    MO2's own installer always writes one; this app previously wrote
    none at all, which is a real (if secondary - it doesn't affect
    whether MO2 considers the mod's file layout valid) divergence from
    installing the same mod through MO2 directly. Skips writing if the
    mod's own archive already shipped a meta.ini, so this never
    clobbers real metadata.
    """
    target = destination / "meta.ini"
    if target.exists():
        return
    # An archive file name is the only outside value here; a newline in it
    # would start new INI lines (keys or sections) of its own.
    installation_file = "".join(
        char for char in installation_file if ord(char) >= 32 and ord(char) != 127
    )
    atomic.write_text(
        target,
        "[General]\n"
        "gameName=stalkeranomaly\n"
        "modid=0\n"
        "version=\n"
        f"installationFile={installation_file}\n"
        "[installedFiles]\n",
    )


def make_staging_dir(gamma_dir: str | Path | None) -> Path:
    """A fresh temporary folder to extract a mod archive into.

    Made inside the GAMMA folder (next to ``mods``, never inside it, where
    MO2 would list it as a mod) so the final move into ``mods`` is a rename
    on one filesystem. The system temp folder is often a RAM-backed tmpfs
    (Arch, SteamOS): a multi-GB mod extracted there could fill RAM, and
    every file was then copied across filesystems a second time. Falls back
    to the system temp folder when the GAMMA folder is unknown or can't be
    written.
    """
    try:
        return Path(tempfile.mkdtemp(prefix=".gamma-mod-install-", dir=gamma_dir))
    except OSError:
        return Path(tempfile.mkdtemp(prefix="gamma-mod-install-"))


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
    temp = make_staging_dir(mods_dir.parent)
    try:
        staging = temp / "payload"
        extract_archive(archive, staging, cancel_event, progress)
        move_payload(staging, mods_dir / mod_name, cancel_event)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return mod_name
