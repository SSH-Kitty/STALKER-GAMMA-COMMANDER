"""In-app Help: configure and use COMMANDER through the GUI."""

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
from .common import info_label, make_card, section_label


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
        outer.addWidget(scroll)

        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(14)
        scroll.setWidget(content)

        root.addWidget(section_label("COMMANDER Help", level=1))
        root.addWidget(
            info_label(
                "Everything in this guide maps to a page or setting in this application. "
                "Start with Profiles, install the game, configure your runner, then launch "
                "through Mod Organizer 2."
            )
        )

        self.snapshot_card, snapshot = make_card()
        root.addWidget(self.snapshot_card)
        snapshot.addWidget(section_label("Current configuration", level=2))
        self.snapshot_status = info_label("")
        self.snapshot_status.setObjectName("accent")
        snapshot.addWidget(self.snapshot_status)
        self.snapshot_values = QVBoxLayout()
        self.snapshot_values.setSpacing(4)
        snapshot.addLayout(self.snapshot_values)

        steps = QGridLayout()
        steps.setSpacing(14)
        root.addLayout(steps)

        steps.addWidget(
            self._step_card(
                "01  Profile",
                "Create or activate a profile before installing anything. The profile "
                "stores the Anomaly, GAMMA and cache paths, your MO2 profile name, and "
                "the number of simultaneous downloads.",
                [
                    ("Anomaly path", "The base S.T.A.L.K.E.R. Anomaly folder."),
                    ("GAMMA path", "The folder containing ModOrganizer.exe."),
                    ("Cache path", "A location with enough room for downloaded archives."),
                    ("MO2 profile", "Usually G.A.M.M.A."),
                    ("Download threads", "Higher values use more bandwidth and disk I/O."),
                ],
                "profiles",
                "Open Profiles",
            ),
            0,
            0,
        )
        steps.addWidget(
            self._step_card(
                "02  Install",
                "The Install page uses the active profile. Install Anomaly first when "
                "needed, then use Start Full Install for the GAMMA modpack. Progress, "
                "logs and cancellation are shown in the same page.",
                [
                    ("Install Anomaly", "Installs the base game engine."),
                    ("Start Full Install", "Downloads and extracts the GAMMA content."),
                    ("Verify Integrity", "Checks Anomaly and installed GAMMA files."),
                ],
                "install",
                "Open Install",
            ),
            0,
            1,
        )
        steps.addWidget(
            self._step_card(
                "03  Runtimes",
                "Winetricks Configuration is on the Install page, immediately left of "
                "Verify Integrity. It installs the native DirectX and Microsoft Visual C++ "
                "runtime libraries required by Mod Organizer 2.",
                [
                    ("Status", "The counter is shown inside the configuration card."),
                    ("Install / Update Runtimes", "Installs all required runtime verbs."),
                    ("Prefix", "The Wine or Proton prefix selected on Play."),
                ],
                "install",
                "Open Winetricks Configuration",
            ),
            1,
            0,
        )
        steps.addWidget(
            self._step_card(
                "04  Play",
                "Play launches the selected executable through Mod Organizer 2 so the "
                "GAMMA virtual file system and enabled mods are active.",
                [
                    ("Runner", "Auto, UMU Proton, Wine, or a discovered Steam Proton."),
                    ("Prefix", "Stored separately for each runner choice."),
                    ("Target", "Select the configured Anomaly executable."),
                    ("Command", "Review or copy the generated launch command."),
                ],
                "play",
                "Open Play",
            ),
            1,
            1,
        )

        root.addWidget(self._workflow_card())
        root.addWidget(self._troubleshooting_card())
        root.addStretch(1)

        self.refresh()

    def _step_card(
        self,
        title: str,
        description: str,
        details: list[tuple[str, str]],
        page: str,
        button_text: str,
    ) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(title, level=2))
        layout.addWidget(info_label(description))
        for label, text in details:
            row = QHBoxLayout()
            key = QLabel(label)
            key.setObjectName("dim")
            value = QLabel(text)
            value.setWordWrap(True)
            row.addWidget(key, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(value, 1)
            layout.addLayout(row)
        button = QPushButton(button_text)
        button.setObjectName("secondary")
        button.clicked.connect(lambda: self.window.set_page(page))
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        return card

    def _workflow_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Updates, mods and maintenance", level=2))
        layout.addWidget(
            info_label(
                "Use Update to check for addon changes before applying them. The update "
                "options let you preserve user.ltx and MCM settings, remove archives after "
                "extraction."
            )
        )
        layout.addWidget(
            info_label(
                "Mod Manager lists the active MO2 profile and lets you inspect, enable, "
                "disable, delete and back up mods. Utilities contains Anomaly integrity, "
                "shader cache, ReShade, cache pruning, logs and Fresh Reset."
            )
        )
        row = QHBoxLayout()
        for page, text in (
            ("update", "Open Update"),
            ("modmanager", "Open Mod Manager"),
            ("utilities", "Open Utilities"),
        ):
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, key=page: self.window.set_page(key))
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        return card

    def _troubleshooting_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Troubleshooting", level=2))
        layout.addWidget(
            info_label(
                "No active profile: create or activate one on Profiles. Missing paths: "
                "check the three folder fields and confirm the folders are writable."
            )
        )
        layout.addWidget(
            info_label(
                "Mod Organizer fails at startup: open Install, confirm the Winetricks "
                "counter, and install the runtimes into the same prefix selected on Play."
            )
        )
        layout.addWidget(
            info_label(
                f"Launch logs are written to {logs_dir() / 'launcher.log'}. Utilities can "
                "open the logs folder. A Proton prefix warning usually means the selected "
                "runner and prefix do not match or an older Wine process is still running."
            )
        )
        return card

    def _set_snapshot_value(self, label: str, value: str) -> None:
        row = QHBoxLayout()
        key = QLabel(label)
        key.setObjectName("dim")
        val = QLabel(value or "Not configured")
        val.setWordWrap(True)
        val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(key, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(val, 1)
        self.snapshot_values.addLayout(row)

    def refresh(self) -> None:
        self.window.refresh_settings()
        self.settings = self.window.settings
        while self.snapshot_values.count():
            item = self.snapshot_values.takeAt(0)
            if item.layout() is not None:
                child = item.layout()
                while child.count():
                    child.takeAt(0).widget()

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
