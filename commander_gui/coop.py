"""xrRazom Co-op (Slim): install, co-op/single-player switch, settings.

The xrRazom zip has two halves:

* ``Anomaly/**`` - replacement engine exes, Steam/GNS DLLs and loose
  gamedata that must sit in the Anomaly folder itself. MO2 cannot switch
  these off, so COMMANDER owns them: the co-op copies live in
  ``<anomaly>/commander-coop/files`` and every original they replace is kept
  in ``<anomaly>/commander-coop/backup`` so single-player can be restored.
  Kept beside the files they guard, so a GAMMA-only reset cannot delete the
  backups while the co-op engine stays in place.
* ``gamedata/**`` - an ordinary MO2 mod, installed as ``xrRazom Co-op`` at
  the highest priority and enabled/disabled together with the engine files.
  A plain Anomaly install (``KIND_ANOMALY``, no MO2) gets these loose in the
  Anomaly folder instead, as more co-op files beside the engine half's.

Co-op settings are the mod's console variables in ``appdata/user.ltx``.
The single-player engine drops console variables it doesn't know when it
saves user.ltx, so a copy is kept in the state file and put back on the
next switch to co-op.

user.ltx and the MCM files (axr_options.ltx) are kept apart for singleplayer
and co-op: each switch saves the side being left under ``modes/`` in the
state folder and puts the other side's copy back. A side never saved yet
inherits the current files.
"""

from __future__ import annotations

import configparser
import functools
import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from . import atomic, modlist, network
from .game_backup import (
    MANIFEST,
    MCM_FILE,
    BackupError,
    _safe_member,
    _write_zip,
    create_backup_of_files,
    mcm_files,
    user_ltx_path,
)
from .game_stats import save_folders
from .mod_install import write_basic_meta_ini

MOD_NAME = "xrRazom Co-op"
MODDB_PAGE = "https://www.moddb.com/mods/stalker-anomaly/addons/xrrazom-stalker-anomaly-co-op-slim"
MODDB_MOD_PAGE = "https://www.moddb.com/mods/xrrazom-stalker-anomaly-co-op"
DISCORD_URL = "https://discord.gg/mpXUXjTQu8"
RELEASE_FILE = "xrRazom-release.txt"
_MAX_UNPACKED = 16 << 30  # a zip claiming more than this is a bomb, not a mod
_ENGINE_ROOT = "Anomaly/"
_MOD_ROOTS = ("gamedata/", "fomod/")

#: Steam app a Steam session runs as. Everyone in a session must use the same
#: one: Call of Pripyat (what xrRazom ships) needs the game on each account;
#: Spacewar, Valve's free Steamworks test app, needs nothing.
COP_APPID, SPACEWAR_APPID = 41700, 480

NOT_INSTALLED, COOP, SINGLEPLAYER, REVERTED = "not_installed", "coop", "singleplayer", "reverted"

#: How the gamedata half is installed: as an MO2 mod for GAMMA, or loose in
#: the Anomaly folder for plain Anomaly. A state without "kind" is GAMMA's.
KIND_GAMMA, KIND_ANOMALY = "gamma", "anomaly"


class CoopError(RuntimeError):
    pass


# -- paths / state ---------------------------------------------------------------
def state_dir(anomaly: str | Path) -> Path:
    return Path(anomaly).expanduser() / "commander-coop"


def _state_file(anomaly) -> Path:
    return state_dir(anomaly) / "state.json"


def load_state(anomaly) -> dict | None:
    try:
        state = json.loads(_state_file(anomaly).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # A hand-edited file holding a list or a string is no state at all.
    return state if isinstance(state, dict) else None


def install_kind(anomaly) -> str:
    return KIND_ANOMALY if (load_state(anomaly) or {}).get("kind") == KIND_ANOMALY else KIND_GAMMA


def _save_state(anomaly, state: dict) -> None:
    atomic.write_text(_state_file(anomaly), json.dumps(state, indent=1, sort_keys=True))


def _sha256(path: Path) -> str:
    # status() runs on the GUI thread (Dashboard, Multiplayer, every launch)
    # and hashes the engine exes each time; a file that has not changed
    # since the last look keeps its digest.
    st = os.stat(path)
    return _sha256_cached(os.fspath(path), st.st_mtime_ns, st.st_size, st.st_ino)


@functools.lru_cache(maxsize=256)
def _sha256_cached(path: str, _mtime: int, _size: int, _ino: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_ci(root: Path, rel: str) -> Path:
    """``root/rel``, reusing any existing component whose name differs only
    by case. Wine sees the Anomaly folder case-insensitively, so writing
    ``bin/AnomalyDX11.exe`` beside an existing ``bin/anomalydx11.exe`` would
    leave two files the game cannot tell apart."""
    current = root
    for part in PurePosixPath(rel).parts:
        candidate = current / part
        if not candidate.exists() and current.is_dir():
            lowered = part.lower()
            for child in current.iterdir():
                if child.name.lower() == lowered:
                    candidate = child
                    break
        current = candidate
    return current


def _copy_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{dst.name}.", suffix=".tmp")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        shutil.copymode(src, tmp)  # mkstemp makes it 0600; keep the source's mode
        os.replace(tmp, dst)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# -- zip -------------------------------------------------------------------------
def parse_release(text: str) -> dict:
    """``xrRazom-release.txt`` -> {"title", "protocol", "engine", ...}."""
    lines = [line.strip() for line in text.lstrip("﻿").splitlines() if line.strip()]
    info = {"title": lines[0] if lines else ""}
    for line in lines[1:]:
        key, _, value = line.partition(" ")
        info[key.lower()] = value.strip()
    match = re.search(r"(\d+(?:\.\d+)+)", info["title"])
    info["version"] = match.group(1) if match else ""
    return info


def _check_unpacked_size(archive: zipfile.ZipFile) -> None:
    if sum(i.file_size for i in archive.infolist()) > _MAX_UNPACKED:
        raise CoopError("This archive unpacks to an unreasonable size.")


def _swap_dir(new: Path, dest: Path) -> None:
    """Put ``new`` at ``dest``; the old ``dest`` comes back if the move fails."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    had_old = dest.exists()
    if had_old:
        os.replace(dest, old)
    try:
        shutil.move(str(new), dest)  # may cross filesystems
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        if had_old:
            os.replace(old, dest)
        raise
    shutil.rmtree(old, ignore_errors=True)


def looks_like_xrrazom(names: list[str]) -> bool:
    """True when an archive's member paths are an xrRazom Co-op zip
    (engine ``Anomaly/bin`` plus ``gamedata``), so Mod Manager can send it
    to the Multiplayer page before unpacking anything.

    Goes by layout, never the name: an addon called "xrRazom something"
    ships only ``gamedata`` and installs as a normal mod. One wrapper
    folder around the whole thing is looked through."""
    parts = [n.replace("\\", "/").strip("/").lower().split("/") for n in names if n.strip("/")]
    if len({p[0] for p in parts}) == 1:
        parts = [p[1:] for p in parts if len(p) > 1]
    return any(p[:2] == ["anomaly", "bin"] for p in parts) and any(p[0] == "gamedata" for p in parts)


def inspect_zip(path: str | Path) -> dict:
    """Validate an xrRazom zip and return its release info."""
    try:
        with zipfile.ZipFile(path) as archive:
            _check_unpacked_size(archive)
            names = [n for n in archive.namelist() if not n.endswith("/")]
            bad = [n for n in names if _safe_member(n) is None]
            if bad:
                raise CoopError(f"Unsafe path in archive: {bad[0]}")
            if not any(n.startswith(_ENGINE_ROOT + "bin/") for n in names) or not any(
                n.startswith("gamedata/") for n in names
            ):
                raise CoopError("This is not an xrRazom Co-op archive (no Anomaly/bin or gamedata folder).")
            release = archive.read(RELEASE_FILE).decode("utf-8", "replace") if RELEASE_FILE in names else ""
    except zipfile.BadZipFile as exc:
        raise CoopError(f"Not a valid zip file: {exc}") from exc
    return parse_release(release)


def find_downloaded_zip() -> Path | None:
    """Newest xrRazom zip in ~/Downloads (or the Desktop), if any."""
    found = []
    for folder in (Path.home() / "Downloads", Path.home() / "Desktop"):
        try:
            found += [(p.stat().st_mtime, p) for p in folder.glob("*.zip") if "xrrazom" in p.name.lower() and p.is_file()]
        except OSError:
            continue
    return max(found, default=(0, None))[1]


# -- modlist ---------------------------------------------------------------------
def _modlist(gamma, mo2_profile) -> Path:
    path = modlist.modlist_path_for(str(gamma), mo2_profile)
    if path is None or not path.exists():
        raise CoopError("The MO2 profile's modlist.txt was not found.")
    return path


def _set_mod_enabled(gamma, mo2_profile, enabled: bool) -> None:
    if not enabled and not _has_modlist(gamma, mo2_profile):
        return  # no modlist, nothing to switch off; the engine files still go back
    path = _modlist(gamma, mo2_profile)
    lines = modlist.read_lines(path)
    index = next((i for i, line in enumerate(lines) if line.strip()[1:].strip() == MOD_NAME), None)
    if index is not None:
        lines.pop(index)
    if enabled or index is not None:
        # First mod line = highest priority (MO2 writes the list reversed).
        modlist.backup_before_change(path)
        lines.insert(modlist._first_entry_index(lines), f"{'+' if enabled else '-'}{MOD_NAME}")
        modlist.save_lines(path, lines)


def _has_modlist(gamma, mo2_profile) -> bool:
    path = modlist.modlist_path_for(str(gamma), mo2_profile)
    return path is not None and path.exists()


def _remove_mod_entry(gamma, mo2_profile) -> None:
    if not _has_modlist(gamma, mo2_profile):
        return
    path = _modlist(gamma, mo2_profile)
    lines = modlist.read_lines(path)
    kept = [line for line in lines if line.strip()[1:].strip() != MOD_NAME]
    if kept != lines:
        modlist.backup_before_change(path)
        modlist.save_lines(path, kept)


# -- install / switch ------------------------------------------------------------
def installed_zip(anomaly, gamma) -> str:
    """The zip co-op was installed from, or "" when unknown."""
    state = load_state(anomaly) or {}
    if state.get("zip"):
        return state["zip"]
    if not str(gamma).strip():
        return ""
    # Installs from before "zip" was saved: GAMMA ones have it in meta.ini.
    meta = configparser.ConfigParser(interpolation=None)
    try:
        meta.read(Path(gamma).expanduser() / "mods" / MOD_NAME / "meta.ini", encoding="utf-8")
    except (OSError, configparser.Error):
        return ""
    # A switch back from Anomaly wrote the mod's name there instead.
    name = meta.get("General", "installationFile", fallback="")
    return name if name.lower().endswith(".zip") else ""


def install(zip_path: str | Path, anomaly, gamma, mo2_profile, progress=None, kind: str = KIND_GAMMA) -> dict:
    """Install (or update) from ``zip_path``.

    A first install stays in singleplayer until the player switches to co-op;
    an update keeps whichever mode was on. ``KIND_ANOMALY`` puts the
    gamedata half loose in the Anomaly folder and never touches ``gamma``,
    which a plain Anomaly profile may leave empty.
    """
    info = inspect_zip(zip_path)
    if kind == KIND_GAMMA and not str(gamma).strip():
        raise CoopError("Pick a GAMMA folder first.")
    root = state_dir(anomaly)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".coop-", dir=root))
    try:
        files: dict[str, str] = {}
        engine_rels: set[str] = set()
        mod_rels: set[str] = set()
        with zipfile.ZipFile(zip_path) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            for count, member in enumerate(members, 1):
                name = member.filename
                if _safe_member(name) is None:
                    raise CoopError(f"Unsafe path in archive: {name}")
                if name.startswith(_ENGINE_ROOT):
                    rel = name[len(_ENGINE_ROOT):]
                    target = staging / "files" / rel
                    engine_rels.add(rel)
                elif kind == KIND_ANOMALY and name.startswith("gamedata/"):
                    rel = name
                    target = staging / "files" / rel
                    mod_rels.add(rel)
                elif kind == KIND_GAMMA and name.startswith(_MOD_ROOTS):
                    rel = None
                    target = staging / "mod" / name
                else:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                if rel is not None:
                    files[rel] = _sha256(target)
                if progress:
                    progress(count, len(members))
        previous = load_state(anomaly)
        if previous and previous.get("active"):
            # Put the originals back first: the new version may ship a
            # different file set, and backups are keyed to the old one.
            switch_to_singleplayer(anomaly, gamma, mo2_profile)
        if kind == KIND_GAMMA:
            write_basic_meta_ini(staging / "mod", Path(zip_path).name)
            _swap_dir(staging / "mod", Path(gamma).expanduser() / "mods" / MOD_NAME)
        elif previous and previous.get("kind", KIND_GAMMA) == KIND_GAMMA:
            _remove_mod(gamma, mo2_profile)  # reinstalled for plain Anomaly
        shutil.rmtree(root / "files", ignore_errors=True)
        os.replace(staging / "files", root / "files")
        state = {"version": info.get("version", ""), "release": info, "files": files, "zip": Path(zip_path).name,
                 "originals": [], "active": False, "kind": kind,
                 "slots_lifted": bool((previous or {}).get("slots_lifted")),
                 "host_default": bool((previous or {}).get("host_default"))}
        if kind == KIND_ANOMALY:
            # Which co-op files are the mod's, for set_install_kind(). One
            # the engine half ships too stays an engine file.
            state["mod_files"] = sorted(mod_rels - engine_rels)
        _save_state(anomaly, state)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    # An update ships xrRazom's own appid; keep the one the player picked.
    set_steam_appid(anomaly, (previous or {}).get("appid", COP_APPID))
    if previous and previous.get("active"):
        switch_to_coop(anomaly, gamma, mo2_profile)
    return info


_APPID = "steam_appid.txt"


def _add_root_appid(state: dict, root: Path) -> None:
    """Also place xrRazom's ``bin/steam_appid.txt`` in the Anomaly folder.

    The game runs with the Anomaly folder as its working directory, which
    is where steam_api looks for it; in ``bin/`` alone Steam init failed
    ("No appID found") and the mod fell back to LAN-only networking. Added
    as one more co-op file, so backup, restore and status cover it too.
    """
    files = state["files"]
    source = next((rel for rel in files if rel.lower() == "bin/" + _APPID), None)
    if source is None or any(rel.lower() == _APPID for rel in files):
        return
    _copy_atomic(root / "files" / source, root / "files" / _APPID)
    files[_APPID] = files[source]


def prepare_launch(anomaly, gamma, mo2_profile) -> None:
    """Get a launch with *mo2_profile* ready for the current mode.

    An install made before ``_add_root_appid`` gets the root appid. The
    co-op mod follows the mode in this profile too: switch_to_coop and
    switch_to_singleplayer only change the profile active then, so one
    picked later (ROMEO) started without co-op, or solo with the mod on.
    """
    state = load_state(anomaly)
    if not state:
        return
    active = bool(state.get("active"))
    if active and not any(rel.lower() == _APPID for rel in state["files"]):
        switch_to_coop(anomaly, gamma, mo2_profile)
    elif state.get("kind", KIND_GAMMA) == KIND_GAMMA and _has_modlist(gamma, mo2_profile):
        lines = modlist.read_lines(_modlist(gamma, mo2_profile))
        if (f"+{MOD_NAME}" in (line.strip() for line in lines)) != active:
            _set_mod_enabled(gamma, mo2_profile, active)


def steam_appid(anomaly) -> int:
    try:
        return int((load_state(anomaly) or {}).get("appid", COP_APPID))
    except (TypeError, ValueError):
        return COP_APPID  # a hand-edited or corrupt "appid"


def set_steam_appid(anomaly, appid: int) -> None:
    """Make Steam sessions run as ``appid`` (``COP_APPID`` or ``SPACEWAR_APPID``).

    Only the steam_appid.txt files change. A copy in the Anomaly folder that
    still holds the co-op version (or is missing) is replaced in place, so the
    backups stay untouched and switching back and forth needs no full re-apply.
    """
    state = load_state(anomaly)
    if state is None:
        raise CoopError("xrRazom Co-op is not installed.")
    anomaly = Path(anomaly).expanduser()
    root = state_dir(anomaly)
    _add_root_appid(state, root)
    for rel, digest in list(state["files"].items()):
        if PurePosixPath(rel).name.lower() != _APPID:
            continue
        stored = root / "files" / rel
        atomic.write_text(stored, str(int(appid)))
        target = _resolve_ci(anomaly, rel)
        if state.get("active") and (not target.is_file() or _sha256(target) == digest):
            _copy_atomic(stored, target)
        state["files"][rel] = _sha256(stored)
    state["appid"] = int(appid)
    _save_state(anomaly, state)


def switch_to_coop(anomaly, gamma, mo2_profile) -> None:
    """Back up whatever is in the Anomaly folder now, then copy co-op in."""
    state = load_state(anomaly)
    if state is None:
        raise CoopError("xrRazom Co-op is not installed.")
    anomaly = Path(anomaly).expanduser()
    root = state_dir(anomaly)
    if not state.get("active"):
        _save_mode(anomaly, gamma, "solo")
    _add_root_appid(state, root)
    originals = set(state.get("originals", []))
    done: list[str] = []
    try:
        for rel, digest in state["files"].items():
            target = _resolve_ci(anomaly, rel)
            if target.is_file():
                # From singleplayer every file on disk is an original, even
                # one identical to the co-op copy (GAMMA ships some of the
                # same Modded Exes files): skipping it made switching back
                # delete it as if co-op had added it.
                if _sha256(target) == digest and (state.get("active") or rel in originals):
                    continue  # already co-op; keep the backup we have
                # Re-captured every time: a GAMMA update since the last
                # switch may have shipped a newer original.
                _copy_atomic(target, root / "backup" / rel)
                originals.add(rel)
            else:
                originals.discard(rel)
            _copy_atomic(root / "files" / rel, target)
            done.append(rel)
    except BaseException:
        _restore(anomaly, root, done, originals)
        raise
    state["originals"] = sorted(originals)
    state["active"] = True
    _save_state(anomaly, state)
    if state.get("kind", KIND_GAMMA) == KIND_GAMMA:
        _set_mod_enabled(gamma, mo2_profile, True)
    _load_mode(anomaly, gamma, "coop")
    # The first switch has no co-op copy yet, and a solo session may have
    # wiped the xrr_* lines.
    values, present = read_settings(anomaly)
    missing = {key: values[key] for key in state.get("settings", {}) if key not in present}
    if missing:
        write_settings(anomaly, missing)


def _mode_files(anomaly, gamma) -> dict[str, Path]:
    """The per-mode settings files by snapshot name: user.ltx and every MCM file."""
    files = {"user.ltx": user_ltx_path(anomaly)}
    if install_kind(anomaly) == KIND_ANOMALY:  # plain Anomaly: no MO2, one MCM file
        files["anomaly/" + MCM_FILE] = Path(anomaly).expanduser() / "gamedata" / "configs" / MCM_FILE
    if str(gamma).strip():
        files.update({"mcm/" + rel: path for rel, path in mcm_files(gamma)})
    return files


def _is_mode_file(name: str) -> bool:
    """A snapshot name ``_mode_files()`` can produce. An imported export is
    someone else's file: anything else in its ``coop/`` folder would be
    copied over GAMMA's own files."""
    parts = PurePosixPath(name).parts
    if name in ("user.ltx", "anomaly/" + MCM_FILE):
        return True
    tail = ("gamedata", "configs", MCM_FILE)
    if parts[:2] == ("mcm", "overwrite"):
        return parts[2:] == tail
    return len(parts) == 6 and parts[:2] == ("mcm", "mods") and parts[3:] == tail and parts[2] not in ("", ".", "..")


def _created_by_game(name: str) -> bool:
    """An MCM file the game or MO2 writes on first use, not one a mod ships:
    missing from a side's snapshot means that side never had it."""
    return name == "anomaly/" + MCM_FILE or name.startswith("mcm/overwrite/")


def _save_mode(anomaly, gamma, mode: str) -> None:
    dest = state_dir(anomaly) / "modes" / mode
    dest.mkdir(parents=True, exist_ok=True)  # an empty snapshot still says "none of these"
    for name, path in _mode_files(anomaly, gamma).items():
        if path.is_file():
            _copy_atomic(path, dest / name)
        elif _created_by_game(name):
            (dest / name).unlink(missing_ok=True)


def _load_mode(anomaly, gamma, mode: str) -> None:
    src = state_dir(anomaly) / "modes" / mode
    if not src.is_dir():
        return
    # A file the other side created (MO2 writes a new MCM file to overwrite/)
    # would carry its values over; the side being left already saved it.
    for name, path in _mode_files(anomaly, gamma).items():
        if _created_by_game(name) and not (src / name).is_file():
            path.unlink(missing_ok=True)
    root = Path(gamma).expanduser() if str(gamma).strip() else None
    for saved in src.rglob("*"):
        name = saved.relative_to(src).as_posix()
        if not saved.is_file() or not _is_mode_file(name):
            continue
        if name == "user.ltx":
            target = user_ltx_path(anomaly)
        elif name == "anomaly/" + MCM_FILE:
            target = Path(anomaly).expanduser() / "gamedata" / "configs" / MCM_FILE
            if not target.parent.is_dir():
                continue
        elif root is not None and name.startswith("mcm/"):
            target = root / name[len("mcm/"):]
            if not target.parent.is_dir():
                continue  # the mod was removed since; don't recreate it
        else:
            continue
        _copy_atomic(saved, target)


def _restore(anomaly: Path, root: Path, rels, originals) -> None:
    for rel in rels:
        target = _resolve_ci(anomaly, rel)
        if rel in originals:
            _copy_atomic(root / "backup" / rel, target)
        else:
            target.unlink(missing_ok=True)


def switch_to_singleplayer(anomaly, gamma, mo2_profile) -> None:
    """Put the originals back and remove what co-op added.

    A file that no longer holds the co-op copy (a GAMMA update or repair
    already replaced it) is left alone: restoring the older backup over it
    would undo that update.
    """
    state = load_state(anomaly)
    if state is None:
        return
    anomaly = Path(anomaly).expanduser()
    # Already off (uninstall from singleplayer): the live user.ltx and MCM
    # files are the singleplayer side, newer than the copy under modes/.
    leaving_coop = bool(state.get("active"))
    if leaving_coop:
        _save_mode(anomaly, gamma, "coop")
    originals = set(state.get("originals", []))
    ours = [
        rel for rel, digest in state["files"].items()
        if (t := _resolve_ci(anomaly, rel)).is_file() and _sha256(t) == digest
    ]
    _restore(anomaly, state_dir(anomaly), ours, originals)
    values, present = read_settings(anomaly)
    state["settings"] = {**state.get("settings", {}), **{key: _format(key, values[key]) for key in present}}
    state["active"] = False
    _save_state(anomaly, state)
    if leaving_coop:
        _load_mode(anomaly, gamma, "solo")
    if state.get("kind", KIND_GAMMA) == KIND_GAMMA:
        _set_mod_enabled(gamma, mo2_profile, False)


def set_install_kind(anomaly, gamma, mo2_profile, kind: str) -> None:
    """Move the gamedata half between GAMMA's MO2 mod and the Anomaly folder,
    from the copies kept here, so no zip is needed. Co-op stays on or off."""
    state = load_state(anomaly)
    if state is None:
        raise CoopError("xrRazom Co-op is not installed.")
    if state.get("kind", KIND_GAMMA) == kind:
        return
    was_active = bool(state.get("active"))
    if was_active:
        switch_to_singleplayer(anomaly, gamma, mo2_profile)
        state = load_state(anomaly)
    root = state_dir(anomaly)
    mod_dir = Path(gamma).expanduser() / "mods" / MOD_NAME
    if kind == KIND_ANOMALY:
        source = mod_dir / "gamedata"
        if not str(gamma).strip() or not source.is_dir():
            raise CoopError("The xrRazom Co-op mod is missing from GAMMA's mods folder. Install the zip again.")
        moved = ["gamedata/" + f.relative_to(source).as_posix() for f in sorted(source.rglob("*")) if f.is_file()]
        if any(rel in state["files"] for rel in moved):
            # Switching back would take the engine half's copy with it.
            raise CoopError("This xrRazom edition can't be switched. Install the zip again for Anomaly.")
        for rel in moved:
            _copy_atomic(source / rel[len("gamedata/"):], root / "files" / rel)
            state["files"][rel] = _sha256(root / "files" / rel)
        state.update(kind=KIND_ANOMALY, mod_files=moved)
        _save_state(anomaly, state)
        _remove_mod(gamma, mo2_profile)
    else:
        moved = state.get("mod_files")
        if moved is None:
            raise CoopError("This co-op install is too old to switch. Install the zip again for GAMMA.")
        staging = Path(tempfile.mkdtemp(prefix=".coop-", dir=root))
        try:
            for rel in moved:
                _copy_atomic(root / "files" / rel, staging / rel)
            write_basic_meta_ini(staging, state.get("zip") or (state.get("release") or {}).get("title") or MOD_NAME)
            _swap_dir(staging, mod_dir)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        for rel in moved:
            state["files"].pop(rel, None)
            (root / "files" / rel).unlink(missing_ok=True)
        state.pop("mod_files")
        state["kind"] = KIND_GAMMA
        _save_state(anomaly, state)
    if was_active:
        switch_to_coop(anomaly, gamma, mo2_profile)


def _remove_mod(gamma, mo2_profile) -> None:
    if not str(gamma).strip():
        return  # plain Anomaly profile: no MO2 folder, and Path("") is the cwd
    _remove_mod_entry(gamma, mo2_profile)
    shutil.rmtree(Path(gamma).expanduser() / "mods" / MOD_NAME, ignore_errors=True)


def uninstall(anomaly, gamma, mo2_profile) -> None:
    if not str(anomaly).strip():
        raise CoopError("Pick an Anomaly folder first.")  # Path("") is the cwd
    was = install_kind(anomaly)
    switch_to_singleplayer(anomaly, gamma, mo2_profile)
    if was == KIND_GAMMA:
        _remove_mod(gamma, mo2_profile)
    shutil.rmtree(state_dir(anomaly), ignore_errors=True)


def status(anomaly) -> str:
    state = load_state(anomaly)
    if state is None:
        return NOT_INSTALLED
    if not state.get("active"):
        return SINGLEPLAYER
    anomaly = Path(anomaly).expanduser()
    for rel, digest in state["files"].items():
        target = _resolve_ci(anomaly, rel)
        if not target.is_file() or _sha256(target) != digest:
            return REVERTED
    return COOP


def engine_changed(anomaly) -> bool:
    """True when the files that replaced co-op differ from the backed-up originals.

    A repair puts the same originals back; a different file means GAMMA
    shipped a new engine the installed xrRazom may not have been built for.
    """
    state = load_state(anomaly)
    if state is None:
        return False
    anomaly = Path(anomaly).expanduser()
    root = state_dir(anomaly)
    for rel in state.get("originals", []):
        target = _resolve_ci(anomaly, rel)
        backup = root / "backup" / rel
        if target.is_file() and _sha256(target) != state["files"].get(rel) and backup.is_file() \
                and _sha256(target) != _sha256(backup):
            return True
    return False


def is_active(anomaly) -> bool:
    state = load_state(anomaly)
    return bool(state and state.get("active"))


def launch_block_reason(anomaly, *, coop_launch: bool, direct: bool = False) -> str | None:
    """Why this launch must not start, or None. Co-op on means co-op only, off means solo only.

    The engine files on disk decide what actually runs, so a "solo" launch with
    co-op applied would start the co-op engine. ``direct`` is a launch without
    MO2: GAMMA's co-op needs MO2 for the mod's gamedata, Anomaly's has
    it loose and must not run under GAMMA's mods.
    """
    try:
        mode = status(anomaly)
    except OSError:
        mode = NOT_INSTALLED
    if mode == REVERTED:
        return "A GAMMA update or Anomaly repair put the original game files back over co-op. Open the Multiplayer page to re-apply co-op or switch to singleplayer."
    if mode == COOP and install_kind(anomaly) == KIND_ANOMALY:
        if not direct:
            return "Co-op is set up for Anomaly. Switch co-op off on the Multiplayer page to play GAMMA."
    elif mode == COOP and direct:
        return "Co-op is on, and co-op only works through Mod Organizer 2. Use Launch Co-op instead."
    if mode == COOP and not coop_launch:
        return "Co-op is on. Start co-op from the Multiplayer page, or switch back to singleplayer there to play solo."
    if mode != COOP and coop_launch:
        return "Co-op is off. Turn it on from the Multiplayer page first."
    return None


# -- settings (user.ltx) ---------------------------------------------------------
#: key -> (kind, fallback shown before the game has written its defaults).
#: Fallbacks are the engine's compiled defaults (xrRazom 1.4 Slim exe).
SETTINGS: dict[str, tuple[str, object]] = {
    "xrr_player_name": ("str", "Player"),
    "xrr_show_marks": ("bool", True),
    "xrr_show_names": ("bool", True),
    "xrr_show_players_hp": ("bool", True),
    "xrr_show_net_health": ("bool", True),
    "xrr_tag_duration": ("num", 60),
    "xrr_chat_enable": ("bool", True),
    "xrr_chat_sound": ("bool", True),
    "xrr_host_session": ("bool", False),
    "xrr_host_steam": ("bool", True),
    "xrr_host_lan_port": ("num", 27015),
    "xrr_max_players": ("num", 3),
    "xrr_skip_mod_check": ("bool", False),
    "xrr_friendly_fire": ("bool", False),
    "xrr_rescue": ("bool", True),
    "xrr_rescue_medkit": ("bool", True),
    "xrr_allow_peer_save_load": ("bool", True),
    "xrr_sync_teleport": ("bool", True),
    "xrr_teleport_radius": ("num", 30),
    "xrr_party_fast_travel": ("bool", True),
    "xrr_gather_radius": ("num", 10),
}


def _parse(key: str, raw: str):
    """A user.ltx value as its Python value; ValueError when unreadable."""
    kind = SETTINGS[key][0]
    raw = str(raw).strip()
    if kind == "bool":
        return raw.lower() in ("on", "1", "true")
    if kind == "num":
        try:
            return int(float(raw))
        except OverflowError as exc:
            raise ValueError(raw) from exc
    return raw


def read_settings(anomaly) -> tuple[dict, set[str]]:
    """(values, keys actually present in user.ltx).

    A key missing from user.ltx falls back to the copy kept in the state
    file, then to the engine default.
    """
    _lift_slot_default(anomaly)
    return _read_settings(anomaly)


def _lift_slot_default(anomaly) -> None:
    """Once per install, raise the engine's default of 3 friends to 4, the
    most xrRazom takes (host + 4), and start as the host. A value picked
    after that stays."""
    state = load_state(anomaly)
    if state is None or (state.get("slots_lifted") and state.get("host_default")):
        return
    values, present = _read_settings(anomaly)
    changes = {}
    if not state.get("slots_lifted") and values["xrr_max_players"] == 3:
        changes["xrr_max_players"] = 4
    if not state.get("host_default") and "xrr_host_session" not in present \
            and "xrr_host_session" not in (state.get("settings") or {}):
        changes["xrr_host_session"] = True
    if changes:
        write_settings(anomaly, changes)
        state = load_state(anomaly)
    state["slots_lifted"] = state["host_default"] = True
    _save_state(anomaly, state)


def _read_settings(anomaly) -> tuple[dict, set[str]]:
    values = {key: fallback for key, (_kind, fallback) in SETTINGS.items()}
    for key, raw in ((load_state(anomaly) or {}).get("settings") or {}).items():
        if key in SETTINGS:
            try:
                values[key] = _parse(key, raw)
            except ValueError:
                pass
    present: set[str] = set()
    try:
        lines = user_ltx_path(anomaly).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values, present
    for line in lines:
        key, _, raw = line.strip().partition(" ")
        if key not in SETTINGS:
            continue
        try:
            values[key] = _parse(key, raw)
        except ValueError:
            continue
        present.add(key)
    return values, present


def _format(key: str, value) -> str:
    kind = SETTINGS[key][0]
    if kind == "bool":
        return "on" if value else "off"
    if kind == "num":
        return str(int(value))
    # One console line: no newlines, and the engine splits on nothing else.
    return " ".join(str(value).split())


def write_settings(anomaly, changes: dict) -> None:
    """Replace/append only the ``xrr_*`` lines in ``changes``; keep the rest."""
    path = user_ltx_path(anomaly)
    try:
        lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except FileNotFoundError:
        lines = []
    pending = {k: v for k, v in changes.items() if k in SETTINGS}
    out = []
    for line in lines:
        key = line.strip().partition(" ")[0]
        if key in pending:
            out.append(f"{key} {_format(key, pending.pop(key))}")
        else:
            out.append(line)
    out += [f"{key} {_format(key, value)}" for key, value in pending.items()]
    atomic.write_text(path, "\n".join(out) + "\n")
    state = load_state(anomaly)
    if state is not None:
        saved = {k: _format(k, v) for k, v in changes.items() if k in SETTINGS}
        state["settings"] = {**(state.get("settings") or {}), **saved}
        _save_state(anomaly, state)


# -- join address (axr_options.ltx) ----------------------------------------------
def _options_file(anomaly, gamma, mo2_profile) -> Path:
    """The axr_options.ltx the game actually reads: overwrite/ wins, then the
    highest-priority enabled mod that ships one. Plain Anomaly reads its own."""
    if install_kind(anomaly) == KIND_ANOMALY:
        return Path(anomaly).expanduser() / "gamedata" / "configs" / "axr_options.ltx"
    gamma = Path(gamma).expanduser()
    candidates = dict(mcm_files(gamma))
    overwrite = "overwrite/gamedata/configs/axr_options.ltx"
    if overwrite in candidates:
        return candidates[overwrite]
    try:
        for status_, name in modlist.entries(modlist.read_lines(_modlist(gamma, mo2_profile))):
            rel = f"mods/{name}/gamedata/configs/axr_options.ltx"
            if status_ == "Enabled" and rel in candidates:
                return candidates[rel]
    except (CoopError, OSError, ValueError):
        pass
    return gamma / overwrite


def read_last_ip(anomaly, gamma, mo2_profile) -> str:
    try:
        text = _options_file(anomaly, gamma, mo2_profile).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    section = re.search(r"^\[xrrazom\][^\n]*\n(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    match = section and re.search(r"^\s*last_ip\s*=\s*(.*?)\s*$", section.group(1), re.MULTILINE)
    return match.group(1) if match else ""


def write_last_ip(anomaly, gamma, mo2_profile, address: str) -> None:
    address = "".join(ch for ch in address.strip() if ch.isprintable() and ch not in "[]=;")
    path = _options_file(anomaly, gamma, mo2_profile)
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except FileNotFoundError:
        text = ""
    line = f"last_ip = {address}"
    section = re.search(r"^\[xrrazom\][^\n]*\n(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    if section is None:
        text = text.rstrip("\n") + ("\n\n" if text else "") + f"[xrrazom]\n{line}\n"
    else:
        body = section.group(1)
        if re.search(r"^\s*last_ip\s*=", body, re.MULTILINE):
            body = re.sub(r"^\s*last_ip\s*=.*$", lambda _m: line, body, count=1, flags=re.MULTILINE)
        else:
            body = line + "\n" + body
        text = text[: section.start(1)] + body + text[section.end(1):]
    atomic.write_text(path, text)


# -- export / import -------------------------------------------------------------
#: ``manifest.json`` "type" of a co-op export, so a game backup or an
#: xrRazom zip is never mistaken for one.
EXPORT_TYPE = "commander-coop"


def _save_roots(anomaly, gamma, mo2_profile) -> dict[str, Path]:
    """Zip folder -> every place a save can be (game_stats.save_folders):
    the Anomaly folder, MO2's per-profile local saves, and MO2's overwrite."""
    names = ("saves", "mo2-saves", "overwrite-saves") if str(gamma).strip() else ("saves",)
    return dict(zip(names, save_folders(anomaly, gamma if str(gamma).strip() else None, mo2_profile)))


def _coop_files(anomaly, gamma) -> dict[str, Path]:
    """The co-op side of user.ltx and the MCM files, by snapshot name: live
    while co-op is on, else the copy the last switch put under ``modes/``."""
    if is_active(anomaly):
        return {name: path for name, path in _mode_files(anomaly, gamma).items() if path.is_file()}
    src = state_dir(anomaly) / "modes" / "coop"
    return {f.relative_to(src).as_posix(): f for f in sorted(src.rglob("*")) if f.is_file()} if src.is_dir() else {}


def export_session(anomaly, gamma, mo2_profile, dest, report=lambda _line: None) -> Path:
    """Write every save plus the co-op settings, keybinds, MCM values, Steam
    game and join address to the zip ``dest``."""
    state = load_state(anomaly)
    if state is None:
        raise CoopError("xrRazom Co-op is not installed.")
    dest = Path(dest).expanduser()
    if dest.suffix.lower() != ".zip":
        dest = dest.with_name(dest.name + ".zip")
    try:
        files = [(f"{top}/{f.relative_to(folder).as_posix()}", f)
                 for top, folder in _save_roots(anomaly, gamma, mo2_profile).items() if folder.is_dir()
                 for f in sorted(folder.rglob("*")) if f.is_file() and not f.is_symlink()]
        files += [("coop/" + name, path) for name, path in _coop_files(anomaly, gamma).items()]
        values, _present = _read_settings(anomaly)  # read_settings() may write
        manifest = {
            "format": 1,
            "type": EXPORT_TYPE,
            "created": time.time(),
            "xrrazom": state.get("version", ""),
            "kind": state.get("kind", KIND_GAMMA),
            "settings": {key: _format(key, value) for key, value in values.items()},
            "appid": steam_appid(anomaly),
            "last_ip": read_last_ip(anomaly, gamma, mo2_profile),
            "saves": sum(1 for name, _path in files if name.lower().endswith(".scop")),
        }
        _write_zip(dest, files, manifest, report)
    except (BackupError, OSError) as exc:
        raise CoopError(f"Could not write the export: {exc}") from exc
    return dest


def read_export(path) -> dict:
    """The manifest of a co-op export; CoopError when ``path`` isn't one."""
    try:
        with zipfile.ZipFile(path) as archive:
            bad = [n for n in archive.namelist() if _safe_member(n) is None]
            if bad:
                raise CoopError(f"Unsafe path in archive: {bad[0]}")
            _check_unpacked_size(archive)
            if archive.getinfo(MANIFEST).file_size > 1_000_000:
                raise KeyError(MANIFEST)
            manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
    except (KeyError, ValueError, zipfile.BadZipFile, OSError) as exc:
        raise CoopError("This isn't a co-op export from COMMANDER.") from exc
    if not isinstance(manifest, dict) or manifest.get("type") != EXPORT_TYPE:
        raise CoopError("This isn't a co-op export from COMMANDER.")
    return manifest


def _extract(archive: zipfile.ZipFile, name: str, dest: Path) -> None:
    if _safe_member(name) is None:
        raise CoopError(f"Unsafe path in archive: {name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as out, archive.open(name) as src:
            shutil.copyfileobj(src, out, 1 << 20)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def import_session(anomaly, gamma, mo2_profile, profile_name: str, path, report=lambda _line: None) -> None:
    """Put an export back. Every file it replaces is first kept in a
    "before restore" backup, listed with the profile's other backups."""
    manifest = read_export(path)
    if load_state(anomaly) is None:
        raise CoopError("Install xrRazom Co-op first, then import.")
    roots = _save_roots(anomaly, gamma, mo2_profile)
    snapshot = state_dir(anomaly) / "modes" / "coop"
    with zipfile.ZipFile(path) as archive:
        plan = []
        for name in archive.namelist():
            parts = PurePosixPath(name).parts
            if name.endswith("/") or len(parts) < 2:
                continue
            if parts[0] in roots:
                plan.append((name, roots[parts[0]].joinpath(*parts[1:])))
            elif parts[0] == "coop" and _is_mode_file(PurePosixPath(*parts[1:]).as_posix()):
                plan.append((name, snapshot.joinpath(*parts[1:])))
        replaced = [(name, dest) for name, dest in plan if not name.startswith("coop/") and dest.is_file()]
        # Co-op on: the snapshot goes live below, over these files.
        live = _mode_files(anomaly, gamma) if is_active(anomaly) else {}
        replaced += [(name, live[name[len("coop/"):]]) for name, _dest in plan
                     if name.startswith("coop/") and live.get(name[len("coop/"):], Path()).is_file()]
        try:
            if replaced:
                report(f"Keeping a copy of the {len(replaced)} files this import replaces")
                create_backup_of_files(profile_name, replaced, report)
            for name, dest in plan:
                _extract(archive, name, dest)
        except BackupError as exc:
            raise CoopError(str(exc)) from exc
    if is_active(anomaly):
        _load_mode(anomaly, gamma, "coop")
    settings = {}
    for key, raw in (manifest.get("settings") or {}).items():
        if key in SETTINGS:
            try:
                settings[key] = _parse(key, raw)
            except ValueError:
                pass
    if settings:
        write_settings(anomaly, settings)
    if manifest.get("appid") in (COP_APPID, SPACEWAR_APPID):
        set_steam_appid(anomaly, manifest["appid"])
    if isinstance(manifest.get("last_ip"), str) and manifest["last_ip"].strip():
        write_last_ip(anomaly, gamma, mo2_profile, manifest["last_ip"])
    report(f"Imported {len(plan)} files.")


# -- addresses -------------------------------------------------------------------
def lan_ip() -> str:
    """This machine's LAN address (no packet is sent; UDP connect only routes)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def public_ip() -> str:
    with network.urlopen_with_retry("https://api.ipify.org", timeout=10) as response:
        return network.read_response_bytes(response, 64).decode().strip()
