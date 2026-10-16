"""Switching between the desktop UI and Steam Deck Mode.

Switching is a real restart, not a widget swap: the window closes, the
process re-execs itself with (or without) ``--deck``, and the replacement
builds a fresh QApplication, theme and window. That costs a second of
startup and buys a guarantee no in-place mode toggle can give - no stale
pages, no half-applied stylesheet, no background thread from the old UI
still holding a CLI handle.

The Qt half of that lives here; the Qt-free mechanics (argv construction,
execve, the loop guard) live in ``commander_gui.deck_launch``.
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication, QWidget

from ..deck_launch import DeckRelaunchError, relaunch_exec
from ..i18n import tr
from .common import cancel_active_runners, mo2_running, resume_after_shutdown
from .dialogs import MessageBox as QMessageBox


def switch_mode(window, *, deck: bool) -> None:
    """Close ``window`` and restart COMMANDER in the other interface.

    Returns only if the switch was refused or ``execve`` failed; on success
    this process is replaced and never comes back.
    """
    parent = window if isinstance(window, QWidget) else None
    # Deck Mode's window asks its questions as in-window overlays (it has no
    # window manager to place a QMessageBox under gamescope); the desktop
    # window has no confirm()/notify() and gets real message boxes.
    overlays = callable(getattr(window, "confirm", None)) and callable(
        getattr(window, "notify", None)
    )

    # A running install drives a stalker-gamma child process that is writing
    # into the install tree. execve would orphan it mid-write with no UI left
    # to report progress or failure, so this is a refusal, not a confirmation.
    if getattr(window, "install_busy", False):
        message = tr(
            "An install, update or download is still running. "
            "Wait for it to finish, then switch."
        )
        if overlays:
            window.notify(message, 6000)
        else:
            QMessageBox.warning(parent, tr("Please wait"), message)
        return

    # The game itself survives the restart - it was launched detached into its
    # own process group - but the timer that tracks the session dies with this
    # process, so the playtime for it is lost.
    if mo2_running():
        title = tr("Restart COMMANDER now?")
        message = tr(
            "COMMANDER restarts to switch interface. Your game keeps running, "
            "but this session's playtime won't be counted."
        )
        if overlays:
            window.confirm(title, message, lambda: _restart(window, deck=deck))
            return
        answer = QMessageBox.question(
            parent,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            labels={QMessageBox.StandardButton.Yes: tr("Restart"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
    _restart(window, deck=deck)


def _restart(window, *, deck: bool) -> None:
    """The unconditional half of :func:`switch_mode`, after its guards."""
    # close() runs the window's own closeEvent, which may veto (MainWindow
    # refuses while an install is running) and which persists the desktop
    # window geometry.
    window.close()
    if window.isVisible():
        return

    # execve would drop the IPC socket anyway, but clear the activity first
    # so Discord doesn't keep showing it until it notices the dead socket.
    try:
        play = getattr(window, "_pages", {}).get("play")
        if play is not None:
            play._stop_discord_presence()
    except Exception:  # noqa: BLE001, S110 - presence must never block a restart
        pass

    # Cancel, do not wait. Waiting here was the whole cost of switching
    # modes: the Dashboard has a winetricks probe, a size scan and an update
    # check in flight whenever its Deck button is pressed, and none of them
    # can be interrupted, so the switch sat out the full timeout with the
    # window already closed. execve discards those threads wholesale.
    cancel_active_runners()

    from ..main import release_instance_lock

    try:
        relaunch_exec(deck=deck, release_lock=release_instance_lock)
        failure = tr("the interpreter could not be restarted")
    except DeckRelaunchError as exc:
        failure = str(exc)

    # Only reached when execve refused. The window is gone and its pages are
    # partly torn down, so re-showing it would be worse than exiting with an
    # explanation the user can act on.
    resume_after_shutdown()
    flag = "--deck" if deck else ""
    QMessageBox.critical(
        None,
        tr("COMMANDER couldn't restart"),
        tr(
            "Something stopped the restart ({reason}).\n\n"
            "Start COMMANDER again yourself. From a terminal: stalker-gamma-commander {flag}",
            reason=failure,
            flag=flag,
        ).strip(),
    )
    app = QApplication.instance()
    if app is not None:
        app.exit(1)
