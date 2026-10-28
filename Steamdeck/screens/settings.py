"""Deck settings: look, language, runner, startup behaviour, Steam, exit.

Still shorter than the desktop page - typed launch options, MO2 DPI, autostart
and Discord presence stay there - but everything a Deck owner plausibly
changes while holding the device is here, including the two that used to
send people back to Desktop Mode: whether COMMANDER opens straight into
Deck Mode, and adding it to the Steam library so Game Mode can launch it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer

from commander_gui import (
    __version__,
    __version_label__,
    coop,
    gui_settings,
    launch_options,
)
from commander_gui.deck_launch import in_game_mode, steam_deck_model
from commander_gui.i18n import LANGUAGE_INFO, set_active_language, tr
from commander_gui.launcher import available_commands
from commander_gui.self_update import commander_appimage_path
from commander_gui.steam_shortcuts import (
    add_to_steam,
    find_shortcuts_vdf,
    list_steam_accounts,
    steam_is_running,
)
from commander_gui.themes import LIVE_TIME_CHOICES, THEME_INFO
from commander_gui.ui.common import BackgroundTask, game_running, play_click_sound
from commander_gui.ui.deck_switch import switch_mode
from commander_gui.updates import is_unstable_version, newer_unstable_tag

from ..launch import runner_label, runner_options
from ..scale import px
from ..widgets import (
    BUTTON_H,
    ROW_H,
    DeckOverlay,
    DeckPicker,
    DeckRow,
    DeckSegmented,
    DeckToggleRow,
    deck_button,
    deck_label,
    picker_overlay,
)
from . import SCREENS
from .base import DeckScreen

#: Percentage steps for the Deck font scale, matching gui_settings' 80-150
#: clamp. Ten-point steps are coarse enough to feel like a real change and
#: fine enough not to overshoot.
_SCALE_MIN, _SCALE_MAX, _SCALE_STEP = 80, 150, 10

#: deck_mode_preference values, as gui_settings validates them, with the
#: short labels the inline switcher shows. "ask" is still a stored value
#: (the default, and what a Deck's first launch asks about) but not a
#: choice here: the switcher shows what "ask" currently resolves to.
#: deck_overlay_shortcut values, as gui_settings validates them.
_OVERLAY_SHORTCUTS = (
    ("L4 + R4 (upper back grips)", "l4r4"),
    ("L5 + R5 (lower back grips)", "l5r5"),
    ("L4 + L5 (left back grips)", "l4l5"),
    ("R4 + R5 (right back grips)", "r4r5"),
    ("Off", "off"),
)

_STARTUP_CHOICES = (
    ("always", "Steam Deck"),
    ("never", "Desktop"),
)


class SettingsScreen(DeckScreen):
    def build(self) -> None:
        self._steam_task: BackgroundTask | None = None

        self.body.addWidget(deck_label(tr("Appearance"), role="rowTitle"))
        self.theme_row = DeckRow(tr("Theme"))
        self.theme_row.activated.connect(self._pick_theme)
        self.body.addWidget(self.theme_row)
        self.live_animation_row = DeckToggleRow(tr("Live theme animation"), True)
        self.live_animation_row.toggled.connect(self._toggle_live_animation)
        self.body.addWidget(self.live_animation_row)
        self.lights_row = DeckToggleRow(tr("Christmas lights"), True)
        self.lights_row.toggled.connect(self._toggle_lights)
        self.body.addWidget(self.lights_row)
        self.live_time_row = DeckRow(tr("Live theme time of day"))
        self.live_time_row.activated.connect(self._pick_live_time)
        self.body.addWidget(self.live_time_row)

        self.language_row = DeckRow(tr("Language"))
        self.language_row.activated.connect(self._pick_language)
        self.body.addWidget(self.language_row)

        self.scale_row = DeckRow(tr("Text size"), chevron=False)
        # The stepper buttons live inside the row, so the row itself must not
        # also be a focus stop - the D-pad would otherwise land on a row that
        # does nothing before reaching the buttons that do.
        self.scale_row.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row_layout = self.scale_row.layout()
        self.minus_button = deck_button(
            "−", role="step", on_click=lambda: self._nudge_scale(-_SCALE_STEP)
        )
        self.plus_button = deck_button(
            "+", role="step", on_click=lambda: self._nudge_scale(_SCALE_STEP)
        )
        for button in (self.minus_button, self.plus_button):
            button.setFixedWidth(px(BUTTON_H))
        row_layout.addWidget(self.minus_button)
        row_layout.addWidget(self.plus_button)
        self.body.addWidget(self.scale_row)

        self.body.addWidget(deck_label(tr("Game"), role="rowTitle"))
        self.runner_row = DeckRow(tr("Runner"))
        self.runner_row.activated.connect(self._pick_runner)
        self.body.addWidget(self.runner_row)
        self.gamemode_row = DeckToggleRow(tr("Always use GameMode"))
        self.gamemode_row.toggled.connect(
            lambda on: gui_settings.save_gui_settings(always_gamemoderun=bool(on))
        )
        self.body.addWidget(self.gamemode_row)
        # Frame generation, the performance overlay and image effects.
        self.graphics_row = DeckRow(tr("Graphics"))
        self.graphics_row.activated.connect(lambda: self.window.set_page("graphics"))
        self.body.addWidget(self.graphics_row)

        # The desktop Play page's one-click launch options, as switches.
        self.body.addWidget(deck_label(tr("Launch options"), role="rowTitle"))
        self.option_rows: list[tuple[DeckToggleRow, str]] = []
        for heading, options in launch_options.GROUPS:
            self.body.addWidget(deck_label(tr(heading), role="caption"))
            for label, option, _tip in options:
                row = DeckToggleRow(tr(label))
                row.toggled.connect(
                    lambda on, option=option: self._toggle_launch_option(option, on)
                )
                self.body.addWidget(row)
                self.option_rows.append((row, option))

        self.body.addWidget(deck_label(tr("Deck Mode"), role="rowTitle"))
        self.start_row = DeckRow(tr("Start screen"))
        self.start_row.activated.connect(self._pick_start_screen)
        self.body.addWidget(self.start_row)

        # Two choices: shown inline, not behind a picker you then scroll.
        self.startup_row = DeckRow(tr("When COMMANDER starts"), chevron=False)
        self.startup_row.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.startup_row.value_label.hide()
        self.startup_choice = DeckSegmented(
            [(tr(label), key) for key, label in _STARTUP_CHOICES]
        )
        self.startup_choice.setFixedWidth(px(400))
        self.startup_choice.changed.connect(self._apply_startup)
        self.startup_row.layout().addWidget(self.startup_choice)
        self.startup_row.setFixedHeight(px(ROW_H))
        self.body.addWidget(self.startup_row)

        self.overlay_row = DeckRow(tr("In-game overlay shortcut"))
        self.overlay_row.activated.connect(self._pick_overlay_shortcut)
        self.body.addWidget(self.overlay_row)
        self.body.addWidget(deck_label(
            tr("Hold it for half a second while a game runs to open Graphics over the game. Hold it again, or press B, to go back to the game."),
            role="caption", wrap=True,
        ))

        self.steam_row = DeckRow(tr("Add COMMANDER to Steam"))
        self.steam_row.activated.connect(self._add_to_steam)
        self.body.addWidget(self.steam_row)

        self.chime_row = DeckToggleRow(tr("Sound when an install finishes"), True)
        self.chime_row.toggled.connect(self._toggle_chime)
        self.body.addWidget(self.chime_row)
        self.rumble_row = DeckToggleRow(tr("Vibrate when an install finishes"), True)
        self.rumble_row.toggled.connect(self._toggle_rumble)
        self.body.addWidget(self.rumble_row)
        self.welcome_row = DeckToggleRow(tr("Show the Welcome screen on startup"), True)
        self.welcome_row.toggled.connect(
            lambda on: gui_settings.save_gui_settings(welcome_hidden=not on)
        )
        self.body.addWidget(self.welcome_row)


        # Stable or Unstable COMMANDER, and the way to the other one. Only
        # the AppImage can swap itself.
        self._unstable_offer: str | None = None
        self._unstable_task: BackgroundTask | None = None
        self.build_row = DeckRow(tr("COMMANDER build"))
        self.build_row.activated.connect(self._switch_build)
        self._appimage = commander_appimage_path() is not None
        self.build_row.setVisible(self._appimage)
        self.body.addWidget(self.build_row)
        self.build_note = deck_label("", role="caption", wrap=True)
        self.build_note.setVisible(self._appimage)
        self.body.addWidget(self.build_note)

        self.help_row = DeckRow(tr("Help"))
        self.help_row.activated.connect(lambda: self.window.set_page("help"))
        self.body.addWidget(self.help_row)

        self.body.addSpacing(16)
        self.body.addWidget(
            deck_label(
                tr(
                    "The full interface has everything Deck Mode leaves out: "
                    "profile editing, mod reordering, integrity repair and "
                    "maintenance tools."
                ),
                role="caption",
                wrap=True,
            )
        )
        self.body.addWidget(
            deck_button(
                tr("Exit Deck Mode"),
                on_click=lambda: switch_mode(self.window, deck=False),
            )
        )
        self.body.addWidget(
            deck_button(tr("Quit COMMANDER"), on_click=self._confirm_quit)
        )
        # Same version string the desktop status bar and About page show.
        self.version_label = deck_label(
            tr("COMMANDER {version_label}", version_label=__version_label__),
            role="caption",
        )
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.version_label)
        self.body.addStretch(1)

    def _confirm_quit(self) -> None:
        # The last button on a long screen, reached by holding Down: one
        # press past the end should not close the app outright.
        self.window.confirm(
            tr("Quit COMMANDER"),
            tr("Close COMMANDER?"),
            self.window.close,
            confirm_text=tr("Quit"),
            confirm_role="danger",
        )

    def refresh(self) -> None:
        state = gui_settings.load_gui_settings()
        theme = state.get("theme") or "gamma"
        self.theme_row.set_value(
            next((label for key, label, _d, _s in THEME_INFO if key == theme), theme)
        )
        language = state.get("language") or "en"
        self.language_row.set_value(
            next(
                (native for code, native, _en in LANGUAGE_INFO if code == language),
                language,
            )
        )
        self.runner_row.set_value(runner_label(state.get("runner") or "auto"))
        self.scale_row.set_value(f"{int(state.get('deck_font_scale') or 100)}%")
        start = state.get("deck_start_screen") or "play"
        self.start_row.set_value(
            next((tr(title) for key, title, _g in SCREENS if key == start), start)
        )
        preference = state.get("deck_mode_preference") or "ask"
        if preference == "ask":
            # Unsettled: on a Deck, startup opens Deck Mode without asking
            # in Game Mode; anywhere else it opens the desktop interface.
            preference = "always" if steam_deck_model() is not None else "never"
        self.startup_choice.set_value(preference)
        shortcut = state.get("deck_overlay_shortcut") or "l4r4"
        self.overlay_row.set_value(next((tr(label) for label, key in _OVERLAY_SHORTCUTS if key == shortcut), ""))
        self.gamemode_row.set_checked(bool(state.get("always_gamemoderun")))
        # Not installed: nothing to switch on, but a switch left on can go off.
        self.gamemode_row.setEnabled(
            bool(state.get("always_gamemoderun") or available_commands().get("gamemoderun"))
        )
        self.chime_row.set_checked(state.get("deck_finish_sound", True) is not False)
        self.rumble_row.set_checked(state.get("deck_finish_rumble", True) is not False)
        self.welcome_row.set_checked(not state.get("welcome_hidden", False))
        self.live_animation_row.set_checked(state.get("live_animation", True) is not False)
        live_time = state.get("live_time", "")
        self.live_time_row.set_value(
            next((tr(label) for value, label in LIVE_TIME_CHOICES if value == live_time), "")
        )
        # Only the Live themes animate and follow the clock.
        for row in (self.live_animation_row, self.live_time_row):
            row.setVisible(theme in ("winter", "redforest"))
        self.lights_row.set_checked(state.get("christmas_lights", True) is not False)
        self.lights_row.setVisible(theme == "winter")
        options = state.get("custom_launch_options") or ""
        for row, option in self.option_rows:
            row.set_checked(launch_options.is_on(options, option))
            # Co-op leaves "No Steam overlay" out (see coop_shortcut_fields).
            if option == launch_options.NO_STEAM_OVERLAY:
                profile = self.profile()
                row.setEnabled(profile is None or not coop.is_active(profile.anomaly))
        self.steam_row.set_value(
            tr("Adding") if self._steam_task is not None else ""
        )
        self._render_build()

    # -- COMMANDER build --------------------------------------------------
    def _render_build(self) -> None:
        if not self._appimage:
            return
        unstable = is_unstable_version(__version__)
        self.build_row.set_value(tr("Unstable") if unstable else tr("Stable"))
        if unstable:
            note = tr("You're on an unstable build: new features sooner, less tested. Going back installs the latest stable release.")
        elif self._unstable_offer:
            note = tr(
                "Unstable build {tag} is available: new features sooner, less "
                "tested. You can go back to the stable build at any time.",
                tag=self._unstable_offer.removeprefix("v"),
            )
        elif self._unstable_task is None and not getattr(self, "_unstable_checked", False):
            note = tr("Checking for an unstable build")
            self._unstable_task = BackgroundTask(newer_unstable_tag, __version__, parent=self)
            self._unstable_task.result.connect(self._unstable_found)
            self._unstable_task.error.connect(lambda _m: self._unstable_found(None))
            self._unstable_task.start()
        elif self._unstable_task is not None:
            note = tr("Checking for an unstable build")
        else:
            note = tr("There's no unstable build newer than the stable one right now.")
        self.build_note.setText(note)

    def _unstable_found(self, tag: object) -> None:
        self._unstable_task = None
        self._unstable_checked = True
        self._unstable_offer = tag if isinstance(tag, str) and tag else None
        self._render_build()

    def _switch_build(self) -> None:
        if is_unstable_version(__version__):
            self.window.switch_commander_build("stable")
        elif self._unstable_offer:
            self.window.switch_commander_build("unstable", self._unstable_offer)
        else:
            self.window.notify(self.build_note.text(), 5000)

    # -- theme ------------------------------------------------------------
    # Switching one of these on plays what it will do, so the choice is
    # heard/felt right away - the same chime and buzz announce_finished uses.
    def _toggle_chime(self, on: bool) -> None:
        gui_settings.save_gui_settings(deck_finish_sound=bool(on))
        if on:
            play_click_sound()

    def _toggle_lights(self, on: bool) -> None:
        gui_settings.save_gui_settings(christmas_lights=bool(on))
        self.window.sync_animations()

    def _toggle_live_animation(self, on: bool) -> None:
        gui_settings.save_gui_settings(live_animation=bool(on))
        self.window.sync_animations()

    def _toggle_launch_option(self, option: str, on: bool) -> None:
        options = gui_settings.load_gui_settings().get("custom_launch_options") or ""
        options = launch_options.toggle(options, option, on)
        gui_settings.save_gui_settings(custom_launch_options=options)
        # One switch can turn another off (Quiet Wine logs and Proton log).
        for row, other in self.option_rows:
            row.set_checked(launch_options.is_on(options, other))

    def _toggle_rumble(self, on: bool) -> None:
        gui_settings.save_gui_settings(deck_finish_rumble=bool(on))
        if on:
            self.window.gamepad.rumble(strength=0.55, length_ms=300)

    def _pick_theme(self) -> None:
        current = gui_settings.load_gui_settings().get("theme") or "gamma"
        picker = DeckPicker(
            "",
            [(label, key) for key, label, _d, _s in THEME_INFO],
            current,
        )
        picker.chosen.connect(self._apply_theme)
        self.window.show_overlay(self._overlay_for(picker, tr("Theme")))

    def _apply_theme(self, key: object) -> None:
        self.window.dismiss_overlay()
        gui_settings.save_gui_settings(theme=str(key))
        # Theme is pure QSS plus a palette, so it takes effect immediately -
        # no widget rebuild needed, unlike a language change.
        self.window.apply_style()
        self.refresh()

    def _pick_live_time(self) -> None:
        picker = DeckPicker(
            "",
            [(tr(label), value) for value, label in LIVE_TIME_CHOICES],
            gui_settings.load_gui_settings().get("live_time", ""),
        )
        picker.chosen.connect(self._apply_live_time)
        self.window.show_overlay(self._overlay_for(picker, tr("Live theme time of day")))

    def _apply_live_time(self, value: object) -> None:
        self.window.dismiss_overlay()
        gui_settings.save_gui_settings(live_time=str(value))
        self.window.apply_style()
        self.refresh()

    # -- language ---------------------------------------------------------
    def _pick_language(self) -> None:
        if self._reject_if_busy():
            return
        current = gui_settings.load_gui_settings().get("language") or "en"
        picker = DeckPicker(
            "",
            [(f"{native} ({english})", code) for code, native, english in LANGUAGE_INFO],
            current,
        )
        picker.chosen.connect(self._apply_language)
        self.window.show_overlay(self._overlay_for(picker, tr("Language")))

    def _apply_language(self, code: object) -> None:
        self.window.dismiss_overlay()
        if self._reject_if_busy():
            return
        gui_settings.save_gui_settings(language=str(code))
        set_active_language(str(code))
        # tr() resolves at widget-construction time, so the only way to
        # re-translate a built interface is to build it again.
        window = self.window
        window.rebuild()
        # Back on the Language row of the rebuilt screen, not its top.
        page = window.current_page()
        row = getattr(page, "language_row", None)
        if row is not None:
            QTimer.singleShot(0, row, lambda: window._focus.focus(row))

    def _reject_if_busy(self) -> bool:
        """Block a language change during an install, as the desktop UI does.

        Rebuilding every widget mid-install would tear down the progress
        view a running CommandRunner is still feeding.
        """
        if self.window.install_busy:
            self.window.notify(tr("An install is already running."))
            return True
        if game_running(force=True) or self.window.session_active():
            self.window.notify(tr("Close the game first"))
            return True
        return False

    # -- runner -----------------------------------------------------------
    def _pick_runner(self) -> None:
        current = gui_settings.load_gui_settings().get("runner") or "auto"
        picker = DeckPicker("", runner_options(), current)
        picker.chosen.connect(self._apply_runner)
        self.window.show_overlay(self._overlay_for(picker, tr("Runner")))

    def _apply_runner(self, kind: object) -> None:
        self.window.dismiss_overlay()
        # Shares the desktop UI's "runner" key on purpose: switching
        # interface must not change what the game launches with.
        gui_settings.save_gui_settings(
            runner=str(kind), deck_runner_confirmed=True
        )
        self.refresh()

    # -- startup ----------------------------------------------------------
    def _pick_start_screen(self) -> None:
        current = gui_settings.load_gui_settings().get("deck_start_screen") or "play"
        picker = DeckPicker("", [(tr(title), key) for key, title, _g in SCREENS], current)
        picker.chosen.connect(self._apply_start_screen)
        self.window.show_overlay(self._overlay_for(picker, tr("Start screen")))

    def _apply_start_screen(self, key: object) -> None:
        self.window.dismiss_overlay()
        gui_settings.save_gui_settings(deck_start_screen=str(key))
        self.refresh()

    def _pick_overlay_shortcut(self) -> None:
        current = gui_settings.load_gui_settings().get("deck_overlay_shortcut") or "l4r4"
        picker = DeckPicker("", [(tr(label), key) for label, key in _OVERLAY_SHORTCUTS], current)
        picker.chosen.connect(self._apply_overlay_shortcut)
        self.window.show_overlay(self._overlay_for(picker, tr("In-game overlay shortcut")))

    def _apply_overlay_shortcut(self, key: object) -> None:
        self.window.dismiss_overlay()
        gui_settings.save_gui_settings(deck_overlay_shortcut=str(key))
        self.window.apply_overlay_shortcut()
        self.refresh()

    def _apply_startup(self, key: object) -> None:
        gui_settings.save_gui_settings(deck_mode_preference=str(key))

    # -- steam ------------------------------------------------------------
    def _add_to_steam(self) -> None:
        if self._steam_task is not None:
            return
        if in_game_mode():
            # Game Mode *is* Steam: restarting it ends the session and takes
            # COMMANDER down with it before the shortcut could be written -
            # and Deck Mode is evidently in the library already to be here.
            self.window.notify(
                tr(
                    "Add to Steam from Desktop Mode - Steam can't restart "
                    "while Game Mode is running."
                ),
                6000,
            )
            return
        vdf_path = find_shortcuts_vdf()
        if vdf_path is not None:
            self._confirm_steam(vdf_path)
            return
        accounts = list_steam_accounts()
        if not accounts:
            self.window.notify(tr("Could not find a Steam installation on this machine."), 6000)
            return
        picker = DeckPicker("", [(label, path) for label, path in accounts])
        picker.chosen.connect(lambda path: self._confirm_steam(path))
        self.window.show_overlay(self._overlay_for(picker, tr("Which Steam account?")))

    def _confirm_steam(self, vdf_path) -> None:
        self.window.dismiss_overlay(refocus=False)
        running = steam_is_running()
        message = tr(
            "Adds \"STALKER COMMANDER\" and \"STALKER COMMANDER DECK\" "
            "to your Steam library, so Game Mode can launch them."
        )
        if running:
            message += "\n\n" + tr(
                "Steam will close and start again to pick them up - anything "
                "running through Steam closes too."
            )
        self.window.confirm(
            tr("Add COMMANDER to Steam"),
            message,
            lambda: self._start_steam(vdf_path, running),
            confirm_text=tr("Restart Steam") if running else tr("Add"),
        )

    def _start_steam(self, vdf_path, restart: bool) -> None:
        self._steam_task = BackgroundTask(
            add_to_steam, vdf_path, restart=restart, parent=self
        )
        self._steam_task.result.connect(lambda _r: self._steam_done(None))
        self._steam_task.error.connect(self._steam_done)
        self._steam_task.start()
        self.window.notify(
            tr("Restarting Steam") if restart else tr("Adding"), 0
        )
        self.refresh()

    def _steam_done(self, error: object) -> None:
        self._steam_task = None
        self.window.toast.hide()
        if error:
            self.window.notify(tr("Add COMMANDER to Steam") + ": " + str(error), 8000)
        else:
            self.window.notify(tr("Added to your Steam library."), 5000)
        self.refresh()

    # -- text size --------------------------------------------------------
    def _nudge_scale(self, delta: int) -> None:
        state = gui_settings.load_gui_settings()
        value = int(state.get("deck_font_scale") or 100) + delta
        value = max(_SCALE_MIN, min(_SCALE_MAX, value))
        gui_settings.save_gui_settings(deck_font_scale=value)
        # The number changes at once; the restyle - the expensive part, and
        # what made each press hitch - runs once the presses stop.
        self.scale_row.set_value(f"{value}%")
        self.window.apply_style_later()

    # -- helpers ----------------------------------------------------------
    def _overlay_for(self, picker: DeckPicker, title: str) -> DeckOverlay:
        return picker_overlay(self.window, title, picker)
