"""Finding model shared by analyzers, the UI and the report exporter.

A :class:`Finding` is one human-readable observation about a log dump:
what happened in plain language, where it was found, why it matters and
how to fix it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum

CATEGORY_GAME = "Game crash"
CATEGORY_INSTALL = "Install & download"
CATEGORY_LAUNCHER = "Launcher & Wine"
CATEGORY_MO2 = "Mods (MO2)"
CATEGORY_SYSTEM = "System & runtime"
CATEGORY_INFO = "Information"

#: Display order for categories in the UI filter box.
CATEGORIES: tuple[str, ...] = (
    CATEGORY_GAME,
    CATEGORY_INSTALL,
    CATEGORY_LAUNCHER,
    CATEGORY_MO2,
    CATEGORY_SYSTEM,
    CATEGORY_INFO,
)

_CATEGORY_RANK = {name: i for i, name in enumerate(CATEGORIES)}


class Severity(IntEnum):
    """Ordered severity; higher value means more serious."""

    OK = -1  # confirmed fine, e.g. GAMMA's expected modified files
    INFO = 0
    WARNING = 1
    ERROR = 2
    FATAL = 3


SEVERITY_LABEL: dict[Severity, str] = {
    Severity.FATAL: "CRASH",
    Severity.ERROR: "PROBLEM",
    Severity.WARNING: "WARNING",
    Severity.INFO: "NOTE",
    Severity.OK: "NORMAL",
}

SEVERITY_COLOR: dict[Severity, str] = {
    Severity.FATAL: "#ff5f52",
    Severity.ERROR: "#e0554f",
    Severity.WARNING: "#d9a04c",
    Severity.INFO: "#7f8f78",
    Severity.OK: "#5fb548",
}


@dataclass
class Finding:
    """One readable observation extracted from a dump."""

    severity: Severity
    category: str
    title: str
    arcname: str
    detail: str = ""
    suggestion: str = ""
    where_label: str = ""
    line_no: int | None = None
    excerpt: str = ""
    technical: str = ""
    count: int = 1
    #: Untruncated identity for collapse_findings() to merge on, for
    #: analyzers whose displayed `title` is truncated (e.g. to a fixed
    #: character count) - without this, two genuinely different messages
    #: that happen to share the same truncated prefix would silently
    #: merge into one, discarding the second one's actual detail. Falls
    #: back to `title` itself when unset (the common case, title already
    #: is the full identity).
    dedup_key: str | None = None

    def where_text(self) -> str:
        """Human-readable location string, e.g. ``file.log · line 24 · 3x``."""
        parts: list[str] = [self.where_label or self.arcname]
        if self.line_no is not None:
            parts.append(f"line {self.line_no}")
        if self.count > 1:
            parts.append(f"happened {self.count} times")
        return " · ".join(parts)


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Order findings most-serious first, then by category and location."""
    return sorted(
        findings,
        key=lambda f: (
            -int(f.severity),
            _CATEGORY_RANK.get(f.category, len(CATEGORIES)),
            f.arcname,
            f.line_no if f.line_no is not None else 0,
        ),
    )


_COLLAPSE_KEY_FIELDS = ("severity", "category", "arcname")


def collapse_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Merge repeats of the same finding into a single entry with a count.

    Repeated log lines (43 identical ``dlopen`` failures, the same failed
    install reported twice, ...) would otherwise flood the UI. Findings are
    merged when severity, category, file and identity (``dedup_key``, or
    ``title`` itself when an analyzer didn't need to truncate it) all
    match; the earliest line number and excerpt are kept and technical
    details concatenated (capped so pathological logs cannot blow up the
    report).
    """
    key_to_index: dict[tuple, int] = {}
    merged: list[Finding] = []
    for finding in findings:
        key = (
            *(getattr(finding, name) for name in _COLLAPSE_KEY_FIELDS),
            finding.dedup_key or finding.title,
        )
        index = key_to_index.get(key)
        if index is None:
            key_to_index[key] = len(merged)
            merged.append(finding)
            continue
        existing = merged[index]
        existing.count += finding.count
        if finding.line_no is not None and (
            existing.line_no is None or finding.line_no < existing.line_no
        ):
            existing.line_no = finding.line_no
        if not existing.excerpt and finding.excerpt:
            existing.excerpt = finding.excerpt
        if finding.technical:
            combined = (existing.technical + "\n" + finding.technical).strip("\n")
            existing.technical = combined[-8000:]
    return merged


@dataclass
class HealthSummary:
    """Sentence-style verdict used by the banner and the exported report."""

    fatal: int = 0
    error: int = 0
    warning: int = 0
    info: int = 0
    total_files: int = 0
    partial_scan: bool = False

    @property
    def verdict(self) -> str:
        """One of ``bad``/``warn``/``good`` for colour-coding."""
        if self.fatal or self.error:
            return "bad"
        if self.warning:
            return "warn"
        return "good"

    @property
    def problems(self) -> int:
        return self.fatal + self.error

    @property
    def headline(self) -> str:
        """Plain-language verdict phrase."""
        if self.problems:
            noun = "problem" if self.problems == 1 else "problems"
            lead = "Crash found: " if self.fatal else ""
            return f"{lead}{self.problems} {noun} to fix"
        if self.warning:
            noun = "warning" if self.warning == 1 else "warnings"
            return f"No serious problems, {self.warning} {noun} to check"
        return "All clear, no problems found"

    def sentence(self) -> str:
        """Full human-readable summary sentence."""
        if self.problems:
            parts = [
                (
                    "Start with the first item in the list. Each one explains "
                    "what happened and how to fix it."
                )
            ]
            if self.warning:
                noun = "warning is" if self.warning == 1 else "warnings are"
                parts.append(f"{self.warning} smaller {noun} also worth a look.")
        elif self.warning:
            parts = ["Nothing here should stop the game from working."]
        else:
            parts = ["The scanned logs show no crashes, errors or warnings."]
        if self.info:
            noun = "entry is" if self.info == 1 else "entries are"
            parts.append(f"{self.info} other {noun} normal and need no action.")
        if self.partial_scan:
            parts.append(
                "(Partial scan: this ZIP does not look like a standard COMMANDER "
                "log dump.)"
            )
        return " ".join(parts)


def summarize(
    findings: Iterable[Finding], total_files: int, partial=False
) -> HealthSummary:
    """Count findings per severity and build the summary."""
    summary = HealthSummary(total_files=total_files, partial_scan=partial)
    for finding in findings:
        if finding.severity is Severity.FATAL:
            summary.fatal += 1
        elif finding.severity is Severity.ERROR:
            summary.error += 1
        elif finding.severity is Severity.WARNING:
            summary.warning += 1
        else:
            summary.info += 1
    return summary
