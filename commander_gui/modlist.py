"""Read/modify MO2 modlist.txt files.

ModOrganizer modlist files use '#' comments, '-Name' for disabled and
'+Name' for enabled mods. We preserve comments and unknown lines while
allowing per-mod status changes and deletion.
"""

from __future__ import annotations

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
    return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()


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


def grouped(
    lines: list[str],
) -> list[tuple[str, list[tuple[str, str, int]]]]:
    """Group mod lines into categories in file order.

    MO2 separators (entries whose names end in ``_separator``) delimit the
    categories; mods before the first separator land in ``Uncategorized``.
    Each mod is ``(status, name, line_index)`` so edits/reorders can target the
    exact line in the file. Empty separator groups are retained.
    """
    groups: list[tuple[str, list[tuple[str, str, int]]]] = []
    category = "Uncategorized"
    mods: list[tuple[str, str, int]] = []
    for idx, line in enumerate(lines):
        info = _line_info(line)
        if info is None:
            continue
        status, name = info
        cat = separator_name(name)
        if cat is not None:
            if mods or category != "Uncategorized":
                groups.append((category, mods))
            category, mods = cat, []
        else:
            mods.append((status, name, idx))
    if mods or category != "Uncategorized":
        groups.append((category, mods))
    return groups


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
    present = {name for _, name in entries(lines)}
    target = [name for name in gamma_order if name in present]
    if len(target) <= 1:
        return list(lines)
    slots = [
        index
        for index, line in enumerate(lines)
        if (info := _line_info(line)) is not None and info[1] in gamma_order
    ]
    by_name = {
        info[1]: line
        for line in lines
        if (info := _line_info(line)) is not None and info[1] in gamma_order
    }
    out = list(lines)
    for slot, name in zip(slots, target):
        out[slot] = by_name[name]
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


def add_mod(
    lines: list[str],
    name: str,
    *,
    enabled: bool = False,
    category: str = "Extra Mods",
) -> list[str]:
    """Add a newly installed mod to the MO2 list, inside *category*.

    New user-installed mods start disabled so they cannot unexpectedly alter a
    working GAMMA setup.  The caller can enable the entry after reviewing it.

    The mod is placed inside the separator category named *category*.  If that
    category does not exist yet, a new separator is appended to the end of the
    list with the mod right after it.  This keeps newly installed mods grouped
    together instead of dropping them into whichever category is last.

    MO2 stores the list in display order, so the new mod appears at the bottom
    of the left pane (highest priority) inside its category.
    """
    if not _valid_name(name):
        raise ValueError("Mod names cannot contain path separators or control characters")
    _reject_status_prefix(name)
    if name.endswith("_separator"):
        raise ValueError("A mod name cannot end in '_separator'")
    if any(mod_name == name for _, mod_name in entries(lines)):
        raise ValueError(f"Mod {name!r} is already in the modlist")
    clean_category = category.strip()
    if not _valid_name(clean_category):
        raise ValueError("Category names cannot contain path separators or control characters")
    separator = f"{clean_category}_separator"
    out = list(lines)
    sep_idx = _category_separator_index(out, clean_category)
    line = ("+" if enabled else "-") + name
    if sep_idx is not None:
        # Category exists: insert after its separator, before the next one.
        insert_at = sep_idx + 1
        while insert_at < len(out):
            info = _line_info(out[insert_at])
            if info is not None and separator_name(info[1]) is not None:
                break
            insert_at += 1
        out.insert(insert_at, line)
        return out
    # Category missing: append the separator entry, then the mod.
    out.append(f"-{separator}")
    out.append(line)
    return out


def _category_separator_index(lines: list[str], category: str) -> int | None:
    """Return the line index of the first separator for *category*."""
    for index, line in enumerate(lines):
        info = _line_info(line)
        if info is None or separator_name(info[1]) is None:
            continue
        if separator_name(info[1]) == category:
            return index
    return None


def reparent_into_category(
    lines: list[str],
    names: list[str],
    category: str = "Extra Mods",
) -> list[str] | None:
    """Move the listed mod entries together into *category*.

    Entries are relocated so that they all live under a single separator for
    *category*: the separator is created at the end of the list when missing,
    and duplicate separators for the category are collapsed into the first.
    Each mod keeps its enabled/disabled status and the relocations happen in
    the order the mods appear in *lines*.  Entries already inside the category
    are left untouched.

    Returns the new line list when anything changed, or ``None`` when the
    entries are already grouped correctly.
    """
    clean_category = category.strip()
    if not clean_category or not _valid_name(clean_category):
        raise ValueError("Category names cannot contain path separators or control characters")
    wanted = {
        name
        for name in names
        if _valid_name(name) and name[:1] not in "+-" and not name.endswith("_separator")
    }
    if not wanted:
        return None
    separator = f"{clean_category}_separator"

    # Map each mod line to the category it currently belongs to.
    category_of: dict[int, str] = {}
    current = "Uncategorized"
    for index, line in enumerate(lines):
        info = _line_info(line)
        if info is None:
            continue
        cat = separator_name(info[1])
        if cat is not None:
            current = cat
        category_of[index] = current

    moving = [
        (index, line)
        for index, line in enumerate(lines)
        if (info := _line_info(line)) is not None
        and info[1] in wanted
        and category_of.get(index) != clean_category
    ]
    if not moving:
        dupes = sum(
            1
            for line in lines
            if (info := _line_info(line)) is not None
            and separator_name(info[1]) == clean_category
        )
        if dupes <= 1:
            return None

    out = list(lines)
    for index, _line in reversed(moving):
        out.pop(index)

    # Collapse duplicate category separators into the first occurrence.
    seps = [
        index
        for index, line in enumerate(out)
        if (info := _line_info(line)) is not None
        and separator_name(info[1]) == clean_category
    ]
    for index in reversed(seps[1:]):
        out.pop(index)

    sep_idx = None
    for index, line in enumerate(out):
        info = _line_info(line)
        if info is not None and separator_name(info[1]) == clean_category:
            sep_idx = index
            break
    if sep_idx is None:
        out.append(f"-{separator}")
        sep_idx = len(out) - 1

    insert_at = sep_idx + 1
    while insert_at < len(out):
        info = _line_info(out[insert_at])
        if info is not None and separator_name(info[1]) is not None:
            break
        insert_at += 1
    for _index, line in moving:
        out.insert(insert_at, line)
        insert_at += 1
    return out


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


def reorder_mods(lines: list[str], from_idx: int, to_idx: int) -> list[str]:
    """Move the mod at ``from_idx`` to position ``to_idx`` within the same category.

    Preserves separators and comment/blank lines between the positions; the
    relative order of all other mods is unchanged.

    ``from_idx`` and ``to_idx`` are line indices in the raw ``lines`` list
    (as returned by ``read_lines``), not category-relative positions.

    Returns the original list unchanged if either index is out of range, points
    at a non-mod line, or the two positions belong to different separator
    categories.
    """
    if not (0 <= from_idx < len(lines)) or not (0 <= to_idx < len(lines)):
        return list(lines)

    info_from = _line_info(lines[from_idx])
    info_to = _line_info(lines[to_idx])

    # Cannot move into a comment/blank/separator line
    if info_from is None or info_to is None:
        return list(lines)

    if _category_at(lines, from_idx) != _category_at(lines, to_idx):
        return list(lines)

    # Same-line no-op
    if from_idx == to_idx:
        return list(lines)

    out = list(lines)
    mod_line = out.pop(from_idx)
    out.insert(to_idx - int(from_idx < to_idx), mod_line)
    return out


def _category_at(lines: list[str], line_index: int) -> str:
    """Return the separator category containing a raw line index."""
    category = "Uncategorized"
    for index, line in enumerate(lines):
        if index > line_index:
            break
        info = _line_info(line)
        if info is not None:
            category = separator_name(info[1]) or category
    return category


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
    into that category.  By default it goes to the end; pass ``at_start=True``
    to place it at the beginning of the category (right after the separator).
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
    elif at_start and target_category != "Uncategorized":
        # Insert at the start of the target category (right after its separator).
        sep_pos = None
        current_category = "Uncategorized"
        for index, line in enumerate(out):
            info = _line_info(line)
            if info is None:
                continue
            separator = separator_name(info[1])
            if separator is not None:
                current_category = separator
            if current_category == target_category:
                sep_pos = index
                break
        if sep_pos is not None:
            insert_at = sep_pos + 1
        else:
            insert_at = 0
    else:
        insert_at = 0 if target_category == "Uncategorized" else len(out)
        current_category = "Uncategorized"
        for index, line in enumerate(out):
            info = _line_info(line)
            if info is None:
                continue
            separator = separator_name(info[1])
            if separator is not None:
                current_category = separator
                if current_category == target_category:
                    insert_at = index + 1
            elif current_category == target_category:
                insert_at = index + 1
    out.insert(min(insert_at, len(out)), mod_line)
    return out


def flip_priority(lines: list[str]) -> list[str]:
    """Reverse the mod order in ``lines`` while preserving comments and separators.

    In MO2 terms: mods that were at the top (lowest priority) move to the
    bottom (highest priority) and vice versa.  Non-mod lines (comments, blanks,
    separators) stay in their absolute positions; only the ``+Name``/``-Name``
    entries are reversed relative to each other.

    The function returns a new list; the original is not modified.
    """
    new_lines = list(lines)
    for _, mods in reversed(grouped(lines)):
        positions = [line_index for _, _, line_index in mods]
        records = [lines[index] for index in positions]
        for index, record in zip(positions, reversed(records)):
            new_lines[index] = record

    return new_lines
