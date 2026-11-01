"""Keep the user's own state across a CLI ``full-install`` or ``update apply``.

Both commands rewrite things the user owns:

* the MO2 profile's modlist.txt, replaced with GAMMA's stock list (mods the
  user added vanish from it, enable/disable choices reset);
* ModOrganizer.ini's ``[Settings]`` (MO2 theme and preferences), the
  executable picked in MO2's dropdown and each executable's arguments
  (e.g. DX11 instead of DX11-AVX, or ``-dbg``);
* Anomaly's engine executables, replaced with the ones GAMMA bundles, which
  can be older than modded exes the user installed themselves. Exes
  COMMANDER installed also need their ``.db0`` kept and GAMMA's loose
  gamedata cleared again (see ``modded_exes.after_gamma_update()``);
* on a ROMEO profile, the Atmos ``.ltx`` files GAMMA copies into Anomaly's
  appdata, which ROMEO's guide says must not be there.

``capture()`` runs before the CLI starts and ``restore()`` after it
succeeds. On failure or cancel the caller just calls ``discard()``, or keeps
the state for a resume: it is also saved beside the copies in the Anomaly
folder, so a resume after COMMANDER was closed still restores the user's own
modlist and exes instead of capturing the stock ones the failed run wrote.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .atomic import write_text
from .i18n import tr
from .modded_exes import GAMEDATA_DB, after_gamma_update
from .modlist import merge_after_update, modlist_path_for, read_lines, save_lines
from .romeo import KEPT_MCM, MCM_MOD, clean_appdata

#: Folder name prefix of the copies ``capture()`` keeps in the Anomaly folder.
_KEEP_PREFIX = ".commander-keep-"
#: The captured state, saved in the Anomaly folder for a resume.
_STATE_FILE = ".commander-keep-state.json"
_PATH_FIELDS = ("mods_dir", "modlist_path", "ini_path", "bin_dir", "exe_copy", "anomaly", "db_copy")


@dataclass
class KeptState:
    mods_dir: Path | None = None
    modlist_path: Path | None = None
    modlist: list[str] | None = None
    official: set[str] = field(default_factory=set)
    ini_path: Path | None = None
    ini_settings: dict[str, str] = field(default_factory=dict)
    selected_exe: str = ""
    exe_arguments: dict[str, str] = field(default_factory=dict)
    bin_dir: Path | None = None
    exe_copy: Path | None = None
    anomaly: Path | None = None
    keep_exes: bool = False
    db_copy: Path | None = None
    romeo: bool = False


def _engine_files(bin_dir: Path) -> list[Path]:
    """Anomaly's engine executables (and their debug symbols) in *bin_dir*."""
    try:
        return [
            path
            for path in bin_dir.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.name.lower().startswith("anomalydx")
            and path.suffix.lower() in (".exe", ".pdb")
        ]
    except OSError:
        return []


def _ini_settings(path: Path) -> dict[str, str]:
    """``[Settings]`` key -> raw line. Raw keeps MO2's QSettings quoting."""
    settings: dict[str, str] = {}
    in_section = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped.lower() == "[settings]"
        elif in_section and "=" in stripped:
            settings[stripped.split("=", 1)[0].strip()] = line
    return settings


def _ini_executables(lines: list[str]) -> dict[str, dict[str, str]]:
    """``[customExecutables]`` index -> {field: raw value}."""
    found: dict[str, dict[str, str]] = {}
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.lower()
        elif section == "[customexecutables]" and "\\" in stripped and "=" in stripped:
            key, _, value = stripped.partition("=")
            index, _, name = key.partition("\\")
            found.setdefault(index, {})[name.lower()] = value
    return found


def _is_path_key(key: str) -> bool:
    """Keys the CLI sets for this install's folders - never put back."""
    lowered = key.lower()
    return "directory" in lowered or "path" in lowered


def _save(state: KeptState) -> None:
    if state.anomaly is None:
        return
    data = asdict(state)
    for key in _PATH_FIELDS:
        data[key] = str(data[key]) if data[key] is not None else None
    data["official"] = sorted(state.official)
    try:
        write_text(state.anomaly / _STATE_FILE, json.dumps(data))
    except OSError:
        pass


def _load(anomaly: Path, gamma: str) -> KeptState | None:
    """The state a stopped run saved for this install, if any."""
    try:
        data = json.loads((anomaly / _STATE_FILE).read_text(encoding="utf-8"))
        state = KeptState(**{
            key: Path(value) if key in _PATH_FIELDS and value else value
            for key, value in data.items()
            if key in KeptState.__dataclass_fields__
        })
        state.official = set(state.official or ())
    except (OSError, ValueError, TypeError):
        return None
    if state.anomaly != anomaly or (gamma and state.ini_path and state.ini_path.parent != Path(gamma)):
        return None
    # A copy that is gone can't be put back.
    if state.exe_copy is not None and not state.exe_copy.is_dir():
        state.exe_copy = state.bin_dir = None
    if state.db_copy is not None and not state.db_copy.is_dir():
        state.db_copy = None
    return state


def capture(profile, *, keep_exes: bool = False, resume: bool = False) -> KeptState:
    """Snapshot what ``restore()`` puts back. Best-effort: never raises.

    With *resume*, the state saved by the run being resumed comes back
    instead, when there is one for this install.
    """
    from .launcher import mo2_selected_title
    from .updates import local_modpack_records

    anomaly_dir = Path(getattr(profile, "anomaly", "") or "").expanduser()
    if resume and getattr(profile, "anomaly", ""):
        saved = _load(anomaly_dir, getattr(profile, "gamma", "") or "")
        if saved is not None:
            return saved
    state = KeptState()
    gamma = getattr(profile, "gamma", "") or ""
    mo2_profile = getattr(profile, "mo2_profile", "") or "G.A.M.M.A"
    if gamma:
        path = modlist_path_for(gamma, mo2_profile)
        if path is not None and path.is_file():
            try:
                state.modlist = read_lines(path)
                state.modlist_path = path
                state.mods_dir = Path(gamma) / "mods"
            except (OSError, ValueError):
                pass
            else:
                state.romeo = bool({f"+{MCM_MOD}", f"+{KEPT_MCM}"} & set(state.modlist))
            records = local_modpack_records(gamma, mo2_profile) or {}
            state.official = {record.folder_name for record in records.values()}
        ini = Path(gamma) / "ModOrganizer.ini"
        if ini.is_file():
            try:
                state.ini_settings = _ini_settings(ini)
                state.ini_path = ini
                lines = ini.read_text(encoding="utf-8", errors="replace").splitlines()
                state.exe_arguments = {
                    fields["title"]: fields["arguments"]
                    for fields in _ini_executables(lines).values()
                    if fields.get("title") and fields.get("arguments")
                }
                state.selected_exe = mo2_selected_title(gamma)
            except OSError:
                pass
    anomaly = getattr(profile, "anomaly", "") or ""
    if anomaly:
        state.anomaly = Path(anomaly).expanduser()
        state.keep_exes = keep_exes
        if keep_exes:
            # Copies go in the Anomaly folder, on disk: /tmp is RAM on
            # SteamOS and Arch, and these are hundreds of MB. A run that
            # was abandoned left its copies behind; they go now.
            for stale in state.anomaly.glob(f"{_KEEP_PREFIX}*"):
                if stale.is_dir():
                    shutil.rmtree(stale, ignore_errors=True)
        db = state.anomaly / "db" / "mods" / GAMEDATA_DB
        if keep_exes and db.is_file():
            try:
                state.db_copy = Path(tempfile.mkdtemp(prefix=f"{_KEEP_PREFIX}db-", dir=state.anomaly))
                shutil.copy2(db, state.db_copy / db.name)
            except OSError:
                if state.db_copy is not None:
                    shutil.rmtree(state.db_copy, ignore_errors=True)
                state.db_copy = None
    if keep_exes and anomaly:
        bin_dir = Path(anomaly).expanduser() / "bin"
        files = _engine_files(bin_dir)
        if files:
            copy = None
            try:
                copy = Path(tempfile.mkdtemp(prefix=f"{_KEEP_PREFIX}exes-", dir=state.anomaly))
                for path in files:
                    shutil.copy2(path, copy / path.name)
            except OSError:
                if copy is not None:
                    shutil.rmtree(copy, ignore_errors=True)
            else:
                state.bin_dir = bin_dir
                state.exe_copy = copy
    _save(state)
    return state


def restore(state: KeptState) -> list[str]:
    """Put the captured state back after a successful CLI run.

    Returns one short note per thing restored or failed, for the log.
    """
    notes: list[str] = []
    if state.modlist is not None and state.modlist_path and state.mods_dir:
        try:
            current = read_lines(state.modlist_path)
            merged = merge_after_update(
                state.modlist,
                current,
                official_before=state.official,
                mods_dir=state.mods_dir,
            )
            if merged != current:
                save_lines(state.modlist_path, merged)
                notes.append(tr("Kept your own mods and enabled/disabled choices in modlist.txt."))
        except (OSError, ValueError) as exc:
            notes.append(tr("Could not restore your modlist choices: {exc}", exc=exc))
    if state.ini_settings and state.ini_path is not None and state.ini_path.is_file():
        try:
            notes.extend(_restore_ini_settings(state.ini_path, state.ini_settings))
        except OSError as exc:
            notes.append(tr("Could not restore MO2 settings: {exc}", exc=exc))
    if (state.selected_exe or state.exe_arguments) and state.ini_path is not None:
        try:
            notes.extend(
                _restore_launch_choices(
                    state.ini_path, state.selected_exe, state.exe_arguments
                )
            )
        except OSError as exc:
            notes.append(tr("Could not restore your MO2 executable choice: {exc}", exc=exc))
    if state.exe_copy is not None and state.bin_dir is not None:
        try:
            for path in state.exe_copy.iterdir():
                shutil.copy2(path, state.bin_dir / path.name)
            notes.append(tr("Kept your current engine executables."))
        except OSError as exc:
            notes.append(tr("Could not restore your engine executables: {exc}", exc=exc))
    if state.anomaly is not None:
        try:
            if state.db_copy is not None:
                (state.anomaly / "db" / "mods").mkdir(parents=True, exist_ok=True)
                shutil.copy2(state.db_copy / GAMEDATA_DB, state.anomaly / "db" / "mods" / GAMEDATA_DB)
            after_gamma_update(state.anomaly, state.keep_exes)
        except OSError as exc:
            notes.append(tr("Could not tidy up after keeping your modded exes: {exc}", exc=exc))
        if state.romeo:
            try:
                clean_appdata(state.anomaly / "appdata")
                notes.append(tr("Removed the Atmos files GAMMA copied back, as ROMEO needs."))
            except OSError as exc:
                notes.append(tr("Could not remove the Atmos files ROMEO doesn't want: {exc}", exc=exc))
    discard(state)
    return notes


def _restore_ini_settings(ini: Path, old: dict[str, str]) -> list[str]:
    text = ini.read_text(encoding="utf-8", errors="surrogateescape")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    wanted = {key: line for key, line in old.items() if not _is_path_key(key)}
    out: list[str] = []
    in_section = False
    seen: set[str] = set()
    changed = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_section:
                # Keys the CLI dropped from [Settings] go back at its end.
                missing = [wanted[k] for k in wanted if k not in seen]
                out.extend(missing)
                changed |= bool(missing)
                seen.update(wanted)
            in_section = stripped.lower() == "[settings]"
        elif in_section and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in wanted:
                seen.add(key)
                if wanted[key] != line:
                    line = wanted[key]
                    changed = True
        out.append(line)
    if in_section:
        missing = [wanted[k] for k in wanted if k not in seen]
        out.extend(missing)
        changed |= bool(missing)
    if not changed:
        return []
    write_text(ini, newline.join(out) + newline)
    return [tr("Kept your Mod Organizer settings (theme and preferences).")]


def _restore_launch_choices(ini: Path, selected: str, arguments: dict[str, str]) -> list[str]:
    """Re-pick *selected* in MO2's dropdown and put back executable arguments.

    Matched by title, since the CLI may renumber the executables.
    """
    from .launcher import set_mo2_selected_title

    if not ini.is_file():
        return []
    notes: list[str] = []
    if selected and set_mo2_selected_title(str(ini.parent), selected):
        notes.append(tr("Kept {selected} as your MO2 executable.", selected=selected))
    text = ini.read_text(encoding="utf-8", errors="surrogateescape")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    index_of = {
        fields.get("title"): index for index, fields in _ini_executables(lines).items()
    }
    wanted = {
        f"{index_of[title]}\\arguments": value
        for title, value in arguments.items()
        if title in index_of
    }
    out: list[str] = []
    section = ""
    changed = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.lower()
        elif section == "[customexecutables]":
            key, _, value = stripped.partition("=")
            if key in wanted and wanted[key] != value:
                line = f"{key}={wanted[key]}"
                changed = True
        out.append(line)
    if changed:
        write_text(ini, newline.join(out) + newline)
        notes.append(tr("Kept your MO2 launch arguments."))
    return notes


def discard(state: KeptState) -> None:
    if state.anomaly is not None:
        try:
            (state.anomaly / _STATE_FILE).unlink(missing_ok=True)
        except OSError:
            pass
    if state.db_copy is not None:
        shutil.rmtree(state.db_copy, ignore_errors=True)
        state.db_copy = None
    if state.exe_copy is not None:
        shutil.rmtree(state.exe_copy, ignore_errors=True)
        state.exe_copy = None
