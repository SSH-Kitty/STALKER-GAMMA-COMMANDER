"""Download and install COMMANDER's own AppImage update in place.

Only meaningful when running as an AppImage (``commander_appimage_path()``
returns a path) - a source checkout or the AUR package (which installs into
``/opt``/``/usr/bin`` via pacman, see ``packaging/aur/PKGBUILD``) must keep
updating through its own normal channel instead, since replacing a file
pacman tracks would fight the package manager.

Nothing is swapped in unless the download proves itself first:

* it must be complete - exactly as many bytes as the server announced (a
  dropped connection otherwise ends the read quietly, and a truncated file
  used to replace the working AppImage);
* it must actually be an AppImage (ELF header plus the type-2 AppImage
  magic), not an HTML error page or a truncated stub;
* when the release carries a ``<AppImage>.sha512sum`` asset (``build-
  appimage.sh`` writes one next to every build), the file must match it.
  Older releases without one fall back to the checks above.

A checksum published in the same release protects against corruption, not
against someone who controls the release itself; that needs a signing key
held outside GitHub (minisign/GPG), which the release process does not have
yet.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .network import urlopen_with_retry as urlopen
from .repair import USER_AGENT
from .updates import _COMMANDER_REPO

#: Generous but bounded - the real AppImage is a few hundred MB.
_MAX_APPIMAGE_BYTES = 2 * 1024**3
_MAX_CHECKSUM_BYTES = 64 * 1024

#: ELF magic, and the AppImage type-2 magic ("AI" + 0x02) that the AppImage
#: runtime stores in the ELF header's padding at offset 8.
_ELF_MAGIC = b"\x7fELF"
_APPIMAGE_MAGIC = b"AI\x02"


def verify_appimage_file(path: Path) -> None:
    """Raise ``CommanderSelfUpdateError`` unless *path* looks like an AppImage."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError as exc:
        raise CommanderSelfUpdateError(f"Could not read the update: {exc}") from exc
    if not head.startswith(_ELF_MAGIC) or head[8:11] != _APPIMAGE_MAGIC:
        raise CommanderSelfUpdateError(
            "The downloaded update is not a valid AppImage - nothing was changed."
        )


def _published_sha512(url: str) -> str | None:
    """The release's ``.sha512sum`` digest for *url*, or None if not published."""
    req = urllib.request.Request(url + ".sha512sum", headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=30) as resp:
            text = resp.read(_MAX_CHECKSUM_BYTES).decode("ascii", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise CommanderSelfUpdateError(f"Could not fetch the update checksum: {exc}") from exc
    except urllib.error.URLError as exc:
        raise CommanderSelfUpdateError(f"Could not fetch the update checksum: {exc}") from exc
    match = re.search(r"\b([0-9a-fA-F]{128})\b", text)
    if match is None:
        raise CommanderSelfUpdateError("The update's checksum file is malformed")
    return match.group(1).lower()


def _sha512_file(path: Path) -> str:
    digest = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CommanderSelfUpdateError(Exception):
    """Raised when a self-update step fails."""


class CommanderUpdateAssetNotFoundError(CommanderSelfUpdateError):
    """Raised when the predicted release asset does not exist (a 404)."""


def commander_appimage_path() -> Path | None:
    """Return the running AppImage's own path, or None off that packaging."""
    appimage = os.environ.get("APPIMAGE")
    return Path(appimage) if appimage else None


def commander_update_asset_url(tag: str) -> str:
    """Predict the AppImage asset URL for release *tag*.

    Matches ``build-appimage.sh``'s own artifact naming:
    ``STALKER-GAMMA-COMMANDER-<version>-x86_64.AppImage``, version without
    a leading "v" even when the release tag has one.
    """
    # The tag comes from the release feed and lands in a URL path: only a
    # plain version-like tag, never "/" or ".." that could point elsewhere.
    if not re.fullmatch(r"v?[0-9A-Za-z][0-9A-Za-z._-]{0,63}", tag) or ".." in tag:
        raise CommanderSelfUpdateError(f"Unexpected release tag: {tag!r}")
    version = tag.removeprefix("v")
    filename = f"STALKER-GAMMA-COMMANDER-{version}-x86_64.AppImage"
    return f"{_COMMANDER_REPO}/releases/download/{tag}/{filename}"


def download_commander_update(
    tag: str,
    running_path: Path,
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> Path:
    """Download the AppImage asset for *tag* into ``running_path``'s own directory.

    Landing in the same directory as *running_path* keeps the later
    ``os.replace()`` swap on one filesystem, so it stays atomic. Raises
    ``CommanderUpdateAssetNotFoundError`` when the predicted asset doesn't
    exist (a 404), ``CommanderSelfUpdateError`` for every other failure.
    """
    url = commander_update_asset_url(tag)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=600) as resp:
            header_value = resp.headers.get("Content-Length")
            try:
                total = int(header_value) if header_value is not None else None
            except (TypeError, ValueError):
                total = None
            if total is not None and total <= 0:
                total = None
            if total is not None and total > _MAX_APPIMAGE_BYTES:
                raise CommanderSelfUpdateError(
                    "Update download exceeds the allowed size"
                )
            # A missing/malformed header must not silently skip the
            # preflight check - assume the worst case (the allowed cap).
            space_check_total = total if total is not None else _MAX_APPIMAGE_BYTES
            try:
                free = shutil.disk_usage(running_path.parent).free
            except OSError as exc:
                raise CommanderSelfUpdateError(
                    f"Could not check free disk space: {exc}"
                ) from exc
            if free < space_check_total * 2:
                raise CommanderSelfUpdateError(
                    "Not enough free disk space to download the update"
                )
            try:
                fd, tmp_name = tempfile.mkstemp(
                    dir=running_path.parent,
                    prefix=f".{running_path.name}.",
                    suffix=".update",
                )
            except OSError as exc:
                raise CommanderSelfUpdateError(
                    f"Could not create a temporary file next to {running_path} - "
                    f"move COMMANDER to a writable folder and try again: {exc}"
                ) from exc
            tmp_path = Path(tmp_name)
            # Progress falls back to indeterminate (total=1) only for the
            # callback, independent of the size-cap/disk-space checks above.
            progress_total = total or 1
            downloaded = 0
            try:
                with os.fdopen(fd, "wb") as f:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise CommanderSelfUpdateError("Update cancelled")
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > _MAX_APPIMAGE_BYTES:
                            raise CommanderSelfUpdateError(
                                "Update download exceeds the allowed size"
                            )
                        if progress_cb is not None:
                            progress_cb(downloaded, progress_total)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise CommanderUpdateAssetNotFoundError(
                f"No update file found at {url}"
            ) from exc
        raise CommanderSelfUpdateError(f"Update download failed: {exc}") from exc
    except urllib.error.URLError as exc:
        raise CommanderSelfUpdateError(f"Update download failed: {exc}") from exc
    try:
        if downloaded == 0:
            raise CommanderSelfUpdateError("Downloaded update file was empty")
        if total is not None and downloaded != total:
            # HTTPResponse.read() returns b"" on a dropped connection instead
            # of raising, so a short file is the only sign of it.
            raise CommanderSelfUpdateError(
                f"The update download was incomplete ({downloaded} of {total} bytes)"
                " - nothing was changed. Try again."
            )
        verify_appimage_file(tmp_path)
        expected = _published_sha512(url)
        if expected is not None and _sha512_file(tmp_path) != expected:
            raise CommanderSelfUpdateError(
                "The update does not match its published checksum - nothing was changed."
            )
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def install_commander_update(downloaded: Path, running_path: Path) -> None:
    """Atomically replace *running_path* with *downloaded* (same filesystem)."""
    verify_appimage_file(downloaded)
    # An AppImage must be executable; the file was verified just above.
    os.chmod(downloaded, 0o755)  # nosec B103
    os.replace(downloaded, running_path)


def relaunch_commander(
    path: Path, release_lock: Callable[[], None] | None = None
) -> None:
    """Start *path* as a new, independent process.

    *release_lock* must drop the single-instance lock before the new
    process starts - same requirement as ``deck_launch.relaunch_exec``'s own
    *release_lock*, and for the same reason: the new process reaches its own
    ``_acquire_instance_lock()`` almost immediately, typically before this
    (still-running) process has actually exited, and a lock still held at
    that moment makes the new process conclude another instance is running
    and exit with no window at all.
    """
    if release_lock is not None:
        release_lock()
    subprocess.Popen(
        [str(path)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def download_and_install_commander_update(
    tag: str,
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> Path:
    """Download and install *tag* over the running AppImage. Returns its path."""
    running_path = commander_appimage_path()
    if running_path is None:
        raise CommanderSelfUpdateError(
            "COMMANDER is not running as an AppImage - it cannot update itself."
        )
    downloaded = download_commander_update(
        tag, running_path, cancel_event=cancel_event, progress_cb=progress_cb
    )
    install_commander_update(downloaded, running_path)
    return running_path


def switch_commander_build(
    target: str,
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> tuple[Path, str]:
    """Install the newest ``"unstable"`` or ``"stable"`` build over this one.

    Unlike an update, this may go *down* a version: reverting from an
    unstable build installs the latest stable release even when it is older.
    Returns the AppImage path and the tag installed.
    """
    from . import __version__
    from .updates import latest_stable_tag, newer_unstable_tag

    if commander_appimage_path() is None:
        raise CommanderSelfUpdateError(
            "COMMANDER is not running as an AppImage - it cannot switch builds itself."
        )
    if target == "unstable":
        tag = newer_unstable_tag(__version__)
        if tag is None:
            raise CommanderSelfUpdateError(
                "There is no unstable build newer than the stable one right now "
                "(or the release list could not be read)."
            )
    elif target == "stable":
        tag = latest_stable_tag()
        if tag is None:
            raise CommanderSelfUpdateError(
                "Could not find the latest stable release - check your connection "
                "and try again."
            )
    else:
        raise CommanderSelfUpdateError(f"Unknown build channel: {target}")
    path = download_and_install_commander_update(
        tag, cancel_event=cancel_event, progress_cb=progress_cb
    )
    return path, tag
