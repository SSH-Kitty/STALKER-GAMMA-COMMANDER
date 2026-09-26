"""MO2-style mod organizer for GAMMA profiles.

The mod list is shown grouped by the separator categories GAMMA ships in
``modlist.txt``. Safe operations (install, toggle, delete, reorder within a
category) are done directly on the file with automatic backups.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    Qt,
    QTimer,
    QUrl,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..cli_runner import run_sync
from ..config import logs_dir
from ..fomod import apply_options, parse_config
from ..integrity import _md5_file, invalidate_baseline
from ..launcher import (
    LaunchError,
    build_command,
    ensure_runner_prefix,
    launch_detached,
    resolve_runner,
)
from ..mod_install import (
    ModInstallError,
    default_mod_name,
    extract_archive,
    move_payload,
    sanitize_name,
    write_basic_meta_ini,
)
from ..modlist import (
    BACKUP_SUFFIX,
    _line_info,
    _valid_name,
    add_category,
    add_custom_mod,
    custom_mod_names,
    delete_at,
    delete_category,
    entries,
    find_enabled_mod_file_conflicts,
    grouped,
    install_conflict,
    looks_flipped,
    mod_conflicts,
    move,
    move_category,
    move_mod,
    read_lines,
    rename_category,
    rename_mod,
    reorder_to_original,
    save_lines,
    seed_new_mo2_profile,
    separator_name,
    set_status_at,
    summarize_mod_conflicts,
    timestamped_backup_path,
    unflip,
)
from ..themes import active_theme_tokens
from ..updates import local_modpack_records
from .common import (
    ACCENT,
    ITEM_GREEN,
    STATUS_GREY,
    BackgroundTask,
    ProgressArea,
    StreamTask,
    info_label,
    make_card,
    mo2_running,
    section_label,
    tr,
)
from .fomod_dialog import FomodWizardDialog

#: Short timeout: these are metadata lookups, not installs.
_QUERY_TIMEOUT = 30


#: What Install Mod (and dropping files on the page) accepts.
MOD_ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar", ".fomod")


def dropped_archives(mime) -> list[Path]:
    """Local mod archives among dragged-in files, in the order dropped."""
    if mime is None or not mime.hasUrls():
        return []
    paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]
    return [
        path for path in paths
        if path.suffix.lower() in MOD_ARCHIVE_SUFFIXES and path.is_file()
    ]


class DragTree(QTreeWidget):
    """QTreeWidget subclass with custom drag-and-drop reordering.

    Uses manual mouse-based drag (not ``QDrag::exec()``) so the event
    loop stays open and wheel events continue to work during a drag,
    allowing the user to scroll the list.
    """

    mod_dropped = Signal(str, object, str, bool)
    #: source_category, target_category, before
    category_dropped = Signal(str, str, bool)
    #: Emitted when a drag ends over empty space/an invalid target -
    #: lets the page surface "that drop didn't do anything" feedback
    #: instead of the drag silently vanishing with no explanation.
    drop_cancelled = Signal()
    #: Mod archives dragged in from a file manager - installed, not moved.
    archives_dropped = Signal(list)
    _SCROLL_MARGIN = 40
    _SCROLL_STEP = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_source_name: str | None = None
        #: Set instead of _drag_source_name when dragging a whole
        #: category header (with all its member mods) rather than a
        #: single mod row - the two are mutually exclusive per drag.
        self._drag_source_category: str | None = None
        self._drag_active = False
        self._drag_label: QLabel | None = None
        self._press_x = 0
        self._press_y = 0
        self._scroll_direction = 0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(16)
        self._scroll_timer.timeout.connect(self._auto_scroll)
        self._hover_header_item = None
        self._hover_header_original_bg = None
        self._drop_indicator = QFrame(self.viewport())
        self._drop_indicator.setFrameShape(QFrame.Shape.HLine)
        self._drop_indicator.setLineWidth(2)
        self._drop_indicator.setStyleSheet(f"color: {ACCENT.name()};")
        self._drop_indicator.hide()
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropOverwriteMode(False)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setMouseTracking(True)
        # Pixel-based scrolling, not Qt's default per-item mode - lets
        # wheelEvent() below animate toward a real pixel offset instead of
        # jumping by a row-index delta (see wheelEvent's docstring).
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._scroll_anim_target = None
        self._scroll_anim = QVariantAnimation(self)
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.setDuration(180)
        self._scroll_anim.valueChanged.connect(self._apply_animated_scroll)

    def _find_scroll_area(self):
        """Walk up the widget tree to find the parent QScrollArea."""
        w = self.parent()
        while w is not None:
            if isinstance(w, QScrollArea):
                return w
            w = w.parent()
        return None

    def _auto_scroll(self) -> None:
        """Scroll the parent QScrollArea while a drag is in progress."""
        if self._scroll_direction == 0:
            self._scroll_timer.stop()
            return
        area = self._find_scroll_area()
        if area is None:
            self._scroll_timer.stop()
            return
        sb = area.verticalScrollBar()
        new_val = sb.value() + self._scroll_direction * self._SCROLL_STEP
        sb.setValue(new_val)
        if (self._scroll_direction < 0 and new_val <= sb.minimum()) or (
            self._scroll_direction > 0 and new_val >= sb.maximum()
        ):
            self._scroll_timer.stop()

    def startDrag(self, supported_actions) -> None:
        """No-op: drag is handled manually via mouse events."""

    # Files dragged in from a file manager. The item view would refuse
    # them (it only understands its own rows), so they are taken here.
    def dragEnterEvent(self, event) -> None:
        if dropped_archives(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if dropped_archives(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        archives = dropped_archives(event.mimeData())
        if archives:
            event.acceptProposedAction()
            self.archives_dropped.emit(archives)
            return
        super().dropEvent(event)

    def _reset_drag_state(self) -> None:
        """Clear any in-progress drag."""
        if self._drag_label is not None:
            self._drag_label.deleteLater()
            self._drag_label = None
        self._hide_drop_indicator()
        self._scroll_timer.stop()
        self._scroll_direction = 0
        self._drag_active = False
        self._drag_source_name = None
        self._drag_source_category = None

    def _header_row_index(self, category: str) -> int | None:
        for i in range(self.topLevelItemCount()):
            if self.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == category:
                return i
        return None

    def _hide_drop_indicator(self) -> None:
        if self._drop_indicator is not None and not self._drop_indicator.isHidden():
            self._drop_indicator.hide()
        self._set_hover_header(None)

    def _set_hover_header(self, header) -> None:
        """Highlight the header row a drop would currently land on/in.

        Distinct from the thin drop-line indicator: that shows the exact
        before/after slot, this shows *which category* is the target at
        a glance, independent of hovering a header row directly or one
        of its mod rows.
        """
        if header is getattr(self, "_hover_header_item", None):
            return
        previous = getattr(self, "_hover_header_item", None)
        if previous is not None:
            try:
                previous.setBackground(0, self._hover_header_original_bg)
                previous.setBackground(1, self._hover_header_original_bg)
            except RuntimeError:
                pass  # the item/tree was rebuilt mid-drag
        if header is not None:
            self._hover_header_original_bg = header.background(0)
            highlight = QColor(ACCENT)
            highlight.setAlpha(60)
            header.setBackground(0, highlight)
            header.setBackground(1, highlight)
        self._hover_header_item = header

    def _update_drop_indicator(self, pos) -> None:
        """Show a bar at the slot the dragged item will land in, if any."""
        drop_item = self.itemAt(pos.x(), pos.y())
        if drop_item is None:
            self._hide_drop_indicator()
            return
        if self._drag_source_category is not None:
            # A category can only be dropped relative to another
            # category's header, never inside a specific mod slot.
            header = drop_item if drop_item.parent() is None else drop_item.parent()
            if header.data(0, Qt.ItemDataRole.UserRole) == self._drag_source_category:
                self._hide_drop_indicator()
                return
            item_rect = self.visualItemRect(header)
            before = pos.y() < item_rect.center().y()
            self._set_hover_header(header)
        else:
            item_rect = self.visualItemRect(drop_item)
            before = pos.y() < item_rect.center().y()
            header = drop_item if drop_item.parent() is None else drop_item.parent()
            self._set_hover_header(header)
        self._drop_indicator.setGeometry(
            0,
            item_rect.top() if before else item_rect.bottom() - 1,
            self.viewport().width(),
            2,
        )
        self._drop_indicator.show()
        self._drop_indicator.raise_()

    def mousePressEvent(self, event) -> None:
        """Begin tracking when the user presses on a mod or header item."""
        super().mousePressEvent(event)
        self._drag_source_name = None
        self._drag_source_category = None
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                if item.parent() is not None:
                    self._drag_source_name = item.text(0)
                else:
                    category = item.data(0, Qt.ItemDataRole.UserRole)
                    # "Uncategorized" has no separator line to move by
                    # name - not draggable as a whole category.
                    if category is not None and category != "Uncategorized":
                        self._drag_source_category = category
                if self._drag_source_name is not None or self._drag_source_category is not None:
                    self._drag_active = False
                    self._press_x = event.position().toPoint().x()
                    self._press_y = event.position().toPoint().y()

    def mouseMoveEvent(self, event) -> None:
        """Track the cursor during a manual drag."""
        if self._drag_source_name is not None or self._drag_source_category is not None:
            pos = event.position().toPoint()
            if not self._drag_active:
                dx = abs(pos.x() - self._press_x)
                dy = abs(pos.y() - self._press_y)
                if dx + dy > QApplication.startDragDistance():
                    self._drag_active = True
                    if self._drag_source_category is not None:
                        # Distinct from a single-mod drag label, so it's
                        # clear at a glance an entire category (and all
                        # its mods) is what's being moved.
                        header_index = self._header_row_index(self._drag_source_category)
                        header_item = (
                            self.topLevelItem(header_index) if header_index is not None else None
                        )
                        count = header_item.childCount() if header_item is not None else 0
                        label_text = tr(
                            "{category} ({count} mods)",
                            category=self._drag_source_category,
                            count=count,
                        )
                    else:
                        label_text = self._drag_source_name
                    self._drag_label = QLabel(label_text, self.viewport())
                    self._drag_label.setStyleSheet(
                        f"background: {ACCENT.name()}; color: white; "
                        "padding: 2px 6px; border-radius: 3px; font-weight: bold;"
                    )
                    self._drag_label.adjustSize()
                    self._drag_label.show()
                    self._drag_label.move(pos.x() + 12, pos.y() + 12)
                else:
                    return
            if self._drag_label is not None:
                self._drag_label.move(pos.x() + 12, pos.y() + 12)
            self._update_drop_indicator(pos)
            y = pos.y()
            if y < self._SCROLL_MARGIN:
                self._scroll_direction = -1
            elif y > self.viewport().height() - self._SCROLL_MARGIN:
                self._scroll_direction = 1
            else:
                self._scroll_direction = 0
            if self._scroll_direction != 0 and not self._scroll_timer.isActive():
                self._scroll_timer.start()
            elif self._scroll_direction == 0 and self._scroll_timer.isActive():
                self._scroll_timer.stop()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """Complete the drag by emitting mod_dropped/category_dropped."""
        dragging_category = self._drag_source_category is not None
        if self._drag_active and (self._drag_source_name is not None or dragging_category):
            pos = event.position().toPoint()
            if self._drag_label is not None:
                self._drag_label.deleteLater()
                self._drag_label = None
            self._hide_drop_indicator()
            self._scroll_timer.stop()
            self._scroll_direction = 0
            drop_item = self.itemAt(pos.x(), pos.y())
            source = self._drag_source_name
            source_category = self._drag_source_category
            self._drag_active = False
            self._drag_source_name = None
            self._drag_source_category = None
            if drop_item is None:
                # Released over empty space: treat as a cancelled drag
                # rather than silently re-filing into the last category.
                self.drop_cancelled.emit()
                return
            target_header = (
                drop_item if drop_item.parent() is None else drop_item.parent()
            )
            target_category = target_header.data(0, Qt.ItemDataRole.UserRole)
            if dragging_category:
                if target_category is None or target_category == source_category:
                    self.drop_cancelled.emit()
                    return
                item_rect = self.visualItemRect(target_header)
                before = pos.y() < item_rect.center().y()
                self.category_dropped.emit(source_category, target_category, before)
            else:
                target_name = None if drop_item.parent() is None else drop_item.text(0)
                item_rect = self.visualItemRect(drop_item)
                before = pos.y() < item_rect.center().y()
                self.mod_dropped.emit(source, target_name, target_category, before)
            return
        self._drag_active = False
        self._drag_source_name = None
        self._drag_source_category = None
        super().mouseReleaseEvent(event)

    def _apply_animated_scroll(self, value) -> None:
        if self._scroll_anim_target is not None:
            self._scroll_anim_target.setValue(int(value))

    def wheelEvent(self, event) -> None:
        """Animate wheel scrolling instead of jumping straight to the target.

        The scrollbar is in ScrollPerPixel mode (see __init__), so its
        units are pixels - directly subtracting the wheel's raw
        angleDelta() (~120 per notch) the way the old code did would once
        have jumped ~120 ROWS per notch back when the scrollbar was still
        in Qt's default per-item mode. This computes a normal per-notch
        pixel step the way Qt's own per-pixel wheel handling does
        (wheelScrollLines() lines per notch, scaled by this tree's own row
        height) and animates toward it.
        """
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        sb = self.verticalScrollBar()
        target_sb = sb if sb.maximum() > sb.minimum() else None
        if target_sb is None:
            area = self._find_scroll_area()
            target_sb = area.verticalScrollBar() if area is not None else None
        if target_sb is None:
            event.accept()
            return
        row_height = self.sizeHintForRow(0)
        if row_height <= 0:
            row_height = 24
        pixels_per_notch = QApplication.wheelScrollLines() * row_height
        pixel_delta = (delta / 120.0) * pixels_per_notch
        # Continue from the animation's own in-flight target rather than
        # the live value, so successive fast notches accumulate smoothly
        # instead of restarting from wherever the animation happens to be
        # mid-flight.
        base = (
            self._scroll_anim.endValue()
            if self._scroll_anim.state() == QAbstractAnimation.State.Running
            and self._scroll_anim_target is target_sb
            else target_sb.value()
        )
        new_value = max(
            target_sb.minimum(), min(target_sb.maximum(), round(base - pixel_delta))
        )
        self._scroll_anim.stop()
        self._scroll_anim_target = target_sb
        self._scroll_anim.setStartValue(target_sb.value())
        self._scroll_anim.setEndValue(new_value)
        self._scroll_anim.start()
        event.accept()

    def leaveEvent(self, event) -> None:
        self._reset_drag_state()
        super().leaveEvent(event)

    def focusOutEvent(self, event) -> None:
        self._reset_drag_state()
        super().focusOutEvent(event)


class ModConflictsDialog(QDialog):
    """One mod's conflicts: what it overrides, and what overrides it.

    Two tabs of the other mods involved, each with its shared-file count;
    expanding a mod lists the files. Right-click → Show Conflicts...
    """

    def __init__(
        self,
        name: str,
        overrides: dict[str, list[str]],
        overridden_by: dict[str, list[str]],
        *,
        enabled: bool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Conflicts - {name}", name=name))
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(section_label(name, level=2))
        note = info_label(
            tr(
                "Files this mod shares with other enabled mods. Where two mods "
                "ship the same file, the one lower in the list wins."
            )
            + (
                ""
                if enabled
                else "\n" + tr("This mod is disabled - this is what enabling it would do.")
            )
        )
        note.setObjectName("dim")
        layout.addWidget(note)
        self.tabs = QTabWidget()
        self.overrides_tree = self._tree(overrides)
        self.overridden_tree = self._tree(overridden_by)
        self.tabs.addTab(
            self.overrides_tree, tr("Overrides ({count})", count=len(overrides))
        )
        self.tabs.addTab(
            self.overridden_tree, tr("Overridden by ({count})", count=len(overridden_by))
        )
        if not overrides and overridden_by:
            self.tabs.setCurrentIndex(1)
        layout.addWidget(self.tabs, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _tree(rows: dict[str, list[str]]) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setHeaderLabels([tr("Mod"), tr("Files")])
        tree.setRootIsDecorated(True)
        if not rows:
            QTreeWidgetItem(tree, [tr("None"), ""])
            return tree
        for mod, files in sorted(rows.items(), key=lambda row: (-len(row[1]), row[0])):
            item = QTreeWidgetItem(tree, [mod, str(len(files))])
            for path in files:
                QTreeWidgetItem(item, [path, ""])
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        return tree


class _ConflictsDialog(QDialog):
    """Scrollable, searchable view of summarize_mod_conflicts()'s output.

    Two earlier approaches both proved unreadable on a real GAMMA profile:
    a plain QMessageBox dump put every owner name for one conflicting
    file on a single comma-joined line (a file shared by 100+ mods became
    one giant wall of text), and a later one-row-per-file table just
    moved that same wall of text into thousands of rows - two mods that
    overlap across dozens of gamedata files (an audio overhaul touching
    many scripts, say) showed up as the exact same two mod names
    repeated dozens of times. summarize_mod_conflicts() already collapses
    that down to one row per distinct (winner, overridden) mod pair - but
    even collapsed, a full GAMMA profile still has ~1000 such pairs, and
    almost all of them are GAMMA's own curated, intentional internal
    overrides that a typical user can't act on and doesn't need to see.

    So this defaults to only the pairs touching a mod actually filed
    under "Custom Mods" (see modlist.custom_mod_names()) - the ones the
    user installed themselves, the only conflicts genuinely worth
    checking - with a checkbox to reveal the full GAMMA-internal picture
    for anyone who wants it.
    """

    def __init__(
        self,
        rows: list[tuple[str, str, int]],
        custom_mods: set[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        self._all_rows = rows
        self._custom_mods = custom_mods
        self._custom_rows = [
            row for row in rows if row[0] in custom_mods or row[1] in custom_mods
        ]
        self._showing_all = False

        self.info = QLabel()
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

        self.show_all_checkbox = QCheckBox(
            tr(
                "Show all {total} conflicts (including GAMMA's own)",
                total=len(self._all_rows),
            )
        )
        self.show_all_checkbox.toggled.connect(self._on_show_all_toggled)
        layout.addWidget(self.show_all_checkbox)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Filter by mod name..."))
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            [tr("Mod"), tr("Overrides"), tr("Files")]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table, 1)

        self._total = 0
        self.count_label = QLabel()
        self.count_label.setObjectName("dim")
        layout.addWidget(self.count_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._refresh_view()

    def _on_show_all_toggled(self, checked: bool) -> None:
        self._showing_all = checked
        self.search.clear()
        self._refresh_view()

    def _refresh_view(self) -> None:
        rows = self._all_rows if self._showing_all else self._custom_rows
        if self._showing_all:
            self.setWindowTitle(
                tr("{count} Mod Conflict(s) Found", count=len(self._all_rows))
            )
            self.info.setText(
                tr(
                    "Every mod pair that overrides each other's gamedata files, "
                    "including GAMMA's own mods overriding each other - mostly "
                    "intentional, curated by the pack itself."
                )
            )
        else:
            self.setWindowTitle(
                tr("{count} Mod Conflict(s) Found", count=len(self._custom_rows))
            )
            if not self._custom_mods:
                self.info.setText(
                    tr(
                        "You haven't installed any mods of your own yet (nothing "
                        "is filed under \"Custom Mods\"), so there's nothing here "
                        "to check."
                    )
                )
            elif not self._custom_rows:
                self.info.setText(
                    tr("None of your own installed mods conflict with anything.")
                )
            else:
                self.info.setText(
                    tr(
                        "Conflicts touching a mod you installed yourself (Custom "
                        "Mods) - the ones actually worth checking."
                    )
                )
        self._populate_table(rows)

    def _populate_table(self, rows: list[tuple[str, str, int]]) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for row, (winner, loser, count) in enumerate(rows):
            self.table.setRowHidden(row, False)
            winner_item = QTableWidgetItem(winner)
            loser_item = QTableWidgetItem(loser)
            count_item = QTableWidgetItem()
            count_item.setData(Qt.ItemDataRole.DisplayRole, count)
            self.table.setItem(row, 0, winner_item)
            self.table.setItem(row, 1, loser_item)
            self.table.setItem(row, 2, count_item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(2, Qt.SortOrder.DescendingOrder)
        self._total = len(rows)
        self.count_label.setText(
            tr("Showing {count} of {total}", count=self._total, total=self._total)
        )

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        visible = 0
        for row in range(self.table.rowCount()):
            match = (
                not needle
                or needle in self.table.item(row, 0).text().lower()
                or needle in self.table.item(row, 1).text().lower()
            )
            self.table.setRowHidden(row, not match)
            visible += int(match)
        self.count_label.setText(
            tr("Showing {count} of {total}", count=visible, total=self._total)
        )


#: Longest MO2 profile name the "MO2 uses:" label shows before eliding.
_SELECTED_NAME_MAX = 40


def _query_mo2_profiles() -> tuple[list[str], str]:
    """Return (profile names, selected profile). Runs on a worker thread."""
    rc, out = run_sync(["mo2", "profiles", "list"], timeout=_QUERY_TIMEOUT)
    names = (
        [line.strip() for line in out.splitlines() if line.strip()] if rc == 0 else []
    )
    rc, out = run_sync(
        ["mo2", "config", "get", "selected-profile"], timeout=_QUERY_TIMEOUT
    )
    # The name is the first line: anything after it is stderr run_sync
    # appends, which is never part of a profile name.
    selected = next((line.strip() for line in out.splitlines() if line.strip()), "")
    return names, selected if rc == 0 else ""


class ModManagerPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._lines: list[str] = []
        self._populating = False
        self._reorder_warned = False
        self._profiles_loading = False
        self._profiles_generation = 0
        self._profiles_task = None
        #: Set by New/Rename Profile just before triggering a reload, so
        #: _on_profiles_loaded() selects the profile just created/renamed
        #: instead of falling back to MO2's own selected profile.
        self._pending_profile_select: str | None = None
        #: Mirrors selected_label's text (MO2's own selected_profile, from
        #: ModOrganizer.ini) as a plain name - Rename Profile needs this to
        #: decide whether the renamed profile was the selected one, and
        #: parsing it back out of the label text would be fragile.
        self._mo2_selected_profile: str = ""
        self._install_task: StreamTask | None = None
        self._finalize_task: BackgroundTask | None = None
        self._install_staging: Path | None = None
        self._install_source: Path | None = None
        self._install_name = ""
        self._install_active = False
        self._install_generation = 0
        #: True while the current _start_mod_install() run is a "Reinstall
        #: from Cache" on an already-listed mod, not a brand-new install -
        #: _on_mod_moved() checks this to skip add_custom_mod() (the entry
        #: already exists) and _finish_install() checks it to know whether
        #: _reinstall_backup needs restoring or discarding.
        self._install_is_reinstall = False
        #: (real destination, moved-aside backup of what was there before)
        #: set by _reinstall_mod_from_cache() right before a reinstall
        #: starts; _finish_install() discards the backup on success or
        #: restores it on any failure/cancellation - see that method.
        self._reinstall_backup: tuple[Path, Path] | None = None
        self._pending_refresh = False
        self._load_failed = False
        #: Name of the mod just installed, so the rebuilt tree can scroll to
        #: and select it - new mods land disabled in "Custom Mods" at the
        #: bottom of the list (see add_custom_mod()), so this also confirms
        #: to the user which entry is the one they just installed.
        self._just_installed_name: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(content)

        card, layout = make_card()
        root.addWidget(card, 1)
        # Tighter than make_card()'s shared default (10px) - reclaims a
        # little vertical space for the tree without touching the shared
        # helper other pages' cards also use.
        layout.setSpacing(8)
        # Two rows, then the list. Row 1 is the profile being edited: its
        # picker, a ⋯ menu for New/Rename/Delete, whether MO2 itself opens
        # it (with a one-click fix when it doesn't), and Open MO2. Row 2 is
        # the toolbar for the list below: add content, the backup safety
        # nets folded into one menu, search, and the view controls.
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(section_label(tr("Profile Mods")))
        title_row.addSpacing(16)
        profile_caption = QLabel(tr("MO2 profile"))
        profile_caption.setObjectName("dim")
        title_row.addWidget(profile_caption)
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(220)
        self.profile_combo.setMinimumHeight(32)
        self.profile_combo.currentIndexChanged.connect(self._load_mods)
        self.profile_combo.currentIndexChanged.connect(self._sync_selected_status)
        title_row.addWidget(self.profile_combo)
        self.manage_profile_button = QPushButton("⋯")
        self.manage_profile_button.setObjectName("iconButton")
        self.manage_profile_button.setToolTip(tr("Manage Profile"))
        self.manage_profile_button.setFixedSize(40, 32)
        manage_profile_menu = QMenu(self)
        manage_profile_menu.addAction(tr("New Profile..."), self._create_mo2_profile)
        manage_profile_menu.addAction(tr("Rename Profile..."), self._rename_mo2_profile)
        manage_profile_menu.addAction(tr("Delete Profile..."), self._delete_mo2_profile)
        self.manage_profile_button.setMenu(manage_profile_menu)
        title_row.addWidget(self.manage_profile_button)
        title_row.addSpacing(8)
        # Whether MO2 opens this profile: a green chip when it does; the
        # profile MO2 uses instead plus a button to switch when it doesn't
        # (see _sync_selected_status()).
        self.selected_label = QLabel(tr("MO2 selected profile: -"))
        self.selected_label.setObjectName("chip")
        title_row.addWidget(self.selected_label)
        self.set_selected_button = QPushButton(tr("Use in MO2"))
        self.set_selected_button.setToolTip(tr("Use as MO2 selected profile"))
        self.set_selected_button.clicked.connect(self._set_selected)
        title_row.addWidget(self.set_selected_button)
        title_row.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("chip")
        title_row.addWidget(self.count_label)
        title_row.addSpacing(4)
        self.open_mo2_button = QPushButton(tr("Open MO2"))
        self.open_mo2_button.clicked.connect(self._open_mo2)
        title_row.addWidget(self.open_mo2_button)
        layout.addLayout(title_row)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.install_button = QPushButton("＋  " + tr("Install Mod"))
        self.install_button.setObjectName("primary")
        self.install_button.setToolTip(
            tr("Install a local ZIP, 7Z, RAR, or FOMOD archive into the GAMMA mods folder.")
        )
        self.install_button.clicked.connect(self._install_mod)
        toolbar.addWidget(self.install_button)
        self.new_category_button = QPushButton(tr("New Category"))
        self.new_category_button.setToolTip(
            tr("Add an MO2 separator category to this modlist.")
        )
        self.new_category_button.clicked.connect(self._create_category)
        toolbar.addWidget(self.new_category_button)

        # The three backup actions share one menu: they are occasional
        # safety nets, not everyday controls, and as three buttons they
        # took half the toolbar. Kept as attributes (now QActions) so the
        # edit guard enables and disables them exactly as before.
        self.backups_button = QPushButton(tr("Backups"))
        backups_menu = QMenu(self)
        backups_menu.setToolTipsVisible(True)
        self.create_backup_button = backups_menu.addAction(
            tr("Create Backup"), self._create_backup
        )
        self.create_backup_button.setToolTip(tr("Save a backup of the current MO2 modlist."))
        self.restore_button = backups_menu.addAction(
            tr("Restore Backup"), self._restore_backup
        )
        backups_menu.addSeparator()
        self.restore_original_button = backups_menu.addAction(
            tr("Restore Original Order"), self._restore_original_order
        )
        self.restore_original_button.setToolTip(
            tr("Restore the modlist saved automatically before the first Commander edit.")
        )
        self.backups_button.setMenu(backups_menu)
        toolbar.addWidget(self.backups_button)
        toolbar.addSpacing(12)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search the modlist..."))
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(32)
        self.search.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self.search, 1)

        # Toggles between collapsing every category and expanding them
        # all back - tracked via _all_collapsed rather than the button's
        # own text, so a modlist reload (which always re-expands, see
        # _load_mods()) can reliably reset it back to "Collapse All".
        self._all_collapsed = False
        self.collapse_all_button = QPushButton(tr("Collapse All"))
        self.collapse_all_button.clicked.connect(self._toggle_collapse_all)
        toolbar.addWidget(self.collapse_all_button)
        self.refresh_button = QPushButton("⟳")
        self.refresh_button.setObjectName("iconButton")
        self.refresh_button.setToolTip(tr("Refresh"))
        self.refresh_button.setFixedWidth(40)
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)

        self.guard_label = QLabel(
            tr("MO2 is running. Close it before editing the modlist; edits are disabled while it is open.")
        )
        self.guard_label.setObjectName("warn")
        self.guard_label.hide()
        # While MO2 is open the page is locked; nothing else tells it when
        # MO2 closes, so it looks every couple of seconds - only while the
        # warning is up and the page is on screen.
        self._mo2_watch = QTimer(self)
        self._mo2_watch.setInterval(2000)
        self._mo2_watch.timeout.connect(self._check_mo2_closed)
        layout.addWidget(self.guard_label)

        # A GAMMA load order reversed end to end - what the old Flip
        # Priority button did - crashes the game on startup. Shown only
        # then (see _sync_flip_warning()), with the one-click repair.
        self.flip_warning = QWidget()
        flip_row = QHBoxLayout(self.flip_warning)
        flip_row.setContentsMargins(0, 0, 0, 0)
        flip_label = QLabel(
            tr("This load order is reversed - GAMMA will likely crash on startup.")
        )
        flip_label.setObjectName("warn")
        flip_label.setWordWrap(True)
        flip_row.addWidget(flip_label, 1)
        self.fix_flip_button = QPushButton(tr("Fix load order"))
        self.fix_flip_button.setObjectName("primary")
        self.fix_flip_button.clicked.connect(self._fix_flipped_order)
        flip_row.addWidget(self.fix_flip_button)
        self.flip_warning.hide()
        layout.addWidget(self.flip_warning)

        self.tree = DragTree(self)
        self.tree.setColumnCount(1)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(18)
        # Taller now that the page-level title/subtitle above are gone -
        # reclaims that freed vertical space for the modlist itself.
        self.tree.setMinimumHeight(680)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemSelectionChanged.connect(self._update_count)
        self.tree.mod_dropped.connect(self._on_tree_drop)
        self.tree.category_dropped.connect(self._on_category_drop)
        self.tree.drop_cancelled.connect(self._on_drop_cancelled)
        self.tree.archives_dropped.connect(self._install_dropped)
        # The rest of the page takes dropped archives too, not only the list.
        self.setAcceptDrops(True)
        self._drop_queue: list[Path] = []
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.tree, 1)

        # Enable/Disable/Delete/Move Up/Move Down used to live here too,
        # duplicating what right-clicking a mod already offers - dropped
        # to declutter; the tree's own context menu and drag-and-drop are
        # the one place to do those now. What's left here acts on the
        # whole list, not a selection.
        btn_row = QHBoxLayout()
        list_tools_label = QLabel(tr("List tools:"))
        list_tools_label.setObjectName("dim")
        btn_row.addWidget(list_tools_label)
        self.conflicts_button = QPushButton(tr("Check for File Conflicts"))
        self.conflicts_button.setToolTip(
            tr("Scan every currently-enabled mod's files for ones that appear in more than one mod - not full conflict resolution, just what the current load order is overriding.")
        )
        self.conflicts_button.clicked.connect(self._check_file_conflicts)
        btn_row.addWidget(self.conflicts_button)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.install_progress = ProgressArea(show_table=False, show_log=True)
        self.install_progress.cancel_button.clicked.connect(self._cancel_install)
        self.install_progress.hide()
        layout.addWidget(self.install_progress)

        backup_row = QHBoxLayout()
        self.backup_status = QLabel("")
        self.backup_status.setObjectName("dim")
        backup_row.addWidget(self.backup_status, 1)
        layout.addLayout(backup_row)

    # ----- data access -----
    def _active_profile(self):
        profile = self.window.settings.active_profile
        if profile is None:
            raise RuntimeError("No active profile")
        return profile

    def _modlist_path(self, mo2_profile: str) -> Path:
        if (
            not mo2_profile
            or mo2_profile in {".", ".."}
            or "/" in mo2_profile
            or "\\" in mo2_profile
        ):
            raise RuntimeError("Invalid MO2 profile name")
        gamma = self._active_profile().gamma
        profiles_root = (Path(gamma) / "profiles").resolve()
        path = profiles_root / mo2_profile / "modlist.txt"
        try:
            path.parent.resolve().relative_to(profiles_root)
        except ValueError as exc:
            raise RuntimeError("MO2 profile path escapes the profiles directory") from exc
        return path

    def _backup_path(self, modlist: Path) -> Path:
        return modlist.with_name(modlist.name + BACKUP_SUFFIX)

    def _timestamped_backup_path(self, modlist: Path) -> Path:
        return timestamped_backup_path(modlist)

    _MAX_BACKUPS = 20

    def _prune_backups(self, modlist: Path) -> None:
        """Keep only this modlist's newest timestamped backups."""
        baks = sorted(
            (
                p
                for p in modlist.parent.glob(f"{modlist.stem}-*.bak")
                if p.is_file()
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in baks[self._MAX_BACKUPS :]:
            try:
                old.unlink()
            except OSError:
                pass

    # ----- MO2 running guard -----
    def _mo2_running(self) -> bool:
        # Every caller here gates a modlist.txt write or backup/restore
        # against MO2 rewriting the same file - a stale cached "not running"
        # answer from mo2_running()'s TTL cache is exactly the race this
        # guard exists to prevent, so always check fresh.
        return mo2_running(force=True)

    def _check_mo2_closed(self) -> None:
        if not self.isVisible():
            self._mo2_watch.stop()
            return
        if mo2_running():
            return
        self._mo2_watch.stop()
        # MO2 rewrites modlist.txt as it exits: reload, which also unlocks
        # the page through _update_guard().
        self._load_mods()
        self._update_guard()

    def hideEvent(self, event) -> None:
        self._mo2_watch.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.guard_label.isVisible() or self.guard_label.isVisibleTo(self):
            self._mo2_watch.start()

    def _update_guard(self) -> None:
        running = self._mo2_running()
        blocked = (
            running
            or self._install_active
            or self.window.install_busy
            or self._load_failed
        )
        self.guard_label.setVisible(running)
        watch = getattr(self, "_mo2_watch", None)
        if watch is not None:
            if running and self.isVisible():
                watch.start()
            elif not running:
                watch.stop()
        for widget in (
            self.tree,
            self.profile_combo,
            self.restore_button,
            self.restore_original_button,
            self.create_backup_button,
            self.install_button,
            self.new_category_button,
            self.set_selected_button,
        ):
            widget.setEnabled(not blocked)
        if self._install_active:
            self.install_button.setEnabled(False)
            self.profile_combo.setEnabled(False)
        if running:
            self._update_count()
        original_backup = False
        profile = self.profile_combo.currentText()
        if profile:
            try:
                original_backup = self._backup_path(self._modlist_path(profile)).is_file()
            except (OSError, RuntimeError):
                pass
        self.restore_original_button.setEnabled(not blocked and original_backup)
        self._sync_flip_warning(blocked)

    def _sync_flip_warning(self, blocked: bool) -> None:
        warning = getattr(self, "flip_warning", None)
        if warning is None:
            return
        warning.setVisible(looks_flipped(getattr(self, "_lines", [])))
        self.fix_flip_button.setEnabled(not blocked)

    def _fix_flipped_order(self) -> None:
        """Put a reversed GAMMA load order back the right way round."""
        if not looks_flipped(self._lines):
            self._update_guard()
            return
        answer = QMessageBox.question(
            self,
            tr("Fix load order"),
            tr(
                "Put the load order back the right way round? A backup of the "
                "current modlist is saved first (Backups ▾ can restore it)."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._write_lines(unflip(self._lines)):
            self._load_mods()
            self.window.statusBar().showMessage(tr("Load order fixed."), 8000)

    def on_busy_changed(self, _busy: bool) -> None:
        """Keep modlist mutations locked during global install operations."""
        self._update_guard()

    # ----- load -----
    def refresh(self) -> None:
        self._profiles_generation += 1
        self.window.refresh_settings()
        if self._profiles_loading:
            self._pending_refresh = True
            return
        self._load_profiles(self._profiles_generation)
        self._update_guard()

    def _load_profiles(self, generation: int) -> None:
        """Query MO2 profiles off the GUI thread.

        Both queries shell out to the CLI; running them inline froze the window
        on every visit to this page.
        """
        if self._profiles_loading:
            return
        self._profiles_loading = True
        self.count_label.setText(tr("Loading MO2 profiles..."))
        task = BackgroundTask(_query_mo2_profiles, parent=self)
        task.result.connect(
            lambda result, task=task, generation=generation: self._on_profiles_loaded(
                result, task, generation
            )
        )
        task.error.connect(
            lambda message, task=task, generation=generation: self._on_profiles_error(
                message, task, generation
            )
        )
        self._profiles_task = task
        task.start()

    def _on_profiles_loaded(
        self, result: tuple[list[str], str], task: BackgroundTask, generation: int
    ) -> None:
        if self._profiles_task is not task:
            return
        self._profiles_loading = False
        self._profiles_task = None
        if generation != self._profiles_generation:
            return
        names, selected = result
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        if names:
            self.profile_combo.addItems(names)
            active = self.window.settings.active_profile
            # Prefer MO2's own currently-selected profile over the
            # CliProfile's configured mo2_profile field: a user who creates
            # or switches to a profile directly in MO2 (e.g. a custom "Solo
            # Profile") without also updating it on the Profiles page would
            # otherwise have every Mod Manager read/write silently target a
            # stale profile's modlist.txt - not the one MO2 (and the game)
            # actually uses. Case-insensitive matching either way: settings
            # .json's mo2_profile and the CLI's on-disk folder name can
            # differ only in case (same pattern as common.py's
            # _find_profile_dir). An exact-match miss here would silently
            # leave the combo on whichever profile the CLI listed first
            # (e.g. MO2's own default "Default" profile).
            for candidate in (selected, active.mo2_profile if active else None):
                if not candidate:
                    continue
                wanted = candidate.upper()
                match = next((n for n in names if n.upper() == wanted), None)
                if match is not None:
                    self.profile_combo.setCurrentText(match)
                    break
            # New/Rename Profile requested a specific selection - it wins
            # over the "follow MO2's own selection" logic above, since
            # neither creating nor renaming a profile changes MO2's own
            # selected_profile. getattr-guarded: some tests build this
            # page via __new__() and stub only the attributes their own
            # scenario touches.
            pending_select = getattr(self, "_pending_profile_select", None)
            if pending_select in names:
                self.profile_combo.setCurrentText(pending_select)
        self._pending_profile_select = None
        self.profile_combo.blockSignals(False)
        self._mo2_selected_profile = selected or ""
        self._sync_selected_status()
        if not names:
            self.count_label.setText(
                tr("No MO2 profiles found. Complete a GAMMA installation first.")
            )
            self.tree.clear()
            return
        self._load_mods()
        if self._pending_refresh:
            self._pending_refresh = False
            self.refresh()

    def _on_profiles_error(
        self, message: str, task: BackgroundTask, generation: int
    ) -> None:
        if self._profiles_task is not task:
            return
        self._profiles_loading = False
        self._profiles_task = None
        if generation != self._profiles_generation:
            return
        self._mo2_selected_profile = ""
        self._sync_selected_status()
        self.count_label.setText(tr("Could not list MO2 profiles: {message}", message=message))
        self.tree.clear()
        if self._pending_refresh:
            self._pending_refresh = False
            self.refresh()

    def _load_mods(self) -> None:
        mo2_profile = self.profile_combo.currentText()
        if not mo2_profile:
            return
        try:
            path = self._modlist_path(mo2_profile)
            self._lines = read_lines(path)
            self.backup_status.setText(self._backup_status_text(path))
            self._restore_missing_user_categories()
            self._populate_tree()
            self._update_count()
        except Exception as exc:  # noqa: BLE001
            # Covers the whole pipeline, not just read_lines: a single
            # malformed +/- line raises ValueError out of grouped()/entries()
            # during _populate_tree(), and that must degrade to the same
            # "could not read" state instead of crashing the page.
            self._lines = []
            self.count_label.setText(tr("Could not read modlist: {exc}", exc=exc))
            self.tree.clear()
            # A failed parse must not look like an empty modlist -- writing
            # here would overwrite the user's real modlist with nothing.
            self._load_failed = True
            self._update_guard()
            return
        self._load_failed = False
        # Re-run the guard with the fresh list: it re-enables the edit
        # controls a failed earlier load had locked, and shows or hides the
        # reversed-load-order warning for the list just read.
        self._update_guard()

    def _backup_status_text(self, modlist: Path) -> str:
        bak = self._backup_path(modlist)
        if bak.is_file():
            # Shown to the user, so render in their local timezone explicitly.
            stamp = (
                datetime.fromtimestamp(bak.stat().st_mtime, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
            )
            return f"Backup: {bak.name} ({stamp})"
        return "No backup yet"

    def _create_backup(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            QMessageBox.warning(
                self,
                tr("Mod Organizer is running"),
                tr("Close Mod Organizer before creating a backup."),
            )
            return
        profile = self.profile_combo.currentText()
        if not profile:
            return
        try:
            modlist = self._modlist_path(profile)
            if not modlist.is_file():
                raise FileNotFoundError(f"Modlist not found: {modlist}")
            default_path = self._timestamped_backup_path(modlist)
            backup_name, _ = QFileDialog.getSaveFileName(
                self,
                "Save modlist backup",
                str(default_path),
                "Modlist backups (*.bak *.txt);;All files (*)",
            )
            if not backup_name:
                return
            backup = Path(backup_name).expanduser()
            if backup.exists():
                QMessageBox.warning(
                    self,
                    tr("Backup Already Exists"),
                    tr("Choose a new timestamped filename so existing backups are not overwritten."),
                )
                return
            if self.window.install_busy or self._mo2_running():
                self._update_guard()
                return
            backup.parent.mkdir(parents=True, exist_ok=True)
            temporary = backup.with_name(f".{backup.name}.tmp")
            try:
                shutil.copy2(modlist, temporary)
                temporary.replace(backup)
            except OSError:
                # Never leave a stray .tmp behind when the replace fails.
                temporary.unlink(missing_ok=True)
                raise
            self.backup_status.setText(tr("Backup saved: {backup}", backup=backup))
            self._update_guard()
        except (OSError, RuntimeError) as exc:
            # RuntimeError: _modlist_path()/_active_profile() raise it when
            # the active profile is gone (e.g. deleted on the Profiles page
            # while this combo still lists its MO2 profiles) - the same
            # cases every other action here already reports instead of
            # letting the exception escape the slot.
            QMessageBox.warning(self, tr("Backup Failed"), str(exc))

    def _populate_tree(self) -> None:
        self._populating = True
        self.tree.blockSignals(True)
        self.tree.clear()
        priority = 0
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Mod", "Priority"])
        self.tree.header().setVisible(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        # MO2 writes modlist.txt with file-top as the HIGHEST-priority mod
        # (rendered at the BOTTOM of MO2's own list) and file-bottom as the
        # lowest-priority mod (rendered at the TOP) - confirmed directly
        # against Mod Organizer 2's own source (profile.cpp: "the priority
        # are reversed ... since the mod list is written in reverse
        # order"). Walking grouped()'s file-order output in reverse here is
        # what makes this tree match MO2's actual on-screen order, category
        # order and mod order alike, and also makes the incrementing
        # `priority` counter below come out matching MO2's own numbering
        # (lowest at screen-top) with no separate formula needed.
        card_bg = QColor(active_theme_tokens()["card"])
        for category, mods in reversed(grouped(self._lines)):
            header = QTreeWidgetItem([f"{category} ({len(mods)})"])
            # The display text carries a "(N)" mod-count suffix - drop
            # handling needs the raw category name to match against
            # modlist.py's separator-derived labels, so keep it out of band
            # rather than re-parsing the display text back off of it.
            header.setData(0, Qt.ItemDataRole.UserRole, category)
            header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            header.setForeground(0, QColor(ACCENT.name()))
            font = header.font(0)
            font.setBold(True)
            header.setFont(0, font)
            # Same background token QHeaderView::section already uses (see
            # themes.py), so the column header and category rows read as
            # one consistent "header" band when scanning a long list.
            header.setBackground(0, card_bg)
            header.setBackground(1, card_bg)
            self.tree.addTopLevelItem(header)
            for status, name, line_index in reversed(mods):
                priority += 1
                item = QTreeWidgetItem([name])
                item.setText(1, str(priority))
                item.setToolTip(1, f"MO2 priority {priority}")
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsDragEnabled
                )
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if status == "Enabled"
                    else Qt.CheckState.Unchecked,
                )
                item.setData(0, Qt.ItemDataRole.UserRole, line_index)
                item.setForeground(
                    0,
                    QColor(STATUS_GREY.name())
                    if status != "Enabled"
                    else QColor(ITEM_GREEN.name()),
                )
                header.addChild(item)
        self.tree.expandAll()
        self._all_collapsed = False
        # getattr-guarded: some tests build a ModManagerPage via __new__()
        # (bypassing __init__) to call _populate_tree() directly against a
        # hand-built self.tree, without the rest of the real page's widgets.
        collapse_all_button = getattr(self, "collapse_all_button", None)
        if collapse_all_button is not None:
            collapse_all_button.setText(tr("Collapse All"))
        self.tree.blockSignals(False)
        self._populating = False
        self._apply_filter()

    def _toggle_collapse_all(self) -> None:
        if self._all_collapsed:
            self.tree.expandAll()
            self.collapse_all_button.setText(tr("Collapse All"))
        else:
            self.tree.collapseAll()
            self.collapse_all_button.setText(tr("Expand All"))
        self._all_collapsed = not self._all_collapsed

    # ----- search filter -----
    def _apply_filter(self) -> None:
        needle = self.search.text().strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            matches = 0
            for j in range(header.childCount()):
                item = header.child(j)
                hit = not needle or needle in item.text(0).lower()
                item.setHidden(not hit)
                matches += int(hit)
            header.setHidden(matches == 0 and bool(needle))
        self._update_count()

    def _update_count(self) -> None:
        total = enabled = visible = 0
        visible_enabled = 0
        # Dedup by name: GAMMA's own official modlist.txt has been
        # confirmed to list at least one mod twice (e.g. "G.A.M.M.A.
        # Vehicles in Darkscape") - there's only one real mod/folder for
        # it, and MO2 counts a name once regardless of how many times it
        # appears, so this must too or it overcounts relative to MO2's
        # own (and the community's) count. See modlist.py's count_mods().
        seen: set[str] = set()
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                item = header.child(j)
                name = item.text(0)
                if name in seen:
                    continue
                seen.add(name)
                total += 1
                if item.checkState(0) == Qt.CheckState.Checked:
                    enabled += 1
                if not item.isHidden():
                    visible += 1
                    visible_enabled += int(item.checkState(0) == Qt.CheckState.Checked)
        if self.search.text().strip():
            self.count_label.setText(
                tr("{visible} matching of {total} mods ({visible_enabled} enabled)", visible=visible, total=total, visible_enabled=visible_enabled)
            )
        else:
            self.count_label.setText(tr("{total} mods ({enabled} enabled)", total=total, enabled=enabled))
        self.window.update_mod_counter()

    # ----- writes -----
    def _write_lines(
        self,
        new_lines: list[str],
        *,
        internal: bool = False,
        quiet: bool = False,
        snapshot: bool = True,
    ) -> bool:
        guard_failed = (
            (self.window.install_busy and not internal)
            or (self._install_active and not internal)
            or self._mo2_running()
        )
        if guard_failed:
            self._update_guard()
            if not quiet:
                QMessageBox.warning(
                    self,
                    tr("Mod Organizer is running"),
                    tr("Close Mod Organizer first - it would overwrite your changes when it exits."),
                )
            return False
        mo2_profile = self.profile_combo.currentText()
        try:
            path = self._modlist_path(mo2_profile)
            if path.exists():
                bak = self._backup_path(path)
                if not bak.exists():
                    shutil.copy2(path, bak)
                if snapshot:
                    shutil.copy2(path, self._timestamped_backup_path(path))
            save_lines(path, new_lines)
            self._lines = new_lines
            self.backup_status.setText(self._backup_status_text(path))
            self._prune_backups(path)
            self._update_guard()
            return True
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                QMessageBox.warning(self, tr("Failed"), str(exc))
            return False

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._populating or item.parent() is None:
            return
        if self.window.install_busy:
            self._update_guard()
            return
        line_index = item.data(0, Qt.ItemDataRole.UserRole)
        if line_index is None or line_index >= len(self._lines):
            return
        enabled = item.checkState(0) == Qt.CheckState.Checked
        new_lines = set_status_at(self._lines, line_index, enabled)
        if self._write_lines(new_lines, snapshot=False):
            self._populating = True
            item.setForeground(
                0,
                QColor(ITEM_GREEN.name()) if enabled else QColor(STATUS_GREY.name()),
            )
            self._populating = False
            self._update_count()
            return
        # The write failed, so put the checkbox back rather than showing a
        # state the file on disk does not have.
        self._populating = True
        self.tree.blockSignals(True)
        item.setCheckState(
            0, Qt.CheckState.Unchecked if enabled else Qt.CheckState.Checked
        )
        self.tree.blockSignals(False)
        self._populating = False

    # ----- actions -----
    def _install_mod(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            QMessageBox.warning(
                self,
                tr("Mod Organizer is running"),
                tr("Close Mod Organizer before installing a mod."),
            )
            return
        archive_name, _ = QFileDialog.getOpenFileName(
            self,
            tr("Install mod archive"),
            str(Path.home()),
            tr("Mod archives") + " (*.zip *.7z *.rar *.fomod);;" + tr("All files") + " (*)",
        )
        if not archive_name:
            return
        self._install_archive(Path(archive_name))

    # ----- drag and drop -----
    def dragEnterEvent(self, event) -> None:
        if dropped_archives(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if dropped_archives(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        archives = dropped_archives(event.mimeData())
        if archives:
            event.acceptProposedAction()
            self._install_dropped(archives)
            return
        super().dropEvent(event)

    def _install_dropped(self, archives: list[Path]) -> None:
        """Install archives dropped on the page, one after another.

        Each still gets the name prompt (and its FOMOD installer); the next
        starts once the previous install has finished.
        """
        if self.window.install_busy or self._install_active or self._mo2_running():
            self._update_guard()
            QMessageBox.warning(
                self,
                tr("Cannot install now"),
                tr("Close Mod Organizer and wait for any running install to finish, then drop the files again."),
            )
            return
        self._drop_queue = list(archives)
        self._install_next_dropped()

    def _install_next_dropped(self) -> None:
        # An archive whose install never started (name prompt cancelled,
        # already installed, ...) must not stall the rest of the drop.
        while self._drop_queue and not self._install_active:
            self._install_archive(self._drop_queue.pop(0))

    def _install_archive(self, archive: Path) -> None:
        """Name, check and install one archive (picked or dropped)."""
        try:
            default_name = default_mod_name(archive)
        except ModInstallError as exc:
            QMessageBox.warning(self, tr("Invalid mod archive"), str(exc))
            return
        # Use a custom dialog for better text wrapping
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("Name installed mod"))
        dialog.resize(520, 130)
        layout = QVBoxLayout(dialog)
        label = QLabel(tr("MO2 mod name:"))
        label.setWordWrap(True)
        layout.addWidget(label)
        text_field = QLineEdit(default_name)
        text_field.setMinimumWidth(300)
        text_field.selectAll()
        layout.addWidget(text_field)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = text_field.text()
        if not name.strip():
            return
        try:
            safe_name = sanitize_name(name)
        except ModInstallError as exc:
            QMessageBox.warning(self, tr("Invalid mod name"), str(exc))
            return
        if not self._resolve_install_target(safe_name):
            return
        self._start_mod_install(archive, safe_name)

    def _resolve_install_target(self, name: str) -> bool:
        """Check the install destination, replacing a leftover folder.

        Returns ``True`` to proceed.  A folder already in use by a listed mod
        blocks the install (enable it instead); a leftover folder from a
        deleted list entry is removed after confirmation.
        """
        try:
            profile = self._active_profile()
        except RuntimeError as exc:
            QMessageBox.warning(self, tr("Error"), str(exc))
            return False
        mods_dir = Path(profile.gamma) / "mods"
        if mods_dir.is_symlink():
            QMessageBox.warning(
                self,
                tr("Cannot install"),
                tr("The GAMMA mods directory cannot be a symlink."),
            )
            return False
        conflict = install_conflict(self._lines, mods_dir, name)
        if conflict is None:
            return True
        if conflict == "listed":
            QMessageBox.warning(
                self,
                tr("Mod already installed"),
                tr("'{name}' is already in the modlist. Enable it in the list, or delete it from the list first if you want to reinstall it.", name=name),
            )
            return False
        answer = QMessageBox.question(
            self,
            tr("Replace existing folder"),
            tr("A leftover folder '{name}' exists in the mods folder but it is not in your modlist.\n\nReplace it with the new install? The old folder will be permanently deleted.", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        # Re-check fresh: this confirmation (and the two dialogs before it in
        # _install_mod - the archive picker and the name prompt) can sit open
        # for as long as the user takes, and _start_mod_install()'s own guard
        # check happens only after this delete already ran.
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return False
        try:
            shutil.rmtree(mods_dir / name)
        except OSError as exc:
            QMessageBox.warning(self, tr("Cannot replace folder"), str(exc))
            return False
        return True

    def _cached_archive_for(self, name: str) -> Path | None:
        """Return the cached archive for an already-listed mod, if findable.

        Matches by the ModPackRecord.folder_name convention (see
        repair.py) against the profile's own modpack_maker_list.txt - the
        only per-mod archive mapping this app has. That list's line
        numbers shift whenever the official list is reordered, so a mod
        installed a while ago can legitimately have no match at all
        (confirmed against a real profile: roughly a third of entries
        don't resolve this way) - callers must treat None as "can't do
        this automatically", not as an error.

        When the record carries an MD5 (md5_mod_db), the cached file must
        match it - an archive that's merely present under the expected
        name is not proof it's the right, uncorrupted one.
        """
        try:
            profile = self._active_profile()
        except RuntimeError:
            return None
        mo2_profile = self.profile_combo.currentText()
        records = local_modpack_records(profile.gamma, mo2_profile)
        if records is None:
            return None
        record = records.get(name)
        if record is None:
            return None
        cache_dir = Path(profile.cache)
        for archive_name in record.archive_names():
            if not archive_name or Path(archive_name).name != archive_name:
                continue
            archive = cache_dir / archive_name
            if not archive.is_file() or archive.is_symlink():
                continue
            if record.md5_mod_db:
                digest = _md5_file(archive)
                if digest is None or digest[0].lower() != record.md5_mod_db.lower():
                    continue
            return archive
        return None

    def _reinstall_mod_from_cache(self, name: str) -> None:
        """Re-extract an already-listed mod from its cached archive.

        Right-click "Reinstall from Cache" - for a mod whose on-disk
        folder is missing or corrupted without needing a full install or
        GAMMA Reset. Only proceeds when _cached_archive_for() finds a
        confirmed-matching cache archive; otherwise explains why not
        instead of silently doing nothing or falling back to a full
        repair the user didn't ask for.
        """
        if self.window.install_busy or self._install_active or self._mo2_running():
            self._update_guard()
            return
        archive = self._cached_archive_for(name)
        if archive is None:
            QMessageBox.information(
                self,
                tr("Can't reinstall from cache"),
                tr(
                    "COMMANDER has no cached archive on file for '{name}' - "
                    "this usually means the official GAMMA list has changed "
                    "since it was installed, or it isn't tracked by the "
                    "official modpack at all. Use the Reset or Uninstall "
                    "tools on the Utilities page instead.",
                    name=name,
                ),
            )
            return
        answer = QMessageBox.question(
            self,
            tr("Reinstall from Cache"),
            tr(
                "Reinstall '{name}' from its cached archive? Any existing "
                "files for this mod are replaced - its enabled/disabled "
                "state and position in the list are unaffected.",
                name=name,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._reinstall_from(
            archive, name, tr("'{name}' was reinstalled from its cached archive.", name=name)
        )

    def _reinstall_from(self, archive: Path, name: str, done_message: str) -> None:
        """Replace an already-listed mod's files with ``archive``'s.

        Its modlist entry - position, enabled state, category - is left
        exactly as it is. The current folder is moved aside first and put
        back by _finish_install() if the new install fails or is cancelled.
        """
        try:
            profile = self._active_profile()
        except RuntimeError as exc:
            QMessageBox.warning(self, tr("Error"), str(exc))
            return
        mods_dir = Path(profile.gamma) / "mods"
        if mods_dir.is_symlink():
            QMessageBox.warning(
                self,
                tr("Cannot install"),
                tr("The GAMMA mods directory cannot be a symlink."),
            )
            return
        destination = mods_dir / name
        self._reinstall_backup = None
        if destination.exists() or destination.is_symlink():
            backup = destination.with_name(f".{name}.reinstall-backup-{uuid.uuid4().hex}")
            try:
                shutil.move(str(destination), str(backup))
            except OSError as exc:
                QMessageBox.warning(self, tr("Cannot reinstall"), str(exc))
                return
            self._reinstall_backup = (destination, backup)
        self._install_is_reinstall = True
        self._reinstall_done_message = done_message
        self._start_mod_install(archive, name)

    def _update_mod_from_archive(self, name: str) -> None:
        """Right-click "Update from Archive..." on a mod the user installed.

        The new version's files replace the old ones; where the mod sits in
        the list, and whether it is enabled, stay as they are.
        """
        if self.window.install_busy or self._install_active or self._mo2_running():
            self._update_guard()
            return
        archive_name, _ = QFileDialog.getOpenFileName(
            self,
            tr("Update {name} from archive", name=name),
            str(Path.home()),
            tr("Mod archives") + " (*.zip *.7z *.rar *.fomod);;" + tr("All files") + " (*)",
        )
        if not archive_name:
            return
        answer = QMessageBox.question(
            self,
            tr("Update from Archive"),
            tr(
                "Replace the files of '{name}' with {archive}? Its place in the "
                "list and its enabled state stay the same. If the install fails, "
                "the current files are put back.",
                name=name,
                archive=Path(archive_name).name,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._reinstall_from(
            Path(archive_name), name, tr("'{name}' was updated.", name=name)
        )

    def _start_mod_install(self, archive: Path, name: str) -> None:
        if self.window.install_busy or self._install_active or self._mo2_running():
            self._update_guard()
            return
        self._install_generation += 1
        generation = self._install_generation
        self._install_source = archive
        self._install_name = name
        self._install_staging = None
        self._install_active = True
        self.window.set_install_busy(True, "mod_install")
        self._update_guard()
        self.install_progress.reset()
        self.install_progress.show()
        self.install_progress.on_started()
        self.install_progress.pause_button.hide()

        cancel_event = None

        def worker(report):
            staging_parent = Path(tempfile.mkdtemp(prefix="gamma-mod-install-"))
            staging = staging_parent / "archive"
            try:
                extract_archive(
                    archive,
                    staging,
                    cancel_event,
                    lambda percent, text: report(
                        f"Extracting {archive.name}... {percent}%"
                        if percent is not None
                        else text
                    ),
                )
                return str(staging)
            except Exception:
                shutil.rmtree(staging_parent, ignore_errors=True)
                raise

        self._install_task = StreamTask(worker, parent=self)
        cancel_event = self._install_task.cancel_event
        self._install_task.line.connect(self.install_progress.status_message)
        self._install_task.result.connect(
            lambda staging_name, generation=generation: self._on_archive_extracted(
                staging_name, generation
            )
        )
        self._install_task.error.connect(
            lambda message, generation=generation: self._on_install_error(
                message, generation
            )
        )
        self._install_task.start()

    def _on_archive_extracted(
        self, staging_name: str, generation: int | None = None
    ) -> None:
        if generation is not None and (
            not self._install_active or generation != self._install_generation
        ):
            return
        staging = Path(staging_name)
        self._install_staging = staging
        config_path = staging / "fomod" / "ModuleConfig.xml"
        fomod_root = staging
        if not config_path.is_file():
            candidates = list(staging.glob("*/fomod/ModuleConfig.xml"))
            if len(candidates) == 1:
                config_path = candidates[0]
                fomod_root = candidates[0].parent.parent
        if config_path.is_file():
            try:
                config = parse_config(config_path)
            except ModInstallError as exc:
                self._on_install_error(str(exc), generation)
                return
            dialog = FomodWizardDialog(config, fomod_root, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self._on_install_error("Mod installation cancelled", generation)
                return
            selected = staging.parent / "selected"
            try:
                apply_options(config, fomod_root, selected, dialog.selections())
            except ModInstallError as exc:
                self._on_install_error(str(exc), generation)
                return
            source = selected
        else:
            source = staging
        self._finalize_mod(source, generation)

    def _finalize_mod(self, source: Path, generation: int) -> None:
        try:
            profile = self._active_profile()
        except RuntimeError as exc:
            self._on_install_error(str(exc), generation)
            return
        mods_dir = Path(profile.gamma) / "mods"
        if mods_dir.is_symlink():
            self._on_install_error("The GAMMA mods directory cannot be a symlink.", generation)
            return
        name = self._install_name
        self.install_progress.status_message(f"Installing {name}...")

        installation_file = self._install_source.name if self._install_source else ""

        def worker():
            destination = mods_dir / name
            move_payload(source, destination, self._finalize_task.cancel_event)
            write_basic_meta_ini(destination, installation_file)
            return destination

        task = BackgroundTask(worker, parent=self)
        task.result.connect(
            lambda destination, generation=generation: self._on_mod_moved(
                destination, generation
            )
        )
        task.error.connect(
            lambda message, generation=generation: self._on_install_error(
                message, generation
            )
        )
        self._finalize_task = task
        task.start()

    def _on_mod_moved(
        self, destination: Path, generation: int | None = None
    ) -> None:
        if generation is not None and (
            not self._install_active or generation != self._install_generation
        ):
            return
        if getattr(self, "_install_is_reinstall", False):
            # The modlist.txt entry already exists (that's the whole
            # point) - add_custom_mod() below would just raise on the
            # duplicate name, so this skips straight to the same
            # post-install housekeeping the new-mod path ends with.
            self._just_installed_name = destination.name
            done_message = getattr(self, "_reinstall_done_message", None) or tr(
                "'{name}' was reinstalled from its cached archive.", name=destination.name
            )
            self.window.statusBar().showMessage(done_message, 8000)
            try:
                invalidate_baseline(self._active_profile().gamma)
            except RuntimeError:
                pass
            self._finish_install()
            # An explicit popup, not just the status-bar line above - a
            # transient message is easy to miss right after dismissing
            # the confirmation dialog, and unlike a brand-new install
            # (which scrolls to a freshly-visible "Custom Mods" entry)
            # a reinstalled mod often sits somewhere already on screen,
            # so there's no other obvious sign anything happened.
            QMessageBox.information(self, tr("Reinstalled"), done_message)
            return
        try:
            new_lines = add_custom_mod(self._lines, destination.name, enabled=False)
        except ValueError as exc:
            # Mod already exists in modlist.txt (e.g., disabled mod whose folder was deleted).
            try:
                shutil.rmtree(destination)
            except OSError:
                pass
            QMessageBox.warning(self, tr("Mod installation failed"), str(exc))
            self._finish_install()
            return
        if self._write_lines(new_lines, internal=True):
            self._just_installed_name = destination.name
            self.window.statusBar().showMessage(
                f"Installed '{destination.name}' - added disabled to "
                "Custom Mods, at the bottom of the list. Enable it below.",
                8000,
            )
            # A new mod folder just appeared under gamma/mods - Verify
            # Integrity's MD5 baseline doesn't know about it yet.
            try:
                invalidate_baseline(self._active_profile().gamma)
            except RuntimeError:
                pass
            self._finish_install()
            return
        try:
            shutil.rmtree(destination)
        except OSError:
            # The folder survived the rollback; tell the user the truth so
            # they can remove the untracked folder by hand.
            QMessageBox.warning(
                self,
                tr("Mod installed but not listed"),
                tr("modlist.txt could not be updated, and the copied files at\n{destination}\ncould not be removed automatically. Delete that folder manually to avoid an untracked mod.", destination=destination),
            )
            self._finish_install()
            return
        QMessageBox.warning(
            self,
            tr("Mod installed but not listed"),
            tr("modlist.txt could not be updated; the copied files were removed."),
        )
        self._finish_install()

    def _on_install_error(self, message: str, generation: int) -> None:
        if not self._install_active or generation != self._install_generation:
            return
        if self._install_task is not None:
            self._install_task.cancel()
        if self._finalize_task is not None:
            self._finalize_task.cancel()
        QMessageBox.warning(self, tr("Mod installation failed"), message)
        self._finish_install()

    def _cancel_install(self) -> None:
        """Cancel only the currently active archive-install tasks."""
        cancelled_any = False
        if self._install_task is not None:
            self._install_task.cancel()
            cancelled_any = True
        if self._finalize_task is not None:
            self._finalize_task.cancel()
            cancelled_any = True
        if not cancelled_any and self._install_active:
            # No task is running (e.g. the FOMOD dialog is open): finalize
            # now or _install_active would stay True and lock edit controls.
            self._install_generation += 1
            self._finish_install()

    def _finish_install(self) -> None:
        if getattr(self, "_reinstall_backup", None) is not None:
            # move_payload() only ever creates `destination` on a genuine
            # success (and removes it again itself on any failure) - so
            # its existence here is a reliable enough signal for every
            # path that ends up at _finish_install() (success, error,
            # cancel, FOMOD-dialog-cancelled) without each needing to say
            # explicitly which case this is.
            destination, backup = self._reinstall_backup
            self._reinstall_backup = None
            if destination.exists():
                shutil.rmtree(backup, ignore_errors=True)
            else:
                try:
                    shutil.move(str(backup), str(destination))
                except OSError:
                    self.window.statusBar().showMessage(
                        f"Could not restore the previous '{destination.name}' "
                        f"- it's saved at {backup}",
                        8000,
                    )
        self._install_is_reinstall = False
        self._reinstall_done_message = None
        if self._install_staging is not None:
            staging_root = self._install_staging.parent
            try:
                shutil.rmtree(staging_root)
            except OSError:
                # Surface the leak once instead of accumulating silently.
                self.window.statusBar().showMessage(
                    f"Could not remove staging folder: {staging_root}", 8000
                )
        self._install_staging = None
        self._install_task = None
        self._finalize_task = None
        self._install_active = False
        self.install_progress.reset()
        self.install_progress.hide()
        if self.window.install_operation == "mod_install":
            self.window.set_install_busy(False)
        self._update_guard()
        self._load_mods()
        if self._just_installed_name is not None:
            # A leftover search term from before the install would leave the
            # new mod's tree item hidden (_apply_filter()'s setHidden(True)),
            # so scrollToItem()/setCurrentItem() below would silently target
            # an invisible row - clear it so the mod we're about to point at
            # is guaranteed to actually be shown.
            if self.search.text():
                self.search.clear()
            self._focus_mod_in_tree(self._just_installed_name)
            self._just_installed_name = None
        if getattr(self, "_drop_queue", None):
            # Several archives were dropped at once: start the next one
            # after this install has fully wound down.
            QTimer.singleShot(0, self, self._install_next_dropped)

    def _focus_mod_in_tree(self, name: str) -> None:
        """Scroll to, select, and briefly highlight a mod by name.

        Used right after install: a newly-added mod lands disabled in
        "Custom Mods", at the bottom of the list (see add_custom_mod()) -
        this scrolls it into view and gives it a clear visual cue so it's
        not mistaken for "it didn't work" just because it isn't near the
        top.
        """
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                item = header.child(j)
                if item.text(0) == name:
                    self.tree.scrollToItem(
                        item, QAbstractItemView.ScrollHint.PositionAtCenter
                    )
                    self.tree.setCurrentItem(item)
                    return

    def _selected_mod_indexes(self) -> list[int]:
        indexes: list[int] = []
        for item in self.tree.selectedItems():
            if item.parent() is not None:
                indexes.append(item.data(0, Qt.ItemDataRole.UserRole))
        return indexes

    def _selected_mod_names(self) -> list[str]:
        return [
            item.text(0)
            for item in self.tree.selectedItems()
            if item.parent() is not None
        ]

    def _select_mod_names(self, names: list[str]) -> None:
        wanted = set(names)
        self.tree.clearSelection()
        for index in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(index)
            for child_index in range(header.childCount()):
                item = header.child(child_index)
                if item.text(0) in wanted:
                    item.setSelected(True)
                    self.tree.scrollToItem(item)
                    wanted.discard(item.text(0))
        self._update_count()

    def _set_selected_mods(self, enabled: bool) -> None:
        if self.window.install_busy:
            self._update_guard()
            return
        indexes = self._selected_mod_indexes()
        if not indexes:
            return
        new_lines = list(self._lines)
        for idx in indexes:
            new_lines = set_status_at(new_lines, idx, enabled)
        if self._write_lines(new_lines, snapshot=False):
            self._load_mods()

    def _delete_selected_mods(self) -> None:
        if self.window.install_busy:
            self._update_guard()
            return
        indexes = self._selected_mod_indexes()
        if not indexes:
            return
        names = self._selected_mod_names()

        box = QMessageBox(self)
        box.setWindowTitle(tr("Delete Mods"))
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            tr("Remove {arg} mod(s) from the modlist?\n\nThis only edits the modlist; mod files are not deleted.", arg=len(indexes))
        )
        delete_files = QCheckBox(
            tr("Also delete the mod folder(s) on disk - cannot be undone")
        )
        box.setCheckBox(delete_files)
        confirm = box.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() != confirm:
            return

        # Delete folders BEFORE committing the modlist: if folder deletion
        # fails partway, the modlist still references the remaining folders
        # and nothing is orphaned.
        deleting_files = delete_files.isChecked()
        if deleting_files and not self._delete_mod_folders(names):
            return
        if deleting_files:
            # Actual files under gamma/mods just changed - Verify
            # Integrity's MD5 baseline must not compare against them.
            try:
                invalidate_baseline(self._active_profile().gamma)
            except RuntimeError:
                pass
        new_lines = list(self._lines)
        for idx in sorted(indexes, reverse=True):
            new_lines = delete_at(new_lines, idx)
        if not self._write_lines(new_lines):
            return
        self._load_mods()

    def _delete_mod_folders(self, names: list[str]) -> bool:
        """Permanently remove the mod folders for *names* from the mods dir.

        Returns ``True`` only when every requested folder is gone, so the
        caller can avoid committing the modlist on a partial failure.
        """
        try:
            profile = self._active_profile()
        except RuntimeError as exc:
            QMessageBox.warning(self, tr("Error"), str(exc))
            return False
        mods_dir = Path(profile.gamma) / "mods"
        if mods_dir.is_symlink():
            QMessageBox.warning(
                self,
                tr("Cannot delete folders"),
                tr("The GAMMA mods directory cannot be a symlink."),
            )
            return False
        failures: list[str] = []
        for name in names:
            folder = mods_dir / name
            try:
                if folder.is_symlink():
                    failures.append(f"{name} (symlink, skipped)")
                    continue
                if not folder.exists():
                    continue
                shutil.rmtree(folder)
            except OSError as exc:
                failures.append(f"{name} ({exc})")
        if failures:
            QMessageBox.warning(
                self,
                tr("Some folders were not deleted"),
                "\n".join(failures),
            )
            return False
        return True

    def _move_selected(self, delta: int) -> None:
        if self.window.install_busy:
            self._update_guard()
            return
        indexes = self._selected_mod_indexes()
        if len(indexes) != 1:
            return
        selected_name = self._selected_mod_names()[0]
        if not self._reorder_warned:
            answer = QMessageBox.question(
                self,
                tr("Reorder Mods"),
                tr("Moving a mod changes the load order. An incorrect load order can break your save or the game.\n\nContinue?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._reorder_warned = True
        new_lines = move(self._lines, indexes[0], delta)
        if new_lines == self._lines:
            return
        if self._write_lines(new_lines):
            self._load_mods()
            self._select_mod_names([selected_name])

    # ----- context menu -----
    def _show_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        if item.parent() is None:
            self._show_category_context_menu(item, pos)
            return
        # Operate on the mod that was right-clicked.
        self.tree.clearSelection()
        self.tree.setCurrentItem(item)
        item.setSelected(True)
        self._update_count()

        blocked = self._mo2_running() or self.window.install_busy or self._install_active
        indexes = self._selected_mod_indexes()
        single = len(indexes) == 1

        menu = QMenu(self)
        rename_action = menu.addAction(tr("Rename..."))
        rename_action.setEnabled(single and not blocked)
        menu.addSeparator()
        enable_action = menu.addAction(tr("Enable"))
        disable_action = menu.addAction(tr("Disable"))
        menu.addSeparator()
        move_up_action = menu.addAction(tr("Move Up"))
        move_down_action = menu.addAction(tr("Move Down"))
        for action in (enable_action, disable_action, move_up_action, move_down_action):
            action.setEnabled(not blocked)
        menu.addSeparator()

        category_actions: dict = {}
        category_menu = menu.addMenu(tr("Move to Category"))
        # dict.fromkeys(): category names should already be unique, but
        # deduping here is a harmless guard against a duplicate menu entry
        # if that ever stops being true.
        categories = list(dict.fromkeys(name for name, _ in grouped(self._lines)))
        # The header's display text carries a "(N)" mod-count suffix (see
        # _populate_tree()), so comparing it against grouped()'s raw
        # category names never matched and the mod's own category was
        # offered in this submenu - use the raw name stored out of band.
        current = item.parent().data(0, Qt.ItemDataRole.UserRole)
        for cat in categories:
            if cat == current:
                continue
            action = category_menu.addAction(cat)
            category_actions[action] = cat
        if not category_actions:
            category_menu.setEnabled(False)

        folder_action = menu.addAction(tr("Open Mod Folder"))
        folder_action.setEnabled(not blocked)
        conflicts_action = menu.addAction(tr("Show Conflicts..."))
        conflicts_action.setEnabled(single)
        menu.addSeparator()
        # Only for a single mod - re-extracting from cache is a per-mod
        # operation, there's no meaningful "for all N selected" version.
        reinstall_action = menu.addAction(tr("Reinstall from Cache..."))
        reinstall_action.setEnabled(single and not blocked)
        # Only for mods the user installed: a GAMMA mod's files are GAMMA's
        # to update, and replacing them by hand breaks the next update.
        custom = set(custom_mod_names(self._lines))
        update_action = menu.addAction(tr("Update from Archive..."))
        update_action.setEnabled(
            single and not blocked and bool(self._selected_mod_names())
            and self._selected_mod_names()[0] in custom
        )
        menu.addSeparator()
        delete_action = menu.addAction(tr("Delete"))
        delete_action.setEnabled(not blocked)

        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == rename_action:
            self._rename_selected_mod()
        elif chosen == enable_action:
            self._set_selected_mods(True)
        elif chosen == disable_action:
            self._set_selected_mods(False)
        elif chosen == move_up_action:
            self._move_selected(1)
        elif chosen == move_down_action:
            self._move_selected(-1)
        elif chosen in category_actions:
            self._move_selected_to_category(category_actions[chosen])
        elif chosen == folder_action:
            names = self._selected_mod_names()
            if names:
                self._open_mod_folder_by_name(names[0])
        elif chosen == reinstall_action:
            names = self._selected_mod_names()
            if names:
                self._reinstall_mod_from_cache(names[0])
        elif chosen == update_action:
            names = self._selected_mod_names()
            if names:
                self._update_mod_from_archive(names[0])
        elif chosen == conflicts_action:
            names = self._selected_mod_names()
            if names:
                self._show_mod_conflicts(names[0])
        elif chosen == delete_action:
            self._delete_selected_mods()

    def _rename_selected_mod(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        indexes = self._selected_mod_indexes()
        if len(indexes) != 1:
            return
        old_name = self._selected_mod_names()[0]
        new_name, accepted = QInputDialog.getText(
            self,
            tr("Rename Mod"),
            tr("New display name in the modlist:"),
            text=old_name,
        )
        if not accepted:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        try:
            new_lines = rename_mod(self._lines, old_name, new_name)
        except ValueError as exc:
            QMessageBox.warning(self, tr("Invalid Name"), str(exc))
            return
        if new_lines == self._lines:
            return
        if self._write_lines(new_lines):
            self._load_mods()
            self._select_mod_names([new_name])

    def _show_category_context_menu(self, item, pos) -> None:
        category = item.data(0, Qt.ItemDataRole.UserRole)
        # "Uncategorized" is grouped()'s synthetic label for trailing,
        # separator-less mods - there's no real separator line to rename.
        if category is None or category == "Uncategorized":
            return
        blocked = self._mo2_running() or self.window.install_busy or self._install_active
        menu = QMenu(self)
        rename_action = menu.addAction(tr("Rename Category..."))
        rename_action.setEnabled(not blocked)
        menu.addSeparator()
        move_up_action = menu.addAction(tr("Move Category Up"))
        move_down_action = menu.addAction(tr("Move Category Down"))
        move_up_action.setEnabled(not blocked)
        move_down_action.setEnabled(not blocked)
        delete_action = None
        if category in self._tracked_user_categories():
            menu.addSeparator()
            delete_action = menu.addAction(tr("Delete Category..."))
            delete_action.setEnabled(not blocked)
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen == rename_action:
            self._rename_category(category)
        elif chosen == move_up_action:
            self._move_category(category, -1)
        elif chosen == move_down_action:
            self._move_category(category, 1)
        elif delete_action is not None and chosen == delete_action:
            self._delete_category(category)

    def _move_category(self, category: str, delta: int) -> None:
        """Move a category, with all its members, past its neighbor -

        delta -1 is "Move Up" (earlier on screen = later in file = lower
        MO2 priority, matching the file's own top-to-bottom = highest-to-
        lowest priority convention), +1 is "Move Down".
        """
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        categories = [name for name, _mods in grouped(self._lines) if name != "Uncategorized"]
        try:
            index = categories.index(category)
        except ValueError:
            return
        neighbor_index = index + delta
        if not 0 <= neighbor_index < len(categories):
            return
        neighbor = categories[neighbor_index]
        try:
            new_lines = move_category(
                self._lines, category, neighbor, before=delta < 0
            )
        except ValueError as exc:
            QMessageBox.warning(self, tr("Cannot Move Category"), str(exc))
            return
        if self._write_lines(new_lines):
            self._load_mods()

    def _delete_category(self, category: str) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        _name, mods = next(
            (
                (name, mods)
                for name, mods in grouped(self._lines)
                if name == category
            ),
            (category, []),
        )
        box = QMessageBox(self)
        box.setWindowTitle(tr("Delete Category"))
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            tr(
                'Delete the category "{category}"? Its {count} mod(s) are moved to Uncategorized, not deleted, unless you check the box below.',
                category=category,
                count=len(mods),
            )
        )
        delete_mods_check = QCheckBox(
            tr("Also remove these {count} mod(s) from the modlist", count=len(mods))
        )
        box.setCheckBox(delete_mods_check)
        confirm = box.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() != confirm:
            return
        try:
            new_lines = delete_category(
                self._lines, category, delete_members=delete_mods_check.isChecked()
            )
        except ValueError as exc:
            QMessageBox.warning(self, tr("Cannot Delete Category"), str(exc))
            return
        if self._write_lines(new_lines):
            self._remove_tracked_user_category(category)
            self._load_mods()

    def _rename_category(self, old_category: str) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        new_name, accepted = QInputDialog.getText(
            self,
            "Rename Category",
            "New category name:",
            text=old_category,
        )
        if not accepted:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_category:
            return
        try:
            new_lines = rename_category(self._lines, old_category, new_name)
        except ValueError as exc:
            QMessageBox.warning(self, tr("Invalid Name"), str(exc))
            return
        if new_lines == self._lines:
            return
        if self._write_lines(new_lines):
            # rename_category() may itself further clean new_name (e.g.
            # stripping a redundant "_separator" suffix the user typed) -
            # find the actual resulting name by diffing rather than
            # assuming new_name is exactly what landed in the file.
            final_name = next(
                (
                    separator_name(_info[1])
                    for old_line, new_line in zip(self._lines, new_lines, strict=True)
                    if old_line != new_line
                    and (_info := _line_info(new_line)) is not None
                    and separator_name(_info[1]) is not None
                ),
                new_name,
            )
            self._rename_tracked_user_category(old_category, final_name)
            self._load_mods()

    # ----- user-created-category tracking (gui_settings.py) -----
    #
    # An allow-list of category names the user themselves created via
    # "New Category" - the *only* source of truth for which categories
    # are safe to offer "Delete Category..." on. Deliberately never a
    # hardcoded/fetched "official GAMMA category names" list: that would
    # go stale whenever the modpack adds/renames categories upstream,
    # risking exposing Delete on a real GAMMA category. Worst case here
    # is the reverse (a user category not yet offered Delete), never a
    # real category wrongly offered it.
    def _restore_missing_user_categories(self) -> None:
        """Re-add a user-created category MO2 silently dropped.

        Reported bug: a "New Category" the user made disappeared after
        launching the game. Root cause is outside this app - Mod
        Organizer 2 itself does not persist a completely empty separator
        category across a session where it rewrites modlist.txt on its
        own (which launching through MO2 does at exit); a brand-new
        category with nothing filed into it yet is exactly the case that
        happens to. This re-adds any category still in this profile's
        own tracked user_created_categories list (see
        _add_tracked_user_category()) but missing from the modlist.txt
        just read from disk - the same list "Delete Category..." already
        uses to know which categories are the user's own, so a category
        only ever stops coming back once the user deletes it themselves
        (which also untracks it - see _delete_category()).

        Silently declines to write while MO2 is running/blocked, same as
        any other edit here - it simply tries again next time this loads.
        """
        tracked = self._tracked_user_categories()
        if not tracked:
            return
        existing = {name for name, _ in grouped(self._lines)}
        missing = [name for name in tracked if name not in existing]
        if not missing:
            return
        restored = self._lines
        for name in missing:
            try:
                restored = add_category(restored, name)
            except ValueError:
                continue
        self._write_lines(restored, quiet=True, snapshot=False)

    def _tracked_user_categories(self) -> list[str]:
        profile = self.window.settings.active_profile
        if profile is None:
            return []
        tracked = gui_settings.load_gui_settings().get("user_created_categories", {})
        return list(tracked.get(profile.profile_name, []))

    def _add_tracked_user_category(self, name: str) -> None:
        profile = self.window.settings.active_profile
        if profile is None:
            return
        tracked = dict(
            gui_settings.load_gui_settings().get("user_created_categories", {})
        )
        names = list(tracked.get(profile.profile_name, []))
        if name not in names:
            names.append(name)
        tracked[profile.profile_name] = names
        gui_settings.save_gui_settings(user_created_categories=tracked)

    def _rename_tracked_user_category(self, old_name: str, new_name: str) -> None:
        profile = self.window.settings.active_profile
        if profile is None:
            return
        tracked = dict(
            gui_settings.load_gui_settings().get("user_created_categories", {})
        )
        names = list(tracked.get(profile.profile_name, []))
        if old_name not in names:
            return
        tracked[profile.profile_name] = [
            new_name if n == old_name else n for n in names
        ]
        gui_settings.save_gui_settings(user_created_categories=tracked)

    def _remove_tracked_user_category(self, name: str) -> None:
        profile = self.window.settings.active_profile
        if profile is None:
            return
        tracked = dict(
            gui_settings.load_gui_settings().get("user_created_categories", {})
        )
        names = [n for n in tracked.get(profile.profile_name, []) if n != name]
        tracked[profile.profile_name] = names
        gui_settings.save_gui_settings(user_created_categories=tracked)

    def _move_selected_to_category(self, category: str) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        names = self._selected_mod_names()
        if len(names) != 1:
            return
        if not self._reorder_warned:
            answer = QMessageBox.question(
                self,
                tr("Move Mod"),
                tr("Moving a mod changes the load order. An incorrect load order can break your save or the game.\n\nContinue?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._reorder_warned = True
        new_lines = move_mod(self._lines, names[0], category=category)
        if new_lines == self._lines:
            return
        if self._write_lines(new_lines):
            self._load_mods()
            self._select_mod_names([names[0]])

    def _open_mod_folder_by_name(self, name: str) -> None:
        try:
            profile = self._active_profile()
        except RuntimeError as exc:
            QMessageBox.warning(self, tr("Error"), str(exc))
            return
        mods_dir = Path(profile.gamma) / "mods"
        if mods_dir.is_symlink():
            QMessageBox.warning(
                self,
                tr("Cannot open folder"),
                tr("The GAMMA mods directory cannot be a symlink."),
            )
            return
        folder = mods_dir / name
        if not folder.is_dir():
            QMessageBox.information(
                self,
                tr("No Mod Folder"),
                tr("No folder '{name}' exists in {mods_dir}.\n\nRenaming only changes the modlist display name; it does not rename the folder on disk.", name=name, mods_dir=mods_dir),
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(self, tr("Cannot open folder"), tr("Could not open:\n{folder}", folder=folder))

    def _set_selected(self) -> None:
        if self.window.install_busy:
            self._update_guard()
            return
        if self._mo2_running():
            # MO2 rewrites ModOrganizer.ini on exit and would silently
            # overwrite this change.
            self._update_guard()
            return
        profile = self.profile_combo.currentText()
        if not profile:
            return
        self.set_selected_button.setEnabled(False)
        task = BackgroundTask(
            run_sync,
            ["mo2", "config", "set", "selected-profile", profile],
            timeout=_QUERY_TIMEOUT,
            parent=self,
        )
        task.result.connect(lambda res: self._on_set_selected_done(profile, *res))
        task.error.connect(lambda msg: self._on_set_selected_error(msg))
        task.start()

    def _on_set_selected_done(self, profile: str, rc: int, out: str) -> None:
        self.set_selected_button.setEnabled(True)
        if rc == 0:
            self._mo2_selected_profile = profile
            self._sync_selected_status()
            # This button's whole point is "make this the profile in use" -
            # without also updating the active CliProfile's own mo2_profile
            # field, everything that reads it directly (Dashboard's Profile
            # overview, Play page's launch command) kept showing/using the
            # old profile until it happened to get edited some other way.
            active = self.window.settings.active_profile
            if active is not None and active.mo2_profile != profile:
                active.mo2_profile = profile
                self.window.settings.save()
                self.window.refresh_settings()
        else:
            QMessageBox.warning(
                self, tr("Failed"), out.strip() or "Could not set selected profile"
            )

    def _sync_selected_status(self, *_args) -> None:
        """Show whether MO2 itself opens the profile being edited.

        A green chip when it does, and nothing else to do. When it doesn't,
        the profile MO2 does open, and the button that switches it. Some
        tests build this page without every widget, hence the getattr.
        """
        label = getattr(self, "selected_label", None)
        if label is None:
            return
        selected = getattr(self, "_mo2_selected_profile", "")
        current = self.profile_combo.currentText()
        in_use = bool(current) and selected == current
        if in_use:
            label.setText("✓  " + tr("Selected in MO2"))
            label.setToolTip(tr("Mod Organizer 2 opens this profile."))
        else:
            shown = selected or "-"
            if len(shown) > _SELECTED_NAME_MAX:
                shown = shown[: _SELECTED_NAME_MAX - 1] + "…"
            label.setText(tr("MO2 uses: {arg}", arg=shown))
            tip = tr("Mod Organizer 2 opens a different profile. Use the button to switch it to this one.")
            if shown != (selected or "-"):
                tip = selected + "\n\n" + tip
            label.setToolTip(tip)
        label.setProperty("state", "ok" if in_use else "bad")
        label.style().unpolish(label)
        label.style().polish(label)
        button = getattr(self, "set_selected_button", None)
        if button is not None:
            button.setVisible(not in_use and bool(current))

    def _on_set_selected_error(self, msg: str) -> None:
        self.set_selected_button.setEnabled(True)
        QMessageBox.warning(self, tr("Error"), msg)

    # ----- MO2 profile management (New/Rename/Delete) -----
    def _existing_mo2_profile_names(self) -> list[str]:
        return [
            self.profile_combo.itemText(i) for i in range(self.profile_combo.count())
        ]

    def _create_mo2_profile(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        existing = self._existing_mo2_profile_names()
        current = self.profile_combo.currentText()
        empty_label = tr("(Empty profile)")

        dialog = QDialog(self)
        dialog.setWindowTitle(tr("New MO2 Profile"))
        dialog.resize(420, 160)
        dlg_layout = QVBoxLayout(dialog)
        dlg_layout.addWidget(QLabel(tr("Profile name:")))
        name_field = QLineEdit()
        name_field.setMinimumWidth(280)
        dlg_layout.addWidget(name_field)
        dlg_layout.addWidget(QLabel(tr("Copy modlist from:")))
        source_combo = QComboBox()
        source_combo.addItem(empty_label)
        source_combo.addItems(existing)
        if current:
            idx = source_combo.findText(current)
            if idx >= 0:
                source_combo.setCurrentIndex(idx)
        dlg_layout.addWidget(source_combo)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dlg_layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        name = name_field.text().strip()
        if not _valid_name(name):
            QMessageBox.warning(
                self,
                tr("Invalid Name"),
                tr("Profile names cannot be empty or contain path separators."),
            )
            return
        if name in existing:
            QMessageBox.warning(
                self,
                tr("Profile Exists"),
                tr('A profile named "{name}" already exists.', name=name),
            )
            return

        gamma = self._active_profile().gamma
        new_modlist = Path(gamma) / "profiles" / name / "modlist.txt"
        source_choice = source_combo.currentText()
        try:
            copied = (
                source_choice != empty_label
                and seed_new_mo2_profile(gamma, name, source_profile=source_choice)
            )
            if not copied:
                # Either "(Empty profile)" was chosen, or the source
                # profile had no modlist.txt of its own to copy (e.g. a
                # brand-new install with nothing set up yet) -
                # save_lines() creates the profile folder either way, so
                # the new profile still shows up and Mod Manager has
                # something to render.
                save_lines(new_modlist, [])
        except OSError as exc:
            QMessageBox.warning(self, tr("Could Not Create Profile"), str(exc))
            return

        self._pending_profile_select = name
        self._load_profiles(self._profiles_generation)

    def _rename_mo2_profile(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        old_name = self.profile_combo.currentText()
        if not old_name:
            return
        existing = self._existing_mo2_profile_names()
        new_name, accepted = QInputDialog.getText(
            self,
            tr("Rename Profile"),
            tr('New name for "{name}":', name=old_name),
            text=old_name,
        )
        if not accepted:
            return
        new_name = new_name.strip()
        if new_name == old_name:
            return
        if not _valid_name(new_name):
            QMessageBox.warning(
                self,
                tr("Invalid Name"),
                tr("Profile names cannot be empty or contain path separators."),
            )
            return
        if new_name in existing:
            QMessageBox.warning(
                self,
                tr("Profile Exists"),
                tr('A profile named "{name}" already exists.', name=new_name),
            )
            return

        gamma = self._active_profile().gamma
        profiles_root = Path(gamma) / "profiles"
        try:
            (profiles_root / old_name).rename(profiles_root / new_name)
        except OSError as exc:
            QMessageBox.warning(self, tr("Could Not Rename Profile"), str(exc))
            return

        # MO2 would otherwise be left pointing at a folder that no longer
        # exists - only touch selected_profile when it actually named the
        # profile just renamed.
        if self._mo2_selected_profile == old_name:
            run_sync(
                ["mo2", "config", "set", "selected-profile", new_name],
                timeout=_QUERY_TIMEOUT,
            )
        # Likewise, any app-level CliProfile pointing at this MO2 profile
        # (by folder name, for this same GAMMA install) would otherwise be
        # left referencing a ghost folder.
        settings = self.window.settings
        changed = False
        for cli_profile in settings.profiles:
            if cli_profile.gamma == gamma and cli_profile.mo2_profile == old_name:
                cli_profile.mo2_profile = new_name
                changed = True
        if changed:
            settings.save()
            self.window.refresh_settings()

        self._pending_profile_select = new_name
        self._load_profiles(self._profiles_generation)

    def _delete_mo2_profile(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        name = self.profile_combo.currentText()
        if not name:
            return
        if self.profile_combo.count() <= 1:
            QMessageBox.warning(
                self,
                tr("Cannot Delete Profile"),
                tr("This is the only MO2 profile - nothing would be left to switch to."),
            )
            return
        box = QMessageBox(self)
        box.setWindowTitle(tr("Delete Profile"))
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            tr(
                'Permanently delete the MO2 profile "{name}" and everything in it '
                "(modlist, saves, settings)? This cannot be undone.",
                name=name,
            )
        )
        confirm = box.addButton(tr("Delete"), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() != confirm:
            return

        self.manage_profile_button.setEnabled(False)
        task = BackgroundTask(
            run_sync, ["mo2", "profile", "delete", name], timeout=_QUERY_TIMEOUT, parent=self
        )
        task.result.connect(lambda res: self._on_delete_profile_done(name, *res))
        task.error.connect(self._on_delete_profile_error)
        task.start()

    def _on_delete_profile_done(self, name: str, rc: int, out: str) -> None:
        self.manage_profile_button.setEnabled(True)
        if rc != 0:
            QMessageBox.warning(
                self, tr("Failed"), out.strip() or tr("Could not delete profile.")
            )
            return
        self._load_profiles(self._profiles_generation)

    def _on_delete_profile_error(self, msg: str) -> None:
        self.manage_profile_button.setEnabled(True)
        QMessageBox.warning(self, tr("Error"), msg)

    def _create_category(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        name, accepted = QInputDialog.getText(self, "New Category", "Category name:")
        if not accepted:
            return
        try:
            new_lines = add_category(self._lines, sanitize_name(name))
        except (ModInstallError, ValueError) as exc:
            QMessageBox.warning(self, tr("Invalid Category"), str(exc))
            return
        if self._write_lines(new_lines):
            # add_category() always appends the new separator as the
            # very last line - read the actual (possibly further
            # cleaned) name back from it rather than assuming the raw
            # typed text is exactly what landed in the file.
            info = _line_info(new_lines[-1])
            final_name = separator_name(info[1]) if info is not None else None
            if final_name is not None:
                self._add_tracked_user_category(final_name)
            self._load_mods()

    def _check_file_conflicts(self) -> None:
        """Scan enabled mods' folders for files shared by 2+ of them.

        Not full MO2-style conflict resolution - just "what is the
        current load order actually overriding," which nothing else in
        the app surfaces. Scanning can take a while with 1000+ mods, so
        it runs in the background rather than freezing the UI.
        """
        if self.window.install_busy:
            self._update_guard()
            return
        try:
            profile = self._active_profile()
        except RuntimeError as exc:
            QMessageBox.warning(self, tr("Error"), str(exc))
            return
        mods_dir = Path(profile.gamma) / "mods"
        if mods_dir.is_symlink():
            QMessageBox.warning(
                self,
                tr("Cannot scan for conflicts"),
                tr("The GAMMA mods directory cannot be a symlink."),
            )
            return
        lines = list(self._lines)
        self.conflicts_button.setEnabled(False)
        self.conflicts_button.setText(tr("Scanning..."))
        task = BackgroundTask(
            find_enabled_mod_file_conflicts, lines, mods_dir, parent=self
        )
        task.result.connect(self._on_conflicts_found)
        task.error.connect(self._on_conflicts_error)
        task.start()

    def _on_conflicts_found(self, conflicts: list[tuple[str, list[str]]]) -> None:
        self.conflicts_button.setEnabled(True)
        self.conflicts_button.setText(tr("Check for File Conflicts"))
        if not conflicts:
            QMessageBox.information(
                self,
                tr("No Conflicts Found"),
                tr("No enabled mod shares a file with another enabled mod."),
            )
            return
        _ConflictsDialog(
            summarize_mod_conflicts(conflicts),
            custom_mod_names(self._lines),
            self,
        ).exec()

    def _show_mod_conflicts(self, name: str) -> None:
        """Right-click "Show Conflicts...": one mod against every enabled mod.

        Scans every enabled mod's game files, so it runs in the background.
        """
        try:
            profile = self._active_profile()
        except RuntimeError as exc:
            QMessageBox.warning(self, tr("Error"), str(exc))
            return
        mods_dir = Path(profile.gamma) / "mods"
        lines = list(self._lines)
        enabled = any(
            mod == name and status == "Enabled" for status, mod in entries(lines)
        )
        self.window.statusBar().showMessage(
            tr("Checking conflicts for {name}...", name=name)
        )
        task = BackgroundTask(mod_conflicts, lines, mods_dir, name, parent=self)
        self._mod_conflicts_task = task

        def _done(result) -> None:
            self._mod_conflicts_task = None
            self.window.statusBar().clearMessage()
            overrides, overridden_by = result
            ModConflictsDialog(
                name, overrides, overridden_by, enabled=enabled, parent=self
            ).exec()

        def _failed(message: str) -> None:
            self._mod_conflicts_task = None
            self.window.statusBar().clearMessage()
            QMessageBox.warning(self, tr("Scan Failed"), message)

        task.result.connect(_done)
        task.error.connect(_failed)
        task.start()

    def _on_conflicts_error(self, message: str) -> None:
        self.conflicts_button.setEnabled(True)
        self.conflicts_button.setText(tr("Check for File Conflicts"))
        QMessageBox.warning(self, tr("Scan Failed"), message)

    def _on_tree_drop(
        self,
        source_name: str,
        target_name: str | None,
        category: str,
        before: bool,
    ) -> None:
        """Persist a drag/drop reorder reported by the mod tree."""
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        # `before` from DragTree means "dropped above the target on
        # screen." move_mod()'s before/at_start are file-index semantics,
        # and screen position runs opposite to file position (see
        # _populate_tree()'s comment) - "above on screen" means "wants
        # lower priority than target," which is a LATER file index, i.e.
        # move_mod's before=False. Invert here, at the one boundary where
        # screen semantics cross into file-index semantics.
        new_lines = move_mod(
            self._lines,
            source_name,
            target_name=target_name,
            category=category if target_name is None else None,
            before=not before,
            at_start=(not before) and target_name is None,
        )
        if new_lines == self._lines:
            return
        if self._write_lines(new_lines):
            # Defer tree rebuild so it runs after Qt's DragDrop
            # state machine finishes cleaning up the drop event.
            QTimer.singleShot(0, self._load_mods)
            QTimer.singleShot(
                0, self, lambda name=source_name: self._select_mod_names([name])
            )

    def _on_category_drop(
        self, source_category: str, target_category: str, before: bool
    ) -> None:
        """Persist a whole-category drag/drop reorder from the mod tree."""
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        # Same screen-vs-file inversion _on_tree_drop() already applies -
        # "dropped above the target on screen" wants lower priority than
        # the target, which is a LATER file position (before=False there).
        try:
            new_lines = move_category(
                self._lines, source_category, target_category, before=not before
            )
        except ValueError as exc:
            QMessageBox.warning(self, tr("Cannot Move Category"), str(exc))
            return
        if new_lines == self._lines:
            return
        if self._write_lines(new_lines):
            QTimer.singleShot(0, self._load_mods)

    def _on_drop_cancelled(self) -> None:
        self.window.statusBar().showMessage(
            tr("Drop cancelled - nothing was moved."), 2500
        )

    def _open_mo2(self) -> None:
        mo2_profile = self.profile_combo.currentText()
        state = gui_settings.load_gui_settings()
        kind = state.get("runner") or "auto"
        # resolve_runner wants the raw configured path (STEAM_COMPAT_DATA_PATH
        # for Proton), not the resolved WINEPREFIX.
        prefix = state.get("wine_prefix") or ""
        try:
            profile = self._active_profile()
            runner = resolve_runner(kind, prefix)
            command, env, cwd = build_command(
                profile.gamma, runner, profile=mo2_profile or None
            )
            ensure_runner_prefix(runner)
            launch_detached(command, env, cwd, log_path=logs_dir() / "launcher.log")
        except (LaunchError, RuntimeError) as exc:
            QMessageBox.warning(self, tr("Cannot launch MO2"), str(exc))

    def _restore_backup(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            QMessageBox.warning(
                self,
                tr("Mod Organizer is running"),
                tr("Close Mod Organizer first - it would overwrite your changes when it exits."),
            )
            return
        mo2_profile = self.profile_combo.currentText()
        try:
            path = self._modlist_path(mo2_profile)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, tr("Failed"), str(exc))
            return
        default_backup = self._backup_path(path)
        start_path = default_backup if default_backup.is_file() else path.parent
        backup_name, _ = QFileDialog.getOpenFileName(
            self,
            "Open modlist backup",
            str(start_path),
            "Modlist backups (*.bak *.txt);;All files (*)",
        )
        if not backup_name:
            return
        bak = Path(backup_name).expanduser()
        try:
            if not bak.is_file():
                raise FileNotFoundError(f"Backup not found: {bak}")
            if bak.is_symlink():
                raise ValueError("The selected backup cannot be a symlink.")
            if bak.resolve() == path.resolve():
                raise ValueError("The selected file is the current modlist.")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, tr("Invalid Backup"), str(exc))
            return
        self._restore_backup_file(path, bak, "Restore Backup")

    def _restore_original_order(self) -> None:
        """Place the GAMMA mods back into their original default load order.

        Only the shared GAMMA mods are reordered to match the automatic
        pre-edit backup. User-installed mods, new categories, comments, and
        each mod's enabled/disabled state are left untouched.
        """
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        mo2_profile = self.profile_combo.currentText()
        if not mo2_profile:
            return
        try:
            path = self._modlist_path(mo2_profile)
            bak = self._backup_path(path)
            if not bak.is_file():
                raise FileNotFoundError("No original modlist backup exists yet.")
            original = read_lines(bak)
        except (OSError, RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, tr("Original Order Unavailable"), str(exc))
            self._update_guard()
            return
        new_lines = reorder_to_original(self._lines, original)
        if new_lines == self._lines:
            QMessageBox.information(
                self,
                tr("Original Order"),
                tr("The GAMMA mods are already in their original order."),
            )
            return
        answer = QMessageBox.question(
            self,
            tr("Restore Original Order"),
            tr("Place the GAMMA mods back into their original default load order?\n\nYour installed mods, new categories, and enabled/disabled state will be kept."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._write_lines(new_lines):
            self._load_mods()
            self.backup_status.setText(tr("GAMMA mods restored to order from: {name}", name=bak.name))

    def _restore_backup_file(self, path: Path, bak: Path, title: str) -> None:
        """Restore *bak* atomically after preserving the current modlist."""
        answer = QMessageBox.question(
            self,
            title,
            tr("Restore the modlist from:\n{bak}\n\nCurrent edits will be lost.", bak=bak),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # Re-check fresh: this dialog (and the open-file dialog before it in
        # _restore_backup) blocks for as long as the user takes, and this
        # write goes straight to modlist.txt via its own file copy rather
        # than through _write_lines(), so it does not get that helper's own
        # guard for free - MO2 could have been started in the meantime.
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            QMessageBox.warning(
                self,
                tr("Mod Organizer is running"),
                tr("Close Mod Organizer first - it would overwrite your changes when it exits."),
            )
            return
        current_backup = self._backup_path(path)
        tmp = path.with_name(path.name + ".restore.tmp")
        try:
            if path.is_file():
                shutil.copy2(path, self._timestamped_backup_path(path))
                if not current_backup.exists():
                    shutil.copy2(path, current_backup)
            shutil.copy2(bak, tmp)
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            QMessageBox.warning(self, tr("Failed"), str(exc))
            return
        self._load_mods()
        self.backup_status.setText(tr("Restored from: {bak}", bak=bak))
        self._update_guard()
