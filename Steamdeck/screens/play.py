"""The Play screen - the reason Deck Mode exists.

One oversized button, two choices, and a status line. Everything the desktop
Play page offers beyond that (command preview, desktop shortcuts, the
GE-Proton downloader, log dumps) is setup work, and setup is not what
someone does while holding the device.
"""

from __future__ import annotations

from commander_gui import gui_settings
from commander_gui.i18n import tr
from commander_gui.ui.common import (
    format_last_played,
    format_playtime,
    play_click_sound,
)

from ..launch import DeckLaunchController, runner_label, runner_options
from ..widgets import (
    DeckPicker,
    DeckRow,
    deck_button,
    deck_label,
    deck_two_column_card,
)
from .base import DeckScreen


class PlayScreen(DeckScreen):
    def build(self) -> None:
        self.controller = DeckLaunchController(self)
        self.controller.state_changed.connect(self._on_state_changed)
        self.controller.status.connect(self._on_status)
        self.controller.failed.connect(self._on_failed)

        self._targets: list[str] = []

        # Shared divided-card pattern (also used by Dashboard/Install) rather
        # than two independent cards, so "two things side by side" looks the
        # same everywhere in the app.
        config_card, target_col, runner_col = deck_two_column_card()

        self.target_row = DeckRow(tr("Launch Game"))
        self.target_row.activated.connect(self._pick_target)
        target_col.addWidget(self.target_row)

        self.runner_row = DeckRow(tr("Runner"))
        self.runner_row.activated.connect(self._pick_runner)
        runner_col.addWidget(self.runner_row)

        self.body.addWidget(config_card)

        # The glyph is not decoration: this is the one control a Deck user
        # aims for without reading it first.
        self.hero = deck_button(
            "\u25b6   " + tr("Play GAMMA"), role="hero", on_click=self._play
        )
        self.hero.clicked.connect(play_click_sound)
        self.body.addWidget(self.hero)

        # Live launch-lifecycle text ("Starting...", "Running", ...) - a
        # plain caption rather than a boxed row, since it is read-only and
        # was never meant to be a D-pad stop.
        self.status_caption = deck_label("", role="caption", wrap=True)
        self.body.addWidget(self.status_caption)

        self.playtime_label = deck_label("", role="caption", wrap=True)
        self.body.addWidget(self.playtime_label)

        self.body.addSpacing(8)
        self.mo2_button = deck_button(tr("Open MO2"), on_click=self._open_mo2)
        self.body.addWidget(self.mo2_button)
        self.body.addStretch(1)

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        profile = self.profile()
        if profile is None:
            self.hero.setEnabled(False)
            self.target_row.set_value(tr("No Profile"))
            self.runner_row.set_value(tr("No Profile"))
            self._set_status(tr("No Profile"), accent=False)
            self.playtime_label.setText(
                tr("Create or activate a profile first (Profiles page).")
            )
            return

        state = gui_settings.load_gui_settings()
        self._targets, default = self.controller.targets(profile)
        saved = state.get("target") or ""
        current = saved if saved in self._targets else default
        if current and current != saved:
            gui_settings.save_gui_settings(target=current)
        self.target_row.set_value(current or tr("Not installed"))
        self.runner_row.set_value(runner_label(state.get("runner") or "auto"))

        busy = self.window.install_busy or self.controller.is_active()
        self.hero.setEnabled(bool(current) and not busy)
        self.mo2_button.setEnabled(not busy)
        if not self.controller.is_active():
            if current:
                self._set_status(tr("Ready"), accent=True)
            else:
                self._set_status(tr("Not installed"), accent=False)

        name = profile.profile_name or ""
        playtime = (state.get("playtime_seconds") or {}).get(name, 0.0)
        last = (state.get("last_played_ts") or {}).get(name)
        self.playtime_label.setText(
            tr("Total playtime")
            + ": "
            + format_playtime(playtime)
            + "   ·   "
            + tr("Last played")
            + ": "
            + format_last_played(last)
        )

    def on_busy_changed(self, busy: bool) -> None:
        self.hero.setEnabled(not busy and bool(self._targets))
        self.mo2_button.setEnabled(not busy)

    # -- actions ----------------------------------------------------------
    def _play(self) -> None:
        profile = self.profile()
        if profile is None or self.window.install_busy:
            return
        target = gui_settings.load_gui_settings().get("target") or ""
        if not target:
            self.window.notify(tr("Select a launch target first."))
            return
        self.controller.launch(profile, target=target)

    def _open_mo2(self) -> None:
        profile = self.profile()
        if profile is None or self.window.install_busy:
            return
        # No target: build_command() then opens Mod Organizer's own window
        # instead of auto-running an executable through it.
        self.controller.launch(profile, target=None)

    def _pick_target(self) -> None:
        if not self._targets:
            self.window.notify(tr("Not installed"))
            return
        current = gui_settings.load_gui_settings().get("target") or ""
        picker = DeckPicker(
            "", [(title, title) for title in self._targets], current
        )
        picker.chosen.connect(self._apply_target)
        self._show_picker(picker, tr("Launch Game"))

    def _apply_target(self, title: object) -> None:
        self.window.dismiss_overlay()
        gui_settings.save_gui_settings(target=str(title))
        self.refresh()

    def _pick_runner(self) -> None:
        current = gui_settings.load_gui_settings().get("runner") or "auto"
        picker = DeckPicker("", runner_options(), current)
        picker.chosen.connect(self._apply_runner)
        self._show_picker(picker, tr("Runner"))

    def _apply_runner(self, kind: object) -> None:
        self.window.dismiss_overlay()
        gui_settings.save_gui_settings(
            runner=str(kind), deck_runner_confirmed=True
        )
        self.refresh()

    def _show_picker(self, picker: DeckPicker, title: str) -> None:
        from ..widgets import DeckOverlay

        self.window.show_overlay(
            DeckOverlay(
                title,
                picker,
                [(tr("Cancel"), self.window.dismiss_overlay, "normal")],
                panel_width=1000,
            )
        )

    # -- controller signals -----------------------------------------------
    def _on_state_changed(self, active: bool) -> None:
        self.hero.setEnabled(not active and bool(self._targets))
        self.mo2_button.setEnabled(not active)
        if not active:
            self.refresh()

    def _on_status(self, text: str) -> None:
        self._set_status(text, accent=True)

    def _set_status(self, text: str, *, accent: bool) -> None:
        """Colored like desktop's launch-status label (accent green for a
        live/good state), plain dim caption only for "no profile"/"not
        installed" - matching play_page.py's own ACCENT-for-success rule.
        """
        self.status_caption.setText(text)
        self.status_caption.setObjectName(
            "deckCaptionAccent" if accent else "deckCaption"
        )
        self.status_caption.style().unpolish(self.status_caption)
        self.status_caption.style().polish(self.status_caption)

    def _on_failed(self, title: str, message: str) -> None:
        body = deck_label(message, role="body", wrap=True)
        from ..widgets import DeckOverlay

        self.window.show_overlay(
            DeckOverlay(
                title,
                body,
                [
                    (tr("OK"), self.window.dismiss_overlay, "primary"),
                    (tr("System Check"), self._goto_system, "normal"),
                ],
            )
        )

    def _goto_system(self) -> None:
        self.window.dismiss_overlay()
        self.window.set_page("system")
