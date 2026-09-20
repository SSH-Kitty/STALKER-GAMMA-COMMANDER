"""The Deck screens and the order they appear in the bottom nav bar.

``SCREENS`` is the single source of truth for that order. Titles are stored
as English source text and passed through ``tr()`` at display time, matching
how ``commander_gui.ui.main_window`` handles ``NAV_ITEMS`` - the strings have
to be re-translated when the language changes, and a module-level constant
is evaluated once at import.

Dashboard leads, as it does in the desktop window: it is the overview, and
the hub its rows send you off from. The rest follow how often a Deck user
reaches for them, Settings last because it is mostly visited once. Leading
the bar is not the same as being the landing screen - ``deck_start_screen``
still defaults to Play, because picking the device up usually means wanting
to play.

Glyphs are deliberately plain Unicode available in the bundled Exo 2 and in
DejaVu Sans, its fallback - the app ships no icon set, and a missing glyph
renders as a replacement box on someone else's machine.

Keeping this list in step with the ``deck_start_screen`` validator in
``commander_gui/gui_settings.py`` is checked by tests/test_steamdeck.py.
"""

from __future__ import annotations

#: (key, English title, nav glyph)
SCREENS: list[tuple[str, str, str]] = [
    ("dashboard", "Dashboard", "⌂"),
    ("play", "Play", "▶"),
    ("install", "Install", "↓"),
    ("update", "Update", "↻"),
    ("mods", "Mods", "☰"),
    ("profile", "Profile", "●"),
    ("system", "System", "✓"),
    ("utilities", "Utilities", "⚒"),
    ("settings", "Settings", "⚙"),
]

SCREEN_KEYS = [key for key, _title, _glyph in SCREENS]
