"""Mod Manager: browse MO2 profiles and manage mods (enable/disable/delete).

The mod list is shown grouped by the separator categories GAMMA ships in
``modlist.txt``. Simple, safe operations (toggle, delete, reorder within a
category) are done directly on the file with automatic backups; anything more
complex is handed over to Mod Organizer itself.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..cli_runner import run_sync
from ..config import logs_dir
from ..launcher import LaunchError, build_command, launch_detached, resolve_runner
from ..modlist import (
    delete_at,
    grouped,
    move,
    read_lines,
    save_lines,
    set_status_at,
)
from .common import BackgroundTask, make_card, mo2_running, section_label

BACKUP_SUFFIX = ".gammagui.bak"
#: Short timeout: these are metadata lookups, not installs.
_QUERY_TIMEOUT = 30


def _query_mo2_profiles() -> tuple[list[str], str]:
    """Return (profile names, selected profile). Runs on a worker thread."""
    rc, out = run_sync(["mo2", "profiles", "list"], timeout=_QUERY_TIMEOUT)
    names = (
        [line.strip() for line in out.splitlines() if line.strip()] if rc == 0 else []
    )
    rc, out = run_sync(
        ["mo2", "config", "get", "selected-profile"], timeout=_QUERY_TIMEOUT
    )
    return names, out.strip() if rc == 0 else ""


class ModManagerPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._lines: list[str] = []
        self._populating = False
        self._reorder_warned = False
        self._profiles_loading = False
        self._profiles_task = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        card, layout = make_card()
        root.addWidget(card, 1)
        layout.addWidget(section_label("Mod Manager"))

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("MO2 profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._load_mods)
        top_row.addWidget(self.profile_combo, 1)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        top_row.addWidget(self.refresh_button)
        layout.addLayout(top_row)

        sel_row = QHBoxLayout()
        self.selected_label = QLabel("Selected profile: -")
        self.selected_label.setObjectName("dim")
        sel_row.addWidget(self.selected_label)
        self.set_selected_button = QPushButton("Set as selected")
        self.set_selected_button.clicked.connect(self._set_selected)
        sel_row.addWidget(self.set_selected_button)
        self.open_mo2_button = QPushButton("Open Mod Organizer")
        self.open_mo2_button.clicked.connect(self._open_mo2)
        sel_row.addWidget(self.open_mo2_button)
        sel_row.addStretch(1)
        layout.addLayout(sel_row)

        self.guard_label = QLabel(
            "Mod Organizer is running - close it before editing mods. "
            "Edits are disabled while it is open."
        )
        self.guard_label.setObjectName("warn")
        self.guard_label.hide()
        layout.addWidget(self.guard_label)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search mods...")
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(1)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemSelectionChanged.connect(self._update_count)
        layout.addWidget(self.tree, 1)

        btn_row = QHBoxLayout()
        self.enable_button = QPushButton("Enable")
        self.enable_button.clicked.connect(lambda: self._set_selected_mods(True))
        self.disable_button = QPushButton("Disable")
        self.disable_button.clicked.connect(lambda: self._set_selected_mods(False))
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete_selected_mods)
        self.move_up_button = QPushButton("Move Up")
        self.move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self.move_down_button = QPushButton("Move Down")
        self.move_down_button.clicked.connect(lambda: self._move_selected(1))
        for b in (
            self.enable_button,
            self.disable_button,
            self.delete_button,
            self.move_up_button,
            self.move_down_button,
        ):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        backup_row = QHBoxLayout()
        self.backup_status = QLabel("")
        self.backup_status.setObjectName("dim")
        backup_row.addWidget(self.backup_status, 1)
        self.restore_button = QPushButton("Restore backup")
        self.restore_button.clicked.connect(self._restore_backup)
        backup_row.addWidget(self.restore_button)
        layout.addLayout(backup_row)

        self.count_label = QLabel("")
        self.count_label.setObjectName("dim")
        layout.addWidget(self.count_label)

    # ----- data access -----
    def _active_profile(self):
        profile = self.window.settings.active_profile
        if profile is None:
            raise RuntimeError("No active profile")
        return profile

    def _modlist_path(self, mo2_profile: str) -> Path:
        gamma = self._active_profile().gamma
        return Path(gamma) / "profiles" / mo2_profile / "modlist.txt"

    def _backup_path(self, modlist: Path) -> Path:
        return modlist.with_name(modlist.name + BACKUP_SUFFIX)

    # ----- MO2 running guard -----
    def _mo2_running(self) -> bool:
        return mo2_running()

    def _update_guard(self) -> None:
        running = self._mo2_running()
        self.guard_label.setVisible(running)
        for widget in (
            self.tree,
            self.enable_button,
            self.disable_button,
            self.delete_button,
            self.move_up_button,
            self.move_down_button,
            self.restore_button,
        ):
            widget.setEnabled(not running)
        if running:
            self._update_count()

    # ----- load -----
    def refresh(self) -> None:
        self.window.refresh_settings()
        self._load_profiles()
        self._update_guard()

    def _load_profiles(self) -> None:
        """Query MO2 profiles off the GUI thread.

        Both queries shell out to the CLI; running them inline froze the window
        on every visit to this page.
        """
        if self._profiles_loading:
            return
        self._profiles_loading = True
        self.count_label.setText("Loading MO2 profiles...")
        task = BackgroundTask(_query_mo2_profiles, parent=self)
        task.result.connect(self._on_profiles_loaded)
        task.error.connect(self._on_profiles_error)
        self._profiles_task = task
        task.start()

    def _on_profiles_loaded(self, result: tuple[list[str], str]) -> None:
        self._profiles_loading = False
        names, selected = result
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        if names:
            self.profile_combo.addItems(names)
            active = self.window.settings.active_profile
            if active is not None and active.mo2_profile in names:
                self.profile_combo.setCurrentText(active.mo2_profile)
        self.profile_combo.blockSignals(False)
        self.selected_label.setText(f"Selected profile: {selected or '-'}")
        if not names:
            self.count_label.setText("No MO2 profiles found. Run a full install first.")
            self.tree.clear()
            return
        self._load_mods()

    def _on_profiles_error(self, message: str) -> None:
        self._profiles_loading = False
        self.selected_label.setText("Selected profile: -")
        self.count_label.setText(f"Could not list MO2 profiles: {message}")
        self.tree.clear()

    def _load_mods(self) -> None:
        mo2_profile = self.profile_combo.currentText()
        if not mo2_profile:
            return
        try:
            path = self._modlist_path(mo2_profile)
            self._lines = read_lines(path)
            self._backup_status_text(path)
        except Exception as exc:  # noqa: BLE001
            self._lines = []
            self.count_label.setText(f"Could not read modlist: {exc}")
            self.tree.clear()
            return
        self._populate_tree()
        self._update_count()

    def _backup_status_text(self, modlist: Path) -> None:
        bak = self._backup_path(modlist)
        if bak.is_file():
            # Shown to the user, so render in their local timezone explicitly.
            stamp = (
                datetime.fromtimestamp(bak.stat().st_mtime, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
            )
            self.backup_status.setText(f"Backup: {bak.name} ({stamp})")
        else:
            self.backup_status.setText("No backup yet")

    def _populate_tree(self) -> None:
        self._populating = True
        self.tree.blockSignals(True)
        self.tree.clear()
        for category, mods in grouped(self._lines):
            header = QTreeWidgetItem([category])
            header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            header.setForeground(0, QColor("#9fe96f"))
            font = header.font(0)
            font.setBold(True)
            header.setFont(0, font)
            self.tree.addTopLevelItem(header)
            for status, name, line_index in mods:
                item = QTreeWidgetItem([name])
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
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
                    QColor("#7f8f78") if status != "Enabled" else QColor("#e2ead8"),
                )
                header.addChild(item)
        self.tree.expandAll()
        self.tree.blockSignals(False)
        self._populating = False
        self._apply_filter()

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
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                item = header.child(j)
                total += 1
                if item.checkState(0) == Qt.CheckState.Checked:
                    enabled += 1
                if not item.isHidden():
                    visible += 1
        if self.search.text().strip():
            self.count_label.setText(
                f"{visible} matching of {total} mods ({enabled} enabled)"
            )
        else:
            self.count_label.setText(f"{total} mods ({enabled} enabled)")

    # ----- writes -----
    def _write_lines(self, new_lines: list[str]) -> bool:
        if self._mo2_running():
            self._update_guard()
            QMessageBox.warning(
                self,
                "Mod Organizer is running",
                "Close Mod Organizer first - it would overwrite your changes "
                "when it exits.",
            )
            return False
        mo2_profile = self.profile_combo.currentText()
        try:
            path = self._modlist_path(mo2_profile)
            if path.exists():
                bak = self._backup_path(path)
                if not bak.exists():
                    shutil.copy2(path, bak)
            save_lines(path, new_lines)
            self._lines = new_lines
            self._backup_status_text(path)
            return True
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Failed", str(exc))
            return False

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._populating or item.parent() is None:
            return
        line_index = item.data(0, Qt.ItemDataRole.UserRole)
        enabled = item.checkState(0) == Qt.CheckState.Checked
        new_lines = set_status_at(self._lines, line_index, enabled)
        if self._write_lines(new_lines):
            item.setForeground(
                0,
                QColor("#e2ead8") if enabled else QColor("#7f8f78"),
            )
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
    def _selected_mod_indexes(self) -> list[int]:
        indexes: list[int] = []
        for item in self.tree.selectedItems():
            if item.parent() is not None:
                indexes.append(item.data(0, Qt.ItemDataRole.UserRole))
        return indexes

    def _set_selected_mods(self, enabled: bool) -> None:
        indexes = self._selected_mod_indexes()
        if not indexes:
            return
        new_lines = list(self._lines)
        for idx in indexes:
            new_lines = set_status_at(new_lines, idx, enabled)
        if self._write_lines(new_lines):
            self._load_mods()

    def _delete_selected_mods(self) -> None:
        indexes = self._selected_mod_indexes()
        if not indexes:
            return
        answer = QMessageBox.question(
            self,
            "Delete Mods",
            f"Remove {len(indexes)} mod(s) from the modlist? This only edits "
            "the modlist; mod files are not deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        new_lines = list(self._lines)
        for idx in sorted(indexes, reverse=True):
            new_lines = delete_at(new_lines, idx)
        if self._write_lines(new_lines):
            self._load_mods()

    def _move_selected(self, delta: int) -> None:
        indexes = self._selected_mod_indexes()
        if len(indexes) != 1:
            return
        if not self._reorder_warned:
            answer = QMessageBox.question(
                self,
                "Reorder Mods",
                "Moving a mod changes the load order. An incorrect load order "
                "can break your save or the game.\n\nContinue?",
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

    def _set_selected(self) -> None:
        profile = self.profile_combo.currentText()
        if not profile:
            return
        rc, out = run_sync(
            ["mo2", "config", "set", "selected-profile", profile],
            timeout=_QUERY_TIMEOUT,
        )
        if rc == 0:
            self.selected_label.setText(f"Selected profile: {profile}")
        else:
            QMessageBox.warning(self, "Failed", out.strip() or "Could not set selected profile")

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
            launch_detached(command, env, cwd, log_path=logs_dir() / "launcher.log")
        except (LaunchError, RuntimeError) as exc:
            QMessageBox.warning(self, "Cannot launch MO2", str(exc))

    def _restore_backup(self) -> None:
        if self._mo2_running():
            self._update_guard()
            QMessageBox.warning(
                self,
                "Mod Organizer is running",
                "Close Mod Organizer first - it would overwrite your changes "
                "when it exits.",
            )
            return
        mo2_profile = self.profile_combo.currentText()
        try:
            path = self._modlist_path(mo2_profile)
            bak = self._backup_path(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Failed", str(exc))
            return
        if not bak.is_file():
            QMessageBox.information(self, "No backup", "No backup available yet.")
            return
        answer = QMessageBox.question(
            self,
            "Restore Backup",
            f"Restore the modlist from {bak.name}? Current edits will be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            shutil.copy2(bak, path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Failed", str(exc))
            return
        self._load_mods()
