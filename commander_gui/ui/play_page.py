"""Play page: the app's front page - launch the game through Mod Organizer.

A hero-style layout: a two-column grid lets the player pick the game target
("Launch Game") and the runner ("Select Runner") as equally important steps,
with the launch actions and a copyable command preview below.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import applog, coop, gui_settings
from ..assistant_launcher import AssistantLaunchError, launch_assistant
from ..config import logs_dir
from ..discord_rpc import (
    DETAILS_TEXT,
    effective_client_id,
    start_presence,
    stop_presence,
    update_presence,
)
from ..launcher import (
    DEFAULT_PROTON_PREFIX,
    DEFAULT_UMU_PREFIX,
    ForeignPrefixError,
    LaunchError,
    Mo2Executable,
    ProcessGroupRegistry,
    _pick_umu_proton,
    _terminate_process_group,
    available_commands,
    build_command,
    build_direct_command,
    debugger_crash_loop,
    default_launch_target,
    ensure_runner_prefix,
    find_extra_protons,
    kill_stray_debuggers,
    launch_detached,
    parse_mo2_executables,
    prefix_foreign_dlls,
    read_log_tail,
    resolve_runner,
    runner_crash_loop,
    runner_graphics_error,
    runner_prefix_error,
    set_mo2_selected_title,
    steam_shortcut_appid,
    unblock_mo2_python,
    write_desktop_shortcut,
)
from ..log_dump import create_log_dump
from ..modlist import (
    backup_before_change,
    looks_flipped,
    modlist_path_for,
    read_lines,
    save_lines,
    unflip,
)
from ..steam_shortcuts import (
    compat_tool_name,
    coop_launch_options,
    coop_launch_url,
    coop_shortcut_current,
    open_in_steam,
    setup_coop_shortcut,
    steam_root_for,
)
from .common import (
    notice,
    ACCENT,
    WARN,
    BackgroundTask,
    clear_layout,
    crash_dump_names,
    discord_presence_state,
    exe_pids,
    format_playtime,
    gamma_installed,
    info_label,
    install_hover_grow_text,
    make_card,
    make_header_row,
    mo2_pids,
    mo2_running,
    normalize_path,
    notify_desktop,
    pick_shortcuts_vdf,
    play_click_sound,
    populate_runner_combo,
    save_profile_dirs,
    section_label,
    set_hover_grow_text,
    steam_running,
    tr,
)
from .dialogs import MessageBox as QMessageBox
from .install_page import _PathEdit, _resume_state_matches
from .multiplayer_page import confirm_player_name, refuse_launch

_HIDDEN_LAUNCH_TARGETS = {"dx8", "dx8-avx"}

#: Launch options the Play page offers as one-click toggles: (label, the
#: token added to the launch options box, tooltip).
_LAUNCH_OPTION_TOGGLES = (
    ("MangoHud", "mangohud", "Show the MangoHud overlay (FPS, frame times, temperatures)."),
    ("FPS counter", "DXVK_HUD=fps", "Show DXVK's small FPS counter in the corner."),
    ("No Esync", "PROTON_NO_ESYNC=1", "Turn off Esync. Can help if the game stutters or hangs."),
    ("No Fsync", "PROTON_NO_FSYNC=1", "Turn off Fsync. Can help if the game stutters or hangs."),
)

# X-Ray's crash handler can take tens of seconds to finish writing a
# .mdmp after the wrapper/MO2 process is already gone (confirmed against
# a real crash: 39s) - poll for it instead of checking once, immediately.
_CRASH_POLL_INTERVAL_MS = 2000
_CRASH_POLL_MAX_ATTEMPTS = 45  # 90s total window
#: While a game session runs without a Discord connection, retry this often.
_DISCORD_RETRY_SECONDS = 15.0


def _is_hidden_launch_target(title: str) -> bool:
    return title.strip().casefold() in _HIDDEN_LAUNCH_TARGETS


def _kill_stray_debuggers(runner_env: dict[str, str] | None) -> None:
    """winedbg instances of this prefix that escaped the process group.

    Scoped to the launch's own prefix - ``pkill -f winedbg`` also killed
    the debuggers of unrelated Wine games.
    """
    kill_stray_debuggers(runner_env)


class PlayPage(QWidget):
    launch_state_changed = Signal(bool)
    #: The live launch status line or the playtime changed (Multiplayer mirrors both).
    launch_status_changed = Signal()

    def __init__(self, window) -> None:
        super().__init__()
        self.setObjectName("playPage")
        self.window = window
        self.executables: list[Mo2Executable] = []
        self._launching = False
        self._install_busy = False
        self._proc = None
        #: The wrapper's own pid, remembered independently of the Popen
        #: handle above - once MO2 takes over, the wrapper process can
        #: exit while MO2/the game keep running under the same process
        #: group. Quitting must still be able to reach them by killpg-ing
        #: this pid even after self._proc has already been cleared.
        self._launch_wrapper_pid: int | None = None
        self._launch_timer = None
        self._monitoring_mo2 = False
        self._mo2_seen = False
        self._handoff_checks = 0
        #: Ticks to wait for MO2 to appear; a Steam launch needs far longer.
        self._handoff_limit = 10
        #: The session was started by Steam (co-op Steam shortcut): no
        #: wrapper process, no launcher.log - tracked only by exe name.
        self._steam_launch = False
        #: The launch's runner environment: whose prefix's winedbg processes
        #: the crash-loop breaker counts.
        self._debugger_env: dict[str, str] = {}
        #: PIDs of MO2 processes already running before this launch started,
        #: and the subset that belongs to this launch once detected - lets
        #: handoff detection ignore an unrelated MO2 window the user already
        #: had open (see mo2_pids() docstring).
        self._pre_launch_mo2_pids: set[int] = set()
        self._mo2_launch_pids: set[int] = set()
        #: The actual game executable's basename (not MO2's own exe) and
        #: its pre-launch pids, so playtime can be recorded when the game
        #: itself exits instead of waiting for MO2 (which stays open
        #: after the game closes) to exit.
        self._game_exe_name: str | None = None
        self._pre_launch_game_pids: set[int] = set()
        self._game_seen = False
        #: A game session is running whose end should be checked for a
        #: new crash dump (see _check_for_crash()).
        self._crash_check_pending = False
        self._pre_launch_crash_dumps: set[str] = set()
        #: X-Ray's own crash handler can take a while (tens of seconds,
        #: confirmed against a real user's crash: 39s) to actually finish
        #: writing the .mdmp file after the wrapper/MO2 process is
        #: already gone - checking once, immediately, at session-end
        #: misses it. These back a bounded poll instead of one shot; see
        #: _poll_for_crash().
        self._crash_poll_timer: QTimer | None = None
        self._crash_poll_anomaly = ""
        self._crash_poll_baseline: set[str] = set()
        self._crash_poll_attempts_left = 0
        self._registry = ProcessGroupRegistry()
        self._launch_status_clear_timer = QTimer(self)
        self._launch_status_clear_timer.setSingleShot(True)
        self._launch_status_clear_timer.timeout.connect(self._clear_launch_status)
        self._crash_report_task: BackgroundTask | None = None
        self._repair_task: BackgroundTask | None = None
        self._launch_started_at: float | None = None
        self._discord_rpc = None
        self._discord_wanted = False
        self._discord_started_at: float | None = None
        self._discord_last_attempt = 0.0
        self._discord_task: BackgroundTask | None = None
        self._persisting = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(16)
        scroll.setWidget(content)

        # -- runner | launch target: one card, split by a divider -------------
        # Same grid + divider pattern as the Install page's cards; each
        # row holds the matching piece of both columns so they line up.
        config_card, config_layout = make_card()
        grid = QGridLayout()
        grid.setHorizontalSpacing(32)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        config_layout.addLayout(grid)
        divider = QFrame()
        divider.setObjectName("installDivider")
        divider.setFrameShape(QFrame.Shape.NoFrame)
        divider.setFixedWidth(1)
        grid.addWidget(divider, 0, 1, 5, 1)

        grid.addLayout(make_header_row("Runner"), 0, 0)
        self.runner_combo = QComboBox()
        self.runner_combo.setMinimumHeight(34)
        self.runner_combo.currentIndexChanged.connect(self._on_runner_changed)
        grid.addWidget(self.runner_combo, 1, 0)
        self.runner_hint = info_label("")
        self.runner_hint.setObjectName("dim")
        self.runner_hint.setWordWrap(True)
        grid.addWidget(self.runner_hint, 2, 0)
        self.gamemode_check = QCheckBox(tr("Always use GameMode"))
        self.gamemode_check.setToolTip(
            tr("Run every launch through gamemoderun (Feral GameMode), Proton included.")
        )
        self.gamemode_check.toggled.connect(self._on_gamemode_toggled)
        grid.addWidget(self.gamemode_check, 3, 0)
        self.manage_proton_button = QPushButton(tr("Manage GE-Proton..."))
        self.manage_proton_button.setObjectName("secondary")
        self.manage_proton_button.setMinimumHeight(34)
        self.manage_proton_button.setToolTip(
            tr("Install new GE-Proton builds, and remove the ones you no longer use.")
        )
        self.manage_proton_button.clicked.connect(self._manage_protons)
        grid.addWidget(self.manage_proton_button, 4, 0)

        grid.addLayout(make_header_row("Launch target"), 0, 2)
        self.target_combo = QComboBox()
        self.target_combo.setMinimumHeight(34)
        self.target_combo.currentIndexChanged.connect(self._on_change)
        grid.addWidget(self.target_combo, 1, 2)
        self.target_path = QLabel("")
        self.target_path.setObjectName("dim")
        self.target_path.setWordWrap(True)
        self.target_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        grid.addWidget(self.target_path, 2, 2, 2, 1, Qt.AlignmentFlag.AlignTop)
        self.shortcut_button = QPushButton(tr("Add shortcut to desktop"))
        self.shortcut_button.setObjectName("secondary")
        self.shortcut_button.setMinimumHeight(34)
        self.shortcut_button.setToolTip(
            tr("Create a desktop shortcut for the selected target and runner.")
        )
        self.shortcut_button.setVisible(os.name != "nt")
        self.shortcut_button.clicked.connect(self._add_desktop_shortcut)
        grid.addWidget(self.shortcut_button, 4, 2)
        root.addWidget(config_card)

        # -- status chip row -------------------------------------------------
        self.chips_row = QHBoxLayout()
        self.chips_row.setSpacing(8)
        root.addLayout(self.chips_row)

        # -- launch actions ---------------------------------------------------
        self.launch_button = QPushButton(tr("Launch Game"))
        self.launch_button.setObjectName("hero")
        self.launch_button.setToolTip(
            tr("Launch through Mod Organizer 2 with the GAMMA modlist.")
        )
        install_hover_grow_text(self.launch_button, "hero_text")
        self.launch_button.clicked.connect(self._on_launch_button_clicked)
        self.launch_button.clicked.connect(play_click_sound)
        root.addWidget(self.launch_button)

        self.playtime_label = info_label("")
        self.playtime_label.setObjectName("dim")
        self.playtime_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(self.playtime_label)

        self.launch_live_status = info_label("")
        self.launch_live_status.setObjectName("accent")
        self.launch_live_status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.launch_live_status.setWordWrap(True)
        self.launch_live_status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        root.addWidget(self.launch_live_status)

        secondary_row = QHBoxLayout()
        secondary_row.setSpacing(12)
        self.open_mo2_button = QPushButton(tr("Open MO2"))
        self.open_mo2_button.setObjectName("secondary")
        self.open_mo2_button.setToolTip(
            tr("Open Mod Organizer 2 to manage the selected MO2 profile and run executables.")
        )
        self.open_mo2_button.clicked.connect(self._open_mo2)
        secondary_row.addWidget(self.open_mo2_button, 1)
        self.direct_button = QPushButton(tr("Launch Anomaly"))
        self.direct_button.setObjectName("secondary")
        self.direct_button.setToolTip(
            tr("Run the selected Anomaly executable without MO2 or its virtual mod list.")
        )
        self.direct_button.clicked.connect(self._launch_direct)
        self.direct_button.clicked.connect(play_click_sound)
        secondary_row.addWidget(self.direct_button, 1)
        root.addLayout(secondary_row)

        # -- custom launch options -------------------------------------------
        options_card, options_layout = make_card()
        options_layout.setSpacing(10)
        options_layout.addWidget(section_label(tr("Launch options"), level=2))
        options_layout.addWidget(
            info_label(
                tr("Extra settings for every launch, written like Steam's launch options. Put %command% before anything that goes after the game.")
            )
        )
        self.custom_options_edit = QLineEdit()
        self.custom_options_edit.setPlaceholderText(
            tr("Type launch options, or pick some below")
        )
        self.custom_options_edit.setMinimumHeight(34)
        self.custom_options_edit.setClearButtonEnabled(True)
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(300)
        self._debounce_timer.timeout.connect(self._on_change)
        self.custom_options_edit.textChanged.connect(
            lambda: self._debounce_timer.start()
        )
        self.custom_options_edit.textChanged.connect(self._sync_option_toggles)
        options_layout.addWidget(self.custom_options_edit)
        toggles_row = QHBoxLayout()
        toggles_row.setSpacing(8)
        self._option_toggles: list[tuple[QPushButton, str]] = []
        for label, token, tip in _LAUNCH_OPTION_TOGGLES:
            toggle = QPushButton(tr(label))
            toggle.setObjectName("segment")
            toggle.setCheckable(True)
            toggle.setFixedHeight(34)
            toggle.setCursor(Qt.CursorShape.PointingHandCursor)
            toggle.setToolTip(tr(tip))
            toggle.toggled.connect(
                lambda on, token=token: self._toggle_launch_option(token, on)
            )
            toggles_row.addWidget(toggle)
            self._option_toggles.append((toggle, token))
        toggles_row.addStretch(1)
        options_layout.addLayout(toggles_row)
        root.addWidget(options_card)

        # -- folders card -----------------------------------------------------
        # Click a folder box to pick a new folder, as on the Install page.
        folders_card, folders_layout = make_card()
        folders_layout.setSpacing(10)
        folders_layout.addWidget(section_label(tr("Folders"), level=2))
        folders_grid = QGridLayout()
        folders_grid.setHorizontalSpacing(12)
        folders_grid.setVerticalSpacing(8)
        folders_grid.setColumnStretch(1, 1)
        folders_layout.addLayout(folders_grid)
        self.anomaly_edit = self._folder_row(
            folders_grid, 0, tr("Anomaly folder:"), "Select Anomaly install folder"
        )
        self.gamma_edit = self._folder_row(
            folders_grid, 1, tr("GAMMA folder:"), "Select GAMMA install folder"
        )
        self.cache_edit = self._folder_row(
            folders_grid, 2, tr("Cache folder:"), "Select cache folder"
        )
        root.addWidget(folders_card)

        # _refresh_preview_inner() still writes the resolved launch command
        # into this (setPlainText/setStyleSheet) as part of deciding
        # whether launch_button/open_mo2_button/direct_button should be
        # enabled - kept alive but never added to any layout, so the
        # command-preview card itself is gone from the page.
        self.preview_label = QPlainTextEdit()
        self.preview_label.setReadOnly(True)

        root.addStretch(1)

        self._reload_runners()
        self._reload_targets()
        self._load_state()
        self._refresh_preview()
        self._update_playtime_label()

    @property
    def is_launching(self) -> bool:
        """Public read-only access to the launch-in-progress state."""
        return self._launching

    # ------------------------------------------------------------------ state
    def _load_state(self) -> None:
        state = gui_settings.load_gui_settings()
        prefixes = dict(state.get("prefixes") or {})
        if not prefixes and state.get("wine_prefix"):
            prefixes[state.get("runner", "auto")] = state["wine_prefix"]
            gui_settings.save_gui_settings(prefixes=prefixes)
        self.custom_options_edit.blockSignals(True)
        self.custom_options_edit.setText(state.get("custom_launch_options", ""))
        self.custom_options_edit.blockSignals(False)
        self._sync_option_toggles()

    def _manage_protons(self) -> None:
        from .proton_manager import ProtonManagerDialog

        # Kept and reused: its size scan may still be running when it is
        # closed, and the thread must outlive the window.
        dialog = getattr(self, "_proton_manager", None)
        if dialog is None:
            dialog = ProtonManagerDialog(self.window, self, on_changed=self._reload_runners)
            self._proton_manager = dialog
        else:
            dialog.reload()
        dialog.exec()

    def _reload_runners(self) -> None:
        current = self.runner_combo.currentData()
        extra_protons = find_extra_protons()
        # The saved runner wins: the Dashboard and Settings change it
        # without this page, and keeping the combo's old choice made the
        # next launch use a different runner (and prefix) than the one
        # shown everywhere else - and Winecfg acts on.
        populate_runner_combo(
            self.runner_combo,
            extra_protons,
            gui_settings.load_gui_settings().get("runner", "auto"),
            fallback=current,
        )
        self._update_runner_hint(self.runner_combo.currentData())

    def _default_prefix(self, kind: str) -> str:
        if kind.startswith("proton:"):
            return str(DEFAULT_PROTON_PREFIX)
        return str(DEFAULT_UMU_PREFIX)

    def _prefix_for(self, *, kind: str | None) -> str:
        if not kind:
            kind = "auto"
        # The shared rule (gui_settings.saved_prefix_for), so a launch uses
        # exactly the prefix Winecfg, the display scale and repairs act on.
        return gui_settings.saved_prefix_for(kind) or self._default_prefix(kind)

    def _reload_targets(self) -> None:
        profile = self.window.settings.active_profile
        self.executables = [
            executable
            for executable in (
                parse_mo2_executables(profile.gamma) if profile is not None else []
            )
            if not _is_hidden_launch_target(executable.title)
        ]
        if not self.executables and profile is not None:
            anomaly_path = Path(profile.anomaly)
            launcher = anomaly_path / "AnomalyLauncher.exe"
            if launcher.is_file():
                self.executables.append(
                    Mo2Executable(
                        title="Anomaly",
                        binary=str(launcher),
                        working_directory=str(anomaly_path),
                    )
                )
        titles = [exe.title for exe in self.executables]
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItems(titles)
        default = default_launch_target(
            titles,
            profile.gamma if profile else "",
            gui_settings.load_gui_settings().get("target") or "",
        )
        if default:
            self.target_combo.setCurrentText(default)
        if self.target_combo.currentIndex() < 0 and self.target_combo.count():
            # Belt and braces: never leave the target combo without a selection.
            self.target_combo.setCurrentIndex(0)
        self.target_combo.blockSignals(False)

    def refresh(self) -> None:
        self.window.refresh_settings()
        self._reload_runners()
        self._reload_targets()
        self._load_folders()
        self._update_playtime_label()
        self.gamemode_check.blockSignals(True)
        self.gamemode_check.setChecked(
            bool(gui_settings.load_gui_settings().get("always_gamemoderun"))
        )
        self.gamemode_check.blockSignals(False)
        self._refresh_preview()

    # ---------------------------------------------------------------- folders
    def _load_folders(self) -> None:
        profile = self.window.settings.active_profile
        if profile is None:
            return
        self.anomaly_edit.setText(profile.anomaly)
        self.gamma_edit.setText(profile.gamma)
        self.cache_edit.setText(profile.cache)

    def _folder_row(self, grid: QGridLayout, row: int, label: str, title: str) -> _PathEdit:
        edit = _PathEdit()
        edit.setPlaceholderText(tr("Click to choose a folder"))
        edit.setToolTip(tr("Click to choose a folder"))
        edit.clicked.connect(lambda: self._pick_folder(edit, title))
        grid.addWidget(QLabel(label), row, 0)
        grid.addWidget(edit, row, 1)
        return edit

    def _pick_folder(self, edit: QLineEdit, title: str) -> None:
        start = edit.text() if Path(edit.text()).is_dir() else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, title, start)
        if folder:
            edit.setText(folder)
            self._persist_dirs()

    # --------------------------------------------------------- launch options
    def _option_tokens(self) -> list[str]:
        text = self.custom_options_edit.text()
        try:
            return shlex.split(text)
        except ValueError:
            return text.split()

    def _toggle_launch_option(self, token: str, on: bool) -> None:
        tokens = [t for t in self._option_tokens() if t != token]
        if on:
            # Wrappers and variables go in front of the game, so before
            # %command% when the user wrote one.
            at = tokens.index("%command%") if "%command%" in tokens else len(tokens)
            tokens.insert(at, token)
        self.custom_options_edit.setText(shlex.join(tokens))

    def _sync_option_toggles(self) -> None:
        tokens = set(self._option_tokens())
        for toggle, token in self._option_toggles:
            toggle.blockSignals(True)
            toggle.setChecked(token in tokens)
            toggle.blockSignals(False)

    def _persist_dirs(self) -> None:
        if self._persisting:
            return
        self._persisting = True
        try:
            if self.window.install_busy or mo2_running():
                QMessageBox.warning(
                    self,
                    tr("Please wait"),
                    tr("An install or the game is running, so folder changes can't be saved right now. Try again when it's done."),
                )
                self._load_folders()
                return
            self.window.refresh_settings()
            profile = self.window.settings.active_profile
            if profile is None:
                return
            anomaly = normalize_path(self.anomaly_edit.text())
            gamma = normalize_path(self.gamma_edit.text())
            cache = normalize_path(self.cache_edit.text())
            self.anomaly_edit.setText(anomaly)
            self.gamma_edit.setText(gamma)
            self.cache_edit.setText(cache)
            if (
                anomaly == profile.anomaly
                and gamma == profile.gamma
                and cache == profile.cache
            ):
                return
            try:
                save_profile_dirs(self.window.settings, profile, anomaly, gamma, cache)
            except OSError as exc:
                QMessageBox.warning(
                    self, tr("Couldn't save the profile"), tr("COMMANDER couldn't write its settings file (settings.json).\n\n{exc}", exc=exc)
                )
                self._load_folders()
                return
            self.window.statusBar().showMessage(
                tr("Folders updated: {anomaly} | {gamma} | {cache}", anomaly=anomaly, gamma=gamma, cache=cache),
                6000,
            )
            self._reload_targets()
            self._refresh_preview()
        finally:
            self._persisting = False

    # ---------------------------------------------------------------- actions
    def _selected_target(self) -> str | None:
        title = self.target_combo.currentText()
        if title in [exe.title for exe in self.executables]:
            return title
        return None

    def _active_profile_name(self) -> str | None:
        profile = self.window.settings.active_profile
        if profile is None or not profile.mo2_profile:
            return None
        profiles_dir = Path(profile.gamma) / "profiles"
        if (profiles_dir / profile.mo2_profile).is_dir():
            return profile.mo2_profile
        return None

    def _runner(self):
        kind = self.runner_combo.currentData() or "auto"
        # Expand for every runner: '~' is equally invalid as a Proton/umu prefix path.
        prefix = os.path.expanduser(self._prefix_for(kind=kind))
        if prefix:
            # A relative value would otherwise resolve against this process's
            # unpredictable inherited cwd instead of a stable, obvious location.
            prefix = str(Path(prefix).resolve())
        runner = resolve_runner(kind, prefix)
        if gui_settings.load_gui_settings().get("always_gamemoderun"):
            gamemoderun = available_commands().get("gamemoderun")
            if gamemoderun and (not runner.wrapper or runner.wrapper[0] != gamemoderun):
                runner.wrapper.insert(0, gamemoderun)
        return runner

    def _resolve_command(self, *, open_mo2: bool, direct: bool, runner=None):
        import re

        profile = self.window.settings.active_profile
        if profile is None:
            raise LaunchError("No active profile. Configure a profile first.")
        if runner is None:
            runner = self._runner()
        if direct:
            target = self._selected_target()
            exe = next(
                (e for e in self.executables if e.title == target), Mo2Executable()
            )
            command, env, cwd = build_direct_command(exe, runner)
        else:
            target = None if open_mo2 else self._selected_target()
            command, env, cwd = build_command(
                profile.gamma,
                runner,
                target=target,
                profile=self._active_profile_name(),
            )
        options_str = self.custom_options_edit.text().strip()
        if options_str:
            try:
                tokens = shlex.split(options_str)
            except ValueError:
                tokens = options_str.split()
            # Steam's syntax: "VAR=1 wrapper %command% --arg". What comes
            # after %command% belongs after the command, as its arguments;
            # it used to be moved in front of it along with the wrappers.
            suffix: list[str] = []
            if "%command%" in tokens:
                marker = tokens.index("%command%")
                tokens, suffix = tokens[:marker], [
                    t for t in tokens[marker + 1 :] if t != "%command%"
                ]
            env_var_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
            prefix: list[str] = []
            for token in tokens:
                if env_var_re.match(token):
                    key, _, value = token.partition("=")
                    env[key] = value
                else:
                    prefix.append(token)
            command = [*prefix, *command, *suffix]
        return command, env, cwd

    def _refresh_preview(self) -> None:
        """Refresh the launch preview, surfacing unexpected errors.

        Any exception here previously left the launch buttons silently dead;
        it is now reported in the preview pane instead.
        """
        try:
            self._refresh_preview_inner()
        except Exception as exc:  # noqa: BLE001 - never leave buttons silently dead
            self.preview_label.setPlainText(tr("Launch check failed: {error}", error=exc))
            self.preview_label.setStyleSheet(f"color: {WARN.name()};")
            if not self._launching:
                self.launch_button.setEnabled(False)
            self.open_mo2_button.setEnabled(False)
            self.direct_button.setEnabled(False)
        # A shortcut runs the same MO2 command, so it is usable exactly when
        # Open MO2 is - on_busy_changed() disabled it and nothing re-enabled it.
        self.shortcut_button.setEnabled(self.open_mo2_button.isEnabled())

    def _refresh_preview_inner(self) -> None:
        # Resolve the runner once: each resolution probes the filesystem for
        # Steam libraries and Proton builds, and this runs on every edit.
        profile = self.window.settings.active_profile
        resume_state = gui_settings.load_gui_settings().get("gamma_install_resume")
        incomplete = profile is not None and _resume_state_matches(
            resume_state, profile
        )
        try:
            runner = self._runner()
        except LaunchError as exc:
            self.preview_label.setPlainText(str(exc))
            self.preview_label.setStyleSheet(f"color: {WARN.name()};")
            if not self._launching:
                self.launch_button.setEnabled(False)
            self.open_mo2_button.setEnabled(False)
            self.direct_button.setEnabled(False)
            self._build_chips(ok=False, runner=None)
            return
        # Try MO2 path first.
        mo2_ok = False
        command = None
        try:
            command, _, _ = self._resolve_command(
                open_mo2=False, direct=False, runner=runner
            )
            mo2_ok = True
        except LaunchError:
            pass
        # Try direct path as fallback.
        direct_ok = False
        try:
            dcommand, _, _ = self._resolve_command(
                open_mo2=False, direct=True, runner=runner
            )
            direct_ok = True
            if command is None:
                command = dcommand
        except LaunchError:
            pass
        if command is None:
            self.preview_label.setPlainText(tr("No launch target available"))
            self.preview_label.setStyleSheet(f"color: {WARN.name()};")
            self.target_path.setText("")
            if not self._launching:
                self.launch_button.setEnabled(False)
            self.open_mo2_button.setEnabled(False)
            self.direct_button.setEnabled(False)
            self._build_chips(ok=False, runner=None)
            return
        self.preview_label.setPlainText(shlex.join(command))
        self.preview_label.setStyleSheet("")
        # Anomaly-direct availability is independent of the MO2/target
        # pipeline: with only Anomaly installed, Launch Anomaly must work.
        anomaly_fallback = next(
            (
                e
                for e in self.executables
                if e.title == "Anomaly" and e.binary and Path(e.binary).is_file()
            ),
            None,
        )
        base_ok = not self._launching and not self._install_busy and not incomplete
        if not self._launching:
            self.launch_button.setEnabled(base_ok and mo2_ok)
        self.open_mo2_button.setEnabled(base_ok and mo2_ok)
        self.direct_button.setEnabled(
            base_ok and (mo2_ok or direct_ok or anomaly_fallback is not None)
        )
        if mo2_ok:
            self.launch_button.setToolTip("")
            self.open_mo2_button.setToolTip("")
        elif not gamma_installed(profile.gamma, profile.mo2_profile):
            tip = tr(
                "Install GAMMA first - Mod Organizer launches require a "
                "GAMMA installation."
            )
            self.launch_button.setToolTip(tip)
            self.open_mo2_button.setToolTip(tip)
        else:
            self.launch_button.setToolTip("")
            self.open_mo2_button.setToolTip("")
        self.direct_button.setToolTip(
            "" if self.direct_button.isEnabled() else tr("No launch target available")
        )
        if incomplete:
            tip = tr(
                "The last GAMMA install attempt failed - resume it on the Install page before playing."
            )
            self.launch_button.setToolTip(tip)
            self.open_mo2_button.setToolTip(tip)
            self.direct_button.setToolTip(tip)
        if self._coop_on():
            self.direct_button.setEnabled(False)
            self.direct_button.setToolTip(tr("Co-op is active. Anomaly cannot be launched without Mod Organizer 2 in co-op mode."))
        if not self._launching:
            set_hover_grow_text(self.launch_button, self._launch_text())
        self._build_chips(ok=True, runner=runner)
        target = self._selected_target()
        exe = next((e for e in self.executables if e.title == target), Mo2Executable())
        self.target_path.setText(exe.binary or "No executable set for this target")

    def _build_chips(self, *, ok: bool, runner=None) -> None:
        clear_layout(self.chips_row)
        chips: list[tuple[str, bool]] = []
        # The GE-Proton build that will actually be used for this launch
        # (not every installed build - with several installed that made
        # this chip very wide). Both an explicit pick and "auto" resolve
        # through umu-run with PROTONPATH set to the chosen build
        # directory - auto's own Runner.label is just the generic
        # string "Proton", so PROTONPATH is the only reliable source of
        # the actual version in both cases.
        proton_version = ""
        if runner is not None and runner.kind == "umu":
            proton_path = runner.env.get("PROTONPATH", "")
            if proton_path:
                proton_version = Path(proton_path).name.removeprefix("GE-Proton")
        proton_text = (
            f"Proton GE: {proton_version}" if proton_version else "Proton GE: none"
        )
        chips.append((proton_text, bool(proton_version)))
        # GameMode (only if installed AND enabled in Settings)
        if gui_settings.load_gui_settings().get("always_gamemoderun"):
            gamemoderun = available_commands().get("gamemoderun")
            chips.append(("GameMode", bool(gamemoderun)))
        # MangoHud (if installed)
        if shutil.which("mangohud"):
            chips.append(("MangoHud", True))
        if self._coop_on():
            chips.append((tr("Co-op mode"), True))
        self.chips_row.addStretch(1)
        for text, state in chips:
            chip = QLabel(text)
            chip.setObjectName("chip")
            chip.setProperty("state", "ok" if state else "bad")
            chip.style().unpolish(chip)
            chip.style().polish(chip)
            self.chips_row.addWidget(chip)
        self.chips_row.addStretch(1)

    def _save_state(self) -> None:
        # currentData() is None on an empty/uninitialized combo (or if a
        # future Qt/style quirk ever lands the current index on the
        # separator); _runner() and _prefix_for() already fall back to
        # "auto" the same way - persisting None here instead would write
        # "runner": null and a "null" key into the prefixes map.
        runner = self.runner_combo.currentData() or "auto"
        target = self.target_combo.currentText()
        gui_settings.save_gui_settings(
            runner=runner,
            target=target,
            custom_launch_options=self.custom_options_edit.text().strip(),
        )
        profile = self.window.settings.active_profile
        if profile is not None and target:
            try:
                set_mo2_selected_title(profile.gamma, target)
            except OSError:
                pass  # MO2 just keeps its own pick.

    def _on_runner_changed(self, *_args) -> None:
        kind = self.runner_combo.currentData()
        self._save_state()
        self._refresh_preview()
        self._update_runner_hint(kind)

    def _update_runner_hint(self, kind: str | None) -> None:
        if not kind or kind == "auto":
            self.runner_hint.setText(
                tr("Pick a GE-Proton runner, or install one with Manage GE-Proton.")
            )
        elif kind.startswith("umup:"):
            self.runner_hint.setText(tr("GE-Proton — recommended for GAMMA."))
        elif kind == "proton:stable":
            self.runner_hint.setText(
                tr("Steam Proton Stable — may crash with MO2 (concrt140.dll). Use GE-Proton instead if available.")
            )
        elif kind.startswith("proton:"):
            self.runner_hint.setText(
                tr("Steam Proton — may crash with MO2 (concrt140.dll). Use GE-Proton instead if available.")
            )
        else:
            self.runner_hint.setText("")

    def _on_change(self, *_args) -> None:
        self._save_state()
        self._refresh_preview()

    def _on_gamemode_toggled(self, checked: bool) -> None:
        gui_settings.save_gui_settings(always_gamemoderun=bool(checked))
        self._refresh_preview()

    def _on_launch_button_clicked(self) -> None:
        """Route the hero button's click: "Launch Game" while idle,

        "Quit Game" (with confirmation) once a launch is under way - see
        _set_launch_button_state().
        """
        if self._launching:
            self._confirm_quit_game()
        else:
            self.launch_game()

    def _confirm_quit_game(self) -> None:
        reply = QMessageBox.question(
            self,
            tr("Quit the game?"),
            tr("The game closes right away. Anything since your last save is lost."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
            labels={QMessageBox.StandardButton.Yes: tr("Quit game"), QMessageBox.StandardButton.No: tr("Keep playing")},
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._abort_launch(tr("Game closed by user."), error=False)  # the user's choice, not a fault
            self._launch_status_clear_timer.start(5000)

    def _confirm_load_order(self) -> bool:
        """Catch a reversed GAMMA load order before it crashes the game.

        The old Flip Priority button reversed modlist.txt end to end, which
        crashes the game on startup. Offers to put it back first; False
        cancels the launch.
        """
        profile = self.window.settings.active_profile
        if profile is None:
            return True
        path = modlist_path_for(profile.gamma, profile.mo2_profile)
        if path is None:
            return True
        try:
            lines = read_lines(path)
        except (OSError, ValueError):
            return True
        if not looks_flipped(lines):
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("Load order is reversed"))
        box.setText(
            tr(
                "This profile's mod load order is upside down, so GAMMA will "
                "most likely crash on startup. COMMANDER can flip it back "
                "before launching."
            )
        )
        box.setInformativeText(tr("A backup of the current order is saved first."))
        fix = box.addButton(tr("Fix and launch"), QMessageBox.ButtonRole.AcceptRole)
        anyway = box.addButton(tr("Launch anyway"), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(fix)
        box.exec()
        if box.clickedButton() is anyway:
            return True
        if box.clickedButton() is not fix:
            return False
        if mo2_running(force=True):
            QMessageBox.warning(
                self,
                tr("Close Mod Organizer first"),
                tr("Mod Organizer is open and would undo this change when it closes. Close it, then try again."),
            )
            return False
        try:
            backup_before_change(path)
            save_lines(path, unflip(lines))
        except OSError as exc:
            QMessageBox.warning(self, tr("Couldn't fix the load order"), str(exc))
            return False
        return True

    def _coop_on(self) -> bool:
        profile = self.window.settings.active_profile
        return profile is not None and coop.is_active(profile.anomaly)

    def _launch_text(self) -> str:
        return tr("Launch Co-op") if self._coop_on() else tr("Launch Game")

    def _steam_session(self, coop_launch: bool | None) -> bool:
        """True for a co-op launch whose session type is Steam.

        xrRazom's Steam sessions need Steamworks, which under Proton only
        works when the Steam client starts the game itself. On Windows the
        game reaches a running Steam on its own, so the normal launch does.
        """
        profile = self.window.settings.active_profile
        if os.name == "nt" or profile is None:
            return False
        want_coop = coop.is_active(profile.anomaly) if coop_launch is None else coop_launch
        return want_coop and coop.read_settings(profile.anomaly)[0]["xrr_host_steam"]

    def _coop_shortcut_fields(self, profile) -> dict:
        """What the co-op Steam shortcut has to run (see steam_shortcuts)."""
        exe = Path(profile.gamma) / "ModOrganizer.exe"
        if not exe.is_file():
            raise LaunchError(f"ModOrganizer.exe not found in {profile.gamma}")
        runner = self._runner()
        prefix = runner.env.get("STEAM_COMPAT_DATA_PATH") or runner.env.get("WINEPREFIX")
        if runner.kind not in ("umu", "proton") or not prefix:
            raise LaunchError(
                "Steam co-op sessions run through Proton. Pick a Proton runner on the Play page first."
            )
        pfx = Path(prefix) / "pfx"
        if not pfx.exists():
            # Steam's Proton looks for <compat data>/pfx; a umu prefix is
            # the pfx itself, and umu links it the same way.
            pfx.symlink_to(".")
        # The build this prefix already runs on, so Steam never upgrades or
        # downgrades it: umu names it in PROTONPATH, a Proton runner runs
        # its proton script directly (gamemoderun may come first).
        proton_script = next((w for w in runner.wrapper if Path(w).name == "proton"), "")
        proton_dir = (
            runner.env.get("PROTONPATH")
            or (str(Path(proton_script).parent) if proton_script else "")
            or _pick_umu_proton()
        )
        if not proton_dir:
            raise LaunchError(
                "Steam co-op sessions need a GE-Proton build. Install one from the Play page first."
            )
        return {
            "exe": str(exe),
            "start_dir": str(profile.gamma),
            "launch_options": coop_launch_options(
                prefix, self._active_profile_name(), self._selected_target(),
                coop.steam_appid(profile.anomaly),
            ),
            "proton_name": compat_tool_name(proton_dir),
        }

    def _launch_coop_steam(self) -> None:
        """Start co-op through the co-op Steam shortcut, adding it first
        (which needs a Steam restart) when Steam does not have it yet."""
        profile = self.window.settings.active_profile
        reason = coop.launch_block_reason(profile.anomaly, coop_launch=True)
        if reason:
            refuse_launch(self.window, self, reason)
            return
        try:
            unblock_mo2_python(profile.gamma)
            coop.ensure_root_appid(profile.anomaly, profile.gamma, profile.mo2_profile)
            fields = self._coop_shortcut_fields(profile)
        except (LaunchError, coop.CoopError, OSError) as exc:
            QMessageBox.warning(self, tr("Co-op couldn't start"), str(exc))
            return
        vdf_path = pick_shortcuts_vdf(
            self,
            tr("Pick your Steam account"),
            tr("This computer has more than one Steam account. Pick the one you play co-op with:"),
        )
        if vdf_path is None:
            return
        steam_root = steam_root_for(vdf_path)
        snapshot = self._session_snapshot(True, False)
        if coop_shortcut_current(vdf_path, **fields):
            self._set_launch_button_state(True)
            try:
                open_in_steam(steam_root, coop_launch_url(fields["exe"]))
            except OSError as exc:
                self._coop_shortcut_failed(str(exc))
                return
            self._track_steam_session(snapshot)
            return
        restart = steam_running()
        if restart and steam_shortcut_appid() is not None:
            # Started by Steam (Game Mode): restarting Steam would close
            # COMMANDER too, halfway through.
            QMessageBox.information(
                self,
                tr("Start COMMANDER outside Steam once"),
                tr("Co-op needs a STALKER COMMANDER xrRAZOM shortcut in Steam, and adding it means restarting Steam. Steam started COMMANDER, so a restart would close COMMANDER too."),
                points=(
                    tr("Close Steam."),
                    tr("Start COMMANDER on its own (on a Steam Deck, from Desktop Mode)."),
                    tr("Launch co-op once."),
                ),
                info=tr("After that, co-op works from Steam too, until you switch between Call of Pripyat and Spacewar."),
            )
            return
        if restart and QMessageBox.question(
            self,
            tr("Restart Steam to start co-op"),
            tr("Co-op starts from a STALKER COMMANDER xrRAZOM shortcut in your Steam library. COMMANDER needs to add or update it, and Steam only picks up the change after a restart."),
            points=(
                tr("Steam closes and opens again."),
                tr("The game then starts in co-op."),
                tr("You only see this the first time, and after switching between Call of Pripyat and Spacewar."),
            ),
            note=tr("Anything running through Steam right now will close."),
            buttons=QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            labels={QMessageBox.StandardButton.Ok: tr("Restart Steam")},
        ) != QMessageBox.StandardButton.Ok:
            return
        self._set_launch_button_state(True)
        self._set_result(
            tr("Restarting Steam to update the STALKER COMMANDER xrRAZOM shortcut")
            if restart
            else tr("Adding the STALKER COMMANDER xrRAZOM shortcut to Steam")
        )
        self._coop_shortcut_task = BackgroundTask(
            setup_coop_shortcut, vdf_path, restart=restart, parent=self, **fields
        )
        self._coop_shortcut_task.result.connect(
            lambda _r: self._track_steam_session(snapshot)
        )
        self._coop_shortcut_task.error.connect(self._coop_shortcut_failed)
        self._coop_shortcut_task.start()

    def _coop_shortcut_failed(self, error: str) -> None:
        self._set_launch_button_state(False)
        self._set_result(tr("Couldn't add the STALKER COMMANDER xrRAZOM shortcut to Steam."), error=True)
        QMessageBox.warning(self, tr("Couldn't set up co-op in Steam"), error)

    def _track_steam_session(self, snapshot: tuple) -> None:
        """Track the session Steam was just asked to start, by process
        name - there is no wrapper of ours to watch."""
        self._launch_status_clear_timer.stop()
        self._cancel_crash_poll()
        self._crash_prompted = False
        self._proc = None
        self._launch_wrapper_pid = None
        self._arm_session("GAMMA", True, snapshot)
        self._steam_launch = True
        try:
            # Steam's Proton runs in the same prefix, so its winedbg
            # processes carry this runner's prefix too.
            self._debugger_env = self._runner().env
        except LaunchError:
            self._debugger_env = {}
        # Steam may still be starting (or restarting), then Proton: allow
        # two minutes for MO2 to appear rather than the usual 2.5 s.
        self._handoff_limit = 480
        self._start_launch_timer("GAMMA", [], None)
        self._set_result(tr("Starting GAMMA through Steam"))

    def launch_game(self, *, coop_launch: bool | None = None) -> None:
        """Launch the selected game target using the primary Play workflow.

        ``coop_launch`` None follows the installed mode: with co-op applied
        every launch here is a co-op launch, never a single-player one.
        """
        # Claim the launch before resolving a runner. Resolution probes the
        # filesystem and can take long enough for a second click to arrive.
        if self._launching or self._install_busy:
            return
        if not self._selected_target():
            QMessageBox.warning(
                self,
                tr("Pick what to launch"),
                tr("Choose a game in the Launch target list, then press Launch Game again."),
                info=tr("Is the list empty? Check that your profile points to a GAMMA install with a set-up ModOrganizer.ini."),
            )
            return
        if not self._confirm_load_order():
            return
        profile = self.window.settings.active_profile
        if (
            profile is not None
            and (coop.is_active(profile.anomaly) if coop_launch is None else coop_launch)
            and not confirm_player_name(self, profile.anomaly)
        ):
            return
        if self._steam_session(coop_launch):
            self._launch_coop_steam()
            return
        self._set_launch_button_state(True)
        # Try MO2 first; fall back to direct launch if MO2 is unavailable.
        try:
            runner = self._runner()
            self._resolve_command(open_mo2=False, direct=False, runner=runner)
            self._run(open_mo2=False, direct=False, runner=runner, coop_launch=coop_launch)
        except LaunchError as exc:
            self._set_launch_button_state(False)
            answer = QMessageBox.question(
                self,
                tr("Mod Organizer can't start"),
                tr("Mod Organizer couldn't be set up for this launch:\n{exc}", exc=exc),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                note=tr("You can start Anomaly directly instead, but then none of the GAMMA mods are loaded."),
                labels={QMessageBox.StandardButton.Yes: tr("Launch Anomaly without mods"), QMessageBox.StandardButton.No: tr("Cancel")},
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._launch_direct()
        except Exception as exc:  # noqa: BLE001 - recover the launch controls
            self._set_launch_button_state(False)
            self._set_result(tr("Couldn't get the game ready to start: {error}", error=exc), error=True)
            QMessageBox.warning(self, tr("The game couldn't start"), str(exc))

    def _open_mo2(self) -> None:
        if self._launching or self._install_busy:
            return
        self._set_launch_button_state(True)
        self._run(open_mo2=True, direct=False)

    def _launch_direct(self) -> None:
        if self._launching or self._install_busy:
            return
        if self._selected_target() is None:
            # Anomaly-only installs may have no combo selection yet.
            fallback = next(
                (e.title for e in self.executables if e.title == "Anomaly"), None
            )
            if fallback is not None:
                self.target_combo.setCurrentText(fallback)
        self._set_launch_button_state(True)
        self._run(open_mo2=False, direct=True)

    def _run(self, *, open_mo2: bool, direct: bool, runner=None, coop_launch: bool | None = None) -> None:
        """Launch the game through Mod Organizer 2 or directly."""
        # Every launch (Play, Dashboard, Multiplayer) lands here: co-op on
        # means co-op only, off means single-player only.
        profile = self.window.settings.active_profile
        if profile is not None:
            want_coop = coop.is_active(profile.anomaly) if coop_launch is None else coop_launch
            reason = coop.launch_block_reason(profile.anomaly, coop_launch=want_coop, direct=direct)
            if reason:
                self._set_launch_button_state(False)
                refuse_launch(self.window, self, reason)
                return
            if not direct:
                try:
                    unblock_mo2_python(profile.gamma)
                except OSError:
                    pass  # MO2 shows its own error then.
        self._launch_status_clear_timer.stop()
        # A crash-dump poll from the previous session must not fire a
        # warning attributed to this brand-new one (e.g. a quick retry
        # launched while the old poll's 90s window was still open).
        self._cancel_crash_poll()
        self._crash_prompted = False
        log_path = logs_dir() / "launcher.log"
        # launch_detached raises LaunchError too (spawn failures); if that
        # escapes, _launching stays True and every launch button stays dead.
        try:
            if runner is None:
                runner = self._runner()
            command, env, cwd = self._resolve_command(
                open_mo2=open_mo2, direct=direct, runner=runner
            )
            ensure_runner_prefix(runner)
            label = (
                "STALKER ANOMALY"
                if direct
                else ("Mod Organizer 2" if open_mo2 else "GAMMA")
            )
            monitoring_mo2 = not direct and os.name != "nt"
            snapshot = self._session_snapshot(monitoring_mo2, open_mo2)
            self._proc = launch_detached(
                command, env, cwd, log_path=log_path, registry=self._registry
            )
            self._launch_wrapper_pid = self._proc.pid
            self._arm_session(label, monitoring_mo2, snapshot)
            self._debugger_env = runner.env
        except ForeignPrefixError as exc:
            self._abort_launch(tr("Couldn't start the game: {error}", error=exc))
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(tr("The game couldn't start"))
            box.setText(str(exc))
            repair_button = box.addButton(
                tr("Repair now"), QMessageBox.ButtonRole.ActionRole
            )
            box.addButton(QMessageBox.StandardButton.Close)
            box.exec()
            if box.clickedButton() is repair_button:
                self._repair_prefix()
            return
        except LaunchError as exc:
            self._abort_launch(tr("Couldn't start the game: {error}", error=exc))
            QMessageBox.warning(self, tr("The game couldn't start"), str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._abort_launch(tr("Something went wrong starting the game: {error}", error=exc))
            QMessageBox.warning(self, tr("The game couldn't start"), str(exc))
            return
        self._start_launch_timer(label, command, log_path)
        self._set_result(tr("Launching {name}", name=label))

    def _start_launch_timer(self, label: str, command: list[str], log_path: Path | None) -> None:
        # Use a repeating timer to detect fast failures without delaying recovery,
        # so buttons are re-enabled as soon as possible regardless of timing.
        self._launch_timer = QTimer(self)
        self._launch_timer.setInterval(250)
        self._launch_timer.timeout.connect(
            lambda: self._on_launch_check(label, command, log_path)
        )
        self._launch_timer.start()

    def _session_snapshot(self, monitoring_mo2: bool, open_mo2: bool) -> tuple:
        """What already runs/exists before a launch, for _arm_session()."""
        # Snapshot pre-existing MO2 processes before spawning so handoff
        # detection can tell this launch's own instance apart from one
        # the user already had open (see mo2_pids() docstring) - taken
        # after launch_detached() a fast wrapper could already have
        # started MO2, making its pid look pre-existing and breaking
        # handoff detection.
        pre_launch_mo2_pids = mo2_pids() if monitoring_mo2 else set()
        # Same idea, but for the actual game executable MO2 will launch
        # (not MO2 itself) - MO2 stays running after the game closes,
        # so playtime must be recorded on the game exiting, not MO2.
        game_exe_name = None
        if monitoring_mo2 and not open_mo2:
            target = self._selected_target()
            game_binary = next(
                (e.binary for e in self.executables if e.title == target),
                None,
            )
            game_exe_name = Path(game_binary).name if game_binary else None
        pre_launch_game_pids = (
            exe_pids(game_exe_name) if game_exe_name else set()
        )
        # Every game session: note the crash dumps that already exist,
        # so one that appears afterwards can be offered for analysis
        # (see _check_for_crash()). A single directory glob.
        crash_check_pending = False
        pre_launch_crash_dumps: set[str] = set()
        if not open_mo2:
            launch_profile = self.window.settings.active_profile
            if launch_profile is not None:
                crash_check_pending = True
                pre_launch_crash_dumps = crash_dump_names(launch_profile.anomaly)
        return (
            pre_launch_mo2_pids, game_exe_name, pre_launch_game_pids,
            crash_check_pending, pre_launch_crash_dumps,
        )

    def _arm_session(self, label: str, monitoring_mo2: bool, snapshot: tuple) -> None:
        """Start tracking a session that was just started (playtime, crash
        check, Discord, MO2/game exit) - see _on_launch_check()."""
        (
            pre_launch_mo2_pids, game_exe_name, pre_launch_game_pids,
            crash_check_pending, pre_launch_crash_dumps,
        ) = snapshot
        # Not "Open MO2" - that opens the mod manager, not the game.
        is_game_session = label != "Mod Organizer 2"
        self._launch_started_at = time.monotonic() if is_game_session else None
        self._discord_wanted = False
        if is_game_session:
            gui_state = gui_settings.load_gui_settings()
            if gui_state.get("discord_rpc_enabled"):
                self._discord_wanted = True
                self._discord_started_at = time.time()
                self._try_start_discord_presence()
        self._monitoring_mo2 = monitoring_mo2
        self._mo2_seen = False
        self._handoff_checks = 0
        self._handoff_limit = 10
        self._steam_launch = False
        self._pre_launch_mo2_pids = pre_launch_mo2_pids
        self._mo2_launch_pids = set()
        self._game_exe_name = game_exe_name
        self._pre_launch_game_pids = pre_launch_game_pids
        self._game_seen = False
        self._crash_check_pending = crash_check_pending
        self._pre_launch_crash_dumps = pre_launch_crash_dumps
        applog.get_logger().info(
            "play: launch label=%s monitoring_mo2=%s game_exe_name=%s",
            label, monitoring_mo2, game_exe_name,
        )

    def _stop_launch_timer(self) -> None:
        timer = getattr(self, "_launch_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._launch_timer = None

    def _break_crash_loop(self, log_path: Path | None) -> None:
        """Stop a Wine crash loop, then say what it was and how to fix it."""
        self._abort_launch(
            tr("Wine crashed repeatedly while starting Mod Organizer - stopped before it could exhaust memory.")
        )
        try:
            runner_env = self._runner().env
        except (LaunchError, OSError, ValueError):
            runner_env = None
        _kill_stray_debuggers(runner_env)
        message = tr(
            "Wine kept crashing on everything it started, so COMMANDER stopped "
            "the launch before it used up all your memory."
        )
        foreign: list[str] = []
        try:
            runner = self._runner()
            prefix = runner.env.get("STEAM_COMPAT_DATA_PATH") or runner.env.get("WINEPREFIX")
            if prefix:
                foreign = prefix_foreign_dlls(prefix, runner)
        except LaunchError:
            pass
        if foreign:
            message += "\n\n" + tr(
                "The cause is in the game's Wine prefix: {files} came from a "
                "different Wine version, so nothing Proton starts can load.",
                files=", ".join(foreign),
            )
            reply = QMessageBox.question(
                self,
                tr("The Wine prefix is damaged"),
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
                info=tr("After the repair, run Install Dependencies on the Install page again."),
                labels={QMessageBox.StandardButton.Yes: tr("Repair prefix"), QMessageBox.StandardButton.No: tr("Not now")},
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._repair_prefix()
            return
        if log_path is None:
            QMessageBox.warning(self, tr("The game couldn't start"), message)
            return
        detail = "\n".join(read_log_tail(log_path, 4096).rstrip().splitlines()[-8:])
        QMessageBox.warning(
            self,
            tr("The game couldn't start"),
            message,
            info=tr("Last log lines:") + "\n" + detail,
        )

    def _repair_prefix(self) -> None:
        """Put the runner's own DLLs back and tell the user what comes next."""
        if self._repair_task is not None:
            return
        try:
            runner = self._runner()
        except LaunchError as exc:
            QMessageBox.warning(self, tr("Couldn't repair the prefix"), str(exc))
            return
        prefix = runner.env.get("STEAM_COMPAT_DATA_PATH") or runner.env.get("WINEPREFIX")
        if not prefix:
            QMessageBox.information(
                self,
                tr("Prefix repaired"),
                tr("{count} file(s) were put back to the versions that match your runner.", count=0),
                note=tr("Now run Install Dependencies on the Install page. Some of the files it installed were replaced."),
            )
            return
        from ..repair import repair_prefix_foreign_dlls

        # Byte-compares every DLL in the prefix against every host Wine
        # build found - real disk I/O, run off the GUI thread. The launch
        # this repairs has already stopped/aborted by the time this runs.
        task = BackgroundTask(repair_prefix_foreign_dlls, prefix, runner, parent=self)
        task.result.connect(self._on_repair_done)
        task.error.connect(self._on_repair_error)
        self._repair_task = task
        task.start()

    def _on_repair_error(self, message: str) -> None:
        self._repair_task = None
        QMessageBox.warning(self, tr("Couldn't repair the prefix"), message)

    def _on_repair_done(self, repaired) -> None:
        self._repair_task = None
        QMessageBox.information(
            self,
            tr("Prefix repaired"),
            tr("{count} file(s) were put back to the versions that match your runner.", count=len(repaired)),
            note=tr("Now run Install Dependencies on the Install page. Some of the files it installed were replaced."),
        )

    def _abort_launch(self, message: str, *, error: bool = True) -> None:
        """Kill any spawned wrapper, release the launch lock, and report."""
        proc = self._proc
        self._proc = None
        if proc is not None:
            # The launcher wrapper failed or the setup after spawn raised;
            # terminate its process group so no orphaned Wine processes linger.
            self._registry.cleanup(proc)
        elif self._launch_wrapper_pid is not None:
            # The wrapper itself already exited (MO2 keeps running
            # independently of it after handoff), but the whole tree
            # still shares its original process group - killing by that
            # remembered pid still reaches MO2/the game.
            _terminate_process_group(self._launch_wrapper_pid)
        elif self._steam_launch:
            # Steam's reaper owns the process group, and killing that group
            # could take Steam's own helpers with it - only this launch's
            # MO2 and game processes go.
            pids = mo2_pids() - self._pre_launch_mo2_pids
            if self._game_exe_name:
                pids |= exe_pids(self._game_exe_name) - self._pre_launch_game_pids
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        self._steam_launch = False
        self._launch_wrapper_pid = None
        self._monitoring_mo2 = False
        self._stop_launch_timer()
        self._set_result(message, error=error)
        self._set_launch_button_state(False)
        # Recompute real availability instead of blindly re-enabling: the
        # install this launched target depended on may have been broken by
        # another page while the game was running.
        self._refresh_preview()

    def _record_playtime(self) -> None:
        """Add this session's elapsed time to the active profile's total.

        Only called from a "closed normally" path - a launch that never
        got as far as actually running (or crashed immediately) has
        nothing meaningful to add, and that distinction is exactly what
        _launch_started_at being None (see _launch(), "Open MO2" is
        excluded too) or unset already encodes.
        """
        started_at = self._launch_started_at
        self._launch_started_at = None
        if started_at is None:
            applog.get_logger().info("play: record_playtime no-op (started_at is None)")
            return
        profile = self.window.settings.active_profile
        if profile is None:
            applog.get_logger().info("play: record_playtime no-op (no active profile)")
            return
        elapsed = time.monotonic() - started_at
        if elapsed <= 0:
            applog.get_logger().info(
                "play: record_playtime no-op (elapsed=%.1fs)", elapsed
            )
            return
        gui_state = gui_settings.load_gui_settings()
        playtime = dict(gui_state.get("playtime_seconds", {}))
        playtime[profile.profile_name] = playtime.get(profile.profile_name, 0.0) + elapsed
        last_played = dict(gui_state.get("last_played_ts", {}))
        last_played[profile.profile_name] = time.time()
        gui_settings.save_gui_settings(
            playtime_seconds=playtime, last_played_ts=last_played
        )
        applog.get_logger().info(
            "play: recorded %.1fs playtime for profile %s", elapsed, profile.profile_name
        )
        self._update_playtime_label()

    def _cancel_crash_poll(self) -> None:
        if self._crash_poll_timer is not None:
            self._crash_poll_timer.stop()
            self._crash_poll_timer = None

    def _check_for_crash(self) -> None:
        """Offer crash analysis if a crash dump appears after a session.

        A new X-Ray minidump (.mdmp) in the Anomaly install's log folder
        reliably indicates a native engine crash, regardless of whether
        the session went through MO2 or a direct launch - checking for
        one this way sidesteps needing this app's own (unreliable,
        MO2-mediated) process-exit-code tracking. Every game session is
        checked. The dump-file check is a bounded poll (_poll_for_crash),
        not a single immediate check, since X-Ray can take a while to
        finish writing it after the session is already reported closed.
        """
        if not self._crash_check_pending:
            return
        self._crash_check_pending = False
        profile = self.window.settings.active_profile
        if profile is None:
            return
        self._cancel_crash_poll()
        self._crash_poll_anomaly = profile.anomaly
        self._crash_poll_baseline = self._pre_launch_crash_dumps
        self._crash_poll_attempts_left = _CRASH_POLL_MAX_ATTEMPTS
        self._poll_for_crash()

    def _poll_for_crash(self) -> None:
        new_dumps = (
            crash_dump_names(self._crash_poll_anomaly) - self._crash_poll_baseline
        )
        if new_dumps:
            self._crash_poll_timer = None
            title = tr("The game crashed")
            message = tr(
                "The game wrote a new crash log, so it looks like it crashed."
            )
            message += "\n\n" + tr(
                "Analyze crash gathers the logs and opens them in ASSISTANT, "
                "which explains the error and suggests a fix."
            )
            is_active = getattr(self.window, "isActiveWindow", lambda: True)()
            if not is_active:
                notify_desktop(title, message)
            self._crash_prompted = True
            if self._ask_analyze_crash(title, message):
                self._start_crash_report(open_assistant=True)
            return
        self._crash_poll_attempts_left -= 1
        # The timer that just fired is done; parented to the page, each of
        # up to ~45 attempts per session used to stay alive.
        if self._crash_poll_timer is not None:
            self._crash_poll_timer.deleteLater()
        if self._crash_poll_attempts_left <= 0:
            self._crash_poll_timer = None
            return
        self._crash_poll_timer = QTimer(self)
        self._crash_poll_timer.setSingleShot(True)
        self._crash_poll_timer.timeout.connect(self._poll_for_crash)
        self._crash_poll_timer.start(_CRASH_POLL_INTERVAL_MS)

    def _update_playtime_label(self) -> None:
        profile = self.window.settings.active_profile
        if profile is None:
            self.playtime_label.setText("")
            return
        playtime_seconds = gui_settings.load_gui_settings().get(
            "playtime_seconds", {}
        ).get(profile.profile_name, 0.0)
        self.playtime_label.setText(
            tr("Total playtime: {arg}", arg=format_playtime(playtime_seconds))
        )
        self.launch_status_changed.emit()

    def _try_start_discord_presence(self) -> None:
        """Connect and publish the presence. Called at launch, then retried
        from the launch tick every so often while Discord isn't reachable,
        so starting Discord after the game still picks it up.

        The IPC connect (and the handshake/SET_ACTIVITY round-trip) is a
        blocking socket call with up to a ~2s timeout per candidate path -
        if Discord's socket file exists but the process is wedged, that
        stall would otherwise repeat on every retry for the whole play
        session. Runs off the GUI thread for exactly that reason.
        """
        if self._discord_task is not None:
            return
        self._discord_last_attempt = time.monotonic()
        gui_state = gui_settings.load_gui_settings()
        client_id = effective_client_id(gui_state.get("discord_client_id"))
        state = discord_presence_state(self.window.settings.active_profile, gui_state)
        started_at = self._discord_started_at

        def _connect_and_update():
            rpc = start_presence(client_id)
            if rpc is not None:
                update_presence(rpc, DETAILS_TEXT, started_at, state=state)
            return rpc

        task = BackgroundTask(_connect_and_update, parent=self)
        self._discord_task = task
        task.result.connect(self._on_discord_presence_started)
        task.error.connect(self._on_discord_presence_error)
        task.start()

    def _on_discord_presence_started(self, rpc) -> None:
        self._discord_task = None
        if not self._discord_wanted:
            # Presence was turned off (or the launch already finished)
            # while this was connecting - don't leave it dangling.
            stop_presence(rpc)
            return
        self._discord_rpc = rpc

    def _on_discord_presence_error(self, _message: str) -> None:
        self._discord_task = None

    def _retry_discord_presence(self) -> None:
        if not self._discord_wanted or self._discord_rpc is not None:
            return
        if time.monotonic() - self._discord_last_attempt < _DISCORD_RETRY_SECONDS:
            return
        self._try_start_discord_presence()

    def _stop_discord_presence(self) -> None:
        self._discord_wanted = False
        stop_presence(self._discord_rpc)
        self._discord_rpc = None

    def _finish_launch(self, message: str, *, error: bool = False) -> None:
        """Release the launch lock, stop monitoring, and show a final status."""
        self._stop_discord_presence()
        self._launch_wrapper_pid = None
        self._monitoring_mo2 = False
        self._stop_launch_timer()
        self._set_launch_button_state(False)
        self._refresh_preview()
        self._set_result(message, error=error)
        self._launch_status_clear_timer.start(3000)

    def _on_launch_check(self, label: str, command: list[str], log_path: Path | None) -> None:
        """Check the wrapper and, for MO2, the handoff process."""
        # Crash-loop breaker, checked before anything else on every tick. A
        # prefix carrying another Wine's ntdll makes every process fault;
        # Wine answers each fault by starting winedbg, whose process faults
        # too. Left alone that is a fork bomb that exhausts memory in about
        # two minutes and freezes the machine. Kill it the moment the
        # pattern is recognisable.
        # Read from the log when COMMANDER has it, and from the process list
        # always - a Steam co-op launch leaves no log of ours.
        if (
            log_path is not None and runner_crash_loop(read_log_tail(log_path))
        ) or debugger_crash_loop(self._debugger_env):
            self._break_crash_loop(log_path)
            return
        self._retry_discord_presence()
        proc = getattr(self, "_proc", None)
        if proc is None:
            if self._monitoring_mo2:
                # umu/proton may be only a wrapper. Keep the lock while MO2
                # appears, and allow a short startup window after wrapper exit.
                # Tracking this launch's own MO2 PID(s) (rather than just
                # "is any MO2 running") means a pre-existing MO2 window the
                # user already had open cannot make the buttons stay
                # disabled forever after this launch's instance closes.
                if not self._mo2_seen:
                    new_pids = mo2_pids() - self._pre_launch_mo2_pids
                    if new_pids:
                        self._mo2_seen = True
                        self._mo2_launch_pids = new_pids
                        self._handoff_checks = -1
                        # Sub-second polling is only needed to catch the
                        # handoff quickly; MO2 can stay open for hours after.
                        self._launch_timer.setInterval(2000)
                        self._set_result(tr("Mod Organizer 2 is running"))
                        applog.get_logger().info(
                            "play: mo2 handoff detected pids=%s", new_pids
                        )
                        return
                    if self._handoff_checks < self._handoff_limit:
                        self._handoff_checks += 1
                        return
                    if self._steam_launch:
                        # Nothing came and went with a wrapper here: Steam
                        # simply never started it. Not a session to record.
                        self._crash_check_pending = False
                        self._finish_launch(
                            tr("Steam didn't start the game. Check Steam for an error, then try again."),
                            error=True,
                        )
                        return
                    # On some runner setups (confirmed via commander.log on
                    # a real system: umu-run/GE-Proton here) the wrapper
                    # process does not detach after starting MO2 - it
                    # blocks until the whole Wine session, MO2 included,
                    # has already closed. By the time the wrapper's own
                    # exit gets us here, MO2 genuinely is not running
                    # anymore (it already came and went together with the
                    # wrapper), not "still starting up" - treating that as
                    # an error left every such session's playtime
                    # unrecorded. A real launch failure before MO2 ever
                    # started would have shown up as a non-zero wrapper
                    # exit code already, handled separately above.
                    applog.get_logger().info(
                        "play: wrapper exited without a separate MO2 "
                        "process ever appearing - treating as a normal "
                        "close (pre_launch_mo2_pids=%s)",
                        self._pre_launch_mo2_pids,
                    )
                    self._record_playtime()
                    self._check_for_crash()
                    self._finish_launch(tr("{name} closed normally.", name=label))
                    return
                if self._game_exe_name:
                    # MO2 stays running after the game it launched closes,
                    # so wait for the game itself to exit rather than MO2
                    # to record playtime that actually reflects play time.
                    new_game_pids = (
                        exe_pids(self._game_exe_name) - self._pre_launch_game_pids
                    )
                    if new_game_pids and not self._game_seen:
                        self._game_seen = True
                        applog.get_logger().info(
                            "play: game process detected exe=%s pids=%s",
                            self._game_exe_name, new_game_pids,
                        )
                    elif not new_game_pids and self._game_seen:
                        applog.get_logger().info(
                            "play: game process gone exe=%s, recording playtime",
                            self._game_exe_name,
                        )
                        self._record_playtime()
                        self._game_exe_name = None
                        # MO2 usually stays open after the game exits, so
                        # look for a crash dump now, not only once MO2 goes.
                        self._check_for_crash()
                if not (mo2_pids() & self._mo2_launch_pids):
                    applog.get_logger().info(
                        "play: mo2 exited, finishing launch (game_seen=%s)",
                        self._game_seen,
                    )
                    self._record_playtime()
                    self._check_for_crash()
                    self._finish_launch(tr("{name} closed normally.", name=label))
                    return
                # _game_exe_name is cleared once the game exits, back to MO2.
                self._set_result(
                    tr("Game is running") if self._game_seen and self._game_exe_name
                    else tr("Mod Organizer 2 is running")
                )
                return
            self._stop_launch_timer()
            return
        if proc.poll() is None:
            if self._monitoring_mo2:
                # Some runners (umu/GE-Proton) keep the wrapper alive for the
                # whole session, so the handoff branch above never runs -
                # still say what's actually running instead of "Launching".
                if self._game_exe_name and exe_pids(self._game_exe_name) - self._pre_launch_game_pids:
                    status = tr("Game is running")
                elif mo2_pids() - self._pre_launch_mo2_pids:
                    status = tr("Mod Organizer 2 is running")
                else:
                    return
                # Found it: no need to scan the process list 4x a second.
                self._launch_timer.setInterval(2000)
                if self.launch_live_status.text() != status:
                    self._set_result(status)
            return
        if proc.poll() is not None:
            # Process has exited -- re-enable buttons and stop timer
            proc_code = proc.returncode
            self._proc = None
            self._registry.discard(proc)
            applog.get_logger().info(
                "play: wrapper exited code=%s monitoring_mo2=%s",
                proc_code, self._monitoring_mo2,
            )
            if proc_code == 0 and self._monitoring_mo2:
                self._set_result(tr("Waiting for Mod Organizer 2 to open"))
                return
            self._launch_wrapper_pid = None
            self._monitoring_mo2 = False
            self._stop_launch_timer()
            self._set_launch_button_state(False)
            self._refresh_preview()
            self._stop_discord_presence()
            code = proc_code
            if code == 0:
                self._record_playtime()
                self._check_for_crash()
                self._set_result(tr("{name} closed normally.", name=label))
            else:
                self._check_for_crash()
                detail = self._log_tail(log_path)
                msg = tr("{name} closed with an error (code {code})", name=label, code=code)

                msg += "\n\n" + tr("Command: {command}", command=shlex.join(command))
                if detail:
                    msg += "\n\n" + tr("Last log lines:") + f"\n{detail}"
                self._set_result(msg, error=True)
                if runner_graphics_error(detail):
                    graphics_message = tr(
                        "The game started with WineD3D instead of DXVK/Vulkan, "
                        "which usually means the Vulkan driver is missing."
                    )
                    QMessageBox.warning(
                        self,
                        tr("Vulkan isn't working"),
                        graphics_message,
                        points=(
                            tr("Install the right Vulkan driver for your graphics card."),
                            tr("Refresh System Check."),
                            tr("Remove PROTON_USE_WINED3D=1 from the launch options, if it's there."),
                        ),
                    )
                elif runner_prefix_error(detail):
                    runner = self._runner()
                    compatibility_message = tr(
                        "The selected runner couldn't use the game's Wine prefix. "
                        "This usually means a different runner version made the "
                        "prefix, or is using it right now.\n\n"
                        "Runner: {runner}\nPrefix: {prefix}",
                        runner=runner.label,
                        prefix=self._prefix_for(kind=self.runner_combo.currentData()),
                    )
                    fixes = [
                        tr("Pick the runner that made this prefix on the Play page."),
                    ]
                    if "concrt140.dll" in detail.lower():
                        fixes.append(tr(
                            "Mod Organizer needs concrt140.dll, which some Proton/Wine "
                            "versions don't have. Switch to GE-Proton and try again."
                        ))
                    info = (
                        tr(
                            "The message about Qt6Pdf.dll is harmless and isn't "
                            "what caused this."
                        )
                        if "qtpdf.dll" in detail.lower()
                        else ""
                    )
                    QMessageBox.warning(
                        self,
                        tr("Runner and prefix don't match"),
                        compatibility_message,
                        points=tuple(fixes),
                        info=info,
                        note=tr("Don't switch runners while the prefix is in use."),
                    )
                else:
                    QMessageBox.warning(self, tr("The game couldn't start"), msg)
                self._offer_crash_report()
            self._launch_status_clear_timer.start(3000)

    def _ask_analyze_crash(self, title: str, message: str) -> bool:
        """The crash notice. True when the user chose Analyze Crash."""
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(title)
        dialog.setText(message)
        analyze = dialog.addButton(tr("Analyze crash"), QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton(QMessageBox.StandardButton.Close)
        dialog.setDefaultButton(analyze)
        dialog.exec()
        return dialog.clickedButton() is analyze

    def _offer_crash_report(self) -> None:
        """Offer to bundle logs into a report right after a failed launch.

        Turns "the game crashed" straight into a ready-to-share bug report
        with one extra click, reusing the same log-dump/ASSISTANT pieces
        Utilities' "Create Log Dump" button already uses - nothing new to
        maintain, just a second entry point at the moment it's most useful.
        """
        if getattr(self, "_crash_prompted", False):
            # The crash notice already offered a log dump for this session.
            return
        answer = QMessageBox.question(
            self,
            tr("Create a bug report?"),
            tr(
                "COMMANDER can pack the logs and system info into one file you can attach when you report the problem."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            labels={QMessageBox.StandardButton.Yes: tr("Create report"), QMessageBox.StandardButton.No: tr("No thanks")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_crash_report()

    def _start_crash_report(self, *, open_assistant: bool = False) -> None:
        if self._crash_report_task is not None:
            return
        self._crash_report_opens_assistant = open_assistant
        if open_assistant:
            self._set_result(tr("Collecting logs for ASSISTANT"))
        task = BackgroundTask(create_log_dump, parent=self)
        self._crash_report_task = task
        task.result.connect(self._on_crash_report_done)
        task.error.connect(self._on_crash_report_error)
        task.start()

    def _on_crash_report_done(self, result: object) -> None:
        self._crash_report_task = None
        if not isinstance(result, (tuple, list)) or len(result) != 2:
            return
        path, _stats = result
        if getattr(self, "_crash_report_opens_assistant", False):
            # One click from the crash notice: straight into ASSISTANT.
            self._crash_report_opens_assistant = False
            try:
                launch_assistant(path)
                self._set_result(tr("Crash analysis opened in ASSISTANT."))
            except AssistantLaunchError as exc:
                QMessageBox.information(
                    self,
                    tr("ASSISTANT couldn't open"),
                    str(exc),
                    info=tr("The log dump is saved here:\n{path}", path=str(path)),
                )
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle(tr("Log dump created"))
        dialog.setText(tr("Saved to:\n{path}", path=str(path)))
        dialog.setInformativeText(tr("Attach this file when you ask for help, or open it in ASSISTANT to see what went wrong."))
        open_button = dialog.addButton(
            tr("Open in ASSISTANT"), QMessageBox.ButtonRole.AcceptRole
        )
        dialog.addButton(QMessageBox.StandardButton.Close)
        dialog.exec()
        if dialog.clickedButton() is open_button:
            try:
                launch_assistant(path)
            except AssistantLaunchError as exc:
                QMessageBox.information(self, tr("ASSISTANT couldn't open"), str(exc))

    def _on_crash_report_error(self, message: str) -> None:
        self._crash_report_task = None
        QMessageBox.warning(self, tr("Couldn't create the log dump"), message)

    def _log_tail(self, path: Path, limit: int = 12) -> str:
        # Bounded read: a crash loop grows launcher.log by megabytes, and this
        # must never be the thing that makes COMMANDER itself heavy.
        lines = read_log_tail(path).rstrip().splitlines()
        return "\n".join(lines[-limit:])

    def _set_result(self, text: str, *, error: bool = False) -> None:
        color = WARN.name() if error else ACCENT.name()
        self._launch_status_clear_timer.stop()
        self.launch_live_status.setStyleSheet(f"color: {color};")
        self.launch_live_status.setText(notice(text))
        self.launch_status_changed.emit()

    def _clear_launch_status(self) -> None:
        self.launch_live_status.clear()
        self.launch_status_changed.emit()

    def _add_desktop_shortcut(self) -> None:
        """Write a .desktop shortcut for the current target + runner."""
        if self._launching or self._install_busy:
            return
        target = self._selected_target()
        if not target:
            QMessageBox.warning(
                self,
                tr("Pick what to launch"),
                tr("Choose a game in the Launch target list, then add the shortcut again."),
                info=tr("Is the list empty? Check that your profile points to a GAMMA install with a set-up ModOrganizer.ini."),
            )
            return
        try:
            runner = self._runner()
            command, env, cwd = self._resolve_command(
                open_mo2=False, direct=False, runner=runner
            )
            # Copied somewhere permanent first: inside an AppImage the
            # bundled PNG lives in a /tmp mount that vanishes on exit.
            from ..steam_shortcuts import bundled_icon, install_icon

            icon = install_icon(bundled_icon())
            path = write_desktop_shortcut(target, command, env, cwd, icon=icon or None)
        except (LaunchError, OSError) as exc:
            QMessageBox.warning(self, tr("Couldn't create the shortcut"), str(exc))
            return
        self._set_result(tr("Shortcut saved: {path}", path=path))
        QMessageBox.information(
            self, tr("Shortcut created"), tr("The shortcut is on your desktop:\n{path}", path=path)
        )

    def _set_launch_button_state(self, launching: bool) -> None:
        self._launching = launching
        # Stays clickable while launching - it becomes "Quit Game" so the
        # user always has a way to force-close a launch (MO2, the game,
        # or a hung wrapper) instead of it running unbounded.
        #
        # install_hover_grow_text() (see common.py) clears this button's
        # own .text() permanently and paints an overlay label instead -
        # plain setText() has no visible effect and would also silently
        # re-populate the button's real text, showing both the overlay's
        # stale text and the new real text at once. set_hover_grow_text()
        # updates the overlay, which is what's actually visible.
        set_hover_grow_text(
            self.launch_button, tr("Quit Game") if launching else self._launch_text()
        )
        self.launch_button.setEnabled(True)
        self.open_mo2_button.setEnabled(not launching)
        self.direct_button.setEnabled(not launching and not self._coop_on())
        self.shortcut_button.setEnabled(not launching)
        self.launch_state_changed.emit(launching)

    def on_busy_changed(self, busy: bool) -> None:
        """Prevent launch actions from racing an install or reset operation."""
        self._install_busy = busy
        if busy:
            # While a game runs this button is "Quit Game" - it must stay
            # usable (a hung game, a log dump or backup started meanwhile).
            if not self._launching:
                self.launch_button.setEnabled(False)
            self.open_mo2_button.setEnabled(False)
            self.direct_button.setEnabled(False)
            self.shortcut_button.setEnabled(False)
        else:
            self._refresh_preview()
