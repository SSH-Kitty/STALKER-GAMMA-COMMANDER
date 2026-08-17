"""Diagnostic log export for troubleshooting.

Gathers system information, app settings and launcher logs into a single
text file that can be shared when reporting issues.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import cli_binary_path, gui_settings_path, logs_dir, settings_path


def _section(title: str, body: str) -> str:
    return f"{'=' * 60}\n{title}\n{'=' * 60}\n{body.strip()}\n"


def _system_info() -> str:
    lines = [
        f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"GUI version: {__version__}",
        f"Python: {sys.version}",
        f"Platform: {platform.platform()}",
        f"Executable: {sys.executable}",
        f"Frozen: {getattr(sys, 'frozen', False)}",
        f"DISPLAY: {os.environ.get('DISPLAY', '(unset)')}",
        f"WAYLAND_DISPLAY: {os.environ.get('WAYLAND_DISPLAY', '(unset)')}",
        f"XDG_CURRENT_DESKTOP: {os.environ.get('XDG_CURRENT_DESKTOP', '(unset)')}",
        f"XDG_SESSION_TYPE: {os.environ.get('XDG_SESSION_TYPE', '(unset)')}",
    ]
    try:
        from PySide6 import QtCore, QtWidgets

        lines.append(f"PySide6: {QtCore.__version__}")
        app = QtWidgets.QApplication.instance()
        if app is not None:
            screen = app.primaryScreen()
            if screen is not None:
                lines.append(f"Screen: {screen.name()} {screen.size().width()}x{screen.size().height()}")
    except Exception:
        pass
    return "\n".join(lines)


def _cli_version() -> str:
    cli = cli_binary_path()
    if not cli.is_file():
        return f"CLI not found at {cli}"
    try:
        result = subprocess.run(
            [str(cli), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = (result.stdout + result.stderr).strip()
        return output if output else f"(no output, exit code {result.returncode})"
    except Exception as exc:
        return f"Failed to run CLI: {exc}"


def _read_file(path: Path) -> str:
    if not path.is_file():
        return f"File not found: {path}"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read {path}: {exc}"


def collect_diagnostics() -> str:
    sections = [
        _section("System Info", _system_info()),
        _section("CLI Version", _cli_version()),
        _section("CLI Settings (settings.json)", _read_file(settings_path())),
        _section("GUI Settings (gui-settings.json)", _read_file(gui_settings_path())),
        _section("Launcher Log", _read_file(logs_dir() / "launcher.log")),
    ]
    return "\n".join(sections)


def export_diagnostics(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(collect_diagnostics(), encoding="utf-8")
