"""The Utilities page's "Saves & Settings Backup" window.

Lists the active profile's backups (see ``commander_gui/game_backup.py``)
and offers Back Up Now, Restore, Delete and Open Backup Folder. Backups and
restores run on a StreamTask and hold the global install lock while they
do, so nothing can wipe or reinstall the folders underneath them.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..game_backup import (
    BackupError,
    BackupInfo,
    clean_name,
    create_backup,
    delete_backup,
    list_backups,
    profile_backup_dir,
    restore_backup,
)
from ..i18n import tr
from ..integrity import format_size
from .common import (
    StreamTask,
    info_label,
    mo2_running,
    open_in_file_manager,
    section_label,
)


def backup_summary(info: BackupInfo) -> str:
    """What a backup holds, translated."""
    parts = []
    if info.saves:
        parts.append(tr("{count} saves", count=info.saves))
    if info.user_ltx:
        parts.append("user.ltx")
    if info.mcm:
        parts.append(tr("MCM settings"))
    return ", ".join(parts) or tr("empty")


def backup_title(info: BackupInfo) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.created))
    # A player's own name is shown as typed; the automatic labels translate.
    return f"{when}  -  {info.name or tr(info.label)}"


class BackupDialog(QDialog):
    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self.window = window
        self._task: StreamTask | None = None
        self.setWindowTitle(tr("Saves & Settings Backup"))
        self.setMinimumSize(640, 460)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(section_label(tr("Saves & Settings Backup"), level=2))
        layout.addWidget(
            info_label(
                tr(
                    "Backs up your saves, user.ltx (options, controls, keybinds) and "
                    "MCM settings to a folder outside the game, so they survive a "
                    "reset or uninstall. One is also made automatically before every "
                    "Fresh Reset, GAMMA Reset and Full Uninstall."
                )
            )
        )
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._update_buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self._restore())
        layout.addWidget(self.list, 1)

        what = QHBoxLayout()
        what.addWidget(QLabel(tr("Restore:")))
        self.saves_box = QCheckBox(tr("Saves"))
        self.saves_box.setChecked(True)
        self.settings_box = QCheckBox(tr("user.ltx and MCM settings"))
        self.settings_box.setChecked(True)
        what.addWidget(self.saves_box)
        what.addWidget(self.settings_box)
        what.addStretch(1)
        layout.addLayout(what)

        self.status = info_label("")
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.backup_button = QPushButton(tr("Back Up Now"))
        self.backup_button.setObjectName("primary")
        self.backup_button.clicked.connect(self._backup)
        self.restore_button = QPushButton(tr("Restore"))
        self.restore_button.clicked.connect(self._restore)
        self.delete_button = QPushButton(tr("Delete"))
        self.delete_button.clicked.connect(self._delete)
        self.folder_button = QPushButton(tr("Open Backup Folder"))
        self.folder_button.clicked.connect(self._open_folder)
        self.close_button = QPushButton(tr("Close"))
        self.close_button.clicked.connect(self.close)
        for button in (self.backup_button, self.restore_button, self.delete_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.folder_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.reload()

    # -- state -------------------------------------------------------------
    def _profile(self):
        return self.window.settings.active_profile

    def reload(self) -> None:
        self.list.clear()
        profile = self._profile()
        if profile is None:
            self.status.setText(tr("Create or activate a profile first (Profiles page)."))
            self._update_buttons()
            return
        backups = list_backups(profile.profile_name)
        for info in backups:
            item = QListWidgetItem(
                f"{backup_title(info)}\n    {backup_summary(info)}  ·  {format_size(info.size)}"
            )
            item.setData(Qt.ItemDataRole.UserRole, info)
            self.list.addItem(item)
        if backups:
            self.list.setCurrentRow(0)
        if self._task is None:
            self.status.setText(
                tr("{count} backup(s) for profile '{name}'.", count=len(backups), name=profile.profile_name)
                if backups
                else tr("No backups yet for profile '{name}'.", name=profile.profile_name)
            )
        self._update_buttons()

    def _selected(self) -> BackupInfo | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _update_buttons(self, *_args) -> None:
        idle = self._task is None and not self.window.install_busy
        has_profile = self._profile() is not None
        selected = self._selected() is not None
        self.backup_button.setEnabled(idle and has_profile)
        self.restore_button.setEnabled(idle and selected)
        self.delete_button.setEnabled(idle and selected)
        self.close_button.setEnabled(self._task is None)

    def closeEvent(self, event) -> None:
        if self._task is not None:
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if self._task is None:
            super().reject()

    # -- actions -----------------------------------------------------------
    def _start(self, fn, on_done, busy_text: str) -> None:
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("Another task is already running."))
            return
        self.window.set_install_busy(True)
        self.status.setText(busy_text)
        task = StreamTask(fn, parent=self)
        self._task = task
        task.line.connect(self.status.setText)
        task.result.connect(lambda result: self._finish(on_done, result, None))
        task.error.connect(lambda message: self._finish(on_done, None, message))
        self._update_buttons()
        task.start()

    def _finish(self, on_done, result, error: str | None) -> None:
        self._task = None
        self.window.set_install_busy(False)
        self.reload()
        on_done(result, error)

    def _backup(self) -> None:
        profile = self._profile()
        if profile is None:
            return
        label, ok = QInputDialog.getText(
            self,
            tr("Back Up Now"),
            tr("Name this backup (optional):"),
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not ok:
            return
        label = clean_name(label)
        name, anomaly, gamma = profile.profile_name, profile.anomaly, profile.gamma

        def done(info, error) -> None:
            if error:
                QMessageBox.warning(self, tr("Backup failed"), error)
            elif info is None:
                self.status.setText(tr("No saves, user.ltx or MCM settings found - nothing to back up."))
            else:
                self.status.setText(tr("Backup saved: {path}", path=str(info.path)))

        self._start(
            lambda report: create_backup(
                name, anomaly, gamma, reason="manual", name=label, report=report
            ),
            done,
            tr("Backing up..."),
        )

    def _restore(self) -> None:
        info = self._selected()
        profile = self._profile()
        if info is None or profile is None or self._task is not None:
            return
        saves = self.saves_box.isChecked()
        settings = self.settings_box.isChecked()
        if not (saves or settings):
            QMessageBox.information(self, tr("Restore"), tr("Tick Saves, settings, or both."))
            return
        if mo2_running(force=True):
            QMessageBox.information(
                self,
                tr("Game Running"),
                tr("Mod Organizer / the game is currently running.\n\nClose it before running this action."),
            )
            return
        answer = QMessageBox.question(
            self,
            tr("Restore"),
            tr(
                "Restore {what} from the backup of {when} into profile '{name}'?\n\n"
                "Files with the same name are replaced. Nothing else is deleted, and "
                "the files being replaced are kept in a new \"Before restore\" backup.",
                what=backup_summary(info),
                when=backup_title(info),
                name=profile.profile_name,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        anomaly, gamma = profile.anomaly, profile.gamma

        def done(count, error) -> None:
            if error:
                QMessageBox.warning(self, tr("Restore failed"), error)
            else:
                self.status.setText(tr("Restored {count} files.", count=count))

        self._start(
            lambda report: restore_backup(
                info, anomaly, gamma, saves=saves, settings=settings, report=report
            ),
            done,
            tr("Restoring..."),
        )

    def _delete(self) -> None:
        info = self._selected()
        if info is None or self._task is not None:
            return
        answer = QMessageBox.question(
            self,
            tr("Delete"),
            tr("Permanently delete the backup of {when}?", when=backup_title(info)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_backup(info)
        except (BackupError, OSError) as exc:
            QMessageBox.warning(self, tr("Delete"), str(exc))
        self.reload()

    def _open_folder(self) -> None:
        profile = self._profile()
        if profile is None:
            return
        folder = profile_backup_dir(profile.profile_name)
        folder.mkdir(parents=True, exist_ok=True)
        open_in_file_manager(folder)
