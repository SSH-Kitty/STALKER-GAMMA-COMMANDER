"""A Steam Deck silhouette drawn with QPainterPath.

Deliberately hand-painted rather than loaded from an SVG file.
``build-appimage.sh`` prunes every PySide6 binding except QtCore, QtGui,
QtWidgets, QtMultimedia and QtNetwork, so ``PySide6.QtSvg`` does not exist
in a shipped AppImage - a QSvgRenderer-based icon would work in a source
checkout and fail only for released builds. QPainterPath needs nothing that
is not already bundled, and painting the shape ourselves also means the
icon takes whatever colour the active theme asks for.

This is the first image asset in the GUI. Every other "icon" in COMMANDER is
a Unicode glyph styled through QSS (the settings cog at
``ui/main_window.py``, the status dots in ``ui/common.py``); there is no
Steam Deck glyph in any font we can rely on, hence this module.

The glyph is a solid silhouette with the screen, thumbsticks and trackpads
cut out of it, not an outline: at the 22-28px the Dashboard uses, a stroked
outline of several overlapping shapes turned into a tangle of lines, while
a filled shape with holes still reads as "handheld" at a glance. The screen
is tinted at low opacity so it looks like a display rather than a hole.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap

#: Shapes are authored in a 24x24 unit box and scaled to the target size
#: *before* the boolean operations run. Qt flattens curves to polygons when
#: it unites/subtracts paths, so doing that at 24 units and scaling up
#: afterwards left visibly faceted corners.
_UNITS = 24.0

#: How strongly the screen is tinted inside its cut-out.
_SCREEN_ALPHA = 0.35


def _rect(scale: float, x: float, y: float, w: float, h: float) -> QRectF:
    return QRectF(x * scale, y * scale, w * scale, h * scale)


def deck_paths(size: float = _UNITS) -> tuple[QPainterPath, QPainterPath]:
    """``(body, screen)`` for a Steam Deck ``size`` pixels wide.

    ``body`` is the shell with every cut-out already removed; ``screen`` is
    the display that sits inside the largest cut-out.
    """
    s = size / _UNITS
    body = QPainterPath()
    # The shell: one wide rounded bar...
    body.addRoundedRect(_rect(s, 0.8, 6.2, 22.4, 10.6), 5.3 * s, 5.3 * s)
    # ...with a grip dropping below each end, merged into one outline.
    for x in (0.8, 16.4):
        grip = QPainterPath()
        grip.addRoundedRect(_rect(s, x, 8.5, 6.8, 10.3), 3.4 * s, 3.4 * s)
        body = body.united(grip)

    holes = QPainterPath()
    # The bezel gap around the screen.
    holes.addRoundedRect(_rect(s, 6.2, 7.4, 11.6, 8.2), 1.4 * s, 1.4 * s)
    # Thumbsticks, high on each side like the real device...
    for cx in (4.1, 19.9):
        holes.addEllipse(_rect(s, cx - 1.75, 9.3, 3.5, 3.5))
    # ...and the square trackpads under them.
    for x in (2.6, 18.4):
        holes.addRoundedRect(_rect(s, x, 14.0, 3.0, 2.6), 0.6 * s, 0.6 * s)

    screen = QPainterPath()
    screen.addRoundedRect(_rect(s, 7.0, 8.2, 10.0, 6.6), 0.9 * s, 0.9 * s)
    return body.subtracted(holes), screen


def _render(color: QColor, size: int, ratio: float) -> QPixmap:
    pixels = max(1, round(size * ratio))
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        body, screen = deck_paths(float(pixels))
        painter.fillPath(body, color)
        tint = QColor(color)
        tint.setAlphaF(_SCREEN_ALPHA * color.alphaF())
        painter.fillPath(screen, tint)
    finally:
        painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def deck_icon(color: QColor, size: int = 24) -> QIcon:
    """The Steam Deck glyph in ``color`` as a QIcon.

    Rendered at 1x and 2x so it stays sharp on a scaled (HiDPI) desktop.
    """
    icon = QIcon()
    for ratio in (1.0, 2.0):
        icon.addPixmap(_render(color, size, ratio))
    return icon
