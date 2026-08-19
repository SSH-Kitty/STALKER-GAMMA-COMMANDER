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
from .common import clear_layout, info_label, make_card, section_label


def _kv_row(label: str, value: str) -> QHBoxLayout:
    row = QHBoxLayout()
    key = QLabel(label)
    key.setObjectName("dim")
    key.setAlignment(Qt.AlignmentFlag.AlignTop)
    val = QLabel(value)
    val.setWordWrap(True)
    val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    row.addWidget(key, 0, Qt.AlignmentFlag.AlignTop)
    row.addWidget(val, 1)
    return row


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
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(14)
        scroll.setWidget(content)

        root.addWidget(section_label("GAMMA HELP", level=1))
        root.addWidget(
            info_label(
                "Everything in this guide maps to a page or setting in this "
                "application. Start with Profiles, install the game, configure "
                "your runner, then launch through Mod Organizer 2."
            )
        )

        root.addWidget(self._quickstart_card())
        root.addWidget(self._snapshot_card())

        root.addWidget(self._guide_card())

        config_card, config = make_card()
        root.addWidget(config_card)
        config.addWidget(section_label("Configuration", level=2))
        config.addWidget(
            info_label(
                "Commander respects XDG_CONFIG_HOME; the paths below assume the "
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
                    "Fill in the Anomaly path, G.A.M.M.A. path and Cache path, "
                    "then Create Profile — it activates automatically. Use "
                    "absolute paths; the CLI defaults are relative to where the "
                    "app was started."
                ),
                "profiles",
                "Open Profiles",
            ),
            (
                "02",
                "Install Anomaly, then G.A.M.M.A.",
                (
                    "Install Anomaly, then Start Full Install for G.A.M.M.A. "
                    "Expect a very large download (~150 GB, or ~100 GB with "
                    "Minimal). Start Full Install is idempotent — it doubles as "
                    "the update and repair path."
                ),
                "install",
                "Open Install",
            ),
            (
                "03",
                "Install the runtimes",
                (
                    "Install / Update Runtimes in the Winetricks panel. MO2 will "
                    "not start without the Visual C++ and DirectX runtimes "
                    "(concrt140.dll)."
                ),
                "install",
                "Open Winetricks",
            ),
            (
                "04",
                "Launch the game",
                (
                    "Pick a runner and launch target on Play, then Launch Game. "
                    "The game starts detached — closing Commander does not kill "
                    "your session."
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
            button.clicked.connect(lambda _checked=False, key=page: self.window.set_page(key))
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
                    "Anomaly and G.A.M.M.A., Winetricks runtime status, storage "
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
                    "G.A.M.M.A. mod is loaded."
                ),
                [
                    "Auto runner detection — UMU Proton, then Steam Proton, then Wine",
                    "Per-runner prefixes, live command preview with a copy button",
                    "Detached launch, with launcher.log diagnostics",
                ],
                "play",
            ),
            (
                "Install",
                (
                    "Full Anomaly and G.A.M.M.A. installation with a live per-addon "
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
                    "mods grouped by the _separator category entries G.A.M.M.A. "
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
            button.clicked.connect(lambda _checked=False, key=page: self.window.set_page(key))
            card_layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(card_widget, index // 2, index % 2)
        layout.addLayout(grid)
        return card

    def _troubleshooting_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Troubleshooting", level=2))
        rows = [
            (
                "CLI Not Found on startup",
                (
                    "The bundled binary is missing or not executable — chmod +x "
                    "cli/usr/bin/stalker-gamma, or set STALKER_GAMMA_CLI."
                ),
            ),
            (
                "MO2 exits immediately; log mentions concrt140.dll",
                (
                    "Winetricks runtimes are not installed in the prefix — "
                    "Install page → Install / Update Runtimes."
                ),
            ),
            (
                "wine client error: version mismatch",
                (
                    "The prefix was built by a different Wine/Proton version. "
                    "Select the original runner, or use a separate prefix for the "
                    "new one."
                ),
            ),
            (
                "Play page has no launch targets",
                (
                    "G.A.M.M.A. is not installed yet, or ModOrganizer.ini has no "
                    "[customExecutables] section."
                ),
            ),
            (
                "Mod Manager edits are disabled",
                (
                    "Mod Organizer is running — close it first; it rewrites "
                    "modlist.txt on exit and would discard your edits."
                ),
            ),
            (
                "Install folders look wrong / files in odd places",
                "Profile paths are relative. Set absolute paths on the Profiles page.",
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
            self._set_snapshot_value("Runner", load_gui_settings().get("runner", "auto"))
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
