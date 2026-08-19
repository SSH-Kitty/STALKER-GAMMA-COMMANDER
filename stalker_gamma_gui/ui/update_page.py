"""Update page: check for and apply GAMMA updates.

The check is performed GUI-side (see ``stalker_gamma_gui.updates``) against the
official modpack maker list and the raw GAMMA version marker, so it never hits
the rate-limited GitHub API the bundled CLI depends on. Applying still shells
out to ``update apply``, whose output is surfaced in the progress log.
"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..cli_runner import cli_command
from ..parsers import UpdateDiff
from ..settings import cli_ok
from ..updates import UpdateStatus, check_updates, format_version, status_summary
from .common import (
    ACCENT,
    LIGHT_GREY,
    OK_GREEN,
    WARN,
    BackgroundTask,
    CommandRunner,
    ProgressArea,
    info_label,
    make_card,
    section_label,
)

_STATUS_COLORS = {
    "Added": OK_GREEN.name(),
    "Modified": ACCENT.name(),
    "Removed": WARN.name(),
}


class UpdatePage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._check_task: BackgroundTask | None = None
        self._apply_runner: CommandRunner | None = None
        self._diffs: list[UpdateDiff] = []
        self._checking = False
        self._applying = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(content)

        # ---------- status card ----------
        card, layout = make_card()
        root.addWidget(card)
        layout.addWidget(section_label("Updates"))

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        grid.addWidget(info_label("Installed version:"), 0, 0)
        self.installed_value = QLabel("-")
        self.installed_value.setObjectName("mono")
        grid.addWidget(self.installed_value, 0, 1)
        grid.addWidget(info_label("Latest version:"), 1, 0)
        self.latest_value = QLabel("-")
        self.latest_value.setObjectName("mono")
        grid.addWidget(self.latest_value, 1, 1)
        grid.setColumnStretch(2, 1)
        layout.addLayout(grid)

        self.status_label = QLabel("Open this page to check for updates.")
        self.status_label.setObjectName("dim")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        row = QHBoxLayout()
        self.check_button = QPushButton("Check for Updates")
        self.check_button.setObjectName("primary")
        self.check_button.clicked.connect(self._check)
        row.addWidget(self.check_button)
        row.addStretch(1)
        layout.addLayout(row)

        # ---------- updates card ----------
        updates_card, updates_layout = make_card()
        root.addWidget(updates_card)
        updates_layout.addWidget(section_label("Available Updates"))
        self.no_updates_label = info_label(
            "No addon changes - GAMMA is up to date."
        )
        self.no_updates_label.setObjectName("accent")
        updates_layout.addWidget(self.no_updates_label)

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
        updates_layout.addWidget(self.table)

        # ---------- apply card ----------
        apply_card, apply_layout = make_card()
        root.addWidget(apply_card)
        apply_layout.addWidget(section_label("Apply Updates"))
        self.minimal_cb = QCheckBox("Minimal (delete archives after extract)")
        self.preserve_user_cb = QCheckBox("Preserve user.ltx settings")
        self.preserve_user_cb.setToolTip(
            "Keep your existing user.ltx (game options) across the update. "
            "If unchecked, controls, keybindings and mod-specific settings will be reset."
        )
        self.preserve_mcm_cb = QCheckBox("Preserve MCM settings")
        self.preserve_mcm_cb.setToolTip(
            "Keep your Mod Configuration Menu (MCM) settings across the update. "
            "If unchecked, all mod configurations (axr_options.ltx) will be lost."
        )
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

        self._render(UpdateStatus())
        self._update_button_states()

    def refresh(self) -> None:
        """Called every time the page is shown; auto-check updates."""
        self.window.refresh_settings()
        self._update_button_states()
        self._check()

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

    # ----- status rendering -----
    def _set_status(self, text: str, kind: str) -> None:
        self.status_label.setText(text)
        self.status_label.setObjectName(kind)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _render(self, status: UpdateStatus) -> None:
        self.installed_value.setText(
            format_version(status.installed, status.installed_human)
        )
        self.latest_value.setText(
            format_version(status.latest, status.latest_human, missing="-")
        )
        self.table.setRowCount(0)
        self._diffs = list(status.diffs)
        for diff in self._diffs:
            self._add_diff_row(diff)
        has_diffs = bool(self._diffs)
        self.no_updates_label.setVisible(not has_diffs)
        self.table.setVisible(has_diffs)

        text, kind = status_summary(status)
        self._set_status(text, kind)
        self._update_button_states()

    def _add_diff_row(self, diff: UpdateDiff) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        status_item = QTableWidgetItem(diff.status)
        status_item.setForeground(QColor(_STATUS_COLORS.get(diff.status, LIGHT_GREY.name())))
        parts = diff.text.split(" -> ")
        name = parts[0].strip()
        change = " -> ".join(p.strip() for p in parts[1:]) if len(parts) > 1 else ""
        self.table.setItem(row, 0, status_item)
        self.table.setItem(row, 1, QTableWidgetItem(name))
        self.table.setItem(row, 2, QTableWidgetItem(change))

    # ----- check -----
    def _check(self) -> None:
        if self._checking or self._applying:
            return
        if self.window.install_busy:
            self._set_status("Install running - update check paused.", "warn")
            return
        profile = self.window.settings.active_profile
        if profile is None:
            self._set_status(
                "No active profile - create or activate one on the Profiles page.",
                "warn",
            )
            return
        self._checking = True
        self.check_button.setText("Checking...")
        self._update_button_states()
        self._set_status("Checking for updates...", "dim")
        task = BackgroundTask(check_updates, profile, parent=self)
        task.result.connect(self._on_check_done)
        task.error.connect(self._on_check_error)
        self._check_task = task
        task.start()

    def _on_check_done(self, status: UpdateStatus) -> None:
        self._checking = False
        self.check_button.setText("Check for Updates")
        self._render(status)

    def _on_check_error(self, message: str) -> None:
        self._checking = False
        self.check_button.setText("Check for Updates")
        self._set_status(f"Update check failed: {message}", "warn")
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

    def _on_apply_finished(self, rc: int, output: str) -> None:
        self._applying = False
        cancelled = self._apply_runner is not None and self._apply_runner.was_cancelled
        self.apply_progress.on_finished(rc, output)
        if cancelled:
            self.apply_progress.status_message("Cancelled")
        elif not cli_ok(rc, output, ""):
            self.apply_progress.log.append_line("[update apply failed]")
            tail = (output or "").strip().splitlines()
            for line in tail[-25:]:
                self.apply_progress.log.append_line(line)
        if not cancelled and cli_ok(rc, output, ""):
            # Applied cleanly: the cached diff list is stale now.
            self._diffs = []
            self.table.setRowCount(0)
            self.no_updates_label.setVisible(True)
            self.table.setVisible(False)
            self._set_status("Updates applied - re-check to confirm", "dim")
        self.window.set_install_busy(False)
        self._update_button_states()

    def _cancel_apply(self) -> None:
        if self._apply_runner is not None:
            self._apply_runner.cancel()
