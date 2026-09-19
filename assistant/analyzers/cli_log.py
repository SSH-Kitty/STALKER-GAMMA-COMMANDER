"""Analyzer for stalker-gamma CLI daily logs."""

from __future__ import annotations

import re

from .. import knowledge
from ..findings import (
    CATEGORY_INSTALL,
    CATEGORY_SYSTEM,
    Finding,
    Severity,
)
from .common import SERILOG_TS_RE, FindingFactory, excerpt

_FAILED_RE = re.compile(
    r"^(?:Anomaly install|Install|Update apply|Update check|Update|"
    r"Prune check|Pruning check|Prune apply|Pruning)\b.*?\bfailed!\s*(.*)$"
)
_DEPENDENCY_RE = re.compile(r"^Dependency not found:\s*(.+)$")
_CORRUPT_RE = re.compile(r"\b(CORRUPT|MD5 mismatch|hash mismatch)\b", re.IGNORECASE)
_EXCEPTION_TYPE_RE = re.compile(r"([\w.]+(?:Exception|Error))")
_STACK_HINTS = ("Exception Message:", "--->", "End of inner exception")

#: Not the bare stem "cancel": that matches inside "CancellationToken", the
#: .NET parameter type name that appears in nearly every async method
#: signature in this CLI's stack traces regardless of whether the operation
#: was actually canceled - "canceled"/"cancelled" still correctly catches
#: OperationCanceledException/TaskCanceledException without that false hit.
_CANCEL_WORDS = ("canceled", "cancelled", "aborted by user")
_DOWNLOAD_WORDS = (
    "download",
    "http",
    "network",
    "socket",
    "timed out",
    "connection",
    "moddb",
    "github record",
)
_INTEGRITY_WORDS = ("md5", "hash", "corrupt", "checksum")
_STORAGE_WORDS = ("extract", "disk", "space", "io error", "unauthorized access")


def analyze_cli(arcname: str, where: str, lines: list[str]) -> list[Finding]:
    """Extract install/update failures, missing dependencies, and integrity problems."""
    factory = FindingFactory(arcname, where)
    findings: list[Finding] = []
    consumed: set[int] = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        if index in consumed:
            index += 1
            continue
        failed = _FAILED_RE.match(line.strip())
        if failed:
            finding, end = _failed_finding(
                factory, lines, index, failed.group(1).strip()
            )
            findings.append(finding)
            # Exception frames belong to this failure: never rescan them as
            # standalone integrity/corrupt hits.
            consumed.update(range(index, end))
            index = end
            continue
        stripped = line.strip()
        dependency = _DEPENDENCY_RE.match(stripped)
        if dependency:
            missing = dependency.group(1).strip()
            findings.append(
                factory.make(
                    Severity.ERROR,
                    CATEGORY_SYSTEM,
                    f"A required tool is missing: {missing}",
                    index + 1,
                    detail="The installer could not find this dependency on the "
                    "system or in the configured paths.",
                    suggestion=knowledge.DEPENDENCY_MISSING,
                    excerpt_text=excerpt(lines, index),
                )
            )
            index += 1
            continue
        if stripped == "No active profile":
            findings.append(
                factory.make(
                    Severity.WARNING,
                    CATEGORY_INSTALL,
                    "COMMANDER has no active profile.",
                    index + 1,
                    detail="The CLI cannot install without an active profile "
                    "pointing at Anomaly/GAMMA folders.",
                    suggestion=knowledge.NO_ACTIVE_PROFILE,
                    excerpt_text=excerpt(lines, index),
                )
            )
            index += 1
            continue
        if _CORRUPT_RE.search(line) and "check md5" not in line.lower():
            findings.append(
                factory.make(
                    Severity.ERROR,
                    CATEGORY_INSTALL,
                    "A mod archive failed its checksum check.",
                    index + 1,
                    detail=line.strip()[:200],
                    suggestion=knowledge.cli_failed("integrity", line.strip())[2],
                    excerpt_text=excerpt(lines, index),
                )
            )
        index += 1
    return findings


def _failed_finding(
    factory: FindingFactory,
    lines: list[str],
    index: int,
    reason: str,
) -> tuple[Finding, int]:
    """Build the failure finding; also return the first unconsumed line index."""
    technical, next_index = _collect_exception_block(lines, index)
    kind = _classify(reason.lower(), technical.lower())
    title, detail, suggestion = knowledge.cli_failed(kind, reason or "unknown reason")
    severity = Severity.INFO if kind == "canceled" else Severity.ERROR
    finding = factory.make(
        severity,
        CATEGORY_INSTALL,
        title,
        index + 1,
        detail=detail,
        suggestion=suggestion,
        excerpt_text=excerpt(lines, index),
        technical=technical,
    )
    return finding, next_index


def _classify(reason: str, technical: str) -> str:
    """Pick the failure kind; the explicit reason line is authoritative.

    Cancellation is checked first across both sources because an
    ``OperationCanceledException`` is definitive even when the reason line
    mentions extracting/downloading. Stack frames can otherwise mention
    unrelated words (a hash check inside a download failure), so the
    technical block only breaks ties after the reason.
    """
    if any(word in reason for word in _CANCEL_WORDS) or any(
        word in technical for word in _CANCEL_WORDS
    ):
        return "canceled"
    if any(word in reason for word in _INTEGRITY_WORDS):
        return "integrity"
    if any(word in reason for word in _STORAGE_WORDS):
        return "storage"
    if any(word in reason for word in _DOWNLOAD_WORDS):
        return "download"
    if any(word in technical for word in _INTEGRITY_WORDS):
        return "integrity"
    if any(word in technical for word in _STORAGE_WORDS):
        return "storage"
    if any(word in technical for word in _DOWNLOAD_WORDS):
        return "download"
    return "generic"


def _collect_exception_block(
    lines: list[str], start: int, max_lines: int = 80
) -> tuple[str, int]:
    """Gather exception frames after a failure line.

    The CLI's own failure dumps intersperse plain "Key: value" context
    lines (Url, Branch, Download Path, Output Dir, Repo URL...) both before
    the first real exception line and between chained inner exceptions, and
    use separator lines ("--- End of stack trace from previous location
    ---") that don't match any of the known stack-frame hints below.
    Bailing out at the first line that doesn't look like a stack frame (an
    earlier version of this function did) stopped the block right after the
    failure header in the common case - e.g. a real "Install failed!"
    dump's very next line is "Url: ...", so the block ended up empty,
    missing the actual "Exception Message: ..." detail and stack trace a
    few lines down. Non-matching lines are now skipped over instead,
    tolerated as long as *some* stack-hint line keeps turning up within a
    reasonable window - if nothing ever does, there's no exception block to
    collect and the scan gives up rather than running all the way to
    max_lines through unrelated log content.

    Returns the formatted block and the first line index *not* consumed
    (so callers can skip these lines in later sweeps).
    """
    collected: list[str] = []
    offset = 1
    unmatched_streak = 0
    while offset <= max_lines and start + offset < len(lines):
        raw = lines[start + offset]
        stripped = raw.strip()
        if SERILOG_TS_RE.match(stripped):
            break
        looks_like_stack = (
            any(hint in raw for hint in _STACK_HINTS)
            or _EXCEPTION_TYPE_RE.search(raw) is not None
            or (stripped.startswith("at ") and len(collected) > 0)
        )
        if looks_like_stack:
            collected.append(f"[line {start + offset + 1}] {raw.rstrip()}")
            unmatched_streak = 0
        elif not stripped:
            if collected:
                break
        else:
            unmatched_streak += 1
            if unmatched_streak > 20:
                break
        offset += 1
    return "\n".join(collected[:60]), start + offset
