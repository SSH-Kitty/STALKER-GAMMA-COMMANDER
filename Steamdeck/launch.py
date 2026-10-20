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

import os
import signal
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from commander_gui import coop, gui_settings
from commander_gui.config import logs_dir
from commander_gui.i18n import tr
from commander_gui.launcher import (
    PTRACE_FIX_COMMAND,
    ProcessGroupRegistry,
    anomaly_launcher_target,
    build_command,
    build_direct_command,
    default_launch_target,
    ensure_runner_prefix,
    find_extra_protons,
    kill_stray_debuggers,
    launch_detached,
    parse_mo2_executables,
    ptrace_scope,
    read_log_tail,
    resolve_runner,
    runner_crash_loop,
    runner_graphics_error,
    runner_prefix_error,
)
from commander_gui.ui import game_splash
from commander_gui.ui.common import BackgroundTask, crash_dump_names, exe_pids, mo2_pids
from commander_gui.ui.play_page import _CRASH_POLL_INTERVAL_MS, _CRASH_POLL_MAX_ATTEMPTS

#: How often the launch watchdog looks at the log and the process table:
#: fast while the game is starting (a crash loop must be caught within
#: seconds), slow once it runs - each tick runs pgrep, and doing that four
#: times a second for a whole session cost CPU and battery on the Deck.
_POLL_MS = 250
_RUNNING_POLL_MS = 2000

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
        (tr("Auto-detect (installed GE-Proton)"), "auto")
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


def _prepare_launch(profile, kind, prefix, target, direct, watch_exe, arguments) -> dict:
    """The disk-bound half of a launch, run on a worker thread."""
    runner = resolve_runner(kind, prefix)
    if direct is not None:
        command, env, cwd = build_direct_command(direct, runner)
    else:
        command, env, cwd = build_command(
            profile.gamma,
            runner,
            target=target or None,
            profile=profile.mo2_profile or None,
            arguments=arguments,
        )
    ensure_runner_prefix(runner)
    return {
        "runner_env": dict(runner.env),
        "command": command,
        "env": env,
        "cwd": cwd,
        "watch_exe": watch_exe,
        "splash": arguments is not None,
        "pre_pids": exe_pids(watch_exe) if watch_exe else set(),
        "pre_mo2": mo2_pids(),
        "crash_baseline": crash_dump_names(profile.anomaly) if profile.anomaly else set(),
    }


class DeckLaunchController(QObject):
    """Runs the launch pipeline and watches what happens next."""

    #: True while a launch is in flight or the game is running.
    state_changed = Signal(bool)
    #: Human-readable progress, for the Play screen's status line.
    status = Signal(str)
    #: (title, message) for a failure worth putting in front of the user.
    failed = Signal(str, str)
    #: A session ended and X-Ray wrote a new crash dump.
    crashed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._registry = ProcessGroupRegistry()
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._active = False
        self._started_at = 0.0
        #: When the game itself first appeared - playtime counts from here,
        #: not from the Play press, so Mod Organizer's own start-up (a minute
        #: or more on a first launch) is not billed as time played.
        self._game_started_at = 0.0
        self._game_seen = False
        self._log_path = logs_dir() / "launcher.log"
        self._profile_name = ""
        # Which process actually represents "the game is running". MO2 keeps
        # running after the game closes, so a session that is timed against
        # MO2 would never end - the desktop Play page watches the target's
        # own executable for exactly this reason.
        self._watch_exe: str | None = None
        self._pre_launch_pids: set[int] = set()
        self._pre_launch_mo2: set[int] = set()
        #: The launch being prepared on a worker thread, if any.
        self._launch_task: BackgroundTask | None = None
        #: True for Open MO2 (Mod Organizer's own window, no game target):
        #: the session is MO2 itself, so the screen says so instead of
        #: "Quit Game".
        self._mo2_only = False
        #: The launched runner's environment, to find its prefix's winedbg.
        self._runner_env: dict[str, str] = {}
        #: The session was started by Steam (co-op shortcut), not by us.
        self._steam_launch = False
        #: Crash dumps that already existed at launch, and the bounded poll
        #: that looks for a new one after the session ends (X-Ray can take
        #: tens of seconds to finish writing it).
        self._anomaly = ""
        self._crash_baseline: set[str] = set()
        self._crash_attempts_left = 0
        self._crash_timer = QTimer(self)
        self._crash_timer.setSingleShot(True)
        self._crash_timer.setInterval(_CRASH_POLL_INTERVAL_MS)
        self._crash_timer.timeout.connect(self._poll_crash)
        #: COMMANDER's splash process for a co-op launch (see game_splash).
        self._splash = None

    def is_active(self) -> bool:
        return self._active

    def mo2_only(self) -> bool:
        """True while the active session is Mod Organizer alone (Open MO2)."""
        return self._active and self._mo2_only

    def game_running(self) -> bool:
        return self._active and self._game_seen

    def session_seconds(self) -> float:
        """How long the game has been running this session, or 0."""
        if not self.game_running():
            return 0.0
        return max(0.0, time.time() - self._game_started_at)

    def force_stop(self) -> None:
        """Kill everything this launch started.

        The Deck has no Alt+F4 and Game Mode has no task manager, so a game
        or Mod Organizer that hangs on exit otherwise leaves the user with
        nothing but a reboot. The session is still recorded.
        """
        if not self._active:
            return
        seen = self._game_seen
        elapsed = self.session_seconds()
        if self._steam_launch:
            # Steam's reaper owns the process group, and killing that group
            # could take Steam's own helpers with it - only this launch's
            # MO2 and game processes go (same as the desktop Play page).
            pids = mo2_pids() - self._pre_launch_mo2
            if self._watch_exe:
                pids |= exe_pids(self._watch_exe) - self._pre_launch_pids
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        self._registry.cleanup_all()
        self._kill_stray_debuggers()
        self._stop_watching()
        if seen:
            self._record_playtime(elapsed)
        self.status.emit(tr("Stopped"))

    # -- launching --------------------------------------------------------
    def launch(self, profile, *, target: str | None, direct=None) -> None:
        """Start the game. ``direct`` bypasses MO2 with an Mo2Executable."""
        if self._active:
            return
        # Co-op on means co-op only (the engine on disk is co-op's), off
        # means single-player only. A launch here follows the installed
        # mode; the Multiplayer screen is where it is changed.
        reason = coop.launch_block_reason(
            profile.anomaly, coop_launch=coop.is_active(profile.anomaly), direct=direct is not None
        )
        if reason:
            self.failed.emit(tr("Launch Failed"), tr(reason))
            return
        scope = ptrace_scope()
        if direct is None and scope >= 2:
            self.failed.emit(
                tr("A Linux setting is blocking GAMMA"),
                tr(
                    "A security setting on your computer (ptrace_scope = {scope}) stops Mod Organizer "
                    "from loading GAMMA's mods, so the game can't start. Setting it to 1 fixes this.",
                    scope=scope,
                )
                + "\n\n"
                + tr("Open a terminal, run this command, then restart your computer:")
                + "\n"
                + PTRACE_FIX_COMMAND,
            )
            return
        self._crash_timer.stop()
        self._steam_launch = False
        state = gui_settings.load_gui_settings()
        kind = state.get("runner") or "auto"
        # The raw saved prefix, as the desktop Play page passes it. Not
        # configured_wine_prefix(): that one is already pfx-resolved, and
        # resolve_runner() resolves again, which for a Steam Proton runner
        # produced STEAM_COMPAT_DATA_PATH=<x>/pfx - a prefix inside the prefix.
        prefix = (state.get("prefixes") or {}).get(kind) or state.get("wine_prefix") or ""

        # Set before _set_active(), whose state_changed the screen labels from.
        self._mo2_only = direct is None and not target
        # Active from here, so a second press can't start a second launch
        # while the first is still being prepared.
        self._set_active(True)
        name = direct.title if direct is not None else target or "Mod Organizer 2"
        self.status.emit(tr("Launching {name}", name=name))

        # Resolving the runner, checking its prefix and listing processes
        # touch the disk: off the GUI thread, so the screen keeps painting
        # (and says "Launching") on slow Deck storage.
        if direct is not None:
            watch_exe = Path(direct.binary).name if direct.binary else None
        else:
            watch_exe = self._target_exe_name(profile, target)
        if direct is None:
            arguments = self._splash_arguments(profile, target)
        else:
            # Plain Anomaly co-op runs the game with the no-splash switch
            # (Multiplayer.start_coop), so ours stands in for the engine's.
            arguments = direct.arguments if game_splash.SPLASH_FLAG in direct.arguments.split() else None
        task = BackgroundTask(
            _prepare_launch, profile, kind, prefix, target, direct, watch_exe, arguments,
            parent=self,
        )
        self._launch_task = task
        task.result.connect(lambda prepared, task=task: self._start_prepared(task, profile, prepared))
        task.error.connect(lambda message, task=task: self._launch_failed(task, message))
        task.start()

    def track_steam_session(self, profile, target: str) -> None:
        """Watch a co-op session Steam was just asked to start.

        Nothing of ours is spawned (Steam runs the co-op shortcut), so the
        session is followed by process name only, and launcher.log - which
        this launch never writes - is not read for a diagnosis.
        """
        if self._active:
            return
        self._crash_timer.stop()
        self._steam_launch = True
        self._mo2_only = False
        plain = coop.install_kind(profile.anomaly) == coop.KIND_ANOMALY
        # Plain Anomaly: Steam runs the game exe AnomalyLauncher.cfg picks
        # (play_page.plain_game_exe); any renderer's exe ("AnomalyDX...") is the session.
        self._watch_exe = "AnomalyDX" if plain else self._target_exe_name(profile, target)
        self._pre_launch_pids = exe_pids(self._watch_exe) if self._watch_exe else set()
        self._pre_launch_mo2 = mo2_pids()
        self._anomaly = profile.anomaly or ""
        self._crash_baseline = crash_dump_names(profile.anomaly) if profile.anomaly else set()
        self._runner_env = {}
        self._profile_name = profile.profile_name or ""
        self._started_at = time.time()
        self._game_started_at = 0.0
        self._game_seen = False
        self._set_active(True)
        self.status.emit(tr("Starting Anomaly through Steam") if plain else tr("Starting GAMMA through Steam"))
        # Plain Anomaly's shortcut hides the engine's splash too (coop_shortcut_fields).
        if plain or self._splash_arguments(profile, target) is not None:
            self._start_splash(profile)
        self._timer.setInterval(_POLL_MS)
        self._timer.start()

    def _launch_failed(self, task, message: str) -> None:
        if task is not self._launch_task:
            return  # stopped meanwhile
        self._launch_task = None
        self._set_active(False)
        self.failed.emit(tr("Launch Failed"), message)

    def _start_prepared(self, task, profile, prepared: dict) -> None:
        if task is not self._launch_task:
            return  # stopped meanwhile: never start the game after that
        self._launch_task = None
        self._runner_env = prepared["runner_env"]
        self._watch_exe = prepared["watch_exe"]
        # Anything already running under these names belongs to an earlier
        # session (or the desktop instance) and must not be mistaken for the
        # one starting now.
        self._pre_launch_pids = prepared["pre_pids"]
        self._pre_launch_mo2 = prepared["pre_mo2"]
        self._anomaly = profile.anomaly or ""
        self._crash_baseline = prepared["crash_baseline"]
        try:
            launch_detached(
                prepared["command"],
                prepared["env"],
                prepared["cwd"],
                log_path=self._log_path,
                registry=self._registry,
            )
        except Exception as exc:  # noqa: BLE001 - never leave the session stuck "active"
            self._set_active(False)
            self.failed.emit(tr("Launch Failed"), str(exc))
            return
        if prepared["splash"]:
            self._start_splash(profile)

        self._profile_name = profile.profile_name or ""
        self._started_at = time.time()
        self._game_started_at = 0.0
        self._game_seen = False
        self._timer.setInterval(_POLL_MS)
        self.status.emit(tr("Waiting for Mod Organizer 2 to open"))
        self._timer.start()

    def _target_exe_name(self, profile, target: str | None) -> str | None:
        """The executable MO2 will actually start for ``target``."""
        if not target:
            return None
        for executable in parse_mo2_executables(profile.gamma):
            if executable.title == target and executable.binary:
                return Path(executable.binary).name
        return None

    def _splash_arguments(self, profile, target: str | None) -> str | None:
        """The target's arguments plus the engine's no-splash switch, for a
        co-op launch of the game itself (see game_splash)."""
        if not target or not coop.is_active(profile.anomaly):
            return None
        exe = next((e for e in parse_mo2_executables(profile.gamma) if e.title == target), None)
        return game_splash.splash_arguments(exe)

    def _start_splash(self, profile) -> None:
        self._close_splash()
        try:
            self._splash = game_splash.start(profile.anomaly, profile.gamma)
        except OSError:
            self._splash = None  # cosmetic; the launch goes on without it

    def _close_splash(self) -> None:
        if self._splash is not None:
            if self._splash.poll() is None:
                self._splash.terminate()
            self._splash = None

    def targets(self, profile) -> tuple[list[str], str]:
        """Launch target titles from ModOrganizer.ini, and the default one."""
        titles = [exe.title for exe in self.executables(profile)]
        saved = gui_settings.load_gui_settings().get("target") or ""
        return titles, default_launch_target(titles, profile.gamma, saved)

    def executables(self, profile):
        """MO2's launch targets, or plain Anomaly's launcher without MO2."""
        found = parse_mo2_executables(profile.gamma)
        launcher = None if found else anomaly_launcher_target(profile.anomaly)
        return [launcher] if launcher is not None else found

    # -- watchdog ---------------------------------------------------------
    def _poll(self) -> None:
        elapsed = time.time() - self._started_at

        # Crash-loop breaker. A prefix carrying another Wine's ntdll makes
        # every process fault; Wine answers each fault with winedbg, whose
        # process faults too. Left alone that exhausts memory in about two
        # minutes and freezes the machine, so it is checked on every tick
        # and killed the moment it is recognisable.
        if not self._steam_launch and runner_crash_loop(read_log_tail(self._log_path)):
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
            running = bool(mo2_pids() - self._pre_launch_mo2)
        if running:
            if not self._game_seen:
                self._game_seen = True
                self._game_started_at = time.time()
                self._timer.setInterval(_RUNNING_POLL_MS)
            self.status.emit(tr("Running"))
            return
        if not self._game_seen and mo2_pids() - self._pre_launch_mo2:
            # MO2 is up and about to hand off to the game. The runner clearly
            # works, so nothing below needs to diagnose a failure yet.
            self.status.emit(tr("Mod Organizer is running"))
            return

        if self._game_seen:
            # Ran and exited: a normal session end.
            self._finish_session(time.time() - self._game_started_at)
            return

        if elapsed < 6.0:
            return  # still starting up
        if self._steam_launch:
            # Steam may restart first; no log of ours to diagnose from.
            if elapsed > _WATCH_TIMEOUT_S:
                self._stop_watching()
                self.status.emit(tr("Ready"))
            return

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
        # Only a natural exit: a Force Stop is the user's own doing.
        if self._anomaly:
            self._crash_attempts_left = _CRASH_POLL_MAX_ATTEMPTS
            self._poll_crash()

    def _poll_crash(self) -> None:
        new_dumps = crash_dump_names(self._anomaly) - self._crash_baseline
        if new_dumps:
            self._crash_baseline |= new_dumps
            self.crashed.emit()
            return
        self._crash_attempts_left -= 1
        if self._crash_attempts_left > 0:
            self._crash_timer.start()

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

    def _kill_stray_debuggers(self) -> None:
        """winedbg instances of *this* prefix that escaped the process group."""
        kill_stray_debuggers(self._runner_env)

    def _stop_watching(self) -> None:
        self._close_splash()
        self._timer.stop()
        # A launch still being prepared must not start after a stop.
        self._launch_task = None
        self._set_active(False)

    def _set_active(self, active: bool) -> None:
        if self._active == active:
            return
        self._active = active
        self.state_changed.emit(active)

    def shutdown(self) -> None:
        self._close_splash()
        self._launch_task = None
        self._timer.stop()
        self._crash_timer.stop()
