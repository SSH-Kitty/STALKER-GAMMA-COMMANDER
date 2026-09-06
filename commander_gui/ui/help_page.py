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
from .common import (
    _kv_row,
    assistant_token,
    clear_layout,
    info_label,
    make_card,
    section_label,
    tr,
)


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

        _title = section_label(tr("HELP"), level=1)
        _title.setWordWrap(True)
        _title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(_title)
        _sub = info_label(
            tr("This guide explains each COMMANDER page. A default profile is created automatically; select an installation directory, install Anomaly and GAMMA, pick a GE-Proton runner, then launch through MO2.")
        )
        _sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(_sub)

        root.addWidget(self._quickstart_card())
        root.addWidget(self._snapshot_card())

        root.addWidget(self._guide_card())

        config_card, config = make_card()
        root.addWidget(config_card)
        config.addWidget(section_label(tr("Configuration"), level=2))
        config.addWidget(
            info_label(
                tr("COMMANDER respects XDG_CONFIG_HOME; the paths below assume the default location.")
            )
        )
        for label, value in (
            (
                "settings.json",
                tr(
                    "~/.config/stalker-gamma/settings.json — shared with the CLI: "
                    "profiles, install paths, MO2 profile, download threads, repo "
                    "URLs and branches"
                ),
            ),
            (
                "gui-settings.json",
                tr(
                    "~/.config/stalker-gamma/gui-settings.json — GUI only: selected "
                    "runner, per-runner prefixes, last launch target"
                ),
            ),
            (
                "logs",
                tr(
                    "~/.config/stalker-gamma/logs/ — CLI logs plus launcher.log "
                    "(rotates at 1 MB)"
                ),
            ),
            (
                tr("Integrity baseline"),
                tr("<gamma>/gamma-md5.txt — MD5 baseline for integrity checking"),
            ),
            (
                tr("Modlist backup"),
                tr(
                    "<gamma>/profiles/<profile>/modlist.txt.gammagui.bak — pre-edit "
                    "modlist backup"
                ),
            ),
        ):
            config.addLayout(_kv_row(label, value))
        config.addWidget(
            info_label(
                tr("settings.json is written atomically, and any keys a newer CLI adds that this GUI does not understand are preserved verbatim on save — editing profiles here never clobbers CLI-only settings.")
            )
        )
        for label, value in (
            (
                "STALKER_GAMMA_CLI",
                tr(
                    "Absolute path to a different stalker-gamma binary to drive "
                    "instead of the bundled one"
                ),
            ),
            (
                "XDG_CONFIG_HOME",
                tr("Relocates the config and log directory"),
            ),
        ):
            config.addLayout(_kv_row(label, value))

        root.addWidget(self._troubleshooting_card())
        root.addStretch(1)

        self.refresh()

    def _quickstart_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("First run"), level=2))
        steps = [
            (
                "01",
                tr("Select an installation directory"),
                tr(
                    "A default profile is created automatically. On Install, "
                    "select a base directory under Installation Directory and "
                    "click Create folders - the tool creates the anomaly, "
                    "gamma, and cache folders and sets them on your profile."
                ),
                "install",
                tr("Open Install"),
            ),
            (
                "02",
                tr("Install STALKER Anomaly then GAMMA"),
                tr(
                    "On Install, select Install GAMMA. Anomaly is installed "
                    "first when it is missing. Expect a large download (~150 "
                    "GB, or ~100 GB with Minimal). Use Updates for addon "
                    "updates and Verify Integrity to check or repair installed "
                    "files."
                ),
                "install",
                tr("Open Install"),
            ),
            (
                "03",
                tr("Install dependencies"),
                tr(
                    "Use Install Dependencies. It installs umu-run and "
                    "protontricks first, then the Visual C++ and DirectX "
                    "runtimes MO2 needs (concrt140.dll)."
                ),
                "install",
                tr("Open Install Dependencies"),
            ),
            (
                "04",
                tr("Launch the game"),
                tr(
                    "On Play, pick a GE-Proton runner (Auto-detect selects the "
                    "latest GE-Proton) and a launch target, then choose Launch "
                    "Game, Open MO2, or Launch Anomaly. The game starts "
                    "detached, so closing COMMANDER will not kill your session."
                ),
                "play",
                tr("Open Play"),
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
        layout.addWidget(section_label(tr("Current configuration"), level=2))
        self.snapshot_status = info_label("")
        self.snapshot_status.setObjectName("accent")
        layout.addWidget(self.snapshot_status)
        self.snapshot_values = QVBoxLayout()
        self.snapshot_values.setSpacing(4)
        layout.addLayout(self.snapshot_values)
        return card

    def _guide_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Page guide"), level=2))
        layout.addWidget(
            info_label(
                tr("Every page in this application, what it does, and where to find it. The details match the project README.")
            )
        )
        grid = QGridLayout()
        grid.setSpacing(16)
        features = [
            (
                tr("Dashboard"),
                tr(
                    "The landing page. Active profile summary, install status for "
                    "Anomaly and GAMMA, dependency status, storage "
                    "usage across your folders, a background update check, and "
                    "quick-open buttons for each folder and the log directory."
                ),
                [],
                "dashboard",
            ),
            (
                tr("Play"),
                tr(
                    "Launches the selected executable through ModOrganizer.exe "
                    "run -e so the MO2 virtual file system is active and every "
                    "GAMMA mod is loaded."
                ),
                [
                    tr("Auto-detect (latest GE-Proton) or an installed GE-Proton build"),
                    tr("Per-runner prefixes, live command preview with a copy button"),
                    tr("Detached launch, with launcher.log diagnostics"),
                ],
                "play",
            ),
            (
                tr("System Check"),
                tr(
                    "Check whether Linux has the dependencies, runners, Proton "
                    "builds, prefixes, and graphics support needed to install and "
                    "run S.T.A.L.K.E.R. G.A.M.M.A."
                ),
                [
                    tr("Commands are shown for manual installation only"),
                    tr("Copy package commands and refresh checks after installing"),
                    tr("Separate runner and prefix checks help prevent Proton mismatches"),
                ],
                "systemcheck",
            ),
            (
                tr("Install"),
                tr(
                    "Full Anomaly and GAMMA installation with a live per-addon "
                    "progress table, an overall completion bar and clean "
                    "cancellation."
                ),
                [
                    tr("Minimal mode deletes archives after extract (~50 GB saved)"),
                    tr("Preserve user.ltx and MCM settings across a reinstall"),
                    tr("Dependencies panel + Verify Integrity"),
                ],
                "install",
            ),
            (
                tr("Updates"),
                tr(
                    "Check for addon changes, review the parsed diff (Added / "
                    "Modified / Removed, including archive-name changes), then "
                    "apply through the same live progress UI."
                ),
                [
                    tr("Holds the global install lock"),
                    tr("Never runs concurrently with an install"),
                ],
                "update",
            ),
            (
                tr("Mod Manager"),
                tr(
                    "Direct, careful editing of the MO2 profile's modlist.txt — "
                    "mods grouped by the _separator category entries GAMMA "
                    "ships, with search."
                ),
                [
                    tr("Backup taken before the first edit (modlist.txt.gammagui.bak)"),
                    tr("Atomic writes — a crash cannot truncate your load order"),
                    tr("Edits blocked while Mod Organizer is running"),
                ],
                "modmanager",
            ),
            (
                tr("Profiles"),
                tr(
                    "Create, edit, activate and delete CLI profiles. Creation, "
                    "activation and deletion are delegated to the CLI so its side "
                    "effects (MO2 selected_profile, modlist download) happen "
                    "exactly as intended."
                ),
                [
                    tr("Advanced fields expose every repo URL and branch the CLI supports"),
                ],
                "profiles",
            ),
            (
                tr("Utilities"),
                tr(
                    "Preview cache cleanup, clean the download cache, clear "
                    "shader cache, remove ReShade, fix GOG installation, and "
                    "create Log Dump."
                ),
                [
                    tr("Fresh Reset wipes both folders and reinstalls from scratch"),
                    tr("Full Uninstall removes the install folders, keeps your prefix"),
                    tr("Both guarded with explicit warnings and path checks"),
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
            button = QPushButton(tr("Open {title}", title=title))
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
        layout.addWidget(section_label(tr("Troubleshooting"), level=2))
        rows = [
            (
                tr("CLI not found on startup"),
                tr(
                    "The bundled binary is missing or not executable. Run chmod +x "
                    "cli/usr/bin/stalker-gamma, or set STALKER_GAMMA_CLI to an "
                    "executable elsewhere."
                ),
            ),
            (
                tr("MO2 exits immediately or mentions concrt140.dll"),
                tr(
                    "Install the required runtimes into the active "
                    "runner prefix from Install → Install Dependencies."
                ),
            ),
            (
                tr("Wine client error: version mismatch"),
                tr(
                    "This prefix was created by a different runner version. "
                    "Select the original GE-Proton build or configure a separate "
                    "prefix for the new runner. Close MO2 and the game before "
                    "switching."
                ),
            ),
            (
                tr("Play page has no launch targets"),
                tr(
                    "The active profile may point to the wrong GAMMA folder, GAMMA "
                    "may not be installed, or ModOrganizer.ini may not contain a "
                    "parseable executable. If available, AnomalyLauncher.exe is "
                    "used as a direct-launch fallback."
                ),
            ),
            (
                tr("Mod Manager edits are disabled"),
                tr(
                    "MO2 is running. Close it first because MO2 rewrites modlist.txt "
                    "when it exits and could discard your edits."
                ),
            ),
            (
                tr("Install folders look wrong / files are in odd places"),
                tr(
                    "Profile paths are relative to the directory where COMMANDER "
                    "was started. Set absolute paths on the Profiles page."
                ),
            ),
            (
                tr("Everything shows No active profile"),
                tr("Create and activate a profile on the Profiles page."),
            ),
            (
                tr("Could not load the xcb platform plugin"),
                tr(
                    "Install libxcb-cursor0 / xcb-util-cursor (see the README "
                    "requirements section)."
                ),
            ),
            (
                tr("Launch fails or exits immediately"),
                tr(
                    "Check launcher.log, the selected target, runner, prefix, and "
                    "required dependencies. The Play page reports the exit code "
                    "and recent launcher output."
                ),
            ),
            (
                tr("No runner detected or umu-run is missing"),
                tr(
                    "Ensure umu-run is installed and at least one GE-Proton "
                    "build is available. Auto-detect selects the latest "
                    "GE-Proton."
                ),
            ),
            (
                tr("Dependency installation fails"),
                tr(
                    "Install Wine and Winetricks. If protontricks is missing, "
                    "COMMANDER can install it with pipx or user-level pip; PEP 668 "
                    "systems should use pipx. Keep MO2 and the game closed."
                ),
            ),
            (
                tr("Settings or autostart changes do not take effect"),
                tr(
                    "GUI settings follow XDG_CONFIG_HOME. Autostart uses the "
                    "stalker-gamma-commander.desktop file under the autostart "
                    "directory; recreate it from Settings if it was removed."
                ),
            ),
            (
                tr("An installation move was interrupted"),
                tr(
                    "Restart COMMANDER and review the orphan-folder prompt before "
                    "removing anything. Verify the listed destination first."
                ),
            ),
            (
                tr("Need diagnostics for a bug report"),
                tr(
                    "Use Settings → Export diagnostics for a report, or Utilities "
                    "→ Create Log Dump to bundle the logs into one zip, then open "
                    "it in {assistant} to check the errors and warnings.",
                    assistant=assistant_token(),
                ),
            ),
        ]
        for symptom, fix in rows:
            layout.addLayout(_kv_row(symptom, fix))
        layout.addWidget(
            info_label(
                tr("Launch logs are written to {arg}. The Dashboard page has a button to open the logs folder; on Utilities, Create Log Dump bundles them into one archive.", arg=logs_dir() / 'launcher.log')
            )
        )
        return card

    def _set_snapshot_value(self, label: str, value: str) -> None:
        self.snapshot_values.addLayout(_kv_row(label, value or tr("Not configured")))

    def refresh(self) -> None:
        self.window.refresh_settings()
        self.settings = self.window.settings
        clear_layout(self.snapshot_values)

        profile = self.settings.active_profile
        if profile is None:
            self.snapshot_status.setText(tr("No active profile. Start on Profiles."))
            self._set_snapshot_value(tr("Profile"), tr("Not configured"))
            self._set_snapshot_value(
                tr("Runner"), load_gui_settings().get("runner", "auto")
            )
            return

        gui = load_gui_settings()
        self.snapshot_status.setText(tr("Active profile: {profile_name}", profile_name=profile.profile_name))
        self._set_snapshot_value("Anomaly", profile.anomaly)
        self._set_snapshot_value("GAMMA", profile.gamma)
        self._set_snapshot_value(tr("Cache"), profile.cache)
        self._set_snapshot_value(tr("MO2 profile"), profile.mo2_profile)
        self._set_snapshot_value(tr("Download threads"), str(profile.download_threads))
        self._set_snapshot_value(tr("Runner"), gui.get("runner", "auto"))
        self._set_snapshot_value(tr("Prefix"), gui.get("wine_prefix", tr("Not configured")))
        self._set_snapshot_value(tr("Launch target"), gui.get("target", tr("Auto")))
