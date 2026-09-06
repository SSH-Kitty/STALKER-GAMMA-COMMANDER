"""Main window: welcome drop screen and the analysis workspace."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import report as report_mod
from ..analyzers import run_analysis, scanned_files
from ..dump import DumpArchive, DumpError, default_dumps_dir, human_size, recent_dumps
from ..findings import (
    CATEGORIES,
    Finding,
    Severity,
    summarize,
)
from . import theme
from .widgets import DetailPane, DropZone, fill_recent_list, severity_pill

_MIN_SEVERITY_FILTERS = (
    ("Everything", Severity.INFO),
    ("Warnings and worse", Severity.WARNING),
    ("Errors and worse", Severity.ERROR),
    ("Critical only", Severity.FATAL),
)


class _Worker(QObject):
    """Runs ``fn(*args, **kwargs)`` on a worker thread and reports back."""

    result = Signal(object)
    error = Signal(object)

    def __init__(self, fn, args, kwargs) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            value = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
            self.error.emit(exc)
        else:
            self.result.emit(value)


def _safe_mtime(path: Path) -> float:
    """Modification time that survives files deleted mid-refresh."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class MainWindow(QMainWindow):
    """COMMANDER Assistant's single-window interface."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("COMMANDER Assistant")
        self.resize(1180, 740)
        self.setAcceptDrops(True)

        self._dump: DumpArchive | None = None
        self._findings: list[Finding] = []
        self._partial = False
        self._session_paths: list[Path] = []
        self._pending_candidates: list[Path] = []
        self._pending_dump: DumpArchive | None = None
        self._pending_partial = False
        self._bg_thread: QThread | None = None
        self._bg_worker: _Worker | None = None

        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        # --- top bar -----------------------------------------------------
        header = QWidget()
        header.setObjectName("topbar")
        top_bar = QHBoxLayout(header)
        top_bar.setContentsMargins(16, 0, 8, 0)
        top_bar.setSpacing(16)
        wordmark_block = QWidget()
        wordmark_block.setObjectName("wordmarkBlock")
        wordmark_layout = QVBoxLayout(wordmark_block)
        wordmark_layout.setContentsMargins(0, 0, 0, 0)
        wordmark_layout.setSpacing(0)
        wordmark = QLabel("ASSISTANT")
        wordmark.setObjectName("wordmark")
        wordmark_layout.addWidget(wordmark)
        byline = QLabel("by SSH-Kitty")
        byline.setObjectName("byline")
        byline.setAlignment(Qt.AlignmentFlag.AlignRight)
        wordmark_layout.addWidget(byline)
        top_bar.addWidget(wordmark_block)
        top_bar.addStretch(1)
        self.theme_combo = QComboBox()
        self.theme_combo.setObjectName("themeSelector")
        for key, label in theme.THEME_INFO:
            self.theme_combo.addItem(label, key)
        self.theme_combo.setToolTip("COMMANDER theme")
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(theme.active_theme()))
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        top_bar.addWidget(self.theme_combo)
        open_button = QPushButton("Open ZIP...")
        open_button.clicked.connect(self._browse)
        top_bar.addWidget(open_button)
        self.recent_combo = QComboBox()
        self.recent_combo.setMinimumWidth(280)
        self.recent_combo.setToolTip("Recently created or opened log dumps")
        self.recent_combo.activated.connect(self._on_recent_selected)
        top_bar.addWidget(self.recent_combo)
        self.save_button = QPushButton("Save analysis")
        self.save_button.setObjectName("primary")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_analysis)
        top_bar.addWidget(self.save_button)
        root.addWidget(header)

        # --- quick actions row (below the logo, tab-styled) ---------------
        quick_actions = QWidget()
        quick_actions.setObjectName("quickActions")
        actions_layout = QHBoxLayout(quick_actions)
        actions_layout.setContentsMargins(8, 0, 8, 0)
        actions_layout.setSpacing(0)
        open_logs_button = QPushButton("Open log folder")
        open_logs_button.setObjectName("tabButton")
        open_logs_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_logs_button.setToolTip(
            f"Open the folder where log dumps are stored:\n{default_dumps_dir()}"
        )
        open_logs_button.clicked.connect(self._open_dumps_folder)
        actions_layout.addStretch(1)
        actions_layout.addWidget(open_logs_button)
        root.addWidget(quick_actions)

        # --- stacked content ---------------------------------------------
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_welcome())
        self.stack.addWidget(self._build_analysis())

        self._refresh_recents()
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self._browse)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save_analysis)

    # ------------------------------------------------------------------ UI

    def _build_welcome(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(44, 30, 44, 22)
        layout.setSpacing(12)

        # Page title, mirroring COMMANDER's #section1 page headers.
        page_title = QLabel("LOG ANALYSIS")
        page_title.setObjectName("pageTitle")
        page_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(page_title)

        headline = QLabel("From crash logs to clear answers.")
        headline.setObjectName("heroTitle")
        headline.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(headline)

        subtitle = QLabel(
            "Open a COMMANDER log dump ZIP created by GAMMA COMMANDER.\n"
            "Every log inside is scanned. STALKER GAMMA crashes, installer "
            "failures, and Wine/MO2 problems are explained in plain language, "
            "with the exact file, line number, and a suggested fix."
        )
        subtitle.setObjectName("heroSub")
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(subtitle)
        layout.addSpacing(2)

        self.welcome_drop = DropZone()
        self.welcome_drop.dropped.connect(self._open_paths)
        self.welcome_drop.rejected_drop.connect(self._flash_reject)
        self.welcome_drop.clicked_browse.connect(self._browse)
        layout.addWidget(self.welcome_drop)
        layout.addSpacing(4)

        # How it works: three step cards using COMMANDER's green "Step N." pattern.
        steps_row = QHBoxLayout()
        steps_row.setSpacing(12)
        steps = [
            (
                1,
                (
                    "In COMMANDER, run Utilities → Create Log Dump to bundle your "
                    "logs into one ZIP."
                ),
            ),
            (2, "Drop that ZIP anywhere on this window, or click to browse."),
            (
                3,
                "Read the findings, follow the fixes, and export a shareable report.",
            ),
        ]
        for number, text in steps:
            steps_row.addWidget(self._step_card(number, text), 1)
        layout.addLayout(steps_row)
        layout.addSpacing(2)

        # Recent dumps card.
        self.recent_card = QFrame()
        self.recent_card.setObjectName("card")
        card_layout = QVBoxLayout(self.recent_card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(6)
        card_header = QLabel("Recent log dumps")
        card_header.setObjectName("section2")
        card_layout.addWidget(card_header)
        self.recent_list = QListWidget()
        self.recent_list.setMaximumHeight(150)
        self.recent_list.itemDoubleClicked.connect(self._recent_item_activated)
        card_layout.addWidget(self.recent_list)
        self.recent_caption = QLabel(f"Stored in {default_dumps_dir()}")
        self.recent_caption.setObjectName("dim")
        card_layout.addWidget(self.recent_caption)
        layout.addWidget(self.recent_card)
        layout.addStretch(1)
        return page

    def _step_card(self, number: int, text: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(12, 10, 12, 10)
        label = QLabel()
        label.setProperty("step_number", number)
        label.setProperty("step_text", text)
        self._theme_step_labels = getattr(self, "_theme_step_labels", [])
        self._theme_step_labels.append(label)
        self._refresh_theme_labels()
        label.setObjectName("info")
        label.setWordWrap(True)
        inner.addWidget(label)
        return frame

    def _refresh_theme_labels(self) -> None:
        for label in getattr(self, "_theme_step_labels", []):
            label.setText(
                f"<span style='color:{theme.token('ACCENT')}; font-weight:700;'>"
                f"Step {label.property('step_number')}.</span> "
                f"{label.property('step_text')}"
            )

    def _theme_changed(self, index: int) -> None:
        name = self.theme_combo.itemData(index)
        if not isinstance(name, str):
            return
        app = QApplication.instance()
        if app is None:
            return
        theme.apply_theme(app, name)
        theme.save_theme(name)
        self._refresh_theme_labels()
        for widget in self.findChildren(QWidget):
            widget.update()

    def _build_analysis(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(8)

        self.analysis_drop = DropZone()
        self.analysis_drop.set_compact(True)
        self.analysis_drop.dropped.connect(self._open_paths)
        self.analysis_drop.rejected_drop.connect(self._flash_reject)
        self.analysis_drop.clicked_browse.connect(self._browse)
        layout.addWidget(self.analysis_drop)

        self.banner = QLabel("")
        self.banner.setObjectName("bannerGood")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Show findings:"))
        self.severity_filter = QComboBox()
        for label, _minimum in _MIN_SEVERITY_FILTERS:
            self.severity_filter.addItem(label)
        self.severity_filter.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self.severity_filter)
        self.category_filter = QComboBox()
        self.category_filter.addItem("All finding categories")
        self.category_filter.addItems(CATEGORIES)
        self.category_filter.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self.category_filter)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search findings... (file, summary, or log)")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._apply_filters)
        filter_row.addWidget(self.search_box, 1)
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("dim")
        filter_row.addWidget(self.stats_label)
        layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Severity", "Category", "What happened", "Where", "Times"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnWidth(0, 96)
        self.table.setColumnWidth(1, 140)
        self.table.setColumnWidth(4, 60)
        self.table.horizontalHeader().setStretchLastSection(False)
        from PySide6.QtWidgets import QHeaderView

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.itemSelectionChanged.connect(self._on_row_selected)

        # Table + friendly empty state share the left pane; the empty label
        # appears whenever the active filters hide every finding.
        self._row_findings: dict[int, Finding] = {}
        table_page = QWidget()
        table_layout = QVBoxLayout(table_page)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(self.table)
        self.empty_state = QLabel(
            "No findings match these filters.\n\n"
            "Try changing the severity, category, or search text above."
        )
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state.hide()
        table_layout.addWidget(self.empty_state, 1)
        splitter.addWidget(table_page)

        self.detail = DetailPane()
        splitter.addWidget(self.detail)
        splitter.setSizes([620, 460])
        layout.addWidget(splitter, 1)
        return page

    # ------------------------------------------------------------- actions

    def _open_dumps_folder(self) -> None:
        from .common import open_in_file_manager

        directory = default_dumps_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not open_in_file_manager(directory):
            QMessageBox.warning(
                self,
                "File Manager Not Found",
                "No file manager was found to open:\n"
                f"{directory}\n\nOpen this location manually in your terminal.",
            )

    def _browse(self) -> None:
        """Open the file picker with the main window as parent.

        Parenting to ``self`` (the top-level window, not an embedded widget)
        keeps the modal dialog stacked above the app on Wayland/KDE, where
        child-of-widget dialogs can otherwise open behind the window.
        """
        start = str(default_dumps_dir())
        try:
            default_dumps_dir().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open COMMANDER Log Dump Archive",
            start if Path(start).is_dir() else str(Path.home()),
            "Log dump archives (*.zip);;All files (*)",
        )
        if path:
            self._open_paths([Path(path)])

    def dragEnterEvent(self, event) -> None:
        # Accept any file drag so dropEvent fires and wrong-file drops can be
        # reported; zip filtering happens in dropEvent.
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [
            Path(u.toLocalFile())
            for u in event.mimeData().urls()
            if u.toLocalFile().lower().endswith(".zip")
        ]
        event.acceptProposedAction()
        if paths:
            self._open_paths(paths)
            return
        self._flash_reject("Only .zip archives can be analyzed.")

    def _run_in_background(self, fn, *args, on_result, on_error, **kwargs) -> None:
        """Run ``fn(*args, **kwargs)`` off the UI thread; deliver the result back on it.

        A large, legitimately-sized log dump (a long play session's xray/launcher
        logs, up to the 256 MB decode cap in dump.py) can take a noticeable time
        to open and scan; running it inline on the UI thread would freeze the
        window for that whole span.
        """
        thread = QThread(self)
        worker = _Worker(fn, args, kwargs)
        worker.moveToThread(thread)
        worker.result.connect(on_result)
        worker.error.connect(on_error)
        worker.result.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.started.connect(worker.run)

        def _cleanup(thread=thread) -> None:
            # on_result may have already chained a second stage (a new
            # thread) before this stage's own finished signal is delivered -
            # only clear tracking / restore the cursor if nothing else has
            # claimed self._bg_thread in the meantime.
            if self._bg_thread is thread:
                self._bg_thread = None
                self._bg_worker = None
                self._set_busy(False)

        thread.finished.connect(_cleanup)
        self._bg_thread = thread
        self._bg_worker = worker
        thread.start()

    def _set_busy(self, busy: bool) -> None:
        if busy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()
        self.welcome_drop.setEnabled(not busy)
        self.analysis_drop.setEnabled(not busy)

    def _open_paths(self, paths: object) -> None:
        if self._bg_thread is not None:
            # A previous open/analyze run is still in flight - the drop zone
            # is disabled meanwhile, but a recent-item click can still race it.
            return
        candidates = [Path(p) for p in paths]  # type: ignore[arg-type]
        if not candidates:
            return
        self._pending_candidates = candidates
        self._set_busy(True)
        self._run_in_background(
            DumpArchive.open,
            candidates[0],
            on_result=self._on_dump_opened,
            on_error=self._on_open_error,
        )

    def _on_open_error(self, exc: object) -> None:
        message = str(exc) if isinstance(exc, DumpError) else f"Unexpected error: {exc}"
        QMessageBox.warning(self, "Could Not Open Archive", message)

    def _on_dump_opened(self, dump: object) -> None:
        assert isinstance(dump, DumpArchive)
        partial = not dump.looks_like_dump()
        if partial:
            answer = QMessageBox.question(
                self,
                "Unrecognised Archive",
                "This ZIP does not look like a COMMANDER log dump (no "
                "MANIFEST.txt or standard folders).\n\nAnalyze it anyway with a "
                "generic scan?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        for path in self._pending_candidates:
            if path not in self._session_paths:
                self._session_paths.append(path)
        # Stash on self rather than a closure: a plain lambda has no thread
        # affinity Qt can resolve, so the cross-thread result signal below
        # would invoke it directly on the worker thread instead of queuing
        # it onto the main thread - exactly the UI-from-a-background-thread
        # bug this whole background-worker change exists to avoid.
        self._pending_dump = dump
        self._pending_partial = partial
        self._run_in_background(
            run_analysis,
            dump,
            partial=partial,
            on_result=self._on_analysis_done,
            on_error=self._on_analysis_error,
        )

    def _on_analysis_error(self, exc: object) -> None:
        QMessageBox.warning(self, "Analysis Failed", f"Could not analyze the archive: {exc}")

    def _on_analysis_done(self, findings: object) -> None:
        assert isinstance(findings, list)
        self._dump = self._pending_dump
        self._findings = findings
        self._partial = self._pending_partial
        self._populate_analysis_view()
        self.stack.setCurrentIndex(1)
        self._refresh_recents()

    def _populate_analysis_view(self) -> None:
        assert self._dump is not None
        dump_name = self._dump.path.name
        self.setWindowTitle(f"{dump_name} — COMMANDER Assistant")
        text_files, binary_files = scanned_files(self._dump)
        summary = summarize(
            self._findings, len(self._dump.files), partial=self._partial
        )
        self.banner.setText(f"<b>{summary.headline}.</b> {summary.sentence()}")
        self.banner.setObjectName(f"banner{summary.verdict.capitalize()}")
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self._scan_summary = (
            f"Scanned {len(text_files)} text files and {len(binary_files)} binary "
            f"files | {human_size(self._dump.size)}"
        )
        self.severity_filter.setCurrentIndex(0)
        self.category_filter.setCurrentIndex(0)
        self.search_box.clear()
        self._apply_filters()
        self.save_button.setEnabled(True)

    def _apply_filters(self) -> None:
        self.detail.clear()
        minimum = _MIN_SEVERITY_FILTERS[max(0, self.severity_filter.currentIndex())][1]
        category = (
            self.category_filter.currentText()
            if self.category_filter.currentIndex() > 0
            else ""
        )
        needle = self.search_box.text().strip().lower()
        self.table.setRowCount(0)
        self._row_findings = {}
        visible = 0
        for finding in self._findings:
            if finding.severity < minimum:
                continue
            if category and finding.category != category:
                continue
            if needle:
                haystack = (
                    f"{finding.title} {finding.detail} {finding.arcname} "
                    f"{finding.where_label}"
                ).lower()
                if needle not in haystack:
                    continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setCellWidget(row, 0, severity_pill(finding.severity))
            self._set_cell(row, 1, finding.category)
            self._set_cell(row, 2, finding.title)
            where = finding.where_label or finding.arcname
            if finding.line_no is not None:
                where += f" : line {finding.line_no}"
            self._set_cell(row, 3, where, dim=True)
            count = str(finding.count) if finding.count > 1 else ""
            count_item = self._set_cell(row, 4, count, dim=True)
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._row_findings[row] = finding
            visible += 1

        total = len(self._findings)
        if total == 0:
            self.empty_state.setText(
                "No findings were detected. This dump looks clean.\n\n"
                "The scanned logs contain no crashes, errors, or warnings."
            )
        elif visible == 0:
            self.empty_state.setText(
                "No findings match these filters.\n\n"
                "Try changing the severity, category, or search text above."
            )
        empty = total == 0 or visible == 0
        self.empty_state.setVisible(empty)
        self.table.setVisible(not empty)
        stats = f"{visible} of {total} findings shown"
        scan = getattr(self, "_scan_summary", "")
        if scan:
            stats += f" | {scan}"
        self.stats_label.setText(stats)

    def _set_cell(self, row: int, column: int, text: str, color=None, dim=False):
        item = QTableWidgetItem(text)
        if color:
            item.setForeground(QColor(color))
        elif dim:
            item.setForeground(QColor(theme.token("DIM")))
        self.table.setItem(row, column, item)
        return item

    def _on_row_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        finding = self._row_findings.get(row)
        if isinstance(finding, Finding):
            self.detail.set_finding(finding)

    def _save_analysis(self) -> None:
        if self._dump is None:
            return
        suggested_dir = self._dump.path.parent
        if not suggested_dir.is_dir():
            suggested_dir = default_dumps_dir()
        target = suggested_dir / f"{self._dump.path.stem}-analysis.md"
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Save Analysis Report",
            str(target),
            "Markdown (*.md);;All files (*)",
        )
        if not chosen:
            return
        try:
            # The exporter honors the exact chosen name (collision-safe), so
            # no intermediate auto-named file is written first.
            written = report_mod.export_report(
                self._dump,
                self._findings,
                dest_dir=Path(chosen).parent,
                filename=Path(chosen).name,
                partial=getattr(self, "_partial", False),
            )
        except OSError as exc:
            QMessageBox.warning(
                self, "Save Failed", f"Could not write the report:\n{exc}"
            )
            return
        QMessageBox.information(
            self,
            "Analysis Saved",
            f"Your analysis has been saved to:\n\n{written}",
        )

    # -------------------------------------------------------------- recents

    def _refresh_recents(self) -> None:
        disk = list(recent_dumps(limit=8))
        merged: list[Path] = []
        for path in [*self._session_paths, *disk]:
            if path.is_file() and path not in merged:
                merged.append(path)
        # Sort defensively: a dump deleted while the app is open must not
        # crash recents refresh (vanished files sort to the end).
        merged.sort(key=_safe_mtime, reverse=True)
        merged = merged[:10]
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem("Recent log dumps...")
        for path in merged:
            self.recent_combo.addItem(path.name, str(path))
        self.recent_combo.blockSignals(False)
        for index in range(1, self.recent_combo.count()):
            path = self.recent_combo.itemData(index)
            if path:
                self.recent_combo.setItemData(
                    index, str(path), Qt.ItemDataRole.ToolTipRole
                )
        fill_recent_list(self.recent_list, merged)
        has_items = bool(merged)
        self.recent_card.setVisible(has_items)
        self.recent_list.setVisible(has_items)
        self.recent_caption.setVisible(has_items)

    def _on_recent_selected(self, index: int) -> None:
        path = self.recent_combo.itemData(index)
        self.recent_combo.setCurrentIndex(-1)
        if path:
            self._open_paths([Path(str(path))])

    def _recent_item_activated(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self._open_paths([Path(str(path))])

    def _flash_reject(self, message: str) -> None:
        self.statusBar().showMessage(message, 4000)
        for zone in (self.welcome_drop, self.analysis_drop):
            zone.flash_reject()
