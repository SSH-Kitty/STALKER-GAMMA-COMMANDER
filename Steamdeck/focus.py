"""D-pad navigation.

The Steam Deck's default desktop controller layout turns the D-pad into
arrow keys, A into Enter, B into Escape and Y into Space. That is the whole
input contract Deck Mode needs - no evdev, no SDL, no device permissions,
and it works identically in Desktop Mode and Game Mode.

What it does need is somewhere sensible for each arrow press to go, and Qt's
own answer is not good enough. ``setTabOrder`` describes a one-dimensional
chain; a D-pad has four directions. Qt's built-in arrow handling within a
layout does not cross a QStackedWidget or reach a separate nav bar, so a
tab chain would leave whole regions unreachable.

So focus moves geometrically instead: from the focused widget, look for the
nearest candidate lying in the pressed direction. That is layout-agnostic
(grids, rows, lists, nav cells and overlay buttons all behave), it survives
the screen rebuilds Deck Mode does on a language change without any
re-registration, and - the reason it is worth the code - it is directly
testable from widget geometry, which is how tests/test_steamdeck.py proves
every control on every screen is reachable.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLineEdit,
    QWidget,
)

#: How many rows Left/Right jumps inside a list. Matches the number of 72px
#: rows visible at once, so it reads as "one page".
LIST_PAGE_ROWS = 7

#: Cross-axis distance is penalised this much relative to along-axis
#: distance, so a press prefers the candidate straight ahead over a nearer
#: one off to the side.
_CROSS_AXIS_PENALTY = 2.0

_ARROWS = {
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Right: "right",
}


def audit_focusables(root: QWidget) -> list[QWidget]:
    """Every visible, enabled, focusable widget under ``root``.

    Used both by the controller to pick a move target and by the tests to
    assert reachability and minimum touch height, so the two can never
    disagree about what counts as interactive.
    """
    found: list[QWidget] = []
    for widget in root.findChildren(QWidget):
        if widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
            continue
        if not widget.isVisible() or not widget.isEnabled():
            continue
        if widget.size().isEmpty():
            continue
        found.append(widget)
    return found


def _centre(widget: QWidget, reference: QWidget) -> QPoint:
    """``widget``'s centre in ``reference``'s coordinate space."""
    rect = widget.rect()
    return widget.mapTo(reference, rect.center())


def nearest(
    current: QWidget,
    candidates: list[QWidget],
    direction: str,
    reference: QWidget,
) -> QWidget | None:
    """The best focus target from ``current`` in ``direction``."""
    origin = _centre(current, reference)
    best: QWidget | None = None
    best_score = float("inf")
    for candidate in candidates:
        if candidate is current or current.isAncestorOf(candidate):
            continue
        if candidate.isAncestorOf(current):
            continue
        point = _centre(candidate, reference)
        dx = point.x() - origin.x()
        dy = point.y() - origin.y()
        if direction == "up":
            along, cross = -dy, abs(dx)
        elif direction == "down":
            along, cross = dy, abs(dx)
        elif direction == "left":
            along, cross = -dx, abs(dy)
        else:
            along, cross = dx, abs(dy)
        if along <= 0:
            continue
        score = along + _CROSS_AXIS_PENALTY * cross
        if score < best_score:
            best_score = score
            best = candidate
    return best


class DeckFocusController(QObject):
    """Window-level event filter implementing the D-pad contract."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window

    # -- helpers ----------------------------------------------------------
    def scope(self) -> QWidget:
        """The subtree focus may move within.

        While an overlay is up this is the overlay itself, so D-pad
        navigation cannot wander behind the scrim to a control the user
        cannot see.
        """
        overlay = getattr(self._window, "current_overlay", None)
        if callable(overlay):
            overlay = overlay()
        if overlay is not None:
            return overlay
        return self._window.centralWidget() or self._window

    def candidates(self) -> list[QWidget]:
        return audit_focusables(self.scope())

    def ensure_focus(self) -> None:
        """Park focus on something real.

        Focus resting on the window itself means every arrow press has no
        origin to move from, which reads to the user as a dead D-pad.
        """
        app = QApplication.instance()
        current = app.focusWidget() if app is not None else None
        scope = self.scope()
        if current is not None and scope.isAncestorOf(current):
            return
        options = self.candidates()
        if options:
            options[0].setFocus(Qt.FocusReason.OtherFocusReason)

    # -- list handling ----------------------------------------------------
    @staticmethod
    def _list_under(widget: QWidget | None) -> QAbstractItemView | None:
        while widget is not None:
            if isinstance(widget, QAbstractItemView):
                return widget
            widget = widget.parentWidget()
        return None

    def _handle_list(self, view: QAbstractItemView, direction: str) -> bool:
        """Move within a list; return False to let focus leave it.

        Up at the top row and Down at the bottom row fall through to normal
        focus movement, which is what makes a list feel continuous with the
        rest of the screen instead of a trap.
        """
        model = view.model()
        if model is None:
            return False
        count = model.rowCount()
        if count == 0:
            return False
        row = view.currentIndex().row()
        row = max(row, 0)
        if direction == "up":
            if row <= 0:
                return False
            target = row - 1
        elif direction == "down":
            if row >= count - 1:
                return False
            target = row + 1
        elif direction == "left":
            target = max(0, row - LIST_PAGE_ROWS)
        else:
            target = min(count - 1, row + LIST_PAGE_ROWS)
        view.setCurrentIndex(model.index(target, 0))
        return True

    # -- event filter -----------------------------------------------------
    def _owns(self, obj) -> bool:
        """True if ``obj`` is this window or a widget inside it.

        Installed on the QApplication (see DeckWindow), because key events go
        to the focused *widget*, not to the window - a filter on the window
        alone would only ever see keys pressed while the window itself held
        focus, which is to say almost never.
        """
        if obj is self._window:
            return True
        return isinstance(obj, QWidget) and self._window.isAncestorOf(obj)

    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.Type.KeyPress or not self._owns(obj):
            return super().eventFilter(obj, event)

        key = event.key()
        if key == Qt.Key.Key_Escape:
            # B. Never closes the window: one accidental press quitting the
            # app in Game Mode would be the worst failure available here.
            self._window.handle_back()
            return True

        direction = _ARROWS.get(key)
        if direction is None:
            return super().eventFilter(obj, event)

        app = QApplication.instance()
        current = app.focusWidget() if app is not None else None

        # Left/Right inside a text field move the caret. Stealing them would
        # make the search box impossible to edit with the trackpad keyboard.
        if isinstance(current, QLineEdit) and direction in ("left", "right"):
            return super().eventFilter(obj, event)

        scope = self.scope()
        if current is None or not scope.isAncestorOf(current):
            self.ensure_focus()
            return True

        view = self._list_under(current)
        if view is not None and self._handle_list(view, direction):
            return True

        target = nearest(current, self.candidates(), direction, scope)
        if target is not None:
            target.setFocus(Qt.FocusReason.OtherFocusReason)
        return True
