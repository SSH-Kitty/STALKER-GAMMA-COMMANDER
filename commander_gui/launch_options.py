"""Steam-style launch options ("VAR=1 wrapper %command% --arg").

Shared by the desktop Play page and Deck Mode: the one-click options both
offer, switching one on or off in the options text, and applying the text
to a built launch command. Qt-free.
"""

from __future__ import annotations

import re
import shlex

from .deck_launch import in_game_mode

#: One-click launch options, in labelled groups: (group heading, ((label,
#: the text added to the launch options, tooltip), ...)).
GROUPS = (
    ("Display", (
        ("Gamescope", "gamescope -f --", "Run the game fullscreen inside Gamescope. Helps with resolution and alt-tab problems."),
        ("FSR upscaling", "WINE_FULLSCREEN_FSR=1", "Sharpen and upscale with AMD FSR when the game runs below your screen's resolution. Proton-GE only."),
        ("Native Wayland", "PROTON_ENABLE_WAYLAND=1", "Run in a native Wayland window instead of XWayland. Experimental, Proton-GE 10 or newer."),
        ("OpenGL", "PROTON_USE_WINED3D=1", "Use OpenGL instead of Vulkan (DXVK). Try it if the game won't start or looks broken on an older GPU."),
    )),
    ("Fixes", (
        ("No Esync", "PROTON_NO_ESYNC=1", "Turn off Esync. Can help if the game stutters or hangs."),
        ("No Fsync", "PROTON_NO_FSYNC=1", "Turn off Fsync. Can help if the game stutters or hangs."),
        ("Fix audio crackle", "PULSE_LATENCY_MSEC=60", "Bigger sound buffer. Fixes crackling or stuttering audio."),
        ("No Steam overlay", "LD_PRELOAD=", "Keep the Steam overlay out of the game when COMMANDER was started from Steam. Can fix stutter. Co-op keeps the overlay for Steam invites."),
    )),
    ("Overlay and logs", (
        ("MangoHud", "mangohud", "Show the MangoHud overlay (FPS, frame times, temperatures)."),
        ("FPS counter", "DXVK_HUD=fps", "Show DXVK's small FPS counter in the corner."),
        ("Proton log", "PROTON_LOG=1", "Write a Proton log file to your home folder. Useful for crash reports."),
        ("Quiet Wine logs", "WINEDEBUG=-all", "Turn off Wine's debug messages. Slightly less CPU work. Switches Proton log off, since it would leave the log empty."),
    )),
)

#: Options that undo each other: switching one on switches the other off.
#: WINEDEBUG=-all silences the Wine messages PROTON_LOG=1 is there to catch.
_CONFLICTS = {"PROTON_LOG=1": "WINEDEBUG=-all", "WINEDEBUG=-all": "PROTON_LOG=1"}

#: The "No Steam overlay" option. Co-op leaves it out (see
#: play_page.coop_shortcut_fields): Steam invites need the overlay.
NO_STEAM_OVERLAY = "LD_PRELOAD="

_GAMESCOPE = ["gamescope", "-f", "--"]
_ENV_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _find(tokens: list[str], seq: list[str]) -> int:
    """Index where seq appears in tokens as a run, or -1."""
    for i in range(len(tokens) - len(seq) + 1):
        if tokens[i : i + len(seq)] == seq:
            return i
    return -1


def is_on(text: str, option: str) -> bool:
    return _find(split(text), shlex.split(option)) >= 0


def toggle(text: str, option: str, on: bool) -> str:
    """text with option switched on or off. An option can be several
    tokens ("gamescope -f --")."""
    seq = shlex.split(option)
    tokens = split(text)
    drops = [seq]
    if on and option in _CONFLICTS:
        drops.append(shlex.split(_CONFLICTS[option]))
    for drop in drops:
        at = _find(tokens, drop)
        if at >= 0:
            del tokens[at : at + len(drop)]
    if on:
        if "=" in seq[0]:
            # Variables go in front of the game, so before %command% when
            # the user wrote one.
            at = tokens.index("%command%") if "%command%" in tokens else len(tokens)
        else:
            # Wrappers go first, so gamescope wraps mangohud and not the
            # other way round.
            at = 0
        tokens[at:at] = seq
    return shlex.join(tokens)


def apply(command: list[str], env: dict[str, str], text: str, *, game_mode: bool | None = None) -> list[str]:
    """command with the options text applied; variables go into env.

    ``game_mode`` overrides in_game_mode() for the Gamescope option."""
    tokens = split(text.strip())
    if not tokens:
        return command
    # What comes after %command% belongs after the command, as its
    # arguments; it used to be moved in front of it along with the wrappers.
    suffix: list[str] = []
    if "%command%" in tokens:
        marker = tokens.index("%command%")
        tokens, suffix = tokens[:marker], [
            t for t in tokens[marker + 1 :] if t != "%command%"
        ]
    prefix: list[str] = []
    for token in tokens:
        if _ENV_VAR.match(token):
            key, _, value = token.partition("=")
            env[key] = value
        else:
            prefix.append(token)
    # Game Mode already is a gamescope session; a nested one from the
    # Gamescope option only adds a second compositor.
    at = _find(prefix, _GAMESCOPE)
    if at >= 0 and (in_game_mode() if game_mode is None else game_mode):
        del prefix[at : at + len(_GAMESCOPE)]
    return [*prefix, *command, *suffix]



def for_coop(text: str) -> str:
    """The options text as co-op uses it: all but "No Steam overlay", since
    co-op keeps the overlay for Steam invites."""
    return toggle(text, NO_STEAM_OVERLAY, False)


def extends(steam: str, base: str) -> bool:
    """True when Steam LaunchOptions ``steam`` is ``base`` plus some launch
    options (see for_steam)."""
    base_env: dict[str, str] = {}
    base_command = apply(["%command%"], base_env, base, game_mode=True)
    env: dict[str, str] = {}
    command = apply(["%command%"], env, steam, game_mode=True)
    return all(env.get(key) == value for key, value in base_env.items()) and (
        _find(command, base_command) >= 0
    )


def for_steam(text: str, base: str) -> str:
    """Steam LaunchOptions ``base`` with the options text merged in.

    Steam runs LaunchOptions through ``sh -c``, where variables only count
    in front of everything else, so both sets of variables lead; base's
    come second and win, since a shortcut can't run without them.
    Gamescope is left out in every mode: Steam runs the shortcut in Game
    Mode's own gamescope too, and a shortcut that changed with the mode
    would need a Steam restart on every switch.
    """
    base_env: dict[str, str] = {}
    command = apply(["%command%"], base_env, base, game_mode=True)
    env: dict[str, str] = {}
    command = apply(command, env, text, game_mode=True)
    env.update(base_env)
    assignments = [f"{key}={shlex.quote(value)}" for key, value in env.items()]
    return " ".join([*assignments, shlex.join(command)])
