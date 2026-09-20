"""Winetricks helpers: runtime detection and tool/verb commands.

Mod Organizer and the game need the native Microsoft Visual C++ and DirectX
runtime DLLs, which a fresh Wine/Proton prefix does not ship (Wine only provides
stubs - MO2 aborts on ``concrt140.dll`` without the real VC++ redistributable).
These helpers drive ``winetricks`` against the GUI's configured prefix.

THE ONE RULE OF THIS MODULE: nothing here may execute a Wine binary that did
not build the prefix it is pointed at.

That rule exists because of a real incident. The old status probe ran
``winetricks list-installed`` with ``WINEPREFIX`` set to the Proton prefix and
no ``WINE=``, so it used the *system* wine. Winetricks runs
``wine cmd /c "echo init"`` before dispatching any command - even a read-only
one - and system wine, finding a prefix whose ``.update-timestamp`` was not
its own, performed its implicit prefix update and overwrote 97 DLLs Proton had
copied into ``system32``, ``ntdll.dll`` included, with its own builds. Every
Proton process then loaded a foreign ntdll and faulted on its first thread;
each fault spawned ``winedbg``, which faulted, which spawned another. The
probe ran on every Dashboard refresh, so the prefix was re-corrupted faster
than anything could repair it, and a full reinstall (which keeps the prefix)
changed nothing. The machine froze from memory exhaustion within two minutes
of pressing Play.

So: status is a file read, and installs go through the runner's own Wine.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .dependencies import _externally_managed, configured_tool

#: Verbs installed by the "Install Dependencies" action, in order.
WINETRICKS_VERBS = (
    "d3dcompiler_43",
    "d3dcompiler_47",
    "d3dx10",
    "d3dx11_43",
    "d3dx9",
    "quartz",
    "dx8vb",
    "vcrun2022",
)

#: Hardcoded UMU release version for zipapp downloads.
UMU_VERSION = "1.4.4"
UMU_ZIPAPP_URL = (
    f"https://github.com/Open-Wine-Components/umu-launcher/releases/"
    f"download/{UMU_VERSION}/umu-launcher-{UMU_VERSION}-zipapp.tar"
)

def winetricks_binary() -> str:
    """Path to winetricks, or '' when it is not on PATH."""
    return configured_tool("winetricks") or shutil.which("winetricks") or ""


def protontricks_binary() -> str:
    """Path to protontricks, or '' when it is not on PATH."""
    return configured_tool("protontricks") or shutil.which("protontricks") or ""


def umu_binary() -> str:
    """Path to umu-run, or '' when it is not on PATH."""
    return configured_tool("umu-run") or shutil.which("umu-run") or ""


def _proton_build_dir(runner) -> Path | None:
    """The Proton build a runner uses, or None for plain Wine / unknown."""
    proton_path = runner.env.get("PROTONPATH")
    if proton_path:
        return Path(proton_path)
    if runner.kind == "proton" and runner.wrapper:
        # wrapper is [<build>/proton, "run"]
        return Path(runner.wrapper[0]).parent
    return None


def winetricks_install_command(
    runner, verbs: tuple[str, ...] = WINETRICKS_VERBS
) -> tuple[list[str], dict[str, str]]:
    """Command and environment to install ``verbs`` with the *runner's* Wine.

    Returns ``([], {})`` when the needed tool is unavailable. The environment
    returned is the complete set of runner-specific variables the call needs;
    callers merge it over ``os.environ`` and add nothing Wine-related.

    umu / GE-Proton runners go through ``umu-run winetricks``, umu's own
    winetricks mode: it runs the winetricks bundled inside the Proton build,
    with that build's wine, inside the Steam runtime container, against the
    prefix umu manages. That is the only path that can install verbs into a
    Proton prefix without a second Wine build touching it.

    Steam Proton and plain-wine runners fall back to the system winetricks
    script, but with ``WINE``/``WINESERVER`` pinned to the runner's own
    binaries, which winetricks honours. What it must never do is what it did
    before: run winetricks bare, and let it pick up whatever ``wine`` is on
    PATH.
    """
    env: dict[str, str] = {"WINEDEBUG": "-all"}
    build = _proton_build_dir(runner)

    if runner.kind == "umu":
        umu = umu_binary()
        if not umu:
            return [], {}
        for key in ("PROTONPATH", "WINEPREFIX", "GAMEID", "STORE"):
            if key in runner.env:
                env[key] = runner.env[key]
        return [umu, "winetricks", *verbs], env

    binary = winetricks_binary()
    if not binary:
        return [], {}

    if runner.kind == "proton":
        if build is None:
            return [], {}
        wine = build / "files" / "bin" / "wine"
        wineserver = build / "files" / "bin" / "wineserver"
        if not wine.is_file():
            return [], {}
        env["WINE"] = str(wine)
        if wineserver.is_file():
            env["WINESERVER"] = str(wineserver)
        compat = runner.env.get("STEAM_COMPAT_DATA_PATH")
        if compat:
            env["WINEPREFIX"] = str(Path(compat) / "pfx")
        return [binary, "-q", *verbs], env

    # Plain wine: winetricks with the same binary that owns the prefix.
    if runner.wrapper:
        env["WINE"] = runner.wrapper[-1]
    if "WINEPREFIX" in runner.env:
        env["WINEPREFIX"] = runner.env["WINEPREFIX"]
    return [binary, "-q", *verbs], env


def protontricks_install_command() -> list[str]:
    """User-level install for protontricks (pipx first, then pip --user).

    Returns an empty list when neither pipx nor a usable pip is available
    (e.g. on systems with PEP 668 externally-managed Python).
    """
    if shutil.which("pipx"):
        return ["pipx", "install", "protontricks"]
    if _externally_managed():
        return []
    return ["python3", "-m", "pip", "install", "--user", "protontricks"]


def umu_install_command() -> list[str]:
    """Download umu-run zipapp to ``~/.local/bin/`` via curl + tar.

    Uses the universal zipapp tarball (no sudo required, works on any distro).
    Returns an empty list when curl is not available.
    """
    if not shutil.which("curl"):
        return []
    return [
        "bash",
        "-c",
        (
            # Download to a temp file with an overall time cap, then extract
            # to a staging file and atomically move into place so a stalled
            # or truncated transfer never leaves a broken umu-run behind.
            # The member is located by name rather than assumed at the tar
            # root: current releases nest it as "umu/umu-run", and a past
            # hardcoded root-level path silently failed extraction outright.
            "set -o pipefail && "
            "mkdir -p ~/.local/bin && "
            'tmp_tar="$(mktemp)" && '
            'tmp_bin="$(mktemp -p ~/.local/bin .umu-run.XXXXXX)" && '
            'trap \'rm -f "$tmp_tar" "$tmp_bin"\' EXIT && '
            f'curl -fL --retry 3 --connect-timeout 30 --max-time 600 '
            f'"{UMU_ZIPAPP_URL}" -o "$tmp_tar" && '
            'member="$(tar -tf "$tmp_tar" | grep -E "(^|/)umu-run$" | head -n1)" && '
            '[ -n "$member" ] && '
            'tar -xOf "$tmp_tar" "$member" > "$tmp_bin" && '
            'chmod +x "$tmp_bin" && '
            'mv -f "$tmp_bin" ~/.local/bin/umu-run && '
            'rm -f "$tmp_tar" && trap - EXIT'
        ),
    ]


def check_winetricks_status(
    prefix: str,
    verbs: tuple[str, ...] = WINETRICKS_VERBS,
    timeout: int = 30,
) -> dict[str, bool]:
    """Return {verb: installed} for ``verbs`` in the prefix.

    Reads ``<prefix>/winetricks.log`` directly. That file is the whole of what
    ``winetricks list-installed`` reports - the script just ``cat``s it - and
    reading it ourselves means no Wine process is ever started for a status
    check. See the module docstring for why that matters: the subprocess this
    replaced ran system wine inside the Proton prefix on every Dashboard
    refresh and corrupted it.

    ``timeout`` is accepted for signature compatibility and ignored; there is
    nothing left to time out.
    """
    del timeout
    result = {verb: False for verb in verbs}
    if not prefix:
        return result
    log = Path(prefix).expanduser() / "winetricks.log"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Never installed, or unreadable: nothing is known to be present.
        return result
    installed = {line.strip() for line in text.splitlines() if line.strip()}
    for verb in verbs:
        result[verb] = verb in installed
    return result


def check_winetricks_full_status(
    prefix: str,
    verbs: tuple[str, ...] = WINETRICKS_VERBS,
    timeout: int = 30,
) -> dict[str, bool]:
    """Return {name: installed} for verbs *and* tool availability (wine, protontricks, umu-run).

    Combines the ``winetricks.log`` read with instant ``shutil.which()``
    checks for wine, protontricks, and umu-run. Starts no process.
    """
    status = check_winetricks_status(prefix, verbs, timeout)
    status["wine"] = bool(configured_tool("wine") or shutil.which("wine"))
    status["protontricks"] = bool(protontricks_binary())
    status["umu"] = bool(umu_binary())
    return status
