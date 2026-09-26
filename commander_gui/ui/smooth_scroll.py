"""Smooth mouse-wheel scrolling for every scrollable view in the desktop UI.

Qt scrolls a fixed number of lines per wheel notch, instantly, which reads as
the page jumping. :class:`SmoothWheelScroller` is one application-wide event
filter that catches wheel events aimed at any scroll area's viewport inside
the main window - pages, lists, tables, text boxes - and turns each notch
into a short ease-out glide of the same distance instead. Fast spins add up:
a new notch continues from where the running glide is heading.

Left alone on purpose:

* touchpads and other high-resolution devices (``pixelDelta``) - they
  already scroll smoothly, and animating them adds lag;
* Ctrl+wheel (zoom) and horizontal / Shift+wheel scrolling;
* a view already at its end - the event goes on to Qt, which passes it to
  the scroll area around it, as usual;
* Deck Mode, whose scroll areas glide by themselves
  (``Steamdeck.widgets.DeckSmoothScrollArea``).
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QObject, Qt, QVariantAnimation
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QScrollBar,
    QWidget,
)

#: Length of one glide.
GLIDE_MS = 200
_ANIMATION_NAME = "commanderSmoothWheel"


class SmoothWheelScroller(QObject):
    """Install with ``QApplication.installEventFilter``; scoped to ``window``."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._window = window

    # -- filter -----------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.Type.Wheel or not isinstance(obj, QWidget):
            return False
        area = obj.parentWidget()
        if not isinstance(area, QAbstractScrollArea) or area.viewport() is not obj:
            return False
        if obj.window() is not self._window or hasattr(area, "smooth_to"):
            return False
        if not event.pixelDelta().isNull():
            return False
        modifiers = event.modifiers()
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return False
        delta = event.angleDelta().y()
        if not delta or event.angleDelta().x():
            return False
        return self._glide(area, delta)

    # -- scrolling --------------------------------------------------------
    def _glide(self, area: QAbstractScrollArea, delta: int) -> bool:
        bar = area.verticalScrollBar()
        if bar is None or bar.maximum() <= bar.minimum():
            return False
        if isinstance(area, QAbstractItemView):
            # Item-at-a-time lists can't glide; move them by pixels.
            area.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        animation = self._animation(bar)
        running = animation.state() == QVariantAnimation.State.Running
        base = int(animation.endValue()) if running else bar.value()
        # The distance Qt itself would scroll for this wheel movement.
        lines = QApplication.wheelScrollLines() or 3
        step = max(1, bar.singleStep()) * lines
        target = max(bar.minimum(), min(bar.maximum(), base - round(delta / 120 * step)))
        if target == base and not running:
            # Already at the end: let Qt hand the event to the outer view.
            return False
        animation.stop()
        animation.setStartValue(bar.value())
        animation.setEndValue(target)
        animation.start()
        return True

    @staticmethod
    def _animation(bar: QScrollBar) -> QVariantAnimation:
        animation = bar.findChild(QVariantAnimation, _ANIMATION_NAME)
        if animation is None:
            animation = QVariantAnimation(bar)
            animation.setObjectName(_ANIMATION_NAME)
            animation.setDuration(GLIDE_MS)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            animation.valueChanged.connect(lambda value, b=bar: b.setValue(int(value)))
        return animation


def install(window: QWidget) -> SmoothWheelScroller | None:
    """Make every scroll area inside ``window`` glide on the mouse wheel."""
    app = QApplication.instance()
    if app is None:
        return None
    scroller = SmoothWheelScroller(window)
    app.installEventFilter(scroller)
    return scroller
