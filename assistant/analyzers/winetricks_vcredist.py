"""Analyzers for Wine-prefix runtime records (winetricks, vcredist)."""

from __future__ import annotations

import re

from commander_gui.i18n import tr

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

# The VC++ bootstrapper logs write hex: "Exit code: 0x643" is 1603.
_EXIT_CODE_RE = re.compile(r"exit code[:= ]+(0x[0-9a-f]{1,8}|\d{1,10})\b", re.IGNORECASE)
_STATUS_CODE_RE = re.compile(r"installation success or error status:\s*(\d{1,10})\b", re.IGNORECASE)
_HISTORY_VERB_RE = re.compile(
    r"(?<![\w-])(" + "|".join(map(re.escape, REQUIRED_VERBS)) + r")(?![\w-])",
    re.IGNORECASE,
)
_HISTORY_FAILURE_RE = re.compile(
    r"\b(?:fail(?:ed|ure)?|error|cancel(?:led|ed)|abort(?:ed)?|not installed)\b",
    re.IGNORECASE,
)

#: Windows installer codes worth translating (titles go through tr() when used).
_CODE_MEANINGS: dict[int, tuple[str, str]] = {
    1603: ("A runtime installer failed (Windows code 1603).", "error"),
    5100: (
        "A runtime installer was blocked: system requirement not met (5100).",
        "error",
    ),
    1638: (
        "Another version of this runtime is already installed (code 1638).",
        "ok",
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
                tr("1 expected runtime not found in winetricks history ({names}).", names=pretty)
                if len(missing) == 1
                else tr(
                    "{count} expected runtimes not found in winetricks history ({names}).",
                    count=len(missing),
                    names=pretty,
                ),
                None,
                detail=tr(
                    "These DirectX/VC++ runtimes are required by MO2 and the "
                    "game. Note: winetricks history can be incomplete if runtimes "
                    "were installed outside winetricks."
                ),
                suggestion=tr(knowledge.WINETRICKS_MISSING),
            )
        ]
    return [
        factory.make(
            Severity.OK,
            CATEGORY_SYSTEM,
            tr("All expected game runtimes are recorded as installed."),
            None,
            detail=tr(knowledge.WINETRICKS_ALL_GOOD),
            suggestion=tr("Nothing to do. This is normal."),
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
        code = int(match.group(1), 0) if match.group(1).lower().startswith("0x") else int(match.group(1))
        if code == 0 or code in seen_codes:
            continue
        seen_codes.add(code)
        if code in _CODE_MEANINGS:
            meaning, level = _CODE_MEANINGS[code]
            meaning = tr(meaning)
        else:
            meaning = tr("A runtime installer exited with Windows error code {code}.", code=code)
            level = "error"
        severity = {"error": Severity.ERROR, "ok": Severity.OK}.get(level, Severity.INFO)
        if level == "error":
            suggestion = tr(knowledge.VCREDIST_FAILED)
        elif code == 1638:
            suggestion = tr(knowledge.VCREDIST_DUPLICATE)
        else:
            suggestion = tr(knowledge.VCREDIST_REBOOT)
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
