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

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from ..deck_launch import DeckRelaunchError, relaunch_exec
from ..i18n import tr
from .common import cancel_active_runners, mo2_running, resume_after_shutdown


def switch_mode(window, *, deck: bool) -> None:
    """Close ``window`` and restart COMMANDER in the other interface.

    Returns only if the switch was refused or ``execve`` failed; on success
    this process is replaced and never comes back.
    """
    parent = window if isinstance(window, QWidget) else None

    # A running install drives a stalker-gamma child process that is writing
    # into the install tree. execve would orphan it mid-write with no UI left
    # to report progress or failure, so this is a refusal, not a confirmation.
    if getattr(window, "install_busy", False):
        QMessageBox.warning(
            parent,
            tr("Busy"),
            tr(
                "An install, update or dependency download is still running. "
                "Wait for it to finish before switching interface."
            ),
        )
        return

    # The game itself survives the restart - it was launched detached into its
    # own process group - but the timer that tracks the session dies with this
    # process, so the playtime for it is lost.
    if mo2_running():
        answer = QMessageBox.question(
            parent,
            tr("Mod Organizer is running"),
            tr(
                "COMMANDER will restart. Your game keeps running, but the "
                "current session's playtime will not be recorded.\n\nContinue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

    # close() runs the window's own closeEvent, which may veto (MainWindow
    # refuses while an install is running) and which persists the desktop
    # window geometry.
    window.close()
    if window.isVisible():
        return

    try:
        from ..discord_rpc import stop_presence

        stop_presence()
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
        tr("Restart Failed"),
        tr(
            "COMMANDER could not restart ({reason}).\n\n"
            "Start it again manually:  stalker-gamma-commander {flag}",
            reason=failure,
            flag=flag,
        ).strip(),
    )
    app = QApplication.instance()
    if app is not None:
        app.exit(1)
