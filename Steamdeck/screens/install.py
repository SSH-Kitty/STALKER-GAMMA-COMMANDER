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
from PySide6.QtWidgets import QGridLayout, QHBoxLayout

from commander_gui import gui_settings
from commander_gui.cli_runner import cli_command
from commander_gui.dependencies import check_all_dependencies
from commander_gui.game_backup import (
    apply_pending_settings_restore,
    backup_settings_before,
)
from commander_gui.gui_settings import configured_wine_prefix
from commander_gui.i18n import tr
from commander_gui.launcher import LaunchError, find_extra_protons
from commander_gui.parsers import parse_progress_line, strip_ansi
from commander_gui.proton_installer import (
    build_size,
    fetch_ge_proton_releases,
    install_proton,
    installed_builds,
    remove_build,
)
from commander_gui.settings import cli_ok
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
from commander_gui.ui.install_page import _full_install_args, _resume_state_matches
from commander_gui.ui.proton_manager import build_status
from commander_gui.winetricks import (
    WINETRICKS_VERBS,
    check_winetricks_full_status,
    protontricks_binary,
    protontricks_install_command,
    umu_install_command,
    winetricks_install_command,
)

from .. import gamepad as pad
from ..power import on_battery
from ..scale import px
from ..widgets import (
    DeckCard,
    DeckPicker,
    DeckProgress,
    DeckRow,
    DeckStatusRow,
    DeckToggleRow,
    deck_button,
    deck_divider,
    deck_label,
    deck_step_badge,
    picker_overlay,
    side_by_side,
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
    line.setSpacing(px(12))
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


def _save_resume(state: dict, window) -> None:
    """Record (or clear) the resumable-install state, never raising.

    Runs from an install's finished handler, before the install lock is
    released. A failed write (a full disk is the usual reason an install
    stops) used to raise out of that handler and leave the lock held.
    """
    try:
        gui_settings.save_gui_settings(gamma_install_resume=state)
    except OSError as exc:
        window.notify(tr("Could not save install progress: {exc}", exc=exc), 8000)


def battery_warning() -> str:
    """A paragraph for a long job's confirm text when running on battery."""
    if not on_battery():
        return ""
    return "\n\n" + tr(
        "You're on battery. Plug the Deck in - this download takes hours, "
        "and COMMANDER keeps the Deck awake while it runs."
    )


class InstallScreen(DeckScreen):
    def build(self) -> None:
        self._runner: CommandRunner | None = None
        self._deps_task: BackgroundTask | None = None
        self._deps_detail_task: BackgroundTask | None = None
        self._stage: str | None = None
        self._heavy: dict[str, float] = {}
        self._anomaly_installed = False
        self._gamma_installed = False
        self._resuming = False
        self._proton_releases: list[dict] = []
        self._proton_releases_task: BackgroundTask | None = None
        self._proton_task: BackgroundTask | None = None
        self._proton_manage_task: BackgroundTask | None = None
        self._proton_version: str = ""
        self._proton_bridge: _ProtonProgressBridge | None = None

        # Progress sits above the steps, not at the bottom of a long page:
        # whichever step started it, it is the first thing on screen and
        # focus_controls() lands on its Cancel button.
        self.progress = DeckProgress()
        self.progress.hide()
        self.body.addWidget(self.progress)

        self.resume_note = deck_label("", role="body", wrap=True)
        self.resume_note.setObjectName("deckBodyWarn")
        self.resume_note.hide()
        self.body.addWidget(self.resume_note)

        # Steps 1 and 2 share one grid rather than two independent columns:
        # each row (step line / status / button) lines up across both, so a
        # one-line step caption next to a two-line one no longer staggers
        # the status cards and buttons below them.
        ag_card = DeckCard()
        grid = QGridLayout()
        grid.setHorizontalSpacing(px(20))
        grid.setVerticalSpacing(px(10))
        grid.addLayout(_step_line(1, tr("Install STALKER Anomaly.")), 0, 0)
        grid.addLayout(_step_line(2, tr("Install the GAMMA modpack.")), 0, 2)
        self.anomaly_row = DeckStatusRow(tr("STALKER Anomaly"))
        self.gamma_row = DeckStatusRow(tr("GAMMA Modpack"))
        grid.addWidget(self.anomaly_row, 1, 0)
        grid.addWidget(self.gamma_row, 1, 2)
        # Same role, same size: the two steps are equals, and a primary
        # Install GAMMA half as tall again as Install Anomaly beside it read
        # as a different kind of control.
        self.anomaly_button = deck_button(
            tr("Install Anomaly"), role="primary", on_click=self._confirm_anomaly_install
        )
        self.install_button = deck_button(
            tr("Install GAMMA"), role="primary", on_click=self._confirm_install
        )
        grid.addWidget(self.anomaly_button, 2, 0)
        grid.addWidget(self.install_button, 2, 2)
        grid.addWidget(deck_divider(), 0, 1, 3, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        ag_card.body.addLayout(grid)
        self.body.addWidget(ag_card)

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

        # Compatibility tool | Install options, side by side.
        # The GE-Proton downloader (commander_gui/proton_installer.py) is
        # independent of which runner is selected, so it is its own tool
        # rather than folded into a runner picker.
        proton_card = DeckCard()
        proton_card.body.addWidget(
            deck_label(tr("Compatibility tool"), role="rowTitle")
        )
        self.proton_row = DeckRow(tr("GE-Proton"))
        self.proton_row.activated.connect(self._pick_proton_version)
        proton_card.body.addWidget(self.proton_row)
        self.proton_install_button = deck_button(
            tr("Install GE-Proton"), on_click=self._install_proton
        )
        self.proton_install_button.setEnabled(False)
        proton_card.body.addWidget(self.proton_install_button)
        self.proton_builds_row = DeckRow(tr("Installed builds"))
        self.proton_builds_row.activated.connect(self._manage_protons)
        proton_card.body.addWidget(self.proton_builds_row)
        self.proton_status = deck_label("", role="caption", wrap=True)
        proton_card.body.addWidget(self.proton_status)
        proton_card.body.addStretch(1)

        options_card = DeckCard()
        options_card.body.addWidget(deck_label(tr("Install options"), role="rowTitle"))
        self.minimal_row = DeckToggleRow(tr("Minimal (~100 GB)"), False)
        self.preserve_user_row = DeckToggleRow(tr("Preserve user.ltx settings"), True)
        self.preserve_mcm_row = DeckToggleRow(tr("Preserve MCM settings"), True)
        for row in (self.minimal_row, self.preserve_user_row, self.preserve_mcm_row):
            options_card.body.addWidget(row)
        options_card.body.addStretch(1)
        self.body.addLayout(side_by_side(proton_card, options_card))

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
                tr("Create or activate a profile first (Dashboard → Profiles).")
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
        self._resuming = _resume_state_matches(
            gui_settings.load_gui_settings().get("gamma_install_resume"), profile
        )
        if self._resuming:
            self.gamma_row.set_status(tr("Incomplete"), "warn")
            self.install_button.setText(tr("Resume install"))
            self.resume_note.setText(
                tr(
                    "The last GAMMA install did not finish. Resume it - "
                    "most of what already downloaded is reused, but some "
                    "large files from GAMMA's GitHub repos download again."
                )
            )
            self.resume_note.show()
        else:
            self.install_button.setText(
                tr("Reinstall GAMMA") if self._gamma_installed else tr("Install GAMMA")
            )
            self.resume_note.hide()
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
        self.update_hints()

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
            self._missing_deps = [str(n) for n in names]
        else:
            self.deps_row.set_status(tr("Ready"), "ok")
            self._missing_deps = []
        self._render_deps_caption()

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
        self._deps_count_text = tr(
            "{installed}/{total} dependencies installed",
            installed=installed,
            total=total,
        )
        self._render_deps_caption()

    def _render_deps_caption(self) -> None:
        # The status card is read-only, so what is missing is spelled out
        # here instead of behind a press.
        parts = [getattr(self, "_deps_count_text", "")]
        missing = getattr(self, "_missing_deps", [])
        if missing:
            parts.append(tr("Missing:") + " " + ", ".join(missing))
        self.deps_detail.setText("  ·  ".join(p for p in parts if p))

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
        message += battery_warning()
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
        message += battery_warning()
        self.window.confirm(
            tr("Install GAMMA"),
            message,
            self._start_install,
            confirm_text=tr("Install"),
        )

    def _start_install(self) -> bool:
        """Start the full install. False if it could not be started."""
        profile = self.profile()
        if profile is None:
            return False
        for path in (profile.anomaly, profile.gamma, profile.cache):
            if not path:
                continue
            try:
                Path(path).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.window.notify(str(exc), 6000)
                return False

        # Both must genuinely exist before trusting the cache to skip
        # re-extraction. Checking Anomaly alone is wrong straight after a
        # GAMMA-only reset, which empties gamma/mods but keeps the download
        # cache: every cached, hash-valid archive would then be skipped
        # instead of re-extracted, permanently losing those mods. The
        # desktop page's guard, reproduced exactly.
        skip_extract = anomaly_installed(profile.anomaly) and gamma_installed(
            profile.gamma, profile.mo2_profile
        )
        if gamma_installed(profile.gamma, profile.mo2_profile):
            error = backup_settings_before(profile, "reinstall")
            if error:
                self.window.notify(tr("Settings backup failed: {message}", message=error), 6000)
        args = _full_install_args(
            self.minimal_row.is_checked(),
            self.preserve_user_row.is_checked(),
            self.preserve_mcm_row.is_checked(),
            skip_extract,
        )
        self._begin_run(
            cli_command(args, progress_interval_ms=200),
            operation="gamma",
            status=tr("Installing GAMMA..."),
        )
        return True

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
        # Its result, not a blanket True: a Fresh/GAMMA Reset holds the
        # install lock until this starts, and releases it only when told the
        # reinstall didn't - otherwise the lock stayed held for the session.
        return self._start_install()

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
        self._operation = operation
        self._heavy = {}
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
        self.progress.focus_controls()

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
        ok = cli_ok(rc, output, "") if self._stage is None else rc == 0

        # Dependency stages chain: umu -> protontricks (unless already
        # present) -> the Winetricks verbs. install_busy stays held across
        # the hand-off - dropping it between stages left a window in which
        # another screen could start a second job against the same prefix.
        if self._stage is not None and ok and not cancelled:
            if self._stage == "umu":
                self._stage = "verbs" if protontricks_binary() else "tools"
                self._run_stage()
                return
            if self._stage == "tools":
                self._stage = "verbs"
                self._run_stage()
                return

        restore_note = None
        self._last_operation = getattr(self, "_operation", None)
        self._last_stage = self._stage
        if getattr(self, "_operation", None) == "gamma":
            # Same record, same rules as the desktop Install page: a failed
            # or cancelled GAMMA install is resumable, a finished one is not.
            if cancelled or not ok:
                self._save_resume_state()
            else:
                _save_resume({}, self.window)
                restore_note = apply_pending_settings_restore(self.profile())
        self._operation = None

        operation = self._finished_operation_title()
        self._finish_run()
        self.window.update_mod_counter()
        if cancelled:
            self.window.notify(tr("Cancelled"))
        elif ok:
            message = tr("{operation} completed successfully.", operation=operation)
            if restore_note:
                message += "\n\n" + restore_note
            self.window.announce_finished(
                tr("{operation} finished", operation=operation), message
            )
        else:
            self.window.announce_finished(
                tr("{operation} failed", operation=operation),
                tr("Failed") + f" (exit {rc})" + "  ·  " + tr("Resume from the Install screen."),
                ok=False,
            )

    def _finished_operation_title(self) -> str:
        if getattr(self, "_last_stage", None) is not None:
            return tr("Dependency install")
        return {
            "gamma": tr("GAMMA install"),
            "anomaly": tr("Anomaly install"),
        }.get(getattr(self, "_last_operation", None) or "", tr("Install"))

    def _save_resume_state(self) -> None:
        profile = self.profile()
        if profile is None:
            return
        _save_resume({
            "profile": profile.profile_name,
            "anomaly": profile.anomaly,
            "gamma": profile.gamma,
            "cache": profile.cache,
        }, self.window)

    def _finish_run(self) -> None:
        self._stage = None
        self._runner = None
        self.window.set_install_busy(False)
        self.progress.hide()
        self.anomaly_button.show()
        self.install_button.show()
        self.deps_button.show()
        self.refresh()

    def on_action(self, action: str) -> bool:
        if action == pad.CONTEXT:
            if self.install_button.isEnabled() and self.install_button.isVisible():
                self._confirm_install()
            return True
        if action == pad.SEARCH:
            if self.deps_button.isEnabled() and self.deps_button.isVisible():
                self._confirm_dependencies()
            return True
        return False

    def hints(self):
        hints = [("A", "Select")]
        # Named after what the button says right now (Install, Reinstall,
        # Resume), and only while X would do it.
        if self.install_button.isEnabled() and self.install_button.isVisible():
            hints.append(("X", self.install_button.text()))
        if self.deps_button.isEnabled() and self.deps_button.isVisible():
            hints.append(("Y", "Dependencies"))
        hints.append(("L1 R1 / L2 R2", "Switch tab"))
        return hints

    def default_focus(self):
        # The next install step still to do, in the order the screen reads.
        for button in (self.anomaly_button, self.install_button, self.proton_install_button):
            if button.isVisible() and button.isEnabled():
                return button
        return None

    def on_back(self) -> bool:
        # A running install must not be abandoned by a stray B press; the
        # Cancel button on the progress view is the deliberate way out.
        if (
            self._runner is not None
            or self._proton_task is not None
            or self._proton_manage_task is not None
        ):
            self.window.notify(tr("An install is already running."))
            return True
        return False

    # -- GE-Proton ----------------------------------------------------------
    def _update_proton_button(self) -> None:
        idle = not self.window.install_busy and self._proton_task is None
        # Cheap count only (runs on every busy change); the full list with
        # sizes and "in use" is built when the row is opened.
        builds = [
            label
            for label, path in find_extra_protons()
            if Path(path).parent.parent.name == "compatibilitytools.d"
        ]
        self.proton_builds_row.set_value(
            tr("{count} - manage", count=len(builds)) if builds else tr("None")
        )
        self.proton_builds_row.setEnabled(idle and bool(builds))
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
            searchable=True,
        )
        picker.chosen.connect(self._apply_proton_version)
        self.window.show_overlay(
            picker_overlay(self.window, tr("GE-Proton version"), picker, panel_width=1100)
        )

    def _apply_proton_version(self, tag: object) -> None:
        self.window.dismiss_overlay()
        self._proton_version = str(tag)
        self.proton_row.set_value(self._proton_version)
        self._update_proton_button()
        # Picking a version is only ever the step before installing it.
        if self.proton_install_button.isEnabled():
            self.window._focus.focus(self.proton_install_button)

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
        self.progress.focus_controls()

    # -- removing builds ------------------------------------------------------
    def _manage_protons(self) -> None:
        if self._proton_manage_task is not None or self.window.install_busy:
            return
        self.window.notify(tr("Checking GE-Proton builds..."), 0)

        def _scan():
            builds = installed_builds()
            return [(build, build_size(build.path)) for build in builds]

        task = BackgroundTask(_scan, parent=self)
        self._proton_manage_task = task
        task.result.connect(self._show_proton_builds)
        task.error.connect(self._on_proton_manage_error)
        task.start()

    def _on_proton_manage_error(self, message: str) -> None:
        self._proton_manage_task = None
        self.window.notify(tr("Failed") + ": " + message, 6000)

    def _show_proton_builds(self, scanned: object) -> None:
        self._proton_manage_task = None
        self.window.toast.hide()
        entries = list(scanned or [])
        if not entries:
            self.window.notify(tr("No GE-Proton builds found in compatibilitytools.d."))
            self._update_proton_button()
            return
        picker = DeckPicker(
            "",
            [
                (f"{build.name}   ·   {human_size(size)}   ·   {build_status(build)}", index)
                for index, (build, size) in enumerate(entries)
            ],
            None,
        )
        picker.chosen.connect(lambda index: self._confirm_proton_removal(*entries[int(index)]))
        self.window.show_overlay(
            picker_overlay(self.window, tr("Remove a GE-Proton build"), picker, panel_width=1200)
        )

    def _confirm_proton_removal(self, build, size: int) -> None:
        self.window.dismiss_overlay()
        if build.in_use:
            self.window.notify(
                tr(
                    "{name} is the build COMMANDER launches the game with. "
                    "Pick another runner on the Play screen first.",
                    name=build.name,
                ),
                6000,
            )
            return
        message = tr("Permanently delete {name} and free {size}?", name=build.name, size=human_size(size))
        if build.steam_uses:
            message += "\n\n" + tr(
                "Steam games set to use a removed build fall back to Steam's "
                "default until you pick another one in their Properties."
            )

        def _start() -> None:
            if self.window.install_busy or self._proton_manage_task is not None:
                self.window.notify(tr("Another task is already running."))
                return
            if mo2_running(force=True):
                self.window.notify(
                    tr("Mod Organizer / the game is currently running. Close it first.")
                )
                return
            self.window.set_install_busy(True)
            runner_kind = gui_settings.load_gui_settings().get("runner") or "auto"
            task = BackgroundTask(remove_build, build.path, runner_kind, parent=self)
            self._proton_manage_task = task
            task.result.connect(lambda _r: self._on_proton_removed(build.name, size, None))
            task.error.connect(lambda msg: self._on_proton_removed(build.name, size, msg))
            task.start()

        self.window.confirm(
            tr("Remove GE-Proton"), message, _start, confirm_role="danger", confirm_text=tr("Remove")
        )

    def _on_proton_removed(self, name: str, size: int, error: str | None) -> None:
        self._proton_manage_task = None
        self.window.set_install_busy(False)
        if error:
            self.window.notify(tr("Failed") + ": " + error, 8000)
        else:
            self.window.notify(
                tr("Removed {name}, freed {size}.", name=name, size=human_size(size)), 5000
            )
        self._update_proton_button()

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
        self.window.announce_finished(
            tr("GE-Proton installed"),
            tr("Installed {version}", version=self._proton_version),
        )

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
            self.window.announce_finished(
                tr("GE-Proton install failed"), tr("Failed") + ": " + message, ok=False
            )
        self._update_proton_button()
