"""The "Hold L4 + R4 to open COMMANDER overlay" pop-up at game start.

Deck Mode's version of the desktop's ``graphics_window._KeyHint``: shown in
the bottom-left corner for a few seconds once the engine logs that it has
started (MO2 and the splash come first), sized with the rest of Deck Mode
(``scale.px``) so it reads the same on the Deck's 1280x800 panel as on a
bigger screen. Never takes focus or clicks: the game keeps both.
"""

from __future__ import annotations

import time
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from commander_gui.i18n import tr
from commander_gui.themes import active_theme_tokens
from commander_gui.ui.game_splash import engine_started
from commander_gui.ui.graphics_window import _HINT_DELAY_MS, _HINT_TIMEOUT_S

from .scale import px

SHOWN_MS = 10_000


class DeckKeyHint(QWidget):
    def __init__(self, key: str) -> None:
        super().__init__(
            None,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowOpacity(0.94)
        tokens = active_theme_tokens()
        label = QLabel(tr("Hold {key} to open COMMANDER overlay").format(
            key=f"<b style='color: {tokens.get('accent', '#9fe96f')}'>{escape(key)}</b>"))
        label.setStyleSheet(
            f"background-color: {tokens['card']}; color: {tokens['text']};"
            f" border: {px(1)}px solid {tokens['border']}; border-radius: {px(12)}px;"
            f" padding: {px(14)}px {px(22)}px; font-size: {px(20)}px;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label)
        QTimer.singleShot(SHOWN_MS, self, self.close)

    def show_on(self, window: QWidget) -> None:
        self.adjustSize()
        screen = window.screen() or QGuiApplication.primaryScreen()
        area = screen.geometry()
        self.move(area.x() + px(24), area.bottom() - self.height() - px(24))
        self.show()
        self.raise_()


def show_when_game_is_up(window, anomaly: str, gamma: str, key: str) -> QTimer:
    """From a launch's start, poll the engine logs, then show the hint over
    the game. Returns the poll timer, so the caller can stop it if the
    session ends first."""
    logs = [Path(anomaly) / "appdata" / "logs"]
    if gamma:
        logs.append(Path(gamma) / "overwrite" / "appdata" / "logs")
    since = time.time() - 1
    deadline = time.monotonic() + _HINT_TIMEOUT_S
    timer = QTimer(window, interval=1000)

    def check() -> None:
        if engine_started(logs, since):
            timer.stop()
            QTimer.singleShot(_HINT_DELAY_MS, window, lambda: window.session_active() and DeckKeyHint(key).show_on(window))
        elif time.monotonic() > deadline:
            timer.stop()

    timer.timeout.connect(check)
    timer.start()
    return timer
