"""D-pad navigation, activation and scrolling.

Two input sources feed this module and both end up in the same place:

* **Keys.** Steam Input's desktop layout (Desktop Mode) sends arrow keys,
  Enter, Escape and Space; a real keyboard sends the same.
* **The gamepad.** In Game Mode Steam sends no keys at all, only a virtual
  controller, which :mod:`Steamdeck.gamepad` reads directly.

Each is translated into a small action vocabulary (``up``, ``accept``,
``back``, ``tab_next``...) and handed to :meth:`DeckFocusController.dispatch`,
so a press means the same thing whichever way it arrived. When Desktop Mode
delivers both - the pad event *and* the key Steam synthesises from it - the
key is dropped as a duplicate.

Focus moves geometrically: from the focused widget, look for the nearest
candidate lying in the pressed direction. That is layout-agnostic (grids,
rows, lists, nav cells and overlay buttons all behave), survives the screen
rebuilds Deck Mode does on a language change, and is directly testable from
widget geometry. Whatever receives focus is then scrolled into view - a
QScrollArea only does that by itself for Tab, never for a programmatic
``setFocus()``, which is how every D-pad move lands.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QLineEdit,
    QScrollArea,
    QScroller,
    QScrollerProperties,
    QWidget,
)
from shiboken6 import isValid

from . import gamepad as pad
from .scale import px

#: How many rows Left/Right jumps inside a list. Matches the number of 72px
#: rows visible at once, so it reads as "one page".
LIST_PAGE_ROWS = 7

#: Cross-axis distance is penalised this much relative to along-axis
#: distance, so a press prefers the candidate straight ahead over a nearer
#: one off to the side.
_CROSS_AXIS_PENALTY = 2.0

#: A key arriving this soon after the same action came from the gamepad is
#: the copy Steam's desktop layout synthesised from that press.
_DUPLICATE_WINDOW_S = 0.08

#: Room left around a widget scrolled into view, so its focus ring and the
#: row above/below it stay visible rather than sitting flush on the edge.
_VISIBLE_MARGIN_X = 0
_VISIBLE_MARGIN_Y = 48

#: Right-stick scrolling: the speed at full tilt, in design pixels a second
#: (about 30 rows of 72px), the exponent of the speed curve, and the frame
#: timer that applies it.
SCROLL_MAX_PX_PER_S = 2200
_SCROLL_CURVE = 1.7
_SCROLL_FRAME_MS = 16
_SCROLL_MAX_FRAME_S = 0.05

_KEY_ACTIONS = {
    Qt.Key.Key_Up: pad.UP,
    Qt.Key.Key_Down: pad.DOWN,
    Qt.Key.Key_Left: pad.LEFT,
    Qt.Key.Key_Right: pad.RIGHT,
    Qt.Key.Key_Escape: pad.BACK,
    Qt.Key.Key_Return: pad.ACCEPT,
    Qt.Key.Key_Enter: pad.ACCEPT,
    Qt.Key.Key_Space: pad.ACCEPT,
    Qt.Key.Key_PageUp: pad.PAGE_UP,
    Qt.Key.Key_PageDown: pad.PAGE_DOWN,
    Qt.Key.Key_Tab: pad.TAB_NEXT,
    Qt.Key.Key_Backtab: pad.TAB_PREV,
    Qt.Key.Key_F1: pad.HELP,
}

#: Keys a text field needs for itself - caret movement and typing a space.
#: Enter is left to the field too, so a physical keyboard submits normally.
_TEXT_KEYS = {
    Qt.Key.Key_Left,
    Qt.Key.Key_Right,
    Qt.Key.Key_Space,
    Qt.Key.Key_Return,
    Qt.Key.Key_Enter,
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


def usable(widget: QWidget | None) -> bool:
    """True if ``widget`` still exists and could take focus right now."""
    return (
        widget is not None
        and isValid(widget)
        and widget.isVisible()
        and widget.isEnabled()
        and widget.focusPolicy() != Qt.FocusPolicy.NoFocus
    )


def reading_order(widgets: list[QWidget], reference: QWidget) -> list[QWidget]:
    """``widgets`` top to bottom, then left to right, as they appear on screen.

    ``findChildren`` returns creation order, which is not what anyone sees:
    a screen that builds its main button after a row of chips above it
    would otherwise start focus on the button, below the chips.
    """

    def key(widget: QWidget) -> tuple[int, int]:
        point = widget.mapTo(reference, QPoint(0, 0))
        return point.y(), point.x()

    return sorted(widgets, key=key)


def rect_in(widget: QWidget, reference: QWidget) -> QRect:
    return QRect(widget.mapTo(reference, QPoint(0, 0)), widget.size())


def closest_to(rect: QRect, candidates: list[QWidget], reference: QWidget) -> QWidget | None:
    """The candidate whose centre is nearest ``rect``'s centre."""
    centre = rect.center()
    best, best_distance = None, float("inf")
    for candidate in candidates:
        point = _centre(candidate, reference)
        distance = (point.x() - centre.x()) ** 2 + (point.y() - centre.y()) ** 2
        if distance < best_distance:
            best, best_distance = candidate, distance
    return best


def _centre(widget: QWidget, reference: QWidget) -> QPoint:
    """``widget``'s centre in ``reference``'s coordinate space."""
    rect = widget.rect()
    return widget.mapTo(reference, rect.center())


def _span(widget: QWidget, reference: QWidget, horizontal: bool) -> tuple[int, int]:
    top_left = widget.mapTo(reference, QPoint(0, 0))
    if horizontal:
        return top_left.x(), top_left.x() + widget.width()
    return top_left.y(), top_left.y() + widget.height()


def _gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Distance between two 1-D ranges; 0 when they overlap."""
    return max(0, max(a[0], b[0]) - min(a[1], b[1]))


def nearest(
    current: QWidget,
    candidates: list[QWidget],
    direction: str,
    reference: QWidget,
) -> QWidget | None:
    """The best focus target from ``current`` in ``direction``.

    Sideways distance is measured edge to edge, not centre to centre: from
    a full-width search box, the chip directly below it overlaps it and is
    "straight ahead", even though the chip's centre is far off to the side.
    Centre distance then only breaks ties, so of several chips below, the
    one under the box's middle wins.
    """
    # Strictly ahead first: the candidate's near edge must lie past the
    # current widget's centre, so pressing Right on a chip under a wide
    # search box can't "move right" up into the box. Only if nothing at
    # all qualifies is the looser centre-only test used.
    return _nearest(current, candidates, direction, reference, strict=True) or _nearest(
        current, candidates, direction, reference, strict=False
    )


def _nearest(
    current: QWidget,
    candidates: list[QWidget],
    direction: str,
    reference: QWidget,
    *,
    strict: bool,
) -> QWidget | None:
    origin = _centre(current, reference)
    vertical = direction in ("up", "down")
    current_span = _span(current, reference, horizontal=vertical)
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
            along = -dy
        elif direction == "down":
            along = dy
        elif direction == "left":
            along = -dx
        else:
            along = dx
        if along <= 0:
            continue
        if strict:
            low, high = _span(candidate, reference, horizontal=not vertical)
            centre = origin.y() if vertical else origin.x()
            if direction in ("down", "right") and low < centre:
                continue
            if direction in ("up", "left") and high > centre:
                continue
        gap = _gap(current_span, _span(candidate, reference, horizontal=vertical))
        centre_offset = abs(dx) if vertical else abs(dy)
        score = along + _CROSS_AXIS_PENALTY * gap + 0.1 * centre_offset
        if score < best_score:
            best_score = score
            best = candidate
    return best


def scroll_area_of(widget: QWidget | None) -> QScrollArea | None:
    """The innermost QScrollArea whose content contains ``widget``."""
    node = widget.parentWidget() if widget is not None else None
    while node is not None:
        content = node.widget() if isinstance(node, QScrollArea) else None
        if content is not None and content.isAncestorOf(widget):
            return node
        node = node.parentWidget()
    return None


def ensure_visible(widget: QWidget | None) -> None:
    """Scroll every QScrollArea around ``widget`` so it is on screen."""
    area = scroll_area_of(widget)
    while area is not None:
        smooth = getattr(area, "smooth_to", None)
        if callable(smooth):
            # Let Qt work out where it would jump to, then glide there.
            bar = area.verticalScrollBar()
            start = bar.value()
            area.ensureWidgetVisible(widget, _VISIBLE_MARGIN_X, _VISIBLE_MARGIN_Y)
            target = bar.value()
            if target != start:
                bar.setValue(start)
                smooth(target)
        else:
            area.ensureWidgetVisible(widget, _VISIBLE_MARGIN_X, _VISIBLE_MARGIN_Y)
        area = scroll_area_of(area)


def enable_kinetic_scroll(view: QAbstractScrollArea) -> None:
    """Let a finger (or the trackpad-as-mouse) flick ``view``.

    Grabbed as a left-button gesture, not only a touch gesture: under
    gamescope the touchscreen arrives as mouse events, so a touch-only
    gesture would never trigger in Game Mode. A drag that turns into a
    scroll cancels the press underneath it, which is what stops a flick
    through the mod list from toggling whatever row it started on.
    """
    if isinstance(view, QAbstractItemView):
        # A list scrolls a whole item at a time by default, so a flick
        # jumped from row to row instead of gliding under the finger.
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        # Per-pixel mode leaves the wheel/trackpad step at a few pixels;
        # keep one notch moving about one row, as before.
        view.verticalScrollBar().setSingleStep(px(24))
    viewport = view.viewport()
    QScroller.grabGesture(viewport, QScroller.ScrollerGestureType.LeftMouseButtonGesture)
    scroller = QScroller.scroller(viewport)
    props = scroller.scrollerProperties()
    off = QScrollerProperties.OvershootPolicy.OvershootAlwaysOff
    props.setScrollMetric(QScrollerProperties.ScrollMetric.VerticalOvershootPolicy, off)
    props.setScrollMetric(QScrollerProperties.ScrollMetric.HorizontalOvershootPolicy, off)
    # Short press delay: a deliberate tap still lands at once, but a drag
    # starting on a button is recognised before that button sees the press.
    props.setScrollMetric(QScrollerProperties.ScrollMetric.MousePressEventDelay, 0.08)
    props.setScrollMetric(QScrollerProperties.ScrollMetric.DragStartDistance, 0.004)
    scroller.setScrollerProperties(props)


def activate(widget: QWidget | None) -> bool:
    """Do what pressing A on ``widget`` means. False if it means nothing."""
    if widget is None or not widget.isEnabled():
        return False
    if isinstance(widget, QAbstractButton):
        widget.click()
        return True
    if isinstance(widget, QAbstractItemView):
        index = widget.currentIndex()
        if not index.isValid():
            return False
        # Through clicked(), not activated(): the Deck's lists act on a
        # single tap, and QListWidget turns clicked() into itemClicked() -
        # one signal path for touch and A alike, so neither can double-fire.
        widget.clicked.emit(index)
        return True
    signal = getattr(widget, "activated", None)
    if signal is not None and hasattr(signal, "emit"):
        signal.emit()
        return True
    return False


class DeckFocusController(QObject):
    """Application-level event filter implementing the D-pad contract."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self._last_pad: dict[str, float] = {}
        #: The widget holding focus, where it was, and which screen or
        #: overlay it belongs to - kept so focus can be put back somewhere
        #: sensible when that widget hides, disables or is deleted.
        self._held: QWidget | None = None
        self._held_rect = QRect()
        self._held_region: QWidget | None = None
        #: (widget, reason) of the last FocusIn, to tell Qt's own
        #: "next in the tab chain" moves apart from deliberate ones.
        self._last_focus_in: tuple[QWidget | None, Qt.FocusReason] = (
            None,
            Qt.FocusReason.OtherFocusReason,
        )
        self._rescue_pending = False
        # The right stick's continuous scroll (see on_pad_scroll).
        self._scroll_speed = 0.0
        self._scroll_direction = 1
        self._scroll_carry = 0.0
        self._scroll_last = 0.0
        self._scroll_widget: QAbstractScrollArea | None = None
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._scroll_timer.setInterval(_SCROLL_FRAME_MS)
        self._scroll_timer.timeout.connect(self._scroll_frame)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)

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

    def focused(self) -> QWidget | None:
        app = QApplication.instance()
        return app.focusWidget() if app is not None else None

    def _current_page(self) -> QWidget | None:
        getter = getattr(self._window, "current_page", None)
        return getter() if callable(getter) else None

    def entry_point(self, region: QWidget | None = None) -> QWidget | None:
        """Where focus should land on arriving at ``region``.

        For a screen: the control used there last, else the screen's own
        ``default_focus()``, else its first control in reading order. For an
        overlay: its default button, else its first control.
        """
        region = region if region is not None else self.scope()
        if region is None or not isValid(region):
            return None
        page = self._current_page()
        if region is self.scope() and page is not None and not hasattr(region, "default_button"):
            # The window as a whole: that means the screen showing in it.
            region = page
        remembered = getattr(region, "deck_last_focus", None)
        if usable(remembered) and region.isAncestorOf(remembered):
            return remembered
        preferred = getattr(region, "default_focus", None)
        preferred = preferred() if callable(preferred) else getattr(region, "default_button", None)
        if usable(preferred) and region.isAncestorOf(preferred):
            return preferred
        options = audit_focusables(region)
        if not options:
            return None
        return reading_order(options, self._window)[0]

    def ensure_focus(self) -> None:
        """Park focus on something real.

        Focus resting on the window itself means every arrow press has no
        origin to move from, which reads to the user as a dead D-pad.
        """
        current = self.focused()
        scope = self.scope()
        if current is not None and scope.isAncestorOf(current) and current.isVisible():
            return
        target = self.entry_point()
        if target is None:
            options = self.candidates()
            target = reading_order(options, self._window)[0] if options else None
        if target is not None:
            self.focus(target)

    def focus(self, widget: QWidget) -> None:
        widget.setFocus(Qt.FocusReason.OtherFocusReason)
        ensure_visible(widget)

    def _region_of(self, widget: QWidget) -> QWidget | None:
        """The overlay or screen ``widget`` lives in; None for window chrome."""
        node = widget
        while node is not None:
            if hasattr(node, "default_button") and hasattr(node, "closed"):
                return node  # a DeckOverlay
            node = node.parentWidget()
        pages = getattr(self._window, "_pages", {})
        for page in pages.values():
            if isValid(page) and page.isAncestorOf(widget):
                return page
        return None

    def _on_focus_changed(self, old, new) -> None:
        if isinstance(new, QWidget) and self._owns(new):
            ensure_visible(new)
            self._held = new
            self._held_rect = rect_in(new, self._window)
            self._held_region = self._region_of(new)
            region = self._held_region
            if region is not None:
                # Remembered per screen (and per overlay), so a tab switch
                # or a closed picker comes back to the same control.
                region.deck_last_focus = new
            # Some prompts depend on what is focused (X on the Mods list
            # acts on a category; elsewhere it jumps to one).
            updater = getattr(self._window, "update_hints", None)
            if callable(updater):
                updater()
            return
        if new is None and self._held is not None and self._window.isActiveWindow():
            # The focused widget was deleted or cleared its own focus. (Not
            # the app losing focus to the game: the window is then inactive.)
            self._schedule_rescue()

    # -- losing focus -----------------------------------------------------
    def _schedule_rescue(self) -> None:
        if self._rescue_pending:
            return
        self._rescue_pending = True
        QTimer.singleShot(0, self, self._rescue)

    def _note_loss(self, widget: QWidget) -> None:
        """The focused widget is hiding or being disabled."""
        if widget is not self._held:
            return
        if isValid(widget) and widget.isVisible():
            self._held_rect = rect_in(widget, self._window)
        self._schedule_rescue()

    def _rescue(self) -> None:
        """Put focus back on a sensible control after it was lost.

        Qt's own reaction to a focused widget hiding or disabling is to
        hand focus to the next widget in *creation* order - often something
        off-screen, in the nav bar, or nothing at all. Starting a job hides
        the button that started it; finishing it hides Cancel; a wizard
        step replaces every widget. Rather than patch each of those, focus
        goes to the screen's default control, or failing that the control
        nearest to where the lost one was.
        """
        self._rescue_pending = False
        if not self._window.isVisible():
            return
        region = self._held_region
        if region is None or not isValid(region) or not region.isVisible():
            return
        scope = self.scope()
        if region is not scope and not scope.isAncestorOf(region):
            # An overlay went up over the screen in the meantime; closing it
            # restores focus by its own route.
            return
        current = self.focused()
        automatic = self._last_focus_in[0] is current and self._last_focus_in[1] in (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
        )
        if usable(current) and scope.isAncestorOf(current) and not automatic:
            # Still fine, or already moved on deliberately (a job's start
            # hands focus to its Cancel button, say).
            return
        preferred = getattr(region, "default_focus", None)
        preferred = preferred() if callable(preferred) else getattr(region, "default_button", None)
        if usable(preferred) and region.isAncestorOf(preferred):
            target = preferred
        else:
            target = closest_to(self._held_rect, audit_focusables(region), self._window)
        if target is None:
            target = self.entry_point()
        if target is not None:
            self.focus(target)

    # -- list handling ----------------------------------------------------
    @staticmethod
    def _list_under(widget: QWidget | None) -> QAbstractItemView | None:
        while widget is not None:
            if isinstance(widget, QAbstractItemView):
                return widget
            widget = widget.parentWidget()
        return None

    def _beside(self, view: QWidget, direction: str) -> QWidget | None:
        """A control level with ``view`` in ``direction``, if there is one.

        Only widgets that share some of the list's height count: from a
        full-width list, Right must not jump up to a chip in the row above.
        """
        scope = self.scope()
        span = _span(view, scope, horizontal=False)
        beside = [
            widget
            for widget in self.candidates()
            if _gap(span, _span(widget, scope, horizontal=False)) == 0
        ]
        return _nearest(view, beside, direction, scope, strict=True)

    def _handle_list(self, view: QAbstractItemView, direction: str) -> bool:
        """Move within a list; return False to let focus leave it.

        Up at the top row and Down at the bottom row fall through to normal
        focus movement, which is what makes a list feel continuous with the
        rest of the screen instead of a trap. Left/Right leave the list for
        a control beside it; with nothing there they jump a page instead.
        """
        model = view.model()
        if model is None:
            return False
        count = model.rowCount()
        if count == 0:
            return False
        if direction in ("left", "right"):
            neighbour = self._beside(view, direction)
            if neighbour is not None:
                self.focus(neighbour)
                return True
        row = max(view.currentIndex().row(), 0)
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

    @staticmethod
    def _scroll_text(widget: QWidget, direction: str) -> bool:
        """Up/Down inside a focusable scrolling text panel scroll it.

        Returns False at either end, so focus moves on past it instead of
        getting stuck in a block of text.
        """
        if direction not in ("up", "down") or not isinstance(widget, QScrollArea):
            return False
        bar = widget.verticalScrollBar()
        if bar.maximum() <= 0:
            return False
        step = max(40, widget.viewport().height() // 3)
        # A gliding panel is judged by where it is heading, not where the
        # animation has got to, so a quick second press is not swallowed.
        position = getattr(widget, "scroll_position", bar.value)()
        if direction == "down":
            if position >= bar.maximum():
                return False
            target = position + step
        else:
            if position <= bar.minimum():
                return False
            target = position - step
        smooth = getattr(widget, "smooth_to", None)
        if callable(smooth):
            smooth(target)
        else:
            bar.setValue(target)
        return True

    # -- actions ----------------------------------------------------------
    def move(self, direction: str) -> None:
        if self._scroll_timer.isActive():
            # Mid stick-scroll: move on from what is in view, not from the
            # control the view left behind.
            self._settle_scroll(self._scroll_widget, self._scroll_direction)
        current = self.focused()
        scope = self.scope()
        # Settle any gliding scroll area first: neighbours are chosen by
        # position, and mid-glide those positions are still moving.
        for area in self._window.findChildren(QScrollArea):
            finish = getattr(area, "finish_glide", None)
            if callable(finish):
                finish()
        if current is None or not scope.isAncestorOf(current):
            self.ensure_focus()
            return
        grid_move = getattr(scope, "grid_move", None)
        if callable(grid_move) and grid_move(current, direction):
            return
        view = self._list_under(current)
        if view is not None and self._handle_list(view, direction):
            return
        if self._scroll_text(current, direction):
            return
        target = nearest(current, self.candidates(), direction, scope)
        if target is not None:
            self.focus(target)

    def _follow_scroll(self, area: QScrollArea, step: int) -> None:
        """After scrolling ``area``, keep focus on something still in view.

        Otherwise the next D-pad press would yank the view straight back to
        where the focused widget was left.
        """
        current = self.focused()
        viewport = area.viewport()
        inside = area.widget()
        if inside is None:
            return
        if (
            current is not None
            and inside.isAncestorOf(current)
            and viewport.rect().contains(current.mapTo(viewport, current.rect().center()))
        ):
            return
        visible = [
            widget
            for widget in audit_focusables(inside)
            if viewport.rect().contains(widget.mapTo(viewport, widget.rect().center()))
        ]
        if visible:
            # Prefer a control already clear of the edges by the margin
            # ensure_visible keeps: focusing one flush against the edge
            # would nudge the view back as the scroll comes to rest.
            clear = viewport.rect().adjusted(0, px(_VISIBLE_MARGIN_Y), 0, -px(_VISIBLE_MARGIN_Y))
            settled = [
                widget
                for widget in visible
                if clear.contains(QRect(widget.mapTo(viewport, QPoint(0, 0)), widget.size()))
            ]
            ordered = sorted(settled or visible, key=lambda w: w.mapTo(viewport, QPoint(0, 0)).y())
            target = ordered[0] if step > 0 else ordered[-1]
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    def page(self, step: int) -> None:
        """L2/R2, PageUp/PageDown: a screenful at a time."""
        current = self.focused()
        view = self._list_under(current)
        if view is not None and view.model() is not None and view.model().rowCount():
            count = view.model().rowCount()
            row = max(view.currentIndex().row(), 0)
            target = max(0, min(count - 1, row + step * LIST_PAGE_ROWS))
            view.setCurrentIndex(view.model().index(target, 0))
            return
        area = scroll_area_of(current)
        if area is None:
            return
        bar = area.verticalScrollBar()
        bar.setValue(bar.value() + step * bar.pageStep())
        self._follow_scroll(area, step)

    def _scroll_target(self) -> QAbstractScrollArea | None:
        """What the right stick scrolls, given where focus is.

        In order: a focused text panel (patch notes, a check's details), an
        overlay's own ``scroll_target`` (a FOMOD option's description), a
        focused list, then the screen itself.
        """
        # The window's own record as a fallback: the application's focus
        # widget is None for a moment while activation settles.
        current = self.focused() or self._window.focusWidget()
        scope = self.scope()
        if (
            isinstance(current, QScrollArea)
            and scope.isAncestorOf(current)
            and current.verticalScrollBar().maximum() > 0
        ):
            return current
        target = getattr(scope, "scroll_target", None)
        if isinstance(target, QScrollArea) and isValid(target) and target.isVisible():
            return target
        view = self._list_under(current)
        if view is not None and scope.isAncestorOf(view):
            return view
        area = scroll_area_of(current)
        if area is None and not hasattr(scope, "default_button"):
            # Focus on nothing scrollable (window chrome): scroll the screen.
            area = getattr(self._current_page(), "scroll", None)
        return area if isinstance(area, QScrollArea) else None

    def _settle_scroll(self, target: QAbstractScrollArea | None, direction: int) -> None:
        """After the view moved under it, bring focus to something in view.

        Otherwise the next D-pad press would yank the view straight back to
        a control left behind off-screen. A list moves its current row onto
        the first (or last) row showing; a screen moves focus to its first
        (or last) visible control. Text panels have nothing to move.
        """
        if target is None or not isValid(target) or not target.isVisible():
            return
        if isinstance(target, QAbstractItemView):
            viewport = target.viewport()
            current = target.currentIndex()
            if current.isValid() and viewport.rect().contains(target.visualRect(current)):
                return
            probe = px(8)
            point = QPoint(probe, probe if direction >= 0 else viewport.height() - probe)
            index = target.indexAt(point)
            if not index.isValid():
                return
            rect = target.visualRect(index)
            if not viewport.rect().contains(rect):
                # Only partly showing: take its neighbour that is fully in.
                neighbour = target.model().index(index.row() + (1 if direction >= 0 else -1), 0)
                if neighbour.isValid():
                    index = neighbour
            target.setCurrentIndex(index)
            return
        if isinstance(target, QScrollArea) and target is not self.focused():
            self._follow_scroll(target, direction or 1)

    def scroll(self, step: int) -> None:
        """One notch of scrolling (a row), for a non-analog scroll action."""
        target = self._scroll_target()
        if target is None:
            return
        finish = getattr(target, "finish_glide", None)
        if callable(finish):
            finish()
        bar = target.verticalScrollBar()
        bar.setValue(bar.value() + step * px(72))
        self._settle_scroll(target, step)

    # -- the right stick ----------------------------------------------------
    def on_pad_scroll(self, deflection: float) -> None:
        """Slot for GamepadMonitor.scroll_axis: analog, continuous scrolling.

        The stick sets a speed, not a step: a frame timer moves the view by
        speed x elapsed time, so it glides at whatever rate the thumb asks
        for - a nudge creeps a line at a time, full tilt crosses the 900-row
        mod list in a few seconds. Focus catches up when the stick is let
        go (see _settle_scroll).
        """
        if deflection and not self._window.isActiveWindow():
            return
        self._scroll_speed = float(deflection)
        if deflection:
            if not self._scroll_timer.isActive():
                self._scroll_widget = None
                self._scroll_carry = 0.0
                self._scroll_last = time.monotonic()
                self._scroll_timer.start()
            self._scroll_direction = 1 if deflection > 0 else -1
        else:
            self._stop_scroll()

    def _stop_scroll(self) -> None:
        self._scroll_speed = 0.0
        if not self._scroll_timer.isActive():
            return
        self._scroll_timer.stop()
        self._settle_scroll(self._scroll_widget, self._scroll_direction)
        self._scroll_widget = None

    def _scroll_frame(self) -> None:
        now = time.monotonic()
        # Capped, so a frame delayed by a busy event loop does not turn
        # into a lurch.
        elapsed = min(now - self._scroll_last, _SCROLL_MAX_FRAME_S)
        self._scroll_last = now
        speed = self._scroll_speed
        if not speed:
            self._stop_scroll()
            return
        target = self._scroll_widget
        if target is None or not isValid(target) or not target.isVisible():
            target = self._scroll_target()
            self._scroll_widget = target
            if target is None:
                return
            finish = getattr(target, "finish_glide", None)
            if callable(finish):
                finish()
        # A curve rather than a straight line: fine control near the
        # centre, real speed at full tilt.
        rate = px(SCROLL_MAX_PX_PER_S) * abs(speed) ** _SCROLL_CURVE
        delta = rate * elapsed * (1 if speed > 0 else -1) + self._scroll_carry
        whole = int(delta)
        self._scroll_carry = delta - whole
        if whole:
            bar = target.verticalScrollBar()
            bar.setValue(bar.value() + whole)

    def dispatch(self, action: str) -> bool:
        """Carry out one action. True if it was handled."""
        window = self._window
        if action == pad.BACK:
            # B never closes the window: one accidental press quitting the
            # app in Game Mode would be the worst failure available here.
            window.handle_back()
            return True
        if action in pad.DIRECTIONS:
            steer = getattr(window, "handle_direction", None)
            if not (callable(steer) and steer(action)):
                self.move(action)
            return True
        if action == pad.ACCEPT_HOLD:
            # Offered to the screen first; where a hold means nothing it is
            # just a slow press.
            handler = getattr(window, "handle_action", None)
            if callable(handler) and handler(action):
                return True
            return self.dispatch(pad.ACCEPT)
        if action == pad.ACCEPT:
            current = self.focused()
            if current is None or not self.scope().isAncestorOf(current):
                self.ensure_focus()
                return True
            if isinstance(current, QLineEdit):
                opener = getattr(window, "open_keyboard", None)
                if callable(opener):
                    opener(current)
                return True
            activate(current)
            return True
        if action in (pad.PAGE_UP, pad.PAGE_DOWN):
            self.page(-1 if action == pad.PAGE_UP else 1)
            return True
        if action in (pad.SCROLL_UP, pad.SCROLL_DOWN):
            self.scroll(-1 if action == pad.SCROLL_UP else 1)
            return True
        handler = getattr(window, "handle_action", None)
        return bool(callable(handler) and handler(action))

    def on_pad_action(self, action: str) -> None:
        """Slot for :class:`Steamdeck.gamepad.GamepadMonitor`."""
        # A pad is read whether or not COMMANDER is in front: while the game
        # has focus, its button presses must not also drive this window.
        if not self._window.isActiveWindow():
            return
        self._last_pad[action] = time.monotonic()
        self.dispatch(action)

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
        kind = event.type()
        if kind == QEvent.Type.FocusIn and isinstance(obj, QWidget):
            self._last_focus_in = (obj, event.reason())
            return super().eventFilter(obj, event)
        if obj is self._held and kind in (QEvent.Type.Hide, QEvent.Type.EnabledChange):
            if kind == QEvent.Type.Hide or not obj.isEnabled():
                self._note_loss(obj)
            return super().eventFilter(obj, event)
        if kind != QEvent.Type.KeyPress or not self._owns(obj):
            return super().eventFilter(obj, event)
        key = event.key()
        action = _KEY_ACTIONS.get(key)
        if action is None:
            return super().eventFilter(obj, event)
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if modifiers not in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
            # Ctrl+Shift+D and friends belong to their shortcuts.
            return super().eventFilter(obj, event)
        if key == Qt.Key.Key_Tab and modifiers & Qt.KeyboardModifier.ShiftModifier:
            action = pad.TAB_PREV

        # Text fields keep caret movement, typing and Enter for themselves.
        if isinstance(self.focused(), QLineEdit) and key in _TEXT_KEYS:
            return super().eventFilter(obj, event)

        last = self._last_pad.get(action)
        if last is not None and time.monotonic() - last < _DUPLICATE_WINDOW_S:
            return True
        if action == pad.ACCEPT:
            # While the pad's A is down and waiting to be told tap from
            # hold, an Enter Steam Input sends for the same press would act
            # on it first (and its autorepeat again and again).
            gamepad = getattr(self._window, "gamepad", None)
            down = getattr(gamepad, "accept_down", None)
            if callable(down) and down():
                return True
        self.dispatch(action)
        return True
