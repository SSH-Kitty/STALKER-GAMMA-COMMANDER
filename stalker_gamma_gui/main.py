"""Stalker GAMMA GUI - entry point."""

from __future__ import annotations

import os
import sys

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

    theme = gui_settings.load_gui_settings().get("theme") or "gamma"
    font_size = int(gui_settings.load_gui_settings().get("font_size") or 13)
    set_active_theme(theme)
    app.setPalette(build_palette(theme))
    app.setStyleSheet(build_stylesheet(theme, font_size=font_size))

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

    saved = gui_settings.load_gui_settings()
    if saved.get("autostart") and not is_autostart_enabled():
        gui_settings.save_gui_settings(autostart=False)

    app.aboutToQuit.connect(shutdown_active_runners)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
