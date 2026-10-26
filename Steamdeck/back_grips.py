"""The in-game overlay shortcut: the Deck's back grips, read from hidraw.

While a game runs, its Steam Input layout turns the controls into keys and
mouse moves, so the virtual pad ``gamepad.GamepadMonitor`` reads goes quiet,
and the back grips are not on it at all. The controller's own HID reports
still carry every button, whatever the layout: this reads them from
``/dev/hidraw*`` (Steam's udev rule gives the seat's user read access),
read-only and never exclusive, so Steam and the game are unaffected.

It emits :attr:`shortcut` when the chosen grip pair is held, and - while
:attr:`navigating` is on (the overlay is up) - the D-pad and face buttons as
``gamepad`` actions, so the overlay is driven the same way as Deck Mode.

Report layout (SDL's ``SDL_hidapi_steamdeck.c``): byte 2 is the report type
(9 = controller state), buttons are two little-endian u32 at bytes 8 and 12.
"""

from __future__ import annotations

import os
import struct
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Signal

from . import gamepad as pad

_VALVE, _DECK = 0x28DE, 0x1205
_DECK_STATE = 9
_REPORT = 64

#: Bits of (buttons_low | buttons_high << 32). If a grip ever reads wrong on
#: a new controller revision, this table is the knob.
L5, R5 = 1 << 15, 1 << 16
L4, R4 = 1 << (32 + 9), 1 << (32 + 10)
_ACTIONS = {
    1 << 8: pad.UP, 1 << 11: pad.DOWN, 1 << 10: pad.LEFT, 1 << 9: pad.RIGHT,
    1 << 7: pad.ACCEPT, 1 << 5: pad.BACK, 1 << 6: pad.CONTEXT, 1 << 4: pad.SEARCH,
    1 << 3: pad.TAB_PREV, 1 << 2: pad.TAB_NEXT, 1 << 14: pad.MENU,
}

#: deck_overlay_shortcut values -> the grips held together.
SHORTCUTS = {"l4r4": L4 | R4, "l5r5": L5 | R5, "l4l5": L4 | L5, "r4r5": R4 | R5}
#: How long the pair must be held: a brush of a grip mid-fight never opens it.
HOLD_S = 0.5


def deck_hidraw_nodes(sys_root: Path = Path("/sys/class/hidraw")) -> list[Path]:
    """/dev/hidraw nodes of the Deck's built-in controller."""
    nodes = []
    for entry in sorted(sys_root.glob("hidraw*")):
        try:
            uevent = (entry / "device" / "uevent").read_text()
        except OSError:
            continue
        if f"HID_ID=0003:{_VALVE:08X}:{_DECK:08X}" in uevent:
            nodes.append(Path("/dev") / entry.name)
    return nodes


def buttons(report: bytes) -> int | None:
    """The pressed-button mask of a controller-state report, else None."""
    if len(report) < 16 or report[2] != _DECK_STATE:
        return None
    low, high = struct.unpack_from("<II", report, 8)
    return low | high << 32


class BackGripMonitor(QObject):
    shortcut = Signal()
    action = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.chord = SHORTCUTS["l4r4"]
        self.navigating = False
        self._fds: list[tuple[int, QSocketNotifier]] = []
        self._pressed = 0
        self._chord_since: float | None = None
        self._fired = False
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self._check_hold)

    def start(self) -> None:
        for node in deck_hidraw_nodes():
            try:
                fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue  # no access: the shortcut stays off, nothing breaks
            notifier = QSocketNotifier(fd, QSocketNotifier.Type.Read, self)
            notifier.activated.connect(lambda _s, fd=fd: self._read(fd))
            self._fds.append((fd, notifier))

    def listening(self) -> bool:
        """A Deck controller is open: the shortcut can work."""
        return bool(self._fds)

    def stop(self) -> None:
        for fd, notifier in self._fds:
            notifier.setEnabled(False)
            os.close(fd)
        self._fds.clear()

    def _read(self, fd: int) -> None:
        try:
            while True:
                report = os.read(fd, _REPORT)
                if not report:
                    return
                mask = buttons(report)
                if mask is not None:
                    self.feed(mask)
        except BlockingIOError:
            return
        except OSError:
            self.stop()  # unplugged or gone: give up quietly

    def feed(self, mask: int) -> None:
        """One report's buttons. Separate from the file handling for tests."""
        pressed, self._pressed = mask & ~self._pressed, mask
        held = bool(self.chord) and mask & self.chord == self.chord
        if held and self._chord_since is None:
            self._chord_since = time.monotonic()
            self._hold.start(int(HOLD_S * 1000))
        elif not held:
            self._chord_since = None
            self._fired = False
            self._hold.stop()
        if self.navigating:
            for bit, name in _ACTIONS.items():
                if pressed & bit:
                    self.action.emit(name)

    def _check_hold(self) -> None:
        if self._chord_since is not None and not self._fired:
            self._fired = True
            self.shortcut.emit()
