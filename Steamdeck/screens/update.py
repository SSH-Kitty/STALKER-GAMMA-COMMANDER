"""Checking for and applying GAMMA updates.

Version banner, then *what* changed - every added, modified and removed
addon from the same diff the desktop Updates page tabulates - then the
button, then the release notes (one collapsible entry per release).
Applying reuses the same CLI command and the same install lock, so an
update started here is indistinguishable from one started in the full
interface.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from commander_gui.cli_runner import cli_command
from commander_gui.game_backup import backup_settings_before
from commander_gui.i18n import tr
from commander_gui.parsers import parse_progress_line, strip_ansi
from commander_gui.settings import cli_ok
from commander_gui.themes import active_theme_tokens
from commander_gui.ui.common import (
    BackgroundTask,
    CommandRunner,
    aggregate_progress_value,
    mo2_running,
)
from commander_gui.updates import (
    check_updates,
    parse_patchnotes_sections,
    status_summary,
)

from .. import gamepad as pad
from ..scale import px
from ..widgets import (
    ROW_GAP,
    DeckProgress,
    DeckRow,
    DeckTextPanel,
    deck_button,
    deck_label,
    repolish,
)
from .base import DeckScreen
from .install import battery_warning

#: Releases listed under Patch notes (Patchnotes.md holds the whole history).
_MAX_RELEASES = 6

_MD_HEADING = re.compile(r"^\s*#{1,6}\s*")


def _plain_notes(body: str) -> str:
    """Patch-note Markdown as readable plain text: no "##" or "**" marks."""
    lines = []
    for line in body.splitlines():
        line = _MD_HEADING.sub("", line)
        lines.append(line.replace("**", "").replace("__", ""))
    return "\n".join(lines).strip()


_LONG_NAME = re.compile(r"S\.?T\.?A\.?L\.?K\.?E\.?R\.?\s+G\.?A\.?M\.?M\.?A\.?", re.IGNORECASE)
_PATCH_NOTES = re.compile(r"\s*[-–:]?\s*patch\s*notes\s*$", re.IGNORECASE)


def _release_title(title: str) -> str:
    """"S.T.A.L.K.E.R. G.A.M.M.A. 0.9.4 Patch Notes" -> "GAMMA 0.9.4".

    The heading wording has drifted between releases; shortened this way
    they read as one list and fit a row.
    """
    short = _PATCH_NOTES.sub("", _LONG_NAME.sub("GAMMA", title)).strip(" -–:")
    return short or title


class _ReleaseNotes(QWidget):
    """One release: a header row that opens and closes its notes."""

    def __init__(self, title: str, body: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(8))
        self.header = DeckRow(_release_title(title), tr("Show") + "  ▾", chevron=False)
        self.header.activated.connect(self.toggle)
        layout.addWidget(self.header)
        self.panel = DeckTextPanel(_plain_notes(body) or tr("No patch notes."), min_height=160, max_height=420)
        self.panel.hide()
        layout.addWidget(self.panel)

    def is_open(self) -> bool:
        return self.panel.isVisible()

    def toggle(self) -> None:
        opening = not self.panel.isVisible()
        self.panel.setVisible(opening)
        self.header.set_value(tr("Hide") + "  ▴" if opening else tr("Show") + "  ▾")


class UpdateScreen(DeckScreen):
    def build(self) -> None:
        self._task: BackgroundTask | None = None
        self._runner: CommandRunner | None = None
        self._status = None
        self._heavy: dict[str, float] = {}

        self.action_button = deck_button(
            tr("Check for updates"), role="primary", on_click=self._on_action
        )
        self.body.addWidget(self.action_button)

        # One line under the button - "GAMMA is up to date" / "Update
        # available (...) - 3 change(s)" - instead of a version card above
        # it that mostly repeated the same build number twice.
        self.summary_label = deck_label("", role="body", wrap=True)
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.summary_label)

        self.progress = DeckProgress()
        self.progress.hide()
        self.body.addWidget(self.progress)

        # What an "update available" actually changes, mod by mod. Before
        # this the screen said "2 change(s)" and gave no way to see which.
        self.changes_title = deck_label(tr("Changes"), role="rowTitle")
        self.changes_summary = deck_label("", role="body")
        self.changes_summary.setTextFormat(Qt.TextFormat.RichText)
        # Rich text in a D-pad-scrollable panel: each change is coloured by
        # kind, which a stylesheet-coloured QListWidget can't do per row.
        self.changes = DeckTextPanel("", min_height=60, max_height=300)
        self.changes.label.setTextFormat(Qt.TextFormat.RichText)
        for widget in (self.changes_title, self.changes_summary, self.changes):
            widget.hide()
            self.body.addWidget(widget)

        self.body.addWidget(deck_label(tr("Patch notes"), role="rowTitle"))
        # One collapsible entry per release, newest first and all closed:
        # the whole history as one wall of text filled most of the screen.
        # A (or a tap) on a version opens its notes in a panel the D-pad
        # scrolls line by line.
        self._releases: list[_ReleaseNotes] = []
        self.notes_box = QVBoxLayout()
        self.notes_box.setContentsMargins(0, 0, 0, 0)
        self.notes_box.setSpacing(px(ROW_GAP))
        self.body.addLayout(self.notes_box)
        self.notes_empty = deck_label("", role="caption", wrap=True)
        self.notes_box.addWidget(self.notes_empty)
        self.body.addStretch(1)

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        profile = self.profile()
        if profile is None:
            self.summary_label.setText(
                tr("Create or activate a profile first (Dashboard → Profiles).")
            )
            self._reset_summary_color()
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
        self.update_hints()
        self.summary_label.setText(tr("Checking..."))
        self._reset_summary_color()
        self._task = BackgroundTask(check_updates, profile, parent=self)
        self._task.result.connect(self._on_checked)
        self._task.error.connect(self._on_check_error)
        self._task.start()

    def _on_checked(self, status: object) -> None:
        self._task = None
        self._status = status
        self.action_button.setEnabled(not self.window.install_busy)

        text, kind = status_summary(status)
        self.summary_label.setText(text)
        # kind is "accent" or "warn", matching desktop's own #accent/#warn
        # object names for this exact text (see update_page.py's
        # _set_status()).
        self.summary_label.setObjectName(
            "deckBodyAccent" if kind == "accent" else "deckBodyWarn"
        )
        repolish(self.summary_label)
        self.action_button.setText(
            tr("Apply updates")
            if getattr(status, "update_available", False)
            else tr("Check for updates")
        )
        if self.window.current_page() is self:
            self.window.update_hints()

        self._render_changes(list(getattr(status, "diffs", []) or []))

        notes = getattr(status, "patchnotes", "") or ""
        sections = parse_patchnotes_sections(notes) if notes else []
        if not sections and notes.strip():
            # Unexpected format: still show it, as one untitled entry.
            sections = [(tr("Patch notes"), notes.strip())]
        self._render_releases(sections[:_MAX_RELEASES])
        self._settle_focus()

    def _settle_focus(self) -> None:
        """After a check: focus on something here if it had nowhere to go.

        On a first visit the check starts before focus arrives, with the
        only button disabled - focus then had nothing on this screen to land
        on and stayed wherever it was.
        """
        self.update_hints()
        if self.window.current_page() is not self or self.window.current_overlay() is not None:
            return
        app = QApplication.instance()
        current = app.focusWidget() if app is not None else None
        if current is not None and self.isAncestorOf(current) and current.isVisible():
            return
        target = self.default_focus()
        if target is None and self._releases:
            target = self._releases[0].header
        if target is not None:
            self.window._focus.focus(target)

    def default_focus(self):
        if self.action_button.isVisible() and self.action_button.isEnabled():
            return self.action_button
        return None

    def _render_releases(self, sections) -> None:
        # Rebuilt on every check: keep focus on the same release, not lose
        # it with the widget it was on.
        app = QApplication.instance()
        focused = app.focusWidget() if app is not None else None
        keep_title = next(
            (
                release.header.title_label.text()
                for release in self._releases
                if focused is not None and (release is focused or release.isAncestorOf(focused))
            ),
            None,
        )
        for release in self._releases:
            self.notes_box.removeWidget(release)
            release.deleteLater()
        self._releases = []
        self.notes_empty.setText("" if sections else tr("No patch notes."))
        self.notes_empty.setVisible(not sections)
        for title, body in sections:
            release = _ReleaseNotes(title, body)
            self.notes_box.addWidget(release)
            self._releases.append(release)
        if keep_title is not None:
            match = next(
                (r for r in self._releases if r.header.title_label.text() == keep_title), None
            )
            if match is None and self._releases:
                match = self._releases[0]
            if match is not None:
                self.window._focus.focus(match.header)

    def _render_changes(self, diffs) -> None:
        from html import escape

        tokens = active_theme_tokens()
        colours = {
            "Added": "#7dc963",
            "Modified": tokens.get("accent_strong", "#9fe96f"),
            "Removed": tokens.get("warn", "#d9a04c"),
        }
        marks = {"Added": "+", "Modified": "~", "Removed": "−"}
        counts = {"Added": 0, "Modified": 0, "Removed": 0}
        lines = []
        dim = tokens.get("text_dim", "#888888")
        for diff in diffs:
            kind = str(getattr(diff, "status", ""))
            counts[kind] = counts.get(kind, 0) + 1
            colour = colours.get(kind, tokens.get("text", "#dddddd"))
            line = (
                f"<span style='color:{colour}; font-weight:bold;'>"
                f"{marks.get(kind, '•')}&nbsp;&nbsp;{escape(tr(kind))}</span>"
                f"&nbsp;&nbsp;&nbsp;{escape(str(getattr(diff, 'text', '')))}"
            )
            detail = str(getattr(diff, "detail", "") or "")
            if detail:
                line += f"<span style='color:{dim};'>&nbsp;&nbsp;—&nbsp;&nbsp;{escape(detail)}</span>"
            lines.append(line)
        self.changes.setText(
            "".join(f"<p style='margin:0 0 {px(8)}px 0;'>{line}</p>" for line in lines)
        )
        # Tall enough for the list (up to about seven lines), no taller.
        self.changes.setFixedHeight(min(px(300), len(lines) * px(30) + px(22)))
        has = bool(diffs)
        for widget in (self.changes_title, self.changes_summary, self.changes):
            widget.setVisible(has)
        if not has:
            return
        self.changes_summary.setText(
            "&nbsp;&nbsp;&nbsp;".join(
                f"<span style='color:{colours[kind]}; font-weight:bold;'>"
                f"{count} {escape(tr(kind).lower())}</span>"
                for kind, count in counts.items()
                if count
            )
        )

    def on_action(self, action: str) -> bool:
        if action == pad.CONTEXT:
            if self.action_button.isEnabled() and self.action_button.isVisible():
                self._on_action()
            return True
        if action == pad.SEARCH:
            if self.changes.isVisible():
                self.changes.setFocus(Qt.FocusReason.OtherFocusReason)
            elif self._releases:
                self._releases[0].header.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        return False

    def hints(self):
        available = self._status is not None and getattr(
            self._status, "update_available", False
        )
        hints = [("A", "Select")]
        if self.action_button.isEnabled() and self.action_button.isVisible():
            hints.append(("X", "Apply updates" if available else "Check"))
        # Y goes to the changes list, or the patch notes - only when there
        # is one of them to go to.
        if self.changes.isVisible():
            hints.append(("Y", "Changes"))
        elif self._releases:
            hints.append(("Y", "Patch notes"))
        return hints + [
            ("L1 R1 / L2 R2", "Switch tab"),
        ]

    def _reset_summary_color(self) -> None:
        self.summary_label.setObjectName("deckBody")
        repolish(self.summary_label)

    def _on_check_error(self, message: str) -> None:
        self._task = None
        self.action_button.setEnabled(not self.window.install_busy)
        self.summary_label.setText(tr("Failed") + ": " + message)
        self.summary_label.setObjectName("deckBodyWarn")
        repolish(self.summary_label)

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
            )
            + battery_warning(),
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
        error = backup_settings_before(self.profile(), "update")
        if error:
            self.window.notify(tr("Settings backup failed: {message}", message=error), 6000)
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
        self.progress.focus_controls()

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
        self.action_button.setFocus()
        if cancelled:
            self.window.notify(tr("Cancelled"))
        elif cli_ok(rc, output, ""):
            self.window.announce_finished(
                tr("GAMMA update finished"), tr("GAMMA update completed successfully.")
            )
            self.window.update_mod_counter()
            self._status = None
            self._check()
        else:
            self.window.announce_finished(
                tr("GAMMA update failed"), tr("Failed") + f" (exit {rc})", ok=False
            )

    def on_back(self) -> bool:
        if self._runner is not None:
            self.window.notify(tr("An install is already running."))
            return True
        return False
