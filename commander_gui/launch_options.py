"""Steam-style launch options ("VAR=1 wrapper %command% --arg").

Shared by the desktop Play page and Deck Mode: the one-click options both
offer, switching one on or off in the options text, and applying the text
to a built launch command. Qt-free.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .deck_launch import in_game_mode

#: One-click launch options, in labelled groups: (group heading, ((label,
#: the text added to the launch options, tooltip), ...)).
GROUPS = (
    ("Display", (
        ("Gamescope", "gamescope -f --", "Run the game fullscreen inside Gamescope. Helps with resolution and alt-tab problems."),
        ("FSR upscaling", "WINE_FULLSCREEN_FSR=1", "Sharpen and upscale with AMD FSR when the game runs below your screen's resolution. Proton-GE only."),
        ("Native Wayland", "PROTON_ENABLE_WAYLAND=1", "Run in a native Wayland window instead of XWayland. Experimental, Proton-GE 10 or newer. The Steam overlay doesn't work with it, so co-op leaves it off."),
        ("OpenGL", "PROTON_USE_WINED3D=1", "Use OpenGL instead of Vulkan (DXVK). Try it if the game won't start or looks broken on an older GPU."),
    )),
    ("Fixes", (
        ("No Esync", "PROTON_NO_ESYNC=1", "Turn off Esync. Can help if the game stutters or hangs."),
        ("No Fsync", "PROTON_NO_FSYNC=1", "Turn off Fsync. Can help if the game stutters or hangs."),
        ("Fix audio crackle", "PULSE_LATENCY_MSEC=60", "Bigger sound buffer. Fixes crackling or stuttering audio."),
        ("No Steam overlay", "LD_PRELOAD=", "Keep the Steam overlay out of the game when COMMANDER was started from Steam. Can fix stutter. Co-op keeps the overlay for Steam invites."),
    )),
    ("Overlay and logs", (
        ("MangoHud", "mangohud", "Show the MangoHud overlay (FPS, frame times, temperatures)."),
        ("FPS counter", "DXVK_HUD=fps", "Show DXVK's small FPS counter in the corner."),
        ("Proton log", "PROTON_LOG=1", "Write a Proton log file to your home folder. Useful for crash reports."),
        ("Quiet Wine logs", "WINEDEBUG=-all", "Turn off Wine's debug messages. Slightly less CPU work. Switches Proton log off, since it would leave the log empty."),
    )),
)

#: Options that undo each other: switching one on switches the other off.
#: WINEDEBUG=-all silences the Wine messages PROTON_LOG=1 is there to catch.
_CONFLICTS = {"PROTON_LOG=1": "WINEDEBUG=-all", "WINEDEBUG=-all": "PROTON_LOG=1"}

#: The "No Steam overlay" option. Co-op leaves it out (see
#: play_page.coop_shortcut_fields): Steam invites need the overlay.
NO_STEAM_OVERLAY = "LD_PRELOAD="
#: Co-op leaves this out too: the Steam overlay can't attach to a native
#: Wayland window, so it draws but never takes Shift+Tab.
NATIVE_WAYLAND = "PROTON_ENABLE_WAYLAND=1"
#: Options co-op drops.
COOP_DROPS = (NO_STEAM_OVERLAY, NATIVE_WAYLAND)

#: The Play page's Frame generation card: lsfg-vk set up from variables in
#: the options text alone, so it needs no lsfg-vk profile for the game.
#: The text always holds lsfg-vk 2's names; apply() turns them into what the
#: installed lsfg-vk and the DLL can actually run (see _resolve_frame_gen).
FRAME_GEN = "LSFGVK_ENV=1"
FG_MULTIPLIER = "LSFGVK_MULTIPLIER"
FG_FLOW_SCALE = "LSFGVK_FLOW_SCALE"
FG_PERFORMANCE = "LSFGVK_PERFORMANCE_MODE"
#: Vulkan layer manifests of lsfg-vk 2 and lsfg-vk 1.
_LSFG_LAYERS = {
    "2": Path("vulkan/implicit_layer.d/VkLayer_LSFGVK_frame_generation.json"),
    "1": Path("vulkan/implicit_layer.d/VkLayer_LS_frame_generation.json"),
}
#: lsfg-vk 2 reads lsfg-vk.dll (Lossless Scaling's lsfg-vk beta branch),
#: lsfg-vk 1 reads Lossless.dll (Lossless Scaling 3.1 and 3.2).
_LOSSLESS_DLLS = ("lsfg-vk.dll", "Lossless.dll")
#: lsfg-vk 1.0's library, shipped in COMMANDER (MIT, from its v1.0.0 release).
_BUNDLED_LSFG1 = Path(__file__).resolve().parent.parent / "cli" / "usr" / "lib" / "lsfg-vk-1" / "liblsfg-vk.so"
#: (path, size, mtime) -> whether lsfg-vk 2 loads that DLL's shaders.
_V2_CHECKS: dict[tuple[str, int, int], bool] = {}

_GAMESCOPE = ["gamescope", "-f", "--"]
_ENV_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def lsfg_versions() -> set[str]:
    """Installed lsfg-vk major versions: "2", "1", both or neither."""
    dirs = [str(_data_home()), *os.environ.get("XDG_DATA_DIRS", "").split(":"), "/usr/local/share", "/usr/share"]
    versions = {
        version for version, layer in _LSFG_LAYERS.items()
        if any((Path(d) / layer).is_file() for d in dirs if d)
    }
    if _BUNDLED_LSFG1.is_file():
        versions.add("1")
    return versions


def native_dll(path: Path) -> bool:
    """True for a Windows DLL that isn't a .NET assembly. Lossless Scaling's
    own LosslessScaling.dll is one, and holds no shaders."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if head[:2] != b"MZ":
        return False
    pe = int.from_bytes(head[0x3C:0x40], "little")
    if head[pe : pe + 4] != b"PE\0\0":
        return False
    optional = pe + 24
    pe32_plus = int.from_bytes(head[optional : optional + 2], "little") == 0x20B
    # Data directory 14 is the CLR header: only .NET assemblies have one.
    clr = optional + (112 if pe32_plus else 96) + 14 * 8
    return len(head) >= clr + 8 and int.from_bytes(head[clr + 4 : clr + 8], "little") == 0


def works_with_v2(dll: Path) -> bool:
    """True when lsfg-vk 2 can load the shaders in dll. Lossless Scaling
    3.1/3.2's Lossless.dll can't ("Unable to find base shader"): only the
    lsfg-vk branch's lsfg-vk.dll can, and a launch with the wrong one shows
    a black screen while lsfg-vk retries every frame."""
    from .config import child_environment

    try:
        stat = dll.stat()
    except OSError:
        return False
    key = (str(dll), stat.st_size, stat.st_mtime_ns)
    if key not in _V2_CHECKS:
        cli = shutil.which("lsfg-vk-cli")
        if cli is None:
            return native_dll(dll)
        try:
            result = subprocess.run(
                [cli, "benchmark", "-d", str(dll), "-w", "64", "-h", "64"],
                env=child_environment(), capture_output=True, timeout=3, check=False,
            )
            _V2_CHECKS[key] = result.returncode == 0
        except subprocess.TimeoutExpired:
            # Still benchmarking: it got past loading the shaders.
            _V2_CHECKS[key] = True
        except OSError:
            return native_dll(dll)
    return _V2_CHECKS[key]


def frame_gen_version(dll: Path | None = None) -> str | None:
    """The lsfg-vk ("2" or "1") that can run frame generation with dll
    (by default lossless_dll()), or None when neither can."""
    dll = dll or lossless_dll()
    if dll is None:
        return None
    versions = lsfg_versions()
    if "2" in versions and works_with_v2(dll):
        return "2"
    # lsfg-vk.dll is the lsfg-vk 2 branch's: only lsfg-vk 2 runs it.
    if "1" in versions and native_dll(dll) and dll.name.lower() != "lsfg-vk.dll":
        return "1"
    return None


@functools.cache  # once per run: the launch preview resolves this on every edit
def _install_lsfg1() -> None:
    """Set up the bundled lsfg-vk 1.0 for this user, if it isn't already.

    Copied out of COMMANDER, whose path changes with each AppImage run and
    may not be visible inside Proton's Steam Runtime container. The manifest
    names the library by full path (a bare name only loads from system
    folders) and only loads it when LSFG_LEGACY is set: without that every
    Vulkan app would load it.
    """
    from .atomic import write_bytes

    if not _BUNDLED_LSFG1.is_file():
        return
    library = _BUNDLED_LSFG1.read_bytes()
    library_path = _data_home() / "lsfg-vk-1" / "liblsfg-vk.so"
    if not library_path.is_file() or library_path.read_bytes() != library:
        library_path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes(library_path, library, lock=False)
    manifest = {
        "file_format_version": "1.0.0",
        "layer": {
            "name": "VK_LAYER_LS_frame_generation",
            "type": "GLOBAL",
            "api_version": "1.4.313",
            "library_path": str(library_path),
            "implementation_version": "1",
            "description": "Lossless Scaling frame generation layer",
            "functions": {
                "vkGetInstanceProcAddr": "layer_vkGetInstanceProcAddr",
                "vkGetDeviceProcAddr": "layer_vkGetDeviceProcAddr",
            },
            "enable_environment": {"LSFG_LEGACY": "1"},
            "disable_environment": {"DISABLE_LSFG": "1"},
        },
    }
    data = (json.dumps(manifest, indent=2) + "\n").encode()
    manifest_path = _data_home() / _LSFG_LAYERS["1"]
    if not manifest_path.is_file() or manifest_path.read_bytes() != data:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes(manifest_path, data, lock=False)


def _lsfg1_config() -> Path:
    """A config file of lsfg-vk 1's own: with none it reads lsfg-vk 2's
    ~/.config/lsfg-vk/conf.toml, fails on its version and switches off."""
    from .config import settings_dir

    path = settings_dir() / "lsfg-vk-1.toml"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("version = 1\n[global]\n", encoding="utf-8")
    return path


def _resolve_frame_gen(env: dict[str, str]) -> None:
    """Frame generation's variables in env, made into ones the installed
    lsfg-vk runs with the DLL: lsfg-vk 2's own, lsfg-vk 1's legacy ones, or
    none at all, so a DLL neither can use never launches to a black screen."""
    if not env.get("LSFGVK_ENV"):
        return
    names = {key: env.pop(key) for key in [k for k in env if k.startswith("LSFGVK_")]}
    dll = lossless_dll()
    version = frame_gen_version(dll)
    if version == "2":
        env.update(names)
        env.setdefault("LSFGVK_DLL_PATH", str(dll))
    elif version == "1":
        _install_lsfg1()
        env["LSFG_LEGACY"] = "1"
        env["LSFG_DLL_PATH"] = str(dll)
        env["LSFG_CONFIG"] = str(_lsfg1_config())
        for name in ("MULTIPLIER", "FLOW_SCALE", "PERFORMANCE_MODE"):
            if f"LSFGVK_{name}" in names:
                env[f"LSFG_{name}"] = names[f"LSFGVK_{name}"]
        # lsfg-vk 2 next to it stays out of the way.
        env["DISABLE_LSFGVK"] = "1"


def lossless_dll() -> Path | None:
    """Lossless Scaling's frame generation DLL: the one picked on the Play
    page, else one from a Steam library, else None."""
    from . import gui_settings
    from .launcher import _steam_library_paths

    chosen = gui_settings.load_gui_settings().get("lossless_dll") or ""
    if chosen and Path(chosen).is_file():
        return Path(chosen)
    for library in _steam_library_paths():
        for name in _LOSSLESS_DLLS:
            dll = library / "steamapps" / "common" / "Lossless Scaling" / name
            if dll.is_file():
                return dll
    return None


def var(text: str, key: str) -> str | None:
    """The value text gives variable key, or None."""
    return next((t.partition("=")[2] for t in split(text) if t.startswith(key + "=")), None)


def set_var(text: str, key: str, value: str | None) -> str:
    """text with variable key set to value, or taken out for None."""
    text = shlex.join(t for t in split(text) if not t.startswith(key + "="))
    return text if value is None else toggle(text, f"{key}={value}", True)


def frame_gen(text: str, on: bool) -> str:
    """text with frame generation switched on (at 2x unless a multiplier is
    set) or off, which takes all of lsfg-vk's variables out."""
    if on:
        text = toggle(text, FRAME_GEN, True)
        return text if var(text, FG_MULTIPLIER) else set_var(text, FG_MULTIPLIER, "2")
    return shlex.join(t for t in split(text) if not t.startswith("LSFGVK_"))


def split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _find(tokens: list[str], seq: list[str]) -> int:
    """Index where seq appears in tokens as a run, or -1."""
    for i in range(len(tokens) - len(seq) + 1):
        if tokens[i : i + len(seq)] == seq:
            return i
    return -1


def is_on(text: str, option: str) -> bool:
    return _find(split(text), shlex.split(option)) >= 0


def toggle(text: str, option: str, on: bool) -> str:
    """text with option switched on or off. An option can be several
    tokens ("gamescope -f --")."""
    seq = shlex.split(option)
    tokens = split(text)
    drops = [seq]
    if on and option in _CONFLICTS:
        drops.append(shlex.split(_CONFLICTS[option]))
    for drop in drops:
        at = _find(tokens, drop)
        if at >= 0:
            del tokens[at : at + len(drop)]
    if on:
        if "=" in seq[0]:
            # Variables go in front of the game, so before %command% when
            # the user wrote one.
            at = tokens.index("%command%") if "%command%" in tokens else len(tokens)
        else:
            # Wrappers go first, so gamescope wraps mangohud and not the
            # other way round.
            at = 0
        tokens[at:at] = seq
    return shlex.join(tokens)


def apply(command: list[str], env: dict[str, str], text: str, *, game_mode: bool | None = None) -> list[str]:
    """command with the options text applied; variables go into env.

    ``game_mode`` overrides in_game_mode() for the Gamescope option."""
    tokens = split(text.strip())
    if not tokens:
        return command
    # What comes after %command% belongs after the command, as its
    # arguments; it used to be moved in front of it along with the wrappers.
    suffix: list[str] = []
    if "%command%" in tokens:
        marker = tokens.index("%command%")
        tokens, suffix = tokens[:marker], [
            t for t in tokens[marker + 1 :] if t != "%command%"
        ]
    prefix: list[str] = []
    for token in tokens:
        if _ENV_VAR.match(token):
            key, _, value = token.partition("=")
            env[key] = value
        else:
            prefix.append(token)
    _resolve_frame_gen(env)
    # Game Mode already is a gamescope session; a nested one from the
    # Gamescope option only adds a second compositor.
    at = _find(prefix, _GAMESCOPE)
    if at >= 0 and (in_game_mode() if game_mode is None else game_mode):
        del prefix[at : at + len(_GAMESCOPE)]
    if suffix and len(command) >= 2 and command[-2] == "-e" and "run" in command[:-2]:
        # MO2's "run [--arguments=...] -e <target>": MO2 quits on a word
        # it doesn't know, so the game's words go into --arguments.
        command = list(command)
        args = next((i for i, t in enumerate(command) if t.startswith("--arguments=")), None)
        if args is None:
            command.insert(len(command) - 2, "--arguments=" + " ".join(suffix))
        else:
            command[args] += " " + " ".join(suffix)
        suffix = []
    return [*prefix, *command, *suffix]



def for_coop(text: str) -> str:
    """The options text as co-op uses it: without the options that break the
    Steam overlay (COOP_DROPS), which co-op needs for Steam invites."""
    for option in COOP_DROPS:
        text = toggle(text, option, False)
    return text


def extends(steam: str, base: str) -> bool:
    """True when Steam LaunchOptions ``steam`` is ``base`` plus some launch
    options (see for_steam)."""
    base_env: dict[str, str] = {}
    base_command = apply(["%command%"], base_env, base, game_mode=True)
    env: dict[str, str] = {}
    command = apply(["%command%"], env, steam, game_mode=True)
    return all(env.get(key) == value for key, value in base_env.items()) and (
        _find(command, base_command) >= 0
    )


def for_steam(text: str, base: str) -> str:
    """Steam LaunchOptions ``base`` with the options text merged in.

    Steam runs LaunchOptions through ``sh -c``, where variables only count
    in front of everything else, so both sets of variables lead; base's
    come second and win, since a shortcut can't run without them.
    Gamescope is left out in every mode: Steam runs the shortcut in Game
    Mode's own gamescope too, and a shortcut that changed with the mode
    would need a Steam restart on every switch.
    """
    base_env: dict[str, str] = {}
    command = apply(["%command%"], base_env, base, game_mode=True)
    env: dict[str, str] = {}
    command = apply(command, env, text, game_mode=True)
    env.update(base_env)
    assignments = [f"{key}={shlex.quote(value)}" for key, value in env.items()]
    return " ".join([*assignments, shlex.join(command)])
