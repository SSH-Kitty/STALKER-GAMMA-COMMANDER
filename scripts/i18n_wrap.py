#!/usr/bin/env python3
"""One-off codemod: wrap user-facing strings in commander_gui/ui/*.py with
tr(...) calls, and emit a manifest of every distinct source string found.

Dev-only tool, not shipped with the app. Surgical by design: it locates the
exact source span of each target string literal via the AST (including
multi-line/triple-quoted strings) and replaces only that span in the raw
source text - it never re-serializes/reformats a whole file, so the diff is
limited to the strings actually wrapped.

Usage:
    python scripts/i18n_wrap.py            # apply changes + write manifest
    python scripts/i18n_wrap.py --dry-run  # write manifest only, no edits
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent.parent / "commander_gui" / "ui"
MANIFEST_PATH = Path(__file__).resolve().parent.parent / "i18n_manifest.json"

#: Call() targets: function/method name -> tuple of positional arg indices
#: (into Call.args, 0-based) that hold translatable text.
_FUNC_TARGETS = {
    "QLabel": (0,),
    "QPushButton": (0,),
    "QCheckBox": (0,),
    "QGroupBox": (0,),
    "section_label": (0,),
    "info_label": (0,),
}
_METHOD_TARGETS = {
    "setText": (0,),
    "setToolTip": (0,),
    "setWindowTitle": (0,),
}
#: QMessageBox.<attr>(parent, title, message, ...) -> title/message are args 1,2.
_MESSAGEBOX_METHODS = {"warning", "information", "question", "critical"}

#: Skip candidate strings that look like CSS/QSS, not prose.
_CSS_HINT_RE = re.compile(
    r"qlineargradient|background-color|border-radius|padding:\s*\d|"
    r"^#[0-9a-fA-F]{3,8}$|font-size:\s*\d|color:\s*#"
)


class _Span:
    __slots__ = ("start", "end", "replacement")

    def __init__(self, start: int, end: int, replacement: str) -> None:
        self.start = start
        self.end = end
        self.replacement = replacement


def _prepare(source: str) -> tuple[list[str], list[int]]:
    """Line list + cumulative *character* offset of each line's start.

    Paired with _byte_col_to_char_col(), converts ast's col_offset (a UTF-8
    *byte* offset within the line - not a character index) into a Python
    string character index. Skipping this conversion silently corrupts any
    line containing a non-ASCII character before or within the target
    string (checkmarks, em dashes, bullets, accented letters, ...): the
    byte offset overshoots the true end of a multi-byte character's string,
    truncating the slice and merging it into whatever follows.
    """
    lines = source.splitlines(keepends=True)
    char_offsets = [0]
    for line in lines:
        char_offsets.append(char_offsets[-1] + len(line))
    return lines, char_offsets


def _byte_col_to_char_col(line: str, byte_col: int) -> int:
    return len(line.encode("utf-8")[:byte_col].decode("utf-8"))


def _node_span(node: ast.expr, lines: list[str], char_offsets: list[int]) -> tuple[int, int]:
    start_line = lines[node.lineno - 1]
    end_line = lines[node.end_lineno - 1]
    start = char_offsets[node.lineno - 1] + _byte_col_to_char_col(start_line, node.col_offset)
    end = char_offsets[node.end_lineno - 1] + _byte_col_to_char_col(end_line, node.end_col_offset)
    return start, end


_slug_re = re.compile(r"\W+")


def _slug(expr: ast.expr, used: set[str]) -> str:
    if isinstance(expr, ast.Name):
        base = expr.id
    elif isinstance(expr, ast.Attribute):
        base = expr.attr
    else:
        base = "arg"
    base = _slug_re.sub("_", base).strip("_") or "arg"
    if base[0].isdigit():
        base = f"_{base}"
    name = base
    i = 1
    while name in used:
        i += 1
        name = f"{base}{i}"
    used.add(name)
    return name


def _is_prose(text: str) -> bool:
    if not text.strip():
        return False
    if _CSS_HINT_RE.search(text):
        return False
    return True


def _template_and_kwargs(node: ast.JoinedStr, source: str) -> tuple[str, str] | None:
    """Build a tr() template + kwargs source snippet for an f-string node."""
    template_parts: list[str] = []
    kwargs: list[str] = []
    used: set[str] = set()
    for value in node.values:
        if isinstance(value, ast.Constant):
            # Literal braces in the original text must survive .format().
            template_parts.append(str(value.value).replace("{", "{{").replace("}", "}}"))
        elif isinstance(value, ast.FormattedValue):
            if value.format_spec is not None or value.conversion != -1:
                # !r/!s conversions and :format-specs are rare here; bail
                # out and leave this f-string unwrapped rather than risk
                # losing formatting behavior.
                return None
            name = _slug(value.value, used)
            template_parts.append(f"{{{name}}}")
            expr_src = ast.get_source_segment(source, value.value)
            if expr_src is None:
                return None
            kwargs.append(f"{name}={expr_src}")
        else:
            return None
    return "".join(template_parts), ", ".join(kwargs)


def _string_literal(text: str) -> str:
    """Render text as a double-quoted Python string literal."""
    return json.dumps(text, ensure_ascii=False)


def _process_call(
    call: ast.Call,
    source: str,
    lines: list[str],
    char_offsets: list[int],
    spans: list[_Span],
    strings: set[str],
) -> None:
    arg_indices: tuple[int, ...] | None = None
    func = call.func
    if isinstance(func, ast.Name) and func.id in _FUNC_TARGETS:
        arg_indices = _FUNC_TARGETS[func.id]
    elif isinstance(func, ast.Attribute):
        if func.attr in _METHOD_TARGETS:
            arg_indices = _METHOD_TARGETS[func.attr]
        elif (
            func.attr in _MESSAGEBOX_METHODS
            and isinstance(func.value, ast.Name)
            and func.value.id == "QMessageBox"
        ):
            arg_indices = (1, 2)
    if arg_indices is None:
        return
    for idx in arg_indices:
        if idx >= len(call.args):
            continue
        arg = call.args[idx]
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "tr":
            continue  # already wrapped
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if not _is_prose(arg.value):
                continue
            start, end = _node_span(arg, lines, char_offsets)
            strings.add(arg.value)
            spans.append(_Span(start, end, f"tr({_string_literal(arg.value)})"))
        elif isinstance(arg, ast.JoinedStr):
            built = _template_and_kwargs(arg, source)
            if built is None:
                continue
            template, kwargs_src = built
            if not _is_prose(template):
                continue
            start, end = _node_span(arg, lines, char_offsets)
            strings.add(template)
            call_src = f"tr({_string_literal(template)}, {kwargs_src})" if kwargs_src else (
                f"tr({_string_literal(template)})"
            )
            spans.append(_Span(start, end, call_src))


def _ensure_tr_import(source: str) -> str:
    if "from .common import" not in source and "from ..i18n import tr" not in source:
        return source
    if re.search(r"from \.common import[^\n]*\btr\b", source):
        return source  # already imported (possibly multi-line - checked below too)
    # Multi-line "from .common import (\n    ...\n)" form.
    match = re.search(r"from \.common import \(\n(.*?)\n\)", source, re.DOTALL)
    if match:
        names = [n.strip().rstrip(",") for n in match.group(1).splitlines() if n.strip()]
        if "tr" in names:
            return source
        names.append("tr")
        names.sort()
        block = "from .common import (\n" + "".join(f"    {n},\n" for n in names) + ")"
        return source[: match.start()] + block + source[match.end() :]
    # Single-line "from .common import a, b" form.
    match = re.search(r"from \.common import (.+)", source)
    if match:
        names = [n.strip() for n in match.group(1).split(",")]
        if "tr" in names:
            return source
        names.append("tr")
        names.sort()
        return source[: match.start()] + "from .common import " + ", ".join(names) + source[match.end() :]
    return source


def process_file(path: Path, strings: set[str], *, dry_run: bool) -> int:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines, char_offsets = _prepare(source)
    spans: list[_Span] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            _process_call(node, source, lines, char_offsets, spans, strings)
    if not spans:
        return 0
    spans.sort(key=lambda s: s.start, reverse=True)
    new_source = source
    for span in spans:
        new_source = new_source[: span.start] + span.replacement + new_source[span.end :]
    if path.name != "common.py":
        new_source = _ensure_tr_import(new_source)
    if not dry_run:
        path.write_text(new_source, encoding="utf-8")
    return len(spans)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    strings: set[str] = set()
    total = 0
    for path in sorted(UI_DIR.glob("*.py")):
        count = process_file(path, strings, dry_run=dry_run)
        if count:
            print(f"{path.name}: wrapped {count} string(s)")
        total += count
    print(f"TOTAL wrapped: {total}, distinct strings: {len(strings)}")
    manifest = {s: s for s in sorted(strings)}
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Manifest written to {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
