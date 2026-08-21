"""Read/modify MO2 modlist.txt files.

ModOrganizer modlist files use '#' comments, '-Name' for disabled and
'+Name' for enabled mods. We preserve comments and unknown lines while
allowing per-mod status changes and deletion.
"""

from __future__ import annotations

from pathlib import Path

from .atomic import write_text


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
    if not name:
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
    exact line in the file. Empty groups are dropped.
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
            if mods:
                groups.append((category, mods))
            category, mods = cat, []
        else:
            mods.append((status, name, idx))
    if mods:
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


def save_lines(path: str | Path, lines: list[str]) -> None:
    """Write the modlist atomically.

    modlist.txt is the only record of the user's load order, so a partial
    write (crash, full disk) must never be able to truncate it. The temp file
    is written alongside the target so ``replace`` stays on one filesystem.
    """
    path = Path(path)
    write_text(path, "\n".join(lines) + "\n")
