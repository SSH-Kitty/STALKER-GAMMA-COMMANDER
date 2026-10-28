"""The Multiplayer screen: xrRazom Co-op (Slim) from Deck Mode.

Everything the desktop Multiplayer page does - install from a zip, switch
between co-op and singleplayer, the session settings, the invite address,
launching - with Deck controls in place of its widgets: inline choice
chips instead of pill buttons, toggle rows instead of checkboxes, -/+
steppers instead of spin boxes and the on-screen keyboard for text.

All the work is in ``commander_gui.coop``; this screen only drives it.
Unlike the desktop page, every change is written at once: text arrives
whole from the keyboard's Done, so there is no half-typed name to hold
back.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QGraphicsBlurEffect,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from commander_gui import coop, gui_settings
from commander_gui.i18n import tr
from commander_gui.launcher import (
    Mo2Executable,
    anomaly_launcher_target,
    parse_mo2_executables,
    resolve_runner,
    steam_shortcut_appid,
    unblock_mo2_python,
)
from commander_gui.steam_shortcuts import (
    coop_launch_url,
    coop_shortcut_current,
    find_shortcuts_vdf,
    list_steam_accounts,
    open_in_steam,
    setup_coop_shortcut,
    steam_is_running,
    steam_root_for,
)
from commander_gui.ui import game_splash
from commander_gui.ui.common import (
    BackgroundTask,
    game_running,
    gamma_installed,
    mo2_running,
    notice,
    play_click_sound,
)
from commander_gui.ui.play_page import (
    coop_options_only_change,
    coop_shortcut_fields,
    plain_coop_exe,
    with_coop_options,
)

from .. import gamepad as pad
from ..folder_picker import show_folder_picker
from ..mod_installer import DeckArchivePicker
from ..scale import px
from ..welcome import open_url
from ..widgets import (
    BUTTON_H,
    MIN_TOUCH,
    ROW_H,
    DeckCard,
    DeckOverlay,
    DeckPicker,
    DeckRow,
    DeckSegmented,
    DeckToggleRow,
    deck_button,
    deck_chip,
    deck_label,
    deck_step_badge,
    picker_overlay,
    repolish,
    side_by_side,
)
from .base import DeckScreen
from .utilities import _tool_row

#: coop.status() -> (English label, chip state).
_STATUS = {
    coop.NOT_INSTALLED: ("Not installed", "warn"),
    coop.COOP: ("Co-op active", "ok"),
    coop.SINGLEPLAYER: ("Singleplayer", "ok"),
    coop.REVERTED: ("Engine reverted", "bad"),
}
_TABS = ("Hosting", "Joining", "Player", "Manage", "Help")


class _Bridge(QObject):
    progress = Signal(int, int)


# ------------------------------------------------------- shared with others
def confirm_singleplayer(window, profile, action: str, proceed) -> None:
    """Before a GAMMA install/update/reset: offer to put the single-player
    engine back, then call ``proceed`` - unless cancelled.

    Those jobs rewrite the Anomaly folder; with co-op's exes in place they
    either flag them as broken or overwrite them, quietly breaking co-op.
    The Deck twin of multiplayer_page.confirm_singleplayer.
    """
    if profile is None or not coop.is_active(profile.anomaly):
        proceed()
        return

    def _switch() -> None:
        window.dismiss_overlay()
        try:
            coop.switch_to_singleplayer(profile.anomaly, profile.gamma, profile.mo2_profile)
        except (coop.CoopError, OSError, ValueError) as exc:
            window.notify(tr("Couldn't switch to singleplayer") + ": " + str(exc), 8000)
            return
        proceed()

    window.show_overlay(
        DeckOverlay(
            tr("Switch to singleplayer first?"),
            deck_label(
                tr("Co-op is on. Switch back to singleplayer before {action}, so co-op isn't broken by the changes.", action=action)
                + "\n\n"
                + tr("You can turn co-op back on from the Multiplayer page afterwards."),
                role="body",
                wrap=True,
            ),
            [
                (tr("Switch to singleplayer"), _switch, "primary"),
                (tr("Cancel"), window.dismiss_overlay, "normal"),
            ],
            panel_width=1000,
        )
    )


def refuse_launch(window, reason: str) -> None:
    """Why a launch was blocked (coop.launch_block_reason), with the way to
    the Multiplayer screen, where the mode is changed."""

    def _open() -> None:
        window.dismiss_overlay(refocus=False)
        window.set_page("multiplayer")

    buttons = [(tr("Close"), window.dismiss_overlay, "normal")]
    if window.current_key() != "multiplayer":
        buttons.insert(0, (tr("Open Multiplayer"), _open, "primary"))
    window.show_overlay(
        DeckOverlay(tr("Can't start this way"), deck_label(tr(reason), role="body", wrap=True), buttons)
    )


def _prepare_coop_steam(profile, kind, prefix, mo2_profile, target, arguments) -> tuple:
    """The disk-bound half of a Steam co-op launch, on a worker thread:
    the shortcut's fields, and the same built without launch options (see
    coop_options_only_change)."""
    if coop.install_kind(profile.anomaly) == coop.KIND_GAMMA:
        unblock_mo2_python(profile.gamma)
    coop.prepare_launch(profile.anomaly, profile.gamma, profile.mo2_profile)
    runner = resolve_runner(kind, prefix)
    base_fields = coop_shortcut_fields(profile, runner, mo2_profile, target, arguments, options="")
    return with_coop_options(base_fields), base_fields


# ------------------------------------------------------------- small pieces
def _panel() -> tuple[QWidget, QVBoxLayout]:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(px(12))
    return widget, layout


def _caption(text: str = "", *, center: bool = False) -> object:
    label = deck_label(text, role="caption", wrap=True)
    if center:
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


def _warn(text: str = "") -> object:
    label = deck_label(text, role="body", wrap=True)
    label.setObjectName("deckBodyWarn")
    return label


def _step(number: int, title: str, text: str) -> QWidget:
    """A numbered step: accent badge, title and wrapped text beside it."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(px(14))
    layout.addWidget(deck_step_badge(number), 0, Qt.AlignmentFlag.AlignTop)
    body = QVBoxLayout()
    body.setSpacing(px(2))
    body.addWidget(deck_label(title, role="rowTitle"))
    body.addWidget(_caption(text))
    layout.addLayout(body, 1)
    return row


def _fact(title: str, text: str) -> QWidget:
    widget, layout = _panel()
    layout.setSpacing(px(2))
    layout.addWidget(deck_label(title, role="rowTitle"))
    layout.addWidget(_caption(text))
    return widget


def _choice_row(title: str, options) -> tuple[DeckRow, DeckSegmented]:
    """A row with inline choice chips (Settings' "When COMMANDER starts")."""
    row = DeckRow(title, chevron=False)
    row.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    row.value_label.hide()
    choice = DeckSegmented(options)
    choice.setFixedWidth(px(460))
    row.layout().addWidget(choice)
    row.setFixedHeight(px(ROW_H))
    return row, choice


class MultiplayerScreen(DeckScreen):
    def build(self) -> None:
        self._task: BackgroundTask | None = None
        self._steam_task: BackgroundTask | None = None
        self._busy = False
        self._status = coop.NOT_INSTALLED
        self._coop_before_op = False
        self._values: dict = {key: fallback for key, (_k, fallback) in coop.SETTINGS.items()}
        #: Profile the settings were read from; edits are written there.
        self._settings_from = None
        self._appid = coop.COP_APPID
        self._found: Path | None = None
        self._public_ip = ""
        self._fetching_ip = False
        self._ip_failed = False
        self._lan_ip = "127.0.0.1"
        self._internet = False
        self._ip_shown = False
        self._address = ""
        self._op_label_text = ""
        self._was_host: bool | None = None
        #: GAMMA or plain Anomaly for a first install (desktop's pills).
        self._kind_pick = coop.KIND_GAMMA
        self._toggles: dict[str, DeckToggleRow] = {}
        self._steppers: dict[str, tuple] = {}
        self._bridge = _Bridge(self)
        self._bridge.progress.connect(self._on_progress)
        self._say_clear = QTimer(self)
        self._say_clear.setSingleShot(True)
        self._say_clear.timeout.connect(lambda: self._say(""))

        self._build_banner()
        self._build_install()
        self._build_dash()
        self._build_settings()
        self.body.addStretch(1)

        play = self._play()
        play.controller.state_changed.connect(lambda _a: self._sync())
        play.controller.status.connect(self._mirror_status)

    def _play(self):
        return self.window._ensure_page("play")

    # ------------------------------------------------------------ banner
    def _build_banner(self) -> None:
        banner = DeckCard()
        banner.setObjectName("deckBanner")
        row = QHBoxLayout()
        row.setSpacing(px(12))
        row.addWidget(deck_label("xrRazom Co-Op", role="title"))
        row.addStretch(1)
        self.status_chip = deck_chip("", "warn")
        row.addWidget(self.status_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        mod_page = deck_button(
            tr("xrRazom mod page"), role="chip",
            on_click=lambda: open_url(self.window, coop.MODDB_MOD_PAGE),
        )
        row.addWidget(mod_page, 0, Qt.AlignmentFlag.AlignVCenter)
        banner.body.addLayout(row)
        self.body.addWidget(banner)

    # ------------------------------------------------------ not installed
    def _build_install(self) -> None:
        self.install_box, setup = _panel()
        self.intro_label = _caption()
        setup.addWidget(self.intro_label)
        self.kind_row, self.kind_choice = _choice_row(
            tr("Play co-op on"), [(tr("GAMMA"), coop.KIND_GAMMA), (tr("Anomaly"), coop.KIND_ANOMALY)]
        )
        self.kind_choice.changed.connect(self._pick_kind)
        setup.addWidget(self.kind_row)
        self.kind_hint = _caption()
        setup.addWidget(self.kind_hint)
        self.download_button = deck_button(
            tr("Download from moddb"), on_click=lambda: open_url(self.window, coop.MODDB_PAGE)
        )
        self.found_label = deck_label("", role="caption", wrap=True)
        self.found_label.setObjectName("deckCaptionAccent")
        tiles = []
        for number, title, text, extra in (
            (1, tr("Download the Slim zip"), tr("Opens moddb in your browser. Get the Slim edition: it's the one made for GAMMA."), self.download_button),
            (2, tr("Install it"), tr("Choose the downloaded zip. COMMANDER backs up your original files first."), self.found_label),
            (3, tr("Play together"), tr("Switch to co-op, pick Hosting or Joining, then launch."), None),
        ):
            tile = DeckCard()
            tile.setObjectName("deckTile")
            tile.body.addWidget(_step(number, title, text))
            tile.body.addStretch(1)
            if extra is not None:
                tile.body.addWidget(extra)
            tiles.append(tile)
        setup.addLayout(side_by_side(*tiles))
        # Never greyed out: installs the zip found in Downloads, or opens
        # the file browser.
        self.zip_button = deck_button("", role="hero", on_click=self._zip_clicked)
        setup.addWidget(self.zip_button)
        self.other_zip_button = deck_button(tr("Choose another zip"), on_click=self._pick_zip)
        setup.addWidget(self.other_zip_button)
        self.body.addWidget(self.install_box)

    # ---------------------------------------------------------- installed
    def _build_dash(self) -> None:
        self.dash_box, dash = _panel()
        # The hero first, as on the Play screen: launching is what this
        # screen is visited for most, so it sits above the fold.
        self.reverted_label = _warn(tr("A GAMMA update or repair put the original game files back. Re-apply co-op to play together again."))
        dash.addWidget(self.reverted_label)
        # Installed for GAMMA, but this profile has no GAMMA: the mod's
        # files would never load.
        self.mismatch_label = _warn(tr("This co-op install was made for GAMMA, but this profile has no GAMMA. Reinstall it for Anomaly to play."))
        dash.addWidget(self.mismatch_label)
        self.mismatch_button = deck_button(
            tr("Reinstall for Anomaly"),
            on_click=lambda: self._install_zip(self._found) if self._found else self._pick_zip(),
        )
        dash.addWidget(self.mismatch_button)

        # One big button that always does the next step: switch to co-op,
        # launch it, or quit it while it runs (the Play screen's hero).
        self.hero = deck_button("", role="hero", on_click=self._hero_clicked)
        dash.addWidget(self.hero)
        self.launch_status = _caption(center=True)
        self.launch_status.setObjectName("deckCaptionAccent")
        dash.addWidget(self.launch_status)
        self.sp_button = deck_button(
            tr("Switch to Singleplayer"), on_click=lambda: self._run_switch(False)
        )
        dash.addWidget(self.sp_button)
        self.role_row, self.role_choice = _choice_row(
            tr("Role"), [(tr("Hosting"), True), (tr("Joining"), False)]
        )
        self.role_choice.changed.connect(lambda v: self._set("xrr_host_session", bool(v)))
        dash.addWidget(self.role_row)
        self.role_hint = _caption()
        dash.addWidget(self.role_hint)

        self.session_row, self.session_choice = _choice_row(
            tr("Session type"), [(tr("Steam"), True), (tr("LAN"), False)]
        )
        self.session_choice.changed.connect(lambda v: self._set("xrr_host_steam", bool(v)))
        dash.addWidget(self.session_row)
        self.session_hint = _caption()
        dash.addWidget(self.session_hint)

        # LAN only: hidden for Steam sessions instead of sitting greyed out.
        self.port_row = DeckRow(tr("LAN port (UDP)"))
        self.port_row.activated.connect(self._edit_port)
        dash.addWidget(self.port_row)

        self.app_row, self.app_choice = _choice_row(
            tr("Run on Steam as"),
            [("Call of Pripyat", coop.COP_APPID), ("Spacewar", coop.SPACEWAR_APPID)],
        )
        self.app_choice.changed.connect(self._set_appid)
        dash.addWidget(self.app_row)
        self.app_note = _caption(
            tr("Changing this restarts Steam once, on your next co-op launch.") if os.name != "nt"
            else tr("Everyone in the session must pick the same one.")
        )
        dash.addWidget(self.app_note)

        self.dash_kind_row, self.dash_kind_choice = _choice_row(
            tr("Play co-op on"), [(tr("GAMMA"), coop.KIND_GAMMA), (tr("Anomaly"), coop.KIND_ANOMALY)]
        )
        self.dash_kind_choice.changed.connect(self._switch_kind)
        dash.addWidget(self.dash_kind_row)
        self.dash_kind_hint = _caption(tr("Move co-op between GAMMA and Anomaly. No zip needed, and co-op stays on or off as it is."))
        dash.addWidget(self.dash_kind_hint)

        # xrRazom version | Player slots, like the Play screen's stat grid.
        stats = DeckCard()
        stats.body.setContentsMargins(px(20), px(8), px(20), px(8))
        grid = QHBoxLayout()
        self.version_value = deck_label("?", role="statValue")
        self.slots_value = deck_label("", role="statValue")
        for value, caption in ((self.version_value, tr("xrRazom version")), (self.slots_value, tr("Player slots"))):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.addWidget(value)
            cell.addWidget(_caption(caption, center=True))
            grid.addLayout(cell, 1)
        stats.body.addLayout(grid)
        dash.addWidget(stats)
        self.zip_label = _caption()
        dash.addWidget(self.zip_label)

        self.body.addWidget(self.dash_box)

        # Shared by every operation on this screen.
        self.op_label = _caption(center=True)
        self.op_label.setObjectName("deckCaptionAccent")
        self.op_label.hide()
        self.body.addWidget(self.op_label)

    # ----------------------------------------------------------- settings
    def _build_settings(self) -> None:
        self.settings_box, box = _panel()
        self.tab_choice = DeckSegmented([(tr(name), index) for index, name in enumerate(_TABS)], 0)
        self.tab_choice.changed.connect(self._show_tab)
        box.addWidget(self.tab_choice)
        self._pages = [
            self._host_tab(), self._join_tab(), self._player_tab(), self._manage_tab(), self._help_tab(),
        ]
        for page in self._pages:
            box.addWidget(page)
        self._show_tab(0)
        self.body.addWidget(self.settings_box)

    def _show_tab(self, index: object) -> None:
        self.tab_choice.set_value(index)
        for i, page in enumerate(self._pages):
            page.setVisible(i == index)
        self.update_hints()

    def _toggle(self, key: str, title: str) -> DeckToggleRow:
        row = DeckToggleRow(title)
        row.toggled.connect(lambda on, k=key: self._set(k, bool(on)))
        self._toggles[key] = row
        return row

    def _stepper(self, key: str, title: str, low: int, high: int, step: int = 1, suffix: str = "") -> DeckRow:
        """A value with -/+ beside it (Settings' "Text size" row)."""
        row = DeckRow(title, chevron=False)
        # The buttons are the focus stops, not the row around them.
        row.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        minus = deck_button("−", role="step", on_click=lambda: self._nudge(key, -step))
        plus = deck_button("+", role="step", on_click=lambda: self._nudge(key, step))
        for button in (minus, plus):
            button.setFixedWidth(px(BUTTON_H))
            row.layout().addWidget(button)
        self._steppers[key] = (row, low, high, suffix, minus, plus)
        return row

    def _host_tab(self) -> QWidget:
        page, layout = _panel()
        layout.addWidget(deck_label(tr("Server"), role="section"))
        layout.addWidget(self._stepper("xrr_max_players", tr("Player slots"), 1, 4))
        layout.addWidget(deck_label(tr("Combat"), role="section"))
        layout.addWidget(self._toggle("xrr_friendly_fire", tr("Friendly fire")))
        layout.addWidget(self._toggle("xrr_rescue", tr("Wounded state and rescue")))
        layout.addWidget(self._toggle("xrr_rescue_medkit", tr("Rescue requires a medkit")))
        layout.addWidget(self._toggle("xrr_allow_peer_save_load", tr("Allow players to save/load")))
        layout.addWidget(deck_label(tr("Travel"), role="section"))
        layout.addWidget(self._toggle("xrr_sync_teleport", tr("Sync quest teleports")))
        layout.addWidget(self._stepper("xrr_teleport_radius", tr("Teleport follow radius"), 0, 200, 5, " m"))
        layout.addWidget(self._toggle("xrr_party_fast_travel", tr("Require whole party for fast travel")))
        layout.addWidget(self._stepper("xrr_gather_radius", tr("Party gather radius"), 5, 250, 5, " m"))
        heading = deck_label(tr("Experimental"), role="section")
        layout.addWidget(heading)
        layout.addWidget(self._toggle("xrr_skip_mod_check", tr("Skip mod check")))
        layout.addWidget(_caption(tr("Lets friends join with different mods. This can cause desyncs or crashes and isn't supported by xrRazom.")))
        layout.addWidget(_caption(tr("Only the host's rules count. They apply to everyone.")))

        layout.addWidget(deck_label(tr("Invite a friend"), role="section"))
        # Steam sessions join through Steam itself; Call of Pripyat can also
        # join by address from the main menu, so it shows both.
        self.steam_invite, steps = _panel()
        for number, (title, text) in enumerate((
            (tr("Start a game"), tr("Load a save or start a new game. That opens your server.")),
            (tr("Invite friends"), tr("Open the Steam overlay (Shift+Tab, or the Steam button on Steam Deck) and invite a friend from your friends list.")),
            (tr("Friends join"), tr("They accept the invite, or join you from their own friends list.")),
        ), 1):
            steps.addWidget(_step(number, title, text))
        steps.addWidget(_caption(tr("Steam invites need no IP or port forwarding.")))
        layout.addWidget(self.steam_invite)

        self.ip_box, ip = _panel()
        self.kind_row, self.kind_choice = _choice_row(
            tr("Address"), [(tr("Same network"), False), (tr("Internet"), True)]
        )
        self.kind_choice.changed.connect(lambda v: self._pick_address(bool(v)))
        ip.addWidget(self.kind_row)
        # Blurred until Show, so a screenshot or a stream never leaks it.
        # The buttons are the focus stops, not the row (as the steppers).
        self.address_row = DeckRow("", chevron=False)
        self.address_row.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._blur = QGraphicsBlurEffect(self.address_row.value_label)
        self._blur.setBlurRadius(24)
        self.address_row.value_label.setGraphicsEffect(self._blur)
        self.show_ip_button = deck_button("", role="chip", on_click=self._toggle_ip)
        copy = deck_button(tr("Copy"), role="chip", on_click=self._copy_invite)
        for button in (self.show_ip_button, copy):
            button.setFixedHeight(px(MIN_TOUCH))
            button.setMinimumWidth(px(104))
            self.address_row.layout().addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        ip.addWidget(self.address_row)
        self.address_hint = _caption()
        ip.addWidget(self.address_hint)
        layout.addWidget(self.ip_box)
        layout.addWidget(_caption(tr("Save before you quit. Everyone's progress is stored on your PC.")))
        return page

    def _join_tab(self) -> QWidget:
        page, layout = _panel()
        layout.addWidget(deck_label(tr("Join a friend"), role="section"))
        layout.addWidget(_step(1, tr("Launch co-op"), tr("Pick the same session type and Steam game as the host, then launch.")))
        self.join_steam_step = _step(2, tr("Join from Steam"), tr("Open the Steam overlay (Shift+Tab, or the Steam button on Steam Deck) and join the host from your friends list, or accept their invite."))
        self.join_lan_step = _step(2, tr("Join by address"), tr("Enter the host's address below. In the game's main menu, click Join Session, then Join."))
        layout.addWidget(self.join_steam_step)
        layout.addWidget(self.join_lan_step)
        layout.addWidget(_step(3, tr("Pick a loadout"), tr("First time joining? Choose a starting loadout and press Start.")))
        layout.addWidget(_caption(tr("Your character and progress are saved on the host under your player name.")))

        # Spacewar joins through Steam only, so it hides the address.
        self.join_address, address = _panel()
        self.join_row = DeckRow(tr("Host address"))
        self.join_row.activated.connect(self._edit_join_address)
        address.addWidget(self.join_row)
        self.join_address_hint = _caption()
        address.addWidget(self.join_address_hint)
        layout.addWidget(self.join_address)

        self.join_tips, tips = _panel()
        tips.addWidget(deck_label(tr("Good to know"), role="section"))
        for text in (
            tr("You need the same mods and xrRazom version as the host. If the join is rejected, search the game log for [xrRazom] cfg-diff to find the file that differs."),
            tr("Crashed or out of sync? Quit to the main menu and join again."),
        ):
            tips.addWidget(_caption("• " + text))
        layout.addWidget(self.join_tips)
        layout.addWidget(_warn(tr("Don't continue from an autosave made while joined. Only the host's saves hold the full game.")))
        return page

    def _player_tab(self) -> QWidget:
        page, layout = _panel()
        layout.addWidget(deck_label(tr("Player"), role="section"))
        self.name_row = DeckRow(tr("Player name"))
        self.name_row.activated.connect(self._edit_name)
        layout.addWidget(self.name_row)
        layout.addWidget(_warn(tr("The host knows you by this name. Change it and you start over as a new player.")))
        layout.addWidget(self._stepper("xrr_tag_duration", tr("Ping tag lifetime"), 5, 300, 5, " s"))
        layout.addWidget(_caption(tr("Bind the tag and chat keys in game: Settings → Control → Keybinds → xrRazom Co-op, at the bottom of the list.")))
        layout.addWidget(deck_label(tr("HUD and chat"), role="section"))
        for key, title in (
            ("xrr_show_marks", tr("Show player markers on PDA map")),
            ("xrr_show_names", tr("Show player nameplates")),
            ("xrr_show_players_hp", tr("Show other players' health bars")),
            ("xrr_show_net_health", tr("Show connection warnings")),
            ("xrr_chat_enable", tr("Text chat")),
            ("xrr_chat_sound", tr("Chat sound")),
        ):
            layout.addWidget(self._toggle(key, title))
        return page

    def _manage_tab(self) -> QWidget:
        page, layout = _panel()
        tile, self.update_button = _tool_row(
            tr("Update xrRazom"), tr("Install a newer Slim zip. Your settings are kept."),
            self._pick_zip, button_text=tr("Update"),
        )
        layout.addWidget(tile)
        # Plain Anomaly co-op starts the game exe the launcher last saved
        # (play_page.plain_game_exe), so the launcher is where it's changed.
        self.launcher_tile, self.launcher_button = _tool_row(
            tr("Renderer and game options"),
            tr("Co-op starts the game with the renderer saved in the Anomaly Launcher, such as DX11 or DX9. Change it there, press Save Settings, then close the launcher."),
            self._open_anomaly_launcher, button_text=tr("Open"),
        )
        layout.addWidget(self.launcher_tile)
        tile, _check = _tool_row(
            tr("New releases"), tr("Everyone in a session must use the same version."),
            lambda: open_url(self.window, coop.MODDB_PAGE), button_text=tr("Open"),
        )
        layout.addWidget(tile)
        tile, self.export_button = _tool_row(
            tr("Export co-op"), tr("Save your co-op settings, keybinds, MCM values and all your saves to a zip."),
            lambda: self._export(), button_text=tr("Export"),
        )
        layout.addWidget(tile)
        tile, self.import_button = _tool_row(
            tr("Import co-op"), tr("Bring back an exported zip. Files it replaces are backed up first."),
            self._import, button_text=tr("Import"),
        )
        layout.addWidget(tile)
        tile, self.uninstall_button = _tool_row(
            tr("Remove co-op"), tr("Puts your original files back and deletes the mod. Co-op saves are kept."),
            self._uninstall, role="danger", button_text=tr("Uninstall"),
        )
        layout.addWidget(tile)
        return page

    def _help_tab(self) -> QWidget:
        """The xrRazom guide, as on the desktop page."""
        page, layout = _panel()
        start = DeckCard()
        start.body.addWidget(deck_label(tr("Getting started"), role="section"))
        for number, (title, text) in enumerate((
            (tr("Pick your role and session"), tr("Choose Hosting or Joining above. The whole party picks the same session type and Steam game.")),
            (tr("Set your player name"), tr("On the Player tab. The host knows you by this name, so keep it the same.")),
            (tr("Launch Co-op"), tr("COMMANDER sets the game up for the choices above.")),
            (tr("Play together"), tr("The host loads a save or starts a new game, then friends join. The Hosting and Joining tabs walk you through it.")),
        ), 1):
            start.body.addWidget(_step(number, title, text))
        start.body.addWidget(_caption(tr("Everyone needs the same mods and xrRazom version as the host.")))

        works = DeckCard()
        works.body.addWidget(deck_label(tr("How it works"), role="section"))
        for title, text in (
            (tr("The host keeps the world"), tr("Everything is created and saved on the host's PC. Everyone else mirrors it.")),
            (tr("Join any time"), tr("Up to 5 players, the host included. Friends can join at any point in the game and leave whenever they like.")),
            (tr("Your name is your profile"), tr("Change it and the host sees a new player with a fresh character.")),
            (tr("The host sets the rules"), tr("Session rules on the Hosting tab apply to everyone.")),
            (tr("Call of Pripyat or Spacewar"), tr("Everyone picks the same one. Call of Pripyat must be owned. Spacewar is free.")),
            (tr("GAMMA or Anomaly"), tr("One Anomaly folder runs co-op for one of them at a time. To keep both ready, use a second Anomaly folder and profile for Anomaly co-op.")),
        ):
            works.body.addWidget(_fact(title, text))
        start.body.addStretch(1)
        works.body.addStretch(1)
        layout.addLayout(side_by_side(start, works))

        game = DeckCard()
        game.body.addWidget(deck_label(tr("In game"), role="section"))
        for key, text in (
            (tr("F5 / F9"), tr("Quicksave and quickload, if the host allows it.")),
            (tr("Send to"), tr("Right-click an item or money near a player to give it to them.")),
            (tr("Tag / Chat"), tr("Bind keys under Settings → Control → Keybinds → xrRazom Co-op.")),
            (tr("Rescue"), tr("Pick up a downed teammate, if the host turned it on.")),
            (tr("Anabiotic"), tr("Protects you from the current emission.")),
        ):
            game.body.addWidget(_fact(key, text))
        game.body.addWidget(_caption(tr("Companions follow the host. Only the host can give them orders.")))

        fixes = DeckCard()
        fixes.body.addWidget(deck_label(tr("Troubleshooting"), role="section"))
        for title, text in (
            (tr("Join rejected"), tr("Your mods differ from the host's. Search the game log for [xrRazom] cfg-diff to find the file.")),
            (tr("Crashed or out of sync"), tr("Quit to the main menu and join again. Your progress is kept on the host.")),
            (tr("Quest won't progress"), tr("Give the quest item to the host, or ask the host to talk to the NPC.")),
            (tr("Engine reverted"), tr("A GAMMA update or repair put the original files back. Click Re-apply co-op.")),
        ):
            fixes.body.addWidget(_fact(title, text))
        game.body.addStretch(1)
        fixes.body.addStretch(1)
        layout.addLayout(side_by_side(game, fixes))

        issues = DeckCard()
        issues.body.addWidget(deck_label(tr("Known issues"), role="section"))
        for text in (
            tr("Saving, loading and level changes can crash now and then. Save often."),
            tr("Don't continue from an autosave made while joined. Only the host's saves hold the full game."),
            tr("Weather can look slightly different for each player."),
            tr("Some quests and dialogs depend on your faction."),
            tr("The debug menu can desync the session. Some options, like Weather, stay in the save."),
        ):
            issues.body.addWidget(_caption("• " + text))
        issues.body.addWidget(_warn(tr("Not in co-op yet: vehicles, personal companions and Azazel mode.")))
        issues.body.addWidget(_warn(tr("GAMMA works with co-op, but not every mod has been tested together. Expect bugs.")))
        layout.addWidget(issues)

        tile, _discord = _tool_row(
            tr("Bugs and questions"), tr("The xrRazom Discord has support, bug reports and patches for other mods."),
            lambda: open_url(self.window, coop.DISCORD_URL), button_text=tr("Open"),
        )
        layout.addWidget(tile)
        return page

    # -------------------------------------------------------------- state
    def refresh(self) -> None:
        profile = self.profile()
        self._found = coop.find_downloaded_zip()
        self._status = self._coop_status(profile)
        state = coop.load_state(profile.anomaly) if profile else None
        self._appid = coop.steam_appid(profile.anomaly) if profile else coop.COP_APPID
        text, level = _STATUS[self._status]
        self.status_chip.setText(f"●  {tr(text)}")
        self.status_chip.setObjectName({"ok": "deckChipOk", "warn": "deckChipWarn", "bad": "deckChipBad"}[level])
        repolish(self.status_chip)
        self.version_value.setText(((state or {}).get("release") or {}).get("version") or "?")
        zip_name = coop.installed_zip(profile.anomaly, profile.gamma) if state else ""
        self.zip_label.setText(tr("Installed xrRazom version: {name}", name=zip_name))
        self.zip_label.setVisible(bool(zip_name))
        self._lan_ip = coop.lan_ip()
        self._ip_failed = False
        if profile is not None:
            self._values, _present = coop.read_settings(profile.anomaly)
            self._join_ip = coop.read_last_ip(profile.anomaly, profile.gamma, profile.mo2_profile)
            self._settings_from = profile
        self._render_values()
        self._sync()

    def _render_values(self) -> None:
        values = self._values
        for key, row in self._toggles.items():
            row.set_checked(bool(values[key]))
        for key, (row, _low, _high, suffix, _m, _p) in self._steppers.items():
            row.set_value(f"{int(values[key])}{suffix}")
        self.name_row.set_value(str(values["xrr_player_name"]))
        self.port_row.set_value(str(int(values["xrr_host_lan_port"])))
        self.join_row.set_value(getattr(self, "_join_ip", "") or tr("Not set"))
        self.slots_value.setText(str(int(values["xrr_max_players"])))
        # Dependent settings, as the desktop page links them.
        for parent, child in (
            ("xrr_rescue", "xrr_rescue_medkit"),
            ("xrr_sync_teleport", "xrr_teleport_radius"),
            ("xrr_party_fast_travel", "xrr_gather_radius"),
        ):
            on = bool(values[parent])
            if child in self._toggles:
                self._toggles[child].setEnabled(on)
            else:
                row, _low, _high, _suffix, minus, plus = self._steppers[child]
                for widget in (row, minus, plus):
                    widget.setEnabled(on)
        self._update_address()

    @staticmethod
    def _coop_status(profile) -> str:
        if profile is None:
            return coop.NOT_INSTALLED
        try:
            return coop.status(profile.anomaly)
        except OSError:
            return coop.NOT_INSTALLED

    def _has_gamma(self) -> bool:
        profile = self.profile()
        return profile is not None and gamma_installed(profile.gamma, profile.mo2_profile)

    def _install_kind(self) -> str:
        """How the next install (or update) of the zip goes for this profile."""
        profile = self.profile()
        if not self._has_gamma():
            return coop.KIND_ANOMALY
        if self._status != coop.NOT_INSTALLED:
            return coop.install_kind(profile.anomaly)  # an update keeps it
        return self._kind_pick

    def _pick_kind(self, kind: object) -> None:
        self._kind_pick = str(kind)
        self._sync()

    def _switch_kind(self, kind: object) -> None:
        profile = self._ready() if kind != self._install_kind() else None
        if profile is None:
            self._sync()  # puts the chips back
            return
        to_plain = kind == coop.KIND_ANOMALY
        self._start(
            tr("Switching co-op to Anomaly") if to_plain else tr("Switching co-op to GAMMA"),
            coop.set_install_kind, profile.anomaly, profile.gamma, profile.mo2_profile, str(kind),
            done=tr("Co-op is set up for Anomaly") if to_plain else tr("Co-op is set up for GAMMA"),
        )

    def _open_anomaly_launcher(self) -> None:
        profile = self.profile()
        launcher = anomaly_launcher_target(profile.anomaly) if profile is not None else None
        if launcher is None:
            self.window.notify(tr("Not installed"))
            return
        self._play().controller.launch(profile, target=None, direct=launcher)

    def _sync_kind(self, installed: bool, idle: bool) -> bool:
        """The install kind's rows; True when the install doesn't fit the profile."""
        has_gamma = self._has_gamma()
        kind = self._install_kind()
        plain = kind == coop.KIND_ANOMALY
        self.intro_label.setText(
            tr("Play Anomaly with up to 4 friends. You can switch back to singleplayer any time.") if plain
            else tr("Play GAMMA with up to 4 friends. You can switch back to singleplayer any time.")
        )
        self.kind_row.setVisible(has_gamma)
        self.kind_choice.set_value(kind)
        if not has_gamma:
            hint = tr("No GAMMA found, so co-op is set up for Anomaly. Everything goes into your Anomaly folder.")
        elif plain:
            hint = tr("Everything goes into your Anomaly folder. While co-op is on, GAMMA can't start: switch to singleplayer first to play GAMMA.")
        else:
            hint = tr("The co-op files are added to GAMMA as a Mod Organizer mod.")
        self.kind_hint.setText(hint)
        for widget in (self.dash_kind_row, self.dash_kind_hint):
            widget.setVisible(installed and has_gamma)
        self.launcher_tile.setVisible(installed and plain)
        self.launcher_button.setEnabled(idle and not self._play().controller.is_active())
        self.dash_kind_choice.set_value(kind)
        self.dash_kind_choice.setEnabled(idle)
        profile = self.profile()
        mismatch = installed and not has_gamma and coop.install_kind(profile.anomaly) == coop.KIND_GAMMA
        self.mismatch_label.setVisible(mismatch)
        self.mismatch_button.setVisible(mismatch)
        self.mismatch_button.setEnabled(idle)
        return mismatch

    def _sync(self) -> None:
        installed = self._status != coop.NOT_INSTALLED
        idle = not self._busy and not self.window.install_busy
        mismatch = self._sync_kind(installed, idle)
        self.install_box.setVisible(not installed)
        self.dash_box.setVisible(installed)
        self.settings_box.setVisible(installed)
        self.reverted_label.setVisible(self._status == coop.REVERTED)
        found = self._found is not None
        self.found_label.setVisible(found)
        self.other_zip_button.setVisible(found)
        if found:
            self.found_label.setText(tr("Found {name} in {folder}.", name=self._found.name, folder=self._found.parent.name))
        self.zip_button.setText(tr("Install {name}", name=self._found.name) if found else tr("Install from zip"))
        for button in (self.zip_button, self.other_zip_button, self.update_button, self.export_button, self.import_button, self.uninstall_button):
            button.setEnabled(idle)

        controller = self._play().controller
        running = controller.is_active()
        quit_coop = running and self._status == coop.COOP
        if quit_coop:
            self.hero.setText("■   " + tr("Quit Co-op"))
            self.hero.setObjectName("deckHeroDanger")
        else:
            text = (
                tr("Launch Co-op") if self._status == coop.COOP
                else tr("Re-apply co-op") if self._status == coop.REVERTED
                else tr("Switch to co-op")
            )
            self.hero.setText("▶   " + text)
            self.hero.setObjectName("deckHero")
        repolish(self.hero)
        self.hero.setEnabled(quit_coop or (idle and not running and self._steam_task is None and not mismatch))
        self.sp_button.setVisible(self._status in (coop.COOP, coop.REVERTED))
        self.sp_button.setEnabled(idle and not running)
        steam = bool(self._values["xrr_host_steam"])
        self.app_choice.setEnabled(idle and steam)
        if not running:
            self.launch_status.setText("")
        self.update_hints()

    def _mirror_status(self, text: str) -> None:
        if self._play().controller.is_active():
            self.launch_status.setText(notice(text))

    def _update_address(self) -> None:
        values = self._values
        host = bool(values["xrr_host_session"])
        steam = bool(values["xrr_host_steam"])
        port = int(values["xrr_host_lan_port"])
        spacewar = self._appid == coop.SPACEWAR_APPID
        self.role_choice.set_value(host)
        self.session_choice.set_value(steam)
        self.app_choice.set_value(coop.SPACEWAR_APPID if spacewar else coop.COP_APPID)
        # A joiner's server settings do nothing: picking a role opens its
        # tab, and Hosting is locked while joining.
        self.tab_choice.button(0).setEnabled(host)
        if host != self._was_host or (not host and self.tab_choice.value() == 0):
            self._was_host = host
            self._show_tab(0 if host else 1)
        self.port_row.setVisible(not steam)
        self.app_row.setVisible(steam)
        self.app_note.setVisible(steam)
        self.role_hint.setText(
            tr("Starts a server when you load a save or start a new game.") if host
            else tr("Pick the same session type as the host before launching.")
        )
        self.session_hint.setText(
            tr("Everyone joins by address. Steam can stay open.") if not steam
            else tr("Everyone keeps Steam open. Spacewar starts on its own, no game to buy.") if spacewar
            else tr("Everyone keeps Steam open and owns Call of Pripyat on Steam.")
        )
        steam_ip = steam and not spacewar
        self.steam_invite.setVisible(steam)
        self.join_steam_step.setVisible(steam)
        self.join_lan_step.setVisible(not steam)
        self.join_address.setVisible(not steam or steam_ip)
        self.join_row.title_label.setText(tr("Join by IP address") if steam else tr("Host address"))
        self.join_address_hint.setText(
            tr("In the game's main menu, click Join Session, then Join.") if steam
            else tr("Ask the host for it: it's on their Hosting tab.")
        )
        self.join_tips.setVisible(not (steam and spacewar))
        self.ip_box.setVisible(not steam or steam_ip)
        self.kind_row.setVisible(not steam)
        if steam_ip and not self._public_ip and not self._ip_failed:
            self._fetch_public_ip()
        internet = self._internet or steam
        self.kind_choice.set_value(internet)
        hint = (
            tr("Couldn't find your internet address") if steam and self._ip_failed
            else tr("Friends without the invite can type this address in the game's main menu, under Join Session. Forward UDP port {port} on your router first.", port=port) if steam
            else tr("Forward UDP port {port} on your router, then share this.", port=port) if internet
            else tr("Share this with friends on your network.")
        )
        ip = (self._public_ip or "…") if internet else self._lan_ip
        self._address = f"{ip}:{port}"
        self.address_row.title_label.setText(
            tr("Join by IP address") if steam else tr("Internet address") if internet else tr("LAN address")
        )
        self.address_row.set_value(self._address)
        self._blur.setEnabled(not self._ip_shown)
        self.show_ip_button.setText(tr("Hide") if self._ip_shown else tr("Show"))
        self.address_hint.setText(hint)

    # ------------------------------------------------------------ editing
    def _set(self, key: str, value) -> None:
        profile = self._settings_from
        if profile is None:
            return
        try:
            coop.write_settings(profile.anomaly, {key: value})
        except (OSError, ValueError) as exc:
            self.window.notify(tr("Couldn't save your settings") + ": " + str(exc), 8000)
            self._render_values()
            return
        self._values[key] = value
        self._render_values()
        self._sync()

    def _nudge(self, key: str, delta: int) -> None:
        _row, low, high, _suffix, _m, _p = self._steppers[key]
        value = max(low, min(high, int(self._values[key]) + delta))
        if value != int(self._values[key]):
            self._set(key, value)

    def _keyboard(self, title: str, text: str, on_done, *, max_length: int = 64, placeholder: str = "") -> None:
        edit = QLineEdit(self)
        edit.hide()
        edit.setMaxLength(max_length)
        edit.setPlaceholderText(placeholder)
        edit.setText(text)

        def _done(value: str) -> None:
            edit.deleteLater()
            on_done(value)

        self.window.open_keyboard(edit, title=title, on_done=_done)

    def _edit_name(self) -> None:
        def _done(text: str) -> None:
            name = " ".join(text.split())[:32]
            # An empty name would make the host treat you as a new player.
            if name and name != self._values["xrr_player_name"]:
                self._set("xrr_player_name", name)

        self._keyboard(tr("Player name"), str(self._values["xrr_player_name"]), _done,
                       max_length=32, placeholder=tr("Your nickname"))

    def _edit_port(self) -> None:
        def _done(text: str) -> None:
            try:
                port = int(text.strip())
            except ValueError:
                port = 0
            if not 1024 <= port <= 65535:
                self.window.notify(tr("LAN port (UDP)") + ": 1024–65535", 4000)
                return
            self._set("xrr_host_lan_port", port)

        self._keyboard(tr("LAN port (UDP)"), str(int(self._values["xrr_host_lan_port"])), _done, max_length=5)

    def _edit_join_address(self) -> None:
        profile = self._settings_from

        def _done(text: str) -> None:
            if profile is None:
                return
            try:
                coop.write_last_ip(profile.anomaly, profile.gamma, profile.mo2_profile, text)
            except (OSError, ValueError) as exc:
                self.window.notify(tr("Couldn't save your settings") + ": " + str(exc), 8000)
                return
            self._join_ip = coop.read_last_ip(profile.anomaly, profile.gamma, profile.mo2_profile)
            self._render_values()

        self._keyboard(tr("Host address"), getattr(self, "_join_ip", ""), _done,
                       placeholder=tr("Host address, e.g. 192.168.1.20:27015"))

    def _set_appid(self, appid: object) -> None:
        appid = int(appid)
        profile = self._ready() if appid != self._appid else None
        if profile is None:
            self._update_address()  # puts the chips back
            return
        try:
            coop.set_steam_appid(profile.anomaly, appid)
        except (coop.CoopError, OSError) as exc:
            self.window.notify(tr("Failed: {error}", error=tr(str(exc))), 8000)
        self.refresh()

    # ----------------------------------------------------------- address
    def _toggle_ip(self) -> None:
        self._ip_shown = not self._ip_shown
        self._update_address()

    def _pick_address(self, internet: bool) -> None:
        self._internet = internet
        if internet and not self._public_ip:
            self._fetch_public_ip()
        self._update_address()

    def _fetch_public_ip(self) -> None:
        if self._fetching_ip:
            return
        self._fetching_ip = True
        task = BackgroundTask(coop.public_ip, parent=self)
        task.result.connect(self._public_ip_found)
        task.error.connect(self._public_ip_failed)
        task.start()

    def _public_ip_found(self, ip: object) -> None:
        self._fetching_ip = False
        self._public_ip = str(ip)
        self._update_address()

    def _public_ip_failed(self, message: str) -> None:
        self._fetching_ip = False
        if not self._internet:
            # Steam as Call of Pripyat fetches on its own: say so quietly.
            self._ip_failed = True
            self._update_address()
            return
        self._internet = False
        self._update_address()
        self.window.notify(tr("Couldn't find your internet address") + ": " + message, 8000)

    def _copy_invite(self) -> None:
        if not self._address or self._address.startswith("…"):
            return
        QGuiApplication.clipboard().setText(self._address)
        self.window.notify(tr("Address copied."))

    # ------------------------------------------------------------ actions
    def _hero_clicked(self) -> None:
        if self._busy:
            return
        play = self._play()
        if play.controller.is_active():
            play._confirm_stop()
        elif self._status == coop.COOP:
            # The Play screen's launch: target, load-order check, then back
            # here through start_coop() for the co-op half.
            play_click_sound()
            play._play()
        else:
            self._run_switch(True)

    def _zip_clicked(self) -> None:
        play_click_sound()
        if self._found is not None:
            self._install_zip(self._found)
        else:
            self._pick_zip()

    def _ready(self):
        profile = self.profile()
        if profile is None:
            self.window.notify(tr("Create or pick a GAMMA profile on the Profiles page first."), 6000)
            return None
        if self.window.install_busy or self._busy:
            self.window.notify(tr("An install is already running."))
            return None
        if mo2_running(force=True) or game_running(force=True):
            self.window.notify(tr("Mod Organizer 2 or the game is still open. Close both, then try again."), 6000)
            return None
        return profile

    def _pick_zip(self) -> None:
        if self._ready() is None:
            return
        start = self._found.parent if self._found else Path.home() / "Downloads"

        def _chosen(path: Path) -> None:
            self.window.dismiss_overlay(refocus=False)
            self._install_zip(path)

        body = DeckArchivePicker(start, _chosen)
        overlay = DeckOverlay(
            tr("Select the xrRazom Slim zip"),
            body,
            [(tr("Cancel"), self.window.dismiss_overlay, "normal")],
            panel_width=1100,
        )
        overlay.default_button = body.picker.list
        overlay.back_handler = body.back
        overlay.hints = (("A", "Open"), ("B", "Back"))
        self.window.show_overlay(overlay)

    def _install_zip(self, path: Path) -> None:
        profile = self._ready()
        if profile is None:
            return
        try:
            info = coop.inspect_zip(path)
        except (coop.CoopError, OSError) as exc:
            self.window.show_overlay(
                DeckOverlay(
                    tr("This isn't an xrRazom zip"),
                    deck_label(tr(str(exc)), role="body", wrap=True),
                    [(tr("OK"), self.window.dismiss_overlay, "primary")],
                )
            )
            return

        def _go() -> None:
            self._start(
                tr("Installing {name}", name=path.name), self._install_job, path, profile,
                done=tr("xrRazom Co-op installed"), announce=True,
            )

        if "slim" in info.get("title", "").lower():
            _go()
            return
        self.window.confirm(
            tr("Install this edition?"),
            tr("{title} isn't the Slim edition from moddb. Other editions and test builds install the same way and work with co-op.", title=info.get("title") or path.name),
            _go,
            confirm_text=tr("Install now"),
        )

    def _install_job(self, path: Path, profile):
        kind = self._install_kind()
        return coop.install(path, profile.anomaly, profile.gamma, profile.mo2_profile, self._bridge.progress.emit, kind=kind)

    def _run_switch(self, to_coop: bool) -> None:
        profile = self._ready()
        if profile is None:
            return
        play_click_sound()
        fn = coop.switch_to_coop if to_coop else coop.switch_to_singleplayer
        label = tr("Switching to co-op") if to_coop else tr("Switching to singleplayer")
        done = tr("Switched to co-op") if to_coop else tr("Switched to singleplayer")
        self._start(label, fn, profile.anomaly, profile.gamma, profile.mo2_profile, done=done)

    def _uninstall(self) -> None:
        profile = self._ready()
        if profile is None:
            return

        def _uninstall() -> None:
            self._start(
                tr("Uninstalling"), coop.uninstall, profile.anomaly, profile.gamma, profile.mo2_profile,
                done=tr("xrRazom Co-op uninstalled"),
            )

        def _export_first() -> None:
            self.window.dismiss_overlay(refocus=False)
            self._export(then=_uninstall)  # uninstalls only once the zip is written

        def _now() -> None:
            self.window.dismiss_overlay()
            _uninstall()

        body = deck_label(
            tr("This removes xrRazom Co-op and puts your original game files back.")
            + "\n\n• " + tr("Your co-op saves are kept.")
            + "\n• " + tr("Export first to keep your co-op settings and saves in a zip.")
            + "\n• " + tr("You can install co-op again at any time."),
            role="body", wrap=True,
        )
        self.window.show_overlay(DeckOverlay(
            tr("Uninstall co-op?"),
            body,
            [
                (tr("Export first"), _export_first, "primary"),
                (tr("Uninstall"), _now, "danger"),
                (tr("Cancel"), self.window.dismiss_overlay, "normal"),
            ],
            default_index=2,
        ))

    def _export(self, then=None) -> None:
        if self._ready() is None:
            return
        name_edit = QLineEdit(self)  # typed into by the on-screen keyboard; never shown
        name_edit.setText(f"xrRazom co-op {time.strftime('%Y-%m-%d')}")

        def _named(folder: Path, name: str) -> None:
            profile = self._ready()
            if profile is None:
                return
            name = " ".join(name.split()).strip(". ") or name_edit.placeholderText()
            dest = folder / (name if name.lower().endswith(".zip") else name + ".zip")
            self.window.confirm(
                tr("Export co-op?"),
                tr("Saves your co-op settings and all your saves to:\n{path}", path=str(dest)),
                lambda: self._start(
                    tr("Exporting"), coop.export_session, profile.anomaly, profile.gamma, profile.mo2_profile, dest,
                    done=tr("Exported to {path}", path=str(dest)), on_done=then,
                ),
                confirm_text=tr("Export"),
            )

        name_edit.setPlaceholderText(name_edit.text())
        show_folder_picker(
            self.window, title=tr("Export co-op: choose a folder"), start=Path.home(),
            on_choose=lambda folder: self.window.open_keyboard(
                name_edit, title=tr("Name the zip"), on_done=lambda name: _named(folder, name),
            ),
        )

    def _import(self) -> None:
        if self._ready() is None:
            return

        def _chosen(path: Path) -> None:
            self.window.dismiss_overlay(refocus=False)
            profile = self._ready()
            if profile is None:
                return
            try:
                manifest = coop.read_export(path)
            except coop.CoopError as exc:
                self.window.notify(tr(str(exc)), 6000)
                return
            game = "Spacewar" if manifest.get("appid") == coop.SPACEWAR_APPID else "Call of Pripyat"
            self.window.confirm(
                tr("Import co-op?"),
                tr("This puts back the co-op setup from {name}.", name=path.name)
                + "\n\n• " + tr("{count} saves", count=int(manifest.get("saves") or 0))
                + "\n• " + tr("Co-op settings, keybinds and MCM values")
                + "\n• " + tr("Steam game: {game}", game=game)
                + "\n• " + tr("Files it replaces are backed up first."),
                lambda: self._start(
                    tr("Importing"), coop.import_session, profile.anomaly, profile.gamma, profile.mo2_profile,
                    profile.profile_name, path, done=tr("Co-op imported"),
                ),
                confirm_text=tr("Import"),
            )

        body = DeckArchivePicker(Path.home(), _chosen)
        overlay = DeckOverlay(
            tr("Import co-op"),
            body,
            [(tr("Cancel"), self.window.dismiss_overlay, "normal")],
            panel_width=1100,
        )
        overlay.default_button = body.picker.list
        overlay.back_handler = body.back
        overlay.hints = (("A", "Open"), ("B", "Back"))
        self.window.show_overlay(overlay)

    def _start(self, label: str, fn, *args, done: str | None = None, announce: bool = False, on_done=None) -> None:
        self._busy = True
        self.window.set_install_busy(True, "coop")
        self._say(label)
        self._sync()
        task = BackgroundTask(fn, *args, parent=self)
        self._task = task
        task.result.connect(lambda _r: self._finish(None, label, done, announce, on_done))
        task.error.connect(lambda message: self._finish(message, label, done, announce))
        task.start()

    def _finish(self, error: str | None, label: str, done: str | None, announce: bool, on_done=None) -> None:
        self._task = None
        self._busy = False
        self.window.set_install_busy(False)
        if error is not None:
            message = tr("Failed: {error}", error=tr(error))
            self._say(message)
            if announce:
                self.window.announce_finished(label, message, ok=False)
        else:
            self._say(done or tr("Done"))
            self._say_clear.start(5000)  # nothing to act on, so it goes
            if announce:
                self.window.announce_finished(done or label, done or tr("Done"))
        self.refresh()
        if error is None and on_done is not None:
            on_done()

    def _say(self, text: str) -> None:
        self._say_clear.stop()
        self._op_label_text = text = notice(text)
        self.op_label.setText(text)
        self.op_label.setVisible(bool(text))

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.op_label.setText(f"{self._op_label_text}   {done * 100 // total}%")

    # ------------------------------------------------------------- launch
    def start_coop(self, profile, target: str) -> None:
        """The co-op half of a launch the Play screen started: make sure the
        player has a name of their own, then start - through Steam for a
        Steam session (Steamworks under Proton needs Steam to start the
        game), directly otherwise."""

        def _go() -> None:
            steam = coop.read_settings(profile.anomaly)[0]["xrr_host_steam"]
            if os.name != "nt" and steam:
                self._launch_steam(profile, target)
            elif coop.install_kind(profile.anomaly) == coop.KIND_ANOMALY:
                # The selected game exe, as the desktop runs it; the launcher
                # becomes the game it would start (plain_coop_exe).
                controller = self._play().controller
                exe = next((e for e in controller.executables(profile) if e.title == target), Mo2Executable())
                controller.launch(profile, target=None, direct=plain_coop_exe(exe, profile.anomaly))
            else:
                self._play().controller.launch(profile, target=target)

        self._confirm_name(profile, _go)

    def _confirm_name(self, profile, then) -> None:
        """Everyone starts as "Player", and the host refuses a second player
        with a name already in the session."""
        default = coop.SETTINGS["xrr_player_name"][1]
        name = coop.read_settings(profile.anomaly)[0]["xrr_player_name"]
        if name.strip() and name.strip().lower() != default.lower():
            then()
            return
        prompt = tr("The host can't let in two players with the same name, and everyone starts as \"{default}\". Pick a name only you use.", default=default)
        body, layout = _panel()
        layout.addWidget(deck_label(prompt, role="body", wrap=True))
        edit = QLineEdit()
        edit.setMaxLength(32)
        edit.setPlaceholderText(tr("Your nickname"))
        layout.addWidget(edit)

        def _save() -> None:
            name = " ".join(edit.text().split())[:32]
            if not name or name.lower() == default.lower():
                self.window.notify(prompt, 5000)
                return
            try:
                coop.write_settings(profile.anomaly, {"xrr_player_name": name})
            except (OSError, ValueError) as exc:
                self.window.notify(tr("Couldn't save your settings") + ": " + str(exc), 8000)
                return
            self.window.dismiss_overlay()
            self.refresh()
            then()

        overlay = DeckOverlay(
            tr("Pick your player name"),
            body,
            [(tr("Save and launch"), _save, "primary"), (tr("Cancel"), self.window.dismiss_overlay, "normal")],
            panel_width=1000,
        )
        # A on the field opens the on-screen keyboard.
        overlay.default_button = edit
        self.window.show_overlay(overlay)

    def _launch_steam(self, profile, target: str) -> None:
        if self._steam_task is not None:
            return
        state = gui_settings.load_gui_settings()
        kind = state.get("runner") or "auto"
        prefix = (state.get("prefixes") or {}).get(kind) or state.get("wine_prefix") or ""
        exe = next((e for e in parse_mo2_executables(profile.gamma) if e.title == target), None)
        # Co-op only: the engine's splash off, COMMANDER's on (see game_splash).
        arguments = game_splash.splash_arguments(exe) or (exe.arguments if exe is not None else "")
        mo2_profile = profile.mo2_profile or "Default"
        self._steam_task = BackgroundTask(
            _prepare_coop_steam, profile, kind, prefix, mo2_profile, target, arguments, parent=self
        )
        self._steam_task.result.connect(lambda prepared: self._pick_account(profile, target, *prepared))
        self._steam_task.error.connect(self._steam_failed)
        self._steam_task.start()
        self._sync()

    def _pick_account(self, profile, target: str, fields: dict, base_fields: dict) -> None:
        self._steam_task = None
        self._sync()
        vdf_path = find_shortcuts_vdf()
        if vdf_path is not None:
            self._steam_with(vdf_path, profile, target, fields, base_fields)
            return
        accounts = list_steam_accounts()
        if not accounts:
            self.window.notify(tr("Could not find a Steam installation on this machine."), 6000)
            return
        picker = DeckPicker("", [(label, path) for label, path in accounts])

        def _chosen(path: object) -> None:
            self.window.dismiss_overlay(refocus=False)
            self._steam_with(Path(path), profile, target, fields, base_fields)

        picker.chosen.connect(_chosen)
        self.window.show_overlay(picker_overlay(self.window, tr("Pick your Steam account"), picker))

    def _steam_with(self, vdf_path: Path, profile, target: str, fields: dict, base_fields: dict) -> None:
        controller = self._play().controller
        if coop_shortcut_current(vdf_path, **fields):
            try:
                open_in_steam(steam_root_for(vdf_path), coop_launch_url(fields["exe"]))
            except OSError as exc:
                self._steam_failed(str(exc))
                return
            controller.track_steam_session(profile, target)
            return
        restart = steam_is_running()
        if restart and steam_shortcut_appid() is not None:
            # Started by Steam (Game Mode): restarting Steam would close
            # COMMANDER too, halfway through.
            self.window.show_overlay(
                DeckOverlay(
                    tr("Start COMMANDER outside Steam once"),
                    deck_label(
                        tr("Co-op needs a STALKER COMMANDER xrRAZOM shortcut in Steam, and adding it means restarting Steam. Steam started COMMANDER, so a restart would close COMMANDER too.")
                        + "\n\n1. " + tr("Close Steam.")
                        + "\n2. " + tr("Start COMMANDER on its own (on a Steam Deck, from Desktop Mode).")
                        + "\n3. " + tr("Launch co-op once.")
                        + "\n\n" + tr("After that, co-op works from Steam too, until you switch between Call of Pripyat and Spacewar."),
                        role="body",
                        wrap=True,
                    ),
                    [(tr("OK"), self.window.dismiss_overlay, "primary")],
                    panel_width=1100,
                )
            )
            return

        def _setup() -> None:
            self._steam_task = BackgroundTask(setup_coop_shortcut, vdf_path, restart=restart, parent=self, **fields)
            self._steam_task.result.connect(lambda _r: self._shortcut_ready(profile, target))
            self._steam_task.error.connect(self._steam_failed)
            self._steam_task.start()
            self.window.notify(
                tr("Restarting Steam to update the STALKER COMMANDER xrRAZOM shortcut") if restart
                else tr("Adding the STALKER COMMANDER xrRAZOM shortcut to Steam"),
                0,
            )
            self._sync()

        if not restart:
            _setup()
            return
        if coop_options_only_change(vdf_path, base_fields):
            self.window.confirm(
                tr("Restart Steam to use your launch options"),
                tr("Your launch options changed. Co-op starts from Steam, and Steam only picks up new launch options after a restart.")
                + "\n\n• " + tr("Steam closes and opens again.")
                + "\n• " + tr("The game then starts in co-op with your new launch options.")
                + "\n• " + tr("You see this after each change to the launch options.")
                + "\n\n" + tr("Anything running through Steam right now will close."),
                _setup,
                confirm_text=tr("Restart Steam"),
            )
            return
        self.window.confirm(
            tr("Restart Steam to start co-op"),
            tr("Co-op starts from a STALKER COMMANDER xrRAZOM shortcut in your Steam library. COMMANDER needs to add or update it, and Steam only picks up the change after a restart.")
            + "\n\n• " + tr("Steam closes and opens again.")
            + "\n• " + tr("The game then starts in co-op.")
            + "\n• " + tr("You only see this the first time, and after switching between Call of Pripyat and Spacewar.")
            + "\n\n" + tr("Anything running through Steam right now will close."),
            _setup,
            confirm_text=tr("Restart Steam"),
        )

    def _shortcut_ready(self, profile, target: str) -> None:
        self._steam_task = None
        self.window.toast.hide()
        self._play().controller.track_steam_session(profile, target)
        self._sync()

    def _steam_failed(self, message: str) -> None:
        self._steam_task = None
        self.window.toast.hide()
        self._sync()
        self.window.show_overlay(
            DeckOverlay(
                tr("Co-op couldn't start"),
                deck_label(tr(message), role="body", wrap=True),
                [(tr("OK"), self.window.dismiss_overlay, "primary")],
            )
        )

    # ------------------------------------------------------- busy / pad
    def on_busy_changed(self, busy: bool) -> None:
        if not self._busy:  # someone else's operation, not ours
            profile = self.profile()
            if busy:
                self._coop_before_op = self._coop_status(profile) == coop.COOP
            elif self._coop_before_op:
                self._coop_before_op = False
                if self._coop_status(profile) == coop.REVERTED:
                    QTimer.singleShot(0, self._offer_reapply)
        self._sync()

    def _offer_reapply(self) -> None:
        """An update or repair just put the original engine back over co-op."""
        self.refresh()
        profile = self.profile()
        try:
            changed = profile is not None and coop.engine_changed(profile.anomaly)
        except OSError:
            changed = False
        message = tr("The update put the original game files back, so co-op is off for now.")
        if changed:
            message += "\n\n" + tr("This update changed the game engine. Your xrRazom version may not work with it, so check the xrRazom Discord for a newer release first.")
        def _reapply() -> None:
            self.window.dismiss_overlay()
            self._run_switch(True)

        self.window.show_overlay(
            DeckOverlay(
                tr("Re-apply co-op?"),
                deck_label(message, role="body", wrap=True),
                [(tr("Re-apply co-op"), _reapply, "primary"), (tr("Not now"), self.window.dismiss_overlay, "normal")],
            )
        )

    def on_action(self, action: str) -> bool:
        if action == pad.CONTEXT:
            if self._status == coop.NOT_INSTALLED:
                self._zip_clicked()
            elif self.hero.isEnabled():
                self._hero_clicked()
            return True
        return False

    def hints(self):
        hints = [("A", "Select")]
        if self._status == coop.NOT_INSTALLED:
            hints.append(("X", "Install"))
        elif self.hero.isEnabled():
            running = self._play().controller.is_active()
            hints.append((
                "X",
                "Quit Co-op" if running else "Launch Co-op" if self._status == coop.COOP
                else "Re-apply co-op" if self._status == coop.REVERTED else "Switch to co-op",
            ))
        hints.append(("L1 R1 / L2 R2", "Switch tab"))
        return hints

    def default_focus(self):
        return self.zip_button if self._status == coop.NOT_INSTALLED else self.hero
