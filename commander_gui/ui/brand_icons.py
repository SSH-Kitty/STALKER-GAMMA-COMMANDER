"""Single-colour SVG icons, tinted to match the theme.

The GitHub and Discord logos, and the achievement icons.

All ship as single-colour SVGs in ``commander_gui/assets``; they are
recoloured on load to sit with the surrounding text, whatever
theme is active.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

from ..themes import active_theme_tokens

try:  # Optional: a build without the QtSvg binding still runs, icon-less.
    from PySide6.QtSvg import QSvgRenderer
except ImportError:  # pragma: no cover - depends on the packaging
    QSvgRenderer = None

from ..config import project_root

_ASSETS = project_root() / "commander_gui" / "assets"
#: Logo name -> file in commander_gui/assets.
_FILES = {"github": "github.svg", "discord": "discord.svg"}


def brand_icon(name: str, color: QColor | str, size: int = 20) -> QIcon:
    """The ``name`` logo filled with ``color``; an empty icon if missing."""
    if name not in _FILES:
        return QIcon()
    return tinted_svg_icon(_ASSETS / _FILES[name], color, size)


def achievement_icon(key: str, color: QColor | str, size: int = 44) -> QIcon:
    """An achievement's icon (``assets/achievements/<key>.svg``) in ``color``."""
    return tinted_svg_icon(_ASSETS / "achievements" / f"{key}.svg", color, size)


def achievement_color(is_unlocked: bool) -> str:
    """The theme's accent for an unlocked achievement's icon, grey if locked."""
    tokens = active_theme_tokens()
    if is_unlocked:
        return tokens.get("accent", "#9fe96f")
    return tokens.get("text_disabled", "#5b6558")


def tinted_svg_icon(path: Path, color: QColor | str, size: int) -> QIcon:
    """A single-colour SVG (drawn in ``#000000``) recoloured to ``color``.

    Empty icon if the file is missing or unreadable.
    """
    try:
        svg = path.read_text(encoding="utf-8")
    except OSError:
        return QIcon()
    return tinted_svg_text_icon(svg, color, size)


def tinted_svg_text_icon(svg: str, color: QColor | str, size: int) -> QIcon:
    """Like ``tinted_svg_icon``, from the SVG's text."""
    if QSvgRenderer is None:
        return QIcon()
    fill = QColor(color).name()
    renderer = QSvgRenderer(QByteArray(svg.replace("#000000", fill).encode("utf-8")))
    if not renderer.isValid():
        return QIcon()
    # Rendered at 2x so it stays crisp on a scaled or high-DPI display.
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter)
    finally:
        painter.end()
    pixmap.setDevicePixelRatio(2.0)
    return QIcon(pixmap)


#: COMMANDER's line icons (the file picker, right-click menus): stroke-only
#: paths on a 24x24 grid, drawn in #000000 and tinted on load.
_LINE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#000000" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{}</svg>'
)
LINE_ICONS = {
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    "documents": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
    '<path d="M14 3v5h5M9 13h6M9 17h6"/>',
    "home": '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
    "desktop": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    "downloads": '<path d="M12 3v12M7 10l5 5 5-5M4 20h16"/>',
    "drive": '<rect x="2" y="7" width="20" height="10" rx="2"/><path d="M6 12h.01M10 12h.01"/>',
    "up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
    "undo": '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "redo": '<path d="M15 14l5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/>',
    "cut": '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4L8.1 15.9M14.5 14.5L20 20M8.1 8.1L12 12"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
    "paste": '<rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>',
    "delete": '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
    "select": '<path d="M5 3H3v2M19 3h2v2M5 21H3v-2M19 21h2v-2M9 3h2M13 3h2M9 21h2M13 21h2M3 9v2M3 13v2M21 9v2M21 13v2"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
}


def line_icon(name: str, token: str = "text_dim", size: int = 18) -> QIcon:
    """The ``name`` line icon in the theme colour ``token``."""
    return _line_icon(name, active_theme_tokens().get(token, "#9aa39a"), size)


@lru_cache(maxsize=128)
def _line_icon(name: str, color: str, size: int) -> QIcon:
    # Cached: a big folder (/usr/bin) would render the SVG once per row.
    return tinted_svg_text_icon(_LINE_SVG.format(LINE_ICONS[name]), color, size)


def icon_size(size: int) -> QSize:
    return QSize(size, size)
