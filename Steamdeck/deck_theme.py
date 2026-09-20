"""The Deck UI stylesheet.

A second QSS template alongside ``commander_gui.themes``'s, filled from the
*same* ``$token`` palettes - so every theme the desktop UI ships (and any
added later) styles Deck Mode with no per-theme work here.

Why not simply reuse ``themes.build_stylesheet()``: that template bakes
desktop geometry into itself - 5px/8px input padding, 30px minimum heights,
12px/18px tab padding, 11px chips, 4px radii - and its only adjustment knob
rescales ``font-size`` declarations. Fonts are not the problem. You cannot
reach a 72px touch row by making text bigger, and the desktop sheet also
targets a dozen object names (#topbar, #navtabs, #cogButton, #installPage,
...) that Deck Mode never creates.

The other thing this sheet fixes is focus. The desktop QSS has exactly one
:focus rule, covering QLineEdit/QSpinBox/QComboBox, which is fine for a
mouse and useless for a D-pad: a focused button is indistinguishable from
an unfocused one. Here every focusable class gets a ring, a background lift
and - for rows, via DeckRow.paintEvent - an accent bar, because a 3px ring
at the Deck's 204 PPI is only about a third of a millimetre wide.
"""

from __future__ import annotations

import string

from commander_gui.themes import (
    _FALLBACK_FONT,
    _FONT_FAMILY_MAP,
    _FONT_SIZE_RE,
    THEMES,
)

#: Smallest font the scale is allowed to produce, matching the floor the
#: desktop stylesheet builder uses.
_MIN_FONT_PX = 9


def build_deck_stylesheet(
    name: str,
    *,
    font_scale: int = 100,
    font_family: str = "Exo 2",
) -> str:
    """Return the Deck QSS for theme ``name`` at ``font_scale`` percent."""
    tokens = THEMES.get(name) or THEMES["gamma"]
    qss = _DECK_TEMPLATE.safe_substitute(tokens)
    if font_scale != 100:
        qss = _FONT_SIZE_RE.sub(
            lambda match: f"font-size: {max(_MIN_FONT_PX, round(int(match.group(1)) * font_scale / 100))}px",
            qss,
        )
    return qss.replace(
        "%FONT_FAMILY%", _FONT_FAMILY_MAP.get(font_family, _FALLBACK_FONT)
    )


_DECK_TEMPLATE = string.Template("""
* {
    font-family: %FONT_FAMILY%;
    font-size: 18px;
    outline: none;
}
QMainWindow {
    background-color: $bg;
}
QWidget {
    background: transparent;
    color: $text;
}
/* The backdrop paints itself (DeckBackdrop.paintEvent); anything stacked on
   top of it stays transparent or semi-transparent so the glow reads through
   rather than being covered by a flat fill. */
QWidget#deckCentral,
QWidget#deckPageContent,
QScrollArea > QWidget#qt_scrollarea_viewport {
    background: transparent;
}
QWidget#deckHeader {
    background-color: $topbar;
    border-bottom: 2px solid $border_strong;
}
QWidget#deckNav {
    background-color: $topbar;
    border-top: 2px solid $border_strong;
}
QLabel#deckTitle {
    font-size: 28px;
    font-weight: bold;
    color: $accent_strong;
    background: transparent;
    letter-spacing: 2px;
}
QLabel#deckHeaderInfo {
    font-size: 18px;
    color: $text_info;
    background: transparent;
}
QLabel#deckHint {
    font-size: 15px;
    color: $text_dim;
    background: transparent;
}
QLabel#deckCaption {
    font-size: 15px;
    color: $text_dim;
    background: transparent;
}
QLabel#deckBody {
    font-size: 18px;
    color: $text;
    background: transparent;
}
QLabel#deckRowTitle {
    font-size: 20px;
    font-weight: 600;
    color: $text_bright;
    background: transparent;
}
QLabel#deckRowValue {
    font-size: 18px;
    color: $text_info;
    background: transparent;
}

/* ---------------------------------------------------------------- cards */
QFrame#deckCard {
    background-color: $card;
    border: 1px solid $border_strong;
    border-radius: 12px;
}

/* -------------------------------------------------------------- buttons */
QPushButton {
    background-color: $btn;
    color: $text_btn;
    border: 1px solid $border_strong;
    border-radius: 10px;
    padding: 0 20px;
    min-height: 64px;
    font-size: 20px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: $btn_hover;
    color: $text_btn_hover;
}
QPushButton:pressed {
    background-color: $btn_pressed;
}
QPushButton:disabled {
    background-color: $btn_disabled;
    color: $text_disabled;
    border-color: $border;
}
QPushButton#deckPrimary {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 $primary_hover, stop: 1 $primary
    );
    color: $accent_text;
    border: 1px solid $accent;
    border-radius: 10px;
    min-height: 96px;
    font-size: 22px;
    letter-spacing: 1px;
}
QPushButton#deckPrimary:hover {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 $accent_strong, stop: 1 $primary_hover
    );
}
QPushButton#deckPrimary:disabled {
    background-color: $primary_disabled_bg;
    border-color: $primary_disabled_border;
    color: $primary_disabled_text;
}
QPushButton#deckHero {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 $hero1, stop: 1 $hero2
    );
    color: $hero_text;
    border: 2px solid $hero_border;
    border-radius: 14px;
    min-height: 168px;
    font-size: 32px;
    font-weight: bold;
    letter-spacing: 2px;
}
QPushButton#deckHero:hover {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 $hero_hover1, stop: 1 $hero_hover2
    );
}
QPushButton#deckHero:disabled {
    background-color: $primary_disabled_bg;
    border-color: $primary_disabled_border;
    color: $primary_disabled_text;
}
QPushButton#deckDanger {
    background-color: $danger_bg;
    color: $danger_text;
    border: 1px solid $danger_border;
    min-height: 96px;
}
QPushButton#deckDanger:hover {
    background-color: $danger_hover;
}
QPushButton#deckChip {
    min-height: 48px;
    padding: 0 14px;
    font-size: 16px;
    border-radius: 6px;
}
/* The A-Z jump strip: 26 of these have to fit across the content area, so
   they drop the horizontal padding every other button carries. Without this
   the row is wider than the screen and the whole Mods page clips. */
QPushButton#deckJumpChip {
    min-height: 44px;
    min-width: 0;
    padding: 0;
    font-size: 15px;
    border-radius: 5px;
}
QPushButton#deckChip:checked {
    background-color: $chip;
    color: $accent;
    border-color: $accent;
    font-weight: bold;
}
QPushButton#deckStep {
    min-height: 64px;
    padding: 0;
    font-size: 26px;
}
QPushButton#deckNavCell {
    background: transparent;
    border: none;
    border-radius: 8px;
    min-height: 88px;
    padding: 0;
    font-size: 15px;
    font-weight: normal;
    color: $text_nav;
}
QPushButton#deckNavCell:hover {
    background-color: $btn_hover;
    color: $text_btn_hover;
}
QPushButton#deckNavCell[current="true"] {
    color: $accent;
    background-color: $chip;
    border-top: 3px solid $accent;
    border-radius: 0 0 8px 8px;
}

/* ----------------------------------------------------------------- rows */
QWidget#deckRow {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 10px;
}
QWidget#deckRow:hover {
    background-color: $btn_hover;
    border-color: $border_secondary;
}

/* ---------------------------------------------------------------- input */
QLineEdit {
    background-color: $mono;
    color: $text;
    border: 1px solid $border_input;
    border-radius: 8px;
    padding: 0 16px;
    min-height: 72px;
    font-size: 20px;
    selection-background-color: $selection;
    selection-color: $selection_text;
}
QLineEdit::placeholder {
    color: $text_dim;
}

/* ----------------------------------------------------------------- list */
QListWidget {
    background-color: $card;
    border: 1px solid $border_strong;
    border-radius: 10px;
    padding: 4px;
    font-size: 20px;
}
QListWidget::item {
    color: $text;
    border: 3px solid transparent;
    border-radius: 6px;
}
QListWidget::item:selected {
    background-color: $btn_hover;
    color: $text_bright;
    border: 3px solid $focus;
}

/* ------------------------------------------------------------- progress */
QProgressBar {
    background-color: $input;
    border: 1px solid $border_input;
    border-radius: 6px;
    min-height: 28px;
    text-align: center;
    color: $text_bright;
    font-size: 16px;
}
QProgressBar::chunk {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 $hero1, stop: 1 $accent_strong
    );
    border-radius: 5px;
}
QPlainTextEdit#deckLog {
    background-color: $mono;
    color: $text_mono;
    border: 1px solid $border;
    border-radius: 8px;
    font-family: monospace;
    font-size: 15px;
}

/* --------------------------------------------------------------- chips */
QLabel#deckChipOk,
QLabel#deckChipWarn,
QLabel#deckChipBad {
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 16px;
    min-width: 92px;
}
QLabel#deckChipOk {
    background-color: $chip;
    color: $chip_ok;
    border: 1px solid $chip_ok_border;
}
QLabel#deckChipWarn {
    background-color: $chip;
    color: $warn;
    border: 1px solid $border_strong;
}
QLabel#deckChipBad {
    background-color: $danger_bg;
    color: $danger_text;
    border: 1px solid $danger_border;
}

/* ------------------------------------------------------------- overlays */
QWidget#deckOverlay {
    background-color: transparent;
}
QFrame#deckOverlayPanel {
    background-color: $card;
    border: 2px solid $border_strong;
    border-radius: 12px;
}
QLabel#deckOverlayTitle {
    font-size: 24px;
    font-weight: bold;
    color: $text_bright;
    background: transparent;
}
QLabel#deckToast {
    background-color: $checking_bg;
    color: $text_bright;
    border: 1px solid $border_strong;
    border-radius: 8px;
    padding: 14px 20px;
    font-size: 18px;
}

/* --------------------------------------------------------------- scroll */
QScrollArea {
    background: transparent;
    border: none;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: $border_secondary;
    border-radius: 5px;
    min-height: 40px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background: $border_secondary;
    border-radius: 5px;
    min-width: 40px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
}

/* ----------------------------------------------------------------- focus
   The whole point of this sheet. Every focusable class gets the same ring
   so D-pad navigation is legible; rows additionally paint an accent bar in
   DeckRow.paintEvent, because a 3px ring is only ~0.4mm on this panel. */
QPushButton:focus,
QLineEdit:focus,
QListWidget:focus,
QCheckBox:focus,
QComboBox:focus,
QAbstractScrollArea:focus,
QWidget#deckRow:focus {
    border: 3px solid $focus;
    background-color: $btn_hover;
}
QPushButton#deckHero:focus {
    border: 3px solid $focus;
    background-color: $hero_hover1;
}
QPushButton#deckNavCell:focus {
    border: 3px solid $focus;
    background-color: $btn_hover;
}

/* --------------------------------------------------------- message boxes
   Deck Mode uses in-window overlays, but a handful of shared helpers in
   commander_gui.ui.common raise a real QMessageBox. Size those up too so
   they are usable with a thumbstick rather than merely present. */
QMessageBox {
    background-color: $card;
}
QMessageBox QLabel {
    font-size: 18px;
    color: $text;
}
QMessageBox QPushButton {
    min-width: 140px;
    min-height: 56px;
}
""")
