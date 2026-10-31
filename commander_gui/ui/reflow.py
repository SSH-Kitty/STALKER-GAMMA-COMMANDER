"""Rows that go one above the other when the window is too narrow.

A page marks a layout with :func:`row` (a box layout), :func:`divided`
(a grid split into sides by divider lines) or :func:`labelled` (a grid of
labelled rows). :func:`watch` re-checks them
whenever the page is resized or its content changes: a row that no longer fits stacks its items,
a divided grid puts each side under the one before it, and both go back
once there is room again. On a resize, instead of jumping, the moved
items and the cards around them glide to their new places (unless
Animations is off in Settings).

A page with its own narrow-window moves wraps them in :func:`animated`.
"""

from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import (
    QCoreApplication,
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import (
    QBoxLayout,
    QGridLayout,
    QHBoxLayout,
    QLayout,
    QScrollArea,
    QWidget,
)

from .. import gui_settings

#: Length of one glide.
GLIDE_MS = 280
_ROW = "reflowRow"
_DIVIDED = "reflowDivided"
_LABELLED = "reflowLabelled"
_DIVIDERS = ("vDivider", "installDivider")
_DIRECT = Qt.FindChildOption.FindDirectChildrenOnly
#: Where a divided grid's items sat side by side, and its column
#: stretches, while it is stacked.
_grid_places: dict[QGridLayout, tuple[list, list[int], list[int]]] = {}
#: Extra room between two stacked sides.
_SIDE_GAP = 14
#: Room a stacked layout needs past its one-line width before it goes back,
#: so a scroll bar coming and going can't flip it to and fro.
_SLACK = 24
#: The running glide per page, stopped when a newer one starts.
_glides: dict[QWidget, QParallelAnimationGroup] = {}
#: Rows with their own width check, see :func:`row`.
_needs: dict[QLayout, object] = {}
#: Pages mid-fit: settling their layouts can resize them again, and the
#: running fit already looks until nothing changes.
_fitting: set[QWidget] = set()


def row(spacing: int = 12, *, natural: bool = False, need=None) -> QHBoxLayout:
    """A left-to-right row that stacks top to bottom when it doesn't fit.
    ``natural`` as for :func:`divided`. ``need``: a function giving the
    width one line needs, for a row whose items scroll inside (a scroll
    area alone always says it fits)."""
    layout = QHBoxLayout()
    layout.setObjectName(_ROW)
    layout.setProperty("reflowNatural", natural)
    layout.setSpacing(spacing)
    if need is not None:
        _needs[layout] = need
    return layout


def mark_row(layout: QBoxLayout) -> QBoxLayout:
    """Make an existing box layout a :func:`row`."""
    layout.setObjectName(_ROW)
    return layout


def divided(grid: QGridLayout, *, natural: bool = False) -> QGridLayout:
    """Mark a grid whose sides are split by divider lines (``#vDivider`` or
    ``#installDivider`` in their own columns): stacked, each side goes under
    the one before it and the dividers hide. ``natural`` stacks it as soon
    as the sides would get less than their natural width, not their
    smallest - for fields that squeeze too far."""
    grid.setObjectName(_DIVIDED)
    grid.setProperty("reflowNatural", natural)
    return grid


def labelled(grid: QGridLayout, columns: int = 2) -> QGridLayout:
    """Mark a grid of rows that each start with a label in column 0:
    stacked, each label goes above its row and the rest wraps ``columns``
    to a line."""
    grid.setObjectName(_LABELLED)
    grid.setProperty("reflowColumns", columns)
    return grid


def group(*items, spacing: int = 8) -> QWidget:
    """Widgets that stay together on one line inside a :func:`row`.
    An int adds that much spacing."""
    widget = QWidget()
    widget.setObjectName("panelTransparent")
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for item in items:
        if isinstance(item, int):
            layout.addSpacing(item)
        elif isinstance(item, QLayout):
            layout.addLayout(item)
        else:
            layout.addWidget(item)
    return widget


class _Watcher(QObject):
    def __init__(self, page: QWidget) -> None:
        super().__init__(page)
        self._page = page
        self._queued = False
        self._before: dict | None = None

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Resize and obj is self._page:
            layout = obj.layout()
            if layout is not None:  # lay out the new size now, not later
                layout.setGeometry(obj.rect())
            fit(obj, before=self._before)
        elif kind in (QEvent.Type.Show, QEvent.Type.LayoutRequest):
            # Shown, or its content changed size: laid out only after this
            # event, so look once that is done. No glide here: a card the
            # page just rebuilt starts side by side, and sliding it into
            # place after every rebuild would look twitchy.
            self._queue()
        return False

    def window_resizing(self) -> None:
        """Before the window passes a new size down: note where everything
        on the page is on screen, so the glide starts there - not from the
        squeezed or stretched places the new size gives the cards for an
        instant. One drag step can resize more than once; the first places
        are the ones last painted."""
        if self._before is None and self._page.isVisible():
            glide = _glides.get(self._page)
            self._before = _snapshot(self._page) if glide is None else _glide_places(glide)
            QTimer.singleShot(0, self._forget)

    def _forget(self) -> None:
        self._before = None

    def _queue(self) -> None:
        if not self._queued:
            self._queued = True
            QTimer.singleShot(0, self._run)

    def _run(self) -> None:
        self._queued = False
        # Mid-glide the widths are half-way ones; the glide's end looks.
        if self._page not in _glides:
            fit(self._page, animate=False)


def watch(page: QWidget) -> None:
    """Re-fit ``page``'s rows whenever it is resized or its content changes.
    A page with moves of its own does them in a ``reflow_extra()`` method,
    run on every fit."""
    watcher = _Watcher(page)
    page.installEventFilter(watcher)
    top = QWidget.window(page)  # pages keep their own ``window`` attribute
    if top is not page:
        _spy(top).watchers.append(watcher)
    for area in page.findChildren(QScrollArea):
        if area.widget() is not None:
            area.widget().installEventFilter(watcher)


class _WindowSpy(QObject):
    """Watches a window's native handle: it hears of a new size before the
    window's layout passes it down (filters on the window widget itself run
    only after that). Re-hooked when the handle is recreated, as switching
    the title bar does."""

    def __init__(self, top: QWidget) -> None:
        super().__init__(top)
        self.watchers: list[_Watcher] = []
        self._top = top
        self._handle = None
        top.installEventFilter(self)
        self._hook()

    def _hook(self) -> None:
        handle = self._top.windowHandle()
        if handle is not None and handle is not self._handle:
            handle.installEventFilter(self)
            self._handle = handle

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if obj is self._top:
            if kind in (QEvent.Type.WinIdChange, QEvent.Type.Show):
                self._hook()
        elif kind == QEvent.Type.Resize:
            for watcher in self.watchers:
                watcher.window_resizing()
        return False


_spies: dict[QWidget, _WindowSpy] = {}


def _spy(top: QWidget) -> _WindowSpy:
    spy = _spies.get(top)
    if spy is None:
        spy = _spies[top] = _WindowSpy(top)
    return spy


# -- fitting --------------------------------------------------------------
def is_stacked(layout: QLayout) -> bool:
    """Whether ``layout`` is one above the other right now."""
    if isinstance(layout, QGridLayout):
        return layout in _grid_places
    return layout.direction() == QBoxLayout.Direction.TopToBottom


def _room(layout: QLayout) -> int:
    """The width ``layout`` would get if nothing on the page were too wide."""
    width = layout.geometry().width()
    widget = layout.parentWidget()
    while widget is not None:
        if isinstance(widget, QScrollArea):
            content = widget.widget()
            if content is not None:
                width -= max(0, content.width() - widget.viewport().width())
            break
        widget = widget.parentWidget()
    return width


def _pending(page: QWidget) -> list[tuple[QLayout, bool]]:
    changes = []
    for name in (_ROW, _DIVIDED, _LABELLED):
        for layout in page.findChildren(QLayout, name):
            owner = layout.parentWidget()
            if owner is None or not owner.isVisible() or not layout.geometry().width():
                continue
            stacked = is_stacked(layout)
            if not stacked:
                # What one line needs, kept for deciding when to go back.
                if layout in _needs:
                    width = _needs[layout]()
                elif layout.property("reflowNatural"):
                    width = layout.sizeHint().width()
                else:
                    width = layout.minimumSize().width()
                layout.setProperty("reflowNeed", width)
            need = layout.property("reflowNeed") or 0
            narrow = _room(layout) < need + (_SLACK if stacked else 0)
            if narrow != stacked:
                changes.append((layout, narrow))
    return changes


def fit(page: QWidget, *, animate: bool = True, before: dict | None = None) -> None:
    """Stack or unstack every marked layout on ``page`` that needs it.
    ``before``: where things were on screen, from :func:`_snapshot`."""
    if not page.isVisible() or page in _fitting:
        return
    _fitting.add(page)
    try:
        _fit(page, animate, before)
    finally:
        _fitting.discard(page)


def _fit(page: QWidget, animate: bool, before: dict | None) -> None:
    extra = getattr(page, "reflow_extra", None)
    gliding = page in _glides
    # The page's own moves first: ``before`` was noted ahead of them.
    moved = bool(extra()) if extra is not None and before is not None else False
    changes = _pending(page)
    # Plain resize steps are left to Qt, which keeps dragging cheap; only
    # stacking glides, and a resize mid-glide re-aims it rather than
    # letting the layout snap everything to the end for a frame.
    if not (changes or moved or (gliding and before is not None)):
        if extra is not None and before is None:
            extra()
        return
    if not (changes or moved) and animate and gliding:
        _reaim(page, before)
        return
    with animated(page, enabled=animate, before=before):
        # Stacking an outer row gives the rows inside it more room (or
        # less), so look again until nothing changes.
        for _ in range(4):
            for layout, narrow in changes:
                _set_stacked(layout, narrow)
            _settle()
            if extra is not None:
                extra()
            changes = _pending(page)
            if not changes:
                break


def _set_stacked(layout: QLayout, stacked: bool) -> None:
    if isinstance(layout, QGridLayout):
        _set_grid_stacked(layout, stacked)
        return
    layout.setDirection(
        QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
    )
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if widget is not None and widget.objectName() in _DIVIDERS:
            widget.setVisible(not stacked)


def _key(item):
    """What a layout item holds - lasts longer than the item itself."""
    widget, layout = item.widget(), item.layout()
    return widget if widget is not None else layout if layout is not None else item


def _set_grid_stacked(grid: QGridLayout, stacked: bool) -> None:
    if stacked == (grid in _grid_places):
        return
    places = []
    while grid.count():
        index = grid.count() - 1
        places.append((*grid.getItemPosition(index), grid.takeAt(index)))
    places.reverse()
    if not stacked:
        originals, stretches, gaps = _grid_places.pop(grid)
        for gap_row in gaps:
            grid.setRowMinimumHeight(gap_row, 0)
        # By what each item holds: the page may have swapped items while
        # it was stacked, deleting the old ones. New ones keep their place.
        current = {_key(item): (row_, column, rows, columns, item) for row_, column, rows, columns, item in places}
        for key, row_, column, rows, columns in originals:
            if key in current:
                item = current.pop(key)[4]
                grid.addItem(item, row_, column, rows, columns)
                if isinstance(key, QWidget) and key.objectName() in _DIVIDERS:
                    key.show()
        for row_, column, rows, columns, item in current.values():
            grid.addItem(item, row_, column, rows, columns)
        for column, stretch in enumerate(stretches):
            grid.setColumnStretch(column, stretch)
        return
    stretches = [grid.columnStretch(c) for c in range(grid.columnCount())]
    gaps: list[int] = []
    _grid_places[grid] = ([(_key(p[4]), *p[:4]) for p in places], stretches, gaps)
    if grid.objectName() == _LABELLED:
        _stack_labelled(grid, places, gaps)
        return
    dividers = sorted(
        {column for _r, column, _rs, _cs, item in places
         if item.widget() is not None and item.widget().objectName() in _DIVIDERS}
    )

    def across(place) -> bool:  # spans the sides, like a shared console
        return any(place[1] < column < place[1] + place[3] for column in dividers)

    new_stretch: dict[int, int] = {}
    top = width = 0
    for side, start in enumerate([0] + [column + 1 for column in dividers]):
        end = dividers[side] if side < len(dividers) else grid.columnCount()
        mine = [p for p in places if start <= p[1] < end and not across(p)]
        # Columns only spanned by a header (spacing either side of the
        # divider) fold away, so the side's content starts at column 0.
        content = sorted({p[1] for p in mine if p[3] == 1}) or sorted({p[1] for p in mine})
        for row_, column, rows, columns, item in mine:
            spanned = [c for c in content if column <= c < column + columns]
            first = content.index(spanned[0]) if spanned else 0
            grid.addItem(item, top + row_, first, rows, max(1, len(spanned)))
            for c in spanned or [column]:
                new_stretch[first] = max(new_stretch.get(first, 0), stretches[c])
        if mine and top:  # an empty row between this side and the last
            gaps.append(top - 1)
        top += max((r + rs for r, _c, rs, _cs, _i in mine), default=0) + 1
        width = max(width, len(content))
    shared = [p for p in places if across(p)]
    first = min((p[0] for p in shared), default=0)
    for row_, _column, rows, _columns, item in shared:
        grid.addItem(item, top + row_ - first, 0, rows, width)
    for _r, column, _rs, _cs, item in places:
        if column in dividers:
            item.widget().hide()
            grid.addItem(item, 0, column, 1, 1)
    for column in range(grid.columnCount()):
        grid.setColumnStretch(column, new_stretch.get(column, 0))
    for gap_row in gaps:
        grid.setRowMinimumHeight(gap_row, _SIDE_GAP)


def _stack_labelled(grid: QGridLayout, places: list, gaps: list[int]) -> None:
    wrap = grid.property("reflowColumns") or 2
    top = 0
    for source_row in sorted({p[0] for p in places}):
        mine = sorted((p for p in places if p[0] == source_row), key=lambda p: p[1])
        if top:
            gaps.append(top)
            top += 1
        rest = [p for p in mine if p[1] > 0]
        for p in mine:
            if p[1] == 0:
                grid.addItem(p[4], top, 0, 1, wrap)
                top += 1
        for index, p in enumerate(rest):
            grid.addItem(p[4], top + index // wrap, index % wrap, 1, 1)
        top += (len(rest) + wrap - 1) // wrap
    for column in range(grid.columnCount()):
        grid.setColumnStretch(column, 1 if column < wrap else 0)
    for gap_row in gaps:
        grid.setRowMinimumHeight(gap_row, _SIDE_GAP // 2)


def _settle() -> None:
    """Run the layout passes Qt queued, so new places can be read now."""
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)


# -- gliding --------------------------------------------------------------
def _depth(widget: QWidget) -> int:
    depth = 0
    while widget.parentWidget() is not None:
        widget = widget.parentWidget()
        depth += 1
    return depth


def _movers(page: QWidget) -> list[QWidget]:
    """The widgets that can visibly move: the cards and what is in them,
    everything directly on the page's scroll area, and what sits in each
    marked layout's widget. Outer ones first, so inner glides win."""
    found: set[QWidget] = set()
    for card in page.findChildren(QWidget, "card"):
        found.add(card)
        found.update(card.findChildren(QWidget, options=_DIRECT))
    for name in (_ROW, _DIVIDED, _LABELLED):
        for layout in page.findChildren(QLayout, name):
            owner = layout.parentWidget()
            if owner is not None:
                found.update(owner.findChildren(QWidget, options=_DIRECT))
    for area in page.findChildren(QScrollArea):
        if area.widget() is not None:
            found.update(area.widget().findChildren(QWidget, options=_DIRECT))
    return sorted((w for w in found if w.isVisible()), key=_depth)


def _glide_places(glide: QParallelAnimationGroup) -> dict[QWidget, QRect]:
    """Where a glide's widgets are on screen now - cheaper than a full
    snapshot, and all a re-aim needs."""
    places = {}
    for index in range(glide.animationCount()):
        target = glide.animationAt(index).targetObject()
        if isinstance(target, QWidget):
            places[target] = QRect(target.geometry())
    return places


def _snapshot(page: QWidget) -> dict[QWidget, QRect]:
    """Where each mover is now - mid-glide, where it is on screen."""
    return {w: QRect(w.geometry()) for w in _movers(page)}


def _drop_dead(*_args) -> None:
    for glide in list(_glides.values()):
        for index in reversed(range(glide.animationCount())):
            animation = glide.animationAt(index)
            if animation.targetObject() is None:
                glide.removeAnimation(animation)
                animation.deleteLater()


def _reaim(page: QWidget, before: dict) -> None:
    """A resize mid-glide with nothing new stacking: keep the same glide,
    now from where its widgets are on screen to their new places."""
    glide = _glides[page]
    glide.stop()
    _relayout(glide)  # their real new places, nested layouts too
    for index in range(glide.animationCount()):
        animation = glide.animationAt(index)
        target = animation.targetObject()
        if isinstance(target, QWidget):
            animation.setStartValue(before.get(target, target.geometry()))
            animation.setEndValue(target.geometry())
    glide.start()


def _stop(page: QWidget) -> None:
    """End ``page``'s glide where it is; the caller lays out from there."""
    glide = _glides.pop(page, None)
    if glide is not None:
        glide.stop()
        _release(glide)


def _release(glide: QParallelAnimationGroup) -> None:
    """Hand every glided place back to the layouts."""
    _relayout(glide)
    glide.deleteLater()


def _relayout(glide: QParallelAnimationGroup) -> None:
    """Lay out again whatever holds the glide's widgets - nested layouts
    too: a layout only re-places its items when it is marked changed, so
    one cut short mid-glide would keep them half-way."""
    owners = set()
    for index in range(glide.animationCount()):
        target = glide.animationAt(index).targetObject()
        if isinstance(target, QWidget) and target.parentWidget() is not None:
            owners.add(target.parentWidget())
    for owner in owners:
        for layout in owner.findChildren(QLayout):
            if layout.parentWidget() is owner:
                layout.invalidate()
        if owner.layout() is not None:
            owner.layout().invalidate()
            owner.layout().activate()


@contextmanager
def animated(
    page: QWidget,
    layouts=(),
    *,
    enabled: bool = True,
    before: dict | None = None,
):
    """Glide whatever the ``with`` block moves on ``page`` to its new place.
    ``layouts`` is kept for callers; every mover on the page is watched.
    ``before``: where things were, if the block isn't the first change."""
    if not enabled or not gui_settings.animations_enabled():
        _stop(page)
        yield
        _settle()
        return
    if before is None:
        before = _snapshot(page)
    _stop(page)
    yield
    _settle()
    glide = QParallelAnimationGroup(page)
    for widget in _movers(page):
        start = before.get(widget)
        end = widget.geometry()
        if start is None or start == end or not start.isValid():
            continue
        animation = QPropertyAnimation(widget, b"geometry", glide)
        animation.setDuration(GLIDE_MS)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.setStartValue(start)
        animation.setEndValue(end)
        glide.addAnimation(animation)
        # A page that rebuilds a card mid-glide deletes what was gliding:
        # take its glide out first. Connected once per widget.
        if not widget.property("reflowWatched"):
            widget.setProperty("reflowWatched", True)
            widget.destroyed.connect(_drop_dead)
    if not glide.animationCount():
        glide.deleteLater()
        return
    _glides[page] = glide

    def done() -> None:
        if _glides.get(page) is glide:
            del _glides[page]
            _release(glide)
            # Anything that changed size while it glided.
            QTimer.singleShot(0, lambda: fit(page, animate=False))

    glide.finished.connect(done)
    glide.start()
