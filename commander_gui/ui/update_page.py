"""Update page: check for and apply GAMMA updates.

The check is performed GUI-side (see ``commander_gui.updates``) against the
official modpack maker list and the raw GAMMA version marker, so it never hits
the rate-limited GitHub API the bundled CLI depends on. Applying still shells
out to ``update apply``, whose output is surfaced in the progress log.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
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
    tr,
)

_STATUS_COLORS = {
    "Added": OK_GREEN.name(),
    "Modified": ACCENT.name(),
    "Removed": WARN.name(),
}

_STATUS_ICONS = {
    "Added": "+",
    "Modified": "~",
    "Removed": "-",
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
        self._check_generation = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(content)

        title = section_label(tr("UPDATES"), level=1)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(title)
        subtitle = info_label(
            tr("Check the installed GAMMA version and addon list against the latest available data, then apply any changes.")
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(subtitle)

        # ---------- version status card ----------
        status_card, status_layout = make_card()
        root.addWidget(status_card)
        status_layout.addWidget(section_label(tr("Version status"), level=2))

        version_grid = QGridLayout()
        version_grid.setHorizontalSpacing(16)
        version_grid.setVerticalSpacing(6)
        version_grid.addWidget(info_label(tr("Installed GAMMA version:")), 0, 0)
        self.installed_value = QLabel(tr("-"))
        self.installed_value.setObjectName("mono")
        version_grid.addWidget(self.installed_value, 0, 1)
        version_grid.addWidget(info_label(tr("Latest GAMMA version:")), 1, 0)
        self.latest_value = QLabel(tr("-"))
        self.latest_value.setObjectName("mono")
        version_grid.addWidget(self.latest_value, 1, 1)
        version_grid.setColumnStretch(2, 1)
        status_layout.addLayout(version_grid)

        self.status_label = info_label(
            tr("Open this page to check the active GAMMA installation.")
        )
        status_layout.addWidget(self.status_label)

        check_row = QHBoxLayout()
        self.check_button = QPushButton(tr("Check for updates"))
        self.check_button.setObjectName("primary")
        self.check_button.clicked.connect(self._check)
        check_row.addWidget(self.check_button)
        check_row.addStretch(1)
        status_layout.addLayout(check_row)

        # ---------- updates card ----------
        updates_card, updates_layout = make_card()
        root.addWidget(updates_card)

        updates_header = QHBoxLayout()
        updates_header.addWidget(section_label(tr("Available addon changes")), 1)
        self.count_summary = QLabel("")
        self.count_summary.setTextFormat(Qt.TextFormat.RichText)
        updates_header.addWidget(self.count_summary)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All", "Added", "Modified", "Removed"])
        self.filter_combo.setMinimumWidth(110)
        self.filter_combo.currentTextChanged.connect(self._apply_filter)
        self.filter_combo.setVisible(False)
        updates_header.addWidget(self.filter_combo)
        updates_layout.addLayout(updates_header)

        self.no_updates_label = info_label(tr("No addon changes. GAMMA is up to date."))
        self.no_updates_label.setObjectName("accent")
        updates_layout.addWidget(self.no_updates_label)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["", "Addon", "Archive change"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
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
        apply_layout.addWidget(section_label(tr("Update options")))
        options_row = QHBoxLayout()
        options_row.setSpacing(18)
        self.minimal_cb = QCheckBox(tr("Minimal (delete archives after extraction)"))
        self.preserve_user_cb = QCheckBox(tr("Keep user.ltx settings"))
        self.preserve_user_cb.setToolTip(
            tr("Keep your existing user.ltx (game options) across the update. If unchecked, controls, keybindings and mod-specific settings will be reset.")
        )
        self.preserve_mcm_cb = QCheckBox(tr("Keep MCM settings"))
        self.preserve_mcm_cb.setToolTip(
            tr("Keep your Mod Configuration Menu (MCM) settings across the update. If unchecked, all mod configurations (axr_options.ltx) will be lost.")
        )
        self.preserve_user_cb.setChecked(True)
        self.preserve_mcm_cb.setChecked(True)
        for cb in (self.minimal_cb, self.preserve_user_cb, self.preserve_mcm_cb):
            options_row.addWidget(cb)
        options_row.addStretch(1)
        apply_layout.addLayout(options_row)

        self.apply_button = QPushButton(tr("Apply updates"))
        self.apply_button.setObjectName("hero")
        self.apply_button.setEnabled(False)
        self.apply_button.setMinimumHeight(44)
        self.apply_button.clicked.connect(self._apply)
        apply_layout.addWidget(self.apply_button)

        self.apply_progress = ProgressArea()
        self.apply_progress.cancel_button.clicked.connect(self._cancel_apply)
        apply_layout.addWidget(self.apply_progress)

        self._render(UpdateStatus())
        self._update_button_states()

    def refresh(self) -> None:
        """Called every time the page is shown; auto-check updates."""
        self._check_generation += 1
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
        idle = (
            not self.window.install_busy and not self._checking and not self._applying
        )
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
        self.filter_combo.setVisible(has_diffs)

        # Plain text change-count summary, colored per kind (no badge chrome).
        counts = {"Added": 0, "Modified": 0, "Removed": 0}
        for diff in self._diffs:
            if diff.status in counts:
                counts[diff.status] += 1
        parts = [
            f"<span style='color:{_STATUS_COLORS[kind]}; font-weight:bold;'>"
            f"{count} {kind.lower()}</span>"
            for kind, count in counts.items()
            if count
        ]
        self.count_summary.setText("  ".join(parts))
        self.count_summary.setVisible(has_diffs and bool(parts))
        self._apply_filter()

        text, kind = status_summary(status)
        self._set_status(text, kind)
        self._update_button_states()

    def _apply_filter(self) -> None:
        selected = self.filter_combo.currentText()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            status = item.data(Qt.ItemDataRole.UserRole) if item else ""
            self.table.setRowHidden(
                row, selected != "All" and status != selected
            )

    def _add_diff_row(self, diff: UpdateDiff) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        icon = _STATUS_ICONS.get(diff.status, "?")
        status_item = QTableWidgetItem(f"{icon} {diff.status}")
        status_item.setForeground(
            QColor(_STATUS_COLORS.get(diff.status, LIGHT_GREY.name()))
        )
        status_item.setData(Qt.ItemDataRole.UserRole, diff.status)
        change_item = QTableWidgetItem(diff.detail)
        change_item.setForeground(QColor(LIGHT_GREY.name()))
        if diff.detail_tooltip:
            change_item.setToolTip(diff.detail_tooltip)
        self.table.setItem(row, 0, status_item)
        self.table.setItem(row, 1, QTableWidgetItem(diff.text))
        self.table.setItem(row, 2, change_item)

    # ----- check -----
    def _check(self) -> None:
        if self._checking or self._applying:
            return
        if self.window.install_busy:
            self._set_status(
                tr("An installation is running. The update check is paused."), "warn"
            )
            return
        profile = self.window.settings.active_profile
        if profile is None:
            self._set_status(
                tr("No active profile. Create or activate one on the Profiles page."),
                "warn",
            )
            return
        self._checking = True
        generation = self._check_generation
        profile_id = (
            profile.profile_name,
            profile.anomaly,
            profile.gamma,
            profile.cache,
        )
        self.check_button.setText(tr("Checking..."))
        self._update_button_states()
        self._set_status(
            tr("Checking the active GAMMA installation for updates..."), "dim"
        )
        task = BackgroundTask(check_updates, profile, parent=self)
        task.result.connect(
            lambda status, task=task, generation=generation, profile_id=profile_id: (
                self._on_check_done(status, task, generation, profile_id)
            )
        )
        task.error.connect(
            lambda message, task=task, generation=generation, profile_id=profile_id: (
                self._on_check_error(message, task, generation, profile_id)
            )
        )
        self._check_task = task
        task.start()

    def _on_check_done(
        self, status: UpdateStatus, task: BackgroundTask, generation: int, profile_id
    ) -> None:
        if self._check_task is not task:
            return
        self._check_task = None
        self._checking = False
        self.check_button.setText(tr("Check for updates"))
        current = self.window.settings.active_profile
        if (
            generation != self._check_generation
            or current is None
            or profile_id
            != (current.profile_name, current.anomaly, current.gamma, current.cache)
        ):
            self._update_button_states()
            return
        self._render(status)

    def _on_check_error(
        self, message: str, task: BackgroundTask, generation: int, profile_id
    ) -> None:
        if self._check_task is not task:
            return
        self._check_task = None
        self._checking = False
        self.check_button.setText(tr("Check for updates"))
        if generation != self._check_generation:
            self._update_button_states()
            return
        self._set_status(tr("Update check failed: {message}", message=message), "warn")
        self._update_button_states()

    # ----- apply -----
    def _apply(self) -> None:
        if self._applying or self._checking:
            return
        if self.window.install_busy:
            QMessageBox.information(
                self,
                tr("Busy"),
                tr("An installation is already running. Wait for it to finish."),
            )
            return
        if not self._diffs:
            QMessageBox.information(self, tr("No Updates"), tr("No updates to apply."))
            return
        answer = QMessageBox.question(
            self,
            tr("Confirm Update"),
            tr("Apply {arg} update(s)? This will download and re-extract the updated addons.", arg=len(self._diffs)),
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
        self.window.set_install_busy(True, "gamma")
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
            self._set_status(tr("Updates applied - re-check to confirm"), "dim")
        self.window.set_install_busy(False)
        self._update_button_states()

    def _cancel_apply(self) -> None:
        if self._apply_runner is not None:
            self._apply_runner.cancel()
