"""Optional Discord Rich Presence integration.

Shows "Playing S.T.A.L.K.E.R. GAMMA" on the user's Discord profile while
the game is running. Entirely optional and best-effort:

* off by default (``discord_rpc_enabled`` in gui-settings.json);
* a no-op if the ``pypresence`` package isn't installed - it is NOT a
  hard dependency of this app, so every function here degrades silently
  rather than raising ImportError;
* requires a Discord "Application Client ID", which only the user can
  obtain (free, a few clicks) by creating an application at
  https://discord.com/developers/applications and copying its ID into
  Settings - there is no generic ID that works for every user/build, so
  this cannot be pre-configured or shipped baked in;
* any connection failure (Discord not running, IPC unavailable) is
  swallowed - Rich Presence is decorative and must never affect an
  actual game launch.
"""

from __future__ import annotations

import time


def start_presence(client_id: str):
    """Connect to the local Discord client. Returns None on any failure."""
    if not client_id:
        return None
    try:
        from pypresence import Presence
    except ImportError:
        return None
    try:
        rpc = Presence(client_id)
        rpc.connect()
    except Exception:  # noqa: BLE001 - any IPC/connection failure is fine to ignore
        return None
    return rpc


def update_presence(rpc, details: str, start_ts: float | None = None) -> None:
    """Set the presence text. No-op if ``rpc`` is None (see start_presence)."""
    if rpc is None:
        return
    try:
        rpc.update(details=details, start=start_ts or time.time())
    except Exception:  # noqa: BLE001, S110
        pass


def stop_presence(rpc) -> None:
    """Disconnect. No-op if ``rpc`` is None."""
    if rpc is None:
        return
    try:
        rpc.close()
    except Exception:  # noqa: BLE001, S110
        pass
