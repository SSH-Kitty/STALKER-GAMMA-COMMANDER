"""Locate and launch the optional bundled COMMANDER ASSISTANT."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from shutil import which

from .config import project_root


class AssistantLaunchError(RuntimeError):
    """Raised when ASSISTANT cannot be located or started."""


_assistant_processes: list[subprocess.Popen[bytes]] = []


def reap_assistant_processes() -> list[subprocess.Popen[bytes]]:
    """Reap completed ASSISTANT processes and return those that exited."""
    completed: list[subprocess.Popen[bytes]] = []
    active: list[subprocess.Popen[bytes]] = []
    for process in _assistant_processes:
        if process.poll() is None:
            active.append(process)
        else:
            completed.append(process)
    _assistant_processes[:] = active
    return completed


def assistant_is_running() -> bool:
    """Return whether an ASSISTANT process is still running."""
    return active_assistant_process() is not None


def active_assistant_process() -> subprocess.Popen[bytes] | None:
    """Return the tracked active ASSISTANT process, if any."""
    reap_assistant_processes()
    return _assistant_processes[0] if _assistant_processes else None


def assistant_command(archive: str | Path | None = None) -> tuple[list[str], Path]:
    """Return the command and working directory for ASSISTANT.

    The bundled package uses COMMANDER's already-bundled Python and PySide6,
    while source installations can fall back to the standalone console entry
    point.  Archive paths are passed as arguments rather than through a shell.
    """
    root = project_root()
    bundled = root / "assistant" / "__main__.py"
    args = [] if archive is None else [str(Path(archive).expanduser().resolve())]
    if bundled.is_file():
        return [sys.executable, "-m", "assistant", *args], root

    configured = os.environ.get("COMMANDER_ASSISTANT", "").strip()
    if configured:
        executable_path = Path(configured).expanduser()
        if not executable_path.is_absolute():
            executable_path = (Path.cwd() / executable_path).resolve()
        executable = str(executable_path)
    else:
        executable = which("commander-assistant")
    if executable:
        return [executable, *args], root
    raise AssistantLaunchError(
        "ASSISTANT is not bundled or installed. Install commander-assistant "
        "or rebuild COMMANDER with the ASSISTANT package."
    )


def launch_assistant(archive: str | Path | None = None) -> subprocess.Popen[bytes]:
    """Start ASSISTANT in a detached process and return its process handle."""
    if active_assistant_process() is not None:
        raise AssistantLaunchError("ASSISTANT is already running.")
    command, cwd = assistant_command(archive)
    env = os.environ.copy()
    if command[1:3] == ["-m", "assistant"]:
        payload = str(cwd)
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            payload
            if not current_pythonpath
            else os.pathsep.join((payload, current_pythonpath))
        )
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            start_new_session=(os.name != "nt"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise AssistantLaunchError(f"Could not start ASSISTANT: {exc}") from exc
    _assistant_processes.append(process)
    return process
