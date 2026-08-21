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

from . import gui_settings
from .dependencies import configured_tool

DEFAULT_UMU_PREFIX = Path.home() / "Games" / "umu" / "umu-default"
DEFAULT_PROTON_PREFIX = Path.home() / "Games" / "proton"

STEAM_ROOT_CANDIDATES = (
    Path.home() / ".local" / "share" / "Steam",
    Path.home() / ".steam" / "steam",
    Path.home()
    / ".var"
    / "app"
    / "com.valvesoftware.Steam"
    / ".local"
    / "share"
    / "Steam",
)

PREFERRED_TARGETS = ("Anomaly (DX11-AVX)", "Anomaly (DX11)", "Anomaly Launcher")


class LaunchError(RuntimeError):
    """Raised when the launcher cannot be resolved or started."""


RUNNER_PREFIX_ERROR_MARKERS = (
    "wine client error:0: version mismatch",
    "your wine binary was not upgraded correctly",
    "wrong wineserver",
    "prefix has an invalid version",
    "concrt140.dll",
    "pfx.lock",
)

RUNNER_GRAPHICS_ERROR_MARKERS = (
    "setcolorspace1",
    "dxgi_color_space",
    "wined3d_swapchain",
    "d3d11_swapchain_setcolorspace",
)


def runner_prefix_error(log_text: str) -> bool:
    """Return whether launcher output indicates a runner/prefix mismatch."""
    text = log_text.lower()
    return any(marker in text for marker in RUNNER_PREFIX_ERROR_MARKERS)


def runner_graphics_error(log_text: str) -> bool:
    """Return whether output indicates WineD3D/DXVK/Vulkan failure."""
    text = log_text.lower()
    return any(marker in text for marker in RUNNER_GRAPHICS_ERROR_MARKERS)


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


def ensure_runner_prefix(runner: Runner) -> None:
    """Create and record a runner prefix, rejecting ownership conflicts."""
    raw = runner.env.get("STEAM_COMPAT_DATA_PATH") or runner.env.get("WINEPREFIX")
    if not raw:
        return
    prefix = Path(raw).expanduser()
    marker = prefix / ".commander-runner"
    owner = f"{runner.kind}:{runner.label}"
    try:
        prefix.mkdir(parents=True, exist_ok=True)
        if marker.is_file():
            saved_owner = marker.read_text(encoding="utf-8").strip()
            saved_kind = saved_owner.split(":", 1)[0] if saved_owner else ""
            if saved_kind and saved_kind != runner.kind:
                raise LaunchError(
                    "The selected runner is incompatible with this prefix.\n\n"
                    f"Prefix owner: {saved_owner}\n"
                    f"Selected runner: {owner}\n\n"
                    "Select the runner that created this prefix or configure a "
                    "separate prefix for the selected runner."
                )
        else:
            marker.write_text(owner + "\n", encoding="utf-8")
    except LaunchError:
        raise
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise LaunchError(f"Could not prepare runner prefix {prefix}: {exc}") from exc


def mo2_path_to_host(value: str) -> str:
    """Translate an MO2 ini path (``Z:/...``) to a host path."""
    if value.startswith("Z:"):
        rest = value[2:].replace("\\", "/")
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
    try:
        lines = ini.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, UnicodeError):
        return []
    for raw in lines:
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
        "umu": configured_tool("umu-run") or shutil.which("umu-run") or "",
        "wine": configured_tool("wine") or shutil.which("wine") or "",
        "gamemoderun": configured_tool("gamemoderun")
        or shutil.which("gamemoderun")
        or "",
    }


def _steam_library_paths() -> list[Path]:
    """Steam library folders: the main install plus any custom libraries."""
    roots: list[Path] = []
    overrides = gui_settings.load_gui_settings().get("tool_overrides") or {}
    steam_root = overrides.get("steam_root", "")
    if steam_root:
        try:
            resolved = Path(steam_root).expanduser().resolve()
            if resolved.is_dir():
                roots.append(resolved)
        except (OSError, RuntimeError):
            pass
    for candidate in STEAM_ROOT_CANDIDATES:
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    for root in list(roots):
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            try:
                library = Path(match.group(1)).resolve()
            except (OSError, RuntimeError):
                continue
            if library.is_dir() and library not in roots:
                roots.append(library)
    return roots


def find_steam_protons() -> list[tuple[str, str]]:
    """Discover Proton versions installed via Steam.

    Returns ``(label, path_to_proton_script)`` pairs, sorted by version
    descending (newest first). Only directories that actually contain a
    ``proton`` executable are reported (this skips e.g. "Proton BattlEye
    Runtime", which ships no launcher).
    """
    found: dict[str, str] = {}
    for library in _steam_library_paths():
        common = library / "steamapps" / "common"
        if not common.is_dir():
            continue
        try:
            entries = sorted(common.iterdir())
        except OSError:
            continue
        for entry in entries:
            proton = entry / "proton"
            if (
                not entry.is_dir()
                or not proton.is_file()
                or not os.access(proton, os.X_OK)
            ):
                continue
            label = entry.name
            if label.startswith("Proton"):
                label = label[len("Proton") :].strip()
            found.setdefault(f"Steam Proton {label}".strip(), str(proton.resolve()))

    def _version_key(item: tuple[str, str]) -> tuple[int, ...]:
        nums = re.findall(r"\d+", item[0])
        return tuple(int(x) for x in nums) or (0,)

    return sorted(found.items(), key=_version_key, reverse=True)


def find_extra_protons() -> list[tuple[str, str]]:
    """Discover GE-Proton builds in Steam's ``compatibilitytools.d`` folders.

    These are launched through ``umu-run`` with the ``PROTONPATH`` environment
    variable.  UMU-Proton builds are excluded.  Returns ``(label, proton_script)``
    pairs, sorted by label.
    """
    roots = list(_steam_library_paths())
    roots.append(DEFAULT_UMU_PREFIX)
    found: dict[str, str] = {}
    overrides = gui_settings.load_gui_settings().get("tool_overrides") or {}
    manual = overrides.get("umu_proton", "")
    if manual:
        build_dir = Path(manual).expanduser()
        if build_dir.is_file():
            build_dir = build_dir.parent
        proton = build_dir / "proton"
        if (
            proton.is_file()
            and os.access(proton, os.X_OK)
            and not build_dir.name.startswith("UMU-Proton")
        ):
            found[str(proton.resolve())] = build_dir.name
    for root in roots:
        tools = root / "compatibilitytools.d"
        if not tools.is_dir():
            continue
        try:
            entries = sorted(tools.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("UMU-Proton"):
                continue
            proton = entry / "proton"
            if (
                not entry.is_dir()
                or not proton.is_file()
                or not os.access(proton, os.X_OK)
            ):
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
        try:
            candidates += sorted(lutris.iterdir())
        except OSError:
            pass
    if bottles.is_dir():
        try:
            candidates += sorted(bottles.iterdir())
        except OSError:
            pass
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
    if not proton.is_file() or not os.access(proton, os.X_OK):
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
    wineserver = proton.parent / "files" / "bin" / "wineserver"
    if wineserver.is_file():
        env["WINESERVER"] = str(wineserver)
    prefix = prefix or str(DEFAULT_PROTON_PREFIX)
    prefix_path = Path(prefix).expanduser()
    try:
        prefix_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LaunchError(
            f"Could not create Proton compatibility-data directory {prefix_path}: {exc}"
        ) from exc
    env["STEAM_COMPAT_DATA_PATH"] = str(prefix_path)
    env["PROTON_USE_WINED3D"] = "0"
    label = proton.parent.name
    if label.startswith("Proton"):
        label = label[len("Proton") :].strip()
    return Runner("proton", f"Steam Proton {label}".strip(), [str(proton), "run"], env)


def _pick_umu_proton() -> str:
    """Pick the latest GE-Proton directory for umu, or ''.

    Only GE-Proton builds are considered.  Directories without a
    ``toolmanifest.vdf`` are skipped since ``umu-run`` validates
    ``PROTONPATH`` against that file.
    """
    candidates = []
    for label, script in find_extra_protons():
        if not label.startswith("GE-Proton"):
            continue
        build_dir = Path(script).parent
        if not (build_dir / "toolmanifest.vdf").is_file():
            continue
        nums = re.findall(r"\d+", label)
        key = tuple(int(x) for x in nums) or (0,)
        candidates.append((key, str(build_dir)))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def _umu_proton_runner(proton_script: str, prefix: str = "") -> Runner:
    """Build a Runner that launches a compatibilitytools.d Proton via umu-run.

    ``umu-run`` selects the Proton build from the ``PROTONPATH`` environment
    variable, which must point at the build *directory* (the one containing
    ``toolmanifest.vdf``). The prefix is honored via ``WINEPREFIX``.
    """
    proton = Path(proton_script)
    if not proton.is_file() or not os.access(proton, os.X_OK):
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
    env["PROTON_USE_WINED3D"] = "0"
    if prefix:
        prefix_path = Path(prefix).expanduser()
        try:
            prefix_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LaunchError(
                f"Could not create Wine prefix {prefix_path}: {exc}"
            ) from exc
        env["WINEPREFIX"] = str(prefix_path)
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
            return Runner("umu", "Proton", wrapper, env)
        if kind == "umu":
            raise LaunchError(
                "umu-run not found on PATH. Install umu-run (or launch via Steam) "
                "and try again."
            )
    if kind == "auto":
        protons = find_steam_protons()
        if protons:
            try:
                return _proton_runner(protons[0][1], wine_prefix)
            except LaunchError:
                pass  # fall through to try wine
    if kind in ("auto", "wine"):
        if avail.get("wine"):
            env = {}
            if wine_prefix:
                env["WINEPREFIX"] = wine_prefix
            return Runner("wine", "Wine", [avail["wine"]], env)
        if kind == "wine":
            raise LaunchError("wine not found on PATH.")
    raise LaunchError(
        "No game runner detected. Install umu-run and a GE-Proton build to launch the game."
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
    cwd = exe.working_directory or os.path.dirname(exe.binary) or "."
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
    if not command:
        raise LaunchError("No command specified")
    full_env = dict(os.environ)
    full_env.update(env)
    for key in list(full_env):
        upper = key.upper()
        if any(
            marker in upper for marker in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")
        ):
            full_env.pop(key, None)
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
                stderr=subprocess.STDOUT
                if log_path is not None
                else subprocess.DEVNULL,
            )
        except OSError as exc:
            raise LaunchError(f"Failed to start {command[0]!r}: {exc}") from exc
