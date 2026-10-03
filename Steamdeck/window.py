"""The Deck Mode window.

Deliberately parallel to ``commander_gui.ui.main_window.MainWindow``: it
exposes the same ``settings`` / ``install_busy`` / ``set_install_busy()`` /
``refresh_settings()`` surface, because shared helpers in
``commander_gui.ui.common`` (notably ``activate_profile``) are handed a
"window" and call into it, and because screens that guard on a running
install read the flag by that name.

Things that differ from the desktop window on purpose, each for a reason
specific to the hardware:

* No QStatusBar. ``QMainWindow.statusBar()`` builds one on first call and
  reserves layout height for it - height this layout has budgeted to the
  pixel. ``statusBar()`` is overridden to return a shim that routes messages
  to a floating toast instead.
* No QDialog. In Game Mode gamescope composites a single surface with no
  window manager, so a second top-level window is at the mercy of whatever
  geometry it guesses. Confirmations, pickers and the keyboard are in-window
  overlays - including the "quit while busy?" question on close.
* Window geometry is never persisted. Deck Mode is 1280x800 or full screen;
  writing that into ``window_width``/``window_height`` would permanently
  shrink the user's desktop window the next time they switch back.
* Input arrives from the gamepad as well as the keyboard (see
  ``Steamdeck.gamepad``), and the footer says which button does what.
* While a long job runs the window keeps the machine awake
  (``Steamdeck.power``) - a handheld that suspends mid-download is the most
  likely way a Deck install fails.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from commander_gui import gui_settings
from commander_gui.deck_launch import in_game_mode, is_steam_deck
from commander_gui.i18n import tr
from commander_gui.settings import load_settings
from commander_gui.themes import build_palette, set_active_theme
from commander_gui.ui.common import (
    GAMMA_PROFILE,
    count_active_mods,
    instance_window_title,
    notify_desktop,
    play_click_sound,
    resume_after_shutdown,
    shutdown_active_runners,
)
from commander_gui.ui.deck_switch import switch_mode
from commander_gui.ui.install_page import _resume_state_matches
from commander_gui.ui.title_bar import (
    BUTTON_HEIGHT,
    EdgeResizeFilter,
    WindowDragFilter,
    attach_resize_filter,
    build_window_buttons,
    pin_top_right,
    update_max_button,
)

from . import gamepad as pad
from .deck_theme import build_deck_stylesheet
from .focus import DeckFocusController
from .power import SleepInhibitor, battery_state
from .scale import px, scale, scale_for, set_scale
from .screens import SCREENS
from .widgets import (
    DECK_H,
    DECK_W,
    FOOTER_H,
    HEADER_H,
    MARGIN_X,
    NAV_H,
    DeckBackdrop,
    DeckHintBar,
    DeckOverlay,
    DeckToast,
    confirm_overlay,
    deck_button,
    deck_label,
    repolish,
)

#: A job that ran at least this long ends with a result panel that waits
#: for A, not just a toast (see DeckWindow.announce_finished).
_ANNOUNCE_AFTER_S = 60.0

#: Smallest window Deck Mode is laid out for when run on an ordinary
#: desktop. Below this the nav cells stop being touch-sized.
_MIN_W, _MIN_H = 1024, 640

#: The first-run wizard. Not a nav tab: it is where a user with no profile
#: lands, and where the Profile screen's "New profile" leads.
SETUP_KEY = "setup"

#: How often the clock and battery refresh.
_CLOCK_MS = 30_000

#: A window resize settles for this long before the interface is rebuilt at
#: a new scale - dragging a window edge must not rebuild on every pixel.
_RESCALE_DEBOUNCE_MS = 300

#: Restyling the whole app costs a noticeable fraction of a second on the
#: Deck. Changes that come in bursts (tapping + for text size) are applied
#: once, this long after the last one.
_RESTYLE_DEBOUNCE_MS = 400

#: Set to "1" to leave the gamepad alone (the test suite, or a user whose
#: controller is already mapped to keys and who wants only that).
NO_GAMEPAD_ENV = "COMMANDER_DECK_NO_GAMEPAD"

_DEFAULT_HINTS = (
    ("A", "Select"),
    ("B", "Back"),
    ("L1 R1 / L2 R2", "Switch tab"),
    ("☰", "Menu"),
)
_OVERLAY_HINTS = (("A", "Select"), ("B", "Close"))

_OPERATION_REASONS = {
    "gamma": "Installing or updating GAMMA",
    "anomaly": "Installing STALKER Anomaly",
    "dependencies": "Installing dependencies",
    "proton": "Installing GE-Proton",
    "move": "Moving the installation",
    "mod_install": "Installing a mod",
}


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


class _UpdateBadge(QLabel):
    """The footer's COMMANDER update status; a tap offers the update."""

    def __init__(self, window: DeckWindow) -> None:
        super().__init__("")
        self._window = window
        self.setObjectName("deckHint")

    def mousePressEvent(self, event) -> None:
        self._window.offer_commander_update()
        super().mousePressEvent(event)


class DeckWindow(QMainWindow):
    """Steam Deck Mode's main window."""

    def __init__(self, fatal: tuple[str, str] | None = None) -> None:
        super().__init__()
        self.setWindowTitle("STALKER COMMANDER")

        self.settings = load_settings()
        self.install_busy = False
        self.install_operation: str | None = None
        # COMMANDER's own title bar, as on the desktop - only ever for the
        # windowed Deck Mode on a desktop (see show_for_environment()).
        self.custom_title_bar = False
        self._drag_filter = WindowDragFilter(self)
        self._resize_filter = EdgeResizeFilter(self)

        self._fatal = fatal
        self._pages: dict[str, QWidget] = {}
        self._nav_buttons: dict[str, QWidget] = {}
        self._overlay: DeckOverlay | None = None
        #: Overlays covered by a stacked one (the keyboard over a picker),
        #: restored in order as the ones above them close.
        self._overlay_stack: list[DeckOverlay] = []
        self._current_key = ""
        self._closing_confirmed = False
        self._inhibitor = SleepInhibitor()
        self._last_sheet = ""
        self._restyle_timer = QTimer(self)
        self._restyle_timer.setSingleShot(True)
        self._restyle_timer.setInterval(_RESTYLE_DEBOUNCE_MS)
        self._restyle_timer.timeout.connect(self.apply_style)
        self._rescale_timer = QTimer(self)
        self._rescale_timer.setSingleShot(True)
        self._rescale_timer.setInterval(_RESCALE_DEBOUNCE_MS)
        self._rescale_timer.timeout.connect(self._apply_window_scale)
        self._rescale_pending = False

        set_scale(1.0)
        self.apply_style()
        self._build_ui()

        self._focus = DeckFocusController(self)
        # On the application, not on the window: Qt delivers key events to
        # the focused widget, so a window-level filter would never see the
        # D-pad at all. The controller ignores anything outside this window.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._focus)

        self.gamepad = pad.GamepadMonitor(self)
        self.gamepad.action.connect(self._focus.on_pad_action)
        self.gamepad.scroll_axis.connect(self._focus.on_pad_scroll)
        self.gamepad.hold_gate = self._accept_hold_wanted
        if os.environ.get(NO_GAMEPAD_ENV) != "1":
            self.gamepad.start()

        # A stuck user needs a keyboard escape hatch that does not depend on
        # reaching the Settings screen.
        exit_shortcut = QShortcut(QKeySequence("Ctrl+Shift+D"), self)
        exit_shortcut.activated.connect(lambda: self.exit_deck_mode())

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(_CLOCK_MS)
        self._clock_timer.timeout.connect(self.update_clock)
        self._clock_timer.start()
        self.update_clock()

        if fatal is None:
            if self.settings.active_profile is None and not self.settings.profiles:
                self.set_page(SETUP_KEY)
            else:
                self.set_page(self.start_screen())
                from commander_gui.ui.welcome_overlay import (
                    mark_welcome_seen,
                    should_show_welcome,
                )

                if should_show_welcome():
                    mark_welcome_seen()
                    # After the first rescale has had its chance to run, so
                    # the panel opens at the size it will stay.
                    QTimer.singleShot(_RESCALE_DEBOUNCE_MS + 200, self.show_welcome)
        QTimer.singleShot(0, self._focus.ensure_focus)

        #: The newest release tag if COMMANDER is out of date, "" if up to
        #: date, None until the startup check answers.
        self._commander_update: str | None = None
        self._render_update_badge()
        if fatal is None:
            self._check_commander_update()

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

    def start_screen(self) -> str:
        start = gui_settings.load_gui_settings().get("deck_start_screen", "play")
        return start if start in self._screen_keys else "play"

    def apply_style(self) -> None:
        """Apply the Deck stylesheet and palette for the saved theme."""
        state = gui_settings.load_gui_settings()
        theme = state.get("theme") or "gamma"
        set_active_theme(theme)
        app = QApplication.instance()
        if app is None:
            return
        self._restyle_timer.stop()
        sheet = build_deck_stylesheet(
            theme,
            font_scale=int(state.get("deck_font_scale") or 100),
            font_family=state.get("font_family") or "Exo 2",
        )
        app.setPalette(build_palette(theme))
        # Re-setting an identical sheet still re-polishes every widget in
        # the application; skip it when nothing actually changed.
        if sheet != self._last_sheet:
            self._last_sheet = sheet
            app.setStyleSheet(sheet)

    def apply_style_later(self) -> None:
        """Restyle once things stop changing (see _RESTYLE_DEBOUNCE_MS)."""
        self._restyle_timer.start()

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        central = DeckBackdrop()
        central.setObjectName("deckCentral")
        # Fills the whole window. It used to be capped at 1280x800 and
        # centred, which left a docked or maximised Deck Mode as a small
        # island; now the interface is rebuilt at a larger scale instead
        # (see resizeEvent / Steamdeck.scale).
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
        root.addWidget(self._build_footer())

        self.setCentralWidget(central)
        # The widget tree used for overlay geometry, the toast, and
        # DeckFocusController's scope.
        self._deck_root = central

        self.toast = DeckToast(central)
        self._status_shim = _DeckStatusBar(self.toast)

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("deckHeader")
        header.setFixedHeight(px(HEADER_H))
        layout = QHBoxLayout(header)
        layout.setContentsMargins(px(MARGIN_X), 0, px(MARGIN_X), 0)
        layout.setSpacing(px(14))

        # Wordmark and byline on one line: the header is 48px now, and
        # two stacked lines of text were most of why it used to be 72.
        wordmark = deck_label(tr("COMMANDER"), role="wordmark")
        layout.addWidget(wordmark)
        byline = deck_label(tr("by SSH-Kitty"), role="byline")
        # Nudged down to the wordmark's baseline rather than centred on it.
        byline.setContentsMargins(0, px(6), 0, 0)
        layout.addWidget(byline)
        layout.addStretch(1)

        # A separate, distinctly-colored label - matching the desktop
        # topbar's own #modCounter.
        self.mod_counter_label = deck_label("", role="modCounter")
        layout.addWidget(self.mod_counter_label)
        self.awake_label = deck_label(tr("Staying awake"), role="headerInfo")
        self.awake_label.setObjectName("deckAwake")
        self.awake_label.hide()
        layout.addWidget(self.awake_label)
        # Game Mode has no system tray and COMMANDER is full screen, so the
        # battery is otherwise one QAM press away.
        self.battery_label = deck_label("", role="headerInfo")
        layout.addWidget(self.battery_label)
        # Nudged down a few px like the desktop bar's counter: centered
        # exactly, the counter text reads higher than the wordmark.
        for label in (self.mod_counter_label, self.awake_label, self.battery_label):
            label.setContentsMargins(0, px(6), 0, 0)

        # Minimize/maximize/close floating in the top-right corner when
        # COMMANDER draws its own title bar (see _sync_title_bar()).
        self._header = header
        self._title_strip = build_window_buttons(
            self, self._drag_filter, on_close=self._ask_close
        )
        pin_top_right(self._title_strip, header)
        for widget in (header, wordmark, byline, self.mod_counter_label):
            widget.installEventFilter(self._drag_filter)
        self._sync_title_bar()
        return header

    # ----------------------------------------------------- own title bar
    def apply_title_bar(self, custom: bool) -> None:
        """Draw COMMANDER's own title bar (frameless) or use the desktop's."""
        # Offscreen/minimal platforms (tests) have no window manager to
        # hand moves and resizes to.
        custom = custom and QGuiApplication.platformName() not in ("offscreen", "minimal")
        visible = self.isVisible()
        self.custom_title_bar = custom
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, custom)
        self._sync_title_bar()
        if visible:
            self.show()
        attach_resize_filter(self, self._resize_filter, custom)

    def _sync_title_bar(self) -> None:
        header = getattr(self, "_header", None)
        if header is None or not isValid(header):
            return
        # Maximized or full screen, Deck Mode fills the screen like on the
        # Deck itself, so the window buttons step aside (double-click the
        # header, or the desktop's own shortcuts, to restore).
        shown = self.custom_title_bar and not (self.isMaximized() or self.isFullScreen())
        self._title_strip.setVisible(shown)
        # The strip's height goes on top of the header, split above and
        # below its content so the content stays centered.
        extra = BUTTON_HEIGHT if shown else 0
        header.setFixedHeight(px(HEADER_H) + extra)
        margins = header.layout().contentsMargins()
        header.layout().setContentsMargins(
            margins.left(), extra // 2, margins.right(), extra - extra // 2
        )

    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _build_nav(self) -> QWidget:
        nav = QWidget()
        nav.setObjectName("deckNav")
        nav.setFixedHeight(px(NAV_H))
        layout = QHBoxLayout(nav)
        layout.setContentsMargins(px(6), px(4), px(6), px(4))
        layout.setSpacing(px(4))
        for key, title, glyph in SCREENS:
            # Glyph and label on one line - the stacked two-line cell was
            # what made the bar 88px tall.
            button = deck_button(f"{glyph}  {tr(title)}", role="normal")
            button.setObjectName("deckNavCell")
            button.setMinimumHeight(px(NAV_H - 10))
            # Never a D-pad or stick stop: moving around stays inside the
            # screen, and tabs change with the shoulder buttons (L1/L2 and
            # R1/R2). Still a tap target on the touchscreen.
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _=False, k=key: self.set_page(k))
            layout.addWidget(button, 1)
            self._nav_buttons[key] = button
        return nav

    def _build_footer(self) -> QWidget:
        footer = QWidget()
        footer.setObjectName("deckFooter")
        footer.setFixedHeight(px(FOOTER_H))
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(px(MARGIN_X), 0, px(MARGIN_X), 0)
        layout.setSpacing(px(10))
        # COMMANDER's own update status - desktop shows it bottom-right;
        # here the right-hand end belongs to the button prompts.
        self.update_badge = _UpdateBadge(self)
        layout.addWidget(self.update_badge)
        layout.addWidget(deck_label("·", role="hint"))
        self.profile_label = deck_label("", role="hint")
        self.profile_label.setObjectName("deckFooterProfile")
        layout.addWidget(self.profile_label)
        layout.addWidget(deck_label("·", role="hint"))
        layout.addWidget(deck_label(tr("Deck Mode"), role="hint"))
        layout.addStretch(1)
        self.hint_bar = DeckHintBar()
        layout.addWidget(self.hint_bar)
        layout.addSpacing(px(12))
        self.clock_label = deck_label("", role="hint")
        self.clock_label.setObjectName("deckClock")
        layout.addWidget(self.clock_label)
        return footer

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
        return page

    # ----------------------------------------------------------- screens
    def _create_page(self, key: str) -> QWidget:
        # Imported lazily and per-key so one screen failing to import cannot
        # stop the window from opening on the others.
        from .screens.dashboard import DashboardScreen
        from .screens.install import InstallScreen
        from .screens.mods import ModsScreen
        from .screens.play import PlayScreen
        from .screens.settings import SettingsScreen
        from .screens.setup import SetupScreen
        from .screens.system import SystemScreen
        from .screens.update import UpdateScreen
        from .screens.utilities import UtilitiesScreen

        classes = {
            "dashboard": DashboardScreen,
            "play": PlayScreen,
            "install": InstallScreen,
            "update": UpdateScreen,
            "mods": ModsScreen,
            "system": SystemScreen,
            "utilities": UtilitiesScreen,
            "settings": SettingsScreen,
            SETUP_KEY: SetupScreen,
        }
        return classes[key](self)

    def _ensure_page(self, key: str) -> QWidget:
        """Build ``key``'s screen on first visit and cache it.

        Same lazy strategy as the desktop window: constructing a screen can
        kick off CLI and network work, so doing all of them upfront would
        make startup slower and noisier than it needs to be.
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
        """Switch to a screen by nav key (or the setup wizard)."""
        if self._fatal is not None:
            return
        if key not in self._screen_keys and key != SETUP_KEY:
            return
        page = self._ensure_page(key)
        self._current_key = key
        self.stack.setCurrentWidget(page)
        for nav_key, button in self._nav_buttons.items():
            button.setProperty("current", "true" if nav_key == key else "false")
            button.style().unpolish(button)
            button.style().polish(button)
        self.update_mod_counter()
        self.update_hints()
        # Refresh after Qt has laid the new screen out, so anything sized
        # from its own contents measures correctly.
        QTimer.singleShot(0, page.refresh)
        QTimer.singleShot(0, self._focus_new_page)

    def _focus_new_page(self) -> None:
        # Focus follows the tab change into the new screen: a focus ring
        # left behind on the old screen's (now hidden) widget, or on the
        # nav bar, would make the next D-pad press start from nowhere.
        app = QApplication.instance()
        current = app.focusWidget() if app is not None else None
        page = self._pages.get(self._current_key)
        if page is None or self._overlay is not None:
            self._focus.ensure_focus()
            return
        if current is not None and page.isAncestorOf(current) and current.isVisible():
            return
        # The control used here last time, else the screen's own default,
        # else its first control in reading order.
        target = self._focus.entry_point(page)
        if target is not None:
            self._focus.focus(target)
        else:
            self._focus.ensure_focus()

    def current_key(self) -> str:
        return self._current_key

    def current_page(self) -> QWidget | None:
        return self._pages.get(self._current_key)

    def cycle_page(self, step: int) -> None:
        """L1/R1: previous/next tab, wrapping round."""
        if self._fatal is not None or self._overlay is not None:
            return
        keys = self._screen_keys
        if self._current_key in keys:
            index = (keys.index(self._current_key) + step) % len(keys)
        else:
            index = 0 if step > 0 else len(keys) - 1
        self.set_page(keys[index])
        # Unlike a tap on a tab, a shoulder press should land in the
        # screen's content, ready for the D-pad - where it was left last
        # time, if the screen was visited before.
        page = self._pages.get(keys[index])
        if page is not None:
            QTimer.singleShot(0, self, lambda p=page: self._focus_entry(p))

    def _focus_entry(self, page: QWidget) -> None:
        if not isValid(page) or page is not self.current_page() or self._overlay is not None:
            return
        target = self._focus.entry_point(page)
        if target is not None:
            self._focus.focus(target)

    # ------------------------------------------------------------- input
    def update_hints(self) -> None:
        if self._overlay is not None:
            hints = getattr(self._overlay, "hints", None) or _OVERLAY_HINTS
        else:
            page = self._pages.get(self._current_key)
            hints = list(getattr(page, "hints", lambda: None)() or _DEFAULT_HINTS)
            # On every screen: where to find the controller layout. Tapping
            # it opens it too.
            hints.append(("\u29c9", "Controls"))
        self.hint_bar.set_hints(
            [(button, tr(label)) for button, label in hints],
            actions={"\u29c9": self.show_controller_help},
        )

    def _accept_hold_wanted(self) -> bool:
        """GamepadMonitor.hold_gate: may this A press become a hold?

        Only where a screen asks for it (the Mods list, to grab a mod) and
        nothing is on top of it - every other A keeps firing on the press.
        """
        if self._overlay is not None or not self.isActiveWindow():
            return False
        wants = getattr(self.current_page(), "wants_hold", None)
        return bool(callable(wants) and wants())

    def handle_direction(self, action: str) -> bool:
        """A screen's first refusal of the D-pad (a grabbed mod moving)."""
        if self._overlay is not None:
            return False
        on_direction = getattr(self.current_page(), "on_direction", None)
        return bool(callable(on_direction) and on_direction(action))

    def handle_action(self, action: str) -> bool:
        """Everything the focus controller does not handle itself.

        Resolved from the most local context outwards: the open overlay,
        then the current screen, then the window's own defaults.
        """
        overlay = self._overlay
        if overlay is not None:
            handler = getattr(overlay, "action_handler", None)
            if callable(handler) and handler(action):
                return True
            if action == pad.MENU:
                self.dismiss_overlay()
                return True
            if action == pad.HELP:
                # The layout is one press away even mid-picker; it stacks,
                # so closing it returns to the overlay underneath.
                self.show_controller_help()
                return True
            return False
        if action == pad.TAB_PREV:
            self.cycle_page(-1)
            return True
        if action == pad.TAB_NEXT:
            self.cycle_page(1)
            return True
        page = self._pages.get(self._current_key)
        on_action = getattr(page, "on_action", None)
        if callable(on_action) and on_action(action):
            return True
        if action == pad.SEARCH:
            focused = QApplication.focusWidget()
            if isinstance(focused, QLineEdit) and self.isAncestorOf(focused):
                self.open_keyboard(focused)
                return True
            return False
        if action == pad.MENU:
            self._ask_exit()
            return True
        if action == pad.HELP:
            self.show_controller_help()
            return True
        return False

    def open_keyboard(self, target: QLineEdit, **kwargs):
        from .osk import open_keyboard

        return open_keyboard(self, target, **kwargs)

    def show_controller_help(self) -> None:
        """The controller layout (View button, F1, the footer's "Controls")."""
        from .controller_map import show_controller_map

        if self._fatal is None and not getattr(self._overlay, "is_controller_map", False):
            show_controller_map(self)

    # ------------------------------------------ MainWindow-compatible API
    def set_install_busy(self, busy: bool, operation: str | None = None) -> None:
        """The global install mutex, with the same contract as MainWindow's.

        Screens read ``install_busy`` before enabling anything that writes to
        the install tree, so two CLI processes can never run against it at
        once. Also where the machine is kept awake for the job's duration.
        """
        if busy and not self.install_busy:
            self._busy_since = time.monotonic()
        self.install_busy = bool(busy)
        self.install_operation = operation if busy else None
        if self.install_busy:
            reason = _OPERATION_REASONS.get(operation or "", "Running a maintenance task")
            self.awake_label.setVisible(self._inhibitor.hold(reason))
        else:
            self._inhibitor.release()
            self.awake_label.hide()
        for page in self._pages.values():
            page.on_busy_changed(self.install_busy)
            page.on_install_activity_changed(self.install_operation)
        if not self.install_busy and self._rescale_pending:
            # A resize that happened mid-install was held back; apply it now.
            self._rescale_timer.start()

    def announce_finished(self, title: str, message: str, *, ok: bool = True) -> None:
        """A long job ended: make sure someone who put the Deck down notices.

        A GAMMA install runs for an hour or more, long enough to set the
        Deck aside, and a toast that fades after a few seconds is easy to
        miss. So: the PDA chime (unless turned off in Settings), a desktop
        notification when the window is not in front (Desktop Mode), and -
        for a job that ran longer than a minute - a result panel that stays
        until A is pressed. Short jobs keep the plain toast.
        """
        self.notify(message, 8000 if not ok else 4000)
        try:
            chime = gui_settings.load_gui_settings().get("deck_finish_sound", True)
        except Exception:  # noqa: BLE001 - settings trouble must not hide the result
            chime = True
        if chime:
            play_click_sound()
        try:
            buzz = gui_settings.load_gui_settings().get("deck_finish_rumble", True)
        except Exception:  # noqa: BLE001 - settings trouble must not hide the result
            buzz = True
        if buzz:
            # One short buzz for done, a long strong one for failed: felt
            # even with the sound off or the Deck face-down on a table.
            if ok:
                self.gamepad.rumble(strength=0.55, length_ms=300)
            else:
                self.gamepad.rumble(strength=0.9, length_ms=700)
        if not self.isActiveWindow():
            notify_desktop(title, message)
        elapsed = time.monotonic() - getattr(self, "_busy_since", time.monotonic())
        if elapsed < _ANNOUNCE_AFTER_S:
            return
        body = deck_label(message, wrap=True)
        overlay = DeckOverlay(
            ("✓  " if ok else "✕  ") + title,
            body,
            [(tr("OK"), self.dismiss_overlay, "primary")],
            panel_width=900,
        )
        self.show_overlay(overlay, stacked=self._overlay is not None)

    def refresh_settings(self) -> None:
        self.settings = load_settings()
        self.update_mod_counter()

    def update_clock(self) -> None:
        self.clock_label.setText(time.strftime("%H:%M"))
        state = battery_state()
        if state is None:
            self.battery_label.hide()
            return
        self.battery_label.show()
        mark = " ⚡" if state.charging else ""
        self.battery_label.setText(f"{state.percent}%{mark}")
        low = state.percent <= 15 and not state.on_ac
        self.battery_label.setObjectName("deckBatteryLow" if low else "deckHeaderInfo")
        repolish(self.battery_label)

    def update_mod_counter(self) -> None:
        profile = self.settings.active_profile
        if profile is None:
            self.profile_label.setText(tr("No Profile"))
            self.mod_counter_label.hide()
            return
        self.profile_label.setText(profile.profile_name or tr("No Profile"))
        # None means "count unavailable" (GAMMA not installed yet, or the
        # profile folder is missing) - show just the profile name then,
        # rather than a misleading zero.
        counts = count_active_mods(
            profile.gamma, profile.mo2_profile or GAMMA_PROFILE
        )
        if counts is None:
            self.mod_counter_label.hide()
            return
        self.mod_counter_label.show()
        enabled, _total = counts
        # Same resume-state check the desktop topbar uses to tell an
        # interrupted install apart from a genuinely small mod count.
        resume_state = gui_settings.load_gui_settings().get("gamma_install_resume")
        incomplete = _resume_state_matches(resume_state, profile)
        if incomplete:
            self.mod_counter_label.setText(
                tr("{enabled} Mods (incomplete)", enabled=enabled)
            )
        else:
            self.mod_counter_label.setText(tr("{enabled} Mods", enabled=enabled))
        self.mod_counter_label.setObjectName(
            "deckModCounterWarn" if incomplete else "deckModCounter"
        )
        repolish(self.mod_counter_label)

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

    def show_overlay(self, overlay: DeckOverlay, *, stacked: bool = False) -> None:
        """Display ``overlay`` over the whole window, focusing its first button.

        Normally replaces any open overlay. ``stacked`` keeps the open one
        underneath instead - the on-screen keyboard opened from a picker's
        filter box must hand back to that picker, not destroy it.
        """
        app = QApplication.instance()
        focused = app.focusWidget() if app is not None else None
        if stacked and self._overlay is not None:
            opener = focused if focused is not None and self._overlay.isAncestorOf(focused) else None
            self._overlay.hide()
            self._overlay_stack.append(self._overlay)
            self._overlay = None
        else:
            # Replacing one overlay with another (a picker handing over to
            # the next step) returns, in the end, to what opened the first.
            previous = self._overlay_stack[0] if self._overlay_stack else self._overlay
            if previous is not None:
                opener = getattr(previous, "deck_opener", None)
                rect = getattr(previous, "deck_opener_rect", None)
            else:
                opener = focused if focused is not None and self.isAncestorOf(focused) else None
                rect = None
            self._clear_overlays()
            if rect is not None:
                overlay.deck_opener_rect = rect
        if opener is not None and isValid(opener):
            from .focus import rect_in

            overlay.deck_opener = opener
            if opener.isVisible():
                overlay.deck_opener_rect = rect_in(opener, self)
        central = self._deck_root
        overlay.setParent(central)
        overlay.setGeometry(central.rect())
        overlay.show()
        overlay.raise_()
        self._overlay = overlay
        self.update_hints()
        if overlay.default_button is not None:
            overlay.default_button.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self._focus.ensure_focus()

    def dismiss_overlay(self, *, refocus: bool = True) -> None:
        if self._overlay is None:
            return
        overlay, self._overlay = self._overlay, None
        opener = getattr(overlay, "deck_opener", None)
        opener_rect = getattr(overlay, "deck_opener_rect", None)
        overlay.hide()
        overlay.closed.emit()
        overlay.deleteLater()
        if self._overlay_stack:
            self._overlay = self._overlay_stack.pop()
            self._overlay.setGeometry(self._deck_root.rect())
            self._overlay.show()
            self._overlay.raise_()
        self.update_hints()
        if refocus:
            self._restore_focus(opener, opener_rect)

    def _restore_focus(self, opener, opener_rect) -> None:
        """Back to the control that opened the overlay just closed.

        Landing on the first control of the screen instead - which is what
        used to happen - meant choosing a runner from the middle of the Play
        screen threw you back to its top.
        """
        from .focus import audit_focusables, closest_to, usable

        scope = self._focus.scope()
        if usable(opener) and scope.isAncestorOf(opener):
            self._focus.focus(opener)
            return
        if opener_rect is not None:
            target = closest_to(opener_rect, audit_focusables(scope), self)
            if target is not None:
                self._focus.focus(target)
                return
        self._focus.ensure_focus()

    def _clear_overlays(self) -> None:
        for overlay in [*self._overlay_stack, self._overlay]:
            if overlay is not None:
                overlay.hide()
                overlay.closed.emit()
                overlay.deleteLater()
        self._overlay_stack.clear()
        self._overlay = None

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

    # -------------------------------------------------------------- back
    def handle_back(self) -> None:
        """B / Escape. Resolved from the most local context outwards."""
        if self._overlay is not None:
            # An overlay with levels of its own (a folder browser, a FOMOD
            # installer's steps) goes back one level before it closes.
            handler = getattr(self._overlay, "back_handler", None)
            if callable(handler) and handler():
                return
            self.dismiss_overlay()
            return
        page = self._pages.get(self._current_key)
        if page is not None and page.on_back():
            return
        start = self.start_screen()
        if (
            self._fatal is None
            and self._current_key != start
            and self._current_key != SETUP_KEY
        ):
            self.set_page(start)
            return
        self._ask_exit()

    def _ask_exit(self) -> None:
        self.show_quick_menu()

    def show_quick_menu(self) -> None:
        """☰ Menu (and B on the start screen): the few things worth a button
        from anywhere - play, leave Deck Mode, quit - and a way back."""
        body = deck_label(
            tr("Play, leave Deck Mode, or close COMMANDER."), role="body", wrap=True
        )
        buttons = []
        if self._fatal is None and self.settings.active_profile is not None:
            buttons.append(("\u25b6  " + tr("Play GAMMA"), self._quick_play, "primary"))
        buttons += [
            (tr("Exit Deck Mode"), self.exit_deck_mode, "normal"),
            (tr("Quit COMMANDER"), self._quit, "danger"),
            (tr("Back"), self.dismiss_overlay, "normal"),
        ]
        self.show_overlay(
            DeckOverlay(tr("Menu"), body, buttons, default_index=len(buttons) - 1)
        )

    # ------------------------------------------------ welcome and updates
    def show_welcome(self) -> None:
        from .welcome import show_welcome

        if self._fatal is None and self._overlay is None:
            show_welcome(self)

    def _check_commander_update(self) -> None:
        from commander_gui import __version__
        from commander_gui.ui.common import BackgroundTask
        from commander_gui.updates import (
            check_commander_update,
            effective_update_channel,
        )

        channel = effective_update_channel(
            gui_settings.load_gui_settings().get("update_channel"), __version__
        )
        task = BackgroundTask(
            check_commander_update, __version__, channel=channel, parent=self
        )
        task.result.connect(self._on_commander_update)
        task.error.connect(lambda _m: None)
        self._update_task = task
        task.start()

    def _on_commander_update(self, tag: object) -> None:
        self._update_task = None
        self._commander_update = tag if isinstance(tag, str) and tag else ""
        self._render_update_badge()

    def _render_update_badge(self) -> None:
        badge = getattr(self, "update_badge", None)
        if badge is None:
            return
        tag = getattr(self, "_commander_update", None)
        if tag is None:
            badge.setText(tr("Checking for updates..."))
            badge.setObjectName("deckHint")
        elif tag:
            badge.setText(tr("COMMANDER update available"))
            badge.setObjectName("deckFooterWarn")
        else:
            badge.setText(tr("COMMANDER is up to date"))
            badge.setObjectName("deckFooterOk")
        badge.setCursor(
            Qt.CursorShape.PointingHandCursor if tag else Qt.CursorShape.ArrowCursor
        )
        repolish(badge)

    def offer_commander_update(self) -> None:
        """Tapped the footer badge while an update is available."""
        tag = getattr(self, "_commander_update", None)
        if not tag:
            return
        from .welcome import RELEASES_URL, open_url

        self.confirm(
            tr("COMMANDER update available"),
            tr(
                "COMMANDER {tag} is available. Open the Releases page to "
                "download it?",
                tag=tag,
            ),
            lambda: open_url(self, RELEASES_URL),
            confirm_text=tr("Open Releases"),
        )

    def _quick_play(self) -> None:
        self.dismiss_overlay()
        self.set_page("play")
        page = self._pages.get("play")
        if page is not None and hasattr(page, "play"):
            QTimer.singleShot(0, page.play)

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
        # Every screen's background checks (dependency probe, update check,
        # size scan, system checks, ...) run on a QThread owned by that
        # screen. Deleting a screen while one is still running destroys a
        # live QThread, which Qt answers by aborting the whole process -
        # so cancel them first, detaching any that won't stop in time, the
        # same way MainWindow.switch_language() does.
        shutdown_active_runners(timeout_ms=2000)
        # An overlay that knows how to re-open itself (the Welcome screen)
        # survives the rebuild; the rest are dropped as before. Read before
        # _overlay is cleared below - reading it after always found None,
        # so a language change closed the Welcome screen for good.
        reopen = getattr(self._overlay, "reopen", None)
        self._pages.clear()
        self._nav_buttons.clear()
        self._overlay = None
        self._overlay_stack.clear()
        old = self.centralWidget()
        self.apply_style()
        self._build_ui()
        if old is not None:
            old.deleteLater()
        resume_after_shutdown()
        self.update_clock()
        self._render_update_badge()
        if self._fatal is None and getattr(self, "_commander_update", None) is None:
            # The rebuild cancels background work, and a check still out
            # when it happened (the Deck rescales right after starting)
            # would leave the badge on "Checking..." for good.
            self._check_commander_update()
        self.set_page(key if key in self._screen_keys or key == SETUP_KEY else "play")
        if callable(reopen):
            QTimer.singleShot(0, reopen)

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
        custom = bool(gui_settings.load_gui_settings().get("custom_title_bar", True))
        if os.environ.get("COMMANDER_DECK_WINDOWED") == "1":
            self._show_windowed(custom)
            return
        if is_steam_deck() or in_game_mode():
            self.showFullScreen()
            return
        self.setMinimumSize(_MIN_W, _MIN_H)
        self._show_windowed(custom)

    def _show_windowed(self, custom: bool) -> None:
        """A normal window, with COMMANDER's own title bar if chosen.

        Taller by the title strip so the interface below keeps its size.
        """
        self.apply_title_bar(custom)
        extra = BUTTON_HEIGHT if self.custom_title_bar else 0
        self.resize(DECK_W, DECK_H + extra)
        self.show()

    def apply_pending_rescale(self) -> None:
        if self._rescale_pending:
            self._rescale_timer.start()

    def session_active(self) -> bool:
        """True while a game/MO2 session started from Deck Mode is running."""
        play = self._pages.get("play")
        controller = getattr(play, "controller", None)
        return bool(controller is not None and controller.is_active())

    # ------------------------------------------------------------- scaling
    def _apply_window_scale(self) -> None:
        """Rebuild at the scale the current window size calls for."""
        central = getattr(self, "_deck_root", None)
        if central is None:
            return
        target = scale_for(self.width(), self.height())
        if abs(target - scale()) < 0.01:
            self._rescale_pending = False
            return
        if self.install_busy or self.session_active():
            # Rebuilding tears down every widget, including the progress
            # view a running install is feeding and the Play screen's launch
            # controller (its watchdog, playtime timer and Force Stop). Wait
            # until the install or the game session is over.
            self._rescale_pending = True
            return
        self._rescale_pending = False
        set_scale(target)
        self.rebuild()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            self._sync_gamepad()
        elif event.type() == QEvent.Type.WindowStateChange:
            update_max_button(self)
            self._sync_title_bar()

    def _sync_gamepad(self) -> None:
        """Read the controller only while this window can use it.

        While a game started from here has the screen, every handle on the
        pad is closed; they reopen the moment COMMANDER is in front again.
        """
        monitor = getattr(self, "gamepad", None)
        if monitor is None or os.environ.get(NO_GAMEPAD_ENV) == "1":
            return
        if self.isActiveWindow() or not self.session_active():
            monitor.resume()
        else:
            monitor.pause()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_rescale_timer"):
            self._rescale_timer.start()
        central = getattr(self, "_deck_root", None)
        if central is None:
            return
        if self._overlay is not None:
            self._overlay.setGeometry(central.rect())
        # setCentralWidget() can trigger a resize before _build_ui() has got
        # as far as creating the toast.
        toast = getattr(self, "toast", None)
        if toast is not None:
            toast.reposition()

    def _confirm_close(self) -> None:
        self._closing_confirmed = True
        self.close()

    def _ask_close(self) -> None:
        """The window's X: switching back to the desktop UI is usually what
        was meant, so offer it next to quitting rather than just closing."""
        body = deck_label(
            tr("Switch to the desktop interface, or close COMMANDER?"),
            role="body",
            wrap=True,
        )
        buttons = [
            (tr("Switch to Desktop"), self.exit_deck_mode, "primary"),
            (tr("Exit COMMANDER"), self._quit, "danger"),
            (tr("Cancel"), self.dismiss_overlay, "normal"),
        ]
        self._clear_overlays()
        self.show_overlay(
            DeckOverlay(tr("Close Deck Mode"), body, buttons, default_index=0)
        )

    def closeEvent(self, event) -> None:
        # Deliberately does NOT persist window_width/window_height: Deck Mode
        # is always 1280x800 or full screen, and saving that would shrink the
        # desktop window the next time the user switches back.
        if event.spontaneous() and not self._closing_confirmed:
            # From the window manager (the title bar's X, Alt+F4). Our own
            # close() calls - Quit COMMANDER, the switch to the desktop UI,
            # the busy confirmation - are not spontaneous and go straight on.
            event.ignore()
            self._ask_close()
            return
        if self.install_busy and not self._closing_confirmed:
            # An overlay, not a QMessageBox: this is the "no dialogs under
            # gamescope" rule too, and a modal question here used to be the
            # one QDialog left in Deck Mode.
            event.ignore()
            self.confirm(
                tr("Busy"),
                tr(
                    "An install, update or dependency download is still "
                    "running. Quit anyway?"
                ),
                self._confirm_close,
                confirm_text=tr("Quit"),
                confirm_role="danger",
            )
            return
        # Only now, when the window is really going: the focus filter lives
        # on the application, so it must come off explicitly - but taking it
        # off before a close that then got cancelled left a window with a
        # dead D-pad.
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._focus)
        self.gamepad.stop()
        self._clock_timer.stop()
        self._inhibitor.release()
        super().closeEvent(event)
