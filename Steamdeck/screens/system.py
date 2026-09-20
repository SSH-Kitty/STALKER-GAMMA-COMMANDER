"""System check - is everything the game needs actually installed.

Read-only by design. The desktop page pairs every failing row with a
copyable install command and a manual override browser; neither is usable
without a keyboard and a file dialog, and running package-manager commands
is not something to drive from a thumbstick. What survives is the diagnosis,
which is what the Play screen sends people here for.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout

from commander_gui.i18n import tr
from commander_gui.ui.common import BackgroundTask
from commander_gui.ui.system_check_page import _collect_checks

from ..widgets import DeckStatusRow, deck_button, deck_label
from .base import DeckScreen

#: _collect_checks' own vocabulary, mapped onto the chip styles. "optional"
#: means a fallback covers for it - a warning, not a failure.
_CHIP_STATE = {"ready": "ok", "optional": "warn", "missing": "bad"}
_CHIP_TEXT = {
    "ready": "OK",
    "optional": "Optional",
    "missing": "Missing",
}


class SystemScreen(DeckScreen):
    def build(self) -> None:
        self._task = None

        # Summary and Re-check share the top row. Below the rows they would
        # sit past the fold on a list this long, and re-checking is the one
        # thing a user comes back to this screen to do.
        header = QHBoxLayout()
        header.setSpacing(12)
        self.summary = deck_label(tr("Checking..."), role="body", wrap=True)
        header.addWidget(self.summary, 1)
        self.recheck_button = deck_button(tr("Re-check"), on_click=self._start)
        self.recheck_button.setFixedWidth(220)
        header.addWidget(self.recheck_button)
        self.body.addLayout(header)

        self.rows_from = self.body.count()
        self.body.addStretch(1)

    def refresh(self) -> None:
        if self._task is None:
            self._start()

    def _start(self) -> None:
        if self._task is not None:
            return
        self.summary.setText(tr("Checking..."))
        self.recheck_button.setEnabled(False)
        # Off the GUI thread: the checks shell out to vulkaninfo, winetricks
        # and protontricks, which take seconds on the Deck's storage.
        self._task = BackgroundTask(_collect_checks, parent=self)
        self._task.result.connect(self._render)
        self._task.error.connect(self._error)
        self._task.start()

    def _clear_rows(self) -> None:
        # Everything between the header row and the trailing stretch.
        while self.body.count() > self.rows_from + 1:
            item = self.body.takeAt(self.rows_from)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _render(self, result: object) -> None:
        self._task = None
        self.recheck_button.setEnabled(True)
        try:
            checks, ready, _overrides = result
        except (TypeError, ValueError):
            self.summary.setText(tr("Failed"))
            return
        self._clear_rows()
        self.summary.setText(
            tr("Everything the game needs is installed.")
            if ready
            else tr("Something needed by the game is missing.")
        )
        for index, check in enumerate(checks):
            state = str(check.get("state", "missing"))
            row = DeckStatusRow(str(check.get("label", "")))
            row.set_status(
                tr(_CHIP_TEXT.get(state, "Missing")),
                _CHIP_STATE.get(state, "bad"),
            )
            detail = str(check.get("detail", "")).strip()
            if detail:
                row.setToolTip(detail)
            self.body.insertWidget(self.rows_from + index, row)

    def _error(self, message: str) -> None:
        self._task = None
        self.recheck_button.setEnabled(True)
        self.summary.setText(tr("Failed") + ": " + message)
