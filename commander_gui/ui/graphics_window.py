"""The graphics overlay: Settings > Graphics in a panel down the left of the
screen over the running game, like NVIDIA's overlay. Opened from the Play
page or with a key (Shift+` by default).

The panel runs in a process of its own through XWayland, as the game splash
does: on Wayland no window can place itself or stay on top. COMMANDER tells
it to show or hide over its stdin.

The key is grabbed from the X server, so it works while an X11 window has
focus: the game under Proton is one, and so is the panel. Only while the
game runs, so the key stays free otherwise."""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QPointF,
    QRectF,
    QSize,
    QSocketNotifier,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QWindow,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..deck_launch import in_game_mode
from ..i18n import tr

#: Key choices: setting -> (label, X keysym, modifier mask). Shift is 1,
#: Control 4. Plain ` opens Anomaly's console, so not that. Labels go
#: through tr() where shown: ` is ^ or § on other layouts, hence "console key".
KEYS = {
    "shift+grave": ("Shift+` (console key)", "grave", 1),
    "kp_multiply": ("Numpad *", "KP_Multiply", 0),
    "ctrl+shift+g": ("Ctrl+Shift+G", "g", 1 | 4),
    "off": ("", "", 0),
}
DEFAULT_KEY = "shift+grave"


def key_setting() -> str:
    key = gui_settings.load_gui_settings().get("graphics_overlay_key")
    return key if key in KEYS else DEFAULT_KEY


def _tab_icon(kind: str, colour: str, size: int = 28) -> QPixmap:
    """A tab's line icon, drawn here so it takes the theme's colours."""
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(colour), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    if kind == "mangohud":
        # Performance: a frame time pulse.
        painter.drawPolyline([QPointF(2, 15), QPointF(8, 15), QPointF(11, 7), QPointF(15.5, 23),
                              QPointF(18.5, 12), QPointF(20.5, 15), QPointF(26, 15)])
    elif kind == "vkbasalt":
        # Effects: a wand with sparkles.
        painter.drawLine(QPointF(4, 24), QPointF(17, 11))

        def sparkle(x: float, y: float, r: float) -> None:
            painter.drawLine(QPointF(x - r, y), QPointF(x + r, y))
            painter.drawLine(QPointF(x, y - r), QPointF(x, y + r))

        sparkle(21, 7, 3.5)
        sparkle(10, 6, 2)
        sparkle(23, 17, 2)
    else:
        # Frame generation: a frame, fast-forwarded.
        painter.drawRoundedRect(QRectF(3, 5, 22, 18), 3, 3)
        for x in (9, 15):
            painter.drawPolyline([QPointF(x, 10), QPointF(x + 4, 14), QPointF(x, 18)])
    painter.end()
    return pixmap


class GraphicsWindow(QWidget):
    """MangoHud, vkBasalt and Frame generation on tabs down the left, docked
    to the left of the screen over other windows."""

    #: Sent to COMMANDER when the Frame generation tab changes the options text.
    options_changed = Signal(str)

    def __init__(self) -> None:
        from ..themes import active_theme_tokens
        from .frame_gen_card import FrameGenCard
        from .settings_page import GraphicsPanel  # deferred: settings_page is heavy

        # Frameless: no title bar or window buttons; the key closes it.
        # Managed by the window manager: Wine takes the game's focus from the
        # one it calls active, not from where X puts the keyboard (see toggle).
        # Kept out of the taskbar and alt-tab by _skip_taskbar.
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle(TITLE)
        self.setWindowOpacity(0.94)  # the game shows through a little
        # No window background: only the rail and the card float on the game.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(_WIDTH, 860)
        self._options = ""
        #: The game's window, while the panel is its transient (see toggle).
        self._game: QWindow | None = None
        #: The game minimized when the panel took focus: it was switched to
        #: fullscreen in the game, which user.ltx only says once it quits.
        #: No focus from then on, for this game (see _check_minimized).
        self._fullscreen = False
        # Keeps the cards' handlers: the cards themselves go on the tabs.
        self.panel = GraphicsPanel(loose=True)
        self.frame_gen = FrameGenCard(lambda: self._options, self._set_options)
        # Settings boxes (#statTile) inside the panel; here they float on the
        # game, so they take the Frame generation card's look.
        self.panel.mangohud_card.setObjectName("card")
        self.panel.vkbasalt_card.setObjectName("card")

        tokens = active_theme_tokens()
        # The theme's card colours for the rail: this is a window of its own,
        # without the main window's background behind it.
        self.setObjectName("graphicsOverlay")
        self.setStyleSheet(
            "QWidget#graphicsOverlay, QStackedWidget { background: transparent; }"
            f"QWidget#graphicsRail {{ background-color: {tokens['card']}; border: 1px solid {tokens['border']};"
            " border-radius: 10px; }"
        )
        rail = QWidget()
        rail.setObjectName("graphicsRail")
        rail.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(10, 16, 10, 16)
        rail_layout.setSpacing(8)
        self.pages = QStackedWidget()
        self.tabs = QButtonGroup(self)
        for index, (kind, name, card) in enumerate((
            ("mangohud", tr("Performance overlay"), self.panel.mangohud_card),
            ("vkbasalt", tr("Image effects"), self.panel.vkbasalt_card),
            ("framegen", tr("Frame generation"), self.frame_gen),
        )):
            icon = QIcon()
            icon.addPixmap(_tab_icon(kind, tokens.get("text_dim", "#7f8f78")), QIcon.Mode.Normal, QIcon.State.Off)
            icon.addPixmap(_tab_icon(kind, tokens.get("accent", "#9fe96f")), QIcon.Mode.Normal, QIcon.State.On)
            tab = QPushButton()
            tab.setObjectName("segment")
            tab.setCheckable(True)
            tab.setIcon(icon)
            tab.setIconSize(QSize(28, 28))
            tab.setFixedSize(56, 56)
            tab.setToolTip(name)
            tab.setCursor(Qt.CursorShape.PointingHandCursor)
            self.tabs.addButton(tab, index)
            rail_layout.addWidget(tab)
            self.pages.addWidget(_page(card))
        self.tabs.idClicked.connect(self.pages.setCurrentIndex)
        self.tabs.button(0).setChecked(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 16, 0, 0)  # the rail and the card share this top
        layout.setSpacing(0)
        layout.addWidget(rail, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.pages, 1)
        self._rail = rail
        # App-wide: a focused combo box or slider would take the key first.
        QApplication.instance().installEventFilter(self)
        # Without focus, clicking back into the game is seen by watching the
        # mouse buttons (X11 only).
        self._pressed = False
        self._pointer = QTimer(self)
        self._pointer.setInterval(50)
        self._pointer.timeout.connect(self._poll_pointer)

    def set_options(self, text: str) -> None:
        """The launch options text, as COMMANDER has it."""
        self._options = text
        self.frame_gen.sync()

    def _set_options(self, text: str) -> None:
        self.set_options(text)
        self.options_changed.emit(text)

    def eventFilter(self, watched, event) -> bool:
        # Esc closes it when it has focus after all (Wayland, a dialog); the
        # overlay key reaches COMMANDER's grab instead.
        if (event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape
                and self.isVisible() and self.isActiveWindow()):
            self.hide()
            return True
        return False

    def _poll_pointer(self) -> None:
        pointer = _pointer()
        if pointer is None:
            self._pointer.stop()
            return
        x, y, pressed = pointer
        ratio = self.devicePixelRatioF() or 1.0
        self.on_pointer(QPointF(x / ratio, y / ratio), pressed)

    def on_pointer(self, position: QPointF, pressed: bool) -> None:
        """A click outside, in the game, closes it. Only a new press: a
        slider dragged past the edge keeps it open, as does a dropdown's list
        or the Choose DLL dialog."""
        clicked = pressed and not self._pressed
        self._pressed = pressed
        if (
            clicked
            and not self.frameGeometry().contains(position.toPoint())
            and not (QApplication.activePopupWidget() or QApplication.activeModalWidget())
        ):
            self.hide()

    def hideEvent(self, event) -> None:
        self._pointer.stop()
        super().hideEvent(event)

    def _dock(self) -> None:
        """Full height down the left of the screen the mouse is on: the game's."""
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.geometry()
        self.setGeometry(area.x(), area.y(), min(self._width(), area.width()), area.height())

    def _width(self) -> int:
        """_WIDTH, or wider when a page needs it (a bigger font, a longer
        language): the card shows whole, with no scroll bar along the bottom."""
        pages = [self.pages.widget(i) for i in range(self.pages.count())]
        card = max(page.widget().minimumSizeHint().width() for page in pages)
        bar = pages[0].verticalScrollBar().sizeHint().width()
        return max(_WIDTH, self.layout().contentsMargins().left() + self._rail.sizeHint().width() + card + bar)

    def toggle(self, *, focus: bool = True) -> None:
        """Show it, or hide it. With focus, the game stops reading the mouse
        and keyboard (they're the overlay's then), as with alt-tab: it is the
        foreground window that gets them. Without, for a game in fullscreen:
        that minimizes itself when it loses focus, so the overlay leaves it
        alone, and clicks there reach the game too."""
        if self.isVisible():
            self.hide()
            return
        focus = focus and not self._fullscreen
        self.panel.render()  # Settings may have changed it meanwhile
        self.frame_gen.sync()
        self._dock()
        # Without focus, a click mustn't take it either: changed while hidden.
        # Out of the window manager's hands too (as the key hint): KWin
        # activates a window clicked even when it takes no focus, and the
        # game minimizes all the same.
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, not focus)
        self.setWindowFlag(Qt.WindowType.BypassWindowManagerHint, not focus)
        self._follow_game()
        # Unseen until its first frame is drawn: before that, the window
        # manager shows the whole full-height window black.
        self.setWindowOpacity(0.0)
        self.show()
        # ponytail: fixed wait for the first frame, hook the compositor's frame callback if it still flashes
        QTimer.singleShot(80, lambda: self.setWindowOpacity(0.94))
        _skip_taskbar(int(self.winId()))
        # KWin keeps a new window out of the taskbar's strip; a move as from
        # a pager isn't, so the panel starts at the very top.
        ratio = self.devicePixelRatioF() or 1.0
        _move_resize(int(self.winId()), *(round(v * ratio) for v in self.geometry().getRect()))
        self.raise_()
        if on_umbriel():
            QTimer.singleShot(100, _umbriel_dock)
        if focus:
            self.activateWindow()
            QTimer.singleShot(100, lambda: self._check_minimized(5))
            if on_umbriel() and not gui_settings.load_gui_settings().get("umbriel_rule_offered"):
                gui_settings.save_gui_settings(umbriel_rule_offered=True)
                QTimer.singleShot(300, lambda: offer_umbriel_rule(self))
        self._pressed = True  # the click that opened it, if any, isn't one outside
        self._pointer.start()

    def _check_minimized(self, tries: int) -> None:
        """A game set to fullscreen in its menu after launch minimizes when
        the panel takes focus. Then it's brought back and the panel shows
        over it without focus, as for a fullscreen game from the start."""
        if not self.isVisible() or self._game is None:
            return
        game = int(self._game.winId())
        if _minimized(game):
            self._fullscreen = True
            self.hide()
            _activate(game)
            # ponytail: fixed wait for the game to come back, poll its state if 0.5 s is short somewhere
            QTimer.singleShot(500, lambda: self.isVisible() or self.toggle(focus=False))
        elif tries:
            QTimer.singleShot(100, lambda: self._check_minimized(tries - 1))

    def _follow_game(self) -> None:
        """Made a transient of the window that has focus, the game's (the key
        was pressed in it). KWin keeps a fullscreen window above the taskbar
        only while it or a transient of it has focus: otherwise taking focus
        would bring the taskbar up over the game."""
        game = _active_window()
        self.winId()  # the native window, for windowHandle()
        if game and game != int(self.winId()):
            if self._game is None or int(self._game.winId()) != game:
                self._game = QWindow.fromWinId(game)
            self.windowHandle().setTransientParent(self._game)
        else:
            self.windowHandle().setTransientParent(None)


_WIDTH = 680

#: The panel's window title. Not translated, so one window rule matches it
#: in every language.
TITLE = "Graphics overlay - STALKER COMMANDER"

#: Umbriel (a scrolling Wayland compositor) places XWayland windows itself,
#: in the middle of the screen. This rule, in its config.toml, docks the
#: panel down the left at full height as on other desktops.
UMBRIEL_RULE = f"""[[window_rule]]
match.title = "^{TITLE}$"
default_floating = true
default_floating_size_px = {{ width = {_WIDTH} }}
default_floating_size = {{ height = 1.0 }}
default_position = {{ x = 0, y = 0, anchor = "top_left" }}
"""


def on_umbriel() -> bool:
    return bool(os.environ.get("UMBRIEL_SOCKET"))


def _umbriel_dock() -> None:
    """Moves the panel to the top-left over Umbriel's socket: without the
    rule it ignores where an X11 window asks to go. Its height can't be set
    this way (Umbriel resizes only the focused window), hence the rule.
    ponytail: the windows reply's shape is guessed from Umbriel 0.1's docs, check it on a real Umbriel"""
    import socket

    def ask(command: dict):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(0.5)
            sock.connect(os.environ["UMBRIEL_SOCKET"])
            sock.sendall(json.dumps(command).encode() + b"\n")
            return json.loads(sock.makefile("rb").readline())

    try:
        windows = ask({"cmd": "windows"}).get("ok")
        if isinstance(windows, dict):
            windows = windows.get("windows", [])
        for window in windows or []:
            if isinstance(window, dict) and window.get("title") == TITLE and "id" in window:
                ask({"cmd": "msg", "arg": f"window-move-to:top_left/0/0/{window['id']}"})
    except (OSError, ValueError, AttributeError, TypeError):
        pass  # nice to have: the rule docks it all the same


def _umbriel_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "umbriel" / "config.toml"


def offer_umbriel_rule(parent: QWidget | None) -> None:
    """The rule to paste into Umbriel's config, with a Copy rule button.
    Shown once, the first time the overlay opens on Umbriel, and from
    Settings > Graphics. COMMANDER never edits that file itself."""
    from .dialogs import MessageBox

    config = _umbriel_config()
    box = MessageBox(
        MessageBox.Icon.Information,
        tr("Umbriel needs a window rule"),
        tr("Umbriel opens the overlay in the middle of the screen. Add this rule to Umbriel's "
           "settings and it sits on the left at full height, as on other desktops."),
        parent=parent,
    )
    box.points = (
        tr("Press Copy rule."),
        tr("Open {path}, paste the rule at the end and save. Umbriel applies it right away.",
           path=str(config).replace(str(Path.home()), "~", 1)),
    )
    box.setInformativeText(UMBRIEL_RULE.strip() + "\n\n" + tr("You can copy it again in Settings > Graphics."))
    if not config.is_file():
        # Umbriel reads only the first config it finds: a new one with just
        # the rule would drop its defaults (keybinds and all).
        box.note = tr("You don't have this file yet. Copy Umbriel's own config.toml there first "
                      "(from /usr/share/umbriel), or Umbriel loses its default settings.")
    copy = box.addButton(tr("Copy rule"), MessageBox.ButtonRole.AcceptRole)
    box.addButton(MessageBox.StandardButton.Close)
    box.exec()
    if box.clickedButton() is copy:
        QGuiApplication.clipboard().setText(UMBRIEL_RULE)


class _KeyHint(QWidget):
    """"Press Shift+` to open COMMANDER overlay" in the bottom-left corner
    for a few seconds once the game is up, as Steam's overlay does. Never
    takes focus or clicks: the game keeps both.

    A tooltip window: on X11 the window manager leaves it alone
    (override-redirect), so it shows above a fullscreen game too, which
    KWin otherwise keeps above every "stay on top" window while it has focus."""

    SHOWN_MS = 10_000

    def __init__(self) -> None:
        from html import escape

        from ..themes import active_theme_tokens

        super().__init__(
            None,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(0.94)
        tokens = active_theme_tokens()
        key = escape(tr(KEYS[key_setting()][0]))
        self._key_text = tr("Press {key} to open COMMANDER overlay").format(
            key=f"<b style='color: {tokens.get('accent', '#9fe96f')}'>{key}</b>")
        self.label = QLabel(self._key_text)
        self.label.setObjectName("keyHint")
        self.label.setStyleSheet(
            f"QLabel#keyHint {{ background-color: {tokens['card']}; border: 1px solid {tokens['border']};"
            " border-radius: 10px; padding: 12px 18px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        self._timer = QTimer(self, singleShot=True, interval=self.SHOWN_MS, timeout=self.hide)

    def show_hint(self, text: str | None = None, shown_ms: int = SHOWN_MS) -> None:
        """The key hint, or ``text`` in its place."""
        self.label.setText(text or self._key_text)
        self.adjustSize()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.geometry()
        self.move(area.x() + 24, area.bottom() - self.height() - 24)
        self.show()
        self.raise_()
        self._timer.start(shown_ms)


class _ToggleWatch:
    """Shows "Image effects on/off" (vkBasalt) or "Performance overlay
    on/off" (MangoHud) in the hint's place when that one goes on or off.
    The bundled layers write their state to a file for this
    (packaging/*-state-file.patch); the first state is the game starting,
    so no pop-up for it."""

    def __init__(self, hint: _KeyHint, which: str) -> None:
        from ..overlays import state_file

        name, self._texts = {
            "hud": ("mangohud", (tr("Performance overlay off"), tr("Performance overlay on"))),
            "effects": ("vkbasalt", (tr("Image effects off"), tr("Image effects on"))),
        }[which]
        self._path = state_file(name)
        self._hint = hint
        self._state = None
        self._timer = QTimer(interval=50, timeout=self._poll)
        self._timer.start()

    def _poll(self) -> None:
        try:
            state = self._path.read_text().strip()
        except OSError:
            return
        if state not in ("0", "1"):  # caught mid-write
            return
        if self._state is not None and state != self._state:
            self._hint.show_hint(self._texts[state == "1"], 2000)
        self._state = state


def _page(card: QWidget) -> QScrollArea:
    """One tab's page: its card at the top, scrolling when it's taller."""
    content = QWidget()
    content.setObjectName("pageContent")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(12, 0, 16, 16)  # the top: the window's, as the rail's
    layout.addWidget(card)
    layout.addStretch(1)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setWidget(content)
    return scroll


# Xlib's default error handler exits the process: another app holding the
# key (BadAccess) must not take COMMANDER down. Module-level, so it outlives
# any grab (Xlib keeps calling it).
_IGNORE_X_ERRORS = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda _d, _e: 0)


@functools.cache
def _xlib():
    """libX11, its functions typed, with the error handler above."""
    x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XStringToKeysym.restype = ctypes.c_ulong
    x.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x.XKeysymToKeycode.restype = ctypes.c_ubyte
    x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x.XGrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int]
    x.XUngrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong]
    x.XPending.argtypes = [ctypes.c_void_p]
    x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    window, number, mask = ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint)
    x.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong, window, window, number, number, number, number, mask]
    x.XInternAtom.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p]
    x.XFlush.argtypes = [ctypes.c_void_p]
    x.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int,
        ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p),
    ]
    x.XFree.argtypes = [ctypes.c_void_p]
    x.XSetErrorHandler(_IGNORE_X_ERRORS)
    return x


class _ClientMessage(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int), ("data", ctypes.c_long * 5),
    ]


def _skip_taskbar(window: int) -> None:
    """Asks the window manager to leave the window out of the taskbar and
    the switcher (_NET_WM_STATE, after it's mapped). X11 only."""
    display = _pointer_display()
    if display is None:
        return
    x = _xlib()
    state = x.XInternAtom(display, b"_NET_WM_STATE", 0)
    for name in (b"_NET_WM_STATE_SKIP_TASKBAR", b"_NET_WM_STATE_SKIP_PAGER"):
        event = _ClientMessage(type=33, send_event=1, display=display, window=window, message_type=state, format=32)
        event.data[0], event.data[1], event.data[3] = 1, x.XInternAtom(display, name, 0), 1  # add; from an app
        root = x.XDefaultRootWindow(display)
        x.XSendEvent(display, root, 0, 1 << 19 | 1 << 20, ctypes.byref(event))  # Substructure(Redirect|Notify)
    x.XFlush(display)


def _active_window() -> int | None:
    """The X window that has focus (_NET_ACTIVE_WINDOW); None without X or
    when it's a Wayland window."""
    display = _pointer_display()
    if display is None:
        return None
    x = _xlib()
    kind, size, count, rest, data = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
    atom = x.XInternAtom(display, b"_NET_ACTIVE_WINDOW", 0)
    # XA_WINDOW (33), one 32-bit item (a long in Xlib's buffer).
    if x.XGetWindowProperty(display, x.XDefaultRootWindow(display), atom, 0, 1, 0, 33,
                            kind, size, count, rest, data) != 0 or not data.value:
        return None
    window = ctypes.c_ulong.from_address(data.value).value if count.value else 0
    x.XFree(data)
    return window or None


def _minimized(window: int) -> bool:
    """Whether the window manager has the window minimized
    (_NET_WM_STATE_HIDDEN). X11 only."""
    display = _pointer_display()
    if display is None:
        return False
    x = _xlib()
    kind, size, count, rest, data = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
    # XA_ATOM (4), up to 32 of them.
    if x.XGetWindowProperty(display, window, x.XInternAtom(display, b"_NET_WM_STATE", 0), 0, 32, 0, 4,
                            kind, size, count, rest, data) != 0 or not data.value:
        return False
    atoms = (ctypes.c_ulong * count.value).from_address(data.value)[:]
    x.XFree(data)
    return x.XInternAtom(display, b"_NET_WM_STATE_HIDDEN", 0) in atoms


def _activate(window: int) -> None:
    """_NET_ACTIVE_WINDOW, sent as a pager would: the window manager brings
    the window back from minimized and gives it focus. X11 only."""
    display = _pointer_display()
    if display is None:
        return
    x = _xlib()
    event = _ClientMessage(type=33, send_event=1, display=display, window=window,
                           message_type=x.XInternAtom(display, b"_NET_ACTIVE_WINDOW", 0), format=32)
    event.data[0] = 2  # from a pager; time 0 (CurrentTime)
    x.XSendEvent(display, x.XDefaultRootWindow(display), 0, 1 << 19 | 1 << 20, ctypes.byref(event))
    x.XFlush(display)


def _move_resize(window: int, left: int, top: int, width: int, height: int) -> None:
    """_NET_MOVERESIZE_WINDOW, sent as a pager would: the window manager
    places it there even over a panel's reserved strip. X11 only."""
    display = _pointer_display()
    if display is None:
        return
    x = _xlib()
    event = _ClientMessage(type=33, send_event=1, display=display, window=window,
                           message_type=x.XInternAtom(display, b"_NET_MOVERESIZE_WINDOW", 0), format=32)
    # NorthWest gravity (1), with x, y, width and height (bits 8-11), from a pager (2).
    event.data[0] = 1 | 0xF << 8 | 2 << 12
    event.data[1], event.data[2], event.data[3], event.data[4] = left, top, width, height
    x.XSendEvent(display, x.XDefaultRootWindow(display), 0, 1 << 19 | 1 << 20, ctypes.byref(event))
    x.XFlush(display)


@functools.cache
def _pointer_display():
    if not os.environ.get("DISPLAY") or QGuiApplication.platformName() != "xcb":
        return None
    return _xlib().XOpenDisplay(None) or None


def _pointer() -> tuple[int, int, bool] | None:
    """The mouse on the screen, in X pixels, and whether a button is down
    (left or right); None without X."""
    display = _pointer_display()
    if display is None:
        return None
    root, child = ctypes.c_ulong(), ctypes.c_ulong()
    x, y, wx, wy, mask = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_uint()
    _xlib().XQueryPointer(display, _xlib().XDefaultRootWindow(display), root, child, x, y, wx, wy, mask)
    return x.value, y.value, bool(mask.value & (1 << 8 | 1 << 10))  # Button1Mask, Button3Mask


class _KeyGrab:
    """A key grabbed from the X server, polled from the Qt event loop."""

    _LOCKS = (0, 2, 16, 2 | 16)  # with Caps Lock and Num Lock on too

    def __init__(self, keysym: str, modifiers: int, on_press) -> None:
        self._x = x = _xlib()

        self._display = x.XOpenDisplay(None)
        if not self._display:
            raise OSError("no X display")
        self._root = x.XDefaultRootWindow(self._display)
        self._keycode = x.XKeysymToKeycode(self._display, x.XStringToKeysym(keysym.encode()))
        self._modifiers = modifiers
        for lock in self._LOCKS:
            # GrabModeAsync (1) for pointer and keyboard: events keep flowing.
            x.XGrabKey(self._display, self._keycode, modifiers | lock, self._root, 0, 1, 1)
        x.XSync(self._display, 0)
        self._on_press = on_press
        self._last = 0.0
        self._event = ctypes.create_string_buffer(192)  # sizeof(XEvent)
        self._timer = QTimer()
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _poll(self) -> None:
        while self._x.XPending(self._display):
            self._x.XNextEvent(self._display, self._event)
            event_type = ctypes.c_int.from_buffer(self._event).value
            # KeyPress (2); a held key repeats, so one press per 0.3 s.
            if event_type == 2 and time.monotonic() - self._last > 0.3:
                self._last = time.monotonic()
                self._on_press()

    def close(self) -> None:
        self._timer.stop()
        for lock in self._LOCKS:
            self._x.XUngrabKey(self._display, self._keycode, self._modifiers | lock, self._root)
        self._x.XCloseDisplay(self._display)


_process: subprocess.Popen | None = None
_reader: QSocketNotifier | None = None
_grab: _KeyGrab | None = None
#: Waits for the game to be on screen, then shows the key hint (see start_key).
_hint_wait: QTimer | None = None
#: The Play page's launch options text: read for the Frame generation tab,
#: written when that tab changes it (see connect_options).
_get_options = None
_set_options = None
#: The Anomaly folder, for the game's screen mode, and the GAMMA folder:
#: both for the engine's log, which says when the game is up.
_get_anomaly = None
_get_gamma = None


def connect_options(get, set_, anomaly=None, gamma=None) -> None:
    global _get_options, _set_options, _get_anomaly, _get_gamma
    _get_options, _set_options, _get_anomaly, _get_gamma = get, set_, anomaly, gamma


def game_fullscreen(anomaly: str | Path | None) -> bool:
    """Whether Anomaly is set to (exclusive) fullscreen in its user.ltx,
    rather than borderless or a window."""
    if not anomaly:
        return False
    try:
        text = (Path(anomaly) / "appdata" / "user.ltx").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.split()[1:2] == ["fullscreen"] for line in text.splitlines() if line.startswith("rs_screenmode"))


def use_borderless(anomaly: str | Path | None) -> None:
    """Anomaly's fullscreen made borderless, so the overlay can take focus
    over it: fullscreen minimizes when it loses focus, and without focus the
    game reads the overlay's clicks. Under Proton the two look and run the
    same: there is no real mode change. Before launch: the game writes
    user.ltx when it quits."""
    from ..atomic import write_bytes

    if not anomaly or not game_fullscreen(anomaly):
        return
    path = Path(anomaly) / "appdata" / "user.ltx"
    data = path.read_bytes()
    lines = data.split(b"\n")
    lines = [b"rs_screenmode borderless" + line[len(b"rs_screenmode fullscreen"):]
             if line.startswith(b"rs_screenmode fullscreen") else line for line in lines]
    write_bytes(path, b"\n".join(lines), lock=False)


def _toggle_command() -> str:
    # Borderless or a window: the overlay takes focus, so the game stops
    # reading the mouse. Fullscreen would minimize, so not there.
    return "toggle" if game_fullscreen(_get_anomaly() if _get_anomaly else None) else "toggle focus"


def _read_reply() -> None:
    """A line from the panel: the options text its Frame generation tab set."""
    line = _process.stdout.readline()
    if not line:  # it quit
        _reader.setEnabled(False)
    elif line.startswith(b"options ") and _set_options:
        _set_options(json.loads(line[8:]))


def _send(command: str) -> bool:
    try:
        _process.stdin.write(f"{command}\n".encode())
        _process.stdin.flush()
        return True
    except (AttributeError, OSError):
        return False


def toggle() -> None:
    """Show or hide the panel, starting its process the first time (shown)."""
    options = f"options {json.dumps(_get_options() if _get_options else '')}"
    if not (_running() and _send(options) and _send(_toggle_command())):
        _start(shown=True)


def prestart() -> None:
    """Start the panel's process hidden, so the key opens it at once: Python
    and Qt starting is most of the wait. While the game runs, so it costs
    nothing otherwise."""
    if not _running():
        _start(shown=False)


def _running() -> bool:
    return _process is not None and _process.poll() is None


def _start(*, shown: bool) -> None:
    global _process, _reader
    options = f"options {json.dumps(_get_options() if _get_options else '')}"
    env = dict(os.environ)
    if env.get("WAYLAND_DISPLAY") and env.get("DISPLAY"):
        env["QT_QPA_PLATFORM"] = "xcb"
    # Its stdin closing (COMMANDER quit) ends it.
    _process = subprocess.Popen(
        [sys.executable, "-m", __name__, *([_toggle_command()] if shown else ["--hidden"])],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    _send(options)
    _reader = QSocketNotifier(_process.stdout.fileno(), QSocketNotifier.Type.Read)
    _reader.activated.connect(_read_reply)


def start_key() -> None:
    """Grab the overlay key: the game started."""
    global _grab
    stop_key()
    _label, keysym, modifiers = KEYS[key_setting()]
    # Steam Deck Game Mode: gamescope shows one window, so not this one.
    if in_game_mode() or not os.environ.get("DISPLAY"):
        return
    from ..overlays import MANGOHUD, VKBASALT

    options = _get_options().split() if _get_options else []
    for layer, which in ((VKBASALT, "effects"), (MANGOHUD, "hud")):
        if layer in options:
            prestart()
            _send(f"watch {which}")
    if not keysym:
        return
    prestart()
    try:
        _grab = _KeyGrab(keysym, modifiers, toggle)
    except OSError:
        _grab = None
        return
    _wait_for_game()


#: Seconds after the engine's "Starting engine...", for its first frame.
_HINT_DELAY_MS = 2000
#: A first start builds shaders, so well past the splash's three minutes.
_HINT_TIMEOUT_S = 600


def _wait_for_game() -> None:
    """Show the key hint once the engine logs it has started: the game's
    window is up then, not at launch (MO2 and the splash come first)."""
    global _hint_wait
    from .. import applog
    from .game_splash import engine_started

    anomaly = _get_anomaly() if _get_anomaly else None
    gamma = _get_gamma() if _get_gamma else None
    if not anomaly:
        return
    logs = [Path(anomaly) / "appdata" / "logs"]
    if gamma:
        logs.append(Path(gamma) / "overwrite" / "appdata" / "logs")
    since = time.time() - 1
    deadline = time.monotonic() + _HINT_TIMEOUT_S

    def check() -> None:
        if engine_started(logs, since):
            _hint_wait.stop()
            applog.get_logger().info("graphics overlay: game up, showing key hint")
            QTimer.singleShot(_HINT_DELAY_MS, lambda: _running() and _send("hint"))
        elif time.monotonic() > deadline:
            _hint_wait.stop()

    _hint_wait = QTimer(interval=1000, timeout=check)
    _hint_wait.start()


def stop_key() -> None:
    """Let the key go: the game ended. The panel's process goes with it."""
    global _grab
    if _hint_wait is not None:
        _hint_wait.stop()
    if _running():
        _reader.setEnabled(False)
        _process.terminate()
    if _grab is not None:
        _grab.close()
        _grab = None


def restart_key() -> None:
    """A new key picked in Settings: swap it in if the game runs."""
    if _grab is not None:
        start_key()


def _main() -> int:
    from ..fonts import load_bundled_font
    from ..i18n import set_active_language
    from ..themes import build_palette, build_stylesheet, set_active_theme

    # COMMANDER's window class and icon, as the game splash.
    app = QApplication(["stalker-gamma-commander"])
    app.setApplicationName("stalker-gamma-commander")
    app.setDesktopFileName("stalker-gamma-commander")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[2] / "cli" / "stalker-gamma.png")))
    load_bundled_font()
    state = gui_settings.load_gui_settings()
    theme = state.get("theme") or "gamma"
    try:
        font_size = int(state.get("font_size") or 13)
    except (TypeError, ValueError):
        font_size = 13
    set_active_language(state.get("language") or "en")
    set_active_theme(theme)
    app.setPalette(build_palette(theme))
    app.setStyleSheet(build_stylesheet(theme, font_size=font_size, font_family=state.get("font_family") or "Exo 2"))
    window = GraphicsWindow()
    hint = _KeyHint()
    watches = []
    window.options_changed.connect(lambda text: print(f"options {json.dumps(text)}", flush=True))
    if "--hidden" not in sys.argv:
        window.toggle(focus="toggle focus" in sys.argv)

    def read() -> None:
        line = sys.stdin.buffer.readline().strip()
        if not line:  # COMMANDER is gone
            app.quit()
        elif line.startswith(b"toggle"):
            hint.hide()
            window.toggle(focus=line == b"toggle focus")
        elif line == b"hint":
            hint.show_hint()
        elif line.startswith(b"watch "):
            watches.append(_ToggleWatch(hint, line[6:].decode()))
        elif line == b"hide":
            window.hide()
        elif line.startswith(b"options "):
            window.set_options(json.loads(line[8:]))

    notifier = QSocketNotifier(sys.stdin.fileno(), QSocketNotifier.Type.Read)
    notifier.activated.connect(read)
    return app.exec()


if __name__ == "__main__":
    sys.exit(_main())
