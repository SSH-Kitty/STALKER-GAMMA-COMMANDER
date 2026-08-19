"""Game launching: detect the runner and launch Mod Organizer / the game.

GAMMA is a Windows mod pack run through Mod Organizer 2 (MO2). On Linux the
CLI wiki recommends running ``gamma/ModOrganizer.exe`` inside a Proton/Wine
prefix. This module detects the most common setups:

* Steam/UMU: ``umu-run`` with the default ``~/Games/umu/umu-default`` prefix,
  optionally wrapped in ``gamemoderun`` (the recommended path).
* Steam Proton: any Proton version installed via Steam is discovered
  (``steamapps/common/Proton*``) and launched with ``proton run <exe>``.
* Plain Wine: ``wine`` with an optional explicit ``WINEPREFIX``.

The game is launched through MO2's ``run -e <title>`` command so the virtual
file system is active and all GAMMA mods are loaded.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_UMU_PREFIX = Path.home() / "Games" / "umu" / "umu-default"
DEFAULT_PROTON_PREFIX = Path.home() / "Games" / "proton"

STEAM_ROOT_CANDIDATES = (
    Path.home() / ".local" / "share" / "Steam",
    Path.home() / ".steam" / "steam",
    Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
)

PREFERRED_TARGETS = ("Anomaly (DX11-AVX)", "Anomaly (DX11)", "Anomaly Launcher")


class LaunchError(RuntimeError):
    """Raised when the launcher cannot be resolved or started."""


RUNNER_PREFIX_ERROR_MARKERS = (
    "wine client error:0: version mismatch",
    "your wine binary was not upgraded correctly",
    "wrong wineserver",
    "prefix has an invalid version",
    "proton: upgrading prefix",
    "concrt140.dll",
)


def runner_prefix_error(log_text: str) -> bool:
    """Return whether launcher output indicates a runner/prefix mismatch."""
    text = log_text.lower()
    return any(marker in text for marker in RUNNER_PREFIX_ERROR_MARKERS)


def wine_prefix_for(kind: str, prefix: str) -> str:
    """Resolve a runner preset + configured prefix to the real WINEPREFIX.

    Steam Proton is driven through ``STEAM_COMPAT_DATA_PATH``; its Wine prefix
    is the ``pfx`` subdirectory of that path. Every other runner treats the
    configured path as the WINEPREFIX itself.
    """
    prefix = os.path.expanduser((prefix or "").strip())
    is_proton = kind.startswith("proton:")
    if not prefix:
        prefix = str(DEFAULT_PROTON_PREFIX if is_proton else DEFAULT_UMU_PREFIX)
    return str(Path(prefix) / "pfx") if is_proton else prefix


@dataclass
class Mo2Executable:
    title: str = ""
    binary: str = ""
    arguments: str = ""
    working_directory: str = ""


@dataclass
class Runner:
    kind: str
    label: str
    wrapper: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def mo2_path_to_host(value: str) -> str:
    """Translate an MO2 ini path (``Z:/...``) to a host path."""
    if value.startswith("Z:"):
        rest = value[2:]
        if not rest.startswith("/"):
            rest = "/" + rest
        return rest
    return value


def parse_mo2_executables(gamma_dir: str) -> list[Mo2Executable]:
    """Parse the ``[customExecutables]`` section of ModOrganizer.ini."""
    ini = Path(gamma_dir) / "ModOrganizer.ini"
    if not ini.is_file():
        return []
    entries: dict[int, Mo2Executable] = {}
    for raw in ini.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if "\\" not in line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        try:
            index_s, field_name = key.split("\\", 1)
            index = int(index_s)
        except ValueError:
            continue
        exe = entries.setdefault(index, Mo2Executable())
        field_name = field_name.lower()
        value = value.strip()
        if field_name == "title":
            exe.title = value
        elif field_name == "binary":
            exe.binary = mo2_path_to_host(value)
        elif field_name == "arguments":
            exe.arguments = value
        elif field_name == "workingdirectory":
            exe.working_directory = mo2_path_to_host(value)
    return [exe for exe in entries.values() if exe.title]


def available_commands() -> dict[str, str]:
    return {
        "umu": shutil.which("umu-run") or "",
        "wine": shutil.which("wine") or "",
        "gamemoderun": shutil.which("gamemoderun") or "",
    }


def _steam_library_paths() -> list[Path]:
    """Steam library folders: the main install plus any custom libraries."""
    roots: list[Path] = []
    for candidate in STEAM_ROOT_CANDIDATES:
        resolved = candidate.resolve()
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    for root in list(roots):
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        text = vdf.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            library = Path(match.group(1)).resolve()
            if library.is_dir() and library not in roots:
                roots.append(library)
    return roots


def find_steam_protons() -> list[tuple[str, str]]:
    """Discover Proton versions installed via Steam.

    Returns ``(label, path_to_proton_script)`` pairs, sorted by label. Only
    directories that actually contain a ``proton`` executable are reported
    (this skips e.g. "Proton BattlEye Runtime", which ships no launcher).
    """
    found: dict[str, str] = {}
    for library in _steam_library_paths():
        common = library / "steamapps" / "common"
        if not common.is_dir():
            continue
        for entry in sorted(common.iterdir()):
            proton = entry / "proton"
            if not entry.is_dir() or not proton.is_file():
                continue
            label = entry.name
            if label.startswith("Proton"):
                label = label[len("Proton"):].strip()
            found.setdefault(str(proton.resolve()), f"Steam Proton {label}".strip())
    return sorted((label, path) for path, label in found.items())


def find_extra_protons() -> list[tuple[str, str]]:
    """Discover Proton builds in Steam's ``compatibilitytools.d`` folders.

    These are the umu-managed builds (GE-Proton, UMU-Proton, ...); they are
    launched through ``umu-run`` with the ``PROTONPATH`` environment variable.
    Returns ``(label, proton_script)`` pairs, sorted by label.
    """
    roots = list(_steam_library_paths())
    roots.append(DEFAULT_UMU_PREFIX)
    found: dict[str, str] = {}
    for root in roots:
        tools = root / "compatibilitytools.d"
        if not tools.is_dir():
            continue
        for entry in sorted(tools.iterdir()):
            proton = entry / "proton"
            if not entry.is_dir() or not proton.is_file():
                continue
            found.setdefault(str(proton.resolve()), entry.name)
    return sorted((label, path) for path, label in found.items())


def find_wine_versions() -> list[tuple[str, str]]:
    """Discover Wine builds: Lutris/Bottles runners and ``/opt/wine*`` installs.

    Returns ``(label, bin/wine path)`` pairs, sorted by label. System Wine is
    intentionally omitted - it is already covered by the plain ``wine`` preset.
    """
    found: dict[str, str] = {}
    candidates: list[Path] = []
    lutris = Path.home() / ".local" / "share" / "lutris" / "runners" / "wine"
    bottles = Path.home() / ".local" / "share" / "bottles" / "runners"
    if lutris.is_dir():
        candidates += sorted(lutris.iterdir())
    if bottles.is_dir():
        candidates += sorted(bottles.iterdir())
    for entry in candidates:
        wine = entry / "bin" / "wine"
        if entry.is_dir() and wine.is_file():
            found.setdefault(str(wine.resolve()), f"Wine ({entry.name})")
    for entry in sorted(Path("/opt").glob("wine*")):
        wine = entry / "bin" / "wine"
        if entry.is_dir() and wine.is_file():
            found.setdefault(str(wine.resolve()), f"Wine ({entry.name})")
    return sorted((label, path) for path, label in found.items())


def _proton_runner(proton_script: str, prefix: str = "") -> Runner:
    """Build a Runner that executes ``proton run`` from a Steam Proton install."""
    proton = Path(proton_script)
    if not proton.is_file():
        raise LaunchError(f"Proton not found at {proton_script}")
    # <steamroot>/steamapps/common/<Proton>/proton - a Proton outside that
    # layout has no discoverable Steam root, so fail with a clear message
    # instead of an IndexError.
    parents = proton.resolve().parents
    if len(parents) < 4:
        raise LaunchError(
            f"{proton_script} is not inside a Steam library "
            "(expected steamapps/common/<Proton>/proton)."
        )
    steam_root = parents[3]
    env = {"STEAM_COMPAT_CLIENT_INSTALL_PATH": str(steam_root)}
    # Keep Wine client/server versions paired when MO2 starts child processes.
    # Without this, a stale system or older Proton wineserver can be selected.
    env["WINESERVER"] = str(proton.parent / "files" / "bin" / "wineserver")
    prefix = prefix or str(DEFAULT_PROTON_PREFIX)
    env["STEAM_COMPAT_DATA_PATH"] = prefix
    label = proton.parent.name
    if label.startswith("Proton"):
        label = label[len("Proton"):].strip()
    return Runner("proton", f"Steam Proton {label}".strip(), [str(proton), "run"], env)


def _pick_umu_proton() -> str:
    """Pick the best compatibilitytools.d Proton directory for umu, or ''.

    Prefers UMU-Proton (umbrella's own build) over GE-Proton; only directories
    with a ``toolmanifest.vdf`` are considered, since ``umu-run`` validates
    ``PROTONPATH`` against that file.
    """
    best: str = ""
    best_key = 10
    for label, script in find_extra_protons():
        build_dir = Path(script).parent
        if not (build_dir / "toolmanifest.vdf").is_file():
            continue
        key = 0 if label.startswith("UMU-Proton") else (1 if label.startswith("GE-Proton") else 2)
        if key < best_key:
            best_key, best = key, str(build_dir)
    return best


def _umu_proton_runner(proton_script: str, prefix: str = "") -> Runner:
    """Build a Runner that launches a compatibilitytools.d Proton via umu-run.

    ``umu-run`` selects the Proton build from the ``PROTONPATH`` environment
    variable, which must point at the build *directory* (the one containing
    ``toolmanifest.vdf``). The prefix is honored via ``WINEPREFIX``.
    """
    proton = Path(proton_script)
    if not proton.is_file():
        raise LaunchError(f"Proton not found at {proton_script}")
    build_dir = proton.parent
    if not (build_dir / "toolmanifest.vdf").is_file():
        raise LaunchError(
            f"Invalid Proton build at {build_dir} - toolmanifest.vdf not found."
        )
    avail = available_commands()
    if not avail.get("umu"):
        raise LaunchError(
            "umu-run not found on PATH. Install umu-run (or launch via Steam) "
            "and try again."
        )
    wrapper = []
    if avail.get("gamemoderun"):
        wrapper.append(avail["gamemoderun"])
    wrapper.append(avail["umu"])
    env = {"PROTONPATH": str(build_dir)}
    if prefix:
        env["WINEPREFIX"] = prefix
    return Runner("umu", proton.parent.name, wrapper, env)


def _wine_runner(wine_binary: str, prefix: str = "") -> Runner:
    """Build a Runner that uses an explicit Wine build (Lutris/Bottles//opt)."""
    wine = Path(wine_binary)
    if not wine.is_file():
        raise LaunchError(f"Wine not found at {wine_binary}")
    env = {}
    if prefix:
        env["WINEPREFIX"] = prefix
    label = wine.parent.parent.name
    return Runner("wine", f"Wine ({label})", [str(wine)], env)


def resolve_runner(kind: str, wine_prefix: str = "") -> Runner:
    """Resolve a runner preset (``auto``/``umu``/``wine``/``proton:``/``umup:``/``wine:``)."""
    if os.name == "nt":
        return Runner("native", "Native (Windows)")
    if kind.startswith("proton:"):
        return _proton_runner(kind.split(":", 1)[1], wine_prefix)
    if kind.startswith("umup:"):
        return _umu_proton_runner(kind.split(":", 1)[1], wine_prefix)
    if kind.startswith("wine:"):
        return _wine_runner(kind.split(":", 1)[1], wine_prefix)
    avail = available_commands()
    if kind in ("auto", "umu"):
        if avail.get("umu"):
            wrapper = []
            if avail.get("gamemoderun"):
                wrapper.append(avail["gamemoderun"])
            wrapper.append(avail["umu"])
            env = {}
            if wine_prefix:
                env["WINEPREFIX"] = wine_prefix
            proton_dir = _pick_umu_proton()
            if proton_dir:
                env["PROTONPATH"] = proton_dir
            return Runner("umu", "Steam / UMU Proton", wrapper, env)
        if kind == "umu":
            raise LaunchError(
                "umu-run not found on PATH. Install umu-run (or launch via Steam) "
                "and try again."
            )
    if kind == "auto":
        protons = find_steam_protons()
        if protons:
            return _proton_runner(protons[0][1], wine_prefix)
    if kind in ("auto", "wine"):
        if avail.get("wine"):
            env = {}
            if wine_prefix:
                env["WINEPREFIX"] = wine_prefix
            return Runner("wine", "Wine", [avail["wine"]], env)
        if kind == "wine":
            raise LaunchError("wine not found on PATH.")
    raise LaunchError(
        "No game runner detected. Install Wine or Steam/umu-run to launch the game."
    )


def default_launch_target(titles: list[str]) -> str:
    for preferred in PREFERRED_TARGETS:
        if preferred in titles:
            return preferred
    return titles[0] if titles else ""


def build_command(
    gamma_dir: str,
    runner: Runner,
    *,
    target: str | None = None,
    profile: str | None = None,
) -> tuple[list[str], dict[str, str], str]:
    """Build (command, env, cwd) to open MO2 and optionally auto-launch ``target``."""
    mo2 = Path(gamma_dir) / "ModOrganizer.exe"
    if not mo2.is_file():
        raise LaunchError(f"ModOrganizer.exe not found in {gamma_dir}")
    args = [str(mo2)]
    if profile and (Path(gamma_dir) / "profiles" / profile).is_dir():
        args += ["-p", profile]
    if target:
        args += ["run", "-e", target]
    return [*runner.wrapper, *args], dict(runner.env), gamma_dir


def build_direct_command(
    exe: Mo2Executable,
    runner: Runner,
) -> tuple[list[str], dict[str, str], str]:
    """Build (command, env, cwd) to run an executable directly (no MO2, no mods)."""
    if not exe.binary:
        raise LaunchError(f"No binary configured for {exe.title!r}")
    cwd = exe.working_directory or os.path.dirname(exe.binary)
    args = [exe.binary]
    if exe.arguments:
        # MO2 stores the argument string as the user typed it, so quoted
        # arguments containing spaces must survive splitting intact.
        try:
            args += shlex.split(exe.arguments)
        except ValueError:
            args += exe.arguments.split()
    return [*runner.wrapper, *args], dict(runner.env), cwd


#: Roll launcher.log over once it passes this size; it is append-only and is
#: read back tail-first to diagnose failed launches.
MAX_LOG_BYTES = 1 << 20


def _rotate_log(path: Path) -> None:
    """Roll ``path`` to ``path.1`` once it grows past :data:`MAX_LOG_BYTES`."""
    try:
        if path.is_file() and path.stat().st_size > MAX_LOG_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass


def launch_detached(
    command: list[str],
    env: dict[str, str],
    cwd: str,
    log_path: str | Path | None = None,
) -> subprocess.Popen:
    """Start a command detached from the GUI (survives GUI close).

    When ``log_path`` is given, the child's stdout/stderr are appended there so
    launch failures can be diagnosed; otherwise they are discarded.
    """
    full_env = dict(os.environ)
    full_env.update(env)
    # The log handle is closed as soon as Popen returns: the child has its own
    # inherited descriptor, so keeping ours open only leaks one per launch.
    with ExitStack() as stack:
        stdout: object = subprocess.DEVNULL
        if log_path is not None:
            log_path = Path(log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_log(log_path)
            stdout = stack.enter_context(
                open(log_path, "a", encoding="utf-8", errors="replace")
            )
        try:
            return subprocess.Popen(
                command,
                cwd=cwd,
                env=full_env,
                start_new_session=True,
                stdout=stdout,
                stderr=subprocess.STDOUT if log_path is not None else subprocess.DEVNULL,
            )
        except OSError as exc:
            raise LaunchError(f"Failed to start {command[0]!r}: {exc}") from exc
