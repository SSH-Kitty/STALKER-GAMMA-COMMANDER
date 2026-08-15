"""Stalker GAMMA GUI - entry point."""

from __future__ import annotations

import os
import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from .config import cli_binary_path
from .ui.common import shutdown_active_runners
from .ui.main_window import MainWindow

STYLESHEET = """
* {
    font-family: "DejaVu Sans", "Noto Sans", sans-serif;
    font-size: 13px;
}
QMainWindow {
    background-color: #0d130c;
    color: #e2ead8;
}
QWidget {
    background-color: rgba(10, 14, 9, 180);
    color: #e2ead8;
}
#topbar {
    background-color: rgba(7, 11, 7, 240);
    border-bottom: 1px solid #26321f;
}
#wordmark {
    font-size: 17px;
    font-weight: bold;
    letter-spacing: 2px;
    color: #9fe96f;
}
#navtabs {
    background: transparent;
}
#navtabs::tab {
    background: transparent;
    color: #8fa387;
    padding: 12px 18px;
    border: none;
    border-bottom: 2px solid transparent;
}
#navtabs::tab:hover {
    color: #d8e6cf;
}
#navtabs::tab:selected {
    color: #9fe96f;
    border-bottom: 2px solid #8fe45c;
}
#card {
    background-color: #141b15;
    border: 1px solid #26321f;
    border-radius: 10px;
}
#section1 {
    font-size: 20px;
    font-weight: bold;
    color: #eef4e8;
}
#section2 {
    font-size: 15px;
    font-weight: bold;
    color: #a8d66f;
}
#info {
    color: #9aa88f;
}
#dim {
    color: #7f8f78;
}
#accent {
    color: #9fe96f;
}
#warn {
    color: #d9a04c;
}
#mono {
    font-family: "DejaVu Sans Mono", monospace;
    font-size: 12px;
    color: #c8d8bc;
    background-color: #0a0f0a;
    border: 1px solid #26321f;
    border-radius: 5px;
    padding: 8px;
}
QLabel {
    background: transparent;
}
QPushButton {
    background-color: #1f2b20;
    border: 1px solid #33452f;
    border-radius: 6px;
    padding: 7px 14px;
    color: #e8f0df;
}
QPushButton:hover {
    background-color: #283828;
}
QPushButton:pressed {
    background-color: #17201a;
}
QPushButton:disabled {
    color: #5f6e5a;
    background-color: #161e16;
}
QPushButton#primary {
    background-color: #5fb548;
    border: 1px solid #8fe45c;
    color: #0c130a;
    font-weight: bold;
}
QPushButton#primary:hover {
    background-color: #6fc95a;
}
QPushButton#primary:disabled {
    background-color: #2a3d27;
    border: 1px solid #3c5038;
    color: #6f8367;
}
QPushButton#hero {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #4fa63c, stop: 1 #6fc95a
    );
    border: 1px solid #9fe96f;
    border-radius: 10px;
    padding: 16px 28px;
    color: #0c130a;
    font-size: 17px;
    font-weight: bold;
}
QPushButton#hero:hover {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #58b843, stop: 1 #7ad964
    );
}
QPushButton#hero:disabled {
    background: #1f2b20;
    border: 1px solid #33452f;
    color: #5f6e5a;
}
QPushButton#secondary {
    background-color: #1f2b20;
    border: 1px solid #3a4f35;
    border-radius: 10px;
    padding: 16px 20px;
    color: #d8e6cf;
    font-size: 14px;
}
QPushButton#secondary:hover {
    background-color: #283828;
    border-color: #6fc95a;
}
QPushButton#tertiary {
    background: transparent;
    border: none;
    color: #9aa88f;
    text-decoration: underline;
    padding: 4px 8px;
}
QPushButton#tertiary:hover {
    color: #c8e2a0;
}
#chip {
    background-color: #18221a;
    border: 1px solid #2e3b2c;
    border-radius: 12px;
    padding: 4px 12px;
    color: #9aa88f;
}
#chip[state="ok"] {
    color: #9fe96f;
    border-color: #3a5a30;
}
#chip[state="bad"] {
    color: #7f8f78;
}
QPushButton#danger {
    background-color: #7a2f2a;
    border: 1px solid #c0554f;
    color: #ffe6dd;
}
QPushButton#danger:hover {
    background-color: #8f3a33;
}
QPushButton#danger:disabled {
    background-color: #321b1a;
    border: 1px solid #57302d;
    color: #80635e;
}
QLineEdit, QSpinBox, QComboBox {
    background-color: #0d130e;
    border: 1px solid #2e3b2c;
    border-radius: 5px;
    padding: 5px 8px;
    color: #e8f0df;
    selection-background-color: #5fb548;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border-color: #8fe45c;
}
QComboBox QAbstractItemView {
    background-color: #141b15;
    border: 1px solid #2e3b2c;
    selection-background-color: #5fb548;
}
QProgressBar {
    background-color: #0d130e;
    border: 1px solid #2e3b2c;
    border-radius: 5px;
    text-align: center;
    color: #e8f0df;
}
QProgressBar::chunk {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #4fa63c, stop: 1 #8fe45c
    );
    border-radius: 4px;
}
QTableWidget, QListWidget {
    background-color: #0d130e;
    border: 1px solid #2e3b2c;
    border-radius: 5px;
    color: #e2ead8;
    gridline-color: #1c261b;
}
QHeaderView::section {
    background-color: #141b15;
    color: #9aa88f;
    border: none;
    border-bottom: 1px solid #2e3b2c;
    padding: 5px;
    font-weight: bold;
}
QTableWidget::item:selected, QListWidget::item:selected {
    background-color: #5fb548;
    color: #0c130a;
}
QPlainTextEdit {
    background-color: #0a0f0a;
    color: #c8d8bc;
    border: 1px solid #2e3b2c;
    border-radius: 5px;
    font-family: "DejaVu Sans Mono", monospace;
    font-size: 12px;
}
QGroupBox {
    border: 1px solid #2e3b2c;
    border-radius: 6px;
    margin-top: 8px;
    padding-top: 6px;
    color: #9aa88f;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #a8d66f;
}
QStatusBar {
    background-color: rgba(7, 11, 7, 240);
    color: #7f8f78;
    border-top: 1px solid #26321f;
}
QPushButton#githubLink {
    color: #ffffff;
    padding: 0 2px 0 8px;
    border: none;
    background: transparent;
}
QPushButton#githubLink:hover {
    color: #dfe8df;
    background: transparent;
}
QMessageBox, QFileDialog {
    background-color: #141b15;
}
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Stalker GAMMA GUI")
    app.setOrganizationName("stalker-gamma")
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#0d130c"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e2ead8"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0d130e"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#141b15"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e2ead8"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#1f2b20"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e8f0df"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#5fb548"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#0c130a"))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)

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

    app.aboutToQuit.connect(shutdown_active_runners)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
