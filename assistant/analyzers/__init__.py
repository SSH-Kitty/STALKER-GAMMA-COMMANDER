"""Analyzer registry: routes archive entries to the right rule engine."""

from __future__ import annotations

import re
from collections.abc import Callable

from ..dump import SOURCE_LABELS, DumpArchive, DumpFile
from ..findings import Finding, collapse_findings, sort_findings
from . import cli_log, generic, launcher_log, mo2, winetricks_vcredist, xray

Analyzer = Callable[[str, str, list[str]], list[Finding]]

_ROUTES: list[tuple[re.Pattern[str], Analyzer]] = [
    (re.compile(r"xray[^/]*\.log$", re.IGNORECASE), xray.analyze_xray),
    (re.compile(r"stalker-gamma-cli\d*\.log$", re.IGNORECASE), cli_log.analyze_cli),
    (re.compile(r"(^|/)launcher\.log$", re.IGNORECASE), launcher_log.analyze_launcher),
    (re.compile(r"mo_interface\.log$", re.IGNORECASE), mo2.analyze_mo2_interface),
    (re.compile(r"usvfs[^/]*\.log$", re.IGNORECASE), mo2.analyze_usvfs),
    (re.compile(r"winetricks\.log$", re.IGNORECASE), winetricks_vcredist.analyze_winetricks),
    (
        re.compile(r"(dd_)?vcre?dist.*\.log$", re.IGNORECASE),
        winetricks_vcredist.analyze_vcredist,
    ),
]


def run_analysis(dump: DumpArchive, partial: bool = False) -> list[Finding]:
    """Analyze every text entry in *dump* and return sorted, merged findings.

    In ``partial`` mode (archives that do not look like a COMMANDER dump)
    only the generic scanners run.
    """
    findings: list[Finding] = []
    for entry in dump.files:
        if not entry.is_text:
            continue
        where_label = _where_label(entry)
        if not partial:
            for pattern, analyzer in _ROUTES:
                if pattern.search(entry.arcname):
                    findings.extend(analyzer(entry.arcname, where_label, entry.lines))
                    break
        findings.extend(generic.scan_generic(entry.arcname, where_label, entry.lines))
    return sort_findings(collapse_findings(findings))


def scanned_files(dump: DumpArchive) -> tuple[list[str], list[str]]:
    """(text files analyzed, binary files skipped) for display and export."""
    text = [f.arcname for f in dump.files if f.is_text]
    binary = [f.arcname for f in dump.files if not f.is_text]
    return text, binary


def _where_label(entry: DumpFile) -> str:
    group = SOURCE_LABELS.get(entry.source, entry.source)
    return f"{group} · {entry.short_name}"
