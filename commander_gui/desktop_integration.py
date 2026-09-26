"""How the running COMMANDER shows up in Linux desktop tools.

Three things decide what a process monitor such as KDE's System Monitor
shows for us:

* The process name. The kernel keeps only 15 characters (``comm``), so
  "STALKER COMMANDER" is cut to "STALKER COMMAND". Monitors that notice the
  truncation (libksysguard does) take the full name from the first word of
  ``/proc/<pid>/cmdline`` instead, when it starts with the short name - so
  that word is rewritten too.
* The Applications view. It groups processes by their systemd scope
  (``app-<desktop id>-<n>.scope``) and names the group after the matching
  ``.desktop`` file. A launch from a terminal or a script sits in the
  terminal's scope, so COMMANDER moves itself into a scope of its own.
* A ``.desktop`` entry for that name and icon. Packaged installs ship one;
  an AppImage or a source checkout has none, so one is written to the
  user's applications folder (it also puts COMMANDER in the app menu).
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .atomic import write_bytes

PROCESS_TITLE = "STALKER COMMANDER"
DESKTOP_ID = "stalker-gamma-commander"

#: Marks a .desktop entry this module wrote, so it may be rewritten when the
#: install moves - and so one a package or the user put there never is.
_GENERATED_KEY = "X-Commander-Generated=true"

_PR_SET_NAME = 15


def set_process_title(title: str = PROCESS_TITLE) -> None:
    """Show ``title`` as this process's name in process monitors."""
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None)
        libc.prctl(_PR_SET_NAME, title.encode()[:15], 0, 0, 0)
    except (AttributeError, OSError):
        pass
    try:
        _rewrite_argv0(title)
    except (OSError, ValueError, IndexError):
        pass


def build_cmdline(old: bytes, title: str) -> bytes | None:
    """The argument area ``old`` with its first word replaced by ``title``.

    Padded with NULs to exactly ``len(old)`` - the kernel's argument area
    cannot grow. The remaining arguments are kept where they fit, dropped
    where they don't; None if even the title alone does not fit.
    """
    args = old.rstrip(b"\0").split(b"\0")
    head = title.encode()
    for new in (b"\0".join([head, *args[1:]]) + b"\0", head + b"\0"):
        if len(new) <= len(old):
            return new.ljust(len(old), b"\0")
    return None


def _rewrite_argv0(title: str) -> None:
    stat = Path("/proc/self/stat").read_bytes()
    # The name field is parenthesised and may hold spaces, so count fields
    # from after its closing parenthesis: that is field 3 (state), which
    # puts arg_start (field 48) and arg_end (field 49) at 45 and 46.
    fields = stat[stat.rindex(b")") + 2 :].split()
    start, end = int(fields[45]), int(fields[46])
    with open("/proc/self/mem", "r+b", buffering=0) as mem:
        mem.seek(start)
        old = mem.read(end - start)
        # Write nothing unless the area really is our own argv - a wrong
        # address here would corrupt live memory.
        if not old.startswith(os.fsencode(sys.orig_argv[0]) + b"\0"):
            return
        new = build_cmdline(old, title)
        if new is None:
            return
        mem.seek(start)
        mem.write(new)


def scope_unit_name(desktop_id: str, pid: int) -> str:
    """systemd scope name that desktop tools read as ``desktop_id``.

    Follows the XDG convention (``app-<id>-<random>.scope``), in which a
    ``-`` inside the id is escaped so it does not end the id early.
    """
    return f"app-{desktop_id.replace('-', chr(92) + 'x2d')}-{pid}.scope"


def join_app_scope(desktop_id: str = DESKTOP_ID) -> bool:
    """Move this process into its own systemd scope named after ``desktop_id``.

    Skipped when a launcher already put it in one (a menu launch). Returns
    True if it is in one or systemd accepted the request - the move itself
    lands a moment later, when systemd runs the job.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    escaped = desktop_id.replace("-", "\\x2d")
    if f"app-{escaped}-" in cgroup or f"app-{escaped}@" in cgroup:
        return True
    busctl = shutil.which("busctl")
    if not busctl:
        return False
    pid = os.getpid()
    try:
        result = subprocess.run(
            [
                busctl, "--user", "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "StartTransientUnit", "ssa(sv)a(sa(sv))",
                scope_unit_name(desktop_id, pid), "fail",
                "1", "PIDs", "au", "1", str(pid),
                "0",
            ],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _data_dirs() -> list[Path]:
    home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    system = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(home), *(Path(p) for p in system.split(":") if p)]


def menu_entry_path() -> Path:
    return _data_dirs()[0] / "applications" / f"{DESKTOP_ID}.desktop"


def menu_entry_content(exec_line: str, icon: str, path: str | None) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={PROCESS_TITLE}",
        "GenericName=STALKER GAMMA Mod Manager",
        "Comment=Install, update and launch the S.T.A.L.K.E.R. Anomaly + GAMMA mod pack",
        f"Exec={exec_line}",
        f"Icon={icon}",
        f"StartupWMClass={DESKTOP_ID}",
        "Terminal=false",
        "Categories=Game;",
        "Keywords=stalker;anomaly;gamma;mods;modorganizer;",
        _GENERATED_KEY,
    ]
    if path:
        lines.append(f"Path={path}")
    return "\n".join(lines) + "\n"


def ensure_menu_entry() -> Path | None:
    """Install or refresh COMMANDER's own ``.desktop`` entry; its path or None.

    Leaves alone any entry this module did not write - a package's in
    /usr/share, or one the user made - and does nothing on other systems.
    """
    if not sys.platform.startswith("linux"):
        return None
    target = menu_entry_path()
    for base in _data_dirs():
        existing = base / "applications" / f"{DESKTOP_ID}.desktop"
        if not existing.is_file():
            continue
        if existing != target:
            return None
        try:
            if _GENERATED_KEY not in existing.read_text(encoding="utf-8"):
                return None
        except (OSError, UnicodeDecodeError):
            return None

    from .autostart import _desktop_exec
    from .steam_shortcuts import (
        ICON_DEST,
        bundled_icon,
        commander_launch_target,
        install_icon,
    )

    exe, start_dir, base_options = commander_launch_target()
    try:
        exec_line = _desktop_exec([exe, *base_options.split()])
    except ValueError:
        return None
    icon = _current_icon(bundled_icon(), ICON_DEST, install_icon) or DESKTOP_ID
    content = menu_entry_content(exec_line, icon, start_dir)
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        # No lock file: it would be left behind as a dotfile in the
        # desktop's applications folder.
        write_bytes(target, content.encode("utf-8"), lock=False)
    except (OSError, UnicodeDecodeError):
        return None
    return target


def _current_icon(source: Path, dest: Path, install) -> str:
    """``dest`` holding ``source``'s image, copied only when it differs."""
    try:
        if dest.is_file() and dest.read_bytes() == source.read_bytes():
            return str(dest)
    except OSError:
        pass
    return install(source, dest)
