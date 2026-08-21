"""Persistence for GUI-only preferences.

These are separate from the CLI's settings.json (which the CLI owns) and only
cover GUI behaviour, currently the game-launcher runner selection.
"""

from __future__ import annotations

import copy
import json

from .atomic import write_text
from .config import gui_settings_path

_DEFAULTS = {
    "runner": "auto",  # "auto" | "umu" | "wine" | "proton:<path-to-proton>"
    "wine_prefix": "",  # WINEPREFIX (Wine) or STEAM_COMPAT_DATA_PATH (Proton)
    "prefixes": {},  # per-runner prefix, keyed by the runner data value
    "target": "",  # last selected launch target title
    "theme": "gamma",  # key into themes.THEMES
    "start_page": "dashboard",  # nav page shown on launch (key into main_window.NAV_ITEMS)
    "font_size": 13,  # base UI font size in px; scales every QSS font
    "font_family": "Exo 2",  # UI font family; applied via QSS font-family
    "always_gamemoderun": False,  # wrap every launch command in gamemoderun
    "autostart": False,  # add to XDG autostart so the app starts at login
    "custom_launch_options": "",  # extra tokens prepended to the launch command
    "tool_overrides": {},  # manually selected Linux tools and runner locations
    "move_dest": "",  # in-progress Move Game destination (cleared on completion)
    "move_expected": [],  # destination folder names owned by an in-progress move
    "window_width": 1080,
    "window_height": 950,
}


def load_gui_settings() -> dict:
    path = gui_settings_path()
    data = copy.deepcopy(_DEFAULTS)
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return data
        if isinstance(stored, dict):
            data.update(stored)
    if not isinstance(data.get("runner"), str):
        data["runner"] = "auto"
    for key in ("wine_prefix", "target", "custom_launch_options"):
        if not isinstance(data.get(key), str):
            data[key] = _DEFAULTS[key]
    if not isinstance(data.get("theme"), str) or data["theme"] not in {
        "gamma",
        "midnight",
        "terminal",
        "black",
        "dusk",
    }:
        data["theme"] = "gamma"
    if data.get("start_page") not in {
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
    try:
        font_size = int(data.get("font_size", 13))
    except (TypeError, ValueError):
        font_size = 13
    data["font_size"] = min(20, max(9, font_size))
    for key in ("window_width", "window_height"):
        try:
            size = int(data.get(key, _DEFAULTS[key]))
        except (TypeError, ValueError):
            size = _DEFAULTS[key]
        data[key] = min(3840, max(640, size))
    _allowed_fonts = {
        "Exo 2",
        "Noto Sans",
        "DejaVu Sans",
        "Ubuntu",
        "Liberation Sans",
        "Inter",
    }
    font_family = data.get("font_family")
    if not isinstance(font_family, str) or font_family not in _allowed_fonts:
        data["font_family"] = "Exo 2"
    for key in ("always_gamemoderun", "autostart"):
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
    if not isinstance(data.get("move_dest"), str):
        data["move_dest"] = ""
    if not isinstance(data.get("move_expected"), list):
        data["move_expected"] = []
    data["move_expected"] = [x for x in data["move_expected"] if isinstance(x, str)]
    return data


def save_gui_settings(**changes) -> None:
    path = gui_settings_path()
    data = load_gui_settings()
    data.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, json.dumps(data, indent=2) + "\n")


def configured_wine_prefix() -> str:
    """The real WINEPREFIX implied by the saved runner + prefix selection.

    The Play page stores whatever the prefix box holds, but for a Steam Proton
    runner that value is ``STEAM_COMPAT_DATA_PATH`` and the actual Wine prefix
    lives one level down in ``pfx``. Tools driven against the prefix directly
    (winetricks) must use the resolved path, not the stored one.
    """
    from .launcher import wine_prefix_for  # deferred: keeps this module leaf-ish

    state = load_gui_settings()
    return wine_prefix_for(
        state.get("runner") or "auto", state.get("wine_prefix") or ""
    )
