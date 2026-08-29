"""Serialized atomic file writes used by GUI and background callbacks."""

from __future__ import annotations

import os
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
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        with _WRITE_LOCK:
            if fcntl is not None:
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
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
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
