"""COMMANDER's own splash screen while a co-op game starts.

The engine's splash is a bitmap baked into its exe. xrRazom's engine can
switch it off (``-nosplashwindow``, read from the real command line only,
not commandline.txt); co-op launches pass that and show this one instead,
until the engine logs "Starting engine..." - the moment its own splash
would have closed. GAMMA's single-player engine has no such switch, so
single-player keeps the engine's splash.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from .. import applog
from ..i18n import tr
from ..themes import active_theme_tokens

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "game_splash"
SPLASH_FLAG = "-nosplashwindow"
#: Logged by CRenderDevice::Run, right after the engine closes its splash.
MARKER = b"Starting engine..."
#: Logical size; the bundled images are twice this for HiDPI screens.
WIDTH, HEIGHT, BAR = 960, 546, 34
TIMEOUT_MS = 180_000
POLL_MS = 500
RAIN_MS = 1100
#: Sideways slant of the falling tile, as in the README banner (114 per 520).
RAIN_DRIFT = HEIGHT * 114 / 520


def wants_splash(binary: str) -> bool:
    """Only the game itself (AnomalyDX11AVX.exe and friends) has a splash."""
    return Path(binary.replace("\\", "/")).name.lower().startswith("anomalydx")


def is_launcher(binary: str) -> bool:
    """AnomalyLauncher.exe starts the game itself, when Play is pressed."""
    return Path(binary.replace("\\", "/")).name.lower().startswith("anomalylauncher")


def engine_started(log_dirs: list[Path], since: float) -> Path | None:
    """The log written after ``since`` that holds MARKER, if any."""
    for folder in log_dirs:
        for log in folder.glob("*.log"):
            try:
                if log.stat().st_mtime >= since and MARKER in log.read_bytes():
                    return log
            except OSError:
                continue
    return None


class GameSplash(QWidget):
    """Shown from Play until the engine is up (or the launch ends)."""

    def __init__(self, anomaly: str | Path, gamma: str | Path, accent: str, label: str) -> None:
        super().__init__(
            None,
            # Tool, not SplashScreen: KWin does not keep splash windows above.
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setFixedSize(WIDTH, HEIGHT)
        self._background = QPixmap(str(ASSETS / "background.jpg"))
        self._logo = QPixmap(str(ASSETS / "logo.png"))
        for pixmap in (self._background, self._logo):
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
        # The engine logs to appdata\logs; MO2 sends a log that does not
        # exist yet to overwrite instead.
        self._logs = [
            Path(anomaly) / "appdata" / "logs",
            Path(gamma) / "overwrite" / "appdata" / "logs",
        ]
        self._since = time.time() - 1
        self._frame = QTimer(self, interval=33, timeout=self.update)
        self._frame.start()
        self._poll = QTimer(self, interval=POLL_MS, timeout=self._check)
        self._poll.start()
        QTimer.singleShot(TIMEOUT_MS, self, self.close)

    def show(self) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.move(screen.center() - self.rect().center())
        super().show()

    def _check(self) -> None:
        log = engine_started(self._logs, self._since)
        if log is not None:
            applog.get_logger().info("splash: engine started (%s)", log)
            self.close()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.drawPixmap(0, 0, self._background)
        painter.save()
        painter.setClipRect(0, BAR, WIDTH, HEIGHT - 2 * BAR)
        rain = QColor("#b9c9bd")
        rain.setAlphaF(0.22)
        painter.setPen(QPen(rain, 1))
        phase = (self._clock.elapsed() % RAIN_MS) / RAIN_MS
        span = WIDTH + 60
        for x, y, length in self._drops:
            top = (y + phase * HEIGHT) % HEIGHT
            left = (x + 60 + phase * RAIN_DRIFT) % span - 60
            painter.drawLine(QPointF(left, top), QPointF(left + length * 0.22, top + length))
        painter.restore()
        logo = self._logo.deviceIndependentSize()
        painter.drawPixmap(
            QPointF(24, HEIGHT - BAR - 10 - logo.height()), self._logo
        )
        painter.fillRect(QRect(0, HEIGHT - BAR, WIDTH, 1), self._accent)
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

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._poll.stop()
        self._frame.stop()
        super().closeEvent(event)


def start(anomaly: str | Path, gamma: str | Path) -> subprocess.Popen:
    """Show the splash in a process of its own; terminate it to close early.

    On Wayland no window can keep itself above others, so the splash runs
    through XWayland, where the window manager honours "stay on top" - the
    MO2 and Steam windows that open meanwhile come up underneath it.
    """
    env = dict(os.environ)
    if env.get("WAYLAND_DISPLAY") and env.get("DISPLAY"):
        env["QT_QPA_PLATFORM"] = "xcb"
    accent = active_theme_tokens().get("accent", "#9fe96f")
    return subprocess.Popen(
        [
            sys.executable, "-m", __name__, str(anomaly), str(gamma), accent,
            tr("Loading").upper(), tr("Loading xrRAZOM"),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    # COMMANDER's window class, desktop file and icon: the taskbar groups
    # it with COMMANDER (StartupWMClass in its .desktop file).
    app = QApplication(["stalker-gamma-commander"])
    app.setApplicationName("stalker-gamma-commander")
    app.setDesktopFileName("stalker-gamma-commander")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[2] / "cli" / "stalker-gamma.png")))
    splash = GameSplash(*sys.argv[1:5])
    splash.setWindowTitle(sys.argv[5])
    splash.show()
    sys.exit(app.exec())
