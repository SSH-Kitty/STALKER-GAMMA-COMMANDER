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
import os
import sys
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QIcon, QKeySequence, QShortcut
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
    QProgressBar,
    QPushButton,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import plugins, themes
from .atomic import write_bytes
from .i18n import set_active_language, tr
from .ui.common import make_card, open_in_file_manager, open_url
from .ui.dialogs import MessageBox
from .ui.multiplayer_page import _hint
from .ui.title_bar import (
    EdgeResizeFilter,
    WindowButton,
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
    " #downloadsbrowse form.filter, .normalcorner .options, #adBanner"
    " { display: none !important; }"
    " .column.span-300-alt { width: 100% !important; margin: 0 !important; }"
    # The comment form's sign-up and social sign-in, which can't work here
    # (they leave ModDB), cut down to its first sentence. CSS can't cut a text
    # node, so the paragraph's text shrinks to nothing and the line is redrawn.
    " .rowcommentguest a, .rowcommentguest .helper { display: none !important; }"
    " .rowcommentguest p { font-size: 0 !important; width: auto !important;"
    " float: none !important; margin: 0 0 6px !important; }"
    " .rowcommentguest p::before { content: 'Your comment will be anonymous.';"
    " font-size: 12px; }"
)


#: Height (px) of everything in the top bar, kept slim.
_BAR_HEIGHT = 26


def page_css(tokens: dict) -> str:
    """CSS added to every page: hides the empty ad boxes and everything but
    the addons, swaps ModDB's backdrop picture for COMMANDER's background, and
    draws the scrollbars like COMMANDER's own (thin, theme colours)."""
    def t(name: str, fallback: str) -> str:
        return tokens.get(name, fallback)

    thumb = t("border_strong", "#3a4a33")
    bg = t("bg", "#0d130c")
    card, chip, border = t("card", "#141b15"), t("chip", "#18221a"), t("border", "#26321f")
    # The standard properties, not ::-webkit-scrollbar: Chromium ignores those
    # as soon as a page sets scrollbar-color or scrollbar-width itself.
    return _HIDE_ADS_CSS + _TRIM_CSS + (
        f" html, body, #body, .container {{ background: {bg} !important; }}"
        # ModDB's grey boxes drawn as COMMANDER cards: card fill, thin border,
        # a slightly lighter title strip, rows on the card colour.
        f" .normalbox {{ background: {card} !important; border: 1px solid {border} !important;"
        " border-radius: 8px; overflow: hidden; }"
        " .normalbox .inner, .normalbox .body, .normalbox .table,"
        " .normalbox div.table div.row, .normalbox div.table div.row:nth-of-type(2n-1)"
        f" {{ background: {card} !important; background-image: none !important; }}"
        f" .normalbox .normalcorner, .normalbox .normalcorner .title {{ background: {chip} !important;"
        f" background-image: none !important; border-bottom: 1px solid {border} !important; }}"
        f" .normalbox .normalcorner .heading {{ color: {t('accent_strong', '#8fe45c')} !important;"
        " text-shadow: none !important; }"
        f" .normalbox div.table div.row div.content p, .normalbox .row .summary p"
        f" {{ background: {chip} !important; border-color: {border} !important; }}"
        f" .normalbox .comment {{ background: {chip} !important; border: 1px solid {border} !important;"
        " border-radius: 6px; }"
        " .normalbox .comment p { background: transparent !important; }"
        f" .normalbox a {{ color: {t('text', '#e2ead8')}; }}"
        f" .normalbox, .normalbox p, .normalbox span, .normalbox h5 {{ color: {t('text_info', '#9aa88f')}; }}"
        f" .normalbox img {{ border-color: {border} !important; }}"
        f" .normalbox input, .normalbox textarea {{ background: {t('input', '#0d130e')} !important;"
        f" color: {t('text', '#e2ead8')} !important; border: 1px solid {t('border_input', '#2e3b2c')} !important; }}"
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
    def __init__(self, window: BrowserWindow) -> None:
        super().__init__()
        self._window = window

    def createWindow(self, type_):
        # Pop-ups, target=_blank and "open in new tab" links open a tab here,
        # through the same filter.
        background = type_ == QWebEnginePage.WebWindowType.WebBrowserBackgroundTab
        return self._window.new_tab(focus=not background)

    def contextMenuEvent(self, event) -> None:
        menu = self.createStandardContextMenu()
        for action in (
            QWebEnginePage.WebAction.SavePage,
            QWebEnginePage.WebAction.ViewSource,
            QWebEnginePage.WebAction.OpenLinkInNewWindow,  # it opens a tab too
        ):
            menu.removeAction(self.pageAction(action))
        menu.popup(event.globalPos())


class BrowserWindow(QMainWindow):
    def __init__(self, config: dict) -> None:
        super().__init__()
        self.custom_title_bar = False  # read by the title-bar filters; set in _apply_title_bar()
        self.setWindowTitle("ModDB Browser")
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
        self._profile = profile
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setMovable(True)
        self.tabs.tabBar().setObjectName("navtabs")
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.setStyleSheet(
            "QTabWidget::pane { border: none; }"
            " QTabBar::tab { padding: 4px 10px; max-width: 220px; }"
        )
        self.tabs.tabBar().setDrawBase(False)
        QShortcut(QKeySequence.StandardKey.AddTab, self, self._new_home_tab)
        QShortcut(QKeySequence.StandardKey.Close, self, lambda: self._close_tab(self.tabs.currentIndex()))

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_topbar())
        body = QVBoxLayout()
        body.setContentsMargins(8, 6, 8, 4)
        body.setSpacing(6)
        body.addWidget(self.tabs, 1)
        # One card per running download, bottom left; gone when it ends.
        self._download_cards = QVBoxLayout()
        self._download_cards.setSpacing(6)
        cards_row = QHBoxLayout()
        cards_row.addLayout(self._download_cards)
        cards_row.addStretch(1)
        body.addLayout(cards_row)
        self.status = _hint(tr("Pick a mod and press Download. COMMANDER asks before installing it."))
        body.addWidget(self.status)
        layout.addLayout(body, 1)
        self._apply_title_bar(bool(config.get("custom_title_bar", True)))
        # The first tab has no close button: it goes when the browser does.
        self._first = self.new_tab()
        self._first.load(QUrl(HOME))

    @property
    def view(self) -> _View:
        return self.tabs.currentWidget()

    def new_tab(self, focus: bool = True) -> _View:
        view = _View(self)
        page = _Page(self._profile, view)
        page.settings().setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        view.setPage(page)
        # A redirect is let through for download mirrors; one that lands on a
        # page outside ModDB goes to the normal browser instead.
        view.urlChanged.connect(lambda url: self._left_moddb(view, url))
        view.titleChanged.connect(lambda title: self._set_tab_title(view, title))
        index = self.tabs.addTab(view, tr("Loading"))
        if self.tabs.count() > 1:
            close = WindowButton("close")
            close.setObjectName("winClose")
            close.setToolTip(tr("Close"))
            close.setFixedSize(18, 18)
            close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            close.clicked.connect(lambda: self._close_tab(self.tabs.indexOf(view)))
            self.tabs.tabBar().setTabButton(index, QTabBar.ButtonPosition.RightSide, close)
        if focus:
            self.tabs.setCurrentIndex(index)
        return view

    def _new_home_tab(self) -> None:
        self.new_tab().load(QUrl(HOME))

    def _set_tab_title(self, view: _View, title: str) -> None:
        # "Better backpacks (0.1) addon - S.T.A.L.K.E.R. Anomaly mod for ...": the name is enough.
        index = self.tabs.indexOf(view)
        self.tabs.setTabText(index, title.split(" - ")[0].removesuffix(" addon"))
        self.tabs.setTabToolTip(index, title)

    def _close_tab(self, index: int) -> None:
        view = self.tabs.widget(index)
        if view is self._first:
            return
        self.tabs.removeTab(index)
        view.deleteLater()

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
            ("←", tr("Back"), lambda: self.view.back()),
            ("→", tr("Forward"), lambda: self.view.forward()),
            (tr("Home"), "", self._home),
            ("+", tr("New tab"), self._new_home_tab),
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
        self.search.setMaximumWidth(900)
        self.search.setFixedHeight(_BAR_HEIGHT)
        self.search.returnPressed.connect(self._search)
        row.addWidget(self.search, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(0)  # only what the search box can't take; Open downloads stays right
        downloads = QPushButton(tr("Open downloads"))
        downloads.setObjectName("settingAction")
        downloads.setCursor(Qt.CursorShape.PointingHandCursor)
        downloads.setFixedHeight(_BAR_HEIGHT)
        downloads.setStyleSheet("padding: 0 12px;")
        downloads.clicked.connect(lambda: open_in_file_manager(self._downloads))
        row.addWidget(downloads, 0, Qt.AlignmentFlag.AlignVCenter)
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

    def _left_moddb(self, view: _View, url: QUrl) -> None:
        if url.scheme() in ("http", "https") and not is_moddb(url):
            open_url(url.toString())
            view.load(QUrl(HOME))

    def _home(self) -> None:
        self.view.load(QUrl(HOME))

    def _search(self) -> None:
        text = self.search.text().strip()
        self.view.load(QUrl(SEARCH.format(quote(text)) if text else HOME))

    def _download(self, request: QWebEngineDownloadRequest) -> None:
        # A download link opened in a new tab leaves that tab blank: close it.
        for index in range(self.tabs.count()):
            view = self.tabs.widget(index)
            if view.page() is request.page() and not view.history().count() and self.tabs.count() > 1:
                self._close_tab(index)
                break
        request.setDownloadDirectory(str(self._downloads))
        name = request.downloadFileName()
        card, layout = make_card()
        card.setFixedWidth(380)
        title = QHBoxLayout()
        label = QLabel(tr("Downloading {name}", name=name))
        label.setToolTip(name)
        label.setMinimumWidth(1)
        title.addWidget(label, 1)
        cancel = QPushButton(tr("Cancel"))
        cancel.setObjectName("settingAction")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(request.cancel)
        title.addWidget(cancel)
        layout.addLayout(title)
        bar = QProgressBar()
        bar.setRange(0, 0)  # busy until the size is known
        layout.addWidget(bar)
        self._download_cards.addWidget(card)

        def progress() -> None:
            total, done = request.totalBytes(), request.receivedBytes()
            if total > 0:
                bar.setRange(0, 100)
                bar.setValue(done * 100 // total)

        def finished() -> None:
            state = request.state()
            if state == QWebEngineDownloadRequest.DownloadState.DownloadInProgress:
                return
            card.deleteLater()
            path = Path(request.downloadDirectory()) / request.downloadFileName()
            if state == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
                self.status.setText(tr("{name} downloaded. Switch to COMMANDER to install it.", name=name))
                print(json.dumps({"event": "downloaded", "file": str(path), "url": request.url().toString()}), flush=True)
                return
            # Cancelled or broken off: the half-written file (Chromium's
            # "<name>.download") is of no use.
            path.with_name(path.name + ".download").unlink(missing_ok=True)
            self.status.setText(
                tr("Download cancelled.")
                if state == QWebEngineDownloadRequest.DownloadState.DownloadCancelled
                else tr("{name} didn't finish downloading.", name=name)
            )

        request.receivedBytesChanged.connect(progress)
        request.isFinishedChanged.connect(finished)
        request.accept()


_DESKTOP_ID = "commander-moddb-browser"


def _write_desktop_entry() -> None:
    """A hidden .desktop entry, so the taskbar shows "ModDB Browser" and its
    icon instead of "python3.12"."""
    home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    target = Path(home) / "applications" / f"{_DESKTOP_ID}.desktop"
    content = (
        "[Desktop Entry]\nType=Application\nName=ModDB Browser\n"
        f"Icon={Path(__file__).with_name('assets') / 'moddb-browser.png'}\n"
        f"Exec={Path(__file__).resolve().parents[2] / 'moddb-browser'}\n"
        f"StartupWMClass={_DESKTOP_ID}\nNoDisplay=true\nTerminal=false\n"
    )
    try:
        if not target.is_file() or target.read_text(encoding="utf-8") != content:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_bytes(target, content.encode())
    except OSError:
        pass  # only the taskbar's label and icon depend on it


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
    app.setApplicationName("ModDB Browser")
    app.setApplicationDisplayName("ModDB Browser")
    app.setDesktopFileName(_DESKTOP_ID)
    _write_desktop_entry()
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("assets") / "moddb-browser.png")))
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
