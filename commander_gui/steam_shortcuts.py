"""Read and write Steam's ``shortcuts.vdf`` (binary VDF / "KeyValues" format).

Only this one format, not general VDF: ``shortcuts.vdf`` is a small, fixed
shape (a root map holding one "shortcuts" map, itself holding one map per
non-Steam game, keyed by string index "0", "1", ...). Every entry is kept as
an ordered ``(key, type_byte, value)`` tuple rather than a dict, so a field
this module doesn't know about round-trips byte-for-byte instead of being
silently dropped or reordered - corrupting a user's entire non-Steam-games
list to add one shortcut would be a much worse failure than doing nothing.

No third-party VDF library: this project has exactly one dependency
(PySide6, see requirements.txt) and hand-parses every other file format it
touches (tarballs, ``libraryfolders.vdf``, its own JSON settings) with the
standard library. This format is small enough to do the same.

Type bytes, straight from Valve's own format:
    0x00  nested map      - followed by a null-terminated key, the map's own
                             entries, then a single 0x08 closing it
    0x01  UTF-8 string    - null-terminated key, then a null-terminated value
    0x02  32-bit int      - null-terminated key, then 4 little-endian bytes
    0x08  end of map      - no key/value, just closes the innermost open map

The root of the file is itself an unlabeled map: it has no opening type byte
or key of its own, only a closing 0x08 at end of file. Parsing/serializing it
with the exact same recursive routine used for every nested map (just without
a caller-supplied type/key for the outermost call) handles that correctly
with no special-casing.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import zlib
from collections.abc import Callable
from pathlib import Path

from . import atomic
from .config import child_environment

#: A parsed entry: (key, type_byte, value). value is a list[Entry] for a map
#: (type 0x00), a str for a string (0x01), or an int for an int32 (0x02).
Entry = tuple[str, int, object]

_TYPE_MAP = 0x00
_TYPE_STRING = 0x01
_TYPE_INT32 = 0x02
_TYPE_END = 0x08


class ShortcutsFileError(ValueError):
    """``shortcuts.vdf`` exists but could not be parsed.

    Raised instead of writing anything: a file this module does not fully
    understand is left exactly as it is.
    """


def _parse_entries(buf: bytes, pos: int) -> tuple[list[Entry], int]:
    entries: list[Entry] = []
    while True:
        if pos >= len(buf):
            # Every map, including the file's own root, ends with 0x08.
            raise ValueError("shortcuts.vdf ends before its closing marker")
        if buf[pos] == _TYPE_END:
            break
        type_byte = buf[pos]
        pos += 1
        end = buf.index(b"\x00", pos)
        key = buf[pos:end].decode("utf-8", errors="replace")
        pos = end + 1
        if type_byte == _TYPE_MAP:
            value, pos = _parse_entries(buf, pos)
            pos += 1  # the 0x08 that closed this nested map
        elif type_byte == _TYPE_STRING:
            end = buf.index(b"\x00", pos)
            value = buf[pos:end].decode("utf-8", errors="replace")
            pos = end + 1
        elif type_byte == _TYPE_INT32:
            raw = buf[pos : pos + 4]
            if len(raw) != 4:
                # A slice past the end is short, not an error - a truncated
                # file used to be read (and later re-written) as if whole.
                raise ValueError("shortcuts.vdf ends inside a number")
            value = int.from_bytes(raw, "little", signed=True)
            pos += 4
        else:
            raise ValueError(f"Unsupported shortcuts.vdf type byte {type_byte:#x}")
        entries.append((key, type_byte, value))
    return entries, pos


def _serialize_entries(entries: list[Entry]) -> bytes:
    out = bytearray()
    for key, type_byte, value in entries:
        out.append(type_byte)
        out += key.encode("utf-8") + b"\x00"
        if type_byte == _TYPE_MAP:
            out += _serialize_entries(value)
            out.append(_TYPE_END)
        elif type_byte == _TYPE_STRING:
            out += str(value).encode("utf-8") + b"\x00"
        elif type_byte == _TYPE_INT32:
            out += int(value).to_bytes(4, "little", signed=True)
        else:
            raise ValueError(f"Unsupported shortcuts.vdf type byte {type_byte:#x}")
    return bytes(out)


def read_shortcuts(path: Path) -> list[Entry]:
    """Parse *path* into the root's own entries (normally just "shortcuts").

    A missing or empty file is a user with no non-Steam games yet, not an
    error - returns ``[]``, which ``write_shortcuts`` turns into a minimal
    valid file (an empty "shortcuts" map).
    """
    if not path.is_file():
        return []
    buf = path.read_bytes()
    if not buf:
        return []
    try:
        entries, _pos = _parse_entries(buf, 0)
    except (ValueError, IndexError, RecursionError) as exc:
        # A truncated file runs a bytes.index()/slice off the end; an
        # unknown type byte raises ValueError; absurdly deep nesting runs
        # out of recursion. Either way: do not guess.
        raise ShortcutsFileError(
            f"{path} is not a shortcuts file this version understands ({exc})"
        ) from exc
    return entries


def write_shortcuts(path: Path, entries: list[Entry]) -> None:
    """Serialize *entries* (as returned by ``read_shortcuts``) back to *path*."""
    data = _serialize_entries(entries) + bytes([_TYPE_END])
    # Atomic: a crash mid-write must never leave Steam a half-written file
    # holding every one of the user's non-Steam games. No lock file - this
    # is Steam's directory, not ours.
    atomic.write_bytes(path, data, lock=False)


def get_shortcuts_map(root: list[Entry]) -> list[Entry]:
    """Return the ``"shortcuts"`` map's own entries, creating it if absent."""
    for key, type_byte, value in root:
        if key == "shortcuts" and type_byte == _TYPE_MAP:
            return value
    return []


def set_shortcuts_map(root: list[Entry], shortcuts: list[Entry]) -> list[Entry]:
    """Return a new root with its ``"shortcuts"`` map replaced by *shortcuts*."""
    new_root: list[Entry] = []
    replaced = False
    for key, type_byte, value in root:
        if not replaced and key == "shortcuts" and type_byte == _TYPE_MAP:
            new_root.append((key, type_byte, shortcuts))
            replaced = True
        else:
            new_root.append((key, type_byte, value))
    if not replaced:
        new_root.append(("shortcuts", _TYPE_MAP, shortcuts))
    return new_root


def _shortcut_appid(quoted_exe: str, appname: str) -> int:
    """Steam's own non-Steam-game appid: crc32(exe+appname) with the top bit
    set, stored as a signed int32 - confirmed against a real shortcuts.vdf,
    where both existing entries' appid fields are negative for exactly this
    reason. Steam recomputes this itself from Exe+appname rather than
    trusting the stored value, but writing the real formula means an entry
    this module adds is byte-for-byte indistinguishable from one Steam
    wrote itself.
    """
    unsigned = (zlib.crc32((quoted_exe + appname).encode("utf-8")) | 0x80000000) & 0xFFFFFFFF
    return unsigned - 0x100000000


def _shortcut_fields(
    appname: str, quoted_exe: str, start_dir: str, *, icon: str, launch_options: str
) -> list[Entry]:
    return [
        ("appid", _TYPE_INT32, _shortcut_appid(quoted_exe, appname)),
        ("appname", _TYPE_STRING, appname),
        ("Exe", _TYPE_STRING, quoted_exe),
        ("StartDir", _TYPE_STRING, f'"{start_dir}"'),
        ("icon", _TYPE_STRING, icon),
        ("ShortcutPath", _TYPE_STRING, ""),
        ("LaunchOptions", _TYPE_STRING, launch_options),
        ("IsHidden", _TYPE_INT32, 0),
        ("AllowDesktopConfig", _TYPE_INT32, 1),
        ("AllowOverlay", _TYPE_INT32, 1),
        ("OpenVR", _TYPE_INT32, 0),
        ("Devkit", _TYPE_INT32, 0),
        ("DevkitGameID", _TYPE_STRING, ""),
        ("DevkitOverrideAppID", _TYPE_INT32, 0),
        ("LastPlayTime", _TYPE_INT32, 0),
        ("FlatpakAppID", _TYPE_STRING, ""),
        ("tags", _TYPE_MAP, []),
    ]


def add_or_update_shortcut(
    shortcuts: list[Entry],
    appname: str,
    exe: str,
    start_dir: str,
    *,
    icon: str = "",
    launch_options: str = "",
) -> list[Entry]:
    """Return a new "shortcuts" map with one entry added or updated.

    Matched by (Exe, LaunchOptions), not appname: what actually identifies
    "this is COMMANDER's own shortcut" across repeated clicks is what it
    launches, not its display name (which a user could rename in Steam).
    Re-running this after moving the install (a different Exe path) adds a
    fresh entry rather than silently discarding the old one; running it
    unchanged updates the same entry in place instead of duplicating it.
    """
    quoted_exe = f'"{exe}"'
    fields = _shortcut_fields(
        appname, quoted_exe, start_dir, icon=icon, launch_options=launch_options
    )
    for index, (key, type_byte, value) in enumerate(shortcuts):
        if type_byte != _TYPE_MAP:
            continue
        value_map = {k: v for k, _t, v in value}
        if (
            value_map.get("Exe") == quoted_exe
            and value_map.get("LaunchOptions", "") == launch_options
        ):
            # Keep what the user set in Steam itself: collections, play
            # history and hidden state used to reset on every "Add to Steam".
            kept = {k: (k, t, v) for k, t, v in value if k in _USER_OWNED_FIELDS}
            merged = [kept.pop(k, (k, t, v)) for k, t, v in fields]
            merged.extend(kept.values())
            new_shortcuts = list(shortcuts)
            new_shortcuts[index] = (key, _TYPE_MAP, merged)
            return new_shortcuts
    existing_indices = [
        int(key) for key, t, _v in shortcuts if t == _TYPE_MAP and key.isdigit()
    ]
    next_index = str(max(existing_indices, default=-1) + 1)
    return [*shortcuts, (next_index, _TYPE_MAP, fields)]


#: shortcuts.vdf fields Steam lets the user change; preserved on update.
_USER_OWNED_FIELDS = frozenset({"tags", "LastPlayTime", "IsHidden"})


def add_commander_shortcuts(
    vdf_path: Path,
    *,
    exe: str,
    start_dir: str,
    icon: str = "",
    base_launch_options: str = "",
) -> None:
    """Add/update both the normal and Deck Mode COMMANDER shortcuts.

    *base_launch_options* covers running from a source checkout, where
    *exe* is the bare Python interpreter and ``-m commander_gui`` has to
    ride along on every launch - Deck Mode's own shortcut then gets
    ``"{base_launch_options} --deck"``, not just ``"--deck"`` on its own.

    Backs up the existing file to ``<name>.gammagui.bak`` first (matching
    this project's established backup convention for modlist.txt) - a
    parsing or serialization bug here must never leave a user unable to
    recover their other non-Steam games.
    """
    # Parsed before the backup is taken, so an unreadable file is neither
    # backed up over a good older backup nor touched at all.
    root = read_shortcuts(vdf_path)
    if vdf_path.is_file():
        backup = vdf_path.with_name(vdf_path.name + ".gammagui.bak")
        if not backup.exists():  # keep the first copy: it is the untouched original
            atomic.write_bytes(backup, vdf_path.read_bytes(), lock=False)
    else:
        vdf_path.parent.mkdir(parents=True, exist_ok=True)
    deck_options = f"{base_launch_options} --deck".strip()
    shortcuts = get_shortcuts_map(root)
    shortcuts = add_or_update_shortcut(
        shortcuts,
        "STALKER COMMANDER",
        exe,
        start_dir,
        icon=icon,
        launch_options=base_launch_options,
    )
    shortcuts = add_or_update_shortcut(
        shortcuts,
        "STALKER COMMANDER DECK",
        exe,
        start_dir,
        icon=icon,
        launch_options=deck_options,
    )
    root = set_shortcuts_map(root, shortcuts)
    write_shortcuts(vdf_path, root)
    quoted_exe = f'"{exe}"'
    for name, source in (
        ("STALKER COMMANDER", COMMANDER_ARTWORK_DIR),
        ("STALKER COMMANDER DECK", DECK_ARTWORK_DIR),
    ):
        install_artwork(vdf_path, _shortcut_appid(quoted_exe, name) & 0xFFFFFFFF, source)


def find_userdata_config_dirs() -> list[Path]:
    """Every distinct ``userdata/<id>/config`` directory across all known
    Steam roots.

    ``~/.local/share/Steam`` and ``~/.steam/steam`` are the same directory
    on most real installs (one is a symlink to the other) - scanning both
    without resolving them first lists every real account twice. Both the
    root candidates and the config dirs they produce are resolved and
    de-duplicated, the same way ``launcher.py``'s own Steam-library-root
    scan already avoids this for Proton discovery.

    "0" is skipped: Steam's placeholder for "no account logged in", not a
    real profile - never a sensible "add this to Steam" target.
    """
    from .launcher import STEAM_ROOT_CANDIDATES

    seen_roots: list[Path] = []
    dirs: list[Path] = []
    for candidate in STEAM_ROOT_CANDIDATES:
        try:
            root = candidate.resolve()
        except OSError:
            continue
        if not root.is_dir() or root in seen_roots:
            continue
        seen_roots.append(root)
        userdata = root / "userdata"
        if not userdata.is_dir():
            continue
        try:
            children = sorted(userdata.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or not child.name.isdigit() or child.name == "0":
                continue
            config = child / "config"
            if not config.is_dir():
                continue
            try:
                resolved_config = config.resolve()
            except OSError:
                resolved_config = config
            if resolved_config not in dirs:
                dirs.append(resolved_config)
    return dirs


def find_shortcuts_vdf() -> Path | None:
    """The config dir to write to when there's exactly one candidate.

    Steam's own loginusers.vdf is not a reliable signal for picking one
    automatically when there's more than one: an account can be listed
    there (e.g. via Family Sharing) without ever having actually used this
    machine, and its Timestamp/AutoLogin fields do not necessarily belong
    to whichever account's userdata is actually here - confirmed against a
    real Steam install, where the account with the newest Timestamp and
    AutoLogin=1 was not the one with a userdata folder at all, and where
    those fields changed underneath a running COMMANDER mid-session. So
    this never guesses between multiple candidates: returns None both when
    no Steam install was found and when more than one account was, and
    callers should offer ``list_steam_accounts()`` for the user to pick
    from in the second case.
    """
    dirs = find_userdata_config_dirs()
    if len(dirs) == 1:
        return dirs[0] / "shortcuts.vdf"
    return None


_STEAM_ID64_BASE = 76561197960265728


def _loginusers_field(steam_root: Path, account_id: str, field: str) -> str | None:
    """One field of the account's entry in that Steam root's loginusers.vdf.

    Best-effort only - a missing/unreadable/unrecognised-shape file is None,
    never a reason to fail the actual lookup.
    """
    if not account_id.isdigit():
        return None
    steamid64 = str(int(account_id) + _STEAM_ID64_BASE)
    loginusers = steam_root / "config" / "loginusers.vdf"
    try:
        text = loginusers.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    block_match = re.search(
        re.escape(f'"{steamid64}"') + r"\s*\{([^}]*)\}", text
    )
    if block_match is None:
        return None
    field_match = re.search(rf'"{field}"\s+"([^"]*)"', block_match.group(1))
    return field_match.group(1) if field_match else None


def _persona_name(steam_root: Path, account_id: str) -> str | None:
    """The account's display name - a friendlier label than a bare ID."""
    return _loginusers_field(steam_root, account_id, "PersonaName")


#: Where Steam keeps its own registry (native, then Flatpak).
REGISTRY_CANDIDATES = (
    Path.home() / ".steam" / "registry.vdf",
    Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".steam" / "registry.vdf",
)


def autologin_shortcuts_vdf(registries=REGISTRY_CANDIDATES) -> Path | None:
    """shortcuts.vdf of the account Steam logs into on start, or None.

    Steam's own registry names that account (``AutoLoginUser``, a login
    name); loginusers.vdf maps it to the userdata folder. Unlike the
    Timestamp/AutoLogin fields in loginusers.vdf (see find_shortcuts_vdf),
    this is the value Steam itself acts on.
    """
    names = set()
    for registry in registries:
        try:
            text = registry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(re.findall(r'"AutoLoginUser"\s+"([^"]+)"', text))
    if not names:
        return None
    for config_dir in find_userdata_config_dirs():
        account = _loginusers_field(config_dir.parents[2], config_dir.parent.name, "AccountName")
        if account in names:
            return config_dir / "shortcuts.vdf"
    return None


def list_steam_accounts() -> list[tuple[str, Path]]:
    """(label, shortcuts.vdf path) for every Steam account found on this
    machine, for a user to choose from when ``find_shortcuts_vdf()``
    can't pick one on its own.

    A label is just the account's display name (or "Account <id>" when
    that can't be resolved) - no path, so the picker reads as a plain list
    of accounts rather than a wall of filesystem paths. The numeric ID is
    only appended when two accounts would otherwise show the exact same
    name, which is the sole thing actually required to keep every entry
    distinguishable.
    """
    raw: list[tuple[str, str, Path]] = []
    for config_dir in find_userdata_config_dirs():
        account_id = config_dir.parent.name
        # userdata/<id>/config -> the Steam root three levels up.
        steam_root = config_dir.parents[2]
        persona = _persona_name(steam_root, account_id)
        label = persona if persona else f"Account {account_id}"
        raw.append((label, account_id, config_dir / "shortcuts.vdf"))

    label_counts: dict[str, int] = {}
    for label, _account_id, _vdf_path in raw:
        label_counts[label] = label_counts.get(label, 0) + 1

    accounts: list[tuple[str, Path]] = []
    for label, account_id, vdf_path in raw:
        if label_counts[label] > 1:
            label = f"{label} ({account_id})"
        accounts.append((label, vdf_path))
    return accounts


def _is_flatpak_steam(steam_root: Path) -> bool:
    marker = ".var/app/com.valvesoftware.Steam"
    try:
        return marker in str(steam_root) or marker in str(steam_root.resolve())
    except OSError:
        return marker in str(steam_root)


def steam_launch_command(steam_root: Path) -> list[str]:
    """The command to start Steam matching how it's installed here."""
    if _is_flatpak_steam(steam_root):
        return ["flatpak", "run", "com.valvesoftware.Steam"]
    return ["steam"]


def restart_steam(
    steam_root: Path,
    *,
    exit_timeout: float = 20.0,
    while_stopped: Callable[[], None] | None = None,
    relaunch_args: tuple[str, ...] = (),
) -> None:
    """Cleanly quit Steam, optionally do something, then relaunch it.

    Steam has no live-reload for its own config - the client only parses
    shortcuts.vdf at startup - so this is the only way to make a shortcut
    just added actually show up without asking the user to do it by hand.

    Pass the shortcuts write as ``while_stopped`` rather than writing first:
    a running Steam keeps its own copy of the shortcut list and can write it
    back on the way out, silently replacing the file just written. If Steam
    has not exited by ``exit_timeout`` nothing is written and ``OSError`` is
    raised. Meant to run off the GUI thread: ``-shutdown`` only requests the
    exit and returns immediately, and the wait can take several seconds.

    ``relaunch_args`` ride along on the relaunch (e.g. a ``steam://`` link):
    starting a second ``steam`` while this one is still coming up leaves
    both stuck, never logged in.
    """
    launch_cmd = steam_launch_command(steam_root)
    try:
        subprocess.run(
            [*launch_cmd, "-shutdown"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # Promised as OSError to callers; TimeoutExpired is not one.
        raise OSError("Steam did not respond to the shutdown request.") from exc
    if not _wait_for_steam_to_exit(exit_timeout) and not _stop_hung_steam():
        raise OSError("Steam did not close in time - nothing was changed.")
    written = False
    try:
        if while_stopped is not None:
            while_stopped()
        written = True
    finally:
        # Steam comes back even when the write failed: the user asked for a
        # shortcut, not to be left with Steam closed. The failure still
        # reaches the caller once it has been restarted - without the
        # relaunch args, which were meant for what was just written.
        subprocess.Popen(
            [*launch_cmd, *(relaunch_args if written else ())],
            env=child_environment(),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _stop_hung_steam() -> bool:
    """Stop a Steam that ignored ``-shutdown``; True once it is gone.

    A hung client - often with no window at all, so it looks closed - never
    answers the request, and swallows every ``steam://`` link handed to it.
    The user already agreed to a restart, so it is terminated, then killed.
    """
    pkill = shutil.which("pkill")
    if not pkill or os.environ.get("SteamGamepadUI"):
        return False  # Game Mode: Steam is the session, killing it ends the session
    for sig, wait in (("TERM", 10.0), ("KILL", 5.0)):
        subprocess.run(
            [pkill, f"-{sig}", "-x", "steam"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if _wait_for_steam_to_exit(wait):
            return True
    return False


def steam_is_running() -> bool:
    """True when a Steam client process is running (best-effort)."""
    pgrep = shutil.which("pgrep")
    if not pgrep:
        return False
    try:
        proc = subprocess.run(
            [pgrep, "-x", "steam"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _wait_for_steam_to_exit(timeout: float) -> bool:
    """True once Steam is gone; False if it is still running at ``timeout``."""
    if not shutil.which("pgrep"):
        # No way to tell - give -shutdown a moment and hope, as before.
        time.sleep(3)
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not steam_is_running():
            return True
        time.sleep(0.5)
    return not steam_is_running()


def steam_root_for(vdf_path: Path) -> Path:
    """userdata/<id>/config/shortcuts.vdf -> the Steam root above it."""
    return vdf_path.parents[3]


#: Where the shortcut icon is copied. A stable path matters: inside an
#: AppImage the bundled PNG lives under a /tmp/.mount_* directory that
#: disappears the moment COMMANDER exits, leaving Steam a dead icon path.
ICON_DEST = Path.home() / ".local" / "share" / "icons" / "stalker-gamma-commander.png"


def install_icon(source: Path, dest: Path = ICON_DEST) -> str:
    """Copy the app icon somewhere permanent; return its path, or ""."""
    if not source.is_file():
        return ""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_bytes(dest, source.read_bytes(), lock=False)
    except OSError:
        return ""
    return str(dest)


def commander_launch_target(
    *,
    appimage: Path | None = None,
    wrapper: str | None = None,
    repo_root: Path | None = None,
) -> tuple[str, str, str]:
    """(exe, start_dir, base_launch_options) for the running install.

    AppImage first (the file itself, which survives updates in place), then
    the packaged ``stalker-gamma-commander`` wrapper, then a source
    checkout - through ``run.sh``, which sets up the virtualenv exactly as a
    manual start would, rather than a bare interpreter path that breaks the
    day the venv is rebuilt.
    """
    if appimage is None:
        from .self_update import commander_appimage_path

        appimage = commander_appimage_path()
    if appimage is not None:
        return str(appimage), str(appimage.parent), ""
    if wrapper is None:
        wrapper = shutil.which("stalker-gamma-commander")
    if wrapper:
        return wrapper, str(Path(wrapper).resolve().parent), ""
    root = repo_root or Path(__file__).resolve().parents[1]
    run_sh = root / "run.sh"
    if run_sh.is_file():
        return str(run_sh), str(root), ""
    return sys.executable, str(root), "-m commander_gui"


def bundled_icon() -> Path:
    return Path(__file__).resolve().parents[1] / "cli" / "stalker-gamma.png"


def add_to_steam(vdf_path: Path, *, restart: bool) -> None:
    """The whole "Add to Steam" action, shared by both interfaces.

    With ``restart`` (Steam is running), Steam is shut down first, the file
    written while it is closed, and Steam started again. Blocking - run it
    on a worker thread. Raises OSError / ShortcutsFileError on failure,
    before anything was written.
    """
    exe, start_dir, base_launch_options = commander_launch_target()
    icon = install_icon(bundled_icon())
    # Parse up front: a file this module can't read must stop the action
    # before Steam is shut down for nothing.
    read_shortcuts(vdf_path)

    def write() -> None:
        add_commander_shortcuts(
            vdf_path,
            exe=exe,
            start_dir=start_dir,
            icon=icon,
            base_launch_options=base_launch_options,
        )

    if restart:
        restart_steam(steam_root_for(vdf_path), while_stopped=write)
    else:
        write()


# -- STALKER COMMANDER xrRAZOM shortcut ------------------------------------------
# xrRazom's Steam sessions need Steamworks (overlay, friends-list join), which
# only works when the Steam client itself starts the game through Proton.
# So a Steam co-op launch goes through a dedicated non-Steam shortcut, forced
# onto the same Proton build COMMANDER's prefix already uses.

COOP_SHORTCUT_NAME = "STALKER COMMANDER xrRAZOM"
#: Older builds' names; a rewrite renames that entry, keeping its collections.
_LEGACY_COOP_NAMES = {"GAMMA Co-op", "STALKER CMNDR xrRAZOM"}

#: Library artwork for each shortcut, as (bundled file, grid file suffix).
#: Steam reads ``userdata/<id>/config/grid/<appid><suffix>``.
_ASSETS = Path(__file__).resolve().parent / "assets"
COOP_ARTWORK_DIR = _ASSETS / "coop_artwork"
COMMANDER_ARTWORK_DIR = _ASSETS / "commander_artwork"
DECK_ARTWORK_DIR = _ASSETS / "deck_artwork"
_ARTWORK = (
    ("cover.jpg", "p.jpg"),
    ("hero.jpg", "_hero.jpg"),
    ("logo.png", "_logo.png"),
    ("wide.png", ".png"),
)


def install_artwork(vdf_path: Path, appid: int, source: Path) -> None:
    """Copy a shortcut's cover, background, logo and wide cover into Steam's
    grid folder for ``appid``. Best effort: artwork never blocks a launch."""
    grid = vdf_path.parent / "grid"
    for name, suffix in _ARTWORK:
        src = source / name
        if not src.is_file():
            continue
        dest = grid / f"{appid}{suffix}"
        try:
            grid.mkdir(parents=True, exist_ok=True)
            # Steam takes .png or .jpg; a leftover of the other kind could win.
            other = ".jpg" if dest.suffix == ".png" else ".png"
            dest.with_suffix(other).unlink(missing_ok=True)
            atomic.write_bytes(dest, src.read_bytes(), lock=False)
        except OSError:
            pass


def coop_launch_options(
    prefix: str, mo2_profile: str, target: str, appid: int, arguments: str = "", *, mo2: bool = True
) -> str:
    """LaunchOptions for the co-op shortcut.

    ``STEAM_COMPAT_DATA_PATH`` points Steam's Proton at COMMANDER's own
    prefix, so MO2 runs with the dependencies already installed there
    rather than in a fresh ``compatdata/<id>``. Steam gives a shortcut its
    own SteamAppId, which steam_api prefers over steam_appid.txt, so it is
    set back to the co-op appid (Call of Pripyat or Spacewar).

    Steam runs LaunchOptions through ``sh -c``: every value is shell-quoted
    so a ``"`` or ``$(...)`` in a path or profile name stays a literal.
    An empty *mo2_profile* leaves out ``-p``: MO2 opens its selected one.
    ``mo2=False`` (plain Anomaly) runs the shortcut's exe as it is.
    """
    q = shlex.quote
    if not mo2:
        try:
            arguments = " ".join(q(a) for a in shlex.split(arguments))
        except ValueError:
            arguments = q(arguments)
        return f"STEAM_COMPAT_DATA_PATH={q(prefix)} SteamAppId={int(appid)} %command% {arguments}".rstrip()
    run = f"run {q('--arguments=' + arguments)} " if arguments else "run "
    profile = f"-p {q(mo2_profile)} " if mo2_profile else ""
    return (
        f"STEAM_COMPAT_DATA_PATH={q(prefix)} SteamAppId={int(appid)} %command% "
        f"{profile}{run}-e {q(target)}"
    )


def compat_tool_name(proton_dir: str | Path) -> str:
    """The internal name Steam knows a Proton build by.

    A compatibilitytools.d build names itself in compatibilitytool.vdf.
    Valve's own builds (steamapps/common/Proton ...) have no such file;
    Steam knows them as ``proton_9`` for "Proton 9.0", ``proton_63`` for
    "Proton 6.3", ``proton_experimental``, ``proton_hotfix``.
    """
    proton_dir = Path(proton_dir)
    try:
        text = (proton_dir / "compatibilitytool.vdf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        valve = re.fullmatch(r"Proton (\d+)\.(\d+)", proton_dir.name)
        if valve:
            major, minor = valve.groups()
            return f"proton_{major}" if minor == "0" else f"proton_{major}{minor}"
        named = re.fullmatch(r"Proton(?: -)? (Experimental|Hotfix)", proton_dir.name)
        return f"proton_{named.group(1).lower()}" if named else proton_dir.name
    match = re.search(r'"compat_tools"\s*\{\s*"([^"]+)"', text, re.IGNORECASE)
    return match.group(1) if match else proton_dir.name


def unsigned_appid(exe: str) -> int:
    """The co-op shortcut's appid as Steam keys it in config.vdf."""
    return _shortcut_appid(f'"{exe}"', COOP_SHORTCUT_NAME) & 0xFFFFFFFF


def rungameid(appid: int) -> str:
    """The 64-bit game ID ``steam://rungameid/`` takes for a shortcut."""
    return str((appid << 32) | 0x02000000)


def _block(text: str, start: int) -> tuple[int, int]:
    """(opening brace, closing brace) of the text-VDF block after ``start``."""
    brace = text.index("{", start)
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return brace, index
    raise ValueError("config.vdf has an unclosed block")


def _mapping_span(text: str) -> tuple[int, int] | None:
    match = re.search(r'"CompatToolMapping"\s*\{', text, re.IGNORECASE)
    return _block(text, match.start()) if match else None


def mapped_compat_tool(text: str, appid: int) -> str | None:
    """The compat tool config.vdf assigns to ``appid``, or None."""
    span = _mapping_span(text)
    if span is None:
        return None
    match = re.search(
        rf'"{appid}"\s*\{{[^{{}}]*?"name"\s*"([^"]*)"', text[span[0] : span[1]]
    )
    return match.group(1) if match else None


def set_compat_tool(text: str, appid: int, name: str) -> str:
    """Return config.vdf ``text`` with ``appid`` forced onto tool ``name``.

    Only the one entry is replaced or added; everything else stays as it was.
    Raises ValueError for a file without a recognisable Steam section rather
    than guessing where the mapping belongs.
    """
    if not re.fullmatch(r"[^\"\\\x00-\x1f]+", name):
        raise ValueError(f"Unusable compatibility tool name: {name!r}")
    entry = f'"{appid}"\n\t\t\t\t\t{{\n\t\t\t\t\t\t"name"\t\t"{name}"\n\t\t\t\t\t\t"config"\t\t""\n\t\t\t\t\t\t"priority"\t\t"250"\n\t\t\t\t\t}}'
    span = _mapping_span(text)
    if span is None:
        valve = re.search(r'"Valve"\s*\{', text, re.IGNORECASE)
        steam = valve and re.compile(r'"Steam"\s*\{', re.IGNORECASE).search(text, valve.end())
        if not steam:
            raise ValueError("config.vdf has no Steam section to add the compatibility tool to")
        brace = steam.end() - 1
        return f'{text[: brace + 1]}\n\t\t\t\t"CompatToolMapping"\n\t\t\t\t{{\n\t\t\t\t\t{entry}\n\t\t\t\t}}{text[brace + 1 :]}'
    open_, close = span
    body = text[open_ + 1 : close]
    existing = re.search(rf'"{appid}"\s*\{{[^{{}}]*\}}', body)
    if existing:
        body = body[: existing.start()] + entry + body[existing.end() :]
    else:
        body = body.rstrip() + "\n\t\t\t\t\t" + entry + "\n\t\t\t\t"
    return text[: open_ + 1] + body + text[close:]


def _config_vdf(vdf_path: Path) -> Path:
    return steam_root_for(vdf_path) / "config" / "config.vdf"


def _coop_entry(shortcuts: list[Entry]) -> dict | None:
    for _key, type_byte, value in shortcuts:
        if type_byte == _TYPE_MAP:
            fields = {k: v for k, _t, v in value}
            if fields.get("appname") == COOP_SHORTCUT_NAME:
                return fields
    return None


def coop_shortcut_current(
    vdf_path: Path, *, exe: str, start_dir: str, launch_options: str, proton_name: str | None
) -> bool:
    """True when Steam already has this exact co-op shortcut (and, on Linux,
    its compat tool), so launching needs no Steam restart."""
    try:
        entry = _coop_entry(get_shortcuts_map(read_shortcuts(vdf_path)))
    except ShortcutsFileError:
        return False
    if entry is None or (entry.get("Exe"), entry.get("StartDir"), entry.get("LaunchOptions")) != (
        f'"{exe}"', f'"{start_dir}"', launch_options
    ):
        return False
    # Older builds wrote it with no icon; rewrite once to add one.
    if not entry.get("icon") and bundled_icon().is_file():
        return False
    if proton_name is None:
        return True
    try:
        text = _config_vdf(vdf_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return mapped_compat_tool(text, unsigned_appid(exe)) == proton_name


def write_coop_shortcut(
    vdf_path: Path, *, exe: str, start_dir: str, launch_options: str, proton_name: str | None
) -> int:
    """Add or update the co-op shortcut (and its compat tool); return its appid.

    Steam must not be running (see ``restart_steam``). Both files are parsed
    before either is written, and each gets a ``.gammagui.bak`` first.
    """
    root = read_shortcuts(vdf_path)
    config = _config_vdf(vdf_path)
    appid = unsigned_appid(exe)
    new_config = None
    if proton_name is not None:
        try:
            old_config = config.read_text(encoding="utf-8")
        except FileNotFoundError:
            old_config = '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t}\n\t\t}\n\t}\n}\n'
        new_config = set_compat_tool(old_config, appid, proton_name)

    # One entry, found by name: a changed profile, target or install path
    # updates it in place instead of piling up stale copies.
    shortcuts, kept = [], False
    for key, type_byte, value in get_shortcuts_map(root):
        name = {k: v for k, _t, v in value}.get("appname") if type_byte == _TYPE_MAP else None
        if name == COOP_SHORTCUT_NAME or name in _LEGACY_COOP_NAMES:
            if kept:
                continue
            kept = True
            value = [
                (k, t, f'"{exe}"' if k == "Exe" else launch_options if k == "LaunchOptions" else v)
                for k, t, v in value
            ]
        shortcuts.append((key, type_byte, value))
    shortcuts = add_or_update_shortcut(
        shortcuts,
        COOP_SHORTCUT_NAME,
        exe,
        start_dir,
        icon=install_icon(bundled_icon()),
        launch_options=launch_options,
    )

    if vdf_path.is_file():
        backup = vdf_path.with_name(vdf_path.name + ".gammagui.bak")
        if not backup.exists():
            atomic.write_bytes(backup, vdf_path.read_bytes(), lock=False)
    else:
        vdf_path.parent.mkdir(parents=True, exist_ok=True)
    before = vdf_path.read_bytes() if vdf_path.is_file() else None
    write_shortcuts(vdf_path, set_shortcuts_map(root, shortcuts))
    try:
        _write_compat_config(config, new_config)
    except OSError:
        # A shortcut without its Proton mapping launches with the wrong tool:
        # put the shortcuts back as they were rather than keep half the change.
        if before is None:
            vdf_path.unlink(missing_ok=True)
        else:
            atomic.write_bytes(vdf_path, before, lock=False)
        raise
    install_artwork(vdf_path, appid, COOP_ARTWORK_DIR)
    return appid


def _write_compat_config(config: Path, new_config: str | None) -> None:
    if new_config is not None:
        if config.is_file():
            backup = config.with_name(config.name + ".gammagui.bak")
            if not backup.exists():
                atomic.write_bytes(backup, config.read_bytes(), lock=False)
        else:
            config.parent.mkdir(parents=True, exist_ok=True)
        # No lock files left behind in Steam's own folders (see write_shortcuts).
        atomic.write_bytes(config, new_config.encode("utf-8"), lock=False)


def coop_launch_url(exe: str) -> str:
    return f"steam://rungameid/{rungameid(unsigned_appid(exe))}"


def setup_coop_shortcut(vdf_path: Path, *, restart: bool, **fields) -> None:
    """Write the co-op shortcut, then start it through Steam.

    With ``restart`` (Steam is running) Steam is closed around the write and
    started again with the launch link - one start, never two racing.
    Blocking - run it on a worker thread. Both files are parsed before Steam
    is shut down, so an unreadable one fails without closing Steam for nothing.
    """
    read_shortcuts(vdf_path)
    if fields.get("proton_name") is not None:
        try:
            set_compat_tool(_config_vdf(vdf_path).read_text(encoding="utf-8"), 0, "check")
        except FileNotFoundError:
            pass
    url = coop_launch_url(fields["exe"])

    def write() -> None:
        write_coop_shortcut(vdf_path, **fields)

    if restart:
        restart_steam(steam_root_for(vdf_path), while_stopped=write, relaunch_args=(url,))
    else:
        write()
        open_in_steam(steam_root_for(vdf_path), url)


def open_in_steam(steam_root: Path, url: str) -> None:
    """Hand ``url`` (``steam://...``) to the Steam client matching this
    install - native or Flatpak - starting Steam first if it is closed.
    Raises OSError if Steam could not be started."""
    subprocess.Popen(
        [*steam_launch_command(steam_root), url],
        env=child_environment(),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
