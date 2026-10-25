"""COMMANDER's own splash screen while a co-op game starts.

The engine's splash is a bitmap baked into its exe. xrRazom's engine can
switch it off (``-nosplashwindow``, read from the real command line only,
not commandline.txt); co-op launches pass that and show this one instead,
until the engine logs "Starting engine..." - the moment its own splash
would have closed. GAMMA's single-player engine has no such switch, so
single-player keeps the engine's splash.
"""

from __future__ import annotations

import itertools
import math
import os
import random
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication, QWidget

from .. import applog

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "game_splash"
SPLASH_FLAG = "-nosplashwindow"
#: Sent by COMMANDER when one of its pop-ups opens and once it closes.
POPUP_OPEN, POPUP_CLOSED = signal.SIGUSR1, signal.SIGUSR2
#: Logged by CRenderDevice::Run, right after the engine closes its splash;
#: or by a crash while loading, whose error box must not open under the splash
#: (a crash outside the engine's own checks logs only the stack trace).
MARKERS = (b"Starting engine...", b"FATAL ERROR", b"stack trace:")
#: Logical size; the bundled images are twice this for HiDPI screens.
WIDTH, HEIGHT, BAR = 960, 546, 34
TIMEOUT_MS = 180_000
POLL_MS = 500
RAIN_MS = 1100
#: Sideways slant of the falling tile, as in the README banner (114 per 520).
RAIN_DRIFT = HEIGHT * 114 / 520
#: Drops fade in below the top bar and out above the bottom one.
RAIN_FADE = 90
#: The engine reports no progress, only that it is done: the bar eases
#: towards 95% (63% of that after LOAD_TAU ms), then fills in FINISH_MS.
LOAD_TAU = 25_000
FINISH_MS = 300
#: Seconds between lightning strikes, and a strike's brightness over its
#: first ms: a flash, a flicker, a second flash, then a fade.
STRIKE_GAP = (5.0, 14.0)
STRIKE = ((0, 1.0), (70, 1.0), (90, 0.15), (150, 0.15), (170, 0.8), (230, 0.8), (650, 0.0))
#: The tree tops of the background, cut out to tree1-3.png (each cropped to
#: its box) and painted out of background.jpg, as (left, right, top, base,
#: sway px, sway ms, phase): each bends from the roofline behind it (base), up to its sway px at the
#: top, once per its sway ms, with a faster gust on top. Each tree has its
#: own swing, so they never move in step.
TREES = (
    (0, 128, 34, 300, 7.0, 4700, 0.0),
    (103, 232, 34, 300, 9.0, 3900, 2.3),
    (408, 523, 173, 275, 6.0, 3100, 4.1),
)
#: At most this share of the screen's width: full size on a 1920 px desktop,
#: 768 px of the Steam Deck's 1280, so the Deck's screen is not covered.
SCREEN_SHARE = 0.6
#: Then drawn at this share of that: 720 px of a large desktop, 576 px on the Deck.
SIZE = 0.75


def wants_splash(binary: str) -> bool:
    """Only the game itself (AnomalyDX11AVX.exe and friends) has a splash."""
    return Path(binary.replace("\\", "/")).name.lower().startswith("anomalydx")


def splash_arguments(exe) -> str | None:
    """An MO2 executable's own arguments plus SPLASH_FLAG, for a co-op launch
    of a game exe; None for anything else (the launcher, tools, no exe)."""
    if exe is None or not wants_splash(exe.binary):
        return None
    return f"{exe.arguments} {SPLASH_FLAG}".strip()


def is_launcher(binary: str) -> bool:
    """AnomalyLauncher.exe starts the game itself, when Play is pressed."""
    return Path(binary.replace("\\", "/")).name.lower().startswith("anomalylauncher")


def engine_started(log_dirs: list[Path], since: float) -> Path | None:
    """The log written after ``since`` that holds one of MARKERS, if any."""
    for folder in log_dirs:
        for log in folder.glob("*.log"):
            try:
                if log.stat().st_mtime >= since:
                    text = log.read_bytes()
                    if any(marker in text for marker in MARKERS):
                        return log
            except OSError:
                continue
    return None


def mo2_error(log: Path, since: float) -> list[str]:
    """MO2's error lines (it shows an error box for them) after ``since``.

    Lines start "[2026-10-17 14:18:53.421 E]" in local time; comparing by
    timestamp, not file offset, survives MO2 rotating the log on start.
    """
    stamp = time.strftime("[%Y-%m-%d %H:%M:%S", time.localtime(since))
    try:
        if log.stat().st_mtime < since:
            return []
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line for line in lines if line[24:27] == " E]" and line[:20] >= stamp]


class GameSplash(QWidget):
    """Shown from Play until the engine is up (or the launch ends)."""

    def __init__(
        self,
        anomaly: str | Path,
        gamma: str | Path,
        accent: str,
        label: str,
        *,
        on_top: bool = True,
        still: bool = False,
    ) -> None:
        # Tool, not SplashScreen: KWin does not keep splash windows above.
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
        if on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)
        self._on_top = on_top
        #: Animations off: a still picture, only the loading bar moves.
        self._still = still
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self._scale = min(1.0, screen.width() * SCREEN_SHARE / WIDTH) * SIZE
        self.setFixedSize(round(WIDTH * self._scale), round(HEIGHT * self._scale))
        self._background = QPixmap(str(ASSETS / "background.jpg"))
        self._logo = QPixmap(str(ASSETS / "logo.png"))
        self._trees = [QPixmap(str(ASSETS / f"tree{i}.png")) for i in range(1, len(TREES) + 1)]
        for pixmap in (self._background, self._logo, *self._trees):
            pixmap.setDevicePixelRatio(2)
        self._accent = QColor(accent)
        self._label = label
        # Rain as in docs/readme/banner.svg: thin slanted streaks, one
        # fixed random tile that falls its own height every RAIN_MS.
        rng = random.Random(7)
        self._drops = [
            (rng.uniform(-60, WIDTH), rng.uniform(0, HEIGHT), rng.uniform(10, 24))
            for _ in range(250)
        ]
        self._clock = QElapsedTimer()
        self._clock.start()
        self._rng = random.Random()
        self._strike_at = 3000
        self._bolt = self._new_bolt()
        #: (elapsed ms, bar progress) once the engine is up.
        self._done: tuple[int, float] | None = None
        # The engine logs to appdata\logs; MO2 sends a log that does not
        # exist yet to overwrite instead.
        self._logs = [
            Path(anomaly) / "appdata" / "logs",
            Path(gamma) / "overwrite" / "appdata" / "logs",
        ]
        self._mo2_log = Path(gamma) / "logs" / "mo_interface.log"
        self._since = time.time() - 1
        self._frame = QTimer(self, interval=1000 if still else 33, timeout=self.update)
        self._frame.start()
        self._poll = QTimer(self, interval=POLL_MS, timeout=self._check)
        self._poll.start()
        QTimer.singleShot(TIMEOUT_MS, self, self.close)

    def stay_on_top(self, on: bool) -> None:
        """Off while a COMMANDER pop-up is open: that pop-up, a Wayland
        window, can never go above an XWayland one kept on top."""
        handle = self.windowHandle()
        if not self._on_top or handle is None:
            return
        handle.setFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        # Still above COMMANDER's window, under the pop-up it raises next.
        self.raise_()

    def show(self) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.move(screen.center() - self.rect().center())
        super().show()

    def _check(self) -> None:
        log = engine_started(self._logs, self._since)
        if log is not None:
            applog.get_logger().info("splash: engine started (%s)", log)
            self._poll.stop()
            now = self._clock.elapsed()
            self._done = (now, self._progress(now))
            QTimer.singleShot(FINISH_MS, self, self.close)
        elif errors := mo2_error(self._mo2_log, self._since):
            # MO2's error box would open under this always-on-top window.
            applog.get_logger().warning("splash: MO2 reported an error:\n  %s", "\n  ".join(errors[-20:]))
            self.close()

    def _progress(self, now: int) -> float:
        if self._done is None:
            return 0.95 * (1 - math.exp(-now / LOAD_TAU))
        start, progress = self._done
        return progress + (1 - progress) * min(1.0, (now - start) / FINISH_MS)

    def _new_bolt(self) -> list[QPolygonF]:
        """A jagged bolt from the clouds that ends above the roofline, and a fork."""
        rng = self._rng
        x, y = rng.uniform(300, 880), float(BAR)
        lean = rng.uniform(-4, 4)
        trunk = [QPointF(x, y)]
        while y < 200:
            x += lean + rng.uniform(-16, 16)
            y += rng.uniform(12, 24)
            trunk.append(QPointF(x, y))
        fork = trunk[rng.randrange(2, len(trunk) - 2)]
        x, y, side = fork.x(), fork.y(), rng.choice((-1, 1))
        branch = [QPointF(fork)]
        for _ in range(4):
            x += side * rng.uniform(6, 18)
            y += rng.uniform(8, 16)
            branch.append(QPointF(x, y))
        return [QPolygonF(trunk), QPolygonF(branch)]

    def _lightning(self, now: int) -> float:
        """The current strike's brightness, 0 between strikes."""
        t = now - self._strike_at
        if t >= STRIKE[-1][0]:
            self._strike_at = now + round(self._rng.uniform(*STRIKE_GAP) * 1000)
            self._bolt = self._new_bolt()
            return 0.0
        for (t0, a), (t1, b) in itertools.pairwise(STRIKE):
            if t0 <= t < t1:
                return a + (b - a) * (t - t0) / (t1 - t0)
        return 0.0

    def _draw_trees(self, painter: QPainter, now: int) -> None:
        """The tree tops, in strips that lean further the higher they are."""
        step = 3
        for (left, right, top, base, sway, period, phase), tree in zip(TREES, self._trees):
            turn = 2 * math.pi * now / period + phase
            lean = sway * (0.75 * math.sin(turn) + 0.25 * math.sin(2.7 * turn + phase))
            for y in range(top, base, step):
                dx = lean * ((base - y) / (base - top)) ** 2
                # One row taller than the step: scaled down, a strip's edges
                # are antialiased, and abutting strips let the sky show through.
                height = min(step + 1, base - y)
                painter.drawPixmap(
                    QRectF(left + dx, y, right - left, height),
                    tree,
                    QRectF(0, (y - top) * 2, (right - left) * 2, height * 2),
                )

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # Laid out at WIDTH x HEIGHT, drawn at the window's size.
        painter.scale(self._scale, self._scale)
        now = self._clock.elapsed()
        fx = 0 if self._still else now
        painter.drawPixmap(0, 0, self._background)
        flash = 0.0 if self._still else self._lightning(now)
        if flash > 0:
            painter.save()
            painter.setClipRect(0, BAR, WIDTH, HEIGHT - 2 * BAR)
            # Lit in proportion to its own brightness: the sky flares.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            painter.setOpacity(0.55 * flash)
            painter.drawPixmap(0, 0, self._background)
            painter.setOpacity(1.0)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            for width, alpha in ((7, 0.12), (1.6, 0.9)):
                glow = QColor("#e4ecff")
                glow.setAlphaF(alpha * flash)
                painter.setPen(QPen(
                    glow, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin
                ))
                for line in self._bolt:
                    painter.drawPolyline(line)
            painter.restore()
        # Over the flash and the bolt, so they stay dark against both.
        self._draw_trees(painter, fx)
        painter.save()
        painter.setClipRect(0, BAR, WIDTH, HEIGHT - 2 * BAR)
        rain = QColor("#b9c9bd")
        pen = QPen(rain, 1)
        # Cycles since start, not their fraction: the drift must keep
        # going, or the whole tile snaps back sideways every RAIN_MS.
        cycles = fx / RAIN_MS
        span = WIDTH + 60
        for x, y, length in self._drops:
            top = (y + cycles * HEIGHT) % HEIGHT
            fade = min(top - BAR, HEIGHT - BAR - top - length) / RAIN_FADE
            if fade <= 0:
                continue
            left = (x + 60 + cycles * RAIN_DRIFT) % span - 60
            rain.setAlphaF(0.22 * min(fade, 1.0))
            pen.setColor(rain)
            painter.setPen(pen)
            painter.drawLine(QPointF(left, top), QPointF(left + length * 0.22, top + length))
        painter.restore()
        logo = self._logo.deviceIndependentSize()
        painter.drawPixmap(
            QPointF(24, HEIGHT - BAR - 10 - logo.height()), self._logo
        )
        track = QColor(self._accent)
        track.setAlphaF(0.25)
        painter.fillRect(QRect(0, HEIGHT - BAR, WIDTH, 1), track)
        painter.fillRect(QRectF(0, HEIGHT - BAR - 1, WIDTH * self._progress(now), 2), self._accent)
        font = QFont(painter.font())
        font.setPixelSize(12)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        painter.setFont(font)
        painter.setPen(self._accent)
        painter.drawText(
            QRect(0, HEIGHT - BAR, WIDTH - 36, BAR),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            self._label,
        )

    def closeEvent(self, event) -> None:
        self._poll.stop()
        self._frame.stop()
        super().closeEvent(event)


def start(anomaly: str | Path, gamma: str | Path, *, on_top: bool = True) -> subprocess.Popen:
    """Show the splash in a process of its own; terminate it to close early.

    On Wayland no window can keep itself above others, so the splash runs
    through XWayland, where the window manager honours "stay on top" - the
    MO2 and Steam windows that open meanwhile come up underneath it.
    ``on_top=False`` is for the Anomaly launcher, which opens first and
    where the player still has to press Play: it opens in front.
    """
    # Here, not at the top: the splash's own process needs neither, and the
    # translations alone add 15 MB and 30 ms to its start.
    from ..gui_settings import animations_enabled
    from ..i18n import tr
    from ..themes import active_theme_tokens

    env = dict(os.environ)
    if env.get("WAYLAND_DISPLAY") and env.get("DISPLAY"):
        env["QT_QPA_PLATFORM"] = "xcb"
    accent = active_theme_tokens().get("accent", "#9fe96f")
    # The splash inherits "ignore" for the pop-up signals until it sets its
    # own handlers: their default action would end it while Python loads.
    # (signal.signal works only on the main thread.)
    signals = (POPUP_OPEN, POPUP_CLOSED) if threading.current_thread() is threading.main_thread() else ()
    previous = [signal.signal(s, signal.SIG_IGN) for s in signals]
    try:
        return subprocess.Popen(
            [
                sys.executable, "-m", __name__, str(anomaly), str(gamma), accent,
                tr("Loading").upper(), tr("Loading xrRAZOM"), *([] if on_top else ["--below"]),
                *([] if animations_enabled() else ["--still"]),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        for s, handler in zip(signals, previous, strict=True):
            signal.signal(s, handler)

if __name__ == "__main__":
    splash = None
    popup_open = False

    def _on_popup(signum, _frame) -> None:
        # Can come before the splash is shown: applied once it is.
        global popup_open
        popup_open = signum == POPUP_OPEN
        if splash is not None:
            splash.stay_on_top(not popup_open)

    # Python runs these between the frame timer's ticks.
    signal.signal(POPUP_OPEN, _on_popup)
    signal.signal(POPUP_CLOSED, _on_popup)
    # COMMANDER's window class, desktop file and icon: the taskbar groups
    # it with COMMANDER (StartupWMClass in its .desktop file).
    app = QApplication(["stalker-gamma-commander"])
    app.setApplicationName("stalker-gamma-commander")
    app.setDesktopFileName("stalker-gamma-commander")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[2] / "cli" / "stalker-gamma.png")))
    flags = sys.argv[6:]
    splash = GameSplash(*sys.argv[1:5], on_top="--below" not in flags, still="--still" in flags)
    splash.setWindowTitle(sys.argv[5])
    # A Tool window never counts as the last one closed, so closing it
    # (game started, timeout) wouldn't end this process: quit with it.
    splash.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    splash.destroyed.connect(app.quit)
    splash.show()
    if popup_open:
        splash.stay_on_top(False)
    sys.exit(app.exec())
