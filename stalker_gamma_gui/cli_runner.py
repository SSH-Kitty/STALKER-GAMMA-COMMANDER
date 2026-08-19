"""Run the stalker-gamma CLI as a subprocess.

Long-running commands (full-install, update apply, anomaly install/check) run in
a QThread so the UI stays responsive and output lines can be streamed into the
GUI. Quick commands use a simple synchronous helper.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from .config import cli_binary_path

if os.name == "nt":
    _CANCEL_SIGNAL = signal.CTRL_BREAK_EVENT
else:
    _CANCEL_SIGNAL = signal.SIGINT

#: Exit code reported when the CLI process could not be started or died
#: unexpectedly (mirrors the shell's "command not executable" convention).
SPAWN_FAILED_RC = 126
#: Exit code reported when a synchronous command exceeded its timeout.
TIMEOUT_RC = 124


class CliWorker(QObject):
    """Runs one CLI invocation, streaming output lines.

    Usage: move to a QThread, call run(command) via invokeMethod/queued signal,
    cancel() sends the cancellation signal to the child process.
    """

    line_ready = Signal(str)
    finished = Signal(int, str)

    def __init__(self) -> None:
        super().__init__()
        self._process: subprocess.Popen[str] | None = None
        self._command: list[str] = []
        self._cwd = ""
        self._env: dict[str, str] | None = None
        self._cancel_pending = False

    def setup(self, command: list[str], cwd: str = "", env: dict[str, str] | None = None) -> None:
        self._command = command
        self._cwd = cwd
        self._env = env

    @Slot()
    def run(self) -> None:
        """Run the command, streaming stdout.

        Every failure path must still emit ``finished``: callers gate UI state
        (busy flags, disabled buttons) on that signal, so letting an exception
        escape this slot would leave the GUI permanently locked.
        """
        command = self._command
        cwd = self._cwd
        env = None
        if self._env:
            env = dict(os.environ)
            env.update(self._env)
        collected: list[str] = []
        try:
            self._process = subprocess.Popen(
                command,
                cwd=cwd or None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self._process = None
            message = f"Failed to start {command[0]!r}: {exc}"
            self.line_ready.emit(message)
            self.finished.emit(SPAWN_FAILED_RC, message)
            return
        if self._cancel_pending:
            # Cancel arrived before the process spawned; apply it now.
            self.cancel()
        try:
            if self._process.stdout is None:
                raise RuntimeError(f"No stdout pipe for {command[0]!r}")
            for raw in self._process.stdout:
                line = raw.rstrip("\r\n")
                collected.append(line)
                self.line_ready.emit(line)
            self._process.wait()
            rc = self._process.returncode
        except Exception as exc:  # noqa: BLE001 - must not escape the slot
            message = f"Error while running {command[0]!r}: {exc}"
            collected.append(message)
            self.line_ready.emit(message)
            proc, self._process = self._process, None
            if proc is not None and proc.poll() is None:
                proc.kill()
            self.finished.emit(SPAWN_FAILED_RC, "\n".join(collected))
            return
        self._process = None
        self.finished.emit(rc, "\n".join(collected))

    @Slot()
    def cancel(self) -> None:
        proc = self._process
        if proc is None or proc.poll() is not None:
            # The process has not been spawned yet (or already exited); run()
            # checks this flag right after Popen and cancels immediately.
            self._cancel_pending = True
            return
        try:
            if os.name == "nt":
                proc.send_signal(_CANCEL_SIGNAL)
            else:
                os.kill(proc.pid, _CANCEL_SIGNAL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass

    def pause(self) -> None:
        """SIGSTOP the child process to freeze it in place."""
        proc = self._process
        if proc is None or proc.poll() is not None or os.name == "nt":
            return
        try:
            os.kill(proc.pid, signal.SIGSTOP)
        except OSError:
            pass

    def resume(self) -> None:
        """SIGCONT the child process to resume from where it was stopped."""
        proc = self._process
        if proc is None or proc.poll() is not None or os.name == "nt":
            return
        try:
            os.kill(proc.pid, signal.SIGCONT)
        except OSError:
            pass

    @Slot()
    def kill(self) -> None:
        proc = self._process
        if proc is not None and proc.poll() is None:
            proc.kill()


def _as_text(value: str | bytes | None) -> str:
    """Normalise captured output to text.

    ``TimeoutExpired`` carries the raw *bytes* read so far even when the
    process was started in text mode, so this cannot assume ``str``.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_sync(
    args: list[str],
    cwd: str | Path | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    """Run a quick CLI command synchronously, returning (exit code, combined output)."""
    cmd = [str(cli_binary_path()), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = _as_text(exc.stdout) + _as_text(exc.stderr)
        return TIMEOUT_RC, f"{output}\n[timed out after {timeout}s]"
    except OSError as exc:
        return SPAWN_FAILED_RC, f"Failed to start {cmd[0]!r}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def cli_command(args: list[str], *, progress_interval_ms: int | None = None) -> list[str]:
    """Build the full command line for a CLI invocation."""
    cmd = [str(cli_binary_path()), *args]
    if progress_interval_ms is not None:
        cmd += ["--progress-update-interval-ms", str(progress_interval_ms)]
    return cmd
