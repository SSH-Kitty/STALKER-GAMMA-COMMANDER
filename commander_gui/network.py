"""Small bounded network-read helpers used by GUI background operations."""

from __future__ import annotations

import random
import time
import urllib.error
import urllib.parse
import urllib.request

_ALLOWED_SCHEMES = ("http", "https")
#: HTTP statuses worth a retry - a momentary rate-limit or upstream hiccup,
#: not a request that's simply wrong (a 404/401 will never succeed no
#: matter how many times it's retried).
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


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


def urlopen_with_retry(
    url_or_request: str | urllib.request.Request,
    *,
    timeout: float,
    attempts: int = 3,
    base_delay: float = 0.5,
):
    """``urlopen()`` with a few retries on transient failures.

    A single dropped packet or a momentary 429/5xx from GitHub previously
    read as "update check failed" on the very first hiccup - most clear up
    within a second or two. Never retries the scheme-rejection ``ValueError``
    (not transient) or a non-retryable HTTP status (a 404 will never
    succeed no matter how many times it's asked again). Only covers
    connection establishment, not a failure partway through reading an
    already-open response - a caller streaming a large body still needs its
    own handling for a mid-download drop.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return urlopen(url_or_request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP_STATUSES or attempt == attempts - 1:
                raise
            last_exc = exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            if attempt == attempts - 1:
                raise
            last_exc = exc
        time.sleep(base_delay * (2**attempt) + random.uniform(0, base_delay))
    raise last_exc  # pragma: no cover - loop above always returns or raises


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
