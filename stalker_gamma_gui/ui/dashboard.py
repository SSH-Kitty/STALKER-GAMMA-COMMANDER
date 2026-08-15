"""Dashboard: active profile overview, install status, storage, quick actions."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..gui_settings import configured_wine_prefix
from ..parsers import strip_ansi
from ..settings import CliSettings
from ..winetricks import check_winetricks_status
from .common import (
    BackgroundTask,
    CommandRunner,
    InstallStatusRow,
    anomaly_installed,
    clear_layout,
    dir_size,
    gamma_installed,
    human_size,
    info_label,
    make_card,
    open_in_file_manager,
    section_label,
    winetricks_tooltip,
)


class DashboardPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.settings: CliSettings = window.settings
        self._sizes: dict[str, int] = {}
        self._update_checker: CommandRunner | None = None
        self._winetricks_task: BackgroundTask | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title = section_label("STALKER GAMMA COMMANDER", level=1)
        subtitle = info_label(
            "A graphical front-end for STALKER GAMMA on Linux. "
            "Install, update and manage your GAMMA modpack."
        )
        root.addWidget(title)
        root.addWidget(subtitle)

        self.profile_card, _ = make_card()
        root.addWidget(self.profile_card)

        self.install_status_card, _ = make_card()
        root.addWidget(self.install_status_card)

        self.updates_card, _ = make_card()
        root.addWidget(self.updates_card)

        self.sizes_card, _ = make_card()
        root.addWidget(self.sizes_card)

        self.actions_card, _ = make_card()
        root.addWidget(self.actions_card)

        root.addStretch(1)
        self.refresh()

    # ----- profile card -----
    def refresh(self) -> None:
        self.window.refresh_settings()
        self.settings = self.window.settings
        self._render_profile()
        self._render_install_status()
        self._build_actions()
        self._start_size_task()
        self._start_update_check()

    # ----- install status card -----
    def _render_install_status(self) -> None:
        layout = self.install_status_card.layout()
        clear_layout(layout)
        layout.addWidget(section_label("Install Status"))
        profile = self.settings.active_profile
        if profile is None:
            layout.addWidget(InstallStatusRow("Stalker Anomaly", "No active profile"))
            layout.addWidget(InstallStatusRow("Stalker GAMMA", "No active profile"))
            return
        layout.addWidget(
            InstallStatusRow(
                "Stalker Anomaly", profile.anomaly, ok=anomaly_installed(profile.anomaly)
            )
        )
        layout.addWidget(
            InstallStatusRow("Stalker GAMMA", profile.gamma, ok=gamma_installed(profile.gamma))
        )
        self.winetricks_status = InstallStatusRow(
            "Winetricks", "Checking...", ok=None
        )
        layout.addWidget(self.winetricks_status)
        self._start_winetricks_status()

    def _start_winetricks_status(self) -> None:
        task = BackgroundTask(
            check_winetricks_status,
            configured_wine_prefix(),
            parent=self,
        )
        self._winetricks_task = task
        task.result.connect(self._render_winetricks_status)
        task.error.connect(self._on_winetricks_error)
        task.start()

    def _render_winetricks_status(self, status: dict[str, bool]) -> None:
        installed = sum(status.values())
        total = len(status)
        self.winetricks_status.set_state(
            installed == total,
            f"{installed}/{total} runtimes installed",
        )
        self.winetricks_status.set_status_tooltip(winetricks_tooltip(status))

    def _on_winetricks_error(self, message: str) -> None:
        self.winetricks_status.set_state(None, "status unavailable")
        self.winetricks_status.set_status_tooltip(
            f"Could not query winetricks: {message}"
        )

    def _render_profile(self) -> None:
        layout = self.profile_card.layout()
        clear_layout(layout)
        profile = self.settings.active_profile
        if profile is None:
            layout.addWidget(section_label("No Active Profile"))
            layout.addWidget(
                info_label(
                    "No profile is configured yet. Create or activate a profile "
                    "to get started."
                )
            )
            go = QPushButton("Go to Profiles")
            go.clicked.connect(lambda: self.window.set_page("profiles"))
            layout.addWidget(go)
            return
        layout.addWidget(section_label("Active Profile"))
        for label, value in [
            ("Profile", profile.profile_name),
            ("Anomaly", profile.anomaly),
            ("GAMMA", profile.gamma),
            ("Cache", profile.cache),
            ("MO2 Profile", profile.mo2_profile),
            ("Download Threads", str(profile.download_threads)),
        ]:
            row = QHBoxLayout()
            key = QLabel(label)
            key.setObjectName("dim")
            val = QLabel(value)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(key)
            row.addStretch(1)
            row.addWidget(val)
            layout.addLayout(row)

    # ----- sizes card -----
    def _start_size_task(self) -> None:
        profile = self.settings.active_profile
        if profile is None:
            return
        paths = {
            "Anomaly": profile.anomaly,
            "GAMMA": profile.gamma,
            "Cache": profile.cache,
        }

        def compute() -> dict[str, int]:
            return {k: dir_size(p) for k, p in paths.items()}

        task = BackgroundTask(compute, parent=self)
        self._size_task = task
        task.result.connect(self._render_sizes)
        task.start()

    def _render_sizes(self, sizes: dict[str, int]) -> None:
        self._sizes = sizes
        layout = self.sizes_card.layout()
        clear_layout(layout)
        layout.addWidget(section_label("Storage Usage"))
        total = sum(sizes.values())
        for key, value in sizes.items():
            bar_label = QLabel(f"{key}: {human_size(value)}")
            layout.addWidget(bar_label)
        total_label = QLabel(f"Total: {human_size(total)}")
        total_label.setObjectName("accent")
        layout.addWidget(total_label)

    # ----- updates card -----
    def _start_update_check(self) -> None:
        if self.settings.active_profile is None:
            return
        # Never spawn a second CLI process against a tree an install is writing.
        if self.window.install_busy:
            self._set_update_status("Install running - update check paused")
            return
        if self._update_checker is not None and self._update_checker.is_running():
            return
        from ..cli_runner import cli_command

        runner = CommandRunner(
            cli_command(["update", "check"]),
            parent=self,
        )
        self._update_checker = runner
        runner.line.connect(self._on_update_line)
        runner.start()

    def _on_update_line(self, line: str) -> None:
        clean = strip_ansi(line)
        if clean.startswith("Updates available:"):
            self._set_update_status(f"{clean.strip()} - click Update above")
        elif clean == "No updates found":
            self._set_update_status("GAMMA is up to date")

    def _set_update_status(self, text: str) -> None:
        layout = self.updates_card.layout()
        clear_layout(layout)
        layout.addWidget(section_label("Updates"))
        label = info_label(text)
        if text.startswith("Updates available"):
            label.setObjectName("accent")
        layout.addWidget(label)

    # ----- actions card -----
    def _build_actions(self) -> None:
        layout = self.actions_card.layout()
        clear_layout(layout)
        layout.addWidget(section_label("Quick Actions"))
        profile = self.settings.active_profile
        if profile is not None:
            play = QPushButton("Play GAMMA")
            play.setObjectName("primary")
            self._play_button = play
            play.clicked.connect(self._play_gamma)
            layout.addWidget(play)
            QTimer.singleShot(0, self._bind_play_state)
            grid = QGridLayout()
            grid.setSpacing(8)
            buttons: list[tuple[str, str]] = [
                ("Open Anomaly folder", profile.anomaly),
                ("Open GAMMA folder", profile.gamma),
                ("Open Cache folder", profile.cache),
                ("Open logs", "logs"),
            ]
            for i, (text, target) in enumerate(buttons):
                btn = QPushButton(text)
                if target == "logs":
                    from ..config import logs_dir

                    log_dir = str(logs_dir())
                    btn.clicked.connect(lambda _, t=log_dir: self._open_folder(t))
                else:
                    btn.clicked.connect(lambda _, t=target: self._open_folder(t))
                grid.addWidget(btn, i // 2, i % 2)
            layout.addLayout(grid)

    def _bind_play_state(self) -> None:
        play_page = self.window._pages.get("play")
        button = getattr(self, "_play_button", None)
        if play_page is None or button is None:
            return
        button.setEnabled(not play_page._launching)
        play_page.launch_state_changed.connect(button.setDisabled)

    def _play_gamma(self) -> None:
        self.window._pages["play"].launch_game()

    def _open_folder(self, target: str) -> None:
        if not open_in_file_manager(target):
            self.window.statusBar().showMessage(
                f"Could not open folder: {target}", 6000
            )
