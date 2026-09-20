"""Download and install COMMANDER's own AppImage update in place.

Only meaningful when running as an AppImage (``commander_appimage_path()``
returns a path) - a source checkout or the AUR package (which installs into
``/opt``/``/usr/bin`` via pacman, see ``packaging/aur/PKGBUILD``) must keep
updating through its own normal channel instead, since replacing a file
pacman tracks would fight the package manager.

No checksum is published alongside COMMANDER's AppImage releases (unlike
GE-Proton, which ``proton_installer.py`` verifies via a companion
``.sha512sum`` asset) - this trusts the HTTPS download the same way the rest
of the app already trusts mod-archive/GAMMA-repo downloads with no published
checksum.
"""

from __future__ import annotations

import os
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
    if downloaded == 0:
        tmp_path.unlink(missing_ok=True)
        raise CommanderSelfUpdateError("Downloaded update file was empty")
    return tmp_path


def install_commander_update(downloaded: Path, running_path: Path) -> None:
    """Atomically replace *running_path* with *downloaded* (same filesystem)."""
    os.chmod(downloaded, 0o755)
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
