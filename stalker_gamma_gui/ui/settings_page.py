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
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..launcher import (
    find_extra_protons,
    find_steam_protons,
    find_wine_versions,
)
from ..themes import THEME_INFO, active_theme
from .common import info_label, make_card, section_label
from .play_page import RUNNER_LABELS


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
        layout.addWidget(section_label("Launch", level=2))
        layout.addWidget(
            info_label(
                "Choose which page COMMANDER opens on when you start it."
            )
        )
        self._start_page_combo = QComboBox()
        self._start_page_combo.currentIndexChanged.connect(self._on_start_page)
        layout.addLayout(
            _option_row("Start page on launch:", self._start_page_combo)
        )

        self._autostart_check = QCheckBox("Launch on startup")
        self._autostart_check.setToolTip(
            "Add COMMANDER to your desktop's autostart list so it starts "
            "automatically when you log in."
        )
        self._autostart_check.toggled.connect(self._on_autostart_toggled)
        layout.addWidget(self._autostart_check)
        return card

    def _launcher_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Default Runner", level=2))
        layout.addWidget(
            info_label(
                "The default runner is used by the Play page. It can still be "
                "overridden per launch there."
            )
        )
        self._runner_combo = QComboBox()
        self._runner_combo.currentIndexChanged.connect(self._on_runner_changed)
        layout.addLayout(_option_row("Default runner:", self._runner_combo))

        self._gamemode_check = QCheckBox("Always use gamemoderun")
        self._gamemode_check.setToolTip(
            "Wrap every launch in gamemoderun (enables the Feral GameMode "
            "CPU governor / scheduler optimisation), even for Wine and Proton."
        )
        self._gamemode_check.toggled.connect(self._on_gamemode_toggled)
        layout.addWidget(self._gamemode_check)
        layout.addWidget(
            info_label(
                "Note: umu-run launches already use gamemoderun automatically "
                "when it is installed."
            )
        )
        return card

    def _appearance_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Appearance", level=2))
        layout.addWidget(
            info_label(
                "Set the base font size. The whole interface scales with it "
                "and the change applies immediately."
            )
        )
        self._font_spin = QSpinBox()
        self._font_spin.setRange(9, 22)
        self._font_spin.setSuffix(" px")
        self._font_spin.valueChanged.connect(self._on_font_size)
        layout.addLayout(_option_row("UI scale / font size:", self._font_spin))
        return card

    def _themes_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Themes", level=2))
        layout.addWidget(
            info_label(
                "Choose the look of COMMANDER. The selection is saved and "
                "applied on every launch."
            )
        )
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for key, label, description, swatches in THEME_INFO:
            row = QHBoxLayout()
            row.setSpacing(10)

            radio = QRadioButton(label)
            radio.setToolTip(description)
            self._group.addButton(radio)
            self._radios[key] = radio
            row.addWidget(radio)

            desc = QLabel(description)
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
        layout.addWidget(section_label("Diagnostics", level=2))
        layout.addWidget(
            info_label(
                "Export a log file with system info, settings and launcher "
                "output for troubleshooting."
            )
        )
        export_btn = QPushButton("Export Log")
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
        from ..autostart import disable_autostart, enable_autostart

        if checked:
            ok = enable_autostart()
        else:
            ok = disable_autostart()
        gui_settings.save_gui_settings(autostart=bool(checked) and ok)

    def _on_runner_changed(self, *_args) -> None:
        runner = self._runner_combo.currentData()
        if runner:
            gui_settings.save_gui_settings(runner=runner)

    def _on_gamemode_toggled(self, checked: bool) -> None:
        gui_settings.save_gui_settings(always_gamemoderun=bool(checked))

    def _on_font_size(self, value: int) -> None:
        self.window.apply_font_size(value)

    def _on_toggled(self, button: QRadioButton, checked: bool) -> None:
        if not checked:
            return
        key = next((k for k, radio in self._radios.items() if radio is button), None)
        if key is None or key == active_theme():
            self.window.apply_theme(key)

    def _on_export_log(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from ..diagnostics import export_diagnostics

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Diagnostics",
            "commander-diagnostics.txt",
            "Text Files (*.txt);;All Files (*)",
        )
        if not path:
            return
        try:
            export_diagnostics(Path(path))
            QMessageBox.information(
                self,
                "Export Complete",
                f"Diagnostics exported to:\n{path}",
            )
        except (OSError, ValueError) as exc:
            QMessageBox.critical(
                self,
                "Export Failed",
                f"Could not export diagnostics:\n{exc}",
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
            self._start_page_combo.addItem(title, key)
            if key == saved_start:
                start_index = index
        self._start_page_combo.setCurrentIndex(start_index)
        self._start_page_combo.blockSignals(False)

        self._runner_combo.blockSignals(True)
        self._runner_combo.clear()
        self._runner_combo.addItem(RUNNER_LABELS["auto"], "auto")
        self._runner_combo.addItem(RUNNER_LABELS["umu"], "umu")
        self._runner_combo.addItem(RUNNER_LABELS["wine"], "wine")
        for label, path in find_steam_protons():
            self._runner_combo.addItem(label, f"proton:{path}")
        for label, path in find_extra_protons():
            self._runner_combo.addItem(label, f"umup:{path}")
        for label, path in find_wine_versions():
            self._runner_combo.addItem(label, f"wine:{path}")
        saved_runner = state.get("runner") or "auto"
        runner_index = self._runner_combo.findData(saved_runner)
        if runner_index < 0:
            runner_index = self._runner_combo.findData("auto")
        self._runner_combo.setCurrentIndex(runner_index)
        self._runner_combo.blockSignals(False)

        self._font_spin.blockSignals(True)
        self._font_spin.setValue(int(state.get("font_size") or 13))
        self._font_spin.blockSignals(False)

        self._gamemode_check.blockSignals(True)
        self._gamemode_check.setChecked(bool(state.get("always_gamemoderun")))
        self._gamemode_check.blockSignals(False)

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
