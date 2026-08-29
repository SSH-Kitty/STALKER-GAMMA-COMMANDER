"""Reusable widgets: drop zone, severity chips and the finding detail pane."""

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
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..findings import (
    SEVERITY_COLOR,
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

    def is_compact(self) -> bool:
        return self._compact

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


def severity_chip(severity: Severity) -> QLabel:
    """Small coloured pill showing the severity label."""
    chip = QLabel(SEVERITY_LABEL[severity])
    chip.setObjectName("chip")
    color = SEVERITY_COLOR[severity]
    chip.setStyleSheet(
        f"color:{color}; border:1px solid {color}; background: transparent;"
    )
    return chip


#: Object names for the solid table pills (theme: QLabel.sevPill + #sev*).
_SEVERITY_PILL_NAMES = {
    Severity.FATAL: "sevFatal",
    Severity.ERROR: "sevError",
    Severity.WARNING: "sevWarning",
    Severity.INFO: "sevInfo",
}


def severity_pill(severity: Severity) -> QLabel:
    """Solid-background severity pill for table rows (COMMANDER status style)."""
    pill = QLabel(SEVERITY_LABEL[severity])
    pill.setProperty("class", "sevPill")
    pill.setObjectName(_SEVERITY_PILL_NAMES[severity])
    pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return pill


class DetailPane(QWidget):
    """Full view of one finding: what happened, where, why, how to fix."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("detailCard")
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self.chip = severity_chip(Severity.INFO)
        top.addWidget(self.chip)
        self.category_label = QLabel()
        self.category_label.setObjectName("dim")
        top.addWidget(self.category_label)
        top.addStretch(1)
        layout.addLayout(top)

        self.title_label = QLabel("Select a finding to see details")
        self.title_label.setObjectName("detailTitle")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.where_label = QLabel("")
        self.where_label.setObjectName("dim")
        layout.addWidget(self.where_label)

        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.detail_label)

        suggestion_frame = QFrame()
        suggestion_frame.setObjectName("suggestionCard")
        suggestion_layout = QVBoxLayout(suggestion_frame)
        suggestion_layout.setContentsMargins(10, 8, 10, 8)
        how = QLabel("How to fix it")
        how.setObjectName("suggestionHow")
        self.suggestion_label = QLabel("")
        self.suggestion_label.setObjectName("suggestionText")
        self.suggestion_label.setWordWrap(True)
        self.suggestion_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        suggestion_layout.addWidget(how)
        suggestion_layout.addWidget(self.suggestion_label)
        layout.addWidget(suggestion_frame)

        excerpt_row = QHBoxLayout()
        excerpt_caption = QLabel("LOG EXCERPT")
        excerpt_caption.setObjectName("caption")
        excerpt_row.addWidget(excerpt_caption)
        excerpt_row.addStretch(1)
        self.copy_button = QPushButton("Copy excerpt")
        self.copy_button.setToolTip(
            "Copy the location and log excerpt to the clipboard"
        )
        self.copy_button.clicked.connect(self._copy_excerpt)
        excerpt_row.addWidget(self.copy_button)
        layout.addLayout(excerpt_row)

        self.excerpt_view = QPlainTextEdit()
        self.excerpt_view.setObjectName("excerpt")
        self.excerpt_view.setReadOnly(True)
        self.excerpt_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self.excerpt_view, 2)

        self.tech_toggle = QToolButton()
        self.tech_toggle.setText("Technical details")
        self.tech_toggle.setCheckable(True)
        self.tech_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.tech_toggle.toggled.connect(self._toggle_technical)
        self.tech_toggle.hide()
        layout.addWidget(self.tech_toggle)
        self.technical_view = QTextEditReadOnlyHolder()
        self.technical_view.hide()
        layout.addWidget(self.technical_view, 1)

        self.clear()

    def set_finding(self, finding: Finding) -> None:
        self.title_label.setText(finding.title)
        self.where_label.setText(finding.where_text())
        self.category_label.setText(finding.category)
        self.chip.setText(SEVERITY_LABEL[finding.severity])
        color = SEVERITY_COLOR[finding.severity]
        self.chip.setStyleSheet(
            f"color:{color}; border:1px solid {color}; background: transparent;"
        )
        self.detail_label.setText(finding.detail or "—")
        self.suggestion_label.setText(finding.suggestion or "No action needed.")
        self.excerpt_view.setPlainText(finding.excerpt or "(no excerpt captured)")
        has_tech = bool(finding.technical)
        self.tech_toggle.setVisible(has_tech)
        self.technical_view.setPlainText(finding.technical)
        if has_tech:
            self.tech_toggle.setChecked(False)
        else:
            self.technical_view.hide()

    def clear(self) -> None:
        self.title_label.setText("Select a finding on the left to see details")
        self.where_label.setText("")
        self.category_label.setText("")
        self.chip.setText("—")
        self.chip.setStyleSheet("")
        self.detail_label.setText("")
        self.suggestion_label.setText("")
        self.excerpt_view.clear()
        self.technical_view.clear()
        self.technical_view.hide()
        self.tech_toggle.hide()

    def _toggle_technical(self, checked: bool) -> None:
        self.tech_toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self.technical_view.setVisible(checked)

    def _copy_excerpt(self) -> None:
        text = "\n".join(
            part
            for part in (self.where_label.text(), self.excerpt_view.toPlainText())
            if part
        )
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.clipboard().setText(text)


class QTextEditReadOnlyHolder(QPlainTextEdit):
    """Monospace read-only viewer used for technical details."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("technical")
        self.setReadOnly(True)


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
