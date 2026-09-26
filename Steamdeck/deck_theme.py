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

import re
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


_PX_RE = re.compile(r"(?<![\w.])(\d+)px")


def build_deck_stylesheet(
    name: str,
    *,
    font_scale: int = 100,
    font_family: str = "Exo 2",
    ui_scale: float | None = None,
) -> str:
    """Return the Deck QSS for theme ``name`` at ``font_scale`` percent.

    ``ui_scale`` (default: the current :mod:`Steamdeck.scale` factor)
    multiplies *every* pixel value - sizes, paddings, radii and fonts - so a
    bigger window gets a proportionally bigger interface. ``font_scale``
    then applies on top, to text only.
    """
    from .scale import scale

    factor = scale() if ui_scale is None else ui_scale
    tokens = dict(THEMES.get(name) or THEMES["gamma"])
    tokens["card_glass"] = _translucent(tokens.get("card", "#141b15"), 240)
    qss = _DECK_TEMPLATE.safe_substitute(tokens)
    if factor != 1.0:
        qss = _PX_RE.sub(
            lambda match: f"{max(1, round(int(match.group(1)) * factor))}px", qss
        )
    if font_scale != 100:
        qss = _FONT_SIZE_RE.sub(
            lambda match: f"font-size: {max(_MIN_FONT_PX, round(int(match.group(1)) * font_scale / 100))}px",
            qss,
        )
    return qss.replace(
        "%FONT_FAMILY%", _FONT_FAMILY_MAP.get(font_family, _FALLBACK_FONT)
    )


def _translucent(color: str, alpha: int) -> str:
    """``color`` (any form QColor parses) as an ``rgba()`` at ``alpha``."""
    from PySide6.QtGui import QColor

    parsed = QColor(color)
    if not parsed.isValid():
        return color
    return f"rgba({parsed.red()}, {parsed.green()}, {parsed.blue()}, {alpha})"


_DECK_TEMPLATE = string.Template("""
* {
    font-family: %FONT_FAMILY%;
    font-size: 18px;
    outline: none;
}
QMainWindow {
    background-color: $bg;
}
/* Fills the margin around the fixed-size Deck panel when the window is
   bigger than 1280x800 (docked to an external display, or resized). */
QWidget#deckLetterbox {
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
    border-bottom: 1px solid $border;
}
QWidget#deckNav {
    background-color: $topbar;
    border-top: 1px solid $border;
}
/* The button-prompt strip under the nav bar, like Steam's own footer. */
QWidget#deckFooter {
    background-color: $topbar;
}
QLabel#deckHintButton {
    font-size: 12px;
    font-weight: bold;
    color: $accent_text;
    background-color: $text_dim;
    border-radius: 9px;
    padding: 1px 8px;
    min-width: 12px;
    min-height: 20px;
    max-height: 20px;
}
QLabel#deckClock {
    font-size: 16px;
    font-weight: bold;
    color: $text_bright;
    background: transparent;
}
QLabel#deckBatteryLow {
    font-size: 18px;
    font-weight: bold;
    color: $danger_text;
    background: transparent;
}
QLabel#deckAwake {
    font-size: 14px;
    color: $accent_strong;
    border: 1px solid $accent;
    border-radius: 12px;
    padding: 2px 10px;
    background: transparent;
}
QLabel#deckTitle {
    font-size: 28px;
    font-weight: bold;
    color: $accent_strong;
    background: transparent;
    letter-spacing: 2px;
}
QLabel#deckWordmark {
    font-size: 22px;
    font-weight: bold;
    letter-spacing: 2px;
    color: $accent_strong;
    background: transparent;
}
QLabel#deckByline {
    font-size: 13px;
    letter-spacing: 1px;
    color: $accent_strong;
    background: transparent;
}
QLabel#deckHeaderInfo {
    font-size: 18px;
    color: $text_info;
    background: transparent;
}
/* Matches desktop's #modCounter exactly - bold and accent-colored, not the
   dim profile-name text next to it. */
QLabel#deckModCounter {
    font-size: 20px;
    font-weight: bold;
    color: $accent_strong;
    background: transparent;
}
/* Literal, not $warn: desktop's own incomplete-install case
   (main_window.py's update_mod_counter()) hardcodes WARN.name() rather
   than going through the theme, so it reads the same amber in every
   theme - matched here rather than substituting the per-theme token. */
QLabel#deckModCounterWarn {
    font-size: 20px;
    font-weight: bold;
    color: #d9a04c;
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
/* Same size as deckCaption, colored like desktop's launch-status label
   (play_page.py's _set_result(), which paints live/good status in accent
   green) - used for Play's "Ready"/"Running" line, not "No Profile". */
QLabel#deckCaptionAccent {
    font-size: 15px;
    color: $accent_strong;
    background: transparent;
}
QLabel#deckBody {
    font-size: 18px;
    color: $text;
    background: transparent;
}
/* Themed variants of deckBody, matching desktop's generic #accent/#warn
   QLabel classes (commander_gui/themes.py) - used for the Update screen's
   and Dashboard's Updates-card status line. */
QLabel#deckBodyAccent {
    font-size: 18px;
    color: $accent_strong;
    background: transparent;
}
QLabel#deckBodyWarn {
    font-size: 18px;
    color: $warn;
    background: transparent;
}
/* Literal, not $accent_strong: matches Dashboard's own storage-total and
   "up to date" text on desktop, which hardcodes OK_GREEN so it always
   agrees with the Installed status dot regardless of the active theme's
   accent hue (see commander_gui/ui/dashboard.py). */
QLabel#deckBodyOk {
    font-size: 18px;
    color: #7dc963;
    background: transparent;
}
QLabel#deckRowTitle {
    font-size: 20px;
    font-weight: 600;
    color: $text_bright;
    background: transparent;
}
/* A locked achievement's name: same size, dimmed. */
QLabel#deckRowTitleDim {
    font-size: 20px;
    font-weight: 600;
    color: $text_dim;
    background: transparent;
}
/* The active profile's row title on the Profile screen, matching desktop's
   profiles_page.py giving that one name #accent instead of plain text. */
QLabel#deckRowTitleAccent {
    font-size: 20px;
    font-weight: 600;
    color: $accent_strong;
    background: transparent;
}
QLabel#deckRowValue {
    font-size: 18px;
    color: $text_info;
    background: transparent;
}
QLabel#deckRowValueOk {
    font-size: 18px;
    color: #7dc963;
    background: transparent;
}
QLabel#deckRowValueWarn {
    font-size: 18px;
    color: $warn;
    background: transparent;
}
/* A small numbered pill in front of a step's label, replacing raw inline
   HTML color spans - same accent color, actually themed and reusable. */
QLabel#deckStepBadge {
    font-size: 16px;
    font-weight: bold;
    color: $accent_text;
    background-color: $accent;
    border-radius: 15px;
    min-width: 30px;
    max-width: 30px;
    min-height: 30px;
    max-height: 30px;
    qproperty-alignment: AlignCenter;
}

/* ---------------------------------------------------------------- cards
   Radius scale: 14px rows/buttons/inputs, 20px cards/overlay, 22px hero,
   pill (radius = half height) for chips/jump letters. Borders lean on the
   dim $border token rather than $border_strong almost everywhere, so
   separation reads mainly as a shade step off the card/bg colors instead
   of a drawn outline - $border_strong/2px is kept in reserve for the hero
   button and the overlay panel, the two things worth calling out. */
QFrame#deckCard {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 20px;
}
/* A tool tile inside a Utilities card, and the section headings above
   them. */
QFrame#deckTile {
    background-color: $mono;
    border: 1px solid $border;
    border-radius: 14px;
}
QLabel#deckSection {
    font-size: 15px;
    font-weight: bold;
    letter-spacing: 1px;
    color: $accent_strong;
    background: transparent;
}
QFrame#deckDangerCard {
    background-color: $card;
    border: 1px solid $danger_border;
    border-radius: 20px;
}
QLabel#deckFooterProfile {
    font-size: 15px;
    font-weight: bold;
    color: $text_info;
    background: transparent;
}
/* Utilities' Run buttons: small pills beside each tool's text. */
QPushButton#deckRunButton,
QPushButton#deckRunDanger {
    min-height: 48px;
    max-height: 48px;
    padding: 0 12px;
    font-size: 17px;
    border-radius: 24px;
}
QPushButton#deckRunDanger {
    background-color: $danger_bg;
    color: $danger_text;
    border: 1px solid $danger_border;
}
QPushButton#deckRunDanger:hover {
    background-color: $danger_hover;
}
QPushButton#deckRunDanger:focus {
    border: 2px solid $focus;
}
/* System check: the small "Copy install command" button on a row. */
QPushButton#deckCopyCommand {
    min-height: 48px;
    max-height: 48px;
    padding: 0 14px;
    font-size: 15px;
    font-weight: normal;
    border-radius: 20px;
}
/* The Mods screen's magnifier and "search: xyz" chips. */
QPushButton#deckIconChip {
    min-height: 48px;
    padding: 0;
    border-radius: 24px;
}
QPushButton#deckSearchChip {
    min-height: 44px;
    padding: 0 18px;
    font-size: 16px;
    border-radius: 22px;
    color: $accent;
    border-color: $accent;
    text-align: left;
}
QListWidget#deckChanges {
    font-size: 17px;
}
/* The Dashboard's "can I play?" banner - the one card worth an accent edge. */
QFrame#deckBanner {
    background-color: $card;
    border: 2px solid $accent;
    border-radius: 20px;
}
QScrollArea#deckTextPanel {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 14px;
}
QFrame#deckDivider {
    background-color: $border;
}

/* -------------------------------------------------------------- buttons */
QPushButton {
    background-color: $btn;
    color: $text_btn;
    border: 1px solid $border;
    border-radius: 14px;
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
    border-radius: 16px;
    /* 2px more than the base padding, handed to the thicker focus ring
       so focusing the button doesn't grow it and shift the layout. */
    padding: 2px 22px;
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
    border-radius: 22px;
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
    padding: 2px 22px;
    min-height: 96px;
}
QPushButton#deckDanger:hover {
    background-color: $danger_hover;
}
QPushButton#deckChip {
    min-height: 48px;
    padding: 0 14px;
    font-size: 16px;
    border-radius: 24px;
}
/* The A-Z jump strip: 26 of these have to fit across the content area, so
   they drop the horizontal padding every other button carries. Without this
   the row is wider than the screen and the whole Mods page clips. */
QPushButton#deckJumpChip {
    min-height: 44px;
    min-width: 0;
    padding: 0;
    font-size: 15px;
    border-radius: 22px;
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
    border-bottom: 3px solid transparent;
    border-radius: 12px;
    min-height: 50px;
    padding: 0 6px;
    font-size: 16px;
    font-weight: normal;
    color: $text_nav;
}
QPushButton#deckNavCell:hover {
    background-color: $btn_hover;
    color: $text_btn_hover;
}
/* A thin accent underline rather than a filled rectangle, so the active tab
   reads as a highlight under the label instead of a boxed-off cell. */
QPushButton#deckNavCell[current="true"] {
    color: $accent;
    background-color: $chip;
    border-bottom: 3px solid $accent;
    font-weight: bold;
}
/* Every button in an overlay's button row shares one height, whatever
   its role - a primary Install beside a normal Cancel used to be half as
   tall again. */
QPushButton[overlayButton="true"],
QPushButton#deckPrimary[overlayButton="true"],
QPushButton#deckDanger[overlayButton="true"] {
    min-height: 64px;
    max-height: 64px;
    font-size: 20px;
}
/* Primary and danger carry 2px of extra padding plus a 1px border (traded
   for a 3px ring on focus), so their content box is 4px shorter to land on
   the same outer height as a plain button's 64px + 1px border. */
QPushButton#deckPrimary[overlayButton="true"],
QPushButton#deckDanger[overlayButton="true"] {
    min-height: 60px;
    max-height: 60px;
}
/* On-screen keyboard keys: compact, no side padding, so ten fit a row. */
QPushButton#deckKey,
QPushButton#deckPrimaryKey {
    min-height: 60px;
    padding: 0;
    font-size: 22px;
    border-radius: 12px;
}
QPushButton#deckKey:checked {
    background-color: $chip;
    color: $accent;
    border-color: $accent;
}
QPushButton#deckPrimaryKey {
    background-color: $primary;
    color: $accent_text;
    border: 1px solid $accent;
}

/* ----------------------------------------------------------------- rows */
QWidget#deckRow {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 14px;
}
QWidget#deckRow:hover {
    background-color: $btn_hover;
    border-color: $border_secondary;
}
/* Status cards (Installed / Ready ...) look like rows but are read-only:
   no hover lift, no focus ring - nothing about them says "press me". */
QWidget#deckStatusRow {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 14px;
}

/* ---------------------------------------------------------------- input */
QLineEdit {
    background-color: $mono;
    color: $text;
    border: 1px solid $border_input;
    border-radius: 14px;
    padding: 0 16px;
    min-height: 72px;
    font-size: 20px;
    selection-background-color: $selection;
    selection-color: $selection_text;
}
QLineEdit::placeholder {
    color: $text_dim;
}
QLineEdit#deckKeyboardPreview {
    font-size: 24px;
    border: 2px solid $accent;
}

/* ----------------------------------------------------------------- list */
QListWidget {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 18px;
    padding: 4px;
    font-size: 20px;
}
QListWidget::item {
    color: $text;
    padding-left: 14px;
    border: 2px solid transparent;
    border-radius: 12px;
}
QListWidget::item:hover {
    background-color: $btn_hover;
    color: $text_bright;
}
/* The current row keeps a muted outline while focus is elsewhere (on a
   chip above the list, say), and only gets the full focus ring while the
   list itself has focus - otherwise two things on screen looked focused. */
QListWidget::item:selected {
    background-color: $btn_hover;
    color: $text_bright;
    border: 2px solid $border_strong;
}
QListWidget::item:selected:focus,
QListWidget:focus::item:selected {
    border: 2px solid $focus;
}

/* ------------------------------------------------------------- progress */
QProgressBar {
    background-color: $input;
    border: 1px solid $border_input;
    border-radius: 14px;
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
    border-radius: 12px;
}
QPlainTextEdit#deckLog {
    background-color: $mono;
    color: $text_mono;
    border: 1px solid $border;
    border-radius: 14px;
    font-family: monospace;
    font-size: 15px;
}

/* --------------------------------------------------------------- chips */
QLabel#deckChipOk,
QLabel#deckChipWarn,
QLabel#deckChipBad {
    border-radius: 16px;
    padding: 0 14px;
    font-size: 16px;
    font-weight: 600;
    min-width: 92px;
    min-height: 32px;
    max-height: 32px;
}
/* Transparent pills: a status sits *on* its row rather than being a box
   of its own, so the row's hover/focus lift shows straight through it
   instead of leaving a dark block where the chip is. */
QLabel#deckChipOk {
    background: transparent;
    color: $chip_ok;
    border: 1px solid $chip_ok_border;
}
QLabel#deckChipWarn {
    background: transparent;
    color: $warn;
    border: 1px solid $border_strong;
}
QLabel#deckChipBad {
    background: transparent;
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
    border-radius: 20px;
}
/* The Welcome panel: the theme's card colour, slightly see-through, so the
   screen behind it still shows. */
QFrame#deckOverlayPanelGlass {
    background-color: $card_glass;
    border: 2px solid $border_strong;
    border-radius: 20px;
}
/* Welcome screen: your version -> latest release, on one strip. */
QFrame#deckVersionBar {
    background-color: $chip;
    border: 1px solid $border;
    border-radius: 12px;
}
QPushButton[overlayHeaderButton="true"] {
    min-height: 48px;
    max-height: 48px;
    padding: 0 16px;
    font-size: 17px;
}
QCheckBox#deckCheck {
    font-size: 16px;
    color: $text_dim;
    spacing: 10px;
    min-height: 48px;
    padding: 0 8px;
    border-radius: 10px;
}
QCheckBox#deckCheck:focus {
    color: $text_bright;
    background-color: $btn_hover;
}
QCheckBox#deckCheck::indicator {
    width: 24px;
    height: 24px;
    border: 2px solid $border_strong;
    border-radius: 6px;
    background-color: $input;
}
QCheckBox#deckCheck::indicator:checked {
    background-color: $accent;
    border-color: $accent;
}
/* Play GAMMA while the game runs: same size as the hero, danger colours. */
QPushButton#deckHeroDanger {
    background-color: $danger_bg;
    color: $danger_text;
    border: 2px solid $danger_border;
    border-radius: 22px;
    min-height: 168px;
    font-size: 32px;
    font-weight: bold;
    letter-spacing: 2px;
}
QPushButton#deckHeroDanger:hover {
    background-color: $danger_hover;
}
/* COMMANDER update status, far left of the footer. */
QLabel#deckFooterOk {
    font-size: 15px;
    color: #7dc963;
    background: transparent;
}
QLabel#deckFooterWarn {
    font-size: 15px;
    font-weight: bold;
    color: #d9a04c;
    background: transparent;
}
/* Game stats on the Play screen. */
QLabel#deckStatValue {
    font-size: 24px;
    font-weight: bold;
    color: $accent;
    background: transparent;
}
QLabel#deckSection {
    font-size: 17px;
    font-weight: bold;
    color: $accent;
    background: transparent;
    padding-top: 6px;
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
    border: 1px solid $border;
    border-radius: 18px;
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
    border: 2px solid $focus;
    background-color: $btn_hover;
}
QPushButton#deckHero:focus {
    border: 2px solid $focus;
    background-color: $hero_hover1;
}
QPushButton#deckPrimaryKey:focus {
    border: 3px solid $focus;
}
/* Primary and danger buttons set their own border by object name, and an
   ID selector outranks the bare :focus rule above - without these the
   focused Play GAMMA / Quit COMMANDER looked exactly like unfocused ones.
   3px, not 2px: their fill is already bright, so a thin ring disappears.
   The primary ring uses $text, not $focus: in every theme $focus is the
   same hue as the primary fill and the ring blended into it. */
QPushButton#deckPrimary:focus {
    border: 3px solid $text;
    padding: 0 20px;
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 $accent_strong, stop: 1 $primary_hover
    );
}
QPushButton#deckDanger:focus {
    border: 3px solid $focus;
    padding: 0 20px;
    background-color: $danger_hover;
}
/* A full ring, not only the underline: the current tab already has an
   accent underline, and $focus is the same hue, so an underline alone left
   "focused" and "current" looking identical. */
QPushButton#deckNavCell:focus {
    border: 2px solid $focus;
    border-bottom: 3px solid $focus;
    background-color: $btn_hover;
}
QPushButton#deckNavCell[current="true"]:focus {
    border: 2px solid $focus;
    border-bottom: 3px solid $focus;
    color: $text_bright;
}
/* Checked chips and keys set their border by state, which outranks the bare
   :focus rule - a focused "Enabled" filter or a Shift that was on showed no
   ring at all. */
QPushButton#deckChip:checked:focus,
QPushButton#deckKey:checked:focus {
    border: 3px solid $focus;
}
/* The Stop button while the game runs: it sets its own border by ID. */
QPushButton#deckDiscord {
    background-color: #4E59CF;
    border: 2px solid #4E59CF;
    color: #f2f3ff;
    font-weight: bold;
}
QPushButton#deckDiscord:hover {
    background-color: #5865F2;
}
QPushButton#deckDiscord:focus {
    border: 4px solid $focus;
    background-color: #5865F2;
}
QPushButton#deckHeroDanger:focus {
    border: 4px solid $focus;
    background-color: $danger_hover;
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
