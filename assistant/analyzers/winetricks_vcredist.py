"""Analyzers for Wine-prefix runtime records (winetricks, vcredist)."""

from __future__ import annotations

import re

from .. import knowledge
from ..findings import CATEGORY_SYSTEM, Finding, Severity
from .common import FindingFactory, excerpt

#: Verbs COMMANDER's Install Dependencies step installs.
REQUIRED_VERBS: tuple[str, ...] = (
    "d3dcompiler_43",
    "d3dcompiler_47",
    "d3dx10",
    "d3dx11_43",
    "d3dx9",
    "quartz",
    "dx8vb",
    "vcrun2022",
)

_EXIT_CODE_RE = re.compile(r"exit code[:= ]+(\d+)", re.IGNORECASE)
_STATUS_CODE_RE = re.compile(r"installation success or error status:\s*(\d+)", re.IGNORECASE)
_HISTORY_VERB_RE = re.compile(
    r"(?<![\w-])(" + "|".join(map(re.escape, REQUIRED_VERBS)) + r")(?![\w-])",
    re.IGNORECASE,
)
_HISTORY_FAILURE_RE = re.compile(
    r"\b(?:fail(?:ed|ure)?|error|cancel(?:led|ed)|abort(?:ed)?|not installed)\b",
    re.IGNORECASE,
)

#: Windows installer codes worth translating.
_CODE_MEANINGS: dict[int, tuple[str, str]] = {
    1603: ("A runtime installer failed (Windows code 1603).", "error"),
    5100: (
        "A runtime installer was blocked: system requirement not met (5100).",
        "error",
    ),
    1638: (
        "Another version of this runtime is already installed (code 1638).",
        "info",
    ),
    3010: ("Runtime installed; a prefix restart is recommended (3010).", "info"),
}


def analyze_winetricks(arcname: str, where: str, lines: list[str]) -> list[Finding]:
    """Cross-check recorded verbs against what COMMANDER expects."""
    factory = FindingFactory(arcname, where)
    installed: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _HISTORY_FAILURE_RE.search(stripped):
            continue
        installed.update(
            match.group(1).lower() for match in _HISTORY_VERB_RE.finditer(stripped)
        )
    missing = [verb for verb in REQUIRED_VERBS if verb not in installed]
    if missing:
        pretty = ", ".join(missing)
        return [
            factory.make(
                Severity.WARNING,
                CATEGORY_SYSTEM,
                f"{len(missing)} expected runtime"
                f"{'s' if len(missing) != 1 else ''} not found in winetricks "
                f"history ({pretty}).",
                None,
                detail="These DirectX/VC++ runtimes are required by MO2 and the "
                "game. Note: winetricks history can be incomplete if runtimes "
                "were installed outside winetricks.",
                suggestion=knowledge.WINETRICKS_MISSING,
            )
        ]
    return [
        factory.make(
            Severity.INFO,
            CATEGORY_SYSTEM,
            "All expected game runtimes are recorded as installed.",
            None,
            detail=knowledge.WINETRICKS_ALL_GOOD,
            suggestion="Informational — no action is required.",
        )
    ]


def analyze_vcredist(arcname: str, where: str, lines: list[str]) -> list[Finding]:
    """Translate Visual C++ installer exit codes."""
    factory = FindingFactory(arcname, where)
    findings: list[Finding] = []
    seen_codes: set[int] = set()
    for index, line in enumerate(lines):
        match = _EXIT_CODE_RE.search(line) or _STATUS_CODE_RE.search(line)
        if not match:
            continue
        code = int(match.group(1))
        if code == 0 or code in seen_codes:
            continue
        seen_codes.add(code)
        meaning, level = _CODE_MEANINGS.get(
            code,
            (f"A runtime installer exited with Windows error code {code}.", "error"),
        )
        severity = Severity.ERROR if level == "error" else Severity.INFO
        suggestion = knowledge.VCREDIST_REBOOT
        if level == "error":
            suggestion = knowledge.VCREDIST_FAILED
        findings.append(
            factory.make(
                severity,
                CATEGORY_SYSTEM,
                meaning,
                index + 1,
                detail=line.strip()[:180],
                suggestion=suggestion,
                excerpt_text=excerpt(lines, index),
            )
        )
    return findings
