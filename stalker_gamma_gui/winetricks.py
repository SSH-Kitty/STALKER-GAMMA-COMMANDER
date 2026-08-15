"""Winetricks helpers: runtime detection and tool/verb commands.

Mod Organizer and the game need the native Microsoft Visual C++ and DirectX
runtime DLLs, which a fresh Wine/Proton prefix does not ship (Wine only provides
stubs - MO2 aborts on ``concrt140.dll`` without the real VC++ redistributable).
These helpers drive ``winetricks`` against the GUI's configured prefix.
"""

from __future__ import annotations

import os
import shutil
import subprocess

#: Verbs installed by the "Install / Update Runtimes" action, in order.
WINETRICKS_VERBS = (
    "d3dcompiler_43",
    "d3dcompiler_47",
    "d3dx10",
    "d3dx11_43",
    "d3dx9",
    "vcrun2022",
)

_NOISE_PREFIXES = (
    "Using winetricks",
    "warning:",
    "wine:",
    "Wine:",
    "wine-",
)


def winetricks_binary() -> str:
    """Path to winetricks, or '' when it is not on PATH."""
    return shutil.which("winetricks") or ""


def protontricks_binary() -> str:
    """Path to protontricks, or '' when it is not on PATH."""
    return shutil.which("protontricks") or ""


def winetricks_install_command(verbs: tuple[str, ...] = WINETRICKS_VERBS) -> list[str]:
    """Build the ``winetricks -q <verbs>`` command line."""
    binary = winetricks_binary()
    if not binary:
        return []
    return [binary, "-q", *verbs]


def protontricks_install_command() -> list[str]:
    """User-level install for protontricks (pipx first, then pip --user)."""
    if shutil.which("pipx"):
        return ["pipx", "install", "protontricks"]
    return ["python3", "-m", "pip", "install", "--user", "protontricks"]


def check_winetricks_status(
    prefix: str,
    verbs: tuple[str, ...] = WINETRICKS_VERBS,
    timeout: int = 30,
) -> dict[str, bool]:
    """Return {verb: installed} for ``verbs`` in the prefix.

    Queries ``winetricks list-installed`` (authoritative - it reads the prefix's
    winetricks.log). File-presence checks are unreliable because Proton ships
    builtin copies of the same DLLs. Returns all-False when winetricks is
    missing or the query fails.
    """
    binary = winetricks_binary()
    result = {verb: False for verb in verbs}
    if not binary:
        return result
    env = dict(os.environ)
    if prefix:
        env["WINEPREFIX"] = prefix
    try:
        proc = subprocess.run(
            [binary, "list-installed"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return result
    tokens: set[str] = set()
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(_NOISE_PREFIXES):
            continue
        tokens.update(line.split())
    for verb in verbs:
        result[verb] = verb in tokens
    return result
