"""Deck-sized building blocks.

Every size here is a deliberate number, not a guess. The Steam Deck panel is
1280x800 at about 204 PPI, so one pixel is roughly 0.125mm, and it is held
around 40cm away. That means a 72px row is ~9mm tall and 18px text subtends
roughly 0.3 degrees - comfortably legible, and close to what 11-12pt looks
like on a 96 DPI monitor at desk distance. Anything smaller starts failing
one of the two tests that matter here: can a thumb hit it, and can you read
it without leaning in.

The desktop UI's shared widgets in ``commander_gui.ui.common`` are reused
wherever they carry no geometry (CommandRunner, BackgroundTask,
mo2_running). The ones that do - ProgressArea with its fixed 100x32 buttons,
InstallStatusRow, make_card, section_label - are replaced here rather than
restyled, because their sizes are baked into Python, not the stylesheet.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
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
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from commander_gui.i18n import tr
from commander_gui.themes import active_theme_tokens

# --------------------------------------------------------------- geometry
#: The Deck's native resolution. Device pixel ratio is 1, so these are real
#: pixels and the layout can be budgeted exactly.
DECK_W, DECK_H = 1280, 800

HEADER_H = 72
NAV_H = 96
MARGIN_X = 32
MARGIN_Y = 24

#: 800 - 72 header - 96 nav = 632, minus 2*24 vertical margins = 584 usable,
#: which is 7 rows of (72 + 8) with 24px to spare - so a seven-item screen
#: never needs to scroll.
CONTENT_H = DECK_H - HEADER_H - NAV_H
CONTENT_W = DECK_W - 2 * MARGIN_X

ROW_H = 72
ROW_GAP = 12
PRIMARY_H = 96
BUTTON_H = 64
#: Taller than the budget strictly needs. The Play screen has five
#: controls in room for seven, and giving the slack to the primary
#: action beats leaving a third of the screen empty beneath it.
HERO_H = 168
TOGGLE_SIZE = 64

#: Hard floor for anything interactive, enforced by tests/test_steamdeck.py.
#: Below this a thumb starts missing targets on a 7.4" screen.
MIN_TOUCH = 48

#: Width of the accent bar DeckRow paints when focused. The QSS ring alone is
#: 3px, about 0.4mm here - visible, but not from arm's length on the
#: lower-contrast themes.
FOCUS_BAR_W = 6


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
            "hint": "deckHint",
        }.get(role, "deckBody")
    )
    label.setWordWrap(wrap)
    return label


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
    button.setMinimumHeight(height)
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
        self.body.setContentsMargins(20, 16, 20, 16)
        self.body.setSpacing(12)


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
        self.setFixedHeight(ROW_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20 + FOCUS_BAR_W, 0, 20, 0)
        layout.setSpacing(16)
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
                QRect(0, 4, FOCUS_BAR_W, self.height() - 8), _token("focus")
            )
        finally:
            painter.end()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
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
    """A row that flips an on/off state, showing it as the value text."""

    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        checked: bool = False,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, "", chevron=False, parent=parent)
        self._checked = bool(checked)
        self._render()
        self.activated.connect(self._flip)

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool, *, notify: bool = False) -> None:
        self._checked = bool(checked)
        self._render()
        if notify:
            self.toggled.emit(self._checked)

    def _flip(self) -> None:
        self.set_checked(not self._checked, notify=True)

    def _render(self) -> None:
        self.set_value(tr("On") if self._checked else tr("Off"))


class DeckStatusRow(DeckRow):
    """A read-only row carrying a status chip instead of a value label."""

    def __init__(
        self,
        title: str,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, "", chevron=False, parent=parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._chip = deck_chip(tr("Checking..."), "warn")
        self.layout().addWidget(self._chip)

    def set_status(self, text: str, state: str = "ok") -> None:
        self._chip.setText(text)
        self._chip.setObjectName(
            {"ok": "deckChipOk", "warn": "deckChipWarn", "bad": "deckChipBad"}.get(
                state, "deckChipWarn"
            )
        )
        # Object-name changes need a style refresh to repaint; the same
        # unpolish/polish dance the desktop UI uses for dynamic properties.
        self._chip.style().unpolish(self._chip)
        self._chip.style().polish(self._chip)


# ---------------------------------------------------------------- pickers
class DeckPicker(QWidget):
    """A full-screen list of choices, used everywhere a combo box would be.

    Shown inside a :class:`DeckOverlay` rather than as a popup, so it obeys
    the window's own geometry under gamescope and stays inside the focus
    controller's reach.
    """

    chosen = Signal(object)

    def __init__(
        self,
        title: str,
        options: Sequence[tuple[str, object]],
        current: object = None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        # Omitted when the picker is hosted in an overlay, which draws its
        # own heading - two identical titles stacked reads as a bug.
        if title:
            layout.addWidget(deck_label(title, role="title"))

        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.list.setUniformItemSizes(True)
        self.list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        for index, (label, value) in enumerate(options):
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setSizeHint(QSize(0, ROW_H))
            self.list.addItem(item)
            if current is not None and value == current:
                self.list.setCurrentRow(index)
        if self.list.currentRow() < 0 and options:
            self.list.setCurrentRow(0)
        self.list.itemActivated.connect(self._emit)
        self.list.itemClicked.connect(self._emit)
        layout.addWidget(self.list, 1)

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
    """

    closed = Signal()

    def __init__(
        self,
        title: str,
        body: QWidget,
        buttons: Sequence[tuple[str, Callable[[], None], str]] = (),
        *,
        parent: QWidget | None = None,
        panel_width: int = 900,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("deckOverlay")
        self.setAutoFillBackground(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(MARGIN_X, MARGIN_Y, MARGIN_X, MARGIN_Y)
        outer.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        panel = QFrame()
        panel.setObjectName("deckOverlayPanel")
        panel.setMaximumWidth(panel_width)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(28, 24, 28, 24)
        panel_layout.setSpacing(16)
        heading = deck_label(title, role="body")
        heading.setObjectName("deckOverlayTitle")
        panel_layout.addWidget(heading)
        panel_layout.addWidget(body, 1)

        self.default_button: QPushButton | None = None
        if buttons:
            button_row = QHBoxLayout()
            button_row.setSpacing(12)
            for label, callback, role in buttons:
                button = deck_button(label, role=role)
                button.clicked.connect(lambda _=False, cb=callback: cb())
                button_row.addWidget(button, 1)
                if self.default_button is None:
                    self.default_button = button
            panel_layout.addLayout(button_row)
        row.addWidget(panel, 1)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 170))
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
    """A two-button yes/no overlay, with Cancel focused first.

    Cancel takes the default focus deliberately: the confirmations Deck Mode
    raises guard destructive or long-running actions, and A is the easiest
    button on the device to press by accident.
    """
    body = deck_label(message, role="body", wrap=True)
    return DeckOverlay(
        title,
        body,
        [
            (cancel_text or tr("Cancel"), on_cancel, "normal"),
            (confirm_text or tr("Continue"), on_confirm, confirm_role),
        ],
    )


class DeckToast(QLabel):
    """A transient message floating over the content.

    Stands in for the desktop status bar, which a QMainWindow would reserve
    real layout height for - height this layout has budgeted down to the
    pixel.
    """

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
        width = min(parent.width() - 2 * MARGIN_X, 900)
        self.setFixedWidth(max(240, width))
        self.adjustSize()
        self.move(
            (parent.width() - self.width()) // 2,
            parent.height() - self.height() - MARGIN_Y,
        )


# --------------------------------------------------------------- progress
class DeckProgress(QWidget):
    """Progress for a running CLI job, sized for the Deck.

    Takes the same inputs as ``commander_gui.ui.common.ProgressArea`` -
    ``on_line``, ``on_started``, ``on_finished``, ``on_cancelled``,
    ``set_runner``, ``status_message`` - so call sites read the same as the
    desktop pages'. What differs is the presentation: no per-addon table, no
    collapsible log panel, no 100x32 buttons; just a fat bar, one status
    line, a short log tail and two touch-sized controls.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runner = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        layout.addWidget(self.bar)

        self.status = deck_label("", role="body", wrap=True)
        layout.addWidget(self.status)

        self.tail = deck_label("", role="caption", wrap=False)
        self.tail.setObjectName("deckCaption")
        self.tail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.tail)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.pause_button = deck_button(tr("Pause"), on_click=self._toggle_pause)
        self.cancel_button = deck_button(tr("Cancel"), on_click=self._cancel)
        buttons.addWidget(self.pause_button, 1)
        buttons.addWidget(self.cancel_button, 1)
        layout.addLayout(buttons)

        self._paused = False

    # -- runner wiring ----------------------------------------------------
    def set_runner(self, runner) -> None:
        self._runner = runner
        enabled = runner is not None
        self.pause_button.setEnabled(enabled)
        self.cancel_button.setEnabled(enabled)

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

    def _cancel(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
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
        self.set_runner(None)

    def on_cancelled(self) -> None:
        self.bar.setRange(0, 100)
        self.status.setText(tr("Cancelled"))
        self.set_runner(None)
