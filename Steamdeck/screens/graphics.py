"""Graphics: frame generation, the performance overlay and image effects.

The desktop's Frame generation card (Play page) and Settings > Graphics, as
Deck rows. Same settings keys and the same config files, so a choice made in
either interface is the other's too. Every change also reaches a running
game: MangoHud and the bundled vkBasalt reread their config files, and frame
generation rereads its own (launch_options.live_frame_gen).

The desktop's on/off keys, transparency and colour sliders are left out:
there is no keyboard to press a key on, and the sliders are fine-tuning a
look picked here already covers.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt

from commander_gui import gui_settings, launch_options, overlays
from commander_gui.i18n import tr
from commander_gui.ui.frame_gen_card import FrameGenCard, game_has_frame_gen

from ..scale import px
from ..widgets import (
    DeckPicker,
    DeckRow,
    DeckSegmented,
    DeckToggleRow,
    deck_label,
    picker_overlay,
)
from .base import DeckScreen

_MANGOHUD_PRESETS = (
    ("FPS only", "fps"), ("Basic", "basic"), ("Full", "full"),
    ("Custom selection", "custom"), ("My own MangoHud.conf", "own"),
)
_POSITIONS = ("Top left", "Top right", "Bottom left", "Bottom right")
_LOOKS = (
    "Natural", "Vibrant", "Cinematic", "Bleak", "Warm", "Cold",
    "High contrast", "Old film", "Zone", "Custom look",
)


class GraphicsScreen(DeckScreen):
    came_from = "settings"

    def build(self) -> None:
        # (segmented, variable) for the frame generation rows.
        self._fg_rows: list[tuple[DeckSegmented, str]] = []
        self._section(tr("Frame generation"))
        self._caption(tr("Lossless Scaling Frame Generation for smoother gameplay. Plays best above 40 FPS but adds a little input lag."))
        self.fg_status = deck_label("", role="caption", wrap=True)
        self.fg_status.setObjectName("deckBodyWarn")
        self.body.addWidget(self.fg_status)
        self.fg_row = DeckToggleRow(tr("Frame generation"))
        self.fg_row.toggled.connect(lambda on: self._set_frame_gen(None, on))
        self.body.addWidget(self.fg_row)
        for label, key, choices in FrameGenCard._ROWS:
            segmented = self._choice_row(
                tr(label),
                [(tr(text), value) for text, value in choices],
                lambda value, key=key: self._set_frame_gen(key, value),
            )
            self._fg_rows.append((segmented, key))
        self.dll_row = DeckRow(tr("Choose DLL"))
        self.dll_row.activated.connect(self._choose_dll)
        self.body.addWidget(self.dll_row)

        self._section(tr("Performance overlay"))
        self._caption(tr("Changes show in a running game right away."))
        self.mangohud_row = DeckToggleRow(tr("Performance overlay"))
        self.mangohud_row.toggled.connect(lambda on: self._toggle_option(overlays.MANGOHUD, on))
        self.body.addWidget(self.mangohud_row)
        self.preset_row = self._picker_row(
            tr("Preset"), [(tr(label), key) for label, key in _MANGOHUD_PRESETS], "mangohud_preset"
        )
        self.layout_choice = self._choice_row(
            tr("Layout"),
            list(zip((tr("Box"), tr("Bar")), overlays.MANGOHUD_LAYOUTS)),
            lambda value: self._save(mangohud_layout=value),
        )
        self.position_row = self._picker_row(
            tr("Position"),
            [(tr(label), key) for label, key in zip(_POSITIONS, overlays.MANGOHUD_POSITIONS)],
            "mangohud_position",
        )
        self.size_choice = self._choice_row(
            tr("Size"),
            list(zip((tr("Small"), tr("Medium"), tr("Large")), overlays.MANGOHUD_SIZES)),
            lambda value: self._save(mangohud_size=value),
        )
        self.fps_row = self._picker_row(
            tr("FPS limit"),
            [(str(limit) if limit else tr("Off"), limit) for limit in overlays.MANGOHUD_FPS_LIMITS],
            "mangohud_fps_limit",
        )
        self.colours_row = DeckToggleRow(tr("Colour-code FPS and load"))
        self.colours_row.toggled.connect(lambda on: self._save(mangohud_colours=bool(on)))
        self.body.addWidget(self.colours_row)

        self._section(tr("Image effects"))
        self._caption(tr("Changes show in a running game right away."))
        self.vkbasalt_row = DeckToggleRow(tr("Image effects"))
        self.vkbasalt_row.toggled.connect(lambda on: self._toggle_option(overlays.VKBASALT, on))
        self.body.addWidget(self.vkbasalt_row)
        self.look_row = self._picker_row(
            tr("Look"),
            [(tr(label), key) for label, key in zip(_LOOKS, overlays.VKBASALT_PRESETS)],
            "vkbasalt_preset",
        )
        self.sharpness_choice = self._choice_row(
            tr("Sharpening"),
            list(zip((tr("Off"), tr("Light"), tr("Medium"), tr("Strong")), overlays.VKBASALT_SHARPNESS)),
            lambda value: self._save(vkbasalt_sharpness=value),
        )
        self.sharpener_choice = self._choice_row(
            tr("Sharpening method"),
            list(zip(("CAS", "DLS"), overlays.VKBASALT_SHARPENERS)),
            lambda value: self._save(vkbasalt_sharpener=value),
        )
        self.aa_choice = self._choice_row(
            tr("Anti-aliasing"),
            list(zip((tr("Off"), "FXAA", "SMAA"), overlays.VKBASALT_AA)),
            lambda value: self._save(vkbasalt_aa=value),
        )
        self.body.addStretch(1)

    # -- building blocks ----------------------------------------------------
    def _section(self, text: str) -> None:
        self.body.addWidget(deck_label(text, role="rowTitle"))

    def _caption(self, text: str) -> None:
        self.body.addWidget(deck_label(text, role="caption", wrap=True))

    def _choice_row(self, title: str, options, on_change) -> DeckSegmented:
        """A row with its choices inline, as Settings' "When COMMANDER starts"."""
        row = DeckRow(title, chevron=False)
        row.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row.value_label.hide()
        segmented = DeckSegmented(options)
        segmented.setFixedWidth(px(150 * len(options)))
        segmented.changed.connect(on_change)
        row.layout().addWidget(segmented)
        self.body.addWidget(row)
        return segmented

    def _picker_row(self, title: str, options, key: str) -> DeckRow:
        row = DeckRow(title)
        row.options = options

        def _pick() -> None:
            picker = DeckPicker("", options, overlays.settings()[key])
            picker.chosen.connect(lambda value: self._picked(key, value))
            self.window.show_overlay(picker_overlay(self.window, title, picker))

        row.activated.connect(_pick)
        self.body.addWidget(row)
        return row

    def _picked(self, key: str, value) -> None:
        self.window.dismiss_overlay()
        self._save(**{key: value})

    # -- state ----------------------------------------------------------------
    def refresh(self) -> None:
        data = overlays.settings()
        text = self._options()
        for row, key in (
            (self.preset_row, "mangohud_preset"),
            (self.position_row, "mangohud_position"),
            (self.fps_row, "mangohud_fps_limit"),
            (self.look_row, "vkbasalt_preset"),
        ):
            row.set_value(next((label for label, value in row.options if value == data[key]), ""))
        for segmented, key in (
            (self.layout_choice, "mangohud_layout"),
            (self.size_choice, "mangohud_size"),
            (self.sharpness_choice, "vkbasalt_sharpness"),
            (self.sharpener_choice, "vkbasalt_sharpener"),
            (self.aa_choice, "vkbasalt_aa"),
        ):
            segmented.set_value(data[key])
        self.colours_row.set_checked(data["mangohud_colours"])
        # My own MangoHud.conf decides all of it then.
        own = data["mangohud_preset"] == "own"
        for widget in (self.layout_choice, self.position_row, self.size_choice, self.fps_row, self.colours_row):
            widget.setEnabled(not own)
        self.mangohud_row.set_checked(launch_options.is_on(text, overlays.MANGOHUD))
        self.mangohud_row.setEnabled(overlays.mangohud_available())
        self.vkbasalt_row.set_checked(launch_options.is_on(text, overlays.VKBASALT))
        self._refresh_frame_gen(text)

    def _refresh_frame_gen(self, text: str) -> None:
        on = launch_options.is_on(text, launch_options.FRAME_GEN)
        dll = launch_options.lossless_dll()
        ready = launch_options.frame_gen_ready(dll)
        if dll is None:
            status = tr("Frame generation needs Lossless Scaling (paid, on Steam). In Desktop Mode, set its Properties > Compatibility to a Proton version and install it: COMMANDER finds its Lossless.dll there.")
        elif not ready and dll.name.lower() == launch_options.BETA_DLL.lower():
            status = tr("lsfg-vk.dll is from Lossless Scaling's lsfg-vk beta. In Steam, switch Lossless Scaling back to its usual version (Properties > Betas > None), or pick its Lossless.dll.")
        elif not ready:
            status = tr("That file isn't one frame generation can use. Pick Lossless.dll from Lossless Scaling.")
        elif game_has_frame_gen() is False:
            status = tr("Frame generation wasn't on when the game started, so changes apply from the next launch. In co-op, let COMMANDER restart Steam when it asks.")
        else:
            status = ""
        self.dll_row.set_value(launch_options.dll_label(dll) if dll else tr("No DLL found"))
        self.fg_status.setText(status)
        self.fg_status.setVisible(bool(status))
        self.fg_row.set_checked(on)
        # Left on with no DLL any more: it can still be switched off.
        self.fg_row.setEnabled(ready or on)
        for segmented, key in self._fg_rows:
            current = launch_options.var(text, key)
            if key == launch_options.FG_MULTIPLIER:
                current = current or "2"
            segmented.set_value(current if on else object())
            segmented.setEnabled(on and ready)

    def _choose_dll(self) -> None:
        """Lossless Scaling's Lossless.dll, picked by hand."""
        from ..mod_installer import pick_file

        dll = launch_options.lossless_dll()
        steam = Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common"
        start = dll.parent if dll else steam if steam.is_dir() else Path.home()

        def _picked(path: Path) -> None:
            gui_settings.save_gui_settings(lossless_dll=str(path))
            self.refresh()

        pick_file(self.window, tr("Select Lossless Scaling's DLL"), start, (".dll",), _picked)

    @staticmethod
    def _options() -> str:
        return gui_settings.load_gui_settings().get("custom_launch_options") or ""

    def _save_options(self, text: str) -> None:
        gui_settings.save_gui_settings(custom_launch_options=text)
        if self.window.session_active():
            try:  # the running game rereads its frame generation config
                launch_options.live_frame_gen(text)
            except OSError:
                pass  # the setting still applies from the next launch
        self.refresh()

    def _set_frame_gen(self, key: str | None, value) -> None:
        text = self._options()
        if key is None:
            text = launch_options.frame_gen(text, bool(value))
        else:
            text = launch_options.set_var(text, key, value)
        self._save_options(text)

    def _toggle_option(self, option: str, on: bool) -> None:
        self._save_options(launch_options.toggle(self._options(), option, on))

    def _save(self, **changes) -> None:
        data = overlays.settings()
        data.update(changes)
        # A bar is as wide as the screen: MangoHud can't put it on the right.
        # The newer choice wins, as on the desktop page.
        if "mangohud_position" in changes and "right" in changes["mangohud_position"]:
            changes["mangohud_layout"] = "box"
        elif changes.get("mangohud_layout") == "bar":
            changes["mangohud_position"] = data["mangohud_position"].replace("right", "left")
        gui_settings.save_gui_settings(**changes)
        data = overlays.settings()
        try:
            if data["mangohud_preset"] != "own":
                overlays.write_mangohud_config(data)  # a running game reloads it
            overlays.write_vkbasalt_config(data)  # and so does the bundled vkBasalt
        except OSError as exc:
            self.window.notify(str(exc), 6000)
        self.refresh()

    def on_back(self) -> bool:
        if getattr(self.window, "_game_overlay_from", None) is not None:
            self.window.close_game_overlay()  # B returns to the game
            return True
        self.window.set_page(self.came_from or "settings")
        return True
