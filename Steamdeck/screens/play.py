"""The Play screen - the reason Deck Mode exists.

One oversized button, two choices, and a status line. Everything the desktop
Play page offers beyond that (command preview, desktop shortcuts, the
GE-Proton downloader, log dumps) is setup work, and setup is not what
someone does while holding the device.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QSizePolicy, QVBoxLayout

from commander_gui import gui_settings
from commander_gui.assistant_launcher import AssistantLaunchError, launch_assistant
from commander_gui.deck_launch import in_game_mode
from commander_gui.game_stats import (
    STAT_FIELDS,
    SaveStats,
    latest_save_stats,
    stat_value,
)
from commander_gui.i18n import tr
from commander_gui.launcher import Mo2Executable
from commander_gui.log_dump import create_log_dump
from commander_gui.modlist import looks_flipped, modlist_path_for, read_lines, unflip
from commander_gui.ui.common import (
    BackgroundTask,
    format_last_played,
    format_playtime,
    play_click_sound,
)

from .. import gamepad as pad
from ..launch import DeckLaunchController, runner_label, runner_options
from ..modlist_io import ModlistWriteBlocked, write_lines
from ..scale import px
from ..widgets import (
    BUTTON_H,
    DeckCard,
    DeckOverlay,
    DeckPicker,
    DeckRow,
    deck_button,
    deck_divider_v,
    deck_label,
    picker_overlay,
    repolish,
    side_by_side,
)
from .base import DeckScreen


class PlayScreen(DeckScreen):
    def build(self) -> None:
        self.controller = DeckLaunchController(self)
        self.controller.state_changed.connect(self._on_state_changed)
        self.controller.status.connect(self._on_status)
        self.controller.failed.connect(self._on_failed)
        self.controller.crashed.connect(self._on_crashed)

        self._targets: list[str] = []
        self._crash_task: BackgroundTask | None = None
        self._stats_task: BackgroundTask | None = None
        self._stats_for: tuple | None = None

        # Two plain rows side by side, like Mods | Storage on the Dashboard.
        # They used to sit in a card of their own, which made them the one
        # block on the screen with a frame around its rows.
        self.target_row = DeckRow(tr("Launch Game"))
        self.target_row.activated.connect(self._pick_target)
        self.runner_row = DeckRow(tr("Runner"))
        self.runner_row.activated.connect(self._pick_runner)
        self.body.addLayout(side_by_side(self.target_row, self.runner_row))

        # The glyph is not decoration: this is the one control a Deck user
        # aims for without reading it first.
        # While the game runs the same button becomes Quit Game (red): the
        # Deck has no Alt+F4 and Game Mode no task manager, so a hung game
        # has no other way out - and it is the button already under the
        # thumb.
        self.hero = deck_button(
            "\u25b6   " + tr("Play GAMMA"), role="hero", on_click=self._hero_clicked
        )
        self.body.addWidget(self.hero)

        # Live launch-lifecycle text ("Starting...", "Running", ...) - a
        # plain caption rather than a boxed row, since it is read-only and
        # was never meant to be a D-pad stop.
        # Centred under the button they describe: "Ready" / "Running 12m"
        # and the playtime line read as the button's caption, not as stray
        # text in the page's left margin.
        self.status_caption = deck_label("", role="caption", wrap=True)
        self.status_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.status_caption)

        self.playtime_label = deck_label("", role="caption", wrap=True)
        self.playtime_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.playtime_label)

        # Open MO2 | Play Anomaly - one fixed height for both, so the pair
        # reads as one row whatever their roles' stylesheet minimums are.
        actions = QHBoxLayout()
        actions.setSpacing(px(12))
        self.mo2_button = deck_button(tr("Open MO2"), on_click=self._open_mo2)
        self.anomaly_button = deck_button(
            tr("Play Anomaly"), on_click=self._play_anomaly
        )
        # Achievements rides in the same row: its own control, clear of the
        # stats grid, and no extra height on a page that is already full.
        self.achievements_button = deck_button(
            "\u2605  " + tr("Achievements"), on_click=self._show_achievements
        )
        self.achievements_button.setEnabled(False)
        self._save: SaveStats | None = None
        for button in (self.mo2_button, self.anomaly_button, self.achievements_button):
            button.setFixedHeight(px(BUTTON_H))
            # Equal thirds whatever the label length.
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        actions.addWidget(self.mo2_button, 1)
        actions.addWidget(deck_divider_v(), 0, Qt.AlignmentFlag.AlignVCenter)
        actions.addWidget(self.anomaly_button, 1)
        actions.addWidget(deck_divider_v(), 0, Qt.AlignmentFlag.AlignVCenter)
        actions.addWidget(self.achievements_button, 1)
        self.body.addLayout(actions)

        self._build_stats()
        self.body.addStretch(1)

        self._session_timer = QTimer(self)
        self._session_timer.setInterval(1000)
        self._session_timer.timeout.connect(self._tick)

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        profile = self.profile()
        if profile is None:
            self.hero.setEnabled(False)
            self.target_row.set_value(tr("No Profile"))
            self.runner_row.set_value(tr("No Profile"))
            self._set_status(tr("No Profile"), accent=False)
            self.playtime_label.setText(
                tr("Create or activate a profile first (Dashboard → Profiles).")
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
        # Quit Game stays pressable while a game runs, whatever else is busy.
        self.hero.setEnabled((bool(current) and not busy) or self.controller.is_active())
        self.mo2_button.setEnabled(not busy)
        self.anomaly_button.setEnabled(bool(current) and not busy)
        self.update_hints()
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
        self._load_stats()

    def on_busy_changed(self, busy: bool) -> None:
        if self.controller.is_active():
            return
        self.hero.setEnabled(not busy and bool(self._targets))
        self.mo2_button.setEnabled(not busy)
        self.anomaly_button.setEnabled(not busy and bool(self._targets))
        self.update_hints()

    # -- shortcuts --------------------------------------------------------
    def on_action(self, action: str) -> bool:
        if action == pad.CONTEXT:
            if self.controller.is_active():
                self._confirm_stop()
            else:
                self.play()
            return True
        if action == pad.SEARCH:
            if self.mo2_button.isEnabled():
                self._open_mo2()
            return True
        return False

    def hints(self):
        running = self.controller.is_active()
        hints = [("A", "Select")]
        if running or self.hero.isEnabled():
            if running:
                stop = "Close MO2" if self.controller.mo2_only() else "Quit Game"
            else:
                stop = "Play"
            hints.append(("X", stop))
        # Only while Y would actually do it: the prompt used to stay up with
        # the game running, when Open MO2 is disabled.
        if self.mo2_button.isEnabled():
            hints.append(("Y", "Open MO2"))
        hints.append(("L1 R1 / L2 R2", "Switch tab"))
        return hints

    def default_focus(self):
        # The one control a Deck user comes here for.
        return self.hero

    # -- actions ----------------------------------------------------------
    def play(self) -> None:
        """Launch, if the Play button itself could be pressed right now."""
        if self.hero.isEnabled() and not self.controller.is_active():
            play_click_sound()
            self._play()

    def _hero_clicked(self) -> None:
        if self.controller.is_active():
            self._confirm_stop()
        else:
            play_click_sound()
            self._play()

    def _play(self) -> None:
        profile = self.profile()
        if profile is None or self.window.install_busy:
            return
        target = gui_settings.load_gui_settings().get("target") or ""
        if not target:
            self.window.notify(tr("Select a launch target first."))
            return
        path = modlist_path_for(profile.gamma, profile.mo2_profile)
        lines: list[str] = []
        if path is not None:
            try:
                lines = read_lines(path)
            except (OSError, ValueError):
                lines = []
        if looks_flipped(lines):
            self._confirm_flipped(profile, target, path, lines)
            return
        self.controller.launch(profile, target=target)

    def _confirm_flipped(self, profile, target: str, path, lines: list[str]) -> None:
        """A reversed load order (the old Flip Priority button) crashes GAMMA
        on startup: offer to put it back before launching."""

        def _fix() -> None:
            self.window.dismiss_overlay()
            try:
                write_lines(self.window, path, unflip(lines), snapshot=True)
            except (ModlistWriteBlocked, OSError) as exc:
                self.window.notify(str(exc), 6000)
                return
            self.window.notify(tr("Load order fixed."), 4000)
            self.controller.launch(profile, target=target)

        def _anyway() -> None:
            self.window.dismiss_overlay()
            self.controller.launch(profile, target=target)

        self.window.show_overlay(
            DeckOverlay(
                tr("Load order is reversed"),
                deck_label(
                    tr(
                        "This profile's load order is reversed, so GAMMA will most "
                        "likely crash on startup. Put it back the right way round "
                        "before launching? A backup is saved first."
                    ),
                    role="body",
                    wrap=True,
                ),
                [
                    (tr("Fix and launch"), _fix, "primary"),
                    (tr("Launch anyway"), _anyway, "normal"),
                    (tr("Cancel"), self.window.dismiss_overlay, "normal"),
                ],
                panel_width=1000,
            )
        )

    def _open_mo2(self) -> None:
        profile = self.profile()
        if profile is None or self.window.install_busy:
            return
        # No target: build_command() then opens Mod Organizer's own window
        # instead of auto-running an executable through it.
        self.controller.launch(profile, target=None)

    def _play_anomaly(self) -> None:
        """Anomaly without MO2, like the desktop Play page's direct launch.

        Runs the selected target's executable (desktop's
        ``_resolve_command(direct=True)``), falling back to the plain
        "Anomaly" entry when nothing is selected yet.
        """
        profile = self.profile()
        if profile is None or self.window.install_busy:
            return
        executables = self.controller.executables(profile)
        target = gui_settings.load_gui_settings().get("target") or "Anomaly"
        exe = next(
            (e for e in executables if e.title == target),
            next((e for e in executables if e.title == "Anomaly"), Mo2Executable()),
        )
        if not exe.binary:
            self.window.notify(tr("Not installed"))
            return
        play_click_sound()
        self.controller.launch(profile, target=None, direct=exe)

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
        self.window.show_overlay(picker_overlay(self.window, title, picker))

    # -- game stats -------------------------------------------------------
    def _build_stats(self) -> None:
        """A card of counters read from the newest save (see game_stats)."""
        card = DeckCard()
        grid = QGridLayout()
        grid.setHorizontalSpacing(px(16))
        grid.setVerticalSpacing(px(2))
        self._stat_values: dict[str, object] = {}
        for index, (key, label) in enumerate(STAT_FIELDS):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            value = deck_label("–", role="statValue")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption = deck_label(tr(label), role="caption")
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.addWidget(value)
            cell.addWidget(caption)
            grid.addLayout(cell, index // 3, index % 3)
            self._stat_values[key] = value
        card.body.setContentsMargins(px(20), px(8), px(20), px(8))
        card.body.setSpacing(px(4))
        card.body.addLayout(grid)
        # "From save: ..." and the Achievements button share the card's last
        # line, so the window costs the full page no extra height.
        self.stats_source = deck_label("", role="caption", wrap=True)
        self.stats_source.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.body.addWidget(self.stats_source)
        self.body.addWidget(card)

    def _render_achievements_button(self) -> None:
        from commander_gui.achievements import ACHIEVEMENTS, unlocked_count

        if self._save is None:
            self.achievements_button.setText("\u2605  " + tr("Achievements"))
            self.achievements_button.setEnabled(False)
            return
        self.achievements_button.setText(
            "\u2605  "
            + tr("Achievements")
            + f"   {unlocked_count(self._save)} / {len(ACHIEVEMENTS)}"
        )
        self.achievements_button.setEnabled(True)

    def _show_achievements(self) -> None:
        from ..achievements import show_achievements

        show_achievements(self.window, self._save)

    def _load_stats(self) -> None:
        profile = self.profile()
        if profile is None:
            return
        folders = (profile.anomaly, profile.gamma, profile.mo2_profile)
        if self._stats_task is not None and self._stats_for == folders:
            return
        # A read still running for another profile's folders (the profile
        # was just switched) is superseded: its result is dropped below.
        self._stats_for = folders
        task = BackgroundTask(latest_save_stats, *folders, parent=self)
        self._stats_task = task
        task.result.connect(
            lambda result, f=folders: self._show_stats(result) if f == self._stats_for else None
        )
        task.error.connect(
            lambda _m, f=folders: self._show_stats(None) if f == self._stats_for else None
        )
        task.start()

    def _show_stats(self, result: object) -> None:
        self._stats_task = None
        self._save = result if isinstance(result, SaveStats) else None
        self._render_achievements_button()
        if not isinstance(result, SaveStats):
            for value in self._stat_values.values():
                value.setText("–")
            self.stats_source.setText(tr("No saves yet - stats appear after your first save."))
            return
        for key, _label in STAT_FIELDS:
            self._stat_values[key].setText(f"{stat_value(result, key):,}")
        self.stats_source.setText(
            tr("From save: {name}", name=result.save_name)
            + "   ·   "
            + format_last_played(result.mtime or time.time())
        )

    # -- controller signals -----------------------------------------------
    def _on_state_changed(self, active: bool) -> None:
        if active:
            # Open MO2 is not a game session: say what the button closes.
            stop = tr("Close MO2") if self.controller.mo2_only() else tr("Quit Game")
            self.hero.setText("\u25a0   " + stop)
            self.hero.setObjectName("deckHeroDanger")
            self.hero.setEnabled(True)
        else:
            self.hero.setText("\u25b6   " + tr("Play GAMMA"))
            self.hero.setObjectName("deckHero")
            self.hero.setEnabled(bool(self._targets) and not self.window.install_busy)
        repolish(self.hero)
        self.mo2_button.setEnabled(not active)
        self.anomaly_button.setEnabled(not active and bool(self._targets))
        if self.window.current_page() is self:
            self.window.update_hints()
        if active:
            self._session_timer.start()
        else:
            self._session_timer.stop()
            self.refresh()
            # A window rescale held back for the session can happen now.
            QTimer.singleShot(0, self.window.apply_pending_rescale)

    def _tick(self) -> None:
        seconds = self.controller.session_seconds()
        if seconds > 0:
            label = tr("Mod Organizer is running") if self.controller.mo2_only() else tr("Running")
            self._set_status(label + "   ·   " + format_playtime(seconds), accent=True)

    def _confirm_stop(self) -> None:
        if self.controller.mo2_only():
            self.window.confirm(
                tr("Close MO2"),
                tr(
                    "Close Mod Organizer immediately? A game started from it "
                    "closes too, and anything not saved in-game is lost."
                ),
                self.controller.force_stop,
                confirm_text=tr("Close MO2"),
                confirm_role="danger",
            )
            return
        self.window.confirm(
            tr("Quit Game"),
            tr(
                "Close the game and Mod Organizer immediately? Anything not "
                "saved in-game is lost."
            ),
            self.controller.force_stop,
            confirm_text=tr("Quit Game"),
            confirm_role="danger",
        )

    def _on_status(self, text: str) -> None:
        if text == tr("Running") and self.controller.session_seconds() > 0:
            # The per-second tick owns this line while the game is up.
            return
        if self.controller.mo2_only() and text == tr("Running"):
            # MO2's own window is this session's "running" process.
            text = tr("Mod Organizer is running")
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

    # -- crash analysis -----------------------------------------------------
    def _on_crashed(self) -> None:
        """A session ended with a new crash dump: offer to analyze it."""
        title = tr("Game Crashed")
        message = tr("The game appears to have crashed - a new crash log was written.")
        message += "\n\n" + tr(
            "Analyze Crash collects the logs into a log dump and opens it in "
            "ASSISTANT, which explains the error and suggests a fix."
        )
        if gui_settings.load_gui_settings().get("deck_finish_rumble", True) is not False:
            # Felt even with the Deck set down after the game closed.
            self.window.gamepad.rumble(strength=0.9, length_ms=700)
        self.window.show_overlay(
            DeckOverlay(
                title,
                deck_label(message, role="body", wrap=True),
                [
                    (tr("Analyze Crash"), self._analyze_crash, "primary"),
                    (tr("Close"), self.window.dismiss_overlay, "normal"),
                ],
                panel_width=1100,
            )
        )

    def _analyze_crash(self) -> None:
        self.window.dismiss_overlay()
        if self._crash_task is not None:
            return
        self.window.notify(tr("Collecting logs for ASSISTANT..."), 0)
        task = BackgroundTask(create_log_dump, parent=self)
        self._crash_task = task
        task.result.connect(self._on_crash_dump_done)
        task.error.connect(self._on_crash_dump_error)
        task.start()

    def _on_crash_dump_done(self, result: object) -> None:
        self._crash_task = None
        self.window.toast.hide()
        if not isinstance(result, (tuple, list)) or len(result) != 2:
            return
        path = result[0]
        # ASSISTANT is a desktop window; under Game Mode's gamescope it
        # would open somewhere the player can't reach, so say where the
        # dump is instead.
        if not in_game_mode():
            try:
                launch_assistant(path)
            except AssistantLaunchError:
                pass
            else:
                self.window.notify(tr("Crash analysis opened in ASSISTANT."), 5000)
                return
        self.window.show_overlay(
            DeckOverlay(
                tr("Log Dump created"),
                deck_label(
                    tr("Log dump saved to:\n{path}", path=str(path))
                    + "\n\n"
                    + tr(
                        "Open it in ASSISTANT from Desktop Mode, or attach it "
                        "to a bug report."
                    ),
                    role="body",
                    wrap=True,
                ),
                [(tr("OK"), self.window.dismiss_overlay, "primary")],
                panel_width=1100,
            )
        )

    def _on_crash_dump_error(self, message: str) -> None:
        self._crash_task = None
        self.window.toast.hide()
        self.window.notify(tr("Log Dump failed: {message}", message=message), 8000)

    def _goto_system(self) -> None:
        self.window.dismiss_overlay()
        self.window.set_page("system")
