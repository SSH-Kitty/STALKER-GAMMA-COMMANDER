"""Persistence for GUI-only preferences.

These are separate from the CLI's settings.json (which the CLI owns) and only
cover GUI behaviour, currently the game-launcher runner selection.
"""

from __future__ import annotations

import copy
import json
import shutil

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
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Try the last-known-good backup if the main file is corrupt.
            backup = path.with_suffix(".json.last-good")
            if backup.exists():
                try:
                    stored = json.loads(backup.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
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
    data["font_size"] = min(22, max(9, font_size))
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
    try:
        dpi = int(data.get("mo2_display_dpi", 120))
    except (TypeError, ValueError):
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
    # Preserve a last-known-good backup before overwriting.
    if path.exists():
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
    from .launcher import wine_prefix_for  # deferred: keeps this module leaf-ish

    state = load_gui_settings()
    return wine_prefix_for(
        state.get("runner") or "auto", state.get("wine_prefix") or ""
    )
