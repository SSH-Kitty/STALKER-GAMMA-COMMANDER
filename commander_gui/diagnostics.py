"""Diagnostic log export for troubleshooting.

Gathers system information, app settings and launcher logs into a single
text file that can be shared when reporting issues.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import cli_binary_path, gui_settings_path, logs_dir, settings_path

_MAX_DIAGNOSTIC_FILE_BYTES = 1_000_000
_SENSITIVE_VALUE_RE = re.compile(
    r'(?i)("[^"]*(?:token|password|passwd|secret|credential|api[_-]?key)[^"]*"\s*:\s*)"[^"]*"'
)


def _redact(text: str) -> str:
    return _SENSITIVE_VALUE_RE.sub(r'\1"[REDACTED]"', text)


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
                lines.append(
                    f"Screen: {screen.name()} {screen.size().width()}x{screen.size().height()}"
                )
    except (ImportError, RuntimeError):
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
            check=False,
        )
        output = (result.stdout + result.stderr).strip()
        return output if output else f"(no output, exit code {result.returncode})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Failed to run CLI: {exc}"


def _read_file(path: Path) -> str:
    if not path.is_file():
        return f"File not found: {path}"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - _MAX_DIAGNOSTIC_FILE_BYTES))
            text = stream.read(_MAX_DIAGNOSTIC_FILE_BYTES).decode(
                "utf-8", errors="replace"
            )
        if size > _MAX_DIAGNOSTIC_FILE_BYTES:
            return "[file tail; beginning omitted]\n" + text
        return text
    except OSError as exc:
        return f"Could not read {path}: {exc}"


def collect_diagnostics() -> str:
    sections = [
        _section("System Info", _system_info()),
        _section("CLI Version", _cli_version()),
        _section("CLI Settings (settings.json)", _redact(_read_file(settings_path()))),
        _section(
            "GUI Settings (gui-settings.json)",
            _redact(_read_file(gui_settings_path())),
        ),
        _section("Launcher Log", _redact(_read_file(logs_dir() / "launcher.log"))),
    ]
    return "\n".join(sections)


def export_diagnostics(path: Path) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(collect_diagnostics())
        Path(tmp).replace(path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
