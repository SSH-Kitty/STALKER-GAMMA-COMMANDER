"""The Updates page's Modded exes box: pick a release, install, go back."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QPushButton

from .. import coop, modded_exes
from .common import (
    BackgroundTask,
    NoWheelComboBox,
    StreamTask,
    game_running,
    mo2_running,
    tr,
)
from .dialogs import MessageBox as QMessageBox
from .multiplayer_page import _hint, _section


def _version_text(label: str | None) -> str:
    if label and label.startswith("file:"):
        return label[5:]
    return label or ""


class ModdedExesCard:
    """Builds the box; ``frame`` goes into the page's layout."""

    def __init__(self, window, on_installed) -> None:
        self.window = window
        self._on_installed = on_installed
        self._releases: list[dict] = []
        self._task: StreamTask | None = None
        self._list_task: BackgroundTask | None = None

        self.frame, layout = _section(tr("Modded exes"))
        layout.addWidget(_hint(tr("The game's engine (demonized's modded exes). GAMMA ships its own version; install a newer one here when a mod or modpack needs it.")))
        self.status = _hint()
        self.status.setObjectName("")
        layout.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.versions = NoWheelComboBox()
        self.versions.setMinimumWidth(260)
        self.versions.setFixedHeight(38)
        row.addWidget(self.versions, 1)
        self.install_button = self._button(tr("Install"), self._install_selected, "primary")
        self.file_button = self._button(tr("From a file…"), self._install_file)
        self.back_button = self._button("", self._go_back)
        self.restore_button = self._button(tr("Restore original exes"), self._restore)
        self.cancel_button = self._button(tr("Cancel"), lambda: self._task and self._task.cancel())
        for button in (self.install_button, self.file_button, self.back_button, self.restore_button, self.cancel_button):
            row.addWidget(button)
        layout.addLayout(row)
        note_row = QHBoxLayout()
        note_row.setSpacing(8)
        self.note = _hint()
        note_row.addWidget(self.note, 1)
        self.coop_button = self._button(tr("Open Multiplayer"), lambda: self.window.set_page("multiplayer"))
        note_row.addWidget(self.coop_button)
        layout.addLayout(note_row)
        self.refresh()

    @staticmethod
    def _button(text: str, slot, name: str = "secondary") -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(name)
        # Sits in a row with the primary Install: same height, no tall padding.
        button.setProperty("besidePrimary", True)
        button.setFixedHeight(38)
        button.setMinimumWidth(110)
        button.clicked.connect(slot)
        return button

    def _anomaly(self) -> str:
        profile = self.window.settings.active_profile
        return str(profile.anomaly).strip() if profile is not None else ""

    # --------------------------------------------------------------- state
    def refresh(self) -> None:
        anomaly = self._anomaly()
        state = modded_exes.load_state(anomaly) if anomaly else None
        if not anomaly:
            text = tr("Pick an Anomaly folder for this profile first.")
        elif state:
            text = tr("Installed: {version} (installed by COMMANDER)", version=_version_text(state.get("version")))
        elif built := modded_exes.build_date(anomaly):
            text = tr("Installed: exes built {date} (GAMMA's or your own)", date=built)
        else:
            text = tr("No engine exes found in the Anomaly folder.")
        self.status.setText(text)
        previous = (state or {}).get("previous")
        self.back_button.setText(tr("Go back to {version}", version=previous) if previous else "")
        self.back_button.setVisible(bool(previous) and not previous.startswith("file:"))
        self.restore_button.setVisible(bool(state))
        coop_on = bool(anomaly) and coop.is_active(anomaly)
        self.note.setText(
            tr("Co-op is on, so this is locked. Switch to singleplayer on the Multiplayer page, update the exes here, then switch co-op back on. Co-op keeps using its own engine.") if coop_on else ""
        )
        self.note.setVisible(coop_on)
        self.coop_button.setVisible(coop_on)
        busy = self._task is not None
        idle = not busy and not self.window.install_busy and bool(anomaly) and not coop_on
        for button in (self.file_button, self.back_button, self.restore_button):
            button.setEnabled(idle)
        self.install_button.setEnabled(idle and self.versions.currentData() is not None)
        self.versions.setEnabled(idle)
        self.cancel_button.setVisible(busy)
        if not self._releases and self._list_task is None:
            self._load_releases()

    def _load_releases(self) -> None:
        self.versions.clear()
        self.versions.addItem(tr("Loading releases…"), None)
        self._list_task = BackgroundTask(modded_exes.fetch_releases)
        self._list_task.result.connect(self._on_releases)
        self._list_task.error.connect(self._on_releases_error)
        self._list_task.start()

    def _on_releases(self, releases: list[dict]) -> None:
        self._releases = releases
        self.versions.clear()
        for release in releases:
            text = f"{release['tag']} · {release['published']}"
            if release["tag"] == modded_exes.RECOMMENDED:
                text += " · " + tr("recommended")
            self.versions.addItem(text, release)
        self._list_task = None
        self.refresh()

    def _on_releases_error(self, message: str) -> None:
        self.versions.clear()
        self.versions.addItem(tr("Couldn't load releases. Check your connection."), None)
        self._list_task = None
        self._releases = []
        self.refresh()

    # ------------------------------------------------------------- actions
    def _blocked(self) -> bool:
        if mo2_running() or game_running():
            QMessageBox.warning(
                self.frame,
                tr("Close the game first"),
                tr("Close Mod Organizer and the game, then try again."),
            )
            return True
        return False

    def _confirm(self, version: str) -> bool:
        first = modded_exes.load_state(self._anomaly()) is None
        answer = QMessageBox.question(
            self.frame,
            tr("Install modded exes {version}?", version=version),
            tr("COMMANDER replaces Anomaly's engine exes and, as modpack guides say, clears the old version's loose files from Anomaly's gamedata and db/mods folders."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            info=tr("The current exes and those files are saved first, so Restore original exes can put them back.") if first else "",
            note="" if version == modded_exes.RECOMMENDED else tr("This version hasn't been tested with COMMANDER yet."),
            labels={QMessageBox.StandardButton.Yes: tr("Install"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        return answer == QMessageBox.StandardButton.Yes

    def _install_selected(self) -> None:
        release = self.versions.currentData()
        if release and not self._blocked() and self._confirm(release["tag"]):
            self._run_install(release=release)

    def _go_back(self) -> None:
        previous = (modded_exes.load_state(self._anomaly()) or {}).get("previous")
        release = next((r for r in self._releases if r["tag"] == previous), None)
        if release is None:
            QMessageBox.warning(
                self.frame,
                tr("Can't find that version"),
                tr("{version} isn't in the release list right now. Check your connection and try again.", version=previous),
            )
            return
        if not self._blocked() and self._confirm(release["tag"]):
            self._run_install(release=release)

    def _install_file(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self.frame, tr("Pick a modded exes zip"), str(Path.home()), tr("Zip files") + " (*.zip)"
        )
        if name and not self._blocked() and self._confirm(Path(name).name):
            self._run_install(zip_path=Path(name))

    def _run_install(self, release: dict | None = None, zip_path: Path | None = None) -> None:
        anomaly = Path(self._anomaly())
        label = release["tag"] if release else f"file:{zip_path.name}"
        cancel_event = None

        def worker(report):
            download_dir = None
            try:
                path = zip_path
                if release is not None:
                    download_dir = Path(tempfile.mkdtemp(prefix=".exes-download-", dir=anomaly))
                    path = modded_exes.download(
                        release, download_dir,
                        lambda done, total: report(
                            tr("Downloading {version}… {percent}%", version=label, percent=done * 100 // total)
                            if total else tr("Downloading {version}…", version=label)
                        ),
                        cancel_event,
                    )
                report(tr("Installing {version}…", version=_version_text(label)))
                modded_exes.install(path, anomaly, label, cancel_event)
            finally:
                if download_dir is not None:
                    shutil.rmtree(download_dir, ignore_errors=True)

        self._start(worker, tr("Modded exes {version} installed.", version=_version_text(label)), installed=True)
        cancel_event = self._task.cancel_event

    def _restore(self) -> None:
        if self._blocked():
            return
        answer = QMessageBox.question(
            self.frame,
            tr("Restore the original exes?"),
            tr("COMMANDER puts back the exes and files that were there before it first installed modded exes."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            labels={QMessageBox.StandardButton.Yes: tr("Restore"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        if answer == QMessageBox.StandardButton.Yes:
            anomaly = self._anomaly()
            self._start(lambda report: modded_exes.restore_original(anomaly), tr("The original exes are back."), installed=False)

    def _start(self, worker, done: str, installed: bool) -> None:
        self.window.set_install_busy(True, "modded_exes")
        self._task = StreamTask(worker)
        self._task.line.connect(self.status.setText)
        self._task.result.connect(lambda _r: self._finished(done, installed))
        self._task.error.connect(self._failed)
        self._task.start()
        self.refresh()

    def _finished(self, done: str, installed: bool) -> None:
        self._end()
        if installed:
            self._on_installed()
        QMessageBox.information(self.frame, tr("Done"), done)

    def _failed(self, message: str) -> None:
        self._end()
        QMessageBox.warning(self.frame, tr("The modded exes change didn't finish"), message)

    def _end(self) -> None:
        self._task = None
        if self.window.install_operation == "modded_exes":
            self.window.set_install_busy(False)
        self.refresh()
