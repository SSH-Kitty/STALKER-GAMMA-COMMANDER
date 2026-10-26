"""Steam-style launch options ("VAR=1 wrapper %command% --arg").

Shared by the desktop Play page and Deck Mode: the one-click options both
offer, switching one on or off in the options text, and applying the text
to a built launch command. Qt-free.
"""

from __future__ import annotations

import functools
import json
import math
import os
import re
import shlex
from pathlib import Path

from . import overlays
from .deck_launch import in_game_mode

#: One-click launch options, in labelled groups: (group heading, ((label,
#: the text added to the launch options, tooltip), ...)).
GROUPS = (
    ("Display", (
        ("Image effects", "ENABLE_VKBASALT=1", "Sharpening, anti-aliasing and colour looks (vkBasalt). Choose the look in Settings > Graphics."),
        ("FSR upscaling", "WINE_FULLSCREEN_FSR=1", "Sharpen and upscale with AMD FSR when the game runs below your screen's resolution. Proton-GE only."),
        ("Gamescope", "gamescope -f --", "Run the game fullscreen inside Gamescope. Helps with resolution and alt-tab problems."),
        ("OpenGL", "PROTON_USE_WINED3D=1", "Use OpenGL instead of Vulkan (DXVK). Try it if the game won't start or looks broken on an older GPU."),
    )),
    ("Fixes", (
        ("No Esync", "PROTON_NO_ESYNC=1", "Turn off Esync. Can help if the game stutters or hangs."),
        ("No Fsync", "PROTON_NO_FSYNC=1", "Turn off Fsync. Can help if the game stutters or hangs."),
        ("Fix audio crackle", "PULSE_LATENCY_MSEC=60", "Bigger sound buffer. Fixes crackling or stuttering audio."),
        ("No Steam overlay", "LD_PRELOAD=", "Keep the Steam overlay out of the game when COMMANDER was started from Steam. Can fix stutter. Co-op keeps the overlay for Steam invites."),
    )),
    ("Overlay and logs", (
        ("Performance overlay", "mangohud", "Show FPS, frame times and temperatures over the game (MangoHud). Choose what it shows in Settings > Graphics."),
        ("FPS counter", "DXVK_HUD=fps", "Show DXVK's small FPS counter in the corner."),
        ("Proton log", "PROTON_LOG=1", "Write a Proton log file to your home folder. Useful for crash reports."),
        ("Quiet Wine logs", "WINEDEBUG=-all", "Turn off Wine's debug messages. Slightly less CPU work. Switches Proton log off, since it would leave the log empty."),
    )),
)

#: Options that undo each other: switching one on switches the other off.
#: WINEDEBUG=-all silences the Wine messages PROTON_LOG=1 is there to catch;
#: vkBasalt's on/off key only reads X11 input, so not in a native Wayland window.
_CONFLICTS = {
    "PROTON_LOG=1": "WINEDEBUG=-all", "WINEDEBUG=-all": "PROTON_LOG=1",
    "ENABLE_VKBASALT=1": "PROTON_ENABLE_WAYLAND=1", "PROTON_ENABLE_WAYLAND=1": "ENABLE_VKBASALT=1",
}

#: The "No Steam overlay" option. Co-op leaves it out (see
#: play_page.coop_shortcut_fields): Steam invites need the overlay.
NO_STEAM_OVERLAY = "LD_PRELOAD="
#: Co-op leaves this out too: the Steam overlay can't attach to a native
#: Wayland window, so it draws but never takes Shift+Tab.
NATIVE_WAYLAND = "PROTON_ENABLE_WAYLAND=1"
#: Options co-op drops.
COOP_DROPS = (NO_STEAM_OVERLAY, NATIVE_WAYLAND)

#: The Play page's Frame generation card: frame generation set up from
#: variables in the options text alone. The text keeps lsfg-vk's variable
#: names, as older COMMANDERs wrote them; apply() turns them into the
#: bundled frame generation's config (see _resolve_frame_gen).
FRAME_GEN = "LSFGVK_ENV=1"
FG_MULTIPLIER = "LSFGVK_MULTIPLIER"
FG_FLOW_SCALE = "LSFGVK_FLOW_SCALE"
FG_PERFORMANCE = "LSFGVK_PERFORMANCE_MODE"
#: Frame generation runs on Lossless Scaling's usual Lossless.dll.
#: lsfg-vk.dll is its lsfg-vk beta branch's, which it doesn't run.
LOSSLESS_DLL = "Lossless.dll"
BETA_DLL = "lsfg-vk.dll"
#: The frame generation layer, shipped in COMMANDER: MAKO Renderer
#: (GPL-3.0-or-later, its render-v4.6.0 release, unmodified; licenses in
#: licenses/MAKO). lsfg-vk 2's code, run with Lossless.dll.
_BUNDLED_FRAME_GEN = Path(__file__).resolve().parent.parent / "cli" / "usr" / "lib" / "mako" / "libmako-render.so"
#: Its explicit layer: enabled through VK_INSTANCE_LAYERS, under the
#: implicit ones. As an implicit layer, Mesa's device_select above it
#: crashes the game at its first frame.
FRAME_GEN_LAYER = "VK_LAYER_COMMANDER_mako"
#: Its multipliers stop at 5x.
MAX_MULTIPLIER = 5

_GAMESCOPE = ["gamescope", "-f", "--"]
_ENV_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


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


def frame_gen_ready(dll: Path | None = None) -> bool:
    """True when frame generation can run with dll (by default
    lossless_dll()): COMMANDER has it, and dll is a Lossless.dll."""
    dll = dll or lossless_dll()
    return (dll is not None and _BUNDLED_FRAME_GEN.is_file()
            and dll.name.lower() != BETA_DLL.lower() and native_dll(dll))


def install_layer(bundled: Path, library_path: Path, manifest: dict, manifest_path: Path) -> None:
    """Copy a bundled Vulkan layer out of COMMANDER, whose path changes with
    each AppImage run and may not be visible inside Proton's Steam Runtime
    container, and write its manifest, naming the copy by full path (a bare
    name only loads from system folders)."""
    from .atomic import write_bytes

    library = bundled.read_bytes()
    if not library_path.is_file() or library_path.read_bytes() != library:
        library_path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes(library_path, library, lock=False)
    manifest["layer"]["library_path"] = str(library_path)
    data = (json.dumps(manifest, indent=2) + "\n").encode()
    if not manifest_path.is_file() or manifest_path.read_bytes() != data:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes(manifest_path, data, lock=False)


def _multiplier(text: str) -> int:
    """The frame generation multiplier in text, kept to 2-MAX_MULTIPLIER:
    launch options are typed by hand, and a bad value fails the whole
    config."""
    try:
        return min(max(int(var(text, FG_MULTIPLIER) or 2), 2), MAX_MULTIPLIER)
    except ValueError:
        return 2


def _flow_scale(text: str) -> float:
    """The detail (flow scale) in text, kept to 0.25-1.0, as _multiplier."""
    try:
        value = float(var(text, FG_FLOW_SCALE) or 1.0)
    except ValueError:
        return 1.0
    return min(max(value, 0.25), 1.0) if math.isfinite(value) else 1.0




#: The profile in the frame generation config, and the games it is active
#: in: matched by executable, so not Wine's own processes in the launch
#: (explorer.exe stalls with frame generation active in it).
_PROFILE = "COMMANDER"
_GAMES = tuple(f"AnomalyDX{dx}{avx}.exe" for dx in ("11", "10", "9", "8") for avx in ("AVX", ""))


def _write_config(path: Path, lines: list[str]) -> Path:
    from .atomic import write_bytes

    data = ("\n".join(lines) + "\n").encode()
    # Unchanged, untouched: a new timestamp makes lsfg-vk rebuild its swapchain.
    if not path.is_file() or path.read_bytes() != data:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes(path, data, lock=False)
    return path


@functools.cache  # once per run: the launch preview resolves this on every edit
def _install_frame_gen() -> None:
    """Set up the bundled frame generation for this user, as an explicit layer."""
    install_layer(_BUNDLED_FRAME_GEN, _data_home() / "commander" / "mako" / _BUNDLED_FRAME_GEN.name, {
        "file_format_version": "1.2.1",
        "layer": {
            "name": FRAME_GEN_LAYER,
            "type": "GLOBAL",
            "api_version": "1.4.365",
            "implementation_version": "2",
            "description": "MAKO Renderer bundled with COMMANDER",
            "instance_extensions": [{"name": "VK_EXT_swapchain_colorspace", "spec_version": "4"}],
        },
    }, _data_home() / "vulkan" / "explicit_layer.d" / "commander-mako.json")


def _frame_gen_config(dll: Path, text: str) -> Path:
    """The frame generation config file (MAKO_CONFIG), from the variables in
    text. It is reread while the game runs, on and off included: off is
    frame_generation_enabled = false, which passes frames through."""
    from .config import settings_dir

    lines = [
        "version = 2",
        "[global]",
        f"dll = {json.dumps(str(dll))}",
        "[[profile]]",
        f"name = {json.dumps(_PROFILE)}",
        f"active_in = {json.dumps(list(_GAMES))}",
        f"frame_generation_enabled = {'true' if is_on(text, FRAME_GEN) else 'false'}",
        f"multiplier = {_multiplier(text)}",
        f"flow_scale = {_flow_scale(text)}",
        f"performance_mode = {'true' if var(text, FG_PERFORMANCE) == '1' else 'false'}",
    ]
    return _write_config(settings_dir() / "frame-gen.toml", lines)


def live_frame_gen(text: str) -> None:
    """The Play page's frame generation settings, to a running game: it
    rereads its config file, on and off included."""
    dll = lossless_dll()
    if frame_gen_ready(dll):
        _frame_gen_config(dll, text)


def _resolve_frame_gen(env: dict[str, str]) -> None:
    """Frame generation's variables in env, made into its config file, or
    left out when it can't run, so a launch never shows a black screen.

    Loaded even with frame generation off (frames then pass straight
    through), so it can be switched on in a running game, and a co-op
    shortcut never changes with it. First in VK_INSTANCE_LAYERS: nearest
    the game, over vkBasalt and MangoHud (see overlays._under_frame_gen)."""
    names = {key: env.pop(key) for key in [k for k in env if k.startswith("LSFGVK_")]}
    dll = lossless_dll()
    if not frame_gen_ready(dll):
        return
    _install_frame_gen()
    env["MAKO_CONFIG"] = str(_frame_gen_config(dll, shlex.join(f"{k}={v}" for k, v in names.items())))
    others = [layer for layer in env.get("VK_INSTANCE_LAYERS", "").split(":") if layer and layer != FRAME_GEN_LAYER]
    env["VK_INSTANCE_LAYERS"] = ":".join([FRAME_GEN_LAYER, *others])
    # As MAKO's own launcher: no lsfg-vk installed on the system next to
    # it, and Gamescope's WSI layer pacing over it slows the game to a crawl.
    env["DISABLE_LSFG"] = env["DISABLE_LSFGVK"] = env["DISABLE_GAMESCOPE_WSI"] = "1"


def dll_label(dll: Path) -> str:
    """The DLL as the Play page shows it: its folder and the Lossless Scaling
    version inside it, e.g. Lossless Scaling/Lossless.dll 3.2.2. Short, so
    the card never widens the window; the tooltip has the full path."""
    where = f"{dll.parent.name}/{dll.name}" if dll.parent.name else dll.name
    version = _dll_version(str(dll), dll.stat().st_mtime if dll.is_file() else 0)
    return f"{where} {version}" if version else where


@functools.lru_cache(maxsize=8)
def _dll_version(path: str, _mtime: float) -> str:
    """FileVersion from the DLL's version resource (UTF-16), trailing .0s
    dropped; "" without one. Cached per file and modification time."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    found = re.search(rb"F\0i\0l\0e\0V\0e\0r\0s\0i\0o\0n\0(?:\0\0)+((?:[0-9.]\0)+)", data)
    if not found:
        return ""
    version = found.group(1).decode("utf-16-le")
    while version.endswith(".0") and version.count(".") > 1:
        version = version[:-2]
    return version


def lossless_dll() -> Path | None:
    """Lossless Scaling's Lossless.dll: the one picked on the Play page,
    else one from a Steam library, else None. A picked lsfg-vk.dll (the
    beta branch's) gives way to a Lossless.dll next to it, else stays, for
    the Play page to say what to do."""
    from . import gui_settings
    from .launcher import _steam_library_paths

    chosen = gui_settings.load_gui_settings().get("lossless_dll") or ""
    if chosen and Path(chosen).is_file():
        chosen = Path(chosen)
        if chosen.name.lower() != BETA_DLL.lower():
            return chosen
        if (chosen.parent / LOSSLESS_DLL).is_file():
            return chosen.parent / LOSSLESS_DLL
    for library in _steam_library_paths():
        dll = library / "steamapps" / "common" / "Lossless Scaling" / LOSSLESS_DLL
        if dll.is_file():
            return dll
    return chosen or None


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


def apply(command: list[str], env: dict[str, str], text: str, *, game_mode: bool | None = None,
          resolve: bool = True) -> list[str]:
    """command with the options text applied; variables go into env.

    ``game_mode`` overrides in_game_mode() for the Gamescope option. Without
    ``resolve`` the text is taken as it is: no frame generation, MangoHud or
    vkBasalt set up from it (for text COMMANDER wrote itself, see for_steam)."""
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
    if resolve:
        _resolve_frame_gen(env)
        prefix = overlays.resolve(env, prefix)
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
    base_command = apply(["%command%"], base_env, base, game_mode=True, resolve=False)
    env: dict[str, str] = {}
    command = apply(["%command%"], env, steam, game_mode=True, resolve=False)
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
    would need a Steam restart on every switch. base is COMMANDER's own,
    taken as it is: set up again, its frame generation layer list would
    replace the one with vkBasalt and MangoHud in it.
    """
    base_env: dict[str, str] = {}
    command = apply(["%command%"], base_env, base, game_mode=True, resolve=False)
    env: dict[str, str] = {}
    command = apply(command, env, text, game_mode=True)
    env.update(base_env)
    assignments = [f"{key}={shlex.quote(value)}" for key, value in env.items()]
    return " ".join([*assignments, shlex.join(command)])
