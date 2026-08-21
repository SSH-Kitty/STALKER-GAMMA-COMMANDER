"""Small bounded network-read helpers used by GUI background operations."""

from __future__ import annotations


def read_response_bytes(response, max_bytes: int) -> bytes:
    """Read a response with a hard byte limit."""
    content_length = response.headers.get("Content-Length")
    try:
        declared = int(content_length) if content_length else 0
    except ValueError:
        declared = 0
    if declared > max_bytes:
        raise ValueError("response exceeds the allowed size")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(65536, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("response exceeds the allowed size")
        chunks.append(chunk)
    return b"".join(chunks)
