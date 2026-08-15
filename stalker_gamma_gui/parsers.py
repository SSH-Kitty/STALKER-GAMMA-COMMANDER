r"""Parsers for the stalker-gamma CLI console output.

The CLI emits Serilog-formatted lines. Progress lines look like:

    \e[97m[12:00:00]\e[0m \e[96mAddonName\e[0m ... | Download | 12.34% | [3/250]

and (with --verbose) omit the timestamp and append the URL. All parsers operate
on ANSI-stripped text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

OPERATIONS = ("Download", "Extract", "Expand", "Check MD5", "Skipped")

# Informational progress line: [HH:mm:ss] Addon | Operation | Percent | [C/T]
INFORMATIONAL_PROGRESS_RE = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\]\s+"
    r"(?P<name>.+?)\s*\|\s*"
    r"(?P<op>Download|Extract|Expand|Check MD5|Skipped)\s*\|\s*"
    r"(?P<percent>\d+(?:[.,]\d+)?)\s*%\s*\|\s*"
    r"\[(?P<complete>\d+)/(?P<total>\d+)\]$"
)
# Verbose progress line: Addon | Operation | Percent | [C/T] [| Url]
VERBOSE_PROGRESS_RE = re.compile(
    r"^(?P<name>.+?)\s*\|\s*"
    r"(?P<op>Download|Extract|Expand|Check MD5|Skipped)\s*\|\s*"
    r"(?P<percent>\d+(?:[.,]\d+)?)\s*%\s*\|\s*"
    r"\[(?P<complete>\d+)/(?P<total>\d+)\]\s*(?:\|.*)?$"
)

MO2_MOD_RE = re.compile(r"^(?P<status>Enabled|Disabled)\s*\|\s*(?P<name>.+)$")
ANOMALY_CHECK_RE = re.compile(
    r"^(?P<file>.+?)\s*\|\s*(?P<status>OK|CORRUPT|NOT FOUND)$"
)
PRUNE_ARCHIVE_RE = re.compile(
    r"^(?P<file>.+?)\s*\|\s*(?P<mb>\d+)mb\s*\|\s*(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$"
)
UPDATE_DIFF_RE = re.compile(
    r"^(?P<status>Modified|Added|Removed):\s+(?P<rest>.+)$"
)
UPDATES_AVAILABLE_RE = re.compile(r"^Updates available:\s*(?P<count>\d+)$")

PROGRESS_EVENT_LINES = ("Download", "Extract", "Expand", "Check MD5", "Skipped")

FINAL_STATUS_LINES = (
    "Install finished",
    "Install failed!",
    "Anomaly install complete",
    "Anomaly install failed!",
    "Update finished",
    "Update failed!",
    "Update check failed!",
    "Pruning check finished",
    "Pruning finished",
    "Prune check failed!",
    "Prune apply failed!",
    "No addons to prune",
    "No updates found",
    "Dependency not found:",
    "No active profile",
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


def parse_progress_line(line: str) -> ProgressEvent | None:
    """Parse a single progress line; returns None if the line isn't progress."""
    match = INFORMATIONAL_PROGRESS_RE.match(line) or VERBOSE_PROGRESS_RE.match(line)
    if not match:
        return None
    percent = float(match.group("percent").replace(",", ".")) / 100.0
    return ProgressEvent(
        name=match.group("name").rstrip(),
        operation=match.group("op"),
        percent=percent,
        complete=int(match.group("complete")),
        total=int(match.group("total")),
    )


@dataclass(frozen=True)
class Mo2Mod:
    status: str
    name: str


def parse_mo2_mod(line: str) -> Mo2Mod | None:
    match = MO2_MOD_RE.match(line)
    if not match:
        return None
    return Mo2Mod(status=match.group("status"), name=match.group("name").strip())


@dataclass(frozen=True)
class AnomalyCheckResult:
    file: str
    status: str


def parse_anomaly_check(line: str) -> AnomalyCheckResult | None:
    match = ANOMALY_CHECK_RE.match(line)
    if not match:
        return None
    return AnomalyCheckResult(
        file=match.group("file").strip(), status=match.group("status")
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
        file=match.group("file").strip(), mb=int(match.group("mb")), date=match.group("date")
    )


@dataclass(frozen=True)
class UpdateDiff:
    status: str  # Modified / Added / Removed
    text: str


def parse_update_diff(line: str) -> UpdateDiff | None:
    match = UPDATE_DIFF_RE.match(line)
    if not match:
        return None
    return UpdateDiff(status=match.group("status"), text=match.group("rest").strip())
