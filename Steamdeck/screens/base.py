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

from ..scale import px
from ..widgets import MARGIN_X, MARGIN_Y, ROW_GAP, DeckSmoothScrollArea


class DeckScreen(QWidget):
    """Base class for every screen in the bottom nav bar."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Glides to a control the D-pad moves to off-screen, instead of
        # jumping (see DeckSmoothScrollArea).
        scroll = DeckSmoothScrollArea()
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
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(px(MARGIN_X), px(MARGIN_Y), px(MARGIN_X), px(MARGIN_Y))
        self.body.setSpacing(px(ROW_GAP))
        scroll.setWidget(content)
        self.scroll = scroll
        from ..focus import enable_kinetic_scroll

        enable_kinetic_scroll(scroll)

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

    def on_action(self, action: str) -> bool:
        """Handle X/Y/Start/View on this screen. True if consumed."""
        return False

    def hints(self) -> list[tuple[str, str]] | None:
        """Footer prompts as (button, English label); None for the default."""
        return None

    def default_focus(self) -> QWidget | None:
        """The control focus lands on when arriving here with nothing
        remembered, or when the focused control disappears (a job's Cancel
        hiding as it finishes). None: the first control in reading order."""
        return None

    def update_hints(self) -> None:
        """Re-read :meth:`hints` into the footer - for screens whose
        buttons come and go with their state."""
        updater = getattr(self.window, "update_hints", None)
        if callable(updater) and getattr(self.window, "current_page", lambda: None)() is self:
            updater()

    # -- helpers ----------------------------------------------------------
    def profile(self):
        """The active CLI profile, or None."""
        return self.window.settings.active_profile
