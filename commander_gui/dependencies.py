"""Pre-flight dependency checks for the winetricks install flow.

Detects the Linux distribution and package manager so error messages can
include the exact command needed to install missing dependencies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import gui_settings

# ---------------------------------------------------------------------------
# Distro / package-manager detection
# ---------------------------------------------------------------------------

# Maps distro IDs (from /etc/os-release) to their package manager.
_PKG_MANAGER_MAP: dict[str, str] = {
    "ubuntu": "apt",
    "debian": "apt",
    "linuxmint": "apt",
    "pop": "apt",
    "elementary": "apt",
    "zorin": "apt",
    "kali": "apt",
    "raspbian": "apt",
    "fedora": "dnf",
    "rhel": "dnf",
    "centos": "dnf",
    "rocky": "dnf",
    "alma": "dnf",
    "ol": "dnf",
    "arch": "pacman",
    "manjaro": "pacman",
    "endeavouros": "pacman",
    "garuda": "pacman",
    "opensuse-leap": "zypper",
    "opensuse-tumbleweed": "zypper",
    "sles": "zypper",
}

# Package names per manager.  Most distros use the same name; differences
# are captured here.
_PKG_NAMES: dict[str, dict[str, str]] = {
    "apt": {
        "winetricks": "winetricks",
        "wine": "wine",
        "pip": "python3-pip",
        "pipx": "pipx",
    },
    "dnf": {
        "winetricks": "winetricks",
        "wine": "wine",
        "pip": "python3-pip",
        "pipx": "python3-pipx",
    },
    "pacman": {
        "winetricks": "winetricks",
        "wine": "wine",
        "pip": "python-pip",
        "pipx": "python-pipx",
    },
    "zypper": {
        "winetricks": "winetricks",
        "wine": "wine",
        "pip": "python3-pip",
        "pipx": "python3-pipx",
    },
}


def _read_os_release() -> dict[str, str]:
    """Parse /etc/os-release into a dict.  Returns empty dict on failure."""
    path = Path("/etc/os-release")
    if not path.is_file():
        return {}
    try:
        data: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                data[key.strip()] = value.strip().strip('"')
        return data
    except OSError:
        return {}


def detect_distro_id() -> str:
    """Return the lowercase distro ID (e.g. 'ubuntu', 'fedora')."""
    release = _read_os_release()
    return release.get("ID", "").lower()


def detect_package_manager() -> str | None:
    """Return the detected package manager name or *None*."""
    release = _read_os_release()
    distro_id = release.get("ID", "").lower()
    distro_id_like = release.get("ID_LIKE", "").lower()

    # Try the primary ID first.
    if distro_id in _PKG_MANAGER_MAP:
        return _PKG_MANAGER_MAP[distro_id]

    # Fall back to ID_LIKE (e.g. "ID_LIKE=debian fedora" on Ubuntu-based).
    for candidate in distro_id_like.split():
        if candidate in _PKG_MANAGER_MAP:
            return _PKG_MANAGER_MAP[candidate]

    return None


def _pkg_name(tool: str) -> str:
    """Return the package name for *tool* on the current distro."""
    mgr = detect_package_manager() or "apt"
    names = _PKG_NAMES.get(mgr, _PKG_NAMES["apt"])
    return names.get(tool, tool)


def _install_command(tool: str) -> str:
    """Return a one-line install command for *tool*."""
    pkg = _pkg_name(tool)
    mgr = detect_package_manager()
    if mgr == "apt":
        return f"sudo apt install {pkg}"
    if mgr == "dnf":
        return f"sudo dnf install {pkg}"
    if mgr == "pacman":
        return f"sudo pacman -S {pkg}"
    if mgr == "zypper":
        return f"sudo zypper install {pkg}"
    # Generic fallback.
    return f"Install '{pkg}' with your package manager"


def install_command(tool: str) -> str:
    """Return the manual package-install command for a dependency."""
    return _install_command(tool)


def configured_tool(tool: str) -> str:
    """Return a valid manual tool override, or an empty string."""
    overrides = gui_settings.load_gui_settings().get("tool_overrides") or {}
    override = overrides.get(tool, "")
    return (
        override
        if override and Path(override).is_file() and os.access(override, os.X_OK)
        else ""
    )


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------


def check_winetricks() -> str | None:
    """Return an error message if winetricks is missing, or *None*."""
    if configured_tool("winetricks") or shutil.which("winetricks"):
        return None
    cmd = _install_command("winetricks")
    return (
        "winetricks is required but was not found.\n"
        f"Install it with:  {cmd}\n"
        "Generic: https://github.com/Winetricks/winetricks#readme"
    )


def check_wine() -> str | None:
    """Return an error message if wine is missing, or *None*."""
    if configured_tool("wine") or shutil.which("wine"):
        return None
    cmd = _install_command("wine")
    return (
        "Wine is required but was not found.\n"
        f"Install it with:  {cmd}\n"
        "Generic: https://www.winehq.org/download"
    )


def check_protontricks() -> tuple[bool, str | None]:
    """Check for protontricks availability.

    Returns ``(need_install, error_message)``:

    * ``(False, None)`` — protontricks is already on PATH.
    * ``(True, None)``  — missing but can be auto-installed via pipx or pip.
    * ``(True, msg)``   — missing and no installer is available; *msg* tells
      the user what to install first.
    """
    if configured_tool("protontricks") or shutil.which("protontricks"):
        return False, None

    # Can we install it automatically?
    if shutil.which("pipx"):
        return True, None
    if _pip_usable():
        return True, None

    # pip exists but is externally managed (PEP 668) — recommend pipx instead.
    if shutil.which("pip") and _externally_managed():
        cmd = _install_command("pipx")
        return True, (
            "protontricks is required but was not found.\n"
            "pip is available but this system marks Python as externally "
            "managed (PEP 668), so pip cannot install packages directly.\n\n"
            f"Install pipx with your package manager:\n  {cmd}\n\n"
            "Then try again — protontricks will be installed automatically."
        )

    # Neither pipx nor pip — tell the user what to install first.
    cmd = _install_command("pipx")
    return True, (
        "protontricks is required but was not found, and neither pipx nor pip "
        "is available to install it automatically.\n\n"
        f"Install pipx with your package manager:\n  {cmd}\n\n"
        "Then try again — protontricks will be installed automatically."
    )


def _pip_module_available() -> bool:
    """Check if ``python3 -m pip`` is available without importing it."""
    python = shutil.which("python3")
    if not python:
        return False
    try:
        result = subprocess.run(
            [python, "-m", "pip", "--version"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _externally_managed() -> bool:
    """Check if this Python is marked as externally managed (PEP 668).

    Distributions like Arch Linux, Fedora, and Debian-based systems place a
    marker file (``EXTERNALLY-MANAGED``) in the stdlib path to prevent
    ``pip install`` from modifying the system Python.
    """
    if not shutil.which("python3"):
        return False
    try:
        import sysconfig

        stdlib_path = sysconfig.get_path("stdlib")
        if stdlib_path:
            marker = Path(stdlib_path) / "EXTERNALLY-MANAGED"
            if marker.is_file():
                return True
    except (OSError, ValueError):
        pass
    return False


def _pip_usable() -> bool:
    """Return True if pip can install packages without PEP 668 errors."""
    if not shutil.which("pip") and not _pip_module_available():
        return False
    return not _externally_managed()


# ---------------------------------------------------------------------------
# Combined check
# ---------------------------------------------------------------------------


def check_all_dependencies() -> list[str]:
    """Run all pre-flight checks.  Returns a list of error messages (empty = OK)."""
    errors: list[str] = []

    wt_err = check_winetricks()
    if wt_err:
        errors.append(wt_err)

    wine_err = check_wine()
    if wine_err:
        errors.append(wine_err)

    need_pt, pt_err = check_protontricks()
    if need_pt and pt_err:
        errors.append(pt_err)

    return errors
