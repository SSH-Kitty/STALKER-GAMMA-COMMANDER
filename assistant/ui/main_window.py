"""Main window: welcome drop screen and the analysis workspace."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
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
    QVBoxLayout,
    QWidget,
)

from commander_gui.gui_settings import load_gui_settings
from commander_gui.ui.title_bar import (
    EdgeResizeFilter,
    WindowDragFilter,
    attach_resize_filter,
    build_window_buttons,
    pin_top_right,
    update_max_button,
)

from .. import report as report_mod
from ..analyzers import run_analysis, scanned_files
from ..dump import DumpArchive, DumpError, default_dumps_dir, human_size, recent_dumps
from ..findings import Finding, summarize
from . import theme
from .widgets import DetailPane, DropZone, FindingList, fill_recent_list


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
        self.custom_title_bar = False
        self._drag_filter = WindowDragFilter(self)
        self._resize_filter = EdgeResizeFilter(self)

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
        # Follow the theme picked in COMMANDER, also while ASSISTANT is open: both
        # share gui-settings.json, which is cheap to stat.
        self._theme_mtime = self._settings_mtime()
        self._theme_watch = QTimer(self)
        self._theme_watch.setInterval(1500)
        self._theme_watch.timeout.connect(self._follow_saved_theme)
        self._theme_watch.start()
        open_logs_button = QPushButton("Log folder")
        open_logs_button.setToolTip(
            f"Open the folder where log dumps are stored:\n{default_dumps_dir()}"
        )
        open_logs_button.clicked.connect(self._open_dumps_folder)
        top_bar.addWidget(open_logs_button)
        open_button = QPushButton("Open ZIP...")
        open_button.setToolTip("Open a log dump ZIP (Ctrl+O). You can also drop one anywhere on this window.")
        open_button.clicked.connect(self._browse)
        top_bar.addWidget(open_button)
        self.recent_combo = QComboBox()
        self.recent_combo.setMinimumWidth(280)
        self.recent_combo.setToolTip("Recently created or opened log dumps")
        self.recent_combo.activated.connect(self._on_recent_selected)
        top_bar.addWidget(self.recent_combo)
        self.save_button = QPushButton("Save report")
        self.save_button.setToolTip("Save this analysis as a Markdown report (Ctrl+S)")
        self.save_button.setObjectName("primary")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_analysis)
        top_bar.addWidget(self.save_button)
        root.addWidget(header)
        self._header = header
        for widget in (header, wordmark_block, wordmark, byline):
            widget.installEventFilter(self._drag_filter)
        self._title_strip = build_window_buttons(self, self._drag_filter)
        pin_top_right(self._title_strip, header)

        # --- stacked content ---------------------------------------------
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_welcome())
        self.stack.addWidget(self._build_analysis())

        self._refresh_recents()
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self._browse)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save_analysis)
        self.apply_title_bar(bool(load_gui_settings().get("custom_title_bar", True)))

    # ----------------------------------------------------------- title bar

    def apply_title_bar(self, custom: bool) -> None:
        """Draw COMMANDER's own title bar (frameless) or use the desktop's."""
        # Offscreen/minimal platforms (tests, screenshots) have no window
        # manager to hand moves and resizes to.
        custom = custom and QGuiApplication.platformName() not in ("offscreen", "minimal")
        visible = self.isVisible()
        self.custom_title_bar = custom
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, custom)
        self._title_strip.setVisible(custom)
        # The top bar's buttons are taller than COMMANDER's, so the whole
        # strip goes above them instead of being split above and below.
        strip = self._title_strip.height() if custom else 0
        self._header.layout().setContentsMargins(16, strip, 8, strip // 2)
        if visible:
            self.show()
        attach_resize_filter(self, self._resize_filter, custom)

    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            update_max_button(self)
        super().changeEvent(event)

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

    def _apply_theme_ui(self, name: str) -> bool:
        app = QApplication.instance()
        if app is None:
            return False
        theme.apply_theme(app, name)
        self._refresh_theme_labels()
        for widget in self.findChildren(QWidget):
            widget.update()
        return True

    @staticmethod
    def _settings_mtime() -> float:
        try:
            return theme.config_path().stat().st_mtime
        except OSError:
            return 0.0

    def _follow_saved_theme(self) -> None:
        mtime = self._settings_mtime()
        if mtime == self._theme_mtime:
            return
        self._theme_mtime = mtime
        name = theme.load_saved_theme()
        if name == theme.active_theme():
            return
        self._apply_theme_ui(name)

    def _build_analysis(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        # Verdict: one glance tells the user whether anything needs fixing.
        self.verdict = QFrame()
        self.verdict.setObjectName("verdictCard")
        verdict_row = QHBoxLayout(self.verdict)
        verdict_row.setContentsMargins(16, 14, 16, 14)
        verdict_row.setSpacing(16)
        self.verdict_icon = QLabel()
        self.verdict_icon.setObjectName("verdictIcon")
        self.verdict_icon.setFixedSize(44, 44)
        self.verdict_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        verdict_row.addWidget(self.verdict_icon, 0, Qt.AlignmentFlag.AlignTop)
        verdict_text = QVBoxLayout()
        verdict_text.setSpacing(3)
        self.verdict_title = QLabel()
        self.verdict_title.setObjectName("verdictTitle")
        self.verdict_title.setTextFormat(Qt.TextFormat.PlainText)
        verdict_text.addWidget(self.verdict_title)
        self.verdict_sub = QLabel()
        self.verdict_sub.setObjectName("verdictSub")
        self.verdict_sub.setTextFormat(Qt.TextFormat.PlainText)
        self.verdict_sub.setWordWrap(True)
        verdict_text.addWidget(self.verdict_sub)
        self.verdict_meta = QLabel()
        self.verdict_meta.setObjectName("dim")
        self.verdict_meta.setTextFormat(Qt.TextFormat.PlainText)
        self.verdict_meta.setWordWrap(True)
        verdict_text.addWidget(self.verdict_meta)
        verdict_row.addLayout(verdict_text, 1)
        layout.addWidget(self.verdict)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search findings, e.g. download, prefix, MO2")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._apply_filters)
        layout.addWidget(self.search_box)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(12)
        self.finding_list = FindingList()
        splitter.addWidget(self.finding_list)
        self.detail = DetailPane()
        self.finding_list.selected.connect(self.detail.set_finding)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([560, 560])
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

        def _cleanup(thread=thread, worker=worker) -> None:
            # on_result may have already chained a second stage (a new
            # thread) before this stage's own finished signal is delivered -
            # only clear tracking / restore the cursor if nothing else has
            # claimed self._bg_thread in the meantime.
            if self._bg_thread is thread:
                self._bg_thread = None
                self._bg_worker = None
                self._set_busy(False)
            # Neither is ever reused after its one run - nothing else holds
            # a reference once _bg_thread/_bg_worker are cleared (or were
            # already replaced by a chained second stage), so both are safe
            # to schedule for deletion here regardless of which branch above
            # ran; leaving this out leaked one QThread + one _Worker per
            # dump opened/analyzed for the life of the app.
            worker.deleteLater()
            thread.deleteLater()

        # QThread.finished is emitted from the worker thread, and a plain
        # function has no thread affinity (see the note in the analyze step
        # below): hop onto this window's thread before touching widgets.
        thread.finished.connect(lambda: QTimer.singleShot(0, self, _cleanup))
        self._bg_thread = thread
        self._bg_worker = worker
        thread.start()

    def closeEvent(self, event) -> None:
        """Block a still-running background analysis from being torn down.

        _run_in_background()'s worker runs fn() synchronously on the
        QThread (no cooperative cancellation), so quit() alone can't stop
        it mid-analysis - closing the window while it's in flight would
        otherwise destroy a still-running QThread out from under it. A
        dump analysis is bounded (256 MB decode cap in dump.py) and
        normally fast, so blocking briefly here is an acceptable trade
        against a crash-on-quit.
        """
        thread = self._bg_thread
        if thread is not None and thread.isRunning() and not thread.wait(10_000):
            # Still running after the wait: closing now would destroy a live
            # QThread, which Qt answers by aborting the process.
            event.ignore()
            QMessageBox.information(
                self,
                "Still Analyzing",
                "The archive is still being analyzed. Close the window "
                "again once it finishes.",
            )
            return
        super().closeEvent(event)

    def _set_busy(self, busy: bool) -> None:
        if busy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()
        self.welcome_drop.setEnabled(not busy)

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
        if self._bg_thread is None:
            # The "Analyze anyway?" question above ran a nested event loop in
            # which stage 1's cleanup already restored the cursor and
            # re-enabled the drop zones; stage 2 must claim them again.
            self._set_busy(True)
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
        text_files, _binary_files = scanned_files(self._dump)
        summary = summarize(self._findings, len(self._dump.files), partial=self._partial)
        self.verdict.setProperty("verdict", summary.verdict)
        self.verdict_icon.setText({"bad": "!", "warn": "!", "good": "✓"}[summary.verdict])
        for widget in (self.verdict, self.verdict_icon):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.verdict_title.setText(summary.headline)
        self.verdict_sub.setText(summary.sentence())
        self.verdict_meta.setText(
            f"{dump_name}  ·  {len(text_files)} logs scanned  ·  "
            f"{human_size(self._dump.size)}"
        )
        self.search_box.blockSignals(True)
        self.search_box.clear()
        self.search_box.blockSignals(False)
        self.finding_list.set_findings(self._findings)
        self.finding_list.setFocus()
        self.save_button.setEnabled(True)

    def _apply_filters(self) -> None:
        self.finding_list.set_filter(self.search_box.text())

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
        self.welcome_drop.flash_reject()
