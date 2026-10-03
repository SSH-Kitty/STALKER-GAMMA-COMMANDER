"""Settings page: launch behaviour, launcher defaults and appearance.

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
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, __version_label__, gui_settings
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
    find_shortcuts_vdf,
    list_steam_accounts,
)
from ..themes import THEME_INFO, active_theme
from ..updates import is_unstable_version, newer_unstable_tag
from .common import (
    OK_GREEN,
    WARN,
    BackgroundTask,
    NoWheelComboBox,
    discord_presence_state,
    info_label,
    make_card,
    mo2_running,
    populate_runner_combo,
    section_label,
    steam_running,
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
        root.addWidget(self._discord_card())
        root.addWidget(self._playtime_card())
        root.addWidget(self._steam_card())
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

        # Shown on every machine, not only Deck hardware: Deck Mode's "When
        # COMMANDER starts" can set "Always" on a desktop PC too, and this
        # is the way back from the full interface.
        self._deck_mode_combo = NoWheelComboBox()
        # The same two choices as Deck Mode's "When COMMANDER starts".
        for label, value in (
            (tr("Steam Deck"), "always"),
            (tr("Desktop"), "never"),
        ):
            self._deck_mode_combo.addItem(label, value)
        self._deck_mode_combo.currentIndexChanged.connect(self._on_deck_mode)
        layout.addLayout(
            _option_row(tr("When COMMANDER starts:"), self._deck_mode_combo)
        )

        self._autostart_check = QCheckBox(tr("Start COMMANDER when I log in"))
        self._autostart_check.setToolTip(
            tr("Add COMMANDER to your desktop's autostart list so it starts automatically when you log in.")
        )
        self._autostart_check.toggled.connect(self._on_autostart_toggled)
        layout.addWidget(self._autostart_check)

        self._welcome_check = QCheckBox(tr("Show the Welcome screen on startup"))
        self._welcome_check.setToolTip(
            tr("The latest COMMANDER patch notes and links to GitHub, Discord and the credits.")
        )
        self._welcome_check.toggled.connect(
            lambda on: gui_settings.save_gui_settings(welcome_hidden=not on)
        )
        layout.addWidget(self._welcome_check)

        self._update_notify_check = QCheckBox(tr("Notify me about GAMMA and COMMANDER updates"))
        self._update_notify_check.setToolTip(
            tr("A desktop notification when a new GAMMA or COMMANDER version comes out - once per version, checked in the background while COMMANDER is open.")
        )
        self._update_notify_check.toggled.connect(
            lambda on: gui_settings.save_gui_settings(update_notifications=on)
        )
        layout.addWidget(self._update_notify_check)

        # Stable or unstable COMMANDER: which one is running, and one button
        # to move to the other. The update channel follows the build.
        build_row = QHBoxLayout()
        build_row.addWidget(QLabel(tr("COMMANDER build:")))
        self._build_label = QLabel()
        build_row.addWidget(self._build_label, 1)
        self._switch_build_button = QPushButton()
        self._switch_build_button.setMinimumWidth(240)
        self._switch_build_button.clicked.connect(self._on_switch_build)
        #: The unstable tag worth switching to (newer than stable), once the
        #: background check has answered; None while unknown or none.
        self._unstable_offer: str | None = None
        self._unstable_checked = False
        self._unstable_task: BackgroundTask | None = None
        self._export_task: BackgroundTask | None = None
        build_row.addWidget(self._switch_build_button)
        layout.addLayout(build_row)
        self._build_note = info_label("")
        layout.addWidget(self._build_note)
        self._render_build()
        return card

    def _render_build(self) -> None:
        unstable = is_unstable_version(__version__)
        kind = tr("Unstable") if unstable else tr("Stable")
        self._build_label.setText(f"{__version_label__}   ·   {kind}")
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
                note = tr("Checking for an unstable build...")
            else:
                note = tr(
                    "There's no unstable build newer than the stable one right now."
                )
            if appimage and not self._unstable_checked:
                self._check_unstable()
        if not appimage:
            self._switch_build_button.setEnabled(False)
            note += "\n" + tr(
                "Only the AppImage can switch builds itself. Running from source: "
                "use 'git switch unstable' or 'git switch main'. The AUR package "
                "is stable only."
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
            switch(target)

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
        self._title_bar_check = QCheckBox(tr("Use COMMANDER's own title bar"))
        self._title_bar_check.setToolTip(
            tr("Turn off to use your desktop's normal title bar instead.")
        )
        self._title_bar_check.toggled.connect(self._on_title_bar_toggled)
        layout.addWidget(self._title_bar_check)
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

    def _discord_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Discord Rich Presence"), level=2))
        layout.addWidget(
            info_label(
                tr("Show that you're playing S.T.A.L.K.E.R. GAMMA on your Discord profile while the game runs. Just have the Discord app open - no setup needed.")
            )
        )
        self._discord_enable_check = QCheckBox(tr("Show my game activity on Discord"))
        self._discord_enable_check.toggled.connect(self._on_discord_enabled_toggled)
        layout.addWidget(self._discord_enable_check)
        self._discord_mods_check = QCheckBox(tr("Include the mod count"))
        self._discord_mods_check.toggled.connect(self._on_discord_mods_toggled)
        layout.addWidget(self._discord_mods_check)
        self._discord_playtime_check = QCheckBox(tr("Include the total playtime"))
        self._discord_playtime_check.toggled.connect(self._on_discord_playtime_toggled)
        layout.addWidget(self._discord_playtime_check)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self._discord_status_label = QLabel()
        self._discord_status_label.setWordWrap(True)
        status_row.addWidget(self._discord_status_label, 1)
        self._discord_check_button = QPushButton(tr("Check again"))
        self._discord_check_button.clicked.connect(self._check_discord)
        status_row.addWidget(self._discord_check_button)
        self._discord_test_button = QPushButton(tr("Test"))
        self._discord_test_button.setToolTip(
            tr("Show the activity on your Discord profile for 15 seconds.")
        )
        self._discord_test_button.clicked.connect(self._on_discord_test)
        status_row.addWidget(self._discord_test_button)
        layout.addLayout(status_row)
        self._discord_task: BackgroundTask | None = None
        self._discord_test_rpc = None

        self._discord_advanced_button = QPushButton(tr("Advanced"))
        self._discord_advanced_button.setCheckable(True)
        self._discord_advanced_button.setFlat(True)
        self._discord_advanced_button.toggled.connect(self._on_discord_advanced_toggled)
        layout.addWidget(self._discord_advanced_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._discord_advanced = QWidget()
        advanced = QVBoxLayout(self._discord_advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.addWidget(
            info_label(
                tr("Only if you want the activity to come from your own Discord application. Leave empty to use COMMANDER's.")
            )
        )
        self._discord_client_id_edit = QLineEdit()
        self._discord_client_id_edit.setPlaceholderText(DEFAULT_CLIENT_ID)
        self._discord_client_id_edit.editingFinished.connect(self._on_discord_client_id_changed)
        id_row = _option_row(tr("Application ID:"), self._discord_client_id_edit)
        reset = QPushButton(tr("Reset to default"))
        reset.clicked.connect(self._on_discord_client_id_reset)
        id_row.addWidget(reset)
        advanced.addLayout(id_row)
        self._discord_advanced.setVisible(False)
        layout.addWidget(self._discord_advanced)
        self._set_discord_status(None)
        return card

    def _playtime_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Playtime"), level=2))
        layout.addWidget(
            info_label(
                tr("Reset the total playtime tracked for the active profile.")
            )
        )
        reset_btn = QPushButton(tr("Reset Playtime"))
        reset_btn.setObjectName("secondary")
        reset_btn.clicked.connect(self._on_reset_playtime)
        layout.addWidget(reset_btn, 0, Qt.AlignmentFlag.AlignLeft)
        return card

    def _steam_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Steam"), level=2))
        layout.addWidget(
            info_label(
                tr(
                    "Add COMMANDER and its Deck Mode to your Steam library, "
                    "so you can launch either one straight from Steam "
                    "(including Big Picture and Game Mode) without using "
                    "Steam's own \"Add a Non-Steam Game\" dialog."
                )
            )
        )
        add_btn = QPushButton(tr("Add COMMANDER to Steam"))
        add_btn.setObjectName("secondary")
        add_btn.clicked.connect(self._on_add_to_steam)
        layout.addWidget(add_btn, 0, Qt.AlignmentFlag.AlignLeft)
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
    def _on_reset_playtime(self) -> None:
        profile = self.window.settings.active_profile
        if profile is None:
            QMessageBox.information(
                self, tr("Reset Playtime"), tr("No active profile.")
            )
            return
        name = profile.profile_name or tr("this profile")
        answer = QMessageBox.question(
            self,
            tr("Reset Playtime"),
            tr("Reset total playtime for profile '{name}' to 0?", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
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
        vdf_path = find_shortcuts_vdf()
        if vdf_path is None:
            accounts = list_steam_accounts()
            if not accounts:
                QMessageBox.warning(
                    self,
                    tr("Add to Steam"),
                    tr("Could not find a Steam installation on this machine."),
                )
                return
            labels = [label for label, _path in accounts]
            label, ok = QInputDialog.getItem(
                self,
                tr("Add to Steam"),
                tr(
                    "More than one Steam account was found on this machine. "
                    "Choose which one to add COMMANDER to:"
                ),
                labels,
                0,
                False,
            )
            if not ok:
                return
            vdf_path = dict(accounts)[label]

        added = tr(
            "Added \"STALKER COMMANDER\" and \"STALKER COMMANDER DECK\" to "
            "your Steam library."
        )
        if not steam_running():
            try:
                add_to_steam(vdf_path, restart=False)
            except (OSError, ShortcutsFileError) as exc:
                QMessageBox.warning(self, tr("Add to Steam"), str(exc))
                return
            QMessageBox.information(self, tr("Add to Steam"), added)
            return

        # Steam must be closed while the file is written: a running client
        # keeps its own copy of the shortcut list and can save it back over
        # the new one on exit. So the only safe offer is close-write-restart.
        answer = QMessageBox.question(
            self,
            tr("Add to Steam"),
            tr(
                "Steam is running. To add the shortcuts it has to close and "
                "start again - this closes anything currently running "
                "through Steam. Restart Steam now?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
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
            lambda err: QMessageBox.warning(self, tr("Add to Steam"), err)
        )
        self._steam_restart_task.start()
        self.window.statusBar().showMessage(tr("Restarting Steam..."), 0)

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
                tr("Discord Rich Presence"),
                tr("An Application ID is a number. Leave the field empty to use COMMANDER's."),
            )
            return
        gui_settings.save_gui_settings(discord_client_id=text)
        self._check_discord()

    def _on_discord_client_id_reset(self) -> None:
        self._discord_client_id_edit.clear()
        self._on_discord_client_id_changed()

    def _set_discord_status(self, connected: bool | None) -> None:
        if connected is None:
            text, color = tr("Looking for Discord..."), None
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
        self._discord_test_button.setText(tr("Showing on Discord..."))
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
        if mo2_running(force=True):
            # The game holds the Wine prefix; winecfg must not touch it live.
            QMessageBox.information(
                self,
                tr("Game Running"),
                tr("Mod Organizer / the game is currently running.\n\nClose it before opening Winecfg."),
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
            QMessageBox.warning(self, tr("Could not open Winecfg"), str(exc))

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
        if mo2_running(force=True):
            # The game holds the Wine prefix; writing to its registry live
            # risks the same corruption already guarded against for winetricks.
            QMessageBox.information(
                self,
                tr("Game Running"),
                tr("Mod Organizer / the game is currently running.\n\nClose it before applying a display scale change."),
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
            QMessageBox.warning(self, tr("Could not apply display scale"), str(exc))

    def _on_title_bar_toggled(self, on: bool) -> None:
        gui_settings.save_gui_settings(custom_title_bar=on)
        apply = getattr(self.window, "apply_title_bar", None)
        if callable(apply):
            apply(on)

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
        from PySide6.QtWidgets import QMessageBox

        self._export_task = None
        QMessageBox.information(
            self,
            tr("Export Complete"),
            tr("Diagnostics exported to:\n{path}", path=path),
        )

    def _on_export_log_error(self, message: str) -> None:
        from PySide6.QtWidgets import QMessageBox

        self._export_task = None
        QMessageBox.critical(
            self,
            tr("Export Failed"),
            tr("Could not export diagnostics:\n{exc}", exc=message),
        )

    # ---------------------------------------------------------------- refresh
    def _on_deck_mode(self, _index: int) -> None:
        gui_settings.save_gui_settings(
            deck_mode_preference=self._deck_mode_combo.currentData() or "ask"
        )

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

        self._welcome_check.blockSignals(True)
        self._welcome_check.setChecked(not state.get("welcome_hidden", False))
        self._welcome_check.blockSignals(False)
        self._title_bar_check.blockSignals(True)
        self._title_bar_check.setChecked(bool(state.get("custom_title_bar", True)))
        self._title_bar_check.blockSignals(False)
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
