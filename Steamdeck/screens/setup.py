"""First-run setup: from "nothing configured" to a profile ready to install.

Before this screen a Deck user who opened Deck Mode first had no way
forward: every screen said "create or activate a profile first", and
creating one meant typing six absolute paths into the desktop interface.
What a profile actually needs on a handheld is one decision - *which drive*
(the internal SSD or the SD card) - so that is the question this asks, and
it derives the rest:

    <drive>/GAMMA/Anomaly   <drive>/GAMMA/GAMMA   <drive>/GAMMA/cache

The name defaults to "GAMMA" and can be changed with the on-screen
keyboard. Creation runs the CLI's own ``config create`` with the same
arguments the desktop Profiles page builds (``create_profile_args``), so a
profile made here is indistinguishable from one made there.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLineEdit

from commander_gui.i18n import tr
from commander_gui.modlist import seed_new_mo2_profile
from commander_gui.settings import CliProfile, cli_ok, run_config_command
from commander_gui.ui.common import BackgroundTask, free_space_bytes, human_size
from commander_gui.ui.profiles_page import create_profile_args

from ..folder_picker import candidate_roots, show_folder_picker
from ..widgets import DeckCard, DeckRow, deck_button, deck_label
from .base import DeckScreen

#: What a full install needs, used only to label drives that are too small.
_NEEDED_BYTES = 150 * 1024**3

#: The folder created under the chosen drive, holding all three install dirs.
_INSTALL_DIR = "GAMMA"


def install_root_for(drive: Path) -> Path:
    """Where the three install folders go on ``drive``.

    The home directory gets ``~/Games/GAMMA`` rather than ``~/GAMMA``,
    matching where Heroic, Lutris and SteamOS's own tooling put games.
    """
    if drive == Path.home():
        return drive / "Games" / _INSTALL_DIR
    return drive / _INSTALL_DIR


def profile_for(root: Path, name: str) -> CliProfile:
    return CliProfile(
        active=True,
        profile_name=name,
        anomaly=str(root / "Anomaly"),
        gamma=str(root / "GAMMA"),
        cache=str(root / "cache"),
    )


def unique_name(base: str, existing: list[str]) -> str:
    if base not in existing:
        return base
    number = 2
    while f"{base} {number}" in existing:
        number += 1
    return f"{base} {number}"


class SetupScreen(DeckScreen):
    def build(self) -> None:
        self._root: Path | None = None
        self._task: BackgroundTask | None = None
        self._step = "drive"
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(tr("Profile name"))
        self.name_edit.setMaxLength(40)
        self._widgets: list = []
        self._default = None
        self._render()

    # -- rendering --------------------------------------------------------
    def _clear(self) -> None:
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.name_edit:
                widget.deleteLater()
        self.name_edit.setParent(None)

    def _render(self) -> None:
        self._clear()
        self._default = None
        if self._step == "drive":
            self._render_drive()
        elif self._step == "review":
            self._render_review()
        else:
            self._render_done()
        self.body.addStretch(1)
        # Each step replaces every widget, the focused one included: move
        # focus onto the new step's own starting point.
        if (
            self._default is not None
            and self.window.current_page() is self
            and self.window.current_overlay() is None
        ):
            self.window._focus.focus(self._default)

    def default_focus(self):
        return self._default

    def _render_drive(self) -> None:
        self.body.addWidget(deck_label(tr("Welcome to COMMANDER"), role="title"))
        self.body.addWidget(
            deck_label(
                tr(
                    "Choose where GAMMA should be installed. A full install "
                    "needs about {size} - the SD card is a good choice if the "
                    "internal drive is small.",
                    size=human_size(_NEEDED_BYTES),
                ),
                role="body",
                wrap=True,
            )
        )
        self.drive_rows: list[DeckRow] = []
        for label, drive in candidate_roots():
            title = tr("Internal storage") if drive == Path.home() else label
            free = free_space_bytes(drive)
            value = tr("{free} free", free=human_size(free)) if free is not None else ""
            row = DeckRow(title, value)
            if free is not None and free < _NEEDED_BYTES:
                row.value_label.setObjectName("deckRowValueWarn")
            row.activated.connect(lambda d=drive: self._choose_drive(d))
            self.body.addWidget(row)
            self.drive_rows.append(row)
        other = DeckRow(tr("Choose another folder..."))
        other.activated.connect(self._browse)
        self.body.addWidget(other)
        self._default = self.drive_rows[0] if self.drive_rows else other

    def _render_review(self) -> None:
        root = self._root
        assert root is not None
        self.body.addWidget(deck_label(tr("Review"), role="title"))
        card = DeckCard()
        card.body.addWidget(deck_label(tr("Profile name"), role="rowTitle"))
        card.body.addWidget(self.name_edit)
        self.name_edit.show()
        for label, path in (
            (tr("Anomaly"), root / "Anomaly"),
            (tr("GAMMA"), root / "GAMMA"),
            (tr("Cache"), root / "cache"),
        ):
            card.body.addWidget(deck_label(f"{label}: {path}", role="caption", wrap=True))
        free = free_space_bytes(root)
        if free is not None:
            warn = free < _NEEDED_BYTES
            note = deck_label(
                tr("{free} free on this drive.", free=human_size(free))
                + (" " + tr("That may not be enough for a full install.") if warn else ""),
                role="body",
                wrap=True,
            )
            if warn:
                note.setObjectName("deckBodyWarn")
            card.body.addWidget(note)
        self.body.addWidget(card)
        self.create_button = deck_button(
            tr("Create profile"), role="primary", on_click=self._create
        )
        self.body.addWidget(self.create_button)
        self.body.addWidget(deck_button(tr("Change location"), on_click=self._back_to_drive))
        # The name is already filled in; creating is the likely next press.
        self._default = self.create_button

    def _render_done(self) -> None:
        self.body.addWidget(deck_label(tr("You're set up"), role="title"))
        self.body.addWidget(
            deck_label(
                tr(
                    "Profile '{name}' is ready. Next, install STALKER Anomaly, "
                    "GAMMA and the dependencies from the Install screen - "
                    "keep the Deck plugged in, it's a long download.",
                    name=self.name_edit.text().strip(),
                ),
                role="body",
                wrap=True,
            )
        )
        go = deck_button(
            tr("Go to Install"),
            role="primary",
            on_click=lambda: self.window.set_page("install"),
        )
        self.body.addWidget(go)
        self._default = go

    # -- flow -------------------------------------------------------------
    def refresh(self) -> None:
        if self._step == "done":
            # Coming back after a finished setup (Profile -> New profile)
            # starts a fresh one rather than showing the old success page.
            self._step = "drive"
            self._root = None
            self.name_edit.clear()
        if self._step == "drive":
            self._render()

    def _choose_drive(self, drive: Path) -> None:
        self._set_root(install_root_for(drive))

    def _browse(self) -> None:
        show_folder_picker(
            self.window,
            title=tr("Install location"),
            start=None,
            on_choose=lambda path: self._set_root(Path(path) / _INSTALL_DIR),
        )

    def _set_root(self, root: Path) -> None:
        self._root = root
        existing = [p.profile_name for p in self.window.settings.profiles]
        if not self.name_edit.text().strip():
            self.name_edit.setText(unique_name("GAMMA", existing))
        self._step = "review"
        self._render()

    def _back_to_drive(self) -> None:
        self._step = "drive"
        self._render()

    def on_back(self) -> bool:
        if self._task is not None:
            self.window.notify(tr("Creating profile..."))
            return True
        if self._step == "review":
            self._back_to_drive()
            return True
        if self.window.settings.profiles:
            # Opened from "New profile": B backs out of it. Only a true first
            # run, with nowhere else to go, falls through to the menu.
            self.window.set_page(self.window.start_screen())
            return True
        return False

    def _create(self) -> None:
        if self._task is not None or self._root is None:
            return
        name = self.name_edit.text().strip()
        existing = [p.profile_name for p in self.window.settings.profiles]
        if not name:
            self.window.notify(tr("A profile name is required."))
            return
        if name in existing:
            self.window.notify(tr("A profile named '{name}' already exists.", name=name))
            return
        profile = profile_for(self._root, name)
        for path in (profile.anomaly, profile.gamma, profile.cache):
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.window.notify(str(exc), 6000)
                return
        try:
            args = create_profile_args(profile)
        except ValueError as exc:
            self.window.notify(str(exc), 6000)
            return
        self.create_button.setEnabled(False)
        self.window.notify(tr("Creating profile..."), 0)
        self._task = BackgroundTask(run_config_command, args, timeout=300, parent=self)
        self._task.result.connect(lambda res: self._on_created(profile, *res))
        self._task.error.connect(self._on_error)
        self._task.start()

    def _on_created(self, profile: CliProfile, rc: int, out: str, err: str) -> None:
        self._task = None
        self.window.toast.hide()
        if not cli_ok(rc, out, err):
            self._on_error((out + "\n" + err).strip() or tr("The CLI did not create the profile."))
            return
        self.window.refresh_settings()
        if not any(p.profile_name == profile.profile_name for p in self.window.settings.profiles):
            self._on_error(tr("The CLI did not create the profile."))
            return
        try:
            seed_new_mo2_profile(profile.gamma, profile.mo2_profile)
        except OSError:
            pass  # best-effort, as on the desktop Profiles page
        self._step = "done"
        self._render()

    def _on_error(self, message: str) -> None:
        self._task = None
        self.window.toast.hide()
        if hasattr(self, "create_button"):
            self.create_button.setEnabled(True)
        self.window.notify(tr("Create Failed") + ": " + message, 8000)
