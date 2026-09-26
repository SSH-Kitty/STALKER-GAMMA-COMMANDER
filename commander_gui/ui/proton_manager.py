"""The Play page's "Manage GE-Proton" window.

Every GE-Proton build is about 1.5 GB, and each "Install GE-Proton" adds
another without removing the old one. This lists the builds in Steam's
``compatibilitytools.d`` folders with their size, marks the one COMMANDER
launches with (which cannot be removed) and the ones Steam games are set to
use, and removes the rest.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .. import gui_settings
from ..i18n import tr
from ..integrity import format_size
from ..proton_installer import ProtonBuild, build_size, installed_builds, remove_build
from .common import BackgroundTask, info_label, mo2_running, section_label


def build_status(build: ProtonBuild) -> str:
    notes = []
    if build.in_use:
        notes.append(tr("In use by COMMANDER"))
    if build.steam_uses:
        notes.append(tr("Used by a Steam game"))
    if build.newest:
        notes.append(tr("Newest"))
    if not (build.in_use or build.steam_uses):
        notes.append(tr("Unused"))
    return ", ".join(notes)


def _sizes(builds: list[ProtonBuild]) -> dict[str, int]:
    return {str(build.path): build_size(build.path) for build in builds}


def _remove(builds: list[ProtonBuild], runner_kind: str) -> list[str]:
    errors = []
    for build in builds:
        try:
            remove_build(build.path, runner_kind)
        except (OSError, ValueError) as exc:
            errors.append(f"{build.name}: {exc}")
    return errors


class ProtonManagerDialog(QDialog):
    def __init__(self, window, parent=None, *, on_changed=None) -> None:
        super().__init__(parent)
        self.window = window
        self._on_changed = on_changed
        self._task: BackgroundTask | None = None
        self._size_task: BackgroundTask | None = None
        self._sizes: dict[str, int] = {}
        self._builds: list[ProtonBuild] = []
        self.setWindowTitle(tr("Manage GE-Proton"))
        self.setMinimumSize(620, 400)
        layout = QVBoxLayout(self)
        layout.addWidget(section_label(tr("Installed GE-Proton builds"), level=2))
        layout.addWidget(
            info_label(
                tr(
                    "Each build takes about 1.5 GB. The build COMMANDER launches the "
                    "game with cannot be removed; pick another runner first."
                )
            )
        )
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.list, 1)
        self.status = info_label("")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.remove_button = QPushButton(tr("Remove Selected"))
        self.remove_button.clicked.connect(self._remove_selected)
        self.unused_button = QPushButton(tr("Remove All Unused"))
        self.unused_button.clicked.connect(self._remove_unused)
        self.close_button = QPushButton(tr("Close"))
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.remove_button)
        buttons.addWidget(self.unused_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        self._builds = installed_builds()
        self.list.clear()
        for build in self._builds:
            size = self._sizes.get(str(build.path))
            size_text = format_size(size) if size is not None else "…"
            item = QListWidgetItem(f"{build.name}    {size_text}\n    {build_status(build)}")
            item.setData(Qt.ItemDataRole.UserRole, build)
            if build.in_use:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.list.addItem(item)
        if not self._builds:
            self.status.setText(tr("No GE-Proton builds found in compatibilitytools.d."))
        elif not self._unused():
            self.status.setText(
                tr("{count} build(s), none unused.", count=len(self._builds))
            )
        else:
            known = [self._sizes.get(str(b.path)) for b in self._unused()]
            freeable = sum(size for size in known if size)
            self.status.setText(
                tr(
                    "{count} build(s). Removing every unused one frees {size}.",
                    count=len(self._builds),
                    size=format_size(freeable) if None not in known else "…",
                )
            )
        self._update_buttons()
        if self._builds and self._size_task is None and any(
            str(b.path) not in self._sizes for b in self._builds
        ):
            task = BackgroundTask(_sizes, list(self._builds), parent=self)
            self._size_task = task
            task.result.connect(self._on_sizes)
            task.error.connect(lambda _msg: setattr(self, "_size_task", None))
            task.start()

    def _on_sizes(self, sizes: object) -> None:
        self._size_task = None
        self._sizes.update(sizes if isinstance(sizes, dict) else {})
        if self._task is None:
            self.reload()

    def _selected(self) -> list[ProtonBuild]:
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.list.selectedItems()
            if item.data(Qt.ItemDataRole.UserRole).removable
        ]

    def _unused(self) -> list[ProtonBuild]:
        return [b for b in self._builds if b.removable and not b.steam_uses]

    def _update_buttons(self) -> None:
        idle = self._task is None and not self.window.install_busy
        self.remove_button.setEnabled(idle and bool(self._selected()))
        self.unused_button.setEnabled(idle and bool(self._unused()))
        self.close_button.setEnabled(self._task is None)

    def closeEvent(self, event) -> None:
        if self._task is not None:
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if self._task is None:
            super().reject()

    def _remove_selected(self) -> None:
        self._confirm_remove(self._selected())

    def _remove_unused(self) -> None:
        self._confirm_remove(self._unused())

    def _confirm_remove(self, builds: list[ProtonBuild]) -> None:
        if not builds or self._task is not None:
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("Another task is already running."))
            return
        if mo2_running(force=True):
            QMessageBox.information(
                self,
                tr("Game Running"),
                tr("Mod Organizer / the game is currently running.\n\nClose it before running this action."),
            )
            return
        names = "\n".join(
            f"  {b.name}" + ("  (" + tr("Used by a Steam game") + ")" if b.steam_uses else "")
            for b in builds
        )
        warning = (
            "\n\n"
            + tr(
                "Steam games set to use a removed build fall back to Steam's "
                "default until you pick another one in their Properties."
            )
            if any(b.steam_uses for b in builds)
            else ""
        )
        answer = QMessageBox.question(
            self,
            tr("Remove GE-Proton"),
            tr("Permanently delete these builds?\n\n{names}", names=names) + warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.window.set_install_busy(True)
        self.status.setText(tr("Removing..."))
        # Read on this thread: the settings cache is not for worker threads.
        runner_kind = gui_settings.load_gui_settings().get("runner") or "auto"
        task = BackgroundTask(_remove, builds, runner_kind, parent=self)
        self._task = task
        task.result.connect(self._on_removed)
        task.error.connect(lambda message: self._on_removed([message]))
        self._update_buttons()
        task.start()

    def _on_removed(self, errors: object) -> None:
        self._task = None
        self.window.set_install_busy(False)
        self.reload()
        if errors:
            QMessageBox.warning(self, tr("Remove GE-Proton"), "\n".join(map(str, errors)))
        if self._on_changed is not None:
            self._on_changed()
