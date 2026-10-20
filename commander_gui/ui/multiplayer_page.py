"""Multiplayer page: install, switch and configure xrRazom Co-op (Slim)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import (
    QAbstractAnimation,
    QObject,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import coop
from .common import (
    BackgroundTask,
    PlainSpinBox,
    game_running,
    gamma_installed,
    info_label,
    install_hover_grow_text,
    make_card,
    mo2_running,
    notice,
    open_url,
    play_click_sound,
    section_label,
    set_hover_grow_text,
    steam_running,
    tr,
)
from .dashboard import _TITLE_ROW_H, _card_title, _field
from .dialogs import InputDialog as QInputDialog
from .dialogs import MessageBox as QMessageBox

_STATUS = {
    coop.NOT_INSTALLED: ("Not installed", ""),
    coop.COOP: ("Co-op active", "healthy"),
    coop.SINGLEPLAYER: ("Singleplayer", ""),
    coop.REVERTED: ("Engine reverted", "attention"),
}
_TABS = ("Hosting", "Joining", "Player", "Manage", "Help")


class _Bridge(QObject):
    progress = Signal(int, int)


def confirm_singleplayer(parent: QWidget, profile, action: str) -> bool:
    """Before a GAMMA update/repair: offer to put the single-player engine back.

    Returns False when the user cancels. Those jobs rewrite the Anomaly
    folder; with co-op's exes in place they either flag them as broken or
    overwrite them, quietly breaking co-op.
    """
    if profile is None or not coop.is_active(profile.anomaly):
        return True
    answer = QMessageBox.question(
        parent,
        tr("Switch to singleplayer first?"),
        tr("Co-op is on. Switch back to singleplayer before {action}, so co-op isn't broken by the changes.", action=action),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
        note=tr("You can turn co-op back on from the Multiplayer page afterwards."),
        labels={
            QMessageBox.StandardButton.Yes: tr("Switch to singleplayer"),
            QMessageBox.StandardButton.No: tr("Keep co-op"),
        },
    )
    if answer == QMessageBox.StandardButton.Cancel:
        return False
    if answer == QMessageBox.StandardButton.Yes:
        try:
            coop.switch_to_singleplayer(profile.anomaly, profile.gamma, profile.mo2_profile)
        except (coop.CoopError, OSError, ValueError) as exc:
            QMessageBox.warning(parent, tr("Couldn't switch to singleplayer"), str(exc))
            return False
    return True


def confirm_player_name(parent: QWidget, anomaly) -> bool:
    """Before a co-op launch: make the player pick a name if it's still the
    default. The host refuses a second player with a name already in the
    session, and everyone starts out as "Player". False when cancelled."""
    default = coop.SETTINGS["xrr_player_name"][1]
    name = coop.read_settings(anomaly)[0]["xrr_player_name"]
    while not name.strip() or name.strip().lower() == default.lower():
        name, ok = QInputDialog.getText(
            parent, tr("Pick your player name"),
            tr("The host can't let in two players with the same name, and everyone starts as \"{default}\". Pick a name only you use.", default=default),
            text="", confirm=tr("Save and launch"),
        )
        if not ok:
            return False
        name = " ".join(name.split())[:32]
        if name and name.lower() != default.lower():
            try:
                coop.write_settings(anomaly, {"xrr_player_name": name})
            except (OSError, ValueError) as exc:
                QMessageBox.warning(parent, tr("Couldn't save your settings"), str(exc))
                return False
    return True


def refuse_launch(window, parent: QWidget, reason: str) -> None:
    """Explain why a launch was blocked (see coop.launch_block_reason) and
    offer the Multiplayer page, where the mode is changed."""
    box = QMessageBox(QMessageBox.Icon.Warning, tr("Can't start this way"), tr(reason), parent=parent)
    go = box.addButton(tr("Open Multiplayer"), QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.exec()
    if box.clickedButton() is go:
        from .main_window import _TAB_INDEX  # deferred: avoids an import cycle

        window.tabs.setCurrentIndex(_TAB_INDEX["multiplayer"])


def _fields(*cells) -> QGridLayout:
    """Dashboard-style grid: two equal columns, each caption above its control."""
    grid = QGridLayout()
    grid.setHorizontalSpacing(16)
    grid.setVerticalSpacing(10)
    grid.setColumnStretch(0, 1)
    grid.setColumnStretch(1, 1)
    for index, (label, control) in enumerate(cells):
        grid.addLayout(_field(label, control), index // 2, index % 2)
    return grid


def _pills(*buttons: QPushButton) -> QWidget:
    """A row of exclusive pill buttons, left-aligned."""
    row = QWidget()
    row.setObjectName("panelTransparent")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    group = QButtonGroup(row)
    for button in buttons:
        group.addButton(button)
        layout.addWidget(button)
    layout.addStretch(1)
    return row


def _tab(text: str, name: str = "segment") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(name)
    button.setCheckable(True)
    button.setFixedHeight(_TITLE_ROW_H)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _divider() -> QFrame:
    line = QFrame()
    line.setObjectName("rowDivider")
    line.setFixedHeight(1)
    return line


def _vline() -> QFrame:
    line = QFrame()
    line.setObjectName("vDivider")
    line.setFixedWidth(1)
    return line


def _panel() -> tuple[QWidget, QVBoxLayout]:
    """A borderless page inside the settings card."""
    widget = QWidget()
    widget.setObjectName("panelTransparent")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    return widget, layout


def _action_row(title: str, text: str, button: QPushButton) -> QWidget:
    """Title and description with the action on the right (Utilities style)."""
    row = QWidget()
    row.setObjectName("panelTransparent")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 2, 0, 2)
    layout.setSpacing(12)
    body = QVBoxLayout()
    body.setSpacing(2)
    body.addWidget(section_label(title, level=2))
    body.addWidget(info_label(text))
    layout.addLayout(body, 1)
    button.setMinimumSize(170, 34)
    layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


def _step(number: int, title: str, text: str) -> QWidget:
    """A numbered step: big accent number, title and text beside it."""
    row = QWidget()
    row.setObjectName("panelTransparent")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(14)
    badge = QLabel(str(number))
    badge.setObjectName("statValue")
    badge.setFixedWidth(28)
    badge.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
    layout.addWidget(badge)
    body = QVBoxLayout()
    body.setSpacing(2)
    body.addWidget(section_label(title, level=3))
    body.addWidget(info_label(text))
    layout.addLayout(body, 1)
    return row


def _facts(layout: QVBoxLayout, facts) -> None:
    """Short heading-and-line pairs split by dividers, then a stretch."""
    for index, (title, text) in enumerate(facts):
        if index:
            layout.addWidget(_divider())
        pair = QVBoxLayout()
        pair.setSpacing(2)
        pair.addWidget(section_label(title, level=3))
        pair.addWidget(_hint(text))
        layout.addLayout(pair)
    layout.addStretch(1)


def _key_row(key: str, text: str) -> QWidget:
    """A key or action in a chip, what it does beside it."""
    row = QWidget()
    row.setObjectName("panelTransparent")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    chip = QLabel(key)
    chip.setObjectName("chip")
    chip.setFixedWidth(110)
    chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(chip)
    layout.addWidget(info_label(text), 1)
    return row


def _hint(text: str = "") -> QLabel:
    label = info_label(text)
    label.setObjectName("dim")
    return label


def _section(title: str, *, warn: bool = False) -> tuple[QFrame, QVBoxLayout]:
    """A bordered box with a heading: one group of settings inside a tab."""
    frame = QFrame()
    frame.setObjectName("statTile")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 16)
    layout.setSpacing(10)
    heading = section_label(title, level=2)
    if warn:
        heading.setObjectName("warn")
    layout.addWidget(heading)
    return frame, layout


def _columns(layout: QVBoxLayout, *columns) -> None:
    """Side-by-side section boxes (or stacks of them), equal widths."""
    row = QHBoxLayout()
    row.setSpacing(12)
    for column in columns:
        if isinstance(column, QWidget):
            row.addWidget(column, 1)
        else:
            row.addLayout(column, 1)
    layout.addLayout(row)


class _Stat(QFrame):
    """A value over its caption in a tile, like the Dashboard's stats."""

    def __init__(self, caption: str) -> None:
        super().__init__()
        self.setObjectName("statTile")
        self.setMinimumWidth(150)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)
        self.value = QLabel()
        self.value.setObjectName("statValue")
        self.value.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.caption = QLabel(caption)
        self.caption.setObjectName("statCaption")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)

    def set(self, value: str, caption: str | None = None) -> None:
        self.value.setText(value)
        if caption is not None:
            self.caption.setText(caption)


class _SecretAddress(QFrame):
    """The invite address, blurred until asked for, so a screenshot or a
    stream never leaks it by accident. No buttons: click to show or hide it
    (Enter/Space, Esc hides), right-click to copy it (Ctrl+C), blurred or not."""

    def __init__(self, on_toggle, on_copy) -> None:
        super().__init__()
        self.setObjectName("panelTransparent")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._on_toggle, self._on_copy = on_toggle, on_copy
        self._hidden = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(4)
        self.caption = section_label("", level=3)
        self.subtitle = _hint()
        self.value = QLabel()
        self.value.setObjectName("statValue")
        self._blur = QGraphicsBlurEffect(self.value)
        self._blur.setBlurRadius(24)
        self._blur.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
        self.value.setGraphicsEffect(self._blur)
        self.usage = _hint()
        for label in (self.caption, self.subtitle, self.value, self.usage):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
        layout.insertSpacing(2, 6)
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.setInterval(1500)
        self._flash.timeout.connect(self._show_usage)

    def set(self, address: str, caption: str, subtitle: str, *, hidden: bool) -> None:
        self._hidden = hidden
        self.value.setText(address)
        self.caption.setText(caption)
        self.subtitle.setText(subtitle)
        self._blur.setEnabled(hidden)
        if not self._flash.isActive():
            self._show_usage()

    def flash(self, text: str) -> None:
        self.usage.setText(text)
        self._flash.start()

    def _show_usage(self) -> None:
        self.usage.setText(tr("Click to show · Right-click to copy") if self._hidden
                           else tr("Click to hide · Right-click to copy"))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self._on_copy()
        elif event.button() == Qt.MouseButton.LeftButton:
            self._on_toggle()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self._on_copy()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) or event.key() == Qt.Key.Key_Escape and not self._hidden:
            self._on_toggle()
        else:
            super().keyPressEvent(event)


def _add_fade(label: QLabel) -> None:
    """Let a status label fade out instead of being cleared.

    The text stays, only its opacity goes to 0: clearing or hiding it would
    reflow the card and flash the buttons around it.
    """
    effect = QGraphicsOpacityEffect(label)
    effect.setOpacity(1.0)  # the effect's own default is 0.7
    label.setGraphicsEffect(effect)
    fade = QPropertyAnimation(effect, b"opacity", label)
    fade.setDuration(400)
    fade.setStartValue(1.0)
    fade.setEndValue(0.0)
    label._fade = fade


def _unfade(label: QLabel) -> None:
    label._fade.stop()
    label.graphicsEffect().setOpacity(1.0)


class MultiplayerPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._task: BackgroundTask | None = None
        self._busy = False
        # A quick background write that keeps the buttons as they are.
        self._quiet = False
        self._status = coop.NOT_INSTALLED
        # Co-op was on when another page's update/repair started.
        self._coop_before_op = False
        self._dirty: set[str] = set()
        self._widgets: dict[str, QWidget] = {}
        self._public_ip = ""
        self._fetching_ip = False
        self._ip_failed = False
        self._lan_ip = "127.0.0.1"
        self._address = ""
        #: Profile the settings widgets were loaded from. Edits save there, even
        #: if the active profile changed meanwhile, and a refresh of the same
        #: profile keeps unsaved edits instead of reloading over them.
        self._settings_from = None
        #: Invite address: which one, and whether it is on screen. Hidden by
        #: default so a screenshot or stream never leaks it by accident.
        self._internet = False
        self._ip_shown = False
        self._found: Path | None = None
        self._appid = coop.COP_APPID
        #: Where a new install goes when the profile has GAMMA too: GAMMA
        #: and plain Anomaly share the Anomaly folder, so co-op is set up
        #: for one of them (see coop.KIND_ANOMALY).
        self._kind_pick = coop.KIND_GAMMA
        self._play_bound = False
        self._bridge = _Bridge(self)
        self._bridge.progress.connect(self._on_progress)
        self._say_clear = QTimer(self)
        self._say_clear.setSingleShot(True)
        self._say_clear.timeout.connect(lambda: self.op_label._fade.start())

        # Same frame as the Play page: the scroll area fills the page and
        # the content carries the margins.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(16)
        scroll.setWidget(content)

        root.addWidget(self._status_card())
        self.settings_card = self._settings_card()
        root.addWidget(self.settings_card)
        root.addStretch(1)

    # ---------------------------------------------------------- status card
    def _status_card(self) -> QWidget:
        card, layout = make_card()
        layout.setSpacing(14)

        # Header row like the Dashboard cards: title, status, mod page link.
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(_card_title("xrRazom Co-Op"))
        header.addStretch(1)
        self.status_pill = QLabel()
        self.status_pill.setObjectName("verifyPill")
        header.addWidget(self.status_pill)
        mod_page = QPushButton(tr("xrRazom mod page"))
        mod_page.setObjectName("pillButton")
        mod_page.setFixedHeight(_TITLE_ROW_H)
        mod_page.setCursor(Qt.CursorShape.PointingHandCursor)
        mod_page.clicked.connect(lambda: open_url(coop.MODDB_MOD_PAGE))
        header.addWidget(mod_page)
        layout.addLayout(header)

        # -- not installed: three step tiles, then one big install button -----
        self.install_box, setup = _panel()
        setup.setSpacing(14)
        self.intro_label = info_label("")
        setup.addWidget(self.intro_label)
        # Only asked when the profile has GAMMA; plain Anomaly has no choice.
        self.gamma_pill = _tab(tr("GAMMA"))
        self.anomaly_pill = _tab(tr("Anomaly"))
        for button, kind in ((self.gamma_pill, coop.KIND_GAMMA), (self.anomaly_pill, coop.KIND_ANOMALY)):
            button.clicked.connect(lambda _c=False, k=kind: self._pick_kind(k))
        self.kind_box = QWidget()
        self.kind_box.setObjectName("panelTransparent")
        kind_column = _field(tr("Play co-op on"), _pills(self.gamma_pill, self.anomaly_pill))
        kind_column.setContentsMargins(0, 0, 0, 0)
        self.kind_box.setLayout(kind_column)
        setup.addWidget(self.kind_box)
        self.kind_hint = _hint()
        setup.addWidget(self.kind_hint)
        self.download_button = QPushButton(tr("Download from moddb"))
        self.download_button.setObjectName("secondary")
        self.download_button.clicked.connect(lambda: open_url(coop.MODDB_PAGE))
        self.found_label = info_label("")
        self.found_label.setObjectName("accent")
        tiles = []
        for number, title, text, extra in (
            (1, tr("Download the Slim zip"), tr("Opens moddb in your browser. Get the Slim edition: it's the one made for GAMMA."), self.download_button),
            (2, tr("Install it"), tr("Choose the downloaded zip. COMMANDER backs up your original files first."), self.found_label),
            (3, tr("Play together"), tr("Switch to co-op, pick Hosting or Joining, then launch."), None),
        ):
            tile = QFrame()
            tile.setObjectName("statTile")
            box = QVBoxLayout(tile)
            box.setContentsMargins(16, 14, 16, 16)
            box.setSpacing(10)
            box.addWidget(_step(number, title, text))
            box.addStretch(1)
            if extra is not None:
                box.addWidget(extra)
            tiles.append(tile)
        _columns(setup, *tiles)
        # Same hero button as the Play page: installs the zip found in
        # Downloads/Desktop, or asks for one.
        self.zip_button = QPushButton(tr("Install from zip…"))
        self.zip_button.setObjectName("hero")
        install_hover_grow_text(self.zip_button, "hero_text")
        self.zip_button.clicked.connect(lambda: self._install_zip(self._found) if self._found else self._pick_zip())
        self.zip_button.clicked.connect(play_click_sound)
        setup.addWidget(self.zip_button)
        self.other_zip_button = QPushButton(tr("Choose another zip…"))
        self.other_zip_button.setObjectName("secondary")
        self.other_zip_button.clicked.connect(self._pick_zip)
        setup.addWidget(self.other_zip_button)
        layout.addWidget(self.install_box)

        # -- installed: session choices left, tiles right (Dashboard style) ---
        self.dash_box, dash = _panel()
        dash.setSpacing(14)
        body = QHBoxLayout()
        body.setSpacing(24)
        # Set by the pills below; kept as widgets so they save like the rest.
        for key in ("xrr_host_session", "xrr_host_steam"):
            self._check(key, "").setParent(card)
            self._widgets[key].hide()
        self.host_pill = _tab(tr("Hosting"))
        self.join_pill = _tab(tr("Joining"))
        for button, host in ((self.host_pill, True), (self.join_pill, False)):
            button.clicked.connect(lambda _c=False, h=host: self._widgets["xrr_host_session"].setChecked(h))
        # Steam or LAN networking: the whole party has to agree on it.
        self.steam_pill = _tab(tr("Steam"))
        self.lan_pill = _tab(tr("LAN"))
        for button, steam in ((self.steam_pill, True), (self.lan_pill, False)):
            button.clicked.connect(lambda _c=False, on=steam: self._widgets["xrr_host_steam"].setChecked(on))
        # The Steam app a Steam session runs as. Applied at once (only the
        # steam_appid.txt files change), and the whole party must match.
        self.cop_pill = _tab("Call of Pripyat")
        self.cop_pill.setToolTip(tr("Everyone needs Call of Pripyat in their Steam library."))
        self.spacewar_pill = _tab("Spacewar")
        self.spacewar_pill.setToolTip(tr("Valve's free Steamworks test app: nothing to buy. Steam shows you as playing Spacewar."))
        for button, appid in ((self.cop_pill, coop.COP_APPID), (self.spacewar_pill, coop.SPACEWAR_APPID)):
            button.clicked.connect(lambda _c=False, a=appid: self._set_appid(a))
        port = self._spin("xrr_host_lan_port", 1024, 65535)
        port.setToolTip(tr("Only for LAN sessions. To play over the internet, forward this UDP port on your router."))
        port.setFixedWidth(160)
        # One column per choice, each with its own hint under it; stat
        # tiles on the right.
        self.role_hint = _hint()
        self.session_hint = _hint()
        # On Linux the appid rides in the co-op shortcut's launch
        # options, and Steam only rereads shortcuts when it starts.
        self.app_note = _hint(
            tr("Changing this restarts Steam once, on your next co-op launch.") if os.name != "nt"
            else tr("Everyone in the session must pick the same one.")
        )
        # LAN only: hidden for Steam sessions instead of sitting greyed out.
        self.port_box = QWidget()
        self.port_box.setObjectName("panelTransparent")
        port_row = QHBoxLayout(self.port_box)
        port_row.setContentsMargins(0, 4, 0, 0)
        port_row.setSpacing(10)
        port_caption = QLabel(tr("LAN port (UDP)"))
        port_caption.setObjectName("dim")
        port_row.addWidget(port_caption)
        port_row.addWidget(port)
        port_row.addStretch(1)
        # GAMMA or Anomaly: only with GAMMA, plain Anomaly alone has nothing
        # to switch to. Moves the install at once, no zip needed.
        self.dash_gamma_pill = _tab(tr("GAMMA"))
        self.dash_anomaly_pill = _tab(tr("Anomaly"))
        for button, kind in ((self.dash_gamma_pill, coop.KIND_GAMMA), (self.dash_anomaly_pill, coop.KIND_ANOMALY)):
            button.clicked.connect(lambda _c=False, k=kind: self._switch_kind(k))
        self.dash_kind_box = QWidget()
        self.dash_kind_box.setObjectName("panelTransparent")
        kind_row = QHBoxLayout(self.dash_kind_box)
        kind_row.setContentsMargins(0, 0, 0, 0)
        kind_row.setSpacing(24)
        for index, (caption, pills, extras) in enumerate((
            (tr("Role"), _pills(self.host_pill, self.join_pill), (self.role_hint,)),
            (tr("Session type"), _pills(self.steam_pill, self.lan_pill), (self.session_hint, self.port_box)),
            (tr("Run on Steam as"), _pills(self.cop_pill, self.spacewar_pill), (self.app_note,)),
            (tr("Play co-op on"), _pills(self.dash_gamma_pill, self.dash_anomaly_pill),
             (_hint(tr("Move co-op between GAMMA and Anomaly. No zip needed, and co-op stays on or off as it is.")),)),
        )):
            target = kind_row if index == 3 else body
            if index == 3:
                body.addWidget(self.dash_kind_box, 1)
            if index:
                target.addWidget(_vline())
            column = _field(caption, pills)
            column.setSpacing(6)
            for widget in extras:
                column.addWidget(widget)
            column.addStretch(1)
            target.addLayout(column, 1)
        body.addWidget(_vline())
        stats = QVBoxLayout()
        stats.setSpacing(8)
        self.version_stat = _Stat(tr("xrRazom version"))
        self.players_stat = _Stat(tr("Player slots"))
        self.players_stat.setToolTip(tr("Max players in your server, you included."))
        stats.addWidget(self.version_stat)
        stats.addWidget(self.players_stat)
        body.addLayout(stats)
        dash.addLayout(body)
        self.reverted_label = info_label(tr("A GAMMA update or repair put the original game files back. Re-apply co-op to play together again."))
        self.reverted_label.setObjectName("warn")
        dash.addWidget(self.reverted_label)
        # Installed for GAMMA (or before plain Anomaly was supported), but
        # this profile has no GAMMA: the mod's files would never load.
        self.mismatch_button = QPushButton(tr("Reinstall for Anomaly"))
        self.mismatch_button.setObjectName("secondary")
        self.mismatch_button.clicked.connect(lambda: self._install_zip(self._found) if self._found else self._pick_zip())
        self.mismatch_box = _action_row(
            tr("Set up for GAMMA"),
            tr("This co-op install was made for GAMMA, but this profile has no GAMMA. Reinstall it for Anomaly to play."),
            self.mismatch_button,
        )
        dash.addWidget(self.mismatch_box)

        # One big button that always does the next step: switch to co-op,
        # or launch it once co-op is on (same hero button as the Play page).
        self.hero_button = QPushButton(tr("Launch Co-op"))
        self.hero_button.setObjectName("hero")
        install_hover_grow_text(self.hero_button, "hero_text")
        self.hero_button.clicked.connect(self._hero_clicked)
        dash.addWidget(self.hero_button)
        # Play page layout: secondary actions in a row under the hero button.
        self.sp_button = QPushButton(tr("Switch to Singleplayer"))
        self.sp_button.setObjectName("secondary")
        self.sp_button.setToolTip(tr("Puts your original files back and turns the mod off. Co-op saves are kept."))
        self.sp_button.clicked.connect(lambda: self._run_switch(False))
        self.sp_button.clicked.connect(play_click_sound)
        dash.addWidget(self.sp_button)
        # Playtime and live launch status, mirrored from the Play page that
        # runs every launch (co-op ones included).
        self.playtime_label = info_label("")
        self.playtime_label.setObjectName("dim")
        self.playtime_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        dash.addWidget(self.playtime_label)
        self.launch_live_status = info_label("")
        self.launch_live_status.setObjectName("accent")
        self.launch_live_status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.launch_live_status.setWordWrap(True)
        self.launch_live_status.hide()
        _add_fade(self.launch_live_status)
        dash.addWidget(self.launch_live_status)
        layout.addWidget(self.dash_box)

        # -- shared progress ---------------------------------------------------
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.op_label = info_label("")
        self.op_label.setObjectName("accent")
        self.op_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.op_label.hide()
        _add_fade(self.op_label)
        layout.addWidget(self.op_label)
        return card

    # -------------------------------------------------------- settings card
    def _settings_card(self) -> QWidget:
        card, layout = make_card()
        layout.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(4)
        self.tab_group = QButtonGroup(self)
        self._pages: list[QWidget] = []
        body = QVBoxLayout()
        body.setSpacing(0)
        for index, (name, page) in enumerate(zip(_TABS, (
            self._host_tab(), self._join_tab(), self._player_tab(), self._manage_tab(), self._help_tab(),
        ))):
            button = _tab(tr(name), "subTab")
            self.tab_group.addButton(button, index)
            header.addWidget(button)
            body.addWidget(page)
            self._pages.append(page)
        self.tab_group.idClicked.connect(self._show_tab)
        header.addStretch(1)
        # The tabs' underline sits on the divider, like the main nav bar.
        tabs = QVBoxLayout()
        tabs.setSpacing(0)
        tabs.addLayout(header)
        tabs.addWidget(_divider())
        layout.addLayout(tabs)
        layout.addLayout(body)
        self.tab_group.button(0).setChecked(True)
        self._show_tab(0)
        return card

    def _show_tab(self, index: int) -> None:
        # Hidden pages take no room, so each tab is exactly as tall as it needs.
        for i, page in enumerate(self._pages):
            page.setVisible(i == index)

    def _mark_dirty(self, key: str, *, save: bool = True) -> None:
        # Toggles and spins save at once; text saves when editing ends, so
        # a half-typed name never reaches user.ltx.
        self._dirty.add(key)
        self._update_address()
        if save:
            self._save()

    def _check(self, key: str, label: str, tip: str = "") -> QCheckBox:
        box = QCheckBox(label)
        box.setToolTip(tip)
        box.toggled.connect(lambda _v, k=key: self._mark_dirty(k))
        self._widgets[key] = box
        return box

    def _spin(self, key: str, low: int, high: int, suffix: str = "", step: int = 1) -> QSpinBox:
        # Ranges are the engine's own console limits.
        spin = PlainSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        spin.setMinimumHeight(34)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(lambda _v, k=key: self._mark_dirty(k))
        self._widgets[key] = spin
        return spin

    def _host_tab(self) -> QWidget:
        page, layout = _panel()
        layout.setSpacing(12)
        hosting, box = _section(tr("Server"))
        slots = self._spin("xrr_max_players", 1, 4)
        slots.setToolTip(tr("How many friends can join, up to 4."))
        slots.setFixedWidth(160)
        box.addLayout(_field(tr("Player slots"), slots))
        self._add_rules(box)
        box.addWidget(_divider())
        experimental = section_label(tr("Experimental"), level=3)
        experimental.setObjectName("warn")
        box.addWidget(experimental)
        danger = self._check("xrr_skip_mod_check", tr("Skip mod check"), tr("Lets friends join with different mods. This can cause desyncs or crashes and isn't supported by xrRazom."))
        danger.setObjectName("warn")
        box.addWidget(danger)
        box.addStretch(1)
        box.addWidget(_divider())
        box.addWidget(_hint(tr("Only the host's rules count. They apply to everyone.")))

        invite, right = _section(tr("Invite a friend"))
        # The address kind sits on the heading's row.
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(right.takeAt(0).widget())
        top.addStretch(1)
        right.addLayout(top)
        # Steam sessions join through Steam itself: the steps. Call of
        # Pripyat can also join by address from the main menu, so it shows both.
        self.steam_invite, steps = _panel()
        steps.setContentsMargins(0, 4, 0, 0)
        steps.setSpacing(14)
        for number, (title, text) in enumerate((
            (tr("Start a game"), tr("Load a save or start a new game. That opens your server.")),
            (tr("Invite friends"), tr("Open the Steam overlay (Shift+Tab, or the Steam button on Steam Deck) and invite a friend from your friends list.")),
            (tr("Friends join"), tr("They accept the invite, or join you from their own friends list.")),
        ), 1):
            steps.addWidget(_step(number, title, text))
        steps.addWidget(_hint(tr("Steam invites need no IP or port forwarding.")))
        steps.addStretch(1)
        right.addWidget(self.steam_invite)
        self.ip_divider = _divider()
        right.addWidget(self.ip_divider)
        # LAN sessions join by address.
        self.ip_box, ip = _panel()
        self.ip_box.setMinimumHeight(0)
        kinds = QButtonGroup(self)
        self.lan_tab = _tab(tr("Same network"))
        self.internet_tab = _tab(tr("Internet"))
        self.lan_tab.setChecked(True)
        for button, internet in ((self.lan_tab, False), (self.internet_tab, True)):
            kinds.addButton(button)
            button.clicked.connect(lambda _c=False, i=internet: self._pick_address(i))
            top.addWidget(button)
        # Centred in whatever room the box has.
        self.secret = _SecretAddress(self._toggle_ip, self._copy_invite)
        ip.addStretch(1)
        ip.addWidget(self.secret)
        ip.addStretch(1)
        right.addWidget(self.ip_box, 1)
        right.addWidget(_divider())
        right.addWidget(_hint(tr("Save before you quit. Everyone's progress is stored on your PC.")))
        _columns(layout, hosting, invite)
        layout.addStretch(1)
        return page

    def _join_tab(self) -> QWidget:
        page, layout = _panel()
        join, left = _section(tr("Join a friend"))
        # Same steps as the host's invite, from the other side. Step 2
        # depends on how the session joins: Steam or by address.
        left.setSpacing(14)
        left.addWidget(_step(1, tr("Launch co-op"), tr("Pick the same session type and Steam game as the host, then launch.")))
        self.join_steam_step = _step(2, tr("Join from Steam"), tr("Open the Steam overlay (Shift+Tab, or the Steam button on Steam Deck) and join the host from your friends list, or accept their invite."))
        self.join_lan_step = _step(2, tr("Join by address"), tr("Enter the host's address on the left. In the game's main menu, click Join Session, then Join."))
        left.addWidget(self.join_steam_step)
        left.addWidget(self.join_lan_step)
        left.addWidget(_step(3, tr("Pick a loadout"), tr("First time joining? Choose a starting loadout and press Start.")))
        left.addStretch(1)
        left.addWidget(_divider())
        left.addWidget(_hint(tr("Your character and progress are saved on the host under your player name.")))
        # Spacewar hides the tips box, so its warning moves here.
        self.join_warn = info_label(tr("Don't continue from an autosave made while joined. Only the host's saves hold the full game."))
        self.join_warn.setObjectName("warn")
        left.addWidget(self.join_warn)

        tips, right = _section(tr("Good to know"))
        # The address sits above the tips: Call of Pripyat can also join by
        # address, like the host's invite. Spacewar hides it.
        self.join_address, address = _panel()
        self.join_address_title = section_label("", level=2)
        address.addWidget(self.join_address_title)
        self.join_edit = QLineEdit()
        self.join_edit.setMinimumHeight(34)
        self.join_edit.setPlaceholderText(tr("Host address, e.g. 192.168.1.20:27015"))
        self.join_edit.textEdited.connect(lambda _t: self._mark_dirty("last_ip", save=False))
        self.join_edit.editingFinished.connect(self._save)
        address.addWidget(self.join_edit)
        self.join_address_hint = _hint()
        address.addWidget(self.join_address_hint)
        address.addWidget(_divider())
        right.insertWidget(0, self.join_address)
        for text in (
            tr("You need the same mods and xrRazom version as the host. If the join is rejected, search the game log for [xrRazom] cfg-diff to find the file that differs."),
            tr("Crashed or out of sync? Quit to the main menu and join again."),
        ):
            right.addWidget(info_label("• " + text))
        right.addStretch(1)
        # Footer on the divider, like the Hosting tab's boxes.
        right.addWidget(_divider())
        warn = info_label(tr("Don't continue from an autosave made while joined. Only the host's saves hold the full game."))
        warn.setObjectName("warn")
        right.addWidget(warn)
        self.join_tips = tips
        _columns(layout, tips, join)
        return page

    def _player_tab(self) -> QWidget:
        page, layout = _panel()
        player, left = _section(tr("Player"))
        name = QLineEdit()
        name.setMinimumHeight(34)
        name.setToolTip(tr("Shown to other players. Must be unique in the session."))
        name.setPlaceholderText(tr("Your nickname"))
        name.setMaxLength(32)
        name.textEdited.connect(lambda _t: self._mark_dirty("xrr_player_name", save=False))
        name.editingFinished.connect(self._save)
        self._widgets["xrr_player_name"] = name
        left.addLayout(_fields(
            (tr("Player name"), name),
            (tr("Ping tag lifetime"), self._spin("xrr_tag_duration", 5, 300, " s", 5)),
        ))
        identity = info_label(tr("The host knows you by this name. Change it and you start over as a new player."))
        identity.setObjectName("warn")
        left.addWidget(identity)
        left.addWidget(_divider())
        left.addWidget(_hint(tr("Bind the tag and chat keys in game: Settings → Control → Keybinds → xrRazom Co-op, at the bottom of the list.")))
        left.addStretch(1)

        hud, right = _section(tr("HUD and chat"))
        right.setSpacing(8)
        for box in (
            self._check("xrr_show_marks", tr("Show player markers on PDA map")),
            self._check("xrr_show_names", tr("Show player nameplates")),
            self._check("xrr_show_players_hp", tr("Show other players' health bars")),
            self._check("xrr_show_net_health", tr("Show connection warnings")),
        ):
            right.addWidget(box)
        right.addWidget(_divider())
        for box in (
            self._check("xrr_chat_enable", tr("Text chat")),
            self._check("xrr_chat_sound", tr("Chat sound")),
        ):
            right.addWidget(box)
        right.addStretch(1)
        _columns(layout, player, hud)
        return page

    def _add_rules(self, box: QVBoxLayout) -> None:
        """Session rules: only the host's count, so they live in the Server box."""
        box.addWidget(_divider())
        row = QHBoxLayout()
        row.setSpacing(16)
        box.addLayout(row)
        combat = QVBoxLayout()
        combat.setSpacing(10)
        row.addLayout(combat, 1)
        row.addWidget(_vline())
        travel = QVBoxLayout()
        travel.setSpacing(10)
        row.addLayout(travel, 1)

        combat.addWidget(section_label(tr("Combat"), level=3))
        combat.addWidget(self._check("xrr_friendly_fire", tr("Friendly fire")))
        rescue = self._check("xrr_rescue", tr("Wounded state and rescue"), tr("A killing blow downs the player instead, and a teammate can pick them up."))
        combat.addWidget(rescue)
        medkit = self._check("xrr_rescue_medkit", tr("Rescue requires a medkit"))
        rescue.toggled.connect(medkit.setEnabled)
        combat.addWidget(medkit)
        combat.addWidget(self._check("xrr_allow_peer_save_load", tr("Allow players to save/load"), tr("Lets joined players quicksave and quickload (F5/F9).")))
        combat.addStretch(1)

        travel.addWidget(section_label(tr("Travel"), level=3))
        sync = self._check("xrr_sync_teleport", tr("Sync quest teleports"), tr("When a quest teleports the host, nearby players come along. Not used in vanilla Anomaly."))
        travel.addWidget(sync)
        radius = self._spin("xrr_teleport_radius", 0, 200, " m", 5)
        radius.setToolTip(tr("0 brings everyone on the same map."))
        sync.toggled.connect(radius.setEnabled)
        party = self._check("xrr_party_fast_travel", tr("Require whole party for fast travel"), tr("Fast travel waits until everyone is within the gather radius."))
        travel.addWidget(party)
        gather = self._spin("xrr_gather_radius", 5, 250, " m", 5)
        party.toggled.connect(gather.setEnabled)
        travel.addLayout(_fields(
            (tr("Teleport follow radius"), radius),
            (tr("Party gather radius"), gather),
        ))
        travel.addStretch(1)

    def _manage_tab(self) -> QWidget:
        page, layout = _panel()
        layout.setSpacing(8)
        self.update_button = QPushButton(tr("Update from zip…"))
        self.update_button.clicked.connect(self._pick_zip)
        layout.addWidget(_action_row(tr("Update xrRazom"), tr("Install a newer Slim zip. Your settings are kept."), self.update_button))
        layout.addWidget(_divider())
        # Plain Anomaly co-op starts the game exe the launcher last saved
        # (play_page.plain_game_exe), so the launcher is where it's changed.
        launcher = QPushButton(tr("Open Anomaly Launcher"))
        launcher.clicked.connect(lambda: self.window._ensure_page("play").open_anomaly_launcher())
        self.launcher_manage = QWidget()
        self.launcher_manage.setObjectName("panelTransparent")
        launcher_box = QVBoxLayout(self.launcher_manage)
        launcher_box.setContentsMargins(0, 0, 0, 0)
        launcher_box.setSpacing(8)
        launcher_box.addWidget(_action_row(
            tr("Renderer and game options"),
            tr("Co-op starts the game with the renderer saved in the Anomaly Launcher, such as DX11 or DX9. Change it there, press Save Settings, then close the launcher."),
            launcher,
        ))
        launcher_box.addWidget(_divider())
        layout.addWidget(self.launcher_manage)
        check = QPushButton(tr("Check moddb for updates"))
        check.clicked.connect(lambda: open_url(coop.MODDB_PAGE))
        layout.addWidget(_action_row(tr("New releases"), tr("Everyone in a session must use the same version."), check))
        layout.addWidget(_divider())
        self.backup_button = QPushButton(tr("Export or import…"))
        self.backup_button.clicked.connect(self._export_or_import)
        layout.addWidget(_action_row(
            tr("Export or import co-op"),
            tr("Keep your co-op settings, keybinds, MCM values and saves in a zip, or bring one back."),
            self.backup_button,
        ))
        layout.addWidget(_divider())
        self.uninstall_button = QPushButton(tr("Uninstall co-op"))
        self.uninstall_button.setObjectName("danger")
        self.uninstall_button.clicked.connect(self._uninstall)
        layout.addWidget(_action_row(tr("Remove co-op"), tr("Puts your original files back and deletes the mod. Co-op saves are kept."), self.uninstall_button))
        return page

    def _help_tab(self) -> QWidget:
        """The xrRazom guide from moddb, reworded for this page's controls."""
        page, layout = _panel()
        layout.setSpacing(12)

        start, left = _section(tr("Getting started"))
        left.setSpacing(14)
        for number, (title, text) in enumerate((
            (tr("Pick your role and session"), tr("Choose Hosting or Joining above. The whole party picks the same session type and Steam game.")),
            (tr("Set your player name"), tr("On the Player tab. The host knows you by this name, so keep it the same.")),
            (tr("Launch Co-op"), tr("COMMANDER sets the game up for the choices above.")),
            (tr("Play together"), tr("The host loads a save or starts a new game, then friends join. The Hosting and Joining tabs walk you through it.")),
        ), 1):
            left.addWidget(_step(number, title, text))
        left.addStretch(1)
        left.addWidget(_divider())
        left.addWidget(_hint(tr("Everyone needs the same mods and xrRazom version as the host.")))
        self.zip_label = _hint()
        left.addWidget(self.zip_label)

        works, right = _section(tr("How it works"))
        _facts(right, (
            (tr("The host keeps the world"), tr("Everything is created and saved on the host's PC. Everyone else mirrors it.")),
            (tr("Join any time"), tr("Up to 5 players, the host included. Friends can join at any point in the game and leave whenever they like.")),
            (tr("Your name is your profile"), tr("Change it and the host sees a new player with a fresh character.")),
            (tr("The host sets the rules"), tr("Session rules on the Hosting tab apply to everyone.")),
            (tr("Call of Pripyat or Spacewar"), tr("Everyone picks the same one. Call of Pripyat must be owned. Spacewar is free.")),
            (tr("GAMMA or Anomaly"), tr("One Anomaly folder runs co-op for one of them at a time. To keep both ready, use a second Anomaly folder and profile for Anomaly co-op.")),
        ))
        _columns(layout, start, works)

        game, left = _section(tr("In game"))
        left.setSpacing(10)
        for key, text in (
            (tr("F5 / F9"), tr("Quicksave and quickload, if the host allows it.")),
            (tr("Send to"), tr("Right-click an item or money near a player to give it to them.")),
            (tr("Tag / Chat"), tr("Bind keys under Settings → Control → Keybinds → xrRazom Co-op.")),
            (tr("Rescue"), tr("Pick up a downed teammate, if the host turned it on.")),
            (tr("Anabiotic"), tr("Protects you from the current emission.")),
        ):
            left.addWidget(_key_row(key, text))
        left.addStretch(1)
        left.addWidget(_divider())
        left.addWidget(_hint(tr("Companions follow the host. Only the host can give them orders.")))

        fixes, right = _section(tr("Troubleshooting"))
        _facts(right, (
            (tr("Join rejected"), tr("Your mods differ from the host's. Search the game log for [xrRazom] cfg-diff to find the file.")),
            (tr("Crashed or out of sync"), tr("Quit to the main menu and join again. Your progress is kept on the host.")),
            (tr("Quest won't progress"), tr("Give the quest item to the host, or ask the host to talk to the NPC.")),
            (tr("Engine reverted"), tr("A GAMMA update or repair put the original files back. Click Re-apply co-op.")),
        ))
        _columns(layout, game, fixes)

        issues, box = _section(tr("Known issues"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        for index, text in enumerate((
            tr("Saving, loading and level changes can crash now and then. Save often."),
            tr("Don't continue from an autosave made while joined. Only the host's saves hold the full game."),
            tr("Weather can look slightly different for each player."),
            tr("Some quests and dialogs depend on your faction."),
            tr("The debug menu can desync the session. Some options, like Weather, stay in the save."),
            tr("Not in co-op yet: vehicles, personal companions and Azazel mode."),
        )):
            label = _hint("• " + text)
            grid.addWidget(label, index // 2, index % 2)
        label.setObjectName("warn")  # the missing-features line, so it stands out
        box.addLayout(grid)
        box.addWidget(_divider())
        gamma = info_label(tr("GAMMA works with co-op, but not every mod has been tested together. Expect bugs."))
        gamma.setObjectName("warn")
        box.addWidget(gamma)
        layout.addWidget(issues)

        layout.addWidget(_divider())
        discord = QPushButton(tr("Open Discord"))
        discord.clicked.connect(lambda: open_url(coop.DISCORD_URL))
        layout.addWidget(_action_row(tr("Bugs and questions"), tr("The xrRazom Discord has support, bug reports and patches for other mods."), discord))
        return page

    # ---------------------------------------------------------------- state
    def _profile(self):
        return self.window.settings.active_profile

    def refresh(self) -> None:
        profile = self._profile()
        self._found = coop.find_downloaded_zip()
        if profile is None:
            self._status = coop.NOT_INSTALLED
        else:
            try:
                self._status = coop.status(profile.anomaly)
            except OSError:
                self._status = coop.NOT_INSTALLED
        state = coop.load_state(profile.anomaly) if profile else None
        self._appid = int((state or {}).get("appid", coop.COP_APPID))
        text, level = _STATUS[self._status]
        self.status_pill.setText(tr(text))
        self.status_pill.setProperty("level", level)
        self.status_pill.style().unpolish(self.status_pill)
        self.status_pill.style().polish(self.status_pill)
        release = (state or {}).get("release", {})
        custom = self._status != coop.NOT_INSTALLED and not release.get("version")
        self.version_stat.set(release.get("version") or (tr("Custom") if custom else "?"))
        self.version_stat.setToolTip(
            tr("A build without a version number, such as a test build. Everyone playing together needs the same one.")
            if custom else tr("Everyone playing together needs this version.")
        )
        zip_name = coop.installed_zip(profile.anomaly, profile.gamma) if state else ""
        self.zip_label.setText(tr("Installed xrRazom version: {name}", name=zip_name))
        self.zip_label.setVisible(bool(zip_name))
        self._lan_ip = coop.lan_ip()
        self._ip_failed = False
        if profile is not None and (
            not self._dirty or self._settings_from is None or str(self._settings_from.anomaly) != str(profile.anomaly)
        ):
            self._load_settings(profile)
        self._update_address()
        self._sync_enabled()

    def _load_settings(self, profile) -> None:
        values, _present = coop.read_settings(profile.anomaly)
        for key, widget in self._widgets.items():
            widget.blockSignals(True)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(values[key]))
            elif isinstance(widget, QSpinBox):
                widget.setValue(int(values[key]))
            else:
                widget.setText(str(values[key]))
            widget.blockSignals(False)
        # Signals were blocked above, so the dependent widgets are synced by hand.
        for parent, child in (
            ("xrr_rescue", "xrr_rescue_medkit"),
            ("xrr_sync_teleport", "xrr_teleport_radius"),
            ("xrr_party_fast_travel", "xrr_gather_radius"),
        ):
            self._widgets[child].setEnabled(self._widgets[parent].isChecked())
        self._widgets["xrr_host_lan_port"].setEnabled(not self._widgets["xrr_host_steam"].isChecked())
        self.join_edit.setText(coop.read_last_ip(profile.anomaly, profile.gamma, profile.mo2_profile))
        self._dirty.clear()
        self._settings_from = profile

    def _has_gamma(self) -> bool:
        profile = self._profile()
        return profile is not None and gamma_installed(profile.gamma, profile.mo2_profile)

    def _install_kind(self) -> str:
        """How the next install (or update) of the zip goes for this profile."""
        profile = self._profile()
        if not self._has_gamma():
            return coop.KIND_ANOMALY
        if self._status != coop.NOT_INSTALLED:
            return coop.install_kind(profile.anomaly)  # an update keeps it
        return self._kind_pick

    def _pick_kind(self, kind: str) -> None:
        self._kind_pick = kind
        self._sync_enabled()

    def _sync_kind(self, installed: bool) -> bool:
        """The install kind's widgets; True when the install doesn't fit the profile."""
        has_gamma = self._has_gamma()
        kind = self._install_kind()
        plain = kind == coop.KIND_ANOMALY
        self.intro_label.setText(
            tr("Play Anomaly with up to 4 friends. You can switch back to singleplayer any time.") if plain
            else tr("Play GAMMA with up to 4 friends. You can switch back to singleplayer any time.")
        )
        self.kind_box.setVisible(has_gamma)
        self.gamma_pill.setChecked(not plain)
        self.anomaly_pill.setChecked(plain)
        if not has_gamma:
            hint = tr("No GAMMA found, so co-op is set up for Anomaly. Everything goes into your Anomaly folder.")
        elif plain:
            hint = tr("Everything goes into your Anomaly folder. While co-op is on, GAMMA can't start: switch to singleplayer first to play GAMMA.")
        else:
            hint = tr("The co-op files are added to GAMMA as a Mod Organizer mod.")
        self.kind_hint.setText(hint)
        self.dash_kind_box.setVisible(installed and has_gamma)
        self.dash_gamma_pill.setChecked(not plain)
        self.dash_anomaly_pill.setChecked(plain)
        self.launcher_manage.setVisible(installed and plain)
        profile = self._profile()
        mismatch = installed and not has_gamma and coop.install_kind(profile.anomaly) == coop.KIND_GAMMA
        self.mismatch_box.setVisible(mismatch)
        return mismatch

    def _sync_enabled(self) -> None:
        installed = self._status != coop.NOT_INSTALLED
        # Greying the buttons for a few file writes only makes them flash.
        idle = self._quiet or (not self._busy and not self.window.install_busy)
        mismatch = self._sync_kind(installed)
        self.install_box.setVisible(not installed)
        self.dash_box.setVisible(installed)
        self.settings_card.setVisible(installed)
        self.reverted_label.setVisible(self._status == coop.REVERTED)
        if self._status == coop.COOP:
            hero = tr("Launch Co-op")
        elif self._status == coop.REVERTED:
            hero = tr("Re-apply co-op")
        else:
            hero = tr("Switch to co-op")
        set_hover_grow_text(self.hero_button, hero)
        self.hero_button.setToolTip(
            tr("Backs up your game files, installs the co-op engine and turns the mod on.")
            if self._status == coop.SINGLEPLAYER else ""
        )
        self.sp_button.setVisible(self._status in (coop.COOP, coop.REVERTED))
        steam = self._widgets["xrr_host_steam"].isChecked()
        for pill in (self.cop_pill, self.spacewar_pill):
            pill.setEnabled(idle and steam)
        play = getattr(self.window, "_pages", {}).get("play")
        # Follow launches started anywhere (Play page, Dashboard, here).
        if play is not None and not self._play_bound:
            self._play_bound = True
            play.launch_state_changed.connect(lambda _launching: self._sync_enabled())
            play.launch_status_changed.connect(self._mirror_launch_status)
            self._mirror_launch_status()
        launching = play is not None and play.is_launching
        # A running co-op game turns the hero into "Quit Co-op", like the
        # Play page's "Quit Game".
        quit_coop = launching and self._status == coop.COOP
        if quit_coop:
            set_hover_grow_text(self.hero_button, tr("Quit Co-op"))
        self.hero_button.setEnabled(idle and (quit_coop or not launching) and not mismatch)
        self.sp_button.setEnabled(idle and not launching)
        for button in (self.zip_button, self.other_zip_button, self.update_button, self.backup_button, self.uninstall_button, self.mismatch_button, self.dash_gamma_pill, self.dash_anomaly_pill):
            button.setEnabled(idle)
        found = self._found is not None
        self.found_label.setVisible(found)
        self.other_zip_button.setVisible(found)
        if found:
            self.found_label.setText(tr("Found {name} in {folder}.", name=self._found.name, folder=self._found.parent.name))
        set_hover_grow_text(self.zip_button, tr("Install {name}", name=self._found.name) if found else tr("Install from zip…"))

    def _mirror_launch_status(self) -> None:
        play = self.window._pages["play"]
        self.playtime_label.setText(play.playtime_label.text())
        label = self.launch_live_status
        if play.launch_live_status.text():
            _unfade(label)
            label.setStyleSheet(play.launch_live_status.styleSheet())
            label.setText(play.launch_live_status.text())
            label.show()
        elif label.graphicsEffect().opacity() == 1.0 and label._fade.state() != QAbstractAnimation.State.Running:
            label._fade.start()

    def _say(self, text: str) -> None:
        self._say_clear.stop()
        _unfade(self.op_label)
        self.op_label.setText(notice(text))
        self.op_label.setVisible(bool(text))

    def on_busy_changed(self, busy: bool) -> None:
        if not self._busy:  # someone else's operation, not ours
            profile = self._profile()
            if busy:
                self._coop_before_op = profile is not None and self._coop_status(profile) == coop.COOP
            elif self._coop_before_op:
                self._coop_before_op = False
                if self._coop_status(profile) == coop.REVERTED:
                    QTimer.singleShot(0, self._offer_reapply)
        self._sync_enabled()

    @staticmethod
    def _coop_status(profile) -> str:
        if profile is None:
            return coop.NOT_INSTALLED
        try:
            return coop.status(profile.anomaly)
        except OSError:
            return coop.NOT_INSTALLED

    def _offer_reapply(self) -> None:
        """An update or repair just put the original engine back over co-op."""
        self.refresh()
        profile = self._profile()
        try:
            changed = profile is not None and coop.engine_changed(profile.anomaly)
        except OSError:
            changed = False
        if QMessageBox.question(
            self, tr("Re-apply co-op?"),
            tr("The update put the original game files back, so co-op is off for now."),
            note=tr("This update changed the game engine. Your xrRazom version may not work with it, so check the xrRazom Discord for a newer release first.")
            if changed else "",
            labels={QMessageBox.StandardButton.Yes: tr("Re-apply co-op"), QMessageBox.StandardButton.No: tr("Not now")},
        ) == QMessageBox.StandardButton.Yes:
            self._run_switch(True)

    def _update_address(self) -> None:
        port = self._widgets["xrr_host_lan_port"].value()
        steam = self._widgets["xrr_host_steam"].isChecked()
        host = self._widgets["xrr_host_session"].isChecked()
        (self.host_pill if host else self.join_pill).setChecked(True)
        # A joiner's server settings do nothing, so lock them out.
        # Picking a role opens its tab; other updates leave the tab alone.
        self.tab_group.button(0).setEnabled(host)
        if host != getattr(self, "_was_host", None) or (not host and self.tab_group.checkedId() == 0):
            self._was_host = host
            tab = 0 if host else 1
            self.tab_group.button(tab).setChecked(True)
            self._show_tab(tab)
        (self.steam_pill if steam else self.lan_pill).setChecked(True)
        # Greying the buttons for a few file writes only makes them flash.
        idle = self._quiet or (not self._busy and not self.window.install_busy)
        for pill in (self.cop_pill, self.spacewar_pill):
            pill.setEnabled(steam and idle)
        self._widgets["xrr_host_lan_port"].setEnabled(not steam)
        self.port_box.setVisible(not steam)
        self.app_note.setVisible(steam)
        self.role_hint.setText(
            tr("Starts a server when you load a save or start a new game.") if host
            else tr("Pick the same session type as the host before launching.")
        )
        spacewar = self._appid == coop.SPACEWAR_APPID
        steam_spacewar = steam and spacewar
        self.session_hint.setText(
            tr("Everyone joins by address. Steam can stay open.") if not steam
            else tr("Everyone keeps Steam open. Spacewar starts on its own, no game to buy.") if spacewar
            else tr("Everyone keeps Steam open and owns Call of Pripyat on Steam.")
        )
        (self.spacewar_pill if spacewar else self.cop_pill).setChecked(True)
        self.players_stat.set(str(self._widgets["xrr_max_players"].value()))
        # A Steam session as Call of Pripyat still joins by the host's public IP.
        steam_ip = steam and not spacewar
        self.steam_invite.setVisible(steam)
        self.join_steam_step.setVisible(steam)
        self.join_lan_step.setVisible(not steam)
        self.join_address.setVisible(not steam or steam_ip)
        self.join_address_title.setText(tr("Join by IP address") if steam else tr("Host address"))
        self.join_address_hint.setText(
            tr("In the game's main menu, click Join Session, then Join.") if steam
            else tr("Ask the host for it: it's on their Hosting tab.")
        )
        self.join_tips.setVisible(not steam_spacewar)
        self.join_warn.setVisible(steam_spacewar)
        self.ip_divider.setVisible(steam_ip)
        self.ip_box.setVisible(not steam or steam_ip)
        # Spacewar keeps the address's space, so switching from Call of
        # Pripyat doesn't move the page.
        for widget in (self.ip_divider, self.ip_box):
            policy = widget.sizePolicy()
            policy.setRetainSizeWhenHidden(steam)
            widget.setSizePolicy(policy)
        for widget in (self.lan_tab, self.internet_tab):
            widget.setVisible(not steam)
        # The Hosting tab shows the address whatever the role, so fetch it
        # whenever it's on screen; otherwise it sits at "…" forever.
        if steam_ip and not self._public_ip and not self._ip_failed:
            self._fetch_public_ip()
        # Spacewar hides the address but keeps its space, so fill it as for
        # Call of Pripyat: the same text height means nothing jumps.
        internet = self._internet or steam
        subtitle = (
            tr("Couldn't find your internet address") if steam and self._ip_failed
            else tr("Friends without the invite can type this address in the game's main menu, under Join Session. Forward UDP port {port} on your router first.", port=port) if steam
            else tr("Forward UDP port {port} on your router, then share this.", port=port) if internet
            else tr("Share this with friends on your network.")
        )
        ip = (self._public_ip or "…") if internet else self._lan_ip
        self._address = f"{ip}:{port}"
        heading = tr("Join by IP address") if steam else tr("Internet address") if internet else tr("LAN address")
        self.secret.set(self._address, heading, subtitle, hidden=not self._ip_shown)

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

    def _public_ip_found(self, ip: str) -> None:
        self._fetching_ip = False
        self._public_ip = ip
        self._update_address()

    def _public_ip_failed(self, message: str) -> None:
        self._fetching_ip = False
        if not self._internet:
            # Steam as Call of Pripyat fetches on its own: say so quietly, no pop-up.
            self._ip_failed = True
            self._update_address()
            return
        self.lan_tab.setChecked(True)
        self._pick_address(False)
        QMessageBox.warning(self, tr("Couldn't find your internet address"), message)

    def _copy_invite(self) -> None:
        if not self._address or self._address.startswith("…"):
            return
        QGuiApplication.clipboard().setText(self._address)
        self.secret.flash(tr("Address copied."))

    def _hero_clicked(self) -> None:
        # Stays clickable during a quiet write; launching mid-write isn't safe.
        if self._busy:
            return
        play = getattr(self.window, "_pages", {}).get("play")
        if play is not None and play.is_launching:
            play._confirm_quit_game()
        elif self._status == coop.COOP:
            play_click_sound()
            self._launch()
        else:
            self._run_switch(True)

    # ---------------------------------------------------------- operations
    def _ready(self) -> object | None:
        profile = self._profile()
        if profile is None:
            QMessageBox.warning(self, tr("No profile selected"), tr("Create or pick a GAMMA profile on the Profiles page first."))
            return None
        if self.window.install_busy or self._busy:
            return None
        if mo2_running(force=True) or game_running(force=True):
            QMessageBox.warning(self, tr("Close the game first"), tr("Mod Organizer 2 or the game is still open. Close both, then try again."))
            return None
        return profile

    def _start(self, label: str, fn, *args, on_done=None, quiet: bool = False, done: str | None = None) -> None:
        self._busy = True
        self._quiet = quiet
        self.window.set_install_busy(True, "coop")
        if not quiet:
            self._say(label)
        self._sync_enabled()
        self._task = BackgroundTask(fn, *args, parent=self)
        self._task.result.connect(lambda result: self._finish(None, result, on_done, quiet, done))
        self._task.error.connect(lambda msg: self._finish(msg, None, on_done, quiet, done))
        self._task.start()

    def _finish(self, error: str | None, result, on_done, quiet: bool = False, done: str | None = None) -> None:
        self._busy = False
        self._quiet = False
        self.window.set_install_busy(False)
        self.progress.hide()
        if error is not None:
            self._say(tr("Failed: {error}", error=tr(error)))
        else:
            if not quiet:
                self._say(done or tr("Done"))
                self._say_clear.start(5000)  # nothing to act on, so it fades
            if on_done:
                on_done(result)
        self.refresh()

    def _on_progress(self, done: int, total: int) -> None:
        # Only jobs that report real progress get the bar; a spinner says nothing the label doesn't.
        if total > 0:
            self.progress.show()
            self.progress.setRange(0, total)
            self.progress.setValue(done)

    def _pick_zip(self) -> None:
        start = str(self._found.parent if self._found else Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(self, tr("Select the xrRazom Slim zip"), start, "Zip (*.zip)")
        if path:
            self._install_zip(Path(path))

    def _install_zip(self, path: Path) -> None:
        profile = self._ready()
        if profile is None:
            return
        try:
            info = coop.inspect_zip(path)
        except (coop.CoopError, OSError) as exc:
            QMessageBox.warning(self, tr("This isn't an xrRazom zip"), tr(str(exc)))
            return
        kind = self._install_kind()
        if kind == coop.KIND_GAMMA and "slim" not in info.get("title", "").lower() and QMessageBox.question(
            self, tr("Install this edition?"),
            tr("{title} isn't the Slim edition from moddb. Other editions and test builds install the same way and work with co-op.", title=info.get("title") or path.name),
            labels={QMessageBox.StandardButton.Yes: tr("Install now"), QMessageBox.StandardButton.No: tr("Cancel")},
        ) != QMessageBox.StandardButton.Yes:
            return
        self._start(tr("Installing {name}…", name=path.name), self._install_job, path, profile, kind, done=tr("xrRazom Co-op installed"))

    def _install_job(self, path: Path, profile, kind: str):
        return coop.install(path, profile.anomaly, profile.gamma, profile.mo2_profile, self._bridge.progress.emit, kind=kind)

    def _run_switch(self, to_coop: bool) -> None:
        profile = self._ready()
        if profile is None:
            return
        fn = coop.switch_to_coop if to_coop else coop.switch_to_singleplayer
        label = tr("Switching to co-op…") if to_coop else tr("Switching to singleplayer…")
        done = tr("Switched to co-op") if to_coop else tr("Switched to singleplayer")
        self._start(label, fn, profile.anomaly, profile.gamma, profile.mo2_profile, done=done)

    def _switch_kind(self, kind: str) -> None:
        profile = self._ready() if kind != self._install_kind() else None
        if profile is None:
            self._sync_enabled()  # puts the pills back
            return
        to_plain = kind == coop.KIND_ANOMALY
        self._start(
            tr("Switching co-op to Anomaly…") if to_plain else tr("Switching co-op to GAMMA…"),
            coop.set_install_kind, profile.anomaly, profile.gamma, profile.mo2_profile, kind,
            done=tr("Co-op is set up for Anomaly") if to_plain else tr("Co-op is set up for GAMMA"),
        )

    def _set_appid(self, appid: int) -> None:
        profile = self._ready() if appid != self._appid else None
        # Unsaved edits would be lost to the refresh after the switch.
        if profile is None or (self._dirty and not self._save()):
            self._update_address()  # puts the pills back
            return
        # A few small file writes: no progress bar or "Done" popping in and out.
        self._start(tr("Switching Steam game…"), coop.set_steam_appid, profile.anomaly, appid, quiet=True)

    def _uninstall(self) -> None:
        profile = self._ready()
        if profile is None:
            return
        SB = QMessageBox.StandardButton
        answer = QMessageBox.question(
            self, tr("Uninstall co-op?"),
            tr("This removes xrRazom Co-op and puts your original game files back."),
            SB.Save | SB.Yes | SB.No, SB.Save,
            points=(
                tr("Your co-op saves are kept."),
                tr("Export first to keep your co-op settings and saves in a zip."),
                tr("You can install co-op again at any time."),
            ),
            labels={SB.Save: tr("Export first"), SB.Yes: tr("Uninstall"), SB.No: tr("Cancel")},
        )
        uninstall = lambda _result=None: self._start(
            tr("Uninstalling…"), coop.uninstall, profile.anomaly, profile.gamma, profile.mo2_profile,
            done=tr("xrRazom Co-op uninstalled"),
        )
        if answer == SB.Save:
            self._export(then=uninstall)  # uninstalls only once the zip is written
        elif answer == SB.Yes:
            uninstall()

    def _export_or_import(self) -> None:
        SB = QMessageBox.StandardButton
        answer = QMessageBox.question(
            self, tr("Export or import co-op"), "",
            SB.Save | SB.Open | SB.Cancel, SB.Save,
            points=(
                tr("Save your co-op settings, keybinds, MCM values and all your saves to a zip."),
                tr("Bring back an exported zip. Files it replaces are backed up first."),
            ),
            labels={SB.Save: tr("Export…"), SB.Open: tr("Import…"), SB.Cancel: tr("Cancel")},
        )
        if answer == SB.Save:
            self._export()
        elif answer == SB.Open:
            self._import()

    def _export(self, then=None) -> None:
        profile = self._ready()
        if profile is None or not self._save():
            return
        suggested = Path.home() / f"xrRazom co-op {time.strftime('%Y-%m-%d')}.zip"
        path, _ = QFileDialog.getSaveFileName(self, tr("Export co-op"), str(suggested), "Zip (*.zip)")
        if not path:
            return

        def exported(dest) -> None:
            self._say(tr("Exported to {path}", path=str(dest)))
            if then:
                then()

        self._start(tr("Exporting…"), coop.export_session, profile.anomaly, profile.gamma, profile.mo2_profile, path,
                    on_done=exported)

    def _import(self) -> None:
        profile = self._ready()
        if profile is None or not self._save():
            return
        path, _ = QFileDialog.getOpenFileName(self, tr("Import co-op"), str(Path.home()), "Zip (*.zip)")
        if not path:
            return
        try:
            manifest = coop.read_export(path)
        except coop.CoopError as exc:
            QMessageBox.warning(self, tr("Couldn't import this zip"), tr(str(exc)))
            return
        game = "Spacewar" if manifest.get("appid") == coop.SPACEWAR_APPID else "Call of Pripyat"
        if QMessageBox.question(
            self, tr("Import co-op?"),
            tr("This puts back the co-op setup from {name}.", name=Path(path).name),
            points=(
                tr("{count} saves", count=int(manifest.get("saves") or 0)),
                tr("Co-op settings, keybinds and MCM values"),
                tr("Steam game: {game}", game=game),
                tr("Files it replaces are backed up first."),
            ),
            labels={QMessageBox.StandardButton.Yes: tr("Import"), QMessageBox.StandardButton.No: tr("Cancel")},
        ) != QMessageBox.StandardButton.Yes:
            return
        self._dirty.clear()  # the imported settings replace what's on screen
        self._start(tr("Importing…"), coop.import_session, profile.anomaly, profile.gamma, profile.mo2_profile,
                    profile.profile_name, path, done=tr("Co-op imported"))

    # -------------------------------------------------------------- settings
    def _save(self) -> bool:
        profile = self._settings_from
        if profile is None:
            return False
        if not self._dirty:
            return True
        changes = {}
        for key in self._dirty - {"last_ip"}:
            widget = self._widgets[key]
            if isinstance(widget, QCheckBox):
                changes[key] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                changes[key] = widget.value()
            elif widget.text().strip():
                changes[key] = widget.text().strip()
        # An empty name would make the host treat you as a new player; keep
        # the saved one instead.
        if "xrr_player_name" in self._dirty and "xrr_player_name" not in changes:
            self._widgets["xrr_player_name"].setText(coop.read_settings(profile.anomaly)[0]["xrr_player_name"])
        try:
            if changes:
                coop.write_settings(profile.anomaly, changes)
            if "last_ip" in self._dirty:
                coop.write_last_ip(profile.anomaly, profile.gamma, profile.mo2_profile, self.join_edit.text())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, tr("Couldn't save your settings"), str(exc))
            return False
        self._dirty.clear()
        self._update_address()
        return True

    def _launch(self) -> None:
        if not self._save():
            return
        steam = self._widgets["xrr_host_steam"].isChecked()
        # On Linux a Steam session launches through Steam, which starts it.
        # LAN doesn't care whether Steam is open.
        if steam and os.name == "nt" and not steam_running() and QMessageBox.question(
            self, tr("Steam isn't running"), tr("A Steam session needs Steam open before the game starts. Start Steam, or launch anyway."),
            labels={QMessageBox.StandardButton.Yes: tr("Launch anyway"), QMessageBox.StandardButton.No: tr("Cancel")},
        ) != QMessageBox.StandardButton.Yes:
            return
        # Launch from here, like the Dashboard's Play button: the page stays
        # put and the hero button is greyed while the game runs.
        play = self.window._ensure_page("play")
        if play.is_launching:
            return
        self._sync_enabled()  # binds launch_state_changed before the launch starts
        play.refresh()
        play.launch_game(coop_launch=True)
        self._sync_enabled()
