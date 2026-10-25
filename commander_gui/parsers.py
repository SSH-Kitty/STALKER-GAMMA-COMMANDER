r"""Parsers for the stalker-gamma CLI console output.

The CLI emits Serilog-formatted lines. Progress lines look like:

    \e[97m[12:00:00]\e[0m \e[96mAddonName\e[0m ... | Download | 12.34% | [3/250]

and (with --verbose) omit the timestamp and append the URL. Since CLI 1.40, a
download whose server sends no size (GitHub archive zips) shows the bytes so
far instead of a percent (``| Download | 12.69 MB | [3/250]``). All parsers
operate on ANSI-stripped text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

# A percent, or (size unknown, CLI 1.40+) the bytes downloaded so far.
_AMOUNT = r"(?:(?P<percent>\d+(?:[.,]\d+)?)\s*%|(?P<size>\d+(?:[.,]\d+)?\s*(?:B|KB|MB|GB)))"

# Informational progress line: [HH:mm:ss] Addon | Operation | Percent | [C/T]
INFORMATIONAL_PROGRESS_RE = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\]\s+"
    r"(?P<name>.+?)\s*\|\s*"
    r"(?P<op>Download|Extract|Expand|Check MD5|Skipped)\s*\|\s*"
    rf"{_AMOUNT}\s*\|\s*"
    r"\[(?P<complete>\d+)/(?P<total>\d+)\]$"
)
# Verbose progress line: Addon | Operation | Percent | [C/T] [| Url]
VERBOSE_PROGRESS_RE = re.compile(
    r"^(?P<name>.+?)\s*\|\s*"
    r"(?P<op>Download|Extract|Expand|Check MD5|Skipped)\s*\|\s*"
    rf"{_AMOUNT}\s*\|\s*"
    r"\[(?P<complete>\d+)/(?P<total>\d+)\]\s*(?:\|.*)?$"
)

PRUNE_ARCHIVE_RE = re.compile(
    r"^(?P<file>.+?)\s*\|\s*(?P<mb>\d+)mb\s*\|\s*(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$"
)


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


@dataclass(frozen=True)
class ProgressEvent:
    name: str
    operation: str
    percent: float
    complete: int
    total: int
    #: Bytes downloaded so far ("12.69 MB") when the size is unknown;
    #: ``percent`` is then 0 until the final line reports 100%.
    size: str = ""

    @property
    def amount(self) -> str:
        """Display text: the percent, or the byte count when there is none."""
        return self.size or f"{self.percent:.1%}"


def parse_progress_line(line: str) -> ProgressEvent | None:
    """Parse a single progress line; returns None if the line isn't progress."""
    match = INFORMATIONAL_PROGRESS_RE.match(line) or VERBOSE_PROGRESS_RE.match(line)
    if not match:
        return None
    raw = match.group("percent")
    percent = float(raw.replace(",", ".")) / 100.0 if raw else 0.0
    return ProgressEvent(
        name=match.group("name").rstrip(),
        operation=match.group("op"),
        percent=percent,
        complete=int(match.group("complete")),
        total=int(match.group("total")),
        size=match.group("size") or "",
    )


@dataclass(frozen=True)
class PruneArchive:
    file: str
    mb: int
    date: str


def parse_prune_archive(line: str) -> PruneArchive | None:
    match = PRUNE_ARCHIVE_RE.match(line)
    if not match:
        return None
    return PruneArchive(
        file=match.group("file").strip(),
        mb=int(match.group("mb")),
        date=match.group("date"),
    )


@dataclass(frozen=True)
class UpdateDiff:
    status: str  # Modified / Added / Removed
    text: str
    detail: str = ""  # human-readable "Archive change" cell text (Modified only)
    detail_tooltip: str = ""  # raw technical detail shown on hover, if any
