"""The Play page's "Manage GE-Proton" window.

Installs new GE-Proton builds and lists the ones in Steam's
``compatibilitytools.d`` folders with their size. Every build is about
1.5 GB and installing one never removes the old ones, so this also removes
the unused ones. The build COMMANDER launches with cannot be removed.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .. import gui_settings
from ..i18n import tr
from ..integrity import format_size
from ..proton_installer import (
    ProtonBuild,
    build_size,
    fetch_ge_proton_releases,
    install_proton,
    installed_builds,
    remove_build,
)
from .common import BackgroundTask, info_label, mo2_running, section_label
from .dialogs import MessageBox as QMessageBox
from .dialogs import style_window


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


def release_installed(tag: str, names: set[str]) -> bool:
    """Newer builds unpack to "<tag>-x86_64"; older ones to "<tag>"."""
    return any(
        name.removesuffix("-x86_64").removesuffix("-aarch64") == tag for name in names
    )


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


class _ProtonVersionComboBox(QComboBox):
    """Keep the large Proton release popup within a usable screen height."""

    _MAX_POPUP_HEIGHT = 360

    def showPopup(self) -> None:
        super().showPopup()
        # Qt creates and sizes the popup after showPopup() starts. Apply the
        # limit on the next event-loop turn so Wayland cannot expand it again.
        QTimer.singleShot(0, self._limit_popup)

    def _limit_popup(self) -> None:
        popup = self.view().window()
        popup.setMaximumHeight(self._MAX_POPUP_HEIGHT)
        if popup.height() > self._MAX_POPUP_HEIGHT:
            popup.resize(popup.width(), self._MAX_POPUP_HEIGHT)


class _ProgressBridge(QObject):
    """Cross-thread signal bridge for download progress updates."""

    updated = Signal(int, str)


class ProtonManagerDialog(QDialog):
    def __init__(self, window, parent=None, *, on_changed=None, on_select=None) -> None:
        super().__init__(parent)
        self.window = window
        self._on_changed = on_changed
        self._on_select = on_select
        self._task: BackgroundTask | None = None
        self._size_task: BackgroundTask | None = None
        self._install_task: BackgroundTask | None = None
        self._cancel_event: threading.Event | None = None
        self._releases: list[dict] = []
        self._sizes: dict[str, int] = {}
        self._builds: list[ProtonBuild] = []
        self.setWindowTitle(tr("Manage GE-Proton"))
        self.setMinimumSize(640, 500)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        style_window(self, tr("Manage GE-Proton"))

        # -- install a new build --
        layout.addWidget(section_label(tr("Install GE-Proton"), level=2))
        install_row = QHBoxLayout()
        install_row.setSpacing(8)
        self.version_combo = _ProtonVersionComboBox()
        self.version_combo.setMinimumHeight(34)
        self.version_combo.setMaxVisibleItems(12)
        self.version_combo.view().setMaximumHeight(360)
        self.version_combo.view().setUniformItemSizes(True)
        self.version_combo.currentIndexChanged.connect(self._update_install_button)
        install_row.addWidget(self.version_combo, 1)
        self.install_button = QPushButton(tr("Install GE-Proton"))
        self.install_button.setObjectName("primary")
        self.install_button.setMinimumSize(200, 34)
        self.install_button.clicked.connect(self._install)
        install_row.addWidget(self.install_button)
        layout.addLayout(install_row)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(8)
        self.progress = QProgressBar()
        self.progress.setFixedHeight(22)
        progress_row.addWidget(self.progress, 1)
        self.cancel_button = QPushButton(tr("Cancel"))
        self.cancel_button.setObjectName("danger")
        self.cancel_button.setFixedSize(100, 28)
        self.cancel_button.setStyleSheet("padding: 0px;")
        self.cancel_button.clicked.connect(self._cancel_install)
        progress_row.addWidget(self.cancel_button)
        layout.addLayout(progress_row)
        self.install_status = info_label("")
        self.install_status.setObjectName("dim")
        layout.addWidget(self.install_status)
        self._hide_install_progress()

        # -- installed builds --
        layout.addSpacing(6)
        layout.addWidget(section_label(tr("Installed GE-Proton builds"), level=2))
        layout.addWidget(
            info_label(
                tr(
                    "Each build takes about 1.5 GB. The build COMMANDER launches the "
                    "game with cannot be removed; pick another runner first."
                )
                + " "
                + tr("Double-click a build to use it as the runner.")
            )
        )
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            [tr("Proton version"), tr("Size"), tr("In use")]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(self._use_build)
        layout.addWidget(self.table, 1)
        self.status = info_label("")
        self.status.setObjectName("dim")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.remove_button = QPushButton(tr("Remove Selected"))
        self.remove_button.setMinimumHeight(34)
        self.remove_button.clicked.connect(self._remove_selected)
        self.unused_button = QPushButton(tr("Remove All Unused"))
        self.unused_button.setMinimumHeight(34)
        self.unused_button.clicked.connect(self._remove_unused)
        self.close_button = QPushButton(tr("Close"))
        self.close_button.setMinimumSize(110, 34)
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.remove_button)
        buttons.addWidget(self.unused_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.reload()
        self._fetch_releases()

    # ----------------------------------------------------------- installing
    def _fetch_releases(self) -> None:
        def _done(result: object) -> None:
            self._releases = result if isinstance(result, list) else []
            self.version_combo.blockSignals(True)
            self.version_combo.clear()
            for rel in self._releases:
                self.version_combo.addItem(rel["tag"], rel["tag"])
            self.version_combo.blockSignals(False)
            self._update_install_button()

        task = BackgroundTask(lambda: fetch_ge_proton_releases(count=100), parent=self)
        task.result.connect(_done)
        task.error.connect(lambda _msg: self._update_install_button())
        task.start()

    def _update_install_button(self) -> None:
        if self._install_task is not None:
            return
        selected = self.version_combo.currentData() or ""
        idle = self._task is None and not self.window.install_busy
        if selected and release_installed(selected, {b.name for b in self._builds}):
            self.install_button.setText(tr("Already installed ✓"))
            self.install_button.setEnabled(False)
        elif selected:
            self.install_button.setText(tr("Install {selected}", selected=selected))
            self.install_button.setEnabled(idle)
        else:
            self.install_button.setText(tr("Install GE-Proton"))
            self.install_button.setEnabled(False)
        self.version_combo.setEnabled(idle)

    def _install(self) -> None:
        version = self.version_combo.currentData()
        if not version or self._install_task is not None or self._task is not None:
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Please wait"), tr("Another task is still running. Wait for it to finish, then try again."))
            return
        overrides = gui_settings.load_gui_settings().get("tool_overrides") or {}
        steam_root = overrides.get("steam_root", "")
        install_dir = (
            Path(steam_root) if steam_root else Path.home() / ".local" / "share" / "Steam"
        ) / "compatibilitytools.d"

        self.install_button.setEnabled(False)
        self.install_button.setText(tr("Installing…"))
        self.version_combo.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.install_status.setText(tr("Preparing download…"))
        self.install_status.setVisible(True)
        self.cancel_button.setText(tr("Cancel"))
        self.cancel_button.setEnabled(True)
        self.cancel_button.setVisible(True)
        cancel_event = self._cancel_event = threading.Event()
        self.window.set_install_busy(True)

        # A bound-method slot, not a lambda: progress arrives on the worker
        # thread, and only a QObject-bound slot is queued onto the GUI thread.
        bridge = _ProgressBridge(parent=self)
        bridge.updated.connect(self._on_progress)

        def _progress(downloaded: int, total: int) -> None:
            if total > 0:
                bridge.updated.emit(
                    int(downloaded * 100 / total),
                    tr(
                        "Downloading {version}... {done}/{total}",
                        version=version,
                        done=format_size(downloaded),
                        total=format_size(total),
                    ),
                )

        def _finish(text: str, delay_ms: int) -> None:
            self._install_task = None
            bridge.deleteLater()
            self.window.set_install_busy(False)
            self.cancel_button.setVisible(False)
            self.install_status.setText(text)
            self.reload()
            QTimer.singleShot(delay_ms, self._hide_install_progress)

        def _done(_result: object) -> None:
            self.progress.setValue(100)
            _finish(tr("Installed {version} ✓", version=version), 3000)
            if self._on_changed is not None:
                self._on_changed()

        def _fail(err: str) -> None:
            if err == "Download cancelled":
                _finish(tr("Download cancelled"), 5000)
            else:
                _finish(tr("Error: {err}", err=err), 5000)
                self.install_button.setText(tr("Retry install"))

        task = BackgroundTask(
            lambda: install_proton(
                version, install_dir, progress_cb=_progress, cancel_event=cancel_event
            ),
            parent=self,
        )
        self._install_task = task
        task.result.connect(_done)
        task.error.connect(_fail)
        self._update_buttons()
        task.start()

    def _on_progress(self, percent: int, text: str) -> None:
        self.progress.setValue(percent)
        self.install_status.setText(text)

    def _cancel_install(self) -> None:
        if self._cancel_event is None:
            return
        answer = QMessageBox.question(
            self,
            tr("Stop the download?"),
            tr("You can download it again later."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            labels={QMessageBox.StandardButton.Yes: tr("Stop download"), QMessageBox.StandardButton.No: tr("Keep downloading")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText(tr("Cancelling…"))

    def _hide_install_progress(self) -> None:
        if self._install_task is not None:
            return
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        self.install_status.setVisible(False)

    # ------------------------------------------------------- installed list
    def reload(self) -> None:
        self._builds = installed_builds()
        self.table.setRowCount(0)
        for build in self._builds:
            row = self.table.rowCount()
            self.table.insertRow(row)
            size = self._sizes.get(str(build.path))
            name_item = QTableWidgetItem(build.name)
            name_item.setData(Qt.ItemDataRole.UserRole, build)
            size_item = QTableWidgetItem(format_size(size) if size is not None else "…")
            use_item = QTableWidgetItem("✓" if build.in_use else "")
            use_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            for item in (name_item, size_item, use_item):
                item.setToolTip(build_status(build))
                if build.in_use:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, size_item)
            self.table.setItem(row, 2, use_item)
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

    def _use_build(self, item) -> None:
        build = self.table.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)
        if self._on_select is None or build.in_use or self._busy():
            return
        self._on_select(f"umup:{build.path / 'proton'}")
        self.reload()

    def _selected(self) -> list[ProtonBuild]:
        builds = (
            self.table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
            for index in self.table.selectionModel().selectedRows()
        )
        return [build for build in builds if build.removable]

    def _unused(self) -> list[ProtonBuild]:
        return [b for b in self._builds if b.removable and not b.steam_uses]

    def _busy(self) -> bool:
        return self._task is not None or self._install_task is not None

    def _update_buttons(self) -> None:
        idle = not self._busy() and not self.window.install_busy
        self.remove_button.setEnabled(idle and bool(self._selected()))
        self.unused_button.setEnabled(idle and bool(self._unused()))
        self.close_button.setEnabled(not self._busy())
        self._update_install_button()

    def closeEvent(self, event) -> None:
        if self._busy():
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if not self._busy():
            super().reject()

    def _remove_selected(self) -> None:
        self._confirm_remove(self._selected())

    def _remove_unused(self) -> None:
        self._confirm_remove(self._unused())

    def _can_remove(self) -> bool:
        if self._busy():
            return False
        if self.window.install_busy:
            QMessageBox.information(self, tr("Please wait"), tr("Another task is still running. Wait for it to finish, then try again."))
            return False
        if mo2_running(force=True):
            QMessageBox.information(
                self,
                tr("Close the game first"),
                tr("Mod Organizer or the game is still open. Close it, then try again."),
            )
            return False
        return True

    def _confirm_remove(self, builds: list[ProtonBuild]) -> None:
        if not builds or not self._can_remove():
            return
        warning = (
            tr(
                "Steam games set to use a removed build go back to Steam's "
                "default until you pick another one in their Properties."
            )
            if any(b.steam_uses for b in builds)
            else ""
        )
        answer = QMessageBox.question(
            self,
            tr("Delete these GE-Proton builds?"),
            tr("They're removed from your computer for good:"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            points=tuple(
                b.name + ("  (" + tr("Used by a Steam game") + ")" if b.steam_uses else "")
                for b in builds
            ),
            note=warning,
            labels={QMessageBox.StandardButton.Yes: tr("Delete"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        # The question was modal: an install or the game may have started
        # while it was open.
        if answer != QMessageBox.StandardButton.Yes or not self._can_remove():
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
            QMessageBox.warning(self, tr("Some builds couldn't be deleted"), "\n".join(map(str, errors)))
        if self._on_changed is not None:
            self._on_changed()
