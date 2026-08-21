"""Stalker GAMMA GUI - entry point."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from . import gui_settings
from .config import cli_binary_path
from .fonts import load_bundled_font
from .themes import build_palette, build_stylesheet, set_active_theme
from .ui.common import shutdown_active_runners
from .ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Stalker GAMMA GUI")
    app.setOrganizationName("stalker-gamma")
    app.setStyle("Fusion")

    load_bundled_font()

    _gui = gui_settings.load_gui_settings()
    theme = _gui.get("theme") or "gamma"
    try:
        font_size = int(_gui.get("font_size") or 13)
    except (TypeError, ValueError):
        font_size = 13
    font_family = _gui.get("font_family") or "Exo 2"
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
    app.aboutToQuit.connect(lambda: shutdown_active_runners(timeout_ms=30000))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
