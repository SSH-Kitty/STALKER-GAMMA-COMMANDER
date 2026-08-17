"""Play page: the app's front page - launch the game through Mod Organizer.

A hero-style layout: a two-column grid lets the player pick the game target
("Launch Game") and the runner ("Select Runner") as equally important steps,
with the launch actions and a copyable command preview below.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..config import logs_dir
from ..launcher import (
    DEFAULT_PROTON_PREFIX,
    DEFAULT_UMU_PREFIX,
    LaunchError,
    Mo2Executable,
    available_commands,
    build_command,
    build_direct_command,
    default_launch_target,
    find_extra_protons,
    find_steam_protons,
    find_wine_versions,
    launch_detached,
    parse_mo2_executables,
    resolve_runner,
    runner_prefix_error,
)
from .common import info_label, make_card, section_label

RUNNER_LABELS = {
    "auto": "Auto-detect (Steam/UMU first, then Wine)",
    "umu": "Steam / UMU Proton (umu-run)",
    "wine": "Plain Wine (WINEPREFIX)",
}


class PlayPage(QWidget):
    launch_state_changed = Signal(bool)

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.executables: list[Mo2Executable] = []
        self._launching = False
        #: Steam Proton labels, refreshed with the runner combo so the chip row
        #: does not re-scan Steam libraries on every keystroke.
        self._proton_labels: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 32, 32, 24)
        root.setSpacing(16)

        # -- hero header ---------------------------------------------------
        hero = section_label("PLAY STALKER GAMMA", level=1)
        hero.setWordWrap(True)
        hero.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(hero)
        subtitle = info_label(
            "Play STALKER GAMMA, open Mod Organizer 2 to manage your mods, or "
            "launch the Anomaly engine directly. Select what to run and which "
            "runner to use, then press Launch Game."
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(subtitle)

        # -- config grid (launch game | select runner) ------------------------
        grid = QHBoxLayout()
        grid.setSpacing(16)

        target_card, target_layout = make_card()
        target_layout.setSpacing(12)
        target_layout.addWidget(section_label("Launch Game", level=2))
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Target:"))
        self.target_combo = QComboBox()
        self.target_combo.setMinimumHeight(34)
        self.target_combo.currentIndexChanged.connect(self._on_change)
        target_row.addWidget(self.target_combo, 1)
        target_layout.addLayout(target_row)
        self.target_path = QLabel("")
        self.target_path.setObjectName("dim")
        self.target_path.setWordWrap(True)
        self.target_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        target_layout.addWidget(self.target_path)
        grid.addWidget(target_card, 1)

        runner_card, runner_layout = make_card()
        runner_layout.setSpacing(12)
        runner_layout.addWidget(section_label("Select Runner", level=2))
        runner_row = QHBoxLayout()
        runner_row.addWidget(QLabel("Runner:"))
        self.runner_combo = QComboBox()
        self.runner_combo.currentIndexChanged.connect(self._on_runner_changed)
        runner_row.addWidget(self.runner_combo, 1)
        runner_layout.addLayout(runner_row)

        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel("Prefix path:"))
        self.prefix_edit = QLineEdit()
        self.prefix_edit.setPlaceholderText("Leave empty for the default prefix location")
        self.prefix_edit.editingFinished.connect(self._on_change)
        prefix_row.addWidget(self.prefix_edit, 1)
        runner_layout.addLayout(prefix_row)

        self.runner_path = QLabel("")
        self.runner_path.setObjectName("dim")
        self.runner_path.setWordWrap(True)
        self.runner_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        runner_layout.addWidget(self.runner_path)
        grid.addWidget(runner_card, 1)

        root.addLayout(grid)

        # -- status chip row -------------------------------------------------
        self.chips_row = QHBoxLayout()
        self.chips_row.setSpacing(8)
        root.addLayout(self.chips_row)

        # -- launch actions ---------------------------------------------------
        self.launch_button = QPushButton("Launch Game")
        self.launch_button.setObjectName("hero")
        self.launch_button.setToolTip(
            "Launch the selected target through Mod Organizer 2, with your mods loaded."
        )
        self.launch_button.clicked.connect(self._launch_via_mo2)
        root.addWidget(self.launch_button)

        secondary_row = QHBoxLayout()
        secondary_row.setSpacing(12)
        self.open_mo2_button = QPushButton("Open Mod Organizer 2")
        self.open_mo2_button.setObjectName("secondary")
        self.open_mo2_button.setToolTip(
            "Open the Mod Organizer 2 interface to manage mods and run executables."
        )
        self.open_mo2_button.clicked.connect(self._open_mo2)
        secondary_row.addWidget(self.open_mo2_button, 1)
        self.direct_button = QPushButton("Launch STALKER ANOMALY")
        self.direct_button.setObjectName("secondary")
        self.direct_button.setToolTip(
            "Run the selected game executable directly, without Mod Organizer or mods."
        )
        self.direct_button.clicked.connect(self._launch_direct)
        secondary_row.addWidget(self.direct_button, 1)
        root.addLayout(secondary_row)

        # -- command preview --------------------------------------------------
        preview_card, preview_layout = make_card()
        preview_row = QHBoxLayout()
        self.preview_label = QLabel("")
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.preview_label.setObjectName("mono")
        preview_row.addWidget(self.preview_label, 1)
        self.copy_button = QPushButton("Copy command")
        self.copy_button.clicked.connect(self._copy_command)
        preview_row.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignTop)
        preview_layout.addLayout(preview_row)
        root.addWidget(preview_card)

        root.addStretch(1)

        self.runner_status = info_label("")
        self.runner_status.setObjectName("dim")
        self.runner_status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(self.runner_status)

        self._load_state()
        self._reload_runners()
        self._reload_targets()
        self._refresh_preview()

    # ------------------------------------------------------------------ state
    def _load_state(self) -> None:
        state = gui_settings.load_gui_settings()
        prefixes = dict(state.get("prefixes") or {})
        if not prefixes and state.get("wine_prefix"):
            prefixes[state.get("runner", "auto")] = state["wine_prefix"]
            gui_settings.save_gui_settings(prefixes=prefixes)

    def _reload_runners(self) -> None:
        current = self.runner_combo.currentData()
        self.runner_combo.blockSignals(True)
        self.runner_combo.clear()
        self.runner_combo.addItem(RUNNER_LABELS["auto"], "auto")
        self.runner_combo.addItem(RUNNER_LABELS["umu"], "umu")
        self.runner_combo.addItem(RUNNER_LABELS["wine"], "wine")
        steam_protons = find_steam_protons()
        self._proton_labels = [label for label, _ in steam_protons]
        for label, path in steam_protons:
            self.runner_combo.addItem(label, f"proton:{path}")
        for label, path in find_extra_protons():
            self.runner_combo.addItem(label, f"umup:{path}")
        for label, path in find_wine_versions():
            self.runner_combo.addItem(label, f"wine:{path}")
        saved = gui_settings.load_gui_settings().get("runner", "auto")
        chosen = current
        if not chosen or self.runner_combo.findData(chosen) < 0:
            chosen = saved
        if self.runner_combo.findData(chosen) < 0:
            chosen = "auto"
        self.runner_combo.setCurrentIndex(self.runner_combo.findData(chosen))
        self.runner_combo.blockSignals(False)
        self.prefix_edit.setText(self._prefix_for(kind=self.runner_combo.currentData()))

    def _default_prefix(self, kind: str) -> str:
        return str(
            DEFAULT_PROTON_PREFIX
            if kind.startswith("proton:")
            else DEFAULT_UMU_PREFIX
        )

    def _prefix_for(self, *, kind: str) -> str:
        state = gui_settings.load_gui_settings()
        prefixes = state.get("prefixes") or {}
        return prefixes.get(kind) or self._default_prefix(kind)

    def _reload_targets(self) -> None:
        profile = self.window.settings.active_profile
        self.executables = (
            parse_mo2_executables(profile.gamma) if profile is not None else []
        )
        titles = [exe.title for exe in self.executables]
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItems(titles)
        preferred = gui_settings.load_gui_settings().get("target") or ""
        default = preferred if preferred in titles else default_launch_target(titles)
        if default:
            self.target_combo.setCurrentText(default)
        self.target_combo.blockSignals(False)

    def refresh(self) -> None:
        self.window.refresh_settings()
        self._reload_runners()
        self._reload_targets()
        self._refresh_preview()

    # ---------------------------------------------------------------- actions
    def _selected_target(self) -> str | None:
        title = self.target_combo.currentText()
        if title in [exe.title for exe in self.executables]:
            return title
        return None

    def _active_profile_name(self) -> str | None:
        profile = self.window.settings.active_profile
        if profile is None or not profile.mo2_profile:
            return None
        profiles_dir = Path(profile.gamma) / "profiles"
        if (profiles_dir / profile.mo2_profile).is_dir():
            return profile.mo2_profile
        return None

    def _runner(self):
        kind = self.runner_combo.currentData() or "auto"
        # Expand for every runner, not just plain Wine: '~' is equally invalid
        # as a Proton/umu prefix path.
        prefix = os.path.expanduser(self.prefix_edit.text().strip())
        runner = resolve_runner(kind, prefix)
        if gui_settings.load_gui_settings().get("always_gamemoderun"):
            gamemoderun = available_commands().get("gamemoderun")
            if gamemoderun and (not runner.wrapper or runner.wrapper[0] != gamemoderun):
                runner.wrapper.insert(0, gamemoderun)
        return runner

    def _resolve_command(self, *, open_mo2: bool, direct: bool, runner=None):
        profile = self.window.settings.active_profile
        if profile is None:
            raise LaunchError("No active profile. Configure a profile first.")
        if runner is None:
            runner = self._runner()
        if direct:
            target = self._selected_target()
            exe = next(
                (e for e in self.executables if e.title == target), Mo2Executable()
            )
            return build_direct_command(exe, runner)
        target = None if open_mo2 else self._selected_target()
        return build_command(
            profile.gamma, runner, target=target, profile=self._active_profile_name()
        )

    def _refresh_preview(self) -> None:
        # Resolve the runner once: each resolution probes the filesystem for
        # Steam libraries and Proton builds, and this runs on every edit.
        try:
            runner = self._runner()
            command, _, _ = self._resolve_command(
                open_mo2=False, direct=False, runner=runner
            )
        except LaunchError as exc:
            self.preview_label.setText(f"<i>{exc}</i>")
            self.preview_label.setStyleSheet("color: #d9a04c;")
            self.target_path.setText("")
            self.runner_path.setText("")
            self.launch_button.setEnabled(False)
            self.open_mo2_button.setEnabled(False)
            self.direct_button.setEnabled(False)
            self._build_chips(ok=False, runner=None)
            return
        self.preview_label.setText(shlex.join(command))
        self.preview_label.setStyleSheet("")
        self.launch_button.setEnabled(not self._launching)
        self.open_mo2_button.setEnabled(not self._launching)
        self.direct_button.setEnabled(
            not self._launching and bool(self._selected_target())
        )
        self._build_chips(ok=True, runner=runner)
        target = self._selected_target()
        exe = next(
            (e for e in self.executables if e.title == target), Mo2Executable()
        )
        self.target_path.setText(exe.binary or "No executable set for this target")
        prefix = (
            runner.env.get("STEAM_COMPAT_DATA_PATH")
            or runner.env.get("WINEPREFIX")
            or ""
        )
        parts = list(runner.wrapper)
        if prefix:
            parts.append(f"prefix: {prefix}")
        self.runner_path.setText(" ".join(parts) or runner.label)

    def _build_chips(self, *, ok: bool, runner=None) -> None:
        while self.chips_row.count():
            item = self.chips_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        chips: list[tuple[str, bool]] = []
        avail = available_commands()
        chips.append(("umu-run", bool(avail.get("umu"))))
        chips.append(("gamemoderun", bool(avail.get("gamemoderun"))))
        chips.append(("Wine", bool(avail.get("wine"))))
        protons = self._proton_labels
        chips.append(("Proton: " + ", ".join(protons) if protons else "Proton: none", bool(protons)))
        if runner is not None:
            chips.append((f"{runner.label} [{runner.kind}]", ok))
        self.chips_row.addStretch(1)
        for text, state in chips:
            chip = QLabel(text)
            chip.setObjectName("chip")
            chip.setProperty("state", "ok" if state else "bad")
            chip.style().unpolish(chip)
            chip.style().polish(chip)
            self.chips_row.addWidget(chip)
        self.chips_row.addStretch(1)

    def _copy_command(self) -> None:
        text = self.preview_label.text().strip()
        if text:
            QGuiApplication.clipboard().setText(text)

    def _save_state(self) -> None:
        runner = self.runner_combo.currentData()
        prefix = self.prefix_edit.text().strip()
        prefixes = dict(gui_settings.load_gui_settings().get("prefixes") or {})
        prefixes[runner] = prefix
        gui_settings.save_gui_settings(
            runner=runner,
            wine_prefix=prefix,
            prefixes=prefixes,
            target=self.target_combo.currentText(),
        )

    def _on_runner_changed(self, *_args) -> None:
        kind = self.runner_combo.currentData()
        self.prefix_edit.setText(self._prefix_for(kind=kind))
        self._save_state()
        self._refresh_preview()

    def _on_change(self, *_args) -> None:
        self._save_state()
        self._refresh_preview()

    def launch_game(self) -> None:
        """Launch the selected game target using the primary Play workflow."""
        self._launch_via_mo2()

    def _launch_via_mo2(self) -> None:
        if self._launching:
            return
        target = self._selected_target()
        if not target:
            QMessageBox.warning(
                self,
                "No target selected",
                "Choose which game to run from the Target list before clicking "
                "Launch Game.\n\n"
                "If the list is empty, make sure your active profile points to "
                "a GAMMA install and its ModOrganizer.ini is configured.",
            )
            self._set_launch_button_state(False)
            return
        self._set_launch_button_state(True)
        self._run(open_mo2=False, direct=False)

    def _open_mo2(self) -> None:
        if self._launching:
            return
        self._set_launch_button_state(True)
        self._run(open_mo2=True, direct=False)

    def _launch_direct(self) -> None:
        if self._launching:
            return
        self._set_launch_button_state(True)
        self._run(open_mo2=False, direct=True)

    def _run(self, *, open_mo2: bool, direct: bool) -> None:
        """Launch the game through Mod Organizer 2 or directly."""
        log_path = logs_dir() / "launcher.log"
        # launch_detached raises LaunchError too (spawn failures); if that
        # escapes, _launching stays True and every launch button stays dead.
        try:
            command, env, cwd = self._resolve_command(open_mo2=open_mo2, direct=direct)
            label = (
                "STALKER ANOMALY"
                if direct
                else ("Mod Organizer 2" if open_mo2 else "STALKER GAMMA")
            )
            self._proc = launch_detached(command, env, cwd, log_path=log_path)
        except LaunchError as exc:
            self._set_result(f"Could not launch: {exc}", error=True)
            QMessageBox.warning(self, "Could not launch", str(exc))
            self._set_launch_button_state(False)
            return
        # Use a repeating timer (1 s) to detect when the process exits,
        # so buttons are re-enabled as soon as possible regardless of timing.
        self._launch_timer = QTimer(self)
        self._launch_timer.setInterval(1000)
        self._launch_timer.timeout.connect(
            lambda: self._on_launch_check(label, command, log_path)
        )
        self._launch_timer.start()
        self._set_result(f"Launching {label}...")

    def _on_launch_check(
        self, label: str, command: list[str], log_path: Path
    ) -> None:
        """Called every second -- if the process has exited, re-enable buttons."""
        timer = getattr(self, "_launch_timer", None)
        if getattr(self, "_proc", None) is None:
            if timer is not None:
                timer.stop()
            return
        if self._proc.poll() is not None:
            # Process has exited -- re-enable buttons and stop timer
            self._launch_timer.stop()
            self._launch_timer.deleteLater()
            self._launch_timer = None
            self._set_launch_button_state(False)
            code = self._proc.returncode
            if code == 0:
                self._set_result(f"{label} closed normally.")
            else:
                detail = self._log_tail(log_path)
                msg = f"{label} exited with an error (code {code})"

                msg += f"\n\nCommand: {shlex.join(command)}"
                if detail:
                    msg += f"\n\nLast log lines:\n{detail}"
                self._set_result(msg, error=True)
                if runner_prefix_error(detail):
                    runner = self._runner()
                    compatibility_message = (
                        "The selected Wine/Proton runner could not use the configured "
                        "prefix correctly. This usually means the prefix was created "
                        "or is currently being used by a different runner version.\n\n"
                        f"Selected runner:\n{runner.label}\n\n"
                        f"Prefix:\n{self.prefix_edit.text().strip()}\n\n"
                        "Open the Play page and select the runner that created this "
                        "prefix, or configure a separate prefix for the selected "
                        "runner. Do not switch runners while the prefix is in use."
                    )
                    if "concrt140.dll" in detail.lower():
                        compatibility_message += (
                            "\n\nThe log also indicates that the required runtime "
                            "may be missing. Check Winetricks Configuration on the "
                            "Install page."
                        )
                    QMessageBox.warning(
                        self, "Runner/Prefix Compatibility Problem", compatibility_message
                    )
                else:
                    QMessageBox.warning(self, "Launch failed", msg)

    def _log_tail(self, path: Path, limit: int = 12) -> str:
        try:
            lines = path.read_text(
                encoding="utf-8", errors="replace"
            ).rstrip().splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-limit:])

    def _set_result(self, text: str, *, error: bool = False) -> None:
        color = "#d9a04c" if error else "#9fe96f"
        self.runner_status.setStyleSheet(f"color: {color};")
        self.runner_status.setText(text)

    def _set_launch_button_state(self, launching: bool) -> None:
        self._launching = launching
        self.launch_button.setEnabled(not launching)
        self.open_mo2_button.setEnabled(not launching)
        self.direct_button.setEnabled(not launching)
        self.launch_state_changed.emit(launching)
