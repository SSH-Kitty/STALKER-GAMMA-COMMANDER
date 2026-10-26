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
import zipfile
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QLineEdit, QScroller, QVBoxLayout, QWidget

from commander_gui.coop import looks_like_xrrazom
from commander_gui.fomod import apply_options, find_installer
from commander_gui.i18n import tr
from commander_gui.integrity import invalidate_baseline
from commander_gui.launcher import select_mo2_profile
from commander_gui.mod_install import (
    ModInstallError,
    _list_archive_paths,
    default_mod_name,
    drop_romeo_prefix,
    extract_archive,
    find_archiver,
    make_staging_dir,
    move_payload,
    sanitize_name,
    write_basic_meta_ini,
)
from commander_gui.modlist import add_custom_mod, install_conflict, read_lines
from commander_gui.romeo import (
    KEPT_MCM,
    MCM_MOD,
    find_romeo,
    is_romeo_profile,
    mcm_changed,
    old_mods,
    unpacked_size,
)
from commander_gui.romeo import install as install_romeo
from commander_gui.ui.common import (
    BackgroundTask,
    StreamTask,
    gamma_installed,
    mo2_running,
    notice,
)

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


def list_archives(directory: Path, suffixes: tuple[str, ...] = ARCHIVE_SUFFIXES) -> list[Path]:
    """Files with one of ``suffixes`` (mod archives by default) directly in
    ``directory``, sorted by name."""
    try:
        return sorted(
            (
                p
                for p in directory.iterdir()
                if p.is_file() and not p.is_symlink() and p.suffix.lower() in suffixes
            ),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []


class DeckArchivePicker(QWidget):
    """Folders first, then the archives in the current folder.

    An overlay body: choosing a folder opens it, choosing an archive calls
    ``on_archive``. Dot-folders and dot-files (``.cache``, ``.wine``...) stay
    out of the list until "Show hidden folders" is switched on - a home
    folder holds dozens of them and none is where a mod archive lands.
    """

    def __init__(
        self, start: Path, on_archive: Callable[[Path], None],
        suffixes: tuple[str, ...] = ARCHIVE_SUFFIXES, empty: str | None = None,
    ) -> None:
        super().__init__()
        self._suffixes = suffixes
        self._empty = empty
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
        archives = list_archives(self._current, self._suffixes)
        if not self._show_hidden:
            folders = [f for f in folders if not _is_hidden(f)]
            archives = [a for a in archives if not _is_hidden(a)]
        options += [("▸  " + f.name, f) for f in folders]
        options += [(a.name, a) for a in archives]
        if not archives and len(options) <= 1:
            options.append((self._empty or tr("No mod archives here"), None))
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


def pick_file(window, title: str, start: Path, suffixes: tuple[str, ...], on_file: Callable[[Path], None]) -> None:
    """A file browser overlay for one kind of file (``suffixes``)."""

    def _chosen(path: Path) -> None:
        window.dismiss_overlay()
        on_file(path)

    body = DeckArchivePicker(start, _chosen, suffixes, tr("No matching files here"))
    overlay = DeckOverlay(title, body, [(tr("Cancel"), window.dismiss_overlay, "normal")], panel_width=1100)
    overlay.default_button = body.picker.list
    overlay.back_handler = body.back
    window.show_overlay(overlay)


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
        if self._offer_romeo(archive) or self._refuse_xrrazom(archive):
            return
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
        # "xx-" is kept only on a ROMEO profile, where it marks ROMEO's mods.
        profile = self.screen.profile()
        if profile is None or not is_romeo_profile(Path(profile.gamma), profile.mo2_profile):
            name = drop_romeo_prefix(name)
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
                # install-busy/MO2 state may have changed while the confirm
                # dialog was open - never delete a folder out from under a
                # concurrent install or a running game.
                reason = self._blocked()
                if reason:
                    self.window.notify(reason, 6000)
                    return
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
                self._status.setText(notice(text))
            except RuntimeError:  # the overlay is already gone
                self._status = None

    def _extract(self, name: str) -> None:
        archive = self._archive
        if archive is None:
            return
        self._name = name
        self._progress_hidden = False
        self.window.set_install_busy(True, "mod_install")
        self._staging_root = make_staging_dir(self.screen.profile().gamma)
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
        archive_name = self._archive.name if self._archive is not None else ""
        try:
            installer = find_installer(staging, archive_name)
        except ModInstallError as exc:
            self._fail(str(exc))
            return
        if installer is None:
            self._finalize(staging, None)
            return
        config, root = installer
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
        except ModlistWriteBlocked as exc:
            # The mod itself installed fine; only the list edit was refused
            # (MO2 opened meanwhile). Keep it: MO2 lists new folders itself.
            self.window.notify(
                tr(
                    "Installed '{name}', but the modlist was not changed: {reason}",
                    name=destination.name,
                    reason=str(exc),
                ),
                8000,
            )
            self.screen.refresh()
            return
        except (OSError, ValueError) as exc:
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

    # -- archives that are more than a mod ----------------------------------------------
    def _refuse_xrrazom(self, archive: Path) -> bool:
        """xrRazom Co-op's engine goes in the Anomaly folder, so only the
        Multiplayer screen installs it. True if refused. Zips only, the one
        format that screen takes: their listing is instant."""
        if archive.suffix.lower() != ".zip":
            return False
        try:
            with zipfile.ZipFile(archive) as opened:
                names = opened.namelist()
        except (OSError, zipfile.BadZipFile):
            return False  # extraction reports the real problem
        if not looks_like_xrrazom(names):
            return False

        def _open() -> None:
            self.window.dismiss_overlay()
            self.window.set_page("multiplayer")

        self.window.confirm(
            tr("Install xrRAZOM from the Multiplayer page"),
            tr("This is xrRAZOM Co-op. It can't be installed as a mod, because the Multiplayer page also sets up the game files co-op needs."),
            _open,
            confirm_text=tr("Open Multiplayer"),
        )
        return True

    def _offer_romeo(self, archive: Path) -> bool:
        """CLIKRΔD's ROMEO repack: ~170 mods, an MO2 profile and Anomaly
        files, installed the way its guide says (the desktop Mod Manager's
        _install_romeo). True if the archive was ROMEO."""
        try:
            archiver = find_archiver()
            found = find_romeo(_list_archive_paths(archiver, archive))
        except ModInstallError:
            return False  # extraction reports the real problem
        if found is None:
            return False
        wrapper, mo2_profile = found
        profile = self.screen.profile()
        if profile is None or not str(profile.anomaly).strip() or not gamma_installed(profile.gamma):
            self.window.notify(tr("ROMEO goes on top of GAMMA. Install GAMMA for this profile, then install ROMEO."), 8000)
            return True
        gamma, anomaly = Path(profile.gamma), Path(profile.anomaly)
        need = unpacked_size(archiver, archive)
        free = shutil.disk_usage(gamma).free
        if need and need > free:
            self.window.notify(
                tr(
                    "ROMEO needs {need} GB free on the GAMMA folder's drive, but only {free} GB is free.",
                    need=round(need / 1e9), free=round(free / 1e9),
                ),
                8000,
            )
            return True
        requirements = tr("ROMEO needs GAMMA 0.9.5 and modded exes from 2026.9.12 or newer. Unpacking takes a while.")
        update = (gamma / "profiles" / mo2_profile).is_dir()

        def _go(keep_mcm: bool) -> None:
            self.window.dismiss_overlay()
            self._install_romeo(archive, wrapper, mo2_profile, gamma, anomaly, keep_mcm, update)

        if not update:
            self.window.confirm(
                tr("Install ROMEO?"),
                tr("This is CLIKRΔD's ROMEO repack. COMMANDER adds its mods and its own Mod Organizer profile to GAMMA. Your G.A.M.M.A mod list isn't changed.")
                + "\n\n" + tr("Both profiles share the Anomaly folder, so as ROMEO's guide asks, GAMMA's Atmos weather files and the shader cache are removed there. A GAMMA update puts the Atmos files back for G.A.M.M.A.")
                + "\n\n" + requirements,
                lambda: _go(False),
                confirm_text=tr("Install ROMEO"),
            )
            return True

        def _confirm_update(keep_mcm: bool) -> None:
            self.window.dismiss_overlay(refocus=False)
            message = tr("COMMANDER removes the {count} old xx- mods, as ROMEO's guide says, then installs the new version.", count=len(old_mods(gamma)))
            if keep_mcm:
                message += "\n\n" + tr("Your MCM settings are kept as '{name}', turned on in place of ROMEO's.", name=KEPT_MCM)
            self.window.confirm(
                tr("Update ROMEO?"), message + "\n\n" + requirements,
                lambda: _go(keep_mcm), confirm_text=tr("Update ROMEO"),
            )

        changed = mcm_changed(gamma, mo2_profile)
        if changed is None and (gamma / "mods" / MCM_MOD).is_dir():
            # Installed by hand: no record of ROMEO's own settings to compare
            # with, so ask. Cancel never silently drops the player's settings.
            self.window.show_overlay(
                DeckOverlay(
                    tr("Keep your MCM settings?"),
                    deck_label(
                        tr("Updating replaces ROMEO's MCM settings. If you changed any MCM settings, COMMANDER can keep yours and turn ROMEO's new ones off."),
                        role="body", wrap=True,
                    ),
                    [
                        (tr("Keep mine"), lambda: _confirm_update(True), "primary"),
                        (tr("Use ROMEO's new ones"), lambda: _confirm_update(False), "normal"),
                        (tr("Cancel"), self.window.dismiss_overlay, "normal"),
                    ],
                    panel_width=1000,
                )
            )
        else:
            _confirm_update(bool(changed))
        return True

    def _install_romeo(
        self, archive: Path, wrapper: str, mo2_profile: str, gamma: Path, anomaly: Path,
        keep_mcm: bool, update: bool,
    ) -> None:
        reason = self._blocked()
        if reason:
            self.window.notify(reason, 6000)
            return
        self._archive = archive
        self._name = mo2_profile
        self._progress_hidden = False
        self.window.set_install_busy(True, "mod_install")
        self._staging_root = make_staging_dir(gamma)
        staging = self._staging_root / "archive"
        self._show_progress(tr("Extracting {name}", name=archive.name))
        task: StreamTask | None = None

        def worker(report):
            extract_archive(
                archive, staging, task.cancel_event,
                lambda percent, text: report(
                    tr("Extracting {name}", name=archive.name) + (f" {percent}%" if percent is not None else "")
                ),
            )
            install_romeo(
                staging, wrapper, mo2_profile, gamma, anomaly, task.cancel_event,
                lambda done, total: report(tr("Installing ROMEO mods {done}/{total}", done=done, total=total)),
                keep_mcm,
            )
            return mo2_profile

        task = StreamTask(worker, parent=self)
        task.line.connect(self._set_status)
        task.result.connect(lambda name: self._romeo_installed(str(name), update))
        task.error.connect(self._fail)
        self._task = task
        task.start()

    def _romeo_installed(self, name: str, update: bool) -> None:
        self._task = None
        self._cleanup()
        self._close_progress()
        try:
            invalidate_baseline(self.screen.profile().gamma)
        except (OSError, AttributeError):
            pass
        if update:
            self.window.announce_finished(
                tr("ROMEO updated"),
                tr("The new version of ROMEO is installed.") + "\n\n"
                + tr("Your previous mod list is saved as modlist.txt.before-update in the ROMEO profile folder."),
            )
            return
        checklist = "\n\n".join((
            tr("Two things to do once in the game:"),
            "•  " + tr("Start a new save. ROMEO doesn't work with old saves."),
            "•  " + tr("Open the console (~) and run: cfg_load atmos_neutral"),
        ))
        profile = self.screen.profile()
        if profile is None or profile.mo2_profile == name:
            self.window.show_overlay(DeckOverlay(
                tr("Finish setting up ROMEO in the game"), deck_label(checklist, role="body", wrap=True),
                [(tr("OK"), self.window.dismiss_overlay, "primary")], panel_width=1000,
            ))
            return

        def _switch() -> None:
            self.window.dismiss_overlay()
            rc, out = select_mo2_profile(profile.gamma, name)
            if rc != 0:
                self.window.notify(tr("Couldn't change the MO2 profile") + ": " + out.strip(), 8000)
                return
            profile.mo2_profile = name
            self.window.settings.save()
            self.screen.refresh()
            self.window.update_mod_counter()

        self.window.show_overlay(DeckOverlay(
            tr("Switch to ROMEO?"),
            deck_label(
                tr("ROMEO is installed as the Mod Organizer profile '{name}'. Switch this COMMANDER profile to it now?", name=name)
                + "\n\n" + tr("You can switch back to G.A.M.M.A on the Dashboard at any time.")
                + "\n\n" + checklist,
                role="body", wrap=True,
            ),
            [(tr("Switch to ROMEO"), _switch, "primary"), (tr("Not now"), self.window.dismiss_overlay, "normal")],
            panel_width=1000,
        ))

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
