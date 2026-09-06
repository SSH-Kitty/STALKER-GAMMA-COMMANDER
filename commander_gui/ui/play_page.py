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
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
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

from .. import gui_settings
from ..config import logs_dir
from ..integrity import format_size
from ..launcher import (
    DEFAULT_PROTON_PREFIX,
    DEFAULT_UMU_PREFIX,
    LaunchError,
    Mo2Executable,
    ProcessGroupRegistry,
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
from ..proton_installer import fetch_ge_proton_releases, install_proton
from .common import (
    ACCENT,
    WARN,
    BackgroundTask,
    gamma_installed,
    info_label,
    make_card,
    mo2_pids,
    mo2_running,
    normalize_path,
    section_label,
    tr,
    update_cache_label,
)

_HIDDEN_LAUNCH_TARGETS = {"dx8", "dx8-avx"}


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
        self._registry = ProcessGroupRegistry()
        self._launch_status_clear_timer = QTimer(self)
        self._launch_status_clear_timer.setSingleShot(True)
        self._launch_status_clear_timer.timeout.connect(self._clear_launch_status)
        self._persisting = False
        #: Steam Proton labels, refreshed with the runner combo so the chip row
        #: does not re-scan Steam libraries on every keystroke.
        self._proton_labels: list[str] = []
        self._installed_protons: list[tuple[str, str]] = []

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

        # -- hero header ---------------------------------------------------
        hero = section_label(tr("PLAY STALKER GAMMA"), level=1)
        hero.setWordWrap(True)
        hero.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(hero)
        subtitle = info_label(
            tr("Launch GAMMA with Mod Organizer 2 (MO2), manage your mods in MO2, or run STALKER Anomaly directly. Choose a target and runner, then launch.")
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(subtitle)

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
        grid.addWidget(target_card, 1)

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
        self.launch_button.clicked.connect(self.launch_game)
        root.addWidget(self.launch_button)

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

        # -- command preview --------------------------------------------------
        preview_card, preview_layout = make_card()
        preview_row = QHBoxLayout()
        self.preview_label = QPlainTextEdit()
        self.preview_label.setReadOnly(True)
        self.preview_label.setMaximumHeight(60)
        self.preview_label.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.preview_label.setObjectName("mono")
        preview_row.addWidget(self.preview_label, 1)
        self.copy_button = QPushButton(tr("Copy launch command"))
        self.copy_button.clicked.connect(self._copy_command)
        preview_row.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignTop)
        preview_layout.addLayout(preview_row)
        root.addWidget(preview_card)

        root.addStretch(1)

        self._reload_runners()
        self._reload_targets()
        self._load_state()
        self._refresh_preview()
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

        bridge = _ProgressBridge(parent=self)
        bridge.updated.connect(
            lambda p, t: (
                self.proton_progress.setValue(p),
                self.proton_status.setText(t),
            )
        )

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
            self.launch_button.setEnabled(False)
            self.open_mo2_button.setEnabled(False)
            self.direct_button.setEnabled(False)

    def _refresh_preview_inner(self) -> None:
        # Resolve the runner once: each resolution probes the filesystem for
        # Steam libraries and Proton builds, and this runs on every edit.
        profile = self.window.settings.active_profile
        try:
            runner = self._runner()
        except LaunchError as exc:
            self.preview_label.setPlainText(str(exc))
            self.preview_label.setStyleSheet(f"color: {WARN.name()};")
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
        base_ok = not self._launching and not self._install_busy
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
        # Installed GE-Proton versions
        proton_versions = []
        for label in self._proton_labels:
            ver = label.removeprefix("GE-Proton").removeprefix("Proton ").strip()
            if ver:
                proton_versions.append(ver)
        proton_text = (
            f"Proton GE: {', '.join(proton_versions)}"
            if proton_versions
            else "Proton GE: none"
        )
        chips.append((proton_text, bool(proton_versions)))
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

    def _copy_command(self) -> None:
        text = self.preview_label.toPlainText().strip()
        if text:
            QGuiApplication.clipboard().setText(text)

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
            self._proc = launch_detached(
                command, env, cwd, log_path=log_path, registry=self._registry
            )
            self._monitoring_mo2 = not direct and os.name != "nt"
            self._mo2_seen = False
            self._handoff_checks = 0
            # Snapshot pre-existing MO2 processes so handoff detection can
            # tell this launch's own instance apart from one the user
            # already had open (see mo2_pids() docstring).
            self._pre_launch_mo2_pids = mo2_pids() if self._monitoring_mo2 else set()
            self._mo2_launch_pids = set()
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
        self._monitoring_mo2 = False
        self._stop_launch_timer()
        self._set_result(message, error=True)
        self._set_launch_button_state(False)
        # Recompute real availability instead of blindly re-enabling: the
        # install this launched target depended on may have been broken by
        # another page while the game was running.
        self._refresh_preview()

    def _finish_launch(self, message: str, *, error: bool = False) -> None:
        """Release the launch lock, stop monitoring, and show a final status."""
        self._monitoring_mo2 = False
        self._stop_launch_timer()
        self._set_launch_button_state(False)
        self._refresh_preview()
        self._set_result(message, error=error)
        self._launch_status_clear_timer.start(3000)

    def _on_launch_check(self, label: str, command: list[str], log_path: Path) -> None:
        """Check the wrapper and, for MO2, the handoff process."""
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
                        return
                    if self._handoff_checks < 10:
                        self._handoff_checks += 1
                        return
                    self._finish_launch(
                        f"{label} launcher exited before MO2 was detected.",
                        error=True,
                    )
                    return
                if not (mo2_pids() & self._mo2_launch_pids):
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
            if proc_code == 0 and self._monitoring_mo2:
                self._set_result("Launcher exited; waiting for MO2...")
                return
            self._monitoring_mo2 = False
            self._stop_launch_timer()
            self._set_launch_button_state(False)
            self._refresh_preview()
            code = proc_code
            if code == 0:
                self._set_result(f"{label} closed normally.")
            else:
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
            self._launch_status_clear_timer.start(3000)

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
        self.launch_button.setEnabled(not launching)
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
