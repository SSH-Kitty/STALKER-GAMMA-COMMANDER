"""Theme palettes for the COMMANDER GUI.

The application stylesheet is a ``string.Template`` whose ``$token``
placeholders are filled from one of the palettes in :data:`THEMES`. Each theme
also carries the painted backdrop glow colors and the native ``QPalette``
colors so dialogs, menus and scrollbars match the active theme.
"""

from __future__ import annotations

import re
import string
import tempfile
import time
from pathlib import Path

from PySide6.QtGui import QColor, QPalette

#: (key, label, description, swatches) shown on the Settings page, in order.
THEME_INFO: list[tuple[str, str, str, tuple[str, str, str]]] = [
    (
        "gamma",
        "GAMMA",
        "The default look: deep green-black with a radiation-green glow.",
        ("#0d130c", "#9fe96f", "#e2ead8"),
    ),
    (
        "black",
        "GAMMA Black",
        "Pure black and near-monochrome with a muted green accent.",
        ("#000000", "#9fe96f", "#e0e0e0"),
    ),
    (
        "midnight",
        "Midnight",
        "Cool slate blues and greys with a teal accent.",
        ("#0b1016", "#6fd3a8", "#dbe6f0"),
    ),
    (
        "reactor",
        "Reactor",
        "Deep black with a glowing reactor-coolant teal accent.",
        ("#010a0a", "#006868", "#cdeaea"),
    ),
    (
        "redforest",
        "Red Forest (Live)",
        "Autumn in the Zone: the forester's tower in the Red Forest, following the clock through dawn, day, dusk and night.",
        ("#efe4d6", "#b5521f", "#2a1e18"),
    ),
    (
        "winter",
        "Winter (Live)",
        "Frosty and snowbound: an abandoned village that follows the clock through dawn, day, dusk and night.",
        ("#dfe5ea", "#3a6d37", "#1b252d"),
    ),
]

_ACTIVE: str = "gamma"


def set_active_theme(name: str) -> None:
    global _ACTIVE, _LIVE_TIME
    if name in THEMES:
        _ACTIVE = name
    # Re-read with the theme, so every window that restyles picks up a new lock.
    from . import gui_settings

    _LIVE_TIME = gui_settings.load_gui_settings().get("live_time", "")


def active_theme() -> str:
    return _ACTIVE


def active_theme_tokens() -> dict[str, str]:
    return THEMES[resolve_theme(_ACTIVE)]


def token_color(name: str, fallback: str) -> QColor:
    """A live theme token as a QColor, ``rgba(r, g, b, a)`` ones included.

    QColor() reads hex but not CSS rgba(), which Winter's see-through
    surfaces (card, chip, btn) use - it came out invalid, i.e. black.
    """
    return parse_color(active_theme_tokens().get(name, fallback), fallback)


def parse_color(value: str, fallback: str) -> QColor:
    """``value`` as a QColor, CSS ``rgba(r, g, b, a)`` included (see token_color)."""
    match = re.fullmatch(r"\s*rgba\(([^)]*)\)\s*", value)
    if match:
        color = QColor(*(int(part) for part in match.group(1).split(",")))
    else:
        color = QColor(value)
    return color if color.isValid() else QColor(fallback)


_FALLBACK_FONT = '"Exo 2", "DejaVu Sans", "Noto Sans", sans-serif'

_FONT_FAMILY_MAP: dict[str, str] = {
    "Exo 2": '"Exo 2", "DejaVu Sans", "Noto Sans", sans-serif',
    "Noto Sans": '"Noto Sans", "DejaVu Sans", sans-serif',
    "DejaVu Sans": '"DejaVu Sans", "Noto Sans", sans-serif',
    "Liberation Sans": '"Liberation Sans", "DejaVu Sans", sans-serif',
    "Inter": '"Inter", "DejaVu Sans", sans-serif',
}


_ROOT = Path(__file__).resolve().parent.parent


def build_stylesheet(
    name: str, font_size: int | None = None, font_family: str | None = None
) -> str:
    tokens = {"status_ok": "#7dc963", **(THEMES.get(resolve_theme(name)) or THEMES["gamma"])}
    if font_size is None:
        font_size = int(tokens["font_size"].rstrip("px"))
    qss = _TEMPLATE.safe_substitute(tokens)
    if font_size != 13:
        scale = font_size / 13.0
        qss = _FONT_SIZE_RE.sub(
            lambda m: f"font-size: {max(1, round(int(m.group(1)) * scale))}px",
            qss,
        )
    qss = qss.replace("%FONT_SIZE%", f"{font_size}px")
    resolved_family = _FONT_FAMILY_MAP.get(font_family or "Exo 2", _FALLBACK_FONT)
    qss = qss.replace("%FONT_FAMILY%", resolved_family)
    qss = qss.replace("%COMBO_ARROW%", _combo_arrow(tokens["text_dim"]))
    return qss.replace("%SLIDER_HANDLE%", (_ROOT / "cli" / "stalker-gamma.png").as_posix())


def _combo_arrow(color: str) -> str:
    # QSS only takes image files, so write one chevron per theme colour.
    hex_color = parse_color(color, "#888888").name()
    path = Path(tempfile.gettempdir()) / f"commander-combo-arrow-{hex_color[1:]}.svg"
    if not path.exists():
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6" viewBox="0 0 10 6">'
            f'<path d="M1 1l4 4 4-4" fill="none" stroke="{hex_color}" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        )
    return path.as_posix()


def build_palette(name: str) -> QPalette:
    tokens = THEMES.get(resolve_theme(name)) or THEMES["gamma"]
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(tokens["pal_window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(tokens["pal_window_text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(tokens["pal_base"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(tokens["pal_alternate"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(tokens["pal_text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(tokens["pal_button"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(tokens["pal_button_text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(tokens["pal_highlight"]))
    palette.setColor(
        QPalette.ColorRole.HighlightedText, QColor(tokens["pal_highlighted_text"])
    )
    return palette


_TEMPLATE = string.Template("""* {
    font-family: %FONT_FAMILY%;
    font-size: %FONT_SIZE%;
}
QMainWindow {
    background-color: $bg;
    color: $text;
}
QWidget {
    background-color: $page;
    color: $text;
}
QScrollArea {
    background: transparent;
    border: none;
}
/* Slim rounded scrollbars: no arrows, clear track, thumb lights up on hover. */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}
QScrollBar::handle {
    background: $border_strong;
    border-radius: 3px;
}
QScrollBar::handle:vertical { min-height: 32px; }
QScrollBar::handle:horizontal { min-width: 32px; }
QScrollBar::handle:hover,
QScrollBar::handle:pressed {
    background: $accent_strong;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0;
    height: 0;
    border: none;
    background: none;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
}
QScrollArea > QWidget#qt_scrollarea_viewport,
QWidget#pageContent {
    background: transparent;
}
QWidget#panelTransparent {
    background: transparent;
}
#topbar {
    background-color: $topbar;
    border-bottom: 1px solid $border;
}
#wordmarkBlock {
    background: transparent;
}
#wordmark {
    font-size: 17px;
    font-weight: bold;
    letter-spacing: 2px;
    color: $accent_strong;
}
#byline {
    font-size: 11px;
    letter-spacing: 1px;
    color: $accent_strong;
    padding-right: 3px;
}
#navtabs {
    background: transparent;
}
#navtabs::tab {
    background: transparent;
    color: $text_nav;
    padding: 12px 18px;
    border: none;
    border-bottom: 2px solid transparent;
}
#navtabs::tab:hover {
    color: $accent_strong;
}
#navtabs::tab:selected {
    color: $accent_strong;
    border-bottom: 2px solid $accent_strong;
}
#navtabs[settingsMode="true"]::tab:selected {
    color: $text_nav;
    border-bottom: 2px solid transparent;
}
#navtabs[settingsMode="true"]::tab:hover {
    color: $accent_strong;
}
/* The top bar's overflow button: looks like a tab, opens the tabs that
   didn't fit. Underlined like a selected tab while one of them is open. */
#navMore {
    background: transparent;
    color: $text_nav;
    padding: 12px 10px;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
}
#navMore:hover {
    color: $accent_strong;
}
#navMore[active="true"] {
    color: $accent_strong;
    border-bottom: 2px solid $accent_strong;
}
#titleStrip, #topbarRow {
    background: transparent;
}
QPushButton#winMin, QPushButton#winMax, QPushButton#winClose {
    background: transparent;
    border: none;
    border-radius: 0;
    padding: 0;
    color: $text_disabled;
    font-size: 10px;
}
QPushButton#winMin:hover, QPushButton#winMax:hover {
    background-color: $btn_hover;
    color: $text_bright;
}
QPushButton#winClose:hover {
    background-color: #c42b1c;
    color: #ffffff;
}
QPushButton#winClose:pressed {
    background-color: #a1261a;
    color: #ffffff;
}
#cogButton {
    background: transparent;
    border: none;
    color: $text_disabled;
    font-size: 18px;
    padding: 0 10px;
}
#cogButton:hover {
    color: $accent_strong;
}
#cogButton[active="true"] {
    color: $accent_strong;
}
/* Opens Steam Deck Mode: a small pill with the Deck glyph (painted in
   ui/deck_icon.py) and a label, so it reads as a control at a glance.
   The Dashboard's Achievements button is the same pill. */
#deckModeButton, #achievementsButton, #pillButton {
    background: transparent;
    border: 1px solid $border;
    border-radius: 17px;
    padding: 0 14px 0 10px;
    color: $text;
    font-weight: 600;
}
#deckModeButton:hover, #achievementsButton:hover, #pillButton:hover {
    background-color: $btn_hover;
    border-color: $accent_strong;
    color: $accent_strong;
}
#deckModeButton:pressed, #achievementsButton:pressed, #pillButton:pressed {
    background-color: $border_strong;
}
#card {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 10px;
}
#updateHero {
    background-color: $card;
    border: 1px solid $border_strong;
    border-radius: 12px;
}
#updateHeroIcon {
    background: transparent;
}
#systemCheckCard {
    background: transparent;
    border: 1px solid $border_strong;
    border-radius: 12px;
}
#checkSection {
    background: transparent;
    border-bottom: 1px solid $border;
}
#installPage QLabel#info {
    color: $text_info;
    font-size: 14px;
}
#installPage QLabel#dim {
    color: $text;
}
#installPage QLabel#section2 {
    color: $accent_section;
    margin-top: 4px;
    margin-bottom: 2px;
}
/* Verify Integrity: same frames as #statTile/#chip, status colours from
   the theme (accent = pass, warn = worth a look, danger = broken, neutral
   text = info). */
/* Plain dot + text, like the other cards' "Installed" status. */
#verifyPill {
    background: transparent;
    border: none;
    padding: 0;
    color: $text_info;
}
/* Fixed green, same as InstallStatusRow's OK_GREEN "Installed". */
#verifyPill[level="healthy"] { color: $status_ok; }
#verifyPill[level="running"] { color: $accent_strong; }
#verifyPill[level="snapshot"] { color: $text_bright; }
#verifyPill[level="attention"] { color: $warn; }
#verifyPill[level="failed"] { color: $danger_text; }
#verifyTile {
    background: transparent;
    border: none;
    min-height: 76px;
}
#verifyTileTitle {
    color: $text_info;
    font-size: 13px;
    font-weight: bold;
}
#verifyTileValue {
    color: $text_bright;
    font-size: 22px;
    font-weight: bold;
}
#verifyTileValue[state="idle"] { color: $text_dim; font-weight: normal; }
#verifyTileValue[state="ok"] { color: $accent_strong; }
#verifyTileValue[state="warn"] { color: $warn; }
#verifyTileValue[state="info"] { color: $text_bright; }
#verifyTileValue[state="problem"] { color: $danger_border; }
#verifyTileCaption {
    color: $text_dim;
    font-size: 12px;
}
#verifyFinding {
    background-color: $input;
    border: 1px solid $border;
    border-left: 3px solid $border_strong;
    border-radius: 8px;
}
#verifyFinding[level="ok"] { border-left-color: $accent_strong; }
#verifyFinding[level="info"] { border-left-color: $text_info; }
#verifyFinding[level="warn"] { border-left-color: $warn; }
#verifyFinding[level="problem"] { border-left-color: $danger_border; }
#verifyFindingIcon {
    font-size: 14px;
    font-weight: bold;
    color: $text_info;
}
#verifyFindingIcon[level="ok"] { color: $accent_strong; }
#verifyFindingIcon[level="warn"] { color: $warn; }
#verifyFindingIcon[level="problem"] { color: $danger_border; }
#verifyFindingTitle {
    color: $text_bright;
    font-weight: bold;
}
#verifyFindingDetail {
    color: $text_info;
    font-size: 12px;
}
#verifyFindingItems {
    color: $text_mono;
    background-color: $mono;
    border: 1px solid $border;
    border-radius: 5px;
    padding: 6px 8px;
    font-family: monospace;
    font-size: 12px;
}
#installPage QLabel#section3 {
    color: $accent_section;
    font-size: 13px;
    font-weight: bold;
    letter-spacing: 0.5px;
}
#installPage QGroupBox, #updatePage QGroupBox {
    margin-top: 12px;
    padding-top: 10px;
    background-color: transparent;
}
#installPage QGroupBox {
    border: none;
}
#installPage QGroupBox::title {
    left: 0;
    padding: 0;
}
#installPage QGroupBox::title, #updatePage QGroupBox::title {
    font-size: 13px;
    font-weight: bold;
}
#installPage QLineEdit {
    min-height: 30px;
}
#installPage QLineEdit#pathEdit:hover, #playPage QLineEdit#pathEdit:hover {
    border-color: $focus;
}
#installPage QLineEdit#pathEdit:disabled, #playPage QLineEdit#pathEdit:disabled {
    color: $text_disabled;
}
#installPage QCheckBox, #updatePage QCheckBox {
    padding: 3px 0;
}
#installPage QFrame#installDivider,
#playPage QFrame#installDivider,
#updatePage QFrame#installDivider {
    background-color: $border;
}
QFrame#rowDivider {
    background-color: $border;
}
QLabel#section3 {
    color: $accent_section;
}
#statusReady, #statusNotReady, #statusOptional, #statusChecking {
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 1px;
    padding: 3px 8px;
    border-radius: 4px;
}
#statusReady {
    color: $accent_text;
    background-color: $accent;
}
#statusNotReady {
    color: $text_bright;
    background-color: $danger_bg;
}
#statusOptional {
    color: $warn_text;
    background-color: $warn;
}
#statusChecking {
    color: $text_dim;
    background-color: $checking_bg;
}
#section1 {
    font-size: 20px;
    font-weight: bold;
    color: $text_bright;
}
#modCounter {
    font-size: 20px;
    font-weight: bold;
    color: $accent_strong;
}
/* A button showing only a glyph (⋯, ⟳): larger glyph, no menu arrow. */
QPushButton#iconButton {
    font-size: 18px;
    padding: 0;
}
QPushButton#iconButton::menu-indicator {
    image: none;
    width: 0;
}
#vDivider {
    background-color: $border;
}
#statValue {
    font-size: 22px;
    font-weight: bold;
    color: $accent_strong;
}
#statCaption {
    color: $text_dim;
}
#achievementsButton:disabled, #pillButton:disabled {
    color: $text_disabled;
}
#achievementName {
    font-weight: bold;
    color: $text_bright;
}
#achievementNameLocked {
    color: $text_dim;
}
QListWidget#achievementList::item, QListWidget#helpTopics::item {
    border-left: 3px solid transparent;
}
QListWidget#achievementList::item:selected, QListWidget#helpTopics::item:selected {
    background-color: $btn_hover;
    border-left: 3px solid $accent_strong;
}
QListWidget#helpTopics {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 4px;
}
QListWidget#helpTopics::item {
    padding: 8px 10px;
    color: $text;
}
QListWidget#helpTopics::item:selected {
    color: $text_bright;
}
#helpTerm {
    font-weight: bold;
    color: $text_bright;
}
QPushButton#docSection {
    background: transparent;
    border: none;
    padding: 0;
    text-align: left;
    font-size: 15px;
    font-weight: bold;
    color: $accent_section;
}
QPushButton#docSection:hover {
    color: $accent_strong;
}
QProgressBar#storageMeter, QProgressBar#storageMeterLow {
    border: none;
    border-radius: 4px;
    max-height: 8px;
    min-height: 8px;
}
QProgressBar#storageMeter::chunk {
    border-radius: 4px;
}
QProgressBar#storageMeterLow::chunk {
    background: $warn;
    border-radius: 4px;
}
QProgressBar#achievementMeter {
    border: none;
    border-radius: 2px;
    max-height: 4px;
    min-height: 4px;
}
QProgressBar#achievementMeter::chunk {
    border-radius: 2px;
}
#section2 {
    font-size: 15px;
    font-weight: bold;
    color: $accent_section;
}
#info {
    color: $text_info;
}
#dim {
    color: $text_dim;
}
#accent {
    color: $accent_strong;
}
#warn {
    color: $warn;
}
#mono {
    font-family: "DejaVu Sans Mono", monospace;
    font-size: 12px;
    color: $text_mono;
    background-color: $mono;
    border: 1px solid $border;
    border-radius: 5px;
    padding: 8px;
}
QLabel {
    background: transparent;
}
QCheckBox {
    background: transparent;
    color: $text;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 2px solid $border_strong;
    border-radius: 4px;
    background-color: $input;
}
QCheckBox::indicator:checked {
    background-color: $primary;
    border-color: $accent_strong;
}
QCheckBox::indicator:hover {
    border-color: $accent_strong;
}
QPushButton {
    background-color: $btn;
    border: 1px solid $border_strong;
    border-radius: 6px;
    padding: 7px 14px;
    color: $text_btn;
}
QPushButton:hover {
    background-color: $btn_hover;
}
QPushButton:pressed {
    background-color: $btn_pressed;
}
QPushButton:disabled {
    color: $text_disabled;
    background-color: $btn_disabled;
}
QPushButton#primary {
    background-color: $primary;
    border: 1px solid $accent_strong;
    color: $accent_text;
    font-weight: bold;
}
QPushButton#primary:hover {
    background-color: $primary_hover;
}
QPushButton#primary:disabled {
    background-color: $primary_disabled_bg;
    border: 1px solid $primary_disabled_border;
    color: $primary_disabled_text;
}
QPushButton#hero {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 $hero1, stop: 1 $hero2
    );
    border: 1px solid $hero_border;
    border-radius: 10px;
    padding: 16px 28px;
    color: $hero_text;
    font-size: 17px;
    font-weight: bold;
}
QPushButton#hero:hover {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 $hero_hover1, stop: 1 $hero_hover2
    );
}
QPushButton#hero:disabled {
    background: $btn;
    border: 1px solid $border_strong;
    color: $text_disabled;
}
QPushButton#secondary {
    background-color: $btn;
    border: 1px solid $border_secondary;
    border-radius: 10px;
    padding: 16px 20px;
    color: $text_btn_hover;
    font-size: 14px;
}
/* A secondary button sitting beside a primary one (Undo last update):
   the primary's height and font, keeping the secondary colours. */
QPushButton#secondary[besidePrimary="true"] {
    padding-top: 0;
    padding-bottom: 0;
    font-size: %FONT_SIZE%;
    font-weight: bold;
}
/* A small secondary button in a card's footer (Choose DLL). */
QPushButton#secondary[compact="true"] {
    padding: 6px 14px;
    font-size: 13px;
}
QPushButton#secondary:hover {
    background-color: $btn_hover;
    border-color: $secondary_hover_border;
}
QPushButton#secondary:disabled {
    color: $text_disabled;
    background-color: $btn_disabled;
    border: 1px solid $btn_disabled;
}
QPushButton#copyCommand {
    background-color: $btn;
    border: 1px solid $border_secondary;
    border-radius: 8px;
    padding: 4px 10px;
    color: $text_btn_hover;
    font-size: 12px;
}
QPushButton#copyCommand:hover {
    background-color: $btn_hover;
    border-color: $secondary_hover_border;
}
QPushButton#copyCommand:disabled {
    color: $text_disabled;
    background-color: $btn_disabled;
    border: 1px solid $btn_disabled;
}
QPushButton#consoleToggle {
    background-color: $btn;
    border: 1px solid $border_secondary;
    border-radius: 6px;
    padding: 2px 6px;
    color: $text_btn_hover;
    font-size: 11px;
}
QPushButton#consoleToggle:hover {
    background-color: $btn_hover;
    border-color: $secondary_hover_border;
}
QPushButton#consoleToggle:disabled {
    color: $text_disabled;
    background-color: $btn_disabled;
    border-color: $btn_disabled;
}
/* Multiplayer settings tabs: the main nav bar's look, an accent
   underline on the picked tab, resting on the divider below. */
QPushButton#subTab {
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 0 14px;
    color: $text_nav;
    font-weight: 600;
}
QPushButton#subTab:hover {
    color: $text_bright;
    border-bottom-color: $border_strong;
}
QPushButton#subTab:checked {
    color: $accent_strong;
    border-bottom-color: $accent_strong;
}
QPushButton#subTab:disabled {
    color: $text_disabled;
    border-bottom-color: transparent;
}
/* Multiplayer page tabs: the Dashboard's pill buttons, latched when picked. */
QPushButton#segment {
    background: transparent;
    border: 1px solid $border;
    border-radius: 17px;
    padding: 0 16px;
    color: $text;
    font-weight: 600;
}
QPushButton#segment:hover {
    background-color: $btn_hover;
    border-color: $accent_strong;
    color: $accent_strong;
}
QPushButton#segment:checked {
    background-color: $chip;
    border-color: $accent_strong;
    color: $accent_strong;
}
QPushButton#segment:disabled {
    background: transparent;
    border-color: $border;
    border-style: dashed;
    color: $text_disabled;
}
/* Locked while picked (frame generation mid-game): still shows the pick. */
QPushButton#segment:checked:disabled {
    background-color: $chip;
    border-color: $border_strong;
    border-style: solid;
    color: $text;
}
QPushButton#tertiary {
    background: transparent;
    border: none;
    color: $text_info;
    text-decoration: underline;
    padding: 4px 8px;
}
QPushButton#tertiary:disabled {
    color: $text_disabled;
    background: transparent;
}
QPushButton#tertiary:hover {
    color: $text_tertiary_hover;
}
/* Settings rows: the setting's name over a dim line, and a compact
   action on the right that lights up in the accent on hover. */
QLabel#settingTitle {
    color: $text_bright;
    font-weight: 600;
}
QPushButton#settingAction, QPushButton#settingDanger {
    background-color: $btn;
    border: 1px solid $border_secondary;
    border-radius: 6px;
    padding: 5px 16px;
    color: $text_btn_hover;
    font-weight: 600;
}
QPushButton#settingAction:hover, QPushButton#settingAction:checked {
    background-color: $chip;
    border-color: $accent_strong;
    color: $accent_strong;
}
QPushButton#settingDanger:hover {
    background-color: $danger_bg;
    border-color: $danger_border;
    color: $danger_text;
}
QPushButton#settingAction:pressed, QPushButton#settingDanger:pressed {
    background-color: $btn_pressed;
}
QPushButton#settingAction:disabled, QPushButton#settingDanger:disabled {
    color: $text_disabled;
    background-color: $btn_disabled;
    border-color: $btn_disabled;
}
#chip {
    background-color: $chip;
    border: 1px solid $border_input;
    border-radius: 12px;
    padding: 4px 12px;
    color: $text_info;
}
#chip[state="ok"] {
    color: $chip_ok;
    border-color: $chip_ok_border;
}
#chip[state="bad"] {
    color: $text_dim;
}
QPushButton#danger {
    background-color: $danger_bg;
    border: 1px solid $danger_border;
    color: $danger_text;
}
QPushButton#danger:hover {
    background-color: $danger_hover;
}
QPushButton#danger:disabled {
    background-color: $danger_disabled_bg;
    border: 1px solid $danger_disabled_border;
    color: $danger_disabled_text;
}
QLineEdit, QSpinBox, QComboBox {
    background-color: $input;
    border: 1px solid $border_input;
    border-radius: 5px;
    padding: 5px 8px;
    color: $text_btn;
    selection-background-color: $selection;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border-color: $focus;
}
QComboBox {
    padding-right: 26px;
}
/* Borderless chevron instead of the native boxed arrow. */
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 24px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    image: url("%COMBO_ARROW%");
    width: 10px;
    height: 6px;
}
QComboBox QAbstractItemView {
    background-color: $card;
    border: 1px solid $border_input;
    selection-background-color: $selection;
}
QComboBox::separator {
    height: 1px;
    background: $border_input;
    margin: 4px 8px;
}
QProgressBar {
    background-color: $input;
    border: 1px solid $border_input;
    border-radius: 5px;
    text-align: center;
    color: $text_btn;
}
QProgressBar::chunk {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 $hero1, stop: 1 $accent_strong
    );
    border-radius: 4px;
}
QProgressBar[full="true"] {
    color: $accent_text;
}
/* Verify Integrity results that aren't a pass (see set_success_state). */
QProgressBar[level="snapshot"]::chunk { background: $text_info; }
QProgressBar[level="attention"]::chunk { background: $warn; }
QProgressBar::text {
    color: $text_btn;
    background: rgba(0, 0, 0, 0.55);
    padding: 1px 6px;
    border-radius: 3px;
}
QListWidget#profileCards {
    background: transparent;
    border: none;
    outline: none;
}
QListWidget#profileCards::item,
QListWidget#profileCards::item:hover,
QListWidget#profileCards::item:selected {
    background: transparent;
    border: none;
    color: $text;
}
QFrame#profileCard {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 8px;
}
QFrame#profileCard:hover {
    border-color: $border_strong;
}
QFrame#profileCard[selected="true"] {
    border-color: $accent_strong;
}
QFrame#profileCard[active="true"] {
    border: 2px solid $accent;
}
/* Dashboard Installation status rows: plain until hovered, then a
   soft tile shows the row can be clicked. */
QFrame#componentRow {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
}
QFrame#componentRow:hover {
    background-color: $input;
    border-color: $border;
}
QFrame#statTile {
    background-color: $input;
    border: 1px solid $border;
    border-radius: 8px;
}
#statValue[state="bad"] {
    color: $text_dim;
}
QLabel#fieldLabel {
    color: $text_info;
    font-size: 12px;
    font-weight: bold;
}
QLabel#cardTitle {
    color: $text_bright;
    font-weight: bold;
}
QPlainTextEdit#noteEdit {
    background-color: $input;
    color: $text_btn;
    font-family: %FONT_FAMILY%;
    font-size: %FONT_SIZE%;
    padding: 4px;
}
QPlainTextEdit#noteEdit:focus {
    border-color: $focus;
}
/* Tall enough for the whole handle: a shorter slider clips it into an oval. */
QSlider:horizontal {
    min-height: 22px;
}
QSlider::groove:horizontal {
    height: 6px;
    background-color: $input;
    border: 1px solid $border_input;
    border-radius: 3px;
}
QSlider::sub-page:horizontal {
    background-color: $accent_strong;
    border-radius: 3px;
}
/* The COMMANDER logo, 18px across and centred on the 6px groove. */
QSlider::handle:horizontal {
    image: url("%SLIDER_HANDLE%");
    width: 18px;
    height: 18px;
    margin: -6px 0;
}
/* -50..50 sliders: no fill, so the handle in the middle reads as "unchanged". */
QSlider[signed="true"]::sub-page:horizontal {
    background: transparent;
}
QSlider::sub-page:horizontal:disabled {
    background-color: $border_input;
}
QTableWidget, QListWidget {
    background-color: $input;
    border: 1px solid $border_input;
    border-radius: 5px;
    color: $text;
    gridline-color: $gridline;
}
QHeaderView::section {
    background-color: $card;
    color: $text_info;
    border: none;
    border-bottom: 1px solid $border_input;
    padding: 5px;
    font-weight: bold;
}
QTableWidget::item:selected, QListWidget::item:selected {
    background-color: $selection;
    color: $selection_text;
}
QTreeWidget {
    background-color: $input;
    border: 1px solid $border_input;
    border-radius: 5px;
    color: $text;
}
QTreeWidget::item {
    padding: 3px 4px;
    border: none;
}
QTreeWidget::item:selected {
    background-color: $selection;
    color: $selection_text;
}
QTreeWidget::branch {
    background: transparent;
}
QTreeWidget::indicator {
    width: 16px;
    height: 16px;
    border: 2px solid $border_strong;
    border-radius: 3px;
    background-color: $input;
}
QTreeWidget::indicator:checked {
    background-color: $primary;
    border-color: $accent_strong;
}
QTreeWidget::indicator:unchecked {
    background-color: $input;
}
QPlainTextEdit {
    background-color: $mono;
    color: $text_mono;
    border: 1px solid $border_input;
    border-radius: 5px;
    font-family: "DejaVu Sans Mono", monospace;
    font-size: 12px;
}
QGroupBox {
    border: 1px solid $border_input;
    border-radius: 6px;
    margin-top: 8px;
    padding-top: 6px;
    color: $text_info;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: $accent_section;
}
QStatusBar {
    /* Flush with the app: the window's backdrop shows through. */
    background: transparent;
    color: $text_dim;
    border: none;
}
QStatusBar::item {
    border: none;
}
QStatusBar QLabel {
    background: transparent;
}
QPushButton#discordButton {
    background-color: #4E59CF;
    border: 1px solid #4E59CF;
    color: #f2f3ff;
    font-weight: bold;
}
QPushButton#discordButton:hover {
    background-color: #5865F2;
    border: 1px solid #5865F2;
}
QPushButton#githubLink, QPushButton#discordLink {
    color: $link;
    padding: 0 2px 0 8px;
    border: none;
    background: transparent;
}
QPushButton#githubLink:hover, QPushButton#discordLink:hover {
    color: $link_hover;
    background: transparent;
}
QPushButton#commanderUpdateStatus {
    padding: 0 4px;
    border: none;
    background: transparent;
    font-size: 11px;
}
QPushButton#commanderUpdateStatus:disabled {
    background: transparent;
}
QFrame#updateBanner {
    background-color: $card;
    border: none;
    border-top: 1px solid $border;
    border-bottom: 2px solid $accent_strong;
}
QLabel#updateBannerText {
    color: $accent;
    font-weight: bold;
    background: transparent;
}
QPushButton#updateBannerClose {
    background: transparent;
    border: none;
    padding: 0;
    min-width: 0;
    font-size: 15px;
    color: $text_dim;
}
QPushButton#updateBannerClose:hover {
    color: $text_bright;
}
QMessageBox, QFileDialog, QDialog#messageDialog, QDialog#popupWindow {
    background-color: $card;
}
QDialog#messageDialog, QDialog#popupWindow {
    border: 1px solid $border_strong;
}
QToolTip {
    color: $text_info;
}
QRadioButton {
    background: transparent;
    color: $text;
    spacing: 8px;
}
QRadioButton::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid $border_strong;
    border-radius: 7px;
    background-color: $input;
}
QRadioButton::indicator:checked {
    border: 1px solid $accent_strong;
    background-color: $selection;
}
QRadioButton::indicator:hover {
    border-color: $secondary_hover_border;
}
""")

_FONT_SIZE_RE = re.compile(r"font-size:\s*(\d+)px")

THEMES: dict[str, dict[str, str]] = {
    "gamma": {
        "font_size": "13px",
        "bg": "#0d130c",
        "page": "rgba(10, 14, 9, 180)",
        "topbar": "rgba(7, 11, 7, 240)",
        "card": "#141b15",
        "input": "#0d130e",
        "mono": "#0a0f0a",
        "btn": "#1f2b20",
        "btn_hover": "#283828",
        "btn_pressed": "#17201a",
        "btn_disabled": "#161e16",
        "chip": "#18221a",
        "border": "#26321f",
        "border_strong": "#33452f",
        "border_input": "#2e3b2c",
        "border_secondary": "#3a4f35",
        "gridline": "#1c261b",
        "text": "#e2ead8",
        "text_bright": "#eef4e8",
        "text_btn": "#e8f0df",
        "text_btn_hover": "#d8e6cf",
        "text_nav": "#8fa387",
        "text_info": "#9aa88f",
        "text_dim": "#7f8f78",
        "text_mono": "#c8d8bc",
        "text_disabled": "#5f6e5a",
        "text_tertiary_hover": "#c8e2a0",
        "accent": "#9fe96f",
        "accent_strong": "#8fe45c",
        "storage_a": "#8fe45c",
        "storage_b": "#3f7f2c",
        "storage_c": "#9aa596",
        "storage_d": "#a3ad5c",
        "accent_text": "#0c130a",
        "warn_text": "#0c130a",
        "accent_section": "#a8d66f",
        "primary": "#5fb548",
        "primary_hover": "#6fc95a",
        "primary_disabled_bg": "#2a3d27",
        "primary_disabled_border": "#3c5038",
        "primary_disabled_text": "#6f8367",
        "hero1": "#4fa63c",
        "hero2": "#6fc95a",
        "hero_hover1": "#58b843",
        "hero_hover2": "#7ad964",
        "hero_border": "#9fe96f",
        "hero_text": "#0c130a",
        "secondary_hover_border": "#6fc95a",
        "selection": "#5fb548",
        "selection_text": "#0c130a",
        "focus": "#8fe45c",
        "chip_ok": "#9fe96f",
        "chip_ok_border": "#3a5a30",
        "checking_bg": "#242c24",
        "warn": "#d9a04c",
        "danger_bg": "#7a2f2a",
        "danger_border": "#c0554f",
        "danger_text": "#ffe6dd",
        "danger_hover": "#c74e46",
        "danger_disabled_bg": "#321b1a",
        "danger_disabled_border": "#57302d",
        "danger_disabled_text": "#80635e",
        "link": "#ffffff",
        "link_hover": "#dfe8df",
        "back_base_a": "#101708",
        "back_base_b": "#070a05",
        "back_glow1_rgb": "122, 217, 90",
        "back_glow1_a": "70",
        "back_glow1b_rgb": "58, 120, 44",
        "back_glow1b_a": "40",
        "back_glow1c_rgb": "26, 55, 22",
        "back_glow1c_a": "18",
        "back_glow2_rgb": "50, 95, 38",
        "back_glow2_a": "55",
        "pal_window": "#0d130c",
        "pal_window_text": "#e2ead8",
        "pal_base": "#0d130e",
        "pal_alternate": "#141b15",
        "pal_text": "#e2ead8",
        "pal_button": "#1f2b20",
        "pal_button_text": "#e8f0df",
        "pal_highlight": "#5fb548",
        "pal_highlighted_text": "#0c130a",
    },
    "midnight": {
        "font_size": "13px",
        "bg": "#0b1016",
        "page": "rgba(11, 16, 23, 180)",
        "topbar": "rgba(9, 12, 18, 240)",
        "card": "#111824",
        "input": "#0c1219",
        "mono": "#070b10",
        "btn": "#1a2432",
        "btn_hover": "#223048",
        "btn_pressed": "#141c28",
        "btn_disabled": "#101620",
        "chip": "#141c28",
        "border": "#1d2a3a",
        "border_strong": "#2a3a4e",
        "border_input": "#1d2a3a",
        "border_secondary": "#2e4257",
        "gridline": "#1a2432",
        "text": "#dbe6f0",
        "text_bright": "#eef4f9",
        "text_btn": "#e2ecf4",
        "text_btn_hover": "#cfe0ee",
        "text_nav": "#8aa3b8",
        "text_info": "#93a8ba",
        "text_dim": "#6e8496",
        "text_mono": "#b8cbd9",
        "text_disabled": "#4f6272",
        "text_tertiary_hover": "#bcd2e0",
        "accent": "#6fd3a8",
        "accent_strong": "#5ec99b",
        "storage_a": "#5ec99b",
        "storage_b": "#2d6e57",
        "storage_c": "#8aa0b2",
        "storage_d": "#8fae7a",
        "accent_text": "#0b1511",
        "warn_text": "#0b1511",
        "accent_section": "#7fceab",
        "primary": "#3f9d7a",
        "primary_hover": "#4bb38c",
        "primary_disabled_bg": "#1c3329",
        "primary_disabled_border": "#2a4537",
        "primary_disabled_text": "#5a7a68",
        "hero1": "#2f8a6a",
        "hero2": "#4bb38c",
        "hero_hover1": "#379977",
        "hero_hover2": "#57bf97",
        "hero_border": "#6fd3a8",
        "hero_text": "#0b1511",
        "secondary_hover_border": "#4bb38c",
        "selection": "#3f9d7a",
        "selection_text": "#0b1511",
        "focus": "#5ec99b",
        "chip_ok": "#6fd3a8",
        "chip_ok_border": "#2a5342",
        "checking_bg": "#202933",
        "warn": "#d9a04c",
        "danger_bg": "#6e2f2a",
        "danger_border": "#b0554f",
        "danger_text": "#ffe6dd",
        "danger_hover": "#b84940",
        "danger_disabled_bg": "#2c1b1a",
        "danger_disabled_border": "#4d302d",
        "danger_disabled_text": "#7a635e",
        "link": "#e6f0f7",
        "link_hover": "#ffffff",
        "back_base_a": "#0c1420",
        "back_base_b": "#060a10",
        "back_glow1_rgb": "102, 189, 160",
        "back_glow1_a": "60",
        "back_glow1b_rgb": "48, 110, 92",
        "back_glow1b_a": "34",
        "back_glow1c_rgb": "22, 50, 42",
        "back_glow1c_a": "16",
        "back_glow2_rgb": "42, 90, 74",
        "back_glow2_a": "45",
        "pal_window": "#0b1016",
        "pal_window_text": "#dbe6f0",
        "pal_base": "#0c1219",
        "pal_alternate": "#111824",
        "pal_text": "#dbe6f0",
        "pal_button": "#1a2432",
        "pal_button_text": "#e2ecf4",
        "pal_highlight": "#3f9d7a",
        "pal_highlighted_text": "#0b1511",
    },
    "black": {
        "font_size": "13px",
        "bg": "#000000",
        "page": "rgba(0, 0, 0, 180)",
        "topbar": "rgba(0, 0, 0, 240)",
        "card": "#0a0a0a",
        "input": "#000000",
        "mono": "#050505",
        "btn": "#141414",
        "btn_hover": "#1d1d1d",
        "btn_pressed": "#0d0d0d",
        "btn_disabled": "#0c0c0c",
        "chip": "#0e0e0e",
        "border": "#1f1f1f",
        "border_strong": "#2b2b2b",
        "border_input": "#1f1f1f",
        "border_secondary": "#2e2e2e",
        "gridline": "#171717",
        "text": "#e0e0e0",
        "text_bright": "#f0f0f0",
        "text_btn": "#e8e8e8",
        "text_btn_hover": "#ffffff",
        "text_nav": "#8a8a8a",
        "text_info": "#9a9a9a",
        "text_dim": "#777777",
        "text_mono": "#c0c0c0",
        "text_disabled": "#555555",
        "text_tertiary_hover": "#b0b0b0",
        "accent": "#9fe96f",
        "accent_strong": "#8fe45c",
        "storage_a": "#8fe45c",
        "storage_b": "#3f7f2c",
        "storage_c": "#8a8a8a",
        "storage_d": "#a3ad5c",
        "accent_text": "#0c130a",
        "warn_text": "#0c130a",
        "accent_section": "#a8d66f",
        "primary": "#5fb548",
        "primary_hover": "#6fc95a",
        "primary_disabled_bg": "#1a2418",
        "primary_disabled_border": "#2c3827",
        "primary_disabled_text": "#5f6e5a",
        "hero1": "#4fa63c",
        "hero2": "#6fc95a",
        "hero_hover1": "#58b843",
        "hero_hover2": "#7ad964",
        "hero_border": "#9fe96f",
        "hero_text": "#0c130a",
        "secondary_hover_border": "#6fc95a",
        "selection": "#5fb548",
        "selection_text": "#0c130a",
        "focus": "#8fe45c",
        "chip_ok": "#9fe96f",
        "chip_ok_border": "#2c3c24",
        "checking_bg": "#25212e",
        "warn": "#d9a04c",
        "danger_bg": "#4d1f1b",
        "danger_border": "#8f3f3a",
        "danger_text": "#ffd9d4",
        "danger_hover": "#a03b34",
        "danger_disabled_bg": "#1f1211",
        "danger_disabled_border": "#38201e",
        "danger_disabled_text": "#6b4a47",
        "link": "#ffffff",
        "link_hover": "#bfbfbf",
        "back_base_a": "#050505",
        "back_base_b": "#000000",
        "back_glow1_rgb": "122, 217, 90",
        "back_glow1_a": "32",
        "back_glow1b_rgb": "58, 120, 44",
        "back_glow1b_a": "18",
        "back_glow1c_rgb": "26, 55, 22",
        "back_glow1c_a": "8",
        "back_glow2_rgb": "50, 95, 38",
        "back_glow2_a": "25",
        "pal_window": "#000000",
        "pal_window_text": "#e0e0e0",
        "pal_base": "#000000",
        "pal_alternate": "#0a0a0a",
        "pal_text": "#e0e0e0",
        "pal_button": "#141414",
        "pal_button_text": "#e8e8e8",
        "pal_highlight": "#5fb548",
        "pal_highlighted_text": "#0c130a",
    },
    "reactor": {
        "font_size": "13px",
        "bg": "#010a0a",
        "page": "rgba(1, 10, 10, 185)",
        "topbar": "rgba(0, 6, 6, 245)",
        "card": "#061616",
        "input": "#020f0f",
        "mono": "#010808",
        "btn": "#0a2020",
        "btn_hover": "#0f2e2e",
        "btn_pressed": "#071818",
        "btn_disabled": "#081717",
        "chip": "#081a1a",
        "border": "#0f3232",
        "border_strong": "#184c4c",
        "border_input": "#123d3d",
        "border_secondary": "#1c5252",
        "gridline": "#0c2828",
        "text": "#cdeaea",
        "text_bright": "#e8fbfb",
        "text_btn": "#d8f3f3",
        "text_btn_hover": "#c2e9e9",
        "text_nav": "#568c8c",
        "text_info": "#6ba3a3",
        "text_dim": "#4a7a7a",
        "text_mono": "#aed9d9",
        "text_disabled": "#385e5e",
        "text_tertiary_hover": "#9fe2e2",
        "accent": "#006868",
        "accent_strong": "#00a3a3",
        "storage_a": "#00a3a3",
        "storage_b": "#035e5e",
        "storage_c": "#8fb3b3",
        "storage_d": "#6fae8f",
        "accent_text": "#eafcfc",
        "warn_text": "#001515",
        "accent_section": "#3aabab",
        "primary": "#047777",
        "primary_hover": "#0a9494",
        "primary_disabled_bg": "#123c3c",
        "primary_disabled_border": "#1f5555",
        "primary_disabled_text": "#4d8080",
        "hero1": "#036363",
        "hero2": "#0aa3a3",
        "hero_hover1": "#057676",
        "hero_hover2": "#14bcbc",
        "hero_border": "#00c2c2",
        "hero_text": "#ffffff",
        "secondary_hover_border": "#0a9494",
        "selection": "#047777",
        "selection_text": "#eafcfc",
        "focus": "#00a3a3",
        "chip_ok": "#00a3a3",
        "chip_ok_border": "#1f5c5c",
        "checking_bg": "#122a2a",
        "warn": "#d9a04c",
        "danger_bg": "#6e2f2a",
        "danger_border": "#b0554f",
        "danger_text": "#ffe6dd",
        "danger_hover": "#b84940",
        "danger_disabled_bg": "#2c1b1a",
        "danger_disabled_border": "#4d302d",
        "danger_disabled_text": "#7a635e",
        "link": "#8fe0e0",
        "link_hover": "#ffffff",
        "back_base_a": "#020f0f",
        "back_base_b": "#000505",
        "back_glow1_rgb": "0, 168, 168",
        "back_glow1_a": "55",
        "back_glow1b_rgb": "0, 104, 104",
        "back_glow1b_a": "35",
        "back_glow1c_rgb": "0, 50, 50",
        "back_glow1c_a": "16",
        "back_glow2_rgb": "10, 90, 90",
        "back_glow2_a": "42",
        "pal_window": "#010a0a",
        "pal_window_text": "#cdeaea",
        "pal_base": "#020f0f",
        "pal_alternate": "#061616",
        "pal_text": "#cdeaea",
        "pal_button": "#0a2020",
        "pal_button_text": "#d8f3f3",
        "pal_highlight": "#047777",
        "pal_highlighted_text": "#eafcfc",
    },
    "winter": {
        "font_size": "13px",
        "bg": "#dfe5ea",
        "page": "rgba(225, 232, 238, 40)",
        "topbar": "rgba(232, 238, 243, 215)",
        "card": "rgba(240, 244, 248, 160)",
        "input": "rgba(255, 255, 255, 190)",
        "mono": "#f1f4f7",
        "btn": "rgba(255, 255, 255, 150)",
        "btn_hover": "rgba(255, 255, 255, 215)",
        "btn_pressed": "rgba(206, 215, 223, 220)",
        "btn_disabled": "rgba(226, 231, 236, 130)",
        "chip": "rgba(255, 255, 255, 165)",
        "border": "#c4ced6",
        "border_strong": "#a9b5bf",
        "border_input": "#b9c4cc",
        "border_secondary": "#9db29b",
        "gridline": "#d2dae0",
        "text": "#1b252d",
        "text_bright": "#0f171d",
        "text_btn": "#1b252d",
        "text_btn_hover": "#0f171d",
        "text_nav": "#4f5d68",
        "text_info": "#4c5a64",
        "text_dim": "#56636d",
        "text_mono": "#26323a",
        "text_disabled": "#97a2aa",
        "text_tertiary_hover": "#2f6a2c",
        "accent": "#3a6d37",
        "accent_strong": "#2f6d2b",
        "storage_a": "#62945a",
        "storage_b": "#3a6d37",
        "storage_c": "#8f9ba5",
        "storage_d": "#9f9a63",
        "accent_text": "#ffffff",
        "warn_text": "#1b1206",
        "accent_section": "#3a6d37",
        "primary": "#3f7a3a",
        "primary_hover": "#4c8c45",
        "primary_disabled_bg": "#c9d3cb",
        "primary_disabled_border": "#b6c3b8",
        "primary_disabled_text": "#7f8c82",
        "hero1": "#3a6d37",
        "hero2": "#62945a",
        "hero_hover1": "#427c3e",
        "hero_hover2": "#6fa266",
        "hero_border": "#2f5f2c",
        "hero_text": "#ffffff",
        "secondary_hover_border": "#4c8c45",
        "selection": "#3f7a3a",
        "selection_text": "#ffffff",
        "focus": "#3f7a3a",
        "chip_ok": "#2f6d2b",
        "chip_ok_border": "#9fc49a",
        "checking_bg": "#d7dee4",
        "warn": "#a86a12",
        "danger_bg": "#b84a40",
        "danger_border": "#9a3a32",
        "danger_text": "#ffffff",
        "danger_hover": "#c9574c",
        "danger_disabled_bg": "#e3cfcc",
        "danger_disabled_border": "#d1b5b1",
        "danger_disabled_text": "#9a7f7b",
        "link": "#2f6d2b",
        "link_hover": "#1f4f1c",
        # The fixed status colours (ui/common.py) are too pale on white.
        "status_ok": "#2f7a2a",
        "status_warn": "#9a5f0c",
        "status_red": "#b2362f",
        "status_grey": "#5f6b74",
        "status_item": "#1b252d",
        "status_light": "#3a4650",
        # Painted over the gradient: a pre-blurred village scene (the cards
        # read as frosted glass over it) with falling snow on top.
        "back_image": "winter/backdrop.jpg",
        "back_snow": "1",
        "back_lights": "1",  # fairy lights around the wordmark
        "back_base_a": "#e3e9ee",
        "back_base_b": "#c3ccd4",
        "back_glow1_rgb": "255, 255, 255",
        "back_glow1_a": "60",
        "back_glow1b_rgb": "240, 244, 248",
        "back_glow1b_a": "40",
        "back_glow1c_rgb": "220, 228, 234",
        "back_glow1c_a": "20",
        "back_glow2_rgb": "200, 210, 218",
        "back_glow2_a": "40",
        "pal_window": "#dfe5ea",
        "pal_window_text": "#1b252d",
        "pal_base": "#f4f7f9",
        "pal_alternate": "#e8edf1",
        "pal_text": "#1b252d",
        "pal_button": "#eef2f5",
        "pal_button_text": "#1b252d",
        "pal_highlight": "#3f7a3a",
        "pal_highlighted_text": "#ffffff",
    },
}

# Winter follows the time of day. Dawn keeps the light day colours over a
# warmer scene; dusk and night switch to dark frosted glass so the cards stay
# readable over the dark scenes.
THEMES["winter_dawn"] = {
    **THEMES["winter"],
    "back_image": "winter/dawn.jpg",
    "back_base_a": "#ecdcd6",
    "back_base_b": "#c9c3cc",
}
THEMES["winter_night"] = {
    "font_size": "13px",
    "bg": "#11161b",
    "page": "rgba(14, 19, 24, 40)",
    "topbar": "rgba(14, 19, 24, 215)",
    "card": "rgba(22, 29, 36, 175)",
    "input": "rgba(10, 14, 18, 190)",
    "mono": "#0e1317",
    "btn": "rgba(44, 54, 64, 160)",
    "btn_hover": "rgba(58, 70, 82, 210)",
    "btn_pressed": "rgba(28, 36, 44, 220)",
    "btn_disabled": "rgba(30, 38, 46, 120)",
    "chip": "rgba(36, 46, 56, 170)",
    "border": "#2e3a44",
    "border_strong": "#3e4c58",
    "border_input": "#36444f",
    "border_secondary": "#3f5a3c",
    "gridline": "#232d36",
    "text": "#dde5ea",
    "text_bright": "#f2f6f8",
    "text_btn": "#dde5ea",
    "text_btn_hover": "#ffffff",
    "text_nav": "#95a2ac",
    "text_info": "#9aa8b2",
    "text_dim": "#8a97a1",
    "text_mono": "#c9d3da",
    "text_disabled": "#56626c",
    "text_tertiary_hover": "#a6dc8a",
    "accent": "#8fcf72",
    "accent_strong": "#9fe07f",
    "storage_a": "#8fcf72",
    "storage_b": "#4f8a45",
    "storage_c": "#7d8a94",
    "storage_d": "#b3ac6e",
    "accent_text": "#0f1a0c",
    "warn_text": "#1b1206",
    "accent_section": "#8fcf72",
    "primary": "#4f8a45",
    "primary_hover": "#5e9c53",
    "primary_disabled_bg": "#26302a",
    "primary_disabled_border": "#34403a",
    "primary_disabled_text": "#6c7a70",
    "hero1": "#3f7a3a",
    "hero2": "#62945a",
    "hero_hover1": "#4a8a44",
    "hero_hover2": "#72a868",
    "hero_border": "#7fbf6a",
    "hero_text": "#ffffff",
    "secondary_hover_border": "#5e9c53",
    "selection": "#4f8a45",
    "selection_text": "#ffffff",
    "focus": "#8fcf72",
    "chip_ok": "#8fcf72",
    "chip_ok_border": "#3f5a3c",
    "checking_bg": "#1c252d",
    "warn": "#d9a04c",
    "danger_bg": "#7a322b",
    "danger_border": "#b0554f",
    "danger_text": "#ffe6dd",
    "danger_hover": "#9c3e35",
    "danger_disabled_bg": "#2c1e1c",
    "danger_disabled_border": "#4d3330",
    "danger_disabled_text": "#7a6460",
    "link": "#a6dc8a",
    "link_hover": "#ffffff",
    "back_image": "winter/night.jpg",
    "back_snow": "1",
    "back_lights": "1",
    "back_base_a": "#1a2128",
    "back_base_b": "#0b0f13",
    "back_glow1_rgb": "120, 140, 160",
    "back_glow1_a": "30",
    "back_glow1b_rgb": "60, 74, 88",
    "back_glow1b_a": "24",
    "back_glow1c_rgb": "30, 38, 46",
    "back_glow1c_a": "12",
    "back_glow2_rgb": "50, 62, 74",
    "back_glow2_a": "30",
    "pal_window": "#11161b",
    "pal_window_text": "#dde5ea",
    "pal_base": "#0e1317",
    "pal_alternate": "#161d24",
    "pal_text": "#dde5ea",
    "pal_button": "#232d36",
    "pal_button_text": "#dde5ea",
    "pal_highlight": "#4f8a45",
    "pal_highlighted_text": "#ffffff",
}
THEMES["winter_dusk"] = {
    **THEMES["winter_night"],
    "bg": "#1a1517",
    "page": "rgba(26, 20, 22, 40)",
    "topbar": "rgba(26, 20, 22, 215)",
    "card": "rgba(36, 28, 30, 175)",
    "input": "rgba(20, 15, 17, 190)",
    "mono": "#171214",
    "border": "#3e3236",
    "gridline": "#2e2528",
    "checking_bg": "#2a2124",
    "back_image": "winter/dusk.jpg",
    "back_base_a": "#3a2c30",
    "back_base_b": "#16100f",
    "pal_window": "#1a1517",
    "pal_base": "#171214",
    "pal_alternate": "#241c1f",
    "pal_button": "#33282b",
}

# Red Forest (Live): the forester's tower in the Zone's Red Forest, on the
# same clock. Light warm glass by day and dawn, dark glass at dusk and
# night. Ember orange stands in for the usual green: green is lost
# among the red and gold leaves. back_redforest names the scene's swaying tree layers (see
# ui/redforest.py); back_fire lights the campfire (dusk and night).
THEMES["redforest"] = {
    "font_size": "13px",
    "bg": "#efe4d6",
    "page": "rgba(236, 224, 208, 40)",
    "topbar": "rgba(244, 236, 226, 215)",
    "card": "rgba(248, 240, 230, 160)",
    "input": "rgba(255, 252, 247, 190)",
    "mono": "#f6efe6",
    "btn": "rgba(255, 250, 244, 150)",
    "btn_hover": "rgba(255, 250, 244, 215)",
    "btn_pressed": "rgba(226, 212, 196, 220)",
    "btn_disabled": "rgba(238, 228, 216, 130)",
    "chip": "rgba(255, 250, 244, 165)",
    "border": "#d8c6b2",
    "border_strong": "#c4ae96",
    "border_input": "#cfbba5",
    "border_secondary": "#d4a888",
    "gridline": "#e2d4c4",
    "text": "#2a1e18",
    "text_bright": "#1a120d",
    "text_btn": "#2a1e18",
    "text_btn_hover": "#1a120d",
    "text_nav": "#6a5446",
    "text_info": "#65503f",
    "text_dim": "#6f5d52",
    "text_mono": "#33261d",
    "text_disabled": "#ab9a8c",
    "text_tertiary_hover": "#8a3a12",
    "accent": "#a8481a",
    "accent_strong": "#9a3f14",
    "storage_a": "#d9782e",
    "storage_b": "#a8481a",
    "storage_c": "#a8958a",
    "storage_d": "#9f9a63",
    "accent_text": "#ffffff",
    "warn_text": "#1b1206",
    "accent_section": "#a8481a",
    "primary": "#b5521f",
    "primary_hover": "#c8622a",
    "primary_disabled_bg": "#e0d2c4",
    "primary_disabled_border": "#cdbba8",
    "primary_disabled_text": "#8f7f72",
    "hero1": "#a8481a",
    "hero2": "#d9782e",
    "hero_hover1": "#b8521f",
    "hero_hover2": "#e68a3c",
    "hero_border": "#8a3a12",
    "hero_text": "#ffffff",
    "secondary_hover_border": "#c8622a",
    "selection": "#b5521f",
    "selection_text": "#ffffff",
    "focus": "#b5521f",
    "chip_ok": "#9a3f14",
    "chip_ok_border": "#e0b090",
    "checking_bg": "#eadccc",
    "warn": "#a86a12",
    "danger_bg": "#b84a40",
    "danger_border": "#9a3a32",
    "danger_text": "#ffffff",
    "danger_hover": "#c9574c",
    "danger_disabled_bg": "#ecd6cf",
    "danger_disabled_border": "#dcbdb4",
    "danger_disabled_text": "#9a7f7b",
    "link": "#9a3f14",
    "link_hover": "#6a2a08",
    "status_ok": "#2f7a2a",
    "status_warn": "#9a5f0c",
    "status_red": "#b2362f",
    "status_grey": "#6f6258",
    "status_item": "#2a1e18",
    "status_light": "#4a3a30",
    "back_image": "redforest/day.jpg",
    "back_base_a": "#f0e4d6",
    "back_base_b": "#d6bba0",
    "back_glow1_rgb": "255, 248, 238",
    "back_glow1_a": "60",
    "back_glow1b_rgb": "246, 236, 224",
    "back_glow1b_a": "40",
    "back_glow1c_rgb": "232, 218, 202",
    "back_glow1c_a": "20",
    "back_glow2_rgb": "214, 196, 176",
    "back_glow2_a": "40",
    "pal_window": "#efe4d6",
    "pal_window_text": "#2a1e18",
    "pal_base": "#fbf6f0",
    "pal_alternate": "#f2e8dc",
    "pal_text": "#2a1e18",
    "pal_button": "#f6eee4",
    "pal_button_text": "#2a1e18",
    "pal_highlight": "#b5521f",
    "pal_highlighted_text": "#ffffff",
    "back_redforest": "redforest/day",
    "back_leaf_alpha": "230",
}
THEMES["redforest_dawn"] = {
    **THEMES["redforest"],
    "back_base_a": "#f2dccf",
    "back_base_b": "#d9b9a8",
    "back_image": "redforest/dawn.jpg",
    "back_redforest": "redforest/dawn",
}
THEMES["redforest_night"] = {
    "font_size": "13px",
    "bg": "#11161b",
    "page": "rgba(14, 19, 24, 40)",
    "topbar": "rgba(14, 19, 24, 215)",
    "card": "rgba(22, 29, 36, 175)",
    "input": "rgba(10, 14, 18, 190)",
    "mono": "#0e1317",
    "btn": "rgba(44, 54, 64, 160)",
    "btn_hover": "rgba(58, 70, 82, 210)",
    "btn_pressed": "rgba(28, 36, 44, 220)",
    "btn_disabled": "rgba(30, 38, 46, 120)",
    "chip": "rgba(36, 46, 56, 170)",
    "border": "#2e3a44",
    "border_strong": "#3e4c58",
    "border_input": "#36444f",
    "border_secondary": "#5a4028",
    "gridline": "#232d36",
    "text": "#dde5ea",
    "text_bright": "#f2f6f8",
    "text_btn": "#dde5ea",
    "text_btn_hover": "#ffffff",
    "text_nav": "#95a2ac",
    "text_info": "#9aa8b2",
    "text_dim": "#8a97a1",
    "text_mono": "#c9d3da",
    "text_disabled": "#56626c",
    "text_tertiary_hover": "#ffc078",
    "accent": "#f0a24a",
    "accent_strong": "#ffb35c",
    "storage_a": "#f0a24a",
    "storage_b": "#b8642a",
    "storage_c": "#7d8a94",
    "storage_d": "#b3ac6e",
    "accent_text": "#1e1206",
    "warn_text": "#1b1206",
    "accent_section": "#f0a24a",
    "primary": "#c46a24",
    "primary_hover": "#d97c30",
    "primary_disabled_bg": "#2e2620",
    "primary_disabled_border": "#3e3228",
    "primary_disabled_text": "#7a6c60",
    "hero1": "#b85a1c",
    "hero2": "#e08a36",
    "hero_hover1": "#c8682a",
    "hero_hover2": "#ee9a46",
    "hero_border": "#f0a24a",
    "hero_text": "#ffffff",
    "secondary_hover_border": "#d97c30",
    "selection": "#c46a24",
    "selection_text": "#ffffff",
    "focus": "#f0a24a",
    "chip_ok": "#f0a24a",
    "chip_ok_border": "#5a4028",
    "checking_bg": "#1c252d",
    "warn": "#d9a04c",
    "danger_bg": "#7a322b",
    "danger_border": "#b0554f",
    "danger_text": "#ffe6dd",
    "danger_hover": "#9c3e35",
    "danger_disabled_bg": "#2c1e1c",
    "danger_disabled_border": "#4d3330",
    "danger_disabled_text": "#7a6460",
    "link": "#ffb35c",
    "link_hover": "#ffffff",
    "back_image": "redforest/night.jpg",
    "back_base_a": "#1a2230",
    "back_base_b": "#0b0f18",
    "back_glow1_rgb": "120, 140, 160",
    "back_glow1_a": "30",
    "back_glow1b_rgb": "60, 74, 88",
    "back_glow1b_a": "24",
    "back_glow1c_rgb": "30, 38, 46",
    "back_glow1c_a": "12",
    "back_glow2_rgb": "50, 62, 74",
    "back_glow2_a": "30",
    "pal_window": "#11161b",
    "pal_window_text": "#dde5ea",
    "pal_base": "#0e1317",
    "pal_alternate": "#161d24",
    "pal_text": "#dde5ea",
    "pal_button": "#232d36",
    "pal_button_text": "#dde5ea",
    "pal_highlight": "#c46a24",
    "pal_highlighted_text": "#ffffff",
    "back_redforest": "redforest/night",
    "back_fire": "1",
    "back_leaf_alpha": "120",
}
THEMES["redforest_dusk"] = {
    **THEMES["redforest_night"],
    "bg": "#1a1517",
    "page": "rgba(26, 20, 22, 40)",
    "topbar": "rgba(26, 20, 22, 215)",
    "card": "rgba(36, 28, 30, 175)",
    "input": "rgba(20, 15, 17, 190)",
    "mono": "#171214",
    "border": "#3e3236",
    "gridline": "#2e2528",
    "checking_bg": "#2a2124",
    "back_base_a": "#4a3448",
    "back_base_b": "#1c1218",
    "pal_window": "#1a1517",
    "pal_base": "#171214",
    "pal_alternate": "#241c1f",
    "pal_button": "#33282b",
    "back_image": "redforest/dusk.jpg",
    "back_redforest": "redforest/dusk",
    "back_leaf_alpha": "160",
}

#: Live themes: (first hour, phase) in order; the last phase runs past midnight.
_PHASES = {
    "winter": ((6, "winter_dawn"), (8, "winter"), (17, "winter_dusk"), (19, "winter_night")),
    "redforest": ((6, "redforest_dawn"), (8, "redforest"), (17, "redforest_dusk"), (19, "redforest_night")),
}
LIVE_THEMES = tuple(_PHASES)


#: Times of day a Live theme can be locked to, in _PHASES order.
LIVE_TIMES = ("dawn", "day", "dusk", "night")
#: Settings choices for the lock: (saved value, untranslated label).
LIVE_TIME_CHOICES = (
    ("", "Follow the clock"),
    ("dawn", "Always dawn"),
    ("day", "Always day"),
    ("dusk", "Always dusk"),
    ("night", "Always night"),
)
#: The locked time of day; "" follows the clock (see set_active_theme).
_LIVE_TIME = ""


def resolve_theme(name: str, hour: int | None = None) -> str:
    """The palette to paint: a Live theme's for the locked time or current hour, any other as is."""
    phases = _PHASES.get(name)
    if phases is None:
        return name
    if hour is None and _LIVE_TIME in LIVE_TIMES:
        return phases[LIVE_TIMES.index(_LIVE_TIME)][1]
    hour = time.localtime().tm_hour if hour is None else hour
    return next((key for start, key in reversed(phases) if hour >= start), phases[-1][1])
