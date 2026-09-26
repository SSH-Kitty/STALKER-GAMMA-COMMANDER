"""Optional Discord Rich Presence integration.

Shows "Playing S.T.A.L.K.E.R. GAMMA" on the user's Discord profile while
the game is running. Entirely optional and best-effort:

* off by default (``discord_rpc_enabled`` in gui-settings.json);
* works out of the box with COMMANDER's own Discord application
  (:data:`DEFAULT_CLIENT_ID`). An Application ID is public - it only names
  whose app the activity belongs to - so it is safe to ship baked in. A
  custom ID in Settings overrides it for anyone who wants their own app;
* talks to the local Discord client over its IPC socket with nothing but
  the standard library, so it works the same from source, the AUR package
  and the AppImage - there is no optional dependency to be missing;
* any connection failure (Discord not running, socket gone, a client that
  never answers) is swallowed - Rich Presence is decorative and must never
  affect an actual game launch. Every socket call has a short timeout so
  a wedged Discord can't hang the GUI thread.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import time
import uuid
from pathlib import Path

from . import __version_label__

DEFAULT_CLIENT_ID = "1548449555371130900"
"""COMMANDER's Discord application ("STALKER COMMANDER")."""

DETAILS_TEXT = "STALKER GAMMA"
"""The line under the app name ("Playing STALKER COMMANDER") on the profile."""

PROJECT_URL = "https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER"

LARGE_IMAGE_URL = (
    "https://raw.githubusercontent.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/"
    "main/cli/stalker-gamma.png"
)

_TIMEOUT_SECONDS = 2.0

_OP_HANDSHAKE = 0
_OP_FRAME = 1
_OP_CLOSE = 2

# Where Discord builds put their socket, relative to the runtime dir:
# native/AUR, Flatpak Discord, Snap Discord, Flatpak Vesktop.
_SOCKET_SUBDIRS = (
    "",
    "app/com.discordapp.Discord",
    "snap.discord",
    ".flatpak/dev.vencord.Vesktop/xdg-run",
)


#: Discord's own replies are a few KB; anything near this is not Discord.
_MAX_FRAME_BYTES = 1024 * 1024


def effective_client_id(custom_id: str | None) -> str:
    """The user's own Application ID if they set one, else COMMANDER's."""
    custom = (custom_id or "").strip()
    return custom if custom.isdigit() else DEFAULT_CLIENT_ID


def _candidate_sockets() -> list[Path]:
    roots: list[str] = []
    for var in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        value = os.environ.get(var)
        if value and value not in roots:
            roots.append(value)
    # Discord's own fallback location. Shared between users, which is why
    # _connect() only uses a socket this user owns.
    if "/tmp" not in roots:  # nosec B108
        roots.append("/tmp")  # nosec B108
    return [
        Path(root) / sub / f"discord-ipc-{n}"
        for root in roots
        for sub in _SOCKET_SUBDIRS
        for n in range(10)
    ]


class _IpcClient:
    """One connection to the local Discord client, after a good handshake."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def _send(self, op: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self._sock.sendall(struct.pack("<II", op, len(data)) + data)

    def _recv_exact(self, size: int) -> bytes:
        chunks = b""
        while len(chunks) < size:
            chunk = self._sock.recv(size - len(chunks))
            if not chunk:
                raise ConnectionError("Discord closed the IPC socket")
            chunks += chunk
        return chunks

    def _recv(self) -> tuple[int, dict]:
        op, length = struct.unpack("<II", self._recv_exact(8))
        # The length comes from the other end of the socket: bounded, so a
        # bogus one can't make this allocate gigabytes.
        if length > _MAX_FRAME_BYTES:
            raise ConnectionError("Discord IPC frame too large")
        return op, json.loads(self._recv_exact(length) or b"{}")

    def handshake(self, client_id: str) -> bool:
        self._send(_OP_HANDSHAKE, {"v": 1, "client_id": client_id})
        op, reply = self._recv()
        return op == _OP_FRAME and reply.get("evt") == "READY"

    def set_activity(self, activity: dict | None) -> None:
        self._send(
            _OP_FRAME,
            {
                "cmd": "SET_ACTIVITY",
                "args": {"pid": os.getpid(), "activity": activity},
                "nonce": uuid.uuid4().hex,
            },
        )
        self._recv()  # Discord answers every command; don't let replies pile up

    def close(self) -> None:
        try:
            self._send(_OP_CLOSE, {})
        except OSError:
            pass
        self._sock.close()


def _owned_by_us(path: Path) -> bool:
    """True if ``path`` belongs to this user. /tmp is shared: a socket there
    made by another account would otherwise receive this user's activity."""
    try:
        return path.stat().st_uid == os.getuid()
    except OSError:
        return False


def _connect(client_id: str) -> _IpcClient | None:
    for path in _candidate_sockets():
        if not path.exists() or not _owned_by_us(path):
            continue
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_TIMEOUT_SECONDS)
        client = _IpcClient(sock)
        try:
            sock.connect(str(path))
            if client.handshake(client_id):
                return client
        except (OSError, ValueError, struct.error):
            pass
        sock.close()
    return None


def start_presence(client_id: str):
    """Connect to the local Discord client. Returns None on any failure."""
    if not client_id:
        return None
    try:
        return _connect(client_id)
    except Exception:  # noqa: BLE001 - any IPC/connection failure is fine to ignore
        return None


def update_presence(
    rpc,
    details: str,
    start_ts: float | None = None,
    *,
    state: str | None = None,
) -> None:
    """Set the presence text. No-op if ``rpc`` is None (see start_presence)."""
    if rpc is None:
        return
    activity: dict = {
        "details": details,
        "timestamps": {"start": int(start_ts or time.time())},
        "assets": {
            "large_image": LARGE_IMAGE_URL,
            "large_text": f"COMMANDER {__version_label__}",
        },
        # Only other people see buttons, never the user on their own profile.
        "buttons": [{"label": "Get COMMANDER", "url": PROJECT_URL}],
    }
    if state:
        activity["state"] = state
    try:
        rpc.set_activity(activity)
    except Exception:  # noqa: BLE001, S110
        pass


def stop_presence(rpc) -> None:
    """Clear the activity and disconnect. No-op if ``rpc`` is None."""
    if rpc is None:
        return
    try:
        rpc.set_activity(None)
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        rpc.close()
    except Exception:  # noqa: BLE001, S110
        pass


def probe_discord(client_id: str = DEFAULT_CLIENT_ID) -> bool:
    """True if a Discord client is running and accepts ``client_id``.

    Blocking (up to a couple of seconds per socket) - call it off the GUI
    thread.
    """
    rpc = start_presence(client_id)
    if rpc is None:
        return False
    try:
        rpc.close()
    except Exception:  # noqa: BLE001, S110
        pass
    return True
