"""Main application window: top tab navigation + stacked pages."""

from __future__ import annotations

import time
from typing import ClassVar

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QPropertyAnimation,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, __version_label__, gui_settings
from ..i18n import LANGUAGE_INFO, active_language, set_active_language
from ..self_update import (
    commander_appimage_path,
    download_and_install_commander_update,
    relaunch_commander,
    switch_commander_build,
)
from ..settings import load_settings
from ..themes import (
    THEME_INFO,
    active_theme,
    active_theme_tokens,
    build_palette,
    build_stylesheet,
    set_active_theme,
)
from ..updates import check_commander_update, check_updates, effective_update_channel
from .about_page import _DISCORD, AboutPage
from .common import (
    OK_GREEN,
    STATUS_GREY,
    WARN,
    BackgroundTask,
    NoWheelComboBox,
    count_active_mods,
    human_size,
    instance_window_title,
    mo2_running,
    notify_desktop,
    open_url,
    resume_after_shutdown,
    shutdown_active_runners,
    tr,
)
from .dashboard import DashboardPage
from .help_page import HelpPage
from .install_page import InstallPage, _resume_state_matches
from .mod_manager_page import ModManagerPage
from .play_page import PlayPage
from .profiles_page import ProfilesPage
from .settings_page import SettingsPage
from .system_check_page import SystemCheckPage
from .title_bar import (
    EdgeResizeFilter,
    WindowDragFilter,
    attach_resize_filter,
    build_window_buttons,
    pin_top_right,
    update_max_button,
)
from .update_page import UpdatePage
from .utilities_page import UtilitiesPage

NAV_ITEMS = [
    ("dashboard", "Dashboard"),
    ("systemcheck", "System Check"),
    ("install", "Install"),
    ("play", "Play"),
    ("update", "Updates"),
    ("modmanager", "Mod Manager"),
    ("profiles", "Profiles"),
    ("utilities", "Utilities"),
    ("help", "Help"),
    ("about", "About"),
]

#: Tab-bar position for each nav key, fixed by NAV_ITEMS order. Separate from
#: page construction (see _ensure_page()): pages are built lazily on first
#: visit, so a page's stack position isn't known until then, but its tab
#: position is known upfront.
_TAB_INDEX: dict[str, int] = {key: i for i, (key, _title) in enumerate(NAV_ITEMS)}

# Indices after which a thin vertical separator is drawn in the tab bar.
_SEPARATOR_AFTER = {1, 4, 7}


#: Text grows to this fraction of its normal size while a tab is hovered.
_HOVER_SCALE = 1.12
_HOVER_ANIM_MS = 150
#: How far (px) tab labels sit above their tab's middle.
_TAB_TEXT_LIFT = 4


class NavTabBar(QTabBar):
    """QTabBar subclass with hand-drawn tabs: hover-scaled text, the
    selected-tab underline, and thin vertical separators between tab groups.

    Text/underline/separator colors used to come entirely from the
    #navtabs/#navtabs::tab QSS rules via the normal super().paintEvent()
    path. Qt style sheets can't animate a property like font-size between
    states (a :hover rule only ever snaps instantly), so growing the text
    smoothly on hover means taking over painting instead. #navtabs::tab's
    background is already transparent and border none - the only two things
    actually drawn are the text color and the selected-tab underline, and
    both reduce to simple rules reproduced exactly below: accent_strong
    whenever a tab is hovered, or selected while not in Settings mode;
    text_nav otherwise, with the underline shown under that same
    selected-and-not-in-Settings-mode condition.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setMouseTracking(True)
        self._hovered_index = -1
        self._hover_scale: dict[int, float] = {}
        self._hover_anims: dict[int, QVariantAnimation] = {}

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        index = self.tabAt(event.pos())
        if index == self._hovered_index:
            return
        previous = self._hovered_index
        self._hovered_index = index
        if previous >= 0:
            self._animate_tab_scale(previous, 1.0)
        if index >= 0:
            self._animate_tab_scale(index, _HOVER_SCALE)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        if self._hovered_index >= 0:
            self._animate_tab_scale(self._hovered_index, 1.0)
            self._hovered_index = -1

    def _animate_tab_scale(self, index: int, target: float) -> None:
        anim = self._hover_anims.get(index)
        if anim is None:
            anim = QVariantAnimation(self)
            anim.valueChanged.connect(
                lambda value, i=index: self._on_scale_changed(i, value)
            )
            self._hover_anims[index] = anim
        anim.stop()
        anim.setStartValue(self._hover_scale.get(index, 1.0))
        anim.setEndValue(target)
        anim.setDuration(_HOVER_ANIM_MS)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start()

    def _on_scale_changed(self, index: int, value: float) -> None:
        self._hover_scale[index] = value
        self.update()

    def _scaled_font(self, scale: float) -> QFont:
        font = QFont(self.font())
        pixel_size = self.font().pixelSize()
        if pixel_size > 0:
            font.setPixelSize(max(1, round(pixel_size * scale)))
        else:
            font.setPointSizeF(max(1.0, self.font().pointSizeF() * scale))
        return font

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        tokens = active_theme_tokens()
        accent = QColor(tokens["accent_strong"])
        text_nav = QColor(tokens["text_nav"])
        settings_mode = bool(self.property("settingsMode"))
        current = self.currentIndex()

        for i in range(self.count()):
            rect = self.tabRect(i)
            is_selected = i == current
            is_hovered = i == self._hovered_index
            active = is_hovered or (is_selected and not settings_mode)

            painter.setFont(self._scaled_font(self._hover_scale.get(i, 1.0)))
            painter.setPen(accent if active else text_nav)
            # Text sits a little above the tab's middle: the underline takes
            # the bottom, and exactly centered labels read low in the bar.
            painter.drawText(
                rect.translated(0, -_TAB_TEXT_LIFT),
                Qt.AlignmentFlag.AlignCenter,
                self.tabText(i),
            )

            if is_selected and not settings_mode:
                pen = QPen(accent)
                pen.setWidth(2)
                painter.setPen(pen)
                painter.drawLine(
                    QPointF(rect.left(), rect.bottom() - 1),
                    QPointF(rect.right(), rect.bottom() - 1),
                )

        if self.count() < 2:
            return

        # Use the active theme accent so separators stay green in GAMMA,
        # teal in Midnight, amber in Dusk, and match the other theme accents.
        separator_color = QColor(tokens["accent_strong"])
        separator_color.setAlpha(150)
        pen = QPen(separator_color)
        pen.setWidth(1)
        painter.setPen(pen)

        for i in range(self.count()):
            if i in _SEPARATOR_AFTER and i < self.count() - 1:
                r = self.tabRect(i)
                x = r.right() + 4
                painter.drawLine(QPointF(x, r.top() + 5), QPointF(x, r.bottom() - 5))

        painter.end()


def paint_backdrop(painter: QPainter, rect) -> None:
    """Dark GAMMA-style backdrop with a soft, blurred radiation-green glow.

    Painted by the main window across its whole area - behind the central
    widget and the status bar alike - so the bottom bar sits on the same
    background as the pages instead of a separate dark strip.
    """
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    tokens = active_theme_tokens()

    def rgb(key: str) -> tuple[int, int, int]:
        parts = tokens[key].split(",")
        return int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())

    base = QLinearGradient(0, 0, rect.width(), rect.height())
    base.setColorAt(0.0, QColor(tokens["back_base_a"]))
    base.setColorAt(1.0, QColor(tokens["back_base_b"]))
    painter.fillRect(rect, base)

    radius = max(rect.width(), rect.height())
    r1 = rgb("back_glow1_rgb")
    glow = QRadialGradient(rect.width() * 0.18, rect.height() * 0.08, radius * 0.95)
    glow.setColorAt(0.0, QColor(r1[0], r1[1], r1[2], int(tokens["back_glow1_a"])))
    r1b = rgb("back_glow1b_rgb")
    glow.setColorAt(
        0.35, QColor(r1b[0], r1b[1], r1b[2], int(tokens["back_glow1b_a"]))
    )
    r1c = rgb("back_glow1c_rgb")
    glow.setColorAt(
        0.7, QColor(r1c[0], r1c[1], r1c[2], int(tokens["back_glow1c_a"]))
    )
    glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    painter.fillRect(rect, glow)

    r2 = rgb("back_glow2_rgb")
    glow2 = QRadialGradient(rect.width() * 0.95, rect.height() * 0.96, radius * 0.7)
    glow2.setColorAt(0.0, QColor(r2[0], r2[1], r2[2], int(tokens["back_glow2_a"])))
    glow2.setColorAt(1.0, QColor(0, 0, 0, 0))
    painter.fillRect(rect, glow2)


class Backdrop(QWidget):
    """The central widget. Transparent: the main window paints the backdrop
    behind it (see paint_backdrop()); page roots stay semi-transparent and
    let the glow bleed through behind the cards."""


class _CommanderUpdateProgressBridge(QObject):
    """Cross-thread signal bridge for the COMMANDER self-update download.

    download_and_install_commander_update()'s progress_cb runs on the
    BackgroundTask's worker thread; PySide only auto-queues a cross-thread
    signal onto the main thread for a QObject-bound slot, so this exists for
    the same reason play_page.py's own _ProgressBridge does - a lambda would
    run on the worker thread instead, touching the QProgressDialog unsafely.
    """

    updated = Signal(int, str)


class MainWindow(QMainWindow):
    #: A newer COMMANDER release tag, from the one shared update check.
    commander_update_found = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        gui_state = gui_settings.load_gui_settings()
        self.resize(gui_state["window_width"], gui_state["window_height"])
        self.settings = load_settings()
        self.custom_title_bar = False
        self._drag_filter = WindowDragFilter(self)
        self._resize_filter = EdgeResizeFilter(self)
        self.install_busy = False
        self.install_operation: str | None = None
        self._settings_open = False
        self._last_tab_key = "dashboard"
        self._nav_refresh_serial = 0

        # Built before _build_ui(): constructing pages there (e.g. the
        # Dashboard) can trigger refresh_settings() -> _refresh_status_bar()
        # immediately, which needs these widgets to already exist.
        self._build_status_bar()
        self._build_ui()
        self._build_shortcuts()
        self.apply_title_bar(bool(gui_state.get("custom_title_bar", True)))
        # Mouse-wheel scrolling glides instead of jumping, in every page,
        # list and text box of this window.
        from .smooth_scroll import install as install_smooth_scroll

        self._smooth_scroll = install_smooth_scroll(self)
        self.tabs.setCurrentIndex(0)
        QTimer.singleShot(0, self._maybe_check_for_updates_in_background)
        QTimer.singleShot(0, self._check_commander_update_status)
        self._start_update_timer()
        # The Welcome screen (latest patch notes, links), unless "Don't show
        # again" was ticked - here or in Deck Mode, which shares the setting.
        QTimer.singleShot(0, self._maybe_show_welcome)

        start_page = gui_settings.load_gui_settings().get("start_page")
        if start_page and start_page in _TAB_INDEX:
            self.tabs.setCurrentIndex(_TAB_INDEX[start_page])
        # tabs.setCurrentIndex() above is a no-op (fires no currentChanged,
        # so _on_nav() never runs) when the target is already the tab bar's
        # default current index (0, Dashboard) - called explicitly here so
        # the initial page is always built, lazy pages notwithstanding.
        self._on_nav(self.tabs.currentIndex())

    def _maybe_show_welcome(self) -> None:
        from .welcome_overlay import maybe_show_welcome

        self._welcome = maybe_show_welcome(self)

    def setWindowTitle(self, title: str) -> None:
        """Mark this window when it is not the first COMMANDER running.

        Overridden rather than applied at each call site because the title is
        set from several places (the nav switch composes "Install - ..."),
        and a marker that only some of them carry would be worse than none.
        """
        super().setWindowTitle(instance_window_title(title))

    def _build_shortcuts(self) -> None:
        """Set up app-wide keyboard shortcuts - called once from __init__.

        Deliberately NOT called from _build_ui(): that method reruns on
        every switch_language() and would otherwise stack a duplicate
        QShortcut (each one firing its action again) on every language
        change, the same trap _build_status_bar()'s own docstring
        documents for status bar widgets.
        """
        focus_search = QShortcut(QKeySequence("Ctrl+F"), self)
        focus_search.activated.connect(self._focus_mod_search)
        open_settings_shortcut = QShortcut(QKeySequence("Ctrl+,"), self)
        open_settings_shortcut.activated.connect(self.open_settings)

    def _focus_mod_search(self) -> None:
        """Jump to Mod Manager and focus its search box.

        Looks up the current page fresh rather than capturing it at
        shortcut-creation time - _build_ui() replaces every page instance
        on each switch_language(), so a captured reference would go stale.
        """
        self.set_page("modmanager")
        page = self._pages.get("modmanager")
        if page is not None and hasattr(page, "search"):
            page.search.setFocus()
            page.search.selectAll()

    #: How often the background update check re-runs on its own, without
    #: the user ever visiting the Dashboard/Updates page.
    _UPDATE_CHECK_INTERVAL_S = 86400

    def _maybe_check_for_updates_in_background(self) -> None:
        """Once a day at most, check for a GAMMA update without being asked.

        The Dashboard/Updates pages already check on demand when visited -
        this covers the user who never opens either, surfacing a desktop
        notification (see notify_desktop()) instead of a badge that would
        need its own always-on UI plumbing.
        """
        gui_state = gui_settings.load_gui_settings()
        last_check = gui_state.get("last_update_check_ts", 0.0)
        try:
            last_check = float(last_check)
        except (TypeError, ValueError):
            last_check = 0.0
        if time.time() - last_check < self._UPDATE_CHECK_INTERVAL_S:
            return
        profile = self.settings.active_profile
        if profile is None:
            return
        task = BackgroundTask(check_updates, profile, parent=self)
        self._scheduled_update_task = task
        task.result.connect(self._on_scheduled_update_checked)
        task.start()

    def _on_scheduled_update_checked(self, status: object) -> None:
        gui_settings.save_gui_settings(last_update_check_ts=time.time())
        if not getattr(status, "update_available", False):
            return
        # Once per GAMMA version: the daily check used to repeat the same
        # notification every day until the update was installed.
        latest = str(getattr(status, "latest", "") or "")
        state = gui_settings.load_gui_settings()
        if not state.get("update_notifications", True):
            return
        if latest and state.get("notified_gamma_version") == latest:
            return
        gui_settings.save_gui_settings(notified_gamma_version=latest)
        notify_desktop(
            tr("GAMMA update available"),
            tr(
                "A new GAMMA update is available - open COMMANDER's Updates page to review it."
            ),
        )

    #: How often an open COMMANDER looks for updates again. Each check keeps
    #: its own interval (GAMMA daily, COMMANDER below); this just wakes up.
    _UPDATE_TIMER_MS = 60 * 60 * 1000
    _COMMANDER_CHECK_INTERVAL_S = 12 * 60 * 60

    def _start_update_timer(self) -> None:
        """Keep checking while COMMANDER stays open - both checks used to
        run only at startup, so an app left open for days never heard of
        an update."""
        if getattr(self, "_update_timer", None) is not None:
            return
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(self._UPDATE_TIMER_MS)
        self._update_timer.timeout.connect(self._periodic_update_check)
        self._update_timer.start()

    def _periodic_update_check(self) -> None:
        if self.install_busy:
            return
        self._maybe_check_for_updates_in_background()
        last = getattr(self, "_commander_checked_at", 0.0)
        if time.monotonic() - last >= self._COMMANDER_CHECK_INTERVAL_S:
            self._check_commander_update_status()

    def _notify_commander_update(self, tag: str) -> None:
        """Desktop notification for a new COMMANDER release, once per tag."""
        state = gui_settings.load_gui_settings()
        if not state.get("update_notifications", True):
            return
        if state.get("notified_commander_tag") == tag:
            return
        gui_settings.save_gui_settings(notified_commander_tag=tag)
        notify_desktop(
            tr("COMMANDER update available"),
            tr(
                "COMMANDER {tag} is out - click the update button in COMMANDER's status bar to get it.",
                tag=tag,
            ),
        )

    def _build_ui(self) -> None:
        """(Re)build the header, nav tabs, and every page from scratch.

        Called once from ``__init__``, and again from ``switch_language()``
        so a language change takes effect immediately instead of requiring
        a restart - every string here was set once at widget-construction
        time from ``tr()``, so the only way to re-translate it is to
        reconstruct the widgets that hold it.
        """
        self.setWindowTitle(tr("STALKER COMMANDER"))

        central = Backdrop(self)
        self.backdrop = central
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Top area: wordmark, tabs, mod counter and cog, all centered in its
        # full height. With COMMANDER's own title bar the bar grows by the
        # window-button strip's height and the strip floats in its top-right
        # corner, above the counter and cog.
        topbar = QWidget()
        topbar.setObjectName("topbar")
        topbar_layout = QHBoxLayout(topbar)
        topbar_layout.setContentsMargins(16, 0, 0, 0)
        topbar_layout.setSpacing(16)

        header = QWidget()
        header.setObjectName("topbarRow")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 8, 0)
        header_layout.setSpacing(16)

        wordmark_block = QWidget()
        wordmark_block.setObjectName("wordmarkBlock")
        wordmark_layout = QVBoxLayout(wordmark_block)
        wordmark_layout.setContentsMargins(0, 0, 0, 0)
        wordmark_layout.setSpacing(0)

        wordmark = QLabel(tr("COMMANDER"))
        wordmark.setObjectName("wordmark")
        wordmark_layout.addWidget(wordmark)

        byline = QLabel(tr("by SSH-Kitty"))
        byline.setObjectName("byline")
        byline.setAlignment(Qt.AlignmentFlag.AlignRight)
        wordmark_layout.addWidget(byline)

        topbar_layout.addWidget(wordmark_block, 0, Qt.AlignmentFlag.AlignVCenter)
        topbar_layout.addWidget(header, 1, Qt.AlignmentFlag.AlignVCenter)
        self._topbar = topbar
        self._topbar_row = header
        # Floats in the top-right corner (pinned there by pin_top_right()) instead
        # of taking a row, so everything else stays centered in the bar.
        pin_top_right(self._build_title_strip(), topbar)

        header_layout.addStretch(1)

        self.tabs = NavTabBar()
        self.tabs.setObjectName("navtabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setExpanding(False)
        header_layout.addWidget(self.tabs)

        header_layout.addStretch(1)

        # Counter and cog share one box, so both move the window when dragged.
        self._right_cluster = QWidget()
        self._right_cluster.setObjectName("panelTransparent")
        right_layout = QHBoxLayout(self._right_cluster)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(16)
        header_layout.addWidget(self._right_cluster)

        self.mod_counter_label = QLabel()
        self.mod_counter_label.setObjectName("modCounter")
        right_layout.addWidget(self.mod_counter_label)

        self._cog = QPushButton(tr("⚙"))
        self._cog.setObjectName("cogButton")
        self._cog.setToolTip(tr("Settings"))
        self._cog.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cog.setFixedHeight(28)
        self._cog.clicked.connect(self.toggle_settings)
        right_layout.addWidget(self._cog)
        self.update_mod_counter()
        # Keeps the topbar counter live on its own (e.g. while the user
        # watches an install/repair finish, or after Mod Manager/MO2
        # itself changes modlist.txt) instead of only updating it at the
        # specific call sites that remember to - cheap (a single small
        # file read/parse), so a few-second cadence is not wasteful.
        # Stopped and dropped first: _build_ui() reruns on every
        # switch_language(), and this timer is parented to the long-lived
        # window rather than to the central widget that rebuild tears down -
        # so nothing else would ever stop the previous one. Left running,
        # every language switch would add another live timer firing
        # update_mod_counter() on the same 5s cadence, forever.
        previous_timer = getattr(self, "_mod_counter_timer", None)
        if previous_timer is not None:
            previous_timer.stop()
            previous_timer.deleteLater()
        self._mod_counter_timer = QTimer(self)
        self._mod_counter_timer.setInterval(5000)
        self._mod_counter_timer.timeout.connect(self.update_mod_counter)
        self._mod_counter_timer.start()

        # Pages are built lazily (see _ensure_page()), not here: constructing
        # every page eagerly - Mod Manager, Install, etc. included - even
        # when a session never visits most of them was the single biggest
        # contributor to this app's idle memory footprint.
        self._pages: dict[str, QWidget] = {}
        self.stack = QStackedWidget()

        for _key, title in NAV_ITEMS:
            # Translated here, not by wrapping NAV_ITEMS itself: NAV_ITEMS is
            # a module-level constant evaluated at import time, before
            # main.py ever calls set_active_language() - tr() would always
            # resolve to English if baked in there instead of at display time.
            self.tabs.addTab(tr(title))

        # A quick fade whenever the visible page changes. Hooking
        # currentChanged (emitted for every setCurrentIndex() call
        # regardless of caller) covers nav-tab switches and opening/closing
        # Settings alike, without needing to touch each call site.
        # Page content is deliberately semi-transparent (Backdrop's glow is
        # meant to bleed through, per its own docstring) - dropping this
        # effect's opacity all the way to 0 doesn't just fade the page, it
        # also fades away that dimming layer itself, briefly exposing the
        # raw, undimmed backdrop glow underneath (most visible as a flash
        # in its brightest spot, the top-left glow). Keeping the floor high
        # (0.9) keeps a perceptible fade without ever un-dimming the glow
        # enough for that to be noticeable.
        self._stack_opacity = QGraphicsOpacityEffect(self.stack)
        # QGraphicsOpacityEffect's own default opacity is 0.7, not 1.0 -
        # left unset, the very first page (Dashboard) renders dim until
        # the first real page switch runs the fade below and leaves it
        # at 1.0 for good.
        self._stack_opacity.setOpacity(1.0)
        self.stack.setGraphicsEffect(self._stack_opacity)
        # Parented to the opacity effect (not self/MainWindow): this whole
        # fade setup is rebuilt fresh on every switch_language() call, and
        # parenting the animation to the long-lived MainWindow instead of
        # something torn down with the old stack would leak one orphaned
        # animation object per language switch for the life of the app.
        self._stack_fade = QPropertyAnimation(
            self._stack_opacity, b"opacity", self._stack_opacity
        )
        self._stack_fade.setDuration(400)
        self._stack_fade.setStartValue(0.9)
        self._stack_fade.setEndValue(1.0)
        self._stack_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.stack.currentChanged.connect(self._on_stack_page_changed)

        self.tabs.currentChanged.connect(self._on_nav)
        self.tabs.tabBarClicked.connect(self._on_tab_clicked)
        layout.addWidget(topbar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        for widget in (
            topbar, header, wordmark_block, wordmark, byline,
            self._right_cluster, self.mod_counter_label,
        ):
            widget.installEventFilter(self._drag_filter)
        self._sync_title_bar_layout()

        # Keep the full navigation strip visible at startup. The saved window
        # size may predate the current tab labels and otherwise enables the
        # QTabBar scroll arrows.
        self.tabs.setMinimumWidth(self.tabs.sizeHint().width())
        header_width = header.sizeHint().width()
        self.setMinimumWidth(header_width)
        if self.width() < header_width:
            self.resize(header_width, self.height())

    def _build_status_bar(self) -> None:
        """Build the persistent status bar contents exactly once.

        Must NOT be called from ``_build_ui()``: that method also runs on
        every ``switch_language()`` rebuild, and ``QStatusBar.addPermanentWidget``
        is not idempotent - nothing removes a previously-added widget, so
        calling this from there would stack a duplicate GitHub button (and
        duplicate Language/Theme combos) on every language switch. Text and
        combo selections are kept current afterward via ``_refresh_status_bar()``
        instead of rebuilding any of this.
        """
        # Kept compact and font-size-independent from the rest of the app
        # (unlike the themed QComboBox elsewhere, which is deliberately
        # roomier for normal clicking) - this bar was a single thin row of
        # plain text before the Language/Theme combos existed, and the
        # combo boxes' usual padding/min-height from the shared stylesheet
        # would otherwise make the whole bar noticeably taller.
        # border/background use the dynamic palette() QSS functions rather
        # than a hardcoded color so the boxed look stays correct across
        # every theme without needing theme tokens imported here.
        _STATUS_LABEL_STYLE = "font-size: 12px;"
        _STATUS_COMBO_STYLE = (
            "QComboBox {"
            "  font-size: 12px;"
            "  padding: 1px 6px;"
            "  border: 1px solid palette(mid);"
            "  border-radius: 3px;"
            "  background: palette(button);"
            "}"
            "QComboBox::drop-down { border: none; width: 16px; }"
        )

        self._status_info_label = QLabel()
        self._status_info_label.setStyleSheet(_STATUS_LABEL_STYLE)
        self.statusBar().addWidget(self._status_info_label)

        self._status_language_combo = NoWheelComboBox()
        self._status_language_combo.setStyleSheet(_STATUS_COMBO_STYLE)
        self._status_language_combo.setFixedHeight(21)
        for code, native, _english in LANGUAGE_INFO:
            self._status_language_combo.addItem(native, code)
        self._status_language_combo.currentIndexChanged.connect(
            self._on_status_language
        )
        self.statusBar().addWidget(self._status_language_combo)

        self._status_theme_label = QLabel()
        self._status_theme_label.setStyleSheet(_STATUS_LABEL_STYLE)
        self.statusBar().addWidget(self._status_theme_label)

        self._status_theme_combo = NoWheelComboBox()
        self._status_theme_combo.setStyleSheet(_STATUS_COMBO_STYLE)
        self._status_theme_combo.setFixedHeight(21)
        for key, label, _description, _swatches in THEME_INFO:
            self._status_theme_combo.addItem(label, key)
        self._status_theme_combo.currentIndexChanged.connect(self._on_status_theme)
        self.statusBar().addWidget(self._status_theme_combo)

        self._status_font_label = QLabel()
        self._status_font_label.setStyleSheet(_STATUS_LABEL_STYLE)
        self.statusBar().addWidget(self._status_font_label)

        self._status_font_combo = NoWheelComboBox()
        self._status_font_combo.setStyleSheet(_STATUS_COMBO_STYLE)
        self._status_font_combo.setFixedHeight(21)
        for family in (
            "Exo 2",
            "Noto Sans",
            "DejaVu Sans",
            "Liberation Sans",
            "Inter",
        ):
            self._status_font_combo.addItem(family, family)
        self._status_font_combo.currentIndexChanged.connect(
            self._on_status_font_family
        )
        self.statusBar().addWidget(self._status_font_combo)

        # No label before this one - a small numeric box right after the
        # Font (family) box, matching the Settings page's own "Interface
        # font size" combo (same 9-22 range) without a separate header.
        self._status_fontsize_combo = NoWheelComboBox()
        self._status_fontsize_combo.setStyleSheet(_STATUS_COMBO_STYLE)
        self._status_fontsize_combo.setFixedHeight(21)
        for size in range(9, 23):
            self._status_fontsize_combo.addItem(str(size), size)
        self._status_fontsize_combo.currentIndexChanged.connect(
            self._on_status_font_size
        )
        self.statusBar().addWidget(self._status_fontsize_combo)

        self._refresh_status_bar()

        # COMMANDER app-update status - separate from the GAMMA-modpack
        # update check (_maybe_check_for_updates_in_background) - a
        # flat, non-interactive label while checking/up to date, or a
        # clickable one (opens the Releases page) once an update is
        # confirmed available. Actually checked once, from __init__, via
        # _check_commander_update_status().
        #
        # All three pieces (status, separator, GitHub link) are built
        # into one container with its own QHBoxLayout and added as a
        # SINGLE addPermanentWidget() call - relying on the relative
        # order of multiple separate addPermanentWidget() calls proved
        # unreliable in practice (worth remembering: don't guess at that
        # ordering again), whereas a layout's own child order is always
        # exactly what it's given, left to right.
        update_area = QWidget()
        # Transparent like the rest of the bar - a plain QWidget would take
        # the stylesheet's page background and show as a dark box.
        update_area.setObjectName("panelTransparent")
        update_layout = QHBoxLayout(update_area)
        update_layout.setContentsMargins(0, 0, 0, 0)
        update_layout.setSpacing(0)

        self._update_status_button = QPushButton(tr("Checking for updates..."))
        self._update_status_button.setObjectName("commanderUpdateStatus")
        self._update_status_button.setFlat(True)
        self._update_status_button.setEnabled(False)
        self._update_status_button.setStyleSheet(
            f"color: {STATUS_GREY.name()}; border: none;"
        )
        update_layout.addWidget(self._update_status_button)

        # Same plain text-pipe separator style already used between the
        # Language/Theme/Font controls above ("   |   " prefix), not a
        # QFrame line - kept visually consistent with the rest of this
        # status bar.
        update_separator = QLabel("   |   ")
        update_separator.setStyleSheet(_STATUS_LABEL_STYLE)
        update_layout.addWidget(update_separator)

        # COMMANDER is up to date | Discord | GitHub - Discord styled
        # exactly like the GitHub link beside it.
        discord_link = QPushButton(tr("Discord"))
        discord_link.setObjectName("discordLink")
        discord_link.setToolTip(tr("Join the COMMANDER Discord"))
        discord_link.setFlat(True)
        discord_link.setCursor(Qt.CursorShape.PointingHandCursor)
        discord_link.clicked.connect(lambda: open_url(_DISCORD))
        update_layout.addWidget(discord_link)
        discord_separator = QLabel("   |   ")
        discord_separator.setStyleSheet(_STATUS_LABEL_STYLE)
        update_layout.addWidget(discord_separator)

        github_link = QPushButton(tr("GitHub"))
        github_link.setObjectName("githubLink")
        github_link.setToolTip(tr("Open SSH-Kitty on GitHub"))
        github_link.setFlat(True)
        github_link.setCursor(Qt.CursorShape.PointingHandCursor)
        github_link.clicked.connect(
            lambda: open_url("https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER")
        )
        update_layout.addWidget(github_link)

        self.statusBar().setSizeGripEnabled(False)
        # No bar of its own any more (transparent, see themes.py), so the
        # text gets the same breathing room from the window edges as pages.
        self.statusBar().setContentsMargins(12, 0, 12, 4)
        self.statusBar().addPermanentWidget(update_area)

    def _check_commander_update_status(self) -> None:
        """Best-effort, non-blocking check for a newer COMMANDER release.

        Runs once, at startup - see the status bar's "Checking for
        updates..." → "Up to date"/"Update available" label built in
        _build_status_bar(). Just a status/link, not an auto-updater.
        """
        # The channel is read here, on the GUI thread; the worker only fetches.
        channel = effective_update_channel(
            gui_settings.load_gui_settings().get("update_channel"), __version__
        )
        self._commander_checked_at = time.monotonic()
        task = BackgroundTask(
            check_commander_update, __version__, channel=channel, parent=self
        )
        task.result.connect(self._on_commander_update_status_checked)
        task.start()

    def _on_commander_update_status_checked(self, tag: object) -> None:
        button = self._update_status_button
        # The check can run again (switching the update channel in
        # Settings); drop the previous result's click handler first, or one
        # click would open the dialog once per check.
        previous = getattr(self, "_update_status_handler", None)
        if previous is not None:
            try:
                button.clicked.disconnect(previous)
            except (RuntimeError, TypeError):
                pass
            self._update_status_handler = None
        if tag and isinstance(tag, str):
            self._commander_update_tag = tag
            self.commander_update_found.emit(tag)
            self._notify_commander_update(tag)
            button.setText(tr("COMMANDER update available"))
            button.setStyleSheet(f"color: {WARN.name()}; border: none;")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setEnabled(True)
            if commander_appimage_path() is not None:
                # Running as an AppImage - offer to download and swap it in
                # place instead of just sending the user to the browser. A
                # source checkout or the AUR package (pacman-tracked, see
                # packaging/aur/PKGBUILD) never sets APPIMAGE, so they fall
                # through to the plain "open the releases page" behavior.
                button.setToolTip(tr("Download and install {tag} now", tag=tag))
                self._update_status_handler = lambda: self._offer_commander_self_update(tag)
                button.clicked.connect(self._update_status_handler)
            else:
                button.setToolTip(tr("Open the Releases page for {tag}", tag=tag))
                self._update_status_handler = lambda: open_url(
                    "https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases"
                )
                button.clicked.connect(self._update_status_handler)
        else:
            button.setText(tr("COMMANDER is up to date"))
            button.setStyleSheet(f"color: {OK_GREEN.name()}; border: none;")
            button.setEnabled(False)

    def _offer_commander_self_update(self, tag: str) -> None:
        """Confirm, then download and swap in the new AppImage, then restart."""
        answer = QMessageBox.question(
            self,
            tr("Update COMMANDER"),
            tr(
                "Download and install COMMANDER {tag} now? "
                "The app will restart when it's done.",
                tag=tag,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run_commander_download(
            tr("Downloading COMMANDER {tag}...", tag=tag),
            lambda cancel, progress: download_and_install_commander_update(
                tag, cancel_event=cancel, progress_cb=progress
            ),
        )

    def switch_commander_build(self, target: str) -> None:
        """Settings' "Switch to Unstable" / "Revert to Stable".

        Installs the newest build of ``target`` ("unstable" or "stable") over
        this AppImage and restarts - going back a version when reverting.
        """
        if target == "unstable":
            title = tr("Try the unstable build")
            question = tr(
                "Download and install the latest unstable COMMANDER build? "
                "Unstable builds get new features sooner but are less tested.\n\n"
                "COMMANDER restarts when it's done, and you can revert to the "
                "stable build from Settings at any time."
            )
        else:
            title = tr("Go back to the stable build")
            question = tr(
                "Download and install the latest stable COMMANDER build?\n\n"
                "It may be older than the unstable build you're running now. "
                "Settings changed by the newer build may not all carry back.\n\n"
                "COMMANDER restarts when it's done."
            )
        answer = QMessageBox.question(
            self,
            title,
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def work(cancel, progress):
            path, _tag = switch_commander_build(
                target, cancel_event=cancel, progress_cb=progress
            )
            return path

        def switched() -> None:
            # The update channel follows the build that is now installed.
            gui_settings.save_gui_settings(update_channel=target)

        self._run_commander_download(
            tr("Downloading the unstable build...")
            if target == "unstable"
            else tr("Downloading the stable build..."),
            work,
            on_installed=switched,
        )

    def _run_commander_download(self, label: str, work, on_installed=None) -> None:
        """Run ``work(cancel_event, progress_cb) -> AppImage path`` with a
        progress dialog, then restart into the installed AppImage.

        Shared by the status bar's update and Settings' build switch; the
        download itself (size, ELF/AppImage and checksum checks, atomic
        swap) is ``self_update``'s.
        """
        # The restart at the end quits through closeEvent, which asks about a
        # running install - answering "No" there left two instances running.
        if self.install_busy:
            QMessageBox.warning(
                self,
                tr("Busy"),
                tr(
                    "An install, update or dependency download is still running. "
                    "Wait for it to finish before updating COMMANDER."
                ),
            )
            return
        progress = QProgressDialog(label, tr("Cancel"), 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)

        bridge = _CommanderUpdateProgressBridge(parent=self)
        bridge.updated.connect(progress.setValue)
        bridge.updated.connect(
            lambda _pct, text: progress.setLabelText(text)
        )

        def _progress(downloaded: int, total: int) -> None:
            pct = int(downloaded * 100 / total) if total > 0 else 0
            text = label + " " + tr(
                "{done}/{total}", done=human_size(downloaded), total=human_size(total)
            )
            bridge.updated.emit(pct, text)

        task = BackgroundTask(lambda: work(task.cancel_event, _progress), parent=self)
        progress.canceled.connect(task.cancel_event.set)

        def on_result(path: object) -> None:
            progress.close()
            if on_installed is not None:
                on_installed()
            from ..main import release_instance_lock

            # release_instance_lock must run in this (still-running)
            # process before the new one starts - see relaunch_commander()'s
            # own docstring for why a lock held past this point makes the
            # new COMMANDER exit immediately with no window.
            relaunch_commander(path, release_lock=release_instance_lock)
            QApplication.quit()

        def on_error(message: str) -> None:
            progress.close()
            self._show_commander_self_update_error(message)

        task.result.connect(on_result)
        task.error.connect(on_error)
        task.start()
        progress.show()

    def _show_commander_self_update_error(self, message: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("Update failed"))
        box.setText(message)
        box.addButton(QMessageBox.StandardButton.Close)
        open_releases = box.addButton(
            tr("Open Releases Page"), QMessageBox.ButtonRole.ActionRole
        )
        box.exec()
        if box.clickedButton() == open_releases:
            open_url("https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases")

    def _refresh_status_bar(self) -> None:
        """Re-render status bar text and re-select the combos' current items.

        Called after anything that could change the active profile, language
        or theme. The status bar is built once and never torn down (unlike
        page content, which switch_language() rebuilds from scratch), so its
        tr()-wrapped text needs this explicit refresh to pick up a language
        change instead of getting it "for free" via reconstruction.
        """
        self._status_info_label.setText(
            f"COMMANDER {__version_label__}   |   "
            + tr("Active profile: {name}", name=self._active_name())
            + f"   |   {tr('Language:')}"
        )
        self._status_theme_label.setText(f"   |   {tr('Theme:')}")
        lang_index = self._status_language_combo.findData(active_language())
        self._status_language_combo.blockSignals(True)
        self._status_language_combo.setCurrentIndex(max(lang_index, 0))
        self._status_language_combo.blockSignals(False)
        theme_index = self._status_theme_combo.findData(active_theme())
        self._status_theme_combo.blockSignals(True)
        self._status_theme_combo.setCurrentIndex(max(theme_index, 0))
        self._status_theme_combo.blockSignals(False)
        self._status_font_label.setText(f"   |   {tr('Font:')}")
        font_family = gui_settings.load_gui_settings().get("font_family") or "Exo 2"
        font_index = self._status_font_combo.findData(font_family)
        self._status_font_combo.blockSignals(True)
        self._status_font_combo.setCurrentIndex(max(font_index, 0))
        self._status_font_combo.blockSignals(False)
        font_size = int(gui_settings.load_gui_settings().get("font_size") or 13)
        size_index = self._status_fontsize_combo.findData(font_size)
        self._status_fontsize_combo.blockSignals(True)
        self._status_fontsize_combo.setCurrentIndex(max(size_index, 0))
        self._status_fontsize_combo.blockSignals(False)

    def _on_status_language(self, *_args) -> None:
        code = self._status_language_combo.currentData()
        if code:
            self.apply_language(code)

    def _on_status_theme(self, *_args) -> None:
        key = self._status_theme_combo.currentData()
        if key:
            self.apply_theme(key)

    def _on_status_font_family(self, *_args) -> None:
        family = self._status_font_combo.currentData()
        if family:
            self.apply_font_family(family)

    def _on_status_font_size(self, *_args) -> None:
        size = self._status_fontsize_combo.currentData()
        if size is not None:
            self.apply_font_size(size)

    #: Page class for each nav key. A page opts into extra dispatch behavior
    #: (see ``_schedule_page_refresh``) by defining the matching method, not
    #: by main_window.py special-casing its key.
    _PAGE_CLASSES: ClassVar[dict[str, type[QWidget]]] = {
        "play": PlayPage,
        "dashboard": DashboardPage,
        "install": InstallPage,
        "systemcheck": SystemCheckPage,
        "update": UpdatePage,
        "modmanager": ModManagerPage,
        "profiles": ProfilesPage,
        "utilities": UtilitiesPage,
        "help": HelpPage,
        "about": AboutPage,
        "settings": SettingsPage,
    }

    def _create_page(self, key: str) -> QWidget:
        try:
            page_class = self._PAGE_CLASSES[key]
        except KeyError:
            raise ValueError(key) from None
        return page_class(self)

    def _ensure_page(self, key: str) -> QWidget:
        """Return the page for ``key``, building and caching it on first call.

        Building on first visit (instead of every page upfront in
        _build_ui()) is what keeps idle memory down - a page built here
        stays cached in self._pages/self.stack for the rest of the session,
        same as the old eager pages did, so there is no cost to calling this
        repeatedly.
        """
        page = self._pages.get(key)
        if page is not None:
            return page
        page = self._create_page(key)
        self._pages[key] = page
        self.stack.addWidget(page)
        # A page built after an install/task already started must reflect
        # that immediately - set_install_busy() only notifies pages that
        # exist at the moment it's called (see its own loop below).
        on_busy_changed = getattr(page, "on_busy_changed", None)
        if callable(on_busy_changed):
            on_busy_changed(self.install_busy)
        on_install_activity_changed = getattr(page, "on_install_activity_changed", None)
        if callable(on_install_activity_changed):
            on_install_activity_changed(self.install_operation)
        return page

    def _active_name(self) -> str:
        profile = self.settings.active_profile
        return profile.profile_name if profile else "(none)"

    def _on_stack_page_changed(self, _index: int) -> None:
        self._stack_fade.stop()
        self._stack_fade.start()

    def _on_nav(self, index: int) -> None:
        if not (0 <= index < len(NAV_ITEMS)):
            return
        self._settings_open = False
        self._set_cog_active(False)
        self.tabs.setProperty("settingsMode", False)
        self.tabs.style().unpolish(self.tabs)
        self.tabs.style().polish(self.tabs)
        key = NAV_ITEMS[index][0]
        self.setWindowTitle(
            "Install - STALKER COMMANDER"
            if key == "install"
            else "STALKER COMMANDER"
        )
        page = self._ensure_page(key)
        self.stack.setCurrentWidget(page)
        # Immediate, not deferred to _schedule_page_refresh: a page switch
        # should reflect the current mod count right away, independent of
        # whether that particular page's own refresh() happens to call it.
        self.update_mod_counter()
        self._schedule_page_refresh(key)

    def _schedule_page_refresh(self, key: str) -> None:
        """Refresh after Qt has painted the newly selected page."""
        self._nav_refresh_serial += 1
        serial = self._nav_refresh_serial

        def refresh() -> None:
            if serial != self._nav_refresh_serial or self._settings_open:
                return
            current = self.tabs.currentIndex()
            if current >= len(NAV_ITEMS) or NAV_ITEMS[current][0] != key:
                return
            page = self._pages[key]
            if hasattr(page, "refresh"):
                page.refresh()
            if hasattr(page, "enable_winetricks_status"):
                page.enable_winetricks_status()

        QTimer.singleShot(0, refresh)

    def _on_tab_clicked(self, index: int) -> None:
        """Leave Settings when a navigation tab is clicked, including itself."""
        if not self._settings_open or not (0 <= index < len(NAV_ITEMS)):
            return
        current = self.tabs.currentIndex()
        self.close_settings()
        if current == index:
            # QTabBar does not emit currentChanged when the selected tab is
            # clicked again, so drive the normal navigation path explicitly.
            self._on_nav(index)
        else:
            self.tabs.setCurrentIndex(index)

    def set_page(self, key: str) -> None:
        if key == "settings":
            self.open_settings()
            return
        if key not in _TAB_INDEX:
            return
        self.tabs.setCurrentIndex(_TAB_INDEX[key])

    def _set_cog_active(self, active: bool) -> None:
        self._cog.setProperty("active", active)
        self._cog.style().unpolish(self._cog)
        self._cog.style().polish(self._cog)

    def open_settings(self) -> None:
        if self._settings_open:
            return
        self._settings_open = True
        self._last_tab_key = NAV_ITEMS[self.tabs.currentIndex()][0]
        self.setWindowTitle(tr("Settings - STALKER COMMANDER"))
        self.tabs.setProperty("settingsMode", True)
        self.tabs.style().unpolish(self.tabs)
        self.tabs.style().polish(self.tabs)
        self._set_cog_active(True)
        page = self._ensure_page("settings")
        self.stack.setCurrentWidget(page)
        QTimer.singleShot(0, page.refresh)

    def close_settings(self) -> None:
        if not self._settings_open:
            return
        self._settings_open = False
        self._set_cog_active(False)
        self.tabs.setProperty("settingsMode", False)
        self.tabs.style().unpolish(self.tabs)
        self.tabs.style().polish(self.tabs)
        page = self._ensure_page(self._last_tab_key)
        self.stack.setCurrentWidget(page)
        self.tabs.setCurrentIndex(_TAB_INDEX[self._last_tab_key])

    def toggle_settings(self) -> None:
        if self._settings_open:
            self.close_settings()
        else:
            self.open_settings()

    def _reject_language_or_theme_change(self, busy_message: str) -> bool:
        """Show why a language/theme change is refused and revert both pickers.

        Returns True if the change was refused (caller should stop), False
        if it's safe to proceed. Shared by apply_theme() and apply_language()
        since both are blocked by the same two conditions: a background task
        (install_busy) or the game/Mod Organizer currently running (same
        mo2_running() check the Launch Game button itself uses) - changing
        either while the game is running is refused even though nothing
        about apply_theme() itself is unsafe then, to keep the two pickers'
        behavior consistent and predictable for the user.
        """
        if self.install_busy:
            QMessageBox.warning(self, tr("Busy"), busy_message)
        elif mo2_running():
            QMessageBox.warning(
                self,
                tr("Busy"),
                tr(
                    "Mod Organizer / the game is currently running.\n\nClose it before running this action."
                ),
            )
        else:
            return False
        settings_page = self._pages.get("settings")
        if settings_page is not None and hasattr(settings_page, "refresh"):
            # Revert both pickers to the still-active value - each already
            # shows the rejected selection from the signal that called us.
            settings_page.refresh()
        self._refresh_status_bar()
        return True

    def apply_theme(self, name: str) -> None:
        if self._reject_language_or_theme_change(
            tr(
                "Cannot change the theme while a background task is running. Wait for it to finish, then try again."
            )
        ):
            return
        gui_settings.save_gui_settings(theme=name)
        self._apply_style()

    def apply_font_size(self, size: int) -> None:
        gui_settings.save_gui_settings(font_size=int(size))
        self._apply_style()

    def apply_font_family(self, family: str) -> None:
        gui_settings.save_gui_settings(font_family=family)
        self._apply_style()

    def apply_language(self, code: str) -> None:
        """Switch the UI language immediately, rebuilding every page.

        Refused while ``install_busy`` (an install, update, verify, or
        similar task is running): every page - and the background thread
        driving that task, which the page's own widgets hold a reference
        to - would be torn down and rebuilt, terminating or orphaning it.
        Also refused while the game/Mod Organizer is running, the same
        ``mo2_running()`` check the Launch Game button itself uses - it
        becomes available again once the game quits.
        """
        if self._reject_language_or_theme_change(
            tr(
                "Cannot change the language while a background task is running. Wait for it to finish, then try again."
            )
        ):
            return
        gui_settings.save_gui_settings(language=code)
        set_active_language(code)
        self.switch_language()

    def switch_language(self) -> None:
        """Tear down and rebuild the entire window in the newly active language.

        Every label/button text in this app is a plain string set once at
        widget-construction time from ``tr()`` - there is no live
        re-translation of an existing widget, so the only way to apply a
        language change is to reconstruct the widgets that hold it.
        """
        previous_key = (
            self._last_tab_key
            if self._settings_open
            else NAV_ITEMS[self.tabs.currentIndex()][0]
        )
        was_settings_open = self._settings_open

        # Stop every in-flight background task (page refresh polls,
        # dependency checks, etc.) and suppress result delivery until the
        # rebuild finishes, so a signal that arrives mid-teardown cannot
        # touch a widget that is about to be destroyed. install_busy being
        # False (checked by the caller) means nothing CLI-driving is
        # running, so this only ever cancels quick, safe-to-abandon lookups.
        shutdown_active_runners(timeout_ms=2000)

        old_central = self.takeCentralWidget()
        if old_central is not None:
            old_central.setParent(None)
            old_central.deleteLater()

        self._settings_open = False
        self._last_tab_key = "dashboard"
        self._nav_refresh_serial = 0
        self._build_ui()
        resume_after_shutdown()
        self._refresh_status_bar()

        if previous_key in _TAB_INDEX:
            self.tabs.setCurrentIndex(_TAB_INDEX[previous_key])
        # As in __init__: setCurrentIndex() above is a no-op when the
        # target is already the tab bar's default index (0, Dashboard),
        # so _on_nav() is called explicitly to guarantee that page is
        # rebuilt after _build_ui() wiped self._pages.
        self._on_nav(self.tabs.currentIndex())
        if was_settings_open:
            self.open_settings()

    def _apply_style(self) -> None:
        state = gui_settings.load_gui_settings()
        name = state.get("theme") or "gamma"
        font_size = int(state.get("font_size") or 13)
        font_family = state.get("font_family") or "Exo 2"
        set_active_theme(name)
        app = QApplication.instance()
        if app is not None:
            app.setPalette(build_palette(name))
            app.setStyleSheet(
                build_stylesheet(name, font_size=font_size, font_family=font_family)
            )
        self.tabs.update()
        self.backdrop.update()
        self.update()
        # The status bar is built exactly once and never rebuilt, so its
        # Theme/Font/size combos do not pick up a change made from the
        # Settings page's own pickers by themselves. Left showing the old
        # value, such a combo can no longer switch back to it: picking the
        # item it already displays emits no currentIndexChanged, so nothing
        # would be applied.
        self._refresh_status_bar()

    def set_install_busy(self, busy: bool, operation: str | None = None) -> None:
        """Lock/unlock every install-affecting control across pages.

        While busy, none of full install / anomaly install / verify / update
        apply / fresh reset / maintenance actions may be started, so two CLI
        processes never write the same install tree at once. Pages opt in by
        defining ``on_busy_changed``.
        """
        self.install_busy = busy
        self.install_operation = operation if busy else None
        for page in self._pages.values():
            notify = getattr(page, "on_busy_changed", None)
            if callable(notify):
                notify(busy)
            activity = getattr(page, "on_install_activity_changed", None)
            if callable(activity):
                activity(self.install_operation)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            paint_backdrop(painter, self.rect())
        finally:
            painter.end()

    # ----- own title bar -----
    def _build_title_strip(self) -> QWidget:
        """Minimize/maximize/close, floating in the top bar's top-right corner.

        Shown only when COMMANDER draws its own title bar (frameless window);
        otherwise the desktop's title bar provides these.
        """
        self._title_strip = build_window_buttons(self, self._drag_filter)
        return self._title_strip

    def apply_title_bar(self, custom: bool) -> None:
        """Draw COMMANDER's own title bar (frameless) or use the desktop's."""
        # Offscreen/minimal platforms (tests, screenshots) have no window
        # manager to hand moves and resizes to.
        custom = custom and QGuiApplication.platformName() not in ("offscreen", "minimal")
        visible = self.isVisible()
        self.custom_title_bar = custom
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, custom)
        self._sync_title_bar_layout()
        if visible:
            self.show()
        attach_resize_filter(self, self._resize_filter, custom)

    def _sync_title_bar_layout(self) -> None:
        """Show the window-button strip only with COMMANDER's own title bar."""
        custom = self.custom_title_bar
        self._title_strip.setVisible(custom)
        # Room for the strip, split equally above and below so the content
        # stays centered and the bar still follows font-size changes.
        pad = self._title_strip.height() // 2 if custom else 0
        self._topbar.layout().setContentsMargins(16, pad, 0, pad)

    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _update_max_button(self) -> None:
        update_max_button(self)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self._update_max_button()
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        if self.install_busy:
            answer = QMessageBox.question(
                self,
                tr("Install Running"),
                tr("An installation, verification, update, or dependency download is currently running. Are you sure you want to close?\n\nThis will terminate the running process and may leave the prefix partially configured."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        gui_settings.save_gui_settings(
            window_width=self.normalGeometry().width(),
            window_height=self.normalGeometry().height(),
        )
        event.accept()

    def update_mod_counter(self) -> None:
        """Refresh the topbar's always-visible active/total mod count.

        Always shown, even with nothing to count yet (no active profile,
        GAMMA not installed) - shows "0 Mods" rather than disappearing,
        so the topbar layout doesn't shift and the counter reads as a
        permanent fixture next to the settings cog.
        """
        profile = self.settings.active_profile
        counts = (
            count_active_mods(profile.gamma, profile.mo2_profile)
            if profile is not None
            else None
        )
        enabled, total = counts if counts is not None else (0, 0)
        incomplete = False
        if profile is not None:
            resume_state = gui_settings.load_gui_settings().get("gamma_install_resume")
            incomplete = _resume_state_matches(resume_state, profile)
        if incomplete:
            self.mod_counter_label.setText(
                tr("{enabled} Mods (incomplete)", enabled=enabled)
            )
            self.mod_counter_label.setStyleSheet(f"color: {WARN.name()};")
        else:
            self.mod_counter_label.setText(tr("{enabled} Mods", enabled=enabled))
            self.mod_counter_label.setStyleSheet("")
        if counts is not None:
            tooltip = tr(
                "{enabled} of {total} mods enabled in profile “{profile_name}”",
                enabled=enabled,
                total=total,
                profile_name=profile.profile_name,
            )
            if incomplete:
                tooltip += "\n" + tr(
                    "The last install attempt failed - this modpack may be incomplete. Resume the install on the Install page to finish it."
                )
            self.mod_counter_label.setToolTip(tooltip)
        else:
            self.mod_counter_label.setToolTip(
                tr("No active profile, or GAMMA is not installed yet.")
            )
        self.mod_counter_label.show()

    def refresh_settings(self) -> None:
        self.settings = load_settings()
        self._refresh_status_bar()
        self.update_mod_counter()
