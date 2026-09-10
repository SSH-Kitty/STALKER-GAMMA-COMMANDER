"""Main application window: top tab navigation + stacked pages."""

from __future__ import annotations

from typing import ClassVar

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    Qt,
    QTimer,
    QUrl,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
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
from ..i18n import LANGUAGE_INFO, active_language, set_active_language
from ..settings import load_settings
from ..themes import (
    THEME_INFO,
    active_theme,
    active_theme_tokens,
    build_palette,
    build_stylesheet,
    set_active_theme,
)
from .about_page import AboutPage
from .common import (
    NoWheelComboBox,
    count_active_mods,
    mo2_running,
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


#: Text grows to this fraction of its normal size while a tab is hovered.
_HOVER_SCALE = 1.12
_HOVER_ANIM_MS = 150


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
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.tabText(i))

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

        # Built before _build_ui(): constructing pages there (e.g. the
        # Dashboard) can trigger refresh_settings() -> _refresh_status_bar()
        # immediately, which needs these widgets to already exist.
        self._build_status_bar()
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
        self.stack.setGraphicsEffect(self._stack_opacity)
        self._stack_fade = QPropertyAnimation(self._stack_opacity, b"opacity", self)
        self._stack_fade.setDuration(400)
        self._stack_fade.setStartValue(0.9)
        self._stack_fade.setEndValue(1.0)
        self._stack_fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.stack.currentChanged.connect(self._on_stack_page_changed)

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

        self._refresh_status_bar()

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

    def _refresh_status_bar(self) -> None:
        """Re-render status bar text and re-select the combos' current items.

        Called after anything that could change the active profile, language
        or theme. The status bar is built once and never torn down (unlike
        page content, which switch_language() rebuilds from scratch), so its
        tr()-wrapped text needs this explicit refresh to pick up a language
        change instead of getting it "for free" via reconstruction.
        """
        self._status_info_label.setText(
            f"COMMANDER {__version_label__}   |   Active profile: {self._active_name()}   |   {tr('Language:')}"
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

    def _on_status_language(self, *_args) -> None:
        code = self._status_language_combo.currentData()
        if code:
            self.apply_language(code)

    def _on_status_theme(self, *_args) -> None:
        key = self._status_theme_combo.currentData()
        if key:
            self.apply_theme(key)

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
        self._refresh_status_bar()
        self.update_mod_counter()
