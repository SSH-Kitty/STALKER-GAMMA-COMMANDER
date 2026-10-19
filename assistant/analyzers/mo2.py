"""Analyzers for Mod Organizer logs (mo_interface.log, usvfs-*.log)."""

from __future__ import annotations

import re

from commander_gui.i18n import tr
from commander_gui.launcher import PTRACE_FIX_COMMAND

from .. import knowledge
from ..findings import CATEGORY_INFO, CATEGORY_MO2, Finding, FindingCollector, Severity
from .common import MO2_LINE_RE, FindingFactory, excerpt, finish_collected

_USVFS_VERSION_RE = re.compile(r"usvfs dll ([\d.]+) initialized")
_VFS_PROBLEM_RE = re.compile(r"(failed|error|could not inject|cannot attach)", re.IGNORECASE)
#: usvfs logs "failed to hook X: No Error" for optional hooks that were
#: intentionally skipped — not a problem at all.
_BENIGN_HOOK_MISS_RE = re.compile(r"failed to hook .*:\s*no error\s*$", re.IGNORECASE)
#: Wine may not write into the game process: kernel.yama.ptrace_scope is 2
#: or 3. MO2 then fails with "Error 87" and the game never starts.
_PTRACE_BLOCKED_RE = re.compile(r"failed to write control data to target process.*access denied", re.IGNORECASE)


def analyze_mo2_interface(arcname: str, where: str, lines: list[str]) -> list[Finding]:
    """Flag [E]/[W] entries in Mod Organizer's interface log."""
    factory = FindingFactory(arcname, where)
    findings = FindingCollector()  # merges repeats as they come
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
                    tr("Mod Organizer reported an error: {message}", message=message[:140]),
                    index + 1,
                    detail=tr("An [E] entry means MO2 encountered a problem while running."),
                    suggestion=tr(
                        "If MO2 misbehaves, note what you were doing when "
                        "this appeared; restarting MO2 often clears transient "
                        "errors."
                    ),
                    excerpt_text=excerpt(lines, index),
                    dedup_key=f"error:{message}",
                )
            )
        elif level == "W" and message.lower() not in knowledge.BENIGN_MO2_WARNINGS:
            findings.append(
                factory.make(
                    Severity.WARNING,
                    CATEGORY_MO2,
                    tr("Mod Organizer warning: {message}", message=message[:140]),
                    index + 1,
                    detail=tr(
                        "A [W] entry is often recoverable, but repeated "
                        "warnings can indicate a misconfiguration."
                    ),
                    suggestion=tr(
                        "Watch whether it repeats; single warnings are "
                        "often non-critical."
                    ),
                    excerpt_text=excerpt(lines, index),
                    dedup_key=f"warning:{message}",
                )
            )
    if not findings:
        findings.append(
            factory.make(
                Severity.OK,
                CATEGORY_INFO,
                tr("Mod Organizer recorded no errors or warnings."),
                None,
                detail=tr(
                    "The interface log contains no [E]/[W] entries for this "
                    "session."
                ),
                suggestion=tr("Nothing to do. This is normal."),
            )
        )
    return finish_collected(factory, findings)


def analyze_usvfs(arcname: str, where: str, lines: list[str]) -> list[Finding]:
    """Summarise the virtual file system log and flag hooking problems."""
    factory = FindingFactory(arcname, where)
    findings: list[Finding] = []
    version: str | None = None
    # Every hook problem collapses into one finding anyway (same title);
    # counting repeats here instead of building a finding with an excerpt
    # per line keeps a huge, noisy usvfs log cheap.
    problem: Finding | None = None
    for index, line in enumerate(lines):
        version_match = _USVFS_VERSION_RE.search(line)
        if version_match:
            version = version_match.group(1)
        if _PTRACE_BLOCKED_RE.search(line):
            findings.append(
                factory.make(
                    Severity.ERROR,
                    CATEGORY_MO2,
                    tr("A Linux setting is blocking GAMMA"),
                    index + 1,
                    detail=tr(
                        "The kernel.yama.ptrace_scope security setting is 2 or 3. That stops "
                        "Mod Organizer from loading GAMMA's mods into the game, so MO2 shows "
                        "\"Error 87\" and the game doesn't start."
                    ),
                    suggestion=tr(
                        "Open a terminal and run: {command}\nThen restart your computer.",
                        command=PTRACE_FIX_COMMAND,
                    ),
                    excerpt_text=excerpt(lines, index),
                )
            )
            continue
        if _VFS_PROBLEM_RE.search(line) and not _BENIGN_HOOK_MISS_RE.search(line):
            if problem is not None:
                problem.count += 1
                continue
            problem = factory.make(
                Severity.WARNING,
                CATEGORY_MO2,
                tr("The virtual file system reported a problem hooking files."),
                index + 1,
                detail=line.strip()[:180],
                suggestion=tr(
                    "If mods appear missing in-game, restart the game via "
                    "MO2 (not directly). Persistent hook failures can mean "
                    "an antivirus or overlay tool is interfering."
                ),
                excerpt_text=excerpt(lines, index),
            )
            findings.append(problem)
    if version:
        findings.append(
            factory.make(
                Severity.OK,
                CATEGORY_INFO,
                tr("Virtual file system initialised correctly (usvfs {version}).", version=version),
                None,
                detail=tr(knowledge.USVFS_OK),
                suggestion=tr("Nothing to do. This is normal."),
            )
        )
    return findings
