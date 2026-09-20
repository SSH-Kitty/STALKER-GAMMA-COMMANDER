"""GUI-side crash/error logging.

Nothing in this app previously persisted an unhandled exception anywhere -
it just printed to stderr and, depending on where it happened, could take
the whole process down with no record left behind. ``install_excepthook()``
gives every uncaught exception a durable home (``logs_dir()/commander.log``,
picked up automatically by ``build_log_dump()`` since it already sweeps the
whole logs directory) and a non-crashing fallback dialog, instead of a
silent terminal death.
"""

from __future__ import annotations

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler

from .config import logs_dir

#: Matches launcher.log's own single-rotation convention (launcher.py).
_MAX_LOG_BYTES = 1 << 20

_logger = logging.getLogger("commander_gui")
_configured = False


def _ensure_configured() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    try:
        logs_dir().mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            logs_dir() / "commander.log",
            maxBytes=_MAX_LOG_BYTES,
            backupCount=1,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        _logger.addHandler(handler)
        _logger.setLevel(logging.INFO)
    except OSError:
        # No writable logs dir - logging becomes a no-op rather than a
        # startup failure.
        pass


def get_logger() -> logging.Logger:
    """The shared commander_gui logger (``logs_dir()/commander.log``), configured on first use."""
    _ensure_configured()
    return _logger


def log_exception(exc_type, exc_value, exc_tb) -> None:
    """Record an exception; safe to call even if the handler never attached."""
    _ensure_configured()
    try:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _logger.error("Unhandled exception:\n%s", text)
    except Exception:  # noqa: BLE001, S110 - logging itself must never crash the app
        pass


def install_excepthook() -> None:
    """Route uncaught exceptions to the log file instead of a silent crash.

    Only logs - deliberately does not try to show a dialog from inside the
    hook itself: an exception raised while a Qt widget/event is in a broken
    state is exactly the situation where constructing another widget is
    most likely to fail too, and a hook that raises out of itself takes
    Python's default unraisable-error path anyway. main() still gets a
    complete, real traceback in the log to diagnose after the fact.
    """
    previous = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb) -> None:
        log_exception(exc_type, exc_value, exc_tb)
        previous(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook
