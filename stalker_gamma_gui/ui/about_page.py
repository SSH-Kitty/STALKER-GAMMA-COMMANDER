"""About page for S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import cli_binary_path, gui_settings_path, logs_dir, settings_path
from .common import info_label, make_card, section_label


class AboutPage(QWidget):
    """Project overview, scope, credits and live environment details."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        # --- About the Developer (moved to top) ---
        about_dev, about_dev_layout = make_card()
        root.addWidget(about_dev)
        about_dev_layout.addWidget(section_label("About the Developer", level=2))
        about_dev_layout.addWidget(
            info_label(
                "COMMANDER is an independent project by SSH-Kitty built to simplify the "
                "setup and management of S.T.A.L.K.E.R. G.A.M.M.A. on Linux. The "
                "application brings installation, profile configuration, Wine/Proton "
                "setup, Mod Organizer 2 launching, updates, verification and "
                "troubleshooting into one desktop interface."
            )
        )
        about_dev_layout.addWidget(
            info_label(
                "Not affiliated with GSC Games or Grok's GAMMA. This is a standalone "
                "GUI for Linux to help users install G.A.M.M.A. on Linux."
            )
        )

        # --- Hero / branding ---
        hero, hero_layout = make_card()
        root.addWidget(hero)
        hero_layout.addWidget(section_label("S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER", level=1))
        version = QLabel(f"COMMANDER GUI v{__version__}")
        version.setObjectName("accent")
        hero_layout.addWidget(version)

        # --- What this project does ---
        overview, overview_layout = make_card()
        root.addWidget(overview)
        overview_layout.addWidget(section_label("What this project does", level=2))
        overview_layout.addWidget(
            info_label(
                "COMMANDER is a graphical interface around the GAMMA installation and "
                "management workflow. It brings profile settings, installation progress, "
                "integrity checks, runtime setup, updates and game launching into one place."
            )
        )
        overview_layout.addWidget(
            info_label(
                "It does not replace Mod Organizer 2 or the game. Instead, it configures "
                "the environment around them and launches the selected GAMMA executable "
                "through MO2 so the mod virtual file system remains active."
            )
        )

        # --- Project scope & how it works ---
        cards = QGridLayout()
        cards.setSpacing(16)
        root.addLayout(cards)

        scope, scope_layout = make_card()
        scope_layout.addWidget(section_label("Project scope", level=2))
        scope_layout.addWidget(
            info_label(
                "• Manage profiles and installation paths\n"
                "• Install Anomaly and the GAMMA modpack\n"
                "• Verify and repair installed content\n"
                "• Install DirectX and Visual C++ runtimes\n"
                "• Configure Wine, Proton and UMU runners\n"
                "• Launch MO2 and selected game targets\n"
                "• Apply GAMMA updates and manage mods\n"
                "• Run maintenance tools and inspect logs"
            )
        )
        cards.addWidget(scope, 0, 0)

        architecture, architecture_layout = make_card()
        architecture_layout.addWidget(section_label("How it works", level=2))
        architecture_layout.addWidget(
            info_label(
                "The interface is built with PySide6. Installation and update operations "
                "use the bundled stalker-gamma CLI, while the Play page builds runner-specific "
                "Wine, Proton or UMU commands."
            )
        )
        architecture_layout.addWidget(
            info_label(
                "CLI profile settings are stored separately from GUI launcher preferences. "
                "This keeps installation data, runner selections and prefixes predictable "
                "and easy to troubleshoot."
            )
        )
        cards.addWidget(architecture, 0, 1)

        # --- Current environment ---
        details, details_layout = make_card()
        root.addWidget(details)
        details_layout.addWidget(section_label("Current environment", level=2))
        self.profile_value = self._path_row(details_layout, "Active profile", "")
        self.cli_value = self._path_row(details_layout, "CLI binary", str(cli_binary_path()))
        self.settings_value = self._path_row(details_layout, "CLI settings", str(settings_path()))
        self.gui_settings_value = self._path_row(
            details_layout, "GUI settings", str(gui_settings_path())
        )
        self.logs_value = self._path_row(details_layout, "Logs", str(logs_dir()))

        # --- Credits (GitHub link only; developer description already above) ---
        credits, credits_layout = make_card()
        root.addWidget(credits)
        credits_layout.addWidget(section_label("Links", level=2))
        github = QPushButton("Open SSH-Kitty on GitHub")
        github.setObjectName("secondary")
        github.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/SSH-Kitty"))
        )
        credits_layout.addWidget(github, 0, Qt.AlignmentFlag.AlignLeft)

        root.addStretch(1)
        self.refresh()

    def _path_row(self, layout: QVBoxLayout, label: str, value: str) -> QLabel:
        row = QHBoxLayout()
        key = QLabel(label)
        key.setObjectName("dim")
        value_label = QLabel(value)
        value_label.setTextInteractionFlags(
            value_label.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        value_label.setWordWrap(True)
        row.addWidget(key)
        row.addWidget(value_label, 1)
        layout.addLayout(row)
        return value_label

    def refresh(self) -> None:
        profile = self.window.settings.active_profile
        self.profile_value.setText(profile.profile_name if profile else "No active profile")