"""Hand cursor over everything clickable in the desktop window.

One application-wide event filter, like :mod:`smooth_scroll`: buttons,
checkboxes, radio buttons, tab bars and combo boxes get the pointing hand
while enabled, and the normal arrow while disabled, so a greyed-out control
never looks clickable. Widgets that set their own non-hand cursor are left
alone.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QTabBar, QWidget

_CLICKABLE = (QAbstractButton, QTabBar, QComboBox)
# Show as well as Polish: a button polished while being built (before it sits
# in a window) is only caught once it is shown.
_EVENTS = (QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.EnabledChange)


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


def install(window: QWidget) -> PointerCursorFilter | None:
    app = QApplication.instance()
    if app is None:
        return None
    pointer = PointerCursorFilter(window)
    app.installEventFilter(pointer)
    return pointer
