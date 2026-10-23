"""The ModDB browser plugin's own program: ``python -m commander_gui.moddb_browser``.

Shipped only in the plugin archive (``build-moddb-plugin.sh``), never run by
COMMANDER's own interpreter - that build has no QtWebEngine. COMMANDER starts
it and writes one JSON line to its stdin (see ``plugins.py``); every finished
download is reported back as one JSON line on stdout.

Only ModDB's own pages open here. A link anywhere else goes to the player's
normal browser; redirects (download mirrors) and frames (the Cloudflare
check) still load, since they aren't somewhere the player browses to.

Requests to the ad and tracking networks ModDB pages load are blocked, and
the empty boxes they leave are hidden.

Changed this file? Raise ``plugins.PROTOCOL``: players who already have the
plugin keep their old copy of it until that number moves.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtWebEngineCore import (
    QWebEngineDownloadRequest,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import plugins, themes
from .i18n import set_active_language, tr
from .ui.common import open_url
from .ui.dialogs import MessageBox
from .ui.multiplayer_page import _hint
from .ui.title_bar import (
    EdgeResizeFilter,
    WindowDragFilter,
    attach_resize_filter,
    build_window_buttons,
    pin_top_right,
    update_max_button,
)

HOME = "https://www.moddb.com/mods/stalker-anomaly/addons"
# ponytail: Anomaly's addon search only; ModDB's site-wide search if players want other games' mods.
SEARCH = HOME + "?filter=t&kw={}"


#: Ad, tracking and ad-block-detection hosts seen on ModDB pages, plus the
#: common ad exchanges behind them. A host and all its subdomains match.
# ponytail: a hand-kept list; load EasyList if ModDB's ads start slipping through.
AD_HOSTS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "googletagmanager.com", "googletagservices.com", "google-analytics.com",
    "adservice.google.com", "fundingchoicesmessages.google.com",
    "ad-delivery.net", "intergient.com", "btloader.com", "html-load.cc",
    "dns-finder.com", "cloudflareinsights.com", "amazon-adsystem.com",
    "adnxs.com", "rubiconproject.com", "pubmatic.com", "openx.net",
    "criteo.com", "criteo.net", "casalemedia.com", "3lift.com",
    "taboola.com", "outbrain.com", "moatads.com", "scorecardresearch.com",
    "quantserve.com", "adsrvr.org", "sharethrough.com", "media.net",
)
#: Hides the boxes the blocked ads would have filled.
_HIDE_ADS_CSS = (
    "ins.adsbygoogle, iframe[src*='doubleclick'], div[id^='google_ads'],"
    " div[id*='-ad-'], div[class*='advert'], div[id^='div-gpt-ad'],"
    " .adbox { display: none !important; }"
)
#: Leaves only the addon list and addon pages: ModDB's header and menus, the
#: Anomaly blurb, the sidebar, the list's filter form and the footer go, and
#: the content column takes the freed width.
_TRIM_CSS = (
    " body > #body > .container > header, #body footer, .headermediabox,"
    " .nextmediabox, .column.span-all, .column.span-300.last,"
    " #downloadsbrowse form.filter, #adBanner { display: none !important; }"
    " .column.span-300-alt { width: 100% !important; margin: 0 !important; }"
)


#: Height (px) of everything in the top bar, kept slim.
_BAR_HEIGHT = 26


def page_css(tokens: dict) -> str:
    """CSS added to every page: hides the empty ad boxes and everything but
    the addons, and draws the scrollbars like COMMANDER's own (thin, theme colours)."""
    thumb = tokens.get("border_strong", "#3a4a33")
    # The standard properties, not ::-webkit-scrollbar: Chromium ignores those
    # as soon as a page sets scrollbar-color or scrollbar-width itself.
    return _HIDE_ADS_CSS + _TRIM_CSS + (
        f" * {{ scrollbar-color: {thumb} transparent !important;"
        " scrollbar-width: thin !important; }"
    )


def is_ad_host(host: str) -> bool:
    host = host.lower()
    return any(host == ad or host.endswith("." + ad) for ad in AD_HOSTS)


class _AdBlock(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info) -> None:
        if is_ad_host(info.requestUrl().host()):
            info.block(True)


def is_moddb(url: QUrl) -> bool:
    host = url.host().lower()
    return url.scheme() == "https" and (host == "moddb.com" or host.endswith(".moddb.com"))


class _Page(QWebEnginePage):
    def acceptNavigationRequest(self, url, nav_type, is_main_frame) -> bool:
        if (
            not is_main_frame
            or nav_type == QWebEnginePage.NavigationType.NavigationTypeRedirect
            or is_moddb(url)
        ):
            return True
        # Only a link the player clicked opens their browser: a page's own
        # script (an ad, a pop-under) navigating away is just stopped.
        if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            open_url(url.toString())
        return False

    def javaScriptConsoleMessage(self, *_args) -> None:
        pass  # pages' console noise would fill COMMANDER's copy of stderr


class _View(QWebEngineView):
    def createWindow(self, _type):
        # Pop-ups and target=_blank links load here, through the same filter.
        return self


class BrowserWindow(QMainWindow):
    def __init__(self, config: dict) -> None:
        super().__init__()
        self.custom_title_bar = False  # read by the title-bar filters; set in _apply_title_bar()
        self.setWindowTitle(tr("ModDB - STALKER COMMANDER"))
        self.resize(1200, 800)
        self._downloads = Path(config["downloads"])
        self._downloads.mkdir(parents=True, exist_ok=True)

        # Owned by the app, not this window: it must outlive the page using it.
        profile = QWebEngineProfile("commander-moddb", QApplication.instance())
        profile.setPersistentStoragePath(config["data"])
        profile.setCachePath(str(Path(config["data"]) / "cache"))
        profile.downloadRequested.connect(self._download)
        self._adblock = _AdBlock(profile)
        profile.setUrlRequestInterceptor(self._adblock)
        style = QWebEngineScript()
        style.setName("commander-style")
        # At creation, before the page paints, so the default scrollbar never flashes.
        style.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        style.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
        # The page has no <html> element yet at that point: add the style the
        # moment it appears.
        style.setSourceCode(
            "const s = document.createElement('style');"
            f"s.textContent = {json.dumps(page_css(config.get('tokens') or {}))};"
            "const add = () => document.documentElement && !s.isConnected"
            " && document.documentElement.appendChild(s);"
            "if (!add()) new MutationObserver((_m, o) => add() && o.disconnect())"
            ".observe(document, {childList: true, subtree: true});"
        )
        profile.scripts().insert(style)
        self.view = _View(self)
        page = _Page(profile, self.view)
        page.settings().setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        self.view.setPage(page)
        # A redirect is let through for download mirrors; one that lands on a
        # page outside ModDB goes to the normal browser instead.
        self.view.urlChanged.connect(self._left_moddb)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_topbar())
        body = QVBoxLayout()
        body.setContentsMargins(8, 6, 8, 4)
        body.setSpacing(6)
        body.addWidget(self.view, 1)
        self.status = _hint(tr("Pick a mod and press Download. COMMANDER asks before installing it."))
        body.addWidget(self.status)
        layout.addLayout(body, 1)
        self._apply_title_bar(bool(config.get("custom_title_bar", True)))
        self._home()

    def _build_topbar(self) -> QWidget:
        """COMMANDER's top bar: wordmark, page buttons, search, window buttons."""
        self._drag_filter = WindowDragFilter(self)
        self._resize_filter = EdgeResizeFilter(self)
        self._topbar = QWidget()
        self._topbar.setObjectName("topbar")
        row = QHBoxLayout(self._topbar)
        row.setSpacing(6)
        # Wordmark and byline side by side, not stacked: one line keeps the bar slim.
        wordmark_block = QWidget()
        wordmark_block.setObjectName("wordmarkBlock")
        words = QHBoxLayout(wordmark_block)
        words.setContentsMargins(0, 0, 10, 0)
        words.setSpacing(8)
        wordmark = QLabel(tr("COMMANDER"))
        wordmark.setObjectName("wordmark")
        byline = QLabel(tr("ModDB browser"))
        byline.setObjectName("byline")
        words.addWidget(wordmark, 0, Qt.AlignmentFlag.AlignBaseline)
        words.addWidget(byline, 0, Qt.AlignmentFlag.AlignBaseline)
        row.addWidget(wordmark_block, 0, Qt.AlignmentFlag.AlignVCenter)
        for text, tip, slot in (
            ("←", tr("Back"), self.view.back),
            ("→", tr("Forward"), self.view.forward),
            (tr("Home"), "", self._home),
        ):
            button = QPushButton(text)
            button.setObjectName("settingAction")
            button.setToolTip(tip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(_BAR_HEIGHT)
            button.setStyleSheet("padding: 0 12px;")
            button.clicked.connect(slot)
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search Anomaly addons on ModDB"))
        self.search.setMaximumWidth(520)
        self.search.setFixedHeight(_BAR_HEIGHT)
        self.search.returnPressed.connect(self._search)
        row.addWidget(self.search, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        for widget in (self._topbar, wordmark_block, wordmark, byline):
            widget.installEventFilter(self._drag_filter)
        self._title_strip = build_window_buttons(self, self._drag_filter)
        pin_top_right(self._title_strip, self._topbar)
        return self._topbar

    def _apply_title_bar(self, custom: bool) -> None:
        """COMMANDER's own title bar (frameless), like its main window, unless
        the player turned that off in Settings."""
        custom = custom and QGuiApplication.platformName() not in ("offscreen", "minimal")
        self.custom_title_bar = custom
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, custom)
        self._title_strip.setVisible(custom)
        # The window buttons sit in the top-right corner, over the empty end
        # of the bar: only room beside them is needed, not above and below.
        right = self._title_strip.width() + 8 if custom else 12
        self._topbar.layout().setContentsMargins(12, 4, right, 4)
        attach_resize_filter(self, self._resize_filter, custom)

    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            update_max_button(self)
        super().changeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # The QWindow only exists once shown; the resize filter goes on it.
        attach_resize_filter(self, self._resize_filter, self.custom_title_bar)

    def _left_moddb(self, url: QUrl) -> None:
        if url.scheme() in ("http", "https") and not is_moddb(url):
            open_url(url.toString())
            self._home()

    def _home(self) -> None:
        self.view.load(QUrl(HOME))

    def _search(self) -> None:
        text = self.search.text().strip()
        self.view.load(QUrl(SEARCH.format(quote(text)) if text else HOME))

    def _download(self, request: QWebEngineDownloadRequest) -> None:
        request.setDownloadDirectory(str(self._downloads))
        name = request.downloadFileName()

        def progress() -> None:
            total, done = request.totalBytes(), request.receivedBytes()
            self.status.setText(
                tr("Downloading {name}… {percent}%", name=name, percent=done * 100 // total)
                if total > 0 else tr("Downloading {name}…", name=name)
            )

        def finished() -> None:
            state = request.state()
            if state == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
                self.status.setText(tr("{name} downloaded. Switch to COMMANDER to install it.", name=name))
                path = Path(request.downloadDirectory()) / request.downloadFileName()
                print(json.dumps({"event": "downloaded", "file": str(path), "url": request.url().toString()}), flush=True)
            elif state != QWebEngineDownloadRequest.DownloadState.DownloadInProgress:
                self.status.setText(tr("{name} didn't finish downloading.", name=name))

        request.receivedBytesChanged.connect(progress)
        request.isFinishedChanged.connect(finished)
        request.accept()


def _apply_theme(app: QApplication, config: dict) -> None:
    tokens = config.get("tokens")
    if isinstance(tokens, dict):
        # Sent by COMMANDER, so a theme newer than this plugin still matches.
        themes.THEMES["commander"] = tokens
        app.setPalette(themes.build_palette("commander"))
    if config.get("stylesheet"):
        app.setStyleSheet(config["stylesheet"])


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("STALKER COMMANDER ModDB")
    app.setStyle("Fusion")
    try:
        config = json.loads(sys.stdin.readline() if not sys.stdin.isatty() else "")
        if config.get("protocol") != plugins.PROTOCOL:
            raise ValueError
    except (ValueError, AttributeError, OSError):
        MessageBox.information(
            None, tr("Open this from COMMANDER"), tr("Open the ModDB browser from COMMANDER: Settings > Plugins.")
        )
        return 2
    set_active_language(config.get("language") or "en")
    _apply_theme(app, config)
    window = BrowserWindow(config)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
