"""Read/modify MO2 modlist.txt files.

ModOrganizer modlist files use '#' comments, '-Name' for disabled and
'+Name' for enabled mods. We preserve comments and unknown lines while
allowing per-mod status changes and deletion.
"""

from __future__ import annotations

import shutil
from itertools import pairwise
from pathlib import Path

from .atomic import write_text


def _valid_name(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and not any(ord(char) < 32 or ord(char) == 127 for char in name)
    )


def _reject_status_prefix(name: str) -> None:
    """Reject names that would collide with MO2's +/- status prefix."""
    if name[:1] in "+-":
        raise ValueError("Names cannot start with '+' or '-'")


def read_lines(path: str | Path) -> list[str]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File {path} doesn't exist")
    try:
        # Strict decode: silently replacing invalid bytes with U+FFFD would
        # permanently corrupt a non-UTF-8 mod name (e.g. a legacy Latin-1
        # modlist.txt) the moment this file is next saved.
        return path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{path} is not valid UTF-8 ({exc}). Fix the file's encoding "
            "before editing it here - saving over invalid characters would "
            "corrupt mod names permanently."
        ) from exc


def entries(lines: list[str]) -> list[tuple[str, str]]:
    """Return (status, name) pairs for each mod in the file."""
    return [info for info in map(_line_info, lines) if info is not None]


def _line_info(line: str) -> tuple[str, str] | None:
    """Return (status, name) for a mod line, None for comments/blank lines."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped[0] not in "+-":
        return None
    enabled = stripped[0] == "+"
    name = stripped[1:].strip()
    if not _valid_name(name):
        if name:
            raise ValueError("Mod names cannot contain path separators or control characters")
        return None
    return ("Enabled" if enabled else "Disabled", name)


def separator_name(name: str) -> str | None:
    """Return the category name if ``name`` is an MO2 separator, else None."""
    if name.endswith("_separator"):
        return name[: -len("_separator")]
    return None


def _category_labels(lines: list[str]) -> list[str | None]:
    """Return each line's category label (``None`` for non-mod lines).

    A separator names the run of mod lines immediately ABOVE it, back to
    the previous separator or the start of the file - matching real MO2 -
    not the run below it. This was verified against GAMMA's actual
    official modlist.txt: the mods sitting immediately before
    "1- Audio_separator" (the file's last separator) are literally audio
    content (SFX, voice lines, ambient music, radio), while nothing
    follows it (end of file). The mods after "G.A.M.M.A. Audio_separator"
    (an earlier one) are weapon mods, not audio - only the mods *before*
    it are audio/voice-themed. A separator labels what precedes it. This
    rule applies uniformly, including to the very first separator in the
    file: an earlier version special-cased it to always read "Uncategorized"
    so a freshly installed mod (inserted at file-top) would reliably show
    as uncategorized, but that special case broke as soon as any operation
    reordered the file - flip_priority() naturally moves whichever category
    was last to the front, and the special case then stripped that (real,
    populated) category's members into a phantom "Uncategorized" bucket.
    Mods genuinely have no category only when nothing ever closes them
    (trailing content after the last separator, or a file with no
    separators at all) - handled below, not here.
    """
    labels: list[str | None] = [None] * len(lines)
    pending: list[int] = []
    for idx, line in enumerate(lines):
        info = _line_info(line)
        if info is None:
            continue
        _status, name = info
        cat = separator_name(name)
        if cat is not None:
            # This separator closes the batch collected since the last one
            # (or file start) - it's the one that NAMES that batch.
            for pending_idx in pending:
                labels[pending_idx] = cat
            pending = []
        else:
            pending.append(idx)
    # Trailing mods with no closing separator (or no separators at all).
    for pending_idx in pending:
        labels[pending_idx] = "Uncategorized"
    return labels


def grouped(
    lines: list[str],
) -> list[tuple[str, list[tuple[str, str, int]]]]:
    """Group mod lines into categories in file order.

    MO2 separators (entries whose names end in ``_separator``) delimit the
    categories - see ``_category_labels()`` for the exact rule this
    follows. Each mod is ``(status, name, line_index)`` so edits/reorders
    can target the exact line in the file. Empty separator groups are
    retained; empty "Uncategorized" runs are not.
    """
    groups: list[tuple[str, list[tuple[str, str, int]]]] = []
    pending: list[tuple[str, str, int]] = []
    for idx, line in enumerate(lines):
        info = _line_info(line)
        if info is None:
            continue
        status, name = info
        cat = separator_name(name)
        if cat is not None:
            # This separator closes the batch collected since the previous
            # one (or file start) - it's the one that NAMES that batch.
            groups.append((cat, pending))
            pending = []
        else:
            pending.append((status, name, idx))
    # Trailing mods with no closing separator (or no separators at all).
    if pending:
        groups.append(("Uncategorized", pending))
    return groups


def count_mods(lines: list[str], mods_dir: str | Path | None = None) -> tuple[int, int]:
    """Return (enabled, total) counts of real mods, excluding separators.

    Uses the same "is this a mod, or a category separator" rule as
    grouped() (separator_name(name) is not None), so this always agrees
    with what the Mod Manager tree displays.

    ``mods_dir``, when given, excludes any entry whose mod folder doesn't
    actually exist on disk - a mod listed in modlist.txt but never
    actually extracted (e.g. one archive that failed during install while
    everything else succeeded) was never really installed, and counting
    it inflates the total past what the user actually has on disk.

    Counts distinct mod NAMES, not raw lines: GAMMA's own official
    modlist.txt has been confirmed to list at least one mod twice (e.g.
    "G.A.M.M.A. Vehicles in Darkscape") - there is only one real mod/
    folder for it, and MO2 counts a name once regardless of how many
    times it appears in the file, so this must too or it overcounts by
    one per duplicate relative to MO2's own (and the community's) count.
    """
    total = enabled = 0
    seen: set[str] = set()
    base = Path(mods_dir) if mods_dir is not None else None
    for status, name in entries(lines):
        if separator_name(name) is not None:
            continue
        if base is not None and not (base / name).is_dir():
            continue
        if name in seen:
            continue
        seen.add(name)
        total += 1
        if status == "Enabled":
            enabled += 1
    return enabled, total


#: Only these top-level subfolders of a mod are actually virtualized into
#: the game by MO2 (same convention as mod_install.py's
#: _GAME_DATA_FOLDER_NAMES) - a loose file sitting at a mod's own root
#: (meta.ini, a README, a screenshot, a changelog) is packaging noise the
#: game engine never reads, never a real conflict. Confirmed against a
#: real profile: "meta.ini" alone exists in 539 of 777 mod folders and
#: swamped every scan with one meaningless "meta.ini" entry listing
#: dozens of unrelated mods.
_CONFLICT_SCAN_ROOTS = {"appdata", "bin", "db", "gamedata"}


def find_enabled_mod_file_conflicts(
    lines: list[str], mods_dir: str | Path
) -> list[tuple[str, list[str]]]:
    """Find files that exist under 2+ currently-enabled mods' folders.

    Not full MO2-style conflict resolution (that also needs each mod's
    own on/off overwrite rules and is a much bigger project) - just "what
    is the current load order actually overriding," which is already
    useful on its own since nothing else in the app surfaces this at all.
    Only looks inside each mod's _CONFLICT_SCAN_ROOTS subfolders - the
    part of a mod that actually reaches the game.

    Mods are listed file-order (this file's top-to-bottom order), which
    is priority order high-to-low - see grouped()'s own note on file-order
    vs MO2 on-screen order: file-top is MO2's on-screen bottom, the
    HIGHEST priority. So for any returned entry, its first mod name is
    the one that currently wins that file.
    """
    mods_dir = Path(mods_dir)
    enabled_names = [
        name
        for status, name in entries(lines)
        if status == "Enabled" and separator_name(name) is None
    ]
    file_owners: dict[str, list[str]] = {}
    for name in enabled_names:
        mod_dir = mods_dir / name
        if not mod_dir.is_dir():
            continue
        for child in mod_dir.iterdir():
            if not child.is_dir() or child.name.lower() not in _CONFLICT_SCAN_ROOTS:
                continue
            for path in child.rglob("*"):
                if path.is_file():
                    rel = f"{child.name}/{path.relative_to(child).as_posix()}"
                    file_owners.setdefault(rel, []).append(name)
    return [(rel, owners) for rel, owners in file_owners.items() if len(owners) > 1]


def summarize_mod_conflicts(
    conflicts: list[tuple[str, list[str]]],
) -> list[tuple[str, str, int]]:
    """Collapse per-file conflicts into per-mod-pair file counts.

    find_enabled_mod_file_conflicts() returns one row per conflicting
    file - a mod pair that overlaps across dozens of gamedata files (an
    audio or animation overhaul touching hundreds of scripts, say) shows
    up as the exact same two mod names repeated dozens of times, which is
    the real reason that table gets so long: not that there are many
    distinct relationships, only that a handful of them each span many
    files. Confirmed against a real profile: 11073 per-file rows collapse
    to 1030 distinct (winner, overridden) pairs. Returns
    ``(winner, overridden, file_count)``, sorted by file_count descending
    (the most significant overlaps first), then by name for a stable
    order among ties.
    """
    counts: dict[tuple[str, str], int] = {}
    for _path, owners in conflicts:
        winner = owners[0]
        for loser in owners[1:]:
            key = (winner, loser)
            counts[key] = counts.get(key, 0) + 1
    return sorted(
        ((winner, loser, count) for (winner, loser), count in counts.items()),
        key=lambda row: (-row[2], row[0], row[1]),
    )


def move(lines: list[str], line_index: int, delta: int) -> list[str]:
    """Move the mod at ``line_index`` by ``delta`` (-1 up, +1 down).

    Swaps it with the adjacent mod line inside the same category; separator
    boundaries and comment/blank lines are never crossed. Returns a new list.
    """
    for _, mods in grouped(lines):
        positions = [idx for _, _, idx in mods]
        if line_index not in positions:
            continue
        index = positions.index(line_index)
        neighbour = index + delta
        if not (0 <= neighbour < len(positions)):
            return list(lines)
        out = list(lines)
        out[line_index], out[positions[neighbour]] = (
            out[positions[neighbour]],
            out[line_index],
        )
        return out
    return list(lines)


def set_status_at(lines: list[str], line_index: int, enabled: bool) -> list[str]:
    """Flip the enabled/disabled prefix of the mod line at ``line_index``."""
    if not 0 <= line_index < len(lines):
        return list(lines)
    out = list(lines)
    info = _line_info(out[line_index])
    if info is None:
        return out
    prefix = "+" if enabled else "-"
    out[line_index] = f"{prefix}{info[1]}"
    return out


def delete_at(lines: list[str], line_index: int) -> list[str]:
    """Remove the line at ``line_index``."""
    return [line for i, line in enumerate(lines) if i != line_index]


def reorder_to_original(lines: list[str], original: list[str]) -> list[str]:
    """Reorder the mods shared with *original* back to the original order.

    Only mod entries present in both lists (the GAMMA mods) are permuted so
    they resume the relative order they had in *original*.  Everything else
    keeps its exact position: user-installed mods, separators (new categories),
    comments, and blank lines.  Each mod keeps its own enabled/disabled prefix,
    and mods missing from *lines* are not re-added.
    """
    gamma_order = [name for _, name in entries(original)]
    # positions/current_names: the existing gamma-mod lines in *lines*, in
    # their current file order - reordering only ever permutes this exact
    # multiset among itself (by a stable sort on each name's rank in
    # *original*), so it can never drop or duplicate a line. A previous
    # version paired ascending positions against a separately-built
    # "target" name list via zip(); GAMMA's own official modlist.txt has
    # been confirmed to list at least one mod name twice (see
    # count_mods()/_update_count()'s dedup), and whenever *original* and
    # *lines* disagreed on how many times a name repeated, that flat
    # zip() silently misaligned every pairing after it - dropping a real
    # mod and duplicating another instead of just reordering them.
    positions: list[int] = []
    current_names: list[str] = []
    by_name: dict[str, str] = {}
    for index, line in enumerate(lines):
        info = _line_info(line)
        if info is None or info[1] not in gamma_order:
            continue
        positions.append(index)
        current_names.append(info[1])
        by_name[info[1]] = line
    if len(current_names) <= 1:
        return list(lines)
    new_names = sorted(current_names, key=gamma_order.index)
    out = list(lines)
    for position, name in zip(positions, new_names, strict=True):
        out[position] = by_name[name]
    return out


def rename_mod(lines: list[str], old_name: str, new_name: str) -> list[str]:
    """Rename a mod in the modlist without touching its folder on disk.

    Keeps the mod's enabled/disabled status.  Raises ``ValueError`` if the new
    name is invalid, duplicates an existing entry, or would turn the entry into
    an MO2 separator.  Returns the original list when the mod is not found.
    """
    if not _valid_name(new_name):
        raise ValueError("Mod names cannot contain path separators or control characters")
    _reject_status_prefix(new_name)
    if new_name.endswith("_separator"):
        raise ValueError("A mod name cannot end in '_separator'")
    if new_name == old_name:
        return list(lines)
    if any(mod_name == new_name for _, mod_name in entries(lines)):
        raise ValueError(f"Mod {new_name!r} is already in the modlist")
    out = list(lines)
    for index, line in enumerate(out):
        info = _line_info(line)
        if info is not None and info[1] == old_name:
            prefix = "+" if info[0] == "Enabled" else "-"
            out[index] = f"{prefix}{new_name}"
    return out


def save_lines(path: str | Path, lines: list[str]) -> None:
    """Write the modlist atomically.

    modlist.txt is the only record of the user's load order, so a partial
    write (crash, full disk) must never be able to truncate it. The temp file
    is written alongside the target so ``replace`` stays on one filesystem.
    """
    path = Path(path)
    write_text(path, "\n".join(lines) + "\n")


def modlist_path_for(gamma: str, mo2_profile: str) -> Path | None:
    """Resolve <gamma>/profiles/<mo2_profile>/modlist.txt, path-escape safe.

    Returns None for an invalid/empty ``mo2_profile`` name rather than
    raising - callers that only want "is there a modlist to work with"
    can treat both "no profile configured" and "path was unsafe" the
    same way.
    """
    if not mo2_profile or mo2_profile in {".", ".."} or "/" in mo2_profile or "\\" in mo2_profile:
        return None
    try:
        profiles_root = (Path(gamma) / "profiles").resolve()
        path = profiles_root / mo2_profile / "modlist.txt"
        path.parent.resolve().relative_to(profiles_root)
    except (OSError, ValueError):
        return None
    return path


def seed_new_mo2_profile(
    gamma_dir: str | Path, mo2_profile: str, source_profile: str = "G.A.M.M.A"
) -> bool:
    """Copy an existing MO2 profile's modlist.txt into a brand-new one.

    Real Mod Organizer 2's own "New Profile" dialog offers to copy the load
    order from an existing profile. Nothing in COMMANDER or the bundled CLI
    does this for a COMMANDER profile created with a not-yet-existing MO2
    profile name (``config create`` only writes settings.json - confirmed by
    running it directly, it never touches the gamma folder), so a fresh
    profile's Mod Manager would otherwise start out completely empty instead
    of showing the pack's default mod selection.

    Also best-effort copies the CLI's own "modpack_maker_list.txt"/".json"
    record of what a full install actually put there, if present:
    Updates/Dashboard read that file (not modlist.txt) to show real update
    status - without it, a profile that's really just a copy of an already
    fully-installed one looks completely uninstalled to them ("No modpack
    list found in this profile. Run a full install..."), even though the
    mods it just inherited really are already installed under
    *gamma_dir*/mods.

    Never overwrites: does nothing if *mo2_profile* already has its own
    modlist.txt (an existing profile, or one a previous call already
    seeded), or if *source_profile* has none to copy (nothing installed at
    *gamma_dir* yet - a subsequent Full Install creates its own profile from
    scratch). Returns whether a file was actually copied.
    """
    base = Path(gamma_dir)
    source_dir = base / "profiles" / source_profile
    dest_dir = base / "profiles" / mo2_profile
    source = source_dir / "modlist.txt"
    destination = dest_dir / "modlist.txt"
    if destination.exists() or not source.is_file():
        return False
    try:
        lines = read_lines(source)
    except (OSError, ValueError):
        return False
    save_lines(destination, lines)
    for name in ("modpack_maker_list.txt", "modpack_maker_list.json"):
        src_file = source_dir / name
        dest_file = dest_dir / name
        if src_file.is_file() and not dest_file.exists():
            try:
                shutil.copy2(src_file, dest_file)
            except OSError:
                pass
    return True


def add_mod(
    lines: list[str],
    name: str,
    *,
    enabled: bool = False,
    category: str | None = None,
) -> list[str]:
    """Add a newly installed mod to the MO2 list.

    New user-installed mods start disabled so they cannot unexpectedly alter a
    working GAMMA setup.  The caller can enable the entry after reviewing it.

    MO2's own modlist.txt is written with the file's FIRST line as the
    HIGHEST-priority mod (rendered at the BOTTOM of MO2's on-screen list)
    and the LAST line as the lowest-priority mod (rendered at the TOP) -
    confirmed directly against Mod Organizer 2's own source
    (Profile::doWriteModlist()/refreshModStatus() in profile.cpp: "the
    priority are reversed to match the plugin list ... since the mod list
    is written in reverse order"). The UI mirrors this by rendering
    grouped()'s output bottom-to-top (see mod_manager_page._populate_tree()),
    so file position and on-screen position are opposite ends of the list.

    With no *category* (the default), the mod is appended to the very END
    of the file - matching where a freshly installed mod actually lands in
    real MO2: top of the left pane, lowest priority, freely movable into
    any category afterward. It lands in the file's trailing "nothing
    closes it" zone, which grouped() already renders as "Uncategorized"
    with no special-casing needed - and stays correct even after the file
    is later reordered (e.g. by Flip Priority).

    With an explicit *category*, the mod is added as a member of that
    category: a category's members sit immediately above its separator
    (not below - see grouped()), so the mod is inserted right before the
    separator, making it that category's last-in-file (i.e. lowest-priority,
    screen-topmost-within-that-category) member. If the category doesn't
    exist yet, it's created by inserting the mod and a new closing
    separator right after the file's last existing separator (or at file
    start if there is none) - not unconditionally at the very end, which
    would silently swallow any mods already sitting in the file's trailing
    "nothing closes it" zone into the brand-new category instead of
    leaving them uncategorized (see _trailing_uncategorized_start()).
    """
    if not _valid_name(name):
        raise ValueError("Mod names cannot contain path separators or control characters")
    _reject_status_prefix(name)
    if name.endswith("_separator"):
        raise ValueError("A mod name cannot end in '_separator'")
    if any(mod_name == name for _, mod_name in entries(lines)):
        raise ValueError(f"Mod {name!r} is already in the modlist")
    line = ("+" if enabled else "-") + name
    out = list(lines)
    if category is None:
        out.append(line)
        return out
    clean_category = category.strip()
    if not _valid_name(clean_category):
        raise ValueError("Category names cannot contain path separators or control characters")
    separator = f"{clean_category}_separator"
    sep_idx = _category_separator_index(out, clean_category)
    if sep_idx is not None:
        # Category exists: insert as its last member, right before its
        # separator (members sit above their separator, not below).
        out.insert(sep_idx, line)
        return out
    # Category missing: insert the mod and a new closing separator right
    # after the file's last existing separator (or at file start if there
    # is none) - NOT unconditionally at the very end. Appending at the
    # absolute end would place this new separator after any mods already
    # sitting in the file's trailing "nothing closes it" zone (rendered as
    # "Uncategorized" - see grouped()), and a separator claims everything
    # above it back to the previous one: those already-uncategorized mods
    # would be silently swallowed into the brand-new category instead of
    # staying uncategorized.
    boundary = _trailing_uncategorized_start(out)
    out[boundary:boundary] = [line, f"-{separator}"]
    return out


def _trailing_uncategorized_start(lines: list[str]) -> int:
    """Index where the file's trailing "nothing closes it" zone begins.

    That's right after the last existing separator, or the very start of
    the file if there are none at all - see add_mod()'s "category missing"
    branch for why a new category is inserted here instead of unconditionally
    at the end.
    """
    for index in range(len(lines) - 1, -1, -1):
        info = _line_info(lines[index])
        if info is not None and separator_name(info[1]) is not None:
            return index + 1
    return 0


def _category_separator_index(lines: list[str], category: str) -> int | None:
    """Return the line index of the first separator for *category*."""
    for index, line in enumerate(lines):
        info = _line_info(line)
        if info is None or separator_name(info[1]) is None:
            continue
        if separator_name(info[1]) == category:
            return index
    return None


def install_conflict(lines: list[str], mods_dir: str | Path, name: str) -> str | None:
    """Describe an existing install target, if any.

    Returns ``None`` when ``mods_dir/name`` is free, ``"listed"`` when the mod
    is already present in ``modlist.txt``, or ``"leftover"`` when only the
    folder exists (the list entry was deleted but the files remain).
    """
    destination = Path(mods_dir) / name
    if not destination.exists() and not destination.is_symlink():
        return None
    if any(mod_name == name for _, mod_name in entries(lines)):
        return "listed"
    return "leftover"


def add_category(lines: list[str], name: str) -> list[str]:
    """Append a new disabled MO2 separator category."""
    clean = name.strip()
    if not _valid_name(clean):
        raise ValueError("Category names cannot contain path separators or control characters")
    _reject_status_prefix(clean)
    if clean.endswith("_separator"):
        clean = clean[: -len("_separator")].rstrip()
    if not clean:
        raise ValueError("Category name cannot be empty")
    separator = f"{clean}_separator"
    if any(mod_name == separator for _, mod_name in entries(lines)):
        raise ValueError(f"Category {clean!r} already exists")
    out = list(lines)
    out.append(f"-{separator}")
    return out


def rename_category(lines: list[str], old_category: str, new_category: str) -> list[str]:
    """Rename a category (an MO2 separator) without touching its members.

    Raises ``ValueError`` if the new name is invalid, collides with an
    existing separator/mod, or is the reserved name "Uncategorized" -
    that name is grouped()'s implicit label for trailing, separator-less
    mods, not a real separator, and must never collide with one. Returns
    the original list when ``old_category`` has no matching separator.
    """
    clean = new_category.strip()
    if not _valid_name(clean):
        raise ValueError("Category names cannot contain path separators or control characters")
    _reject_status_prefix(clean)
    if clean.endswith("_separator"):
        clean = clean[: -len("_separator")].rstrip()
    if not clean:
        raise ValueError("Category name cannot be empty")
    if clean.lower() == "uncategorized":
        raise ValueError('"Uncategorized" is reserved for mods with no category')
    if clean == old_category:
        return list(lines)
    new_separator = f"{clean}_separator"
    if any(mod_name == new_separator for _, mod_name in entries(lines)):
        raise ValueError(f"Category {clean!r} already exists")
    old_separator = f"{old_category}_separator"
    out = list(lines)
    for index, line in enumerate(out):
        info = _line_info(line)
        if info is not None and info[1] == old_separator:
            prefix = "+" if info[0] == "Enabled" else "-"
            out[index] = f"{prefix}{new_separator}"
    return out


def _category_at(lines: list[str], line_index: int) -> str:
    """Return the category containing a raw mod line index.

    Uses the same rule as grouped()/_category_labels(): a category's
    members are the mod lines immediately above its separator, not below.
    """
    if not 0 <= line_index < len(lines):
        return "Uncategorized"
    return _category_labels(lines)[line_index] or "Uncategorized"


def move_mod(
    lines: list[str],
    name: str,
    *,
    target_name: str | None = None,
    category: str | None = None,
    before: bool = True,
    at_start: bool = False,
) -> list[str]:
    """Move a mod by name beside another mod or into a category.

    When *target_name* is ``None`` and *category* is given, the mod is placed
    into that category. A category's members sit immediately above its
    separator (see grouped()): by default the mod goes to the file position
    right before the separator (last-in-file, lowest priority - renders at
    that category's own screen-top); pass ``at_start=True`` to place it at
    the file position right after the previous separator instead
    (first-in-file, highest priority - renders at that category's own
    screen-bottom).
    """
    source_idx = next(
        (
            index
            for index, line in enumerate(lines)
            if (info := _line_info(line)) is not None and info[1] == name
        ),
        None,
    )
    if source_idx is None:
        return list(lines)
    source_category = _category_at(lines, source_idx)
    target_idx = None
    target_category = category or source_category
    if target_name is not None:
        target_idx = next(
            (
                index
                for index, line in enumerate(lines)
                if (info := _line_info(line)) is not None and info[1] == target_name
            ),
            None,
        )
        if target_idx is None:
            return list(lines)
        target_category = _category_at(lines, target_idx)
    if category is not None and category == "Uncategorized":
        target_category = category

    out = list(lines)
    mod_line = out.pop(source_idx)
    if target_idx is not None:
        insert_at = target_idx - int(source_idx < target_idx)
        if not before:
            insert_at += 1
    else:
        # A category's members sit immediately above its separator, not
        # below (see grouped()) - "start of category" is the first such
        # member's position, "end of category" is right before its
        # separator (i.e. right after the last member).
        labels = _category_labels(out)
        members = [i for i, label in enumerate(labels) if label == target_category]
        if members:
            insert_at = members[0] if at_start else members[-1] + 1
        elif target_category == "Uncategorized":
            # File-end, not file-start: matches add_mod()'s default landing
            # spot (see its docstring for the file/screen-direction note),
            # which the trailing "nothing closes it" rule already renders
            # as Uncategorized with no special-casing needed.
            insert_at = len(out)
        else:
            sep_idx = _category_separator_index(out, target_category)
            insert_at = sep_idx if sep_idx is not None else len(out)
    out.insert(min(insert_at, len(out)), mod_line)
    return out


#: G.A.M.M.A.'s official modlist.txt always wraps its manually/MO2-added
#: mods in a category with this exact name, positioned first in the file
#: (= highest real MO2 priority = rendered at the *bottom*, i.e. the
#: literal end, of MO2's on-screen list - the category name is not a
#: coincidence). Reversing it like an ordinary category would invert its
#: entire purpose, so flip_priority() pins it at the front the same way
#: it already pins the trailing Uncategorized run at the back.
_PINNED_FIRST_CATEGORY = "g.a.m.m.a. end of list"

#: Reserved category add_custom_mod() files "Install Mod" installs into.
#: Sits directly under _PINNED_FIRST_CATEGORY on screen (file-order just
#: before it - see _PINNED_FIRST_CATEGORIES), so a user's own installed
#: mods always keep the highest priority in the list, immune to Flip
#: Priority. Not created up front - only once a mod is actually
#: installed via add_custom_mod(). Lowercase - only for the
#: case-insensitive comparisons _PINNED_FIRST_CATEGORIES is used for;
#: the separator actually written to the file uses the properly-cased
#: _CUSTOM_MODS_CATEGORY_DISPLAY below.
_CUSTOM_MODS_CATEGORY = "custom mods"
_CUSTOM_MODS_CATEGORY_DISPLAY = "Custom Mods"

#: Categories flip_priority()/move_category()/delete_category() treat as
#: pinned at file-start, in this exact relative order to each other.
_PINNED_FIRST_CATEGORIES = (_CUSTOM_MODS_CATEGORY, _PINNED_FIRST_CATEGORY)


def flip_priority(lines: list[str]) -> list[str]:
    """Reverse the entire mod load order in ``lines``, matching MO2 semantics.

    A category (a separator entry plus every line up to the next separator,
    including any comments/blanks) is treated as one contiguous block.
    Flipping reverses the order of these blocks *and* the mod entries within
    each block, so the whole load order is inverted end-to-end.

    Reversing only the mods inside each category while leaving the
    categories themselves in place (an earlier version of this function)
    left the file in a mixed order that was neither the original nor a
    true reversal - the categories never actually flip position, so the
    overall load order barely changes even though every individual mod
    inside a category does.

    A separator line always stays as the LAST line of its own block (its
    category name does not change) - a category's members sit immediately
    above its separator, not below (see grouped()) - and non-mod lines
    inside a block (comments, blanks) keep their position relative to that
    block.

    Special blocks are exempt from the reversal and stay pinned at their
    end of the file instead: a trailing run of mods with no separator of
    its own ("Uncategorized", see grouped()) stays pinned last, and every
    category named in _PINNED_FIRST_CATEGORIES stays pinned first, in
    that exact relative order to each other - "Custom Mods" (where
    add_custom_mod() files anything installed via the "Install Mod"
    button) directly followed by _PINNED_FIRST_CATEGORY (G.A.M.M.A.'s own
    "end of list" anchor category). A user-installed mod's priority
    relative to the rest of GAMMA must never move - Anomaly can crash on
    load if it does - so "Custom Mods" gets the same absolute protection
    from this function as GAMMA's own anchor, not just the ordinary
    "stays Uncategorized" convention add_mod() otherwise relies on. Each
    pinned block still has its own internal mod order flipped like any
    other block - only its position among the blocks is exempt.

    The function returns a new list; the original is not modified.
    """
    boundaries = [0]
    for index, line in enumerate(lines):
        info = _line_info(line)
        if info is not None and separator_name(info[1]) is not None:
            boundaries.append(index + 1)
    # A trailing run of mods with no separator of its own is "Uncategorized"
    # (see grouped()) - not a real category, just whatever's left at
    # file-end. There can be at most one such run, and it's always last.
    trailing_uncategorized = boundaries[-1] != len(lines)
    if trailing_uncategorized:
        boundaries.append(len(lines))

    blocks = [
        lines[start:end] for start, end in pairwise(boundaries) if start != end
    ]

    def flip_block(block: list[str]) -> list[str]:
        mod_positions = [
            index
            for index, line in enumerate(block)
            if (info := _line_info(line)) is not None
            and separator_name(info[1]) is None
        ]
        records = [block[index] for index in mod_positions]
        new_block = list(block)
        for index, record in zip(mod_positions, reversed(records)):
            new_block[index] = record
        return new_block

    # The Uncategorized run must stay pinned at file-end even though every
    # real category's block position gets reversed: relocating it next to
    # some other block's separator would make grouped() silently absorb it
    # into that category on the next read, since category membership is
    # derived purely from "immediately above a separator." Its own mod
    # order still gets flipped like any other block - only its position
    # among the blocks is exempt from the reversal.
    trailing_block = blocks.pop() if trailing_uncategorized and blocks else None

    # Likewise, every category in _PINNED_FIRST_CATEGORIES must stay
    # pinned at file-start, in that exact relative order to each other -
    # find each among the remaining blocks (normally the first ones, but
    # this doesn't assume that) and pop them out before the reversal.
    pinned_first_blocks = []
    for pinned_name in _PINNED_FIRST_CATEGORIES:
        for index, block in enumerate(blocks):
            name = _block_category_name(block)
            if name is not None and name.strip().lower() == pinned_name:
                pinned_first_blocks.append(blocks.pop(index))
                break

    new_lines: list[str] = []
    for pinned_block in pinned_first_blocks:
        new_lines.extend(flip_block(pinned_block))
    for block in reversed(blocks):
        new_lines.extend(flip_block(block))
    if trailing_block is not None:
        new_lines.extend(flip_block(trailing_block))
    return new_lines


def _carve_category_blocks(
    lines: list[str],
) -> tuple[list[list[str]], list[str] | None]:
    """Split ``lines`` into per-category blocks, same rule as flip_priority().

    Returns ``(blocks, trailing_block)`` - ``blocks`` holds every real
    (separator-terminated) category in file order; ``trailing_block`` is
    the separator-less "Uncategorized" run at file-end, if any (already
    excluded from ``blocks``).
    """
    boundaries = [0]
    for index, line in enumerate(lines):
        info = _line_info(line)
        if info is not None and separator_name(info[1]) is not None:
            boundaries.append(index + 1)
    trailing_uncategorized = boundaries[-1] != len(lines)
    if trailing_uncategorized:
        boundaries.append(len(lines))
    blocks = [
        lines[start:end] for start, end in pairwise(boundaries) if start != end
    ]
    trailing_block = blocks.pop() if trailing_uncategorized and blocks else None
    return blocks, trailing_block


def _block_category_name(block: list[str]) -> str | None:
    info = _line_info(block[-1])
    return separator_name(info[1]) if info is not None else None


def move_category(
    lines: list[str], category: str, target_category: str, *, before: bool
) -> list[str]:
    """Move an entire category (its separator plus every member mod) to

    sit immediately before/after another category - reordering whole
    categories relative to each other, not just mods within one.

    ``target_category`` may be the literal "Uncategorized" sentinel, to
    move ``category`` next to the trailing separator-less run (see
    grouped()) - ``category`` itself can never be "Uncategorized" (it
    has no separator line to identify it by). Refuses to move any
    category in _PINNED_FIRST_CATEGORIES or move anything to before one,
    mirroring the same protection flip_priority() already gives them.
    """
    if category.strip().lower() in _PINNED_FIRST_CATEGORIES:
        raise ValueError(f'"{category}" cannot be moved')
    if before and target_category.strip().lower() in _PINNED_FIRST_CATEGORIES:
        raise ValueError(f'Cannot move a category before "{target_category}"')

    blocks, trailing_block = _carve_category_blocks(lines)
    source_index = next(
        (i for i, block in enumerate(blocks) if _block_category_name(block) == category),
        None,
    )
    if source_index is None:
        raise ValueError(f"Category {category!r} not found")
    source_block = blocks.pop(source_index)

    if target_category == "Uncategorized":
        # There's nothing to be "before"/"after" within the trailing run
        # itself - either way, the moved category simply lands right
        # next to it (immediately before, since nothing may ever follow
        # the trailing run and still count as trailing).
        blocks.append(source_block)
    else:
        target_index = next(
            (
                i
                for i, block in enumerate(blocks)
                if _block_category_name(block) == target_category
            ),
            None,
        )
        if target_index is None:
            raise ValueError(f"Category {target_category!r} not found")
        blocks.insert(target_index if before else target_index + 1, source_block)

    new_lines: list[str] = []
    for block in blocks:
        new_lines.extend(block)
    if trailing_block is not None:
        new_lines.extend(trailing_block)
    return new_lines


def delete_category(
    lines: list[str], category: str, *, delete_members: bool = False
) -> list[str]:
    """Remove a category (its separator line).

    By default its member mods are relocated to Uncategorized
    (file-end), matching add_mod()'s/move_mod()'s existing landing
    convention for mods with no category - nothing is lost, just
    re-filed. With ``delete_members=True`` the members are removed
    outright instead. Refuses to delete any category in
    _PINNED_FIRST_CATEGORIES or a category that doesn't exist.
    """
    if category.strip().lower() in _PINNED_FIRST_CATEGORIES:
        raise ValueError(f'"{category}" cannot be deleted')
    sep_index = _category_separator_index(lines, category)
    if sep_index is None:
        raise ValueError(f"Category {category!r} not found")
    labels = _category_labels(lines)
    member_indexes = [i for i, label in enumerate(labels) if label == category]
    remove = set(member_indexes) | {sep_index}
    kept = [line for i, line in enumerate(lines) if i not in remove]
    if delete_members:
        return kept
    members = [lines[i] for i in member_indexes]
    return kept + members


def _block_start_index(lines: list[str], separator_index: int) -> int:
    """Index where the category block ending at *separator_index* begins.

    Same backward-search shape as _trailing_uncategorized_start(): walk
    back to the previous separator (exclusive) or file-start.
    """
    for index in range(separator_index - 1, -1, -1):
        info = _line_info(lines[index])
        if info is not None and separator_name(info[1]) is not None:
            return index + 1
    return 0


def _pinned_category_start(lines: list[str], pinned_name: str) -> int | None:
    """Index where the (case-insensitively matched) *pinned_name* block

    begins, or None if no such category exists. *pinned_name* must
    already be lowercase, matching _PINNED_FIRST_CATEGORIES' own values -
    real modlist.txt separators keep whatever case GAMMA/the user gave
    them (e.g. "G.A.M.M.A. End of List"), so an exact-string lookup like
    _category_separator_index() would miss it.
    """
    for index, line in enumerate(lines):
        info = _line_info(line)
        if info is None:
            continue
        name = separator_name(info[1])
        if name is not None and name.strip().lower() == pinned_name:
            return _block_start_index(lines, index)
    return None


def add_custom_mod(lines: list[str], name: str, *, enabled: bool = False) -> list[str]:
    """Add a mod installed via the "Install Mod" button to "Custom Mods".

    Unlike add_mod()'s default (file-end "Uncategorized" - MO2's lowest
    priority, rendered at the on-screen top), this always files into the
    reserved _CUSTOM_MODS_CATEGORY, which flip_priority() pins at
    file-start (highest priority, on-screen bottom, directly under
    _PINNED_FIRST_CATEGORY) exactly like GAMMA's own anchor category - see
    _PINNED_FIRST_CATEGORIES. A user-installed override mod's priority
    relative to the rest of GAMMA must never move, or Anomaly can crash on
    load; a plain "Uncategorized" mod has no such protection once it's
    filed into an ordinary category by hand (e.g. via drag-and-drop), so
    "Install Mod" uses this instead of add_mod().

    If "Custom Mods" doesn't exist yet, it's created directly before
    _PINNED_FIRST_CATEGORY's own block (or at file-start if that category
    is missing too) - never at file-end, where add_category()/add_mod()
    would otherwise put a brand-new category.
    """
    if not _valid_name(name):
        raise ValueError("Mod names cannot contain path separators or control characters")
    _reject_status_prefix(name)
    if name.endswith("_separator"):
        raise ValueError("A mod name cannot end in '_separator'")
    if any(mod_name == name for _, mod_name in entries(lines)):
        raise ValueError(f"Mod {name!r} is already in the modlist")
    line = ("+" if enabled else "-") + name

    sep_idx = _category_separator_index(lines, _CUSTOM_MODS_CATEGORY_DISPLAY)
    if sep_idx is not None:
        # Category exists: insert as its last member, right before its
        # separator (members sit above their separator, not below).
        out = list(lines)
        out.insert(sep_idx, line)
        return out

    insert_at = _pinned_category_start(lines, _PINNED_FIRST_CATEGORY) or 0
    out = list(lines)
    out[insert_at:insert_at] = [line, f"-{_CUSTOM_MODS_CATEGORY_DISPLAY}_separator"]
    return out


def custom_mod_names(lines: list[str]) -> set[str]:
    """Return the names currently filed under the "Custom Mods" category.

    The one reliable way to tell "a mod the user personally installed"
    apart from "a mod GAMMA's own official list ships" - matching against
    the official modpack list instead is unreliable (its own line numbers
    drift as the list is reordered upstream, confirmed against a real
    profile: roughly a third of GAMMA's own mods fail to match by name
    that way, which would produce false positives here too).
    """
    for name, mods in grouped(lines):
        if name.strip().lower() == _CUSTOM_MODS_CATEGORY:
            return {mod_name for _status, mod_name, _idx in mods}
    return set()
