"""Single-colour SVG icons, tinted to match the theme.

The GitHub and Discord logos, and the achievement icons.

All ship as single-colour SVGs in ``commander_gui/assets``; they are
recoloured on load to sit with the surrounding text, whatever
theme is active.
"""

from __future__ import annotations

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


def icon_size(size: int) -> QSize:
    return QSize(size, size)
