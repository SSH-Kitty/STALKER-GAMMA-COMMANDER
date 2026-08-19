"""Dashboard: active profile overview, install status, storage, quick actions."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..gui_settings import configured_wine_prefix
from ..settings import CliSettings
from ..updates import UpdateStatus, check_updates, format_version, status_summary
from ..winetricks import WINETRICKS_VERBS, check_winetricks_status
from .common import (
    BackgroundTask,
    InstallStatusRow,
    anomaly_installed,
    clear_layout,
    dir_size,
    gamma_installed,
    human_size,
    info_label,
    make_card,
    mo2_running,
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
        self._update_checker: BackgroundTask | None = None
        self._update_checking = False
        self._winetricks_task: BackgroundTask | None = None
        self._size_task: BackgroundTask | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(content)

        title = section_label("COMMANDER DASHBOARD", level=1)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        subtitle = info_label(
            "A graphical front-end for STALKER GAMMA on Linux. "
            "Install, update and manage your GAMMA modpack."
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(title)
        root.addWidget(subtitle)

        self.profile_card, _ = make_card()
        root.addWidget(self.profile_card)

        self.actions_card, _ = make_card()
        root.addWidget(self.actions_card)

        self.install_status_card, _ = make_card()
        root.addWidget(self.install_status_card)

        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        root.addLayout(bottom)
        self.updates_card, _ = make_card()
        bottom.addWidget(self.updates_card, 1)
        self.sizes_card, _ = make_card()
        bottom.addWidget(self.sizes_card, 1)

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
            "Winetricks", "Checking...", ok=None, pending_text="Checking"
        )
        layout.addWidget(self.winetricks_status)
        self._start_winetricks_status()

    def _paused_winetricks_status(self) -> None:
        """Hold the status as Installed while the game is running.

        The game cannot run without the runtimes, and winetricks queries against
        a running prefix are unreliable, so the live check is paused until the
        game closes.
        """
        paused = {verb: True for verb in WINETRICKS_VERBS}
        total = len(paused)
        self.winetricks_status.set_state(
            True, f"{total}/{total} runtimes installed (paused - game running)"
        )
        self.winetricks_status.set_status_tooltip(winetricks_tooltip(paused))

    def _start_winetricks_status(self) -> None:
        if mo2_running():
            self._paused_winetricks_status()
            return
        if self._winetricks_task is not None:
            return
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
        self._winetricks_task = None
        if mo2_running():
            self._paused_winetricks_status()
            return
        installed = sum(status.values())
        total = len(status)
        self.winetricks_status.set_state(
            installed == total,
            f"{installed}/{total} runtimes installed",
        )
        self.winetricks_status.set_status_tooltip(winetricks_tooltip(status))

    def _on_winetricks_error(self, message: str) -> None:
        self._winetricks_task = None
        if mo2_running():
            self._paused_winetricks_status()
            return
        self.winetricks_status.set_state(None, "status unavailable", pending_text="Unknown")
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
        if self._size_task is not None:
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
        self._size_task = None
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
        profile = self.settings.active_profile
        if profile is None:
            self._render_update_card(
                None, "No active profile - create one on the Profiles page.", "warn"
            )
            return
        # Never spawn a second check against a tree an install is writing.
        if self.window.install_busy:
            self._render_update_card(
                None, "Install running - update check paused.", "warn"
            )
            return
        if self._update_checking:
            return
        self._update_checking = True
        self._render_update_card(None, "Checking for updates...", "dim")
        task = BackgroundTask(
            check_updates,
            profile,
            parent=self,
        )
        self._update_checker = task
        task.result.connect(self._on_update_check_done)
        task.error.connect(self._on_update_check_error)
        task.start()

    def _on_update_check_done(self, status: UpdateStatus) -> None:
        self._update_checker = None
        self._update_checking = False
        text, kind = status_summary(status)
        self._render_update_card(status, text, kind)

    def _on_update_check_error(self, message: str) -> None:
        self._update_checker = None
        self._update_checking = False
        self._render_update_card(None, f"Update check failed: {message}", "warn")

    def _render_update_card(
        self,
        status: UpdateStatus | None,
        status_text: str,
        status_kind: str,
    ) -> None:
        layout = self.updates_card.layout()
        clear_layout(layout)
        layout.addWidget(section_label("Updates"))

        if status is not None and status.installed is not None:
            grid = QGridLayout()
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(6)
            grid.addWidget(info_label("Installed version:"), 0, 0)
            installed_value = QLabel(
                format_version(status.installed, status.installed_human)
            )
            grid.addWidget(installed_value, 0, 1)
            grid.addWidget(info_label("Latest version:"), 1, 0)
            latest_value = QLabel(
                format_version(status.latest, status.latest_human, missing="-")
            )
            grid.addWidget(latest_value, 1, 1)
            grid.setColumnStretch(2, 1)
            layout.addLayout(grid)

        status_label = info_label(status_text)
        status_label.setObjectName(status_kind)
        status_label.style().unpolish(status_label)
        status_label.style().polish(status_label)
        layout.addWidget(status_label)

        row = QHBoxLayout()
        check_button = QPushButton("Check for Updates")
        check_button.setObjectName("primary")
        check_button.setEnabled(
            not self._update_checking
            and not self.window.install_busy
            and self.settings.active_profile is not None
        )
        check_button.clicked.connect(self._start_update_check)
        row.addWidget(check_button)
        if status is not None and status.update_available:
            goto_button = QPushButton("Go to Update Page")
            goto_button.clicked.connect(lambda: self.window.set_page("update"))
            row.addWidget(goto_button)
        row.addStretch(1)
        layout.addLayout(row)

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
