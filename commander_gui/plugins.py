"""Optional plugins: the ModDB browser, downloaded on demand.

The browser needs QtWebEngine (a whole Chromium), which COMMANDER's own build
strips out to stay small. So it ships as its own archive on each GitHub
release - ``build-moddb-plugin.sh`` makes it - and COMMANDER only fetches it
when the player installs it from Settings > Plugins.

The archive is one top-level ``moddb-browser`` folder holding its own Python
and Qt, plus ``plugin.json``: ``protocol`` (how COMMANDER talks to it), ``qt``
(the Qt it was built with) and ``exec`` (its launcher, relative to the
folder). An installed plugin needs an update when its protocol differs from
:data:`PROTOCOL`, or its Qt is older than COMMANDER's: every Qt release brings
Chromium security fixes, and the plugin opens real web pages.

How COMMANDER talks to it (protocol 1): one JSON line on its stdin with the
theme, language and folders; one JSON line on its stdout per finished
download, ``{"event": "downloaded", "file": ..., "url": ...}``.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from . import __version__, network
from .proton_installer import SHA512_RE, _safe_extract, host_arch, sha512_file
from .repair import USER_AGENT
from .self_update import CommanderSelfUpdateError, _published_sha512
from .updates import _COMMANDER_REPO, _feed_tags, is_unstable_version

#: Raise by one whenever the plugin itself changes - moddb_browser.py, or
#: anything it imports - not only how COMMANDER talks to it. Installed
#: plugins carry their own copy of that code and are only asked to update
#: when this number (or, in the AppImage, the Qt version) moves.
PROTOCOL = 4
MODDB = "moddb-browser"
_MAX_ARCHIVE_BYTES = 2 * 1024**3
#: Releases tried, newest first, when the running version's has no plugin.
_MAX_RELEASES_TRIED = 5


class PluginError(Exception):
    """A plugin step failed; the message is shown to the player."""


def data_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
    return Path(base) / "stalker-gamma-commander" / "plugins"


def install_dir() -> Path:
    return data_root() / MODDB


def browser_data_dir() -> Path:
    """Cookies and cache - kept outside the install so updates keep the login."""
    return data_root() / f"{MODDB}-data"


def downloads_dir() -> Path:
    return data_root() / "downloads"


def asset_name() -> str:
    system = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    return f"COMMANDER-ModDB-Browser-Plugin-{system}-{host_arch()}.tar.xz"


def _qt_tuple(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.split(".")[:3])
    except ValueError:
        return ()


def installed() -> dict | None:
    """The installed plugin's ``plugin.json``, or None when not installed."""
    try:
        info = json.loads((install_dir() / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def needs_update(info: dict, qt_version: str | None = None) -> bool:
    """True when *info*'s protocol differs, or its Qt is older than *qt_version*.

    Only the AppImage passes its Qt by default: the plugin is built from the
    same pinned Qt, so a newer one there means a newer plugin exists. A
    source or AUR install runs whatever Qt the system has, which no plugin
    release may match - comparing there would ask for the same update forever.
    """
    if qt_version is None and os.environ.get("APPIMAGE"):
        import PySide6

        qt_version = PySide6.__version__
    outdated_qt = qt_version is not None and _qt_tuple(str(info.get("qt", ""))) < _qt_tuple(qt_version)
    return info.get("protocol") != PROTOCOL or outdated_qt


def launcher(info: dict) -> Path:
    """The plugin's own launcher, refused if ``exec`` points outside it."""
    root = install_dir().resolve()
    path = (root / str(info.get("exec", ""))).resolve()
    if root not in path.parents or not path.is_file():
        raise PluginError("The ModDB browser plugin is damaged. Remove it and install it again.")
    return path


def release_candidates(tags: list[str], version: str = __version__) -> list[str]:
    """The release tags to look in, best first.

    This version's own release, then the newest others from the feed
    (pre-releases included). A stable build never takes a plugin from a
    pre-release, the same rule its own updates follow.
    """
    if not is_unstable_version(version):
        tags = [tag for tag in tags if not is_unstable_version(tag)]
    # sort() is stable: after this version's tag, the feed's newest-first order.
    tags = sorted(tags, key=lambda tag: tag.removeprefix("v").lower() != version)
    return tags[:_MAX_RELEASES_TRIED]


def _find_release() -> tuple[str, str]:
    """(asset URL, its SHA512) from the first of ``release_candidates()`` that has it."""
    for tag in release_candidates(_feed_tags()):
        # Same rule as self_update: a plain version-like tag, never "/" or
        # ".." that could point the download somewhere else.
        if not re.fullmatch(r"v?[0-9A-Za-z][0-9A-Za-z._-]{0,63}", tag) or ".." in tag:
            continue
        url = f"{_COMMANDER_REPO}/releases/download/{tag}/{asset_name()}"
        try:
            digest = _published_sha512(url)
        except CommanderSelfUpdateError as exc:
            raise PluginError(str(exc)) from exc
        if digest:
            return url, digest
    raise PluginError(
        f"No release has the ModDB browser for this system ({platform.system()} {host_arch()}) yet."
    )


def _download(url: str, target: Path, cancel_event, progress_cb) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with network.urlopen_with_retry(req, timeout=600) as resp, open(target, "wb") as out:
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0
            if total > _MAX_ARCHIVE_BYTES:
                raise PluginError("The plugin download is larger than it can be.")
            if shutil.disk_usage(target.parent).free < (total or _MAX_ARCHIVE_BYTES) * 4:
                raise PluginError("Not enough free disk space for the plugin.")
            done = 0
            while chunk := resp.read(1 << 20):
                if cancel_event is not None and cancel_event.is_set():
                    raise PluginError("Download cancelled")
                done += len(chunk)
                if done > _MAX_ARCHIVE_BYTES:
                    raise PluginError("The plugin download is larger than it can be.")
                out.write(chunk)
                if progress_cb:
                    progress_cb(done, total)
    except (urllib.error.URLError, OSError) as exc:
        raise PluginError(f"The plugin download failed: {exc}") from exc
    if total and done != total:
        raise PluginError("The plugin download was incomplete. Try again.")


def local_checksum(archive: Path) -> str | None:
    """The digest in a ``.sha512sum`` saved next to *archive*, if there is one."""
    try:
        text = Path(f"{archive}.sha512sum").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    match = SHA512_RE.search(text)
    return match.group(1).lower() if match else None


def install_archive(archive: Path, expected_sha512: str | None) -> None:
    """Check *archive*, unpack it and swap it in for the installed plugin.

    ``None`` skips the checksum: only for a file the player picked
    themselves, after COMMANDER warned them it can't be checked.
    """
    if expected_sha512 is not None and sha512_file(archive) != expected_sha512.lower():
        raise PluginError("The plugin doesn't match its published checksum. Nothing was changed.")
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".plugin-staging-", dir=root))
    old = root / f".{MODDB}.old"
    try:
        try:
            with tarfile.open(archive, "r:xz") as tf:
                _safe_extract(tf, staging)
        except (tarfile.TarError, ValueError, EOFError) as exc:
            raise PluginError(f"The plugin archive couldn't be unpacked: {exc}") from exc
        new = staging / MODDB
        if not (new / "plugin.json").is_file():
            raise PluginError("The plugin archive isn't a COMMANDER plugin.")
        shutil.rmtree(old, ignore_errors=True)
        had_old = install_dir().exists()
        if had_old:
            os.replace(install_dir(), old)
        try:
            os.replace(new, install_dir())
        except OSError:
            if had_old:
                os.replace(old, install_dir())  # the installed plugin keeps working
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)


def download_and_install(
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    url, digest = _find_release()
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    # Next to the install, not /tmp: often a small RAM-backed tmpfs.
    with tempfile.TemporaryDirectory(prefix=".plugin-download-", dir=root) as tmp:
        archive = Path(tmp) / asset_name()
        _download(url, archive, cancel_event, progress_cb)
        install_archive(archive, digest)


def remove() -> None:
    shutil.rmtree(install_dir(), ignore_errors=True)


def downloaded_files() -> list[Path]:
    try:
        return [path for path in downloads_dir().iterdir() if path.is_file()]
    except OSError:
        return []


def delete_downloads() -> None:
    """Delete the browser's downloads. Installed mods live in GAMMA's mods folder, untouched."""
    for path in downloaded_files():
        path.unlink(missing_ok=True)


def parse_event(line: str) -> Path | None:
    """The downloaded file from one stdout line of the plugin, if it names one.

    Only a file inside :func:`downloads_dir` - the plugin is a separate
    program, and its output never points COMMANDER anywhere else.
    """
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("event") != "downloaded":
        return None
    path = Path(str(event.get("file", ""))).resolve()
    if downloads_dir().resolve() not in path.parents or not path.is_file():
        return None
    return path
