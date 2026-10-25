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

from . import __version_label__
from .atomic import write_bytes
from .config import cli_binary_path, gui_settings_path, logs_dir, settings_path
from .launcher import ptrace_scope

_MAX_DIAGNOSTIC_FILE_BYTES = 1_000_000
_SENSITIVE_VALUE_RE = re.compile(
    r'(?i)("[^"]*(?:token|password|passwd|secret|credential|api[_-]?key)[^"]*"\s*:\s*)"[^"]*"'
)
#: A plain `\b` word-boundary fails immediately before the keyword whenever
#: it's preceded by another word character - and `_` counts as one, so
#: `\btoken\b` never matches the "token" in `GITHUB_TOKEN=...` (extremely
#: common env-var/config convention). `(?:^|(?<=[^A-Za-z]))` instead only
#: requires the preceding character to not be a letter (start-of-string,
#: underscore, digit, punctuation all count), which still excludes false
#: positives like "tokenizer" while actually catching the underscore-joined
#: convention this is meant to guard against.
_KEY_START = r"(?:^|(?<=[^A-Za-z]))"
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)" + _KEY_START + r"((?:token|password|passwd|secret|credential|api[_-]?key)\b\s*[:=]\s*)"
    r'''("[^"]*"|'[^']*')'''
)
_SENSITIVE_ASSIGNMENT_UNQUOTED_RE = re.compile(
    r"(?i)" + _KEY_START + r"((?:token|password|passwd|secret|credential|api[_-]?key)\b\s*[:=]\s*)"
    r"[^\s,;&]+"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic)\s+)\S+"
)
_URL_CREDENTIALS_RE = re.compile(r"(?i)(://)[^/@\s]+:[^/@\s]+@")


def redact(text: str) -> str:
    """Mask tokens, passwords, auth headers, and URL credentials in *text*.

    Public: also used by log_dump.py to scrub log dumps, not just this
    module's own diagnostics export.
    """
    redacted = _SENSITIVE_VALUE_RE.sub(r'\1"[REDACTED]"', text)
    redacted = _SENSITIVE_ASSIGNMENT_RE.sub(r'\1"[REDACTED]"', redacted)
    redacted = _SENSITIVE_ASSIGNMENT_UNQUOTED_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _AUTH_HEADER_RE.sub(r"\1[REDACTED]", redacted)
    return _URL_CREDENTIALS_RE.sub(r"\1[REDACTED]@", redacted)


def _section(title: str, body: str) -> str:
    return f"{'=' * 60}\n{title}\n{'=' * 60}\n{body.strip()}\n"


def _first_line(path: str, prefix: str = "") -> str:
    """First line of *path* starting with *prefix* (minus the prefix), or ''."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if line.startswith(prefix):
                    return line[len(prefix):].strip()
    except OSError:
        pass
    return ""


def _distro() -> str:
    name = _first_line("/etc/os-release", "PRETTY_NAME=").strip('"')
    return name or "(unknown)"


def _memory() -> str:
    def gib(key: str) -> str:
        value = _first_line("/proc/meminfo", key + ":").split()
        return f"{int(value[0]) / 1048576:.1f} GiB" if value else "?"

    return f"{gib('MemTotal')} total, {gib('MemAvailable')} available, swap {gib('SwapTotal')}"


def _gpus() -> list[str]:
    """Vulkan devices and their drivers; the DRM drivers when vulkaninfo is missing."""
    try:
        result = subprocess.run(
            ["vulkaninfo", "--summary"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        keys = ("deviceName", "driverName", "driverInfo", "apiVersion")
        groups: list[list[str]] = []
        for line in result.stdout.splitlines():
            if re.match(r"GPU\d+:", line.strip()):  # one block per device
                groups.append([])
            key, _, value = line.partition("=")
            if groups and value and key.strip() in keys:
                groups[-1].append(value.strip())
        if any(groups):
            return ["GPU: " + " | ".join(group) for group in groups if group]
        vulkan = f"vulkaninfo found no device (exit {result.returncode})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        vulkan = f"vulkaninfo unavailable ({exc.__class__.__name__})"
    lines = [f"GPU: {vulkan}"]
    for uevent in sorted(Path("/sys/class/drm").glob("card[0-9]*/device/uevent")):
        driver = _first_line(str(uevent), "DRIVER=")
        pci = _first_line(str(uevent), "PCI_ID=")
        lines.append(f"GPU: {uevent.parent.parent.name} driver={driver or '?'} pci={pci or '?'}")
    return lines


def _install_kind() -> str:
    if os.environ.get("APPIMAGE"):
        return "AppImage"
    from .config import project_root

    return str(project_root())


def _login_shape() -> str:
    """What Wine and MO2 care about in the login name, without the name:
    non-ASCII characters and spaces break some of their paths."""
    name = Path.home().name
    return ", ".join((
        "ASCII" if name.isascii() else "non-ASCII",
        "contains spaces" if " " in name else "no spaces",
    ))


def system_snapshot() -> list[str]:
    """Version, system, hardware and limits: the startup banner in commander.log
    and the top of every diagnostics export."""
    import resource
    import shutil

    from .deck_launch import steam_deck_model

    try:
        nofile = "/".join(str(v) for v in resource.getrlimit(resource.RLIMIT_NOFILE))
    except (OSError, ValueError):
        nofile = "?"
    proxies = [k for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") if os.environ.get(k) or os.environ.get(k.lower())]
    lines = [
        f"GUI version: {__version_label__}",
        f"Install: {_install_kind()}",
        f"Distro: {_distro()}",
        f"Kernel: {platform.release()}",
        f"Python: {sys.version.split()[0]}",
        (
            f"Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', '(unset)')} "
            f"session={os.environ.get('XDG_SESSION_TYPE', '(unset)')} "
            f"DISPLAY={os.environ.get('DISPLAY', '(unset)')} "
            f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '(unset)')}"
        ),
        f"Locale: {os.environ.get('LC_ALL') or os.environ.get('LANG') or '(unset)'}",
        f"Login name: {_login_shape()}",
        f"CPU: {_first_line('/proc/cpuinfo', 'model name').lstrip(': ') or platform.machine()} ({os.cpu_count()} threads)",
        f"RAM: {_memory()}",
        *_gpus(),
        f"Steam Deck: {steam_deck_model() or 'no'}",
        f"vm.max_map_count: {_first_line('/proc/sys/vm/max_map_count') or '?'}",
        f"Open files limit (soft/hard): {nofile}",
        f"gamemoderun: {'yes' if shutil.which('gamemoderun') else 'no'}",
        f"ptrace_scope: {ptrace_scope()}",
        f"Proxy: {', '.join(proxies) if proxies else 'none'}",
    ]
    try:
        from PySide6 import QtCore

        lines.append(f"PySide6: {QtCore.__version__} (Qt {QtCore.qVersion()})")
    except ImportError:
        pass
    return lines


def _system_info() -> str:
    lines = [
        f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        *system_snapshot(),
        f"Executable: {sys.executable}",
        f"Frozen: {getattr(sys, 'frozen', False)}",
    ]
    try:
        from PySide6 import QtWidgets

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


def _anomaly_settings() -> str:
    """The game's user.ltx (renderer, resolution, every option) and the
    size of its shader cache."""
    from .settings import load_settings

    profile = load_settings().active_profile
    if profile is None:
        return "No active profile"
    appdata = Path(profile.anomaly).expanduser() / "appdata"
    cache = appdata / "shaders_cache"
    try:
        size = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file())
        cache_line = f"Shader cache: {size / 1048576:.1f} MiB"
    except OSError as exc:
        cache_line = f"Shader cache: unreadable ({exc.__class__.__name__})"
    # The co-op player name is often the person's usual online handle.
    ltx = "\n".join(
        line for line in _read_file(appdata / "user.ltx").splitlines()
        if not line.startswith("xrr_player_name")
    )
    return cache_line + "\n\n" + ltx


def _cli_version() -> str:
    cli = cli_binary_path()
    if not cli.is_file():
        return f"CLI not found at {cli}"
    try:
        result = subprocess.run(
            [str(cli), "--version"],
            capture_output=True,
            text=True,
            # Without an explicit errors= the locale codec decodes strictly,
            # so a CLI binary that emits one non-UTF-8 byte raises
            # UnicodeDecodeError - which is a ValueError, not an OSError, so
            # it escapes the handler below and aborts the whole diagnostics
            # export (and the log dump built on top of it) in a background
            # thread. Matches how every other subprocess here decodes.
            encoding="utf-8",
            errors="replace",
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
        _section("CLI Settings (settings.json)", redact(_read_file(settings_path()))),
        _section(
            "GUI Settings (gui-settings.json)",
            redact(_read_file(gui_settings_path())),
        ),
        _section("Anomaly Settings (user.ltx)", redact(_anomaly_settings())),
        _section("Launcher Log", redact(_read_file(logs_dir() / "launcher.log"))),
        _section(
            "COMMANDER App Log", redact(_read_file(logs_dir() / "commander.log"))
        ),
    ]
    return "\n".join(sections)


def export_diagnostics(path: Path) -> None:
    # Atomic; no .lock dotfile beside a file saved wherever the user chose.
    write_bytes(path, collect_diagnostics().encode("utf-8"), lock=False)
