"""Steam Deck Mode: hardware detection and the in-place relaunch.

Deck Mode is a second, minimal GUI (the top-level ``Steamdeck`` package)
sized for the Steam Deck's 1280x800 screen. It is not a mode toggle inside
the running window - the app genuinely closes and reopens, by replacing its
own process image with ``os.execve``. That gives a clean Qt application,
a freshly loaded theme/language, and no half-torn-down desktop widgets,
while keeping the same PID so the single-instance lock file stays coherent.

This module is deliberately Qt-free and side-effect-free at import: it is
imported by ``commander_gui.main`` before ``QApplication`` exists, it is
exercised by the AppImage build's headless payload import check, and every
function takes its inputs by argument so the tests never have to patch
global state.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

#: The command-line flag that selects Deck Mode. Forwarded by run.sh,
#: packaging/stalker-gamma-commander.sh and the AppImage's AppRun, all of
#: which end in ``-m commander_gui "$@"``.
DECK_FLAG = "--deck"

#: Counts how many times this process has re-exec'd itself. A bug that lost
#: the flag would otherwise ping-pong forever, and in Steam's Game Mode there
#: is no terminal and no window manager to kill it from.
DEPTH_ENV = "COMMANDER_DECK_RELAUNCH_DEPTH"
MAX_RELAUNCH_DEPTH = 2

#: DMI product names for the two Steam Deck revisions.
DECK_PRODUCT_NAMES: dict[str, str] = {
    "Jupiter": "lcd",   # Steam Deck LCD (2022)
    "Galileo": "oled",  # Steam Deck OLED (2023)
}

_DMI_PRODUCT_NAME = Path("/sys/class/dmi/id/product_name")


class DeckRelaunchError(RuntimeError):
    """Raised when a relaunch must not be attempted at all."""


def deck_requested(argv: Sequence[str]) -> bool:
    """True if ``--deck`` appears anywhere in ``argv``."""
    return DECK_FLAG in tuple(argv)


def strip_deck_flag(argv: Sequence[str]) -> list[str]:
    """``argv`` with every ``--deck`` removed.

    Qt ignores unknown arguments, but QApplication.arguments() is visible to
    anything that reads it, so hand Qt an argv that reflects what it actually
    got to parse.
    """
    return [arg for arg in argv if arg != DECK_FLAG]


def relaunch_argv(
    *,
    deck: bool,
    orig_argv: Sequence[str] | None = None,
    executable: str | None = None,
) -> list[str]:
    """Build the argv for re-exec'ing this process into (or out of) Deck Mode.

    Built from ``sys.orig_argv``, not ``sys.argv``, because the interpreter
    flags matter: the AppImage's AppRun runs ``python3.12 -s -P -m
    commander_gui``, where ``-s`` keeps the user's ~/.local site-packages out
    of sys.path and ``-P`` stops a stray ``commander_gui`` next to the cwd
    from shadowing the bundled copy. ``sys.argv`` has already dropped both,
    so relaunching from it would hand the new process a different (and on
    some machines, broken - a mismatched system PySide6) interpreter setup.

    ``argv[0]`` is replaced with ``executable`` (defaulting to
    ``sys.executable``): ``orig_argv[0]`` is whatever string invoked the
    interpreter, which may be a relative path or a bare ``python3`` that is
    not on PATH by the time execve resolves it.
    """
    base = list(sys.orig_argv if orig_argv is None else orig_argv)
    if not base:
        # Embedded interpreters may expose no orig_argv at all.
        base = [sys.executable, "-m", "commander_gui"]
    argv = [executable or sys.executable, *(a for a in base[1:] if a != DECK_FLAG)]
    if deck:
        argv.append(DECK_FLAG)
    return argv


def relaunch_exec(
    *,
    deck: bool,
    release_lock: Callable[[], None],
    pre_exec: Callable[[], None] | None = None,
) -> bool:
    """Replace this process with a fresh one in the requested mode.

    Does not return on success. Returns ``False`` if ``execve`` itself failed
    (ENOMEM, ENOEXEC, a deleted interpreter), leaving the caller to report it
    - by then the window is already closed, so there is nothing to restore.

    ``release_lock`` must drop the single-instance QLockFile *before* the
    exec. QLockFile records the owning PID, and execve keeps the PID, so the
    replacement process would otherwise find a lock whose owner is alive and
    whose process name matches, conclude that COMMANDER is already running,
    and exit without ever showing a window.
    """
    try:
        depth = int(os.environ.get(DEPTH_ENV, "0") or 0)
    except ValueError:
        depth = 0
    if depth >= MAX_RELAUNCH_DEPTH:
        raise DeckRelaunchError(
            f"refusing to relaunch: already re-exec'd {depth} times"
        )
    env = dict(os.environ)
    env[DEPTH_ENV] = str(depth + 1)
    argv = relaunch_argv(deck=deck)
    if pre_exec is not None:
        pre_exec()
    release_lock()
    try:
        os.execve(argv[0], argv, env)
    except OSError:
        return False
    return False  # unreachable: execve either replaces the process or raises


def clear_relaunch_depth() -> None:
    """Forget the re-exec counter once a window has stayed up a while.

    Called from a timer in ``main()`` so that a session where the user
    deliberately toggles modes several times is not mistaken for a loop.
    """
    os.environ[DEPTH_ENV] = "0"


def steam_deck_model(
    *,
    dmi_path: Path = _DMI_PRODUCT_NAME,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return ``"lcd"``/``"oled"``/``"unknown"`` on a Steam Deck, else None.

    DMI is checked first because it is the only source that cannot be faked
    by accident. ``SteamDeck=1`` is a useful fallback (Steam sets it for
    processes it launches on Deck hardware) but users also set it by hand to
    coax Deck-targeted behaviour out of games, so it never gets to claim a
    specific model.
    """
    environ = os.environ if env is None else env
    try:
        name = dmi_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        # Missing on non-Deck hardware, and unreadable under some sandboxes.
        name = ""
    model = DECK_PRODUCT_NAMES.get(name)
    if model is not None:
        return model
    if str(environ.get("SteamDeck", "")).strip() == "1":
        return "unknown"
    return None


def is_steam_deck(**kwargs) -> bool:
    """True if this machine looks like a Steam Deck."""
    return steam_deck_model(**kwargs) is not None


def in_game_mode(env: Mapping[str, str] | None = None) -> bool:
    """True if running inside SteamOS Game Mode (a gamescope session).

    Game Mode has no window manager: gamescope composites one surface, so a
    window that does not full-screen itself is rendered at whatever size it
    asked for with no way for the user to resize it. This is only consulted
    for that decision, never for whether to offer Deck Mode.
    """
    environ = os.environ if env is None else env
    if str(environ.get("SteamDeck", "")).strip() != "1":
        return False
    markers = (
        environ.get("XDG_CURRENT_DESKTOP", ""),
        environ.get("XDG_SESSION_DESKTOP", ""),
        environ.get("XDG_SESSION_TYPE", ""),
    )
    return any("gamescope" in str(value).lower() for value in markers)
