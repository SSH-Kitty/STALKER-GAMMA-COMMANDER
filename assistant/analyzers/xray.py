"""Analyzer for XRay engine logs (Anomaly crash logs)."""

from __future__ import annotations

import re

from .. import knowledge
from ..findings import CATEGORY_GAME, CATEGORY_INFO, Finding, Severity
from .common import FindingFactory, excerpt

_SECTION_RE = re.compile(r"Can't open section '([^']+)'")
_ERROR_PREFIX_RE = re.compile(r"^!\s*\[Error\]\s*(.*)$")
_ENGINE_BUILD_RE = re.compile(r"'(\w+)' build (\d+)")
_ERROR_ENTRY_RE = re.compile(r"^\[error\]\s*[^:\r\n]{1,80}:", re.IGNORECASE)
_MAX_CONTINUATION_LINES = 20
_MAX_BLOCK_TEXT = 8192


def analyze_xray(arcname: str, where: str, lines: list[str]) -> list[Finding]:
    """Parse FATAL ERROR blocks and stray [error] lines."""
    factory = FindingFactory(arcname, where)
    findings: list[Finding] = []
    consumed_fatal_block = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() == "FATAL ERROR":
            block, block_end = _collect_fatal_block(lines, index)
            findings.append(_fatal_finding(factory, index + 1, lines, block))
            consumed_fatal_block = True
            index = block_end
            continue
        error_prefix = _ERROR_PREFIX_RE.match(line.strip())
        if error_prefix:
            message = error_prefix.group(1).strip()
            findings.append(
                factory.make(
                    Severity.WARNING,
                    CATEGORY_GAME,
                    f"The engine reported an in-game error: {message[:120]}",
                    index + 1,
                    detail=(
                        "Non-fatal engine errors appear during play. Repeats of "
                        "the same message may point at one broken asset or "
                        "script."
                    ),
                    suggestion=(
                        "If gameplay is affected, note which action triggers it. "
                        "COMMANDER's Install page has Verify Integrity, which can "
                        "repair broken game files."
                    ),
                    excerpt_text=excerpt(lines, index),
                )
            )
        match = _ENGINE_BUILD_RE.search(line)
        if match and not any(f.title.startswith("Engine") for f in findings):
            findings.append(
                factory.make(
                    Severity.INFO,
                    CATEGORY_INFO,
                    f"Game engine started ({match.group(1)} build {match.group(2)}).",
                    index + 1,
                    detail="The XRay engine initialized far enough to log its build.",
                    suggestion="Informational — no action is required.",
                )
            )
        index += 1
    if not findings and not consumed_fatal_block:
        findings.append(
            factory.make(
                Severity.INFO,
                CATEGORY_INFO,
                "The game ran without recording any fatal errors.",
                None,
                detail="No FATAL ERROR blocks were found in this session's log.",
                suggestion="Informational — no action is required.",
            )
        )
    return findings


def _collect_fatal_block(
    lines: list[str], start: int
) -> tuple[list[tuple[int, str]], int]:
    """Collect the [error] key/value block after a FATAL ERROR line."""
    collected: list[tuple[int, str]] = []
    continuation_lines = 0
    block_text_size = 0
    index = start + 1
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if stripped.lower().startswith("stack trace"):
            break
        if _ERROR_ENTRY_RE.match(stripped):
            collected.append((index, stripped))
            continuation_lines = 0
            block_text_size += len(stripped)
        elif (
            collected
            and raw[:1].isspace()
            and stripped
            and continuation_lines < _MAX_CONTINUATION_LINES
            and block_text_size + len(stripped) + 1 <= _MAX_BLOCK_TEXT
        ):
            # Continuation of the previous value (e.g. long Arguments).
            key_index, prev = collected[-1]
            collected[-1] = (key_index, prev + " " + stripped)
            continuation_lines += 1
            block_text_size += len(stripped) + 1
        elif collected:
            break
        index += 1
    return collected, index


def _fatal_finding(
    factory: FindingFactory,
    line_no: int,
    lines: list[str],
    block: list[tuple[int, str]],
) -> Finding:
    values: dict[str, str] = {}
    for _idx, entry in block:
        key, _, value = entry.partition(":")
        values[key.removeprefix("[error]").strip().lower()] = value.strip()
    arguments = values.get("arguments", "")
    section_match = _SECTION_RE.search(arguments)
    if section_match:
        title, detail, suggestion = knowledge.xray_missing_section(
            section_match.group(1)
        )
    else:
        base_title, base_detail = knowledge.XRAY_FATAL_GENERIC
        description = values.get("description", "")
        title = base_title + (f" ({description})" if description else "")
        detail = base_detail
        suggestion = (
            "Check the technical details below for the failing subsystem. "
            "COMMANDER's Install page has Verify Integrity, which can repair "
            "broken game files; recently added mods are a common cause."
        )
    technical_lines = [f"[line {idx + 1}] {text}" for idx, text in block]
    return factory.make(
        Severity.FATAL,
        CATEGORY_GAME,
        title,
        line_no,
        detail=detail,
        suggestion=suggestion,
        excerpt_text=excerpt(lines, line_no - 1, before=1, after=len(block) + 3),
        technical="\n".join(technical_lines),
    )
