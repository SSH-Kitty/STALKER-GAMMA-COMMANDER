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

from PySide6.QtWidgets import QVBoxLayout, QWidget

from commander_gui.i18n import tr

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
    the overlay's own Cancel/"Use this folder" buttons driving it.
    """

    def __init__(self, start: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current = start if start.is_dir() else Path.home()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.path_label = deck_label("", role="caption", wrap=True)
        layout.addWidget(self.path_label)
        self.picker = DeckPicker("", [])
        self.picker.chosen.connect(self._activate)
        layout.addWidget(self.picker, 1)
        self._render()

    def _render(self) -> None:
        self.path_label.setText(str(self._current))
        options: list[tuple[str, object]] = []
        if self._current.parent != self._current:
            options.append((tr(".. (up)"), _UP))
        options += [(f.name, f) for f in list_subfolders(self._current)]
        self.picker.set_options(options)

    def _activate(self, value: object) -> None:
        self._current = self._current.parent if value is _UP else Path(value)
        self._render()

    def current(self) -> Path:
        return self._current


def show_folder_picker(
    window,
    *,
    title: str,
    start: Path | None,
    on_choose: Callable[[Path], None],
) -> None:
    """Entry point: root chooser (if no ``start`` given), then a folder browser."""

    def _browse(root: Path) -> None:
        body = DeckFolderPicker(root)

        def _use() -> None:
            window.dismiss_overlay()
            on_choose(body.current())

        window.show_overlay(
            DeckOverlay(
                title,
                body,
                [
                    (tr("Cancel"), window.dismiss_overlay, "normal"),
                    (tr("Use this folder"), _use, "primary"),
                ],
                panel_width=1000,
            )
        )

    if start is not None:
        _browse(start)
        return

    roots = candidate_roots()
    picker = DeckPicker("", [(label, path) for label, path in roots])
    picker.chosen.connect(_browse)
    window.show_overlay(
        DeckOverlay(
            title,
            picker,
            [(tr("Cancel"), window.dismiss_overlay, "normal")],
            panel_width=1000,
        )
    )
