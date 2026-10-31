"""Live theme extras: swaying trees (Red Forest and Winter), and Red Forest's
flickering campfire and falling leaves, over the painted scene (see
live_scene.py).

Shared by the desktop window and Deck Mode. The scenes come from
build-redforest-backdrop.py and build-winter-backdrop.py: the backdrop image
without the edge trees (or the fire's flames), plus one see-through layer per
edge (``<back_trees>_left.png`` pinned to the image's left edge,
``<back_trees>_right.png`` to its right), which this module draws gently bent
back and forth, once every ``back_sway_s`` seconds.
"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QWidget

from .. import gui_settings
from ..themes import active_theme_tokens
from .live_scene import scene_pixmap, scene_rect

#: The campfire's base, as a fraction of the scene image, and the size of one
#: unit of its flames in image pixels (printed by build-redforest-backdrop.py).
FIRE_AT = (0.5594, 0.8591)
FIRE_UNIT = 1.612
#: Flame layers, back to front: colour, half-width, height (flame units).
FLAMES = (("#d9481c", 15, 38), ("#f08a2a", 10, 28), ("#ffd46a", 5, 16))
#: The scene's blur, in image pixels (build-redforest-backdrop.py's BLUR).
BLUR = 2
LEAF_COLORS = ("#c2501f", "#d97a2b", "#a8341c", "#e0a33a")


def _leaf_path() -> QPainterPath:
    """A leaf two units long, pointed at both ends."""
    path = QPainterPath(QPointF(-1, 0))
    path.quadTo(QPointF(0, -0.55), QPointF(1, 0))
    path.quadTo(QPointF(0, 0.55), QPointF(-1, 0))
    return path


class RedForest:
    """The Red Forest's moving parts over ``host``, which calls paint() from
    its paintEvent and sync() whenever it may have become visible or hidden.

    The edge trees and the fire are part of the scene and always painted; only
    the movement stops - in the background, minimized, while a game has
    focus, in other themes and when switched off in Settings. Leaves repaint
    the small area under each one every tick, the trees and fire every second
    tick: their slow movement needs no more.
    """

    LEAVES = 40
    INTERVAL_MS = 33  # 30 fps for the leaves, 15 for trees and fire
    SWAY = 0.008  # shear at the crown: ~10 px at the top of a 1200 px tree
    SWAY_PERIOD_S = 5.0

    def __init__(self, host: QWidget) -> None:
        self._host = host
        #: x, y, half-length, fall speed (px/tick), drift phase, angle, spin, colour index
        self.leaves: list[list[float]] = []
        self._ticks = 0
        self._sway = 0.0
        self._layers: dict[tuple, QPixmap] = {}
        #: Areas the trees and fire covered at the last paint: repainted as they move.
        self._moving: list[QRect] = []
        self._leaf = _leaf_path()
        self._timer = QTimer(host)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    def sync(self, visible: bool) -> None:
        tokens = active_theme_tokens()
        enabled = bool(tokens.get("back_trees")) and gui_settings.load_gui_settings().get(
            "live_animation", True
        )
        wanted = enabled and visible
        if wanted and not self._timer.isActive():
            self._timer.start()
        elif not wanted and self._timer.isActive():
            self._timer.stop()
        # Paused leaves stay put; switched-off ones (or another theme's) go.
        if not (enabled and tokens.get("back_redforest")) and self.leaves:
            self.leaves.clear()
            self._host.update()

    # ----- painting -----
    def paint(self, painter: QPainter) -> None:
        """The trees, the fire and the leaves, over the scene image."""
        tokens = active_theme_tokens()
        prefix = tokens.get("back_trees")
        image = scene_pixmap(tokens.get("back_image", "")) if prefix else None
        if image is None or image.isNull():
            return
        host = self._host
        target = scene_rect(host.rect(), image.size(), host.screen().size() if host.isWindow() else None)
        k = target.width() / image.width()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        moving = []
        for i, side in enumerate(("left", "right")):
            layer = self._layer(f"{prefix}_{side}.png", k)
            if layer.isNull():
                continue
            w = layer.width() / layer.devicePixelRatio()
            h = layer.height() / layer.devicePixelRatio()
            x = target.left() if side == "left" else target.left() + target.width() - w
            # Bent from the roots: the shear moves the crowns, not the trunks' feet.
            shear = self.SWAY * math.sin(self._sway + i * 1.9)
            painter.save()
            painter.translate(x, target.top() + h)
            painter.shear(shear, 0)
            painter.drawPixmap(QPointF(0, -h), layer)
            painter.restore()
            reach = math.ceil(self.SWAY * h) + 2
            moving.append(QRect(int(x) - reach, target.top(), int(w) + 2 * reach, int(h) + 1))
        # Lit at dusk and night only; at dawn just the baked-in smoke is left.
        if tokens.get("back_fire"):
            moving.append(self._paint_fire(painter, target, k))
        self._moving = moving
        self.paint_leaves(painter)

    def _layer(self, name: str, k: float) -> QPixmap:
        """A tree layer scaled once per size, in device pixels."""
        dpr = self._host.devicePixelRatioF()
        key = (name, round(k, 4), dpr)
        layer = self._layers.get(key)
        if layer is None:
            source = scene_pixmap(name)
            if source.isNull():
                return source
            layer = source.scaled(
                round(source.width() * k * dpr),
                round(source.height() * k * dpr),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            layer.setDevicePixelRatio(dpr)
            # Only this phase's two layers at this size are kept (~8 MB).
            phase = name.rsplit("_", 1)[0]
            self._layers = {
                n: p for n, p in self._layers.items()
                if n[1:] == key[1:] and n[0].rsplit("_", 1)[0] == phase
            }
            self._layers[key] = layer
        return layer

    def _paint_fire(self, painter: QPainter, target: QRect, k: float) -> QRect:
        unit = FIRE_UNIT * k
        x = target.left() + FIRE_AT[0] * target.width()
        y = target.top() + FIRE_AT[1] * target.height()
        box = QRectF(x - 22 * unit, y - 52 * unit, 44 * unit, 58 * unit)
        # Drawn sharp into a small image, then shrunk and stretched back: the
        # same soft edge as the scene's baked-in blur (BLUR px at 1920 wide).
        dpr = self._host.devicePixelRatioF()
        sharp = QImage(max(1, round(box.width() * dpr)), max(1, round(box.height() * dpr)),
                       QImage.Format.Format_ARGB32_Premultiplied)
        sharp.fill(Qt.GlobalColor.transparent)
        flames = QPainter(sharp)
        flames.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        flames.setPen(Qt.PenStyle.NoPen)
        flames.scale(unit * dpr, unit * dpr)
        flames.translate(22, 52)
        t = self._ticks * 0.15
        for n, (color, half, height) in enumerate(FLAMES):
            # Two sines out of step: a flicker that never quite repeats.
            flick = 1 + 0.14 * math.sin(t * 2.3 + n * 1.7) + 0.08 * math.sin(t * 5.1 + n)
            lean = 2 + 3 * math.sin(t * 1.3 + n * 0.9)
            h, w = height * flick, half * (1 + 0.05 * math.sin(t * 3.7 + n))
            path = QPainterPath(QPointF(-w, -4))
            path.quadTo(QPointF(-w * 0.6, -h * 0.6), QPointF(lean, -h))
            path.quadTo(QPointF(w * 0.3, -h * 0.5), QPointF(w, -4))
            path.closeSubpath()
            flames.setBrush(QColor(color))
            flames.drawPath(path)
        flames.end()
        shrink = max(1.0, 2 * BLUR * k * dpr)  # a CSS blur(3px) softens ~2x its radius
        soft = sharp.scaled(
            max(1, round(sharp.width() / shrink)), max(1, round(sharp.height() / shrink)),
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
        )
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setOpacity(0.92)
        painter.drawImage(box, soft)
        painter.restore()
        return box.toAlignedRect().adjusted(-1, -1, 1, 1)

    def paint_leaves(self, painter: QPainter) -> None:
        if not self.leaves:
            return
        alpha = int(active_theme_tokens().get("back_leaf_alpha", "220"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        for x, y, size, _speed, _phase, angle, _spin, color in self.leaves:
            leaf = QColor(LEAF_COLORS[int(color)])
            leaf.setAlpha(alpha)
            painter.save()
            painter.translate(x, y)
            painter.rotate(angle)
            # Tumbling: the leaf turns edge-on and back as it falls.
            painter.scale(size, size * max(0.25, abs(math.cos(math.radians(angle * 1.7)))))
            painter.setBrush(leaf)
            painter.drawPath(self._leaf)
            painter.restore()

    # ----- movement -----
    def _tick(self) -> None:
        self._ticks += 1
        host = self._host
        w, h = host.width(), host.height()
        tokens = active_theme_tokens()
        if not self.leaves and tokens.get("back_redforest"):
            rnd = random.random
            self.leaves = [
                [rnd() * w, rnd() * h, size, 0.5 + size * 0.09, rnd() * 6.3, rnd() * 360,
                 random.choice((-3.0, -2.0, 2.0, 3.0)), random.randrange(len(LEAF_COLORS))]
                for size in (random.uniform(6.0, 10.0) for _ in range(self.LEAVES))
            ]
        for leaf in self.leaves:
            x, y, size, speed, phase, angle, spin, _color = leaf
            reach = int(size) + 2
            old = QRect(int(x) - reach, int(y) - reach, 2 * reach, 2 * reach)
            phase += 0.04
            # Drifting downwind and swinging side to side, unlike snow's straight fall.
            x = (x + 1.1 * math.sin(phase) - 0.45) % w
            y += speed
            if y - size > h:
                y, x = -size, random.random() * w
            leaf[0], leaf[1], leaf[4], leaf[5] = x, y, phase, (angle + spin) % 360
            host.update(old.united(QRect(int(x) - reach, int(y) - reach, 2 * reach, 2 * reach)))
        if self._ticks % 2 == 0:
            period = float(tokens.get("back_sway_s", self.SWAY_PERIOD_S))
            self._sway += 2 * math.pi * 2 * self.INTERVAL_MS / 1000 / period
            for rect in self._moving:
                host.update(rect)
