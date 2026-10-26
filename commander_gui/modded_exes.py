"""Install demonized's modded exes (xray-monolith) into the Anomaly folder.

GAMMA pins its own build: the exes go in ``bin/`` and the gamedata that build
needs is copied loose into ``gamedata/`` (configs, scripts, shaders,
materials). A release zip instead carries that gamedata packed in
``db/mods/00_modded_exes_gamedata.db0``, and loose files win over a ``.db0``,
so installing one follows the guide modpacks give (ROMEO's included): empty
``db/mods``, clear ``gamedata/configs`` except ``axr_options.ltx`` and
``cache_dbg.ltx``, drop ``gamedata/materials``, ``scripts`` and ``shaders``,
then unpack the zip over Anomaly.

Nothing is deleted the first time: what the clean-up removes, and the exes it
replaces, move into ``commander-exes/original`` so ``restore_original()`` can
put the folder back exactly as it was. A GAMMA update copies its loose files
and exes back; ``after_gamma_update()`` cleans up again when the player kept
their exes.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from . import __version__, network
from .mod_install import ModInstallError, extract_archive
from .network import read_response_bytes

_RELEASES_API = "https://api.github.com/repos/themrdemonized/xray-monolith/releases"
_USER_AGENT = f"CommanderGUI/{__version__}"
_ASSET_RE = re.compile(r"^STALKER-Anomaly-modded-exes-MT-TEST_[\d.]+\.zip$")
_TAG_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}$")
_MAX_API_BYTES = 4 * 1024 * 1024
_MAX_ZIP_BYTES = 1024**3

#: The release COMMANDER marks as tested. None until one has been played on.
RECOMMENDED: str | None = None

GAMEDATA_DB = "00_modded_exes_gamedata.db0"
_KEEP_CONFIGS = {"axr_options.ltx", "cache_dbg.ltx"}
_DROP_FOLDERS = ("materials", "scripts", "shaders")
_STATE_DIR = "commander-exes"


class ExesError(RuntimeError):
    pass


# -- versions -----------------------------------------------------------------
def fetch_releases(count: int = 30) -> list[dict]:
    """Recent releases with an MT-TEST zip, newest first.

    Each has ``tag``, ``published`` (ISO date), ``url``, ``size`` and
    ``sha256`` (GitHub's digest of the zip, "" when it has none).
    """
    req = urllib.request.Request(
        f"{_RELEASES_API}?per_page={min(100, max(1, count))}",
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with network.urlopen_with_retry(req, timeout=30) as resp:
        data = json.loads(read_response_bytes(resp, _MAX_API_BYTES))
    out = []
    for release in data:
        tag = release.get("tag_name", "")
        asset = next((a for a in release.get("assets", []) if _ASSET_RE.match(a.get("name", ""))), None)
        if not _TAG_RE.match(tag) or asset is None or release.get("draft"):
            continue
        digest = asset.get("digest") or ""
        out.append({
            "tag": tag,
            "published": (release.get("published_at") or "")[:10],
            "url": asset.get("browser_download_url", ""),
            "size": int(asset.get("size") or 0),
            "sha256": digest.removeprefix("sha256:") if digest.startswith("sha256:") else "",
        })
    return out


def build_date(anomaly: str | Path) -> str | None:
    """Build date of ``bin/AnomalyDX11.exe`` (from its PE header), or None."""
    try:
        with open(Path(anomaly) / "bin" / "AnomalyDX11.exe", "rb") as exe:
            head = exe.read(4096)
        offset = struct.unpack_from("<I", head, 0x3C)[0]
        if head[offset:offset + 4] != b"PE\0\0":
            return None
        stamp = struct.unpack_from("<I", head, offset + 8)[0]
    except (OSError, struct.error):
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()


def at_least(anomaly: str | Path, tag: str) -> bool:
    """True if the installed exes are release ``tag`` (``YYYY.M.D``) or newer.

    Uses COMMANDER's record when it installed a release, else the exe's build
    date (the 2026.9.12 exe is stamped 2026-09-12, the release day)."""
    def _key(text: str) -> tuple[int, ...]:
        return tuple(int(part) for part in re.split(r"[.-]", text))

    version = (load_state(anomaly) or {}).get("version")
    if not (isinstance(version, str) and _TAG_RE.match(version)):
        version = build_date(anomaly)
    return version is not None and _key(version) >= _key(tag)


def load_state(anomaly: str | Path) -> dict | None:
    """``{"version", "previous"}`` when COMMANDER installed the exes.

    ``version``/``previous`` are a release tag, ``"file:<zip name>"``, or
    None for "what was there before COMMANDER" (``previous`` only).
    """
    try:
        state = json.loads((Path(anomaly) / _STATE_DIR / "state.json").read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _save_state(anomaly: Path, state: dict) -> None:
    (anomaly / _STATE_DIR / "state.json").write_text(json.dumps(state))


# -- download -----------------------------------------------------------------
def download(release: dict, dest_dir: Path, progress=None, cancel_event: Event | None = None) -> Path:
    """Download a release zip into ``dest_dir`` and check its SHA-256."""
    url = release["url"]
    if not url.startswith("https://github.com/"):
        raise ExesError(f"Unexpected download address: {url}")
    if not release.get("sha256"):
        raise ExesError("GitHub lists no checksum for this release, so it can't be checked.")
    target = dest_dir / url.rsplit("/", 1)[-1]
    digest = hashlib.sha256()
    done = 0
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with network.urlopen_with_retry(req, timeout=600) as resp, open(target, "wb") as out:
        while chunk := resp.read(1 << 20):
            if cancel_event is not None and cancel_event.is_set():
                raise ExesError("Download cancelled")
            done += len(chunk)
            if done > _MAX_ZIP_BYTES:
                raise ExesError("The download is larger than a modded exes release can be.")
            digest.update(chunk)
            out.write(chunk)
            if progress:
                progress(done, release.get("size") or 0)
    if digest.hexdigest() != release["sha256"].lower():
        raise ExesError("The download doesn't match GitHub's checksum. Try again.")
    return target


# -- install ------------------------------------------------------------------
def _clean_targets(anomaly: Path) -> list[Path]:
    """What the guide's clean-up removes from the Anomaly folder."""
    found = []
    db = anomaly / "db" / "mods"
    if db.is_dir():
        found += sorted(db.iterdir())
    configs = anomaly / "gamedata" / "configs"
    if configs.is_dir():
        found += sorted(p for p in configs.iterdir() if p.name.lower() not in _KEEP_CONFIGS)
    found += [anomaly / "gamedata" / name for name in _DROP_FOLDERS if (anomaly / "gamedata" / name).exists()]
    return found


def _move(path: Path, anomaly: Path, backup: Path) -> None:
    target = backup / path.relative_to(anomaly)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), target)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _check_layout(staging: Path) -> None:
    if not (staging / "bin" / "AnomalyDX11.exe").is_file() or not (staging / "db" / "mods" / GAMEDATA_DB).is_file():
        raise ExesError("This zip isn't a modded exes release: it needs bin/AnomalyDX11.exe and db/mods/" + GAMEDATA_DB + ".")
    extra = {p.name for p in staging.iterdir()} - {"bin", "db"}
    if extra:
        raise ExesError(f"This zip has more than modded exes in it: {', '.join(sorted(extra))}")


def install(zip_path: Path, anomaly: str | Path, label: str, cancel_event: Event | None = None) -> None:
    """Install a modded exes zip. ``label`` is its release tag or ``file:<name>``."""
    from . import coop

    anomaly = Path(anomaly)
    if not (anomaly / "bin").is_dir():
        raise ExesError("The Anomaly folder has no bin folder.")
    if coop.is_active(anomaly):
        # Co-op's engine is in place and its backups hold the singleplayer
        # files. Switched off is fine: switching on backs up afresh.
        raise ExesError("Co-op is on. Switch to singleplayer on the Multiplayer page first.")
    state = load_state(anomaly)
    root = anomaly / _STATE_DIR
    root.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".exes-", dir=root))
    try:
        extract_archive(zip_path, staging / "zip", cancel_event)
        _check_layout(staging / "zip")
        if cancel_event is not None and cancel_event.is_set():
            raise ModInstallError("Mod installation cancelled")
        original = root / "original"
        first = not original.exists()
        added = set(state.get("added", [])) if state else set()
        if first:
            # Everything the zip replaces or the clean-up removes, so
            # restore_original() can put the folder back as it was.
            original.mkdir()
            (original / "bin").mkdir()
        for exe in (staging / "zip" / "bin").iterdir():
            current = anomaly / "bin" / exe.name
            if first and current.is_file():
                shutil.copy2(current, original / "bin" / exe.name)
            elif not (original / "bin" / exe.name).exists():
                added.add(exe.name)  # no original: restore deletes it
        for path in _clean_targets(anomaly):
            if first:
                _move(path, anomaly, original)
            else:
                # Only what a GAMMA update copied back since: the
                # originals are already saved.
                _remove(path)
        for path in sorted((staging / "zip").rglob("*")):
            if path.is_file():
                target = anomaly / path.relative_to(staging / "zip")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), target)
        _save_state(anomaly, {
            "version": label,
            "previous": state.get("version") if state else None,
            "added": sorted(added),
        })
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def restore_original(anomaly: str | Path) -> None:
    """Put back what was there before COMMANDER installed modded exes."""
    from . import coop

    anomaly = Path(anomaly)
    if coop.is_active(anomaly):
        raise ExesError("Co-op is on. Switch to singleplayer on the Multiplayer page first.")
    root = anomaly / _STATE_DIR
    original = root / "original"
    if not original.is_dir():
        raise ExesError("There is no saved copy of the original exes.")
    for path in _clean_targets(anomaly):
        _remove(path)
    for name in (load_state(anomaly) or {}).get("added", []):
        if Path(name).name == name:
            (anomaly / "bin" / name).unlink(missing_ok=True)
    for path in sorted(original.rglob("*"), reverse=True):
        if path.is_file():
            target = anomaly / path.relative_to(original)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), target)
    for path in sorted(original.rglob("*"), reverse=True):
        if path.is_dir():
            path.rmdir()  # empty folders once their files have moved back
    shutil.rmtree(root)


def after_gamma_update(anomaly: str | Path, kept_exes: bool) -> None:
    """Fix up the folder after a GAMMA update that may have copied its own
    exes and loose gamedata back.

    Kept exes: redo the clean-up, and keep the ``.db0`` (saved by the caller
    across the update). Not kept: GAMMA's exes are back, so COMMANDER's
    record and backup no longer describe the folder; drop them and the
    ``.db0``, whose loose counterparts GAMMA just reinstalled.
    """
    anomaly = Path(anomaly)
    if load_state(anomaly) is None:
        return
    if kept_exes:
        for path in _clean_targets(anomaly):
            if path.name != GAMEDATA_DB:
                _remove(path)
    else:
        (anomaly / "db" / "mods" / GAMEDATA_DB).unlink(missing_ok=True)
        shutil.rmtree(anomaly / _STATE_DIR, ignore_errors=True)
