"""Entry point: ``python -m assistant [dump.zip]``."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QTimer, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from .ui.main_window import MainWindow
from .ui.theme import apply_theme

#: Substrings of Qt log messages we never want on the console. Third-party
#: icon themes (e.g. Mkos-Big-Sur) ship mimetype SVGs referencing pattern
#: definitions Qt cannot resolve; the icons render anyway but spam one
#: warning per parse whenever a file picker shows zip entries.
_SUPPRESSED_MARKERS = ("Could not resolve property:",)


def filtered_message_handler(previous):
    """Build a Qt log handler dropping suppressed markers, passing the rest.

    ``previous`` is the handler returned by ``qInstallMessageHandler`` (or
    ``None`` for Qt's built-in stderr output).
    """

    def handler(mode, context, message):
        if any(marker in message for marker in _SUPPRESSED_MARKERS):
            return
        if previous is not None:
            previous(mode, context, message)
        else:
            sys.stderr.write(f"{message}\n")

    return handler


def install_message_filter() -> None:
    """Install :func:`filtered_message_handler`, preserving the old chain."""
    previous = qInstallMessageHandler(None)
    qInstallMessageHandler(filtered_message_handler(previous))


def _path_from_argument(argument: str) -> Path:
    """Convert a command-line path or local file URI to a filesystem path."""
    parsed = urlparse(argument)
    if parsed.scheme.lower() != "file":
        return Path(argument)
    uri_path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        uri_path = f"//{parsed.netloc}{uri_path}"
    return Path(uri_path)


def main() -> None:
    args = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    candidates = [_path_from_argument(arg) for arg in args]
    invalid = next((path for path in candidates if not path.is_file()), None)
    if invalid is not None:
        print(f"Error: archive is not a file: {invalid}", file=sys.stderr)
        raise SystemExit(2)

    install_message_filter()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("COMMANDER Assistant")
    apply_theme(app)
    window = MainWindow()
    window.show()
    if candidates:
        QTimer.singleShot(0, lambda: window._open_paths(candidates))
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
