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
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import cli_binary_path
from ..settings import load_settings
from .about_page import AboutPage
from .dashboard import DashboardPage
from .help_page import HelpPage
from .install_page import InstallPage
from .mod_manager_page import ModManagerPage
from .play_page import PlayPage
from .profiles_page import ProfilesPage
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

        base = QLinearGradient(0, 0, rect.width(), rect.height())
        base.setColorAt(0.0, QColor("#101708"))
        base.setColorAt(1.0, QColor("#070a05"))
        painter.fillRect(rect, base)

        radius = max(rect.width(), rect.height())
        glow = QRadialGradient(
            rect.width() * 0.18, rect.height() * 0.08, radius * 0.95
        )
        glow.setColorAt(0.0, QColor(122, 217, 90, 70))
        glow.setColorAt(0.35, QColor(58, 120, 44, 40))
        glow.setColorAt(0.7, QColor(26, 55, 22, 18))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(rect, glow)

        glow2 = QRadialGradient(
            rect.width() * 0.95, rect.height() * 0.96, radius * 0.7
        )
        glow2.setColorAt(0.0, QColor(50, 95, 38, 55))
        glow2.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(rect, glow2)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER")
        self.resize(1080, 720)
        self.settings = load_settings()
        self.install_busy = False

        central = Backdrop(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # top header: wordmark + tab navigation
        header = QWidget()
        header.setObjectName("topbar")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 0, 8, 0)
        header_layout.setSpacing(16)

        wordmark = QLabel("COMMANDER")
        wordmark.setObjectName("wordmark")
        header_layout.addWidget(wordmark)

        self.tabs = QTabBar()
        self.tabs.setObjectName("navtabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setExpanding(False)
        header_layout.addWidget(self.tabs, 1)

        self._page_index: dict[str, int] = {}
        self._pages: dict[str, QWidget] = {}
        self.stack = QStackedWidget()

        for key, title in NAV_ITEMS:
            self._pages[key] = self._create_page(key)
            self._page_index[key] = self.stack.count()
            self.stack.addWidget(self._pages[key])
            self.tabs.addTab(title)

        self.tabs.currentChanged.connect(self._on_nav)
        layout.addWidget(header)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        # addTab() already selected index 0 before the signal was connected, so
        # setCurrentIndex(0) is a no-op. Drive the first page explicitly rather
        # than relying on it to refresh itself in its constructor.
        self.tabs.setCurrentIndex(0)
        self._on_nav(0)

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
        raise ValueError(key)

    def _active_name(self) -> str:
        profile = self.settings.active_profile
        return profile.profile_name if profile else "(none)"

    def _on_nav(self, index: int) -> None:
        if not (0 <= index < len(NAV_ITEMS)):
            return
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
        self.tabs.setCurrentIndex(self._page_index[key])

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
