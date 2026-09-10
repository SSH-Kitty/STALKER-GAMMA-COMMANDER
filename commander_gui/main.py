"""Stalker GAMMA GUI - entry point."""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QLockFile,
    QPropertyAnimation,
    QtMsgType,
    qInstallMessageHandler,
)
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from . import gui_settings
from .config import cli_binary_path, project_root, settings_dir
from .fonts import load_bundled_font
from .i18n import set_active_language
from .themes import build_palette, build_stylesheet, set_active_theme
from .ui.common import begin_shutdown, shutdown_active_runners
from .ui.main_window import MainWindow

_INSTANCE_LOCK: QLockFile | None = None


def _set_linux_process_name() -> None:
    """Set the process label used by Linux process monitors."""
    if os.name != "posix":
        return
    try:
        libc = ctypes.CDLL(None)
        libc.prctl(15, b"STALKER COMMAND", 0, 0, 0)
    except (AttributeError, OSError):
        pass


def _is_svg_noise(mode: QtMsgType, message: str | None) -> bool:
    """True for cosmetic Qt SVG renderer warnings from icon-theme SVGs.

    Some system icon themes (e.g. Mkos-Big-Sur-Night) ship SVG files that
    reference undefined patterns.  Qt's SVG renderer emits a ``qt.svg:``
    warning for every such file whenever the file dialog renders a zip/7z
    icon, drowning the console in noise.
    """
    if mode != QtMsgType.QtWarningMsg:
        return False
    text = message or ""
    return text.startswith("qt.svg:") or "Could not resolve property" in text


def _is_window_opacity_noise(mode: QtMsgType, message: str | None) -> bool:
    """True for the harmless "does not support setting window opacity" warning.

    Emitted by the startup fade-in (``main()``'s ``window.setWindowOpacity()``
    animation) on any window manager/QPA backend without compositing support
    - the property is still tracked and animated internally, it just has no
    visible effect there. That's a harmless no-op, but the animation ticks
    ~60 times a second while it runs, so left unfiltered this one warning
    floods the console on every single launch.
    """
    if mode != QtMsgType.QtWarningMsg:
        return False
    text = message or ""
    return "does not support setting window opacity" in text


def _is_portal_noise(mode: QtMsgType, message: str | None) -> bool:
    """True for the harmless xdg-desktop-portal app-id registration warning.

    Qt tries to register the app-id set via setDesktopFileName() with the
    host's xdg-desktop-portal. That registration only succeeds when a
    matching .desktop file is installed somewhere the portal's AppInfo
    lookup can see it - running from a source checkout, or an AppImage
    that was never integrated with a tool like appimaged/AppImageLauncher,
    has no such file, so the portal call always fails here. Portal-backed
    features (native file dialogs, etc.) still work without it; this only
    means the app can't be identified to the portal by app ID.
    """
    if mode != QtMsgType.QtWarningMsg:
        return False
    text = message or ""
    return "Failed to register with host portal" in text


_PREVIOUS_QT_HANDLER = None


def _quiet_qt_message_handler(mode: QtMsgType, context, message: str) -> None:
    """Forward Qt messages to the previous handler, skipping known noise.

    Qt may emit from any thread, so never uninstall/reinstall the global
    handler here -- forward to the handler captured at install time instead.
    """
    if (
        _is_svg_noise(mode, message)
        or _is_portal_noise(mode, message)
        or _is_window_opacity_noise(mode, message)
    ):
        return
    if _PREVIOUS_QT_HANDLER is not None:
        _PREVIOUS_QT_HANDLER(mode, context, message)
    else:
        sys.stderr.write(message + "\n")


def _acquire_instance_lock() -> QLockFile | None:
    """Acquire the shared lock used to prevent duplicate Commander windows."""
    lock_path = settings_dir() / "commander.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_path))
    return lock if lock.tryLock(0) else None


def main() -> int:
    global _INSTANCE_LOCK, _PREVIOUS_QT_HANDLER
    _PREVIOUS_QT_HANDLER = qInstallMessageHandler(_quiet_qt_message_handler)
    app = QApplication(sys.argv)
    app.setApplicationName("STALKER COMMANDER")
    app.setApplicationDisplayName("STALKER COMMANDER")
    app.setDesktopFileName("stalker-gamma-commander")
    app.setOrganizationName("stalker-gamma")
    icon = project_root() / "cli" / "stalker-gamma.png"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    _set_linux_process_name()
    app.setStyle("Fusion")

    try:
        _INSTANCE_LOCK = _acquire_instance_lock()
    except OSError as exc:
        QMessageBox.critical(
            None,
            "COMMANDER",
            f"Could not create the Commander instance lock:\n{exc}",
        )
        return 1
    if _INSTANCE_LOCK is None:
        QMessageBox.information(None, "COMMANDER", "COMMANDER is already running.")
        return 0

    load_bundled_font()

    _gui = gui_settings.load_gui_settings()
    theme = _gui.get("theme") or "gamma"
    try:
        font_size = int(_gui.get("font_size") or 13)
    except (TypeError, ValueError):
        font_size = 13
    font_family = _gui.get("font_family") or "Exo 2"
    set_active_language(_gui.get("language") or "en")
    set_active_theme(theme)
    app.setPalette(build_palette(theme))
    app.setStyleSheet(
        build_stylesheet(theme, font_size=font_size, font_family=font_family)
    )

    # Checked before the window is built: constructing the pages kicks off
    # background CLI calls, so a missing binary must be reported first.
    binary = cli_binary_path()
    if not binary.is_file():
        QMessageBox.critical(
            None,
            "CLI Not Found",
            f"Could not locate the stalker-gamma CLI at:\n{binary}\n\n"
            "Place the extracted CLI bundle under Project/cli/usr/bin/ "
            "or set the STALKER_GAMMA_CLI environment variable.",
        )
        return 1
    if not os.access(binary, os.X_OK):
        QMessageBox.critical(
            None,
            "CLI Not Executable",
            f"The stalker-gamma CLI is not executable:\n{binary}\n\n"
            f"Run:  chmod +x '{binary}'",
        )
        return 1

    # If autostart was enabled but the .desktop file no longer exists (e.g.
    # the user removed it externally), sync the setting to False.
    from .autostart import is_autostart_enabled

    saved = _gui
    if saved.get("autostart") and not is_autostart_enabled():
        gui_settings.save_gui_settings(autostart=False)

    # Clean up only the folders recorded by a previously interrupted Move Game.
    move_dest = saved.get("move_dest", "")
    expected = {
        name for name in saved.get("move_expected", []) if isinstance(name, str)
    }
    if move_dest and expected:
        try:
            raw_dest = Path(move_dest).expanduser()
            dest = raw_dest.resolve()
        except (OSError, RuntimeError, ValueError):
            raw_dest = None
            dest = None
        safe_destination = (
            raw_dest is not None
            and dest is not None
            and raw_dest.is_dir()
            and not raw_dest.is_symlink()
            and dest == raw_dest
            and dest != Path.home()
            and dest != Path.cwd().resolve()
            and dest.parent != dest
        )
        safe_names = {
            name
            for name in expected
            if name and Path(name).name == name and Path(name).is_absolute() is False
        }
        if safe_destination:
            orphans = [
                name
                for name in safe_names
                if (dest / name).is_dir()
                and not (dest / name).is_symlink()
                and (dest / name).resolve().parent == dest
            ]
            if orphans:
                answer = QMessageBox.question(
                    None,
                    "Move Game Interrupted",
                    "A previous Move Game operation was interrupted.\n\n"
                    f"Orphaned folders found at:\n{move_dest}\n"
                    f"Folders: {', '.join(orphans)}\n\n"
                    "Delete them?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    for name in orphans:
                        shutil.rmtree(dest / name, ignore_errors=True)
                    gui_settings.save_gui_settings(move_dest="", move_expected=[])
            else:
                gui_settings.save_gui_settings(move_dest="", move_expected=[])
        else:
            gui_settings.save_gui_settings(move_dest="", move_expected=[])

    # Background network checks have bounded timeouts but may outlive the
    # window-close event. Wait long enough for them to finish before Qt tears
    # down their QThreads.
    def _shutdown() -> None:
        begin_shutdown()
        shutdown_active_runners(timeout_ms=30000)

    app.aboutToQuit.connect(_shutdown)
    window = MainWindow()
    window.setWindowOpacity(0.0)
    window.show()
    startup_fade = QPropertyAnimation(window, b"windowOpacity")
    startup_fade.setDuration(250)
    startup_fade.setStartValue(0.0)
    startup_fade.setEndValue(1.0)
    startup_fade.setEasingCurve(QEasingCurve.Type.OutCubic)
    startup_fade.start()
    # Kept alive on the window itself - nothing else references it, and
    # Python would otherwise garbage-collect it mid-animation.
    window._startup_fade = startup_fade
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
