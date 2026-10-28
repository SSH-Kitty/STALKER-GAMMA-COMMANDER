"""The painted scene behind the Live themes (Winter, Red Forest).

Shared by the desktop window and Deck Mode.
"""

from __future__ import annotations

import functools
from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QPainter, QPixmap

from ..themes import active_theme_tokens


@functools.cache
def scene_pixmap(image: str) -> QPixmap:
    # ponytail: keeps every image seen this session decoded (a few MB at most).
    return QPixmap(str(Path(__file__).resolve().parents[1] / "assets" / image))


def scene_rect(rect, size: QSize, screen: QSize | None = None) -> QRect:
    """Where a scene of ``size`` lands: scaled to cover the screen (or the
    window, if larger) and centred, so resizing the window crops the scene
    around its centre instead of zooming it."""
    cover = rect.size() if screen is None else rect.size().expandedTo(screen)
    scaled = size.scaled(cover, Qt.AspectRatioMode.KeepAspectRatioByExpanding)
    return QRect(
        (rect.width() - scaled.width()) // 2,
        (rect.height() - scaled.height()) // 2,
        scaled.width(),
        scaled.height(),
    )


def paint_scene(painter: QPainter, rect, screen: QSize | None = None) -> bool:
    """Paint the theme's scene image, if it has one; True if painted."""
    image = active_theme_tokens().get("back_image")
    pixmap = scene_pixmap(image) if image else None
    if pixmap is None or pixmap.isNull():
        return False
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawPixmap(scene_rect(rect, pixmap.size(), screen), pixmap)
    return True
