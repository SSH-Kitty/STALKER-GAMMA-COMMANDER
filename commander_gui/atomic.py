"""Serialized atomic file writes used by GUI and background callbacks."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

_WRITE_LOCK = threading.RLock()


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text through a unique sibling temporary file."""
    _write(path, text.encode("utf-8"), lock=True)


def write_bytes(path: Path, data: bytes, *, lock: bool = True) -> None:
    """Write ``data`` through a unique sibling temporary file.

    ``lock=False`` skips the sibling ``.<name>.lock`` file - for writing
    into a directory COMMANDER does not own (Steam's userdata), where a
    stray dotfile left behind would be litter in someone else's config.
    """
    _write(path, data, lock=lock)


def _write(path: Path, data: bytes, *, lock: bool) -> None:
    # A symlinked settings.json or modlist.txt (dotfile repos, profiles
    # shared between installs) is written *through*: os.replace() on the
    # link itself would swap the link for a plain file and silently cut it.
    if path.is_symlink():
        path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        with _WRITE_LOCK:
            if fcntl is not None and lock:
                lock_fd = os.open(
                    path.parent / f".{path.name}.lock",
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            fd, temporary = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            temporary_path = Path(temporary)
            try:
                # mkstemp creates 0o600; keep the replaced file's permissions
                # so shared files (e.g. the autostart .desktop) stay readable.
                os.chmod(
                    temporary_path,
                    stat.S_IMODE(path.stat().st_mode),
                )
            except OSError:
                pass
            try:
                stream = os.fdopen(fd, "wb")
            except BaseException:
                # fdopen failed before taking ownership of the descriptor -
                # close it directly or it leaks (the except block below only
                # unlinks the already-created temp file, not this fd).
                os.close(fd)
                raise
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
                lock_fd = None
    except BaseException:
        if "temporary_path" in locals():
            temporary_path.unlink(missing_ok=True)
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        raise
