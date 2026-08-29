"""Generic scanners applied to every text file (tracebacks, segfaults)."""

from __future__ import annotations

import re

from ..findings import CATEGORY_LAUNCHER, CATEGORY_SYSTEM, Finding, Severity
from .common import FindingFactory, excerpt

_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):")
_LAST_ERROR_RE = re.compile(r"^([\w.]+(?:Error|Exception|Interrupt)):?\s*(.*)$")
_SEGFAULT_RE = re.compile(
    r"\bsegmentation fault\b|\b(?:aborted|bus error|illegal instruction|"
    r"floating point exception)\s*\(\s*core dumped\s*\)",
    re.IGNORECASE,
)
_INDENTED_RE = re.compile(r"^\s+")


def scan_generic(arcname: str, where: str, lines: list[str]) -> list[Finding]:
    """Detect Python tracebacks and segmentation faults in any file."""
    factory = FindingFactory(arcname, where)
    findings: list[Finding] = []
    for index, line in enumerate(lines):
        if _TRACEBACK_RE.search(line):
            findings.append(_traceback_finding(factory, lines, index))
        elif _SEGFAULT_RE.search(line):
            findings.append(
                factory.make(
                    Severity.FATAL,
                    CATEGORY_LAUNCHER,
                    "A process encountered a segmentation fault and stopped.",
                    index + 1,
                    detail=line.strip()[:180],
                    suggestion=(
                        "Segmentation faults often come from the game or a Wine "
                        "component. Check surrounding lines for the failing "
                        "module, and try Verify Integrity plus a GE-Proton "
                        "runner."
                    ),
                    excerpt_text=excerpt(lines, index),
                )
            )
    return findings


def _traceback_finding(
    factory: FindingFactory,
    lines: list[str],
    start: int,
) -> Finding:
    collected = [lines[start]]
    last_error_line = ""
    for offset in range(start + 1, min(start + 60, len(lines))):
        raw = lines[offset]
        error_match = _LAST_ERROR_RE.match(raw.strip())
        if error_match:
            last_error_line = raw.strip()
            collected.append(raw.rstrip())
            continue
        if _INDENTED_RE.match(raw) or not raw.strip():
            if raw.strip() or (collected and _INDENTED_RE.match(collected[-1])):
                collected.append(raw.rstrip())
                continue
            break
        break
    summary = (
        f"{last_error_line[:150]}"
        if last_error_line
        else "An unhandled Python error occurred."
    )
    return factory.make(
        Severity.ERROR,
        CATEGORY_SYSTEM,
        f"A tool crashed with a Python error: {summary}",
        start + 1,
        detail="A component written in Python raised an unhandled exception. "
        "The last line identifies the reported problem; the frames above show how it "
        "was reached.",
        suggestion=(
            "Include this traceback if you report the issue. Restarting the "
            "tool often clears one-off failures."
        ),
        excerpt_text=excerpt(lines, start, before=0, after=min(len(collected), 12)),
        technical="\n".join(
            f"[line {start + i + 1}] {text}" for i, text in enumerate(collected)
        ),
    )
