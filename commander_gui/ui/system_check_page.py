"""System readiness checks and manual dependency guidance."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
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
    detect_gpu_vendors,
    detect_package_manager,
    install_command,
    vulkan32_driver_command,
    vulkan_driver_command,
)
from ..gui_settings import configured_wine_prefix
from ..launcher import find_extra_protons
from ..settings import load_settings
from ..winetricks import (
    WINETRICKS_VERBS,
    check_winetricks_status,
    umu_install_command,
    winetricks_binary,
)
from .common import (
    BackgroundTask,
    anomaly_installed,
    clear_layout,
    gamma_installed,
    info_label,
    make_card,
    section_label,
    tr,
    winetricks_tooltip,
)


def _manual_command(
    tool: str, manager: str | None, vendors: list[str] | None = None
) -> str:
    if tool == "Steam":
        return install_command("steam")
    if tool in {"Wine", "Winetricks"}:
        return install_command(tool.lower())
    if tool == "Protontricks":
        return install_command("pipx") + " && pipx install protontricks"
    if tool == "Vulkan":
        return vulkan_driver_command(manager, vendors or [])
    if tool == "Vulkan32":
        return vulkan32_driver_command(manager, vendors or [])
    if tool == "umu-run":
        # Reuses the same command the in-app installer runs, rather than a
        # second hand-written copy that can silently drift out of sync with
        # the release archive's actual layout (it previously did, and both
        # copies extracted the wrong tar member as a result).
        command = umu_install_command()
        if not command:
            return (
                "Install curl, then re-check - umu-run can be installed "
                "automatically from Play or Install."
            )
        return (
            "Install umu-launcher from "
            "https://github.com/Open-Wine-Components/umu-launcher or run:\n"
            + command[-1]
        )
    return ""


#: 32-bit Vulkan loader location by package manager. Distros disagree on
#: which physical path is the 32-bit one - on Arch (pacman) /usr/lib is the
#: 64-bit path and /usr/lib32 is the 32-bit compat path, while on
#: Fedora/openSUSE (dnf/zypper) it is the other way around (/usr/lib is
#: 32-bit, /usr/lib64 is 64-bit) - so the check must be distro-aware rather
#: than probing every path, which would false-positive on Arch by finding
#: the 64-bit library at /usr/lib/libvulkan.so.1.
_VULKAN32_PATHS: dict[str, tuple[str, ...]] = {
    "pacman": ("/usr/lib32/libvulkan.so.1",),
    "apt": (
        "/usr/lib/i386-linux-gnu/libvulkan.so.1",
        "/lib/i386-linux-gnu/libvulkan.so.1",
    ),
    "dnf": ("/usr/lib/libvulkan.so.1",),
    "zypper": ("/usr/lib/libvulkan.so.1",),
}


def _check_vulkan32(manager: str | None, vendors: list[str]) -> dict[str, str]:
    """Check for a 32-bit Vulkan loader (needed by the 32-bit game/MO2)."""
    candidates = _VULKAN32_PATHS.get(manager or "", ())
    found = any(Path(p).is_file() for p in candidates)
    return {
        "label": "32-bit Vulkan",
        "state": "ready" if found else "missing",
        "detail": (
            tr("32-bit Vulkan loader detected.")
            if found
            else tr(
                "No 32-bit Vulkan loader found - DXVK/vkd3d need it for "
                "the (32-bit) game and MO2."
            )
        ),
        "command": _manual_command("Vulkan32", manager, vendors),
    }


#: One-line "why this matters" clause shown alongside a missing tool, so the
#: row explains itself instead of just naming a binary that was not found.
_TOOL_WHY: dict[str, str] = {
    "Steam": "used to discover Steam library folders and Proton builds.",
    "Wine": "runs the Windows game and Mod Organizer directly (umu-run or "
    "Steam Proton also work instead).",
    "umu-run": "the recommended way to run the game and Mod Organizer "
    "through Proton.",
    "Winetricks": "installs the Visual C++/DirectX runtimes Mod Organizer "
    "and the game need.",
    "Protontricks": "runs Winetricks against a Steam Proton prefix "
    "specifically.",
    "Vulkan": "confirms your graphics driver supports Vulkan, which DXVK/"
    "vkd3d (DirectX-over-Vulkan) needs to run the game.",
}


def _check_tool(
    label: str, command: str, manager: str | None, vendors: list[str] | None = None
) -> dict[str, str]:
    found = configured_tool(command) or shutil.which(command)
    # Validate umu-run actually runs — PATH may point to a broken Lutris stub.
    if found and command == "umu-run":
        try:
            result = subprocess.run(
                [found, "--version"],
                capture_output=True,
                check=False,
                timeout=5,
            )
            if result.returncode != 0:
                found = None
        except (OSError, subprocess.TimeoutExpired):
            found = None
    why = _TOOL_WHY.get(label, "")
    if why:
        missing_detail = tr(
            "{command} was not found on PATH. {why}", command=command, why=tr(why)
        )
    else:
        missing_detail = tr("{command} was not found on PATH.", command=command)
    return {
        "label": label,
        "state": "ready" if found else "missing",
        "detail": found or missing_detail,
        "command": _manual_command(label, manager, vendors),
        "detected": found,
        "override_key": command,
        "manager": "" if found else (manager or ""),
    }


def _short_proton_name(label: str) -> str:
    """Turn a discovered runner label into a compact, path-free name."""
    ver = label.removeprefix("GE-Proton").removeprefix("Proton ").strip()
    return f"GE-Proton {ver}" if ver else "GE-Proton Unknown"


#: Plain-language names for the Winetricks verb codenames. The codename
#: itself is not hidden - it stays visible in this row's tooltip - just
#: demoted from the primary label, where "d3dcompiler_43" reads as pure
#: jargon to anyone who is not already a Winetricks user.
_VERB_LABELS: dict[str, str] = {
    "d3dcompiler_43": "DirectX Shader Compiler (legacy)",
    "d3dcompiler_47": "DirectX Shader Compiler",
    "d3dx10": "DirectX 10 Extensions",
    "d3dx11_43": "DirectX 11 Extensions",
    "d3dx9": "DirectX 9 Extensions",
    "quartz": "DirectShow Multimedia (Quartz)",
    "dx8vb": "DirectX 8 Visual Basic Runtime",
    "vcrun2022": "Visual C++ Runtime 2022",
}


def _winetricks_checks(
    status: dict[str, bool],
    binary: str,
    extra_tools: list[tuple[str, str, bool, str]],
) -> list[dict[str, str]]:
    """Build one summarized readiness row for the active prefix's runtimes.

    Eight separate rows, one per Winetricks verb codename, read as a wall of
    jargon with no sense of overall readiness at a glance. This collapses
    them into a single row; the same tooltip already used for this elsewhere
    (Dashboard, Install page) gives the per-item breakdown on hover.

    ``extra_tools`` is ``(tooltip_key, display_label, installed, manual_command)``
    for Wine, Protontricks and umu-run - folded into this row's count and
    copy-paste command so it reports the same "X/Y dependencies installed"
    total as the Dashboard, even though each of the three also has its own
    row further up this page.
    """
    if not binary:
        return [
            {
                "label": "Runtime libraries",
                "state": "missing",
                "detail": tr(
                    "Winetricks is not installed, so these cannot be "
                    "checked yet - see the Winetricks row above."
                ),
                "command": "",
            }
        ]
    installed = sum(1 for verb in WINETRICKS_VERBS if status.get(verb, False))
    installed += sum(1 for _key, _label, ok, _cmd in extra_tools if ok)
    total = len(WINETRICKS_VERBS) + len(extra_tools)
    missing_verbs = [verb for verb in WINETRICKS_VERBS if not status.get(verb, False)]
    missing_tools = [label for _key, label, ok, _cmd in extra_tools if not ok]
    missing_names = [
        tr(_VERB_LABELS.get(verb, verb)) for verb in missing_verbs
    ] + missing_tools
    # Winetricks verbs are idempotent, so that line is always offered; the
    # Wine/Protontricks/umu-run install commands can need sudo or overwrite
    # an existing binary, so only add those lines when actually missing.
    commands = [f"{binary} -q {' '.join(WINETRICKS_VERBS)}"]
    commands.extend(cmd for _key, _label, ok, cmd in extra_tools if not ok and cmd)
    tooltip_status = dict(status)
    for key, _label, ok, _cmd in extra_tools:
        tooltip_status[key] = ok
    return [
        {
            "label": "Runtime libraries",
            "state": "ready" if not missing_names else "missing",
            "detail": (
                tr(
                    "{installed}/{total} runtime libraries installed - the "
                    "Visual C++ and DirectX runtimes Mod Organizer and the "
                    "game need.",
                    installed=installed,
                    total=total,
                )
                if not missing_names
                else tr(
                    "{installed}/{total} runtime libraries installed. Missing: {missing}",
                    installed=installed,
                    total=total,
                    missing=", ".join(missing_names),
                )
            ),
            "command": "\n".join(commands),
            "tooltip": winetricks_tooltip(tooltip_status),
        }
    ]


def _installation_checks() -> list[dict[str, str]]:
    """Report the active profile and whether its game folders are installed."""
    profile = load_settings().active_profile
    if profile is None:
        return [
            {
                "label": "Active profile",
                "state": "missing",
                "detail": tr("Create or activate a profile before installing GAMMA."),
                "command": "",
            },
            {
                "label": "Anomaly installation",
                "state": "missing",
                "detail": tr("No active profile provides an Anomaly folder."),
                "command": "",
            },
            {
                "label": "GAMMA modpack",
                "state": "missing",
                "detail": tr("No active profile provides a GAMMA folder."),
                "command": "",
            },
        ]
    return [
        {
            "label": "Active profile",
            "state": "ready",
            "detail": profile.profile_name,
            "command": "",
        },
        {
            "label": "Anomaly installation",
            "state": "ready" if anomaly_installed(profile.anomaly) else "missing",
            "detail": (
                tr("Installed at {path}.", path=profile.anomaly)
                if anomaly_installed(profile.anomaly)
                else tr("Not installed at {path}.", path=profile.anomaly)
            ),
            "command": "",
        },
        {
            "label": "GAMMA modpack",
            "state": "ready" if gamma_installed(profile.gamma, profile.mo2_profile) else "missing",
            "detail": (
                tr("Installed at {path}.", path=profile.gamma)
                if gamma_installed(profile.gamma, profile.mo2_profile)
                else tr("Not installed at {path}.", path=profile.gamma)
            ),
            "command": "",
        },
    ]


def _collect_checks() -> tuple[list[dict[str, str]], bool, dict[str, str]]:
    manager = detect_package_manager()
    vendors = detect_gpu_vendors()
    checks: list[dict[str, str]] = []
    checks.extend(_installation_checks())
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
            "detail": f"{detect_distro_id() or tr('Unknown distribution')} / {platform.machine()}",
            "command": "",
        }
    )
    checks.extend(
        [
            _check_tool("Steam", "steam", manager),
            _check_tool("Wine", "wine", manager),
            _check_tool("umu-run", "umu-run", manager),
            _check_tool("Winetricks", "winetricks", manager),
            _check_tool("Protontricks", "protontricks", manager),
            _check_tool("Vulkan", "vulkaninfo", manager, vendors),
            _check_vulkan32(manager, vendors),
        ]
    )
    by_label = {check["label"]: check for check in checks}
    # vulkaninfo on PATH does not prove a working driver; ask it for the GPU.
    vulkan = by_label["Vulkan"]
    if vulkan["state"] == "ready":
        try:
            result = subprocess.run(
                [vulkan["detected"], "--summary"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            gpu = next(
                (
                    line.split("=", 1)[1].strip()
                    for line in result.stdout.splitlines()
                    if "deviceName" in line and "=" in line
                ),
                "",
            )
            if result.returncode == 0 and gpu:
                vulkan["detail"] = f"{vulkan['detected']} — {gpu}"
            elif result.returncode != 0:
                vulkan["state"] = "missing"
                vulkan["detail"] = tr(
                    "vulkaninfo failed to query a Vulkan device; check your "
                    "GPU drivers."
                )
        except (OSError, subprocess.TimeoutExpired):
            pass  # keep PATH-based result
    # Readiness only needs one working runner backend; Wine alone is not
    # required when umu-run or Steam is available.
    runner_checks = [by_label[name] for name in ("Steam", "Wine", "umu-run")]
    runner_found = any(check["state"] == "ready" for check in runner_checks)
    for check in runner_checks:
        if check["state"] != "ready" and runner_found:
            check["state"] = "optional"
            check["detail"] += " " + tr("Another runner is available.")
    try:
        prefix = configured_wine_prefix()
        winetricks = winetricks_binary()
        runtime_status = check_winetricks_status(prefix)
    except (OSError, RuntimeError, ValueError):
        winetricks = ""
        runtime_status = {verb: False for verb in WINETRICKS_VERBS}
    if not winetricks and not (
        configured_tool("winetricks") or shutil.which("winetricks")
    ):
        # Winetricks itself is missing: skip per-verb rows, the tool row
        # already explains what to install.
        runtime_status = {}
    # Ground truth for "is it actually present", independent of the "state"
    # field above (which the runner-fallback loop just downgraded to
    # "optional" for Wine/umu-run when another runner covers for them).
    umu_script = umu_install_command()
    extra_tools = [
        (
            "wine",
            "Wine",
            bool(by_label["Wine"].get("detected")),
            by_label["Wine"].get("command", ""),
        ),
        (
            "protontricks",
            "Protontricks",
            bool(by_label["Protontricks"].get("detected")),
            by_label["Protontricks"].get("command", ""),
        ),
        (
            "umu",
            "umu-run",
            bool(by_label["umu-run"].get("detected")),
            umu_script[-1] if umu_script else "",
        ),
    ]
    checks.extend(_winetricks_checks(runtime_status, winetricks, extra_tools))
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
    ge_builds = [
        (_short_proton_name(label), "ready", str(path))
        for label, path in extra_protons
    ]
    checks.append(
        {
            "label": "Proton Builds",
            "state": "ready" if ge_builds else "optional",
            "detail": tr("{count} GE-Proton build(s) detected.", count=len(ge_builds))
            if ge_builds
            else tr("No GE-Proton builds detected."),
            "command": "",
            "builds": ge_builds,
        }
    )
    protontricks = by_label["Protontricks"]
    if protontricks["state"] != "ready":
        protontricks["state"] = "optional"
    gamemode_found = configured_tool("gamemoderun") or shutil.which("gamemoderun")
    checks.append(
        {
            "label": "GameMode",
            "state": "ready" if gamemode_found else "optional",
            "detail": tr("Optional performance helper."),
            "command": "" if gamemode_found else install_command("gamemode"),
            "manager": "" if gamemode_found else (manager or ""),
        }
    )
    mangohud_found = configured_tool("mangohud") or shutil.which("mangohud")
    checks.append(
        {
            "label": "MangoHud",
            "state": "ready" if mangohud_found else "optional",
            "detail": tr("Optional performance overlay."),
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
                check["detail"] = tr(
                    "Steam library override is invalid: {path}", path=dir_path
                )
                check["command"] = ""
        if key == "umu_proton" or (
            key == "umu-run" and "umu_proton" in saved_overrides
        ):
            dir_path = saved_overrides.get("umu_proton", "")
            if dir_path and not Path(dir_path).is_dir():
                check["state"] = "missing"
                check["detail"] = tr(
                    "GE-Proton override path is invalid: {path}", path=dir_path
                )
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
                check["detail"] = tr(
                    "Override path is not executable: {path}", path=file_path
                )
                check["command"] = ""
    # Installation-state rows (profile/folders) do not gate system readiness:
    # this page checks the system, not whether the game is installed yet.
    _non_blocking = {"Active profile", "Anomaly installation", "GAMMA modpack"}
    required_missing = any(
        item["state"] == "missing" and item["label"] not in _non_blocking
        for item in checks
    )
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
        self._last_check_ts: float = 0.0
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

        title = section_label(tr("SYSTEM CHECK"), level=1)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(title)
        subtitle = info_label(
            tr("Verify that all required tools, runners, and dependencies are installed before setting up the game.")
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(subtitle)

        self.summary = QLabel(tr("Checking system readiness..."))
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
        header.addWidget(section_label(tr("Readiness Checks"), level=2), 1)
        self.last_checked_label = QLabel()
        self.last_checked_label.setObjectName("accent")
        self.last_checked_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self.last_checked_label)
        self.refresh_button = QPushButton(tr("Refresh Checks"))
        self.refresh_button.setObjectName("primary")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        check_layout.addLayout(header)

        check_layout.addWidget(self.checking_bar)

        self._sections: dict[str, QVBoxLayout] = {}
        for section_title in (
            "System",
            "Required Tools",
            "Runtime Libraries",
            "Proton Builds",
            "Optional Enhancements",
        ):
            section = QWidget()
            section.setObjectName("checkSection")
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(12, 10, 12, 10)
            section_layout.setSpacing(8)
            heading = QLabel(tr(section_title))
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
        override_layout.addWidget(section_label(tr("Manual Overrides"), level=2))
        override_layout.addWidget(
            info_label(
                tr("Point COMMANDER to tools or Proton builds installed outside the normal search paths. Overrides are saved for future launches.")
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
            self._add_row(layout, title, tr("Scanning..."), "checking", "")

    def showEvent(self, event) -> None:
        """Re-run checks when revisiting the page if the last run is stale."""
        super().showEvent(event)
        last = getattr(self, "_last_check_ts", 0.0)
        if self._task is None and time.monotonic() - last > 60:
            self.refresh()

    def refresh(self) -> None:
        if os.name == "nt":
            self.summary.setText(
                tr("System check is only relevant on Linux; this Windows install runs the game natively.")
            )
            for layout in self._sections.values():
                clear_layout(layout)
            self._add_row(
                next(iter(self._sections.values())),
                "Windows",
                tr("No Wine/Proton tooling required."),
                "ready",
                "",
            )
            self.refresh_button.setEnabled(False)
            return
        if self._task is not None:
            return
        if self._pending_timer is not None:
            self._pending_timer.stop()
            self._pending_timer = None
        self._pending_result = None
        self._pending_error = None
        self._refresh_start = time.monotonic()
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText(tr("Checking..."))
        self.checking_bar.show()
        self.summary.setText(tr("Checking system readiness..."))
        for lbl in self._status_labels.values():
            lbl.setText(tr("Checking..."))
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
        # The artificial minimum only applies once results are on screen; the
        # first load already shows "Scanning..." placeholders.
        if self._last_check_ts and elapsed < self._min_check_seconds:
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
        self.refresh_button.setText(tr("Refresh Checks"))
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
                    for label in (
                        "Active profile",
                        "Anomaly installation",
                        "GAMMA modpack",
                        "GAMMA CLI",
                        "Linux system",
                    )
                ],
            )
            self._update_section(
                "Required Tools",
                [
                    by_label.get(label, _missing)
                    for label in (
                        "Steam",
                        "Wine",
                        "umu-run",
                        "Winetricks",
                        "Protontricks",
                        "Vulkan",
                        "32-bit Vulkan",
                    )
                ],
            )
            self._update_section(
                "Runtime Libraries",
                [by_label.get("Runtime libraries", _missing)],
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
            self.summary.setText(tr("System check display failed: {exc}", exc=exc))
            self.last_checked_label.setText(
                tr("Last checked: {arg}", arg=datetime.now(tz=timezone.utc).astimezone().strftime('%H:%M:%S'))
            )
            return
        self.summary.setText(
            tr("System ready for installation.")
            if ready
            else tr("Install the missing requirements, then refresh checks.")
        )
        self.last_checked_label.setText(
            tr("Last checked: {arg}", arg=datetime.now(tz=timezone.utc).astimezone().strftime('%H:%M:%S'))
        )
        self._last_check_ts = time.monotonic()
        self._refresh_override_values()

    def _update_section(self, title: str, checks: list[dict[str, object]]) -> None:
        layout = self._sections[title]
        clear_layout(layout)
        for check in checks:
            if "builds" in check:
                builds = check["builds"] or []
                for name, state, *rest in builds:
                    self._add_row(layout, name, rest[0] if rest else "", state, "")
                if not builds:
                    self._add_row(
                        layout,
                        tr("Proton build"),
                        tr("No compatible build detected."),
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
                str(check.get("tooltip", "")),
            )

    def _add_row(
        self,
        layout: QVBoxLayout,
        label_text: str,
        detail_text: str,
        state_text: str,
        command: str,
        manager: str = "",
        tooltip: str = "",
    ) -> None:
        row = QGridLayout()
        row.setColumnStretch(1, 1)
        label = QLabel(tr(label_text))
        detail = QLabel(detail_text)
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        if tooltip:
            # Per-item breakdown (e.g. which Winetricks verb is missing) one
            # hover away, instead of either hiding it or spelling out every
            # item in the always-visible row.
            label.setToolTip(tooltip)
            detail.setToolTip(tooltip)
        # "Installed"/"Missing" reads plainer than "READY"/"NOT READY" for a
        # row that is really just reporting whether one tool or file is
        # present - "ready" invites "ready for what?" without more context.
        # The page's overall readiness verdict is a separate plain sentence
        # (self.summary), not one of these per-row badges.
        status_text = {
            "ready": tr("Installed"),
            "optional": tr("Optional"),
            "checking": tr("Checking..."),
        }.get(state_text, tr("Missing"))
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
        if command:
            copy_button = QPushButton(tr("Copy install command"))
            copy_button.setObjectName("copyCommand")
            copy_button.clicked.connect(
                lambda _checked=False, value=command, btn=copy_button: (
                    QGuiApplication.clipboard().setText(value),
                    btn.setText(tr("Copied!")),
                    QTimer.singleShot(1500, lambda b=btn: b.setText(tr("Copy install command"))),
                )
            )
            row.addWidget(copy_button, 0, 2)
            sep = QLabel(tr("|"))
            sep.setObjectName("accent")
            sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(sep, 0, 3)
        row.addWidget(state, 0, 4, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(row)

    def _add_override_row(
        self, layout: QVBoxLayout, key: str, label: str, directory: bool
    ) -> None:
        row = QHBoxLayout()
        row.addWidget(QLabel(tr(label)))
        edit = QLineEdit()
        edit.setPlaceholderText(tr("Automatic detection"))
        saved = gui_settings.load_gui_settings().get("tool_overrides") or {}
        edit.setText(saved.get(key, ""))
        edit.editingFinished.connect(
            lambda key=key, edit=edit: self._save_override(key, edit)
        )
        self._override_edits[key] = edit
        row.addWidget(edit, 1)
        automatic = QCheckBox(tr("Detect automatically"))
        has_override = bool(saved.get(key))
        automatic.blockSignals(True)
        automatic.setChecked(not has_override)
        automatic.blockSignals(False)
        automatic.toggled.connect(
            lambda checked, key=key: self._toggle_override(key, checked)
        )
        self._override_checks[key] = automatic
        row.addWidget(automatic)
        browse = QPushButton(tr("Browse..."))
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
            has_manual = bool(manual)
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
                # Empty means "no override" everywhere; a stored "" would be
                # treated as manual by `key in saved` but automatic by bool().
                overrides.pop(key, None)
                edit.clear()
        gui_settings.save_gui_settings(tool_overrides=overrides)
        self._set_override_controls(key, automatic)
        self.refresh()

    def _browse_override(self, key: str, edit: QLineEdit, directory: bool) -> None:
        start = edit.text() or str(Path.home())
        title = tr("Select folder") if directory else tr("Select file")
        if directory:
            path = QFileDialog.getExistingDirectory(self, title, start)
        else:
            path, _ = QFileDialog.getOpenFileName(self, title, start)
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
        self.refresh_button.setText(tr("Refresh Checks"))
        self.checking_bar.hide()
        self._status_labels.clear()
        # Replace stale rows with a single error row so old results are not
        # shown alongside the failure message.
        for layout in self._sections.values():
            clear_layout(layout)
        first_section = next(iter(self._sections.values()))
        self._add_row(first_section, "System check", message, "missing", "")
        self.summary.setText(tr("System check failed: {message}", message=message))
        self.last_checked_label.setText(
            tr("Last checked: {arg}", arg=datetime.now(tz=timezone.utc).astimezone().strftime('%H:%M:%S'))
        )
