"""Main application window: top tab navigation + stacked pages."""

from __future__ import annotations

from typing import ClassVar

from PySide6.QtCore import QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version_label__, gui_settings
from ..i18n import set_active_language
from ..settings import load_settings
from ..themes import (
    active_theme_tokens,
    build_palette,
    build_stylesheet,
    set_active_theme,
)
from .about_page import AboutPage
from .common import (
    count_active_mods,
    resume_after_shutdown,
    shutdown_active_runners,
    tr,
)
from .dashboard import DashboardPage
from .help_page import HelpPage
from .install_page import InstallPage
from .mod_manager_page import ModManagerPage
from .play_page import PlayPage
from .profiles_page import ProfilesPage
from .settings_page import SettingsPage
from .system_check_page import SystemCheckPage
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

# Indices after which a thin vertical separator is drawn in the tab bar.
_SEPARATOR_AFTER = {1, 4, 7}


class NavTabBar(QTabBar):
    """QTabBar subclass that draws thin vertical separators between tab groups."""

    def paintEvent(self, _event) -> None:
        super().paintEvent(_event)

        if self.count() < 2:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Use the active theme accent so separators stay green in GAMMA,
        # teal in Midnight, amber in Dusk, and match the other theme accents.
        accent = QColor(active_theme_tokens()["accent_strong"])
        accent.setAlpha(150)
        pen = QPen(accent)
        pen.setWidth(1)
        painter.setPen(pen)

        for i in range(self.count()):
            if i in _SEPARATOR_AFTER and i < self.count() - 1:
                r = self.tabRect(i)
                x = r.right() + 4
                painter.drawLine(QPointF(x, r.top() + 5), QPointF(x, r.bottom() - 5))

        painter.end()


class Backdrop(QWidget):
    """Dark GAMMA-style backdrop with a soft, blurred radiation-green glow.

    The background is painted (not styled) so the QSS ``QWidget`` background
    rule does not cover it; page roots stay semi-transparent and let the glow
    bleed through behind the cards.
    """

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        gui_state = gui_settings.load_gui_settings()
        self.resize(gui_state["window_width"], gui_state["window_height"])
        self.settings = load_settings()
        self.install_busy = False
        self.install_operation: str | None = None
        self._settings_open = False
        self._last_tab_key = "dashboard"
        self._nav_refresh_serial = 0

        self._build_ui()
        self.tabs.setCurrentIndex(0)

        start_page = gui_settings.load_gui_settings().get("start_page")
        if start_page and start_page in self._page_index and start_page != "settings":
            self.tabs.setCurrentIndex(self._page_index[start_page])

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

        # top header: wordmark + tab navigation
        header = QWidget()
        header.setObjectName("topbar")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 0, 8, 0)
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

        header_layout.addWidget(wordmark_block)

        header_layout.addStretch(1)

        self.tabs = NavTabBar()
        self.tabs.setObjectName("navtabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setExpanding(False)
        header_layout.addWidget(self.tabs)

        header_layout.addStretch(1)

        self.mod_counter_label = QLabel()
        self.mod_counter_label.setObjectName("modCounter")
        self.mod_counter_label.hide()
        header_layout.addWidget(self.mod_counter_label)

        self._cog = QPushButton(tr("⚙"))
        self._cog.setObjectName("cogButton")
        self._cog.setToolTip(tr("Settings"))
        self._cog.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cog.setFixedHeight(28)
        self._cog.clicked.connect(self.toggle_settings)
        header_layout.addWidget(self._cog)
        self.update_mod_counter()

        self._page_index: dict[str, int] = {}
        self._pages: dict[str, QWidget] = {}
        self.stack = QStackedWidget()

        for key, title in NAV_ITEMS:
            self._pages[key] = self._create_page(key)
            self._page_index[key] = self.stack.count()
            self.stack.addWidget(self._pages[key])
            # Translated here, not by wrapping NAV_ITEMS itself: NAV_ITEMS is
            # a module-level constant evaluated at import time, before
            # main.py ever calls set_active_language() - tr() would always
            # resolve to English if baked in there instead of at display time.
            self.tabs.addTab(tr(title))

        self._pages["settings"] = self._create_page("settings")
        self._page_index["settings"] = self.stack.count()
        self.stack.addWidget(self._pages["settings"])

        self.tabs.currentChanged.connect(self._on_nav)
        self.tabs.tabBarClicked.connect(self._on_tab_clicked)
        layout.addWidget(header)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        # Keep the full navigation strip visible at startup. The saved window
        # size may predate the current tab labels and otherwise enables the
        # QTabBar scroll arrows.
        self.tabs.setMinimumWidth(self.tabs.sizeHint().width())
        header_width = header.sizeHint().width()
        self.setMinimumWidth(header_width)
        if self.width() < header_width:
            self.resize(header_width, self.height())

        self.statusBar().showMessage(
            f"COMMANDER {__version_label__}   |   Active profile: {self._active_name()}"
        )
        github_link = QPushButton(tr("GitHub"))
        github_link.setObjectName("githubLink")
        github_link.setToolTip(tr("Open SSH-Kitty on GitHub"))
        github_link.setFlat(True)
        github_link.setCursor(Qt.CursorShape.PointingHandCursor)
        github_link.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl("https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER")
            )
        )
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().addPermanentWidget(github_link)

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

    def _active_name(self) -> str:
        profile = self.settings.active_profile
        return profile.profile_name if profile else "(none)"

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
        self.stack.setCurrentIndex(self._page_index[key])
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
        if key not in self._page_index:
            return
        self.tabs.setCurrentIndex(self._page_index[key])

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
        self.stack.setCurrentIndex(self._page_index["settings"])
        QTimer.singleShot(0, self._pages["settings"].refresh)

    def close_settings(self) -> None:
        if not self._settings_open:
            return
        self._settings_open = False
        self._set_cog_active(False)
        self.tabs.setProperty("settingsMode", False)
        self.tabs.style().unpolish(self.tabs)
        self.tabs.style().polish(self.tabs)
        page_index = self._page_index[self._last_tab_key]
        self.stack.setCurrentIndex(page_index)
        self.tabs.setCurrentIndex(page_index)

    def toggle_settings(self) -> None:
        if self._settings_open:
            self.close_settings()
        else:
            self.open_settings()

    def apply_theme(self, name: str) -> None:
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
        """
        if self.install_busy:
            QMessageBox.warning(
                self,
                tr("Busy"),
                tr("Cannot change the language while a background task is running. Wait for it to finish, then try again."),
            )
            settings_page = self._pages.get("settings")
            if settings_page is not None and hasattr(settings_page, "refresh"):
                # Revert the combo to the still-active language - it already
                # shows the rejected selection from the signal that called us.
                settings_page.refresh()
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

        if previous_key in self._page_index:
            self.tabs.setCurrentIndex(self._page_index[previous_key])
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
        """Refresh the topbar's always-visible active/total mod count."""
        profile = self.settings.active_profile
        counts = (
            count_active_mods(profile.gamma, profile.mo2_profile)
            if profile is not None
            else None
        )
        if counts is None:
            self.mod_counter_label.hide()
            return
        enabled, total = counts
        self.mod_counter_label.setText(tr("{enabled} Mods", enabled=enabled))
        self.mod_counter_label.setToolTip(
            tr("{enabled} of {total} mods enabled in profile “{profile_name}”", enabled=enabled, total=total, profile_name=profile.profile_name)
        )
        self.mod_counter_label.show()

    def refresh_settings(self) -> None:
        self.settings = load_settings()
        self.statusBar().showMessage(
            f"COMMANDER {__version_label__}   |   Active profile: {self._active_name()}"
        )
        self.update_mod_counter()
