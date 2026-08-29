"""Shared widgets and helpers for the GUI."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..integrity import format_size
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
GAMMA_PROFILE = "G.A.M.M.A"

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
        label.setText("No archives cached")
        label.setStyleSheet(f"color: {STATUS_RED.name()};")
    else:
        label.setText(
            f"{count} archive{'s' if count != 1 else ''} cached"
        )
        label.setStyleSheet(f"color: {OK_GREEN.name()};")


_MO2_RUNNING_CACHE: float = 0.0
_MO2_RUNNING_RESULT: bool = False
_MO2_CACHE_TTL: float = 3.0


def mo2_running() -> bool:
    """True when a Mod Organizer process (and so typically the game) is running.

    Mod Organizer stays alive while it runs the game through ``run -e``, so this
    is the reliable proxy for "the Wine prefix is in use".  Results are cached
    for a few seconds to avoid blocking the GUI thread repeatedly.
    """
    global _MO2_RUNNING_CACHE, _MO2_RUNNING_RESULT

    import time

    now = time.monotonic()
    if now - _MO2_RUNNING_CACHE < _MO2_CACHE_TTL:
        return _MO2_RUNNING_RESULT
    exe = shutil.which("pgrep")
    if not exe:
        return False
    try:
        proc = subprocess.run(
            # The bracket expression matches MO2 but not this pgrep command's
            # own arguments, avoiding a false positive when MO2 is closed.
            [exe, "-f", r"[Mm]odOrganizer\.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    _MO2_RUNNING_CACHE = now
    _MO2_RUNNING_RESULT = proc.returncode == 0
    return _MO2_RUNNING_RESULT


def anomaly_installed(path: str) -> bool:
    """True when the Anomaly game engine appears to be installed at ``path``."""
    base = Path(path)
    return base.is_dir() and any((base / m).is_file() for m in ANOMALY_MARKERS)


def gamma_installed(path: str) -> bool:
    """True when a GAMMA Mod Organizer instance (G.A.M.M.A profile) exists."""
    base = Path(path)
    if not base.is_dir() or not all((base / m).is_file() for m in GAMMA_MARKERS):
        return False
    profiles = base / "profiles"
    return profiles.is_dir() and any(
        p.is_dir() and p.name.upper() == GAMMA_PROFILE for p in profiles.iterdir()
    )


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
        self._dot = QLabel("\u25cf")
        self._dot.setFixedWidth(24)
        self._status = QLabel("Unknown")
        self._detail = QLabel(detail)
        self._detail.setObjectName("info")
        self._detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        row.addWidget(self._dot)
        row.addWidget(self._status)
        if name:
            name_lbl = QLabel(name)
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
            text = "Installed"
        elif ok is False:
            color = STATUS_RED.name()
            text = "Not installed"
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
        self._status.setText("Installing")
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


def shutdown_active_runners(timeout_ms: int = 5000) -> None:
    """Cancel and wait for all active command threads (called on app quit)."""
    begin_shutdown()
    for runner in list(_ACTIVE_RUNNERS):
        try:
            runner.shutdown(timeout_ms=timeout_ms)
        except Exception:  # noqa: BLE001, S110
            pass
    for task in list(_ACTIVE_TASKS):
        shutdown = getattr(task, "shutdown", None)
        if shutdown is not None:
            try:
                shutdown(timeout_ms=timeout_ms)
            except Exception:  # noqa: BLE001, S110
                pass


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
            if not _SHUTTING_DOWN:
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

    def start(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
            self._worker = None
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
        if not _SHUTTING_DOWN:
            self.result.emit(result)

    def _on_error(self, message: str) -> None:
        if not _SHUTTING_DOWN:
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

    def start(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
            self._worker = None
        self._cancel_event.clear()
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
        if not _SHUTTING_DOWN:
            self.line.emit(line)

    def _on_result(self, result) -> None:
        if not _SHUTTING_DOWN:
            self.result.emit(result)

    def _on_error(self, message: str) -> None:
        if not _SHUTTING_DOWN:
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
        rows.append("<b>Tools</b>")
        for label, ok in tool_items:
            color = OK_GREEN.name() if ok else STATUS_RED.name()
            state = "installed" if ok else "missing"
            rows.append(
                f"<span style='color:{color}'>&#9679;</span> "
                f"<span style='color:{color}'>{label} - {state}</span>"
            )
    # Runtimes section (winetricks verbs).
    rows.append("<b>Runtimes</b>")
    for verb in WINETRICKS_VERBS:
        ok = status.get(verb, False)
        color = OK_GREEN.name() if ok else STATUS_RED.name()
        state = "installed" if ok else "missing"
        rows.append(
            f"<span style='color:{color}'>&#9679;</span> "
            f"<span style='color:{color}'>{verb} - {state}</span>"
        )
    return "<br>".join(rows)


def dir_size(path: str | Path) -> int:
    """Best-effort total size of a directory tree."""
    total = 0
    try:
        for entry in Path(path).rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        pass
    return total


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
        self._rows: dict[str, int] = {}

    def reset(self) -> None:
        self.setRowCount(0)
        self._rows.clear()

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

        if event.percent >= 1.0:
            self.item(row, 1).setForeground(QColor(OK_GREEN.name()))
            self.item(row, 2).setForeground(QColor(OK_GREEN.name()))
        elif event.operation == "Check MD5":
            self.item(row, 1).setForeground(QColor(TEAL.name()))
        else:
            self.item(row, 1).setForeground(QColor(LIGHT_GREY.name()))
            self.item(row, 2).setForeground(QColor(LIGHT_GREY.name()))

    def finish_all(self) -> None:
        """Mark every remaining row as 100% complete (used when a run ends)."""
        for row in set(self._rows.values()):
            op = self.item(row, 1)
            pct = self.item(row, 2)
            if op is None or pct is None:
                continue
            op.setText("Complete")
            pct.setText("100.0%")
            op.setForeground(QColor(OK_GREEN.name()))
            pct.setForeground(QColor(OK_GREEN.name()))


def progress_value(complete: int, total: int) -> int:
    """Convert aggregate completed/total mod counts to a bounded percentage."""
    if total <= 0:
        return 0
    return max(0, min(100, round(complete / total * 100)))


def aggregate_progress_value(complete: int, total: int, percent: float) -> int:
    """Return overall progress using the CLI counter and current item fraction."""
    if total <= 0:
        return 0
    current = max(0.0, min(1.0, percent))
    completed_before_current = max(0, complete - 1)
    fraction = (completed_before_current + current) / total
    return max(0, min(100, round(fraction * 100)))


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
    ) -> None:
        super().__init__(parent)
        self.show_table = show_table
        self.show_log = show_log
        self.stage_progress = stage_progress
        self._bar_idle_format = "Idle"
        self._bar_percent_format = "%p%"
        self._status_idle = ""
        self._seen: set[str] = set()
        self._completed: set[str] = set()
        self._per_archive: dict[str, float] = {}
        self._max_bar_value: int = 0
        self._runner: CommandRunner | None = None
        self._paused = False
        self.bar = QProgressBar(self)
        self.bar.setTextVisible(True)
        self.bar.setFormat(self._bar_idle_format)

        self.table = ProgressTable(self) if show_table else None

        self.status_label = QLabel(self._status_idle)
        self.status_label.setObjectName("info")

        self.log = OutputPane(self) if show_log else None
        if self.log is not None and log_max_height is not None:
            self.log.setMaximumHeight(log_max_height)

        self.pause_button = QPushButton("Pause", self)
        self.pause_button.setObjectName("secondary")
        self.pause_button.setFixedSize(100, 32)
        self.pause_button.setStyleSheet("padding: 0px;")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.pause_button.hide()

        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setObjectName("danger")
        self.cancel_button.setFixedSize(100, 32)
        self.cancel_button.setStyleSheet("padding: 0px;")
        self.cancel_button.hide()
        self.cancel_button.setText("Cancel")

        status_row = QHBoxLayout()
        status_row.addWidget(self.bar, 1)
        status_row.addWidget(self.pause_button)
        status_row.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(status_row)
        layout.addWidget(self.status_label)
        if show_table:
            layout.addWidget(self.table, 3)
            if self.log is not None:
                layout.addWidget(self.log, 2)
        else:
            if self.log is not None:
                layout.addWidget(self.log, 2)
            else:
                layout.addStretch(1)
        self.reset()

    def reset(self) -> None:
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat(self._bar_idle_format)
        self.bar.setStyleSheet("")
        self._seen.clear()
        self._completed.clear()
        self._per_archive.clear()
        self._max_bar_value = 0
        self._paused = False
        if self.table is not None:
            self.table.reset()
        self.status_label.setText(self._status_idle)
        if self.log is not None:
            self.log.clear()
        self.pause_button.hide()
        self.pause_button.setText("Pause")
        self.cancel_button.hide()

    def set_runner(self, runner: CommandRunner | None) -> None:
        """Bind a CommandRunner so the pause button can control it."""
        self._runner = runner
        self._paused = False
        self.pause_button.setText("Pause")

    @property
    def is_paused(self) -> bool:
        return self._paused

    def _toggle_pause(self) -> None:
        if self._runner is None:
            return
        if self._paused:
            self._runner.resume()
            self._paused = False
            self.pause_button.setText("Pause")
            self.bar.setFormat(self._bar_percent_format)
            self.status_label.setText("")
        else:
            self._runner.pause()
            self._paused = True
            self.pause_button.setText("Resume")
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
                    self.status_label.setText(f"{event.name} - Skipped")
                else:
                    # Extract/Expand/Check MD5 report a numeric percentage
                    # for single-file installs; map each phase onto its fixed
                    # range so the bar advances rather than looping.
                    pct = single_file_progress(event.operation, event.percent)
                    self.bar.setRange(0, 100)
                    self.bar.setValue(pct)
                    self.bar.setFormat(f"{pct}%")
                    self.status_label.setText(f"{event.name} - {event.operation}...")
            else:
                self.status_label.setText(
                    f"{event.name} - {event.operation} - {event.percent:.1%}"
                )
                if self.table is None:
                    self.bar.setRange(0, 100)
                    self.bar.setValue(round(event.percent * 100))
                    self.bar.setFormat(self._bar_percent_format)
            if self.table is not None:
                self.table.upsert(event)
            self._seen.add(event.name)
            # Track per-archive percent for average-based progress.
            # Terminal states (percent >= 1.0, Skipped) pin at 1.0.
            # Extract/Expand/Check MD5 keep prior value (no regression).
            if event.percent >= 1.0 or event.operation == "Skipped":
                self._per_archive[event.name] = 1.0
                self._completed.add(event.name)
            elif event.operation in ("Extract", "Expand", "Check MD5"):
                # Keep prior value; don't regress to 0
                pass
            else:
                # Download or other: update current percent
                self._per_archive[event.name] = event.percent
            # The CLI counter is authoritative for overall progress. Include
            # the current archive's fraction so a large download does not look
            # stalled until that archive finishes.
            value = aggregate_progress_value(
                event.complete, event.total, event.percent
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

    def status_message(self, text: str) -> None:
        self.status_label.setText(text)

    def on_started(self) -> None:
        self.cancel_button.show()
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel")
        self.pause_button.show()
        self.pause_button.setEnabled(True)
        self.pause_button.setText("Pause")
        self._paused = False
        self.bar.setStyleSheet("")
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat("Starting...")

    def on_finished(self, rc: int, output: str) -> None:
        self.cancel_button.hide()
        self.pause_button.hide()
        self._paused = False
        from ..settings import cli_ok

        if cli_ok(rc, output, ""):
            self.bar.setRange(0, 1)
            self.bar.setValue(1)
            self.bar.setFormat("Finished")
            self.status_label.setText("Complete")
            if self.table is not None:
                self.table.finish_all()
        else:
            self.bar.setFormat("Failed")
            self.bar.setValue(0)
            self.status_label.setText("Failed")
        self._seen.clear()
        self._completed.clear()
        self._per_archive.clear()

    def on_cancelled(self) -> None:
        """Reset the bar/buttons to an idle Cancelled state (keeps the log)."""
        self.cancel_button.hide()
        self.pause_button.hide()
        self._paused = False
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat("Cancelled")
        self._seen.clear()
        self._completed.clear()
        self._per_archive.clear()

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
        self._seen.clear()
        self._completed.clear()
        self._per_archive.clear()

    def set_installed_state(self, installed: bool | None, detail: str = "") -> None:
        """Set the bar to a persistent Installed / Not installed / Unknown state."""
        from ..themes import active_theme_tokens

        self.cancel_button.hide()
        self.pause_button.hide()
        self.bar.setRange(0, 1)
        tokens = active_theme_tokens()
        if installed is True:
            self.bar.setValue(1)
            self.bar.setFormat("Installed")
            gradient = (
                f"qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                f"stop:0 {tokens['hero1']},stop:1 {tokens['accent_strong']})"
            )
        elif installed is False:
            self.bar.setValue(0)
            self.bar.setFormat("Not installed")
            gradient = STATUS_RED.name()
        else:
            self.bar.setValue(0)
            self.bar.setFormat("Unknown")
            gradient = STATUS_GREY.name()
        self.bar.setStyleSheet(
            f"QProgressBar::chunk {{ background: {gradient}; border-radius: 4px; }}"
        )
        self.status_label.setText(detail)
        self._seen.clear()
        self._completed.clear()
        self._per_archive.clear()
