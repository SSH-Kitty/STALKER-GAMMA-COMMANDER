"""Switch install: GAMMA in more than one folder, picked with the pad.

The desktop Install page's switcher, Deck-sized. Same list (``game_installs``
in gui-settings.json), same rules for reading it, and the same way of
pointing the active profile at another install - so an install added in
either interface shows up in both. Only the folders change; the runner and
launch options stay.

Copying an install off a Windows drive stays a desktop job (it needs a
second folder picked and a long copy watched). Here such an install can
still be used as it is, after a warning.
"""

from __future__ import annotations

from pathlib import Path

from commander_gui import gui_settings
from commander_gui.game_installs import (
    find_install,
    fix_windows_paths,
    mo2_profile_for,
    needs_fix,
    windows_drive_folder,
)
from commander_gui.i18n import tr
from commander_gui.ui.common import game_running, normalize_path, save_profile_dirs
from commander_gui.ui.install_page import InstallPage

from . import gamepad as pad
from .folder_picker import show_folder_picker
from .widgets import DeckOverlay, DeckPicker, DeckRow, deck_label


class InstallSwitcher(DeckRow):
    """A row naming the install in use; A opens the list of installs."""

    # The desktop's own reading of the list, so both agree on it.
    _install_key = staticmethod(InstallPage._install_key)
    _install_root = staticmethod(InstallPage._install_root)
    _known_installs = InstallPage._known_installs
    _is_current_install = InstallPage._is_current_install

    def __init__(self, window, on_switched) -> None:
        super().__init__(tr("Switch install"))
        self.window = window
        self._on_switched = on_switched
        self.activated.connect(self.open)

    def refresh(self) -> None:
        installs = self._known_installs()
        if installs and self._is_current_install(installs[0]):
            root = self._install_root(installs[0])
            self.set_value(tr("Playing: {name}", name=Path(root).name or root))
        else:
            self.set_value(tr("{count} saved in COMMANDER", count=len(installs)))

    def _locked(self) -> bool:
        if self.window.install_busy or game_running(force=True):
            self.window.notify(
                tr("An install or the game is running, so folder changes can't be saved right now. Try again when it's done."),
                6000,
            )
            return True
        return False

    def _save(self, installs) -> None:
        try:
            gui_settings.save_gui_settings(game_installs=[list(i) for i in installs])
        except OSError as exc:
            self.window.notify(tr("Couldn't save the install list: {exc}", exc=exc), 8000)

    # -- the list -----------------------------------------------------------
    def open(self) -> None:
        installs = self._known_installs()
        options = []
        for install in installs:
            root = self._install_root(install)
            mark = "●  " if self._is_current_install(install) else "○  "
            options.append((mark + root, install))
        picker = DeckPicker("", options, installs[0] if installs else None)
        picker.chosen.connect(self._chosen)
        overlay = DeckOverlay(
            tr("Switch install"),
            picker if options else deck_label(tr("No GAMMA install found"), wrap=True),
            [(tr("Cancel"), self.window.dismiss_overlay, "normal")],
            panel_width=1100,
            header_buttons=[("+  " + tr("Add install"), self._add, "normal")],
        )
        if options:
            overlay.default_button = picker.list
        overlay.hints = (("A", "Choose"), ("Y", "Add install"), ("B", "Cancel"))

        def _on_action(action: str) -> bool:
            if action == pad.SEARCH:
                self._add()
                return True
            return False

        overlay.action_handler = _on_action
        self.window.show_overlay(overlay)

    def _chosen(self, install: object) -> None:
        install = tuple(install)
        active = self._is_current_install(install)
        root = self._install_root(install)
        buttons = []
        if not active:
            buttons.append((tr("Play this"), lambda: self._play(install), "primary"))
            buttons.append((tr("Remove"), lambda: self._remove(install), "danger"))
        buttons.append((tr("Cancel"), self.window.dismiss_overlay, "normal"))
        lines = [
            tr("Anomaly folder:") + "  " + install[0],
            tr("GAMMA folder:") + "  " + install[1],
            tr("Cache folder:") + "  " + install[2],
        ]
        if active:
            lines.insert(0, tr("Playing"))
        else:
            lines.append("")
            lines.append(tr("Remove from this list. No files are deleted."))
        self.window.show_overlay(
            DeckOverlay(
                Path(root).name or root,
                deck_label("\n".join(lines), wrap=True),
                buttons,
                panel_width=1100,
                default_index=len(buttons) - 1 if active else 0,
            )
        )

    def _remove(self, install) -> None:
        self._save([i for i in self._known_installs() if i != install])
        self.refresh()
        self.open()

    def _add(self) -> None:
        def _picked(folder: Path) -> None:
            found = find_install(str(folder))
            if found is None:
                self.window.notify(
                    tr("Pick the folder that holds both the Anomaly folder and the GAMMA folder (the one with ModOrganizer.exe)."),
                    8000,
                )
                return
            install = self._install_key(found)
            installs = self._known_installs()
            if install not in installs:
                self._save([*installs, install])
            self.refresh()
            self.open()

        show_folder_picker(
            self.window,
            title=tr("Select a folder with a GAMMA install"),
            start=Path.home(),
            on_choose=_picked,
        )

    # -- playing from another install ---------------------------------------
    def _play(self, install) -> None:
        self.window.dismiss_overlay()
        if self._locked():
            return
        if windows_drive_folder(install[0], install[1]):
            self.window.confirm(
                tr("This folder is on a Windows drive"),
                tr("This install is on a Windows drive (NTFS). GAMMA can run slow, fail to save or not start from there."),
                lambda: self._fix_then_use(install),
                confirm_text=tr("Use it anyway"),
            )
            return
        self._fix_then_use(install)

    def _fix_then_use(self, install) -> None:
        if not needs_fix(install[0], install[1]):
            self._use(install)
            return

        def _fix() -> None:
            try:
                left = fix_windows_paths(install[0], install[1])
            except OSError as exc:
                self.window.notify(tr("ModOrganizer.ini could not be updated: {exc}", exc=exc), 8000)
                return
            info = ""
            if left:
                info = tr("These are on another Windows drive, so they were left as they are. Change them in Mod Organizer if you need them.")
            self._use(install, info)

        self.window.confirm(
            tr("Fix this install for Linux?"),
            tr("This GAMMA install was set up on Windows. COMMANDER changes its paths so it runs here, then plays from it.")
            + "\n\n•  " + tr("Only ModOrganizer.ini changes. Mods, saves and settings stay as they are.")
            + "\n•  " + tr("The old file is kept as ModOrganizer.ini.windows.bak."),
            _fix,
            confirm_text=tr("Fix and play"),
        )

    def _use(self, install, info: str = "") -> None:
        """Point the active profile at *install*, as the desktop page does."""
        if self._locked():
            return
        self.window.refresh_settings()
        profile = self.window.settings.active_profile
        if profile is None:
            return
        known = self._known_installs()
        # Each install keeps the MO2 profile it was last played with.
        old_mo2 = profile.mo2_profile
        remembered = gui_settings.load_gui_settings().get("install_mo2_profiles")
        remembered = {
            normalize_path(k): v
            for k, v in (remembered if isinstance(remembered, dict) else {}).items()
            if isinstance(k, str) and isinstance(v, str)
        }
        remembered[normalize_path(profile.gamma)] = old_mo2
        profile.mo2_profile = mo2_profile_for(install[1], remembered.get(normalize_path(install[1]), old_mo2))
        try:
            save_profile_dirs(self.window.settings, profile, *install)
        except OSError as exc:
            profile.mo2_profile = old_mo2
            self.window.notify(
                tr("COMMANDER couldn't write its settings file (settings.json).\n\n{exc}", exc=exc), 8000
            )
            return
        key = self._install_key(install)
        try:
            gui_settings.save_gui_settings(
                game_installs=[list(i) for i in [key, *(i for i in known if i != key)]],
                install_mo2_profiles=remembered,
            )
        except OSError as exc:
            self.window.notify(tr("Couldn't save the install list: {exc}", exc=exc), 8000)
        self.window.refresh_settings()
        self._on_switched()
        folder = self._install_root(install)
        message = "\n".join(
            filter(
                None,
                (
                    tr("The game now runs from {name}.", name=Path(folder).name or folder),
                    tr("Mod Organizer profile: {name}", name=profile.mo2_profile),
                    tr("Your runner and launch options stay the same."),
                    info,
                ),
            )
        )
        self.window.announce_finished(tr("Install switched"), message)
