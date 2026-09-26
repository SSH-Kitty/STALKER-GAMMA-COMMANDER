"""Profiles, as a panel on the Dashboard.

There used to be a Profile tab. What it held - switch to another profile,
edit this one, start a new one - is a handful of rows, and the Dashboard
already had an "Active profile" row pointing at it, so it now lives there
instead: one less tab, and the profile you are about to play with is on the
screen you look at first.

The active profile is one row; A on it opens a list of every profile to
switch to (a dropdown, rather than every profile stacked on the Dashboard).
Edit profile opens the editor, where the name and the three install folders - Anomaly,
GAMMA, cache - are changed with the on-screen keyboard and Deck Mode's own
folder browser. That is what the desktop Profiles page's Browse buttons are
for: pointing COMMANDER at an install on another drive, or at a second copy
of the game. Repository URLs and branches stay a desktop job.

Saving goes through the same ``save_profile`` the desktop page uses
(``commander_gui/ui/profiles_page.py``), so the two can never disagree about
what an edited profile looks like on disk.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

from commander_gui.i18n import tr
from commander_gui.ui.common import activate_profile, game_running, mo2_running
from commander_gui.ui.profiles_page import check_cli_values, save_profile

from . import gamepad as pad
from .folder_picker import show_folder_picker
from .scale import px
from .widgets import (
    ROW_GAP,
    DeckOverlay,
    DeckPicker,
    DeckRow,
    deck_label,
    repolish,
)


def _shorten(path: str, limit: int = 46) -> str:
    """Keep the end of a long path - the part that differs between installs."""
    return path if len(path) <= limit else "…" + path[-(limit - 1):]


class ProfileEditor(QWidget):
    """The editor overlay's body: a name field and three folder rows."""

    FIELDS = (
        ("anomaly", "STALKER Anomaly folder"),
        ("gamma", "GAMMA folder"),
        ("cache", "Download cache folder"),
    )

    def __init__(self, window, profile) -> None:
        super().__init__()
        self.window = window
        self.paths = {key: getattr(profile, key) or "" for key, _ in self.FIELDS}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(10))
        layout.addWidget(deck_label(tr("Profile name"), role="caption"))
        self.name_edit = QLineEdit(profile.profile_name or "")
        self.name_edit.setPlaceholderText(tr("Profile name"))
        self.name_edit.setMaxLength(40)
        layout.addWidget(self.name_edit)
        self.rows: dict[str, DeckRow] = {}
        for key, label in self.FIELDS:
            row = DeckRow(tr(label), _shorten(self.paths[key]) or tr("Not set"))
            row.activated.connect(lambda k=key: self._browse(k))
            layout.addWidget(row)
            self.rows[key] = row
        layout.addWidget(
            deck_label(
                tr(
                    "Point these at an existing install to use it, or at empty "
                    "folders to install a fresh copy there."
                ),
                role="caption",
                wrap=True,
            )
        )

    def _browse(self, key: str) -> None:
        current = Path(self.paths[key]).expanduser() if self.paths[key] else None
        start = None
        if current is not None:
            start = current if current.is_dir() else current.parent
            if not start.is_dir():
                start = None
        show_folder_picker(
            self.window,
            title=tr(dict(self.FIELDS)[key]),
            start=start,
            on_choose=lambda path, k=key: self.set_path(k, str(path)),
            stacked=True,
        )

    def set_path(self, key: str, path: str) -> None:
        self.paths[key] = path
        self.rows[key].set_value(_shorten(path))


class ProfilePanel(QWidget):
    """The active profile as one row; A opens the switcher.

    A plain row-height line like the Dashboard's other rows. Edit and New
    live in the switcher's title bar rather than beside this row: they are
    profile management, which is what that panel is for.
    """

    def __init__(self, window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self._task = None
        self._editor: ProfileEditor | None = None
        self._editing_name: str | None = None
        line = QHBoxLayout(self)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(px(ROW_GAP))
        self.chooser = DeckRow(tr("Active profile"))
        self.chooser.activated.connect(self.choose_profile)
        line.addWidget(self.chooser, 1)

    # -- rendering --------------------------------------------------------
    def refresh(self) -> None:
        busy = self.window.install_busy
        name = self._active_name()
        self.chooser.set_value((name or tr("No Profile")) + "   ▾")
        self.chooser.value_label.setObjectName("deckRowTitleAccent" if name else "deckRowValue")
        repolish(self.chooser.value_label)
        # With no profiles, A on the row starts a new one (choose_profile).
        self.chooser.setEnabled(not busy)

    def _active_name(self) -> str | None:
        active = self.window.settings.active_profile
        return active.profile_name if active is not None else None

    def choose_profile(self) -> None:
        """The dropdown: every profile, the active one pre-selected."""
        profiles = self.window.settings.profiles
        if not profiles:
            self.new_profile()
            return
        active = self._active_name()
        picker = DeckPicker(
            "",
            [
                (("●  " if p.profile_name == active else "○  ") + p.profile_name, p.profile_name)
                for p in profiles
            ],
            active,
        )
        picker.chosen.connect(self._on_chosen)
        overlay = DeckOverlay(
            tr("Switch profile"),
            picker,
            [(tr("Cancel"), self.window.dismiss_overlay, "normal")],
            panel_width=1000,
            header_buttons=[
                (tr("Edit"), self._edit_from_picker, "normal"),
                ("+  " + tr("New"), self._new_from_picker, "normal"),
            ],
        )
        # Straight onto the list, on the active profile; Edit and New sit
        # up in the title row, so they get face buttons of their own.
        overlay.default_button = picker.list
        overlay.hints = (("A", "Switch"), ("X", "Edit"), ("Y", "New"), ("B", "Cancel"))

        def _on_action(action: str) -> bool:
            if action == pad.CONTEXT:
                self._edit_from_picker()
                return True
            if action == pad.SEARCH:
                self._new_from_picker()
                return True
            return False

        overlay.action_handler = _on_action
        self.window.show_overlay(overlay)

    def _edit_from_picker(self) -> None:
        self.window.dismiss_overlay(refocus=False)
        self.open_editor(self._active_name())

    def _new_from_picker(self) -> None:
        self.window.dismiss_overlay()
        self.new_profile()

    def _on_chosen(self, name: object) -> None:
        self.window.dismiss_overlay()
        if name and name != self._active_name():
            self._confirm_switch(str(name))

    def new_profile(self) -> None:
        from .window import SETUP_KEY

        if self.window.install_busy:
            self.window.notify(tr("An install is already running."))
            return
        self.window.set_page(SETUP_KEY)

    # -- editing ----------------------------------------------------------
    def open_editor(self, name: str | None) -> None:
        profile = next(
            (p for p in self.window.settings.profiles if p.profile_name == name), None
        )
        if profile is None:
            return
        if self.window.install_busy:
            self.window.notify(tr("An install is already running."))
            return
        self._editing_name = name
        self._editor = ProfileEditor(self.window, profile)
        overlay = DeckOverlay(
            tr("Edit profile"),
            self._editor,
            [
                (tr("Save"), self._save, "primary"),
                (tr("Cancel"), self.window.dismiss_overlay, "normal"),
            ],
            panel_width=1100,
            default_index=1,
        )
        overlay.default_button = self._editor.name_edit
        self.window.show_overlay(overlay)

    def _save(self) -> None:
        editor, original = self._editor, self._editing_name
        if editor is None or original is None:
            return
        name = editor.name_edit.text().strip()
        if not name:
            self.window.notify(tr("A profile name is required."))
            return
        if any(
            p.profile_name == name and p.profile_name != original
            for p in self.window.settings.profiles
        ):
            self.window.notify(tr("A profile named '{name}' already exists.", name=name))
            return
        if not all(editor.paths.values()):
            self.window.notify(tr("Anomaly, GAMMA, and cache folders are required."))
            return
        if self.window.install_busy:
            self.window.notify(tr("An install is already running."))
            return
        if mo2_running(force=True):
            self.window.notify(
                tr("Mod Organizer / the game is currently running. Close it first.")
            )
            return
        existing = next(
            (p for p in self.window.settings.profiles if p.profile_name == original), None
        )
        if existing is None:
            return
        edited = dataclasses.replace(
            existing,
            profile_name=name,
            anomaly=editor.paths["anomaly"],
            gamma=editor.paths["gamma"],
            cache=editor.paths["cache"],
            extra=dict(existing.extra),
        )
        try:
            check_cli_values(edited)
        except ValueError as exc:
            self.window.notify(str(exc), 6000)
            return
        try:
            save_profile(self.window.settings, original, edited)
        except OSError as exc:
            self.window.notify(
                tr("Could not write settings.json:\n{exc}", exc=exc), 8000
            )
            return
        self.window.dismiss_overlay()
        self._editor = None
        self.window.refresh_settings()
        # Every screen reads the profile's folders; bring them all up to date.
        for page in list(self.window._pages.values()):
            page.refresh()
        self.window.notify(tr("Profile '{profile_name}' saved.", profile_name=name))

    # -- switching --------------------------------------------------------
    def _confirm_switch(self, name: str) -> None:
        if self._task is not None:
            # One switch at a time: two overlapping `config use` runs made the
            # first to finish report failure against the other's result.
            self.window.notify(tr("Switching profile..."))
            return
        if self.window.install_busy:
            self.window.notify(tr("An install is already running."))
            return
        active = self.window.settings.active_profile
        if game_running(force=True):
            # MO2 or a game started without it (Play Anomaly): the same
            # question the desktop Dashboard asks.
            self.window.confirm(
                tr("Game Running"),
                tr(
                    "Mod Organizer / the game appears to be running under the current active profile ('{active_name}').\n\nSwitching the active profile now will not stop it, but COMMANDER's other pages will stop reflecting its state.\n\nSwitch anyway?",
                    active_name=active.profile_name if active is not None else "",
                ),
                lambda: self._switch(name),
            )
            return
        self._switch(name)

    def _switch(self, name: str) -> None:
        self.window.notify(tr("Activating {name}...", name=name), 0)
        # activate_profile only needs window.refresh_settings() and
        # window.settings, both of which DeckWindow provides - so the
        # desktop helper is reused exactly as-is, CLI call and all.
        self._task = activate_profile(self.window, self, name, on_done=self._done)
        self.chooser.setEnabled(False)

    def _done(self, ok: bool) -> None:
        self._task = None
        self.chooser.setEnabled(True)
        self.window.toast.hide()
        if ok:
            active = self.window.settings.active_profile
            self.window.notify(
                tr(
                    "Profile '{name}' is now active.",
                    name=active.profile_name if active is not None else "",
                )
            )
        # A different profile means different folders, mods and updates -
        # and results cached for the old one must not be shown for it.
        for page in list(self.window._pages.values()):
            forget = getattr(page, "forget_cached_checks", None)
            if callable(forget):
                forget()
            page.refresh()
