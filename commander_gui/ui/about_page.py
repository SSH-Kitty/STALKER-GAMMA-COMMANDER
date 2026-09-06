"""Product About page for STALKER GAMMA COMMANDER."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from .. import __version_label__
from ..config import cli_binary_path, gui_settings_path, logs_dir, settings_path
from .common import info_label, make_card, section_label, tr

_GITHUB = "https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER"


def _mono(text: str) -> QLabel:
    """Create a selectable label for a path or other environment value."""
    label = QLabel(text)
    label.setObjectName("mono")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class AboutPage(QWidget):
    """Product overview, requirements, environment details and project credits."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 20)
        outer.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(14)
        scroll.setWidget(content)

        title = section_label(tr("ABOUT"), level=1)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(title)
        subtitle = info_label(
            tr("A focused Linux desktop companion for installing, managing and playing GAMMA.")
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(subtitle)

        root.addWidget(self._hero_card())
        root.addWidget(self._included_card())
        root.addWidget(self._how_it_works_card())
        root.addWidget(self._requirements_card())
        root.addWidget(self._environment_card())
        root.addWidget(self._credits_card())
        root.addWidget(self._license_card())
        root.addWidget(self._links_card())
        root.addStretch(1)

        self.refresh()

    def _hero_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("STALKER GAMMA COMMANDER"), level=1))
        version = QLabel(tr("COMMANDER GUI {version_label}", version_label=__version_label__))
        version.setObjectName("accent")
        layout.addWidget(version)
        layout.addWidget(
            info_label(
                tr("COMMANDER is a GUI around FaithBeam/stalker-gamma-cli. It keeps the CLI's installation workflow and adds a practical desktop interface for management, launch and repair tasks.")
            )
        )
        return card

    def _included_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("What is included"), level=2))
        for line in (
            tr("Dashboard — see profile, install, storage and update status at a glance."),
            tr("System Check — inspect Linux, CLI, graphics, Wine/Proton and runtime readiness."),
            tr("Install — set up Anomaly and GAMMA with live progress, Install Dependencies and Verify Integrity."),
            tr("Play — choose from detected GE-Proton builds, then launch through MO2 or launch Anomaly directly."),
            tr("Updates — review GAMMA addon changes and apply updates through the CLI."),
            tr("Mod Manager — safely search and edit the active MO2 modlist with backups."),
            tr("Profiles — create, edit, activate and remove COMMANDER profiles."),
            tr("Utilities — clean cache or shader files, remove ReShade, repair GOG paths, create log dumps, reset or move an installation."),
            tr("Settings — choose themes, startup behavior and desktop autostart options."),
            tr("ASSISTANT — the bundled, optional companion opens log dumps for focused analysis."),
        ):
            layout.addWidget(info_label(tr("• {line}", line=line)))
        return card

    def _how_it_works_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Safety and how it works"), level=2))
        layout.addWidget(
            info_label(
                tr("COMMANDER builds and runs real stalker-gamma-cli commands, showing their progress in the GUI instead of reimplementing the installer. The CLI continues to own downloads, checksums and GAMMA data operations.")
            )
        )
        layout.addWidget(
            info_label(
                tr("Only one install, update or repair operation runs at a time. File edits are made atomically, and destructive reset or move actions show the affected locations before they proceed.")
            )
        )
        layout.addWidget(
            info_label(
                tr("Play uses Mod Organizer 2 or a direct Anomaly target in a Wine/Proton environment. The current runner selector is GE-Proton-only; COMMANDER keeps runner prefixes separate and captures launch logs for diagnostics.")
            )
        )
        return card

    def _requirements_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Requirements"), level=2))
        for line in (
            tr("Platform — Linux desktop on x86_64 with an X11 or Wayland session."),
            tr("AppImage — glibc 2.34 or newer; Python 3.12, Qt 6 and the CLI are bundled."),
            tr("From source — Python 3.10+, PySide6 6.6+ and network access on first launch."),
            tr("To play — umu-run with GE-Proton is recommended; a suitable Wine/Proton runtime and Vulkan support are required."),
            tr("Optional — GameMode and MangoHud can be used when installed on the system."),
        ):
            layout.addWidget(info_label(tr("• {line}", line=line)))
        return card

    def _environment_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Current environment"), level=2))
        self.profile_value = self._path_row(layout, tr("Active profile"), "")
        self.cli_value = self._path_row(layout, tr("CLI binary"), str(cli_binary_path()))
        self.settings_value = self._path_row(layout, tr("CLI settings"), str(settings_path()))
        self.gui_settings_value = self._path_row(
            layout, tr("GUI settings"), str(gui_settings_path())
        )
        self.logs_value = self._path_row(layout, tr("Logs"), str(logs_dir()))
        return card

    def _credits_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Credits"), level=2))
        for line in (
            tr("FaithBeam — stalker-gamma-cli, the CLI this GUI drives."),
            tr("Grokitach and the GAMMA team — the GAMMA modpack."),
            tr("GSC Game World and the Anomaly team — the game."),
            tr("SSH-Kitty — the COMMANDER graphical interface."),
        ):
            layout.addWidget(info_label(tr("• {line}", line=line)))
        return card

    def _license_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("License"), level=2))
        layout.addWidget(
            info_label(
                tr("COMMANDER is licensed under the GNU General Public License 3.0 (GPL-3.0). It is not affiliated with GSC Game World or the GAMMA team.")
            )
        )
        return card

    def _links_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Links"), level=2))
        links = (
            (tr("Project on GitHub"), _GITHUB, "primary"),
            (tr("Releases"), f"{_GITHUB}/releases", "secondary"),
            (
                tr("stalker-gamma-cli (FaithBeam)"),
                "https://github.com/FaithBeam/stalker-gamma-cli",
                "secondary",
            ),
            (
                tr("GAMMA Modpack (Grokitach)"),
                "https://github.com/Grokitach/Stalker_GAMMA",
                "secondary",
            ),
        )
        for text, url, style in links:
            button = QPushButton(text)
            button.setObjectName(style)
            button.clicked.connect(
                lambda _checked=False, target=url: QDesktopServices.openUrl(QUrl(target))
            )
            layout.addWidget(button)
        return card

    def _path_row(self, layout: QVBoxLayout, label: str, value: str) -> QLabel:
        key = QLabel(label)
        key.setObjectName("dim")
        layout.addWidget(key)
        value_label = _mono(value)
        layout.addWidget(value_label)
        return value_label

    def refresh(self) -> None:
        profile = self.window.settings.active_profile
        self.profile_value.setText(profile.profile_name if profile else tr("No active profile"))
