"""Download and install GE-Proton builds from GitHub releases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .network import read_response_bytes

_GITHUB_API = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"
_USER_AGENT = "CommanderGUI/1.2"
_ASSET_RE = re.compile(r"GE-Proton\d+-\d+(?:-\w+)?\.tar\.gz$")
_CHECKSUM_RE = re.compile(r"\.sha512sum$")
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
    with urllib.request.urlopen(req, timeout=30) as resp:
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


def _find_assets(release_tag: str) -> tuple[str, str]:
    """Return ``(tarball_url, checksum_url)`` for *release_tag*.

    Raises ``ValueError`` if the release or assets are not found.
    """
    rel = _api_get(f"{_GITHUB_API}/tags/{release_tag}")
    assets = rel.get("assets", [])
    tar_url = ""
    sum_url = ""
    for asset in assets:
        name = asset.get("name", "")
        url = asset.get("browser_download_url", "")
        if _ASSET_RE.search(name):
            tar_url = url
        elif _CHECKSUM_RE.search(name):
            sum_url = url
    if not tar_url:
        raise ValueError(f"No .tar.gz asset found for {release_tag}")
    if not sum_url:
        raise ValueError(f"No SHA512 checksum asset found for {release_tag}")
    return tar_url, sum_url


def _sha512(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract(tf: tarfile.TarFile, destination: Path) -> None:
    """Extract archive members without path or link escapes."""
    root = destination.resolve()
    members: list[tarfile.TarInfo] = []
    expanded_bytes = 0
    for member in tf:
        members.append(member)
        if len(members) > _MAX_ARCHIVE_MEMBERS:
            raise ValueError("Refusing Proton archive with too many members")
        if member.issym() or member.islnk():
            raise ValueError(f"Refusing unsafe link in Proton archive: {member.name}")
        expanded_bytes += max(0, member.size)
        if expanded_bytes > _MAX_EXPANDED_BYTES:
            raise ValueError("Refusing Proton archive with excessive expanded size")
        target = (root / member.name).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"Refusing unsafe archive path: {member.name}")
    try:
        tf.extractall(destination, members=members, filter="data")
    except TypeError:
        # Python < 3.12 has no extraction filter; members were validated above.
        tf.extractall(destination, members=members)


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
    tar_url, sum_url = _find_assets(version_tag)

    install_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="proton_install_") as tmp:
        tmp_path = Path(tmp)
        tar_name = tar_url.rsplit("/", 1)[-1]
        tar_path = tmp_path / tar_name

        # --- download tarball ---
        req = urllib.request.Request(tar_url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=600) as resp:
            total = int(resp.headers.get("Content-Length", 0)) or 1
            if total > _MAX_ARCHIVE_BYTES:
                raise ValueError("Proton archive exceeds the allowed download size")
            if shutil.disk_usage(install_dir).free < total * 2:
                raise ValueError("Not enough free disk space for the Proton archive")
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
        req_sum = urllib.request.Request(sum_url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req_sum, timeout=30) as resp:
            checksum_text = read_response_bytes(resp, _MAX_CHECKSUM_BYTES).decode(
                errors="replace"
            )
        match = re.search(r"\b([0-9a-fA-F]{128})\b", checksum_text)
        if match is None:
            raise ValueError("Checksum asset did not contain a valid SHA512 digest")
        expected = match.group(1).lower()
        actual = _sha512(tar_path)
        if actual != expected:
            raise ValueError(
                f"SHA512 mismatch: expected {expected[:16]}… got {actual[:16]}…"
            )

        # --- extract transactionally ---
        dir_name = tar_name.removesuffix(".tar.gz")
        destination = install_dir / dir_name
        if destination.exists():
            raise ValueError(f"Proton build already exists: {destination.name}")
        staging = Path(tempfile.mkdtemp(prefix=".proton-staging-", dir=install_dir))
        try:
            with tarfile.open(tar_path, "r:gz") as tf:
                _safe_extract(tf, staging)
            installed_staged = staging / dir_name
            if not installed_staged.is_dir():
                raise ValueError(f"Expected directory {dir_name} not found in archive")
            os.replace(installed_staged, destination)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # --- locate installed dir ---
    # The top-level dir inside the tarball is the asset name minus .tar.gz
    installed = install_dir / dir_name
    if not installed.is_dir():
        raise ValueError(f"Expected directory {dir_name} not found after installation")
    return installed
