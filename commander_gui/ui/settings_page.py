"""Settings page: launch behaviour, launcher defaults and appearance.

Start page, default runner and the "always gamemoderun" option persist to the
GUI settings file and take effect immediately or on the next launch as
described next to each control. Themes and the UI scale apply live.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..config import logs_dir
from ..i18n import LANGUAGE_INFO
from ..launcher import (
    LaunchError,
    build_runner_tool_command,
    find_extra_protons,
    launch_detached,
    resolve_runner,
)
from ..themes import THEME_INFO, active_theme
from .common import (
    NoWheelComboBox,
    info_label,
    make_card,
    mo2_running,
    section_label,
    tr,
)


def _swatch(color: str) -> QFrame:
    frame = QFrame()
    frame.setFixedSize(18, 18)
    frame.setStyleSheet(
        f"background-color: {color}; border: 1px solid rgba(0, 0, 0, 90); "
        "border-radius: 3px;"
    )
    return frame


def _option_row(label: str, widget: QWidget, description: str = "") -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(10)
    key = QLabel(label)
    key.setObjectName("info")
    row.addWidget(key)
    row.addWidget(widget, 1)
    if description:
        hint = QLabel(description)
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        row.addWidget(hint, 1)
    return row


class SettingsPage(QWidget):
    """Launch behaviour, launcher defaults, appearance and themes."""

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

        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(14)
        scroll.setWidget(content)

        root.addWidget(self._launch_card())
        root.addWidget(self._launcher_card())
        root.addWidget(self._appearance_card())
        root.addWidget(self._themes_card())
        root.addWidget(self._diagnostics_card())
        root.addStretch(1)

        self.refresh()

    # ------------------------------------------------------------------ cards
    def _launch_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Startup"), level=2))
        layout.addWidget(info_label(tr("Choose the page COMMANDER opens when it starts.")))
        self._start_page_combo = NoWheelComboBox()
        self._start_page_combo.currentIndexChanged.connect(self._on_start_page)
        layout.addLayout(_option_row(tr("Page on startup:"), self._start_page_combo))

        self._autostart_check = QCheckBox(tr("Start COMMANDER when I log in"))
        self._autostart_check.setToolTip(
            tr("Add COMMANDER to your desktop's autostart list so it starts automatically when you log in.")
        )
        self._autostart_check.toggled.connect(self._on_autostart_toggled)
        layout.addWidget(self._autostart_check)
        return card

    def _launcher_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Runner"), level=2))
        layout.addWidget(
            info_label(
                tr("Choose the runner used by default on the Play page. You can override it for each launch.")
            )
        )
        self._runner_combo = NoWheelComboBox()
        self._runner_combo.currentIndexChanged.connect(self._on_runner_changed)
        layout.addLayout(_option_row(tr("Default runner:"), self._runner_combo))

        self._gamemode_check = QCheckBox(tr("Always use GameMode"))
        self._gamemode_check.setToolTip(
            tr("Wrap every launch in gamemoderun (enables the Feral GameMode CPU governor / scheduler optimisation), even for Proton.")
        )
        self._gamemode_check.toggled.connect(self._on_gamemode_toggled)
        layout.addWidget(self._gamemode_check)
        layout.addWidget(
            info_label(
                tr("umu-run launches already use gamemoderun automatically when it is installed.")
            )
        )
        self._winecfg_button = QPushButton(tr("Open Winecfg"))
        self._winecfg_button.setObjectName("secondary")
        self._winecfg_button.setToolTip(
            tr("Open Wine Configuration for the default runner and prefix.")
        )
        self._winecfg_button.clicked.connect(self._open_winecfg)
        layout.addWidget(self._winecfg_button, 0, Qt.AlignmentFlag.AlignLeft)

        display_row = QHBoxLayout()
        display_row.addWidget(QLabel(tr("MO2 Display Scale")))
        self._display_scale_combo = NoWheelComboBox()
        for percent, dpi in ((100, 96), (125, 120), (150, 144), (175, 168), (200, 192)):
            self._display_scale_combo.addItem(f"{percent}% ({dpi} DPI)", dpi)
        self._display_scale_combo.currentIndexChanged.connect(
            self._on_display_scale_changed
        )
        display_row.addWidget(self._display_scale_combo, 1)
        self._apply_display_scale_button = QPushButton(tr("Apply"))
        self._apply_display_scale_button.setObjectName("secondary")
        # A fixed pixel width sized for the English text clips or overlaps
        # longer translations (e.g. Polish "Zastosowano" for "Applied") -
        # a minimum width still keeps the button a consistent size for the
        # common case, but lets it grow for longer text instead of clipping.
        self._apply_display_scale_button.setMinimumSize(64, 30)
        self._apply_display_scale_button.setFixedHeight(30)
        self._apply_display_scale_button.setStyleSheet("padding: 0 6px;")
        self._apply_display_scale_button.clicked.connect(self._apply_display_scale)
        display_row.addWidget(self._apply_display_scale_button)
        layout.addLayout(display_row)
        layout.addWidget(
            info_label(
                tr("125% is recommended for small MO2 text. Restart MO2 after applying.")
            )
        )
        return card

    def _appearance_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Appearance"), level=2))
        layout.addWidget(
            info_label(
                tr("Set the interface font family and size. Changes apply immediately.")
            )
        )
        self._font_family_combo = NoWheelComboBox()
        for family in (
            "Exo 2",
            "Noto Sans",
            "DejaVu Sans",
            "Ubuntu",
            "Liberation Sans",
            "Inter",
        ):
            self._font_family_combo.addItem(family, family)
        self._font_family_combo.currentIndexChanged.connect(self._on_font_family)
        layout.addLayout(_option_row(tr("Font family:"), self._font_family_combo))
        self._font_size_combo = NoWheelComboBox()
        for size in range(9, 23):
            self._font_size_combo.addItem(f"{size} px", size)
        self._font_size_combo.currentIndexChanged.connect(self._on_font_size)
        layout.addLayout(_option_row(tr("Interface font size:"), self._font_size_combo))
        self._language_combo = NoWheelComboBox()
        for code, native, _english in LANGUAGE_INFO:
            self._language_combo.addItem(native, code)
        self._language_combo.currentIndexChanged.connect(self._on_language)
        layout.addLayout(_option_row(tr("Language:"), self._language_combo))
        layout.addWidget(
            info_label(tr("Applies immediately, unless a background task is running."))
        )
        return card

    def _themes_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Themes"), level=2))
        layout.addWidget(
            info_label(
                tr("Choose a COMMANDER theme. The selection is saved and applied on every launch.")
            )
        )
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for key, label, description, swatches in THEME_INFO:
            row = QHBoxLayout()
            row.setSpacing(10)

            radio = QRadioButton(tr(label))
            radio.setToolTip(tr(description))
            self._group.addButton(radio)
            self._radios[key] = radio
            row.addWidget(radio)

            desc = QLabel(tr(description))
            desc.setObjectName("info")
            desc.setWordWrap(True)
            row.addWidget(desc, 1)

            row.addStretch(0)
            for color in swatches:
                row.addWidget(_swatch(color))

            container = QWidget()
            container.setLayout(row)
            layout.addWidget(container)

        self._group.buttonToggled.connect(self._on_toggled)
        return card

    def _diagnostics_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Diagnostics"), level=2))
        layout.addWidget(
            info_label(
                tr("Export system information, settings, and launcher output for troubleshooting.")
            )
        )
        export_btn = QPushButton(tr("Export diagnostics"))
        export_btn.setObjectName("secondary")
        export_btn.clicked.connect(self._on_export_log)
        layout.addWidget(export_btn, 0, Qt.AlignmentFlag.AlignLeft)
        return card

    # --------------------------------------------------------------- handlers
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
                tr("Autostart"),
                tr("Could not update the autostart entry.")
                + (f"\n\n{reason}" if reason else ""),
            )

    def _on_runner_changed(self, *_args) -> None:
        runner = self._runner_combo.currentData()
        if runner:
            gui_settings.save_gui_settings(runner=runner)

    def _on_gamemode_toggled(self, checked: bool) -> None:
        gui_settings.save_gui_settings(always_gamemoderun=bool(checked))

    def _open_winecfg(self) -> None:
        if mo2_running(force=True):
            # The game holds the Wine prefix; winecfg must not touch it live.
            QMessageBox.information(
                self,
                tr("Game Running"),
                tr("Mod Organizer / the game is currently running.\n\nClose it before opening Winecfg."),
            )
            return
        state = gui_settings.load_gui_settings()
        kind = state.get("runner") or "auto"
        prefixes = state.get("prefixes") or {}
        prefix = prefixes.get(kind) or state.get("wine_prefix") or ""
        try:
            runner = resolve_runner(kind, prefix)
            profile = self.window.settings.active_profile
            cwd = profile.gamma if profile is not None else str(Path.home())
            command, env, cwd = build_runner_tool_command(runner, "winecfg", cwd=cwd)
            launch_detached(command, env, cwd, log_path=logs_dir() / "launcher.log")
        except (LaunchError, OSError) as exc:
            QMessageBox.warning(self, tr("Could not open Winecfg"), str(exc))

    def _on_display_scale_changed(self, index: int) -> None:
        if index >= 0:
            gui_settings.save_gui_settings(
                mo2_display_dpi=int(self._display_scale_combo.itemData(index))
            )

    def _apply_display_scale(self) -> None:
        dpi = self._display_scale_combo.currentData()
        if not isinstance(dpi, int):
            return
        if mo2_running(force=True):
            # The game holds the Wine prefix; writing to its registry live
            # risks the same corruption already guarded against for winetricks.
            QMessageBox.information(
                self,
                tr("Game Running"),
                tr("Mod Organizer / the game is currently running.\n\nClose it before applying a display scale change."),
            )
            return
        state = gui_settings.load_gui_settings()
        kind = state.get("runner") or "auto"
        prefixes = state.get("prefixes") or {}
        prefix = prefixes.get(kind) or state.get("wine_prefix") or ""
        try:
            runner = resolve_runner(kind, prefix)
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
            QMessageBox.warning(self, tr("Could not apply display scale"), str(exc))

    def _on_font_size(self, *_args) -> None:
        size = self._font_size_combo.currentData()
        if size:
            self.window.apply_font_size(size)

    def _on_font_family(self, *_args) -> None:
        family = self._font_family_combo.currentData()
        if family:
            self.window.apply_font_family(family)

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

    def _on_export_log(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from ..diagnostics import export_diagnostics

        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Export Diagnostics"),
            "commander-diagnostics.txt",
            tr("Text Files (*.txt);;All Files (*)"),
        )
        if not path:
            return
        try:
            export_diagnostics(Path(path))
            QMessageBox.information(
                self,
                tr("Export Complete"),
                tr("Diagnostics exported to:\n{path}", path=path),
            )
        except (OSError, ValueError) as exc:
            QMessageBox.critical(
                self,
                tr("Export Failed"),
                tr("Could not export diagnostics:\n{exc}", exc=exc),
            )

    # ---------------------------------------------------------------- refresh
    def refresh(self) -> None:
        state = gui_settings.load_gui_settings()

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

        self._runner_combo.blockSignals(True)
        self._runner_combo.clear()
        self._runner_combo.addItem(tr("Auto-detect (latest GE-Proton)"), "auto")
        extra_protons = find_extra_protons()
        if extra_protons:
            self._runner_combo.insertSeparator(self._runner_combo.count())
            for label, path in extra_protons:
                self._runner_combo.addItem(tr("{label} (Installed)", label=label), f"umup:{path}")
        saved_runner = state.get("runner") or "auto"
        runner_index = self._runner_combo.findData(saved_runner)
        if runner_index < 0:
            runner_index = self._runner_combo.findData("auto")
        self._runner_combo.setCurrentIndex(max(runner_index, 0))
        self._runner_combo.blockSignals(False)

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

        self._gamemode_check.blockSignals(True)
        self._gamemode_check.setChecked(bool(state.get("always_gamemoderun")))
        self._gamemode_check.blockSignals(False)

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

        current = state.get("theme") or "gamma"
        for key, radio in self._radios.items():
            radio.blockSignals(True)
            radio.setChecked(key == current)
            radio.blockSignals(False)
