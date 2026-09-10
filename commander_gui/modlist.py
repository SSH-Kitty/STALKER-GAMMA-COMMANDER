"""Read/modify MO2 modlist.txt files.

ModOrganizer modlist files use '#' comments, '-Name' for disabled and
'+Name' for enabled mods. We preserve comments and unknown lines while
allowing per-mod status changes and deletion.
"""

from __future__ import annotations

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
    it are audio/voice-themed. A separator labels what precedes it.

    Mods before the very first separator in the whole file are always
    "Uncategorized", never swept into that first separator's name - a
    deliberate carve-out so a freshly installed mod (inserted at the top
    of the file, see add_mod()) reliably lands uncategorized rather than
    acquiring whatever the first existing category happens to be named,
    matching real MO2's landing spot for a new install.
    """
    labels: list[str | None] = [None] * len(lines)
    pending: list[int] = []
    seen_separator = False
    for idx, line in enumerate(lines):
        info = _line_info(line)
        if info is None:
            continue
        _status, name = info
        cat = separator_name(name)
        if cat is not None:
            # This separator closes the batch collected since the last one
            # (or file start) - it's the one that NAMES that batch, not
            # whatever separator came before it.
            label = cat if seen_separator else "Uncategorized"
            for pending_idx in pending:
                labels[pending_idx] = label
            pending = []
            seen_separator = True
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
    seen_separator = False
    for idx, line in enumerate(lines):
        info = _line_info(line)
        if info is None:
            continue
        status, name = info
        cat = separator_name(name)
        if cat is not None:
            if not seen_separator:
                # The very first separator's own members always go to
                # Uncategorized instead (see _category_labels()) - but the
                # separator/category itself still exists and is retained,
                # just empty, so it still shows up (and can be moved into).
                if pending:
                    groups.append(("Uncategorized", pending))
                groups.append((cat, []))
                seen_separator = True
            else:
                # This separator closes the batch collected since the
                # previous one - it's the one that NAMES that batch.
                groups.append((cat, pending))
            pending = []
        else:
            pending.append((status, name, idx))
    # Trailing mods with no closing separator (or no separators at all).
    if pending:
        groups.append(("Uncategorized", pending))
    return groups


def count_mods(lines: list[str]) -> tuple[int, int]:
    """Return (enabled, total) counts of real mods, excluding separators.

    Uses the same "is this a mod, or a category separator" rule as
    grouped() (separator_name(name) is not None), so this always agrees
    with what the Mod Manager tree displays.
    """
    total = enabled = 0
    for status, name in entries(lines):
        if separator_name(name) is not None:
            continue
        total += 1
        if status == "Enabled":
            enabled += 1
    return enabled, total


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


def _top_insertion_index(lines: list[str]) -> int:
    """Return the insertion index for a genuinely top-of-list, uncategorized mod.

    Skips any leading comment/blank lines (e.g. MO2's own "automatically
    generated by Mod Organizer" header) so those stay first, but otherwise
    lands before all real content - including before the file's first
    separator, so the inserted mod falls in the "Uncategorized" zone (see
    _category_labels()) rather than being swept into whatever category
    happens to be first.
    """
    index = 0
    while index < len(lines) and _line_info(lines[index]) is None:
        index += 1
    return index


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

    With no *category* (the default), the mod is inserted at the very top
    of the list, before any existing separator - the "Uncategorized" zone
    (see _category_labels()) - matching where a freshly installed mod
    actually lands in real MO2: top of the left pane, in no category, and
    freely movable into a real category afterward.

    With an explicit *category*, the mod is added as a member of that
    category: a category's members sit immediately above its separator
    (not below - see grouped()), so the mod is inserted right before the
    separator, making it that category's last/highest-priority member. If
    the category doesn't exist yet, it's created by appending the mod
    followed by a new separator to the end of the list.
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
        out.insert(_top_insertion_index(out), line)
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
    # Category missing: append the mod, then a new separator to close it.
    out.append(line)
    out.append(f"-{separator}")
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
            insert_at = _top_insertion_index(out)
        else:
            sep_idx = _category_separator_index(out, target_category)
            insert_at = sep_idx if sep_idx is not None else len(out)
    out.insert(min(insert_at, len(out)), mod_line)
    return out


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

    The function returns a new list; the original is not modified.
    """
    boundaries = [0]
    for index, line in enumerate(lines):
        info = _line_info(line)
        if info is not None and separator_name(info[1]) is not None:
            boundaries.append(index + 1)
    if boundaries[-1] != len(lines):
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

    new_lines: list[str] = []
    for block in reversed(blocks):
        new_lines.extend(flip_block(block))
    return new_lines
