"""Dashboard: active profile overview, install status, storage, quick actions."""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..cli_runner import run_sync
from ..gui_settings import configured_wine_prefix, load_gui_settings, save_gui_settings
from ..launcher import find_extra_protons
from ..settings import CliSettings
from ..updates import UpdateStatus, check_updates, format_version, status_summary
from ..winetricks import WINETRICKS_VERBS, check_winetricks_full_status
from .common import (
    OK_GREEN,
    BackgroundTask,
    InstallStatusRow,
    NoWheelComboBox,
    activate_profile,
    anomaly_installed,
    clear_layout,
    dir_size,
    display_state,
    format_last_played,
    format_playtime,
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
from .mod_manager_page import _QUERY_TIMEOUT, _query_mo2_profiles

#: Shared fixed width for every flat value combo on the Profile overview
#: card (Profile, MO2 profile, Current runner, Download threads) - each
#: sits at the end of its own row, so a shared width is what makes their
#: text actually line up into one column instead of each combo just
#: hugging its own (differently sized) content.
_VALUE_COMBO_WIDTH = 260


class _FlatValueCombo(NoWheelComboBox):
    """A read-only combo whose current value sits right-aligned, flush

    against the dropdown arrow, instead of the default left-aligned combo
    label - matches how the plain read-only value rows around it
    (Total playtime, Last played, ...) are right-aligned too, and keeps
    the text close to the arrow that opens it rather than floating off to
    the left of a wide, mostly-empty box.

    Achieved by making the combo editable with a read-only QLineEdit
    (the only way to get right-aligned text out of a QComboBox) - which,
    as a side effect, stops a click on the text itself from opening the
    popup (only the arrow would). The event filter below restores that.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        line_edit = self.lineEdit()
        line_edit.setReadOnly(True)
        line_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        line_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        line_edit.installEventFilter(self)

    _CLICK_EVENTS = (
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick,
    )

    def eventFilter(self, obj, event):
        if obj is self.lineEdit() and event.type() in self._CLICK_EVENTS:
            # Open on release, not press: calling showPopup() from within
            # the press handler starts the popup's own mouse grab while
            # this same click is still in progress, so Qt reads the click's
            # own release (landing back on the line edit, outside the
            # popup's list) as the native combo box's press-drag-release
            # "select on release" gesture cancelling with nothing picked -
            # closing the popup the instant it opened. Waiting for release
            # to open it means there is no in-flight grab for that release
            # to cancel; press is still swallowed so the line edit itself
            # never reacts to it (cursor placement, focus selection, ...).
            if event.type() == QEvent.Type.MouseButtonRelease:
                self.showPopup()
            return True
        return super().eventFilter(obj, event)


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

        self.install_status_card, _ = make_card(expand=True)
        root.addWidget(self.install_status_card)

        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        root.addLayout(bottom)
        self.updates_card, _ = make_card(expand=True)
        bottom.addWidget(self.updates_card, 1)
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

    # ----- install status card -----
    def _render_install_status(self) -> None:
        layout = self.install_status_card.layout()
        clear_layout(layout)
        layout.addWidget(section_label(tr("Installation status")))
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
        layout.addWidget(section_label(tr("Profile overview")))

        # Profile row is a live switcher (not a static label) when more
        # than one profile exists - Anomaly/GAMMA/Cache folder paths were
        # removed from this card entirely, since they're already shown as
        # the detail text under "STALKER Anomaly"/"GAMMA Modpack" in the
        # Installation status card above.
        profile_row = QHBoxLayout()
        profile_key = QLabel(tr("Profile"))
        profile_key.setObjectName("dim")
        profile_row.addWidget(profile_key)
        profile_row.addStretch(1)
        profile_combo = _FlatValueCombo()
        profile_combo.setObjectName("flatValueCombo")
        profile_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        profile_combo.setFixedWidth(_VALUE_COMBO_WIDTH)
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
        profile_row.addWidget(profile_combo)
        layout.addLayout(profile_row)

        # MO2 profile is a live switcher too, same as Profile above -
        # populated for real by _start_mo2_profiles_task() once its
        # background query returns; starts out showing just the
        # configured profile so there is never a blank/empty combo.
        mo2_row = QHBoxLayout()
        mo2_key = QLabel(tr("MO2 profile"))
        mo2_key.setObjectName("dim")
        mo2_row.addWidget(mo2_key)
        mo2_row.addStretch(1)
        self.mo2_profile_combo = _FlatValueCombo()
        self.mo2_profile_combo.setObjectName("flatValueCombo")
        self.mo2_profile_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mo2_profile_combo.setFixedWidth(_VALUE_COMBO_WIDTH)
        self.mo2_profile_combo.addItem(profile.mo2_profile, profile.mo2_profile)
        self.mo2_profile_combo.currentIndexChanged.connect(
            lambda _index, combo=self.mo2_profile_combo: self._on_mo2_profile_switch(
                combo
            )
        )
        mo2_row.addWidget(self.mo2_profile_combo)
        layout.addLayout(mo2_row)

        # Current runner is a live switcher too - same combo (Auto-detect +
        # every installed GE-Proton version) and the same gui_settings
        # "runner" key as the Play page's own runner selector, which is
        # where this value actually comes from/is normally changed.
        runner_row = QHBoxLayout()
        runner_key = QLabel(tr("Current runner"))
        runner_key.setObjectName("dim")
        runner_row.addWidget(runner_key)
        runner_row.addStretch(1)
        self.runner_combo = _FlatValueCombo()
        self.runner_combo.setObjectName("flatValueCombo")
        self.runner_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.runner_combo.setFixedWidth(_VALUE_COMBO_WIDTH)
        self._populate_runner_combo(self.runner_combo)
        self.runner_combo.currentIndexChanged.connect(
            lambda _index, combo=self.runner_combo: self._on_runner_switch(combo)
        )
        runner_row.addWidget(self.runner_combo)
        layout.addLayout(runner_row)

        # Download threads is a live switcher too - a fixed 3-option
        # choice (rather than the Profiles page's free-form 1-20 spin box)
        # since this is meant as a quick, low-friction "just pick a speed"
        # control, not the full range editing already available there.
        threads_row = QHBoxLayout()
        threads_key = QLabel(tr("Download threads"))
        threads_key.setObjectName("dim")
        threads_row.addWidget(threads_key)
        threads_row.addStretch(1)
        self.download_threads_combo = _FlatValueCombo()
        self.download_threads_combo.setObjectName("flatValueCombo")
        self.download_threads_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_threads_combo.setFixedWidth(_VALUE_COMBO_WIDTH)
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
        threads_row.addWidget(self.download_threads_combo)
        layout.addLayout(threads_row)

        playtime_seconds = load_gui_settings().get("playtime_seconds", {}).get(
            profile.profile_name, 0.0
        )
        last_played_ts = load_gui_settings().get("last_played_ts", {}).get(
            profile.profile_name
        )
        for label, value in [
            ("Total playtime", format_playtime(playtime_seconds)),
            ("Last played", format_last_played(last_played_ts)),
        ]:
            row = QHBoxLayout()
            key = QLabel(tr(label))
            key.setObjectName("dim")
            val = QLabel(value)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(key)
            row.addStretch(1)
            row.addWidget(val)
            layout.addLayout(row)

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
        if active is not None and mo2_running():
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
        layout = self.sizes_card.layout()
        clear_layout(layout)
        layout.addWidget(section_label(tr("Storage usage")))
        total = sum(sizes.values())
        for key, value in sizes.items():
            bar_label = QLabel(tr("{key}: {arg}", key=key, arg=human_size(value)))
            layout.addWidget(bar_label)
        total_label = QLabel(tr("Total: {arg}", arg=human_size(total)))
        total_label.setStyleSheet(f"color: {OK_GREEN.name()};")
        layout.addWidget(total_label)
        if unavailable is not None:
            status_label = info_label(tr("Storage usage unavailable: {unavailable}", unavailable=unavailable))
            status_label.setObjectName("warn")
            layout.addWidget(status_label)

    def _on_size_error(self, message: str, generation: int) -> None:
        self._size_task = None
        if generation != self._refresh_generation:
            self._start_size_task()
            return
        self._render_sizes(self._sizes, generation, unavailable=message)

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
        layout.addWidget(section_label(tr("Updates")))

        if status is not None and status.installed is not None:
            grid = QGridLayout()
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(6)
            grid.addWidget(info_label(tr("Installed GAMMA version:")), 0, 0)
            installed_value = QLabel(
                format_version(status.installed, status.installed_human)
            )
            grid.addWidget(installed_value, 0, 1)
            grid.addWidget(info_label(tr("Latest GAMMA version:")), 1, 0)
            latest_value = QLabel(
                format_version(status.latest, status.latest_human, missing="-")
            )
            grid.addWidget(latest_value, 1, 1)
            grid.setColumnStretch(2, 1)
            layout.addLayout(grid)

        status_label = info_label(status_text)
        if status_kind == "accent":
            # "Up to date" is a positive/ready result, same as the Installed
            # status dot elsewhere on this page - use the same fixed green
            # rather than the theme's accent color so the two always match.
            status_label.setStyleSheet(f"color: {OK_GREEN.name()};")
        else:
            status_label.setObjectName(status_kind)
            status_label.style().unpolish(status_label)
            status_label.style().polish(status_label)
        layout.addWidget(status_label)

        row = QHBoxLayout()
        check_button = QPushButton(tr("Check for updates"))
        check_button.setObjectName("primary")
        check_button.setEnabled(
            not self._update_checking
            and not self.window.install_busy
            and self.settings.active_profile is not None
        )
        check_button.clicked.connect(self._start_update_check)
        row.addWidget(check_button)
        if status is not None and status.update_available:
            goto_button = QPushButton(tr("Open updates"))
            goto_button.clicked.connect(lambda: self.window.set_page("update"))
            row.addWidget(goto_button)
        row.addStretch(1)
        layout.addLayout(row)

    # ----- actions card -----
    def _build_actions(self) -> None:
        layout = self.actions_card.layout()
        clear_layout(layout)
        layout.addWidget(section_label(tr("Quick actions")))
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
        play_page = self.window._pages.get("play")
        # Reject duplicate dashboard clicks before delegating to the Play page.
        if self.window.install_busy or play_page is None or play_page.is_launching:
            return
        play_page.launch_game()

    def _open_folder(self, target: str) -> None:
        if not open_in_file_manager(target):
            self.window.statusBar().showMessage(
                f"Could not open folder: {target}", 6000
            )
