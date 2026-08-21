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
from ..winetricks import WINETRICKS_VERBS, check_winetricks_full_status
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


def _query_winetricks_status(prefix: str) -> dict[str, bool] | None:
    """Check prefix runtimes without probing running processes on the GUI thread."""
    if mo2_running():
        return None
    return check_winetricks_full_status(prefix)


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
        self._refresh_generation = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(content)

        title = section_label("COMMANDER DASHBOARD", level=1)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        subtitle = info_label(
            "COMMANDER manages STALKER Anomaly and the GAMMA Modpack on Linux. "
            "Install, update, verify, and launch your game from one place."
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(title)
        root.addWidget(subtitle)

        self.profile_card, _ = make_card(expand=True)
        root.addWidget(self.profile_card)

        self.actions_card, _ = make_card(expand=True)
        root.addWidget(self.actions_card)

        self.install_status_card, _ = make_card(expand=True)
        root.addWidget(self.install_status_card)

        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        root.addLayout(bottom)
        self.updates_card, _ = make_card(expand=True)
        bottom.addWidget(self.updates_card, 1)
        self.sizes_card, _ = make_card(expand=True)
        bottom.addWidget(self.sizes_card, 1)

        self.refresh()

    # ----- profile card -----
    def refresh(self) -> None:
        self._refresh_generation += 1
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
        layout.addWidget(section_label("Installation status"))
        profile = self.settings.active_profile
        if profile is None:
            layout.addWidget(InstallStatusRow("STALKER Anomaly", "No active profile"))
            layout.addWidget(InstallStatusRow("GAMMA Modpack", "No active profile"))
            return
        layout.addWidget(
            InstallStatusRow(
                "STALKER Anomaly",
                profile.anomaly,
                ok=anomaly_installed(profile.anomaly),
            )
        )
        layout.addWidget(
            InstallStatusRow(
                "GAMMA Modpack", profile.gamma, ok=gamma_installed(profile.gamma)
            )
        )
        self.winetricks_status = InstallStatusRow(
            "Winetricks", "Checking...", ok=None, pending_text="Checking"
        )
        layout.addWidget(self.winetricks_status)
        self._start_winetricks_status(self._refresh_generation, self.winetricks_status)

    def _paused_winetricks_status(self) -> None:
        """Hold the status as Installed while the game is running.

        The game cannot run without the runtimes, and winetricks queries against
        a running prefix are unreliable, so the live check is paused until the
        game closes.
        """
        paused = {verb: True for verb in WINETRICKS_VERBS}
        paused["wine"] = True
        paused["protontricks"] = True
        total = len(paused)
        self.winetricks_status.set_state(
            True, f"{total}/{total} dependencies installed (paused - game running)"
        )
        self.winetricks_status.set_status_tooltip(winetricks_tooltip(paused))

    def _start_winetricks_status(self, generation: int, status_widget) -> None:
        if self._winetricks_task is not None:
            return
        task = BackgroundTask(
            _query_winetricks_status,
            configured_wine_prefix(),
            parent=self,
        )
        self._winetricks_task = task
        task.result.connect(
            lambda status, generation=generation, widget=status_widget: (
                self._render_winetricks_status(status, generation, widget)
            )
        )
        task.error.connect(
            lambda message, generation=generation, widget=status_widget: (
                self._on_winetricks_error(message, generation, widget)
            )
        )
        task.start()

    def _render_winetricks_status(
        self, status: dict[str, bool] | None, generation: int, status_widget
    ) -> None:
        self._winetricks_task = None
        if (
            generation != self._refresh_generation
            or status_widget is not self.winetricks_status
        ):
            return
        if status is None:
            self._paused_winetricks_status()
            return
        installed = sum(status.values())
        total = len(status)
        self.winetricks_status.set_state(
            installed == total,
            f"{installed}/{total} dependencies installed",
        )
        self.winetricks_status.set_status_tooltip(winetricks_tooltip(status))

    def _on_winetricks_error(
        self, message: str, generation: int, status_widget
    ) -> None:
        self._winetricks_task = None
        if (
            generation != self._refresh_generation
            or status_widget is not self.winetricks_status
        ):
            return
        self.winetricks_status.set_state(
            None, "status unavailable", pending_text="Unknown"
        )
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
                    "No COMMANDER profile is active yet. Create or activate one on "
                    "the Profiles page to manage Anomaly and GAMMA."
                )
            )
            go = QPushButton("Go to Profiles")
            go.clicked.connect(lambda: self.window.set_page("profiles"))
            layout.addWidget(go)
            return
        layout.addWidget(section_label("Active COMMANDER profile"))
        for label, value in [
            ("Profile", profile.profile_name),
            ("Anomaly folder", profile.anomaly),
            ("GAMMA", profile.gamma),
            ("Cache folder", profile.cache),
            ("MO2 profile", profile.mo2_profile),
            ("Download threads", str(profile.download_threads)),
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
        generation = self._refresh_generation

        def compute() -> dict[str, int]:
            return {k: dir_size(p) for k, p in paths.items()}

        task = BackgroundTask(compute, parent=self)
        self._size_task = task
        task.result.connect(
            lambda sizes, generation=generation: self._render_sizes(sizes, generation)
        )
        task.start()

    def _render_sizes(self, sizes: dict[str, int], generation: int) -> None:
        self._size_task = None
        if generation != self._refresh_generation:
            return
        self._sizes = sizes
        layout = self.sizes_card.layout()
        clear_layout(layout)
        layout.addWidget(section_label("Storage usage"))
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
                None,
                "No active profile. Create or activate one on the Profiles page.",
                "warn",
            )
            return
        # Never spawn a second check against a tree an install is writing.
        if self.window.install_busy:
            self._render_update_card(
                None, "An installation is running. The update check is paused.", "warn"
            )
            return
        if self._update_checking:
            return
        self._update_checking = True
        generation = self._refresh_generation
        profile_id = (
            profile.profile_name,
            profile.anomaly,
            profile.gamma,
            profile.cache,
        )
        self._render_update_card(
            None, "Checking the active GAMMA installation for updates...", "dim"
        )
        task = BackgroundTask(
            check_updates,
            profile,
            parent=self,
        )
        self._update_checker = task
        task.result.connect(
            lambda status, generation=generation, profile_id=profile_id: (
                self._on_update_check_done(status, generation, profile_id)
            )
        )
        task.error.connect(
            lambda message, generation=generation, profile_id=profile_id: (
                self._on_update_check_error(message, generation, profile_id)
            )
        )
        task.start()

    def _on_update_check_done(
        self, status: UpdateStatus, generation: int, profile_id
    ) -> None:
        self._update_checker = None
        current = self.window.settings.active_profile
        if (
            generation != self._refresh_generation
            or current is None
            or profile_id
            != (current.profile_name, current.anomaly, current.gamma, current.cache)
        ):
            return
        self._update_checking = False
        text, kind = status_summary(status)
        self._render_update_card(status, text, kind)

    def _on_update_check_error(self, message: str, generation: int, profile_id) -> None:
        self._update_checker = None
        current = self.window.settings.active_profile
        if (
            generation != self._refresh_generation
            or current is None
            or profile_id
            != (current.profile_name, current.anomaly, current.gamma, current.cache)
        ):
            return
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
            grid.addWidget(info_label("Installed GAMMA version:"), 0, 0)
            installed_value = QLabel(
                format_version(status.installed, status.installed_human)
            )
            grid.addWidget(installed_value, 0, 1)
            grid.addWidget(info_label("Latest GAMMA version:"), 1, 0)
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
        check_button = QPushButton("Check for updates")
        check_button.setObjectName("primary")
        check_button.setEnabled(
            not self._update_checking
            and not self.window.install_busy
            and self.settings.active_profile is not None
        )
        check_button.clicked.connect(self._start_update_check)
        row.addWidget(check_button)
        if status is not None and status.update_available:
            goto_button = QPushButton("Open updates")
            goto_button.clicked.connect(lambda: self.window.set_page("update"))
            row.addWidget(goto_button)
        row.addStretch(1)
        layout.addLayout(row)

    # ----- actions card -----
    def _build_actions(self) -> None:
        layout = self.actions_card.layout()
        clear_layout(layout)
        layout.addWidget(section_label("Quick actions"))
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
                ("Open cache folder", profile.cache),
                ("Open log folder", "logs"),
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
