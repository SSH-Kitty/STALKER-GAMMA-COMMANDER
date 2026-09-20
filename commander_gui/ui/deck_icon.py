"""A Steam Deck silhouette drawn with QPainterPath.

Deliberately hand-painted rather than loaded from an SVG file.
``build-appimage.sh`` prunes every PySide6 binding except QtCore, QtGui,
QtWidgets, QtMultimedia and QtNetwork, so ``PySide6.QtSvg`` does not exist
in a shipped AppImage - a QSvgRenderer-based icon would work in a source
checkout and fail only for released builds. QPainterPath needs nothing that
is not already bundled, and painting the outline ourselves also means the
icon takes whatever colour the active theme asks for.

This is the first image asset in the GUI. Every other "icon" in COMMANDER is
a Unicode glyph styled through QSS (the settings cog at
``ui/main_window.py``, the status dots in ``ui/common.py``); there is no
Steam Deck glyph in any font we can rely on, hence this module.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTransform,
)

#: The path is authored in a 24x24 unit box and scaled by the caller, so one
#: definition serves every icon size without re-tuning proportions.
_UNITS = 24.0


def deck_path(size: float = _UNITS) -> QPainterPath:
    """A Steam Deck outline: body, grips, screen, thumbsticks.

    Proportions follow the real device - a wide centre screen with a grip
    lobe bulging below each end - abstracted far enough to stay readable at
    22px, which is the size the Dashboard button uses.
    """
    scale = size / _UNITS
    path = QPainterPath()
    # Body: the full width of the shell, with the screen bezel's flat top.
    path.addRoundedRect(QRectF(1.0, 5.5, 22.0, 13.0), 3.6, 3.6)
    # Grip lobes, one under each hand.
    path.addEllipse(QRectF(0.6, 9.0, 6.4, 9.4))
    path.addEllipse(QRectF(17.0, 9.0, 6.4, 9.4))
    # Screen.
    path.addRoundedRect(QRectF(7.4, 8.2, 9.2, 7.6), 1.1, 1.1)
    # Thumbsticks.
    path.addEllipse(QRectF(2.9, 11.0, 3.4, 3.4))
    path.addEllipse(QRectF(17.7, 11.0, 3.4, 3.4))
    if scale == 1.0:
        return path
    # QPainterPath cannot be scaled in place, so map it through a transform
    # rather than recomputing every rectangle for each icon size.
    return QTransform().scale(scale, scale).map(path)


def deck_icon(color: QColor, size: int = 24, *, filled: bool = False) -> QIcon:
    """Render :func:`deck_path` into a transparent QIcon in ``color``.

    ``filled`` swaps the outline for a solid silhouette, for cases where a
    stroke would disappear (very small sizes, or a busy background).
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = deck_path(float(size))
        if filled:
            painter.fillPath(path, color)
        else:
            # Stroke width scales with the icon so the outline keeps the same
            # visual weight from 16px up to 48px.
            width = max(1.0, 1.6 * size / _UNITS)
            pen = QPen(
                color,
                width,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            painter.setPen(pen)
            painter.drawPath(path)
    finally:
        painter.end()
    return QIcon(pixmap)
