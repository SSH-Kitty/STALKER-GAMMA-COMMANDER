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
)
from ..proton_installer import fetch_ge_proton_releases, install_proton
from .common import (
    ACCENT,
    OK_GREEN,
    STATUS_RED,
    WARN,
    BackgroundTask,
    info_label,
    make_card,
    section_label,
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
        self._proc = None
        self._launch_timer = None
        self._launch_status_clear_timer = QTimer(self)
        self._launch_status_clear_timer.setSingleShot(True)
        self._launch_status_clear_timer.timeout.connect(self._clear_launch_status)
        self._persisting = False
        #: Steam Proton labels, refreshed with the runner combo so the chip row
        #: does not re-scan Steam libraries on every keystroke.
        self._proton_labels: list[str] = []

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
        hero = section_label("PLAY STALKER GAMMA", level=1)
        hero.setWordWrap(True)
        hero.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(hero)
        subtitle = info_label(
            "Launch GAMMA with Mod Organizer 2 (MO2), manage your mods in MO2, "
            "or run STALKER Anomaly directly. Choose a target and runner, "
            "then launch."
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(subtitle)

        # -- config grid (launch game | select runner) ------------------------
        grid = QHBoxLayout()
        grid.setSpacing(16)

        target_card, target_layout = make_card()
        target_layout.setSpacing(12)
        target_layout.addWidget(section_label("Launch target", level=2))
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
        grid.addWidget(target_card, 1)

        runner_card, runner_layout = make_card()
        runner_layout.setSpacing(12)
        runner_layout.addWidget(section_label("Runner", level=2))
        runner_row = QHBoxLayout()
        runner_row.addWidget(QLabel("Runner:"))
        self.runner_combo = QComboBox()
        self.runner_combo.currentIndexChanged.connect(self._on_runner_changed)
        runner_row.addWidget(self.runner_combo, 1)
        runner_layout.addLayout(runner_row)

        self.runner_hint = info_label("")
        self.runner_hint.setObjectName("dim")
        self.runner_hint.setWordWrap(True)
        runner_layout.addWidget(self.runner_hint)

        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel("Runner prefix:"))
        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText(
            "Leave blank to use the runner's default prefix"
        )
        self.prefix_edit.editingFinished.connect(self._on_change)
        prefix_row.addWidget(self.prefix_edit, 1)
        runner_layout.addLayout(prefix_row)

        proton_row = QHBoxLayout()
        proton_row.setSpacing(8)
        self.install_proton_button = QPushButton("Install GE-Proton")
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
        self.cancel_button = QPushButton("Cancel")
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
        self.launch_button = QPushButton("Launch Game")
        self.launch_button.setObjectName("hero")
        self.launch_button.setToolTip(
            "Launch the selected target through Mod Organizer 2 with the GAMMA modlist and virtual file system."
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
        self.open_mo2_button = QPushButton("Open MO2")
        self.open_mo2_button.setObjectName("secondary")
        self.open_mo2_button.setToolTip(
            "Open Mod Organizer 2 to manage the selected MO2 profile and run executables."
        )
        self.open_mo2_button.clicked.connect(self._open_mo2)
        secondary_row.addWidget(self.open_mo2_button, 1)
        self.direct_button = QPushButton("Launch Anomaly")
        self.direct_button.setObjectName("secondary")
        self.direct_button.setToolTip(
            "Run the selected Anomaly executable without MO2 or its virtual mod list."
        )
        self.direct_button.clicked.connect(self._launch_direct)
        secondary_row.addWidget(self.direct_button, 1)
        root.addLayout(secondary_row)

        # -- custom launch options -------------------------------------------
        options_card, options_layout = make_card()
        options_layout.addWidget(section_label("Launch options", level=2))
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
        folders_layout.addWidget(section_label("Folders", level=2))

        anomaly_row = QHBoxLayout()
        anomaly_row.addWidget(QLabel("Anomaly folder:"))
        self.anomaly_edit = QLineEdit()
        self.anomaly_edit.setPlaceholderText("Enter or browse to a folder...")
        self.anomaly_edit.editingFinished.connect(self._persist_dirs)
        anomaly_row.addWidget(self.anomaly_edit, 1)
        self.anomaly_browse = QPushButton("Browse...")
        self.anomaly_browse.clicked.connect(self._browse_anomaly)
        anomaly_row.addWidget(self.anomaly_browse)
        folders_layout.addLayout(anomaly_row)

        gamma_row = QHBoxLayout()
        gamma_row.addWidget(QLabel("GAMMA folder:"))
        self.gamma_edit = QLineEdit()
        self.gamma_edit.setPlaceholderText("Enter or browse to a folder...")
        self.gamma_edit.editingFinished.connect(self._persist_dirs)
        gamma_row.addWidget(self.gamma_edit, 1)
        self.gamma_browse = QPushButton("Browse...")
        self.gamma_browse.clicked.connect(self._browse_gamma)
        gamma_row.addWidget(self.gamma_browse)
        folders_layout.addLayout(gamma_row)

        cache_row = QHBoxLayout()
        cache_row.addWidget(QLabel("Cache folder:"))
        self.cache_edit = QLineEdit()
        self.cache_edit.setPlaceholderText("Enter or browse to a folder...")
        self.cache_edit.editingFinished.connect(self._persist_dirs)
        cache_row.addWidget(self.cache_edit, 1)
        self.cache_browse = QPushButton("Browse...")
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
        self.copy_button = QPushButton("Copy launch command")
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
        self.runner_combo.addItem("Auto-detect (latest GE-Proton)", "auto")
        extra_protons = find_extra_protons()
        self._proton_labels = [label for label, _ in extra_protons]
        if extra_protons:
            self.runner_combo.insertSeparator(self.runner_combo.count())
            for label, path in extra_protons:
                self.runner_combo.addItem(f"{label} (Installed)", f"umup:{path}")
        saved = gui_settings.load_gui_settings().get("runner", "auto")
        chosen = current
        if not chosen or self.runner_combo.findData(chosen) < 0:
            chosen = saved
        if self.runner_combo.findData(chosen) < 0:
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
        installed = {label for label, _ in find_extra_protons()}
        selected = self.proton_version_combo.currentData() or ""
        if not selected and self._releases:
            selected = self._releases[0]["tag"]
        if selected and any(selected in label for label in installed):
            self.install_proton_button.setText("GE-Proton installed ✓")
            self.install_proton_button.setEnabled(False)
        elif selected:
            self.install_proton_button.setText(f"Install {selected}")
            self.install_proton_button.setEnabled(True)
        else:
            self.install_proton_button.setText("Install GE-Proton")
            self.install_proton_button.setEnabled(False)
        self.install_proton_button.update()

    def _install_proton(self) -> None:
        version = self.proton_version_combo.currentData()
        if not version:
            return
        installed = {label for label, _ in find_extra_protons()}
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
        self.install_proton_button.setText("Installing…")
        self.proton_progress.setValue(0)
        self.proton_progress.setVisible(True)
        self.proton_status.setText("Preparing download…")
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
            self.cancel_button.setText("Cancel")
            self.proton_progress.setValue(100)
            self.proton_status.setText(f"Installed {version} ✓")
            self._reload_runners()
            self._update_install_button()
            self._refresh_preview()
            self.launch_state_changed.emit(False)
            QTimer.singleShot(3000, self._hide_proton_progress)

        def _fail(err: str) -> None:
            self.window.set_install_busy(False)
            self.cancel_button.setVisible(False)
            self.cancel_button.setText("Cancel")
            if err == "Download cancelled":
                self.proton_status.setText("Download cancelled")
                self.install_proton_button.setText(f"Install {version}")
                self.install_proton_button.setEnabled(True)
            else:
                self.proton_status.setText(f"Error: {err}")
                self.install_proton_button.setText("Retry install")
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
        self.cancel_button.setText("Cancelling…")

    def _hide_proton_progress(self) -> None:
        self.proton_progress.setVisible(False)
        self.proton_status.setVisible(False)

    def _default_prefix(self, kind: str) -> str:
        if kind.startswith("proton:"):
            return str(DEFAULT_PROTON_PREFIX)
        if kind == "wine" or kind.startswith("wine:"):
            return str(Path.home() / "Games" / "wine" / "default")
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
        if not cache_path:
            self.cache_info_label.setText("")
            return
        cache_dir = Path(cache_path)
        if not cache_dir.is_dir():
            self.cache_info_label.setText("No archives cached")
            self.cache_info_label.setStyleSheet(f"color: {STATUS_RED.name()};")
            return
        count = sum(1 for _ in cache_dir.glob("*.zip"))
        if count == 0:
            self.cache_info_label.setText("No archives cached")
            self.cache_info_label.setStyleSheet(f"color: {STATUS_RED.name()};")
        else:
            self.cache_info_label.setText(f"{count} archives cached")
            self.cache_info_label.setStyleSheet(f"color: {OK_GREEN.name()};")

    def _persist_dirs(self) -> None:
        if self._persisting:
            return
        self._persisting = True
        try:
            self.window.refresh_settings()
            profile = self.window.settings.active_profile
            if profile is None:
                return
            anomaly = self.anomaly_edit.text().strip()
            gamma = self.gamma_edit.text().strip()
            cache = self.cache_edit.text().strip()
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
                    self, "Save Failed", f"Could not write settings.json:\n{exc}"
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
        # Expand for every runner, not just plain Wine: '~' is equally invalid
        # as a Proton/umu prefix path.
        prefix = os.path.expanduser(self.prefix_edit.text().strip())
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
        # Resolve the runner once: each resolution probes the filesystem for
        # Steam libraries and Proton builds, and this runs on every edit.
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
        self.launch_button.setEnabled(not self._launching and (mo2_ok or direct_ok))
        self.open_mo2_button.setEnabled(not self._launching and mo2_ok)
        self.direct_button.setEnabled(
            not self._launching and direct_ok and bool(self._selected_target())
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
        runner = self.runner_combo.currentData()
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
                "Select a Proton-GE Runner or install a version from below."
            )
        elif kind.startswith("umup:"):
            self.runner_hint.setText("GE-Proton — recommended for GAMMA.")
        elif kind == "proton:stable":
            self.runner_hint.setText(
                "Steam Proton Stable — may crash with MO2 (concrt140.dll). "
                "Use GE-Proton instead if available."
            )
        elif kind.startswith("proton:"):
            self.runner_hint.setText(
                "Steam Proton — may crash with MO2 (concrt140.dll). "
                "Use GE-Proton instead if available."
            )
        elif kind.startswith("wine"):
            self.runner_hint.setText(
                "System Wine — not recommended. MO2 and GAMMA are tested "
                "against GE-Proton."
            )
        else:
            self.runner_hint.setText("")

    def _on_change(self, *_args) -> None:
        self._save_state()
        self._refresh_preview()

    def launch_game(self) -> None:
        """Launch the selected game target using the primary Play workflow."""
        # Try MO2 first; fall back to direct launch if MO2 is unavailable.
        try:
            runner = self._runner()
            self._resolve_command(open_mo2=False, direct=False, runner=runner)
            self._launch_via_mo2()
        except LaunchError:
            self._launch_direct()

    def _launch_via_mo2(self) -> None:
        if self._launching:
            return
        target = self._selected_target()
        if not target:
            QMessageBox.warning(
                self,
                "No target selected",
                "Choose which game to run from the Target list before clicking "
                "Launch Game.\n\n"
                "If the list is empty, make sure your active profile points to "
                "a GAMMA install and its ModOrganizer.ini is configured.",
            )
            self._set_launch_button_state(False)
            return
        self._set_launch_button_state(True)
        self._run(open_mo2=False, direct=False)

    def _open_mo2(self) -> None:
        if self._launching:
            return
        self._set_launch_button_state(True)
        self._run(open_mo2=True, direct=False)

    def _launch_direct(self) -> None:
        if self._launching:
            return
        self._set_launch_button_state(True)
        self._run(open_mo2=False, direct=True)

    def _run(self, *, open_mo2: bool, direct: bool) -> None:
        """Launch the game through Mod Organizer 2 or directly."""
        self._launch_status_clear_timer.stop()
        log_path = logs_dir() / "launcher.log"
        # launch_detached raises LaunchError too (spawn failures); if that
        # escapes, _launching stays True and every launch button stays dead.
        try:
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
            self._proc = launch_detached(command, env, cwd, log_path=log_path)
        except LaunchError as exc:
            self._set_result(f"Could not launch: {exc}", error=True)
            QMessageBox.warning(self, "Could not launch", str(exc))
            self._set_launch_button_state(False)
            return
        except Exception as exc:  # noqa: BLE001
            self._set_result(f"Unexpected launch error: {exc}", error=True)
            QMessageBox.warning(self, "Could not launch", str(exc))
            self._set_launch_button_state(False)
            return
        # Use a repeating timer (1 s) to detect when the process exits,
        # so buttons are re-enabled as soon as possible regardless of timing.
        self._launch_timer = QTimer(self)
        self._launch_timer.setInterval(1000)
        self._launch_timer.timeout.connect(
            lambda: self._on_launch_check(label, command, log_path)
        )
        self._launch_timer.start()
        self._set_result(f"Launching {label}...")

    def _on_launch_check(self, label: str, command: list[str], log_path: Path) -> None:
        """Called every second -- if the process has exited, re-enable buttons."""
        timer = getattr(self, "_launch_timer", None)
        if getattr(self, "_proc", None) is None:
            if timer is not None:
                timer.stop()
            return
        if self._proc.poll() is not None:
            # Process has exited -- re-enable buttons and stop timer
            self._launch_timer.stop()
            self._launch_timer.deleteLater()
            self._launch_timer = None
            self._set_launch_button_state(False)
            code = self._proc.returncode
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
                    QMessageBox.warning(self, "DXVK/Vulkan Problem", graphics_message)
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
                        "Runner/Prefix Compatibility Problem",
                        compatibility_message,
                    )
                else:
                    QMessageBox.warning(self, "Launch failed", msg)
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

    def _set_launch_button_state(self, launching: bool) -> None:
        self._launching = launching
        self.launch_button.setEnabled(not launching)
        self.open_mo2_button.setEnabled(not launching)
        self.direct_button.setEnabled(not launching)
        self.launch_state_changed.emit(launching)
