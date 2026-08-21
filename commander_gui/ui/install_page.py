"""
Install page: Anomaly + GAMMA install (top), cache folder (middle),
winetricks and verify (bottom).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..cli_runner import cli_command
from ..dependencies import check_all_dependencies
from ..gui_settings import configured_wine_prefix
from ..integrity import (
    anomaly_status,
    fetch_official_mod_names,
    scan_mods_md5,
    verify_gamma,
)
from ..repair import (
    classify_problems,
    delete_mod_and_archive,
    fetch_modpack_records,
)
from ..settings import cli_ok
from ..winetricks import (
    WINETRICKS_VERBS,
    check_winetricks_full_status,
    protontricks_binary,
    protontricks_install_command,
    winetricks_install_command,
)
from .common import (
    OK_GREEN,
    STATUS_RED,
    BackgroundTask,
    CommandRunner,
    InstallStatusRow,
    ProgressArea,
    StreamTask,
    _kv_row,
    anomaly_installed,
    gamma_installed,
    info_label,
    make_card,
    mo2_running,
    section_label,
    winetricks_tooltip,
)

_CHECKBOXES = [
    *(
        (
            "minimal",
            "Minimal (~100GB)",
            "Delete addon archives after extraction to save ~50GB of disk space.",
        ),
        (
            "preserve_user",
            "Preserve user.ltx settings",
            "Keep your existing user.ltx (game options) across the install. If unchecked, controls, keybindings and mod-specific settings will be reset.",
        ),
        (
            "preserve_mcm",
            "Preserve MCM settings",
            "Keep your Mod Configuration Menu (MCM) settings across the install. If unchecked, all mod configurations (axr_options.ltx) will be lost.",
        ),
    )
]

_WT_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3})\s*%")


def _winetricks_progress(line: str, stage: str, completed: set[str]) -> int | None:
    """Extract a Winetricks percentage or approximate verb-stage progress."""
    clean = line.strip()
    match = _WT_PERCENT_RE.search(clean)
    if match:
        return min(100, int(match.group(1)))
    if stage != "verbs":
        return None
    for index, verb in enumerate(WINETRICKS_VERBS, start=1):
        if verb in clean and verb not in completed:
            completed.add(verb)
            return round(index / len(WINETRICKS_VERBS) * 100)
    return None


class InstallPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self._runner = None
        self._anomaly_runner = None
        self._auto_chain = False
        self._auto_cancelled = False
        self._verify_runner = None
        self._verify_task = None
        self._verify_counts = {"OK": 0, "CORRUPT": 0, "NOT FOUND": 0}
        self._verify_anomaly_ok = False
        self._scan_cancel = None
        self._presence = None
        self._repair_plan = None
        self._repair_records = {}
        self._repair_runner = None
        self._wt_runner = None
        self._wt_task = None
        self._wt_checking = False
        self._wt_installed: bool | None = None
        self._wt_stage = "verbs"
        self._wt_completed_verbs: set[str] = set()
        self._winetricks_status_enabled = False
        self._persisting = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(content)
        title = section_label("INSTALL ANOMALY + GAMMA", level=1)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(title)
        root.addWidget(
            info_label(
                "Install the STALKER Anomaly base game and the GAMMA Modpack. Progress, current activity, and full-install addon details appear below."
            )
        )
        split = QHBoxLayout()
        split.setSpacing(16)
        root.addLayout(split, 1)
        # -- Anomaly card (left) --
        self.anomaly_card, a_layout = make_card()
        split.addWidget(self.anomaly_card, 1)
        self.anomaly_status = InstallStatusRow("")
        a_layout.addWidget(self.anomaly_status)
        a_layout.addWidget(section_label("STALKER Anomaly", level=2))
        a_layout.addWidget(
            info_label(
                "<span style='color:#00ff00;'>Step 1.</span> Installs the core Anomaly 1.5.3 game files. Click to download, verify, and extract the official archive."
            )
        )
        details_group = QGroupBox("Installation details")
        details_layout = QVBoxLayout()
        details_layout.setSpacing(6)
        details_layout.addLayout(_kv_row("Version", "1.5.3"))
        details_layout.addLayout(_kv_row("Download size", "~9 GB"))
        details_layout.addLayout(_kv_row("Installed size", "~16 GB"))
        details_layout.addLayout(_kv_row("Required for", "GAMMA Modpack"))
        details_group.setLayout(details_layout)
        a_layout.addWidget(details_group)
        self.anomaly_edit, self.anomaly_browse, self.anomaly_folder_row = (
            self._make_folder_row("Anomaly folder:", self._browse_anomaly)
        )
        a_layout.addLayout(self.anomaly_folder_row)
        self.anomaly_button = QPushButton("Install Anomaly")
        self.anomaly_button.setObjectName("primary")
        # Wrapped: clicked() passes a bool that would land in skip_confirm.
        self.anomaly_button.clicked.connect(lambda: self._start_anomaly_install())
        a_layout.addWidget(self.anomaly_button)
        self.anomaly_progress = ProgressArea(show_table=False, show_log=False)
        self.anomaly_progress.cancel_button.clicked.connect(
            self._cancel_anomaly_install
        )
        a_layout.addWidget(self.anomaly_progress)
        # -- GAMMA card (right) --
        self.gamma_card, g_layout = make_card()
        split.addWidget(self.gamma_card, 1)
        self.gamma_status = InstallStatusRow("")
        g_layout.addWidget(self.gamma_status)
        g_layout.addWidget(section_label("GAMMA Modpack", level=2))
        g_layout.addWidget(
            info_label(
                "<span style='color:#00ff00;'>Step 2.</span> Installs the GAMMA modpack on top of your Anomaly installation (~150 GB). Click to download and all mods in sequence."
            )
        )
        self.gamma_edit, self.gamma_browse, self.gamma_folder_row = (
            self._make_folder_row("GAMMA folder:", self._browse_gamma)
        )
        g_layout.addLayout(self.gamma_folder_row)
        opts_group = QGroupBox("Install options")
        opts_layout = QVBoxLayout()
        opts_layout.setSpacing(6)
        self.checkboxes = {}
        for key, label, tooltip in _CHECKBOXES:
            cb = QCheckBox(label)
            cb.setToolTip(tooltip)
            self.checkboxes[key] = cb
            opts_layout.addWidget(cb)
        opts_group.setLayout(opts_layout)
        g_layout.addWidget(opts_group)
        g_layout.addWidget(section_label("Download cache", level=3))
        self.cache_edit, self.cache_browse, self.cache_folder_row = (
            self._make_folder_row("Cache folder:", self._browse_cache)
        )
        g_layout.addLayout(self.cache_folder_row)
        self.cache_info_label = info_label("")
        self.cache_info_label.setObjectName("dim")
        g_layout.addWidget(self.cache_info_label)
        self.install_button = QPushButton("Install GAMMA")
        self.install_button.setObjectName("primary")
        self.install_button.setToolTip(
            "Install or update GAMMA. Anomaly is installed first if it is missing."
        )
        # Wrapped: clicked() passes a bool that would land in skip_confirm.
        self.install_button.clicked.connect(lambda: self._start_full_install())
        g_layout.addWidget(self.install_button)
        self.full_progress = ProgressArea(show_log=False)
        self.full_progress.cancel_button.clicked.connect(self._cancel_full_install)
        g_layout.addWidget(self.full_progress, 1)
        self.wt_card, wt_layout = make_card()
        wt_layout.addWidget(section_label("Winetricks Configuration", level=2))
        wt_layout.addWidget(
            info_label(
                "<span style='color:#00ff00;'>Step 3.</span> Configure your Wine prefix. This installs the essential Microsoft Visual C++ and DirectX runtimes needed to run MO2 and Anomaly."
            )
        )
        self.wt_status = InstallStatusRow(
            "Winetricks runtimes", ok=None, pending_text="Checking"
        )
        wt_layout.addWidget(self.wt_status)
        self.wt_prefix_label = info_label("", wrap=False)
        self.wt_prefix_label.setObjectName("dim")
        wt_layout.addWidget(self.wt_prefix_label)
        self.winetricks_button = QPushButton("Install / Update Runtimes")
        self.winetricks_button.setObjectName("primary")
        self.winetricks_button.setToolTip(
            "Installs the native Microsoft Visual C++ and DirectX runtimes into "
            "the Wine prefix. Checks d3dcompiler_43, d3dcompiler_47, d3dx10, "
            "d3dx11_43, d3dx9, quartz, dx8vb, and vcrun2022. Enabled only while "
            "the runtimes are not installed."
        )
        self.winetricks_button.clicked.connect(self._start_winetricks)
        wt_layout.addWidget(self.winetricks_button)
        self.wt_progress = ProgressArea(show_table=False, show_log=True)
        self.wt_progress.cancel_button.clicked.connect(self._cancel_winetricks)
        wt_layout.addWidget(self.wt_progress, 1)
        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        root.addLayout(bottom)
        bottom.addWidget(self.wt_card, 1)
        self.verify_card, v_layout = make_card()
        bottom.addWidget(self.verify_card, 1)
        v_layout.addWidget(section_label("Verify Integrity", level=2))
        v_layout.addWidget(
            info_label(
                "<span style='color:#00ff00;'>Step 4.</span> Verify your game files. Run an MD5 check across Anomaly and GAMMA files. If any files or MO2 mods are missing/corrupted, the tool will automatically redownload and repair them."
            )
        )
        self.verify_button = QPushButton("Verify Integrity")
        self.verify_button.setObjectName("primary")
        self.verify_button.clicked.connect(self._start_verify)
        v_layout.addWidget(self.verify_button)
        self.verify_progress = ProgressArea(show_table=False, show_log=True)
        self.verify_progress.cancel_button.clicked.connect(self._cancel_verify)
        v_layout.addWidget(self.verify_progress)
        self.refresh()

    def enable_winetricks_status(self):
        self._winetricks_status_enabled = True
        self._refresh_winetricks_status()

    def refresh(self):
        self.window.refresh_settings()
        profile = self.window.settings.active_profile
        if profile is not None:
            self.anomaly_edit.setText(profile.anomaly)
            self.gamma_edit.setText(profile.gamma)
            self.cache_edit.setText(profile.cache)
            self._update_cache_info(profile.cache)
            if not self.window.install_busy:
                if anomaly_installed(profile.anomaly):
                    self.anomaly_progress.bar.setRange(0, 1)
                    self.anomaly_progress.bar.setValue(1)
                    self.anomaly_progress.bar.setFormat("Installed")
                if gamma_installed(profile.gamma):
                    self.full_progress.bar.setRange(0, 1)
                    self.full_progress.bar.setValue(1)
                    self.full_progress.bar.setFormat("Installed")
        else:
            self._update_cache_info("")
        self._update_install_status()
        self.wt_prefix_label.setText(f"Prefix: {self._wt_prefix()}")
        self._refresh_winetricks_status()
        self._update_button_states()

    def on_busy_changed(self, _busy):
        """Global install lock changed; re-evaluate this page's controls."""
        self._update_button_states()

    def _update_button_states(self):
        busy = self.window.install_busy
        profile = self.window.settings.active_profile
        if busy or profile is None:
            self.anomaly_button.setEnabled(False)
            self.install_button.setEnabled(False)
            self.verify_button.setEnabled(False)
            self.winetricks_button.setEnabled(False)
            self.anomaly_browse.setEnabled(False)
            self.gamma_browse.setEnabled(False)
            self.cache_browse.setEnabled(False)
            self.anomaly_edit.setReadOnly(True)
            self.gamma_edit.setReadOnly(True)
            self.cache_edit.setReadOnly(True)
            for cb in self.checkboxes.values():
                cb.setEnabled(False)
            return
        anomaly = anomaly_installed(profile.anomaly)
        self.anomaly_button.setEnabled(not anomaly)
        self.install_button.setEnabled(not gamma_installed(profile.gamma))
        self.verify_button.setEnabled(not mo2_running())
        self.winetricks_button.setEnabled(
            self._wt_installed is False and not mo2_running()
        )
        self.anomaly_browse.setEnabled(True)
        self.gamma_browse.setEnabled(True)
        self.cache_browse.setEnabled(True)
        self.anomaly_edit.setReadOnly(False)
        self.gamma_edit.setReadOnly(False)
        self.cache_edit.setReadOnly(False)
        for cb in self.checkboxes.values():
            cb.setEnabled(True)

    def _update_install_status(self):
        profile = self.window.settings.active_profile
        if profile is None:
            self.anomaly_status.set_state(None)
            self.gamma_status.set_state(None)
            return
        self.anomaly_status.set_state(anomaly_installed(profile.anomaly))
        self.gamma_status.set_state(gamma_installed(profile.gamma))

    def _update_cache_info(self, cache_path: str) -> None:
        if not cache_path:
            self.cache_info_label.setText("")
            return
        cache_dir = Path(cache_path)
        if not cache_dir.is_dir():
            self.cache_info_label.setText("No archives cached")
            self.cache_info_label.setStyleSheet(f"color: {STATUS_RED.name()};")
            return
        count = sum(1 for _ in cache_dir.glob("*.zip"))
        if count == 0:
            self.cache_info_label.setText("No archives cached")
            self.cache_info_label.setStyleSheet(f"color: {STATUS_RED.name()};")
        else:
            self.cache_info_label.setText(
                f"{count} archive{'s' if count != 1 else ''} cached"
            )
            self.cache_info_label.setStyleSheet(f"color: {OK_GREEN.name()};")

    def _make_folder_row(self, label_text, on_browse):
        edit = QLineEdit()
        edit.setPlaceholderText("Enter or browse to a folder...")
        edit.editingFinished.connect(self._persist_dirs)
        browse = QPushButton("Browse...")
        browse.clicked.connect(on_browse)
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        row.addWidget(edit, 1)
        row.addWidget(browse)
        return (edit, browse, row)

    def _browse_anomaly(self):
        start = str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Select Anomaly install folder", start
        )
        if folder:
            self.anomaly_edit.setText(folder)
            self._persist_dirs()
            return

    def _browse_gamma(self):
        start = str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Select GAMMA install folder", start
        )
        if folder:
            self.gamma_edit.setText(folder)
            self._persist_dirs()
            return

    def _browse_cache(self):
        start = str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Select cache folder", start)
        if folder:
            self.cache_edit.setText(folder)
            self._update_cache_info(folder)
            self._persist_dirs()
            return

    def _persist_dirs(self):
        # editingFinished fires on focus-out, and the message boxes below steal
        # focus - without this guard the handler re-enters itself.
        if self._persisting:
            return
        self._persisting = True
        try:
            self._persist_dirs_locked()
        finally:
            self._persisting = False

    def _persist_dirs_locked(self):
        self.window.refresh_settings()
        profile = self.window.settings.active_profile
        if profile is None:
            QMessageBox.warning(
                self,
                "No Profile",
                "Create or activate a profile first (Profiles page).",
            )
            self.refresh()
            return
        anomaly = self.anomaly_edit.text().strip()
        gamma = self.gamma_edit.text().strip()
        cache = self.cache_edit.text().strip()
        if not anomaly or not gamma:
            QMessageBox.warning(
                self,
                "Invalid Folder",
                "Both install folders must be set. Reverting to the saved paths.",
            )
            self.refresh()
            return
        if not cache:
            QMessageBox.warning(
                self,
                "Invalid Folder",
                "Cache folder must be set. Reverting to the saved path.",
            )
            self.refresh()
            return
        if (
            anomaly == profile.anomaly
            and gamma == profile.gamma
            and cache == profile.cache
        ):
            return
        profile.anomaly = anomaly
        profile.gamma = gamma
        profile.cache = cache
        try:
            self.window.settings.save()
        except OSError as exc:
            QMessageBox.warning(
                self, "Save Failed", f"Could not write settings.json:\n{exc}"
            )
            self.refresh()
            return
        self.window.refresh_settings()
        self._update_install_status()
        self.window.statusBar().showMessage(
            f"Install folders updated: {anomaly} | {gamma} | {cache}", 6000
        )
        return

    def _build_full_command(self):
        args = ["full-install"]
        if self.checkboxes["minimal"].isChecked():
            args.append("--minimal")
        if self.checkboxes["preserve_user"].isChecked():
            args.append("--preserve-user-settings")
        if self.checkboxes["preserve_mcm"].isChecked():
            args.append("--preserve-mcm-settings")
        return args

    def _start_full_install(self, skip_confirm=False):
        if self._runner is not None and self._runner.is_running():
            return
        if self.window.install_busy and not skip_confirm:
            QMessageBox.information(self, "Busy", "An install is already running.")
            return
        if self.window.settings.active_profile is None:
            QMessageBox.warning(
                self,
                "No Profile",
                "Create or activate a profile first (Profiles page).",
            )
            return
        minimal = self.checkboxes["minimal"].isChecked()
        size_hint = "~100GB" if minimal else "~150GB"
        if not skip_confirm:
            answer = QMessageBox.question(
                self,
                "Confirm Install GAMMA",
                "<html><body>"
                "This will install/update Anomaly (if not already installed) and all "
                f"GAMMA addons ({size_hint}). Existing installations are preserved."
                "<br><br>"
                "<span style='color: #ff3333; font-weight: bold;'>"
                "WARNING: Your user.ltx (keybindings, controls) and MCM settings will "
                "be overwritten unless you checked the preserve options above."
                "</span>"
                "<br><br>"
                "Continue?"
                "</body></html>",
                (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.window.set_install_busy(True)
        self.full_progress.reset()
        self.install_button.setEnabled(False)
        self.anomaly_button.setEnabled(False)
        cmd = cli_command(self._build_full_command(), progress_interval_ms=200)
        self._runner = CommandRunner(cmd, parent=self)
        self._runner.line.connect(self.full_progress.on_line)
        self._runner.finished.connect(self._on_full_finished)
        self._runner.cancelled.connect(
            lambda: self.full_progress.status_message("Cancelled")
        )
        self.full_progress.set_runner(self._runner)
        self.full_progress.on_started()
        self._runner.start()

    def _on_full_finished(self, rc, output):
        cancelled = self._runner is not None and self._runner.was_cancelled
        self.full_progress.on_finished(rc, output)
        self.full_progress.set_runner(None)
        if cancelled:
            self.full_progress.bar.setFormat("Cancelled")
            self.full_progress.status_message("Cancelled")
        elif not cli_ok(rc, output, ""):
            self.full_progress.status_message(f"Failed (exit code {rc})")
            self._show_error_popup("Install Failed", rc, output)
        self._update_install_status()
        self.window.set_install_busy(False)

    def _cancel_full_install(self):
        if self._runner is not None:
            if self.full_progress.is_paused:
                self._runner.resume()
            self._runner.cancel()
            return

    def _start_anomaly_install(self, skip_confirm=False):
        if self._anomaly_runner is not None and self._anomaly_runner.is_running():
            return
        if self.window.install_busy and not skip_confirm:
            QMessageBox.information(self, "Busy", "An install is already running.")
            return
        if self.window.settings.active_profile is None:
            QMessageBox.warning(
                self,
                "No Profile",
                "Create or activate a profile first (Profiles page).",
            )
            return
        if not skip_confirm:
            answer = QMessageBox.question(
                self,
                "Confirm Anomaly install",
                "Download and install STALKER Anomaly 1.5.3?",
                (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.window.set_install_busy(True)
        self.anomaly_progress.reset()
        self.anomaly_button.setEnabled(False)
        self.install_button.setEnabled(False)
        cmd = cli_command(["anomaly", "install"], progress_interval_ms=200)
        self._anomaly_runner = CommandRunner(cmd, parent=self)
        self._anomaly_runner.line.connect(self.anomaly_progress.on_line)
        self._anomaly_runner.finished.connect(self._on_anomaly_finished)
        self._anomaly_runner.cancelled.connect(
            lambda: self.anomaly_progress.status_message("Cancelled")
        )
        self._anomaly_runner.cancelled.connect(self._on_anomaly_cancelled)
        self.anomaly_progress.on_started()
        self._anomaly_runner.start()

    def start_auto_install(self):
        if self._runner is not None and self._runner.is_running():
            return False
        if self._anomaly_runner is not None and self._anomaly_runner.is_running():
            return False
        if self.window.settings.active_profile is None:
            return False
        self._auto_chain = True
        self._auto_cancelled = False
        self._start_anomaly_install(skip_confirm=True)
        return True

    def _on_anomaly_cancelled(self):
        self._auto_cancelled = True

    def _on_anomaly_finished(self, rc, output):
        cancelled = self._auto_cancelled or (
            self._anomaly_runner is not None and self._anomaly_runner.was_cancelled
        )
        self.anomaly_progress.on_finished(rc, output)
        if cancelled:
            self.anomaly_progress.status_message("Cancelled")
        elif not cli_ok(rc, output, ""):
            self.anomaly_progress.status_message(f"Failed (exit code {rc})")
            self._show_error_popup("Anomaly Install Failed", rc, output)
        self._update_install_status()
        chain = self._auto_chain
        # The chain is single-shot: clear it on every outcome so a failed
        # anomaly install cannot leave it armed for a later manual run.
        self._auto_chain = False
        if chain and not cancelled and cli_ok(rc, output, ""):
            self._start_full_install(skip_confirm=True)
            return
        self.window.set_install_busy(False)

    def _cancel_anomaly_install(self):
        if self._anomaly_runner is not None:
            self._anomaly_runner.cancel()
            return

    def _show_error_popup(self, title: str, rc: int, output: str) -> None:
        """Show the last meaningful lines of CLI output in a popup."""
        import re

        progress_re = re.compile(
            r"^\[\d{2}:\d{2}:\d{2}\]\s+.+?\s*\|.+\|\s*\d+(?:[.,]\d+)?\s*%\s*\|\s*\[\d+/\d+\]$"
        )
        lines = [
            l
            for l in (output or "").splitlines()
            if l.strip() and not progress_re.match(l)
        ]
        tail = "\n".join(lines[-20:]) if lines else "(no details available)"
        QMessageBox.warning(
            self,
            title,
            f"Exit code: {rc}\n\n{tail}",
        )

    def _start_verify(self):
        if self._verify_runner is not None and self._verify_runner.is_running():
            return
        if self.window.install_busy:
            QMessageBox.information(self, "Busy", "An install is already running.")
            return
        if mo2_running():
            # The game holds the Wine prefix; verification must not read it.
            return
        if self.window.settings.active_profile is None:
            QMessageBox.warning(
                self,
                "No Profile",
                "Create or activate a profile first (Profiles page).",
            )
            return
        self._verify_counts = {"OK": 0, "CORRUPT": 0, "NOT FOUND": 0}
        self._verify_anomaly_ok = False
        self._presence = None
        self._repair_plan = None
        self._repair_records = {}
        # Cleared so a cancelled *previous* repair cannot suppress this run.
        self._repair_runner = None
        self.window.set_install_busy(True)
        self.verify_progress.reset()
        self.verify_button.setEnabled(False)
        self.verify_progress.on_started()
        self.verify_progress.status_message("Checking Anomaly files...")
        self.verify_progress.log.append_line("== Anomaly integrity check ==")
        runner = CommandRunner(cli_command(["anomaly", "check"]), parent=self)
        runner.line.connect(self._on_verify_line)
        runner.finished.connect(self._on_anomaly_verify_finished)
        runner.cancelled.connect(self._on_verify_cancelled)
        self._verify_runner = runner
        runner.start()

    def _on_verify_line(self, line):
        self.verify_progress.on_line(line)
        status = anomaly_status(line)
        if status is not None:
            if status in self._verify_counts:
                self._verify_counts[status] += 1
                return
            return

    def _on_anomaly_verify_finished(self, rc, output):
        # 'finished' still arrives after a cancel (the CLI exits in response to
        # SIGINT), so the cancelled run must not fall through to the next stage.
        if self._verify_runner is not None and self._verify_runner.was_cancelled:
            return
        counts = self._verify_counts
        self.verify_progress.log.append_line("")
        parsed = counts["OK"] + counts["CORRUPT"] + counts["NOT FOUND"]
        # The CLI exits 0 on some failures, so trust it only when no failure
        # markers appear and at least one status line was actually parsed.
        if not cli_ok(rc, output, ""):
            self.verify_progress.log.append_line("Anomaly check failed.")
            self._verify_anomaly_ok = False
        elif parsed == 0:
            self.verify_progress.log.append_line(
                "Anomaly check produced no results - treated as failed."
            )
            self._verify_anomaly_ok = False
        else:
            self._verify_anomaly_ok = counts["NOT FOUND"] == 0
            self.verify_progress.log.append_line(
                f"Anomaly: {counts['OK']} OK, {counts['CORRUPT']} CORRUPT, {counts['NOT FOUND']} NOT FOUND"
            )
        self._start_gamma_verify()

    def _start_gamma_verify(self):
        self._scan_cancel = threading.Event()
        self.verify_progress.cancel_button.show()
        self.verify_progress.status_message("Checking GAMMA mods...")
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== GAMMA integrity check ==")
        task = StreamTask(self._run_gamma_verify, parent=self)
        task.line.connect(self._on_gamma_verify_progress)
        task.result.connect(self._on_gamma_verify_done)
        task.error.connect(self._on_gamma_verify_error)
        self._verify_task = task
        task.start()

    def _run_gamma_verify(self, report):
        profile = self.window.settings.active_profile
        if profile is None:
            raise RuntimeError("No active profile")
        report("Downloading official GAMMA mod list...")
        official = fetch_official_mod_names(profile.mod_list_url)
        if official is None:
            report("Official mod list not reachable - showing total counts only")
        presence = verify_gamma(
            profile.gamma,
            profile.mo2_profile,
            on_progress=(
                lambda done, total, name: report(
                    f"Checking GAMMA mod {done}/{total}: {name}"
                )
            ),
            official_mods=official,
        )
        report("Starting full MD5 scan of mod files...")
        scan = scan_mods_md5(
            profile.gamma,
            on_progress=(
                lambda done, total, size: report(
                    f"MD5 hashing {done}/{total} files ({size})"
                )
            ),
            cancel=self._scan_cancel,
        )
        plan = None
        records = {}
        if scan.problems and not scan.cancelled and not scan.created:
            report("Looking up download sources for broken mods...")
            records = fetch_modpack_records(profile.mod_pack_maker_url)
            plan = classify_problems(scan, records)
        return (presence, scan, plan, records)

    def _on_gamma_verify_progress(self, text):
        self.verify_progress.status_message(text)
        self.verify_progress.log.append_line(text)

    def _on_gamma_verify_done(self, result):
        presence, scan, plan, records = result
        self._presence = presence
        self._repair_plan = plan
        self._repair_records = records
        for line in presence.lines():
            self.verify_progress.log.append_line(line)
        for line in scan.lines():
            self.verify_progress.log.append_line(line)
        counts = self._verify_counts
        anomaly_ok = self._verify_anomaly_ok and counts["CORRUPT"] == 0
        presence_ok = presence.problems == 0
        if scan.cancelled:
            self._finish_verify(
                ok=False,
                message="Verify cancelled during the GAMMA MD5 scan.",
                summary="Verify cancelled",
            )
            return
        if presence_ok and scan.problems == 0 and anomaly_ok:
            self._finish_verify(
                ok=True,
                message=self._gamma_ok_message(repaired=0),
                summary=presence.summary,
            )
            return
        if plan is not None and plan.has_repairable:
            self._prompt_repair()
            return
        self._finish_with_issues()

    def _gamma_ok_message(self, repaired):
        if repaired:
            return f"Confirmed: GAMMA repaired ({repaired} mod(s)) and verified successfully."
        return "Confirmed: Anomaly and GAMMA verified successfully."

    def _finish_with_issues(self):
        plan = self._repair_plan
        if plan is not None:
            if plan.unrepairable:
                shown = ", ".join(plan.unrepairable[:5])
                if len(plan.unrepairable) > 5:
                    shown += f"... (+{(len(plan.unrepairable) - 5)} more)"
                self.verify_progress.log.append_line(
                    f"Not repairable ({len(plan.unrepairable)}): no download source found - {shown}"
                )
            if plan.added_only:
                self.verify_progress.log.append_line(
                    f"Left untouched ({len(plan.added_only)} mod(s) with added files - your own edits are kept)."
                )
        message = "Verify finished with issues - Anomaly and/or GAMMA are not fully verified (see details above)."
        self._finish_verify(ok=False, message=message, summary="GAMMA has issues")

    def _prompt_repair(self):
        plan = self._repair_plan
        names = plan.repairable
        shown = "\n".join(f"  - {name}" for name in names[:8])
        if len(names) > 8:
            shown += f"\n  ... and {(len(names) - 8)} more"
        answer = QMessageBox.question(
            self,
            "Repair Broken Mods",
            f"Verify Integrity found {len(names)} broken mod(s):\n\n{shown}\n\nRepair now? This permanently deletes each mod folder and its cached archive, then re-downloads and re-installs it (MD5-verified).\n\nExtra mods and your own added files are never touched.",
            (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._finish_with_issues()
            return
        self._start_repair()

    def _start_repair(self):
        self.verify_progress.cancel_button.show()
        self.verify_progress.status_message("Deleting broken mods...")
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Repairing GAMMA mods ==")
        task = StreamTask(self._run_repair_deletion, parent=self)
        task.line.connect(self._on_gamma_verify_progress)
        task.result.connect(self._on_repair_deleted)
        task.error.connect(self._on_gamma_verify_error)
        self._verify_task = task
        task.start()

    def _run_repair_deletion(self, report):
        profile = self.window.settings.active_profile
        if profile is None:
            raise RuntimeError("No active profile")
        removed = []
        for folder in self._repair_plan.repairable:
            if self._scan_cancel is not None and self._scan_cancel.is_set():
                report("Repair cancelled - deletion aborted.")
                return removed
            report(f"Deleting {folder}")
            removed.extend(
                delete_mod_and_archive(
                    profile.gamma, folder, self._repair_records.get(folder)
                )
            )
        return removed

    def _on_repair_deleted(self, removed):
        if self._scan_cancel is not None and self._scan_cancel.is_set():
            self.verify_progress.log.append_line("Repair cancelled before reinstall.")
            self._finish_verify(
                ok=False, message="Repair cancelled.", summary="Repair cancelled"
            )
            return
        self.verify_progress.log.append_line(
            f"Deleted {len(removed)} folder(s)/archive(s)"
        )
        self._start_repair_install()

    def _start_repair_install(self):
        self.verify_progress.status_message(
            "Re-downloading and re-installing broken mods..."
        )
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Running installer (repair) ==")
        args = ["full-install", "--skip-extract-on-hash-match"]
        runner = CommandRunner(cli_command(args, progress_interval_ms=200), parent=self)
        runner.line.connect(self._on_verify_line)
        runner.finished.connect(self._on_repair_install_finished)
        runner.cancelled.connect(self._on_repair_install_cancelled)
        self._verify_runner = runner
        self._repair_runner = runner
        runner.start()

    def _on_repair_install_finished(self, rc, output):
        # A cancelled repair must not fall through to the post-scan: that scan
        # re-baselines the MD5 manifest and would record the broken state as
        # the new reference.
        if self._repair_runner is not None and self._repair_runner.was_cancelled:
            return
        self.verify_progress.log.append_line("")
        if not cli_ok(rc, output, ""):
            self.verify_progress.log.append_line("Repair install failed.")
            self._finish_verify(
                ok=False,
                message="Repair install failed - GAMMA is not fully repaired (see details above).",
                summary="Repair failed",
            )
            return
        self._start_post_scan()

    def _on_repair_install_cancelled(self):
        self.verify_progress.log.append_line("Repair install cancelled")
        self._finish_verify(
            ok=False, message="Repair cancelled.", summary="Repair cancelled"
        )

    def _start_post_scan(self):
        self.verify_progress.status_message("Re-checking GAMMA mods...")
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Re-checking after repair ==")
        task = StreamTask(self._run_post_scan, parent=self)
        task.line.connect(self._on_gamma_verify_progress)
        task.result.connect(self._on_post_scan_done)
        task.error.connect(self._on_gamma_verify_error)
        self._verify_task = task
        task.start()

    def _run_post_scan(self, report):
        profile = self.window.settings.active_profile
        if profile is None:
            raise RuntimeError("No active profile")
        post = scan_mods_md5(
            profile.gamma,
            on_progress=(
                lambda done, total, size: report(
                    f"MD5 hashing {done}/{total} files ({size})"
                )
            ),
            cancel=self._scan_cancel,
            rebaseline=True,
        )
        if post.cancelled:
            return (post, None)
        report("Re-checking GAMMA mods are present...")
        presence = verify_gamma(
            profile.gamma,
            profile.mo2_profile,
            on_progress=(
                lambda done, total, name: report(
                    f"Checking GAMMA mod {done}/{total}: {name}"
                )
            ),
        )
        return (post, presence)

    def _on_post_scan_done(self, result):
        post, presence = result
        for line in post.lines():
            self.verify_progress.log.append_line(line)
        if presence is not None:
            for line in presence.lines():
                self.verify_progress.log.append_line(line)
        counts = self._verify_counts
        anomaly_ok = self._verify_anomaly_ok and counts["CORRUPT"] == 0
        if post.cancelled:
            self._finish_verify(
                ok=False,
                message="Verify cancelled during the post-repair scan.",
                summary="Verify cancelled",
            )
            return
        repaired = len(self._repair_plan.repairable) if self._repair_plan else 0
        remaining = post.problems + (presence.problems if presence is not None else 0)
        if remaining == 0:
            self._finish_verify(
                ok=anomaly_ok,
                message=self._gamma_ok_message(repaired=repaired),
                summary=f"GAMMA repaired ({repaired} mod(s))",
            )
            return
        self.verify_progress.log.append_line(
            f"{remaining} problem(s) remain after repair."
        )
        self._finish_with_issues()

    def _on_gamma_verify_error(self, message):
        self.verify_progress.log.append_line(f"GAMMA check failed: {message}")
        self._finish_verify(
            ok=False,
            message="Verify failed - Anomaly and/or GAMMA are not fully verified.",
            summary="GAMMA check failed",
        )

    def _finish_verify(self, ok, message, summary):
        self.verify_button.setEnabled(True)
        self.verify_progress.cancel_button.hide()
        self._scan_cancel = None
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line(message)
        if ok:
            self.verify_progress.set_success_state("Verified successfully")
        else:
            self.verify_progress.on_finished(1, "")
        self.verify_progress.status_message(message)
        self.window.statusBar().showMessage(summary, 8000)
        self.window.set_install_busy(False)

    def _cancel_verify(self):
        if self._verify_runner is not None:
            self._verify_runner.cancel()
        if self._scan_cancel is not None:
            self._scan_cancel.set()
            return

    def _on_verify_cancelled(self):
        self.verify_progress.log.append_line("Anomaly check cancelled")
        self.verify_button.setEnabled(True)
        self.verify_progress.status_message("Cancelled")
        self.verify_progress.cancel_button.hide()
        self.window.set_install_busy(False)

    def _wt_prefix(self):
        # Resolves STEAM_COMPAT_DATA_PATH -> <path>/pfx for Proton runners, so
        # winetricks acts on the prefix the game actually uses.
        return configured_wine_prefix()

    def _paused_status(self):
        """Status shown while the game is running.

        The game cannot start without the runtimes, so it stays "Installed";
        the live winetricks query is unreliable against a running prefix (it
        can even report everything missing), so it is paused until the game
        closes and this page next refreshes.
        """
        paused = {verb: True for verb in WINETRICKS_VERBS}
        paused["wine"] = True
        paused["protontricks"] = True
        total = len(paused)
        self._wt_installed = True
        self.wt_status.set_state(
            True, f"{total}/{total} dependencies installed (paused - game running)"
        )
        self.wt_status.set_status_tooltip(winetricks_tooltip(paused))
        self.wt_progress.set_installed_state(
            True, f"{total}/{total} dependencies installed (paused - game running)"
        )
        self._update_button_states()

    def _refresh_winetricks_status(self):
        if not self._winetricks_status_enabled:
            return
        if self._wt_checking:
            return
        if mo2_running():
            self._paused_status()
            return
        self._wt_installed = None
        self._update_button_states()
        self._wt_checking = True
        task = BackgroundTask(
            check_winetricks_full_status, self._wt_prefix(), parent=self
        )
        task.result.connect(self._on_winetricks_status)
        task.error.connect(self._on_winetricks_status_error)
        self._wt_task = task
        task.start()

    def _on_winetricks_status(self, status):
        self._wt_checking = False
        if mo2_running():
            # The game started while the check was in flight; the result is stale.
            self._paused_status()
            return
        installed = sum(1 for ok in status.values() if ok)
        total = len(status)
        self._wt_installed = installed == total
        self.wt_status.set_state(
            installed == total, f"{installed}/{total} dependencies installed"
        )
        self.wt_status.set_status_tooltip(winetricks_tooltip(status))
        self.wt_progress.set_installed_state(
            installed == total, f"{installed}/{total} dependencies installed"
        )
        self._update_button_states()

    def _on_winetricks_status_error(self, message):
        # Must clear the in-flight flag, or the status never refreshes again.
        self._wt_checking = False
        if mo2_running():
            self._paused_status()
            return
        self._wt_installed = None
        self.wt_status.set_state(None, "status unavailable", pending_text="Unknown")
        self.wt_status.set_status_tooltip(f"Could not query winetricks: {message}")
        self.wt_progress.set_installed_state(None, "Status unavailable")
        self._update_button_states()

    def _start_winetricks(self):
        self._winetricks_status_enabled = True
        if self._wt_runner is not None and self._wt_runner.is_running():
            return
        if self.window.install_busy:
            QMessageBox.information(self, "Busy", "An install is already running.")
            return
        if mo2_running():
            # The game holds the Wine prefix; winetricks must not touch it.
            return
        errors = check_all_dependencies()
        if errors:
            QMessageBox.warning(
                self,
                "Missing Dependencies",
                "The following are required but not found:\n\n"
                + "\n\n".join(errors)
                + "\n\nInstall them and try again.",
            )
            return
        prefix = self._wt_prefix()
        need_protontricks = not protontricks_binary()
        message = f"This installs the runtime libraries needed by Mod Organizer and the game into:\n\n{prefix}\n\nVerbs: {', '.join(WINETRICKS_VERBS)}\n(~150 MB download on first run)."
        if need_protontricks:
            message += """

    protontricks is missing and will be installed first."""
        answer = QMessageBox.question(
            self,
            "Confirm Winetricks Configuration",
            message,
            (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._wt_stage = "tools" if need_protontricks else "verbs"
        self._wt_completed_verbs.clear()
        self.window.set_install_busy(True)
        self.wt_progress.reset()
        self._start_winetricks_stage()

    def _start_winetricks_stage(self):
        self.wt_progress.on_started()
        self.wt_progress.bar.setRange(0, 100)
        self.wt_progress.bar.setValue(0)
        self.wt_progress.bar.setFormat("%p%")
        tools_stage = self._wt_stage == "tools"
        command = (
            protontricks_install_command()
            if tools_stage
            else winetricks_install_command()
        )
        if tools_stage and not command:
            from ..dependencies import _externally_managed, _install_command

            msg = "protontricks could not be installed."
            if _externally_managed():
                cmd = _install_command("pipx")
                msg += (
                    "\n\nThis system marks Python as externally managed "
                    "(PEP 668), so pip cannot install packages directly."
                    f"\n\nInstall pipx first:\n  {cmd}\n\n"
                    "Then try again — protontricks will be installed automatically."
                )
            else:
                msg += "\n\nInstall pipx or pip, then try again."
            self.wt_progress.on_finished(1, msg)
            self._wt_runner = None
            self.window.set_install_busy(False)
            return
        if tools_stage:
            self.wt_progress.status_message("Installing protontricks...")
            self.wt_progress.log.append_line("== Installing protontricks ==")
        else:
            self.wt_progress.status_message("Installing runtimes...")
            self.wt_progress.log.append_line(
                "== Winetricks: " + " ".join(WINETRICKS_VERBS) + " =="
            )
        self._wt_runner = CommandRunner(
            command,
            env={"WINEPREFIX": self._wt_prefix(), "WINEDEBUG": "-all"},
            parent=self,
        )
        self._wt_runner.line.connect(self._on_winetricks_line)
        self._wt_runner.finished.connect(self._on_winetricks_finished)
        self._wt_runner.cancelled.connect(self._on_winetricks_cancelled)
        self._wt_runner.start()

    def _on_winetricks_line(self, line):
        self.wt_progress.on_line(line)
        clean = line.strip()
        if not clean:
            return
        # Skip noisy winetricks/gamemode lines.
        if clean.startswith(("Using winetricks", "gamemodeauto")):
            return
        pct = _winetricks_progress(clean, self._wt_stage, self._wt_completed_verbs)
        if pct is not None:
            self.wt_progress.bar.setRange(0, 100)
            self.wt_progress.bar.setValue(pct)
            self.wt_progress.bar.setFormat("%p%")

    def _on_winetricks_finished(self, rc, output):
        # 'finished' still arrives after a cancel; don't overwrite the
        # 'Cancelled' status with a spurious failure.
        if self._wt_runner is not None and self._wt_runner.was_cancelled:
            self._wt_runner = None
            self.window.set_install_busy(False)
            self._refresh_winetricks_status()
            return
        if self._wt_stage == "tools" and rc == 0:
            self._wt_stage = "verbs"
            self._wt_runner = None
            # Do not call on_finished — the verbs stage continues directly.
            self._start_winetricks_stage()
            return
        self.wt_progress.on_finished(rc, output)
        if rc == 0:
            self.wt_progress.status_message("Complete - runtimes installed")
        else:
            self.wt_progress.status_message(f"Failed (exit code {rc})")
        self._wt_runner = None
        self.window.set_install_busy(False)
        self._refresh_winetricks_status()

    def _on_winetricks_cancelled(self):
        self.wt_progress.log.append_line("Winetricks cancelled")
        self.wt_progress.status_message("Cancelled")

    def _cancel_winetricks(self):
        if self._wt_runner is None or not self._wt_runner.is_running():
            return
        answer = QMessageBox.question(
            self,
            "Cancel Winetricks Configuration",
            "Winetricks is downloading or installing runtime dependencies.\n\n"
            "Cancel the operation? The prefix may be left partially configured.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.wt_progress.cancel_button.setEnabled(False)
        self.wt_progress.cancel_button.setText("Cancelling...")
        self.wt_progress.status_message("Cancelling Winetricks...")
        self._wt_runner.cancel()
