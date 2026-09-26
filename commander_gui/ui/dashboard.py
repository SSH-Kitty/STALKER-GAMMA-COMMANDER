"""Dashboard: active profile overview, install status, storage, quick actions."""

from __future__ import annotations

import time

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..achievements import ACHIEVEMENTS, unlocked_count
from ..cli_runner import run_sync
from ..game_stats import STAT_FIELDS, SaveStats, latest_save_stats, stat_value
from ..gui_settings import configured_wine_prefix, load_gui_settings, save_gui_settings
from ..launcher import find_extra_protons
from ..settings import CliSettings
from ..themes import active_theme_tokens
from ..updates import UpdateStatus, check_updates, format_version, status_summary
from ..winetricks import WINETRICKS_VERBS, check_winetricks_full_status
from .common import (
    OK_GREEN,
    WARN,
    BackgroundTask,
    InstallStatusRow,
    NoWheelComboBox,
    activate_profile,
    anomaly_installed,
    clear_layout,
    dir_size,
    disk_usage_bytes,
    display_state,
    format_last_played,
    format_playtime,
    game_running,
    gamma_installed,
    human_size,
    info_label,
    install_hover_grow_text,
    make_card,
    mo2_running,
    open_in_file_manager,
    play_click_sound,
    section_label,
    tr,
    winetricks_tooltip,
)
from .deck_icon import deck_icon
from .deck_switch import switch_mode
from .mod_manager_page import _QUERY_TIMEOUT, _query_mo2_profiles

#: Below this much free space the drive meter turns to the warning colour:
#: a GAMMA update alone can download tens of GB.
_LOW_DISK_BYTES = 50 * 1024**3


#: Theme tokens colouring the Storage usage segments, in folder order:
#: bright accent, dark shade, light neutral - picked per theme so the three
#: always stay apart (a theme's own accent shades can be near-identical).
#: Looked up at paint time, so a theme switch recolours them straight away.
_SEGMENT_TOKENS = ("storage_a", "storage_b", "storage_c")


def _token_color(token: str) -> QColor:
    return QColor(active_theme_tokens().get(token, "#7f8f78"))


#: Height of a card's title row - the pill buttons' height (Deck Mode,
#: Achievements, Check for updates). Plain titles get the same row so cards
#: side by side have their titles on one line.
_TITLE_ROW_H = 34


def _card_title(text: str) -> QLabel:
    """A card title the height of a title row with a pill button in it."""
    label = section_label(text)
    label.setFixedHeight(_TITLE_ROW_H)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return label


def _field_combo() -> NoWheelComboBox:
    """A Profile overview dropdown: fills its grid cell, never widens the
    card for a long runner name, and shows the full text on hover."""
    combo = NoWheelComboBox()
    combo.setMinimumHeight(34)
    combo.setCursor(Qt.CursorShape.PointingHandCursor)
    combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    combo.setMinimumContentsLength(8)
    combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    combo.currentTextChanged.connect(combo.setToolTip)
    return combo


def _field(label: str, control: QWidget) -> QVBoxLayout:
    """A dim caption right above its control."""
    column = QVBoxLayout()
    column.setSpacing(4)
    caption = QLabel(label)
    caption.setObjectName("dim")
    column.addWidget(caption)
    column.addWidget(control)
    return column


class _Swatch(QWidget):
    """A small rounded square in a theme colour: a legend entry's key."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token
        self.setFixedSize(10, 10)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_token_color(self._token))
        painter.drawRoundedRect(self.rect(), 2, 2)
        painter.end()


class _SegmentedBar(QWidget):
    """One rounded bar split into ``(value, theme token)`` segments."""

    def __init__(self, segments: list[tuple[int, str]]) -> None:
        super().__init__()
        self._segments = segments
        self.setFixedHeight(14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        clip = QPainterPath()
        clip.addRoundedRect(rect, 5, 5)
        painter.setClipPath(clip)
        painter.fillRect(rect, _token_color("input"))
        total = sum(value for value, _color in self._segments)
        if total > 0:
            x = 0.0
            for index, (value, token) in enumerate(self._segments):
                width = rect.width() * value / total
                # A 2px gap between segments, not after the last.
                gap = 2 if index < len(self._segments) - 1 else 0
                painter.fillRect(
                    QRectF(x, 0, max(width - gap, 0), rect.height()), _token_color(token)
                )
                x += width
        painter.end()


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
        self._update_checking = False
        self._winetricks_task: BackgroundTask | None = None
        self._size_task: BackgroundTask | None = None
        self._mo2_profiles_task: BackgroundTask | None = None
        self._set_mo2_selected_task: BackgroundTask | None = None
        # Re-walking a ~150GB install tree on every Dashboard visit is
        # expensive; reuse a recent scan of the same paths instead.
        self._size_cache_key: tuple[str, str, str] | None = None
        self._size_cache_time: float = 0.0
        self._SIZE_CACHE_TTL = 30.0
        self._refresh_generation = 0
        self._play_button_connection = None

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


        self.profile_card, _ = make_card(expand=True)
        root.addWidget(self.profile_card)

        self.actions_card, _ = make_card(expand=True)
        root.addWidget(self.actions_card)

        # Two rows of two: Installation status | Updates, then
        # Game stats | Storage usage.
        status_row = QHBoxLayout()
        status_row.setSpacing(16)
        root.addLayout(status_row)
        self.install_status_card, _ = make_card(expand=True)
        status_row.addWidget(self.install_status_card, 1)
        self.updates_card, _ = make_card(expand=True)
        status_row.addWidget(self.updates_card, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        root.addLayout(bottom)
        self._save: SaveStats | None = None
        self._stats_task: BackgroundTask | None = None
        self._stats_for: tuple | None = None
        self._stats_connection = None
        self._build_stats_card()
        bottom.addWidget(self.stats_card, 1)
        self.sizes_card, _ = make_card(expand=True)
        bottom.addWidget(self.sizes_card, 1)

        # Without this, a window taller than the page's natural content
        # stretches the Expanding-policy cards above (every make_card()
        # here uses expand=True) to fill the extra height instead of
        # leaving it as blank page space - shifting each card's, and so
        # each title's, vertical position as the window is resized rather
        # than keeping every card pinned at its natural size.
        root.addStretch(1)

        self.refresh()

    # ----- profile card -----
    def refresh(self) -> None:
        self._refresh_generation += 1
        self.window.refresh_settings()
        self.settings = self.window.settings
        self._render_profile()
        self._render_install_status()
        self._build_actions()
        self._start_mo2_profiles_task()
        self._start_size_task()
        self._start_update_check()
        self._start_stats_task()

    # ----- install status card -----
    def _render_install_status(self) -> None:
        layout = self.install_status_card.layout()
        clear_layout(layout)
        layout.addWidget(_card_title(tr("Installation status")))
        profile = self.settings.active_profile
        if profile is None:
            layout.addWidget(InstallStatusRow("STALKER Anomaly", tr("No active profile")))
            layout.addWidget(InstallStatusRow("GAMMA Modpack", tr("No active profile")))
            return
        op = getattr(self.window, "install_operation", None)
        anomaly_state = display_state(anomaly_installed(profile.anomaly), op, "anomaly")
        gamma_state = display_state(
            gamma_installed(profile.gamma, profile.mo2_profile), op, "gamma"
        )
        if anomaly_state == "installing":
            self.anomaly_status = InstallStatusRow("STALKER Anomaly", profile.anomaly)
            self.anomaly_status.set_installing(tr("Installing Anomaly..."))
        else:
            self.anomaly_status = InstallStatusRow(
                "STALKER Anomaly",
                profile.anomaly,
                ok=bool(anomaly_state),
            )
        layout.addWidget(self.anomaly_status)
        if gamma_state == "installing":
            self.gamma_status = InstallStatusRow("GAMMA Modpack", profile.gamma)
            self.gamma_status.set_installing(tr("Installing GAMMA..."))
        else:
            self.gamma_status = InstallStatusRow(
                "GAMMA Modpack", profile.gamma, ok=bool(gamma_state)
            )
        layout.addWidget(self.gamma_status)
        self.winetricks_status = InstallStatusRow(
            "Dependencies", tr("Checking..."), ok=None, pending_text=tr("Checking")
        )
        layout.addWidget(self.winetricks_status)
        if op == "dependencies":
            self.winetricks_status.set_installing(tr("Installing dependencies..."))
        else:
            self._start_winetricks_status(
                self._refresh_generation, self.winetricks_status
            )

    def _paused_winetricks_status(self) -> None:
        """Hold the status as Installed while the game is running.

        The game cannot run without the runtimes, and winetricks queries against
        a running prefix are unreliable, so the live check is paused until the
        game closes.
        """
        paused = {verb: True for verb in WINETRICKS_VERBS}
        paused["wine"] = True
        paused["protontricks"] = True
        paused["umu"] = True
        total = len(paused)
        self.winetricks_status.set_state(
            True,
            tr(
                "{total}/{total} dependencies installed (paused - game running)",
                total=total,
            ),
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
            if generation != self._refresh_generation:
                self._render_install_status()
            return
        if getattr(self.window, "install_operation", None) == "dependencies":
            # Live "Installing..." status must survive refreshes.
            return
        if status is None:
            self._paused_winetricks_status()
            return
        installed = sum(status.values())
        total = len(status)
        self.winetricks_status.set_state(
            installed == total,
            tr("{installed}/{total} dependencies installed", installed=installed, total=total),
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
            if generation != self._refresh_generation:
                self._render_install_status()
            return
        if getattr(self.window, "install_operation", None) == "dependencies":
            return
        self.winetricks_status.set_state(
            None, tr("status unavailable"), pending_text=tr("Unknown")
        )
        self.winetricks_status.set_status_tooltip(
            tr("Could not query dependencies: {message}", message=message)
        )

    def _render_profile(self) -> None:
        layout = self.profile_card.layout()
        clear_layout(layout)
        profile = self.settings.active_profile
        if profile is None:
            layout.addWidget(section_label(tr("No Active Profile")))
            layout.addWidget(
                info_label(
                    tr("No COMMANDER profile is active yet. Create or activate one on the Profiles page to manage Anomaly and GAMMA.")
                )
            )
            go = QPushButton(tr("Go to Profiles"))
            go.clicked.connect(lambda: self.window.set_page("profiles"))
            layout.addWidget(go)
            return
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(_card_title(tr("Profile overview")))
        header.addStretch(1)
        manage = QPushButton(tr("Manage profiles"))
        manage.setObjectName("pillButton")
        manage.setFixedHeight(_TITLE_ROW_H)
        manage.setCursor(Qt.CursorShape.PointingHandCursor)
        manage.clicked.connect(lambda: self.window.set_page("profiles"))
        header.addWidget(manage)
        layout.addLayout(header)

        # Settings on the left as a 2x2 grid of labelled fields - each
        # label right above its control, rather than at the far end of a
        # full-width row - and the session numbers as tiles on the right.
        # Anomaly/GAMMA/Cache folder paths are deliberately not repeated
        # here: the Installation status card already shows them.
        body = QHBoxLayout()
        body.setSpacing(24)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        # Profile: a live switcher between COMMANDER profiles.
        profile_combo = _field_combo()
        for candidate in self.settings.profiles:
            profile_combo.addItem(candidate.profile_name, candidate.profile_name)
        profile_combo.blockSignals(True)
        profile_combo.setCurrentIndex(
            max(profile_combo.findData(profile.profile_name), 0)
        )
        profile_combo.blockSignals(False)
        profile_combo.currentIndexChanged.connect(
            lambda _index, combo=profile_combo: self._on_dashboard_profile_switch(combo)
        )
        grid.addLayout(_field(tr("Profile"), profile_combo), 0, 0)

        # MO2 profile - populated for real by _start_mo2_profiles_task()
        # once its background query returns; starts out showing just the
        # configured profile so there is never a blank/empty combo.
        self.mo2_profile_combo = _field_combo()
        self.mo2_profile_combo.addItem(profile.mo2_profile, profile.mo2_profile)
        self.mo2_profile_combo.currentIndexChanged.connect(
            lambda _index, combo=self.mo2_profile_combo: self._on_mo2_profile_switch(
                combo
            )
        )
        grid.addLayout(_field(tr("MO2 profile"), self.mo2_profile_combo), 0, 1)

        # Current runner - same combo (Auto-detect + every installed
        # GE-Proton version) and the same gui_settings "runner" key as the
        # Play page's own runner selector.
        self.runner_combo = _field_combo()
        self._populate_runner_combo(self.runner_combo)
        self.runner_combo.currentIndexChanged.connect(
            lambda _index, combo=self.runner_combo: self._on_runner_switch(combo)
        )
        grid.addLayout(_field(tr("Current runner"), self.runner_combo), 1, 0)

        # Download threads - a fixed 3-option "just pick a speed" choice
        # rather than the Profiles page's free-form 1-20 spin box.
        self.download_threads_combo = _field_combo()
        for value, threads_label in (
            (4, tr("4 (Safe)")),
            (6, tr("6 (Balanced)")),
            (8, tr("8 (Fast)")),
        ):
            self.download_threads_combo.addItem(threads_label, value)
        idx = self.download_threads_combo.findData(profile.download_threads)
        self.download_threads_combo.blockSignals(True)
        self.download_threads_combo.setCurrentIndex(max(idx, 0))
        self.download_threads_combo.blockSignals(False)
        self.download_threads_combo.currentIndexChanged.connect(
            lambda _index, combo=self.download_threads_combo: (
                self._on_download_threads_switch(combo)
            )
        )
        grid.addLayout(
            _field(tr("Download threads"), self.download_threads_combo), 1, 1
        )
        body.addLayout(grid, 3)

        divider = QFrame()
        divider.setObjectName("vDivider")
        divider.setFixedWidth(1)
        body.addWidget(divider)

        state = load_gui_settings()
        playtime_seconds = state.get("playtime_seconds", {}).get(profile.profile_name, 0.0)
        last_played_ts = state.get("last_played_ts", {}).get(profile.profile_name)
        tiles = QVBoxLayout()
        tiles.setSpacing(10)
        tiles.addStretch(1)
        for caption, value in (
            ("Total playtime", format_playtime(playtime_seconds)),
            ("Last played", format_last_played(last_played_ts)),
        ):
            value_label = QLabel(value)
            value_label.setObjectName("statValue")
            value_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            caption_label = QLabel(tr(caption))
            caption_label.setObjectName("statCaption")
            caption_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            tiles.addWidget(value_label)
            tiles.addWidget(caption_label)
            tiles.addSpacing(4)
        tiles.addStretch(1)
        body.addLayout(tiles, 1)
        layout.addLayout(body)

    def _sync_profile_combo(self, combo: NoWheelComboBox) -> None:
        """Reset the profile combo's displayed selection to the real active profile."""
        active = self.settings.active_profile
        combo.blockSignals(True)
        if active is not None:
            combo.setCurrentIndex(max(combo.findData(active.profile_name), 0))
        combo.blockSignals(False)

    def _on_dashboard_profile_switch(self, combo: NoWheelComboBox) -> None:
        name = combo.currentData()
        if not name:
            return
        active = self.settings.active_profile
        if active is not None and active.profile_name == name:
            return
        # Same guard the Profiles page's "Set active" applies: repointing
        # every page at another profile while an install is writing into the
        # current one's folders must not be possible from here either.
        if self.window.install_busy:
            self._sync_profile_combo(combo)
            return
        if active is not None and game_running(force=True):
            answer = QMessageBox.question(
                self,
                tr("Game Running"),
                tr("Mod Organizer / the game appears to be running under the current active profile ('{active_name}').\n\nSwitching the active profile now will not stop it, but COMMANDER's other pages will stop reflecting its state.\n\nSwitch anyway?", active_name=active.profile_name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._sync_profile_combo(combo)
                return
        combo.setEnabled(False)
        # _render_profile() rebuilds this card from scratch (clear_layout()
        # deletes the combo) on every refresh, and a refresh can land while
        # the CLI "config use" call is still in flight - touching the combo
        # from the callback then raises "Internal C++ object already
        # deleted" inside a slot. The newly rendered combo already shows the
        # real active profile, so there is nothing left to restore.
        generation = self._refresh_generation

        def _done(success: bool) -> None:
            if success:
                self.refresh()
            elif generation == self._refresh_generation:
                combo.setEnabled(True)
                self._sync_profile_combo(combo)

        activate_profile(self.window, self, name, on_done=_done)

    # ----- MO2 profile switcher -----
    def _start_mo2_profiles_task(self) -> None:
        if self._mo2_profiles_task is not None:
            return
        if self.settings.active_profile is None:
            return
        generation = self._refresh_generation
        task = BackgroundTask(_query_mo2_profiles, parent=self)
        task.result.connect(
            lambda result, generation=generation: self._on_mo2_profiles_loaded(
                result, generation
            )
        )
        task.error.connect(
            lambda _msg, generation=generation: self._on_mo2_profiles_error(generation)
        )
        self._mo2_profiles_task = task
        task.start()

    def _on_mo2_profiles_loaded(
        self, result: tuple[list[str], str], generation: int
    ) -> None:
        self._mo2_profiles_task = None
        if generation != self._refresh_generation:
            return
        combo = getattr(self, "mo2_profile_combo", None)
        names, selected = result
        if combo is None or not names:
            return
        profile = self.settings.active_profile
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(names)
        # Same preference as Mod Manager's own profile combo: MO2's actual
        # selected profile wins over a stale CliProfile.mo2_profile field.
        for candidate in (selected, profile.mo2_profile if profile else None):
            if not candidate:
                continue
            wanted = candidate.upper()
            match = next((n for n in names if n.upper() == wanted), None)
            if match is not None:
                combo.setCurrentText(match)
                break
        combo.blockSignals(False)

    def _on_mo2_profiles_error(self, generation: int) -> None:
        self._mo2_profiles_task = None

    def _sync_mo2_profile_combo(self, combo: NoWheelComboBox) -> None:
        """Reset the MO2 profile combo to the real configured profile."""
        profile = self.settings.active_profile
        combo.blockSignals(True)
        if profile is not None:
            idx = combo.findText(profile.mo2_profile)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _on_mo2_profile_switch(self, combo: NoWheelComboBox) -> None:
        name = combo.currentText()
        if not name:
            return
        profile = self.settings.active_profile
        if profile is not None and profile.mo2_profile == name:
            return
        if self.window.install_busy:
            self._sync_mo2_profile_combo(combo)
            return
        if mo2_running():
            # Mirrors Mod Manager's own guard on this exact action: MO2
            # rewrites ModOrganizer.ini on exit and would silently
            # overwrite this change.
            QMessageBox.warning(
                self,
                tr("Mod Organizer is running"),
                tr("Close Mod Organizer first - it would overwrite this change when it exits."),
            )
            self._sync_mo2_profile_combo(combo)
            return
        combo.setEnabled(False)
        generation = self._refresh_generation
        task = BackgroundTask(
            run_sync,
            ["mo2", "config", "set", "selected-profile", name],
            timeout=_QUERY_TIMEOUT,
            parent=self,
        )
        task.result.connect(
            lambda res, name=name, generation=generation: self._on_mo2_profile_set(
                name, generation, *res
            )
        )
        task.error.connect(
            lambda _msg, generation=generation: self._on_mo2_profile_set_error(
                generation
            )
        )
        self._set_mo2_selected_task = task
        task.start()

    def _on_mo2_profile_set(
        self, name: str, generation: int, rc: int, out: str
    ) -> None:
        self._set_mo2_selected_task = None
        if generation != self._refresh_generation:
            return
        combo = getattr(self, "mo2_profile_combo", None)
        if rc != 0:
            if combo is not None:
                combo.setEnabled(True)
                self._sync_mo2_profile_combo(combo)
            QMessageBox.warning(
                self, tr("Failed"), out.strip() or tr("Could not set selected profile")
            )
            return
        # _render_profile() rebuilds this card from scratch on refresh() -
        # do not touch `combo` after this point (see the Profile combo's
        # own switch handler above for why).
        profile = self.settings.active_profile
        if profile is not None and profile.mo2_profile != name:
            profile.mo2_profile = name
            self.settings.save()
        self.refresh()

    def _on_mo2_profile_set_error(self, generation: int) -> None:
        self._set_mo2_selected_task = None
        if generation != self._refresh_generation:
            return
        combo = getattr(self, "mo2_profile_combo", None)
        if combo is not None:
            combo.setEnabled(True)
            self._sync_mo2_profile_combo(combo)

    def _populate_runner_combo(self, combo: NoWheelComboBox) -> None:
        """Same item list/order as the Play page's own runner combo."""
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(tr("Auto-detect (latest GE-Proton)"), "auto")
        extra_protons = find_extra_protons()
        if extra_protons:
            combo.insertSeparator(combo.count())
            for label, path in extra_protons:
                combo.addItem(tr("{label} (Installed)", label=label), f"umup:{path}")
        saved = load_gui_settings().get("runner", "auto")
        idx = combo.findData(saved)
        if idx < 0:
            idx = combo.findData("auto")
        combo.setCurrentIndex(max(idx, 0))
        combo.blockSignals(False)

    def _on_runner_switch(self, combo: NoWheelComboBox) -> None:
        kind = combo.currentData() or "auto"
        if kind == (load_gui_settings().get("runner") or "auto"):
            return
        # Just the shared gui_settings "runner" key - the Play page
        # computes its own wine-prefix-per-runner and launch preview from
        # this same key the next time it loads, so there is nothing else
        # to keep in sync here.
        save_gui_settings(runner=kind)
        self._render_profile()

    def _on_download_threads_switch(self, combo: NoWheelComboBox) -> None:
        value = combo.currentData()
        profile = self.settings.active_profile
        if profile is None or value is None or profile.download_threads == value:
            return
        profile.download_threads = value
        self.settings.save()

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
        cache_key = (profile.anomaly, profile.gamma, profile.cache)
        if (
            cache_key == self._size_cache_key
            and time.monotonic() - self._size_cache_time < self._SIZE_CACHE_TTL
        ):
            self._render_sizes(self._sizes, generation)
            return
        if self._size_cache_key != cache_key:
            # Walking ~100k files takes a second or more - several on a
            # cold disk cache right after launch. Meanwhile show the last
            # numbers measured for these same folders (kept across
            # launches), or a placeholder, never an empty card.
            stored = load_gui_settings().get("storage_sizes") or {}
            if stored.get("key") == list(cache_key) and isinstance(
                stored.get("sizes"), dict
            ):
                self._draw_sizes(stored["sizes"])
            else:
                self._draw_sizes(None)

        def compute() -> dict[str, int]:
            return {k: dir_size(p) for k, p in paths.items()}

        task = BackgroundTask(compute, parent=self)
        self._size_task = task
        task.result.connect(
            lambda sizes, generation=generation, key=cache_key: self._render_sizes(
                sizes, generation, cache_key=key
            )
        )
        task.error.connect(
            lambda message, generation=generation: self._on_size_error(
                message, generation
            )
        )
        task.start()

    def _render_sizes(
        self,
        sizes: dict[str, int],
        generation: int,
        unavailable: str | None = None,
        cache_key: tuple[str, str, str] | None = None,
    ) -> None:
        self._size_task = None
        if generation != self._refresh_generation:
            self._start_size_task()
            return
        self._sizes = sizes
        if cache_key is not None:
            self._size_cache_key = cache_key
            self._size_cache_time = time.monotonic()
            stored = load_gui_settings().get("storage_sizes") or {}
            if stored.get("key") != list(cache_key) or stored.get("sizes") != sizes:
                save_gui_settings(storage_sizes={"key": list(cache_key), "sizes": sizes})
        self._draw_sizes(sizes, unavailable)

    def _draw_sizes(
        self, sizes: dict[str, int] | None, unavailable: str | None = None
    ) -> None:
        """Draw the Storage usage card; ``sizes`` None is the placeholder
        shown while the first measurement runs."""
        layout = self.sizes_card.layout()
        clear_layout(layout)
        layout.addWidget(_card_title(tr("Storage usage")))
        if sizes is None:
            layout.addWidget(_SegmentedBar([]))
            measuring = QLabel(tr("Measuring folder sizes..."))
            measuring.setObjectName("dim")
            layout.addWidget(measuring)
            self._draw_free_space(layout)
            return
        total = sum(sizes.values())
        segments = [
            (value, _SEGMENT_TOKENS[index % len(_SEGMENT_TOKENS)])
            for index, value in enumerate(sizes.values())
        ]
        layout.addWidget(_SegmentedBar(segments))

        # Legend: a swatch per folder, then the total on the right.
        legend = QHBoxLayout()
        legend.setSpacing(18)
        for (key, value), (_value, token) in zip(sizes.items(), segments, strict=True):
            item = QHBoxLayout()
            item.setSpacing(6)
            item.addWidget(_Swatch(token), 0, Qt.AlignmentFlag.AlignVCenter)
            item.addWidget(QLabel(tr("{key}: {arg}", key=key, arg=human_size(value))))
            legend.addLayout(item)
        legend.addStretch(1)
        total_label = QLabel(tr("Total: {arg}", arg=human_size(total)))
        total_label.setStyleSheet(f"color: {OK_GREEN.name()};")
        legend.addWidget(total_label)
        layout.addLayout(legend)
        self._draw_free_space(layout)
        if unavailable is not None:
            status_label = info_label(tr("Storage usage unavailable: {unavailable}", unavailable=unavailable))
            status_label.setObjectName("warn")
            layout.addWidget(status_label)

    def _draw_free_space(self, layout: QVBoxLayout) -> None:
        """Free space on the drive - one statvfs call, so never waited on."""
        profile = self.settings.active_profile
        usage = (
            disk_usage_bytes(profile.gamma or profile.anomaly)
            if profile is not None and (profile.gamma or profile.anomaly)
            else None
        )
        if usage is not None and usage[0] > 0:
            disk_total, free = usage
            used_percent = round(100 * (disk_total - free) / disk_total)
            layout.addSpacing(6)
            free_row = QHBoxLayout()
            free_label = QLabel(
                tr(
                    "Free on drive: {free} of {total}",
                    free=human_size(free),
                    total=human_size(disk_total),
                )
            )
            free_row.addWidget(free_label)
            free_row.addStretch(1)
            percent_label = QLabel(tr("{percent}% used", percent=used_percent))
            percent_label.setObjectName("dim")
            free_row.addWidget(percent_label)
            layout.addLayout(free_row)
            meter = QProgressBar()
            meter.setTextVisible(False)
            meter.setRange(0, 100)
            meter.setValue(used_percent)
            low = free < _LOW_DISK_BYTES
            meter.setObjectName("storageMeterLow" if low else "storageMeter")
            if low:
                free_label.setObjectName("warn")
                hint = tr("Low on space - GAMMA updates and new mods need room.")
                free_label.setToolTip(hint)
                meter.setToolTip(hint)
            layout.addWidget(meter)

    def _on_size_error(self, message: str, generation: int) -> None:
        self._size_task = None
        if generation != self._refresh_generation:
            self._start_size_task()
            return
        self._render_sizes(self._sizes, generation, unavailable=message)

    # ----- game stats card -----
    def _build_stats_card(self) -> None:
        """Counters from the newest save, and the Achievements window.

        Built once: only the values and the footer change on a refresh.
        """
        self.stats_card, layout = make_card(expand=True)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        # Which save the numbers come from is a hover on the title, not a
        # line of its own under the grid.
        self.stats_title = section_label(tr("Game stats"))
        header.addWidget(self.stats_title)
        header.addStretch(1)
        self.achievements_button = QPushButton()
        self.achievements_button.setObjectName("achievementsButton")
        self.achievements_button.setFixedHeight(34)
        self.achievements_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.achievements_button.setToolTip(
            tr("Every achievement, what unlocks it and what it gives.")
        )
        self.achievements_button.clicked.connect(self._show_achievements)
        header.addWidget(self.achievements_button)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(12)
        self._stat_values: dict[str, QLabel] = {}
        for index, (key, caption) in enumerate(STAT_FIELDS):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            value = QLabel("–")
            value.setObjectName("statValue")
            value.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            label = QLabel(tr(caption))
            label.setObjectName("statCaption")
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            cell.addWidget(value)
            cell.addWidget(label)
            grid.addLayout(cell, index // 3, index % 3)
            self._stat_values[key] = value
        layout.addLayout(grid)

        # Shown only while there is no save, to explain the dashes.
        self.stats_source = info_label("")
        self.stats_source.setObjectName("dim")
        self.stats_source.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.stats_source)
        self._render_stats(None)

    def _start_stats_task(self, *, force: bool = False) -> None:
        profile = self.settings.active_profile
        self.stats_card.setVisible(profile is not None)
        if profile is None:
            return
        folders = (profile.anomaly, profile.gamma, profile.mo2_profile)
        if self._stats_task is not None and self._stats_for == folders and not force:
            return
        # A read still running for another profile's folders (the profile
        # was just switched) is superseded: its result is dropped below.
        self._stats_for = folders
        task = BackgroundTask(latest_save_stats, *folders, parent=self)
        self._stats_task = task
        task.result.connect(
            lambda result, t=task: self._on_stats_loaded(t, result)
        )
        task.error.connect(lambda _message, t=task: self._on_stats_loaded(t, None))
        task.start()

    def _on_stats_loaded(self, task: BackgroundTask, result: object) -> None:
        if task is not self._stats_task:
            return
        self._stats_task = None
        self._render_stats(result if isinstance(result, SaveStats) else None)

    def _render_stats(self, save: SaveStats | None) -> None:
        self._save = save
        self.achievements_button.setText(
            "\u2605  "
            + tr("Achievements")
            + f"   {unlocked_count(save)} / {len(ACHIEVEMENTS)}"
        )
        self.achievements_button.setEnabled(save is not None)
        if save is None:
            for value in self._stat_values.values():
                value.setText("–")
            hint = tr("No saves yet - stats appear after your first save.")
            self.stats_source.setText(hint)
            self.stats_source.setVisible(True)
            self.stats_title.setToolTip(hint)
            return
        for key, _caption in STAT_FIELDS:
            self._stat_values[key].setText(f"{stat_value(save, key):,}")
        self.stats_source.setVisible(False)
        self.stats_title.setToolTip(
            tr("From save: {name}", name=save.save_name)
            + "   ·   "
            + format_last_played(save.mtime or time.time())
        )

    def _show_achievements(self) -> None:
        from .achievements_dialog import AchievementsDialog

        AchievementsDialog(self, self._save).exec()

    # ----- updates card -----
    def _start_update_check(self) -> None:
        profile = self.settings.active_profile
        if profile is None:
            self._render_update_card(
                None,
                tr("No active profile. Create or activate one on the Profiles page."),
                "warn",
            )
            return
        # Never spawn a second check against a tree an install is writing.
        if self.window.install_busy:
            self._render_update_card(
                None,
                tr("An installation is running. The update check is paused."),
                "warn",
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
            None, tr("Checking the active GAMMA installation for updates..."), "dim"
        )
        task = BackgroundTask(
            check_updates,
            profile,
            parent=self,
        )
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
        current = self.window.settings.active_profile
        if (
            generation != self._refresh_generation
            or current is None
            or profile_id
            != (current.profile_name, current.anomaly, current.gamma, current.cache)
        ):
            self._update_checking = False
            self._start_update_check()
            return
        self._update_checking = False
        text, kind = status_summary(status)
        self._render_update_card(status, text, kind)

    def _on_update_check_error(self, message: str, generation: int, profile_id) -> None:
        current = self.window.settings.active_profile
        if (
            generation != self._refresh_generation
            or current is None
            or profile_id
            != (current.profile_name, current.anomaly, current.gamma, current.cache)
        ):
            self._update_checking = False
            self._start_update_check()
            return
        self._update_checking = False
        self._render_update_card(
            None, tr("Update check failed: {message}", message=message), "warn"
        )

    def _render_update_card(
        self,
        status: UpdateStatus | None,
        status_text: str,
        status_kind: str,
    ) -> None:
        layout = self.updates_card.layout()
        clear_layout(layout)
        # Title with the check as a pill beside it, like Deck Mode and
        # Achievements on the neighbouring cards.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(section_label(tr("Updates")))
        header.addStretch(1)
        check_button = QPushButton(tr("Check for updates"))
        check_button.setObjectName("pillButton")
        check_button.setFixedHeight(34)
        check_button.setCursor(Qt.CursorShape.PointingHandCursor)
        check_button.setEnabled(
            not self._update_checking
            and not self.window.install_busy
            and self.settings.active_profile is not None
        )
        check_button.clicked.connect(self._start_update_check)
        header.addWidget(check_button)
        layout.addLayout(header)

        if status is not None and status.installed is not None:
            grid = QGridLayout()
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(6)
            rows = (
                (
                    tr("Installed GAMMA version:"),
                    format_version(
                        status.installed, status.installed_human, show_build=False
                    ),
                ),
                (
                    tr("Latest GAMMA version:"),
                    format_version(
                        status.latest, status.latest_human, missing="-", show_build=False
                    ),
                ),
            )
            for index, (key, value) in enumerate(rows):
                # No wrapping: the key column sizes to its longest label
                # instead of breaking "Installed GAMMA version:" in two.
                key_label = QLabel(key)
                key_label.setObjectName("dim")
                grid.addWidget(key_label, index, 0)
                grid.addWidget(QLabel(value), index, 1)
            grid.setColumnStretch(2, 1)
            layout.addLayout(grid)

        # The result as a dot + text, like the Installation status rows.
        if status_kind == "accent":
            # "Up to date" is a positive/ready result, same as the Installed
            # status dot elsewhere on this page - use the same fixed green
            # rather than the theme's accent color so the two always match.
            color = OK_GREEN.name()
        elif status_kind == "warn":
            color = WARN.name()
        else:
            color = None
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        if color is not None:
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {color}; font-size: 18px;")
            dot.setFixedWidth(24)
            status_row.addWidget(dot)
        status_label = info_label(status_text)
        if color is not None:
            status_label.setStyleSheet(f"color: {color};")
        else:
            status_label.setObjectName(status_kind)
            status_label.style().unpolish(status_label)
            status_label.style().polish(status_label)
        status_row.addWidget(status_label, 1)
        if status is not None and status.update_available:
            goto_button = QPushButton(tr("Open updates"))
            goto_button.setObjectName("primary")
            goto_button.clicked.connect(lambda: self.window.set_page("update"))
            status_row.addWidget(goto_button)
        layout.addLayout(status_row)

    # ----- actions card -----
    def _build_actions(self) -> None:
        layout = self.actions_card.layout()
        clear_layout(layout)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(section_label(tr("Quick actions")))
        header.addStretch(1)
        # Deliberately not stored on self: _build_actions() runs on every
        # refresh() and clear_layout() above deletes whatever it made, so a
        # retained reference would go stale - the same trap _play_button
        # below has to null out explicitly. This button holds no state and
        # nothing outside this method touches it, so rebuilding is enough.
        # Icon plus a short label: the bare glyph alone, tucked in the card's
        # corner, was easy to miss and did not say what it opened.
        deck_button = QPushButton(" " + tr("Deck Mode"))
        deck_button.setObjectName("deckModeButton")
        deck_button.setIcon(
            deck_icon(QColor(active_theme_tokens().get("accent_strong", "#9fe96f")), 28)
        )
        deck_button.setIconSize(QSize(28, 28))
        deck_button.setFixedHeight(34)
        deck_button.setToolTip(tr("Switch to Steam Deck Mode"))
        deck_button.setCursor(Qt.CursorShape.PointingHandCursor)
        deck_button.clicked.connect(lambda: switch_mode(self.window, deck=True))
        header.addWidget(deck_button)
        layout.addLayout(header)
        profile = self.settings.active_profile
        # clear_layout() above just deleted the previous render's Play
        # button. Drop the reference to it before deciding whether a new one
        # is built: with no active profile there is no replacement, and
        # on_busy_changed()/_set_play_button_disabled() would otherwise
        # still be holding the deleted widget.
        self._play_button = None
        if profile is not None:
            play = QPushButton(tr("Play GAMMA"))
            play.setObjectName("primary")
            self._play_button = play
            install_hover_grow_text(play, "accent_text")
            play.clicked.connect(self._play_gamma)
            play.clicked.connect(play_click_sound)
            layout.addWidget(play)
            QTimer.singleShot(0, self._bind_play_state)
            grid = QGridLayout()
            grid.setSpacing(8)
            buttons: list[tuple[str, str]] = [
                (tr("Open Anomaly folder"), profile.anomaly),
                (tr("Open GAMMA folder"), profile.gamma),
                (tr("Open cache folder"), profile.cache),
                (tr("Open log folder"), "logs"),
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
        play_page = getattr(self.window, "_pages", {}).get("play")
        button = getattr(self, "_play_button", None)
        if play_page is None or button is None:
            return
        button.setEnabled(not play_page.is_launching and not self.window.install_busy)
        # Connect only once - reconnecting each refresh disconnects the stale
        # connection object, which PySide6 reports as "Failed to disconnect".
        if self._play_button_connection is None:
            self._play_button_connection = play_page.launch_state_changed.connect(
                self._set_play_button_disabled
            )
        if self._stats_connection is None:
            # A session that just ended most likely wrote a new save.
            self._stats_connection = play_page.launch_state_changed.connect(
                lambda launching: None if launching else self._start_stats_task(force=True)
            )

    def _set_play_button_disabled(self, disabled: bool) -> None:
        button = getattr(self, "_play_button", None)
        if button is not None:
            button.setDisabled(disabled)

    def on_busy_changed(self, busy: bool) -> None:
        """Mirror the Play page lock while installation work is active."""
        button = getattr(self, "_play_button", None)
        if button is None:
            return
        play_page = getattr(self.window, "_pages", {}).get("play")
        is_launching = play_page.is_launching if play_page is not None else False
        button.setEnabled(not busy and not is_launching)

    def on_install_activity_changed(self, operation: str | None) -> None:
        """Show active Anomaly/GAMMA installs in the dashboard status card."""
        if operation == "anomaly" and hasattr(self, "anomaly_status"):
            self.anomaly_status.set_installing(tr("Installing Anomaly..."))
        elif operation == "gamma" and hasattr(self, "gamma_status"):
            self.gamma_status.set_installing(tr("Installing GAMMA..."))
        elif operation is None and self.settings.active_profile is not None:
            # Bump the generation the same way refresh() does: _render_
            # install_status() below replaces self.winetricks_status with a
            # new widget, but _render_winetricks_status()/_on_winetricks_
            # error() only self-heal (re-render) a stale in-flight check
            # when they see a generation mismatch - without bumping it
            # here, a check started by an earlier refresh() that's still
            # running when an install finishes sees a matching generation
            # despite the widget having changed underneath it, so it just
            # no-ops instead of re-rendering, leaving the new widget stuck
            # on "Checking..." until the next real refresh().
            self._refresh_generation += 1
            self._render_install_status()

    def _play_gamma(self) -> None:
        # Play pages are built lazily by MainWindow, only on first visit to
        # the Play tab - use _ensure_page() (falling back to a plain lookup
        # for stub windows in tests) so clicking Play here builds it too,
        # rather than silently no-op'ing because it was never visited yet.
        ensure_page = getattr(self.window, "_ensure_page", None)
        play_page = (
            ensure_page("play")
            if ensure_page is not None
            else getattr(self.window, "_pages", {}).get("play")
        )
        # Reject duplicate dashboard clicks before delegating to the Play page.
        if self.window.install_busy or play_page is None or play_page.is_launching:
            return
        # A Play page built just now, by this click, has no listener yet -
        # _bind_play_state() only ran at the last refresh(), when it did not
        # exist - so the button stayed green through the launch until the
        # next tab switch. Connect before launching, then reflect the state.
        self._bind_play_state()
        play_page.launch_game()
        self._set_play_button_disabled(play_page.is_launching)

    def _open_folder(self, target: str) -> None:
        if not open_in_file_manager(target):
            self.window.statusBar().showMessage(
                f"Could not open folder: {target}", 6000
            )
