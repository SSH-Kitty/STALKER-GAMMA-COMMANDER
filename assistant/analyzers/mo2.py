"""Analyzers for Mod Organizer logs (mo_interface.log, usvfs-*.log)."""

from __future__ import annotations

import re

from .. import knowledge
from ..findings import CATEGORY_INFO, CATEGORY_MO2, Finding, Severity
from .common import MO2_LINE_RE, FindingFactory, excerpt

_USVFS_VERSION_RE = re.compile(r"usvfs dll ([\d.]+) initialized")
_VFS_PROBLEM_RE = re.compile(r"(failed|error|could not inject|cannot attach)", re.IGNORECASE)
#: usvfs logs "failed to hook X: No Error" for optional hooks that were
#: intentionally skipped — not a problem at all.
_BENIGN_HOOK_MISS_RE = re.compile(r"failed to hook .*:\s*no error\s*$", re.IGNORECASE)


def analyze_mo2_interface(arcname: str, where: str, lines: list[str]) -> list[Finding]:
    """Flag [E]/[W] entries in Mod Organizer's interface log."""
    factory = FindingFactory(arcname, where)
    findings: list[Finding] = []
    for index, line in enumerate(lines):
        match = MO2_LINE_RE.match(line.strip())
        if not match:
            continue
        level, message = match.group(1), match.group(2).strip()
        if level == "E":
            findings.append(
                factory.make(
                    Severity.ERROR,
                    CATEGORY_MO2,
                    f"Mod Organizer reported an error: {message[:140]}",
                    index + 1,
                    detail="An [E] entry means MO2 encountered a problem while running.",
                    suggestion="If MO2 misbehaves, note what you were doing when "
                    "this appeared; restarting MO2 often clears transient "
                    "errors.",
                    excerpt_text=excerpt(lines, index),
                )
            )
        elif level == "W" and message.lower() not in knowledge.BENIGN_MO2_WARNINGS:
            findings.append(
                factory.make(
                    Severity.WARNING,
                    CATEGORY_MO2,
                    f"Mod Organizer warning: {message[:140]}",
                    index + 1,
                    detail="A [W] entry is often recoverable, but repeated "
                    "warnings can indicate a misconfiguration.",
                    suggestion="Watch whether it repeats; single warnings are "
                    "often non-critical.",
                    excerpt_text=excerpt(lines, index),
                )
            )
    if not findings:
        findings.append(
            factory.make(
                Severity.INFO,
                CATEGORY_INFO,
                "Mod Organizer recorded no errors or warnings.",
                None,
                detail="The interface log contains no [E]/[W] entries for this "
                "session.",
                suggestion="Informational — no action is required.",
            )
        )
    return findings


def analyze_usvfs(arcname: str, where: str, lines: list[str]) -> list[Finding]:
    """Summarise the virtual file system log and flag hooking problems."""
    factory = FindingFactory(arcname, where)
    findings: list[Finding] = []
    version: str | None = None
    for index, line in enumerate(lines):
        version_match = _USVFS_VERSION_RE.search(line)
        if version_match:
            version = version_match.group(1)
        if _VFS_PROBLEM_RE.search(line) and not _BENIGN_HOOK_MISS_RE.search(line):
            findings.append(
                factory.make(
                    Severity.WARNING,
                    CATEGORY_MO2,
                    "The virtual file system reported a problem hooking files.",
                    index + 1,
                    detail=line.strip()[:180],
                    suggestion=(
                        "If mods appear missing in-game, restart the game via "
                        "MO2 (not directly). Persistent hook failures can mean "
                        "an antivirus or overlay tool is interfering."
                    ),
                    excerpt_text=excerpt(lines, index),
                )
            )
    if version:
        findings.append(
            factory.make(
                Severity.INFO,
                CATEGORY_INFO,
                f"Virtual file system initialised correctly (usvfs {version}).",
                None,
                detail=knowledge.USVFS_OK,
                suggestion="Informational — no action is required.",
            )
        )
    return findings
