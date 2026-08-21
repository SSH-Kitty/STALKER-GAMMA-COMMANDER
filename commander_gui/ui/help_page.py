"""In-app Help: a practical, page-by-page guide to COMMANDER.

Content mirrors the project README so the in-app guide and the docs never
drift apart.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..config import logs_dir
from ..gui_settings import load_gui_settings
from .common import _kv_row, clear_layout, info_label, make_card, section_label


def _bullets(lines: list[str]) -> QLabel:
    return info_label("\n".join(f"• {line}" for line in lines))


class HelpPage(QWidget):
    """A practical guide to the settings and workflows exposed by COMMANDER."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.settings = window.settings

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 20)
        outer.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.viewport().setStyleSheet("background: transparent;")
        outer.addWidget(scroll)

        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(14)
        scroll.setWidget(content)

        _title = section_label("HELP", level=1)
        _title.setWordWrap(True)
        _title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(_title)
        _sub = info_label(
            "This guide explains each COMMANDER page. Start with a COMMANDER profile, "
            "install Anomaly and GAMMA, choose a Wine/Proton runner, then launch through MO2."
        )
        _sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(_sub)

        root.addWidget(self._quickstart_card())
        root.addWidget(self._snapshot_card())

        root.addWidget(self._guide_card())

        config_card, config = make_card()
        root.addWidget(config_card)
        config.addWidget(section_label("Configuration", level=2))
        config.addWidget(
            info_label(
                "COMMANDER respects XDG_CONFIG_HOME; the paths below assume the "
                "default location."
            )
        )
        for label, value in (
            (
                "settings.json",
                (
                    "~/.config/stalker-gamma/settings.json — shared with the CLI: "
                    "profiles, install paths, MO2 profile, download threads, repo "
                    "URLs and branches"
                ),
            ),
            (
                "gui-settings.json",
                (
                    "~/.config/stalker-gamma/gui-settings.json — GUI only: selected "
                    "runner, per-runner prefixes, last launch target"
                ),
            ),
            (
                "logs",
                (
                    "~/.config/stalker-gamma/logs/ — CLI logs plus launcher.log "
                    "(rotates at 1 MB)"
                ),
            ),
            (
                "Integrity baseline",
                "<gamma>/gamma-md5.txt — MD5 baseline for integrity checking",
            ),
            (
                "Modlist backup",
                (
                    "<gamma>/profiles/<profile>/modlist.txt.gammagui.bak — pre-edit "
                    "modlist backup"
                ),
            ),
        ):
            config.addLayout(_kv_row(label, value))
        config.addWidget(
            info_label(
                "settings.json is written atomically, and any keys a newer CLI "
                "adds that this GUI does not understand are preserved verbatim on "
                "save — editing profiles here never clobbers CLI-only settings."
            )
        )
        for label, value in (
            (
                "STALKER_GAMMA_CLI",
                (
                    "Absolute path to a different stalker-gamma binary to drive "
                    "instead of the bundled one"
                ),
            ),
            (
                "XDG_CONFIG_HOME",
                "Relocates the config and log directory",
            ),
        ):
            config.addLayout(_kv_row(label, value))

        root.addWidget(self._troubleshooting_card())
        root.addStretch(1)

        self.refresh()

    def _quickstart_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("First run", level=2))
        steps = [
            (
                "01",
                "Create a profile",
                (
                    "Fill in the Anomaly folder, GAMMA folder, and cache folder, "
                    "then Create Profile — it activates automatically. Use "
                    "absolute paths; the CLI defaults are relative to where the "
                    "app was started."
                ),
                "profiles",
                "Open Profiles",
            ),
            (
                "02",
                "Install Anomaly, then GAMMA",
                (
                    "Open Install and select Install GAMMA. Anomaly is installed "
                    "first when it is missing. "
                    "Expect a very large download (~150 GB, or ~100 GB with "
                    "Minimal). Use Updates for normal addon updates and Verify "
                    "Integrity when checking or repairing installed files."
                ),
                "install",
                "Open Install",
            ),
            (
                "03",
                "Install the runtimes",
                (
                    "Install / Update Runtimes in the Winetricks panel. The "
                    "workflow may install protontricks first, then the Visual C++ "
                    "and DirectX runtimes required by MO2 (concrt140.dll)."
                ),
                "install",
                "Open Winetricks",
            ),
            (
                "04",
                "Launch the game",
                (
                    "Pick a Wine/Proton runner and launch target on Play, then "
                    "choose Launch Game, Open MO2, or Launch Anomaly. The game "
                    "starts detached, so closing COMMANDER does not kill your "
                    "session."
                ),
                "play",
                "Open Play",
            ),
        ]
        for number, title, text, page, button_text in steps:
            row = QHBoxLayout()
            row.setSpacing(12)
            chip = QLabel(number)
            chip.setObjectName("chip")
            chip.setAlignment(Qt.AlignmentFlag.AlignTop)
            row.addWidget(chip, 0, Qt.AlignmentFlag.AlignTop)
            body = QVBoxLayout()
            body.setSpacing(2)
            body.addWidget(section_label(title, level=2))
            body.addWidget(info_label(text))
            row.addLayout(body, 1)
            button = QPushButton(button_text)
            button.setObjectName("secondary")
            button.clicked.connect(
                lambda _checked=False, key=page: self.window.set_page(key)
            )
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
            layout.addLayout(row)
        return card

    def _snapshot_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Current configuration", level=2))
        self.snapshot_status = info_label("")
        self.snapshot_status.setObjectName("accent")
        layout.addWidget(self.snapshot_status)
        self.snapshot_values = QVBoxLayout()
        self.snapshot_values.setSpacing(4)
        layout.addLayout(self.snapshot_values)
        return card

    def _guide_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Page guide", level=2))
        layout.addWidget(
            info_label(
                "Every page in this application, what it does, and where to find "
                "it. The details match the project README."
            )
        )
        grid = QGridLayout()
        grid.setSpacing(16)
        features = [
            (
                "Dashboard",
                (
                    "The landing page. Active profile summary, install status for "
                    "Anomaly and GAMMA, Winetricks runtime status, storage "
                    "usage across your folders, a background update check, and "
                    "quick-open buttons for each folder and the log directory."
                ),
                [],
                "dashboard",
            ),
            (
                "Play",
                (
                    "Launches the selected executable through ModOrganizer.exe "
                    "run -e so the MO2 virtual file system is active and every "
                    "GAMMA mod is loaded."
                ),
                [
                    "Auto runner detection — Proton, then Wine",
                    "Per-runner prefixes, live command preview with a copy button",
                    "Detached launch, with launcher.log diagnostics",
                ],
                "play",
            ),
            (
                "System Check",
                (
                    "Check whether Linux has the dependencies, runners, Proton "
                    "builds, prefixes, and graphics support needed to install and "
                    "run S.T.A.L.K.E.R. G.A.M.M.A."
                ),
                [
                    "Commands are shown for manual installation only",
                    "Copy package commands and refresh checks after installing",
                    "Separate runner and prefix checks help prevent Proton mismatches",
                ],
                "systemcheck",
            ),
            (
                "Install",
                (
                    "Full Anomaly and GAMMA installation with a live per-addon "
                    "progress table, an overall completion bar and clean "
                    "cancellation."
                ),
                [
                    "Minimal mode deletes archives after extract (~50 GB saved)",
                    "Preserve user.ltx and MCM settings across a reinstall",
                    "Winetricks runtimes panel + Verify Integrity",
                ],
                "install",
            ),
            (
                "Updates",
                (
                    "Check for addon changes, review the parsed diff (Added / "
                    "Modified / Removed, including archive-name changes), then "
                    "apply through the same live progress UI."
                ),
                [
                    "Holds the global install lock",
                    "Never runs concurrently with an install",
                ],
                "update",
            ),
            (
                "Mod Manager",
                (
                    "Direct, careful editing of the MO2 profile's modlist.txt — "
                    "mods grouped by the _separator category entries GAMMA "
                    "ships, with search."
                ),
                [
                    "Backup taken before the first edit (modlist.txt.gammagui.bak)",
                    "Atomic writes — a crash cannot truncate your load order",
                    "Edits blocked while Mod Organizer is running",
                ],
                "modmanager",
            ),
            (
                "Profiles",
                (
                    "Create, edit, activate and delete CLI profiles. Creation, "
                    "activation and deletion are delegated to the CLI so its side "
                    "effects (MO2 selected_profile, modlist download) happen "
                    "exactly as intended."
                ),
                [
                    "Advanced fields expose every repo URL and branch the CLI supports",
                ],
                "profiles",
            ),
            (
                "Utilities",
                (
                    "Anomaly integrity, shader cache purge, ReShade removal, "
                    "cache prune, GOG fix-install, log folder, and debug "
                    "hash-install."
                ),
                [
                    "Fresh Reset wipes both folders and reinstalls from scratch",
                    "Full Uninstall removes the install folders, keeps your prefix",
                    "Both guarded with explicit warnings and path checks",
                ],
                "utilities",
            ),
        ]
        for index, (title, description, bullets, page) in enumerate(features):
            card_widget, card_layout = make_card()
            card_layout.addWidget(section_label(title, level=2))
            card_layout.addWidget(info_label(description))
            if bullets:
                card_layout.addWidget(_bullets(bullets))
            button = QPushButton(f"Open {title}")
            button.setObjectName("secondary")
            button.clicked.connect(
                lambda _checked=False, key=page: self.window.set_page(key)
            )
            card_layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(card_widget, index // 2, index % 2)
        layout.addLayout(grid)
        return card

    def _troubleshooting_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Troubleshooting", level=2))
        rows = [
            (
                "CLI not found on startup",
                (
                    "The bundled binary is missing or not executable. Run chmod +x "
                    "cli/usr/bin/stalker-gamma, or set STALKER_GAMMA_CLI to an "
                    "executable elsewhere."
                ),
            ),
            (
                "MO2 exits immediately or mentions concrt140.dll",
                (
                    "Install the required Wine/Proton runtimes into the active "
                    "runner prefix from Install → Install / Update Runtimes."
                ),
            ),
            (
                "Wine client error: version mismatch",
                (
                    "This prefix was created by a different Wine/Proton version. "
                    "Select the original runner or configure a separate prefix "
                    "for the new runner. Close MO2 and the game before switching."
                ),
            ),
            (
                "Play page has no launch targets",
                (
                    "The active profile may point to the wrong GAMMA folder, GAMMA "
                    "may not be installed, or ModOrganizer.ini may not contain a "
                    "parseable executable. If available, AnomalyLauncher.exe is "
                    "used as a direct-launch fallback."
                ),
            ),
            (
                "Mod Manager edits are disabled",
                (
                    "MO2 is running. Close it first because MO2 rewrites modlist.txt "
                    "when it exits and could discard your edits."
                ),
            ),
            (
                "Install folders look wrong / files are in odd places",
                (
                    "Profile paths are relative to the directory where COMMANDER "
                    "was started. Set absolute paths on the Profiles page."
                ),
            ),
            (
                "Everything shows No active profile",
                "Create and activate a profile on the Profiles page.",
            ),
            (
                "Could not load the xcb platform plugin",
                (
                    "Install libxcb-cursor0 / xcb-util-cursor (see the README "
                    "requirements section)."
                ),
            ),
            (
                "Launch fails or exits immediately",
                (
                    "Check launcher.log, the selected target, runner, prefix, and "
                    "required dependencies. The Play page reports the exit code "
                    "and recent launcher output."
                ),
            ),
            (
                "No runner detected or umu-run is missing",
                (
                    "Install or configure Wine, Steam Proton, or umu-run. Auto "
                    "detection tries Proton, then Wine."
                ),
            ),
            (
                "Winetricks or runtime installation fails",
                (
                    "Install Wine and Winetricks. If protontricks is missing, "
                    "COMMANDER can install it with pipx or user-level pip; PEP 668 "
                    "systems should use pipx. Keep MO2 and the game closed."
                ),
            ),
            (
                "Settings or autostart changes do not take effect",
                (
                    "GUI settings follow XDG_CONFIG_HOME. Autostart uses the "
                    "stalker-gamma-commander.desktop file under the autostart "
                    "directory; recreate it from Settings if it was removed."
                ),
            ),
            (
                "An installation move was interrupted",
                (
                    "Restart COMMANDER and review the orphan-folder prompt before "
                    "removing anything. Verify the listed destination first."
                ),
            ),
            (
                "Need diagnostics for a bug report",
                (
                    "Use Settings → Export diagnostics for a report, or Utilities "
                    "→ Create diagnostic archive for the CLI install hash archive."
                ),
            ),
        ]
        for symptom, fix in rows:
            layout.addLayout(_kv_row(symptom, fix))
        layout.addWidget(
            info_label(
                f"Launch logs are written to {logs_dir() / 'launcher.log'}. The "
                "Dashboard and Utilities pages both have a button to open the "
                "logs folder."
            )
        )
        return card

    def _set_snapshot_value(self, label: str, value: str) -> None:
        self.snapshot_values.addLayout(_kv_row(label, value or "Not configured"))

    def refresh(self) -> None:
        self.window.refresh_settings()
        self.settings = self.window.settings
        clear_layout(self.snapshot_values)

        profile = self.settings.active_profile
        if profile is None:
            self.snapshot_status.setText("No active profile. Start on Profiles.")
            self._set_snapshot_value("Profile", "Not configured")
            self._set_snapshot_value(
                "Runner", load_gui_settings().get("runner", "auto")
            )
            return

        gui = load_gui_settings()
        self.snapshot_status.setText(f"Active profile: {profile.profile_name}")
        self._set_snapshot_value("Anomaly", profile.anomaly)
        self._set_snapshot_value("GAMMA", profile.gamma)
        self._set_snapshot_value("Cache", profile.cache)
        self._set_snapshot_value("MO2 profile", profile.mo2_profile)
        self._set_snapshot_value("Download threads", str(profile.download_threads))
        self._set_snapshot_value("Runner", gui.get("runner", "auto"))
        self._set_snapshot_value("Prefix", gui.get("wine_prefix", "Not configured"))
        self._set_snapshot_value("Launch target", gui.get("target", "Auto"))
