"""Serialized atomic file writes used by GUI and background callbacks."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

_WRITE_LOCK = threading.RLock()


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text through a unique sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        with _WRITE_LOCK, os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
