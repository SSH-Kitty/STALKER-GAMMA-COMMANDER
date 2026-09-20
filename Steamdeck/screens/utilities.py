"""Recovery and maintenance tools, ported from the desktop Utilities page.

Everything here reuses the desktop's own Python helpers for the actual work
(deletion, moving folders, prefix repair, log collection) - this module only
supplies the Deck-native confirm/run/report shell around them. Two things on
the desktop page are deliberately not here: the ASSISTANT launcher (a
separate desktop GUI application, not something a gamepad session opens),
and the destructive resets' second "cache preflight" dialog (a detailed
table of archive counts - simplified to one plain confirm, matching the
declutter every other Deck screen in this overhaul already went through).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QVBoxLayout, QWidget

from commander_gui.cli_runner import cli_command
from commander_gui.gui_settings import configured_runner
from commander_gui.i18n import tr
from commander_gui.launcher import LaunchError
from commander_gui.log_dump import create_log_dump
from commander_gui.parsers import parse_prune_archive, strip_ansi
from commander_gui.repair import foreign_prefix_dlls, repair_prefix_foreign_dlls
from commander_gui.ui.common import (
    BackgroundTask,
    CommandRunner,
    StreamTask,
    mo2_running,
)
from commander_gui.ui.utilities_page import (
    _move_folders,
    _resolved_wipe_target,
    _rewrite_mo2_ini_paths,
    _save_moved_profile,
    _validate_move_destination,
    _validate_wipe_paths,
    _wipe_folders,
)

from ..folder_picker import show_folder_picker
from ..widgets import (
    DeckCard,
    DeckOverlay,
    DeckProgress,
    DeckRow,
    deck_button,
    deck_label,
)
from .base import DeckScreen


def _tool_row(title, description, on_run, *, role="normal"):
    """A title + wrapped description + a real, focusable Run button.

    Not a DeckRow: descriptions here run long enough to need wrapping past
    DeckRow's fixed ROW_H, and each of these needs its own click target
    rather than the whole row being one "tap to pick" control.
    """
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    layout.addWidget(deck_label(title, role="rowTitle"))
    layout.addWidget(deck_label(description, role="caption", wrap=True))
    button = deck_button(tr("Run"), role=role, on_click=on_run)
    layout.addWidget(button)
    return box, button


class UtilitiesScreen(DeckScreen):
    def build(self) -> None:
        self._runner: CommandRunner | None = None
        self._wipe_task: StreamTask | None = None
        self._move_task: StreamTask | None = None
        self._log_dump_task: StreamTask | None = None
        self._repair_task: BackgroundTask | None = None
        self._action_buttons: list = []
        self._prune_mb = 0
        self._move_dest: Path | None = None
        self._move_sources: list[tuple[str, str]] = []
        self._move_profile_name = ""
        self._reset_includes_anomaly = True
        self._wipe_targets: tuple[str, str] = ("", "")
        self._full_uninstall_targets: tuple[str, str, str] = ("", "", "")

        self.status = deck_label("", role="caption", wrap=True)
        self.body.addWidget(self.status)

        self.progress = DeckProgress()
        self.progress.hide()
        self.body.addWidget(self.progress)

        self.body.addWidget(self._tools_card())
        self.body.addWidget(self._destructive_section())
        self.body.addStretch(1)

    # -- layout -------------------------------------------------------------
    def _tools_card(self) -> DeckCard:
        card = DeckCard()
        card.body.addWidget(deck_label(tr("Tools"), role="rowTitle"))

        def add(title, description, on_run, *, role="normal"):
            box, button = _tool_row(title, description, on_run, role=role)
            card.body.addWidget(box)
            self._action_buttons.append(button)

        add(
            tr("Preview cache cleanup"),
            tr(
                "List out-of-date addon archives in the cache with the total "
                "size that can be reclaimed."
            ),
            self._prune_check,
        )
        add(
            tr("Clean the download cache"),
            tr("Permanently delete out-of-date addon archives from the cache."),
            self._prune_apply,
        )
        add(
            tr("Clear shader cache"),
            tr("Delete the shader cache for the active Anomaly profile."),
            self._purge_shader_cache,
        )
        add(
            tr("Remove ReShade"),
            tr("Remove all ReShade-related files from the Anomaly bin directory."),
            self._delete_reshade,
        )
        add(
            tr("Fix GOG installation"),
            tr("Fix the ModOrganizer.ini paths for a GOG-provided install."),
            self._gog_fix,
        )
        add(
            tr("Repair Wine prefix"),
            tr(
                "Restore the runner's own system DLLs if another Wine has "
                "written into the game's prefix. Use this when every launch "
                "crashes immediately, then reinstall the dependencies."
            ),
            self._repair_prefix,
            role="danger",
        )
        add(
            tr("Move Installation"),
            tr(
                "Move the Anomaly, GAMMA, and cache folders to another drive. "
                "Files are copied and checked before the originals are removed."
            ),
            self._open_move_overlay,
        )
        add(
            tr("Create Log Dump"),
            tr(
                "Collect COMMANDER, Anomaly, GAMMA/MO2 and Wine-prefix logs "
                "plus crash dumps into one zip archive."
            ),
            self._start_log_dump,
        )
        return card

    def _destructive_section(self) -> DeckCard:
        card = DeckCard()
        card.body.addWidget(deck_label(tr("Reset or uninstall"), role="rowTitle"))
        card.body.addWidget(
            deck_label(
                tr(
                    "All reset and uninstall actions refuse to operate on "
                    "system paths, home directories or symlinks, and "
                    "re-check that the profile still points where it did "
                    "before deleting anything."
                ),
                role="caption",
                wrap=True,
            )
        )

        box, self.fresh_reset_button = _tool_row(
            tr("Fresh Reset"),
            tr(
                "Deletes the Anomaly and GAMMA folders, then reinstalls "
                "both from scratch into the same locations."
            ),
            self._start_fresh_reset,
            role="danger",
        )
        card.body.addWidget(box)

        box, self.gamma_reset_button = _tool_row(
            tr("GAMMA Reset"),
            tr(
                "Deletes the GAMMA folder and reinstalls GAMMA while "
                "preserving the existing Anomaly installation. Uses the "
                "Preserve toggles on the Install screen."
            ),
            self._start_gamma_reset,
            role="danger",
        )
        card.body.addWidget(box)

        box, self.full_uninstall_button = _tool_row(
            tr("Full Uninstall"),
            tr(
                "Removes the Anomaly, GAMMA, and cache folders, leaving "
                "your Wine/Proton prefix intact."
            ),
            self._start_full_uninstall,
            role="danger",
        )
        card.body.addWidget(box)
        return card

    # -- state ----------------------------------------------------------
    def refresh(self) -> None:
        self._update_destructive_enabled()

    def on_busy_changed(self, busy: bool) -> None:
        self._set_actions_enabled(not busy)

    def on_back(self) -> bool:
        # Mirrors InstallScreen.on_back(): a running job must not be
        # abandoned by a stray B press.
        if not self._tasks_idle():
            self.window.notify(tr("A task is already running."))
            return True
        return False

    def _tasks_idle(self) -> bool:
        return (
            self._runner is None
            and self._wipe_task is None
            and self._move_task is None
            and self._log_dump_task is None
            and self._repair_task is None
        )

    def _reject_if_mo2_running(self) -> bool:
        if mo2_running(force=True):
            self.window.notify(
                tr("Mod Organizer / the game is currently running. Close it first.")
            )
            return True
        return False

    def _set_actions_enabled(self, enabled: bool) -> None:
        idle = enabled and self._tasks_idle() and not self.window.install_busy
        for button in self._action_buttons:
            button.setEnabled(idle)
        self._update_destructive_enabled()

    def _update_destructive_enabled(self) -> None:
        profile = self.profile()
        idle = self._tasks_idle() and not self.window.install_busy
        anomaly_present = profile is not None and _resolved_wipe_target(profile.anomaly) is not None
        gamma_present = profile is not None and _resolved_wipe_target(profile.gamma) is not None
        cache_present = profile is not None and _resolved_wipe_target(profile.cache) is not None
        self.fresh_reset_button.setEnabled(idle and (anomaly_present or gamma_present))
        self.gamma_reset_button.setEnabled(idle and gamma_present)
        self.full_uninstall_button.setEnabled(
            idle and (anomaly_present or gamma_present or cache_present)
        )

    # -- simple CLI-backed tools --------------------------------------------
    def _run_cli(
        self,
        args: list[str],
        *,
        confirm: str | None = None,
        confirm_title: str = "",
        confirm_role: str = "primary",
        guard_mo2: bool = False,
        handler=None,
    ) -> None:
        if self.profile() is None:
            self.window.notify(tr("Create or activate a profile first (Profiles page)."))
            return
        if not self._tasks_idle() or self.window.install_busy:
            self.window.notify(tr("Another task is already running."))
            return

        def _start() -> None:
            # install_busy/mo2 may have changed while the confirm was open.
            if self.window.install_busy:
                self.window.notify(tr("Another task is already running."))
                return
            if guard_mo2 and self._reject_if_mo2_running():
                return
            self._begin_cli_run(args, handler=handler)

        if confirm is not None:
            self.window.confirm(confirm_title, confirm, _start, confirm_role=confirm_role)
        else:
            _start()

    def _begin_cli_run(self, args: list[str], *, handler=None) -> None:
        self.window.set_install_busy(True)
        self.status.setText("")
        self.progress.reset()
        self.progress.show()
        self._set_actions_enabled(False)
        runner = CommandRunner(cli_command(args), parent=self)
        self._runner = runner
        runner.line.connect(handler or self.progress.on_line)
        runner.finished.connect(self._on_cli_finished)
        runner.cancelled.connect(self.progress.on_cancelled)
        self.progress.set_runner(runner)
        self.progress.on_started()
        runner.start()

    def _on_cli_finished(self, rc: int, _output: str) -> None:
        self.progress.on_finished(rc, "")
        self._runner = None
        self.window.set_install_busy(False)
        self._set_actions_enabled(True)
        self.status.setText(tr("Done") if rc == 0 else tr("Failed (exit {rc})", rc=rc))

    def _prune_handler(self, line: str) -> None:
        self.progress.on_line(line)
        clean = strip_ansi(line)
        archive = parse_prune_archive(clean)
        if archive is not None:
            self._prune_mb += archive.mb
            self.progress.status_message(
                tr("Total size to reclaim: {mb} MB", mb=self._prune_mb)
            )
        elif clean.startswith("Total size to reclaim:"):
            self.progress.status_message(clean.strip())

    def _prune_check(self) -> None:
        self._prune_mb = 0
        self._run_cli(["cache", "prune", "check"], handler=self._prune_handler)

    def _prune_apply(self) -> None:
        self._prune_mb = 0
        self._run_cli(
            ["cache", "prune", "apply"],
            confirm=tr("Permanently delete out-of-date addon archives from the cache?"),
            confirm_title=tr("Prune Cache"),
            guard_mo2=True,
            handler=self._prune_handler,
        )

    def _purge_shader_cache(self) -> None:
        self._run_cli(
            ["anomaly", "purge-shader-cache"],
            confirm=tr("Delete the shader cache for the active Anomaly profile?"),
            confirm_title=tr("Purge Shader Cache"),
            guard_mo2=True,
        )

    def _delete_reshade(self) -> None:
        self._run_cli(
            ["anomaly", "delete-reshade"],
            confirm=tr("Delete all ReShade-related files from the Anomaly bin directory?"),
            confirm_title=tr("Delete ReShade"),
            guard_mo2=True,
        )

    def _gog_fix(self) -> None:
        self._run_cli(
            ["gog", "fix-install"],
            confirm=tr("Fix the ModOrganizer.ini paths for a GOG-provided install?"),
            confirm_title=tr("Fix GOG Install"),
            guard_mo2=True,
        )

    # -- repair wine prefix (bespoke, off the GUI thread) --------------------
    def _repair_prefix(self) -> None:
        if self.profile() is None:
            self.window.notify(tr("Create or activate a profile first (Profiles page)."))
            return
        if not self._tasks_idle() or self.window.install_busy:
            self.window.notify(tr("Another task is already running."))
            return
        if self._reject_if_mo2_running():
            return
        try:
            runner = configured_runner()
        except LaunchError as exc:
            self.window.notify(str(exc), 6000)
            return
        prefix = runner.env.get("STEAM_COMPAT_DATA_PATH") or runner.env.get("WINEPREFIX")
        if not prefix:
            self.window.notify(tr("The selected runner does not use a Proton prefix."))
            return
        self.status.setText(tr("Scanning prefix..."))
        task = BackgroundTask(foreign_prefix_dlls, prefix, runner, parent=self)
        self._repair_task = task
        task.result.connect(lambda found: self._on_prefix_scanned(prefix, runner, found))
        task.error.connect(self._on_repair_error)
        task.start()

    def _on_prefix_scanned(self, prefix, runner, foreign) -> None:
        self._repair_task = None
        if not foreign:
            self.status.setText("")
            self.window.notify(tr("Nothing to repair."))
            return
        names = sorted({item.name for item, _replacement in foreign})
        shown = ", ".join(names[:8])
        if len(names) > 8:
            shown += tr(" and {count} more", count=len(names) - 8)
        self.window.confirm(
            tr("Repair Wine prefix"),
            tr(
                "{count} DLLs in {prefix} were written by a different Wine "
                "build ({names}).\n\nThey will be restored to the runner's "
                "own versions. Reinstall Dependencies afterwards.\n\n"
                "Repair now?",
                count=len(foreign),
                prefix=prefix,
                names=shown,
            ),
            lambda: self._start_prefix_repair(prefix, runner),
            confirm_role="danger",
            confirm_text=tr("Repair"),
        )

    def _start_prefix_repair(self, prefix, runner) -> None:
        if self.window.install_busy:
            self.window.notify(tr("Another task is already running."))
            return
        self.window.set_install_busy(True)
        self._set_actions_enabled(False)
        task = BackgroundTask(repair_prefix_foreign_dlls, prefix, runner, parent=self)
        self._repair_task = task
        task.result.connect(self._on_repaired)
        task.error.connect(self._on_repair_error)
        task.start()

    def _on_repaired(self, repaired) -> None:
        self._repair_task = None
        self.window.set_install_busy(False)
        self._set_actions_enabled(True)
        self.status.setText(
            tr("Repaired {count} files. Reinstall Dependencies next.", count=len(repaired))
        )
        self.window.notify(tr("Prefix repaired"), 6000)

    def _on_repair_error(self, message: str) -> None:
        self._repair_task = None
        self.window.set_install_busy(False)
        self._set_actions_enabled(True)
        self.window.notify(tr("Repair failed: {message}", message=message), 8000)

    # -- create log dump -----------------------------------------------------
    def _start_log_dump(self) -> None:
        if not self._tasks_idle() or self.window.install_busy:
            self.window.notify(tr("Another task is already running."))
            return
        self.window.set_install_busy(True)
        self.status.setText(tr("Creating Log Dump..."))
        self.progress.reset()
        self.progress.show()
        self.progress.set_cancellable(None)
        self._set_actions_enabled(False)
        task = StreamTask(create_log_dump, parent=self)
        self._log_dump_task = task
        task.line.connect(self.progress.on_line)
        task.result.connect(self._on_log_dump_done)
        task.error.connect(self._on_log_dump_error)
        task.start()

    def _on_log_dump_done(self, result: object) -> None:
        self._log_dump_task = None
        self.progress.hide()
        self.window.set_install_busy(False)
        self._set_actions_enabled(True)
        try:
            path, stats = result
            files = int(stats.get("files", 0))
            skipped = int(stats.get("skipped", 0))
            size_mb = float(stats.get("bytes", 0)) / (1024 * 1024)
        except (TypeError, ValueError, KeyError):
            self.status.setText(tr("Log Dump Failed"))
            return
        self.status.setText(
            tr(
                "Log Dump saved: {path}\n{files} files, {skipped} skipped, {size} MB",
                path=path,
                files=files,
                skipped=skipped,
                size=f"{size_mb:.1f}",
            )
        )
        self.window.notify(tr("Log Dump created"), 5000)

    def _on_log_dump_error(self, message: str) -> None:
        self._log_dump_task = None
        self.progress.hide()
        self.window.set_install_busy(False)
        self._set_actions_enabled(True)
        self.window.notify(tr("Log Dump failed: {message}", message=message), 8000)

    # -- move installation ----------------------------------------------------
    def _open_move_overlay(self) -> None:
        if self.profile() is None:
            self.window.notify(tr("Create or activate a profile first (Profiles page)."))
            return
        if not self._tasks_idle() or self.window.install_busy:
            self.window.notify(tr("Another task is already running."))
            return
        self._move_dest = None
        self._render_move_overlay()

    def _render_move_overlay(self) -> None:
        profile = self.profile()
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        for label, path in (
            (tr("Anomaly"), profile.anomaly),
            (tr("GAMMA"), profile.gamma),
            (tr("Cache"), profile.cache),
        ):
            layout.addWidget(
                deck_label(f"{label}: {path or tr('(not set)')}", role="caption", wrap=True)
            )
        dest_row = DeckRow(
            tr("Destination"), str(self._move_dest) if self._move_dest else tr("Not set")
        )
        dest_row.activated.connect(self._pick_move_destination)
        layout.addWidget(dest_row)
        move_button = deck_button(
            tr("Move Installation"), role="danger", on_click=self._confirm_move
        )
        move_button.setEnabled(self._move_dest is not None)
        layout.addWidget(move_button)
        self.window.show_overlay(
            DeckOverlay(
                tr("Move Installation"),
                body,
                [(tr("Close"), self.window.dismiss_overlay, "normal")],
                panel_width=1000,
            )
        )

    def _pick_move_destination(self) -> None:
        show_folder_picker(
            self.window,
            title=tr("Select destination folder"),
            start=self._move_dest,
            on_choose=self._set_move_dest,
        )

    def _set_move_dest(self, path: Path) -> None:
        self._move_dest = path
        self._render_move_overlay()

    def _confirm_move(self) -> None:
        profile = self.profile()
        if profile is None or self._move_dest is None:
            return
        sources = [
            ("Anomaly", profile.anomaly),
            ("GAMMA", profile.gamma),
            ("Cache", profile.cache),
        ]
        try:
            dest_path = _validate_move_destination(str(self._move_dest), sources)
        except ValueError as exc:
            self.window.dismiss_overlay()
            self.window.notify(str(exc), 8000)
            return
        self.window.dismiss_overlay()
        message = tr(
            "Copies these folders to the destination, then removes the "
            "originals:\n\nAnomaly: {a}\nGAMMA: {g}\nCache: {c}\n\n"
            "Destination: {d}",
            a=profile.anomaly,
            g=profile.gamma,
            c=profile.cache,
            d=dest_path,
        )
        self.window.confirm(
            tr("Move Installation"),
            message,
            lambda: self._start_move(sources, dest_path, profile.profile_name),
            confirm_role="danger",
            confirm_text=tr("Move"),
        )

    def _start_move(self, sources, dest_path, profile_name) -> None:
        # install_busy/mo2 may have changed while the confirm was open.
        if self.window.install_busy or self._reject_if_mo2_running():
            return
        self.window.set_install_busy(True, "move")
        self._set_actions_enabled(False)
        self.status.setText(tr("Moving installation..."))
        self.progress.reset()
        self.progress.show()
        self._move_sources = sources
        self._move_profile_name = profile_name
        task = StreamTask(
            lambda report: _move_folders(sources, dest_path, report, task.cancel_event),
            parent=self,
        )
        self._move_task = task
        self.progress.set_cancellable(task.cancel)
        task.line.connect(self.progress.on_line)
        task.result.connect(self._on_move_done)
        task.error.connect(self._on_move_error)
        task.start()

    def _on_move_done(self, moved: object) -> None:
        self._move_task = None
        self.progress.hide()
        consistency_error: Exception | None = None
        if isinstance(moved, list) and moved:
            old_paths = dict(self._move_sources)
            new_paths = dict(moved)
            try:
                _rewrite_mo2_ini_paths(
                    new_paths["GAMMA"],
                    [
                        (old_paths["Anomaly"], new_paths["Anomaly"]),
                        (old_paths["GAMMA"], new_paths["GAMMA"]),
                    ],
                )
            except (OSError, ValueError, KeyError) as exc:
                consistency_error = exc
            try:
                _save_moved_profile(self.window.settings, self._move_profile_name, moved)
            except (OSError, ValueError) as exc:
                consistency_error = consistency_error or exc
            self.window.refresh_settings()
            for key in ("dashboard", "play", "install", "mods"):
                page = self.window._pages.get(key)
                if page is not None:
                    page.refresh()
        self.window.set_install_busy(False)
        self._set_actions_enabled(True)
        self._move_dest = None
        if isinstance(moved, list) and moved:
            if consistency_error is None:
                self.status.setText(tr("Move complete."))
            else:
                self.status.setText(
                    tr("Move Recovery Required: {error}", error=consistency_error)
                )
        else:
            self.status.setText(tr("Nothing was moved."))
        self.refresh()

    def _on_move_error(self, message: str) -> None:
        self._move_task = None
        self.progress.hide()
        self.window.set_install_busy(False)
        self._set_actions_enabled(True)
        self.window.notify(tr("Move failed: {message}", message=message), 8000)

    # -- destructive resets ---------------------------------------------------
    def _start_fresh_reset(self) -> None:
        self._start_reset(include_anomaly=True)

    def _start_gamma_reset(self) -> None:
        self._start_reset(include_anomaly=False)

    def _start_reset(self, *, include_anomaly: bool) -> None:
        if not self._tasks_idle() or self.window.install_busy:
            self.window.notify(tr("Another task is already running."))
            return
        profile = self.profile()
        if profile is None:
            self.window.notify(tr("Create or activate a profile first (Profiles page)."))
            return
        wipe_paths = [("GAMMA", profile.gamma)]
        if include_anomaly:
            wipe_paths.insert(0, ("Anomaly", profile.anomaly))
        try:
            _validate_wipe_paths(wipe_paths)
        except ValueError as exc:
            self.window.notify(str(exc), 8000)
            return

        title = tr("Fresh Reset") if include_anomaly else tr("GAMMA Reset")
        folders = tr("Anomaly and GAMMA") if include_anomaly else tr("GAMMA")
        message = tr(
            "This permanently deletes the {folders} folder(s):\n\n{paths}\n\n"
            "This deletes ALL SAVES, MO2 settings, MCM settings and any mods "
            "you added. Back up anything you want to keep first.",
            folders=folders,
            paths="\n".join(path for _label, path in wipe_paths),
        )

        def _start() -> None:
            if self.window.install_busy:
                self.window.notify(tr("Another task is already running."))
                return
            if self._reject_if_mo2_running():
                return
            self._begin_reset(include_anomaly, wipe_paths, profile, title)

        self.window.confirm(title, message, _start, confirm_role="danger", confirm_text=title)

    def _begin_reset(self, include_anomaly, wipe_paths, profile, title) -> None:
        self._reset_includes_anomaly = include_anomaly
        self._wipe_targets = (profile.anomaly, profile.gamma)
        self.window.set_install_busy(True, "anomaly" if include_anomaly else "gamma")
        self._set_actions_enabled(False)
        self.status.setText(tr("Wiping {title}...", title=title))
        self.progress.reset()
        self.progress.show()
        self.progress.set_cancellable(None)
        task = StreamTask(lambda report: _wipe_folders(wipe_paths, report), parent=self)
        self._wipe_task = task
        task.line.connect(self.progress.on_line)
        task.result.connect(lambda _wiped: self._on_reset_wiped(title))
        task.error.connect(self._on_reset_wipe_error)
        task.start()

    def _on_reset_wiped(self, title: str) -> None:
        self._wipe_task = None
        self.progress.hide()
        profile = self.profile()
        if profile is None or (profile.anomaly, profile.gamma) != self._wipe_targets:
            self.window.set_install_busy(False)
            self._set_actions_enabled(True)
            self.window.notify(
                tr(
                    "The active profile's install folders changed during the "
                    "wipe. Re-install aborted."
                ),
                8000,
            )
            return
        self.status.setText(tr("{title}: folders wiped, reinstalling...", title=title))
        self.window.set_page("install")
        install_page = self.window._pages["install"]
        if self._reset_includes_anomaly:
            preserve_user = False
            preserve_mcm = False
        else:
            preserve_user = install_page.preserve_user_row.is_checked()
            preserve_mcm = install_page.preserve_mcm_row.is_checked()
        if not install_page.start_auto_install(
            include_anomaly=self._reset_includes_anomaly,
            preserve_user=preserve_user,
            preserve_mcm=preserve_mcm,
        ):
            self.window.set_install_busy(False)
            self._set_actions_enabled(True)
            self.window.notify(tr("{title} could not be started.", title=title), 8000)

    def _on_reset_wipe_error(self, message: str) -> None:
        self._wipe_task = None
        self.progress.hide()
        self.window.set_install_busy(False)
        self._set_actions_enabled(True)
        self.window.notify(tr("Wipe failed: {message}", message=message), 8000)

    def _start_full_uninstall(self) -> None:
        if not self._tasks_idle() or self.window.install_busy:
            self.window.notify(tr("Another task is already running."))
            return
        profile = self.profile()
        if profile is None:
            self.window.notify(tr("Create or activate a profile first (Profiles page)."))
            return
        nothing_to_remove = (
            _resolved_wipe_target(profile.anomaly) is None
            and _resolved_wipe_target(profile.gamma) is None
            and _resolved_wipe_target(profile.cache) is None
        )
        if nothing_to_remove:
            self.window.notify(tr("Nothing found to uninstall."))
            return
        uninstall_paths = [
            ("Anomaly", profile.anomaly),
            ("GAMMA", profile.gamma),
            ("Cache", profile.cache),
        ]
        try:
            _validate_wipe_paths(uninstall_paths)
        except ValueError as exc:
            self.window.notify(str(exc), 8000)
            return

        message = tr(
            "This permanently deletes:\n\n{paths}\n\n"
            "This deletes ALL SAVES, MO2 settings, MCM settings, mods and "
            "the download cache. The Wine/Proton prefix is kept.\n\n"
            "Back up anything you want to keep first.",
            paths="\n".join(path for _label, path in uninstall_paths),
        )

        def _start() -> None:
            if self.window.install_busy:
                self.window.notify(tr("Another task is already running."))
                return
            if self._reject_if_mo2_running():
                return
            self._begin_full_uninstall(uninstall_paths, profile)

        self.window.confirm(
            tr("Full Uninstall"),
            message,
            _start,
            confirm_role="danger",
            confirm_text=tr("Full Uninstall"),
        )

    def _begin_full_uninstall(self, uninstall_paths, profile) -> None:
        self._full_uninstall_targets = (profile.anomaly, profile.gamma, profile.cache)
        self.window.set_install_busy(True)
        self._set_actions_enabled(False)
        self.status.setText(tr("Removing Anomaly, GAMMA, and cache folders..."))
        self.progress.reset()
        self.progress.show()
        self.progress.set_cancellable(None)
        task = StreamTask(lambda report: _wipe_folders(uninstall_paths, report), parent=self)
        self._wipe_task = task
        task.line.connect(self.progress.on_line)
        task.result.connect(self._on_full_uninstall_done)
        task.error.connect(self._on_full_uninstall_error)
        task.start()

    def _on_full_uninstall_done(self, _wiped: object) -> None:
        self._wipe_task = None
        self.progress.hide()
        self.window.set_install_busy(False)
        self._set_actions_enabled(True)
        profile = self.profile()
        if profile is None or (
            profile.anomaly,
            profile.gamma,
            profile.cache,
        ) != self._full_uninstall_targets:
            self.status.setText(
                tr("Uninstall finished, but the active profile changed while it was running.")
            )
        else:
            self.status.setText(tr("Anomaly and GAMMA completely uninstalled"))
        self.refresh()
        for key in ("dashboard", "install"):
            page = self.window._pages.get(key)
            if page is not None:
                page.refresh()

    def _on_full_uninstall_error(self, message: str) -> None:
        self._wipe_task = None
        self.progress.hide()
        self.window.set_install_busy(False)
        self._set_actions_enabled(True)
        self.window.notify(tr("Uninstall failed: {message}", message=message), 8000)
