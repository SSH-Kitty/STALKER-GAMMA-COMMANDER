"""Theme palettes for the COMMANDER GUI.

The application stylesheet is a ``string.Template`` whose ``$token``
placeholders are filled from one of the palettes in :data:`THEMES`. Each theme
also carries the painted backdrop glow colors and the native ``QPalette``
colors so dialogs, menus and scrollbars match the active theme.
"""

from __future__ import annotations

import re
import string

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
        "dusk",
        "Dusk",
        "Warm charcoal and amber with orange sunset accents.",
        ("#140d06", "#ff9f45", "#f0e2cc"),
    ),
    (
        "midnight",
        "Midnight",
        "Cool slate blues and greys with a teal accent.",
        ("#0b1016", "#6fd3a8", "#dbe6f0"),
    ),
    (
        "terminal",
        "Terminal",
        "Retro CRT: pure black with neon green phosphor text.",
        ("#000000", "#33ff66", "#33cc55"),
    ),
]

_ACTIVE: str = "gamma"


def set_active_theme(name: str) -> None:
    global _ACTIVE
    if name in THEMES:
        _ACTIVE = name


def active_theme() -> str:
    return _ACTIVE


def active_theme_tokens() -> dict[str, str]:
    return THEMES[_ACTIVE]


_FALLBACK_FONT = '"Exo 2", "DejaVu Sans", "Noto Sans", sans-serif'

_FONT_FAMILY_MAP: dict[str, str] = {
    "Exo 2": '"Exo 2", "DejaVu Sans", "Noto Sans", sans-serif',
    "Noto Sans": '"Noto Sans", "DejaVu Sans", sans-serif',
    "DejaVu Sans": '"DejaVu Sans", "Noto Sans", sans-serif',
    "Ubuntu": '"Ubuntu", "DejaVu Sans", sans-serif',
    "Liberation Sans": '"Liberation Sans", "DejaVu Sans", sans-serif',
    "Inter": '"Inter", "DejaVu Sans", sans-serif',
}


def build_stylesheet(
    name: str, font_size: int | None = None, font_family: str | None = None
) -> str:
    tokens = THEMES.get(name) or THEMES["gamma"]
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
    return qss


def build_palette(name: str) -> QPalette:
    tokens = THEMES.get(name) or THEMES["gamma"]
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
QScrollArea > QWidget#qt_scrollarea_viewport,
QWidget#pageContent {
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
    color: $accent;
}
#byline {
    font-size: 11px;
    letter-spacing: 1px;
    color: $accent;
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
    color: $accent;
}
#navtabs::tab:selected {
    color: $accent;
    border-bottom: 2px solid $accent_strong;
}
#navtabs[settingsMode="true"]::tab:selected {
    color: $text_nav;
    border-bottom: 2px solid transparent;
}
#navtabs[settingsMode="true"]::tab:hover {
    color: $accent;
}
#cogButton {
    background: transparent;
    border: none;
    color: $text_disabled;
    font-size: 18px;
    padding: 0 10px;
}
#cogButton:hover {
    color: $accent;
}
#cogButton[active="true"] {
    color: $accent;
}
#card {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 10px;
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
QLabel#section3 {
    color: #7dc963;
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
    color: $accent_text;
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
    color: $accent;
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
    color: $accent_text;
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
QPushButton#tertiary {
    background: transparent;
    border: none;
    color: $text_info;
    text-decoration: underline;
    padding: 4px 8px;
}
QPushButton#tertiary:hover {
    color: $text_tertiary_hover;
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
QProgressBar::text {
    color: $text_btn;
    background: rgba(0, 0, 0, 0.55);
    padding: 1px 6px;
    border-radius: 3px;
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
    background-color: $topbar;
    color: $text_dim;
    border-top: 1px solid $border;
}
QPushButton#githubLink {
    color: $link;
    padding: 0 2px 0 8px;
    border: none;
    background: transparent;
}
QPushButton#githubLink:hover {
    color: $link_hover;
    background: transparent;
}
QMessageBox, QFileDialog {
    background-color: $card;
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
        "accent_text": "#0c130a",
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
        "danger_hover": "#8f3a33",
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
        "accent_text": "#0b1511",
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
        "danger_hover": "#803a33",
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
    "terminal": {
        "font_size": "13px",
        "bg": "#000000",
        "page": "rgba(0, 0, 0, 190)",
        "topbar": "rgba(0, 0, 0, 245)",
        "card": "#060b06",
        "input": "#000000",
        "mono": "#000000",
        "btn": "#0a120a",
        "btn_hover": "#122212",
        "btn_pressed": "#060c06",
        "btn_disabled": "#080c08",
        "chip": "#071007",
        "border": "#123912",
        "border_strong": "#1c4f1c",
        "border_input": "#123012",
        "border_secondary": "#1c4a1c",
        "gridline": "#0c240c",
        "text": "#33cc55",
        "text_bright": "#66ff88",
        "text_btn": "#3ddc5a",
        "text_btn_hover": "#5cf27a",
        "text_nav": "#1f7a33",
        "text_info": "#2fa847",
        "text_dim": "#1f7a33",
        "text_mono": "#33cc55",
        "text_disabled": "#1a5c28",
        "text_tertiary_hover": "#3ddc5a",
        "accent": "#33ff66",
        "accent_strong": "#2ee75c",
        "accent_text": "#001a05",
        "accent_section": "#33dd55",
        "primary": "#1f8a3a",
        "primary_hover": "#26a346",
        "primary_disabled_bg": "#0f3318",
        "primary_disabled_border": "#185c26",
        "primary_disabled_text": "#2a8a42",
        "hero1": "#16a13a",
        "hero2": "#26c94e",
        "hero_hover1": "#1bb844",
        "hero_hover2": "#30dd5a",
        "hero_border": "#33ff66",
        "secondary_hover_border": "#26c94e",
        "selection": "#1f8a3a",
        "selection_text": "#001a05",
        "focus": "#2ee75c",
        "chip_ok": "#33ff66",
        "chip_ok_border": "#1c6b2e",
        "checking_bg": "#112817",
        "warn": "#cc9933",
        "danger_bg": "#5a1f1c",
        "danger_border": "#a0443f",
        "danger_text": "#ffd9d4",
        "danger_hover": "#6f2a26",
        "danger_disabled_bg": "#241211",
        "danger_disabled_border": "#40201e",
        "danger_disabled_text": "#7a504d",
        "link": "#66ff88",
        "link_hover": "#aaffcc",
        "back_base_a": "#020702",
        "back_base_b": "#000000",
        "back_glow1_rgb": "33, 255, 102",
        "back_glow1_a": "45",
        "back_glow1b_rgb": "16, 120, 50",
        "back_glow1b_a": "25",
        "back_glow1c_rgb": "8, 55, 24",
        "back_glow1c_a": "12",
        "back_glow2_rgb": "12, 90, 30",
        "back_glow2_a": "35",
        "pal_window": "#000000",
        "pal_window_text": "#33cc55",
        "pal_base": "#000000",
        "pal_alternate": "#060b06",
        "pal_text": "#33cc55",
        "pal_button": "#0a120a",
        "pal_button_text": "#3ddc5a",
        "pal_highlight": "#1f8a3a",
        "pal_highlighted_text": "#001a05",
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
        "accent_text": "#0c130a",
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
        "danger_hover": "#5f2823",
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
    "dusk": {
        "font_size": "13px",
        "bg": "#140d06",
        "page": "rgba(20, 13, 6, 180)",
        "topbar": "rgba(16, 10, 4, 240)",
        "card": "#1d140a",
        "input": "#120b04",
        "mono": "#0d0703",
        "btn": "#2a1c10",
        "btn_hover": "#3a2817",
        "btn_pressed": "#1d1309",
        "btn_disabled": "#1c1208",
        "chip": "#221608",
        "border": "#3a2a18",
        "border_strong": "#4f3a20",
        "border_input": "#3a2a18",
        "border_secondary": "#57401f",
        "gridline": "#2a1d10",
        "text": "#f0e2cc",
        "text_bright": "#faf0e0",
        "text_btn": "#f5e8d4",
        "text_btn_hover": "#ffe9c4",
        "text_nav": "#b09778",
        "text_info": "#c2a885",
        "text_dim": "#9c8261",
        "text_mono": "#e8d4b8",
        "text_disabled": "#7a6245",
        "text_tertiary_hover": "#e8c9a0",
        "accent": "#ff9f45",
        "accent_strong": "#ff8f2e",
        "accent_text": "#2a1600",
        "accent_section": "#f0a75f",
        "primary": "#c8721e",
        "primary_hover": "#dd8530",
        "primary_disabled_bg": "#4a2e12",
        "primary_disabled_border": "#6b4518",
        "primary_disabled_text": "#a8804f",
        "hero1": "#d97a1e",
        "hero2": "#ff9f45",
        "hero_hover1": "#ea8a2c",
        "hero_hover2": "#ffb061",
        "hero_border": "#ffb061",
        "secondary_hover_border": "#ff9f45",
        "selection": "#c8721e",
        "selection_text": "#2a1600",
        "focus": "#ff8f2e",
        "chip_ok": "#ff9f45",
        "chip_ok_border": "#7a4f1c",
        "checking_bg": "#2e261d",
        "warn": "#e0a24a",
        "danger_bg": "#6e2a1e",
        "danger_border": "#b04f3d",
        "danger_text": "#ffe0d4",
        "danger_hover": "#833424",
        "danger_disabled_bg": "#2d1610",
        "danger_disabled_border": "#4d2919",
        "danger_disabled_text": "#7a5a48",
        "link": "#ffe9c4",
        "link_hover": "#ffffff",
        "back_base_a": "#201408",
        "back_base_b": "#0d0703",
        "back_glow1_rgb": "255, 159, 69",
        "back_glow1_a": "55",
        "back_glow1b_rgb": "150, 80, 30",
        "back_glow1b_a": "32",
        "back_glow1c_rgb": "70, 38, 14",
        "back_glow1c_a": "14",
        "back_glow2_rgb": "160, 90, 40",
        "back_glow2_a": "45",
        "pal_window": "#140d06",
        "pal_window_text": "#f0e2cc",
        "pal_base": "#120b04",
        "pal_alternate": "#1d140a",
        "pal_text": "#f0e2cc",
        "pal_button": "#2a1c10",
        "pal_button_text": "#f5e8d4",
        "pal_highlight": "#c8721e",
        "pal_highlighted_text": "#2a1600",
    },
}
