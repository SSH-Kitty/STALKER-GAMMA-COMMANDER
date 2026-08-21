"""Utilities page: integrity checks, cache pruning, maintenance tasks."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..cli_runner import cli_command
from ..config import logs_dir
from ..parsers import (
    parse_anomaly_check,
    parse_prune_archive,
    strip_ansi,
)
from .common import (
    STATUS_RED,
    CommandRunner,
    OutputPane,
    ProgressArea,
    StreamTask,
    anomaly_installed,
    gamma_installed,
    info_label,
    make_card,
    open_in_file_manager,
    section_label,
)

#: Directories that must never be handed to rmtree, whatever a profile says.
_PROTECTED_ROOTS = (
    "/",
    "/home",
    "/root",
    "/usr",
    "/etc",
    "/var",
    "/opt",
    "/boot",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/srv",
    "/mnt",
    "/media",
    "/tmp",
)


def _resolved_wipe_target(path: str) -> Path | None:
    """Resolve ``path`` to the directory that would actually be deleted.

    Returns ``None`` when the path is blank or not a real directory.
    Resolution and deletion must agree on one path, so callers use this
    result for both.
    """
    if not path or not path.strip():
        return None
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_dir() else None


def _safe_wipe_path(raw: str, resolved: Path) -> bool:
    """Refuse paths too broad to be a GAMMA install folder.

    A real install folder is nested at least two levels below the filesystem
    root, is never a system directory, any user's home, the GUI's own
    location, the current working directory, or a symlink (deleting through
    one would take out an unrelated tree).
    """
    if resolved.parent == resolved:
        return False
    if str(resolved) in _PROTECTED_ROOTS:
        return False
    home = Path.home()
    # Our home, its parent, and any sibling of it (/home/<someone-else>).
    if resolved in (home, home.parent) or resolved.parent == home.parent:
        return False
    try:
        if resolved == Path(__file__).resolve().parents[2]:
            return False
    except IndexError:
        pass
    if resolved == Path.cwd().resolve():
        return False
    # Deleting through a symlink would take out an unrelated tree; resolve()
    # already followed it, so inspect the pre-resolution path itself.
    if Path(raw).expanduser().is_symlink():
        return False
    return len(resolved.parts) >= 3


def _wipe_folders(paths: list[tuple[str, str]], report) -> list[str]:
    """Delete the given ``(label, path)`` install folders completely.

    Raises ``ValueError`` if any present path fails :func:`_safe_wipe_path`.
    Returns the list of paths that were actually deleted.
    """
    wiped: list[str] = []
    for label, path in paths:
        resolved = _resolved_wipe_target(path)
        if resolved is None:
            report(f"{label}: folder not present, skipping ({path})")
            continue
        if not _safe_wipe_path(path, resolved):
            raise ValueError(f"Refusing to wipe unsafe path: {resolved}")
        report(f"Deleting {label} folder: {resolved} ...")
        shutil.rmtree(resolved, ignore_errors=True)
        if resolved.exists():
            raise ValueError(f"{label} folder could not be fully deleted: {resolved}")
        wiped.append(str(resolved))
        report(f"{label} folder deleted.")
    return wiped


def _copy_dir_tree(src: Path, dst: Path, report, cancel_event=None) -> None:
    """Copy *src* into *dst*, streaming progress via *report*."""
    if not src.is_dir():
        raise ValueError(f"Source folder does not exist: {src}")
    dst.mkdir(parents=True, exist_ok=True)
    total = sum(1 for _ in src.rglob("*"))
    idx = 0
    for root, dirs, names in os.walk(src):
        dirs.sort()
        names.sort()
        for name in (*dirs, *names):
            item = Path(root) / name
            idx += 1
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Move cancelled")
            rel = item.relative_to(src)
            target = dst / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
            if idx % 200 == 0 or idx == total:
                report(f"  copied {idx}/{total} entries ...")


def _file_count_and_verify(source: Path, destination: Path) -> tuple[int, bool]:
    """Count source files while checking that each exists in the destination."""
    source_count = 0
    valid = True
    for root, _dirs, names in os.walk(source):
        for name in names:
            source_count += 1
            relative = (Path(root) / name).relative_to(source)
            target = destination / relative
            if not target.is_file():
                valid = False
    destination_count = sum(len(names) for _root, _dirs, names in os.walk(destination))
    return source_count, valid and source_count == destination_count


def _move_folders(
    sources: list[tuple[str, str]],
    dest_parent: Path,
    report,
    cancel_event=None,
) -> list[tuple[str, str]]:
    """Copy then delete the given ``(label, path)`` folders into *dest_parent*.

    Returns a list of ``(label, new_path)`` for folders that were moved.
    Raises ``ValueError`` on failure; originals are left intact in that case.
    """
    resolved_sources: list[tuple[str, Path]] = []
    for label, path in sources:
        if not path or not path.strip():
            report(f"{label}: path is empty, skipping")
            continue
        src = Path(path).expanduser().resolve()
        if not src.is_dir():
            report(f"{label}: folder not found, skipping ({src})")
            continue
        if not _safe_wipe_path(path, src):
            raise ValueError(f"Refusing to move unsafe path: {src}")
        if dest_parent == src or dest_parent in src.parents:
            raise ValueError(
                f"Destination is inside the {label} source folder: {dest_parent}"
            )
        resolved_sources.append((label, src))

    for index, (label, src) in enumerate(resolved_sources):
        for other_label, other in resolved_sources[index + 1 :]:
            if src == other or src in other.parents or other in src.parents:
                raise ValueError(f"Source folders overlap: {label} and {other_label}")

    destinations: list[tuple[str, Path, Path]] = []
    for label, src in resolved_sources:
        dst = dest_parent / src.name
        if dst.exists():
            raise ValueError(
                f"Destination already exists: {dst}\n"
                "Choose a folder that does not already contain these subfolders."
            )
        destinations.append((label, src, dst))

    copied: list[Path] = []
    backups: list[tuple[Path, Path]] = []
    try:
        for label, src, dst in destinations:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Move cancelled")
            report(f"Copying {label}: {src} -> {dst}")
            _copy_dir_tree(src, dst, report, cancel_event)
            report("Copy complete. Verifying ...")
            _source_count, verified = _file_count_and_verify(src, dst)
            if not verified:
                raise ValueError(f"Copy verification failed for {label}: {dst}")
            copied.append(dst)

        for label, src, _dst in destinations:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Move cancelled")
            backup = src.with_name(f".{src.name}.move-backup-{uuid.uuid4().hex}")
            src.rename(backup)
            backups.append((src, backup))
            report(f"Original {label} staged for removal.")

        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Move cancelled")
        for (label, _src, _dst), (_original, backup) in zip(destinations, backups):
            report(f"Removing original {label}: {backup}")
            shutil.rmtree(backup)

        return [(label, str(dst)) for label, _src, dst in destinations]
    except Exception:
        for original, backup in reversed(backups):
            try:
                if backup.exists() and not original.exists():
                    backup.rename(original)
                else:
                    report(
                        f"Warning: backup kept at {backup} (original still present or "
                        "backup missing)"
                    )
            except OSError:
                report(
                    f"Warning: could not restore {original} from {backup}; "
                    "backup file preserved for manual recovery"
                )
        for dst in reversed(copied):
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
        raise


class UtilitiesPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._runner: CommandRunner | None = None
        self._prune_mb = 0
        self._wipe_task: StreamTask | None = None
        self._wipe_targets: tuple[str, str] = ("", "")
        self._full_uninstall_targets: tuple[str, str, str] = ("", "", "")
        self._move_task: StreamTask | None = None
        self.buttons: list[QPushButton] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 20)
        outer.setSpacing(12)

        title = section_label("UTILITIES", level=1)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(title)
        outer.addWidget(
            info_label(
                "Maintenance tools for Anomaly and GAMMA: verify files, manage the "
                "download and shader caches, fix GOG paths, and safely reset or remove folders."
            )
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(14)
        scroll.setWidget(content)

        root.addWidget(self._tools_card())
        root.addWidget(self._move_card())
        root.addWidget(self._destructive_card())
        root.addWidget(self._console_card())
        root.addStretch(1)

        self.refresh()

    def _tools_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Tools", level=2))
        layout.addWidget(
            info_label(
                "Tools use the active profile's folders. Technical output is available in the console below."
            )
        )
        grid = QGridLayout()
        grid.setSpacing(8)
        tools = [
            (
                "Check Anomaly files",
                "Verify the base game files against the official checksums.",
                self._anomaly_check,
            ),
            (
                "Preview cache cleanup",
                (
                    "List out-of-date addon archives in the cache with the total "
                    "size that can be reclaimed."
                ),
                self._prune_check,
            ),
            (
                "Clean the download cache",
                "Permanently delete out-of-date addon archives from the cache.",
                self._prune_apply,
            ),
            (
                "Clear shader cache",
                "Delete the shader cache for the active Anomaly profile.",
                self._purge_shader_cache,
            ),
            (
                "Remove ReShade",
                "Remove all ReShade-related files from the Anomaly bin directory.",
                self._delete_reshade,
            ),
            (
                "Fix GOG installation",
                "Fix the ModOrganizer.ini paths for a GOG-provided install.",
                self._gog_fix,
            ),
            (
                "Open log folder",
                "Open the CLI and launcher log directory in your file manager.",
                self._open_logs,
            ),
            (
                "Create diagnostic archive",
                (
                    "Hash all installation files and create a compressed archive "
                    "in the current working directory."
                ),
                self._hash_install,
            ),
        ]
        for i, (title, description, slot) in enumerate(tools):
            grid.addWidget(self._tool_row(title, description, slot), i // 2, i % 2)
        layout.addLayout(grid)
        return card

    def _tool_row(self, title: str, description: str, slot) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        body = QVBoxLayout()
        body.setSpacing(2)
        name = QLabel(title)
        name.setObjectName("section2")
        body.addWidget(name)
        body.addWidget(info_label(description))
        row.addLayout(body, 1)
        button = QPushButton("Run")
        button.clicked.connect(slot)
        self.buttons.append(button)
        row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        return container

    def _destructive_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Reset or uninstall", level=2))
        layout.addWidget(
            info_label(
                "These destructive actions show the exact folders to be deleted and ask for confirmation first."
            )
        )
        panels = QHBoxLayout()
        panels.setSpacing(14)

        fresh_panel, self.fresh_reset_button = self._destructive_panel(
            "Fresh reset",
            "Deletes the Anomaly and GAMMA folders, then reinstalls both from "
            "scratch into the same locations.",
            [
                "Deletes ALL saves, MO2 settings, MCM settings and any mods you added",
                "Requires Anomaly and GAMMA to be installed",
            ],
            "Fresh Reset",
            self._start_fresh_reset,
        )
        panels.addWidget(fresh_panel, 1)

        full_panel, self.full_uninstall_button = self._destructive_panel(
            "Full uninstall",
            "Removes the Anomaly, GAMMA, and cache folders, leaving your "
            "Wine/Proton prefix intact.",
            [
                (
                    "Deletes ALL saves, MO2 settings, MCM settings, added mods "
                    "and the download cache"
                ),
                (
                    "The configured Wine/Proton prefix and its Winetricks "
                    "configuration are kept"
                ),
            ],
            "Full Uninstall",
            self._start_full_uninstall,
        )
        panels.addWidget(full_panel, 1)
        layout.addLayout(panels)

        caution = info_label(
            "Both actions refuse to operate on system paths, home directories or "
            "symlinks, and re-check that the profile still points where it did "
            "before deleting anything."
        )
        caution.setObjectName("warn")
        layout.addWidget(caution)

        self.fresh_reset_hint = info_label(
            "Requires Anomaly and GAMMA to be installed."
        )
        layout.addWidget(self.fresh_reset_hint)
        return card

    def _destructive_panel(
        self,
        title: str,
        description: str,
        bullets: list[str],
        button_text: str,
        slot,
    ) -> tuple[QWidget, QPushButton]:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        v.addWidget(section_label(title, level=2))
        v.addWidget(info_label(description))
        for bullet in bullets:
            v.addWidget(info_label(f"• {bullet}"))
        button = QPushButton(button_text)
        button.setObjectName("danger")
        button.clicked.connect(slot)
        v.addStretch(1)
        v.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        return panel, button

    # ----- move game -----
    def _move_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Move installation", level=2))
        layout.addWidget(
            info_label(
                "Move the Anomaly, GAMMA, and cache folders to another drive. "
                "Files are copied and checked before the originals are removed."
            )
        )

        self._move_anomaly_label = info_label("Anomaly: (no profile)")
        self._move_gamma_label = info_label("GAMMA: (no profile)")
        self._move_cache_label = info_label("Cache: (no profile)")
        layout.addWidget(self._move_anomaly_label)
        layout.addWidget(self._move_gamma_label)
        layout.addWidget(self._move_cache_label)

        dest_row = QHBoxLayout()
        dest_row.setSpacing(10)
        self._move_dest_edit = QLineEdit()
        self._move_dest_edit.setPlaceholderText("Select destination folder ...")
        dest_btn = QPushButton("Browse...")
        dest_btn.clicked.connect(self._browse_move_dest)
        dest_row.addWidget(self._move_dest_edit, 1)
        dest_row.addWidget(dest_btn)
        layout.addLayout(dest_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self._move_btn = QPushButton("Move installation")
        self._move_btn.setObjectName("danger")
        self._move_btn.clicked.connect(self._start_move)
        btn_row.addWidget(self._move_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self._move_cancel_btn = QPushButton("Cancel")
        self._move_cancel_btn.setObjectName("secondary")
        self._move_cancel_btn.setFixedSize(100, 32)
        self._move_cancel_btn.clicked.connect(self._cancel_move)
        self._move_cancel_btn.hide()
        btn_row.addWidget(self._move_cancel_btn, 0, Qt.AlignmentFlag.AlignLeft)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._move_progress = ProgressArea(show_table=False, show_log=True)
        layout.addWidget(self._move_progress)
        return card

    def _browse_move_dest(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select destination folder", self._move_dest_edit.text()
        )
        if path:
            self._move_dest_edit.setText(path)

    def _refresh_move_paths(self) -> None:
        profile = self.window.settings.active_profile
        if profile is None:
            self._move_anomaly_label.setText("Anomaly: (no profile)")
            self._move_gamma_label.setText("GAMMA: (no profile)")
            self._move_cache_label.setText("Cache: (no profile)")
            self._move_btn.setEnabled(False)
            return
        self._move_anomaly_label.setText(f"Anomaly:  {profile.anomaly}")
        self._move_gamma_label.setText(f"GAMMA:    {profile.gamma}")
        self._move_cache_label.setText(f"Cache:    {profile.cache}")
        has_paths = bool(profile.anomaly and profile.gamma and profile.cache)
        self._move_btn.setEnabled(has_paths and not self.window.install_busy)

    def _start_move(self) -> None:
        if self._move_task is not None:
            QMessageBox.information(self, "Busy", "A move is already in progress.")
            return
        if self._runner is not None and self._runner.is_running():
            QMessageBox.information(self, "Busy", "Another task is running.")
            return
        if self.window.install_busy:
            QMessageBox.information(self, "Busy", "An install is already running.")
            return
        if not self._require_profile():
            return

        dest = self._move_dest_edit.text().strip()
        if not dest:
            QMessageBox.warning(
                self, "No Destination", "Select a destination folder first."
            )
            return
        dest_path = Path(dest).expanduser().resolve()
        if not dest_path.is_dir():
            QMessageBox.warning(
                self, "Invalid Destination", f"Folder does not exist:\n{dest}"
            )
            return

        profile = self.window.settings.active_profile
        sources = [
            ("Anomaly", profile.anomaly),
            ("GAMMA", profile.gamma),
            ("Cache", profile.cache),
        ]

        answer = QMessageBox.question(
            self,
            "Move Game",
            "<html><body>"
            "<div style='font-weight: bold; font-size: 13px;'>Move Game</div><br>"
            "This will copy the following folders to the destination and "
            "then remove the originals:<br><br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;Anomaly: {profile.anomaly}<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;GAMMA: {profile.gamma}<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;Cache: {profile.cache}<br><br>"
            f"<strong>Destination:</strong> {dest}<br><br>"
            "Make sure the destination has enough free space.<br><br>"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.window.set_install_busy(True)
        self._show_console()
        self._move_progress.reset()
        self._move_progress.on_started()
        self._move_btn.setEnabled(False)
        self._move_cancel_btn.show()
        self._set_buttons_enabled(False)
        gui_settings.save_gui_settings(
            move_dest=str(dest_path),
            move_expected=[Path(path).name for _label, path in sources if path],
        )

        task = StreamTask(
            lambda report: _move_folders(sources, dest_path, report, task.cancel_event),
            parent=self,
        )
        self._move_task = task
        task.line.connect(self._on_move_progress)
        task.result.connect(self._on_move_done)
        task.error.connect(self._on_move_error)
        task.start()

    def _on_move_progress(self, line: str) -> None:
        self._move_progress.on_line(line)

    def _on_move_done(self, moved: object) -> None:
        self._move_task = None
        self._move_cancel_btn.hide()
        self._move_progress.on_finished(0, "")
        self._move_progress.status_message("Move complete")
        self.window.set_install_busy(False)
        gui_settings.save_gui_settings(move_dest="", move_expected=[])
        self._set_buttons_enabled(True)

        save_error = None
        if isinstance(moved, list) and moved:
            profile = self.window.settings.active_profile
            if profile is not None:
                for label, new_path in moved:
                    attr = label.lower()
                    if hasattr(profile, attr):
                        setattr(profile, attr, new_path)
                try:
                    self.window.settings.save()
                except OSError as exc:
                    save_error = exc
            self.window.refresh_settings()

        self._refresh_move_paths()
        if isinstance(moved, list) and moved:
            message = (
                "Game folders moved successfully.\nProfile paths have been updated."
            )
            if save_error is not None:
                message = (
                    "Game folders moved successfully, but the updated profile could not "
                    f"be saved:\n{save_error}\n\nUpdate the profile paths manually."
                )
            QMessageBox.information(
                self,
                "Move Complete",
                message,
            )

    def _on_move_error(self, message: str) -> None:
        self._move_task = None
        self._move_cancel_btn.hide()
        self._move_progress.on_finished(1, "")
        self._move_progress.status_message("Move failed")
        self.window.set_install_busy(False)
        gui_settings.save_gui_settings(move_dest="", move_expected=[])
        self._set_buttons_enabled(True)
        self._refresh_move_paths()
        QMessageBox.warning(self, "Move Failed", f"Move failed:\n{message}")

    def _cancel_move(self) -> None:
        if self._move_task is not None:
            self._move_task.cancel()
            self._move_progress.status_message("Cancelling ...")

    # ----- console -----
    def _console_card(self) -> QWidget:
        card, layout = make_card()
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(section_label("Console", level=2))
        header.addStretch(1)
        self._console_toggle = QPushButton("Show")
        self._console_toggle.setObjectName("consoleToggle")
        self._console_toggle.setFixedSize(60, 26)
        self._console_toggle.clicked.connect(self._toggle_console)
        header.addWidget(self._console_toggle)
        layout.addLayout(header)

        self.summary = QLabel("")
        self.summary.setObjectName("accent")
        layout.addWidget(self.summary)
        self.output = OutputPane()
        self.output.setMaximumHeight(180)
        layout.addWidget(self.output)

        self.summary.hide()
        self.output.hide()
        self._console_visible = False
        return card

    def _toggle_console(self) -> None:
        if self._console_visible:
            self.summary.hide()
            self.output.hide()
            self._console_toggle.setText("Show")
            self._console_visible = False
        else:
            self.summary.show()
            self.output.show()
            self._console_toggle.setText("Hide")
            self._console_visible = True

    def _show_console(self) -> None:
        if not self._console_visible:
            self.summary.show()
            self.output.show()
            self._console_toggle.setText("Hide")
            self._console_visible = True

    def refresh(self) -> None:
        self.window.refresh_settings()
        self._update_fresh_reset_enabled()
        self._refresh_move_paths()

    def _update_fresh_reset_enabled(self) -> None:
        profile = self.window.settings.active_profile
        installed = (
            profile is not None
            and anomaly_installed(profile.anomaly)
            and gamma_installed(profile.gamma)
        )
        fresh_reset_enabled = installed and not self.window.install_busy
        full_uninstall_enabled = (
            profile is not None
            and gamma_installed(profile.gamma)
            and not self.window.install_busy
        )
        self.fresh_reset_button.setEnabled(fresh_reset_enabled)
        self.full_uninstall_button.setEnabled(full_uninstall_enabled)
        self.fresh_reset_hint.setVisible(
            not (fresh_reset_enabled or full_uninstall_enabled)
        )

    def _require_profile(self) -> bool:
        if self.window.settings.active_profile is None:
            QMessageBox.warning(
                self,
                "No Profile",
                "Create or activate a profile first (Profiles page).",
            )
            return False
        return True

    def _confirm(self, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            "Confirm",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _run(
        self,
        args: list[str],
        *,
        handler=None,
        confirm: str | None = None,
        on_finished=None,
    ) -> None:
        if self._runner is not None and self._runner.is_running():
            QMessageBox.information(self, "Busy", "A task is already running.")
            return
        if confirm is not None and not self._confirm(confirm):
            return
        if self.window.install_busy:
            QMessageBox.information(self, "Busy", "Another install task is running.")
            return
        self._show_console()
        self.summary.setText("")
        self.output.clear()
        self._prune_mb = 0
        self._set_buttons_enabled(False)
        self.window.set_install_busy(True)
        runner = CommandRunner(cli_command(args), parent=self)
        self._runner = runner
        runner.line.connect(handler if handler is not None else self.output.append_line)
        runner.finished.connect(lambda rc, out: self._on_finished(rc, out, on_finished))
        runner.cancelled.connect(lambda: self.output.append_line("[cancelled]"))
        runner.start()

    def on_busy_changed(self, busy: bool) -> None:
        """Global install lock changed; re-evaluate this page's controls."""
        self._set_buttons_enabled(not busy and self._tasks_idle())

    def _tasks_idle(self) -> bool:
        return (
            not (self._runner is not None and self._runner.is_running())
            and self._wipe_task is None
            and self._move_task is None
        )

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in self.buttons:
            btn.setEnabled(
                enabled and not self.window.install_busy and self._tasks_idle()
            )
        self._update_fresh_reset_enabled()

    def _on_finished(self, rc: int, _output: str, on_finished) -> None:
        try:
            if on_finished:
                on_finished(rc, _output)
            elif rc != 0:
                self.output.append_line(f"[command exited with code {rc}]")
        finally:
            self._runner = None
            self.window.set_install_busy(False)
            self._set_buttons_enabled(True)

    # ----- fresh reset -----
    def _start_fresh_reset(self) -> None:
        if self._runner is not None and self._runner.is_running():
            QMessageBox.information(self, "Busy", "A task is already running.")
            return
        if self._wipe_task is not None:
            QMessageBox.information(
                self, "Busy", "A fresh reset is already in progress."
            )
            return
        if self.window.install_busy:
            QMessageBox.information(self, "Busy", "An install is already running.")
            return
        if not self._require_profile():
            return
        profile = self.window.settings.active_profile
        self._wipe_targets = (profile.anomaly, profile.gamma)
        answer = QMessageBox.question(
            self,
            "Fresh Reset",
            "<html><body>"
            "<div style='font-weight: bold; font-size: 13px;'>WARNING</div><br>"
            f"<div style='color: {STATUS_RED.name()}; text-align: center; font-weight: bold; font-size: 14px;'>"
            "FRESH RESET WILL COMPLETELY WIPE & RE-INSTALL STALKER ANOMALY & GAMMA FOLDERS"
            "</div><br><br>"
            "This will permanently delete the following folders:<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;{profile.anomaly}<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;{profile.gamma}<br><br>"
            "<strong>THIS DELETES:</strong><br>"
            "&nbsp;&nbsp;• ALL SAVES<br>"
            "&nbsp;&nbsp;• MO2 SETTINGS<br>"
            "&nbsp;&nbsp;• MCM SETTINGS<br>"
            "&nbsp;&nbsp;• ANY ADDITIONAL MODS YOU ADDED<br><br>"
            "Please back up anything you want to keep before continuing.<br><br>"
            "Are you sure you want to run a Fresh Reset?</body></html>",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.window.set_install_busy(True)
        self._show_console()
        self.summary.setText("Wiping Anomaly and GAMMA folders...")
        self.output.clear()
        self._set_buttons_enabled(False)
        task = StreamTask(
            lambda report: _wipe_folders(
                [
                    ("Anomaly", self._wipe_targets[0]),
                    ("GAMMA", self._wipe_targets[1]),
                ],
                report,
            ),
            parent=self,
        )
        self._wipe_task = task
        task.line.connect(self.output.append_line)
        task.result.connect(self._on_wipe_done)
        task.error.connect(self._on_wipe_error)
        task.start()

    def _on_wipe_done(self, _wiped: object) -> None:
        self._wipe_task = None
        self._set_buttons_enabled(True)
        profile = self.window.settings.active_profile
        if profile is None:
            self.window.set_install_busy(False)
            QMessageBox.warning(
                self, "Fresh Reset", "No active profile. Reinstall aborted."
            )
            return
        if (
            profile.anomaly != self._wipe_targets[0]
            or profile.gamma != self._wipe_targets[1]
        ):
            self.window.set_install_busy(False)
            QMessageBox.warning(
                self,
                "Fresh Reset",
                "The active profile's install folders changed during the wipe. "
                "Re-install aborted so nothing is installed to the wrong location.",
            )
            return
        self.summary.setText("Folders wiped. Reinstalling Anomaly and GAMMA...")
        install_page = self.window._pages["install"]
        self.window.set_page("install")
        if not install_page.start_auto_install():
            self.window.set_install_busy(False)
            QMessageBox.warning(
                self,
                "Fresh Reset",
                "Fresh Reset could not be started (another task is running or "
                "no profile is active).",
            )

    def _on_wipe_error(self, message: str) -> None:
        self._wipe_task = None
        self.window.set_install_busy(False)
        self._set_buttons_enabled(True)
        self.summary.setText("")
        QMessageBox.warning(self, "Fresh Reset", f"Wipe failed: {message}")

    def _start_full_uninstall(self) -> None:
        if self._runner is not None and self._runner.is_running():
            QMessageBox.information(self, "Busy", "A task is already running.")
            return
        if self._wipe_task is not None:
            QMessageBox.information(
                self, "Busy", "A removal task is already in progress."
            )
            return
        if self.window.install_busy:
            QMessageBox.information(self, "Busy", "An install is already running.")
            return
        if not self._require_profile():
            return
        profile = self.window.settings.active_profile
        if not gamma_installed(profile.gamma):
            QMessageBox.information(self, "Not Installed", "GAMMA is not installed.")
            self._update_fresh_reset_enabled()
            return

        self._full_uninstall_targets = (profile.anomaly, profile.gamma, profile.cache)
        answer = QMessageBox.question(
            self,
            "Full Uninstall",
            "<html><body>"
            "<div style='font-weight: bold; font-size: 13px;'>WARNING</div><br>"
            f"<div style='color: {STATUS_RED.name()}; text-align: center; font-weight: bold; font-size: 14px;'>"
            "FULL UNINSTALL WILL COMPLETELY REMOVE STALKER ANOMALY & GAMMA"
            "</div><br><br>"
            "This will permanently delete the following folders:<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;{profile.anomaly}<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;{profile.gamma}<br><br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;{profile.cache}<br><br>"
            "<strong>THIS DELETES:</strong><br>"
            "&nbsp;&nbsp;• ALL SAVES<br>"
            "&nbsp;&nbsp;• MO2 SETTINGS<br>"
            "&nbsp;&nbsp;• MCM SETTINGS<br>"
            "&nbsp;&nbsp;• ANY ADDITIONAL MODS YOU ADDED<br><br>"
            "&nbsp;&nbsp;• DOWNLOAD CACHE<br><br>"
            "The configured Wine/Proton prefix and its Winetricks configuration "
            "will not be deleted.<br><br>"
            "Please back up anything you want to keep before continuing.<br><br>"
            "Are you sure you want to completely uninstall Anomaly and GAMMA?</body></html>",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.window.set_install_busy(True)
        self._show_console()
        self.summary.setText("Removing Anomaly, GAMMA, and cache folders...")
        self.output.clear()
        self._set_buttons_enabled(False)
        task = StreamTask(
            lambda report: _wipe_folders(
                [
                    ("Anomaly", self._full_uninstall_targets[0]),
                    ("GAMMA", self._full_uninstall_targets[1]),
                    ("Cache", self._full_uninstall_targets[2]),
                ],
                report,
            ),
            parent=self,
        )
        self._wipe_task = task
        task.line.connect(self.output.append_line)
        task.result.connect(self._on_full_uninstall_done)
        task.error.connect(self._on_full_uninstall_error)
        task.start()

    def _on_full_uninstall_done(self, _wiped: object) -> None:
        self._wipe_task = None
        self.window.set_install_busy(False)
        self._set_buttons_enabled(True)
        self.summary.setText("Anomaly and GAMMA completely uninstalled")
        self.refresh()

    def _on_full_uninstall_error(self, message: str) -> None:
        self._wipe_task = None
        self.window.set_install_busy(False)
        self._set_buttons_enabled(True)
        self.summary.setText("")
        QMessageBox.warning(self, "Full Uninstall", f"Uninstall failed: {message}")

    # ----- tasks -----
    def _anomaly_check(self) -> None:
        if not self._require_profile():
            return

        def handler(line: str) -> None:
            clean = strip_ansi(line)
            self.output.append_line(clean)
            result = parse_anomaly_check(clean)
            if result is not None:
                if result.status == "OK":
                    self._counts["OK"] = self._counts.get("OK", 0) + 1
                elif result.status == "CORRUPT":
                    self._counts["CORRUPT"] = self._counts.get("CORRUPT", 0) + 1
                elif result.status == "NOT FOUND":
                    self._counts["NOT FOUND"] = self._counts.get("NOT FOUND", 0) + 1
                self.summary.setText(
                    f"OK: {self._counts.get('OK', 0)}   "
                    f"CORRUPT: {self._counts.get('CORRUPT', 0)}   "
                    f"NOT FOUND: {self._counts.get('NOT FOUND', 0)}"
                )

        self._counts = {}
        self._run(["anomaly", "check"], handler=handler)

    def _purge_shader_cache(self) -> None:
        if self._require_profile():
            self._run(
                ["anomaly", "purge-shader-cache"],
                confirm="Delete the shader cache for the active Anomaly profile?",
            )

    def _delete_reshade(self) -> None:
        if self._require_profile():
            self._run(
                ["anomaly", "delete-reshade"],
                confirm="Delete all ReShade-related files from the Anomaly bin directory?",
            )

    def _prune_check(self) -> None:
        if self._require_profile():
            self._run(["cache", "prune", "check"], handler=self._prune_handler)

    def _prune_apply(self) -> None:
        if self._require_profile():
            self._run(
                ["cache", "prune", "apply"],
                handler=self._prune_handler,
                confirm="Permanently delete out-of-date addon archives from the cache?",
            )

    def _prune_handler(self, line: str) -> None:
        clean = strip_ansi(line)
        self.output.append_line(clean)
        archive = parse_prune_archive(clean)
        if archive is not None:
            self._prune_mb = self._prune_mb + archive.mb
            self.summary.setText(f"Total size to reclaim: {self._prune_mb} MB")
        elif clean.startswith("Total size to reclaim:"):
            self.summary.setText(clean.strip())

    def _gog_fix(self) -> None:
        if self._require_profile():
            self._run(
                ["gog", "fix-install"],
                confirm="Fix the ModOrganizer.ini paths for a GOG-provided install?",
            )

    def _open_logs(self) -> None:
        opened = open_in_file_manager(logs_dir())
        if not opened:
            QMessageBox.warning(
                self,
                "No File Manager",
                "No file manager (dolphin, nautilus, nemo, thunar) or xdg-open "
                f"was found to open:\n{logs_dir()}",
            )

    def _hash_install(self) -> None:
        if self._require_profile():
            self._run(
                ["debug", "hash-install"],
                confirm=(
                    "Hash all installation files and create a compressed archive "
                    "in the current working directory? This can take a while."
                ),
            )
