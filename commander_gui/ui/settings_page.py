"""Settings page: launch behaviour, launcher defaults and appearance,
grouped into tabs like the Multiplayer page's.

Start page and default runner persist to the GUI settings file and take
effect immediately or on the next launch as described next to each
control. Themes and the UI scale apply live.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, __version_label__, gui_settings, overlays, plugins
from ..config import logs_dir
from ..deck_launch import steam_deck_model
from ..discord_rpc import (
    DEFAULT_CLIENT_ID,
    DETAILS_TEXT,
    effective_client_id,
    probe_discord,
    start_presence,
    stop_presence,
    update_presence,
)
from ..i18n import LANGUAGE_INFO
from ..launcher import (
    LaunchError,
    build_runner_tool_command,
    find_extra_protons,
    launch_detached,
)
from ..self_update import commander_appimage_path
from ..steam_shortcuts import (
    ShortcutsFileError,
    add_to_steam,
)
from ..themes import LIVE_THEMES, LIVE_TIME_CHOICES, THEME_INFO, active_theme, active_theme_tokens
from ..updates import is_unstable_version, newer_unstable_tag
from . import graphics_window
from .common import (
    OK_GREEN,
    WARN,
    BackgroundTask,
    NoWheelComboBox,
    discord_presence_state,
    make_card,
    game_running,
    open_in_file_manager,
    pick_shortcuts_vdf,
    populate_runner_combo,
    steam_running,
    tr,
)
from .dashboard import _card_title
from .dialogs import MessageBox as QMessageBox
from .moddb_plugin import controller as moddb_controller
from .multiplayer_page import (
    _columns,
    _divider,
    _hint,
    _panel,
    _section,
    _Stat,
    _tab,
    _vline,
)


def _swatch(color: str) -> QFrame:
    frame = QFrame()
    frame.setFixedSize(18, 18)
    frame.setStyleSheet(
        f"background-color: {color}; border: 1px solid rgba(0, 0, 0, 90); "
        "border-radius: 3px;"
    )
    return frame


def _action(text: str) -> QPushButton:
    """A compact button for a settings row: the action, not the setting."""
    button = QPushButton(text)
    button.setObjectName("settingAction")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _combo(width: int = 260) -> NoWheelComboBox:
    combo = NoWheelComboBox()
    combo.setMinimumWidth(width)
    return combo


def _live_hint() -> QLabel:
    """The Graphics cards' "shows right away" line, in the accent green."""
    hint = _hint(tr("Changes show in a running game right away."))
    hint.setObjectName("accent")
    return hint


def _card(title: str, text: str = "") -> tuple[QFrame, QVBoxLayout]:
    """A settings group: heading, an optional dim line under it, then rows."""
    frame, layout = _section(title)
    layout.setSpacing(6)
    if text:
        layout.addWidget(_hint(text))
    layout.addSpacing(4)
    return frame, layout


def _setting_row(title: str, text: str, *controls: QWidget) -> QWidget:
    """The setting's name and what it does on the left, its controls on the
    right, like a system settings panel."""
    row = QWidget()
    row.setObjectName("panelTransparent")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 6, 0, 6)
    layout.setSpacing(16)
    body = QVBoxLayout()
    body.setSpacing(2)
    if title:
        name = QLabel(title)
        name.setObjectName("settingTitle")
        name.setWordWrap(True)
        body.addWidget(name)
    if text:
        body.addWidget(_hint(text))
    layout.addLayout(body, 1)
    for control in controls:
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


def _check_row(check: QCheckBox, text: str) -> QWidget:
    """A checkbox with a dim line under its label."""
    row = QWidget()
    row.setObjectName("panelTransparent")
    layout = QVBoxLayout(row)
    layout.setContentsMargins(0, 4, 0, 4)
    layout.setSpacing(0)
    layout.addWidget(check)
    hint = _hint(text)
    # Lined up with the label, past the 18px indicator and 8px spacing.
    hint.setContentsMargins(26, 0, 0, 0)
    layout.addWidget(hint)
    return row


def _rows(layout: QVBoxLayout, *rows: QWidget) -> None:
    """Rows split by dividers."""
    for index, row in enumerate(rows):
        if index:
            layout.addWidget(_divider())
        layout.addWidget(row)


#: Tab names, in order. Each one is built by the matching ``_<name>_tab``.
_TABS = (
    ("General", "general"),
    ("Game", "game"),
    ("Graphics", "graphics"),
    ("Appearance", "appearance"),
    ("Integrations", "integrations"),
    ("Plugins", "plugins"),
    ("Maintenance", "maintenance"),
)


class GraphicsPanel(QWidget):
    """Settings > Graphics: MangoHud and vkBasalt. The graphics overlay shows
    the two cards (mangohud_card, vkbasalt_card) on tabs of its own."""

    def __init__(self, *, loose: bool = False) -> None:
        super().__init__()
        self._loose = loose  # cards left out of the panel, for the overlay's tabs
        self.setObjectName("panelTransparent")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._graphics_tab())
        self.render()

    def _graphics_tab(self) -> QWidget:
        page, layout = _panel()
        layout.setSpacing(12)

        mangohud, box = _card(
            tr("Performance overlay"),
            tr("MangoHud: FPS, temperatures and more over the game. Switch it on with Performance overlay on the Play page."),
        )
        box.addWidget(_live_hint())
        self._mangohud_preset = _combo(180)
        for label, key in (
            (tr("FPS only"), "fps"), (tr("Basic"), "basic"), (tr("Full"), "full"),
            (tr("Custom selection"), "custom"), (tr("My own MangoHud.conf"), "own"),
        ):
            self._mangohud_preset.addItem(label, key)
        self._mangohud_preset.currentIndexChanged.connect(self._on_mangohud_preset)
        # "Show" above two columns of checkboxes across the card's full width.
        items = QWidget()
        items.setObjectName("panelTransparent")
        items_layout = QVBoxLayout(items)
        items_layout.setContentsMargins(0, 6, 0, 6)
        items_layout.setSpacing(6)
        show_title = QLabel(tr("Show"))
        show_title.setObjectName("settingTitle")
        items_layout.addWidget(show_title)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(4)
        items_layout.addLayout(grid)
        self._mangohud_items: dict[str, QCheckBox] = {}
        # Most used first, down the left column then the right. The overlay
        # keeps its own order whatever the order here.
        mangohud_items = (
            ("fps", tr("FPS")),
            ("frametime", tr("Frame time (ms)")),
            ("graph", tr("Frame time graph")),
            ("lows", tr("1% and 0.1% low FPS")),
            ("gpu", tr("GPU load and temperature")),
            ("cpu", tr("CPU load and temperature")),
            ("memory", tr("RAM and VRAM use")),
            ("cores", tr("Load of each CPU core")),
            ("procmem", tr("Game's memory use")),
            ("time", tr("Clock")),
            ("disk", tr("Disk reads and writes")),
            ("throttling", tr("GPU slowing down (heat or power)")),
            ("battery", tr("Battery")),
        )
        per_column = (len(mangohud_items) + 1) // 2
        for index, (key, label) in enumerate(mangohud_items):
            check = QCheckBox(label)
            check.toggled.connect(self._on_mangohud_item)
            grid.addWidget(check, index % per_column, index // per_column)
            self._mangohud_items[key] = check
        self._mangohud_items["throttling"].setToolTip(tr("Can cause lag on NVIDIA RTX 30 cards."))
        self._mangohud_position = _combo(180)
        for label, key in zip(
            (tr("Top left"), tr("Top right"), tr("Bottom left"), tr("Bottom right")),
            overlays.MANGOHUD_POSITIONS,
        ):
            self._mangohud_position.addItem(label, key)
        self._mangohud_size = _combo(180)
        for label, key in zip((tr("Small"), tr("Medium"), tr("Large")), overlays.MANGOHUD_SIZES):
            self._mangohud_size.addItem(label, key)
        self._mangohud_key = _combo(180)
        for key in overlays.MANGOHUD_KEYS:
            self._mangohud_key.addItem(key.replace("_", " "), key)
        self._mangohud_layout = _combo(180)
        for label, key in zip((tr("Box"), tr("Bar")), overlays.MANGOHUD_LAYOUTS):
            self._mangohud_layout.addItem(label, key)
        self._mangohud_fps_limit = _combo(180)
        for limit in overlays.MANGOHUD_FPS_LIMITS:
            self._mangohud_fps_limit.addItem(str(limit) if limit else tr("Off"), limit)
        for combo in (self._mangohud_size, self._mangohud_key, self._mangohud_fps_limit):
            combo.currentIndexChanged.connect(self._save_graphics)
        self._mangohud_position.currentIndexChanged.connect(self._on_mangohud_position)
        self._mangohud_layout.currentIndexChanged.connect(self._on_mangohud_layout)
        self._mangohud_colours = QCheckBox(tr("Colour-code FPS and load"))
        self._mangohud_start_hidden = QCheckBox(tr("Start with the overlay hidden"))
        for check in (self._mangohud_colours, self._mangohud_start_hidden):
            check.toggled.connect(self._save_graphics)
        background_row, self._mangohud_background = self._slider(0, 100)
        overlay_row, self._mangohud_transparency = self._slider(0, 90)
        _rows(
            box,
            _setting_row(tr("Preset"), tr("My own MangoHud.conf uses the file in ~/.config/MangoHud."), self._mangohud_preset),
            items,
            _check_row(self._mangohud_colours, tr("Red when FPS is low or the GPU or CPU is close to full load.")),
            _setting_row(tr("FPS limit"), tr("Needs Performance overlay on. Still works while hidden with the on/off key. Frame generation's frames count too."), self._mangohud_fps_limit),
            _setting_row(tr("Layout"), tr("Bar puts everything on one line across the screen, at the top or bottom."), self._mangohud_layout),
            _setting_row(tr("Position"), tr("A bar can only sit on the left, so a right corner switches Layout to Box."), self._mangohud_position),
            _setting_row(tr("Size"), "", self._mangohud_size),
            _setting_row(tr("Background transparency"), tr("0% is solid."), background_row),
            _setting_row(tr("Overlay transparency"), tr("The text and graphs too."), overlay_row),
            _setting_row(tr("On/off key"), tr("Switches the overlay off and on in-game."), self._mangohud_key),
            _check_row(self._mangohud_start_hidden, tr("Press the on/off key in-game to bring it in.")),
        )
        box.addStretch(1)

        vkbasalt, box = _card(
            tr("Image effects"),
            tr("vkBasalt: sharpening, anti-aliasing and colour looks. Switch it on with Image effects on the Play page."),
        )
        box.addWidget(_live_hint())
        self._vkbasalt_preset = _combo(180)
        for label, key in zip(
            (tr("Natural"), tr("Vibrant"), tr("Cinematic"), tr("Bleak"), tr("Warm"), tr("Cold"),
             tr("High contrast"), tr("Old film"), tr("Zone"), tr("Custom look")),
            overlays.VKBASALT_PRESETS,
        ):
            self._vkbasalt_preset.addItem(label, key)
        self._vkbasalt_aa = _combo(180)
        for label, key in zip((tr("Off"), "FXAA", "SMAA"), overlays.VKBASALT_AA):
            self._vkbasalt_aa.addItem(label, key)
        self._vkbasalt_sharpener = _combo(180)
        for label, key in zip(("CAS", "DLS"), overlays.VKBASALT_SHARPENERS):
            self._vkbasalt_sharpener.addItem(label, key)
        self._vkbasalt_sharpness = _combo(180)
        for label, key in zip((tr("Off"), tr("Light"), tr("Medium"), tr("Strong")), overlays.VKBASALT_SHARPNESS):
            self._vkbasalt_sharpness.addItem(label, key)
        self._vkbasalt_key = _combo(180)
        for key in overlays.VKBASALT_KEYS:
            self._vkbasalt_key.addItem(key, key)
        self._vkbasalt_preset.currentIndexChanged.connect(self._on_vkbasalt_look)
        for combo in (self._vkbasalt_aa, self._vkbasalt_sharpener, self._vkbasalt_sharpness, self._vkbasalt_key):
            combo.currentIndexChanged.connect(self._save_graphics)
        self._vkbasalt_colours: dict[str, QSlider] = {}
        colour_rows = []
        for name, label in zip(
            overlays.VKBASALT_COLOURS,
            (tr("Saturation"), tr("Contrast"), tr("Brightness"), tr("Warmth"), tr("Tint")),
        ):
            row, self._vkbasalt_colours[name] = self._slider(-50, 50, "", self._on_vkbasalt_colour)
            colour_rows.append(_setting_row(label, "", row))
        self._vkbasalt_start_off = QCheckBox(tr("Start with the effects off"))
        self._vkbasalt_start_off.toggled.connect(self._save_graphics)
        _rows(
            box,
            _setting_row(tr("Sharpening"), "", self._vkbasalt_sharpness),
            _setting_row(tr("Sharpening method"), tr("DLS also cleans up grain and noise."), self._vkbasalt_sharpener),
            _setting_row(tr("Anti-aliasing"), tr("Smooths jagged edges. SMAA looks better, FXAA costs less."), self._vkbasalt_aa),
            _setting_row(
                tr("Look"),
                tr("A starting point for the colours. The sliders below fine-tune it."),
                self._vkbasalt_preset,
            ),
            *colour_rows,
            _setting_row(
                tr("On/off key"),
                tr("Switches the effects off and on in-game."),
                self._vkbasalt_key,
            ),
            _check_row(self._vkbasalt_start_off, tr("Press the on/off key in-game to bring them in.")),
        )
        box.addStretch(1)
        self.mangohud_card, self.vkbasalt_card = mangohud, vkbasalt
        if not self._loose:
            _columns(layout, mangohud, vkbasalt)
        return page

    def _slider(self, minimum: int, maximum: int, suffix: str = "%", on_change=None) -> tuple[QWidget, QSlider]:
        """A slider with its value beside it."""
        row = QWidget()
        row.setObjectName("panelTransparent")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setProperty("signed", minimum < 0)
        slider.setSingleStep(5)
        slider.setPageStep(10)
        slider.setFixedWidth(130)
        value = QLabel()
        value.setMinimumWidth(42)
        # Signed sliders show +10 / -10; the label also follows render.
        slider.show_value = lambda v: value.setText(f"{v:+d}" if minimum < 0 and v else f"{v}{suffix}")
        slider.valueChanged.connect(slider.show_value)
        slider.valueChanged.connect(on_change or self._save_graphics)
        layout.addWidget(slider)
        layout.addWidget(value)
        return row, slider

    def render(self) -> None:
        data = overlays.settings()
        for combo, key in (
            (self._mangohud_preset, "mangohud_preset"),
            (self._mangohud_position, "mangohud_position"),
            (self._mangohud_size, "mangohud_size"),
            (self._mangohud_key, "mangohud_key"),
            (self._mangohud_layout, "mangohud_layout"),
            (self._mangohud_fps_limit, "mangohud_fps_limit"),
            (self._vkbasalt_preset, "vkbasalt_preset"),
            (self._vkbasalt_sharpness, "vkbasalt_sharpness"),
            (self._vkbasalt_sharpener, "vkbasalt_sharpener"),
            (self._vkbasalt_aa, "vkbasalt_aa"),
            (self._vkbasalt_key, "vkbasalt_key"),
        ):
            combo.blockSignals(True)
            combo.setCurrentIndex(max(combo.findData(data[key]), 0))
            combo.blockSignals(False)
        for key, check in self._mangohud_items.items():
            check.blockSignals(True)
            check.setChecked(key in data["mangohud_items"])
            check.blockSignals(False)
        for slider, key in (
            (self._mangohud_background, "mangohud_background_transparency"),
            (self._mangohud_transparency, "mangohud_transparency"),
            *((slider, f"vkbasalt_{name}") for name, slider in self._vkbasalt_colours.items()),
        ):
            slider.blockSignals(True)
            slider.setValue(data[key])
            slider.blockSignals(False)
            slider.show_value(data[key])
        for check, key in (
            (self._mangohud_colours, "mangohud_colours"),
            (self._mangohud_start_hidden, "mangohud_start_hidden"),
            (self._vkbasalt_start_off, "vkbasalt_start_off"),
        ):
            check.blockSignals(True)
            check.setChecked(data[key])
            check.blockSignals(False)
        self._sync_mangohud_enabled()
        self._sync_mangohud_positions()

    def _sync_mangohud_positions(self) -> None:
        # A bar is as wide as the screen: MangoHud can't put it on the right.
        position = self._mangohud_position.currentData()
        if self._mangohud_layout.currentData() == "bar" and "right" in position:
            self._mangohud_position.blockSignals(True)
            self._mangohud_position.setCurrentIndex(self._mangohud_position.findData(position.replace("right", "left")))
            self._mangohud_position.blockSignals(False)

    def _on_mangohud_position(self) -> None:
        # The newer choice wins: a right corner needs the box layout.
        if "right" in self._mangohud_position.currentData() and self._mangohud_layout.currentData() == "bar":
            self._mangohud_layout.blockSignals(True)
            self._mangohud_layout.setCurrentIndex(self._mangohud_layout.findData("box"))
            self._mangohud_layout.blockSignals(False)
        self._save_graphics()

    def _on_mangohud_layout(self) -> None:
        self._sync_mangohud_positions()
        self._save_graphics()

    def _sync_mangohud_enabled(self) -> None:
        # MangoHud.conf decides all of it then.
        own = self._mangohud_preset.currentData() == "own"
        for widget in (
            *self._mangohud_items.values(), self._mangohud_position, self._mangohud_size,
            self._mangohud_key, self._mangohud_layout, self._mangohud_background, self._mangohud_transparency,
            self._mangohud_fps_limit, self._mangohud_colours, self._mangohud_start_hidden,
        ):
            widget.setEnabled(not own)

    def _on_mangohud_preset(self) -> None:
        items = overlays.MANGOHUD_PRESETS.get(self._mangohud_preset.currentData())
        if items is not None:
            for key, check in self._mangohud_items.items():
                check.blockSignals(True)
                check.setChecked(key in items)
                check.blockSignals(False)
        self._sync_mangohud_enabled()
        self._save_graphics()

    def _on_vkbasalt_look(self) -> None:
        look = self._vkbasalt_preset.currentData()
        values = overlays.VKBASALT_LOOKS.get(look) or overlays.settings()["vkbasalt_custom"]
        for slider, value in zip(self._vkbasalt_colours.values(), values):
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
            slider.show_value(value)
        self._save_graphics()

    def _on_vkbasalt_colour(self) -> None:
        # Moving a slider by hand makes it a custom look.
        self._vkbasalt_preset.blockSignals(True)
        self._vkbasalt_preset.setCurrentIndex(self._vkbasalt_preset.findData("custom"))
        self._vkbasalt_preset.blockSignals(False)
        self._save_graphics()

    def _on_mangohud_item(self) -> None:
        # Ticking by hand makes it a custom set.
        self._mangohud_preset.blockSignals(True)
        self._mangohud_preset.setCurrentIndex(self._mangohud_preset.findData("custom"))
        self._mangohud_preset.blockSignals(False)
        self._save_graphics()

    def _save_graphics(self) -> None:
        # Only the custom look's slider values are saved: a named look's come
        # from VKBASALT_LOOKS, and saving them would overwrite the custom look.
        colours = (
            {f"vkbasalt_{name}": slider.value() for name, slider in self._vkbasalt_colours.items()}
            if self._vkbasalt_preset.currentData() == "custom" else {}
        )
        gui_settings.save_gui_settings(
            mangohud_preset=self._mangohud_preset.currentData(),
            mangohud_items=[key for key, check in self._mangohud_items.items() if check.isChecked()],
            mangohud_position=self._mangohud_position.currentData(),
            mangohud_size=self._mangohud_size.currentData(),
            mangohud_key=self._mangohud_key.currentData(),
            mangohud_layout=self._mangohud_layout.currentData(),
            mangohud_background_transparency=self._mangohud_background.value(),
            mangohud_transparency=self._mangohud_transparency.value(),
            mangohud_fps_limit=self._mangohud_fps_limit.currentData(),
            mangohud_start_hidden=self._mangohud_start_hidden.isChecked(),
            mangohud_colours=self._mangohud_colours.isChecked(),
            vkbasalt_preset=self._vkbasalt_preset.currentData(),
            vkbasalt_sharpness=self._vkbasalt_sharpness.currentData(),
            vkbasalt_key=self._vkbasalt_key.currentData(),
            vkbasalt_sharpener=self._vkbasalt_sharpener.currentData(),
            vkbasalt_aa=self._vkbasalt_aa.currentData(),
            vkbasalt_start_off=self._vkbasalt_start_off.isChecked(),
            **colours,
        )
        if self._mangohud_preset.currentData() != "own":
            overlays.write_mangohud_config()  # a running game reloads it
        overlays.write_vkbasalt_config()  # and so does the bundled vkBasalt


class SettingsPage(QWidget):
    """Launch behaviour, launcher defaults, appearance and themes, in tabs."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._radios: dict[str, QRadioButton] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 20)
        outer.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        self._scroll = scroll

        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(14)
        scroll.setWidget(content)

        root.addWidget(self._header_card())
        root.addWidget(self._tabs_card())
        root.addStretch(1)

        self.refresh()

    # ----------------------------------------------------------------- header
    def _header_card(self) -> QWidget:
        card, layout = make_card()
        row = QHBoxLayout()
        row.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addWidget(_card_title(tr("Settings")))
        text.addWidget(
            _hint(tr("Changes save as you make them. Most apply right away."))
        )
        text.addStretch(1)
        row.addLayout(text, 1)
        version = _Stat(tr("COMMANDER version"))
        version.set(__version_label__)
        row.addWidget(version)
        self._build_stat = _Stat(tr("Build"))
        self._build_label = self._build_stat.value
        row.addWidget(self._build_stat)
        layout.addLayout(row)
        return card

    # ------------------------------------------------------------------- tabs
    def _tabs_card(self) -> QWidget:
        # Same sub-tabs as the Multiplayer page: buttons over a divider,
        # one page shown at a time.
        card, layout = make_card()
        layout.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(4)
        self.tab_group = QButtonGroup(self)
        self._tab_pages: list[QWidget] = []
        body = QVBoxLayout()
        body.setSpacing(0)
        for index, (name, key) in enumerate(_TABS):
            button = _tab(tr(name), "subTab")
            self.tab_group.addButton(button, index)
            header.addWidget(button)
            page = getattr(self, f"_{key}_tab")()
            body.addWidget(page)
            self._tab_pages.append(page)
        self.tab_group.idClicked.connect(self._show_tab)
        header.addStretch(1)
        tabs = QVBoxLayout()
        tabs.setSpacing(0)
        tabs.addLayout(header)
        tabs.addWidget(_divider())
        layout.addLayout(tabs)
        layout.addLayout(body)
        self.tab_group.button(0).setChecked(True)
        self._show_tab(0)
        return card

    def _show_tab(self, index: int) -> None:
        for i, page in enumerate(self._tab_pages):
            page.setVisible(i == index)

    def show_build(self) -> None:
        """The General tab, scrolled to COMMANDER build (Welcome's "Unstable
        build available" link)."""
        self.tab_group.button(0).setChecked(True)
        self._show_tab(0)
        QTimer.singleShot(0, lambda: self._scroll.ensureWidgetVisible(self._build_box, 0, 80))

    def show_plugins(self) -> None:
        """The Plugins tab (the Mod manager's ModDB button, before it's installed)."""
        index = next(i for i, (_name, key) in enumerate(_TABS) if key == "plugins")
        self.tab_group.button(index).setChecked(True)
        self._show_tab(index)

    def _general_tab(self) -> QWidget:
        page, layout = _panel()
        layout.setSpacing(12)

        startup, box = _card(tr("Startup"))
        self._start_page_combo = _combo()
        self._start_page_combo.currentIndexChanged.connect(self._on_start_page)
        # Shown on every machine, not only Deck hardware: Deck Mode's "When
        # COMMANDER starts" can set "Always" on a desktop PC too, and this
        # is the way back from the full interface.
        self._deck_mode_combo = _combo()
        # The same two choices as Deck Mode's "When COMMANDER starts".
        for label, value in (
            (tr("Steam Deck"), "always"),
            (tr("Desktop"), "never"),
        ):
            self._deck_mode_combo.addItem(label, value)
        self._deck_mode_combo.currentIndexChanged.connect(self._on_deck_mode)
        _rows(
            box,
            _setting_row(
                tr("Page on startup"),
                tr("Choose the page COMMANDER opens when it starts."),
                self._start_page_combo,
            ),
            _setting_row(
                tr("When COMMANDER starts"),
                tr("Open the desktop interface or Steam Deck Mode."),
                self._deck_mode_combo,
            ),
        )
        layout.addWidget(startup)

        behaviour, box = _card(tr("Behaviour"))
        self._autostart_check = QCheckBox(tr("Start COMMANDER when I log in"))
        self._autostart_check.toggled.connect(self._on_autostart_toggled)
        self._welcome_check = QCheckBox(tr("Show the Welcome screen on startup"))
        self._welcome_check.toggled.connect(
            lambda on: gui_settings.save_gui_settings(welcome_hidden=not on)
        )
        self._update_notify_check = QCheckBox(tr("Notify me about GAMMA and COMMANDER updates"))
        self._update_notify_check.toggled.connect(
            lambda on: gui_settings.save_gui_settings(update_notifications=on)
        )
        _rows(
            box,
            _check_row(
                self._autostart_check,
                tr("Start COMMANDER automatically when you log in."),
            ),
            _check_row(
                self._welcome_check,
                tr("The latest COMMANDER patch notes and links to GitHub, Discord and the credits."),
            ),
            _check_row(
                self._update_notify_check,
                tr("Notify you when a new GAMMA or COMMANDER version is out, once per version."),
            ),
        )
        layout.addWidget(behaviour)

        # Stable or unstable COMMANDER: which one is running (header tile),
        # and one button to move to the other. The update channel follows
        # the build.
        self._build_box, box = _card(tr("COMMANDER build"))
        self._build_note = _hint()
        self._switch_build_button = _action("")
        self._switch_build_button.clicked.connect(self._on_switch_build)
        row = _setting_row("", "", self._switch_build_button)
        row.layout().itemAt(0).layout().addWidget(self._build_note)
        box.addWidget(row)
        #: The unstable tag worth switching to (newer than stable), once the
        #: background check has answered; None while unknown or none.
        self._unstable_offer: str | None = None
        self._unstable_checked = False
        self._unstable_task: BackgroundTask | None = None
        self._export_task: BackgroundTask | None = None
        layout.addWidget(self._build_box)
        self._render_build()
        return page

    def _game_tab(self) -> QWidget:
        page, layout = _panel()
        layout.setSpacing(12)

        runner, box = _card(tr("Runner"))
        self._runner_combo = _combo(320)
        self._runner_combo.currentIndexChanged.connect(self._on_runner_changed)
        self._winecfg_button = _action(tr("Open Winecfg"))
        self._winecfg_button.clicked.connect(self._open_winecfg)
        _rows(
            box,
            _setting_row(
                tr("Default runner"),
                tr("Choose the runner used by default on the Play page. You can override it for each launch."),
                self._runner_combo,
            ),
            _setting_row(
                tr("Wine configuration"),
                tr("Open Wine Configuration for the default runner and prefix."),
                self._winecfg_button,
            ),
        )
        layout.addWidget(runner)

        display, box = _card(tr("MO2 display"))
        self._display_scale_combo = _combo(200)
        for percent, dpi in ((100, 96), (125, 120), (150, 144), (175, 168), (200, 192)):
            self._display_scale_combo.addItem(f"{percent}% ({dpi} DPI)", dpi)
        self._display_scale_combo.currentIndexChanged.connect(
            self._on_display_scale_changed
        )
        self._apply_display_scale_button = _action(tr("Apply"))
        # A minimum width, not a fixed one: longer translations (e.g. Polish
        # "Zastosowano" for "Applied") grow the button instead of clipping.
        self._apply_display_scale_button.setMinimumWidth(110)
        self._apply_display_scale_button.clicked.connect(self._apply_display_scale)
        _rows(
            box,
            _setting_row(
                tr("MO2 Display Scale"),
                tr("125% is recommended for small MO2 text. Restart MO2 after applying."),
                self._display_scale_combo,
                self._apply_display_scale_button,
            ),
        )
        layout.addWidget(display)
        return page

    def _graphics_tab(self) -> QWidget:
        page, layout = _panel()
        layout.setSpacing(12)
        overlay, box = _card(tr("Graphics overlay"), tr("These settings in a window over the running game."))
        self._overlay_key = _combo(230)
        for key, (label, _keysym, _modifiers) in graphics_window.KEYS.items():
            self._overlay_key.addItem(tr(label) if label else tr("Off"), key)
        self._overlay_key.currentIndexChanged.connect(self._on_overlay_key)
        rows = [_setting_row(
            tr("Key"),
            tr("Works while the game runs, not in Steam Deck Game Mode. Esc closes the window."),
            self._overlay_key,
        )]
        if graphics_window.on_umbriel():
            rule = QPushButton(tr("Copy rule"))
            rule.clicked.connect(lambda: graphics_window.offer_umbriel_rule(self))
            rows.append(_setting_row(
                tr("Umbriel window rule"),
                tr("Umbriel opens the overlay in the middle of the screen. This rule docks it on the left."),
                rule,
            ))
        _rows(box, *rows)
        layout.addWidget(overlay)
        self._graphics = GraphicsPanel()
        layout.addWidget(self._graphics)
        return page

    def _on_overlay_key(self) -> None:
        gui_settings.save_gui_settings(graphics_overlay_key=self._overlay_key.currentData())
        graphics_window.restart_key()

    def _render_graphics(self) -> None:
        self._graphics.render()
        self._overlay_key.blockSignals(True)
        self._overlay_key.setCurrentIndex(self._overlay_key.findData(graphics_window.key_setting()))
        self._overlay_key.blockSignals(False)

    def _appearance_tab(self) -> QWidget:
        page, layout = _panel()
        layout.setSpacing(12)

        interface, box = _card(
            tr("Interface"),
            tr("Applies immediately, unless a background task is running."),
        )
        self._font_family_combo = _combo()
        for family in (
            "Exo 2",
            "Noto Sans",
            "DejaVu Sans",
            "Liberation Sans",
            "Inter",
        ):
            self._font_family_combo.addItem(family, family)
        self._font_family_combo.currentIndexChanged.connect(self._on_font_family)
        self._font_size_combo = _combo()
        for size in range(9, 23):
            self._font_size_combo.addItem(f"{size} px", size)
        self._font_size_combo.currentIndexChanged.connect(self._on_font_size)
        self._language_combo = _combo()
        for code, native, _english in LANGUAGE_INFO:
            self._language_combo.addItem(native, code)
        self._language_combo.currentIndexChanged.connect(self._on_language)
        self._title_bar_check = QCheckBox(tr("Use COMMANDER's own title bar"))
        self._title_bar_check.toggled.connect(self._on_title_bar_toggled)
        self._animations_check = QCheckBox(tr("Animations"))
        self._animations_check.toggled.connect(self._on_animations_toggled)
        self._live_animation_check = QCheckBox(tr("Live theme animation"))
        self._live_animation_check.toggled.connect(self._on_live_animation_toggled)
        self._lights_check = QCheckBox(tr("Christmas lights"))
        self._lights_check.toggled.connect(self._on_lights_toggled)
        self._live_time_combo = _combo()
        for value, label in LIVE_TIME_CHOICES:
            self._live_time_combo.addItem(tr(label), value)
        self._live_time_combo.currentIndexChanged.connect(self._on_live_time)
        _rows(
            box,
            _setting_row(tr("Font family"), "", self._font_family_combo),
            _setting_row(tr("Interface font size"), "", self._font_size_combo),
            _setting_row(tr("Language"), "", self._language_combo),
            _check_row(
                self._title_bar_check,
                tr("Turn off to use your desktop's normal title bar instead."),
            ),
            _check_row(
                self._animations_check,
                tr("Hover effects, page fades, smooth scrolling and the game loading screen's moving scenery. Turn off for a still interface."),
            ),
        )
        # Rows at the top when Themes makes the column taller.
        box.addStretch(1)

        themes, box = _card(
            tr("Themes"),
            tr("Choose a COMMANDER theme. The selection is saved and applied on every launch."),
        )
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for index, (key, label, description, swatches) in enumerate(THEME_INFO):
            if index:
                box.addWidget(_divider())
            row = QHBoxLayout()
            row.setContentsMargins(0, 4, 0, 4)
            row.setSpacing(10)
            text = QVBoxLayout()
            text.setSpacing(0)
            radio = QRadioButton(tr(label))
            radio.setToolTip(tr(description))
            radio.setCursor(Qt.CursorShape.PointingHandCursor)
            self._group.addButton(radio)
            self._radios[key] = radio
            text.addWidget(radio)
            hint = _hint(tr(description))
            hint.setContentsMargins(26, 0, 0, 0)
            text.addWidget(hint)
            row.addLayout(text, 1)
            for color in swatches:
                row.addWidget(_swatch(color), 0, Qt.AlignmentFlag.AlignVCenter)
            box.addLayout(row)
        self._group.buttonToggled.connect(self._on_toggled)
        # Live themes only: on_theme_changed() shows or hides it, dividers and all.
        self._live_rows = QWidget()
        live_box = QVBoxLayout(self._live_rows)
        live_box.setContentsMargins(0, 0, 0, 0)
        live_box.setSpacing(0)
        live_box.addWidget(_divider())
        _rows(
            live_box,
            _check_row(
                self._live_animation_check,
                tr("Falling snow in Winter (Live); swaying trees, a flickering campfire and falling leaves in Red Forest (Live). It pauses while COMMANDER is in the background."),
            ),
            _setting_row(
                tr("Live theme time of day"),
                tr("Keep the Live themes at one time of day instead of following the clock."),
                self._live_time_combo,
            ),
        )
        # Winter only, within the Live rows.
        self._lights_row = QWidget()
        lights_box = QVBoxLayout(self._lights_row)
        lights_box.setContentsMargins(0, 0, 0, 0)
        lights_box.setSpacing(0)
        lights_box.addWidget(_divider())
        lights_box.addWidget(
            _check_row(self._lights_check, tr("The string of lights under the top bar in Winter (Live)."))
        )
        live_box.addWidget(self._lights_row)
        box.addWidget(self._live_rows)
        box.addStretch(1)
        _columns(layout, interface, themes)
        layout.addWidget(self._top_bar_card())
        return page

    def _top_bar_card(self) -> QFrame:
        from .main_window import (  # deferred: avoids an import cycle
            _SEPARATOR_AFTER,
            NAV_ITEMS,
        )

        card, box = _card(
            tr("Top bar"),
            tr("Choose which pages get a tab in the top bar. Dashboard is always shown."),
        )
        # One column per tab group, in the top bar's own order, split by the
        # same thin lines the bar draws between its groups.
        row = QHBoxLayout()
        row.setSpacing(24)
        self._nav_checks: dict[str, QCheckBox] = {}
        columns: dict[int, QVBoxLayout] = {}
        for index, (key, title) in enumerate(NAV_ITEMS):
            group = sum(index > after for after in _SEPARATOR_AFTER)
            if group not in columns:
                if columns:
                    row.addWidget(_vline())
                columns[group] = QVBoxLayout()
                columns[group].setSpacing(8)
                columns[group].addStretch(1)
                row.addLayout(columns[group], 1)
            check = QCheckBox(tr(title))
            if key == NAV_ITEMS[0][0]:  # Dashboard: always in the bar
                check.setChecked(True)
                check.setEnabled(False)
            else:
                check.toggled.connect(self._on_nav_tab_toggled)
                self._nav_checks[key] = check
            columns[group].insertWidget(columns[group].count() - 1, check)
        box.addLayout(row)
        return card

    def _integrations_tab(self) -> QWidget:
        page, layout = _panel()
        layout.setSpacing(12)

        discord, box = _card(
            tr("Discord Rich Presence"),
            tr("Show that you're playing S.T.A.L.K.E.R. GAMMA on your Discord profile while the game runs. Just have the Discord app open - no setup needed."),
        )
        self._discord_enable_check = QCheckBox(tr("Show my game activity on Discord"))
        self._discord_enable_check.toggled.connect(self._on_discord_enabled_toggled)
        box.addWidget(self._discord_enable_check)
        self._discord_mods_check = QCheckBox(tr("Include the mod count"))
        self._discord_mods_check.toggled.connect(self._on_discord_mods_toggled)
        box.addWidget(self._discord_mods_check)
        self._discord_playtime_check = QCheckBox(tr("Include the total playtime"))
        self._discord_playtime_check.toggled.connect(self._on_discord_playtime_toggled)
        box.addWidget(self._discord_playtime_check)
        box.addSpacing(4)
        box.addWidget(_divider())
        self._discord_status_label = QLabel()
        self._discord_status_label.setWordWrap(True)
        self._discord_check_button = _action(tr("Check again"))
        self._discord_check_button.clicked.connect(self._check_discord)
        self._discord_test_button = _action(tr("Test"))
        self._discord_test_button.setToolTip(
            tr("Show the activity on your Discord profile for 15 seconds.")
        )
        self._discord_test_button.clicked.connect(self._on_discord_test)
        self._discord_advanced_button = _action(tr("Advanced"))
        self._discord_advanced_button.setCheckable(True)
        self._discord_advanced_button.toggled.connect(self._on_discord_advanced_toggled)
        status = _setting_row(
            "", "",
            self._discord_check_button, self._discord_test_button, self._discord_advanced_button,
        )
        status.layout().setSpacing(8)
        status.layout().itemAt(0).layout().addWidget(self._discord_status_label)
        box.addWidget(status)
        self._discord_task: BackgroundTask | None = None
        self._discord_test_rpc = None
        self._discord_advanced = QWidget()
        self._discord_advanced.setObjectName("panelTransparent")
        advanced = QVBoxLayout(self._discord_advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.addWidget(_divider())
        self._discord_client_id_edit = QLineEdit()
        self._discord_client_id_edit.setPlaceholderText(DEFAULT_CLIENT_ID)
        self._discord_client_id_edit.setMinimumWidth(260)
        self._discord_client_id_edit.editingFinished.connect(self._on_discord_client_id_changed)
        reset = _action(tr("Reset to default"))
        reset.clicked.connect(self._on_discord_client_id_reset)
        advanced.addWidget(_setting_row(
            tr("Application ID"),
            tr("Only if you want the activity to come from your own Discord application. Leave empty to use COMMANDER's."),
            self._discord_client_id_edit,
            reset,
        ))
        self._discord_advanced.setVisible(False)
        box.addWidget(self._discord_advanced)
        self._set_discord_status(None)
        layout.addWidget(discord)

        steam, box = _card(tr("Steam"))
        add_btn = _action(tr("Add COMMANDER to Steam"))
        add_btn.clicked.connect(self._on_add_to_steam)
        box.addWidget(_setting_row(
            tr("Steam library"),
            tr(
                "Add COMMANDER and its Deck Mode to your Steam library, "
                "so you can launch either one straight from Steam "
                "(including Big Picture and Game Mode) without using "
                "Steam's own \"Add a Non-Steam Game\" dialog."
            ),
            add_btn,
        ))
        layout.addWidget(steam)
        return page

    def _plugins_tab(self) -> QWidget:
        page, layout = _panel()
        layout.setSpacing(12)
        self._moddb = moddb_controller(self.window)
        # One bordered list, one slim row per plugin: name and status, a
        # one-line description, actions on the right.
        frame = QFrame()
        frame.setObjectName("statTile")
        rows = QVBoxLayout(frame)
        rows.setContentsMargins(14, 4, 14, 4)
        rows.setSpacing(0)

        row = QWidget()
        row.setObjectName("panelTransparent")
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 8, 0, 8)
        line.setSpacing(8)
        text = QVBoxLayout()
        text.setSpacing(0)
        heading = QHBoxLayout()
        heading.setSpacing(10)
        name = QLabel(tr("ModDB browser"))
        name.setObjectName("settingTitle")
        heading.addWidget(name)
        self._plugin_status = QLabel()
        self._plugin_status.setObjectName("dim")
        heading.addWidget(self._plugin_status)
        heading.addStretch(1)
        text.addLayout(heading)
        text.addWidget(_hint(tr("Browse and search ModDB, then install what you download.")))
        line.addLayout(text, 1)

        self._plugin_delete_downloads = _action(tr("Delete downloads"))
        self._plugin_delete_downloads.setObjectName("settingDanger")
        self._plugin_delete_downloads.clicked.connect(self._delete_plugin_downloads)
        self._plugin_open_folder = _action(tr("Open folder"))
        self._plugin_open_folder.clicked.connect(self._open_plugin_downloads)
        for button in (self._plugin_delete_downloads, self._plugin_open_folder):
            button.setToolTip(tr("Mods the ModDB browser downloaded. Safe to delete once they're installed."))
        self._plugin_from_file = _action(tr("From a file"))
        self._plugin_from_file.setToolTip(tr("Install a plugin .tar.xz you already downloaded from COMMANDER's GitHub releases."))
        self._plugin_from_file.clicked.connect(self._install_plugin_file)
        self._plugin_install = _action("")
        self._plugin_install.clicked.connect(self._moddb.install)
        self._plugin_open = _action(tr("Open"))
        self._plugin_open.clicked.connect(self._moddb.open)
        self._plugin_cancel = _action(tr("Cancel"))
        self._plugin_cancel.clicked.connect(self._moddb.cancel)
        self._plugin_remove = _action(tr("Remove"))
        self._plugin_remove.setObjectName("settingDanger")
        self._plugin_remove.clicked.connect(self._moddb.remove)
        for button in (
            self._plugin_delete_downloads, self._plugin_open_folder,
            self._plugin_from_file, self._plugin_install,
            self._plugin_open, self._plugin_cancel, self._plugin_remove,
        ):
            line.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        rows.addWidget(row)

        self._plugin_folder_change = _action(tr("Choose"))
        self._plugin_folder_change.clicked.connect(self._pick_plugin_downloads)
        self._plugin_folder_reset = _action(tr("Reset"))
        self._plugin_folder_reset.setToolTip(tr("Save to COMMANDER's own folder again."))
        self._plugin_folder_reset.clicked.connect(lambda: self._set_plugin_downloads(""))
        self._plugin_folder = _setting_row(
            tr("Download folder"), " ", self._plugin_folder_change, self._plugin_folder_reset
        )
        self._plugin_folder.layout().setContentsMargins(0, 0, 0, 8)
        self._plugin_folder_path = self._plugin_folder.findChild(QLabel, "dim")
        rows.addWidget(self._plugin_folder)
        layout.addWidget(frame)

        self._moddb.changed.connect(self._render_plugins)
        self._moddb.line.connect(self._plugin_status.setText)
        self._render_plugins()
        return page

    def _render_plugins(self) -> None:
        moddb = self._moddb
        info = plugins.installed()
        outdated = info is not None and plugins.needs_update(info)
        self._plugin_status.setToolTip("")
        if moddb.busy:
            pass  # the install's own progress lines show
        elif info is None:
            self._plugin_status.setText(tr("Not installed"))
        elif outdated:
            self._plugin_status.setText(tr("Update needed"))
            self._plugin_status.setToolTip(tr("An update is needed before it can open, for security fixes or to match this COMMANDER."))
        elif moddb.running:
            self._plugin_status.setText(tr("Running"))
        else:
            self._plugin_status.setText(tr("Installed"))
        ready = not moddb.busy and info is not None and not outdated
        self._plugin_status.setObjectName("accent" if ready else "dim")
        self._plugin_status.style().unpolish(self._plugin_status)
        self._plugin_status.style().polish(self._plugin_status)
        self._plugin_install.setText(tr("Update") if outdated else tr("Install"))
        self._plugin_install.setToolTip(tr("A separate download of about 100 MB."))
        can_install = not moddb.busy and (info is None or outdated)
        self._plugin_install.setVisible(can_install)
        self._plugin_install.setEnabled(not moddb.running)
        self._plugin_from_file.setVisible(can_install)
        self._plugin_from_file.setEnabled(not moddb.running)
        self._plugin_open.setVisible(info is not None and not outdated and not moddb.busy)
        self._plugin_open.setEnabled(not moddb.running)
        self._plugin_cancel.setVisible(moddb.cancellable)
        self._plugin_remove.setVisible(info is not None and not moddb.busy)
        self._plugin_remove.setEnabled(not moddb.running)
        downloads = bool(plugins.downloaded_files())
        self._plugin_open_folder.setVisible(info is not None or downloads)
        self._plugin_folder.setVisible(info is not None)
        self._plugin_folder_path.setText(str(plugins.downloads_dir()))
        custom = bool(gui_settings.load_gui_settings().get("moddb_downloads"))
        self._plugin_folder_reset.setVisible(custom)
        # The running browser keeps saving where it started.
        for button in (self._plugin_folder_change, self._plugin_folder_reset):
            button.setEnabled(not moddb.running)
        self._plugin_delete_downloads.setVisible(info is not None or downloads)
        # A download in progress, or one being installed, would break.
        self._plugin_delete_downloads.setEnabled(
            downloads and not moddb.running and not self.window.install_busy
        )

    def _install_plugin_file(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, tr("Pick the plugin file"), str(Path.home()), tr("Plugin files") + " (*.tar.xz)"
        )
        if name:
            self._moddb.install_file(Path(name))

    def _delete_plugin_downloads(self) -> None:
        answer = QMessageBox.question(
            self,
            tr("Delete all ModDB downloads?"),
            tr("This deletes every mod archive the ModDB browser plugin downloaded."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            info=tr("Mods you already installed stay installed."),
            labels={QMessageBox.StandardButton.Yes: tr("Delete downloads"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            plugins.delete_downloads()
        except OSError as exc:
            QMessageBox.warning(self, tr("Couldn't delete the downloads"), str(exc))
        self._render_plugins()

    def _pick_plugin_downloads(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, tr("Pick the download folder"), str(plugins.downloads_dir())
        )
        if folder:
            self._set_plugin_downloads(folder)

    def _set_plugin_downloads(self, folder: str) -> None:
        gui_settings.save_gui_settings(moddb_downloads=folder)
        self._render_plugins()

    def _open_plugin_downloads(self) -> None:
        folder = plugins.downloads_dir()
        folder.mkdir(parents=True, exist_ok=True)
        open_in_file_manager(folder)

    def _maintenance_tab(self) -> QWidget:
        page, layout = _panel()
        layout.setSpacing(12)

        maintenance, box = _card(tr("Maintenance"))
        reset_btn = _action(tr("Reset"))
        reset_btn.setObjectName("settingDanger")
        reset_btn.clicked.connect(self._on_reset_playtime)
        export_btn = _action(tr("Export diagnostics"))
        export_btn.clicked.connect(self._on_export_log)
        _rows(
            box,
            _setting_row(
                tr("Playtime"),
                tr("Reset the total playtime tracked for the active profile."),
                reset_btn,
            ),
            _setting_row(
                tr("Diagnostics"),
                tr("Export system information, settings, and launcher output for troubleshooting."),
                export_btn,
            ),
        )
        layout.addWidget(maintenance)
        return page

    def _render_build(self) -> None:
        unstable = is_unstable_version(__version__)
        kind = tr("Unstable") if unstable else tr("Stable")
        self._build_label.setText(kind)
        self._build_label.setStyleSheet(
            f"color: {(WARN if unstable else OK_GREEN).name()}; font-weight: bold;"
        )
        appimage = commander_appimage_path() is not None
        if unstable:
            self._switch_build_button.setText(tr("Go back to the stable build"))
            self._switch_build_button.setEnabled(appimage)
            note = tr(
                "You're on an unstable build: new features sooner, less tested. "
                "Going back installs the latest stable release."
            )
        else:
            self._switch_build_button.setText(tr("Try the unstable build"))
            # Only when a published unstable build is newer than stable.
            self._switch_build_button.setEnabled(appimage and bool(self._unstable_offer))
            if self._unstable_offer:
                note = tr(
                    "Unstable build {tag} is available: new features sooner, less "
                    "tested. You can go back to the stable build at any time.",
                    tag=self._unstable_offer.removeprefix("v"),
                )
            elif not self._unstable_checked:
                note = tr("Checking for an unstable build")
            else:
                note = tr(
                    "There's no unstable build newer than the stable one right now."
                )
            # Also from source: the note still says whether one exists, and
            # skipping the check left "Checking..." on screen for good.
            if not self._unstable_checked:
                self._check_unstable()
        if not appimage:
            # Nothing to press: the note says how to switch instead.
            self._switch_build_button.setEnabled(False)
            self._switch_build_button.setVisible(False)
            note += "\n\n" + tr(
                "Only the AppImage can switch builds itself. Running from source: "
                "use git switch to change to an unstable branch, or to main for "
                "stable. The AUR package is stable only."
            )
        self._build_note.setText(note)

    def _check_unstable(self) -> None:
        if self._unstable_task is not None:
            return
        task = BackgroundTask(newer_unstable_tag, __version__, parent=self)
        self._unstable_task = task

        def done(tag: object) -> None:
            self._unstable_task = None
            self._unstable_checked = True
            self._unstable_offer = tag if isinstance(tag, str) and tag else None
            self._render_build()

        task.result.connect(done)
        task.error.connect(lambda _m: done(None))
        task.start()

    def _on_switch_build(self) -> None:
        target = "stable" if is_unstable_version(__version__) else "unstable"
        switch = getattr(self.window, "switch_commander_build", None)
        if callable(switch):
            switch(target, self._unstable_offer if target == "unstable" else None)

    # --------------------------------------------------------------- handlers
    def _on_reset_playtime(self) -> None:
        profile = self.window.settings.active_profile
        if profile is None:
            QMessageBox.information(
                self, tr("No profile selected"), tr("Pick a profile first, then reset its playtime.")
            )
            return
        name = profile.profile_name or tr("this profile")
        answer = QMessageBox.question(
            self,
            tr("Reset playtime?"),
            tr("The total playtime for '{name}' goes back to 0. This can't be undone.", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            labels={QMessageBox.StandardButton.Yes: tr("Reset"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        state = gui_settings.load_gui_settings()
        playtime = dict(state.get("playtime_seconds") or {})
        playtime[profile.profile_name] = 0.0
        gui_settings.save_gui_settings(playtime_seconds=playtime)
        # Dashboard/Play only re-read this on their own refresh() - normally
        # triggered by a tab switch - so without this the reset would sit
        # invisible until the user happened to leave and come back.
        for key in ("dashboard", "play"):
            page = self.window._pages.get(key)
            if page is not None and hasattr(page, "refresh"):
                page.refresh()

    def _on_add_to_steam(self) -> None:
        vdf_path = pick_shortcuts_vdf(
            self,
            tr("Pick your Steam account"),
            tr("This computer has more than one Steam account. Pick the one to add COMMANDER to:"),
        )
        if vdf_path is None:
            return

        added = tr(
            "\"STALKER COMMANDER\" and \"STALKER COMMANDER DECK\" are now in "
            "your Steam library."
        )
        if not steam_running():
            try:
                add_to_steam(vdf_path, restart=False)
            except (OSError, ShortcutsFileError) as exc:
                QMessageBox.warning(self, tr("Couldn't add COMMANDER to Steam"), str(exc))
                return
            QMessageBox.information(self, tr("Added to Steam"), added)
            return

        # Steam must be closed while the file is written: a running client
        # keeps its own copy of the shortcut list and can save it back over
        # the new one on exit. So the only safe offer is close-write-restart.
        answer = QMessageBox.question(
            self,
            tr("Restart Steam to add COMMANDER?"),
            tr(
                "Steam only picks up new library shortcuts after a restart. "
                "COMMANDER closes Steam, adds them, and opens Steam again."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
            note=tr("Anything running through Steam right now will close."),
            labels={QMessageBox.StandardButton.Yes: tr("Restart Steam"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._steam_restart_task = BackgroundTask(
            add_to_steam, vdf_path, restart=True, parent=self
        )
        self._steam_restart_task.result.connect(
            lambda _r: self.window.statusBar().showMessage(added, 6000)
        )
        self._steam_restart_task.error.connect(
            lambda err: (
                self.window.statusBar().clearMessage(),
                QMessageBox.warning(self, tr("Couldn't add COMMANDER to Steam"), err),
            )
        )
        self._steam_restart_task.start()
        self.window.statusBar().showMessage(tr("Restarting Steam"), 0)

    def _on_start_page(self, *_args) -> None:
        key = self._start_page_combo.currentData()
        if key:
            gui_settings.save_gui_settings(start_page=key)

    def _on_autostart_toggled(self, checked: bool) -> None:
        from ..autostart import disable_autostart, enable_autostart, last_error

        if checked:
            ok = enable_autostart()
        else:
            ok = disable_autostart()
        gui_settings.save_gui_settings(autostart=bool(checked) and ok)
        if not ok:
            self._autostart_check.blockSignals(True)
            self._autostart_check.setChecked(not checked)
            self._autostart_check.blockSignals(False)
            reason = last_error()
            QMessageBox.warning(
                self,
                tr("Couldn't change autostart"),
                tr("COMMANDER couldn't change whether it starts when you log in.")
                + (f"\n\n{reason}" if reason else ""),
            )

    def _on_discord_enabled_toggled(self, checked: bool) -> None:
        gui_settings.save_gui_settings(discord_rpc_enabled=bool(checked))
        self._discord_mods_check.setEnabled(bool(checked))
        self._discord_playtime_check.setEnabled(bool(checked))

    def _on_discord_mods_toggled(self, checked: bool) -> None:
        gui_settings.save_gui_settings(discord_show_mods=bool(checked))

    def _on_discord_playtime_toggled(self, checked: bool) -> None:
        gui_settings.save_gui_settings(discord_show_playtime=bool(checked))

    def _on_discord_advanced_toggled(self, checked: bool) -> None:
        self._discord_advanced.setVisible(checked)

    def _discord_client_id(self) -> str:
        return effective_client_id(self._discord_client_id_edit.text())

    def _on_discord_client_id_changed(self) -> None:
        text = self._discord_client_id_edit.text().strip()
        if text and not text.isdigit():
            QMessageBox.warning(
                self,
                tr("That's not an Application ID"),
                tr("A Discord Application ID is a number. Leave the box empty to use COMMANDER's own."),
            )
            return
        gui_settings.save_gui_settings(discord_client_id=text)
        self._check_discord()

    def _on_discord_client_id_reset(self) -> None:
        self._discord_client_id_edit.clear()
        self._on_discord_client_id_changed()

    def _set_discord_status(self, connected: bool | None) -> None:
        if connected is None:
            text, color = tr("Looking for Discord"), None
        elif connected:
            text, color = tr("● Discord is running - ready"), OK_GREEN
        else:
            text, color = (
                tr("○ Discord not found - open the Discord app, then press Check again"),
                WARN,
            )
        self._discord_status_label.setText(text)
        self._discord_status_label.setStyleSheet(
            f"color: {color.name()}; font-weight: bold;" if color else ""
        )
        self._discord_test_button.setEnabled(bool(connected))

    def _check_discord(self) -> None:
        if self._discord_task is not None:
            return
        self._set_discord_status(None)
        self._discord_check_button.setEnabled(False)
        task = BackgroundTask(probe_discord, self._discord_client_id(), parent=self)
        self._discord_task = task

        def done(result: object) -> None:
            self._discord_task = None
            self._discord_check_button.setEnabled(True)
            self._set_discord_status(result is True)

        task.result.connect(done)
        task.error.connect(lambda _m: done(False))
        task.start()

    def _on_discord_test(self) -> None:
        if self._discord_test_rpc is not None or not self._discord_test_button.isEnabled():
            return
        client_id = self._discord_client_id()
        profile = getattr(getattr(self.window, "settings", None), "active_profile", None)
        state = discord_presence_state(profile, gui_settings.load_gui_settings())

        # The IPC connect blocks up to ~2s per socket path when Discord is
        # wedged - off the GUI thread, like the Play page's presence.
        def _connect():
            rpc = start_presence(client_id)
            if rpc is not None:
                update_presence(rpc, DETAILS_TEXT, state=state)
            return rpc

        self._discord_test_button.setEnabled(False)
        task = BackgroundTask(_connect, parent=self)
        task.result.connect(self._on_discord_test_connected)
        task.error.connect(lambda _m: self._on_discord_test_connected(None))
        task.start()

    def _on_discord_test_connected(self, rpc) -> None:
        if rpc is None:
            self._discord_test_button.setEnabled(True)
            self._set_discord_status(False)
            return
        self._discord_test_rpc = rpc
        self._discord_test_button.setText(tr("Showing on Discord"))
        QTimer.singleShot(15_000, self._end_discord_test)

    def _end_discord_test(self) -> None:
        stop_presence(self._discord_test_rpc)
        self._discord_test_rpc = None
        self._discord_test_button.setText(tr("Test"))
        self._discord_test_button.setEnabled(True)

    def _on_runner_changed(self, *_args) -> None:
        runner = self._runner_combo.currentData()
        if runner:
            gui_settings.save_gui_settings(runner=runner)

    def _open_winecfg(self) -> None:
        if game_running(force=True):
            # The game holds the Wine prefix; winecfg must not touch it live.
            QMessageBox.information(
                self,
                tr("Close the game first"),
                tr("Mod Organizer or the game is still open. Close it, then open Winecfg again."),
            )
            return
        try:
            # The same runner and prefix a launch uses (see
            # gui_settings.saved_prefix_for) - otherwise this can act on a
            # different prefix than the one MO2 runs in.
            runner = gui_settings.configured_runner()
            profile = self.window.settings.active_profile
            cwd = profile.gamma if profile is not None else str(Path.home())
            command, env, cwd = build_runner_tool_command(runner, "winecfg", cwd=cwd)
            launch_detached(command, env, cwd, log_path=logs_dir() / "launcher.log")
        except (LaunchError, OSError) as exc:
            QMessageBox.warning(self, tr("Couldn't open Winecfg"), str(exc))

    def _on_display_scale_changed(self, index: int) -> None:
        if index >= 0:
            gui_settings.save_gui_settings(
                mo2_display_dpi=int(self._display_scale_combo.itemData(index))
            )
            # A previous "Applied" label is stale the moment the selection
            # changes - it hasn't actually been applied for this new value.
            self._apply_display_scale_button.setText(tr("Apply"))

    def _apply_display_scale(self) -> None:
        dpi = self._display_scale_combo.currentData()
        if not isinstance(dpi, int):
            return
        if game_running(force=True):
            # The game holds the Wine prefix; writing to its registry live
            # risks the same corruption already guarded against for winetricks.
            QMessageBox.information(
                self,
                tr("Close the game first"),
                tr("Mod Organizer or the game is still open. Close it, then change the display scale again."),
            )
            return
        try:
            # The same runner and prefix a launch uses (see
            # gui_settings.saved_prefix_for) - otherwise this can act on a
            # different prefix than the one MO2 runs in.
            runner = gui_settings.configured_runner()
            profile = self.window.settings.active_profile
            cwd = profile.gamma if profile is not None else str(Path.home())
            command, env, cwd = build_runner_tool_command(
                runner,
                "reg",
                [
                    "add",
                    r"HKCU\Control Panel\Desktop",
                    "/v",
                    "LogPixels",
                    "/t",
                    "REG_DWORD",
                    "/d",
                    str(dpi),
                    "/f",
                ],
                cwd,
            )
            launch_detached(command, env, cwd, log_path=logs_dir() / "launcher.log")
            self._apply_display_scale_button.setText(tr("Applied"))
        except (LaunchError, OSError) as exc:
            QMessageBox.warning(self, tr("Couldn't change the display scale"), str(exc))

    def _on_title_bar_toggled(self, on: bool) -> None:
        gui_settings.save_gui_settings(custom_title_bar=on)
        apply = getattr(self.window, "apply_title_bar", None)
        if callable(apply):
            apply(on)

    def _on_animations_toggled(self, on: bool) -> None:
        gui_settings.save_gui_settings(animations_enabled=on)

    def _on_live_animation_toggled(self, on: bool) -> None:
        gui_settings.save_gui_settings(live_animation=on)
        sync = getattr(self.window, "sync_animations", None)
        if callable(sync):
            sync()

    def on_theme_changed(self) -> None:
        """Run by the window on every theme change, from any picker."""
        self._live_rows.setVisible(active_theme() in LIVE_THEMES)
        self._lights_row.setVisible(bool(active_theme_tokens().get("back_lights")))

    def _on_lights_toggled(self, on: bool) -> None:
        gui_settings.save_gui_settings(christmas_lights=on)
        self.window.sync_animations()

    def _on_live_time(self, *_args) -> None:
        self.window.apply_live_time(self._live_time_combo.currentData())

    def _on_nav_tab_toggled(self, *_args) -> None:
        gui_settings.save_gui_settings(
            hidden_nav_tabs=[k for k, c in self._nav_checks.items() if not c.isChecked()]
        )
        apply = getattr(self.window, "apply_hidden_tabs", None)
        if callable(apply):
            apply()

    def _on_font_size(self, *_args) -> None:
        size = self._font_size_combo.currentData()
        if size:
            self.window.apply_font_size(size)
            # The status bar exposes the same font size/family/theme settings
            # and is on screen at the same time as this page, but it is built
            # once and only re-selects its combos from refresh_settings() -
            # without this it keeps showing the previous value.
            self.window.refresh_settings()

    def _on_font_family(self, *_args) -> None:
        family = self._font_family_combo.currentData()
        if family:
            self.window.apply_font_family(family)
            self.window.refresh_settings()

    def _on_language(self, *_args) -> None:
        code = self._language_combo.currentData()
        if code:
            self.window.apply_language(code)

    def _on_toggled(self, button: QRadioButton, checked: bool) -> None:
        if not checked:
            return
        key = next((k for k, radio in self._radios.items() if radio is button), None)
        if key is not None and key != active_theme():
            self.window.apply_theme(key)
            self.window.refresh_settings()

    def _on_export_log(self) -> None:
        from ..diagnostics import export_diagnostics

        if self._export_task is not None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Export Diagnostics"),
            "commander-diagnostics.txt",
            tr("Text Files (*.txt);;All Files (*)"),
        )
        if not path:
            return
        # Gathers system info and log files - runs off the GUI thread so a
        # large log doesn't freeze Settings while it collects everything.
        task = BackgroundTask(export_diagnostics, Path(path), parent=self)
        task.result.connect(lambda _r: self._on_export_log_done(path))
        task.error.connect(self._on_export_log_error)
        self._export_task = task
        task.start()

    def _on_export_log_done(self, path: str) -> None:
        self._export_task = None
        QMessageBox.information(
            self,
            tr("Diagnostics saved"),
            tr("Saved to:\n{path}\n\nAttach this file when you ask for help on Discord or GitHub.", path=path),
        )

    def _on_export_log_error(self, message: str) -> None:
        self._export_task = None
        QMessageBox.critical(
            self,
            tr("Couldn't save diagnostics"),
            tr("The diagnostics file couldn't be written.\n\n{exc}", exc=message),
        )

    # ---------------------------------------------------------------- refresh
    def _on_deck_mode(self, _index: int) -> None:
        gui_settings.save_gui_settings(
            deck_mode_preference=self._deck_mode_combo.currentData() or "ask"
        )

    def refresh(self) -> None:
        state = gui_settings.load_gui_settings()
        self._render_plugins()
        self._render_graphics()

        from .main_window import NAV_ITEMS  # deferred: avoids an import cycle

        self._start_page_combo.blockSignals(True)
        self._start_page_combo.clear()
        saved_start = state.get("start_page") or "dashboard"
        start_index = 0
        for index, (key, title) in enumerate(NAV_ITEMS):
            self._start_page_combo.addItem(tr(title), key)
            if key == saved_start:
                start_index = index
        self._start_page_combo.setCurrentIndex(start_index)
        self._start_page_combo.blockSignals(False)

        self._welcome_check.blockSignals(True)
        self._welcome_check.setChecked(not state.get("welcome_hidden", False))
        self._welcome_check.blockSignals(False)
        self._title_bar_check.blockSignals(True)
        self._title_bar_check.setChecked(bool(state.get("custom_title_bar", True)))
        self._title_bar_check.blockSignals(False)
        self._animations_check.blockSignals(True)
        self._animations_check.setChecked(bool(state.get("animations_enabled", True)))
        self._animations_check.blockSignals(False)
        self._live_animation_check.blockSignals(True)
        self._live_animation_check.setChecked(bool(state.get("live_animation", True)))
        self._live_animation_check.blockSignals(False)
        self._lights_check.blockSignals(True)
        self._lights_check.setChecked(bool(state.get("christmas_lights", True)))
        self._lights_check.blockSignals(False)
        self.on_theme_changed()
        self._live_time_combo.blockSignals(True)
        self._live_time_combo.setCurrentIndex(max(self._live_time_combo.findData(state.get("live_time", "")), 0))
        self._live_time_combo.blockSignals(False)
        hidden = set(state.get("hidden_nav_tabs") or [])
        for key, check in self._nav_checks.items():
            check.blockSignals(True)
            check.setChecked(key not in hidden)
            check.blockSignals(False)
        self._update_notify_check.blockSignals(True)
        self._update_notify_check.setChecked(bool(state.get("update_notifications", True)))
        self._update_notify_check.blockSignals(False)

        self._render_build()

        self._deck_mode_combo.blockSignals(True)
        preference = state.get("deck_mode_preference") or "ask"
        if preference == "ask":
            # Unsettled: shown as what startup does with it on this machine.
            preference = "always" if steam_deck_model() is not None else "never"
        index = self._deck_mode_combo.findData(preference)
        self._deck_mode_combo.setCurrentIndex(max(index, 0))
        self._deck_mode_combo.blockSignals(False)

        populate_runner_combo(
            self._runner_combo, find_extra_protons(), state.get("runner") or "auto"
        )

        self._font_size_combo.blockSignals(True)
        font_size_index = self._font_size_combo.findData(int(state.get("font_size") or 13))
        self._font_size_combo.setCurrentIndex(max(font_size_index, 0))
        self._font_size_combo.blockSignals(False)

        saved_font_family = state.get("font_family") or "Exo 2"
        family_index = self._font_family_combo.findData(saved_font_family)
        self._font_family_combo.blockSignals(True)
        self._font_family_combo.setCurrentIndex(max(family_index, 0))
        self._font_family_combo.blockSignals(False)

        saved_language = state.get("language") or "en"
        language_index = self._language_combo.findData(saved_language)
        self._language_combo.blockSignals(True)
        self._language_combo.setCurrentIndex(max(language_index, 0))
        self._language_combo.blockSignals(False)

        dpi = int(state.get("mo2_display_dpi", 120))
        scale_index = self._display_scale_combo.findData(dpi)
        self._display_scale_combo.blockSignals(True)
        self._display_scale_combo.setCurrentIndex(max(scale_index, 0))
        self._display_scale_combo.blockSignals(False)
        self._apply_display_scale_button.setText(tr("Apply"))

        from ..autostart import is_autostart_enabled

        autostart_on = bool(state.get("autostart")) and is_autostart_enabled()
        self._autostart_check.blockSignals(True)
        self._autostart_check.setChecked(autostart_on)
        self._autostart_check.blockSignals(False)

        self._discord_enable_check.blockSignals(True)
        self._discord_enable_check.setChecked(bool(state.get("discord_rpc_enabled")))
        self._discord_enable_check.blockSignals(False)
        self._discord_mods_check.blockSignals(True)
        self._discord_mods_check.setChecked(bool(state.get("discord_show_mods", True)))
        self._discord_mods_check.blockSignals(False)
        self._discord_mods_check.setEnabled(self._discord_enable_check.isChecked())
        self._discord_playtime_check.blockSignals(True)
        self._discord_playtime_check.setChecked(bool(state.get("discord_show_playtime", True)))
        self._discord_playtime_check.blockSignals(False)
        self._discord_playtime_check.setEnabled(self._discord_enable_check.isChecked())
        self._discord_client_id_edit.blockSignals(True)
        custom_id = str(state.get("discord_client_id") or "")
        self._discord_client_id_edit.setText(custom_id)
        self._discord_client_id_edit.blockSignals(False)
        if custom_id:
            self._discord_advanced_button.setChecked(True)
        self._check_discord()

        current = state.get("theme") or "gamma"
        for key, radio in self._radios.items():
            radio.blockSignals(True)
            radio.setChecked(key == current)
            radio.blockSignals(False)
