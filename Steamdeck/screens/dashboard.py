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
from PySide6.QtWidgets import QHBoxLayout

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
from commander_gui.ui.install_page import _resume_state_matches
from commander_gui.updates import check_updates, format_version

from .. import gamepad as pad
from ..profiles import ProfilePanel
from ..widgets import (
    DeckCard,
    DeckRow,
    DeckStatusRow,
    DeckStatusTile,
    deck_button,
    deck_divider_v,
    deck_label,
    repolish,
    side_by_side,
)
from .base import DeckScreen

#: Directory sizes mean walking the whole install tree, which on a Deck's SD
#: card is slow enough to notice. The desktop Dashboard caches for the same
#: reason and by the same margin.
_SIZE_CACHE_S = 30.0

#: Update and dependency checks are network/package-manager calls. Every
#: tab switch back to the Dashboard used to repeat both; now a result stays
#: good for this long (an install finishing clears it early).
_CHECK_CACHE_S = 300.0


class DashboardScreen(DeckScreen):
    def build(self) -> None:
        self._update_task: BackgroundTask | None = None
        self._deps_task: BackgroundTask | None = None
        self._size_task: BackgroundTask | None = None
        self._sizes_at = 0.0
        self._update_at = 0.0
        self._deps_at = 0.0
        self._update_status = None
        #: Bumped when cached results stop applying (profile switch); a
        #: check started under an older generation drops its result.
        self._generation = 0

        # The one-line answer to "can I play?", with the single action that
        # moves things forward (X does the same) - the rest is the detail.
        self.banner = DeckCard()
        self.banner.setObjectName("deckBanner")
        banner_row = self.banner.body
        self.banner_title = deck_label("", role="title")
        self.banner_detail = deck_label("", role="caption", wrap=True)
        self.banner_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        banner_row.addWidget(self.banner_title)
        banner_row.addWidget(self.banner_detail)
        self.banner_button = deck_button("", role="primary", on_click=self._banner_action)
        banner_row.addWidget(self.banner_button)
        self._banner_target = "play"
        self._play_hooked = False
        self.body.addWidget(self.banner)

        # Playtime right under the banner - what you glance at on the way to
        # pressing Play, not something to scroll down for.
        self.footer = deck_label("", role="caption", wrap=True)
        self.footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.footer)

        # Anomaly | GAMMA | Dependencies: three plain rows side by side, the
        # same height as Updates / Mods / Storage below (they used to sit in
        # a taller card of their own). Their captions - only ever used for
        # the list of missing dependencies - go on a line under each one.
        self.anomaly_row = DeckStatusTile(tr("STALKER Anomaly"), tr("Anomaly"))
        self.gamma_row = DeckStatusTile(tr("GAMMA Modpack"), tr("GAMMA"))
        self.deps_row = DeckStatusTile(tr("Dependencies"))
        self.body.addLayout(side_by_side(self.anomaly_row, self.gamma_row, self.deps_row))
        self.anomaly_detail = deck_label("", role="caption", wrap=True)
        self.gamma_detail = deck_label("", role="caption", wrap=True)
        self.deps_detail = deck_label("", role="caption", wrap=True)
        self.body.addLayout(side_by_side(self.anomaly_detail, self.gamma_detail, self.deps_detail))
        self.anomaly_detail.hide()
        self.gamma_detail.hide()
        self.deps_detail.hide()

        # One card for updates: "Updates | Up to date ... Details >". The
        # status and the way to act on it used to be two cards side by side;
        # the whole row now opens the Update screen.
        self.update_row = DeckRow(tr("Updates"), tr("Details"))
        row_layout = self.update_row.layout()
        row_layout.insertWidget(1, deck_divider_v(), 0, Qt.AlignmentFlag.AlignVCenter)
        self.updates_status_label = deck_label("", role="body")
        row_layout.insertWidget(2, self.updates_status_label)
        self.update_row.activated.connect(lambda: self.window.set_page("update"))
        self.body.addWidget(self.update_row)

        # Mods | Storage usage
        self.mods_row = DeckRow(tr("Mods"))
        self.mods_row.activated.connect(lambda: self.window.set_page("mods"))
        self.storage_row = DeckStatusRow(tr("Storage"))
        self.storage_row.layout().removeWidget(self.storage_row._chip)
        self.storage_row._chip.hide()
        self.body.addLayout(self._pair(self.mods_row, self.storage_row))

        # Profiles live here now (there is no Profile tab): switch, edit the
        # active one's folders, or start a new one.
        self.profile_panel = ProfilePanel(self.window)
        self.body.addWidget(self.profile_panel)
        self.body.addStretch(1)

    @staticmethod
    def _pair(left, right) -> QHBoxLayout:
        row = side_by_side(left, right)
        row.setAlignment(left, Qt.AlignmentFlag.AlignVCenter)
        row.setAlignment(right, Qt.AlignmentFlag.AlignVCenter)
        return row

    # -- shortcuts --------------------------------------------------------
    def on_action(self, action: str) -> bool:
        if action == pad.CONTEXT:
            self._banner_action()
            return True
        if action == pad.SEARCH:
            self._update_at = self._deps_at = self._sizes_at = 0.0
            self.window.notify(tr("Checking..."), 1500)
            self.refresh()
            return True
        return False

    def hints(self):
        return [
            ("A", "Select"),
            ("X", self.banner_button.text().replace("\u25b6", "").replace("\u25a0", "").strip() or "Go"),
            ("Y", "Re-check"),
            ("L1 R1 / L2 R2", "Switch tab"),
        ]

    def default_focus(self):
        # The banner's button is the one next step, whatever it says.
        return self.banner_button

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        profile = self.profile()
        self._render_banner(profile)
        self.profile_panel.refresh()
        if profile is None:
            for row in (self.anomaly_row, self.gamma_row, self.deps_row):
                row.set_status(tr("No Profile"), "warn")
            for label in (self.anomaly_detail, self.gamma_detail, self.deps_detail):
                label.setText("")
            self.updates_status_label.setText("")
            self.update_row.set_value("")
            self.mods_row.set_value("")
            self.storage_row.set_value("")
            self.footer.setText(
                tr("Create or activate a profile first (Dashboard → Profiles).")
            )
            return


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
        self._sync_captions()
        self._start_dependency_check()
        self._start_update_check(profile)
        self._start_size_check(profile)

    def _sync_captions(self) -> None:
        # An empty caption still takes a line of layout, which left a blank
        # band under each status row inside its card.
        for label in (self.anomaly_detail, self.gamma_detail, self.deps_detail):
            label.setVisible(bool(label.text()))

    def forget_cached_checks(self) -> None:
        """Drop cached update/dependency/size results (e.g. new profile).

        The generation bump makes any check still in flight for the old
        profile discard its result instead of showing it for the new one.
        """
        self._update_at = self._deps_at = self._sizes_at = 0.0
        self._update_status = None
        self._generation += 1
        self._update_task = self._size_task = None

    def on_busy_changed(self, busy: bool) -> None:
        # The profile chooser is off limits while an install runs; say so
        # at once rather than on the next visit.
        self.profile_panel.refresh()
        # An install rewrites everything this screen reports on.
        if not busy:
            self._update_at = self._deps_at = self._sizes_at = 0.0
            self.refresh()

    # -- banner -----------------------------------------------------------
    def _render_banner(self, profile) -> None:
        from ..window import SETUP_KEY

        if profile is None:
            title, detail, action, target = (
                tr("Welcome"),
                tr("Set up where GAMMA goes, then install it."),
                tr("Set up COMMANDER"),
                SETUP_KEY,
            )
        elif self._game_running():
            title, detail, action, target = (
                tr("Running"),
                tr("GAMMA is running."),
                "\u25a0   " + tr("Quit Game"),
                "quit",
            )
        else:
            anomaly = anomaly_installed(profile.anomaly)
            gamma = gamma_installed(profile.gamma, profile.mo2_profile)
            resume = _resume_state_matches(
                gui_settings.load_gui_settings().get("gamma_install_resume"), profile
            )
            if resume:
                title, detail, action, target = (
                    tr("Install incomplete"),
                    tr("The last install stopped part-way. Resume it to finish."),
                    tr("Resume install"),
                    "install",
                )
            elif not (anomaly and gamma):
                title, detail, action, target = (
                    tr("Install needed"),
                    tr("STALKER Anomaly and GAMMA aren't installed yet."),
                    tr("Go to Install"),
                    "install",
                )
            elif getattr(self._update_status, "update_available", False):
                title, detail, action, target = (
                    tr("Update available"),
                    tr("A new GAMMA version is ready to download."),
                    tr("Review update"),
                    "update",
                )
            else:
                title, detail, action, target = (
                    tr("Ready to play"),
                    tr("Everything is installed."),
                    "\u25b6   " + tr("Play GAMMA"),
                    "play",
                )
        self.banner_title.setText(title)
        self.banner_detail.setText(detail)
        self.banner_button.setText(action)
        self.banner_button.setObjectName("deckDanger" if target == "quit" else "deckPrimary")
        repolish(self.banner_button)
        self._banner_target = target
        if self.window.current_page() is self:
            self.window.update_hints()

    def _banner_action(self) -> None:
        # Play and Quit Game act from here, through the Play screen's own
        # launch controller - the Dashboard stays on screen.
        if self._banner_target == "play":
            page = self._play_page()
            if page is not None:
                page.play()
            return
        if self._banner_target == "quit":
            page = self._play_page()
            if page is not None:
                page._confirm_stop()
            return
        self.window.set_page(self._banner_target)

    def _play_page(self):
        page = self.window._ensure_page("play")
        controller = getattr(page, "controller", None)
        if controller is None:
            return None
        if not self._play_hooked:
            self._play_hooked = True
            controller.state_changed.connect(
                lambda _active: self._render_banner(self.profile())
            )
        # A page built just now has not read the profile yet: without this
        # its Play button is still disabled and play() does nothing.
        if not getattr(page, "_targets", None):
            page.refresh()
        return page

    def _game_running(self) -> bool:
        page = self.window._pages.get("play")
        controller = getattr(page, "controller", None)
        return controller is not None and controller.is_active()

    # -- dependencies -----------------------------------------------------
    def _start_dependency_check(self) -> None:
        if self._deps_task is not None:
            return
        if time.monotonic() - self._deps_at < _CHECK_CACHE_S and self._deps_at:
            return
        self.deps_row.set_status(tr("Checking..."), "warn")
        self._deps_task = BackgroundTask(check_all_dependencies, parent=self)
        self._deps_task.result.connect(self._on_dependencies)
        self._deps_task.error.connect(lambda _m: self._on_dependencies(None))
        self._deps_task.start()

    def _on_dependencies(self, missing: object) -> None:
        self._deps_task = None
        self._deps_at = time.monotonic()
        if missing is None:
            self.deps_row.set_status(tr("Unknown"), "warn")
            return
        names = list(missing)
        if names:
            detail = ", ".join(str(name) for name in names)
            self.deps_row.set_status(tr("{count} missing", count=len(names)), "bad")
            self.deps_row.set_detail(
                tr("Missing:")
                + "\n"
                + "\n".join(f"• {name}" for name in names)
                + "\n\n"
                + tr("Install them from the Install screen (step 3).")
            )
            self.deps_detail.setText(detail)
        else:
            self.deps_row.set_status(tr("Ready"), "ok")
            self.deps_row.set_detail("")
            self.deps_detail.setText("")
        self._sync_captions()

    # -- updates ----------------------------------------------------------
    def _start_update_check(self, profile) -> None:
        if self._update_task is not None:
            return
        if self._update_at and time.monotonic() - self._update_at < _CHECK_CACHE_S:
            return
        self._set_update_text(tr("Checking..."), "deckBody")
        self._update_task = BackgroundTask(check_updates, profile, parent=self)
        gen = self._generation
        self._update_task.result.connect(
            lambda status, g=gen: self._on_update_checked(status) if g == self._generation else None
        )
        self._update_task.error.connect(
            lambda _m, g=gen: self._finish_update(tr("status unavailable"), None)
            if g == self._generation
            else None
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
        self._update_at = time.monotonic()
        self._update_status = status
        self._render_banner(self.profile())
        if status is None:
            self._set_update_text(text, "deckBody")
        else:
            # Both states are desktop's "accent" case, in the same fixed
            # green as the Installed status dot (deckBodyOk).
            self._set_update_text(
                tr("Update available")
                if getattr(status, "update_available", False)
                else tr("Up to date"),
                "deckBodyOk",
            )
        self.update_row.set_value(tr("Details"))

    def _set_update_text(self, text: str, object_name: str) -> None:
        self.updates_status_label.setText(text)
        self.updates_status_label.setObjectName(object_name)
        repolish(self.updates_status_label)

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
        gen = self._generation
        self._size_task.result.connect(
            lambda result, g=gen: self._on_sizes(result) if g == self._generation else None
        )
        self._size_task.error.connect(
            lambda _m, g=gen: self._on_sizes(None) if g == self._generation else None
        )
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
