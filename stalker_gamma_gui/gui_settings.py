"""Persistence for GUI-only preferences.

These are separate from the CLI's settings.json (which the CLI owns) and only
cover GUI behaviour, currently the game-launcher runner selection.
"""

from __future__ import annotations

import json

from .config import gui_settings_path

_DEFAULTS = {
    "runner": "auto",  # "auto" | "umu" | "wine" | "proton:<path-to-proton>"
    "wine_prefix": "",  # WINEPREFIX (Wine) or STEAM_COMPAT_DATA_PATH (Proton)
    "prefixes": {},  # per-runner prefix, keyed by the runner data value
    "target": "",  # last selected launch target title
    "theme": "gamma",  # key into themes.THEMES
    "start_page": "dashboard",  # nav page shown on launch (key into main_window.NAV_ITEMS)
    "font_size": 13,  # base UI font size in px; scales every QSS font
    "always_gamemoderun": False,  # wrap every launch command in gamemoderun
    "autostart": False,  # add to XDG autostart so the app starts at login
    "custom_launch_options": "",  # extra tokens prepended to the launch command
}


def load_gui_settings() -> dict:
    path = gui_settings_path()
    data = dict(_DEFAULTS)
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return data
        if isinstance(stored, dict):
            data.update(stored)
    return data


def save_gui_settings(**changes) -> None:
    path = gui_settings_path()
    data = load_gui_settings()
    data.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


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
