"""Update page: check for and apply GAMMA updates."""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..cli_runner import cli_command
from ..parsers import parse_update_diff, strip_ansi
from .common import CommandRunner, OutputPane, ProgressArea, make_card, section_label


class UpdatePage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._check_runner: CommandRunner | None = None
        self._apply_runner: CommandRunner | None = None
        self._diffs: list = []
        self._checking = False
        self._applying = False

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        # ---------- Check ----------
        card, layout = make_card()
        root.addWidget(card)
        layout.addWidget(section_label("Check for Updates"))
        row = QHBoxLayout()
        self.check_button = QPushButton("Check for Updates")
        self.check_button.setObjectName("primary")
        self.check_button.clicked.connect(self._check)
        row.addWidget(self.check_button)
        self.check_status = QLabel("")
        self.check_status.setObjectName("dim")
        row.addWidget(self.check_status)
        row.addStretch(1)
        layout.addLayout(row)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Status", "Addon", "Archive change"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, self.table.horizontalHeader().ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, self.table.horizontalHeader().ResizeMode.Stretch
        )
        layout.addWidget(self.table)

        self.check_log = OutputPane()
        self.check_log.setMaximumHeight(140)
        layout.addWidget(self.check_log)

        # ---------- Apply ----------
        apply_card, apply_layout = make_card()
        root.addWidget(apply_card)
        apply_layout.addWidget(section_label("Apply Updates"))
        self.minimal_cb = QCheckBox("Minimal (delete archives after extract)")
        self.preserve_user_cb = QCheckBox("Preserve user.ltx settings")
        self.preserve_mcm_cb = QCheckBox("Preserve MCM settings")
        for cb in (self.minimal_cb, self.preserve_user_cb, self.preserve_mcm_cb):
            apply_layout.addWidget(cb)

        self.apply_button = QPushButton("Apply Updates")
        self.apply_button.setObjectName("primary")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        apply_layout.addWidget(self.apply_button)

        self.apply_progress = ProgressArea()
        self.apply_progress.cancel_button.clicked.connect(self._cancel_apply)
        apply_layout.addWidget(self.apply_progress)

        root.addStretch(1)
        self._update_button_states()

    def refresh(self) -> None:
        self.window.refresh_settings()
        self._update_button_states()

    def on_busy_changed(self, _busy: bool) -> None:
        """Global install lock changed; re-evaluate this page's controls."""
        self._update_button_states()

    def _update_button_states(self) -> None:
        """Gate this page on the global install lock.

        ``update apply`` writes the same install tree as a full install, so it
        must never run alongside one. The in-flight flags are explicit rather
        than derived from ``is_running()``: the worker thread has not always
        stopped by the time its ``finished`` handler runs, which would leave
        the buttons stuck disabled.
        """
        idle = not self.window.install_busy and not self._checking and not self._applying
        self.check_button.setEnabled(idle)
        self.apply_button.setEnabled(idle and bool(self._diffs))

    # ----- check -----
    def _check(self) -> None:
        if self._checking or self._applying:
            return
        if self.window.install_busy:
            QMessageBox.information(
                self, "Busy", "An install is already running. Wait for it to finish."
            )
            return
        if self.window.settings.active_profile is None:
            QMessageBox.warning(
                self, "No Profile", "Create or activate a profile first (Profiles page)."
            )
            return
        self._checking = True
        self._update_button_states()
        self.table.setRowCount(0)
        self._diffs = []
        self.check_status.setText("Checking...")
        self.check_log.clear()
        self._check_runner = CommandRunner(cli_command(["update", "check"]), parent=self)
        self._check_runner.line.connect(self._on_check_line)
        self._check_runner.finished.connect(self._on_check_finished)
        self._check_runner.start()

    def _on_check_line(self, line: str) -> None:
        clean = strip_ansi(line)
        self.check_log.append_line(clean)
        diff = parse_update_diff(clean)
        if diff is not None:
            self._diffs.append(diff)
            self._add_diff_row(diff)
        elif clean.startswith("Updates available:"):
            self.check_status.setText(clean.strip())
        elif clean == "No updates found":
            self.check_status.setText("GAMMA is up to date")

    def _add_diff_row(self, diff) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        status_item = QTableWidgetItem(diff.status)
        color = {
            "Added": "#7dc963",
            "Modified": "#9fe96f",
            "Removed": "#d9a04c",
        }.get(diff.status, "#cfd9c6")
        status_item.setForeground(QColor(color))
        parts = diff.text.split(" -> ")
        name = parts[0].strip()
        change = " -> ".join(p.strip() for p in parts[1:]) if len(parts) > 1 else ""
        self.table.setItem(row, 0, status_item)
        self.table.setItem(row, 1, QTableWidgetItem(name))
        self.table.setItem(row, 2, QTableWidgetItem(change))

    def _on_check_finished(self, rc: int, _output: str) -> None:
        self._checking = False
        if rc != 0:
            self.check_status.setText("Check failed")
        self._update_button_states()

    # ----- apply -----
    def _apply(self) -> None:
        if self._applying or self._checking:
            return
        if self.window.install_busy:
            QMessageBox.information(
                self, "Busy", "An install is already running. Wait for it to finish."
            )
            return
        if not self._diffs:
            QMessageBox.information(self, "No Updates", "No updates to apply.")
            return
        answer = QMessageBox.question(
            self,
            "Confirm Update",
            f"Apply {len(self._diffs)} update(s)? This will download and re-extract "
            "the updated addons.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        args = ["update", "apply"]
        if self.minimal_cb.isChecked():
            args.append("--minimal")
        if self.preserve_user_cb.isChecked():
            args.append("--preserve-user-settings")
        if self.preserve_mcm_cb.isChecked():
            args.append("--preserve-mcm-settings")

        self._applying = True
        # Holds the global lock for the duration: this writes the install tree.
        self.window.set_install_busy(True)
        self.apply_progress.reset()
        self._apply_runner = CommandRunner(
            cli_command(args, progress_interval_ms=200), parent=self
        )
        self._apply_runner.line.connect(self.apply_progress.on_line)
        self._apply_runner.finished.connect(self._on_apply_finished)
        self._apply_runner.cancelled.connect(
            lambda: self.apply_progress.log.append_line("[cancelled]")
        )
        self.apply_progress.on_started()
        self._apply_runner.start()

    def _on_apply_finished(self, rc: int, _output: str) -> None:
        self._applying = False
        cancelled = self._apply_runner is not None and self._apply_runner.was_cancelled
        self.apply_progress.on_finished(rc, _output)
        if cancelled:
            self.apply_progress.status_message("Cancelled")
        elif rc != 0:
            self.apply_progress.log.append_line(f"[update apply exited with code {rc}]")
        if rc == 0 and not cancelled:
            # Applied cleanly: the cached diff list is stale now.
            self._diffs = []
            self.table.setRowCount(0)
            self.check_status.setText("Updates applied - re-check to confirm")
        self.window.set_install_busy(False)
        self._update_button_states()

    def _cancel_apply(self) -> None:
        if self._apply_runner is not None:
            self._apply_runner.cancel()
