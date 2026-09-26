"""Persistence for GUI-only preferences.

These are separate from the CLI's settings.json (which the CLI owns) and only
cover GUI behaviour, currently the game-launcher runner selection.
"""

from __future__ import annotations

import copy
import json
import math
import shutil
from pathlib import Path

from .atomic import write_text
from .config import gui_settings_path
from .i18n import LANGUAGE_INFO
from .themes import THEME_INFO

_allowed_languages = {code for code, _native, _english in LANGUAGE_INFO}
_allowed_themes = {key for key, _label, _description, _swatches in THEME_INFO}

_DEFAULTS = {
    "runner": "auto",  # "auto" | "umu" | "wine" | "proton:<path-to-proton>"
    "wine_prefix": "",  # WINEPREFIX (Wine) or STEAM_COMPAT_DATA_PATH (Proton)
    "prefixes": {},  # per-runner prefix, keyed by the runner data value
    "target": "",  # last selected launch target title
    "theme": "gamma",  # key into themes.THEMES
    "language": "en",  # key into i18n.LANGUAGE_INFO
    "start_page": "dashboard",  # nav page shown on launch (key into main_window.NAV_ITEMS)
    "font_size": 13,  # base UI font size in px; scales every QSS font
    "font_family": "Exo 2",  # UI font family; applied via QSS font-family
    "always_gamemoderun": False,  # wrap every launch command in gamemoderun
    "autostart": False,  # add to XDG autostart so the app starts at login
    "custom_launch_options": "",  # extra tokens prepended to the launch command
    "mo2_display_dpi": 120,  # Wine display DPI for Mod Organizer 2 (125%)
    "tool_overrides": {},  # manually selected Linux tools and runner locations
    "move_dest": "",  # in-progress Move Game destination (cleared on completion)
    "move_expected": [],  # destination folder names owned by an in-progress move
    "window_width": 1080,
    "window_height": 950,
    "last_update_check_ts": 0.0,  # time.time() of the last scheduled background update check
    "update_notifications": True,  # desktop notification when a GAMMA or COMMANDER update comes out
    "notified_gamma_version": "",  # latest GAMMA version already notified about (notify once per version)
    "notified_commander_tag": "",  # COMMANDER release tag already notified about
    "playtime_seconds": {},  # accumulated play time per profile name, in seconds
    "last_played_ts": {},  # time.time() a session last ended, per profile name
    "user_created_categories": {},  # profile name -> list of category names the user created (deletable); official GAMMA categories are never in this list
    "discord_rpc_enabled": False,  # show "Playing S.T.A.L.K.E.R. GAMMA" on Discord
    "discord_show_mods": True,  # add the enabled mod count under the presence line
    "discord_show_playtime": True,  # add the profile's total playtime there too
    "discord_client_id": "",  # optional override; empty = COMMANDER's own Discord app
    # Steam Deck Mode (the separate `Steamdeck` GUI). Deliberately a small,
    # self-contained set: Deck Mode shares runner/prefix/target/theme/language
    # and the playtime tallies with the desktop UI, because switching modes
    # must not change which profile or runner you play with.
    "deck_mode_preference": "ask",  # "ask" | "always" | "never" - auto-start on Deck hardware
    "deck_font_scale": 100,  # percent; scales the Deck UI's fonts only, never the desktop UI
    "deck_start_screen": "play",  # Deck screen shown on launch (key into Steamdeck.screens.SCREENS)
    "deck_runner_confirmed": False,  # user picked a runner once in Deck Mode
    "deck_finish_sound": True,  # play the PDA chime when a long Deck task ends
    "deck_finish_rumble": True,  # buzz the controller when a long Deck task ends
    "welcome_hidden": False,  # "Don't show again" on the Welcome screen (desktop and Deck Mode)
    "welcome_seen_version": "",  # COMMANDER version the Welcome screen last showed for (always shown after an update)
    "update_channel": "stable",  # "stable" | "unstable" - COMMANDER self-update channel (unstable includes pre-releases)
    "pending_settings_restore": {},  # profile -> pre-reset backup to restore after the GAMMA reinstall (game_backup)
}

_cache: dict | None = None
_cache_path_str: str = ""
_cache_file_mtime: float = 0.0


def load_gui_settings() -> dict:
    global _cache, _cache_path_str, _cache_file_mtime
    path = gui_settings_path()
    try:
        current_mtime = path.stat().st_mtime
    except OSError:
        current_mtime = 0.0
    if (
        _cache is not None
        and str(path) == _cache_path_str
        and current_mtime == _cache_file_mtime
    ):
        return copy.deepcopy(_cache)
    data = copy.deepcopy(_DEFAULTS)
    if path.exists():
        try:
            # utf-8-sig tolerates a BOM; ValueError covers UnicodeDecodeError
            # too - garbage bytes here used to crash startup outright.
            stored = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            # Try the last-known-good backup if the main file is corrupt.
            backup = path.with_suffix(".json.last-good")
            if backup.exists():
                try:
                    stored = json.loads(backup.read_text(encoding="utf-8-sig"))
                except (ValueError, OSError):
                    return data
            else:
                return data
        if isinstance(stored, dict):
            data.update(stored)
    if not isinstance(data.get("runner"), str):
        data["runner"] = "auto"
    for key in ("wine_prefix", "target", "custom_launch_options"):
        if not isinstance(data.get(key), str):
            data[key] = _DEFAULTS[key]
    if not isinstance(data.get("theme"), str) or data["theme"] not in _allowed_themes:
        data["theme"] = "gamma"
    if not isinstance(data.get("language"), str) or data["language"] not in _allowed_languages:
        data["language"] = "en"
    # A hand-edited or corrupted settings file can hold any JSON type here,
    # and an unhashable one (a list, an object) makes `x not in {...}` raise
    # TypeError rather than simply fail the check - which would crash the
    # app during startup, before any window exists to report it.
    if data.get("update_channel") not in ("stable", "unstable"):
        data["update_channel"] = "stable"
    if not isinstance(data.get("start_page"), str) or data.get("start_page") not in {
        "dashboard",
        "systemcheck",
        "play",
        "install",
        "update",
        "modmanager",
        "profiles",
        "utilities",
        "help",
        "about",
    }:
        data["start_page"] = "dashboard"
    if not isinstance(data.get("deck_mode_preference"), str) or data[
        "deck_mode_preference"
    ] not in {"ask", "always", "never"}:
        data["deck_mode_preference"] = "ask"
    # Hardcoded for the same reason start_page above is: deriving this from
    # Steamdeck.screens.SCREENS would make this module import the Deck GUI,
    # inverting the dependency (Steamdeck imports commander_gui, never the
    # other way round). tests/test_steamdeck.py asserts the two stay in sync.
    if not isinstance(data.get("deck_start_screen"), str) or data[
        "deck_start_screen"
    ] not in {
        "dashboard",
        "play",
        "install",
        "update",
        "mods",
        "system",
        "utilities",
        "settings",
    }:
        data["deck_start_screen"] = "play"
    try:
        font_size = int(data.get("font_size", 13))
    except (TypeError, ValueError, OverflowError):
        # OverflowError: json.loads() accepts the bare "Infinity"/"-Infinity"
        # tokens as real floats, and int() on a non-finite float raises
        # OverflowError rather than ValueError - a corrupt/hand-edited
        # gui-settings.json with "font_size": Infinity would otherwise crash
        # load_gui_settings() itself, called during startup.
        font_size = 13
    data["font_size"] = min(22, max(9, font_size))
    try:
        deck_font_scale = int(data.get("deck_font_scale", 100))
    except (TypeError, ValueError, OverflowError):
        # Same OverflowError guard as font_size above: a hand-edited
        # gui-settings.json containing the bare JSON token Infinity parses
        # to a float that int() refuses to convert.
        deck_font_scale = 100
    data["deck_font_scale"] = min(150, max(80, deck_font_scale))
    for key in ("window_width", "window_height"):
        try:
            size = int(data.get(key, _DEFAULTS[key]))
        except (TypeError, ValueError, OverflowError):
            size = _DEFAULTS[key]
        data[key] = min(3840, max(640, size))
    _allowed_fonts = {
        "Exo 2",
        "Noto Sans",
        "DejaVu Sans",
        "Liberation Sans",
        "Inter",
    }
    font_family = data.get("font_family")
    if not isinstance(font_family, str) or font_family not in _allowed_fonts:
        data["font_family"] = "Exo 2"
    if not isinstance(data.get("welcome_seen_version"), str):
        data["welcome_seen_version"] = ""
    if not isinstance(data.get("discord_client_id"), str):
        data["discord_client_id"] = ""
    if not isinstance(data.get("update_notifications"), bool):
        data["update_notifications"] = True
    for key in ("notified_gamma_version", "notified_commander_tag"):
        if not isinstance(data.get(key), str):
            data[key] = ""
    for key in (
        "always_gamemoderun",
        "autostart",
        "discord_rpc_enabled",
        "discord_show_mods",
        "discord_show_playtime",
        "deck_runner_confirmed",
        "welcome_hidden",
    ):
        v = data.get(key)
        if isinstance(v, bool):
            pass
        elif isinstance(v, str) and v.strip().lower() in {"true", "false"}:
            v = v.strip().lower() == "true"
        else:
            v = False
        data[key] = v
    for key in ("prefixes", "tool_overrides"):
        value = data.get(key)
        data[key] = (
            {str(k): v for k, v in value.items() if isinstance(v, str)}
            if isinstance(value, dict)
            else {}
        )
    playtime = data.get("playtime_seconds")
    data["playtime_seconds"] = (
        {
            str(k): float(v)
            for k, v in playtime.items()
            if isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(v)
            and v >= 0
        }
        if isinstance(playtime, dict)
        else {}
    )
    last_played = data.get("last_played_ts")
    data["last_played_ts"] = (
        {
            str(k): float(v)
            for k, v in last_played.items()
            if isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(v)
            and v >= 0
        }
        if isinstance(last_played, dict)
        else {}
    )
    # Left by the removed Flip Priority button; dropped on load.
    data.pop("flip_priority_pending", None)
    user_categories = data.get("user_created_categories")
    data["user_created_categories"] = (
        {
            str(k): [n for n in v if isinstance(n, str)]
            for k, v in user_categories.items()
            if isinstance(v, list)
        }
        if isinstance(user_categories, dict)
        else {}
    )
    if not isinstance(data.get("move_dest"), str):
        data["move_dest"] = ""
    if not isinstance(data.get("move_expected"), list):
        data["move_expected"] = []
    data["move_expected"] = [x for x in data["move_expected"] if isinstance(x, str)]
    try:
        dpi = int(data.get("mo2_display_dpi", 120))
    except (TypeError, ValueError, OverflowError):
        dpi = 120
    data["mo2_display_dpi"] = dpi if dpi in {96, 120, 144, 168, 192} else 120
    _cache = copy.deepcopy(data)
    _cache_path_str = str(path)
    try:
        _cache_file_mtime = path.stat().st_mtime
    except OSError:
        _cache_file_mtime = 0.0
    return data


def save_gui_settings(**changes) -> None:
    global _cache, _cache_path_str, _cache_file_mtime
    path = gui_settings_path()
    data = load_gui_settings()
    data.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve a last-known-good backup before overwriting - but only if the
    # current file is actually valid JSON. Copying a corrupted file over the
    # backup would destroy the one thing load_gui_settings() falls back to
    # when corruption strikes, leaving no way to recover the next time.
    if path.exists():
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            # About to be overwritten: keep the corrupt original for
            # inspection/recovery instead of losing it silently (the same
            # courtesy settings.py extends to the CLI's settings.json).
            try:
                shutil.copy2(path, path.with_suffix(".json.corrupt"))
            except OSError:
                pass
        else:
            try:
                backup = path.with_suffix(".json.last-good")
                shutil.copy2(path, backup)
            except OSError:
                pass
    write_text(path, json.dumps(data, indent=2) + "\n")
    _cache = None
    _cache_path_str = ""
    _cache_file_mtime = 0.0


def configured_wine_prefix() -> str:
    """The real WINEPREFIX implied by the saved runner + prefix selection.

    The Play page stores whatever the prefix box holds, but for a Steam Proton
    runner that value is ``STEAM_COMPAT_DATA_PATH`` and the actual Wine prefix
    lives one level down in ``pfx``. Tools driven against the prefix directly
    (winetricks) must use the resolved path, not the stored one.
    """
    from .launcher import LaunchError, wine_prefix_for  # deferred: leaf-ish

    # Derived from the runner a launch or a dependency install would really
    # use, not from the saved runner *name*: "auto" can resolve to Steam
    # Proton when umu-run is missing, whose prefix is <compat>/pfx rather
    # than umu's default - checking the name made dependency status (and
    # log dumps) read one prefix while installs wrote to another.
    try:
        env = configured_runner().env
    except (LaunchError, OSError, ValueError):
        env = {}
    compat = env.get("STEAM_COMPAT_DATA_PATH")
    if compat:
        return str(Path(compat) / "pfx")
    if env.get("WINEPREFIX"):
        return env["WINEPREFIX"]
    state = load_gui_settings()
    kind = state.get("runner") or "auto"
    return wine_prefix_for(kind, saved_prefix_for(kind, state))


def saved_prefix_for(kind: str, state: dict | None = None) -> str:
    """The prefix saved for runner ``kind``, or "" for the runner's default.

    The one rule every part of COMMANDER uses to pick a runner's prefix -
    the Play page's launch, Winecfg and the display scale on the Settings
    page, dependency installs and the prefix repair. Each used to do its
    own lookup, and they disagreed: after the runner was changed in
    Settings or on the Dashboard (which save only the runner), Winecfg
    fell back to the previous runner's prefix while MO2 launched in the new
    runner's default one - Winecfg changes never reached MO2.

    The single legacy ``wine_prefix`` is honoured only for settings from
    before prefixes were kept per runner (no ``prefixes`` map at all), and
    only for the runner it was saved with.
    """
    from pathlib import Path

    state = load_gui_settings() if state is None else state
    prefixes = state.get("prefixes") or {}
    saved = prefixes.get(kind) or ""
    if not saved and not prefixes and kind == (state.get("runner") or "auto"):
        saved = state.get("wine_prefix") or ""
    if kind.startswith("proton:") and Path(saved).name == "umu-default":
        # Older builds could carry the UMU default into a Steam Proton runner.
        saved = ""
    return saved


def configured_runner():
    """The Runner the saved settings describe, resolved.

    The same choice the Play page makes from its combo and prefix box, taken
    from the saved ``runner`` key and the raw (not ``pfx``-resolved) prefix
    stored for it. Anything that must act on the game's prefix with the
    game's own Wine - installing winetricks verbs, repairing the prefix -
    goes through this so it can never pick a different Wine than a launch
    would.
    """
    from .launcher import resolve_runner  # deferred: keeps this module leaf-ish

    state = load_gui_settings()
    kind = state.get("runner") or "auto"
    return resolve_runner(kind, saved_prefix_for(kind, state))
