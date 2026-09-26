"""System check - is everything the game needs actually installed.

Read-only by design. The desktop page pairs every failing row with a
copyable install command and a manual override browser; neither is usable
without a keyboard and a file dialog, and running package-manager commands
is not something to drive from a thumbstick. What survives is the diagnosis,
which is what the Play screen sends people here for - and a row with more to
say is a focus stop: A opens its full detail, which on the desktop lives in
a tooltip the Deck has no hover to show.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout

from commander_gui.i18n import tr
from commander_gui.ui.common import BackgroundTask
from commander_gui.ui.system_check_page import CHECK_SECTIONS, _collect_checks

from .. import gamepad as pad
from ..scale import px
from ..widgets import MIN_TOUCH, DeckStatusRow, deck_button, deck_label
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
        self._checked = False

        # Summary and Re-check share the top row. Below the rows they would
        # sit past the fold on a list this long, and re-checking is the one
        # thing a user comes back to this screen to do.
        header = QHBoxLayout()
        header.setSpacing(px(12))
        self.summary = deck_label(tr("Checking..."), role="body", wrap=True)
        # Always accent-colored, matching desktop's system_check_page.py -
        # the summary line stays #accent even when reporting something is
        # missing, a deliberate desktop quirk this mirrors exactly.
        self.summary.setObjectName("deckBodyAccent")
        header.addWidget(self.summary, 1)
        self.recheck_button = deck_button(tr("Re-check"), on_click=self._start)
        self.recheck_button.setFixedWidth(px(220))
        header.addWidget(self.recheck_button)
        self.body.addLayout(header)

        self.rows_from = self.body.count()
        self.body.addStretch(1)

    def refresh(self) -> None:
        # Checked once per session, then on demand: the probes shell out to
        # vulkaninfo, winetricks and protontricks, several seconds on the
        # Deck - too slow to repeat on every tab switch.
        if self._task is None and not self._checked:
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
                # Detached now, deleted later: a re-check must not leave the
                # old rows findable (or painted) until the deferred delete.
                widget.setParent(None)
                widget.deleteLater()

    def _render(self, result: object) -> None:
        self._task = None
        self._checked = True
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
        # Grouped like the desktop page (CHECK_SECTIONS); anything a newer
        # _collect_checks() adds that no section names goes under "Other"
        # rather than vanishing.
        by_label = {str(check.get("label", "")): check for check in checks}
        grouped = {label for _title, labels in CHECK_SECTIONS for label in labels}
        sections = [
            (title, [by_label[label] for label in labels if label in by_label])
            for title, labels in CHECK_SECTIONS
        ]
        extra = [c for c in checks if str(c.get("label", "")) not in grouped]
        if extra:
            sections.append(("Other", extra))
        position = self.rows_from
        for title, section_checks in sections:
            if not section_checks:
                continue
            heading = deck_label(tr(title), role="section")
            self.body.insertWidget(position, heading)
            position += 1
            for check in section_checks:
                for row in self._rows_for(check):
                    self.body.insertWidget(position, row)
                    position += 1

    def _rows_for(self, check: dict) -> list:
        """One row per check - or, for Proton Builds, one per build found."""
        if "builds" in check:
            builds = check.get("builds") or []
            if not builds:
                row = DeckStatusRow(tr("Proton build"), interactive=True)
                row.set_status(tr("Missing"), "bad")
                row.set_detail(tr("No compatible build detected."))
                return [row]
            rows = []
            for name, state, *rest in builds:
                row = DeckStatusRow(str(name), interactive=True)
                row.set_status(
                    tr(_CHIP_TEXT.get(str(state), "Missing")),
                    _CHIP_STATE.get(str(state), "bad"),
                )
                if rest and rest[0]:
                    row.set_detail(str(rest[0]))
                rows.append(row)
            return rows
        state = str(check.get("state", "missing"))
        row = DeckStatusRow(str(check.get("label", "")), interactive=True)
        row.set_status(
            tr(_CHIP_TEXT.get(state, "Missing")),
            _CHIP_STATE.get(state, "bad"),
        )
        command = str(check.get("command", "") or "").strip()
        if command:
            # "Wine | Copy install command": the desktop page's copy
            # button, right after the name, so the fix is one press away
            # - no detail window to open first.
            # Placed just left of the status chip, so every copy button
            # sits in one column and Up/Down run straight down it instead
            # of zig-zagging with the length of each name.
            layout = row.layout()
            copy = deck_button(tr("Copy install command"))
            copy.setObjectName("deckCopyCommand")
            copy.setMinimumHeight(px(MIN_TOUCH))
            copy.setFixedHeight(px(MIN_TOUCH))
            copy.clicked.connect(
                lambda _=False, value=command, button=copy: self._copy(value, button)
            )
            layout.insertWidget(layout.count() - 1, copy, 0, Qt.AlignmentFlag.AlignVCenter)
            row.copy_button = copy
        return [row]

    def _copy(self, command: str, button) -> None:
        QGuiApplication.clipboard().setText(command)
        button.setText(tr("Copied!"))
        # The button as context: a re-check that replaces the row first
        # simply cancels this.
        QTimer.singleShot(1500, button, lambda: _revert(button))

    def on_action(self, action: str) -> bool:
        if action == pad.CONTEXT:
            self._start()
            return True
        return False

    def hints(self):
        return [("A", "Select"), ("X", "Re-check"), ("B", "Back"), ("L1 R1 / L2 R2", "Switch tab")]

    def default_focus(self):
        return self.recheck_button if self.recheck_button.isEnabled() else None

    def _error(self, message: str) -> None:
        self._task = None
        self.recheck_button.setEnabled(True)
        self.summary.setText(tr("Failed") + ": " + message)


def _revert(button) -> None:
    """Put a copy button's label back - unless a re-check already replaced
    the row, and with it the button."""
    try:
        button.setText(tr("Copy install command"))
    except RuntimeError:
        pass
