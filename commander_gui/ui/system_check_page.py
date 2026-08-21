"""System readiness checks and manual dependency guidance."""

from __future__ import annotations

import os
import platform
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..cli_runner import cli_binary_path
from ..dependencies import (
    configured_tool,
    detect_distro_id,
    detect_package_manager,
    install_command,
)
from ..gui_settings import configured_wine_prefix
from ..launcher import find_extra_protons
from ..winetricks import WINETRICKS_VERBS, check_winetricks_status, winetricks_binary
from .common import (
    BackgroundTask,
    clear_layout,
    info_label,
    make_card,
    section_label,
)


def _manual_command(tool: str, manager: str | None) -> str:
    if tool == "Steam":
        return install_command("steam")
    if tool in {"Wine", "Winetricks"}:
        return install_command(tool.lower())
    if tool == "Protontricks":
        return install_command("pipx") + " && pipx install protontricks"
    if tool == "Vulkan":
        commands = {
            "apt": "sudo apt install mesa-vulkan-drivers",
            "dnf": "sudo dnf install mesa-vulkan-drivers",
            "pacman": "sudo pacman -S vulkan-radeon",
            "zypper": "sudo zypper install Mesa-vulkan-drivers",
        }
        return commands.get(manager or "", "Install your GPU vendor's Vulkan drivers")
    if tool == "umu-run":
        return "Install umu-launcher from https://github.com/Open-Wine-Components/umu-launcher"
    return ""


def _check_tool(label: str, command: str, manager: str | None) -> dict[str, str]:
    found = configured_tool(command) or shutil.which(command)
    return {
        "label": label,
        "state": "ready" if found else "missing",
        "detail": found or f"{command} was not found on PATH.",
        "command": _manual_command(label, manager),
        "detected": found,
        "override_key": command,
        "manager": "" if found else (manager or ""),
    }


def _short_proton_name(label: str) -> str:
    """Turn a discovered runner label into a compact, path-free name."""
    ver = label.removeprefix("GE-Proton").removeprefix("Proton ").strip()
    return f"GE-Proton {ver}" if ver else "GE-Proton Unknown"


def _winetricks_checks(status: dict[str, bool], binary: str) -> list[dict[str, str]]:
    """Build readiness rows for each runtime verb in the active prefix."""
    return [
        {
            "label": verb,
            "state": "ready" if status.get(verb, False) else "missing",
            "detail": (
                "Installed in the active Wine/Proton prefix."
                if status.get(verb, False)
                else "Not installed in the active Wine/Proton prefix."
            ),
            "command": f"{binary or 'winetricks'} -q {verb}",
        }
        for verb in WINETRICKS_VERBS
    ]


def _collect_checks() -> tuple[list[dict[str, str]], bool, dict[str, str]]:
    manager = detect_package_manager()
    checks: list[dict[str, str]] = []
    binary = cli_binary_path()
    checks.append(
        {
            "label": "GAMMA CLI",
            "state": "ready"
            if binary.is_file() and os.access(binary, os.X_OK)
            else "missing",
            "detail": str(binary),
            "command": "",
        }
    )
    checks.append(
        {
            "label": "Linux system",
            "state": "ready",
            "detail": f"{detect_distro_id() or 'Unknown distribution'} / {platform.machine()}",
            "command": "",
        }
    )
    checks.extend(
        [
            _check_tool("Steam", "steam", manager),
            _check_tool("umu-run", "umu-run", manager),
            _check_tool("Winetricks", "winetricks", manager),
            _check_tool("Protontricks", "protontricks", manager),
            _check_tool("Vulkan", "vulkaninfo", manager),
        ]
    )
    try:
        prefix = configured_wine_prefix()
        winetricks = winetricks_binary()
        runtime_status = check_winetricks_status(prefix)
    except (OSError, RuntimeError, ValueError):
        winetricks = ""
        runtime_status = {verb: False for verb in WINETRICKS_VERBS}
    checks.extend(_winetricks_checks(runtime_status, winetricks))
    try:
        extra_protons = find_extra_protons()
    except (OSError, RuntimeError):
        extra_protons = []
    detected_overrides: dict[str, str] = {
        check.get("override_key", check["label"]): check.get("detected", "")
        for check in checks
        if check.get("detected")
    }
    if "steam_root" not in detected_overrides:
        from ..launcher import STEAM_ROOT_CANDIDATES

        for candidate in STEAM_ROOT_CANDIDATES:
            try:
                resolved = candidate.resolve()
            except (OSError, RuntimeError):
                continue
            if resolved.is_dir():
                detected_overrides["steam_root"] = str(resolved)
                break
    if extra_protons:
        detected_overrides["umu_proton"] = str(Path(extra_protons[0][1]).parent)
    ge_builds = [(_short_proton_name(label), "ready") for label, _path in extra_protons]
    checks.append(
        {
            "label": "Proton Builds",
            "state": "ready" if ge_builds else "optional",
            "detail": f"{len(ge_builds)} GE-Proton build(s) detected."
            if ge_builds
            else "No GE-Proton builds detected.",
            "command": "",
            "builds": ge_builds,
        }
    )
    gamemode_found = configured_tool("gamemoderun") or shutil.which("gamemoderun")
    checks.append(
        {
            "label": "GameMode",
            "state": "ready" if gamemode_found else "optional",
            "detail": "Optional performance helper.",
            "command": "" if gamemode_found else install_command("gamemode"),
            "manager": "" if gamemode_found else (manager or ""),
        }
    )
    mangohud_found = configured_tool("mangohud") or shutil.which("mangohud")
    checks.append(
        {
            "label": "MangoHud",
            "state": "ready" if mangohud_found else "optional",
            "detail": "Optional performance overlay.",
            "command": "" if mangohud_found else install_command("mangohud"),
            "manager": "" if mangohud_found else (manager or ""),
        }
    )
    # Cross-reference checks with saved manual overrides — if the user set a
    # custom path that is invalid, downgrade the check to "missing".
    saved_overrides = gui_settings.load_gui_settings().get("tool_overrides") or {}
    for check in checks:
        key = check.get("override_key", "")
        # Directory overrides (steam_root, umu_proton)
        if key == "steam_root" or (key == "steam" and "steam_root" in saved_overrides):
            dir_path = saved_overrides.get("steam_root", "")
            if dir_path and not Path(dir_path).is_dir():
                check["state"] = "missing"
                check["detail"] = f"Steam library override is invalid: {dir_path}"
                check["command"] = ""
        if key == "umu_proton" or (
            key == "umu-run" and "umu_proton" in saved_overrides
        ):
            dir_path = saved_overrides.get("umu_proton", "")
            if dir_path and not Path(dir_path).is_dir():
                check["state"] = "missing"
                check["detail"] = f"GE-Proton override path is invalid: {dir_path}"
                check["command"] = ""
        # File overrides (umu-run, winetricks, protontricks, vulkaninfo)
        if (
            key in saved_overrides
            and saved_overrides[key]
            and key not in ("steam_root", "umu_proton")
        ):
            file_path = saved_overrides[key]
            if not Path(file_path).is_file() or not os.access(file_path, os.X_OK):
                check["state"] = "missing"
                check["detail"] = f"Override path is not executable: {file_path}"
                check["command"] = ""
    required_missing = any(item["state"] == "missing" for item in checks)
    return checks, not required_missing, detected_overrides


class SystemCheckPage(QWidget):
    """Read-only system checks with commands the user can run manually."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._task: BackgroundTask | None = None
        self._override_edits: dict[str, QLineEdit] = {}
        self._override_checks: dict[str, QCheckBox] = {}
        self._override_browse: dict[str, QPushButton] = {}
        self._detected_overrides: dict[str, str] = {}
        self._status_labels: dict[str, QLabel] = {}
        self._pending_result: (
            tuple[list[dict[str, str]], bool, dict[str, str]] | None
        ) = None
        self._pending_error: str | None = None
        self._pending_timer: QTimer | None = None
        self._refresh_start: float = 0.0
        self._min_check_seconds: float = 2.0

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

        title = section_label("SYSTEM CHECK", level=1)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(title)
        subtitle = info_label(
            "Verify that all required tools, runners, and dependencies are "
            "installed before setting up the game."
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(subtitle)

        self.summary = QLabel("Checking system readiness...")
        self.summary.setObjectName("accent")
        self.summary.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(self.summary)

        self.checking_bar = QProgressBar()
        self.checking_bar.setRange(0, 0)
        self.checking_bar.setTextVisible(False)
        self.checking_bar.setMaximumHeight(4)
        self.checking_bar.hide()

        card, check_layout = make_card()
        card.setObjectName("systemCheckCard")

        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(section_label("Readiness Checks", level=2), 1)
        self.last_checked_label = QLabel()
        self.last_checked_label.setObjectName("accent")
        self.last_checked_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self.last_checked_label)
        self.refresh_button = QPushButton("Refresh Checks")
        self.refresh_button.setObjectName("primary")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        check_layout.addLayout(header)

        check_layout.addWidget(self.checking_bar)

        self._sections: dict[str, QVBoxLayout] = {}
        for section_title in (
            "System",
            "Required Tools",
            "Winetricks Dependencies",
            "Proton Builds",
            "Optional Enhancements",
        ):
            section = QWidget()
            section.setObjectName("checkSection")
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(12, 10, 12, 10)
            section_layout.setSpacing(8)
            heading = QLabel(section_title)
            heading.setObjectName("section3")
            heading.setProperty("class", "greenHeading")
            section_layout.addWidget(heading)
            content_layout = QVBoxLayout()
            content_layout.setContentsMargins(0, 0, 0, 0)
            content_layout.setSpacing(8)
            section_layout.addLayout(content_layout)
            check_layout.addWidget(section)
            self._sections[section_title] = content_layout

        root.addWidget(card)

        override_card, override_layout = make_card()
        override_card.setObjectName("systemCheckCard")
        override_layout.addWidget(section_label("Manual Overrides", level=2))
        override_layout.addWidget(
            info_label(
                "Point COMMANDER to tools or Proton builds installed outside the "
                "normal search paths. Overrides are saved for future launches."
            )
        )
        for key, label, directory in (
            ("steam_root", "Steam library", True),
            ("steam", "Steam executable", False),
            ("umu-run", "umu-run executable", False),
            ("winetricks", "Winetricks executable", False),
            ("protontricks", "Protontricks executable", False),
            ("vulkaninfo", "Vulkan info executable", False),
            ("umu_proton", "GE-Proton build", True),
        ):
            self._add_override_row(override_layout, key, label, directory)
        root.addWidget(override_card)
        root.addStretch(1)

        self._set_all_checking()
        self.refresh()

    def _set_all_checking(self) -> None:
        """Pre-populate every section with a single CHECKING... placeholder."""
        for title, layout in self._sections.items():
            clear_layout(layout)
            self._add_row(layout, title, "Scanning...", "checking", "")

    def refresh(self) -> None:
        if self._task is not None:
            return
        if self._pending_timer is not None:
            self._pending_timer.stop()
            self._pending_timer = None
        self._pending_result = None
        self._pending_error = None
        self._refresh_start = time.monotonic()
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Checking...")
        self.checking_bar.show()
        self.summary.setText("Checking system readiness...")
        for lbl in self._status_labels.values():
            lbl.setText("CHECKING...")
            lbl.setObjectName("statusChecking")
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)
            lbl.update()
        self._task = BackgroundTask(_collect_checks, parent=self)
        self._task.result.connect(self._show_checks)
        self._task.error.connect(self._show_error)
        self._task.start()

    def _show_checks(
        self, result: tuple[list[dict[str, str]], bool, dict[str, str]]
    ) -> None:
        self._task = None
        elapsed = time.monotonic() - self._refresh_start
        if elapsed < self._min_check_seconds:
            self._pending_result = result
            delay_ms = int((self._min_check_seconds - elapsed) * 1000)
            self._pending_timer = QTimer(self)
            self._pending_timer.setSingleShot(True)
            self._pending_timer.timeout.connect(self._apply_checks)
            self._pending_timer.start(delay_ms)
            return
        self._pending_result = result
        self._apply_checks()

    def _apply_checks(self) -> None:
        result = self._pending_result
        self._pending_result = None
        self._pending_timer = None
        if result is None:
            return
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Refresh Checks")
        self.checking_bar.hide()
        self._status_labels.clear()
        checks, ready, self._detected_overrides = result
        by_label = {check["label"]: check for check in checks}
        _missing = {
            "label": "?",
            "state": "missing",
            "detail": "Data unavailable.",
            "command": "",
        }
        try:
            self._update_section(
                "System",
                [
                    by_label.get(label, _missing)
                    for label in ("GAMMA CLI", "Linux system")
                ],
            )
            self._update_section(
                "Required Tools",
                [
                    by_label.get(label, _missing)
                    for label in (
                        "Steam",
                        "umu-run",
                        "Winetricks",
                        "Protontricks",
                        "Vulkan",
                    )
                ],
            )
            self._update_section(
                "Winetricks Dependencies",
                [by_label.get(verb, _missing) for verb in WINETRICKS_VERBS],
            )
            self._update_section(
                "Proton Builds",
                [by_label.get("Proton Builds", _missing)],
            )
            self._update_section(
                "Optional Enhancements",
                [by_label.get(label, _missing) for label in ("GameMode", "MangoHud")],
            )
        except (KeyError, TypeError, ValueError) as exc:
            self.summary.setText(f"System check display failed: {exc}")
            self.last_checked_label.setText(
                f"Last checked: {datetime.now(tz=timezone.utc).astimezone().strftime('%H:%M:%S')}"
            )
        self.summary.setText(
            "System ready for installation."
            if ready
            else "Install the missing requirements, then refresh checks."
        )
        self.last_checked_label.setText(
            f"Last checked: {datetime.now(tz=timezone.utc).astimezone().strftime('%H:%M:%S')}"
        )
        self._refresh_override_values()

    def _update_section(self, title: str, checks: list[dict[str, object]]) -> None:
        layout = self._sections[title]
        clear_layout(layout)
        for check in checks:
            if "builds" in check:
                builds = check["builds"] or []
                for name, state in builds:
                    self._add_row(layout, name, "", state, "")
                if not builds:
                    self._add_row(
                        layout,
                        "Proton build",
                        "No compatible build detected.",
                        "missing",
                        "",
                    )
                continue
            self._add_row(
                layout,
                str(check["label"]),
                str(check["detail"]),
                str(check["state"]),
                str(check["command"]),
                str(check.get("manager", "")),
            )

    def _add_row(
        self,
        layout: QVBoxLayout,
        label_text: str,
        detail_text: str,
        state_text: str,
        command: str,
        manager: str = "",
    ) -> None:
        row = QGridLayout()
        row.setColumnStretch(1, 1)
        label = QLabel(label_text)
        detail = QLabel(detail_text)
        detail.setWordWrap(True)
        status_text = {
            "ready": "READY",
            "optional": "OPTIONAL",
            "checking": "CHECKING...",
        }.get(state_text, "NOT READY")
        status_object = {
            "ready": "statusReady",
            "optional": "statusOptional",
            "checking": "statusChecking",
        }.get(state_text, "statusNotReady")
        state = QLabel(status_text)
        state.setObjectName(status_object)
        self._status_labels[label_text] = state
        row.addWidget(label, 0, 0)
        row.addWidget(detail, 0, 1)
        row.addWidget(state, 0, 2, Qt.AlignmentFlag.AlignRight)
        if command:
            sep = QLabel("|")
            sep.setObjectName("accent")
            sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(sep, 0, 3)
            copy_button = QPushButton("Copy install command")
            copy_button.setObjectName("copyCommand")
            copy_button.clicked.connect(
                lambda _checked=False, value=command: (
                    QGuiApplication.clipboard().setText(value)
                )
            )
            row.addWidget(copy_button, 0, 4)
        layout.addLayout(row)

    def _add_override_row(
        self, layout: QVBoxLayout, key: str, label: str, directory: bool
    ) -> None:
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        edit = QLineEdit()
        edit.setPlaceholderText("Automatic detection")
        saved = gui_settings.load_gui_settings().get("tool_overrides") or {}
        edit.setText(saved.get(key, ""))
        edit.editingFinished.connect(
            lambda key=key, edit=edit: self._save_override(key, edit)
        )
        self._override_edits[key] = edit
        row.addWidget(edit, 1)
        automatic = QCheckBox("Detect automatically")
        has_override = bool(saved.get(key))
        automatic.blockSignals(True)
        automatic.setChecked(not has_override)
        automatic.blockSignals(False)
        automatic.toggled.connect(
            lambda checked, key=key: self._toggle_override(key, checked)
        )
        self._override_checks[key] = automatic
        row.addWidget(automatic)
        browse = QPushButton("Browse")
        browse.setObjectName("secondary")
        browse.clicked.connect(
            lambda _checked=False, key=key, edit=edit, directory=directory: (
                self._browse_override(key, edit, directory)
            )
        )
        row.addWidget(browse)
        self._override_browse[key] = browse
        self._set_override_controls(key, not has_override)
        layout.addLayout(row)

    def _refresh_override_values(self) -> None:
        saved = gui_settings.load_gui_settings().get("tool_overrides") or {}
        for key, edit in self._override_edits.items():
            manual = saved.get(key, "")
            has_manual = key in saved
            detected = self._detected_overrides.get(key) or ""
            edit.blockSignals(True)
            edit.setText(manual or detected)
            edit.blockSignals(False)
            automatic = not has_manual
            self._override_checks[key].blockSignals(True)
            self._override_checks[key].setChecked(automatic)
            self._override_checks[key].blockSignals(False)
            self._set_override_controls(key, automatic)

    def _set_override_controls(self, key: str, automatic: bool) -> None:
        self._override_edits[key].setReadOnly(automatic)
        self._override_browse[key].setEnabled(not automatic)

    def _toggle_override(self, key: str, automatic: bool) -> None:
        overrides = dict(gui_settings.load_gui_settings().get("tool_overrides") or {})
        edit = self._override_edits[key]
        if automatic:
            overrides.pop(key, None)
            edit.setText(self._detected_overrides.get(key) or "")
        else:
            value = edit.text().strip()
            if value and value != "Not detected":
                overrides[key] = value
            else:
                overrides[key] = ""
                edit.clear()
        gui_settings.save_gui_settings(tool_overrides=overrides)
        self._set_override_controls(key, automatic)
        self.refresh()

    def _browse_override(self, key: str, edit: QLineEdit, directory: bool) -> None:
        start = edit.text() or str(Path.home())
        if directory:
            path = QFileDialog.getExistingDirectory(self, f"Select {key}", start)
        else:
            path, _ = QFileDialog.getOpenFileName(self, f"Select {key}", start)
        if path:
            edit.setText(path)
            self._persist_override(key, edit)

    def _save_override(self, key: str, edit: QLineEdit) -> None:
        self._persist_override(key, edit)

    def _persist_override(self, key: str, edit: QLineEdit) -> None:
        overrides = dict(gui_settings.load_gui_settings().get("tool_overrides") or {})
        value = edit.text().strip()
        if value:
            overrides[key] = value
        else:
            cb = self._override_checks.get(key)
            if cb is not None and not cb.isChecked():
                overrides[key] = ""
            else:
                overrides.pop(key, None)
        gui_settings.save_gui_settings(tool_overrides=overrides)
        self.refresh()

    def _show_error(self, message: str) -> None:
        self._task = None
        elapsed = time.monotonic() - self._refresh_start
        if elapsed < self._min_check_seconds:
            self._pending_error = message
            delay_ms = int((self._min_check_seconds - elapsed) * 1000)
            self._pending_timer = QTimer(self)
            self._pending_timer.setSingleShot(True)
            self._pending_timer.timeout.connect(self._apply_error)
            self._pending_timer.start(delay_ms)
            return
        self._apply_error(message)

    def _apply_error(self, message: str | None = None) -> None:
        if message is None:
            message = self._pending_error
        self._pending_error = None
        self._pending_timer = None
        if message is None:
            return
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Refresh Checks")
        self.checking_bar.hide()
        for lbl in self._status_labels.values():
            lbl.setText("NOT READY")
            lbl.setObjectName("statusNotReady")
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)
            lbl.update()
        self._status_labels.clear()
        self.summary.setText(f"System check failed: {message}")
        self.last_checked_label.setText(
            f"Last checked: {datetime.now(tz=timezone.utc).astimezone().strftime('%H:%M:%S')}"
        )
