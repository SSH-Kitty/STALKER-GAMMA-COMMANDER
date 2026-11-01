"""Verify Integrity for Deck Mode: the desktop check and repair, Deck-sized.

The check-and-repair pipeline is the desktop Install page's own, borrowed
method by method (``_ENGINE``) rather than copied: it is some 1,500 lines of
careful ordering (set broken mods aside, reinstall, settle, re-check, only
then re-baseline) that must not drift between the two interfaces. Those
methods only need a handful of widgets to talk to, so this card supplies
Deck versions of them - the four tiles, the findings list, the progress
block, the Verify and Repair buttons - and replaces the few methods that
open a desktop dialog (the repair question, Stop) with Deck overlays.

The check options are the desktop's own saved preferences where it has
them ("Show the repair list right away"); the Anomaly and cache checks are
on each time, as on the desktop.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from commander_gui import gui_settings
from commander_gui.i18n import tr
from commander_gui.ui.common import clear_layout, game_running
from commander_gui.ui.install_page import _FINDING_ITEMS_SHOWN, InstallPage

from .scale import px
from .widgets import (
    DeckCard,
    DeckOverlay,
    DeckProgress,
    DeckStatusRow,
    DeckTextPanel,
    DeckToggleRow,
    deck_button,
    deck_chip,
    deck_label,
    deck_step_badge,
    repolish,
    side_by_side,
)

#: The desktop methods this card runs as its own (see the module docstring).
_ENGINE = (
    "_on_verify_line", "_on_anomaly_verify_finished", "_verify_phase_busy",
    "_set_verify_phase", "_start_gamma_verify", "_run_gamma_verify",
    "_on_gamma_verify_progress", "_on_gamma_verify_done", "_offer_repair",
    "_begin_repair_run", "_repair_prompt_text", "_arm_repair",
    "_advance_repair_pipeline", "_start_repo_repair", "_on_repo_repaired",
    "_start_gamma_overlay_restore", "_run_anomaly_repair",
    "_on_anomaly_repair_finished", "_on_anomaly_recheck_finished",
    "_conclude_verify", "_on_overlay_checked", "_deliver_verdict",
    "_start_gamma_repair_deletion", "_archive_reuse_keys",
    "_run_repair_quarantine", "_on_repair_quarantined",
    "_restore_all_quarantined", "_start_repair_install", "_run_repair_installer",
    "_restore_user_modlist", "_on_repair_install_finished",
    "_on_quarantine_settled", "_on_repair_install_cancelled",
    "_restore_after_repair_cancel", "_start_post_scan", "_run_post_scan",
    "_on_post_scan_done", "_on_gamma_verify_error", "_settle_running_tiles",
    "_update_verify_tiles", "_save_verify_result", "_show_saved_verify_result",
    "_set_saved_verify_pill", "_refresh_verify_age", "_stop_verify",
    "_on_verify_cancelled", "_anomaly_install_args",
)

#: Tile and finding states onto the Deck's three chip colours.
_CHIP_STATE = {"ok": "ok", "problem": "bad"}



def _level_text(level: str) -> str:
    return {
        "ok": tr("OK"),
        "warn": tr("Changed"),
        "problem": tr("Needs attention"),
    }.get(level, tr("Info"))


def finding_row(finding) -> QWidget:
    """An ``integrity.Finding`` as a row: its title and a status chip, with
    the explanation and file list one A press away. A bare note (info, no
    detail or list) is a sentence, so it stays one: a wrapped caption."""
    if finding.level == "info" and not finding.detail and not finding.items:
        return deck_label(finding.title, role="caption", wrap=True)
    title = f"{finding.count:,}  {finding.title}" if finding.count else finding.title
    row = DeckStatusRow(title, interactive=True)
    row.set_status(_level_text(finding.level), _CHIP_STATE.get(finding.level, "warn"))
    detail = [finding.detail] if finding.detail else []
    if finding.items:
        shown = finding.items[:_FINDING_ITEMS_SHOWN]
        detail.append("\n".join(shown))
        if len(finding.items) > len(shown):
            detail.append(tr("and {count} more", count=len(finding.items) - len(shown)))
    # A finding with nothing more to say is still read in full: its title
    # can be longer than the row.
    row.set_detail("\n\n".join(detail) or finding.title)
    return row


class _Flag:
    """A checkbox stand-in: the engine only ever asks ``isChecked()``."""

    def __init__(self, read) -> None:
        self._read = read

    def isChecked(self) -> bool:
        return bool(self._read())


class _Hidden:
    """Stands in for desktop widgets the Deck card has no use for."""

    def setVisible(self, _visible: bool) -> None:
        pass

    def isVisible(self) -> bool:
        return False


class _Log:
    """The desktop's technical log, reduced to the progress block's tail."""

    def __init__(self, progress: DeckProgress) -> None:
        self._progress = progress

    def append_line(self, line: str) -> None:
        self._progress.on_line(line)

    def isVisible(self) -> bool:
        return False


class _VerifyProgress(DeckProgress):
    """DeckProgress with the extra calls the verify engine makes.

    Cancel always means "stop the whole check or repair" - whichever stage
    is running, a CLI run or a file scan - so it goes to the engine's own
    stop rather than to whichever runner was handed over last.
    """

    def __init__(self, stop) -> None:
        super().__init__()
        self._stop = stop
        self.log = _Log(self)
        self.log_toggle = _Hidden()
        self.pause_button.hide()

    def set_runner(self, runner) -> None:
        self._runner = runner
        self.pause_button.hide()
        self.cancel_button.setEnabled(True)

    def _do_cancel(self) -> None:
        self._stop()
        self.status.setText(tr("Cancelling"))

    def set_success_state(self, text: str, _level: str = "") -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.bar.setFormat(text)


class _Tile(QFrame):
    """One of the four check results: title, big number, caption."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("deckTile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(12), px(10), px(12), px(10))
        layout.setSpacing(px(2))
        self.title = deck_label(title, role="caption")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value = deck_label("", role="statValue")
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption = deck_label("", role="caption", wrap=True)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)
        layout.addStretch(1)
        self.set_state("idle", "-", tr("Not checked yet"))

    def set_state(self, state: str, value: str, caption: str) -> None:
        self.state = state
        self.value.setText("—" if value == "-" else value)
        self.caption.setText(caption)
        self.value.setProperty("state", state)
        repolish(self.value)

    def snapshot(self) -> list[str]:
        return [self.state, self.value.text(), self.caption.text()]


class DeckVerifyCard(DeckCard):
    """Step 4 on the Install screen: check the install, then repair it."""

    def __init__(self, window, number: int, anomaly_mirror) -> None:
        super().__init__()
        self.window = window
        self._verify_runner = None
        self._repair_runner = None
        self._verify_task = None
        self._scan_cancel = None
        self._pending_repair = None

        head = QHBoxLayout()
        head.setSpacing(px(12))
        head.addWidget(deck_step_badge(number), 0)
        head.addWidget(
            deck_label(
                tr("Find and repair missing or changed game files. Your saves and settings are left alone."),
                role="body",
                wrap=True,
            ),
            1,
        )
        self.verify_pill = deck_chip("●  " + tr("Never checked"), "warn")
        head.addWidget(self.verify_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body.addLayout(head)

        self.verify_tiles = {
            "anomaly": _Tile(tr("Anomaly files")),
            "mods": _Tile(tr("GAMMA mods")),
            "md5": _Tile(tr("Mod file changes")),
            "cache": _Tile(tr("Download cache")),
        }
        tiles = QGridLayout()
        tiles.setSpacing(px(10))
        for index, tile in enumerate(self.verify_tiles.values()):
            tiles.addWidget(tile, 0, index)
            tiles.setColumnStretch(index, 1)
        self.body.addLayout(tiles)

        # Results, one row each; A on one lists its files.
        self.verify_findings = QVBoxLayout()
        self.verify_findings.setSpacing(px(8))
        self.body.addLayout(self.verify_findings)

        saved = gui_settings.load_gui_settings()
        self.anomaly_row = DeckToggleRow(tr("Anomaly files"), True)
        self.cache_row = DeckToggleRow(tr("Download cache"), True)
        self.prompt_row = DeckToggleRow(
            tr("Show the repair list right away"), bool(saved.get("verify_repair_prompt", False))
        )
        self.prompt_row.toggled.connect(
            lambda on: gui_settings.save_gui_settings(verify_repair_prompt=on)
        )
        self.body.addLayout(side_by_side(self.anomaly_row, self.cache_row))
        self.body.addWidget(self.prompt_row)
        self.verify_checkboxes = {
            "check_anomaly": _Flag(self.anomaly_row.is_checked),
            "check_cache": _Flag(self.cache_row.is_checked),
            "repair_prompt": _Flag(self.prompt_row.is_checked),
        }
        # A repair never keeps engine exes the player swapped in: the
        # desktop's own default, and Deck Mode has no such option.
        self.checkboxes = {"keep_engine_exes": _Flag(lambda: False)}
        self.anomaly_checkboxes = {"anomaly_mirror": _Flag(anomaly_mirror)}

        self.verify_progress = _VerifyProgress(self._stop_verify)
        self.verify_progress.hide()
        self.body.addWidget(self.verify_progress)

        self.verify_button = deck_button(
            tr("Verify Integrity"), role="primary", on_click=self._start_verify
        )
        self.repair_button = deck_button(
            tr("Repair now"), role="danger", on_click=self._on_repair_clicked
        )
        self.repair_button.hide()
        self.body.addLayout(side_by_side(self.verify_button, self.repair_button))

    # -- screen hooks -------------------------------------------------------
    def refresh(self) -> None:
        if self._busy():
            return
        if self.window.settings.active_profile is None:
            for tile in self.verify_tiles.values():
                tile.set_state("idle", "-", tr("Not checked yet"))
            clear_layout(self.verify_findings)
            self._set_verify_pill("", tr("No Profile"))
            self.repair_button.hide()
            return
        self._show_saved_verify_result()

    def set_enabled(self, idle: bool) -> None:
        has_profile = self.window.settings.active_profile is not None
        running = self._busy()
        self.verify_button.setEnabled(idle and has_profile and not running)
        self.repair_button.setEnabled(idle and not running)
        for row in (self.anomaly_row, self.cache_row):
            row.setEnabled(not running)

    def _busy(self) -> bool:
        return (
            self._scan_cancel is not None
            or (self._verify_runner is not None and self._verify_runner.is_running())
            or self._verify_task is not None
        )

    def busy(self) -> bool:
        """A check or repair is running (B must not walk away from it)."""
        return self._busy()

    # -- engine overrides ---------------------------------------------------
    def _start_verify(self) -> None:
        if self._busy():
            return
        if self.window.install_busy:
            self.window.notify(tr("Another task is already running."))
            return
        if game_running(force=True):
            self.window.notify(tr("Close the game first"))
            return
        if self.window.settings.active_profile is None:
            self.window.notify(tr("Create or activate a profile first (Dashboard → Profiles)."))
            return
        self.verify_progress.show()
        InstallPage._start_verify(self)
        self.verify_progress.cancel_button.show()
        self.verify_progress.cancel_button.setEnabled(True)
        self.verify_progress.focus_controls()

    def _on_repair_clicked(self) -> None:
        pending = self._pending_repair
        profile = self.window.settings.active_profile
        if (
            pending is None
            or profile is None
            or pending[3] != (profile.anomaly, profile.gamma, profile.cache)
        ):
            self._pending_repair = None
            self.repair_button.hide()
            self.window.notify(
                tr("The last check was for a different profile or folder. Run Verify Integrity again, then repair."),
                6000,
            )
            return
        if self.window.install_busy:
            self.window.notify(tr("Another task is already running."))
            return
        if game_running(force=True):
            self.window.notify(tr("Close the game first"))
            return
        text, details = self._repair_prompt_text(*pending[:3])

        def _details() -> None:
            panel = DeckTextPanel(details, min_height=200, max_height=440)
            self.window.show_overlay(
                DeckOverlay(
                    tr("Show details"),
                    panel,
                    [(tr("Back"), self.window.dismiss_overlay, "primary")],
                    panel_width=1100,
                ),
                stacked=True,
            )

        def _go() -> None:
            self.window.dismiss_overlay()
            if self.window.install_busy or self._busy():
                return
            self._arm_repair(*pending[:3])
            self._pending_repair = None
            self.repair_button.hide()
            self.verify_progress.show()
            self._begin_repair_run()
            self.verify_progress.cancel_button.show()
            self.verify_progress.cancel_button.setEnabled(True)
            self.verify_progress.focus_controls()
            self._advance_repair_pipeline()

        body = DeckTextPanel(text, min_height=200, max_height=400)
        self.window.show_overlay(
            DeckOverlay(
                tr("Repair now?"),
                body,
                [
                    (tr("Repair"), _go, "danger"),
                    (tr("Show details"), _details, "normal"),
                    (tr("Cancel"), self.window.dismiss_overlay, "normal"),
                ],
                panel_width=1100,
                default_index=2,
            )
        )

    def _finish_verify(self, ok, message, summary, baseline_created=False, verdict=None):
        InstallPage._finish_verify(self, ok, message, summary, baseline_created, verdict)
        # The pill and the findings carry the result; a finished bar under
        # them would only repeat it.
        self.verify_progress.hide()
        self.window.announce_finished(summary, message, ok=ok)
        self.set_enabled(True)

    def _verify_cancel_done(self) -> None:
        InstallPage._verify_cancel_done(self)
        # The desktop leaves these for the next run to overwrite; here they
        # also say whether a run is still going (_busy).
        self._verify_runner = None
        self._verify_task = None
        self._scan_cancel = None
        self.verify_progress.hide()
        self.set_enabled(True)

    def _set_verify_pill(self, level: str, text: str) -> None:
        # The leading dot, as on every other status chip in Deck Mode.
        self.verify_pill.setText(f"●  {text}")
        self.verify_pill.setProperty("level", level)
        self.verify_pill.setObjectName(
            {"healthy": "deckChipOk", "snapshot": "deckChipOk", "attention": "deckChipBad"}.get(
                level, "deckChipWarn"
            )
        )
        repolish(self.verify_pill)

    def _render_findings(self, findings) -> None:
        clear_layout(self.verify_findings)
        for finding in findings:
            self.verify_findings.addWidget(finding_row(finding))

    def _set_verify_console(self, _running: bool) -> None:
        """The desktop's Show Console toggle; the Deck shows its log tail."""


for _name in _ENGINE:
    setattr(DeckVerifyCard, _name, getattr(InstallPage, _name))
del _name
