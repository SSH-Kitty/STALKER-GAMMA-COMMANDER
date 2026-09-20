"""Checking for and applying GAMMA updates.

The desktop Updates page shows a full Added/Modified/Removed diff of every
addon that changed. On a 7.4" screen that is a wall of text nobody reads;
what matters is whether there is an update, how big the change is, and the
release notes. Applying reuses the same CLI command and the same install
lock, so an update started here is indistinguishable from one started in
the full interface.
"""

from __future__ import annotations

from commander_gui.cli_runner import cli_command
from commander_gui.i18n import tr
from commander_gui.parsers import parse_progress_line, strip_ansi
from commander_gui.settings import cli_ok
from commander_gui.ui.common import (
    BackgroundTask,
    CommandRunner,
    aggregate_progress_value,
    mo2_running,
)
from commander_gui.updates import (
    check_updates,
    format_version,
    parse_patchnotes_sections,
    status_summary,
)

from ..widgets import DeckCard, DeckProgress, deck_button, deck_label
from .base import DeckScreen


class UpdateScreen(DeckScreen):
    def build(self) -> None:
        self._task: BackgroundTask | None = None
        self._runner: CommandRunner | None = None
        self._status = None
        self._heavy: dict[str, float] = {}

        self.card = DeckCard()
        self.version_label = deck_label("", role="title")
        self.summary_label = deck_label("", role="body", wrap=True)
        self.card.body.addWidget(self.version_label)
        self.card.body.addWidget(self.summary_label)
        self.body.addWidget(self.card)

        self.action_button = deck_button(
            tr("Check for updates"), role="primary", on_click=self._on_action
        )
        self.body.addWidget(self.action_button)

        self.progress = DeckProgress()
        self.progress.hide()
        self.body.addWidget(self.progress)

        self.body.addWidget(deck_label(tr("Patch notes"), role="rowTitle"))
        self.notes_label = deck_label("", role="body", wrap=True)
        self.body.addWidget(self.notes_label)
        self.body.addStretch(1)

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        profile = self.profile()
        if profile is None:
            self.version_label.setText(tr("No Profile"))
            self.summary_label.setText(
                tr("Create or activate a profile first (Profiles page).")
            )
            self.action_button.setEnabled(False)
            return
        self.action_button.setEnabled(
            not self.window.install_busy and self._task is None
        )
        if self._status is None and self._task is None:
            self._check()

    def on_busy_changed(self, busy: bool) -> None:
        self.action_button.setEnabled(not busy and self._runner is None)

    def _on_action(self) -> None:
        if self._status is not None and self._status.update_available:
            self._confirm_apply()
        else:
            self._check()

    # -- checking ---------------------------------------------------------
    def _check(self) -> None:
        profile = self.profile()
        if profile is None or self._task is not None:
            return
        self.action_button.setEnabled(False)
        self.summary_label.setText(tr("Checking..."))
        self._task = BackgroundTask(check_updates, profile, parent=self)
        self._task.result.connect(self._on_checked)
        self._task.error.connect(self._on_check_error)
        self._task.start()

    def _on_checked(self, status: object) -> None:
        self._task = None
        self._status = status
        self.action_button.setEnabled(not self.window.install_busy)

        installed = format_version(
            getattr(status, "installed", None), getattr(status, "installed_human", None)
        )
        latest = format_version(
            getattr(status, "latest", None), getattr(status, "latest_human", None)
        )
        self.version_label.setText(f"{installed}  →  {latest}")
        text, _kind = status_summary(status)
        self.summary_label.setText(text)
        self.action_button.setText(
            tr("Apply updates")
            if getattr(status, "update_available", False)
            else tr("Check for updates")
        )

        notes = getattr(status, "patchnotes", "") or ""
        sections = parse_patchnotes_sections(notes) if notes else []
        if sections:
            self.notes_label.setText(
                "\n\n".join(
                    f"{heading}\n{body}".strip() for heading, body in sections[:6]
                )
            )
        else:
            self.notes_label.setText(notes.strip() or tr("No patch notes."))

    def _on_check_error(self, message: str) -> None:
        self._task = None
        self.action_button.setEnabled(True)
        self.summary_label.setText(tr("Failed") + ": " + message)

    # -- applying ---------------------------------------------------------
    def _confirm_apply(self) -> None:
        if self.window.install_busy:
            return
        if mo2_running():
            self.window.notify(tr("Mod Organizer is running"))
            return
        self.window.confirm(
            tr("Update GAMMA"),
            tr(
                "Downloads and applies every changed addon. Your user.ltx "
                "and MCM settings are preserved."
            ),
            self._apply,
            confirm_text=tr("Apply"),
        )

    def _apply(self) -> None:
        args = [
            "update",
            "apply",
            "--preserve-user-settings",
            "--preserve-mcm-settings",
        ]
        self._heavy = {}
        self.window.set_install_busy(True, "gamma")
        self.progress.reset()
        self.progress.show()
        self.action_button.hide()
        self._runner = CommandRunner(
            cli_command(args, progress_interval_ms=200), parent=self
        )
        self._runner.line.connect(self._on_line)
        self._runner.finished.connect(self._on_applied)
        self._runner.cancelled.connect(self.progress.on_cancelled)
        self.progress.set_runner(self._runner)
        self.progress.on_started()
        self._runner.start()

    def _on_line(self, line: str) -> None:
        self.progress.on_line(line)
        event = parse_progress_line(strip_ansi(line))
        if event is None:
            return
        self.progress.set_percent(
            aggregate_progress_value(
                event.complete,
                event.total,
                event.percent,
                event.name,
                self._heavy,
                event.operation,
            )
        )
        self.progress.status_message(f"{event.operation}  {event.name}")

    def _on_applied(self, rc: int, output: str) -> None:
        cancelled = self._runner is not None and self._runner.was_cancelled
        self.progress.on_finished(rc, output)
        self._runner = None
        self.window.set_install_busy(False)
        self.progress.hide()
        self.action_button.show()
        if cancelled:
            self.window.notify(tr("Cancelled"))
        elif cli_ok(rc, output, ""):
            self.window.notify(tr("Done"))
            self._status = None
            self._check()
        else:
            self.window.notify(tr("Failed") + f" (exit {rc})", 8000)

    def on_back(self) -> bool:
        if self._runner is not None:
            self.window.notify(tr("An install is already running."))
            return True
        return False
