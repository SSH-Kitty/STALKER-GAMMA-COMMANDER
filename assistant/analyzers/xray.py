"""Analyzer for XRay engine logs (Anomaly crash logs)."""

from __future__ import annotations

import re

from commander_gui.i18n import tr

from .. import knowledge
from ..findings import CATEGORY_GAME, CATEGORY_INFO, Finding, FindingCollector, Severity
from .common import FindingFactory, excerpt, finish_collected

_SECTION_RE = re.compile(r"Can't open section '([^']+)'")
_ERROR_PREFIX_RE = re.compile(r"^!\s*\[Error\]\s*(.*)$")
_ENGINE_BUILD_RE = re.compile(r"'(\w+)' build (\d+)")
_ERROR_ENTRY_RE = re.compile(r"^\[error\]\s*[^:\r\n]{1,80}:", re.IGNORECASE)
_MAX_CONTINUATION_LINES = 20
_MAX_BLOCK_TEXT = 8192


def analyze_xray(arcname: str, where: str, lines: list[str]) -> list[Finding]:
    """Parse FATAL ERROR blocks and stray [error] lines."""
    factory = FindingFactory(arcname, where)
    findings = FindingCollector()  # merges repeats as they come
    consumed_fatal_block = False
    engine_logged = False
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
                    tr("The engine reported an in-game error: {message}", message=message[:120]),
                    index + 1,
                    detail=tr(
                        "Non-fatal engine errors appear during play. Repeats of "
                        "the same message may point at one broken asset or "
                        "script."
                    ),
                    suggestion=tr(
                        "If gameplay is affected, note which action triggers it. "
                        "COMMANDER's Install page has Verify Integrity, which can "
                        "repair broken game files."
                    ),
                    excerpt_text=excerpt(lines, index),
                    dedup_key=message,  # the title is cut at 120 characters
                )
            )
        match = _ENGINE_BUILD_RE.search(line)
        if match and not engine_logged:
            engine_logged = True
            findings.append(
                factory.make(
                    Severity.OK,
                    CATEGORY_INFO,
                    tr(
                        "Game engine started ({engine} build {build}).",
                        engine=match.group(1),
                        build=match.group(2),
                    ),
                    index + 1,
                    detail=tr("The XRay engine initialized far enough to log its build."),
                    suggestion=tr("Nothing to do. This is normal."),
                )
            )
        index += 1
    if not findings and not consumed_fatal_block:
        findings.append(
            factory.make(
                Severity.OK,
                CATEGORY_INFO,
                tr("The game ran without recording any fatal errors."),
                None,
                detail=tr("No FATAL ERROR blocks were found in this session's log."),
                suggestion=tr("Nothing to do. This is normal."),
            )
        )
    return finish_collected(factory, findings)


#: Lines after a FATAL ERROR line searched for its first [error] entry.
_MAX_LEAD_IN_LINES = 5


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
        elif collected or index - start > _MAX_LEAD_IN_LINES:
            # Nothing block-shaped right after the FATAL line: give up
            # instead of swallowing the rest of the log (and every later
            # fatal block in it).
            break
        index += 1
    # Empty block: resume right after the FATAL line so nothing is skipped.
    return collected, (index if collected else start + 1)


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
        title = tr(base_title) + (f" ({description})" if description else "")
        detail = tr(base_detail)
        suggestion = tr(
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
