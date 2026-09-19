"""Play page: the app's front page - launch the game through Mod Organizer.

A hero-style layout: a two-column grid lets the player pick the game target
("Launch Game") and the runner ("Select Runner") as equally important steps,
with the launch actions and a copyable command preview below.
"""

from __future__ import annotations

import os
import shlex
import shutil
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import applog, gui_settings
from ..assistant_launcher import AssistantLaunchError, launch_assistant
from ..config import logs_dir
from ..discord_rpc import start_presence, stop_presence, update_presence
from ..integrity import format_size
from ..launcher import (
    DEFAULT_PROTON_PREFIX,
    DEFAULT_UMU_PREFIX,
    LaunchError,
    Mo2Executable,
    ProcessGroupRegistry,
    _terminate_process_group,
    available_commands,
    build_command,
    build_direct_command,
    default_launch_target,
    ensure_runner_prefix,
    find_extra_protons,
    launch_detached,
    parse_mo2_executables,
    resolve_runner,
    runner_graphics_error,
    runner_prefix_error,
    write_desktop_shortcut,
)
from ..log_dump import create_log_dump
from ..proton_installer import fetch_ge_proton_releases, install_proton
from .common import (
    ACCENT,
    WARN,
    BackgroundTask,
    crash_dump_names,
    exe_pids,
    format_playtime,
    gamma_installed,
    info_label,
    install_hover_grow_text,
    make_card,
    mo2_pids,
    mo2_running,
    normalize_path,
    notify_desktop,
    play_click_sound,
    section_label,
    set_hover_grow_text,
    tr,
    update_cache_label,
)
from .install_page import _resume_state_matches

_HIDDEN_LAUNCH_TARGETS = {"dx8", "dx8-avx"}

# X-Ray's crash handler can take tens of seconds to finish writing a
# .mdmp after the wrapper/MO2 process is already gone (confirmed against
# a real crash: 39s) - poll for it instead of checking once, immediately.
_CRASH_POLL_INTERVAL_MS = 2000
_CRASH_POLL_MAX_ATTEMPTS = 45  # 90s total window

#: If nothing (MO2/the game) has appeared this long after launching, warn
#: that the launch may be stuck (e.g. a Wine crash loop) - confirmed via
#: a real incident: a hung wrapper spun indefinitely with no way for the
#: user to know something was wrong short of a full system restart.
_STALL_WARNING_SECONDS = 180


def _is_hidden_launch_target(title: str) -> bool:
    return title.strip().casefold() in _HIDDEN_LAUNCH_TARGETS


class _ProtonVersionComboBox(QComboBox):
    """Keep the large Proton release popup within a usable screen height."""

    _MAX_POPUP_HEIGHT = 360

    def showPopup(self) -> None:
        super().showPopup()
        # Qt creates and sizes the popup after showPopup() starts. Apply the
        # limit on the next event-loop turn so Wayland cannot expand it again.
        QTimer.singleShot(0, self._limit_popup)

    def _limit_popup(self) -> None:
        popup = self.view().window()
        popup.setMaximumHeight(self._MAX_POPUP_HEIGHT)
        if popup.height() > self._MAX_POPUP_HEIGHT:
            popup.resize(popup.width(), self._MAX_POPUP_HEIGHT)


class _ProgressBridge(QObject):
    """Cross-thread signal bridge for download progress updates."""

    updated = Signal(int, str)


class PlayPage(QWidget):
    launch_state_changed = Signal(bool)

    def __init__(self, window) -> None:
        super().__init__()
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
        #: One-shot guard so the stall warning (see _on_launch_check) only
        #: ever fires once per launch attempt.
        self._stall_warned = False
        self._launch_timer = None
        self._monitoring_mo2 = False
        self._mo2_seen = False
        self._handoff_checks = 0
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
        #: Whether Flip Priority was used since the last completed
        #: session - if so, a new crash dump at session-end gets a
        #: dedicated warning instead of being silently indistinguishable
        #: from any other crash. See _check_flip_priority_crash().
        self._crash_check_pending = False
        self._pre_launch_crash_dumps: set[str] = set()
        #: X-Ray's own crash handler can take a while (tens of seconds,
        #: confirmed against a real user's crash: 39s) to actually finish
        #: writing the .mdmp file after the wrapper/MO2 process is
        #: already gone - checking once, immediately, at session-end
        #: misses it. These back a bounded poll instead of one shot; see
        #: _poll_for_flip_priority_crash().
        self._crash_poll_timer: QTimer | None = None
        self._crash_poll_anomaly = ""
        self._crash_poll_baseline: set[str] = set()
        self._crash_poll_attempts_left = 0
        self._registry = ProcessGroupRegistry()
        self._launch_status_clear_timer = QTimer(self)
        self._launch_status_clear_timer.setSingleShot(True)
        self._launch_status_clear_timer.timeout.connect(self._clear_launch_status)
        self._crash_report_task: BackgroundTask | None = None
        self._launch_started_at: float | None = None
        self._discord_rpc = None
        self._persisting = False
        #: Steam Proton labels, refreshed with the runner combo so the chip row
        #: does not re-scan Steam libraries on every keystroke.
        self._proton_labels: list[str] = []
        self._installed_protons: list[tuple[str, str]] = []
        #: Set only once a GE-Proton install actually starts (_install_proton());
        #: explicit here so cancel logic never has to guess whether one is running.
        self._cancel_event: threading.Event | None = None

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

        # -- config grid (launch game | select runner) ------------------------
        grid = QHBoxLayout()
        grid.setSpacing(16)

        target_card, target_layout = make_card()
        target_layout.setSpacing(12)
        target_layout.addWidget(section_label(tr("Launch target"), level=2))
        target_row = QHBoxLayout()
        self.target_combo = QComboBox()
        self.target_combo.setMinimumHeight(34)
        self.target_combo.currentIndexChanged.connect(self._on_change)
        target_row.addWidget(self.target_combo, 1)
        target_layout.addLayout(target_row)
        self.target_path = QLabel("")
        self.target_path.setObjectName("dim")
        self.target_path.setWordWrap(True)
        self.target_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        target_layout.addWidget(self.target_path)
        self.shortcut_button = QPushButton(tr("Add shortcut to desktop"))
        self.shortcut_button.setObjectName("secondary")
        self.shortcut_button.setMinimumHeight(30)
        self.shortcut_button.setToolTip(
            tr("Create a desktop shortcut that launches the selected target with the currently selected runner.")
        )
        self.shortcut_button.setVisible(os.name != "nt")
        self.shortcut_button.clicked.connect(self._add_desktop_shortcut)
        target_layout.addWidget(self.shortcut_button)
        runner_card, runner_layout = make_card()
        runner_layout.setSpacing(12)
        runner_layout.addWidget(section_label(tr("Runner"), level=2))
        runner_row = QHBoxLayout()
        runner_row.addWidget(QLabel(tr("Runner:")))
        self.runner_combo = QComboBox()
        self.runner_combo.currentIndexChanged.connect(self._on_runner_changed)
        runner_row.addWidget(self.runner_combo, 1)
        runner_layout.addLayout(runner_row)

        self.runner_hint = info_label("")
        self.runner_hint.setObjectName("dim")
        self.runner_hint.setWordWrap(True)
        runner_layout.addWidget(self.runner_hint)

        self.gamemode_check = QCheckBox(tr("Always use GameMode"))
        self.gamemode_check.setToolTip(
            tr("Wrap every launch in gamemoderun (enables the Feral GameMode CPU governor / scheduler optimisation), even for Proton.")
        )
        self.gamemode_check.toggled.connect(self._on_gamemode_toggled)
        runner_layout.addWidget(self.gamemode_check)

        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel(tr("Runner prefix:")))
        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText(
            "Leave blank to use the runner's default prefix"
        )
        self.prefix_edit.editingFinished.connect(self._on_change)
        prefix_row.addWidget(self.prefix_edit, 1)
        runner_layout.addLayout(prefix_row)

        proton_row = QHBoxLayout()
        proton_row.setSpacing(8)
        self.install_proton_button = QPushButton(tr("Install GE-Proton"))
        self.install_proton_button.setObjectName("secondary")
        self.install_proton_button.setMinimumHeight(52)
        self.install_proton_button.setMinimumWidth(180)
        self.install_proton_button.clicked.connect(self._install_proton)
        proton_row.addWidget(self.install_proton_button)
        self.proton_version_combo = _ProtonVersionComboBox()
        self.proton_version_combo.setMinimumHeight(30)
        self.proton_version_combo.setMinimumWidth(180)
        self.proton_version_combo.setMaxVisibleItems(12)
        self.proton_version_combo.view().setMaximumHeight(360)
        self.proton_version_combo.view().setUniformItemSizes(True)
        self.proton_version_combo.currentIndexChanged.connect(
            self._update_install_button
        )
        proton_row.addWidget(self.proton_version_combo)
        runner_layout.addLayout(proton_row)

        self.proton_progress = QProgressBar()
        self.proton_progress.setMinimumHeight(20)
        self.proton_progress.setMaximumHeight(20)
        self.proton_progress.setVisible(False)
        runner_layout.addWidget(self.proton_progress)

        self.proton_status = QLabel("")
        self.proton_status.setObjectName("dim")
        self.proton_status.setVisible(False)
        runner_layout.addWidget(self.proton_status)

        cancel_row = QHBoxLayout()
        cancel_row.addStretch(1)
        self.cancel_button = QPushButton(tr("Cancel"))
        self.cancel_button.setObjectName("danger")
        self.cancel_button.setMinimumHeight(40)
        self.cancel_button.setMinimumWidth(80)
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_proton_download)
        cancel_row.addWidget(self.cancel_button)
        runner_layout.addLayout(cancel_row)

        grid.addWidget(runner_card, 1)
        grid.addWidget(target_card, 1)

        root.addLayout(grid)

        # -- status chip row -------------------------------------------------
        self.chips_row = QHBoxLayout()
        self.chips_row.setSpacing(8)
        root.addLayout(self.chips_row)

        # -- launch actions ---------------------------------------------------
        self.launch_button = QPushButton(tr("Launch Game"))
        self.launch_button.setObjectName("hero")
        self.launch_button.setToolTip(
            tr("Launch the selected target through Mod Organizer 2 with the GAMMA modlist and virtual file system.")
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
        options_layout.addWidget(section_label(tr("Launch options"), level=2))
        self.custom_options_edit = QLineEdit()
        self.custom_options_edit.setPlaceholderText(
            "Optional launch options, e.g. gamemoderun mangohud"
        )
        self.custom_options_edit.setMinimumHeight(34)
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(300)
        self._debounce_timer.timeout.connect(self._on_change)
        self.custom_options_edit.textChanged.connect(
            lambda: self._debounce_timer.start()
        )
        options_layout.addWidget(self.custom_options_edit)
        root.addWidget(options_card)

        # -- folders card -----------------------------------------------------
        folders_card, folders_layout = make_card()
        folders_layout.setSpacing(8)
        folders_layout.addWidget(section_label(tr("Folders"), level=2))

        anomaly_row = QHBoxLayout()
        anomaly_row.addWidget(QLabel(tr("Anomaly folder:")))
        self.anomaly_edit = QLineEdit()
        self.anomaly_edit.setPlaceholderText("Enter or browse to a folder...")
        self.anomaly_edit.editingFinished.connect(self._persist_dirs)
        anomaly_row.addWidget(self.anomaly_edit, 1)
        self.anomaly_browse = QPushButton(tr("Browse..."))
        self.anomaly_browse.clicked.connect(self._browse_anomaly)
        anomaly_row.addWidget(self.anomaly_browse)
        folders_layout.addLayout(anomaly_row)

        gamma_row = QHBoxLayout()
        gamma_row.addWidget(QLabel(tr("GAMMA folder:")))
        self.gamma_edit = QLineEdit()
        self.gamma_edit.setPlaceholderText("Enter or browse to a folder...")
        self.gamma_edit.editingFinished.connect(self._persist_dirs)
        gamma_row.addWidget(self.gamma_edit, 1)
        self.gamma_browse = QPushButton(tr("Browse..."))
        self.gamma_browse.clicked.connect(self._browse_gamma)
        gamma_row.addWidget(self.gamma_browse)
        folders_layout.addLayout(gamma_row)

        cache_row = QHBoxLayout()
        cache_row.addWidget(QLabel(tr("Cache folder:")))
        self.cache_edit = QLineEdit()
        self.cache_edit.setPlaceholderText("Enter or browse to a folder...")
        self.cache_edit.editingFinished.connect(self._persist_dirs)
        cache_row.addWidget(self.cache_edit, 1)
        self.cache_browse = QPushButton(tr("Browse..."))
        self.cache_browse.clicked.connect(self._browse_cache)
        cache_row.addWidget(self.cache_browse)
        folders_layout.addLayout(cache_row)

        self.cache_info_label = info_label("")
        self.cache_info_label.setObjectName("dim")
        folders_layout.addWidget(self.cache_info_label)

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
        self._releases: list[dict] = []
        self._fetch_proton_releases()

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

    def _reload_runners(self) -> None:
        current = self.runner_combo.currentData()
        self.runner_combo.blockSignals(True)
        self.runner_combo.clear()
        self.runner_combo.addItem(tr("Auto-detect (latest GE-Proton)"), "auto")
        extra_protons = find_extra_protons()
        self._proton_labels = [label for label, _ in extra_protons]
        self._installed_protons = extra_protons
        if extra_protons:
            self.runner_combo.insertSeparator(self.runner_combo.count())
            for label, path in extra_protons:
                self.runner_combo.addItem(tr("{label} (Installed)", label=label), f"umup:{path}")
        saved = gui_settings.load_gui_settings().get("runner", "auto")
        chosen = current
        if not chosen or self.runner_combo.findData(chosen) < 0:
            chosen = saved
        # QComboBox.findData(None) matches the separator item above (its
        # itemData is also None), returning its index instead of -1 - so a
        # falsy `chosen` must be caught explicitly, or a saved runner of
        # None/"" would stick the selection on the separator instead of
        # falling back to "auto".
        if not chosen or self.runner_combo.findData(chosen) < 0:
            chosen = "auto"
        self.runner_combo.setCurrentIndex(self.runner_combo.findData(chosen))
        self.runner_combo.blockSignals(False)
        self.prefix_edit.setText(self._prefix_for(kind=self.runner_combo.currentData()))
        self._update_runner_hint(self.runner_combo.currentData())

    def _fetch_proton_releases(self) -> None:
        def _work() -> list[dict]:
            return fetch_ge_proton_releases(count=100)

        def _done(result: object) -> None:
            self._releases = result if isinstance(result, list) else []  # type: ignore[assignment]
            self.proton_version_combo.blockSignals(True)
            self.proton_version_combo.clear()
            for rel in self._releases:
                self.proton_version_combo.addItem(rel["tag"], rel["tag"])
            self.proton_version_combo.blockSignals(False)
            self._update_install_button()

        task = BackgroundTask(_work, parent=self)
        task.result.connect(_done)
        task.error.connect(lambda _: self._update_install_button())
        task.start()

    def _update_install_button(self) -> None:
        installed = {label for label, _ in self._installed_protons}
        selected = self.proton_version_combo.currentData() or ""
        if not selected and self._releases:
            selected = self._releases[0]["tag"]
        if selected and any(selected in label for label in installed):
            self.install_proton_button.setText(tr("GE-Proton installed ✓"))
            self.install_proton_button.setEnabled(False)
        elif selected:
            self.install_proton_button.setText(tr("Install {selected}", selected=selected))
            self.install_proton_button.setEnabled(True)
        else:
            self.install_proton_button.setText(tr("Install GE-Proton"))
            self.install_proton_button.setEnabled(False)
        self.install_proton_button.update()

    def _install_proton(self) -> None:
        if self._install_busy or self._launching:
            # An install/launch elsewhere already holds the busy flag; a
            # Proton install finishing here would clear it out from under
            # that operation via the unconditional set_install_busy(False)
            # in _done/_fail below.
            return
        version = self.proton_version_combo.currentData()
        if not version:
            return
        installed = {label for label, _ in self._installed_protons}
        if any(version in label for label in installed):
            return
        overrides = gui_settings.load_gui_settings().get("tool_overrides") or {}
        steam_root = overrides.get("steam_root", "")
        install_dir = (
            Path(steam_root)
            if steam_root
            else Path.home() / ".local" / "share" / "Steam"
        )
        install_dir = install_dir / "compatibilitytools.d"
        self.install_proton_button.setEnabled(False)
        self.install_proton_button.setText(tr("Installing…"))
        self.proton_progress.setValue(0)
        self.proton_progress.setVisible(True)
        self.proton_status.setText(tr("Preparing download…"))
        self.proton_status.setVisible(True)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self._cancel_event = threading.Event()
        self.window.set_install_busy(True)
        self.launch_button.setEnabled(False)
        self.launch_state_changed.emit(True)

        # A retry after a failed/cancelled install would otherwise leave the
        # previous bridge alive as an idle child of PlayPage forever - it's
        # done with once this new one takes over.
        old_bridge = getattr(self, "_proton_bridge", None)
        if old_bridge is not None:
            old_bridge.deleteLater()
        bridge = _ProgressBridge(parent=self)
        self._proton_bridge = bridge
        # Bound method, not a lambda: install_proton()'s progress_cb runs on
        # the BackgroundTask's worker thread, and PySide only auto-queues a
        # cross-thread signal onto the main thread for a QObject-bound slot
        # (as every other worker-thread signal in this codebase already
        # does, see common.py's CommandRunner/BackgroundTask) - a lambda has
        # no thread affinity of its own, so it would run direct, on the
        # worker thread, touching these QWidgets unsafely.
        bridge.updated.connect(self._on_proton_progress)

        def _progress(downloaded: int, total: int) -> None:
            if total > 0:
                pct = int(downloaded * 100 / total)
                text = f"Downloading {version}… {format_size(downloaded)}/{format_size(total)}"
                bridge.updated.emit(pct, text)

        def _work() -> Path:
            return install_proton(
                version,
                install_dir,
                progress_cb=_progress,
                cancel_event=self._cancel_event,
            )

        def _done(result: object) -> None:
            self.window.set_install_busy(False)
            self.cancel_button.setVisible(False)
            self.cancel_button.setText(tr("Cancel"))
            self.proton_progress.setValue(100)
            self.proton_status.setText(tr("Installed {version} ✓", version=version))
            self._reload_runners()
            self._update_install_button()
            self._refresh_preview()
            self.launch_state_changed.emit(False)
            QTimer.singleShot(3000, self._hide_proton_progress)

        def _fail(err: str) -> None:
            self.window.set_install_busy(False)
            self.cancel_button.setVisible(False)
            self.cancel_button.setText(tr("Cancel"))
            if err == "Download cancelled":
                self.proton_status.setText(tr("Download cancelled"))
                self.install_proton_button.setText(tr("Install {version}", version=version))
                self.install_proton_button.setEnabled(True)
            else:
                self.proton_status.setText(tr("Error: {err}", err=err))
                self.install_proton_button.setText(tr("Retry install"))
                self.install_proton_button.setEnabled(True)
            self._refresh_preview()
            self.launch_state_changed.emit(False)
            QTimer.singleShot(5000, self._hide_proton_progress)

        task = BackgroundTask(_work, parent=self)
        task.result.connect(_done)
        task.error.connect(_fail)
        task.start()

    def _on_proton_progress(self, percent: int, text: str) -> None:
        self.proton_progress.setValue(percent)
        self.proton_status.setText(text)

    def _cancel_proton_download(self) -> None:
        if self._cancel_event:
            self._cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText(tr("Cancelling…"))

    def _hide_proton_progress(self) -> None:
        self.proton_progress.setVisible(False)
        self.proton_status.setVisible(False)

    def _default_prefix(self, kind: str) -> str:
        if kind.startswith("proton:"):
            return str(DEFAULT_PROTON_PREFIX)
        return str(DEFAULT_UMU_PREFIX)

    def _prefix_for(self, *, kind: str | None) -> str:
        if not kind:
            kind = "auto"
        state = gui_settings.load_gui_settings()
        prefixes = state.get("prefixes") or {}
        saved = prefixes.get(kind) or ""
        # Older builds could carry the UMU default into a manually selected
        # Steam Proton runner. Do not reuse that prefix across runner types.
        if kind.startswith("proton:") and Path(saved).name == "umu-default":
            saved = ""
        return saved or self._default_prefix(kind)

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
        preferred = gui_settings.load_gui_settings().get("target") or ""
        default = preferred if preferred in titles else default_launch_target(titles)
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
        self._update_cache_info(profile.cache)

    def _browse_anomaly(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select Anomaly install folder", str(Path.home())
        )
        if folder:
            self.anomaly_edit.setText(folder)
            self._persist_dirs()

    def _browse_gamma(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select GAMMA install folder", str(Path.home())
        )
        if folder:
            self.gamma_edit.setText(folder)
            self._persist_dirs()

    def _browse_cache(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select cache folder", str(Path.home())
        )
        if folder:
            self.cache_edit.setText(folder)
            self._persist_dirs()
            self._update_cache_info(self.cache_edit.text().strip())

    def _update_cache_info(self, cache_path: str) -> None:
        update_cache_label(self.cache_info_label, cache_path)

    def _persist_dirs(self) -> None:
        if self._persisting:
            return
        self._persisting = True
        try:
            if self.window.install_busy or mo2_running():
                QMessageBox.warning(
                    self,
                    tr("Busy"),
                    tr("An install is running or the game is currently running. Folder changes cannot be saved right now."),
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
            profile.anomaly = anomaly
            profile.gamma = gamma
            profile.cache = cache
            try:
                self.window.settings.save()
            except OSError as exc:
                QMessageBox.warning(
                    self, tr("Save Failed"), tr("Could not write settings.json:\n{exc}", exc=exc)
                )
                return
            self.window.statusBar().showMessage(
                f"Folders updated: {anomaly} | {gamma} | {cache}", 6000
            )
            self._reload_targets()
            self._refresh_preview()
            self._update_cache_info(cache)
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
        prefix = os.path.expanduser(self.prefix_edit.text().strip())
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
            tokens = [t for t in tokens if t != "%command%"]
            env_var_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
            prefix: list[str] = []
            for token in tokens:
                if env_var_re.match(token):
                    key, _, value = token.partition("=")
                    env[key] = value
                else:
                    prefix.append(token)
            command = [*prefix, *command]
        return command, env, cwd

    def _refresh_preview(self) -> None:
        """Refresh the launch preview, surfacing unexpected errors.

        Any exception here previously left the launch buttons silently dead;
        it is now reported in the preview pane instead.
        """
        try:
            self._refresh_preview_inner()
        except Exception as exc:  # noqa: BLE001 - never leave buttons silently dead
            self.preview_label.setPlainText(f"Launch check failed: {exc}")
            self.preview_label.setStyleSheet(f"color: {WARN.name()};")
            if not self._launching:
                self.launch_button.setEnabled(False)
            self.open_mo2_button.setEnabled(False)
            self.direct_button.setEnabled(False)

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
            self.preview_label.setPlainText("No launch target available")
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
            tip = (
                "Install GAMMA first - Mod Organizer launches require a "
                "GAMMA installation."
            )
            self.launch_button.setToolTip(tip)
            self.open_mo2_button.setToolTip(tip)
        else:
            self.launch_button.setToolTip("")
            self.open_mo2_button.setToolTip("")
        self.direct_button.setToolTip(
            "" if self.direct_button.isEnabled() else "No launch target available"
        )
        if incomplete:
            tip = tr(
                "The last GAMMA install attempt failed - resume it on the Install page before playing."
            )
            self.launch_button.setToolTip(tip)
            self.open_mo2_button.setToolTip(tip)
            self.direct_button.setToolTip(tip)
        self._build_chips(ok=True, runner=runner)
        target = self._selected_target()
        exe = next((e for e in self.executables if e.title == target), Mo2Executable())
        self.target_path.setText(exe.binary or "No executable set for this target")

    def _build_chips(self, *, ok: bool, runner=None) -> None:
        while self.chips_row.count():
            item = self.chips_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
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
        prefix = self.prefix_edit.text().strip()
        prefixes = dict(gui_settings.load_gui_settings().get("prefixes") or {})
        prefixes[runner] = prefix
        gui_settings.save_gui_settings(
            runner=runner,
            wine_prefix=prefix,
            prefixes=prefixes,
            target=self.target_combo.currentText(),
            custom_launch_options=self.custom_options_edit.text().strip(),
        )

    def _on_runner_changed(self, *_args) -> None:
        kind = self.runner_combo.currentData()
        self.prefix_edit.setText(self._prefix_for(kind=kind))
        self._save_state()
        self._refresh_preview()
        self._update_runner_hint(kind)

    def _update_runner_hint(self, kind: str | None) -> None:
        if not kind or kind == "auto":
            self.runner_hint.setText(
                tr("Select a Proton-GE Runner or install a version from below.")
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
            tr("Quit Game"),
            tr("Are you sure you want to quit the game?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._abort_launch(tr("Game closed by user."))

    def _prompt_possible_stall(self) -> None:
        """Warn once per launch if nothing has appeared for a while.

        Confirmed via a real incident: a hung wrapper (a Wine crash loop,
        in that case) can run indefinitely with no MO2/game window ever
        appearing, silently consuming system memory the whole time - the
        user had no way to know something was wrong short of a full
        system restart. See _STALL_WARNING_SECONDS.
        """
        reply = QMessageBox.question(
            self,
            tr("Launch May Be Stuck"),
            tr(
                "The game hasn't started after a few minutes - this can happen during a crash loop, which may consume system memory the longer it runs.\n\nQuit the game now?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._abort_launch(tr("Game closed by user."))

    def launch_game(self) -> None:
        """Launch the selected game target using the primary Play workflow."""
        # Claim the launch before resolving a runner. Resolution probes the
        # filesystem and can take long enough for a second click to arrive.
        if self._launching or self._install_busy:
            return
        if not self._selected_target():
            QMessageBox.warning(
                self,
                tr("No target selected"),
                tr("Choose which game to run from the Target list before clicking Launch Game.\n\nIf the list is empty, make sure your active profile points to a GAMMA install and its ModOrganizer.ini is configured."),
            )
            return
        self._set_launch_button_state(True)
        # Try MO2 first; fall back to direct launch if MO2 is unavailable.
        try:
            runner = self._runner()
            self._resolve_command(open_mo2=False, direct=False, runner=runner)
            self._run(open_mo2=False, direct=False, runner=runner)
        except LaunchError as exc:
            self._set_launch_button_state(False)
            answer = QMessageBox.question(
                self,
                tr("MO2 launch unavailable"),
                tr("Mod Organizer could not be prepared for this launch.\n\nReason: {exc}\n\nLaunch Anomaly directly instead? Mods managed by MO2 will not be active in a direct launch.", exc=exc),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._launch_direct()
        except Exception as exc:  # noqa: BLE001 - recover the launch controls
            self._set_launch_button_state(False)
            self._set_result(f"Could not prepare launch: {exc}", error=True)
            QMessageBox.warning(self, tr("Could not launch"), str(exc))

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

    def _run(self, *, open_mo2: bool, direct: bool, runner=None) -> None:
        """Launch the game through Mod Organizer 2 or directly."""
        self._launch_status_clear_timer.stop()
        # A crash-dump poll from the previous session must not fire a
        # warning attributed to this brand-new one (e.g. a quick retry
        # launched while the old poll's 90s window was still open).
        self._cancel_crash_poll()
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
            # Only bother listing crash dumps at all when Flip Priority
            # was actually used since the last completed session - zero
            # filesystem work on every ordinary launch otherwise. See
            # _check_flip_priority_crash().
            crash_check_pending = False
            pre_launch_crash_dumps: set[str] = set()
            if not open_mo2:
                launch_profile = self.window.settings.active_profile
                if launch_profile is not None:
                    pending_map = gui_settings.load_gui_settings().get(
                        "flip_priority_pending", {}
                    )
                    crash_check_pending = bool(
                        pending_map.get(launch_profile.profile_name)
                    )
                    if crash_check_pending:
                        pre_launch_crash_dumps = crash_dump_names(
                            launch_profile.anomaly
                        )
            self._proc = launch_detached(
                command, env, cwd, log_path=log_path, registry=self._registry
            )
            self._launch_wrapper_pid = self._proc.pid
            self._stall_warned = False
            # Not "Open MO2" - that opens the mod manager, not the game.
            is_game_session = label != "Mod Organizer 2"
            self._launch_started_at = time.monotonic() if is_game_session else None
            if is_game_session:
                gui_state = gui_settings.load_gui_settings()
                if gui_state.get("discord_rpc_enabled") and gui_state.get(
                    "discord_client_id"
                ):
                    self._discord_rpc = start_presence(gui_state["discord_client_id"])
                    update_presence(self._discord_rpc, "Playing S.T.A.L.K.E.R. GAMMA")
            self._monitoring_mo2 = monitoring_mo2
            self._mo2_seen = False
            self._handoff_checks = 0
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
        except LaunchError as exc:
            self._abort_launch(f"Could not launch: {exc}")
            QMessageBox.warning(self, tr("Could not launch"), str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._abort_launch(f"Unexpected launch error: {exc}")
            QMessageBox.warning(self, tr("Could not launch"), str(exc))
            return
        # Use a repeating timer to detect fast failures without delaying recovery,
        # so buttons are re-enabled as soon as possible regardless of timing.
        self._launch_timer = QTimer(self)
        self._launch_timer.setInterval(250)
        self._launch_timer.timeout.connect(
            lambda: self._on_launch_check(label, command, log_path)
        )
        self._launch_timer.start()
        self._set_result(f"Launching {label}...")

    def _stop_launch_timer(self) -> None:
        timer = getattr(self, "_launch_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._launch_timer = None

    def _abort_launch(self, message: str) -> None:
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
        self._launch_wrapper_pid = None
        self._monitoring_mo2 = False
        self._stop_launch_timer()
        self._set_result(message, error=True)
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

    def _check_flip_priority_crash(self) -> None:
        """Warn if a crash dump appears after a recent Flip Priority.

        A new X-Ray minidump (.mdmp) in the Anomaly install's log folder
        reliably indicates a native engine crash, regardless of whether
        the session went through MO2 or a direct launch - checking for
        one this way sidesteps needing this app's own (unreliable,
        MO2-mediated) process-exit-code tracking. Only checked when Flip
        Priority was used since the last completed session, to avoid
        false-positive noise on every ordinary crash; the pending flag
        is consumed (cleared) here regardless of outcome, so only the
        first launch after a flip gets attributed to it. The actual
        dump-file check is a bounded poll (_poll_for_flip_priority_crash),
        not a single immediate check, since X-Ray can take a while to
        finish writing it after the session is already reported closed.
        """
        if not self._crash_check_pending:
            return
        self._crash_check_pending = False
        profile = self.window.settings.active_profile
        if profile is None:
            return
        pending_map = dict(
            gui_settings.load_gui_settings().get("flip_priority_pending", {})
        )
        pending_map.pop(profile.profile_name, None)
        gui_settings.save_gui_settings(flip_priority_pending=pending_map)
        self._cancel_crash_poll()
        self._crash_poll_anomaly = profile.anomaly
        self._crash_poll_baseline = self._pre_launch_crash_dumps
        self._crash_poll_attempts_left = _CRASH_POLL_MAX_ATTEMPTS
        self._poll_for_flip_priority_crash()

    def _poll_for_flip_priority_crash(self) -> None:
        new_dumps = (
            crash_dump_names(self._crash_poll_anomaly) - self._crash_poll_baseline
        )
        if new_dumps:
            self._crash_poll_timer = None
            message = tr(
                "The game appears to have crashed (a new crash log was found), and you recently used Flip Priority on the Mod Manager page.\n\nAn incompatible mod load order can cause crashes like this - if it keeps happening, consider flipping the priority order back."
            )
            QMessageBox.warning(self, tr("Possible Crash After Flip Priority"), message)
            is_active = getattr(self.window, "isActiveWindow", lambda: True)()
            if not is_active:
                notify_desktop(tr("Possible Crash After Flip Priority"), message)
            return
        self._crash_poll_attempts_left -= 1
        if self._crash_poll_attempts_left <= 0:
            self._crash_poll_timer = None
            return
        self._crash_poll_timer = QTimer(self)
        self._crash_poll_timer.setSingleShot(True)
        self._crash_poll_timer.timeout.connect(self._poll_for_flip_priority_crash)
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

    def _stop_discord_presence(self) -> None:
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

    def _on_launch_check(self, label: str, command: list[str], log_path: Path) -> None:
        """Check the wrapper and, for MO2, the handoff process."""
        if (
            not self._stall_warned
            and self._launch_started_at is not None
            and time.monotonic() - self._launch_started_at >= _STALL_WARNING_SECONDS
        ):
            self._stall_warned = True
            self._prompt_possible_stall()
            return
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
                        self._set_result("MO2 is running...")
                        applog.get_logger().info(
                            "play: mo2 handoff detected pids=%s", new_pids
                        )
                        return
                    if self._handoff_checks < 10:
                        self._handoff_checks += 1
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
                    self._check_flip_priority_crash()
                    self._finish_launch(f"{label} closed normally.")
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
                if not (mo2_pids() & self._mo2_launch_pids):
                    applog.get_logger().info(
                        "play: mo2 exited, finishing launch (game_seen=%s)",
                        self._game_seen,
                    )
                    self._record_playtime()
                    self._check_flip_priority_crash()
                    self._finish_launch(f"{label} closed normally.")
                    return
                self._set_result("MO2 is running...")
                return
            self._stop_launch_timer()
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
                self._set_result("Launcher exited; waiting for MO2...")
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
                self._check_flip_priority_crash()
                self._set_result(f"{label} closed normally.")
            else:
                self._check_flip_priority_crash()
                detail = self._log_tail(log_path)
                msg = f"{label} exited with an error (code {code})"

                msg += f"\n\nCommand: {shlex.join(command)}"
                if detail:
                    msg += f"\n\nLast log lines:\n{detail}"
                self._set_result(msg, error=True)
                if runner_graphics_error(detail):
                    graphics_message = (
                        "The game started through WineD3D instead of DXVK/Vulkan.\n\n"
                        "Install the correct Vulkan driver for the graphics card, "
                        "then refresh System Check. Also remove "
                        "PROTON_USE_WINED3D=1 from Custom Launch Options if present."
                    )
                    QMessageBox.warning(self, tr("DXVK/Vulkan Problem"), graphics_message)
                elif runner_prefix_error(detail):
                    runner = self._runner()
                    compatibility_message = (
                        "The selected Wine/Proton runner could not use the configured "
                        "prefix correctly. This usually means the prefix was created "
                        "or is currently being used by a different runner version.\n\n"
                        f"Selected runner:\n{runner.label}\n\n"
                        f"Prefix:\n{self.prefix_edit.text().strip()}\n\n"
                        "Open the Play page and select the runner that created this "
                        "prefix, or configure a separate prefix for the selected "
                        "runner. Do not switch runners while the prefix is in use."
                    )
                    if "concrt140.dll" in detail.lower():
                        compatibility_message += (
                            "\n\nMO2 requires concrt140.dll (Microsoft Concurrency "
                            "Runtime) which some Proton/Wine versions do not "
                            "implement. Switch to GE-Proton on the Play page and "
                            "try again."
                        )
                    if "qtpdf.dll" in detail.lower():
                        compatibility_message += (
                            "\n\nQt6Pdf.dll was not found — this is a cosmetic "
                            "warning from MO2's imageformats plugin and is not "
                            "the cause of the crash."
                        )
                    QMessageBox.warning(
                        self,
                        tr("Runner/Prefix Compatibility Problem"),
                        compatibility_message,
                    )
                else:
                    QMessageBox.warning(self, tr("Launch failed"), msg)
                self._offer_crash_report()
            self._launch_status_clear_timer.start(3000)

    def _offer_crash_report(self) -> None:
        """Offer to bundle logs into a report right after a failed launch.

        Turns "the game crashed" straight into a ready-to-share bug report
        with one extra click, reusing the same log-dump/ASSISTANT pieces
        Utilities' "Create Log Dump" button already uses - nothing new to
        maintain, just a second entry point at the moment it's most useful.
        """
        answer = QMessageBox.question(
            self,
            tr("Create Bug Report?"),
            tr(
                "Create a log dump for this failure? It bundles logs and system info you can attach when reporting a bug."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
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
        dialog = QMessageBox(self)
        dialog.setWindowTitle(tr("Log Dump Created"))
        dialog.setText(tr("Saved to:\n{path}", path=str(path)))
        open_button = dialog.addButton(
            tr("Open in ASSISTANT"), QMessageBox.ButtonRole.AcceptRole
        )
        dialog.addButton(QMessageBox.StandardButton.Close)
        dialog.exec()
        if dialog.clickedButton() is open_button:
            try:
                launch_assistant(path)
            except AssistantLaunchError as exc:
                QMessageBox.information(self, tr("ASSISTANT Unavailable"), str(exc))

    def _on_crash_report_error(self, message: str) -> None:
        self._crash_report_task = None
        QMessageBox.warning(self, tr("Log Dump Failed"), message)

    def _log_tail(self, path: Path, limit: int = 12) -> str:
        try:
            lines = (
                path.read_text(encoding="utf-8", errors="replace").rstrip().splitlines()
            )
        except OSError:
            return ""
        return "\n".join(lines[-limit:])

    def _set_result(self, text: str, *, error: bool = False) -> None:
        color = WARN.name() if error else ACCENT.name()
        self._launch_status_clear_timer.stop()
        self.launch_live_status.setStyleSheet(f"color: {color};")
        self.launch_live_status.setText(text)

    def _clear_launch_status(self) -> None:
        self.launch_live_status.clear()

    def _add_desktop_shortcut(self) -> None:
        """Write a .desktop shortcut for the current target + runner."""
        if self._launching or self._install_busy:
            return
        target = self._selected_target()
        if not target:
            QMessageBox.warning(
                self,
                tr("No target selected"),
                tr("Choose which game to run from the Target list before adding a desktop shortcut.\n\nIf the list is empty, make sure your active profile points to a GAMMA install and its ModOrganizer.ini is configured."),
            )
            return
        try:
            runner = self._runner()
            command, env, cwd = self._resolve_command(
                open_mo2=False, direct=False, runner=runner
            )
            icon = Path(__file__).resolve().parent.parent.parent / "cli" / "stalker-gamma.png"
            path = write_desktop_shortcut(
                target, command, env, cwd, icon=str(icon) if icon.is_file() else None
            )
        except (LaunchError, OSError) as exc:
            QMessageBox.warning(self, tr("Shortcut failed"), str(exc))
            return
        self._set_result(f"Shortcut saved: {path}")
        QMessageBox.information(
            self, tr("Shortcut created"), tr("Desktop shortcut created:\n{path}", path=path)
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
            self.launch_button, tr("Quit Game") if launching else tr("Launch Game")
        )
        self.launch_button.setEnabled(True)
        self.open_mo2_button.setEnabled(not launching)
        self.direct_button.setEnabled(not launching)
        self.shortcut_button.setEnabled(not launching)
        self.launch_state_changed.emit(launching)

    def on_busy_changed(self, busy: bool) -> None:
        """Prevent launch actions from racing an install or reset operation."""
        self._install_busy = busy
        if busy:
            self.launch_button.setEnabled(False)
            self.open_mo2_button.setEnabled(False)
            self.direct_button.setEnabled(False)
            self.shortcut_button.setEnabled(False)
            self.install_proton_button.setEnabled(False)
            self.proton_version_combo.setEnabled(False)
        else:
            self._refresh_preview()
            self._update_install_button()
            self.proton_version_combo.setEnabled(True)
