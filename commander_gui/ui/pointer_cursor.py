"""Hand cursor over everything clickable in the desktop window.

One application-wide event filter, like :mod:`smooth_scroll`: buttons,
checkboxes, radio buttons, tab bars and combo boxes get the pointing hand
while enabled, and the normal arrow while disabled, so a greyed-out control
never looks clickable. Widgets that set their own non-hand cursor are left
alone.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QMenu,
    QTabBar,
    QWidget,
)

from .context_menu import polish_menu, show_text_menu

_CLICKABLE = (QAbstractButton, QTabBar, QComboBox)
# Show as well as Polish: a button polished while being built (before it sits
# in a window) is only caught once it is shown.
_EVENTS = (QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.EnabledChange)
_WHEEL = QEvent.Type.Wheel
_CONTEXT_MENU = QEvent.Type.ContextMenu


class PointerCursorFilter(QObject):
    def __init__(self, window: QWidget) -> None:
        super().__init__(window)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in _EVENTS and isinstance(obj, _CLICKABLE):
            self._apply(obj)
        return False

    @staticmethod
    def _apply(widget: QWidget) -> None:
        custom = widget.testAttribute(Qt.WidgetAttribute.WA_SetCursor)
        if custom and widget.cursor().shape() != Qt.CursorShape.PointingHandCursor:
            return
        if widget.isEnabled():
            widget.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            widget.unsetCursor()


class AppEventFilter(QObject):
    """Hand cursors, smooth wheel scrolling (:mod:`smooth_scroll`) and
    themed right-click menus (:mod:`context_menu`) in one
    application-wide filter. Every Qt event in the app - each paint, layout
    and resize - passes through every Python filter, so two separate ones
    cost twice the trips into Python for what is almost always nothing."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        from .smooth_scroll import SmoothWheelScroller

        self._scroller = SmoothWheelScroller(window)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == _WHEEL:
            return self._scroller.wheel(obj, event)
        if kind in _EVENTS and isinstance(obj, _CLICKABLE):
            PointerCursorFilter._apply(obj)
        elif kind == QEvent.Type.Polish and isinstance(obj, QMenu):
            polish_menu(obj)
        elif kind == _CONTEXT_MENU:
            return show_text_menu(obj, event)
        return False


def install_app_filter(window: QWidget) -> AppEventFilter | None:
    app = QApplication.instance()
    if app is None:
        return None
    events = AppEventFilter(window)
    app.installEventFilter(events)
    return events


def install(window: QWidget) -> PointerCursorFilter | None:
    app = QApplication.instance()
    if app is None:
        return None
    pointer = PointerCursorFilter(window)
    app.installEventFilter(pointer)
    return pointer
