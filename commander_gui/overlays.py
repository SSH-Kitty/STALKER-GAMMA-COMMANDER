"""MangoHud and vkBasalt for the game: their config files from Settings >
Graphics, and the copies COMMANDER bundles for systems without them.

Both are Vulkan layers, and Anomaly runs on Vulkan through DXVK. An
installed MangoHud or vkBasalt is used when there is one; otherwise the
bundled one (cli/usr/lib, see build-vulkan-layers.sh) is set up like
frame generation is (launch_options._install_frame_gen), under its own layer name
and switch, so it never clashes with one installed later. Qt-free.
"""

from __future__ import annotations

import functools
import hashlib
import os
import shutil
from pathlib import Path

from . import gui_settings
from .config import settings_dir

_LIB = Path(__file__).resolve().parent.parent / "cli" / "usr" / "lib"
BUNDLED_MANGOHUD = _LIB / "mangohud" / "libMangoHud.so"
BUNDLED_VKBASALT = _LIB / "vkbasalt" / "libvkbasalt.so"

#: The launch options that switch them on (launch_options.GROUPS).
MANGOHUD = "mangohud"
VKBASALT = "ENABLE_VKBASALT=1"

#: What the MangoHud card can show: key -> MangoHud's config lines, in the
#: order MangoHud shows them (legacy_layout=0 goes by the file's order).
MANGOHUD_ITEMS = {
    "time": ("time",),
    "gpu": ("gpu_stats", "gpu_temp"),
    "cpu": ("cpu_stats", "cpu_temp"),
    "memory": ("ram", "vram"),
    "procmem": ("procmem",),
    "battery": ("battery",),
    "fps": ("fps",),
    "frametime": ("frametime",),
    "graph": ("frame_timing",),
}
MANGOHUD_PRESETS = {
    "fps": ("fps",),
    "basic": ("gpu", "cpu", "fps", "graph"),
    "full": tuple(MANGOHUD_ITEMS),
}
MANGOHUD_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")
MANGOHUD_SIZES = {"small": 18, "medium": 24, "large": 32}
MANGOHUD_LAYOUTS = ("box", "bar")
#: X keysym names, as MangoHud and vkBasalt read them. None is bound in
#: Anomaly; F11, F12 (screenshot) and Pause are, so they're left out.
MANGOHUD_KEYS = ("Insert", "Scroll_Lock", "Page_Up")
VKBASALT_KEYS = ("Home", "End", "Delete")
#: The colour sliders, each -50 to 50 with 0 leaving the game's colours alone.
VKBASALT_COLOURS = ("saturation", "contrast", "brightness", "warmth", "tint")
#: Each look is a set of slider values; moving a slider makes it "custom".
VKBASALT_LOOKS = {
    "natural": (0, 0, 0, 0, 0),
    "vibrant": (25, 6, 2, 3, 0),
    "cinematic": (-10, 12, 0, 8, 2),
    "bleak": (-35, 8, 0, -8, -3),
    "warm": (8, 3, 2, 18, 2),
    "cold": (-10, 5, 0, -18, -2),
    "contrast": (10, 22, 2, 0, 0),
    "oldfilm": (-40, -8, 4, 15, 3),
    "zone": (-20, 6, 0, -4, -8),  # muted, a little green
}
VKBASALT_PRESETS = (*VKBASALT_LOOKS, "custom")
VKBASALT_SHARPNESS = {"off": 0.0, "light": 0.3, "medium": 0.5, "strong": 0.8}
VKBASALT_SHARPENERS = ("cas", "dls")
VKBASALT_AA = ("off", "fxaa", "smaa")

_DEFAULTS = {
    # "" until picked: "own" when the player has a MangoHud.conf, else basic.
    "mangohud_preset": "",
    "mangohud_items": list(MANGOHUD_PRESETS["basic"]),
    "mangohud_position": "top-left",
    "mangohud_size": "medium",
    "mangohud_key": MANGOHUD_KEYS[0],
    "mangohud_layout": "box",
    # Percent transparent: 0 is solid. MangoHud's own defaults.
    "mangohud_background_transparency": 50,
    "mangohud_transparency": 0,
    "vkbasalt_preset": "natural",
    "vkbasalt_sharpness": "medium",
    "vkbasalt_sharpener": "cas",
    "vkbasalt_aa": "off",
    "vkbasalt_key": VKBASALT_KEYS[0],
    "vkbasalt_start_off": False,
    **{f"vkbasalt_{name}": 0 for name in VKBASALT_COLOURS},
}


def _own_mangohud_conf() -> bool:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (config / "MangoHud" / "MangoHud.conf").is_file()


def settings() -> dict:
    """Settings > Graphics, with anything missing or hand-broken defaulted."""
    stored = gui_settings.load_gui_settings()
    data = {key: stored.get(key, default) for key, default in _DEFAULTS.items()}
    choices = {
        "mangohud_preset": ("custom", "own", *MANGOHUD_PRESETS),
        "mangohud_position": MANGOHUD_POSITIONS,
        "mangohud_size": tuple(MANGOHUD_SIZES),
        "mangohud_key": MANGOHUD_KEYS,
        "mangohud_layout": MANGOHUD_LAYOUTS,
        "mangohud_background_transparency": range(101),
        "mangohud_transparency": range(91),
        "vkbasalt_preset": VKBASALT_PRESETS,
        "vkbasalt_sharpness": tuple(VKBASALT_SHARPNESS),
        "vkbasalt_sharpener": VKBASALT_SHARPENERS,
        "vkbasalt_aa": VKBASALT_AA,
        "vkbasalt_key": VKBASALT_KEYS,
        "vkbasalt_start_off": (True, False),
        **{f"vkbasalt_{name}": range(-50, 51) for name in VKBASALT_COLOURS},
    }
    for key, allowed in choices.items():
        if isinstance(data[key], bool) != (key == "vkbasalt_start_off") or data[key] not in allowed:
            data[key] = _DEFAULTS[key]
    if not data["mangohud_preset"]:
        data["mangohud_preset"] = "own" if _own_mangohud_conf() else "basic"
    if data["mangohud_layout"] == "bar":
        # MangoHud draws a bar across the whole screen: "right" lands on the
        # left too (or off-screen without horizontal_stretch).
        data["mangohud_position"] = data["mangohud_position"].replace("right", "left")
    if data["mangohud_preset"] in MANGOHUD_PRESETS:
        data["mangohud_items"] = list(MANGOHUD_PRESETS[data["mangohud_preset"]])
    elif not isinstance(data["mangohud_items"], list):
        data["mangohud_items"] = list(_DEFAULTS["mangohud_items"])
    data["mangohud_items"] = [item for item in data["mangohud_items"] if item in MANGOHUD_ITEMS]
    # The sliders as saved: the custom look, kept while a named look is picked.
    data["vkbasalt_custom"] = tuple(data[f"vkbasalt_{name}"] for name in VKBASALT_COLOURS)
    if data["vkbasalt_preset"] in VKBASALT_LOOKS:
        data.update(zip((f"vkbasalt_{name}" for name in VKBASALT_COLOURS), VKBASALT_LOOKS[data["vkbasalt_preset"]]))
    return data


def mangohud_installed() -> bool:
    return bool(shutil.which("mangohud"))


def mangohud_available() -> bool:
    return mangohud_installed() or BUNDLED_MANGOHUD.is_file()


def vkbasalt_installed() -> bool:
    """A vkBasalt layer installed on the system or for this user."""
    from .launch_options import _data_home

    dirs = [str(_data_home()), *os.environ.get("XDG_DATA_DIRS", "").split(":"), "/usr/local/share", "/usr/share"]
    return any(
        (Path(d) / "vulkan" / "implicit_layer.d" / name).is_file()
        for d in dirs if d
        for name in ("vkBasalt.json", "vkBasalt.x86_64.json")
    )


def _write(path: Path, text: str) -> Path:
    """path holding text, rewritten only when it changed."""
    from .atomic import write_bytes

    data = text.encode()
    if not path.is_file() or path.read_bytes() != data:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes(path, data, lock=False)
    return path


def write_mangohud_config(data: dict | None = None) -> Path:
    """COMMANDER's MangoHud.conf, from Settings > Graphics. MangoHud watches
    the file, so a running game picks a change up within a moment."""
    return _write(settings_dir() / "mangohud.conf", mangohud_config(data or settings()))


def mangohud_config(data: dict) -> str:
    # legacy_layout=0: only the lines listed, not MangoHud's default set.
    lines = ["legacy_layout=0", f"position={data['mangohud_position']}",
             f"font_size={MANGOHUD_SIZES[data['mangohud_size']]}",
             f"toggle_hud={data['mangohud_key']}",
             f"background_alpha={(100 - data['mangohud_background_transparency']) / 100}",
             f"alpha={(100 - data['mangohud_transparency']) / 100}"]
    if data["mangohud_layout"] == "bar":
        lines.append("horizontal")
    for item, config in MANGOHUD_ITEMS.items():
        if item in data["mangohud_items"]:
            lines += config
    return "\n".join(lines) + "\n"


def _adjust(data: dict, r: float, g: float, b: float) -> tuple[float, float, float]:
    """One colour through the colour sliders."""
    brightness, contrast, saturation, warmth, tint = (
        data[f"vkbasalt_{name}"] / 100 for name in ("brightness", "contrast", "saturation", "warmth", "tint")
    )
    # Black and white stay put through brightness and contrast, so a dark
    # game's shadows aren't crushed: a gamma curve, then an S-curve.
    r, g, b = (c ** (1 - brightness) for c in (r, g, b))
    r, g, b = (c + contrast * (c * c * (3 - 2 * c) - c) for c in (r, g, b))
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    r, g, b = (luma + (c - luma) * (1 + saturation) for c in (r, g, b))
    # Warmth: red against blue. Tint: magenta against green. Gains, not
    # offsets, so near-black stays neutral instead of turning green or blue.
    return r * (1 + warmth * 0.4 + tint * 0.2), g * (1 - tint * 0.4), b * (1 - warmth * 0.4 + tint * 0.2)


def colours_changed(data: dict) -> bool:
    """Whether the colour sliders change the picture at all."""
    return any(data[f"vkbasalt_{name}"] for name in VKBASALT_COLOURS)


def lut(data: dict, size: int = 17) -> str:
    """A .cube colour lookup table for the colour sliders (red runs fastest)."""
    rows = [f"LUT_3D_SIZE {size}"]
    steps = [i / (size - 1) for i in range(size)]
    for b in steps:
        for g in steps:
            for r in steps:
                colour = _adjust(data, r, g, b)
                rows.append(" ".join(f"{min(max(c, 0.0), 1.0):.6f}" for c in colour))
    return "\n".join(rows) + "\n"


def vkbasalt_config(data: dict, lut_file: Path | None) -> str:
    sharpness = VKBASALT_SHARPNESS[data["vkbasalt_sharpness"]]
    # Smooth edges first, then colour, then sharpen what's left.
    effects = [data["vkbasalt_aa"]] if data["vkbasalt_aa"] != "off" else []
    effects += ["lut"] if lut_file else []
    effects += [data["vkbasalt_sharpener"]] if sharpness else []
    lines = [f"effects = {':'.join(effects) or 'cas'}",
             f"casSharpness = {sharpness}",
             f"dlsSharpness = {sharpness}",
             f"toggleKey = {data['vkbasalt_key']}",
             f"enableOnLaunch = {not data['vkbasalt_start_off']}"]
    if lut_file:
        # The look's own line too: a new look changes this file, and the
        # bundled vkBasalt reloads when it does.
        look = hashlib.sha1(lut_file.read_bytes(), usedforsecurity=False).hexdigest()[:12]
        lines += [f'lutFile = "{lut_file}"', f"# look {look}"]
    return "\n".join(lines) + "\n"


def write_vkbasalt_config(data: dict | None = None) -> Path:
    """COMMANDER's vkBasalt.conf, from Settings > Graphics. The bundled
    vkBasalt watches it, so a running game picks a change up."""
    data = data or settings()
    lut_file = _write(settings_dir() / "vkbasalt" / "look.cube", lut(data)) if colours_changed(data) else None
    return _write(settings_dir() / "vkbasalt" / "vkBasalt.conf", vkbasalt_config(data, lut_file))


#: Bundled layers: name -> (library, the variable that switches it on,
#: the entry points its manifest names).
_BUNDLED = {
    "mangohud": (BUNDLED_MANGOHUD, "COMMANDER_MANGOHUD", {
        "vkNegotiateLoaderLayerInterfaceVersion": "vkNegotiateLoaderLayerInterfaceVersion",
    }),
    "vkbasalt": (BUNDLED_VKBASALT, "COMMANDER_VKBASALT", {
        "vkGetInstanceProcAddr": "vkBasalt_GetInstanceProcAddr",
        "vkGetDeviceProcAddr": "vkBasalt_GetDeviceProcAddr",
    }),
}


@functools.cache  # once per run: the launch preview resolves this on every edit
def _install_bundled(name: str) -> bool:
    """Set the bundled layer up for this user; False when it isn't bundled."""
    from .launch_options import _data_home, install_layer

    library, switch, functions = _BUNDLED[name]
    if not library.is_file():
        return False

    def manifest(layer: dict) -> dict:
        return {"file_format_version": "1.0.0", "layer": {
            "type": "GLOBAL",
            "api_version": "1.3.0",
            "implementation_version": "1",
            "description": f"{name} bundled with COMMANDER",
            "functions": functions,
            **layer,
        }}

    install_layer(library, _data_home() / "commander" / library.name, manifest({
        "name": f"VK_LAYER_COMMANDER_{name}",
        "enable_environment": {switch: "1"},
        "disable_environment": {f"DISABLE_{switch}": "1"},
    }), _data_home() / "vulkan" / "implicit_layer.d" / f"commander-{name}.json")
    # The same layer as an explicit one, for _under_frame_gen.
    install_layer(library, _data_home() / "commander" / library.name, manifest({
        "name": f"VK_LAYER_COMMANDER_{name}_explicit",
    }), _data_home() / "vulkan" / "explicit_layer.d" / f"commander-{name}.json")
    return True


def _under_frame_gen(env: dict[str, str], name: str) -> None:
    """The bundled layer name under frame generation. Vulkan's loader puts
    implicit layers nearest the game, so there vkBasalt would sit between
    the game and frame generation (with lsfg-vk 2: a black screen) and
    MangoHud would count only the game's frames. Enabled through
    VK_INSTANCE_LAYERS after it, the explicit one goes under it: vkBasalt
    works on the generated frames too, and MangoHud counts them."""
    layer = f"VK_LAYER_COMMANDER_{name}_explicit"
    layers = [other for other in env.get("VK_INSTANCE_LAYERS", "").split(":") if other and other != layer]
    env["VK_INSTANCE_LAYERS"] = ":".join([*layers, layer])


def resolve(env: dict[str, str], prefix: list[str]) -> list[str]:
    """The launch's MangoHud and vkBasalt, set up from Settings > Graphics:
    their config files, and the bundled copies where none is installed."""
    mangohud = MANGOHUD in prefix
    vkbasalt = env.get("ENABLE_VKBASALT") == "1"
    if not (mangohud or vkbasalt):
        return prefix
    data = settings()
    frame_gen = "MAKO_CONFIG" in env  # frame generation loads
    # vkBasalt first: listed first is nearer the game, so it never colours
    # MangoHud's text.
    if vkbasalt:
        env["VKBASALT_CONFIG_FILE"] = str(write_vkbasalt_config(data))
        # The bundled one even over an installed one: only it reloads live.
        if _install_bundled("vkbasalt"):
            del env["ENABLE_VKBASALT"]
            if frame_gen:
                _under_frame_gen(env, "vkbasalt")
            else:
                env["COMMANDER_VKBASALT"] = "1"
    if mangohud:
        if data["mangohud_preset"] != "own":
            env["MANGOHUD_CONFIGFILE"] = str(write_mangohud_config(data))
        # With frame generation the bundled one even over an installed one,
        # to count the generated frames too.
        if (frame_gen or not mangohud_installed()) and _install_bundled("mangohud"):
            prefix = [token for token in prefix if token != MANGOHUD]
            if frame_gen:
                _under_frame_gen(env, "mangohud")
            else:
                env["COMMANDER_MANGOHUD"] = "1"
    return prefix
