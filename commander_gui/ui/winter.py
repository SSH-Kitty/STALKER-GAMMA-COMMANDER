"""Winter (Live) theme extras: the painted scene, falling snow and fairy lights.

Shared by the desktop window and Deck Mode.
"""

from __future__ import annotations

import functools
import math
import random
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QWidget

from .. import gui_settings
from ..themes import active_theme_tokens


@functools.cache
def _scene(image: str) -> QPixmap:
    # ponytail: keeps every scene seen this session decoded (4 small JPEGs at most).
    return QPixmap(str(Path(__file__).resolve().parents[1] / "assets" / image))


def paint_scene(painter: QPainter, rect, screen: QSize | None = None) -> bool:
    """Paint the theme's scene image, if it has one; True if painted.

    Scaled to cover the screen (or the window, if larger), so resizing the
    window crops the scene around its centre instead of zooming it.
    """
    image = active_theme_tokens().get("back_image")
    pixmap = _scene(image) if image else None
    if pixmap is None or pixmap.isNull():
        return False
    cover = rect.size() if screen is None else rect.size().expandedTo(screen)
    scaled = pixmap.size().scaled(cover, Qt.AspectRatioMode.KeepAspectRatioByExpanding)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawPixmap(
        QRect(
            (rect.width() - scaled.width()) // 2,
            (rect.height() - scaled.height()) // 2,
            scaled.width(),
            scaled.height(),
        ),
        pixmap,
    )
    return True


class Snowfall:
    """Winter's falling flakes over ``host``, which calls paint() from its
    paintEvent and sync() whenever it may have become visible or hidden.

    Every tick repaints the small area under each flake (and the bits of
    cards there), so it stops in the background, minimized, while a game
    has focus, in other themes and when switched off in Settings.
    """

    FLAKES = 70
    INTERVAL_MS = 33  # 30 fps
    STEP = 0.67  # movement per frame, scaled so 30 fps falls as fast as 20 did

    def __init__(self, host: QWidget) -> None:
        self._host = host
        #: x, y, radius, fall speed (px/tick), sway phase
        self.flakes: list[list[float]] = []
        #: Twinkled off this timer; set by the owner whenever it builds them.
        self.lights: ChristmasLights | None = None
        self._ticks = 0
        self._timer = QTimer(host)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    def sync(self, visible: bool) -> None:
        tokens = active_theme_tokens()
        enabled = bool(tokens.get("back_snow")) and gui_settings.load_gui_settings().get(
            "snow_enabled", True
        )
        wanted = enabled and visible
        if wanted and not self._timer.isActive():
            self._timer.start()
        elif not wanted and self._timer.isActive():
            self._timer.stop()
        if self.lights is not None:
            self.lights.setVisible(bool(tokens.get("back_lights")))
        # Paused flakes stay put; switched-off ones (or another theme's) go.
        if not enabled and self.flakes:
            self.flakes.clear()
            self._host.update()

    def paint(self, painter: QPainter) -> None:
        if not self.flakes:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # A faint grey rim keeps white flakes visible over the snowfield.
        painter.setPen(QPen(QColor(110, 125, 140, 70), 0.6))
        for x, y, r, _speed, _phase in self.flakes:
            painter.setBrush(QColor(255, 255, 255, int(110 + r * 45)))
            painter.drawEllipse(QPointF(x, y), r, r)

    def _tick(self) -> None:
        self._ticks += 1
        if self._ticks % 15 == 0 and self.lights is not None:
            self.lights.twinkle()
        host = self._host
        w, h = host.width(), host.height()
        if not self.flakes:
            rnd = random.random
            self.flakes = [
                [rnd() * w, rnd() * h, r, 0.6 + r * 0.45, rnd() * 6.3]
                for r in (random.choice((1.0, 1.0, 1.5, 2.0, 2.5, 3.0)) for _ in range(self.FLAKES))
            ]
        for flake in self.flakes:
            x, y, r, speed, phase = flake
            old = QRect(int(x - r) - 1, int(y - r) - 1, int(2 * r) + 3, int(2 * r) + 3)
            phase += 0.05 * self.STEP
            x = (x + (0.4 * math.sin(phase) - 0.35) * self.STEP) % w
            y += speed * self.STEP
            if y - r > h:
                y, x = -r, random.random() * w
            flake[0], flake[1], flake[4] = x, y, phase
            host.update(old.united(QRect(int(x - r) - 1, int(y - r) - 1, int(2 * r) + 3, int(2 * r) + 3)))


class ChristmasLights(QWidget):
    """A string of fairy lights along the bottom edge of the top bar (Winter).

    Floats over the page just below the bar, ignoring the mouse. Twinkles
    off the snow's timer (see Snowfall), so it costs nothing of
    its own and stays lit but still whenever the snow is paused.
    """

    _HEIGHT = 18  # from the bar's bottom edge down: room for the sag and bulbs
    _SWAG = 64  # width of one dip between two hooks
    _COLORS = ("#e8443a", "#f2b632", "#4fae4a", "#fff1c9", "#f07a2c")

    def __init__(self, bar: QWidget, parent: QWidget) -> None:
        super().__init__(parent)
        self._bar = bar
        self._phase = 0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # Rebuilt on a language change, which doesn't run Snowfall.sync().
        self.setHidden(not active_theme_tokens().get("back_lights"))
        bar.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if watched is self._bar and event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
            bar = self._bar.geometry()
            self.setGeometry(bar.left(), bar.bottom() - 2, bar.width(), self._HEIGHT)
            self.raise_()
        return super().eventFilter(watched, event)

    def twinkle(self) -> None:
        self._phase += 1
        self.update()

    def _wire(self) -> QPainterPath:
        """Hooked to the bar's edge every _SWAG px, sagging between hooks."""
        swags = max(1, round(self.width() / self._SWAG))
        step = self.width() / swags
        path = QPainterPath(QPointF(0, 2))
        for i in range(swags):
            path.quadTo(QPointF((i + 0.5) * step, 14), QPointF((i + 1) * step, 2))
        return path

    def paintEvent(self, _event) -> None:
        # Only three looks (the chase repeats every third bulb), and snow
        # falling past repaints this many times a second, so each look is
        # drawn once per size and blitted from then on.
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr, self._phase % 3)
        cache = self.__dict__.setdefault("_frames", {})
        if key[:3] != getattr(self, "_frames_size", None):
            cache.clear()
            self._frames_size = key[:3]
        frame = cache.get(key)
        if frame is None:
            frame = QPixmap(round(self.width() * dpr), round(self.height() * dpr))
            frame.setDevicePixelRatio(dpr)
            frame.fill(Qt.GlobalColor.transparent)
            self._paint_frame(QPainter(frame))
            cache[key] = frame
        painter = QPainter(self)
        painter.drawPixmap(0, 0, frame)
        painter.end()

    def _paint_frame(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = self._wire()
        painter.setPen(QPen(QColor(40, 62, 38, 200), 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        count = max(8, int(path.length() / 22))
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(count):
            # Bulbs hang just under the wire.
            point = path.pointAtPercent((i + 0.5) / count) + QPointF(0, 2.5)
            color = QColor(self._COLORS[i % len(self._COLORS)])
            # Every third bulb in turn rests, so the lights slowly chase.
            lit = (i + self._phase) % 3 != 0
            if lit:
                glow = QRadialGradient(point, 6)
                halo = QColor(color)
                halo.setAlpha(110)
                glow.setColorAt(0.0, halo)
                glow.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
                painter.setBrush(glow)
                painter.drawEllipse(point, 6, 6)
            else:
                color = color.darker(190)
            painter.setBrush(color)
            painter.drawEllipse(point, 2.0, 2.6)
        painter.end()
