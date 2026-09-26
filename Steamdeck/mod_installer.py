"""Installing a mod archive from Deck Mode's Mods screen.

The same steps as the desktop Mod Manager's Install Mod, with Deck-native
controls in place of its dialogs:

1. pick an archive in a controller-friendly file browser (Downloads first),
2. confirm or rename the mod (the on-screen keyboard),
3. extract it, run its FOMOD installer if it has one (``fomod_panel``),
4. move it into ``gamma/mods`` and file it, disabled, under "Custom Mods"
   at the bottom of the list - the same place a desktop install lands.

The install lock is held from extraction to the move, so nothing else can
touch the install tree meanwhile; modlist.txt is written after the lock is
released, through the Mods screen's guarded writer.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QLineEdit, QScroller, QVBoxLayout, QWidget

from commander_gui.fomod import apply_options, parse_config
from commander_gui.i18n import tr
from commander_gui.integrity import invalidate_baseline
from commander_gui.mod_install import (
    ModInstallError,
    default_mod_name,
    extract_archive,
    move_payload,
    sanitize_name,
    write_basic_meta_ini,
)
from commander_gui.modlist import add_custom_mod, install_conflict, read_lines
from commander_gui.ui.common import BackgroundTask, StreamTask, mo2_running

from . import gamepad as pad
from .folder_picker import candidate_roots, list_subfolders
from .modlist_io import ModlistWriteBlocked, write_lines
from .scale import px
from .widgets import DeckOverlay, DeckPicker, DeckRow, DeckToggleRow, deck_label

#: What the file browser offers - the archive types the extractor handles.
ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar", ".fomod")

_UP = object()


def _is_hidden(path: Path) -> bool:
    return path.name.startswith(".")


def list_archives(directory: Path) -> list[Path]:
    """Mod archives directly in ``directory``, sorted by name."""
    try:
        return sorted(
            (
                p
                for p in directory.iterdir()
                if p.is_file() and not p.is_symlink() and p.suffix.lower() in ARCHIVE_SUFFIXES
            ),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []


def find_fomod(staging: Path) -> tuple[Path, Path] | None:
    """``(ModuleConfig.xml, archive root)`` for an extracted FOMOD, else None."""
    config = staging / "fomod" / "ModuleConfig.xml"
    if config.is_file():
        return config, staging
    candidates = list(staging.glob("*/fomod/ModuleConfig.xml"))
    if len(candidates) == 1:
        return candidates[0], candidates[0].parent.parent
    return None


class DeckArchivePicker(QWidget):
    """Folders first, then the archives in the current folder.

    An overlay body: choosing a folder opens it, choosing an archive calls
    ``on_archive``. Dot-folders and dot-files (``.cache``, ``.wine``...) stay
    out of the list until "Show hidden folders" is switched on - a home
    folder holds dozens of them and none is where a mod archive lands.
    """

    def __init__(self, start: Path, on_archive: Callable[[Path], None]) -> None:
        super().__init__()
        self._current = start if start.is_dir() else Path.home()
        #: Where browsing began (or "Other places" jumped to): B climbs back
        #: up to here, then closes.
        self._start = self._current
        self._on_archive = on_archive
        self._show_hidden = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(12))
        self.path_label = deck_label("", role="caption", wrap=True)
        layout.addWidget(self.path_label)
        self.hidden_toggle = DeckToggleRow(tr("Show hidden folders"), False)
        self.hidden_toggle.toggled.connect(self._on_hidden_toggled)
        layout.addWidget(self.hidden_toggle)
        self.picker = DeckPicker("", [])
        self.picker.chosen.connect(self._activate)
        layout.addWidget(self.picker, 1)
        self.render()

    def render(self, select: object = None) -> None:
        self.path_label.setText(str(self._current))
        options: list[tuple[str, object]] = []
        if self._current.parent != self._current:
            options.append((tr(".. (up)"), _UP))
        folders = list_subfolders(self._current)
        archives = list_archives(self._current)
        if not self._show_hidden:
            folders = [f for f in folders if not _is_hidden(f)]
            archives = [a for a in archives if not _is_hidden(a)]
        options += [("▸  " + f.name, f) for f in folders]
        options += [(a.name, a) for a in archives]
        if not archives and len(options) <= 1:
            options.append((tr("No mod archives here"), None))
        self.picker.set_options(options, select)
        if select is None:
            self.jump_to_top()

    def jump_to_top(self) -> None:
        """Highlight the first entry and scroll the list back to the top.

        A folder opens here, not wherever the last one was scrolled to: a
        flick still gliding when a row was tapped, or the old scroll offset
        surviving the reload, used to leave a new folder open mid-list.
        The scroll is repeated once the list has laid out its new rows,
        since that layout can move it again.
        """
        view = self.picker.list
        QScroller.scroller(view.viewport()).stop()
        if view.count():
            view.setCurrentRow(0)
        view.scrollToTop()
        QTimer.singleShot(0, self._scroll_to_top_later)

    def _scroll_to_top_later(self) -> None:
        view = getattr(self.picker, "list", None)
        if view is not None and view.currentRow() == 0:
            view.scrollToTop()

    def _on_hidden_toggled(self, checked: bool) -> None:
        self._show_hidden = checked
        # Stay on the highlighted entry; it is still listed unless it was
        # a hidden one just filtered out.
        item = self.picker.list.currentItem()
        self.render(select=item.data(Qt.ItemDataRole.UserRole) if item else None)

    def open(self, folder: Path, *, as_start: bool = False) -> None:
        self._current = folder
        if as_start:
            self._start = folder
        self.render()

    def go_up(self) -> None:
        """One level up, landing on the folder just left."""
        came_from = self._current
        self._current = self._current.parent
        self.render(select=came_from)

    def back(self) -> bool:
        """B: up a level while below the starting folder; False at it."""
        if self._current == self._start or self._current.parent == self._current:
            return False
        self.go_up()
        return True

    def _activate(self, value: object) -> None:
        if value is None:
            return
        if value is _UP:
            self.go_up()
            return
        path = Path(value)
        if path.is_dir():
            self.open(path)
        else:
            self._on_archive(path)


class DeckModInstaller(QObject):
    """One install at a time, started from the Mods screen."""

    def __init__(self, screen) -> None:
        super().__init__(screen)
        self.screen = screen
        self.window = screen.window
        self._staging_root: Path | None = None
        self._task: QObject | None = None
        self._status = None
        self._name = ""
        self._archive: Path | None = None
        self._progress_overlay = None
        self._progress_hidden = False

    # -- 1. pick ----------------------------------------------------------------
    def start(self) -> None:
        reason = self._blocked()
        if reason:
            self.window.notify(reason, 6000)
            return
        downloads = Path.home() / "Downloads"
        body = DeckArchivePicker(
            downloads if downloads.is_dir() else Path.home(), self._chosen
        )

        def _places() -> None:
            roots = [(tr("Downloads"), downloads)] if downloads.is_dir() else []
            roots += candidate_roots()
            picker = DeckPicker("", roots)

            def _go(path: object) -> None:
                self.window.dismiss_overlay(refocus=False)
                body.open(Path(path), as_start=True)
                body.picker.list.setFocus(Qt.FocusReason.OtherFocusReason)

            picker.chosen.connect(_go)
            overlay = DeckOverlay(
                tr("Other places"),
                picker,
                [(tr("Cancel"), self.window.dismiss_overlay, "normal")],
                panel_width=900,
            )
            overlay.default_button = picker.list
            self.window.show_overlay(overlay, stacked=True)

        overlay = DeckOverlay(
            tr("Install mod"),
            body,
            [
                (tr("Other places"), _places, "normal"),
                (tr("Cancel"), self.window.dismiss_overlay, "normal"),
            ],
            panel_width=1100,
        )
        overlay.default_button = body.picker.list
        overlay.back_handler = body.back
        overlay.hints = (
            ("A", "Open"),
            ("X", "Other places"),
            ("Y", "Top"),
            ("B", "Back"),
        )

        def _on_action(action: str) -> bool:
            if action == pad.CONTEXT:
                _places()
                return True
            if action == pad.SEARCH:
                body.jump_to_top()
                body.picker.list.setFocus(Qt.FocusReason.OtherFocusReason)
                return True
            return False

        overlay.action_handler = _on_action
        self.window.show_overlay(overlay)

    def _blocked(self) -> str | None:
        if self.window.install_busy:
            return tr("An install is already running.")
        if mo2_running(force=True):
            return tr("Close Mod Organizer before installing a mod.")
        if self.screen.profile() is None or self.screen._path is None:
            return tr("Create or activate a profile first (Dashboard → Profiles).")
        return None

    # -- 2. name ----------------------------------------------------------------
    def _chosen(self, archive: Path) -> None:
        try:
            name = default_mod_name(archive)
        except ModInstallError as exc:
            self.window.notify(str(exc), 6000)
            return
        self._archive = archive
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(12))
        layout.addWidget(deck_label(archive.name, role="caption", wrap=True))
        edit = QLineEdit(name)
        edit.hide()
        row = DeckRow(tr("MO2 mod name:"), name)
        layout.addWidget(row)
        layout.addWidget(
            deck_label(
                tr("It is added disabled, at the bottom of the list under Custom Mods."),
                role="caption",
                wrap=True,
            )
        )
        layout.addWidget(edit)

        def _rename() -> None:
            self.window.open_keyboard(
                edit,
                title=tr("MO2 mod name:"),
                on_done=lambda text: row.set_value(text.strip() or name),
            )

        row.activated.connect(_rename)

        def _install() -> None:
            chosen = edit.text().strip() or name
            self.window.dismiss_overlay()
            self._confirm_target(chosen)

        overlay = DeckOverlay(
            tr("Install mod"),
            body,
            [
                (tr("Install"), _install, "primary"),
                (tr("Cancel"), self.window.dismiss_overlay, "normal"),
            ],
            panel_width=900,
        )
        self.window.show_overlay(overlay)

    def _confirm_target(self, name: str) -> None:
        try:
            safe = sanitize_name(name)
        except ModInstallError as exc:
            self.window.notify(str(exc), 6000)
            return
        reason = self._blocked()
        if reason:
            self.window.notify(reason, 6000)
            return
        mods_dir = Path(self.screen.profile().gamma) / "mods"
        if mods_dir.is_symlink():
            self.window.notify(tr("The GAMMA mods directory cannot be a symlink."), 6000)
            return
        conflict = install_conflict(self.screen._lines, mods_dir, safe)
        if conflict == "listed":
            self.window.notify(
                tr(
                    "'{name}' is already in the modlist. Enable it in the list, or delete it from the list first if you want to reinstall it.",
                    name=safe,
                ),
                8000,
            )
            return
        if conflict == "leftover":

            def _replace() -> None:
                try:
                    shutil.rmtree(mods_dir / safe)
                except OSError as exc:
                    self.window.notify(str(exc), 6000)
                    return
                self._extract(safe)

            self.window.confirm(
                tr("Replace existing folder"),
                tr(
                    "A leftover folder '{name}' exists in the mods folder but it is not in your modlist.\n\nReplace it with the new install? The old folder will be permanently deleted.",
                    name=safe,
                ),
                _replace,
                confirm_text=tr("Replace"),
                confirm_role="danger",
            )
            return
        self._extract(safe)

    # -- 3. extract / FOMOD -------------------------------------------------------
    def _show_progress(self, text: str) -> None:
        if self._progress_hidden:
            # B put it away: the install carries on quietly and says how it
            # went with a notification, rather than popping back up over
            # whatever the user has moved on to.
            return
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        self._status = deck_label(text, role="body", wrap=True)
        layout.addWidget(self._status)
        overlay = DeckOverlay(
            tr("Installing {name}...", name=self._name),
            body,
            [(tr("Cancel"), self._cancel, "normal")],
            panel_width=900,
        )
        self._progress_overlay = overlay
        overlay.closed.connect(lambda o=overlay: self._on_progress_closed(o))
        self.window.show_overlay(overlay)

    def _on_progress_closed(self, overlay) -> None:
        # Still ours: nobody took it down through _close_progress(), so the
        # user did (B, or another panel replaced it).
        if self._progress_overlay is overlay:
            self._progress_overlay = None
            self._progress_hidden = True

    def _close_progress(self, *, refocus: bool = True) -> None:
        """Take down this installer's own progress panel - and only that.

        B can close it while the install carries on; by the time the
        install ends another panel may be open, which must stay.
        """
        overlay = getattr(self, "_progress_overlay", None)
        self._progress_overlay = None
        if overlay is not None and self.window.current_overlay() is overlay:
            self.window.dismiss_overlay(refocus=refocus)

    def _set_status(self, text: str) -> None:
        if self._status is not None:
            try:
                self._status.setText(text)
            except RuntimeError:  # the overlay is already gone
                self._status = None

    def _extract(self, name: str) -> None:
        archive = self._archive
        if archive is None:
            return
        self._name = name
        self._progress_hidden = False
        self.window.set_install_busy(True, "mod_install")
        self._staging_root = Path(tempfile.mkdtemp(prefix="gamma-mod-install-"))
        staging = self._staging_root / "archive"
        self._show_progress(tr("Extracting {name}...", name=archive.name))
        task: StreamTask | None = None

        def worker(report):
            extract_archive(
                archive,
                staging,
                task.cancel_event,
                lambda percent, text: report(
                    tr("Extracting {name}...", name=archive.name)
                    + (f" {percent}%" if percent is not None else "")
                ),
            )
            return str(staging)

        task = StreamTask(worker, parent=self)
        task.line.connect(self._set_status)
        task.result.connect(lambda path: self._extracted(Path(path)))
        task.error.connect(self._fail)
        self._task = task
        task.start()

    def _extracted(self, staging: Path) -> None:
        self._task = None
        fomod = find_fomod(staging)
        if fomod is None:
            self._finalize(staging, None)
            return
        config_path, root = fomod
        try:
            config = parse_config(config_path)
        except ModInstallError as exc:
            self._fail(str(exc))
            return
        from .fomod_panel import show_fomod

        # The progress overlay gives way to the installer, and comes back
        # once there is copying to wait for.
        self._close_progress(refocus=False)
        show_fomod(
            self.window,
            config,
            root,
            on_install=lambda selections: self._finalize(root, (config, selections)),
            on_cancel=lambda: self._fail(tr("Mod installation cancelled")),
        )

    # -- 4. install -----------------------------------------------------------------
    def _finalize(self, source: Path, fomod) -> None:
        profile = self.screen.profile()
        if profile is None or self._staging_root is None:
            self._fail(tr("No active profile"))
            return
        destination = Path(profile.gamma) / "mods" / self._name
        selected = self._staging_root / "selected"
        archive_name = self._archive.name if self._archive is not None else ""
        self._show_progress(tr("Installing {name}...", name=self._name))

        def worker():
            payload = source
            if fomod is not None:
                config, selections = fomod
                apply_options(config, source, selected, selections)
                payload = selected
            move_payload(payload, destination, task.cancel_event)
            write_basic_meta_ini(destination, archive_name)
            return str(destination)

        task = BackgroundTask(worker, parent=self)
        task.result.connect(lambda path: self._installed(Path(path)))
        task.error.connect(self._fail)
        self._task = task
        task.start()

    def _installed(self, destination: Path) -> None:
        self._task = None
        self._cleanup()
        self._close_progress()
        path = self.screen._path
        try:
            lines = read_lines(path)
            new_lines = add_custom_mod(lines, destination.name, enabled=False)
            write_lines(self.window, path, new_lines, snapshot=True)
        except (OSError, ValueError, ModlistWriteBlocked) as exc:
            shutil.rmtree(destination, ignore_errors=True)
            self.window.notify(tr("Mod installation failed") + ": " + str(exc), 8000)
            self.screen.refresh()
            return
        try:
            invalidate_baseline(self.screen.profile().gamma)
        except (OSError, AttributeError):
            pass
        self.window.update_mod_counter()
        self.screen.refresh()
        self.screen.reveal_mod(destination.name)
        self.window.notify(
            tr(
                "Installed '{name}' - added disabled to Custom Mods, at the bottom of the list.",
                name=destination.name,
            ),
            8000,
        )

    # -- ending early --------------------------------------------------------------
    def _cancel(self) -> None:
        if self._task is not None and hasattr(self._task, "cancel"):
            self._task.cancel()

    def _fail(self, message: str) -> None:
        self._task = None
        self._cleanup()
        self._close_progress()
        self.window.notify(tr("Mod installation failed") + ": " + message, 8000)

    def _cleanup(self) -> None:
        if self._staging_root is not None:
            shutil.rmtree(self._staging_root, ignore_errors=True)
            self._staging_root = None
        self._status = None
        if self.window.install_operation == "mod_install":
            self.window.set_install_busy(False)
        # Back to the mod list - but only if the user is still looking at
        # it. Dismissing the progress panel and moving on (another tab, a
        # picker) must not have focus yanked back here when the job ends.
        focus = getattr(self.screen, "list", None)
        if (
            focus is not None
            and self.window.current_page() is self.screen
            and self.window.current_overlay() is None
        ):
            focus.setFocus(Qt.FocusReason.OtherFocusReason)
