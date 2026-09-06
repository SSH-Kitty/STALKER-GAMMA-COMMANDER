"""Small bounded network-read helpers used by GUI background operations."""

from __future__ import annotations

import urllib.parse
import urllib.request

_ALLOWED_SCHEMES = ("http", "https")


def urlopen(url_or_request: str | urllib.request.Request, *, timeout: float):
    """``urllib.request.urlopen`` restricted to http(s).

    Every URL opened here ultimately comes from a user-editable profile
    field (``mod_list_url``, ``mod_pack_maker_url``, repo URLs) or a
    hardcoded release endpoint - never from a remote/untrusted party
    directly. Restricting the scheme costs nothing for that legitimate
    case, and closes off a ``file:///etc/passwd``-style local-file read if
    a profile is ever imported from a file someone else handed the user.
    """
    full_url = (
        url_or_request.full_url
        if isinstance(url_or_request, urllib.request.Request)
        else url_or_request
    )
    scheme = urllib.parse.urlsplit(full_url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Refusing to open a non-http(s) URL: {full_url!r}")
    return urllib.request.urlopen(url_or_request, timeout=timeout)


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
