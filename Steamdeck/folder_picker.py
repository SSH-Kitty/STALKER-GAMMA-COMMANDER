"""A Deck-native replacement for QFileDialog.getExistingDirectory.

QFileDialog is a QDialog - a separate top-level window with no window
manager to place or size it under gamescope (the same reason DeckOverlay
exists). This lists real directories using the window's own overlay/
DeckPicker machinery instead. Final destination safety is NOT re-validated
here - commander_gui.ui.utilities_page._validate_move_destination already
owns that, checked once at the caller's confirm step, exactly as
QFileDialog.getExistingDirectory lets you pick anywhere and desktop's own
validator is the only gate.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from commander_gui.i18n import tr

from . import gamepad as pad
from .scale import px
from .widgets import DeckOverlay, DeckPicker, deck_label

#: Sentinel for the ".. (up)" entry, distinct from any real Path value.
_UP = object()

#: Where removable media actually mounts on a Deck (SD card, USB drives).
_MEDIA_ROOTS = (Path("/run/media"), Path("/media"))


def candidate_roots() -> list[tuple[str, Path]]:
    """Home, plus any real mounted external volume (SD card / USB)."""
    home = Path.home()
    roots: list[tuple[str, Path]] = [(tr("Home"), home)]
    seen = {home}
    for base in _MEDIA_ROOTS:
        if not base.is_dir():
            continue
        try:
            user_dirs = list(base.iterdir())
        except OSError:
            continue
        for user_dir in user_dirs:
            # Newer SteamOS mounts the SD card straight under /run/media
            # (/run/media/<label>) rather than per user
            # (/run/media/deck/<label>) - take the mount itself when so.
            try:
                if user_dir not in seen and os.path.ismount(user_dir):
                    roots.append((user_dir.name, user_dir))
                    seen.add(user_dir)
                    continue
            except OSError:
                pass
            try:
                if not user_dir.is_dir():
                    continue
                entries = list(user_dir.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry in seen:
                    continue
                try:
                    mounted = os.path.ismount(entry)
                except OSError:
                    mounted = False
                if not mounted:
                    continue
                roots.append((entry.name, entry))
                seen.add(entry)
    return roots


def list_subfolders(directory: Path) -> list[Path]:
    """Real, non-symlink subdirectories of ``directory``, sorted by name."""
    try:
        return sorted(
            (p for p in directory.iterdir() if p.is_dir() and not p.is_symlink()),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []


class DeckFolderPicker(QWidget):
    """Browses subfolders of one starting directory.

    Not itself a dialog - meant to be shown as a DeckOverlay's body, with
    the overlay's own buttons driving it.
    """

    def __init__(self, start: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current = start if start.is_dir() else Path.home()
        #: Where browsing began: B climbs back up to here, then closes.
        self._start = self._current
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(12))
        self.path_label = deck_label("", role="caption", wrap=True)
        layout.addWidget(self.path_label)
        self.picker = DeckPicker("", [])
        self.picker.chosen.connect(self._activate)
        layout.addWidget(self.picker, 1)
        # Typed into by the on-screen keyboard for "New folder"; never shown.
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText(tr("New folder name"))
        self.name_edit.hide()
        self._render()

    def _render(self, select: Path | None = None) -> None:
        self.path_label.setText(str(self._current))
        options: list[tuple[str, object]] = []
        if self._current.parent != self._current:
            options.append((tr(".. (up)"), _UP))
        options += [(f.name, f) for f in list_subfolders(self._current)]
        self.picker.set_options(options, select)

    def _activate(self, value: object) -> None:
        if value is _UP:
            self.go_up()
            return
        self._current = Path(value)
        self._render()

    def go_up(self) -> None:
        """One level up, landing on the folder just left."""
        came_from = self._current
        self._current = self._current.parent
        self._render(select=came_from)

    def back(self) -> bool:
        """B: up a level while below the starting folder; False at it."""
        if self._current == self._start or self._current.parent == self._current:
            return False
        self.go_up()
        return True

    def current(self) -> Path:
        return self._current

    def create_folder(self, name: str) -> Path | None:
        """Make ``name`` under the current folder and open it. None on failure."""
        name = name.strip()
        if not name or "/" in name or name in (".", ".."):
            return None
        target = self._current / name
        try:
            target.mkdir(parents=False, exist_ok=True)
        except OSError:
            return None
        self._current = target
        self._render()
        return target


def show_folder_picker(
    window,
    *,
    title: str,
    start: Path | None,
    on_choose: Callable[[Path], None],
    stacked: bool = False,
) -> None:
    """Root chooser (if no ``start`` given), then a folder browser.

    ``stacked`` opens it over whatever overlay is already up (the profile
    editor), which it returns to when chosen or cancelled.
    """

    def _browse(root: Path, *, replace_top: bool = False) -> None:
        if replace_top:
            # Swap the root chooser for the browser, keeping whatever was
            # underneath the chooser (the editor) on the stack.
            window.dismiss_overlay(refocus=False)
        body = DeckFolderPicker(Path(root))

        def _use() -> None:
            window.dismiss_overlay()
            on_choose(body.current())

        def _new_folder() -> None:
            body.name_edit.clear()

            def _made(name: str) -> None:
                if name.strip() and body.create_folder(name) is None:
                    window.notify(tr("Could not create that folder here."), 5000)

            window.open_keyboard(body.name_edit, title=tr("New folder name"), on_done=_made)

        overlay = DeckOverlay(
            title,
            body,
            [
                (tr("Use this folder"), _use, "primary"),
                (tr("New folder"), _new_folder, "normal"),
                (tr("Cancel"), window.dismiss_overlay, "normal"),
            ],
            panel_width=1100,
        )
        overlay.default_button = body.picker.list
        overlay.back_handler = body.back
        overlay.hints = (
            ("A", "Open"),
            ("X", "Use this folder"),
            ("Y", "New folder"),
            ("B", "Back"),
        )

        def _on_action(action: str) -> bool:
            if action == pad.CONTEXT:
                _use()
                return True
            if action == pad.SEARCH:
                _new_folder()
                return True
            return False

        overlay.action_handler = _on_action
        window.show_overlay(overlay, stacked=stacked)

    if start is not None:
        _browse(start)
        return

    roots = candidate_roots()
    picker = DeckPicker("", [(label, path) for label, path in roots])
    picker.chosen.connect(lambda root: _browse(root, replace_top=True))
    overlay = DeckOverlay(
        title,
        picker,
        [(tr("Cancel"), window.dismiss_overlay, "normal")],
        panel_width=1000,
    )
    overlay.default_button = picker.list
    window.show_overlay(overlay, stacked=stacked)
