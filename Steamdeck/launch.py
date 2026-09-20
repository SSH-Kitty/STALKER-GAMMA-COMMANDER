"""Launching the game from Deck Mode.

The desktop Play page carries a lot that a Deck user does not need in the
moment: a command preview, desktop-shortcut creation, a GE-Proton
downloader, crash-dump triage. What it does that Deck Mode must reproduce
exactly is the launch pipeline itself and the follow-up that turns a silent
failure into a sentence the user can act on.

No widgets here - this is a QObject that owns the pipeline and the polling
timer and reports through signals, so the Play screen stays a layout.
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from commander_gui import gui_settings
from commander_gui.config import logs_dir
from commander_gui.i18n import tr
from commander_gui.launcher import (
    LaunchError,
    ProcessGroupRegistry,
    build_command,
    build_direct_command,
    default_launch_target,
    ensure_runner_prefix,
    find_extra_protons,
    launch_detached,
    parse_mo2_executables,
    read_log_tail,
    resolve_runner,
    runner_crash_loop,
    runner_graphics_error,
    runner_prefix_error,
)
from commander_gui.ui.common import exe_pids, mo2_pids

#: How often the launch watchdog looks at the log and the process table.
_POLL_MS = 250

#: Give up waiting for the game to appear after this long. MO2 itself
#: starting is confirmation enough that the runner works; past this point
#: the user is better served by the UI unlocking than by a spinner.
_WATCH_TIMEOUT_S = 240.0


def runner_options() -> list[tuple[str, str]]:
    """(label, value) runner choices, matching the desktop UI's list.

    Same values the desktop pages store under the shared ``runner`` key, so
    a choice made in either interface means the same thing in the other.
    """
    options: list[tuple[str, str]] = [
        (tr("Auto-detect (latest GE-Proton)"), "auto")
    ]
    for label, path in find_extra_protons():
        options.append((tr("{label} (Installed)", label=label), f"umup:{path}"))
    return options


def runner_label(kind: str) -> str:
    """A human label for a stored runner value."""
    for label, value in runner_options():
        if value == kind:
            return label
    if kind.startswith(("umup:", "proton:")):
        return Path(kind.split(":", 1)[1]).name
    return kind


class DeckLaunchController(QObject):
    """Runs the launch pipeline and watches what happens next."""

    #: True while a launch is in flight or the game is running.
    state_changed = Signal(bool)
    #: Human-readable progress, for the Play screen's status line.
    status = Signal(str)
    #: (title, message) for a failure worth putting in front of the user.
    failed = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._registry = ProcessGroupRegistry()
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._active = False
        self._started_at = 0.0
        self._game_seen = False
        self._log_path = logs_dir() / "launcher.log"
        self._profile_name = ""
        # Which process actually represents "the game is running". MO2 keeps
        # running after the game closes, so a session that is timed against
        # MO2 would never end - the desktop Play page watches the target's
        # own executable for exactly this reason.
        self._watch_exe: str | None = None
        self._pre_launch_pids: set[int] = set()

    def is_active(self) -> bool:
        return self._active

    # -- launching --------------------------------------------------------
    def launch(self, profile, *, target: str | None, direct=None) -> None:
        """Start the game. ``direct`` bypasses MO2 with an Mo2Executable."""
        if self._active:
            return
        state = gui_settings.load_gui_settings()
        kind = state.get("runner") or "auto"
        # The raw saved prefix, as the desktop Play page passes it. Not
        # configured_wine_prefix(): that one is already pfx-resolved, and
        # resolve_runner() resolves again, which for a Steam Proton runner
        # produced STEAM_COMPAT_DATA_PATH=<x>/pfx - a prefix inside the prefix.
        prefix = (state.get("prefixes") or {}).get(kind) or state.get("wine_prefix") or ""

        # Announce before the synchronous work below: resolving a runner and
        # creating a Wine prefix touch the disk, and on a Deck's storage that
        # is long enough to look like a freeze if nothing has been said yet.
        self._set_active(True)
        self.status.emit(tr("Starting..."))

        try:
            runner = resolve_runner(kind, prefix)
            if direct is not None:
                command, env, cwd = build_direct_command(direct, runner)
                self._watch_exe = Path(direct.binary).name if direct.binary else None
            else:
                self._watch_exe = self._target_exe_name(profile, target)
                command, env, cwd = build_command(
                    profile.gamma,
                    runner,
                    target=target or None,
                    profile=profile.mo2_profile or None,
                )
            ensure_runner_prefix(runner)
            # Anything already running under this name belongs to an earlier
            # session and must not be mistaken for the one starting now.
            self._pre_launch_pids = (
                exe_pids(self._watch_exe) if self._watch_exe else set()
            )
            launch_detached(
                command,
                env,
                cwd,
                log_path=self._log_path,
                registry=self._registry,
            )
        except LaunchError as exc:
            self._set_active(False)
            self.failed.emit(tr("Launch Failed"), str(exc))
            return
        except OSError as exc:
            self._set_active(False)
            self.failed.emit(tr("Launch Failed"), str(exc))
            return

        self._profile_name = profile.profile_name or ""
        self._started_at = time.time()
        self._game_seen = False
        self.status.emit(tr("Waiting for Mod Organizer..."))
        self._timer.start()

    def _target_exe_name(self, profile, target: str | None) -> str | None:
        """The executable MO2 will actually start for ``target``."""
        if not target:
            return None
        for executable in parse_mo2_executables(profile.gamma):
            if executable.title == target and executable.binary:
                return Path(executable.binary).name
        return None

    def targets(self, profile) -> tuple[list[str], str]:
        """Launch target titles from ModOrganizer.ini, and the default one."""
        titles = [exe.title for exe in parse_mo2_executables(profile.gamma)]
        return titles, default_launch_target(titles)

    def executables(self, profile):
        return parse_mo2_executables(profile.gamma)

    # -- watchdog ---------------------------------------------------------
    def _poll(self) -> None:
        elapsed = time.time() - self._started_at

        # Crash-loop breaker. A prefix carrying another Wine's ntdll makes
        # every process fault; Wine answers each fault with winedbg, whose
        # process faults too. Left alone that exhausts memory in about two
        # minutes and freezes the machine, so it is checked on every tick
        # and killed the moment it is recognisable.
        if runner_crash_loop(read_log_tail(self._log_path)):
            self._registry.cleanup_all()
            self._kill_stray_debuggers()
            self._stop_watching()
            self.failed.emit(
                tr("Launch Failed"),
                tr(
                    "Wine crashed repeatedly while starting Mod Organizer, and "
                    "COMMANDER stopped it before it could exhaust memory. This "
                    "usually means another Wine has written into the game's "
                    "prefix. Use Repair Prefix in the full interface, then "
                    "reinstall the dependencies."
                ),
            )
            return

        if self._watch_exe:
            running = bool(exe_pids(self._watch_exe) - self._pre_launch_pids)
        else:
            # Launching MO2 itself: MO2's own window is the session.
            running = bool(mo2_pids())
        if running:
            self._game_seen = True
            self.status.emit(tr("Running"))
            return
        if not self._game_seen and mo2_pids():
            # MO2 is up and about to hand off to the game. The runner clearly
            # works, so nothing below needs to diagnose a failure yet.
            self.status.emit(tr("Mod Organizer is running"))
            return

        if self._game_seen:
            # Ran and exited: a normal session end.
            self._finish_session(elapsed)
            return

        if elapsed < 6.0:
            return  # still starting up

        log = read_log_tail(self._log_path)
        if runner_prefix_error(log):
            self._stop_watching()
            self.failed.emit(
                tr("Launch Failed"),
                tr(
                    "The selected runner could not use this Wine prefix. "
                    "Pick a different runner, or let the full interface "
                    "rebuild the prefix."
                ),
            )
            return
        if runner_graphics_error(log):
            self._stop_watching()
            self.failed.emit(
                tr("Launch Failed"),
                tr(
                    "The game could not start its graphics backend. Check "
                    "the Vulkan entries on the System screen."
                ),
            )
            return
        if elapsed > _WATCH_TIMEOUT_S:
            self._stop_watching()
            self.status.emit(tr("Ready"))

    def _finish_session(self, elapsed: float) -> None:
        self._stop_watching()
        self._record_playtime(elapsed)
        self.status.emit(tr("Ready"))

    def _record_playtime(self, elapsed: float) -> None:
        """Add this session to the shared per-profile playtime tally.

        Same keys the desktop Play page writes, so total playtime stays
        continuous across a switch between interfaces.
        """
        if not self._profile_name or elapsed <= 0:
            return
        state = gui_settings.load_gui_settings()
        playtime = dict(state.get("playtime_seconds") or {})
        last_played = dict(state.get("last_played_ts") or {})
        playtime[self._profile_name] = (
            float(playtime.get(self._profile_name, 0.0)) + elapsed
        )
        last_played[self._profile_name] = time.time()
        gui_settings.save_gui_settings(
            playtime_seconds=playtime, last_played_ts=last_played
        )

    @staticmethod
    def _kill_stray_debuggers() -> None:
        """winedbg instances that escaped the launch's process group."""
        import subprocess

        try:
            subprocess.run(
                ["pkill", "-f", "winedbg"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _stop_watching(self) -> None:
        self._timer.stop()
        self._set_active(False)

    def _set_active(self, active: bool) -> None:
        if self._active == active:
            return
        self._active = active
        self.state_changed.emit(active)

    def shutdown(self) -> None:
        self._timer.stop()
