"""Installing GAMMA and the runtimes it needs.

The long, unattended job - a ~150GB download that a Deck will most likely
be left docked to finish. So this screen has to do two things well: say
clearly what state the install is in before it starts, and report progress
in a way that is readable from across a room.

The install options mirror the desktop Install page's own set (minus
auto-retry, which has no shared implementation to port yet): Minimal mode,
and Preserve user.ltx / MCM settings. The two preserve toggles default on,
matching what was previously hardcoded here, so a Deck user who has tuned
their controls and MCM settings keeps them unless they deliberately ask for
a clean slate.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QHBoxLayout

from commander_gui import gui_settings
from commander_gui.cli_runner import cli_command
from commander_gui.dependencies import check_all_dependencies
from commander_gui.gui_settings import configured_wine_prefix
from commander_gui.i18n import tr
from commander_gui.launcher import LaunchError, find_extra_protons
from commander_gui.parsers import parse_progress_line, strip_ansi
from commander_gui.proton_installer import fetch_ge_proton_releases, install_proton
from commander_gui.ui.common import (
    BackgroundTask,
    CommandRunner,
    aggregate_progress_value,
    anomaly_installed,
    free_space_bytes,
    gamma_installed,
    human_size,
    mo2_running,
)
from commander_gui.ui.install_page import _full_install_args
from commander_gui.winetricks import (
    WINETRICKS_VERBS,
    check_winetricks_full_status,
    protontricks_binary,
    protontricks_install_command,
    umu_install_command,
    winetricks_install_command,
)

from ..widgets import (
    DeckCard,
    DeckOverlay,
    DeckPicker,
    DeckProgress,
    DeckRow,
    DeckStatusRow,
    DeckToggleRow,
    deck_button,
    deck_label,
    deck_step_badge,
    deck_two_column_card,
)
from .base import DeckScreen


class _ProtonProgressBridge(QObject):
    """Cross-thread signal bridge for GE-Proton download progress.

    install_proton()'s progress_cb runs on the BackgroundTask's worker
    thread; PySide only auto-queues a cross-thread signal onto the main
    thread for a QObject-bound slot, so this exists for the same reason
    desktop's play_page.py has its own _ProgressBridge - a lambda would run
    on the worker thread instead, touching Deck widgets unsafely.
    """

    updated = Signal(int, str)


def _step_line(number: int, text: str) -> QHBoxLayout:
    """A numbered-badge + wrapped label row, in front of a step's status row."""
    line = QHBoxLayout()
    line.setSpacing(12)
    line.addWidget(deck_step_badge(number), 0)
    label = deck_label(text, role="body", wrap=True)
    line.addWidget(label, 1)
    return line

#: Rough on-disk requirement, used only to warn before starting - the CLI
#: does the real accounting.
_FULL_INSTALL_BYTES = 150 * 1024**3
_MINIMAL_INSTALL_BYTES = 100 * 1024**3
_ANOMALY_INSTALL_BYTES = 20 * 1024**3

#: Where each dependency stage sits on the overall bar. The stages emit no
#: parsable progress of their own, so the bar advances a step at a time
#: rather than pretending to a precision it does not have - but it does
#: advance, which an indeterminate spinner over a ten-minute Winetricks run
#: does not.
_STAGE_START = {"umu": 0, "tools": 20, "verbs": 40}


class InstallScreen(DeckScreen):
    def build(self) -> None:
        self._runner: CommandRunner | None = None
        self._deps_task: BackgroundTask | None = None
        self._deps_detail_task: BackgroundTask | None = None
        self._stage: str | None = None
        self._heavy: dict[str, float] = {}
        self._anomaly_installed = False
        self._gamma_installed = False
        self._proton_releases: list[dict] = []
        self._proton_releases_task: BackgroundTask | None = None
        self._proton_task: BackgroundTask | None = None
        self._proton_version: str = ""
        self._proton_bridge: _ProtonProgressBridge | None = None

        # Each step's own action button lives in its step's card, matching
        # the desktop Install page's Anomaly/GAMMA columns - a step counter
        # next to a button that does something else entirely (at the bottom
        # of the page) made the numbering pointless.
        ag_card, anomaly_col, gamma_col = deck_two_column_card()
        anomaly_col.addLayout(
            _step_line(1, tr("Install STALKER Anomaly."))
        )
        self.anomaly_row = DeckStatusRow(tr("STALKER Anomaly"))
        anomaly_col.addWidget(self.anomaly_row)
        self.anomaly_button = deck_button(
            tr("Install Anomaly"), on_click=self._confirm_anomaly_install
        )
        anomaly_col.addWidget(self.anomaly_button)

        gamma_col.addLayout(
            _step_line(2, tr("Install the GAMMA modpack."))
        )
        self.gamma_row = DeckStatusRow(tr("GAMMA Modpack"))
        gamma_col.addWidget(self.gamma_row)
        self.install_button = deck_button(
            tr("Install GAMMA"), role="primary", on_click=self._confirm_install
        )
        gamma_col.addWidget(self.install_button)
        self.body.addWidget(ag_card)

        # Step 3 gets the same card treatment as Steps 1/2 above, rather than
        # sitting bare in self.body - the three steps read as one sequence.
        deps_card = DeckCard()
        deps_card.body.addLayout(
            _step_line(3, tr("Install required dependencies."))
        )
        self.deps_row = DeckStatusRow(tr("Dependencies"))
        deps_card.body.addWidget(self.deps_row)
        self.deps_detail = deck_label("", role="caption", wrap=True)
        deps_card.body.addWidget(self.deps_detail)
        self.deps_button = deck_button(
            tr("Install Dependencies"), on_click=self._confirm_dependencies
        )
        deps_card.body.addWidget(self.deps_button)
        self.body.addWidget(deps_card)

        # Ported from the desktop Play page's GE-Proton downloader
        # (commander_gui/proton_installer.py) - installing a build is
        # independent of which runner is currently selected, so it lives
        # here as its own tool rather than folded into a runner picker.
        proton_card = DeckCard()
        proton_card.body.addWidget(
            deck_label(tr("Compatibility tool"), role="rowTitle")
        )
        self.proton_row = DeckRow(tr("GE-Proton version"))
        self.proton_row.activated.connect(self._pick_proton_version)
        proton_card.body.addWidget(self.proton_row)
        self.proton_install_button = deck_button(
            tr("Install GE-Proton"), on_click=self._install_proton
        )
        self.proton_install_button.setEnabled(False)
        proton_card.body.addWidget(self.proton_install_button)
        self.proton_status = deck_label("", role="caption", wrap=True)
        proton_card.body.addWidget(self.proton_status)
        self.body.addWidget(proton_card)

        self.body.addWidget(deck_label(tr("Install options"), role="rowTitle"))
        options_card = DeckCard()
        self.minimal_row = DeckToggleRow(tr("Minimal (~100 GB)"), False)
        self.preserve_user_row = DeckToggleRow(tr("Preserve user.ltx settings"), True)
        self.preserve_mcm_row = DeckToggleRow(tr("Preserve MCM settings"), True)
        for row in (self.minimal_row, self.preserve_user_row, self.preserve_mcm_row):
            options_card.body.addWidget(row)
        self.body.addWidget(options_card)

        self.progress = DeckProgress()
        self.progress.hide()
        self.body.addWidget(self.progress)

        self.note = deck_label("", role="caption", wrap=True)
        self.body.addWidget(self.note)
        self.body.addStretch(1)

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        # Independent of the active profile - a compatibility tool install
        # is a system-wide action, not something tied to one GAMMA install.
        self._update_proton_button()

        profile = self.profile()
        if profile is None:
            for row in (self.anomaly_row, self.gamma_row, self.deps_row):
                row.set_status(tr("No Profile"), "warn")
            self.deps_detail.setText("")
            self.anomaly_button.setEnabled(False)
            self.install_button.setEnabled(False)
            self.deps_button.setEnabled(False)
            self.note.setText(
                tr("Create or activate a profile first (Profiles page).")
            )
            return

        self._anomaly_installed = anomaly_installed(profile.anomaly)
        self._gamma_installed = gamma_installed(profile.gamma, profile.mo2_profile)
        self.anomaly_row.set_status(
            tr("Installed") if self._anomaly_installed else tr("Not installed"),
            "ok" if self._anomaly_installed else "bad",
        )
        self.gamma_row.set_status(
            tr("Installed") if self._gamma_installed else tr("Not installed"),
            "ok" if self._gamma_installed else "bad",
        )
        self._update_buttons()
        self._start_dependency_check()
        self._start_dependency_detail_check()
        self.note.setText("")

    def on_busy_changed(self, busy: bool) -> None:
        self._update_buttons()

    def _update_buttons(self) -> None:
        idle = not self.window.install_busy and self._runner is None
        has_profile = self.profile() is not None
        # Disabled once installed, matching desktop's own anomaly_button -
        # there's nothing a second "Install Anomaly" click should do.
        self.anomaly_button.setEnabled(
            idle and has_profile and not self._anomaly_installed
        )
        self.install_button.setEnabled(idle and has_profile)
        self.deps_button.setEnabled(idle and has_profile)
        for row in (self.minimal_row, self.preserve_user_row, self.preserve_mcm_row):
            row.setEnabled(idle)
        self._update_proton_button()

    # -- dependency status ------------------------------------------------
    def _start_dependency_check(self) -> None:
        if self._deps_task is not None:
            return
        self.deps_row.set_status(tr("Checking..."), "warn")
        # Off-thread on purpose. The desktop page calls this synchronously,
        # which is tolerable on a desktop and a visible stall on the Deck -
        # it shells out to several package managers and binaries.
        self._deps_task = BackgroundTask(check_all_dependencies, parent=self)
        self._deps_task.result.connect(self._on_dependencies_checked)
        self._deps_task.error.connect(lambda _msg: self._clear_deps_task())
        self._deps_task.start()

    def _clear_deps_task(self) -> None:
        self._deps_task = None
        self.deps_row.set_status(tr("Unknown"), "warn")

    def _on_dependencies_checked(self, missing: object) -> None:
        self._deps_task = None
        names = list(missing or [])
        if names:
            self.deps_row.set_status(
                tr("{count} missing", count=len(names)), "bad"
            )
            self.deps_row.setToolTip(", ".join(str(n) for n in names))
        else:
            self.deps_row.set_status(tr("Ready"), "ok")
            self.deps_row.setToolTip("")

    def _start_dependency_detail_check(self) -> None:
        if self._deps_detail_task is not None:
            return
        self._deps_detail_task = BackgroundTask(
            check_winetricks_full_status, configured_wine_prefix(), parent=self
        )
        self._deps_detail_task.result.connect(self._on_dependency_detail)
        self._deps_detail_task.error.connect(
            lambda _msg: self._clear_deps_detail_task()
        )
        self._deps_detail_task.start()

    def _clear_deps_detail_task(self) -> None:
        self._deps_detail_task = None

    def _on_dependency_detail(self, status: object) -> None:
        self._deps_detail_task = None
        status = status or {}
        installed = sum(1 for ok in status.values() if ok)
        total = len(status)
        self.deps_detail.setText(
            tr(
                "{installed}/{total} dependencies installed",
                installed=installed,
                total=total,
            )
        )

    # -- Anomaly install ----------------------------------------------------
    def _confirm_anomaly_install(self) -> None:
        """Just Anomaly, via the CLI's own ``anomaly install`` verb.

        Separate from GAMMA's ``full-install`` (which already installs
        Anomaly first if it's missing) - this is for a user who wants
        Anomaly down ahead of time, matching desktop's own Install Anomaly
        button.
        """
        profile = self.profile()
        if profile is None or self.window.install_busy:
            return
        if mo2_running():
            self.window.notify(tr("Mod Organizer is running"))
            return

        message = tr("Download and install STALKER Anomaly?")
        free = free_space_bytes(profile.anomaly or profile.gamma)
        if free is not None and free < _ANOMALY_INSTALL_BYTES:
            message += "\n\n" + tr(
                "Only {free} free on that drive.", free=human_size(free)
            )
        self.window.confirm(
            tr("Install Anomaly"),
            message,
            self._start_anomaly_install,
            confirm_text=tr("Install"),
        )

    def _start_anomaly_install(self) -> None:
        profile = self.profile()
        if profile is None:
            return
        if profile.anomaly:
            try:
                Path(profile.anomaly).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.window.notify(str(exc), 6000)
                return
        self._begin_run(
            cli_command(["anomaly", "install"], progress_interval_ms=200),
            operation="anomaly",
            status=tr("Installing Anomaly..."),
        )

    # -- GAMMA install ----------------------------------------------------
    def _confirm_install(self) -> None:
        profile = self.profile()
        if profile is None or self.window.install_busy:
            return
        if mo2_running():
            self.window.notify(tr("Mod Organizer is running"))
            return

        minimal = self.minimal_row.is_checked()
        needed = _MINIMAL_INSTALL_BYTES if minimal else _FULL_INSTALL_BYTES
        message = tr(
            "This installs Anomaly (if missing) and every GAMMA addon. "
            "Expect about {size} of downloads.",
            size=human_size(needed),
        )
        free = free_space_bytes(profile.gamma or profile.anomaly)
        if free is not None and free < needed:
            message += "\n\n" + tr(
                "Only {free} free on that drive.", free=human_size(free)
            )
        self.window.confirm(
            tr("Install GAMMA"),
            message,
            self._start_install,
            confirm_text=tr("Install"),
        )

    def _start_install(self) -> None:
        profile = self.profile()
        if profile is None:
            return
        for path in (profile.anomaly, profile.gamma, profile.cache):
            if not path:
                continue
            try:
                Path(path).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.window.notify(str(exc), 6000)
                return

        # Both must genuinely exist before trusting the cache to skip
        # re-extraction. Checking Anomaly alone is wrong straight after a
        # GAMMA-only reset, which empties gamma/mods but keeps the download
        # cache: every cached, hash-valid archive would then be skipped
        # instead of re-extracted, permanently losing those mods. The
        # desktop page's guard, reproduced exactly.
        skip_extract = anomaly_installed(profile.anomaly) and gamma_installed(
            profile.gamma, profile.mo2_profile
        )
        args = _full_install_args(
            self.minimal_row.is_checked(),
            self.preserve_user_row.is_checked(),
            self.preserve_mcm_row.is_checked(),
            skip_extract,
        )
        self._heavy = {}
        self._begin_run(
            cli_command(args, progress_interval_ms=200),
            operation="gamma",
            status=tr("Installing GAMMA..."),
        )

    def start_auto_install(
        self,
        *,
        include_anomaly: bool = True,
        preserve_user: bool = False,
        preserve_mcm: bool = False,
    ) -> bool:
        """Kick off a reinstall right after Utilities has wiped the folders.

        Called by the Utilities screen's Fresh/GAMMA Reset, after the old
        folders are already gone. Reuses this screen's own single-
        CommandRunner full-install flow - this screen never had desktop's
        separate Anomaly-then-verify staged pipeline to begin with, so this
        is not a port of desktop's own ``start_auto_install``, just a
        same-named entry point into the simpler flow Deck already has.
        Always forces Minimal off, matching desktop's own auto-chained
        install (it does not respect the minimal option either).
        """
        if self._runner is not None or self.profile() is None:
            return False
        self.minimal_row.set_checked(False)
        self.preserve_user_row.set_checked(preserve_user)
        self.preserve_mcm_row.set_checked(preserve_mcm)
        self._start_install()
        return True

    # -- dependencies -----------------------------------------------------
    def _confirm_dependencies(self) -> None:
        if self.window.install_busy:
            return
        if mo2_running():
            self.window.notify(tr("Mod Organizer is running"))
            return
        self.window.confirm(
            tr("Install Dependencies"),
            tr(
                "Installs umu-run, protontricks and the Visual C++ / DirectX "
                "runtimes Mod Organizer and the game need:\n\n{verbs}",
                verbs=" ".join(WINETRICKS_VERBS),
            ),
            self._start_dependencies,
            confirm_text=tr("Install"),
        )

    def _start_dependencies(self) -> None:
        self._stage = "umu"
        self._run_stage()

    def _run_stage(self) -> None:
        from commander_gui.gui_settings import configured_runner

        stage = self._stage
        # curl / pipx stages touch no Wine and get no Wine env; the verbs
        # stage gets exactly the env winetricks_install_command() decides.
        env: dict[str, str] | None = None
        if stage == "umu":
            command = umu_install_command()
            failure = tr("umu-run could not be installed (curl is not available).")
            status = tr("Installing umu-run...")
        elif stage == "tools":
            command = protontricks_install_command()
            failure = tr("protontricks could not be installed.")
            status = tr("Installing protontricks...")
        else:
            # Through the game's own runner - bare winetricks would run the
            # host's wine inside the Proton prefix and corrupt it.
            try:
                command, env = winetricks_install_command(configured_runner())
                failure = tr("Winetricks is not available.")
            except LaunchError as exc:
                command, failure = [], str(exc)
            status = tr("Installing runtimes...")
        if not command:
            self._finish_run()
            self.window.notify(failure, 8000)
            return
        self._begin_run(
            command,
            operation="dependencies",
            status=status,
            env=env,
        )
        self.progress.set_percent(_STAGE_START.get(stage, 0))

    # -- shared runner plumbing -------------------------------------------
    def _begin_run(
        self,
        command: list[str],
        *,
        operation: str,
        status: str,
        env: dict[str, str] | None = None,
    ) -> None:
        self.window.set_install_busy(True, operation)
        self.progress.reset()
        self.progress.show()
        self.anomaly_button.hide()
        self.install_button.hide()
        self.deps_button.hide()
        self._runner = CommandRunner(command, env=env, parent=self)
        self._runner.line.connect(self._on_line)
        self._runner.finished.connect(self._on_finished)
        self._runner.cancelled.connect(self.progress.on_cancelled)
        self.progress.set_runner(self._runner)
        self.progress.on_started()
        self.progress.status_message(status)
        self._runner.start()

    def _on_line(self, line: str) -> None:
        self.progress.on_line(line)
        if self._stage is not None:
            # Dependency stages emit no parsable progress; the bar is moved
            # by _run_stage() as each stage begins.
            return
        event = parse_progress_line(strip_ansi(line))
        if event is None:
            return
        self.progress.set_percent(
            aggregate_progress_value(
                event.complete,
                event.total,
                event.percent,
                event.name,
                self._heavy,
                event.operation,
            )
        )
        self.progress.status_message(f"{event.operation}  {event.name}")

    def _on_finished(self, rc: int, output: str) -> None:
        cancelled = self._runner is not None and self._runner.was_cancelled
        self.progress.on_finished(rc, output)
        self._runner = None

        # Dependency stages chain: umu -> protontricks (unless already
        # present) -> the Winetricks verbs.
        if self._stage is not None and rc == 0 and not cancelled:
            if self._stage == "umu":
                self._stage = "verbs" if protontricks_binary() else "tools"
                self.window.set_install_busy(False)
                self._run_stage()
                return
            if self._stage == "tools":
                self._stage = "verbs"
                self.window.set_install_busy(False)
                self._run_stage()
                return

        self._finish_run()
        if cancelled:
            self.window.notify(tr("Cancelled"))
        elif rc == 0:
            self.window.notify(tr("Done"))
        else:
            self.window.notify(tr("Failed") + f" (exit {rc})", 8000)

    def _finish_run(self) -> None:
        self._stage = None
        self._runner = None
        self.window.set_install_busy(False)
        self.progress.hide()
        self.anomaly_button.show()
        self.install_button.show()
        self.deps_button.show()
        self.refresh()

    def on_back(self) -> bool:
        # A running install must not be abandoned by a stray B press; the
        # Cancel button on the progress view is the deliberate way out.
        if self._runner is not None or self._proton_task is not None:
            self.window.notify(tr("An install is already running."))
            return True
        return False

    # -- GE-Proton ----------------------------------------------------------
    def _update_proton_button(self) -> None:
        idle = not self.window.install_busy and self._proton_task is None
        version = self._proton_version
        if not version:
            self.proton_install_button.setText(tr("Install GE-Proton"))
            self.proton_install_button.setEnabled(False)
            return
        installed = {label for label, _path in find_extra_protons()}
        if any(version in label for label in installed):
            self.proton_install_button.setText(tr("GE-Proton installed") + " ✓")
            self.proton_install_button.setEnabled(False)
        else:
            self.proton_install_button.setText(tr("Install {version}", version=version))
            self.proton_install_button.setEnabled(idle)

    def _pick_proton_version(self) -> None:
        if self._proton_releases_task is not None:
            return
        if self._proton_releases:
            self._show_proton_picker()
            return
        # Fetched on demand, not eagerly on screen build: this is a GitHub
        # API call, and most visits to this screen never touch it.
        self.window.notify(tr("Fetching GE-Proton releases..."), 0)
        self._proton_releases_task = BackgroundTask(
            fetch_ge_proton_releases, count=100, parent=self
        )
        self._proton_releases_task.result.connect(self._on_proton_releases_fetched)
        self._proton_releases_task.error.connect(self._on_proton_releases_error)
        self._proton_releases_task.start()

    def _on_proton_releases_fetched(self, releases: object) -> None:
        self._proton_releases_task = None
        self.window.toast.hide()
        self._proton_releases = list(releases or [])
        if not self._proton_releases:
            self.window.notify(tr("No GE-Proton releases found."), 4000)
            return
        self._show_proton_picker()

    def _on_proton_releases_error(self, message: str) -> None:
        self._proton_releases_task = None
        self.window.notify(tr("Failed") + ": " + message, 6000)

    def _show_proton_picker(self) -> None:
        picker = DeckPicker(
            "",
            [(rel["tag"], rel["tag"]) for rel in self._proton_releases],
            self._proton_version,
        )
        picker.chosen.connect(self._apply_proton_version)
        self.window.show_overlay(
            DeckOverlay(
                tr("GE-Proton version"),
                picker,
                [(tr("Cancel"), self.window.dismiss_overlay, "normal")],
                panel_width=1000,
            )
        )

    def _apply_proton_version(self, tag: object) -> None:
        self.window.dismiss_overlay()
        self._proton_version = str(tag)
        self.proton_row.set_value(self._proton_version)
        self._update_proton_button()

    def _install_proton(self) -> None:
        version = self._proton_version
        if not version or self.window.install_busy or self._proton_task is not None:
            return
        installed = {label for label, _path in find_extra_protons()}
        if any(version in label for label in installed):
            return
        self.window.confirm(
            tr("Install GE-Proton"),
            tr(
                "Downloads and installs {version} as a Steam compatibility "
                "tool.",
                version=version,
            ),
            lambda: self._start_proton_install(version),
            confirm_text=tr("Install"),
        )

    def _start_proton_install(self, version: str) -> None:
        # Same install_dir computation as the desktop Play page's
        # _install_proton().
        overrides = gui_settings.load_gui_settings().get("tool_overrides") or {}
        steam_root = overrides.get("steam_root", "")
        install_dir = (
            Path(steam_root) if steam_root else Path.home() / ".local" / "share" / "Steam"
        ) / "compatibilitytools.d"

        self.window.set_install_busy(True, "proton")
        self.proton_install_button.setEnabled(False)
        self.proton_install_button.setText(tr("Installing..."))
        self.proton_status.setText(tr("Preparing download..."))
        self.progress.reset()
        self.progress.show()
        self.anomaly_button.hide()
        self.install_button.hide()
        self.deps_button.hide()

        old_bridge = self._proton_bridge
        if old_bridge is not None:
            old_bridge.deleteLater()
        bridge = _ProtonProgressBridge(parent=self)
        self._proton_bridge = bridge
        bridge.updated.connect(self._on_proton_progress)

        def _progress(downloaded: int, total: int) -> None:
            if total > 0:
                pct = int(downloaded * 100 / total)
                text = tr(
                    "Downloading {version}... {done}/{total}",
                    version=version,
                    done=human_size(downloaded),
                    total=human_size(total),
                )
                bridge.updated.emit(pct, text)

        def _work() -> Path:
            return install_proton(
                version, install_dir, progress_cb=_progress,
                cancel_event=task.cancel_event,
            )

        task = BackgroundTask(_work, parent=self)
        self._proton_task = task
        task.result.connect(self._on_proton_done)
        task.error.connect(self._on_proton_failed)
        self.progress.set_cancellable(task.cancel)
        task.start()

    def _on_proton_progress(self, percent: int, text: str) -> None:
        self.progress.set_percent(percent)
        self.progress.status_message(text)

    def _on_proton_done(self, _result: object) -> None:
        self._proton_task = None
        self.window.set_install_busy(False)
        self.progress.set_percent(100)
        self.progress.hide()
        self.anomaly_button.show()
        self.install_button.show()
        self.deps_button.show()
        self.proton_status.setText(
            tr("Installed {version}", version=self._proton_version) + " ✓"
        )
        self._update_proton_button()

    def _on_proton_failed(self, message: str) -> None:
        self._proton_task = None
        self.window.set_install_busy(False)
        self.progress.hide()
        self.anomaly_button.show()
        self.install_button.show()
        self.deps_button.show()
        if message == "Download cancelled":
            self.proton_status.setText(tr("Cancelled"))
        else:
            self.proton_status.setText(tr("Failed") + ": " + message)
        self._update_proton_button()
