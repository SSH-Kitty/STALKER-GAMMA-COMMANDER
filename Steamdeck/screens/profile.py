"""Switching the active CLI profile.

Switching only. Creating or editing a profile means typing six absolute
paths plus repository URLs - a keyboard job, and one the desktop interface
already does well. What a Deck user plausibly wants here is to move between
profiles they set up earlier.
"""

from __future__ import annotations

from PySide6.QtCore import Qt

from commander_gui.i18n import tr
from commander_gui.ui.common import activate_profile, mo2_running

from ..widgets import DeckRow, deck_label
from .base import DeckScreen


class ProfileScreen(DeckScreen):
    def build(self) -> None:
        self._task = None
        self.caption = deck_label("", role="caption", wrap=True)
        self.body.addWidget(self.caption)
        self.rows_from = self.body.count()
        self.body.addStretch(1)

    def refresh(self) -> None:
        # Rebuilt wholesale rather than diffed: the list is short, and a
        # profile's name is its identity, so there is no row state worth
        # preserving across a refresh.
        while self.body.count() > self.rows_from:
            item = self.body.takeAt(self.rows_from)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.window.refresh_settings()
        profiles = self.window.settings.profiles
        active = self.window.settings.active_profile
        active_name = active.profile_name if active is not None else ""
        self.caption.setText(
            tr("Active profile") + ": " + (active_name or tr("No Profile"))
        )

        if not profiles:
            self.body.addWidget(
                deck_label(
                    tr(
                        "A COMMANDER profile stores install folders, download "
                        "settings, and repository options. The active profile "
                        "is what the other pages use."
                    ),
                    role="body",
                    wrap=True,
                )
            )
            self.body.addStretch(1)
            return

        for profile in profiles:
            name = profile.profile_name or tr("No Profile")
            selected = name == active_name
            row = DeckRow(
                ("●  " if selected else "○  ") + name,
                tr("Active") if selected else "",
                chevron=not selected,
            )
            row.setEnabled(not self.window.install_busy)
            if not selected:
                row.activated.connect(
                    lambda _=False, n=name: self._confirm_switch(n)
                )
            else:
                row.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.body.addWidget(row)
        self.body.addStretch(1)

    def on_busy_changed(self, busy: bool) -> None:
        self.refresh()

    # -- switching --------------------------------------------------------
    def _confirm_switch(self, name: str) -> None:
        if self.window.install_busy:
            self.window.notify(tr("An install is already running."))
            return
        if mo2_running():
            self.window.confirm(
                tr("Mod Organizer is running"),
                tr(
                    "Switching profile while Mod Organizer is open can "
                    "confuse it.\n\nSwitch anyway?"
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

    def _done(self, ok: bool) -> None:
        self.window.toast.hide()
        if ok:
            self.window.notify(tr("Profile '{name}' is now active.", name=(
                self.window.settings.active_profile.profile_name
                if self.window.settings.active_profile is not None
                else ""
            )))
        self.refresh()
