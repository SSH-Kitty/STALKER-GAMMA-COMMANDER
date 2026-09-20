"""Installing GAMMA and the runtimes it needs.

The long, unattended job - a ~150GB download that a Deck will most likely
be left docked to finish. So this screen has to do two things well: say
clearly what state the install is in before it starts, and report progress
in a way that is readable from across a room.

The choices the desktop Install page exposes are reduced to one. Minimal
mode stays, because it is the difference between 150GB and 100GB on a
device where storage is the scarce resource. The preserve-settings options
do not: they are forced on. A Deck user who has tuned their controls and MCM
settings has invested real effort that a reinstall would silently discard,
and the full interface is still there for anyone who genuinely wants a
clean slate.
"""

from __future__ import annotations

from pathlib import Path

from commander_gui.cli_runner import cli_command
from commander_gui.dependencies import check_all_dependencies
from commander_gui.i18n import tr
from commander_gui.launcher import LaunchError
from commander_gui.parsers import parse_progress_line, strip_ansi
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
    protontricks_binary,
    protontricks_install_command,
    umu_install_command,
    winetricks_install_command,
)

from ..widgets import (
    PRIMARY_H,
    DeckProgress,
    DeckStatusRow,
    DeckToggleRow,
    deck_button,
    deck_label,
)
from .base import DeckScreen

#: Rough on-disk requirement, used only to warn before starting - the CLI
#: does the real accounting.
_FULL_INSTALL_BYTES = 150 * 1024**3
_MINIMAL_INSTALL_BYTES = 100 * 1024**3

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
        self._stage: str | None = None
        self._heavy: dict[str, float] = {}

        self.anomaly_row = DeckStatusRow(tr("STALKER Anomaly"))
        self.gamma_row = DeckStatusRow(tr("GAMMA Modpack"))
        self.deps_row = DeckStatusRow(tr("Dependencies"))
        for row in (self.anomaly_row, self.gamma_row, self.deps_row):
            self.body.addWidget(row)

        self.minimal_row = DeckToggleRow(tr("Minimal (~100 GB)"), False)
        self.body.addWidget(self.minimal_row)

        self.install_button = deck_button(
            tr("Install GAMMA"), role="primary", on_click=self._confirm_install
        )
        self.body.addWidget(self.install_button)

        # Deliberately not "primary": two full-width green buttons stacked
        # read as equally weighted, and installing GAMMA is the action this
        # screen exists for.
        self.deps_button = deck_button(
            tr("Install Dependencies"),
            on_click=self._confirm_dependencies,
        )
        self.deps_button.setMinimumHeight(PRIMARY_H)
        self.body.addWidget(self.deps_button)

        self.progress = DeckProgress()
        self.progress.hide()
        self.body.addWidget(self.progress)

        self.note = deck_label("", role="caption", wrap=True)
        self.body.addWidget(self.note)
        self.body.addStretch(1)

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        profile = self.profile()
        if profile is None:
            for row in (self.anomaly_row, self.gamma_row, self.deps_row):
                row.set_status(tr("No Profile"), "warn")
            self.install_button.setEnabled(False)
            self.deps_button.setEnabled(False)
            self.note.setText(
                tr("Create or activate a profile first (Profiles page).")
            )
            return

        anomaly = anomaly_installed(profile.anomaly)
        gamma = gamma_installed(profile.gamma, profile.mo2_profile)
        self.anomaly_row.set_status(
            tr("Installed") if anomaly else tr("Not installed"),
            "ok" if anomaly else "bad",
        )
        self.gamma_row.set_status(
            tr("Installed") if gamma else tr("Not installed"),
            "ok" if gamma else "bad",
        )
        self._update_buttons()
        self._start_dependency_check()
        self.note.setText(
            tr(
                "Your user.ltx (keybindings, controls) and MCM settings are "
                "always preserved in Deck Mode."
            )
        )

    def on_busy_changed(self, busy: bool) -> None:
        self._update_buttons()

    def _update_buttons(self) -> None:
        idle = not self.window.install_busy and self._runner is None
        has_profile = self.profile() is not None
        self.install_button.setEnabled(idle and has_profile)
        self.deps_button.setEnabled(idle and has_profile)
        self.minimal_row.setEnabled(idle)

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
            True,  # preserve user.ltx
            True,  # preserve MCM settings
            skip_extract,
        )
        self._heavy = {}
        self._begin_run(
            cli_command(args, progress_interval_ms=200),
            operation="gamma",
            status=tr("Installing GAMMA..."),
        )

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
        self.install_button.show()
        self.deps_button.show()
        self.refresh()

    def on_back(self) -> bool:
        # A running install must not be abandoned by a stray B press; the
        # Cancel button on the progress view is the deliberate way out.
        if self._runner is not None:
            self.window.notify(tr("An install is already running."))
            return True
        return False
