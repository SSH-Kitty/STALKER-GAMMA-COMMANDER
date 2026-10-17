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

Co-op settings are the mod's console variables in ``appdata/user.ltx``.
The single-player engine drops console variables it doesn't know when it
saves user.ltx, so a copy is kept in the state file and put back on the
next switch to co-op.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from . import atomic, modlist, network
from .game_backup import _safe_member, mcm_files, user_ltx_path
from .mod_install import write_basic_meta_ini

MOD_NAME = "xrRazom Co-op"
MODDB_PAGE = "https://www.moddb.com/mods/stalker-anomaly/addons/xrrazom-stalker-anomaly-co-op-slim"
MODDB_MOD_PAGE = "https://www.moddb.com/mods/xrrazom-stalker-anomaly-co-op"
DISCORD_URL = "https://discord.gg/mpXUXjTQu8"
RELEASE_FILE = "xrRazom-release.txt"
_ENGINE_ROOT = "Anomaly/"
_MOD_ROOTS = ("gamedata/", "fomod/")

#: Steam app a Steam session runs as. Everyone in a session must use the same
#: one: Call of Pripyat (what xrRazom ships) needs the game on each account;
#: Spacewar, Valve's free Steamworks test app, needs nothing.
COP_APPID, SPACEWAR_APPID = 41700, 480

NOT_INSTALLED, COOP, SINGLEPLAYER, REVERTED = "not_installed", "coop", "singleplayer", "reverted"


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


def inspect_zip(path: str | Path) -> dict:
    """Validate an xrRazom zip and return its release info."""
    try:
        with zipfile.ZipFile(path) as archive:
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
def install(zip_path: str | Path, anomaly, gamma, mo2_profile, progress=None) -> dict:
    """Install (or update) from ``zip_path``.

    A first install stays in singleplayer until the player switches to co-op;
    an update keeps whichever mode was on.
    """
    info = inspect_zip(zip_path)
    gamma = Path(gamma).expanduser()
    root = state_dir(anomaly)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".coop-", dir=root))
    try:
        files: dict[str, str] = {}
        with zipfile.ZipFile(zip_path) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            for count, member in enumerate(members, 1):
                name = member.filename
                if name.startswith(_ENGINE_ROOT):
                    rel = name[len(_ENGINE_ROOT):]
                    target = staging / "files" / rel
                elif name.startswith(_MOD_ROOTS):
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
        write_basic_meta_ini(staging / "mod", Path(zip_path).name)
        mod_dir = gamma / "mods" / MOD_NAME
        shutil.rmtree(mod_dir, ignore_errors=True)
        mod_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging / "mod"), mod_dir)  # may cross filesystems
        shutil.rmtree(root / "files", ignore_errors=True)
        os.replace(staging / "files", root / "files")
        state = {"version": info.get("version", ""), "release": info, "files": files,
                 "originals": [], "active": False,
                 "slots_lifted": bool((previous or {}).get("slots_lifted"))}
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


def ensure_root_appid(anomaly, gamma, mo2_profile) -> None:
    """Bring an install made before ``_add_root_appid`` up to date."""
    state = load_state(anomaly)
    if state and state.get("active") and not any(rel.lower() == _APPID for rel in state["files"]):
        switch_to_coop(anomaly, gamma, mo2_profile)


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
    _add_root_appid(state, root)
    originals = set(state.get("originals", []))
    done: list[str] = []
    try:
        for rel, digest in state["files"].items():
            target = _resolve_ci(anomaly, rel)
            if target.is_file():
                if _sha256(target) == digest:
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
    _set_mod_enabled(gamma, mo2_profile, True)
    # A solo session since the last switch may have wiped the xrr_* lines.
    values, present = read_settings(anomaly)
    missing = {key: values[key] for key in state.get("settings", {}) if key not in present}
    if missing:
        write_settings(anomaly, missing)


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
    _set_mod_enabled(gamma, mo2_profile, False)


def uninstall(anomaly, gamma, mo2_profile) -> None:
    switch_to_singleplayer(anomaly, gamma, mo2_profile)
    _remove_mod_entry(gamma, mo2_profile)
    shutil.rmtree(Path(gamma).expanduser() / "mods" / MOD_NAME, ignore_errors=True)
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
    co-op applied would start the co-op engine, and a direct (no MO2) launch
    would start it without the mod's gamedata.
    """
    try:
        mode = status(anomaly)
    except OSError:
        mode = NOT_INSTALLED
    if mode == REVERTED:
        return "A GAMMA update or Anomaly repair put the original game files back over co-op. Open the Multiplayer page to re-apply co-op or switch to singleplayer."
    if mode == COOP and direct:
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
    most xrRazom takes (host + 4). A value picked after that stays."""
    state = load_state(anomaly)
    if state is None or state.get("slots_lifted"):
        return
    if _read_settings(anomaly)[0]["xrr_max_players"] == 3:
        write_settings(anomaly, {"xrr_max_players": 4})
        state = load_state(anomaly)
    state["slots_lifted"] = True
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
def _options_file(gamma, mo2_profile) -> Path:
    """The axr_options.ltx the game actually reads: overwrite/ wins, then the
    highest-priority enabled mod that ships one."""
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


def read_last_ip(gamma, mo2_profile) -> str:
    try:
        text = _options_file(gamma, mo2_profile).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    section = re.search(r"^\[xrrazom\][^\n]*\n(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    match = section and re.search(r"^\s*last_ip\s*=\s*(.*?)\s*$", section.group(1), re.MULTILINE)
    return match.group(1) if match else ""


def write_last_ip(gamma, mo2_profile, address: str) -> None:
    address = "".join(ch for ch in address.strip() if ch.isprintable() and ch not in "[]=;")
    path = _options_file(gamma, mo2_profile)
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
