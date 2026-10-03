"""Deck-sized building blocks.

Every size here is a deliberate number, not a guess. The Steam Deck panel is
1280x800 at about 204 PPI, so one pixel is roughly 0.125mm, and it is held
around 40cm away. That means a 72px row is ~9mm tall and 18px text subtends
roughly 0.3 degrees - comfortably legible, and close to what 11-12pt looks
like on a 96 DPI monitor at desk distance. Anything smaller starts failing
one of the two tests that matter here: can a thumb hit it, and can you read
it without leaning in.

The constants below are *design* pixels at the Deck's own size. Anything
that turns one into a real widget size goes through :func:`Steamdeck.scale.px`,
so on a bigger window (a monitor, a TV) the whole interface grows together.

The desktop UI's shared widgets in ``commander_gui.ui.common`` are reused
wherever they carry no geometry (CommandRunner, BackgroundTask,
mo2_running). The ones that do - ProgressArea with its fixed 100x32 buttons,
InstallStatusRow, make_card, section_label - are replaced here rather than
restyled, because their sizes are baked into Python, not the stylesheet.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import (
    QEasingCurve,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from commander_gui.i18n import tr
from commander_gui.themes import active_theme_tokens

from .scale import px

# --------------------------------------------------------------- geometry
#: The Deck's native resolution. Device pixel ratio is 1, so these are real
#: pixels and the layout can be budgeted exactly.
DECK_W, DECK_H = 1280, 800

#: Kept slim on purpose: every pixel of chrome is a pixel the screens don't
#: get, and on an 800px-tall panel the bars used to take a quarter of it.
HEADER_H = 48
NAV_H = 60
#: The strip under the nav bar: profile on the left, button prompts and the
#: clock on the right.
FOOTER_H = 30
MARGIN_X = 28
MARGIN_Y = 20

#: 800 - 48 header - 60 nav - 30 footer = 662 of content height.
CONTENT_H = DECK_H - HEADER_H - NAV_H - FOOTER_H

ROW_H = 72
ROW_GAP = 12
PRIMARY_H = 96
BUTTON_H = 64
#: The Play screen's one big button.
HERO_H = 168

#: The on/off switch DeckToggleRow paints at its right-hand end.
_SWITCH_W, _SWITCH_H = 64, 34

#: Hard floor for anything interactive, enforced by tests/test_steamdeck.py.
#: Below this a thumb starts missing targets on a 7.4" screen.
MIN_TOUCH = 48

#: Width of the accent bar DeckRow paints when focused. The QSS ring alone is
#: 3px, about 0.4mm here - visible, but not from arm's length on the
#: lower-contrast themes.
FOCUS_BAR_W = 6

#: How many rows a picker shows before it scrolls. Six fills an overlay on
#: the Deck's panel; any list up to that length is shown whole.
PICKER_ROWS = 6


def _token(name: str, fallback: str = "#888888") -> QColor:
    """A QColor from the active theme's palette."""
    return QColor(active_theme_tokens().get(name, fallback))


# --------------------------------------------------------------- backdrop
class DeckBackdrop(QWidget):
    """The painted background behind every Deck screen.

    The same treatment the desktop window uses - a diagonal base gradient
    with two soft radial glows over it - drawn from the same theme tokens,
    so Deck Mode looks like the rest of COMMANDER rather than a generic Qt
    application that happens to share its colours.

    Painted rather than styled: a QSS background on this widget would sit
    on top of the glow. Everything stacked above it is semi-transparent, so
    the glow reads through the cards and rows.
    """

    def sizeHint(self) -> QSize:
        return QSize(DECK_W, DECK_H)

    def _rgb(self, tokens: dict[str, str], key: str) -> tuple[int, int, int]:
        red, green, blue = (part.strip() for part in tokens[key].split(","))
        return int(red), int(green), int(blue)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect()
            tokens = active_theme_tokens()

            base = QLinearGradient(0, 0, rect.width(), rect.height())
            base.setColorAt(0.0, QColor(tokens["back_base_a"]))
            base.setColorAt(1.0, QColor(tokens["back_base_b"]))
            painter.fillRect(rect, base)

            radius = max(rect.width(), rect.height())
            top_left = QRadialGradient(
                rect.width() * 0.18, rect.height() * 0.08, radius * 0.95
            )
            for stop, key, alpha in (
                (0.0, "back_glow1_rgb", "back_glow1_a"),
                (0.35, "back_glow1b_rgb", "back_glow1b_a"),
                (0.7, "back_glow1c_rgb", "back_glow1c_a"),
            ):
                red, green, blue = self._rgb(tokens, key)
                top_left.setColorAt(
                    stop, QColor(red, green, blue, int(tokens[alpha]))
                )
            top_left.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.fillRect(rect, top_left)

            red, green, blue = self._rgb(tokens, "back_glow2_rgb")
            bottom_right = QRadialGradient(
                rect.width() * 0.95, rect.height() * 0.96, radius * 0.7
            )
            bottom_right.setColorAt(
                0.0, QColor(red, green, blue, int(tokens["back_glow2_a"]))
            )
            bottom_right.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.fillRect(rect, bottom_right)
        finally:
            painter.end()


# ----------------------------------------------------------------- labels
def deck_label(text: str, *, role: str = "body", wrap: bool = False) -> QLabel:
    """A label styled by role: title, body, caption, rowTitle, rowValue."""
    label = QLabel(text)
    label.setObjectName(
        {
            "title": "deckTitle",
            "body": "deckBody",
            "caption": "deckCaption",
            "rowTitle": "deckRowTitle",
            "rowValue": "deckRowValue",
            "headerInfo": "deckHeaderInfo",
            "modCounter": "deckModCounter",
            "hint": "deckHint",
            "wordmark": "deckWordmark",
            "byline": "deckByline",
            "section": "deckSection",
            "statValue": "deckStatValue",
        }.get(role, "deckBody")
    )
    label.setWordWrap(wrap)
    return label


def deck_step_badge(number: int) -> QLabel:
    """A small numbered accent pill, e.g. the "1" before "Install Anomaly"."""
    badge = QLabel(str(number))
    badge.setObjectName("deckStepBadge")
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return badge


def deck_chip(text: str, state: str = "ok") -> QLabel:
    """A small status pill: ``ok``, ``warn`` or ``bad``."""
    chip = QLabel(text)
    chip.setObjectName(
        {"ok": "deckChipOk", "warn": "deckChipWarn", "bad": "deckChipBad"}.get(
            state, "deckChipWarn"
        )
    )
    chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return chip


def repolish(widget: QWidget) -> None:
    """Re-apply the stylesheet after an object-name/property change."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# ---------------------------------------------------------------- buttons
def deck_button(
    text: str,
    *,
    role: str = "normal",
    on_click: Callable[[], None] | None = None,
) -> QPushButton:
    """A touch-sized button. Roles: normal, primary, hero, danger, chip, step."""
    button = QPushButton(text)
    object_name, height = {
        "primary": ("deckPrimary", PRIMARY_H),
        "hero": ("deckHero", HERO_H),
        "danger": ("deckDanger", PRIMARY_H),
        "chip": ("deckChip", MIN_TOUCH),
        "step": ("deckStep", BUTTON_H),
        "normal": ("", BUTTON_H),
    }.get(role, ("", BUTTON_H))
    if object_name:
        button.setObjectName(object_name)
    button.setMinimumHeight(px(height))
    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click is not None:
        button.clicked.connect(lambda: on_click())
    return button


# ------------------------------------------------------------------ cards
class DeckCard(QFrame):
    """A bordered panel. The Deck equivalent of ``common.make_card``."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deckCard")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(px(20), px(16), px(20), px(16))
        self.body.setSpacing(px(12))


def deck_divider() -> QFrame:
    divider = QFrame()
    divider.setObjectName("deckDivider")
    divider.setFrameShape(QFrame.Shape.NoFrame)
    divider.setFixedWidth(1)
    return divider


def deck_divider_v(height: int = 26) -> QFrame:
    """A short vertical rule for separating items on one line."""
    divider = deck_divider()
    divider.setFixedHeight(px(height))
    return divider


def side_by_side(*widgets: QWidget, spacing: int = ROW_GAP) -> QHBoxLayout:
    """Equal-width columns - two cards, two rows - sharing one line."""
    row = QHBoxLayout()
    row.setSpacing(px(spacing))
    for widget in widgets:
        row.addWidget(widget, 1)
    return row


# ------------------------------------------------------------------- rows
class DeckRow(QWidget):
    """A focusable 72px row: title on the left, value and chevron on the right.

    Clicking or activating it emits :attr:`activated`. Used for every
    "tap to choose" control in Deck Mode, in place of a QComboBox - a combo's
    popup is a separate top-level window, which gamescope has no window
    manager to place or size.
    """

    activated = Signal()

    def __init__(
        self,
        title: str,
        value: str = "",
        *,
        chevron: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("deckRow")
        # A plain QWidget ignores stylesheet backgrounds unless it is told to
        # style itself, and cannot take focus unless asked - both required for
        # the focus ring and the D-pad to reach this row at all.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(px(ROW_H))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(20 + FOCUS_BAR_W), 0, px(20), 0)
        layout.setSpacing(px(16))
        self.title_label = deck_label(title, role="rowTitle")
        self.value_label = deck_label(value, role="rowValue")
        self.value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self.title_label)
        layout.addStretch(1)
        layout.addWidget(self.value_label)
        if chevron:
            layout.addWidget(deck_label("›", role="rowValue"))

    def set_value(self, text: str) -> None:
        self.value_label.setText(text)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.hasFocus():
            return
        painter = QPainter(self)
        try:
            painter.fillRect(
                QRect(0, px(4), px(FOCUS_BAR_W), self.height() - px(8)),
                _token("focus"),
            )
        finally:
            painter.end()

    def mouseReleaseEvent(self, event) -> None:
        if (
            self.focusPolicy() != Qt.FocusPolicy.NoFocus
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.activated.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        # A (Enter) and Y (Space) in the Deck's default desktop layout.
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.activated.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class DeckToggleRow(DeckRow):
    """A row that flips an on/off state, drawn as an animated switch."""

    toggled = Signal(bool)

    #: Knob travel time. Long enough to read as motion, short enough that a
    #: second press never waits on the first.
    ANIMATION_MS = 150

    def __init__(
        self,
        title: str,
        checked: bool = False,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, "", chevron=False, parent=parent)
        self._checked = bool(checked)
        #: 0.0 = off, 1.0 = on; animated between the two.
        self._position = 1.0 if self._checked else 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(self.ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._on_frame)
        # Room on the right for the painted switch; the On/Off text sits
        # just left of it.
        margins = self.layout().contentsMargins()
        self.layout().setContentsMargins(
            margins.left(), 0, margins.right() + px(_SWITCH_W + 16), 0
        )
        self._render()
        self.activated.connect(self._flip)

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool, *, notify: bool = False) -> None:
        checked = bool(checked)
        changed = checked != self._checked
        self._checked = checked
        target = 1.0 if checked else 0.0
        self._animation.stop()
        if changed and self.isVisible():
            self._animation.setStartValue(self._position)
            self._animation.setEndValue(target)
            self._animation.start()
        else:
            self._position = target
        self._render()
        if notify:
            self.toggled.emit(self._checked)

    def _on_frame(self, value) -> None:
        self._position = float(value)
        self.update()

    def _flip(self) -> None:
        self.set_checked(not self._checked, notify=True)

    def _render(self) -> None:
        self.set_value(tr("On") if self._checked else tr("Off"))
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            width, height = px(_SWITCH_W), px(_SWITCH_H)
            track = QRectF(
                self.width() - px(20) - width,
                (self.height() - height) / 2,
                width,
                height,
            )
            t = self._position if self.isEnabled() else 0.0
            off = _token("border_strong", "#444444")
            on = _token("accent")
            colour = QColor(
                round(off.red() + (on.red() - off.red()) * t),
                round(off.green() + (on.green() - off.green()) * t),
                round(off.blue() + (on.blue() - off.blue()) * t),
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(track, height / 2, height / 2)
            inset = px(4)
            knob = height - 2 * inset
            left = track.left() + inset
            right = track.right() - knob - inset
            x = left + (right - left) * self._position
            painter.setBrush(
                _token("accent_text", "#111111") if t > 0.5 else _token("text_dim", "#dddddd")
            )
            painter.drawEllipse(QRectF(x, track.top() + inset, knob, knob))
        finally:
            painter.end()


class DeckStatusRow(DeckRow):
    """A row carrying a status chip instead of a value label.

    Static by default: a status card is something to read, not to press, so
    it takes no focus, shows no hover and keeps the arrow cursor. Pass
    ``interactive=True`` (the System screen does) to make a row with
    :meth:`set_detail` text a focus stop that opens that detail on A - the
    Deck has no hover, so a tooltip would be text nobody ever sees.
    """

    def __init__(
        self,
        title: str,
        *,
        interactive: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, "", chevron=False, parent=parent)
        self._title = title
        self._detail = ""
        self._interactive = interactive
        self.setObjectName("deckStatusRow")
        self._chip = deck_chip("", "warn")
        # Vertically centred at its own pill height, not stretched to the
        # full row - a row-tall chip read as a second box jammed against
        # the row's right edge.
        self.layout().addWidget(self._chip, 0, Qt.AlignmentFlag.AlignVCenter)
        self.activated.connect(self._show_detail)
        self.set_detail("")
        self.set_status(tr("Checking..."), "warn")

    def set_detail(self, text: str) -> None:
        """Extra text, opened with A on an interactive row."""
        self._detail = (text or "").strip()
        active = self._interactive and bool(self._detail)
        self.setObjectName("deckRow" if active else "deckStatusRow")
        self.setFocusPolicy(
            Qt.FocusPolicy.StrongFocus if active else Qt.FocusPolicy.NoFocus
        )
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if active else Qt.CursorShape.ArrowCursor
        )
        repolish(self)

    def detail(self) -> str:
        return self._detail

    def _show_detail(self) -> None:
        if self._interactive and self._detail:
            show_detail(self.window(), self._title, self._detail)

    def set_status(self, text: str, state: str = "ok") -> None:
        # A leading dot in the chip's own colour: state reads at a glance
        # even on themes where the text colours sit close together.
        self._chip.setText(f"●  {text}")
        self._chip.setObjectName(
            {"ok": "deckChipOk", "warn": "deckChipWarn", "bad": "deckChipBad"}.get(
                state, "deckChipWarn"
            )
        )
        repolish(self._chip)


class _ElidedLabel(QLabel):
    """A one-line label that ends in "…" when it doesn't fit.

    A plain QLabel's minimum width is its whole text, so in a narrow row it
    either pushes its neighbours out or gets clipped mid-letter. This one
    can shrink to nothing and draws as much of the text as fits, in the
    colour and font the stylesheet gave it.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        #: Shorter wordings tried, in order, before cutting with "…".
        self.alternatives: list[str] = []

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        try:
            painter.setFont(self.font())
            painter.setPen(self.palette().color(self.foregroundRole()))
            width = self.contentsRect().width()
            metrics = self.fontMetrics()
            text = next(
                (
                    candidate
                    for candidate in (self.text(), *self.alternatives)
                    if metrics.horizontalAdvance(candidate) <= width
                ),
                None,
            )
            if text is None:
                text = metrics.elidedText(
                    (self.alternatives or [self.text()])[-1], Qt.TextElideMode.ElideRight, width
                )
            painter.drawText(self.contentsRect(), int(self.alignment()), text)
        finally:
            painter.end()


class DeckStatusTile(DeckStatusRow):
    """A status as a normal-height row, for three side by side.

    Same shape as every other row on the Dashboard (title left, status chip
    right, ``ROW_H`` tall), so Anomaly | GAMMA | Dependencies line up with
    Updates, Mods and Storage instead of sitting in a taller card of their
    own. At a third of the screen the title is what gives way - it ends in
    "…" - and the chip is never cut. Always read-only.
    """

    def __init__(
        self, title: str, short_title: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(title, parent=parent)
        layout = self.layout()
        old = self.title_label
        self.title_label = _ElidedLabel(title)
        if short_title:
            # e.g. "STALKER Anomaly" -> "Anomaly" at large text sizes,
            # before resorting to "…".
            self.title_label.alternatives = [short_title]
        self.title_label.setObjectName(old.objectName())
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.title_label.setToolTip(title)
        layout.replaceWidget(old, self.title_label)
        old.deleteLater()
        # The title fills the row up to the chip; the value label and the
        # stretch a normal row keeps between them would only squeeze it.
        layout.setStretchFactor(self.title_label, 1)
        self.value_label.hide()
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item is not None and item.spacerItem() is not None:
                layout.removeItem(item)
                break
        self._chip.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        # set_detail() below is a no-op here, so the "read-only" state that
        # DeckStatusRow.set_detail("") normally applies is set directly.
        self.setObjectName("deckStatusRow")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        # Same insets as every other row, so the titles line up down the page.
        layout.setContentsMargins(px(20 + FOCUS_BAR_W), 0, px(20), 0)
        layout.setSpacing(px(10))

    def set_detail(self, _text: str) -> None:
        """Read-only tile: details are shown as captions beside it instead."""


# ------------------------------------------------------------- segmented
class DeckSegmented(QWidget):
    """A handful of mutually exclusive choices shown inline as chips.

    For settings with two to four options, where opening a picker overlay
    to choose between them is a detour.
    """

    changed = Signal(object)

    def __init__(
        self,
        options: Sequence[tuple[str, object]],
        current: object = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(8))
        self._buttons: dict[object, QPushButton] = {}
        for label, value in options:
            button = deck_button(label, role="chip")
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, v=value: self._choose(v))
            layout.addWidget(button, 1)
            self._buttons[value] = button
        self.set_value(current)

    def value(self) -> object:
        return next((v for v, b in self._buttons.items() if b.isChecked()), None)

    def set_value(self, value: object) -> None:
        for key, button in self._buttons.items():
            button.setChecked(key == value)

    def button(self, value: object) -> QPushButton | None:
        return self._buttons.get(value)

    def _choose(self, value: object) -> None:
        self.set_value(value)
        self.changed.emit(value)


# ---------------------------------------------------------------- pickers
class DeckPicker(QWidget):
    """A list of choices, used everywhere a combo box would be.

    Shown inside a :class:`DeckOverlay` rather than as a popup, so it obeys
    the window's own geometry under gamescope and stays inside the focus
    controller's reach. Sized to its content: up to ``PICKER_ROWS`` options
    are all visible with no scrolling, so a three-way choice is never a
    list you have to scroll through.
    """

    chosen = Signal(object)

    def __init__(
        self,
        title: str,
        options: Sequence[tuple[str, object]],
        current: object = None,
        *,
        searchable: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(12))
        # Omitted when the picker is hosted in an overlay, which draws its
        # own heading - two identical titles stacked reads as a bug.
        if title:
            layout.addWidget(deck_label(title, role="title"))

        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.list.setUniformItemSizes(True)
        self.list.setMouseTracking(True)
        self.list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        # itemClicked only. itemActivated also fires on a single click under
        # some styles, which made every pick happen twice (and a folder
        # browser's "up" entry climb two levels). The A button reaches the
        # same slot through focus.activate(), which emits clicked().
        self.list.itemClicked.connect(self._emit)
        self._all_options: list[tuple[str, object]] = []
        self.search: QLineEdit | None = None
        if searchable:
            self.search = QLineEdit()
            self.search.setPlaceholderText(tr("Type to filter..."))
            self.search.textChanged.connect(self._apply_filter)
            layout.addWidget(self.search)
        layout.addWidget(self.list, 1)
        from .focus import enable_kinetic_scroll

        enable_kinetic_scroll(self.list)
        self.set_options(options, current)

    def set_options(
        self, options: Sequence[tuple[str, object]], current: object = None
    ) -> None:
        """Reload the list in place - e.g. a folder browser navigating.

        ``current`` is selected if present, so a browser going up a level
        can land on the folder it just came out of.
        """
        self._all_options = list(options)
        if self.search is not None and self.search.text():
            needle = self.search.text().strip().lower()
            options = [o for o in options if needle in str(o[0]).lower()]
        self.list.clear()
        for index, (label, value) in enumerate(options):
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setSizeHint(QSize(0, px(ROW_H)))
            self.list.addItem(item)
            if current is not None and value == current:
                self.list.setCurrentRow(index)
        if self.list.currentRow() < 0 and options:
            self.list.setCurrentRow(0)
        # Height follows the full option count (not the filtered one), so
        # typing in the filter box doesn't make the panel jump around.
        rows = max(1, min(len(self._all_options), PICKER_ROWS))
        frame = self.list.frameWidth() * 2 + px(10)
        height = rows * px(ROW_H) + frame
        self.list.setMinimumHeight(height)
        # A list that fits entirely is exactly that tall - no empty band
        # under a two-option choice. Longer lists may grow into the panel.
        fits = len(self._all_options) <= PICKER_ROWS
        self.list.setMaximumHeight(height if fits else 16777215)

    def _apply_filter(self, _text: str) -> None:
        self.set_options(self._all_options)

    def _emit(self, item: QListWidgetItem) -> None:
        self.chosen.emit(item.data(Qt.ItemDataRole.UserRole))


# --------------------------------------------------------------- overlays
class DeckOverlay(QWidget):
    """A modal panel drawn *inside* the window.

    Not a QDialog on purpose. A QDialog is a separate top-level window, and
    in Steam's Game Mode there is no window manager to place, size or focus
    one - they routinely come up at the wrong size or swallow input. A child
    widget filling the central widget cannot go wrong, and the focus
    controller can restrict D-pad traversal to its subtree so nothing behind
    the scrim is reachable.

    Buttons are laid out in the order given - the action first (left), then
    Cancel/Close (right) - all the same height and width. ``default_index``
    picks which one has focus when the overlay opens (the first by default).

    ``header_buttons`` are compact buttons in the title row, right-aligned -
    for actions that belong to the panel as a whole (Edit / New on the
    profile switcher, the Welcome screen's close cross). ``translucent``
    lets the screen behind show through a little: a lighter scrim and a
    panel drawn in the theme's card colour at partial opacity.
    """

    #: Emitted when the overlay is taken down - by its own buttons, B, or
    #: another overlay replacing it - so a flow waiting on it can clean up.
    closed = Signal()

    def __init__(
        self,
        title: str,
        body: QWidget,
        buttons: Sequence[tuple[str, Callable[[], None], str]] = (),
        *,
        parent: QWidget | None = None,
        panel_width: int = 900,
        default_index: int = 0,
        header_buttons: Sequence[tuple[str, Callable[[], None], str]] = (),
        translucent: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("deckOverlay")
        self.setAutoFillBackground(False)
        self._scrim_alpha = 110 if translucent else 170

        outer = QVBoxLayout(self)
        outer.setContentsMargins(px(MARGIN_X), px(MARGIN_Y), px(MARGIN_X), px(MARGIN_Y))
        outer.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        panel = QFrame()
        panel.setObjectName(
            "deckOverlayPanelGlass" if translucent else "deckOverlayPanel"
        )
        panel.setMaximumWidth(px(panel_width))
        # The side stretches would otherwise split the width three ways and
        # squeeze every panel to a third of the screen. Capped so a window
        # smaller than the Deck still fits it.
        panel.setMinimumWidth(min(px(panel_width), DECK_W - 4 * MARGIN_X))
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(px(28), px(24), px(28), px(24))
        panel_layout.setSpacing(px(16))
        heading = deck_label(title, role="body")
        heading.setObjectName("deckOverlayTitle")
        self.header_buttons: list[QPushButton] = []
        if header_buttons:
            title_row = QHBoxLayout()
            title_row.setSpacing(px(10))
            title_row.addWidget(heading, 1)
            for label, callback, role in header_buttons:
                button = deck_button(label, role=role)
                button.setProperty("overlayHeaderButton", True)
                button.setFixedHeight(px(MIN_TOUCH))
                button.setMinimumWidth(px(MIN_TOUCH))
                button.clicked.connect(lambda _=False, cb=callback: cb())
                title_row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
                self.header_buttons.append(button)
            panel_layout.addLayout(title_row)
        else:
            panel_layout.addWidget(heading)
        panel_layout.addWidget(body, 1)
        self.panel = panel

        self.default_button: QPushButton | None = None
        self.buttons: list[QPushButton] = []
        #: Optional footer prompts while this overlay is up, as
        #: (button, English label) pairs; None means "A Select, B Close".
        self.hints: Sequence[tuple[str, str]] | None = None
        #: Optional ``handler(action) -> bool`` for face buttons other than
        #: A/B - the on-screen keyboard maps X/Y/Start this way.
        self.action_handler: Callable[[str], bool] | None = None
        #: Optional ``handler() -> bool`` for B, tried before the overlay
        #: closes: True means it went back a level (up a folder, a
        #: previous installer step) and the overlay stays.
        self.back_handler: Callable[[], bool] | None = None
        #: Optional scroll area the right stick scrolls when focus is not
        #: in one itself - a description panel beside a list of options.
        self.scroll_target: QScrollArea | None = None
        if buttons:
            button_row = QHBoxLayout()
            button_row.setSpacing(px(12))
            for label, callback, role in buttons:
                button = deck_button(label, role=role)
                # One height for every button in the row, whatever its role:
                # a primary Install beside a normal Cancel used to be half as
                # tall again, which read as two unrelated controls.
                button.setProperty("overlayButton", True)
                button.setFixedHeight(px(BUTTON_H))
                button.clicked.connect(lambda _=False, cb=callback: cb())
                button_row.addWidget(button, 1)
                self.buttons.append(button)
            panel_layout.addLayout(button_row)
            index = default_index if 0 <= default_index < len(self.buttons) else 0
            self.default_button = self.buttons[index]
        row.addWidget(panel, 1)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(0, 0, 0, self._scrim_alpha))
        finally:
            painter.end()
        super().paintEvent(event)


def confirm_overlay(
    title: str,
    message: str,
    *,
    on_confirm: Callable[[], None],
    on_cancel: Callable[[], None],
    confirm_text: str | None = None,
    cancel_text: str | None = None,
    confirm_role: str = "primary",
) -> DeckOverlay:
    """A two-button question: the action on the left, Cancel on the right.

    Cancel takes the default focus deliberately: the confirmations Deck Mode
    raises guard destructive or long-running actions, and A is the easiest
    button on the device to press by accident.
    """
    body = deck_label(message, role="body", wrap=True)
    return DeckOverlay(
        title,
        body,
        [
            (confirm_text or tr("Continue"), on_confirm, confirm_role),
            (cancel_text or tr("Cancel"), on_cancel, "normal"),
        ],
        default_index=1,
    )


def picker_overlay(
    window, title: str, picker: DeckPicker, *, panel_width: int = 1000
) -> DeckOverlay:
    """The standard host for a :class:`DeckPicker`: the list and a Cancel.

    Opens with focus on the list, on the current choice - the reason the
    picker was opened. Cancel is still there below it, and B does the same.
    A searchable picker's filter opens the keyboard on Y.
    """
    overlay = DeckOverlay(
        title,
        picker,
        [(tr("Cancel"), window.dismiss_overlay, "normal")],
        panel_width=panel_width,
    )
    overlay.default_button = picker.list
    hints = [("A", "Choose"), ("B", "Cancel")]
    if picker.search is not None:
        search = picker.search
        hints.insert(1, ("Y", "Search"))

        def _on_action(action: str) -> bool:
            from . import gamepad as pad

            if action == pad.SEARCH and callable(getattr(window, "open_keyboard", None)):
                window.open_keyboard(search)
                return True
            return False

        overlay.action_handler = _on_action
    overlay.hints = hints
    return overlay


class DeckToast(QLabel):
    """A transient message floating over the content."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deckToast")
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, msecs: int = 3000) -> None:
        self.setText(text)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        if msecs > 0:
            self._timer.start(msecs)
        else:
            self._timer.stop()

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        width = min(parent.width() - 2 * px(MARGIN_X), px(900))
        self.setFixedWidth(max(px(240), width))
        self.adjustSize()
        # Above the nav bar, not on it: a toast covering the tabs hides the
        # one thing that says where you are.
        self.move(
            (parent.width() - self.width()) // 2,
            parent.height() - self.height() - px(NAV_H + FOOTER_H + MARGIN_Y),
        )


# --------------------------------------------------------------- progress
class DeckProgress(QWidget):
    """Progress for a running CLI job, sized for the Deck.

    Takes the same inputs as ``commander_gui.ui.common.ProgressArea`` -
    ``on_line``, ``on_started``, ``on_finished``, ``on_cancelled``,
    ``set_runner``, ``status_message`` - so call sites read the same as the
    desktop pages'. What differs is the presentation: a fat bar, one status
    line, a short log tail and two touch-sized controls.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner = None
        self._cancel_fn: Callable[[], None] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(12))

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        self.bar.setTextVisible(True)
        layout.addWidget(self.bar)

        self.status = deck_label("", role="body", wrap=True)
        layout.addWidget(self.status)

        self.tail = deck_label("", role="caption", wrap=False)
        self.tail.setObjectName("deckCaption")
        self.tail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.tail)

        buttons = QHBoxLayout()
        buttons.setSpacing(px(12))
        self.pause_button = deck_button(tr("Pause"), on_click=self._toggle_pause)
        self.cancel_button = deck_button(tr("Cancel"), on_click=self._cancel)
        buttons.addWidget(self.pause_button, 1)
        buttons.addWidget(self.cancel_button, 1)
        layout.addLayout(buttons)

        self._paused = False

    # -- runner wiring ----------------------------------------------------
    def set_runner(self, runner) -> None:
        self._runner = runner
        self._cancel_fn = None
        self.pause_button.show()
        enabled = runner is not None
        self.pause_button.setEnabled(enabled)
        self.cancel_button.setEnabled(enabled)

    def set_cancellable(self, cancel: Callable[[], None] | None) -> None:
        """Drive a plain StreamTask/BackgroundTask job: Cancel only, no Pause."""
        self._runner = None
        self._cancel_fn = cancel
        self.pause_button.hide()
        self.cancel_button.setEnabled(cancel is not None)

    def reset(self) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.status.setText("")
        self.tail.setText("")
        self._paused = False
        self.pause_button.setText(tr("Pause"))

    def is_paused(self) -> bool:
        return self._paused

    def _toggle_pause(self) -> None:
        if self._runner is None:
            return
        if self._paused:
            self._runner.resume()
            self._paused = False
            self.pause_button.setText(tr("Pause"))
        else:
            self._runner.pause()
            self._paused = True
            self.pause_button.setText(tr("Resume"))

    def focus_controls(self) -> None:
        """Put focus on Cancel and scroll the progress block into view.

        Starting a job hides the button that started it, and a hidden widget
        cannot keep focus - left alone focus falls back to the first control
        on the screen and the progress bar sits below the fold.
        """
        from .focus import ensure_visible

        if self.cancel_button.isEnabled():
            self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)
        ensure_visible(self.cancel_button)

    def _cancel(self) -> None:
        # One press of A on a focused Cancel would otherwise throw away
        # hours of a 150 GB download.
        window = self.window()
        confirm = getattr(window, "confirm", None)
        if callable(confirm):
            confirm(
                tr("Cancel"),
                tr("Stop the running task? Progress so far may be lost."),
                self._do_cancel,
                confirm_text=tr("Stop"),
                confirm_role="danger",
            )
            return
        self._do_cancel()

    def _do_cancel(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            self.status.setText(tr("Cancelling..."))
        elif self._cancel_fn is not None:
            self._cancel_fn()
            self.status.setText(tr("Cancelling..."))

    # -- slots ------------------------------------------------------------
    def status_message(self, text: str) -> None:
        self.status.setText(text)

    def set_percent(self, value: int) -> None:
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 100)
        self.bar.setValue(max(0, min(100, int(value))))

    def set_indeterminate(self) -> None:
        self.bar.setRange(0, 0)

    def on_started(self) -> None:
        self.reset()
        self.set_indeterminate()
        self.status.setText(tr("Starting..."))

    def on_line(self, line: str) -> None:
        text = line.strip()
        if text:
            self.tail.setText(text[:120])

    def on_finished(self, rc: int, _output: str = "") -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(100 if rc == 0 else self.bar.value())
        self._paused = False
        self.pause_button.setText(tr("Pause"))
        self.set_runner(None)

    def on_cancelled(self) -> None:
        self.bar.setRange(0, 100)
        self.status.setText(tr("Cancelled"))
        self.set_runner(None)


# ------------------------------------------------------------ hint bar
class DeckHintBar(QWidget):
    """A row of "button + what it does" prompts, like Steam's own footer."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deckHintBar")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(px(10))
        self._hints: list[tuple[str, str]] = []
        self._actions: dict[str, Callable[[], None]] = {}

    def hints(self) -> list[tuple[str, str]]:
        return list(self._hints)

    def set_hints(
        self,
        hints: Sequence[tuple[str, str]],
        *,
        actions: dict[str, Callable[[], None]] | None = None,
    ) -> None:
        """``actions`` maps a button to what tapping its prompt does."""
        self._actions = dict(actions or {})
        if list(hints) == self._hints:
            return
        self._hints = list(hints)
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Hidden now, deleted later - otherwise the old prompts stay
                # painted under the new ones until the deferred delete runs.
                widget.hide()
                widget.deleteLater()
        centre = Qt.AlignmentFlag.AlignVCenter
        for button, label in self._hints:
            pill = _HintLabel(button, self, button)
            pill.setObjectName("deckHintButton")
            pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._layout.addWidget(pill, 0, centre)
            text = _HintLabel(label, self, button)
            text.setObjectName("deckHint")
            self._layout.addWidget(text, 0, centre)
            self._layout.addSpacing(px(4))

    def activate(self, button: str) -> bool:
        action = getattr(self, "_actions", {}).get(button)
        if action is None:
            return False
        action()
        return True


class _HintLabel(QLabel):
    """A footer prompt; tapping it runs its button's action, if it has one."""

    def __init__(self, text: str, bar: DeckHintBar, button: str) -> None:
        super().__init__(text)
        self._bar = bar
        self._button = button

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._bar.activate(self._button):
            event.accept()
            return
        super().mouseReleaseEvent(event)


# ------------------------------------------------------------- details
def show_detail(window, title: str, text: str) -> None:
    """Show ``text`` in a dismissable overlay on ``window``."""
    show = getattr(window, "show_overlay", None)
    dismiss = getattr(window, "dismiss_overlay", None)
    if not callable(show) or not callable(dismiss):
        return
    body = DeckTextPanel(text, min_height=160, max_height=440)
    show(DeckOverlay(title, body, [(tr("OK"), dismiss, "primary")]))


class DeckSmoothScrollArea(QScrollArea):
    """A scroll area that glides instead of jumping.

    D-pad moves (through ``focus.ensure_visible``, which calls
    :meth:`smooth_to`) and mouse-wheel / trackpad notches animate the
    scroll position over a short ease-out, so a long list reads as one
    surface moving rather than a series of jumps. Touch flicks keep using
    the kinetic scroller.
    """

    #: Duration of one glide. Short enough that holding the D-pad down
    #: still keeps up; each new move restarts from wherever it is.
    GLIDE_MS = 220

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._glide = QVariantAnimation(self)
        self._glide.setDuration(self.GLIDE_MS)
        self._glide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._glide.valueChanged.connect(
            lambda value: self.verticalScrollBar().setValue(int(value))
        )
        #: Where the current glide is heading - wheel notches add to this,
        #: not to the half-way position, so fast scrolling stays in step.
        self._target: int | None = None

    def smooth_to(self, value: int) -> None:
        bar = self.verticalScrollBar()
        value = max(bar.minimum(), min(bar.maximum(), int(value)))
        self._glide.stop()
        self._target = value
        if self.GLIDE_MS <= 0:
            bar.setValue(value)
            return
        self._glide.setDuration(self.GLIDE_MS)
        self._glide.setStartValue(bar.value())
        self._glide.setEndValue(value)
        self._glide.start()

    def finish_glide(self) -> None:
        """Jump to the end of a glide in progress.

        The focus controller calls this before working out where a D-pad
        press goes: mid-glide, every widget in here is still sliding, and
        picking a neighbour from half-way positions can pick the wrong one.
        """
        if self._glide.state() == QVariantAnimation.State.Running and self._target is not None:
            self._glide.stop()
            self.verticalScrollBar().setValue(self._target)

    def scroll_position(self) -> int:
        """Where the view is heading: the glide's target while one runs."""
        if self._target is not None and self._glide.state() == QVariantAnimation.State.Running:
            return self._target
        return self.verticalScrollBar().value()

    def scroll_by(self, delta: int) -> None:
        self.smooth_to(self.scroll_position() + delta)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if not delta:
            super().wheelEvent(event)
            return
        # One notch (120) moves about a row.
        self.scroll_by(-int(delta / 120 * px(ROW_H)))
        event.accept()


class DeckTextPanel(DeckSmoothScrollArea):
    """Read-only text that scrolls with the D-pad.

    A focus stop of its own: Up/Down scroll it a few lines at a time (see
    ``DeckFocusController._scroll_text``) and only move focus on once the
    text has run out, so long patch notes or a check's explanation can be
    read without a touchscreen or a mouse wheel. Both glide
    (:class:`DeckSmoothScrollArea`).

    Given an explicit minimum height and an expanding vertical policy: its
    own size hint is measured before the label knows its width, which is
    what collapsed the Update screen's patch notes to two lines until the
    next relayout.
    """

    def __init__(
        self,
        text: str = "",
        *,
        min_height: int = 240,
        max_height: int | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("deckTextPanel")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(px(min_height))
        if max_height is not None:
            self.setMaximumHeight(px(max_height))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.label = deck_label(text, role="body", wrap=True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.label.setContentsMargins(px(16), px(12), px(16), px(12))
        self.setWidget(self.label)
        from .focus import enable_kinetic_scroll

        enable_kinetic_scroll(self)

    def setText(self, text: str) -> None:
        self.label.setText(text)
        self.verticalScrollBar().setValue(0)

    def text(self) -> str:
        return self.label.text()
