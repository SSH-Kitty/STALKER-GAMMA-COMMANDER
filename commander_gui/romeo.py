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

Uninstalling undoes the install: ROMEO's mods and MO2 profile go, its
appdata presets and splash screen go, GAMMA's Atmos files come back from the
download cache, and every COMMANDER profile on ROMEO goes back to G.A.M.M.A.
The overwrite files it filled in are left: they can't be told apart from the
player's own.
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
GAMMA_PROFILE = "G.A.M.M.A"
#: GAMMA's own splash, put aside by install() and back by uninstall().
_SPLASH_BACKUP = "splash.png.before-romeo"
# ponytail: the 2026.10.12 release's Anomaly files; record them at install
# if a later release adds more.
_APPDATA_FILES = ("clikrad_lean_presets.ltx", "dhf_weapon_presets.ltx", "dvm_weapon_presets.ltx")
_ATMOS_SOURCE = "G.A.M.M.A/modpack_patches/appdata/"


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
    # First install only: on an update splash.png is already ROMEO's.
    if old_modlist is None and (gamma / "splash.png").is_file() and not (gamma / _SPLASH_BACKUP).exists():
        os.replace(gamma / "splash.png", gamma / _SPLASH_BACKUP)
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


def romeo_profiles(gamma: Path) -> list[str]:
    """MO2 profiles that run ROMEO: installed by COMMANDER, or listing
    ROMEO's MCM mod. Never G.A.M.M.A itself."""
    root = gamma / "profiles"
    return [path.name for path in sorted(root.iterdir()) if is_romeo_profile(gamma, path.name)] if root.is_dir() else []


def is_romeo_profile(gamma: Path, profile: str) -> bool:
    path = gamma / "profiles" / profile
    if profile == GAMMA_PROFILE or not path.is_dir():
        return False
    try:
        listed = {line[1:] for line in read_lines(path / "modlist.txt")}
    except (OSError, ValueError):
        listed = set()
    return (path / _STATE).is_file() or MCM_MOD in listed


def installed(gamma: Path) -> bool:
    return bool(romeo_profiles(gamma) or old_mods(gamma))


def _restore_atmos(appdata: Path, cache: Path) -> bool:
    """Put GAMMA's Atmos .ltx files (which install removed) back from the
    Stalker_GAMMA repository in the download cache. False if it couldn't."""
    from .config import child_environment
    from .integrity import _gamma_repo

    git, repo = shutil.which("git"), _gamma_repo(cache)
    if git is None or repo is None:
        return False
    base = [git, "-c", f"safe.directory={repo}", "--git-dir", str(repo)]
    env = {**child_environment(), "GIT_TERMINAL_PROMPT": "0"}
    try:
        names = subprocess.run(
            [*base, "ls-tree", "--name-only", "HEAD", _ATMOS_SOURCE],
            capture_output=True, text=True, timeout=60, env=env, check=True,
        ).stdout.splitlines()
        appdata.mkdir(parents=True, exist_ok=True)
        for name in names:
            filename = name.rsplit("/", 1)[-1]
            if filename.lower().startswith("atmos") and filename.lower().endswith(".ltx"):
                data = subprocess.run(
                    [*base, "show", f"HEAD:{name}"], capture_output=True, timeout=60, env=env, check=True,
                ).stdout
                (appdata / filename).write_bytes(data)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def uninstall(gamma: Path, anomaly: Path, cache: Path) -> tuple[list[str], bool]:
    """Remove ROMEO from GAMMA and Anomaly; G.A.M.M.A is left as it was.

    Returns the MO2 profiles removed and whether GAMMA's Atmos files came
    back (if not, a GAMMA update restores them). The profiles go last, so a
    failure part-way leaves ROMEO findable for another try.
    """
    if not (gamma / "profiles" / GAMMA_PROFILE / "modlist.txt").is_file():
        raise ModInstallError("The G.A.M.M.A profile is missing. Run a GAMMA update first, then uninstall ROMEO.")
    profiles = romeo_profiles(gamma)
    mods = gamma / "mods"
    kept = [p for p in mods.iterdir() if p.name.startswith(KEPT_MCM) and p.is_dir()] if mods.is_dir() else []
    for mod in old_mods(gamma) + kept:
        shutil.rmtree(mod)
    appdata = anomaly / "appdata"
    for name in _APPDATA_FILES:
        (appdata / name).unlink(missing_ok=True)
    # Built from ROMEO's shaders.
    shutil.rmtree(appdata / "shaders_cache", ignore_errors=True)
    if (gamma / _SPLASH_BACKUP).is_file():
        os.replace(gamma / _SPLASH_BACKUP, gamma / "splash.png")
    else:
        # GAMMA ships no splash.png; MO2 falls back to its own.
        (gamma / "splash.png").unlink(missing_ok=True)
    atmos = _restore_atmos(appdata, cache)
    for name in profiles:
        shutil.rmtree(gamma / "profiles" / name)
    from .launcher import set_mo2_selected_profile

    set_mo2_selected_profile(str(gamma), GAMMA_PROFILE)
    return profiles, atmos


def back_to_gamma(settings, gamma: Path, removed: list[str]) -> None:
    """Point every COMMANDER profile that used a removed ROMEO profile at
    G.A.M.M.A, and save."""
    for profile in settings.profiles:
        if profile.mo2_profile in removed and Path(profile.gamma or "").expanduser() == gamma:
            profile.mo2_profile = GAMMA_PROFILE
    settings.save()
