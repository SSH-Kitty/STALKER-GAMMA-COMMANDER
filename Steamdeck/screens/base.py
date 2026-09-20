"""The common shape of a Deck screen.

Mirrors the duck-typed page protocol the desktop UI already uses
(``__init__(window)``, ``refresh()``, ``on_busy_changed(bool)``,
``on_install_activity_changed(str|None)``) so the two window classes can
drive their children the same way, and so a screen could in principle be
moved between them.

Each screen owns a scrollable content column. The 1280x800 budget is a
design target, not an assumption: Deck Mode also runs in a resizable window
on an ordinary desktop, so content scrolls rather than clips when the window
is shorter than the Deck's panel.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from commander_gui.i18n import tr

from ..widgets import DECK_W, MARGIN_X, MARGIN_Y, ROW_GAP, deck_label


class DeckScreen(QWidget):
    """Base class for every screen in the bottom nav bar."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # The scroll area itself must not be a focus stop: it would be one
        # more thing the D-pad lands on with nothing to do there.
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        outer.addWidget(scroll)

        content = QWidget()
        content.setObjectName("deckPageContent")
        # Horizontal scrolling is off, so any child that insists on being
        # wider than the viewport is simply clipped off the right-hand edge
        # with no way to reach it. Cap the content instead.
        content.setMaximumWidth(DECK_W)
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(MARGIN_X, MARGIN_Y, MARGIN_X, MARGIN_Y)
        self.body.setSpacing(ROW_GAP)
        scroll.setWidget(content)

        self.build()

    # -- subclass hooks ---------------------------------------------------
    def build(self) -> None:
        """Create this screen's widgets into ``self.body``."""

    def refresh(self) -> None:
        """Re-read state. Called whenever the screen becomes visible."""

    def on_busy_changed(self, busy: bool) -> None:
        """React to a global install starting or finishing."""

    def on_install_activity_changed(self, operation: str | None) -> None:
        """React to *which* long-running operation is in progress."""

    def on_back(self) -> bool:
        """Handle B/Escape. Return True if the screen consumed it."""
        return False

    # -- helpers ----------------------------------------------------------
    def no_profile_notice(self) -> QWidget:
        """The placeholder shown when no CLI profile is active."""
        return deck_label(
            tr("Create or activate a profile first (Profiles page)."),
            role="body",
            wrap=True,
        )

    def profile(self):
        """The active CLI profile, or None."""
        return self.window.settings.active_profile
