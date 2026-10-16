"""Update page: check for and apply GAMMA updates.

The check is performed GUI-side (see ``commander_gui.updates``) against the
official modpack maker list and the raw GAMMA version marker, so it never hits
the rate-limited GitHub API the bundled CLI depends on. Applying still shells
out to ``update apply``, whose output is surfaced in the progress log.

Layout follows the Multiplayer page: a status card (version tiles and one
hero button that does the next step), then a tabbed card with the release
notes, the per-addon changes and the update options.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
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
    new_releases,
    parse_patchnotes_sections,
    release_title,
    status_summary,
)
from .common import (
    notice,
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
    install_hover_grow_text,
    make_card,
    mo2_running,
    notify_desktop,
    play_click_sound,
    section_label,
    set_hover_grow_text,
    tr,
)
from .dashboard import _TITLE_ROW_H, _card_title
from .dialogs import MessageBox as QMessageBox
from .multiplayer_page import (
    _Stat,
    _columns,
    _divider,
    _hint,
    _pills,
    _section,
    _tab,
    confirm_singleplayer,
)

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

#: Releases listed under What's new (Patchnotes.md holds the whole history).
_MAX_RELEASES = 8
#: Tallest a release's notes get before they scroll inside their tile.
_NOTES_MAX_HEIGHT = 440
_TABLE_MAX_HEIGHT = 480

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]*", re.MULTILINE)


def _notes_markdown(body: str) -> str:
    """Patch notes for a QLabel: no remote images, headings at text size."""
    return _MD_HEADING.sub("#### ", _MD_IMAGE.sub("", body)).strip()


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


class _Release(QFrame):
    """One release in What's new: title, badge, and notes that fold away."""

    def __init__(self, title: str, body: str, badge: str, *, opened: bool) -> None:
        super().__init__()
        self.setObjectName("statTile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(section_label(release_title(title), level=2))
        if badge:
            chip = QLabel(badge)
            chip.setObjectName("chip")
            chip.setProperty("state", "ok")
            header.addWidget(chip)
        header.addStretch(1)
        self.toggle_button = QPushButton()
        self.toggle_button.setObjectName("pillButton")
        self.toggle_button.setFixedHeight(_TITLE_ROW_H)
        self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_button.clicked.connect(lambda: self.set_open(not self.body.isVisible()))
        header.addWidget(self.toggle_button)
        layout.addLayout(header)
        # A browser, not a label: long notes scroll inside their tile instead
        # of stretching the page, and paragraphs get room between them.
        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        self.body.setStyleSheet("background: transparent; border: none;")
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.body.setMarkdown(_notes_markdown(body) or tr("No patch notes."))
        block = self.body.document().begin()
        while block.isValid():
            fmt = block.blockFormat()
            fmt.setBottomMargin(6)
            QTextCursor(block).setBlockFormat(fmt)
            block = block.next()
        layout.addWidget(self.body)
        self.set_open(opened)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # As tall as the notes, up to a cap; past that they scroll.
        document = self.body.document()
        document.setTextWidth(self.body.viewport().width())
        height = int(document.size().height()) + 8
        self.body.setFixedHeight(min(height, _NOTES_MAX_HEIGHT))

    def set_open(self, opened: bool) -> None:
        self.body.setVisible(opened)
        self.toggle_button.setText(tr("Hide notes") if opened else tr("Show notes"))


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
        self._up_to_date = False
        #: No gamma/version.txt (GOG or hand-made install): one update syncs it.
        self._needs_sync = False
        self._filter = "All"
        self._new_count = 0
        self._check_generation = 0
        #: In-memory only (not persisted) - the moment the "Last checked"
        #: label refers to; matches the existing ephemeral
        #: _check_generation pattern.
        self._last_checked_at: datetime | None = None

        # Same frame as the Multiplayer page.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(16)
        scroll.setWidget(content)

        root.addWidget(self._status_card())
        root.addWidget(self._details_card())
        root.addStretch(1)

        self._render(UpdateStatus())
        self._update_button_states()

    # ---------------------------------------------------------- status card
    def _status_card(self) -> QWidget:
        card, layout = make_card()
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(_card_title(tr("GAMMA updates")))
        header.addStretch(1)
        self.status_pill = QLabel("")
        self.status_pill.setObjectName("verifyPill")
        header.addWidget(self.status_pill)
        self.check_button = QPushButton(tr("Check for updates"))
        self.check_button.setObjectName("pillButton")
        self.check_button.setFixedHeight(_TITLE_ROW_H)
        self.check_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check_button.clicked.connect(self._check)
        header.addWidget(self.check_button)
        layout.addLayout(header)

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
        layout.addLayout(status_row)
        # Why the mod list was rebuilt (GOG/manual install); long, so it wraps.
        self.note_label = info_label("")
        self.note_label.setVisible(False)
        layout.addWidget(self.note_label)

        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.installed_stat = _Stat(tr("Installed version"))
        self.latest_stat = _Stat(tr("Latest version"))
        self.changes_stat = _Stat(tr("Mod changes"))
        self.space_stat = _Stat(tr("Free space"))
        self.space_stat.setToolTip(
            tr("Free space on the drive with your download cache. Updates can need several GB.")
        )
        for tile in (self.installed_stat, self.latest_stat, self.changes_stat, self.space_stat):
            tiles.addWidget(tile, 1)
        layout.addLayout(tiles)

        # The one next step, like Play and Multiplayer: install the updates,
        # or say there's nothing to do.
        self.apply_button = QPushButton(tr("Install updates"))
        self.apply_button.setObjectName("hero")
        install_hover_grow_text(self.apply_button, "hero_text")
        self.apply_button.setEnabled(False)
        # With nothing to install it stays green and checks again instead.
        self.apply_button.clicked.connect(
            lambda: self._apply() if self._diffs or self._needs_sync else self._check()
        )
        self.apply_button.clicked.connect(play_click_sound)
        layout.addWidget(self.apply_button)

        # show_table=False: a handful of addon updates, not a full install.
        # The bar and Show Console appear only while an update runs (see
        # _set_console_visible()).
        self.apply_progress = ProgressArea(
            show_table=False,
            show_log=True,
            log_max_height=180,
        )
        self.apply_progress.cancel_button.clicked.connect(self._cancel_apply)
        layout.addWidget(self.apply_progress)
        self._set_console_visible(False)
        return card

    # --------------------------------------------------------- details card
    def _details_card(self) -> QWidget:
        card, layout = make_card()
        layout.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(4)
        self.tab_group = QButtonGroup(self)
        self._pages: list[QWidget] = []
        body = QVBoxLayout()
        body.setSpacing(0)
        for index, page in enumerate((self._notes_tab(), self._changes_tab(), self._options_tab())):
            button = _tab("", "subTab")
            self.tab_group.addButton(button, index)
            header.addWidget(button)
            body.addWidget(page)
            self._pages.append(page)
        self.tab_group.idClicked.connect(self._show_tab)
        header.addStretch(1)
        tabs = QVBoxLayout()
        tabs.setSpacing(0)
        tabs.addLayout(header)
        tabs.addWidget(_divider())
        layout.addLayout(tabs)
        layout.addLayout(body)
        self._set_tab_titles(0, 0)
        self.tab_group.button(0).setChecked(True)
        self._show_tab(0)
        return card

    def _set_tab_titles(self, new_count: int, change_count: int) -> None:
        notes = tr("What's new")
        changes = tr("Mod changes")
        self.tab_group.button(0).setText(f"{notes} ({new_count})" if new_count else notes)
        self.tab_group.button(1).setText(f"{changes} ({change_count})" if change_count else changes)
        self.tab_group.button(2).setText(tr("Update options"))

    def _show_tab(self, index: int) -> None:
        # Hidden pages take no room, so each tab is exactly as tall as it needs.
        for i, page in enumerate(self._pages):
            page.setVisible(i == index)

    @staticmethod
    def _page() -> tuple[QWidget, QVBoxLayout]:
        widget = QWidget()
        widget.setObjectName("panelTransparent")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        return widget, layout

    def _notes_tab(self) -> QWidget:
        page, layout = self._page()
        layout.addWidget(
            _hint(tr("GAMMA's patch notes, newest first. Releases you haven't installed yet are marked New."))
        )
        self.releases_box = QVBoxLayout()
        self.releases_box.setSpacing(10)
        layout.addLayout(self.releases_box)
        self._releases: list[_Release] = []
        self.notes_empty = _hint(tr("Patch notes show here after the update check."))
        layout.addWidget(self.notes_empty)
        return page

    def _changes_tab(self) -> QWidget:
        page, layout = self._page()
        controls = QHBoxLayout()
        controls.setSpacing(12)
        self._filter_buttons: dict[str, QPushButton] = {}
        for kind, label in (
            ("All", tr("All")),
            ("Added", tr("Added")),
            ("Modified", tr("Modified")),
            ("Removed", tr("Removed")),
        ):
            button = _tab(label)
            button.clicked.connect(lambda _c=False, k=kind: self._set_filter(k))
            self._filter_buttons[kind] = button
        self._filter_buttons["All"].setChecked(True)
        self.filter_row = _pills(*self._filter_buttons.values())
        controls.addWidget(self.filter_row, 1)
        self.count_summary = QLabel("")
        self.count_summary.setTextFormat(Qt.TextFormat.RichText)
        controls.addWidget(self.count_summary)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["", tr("Addon"), tr("Archive change")])
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
        layout.addWidget(self.table)
        self.changes_empty = _hint(
            tr("No mod changes. Your mod list matches the official one.")
        )
        layout.addWidget(self.changes_empty)
        return page

    def _options_tab(self) -> QWidget:
        page, layout = self._page()

        keep, keep_layout = _section(tr("Keep my settings"))
        self.preserve_user_cb = QCheckBox(tr("Keep my game settings (user.ltx)"))
        self.preserve_mcm_cb = QCheckBox(tr("Keep my MCM settings"))
        self.keep_exes_cb = QCheckBox(tr("Keep my current engine executables"))
        self.preserve_user_cb.setChecked(True)
        self.preserve_mcm_cb.setChecked(True)
        for box, hint in (
            (self.preserve_user_cb, tr("Keep your game options (user.ltx). Unchecked resets controls and keybindings.")),
            (self.preserve_mcm_cb, tr("Keep your MCM settings (axr_options.ltx). Unchecked resets them.")),
            (self.keep_exes_cb, tr("Keep your current Anomaly executables instead of GAMMA's bundled ones.")),
        ):
            keep_layout.addWidget(box)
            keep_layout.addWidget(_hint(hint))
        keep_layout.addStretch(1)

        disk, disk_layout = _section(tr("Downloads"))
        self.minimal_cb = QCheckBox(tr("Delete downloads after unpacking"))
        disk_layout.addWidget(self.minimal_cb)
        disk_layout.addWidget(
            _hint(tr("Saves disk space, but the next update has to download everything it needs again."))
        )
        disk_layout.addStretch(1)

        undo, undo_layout = _section(tr("Undo last update"))
        undo_layout.addWidget(
            _hint(tr("Restore the modlist.txt saved right before the last update was applied."))
        )
        self.undo_hint = info_label("")
        undo_layout.addWidget(self.undo_hint)
        undo_layout.addStretch(1)
        self.undo_button = QPushButton(tr("Undo last update"))
        self.undo_button.setObjectName("secondary")
        self.undo_button.setMinimumHeight(34)
        self.undo_button.clicked.connect(self._undo_last_update)
        self.undo_button.setEnabled(False)
        undo_layout.addWidget(self.undo_button)

        _columns(layout, keep, disk, undo)
        layout.addWidget(_hint(tr("Your saves are never touched by an update.")))
        return page

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
        self.apply_button.setEnabled(idle)
        if self._applying:
            text = tr("Installing updates...")
        elif self._checking:
            text = tr("Checking for updates...")
        elif self._diffs:
            text = tr("Install {count} update(s)", count=len(self._diffs))
        elif self._needs_sync:
            text = tr("Install updates")
        elif self._up_to_date:
            text = tr("You're up to date · Check again")
        else:
            text = tr("Check for updates")
        set_hover_grow_text(self.apply_button, text)
        snapshot = self._pre_update_snapshot_path()
        self.undo_button.setEnabled(idle and snapshot is not None)
        if snapshot is None:
            self.undo_hint.setText(tr("Nothing to undo yet."))
        else:
            try:
                saved = datetime.fromtimestamp(snapshot.stat().st_mtime, timezone.utc)
                self.undo_hint.setText(tr("Saved {when}", when=_format_relative_time(saved)))
            except OSError:
                self.undo_hint.setText("")

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
        if not visible and not progress.log.isHidden():
            progress._toggle_log()
        # The whole area too: hidden children still left its margins as a gap.
        progress.setVisible(visible)
        progress.log_toggle.setVisible(visible)
        progress.bar.setVisible(visible)
        progress.status_label.setVisible(visible)

    def _set_status(self, text: str, kind: str) -> None:
        self.status_label.setText(notice(text))
        self.status_label.setObjectName(kind)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _render(self, status: UpdateStatus) -> None:
        checked = self._last_checked_at is not None
        if not checked:
            self.status_pill.setVisible(False)
        elif status.version_unknown:
            self._set_pill(tr("Version unknown"), "attention")
        elif status.installed is None:
            self._set_pill(tr("Not installed"), "attention")
        elif status.update_available:
            self._set_pill(tr("Update available"), "running")
        elif status.error:
            self._set_pill(tr("Check failed"), "failed")
        else:
            self._set_pill(tr("Up to date"), "healthy")
        self._needs_sync = checked and status.version_unknown
        self._up_to_date = checked and status.installed is not None and not (
            status.update_available or status.error
        )

        self.installed_stat.set(
            format_version(status.installed, status.installed_human, "-", show_build=False)
        )
        self.installed_stat.setToolTip(
            tr("Build {build}", build=status.installed) if status.installed else ""
        )
        self.latest_stat.set(
            format_version(status.latest, status.latest_human, "-", show_build=False)
        )
        self.latest_stat.setToolTip(
            tr("Build {build}", build=status.latest) if status.latest else ""
        )
        self._render_space()
        self._render_diffs(list(status.diffs), known=checked and not status.error)
        self._render_releases(status)

        text, kind = status_summary(status)
        self._set_status(text, kind)
        self.note_label.setText(status.note or "")
        self.note_label.setVisible(bool(status.note))
        self._update_button_states()

    def _render_space(self) -> None:
        profile = self.window.settings.active_profile
        free = free_space_bytes(profile.cache or profile.gamma) if profile else None
        low = free is not None and free < _LOW_DISK_SPACE_WARNING_BYTES
        self.space_stat.set(human_size(free) if free is not None else "-")
        self.space_stat.value.setStyleSheet(f"color: {WARN.name()};" if low else "")

    def _render_diffs(self, diffs: list[UpdateDiff], *, known: bool = True) -> None:
        """Table, filter, counts, tile and tab title: always kept in step."""
        self._diffs = diffs
        self.table.setRowCount(0)
        for diff in diffs:
            self._add_diff_row(diff)
        has_diffs = bool(diffs)
        # As tall as its rows, up to a cap; past that it scrolls.
        self.table.setFixedHeight(min(
            self.table.horizontalHeader().sizeHint().height()
            + self.table.verticalHeader().length() + 4,
            _TABLE_MAX_HEIGHT,
        ))
        self.table.setVisible(has_diffs)
        self.filter_row.setVisible(has_diffs)
        self.changes_empty.setVisible(not has_diffs and known)

        # Plain text change-count summary, colored per kind (no badge chrome).
        counts = {"Added": 0, "Modified": 0, "Removed": 0}
        for diff in diffs:
            if diff.status in counts:
                counts[diff.status] += 1
        parts = [
            f"<span style='color:{_STATUS_COLORS[kind].name()}; font-weight:bold;'>"
            f"{_STATUS_ICONS[kind]}{count} {tr(kind).lower()}</span>"
            for kind, count in counts.items()
            if count
        ]
        self.count_summary.setText("&nbsp;&nbsp;".join(parts))
        self.count_summary.setVisible(has_diffs and bool(parts))
        self.changes_stat.set(str(len(diffs)) if known or has_diffs else "-")
        self.changes_stat.value.setStyleSheet(
            f"color: {ACCENT.name()};" if has_diffs else ""
        )
        self._set_tab_titles(self._new_count, len(diffs))
        self._apply_filter()

    def _seen_version(self, status: UpdateStatus) -> str | None:
        """The last GAMMA version this profile was up to date on.

        Saved whenever a check finds the profile current, since the installed
        build number alone can't be matched to a release heading.
        """
        profile = self.window.settings.active_profile
        if profile is None:
            return None
        seen = dict(gui_settings.load_gui_settings().get("gamma_seen_version", {}))
        current = (
            status.latest_human
            if status.installed and status.installed == status.latest
            else None
        )
        if current and seen.get(profile.profile_name) != current:
            seen[profile.profile_name] = current
            gui_settings.save_gui_settings(gamma_seen_version=seen)
        return seen.get(profile.profile_name)

    def _render_releases(self, status: UpdateStatus) -> None:
        notes = status.patchnotes or ""
        if not notes and self._releases:
            return  # A failed fetch keeps the notes already on screen.
        clear_layout(self.releases_box)
        self._releases = []
        sections = parse_patchnotes_sections(notes) if notes else []
        if not sections and notes.strip():
            # Unexpected format: still show it, as one untitled entry.
            sections = [(tr("Patch notes"), notes.strip())]
        sections = sections[:_MAX_RELEASES]
        flags = new_releases(
            [title for title, _body in sections],
            self._seen_version(status),
            status.update_available,
        )
        for index, ((title, body), new) in enumerate(zip(sections, flags)):
            current = index == 0 and not status.update_available and status.installed
            badge = tr("New") if new else tr("Installed") if current else ""
            release = _Release(title, body, badge, opened=new or (index == 0 and not any(flags)))
            self.releases_box.addWidget(release)
            self._releases.append(release)
        self._new_count = sum(flags)
        self.notes_empty.setText(
            tr("Patch notes couldn't be loaded. Check again later.")
            if self._last_checked_at is not None
            else tr("Patch notes show here after the update check.")
        )
        self.notes_empty.setVisible(not sections)
        self._set_tab_titles(self._new_count, len(self._diffs))

    def _set_filter(self, kind: str) -> None:
        self._filter = kind
        self._apply_filter()

    def _apply_filter(self) -> None:
        selected = self._filter
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
        status_item = QTableWidgetItem(f"{icon} {tr(diff.status)}")
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
        if not self._diffs and not self._needs_sync:
            QMessageBox.information(self, tr("You're up to date"), tr("There are no GAMMA updates to install right now."))
            return
        answer = QMessageBox.question(
            self,
            tr("Install {arg} update(s)?", arg=len(self._diffs))
            if self._diffs
            else tr("Install updates?"),
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
            # Applied cleanly: the cached diff list is stale now. Through
            # _render_diffs() so the filter, counts, tile and tab title
            # don't keep showing the pre-update changes.
            self._render_diffs([], known=False)
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
