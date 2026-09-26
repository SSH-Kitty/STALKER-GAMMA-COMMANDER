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

import re
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
    except (ValueError, IndexError) as exc:
        # A truncated file runs a bytes.index()/slice off the end; an
        # unknown type byte raises ValueError. Either way: do not guess.
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
            new_shortcuts = list(shortcuts)
            new_shortcuts[index] = (key, _TYPE_MAP, fields)
            return new_shortcuts
    existing_indices = [
        int(key) for key, t, _v in shortcuts if t == _TYPE_MAP and key.isdigit()
    ]
    next_index = str(max(existing_indices, default=-1) + 1)
    return [*shortcuts, (next_index, _TYPE_MAP, fields)]


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
        backup.write_bytes(vdf_path.read_bytes())
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


def _persona_name(steam_root: Path, account_id: str) -> str | None:
    """The account's display name, read from that Steam root's loginusers.vdf.

    Best-effort only - a friendlier label than a bare numeric ID when it
    works, never a reason to fail the actual lookup when it doesn't (a
    missing/unreadable/unrecognised-shape file just falls back to the ID).
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
    name_match = re.search(r'"PersonaName"\s+"([^"]*)"', block_match.group(1))
    return name_match.group(1) if name_match else None


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
    return ".var/app/com.valvesoftware.Steam" in str(steam_root)


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
    if not _wait_for_steam_to_exit(exit_timeout):
        raise OSError("Steam did not close in time - nothing was changed.")
    try:
        if while_stopped is not None:
            while_stopped()
    finally:
        # Steam comes back even when the write failed: the user asked for a
        # shortcut, not to be left with Steam closed. The failure still
        # reaches the caller once it has been restarted.
        subprocess.Popen(
            launch_cmd,
            env=child_environment(),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


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
