"""Shared COMMANDER themes and their persistent GUI preference."""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

THEME_INFO = [
    ("gamma", "GAMMA"),
    ("black", "GAMMA Black"),
    ("dusk", "Dusk"),
    ("midnight", "Midnight"),
    ("terminal", "Terminal"),
    ("reactor", "Reactor"),
]

# These are the COMMANDER core colors. The smaller assistant UI derives all
# of its other colors from these tokens, rather than keeping a second palette.
THEMES = {
    "gamma": {"bg": "#0d130c", "panel": "#141b15", "card": "#1f2b20", "border": "#26321f", "text": "#e2ead8", "dim": "#7f8f78", "accent": "#9fe96f", "strong": "#8fe45c", "selection": "#5fb548", "danger": "#e0554f", "warn": "#d9a04c"},
    "black": {"bg": "#000000", "panel": "#0a0a0a", "card": "#141414", "border": "#1f1f1f", "text": "#e0e0e0", "dim": "#777777", "accent": "#9fe96f", "strong": "#8fe45c", "selection": "#5fb548", "danger": "#e0554f", "warn": "#d9a04c"},
    "dusk": {"bg": "#140d06", "panel": "#1d140a", "card": "#2a1c10", "border": "#3a2a18", "text": "#f0e2cc", "dim": "#9c8261", "accent": "#ff9f45", "strong": "#ff8f2e", "selection": "#c8721e", "danger": "#b04f3d", "warn": "#e0a24a"},
    "midnight": {"bg": "#0b1016", "panel": "#111824", "card": "#1a2432", "border": "#1d2a3a", "text": "#dbe6f0", "dim": "#6e8496", "accent": "#6fd3a8", "strong": "#5ec99b", "selection": "#3f9d7a", "danger": "#b0554f", "warn": "#d9a04c"},
    "terminal": {"bg": "#000000", "panel": "#060b06", "card": "#0a120a", "border": "#123912", "text": "#33cc55", "dim": "#1f7a33", "accent": "#33ff66", "strong": "#2ee75c", "selection": "#1f8a3a", "danger": "#a0443f", "warn": "#cc9933"},
    "reactor": {"bg": "#010a0a", "panel": "#061616", "card": "#0a2020", "border": "#0f3232", "text": "#cdeaea", "dim": "#4a7a7a", "accent": "#00a3a3", "strong": "#00c2c2", "selection": "#0a9494", "danger": "#b0554f", "warn": "#d9a04c"},
}

_ACTIVE = "gamma"


def config_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    return (Path(root) if root else Path.home() / ".config") / "stalker-gamma" / "gui-settings.json"


def load_saved_theme() -> str:
    try:
        value = json.loads(config_path().read_text(encoding="utf-8")).get("theme")
    except (OSError, ValueError, AttributeError):
        value = None
    return value if value in THEMES else "gamma"


def save_theme(name: str) -> None:
    if name not in THEMES:
        return
    path = config_path()
    try:
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}
        settings["theme"] = name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def set_active_theme(name: str) -> None:
    global _ACTIVE
    if name in THEMES:
        _ACTIVE = name
        # The shared title-bar buttons paint from COMMANDER's theme tokens.
        from commander_gui import themes as commander_themes

        commander_themes.set_active_theme(name)


def active_theme() -> str:
    return _ACTIVE


def token(name: str) -> str:
    values = THEMES[_ACTIVE]
    aliases = {"PANEL": "panel", "CARD": "card", "BG": "bg", "BORDER": "border", "TEXT": "text", "DIM": "dim", "ACCENT": "accent", "RED": "danger", "AMBER": "warn", "SECTION_ACCENT": "accent", "WORDMARK_ACCENT": "accent", "NAV_TEXT": "dim", "INFO_TEXT": "dim", "TEXT_BRIGHT": "text", "GREY": "dim"}
    return values.get(aliases.get(name, name), values["text"])


def _stylesheet() -> str:
    t = THEMES[_ACTIVE]
    return f"""QWidget {{ background:{t['bg']}; color:{t['text']}; font-family:\"Exo 2\",\"DejaVu Sans\",sans-serif; font-size:13px; }}
QLabel {{ background:transparent; }} QWidget#topbar {{ background:{t['panel']}; border-bottom:1px solid {t['border']}; }}
QWidget#quickActions, QWidget#wordmarkBlock {{ background:transparent; }} QPushButton {{ background:{t['card']}; color:{t['text']}; border:1px solid {t['border']}; border-radius:6px; padding:6px 14px; }}
QPushButton:hover {{ border-color:{t['accent']}; }} QPushButton#primary {{ background:{t['accent']}; color:{t['bg']}; font-weight:600; border:none; }}
QPushButton#tabButton {{ background:transparent; color:{t['dim']}; border:none; border-bottom:2px solid transparent; border-radius:0; padding:12px 18px; }} QPushButton#tabButton:hover, QPushButton#tabButton:pressed {{ color:{t['accent']}; border-bottom-color:{t['accent']}; }}
 QLabel#wordmark {{ color:{t['accent']}; font-size:24px; font-weight:bold; letter-spacing:2px; }} QLabel#appTitle {{ color:{t['accent']}; }} QLabel#byline {{ color:{t['accent']}; font-size:11px; letter-spacing:1px; padding-right:3px; }} QLabel#heroSub, QLabel#dim, QLabel#caption, QLabel#emptyState {{ color:{t['dim']}; }} QLabel#heroTitle, QLabel#detailTitle {{ color:{t['text']}; }} QLabel#pageTitle {{ color:{t['text']}; font-size:20px; font-weight:bold; }} QLabel#section2 {{ color:{t['accent']}; font-weight:bold; }} QLabel#info {{ color:{t['dim']}; }}
QFrame#card, QFrame#detailCard {{ background:{t['panel']}; border:1px solid {t['border']}; border-radius:10px; }} QFrame#suggestionCard {{ background:{t['card']}; border:1px solid {t['border']}; border-left:3px solid {t['accent']}; border-radius:8px; }} QLabel#suggestionHow {{ color:{t['accent']}; font-size:11px; font-weight:bold; letter-spacing:1px; }} QLabel#suggestionText {{ color:{t['text']}; }}
QLineEdit, QComboBox {{ background:{t['card']}; color:{t['text']}; border:1px solid {t['border']}; border-radius:6px; padding:4px 8px; }} QComboBox QAbstractItemView {{ background:{t['card']}; color:{t['text']}; selection-background-color:{t['selection']}; }}
QTableWidget, QListWidget, QTreeWidget, QTextEdit, QPlainTextEdit {{ background:{t['bg']}; color:{t['text']}; border:1px solid {t['border']}; border-radius:8px; selection-background-color:{t['selection']}; }} QHeaderView::section {{ background:{t['panel']}; color:{t['dim']}; border:none; padding:6px; }}
QSplitter::handle {{ background:transparent; }} QToolButton {{ background:transparent; border:none; color:{t['dim']}; }}
QPushButton:checked {{ border-color:{t['accent']}; color:{t['accent']}; }} QPushButton:disabled {{ color:{t['dim']}; }}
QPushButton#linkButton {{ background:transparent; border:none; color:{t['accent']}; padding:2px 4px; }} QPushButton#linkButton:hover {{ text-decoration:underline; }}
QFrame#verdictCard {{ background:{t['panel']}; border:1px solid {t['border']}; border-left:4px solid {t['accent']}; border-radius:10px; }}
QFrame#verdictCard[verdict="bad"] {{ border-left-color:{t['danger']}; }} QFrame#verdictCard[verdict="warn"] {{ border-left-color:{t['warn']}; }}
QLabel#verdictIcon {{ background:{t['accent']}; color:{t['bg']}; border-radius:22px; font-size:24px; font-weight:bold; }}
QFrame#verdictCard[verdict="bad"] QLabel#verdictIcon {{ background:{t['danger']}; }} QFrame#verdictCard[verdict="warn"] QLabel#verdictIcon {{ background:{t['warn']}; }}
QLabel#verdictTitle {{ color:{t['text']}; font-size:20px; font-weight:bold; }} QLabel#verdictSub {{ color:{t['text']}; }}
QScrollArea#findingScroll, QWidget#findingList, QScrollArea#detailScroll, QWidget#detailBody, QWidget#detailButtons {{ background:transparent; border:none; }}
QLabel#groupHeader {{ color:{t['dim']}; font-size:11px; font-weight:bold; letter-spacing:1px; }}
QLabel#groupHeader[sev="fatal"], QLabel#groupHeader[sev="error"] {{ color:{t['danger']}; }} QLabel#groupHeader[sev="warning"] {{ color:{t['warn']}; }}
QFrame#findingCard {{ background:{t['panel']}; border:1px solid {t['border']}; border-radius:8px; }} QFrame#findingCard:hover {{ border-color:{t['dim']}; }}
QFrame#findingCard[selected="true"] {{ background:{t['card']}; border-color:{t['accent']}; }}
QLabel#findingTitle {{ color:{t['text']}; font-size:14px; }} QLabel#findingMeta {{ color:{t['dim']}; font-size:12px; }}
QLabel#countBadge {{ color:{t['dim']}; background:{t['bg']}; border:1px solid {t['border']}; border-radius:9px; padding:1px 8px; font-size:11px; }}
QFrame#sevBar {{ background:{t['dim']}; border:none; border-top-left-radius:8px; border-bottom-left-radius:8px; }}
QFrame#sevBar[sev="fatal"], QFrame#sevBar[sev="error"] {{ background:{t['danger']}; }} QFrame#sevBar[sev="warning"] {{ background:{t['warn']}; }} QFrame#sevBar[sev="ok"] {{ background:#5fb548; }}
QLabel#sevPill {{ font-size:11px; font-weight:bold; letter-spacing:1px; padding:3px 10px; border-radius:4px; background:{t['dim']}; color:{t['bg']}; }}
QLabel#sevPill[sev="fatal"], QLabel#sevPill[sev="error"] {{ background:{t['danger']}; }} QLabel#sevPill[sev="warning"] {{ background:{t['warn']}; }} QLabel#sevPill[sev="ok"] {{ background:#5fb548; }}
QLabel#detailTitle {{ font-size:18px; font-weight:bold; }} QLabel#caption {{ font-size:11px; font-weight:bold; letter-spacing:1px; }}
QFrame#suggestionCard[tone="calm"] {{ border-left-color:{t['dim']}; }} QFrame#suggestionCard[tone="calm"] QLabel#suggestionHow {{ color:{t['dim']}; }}
QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }} QScrollBar:horizontal {{ background:transparent; height:10px; margin:2px; }}
QScrollBar::handle {{ background:{t['border']}; border-radius:3px; min-height:24px; min-width:24px; }} QScrollBar::handle:hover {{ background:{t['dim']}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background:none; border:none; width:0; height:0; }}
QWidget#titleStrip {{ background:transparent; }} QPushButton#winMin, QPushButton#winMax, QPushButton#winClose {{ background:transparent; border:none; border-radius:0; padding:0; }}
QPushButton#winMin:hover, QPushButton#winMax:hover {{ background:{t['card']}; }} QPushButton#winClose:hover {{ background:#c42b1c; }} QPushButton#winClose:pressed {{ background:#a1261a; }}
QPlainTextEdit#excerpt, QPlainTextEdit#technical {{ font-family:monospace; font-size:12px; background:{t['bg']}; }}"""


def load_font() -> str:
    font_path = Path(__file__).parent.parent / "fonts" / "Exo2-Variable.ttf"
    if font_path.is_file() and QFontDatabase.addApplicationFont(str(font_path)) >= 0:
        return "Exo 2"
    return ""


def apply_theme(app: QApplication, name: str | None = None) -> None:
    """Apply a COMMANDER theme; with no name, use the shared saved setting."""
    if name is not None:
        set_active_theme(name)
    else:
        set_active_theme(load_saved_theme())
    load_font()
    app.setStyle("Fusion")
    t = THEMES[_ACTIVE]
    palette = QPalette()
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.AlternateBase):
        palette.setColor(role, QColor(t["bg"]))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.ToolTipText):
        palette.setColor(role, QColor(t["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(t["card"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(t["panel"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(t["selection"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(t["bg"]))
    app.setPalette(palette)
    app.setStyleSheet(_stylesheet())
    globals()["QSS"] = _stylesheet()


QSS = _stylesheet()


def __getattr__(name: str) -> str:
    """Keep the old color-constant API while resolving colors dynamically."""
    if name.isupper() and name in {"BG", "PANEL", "CARD", "BORDER", "TEXT", "DIM", "ACCENT", "RED", "AMBER", "SECTION_ACCENT", "WORDMARK_ACCENT", "NAV_TEXT", "INFO_TEXT", "TEXT_BRIGHT", "GREY"}:
        return token(name)
    raise AttributeError(name)
