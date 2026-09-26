"""Download and install GE-Proton builds from GitHub releases."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__, network
from .network import read_response_bytes

_GITHUB_API = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"
_USER_AGENT = f"CommanderGUI/{__version__}"
_ASSET_RE = re.compile(r"GE-Proton\d+-\d+(?:-\w+)?\.tar\.gz$")
_CHECKSUM_RE = re.compile(r"\.sha512sum$")

#: platform.machine() values -> the architecture suffix GE-Proton puts on
#: its asset names ("GE-Proton11-5-x86_64.tar.gz"). Releases since 11-x ship
#: an x86_64 and an aarch64 build side by side; older ones ship one archive
#: with no suffix at all.
_ARCH_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}


def host_arch() -> str:
    return _ARCH_ALIASES.get(platform.machine().lower(), "x86_64")
_MAX_API_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_CHECKSUM_BYTES = 1 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 8 * 1024**3
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_EXPANDED_BYTES = 16 * 1024**3


def _api_get(url: str) -> dict | list:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with network.urlopen_with_retry(req, timeout=30) as resp:
        return json.loads(read_response_bytes(resp, _MAX_API_RESPONSE_BYTES))


def fetch_ge_proton_releases(count: int = 10) -> list[dict]:
    """Return recent GE-Proton releases from GitHub.

    Each entry has ``tag`` (e.g. ``"GE-Proton11-5"``) and ``published``.
    """
    count = min(100, max(1, int(count)))
    data = _api_get(f"{_GITHUB_API}?per_page={count}")
    out: list[dict] = []
    for rel in data:
        tag = rel.get("tag_name", "")
        if not tag.startswith("GE-Proton"):
            continue
        out.append({"tag": tag, "published": rel.get("published_at", "")})
    return out


def _pick_assets(assets: list[dict], arch: str) -> tuple[str, str, str]:
    """Choose ``(tarball_url, checksum_url, tarball_name)`` for ``arch``.

    Picks the archive built for this machine, then the checksum file that
    belongs to *that* archive (same name, ``.sha512sum`` instead of
    ``.tar.gz``). Previously the last matching asset of each kind won, so
    with an x86_64 and an aarch64 build in one release the result depended
    on GitHub's listing order - and the checksum could come from the other
    build than the tarball.
    """
    tarballs = {
        a.get("name", ""): a.get("browser_download_url", "")
        for a in assets
        if _ASSET_RE.search(a.get("name", ""))
    }
    checksums = {
        a.get("name", ""): a.get("browser_download_url", "")
        for a in assets
        if _CHECKSUM_RE.search(a.get("name", ""))
    }
    if not tarballs:
        raise ValueError("No .tar.gz asset found")
    suffixes = {f"-{name}.tar.gz" for name in set(_ARCH_ALIASES.values())}
    own = [name for name in tarballs if name.endswith(f"-{arch}.tar.gz")]
    plain = [name for name in tarballs if not any(name.endswith(x) for x in suffixes)]
    candidates = own or plain
    if not candidates:
        raise ValueError(f"No {arch} build in this release")
    name = min(candidates)
    stem = name.removesuffix(".tar.gz")
    checksum = checksums.get(f"{stem}.sha512sum")
    if checksum is None and len(checksums) == 1:
        # Older single-build releases: one checksum file for one archive.
        checksum = next(iter(checksums.values()))
    if not checksum:
        raise ValueError(f"No SHA512 checksum asset found for {name}")
    return tarballs[name], checksum, name


def _find_assets(release_tag: str) -> tuple[str, str]:
    """Return ``(tarball_url, checksum_url)`` for *release_tag* on this machine.

    Raises ``ValueError`` if the release or assets are not found.
    """
    rel = _api_get(f"{_GITHUB_API}/tags/{release_tag}")
    try:
        tar_url, sum_url, _name = _pick_assets(rel.get("assets", []), host_arch())
    except ValueError as exc:
        raise ValueError(f"{exc} ({release_tag})") from exc
    return tar_url, sum_url


def _sha512(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _inside(root: str, path: str) -> bool:
    """True if normalised archive path ``path`` stays inside ``root`` ("")."""
    normal = posixpath.normpath(path)
    return not (normal == ".." or normal.startswith(("/", "../"))) and (root == "" or normal == root or normal.startswith(root + "/"))


def _safe_extract(tf: tarfile.TarFile, destination: Path) -> None:
    """Extract archive members without path or link escapes.

    Symlinks are allowed - a Proton build is full of them (``libfoo.so.0``
    -> ``libfoo.so.0.0.0``), and refusing every one is what made recent
    GE-Proton releases impossible to install - but only relative ones that
    resolve *inside* the archive's own top-level folder. Hard links must name
    another member inside it too. Absolute links, ``..`` escapes, device
    files and FIFOs are still refused.
    """
    root = destination.resolve()
    members: list[tarfile.TarInfo] = []
    expanded_bytes = 0
    for member in tf:
        members.append(member)
        if len(members) > _MAX_ARCHIVE_MEMBERS:
            raise ValueError("Refusing Proton archive with too many members")
        name = member.name
        if not _inside("", name):
            raise ValueError(f"Refusing unsafe archive path: {name}")
        top = posixpath.normpath(name).split("/", 1)[0]
        if member.issym():
            if member.linkname.startswith("/"):
                raise ValueError(f"Refusing absolute link in Proton archive: {name}")
            target = posixpath.join(posixpath.dirname(name), member.linkname)
            if not _inside(top, target):
                raise ValueError(f"Refusing unsafe link in Proton archive: {name}")
        elif member.islnk():
            if not _inside(top, member.linkname):
                raise ValueError(f"Refusing unsafe link in Proton archive: {name}")
        elif not (member.isdir() or member.isreg()):
            raise ValueError(f"Refusing special file in Proton archive: {name}")
        expanded_bytes += max(0, member.size)
        if expanded_bytes > _MAX_EXPANDED_BYTES:
            raise ValueError("Refusing Proton archive with excessive expanded size")
        target_path = (root / name).resolve()
        if target_path != root and root not in target_path.parents:
            raise ValueError(f"Refusing unsafe archive path: {name}")
    try:
        # The "data" filter re-checks every link against the destination as a
        # second line of defence; the links vetted above all pass it.
        tf.extractall(destination, members=members, filter="data")
    except TypeError:
        # Python without extraction filters (older than 3.12 / the 3.10.12
        # and 3.11.4 backports). The checks above are by name only, and a
        # chain of links can still point outside - so extract one member at
        # a time, refusing any whose parent folder already resolves outside
        # the root (a link planted by an earlier member), then verify every
        # link that landed.
        for member in members:
            parent = Path(os.path.realpath(root / posixpath.dirname(member.name)))
            if parent != root and root not in parent.parents:
                raise ValueError(f"Refusing unsafe archive path: {member.name}") from None
            tf.extract(member, destination)
        for path in destination.rglob("*"):
            if path.is_symlink():
                resolved = Path(os.path.realpath(path))
                if resolved != root and root not in resolved.parents:
                    raise ValueError(f"Refusing unsafe link in Proton archive: {path.name}") from None


def _top_level_dir(tar_path: Path) -> str | None:
    """The archive's single top-level folder, or None if it has several."""
    with tarfile.open(tar_path, "r:gz") as tf:
        tops = {posixpath.normpath(m.name).split("/", 1)[0] for m in tf}
    tops.discard(".")
    return tops.pop() if len(tops) == 1 else None


def install_proton(
    version_tag: str,
    install_dir: Path,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Download and install a GE-Proton build.

    *install_dir* is the ``compatibilitytools.d`` parent directory.
    Returns the path to the installed build directory.
    Raises ``ValueError`` or ``OSError`` on failure.
    """
    def check_cancelled() -> None:
        if cancel_event and cancel_event.is_set():
            raise ValueError("Download cancelled")

    tar_url, sum_url = _find_assets(version_tag)

    install_dir.mkdir(parents=True, exist_ok=True)
    installed_by_us = False
    # Downloaded next to where it installs, not into the system temp dir:
    # /tmp is often a RAM-backed tmpfs too small for a ~500 MB archive, and
    # the free-space check below is made against install_dir.
    with tempfile.TemporaryDirectory(prefix=".proton-download-", dir=install_dir) as tmp:
        tmp_path = Path(tmp)
        tar_name = tar_url.rsplit("/", 1)[-1]
        tar_path = tmp_path / tar_name

        # --- download tarball ---
        req = urllib.request.Request(tar_url, headers={"User-Agent": _USER_AGENT})
        with network.urlopen_with_retry(req, timeout=600) as resp:
            try:
                header_value = resp.headers.get("Content-Length")
                total = int(header_value) if header_value is not None else None
                # A literal "0" is still "not None" but is just as
                # untrustworthy as a missing header - treating it as a
                # real size let the worst-case disk-space fallback below
                # be silently skipped instead of applied.
                if total is not None and total <= 0:
                    total = None
            except (TypeError, ValueError):
                total = None
            if total is not None and total > _MAX_ARCHIVE_BYTES:
                raise ValueError("Proton archive exceeds the allowed download size")
            # A missing/malformed header must not silently skip the preflight
            # check - assume the worst case (the allowed cap) instead so a
            # near-full disk still fails fast rather than mid-download.
            space_check_total = total if total is not None else _MAX_ARCHIVE_BYTES
            if shutil.disk_usage(install_dir).free < space_check_total * 2:
                raise ValueError("Not enough free disk space for the Proton archive")
            # Progress falls back to indeterminate (total=1) only for the
            # callback, independent of the checks above.
            total = total or 1
            downloaded = 0
            with open(tar_path, "wb") as f:
                while True:
                    if cancel_event and cancel_event.is_set():
                        raise ValueError("Download cancelled")
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > _MAX_ARCHIVE_BYTES:
                        raise ValueError(
                            "Proton archive exceeds the allowed download size"
                        )
                    if progress_cb:
                        progress_cb(downloaded, total)

        # --- verify checksum ---
        check_cancelled()
        req_sum = urllib.request.Request(sum_url, headers={"User-Agent": _USER_AGENT})
        with network.urlopen_with_retry(req_sum, timeout=30) as resp:
            checksum_text = read_response_bytes(resp, _MAX_CHECKSUM_BYTES).decode(
                errors="replace"
            )
        check_cancelled()
        match = re.search(r"\b([0-9a-fA-F]{128})\b", checksum_text)
        if match is None:
            raise ValueError("Checksum asset did not contain a valid SHA512 digest")
        expected = match.group(1).lower()
        check_cancelled()
        actual = _sha512(tar_path)
        check_cancelled()
        if actual != expected:
            raise ValueError(
                f"SHA512 mismatch: expected {expected[:16]}… got {actual[:16]}…"
            )

        # --- extract transactionally ---
        # Named after what is actually inside, not guessed from the file name:
        # the two only agree by convention.
        dir_name = _top_level_dir(tar_path) or tar_name.removesuffix(".tar.gz")
        destination = install_dir / dir_name
        if destination.exists():
            raise ValueError(f"Proton build already exists: {destination.name}")
        staging = Path(tempfile.mkdtemp(prefix=".proton-staging-", dir=install_dir))
        try:
            check_cancelled()
            with tarfile.open(tar_path, "r:gz") as tf:
                _safe_extract(tf, staging)
            installed_staged = staging / dir_name
            if not installed_staged.is_dir():
                raise ValueError(f"Expected directory {dir_name} not found in archive")
            check_cancelled()
            os.replace(installed_staged, destination)
            installed_by_us = True
            # No check_cancelled() here: once the rename above has
            # succeeded, the install is committed. The single cleanup path
            # for a cancellation that lands after that point is the
            # unconditional check below, against `installed` (the same
            # path as `destination`) - raising here instead would skip
            # that cleanup and leave a fully-installed build behind while
            # still reporting cancellation, permanently blocking a retry
            # with "Proton build already exists".
        finally:
            # A cancel between os.replace() and here is handled once, below,
            # by the unconditional post-`with` check against `installed`
            # (the same path as `destination`) - no need to also clean up here.
            shutil.rmtree(staging, ignore_errors=True)

    # --- locate installed dir ---
    # The top-level dir inside the tarball is the asset name minus .tar.gz
    installed = install_dir / dir_name
    if cancel_event and cancel_event.is_set():
        if installed_by_us:
            shutil.rmtree(installed, ignore_errors=True)
        raise ValueError("Download cancelled")
    if not installed.is_dir():
        raise ValueError(f"Expected directory {dir_name} not found after installation")
    return installed


# -- managing installed builds ------------------------------------------------
@dataclass
class ProtonBuild:
    """A GE-Proton build in a ``compatibilitytools.d`` folder."""

    name: str
    path: Path
    in_use: bool = False
    newest: bool = False
    steam_uses: bool = False

    @property
    def removable(self) -> bool:
        return not self.in_use


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", name)) or (0,)


def _build_in_use(runner_kind: str) -> Path | None:
    """The build a launch with the saved runner would use, if it is one."""
    from .launcher import _pick_umu_proton  # deferred: launcher is heavier

    if runner_kind.startswith("umup:"):
        try:
            return Path(runner_kind.split(":", 1)[1]).expanduser().resolve().parent
        except OSError:
            return None
    if runner_kind in ("auto", "umu", ""):
        picked = _pick_umu_proton()
        if picked:
            try:
                return Path(picked).resolve()
            except OSError:
                return None
    return None


def steam_compat_tool_names() -> set[str]:
    """Compatibility tools Steam games are set to use (config.vdf)."""
    from .launcher import STEAM_ROOT_CANDIDATES

    names: set[str] = set()
    for root in STEAM_ROOT_CANDIDATES:
        config = root / "config" / "config.vdf"
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        start = text.find('"CompatToolMapping"')
        if start < 0:
            continue
        brace = text.find("{", start)
        depth, end = 0, len(text)
        for index in range(brace, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        names.update(re.findall(r'"name"\s+"([^"]+)"', text[brace:end]))
    return names


def installed_builds(runner_kind: str | None = None) -> list[ProtonBuild]:
    """Every removable-kind build COMMANDER can see, newest first.

    Only builds directly inside a ``compatibilitytools.d`` folder are listed
    - a build picked by hand from somewhere else is not COMMANDER's to
    remove. ``in_use`` marks the build the saved runner launches with.
    """
    from . import gui_settings
    from .launcher import find_extra_protons

    if runner_kind is None:
        runner_kind = gui_settings.load_gui_settings().get("runner") or "auto"
    used = _build_in_use(runner_kind)
    steam_names = steam_compat_tool_names()
    builds: list[ProtonBuild] = []
    for name, script in find_extra_protons():
        build_dir = Path(script).parent
        if build_dir.parent.name != "compatibilitytools.d":
            continue
        builds.append(
            ProtonBuild(
                name=name,
                path=build_dir,
                in_use=used is not None and build_dir == used,
                steam_uses=name in steam_names,
            )
        )
    builds.sort(key=lambda build: _version_key(build.name), reverse=True)
    ge = [build for build in builds if build.name.startswith("GE-Proton")]
    if ge:
        ge[0].newest = True
    return builds


def build_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def remove_build(path: Path, runner_kind: str | None = None) -> None:
    """Delete a GE-Proton build. Raises ``ValueError`` when it must not be."""
    path = Path(path)
    if path.parent.name != "compatibilitytools.d":
        raise ValueError(f"Not inside a compatibilitytools.d folder: {path}")
    if path.name.startswith("UMU-Proton") or path.name in ("", ".", ".."):
        raise ValueError(f"Refusing to remove {path}")
    if not (path / "proton").is_file():
        raise ValueError(f"Not a Proton build (no proton script): {path}")
    if runner_kind is None:
        from . import gui_settings

        runner_kind = gui_settings.load_gui_settings().get("runner") or "auto"
    used = _build_in_use(runner_kind)
    if used is not None and path.resolve() == used:
        raise ValueError(
            f"{path.name} is the build COMMANDER launches the game with. "
            "Pick another runner on the Play page first."
        )
    if path.is_symlink():
        path.unlink()
        return
    shutil.rmtree(path)
    if path.exists():
        raise ValueError(f"{path} could not be fully removed.")
