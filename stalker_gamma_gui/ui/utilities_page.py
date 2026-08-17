"""Utilities page: integrity checks, cache pruning, maintenance tasks."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..cli_runner import cli_command
from ..config import logs_dir
from ..parsers import (
    parse_anomaly_check,
    parse_prune_archive,
    strip_ansi,
)
from .common import (
    CommandRunner,
    OutputPane,
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
    "/", "/home", "/root", "/usr", "/etc", "/var", "/opt", "/boot",
    "/bin", "/sbin", "/lib", "/lib64", "/srv", "/mnt", "/media", "/tmp",
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


class UtilitiesPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._runner: CommandRunner | None = None
        self._wipe_task: StreamTask | None = None
        self._wipe_targets: tuple[str, str] = ("", "")
        self._full_uninstall_targets: tuple[str, str, str] = ("", "", "")
        self.buttons: list[QPushButton] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 20)
        outer.setSpacing(12)

        outer.addWidget(section_label("Utilities", level=1))
        outer.addWidget(
            info_label(
                "Maintenance tools for your GAMMA install: verify Anomaly "
                "integrity, prune the addon cache, manage ReShade and shader "
                "caches, and — guarded by explicit warnings — reset or remove "
                "the install folders."
            )
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(14)
        scroll.setWidget(content)

        root.addWidget(self._tools_card())
        root.addWidget(self._destructive_card())
        root.addStretch(1)

        console, c_layout = make_card()
        outer.addWidget(console)
        c_layout.addWidget(section_label("Console", level=2))
        self.summary = QLabel("")
        self.summary.setObjectName("accent")
        c_layout.addWidget(self.summary)
        self.output = OutputPane()
        self.output.setMaximumHeight(180)
        c_layout.addWidget(self.output)

        self.refresh()

    def _tools_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Tools", level=2))
        layout.addWidget(
            info_label(
                "Each tool runs against the active profile's folders and streams "
                "its output to the console below."
            )
        )
        grid = QGridLayout()
        grid.setSpacing(14)
        tools = [
            (
                "Anomaly integrity check",
                "Verify the base game files against the official checksums.",
                self._anomaly_check,
            ),
            (
                "Cache prune check",
                (
                    "List out-of-date addon archives in the cache with the total "
                    "size that can be reclaimed."
                ),
                self._prune_check,
            ),
            (
                "Cache prune apply",
                "Permanently delete out-of-date addon archives from the cache.",
                self._prune_apply,
            ),
            (
                "Purge shader cache",
                "Delete the shader cache for the active Anomaly profile.",
                self._purge_shader_cache,
            ),
            (
                "Delete ReShade",
                "Remove all ReShade-related files from the Anomaly bin directory.",
                self._delete_reshade,
            ),
            (
                "GOG fix-install",
                "Fix the ModOrganizer.ini paths for a GOG-provided install.",
                self._gog_fix,
            ),
            (
                "Open logs folder",
                "Open the CLI and launcher log directory in your file manager.",
                self._open_logs,
            ),
            (
                "Hash install (debug)",
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
        layout.addWidget(section_label("Reinstall / Uninstall", level=2))
        layout.addWidget(
            info_label(
                "Two guarded destructive actions. Both show an explicit warning "
                "listing the exact folders that will be deleted before anything "
                "happens."
            )
        )
        panels = QHBoxLayout()
        panels.setSpacing(14)

        fresh_panel, self.fresh_reset_button = self._destructive_panel(
            "Fresh Reset",
            "Wipes the Anomaly and G.A.M.M.A. folders and reinstalls both from "
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
            "Full Uninstall",
            "Removes the Anomaly, G.A.M.M.A. and cache folders, leaving your "
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

        self.fresh_reset_hint = info_label("Requires Anomaly and GAMMA to be installed.")
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

    def refresh(self) -> None:
        self.window.refresh_settings()
        self._update_fresh_reset_enabled()

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
        self.fresh_reset_hint.setVisible(not (fresh_reset_enabled or full_uninstall_enabled))

    def _require_profile(self) -> bool:
        if self.window.settings.active_profile is None:
            QMessageBox.warning(
                self, "No Profile", "Create or activate a profile first (Profiles page)."
            )
            return False
        return True

    def _confirm(self, text: str) -> bool:
        answer = QMessageBox.question(
            self, "Confirm", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _run(self, args: list[str], *, handler=None, confirm: str | None = None,
             on_finished=None) -> None:
        if self._runner is not None and self._runner.is_running():
            QMessageBox.information(self, "Busy", "A task is already running.")
            return
        if confirm is not None and not self._confirm(confirm):
            return
        self.summary.setText("")
        self.output.clear()
        self._prune_mb = 0
        self._set_buttons_enabled(False)
        runner = CommandRunner(cli_command(args), parent=self)
        self._runner = runner
        runner.line.connect(handler if handler is not None else self.output.append_line)
        runner.finished.connect(lambda rc, out: self._on_finished(rc, out, on_finished))
        runner.cancelled.connect(lambda: self.output.append_line("[cancelled]"))
        runner.start()

    def on_busy_changed(self, busy: bool) -> None:
        """Global install lock changed; re-evaluate this page's controls."""
        self._set_buttons_enabled(not busy)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in self.buttons:
            btn.setEnabled(enabled and not self.window.install_busy)
        self._update_fresh_reset_enabled()

    def _on_finished(self, rc: int, _output: str, on_finished) -> None:
        self._set_buttons_enabled(True)
        if on_finished:
            on_finished(rc, _output)
        elif rc != 0:
            self.output.append_line(f"[command exited with code {rc}]")

    # ----- fresh reset -----
    def _start_fresh_reset(self) -> None:
        if self._runner is not None and self._runner.is_running():
            QMessageBox.information(self, "Busy", "A task is already running.")
            return
        if self._wipe_task is not None:
            QMessageBox.information(self, "Busy", "A fresh reset is already in progress.")
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
        "<div style='color: #ff3333; text-align: center; font-weight: bold; font-size: 14px;'>"
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
            QMessageBox.information(self, "Busy", "A removal task is already in progress.")
            return
        if self.window.install_busy:
            QMessageBox.information(self, "Busy", "An install is already running.")
            return
        if not self._require_profile():
            return
        profile = self.window.settings.active_profile
        if not gamma_installed(profile.gamma):
            self._update_fresh_reset_enabled()
            return

        self._full_uninstall_targets = (profile.anomaly, profile.gamma, profile.cache)
        answer = QMessageBox.question(
            self,
            "Full Uninstall",
            "<html><body>"
            "<div style='font-weight: bold; font-size: 13px;'>WARNING</div><br>"
            "<div style='color: #ff3333; text-align: center; font-weight: bold; font-size: 14px;'>"
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
            "Are you sure you want to completely uninstall STALKER GAMMA?</body></html>",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.window.set_install_busy(True)
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
        self.summary.setText("STALKER GAMMA completely uninstalled")
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
