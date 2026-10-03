"""COMMANDER's own title bar: window buttons, dragging and edge resizing.

Shared by the desktop window and Deck Mode's windowed mode. A window using
these provides ``custom_title_bar`` (bool) and ``toggle_maximized()``.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QWidget

from ..themes import active_theme_tokens

#: Window-button size (px), also the height of the strip holding them.
BUTTON_WIDTH = 36
BUTTON_HEIGHT = 18

#: How close to the window edge (px) the pointer resizes a frameless window.
_RESIZE_MARGIN = 6
#: The same along the top edge, which borders the drag strip.
_TOP_RESIZE_MARGIN = 2
#: Pointer travel (px) that turns a press on the title bar into a move.
_DRAG_START_DISTANCE = 3


class WindowDragFilter(QObject):
    """Moves (drag) and maximizes (double-click) the frameless window from
    the empty parts of its title strip and top bar."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self._press_pos = None

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if not self._window.custom_title_bar:
            return False
        kind = event.type()
        if kind == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = None
            self._window.toggle_maximized()
            return True
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            # The move starts on the first drag motion, not on the press:
            # once the compositor takes the pointer for a move, the second
            # click of a double-click never reaches the window.
            self._press_pos = event.globalPosition().toPoint()
            return True
        if (
            kind == QEvent.Type.MouseMove
            and self._press_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            moved = event.globalPosition().toPoint() - self._press_pos
            if moved.manhattanLength() >= _DRAG_START_DISTANCE:
                self._press_pos = None
                handle = self._window.windowHandle()
                if handle is not None:
                    handle.startSystemMove()
            return True
        if kind == QEvent.Type.MouseButtonRelease:
            self._press_pos = None
        return False


class WindowButton(QPushButton):
    """Minimize/maximize/restore/close button with a painted icon.

    Drawn as lines instead of text glyphs: font glyphs for these symbols
    come in different sizes, so the three buttons never looked matched.
    """

    ICON_SIZE = 8

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        tokens = active_theme_tokens()
        if self.underMouse():
            color = QColor("#ffffff" if self.kind == "close" else tokens.get("text_bright", "#ffffff"))
        else:
            color = QColor(tokens.get("text_disabled", "#888888"))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, self.kind == "close")
        pen = QPen(color)
        pen.setWidthF(1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        size = self.ICON_SIZE
        left = (self.width() - size) // 2
        top = (self.height() - size) // 2
        if self.kind == "min":
            y = top + size // 2
            painter.drawLine(left, y, left + size, y)
        elif self.kind == "max":
            painter.drawRect(left, top, size, size)
        elif self.kind == "restore":
            inner = size - 2
            painter.drawRect(left, top + 2, inner, inner)
            painter.drawLine(left + 2, top, left + size, top)
            painter.drawLine(left + size, top, left + size, top + inner)
        else:
            painter.drawLine(left, top, left + size, top + size)
            painter.drawLine(left + size, top, left, top + size)
        painter.end()


class EdgeResizeFilter(QObject):
    """Resizes the frameless window from its edges and corners.

    Installed on the window's QWindow, which sees every pointer move even
    over child widgets; the compositor does the actual resize
    (startSystemResize), so it works the same on Wayland and X11.
    """

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self._cursor_set = False

    def _edges(self, pos) -> Qt.Edge:
        w = self._window
        edges = Qt.Edge(0)
        if not w.custom_title_bar or w.isMaximized() or w.isFullScreen():
            return edges
        x, y = pos.x(), pos.y()
        if x <= _RESIZE_MARGIN:
            edges |= Qt.Edge.LeftEdge
        elif x >= w.width() - _RESIZE_MARGIN:
            edges |= Qt.Edge.RightEdge
        # The top edge sits on the thin drag strip, so it only takes the
        # outermost pixels there (full margin in the corners), leaving the
        # rest of the strip for moving the window.
        top_margin = _RESIZE_MARGIN if edges else _TOP_RESIZE_MARGIN
        if y <= top_margin:
            edges |= Qt.Edge.TopEdge
        elif y >= w.height() - _RESIZE_MARGIN:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _set_cursor(self, edges: Qt.Edge) -> None:
        horizontal = edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge)
        vertical = edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge)
        if horizontal and vertical:
            main_diagonal = edges in (
                Qt.Edge.LeftEdge | Qt.Edge.TopEdge,
                Qt.Edge.RightEdge | Qt.Edge.BottomEdge,
            )
            shape = (
                Qt.CursorShape.SizeFDiagCursor if main_diagonal else Qt.CursorShape.SizeBDiagCursor
            )
        elif horizontal:
            shape = Qt.CursorShape.SizeHorCursor
        elif vertical:
            shape = Qt.CursorShape.SizeVerCursor
        else:
            shape = None
        if shape is None:
            if self._cursor_set:
                QApplication.restoreOverrideCursor()
                self._cursor_set = False
            return
        if self._cursor_set:
            QApplication.changeOverrideCursor(shape)
        else:
            QApplication.setOverrideCursor(shape)
            self._cursor_set = True

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseMove and event.buttons() == Qt.MouseButton.NoButton:
            self._set_cursor(self._edges(event.position()))
        elif kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            edges = self._edges(event.position())
            if edges:
                self._set_cursor(Qt.Edge(0))
                return bool(obj.startSystemResize(edges))
        elif kind == QEvent.Type.Leave:
            self._set_cursor(Qt.Edge(0))
        return False


def build_window_buttons(window, drag_filter: QObject, *, on_close=None) -> QWidget:
    """The minimize/maximize/close strip for ``window``.

    The buttons are stored on the window as ``_win_min``, ``_win_max`` and
    ``_win_close``; ``update_max_button(window)`` keeps the middle one in
    step with the window state. ``on_close`` replaces ``window.close`` for
    the close button (Deck Mode asks what closing should mean first).
    """
    from ..i18n import tr

    strip = QWidget()
    strip.setObjectName("titleStrip")
    strip.setFixedSize(3 * BUTTON_WIDTH, BUTTON_HEIGHT)
    row = QHBoxLayout(strip)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    window._win_min = WindowButton("min")
    window._win_min.setObjectName("winMin")
    window._win_min.setToolTip(tr("Minimize"))
    window._win_min.clicked.connect(window.showMinimized)
    window._win_max = WindowButton("max")
    window._win_max.setObjectName("winMax")
    window._win_max.clicked.connect(window.toggle_maximized)
    window._win_close = WindowButton("close")
    window._win_close.setObjectName("winClose")
    window._win_close.setToolTip(tr("Close"))
    window._win_close.clicked.connect(on_close or window.close)
    for button in (window._win_min, window._win_max, window._win_close):
        button.setFixedSize(BUTTON_WIDTH, BUTTON_HEIGHT)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row.addWidget(button)
    strip.installEventFilter(drag_filter)
    update_max_button(window)
    return strip


class _TopRightPin(QObject):
    """Keeps a widget in its host's top-right corner as the host resizes."""

    def __init__(self, widget: QWidget, host: QWidget) -> None:
        super().__init__(host)
        self._widget = widget
        host.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._widget.move(obj.width() - self._widget.width(), 0)
            self._widget.raise_()
        return False


def pin_top_right(widget: QWidget, host: QWidget) -> None:
    """Parent ``widget`` to ``host`` and keep it in the top-right corner.

    Follows the host's own resizes, so it stays right even when the host is
    rebuilt or resized after the window itself (Deck Mode rescales that way).
    """
    widget.setParent(host)
    _TopRightPin(widget, host)
    widget.move(host.width() - widget.width(), 0)
    widget.raise_()


def update_max_button(window) -> None:
    from ..i18n import tr

    button = getattr(window, "_win_max", None)
    if button is None:
        return
    maximized = window.isMaximized()
    button.set_kind("restore" if maximized else "max")
    button.setToolTip(tr("Restore") if maximized else tr("Maximize"))


def attach_resize_filter(window, resize_filter: EdgeResizeFilter, custom: bool) -> None:
    """(Re)attach the edge-resize filter to the window's current QWindow."""
    handle = window.windowHandle()
    if handle is None and custom:
        window.winId()
        handle = window.windowHandle()
    # Drop a resize cursor the filter may still hold before removing it.
    resize_filter._set_cursor(Qt.Edge(0))
    if handle is not None:
        handle.removeEventFilter(resize_filter)
        if custom:
            handle.installEventFilter(resize_filter)
