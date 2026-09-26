"""The Welcome screen shown when COMMANDER opens (desktop).

The desktop twin of Deck Mode's ``Steamdeck/welcome.py``: the latest
COMMANDER patch notes on the left, GitHub / Discord / Credits on the right,
and a "Don't show again" box in the bottom-right corner - the same
``welcome_hidden`` setting, so hiding it in one interface hides it in both.

Drawn inside the main window rather than as a QDialog: a dimmed scrim over
the whole window and a panel in the theme's card colour at partial opacity,
so the app behind still shows through. Both are painted from
``active_theme_tokens()`` on every paint, and a theme change restyles the
whole application (which repaints this too), so it follows the theme live.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, __version_label__, gui_settings
from ..themes import active_theme_tokens
from ..updates import (
    _numeric_version_tuple,
    effective_update_channel,
    fetch_latest_release_notes,
)
from .about_page import _DISCORD, _GITHUB, CREDITS
from .brand_icons import brand_icon, icon_size
from .common import (
    OK_GREEN,
    WARN,
    BackgroundTask,
    info_label,
    open_url,
    section_label,
    tr,
)

RELEASES_URL = f"{_GITHUB}/releases"

#: Panel opacity (0-255): mostly opaque - readable - with the app faintly
#: visible behind it.
_PANEL_ALPHA = 240
_SCRIM_ALPHA = 120


def release_name(latest_title: str | None) -> str:
    """"COMMANDER 1.2.9H3" -> "1.2.9H3"; "" when unknown."""
    return (latest_title or "").replace("COMMANDER", "").strip()


def version_status(latest_title: str | None) -> tuple[str, str]:
    """How the running COMMANDER compares with the latest release.

    ``(text, kind)``: a two- or three-word status for the version bar and
    ``"ok"`` / ``"warn"`` / ``"dim"`` for its colour. ``latest_title`` None
    means the release feed hasn't answered yet, "" that it couldn't be read.
    Shared with Deck Mode's Welcome screen.
    """
    if latest_title is None:
        return tr("Checking..."), "dim"
    installed = _numeric_version_tuple(__version__)
    latest = _numeric_version_tuple(latest_title)
    if installed is None or latest is None:
        return tr("Couldn't check"), "dim"
    if installed > latest:
        return tr("Development build"), "ok"
    if installed == latest:
        return tr("Up to date"), "ok"
    return tr("Update available"), "warn"


def _token(name: str, fallback: str) -> QColor:
    color = QColor(active_theme_tokens().get(name, fallback))
    return color if color.isValid() else QColor(fallback)


def _clear_background(frame: QFrame) -> None:
    """Stop the theme filling ``frame``'s rectangle.

    The desktop stylesheet gives every QWidget a solid page background, and
    a QFrame paints it - a square behind the rounded shape these frames
    draw themselves, showing at the corners.
    """
    frame.setStyleSheet(
        f"QFrame#{frame.objectName()} {{ background: transparent; border: none; }}"
    )


class _GlassPanel(QFrame):
    """A rounded panel painted in the live theme's card colour."""

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            fill = _token("card", "#141b15")
            fill.setAlpha(_PANEL_ALPHA)
            painter.setBrush(fill)
            painter.setPen(QPen(_token("border_strong", "#3a4a36"), 2))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 16, 16)
        finally:
            painter.end()
        super().paintEvent(event)


class _VersionBar(QFrame):
    """One line: your version, the latest release, and how they compare.

    Painted like the glass panel - from the live theme on every paint - as
    a slightly lighter strip, so it reads as one unit.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("welcomeVersionBar")
        _clear_background(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(8)

        def pair(caption: str) -> QLabel:
            label = QLabel(caption)
            label.setObjectName("dim")
            layout.addWidget(label)
            value = QLabel()
            value.setObjectName("accent")
            value.setStyleSheet("font-weight: bold;")
            layout.addWidget(value)
            return value

        self.installed = pair(tr("Your version"))
        self.installed.setText(__version_label__)
        # A thin vertical rule between the two versions.
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFixedWidth(1)
        separator.setFixedHeight(18)
        separator.setStyleSheet(f"background-color: {_token('border_strong', '#3a4a36').name()}; border: none;")
        layout.addSpacing(8)
        layout.addWidget(separator, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addSpacing(8)
        self.latest = pair(tr("Latest release"))
        layout.addStretch(1)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.set_latest(None)

    def set_latest(self, latest_title: str | None) -> None:
        name = release_name(latest_title)
        self.latest.setText(name or ("\u2026" if latest_title is None else "?"))
        text, kind = version_status(latest_title)
        color = {"ok": OK_GREEN.name(), "warn": WARN.name()}.get(kind)
        self.status.setText("\u25cf  " + text if color else text)
        self.status.setStyleSheet(f"color: {color}; font-weight: bold;" if color else "")
        self.status.setObjectName("" if color else "dim")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            fill = _token("chip", "#1f2a1e")
            painter.setBrush(fill)
            painter.setPen(QPen(_token("border", "#2b3a28"), 1))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        finally:
            painter.end()
        super().paintEvent(event)


class WelcomeOverlay(QWidget):
    """Covers the main window; closes with ✕, Escape or a click outside."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self._task: BackgroundTask | None = None
        #: The patch notes as shown, so Credits can swap back to them.
        self._notes: tuple[str, str, bool] | None = None
        self._showing_credits = False
        self.setObjectName("welcomeOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 40, 40, 40)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)

        self.panel = _GlassPanel()
        self.panel.setObjectName("welcomePanel")
        _clear_background(self.panel)
        self.panel.setMaximumWidth(980)
        self.panel.setMinimumWidth(640)
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(24, 18, 24, 18)
        panel_layout.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.addWidget(section_label(tr("Welcome to COMMANDER"), level=1), 1)
        close = QPushButton("✕")
        close.setObjectName("secondary")
        close.setFixedSize(36, 36)
        # The theme's button padding would leave no room for the glyph.
        close.setStyleSheet("padding: 0px; font-size: 16px;")
        close.setToolTip(tr("Close"))
        close.clicked.connect(self.close_overlay)
        title_row.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        panel_layout.addLayout(title_row)
        self.version_bar = _VersionBar()
        panel_layout.addWidget(self.version_bar)

        body = QHBoxLayout()
        body.setSpacing(20)
        notes_column = QVBoxLayout()
        notes_column.setSpacing(6)
        self.notes_title = section_label(tr("Patch notes"), level=3)
        notes_column.addWidget(self.notes_title)
        self.notes = QTextBrowser()
        self.notes.setObjectName("welcomeNotes")
        self.notes.setOpenExternalLinks(True)
        self.notes.setMinimumHeight(300)
        self.notes.setPlainText(tr("Loading patch notes..."))
        notes_column.addWidget(self.notes, 1)
        body.addLayout(notes_column, 3)

        # Right-hand column: who/what this is, then four identical link
        # buttons. One object name and one fixed height for all of them -
        # the theme pads #primary and #secondary differently, which is what
        # made GitHub shorter than the rest.
        links = QVBoxLayout()
        links.setSpacing(10)
        links.addWidget(section_label(tr("Links"), level=3))
        links.addWidget(
            info_label(
                tr(
                    "Found a bug or have an idea? Open an issue on GitHub. "
                    "Need help? Ask on the Discord."
                )
            )
        )
        links.addSpacing(6)
        self.link_buttons: list[QPushButton] = []
        icon_color = _token("text_btn_hover", "#e0e0e0")
        # Discord first and in its own brand colour: it is where players
        # get help, so it is the link the Welcome screen most wants clicked.
        for text, handler, logo in (
            (tr("Discord"), lambda: self._open(_DISCORD), "discord"),
            (tr("GitHub"), lambda: self._open(_GITHUB), "github"),
            ("\u2197  " + tr("All releases"), lambda: self._open(RELEASES_URL), None),
            ("\u2605  " + tr("Credits"), self._show_credits, None),
        ):
            button = QPushButton(("  " + text) if logo else text)
            name = "discordButton" if logo == "discord" else "welcomeLink"
            if logo:
                color = "#ffffff" if logo == "discord" else icon_color
                button.setIcon(brand_icon(logo, color, 20))
                button.setIconSize(icon_size(20))
            button.setObjectName(name)
            button.setFixedHeight(44)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton#{name} {{ padding: 0px 14px; text-align: left; }}"
            )
            button.clicked.connect(lambda _=False, h=handler: h())
            links.addWidget(button)
            self.link_buttons.append(button)
        self.credits_button = self.link_buttons[-1]
        links.addStretch(1)
        body.addLayout(links, 1)
        panel_layout.addLayout(body, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.hide_check = QCheckBox(tr("Don't show again"))
        self.hide_check.setChecked(
            bool(gui_settings.load_gui_settings().get("welcome_hidden"))
        )
        self.hide_check.toggled.connect(
            lambda on: gui_settings.save_gui_settings(welcome_hidden=bool(on))
        )
        bottom.addWidget(self.hide_check)
        panel_layout.addLayout(bottom)

        row.addWidget(self.panel, 6)
        row.addStretch(1)
        outer.addLayout(row, 4)
        outer.addStretch(1)

        window.installEventFilter(self)
        self.setGeometry(window.rect())
        self._load_notes()

    # -- geometry / closing ------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if obj is self.window and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.window.rect())
        return super().eventFilter(obj, event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(0, 0, 0, _SCRIM_ALPHA))
        finally:
            painter.end()

    def mousePressEvent(self, event) -> None:
        # A click on the dimmed area outside the panel closes it, like
        # clicking away from any popup.
        if not self.panel.geometry().contains(event.position().toPoint()):
            self.close_overlay()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_overlay()
            return
        super().keyPressEvent(event)

    def close_overlay(self) -> None:
        self.window.removeEventFilter(self)
        self.hide()
        self.deleteLater()

    # -- content -----------------------------------------------------------
    def _load_notes(self) -> None:
        channel = effective_update_channel(
            gui_settings.load_gui_settings().get("update_channel"), __version__
        )
        task = BackgroundTask(fetch_latest_release_notes, channel, parent=self)
        self._task = task
        task.result.connect(self._show_notes)
        task.error.connect(lambda _m: self._show_notes(None))
        task.start()

    def _show_notes(self, result: object) -> None:
        self._task = None
        if not isinstance(result, tuple) or len(result) != 2 or not result[1]:
            self.version_bar.set_latest("")
            self._notes = (
                tr("Patch notes"),
                tr("Couldn't load the patch notes. They are on the Releases page:")
                + "\n"
                + RELEASES_URL,
                False,
            )
        else:
            title, body = result
            self._notes = (tr("Patch notes"), str(body), True)
            self.version_bar.set_latest(str(title))
        if not self._showing_credits:
            self._render_notes()

    def _render_notes(self) -> None:
        if self._notes is None:
            self.notes_title.setText(tr("Patch notes"))
            self.notes.setPlainText(tr("Loading patch notes..."))
            return
        title, body, html = self._notes
        self.notes_title.setText(title)
        # GitHub's feed carries the release body already rendered to HTML;
        # QTextBrowser fetches nothing remote and runs no script.
        if html:
            self.notes.setHtml(body)
        else:
            self.notes.setPlainText(body)

    def _open(self, url: str) -> None:
        open_url(url)

    def _show_credits(self) -> None:
        """Credits in the notes box; pressed again, back to the notes."""
        self._showing_credits = not self._showing_credits
        if self._showing_credits:
            self.notes_title.setText(tr("Credits"))
            self.notes.setPlainText(
                "\n\n".join(tr("• {line}", line=tr(line)) for line in CREDITS)
            )
            self.credits_button.setText("\u2190  " + tr("Patch notes"))
        else:
            self._render_notes()
            self.credits_button.setText("\u2605  " + tr("Credits"))


def show_welcome(window) -> WelcomeOverlay:
    overlay = WelcomeOverlay(window)
    overlay.show()
    overlay.raise_()
    overlay.setFocus(Qt.FocusReason.OtherFocusReason)
    return overlay


#: Shown once per run at most: switching between the desktop window and
#: Deck Mode must not bring it up a second time.
_shown_this_run = False
#: The same promise across a mode switch, which is a real restart (execve,
#: see commander_gui.deck_launch) and so starts with _shown_this_run False
#: again. The environment survives the exec; a launch from Steam or the
#: menu starts without it. Holds the version it was shown for, so the
#: restart after a self-update still shows the new release's notes.
WELCOME_SHOWN_ENV = "COMMANDER_WELCOME_SHOWN"


def should_show_welcome() -> bool:
    """True unless "Don't show again" is ticked - and always after an update.

    Each COMMANDER version shows it at least once, "Don't show again" or
    not, so everyone sees the new release's notes (and the Discord link)
    after updating. Shared with Deck Mode.
    """
    if _shown_this_run or os.environ.get(WELCOME_SHOWN_ENV) == __version__:
        return False
    state = gui_settings.load_gui_settings()
    if state.get("welcome_seen_version") != __version__:
        return True
    return not state.get("welcome_hidden")


def mark_welcome_seen() -> None:
    global _shown_this_run
    _shown_this_run = True
    os.environ[WELCOME_SHOWN_ENV] = __version__
    gui_settings.save_gui_settings(welcome_seen_version=__version__)


def maybe_show_welcome(window) -> WelcomeOverlay | None:
    """Show the Welcome screen if :func:`should_show_welcome` says so."""
    if not should_show_welcome():
        return None
    mark_welcome_seen()
    return show_welcome(window)

