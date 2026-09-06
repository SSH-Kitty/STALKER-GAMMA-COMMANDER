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

_PKG_MANAGER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("apt-get", "apt"),
    ("dnf", "dnf"),
    ("pacman", "pacman"),
    ("zypper", "zypper"),
    ("apk", "apk"),
    ("xbps-install", "xbps"),
    ("emerge", "emerge"),
    ("eopkg", "eopkg"),
    ("nix-env", "nix"),
)

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

    for command, manager in _PKG_MANAGER_COMMANDS:
        if shutil.which(command):
            return manager
    return None


#: PCI vendor IDs (from /sys) for the GPU vendors we give specific advice for.
_GPU_VENDOR_IDS: dict[str, str] = {
    "0x10de": "nvidia",
    "0x1002": "amd",
    "0x8086": "intel",
}


def detect_gpu_vendors() -> list[str]:
    """Return detected GPU vendor(s) ('nvidia'/'amd'/'intel'), sorted, deduped.

    Reads PCI vendor IDs directly from sysfs rather than asking Vulkan/OpenGL
    for the active driver, so it still works when no GPU driver is installed
    yet - exactly the situation this feeds install advice for. A hybrid
    laptop (Intel + NVIDIA/AMD) reports every vendor it finds, since both
    need their own driver.
    """
    vendors: set[str] = set()
    drm_dir = Path("/sys/class/drm")
    if not drm_dir.is_dir():
        return []
    try:
        entries = list(drm_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        # Only bare "cardN" entries are GPUs; "cardN-<connector>" entries
        # (e.g. card0-DP-1) are display outputs, not separate devices.
        if not entry.name.startswith("card") or "-" in entry.name:
            continue
        try:
            vendor_id = (entry / "device" / "vendor").read_text().strip().lower()
        except OSError:
            continue
        vendor = _GPU_VENDOR_IDS.get(vendor_id)
        if vendor:
            vendors.add(vendor)
    return sorted(vendors)


def _pkg_name(tool: str) -> str:
    """Return the package name for *tool* on the current distro."""
    mgr = detect_package_manager()
    names = _PKG_NAMES.get(mgr, {})
    return names.get(tool, tool)


# ---------------------------------------------------------------------------
# GPU-vendor-aware Vulkan driver guidance
# ---------------------------------------------------------------------------

#: Mesa's Vulkan drivers ship as one combined package covering AMD and Intel
#: together on every distro except Arch, which splits them per vendor.
_MESA_VULKAN_COMMANDS: dict[str, str] = {
    "apt": "sudo apt install mesa-vulkan-drivers",
    "dnf": "sudo dnf install mesa-vulkan-drivers",
    "zypper": "sudo zypper install Mesa-vulkan-drivers",
    "apk": "sudo apk add mesa-vulkan-lavapipe vulkan-tools",
    "xbps": "sudo xbps-install -S Vulkan-Headers vulkan-loader",
    "emerge": "sudo emerge media-libs/vulkan-loader",
    "eopkg": "sudo eopkg install vulkan-tools",
    "nix": "Install vulkan-loader and vulkan-tools from nixpkgs",
}
_MESA_VULKAN32_COMMANDS: dict[str, str] = {
    "apt": "sudo dpkg --add-architecture i386 && sudo apt update "
    "&& sudo apt install libvulkan1:i386",
    "dnf": "sudo dnf install vulkan-loader.i686 mesa-vulkan-drivers.i686",
    "zypper": "sudo zypper install libvulkan1-32bit",
    "apk": "sudo apk add mesa-vulkan-lavapipe:i386",
    "xbps": "sudo xbps-install -S Vulkan-Loader-32bit",
}
_PACMAN_VULKAN_BY_VENDOR: dict[str, str] = {
    "amd": "sudo pacman -S vulkan-radeon",
    "intel": "sudo pacman -S vulkan-intel",
    "nvidia": "sudo pacman -S nvidia-utils",
}
_PACMAN_VULKAN32_BY_VENDOR: dict[str, str] = {
    "amd": "sudo pacman -S lib32-vulkan-icd-loader lib32-vulkan-radeon",
    "intel": "sudo pacman -S lib32-vulkan-icd-loader lib32-vulkan-intel",
    "nvidia": "sudo pacman -S lib32-nvidia-utils",
}
_NVIDIA_VULKAN_ADVICE = (
    "Install the official NVIDIA driver (it includes Vulkan support) - see "
    "https://www.nvidia.com/Download/index.aspx or your distro's NVIDIA "
    "driver installation guide."
)
_NVIDIA_VULKAN32_ADVICE = (
    "Install the 32-bit NVIDIA driver libraries (bundled with the official "
    "driver package on most distros) - see your distro's NVIDIA driver "
    "installation guide."
)


def _vulkan_driver_command(
    manager: str | None,
    vendors: list[str],
    *,
    mesa_commands: dict[str, str],
    pacman_by_vendor: dict[str, str],
    pacman_unknown_hint: str,
    nvidia_advice: str,
) -> str:
    """Build a GPU-vendor-aware Vulkan driver install command.

    Arch (pacman) splits Mesa's Vulkan ICDs per vendor, so it needs the
    detected vendor to name the right package; NVIDIA's driver is a separate
    package everywhere regardless of vendor split, and its exact name varies
    enough by distro/repo setup that pointing at NVIDIA's own install guide
    is safer than guessing a command that might be stale or wrong.
    """
    if manager == "pacman":
        if not vendors:
            return pacman_unknown_hint
        commands = [
            pacman_by_vendor[vendor] for vendor in vendors if vendor in pacman_by_vendor
        ]
        return " && ".join(dict.fromkeys(commands)) or (
            "Install your GPU vendor's Vulkan drivers"
        )
    if vendors == ["nvidia"]:
        return nvidia_advice
    base = mesa_commands.get(manager or "", "Install your GPU vendor's Vulkan drivers")
    if "nvidia" in vendors:
        # Hybrid system (e.g. an Intel + NVIDIA laptop): Mesa covers the
        # integrated GPU, NVIDIA's proprietary driver is a separate step.
        return f"{base}\n(For the NVIDIA GPU: {nvidia_advice})"
    return base


def vulkan_driver_command(manager: str | None, vendors: list[str]) -> str:
    """64-bit Vulkan driver install command for the detected GPU vendor(s)."""
    return _vulkan_driver_command(
        manager,
        vendors,
        mesa_commands=_MESA_VULKAN_COMMANDS,
        pacman_by_vendor=_PACMAN_VULKAN_BY_VENDOR,
        pacman_unknown_hint=(
            "Install the Vulkan driver for your GPU: AMD -> "
            "'sudo pacman -S vulkan-radeon', Intel -> 'sudo pacman -S "
            "vulkan-intel', NVIDIA -> 'sudo pacman -S nvidia-utils'."
        ),
        nvidia_advice=_NVIDIA_VULKAN_ADVICE,
    )


def vulkan32_driver_command(manager: str | None, vendors: list[str]) -> str:
    """32-bit Vulkan driver install command for the detected GPU vendor(s)."""
    return _vulkan_driver_command(
        manager,
        vendors,
        mesa_commands=_MESA_VULKAN32_COMMANDS,
        pacman_by_vendor=_PACMAN_VULKAN32_BY_VENDOR,
        pacman_unknown_hint=(
            "Install the 32-bit Vulkan driver for your GPU: AMD -> 'sudo "
            "pacman -S lib32-vulkan-icd-loader lib32-vulkan-radeon', Intel "
            "-> 'sudo pacman -S lib32-vulkan-icd-loader lib32-vulkan-intel', "
            "NVIDIA -> 'sudo pacman -S lib32-nvidia-utils'."
        ),
        nvidia_advice=_NVIDIA_VULKAN32_ADVICE,
    )


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
    if mgr == "apk":
        return f"sudo apk add {pkg}"
    if mgr == "xbps":
        return f"sudo xbps-install -S {pkg}"
    if mgr == "emerge":
        return f"sudo emerge {pkg}"
    if mgr == "eopkg":
        return f"sudo eopkg install {pkg}"
    if mgr == "nix":
        return f"nix-env -iA nixpkgs.{pkg}"
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


def _umu_binary_valid() -> bool:
    """Return True if umu-run is on PATH and actually runs."""
    path = configured_tool("umu-run") or shutil.which("umu-run")
    if not path:
        return False
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def check_umu() -> tuple[bool, str | None]:
    """Check for umu-run (Proton launcher) availability.

    Returns ``(need_install, error_message)``:

    * ``(False, None)`` — umu-run is on PATH and validated.
    * ``(True, None)``  — missing but can be auto-installed via curl.
    * ``(True, msg)``   — missing and curl is not available; *msg* tells
      the user what to install first.
    """
    if _umu_binary_valid():
        return False, None
    if shutil.which("curl"):
        return True, None
    return True, (
        "umu-run is required but was not found, and curl is not available "
        "to download it automatically.\n\n"
        "Install curl with your package manager, then try again."
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
    """Check if the system's python3 is marked as externally managed (PEP 668).

    Distributions like Arch Linux, Fedora, and Debian-based systems place a
    marker file (``EXTERNALLY-MANAGED``) in the stdlib path to prevent
    ``pip install`` from modifying the system Python.

    This must inspect the interpreter that ``protontricks_install_command()``
    actually invokes (the ``python3`` resolved from PATH) rather than the
    interpreter currently running this code via ``sysconfig``. They are the
    same thing when running from a source checkout's venv, but the AppImage
    runs a bundled, private Python that carries no distro-injected marker
    file at all - checking it directly always reports "not managed" even on
    a PEP 668 system, silently approving a pip install that the system
    python3 that actually runs it then refuses to perform.
    """
    python = shutil.which("python3")
    if not python:
        return False
    try:
        result = subprocess.run(
            [python, "-c", "import sysconfig; print(sysconfig.get_path('stdlib'))"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    stdlib_path = result.stdout.strip()
    return bool(stdlib_path) and (Path(stdlib_path) / "EXTERNALLY-MANAGED").is_file()


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

    need_umu, umu_err = check_umu()
    if need_umu and umu_err:
        errors.append(umu_err)

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
