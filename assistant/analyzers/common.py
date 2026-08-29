"""Shared helpers for analyzers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..findings import Finding, Severity


def excerpt(lines: list[str], index: int, before: int = 2, after: int = 6) -> str:
    """Lines around *index* (0-based) with ``...`` markers when trimmed."""
    start = max(0, index - before)
    end = min(len(lines), index + after + 1)
    parts: list[str] = []
    if start > 0:
        parts.append("...")
    parts.extend(lines[start:end])
    if end < len(lines):
        parts.append("...")
    return "\n".join(parts)


@dataclass
class FindingFactory:
    """Builds findings pre-filled with a file's location info."""

    arcname: str
    where_label: str

    def make(
        self,
        severity: Severity,
        category: str,
        title: str,
        line_no: int | None,
        *,
        detail: str = "",
        suggestion: str = "",
        excerpt_text: str = "",
        technical: str = "",
    ) -> Finding:
        return Finding(
            severity=severity,
            category=category,
            title=title,
            arcname=self.arcname,
            where_label=self.where_label,
            line_no=line_no,
            detail=detail,
            suggestion=suggestion,
            excerpt=excerpt_text,
            technical=technical,
        )


XRAY_ERROR_RE = re.compile(r"^\[error\]\s*(\w+)\s*:\s?(.*)$")
SERILOG_TS_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}")
MO2_LINE_RE = re.compile(r"^\[[\d:. -]+?\s([EWDI])\]\s?(.*)$")
