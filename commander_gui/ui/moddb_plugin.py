"""Runs the ModDB browser plugin and installs what it downloads.

One for the app (``controller()``): Settings > Plugins and the Mod
manager's ModDB button share it, and with it the running browser.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from PySide6.QtWidgets import QApplication

from .. import gui_settings, plugins
from ..config import child_environment
from ..i18n import active_language
from ..themes import active_theme_tokens
from .common import StreamTask, tr
from .dialogs import MessageBox as QMessageBox


class ModDBPlugin(QObject):
    #: Installed, running or busy changed - pages showing it refresh.
    changed = Signal()
    #: Progress text while installing.
    line = Signal(str)

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._process: QProcess | None = None
        self._task: StreamTask | None = None
        self._cancellable = False
        self._buffer = b""
        #: The end of the browser's stderr, for the "closed unexpectedly" box.
        #: Only the tail is kept: Chromium logs a lot over a long session.
        self._stderr = b""

    @property
    def running(self) -> bool:
        return self._process is not None

    @property
    def busy(self) -> bool:
        return self._task is not None

    @property
    def cancellable(self) -> bool:
        """Only the download stops on Cancel; unpacking a file runs to the end."""
        return self.busy and self._cancellable

    # ------------------------------------------------------------ install
    def install(self) -> None:
        if self.busy or self.running:
            return

        def worker(report):
            report(tr("Looking for the plugin"))
            plugins.download_and_install(
                self._task.cancel_event,
                lambda done, total: report(
                    tr("Downloading the ModDB browser {percent}%", percent=done * 100 // total)
                    if total else tr("Downloading the ModDB browser")
                ),
            )

        self._run(worker, cancellable=True)

    def install_file(self, archive) -> None:
        """Install from a plugin archive the player already has."""
        if self.busy or self.running:
            return
        digest = plugins.local_checksum(archive)
        if digest is None:
            answer = QMessageBox.question(
                self.window,
                tr("Install the plugin from this file?"),
                tr("COMMANDER can't check this file, because there's no .sha512sum file next to it."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                note=tr("Only install a plugin file from COMMANDER's GitHub releases."),
                labels={QMessageBox.StandardButton.Yes: tr("Install"), QMessageBox.StandardButton.No: tr("Cancel")},
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        def worker(report):
            report(tr("Installing the ModDB browser"))
            plugins.install_archive(archive, digest)

        self._run(worker, cancellable=False)

    def _run(self, worker, cancellable: bool) -> None:
        self._cancellable = cancellable
        self._task = StreamTask(worker, self)
        self._task.line.connect(self.line)
        self._task.result.connect(lambda _r: self._install_done(None))
        self._task.error.connect(self._install_done)
        self._task.start()
        self.changed.emit()

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _install_done(self, error: str | None) -> None:
        cancelled = self._task is not None and self._task.cancel_event.is_set()
        self._task = None
        self.changed.emit()
        if error and not cancelled:  # Cancel ends the download with an error; the player asked for it
            QMessageBox.warning(self.window, tr("The ModDB browser didn't install"), error)

    def remove(self) -> None:
        if self.busy or self.running:
            return
        answer = QMessageBox.question(
            self.window,
            tr("Remove the ModDB browser?"),
            tr("COMMANDER deletes the plugin. Mods you installed with it stay installed."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            labels={QMessageBox.StandardButton.Yes: tr("Remove"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        if answer == QMessageBox.StandardButton.Yes:
            plugins.remove()
            self.changed.emit()

    # --------------------------------------------------------------- open
    def open(self) -> None:
        """Start the browser, or send the player to Settings > Plugins first."""
        if self.running:
            return
        info = plugins.installed()
        if info is None or plugins.needs_update(info):
            self.window.open_settings()
            self.window._ensure_page("settings").show_plugins()
            return
        try:
            program = plugins.launcher(info)
        except plugins.PluginError as exc:
            QMessageBox.warning(self.window, tr("The ModDB browser can't start"), str(exc))
            return
        process = QProcess(self)
        env = QProcessEnvironment()
        for key, value in child_environment().items():
            env.insert(key, value)
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(self._read)
        process.readyReadStandardError.connect(self._read_stderr)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._failed)
        # Before start(): a program that can't run fails inside it, and
        # _failed() must find it to clear it, or it stays "running" for good.
        self._process = process
        self._buffer = b""
        self._stderr = b""
        process.start(str(program), [])
        if self._process is None:
            return
        config = {
            "protocol": plugins.PROTOCOL,
            "language": active_language(),
            "tokens": active_theme_tokens(),
            "stylesheet": QApplication.instance().styleSheet(),
            "downloads": str(plugins.downloads_dir()),
            "data": str(plugins.browser_data_dir()),
            "custom_title_bar": bool(gui_settings.load_gui_settings().get("custom_title_bar", True)),
        }
        process.write(json.dumps(config).encode() + b"\n")
        process.closeWriteChannel()
        self.changed.emit()

    def _read(self) -> None:
        self._buffer += bytes(self._process.readAllStandardOutput())
        *lines, self._buffer = self._buffer.split(b"\n")
        for raw in lines:
            path = plugins.parse_event(raw.decode("utf-8", "replace"))
            if path is not None:
                plugins.remember_download(path)
                self.changed.emit()
                self._hand_to_mod_manager(path)

    def _read_stderr(self) -> None:
        self._stderr = (self._stderr + bytes(self._process.readAllStandardError()))[-8192:]

    def _hand_to_mod_manager(self, path) -> None:
        self.window.close_settings()  # opened from Settings > Plugins
        self.window.set_page("modmanager")
        self.window.raise_()
        self.window.activateWindow()
        page = self.window._ensure_page("modmanager")
        page._drop_queue = [*page._drop_queue, path]
        self._install_queued()

    def _install_queued(self) -> None:
        """Install what the browser downloaded once nothing is in the way:
        a dialog, an install or update, or Mod Organizer running."""
        page = self.window._pages.get("modmanager")
        if page is None or not page._drop_queue or page._install_active:
            # Gone with a language switch, done, or an install is running,
            # whose end starts the next one itself.
            return
        if QApplication.activeModalWidget() or self.window.install_busy or page._mo2_running():
            # ponytail: polls every 2 s; a signal from each blocker if this ever matters
            QTimer.singleShot(2000, self, self._install_queued)
            return
        page._install_next_dropped()

    def _failed(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._process = None
            self.changed.emit()
            QMessageBox.warning(
                self.window,
                tr("The ModDB browser can't start"),
                tr("Remove the plugin in Settings > Plugins and install it again."),
            )

    def _finished(self, code: int, status) -> None:
        self._read_stderr()
        stderr = self._stderr.decode("utf-8", "replace").strip()
        self._process = None
        self.changed.emit()
        if status == QProcess.ExitStatus.CrashExit or code not in (0, 2):
            QMessageBox.warning(
                self.window,
                tr("The ModDB browser closed unexpectedly"),
                tr("Open it again. If it keeps closing, remove the plugin and install it again."),
                info="\n".join(stderr.splitlines()[-8:]),
            )


_CONTROLLER: ModDBPlugin | None = None


def controller(window) -> ModDBPlugin:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = ModDBPlugin(window)
    return _CONTROLLER
