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

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..cli_runner import cli_command
from ..integrity import invalidate_baseline
from ..modlist import modlist_path_for
from ..parsers import UpdateDiff
from ..settings import cli_ok
from ..updates import (
    UpdateStatus,
    changelog_web_url,
    check_updates,
    format_version,
    parse_patchnotes_sections,
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
    clear_layout,
    free_space_bytes,
    human_size,
    info_label,
    make_card,
    make_header_row,
    mo2_running,
    notify_desktop,
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

#: Below this, warn before applying an update - GAMMA updates can involve
#: multi-GB re-extraction and there's no advance size estimate the way
#: Proton's installer has one (no Content-Length known up front), so this
#: is a coarse, skippable warning rather than a hard block.
_LOW_DISK_SPACE_WARNING_BYTES = 5 * 1024**3

#: Height of one expanded release's own notes body - Patchnotes.md holds
#: the full release history (confirmed against the real file: 7 releases
#: back to 0.9.1), and a single release's own section can run to
#: thousands of words, so each body scrolls within a fixed height rather
#: than growing the card to match whichever entry happens to be open.
_RELEASE_BODY_HEIGHT = 320


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


class _ReleaseNotesSection(QWidget):
    """One collapsible release's notes: a click-to-expand title, then its body.

    Patchnotes.md is the whole GAMMA release history, not just the
    latest one - this renders each release as its own collapsed-by-
    default entry (the caller expands the first/latest one) instead of
    dumping every past release's full notes into view at once.
    """

    def __init__(
        self, title: str, body: str, *, expanded: bool = False, parent=None
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._title = title
        self.toggle_button = QPushButton(self)
        self.toggle_button.setObjectName("releaseNotesToggle")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_button.toggled.connect(self._on_toggled)
        layout.addWidget(self.toggle_button)

        self.body = QTextEdit(self)
        self.body.setReadOnly(True)
        self.body.setMarkdown(body)
        self.body.setFixedHeight(_RELEASE_BODY_HEIGHT)
        self.body.setVisible(expanded)
        layout.addWidget(self.body)

        self._update_toggle_text()

    def _update_toggle_text(self) -> None:
        arrow = "▾" if self.toggle_button.isChecked() else "▸"
        self.toggle_button.setText(f"{arrow}  {self._title}")

    def _on_toggled(self, checked: bool) -> None:
        self.body.setVisible(checked)
        self._update_toggle_text()


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

        # ---------- available addon changes card (top) ----------
        updates_card, updates_layout = make_card()
        root.addWidget(updates_card)

        # Title centered on its own row, rather than pinned to the left
        # edge - the count/filter controls (only shown once there are
        # actual diffs) get their own row below instead of crowding it.
        updates_header = QHBoxLayout()
        updates_header.addStretch(1)
        updates_header.addWidget(section_label(tr("Available addon changes"), level=2))
        updates_header.addStretch(1)
        updates_layout.addLayout(updates_header)

        controls_row = QHBoxLayout()
        controls_row.addStretch(1)
        self.count_summary = QLabel("")
        self.count_summary.setTextFormat(Qt.TextFormat.RichText)
        controls_row.addWidget(self.count_summary)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All", "Added", "Modified", "Removed"])
        self.filter_combo.setMinimumWidth(110)
        self.filter_combo.currentTextChanged.connect(self._apply_filter)
        self.filter_combo.setVisible(False)
        controls_row.addWidget(self.filter_combo)
        updates_layout.addLayout(controls_row)

        self.no_updates_label = info_label(tr("No addon changes. GAMMA is up to date."))
        self.no_updates_label.setObjectName("accent")
        self.no_updates_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
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

        # ---------- version card: Installed | Latest Available ----------
        # Installed hugs the card's left edge, Latest Available hugs its
        # right edge - no vertical divider between them (removed: with
        # the columns this far apart it read as a stray, misaligned mark
        # rather than a meaningful separator).
        version_card, version_layout = make_card()
        root.addWidget(version_card)

        version_grid = QGridLayout()
        version_grid.setHorizontalSpacing(32)
        version_grid.setVerticalSpacing(6)
        version_grid.setColumnStretch(0, 1)
        version_grid.setColumnStretch(1, 1)
        version_layout.addLayout(version_grid)

        version_grid.addLayout(make_header_row("Installed"), 0, 0)
        # Right-aligned mirror of make_header_row(): title flush with the
        # card's right edge, matching the value below it.
        latest_header = QHBoxLayout()
        latest_header.addStretch(1)
        latest_header.addWidget(section_label(tr("Latest Available"), level=2))
        version_grid.addLayout(latest_header, 0, 1)

        # #modCounter (the same style as the topbar's mod-count badge) -
        # bold, accent-colored, no border/background - instead of the
        # previous #mono boxed style, whose bordered box stretched to
        # fill the whole grid column and looked like a long empty bar
        # around a couple of short digits.
        self.installed_value = QLabel(tr("-"))
        self.installed_value.setObjectName("modCounter")
        version_grid.addWidget(
            self.installed_value, 1, 0, alignment=Qt.AlignmentFlag.AlignLeft
        )
        self.latest_value = QLabel(tr("-"))
        self.latest_value.setObjectName("modCounter")
        version_grid.addWidget(
            self.latest_value, 1, 1, alignment=Qt.AlignmentFlag.AlignRight
        )

        # Full-width hero button, same shape as Play page's Launch Game.
        self.check_button = QPushButton(tr("Check for updates"))
        self.check_button.setObjectName("hero")
        self.check_button.clicked.connect(self._check)
        version_layout.addWidget(self.check_button)

        # Status + "Last checked" centered on one line under the button.
        # The separator reuses the exact same #installDivider QFrame as
        # the Installed/Latest column divider above (not a "|" text
        # glyph) so both are the identical color and both sit vertically
        # centered on their own row, instead of a text pipe's off-center,
        # differently-shaded look next to the divider bar.
        status_row = QHBoxLayout()
        status_row.addStretch(1)
        # wrap=False on both labels: word-wrap made either one grow to 2
        # lines depending on available width, which made the row taller
        # than a single line and threw off the fixed-height separator's
        # vertical centering against it. Neither string runs long enough
        # to need wrapping.
        self.status_label = info_label(
            tr("Open this page to check the active GAMMA installation."), wrap=False
        )
        status_row.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status_separator = QFrame()
        self.status_separator.setObjectName("installDivider")
        self.status_separator.setFrameShape(QFrame.Shape.NoFrame)
        self.status_separator.setFixedWidth(1)
        self.status_separator.setFixedHeight(14)
        # Nothing to separate from yet - shown once a check completes and
        # last_checked_label actually has text (see _on_check_done()).
        self.status_separator.setVisible(False)
        status_row.addSpacing(10)
        status_row.addWidget(self.status_separator, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addSpacing(10)
        self.last_checked_label = info_label("", wrap=False)
        self.last_checked_label.setObjectName("dim")
        status_row.addWidget(self.last_checked_label, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addStretch(1)
        version_layout.addLayout(status_row)

        # ---------- apply card ----------
        # Between "Installed" and "What's New" - the options/actions for
        # the version comparison right above, read before the (often
        # long) release notes below.
        apply_card, apply_layout = make_card()
        root.addWidget(apply_card)
        apply_layout.setSpacing(8)
        apply_layout.addWidget(section_label(tr("Update options"), level=2))
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

        # Apply stays a full-width standalone hero button, same shape as
        # Play page's own hero button (its proven pattern) - squeezing it
        # into a shared row with Undo previously fought the QSS's own
        # padding/font-size for vertical space and clipped the text top
        # and bottom.
        self.apply_button = QPushButton(tr("Apply updates"))
        self.apply_button.setObjectName("hero")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        apply_layout.addWidget(self.apply_button)

        # Built before apply_progress so it can be handed in as the
        # console-toggle row's extra widget below - folds Undo onto the
        # same line as "Show Console" instead of a separate row, keeping
        # this card shorter.
        self.undo_button = QPushButton(tr("Undo Last Update"))
        self.undo_button.setObjectName("tertiary")
        self.undo_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_button.setToolTip(
            tr("Restore the modlist.txt saved right before the last update was applied.")
        )
        self.undo_button.clicked.connect(self._undo_last_update)
        self.undo_button.setEnabled(False)

        # show_table=False: the per-addon progress table makes sense for
        # a full GAMMA install (hundreds of mods downloading in
        # parallel), not a handful of addon updates - it otherwise sat
        # permanently visible above the actual (collapsible) console,
        # reading as a second, un-closeable "console" box.
        # auto_expand_log=False: stays collapsed by default, even once
        # an update starts - a plain install-page precedent none of this
        # page's users need open by default the way a full install's
        # live per-addon log does.
        # bar_follows_log=True: the bar (and idle status line) stay
        # hidden until "Show Console" is clicked too, right under this
        # button - an update here is a handful of small downloads, not
        # something that needs an always-visible progress bar the way a
        # full install does.
        self.apply_progress = ProgressArea(
            show_table=False,
            show_log=True,
            log_max_height=180,
            auto_expand_log=False,
            bar_follows_log=True,
            toggle_row_extra=self.undo_button,
        )
        self.apply_progress.cancel_button.clicked.connect(self._cancel_apply)
        apply_layout.addWidget(self.apply_progress)

        # ---------- what's new card ----------
        whats_new_card, whats_new_layout = make_card()
        root.addWidget(whats_new_card)
        whats_new_layout.setSpacing(6)

        whats_new_header = QHBoxLayout()
        whats_new_header.addWidget(section_label(tr("What's New"), level=2), 1)
        self.changelog_link = QPushButton(tr("View full changelog on GitHub"))
        self.changelog_link.setObjectName("githubLink")
        self.changelog_link.setFlat(True)
        self.changelog_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.changelog_link.clicked.connect(self._open_changelog)
        whats_new_header.addWidget(self.changelog_link)
        whats_new_layout.addLayout(whats_new_header)

        # One collapsible _ReleaseNotesSection per release found in
        # Patchnotes.md (see parse_patchnotes_sections()) - rebuilt fresh
        # on every successful check, latest release expanded by default.
        self.whats_new_sections = QWidget()
        self.whats_new_sections_layout = QVBoxLayout(self.whats_new_sections)
        self.whats_new_sections_layout.setContentsMargins(0, 0, 0, 0)
        self.whats_new_sections_layout.setSpacing(4)
        whats_new_layout.addWidget(self.whats_new_sections)

        self.no_patchnotes_label = info_label(
            tr("Patch notes aren't available yet - check for updates first.")
        )
        self.no_patchnotes_label.setObjectName("dim")
        whats_new_layout.addWidget(self.no_patchnotes_label)

        # Without this, a window taller than the page's natural content
        # forces the scroll area's content widget taller too - with
        # nothing else in root claiming that leftover space, it leaked
        # into apply_progress's own internal layout instead (its empty
        # status label quietly stretching to absorb it), pushing "Show
        # Console" far down below a large gap. Same fix Play page already
        # uses for its own last card.
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
                tr("Game Running"),
                tr("Close MO2/the game before restoring a modlist.txt backup."),
            )
            return
        answer = QMessageBox.question(
            self,
            tr("Undo Last Update"),
            tr(
                "Restore modlist.txt to how it was right before the last update was applied? The current modlist.txt will be overwritten."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            shutil.copy2(snapshot, modlist_path)
        except OSError as exc:
            QMessageBox.warning(
                self, tr("Undo Failed"), tr("Could not restore modlist.txt:\n{exc}", exc=exc)
            )
            return
        QMessageBox.information(
            self,
            tr("Undo Complete"),
            tr("modlist.txt restored to its pre-update state."),
        )
        self._update_button_states()

    # ----- status rendering -----
    def _set_status(self, text: str, kind: str) -> None:
        self.status_label.setText(text)
        self.status_label.setObjectName(kind)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _open_changelog(self) -> None:
        profile = self.window.settings.active_profile
        if profile is None:
            return
        QDesktopServices.openUrl(QUrl(changelog_web_url(profile)))

    def _populate_whats_new(self, patchnotes: str | None) -> None:
        """Rebuild the release list from a fresh check's patchnotes text."""
        clear_layout(self.whats_new_sections_layout)
        has_patchnotes = bool(patchnotes)
        self.whats_new_sections.setVisible(has_patchnotes)
        self.no_patchnotes_label.setVisible(not has_patchnotes)
        if not has_patchnotes:
            return
        sections = parse_patchnotes_sections(patchnotes)
        if not sections:
            # No recognizable "# " heading at all (an unexpected format) -
            # still show the raw text rather than nothing.
            sections = [(tr("Latest changes"), patchnotes)]
        for index, (title, body) in enumerate(sections):
            self.whats_new_sections_layout.addWidget(
                _ReleaseNotesSection(title, body, expanded=index == 0)
            )

    def _render(self, status: UpdateStatus) -> None:
        self.installed_value.setText(
            format_version(status.installed, status.installed_human)
        )
        self.latest_value.setText(
            format_version(status.latest, status.latest_human, missing="-")
        )

        self._populate_whats_new(status.patchnotes)

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

        profile = self.window.settings.active_profile
        if profile is not None:
            free = free_space_bytes(profile.cache or profile.gamma)
            if free is not None and free < _LOW_DISK_SPACE_WARNING_BYTES:
                proceed = QMessageBox.question(
                    self,
                    tr("Low Disk Space"),
                    tr(
                        "Only {free} of free space is available. GAMMA updates can need several GB to download and re-extract. Continue anyway?",
                        free=human_size(free),
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if proceed != QMessageBox.StandardButton.Yes:
                    return

        self._snapshot_modlist_before_update()

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
        if not cancelled and cli_ok(rc, output, ""):
            # Applied cleanly: the cached diff list is stale now.
            self._diffs = []
            self.table.setRowCount(0)
            self.no_updates_label.setVisible(True)
            self.table.setVisible(False)
            # _render() keeps these four widgets consistent; hiding only the
            # table here left the filter box and the pre-update change counts
            # ("12 modified") on screen next to "No addon changes."
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
        self.window.set_install_busy(False)
        self._update_button_states()

    def _cancel_apply(self) -> None:
        if self._apply_runner is not None:
            self._apply_runner.cancel()
