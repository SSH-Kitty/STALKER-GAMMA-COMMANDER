"""Bundled Exo 2 font (SIL Open Font License 1.1)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFontDatabase

_FONT_LOADED = False


def load_bundled_font() -> str:
    """Load the bundled Exo 2 font and return its family name.

    The font is loaded once; subsequent calls return the cached name.
    Returns the system fallback family name if loading fails.
    """
    global _FONT_LOADED
    if _FONT_LOADED:
        return "Exo 2"
    font_path = Path(__file__).parent / "Exo2-Variable.ttf"
    if not font_path.exists():
        return "sans-serif"
    try:
        id_ = QFontDatabase.addApplicationFont(str(font_path))
    except Exception:  # noqa: BLE001
        return "sans-serif"
    if id_ == -1:
        return "sans-serif"
    families = QFontDatabase.applicationFontFamilies(id_)
    if not families:
        return "sans-serif"
    _FONT_LOADED = True
    return families[0]
