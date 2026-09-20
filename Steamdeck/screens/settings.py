"""Quick settings: theme, language, runner, text size, and the way out.

Deliberately short. The desktop Settings page covers launch options, MO2
DPI, autostart, start page and Discord presence; none of that is worth
navigating with a thumbstick, and all of it is still there one restart away.
What is here is what a Deck user plausibly changes while holding the device.
"""

from __future__ import annotations

from PySide6.QtCore import Qt

from commander_gui import gui_settings
from commander_gui.i18n import LANGUAGE_INFO, set_active_language, tr
from commander_gui.themes import THEME_INFO
from commander_gui.ui.common import mo2_running
from commander_gui.ui.deck_switch import switch_mode

from ..launch import runner_label, runner_options
from ..widgets import (
    BUTTON_H,
    DeckPicker,
    DeckRow,
    deck_button,
    deck_label,
)
from .base import DeckScreen

#: Percentage steps for the Deck font scale, matching gui_settings' 80-150
#: clamp. Ten-point steps are coarse enough to feel like a real change and
#: fine enough not to overshoot.
_SCALE_MIN, _SCALE_MAX, _SCALE_STEP = 80, 150, 10


class SettingsScreen(DeckScreen):
    def build(self) -> None:
        self.theme_row = DeckRow(tr("Theme"))
        self.theme_row.activated.connect(self._pick_theme)
        self.body.addWidget(self.theme_row)

        self.language_row = DeckRow(tr("Language"))
        self.language_row.activated.connect(self._pick_language)
        self.body.addWidget(self.language_row)

        self.runner_row = DeckRow(tr("Runner"))
        self.runner_row.activated.connect(self._pick_runner)
        self.body.addWidget(self.runner_row)

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
            button.setFixedWidth(BUTTON_H)
        row_layout.addWidget(self.minus_button)
        row_layout.addWidget(self.plus_button)
        self.body.addWidget(self.scale_row)

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
                role="danger",
                on_click=lambda: switch_mode(self.window, deck=False),
            )
        )
        self.body.addWidget(
            deck_button(tr("Quit COMMANDER"), on_click=self.window.close)
        )
        self.body.addStretch(1)

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

    # -- theme ------------------------------------------------------------
    def _pick_theme(self) -> None:
        current = gui_settings.load_gui_settings().get("theme") or "gamma"
        picker = DeckPicker(
            "",
            [(label, key) for key, label, _d, _s in THEME_INFO],
            current,
        )
        picker.chosen.connect(self._apply_theme)
        self.window.show_overlay(
            self._overlay_for(picker, tr("Theme"))
        )

    def _apply_theme(self, key: object) -> None:
        self.window.dismiss_overlay()
        gui_settings.save_gui_settings(theme=str(key))
        # Theme is pure QSS plus a palette, so it takes effect immediately -
        # no widget rebuild needed, unlike a language change.
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
        self.window.rebuild()

    def _reject_if_busy(self) -> bool:
        """Block a language change during an install, as the desktop UI does.

        Rebuilding every widget mid-install would tear down the progress
        view a running CommandRunner is still feeding.
        """
        if self.window.install_busy:
            self.window.notify(tr("An install is already running."))
            return True
        if mo2_running():
            self.window.notify(tr("Mod Organizer is running"))
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

    # -- text size --------------------------------------------------------
    def _nudge_scale(self, delta: int) -> None:
        state = gui_settings.load_gui_settings()
        value = int(state.get("deck_font_scale") or 100) + delta
        value = max(_SCALE_MIN, min(_SCALE_MAX, value))
        gui_settings.save_gui_settings(deck_font_scale=value)
        self.window.apply_style()
        self.refresh()

    # -- helpers ----------------------------------------------------------
    def _overlay_for(self, picker: DeckPicker, title: str):
        from ..widgets import DeckOverlay

        return DeckOverlay(
            title,
            picker,
            [(tr("Cancel"), self.window.dismiss_overlay, "normal")],
            panel_width=1000,
        )
