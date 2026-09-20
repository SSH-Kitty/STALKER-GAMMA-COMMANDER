"""Stalker GAMMA GUI - entry point."""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path

# Qt's FFmpeg multimedia backend (used for the click sound - see
# ui/common.py::play_click_sound) prints its own startup banner and a
# full ffmpeg-style "Input #0, wav, from ..." format dump to the
# console the first time it loads an audio source - harmless, but reads
# like an error/warning to anyone watching the terminal. Must be set
# before Qt's logging categories are first touched; setdefault() so a
# user debugging real audio issues can still override it themselves.
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.ffmpeg*=false")

from PySide6.QtCore import (
    QLockFile,
    QTimer,
    QtMsgType,
    qInstallMessageHandler,
)
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QCheckBox, QMessageBox

from . import gui_settings
from .applog import install_excepthook
from .config import (
    cli_binary_path,
    mark_secondary_instance,
    multiple_instances_allowed,
    project_root,
    settings_dir,
)
from .deck_launch import (
    clear_relaunch_depth,
    deck_requested,
    steam_deck_model,
    strip_deck_flag,
)
from .fonts import load_bundled_font
from .i18n import set_active_language, tr
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


def release_instance_lock() -> None:
    """Drop the single-instance lock before re-exec'ing into Deck Mode.

    Load-bearing, not tidiness. QLockFile records the owning PID in the lock
    file, and ``os.execve`` replaces the process image while keeping the same
    PID - so without this the relaunched COMMANDER finds a lock whose owner
    is alive and whose process name matches its own, concludes that another
    instance is running, and exits at the check in ``main()`` without ever
    showing a window.
    """
    global _INSTANCE_LOCK

    lock = _INSTANCE_LOCK
    _INSTANCE_LOCK = None
    if lock is None:
        return
    try:
        lock.unlock()
    except (OSError, RuntimeError):
        pass


def _cli_binary_problem() -> tuple[str, str] | None:
    """Return ``(title, message)`` if the bundled CLI is unusable, else None.

    Split out of ``main()`` so Deck Mode can render the same diagnosis inside
    its own window: a process that exits before showing anything leaves a
    Game Mode user staring at a black screen with no explanation at all.
    """
    binary = cli_binary_path()
    if not binary.is_file():
        return (
            "CLI Not Found",
            (f"Could not locate the stalker-gamma CLI at:\n{binary}\n\n"
            "Place the extracted CLI bundle under Project/cli/usr/bin/ "
            "or set the STALKER_GAMMA_CLI environment variable."),
        )
    if not os.access(binary, os.X_OK):
        return (
            "CLI Not Executable",
            (f"The stalker-gamma CLI is not executable:\n{binary}\n\n"
            f"Run:  chmod +x '{binary}'"),
        )
    return None


def _ask_deck_mode() -> bool:
    """Offer Deck Mode on detected Steam Deck hardware; remember if asked to.

    Runs before any window exists, so choosing Deck Mode here simply builds a
    DeckWindow instead of a MainWindow - the re-exec machinery in
    ``deck_launch`` is only needed to switch modes once a UI is already up.
    """
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(tr("Steam Deck detected"))
    box.setText(tr("Use Deck Mode?"))
    box.setInformativeText(
        tr(
            "Deck Mode is a simplified, controller- and touch-friendly "
            "interface sized for the Steam Deck's screen. You can switch back "
            "to the full interface at any time from Deck Mode's settings."
        )
    )
    remember = QCheckBox(tr("Remember my choice"))
    box.setCheckBox(remember)
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    box.setDefaultButton(QMessageBox.StandardButton.Yes)
    chose_deck = box.exec() == QMessageBox.StandardButton.Yes
    if remember.isChecked():
        gui_settings.save_gui_settings(
            deck_mode_preference="always" if chose_deck else "never"
        )
    return chose_deck


def _cleanup_interrupted_move(saved: dict) -> None:
    """Offer to delete folders left behind by an interrupted Move Game.

    Desktop mode only. This is a parentless modal that can rmtree
    directories, shown before any window exists - not something to put in
    front of a Game Mode user with a gamepad. Deck Mode leaves ``move_dest``
    and ``move_expected`` untouched so the next desktop launch still recovers.
    """
    move_dest = saved.get("move_dest", "")
    expected = {
        name for name in saved.get("move_expected", []) if isinstance(name, str)
    }
    if not (move_dest and expected):
        return
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
    if not safe_destination:
        gui_settings.save_gui_settings(move_dest="", move_expected=[])
        return
    orphans = [
        name
        for name in safe_names
        if (dest / name).is_dir()
        and not (dest / name).is_symlink()
        and (dest / name).resolve().parent == dest
    ]
    if not orphans:
        gui_settings.save_gui_settings(move_dest="", move_expected=[])
        return
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


def main(argv: list[str] | None = None) -> int:
    global _INSTANCE_LOCK, _PREVIOUS_QT_HANDLER
    install_excepthook()
    argv = list(sys.argv if argv is None else argv)
    deck = deck_requested(argv)
    _PREVIOUS_QT_HANDLER = qInstallMessageHandler(_quiet_qt_message_handler)
    app = QApplication(strip_deck_flag(argv))
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
        # The lock is still attempted either way, because losing it is what
        # identifies this process as an extra instance rather than the first
        # one - which is what the title bar goes on to say.
        if not multiple_instances_allowed():
            QMessageBox.information(
                None, "COMMANDER", "COMMANDER is already running."
            )
            return 0
        mark_secondary_instance()

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
    problem = _cli_binary_problem()
    if problem is not None and not deck:
        QMessageBox.critical(None, problem[0], problem[1])
        return 1

    # Offer Deck Mode on real Deck hardware, unless the user already settled
    # it. Nothing has been built yet, so a "yes" just changes which window
    # class gets constructed below - no re-exec is involved on this path.
    if not deck and steam_deck_model() is not None:
        preference = _gui.get("deck_mode_preference", "ask")
        if preference == "always":
            deck = True
        elif preference == "ask":
            deck = _ask_deck_mode()

    # If autostart was enabled but the .desktop file no longer exists (e.g.
    # the user removed it externally), sync the setting to False.
    from .autostart import is_autostart_enabled

    saved = _gui
    if saved.get("autostart") and not is_autostart_enabled():
        gui_settings.save_gui_settings(autostart=False)

    if not deck:
        _cleanup_interrupted_move(saved)

    # Background network checks have bounded timeouts but may outlive the
    # window-close event. Wait long enough for them to finish before Qt tears
    # down their QThreads.
    def _shutdown() -> None:
        begin_shutdown()
        shutdown_active_runners(timeout_ms=30000)

    app.aboutToQuit.connect(_shutdown)
    if deck:
        # The single place commander_gui reaches into the Steamdeck package.
        # Kept function-local so importing commander_gui never pulls in the
        # Deck GUI, and so the dependency stays one-way and greppable.
        from Steamdeck.window import DeckWindow

        # Drives the X11 WM_CLASS, which the Deck .desktop entry's
        # StartupWMClass has to match for desktop sessions to associate the
        # window with the right launcher.
        app.setDesktopFileName("stalker-gamma-commander-deck")
        window = DeckWindow(fatal=problem)
        window.show_for_environment()
    else:
        window = MainWindow()
        window.show()
    # A session where the user deliberately toggles modes a few times must not
    # look like a relaunch loop to deck_launch's depth guard.
    QTimer.singleShot(5000, clear_relaunch_depth)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
