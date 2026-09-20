"""The Deck Mode window.

Deliberately parallel to ``commander_gui.ui.main_window.MainWindow``: it
exposes the same ``settings`` / ``install_busy`` / ``set_install_busy()`` /
``refresh_settings()`` surface, because shared helpers in
``commander_gui.ui.common`` (notably ``activate_profile``) are handed a
"window" and call into it, and because screens that guard on a running
install read the flag by that name.

Three things differ from the desktop window on purpose, each for a reason
specific to the hardware:

* No QStatusBar. ``QMainWindow.statusBar()`` builds one on first call and
  reserves layout height for it - height this layout has budgeted to the
  pixel. ``statusBar()`` is overridden to return a shim that routes messages
  to a floating toast instead.
* No QDialog. In Game Mode gamescope composites a single surface with no
  window manager, so a second top-level window is at the mercy of whatever
  geometry it guesses. Confirmations and pickers are in-window overlays.
* Window geometry is never persisted. Deck Mode is 1280x800 or full screen;
  writing that into ``window_width``/``window_height`` would permanently
  shrink the user's desktop window the next time they switch back.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from commander_gui import gui_settings
from commander_gui.deck_launch import in_game_mode, is_steam_deck
from commander_gui.i18n import tr
from commander_gui.settings import load_settings
from commander_gui.themes import build_palette, set_active_theme
from commander_gui.ui.common import (
    GAMMA_PROFILE,
    count_active_mods,
    instance_window_title,
)
from commander_gui.ui.deck_switch import switch_mode

from .deck_theme import build_deck_stylesheet
from .focus import DeckFocusController
from .screens import SCREENS
from .widgets import (
    DECK_H,
    DECK_W,
    HEADER_H,
    MARGIN_X,
    NAV_H,
    DeckBackdrop,
    DeckOverlay,
    DeckToast,
    confirm_overlay,
    deck_button,
    deck_label,
)

#: Smallest window Deck Mode is laid out for when run on an ordinary
#: desktop. Below this the seven nav cells stop being touch-sized.
_MIN_W, _MIN_H = 1024, 640


class _DeckStatusBar:
    """A stand-in for QStatusBar that costs no layout height.

    Not a QWidget: the point is that nothing can accidentally add it to a
    layout. It only needs the two methods shared code actually calls.
    """

    def __init__(self, toast: DeckToast) -> None:
        self._toast = toast

    def showMessage(self, text: str, msecs: int = 3000) -> None:
        self._toast.show_message(text, msecs)

    def clearMessage(self) -> None:
        self._toast.hide()

    def addWidget(self, *_args, **_kwargs) -> None:
        """Accepted and ignored - Deck Mode has no status bar to fill."""

    addPermanentWidget = addWidget


class DeckWindow(QMainWindow):
    """Steam Deck Mode's main window."""

    def __init__(self, fatal: tuple[str, str] | None = None) -> None:
        super().__init__()
        self.setWindowTitle("STALKER COMMANDER")

        self.settings = load_settings()
        self.install_busy = False
        self.install_operation: str | None = None

        self._fatal = fatal
        self._pages: dict[str, QWidget] = {}
        self._nav_buttons: dict[str, QWidget] = {}
        self._back: list[Callable[[], bool]] = []
        self._overlay: DeckOverlay | None = None
        self._current_key = ""

        self.apply_style()
        self._build_ui()

        self._focus = DeckFocusController(self)
        # On the application, not on the window: Qt delivers key events to
        # the focused widget, so a window-level filter would never see the
        # D-pad at all. The controller ignores anything outside this window.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._focus)

        # A stuck user needs a keyboard escape hatch that does not depend on
        # reaching the Settings screen.
        exit_shortcut = QShortcut(QKeySequence("Ctrl+Shift+D"), self)
        exit_shortcut.activated.connect(lambda: self.exit_deck_mode())

        if fatal is None:
            start = gui_settings.load_gui_settings().get("deck_start_screen", "play")
            self.set_page(start if start in self._screen_keys else "play")
        QTimer.singleShot(0, self._focus.ensure_focus)

    def setWindowTitle(self, title: str) -> None:
        """Mark this window when it is not the first COMMANDER running.

        Overridden rather than applied at each call site because the title is
        set from several places (the nav switch composes "Install - ..."),
        and a marker that only some of them carry would be worse than none.
        """
        super().setWindowTitle(instance_window_title(title))

    # ------------------------------------------------------------ styling
    @property
    def _screen_keys(self) -> list[str]:
        return [key for key, _title, _glyph in SCREENS]

    def apply_style(self) -> None:
        """Apply the Deck stylesheet and palette for the saved theme."""
        state = gui_settings.load_gui_settings()
        theme = state.get("theme") or "gamma"
        set_active_theme(theme)
        app = QApplication.instance()
        if app is None:
            return
        app.setPalette(build_palette(theme))
        app.setStyleSheet(
            build_deck_stylesheet(
                theme,
                font_scale=int(state.get("deck_font_scale") or 100),
                font_family=state.get("font_family") or "Exo 2",
            )
        )

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        central = DeckBackdrop()
        central.setObjectName("deckCentral")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        self.stack = QStackedWidget()
        self.stack.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        root.addWidget(self.stack, 1)

        if self._fatal is None:
            root.addWidget(self._build_nav())
        else:
            self.stack.addWidget(self._build_fatal_screen())

        self.setCentralWidget(central)

        self.toast = DeckToast(central)
        self._status_shim = _DeckStatusBar(self.toast)

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("deckHeader")
        header.setFixedHeight(HEADER_H)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(MARGIN_X, 0, MARGIN_X, 0)
        layout.setSpacing(16)

        self.title_label = deck_label("", role="title")
        layout.addWidget(self.title_label)
        layout.addStretch(1)

        self.info_label = deck_label("", role="headerInfo")
        layout.addWidget(self.info_label)

        hint = tr("B  Back")
        if not (is_steam_deck() or in_game_mode()):
            # Say so plainly rather than pretending: on a desktop this is a
            # preview of the Deck interface, not the Deck.
            hint = tr("Deck Mode (windowed)") + "   ·   " + hint
        layout.addWidget(deck_label(hint, role="hint"))
        return header

    def _build_nav(self) -> QWidget:
        nav = QWidget()
        nav.setObjectName("deckNav")
        nav.setFixedHeight(NAV_H)
        layout = QHBoxLayout(nav)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)
        for key, title, glyph in SCREENS:
            button = deck_button(f"{glyph}\n{tr(title)}", role="normal")
            button.setObjectName("deckNavCell")
            button.setMinimumHeight(NAV_H - 8)
            button.clicked.connect(lambda _=False, k=key: self.set_page(k))
            layout.addWidget(button, 1)
            self._nav_buttons[key] = button
        return nav

    def _build_fatal_screen(self) -> QWidget:
        title, message = self._fatal or ("", "")
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(MARGIN_X, 40, MARGIN_X, 40)
        layout.setSpacing(20)
        layout.addStretch(1)
        layout.addWidget(deck_label(title, role="title"))
        layout.addWidget(deck_label(message, role="body", wrap=True))
        layout.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(
            deck_button(
                tr("Exit Deck Mode"),
                role="primary",
                on_click=lambda: switch_mode(self, deck=False),
            ),
            1,
        )
        row.addWidget(
            deck_button(tr("Quit"), on_click=self.close),
            1,
        )
        layout.addLayout(row)
        self.title_label.setText(tr("Setup problem"))
        return page

    # ----------------------------------------------------------- screens
    def _create_page(self, key: str) -> QWidget:
        # Imported lazily and per-key so one screen failing to import cannot
        # stop the window from opening on the others.
        from .screens.dashboard import DashboardScreen
        from .screens.install import InstallScreen
        from .screens.mods import ModsScreen
        from .screens.play import PlayScreen
        from .screens.profile import ProfileScreen
        from .screens.settings import SettingsScreen
        from .screens.system import SystemScreen
        from .screens.update import UpdateScreen

        classes = {
            "dashboard": DashboardScreen,
            "play": PlayScreen,
            "install": InstallScreen,
            "update": UpdateScreen,
            "mods": ModsScreen,
            "profile": ProfileScreen,
            "system": SystemScreen,
            "settings": SettingsScreen,
        }
        return classes[key](self)

    def _ensure_page(self, key: str) -> QWidget:
        """Build ``key``'s screen on first visit and cache it.

        Same lazy strategy as the desktop window: constructing a screen can
        kick off CLI and network work, so doing all seven upfront would make
        startup slower and noisier than it needs to be.
        """
        page = self._pages.get(key)
        if page is not None:
            return page
        page = self._create_page(key)
        self._pages[key] = page
        self.stack.addWidget(page)
        page.on_busy_changed(self.install_busy)
        page.on_install_activity_changed(self.install_operation)
        return page

    def set_page(self, key: str) -> None:
        """Switch to a screen by nav key."""
        if key not in self._screen_keys or self._fatal is not None:
            return
        page = self._ensure_page(key)
        self._current_key = key
        self.stack.setCurrentWidget(page)
        for nav_key, button in self._nav_buttons.items():
            button.setProperty("current", "true" if nav_key == key else "false")
            button.style().unpolish(button)
            button.style().polish(button)
        title = next(t for k, t, _g in SCREENS if k == key)
        self.title_label.setText(tr(title))
        self.update_mod_counter()
        # Refresh after Qt has laid the new screen out, so anything sized
        # from its own contents measures correctly.
        QTimer.singleShot(0, page.refresh)
        QTimer.singleShot(0, self._focus.ensure_focus)

    def current_key(self) -> str:
        return self._current_key

    # ------------------------------------------ MainWindow-compatible API
    def set_install_busy(self, busy: bool, operation: str | None = None) -> None:
        """The global install mutex, with the same contract as MainWindow's.

        Screens read ``install_busy`` before enabling anything that writes to
        the install tree, so two CLI processes can never run against it at
        once.
        """
        self.install_busy = bool(busy)
        self.install_operation = operation if busy else None
        for page in self._pages.values():
            page.on_busy_changed(self.install_busy)
            page.on_install_activity_changed(self.install_operation)

    def refresh_settings(self) -> None:
        self.settings = load_settings()
        self.update_mod_counter()

    def update_mod_counter(self) -> None:
        profile = self.settings.active_profile
        if profile is None:
            self.info_label.setText(tr("No Profile"))
            return
        name = profile.profile_name or tr("No Profile")
        # None means "count unavailable" (GAMMA not installed yet, or the
        # profile folder is missing) - show just the profile name then,
        # rather than a misleading zero.
        counts = count_active_mods(
            profile.gamma, profile.mo2_profile or GAMMA_PROFILE
        )
        if counts is None:
            self.info_label.setText(name)
            return
        enabled, _total = counts
        self.info_label.setText(
            name + "   \u00b7   " + tr("{enabled} Mods", enabled=enabled)
        )

    def statusBar(self) -> _DeckStatusBar:
        """Return the toast shim, never a real QStatusBar.

        QMainWindow.statusBar() creates one on demand and gives it layout
        height at the bottom of the window, which would push the nav bar
        off a full-screen Deck layout.
        """
        return self._status_shim

    # ---------------------------------------------------------- overlays
    def current_overlay(self) -> DeckOverlay | None:
        return self._overlay

    def show_overlay(self, overlay: DeckOverlay) -> None:
        """Display ``overlay`` over the whole window, focusing its first button."""
        self.dismiss_overlay()
        central = self.centralWidget()
        overlay.setParent(central)
        overlay.setGeometry(central.rect())
        overlay.show()
        overlay.raise_()
        self._overlay = overlay
        if overlay.default_button is not None:
            overlay.default_button.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self._focus.ensure_focus()

    def dismiss_overlay(self) -> None:
        if self._overlay is None:
            return
        overlay, self._overlay = self._overlay, None
        overlay.hide()
        overlay.deleteLater()
        self._focus.ensure_focus()

    def confirm(
        self,
        title: str,
        message: str,
        on_confirm: Callable[[], None],
        *,
        confirm_text: str | None = None,
        confirm_role: str = "primary",
    ) -> None:
        """Ask a yes/no question in an overlay."""

        def _accept() -> None:
            self.dismiss_overlay()
            on_confirm()

        self.show_overlay(
            confirm_overlay(
                title,
                message,
                on_confirm=_accept,
                on_cancel=self.dismiss_overlay,
                confirm_text=confirm_text,
                confirm_role=confirm_role,
            )
        )

    def notify(self, message: str, msecs: int = 3000) -> None:
        self.toast.show_message(message, msecs)

    # -------------------------------------------------------- back stack
    def push_back(self, handler: Callable[[], bool]) -> None:
        self._back.append(handler)

    def pop_back(self) -> None:
        if self._back:
            self._back.pop()

    def handle_back(self) -> None:
        """B / Escape. Resolved from the most local context outwards."""
        if self._overlay is not None:
            self.dismiss_overlay()
            return
        while self._back:
            handler = self._back.pop()
            if handler():
                return
        page = self._pages.get(self._current_key)
        if page is not None and page.on_back():
            return
        start = gui_settings.load_gui_settings().get("deck_start_screen", "play")
        if self._fatal is None and self._current_key != start:
            self.set_page(start if start in self._screen_keys else "play")
            return
        self._ask_exit()

    def _ask_exit(self) -> None:
        body = deck_label(
            tr("Leave Deck Mode, or close COMMANDER?"), role="body", wrap=True
        )
        self.show_overlay(
            DeckOverlay(
                tr("Exit Deck Mode"),
                body,
                [
                    (tr("Back to Deck Mode"), self.dismiss_overlay, "primary"),
                    (tr("Exit Deck Mode"), self.exit_deck_mode, "normal"),
                    (tr("Quit COMMANDER"), self._quit, "danger"),
                ],
            )
        )

    def exit_deck_mode(self) -> None:
        self.dismiss_overlay()
        switch_mode(self, deck=False)

    def _quit(self) -> None:
        self.dismiss_overlay()
        self.close()

    # ----------------------------------------------------------- lifecycle
    def rebuild(self) -> None:
        """Rebuild every widget, for a language change.

        Strings are set once at construction time - ``tr()`` is not live - so
        the only way to re-translate the interface is to build it again. Same
        approach as ``MainWindow.switch_language()``.
        """
        key = self._current_key
        self._pages.clear()
        self._nav_buttons.clear()
        self._overlay = None
        self._back.clear()
        old = self.centralWidget()
        self.apply_style()
        self._build_ui()
        if old is not None:
            old.deleteLater()
        self.set_page(key if key in self._screen_keys else "play")

    def show_for_environment(self) -> None:
        """Show full screen on a Deck, windowed anywhere else.

        Full screen rather than maximised: Game Mode has no window manager,
        so "maximised" is resolved against a nominal geometry that need not
        match the composited output - which is exactly how a window ends up
        rendering at the wrong size there.

        On an ordinary desktop it opens as a normal 1280x800 window.
        Taking over someone's monitor because they clicked a button to see
        what Deck Mode looks like would be hostile, and a windowed Deck UI is
        what makes it demonstrable and testable.
        """
        if os.environ.get("COMMANDER_DECK_WINDOWED") == "1":
            self.resize(DECK_W, DECK_H)
            self.show()
            return
        if is_steam_deck() or in_game_mode():
            self.showFullScreen()
            return
        self.resize(DECK_W, DECK_H)
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.show()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        central = self.centralWidget()
        if central is None:
            return
        if self._overlay is not None:
            self._overlay.setGeometry(central.rect())
        # setCentralWidget() can trigger a resize before _build_ui() has got
        # as far as creating the toast.
        toast = getattr(self, "toast", None)
        if toast is not None:
            toast.reposition()

    def closeEvent(self, event) -> None:
        # The focus filter lives on the application, so it has to be taken
        # off explicitly - otherwise it outlives the window it steers and
        # keeps answering key events for whatever opens next.
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._focus)
        # Deliberately does NOT persist window_width/window_height: Deck Mode
        # is always 1280x800 or full screen, and saving that would shrink the
        # desktop window the next time the user switches back.
        if self.install_busy:
            answer = QMessageBox.question(
                self,
                tr("Busy"),
                tr(
                    "An install, update or dependency download is still "
                    "running. Quit anyway?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        super().closeEvent(event)
