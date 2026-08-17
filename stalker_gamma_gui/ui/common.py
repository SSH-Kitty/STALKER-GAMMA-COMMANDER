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

ANOMALY_MARKERS = ("AnomalyLauncher.exe", "fsgame.ltx")
GAMMA_MARKERS = ("ModOrganizer.exe", "ModOrganizer.ini")
GAMMA_PROFILE = "G.A.M.M.A"


def mo2_running() -> bool:
    """True when a Mod Organizer process (and so typically the game) is running.

    Mod Organizer stays alive while it runs the game through ``run -e``, so this
    is the reliable proxy for "the Wine prefix is in use".
    """
    exe = shutil.which("pgrep")
    if not exe:
        return False
    try:
        proc = subprocess.run(
            # Match the executable, not the bare word: '-f ModOrganizer'
            # also hits this GUI when its own path contains that string.
            [exe, "-f", r"ModOrganizer\.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


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
        self._detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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

    def set_state(self, ok: bool | None, detail: str = "", pending_text: str | None = None) -> None:
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

    def set_status_tooltip(self, text: str) -> None:
        """Show the same status details when hovering any part of the row."""
        for widget in (self, self._dot, self._status, self._detail):
            widget.setToolTip(text)


_ACTIVE_RUNNERS: set[CommandRunner] = set()


def shutdown_active_runners(timeout_ms: int = 5000) -> None:
    """Cancel and wait for all active command threads (called on app quit)."""
    for runner in list(_ACTIVE_RUNNERS):
        runner.shutdown(timeout_ms=timeout_ms)


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
        worker.line_ready.connect(self.line)
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
            self.cancelled.emit()

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
                thread.wait(3000)
        _ACTIVE_RUNNERS.discard(self)
        self._worker = None
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _on_finished(self, rc: int, output: str) -> None:
        thread = self._thread
        if thread is not None:
            thread.quit()
        self.finished.emit(rc, output)

    def _on_thread_finished(self) -> None:
        # The worker thread has fully stopped; it is now safe to release the
        # worker and thread objects. Dropping the Python references earlier (as
        # the old deleteLater setup did) double-frees the C++ objects, because a
        # queued DeferredDelete in the worker thread still pointed at them.
        # The runner also stays in _ACTIVE_RUNNERS until this point so it is not
        # garbage-collected (destroying its QThread) while the thread still runs.
        _ACTIVE_RUNNERS.discard(self)
        self._worker = None
        self._thread = None


class BackgroundTask(QObject):
    """Run a plain Python callable on a worker thread, emit its result."""

    result = Signal(object)
    error = Signal(str)

    def __init__(self, fn, *args, parent: QObject | None = None, **kwargs) -> None:
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def start(self) -> None:
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        try:
            self.result.emit(self._fn(*self._args, **self._kwargs))
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))


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

    def start(self) -> None:
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        try:
            self.result.emit(self._fn(self.line.emit))
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))


class OutputPane(QFrame):
    """A read-only console-style log view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QPlainTextEdit

        self.edit = QPlainTextEdit(self)
        self.edit.setReadOnly(True)
        self.edit.setMaximumBlockCount(20000)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)

    def append_line(self, text: str) -> None:
        self.edit.appendPlainText(strip_ansi(text))

    def clear(self) -> None:
        self.edit.clear()


def make_card(parent: QWidget | None = None, *, expand: bool = False) -> tuple[QFrame, QVBoxLayout]:
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


#: Re-exported so UI code keeps a single, obvious import site for sizes.
human_size = format_size


def winetricks_tooltip(status: dict[str, bool]) -> str:
    """Rich-text bullet list of per-verb winetricks state."""
    rows = []
    for verb in WINETRICKS_VERBS:
        ok = status.get(verb, False)
        color = "#7dc963" if ok else "#e0554f"
        state = "installed" if ok else "missing"
        rows.append(f"<span style='color:{color}'>&#9679;</span> {verb} - {state}")
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
            command = ["explorer.exe", path.replace("/", "\\")]

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
        self.horizontalHeader().setSectionResizeMode(1, self.horizontalHeader().ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(2, self.horizontalHeader().ResizeMode.ResizeToContents)
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
            self.item(row, 1).setText(event.operation)
            self.item(row, 2).setText(f"{event.percent:.1%}")

        if event.percent >= 1.0:
            self.item(row, 1).setForeground(QColor("#7dc963"))
            self.item(row, 2).setForeground(QColor("#7dc963"))
        elif event.operation == "Check MD5":
            self.item(row, 1).setForeground(QColor("#5db7a8"))
        else:
            self.item(row, 1).setForeground(QColor("#cfd9c6"))
            self.item(row, 2).setForeground(QColor("#cfd9c6"))
        self.scrollToItem(self.item(row, 0))

    def finish_all(self) -> None:
        """Mark every remaining row as 100% complete (used when a run ends)."""
        for row in set(self._rows.values()):
            op = self.item(row, 1)
            pct = self.item(row, 2)
            if op is None or pct is None:
                continue
            op.setText("Complete")
            pct.setText("100.0%")
            op.setForeground(QColor("#7dc963"))
            pct.setForeground(QColor("#7dc963"))


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
    ) -> None:
        super().__init__(parent)
        self.show_table = show_table
        self.show_log = show_log
        self._bar_idle_format = "Idle"
        self._bar_percent_format = "%p%"
        self._status_idle = "" if show_table else "Idle"
        self.bar = QProgressBar(self)
        self.bar.setTextVisible(True)
        self.bar.setFormat(self._bar_idle_format)

        self.table = ProgressTable(self) if show_table else None

        self.status_label = QLabel(self._status_idle)
        self.status_label.setObjectName("info")

        self.log = OutputPane(self) if show_log else None

        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setObjectName("danger")
        self.cancel_button.hide()

        status_row = QHBoxLayout()
        status_row.addWidget(self.bar, 1)
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
        if self.table is not None:
            self.table.reset()
        self.status_label.setText(self._status_idle)
        if self.log is not None:
            self.log.clear()
        self.cancel_button.hide()

    def on_line(self, line: str) -> None:
        clean = strip_ansi(line)
        if self.log is not None:
            self.log.append_line(clean)
        event = parse_progress_line(clean)
        if event is not None:
            self.status_label.setText(
                f"{event.name} - {event.operation} - {event.percent:.1%}"
            )
            if self.table is not None:
                self.table.upsert(event)
            if self.table is not None and event.total > 1:
                self.bar.setRange(0, event.total)
                self.bar.setValue(event.complete)
                name = event.name.replace("%", "%%")
                self.bar.setFormat(f"%p%  {event.operation}: {name}")
            else:
                self.bar.setRange(0, 100)
                self.bar.setValue(round(event.percent * 100))
                self.bar.setFormat(self._bar_percent_format)

    def status_message(self, text: str) -> None:
        self.status_label.setText(text)

    def on_started(self) -> None:
        self.cancel_button.show()
        self.cancel_button.setEnabled(True)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFormat("Starting...")

    def on_finished(self, rc: int, _output: str) -> None:
        self.cancel_button.hide()
        if rc == 0:
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
