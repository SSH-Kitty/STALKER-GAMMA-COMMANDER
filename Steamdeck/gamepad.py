"""Reading the Deck's controls straight from the Linux input layer.

Deck Mode's original input contract was Steam Input's *desktop* layout, where
Steam turns the D-pad into arrow keys and A/B into Enter/Escape. That layout
only applies in Desktop Mode. In Game Mode a non-Steam shortcut gets Steam's
*gamepad* template instead: Steam exposes a virtual Xbox-style controller and
sends no keys at all, so a keyboard-only UI sits there ignoring every press.

This module closes that gap with nothing but the standard library: it opens
the gamepad's ``/dev/input/event*`` node read-only (never grabbed, so Steam
and games keep seeing it too), decodes ``struct input_event`` records and
turns them into a small vocabulary of UI actions. systemd's uaccess rules
already give the seat's user read access to joystick devices, so there is no
permission to request and no package to install. Anything that goes wrong -
no ``/dev/input``, no readable pad, a non-Linux system - leaves the monitor
silently idle, and the keyboard path keeps working exactly as before.

The event decoding (:meth:`GamepadMonitor.feed`) is deliberately separate
from the file handling, so the tests drive it with plain tuples.
"""

from __future__ import annotations

import os
import struct
import sys
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Signal

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

# ----------------------------------------------------------- kernel ABI
#: struct input_event on 64-bit Linux: timeval (two longs), u16 type,
#: u16 code, s32 value.
_EVENT = struct.Struct("llHHi")

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03

BTN_SOUTH = 0x130  # A
BTN_EAST = 0x131  # B
# Steam's virtual pad follows the xpad driver, which reports the Xbox "X"
# (left face button) as BTN_X/BTN_NORTH and "Y" (top) as BTN_Y/BTN_WEST.
BTN_X = 0x133
BTN_Y = 0x134
BTN_TL = 0x136  # L1
BTN_TR = 0x137  # R1
BTN_TL2 = 0x138  # L2, on pads that report triggers as buttons
BTN_TR2 = 0x139  # R2
BTN_SELECT = 0x13A  # View / "..."
BTN_START = 0x13B  # Menu / "≡"
BTN_DPAD_UP = 0x220
BTN_DPAD_DOWN = 0x221
BTN_DPAD_LEFT = 0x222
BTN_DPAD_RIGHT = 0x223

ABS_X = 0x00
ABS_Y = 0x01
ABS_Z = 0x02  # left trigger on Xbox-style pads
ABS_RX = 0x03  # right stick
ABS_RY = 0x04
ABS_RZ = 0x05  # right trigger
ABS_HAT0X = 0x10
ABS_HAT0Y = 0x11

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc_read(nr: int, size: int) -> int:
    return (_IOC_READ << 30) | (size << 16) | (ord("E") << 8) | nr


def _ioc_write(nr: int, size: int) -> int:
    return (_IOC_WRITE << 30) | (size << 16) | (ord("E") << 8) | nr


# ------------------------------------------------------ force feedback
EV_FF = 0x15
FF_RUMBLE = 0x50
#: struct ff_effect on 64-bit Linux, rumble variant: u16 type, s16 id,
#: u16 direction, ff_trigger (u16 button, u16 interval), ff_replay (u16
#: length, u16 delay), 2 bytes of padding, then the 32-byte union whose
#: rumble member is u16 strong_magnitude, u16 weak_magnitude.
_FF_EFFECT = struct.Struct("<HhHHHHHxxHH28x")
_EVIOCSFF = _ioc_write(0x80, _FF_EFFECT.size)


def _eviocgname(length: int) -> int:
    return _ioc_read(0x06, length)


def _eviocgbit(ev: int, length: int) -> int:
    return _ioc_read(0x20 + ev, length)


def _eviocgabs(axis: int) -> int:
    return _ioc_read(0x40 + axis, 24)  # struct input_absinfo: six s32


# ------------------------------------------------------------ vocabulary
UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
ACCEPT = "accept"
#: A held past HOLD_MS - only where GamepadMonitor.hold_gate asked for it.
ACCEPT_HOLD = "accept_hold"
BACK = "back"
CONTEXT = "context"
SEARCH = "search"
TAB_PREV = "tab_prev"
TAB_NEXT = "tab_next"
PAGE_UP = "page_up"
PAGE_DOWN = "page_down"
MENU = "menu"
HELP = "help"
#: One notch of scrolling, for callers without an analog stick. The right
#: stick itself scrolls continuously (GamepadMonitor.scroll_axis).
SCROLL_UP = "scroll_up"
SCROLL_DOWN = "scroll_down"

DIRECTIONS = (UP, DOWN, LEFT, RIGHT)

_BUTTONS = {
    BTN_SOUTH: ACCEPT,
    BTN_EAST: BACK,
    BTN_X: CONTEXT,
    BTN_Y: SEARCH,
    BTN_TL: TAB_PREV,
    BTN_TR: TAB_NEXT,
    # Each side's bumper and trigger do the same thing - previous / next tab
    # - so whichever one a finger rests on switches tabs.
    BTN_TL2: TAB_PREV,
    BTN_TR2: TAB_NEXT,
    BTN_START: MENU,
    BTN_SELECT: HELP,
}
_DPAD_BUTTONS = {
    BTN_DPAD_UP: UP,
    BTN_DPAD_DOWN: DOWN,
    BTN_DPAD_LEFT: LEFT,
    BTN_DPAD_RIGHT: RIGHT,
}

#: Hold-to-repeat timing for a held direction, in milliseconds. The delay is
#: long enough that a normal tap never repeats; the rate walks a 900-row mod
#: list in a few seconds without overshooting a short screen.
REPEAT_DELAY_MS = 400
REPEAT_RATE_MS = 90
#: After this many repeats a held direction speeds up, so walking a long
#: list does not drag, while a short screen is crossed before it kicks in.
REPEAT_FAST_AFTER = 8
REPEAT_FAST_RATE_MS = 45
#: How long A must stay down to count as a hold rather than a tap. Long
#: enough that a deliberate tap never becomes one, short enough not to feel
#: like waiting.
HOLD_MS = 450

#: A stick counts as pushed past this fraction of its half-range, and as
#: released below the lower one - the gap stops a stick resting near the
#: threshold from chattering.
_STICK_ON = 0.55
_STICK_OFF = 0.35
#: The right stick's rest zone, as a fraction of its half-range. Deck
#: sticks rarely return to exactly centre; below this reads as released.
_SCROLL_DEADZONE = 0.12
#: Smallest change in deflection worth re-emitting, so a stick held still
#: does not flood the event queue with near-identical values.
_SCROLL_STEP = 0.02
_TRIGGER_ON = 0.6
_TRIGGER_OFF = 0.3
#: A trigger reported as a button and as an axis arrives as two events this
#: close together; they are one pull.
_TRIGGER_SAME_PULL_S = 0.15


#: _open()'s answer for a node that exists but can't be read yet.
_RETRY = object()


def _bit_set(bits: bytes, index: int) -> bool:
    byte = index // 8
    return byte < len(bits) and bool(bits[byte] & (1 << (index % 8)))


class _Device:
    """One open event node and what is known about its axes."""

    def __init__(self, path: Path, fd: int, name: str) -> None:
        self.path = path
        self.fd = fd
        self.name = name
        #: axis code -> (minimum, maximum)
        self.ranges: dict[int, tuple[int, int]] = {}
        self.notifier: QSocketNotifier | None = None


class GamepadMonitor(QObject):
    """Turns gamepad events into Deck Mode actions.

    Emits :attr:`action` with one of the module-level action names. Direction
    actions repeat while held; every other button fires once per press.
    """

    action = Signal(str)
    #: The right stick's vertical deflection, -1.0 (up) to 1.0 (down), with
    #: the rest zone removed - 0.0 means released. Analog on purpose: how
    #: far the stick is pushed sets how fast the view scrolls.
    scroll_axis = Signal(float)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        input_dir: Path = Path("/dev/input"),
        rescan_ms: int = 3000,
    ) -> None:
        super().__init__(parent)
        self._input_dir = input_dir
        self._devices: dict[Path, _Device] = {}
        self._rejected: set[Path] = set()
        self._rescan = QTimer(self)
        self._rescan.setInterval(rescan_ms)
        self._rescan.timeout.connect(self.rescan)

        self._repeat = QTimer(self)
        self._repeat.setSingleShot(True)
        self._repeat.timeout.connect(self._on_repeat)
        self._held: str | None = None
        #: Which source currently holds each direction ("hat", "stick",
        #: "dpad"), so releasing one source does not cancel another.
        self._sources: dict[str, set[str]] = {d: set() for d in DIRECTIONS}
        self._scroll = 0.0
        self._repeats = 0
        self._triggers: dict[int, bool] = {ABS_Z: False, ABS_RZ: False}
        self._trigger_at: dict[str, float] = {}
        self.last_action_at = 0.0
        #: Asked as A goes down: True means this press may become a hold,
        #: so "accept" waits for the release (a tap) and "accept_hold" fires
        #: if the button is still down after HOLD_MS. Unset or False keeps A
        #: firing on the press, as everywhere that has no use for a hold.
        self.hold_gate: Callable[[], bool] | None = None
        self._accept_timer = QTimer(self)
        self._accept_timer.setSingleShot(True)
        self._accept_timer.setInterval(HOLD_MS)
        self._accept_timer.timeout.connect(self._on_accept_held)
        #: "pending" while a gated A is down and undecided, "held" once it
        #: has fired as a hold, None otherwise.
        self._accept_state: str | None = None

    # ------------------------------------------------------------ lifecycle
    def available(self) -> bool:
        return sys.platform.startswith("linux") and fcntl is not None

    def start(self) -> None:
        if not self.available():
            return
        self.rescan()
        self._rescan.start()

    def stop(self) -> None:
        self._rescan.stop()
        self._repeat.stop()
        for device in list(self._devices.values()):
            self._close(device)

    def pause(self) -> None:
        """Let go of every controller until :meth:`resume`.

        Called while the game has the screen. Reading a pad this way never
        takes input away from anything else - the nodes are opened
        read-only and never grabbed - but a game session is a long time to
        hold a handle open for nothing, and closing them rules this window
        out entirely when input trouble in the game is being chased.
        """
        self._paused = True
        self.stop()

    def resume(self) -> None:
        if not getattr(self, "_paused", False):
            return
        self._paused = False
        self.start()

    def is_paused(self) -> bool:
        return getattr(self, "_paused", False)

    def device_names(self) -> list[str]:
        return [device.name for device in self._devices.values()]

    # ------------------------------------------------------------- devices
    def rescan(self) -> None:
        """Open any gamepad node not already open; forget vanished ones."""
        try:
            paths = sorted(self._input_dir.glob("event*"))
        except OSError:
            return
        present = set(paths)
        for path in list(self._devices):
            if path not in present:
                self._close(self._devices[path])
        self._rejected &= present
        for path in paths:
            if path in self._devices or path in self._rejected:
                continue
            device = self._open(path)
            if device is _RETRY:
                # Not readable *yet* (udev applies the uaccess ACL a moment
                # after a hot-plugged pad appears): try again next rescan
                # rather than ignoring the controller until it's replugged.
                continue
            if device is None:
                self._rejected.add(path)
                continue
            self._devices[path] = device
            notifier = QSocketNotifier(device.fd, QSocketNotifier.Type.Read, self)
            # activated's argument list differs across PySide6 releases
            # (6.11 sends both the socket and the notifier type), so swallow
            # all of it: a default-bound ``d`` would otherwise be overwritten.
            notifier.activated.connect(self._reader(device))
            device.notifier = notifier

    def _open(self, path: Path):
        """The opened gamepad, None if it isn't one, or _RETRY if unreadable."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except PermissionError:
            return _RETRY
        except OSError:
            return None
        try:
            keys = bytearray(0x300 // 8)
            fcntl.ioctl(fd, _eviocgbit(EV_KEY, len(keys)), keys)
            if not _bit_set(bytes(keys), BTN_SOUTH):
                os.close(fd)
                return None
            name_buf = bytearray(256)
            try:
                fcntl.ioctl(fd, _eviocgname(len(name_buf)), name_buf)
                name = bytes(name_buf).split(b"\x00", 1)[0].decode(
                    "utf-8", errors="replace"
                )
            except OSError:
                name = path.name
            device = _Device(path, fd, name)
            for axis in (ABS_X, ABS_Y, ABS_RX, ABS_RY, ABS_Z, ABS_RZ):
                info = bytearray(24)
                try:
                    fcntl.ioctl(fd, _eviocgabs(axis), info)
                except OSError:
                    continue
                _value, minimum, maximum = struct.unpack("iii", bytes(info[:12]))
                if maximum > minimum:
                    device.ranges[axis] = (minimum, maximum)
            return device
        except OSError:
            os.close(fd)
            return None

    # ------------------------------------------------------------ rumble
    def rumble(self, *, strength: float = 0.6, length_ms: int = 300, count: int = 1) -> int:
        """Buzz every open pad that supports rumble; the number that did.

        Best-effort and silent: a pad without force feedback, a node that
        can't be opened for writing, or a kernel that refuses the effect is
        simply skipped. The read handles stay read-only; a separate
        write handle is opened for the effect and closed once it has played
        (closing it earlier would cut the effect short).
        """
        if not self.available():
            return 0
        magnitude = max(0, min(0xFFFF, int(strength * 0xFFFF)))
        played = 0
        for device in list(self._devices.values()):
            fd = None
            try:
                fd = os.open(device.path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
                bits = bytearray(0x80 // 8 + 1)
                fcntl.ioctl(fd, _eviocgbit(EV_FF, len(bits)), bits)
                if not _bit_set(bytes(bits), FF_RUMBLE):
                    os.close(fd)
                    continue
                effect = bytearray(
                    _FF_EFFECT.pack(FF_RUMBLE, -1, 0, 0, 0, length_ms, 0, magnitude, magnitude)
                )
                fcntl.ioctl(fd, _EVIOCSFF, effect)
                effect_id = _FF_EFFECT.unpack(bytes(effect))[1]
                now = time.time()
                os.write(
                    fd,
                    _EVENT.pack(int(now), int((now % 1) * 1e6), EV_FF, effect_id, max(1, count)),
                )
            except OSError:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                continue
            played += 1
            QTimer.singleShot(
                length_ms * max(1, count) + 300, self, lambda fd=fd: self._close_ff(fd)
            )
        return played

    @staticmethod
    def _close_ff(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def _close(self, device: _Device) -> None:
        if device.notifier is not None:
            device.notifier.setEnabled(False)
            device.notifier.deleteLater()
            device.notifier = None
        try:
            os.close(device.fd)
        except OSError:
            pass
        self._devices.pop(device.path, None)
        self._release_all()

    def _reader(self, device: _Device):
        return lambda *_args: self._read(device)

    def _read(self, device: _Device) -> None:
        try:
            data = os.read(device.fd, _EVENT.size * 64)
        except BlockingIOError:
            return
        except OSError:
            # ENODEV: the pad went away (Steam restarting its virtual pad,
            # a Bluetooth controller powering off). The rescan reopens it.
            self._close(device)
            return
        usable = len(data) - len(data) % _EVENT.size
        for offset in range(0, usable, _EVENT.size):
            _sec, _usec, etype, code, value = _EVENT.unpack_from(data, offset)
            self.feed(etype, code, value, device.ranges)

    # ------------------------------------------------------------ decoding
    def feed(
        self,
        etype: int,
        code: int,
        value: int,
        ranges: dict[int, tuple[int, int]] | None = None,
    ) -> None:
        """Interpret one input event. Public so the tests can drive it."""
        if etype == EV_KEY:
            if code in _DPAD_BUTTONS:
                self._set_direction(_DPAD_BUTTONS[code], "dpad", value != 0)
            elif code == BTN_SOUTH:
                self._accept_button(value)
            elif code in (BTN_TL2, BTN_TR2) and value == 1:
                self._trigger_pull(_BUTTONS[code])
            elif code in _BUTTONS and value == 1:
                # value 2 is the kernel's own autorepeat - buttons other than
                # directions must fire exactly once per press.
                self._emit(_BUTTONS[code])
            return
        if etype != EV_ABS:
            return
        if code == ABS_HAT0X:
            self._set_direction(LEFT, "hat", value < 0)
            self._set_direction(RIGHT, "hat", value > 0)
        elif code == ABS_HAT0Y:
            self._set_direction(UP, "hat", value < 0)
            self._set_direction(DOWN, "hat", value > 0)
        elif code in (ABS_X, ABS_Y):
            self._stick_axis(code, value, (ranges or {}).get(code, (-32768, 32767)))
        elif code == ABS_RY:
            self._scroll_axis(value, (ranges or {}).get(code, (-32768, 32767)))
        elif code in (ABS_Z, ABS_RZ):
            self._trigger(code, value, (ranges or {}).get(code, (0, 255)))

    def _accept_button(self, value: int) -> None:
        """A: fires on the press, unless hold_gate wants tap and hold told apart."""
        if value == 1:
            gate = self.hold_gate
            if gate is not None and gate():
                self._accept_state = "pending"
                self._accept_timer.start()
            else:
                self._accept_state = None
                self._emit(ACCEPT)
        elif value == 0:
            state = self._accept_state
            self._accept_state = None
            self._accept_timer.stop()
            if state == "pending":
                self._emit(ACCEPT)

    def _on_accept_held(self) -> None:
        if self._accept_state == "pending":
            self._accept_state = "held"
            self._emit(ACCEPT_HOLD)

    def accept_down(self) -> bool:
        """True while a gated A press is still down (a tap or a hold)."""
        return self._accept_state is not None

    def _normalise(self, value: int, span: tuple[int, int]) -> float:
        low, high = span
        centre = (low + high) / 2
        half = (high - low) / 2 or 1
        return (value - centre) / half

    def _stick_axis(self, code: int, value: int, span: tuple[int, int]) -> None:
        position = self._normalise(value, span)
        negative, positive = (LEFT, RIGHT) if code == ABS_X else (UP, DOWN)
        for direction, sign in ((negative, -1), (positive, 1)):
            held = "stick" in self._sources[direction]
            magnitude = position * sign
            if not held and magnitude >= _STICK_ON:
                self._set_direction(direction, "stick", True)
            elif held and magnitude < _STICK_OFF:
                self._set_direction(direction, "stick", False)

    def _scroll_axis(self, value: int, span: tuple[int, int]) -> None:
        position = max(-1.0, min(1.0, self._normalise(value, span)))
        size = abs(position)
        if size < _SCROLL_DEADZONE:
            deflection = 0.0
        else:
            # Rescaled so speed starts from zero at the edge of the rest
            # zone rather than jumping in at 12%.
            deflection = (size - _SCROLL_DEADZONE) / (1 - _SCROLL_DEADZONE)
            deflection = deflection if position > 0 else -deflection
        if deflection == self._scroll:
            return
        if deflection != 0.0 and self._scroll != 0.0 and abs(deflection - self._scroll) < _SCROLL_STEP:
            return
        self._scroll = deflection
        self.last_action_at = time.monotonic()
        self.scroll_axis.emit(deflection)

    def _trigger(self, code: int, value: int, span: tuple[int, int]) -> None:
        low, high = span
        fraction = (value - low) / ((high - low) or 1)
        pressed = self._triggers[code]
        if not pressed and fraction >= _TRIGGER_ON:
            self._triggers[code] = True
            self._trigger_pull(TAB_PREV if code == ABS_Z else TAB_NEXT)
        elif pressed and fraction < _TRIGGER_OFF:
            self._triggers[code] = False

    def _trigger_pull(self, action: str) -> None:
        """One trigger pull, however the pad reports it.

        Some pads send a trigger both as a button and as an axis. Counted
        twice, one pull would skip two tabs - so a second report of the same
        side within a moment is the same pull.
        """
        now = time.monotonic()
        if now - self._trigger_at.get(action, 0.0) < _TRIGGER_SAME_PULL_S:
            return
        self._trigger_at[action] = now
        self._emit(action)

    # -------------------------------------------------------------- repeat
    def _set_direction(self, direction: str, source: str, down: bool) -> None:
        sources = self._sources[direction]
        was_held = bool(sources)
        if down:
            sources.add(source)
        else:
            sources.discard(source)
        if down and not was_held:
            self._held = direction
            self._repeats = 0
            self._emit(direction)
            self._repeat.start(REPEAT_DELAY_MS)
        elif not down and was_held and not sources and self._held == direction:
            self._held = None
            self._repeat.stop()

    def _on_repeat(self) -> None:
        if self._held is None or not self._sources[self._held]:
            self._held = None
            return
        self._emit(self._held)
        self._repeats += 1
        self._repeat.start(
            REPEAT_FAST_RATE_MS if self._repeats >= REPEAT_FAST_AFTER else REPEAT_RATE_MS
        )

    def repeat_interval(self) -> int:
        """The delay before the next repeat of a held direction, in ms."""
        return REPEAT_FAST_RATE_MS if self._repeats >= REPEAT_FAST_AFTER else REPEAT_RATE_MS

    def _release_all(self) -> None:
        for sources in self._sources.values():
            sources.clear()
        self._held = None
        self._repeat.stop()
        # A pad gone mid-press has no release coming: the press is dropped.
        self._accept_state = None
        self._accept_timer.stop()
        if self._scroll:
            # A pad vanishing mid-scroll must not leave the view gliding.
            self._scroll = 0.0
            self.scroll_axis.emit(0.0)

    def _emit(self, name: str) -> None:
        self.last_action_at = time.monotonic()
        self.action.emit(name)
