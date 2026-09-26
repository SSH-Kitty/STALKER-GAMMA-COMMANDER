"""An on-screen keyboard that lives inside the Deck window.

Steam's own keyboard (Steam + X) exists, but it only pops up by itself for
applications Steam knows are asking for text, which a Qt app launched as a
non-Steam shortcut is not - and in Game Mode it has to be summoned with a
chord most people have never heard of. So Deck Mode carries its own: a
QWERTY grid in a :class:`~Steamdeck.widgets.DeckOverlay`, walked with the
D-pad like every other screen, tapped like every other button, and writing
straight into the text field it was opened for so the effect (a mod list
filtering as you type) is live.

Face buttons inside the keyboard: A types the focused key, X is Space, Y is
Backspace, L1 is Shift (twice quickly for caps lock), and B or Start is
Done - text is written as it is typed, so there is nothing to lose by
leaving, and whatever the field was opened for (naming a folder, say) still
happens. The D-pad walks the keys as a grid: Left/Right wrap round a row,
Up/Down keep to the same column.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from commander_gui.i18n import tr

from . import gamepad as pad
from .scale import px
from .widgets import DeckOverlay, deck_button

_ROWS = (
    "1234567890",
    "qwertyuiop",
    "asdfghjkl-",
    "zxcvbnm_./",
)

#: What the number row types with Shift, as on a physical keyboard - the
#: symbols most often wanted in a name or a search, without a second page.
_SHIFTED = dict(zip("1234567890", "!@#$%^&*()"))

#: A second Shift within this long turns caps lock on.
_CAPS_DOUBLE_TAP_S = 0.4

#: Key cap height. Above MIN_TOUCH with room to spare, and small enough
#: that five rows plus the preview fit the overlay on an 800px panel.
_KEY_H = 60


class DeckKeyboard(QWidget):
    """The key grid. Edits ``target`` in place."""

    def __init__(
        self,
        target: QLineEdit,
        *,
        on_done: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.target = target
        self._on_done = on_done
        self._shift = False
        self._caps = False
        self._last_shift = 0.0
        self._letter_keys: list[QPushButton] = []
        self._number_keys: list[QPushButton] = []
        #: key -> (row, first column, columns spanned), for grid_move.
        self._cells: dict[QPushButton, tuple[int, int, int]] = {}
        #: The column Up/Down keep to across the wide bottom-row keys.
        self._column = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(12))

        # A read-only mirror of the field being edited: the real one may be
        # hidden under the overlay's scrim.
        self.preview = QLineEdit(target.text())
        self.preview.setObjectName("deckKeyboardPreview")
        self.preview.setReadOnly(True)
        self.preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.preview.setPlaceholderText(target.placeholderText())
        layout.addWidget(self.preview)

        grid = QGridLayout()
        grid.setSpacing(px(6))
        def place(button: QPushButton, row: int, column: int, span: int = 1) -> None:
            grid.addWidget(button, row, column, 1, span)
            self._cells[button] = (row, column, span)

        for row, keys in enumerate(_ROWS):
            for column, char in enumerate(keys):
                # The label, not the char, is typed: Shift relabels the keys.
                button = self._key(char, None)
                button.clicked.connect(lambda _=False, b=button: self._type(b.text()))
                place(button, row, column)
                if char.isalpha():
                    self._letter_keys.append(button)
                elif char in _SHIFTED:
                    self._number_keys.append(button)
        bottom = len(_ROWS)
        self.shift_button = self._key("⇧", self.press_shift)
        self.shift_button.setCheckable(True)
        place(self.shift_button, bottom, 0, 2)
        place(self._key(tr("Space"), lambda: self._type(" ")), bottom, 2, 4)
        place(self._key("⌫", self.backspace), bottom, 6, 2)
        done = self._key(tr("Done"), self.done)
        done.setObjectName("deckPrimaryKey")
        place(done, bottom, 8, 2)
        layout.addLayout(grid)
        self.first_key = self._letter_keys[0]

    def _key(self, label: str, handler: Callable) -> QPushButton:
        button = deck_button(label, role="normal")
        button.setObjectName("deckKey")
        button.setMinimumHeight(px(_KEY_H))
        button.setMinimumWidth(0)
        # Held keys should not repeat: a thumb resting on A would otherwise
        # type a line of the same letter.
        button.setAutoRepeat(False)
        if handler is not None:
            button.clicked.connect(lambda _=False: handler())
        return button

    # -- moving -----------------------------------------------------------
    def grid_move(self, current, direction: str) -> bool:
        """D-pad movement across the keys; False if ``current`` isn't one.

        Left/Right wrap round the row, so the far end of a row is one press
        away. Up/Down keep to a column, remembered across the wide keys on
        the bottom row - Down from "g" to Space and back Up lands on "g",
        not on whichever key happened to be nearest Space's centre.
        """
        cell = self._cells.get(current)
        if cell is None:
            return False
        row, column, span = cell
        rows = len(_ROWS) + 1
        if direction in ("left", "right"):
            in_row = sorted(
                ((c, key) for key, (r, c, _s) in self._cells.items() if r == row),
                key=lambda pair: pair[0],
            )
            keys = [key for _c, key in in_row]
            step = 1 if direction == "right" else -1
            target = keys[(keys.index(current) + step) % len(keys)]
            self._column = self._cells[target][1]
        else:
            if span == 1 or not column <= self._column < column + span:
                self._column = column
            new_row = row + (1 if direction == "down" else -1)
            if not 0 <= new_row < rows:
                return True  # the edge: stay put rather than leave the grid
            target = next(
                key
                for key, (r, c, s) in self._cells.items()
                if r == new_row and c <= self._column < c + s
            )
        target.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    # -- editing ----------------------------------------------------------
    def _sync(self, text: str) -> None:
        self.target.setText(text)
        self.preview.setText(text)
        self.preview.setCursorPosition(len(text))

    def _type(self, char: str) -> None:
        if self._shift and len(char) == 1:
            char = _SHIFTED.get(char, char.upper())
        if self._shift:
            # One-shot: Shift applies to one character. Caps lock stays.
            self._set_shift(False)
        self._sync(self.target.text() + char)

    def backspace(self) -> None:
        self._sync(self.target.text()[:-1])

    def space(self) -> None:
        self._type(" ")

    def press_shift(self) -> None:
        """Shift for one character; pressed again quickly, caps lock."""
        now = time.monotonic()
        if self._shift and now - self._last_shift < _CAPS_DOUBLE_TAP_S:
            self._caps = True
            self._set_shift(False)
        elif self._caps:
            self._caps = False
            self._set_shift(False)
        else:
            self._set_shift(not self._shift)
        self._last_shift = now

    def _toggle_shift(self) -> None:
        self._set_shift(not self._shift)

    def _set_shift(self, on: bool) -> None:
        self._shift = on
        upper = self._shift or self._caps
        self.shift_button.setChecked(upper)
        self.shift_button.setText("⇪" if self._caps else "⇧")
        for button in self._letter_keys:
            text = button.text()
            button.setText(text.upper() if upper else text.lower())
        for button, char in zip(self._number_keys, _ROWS[0]):
            button.setText(_SHIFTED[char] if self._shift else char)

    def done(self) -> None:
        self._on_done()

    def handle_action(self, action: str) -> bool:
        """Face-button shortcuts while the keyboard is open."""
        if action == pad.SEARCH:
            self.backspace()
            return True
        if action == pad.CONTEXT:
            self.space()
            return True
        if action == pad.MENU:
            self.done()
            return True
        if action == pad.TAB_PREV:
            self.press_shift()
            return True
        return False


def open_keyboard(
    window,
    target: QLineEdit,
    *,
    title: str | None = None,
    on_done: Callable[[str], None] | None = None,
) -> DeckKeyboard:
    """Show the keyboard for ``target`` on ``window``; return it."""

    def _finish() -> None:
        window.dismiss_overlay()
        target.setFocus(Qt.FocusReason.OtherFocusReason)
        if on_done is not None:
            on_done(target.text())

    keyboard = DeckKeyboard(target, on_done=_finish)
    overlay = DeckOverlay(
        title or target.placeholderText() or tr("Keyboard"),
        keyboard,
        panel_width=1100,
    )
    overlay.action_handler = keyboard.handle_action
    # B is Done too. The text is already in the field, so "closing" never
    # discarded it - but skipping the Done step did skip what the field was
    # opened for, like creating the folder just named.
    overlay.back_handler = lambda: (keyboard.done(), True)[1]
    overlay.grid_move = keyboard.grid_move
    overlay.hints = (
        ("A", "Type"),
        ("X", "Space"),
        ("Y", "Delete"),
        ("L1", "Shift"),
        ("B", "Done"),
    )
    overlay.default_button = keyboard.first_key
    # Stacked when the field lives in an overlay itself (a picker's filter
    # box): closing the keyboard then returns to that overlay intact.
    in_overlay = window.current_overlay() is not None and window.current_overlay().isAncestorOf(target)
    window.show_overlay(overlay, stacked=in_overlay)
    return keyboard
