"""Shared widgets and helpers for the GUI."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    Qt,
    QThread,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import is_secondary_instance
from ..i18n import tr
from ..integrity import format_size
from ..modlist import count_mods, read_lines
from ..parsers import ProgressEvent, parse_progress_line, strip_ansi
from ..winetricks import WINETRICKS_VERBS

ACCENT = QColor("#8fe45c")
WARN = QColor("#d9a04c")
OK_GREEN = QColor("#7dc963")
STATUS_RED = QColor("#e0554f")
STATUS_GREY = QColor("#7f8f78")
ITEM_GREEN = QColor("#e2ead8")
TEAL = QColor("#5db7a8")
LIGHT_GREY = QColor("#cfd9c6")

ANOMALY_MARKERS = ("AnomalyLauncher.exe", "fsgame.ltx")
GAMMA_MARKERS = ("ModOrganizer.exe", "ModOrganizer.ini")
#: Fallback MO2 profile name, matching the default used when a profile's own
#: form leaves the field blank (see profiles_page.py's ``_form_values``).
GAMMA_PROFILE = "G.A.M.M.A"

def normalize_path(raw: str) -> str:
    """Expand ``~`` and resolve to an absolute path.

    Typed manually (as opposed to via Browse, which already yields a clean
    absolute path), a value like ``~/Games/Anomaly`` or a relative path must
    be resolved here, in the GUI, before it ever reaches the CLI: the CLI
    treats ``~`` as a literal folder-name segment and resolves a relative
    value against its own subprocess cwd (unset by the launcher, so it can
    differ run to run) rather than expanding it - either way it would create
    the real install at a location neither this app nor the user intended,
    not just report a false "not installed".
    """
    value = raw.strip()
    if not value:
        return value
    return str(Path(value).expanduser().resolve())


def count_cached_archives(cache_path: str) -> int:
    """Count .zip archives in *cache_path*."""
    if not cache_path:
        return 0
    cache_dir = Path(cache_path)
    if not cache_dir.is_dir():
        return 0
    count = 0
    for _ in cache_dir.glob("*.zip"):
        count += 1
    return count


def update_cache_label(label: QLabel, cache_path: str) -> None:
    """Set *label* text and colour based on archive count."""
    if not cache_path:
        label.setText("")
        return
    count = count_cached_archives(cache_path)
    if count == 0:
        label.setText(tr("No archives cached"))
        label.setStyleSheet(f"color: {STATUS_RED.name()};")
    else:
        # Separate singular/plural keys instead of an English-only "s" suffix
        # appended to a translated noun: Romanian (and most languages) plural
        # by changing the word itself ("arhivă" -> "arhive"), not by adding a
        # suffix, so a shared "{count} archive{arg} cached" template could
        # never translate correctly for them.
        label.setText(
            tr("{count} archive cached", count=count)
            if count == 1
            else tr("{count} archives cached", count=count)
        )
        label.setStyleSheet(f"color: {OK_GREEN.name()};")


def format_playtime(total_seconds: float) -> str:
    """Format accumulated play time as e.g. "3h 24m", "12m", or "0m"."""
    total_minutes = int(total_seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def format_last_played(timestamp: float | None) -> str:
    """Format a last-played unix timestamp as a readable local date/time.

    Absolute (not "N minutes ago"): the Dashboard only re-renders this on
    explicit refreshes, not on a live ticking timer, so a relative string
    would silently go stale between refreshes.
    """
    if not timestamp:
        return tr("Never")
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%d/%m/%Y %H:%M")


_MO2_RUNNING_CACHE: float = 0.0
_MO2_RUNNING_RESULT: bool = False
_MO2_CACHE_TTL: float = 3.0


def mo2_running(*, force: bool = False) -> bool:
    """True when a Mod Organizer process (and so typically the game) is running.

    Mod Organizer stays alive while it runs the game through ``run -e``, so this
    is the reliable proxy for "the Wine prefix is in use".  Results are cached
    for a few seconds to avoid blocking the GUI thread repeatedly on passive
    UI polling. Pass ``force=True`` immediately before starting a write to a
    file MO2 also owns (modlist.txt) or a use of the Wine prefix (Verify
    Integrity, Winetricks) - the cache's TTL window is otherwise wide enough
    for MO2 to have just started without that action seeing it yet.
    """
    global _MO2_RUNNING_CACHE, _MO2_RUNNING_RESULT

    import time

    now = time.monotonic()
    if not force and now - _MO2_RUNNING_CACHE < _MO2_CACHE_TTL:
        return _MO2_RUNNING_RESULT
    exe = shutil.which("pgrep")
    if not exe:
        # Cache the "pgrep unavailable" answer too, or every caller re-probes
        # the PATH on each poll.
        _MO2_RUNNING_CACHE = now
        _MO2_RUNNING_RESULT = False
        return False
    try:
        proc = subprocess.run(
            # Case-insensitive: Wine/umu-run can report the running
            # process's own path in a different case than the literal
            # "ModOrganizer.exe" filename (e.g. fully lowercased) - a
            # bracket expression on just the first letter (the previous
            # form of this pattern) does not cover that. pgrep excludes
            # its own PID by default, so this cannot self-match.
            [exe, "-if", r"ModOrganizer\.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Cache the failure for the TTL as well; a missing/slow pgrep must
        # not block the GUI thread on every poll.
        _MO2_RUNNING_CACHE = now
        _MO2_RUNNING_RESULT = False
        return False
    _MO2_RUNNING_CACHE = now
    _MO2_RUNNING_RESULT = proc.returncode == 0
    return _MO2_RUNNING_RESULT


def mo2_pids() -> set[int]:
    """PIDs of currently running Mod Organizer processes, uncached.

    Used to tell "the MO2 instance this launch started" apart from an
    unrelated MO2 window the user already had open - ``mo2_running()``'s
    cached, name-only check cannot make that distinction, and answering it
    wrongly leaves the Play page's buttons disabled forever once the launch's
    own instance closes while a pre-existing one lingers.
    """
    exe = shutil.which("pgrep")
    if not exe:
        return set()
    try:
        proc = subprocess.run(
            # See mo2_running()'s matching pattern comment: case-insensitive
            # for the same reason.
            [exe, "-if", r"ModOrganizer\.exe"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    pids: set[int] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.add(int(line))
    return pids


def exe_pids(exe_name: str) -> set[int]:
    """PIDs of processes whose command line contains ``exe_name``, uncached.

    Used to detect the actual game executable (as opposed to Mod
    Organizer, which stays running after the game it launched exits) so
    playtime can be recorded when the game itself closes.

    Matches case-insensitively (``-i``), same as ``mo2_pids()``'s own
    ``[Mm]odOrganizer\\.exe`` pattern - the exe name comes from
    ModOrganizer.ini's ``[customExecutables]`` path, whose case is not
    guaranteed to match how Wine reports the running process's own
    command line.
    """
    exe = shutil.which("pgrep")
    if not exe:
        return set()
    try:
        proc = subprocess.run(
            [exe, "-if", re.escape(exe_name)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    pids: set[int] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.add(int(line))
    return pids


_click_sound_player = None
_click_sound_output = None


def play_click_sound() -> None:
    """Play the short launch-button click sound, best-effort.

    A missing asset or a broken audio backend must never block or crash
    a game launch - any failure here is silently swallowed.
    """
    global _click_sound_player, _click_sound_output
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

        from ..config import project_root

        if _click_sound_player is None:
            # WAV, not MP3: Qt's FFmpeg backend re-decodes from scratch on
            # every stop()+play() replay, and MP3's LAME encoder-delay/
            # padding metadata makes the mp3float decoder print "Could not
            # update timestamps for skipped samples" to stderr on a fast
            # restart of a very short clip. PCM has no such metadata, so
            # the warning class doesn't exist for it.
            path = project_root() / "commander_gui" / "assets" / "pda.wav"
            if not path.is_file():
                return
            _click_sound_output = QAudioOutput()
            _click_sound_output.setVolume(0.5)
            _click_sound_player = QMediaPlayer()
            _click_sound_player.setAudioOutput(_click_sound_output)
            _click_sound_player.setSource(QUrl.fromLocalFile(str(path)))
        _click_sound_player.stop()
        _click_sound_player.play()
    except Exception:  # noqa: BLE001, S110 - a broken sound must never block a launch
        pass


def anomaly_installed(path: str) -> bool:
    """True when the Anomaly game engine appears to be installed at ``path``."""
    base = Path(path)
    return base.is_dir() and any((base / m).is_file() for m in ANOMALY_MARKERS)


def _find_profile_dir(profiles: Path, mo2_profile: str) -> Path | None:
    """Case-insensitive match for an MO2 profile's on-disk folder name."""
    if not profiles.is_dir():
        return None
    wanted = (mo2_profile or GAMMA_PROFILE).upper()
    return next(
        (p for p in profiles.iterdir() if p.is_dir() and p.name.upper() == wanted),
        None,
    )


def gamma_installed(path: str, mo2_profile: str = GAMMA_PROFILE) -> bool:
    """True when a GAMMA Mod Organizer instance exists at ``path``.

    ``mo2_profile`` is the active profile's actual MO2 profile folder name -
    it defaults to the stock "G.A.M.M.A" only because callers that have no
    profile object handy (or an unset field) need some name to check; a
    profile with a custom MO2 profile name would otherwise always report
    "not installed" here even with a fully working install.
    """
    base = Path(path)
    if not base.is_dir() or not all((base / m).is_file() for m in GAMMA_MARKERS):
        return False
    return _find_profile_dir(base / "profiles", mo2_profile) is not None


def count_active_mods(
    gamma_dir: str, mo2_profile: str = GAMMA_PROFILE
) -> tuple[int, int] | None:
    """Return (enabled, total) mod counts for a profile's modlist.txt.

    None means "count unavailable" (GAMMA not installed, profile folder or
    modlist.txt missing/unreadable) - callers must treat that as a reason
    to hide the counter, not an error.
    """
    base = Path(gamma_dir)
    if not base.is_dir() or not all((base / m).is_file() for m in GAMMA_MARKERS):
        return None
    profile_dir = _find_profile_dir(base / "profiles", mo2_profile)
    if profile_dir is None:
        return None
    try:
        lines = read_lines(profile_dir / "modlist.txt")
    except (OSError, ValueError):
        return None
    return count_mods(lines, mods_dir=base / "mods")


class NoWheelComboBox(QComboBox):
    """A QComboBox that ignores mouse wheel events.

    Placed inside a scrolling page, a plain QComboBox silently changes its
    selection when the cursor happens to pass over it while the user is just
    scrolling the page - easy to trigger by accident and easy to miss, since
    nothing visually flags that the value changed. Ignoring the wheel event
    here lets it bubble up to the enclosing QScrollArea instead, so hovering
    the box while scrolling scrolls the page like everywhere else; the value
    can still be changed by clicking the dropdown as normal.
    """

    def wheelEvent(self, event) -> None:
        event.ignore()


def display_state(installed: bool, operation: str | None, key: str) -> bool | str:
    """Resolve a target's displayed install state.

    Returns ``"installing"`` while that target's operation is active,
    otherwise the filesystem-derived boolean.
    """
    if operation == key:
        return "installing"
    return installed


class InstallStatusRow(QWidget):
    """A coloured dot + status (Installed / Not installed / Unknown) for a target."""

    def __init__(
        self,
        name: str,
        detail: str = "",
        ok: bool | None = None,
        pending_text: str = "Unknown",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._pending_text = pending_text
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._dot = QLabel(tr("●"))
        self._dot.setFixedWidth(24)
        self._status = QLabel(tr("Unknown"))
        self._detail = QLabel(detail)
        self._detail.setObjectName("info")
        self._detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        row.addWidget(self._dot)
        row.addWidget(self._status)
        if name:
            name_lbl = QLabel(tr(name))
            name_lbl.setObjectName("dim")
            row.addSpacing(6)
            row.addWidget(name_lbl)
        row.addWidget(self._detail)
        row.addStretch(1)
        self.set_state(ok, detail)

    def set_state(
        self, ok: bool | None, detail: str = "", pending_text: str | None = None
    ) -> None:
        self._detail.setText(detail)
        self._detail.setVisible(bool(detail))
        if ok is True:
            color = OK_GREEN.name()
            text = tr("Installed")
        elif ok is False:
            color = STATUS_RED.name()
            text = tr("Not installed")
        else:
            color = STATUS_GREY.name()
            text = pending_text if pending_text is not None else self._pending_text
        self._status.setText(text)
        self._dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        self._status.setStyleSheet(f"color: {color};")

    def set_installing(self, detail: str = "") -> None:
        """Show the transient orange state used while an install is running."""
        color = WARN.name()
        self._detail.setText(detail)
        self._detail.setVisible(bool(detail))
        self._status.setText(tr("Installing"))
        self._dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        self._status.setStyleSheet(f"color: {color};")

    def set_incomplete(self, detail: str = "") -> None:
        """Show the amber "incomplete" state - present on disk, but the last

        install attempt failed and never finished (see gamma_install_resume)
        - distinct from the green "Installed" state, which would otherwise
        look identical for a partial and a fully completed install.
        """
        color = WARN.name()
        self._detail.setText(detail)
        self._detail.setVisible(bool(detail))
        self._status.setText(tr("Incomplete"))
        self._dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        self._status.setStyleSheet(f"color: {color};")

    def set_status_tooltip(self, text: str) -> None:
        """Show the same status details when hovering any part of the row."""
        for widget in (self, self._dot, self._status, self._detail):
            widget.setToolTip(text)


_ACTIVE_RUNNERS: set[CommandRunner] = set()
_ACTIVE_TASKS: set[QObject] = set()
_SHUTTING_DOWN = False


def begin_shutdown() -> None:
    """Stop background results from reaching UI handlers during app teardown."""
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = True


def resume_after_shutdown() -> None:
    """Re-arm background task result delivery after a mid-session teardown.

    ``begin_shutdown()``/``shutdown_active_runners()`` exist for app exit,
    where the process ends right after and the flag never needs to flip
    back. A live UI rebuild (e.g. changing the language without
    restarting) reuses the exact same "stop stale results from touching
    about-to-be-destroyed widgets" mechanism, but the app keeps running -
    call this once the old pages have been torn down and the new ones
    built, so their own background tasks work normally again.
    """
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = False


def shutdown_active_runners(timeout_ms: int = 5000) -> None:
    """Cancel and wait for all active command threads (called on app quit).

    ``timeout_ms`` is a budget shared across every active runner/task, not
    a fresh allowance handed to each one - a page that starts several
    background checks at once (dashboard's size scan, its update check,
    a dependency probe, ...) previously cost ``timeout_ms`` *per task*
    here, so e.g. 6 still-in-flight tasks made a 2000ms caller (a
    language switch) take a full 12 real seconds, and a 30000ms caller
    (app quit) could take minutes. A single deadline, with each
    remaining-time slice shrinking as it goes, keeps the real wait
    bounded by what the caller actually asked for.
    """
    begin_shutdown()
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    for runner in list(_ACTIVE_RUNNERS):
        remaining_ms = max(0, round((deadline - time.monotonic()) * 1000))
        try:
            runner.shutdown(timeout_ms=remaining_ms)
        except Exception:  # noqa: BLE001, S110
            pass
    for task in list(_ACTIVE_TASKS):
        remaining_ms = max(0, round((deadline - time.monotonic()) * 1000))
        shutdown = getattr(task, "shutdown", None)
        if shutdown is not None:
            try:
                shutdown(timeout_ms=remaining_ms)
            except Exception:  # noqa: BLE001, S110
                pass


def instance_window_title(title: str) -> str:
    """Mark ``title`` when this process is not the first COMMANDER running.

    Several COMMANDERs can share one set of settings when a from-source
    launch opts out of the single-instance rule (see
    ``config.multiple_instances_allowed``). They all write the same files on
    a last-write-wins basis, so which window is the spare needs to be
    obvious at a glance rather than inferred.

    Idempotent: the windows call this from a ``setWindowTitle`` override, and
    a title round-tripped through ``windowTitle()`` must not collect the
    suffix twice.
    """
    if not is_secondary_instance():
        return title
    suffix = "  " + tr("[extra instance]")
    return title if title.endswith(suffix) else title + suffix


def cancel_active_runners() -> None:
    """Cancel background work without waiting for any of it to finish.

    For the ``execve`` handoff into (or out of) Steam Deck Mode, where
    ``shutdown_active_runners()`` is the wrong tool: its wait exists because
    app quit destroys ``QThread`` objects, and Qt answers the destruction of
    a still-running one with ``qFatal``. ``execve`` replaces the whole
    process image in one step - no destructor runs, no ``QThread`` is
    destroyed - so that hazard cannot arise, and a ``BackgroundTask`` running
    an uninterruptible Python callable would otherwise hold the switch for
    the full timeout while the user stares at a closed window.

    Cancelling still matters for ``CommandRunner``: it signals the child's
    process group, so a CLI subprocess is asked to die rather than being
    orphaned by the exec. That call does not block - it signals and leaves a
    daemon thread to follow up - so this returns immediately.
    """
    begin_shutdown()
    for runner in list(_ACTIVE_RUNNERS):
        try:
            runner.cancel()
        except Exception:  # noqa: BLE001, S110 - nothing here may block a restart
            pass
    for task in list(_ACTIVE_TASKS):
        cancel = getattr(task, "cancel", None)
        if cancel is None:
            continue
        try:
            cancel()
        except Exception:  # noqa: BLE001, S110
            pass


def _detach_unfinished_task(task: QObject) -> None:
    """Cut a still-running task loose from the widget that owns it.

    ``BackgroundTask``/``StreamTask`` run a plain Python callable that cannot
    be force-killed, so ``shutdown()``'s wait is bounded and can expire with
    the worker thread still running. The task is parented to the page that
    started it, and its ``QThread`` is parented to the task in turn - so a
    caller that tears those pages down right after shutting the tasks down
    (``MainWindow.switch_language()`` deleting the whole central widget) would
    destroy a running ``QThread``, which Qt answers with a ``qFatal`` abort,
    killing the app outright.

    Re-parenting to nothing takes the task out of that destruction chain;
    ``_ACTIVE_TASKS`` keeps it alive until ``QThread.finished`` arrives. Its
    results are dropped from here on: the widgets that asked for them are
    being destroyed, and ``_SHUTTING_DOWN`` alone cannot suppress them because
    a mid-session rebuild clears that flag again (``resume_after_shutdown``).
    """
    task._abandoned = True
    task.setParent(None)


class CommandRunner(QObject):
    """Runs a CLI command on a background thread, streaming output lines."""

    line = Signal(str)
    finished = Signal(int, str)
    cancelled = Signal()

    def __init__(
        self,
        command: list[str],
        cwd: str = "",
        env: dict[str, str] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._command = command
        self._cwd = cwd
        self._env = env
        self._thread: QThread | None = None
        self._worker = None
        self._cancel_requested = False

    @property
    def was_cancelled(self) -> bool:
        """True once :meth:`cancel` was requested for the current run.

        ``finished`` is still emitted after a cancel (the process exits in
        response to the signal), so handlers that chain a *next* step must
        check this before continuing.
        """
        return self._cancel_requested

    def start(self) -> None:
        from ..cli_runner import CliWorker  # deferred import avoids cycle

        if self._thread is not None and self._thread.isRunning():
            # Re-entry would orphan the in-flight worker and its process
            # group; cancel()/kill() would only reach the newest worker.
            return
        self._cancel_requested = False
        thread = QThread(self)
        worker = CliWorker()
        worker.setup(self._command, self._cwd, self._env)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.line_ready.connect(self._on_line)
        worker.finished.connect(self._on_finished)
        thread.finished.connect(self._on_thread_finished)
        self._thread = thread
        self._worker = worker
        _ACTIVE_RUNNERS.add(self)
        thread.start()

    def cancel(self) -> None:
        if self._worker is not None:
            self._cancel_requested = True
            self._worker.cancel()
            # Only emit when a run is actually in flight; otherwise handlers
            # would clobber an already-finished UI state.
            running = self._thread is not None and self._thread.isRunning()
            if running and not _SHUTTING_DOWN:
                self.cancelled.emit()

    def pause(self) -> None:
        """SIGSTOP the child process to freeze it in place."""
        if self._worker is not None:
            self._worker.pause()

    def resume(self) -> None:
        """SIGCONT the child process to resume from where it was stopped."""
        if self._worker is not None:
            self._worker.resume()

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Cancel a running command and wait for its thread to finish."""
        if not self.is_running():
            _ACTIVE_RUNNERS.discard(self)
            return
        self.cancel()
        thread = self._thread
        if thread is not None:
            thread.quit()
            if not thread.wait(timeout_ms):
                if self._worker is not None:
                    self._worker.kill()
                if not thread.wait(3000):
                    return
        if thread.isRunning():
            return

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _on_finished(self, rc: int, output: str) -> None:
        thread = self._thread
        if thread is not None:
            thread.quit()
        if not _SHUTTING_DOWN:
            self.finished.emit(rc, output)

    def _on_line(self, line: str) -> None:
        if not _SHUTTING_DOWN:
            self.line.emit(line)

    def _on_thread_finished(self, thread: QThread | None = None) -> None:
        # The worker thread has fully stopped; it is now safe to release the
        # worker and thread objects. Dropping the Python references earlier (as
        # the old deleteLater setup did) double-frees the C++ objects, because a
        # queued DeferredDelete in the worker thread still pointed at them.
        # The runner also stays in _ACTIVE_RUNNERS until this point so it is not
        # garbage-collected (destroying its QThread) while the thread still runs.
        if thread is not None and self._thread is not thread:
            return
        _ACTIVE_RUNNERS.discard(self)
        self._worker = None
        self._thread = None


class _Worker(QObject):
    """Generic worker that runs ``fn`` and emits result/error signals."""

    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, fn, *args, **kwargs) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            self.result.emit(self._fn(*self._args, **self._kwargs))
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class BackgroundTask(QObject):
    """Run a plain Python callable on a worker thread, emit its result."""

    result = Signal(object)
    error = Signal(str)

    def __init__(self, fn, *args, parent: QObject | None = None, **kwargs) -> None:
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._cancel_event = threading.Event()
        self._abandoned = False

    def start(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
            self._worker = None
        self._abandoned = False
        self._thread = QThread(self)
        self._worker = _Worker(self._fn, *self._args, **self._kwargs)
        self._worker.moveToThread(self._thread)
        self._worker.result.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._thread.started.connect(self._worker.run)
        self._thread.finished.connect(
            lambda thread=self._thread: self._on_thread_finished(thread)
        )
        _ACTIVE_TASKS.add(self)
        self._thread.start()

    @property
    def cancel_event(self) -> threading.Event:
        """Cancellation token for callables that support cooperative cancel."""
        return self._cancel_event

    def cancel(self) -> None:
        """Request cooperative cancellation of the running callable."""
        self._cancel_event.set()

    def _on_result(self, result) -> None:
        if not _SHUTTING_DOWN and not self._abandoned:
            self.result.emit(result)

    def _on_error(self, message: str) -> None:
        if not _SHUTTING_DOWN and not self._abandoned:
            self.error.emit(message)

    def _on_thread_finished(self, thread: QThread) -> None:
        if self._thread is not thread:
            return
        _ACTIVE_TASKS.discard(self)
        self._worker = None
        self._thread = None

    def shutdown(self, timeout_ms: int = 5000) -> None:
        self.cancel()
        thread = self._thread
        if thread is None:
            _ACTIVE_TASKS.discard(self)
            return
        if not thread.isRunning():
            self._on_thread_finished(thread)
            return
        # A Python callable cannot safely be force-killed. Keep all references
        # and the active-task entry until QThread.finished if it outlives this
        # bounded wait.
        if thread.wait(max(0, timeout_ms)):
            self._on_thread_finished(thread)
            return
        _detach_unfinished_task(self)


def activate_profile(window, parent: QWidget, name: str, on_done=None) -> BackgroundTask:
    """Switch the CLI's active profile to ``name``, asynchronously.

    Runs ``config use <name>`` via the CLI, then refreshes ``window``'s
    settings and verifies the switch actually took effect. Shared by the
    Profiles page's "Set active" button and the Dashboard's inline
    profile switcher - callers are responsible for their own busy-guard
    and any "game running" confirmation before calling this (see
    ``ProfilesPage._set_active`` for that pattern). ``on_done(True)``
    fires on confirmed success, ``on_done(False)`` on any failure -
    callers use it to restore UI state (re-enable buttons, revert a
    combo selection, etc). Returns the ``BackgroundTask`` so a caller
    that tracks its own in-flight task (e.g. for a busy guard) can keep
    a reference to it.
    """
    from ..settings import cli_ok, run_config_command

    def _finish(result) -> None:
        rc, out, err = result
        if not cli_ok(rc, out, err):
            QMessageBox.warning(
                parent, tr("Failed"), (out + "\n" + err).strip() or "config use failed"
            )
            if on_done is not None:
                on_done(False)
            return
        window.refresh_settings()
        active_now = window.settings.active_profile
        if active_now is None or active_now.profile_name != name:
            QMessageBox.warning(
                parent, tr("Failed"), tr("Profile '{name}' could not be activated.", name=name)
            )
            if on_done is not None:
                on_done(False)
            return
        if on_done is not None:
            on_done(True)

    def _error(msg: str) -> None:
        QMessageBox.warning(parent, tr("Error"), msg)
        if on_done is not None:
            on_done(False)

    task = BackgroundTask(run_config_command, ["use", name], timeout=300, parent=parent)
    task.result.connect(_finish)
    task.error.connect(_error)
    task.start()
    return task


class _StreamWorker(QObject):
    """Worker that runs ``fn(report)`` and emits line/result/error signals."""

    line = Signal(str)
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.result.emit(self._fn(self.line.emit))
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class StreamTask(QObject):
    """Run ``fn(report)`` on a worker thread, streaming ``report(text)`` lines.

    ``fn`` receives a callable and should call it with progress/status text as
    it works; each call is emitted on the ``line`` signal. The return value of
    ``fn`` is emitted on ``result``.
    """

    line = Signal(str)
    result = Signal(object)
    error = Signal(str)

    def __init__(self, fn, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fn = fn
        self._cancel_event = threading.Event()
        self._thread: QThread | None = None
        self._worker: _StreamWorker | None = None
        self._abandoned = False

    def start(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
            self._worker = None
        self._cancel_event.clear()
        self._abandoned = False
        self._thread = QThread(self)
        self._worker = _StreamWorker(self._fn)
        self._worker.moveToThread(self._thread)
        self._worker.line.connect(self._on_line)
        self._worker.result.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._thread.started.connect(self._worker.run)
        self._thread.finished.connect(
            lambda thread=self._thread: self._on_thread_finished(thread)
        )
        _ACTIVE_TASKS.add(self)
        self._thread.start()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    def cancel(self) -> None:
        self._cancel_event.set()

    def _on_line(self, line: str) -> None:
        if not _SHUTTING_DOWN and not self._abandoned:
            self.line.emit(line)

    def _on_result(self, result) -> None:
        if not _SHUTTING_DOWN and not self._abandoned:
            self.result.emit(result)

    def _on_error(self, message: str) -> None:
        if not _SHUTTING_DOWN and not self._abandoned:
            self.error.emit(message)

    def _on_thread_finished(self, thread: QThread) -> None:
        if self._thread is not thread:
            return
        _ACTIVE_TASKS.discard(self)
        self._worker = None
        self._thread = None

    def shutdown(self, timeout_ms: int = 5000) -> None:
        self.cancel()
        thread = self._thread
        if thread is None:
            _ACTIVE_TASKS.discard(self)
            return
        if not thread.isRunning():
            self._on_thread_finished(thread)
            return
        # Retain the worker and thread when cooperative cancellation exceeds
        # the timeout; releasing either while the thread runs is unsafe.
        if thread.wait(max(0, timeout_ms)):
            self._on_thread_finished(thread)
            return
        _detach_unfinished_task(self)


class OutputPane(QFrame):
    """A read-only console-style log view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QPlainTextEdit

        self.edit = QPlainTextEdit(self)
        self.edit.setReadOnly(True)
        self.edit.setMaximumBlockCount(20000)
        self.edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.edit.setWordWrapMode(
            __import__(
                "PySide6.QtGui", fromlist=["QTextOption"]
            ).QTextOption.WrapMode.WrapAnywhere
        )
        self.edit.setHorizontalScrollBarPolicy(
            __import__(
                "PySide6.QtCore", fromlist=["Qt"]
            ).Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)

    def append_line(self, text: str) -> None:
        self.edit.appendPlainText(strip_ansi(text))

    def clear(self) -> None:
        self.edit.clear()


def make_card(
    parent: QWidget | None = None, *, expand: bool = False
) -> tuple[QFrame, QVBoxLayout]:
    """Create a titled card container. Returns (frame, inner layout)."""
    from PySide6.QtWidgets import QSizePolicy

    frame = QFrame(parent)
    frame.setObjectName("card")
    if expand:
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(10)
    return frame, layout


def install_hover_grow_text(
    button: QPushButton,
    color_token: str,
    *,
    scale: float = 1.12,
    duration: int = 150,
) -> None:
    """Grow a button's label text on hover, purely decorative.

    Used for the Dashboard's "Play GAMMA" and Play page's "Launch Game"
    buttons - matches the same hover-grow treatment already applied to the
    nav tabs, without touching either button's own click handling, enabled
    state, or styling at rest.

    The label is drawn by an overlay QLabel positioned over the button (the
    button's own text is cleared) rather than by growing the button's own
    font directly. Both of these buttons have a Minimum/Fixed size policy
    from their layout (one stretches to fill a card's width, matching
    QPushButton's default), and growing a widget's real font changes its
    size hint - risking a small layout shift in either direction as the
    animation plays. Painting the bigger text on a same-size overlay avoids
    that entirely: the button's own geometry, background and border (and
    its QSS :hover background-color change) are completely unaffected.
    """
    from ..themes import active_theme_tokens

    text = button.text()
    button.setText("")

    overlay = QLabel(text, button)
    overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    overlay.setGeometry(button.rect())

    base_font = QFont(button.font())
    state = {"scale": 1.0}

    def apply_scale(value: float) -> None:
        state["scale"] = value
        font = QFont(base_font)
        pixel_size = base_font.pixelSize()
        if pixel_size > 0:
            font.setPixelSize(max(1, round(pixel_size * value)))
        else:
            font.setPointSizeF(max(1.0, base_font.pointSizeF() * value))
        overlay.setFont(font)
        color = active_theme_tokens().get(color_token, "#ffffff")
        overlay.setStyleSheet(f"background: transparent; border: none; color: {color};")

    apply_scale(1.0)

    anim = QVariantAnimation(button)
    anim.valueChanged.connect(apply_scale)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def start(target: float) -> None:
        anim.stop()
        anim.setDuration(duration)
        anim.setStartValue(state["scale"])
        anim.setEndValue(target)
        anim.start()

    class _HoverGrowFilter(QObject):
        def eventFilter(self, _obj: QObject, event) -> bool:
            if event.type() == QEvent.Type.Enter:
                start(scale)
            elif event.type() == QEvent.Type.Leave:
                start(1.0)
            elif event.type() == QEvent.Type.Resize:
                overlay.setGeometry(button.rect())
            return False

    hover_filter = _HoverGrowFilter(button)
    button.installEventFilter(hover_filter)
    # Keep every piece alive for the button's lifetime - nothing else
    # references them, and Python would otherwise garbage-collect them.
    button._hover_grow_overlay = overlay
    button._hover_grow_filter = hover_filter
    button._hover_grow_anim = anim


def set_hover_grow_text(button: QPushButton, text: str) -> None:
    """Change the visible text of a button set up via install_hover_grow_text().

    That function clears the button's own .text() permanently and paints
    an overlay QLabel instead (see its docstring) - calling
    button.setText() afterward has no visible effect (the overlay hides
    it) while also silently re-populating the button's real, supposedly-
    empty text, so a later hover-grow animation would show both the
    overlay's old text and the button's own new text at once. This
    updates the overlay instead, which is what's actually visible.
    """
    overlay = getattr(button, "_hover_grow_overlay", None)
    if overlay is not None:
        overlay.setText(text)
    else:
        button.setText(text)


def clear_layout(layout) -> None:
    """Remove and delete every item (widgets and nested layouts) in a layout.

    Deleting a sub-layout via ``takeAt`` alone leaks the widgets inside it, which
    then keep their old geometry and render on top of freshly added rows.
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            clear_layout(child)
            child.deleteLater()


def section_label(text: str, *, level: int = 1) -> QLabel:
    label = QLabel(text)
    label.setObjectName(f"section{level}")
    return label


def info_label(text: str, *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(wrap)
    label.setObjectName("info")
    return label


def make_header_row(title: str, status: QWidget | None = None) -> QHBoxLayout:
    """A card column's title, with its status badge (if any) pushed to

    the top-right of the same row instead of its own separate row above
    the title. Shared by every page that lays two columns of content
    side by side (Install page's Anomaly/GAMMA card, Updates page's
    Installed/Latest Available card, ...).
    """
    row = QHBoxLayout()
    row.addWidget(section_label(tr(title), level=2))
    row.addStretch(1)
    if status is not None:
        row.addWidget(status)
    return row


def assistant_token(text: str = "ASSISTANT") -> str:
    """Return *text* as a highlighted, link-styled HTML span.

    Used to call out the ASSISTANT support companion tool. The span reads as
    a link so it can later be wrapped in an ``<a href=...>`` for a GitHub link.
    """
    from ..themes import active_theme_tokens

    accent = active_theme_tokens().get("accent", "#9fe96f")
    return (
        f"<span style='color:{accent}; text-decoration:underline;"
        f" font-weight:bold;'>{text}</span>"
    )


def _kv_row(label: str, value: str) -> QHBoxLayout:
    """Key-value row: dim label on the left, selectable value on the right."""
    row = QHBoxLayout()
    key = QLabel(label)
    key.setObjectName("dim")
    key.setAlignment(Qt.AlignmentFlag.AlignTop)
    val = QLabel(value)
    val.setWordWrap(True)
    val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    row.addWidget(key, 0, Qt.AlignmentFlag.AlignTop)
    row.addWidget(val, 1)
    return row


#: Re-exported so UI code keeps a single, obvious import site for sizes.
human_size = format_size


def winetricks_tooltip(status: dict[str, bool]) -> str:
    """Rich-text bullet list of tool availability and per-verb winetricks state."""
    rows: list[str] = []
    # Tools section (wine, protontricks) — only shown when present in status.
    tool_keys = [("wine", "Wine"), ("protontricks", "Protontricks"), ("umu", "umu-run")]
    tool_items = [
        (label, status.get(key, False)) for key, label in tool_keys if key in status
    ]
    if tool_items:
        rows.append(f"<b>{tr('Tools')}</b>")
        for label, ok in tool_items:
            color = OK_GREEN.name() if ok else STATUS_RED.name()
            state = tr("installed") if ok else tr("missing")
            rows.append(
                f"<span style='color:{color}'>&#9679;</span> "
                f"<span style='color:{color}'>{label} - {state}</span>"
            )
    # Runtimes section (winetricks verbs).
    rows.append(f"<b>{tr('Runtimes')}</b>")
    for verb in WINETRICKS_VERBS:
        ok = status.get(verb, False)
        color = OK_GREEN.name() if ok else STATUS_RED.name()
        state = tr("installed") if ok else tr("missing")
        rows.append(
            f"<span style='color:{color}'>&#9679;</span> "
            f"<span style='color:{color}'>{verb} - {state}</span>"
        )
    return "<br>".join(rows)


def dir_size(path: str | Path, *, max_entries: int = 200_000) -> int:
    """Best-effort total size of a directory tree.

    Stops after ``max_entries`` files so a huge or runaway tree cannot stall
    the caller indefinitely (GAMMA installs hold 100k+ files).
    """
    total = 0
    try:
        for count, entry in enumerate(Path(path).rglob("*")):
            if count >= max_entries:
                break
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        pass
    return total


def free_space_bytes(path: str | Path) -> int | None:
    """Free space on the filesystem that would hold ``path``.

    ``path`` (e.g. a profile's Anomaly/GAMMA folder) need not exist yet -
    walks up to the nearest existing ancestor first, since a fresh
    install's target folders are typically only created after the user
    confirms. Returns ``None`` on any ``OSError`` (missing permissions, an
    unmounted path, ...) rather than raising - a failed check must never
    block the caller (e.g. an install confirmation dialog) from proceeding.
    """
    candidate = Path(path).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        pass
    for ancestor in (candidate, *candidate.parents):
        if ancestor.exists():
            try:
                return shutil.disk_usage(ancestor).free
            except OSError:
                return None
    return None


def crash_dump_names(anomaly_path: str | Path) -> set[str]:
    """Filenames of X-Ray crash minidumps (``.mdmp``) in the Anomaly

    install's log folder. X-Ray's own crash handler reliably writes one
    of these on any native engine crash, regardless of how the game was
    launched (MO2-mediated or direct) - a simpler, more reliable signal
    than trying to track the actual game process's exit code, which this
    app cannot do at all for the MO2-mediated launch path.
    """
    logs_dir = Path(anomaly_path).expanduser() / "appdata" / "logs"
    try:
        return {p.name for p in logs_dir.glob("*.mdmp")}
    except OSError:
        return set()


def open_in_file_manager(path: str | Path) -> bool:
    """Open ``path`` in the file manager. Returns True if a launcher was started.

    On Plasma, ``xdg-open`` routes through ``kde-open5``/kio which can return
    success without opening anything, so Dolphin is launched directly (with
    ``--new-window`` to avoid being absorbed into a running instance).
    """
    path = str(path)
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
    command: list[str] | None = None

    if shutil.which("dolphin") and ("kde" in desktop or "plasma" in desktop):
        command = [shutil.which("dolphin"), "--new-window", path]
    else:
        for opener in ("nautilus", "nemo", "thunar"):
            exe = shutil.which(opener)
            if exe:
                command = [exe, path]
                break
        if command is None:
            for opener in ("gio", "xdg-open", "gnome-open", "kde-open5", "open"):
                exe = shutil.which(opener)
                if exe:
                    command = [exe, "open", path] if opener == "gio" else [exe, path]
                    break
        if command is None and shutil.which("explorer.exe"):
            wsl_path = path
            wslpath = shutil.which("wslpath")
            if wslpath:
                try:
                    wsl_path = subprocess.run(
                        [wslpath, "-w", str(path)],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    ).stdout.strip()
                except (OSError, subprocess.TimeoutExpired):
                    wsl_path = str(path).replace("/", "\\")
            else:
                wsl_path = str(path).replace("/", "\\")
            command = ["explorer.exe", wsl_path]

    if command is None:
        return False
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def notify_desktop(title: str, message: str) -> None:
    """Best-effort desktop notification for a long task finishing.

    GAMMA installs/updates can run for a long time - if the user alt-tabs
    away, this is the only way they find out it's done without coming
    back to check. Uses notify-send (present on virtually every Linux
    desktop via libnotify) rather than a persistent QSystemTrayIcon, so
    nothing new appears in the tray. Silently does nothing if it isn't
    available - a missing notification must never affect the operation
    that just completed.
    """
    exe = shutil.which("notify-send")
    if exe is None:
        return
    try:
        subprocess.Popen(
            [exe, "--app-name=STALKER COMMANDER", title, message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


class ProgressTable(QTableWidget):
    """Live table of per-addon install progress."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, 3, parent)
        self.setHorizontalHeaderLabels(["Addon", "Operation", "Percent"])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().setSectionResizeMode(
            0, self.horizontalHeader().ResizeMode.Stretch
        )
        self.horizontalHeader().setSectionResizeMode(
            1, self.horizontalHeader().ResizeMode.ResizeToContents
        )
        self.horizontalHeader().setSectionResizeMode(
            2, self.horizontalHeader().ResizeMode.ResizeToContents
        )
        self.setSortingEnabled(False)
        # OrderedDict, not dict: move_to_end() on every touch in upsert()
        # turns iteration order into least-recently-touched-first, which
        # finish_stale_by_concurrency() relies on to evict the *oldest*
        # still-open rows first.
        self._rows: OrderedDict[str, int] = OrderedDict()
        # Matches the profile default of 6 download threads (see
        # set_concurrency()) until a real install configures the actual
        # value.
        self._concurrency_cap = 10

    def reset(self) -> None:
        self.setRowCount(0)
        self._rows.clear()

    def set_concurrency(self, threads: int) -> None:
        """Set how many rows can plausibly be in flight at once.

        With only N download threads, at most roughly N mods can
        genuinely be downloading/extracting concurrently - the +4 buffer
        allows for normal overlap between pipeline stages (e.g. one item
        finishing Download while another starts Extract). Anything beyond
        this count showing as non-terminal must be stale, not still
        running - see finish_stale_by_concurrency().
        """
        self._concurrency_cap = max(1, threads) + 4

    def upsert(self, event: ProgressEvent) -> None:
        row = self._rows.get(event.name)
        if row is None:
            row = self.rowCount()
            self.insertRow(row)
            self._rows[event.name] = row
            self.setItem(row, 0, QTableWidgetItem(event.name))
            self.setItem(row, 1, QTableWidgetItem(event.operation))
            self.setItem(row, 2, QTableWidgetItem(f"{event.percent:.1%}"))
        else:
            op = self.item(row, 1)
            pct = self.item(row, 2)
            if op is None or pct is None:
                return
            op.setText(event.operation)
            pct.setText(f"{event.percent:.1%}")
            self._rows.move_to_end(event.name)

        if event.operation == "Skipped" or event.percent >= 1.0:
            self.item(row, 1).setForeground(QColor(OK_GREEN.name()))
            self.item(row, 2).setForeground(QColor(OK_GREEN.name()))
            # Done - hide instead of leaving a finished archive's row (and,
            # for anything whose very last line never cleanly hit 100%, its
            # stale operation/percent) cluttering the list indefinitely.
            self.setRowHidden(row, True)
        elif event.operation == "Check MD5":
            self.item(row, 1).setForeground(QColor(TEAL.name()))
        else:
            self.item(row, 1).setForeground(QColor(LIGHT_GREY.name()))
            self.item(row, 2).setForeground(QColor(LIGHT_GREY.name()))

    def _finish_rows(self, names) -> None:
        """Shared "force to Complete/100%, green, hidden" body."""
        for name in names:
            row = self._rows.get(name)
            if row is None or self.isRowHidden(row):
                continue
            op = self.item(row, 1)
            pct = self.item(row, 2)
            if op is None or pct is None:
                continue
            op.setText(tr("Complete"))
            pct.setText(tr("100.0%"))
            op.setForeground(QColor(OK_GREEN.name()))
            pct.setForeground(QColor(OK_GREEN.name()))
            self.setRowHidden(row, True)

    def finish_all(self) -> None:
        """Mark every remaining row as 100% complete (used when a run ends)."""
        self._finish_rows(list(self._rows.keys()))

    def finish_all_except(self, keep_names: frozenset[str]) -> None:
        """Force-complete and hide every row not in ``keep_names``.

        Used once the CLI's own [complete/total] counter proves every item
        still not finished must be one of the still-pending heavy repos in
        ``keep_names`` - so any other row still lingering as non-terminal
        (its own last line never cleanly signalled completion) is safe to
        force-finish and hide instead of leaving it stuck.
        """
        self._finish_rows(
            name for name in self._rows if name.strip().lower() not in keep_names
        )

    def finish_stale_by_concurrency(self, keep_names: frozenset[str]) -> None:
        """Force-complete the oldest visible rows beyond the concurrency cap.

        With only a handful of download threads, at most roughly that many
        ordinary rows can genuinely be in flight at once - covers the
        mid-install case finish_all_except() cannot: a row whose own last
        line never cleanly reached 100%/Skipped, well before the CLI's
        [complete/total] counter is anywhere near the end of the run.
        """
        visible = [
            name
            for name in self._rows
            if name.strip().lower() not in keep_names
            and not self.isRowHidden(self._rows[name])
        ]
        excess = len(visible) - self._concurrency_cap
        if excess > 0:
            self._finish_rows(visible[:excess])

    def mark_interrupted(self) -> None:
        """Relabel any row still non-terminal when a run ends without success.

        The row stays visible (it did not actually finish - hiding it
        would be dishonest), but its stale operation/percent no longer
        looks like it is silently still running once the whole process
        has actually stopped or been cancelled.
        """
        for row in set(self._rows.values()):
            if self.isRowHidden(row):
                continue
            op = self.item(row, 1)
            if op is None or op.text() == tr("Complete"):
                continue
            op.setText(tr("Interrupted"))
            op.setForeground(QColor(WARN.name()))


def progress_value(complete: int, total: int) -> int:
    """Convert aggregate completed/total mod counts to a bounded percentage."""
    if total <= 0:
        return 0
    return max(0, min(100, round(complete / total * 100)))


#: The three git-cloned "special repos" (see
#: install_page._looks_like_special_repo_clone_failure) - confirmed via a
#: real captured install log to take the large majority of total
#: wall-clock install time despite being only 3 of ~577 total items.
_HEAVY_ARCHIVE_NAMES = frozenset(
    {"stalker_gamma", "gamma_setup", "gamma_large_files_v2"}
)
#: Collective share of the overall bar reserved for those 3 items, so the
#: bar can't race to ~99% purely by finishing hundreds of small archives
#: while the real bottleneck (the heavy clones) hasn't even started.
_HEAVY_ARCHIVE_SHARE = 0.5

#: Minimum seconds between status-label repaints during a GAMMA install -
#: with several archives downloading at once the raw line rate can flip
#: this label several times a second; holding each update for a full
#: second keeps it readable instead of flickering.
_STATUS_LABEL_THROTTLE_S = 1.0


def _heavy_item_phase_value(operation: str, percent: float) -> float:
    """Fold a heavy item's own Download/Extract percent into one 0..1
    completion fraction for that item (clone = first half, checkout =
    second half - the only two operations seen for these repos)."""
    pct = max(0.0, min(1.0, percent))
    if operation == "Download":
        return pct * 0.5
    if operation == "Extract":
        return 0.5 + pct * 0.5
    return pct


def aggregate_progress_value(
    complete: int,
    total: int,
    percent: float,
    name: str = "",
    heavy_progress: dict[str, float] | None = None,
    operation: str = "",
) -> int:
    """Return overall progress using the CLI counter and current item fraction.

    Once a known-heavy git-cloned repo (see ``_HEAVY_ARCHIVE_NAMES``) has
    reported progress, its collective share of the bar is fixed at
    ``_HEAVY_ARCHIVE_SHARE`` instead of the flat ``1/total`` every other
    item gets - otherwise those 3 items, which dominate real install time,
    would only ever move the bar by a fraction of a percent each.
    """
    if total <= 0:
        return 0
    current = max(0.0, min(1.0, percent))
    heavy_progress = {} if heavy_progress is None else heavy_progress
    key = name.strip().lower()
    if key in _HEAVY_ARCHIVE_NAMES:
        heavy_progress[key] = _heavy_item_phase_value(operation, current)
    if not heavy_progress:
        completed_before_current = max(0, complete - 1)
        fraction = (completed_before_current + current) / total
        return max(0, min(100, round(fraction * 100)))

    heavy_fraction = sum(heavy_progress.values()) / len(_HEAVY_ARCHIVE_NAMES)
    light_total = max(1, total - len(_HEAVY_ARCHIVE_NAMES))
    heavy_done = sum(1 for p in heavy_progress.values() if p >= 1.0)
    completed_light = max(0, complete - 1 - heavy_done)
    light_current = 0.0 if key in _HEAVY_ARCHIVE_NAMES else current
    light_fraction = min(1.0, (completed_light + light_current) / light_total)
    overall = (
        (1 - _HEAVY_ARCHIVE_SHARE) * light_fraction
        + _HEAVY_ARCHIVE_SHARE * heavy_fraction
    )
    return max(0, min(100, round(overall * 100)))


def single_file_progress(operation: str, percent: float) -> int:
    """Per-phase progress for single-file installs.

    Download tracks the true percent so the bar matches the reported
    download size exactly; later phases map into fixed tail ranges and are
    kept for any numeric source that appears upstream.
    """
    pct = max(0, min(100, round(percent * 100)))
    if operation == "Download":
        return pct
    stages = {
        "Extract": (50, 85),
        "Expand": (85, 95),
        "Check MD5": (95, 100),
        "Skipped": (100, 100),
    }
    start, end = stages.get(operation, (pct, pct))
    return round(start + (end - start) * pct / 100)


class ProgressArea(QWidget):
    """Combined progress bar + optional addon table + log pane + cancel button.

    ``show_table=True`` (full GAMMA install) shows a per-addon table.
    ``show_table=False`` (single-file Anomaly download) replaces the table with
    a status label showing the current file, operation and percent. With
    ``show_log=False`` the console pane is omitted entirely and the status label
    sits directly under the bar, so the panel stays compact.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        show_table: bool = True,
        show_log: bool = True,
        stage_progress: bool = False,
        log_max_height: int | None = None,
        auto_expand_log: bool = True,
        bar_follows_log: bool = False,
        toggle_row_extra: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.show_table = show_table
        self.show_log = show_log
        #: An extra widget (e.g. a secondary action button) placed to the
        #: left of "Show Console" on its own row, instead of the page
        #: giving it a separate row of its own - lets a page fold a
        #: secondary action onto the console toggle's row to stay compact.
        self._toggle_row_extra = toggle_row_extra
        #: Whether on_started() auto-expands the collapsed log/table pane
        #: (see _show_log()) - the right default for a page where seeing
        #: live output matters (a full install), not for a page where
        #: the console should stay tucked away unless the user asks.
        self._auto_expand_log = auto_expand_log
        #: Whether the progress bar and idle status line are tied to the
        #: log/table's own collapsed state - both hidden while the
        #: console is collapsed, shown alongside it (see
        #: _toggle_log()/_toggle_table()/_show_log()). False everywhere
        #: else: the bar is normally the one thing worth seeing at a
        #: glance without opening the console at all.
        self._bar_follows_log = bar_follows_log
        self.stage_progress = stage_progress
        self._bar_idle_format = "Idle"
        self._bar_percent_format = "%p%"
        self._status_idle = ""
        self._max_bar_value: int = 0
        self._heavy_progress: dict[str, float] = {}
        self._last_status_update: float = 0.0
        self._runner: CommandRunner | None = None
        self._paused = False
        self.bar = QProgressBar(self)
        self.bar.setTextVisible(True)
        self.bar.setFormat(self._bar_idle_format)

        self.table = ProgressTable(self) if show_table else None

        self.status_label = QLabel(self._status_idle)
        self.status_label.setObjectName("info")

        self.heavy_notice_label: QLabel | None = None
        if show_table:
            self.heavy_notice_label = QLabel()
            self.heavy_notice_label.setObjectName("info")
            self.heavy_notice_label.setStyleSheet(f"color: {WARN.name()};")
            self.heavy_notice_label.setWordWrap(True)
            self.heavy_notice_label.hide()

        self.log = OutputPane(self) if show_log else None
        self.log_toggle: QPushButton | None = None
        if self.log is not None:
            if log_max_height is not None:
                self.log.setMaximumHeight(log_max_height)
            self.log.hide()
            self.log_toggle = QPushButton(tr("Show Console"), self)
            self.log_toggle.setObjectName("consoleToggle")
            self.log_toggle.setFixedSize(110, 26)
            self.log_toggle.clicked.connect(self._toggle_log)
            if self._bar_follows_log:
                self.bar.hide()
                self.status_label.hide()

        # A table with no log (full GAMMA install) has nothing else to
        # collapse it behind - give it its own toggle so it isn't stuck
        # permanently visible like every other progress box's console.
        self.table_toggle: QPushButton | None = None
        if self.table is not None and self.log is None:
            self.table.hide()
            self.table_toggle = QPushButton(tr("Show Console"), self)
            self.table_toggle.setObjectName("consoleToggle")
            self.table_toggle.setFixedSize(110, 26)
            self.table_toggle.clicked.connect(self._toggle_table)
            if self._bar_follows_log:
                self.bar.hide()
                self.status_label.hide()

        self.pause_button = QPushButton(tr("Pause"), self)
        self.pause_button.setObjectName("secondary")
        self.pause_button.setFixedSize(100, 32)
        self.pause_button.setStyleSheet("padding: 0px;")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.pause_button.hide()

        self.cancel_button = QPushButton(tr("Cancel"), self)
        self.cancel_button.setObjectName("danger")
        self.cancel_button.setFixedSize(100, 32)
        self.cancel_button.setStyleSheet("padding: 0px;")
        self.cancel_button.hide()
        self.cancel_button.setText(tr("Cancel"))

        status_row = QHBoxLayout()
        status_row.addWidget(self.bar, 1)
        status_row.addWidget(self.pause_button)
        status_row.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(status_row)
        layout.addSpacing(2)
        layout.addWidget(self.status_label)
        if show_table:
            # Tight, matching the log-toggle gap below - pulls the addon
            # table (Addon/Operation/Percent) up right under the bar and
            # status text instead of floating two full 8px gaps down
            # (heavy_notice_label is empty/hidden most of the time, so
            # those gaps were pure dead space above the table).
            layout.addSpacing(2)
            layout.addWidget(self.heavy_notice_label)
            layout.addSpacing(2)
            layout.addWidget(self.table, 3)
            if self.log is not None:
                # Tighter than the other gaps - pulls the Show Console
                # toggle up right under the table/status text instead of
                # leaving it floating with a full 8px gap, so the console
                # (and the card around it) sits shorter while the log
                # stays collapsed, its default state.
                layout.addSpacing(2)
                layout.addLayout(self._log_toggle_row())
                layout.addSpacing(8)
                layout.addWidget(self.log, 2)
            elif self.table_toggle is not None:
                layout.addSpacing(2)
                layout.addLayout(self._table_toggle_row())
        else:
            if self.log is not None:
                layout.addSpacing(2)
                layout.addLayout(self._log_toggle_row())
                layout.addSpacing(8)
                layout.addWidget(self.log, 2)
            else:
                layout.addStretch(1)
        self.reset()

    def _log_toggle_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        if self._toggle_row_extra is not None:
            row.addWidget(self._toggle_row_extra)
        row.addStretch(1)
        row.addWidget(self.log_toggle)
        return row

    def _toggle_log(self) -> None:
        if self.log is None or self.log_toggle is None:
            return
        if self.log.isVisible():
            self.log.hide()
            self.log_toggle.setText(tr("Show Console"))
        else:
            self.log.show()
            self.log_toggle.setText(tr("Hide Console"))
        if self._bar_follows_log:
            self.bar.setVisible(self.log.isVisible())
            self.status_label.setVisible(self.log.isVisible())

    def _table_toggle_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.table_toggle)
        return row

    def _toggle_table(self) -> None:
        if self.table is None or self.table_toggle is None:
            return
        if self.table.isVisible():
            self.table.hide()
            self.table_toggle.setText(tr("Show Console"))
        else:
            self.table.show()
            self.table_toggle.setText(tr("Hide Console"))
        if self._bar_follows_log:
            self.bar.setVisible(self.table.isVisible())
            self.status_label.setVisible(self.table.isVisible())

    def _show_log(self) -> None:
        """Expand the log/table pane, e.g. when a run starts - collapsed by default."""
        if (
            self.log is not None
            and self.log_toggle is not None
            and not self.log.isVisible()
        ):
            self.log.show()
            self.log_toggle.setText(tr("Hide Console"))
        if (
            self.table is not None
            and self.table_toggle is not None
            and not self.table.isVisible()
        ):
            self.table.show()
            self.table_toggle.setText(tr("Hide Console"))
        if self._bar_follows_log:
            self.bar.show()
            self.status_label.show()

    def reset(self) -> None:
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat(self._bar_idle_format)
        self.bar.setStyleSheet("")
        self._max_bar_value = 0
        self._heavy_progress = {}
        self._last_status_update = 0.0
        self._paused = False
        if self.table is not None:
            self.table.reset()
        if self.heavy_notice_label is not None:
            self.heavy_notice_label.hide()
        self.status_label.setText(self._status_idle)
        if self.log is not None:
            self.log.clear()
        self.pause_button.hide()
        self.pause_button.setText(tr("Pause"))
        self.cancel_button.hide()

    def set_runner(self, runner: CommandRunner | None) -> None:
        """Bind a CommandRunner so the pause button can control it."""
        self._runner = runner
        self._paused = False
        self.pause_button.setText(tr("Pause"))

    def set_concurrency(self, threads: int) -> None:
        """Forward the configured download-thread count to the addon table.

        No-op for the single-file install path (no table).
        """
        if self.table is not None:
            self.table.set_concurrency(threads)

    @property
    def is_paused(self) -> bool:
        return self._paused

    def _toggle_pause(self) -> None:
        if self._runner is None:
            return
        if self._paused:
            self._runner.resume()
            self._paused = False
            self.pause_button.setText(tr("Pause"))
            self.bar.setFormat(self._bar_percent_format)
            self.status_label.setText("")
        else:
            self._runner.pause()
            self._paused = True
            self.pause_button.setText(tr("Resume"))
            self.bar.setFormat("Paused")

    def on_line(self, line: str) -> None:
        clean = strip_ansi(line)
        if self.log is not None:
            self.log.append_line(clean)
        event = parse_progress_line(clean)
        if event is not None:
            self.bar.setStyleSheet("")
            if self.stage_progress:
                if event.operation == "Download":
                    # The bar must mirror the true download percentage; no
                    # duplicate percent text below the bar during download.
                    pct = max(0, min(100, round(event.percent * 100)))
                    self.bar.setRange(0, 100)
                    self.bar.setValue(pct)
                    self.bar.setFormat(f"{pct}%")
                    self.status_label.setText("")
                elif event.operation == "Skipped":
                    self.bar.setRange(0, 1)
                    self.bar.setValue(1)
                    self.bar.setFormat("Skipped")
                    self.status_label.setText(tr("{name} - Skipped", name=event.name))
                else:
                    # Extract/Expand/Check MD5 report a numeric percentage
                    # for single-file installs; map each phase onto its fixed
                    # range so the bar advances rather than looping.
                    pct = single_file_progress(event.operation, event.percent)
                    self.bar.setRange(0, 100)
                    self.bar.setValue(pct)
                    self.bar.setFormat(f"{pct}%")
                    self.status_label.setText(tr("{name} - {operation}...", name=event.name, operation=event.operation))
            else:
                # Dropped, not queued: with several archives downloading at
                # once the raw line rate can flip this label several times a
                # second - showing whichever archive is active at each tick,
                # held for a full second, reads far calmer than chasing
                # every single line.
                now = time.monotonic()
                if now - self._last_status_update >= _STATUS_LABEL_THROTTLE_S:
                    self._last_status_update = now
                    self.status_label.setText(
                        f"{event.name} — {event.operation} — {event.percent:.0%}"
                        f"  ·  {event.complete}/{event.total} done"
                    )
                if self.table is None:
                    self.bar.setRange(0, 100)
                    self.bar.setValue(round(event.percent * 100))
                    self.bar.setFormat(self._bar_percent_format)
            if self.table is not None:
                self.table.upsert(event)
            # The CLI counter is authoritative for overall progress. Include
            # the current archive's fraction so a large download does not look
            # stalled until that archive finishes.
            value = aggregate_progress_value(
                event.complete,
                event.total,
                event.percent,
                name=event.name,
                heavy_progress=self._heavy_progress,
                operation=event.operation,
            )
            self._max_bar_value = max(self._max_bar_value, value)
            if self.table is not None:
                self.bar.setRange(0, 100)
                self.bar.setValue(self._max_bar_value)
                self.bar.setFormat(self._bar_percent_format)
            elif event.operation == "Download":
                self.bar.setRange(0, 100)
                self.bar.setValue(round(event.percent * 100))
                self.bar.setFormat(self._bar_percent_format)
            if self.table is not None:
                heavy_not_done = sum(
                    1
                    for h in _HEAVY_ARCHIVE_NAMES
                    if self._heavy_progress.get(h, 0.0) < 1.0
                )
                # The CLI's own counter can never say fewer items are
                # unfinished than the heavy repos we know are still
                # pending - equality means nothing else is left running,
                # so any other row still lingering as non-terminal is safe
                # to force-finish instead of staying stuck.
                if event.total - event.complete == heavy_not_done:
                    self.table.finish_all_except(_HEAVY_ARCHIVE_NAMES)
                else:
                    # Mid-install case the counter check above can't catch:
                    # a row whose own last line never cleanly reached
                    # 100%/Skipped, long before the run is anywhere near
                    # its end.
                    self.table.finish_stale_by_concurrency(_HEAVY_ARCHIVE_NAMES)
                if self.heavy_notice_label is not None:
                    heavy_active = any(
                        p < 1.0 for p in self._heavy_progress.values()
                    )
                    if heavy_active:
                        self.heavy_notice_label.setText(
                            tr(
                                "A large repository is downloading in the background - other addons may pause until it finishes. This can take a while."
                            )
                        )
                        self.heavy_notice_label.show()
                    else:
                        self.heavy_notice_label.hide()

    def status_message(self, text: str) -> None:
        self.status_label.setText(text)

    def on_started(self) -> None:
        self.cancel_button.show()
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText(tr("Cancel"))
        self.pause_button.show()
        self.pause_button.setEnabled(True)
        self.pause_button.setText(tr("Pause"))
        self._paused = False
        self.bar.setStyleSheet("")
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat("Starting...")
        if self._auto_expand_log:
            self._show_log()

    def on_finished(self, rc: int, output: str) -> None:
        self.cancel_button.hide()
        self.pause_button.hide()
        self._paused = False
        from ..settings import cli_ok

        if cli_ok(rc, output, ""):
            self.bar.setRange(0, 1)
            self.bar.setValue(1)
            self.bar.setFormat("Finished")
            self.status_label.setText(tr("Complete"))
            if self.table is not None:
                self.table.finish_all()
        else:
            self.bar.setFormat("Failed")
            self.bar.setValue(0)
            self.status_label.setText(tr("Failed"))
            if self.table is not None:
                self.table.mark_interrupted()
        # The run has ended (success or failure) - "still downloading in
        # the background" can no longer be true, regardless of whether the
        # last progress line happened to trigger the per-line hide check
        # in on_line() above.
        if self.heavy_notice_label is not None:
            self.heavy_notice_label.hide()

    def on_cancelled(self) -> None:
        """Reset the bar/buttons to an idle Cancelled state (keeps the log)."""
        self.cancel_button.hide()
        self.pause_button.hide()
        self._paused = False
        if self.table is not None:
            self.table.mark_interrupted()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat("Cancelled")
        if self.heavy_notice_label is not None:
            self.heavy_notice_label.hide()

    def set_success_state(self, text: str = "Verified successfully") -> None:
        """Show a successful completed state using the install-bar styling."""
        from ..themes import active_theme_tokens

        self.cancel_button.hide()
        self.pause_button.hide()
        self._paused = False
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.bar.setFormat(text)
        tokens = active_theme_tokens()
        gradient = (
            f"qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 {tokens['hero1']},stop:1 {tokens['accent_strong']})"
        )
        self.bar.setStyleSheet(
            f"QProgressBar::chunk {{ background: {gradient}; border-radius: 4px; }}"
        )
        self.status_label.setText(text)
