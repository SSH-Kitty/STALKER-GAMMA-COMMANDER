"""Markdown report export."""

from __future__ import annotations

import html
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path, PureWindowsPath

from . import __version_label__
from .dump import DumpArchive, human_size
from .findings import (
    CATEGORIES,
    CATEGORY_INFO,
    SEVERITY_COLOR,
    SEVERITY_LABEL,
    Finding,
    Severity,
    summarize,
)


def export_report(
    dump: DumpArchive,
    findings: list[Finding],
    dest_dir: Path | None = None,
    now: datetime | None = None,
    partial: bool = False,
    filename: str | None = None,
) -> Path:
    """Write a shareable markdown analysis next to the dump (or in *dest_dir*).

    ``filename`` overrides the default auto-generated name (used when the
    user picked an exact path in the save dialog). Returns the path of the
    written report; collisions get ``-2``/``-3`` suffixes instead of
    overwriting.
    """
    directory = dest_dir if dest_dir is not None else dump.path.parent
    directory.mkdir(parents=True, exist_ok=True)
    base = (
        f"{dump.path.stem}-analysis.md"
        if filename is None
        else _validate_filename(filename)
    )
    target = _unique_path(directory, base)
    stamp = (
        (now or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    )
    text_files, binary_files = _scanned_lists(dump)
    summary = summarize(findings, total_files=len(dump.files), partial=partial)

    lines: list[str] = [
        "# COMMANDER Assistant — Log Analysis",
        "",
        f"- **Dump file:** {_code(dump.path.name)}",
        f"- **Analyzed:** {stamp}",
        f"- **Assistant version:** {__version_label__}",
        (
            f"- **Archive size:** {human_size(dump.size)} · "
            f"{len(dump.files)} files ({len(text_files)} scanned, "
            f"{len(binary_files)} binary/skipped)"
        ),
        (
            f"- **Findings:** {summary.fatal} critical · {summary.error} problems · "
            f"{summary.warning} warnings · {summary.info} notes"
        ),
        "",
        "## Analysis summary",
        "",
        f"**{summary.headline}.** {summary.sentence()}",
        "",
    ]

    for severity in sorted({f.severity for f in findings}, reverse=True):
        group = [f for f in findings if f.severity is severity]
        if not group:
            continue
        label = SEVERITY_LABEL[severity]
        lines.append(f"## {_safe_inline(label)} ({len(group)})")
        lines.append("")
        for category in CATEGORIES:
            bucket = [f for f in group if f.category == category]
            if not bucket:
                continue
            lines.append(f"### {_safe_inline(category)}")
            lines.append("")
            for finding in bucket:
                lines.extend(_finding_lines(finding, severity))
        lines.append("")

    lines.append("## Files scanned")
    lines.append("")
    for arcname in text_files:
        entry = dump.file_by_arcname(arcname)
        size = human_size(entry.size) if entry else "?"
        lines.append(f"- {_code(arcname)} ({size})")
    if binary_files:
        lines.append("")
        lines.append("**Binary files not scanned:**")
        lines.append("")
        for arcname in binary_files:
            lines.append(f"- {_code(arcname)}")
    lines.append("")

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write("\n".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def _finding_lines(finding: Finding, severity: Severity) -> list[str]:
    color = SEVERITY_COLOR[severity]
    label = SEVERITY_LABEL[severity]
    out = [
        f"#### {_safe_single_line(finding.title)}",
        "",
        f"- **{_safe_single_line(finding.where_text())}**",
        "",
    ]
    if finding.detail:
        out += [_paragraph(finding.detail), ""]
    if finding.suggestion:
        out += [f"**Suggested action:** {_paragraph(finding.suggestion)}", ""]
    if finding.technical:
        out += [
            "<details><summary>Technical details</summary>",
            "",
            *_code_block(finding.technical),
            "</details>",
            "",
        ]
    elif finding.excerpt and severity is not Severity.INFO:
        out += [*_code_block(finding.excerpt)]
    del color, label  # markdown keeps it plain; kept for future HTML export
    return out


_CREDENTIAL = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|token|api[_-]?key|secret|access[_-]?key|client[_-]?secret)\b\s*[:=]\s*)[^\s,;]+"
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)[^\s]+")
_KNOWN_TOKEN = re.compile(r"\b(?:gh[pousr]_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_./-]+")


def _redact(value: str) -> str:
    """Remove common secrets and the local user's home path from exports."""
    text = str(value).replace(str(Path.home()), "~")
    text = _CREDENTIAL.sub(r"\1[REDACTED]", text)
    text = _BEARER.sub(r"\1[REDACTED]", text)
    return _KNOWN_TOKEN.sub("[REDACTED]", text)


def _safe_inline(value: str) -> str:
    """Escape text used outside code blocks without changing ordinary prose."""
    text = html.escape(_redact(value), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>])", r"\\\1", text)


def _safe_single_line(value: str) -> str:
    return _safe_inline(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _code(value: str) -> str:
    text = _redact(value).replace("\r\n", "\n").replace("\r", "\n")
    text = " ".join(text.splitlines())
    run = max((len(part) for part in re.findall(r"`+", text)), default=0)
    fence = "`" * max(1, run + 1)
    return f"{fence}{text}{fence}"


def _paragraph(value: str) -> str:
    text = _safe_inline(value).replace("\n", "<br>\n")
    return f"<p>{text}</p>"


def _code_block(value: str) -> list[str]:
    text = _redact(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    run = max((len(part) for part in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, run + 1)
    return [fence, *lines, fence, ""]


def _validate_filename(name: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or Path(name).is_absolute()
        or PureWindowsPath(name).is_absolute()
        or PureWindowsPath(name).drive
    ):
        raise ValueError("filename must be a safe basename")
    return name


def _unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _scanned_lists(dump: DumpArchive) -> tuple[list[str], list[str]]:
    text = [f.arcname for f in dump.files if f.is_text]
    binary = [f.arcname for f in dump.files if not f.is_text]
    return text, binary


# Re-exported so callers can mention the info category without new imports.
INFO_CATEGORY = CATEGORY_INFO
