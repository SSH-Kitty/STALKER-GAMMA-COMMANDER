"""Battery status and keeping the Deck awake through long jobs.

A full GAMMA install is a ~150 GB download. Left alone, a handheld on
battery dims, then suspends, part-way through - and a suspended download is
at best a stall, at worst a half-extracted archive. So while an install,
update or move runs, Deck Mode holds a logind ``sleep:idle`` inhibitor, the
same mechanism a video player uses, and says so in the header.

The inhibitor is a ``systemd-inhibit`` child process rather than a D-Bus
call: logind hands the lock out as a file descriptor, which QtDBus can't
reliably keep open from Python, whereas a child process holds it for exactly
as long as it lives and releases it the instant it dies. What it runs is
``tail --pid=<COMMANDER> -f /dev/null``, which exits by itself when
COMMANDER does - including when COMMANDER crashes or is killed and never
gets to call ``release()`` (a plain ``sleep infinity`` held the lock until
reboot in that case). No systemd
(or no ``systemd-inhibit``) simply means no inhibitor; nothing else changes.

Battery readings come straight from ``/sys/class/power_supply``, which
needs no permissions and exists on every Linux handheld.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

_POWER_SUPPLY = Path("/sys/class/power_supply")


@dataclass(frozen=True)
class BatteryState:
    percent: int
    charging: bool
    #: True when the machine is on external power, charging or not.
    on_ac: bool


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return ""


def battery_state(root: Path = _POWER_SUPPLY) -> BatteryState | None:
    """The system battery, or None on a machine without one."""
    try:
        supplies = sorted(root.iterdir())
    except OSError:
        return None
    battery = None
    on_ac = False
    for supply in supplies:
        kind = _read(supply / "type")
        if kind == "Battery" and _read(supply / "scope") != "Device":
            # scope=Device is a peripheral (a mouse, a controller), not the
            # battery that keeps this machine running.
            battery = battery or supply
        elif kind in ("Mains", "USB") and _read(supply / "online") == "1":
            on_ac = True
    if battery is None:
        return None
    try:
        percent = int(_read(battery / "capacity"))
    except ValueError:
        return None
    status = _read(battery / "status")
    charging = status in ("Charging", "Full")
    if status in ("Charging", "Full", "Not charging"):
        on_ac = True
    return BatteryState(max(0, min(100, percent)), charging, on_ac)


def on_battery(root: Path = _POWER_SUPPLY) -> bool:
    """True only when there *is* a battery and nothing is powering it."""
    state = battery_state(root)
    return state is not None and not state.on_ac


class SleepInhibitor:
    """Holds a logind sleep/idle inhibitor while :meth:`hold` is in effect."""

    def __init__(self, *, who: str = "STALKER COMMANDER") -> None:
        self._who = who
        self._process: subprocess.Popen | None = None

    def active(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def hold(self, why: str) -> bool:
        """Start inhibiting. Idempotent; False if it isn't possible here."""
        if self.active():
            return True
        binary = shutil.which("systemd-inhibit")
        tail = shutil.which("tail")
        if not binary or not tail:
            return False
        try:
            self._process = subprocess.Popen(
                [
                    binary,
                    "--what=sleep:idle",
                    f"--who={self._who}",
                    f"--why={why}",
                    "--mode=block",
                    # Lives exactly as long as COMMANDER, crash or not.
                    tail,
                    f"--pid={os.getpid()}",
                    "-f",
                    "/dev/null",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Its own process group, so release() can take down the
                # `tail` child too in one signal.
                start_new_session=True,
            )
        except OSError:
            self._process = None
            return False
        return True

    def release(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
