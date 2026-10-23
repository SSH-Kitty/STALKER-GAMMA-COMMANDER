"""Install CLIKRΔD's ROMEO repack over an existing GAMMA install.

ROMEO ships as one archive holding ``GAMMA/`` and ``Anomaly/`` folders that
its guide says to extract over the player's own: ~170 extra ``xx-`` mods, its
own MO2 profile, a few settings files for MO2's overwrite folder and some
Anomaly appdata presets. This does the same, then the guide's two clean-up
steps (shader cache and Atmos ``.ltx`` files in Anomaly's appdata).

Updating follows the guide's update section: every ``xx-`` mod is deleted
first, since a mod a new release dropped would otherwise linger. The player's
MCM settings live in ``xx-CLIKRΔD MCM Values`` (MO2 saves a changed file back
into the mod it came from), so a changed copy is renamed out of the ``xx-``
range and put back in its place, with ROMEO's own turned off.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from threading import Event

from .coop import _swap_dir
from .mod_install import ModInstallError
from .modlist import read_lines, save_lines

MCM_MOD = "xx-CLIKRΔD MCM Values"
KEPT_MCM = "CLIKRΔD MCM Values (my settings)"
#: In the ROMEO profile folder; MO2 ignores files it doesn't know.
_STATE = "commander-romeo.json"


def find_romeo(names: list[str]) -> tuple[str, str] | None:
    """``(wrapper, mo2_profile)`` when an archive's member paths are ROMEO.

    Goes by layout: ``GAMMA/profiles/<name>/modlist.txt`` plus ``GAMMA/mods``,
    with at most one wrapper folder around them (the release has one).
    """
    found = None
    has_mods = set()
    for name in names:
        parts = name.replace("\\", "/").strip("/").split("/")
        wrapper = parts[0] if len(parts) > 1 and parts[0] != "GAMMA" else ""
        rest = parts[1:] if wrapper else parts
        if rest[:2] == ["GAMMA", "mods"] and len(rest) > 2:
            has_mods.add(wrapper)
        if len(rest) == 4 and rest[:2] == ["GAMMA", "profiles"] and rest[3] == "modlist.txt":
            found = (wrapper, rest[2])
    return found if found and found[0] in has_mods else None


def unpacked_size(archiver: Path, archive: Path) -> int | None:
    """Total unpacked bytes from 7-Zip's listing summary, None if unknown."""
    try:
        out = subprocess.run(
            [str(archiver), "l", "-p", "--", str(archive)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line for line in out.splitlines() if line.strip()]
    match = re.match(r"^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)?\s+(\d+)\s", lines[-1]) if lines else None
    return int(match.group(1)) if match else None


def _mcm_hash(gamma: Path) -> str | None:
    try:
        data = (gamma / "mods" / MCM_MOD / "gamedata" / "configs" / "axr_options.ltx").read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def mcm_changed(gamma: Path, profile: str) -> bool | None:
    """Whether the player changed ROMEO's MCM settings since COMMANDER
    installed it. None when unknown (installed by hand, or the file is gone)."""
    try:
        recorded = json.loads((gamma / "profiles" / profile / _STATE).read_text())["mcm_sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    current = _mcm_hash(gamma)
    return None if current is None else current != recorded


def old_mods(gamma: Path) -> list[Path]:
    """The ``xx-`` mod folders an update deletes."""
    mods = gamma / "mods"
    return sorted(p for p in mods.iterdir() if p.name.startswith("xx-") and p.is_dir()) if mods.is_dir() else []


def _keep_mcm(gamma: Path) -> None:
    """Rename the player's MCM mod out of the ``xx-`` range."""
    source = gamma / "mods" / MCM_MOD
    if not source.is_dir():
        return
    target, count = gamma / "mods" / KEPT_MCM, 1
    while target.exists():  # an older kept copy stays as it is
        count += 1
        target = gamma / "mods" / f"{KEPT_MCM} {count}"
    os.replace(source, target)


def _copy_number(name: str) -> int:
    """1 for KEPT_MCM itself, N for the "KEPT_MCM N" _keep_mcm() made."""
    rest = name[len(KEPT_MCM):].strip()
    return int(rest) if rest.isdigit() else 1 if not rest else 0


def _place_kept_mcm(modlist: Path, kept: list[str]) -> None:
    """Put kept MCM mods where ROMEO's is, on, and turn ROMEO's off."""
    lines = read_lines(modlist)
    try:
        index = lines.index(f"+{MCM_MOD}")
    except ValueError:
        return
    lines[index] = f"-{MCM_MOD}"
    # modlist.txt lists the winning mod first.
    lines[index:index] = [f"+{name}" for name in kept]
    save_lines(modlist, lines)


def _merge(src: Path, dest: Path, keep_existing: bool) -> None:
    """Move every file under ``src`` to the same place under ``dest``."""
    for path in sorted(src.rglob("*")):
        if path.is_dir():
            continue
        target = dest / path.relative_to(src)
        if keep_existing and target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), target)


def install(
    staging: Path, wrapper: str, profile: str, gamma: Path, anomaly: Path,
    cancel_event: Event | None = None, progress=None, keep_mcm: bool = False,
) -> None:
    """Put an extracted ROMEO archive (in ``staging``) into GAMMA and Anomaly.

    Over an existing ROMEO this is an update: the old ``xx-`` mods go first,
    and the old profile's mod list is kept as ``modlist.txt.before-update``.
    """
    src = staging / wrapper if wrapper else staging
    profile_dir = gamma / "profiles" / profile
    try:
        old_modlist = (profile_dir / "modlist.txt").read_bytes()
    except OSError:
        old_modlist = None
    if keep_mcm:
        _keep_mcm(gamma)
    for old in old_mods(gamma):
        shutil.rmtree(old)
    mods = sorted((src / "GAMMA" / "mods").iterdir())
    for count, mod in enumerate(mods, 1):
        if cancel_event is not None and cancel_event.is_set():
            raise ModInstallError("Mod installation cancelled")
        if progress:
            progress(count, len(mods))
        _swap_dir(mod, gamma / "mods" / mod.name)
    _swap_dir(src / "GAMMA" / "profiles" / profile, gamma / "profiles" / profile)
    # overwrite/ is shared by every MO2 profile and holds the player's own
    # MCM saves; only fill in what is missing. Everything else (the splash)
    # replaces, as the guide says.
    if (src / "GAMMA" / "overwrite").is_dir():
        _merge(src / "GAMMA" / "overwrite", gamma / "overwrite", keep_existing=True)
    shutil.rmtree(src / "GAMMA" / "mods", ignore_errors=True)
    shutil.rmtree(src / "GAMMA" / "profiles", ignore_errors=True)
    shutil.rmtree(src / "GAMMA" / "overwrite", ignore_errors=True)
    _merge(src / "GAMMA", gamma, keep_existing=False)
    if (src / "Anomaly").is_dir():
        _merge(src / "Anomaly", anomaly, keep_existing=False)
    clean_appdata(anomaly / "appdata")
    if old_modlist is not None:
        (profile_dir / "modlist.txt.before-update").write_bytes(old_modlist)
    # Newest copy first, so it wins: "<KEPT_MCM> 3" over "<KEPT_MCM> 2" over "<KEPT_MCM>".
    kept = sorted(
        (p.name for p in (gamma / "mods").iterdir() if p.name.startswith(KEPT_MCM)),
        key=_copy_number, reverse=True,
    )
    if kept:
        _place_kept_mcm(profile_dir / "modlist.txt", kept)
    (profile_dir / _STATE).write_text(json.dumps({"mcm_sha256": _mcm_hash(gamma)}))


def clean_appdata(appdata: Path) -> None:
    """The guide's steps 8-9: drop the shader cache and every Atmos .ltx."""
    shutil.rmtree(appdata / "shaders_cache", ignore_errors=True)
    if not appdata.is_dir():
        return
    for path in appdata.iterdir():
        name = path.name.lower()
        if name.startswith("atmos") and name.endswith(".ltx") and path.is_file():
            os.unlink(path)
