"""The at-a-glance state of the install.

Deck Mode's other screens each do one thing. This one answers the question
you actually have when you pick the device up: is everything still where I
left it - installed, up to date, enough space, which profile am I on.

It is a hub as well as a readout. The rows that have somewhere to go are
focusable and go there; the rows that are pure status are skipped by the
D-pad, so holding a direction walks between the things you can act on
rather than stopping on every line.

Nothing here computes anything the other screens don't. Install status,
update status and mod counts come from the same backend calls those screens
make; the one thing this screen owns is the storage total, because nowhere
else in Deck Mode reports it.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt

from commander_gui import gui_settings
from commander_gui.dependencies import check_all_dependencies
from commander_gui.i18n import tr
from commander_gui.ui.common import (
    BackgroundTask,
    anomaly_installed,
    count_active_mods,
    dir_size,
    format_last_played,
    format_playtime,
    free_space_bytes,
    gamma_installed,
    human_size,
)
from commander_gui.updates import check_updates, format_version

from ..widgets import DeckCard, DeckRow, DeckStatusRow, deck_label, deck_two_column_card
from .base import DeckScreen

#: Directory sizes mean walking the whole install tree, which on a Deck's SD
#: card is slow enough to notice. The desktop Dashboard caches for the same
#: reason and by the same margin.
_SIZE_CACHE_S = 30.0


class DashboardScreen(DeckScreen):
    def build(self) -> None:
        # Seven rows plus the footer is twelve pixels more than the standard
        # gap leaves room for, and this is the one screen where seeing
        # everything at once is the entire point - so it gives the gap back
        # rather than making the user scroll for the last line.
        self.body.setSpacing(8)

        self._update_task: BackgroundTask | None = None
        self._deps_task: BackgroundTask | None = None
        self._size_task: BackgroundTask | None = None
        self._sizes_at = 0.0

        self.profile_row = DeckRow(tr("Active profile"))
        self.profile_row.activated.connect(
            lambda: self.window.set_page("profile")
        )
        self.body.addWidget(self.profile_row)

        install_card, anomaly_col, gamma_col = deck_two_column_card()
        self.anomaly_row = DeckStatusRow(tr("STALKER Anomaly"))
        self.anomaly_detail = deck_label("", role="caption", wrap=True)
        anomaly_col.addWidget(self.anomaly_row)
        anomaly_col.addWidget(self.anomaly_detail)

        self.gamma_row = DeckStatusRow(tr("GAMMA Modpack"))
        self.gamma_detail = deck_label("", role="caption", wrap=True)
        gamma_col.addWidget(self.gamma_row)
        gamma_col.addWidget(self.gamma_detail)
        self.body.addWidget(install_card)

        deps_card = DeckCard()
        self.deps_row = DeckStatusRow(tr("Dependencies"))
        deps_card.body.addWidget(self.deps_row)
        self.deps_detail = deck_label("", role="caption", wrap=True)
        deps_card.body.addWidget(self.deps_detail)
        self.body.addWidget(deps_card)

        self.updates_card = DeckCard()
        self.updates_installed_label = deck_label("", role="rowValue")
        self.updates_status_label = deck_label("", role="body", wrap=True)
        self.updates_card.body.addWidget(deck_label(tr("Updates"), role="rowTitle"))
        self.updates_card.body.addWidget(self.updates_installed_label)
        self.updates_card.body.addWidget(self.updates_status_label)
        self.body.addWidget(self.updates_card)

        self.update_row = DeckRow(tr("Open Updates page"))
        self.update_row.activated.connect(lambda: self.window.set_page("update"))
        self.body.addWidget(self.update_row)

        self.mods_row = DeckRow(tr("Mods"))
        self.mods_row.activated.connect(lambda: self.window.set_page("mods"))
        self.body.addWidget(self.mods_row)

        self.storage_row = DeckRow(tr("Storage usage"), chevron=False)
        # Pure readout - nothing to activate, so keep it off the D-pad's path.
        self.storage_row.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.body.addWidget(self.storage_row)

        self.footer = deck_label("", role="caption", wrap=True)
        self.body.addWidget(self.footer)
        self.body.addStretch(1)

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        profile = self.profile()
        if profile is None:
            self.profile_row.set_value(tr("No Profile"))
            for row in (self.anomaly_row, self.gamma_row, self.deps_row):
                row.set_status(tr("No Profile"), "warn")
            for label in (self.anomaly_detail, self.gamma_detail, self.deps_detail):
                label.setText("")
            self.updates_installed_label.setText("")
            self.updates_status_label.setText("")
            self.update_row.set_value("")
            self.mods_row.set_value("")
            self.storage_row.set_value("")
            self.footer.setText(
                tr("Create or activate a profile first (Profiles page).")
            )
            return

        self.profile_row.set_value(profile.profile_name or tr("No Profile"))

        anomaly = anomaly_installed(profile.anomaly)
        gamma = gamma_installed(profile.gamma, profile.mo2_profile)
        self.anomaly_row.set_status(
            tr("Installed") if anomaly else tr("Not installed"),
            "ok" if anomaly else "bad",
        )
        self.gamma_row.set_status(
            tr("Installed") if gamma else tr("Not installed"),
            "ok" if gamma else "bad",
        )

        counts = count_active_mods(profile.gamma, profile.mo2_profile)
        self.mods_row.set_value(
            f"{counts[0]} / {counts[1]}" if counts else tr("Not installed")
        )

        self._render_footer(profile)
        self._start_dependency_check()
        self._start_update_check(profile)
        self._start_size_check(profile)

    def on_busy_changed(self, busy: bool) -> None:
        # An install rewrites everything this screen reports on.
        if not busy:
            self.refresh()

    # -- dependencies -----------------------------------------------------
    def _start_dependency_check(self) -> None:
        if self._deps_task is not None:
            return
        self.deps_row.set_status(tr("Checking..."), "warn")
        self._deps_task = BackgroundTask(check_all_dependencies, parent=self)
        self._deps_task.result.connect(self._on_dependencies)
        self._deps_task.error.connect(lambda _m: self._on_dependencies(None))
        self._deps_task.start()

    def _on_dependencies(self, missing: object) -> None:
        self._deps_task = None
        if missing is None:
            self.deps_row.set_status(tr("Unknown"), "warn")
            return
        names = list(missing)
        if names:
            detail = ", ".join(str(name) for name in names)
            self.deps_row.set_status(tr("{count} missing", count=len(names)), "bad")
            self.deps_row.setToolTip(detail)
            self.deps_detail.setText(detail)
        else:
            self.deps_row.set_status(tr("Ready"), "ok")
            self.deps_row.setToolTip("")
            self.deps_detail.setText("")

    # -- updates ----------------------------------------------------------
    def _start_update_check(self, profile) -> None:
        if self._update_task is not None:
            return
        self.updates_installed_label.setText(tr("Checking..."))
        self.updates_status_label.setText("")
        self._update_task = BackgroundTask(check_updates, profile, parent=self)
        self._update_task.result.connect(self._on_update_checked)
        self._update_task.error.connect(
            lambda _m: self._finish_update(tr("status unavailable"), None)
        )
        self._update_task.start()

    def _on_update_checked(self, status: object) -> None:
        if getattr(status, "error", None):
            self._finish_update(tr("status unavailable"), None)
            return
        installed = format_version(
            getattr(status, "installed", None),
            getattr(status, "installed_human", None),
        )
        if getattr(status, "update_available", False):
            latest = format_version(
                getattr(status, "latest", None),
                getattr(status, "latest_human", None),
            )
            self._finish_update(f"{installed}  →  {latest}", status)
        else:
            self._finish_update(tr("Up to date") + f"  ({installed})", status)

    def _finish_update(self, text: str, status: object) -> None:
        self._update_task = None
        self.updates_installed_label.setText(text)
        if status is None:
            self.updates_status_label.setText("")
        else:
            # Both possible states here are desktop's "accent" case (an
            # update is available, or the pack is up to date) - it hardcodes
            # the same fixed green as the Installed status dot rather than
            # the theme's accent color, so this does too (deckBodyOk).
            self.updates_status_label.setText(
                tr("Update available")
                if getattr(status, "update_available", False)
                else tr("Up to date")
            )
            self.updates_status_label.setObjectName("deckBodyOk")
            self.updates_status_label.style().unpolish(self.updates_status_label)
            self.updates_status_label.style().polish(self.updates_status_label)
        self.update_row.set_value(tr("Details"))

    # -- storage ----------------------------------------------------------
    def _start_size_check(self, profile) -> None:
        if self._size_task is not None:
            return
        if time.monotonic() - self._sizes_at < _SIZE_CACHE_S:
            return
        self.storage_row.set_value(tr("Checking..."))
        paths = [profile.anomaly, profile.gamma, profile.cache]

        def measure() -> tuple[int, int | None]:
            total = sum(dir_size(path) for path in paths if path)
            free = free_space_bytes(profile.gamma or profile.anomaly or ".")
            return total, free

        self._size_task = BackgroundTask(measure, parent=self)
        self._size_task.result.connect(self._on_sizes)
        self._size_task.error.connect(lambda _m: self._on_sizes(None))
        self._size_task.start()

    def _on_sizes(self, result: object) -> None:
        self._size_task = None
        self._sizes_at = time.monotonic()
        if not result:
            self.storage_row.set_value(tr("status unavailable"))
            self.storage_row.value_label.setObjectName("deckRowValue")
            self._repolish_storage_value()
            return
        total, free = result
        text = human_size(total)
        if free is not None:
            text += "   ·   " + tr("{free} free", free=human_size(free))
        self.storage_row.set_value(text)
        # Matches desktop's storage "Total" line, hardcoded to the same
        # fixed green as the Installed status dot rather than the theme's
        # accent color.
        self.storage_row.value_label.setObjectName("deckRowValueOk")
        self._repolish_storage_value()

    def _repolish_storage_value(self) -> None:
        label = self.storage_row.value_label
        label.style().unpolish(label)
        label.style().polish(label)

    # -- footer -----------------------------------------------------------
    def _render_footer(self, profile) -> None:
        state = gui_settings.load_gui_settings()
        name = profile.profile_name or ""
        playtime = (state.get("playtime_seconds") or {}).get(name, 0.0)
        last = (state.get("last_played_ts") or {}).get(name)
        self.footer.setText(
            tr("Total playtime")
            + ": "
            + format_playtime(playtime)
            + "   ·   "
            + tr("Last played")
            + ": "
            + format_last_played(last)
        )
