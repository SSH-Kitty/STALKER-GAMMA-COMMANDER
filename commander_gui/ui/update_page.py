"""Update page: check for and apply GAMMA updates.

The check is performed GUI-side (see ``commander_gui.updates``) against the
official modpack maker list and the raw GAMMA version marker, so it never hits
the rate-limited GitHub API the bundled CLI depends on. Applying still shells
out to ``update apply``, whose output is surfaced in the progress log.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..cli_runner import cli_command
from ..game_backup import backup_settings_before
from ..integrity import invalidate_baseline
from ..keep_state import capture, discard, restore
from ..modlist import modlist_path_for
from ..parsers import UpdateDiff
from ..settings import cli_ok
from ..updates import (
    UpdateStatus,
    check_updates,
    format_version,
    status_summary,
)
from .common import (
    ACCENT,
    LIGHT_GREY,
    OK_GREEN,
    WARN,
    BackgroundTask,
    CommandRunner,
    ProgressArea,
    free_space_bytes,
    human_size,
    info_label,
    make_card,
    make_header_row,
    mo2_running,
    notify_desktop,
    tr,
)
from .dialogs import MessageBox as QMessageBox
from .multiplayer_page import confirm_singleplayer

_STATUS_COLORS = {
    "Added": OK_GREEN,
    "Modified": ACCENT,  # read at use: follows the active theme
    "Removed": WARN,
}

_STATUS_ICONS = {
    "Added": "+",
    "Modified": "~",
    "Removed": "-",
}

#: Below this, warn before applying an update - GAMMA updates can involve
#: multi-GB re-extraction and there's no advance size estimate the way
#: Proton's installer has one (no Content-Length known up front), so this
#: is a coarse, skippable warning rather than a hard block.
_LOW_DISK_SPACE_WARNING_BYTES = 5 * 1024**3

def _format_relative_time(checked_at: datetime) -> str:
    """Render a UTC datetime as "just now"/"N minute(s) ago"/etc."""
    seconds = (datetime.now(timezone.utc) - checked_at).total_seconds()
    if seconds < 60:
        return tr("just now")
    minutes = int(seconds // 60)
    if minutes < 60:
        return tr("{minutes} minute(s) ago", minutes=minutes)
    hours = minutes // 60
    if hours < 24:
        return tr("{hours} hour(s) ago", hours=hours)
    days = hours // 24
    return tr("{days} day(s) ago", days=days)


class UpdatePage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.setObjectName("updatePage")
        self.window = window
        self._check_task: BackgroundTask | None = None
        self._apply_runner: CommandRunner | None = None
        self._diffs: list[UpdateDiff] = []
        self._checking = False
        self._applying = False
        self._check_generation = 0
        #: In-memory only (not persisted) - the moment the "Last checked"
        #: label refers to; matches the existing ephemeral
        #: _check_generation pattern.
        self._last_checked_at: datetime | None = None

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

        # ---------- status card: version, check, and what changed ----------
        status_card, status_layout = make_card()
        status_layout.setSpacing(10)
        root.addWidget(status_card)

        self.status_pill = QLabel("")
        self.status_pill.setObjectName("verifyPill")
        status_layout.addLayout(make_header_row("GAMMA updates", self.status_pill))

        # The installed version once; status_label names the new version
        # when there is one ("Update available (0.9.4 → 0.9.5)").
        self.version_label = info_label("", wrap=False)
        status_layout.addWidget(self.version_label)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.status_label = info_label(
            tr("Open this page to check the active GAMMA installation."), wrap=False
        )
        status_row.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status_separator = QFrame()
        self.status_separator.setObjectName("installDivider")
        self.status_separator.setFrameShape(QFrame.Shape.NoFrame)
        self.status_separator.setFixedWidth(1)
        self.status_separator.setFixedHeight(14)
        # Shown once a check completes and last_checked_label has text.
        self.status_separator.setVisible(False)
        status_row.addWidget(self.status_separator, 0, Qt.AlignmentFlag.AlignVCenter)
        self.last_checked_label = info_label("", wrap=False)
        self.last_checked_label.setObjectName("dim")
        status_row.addWidget(self.last_checked_label, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addStretch(1)
        status_layout.addLayout(status_row)

        # Addon changes: only shown when a check found some.
        controls_row = QHBoxLayout()
        self.count_summary = QLabel("")
        self.count_summary.setTextFormat(Qt.TextFormat.RichText)
        controls_row.addWidget(self.count_summary)
        controls_row.addStretch(1)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All", "Added", "Modified", "Removed"])
        self.filter_combo.setMinimumWidth(110)
        self.filter_combo.currentTextChanged.connect(self._apply_filter)
        self.filter_combo.setVisible(False)
        controls_row.addWidget(self.filter_combo)
        status_layout.addLayout(controls_row)

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
        self.table.setMinimumHeight(220)
        status_layout.addWidget(self.table)

        self.check_button = QPushButton(tr("Check for updates"))
        # Secondary: Apply below is the one green action on the page.
        self.check_button.setObjectName("secondary")
        self.check_button.setMinimumHeight(36)
        self.check_button.clicked.connect(self._check)
        status_layout.addWidget(self.check_button)

        # ---------- apply card: options, Apply and Undo ----------
        apply_card, apply_layout = make_card()
        root.addWidget(apply_card)
        apply_layout.setSpacing(10)
        apply_layout.addLayout(make_header_row("Install updates"))
        apply_layout.addWidget(
            info_label(
                tr("Downloads the changed mods and unpacks them into GAMMA. Your saves are left alone.")
            )
        )

        options_group = QGroupBox(tr("Update options"))
        # Two columns of two, like the Install page's options.
        options_grid = QGridLayout()
        options_grid.setVerticalSpacing(6)
        options_grid.setHorizontalSpacing(16)
        self.minimal_cb = QCheckBox(tr("Delete downloads after unpacking"))
        self.minimal_cb.setToolTip(
            tr("Saves disk space, but the next update has to download everything it needs again.")
        )
        self.preserve_user_cb = QCheckBox(tr("Keep my game settings (user.ltx)"))
        self.preserve_user_cb.setToolTip(
            tr("Keep your game options (user.ltx). Unchecked resets controls and keybindings.")
        )
        self.preserve_mcm_cb = QCheckBox(tr("Keep my MCM settings"))
        self.preserve_mcm_cb.setToolTip(
            tr("Keep your MCM settings (axr_options.ltx). Unchecked resets them.")
        )
        self.keep_exes_cb = QCheckBox(tr("Keep my current engine executables"))
        self.keep_exes_cb.setToolTip(
            tr("Keep your current Anomaly executables instead of GAMMA's bundled ones.")
        )
        self.preserve_user_cb.setChecked(True)
        self.preserve_mcm_cb.setChecked(True)
        options_grid.addWidget(self.preserve_user_cb, 0, 0)
        options_grid.addWidget(self.preserve_mcm_cb, 1, 0)
        options_grid.addWidget(self.keep_exes_cb, 0, 1)
        options_grid.addWidget(self.minimal_cb, 1, 1)
        options_group.setLayout(options_grid)
        apply_layout.addWidget(options_group)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(12)
        self.apply_button = QPushButton(tr("Apply updates"))
        self.apply_button.setObjectName("primary")
        self.apply_button.setMinimumHeight(36)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        buttons_row.addWidget(self.apply_button, 1)
        self.undo_button = QPushButton(tr("Undo last update"))
        self.undo_button.setObjectName("secondary")
        self.undo_button.setMinimumSize(180, 36)
        # Match Apply's height and font (see themes.py); the row's height,
        # set by Apply, stretches this button.
        self.undo_button.setProperty("besidePrimary", True)
        self.undo_button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.undo_button.setToolTip(
            tr("Restore the modlist.txt saved right before the last update was applied.")
        )
        self.undo_button.clicked.connect(self._undo_last_update)
        self.undo_button.setEnabled(False)
        buttons_row.addWidget(self.undo_button)
        apply_layout.addLayout(buttons_row)

        # show_table=False: a handful of addon updates, not a full install.
        # The bar and Show Console appear only while an update runs (see
        # _set_console_visible()).
        self.apply_progress = ProgressArea(
            show_table=False,
            show_log=True,
            log_max_height=180,
        )
        self.apply_progress.cancel_button.clicked.connect(self._cancel_apply)
        apply_layout.addWidget(self.apply_progress)
        self._set_console_visible(False)

        # Keeps a tall window's spare height out of apply_progress's layout.
        root.addStretch(1)

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
        self.undo_button.setEnabled(idle and self._pre_update_snapshot_path() is not None)

    def _pre_update_snapshot_path(self) -> Path | None:
        """Path to the modlist.txt snapshot taken right before the last

        update, for the active profile - or None if there isn't one (no
        update has been applied through this page yet, or there's no
        active profile).
        """
        profile = self.window.settings.active_profile
        if profile is None:
            return None
        modlist_path = modlist_path_for(profile.gamma, profile.mo2_profile)
        if modlist_path is None:
            return None
        snapshot = modlist_path.with_name(modlist_path.name + ".pre-update.bak")
        return snapshot if snapshot.is_file() else None

    def _snapshot_modlist_before_update(self) -> None:
        """Best-effort: save the current modlist.txt so "Undo Last Update"

        has something to restore. Never blocks applying the update itself
        - a missing/unreadable modlist.txt just means there's nothing to
        snapshot yet (e.g. GAMMA isn't installed at this path at all).
        """
        profile = self.window.settings.active_profile
        if profile is None:
            return
        modlist_path = modlist_path_for(profile.gamma, profile.mo2_profile)
        if modlist_path is None or not modlist_path.is_file():
            return
        try:
            shutil.copy2(
                modlist_path, modlist_path.with_name(modlist_path.name + ".pre-update.bak")
            )
        except OSError:
            pass

    def _undo_last_update(self) -> None:
        if self.window.install_busy or self._checking or self._applying:
            return
        snapshot = self._pre_update_snapshot_path()
        if snapshot is None:
            return
        profile = self.window.settings.active_profile
        modlist_path = modlist_path_for(profile.gamma, profile.mo2_profile)
        if modlist_path is None:
            return
        if mo2_running():
            QMessageBox.warning(
                self,
                tr("Close the game first"),
                tr("Mod Organizer or the game is still open. Close it, then undo the update."),
            )
            return
        answer = QMessageBox.question(
            self,
            tr("Undo the last update?"),
            tr(
                "Your mod list (modlist.txt) goes back to how it was just before the last update."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            note=tr("Changes made to the mod list since then are lost."),
            labels={QMessageBox.StandardButton.Yes: tr("Undo update"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            shutil.copy2(snapshot, modlist_path)
        except OSError as exc:
            QMessageBox.warning(
                self, tr("Couldn't undo the update"), tr("The old mod list couldn't be put back.\n\n{exc}", exc=exc)
            )
            return
        QMessageBox.information(
            self,
            tr("Update undone"),
            tr("Your mod list is back to how it was before the last update."),
        )
        self._update_button_states()

    # ----- status rendering -----
    def _set_pill(self, text: str, level: str) -> None:
        self.status_pill.setText(text)
        self.status_pill.setProperty("level", level)
        self.status_pill.style().unpolish(self.status_pill)
        self.status_pill.style().polish(self.status_pill)
        self.status_pill.setVisible(True)

    def _set_console_visible(self, visible: bool) -> None:
        """The bar and Show Console are only there while an update runs."""
        progress = self.apply_progress
        if not visible and progress.log.isVisible():
            progress._toggle_log()
        progress.log_toggle.setVisible(visible)
        progress.bar.setVisible(visible)
        progress.status_label.setVisible(visible)

    def _set_status(self, text: str, kind: str) -> None:
        self.status_label.setText(text)
        self.status_label.setObjectName(kind)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _render(self, status: UpdateStatus) -> None:
        self.version_label.setText(
            tr(
                "Installed version: {version}",
                version=format_version(status.installed, status.installed_human),
            )
        )
        self.version_label.setVisible(status.installed is not None)
        if self._last_checked_at is None:
            self.status_pill.setVisible(False)
        elif status.installed is None:
            self._set_pill(tr("Not installed"), "attention")
        elif status.update_available:
            self._set_pill(tr("Update available"), "running")
        elif status.error:
            self._set_pill(tr("Check failed"), "failed")
        else:
            self._set_pill(tr("Up to date"), "healthy")

        self.table.setRowCount(0)
        self._diffs = list(status.diffs)
        for diff in self._diffs:
            self._add_diff_row(diff)
        has_diffs = bool(self._diffs)
        self.table.setVisible(has_diffs)
        self.filter_combo.setVisible(has_diffs)

        # Plain text change-count summary, colored per kind (no badge chrome).
        counts = {"Added": 0, "Modified": 0, "Removed": 0}
        for diff in self._diffs:
            if diff.status in counts:
                counts[diff.status] += 1
        parts = [
            f"<span style='color:{_STATUS_COLORS[kind].name()}; font-weight:bold;'>"
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
            QColor(_STATUS_COLORS.get(diff.status, LIGHT_GREY).name())
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
        self._set_pill(tr("Checking..."), "running")
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
            # This result is being thrown away, and refresh() could not start
            # a replacement while this check was still in flight (_check()
            # returns early then) - without re-checking here the page would
            # sit on "Checking ..." with no data until the user either left
            # and came back or pressed the button themselves.
            self._check()
            return
        self._last_checked_at = datetime.now(timezone.utc)
        self.last_checked_label.setText(
            tr("Last checked: {when}", when=_format_relative_time(self._last_checked_at))
        )
        self.status_separator.setVisible(True)
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
            # Same as _on_check_done(): the discarded result must be replaced
            # by a fresh check, or the page stays stuck on "Checking ...".
            self._check()
            return
        self._set_status(tr("Update check failed: {message}", message=message), "warn")
        self._set_pill(tr("Check failed"), "failed")
        self._update_button_states()

    # ----- apply -----
    def _apply(self) -> None:
        if self._applying or self._checking:
            return
        if self.window.install_busy:
            QMessageBox.information(
                self,
                tr("Please wait"),
                tr("Another install is still running. Wait for it to finish, then try again."),
            )
            return
        if not self._diffs:
            QMessageBox.information(self, tr("You're up to date"), tr("There are no GAMMA updates to install right now."))
            return
        answer = QMessageBox.question(
            self,
            tr("Install {arg} update(s)?", arg=len(self._diffs)),
            tr("COMMANDER downloads the updated mods and unpacks them into GAMMA."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            labels={QMessageBox.StandardButton.Yes: tr("Update"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        profile = self.window.settings.active_profile
        if profile is not None:
            free = free_space_bytes(profile.cache or profile.gamma)
            if free is not None and free < _LOW_DISK_SPACE_WARNING_BYTES:
                proceed = QMessageBox.question(
                    self,
                    tr("Low on disk space"),
                    tr(
                        "Only {free} is free. GAMMA updates can need several GB to download and unpack, so the update may fail partway.",
                        free=human_size(free),
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    labels={QMessageBox.StandardButton.Yes: tr("Update anyway"), QMessageBox.StandardButton.No: tr("Cancel")},
                )
                if proceed != QMessageBox.StandardButton.Yes:
                    return

        # Re-checked after the questions above: another page could have
        # started an install (or MO2 been opened) while they were showing.
        if self.window.install_busy or mo2_running(force=True):
            QMessageBox.warning(
                self,
                tr("Please wait"),
                tr("An install is running, or Mod Organizer is open. Wait until it's done, then try again."),
            )
            return

        if not confirm_singleplayer(self, profile, tr("updating GAMMA")):
            return
        self._snapshot_modlist_before_update()
        backup_error = backup_settings_before(profile, "update")
        self._kept = capture(profile, keep_exes=self.keep_exes_cb.isChecked())

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
        if backup_error:
            self.apply_progress.log.append_line(f"Settings backup failed: {backup_error}")
        self._apply_runner = CommandRunner(
            cli_command(args, progress_interval_ms=200), parent=self
        )
        self._apply_runner.line.connect(self.apply_progress.on_line)
        self._apply_runner.finished.connect(self._on_apply_finished)
        self._apply_runner.cancelled.connect(
            lambda: self.apply_progress.log.append_line("[cancelled]")
        )
        self.apply_progress.on_started()
        self._set_console_visible(True)
        self._apply_runner.start()

    def _on_apply_finished(self, rc: int, output: str) -> None:
        self._applying = False
        cancelled = self._apply_runner is not None and self._apply_runner.was_cancelled
        if cancelled:
            # on_finished() would otherwise leave the bar showing "Failed"
            # styling (a cancelled run doesn't pass cli_ok) underneath a
            # label that separately says "Cancelled" - on_cancelled() is
            # the one that actually sets the bar's own format to match.
            self.apply_progress.on_cancelled()
        else:
            self.apply_progress.on_finished(rc, output)
        if not cancelled and not cli_ok(rc, output, ""):
            self.apply_progress.log.append_line("[update apply failed]")
            tail = (output or "").strip().splitlines()
            for line in tail[-25:]:
                self.apply_progress.log.append_line(line)
        kept, self._kept = getattr(self, "_kept", None), None
        if kept is not None and not cancelled and cli_ok(rc, output, ""):
            for note in restore(kept):
                self.apply_progress.log.append_line(note)
        elif kept is not None:
            discard(kept)
        if not cancelled and cli_ok(rc, output, ""):
            # Applied cleanly: the cached diff list is stale now.
            self._diffs = []
            self.table.setRowCount(0)
            self.table.setVisible(False)
            # _render() keeps these widgets consistent; hiding only the
            # table here left the filter box and the pre-update change counts
            # ("12 modified") on screen.
            self.filter_combo.setVisible(False)
            self.count_summary.setVisible(False)
            self._set_status(tr("Updates applied - re-check to confirm"), "dim")
            # An update legitimately changes files under gamma/mods -
            # Verify Integrity's MD5 baseline must not compare against
            # the pre-update state next time, or every updated file
            # would falsely report as corrupted.
            profile = self.window.settings.active_profile
            if profile is not None:
                invalidate_baseline(profile.gamma)
        is_active = getattr(self.window, "isActiveWindow", lambda: True)()
        if not cancelled and not is_active:
            if cli_ok(rc, output, ""):
                notify_desktop(
                    tr("GAMMA update finished"),
                    tr("The GAMMA update was applied successfully."),
                )
            else:
                notify_desktop(
                    tr("GAMMA update failed"),
                    tr(
                        "Update failed with exit code {rc}. Open COMMANDER for details.",
                        rc=rc,
                    ),
                )
        # A failed update keeps its console up so the error can be read.
        self._set_console_visible(not cancelled and not cli_ok(rc, output, ""))
        self.window.set_install_busy(False)
        self._update_button_states()

    def _cancel_apply(self) -> None:
        if self._apply_runner is not None:
            self._apply_runner.cancel()
