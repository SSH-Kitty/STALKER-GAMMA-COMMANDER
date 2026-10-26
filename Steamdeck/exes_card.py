"""The Update screen's Modded exes card: pick a release, install, go back.

The desktop Updates page's ModdedExesCard (commander_gui/ui/exes_card.py)
with Deck rows and overlays in place of its combo box and message boxes.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject

from commander_gui import coop, modded_exes
from commander_gui.i18n import tr
from commander_gui.ui.common import (
    BackgroundTask,
    StreamTask,
    game_running,
    mo2_running,
)
from commander_gui.ui.exes_card import _version_text

from .widgets import (
    DeckCard,
    DeckPicker,
    DeckRow,
    deck_button,
    deck_label,
    picker_overlay,
)


class DeckExesCard(QObject):
    """``card`` goes into the screen's layout."""

    def __init__(self, screen) -> None:
        super().__init__(screen)
        self.screen = screen
        self.window = screen.window
        self._releases: list[dict] = []
        self._release: dict | None = None
        self._task: StreamTask | None = None
        self._list_task: BackgroundTask | None = None

        self.card = DeckCard()
        body = self.card.body
        body.addWidget(deck_label(tr("Modded exes"), role="rowTitle"))
        body.addWidget(deck_label(tr("The game's engine (demonized's modded exes). GAMMA ships its own version; install a newer one here when a mod or modpack needs it."), role="caption", wrap=True))
        self.status = deck_label("", role="body", wrap=True)
        body.addWidget(self.status)
        self.version_row = DeckRow(tr("Version"))
        self.version_row.activated.connect(self._pick_version)
        body.addWidget(self.version_row)
        self.install_button = deck_button(tr("Install"), role="primary", on_click=self._install_selected)
        self.file_button = deck_button(tr("From a file…"), on_click=self._install_file)
        self.back_button = deck_button("", on_click=self._go_back)
        self.restore_button = deck_button(tr("Restore original exes"), on_click=self._restore)
        self.cancel_button = deck_button(tr("Cancel"), on_click=lambda: self._task and self._task.cancel())
        self.note = deck_label(tr("Co-op is on, so this is locked. Switch to singleplayer on the Multiplayer page, update the exes here, then switch co-op back on. Co-op keeps using its own engine."), role="caption", wrap=True)
        self.coop_button = deck_button(tr("Open Multiplayer"), on_click=lambda: self.window.set_page("multiplayer"))
        for widget in (self.install_button, self.file_button, self.back_button, self.restore_button, self.cancel_button, self.note, self.coop_button):
            body.addWidget(widget)

    def _anomaly(self) -> str:
        profile = self.screen.profile()
        return str(profile.anomaly).strip() if profile is not None else ""

    # --------------------------------------------------------------- state
    def refresh(self) -> None:
        anomaly = self._anomaly()
        state = modded_exes.load_state(anomaly) if anomaly else None
        if self._task is None:
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
        self.note.setVisible(coop_on)
        self.coop_button.setVisible(coop_on)
        busy = self._task is not None
        idle = not busy and not self.window.install_busy and bool(anomaly) and not coop_on
        for widget in (self.file_button, self.back_button, self.restore_button, self.version_row):
            widget.setEnabled(idle)
        self.install_button.setEnabled(idle and self._release is not None)
        self.cancel_button.setVisible(busy)
        if self._release is not None:
            self.version_row.set_value(self._release_text(self._release))
        elif self._list_task is not None:
            self.version_row.set_value(tr("Loading releases…"))
        if not self._releases and self._list_task is None:
            self._load_releases()

    @staticmethod
    def _release_text(release: dict) -> str:
        text = f"{release['tag']} · {release['published']}"
        if release["tag"] == modded_exes.RECOMMENDED:
            text += " · " + tr("recommended")
        return text

    def _load_releases(self) -> None:
        self.version_row.set_value(tr("Loading releases…"))
        self._list_task = BackgroundTask(modded_exes.fetch_releases, parent=self)
        self._list_task.result.connect(self._on_releases)
        self._list_task.error.connect(self._on_releases_error)
        self._list_task.start()

    def _on_releases(self, releases: list[dict]) -> None:
        self._releases = releases
        self._release = releases[0] if releases else None
        self._list_task = None
        self.refresh()

    def _on_releases_error(self, _message: str) -> None:
        self._list_task = None
        self.version_row.set_value(tr("Couldn't load releases. Check your connection."))
        self.refresh()

    def _pick_version(self) -> None:
        if not self._releases:
            self._load_releases()
            return
        picker = DeckPicker("", [(self._release_text(r), r["tag"]) for r in self._releases],
                            self._release["tag"] if self._release else None)

        def _chosen(tag: object) -> None:
            self.window.dismiss_overlay()
            self._release = next((r for r in self._releases if r["tag"] == tag), self._release)
            self.refresh()

        picker.chosen.connect(_chosen)
        self.window.show_overlay(picker_overlay(self.window, tr("Version"), picker))

    # ------------------------------------------------------------- actions
    def _blocked(self) -> bool:
        if mo2_running() or game_running():
            self.window.notify(tr("Close Mod Organizer and the game, then try again."), 6000)
            return True
        return False

    def _confirm(self, version: str, then) -> None:
        message = tr("COMMANDER replaces Anomaly's engine exes and, as modpack guides say, clears the old version's loose files from Anomaly's gamedata and db/mods folders.")
        if modded_exes.load_state(self._anomaly()) is None:
            message += "\n\n" + tr("The current exes and those files are saved first, so Restore original exes can put them back.")
        if version != modded_exes.RECOMMENDED:
            message += "\n\n" + tr("This version hasn't been tested with COMMANDER yet.")
        self.window.confirm(tr("Install modded exes {version}?", version=version), message, then, confirm_text=tr("Install"))

    def _install_selected(self) -> None:
        release = self._release
        if release and not self._blocked():
            self._confirm(release["tag"], lambda: self._run_install(release=release))

    def _go_back(self) -> None:
        previous = (modded_exes.load_state(self._anomaly()) or {}).get("previous")
        release = next((r for r in self._releases if r["tag"] == previous), None)
        if release is None:
            self.window.notify(tr("{version} isn't in the release list right now. Check your connection and try again.", version=previous), 6000)
            return
        if not self._blocked():
            self._confirm(release["tag"], lambda: self._run_install(release=release))

    def _install_file(self) -> None:
        from .mod_installer import pick_file

        downloads = Path.home() / "Downloads"

        def _picked(path: Path) -> None:
            if not self._blocked():
                self._confirm(path.name, lambda: self._run_install(zip_path=path))

        pick_file(self.window, tr("Pick a modded exes zip"), downloads if downloads.is_dir() else Path.home(), (".zip",), _picked)

    def _run_install(self, release: dict | None = None, zip_path: Path | None = None) -> None:
        anomaly = Path(self._anomaly())
        label = release["tag"] if release else f"file:{zip_path.name}"

        def worker(report):
            cancel_event = self._task.cancel_event  # set before the task starts
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

        self._start(worker, tr("Modded exes {version} installed.", version=_version_text(label)))

    def _restore(self) -> None:
        if self._blocked():
            return
        anomaly = self._anomaly()
        self.window.confirm(
            tr("Restore the original exes?"),
            tr("COMMANDER puts back the exes and files that were there before it first installed modded exes."),
            lambda: self._start(lambda report: modded_exes.restore_original(anomaly), tr("The original exes are back.")),
            confirm_text=tr("Restore"),
        )

    def _start(self, worker, done: str) -> None:
        self.window.set_install_busy(True, "modded_exes")
        self._task = StreamTask(worker, parent=self)
        self._task.line.connect(self.status.setText)
        self._task.result.connect(lambda _r: self._end(done, ok=True))
        self._task.error.connect(lambda message: self._end(message, ok=False))
        self._task.start()
        self.refresh()

    def _end(self, message: str, *, ok: bool) -> None:
        self._task = None
        if self.window.install_operation == "modded_exes":
            self.window.set_install_busy(False)
        self.refresh()
        self.window.announce_finished(
            tr("Done") if ok else tr("The modded exes change didn't finish"), message, ok=ok
        )
