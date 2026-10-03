"""Reusable widgets: drop zone, findings list and the finding detail pane."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..findings import (
    SEVERITY_LABEL,
    Finding,
    Severity,
)
from . import theme


class DropZone(QFrame):
    """Drag-and-drop / click-to-browse box for log-dump zip archives."""

    dropped = Signal(object)  # list[Path]
    rejected_drop = Signal(str)
    clicked_browse = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(220)
        self._hover = False
        self._compact = False
        self._pressed = False
        self._reject = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_compact(self, compact: bool) -> None:
        """Shrink to a slim bar for the analysis view."""
        self._compact = compact
        self.setMinimumHeight(44 if compact else 220)
        self.setMaximumHeight(52 if compact else 260)
        self.update()

    def flash_reject(self) -> None:
        """Paint a red dashed border briefly (custom paintEvent state)."""
        self._reject = True
        self._hover = False
        self.update()
        QTimer.singleShot(700, self._clear_reject)

    def _clear_reject(self) -> None:
        self._reject = False
        self.update()

    # --- drag & drop -----------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if not mime.hasUrls():
            event.ignore()
            return
        # Accept any file drag so dropEvent always fires and wrong-file drops
        # can give feedback; only zip drags get the green hover highlight.
        event.acceptProposedAction()
        self._hover = bool(_zip_paths(mime))
        self.update()

    def dragLeaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._hover = False
        paths = _zip_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.dropped.emit(paths)
            return
        event.acceptProposedAction()
        self.flash_reject()
        self.rejected_drop.emit("Only .zip archives can be analyzed.")

    # --- click handling ----------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            was_pressed = self._pressed
            self._pressed = False
            event.accept()
            # Only treat as a click when the press started here too, so a
            # drag-release from elsewhere cannot trigger the dialog.
            if was_pressed:
                self.clicked_browse.emit()
            return
        super().mouseReleaseEvent(event)

    # --- painting ----------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = 12.0
        rect = self.rect().adjusted(1, 1, -2, -2)

        # Card body (solid fill like COMMANDER's #card panels).
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(
            QColor(
                "#2d1a18"
                if self._reject
                    else theme.token("CARD")
                if not self._hover
                else "#22301f"
            )
        )
        painter.drawRoundedRect(rect, radius, radius)

        # Dashed border: red on reject-flash, accent on hover, quiet otherwise.
        if self._reject:
            pen_color = theme.token("RED")
        elif self._hover:
            pen_color = theme.token("ACCENT")
        else:
            pen_color = theme.token("BORDER")
        pen = QPen(QColor(pen_color), 2, Qt.PenStyle.DashLine)
        pen.setDashPattern([5, 4])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(5, 5, -5, -5), radius - 3, radius - 3)

        if self._reject:
            title_color = theme.token("RED")
        elif self._hover:
            title_color = theme.token("ACCENT")
        else:
            title_color = theme.token("TEXT")
        sub_color = theme.token("DIM")

        if self._compact:
            painter.setPen(QColor(title_color))
            font = painter.font()
            font.setPointSize(12)
            font.setBold(False)
            painter.setFont(font)
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignCenter,
                "+ Open another dump: drop a ZIP here or click",
            )
            return

        third = max(1, rect.height() // 3)

        # Drawn down-arrow icon in the upper zone.
        cx = rect.center().x()
        arrow_top = rect.top() + int(third * 0.55)
        arrow_size = 13
        icon_color = QColor(theme.token("ACCENT")) if self._hover else QColor(theme.token("DIM"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(icon_color)
        from PySide6.QtCore import QPointF

        painter.drawPolygon(
            [
                QPointF(cx - arrow_size, arrow_top),
                QPointF(cx + arrow_size, arrow_top),
                QPointF(cx, arrow_top + int(arrow_size * 1.2)),
            ]
        )

        # Title in the middle zone.
        painter.setPen(QColor(title_color))
        font = painter.font()
        font.setPointSize(15)
        font.setBold(True)
        painter.setFont(font)
        title_rect = rect.adjusted(24, third, -24, -(third - 10))
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            "Drop a COMMANDER log dump ZIP here",
        )

        # Subtitle in the lower zone.
        painter.setPen(QColor(sub_color))
        small = painter.font()
        small.setBold(False)
        small.setPointSize(11)
        painter.setFont(small)
        sub_rect = rect.adjusted(24, 2 * third - 8, -24, -14)
        painter.drawText(
            sub_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "or click to browse. Every log inside is analyzed automatically",
        )


def _zip_paths(mime: QMimeData) -> list[Path]:
    if not mime.hasUrls():
        return []
    paths: list[Path] = []
    for url in mime.urls():
        local = url.toLocalFile()
        if local and local.lower().endswith(".zip"):
            candidate = Path(local)
            if candidate.is_file():
                paths.append(candidate)
    return paths


#: Value of the ``sev`` style property (theme: #sevPill / #sevBar).
_SEVERITY_KEYS = {
    Severity.FATAL: "fatal",
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.INFO: "info",
    Severity.OK: "ok",
}

#: How the findings list is split, most urgent first. The last group holds
#: entries that need no action and starts collapsed when anything else exists.
FINDING_GROUPS: tuple[tuple[str, tuple[Severity, ...]], ...] = (
    ("Fix these first", (Severity.FATAL, Severity.ERROR)),
    ("Worth checking", (Severity.WARNING,)),
    ("Good to know, no action needed", (Severity.INFO, Severity.OK)),
)


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def severity_pill(severity: Severity) -> QLabel:
    """Solid-background severity pill (COMMANDER status style)."""
    pill = QLabel(SEVERITY_LABEL[severity])
    pill.setObjectName("sevPill")
    pill.setProperty("sev", _SEVERITY_KEYS[severity])
    pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return pill


def _display_title(finding: Finding) -> str:
    return finding.title.removesuffix(".")


def _location(finding: Finding) -> str:
    where = finding.where_label or finding.arcname
    if finding.line_no is not None:
        where += f", line {finding.line_no}"
    return where


class FindingCard(QFrame):
    """One clickable row in the findings list."""

    clicked = Signal(object)  # Finding

    def __init__(self, finding: Finding, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.finding = finding
        self.setObjectName("findingCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 12, 0)
        row.setSpacing(12)

        bar = QFrame()
        bar.setObjectName("sevBar")
        bar.setProperty("sev", _SEVERITY_KEYS[finding.severity])
        bar.setFixedWidth(4)
        row.addWidget(bar)

        text = QVBoxLayout()
        text.setContentsMargins(0, 9, 0, 9)
        text.setSpacing(2)
        title = QLabel(_display_title(finding))
        title.setObjectName("findingTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        text.addWidget(title)
        meta = QLabel(f"{finding.category}  ·  {_location(finding)}")
        meta.setObjectName("findingMeta")
        meta.setTextFormat(Qt.TextFormat.PlainText)
        meta.setWordWrap(True)
        text.addWidget(meta)
        row.addLayout(text, 1)

        if finding.count > 1:
            badge = QLabel(f"{finding.count}×")
            badge.setObjectName("countBadge")
            badge.setToolTip(f"Seen {finding.count} times")
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        _repolish(self)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.finding)
            event.accept()
            return
        super().mousePressEvent(event)


class FindingList(QScrollArea):
    """Findings grouped by urgency; Up/Down keys move the selection."""

    selected = Signal(object)  # Finding

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("findingScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._findings: list[Finding] = []
        self._needle = ""
        self._minor_open = False
        self._cards: list[FindingCard] = []
        self._current: Finding | None = None

    def set_findings(self, findings: list[Finding]) -> None:
        """Show a new dump's findings; resets search and collapse state."""
        self._findings = findings
        self._needle = ""
        has_action = any(f.severity >= Severity.WARNING for f in findings)
        self._minor_open = not has_action
        self._current = None
        self._rebuild()

    def set_filter(self, needle: str) -> None:
        self._needle = needle.strip().lower()
        self._rebuild()

    def _matches(self, finding: Finding) -> bool:
        if not self._needle:
            return True
        haystack = (
            f"{finding.title} {finding.detail} {finding.category} "
            f"{finding.arcname} {finding.where_label}"
        ).lower()
        return self._needle in haystack

    def _toggle_minor(self) -> None:
        self._minor_open = not self._minor_open
        self._rebuild()

    def _rebuild(self) -> None:
        container = QWidget()
        container.setObjectName("findingList")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(6)
        self._cards = []
        visible = [f for f in self._findings if self._matches(f)]
        minor_name = FINDING_GROUPS[-1][0]

        for name, severities in FINDING_GROUPS:
            group = [f for f in visible if f.severity in severities]
            # Confirmed-normal (green) entries lead their group.
            group.sort(key=lambda f: f.severity != Severity.OK)
            if not group:
                continue
            is_minor = name == minor_name
            # A search always reveals matching minor entries.
            collapsed = is_minor and not self._minor_open and not self._needle
            header = QHBoxLayout()
            header.setContentsMargins(2, 10 if self._cards else 0, 0, 2)
            label = QLabel(f"{name.upper()}  ·  {len(group)}")
            label.setObjectName("groupHeader")
            label.setProperty("sev", _SEVERITY_KEYS[severities[0]])
            header.addWidget(label)
            header.addStretch(1)
            if is_minor and not self._needle and len(visible) > len(group):
                toggle = QPushButton("Show" if collapsed else "Hide")
                toggle.setObjectName("linkButton")
                toggle.setCursor(Qt.CursorShape.PointingHandCursor)
                toggle.clicked.connect(self._toggle_minor)
                header.addWidget(toggle)
            layout.addLayout(header)
            if collapsed:
                continue
            for finding in group:
                card = FindingCard(finding)
                card.clicked.connect(self._select)
                layout.addWidget(card)
                self._cards.append(card)

        if not visible:
            empty = QLabel(
                f"Nothing matches \u201c{self._needle}\u201d."
                if self._needle
                else "No findings. The scanned logs contain no crashes, "
                "errors or warnings."
            )
            empty.setObjectName("emptyState")
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty)
        layout.addStretch(1)
        self.setWidget(container)

        shown = [card.finding for card in self._cards]
        if self._current in shown:
            self._select(self._current)
        elif shown:
            self._select(shown[0])
        else:
            self._current = None
            self.selected.emit(None)

    def _select(self, finding: Finding) -> None:
        self._current = finding
        for card in self._cards:
            is_current = card.finding is finding
            card.set_selected(is_current)
            if is_current:
                self.ensureWidgetVisible(card, 0, 40)
        self.selected.emit(finding)

    def keyPressEvent(self, event) -> None:
        step = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1}.get(event.key())
        shown = [card.finding for card in self._cards]
        if step is None or not shown:
            super().keyPressEvent(event)
            return
        index = shown.index(self._current) if self._current in shown else -step
        self._select(shown[max(0, min(len(shown) - 1, index + step))])


class DetailPane(QFrame):
    """Full view of one finding: what happened, how to fix it, where."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("detailCard")
        self._finding: Finding | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("detailScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("detailBody")
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(6)

        top = QHBoxLayout()
        self.pill = severity_pill(Severity.INFO)
        top.addWidget(self.pill)
        self.category_label = QLabel()
        self.category_label.setTextFormat(Qt.TextFormat.PlainText)
        self.category_label.setObjectName("dim")
        top.addWidget(self.category_label)
        top.addStretch(1)
        layout.addLayout(top)

        self.title_label = _wrapped("detailTitle")
        layout.addWidget(self.title_label)
        layout.addSpacing(8)

        self.what_caption = _caption("What happened")
        layout.addWidget(self.what_caption)
        self.detail_label = _wrapped(selectable=True)
        layout.addWidget(self.detail_label)
        layout.addSpacing(8)

        self.fix_card = QFrame()
        self.fix_card.setObjectName("suggestionCard")
        fix_layout = QVBoxLayout(self.fix_card)
        fix_layout.setContentsMargins(14, 10, 14, 12)
        fix_layout.setSpacing(4)
        self.fix_caption = QLabel()
        self.fix_caption.setObjectName("suggestionHow")
        fix_layout.addWidget(self.fix_caption)
        self.suggestion_label = _wrapped("suggestionText", selectable=True)
        fix_layout.addWidget(self.suggestion_label)
        layout.addWidget(self.fix_card)
        layout.addSpacing(8)

        self.where_caption = _caption("Where it was found")
        layout.addWidget(self.where_caption)
        self.where_label = _wrapped(selectable=True)
        layout.addWidget(self.where_label)
        self.file_label = _wrapped("dim", selectable=True)
        layout.addWidget(self.file_label)
        layout.addSpacing(10)

        buttons = QHBoxLayout()
        self.excerpt_toggle = QPushButton("Show log lines")
        self.excerpt_toggle.setCheckable(True)
        self.excerpt_toggle.toggled.connect(self._toggle_excerpt)
        buttons.addWidget(self.excerpt_toggle)
        self.tech_toggle = QPushButton("Show technical details")
        self.tech_toggle.setCheckable(True)
        self.tech_toggle.toggled.connect(self._toggle_technical)
        buttons.addWidget(self.tech_toggle)
        buttons.addStretch(1)
        self.copy_button = QPushButton("Copy for bug report")
        self.copy_button.setToolTip(
            "Copy this finding and its log lines to the clipboard "
            "(personal paths and tokens are removed)"
        )
        self.copy_button.clicked.connect(self._copy)
        buttons.addWidget(self.copy_button)
        self.buttons = QWidget()
        self.buttons.setObjectName("detailButtons")
        self.buttons.setLayout(buttons)
        buttons.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.buttons)

        self.excerpt_view = _code_view("excerpt")
        layout.addWidget(self.excerpt_view)
        self.technical_view = _code_view("technical")
        layout.addWidget(self.technical_view)
        layout.addStretch(1)

        self._content = (
            self.pill,
            self.what_caption,
            self.fix_card,
            self.where_caption,
            self.where_label,
            self.file_label,
            self.buttons,
        )
        self.clear()

    def set_finding(self, finding: Finding | None) -> None:
        if finding is None:
            self.clear()
            return
        self._finding = finding
        for widget in self._content:
            widget.show()
        self.pill.setText(SEVERITY_LABEL[finding.severity])
        self.pill.setProperty("sev", _SEVERITY_KEYS[finding.severity])
        _repolish(self.pill)
        self.category_label.setText(finding.category)
        self.title_label.setText(_display_title(finding))
        self.detail_label.setText(finding.detail or "No further explanation available.")
        self.detail_label.setVisible(True)

        actionable = finding.severity >= Severity.WARNING
        self.fix_caption.setText("HOW TO FIX IT" if actionable else "DO I NEED TO DO ANYTHING?")
        self.fix_card.setProperty("tone", "action" if actionable else "calm")
        _repolish(self.fix_card)
        self.suggestion_label.setText(finding.suggestion or "Nothing to do.")

        where = _location(finding)
        if finding.count > 1:
            where += f", seen {finding.count} times"
        self.where_label.setText(where)
        self.file_label.setText(f"File in the dump: {finding.arcname}")

        self.excerpt_view.setPlainText(finding.excerpt)
        self.technical_view.setPlainText(finding.technical)
        self.excerpt_toggle.setVisible(bool(finding.excerpt))
        self.tech_toggle.setVisible(bool(finding.technical))
        self.excerpt_toggle.setChecked(False)
        self.tech_toggle.setChecked(False)
        self.excerpt_view.hide()
        self.technical_view.hide()

    def clear(self) -> None:
        self._finding = None
        for widget in (*self._content, self.excerpt_view, self.technical_view):
            widget.hide()
        self.detail_label.hide()
        self.category_label.setText("")
        self.title_label.setText(
            "Pick an item on the left to see what it means and how to fix it."
        )

    def _toggle_excerpt(self, checked: bool) -> None:
        self.excerpt_toggle.setText("Hide log lines" if checked else "Show log lines")
        self.excerpt_view.setVisible(checked)

    def _toggle_technical(self, checked: bool) -> None:
        self.tech_toggle.setText(
            "Hide technical details" if checked else "Show technical details"
        )
        self.technical_view.setVisible(checked)

    def _copy(self) -> None:
        # Route through the same redaction the export path uses - a
        # one-click "copy this and paste it into a bug report" action must
        # not be the one place secrets slip out unfiltered.
        from PySide6.QtGui import QGuiApplication

        from ..report import _redact

        finding = self._finding
        if finding is None:
            return
        text = "\n".join(
            part
            for part in (
                f"[{SEVERITY_LABEL[finding.severity]}] {finding.title}",
                f"{finding.arcname}"
                + (f" line {finding.line_no}" if finding.line_no is not None else ""),
                finding.excerpt,
            )
            if part
        )
        QGuiApplication.clipboard().setText(_redact(text))
        self.copy_button.setText("Copied")
        QTimer.singleShot(1500, lambda: self.copy_button.setText("Copy for bug report"))


def _wrapped(name: str = "", selectable: bool = False) -> QLabel:
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if name:
        label.setObjectName(name)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _caption(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("caption")
    return label


def _code_view(name: str) -> QPlainTextEdit:
    view = QPlainTextEdit()
    view.setObjectName(name)
    view.setReadOnly(True)
    view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    view.setFixedHeight(220)
    return view


def fill_recent_list(widget, dumps: Iterable[Path]) -> None:
    """Populate a QListWidget with human-friendly recent dump entries."""
    from datetime import datetime

    widget.clear()
    for path in dumps:
        try:
            stamp = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            when = stamp.strftime("%Y-%m-%d %H:%M")
        except OSError:
            when = "unknown date"
        item_text = f"{path.name}   ({when})"
        widget.addItem(item_text)
        item = widget.item(widget.count() - 1)
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        item.setToolTip(str(path))
