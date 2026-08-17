"""Main application window: top tab navigation + stacked pages."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QLinearGradient,
    QPainter,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, gui_settings
from ..config import cli_binary_path
from ..settings import load_settings
from ..themes import (
    active_theme_tokens,
    build_palette,
    build_stylesheet,
    set_active_theme,
)
from .about_page import AboutPage
from .dashboard import DashboardPage
from .help_page import HelpPage
from .install_page import InstallPage
from .mod_manager_page import ModManagerPage
from .play_page import PlayPage
from .profiles_page import ProfilesPage
from .settings_page import SettingsPage
from .update_page import UpdatePage
from .utilities_page import UtilitiesPage

NAV_ITEMS = [
    ("dashboard", "Dashboard"),
    ("play", "Play"),
    ("install", "Install"),
    ("update", "Update"),
    ("modmanager", "Mod Manager"),
    ("profiles", "Profiles"),
    ("utilities", "Utilities"),
    ("help", "Help"),
    ("about", "About"),
]


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
        glow = QRadialGradient(
            rect.width() * 0.18, rect.height() * 0.08, radius * 0.95
        )
        glow.setColorAt(0.0, QColor(r1[0], r1[1], r1[2], int(tokens["back_glow1_a"])))
        r1b = rgb("back_glow1b_rgb")
        glow.setColorAt(0.35, QColor(r1b[0], r1b[1], r1b[2], int(tokens["back_glow1b_a"])))
        r1c = rgb("back_glow1c_rgb")
        glow.setColorAt(0.7, QColor(r1c[0], r1c[1], r1c[2], int(tokens["back_glow1c_a"])))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(rect, glow)

        r2 = rgb("back_glow2_rgb")
        glow2 = QRadialGradient(
            rect.width() * 0.95, rect.height() * 0.96, radius * 0.7
        )
        glow2.setColorAt(0.0, QColor(r2[0], r2[1], r2[2], int(tokens["back_glow2_a"])))
        glow2.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(rect, glow2)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER")
        self.resize(1080, 720)
        self.settings = load_settings()
        self.install_busy = False
        self._settings_open = False
        self._last_tab = 0

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

        wordmark = QLabel("COMMANDER")
        wordmark.setObjectName("wordmark")
        wordmark_layout.addWidget(wordmark)

        byline = QLabel("by SSH-Kitty")
        byline.setObjectName("byline")
        byline.setAlignment(Qt.AlignmentFlag.AlignRight)
        wordmark_layout.addWidget(byline)

        header_layout.addWidget(wordmark_block)

        self.tabs = QTabBar()
        self.tabs.setObjectName("navtabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setExpanding(False)
        header_layout.addWidget(self.tabs, 1)

        cog = QPushButton("\u2699")
        cog.setObjectName("cogButton")
        cog.setToolTip("Settings")
        cog.setCursor(Qt.CursorShape.PointingHandCursor)
        cog.setFixedHeight(28)
        cog.clicked.connect(self.toggle_settings)
        header_layout.addWidget(cog)

        self._page_index: dict[str, int] = {}
        self._pages: dict[str, QWidget] = {}
        self.stack = QStackedWidget()

        for key, title in NAV_ITEMS:
            self._pages[key] = self._create_page(key)
            self._page_index[key] = self.stack.count()
            self.stack.addWidget(self._pages[key])
            self.tabs.addTab(title)

        self._pages["settings"] = self._create_page("settings")
        self._page_index["settings"] = self.stack.count()
        self.stack.addWidget(self._pages["settings"])

        self.tabs.currentChanged.connect(self._on_nav)
        layout.addWidget(header)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        # addTab() already selected index 0 before the signal was connected, so
        # setCurrentIndex(0) is a no-op. Drive the first page explicitly rather
        # than relying on it to refresh itself in its constructor.
        self.tabs.setCurrentIndex(0)
        self._on_nav(0)

        start_page = gui_settings.load_gui_settings().get("start_page")
        if start_page and start_page in self._page_index and start_page != "settings":
            self.tabs.setCurrentIndex(self._page_index[start_page])

        self.statusBar().showMessage(
            f"CLI: {cli_binary_path()}   |   "
            f"GUI v{__version__}   |   "
            f"Active profile: {self._active_name()}"
        )
        github_link = QPushButton("GitHub")
        github_link.setObjectName("githubLink")
        github_link.setToolTip("Open SSH-Kitty on GitHub")
        github_link.setFlat(True)
        github_link.setCursor(Qt.CursorShape.PointingHandCursor)
        github_link.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER"))
        )
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().addPermanentWidget(github_link)

    def _create_page(self, key: str) -> QWidget:
        if key == "play":
            return PlayPage(self)
        if key == "dashboard":
            return DashboardPage(self)
        if key == "install":
            return InstallPage(self)
        if key == "update":
            return UpdatePage(self)
        if key == "modmanager":
            return ModManagerPage(self)
        if key == "profiles":
            return ProfilesPage(self)
        if key == "utilities":
            return UtilitiesPage(self)
        if key == "help":
            return HelpPage(self)
        if key == "about":
            return AboutPage(self)
        if key == "settings":
            return SettingsPage(self)
        raise ValueError(key)

    def _active_name(self) -> str:
        profile = self.settings.active_profile
        return profile.profile_name if profile else "(none)"

    def _on_nav(self, index: int) -> None:
        if not (0 <= index < len(NAV_ITEMS)):
            return
        self._settings_open = False
        key = NAV_ITEMS[index][0]
        self.setWindowTitle(
            "Install S.T.A.L.K.E.R. G.A.M.M.A."
            if key == "install"
            else "S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER"
        )
        self.stack.setCurrentIndex(self._page_index[key])
        page = self._pages[key]
        if hasattr(page, "refresh"):
            page.refresh()
        if key == "install" and hasattr(page, "enable_winetricks_status"):
            page.enable_winetricks_status()

    def set_page(self, key: str) -> None:
        if key == "settings":
            self.open_settings()
            return
        self.tabs.setCurrentIndex(self._page_index[key])

    def open_settings(self) -> None:
        if self._settings_open:
            return
        self._settings_open = True
        self._last_tab = self.tabs.currentIndex()
        self.setWindowTitle("Settings - S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER")
        self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(self._last_tab)
        self.tabs.blockSignals(False)
        self.stack.setCurrentIndex(self._page_index["settings"])
        self._pages["settings"].refresh()

    def close_settings(self) -> None:
        if not self._settings_open:
            return
        self._settings_open = False
        self.tabs.setCurrentIndex(self._last_tab)

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

    def _apply_style(self) -> None:
        state = gui_settings.load_gui_settings()
        name = state.get("theme") or "gamma"
        font_size = int(state.get("font_size") or 13)
        set_active_theme(name)
        app = QApplication.instance()
        if app is not None:
            app.setPalette(build_palette(name))
            app.setStyleSheet(build_stylesheet(name, font_size=font_size))
        self.backdrop.update()

    def set_install_busy(self, busy: bool) -> None:
        """Lock/unlock every install-affecting control across pages.

        While busy, none of full install / anomaly install / verify / update
        apply / fresh reset / maintenance actions may be started, so two CLI
        processes never write the same install tree at once. Pages opt in by
        defining ``on_busy_changed``.
        """
        self.install_busy = busy
        for page in self._pages.values():
            notify = getattr(page, "on_busy_changed", None)
            if callable(notify):
                notify(busy)

    def refresh_settings(self) -> None:
        self.settings = load_settings()
        self.statusBar().showMessage(
            f"Active profile: {self._active_name()}"
        )
