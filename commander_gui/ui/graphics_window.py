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
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
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
#: Control 4. Plain ` opens Anomaly's console, so not that.
KEYS = {
    "shift+grave": ("Shift+`", "grave", 1),
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
        # A frame time graph in a screen.
        painter.drawRoundedRect(QRectF(3, 5, 22, 18), 3, 3)
        painter.drawPolyline([QPointF(7, 18), QPointF(11, 13), QPointF(14, 16), QPointF(18, 9), QPointF(21, 12)])
    elif kind == "vkbasalt":
        # Colour: a circle half filled, like a contrast control.
        painter.drawEllipse(QPointF(14, 14), 10, 10)
        half = QPainterPath()
        half.moveTo(14, 4)
        half.arcTo(QRectF(4, 4, 20, 20), 90, 180)
        half.closeSubpath()
        painter.fillPath(half, QColor(colour))
    else:
        # Frame generation: frames stacked behind the one shown.
        painter.drawRoundedRect(QRectF(10, 4, 15, 12), 2, 2)
        painter.drawRoundedRect(QRectF(6.5, 8, 15, 12), 2, 2)
        painter.setBrush(QColor(colour))
        painter.drawRoundedRect(QRectF(3, 12, 15, 12), 2, 2)
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
        self.setWindowTitle(f"{tr('Graphics overlay')} - STALKER COMMANDER")
        self.setWindowOpacity(0.94)  # the game shows through a little
        self.resize(_WIDTH, 860)
        self._options = ""
        # Keeps the cards' handlers: the cards themselves go on the tabs.
        self.panel = GraphicsPanel(loose=True)
        self.frame_gen = FrameGenCard(lambda: self._options, self._set_options)

        tokens = active_theme_tokens()
        # The theme's own window and card colours: this is a window of its
        # own, without the main window's background behind it.
        self.setObjectName("graphicsOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(
            f"QWidget#graphicsOverlay {{ background-color: {tokens['bg']}; }}"
            f"QWidget#graphicsRail {{ background-color: {tokens['card']}; border-right: 1px solid {tokens['border']}; }}"
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
            ("mangohud", "MangoHud", self.panel.mangohud_card),
            ("vkbasalt", "vkBasalt", self.panel.vkbasalt_card),
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
        rail_layout.addStretch(1)
        self.tabs.idClicked.connect(self.pages.setCurrentIndex)
        self.tabs.button(0).setChecked(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(rail)
        layout.addWidget(self.pages, 1)
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
        self.setGeometry(area.x(), area.y(), min(_WIDTH, area.width()), area.height())

    def toggle(self, *, focus: bool = True) -> None:
        """Show it, or hide it. With focus, the game stops reading the mouse
        and keyboard (they're the overlay's then), as with alt-tab: it is the
        foreground window that gets them. Without, for a game in fullscreen:
        that minimizes itself when it loses focus, so the overlay leaves it
        alone, and clicks there reach the game too."""
        if self.isVisible():
            self.hide()
            return
        self.panel.render()  # Settings may have changed it meanwhile
        self.frame_gen.sync()
        self._dock()
        # Without focus, a click mustn't take it either: changed while hidden.
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, not focus)
        self.show()
        _skip_taskbar(int(self.winId()))
        self.raise_()
        if focus:
            self.activateWindow()
        self._pressed = True  # the click that opened it, if any, isn't one outside
        self._pointer.start()


_WIDTH = 680


def _page(card: QWidget) -> QScrollArea:
    """One tab's page: its card at the top, scrolling when it's taller."""
    content = QWidget()
    content.setObjectName("pageContent")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(12, 16, 16, 16)
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
#: The Play page's launch options text: read for the Frame generation tab,
#: written when that tab changes it (see connect_options).
_get_options = None
_set_options = None
#: The Anomaly folder, for the game's screen mode (see connect_options).
_get_anomaly = None


def connect_options(get, set_, anomaly=None) -> None:
    global _get_options, _set_options, _get_anomaly
    _get_options, _set_options, _get_anomaly = get, set_, anomaly


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
    if not keysym or in_game_mode() or not os.environ.get("DISPLAY"):
        return
    prestart()
    try:
        _grab = _KeyGrab(keysym, modifiers, toggle)
    except OSError:
        _grab = None


def stop_key() -> None:
    """Let the key go: the game ended. The panel's process goes with it."""
    global _grab
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
    window.options_changed.connect(lambda text: print(f"options {json.dumps(text)}", flush=True))
    if "--hidden" not in sys.argv:
        window.toggle(focus="toggle focus" in sys.argv)

    def read() -> None:
        line = sys.stdin.buffer.readline().strip()
        if not line:  # COMMANDER is gone
            app.quit()
        elif line.startswith(b"toggle"):
            window.toggle(focus=line == b"toggle focus")
        elif line == b"hide":
            window.hide()
        elif line.startswith(b"options "):
            window.set_options(json.loads(line[8:]))

    notifier = QSocketNotifier(sys.stdin.fileno(), QSocketNotifier.Type.Read)
    notifier.activated.connect(read)
    return app.exec()


if __name__ == "__main__":
    sys.exit(_main())
