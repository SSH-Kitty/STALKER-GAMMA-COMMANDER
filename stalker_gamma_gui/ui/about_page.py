"""About page for S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER.

Project overview, architecture, requirements, credits and license. Content
mirrors the project README so the About page and the docs never drift apart.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import cli_binary_path, gui_settings_path, logs_dir, settings_path
from .common import info_label, make_card, section_label

_GITHUB = "https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER"


def _equalize_widths(buttons: list[QPushButton]) -> None:
    max_w = max(btn.sizeHint().width() for btn in buttons)
    for btn in buttons:
        btn.setFixedWidth(max_w)


def _chip(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("chip")
    return label


def _mono(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("mono")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


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


class AboutPage(QWidget):
    """Project overview, scope, architecture, credits and environment details."""

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
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(14)
        scroll.setWidget(content)

        root.addWidget(self._hero_card())

        cards = QGridLayout()
        cards.setSpacing(16)
        root.addLayout(cards)
        cards.addWidget(self._what_card(), 0, 0)
        cards.addWidget(self._architecture_card(), 0, 1)
        cards.addWidget(self._features_card(), 1, 0)
        cards.addWidget(self._requirements_card(), 1, 1)
        cards.addWidget(self._installation_card(), 2, 0)
        cards.addWidget(self._environment_card(), 2, 1)

        root.addWidget(self._credits_card())
        root.addWidget(self._license_card())
        root.addWidget(self._links_card())
        root.addStretch(1)

        self.refresh()

    def _hero_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER", level=1))
        version = QLabel(f"COMMANDER GUI v{__version__}")
        version.setObjectName("accent")
        layout.addWidget(version)
        layout.addWidget(
            info_label(
                "A complete graphical front-end for installing, updating, managing "
                "and launching the S.T.A.L.K.E.R. Anomaly + G.A.M.M.A. mod pack "
                "on Linux."
            )
        )
        chips = QHBoxLayout()
        chips.setSpacing(8)
        for text in ("Linux x86_64", "Python 3.10+", "PySide6 / Qt 6", "GPL-3.0"):
            chips.addWidget(_chip(text))
        chips.addStretch(1)
        layout.addLayout(chips)
        return card

    def _what_card(self) -> QWidget:
        card, layout = make_card(expand=True)
        layout.addWidget(section_label("What this is", level=2))
        layout.addWidget(
            info_label(
                "G.A.M.M.A. is normally installed through a Windows launcher and "
                "run through Mod Organizer 2. On Linux the community solution is "
                "FaithBeam's stalker-gamma-cli — an excellent but entirely "
                "terminal-driven installer."
            )
        )
        layout.addWidget(
            info_label(
                "Commander is a desktop GUI around that CLI. It does not "
                "reimplement any installer logic: it drives the real stalker-gamma "
                "binary as a subprocess and parses its output live. Every "
                "download, checksum, ModDB fetch and extraction is performed by "
                "the upstream CLI, so results are identical to using it by hand."
            )
        )
        layout.addWidget(
            info_label(
                "On top of the CLI it adds what the CLI does not do: launching the "
                "game through Mod Organizer in a Wine/Proton prefix, editing "
                "modlist.txt, installing the Visual C++/DirectX runtimes MO2 needs, "
                "and a full MD5 integrity-and-repair pass over your installed mods."
            )
        )
        layout.addWidget(
            info_label(
                "Scope: Linux desktop, x86_64. The underlying CLI also supports "
                "Windows, but this GUI's launcher, prefix handling and runner "
                "detection are Linux-specific."
            )
        )
        layout.addStretch(1)
        return card

    def _architecture_card(self) -> QWidget:
        card, layout = make_card(expand=True)
        layout.addWidget(section_label("How it works", level=2))
        layout.addWidget(
            info_label(
                "This is a graphical version of the GAMMA setup process with "
                "extra features on top. It uses the stalker-gamma CLI as its "
                "framework: the app builds the commands, runs them in the "
                "background, and reads their progress to fill the interface, so "
                "installs and updates behave the same way every time."
            )
        )
        layout.addWidget(
            info_label(
                "While a task runs you see live progress instead of a frozen "
                "window, and you can cancel at any time. A global lock makes "
                "sure only one install or update runs at once, so two "
                "operations can never touch the same folders."
            )
        )
        layout.addWidget(
            info_label(
                "Destructive actions such as Fresh Reset and Full Uninstall "
                "double-check the folders before deleting anything, and refuse "
                "system directories, home directories and symlinks. Config and "
                "modlist files are written atomically, so a crash cannot "
                "corrupt them."
            )
        )
        layout.addStretch(1)
        return card

    def _features_card(self) -> QWidget:
        card, layout = make_card(expand=True)
        layout.addWidget(section_label("Features at a glance", level=2))
        for line in (
            (
                "Dashboard — install status, Winetricks runtimes, storage usage, "
                "background update check"
            ),
            (
                "Play — auto runner detection, per-runner prefixes, detached MO2 "
                "launch with launcher.log diagnostics"
            ),
            (
                "Install — live per-addon progress table, minimal mode, winetricks "
                "panel and Verify Integrity"
            ),
            (
                "Update — diff review (Added / Modified / Removed) then apply, "
                "holding the global install lock"
            ),
            (
                "Mod Manager — safe modlist.txt editing with backup and atomic "
                "writes, blocked while MO2 runs"
            ),
            (
                "Profiles — create/edit/activate/delete delegated to the CLI, with "
                "all repo URLs and branches"
            ),
            (
                "Utilities — integrity, shader cache, ReShade, cache pruning, "
                "logs, guarded Fresh Reset and Full Uninstall"
            ),
        ):
            layout.addWidget(info_label(f"• {line}"))
        layout.addStretch(1)
        return card

    def _requirements_card(self) -> QWidget:
        card, layout = make_card(expand=True)
        layout.addWidget(section_label("Requirements", level=2))
        for line in (
            "AppImage — x86_64, glibc 2.34+, any X11 or Wayland session",
            "Bundled — Python 3.12, Qt 6 (PySide6), the stalker-gamma CLI",
            "May need — libxcb-cursor0 / xcb-util-cursor when the xcb plugin cannot load",
            "From source — Python 3.10+, PySide6 >= 6.6",
            "To play — umu-run (recommended), Steam with any Proton, or system Wine; "
            "optionally gamemoderun",
        ):
            layout.addWidget(info_label(f"• {line}"))
        layout.addStretch(1)
        return card

    def _installation_card(self) -> QWidget:
        card, layout = make_card(expand=True)
        layout.addWidget(section_label("Installation", level=2))
        layout.addWidget(info_label("AppImage (recommended) — grab the latest "
                                    "release from GitHub."))
        layout.addWidget(_mono("chmod +x STALKER-GAMMA-COMMANDER-*-x86_64.AppImage\n"
                               "./STALKER-GAMMA-COMMANDER-*-x86_64.AppImage"))
        layout.addWidget(info_label("From source — run.sh creates the virtual "
                                    "environment and installs PySide6 for you."))
        layout.addWidget(_mono("git clone https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER.git\n"
                               "cd STALKER-GAMMA-COMMANDER && ./run.sh"))
        layout.addWidget(info_label("To point at a different CLI build:"))
        layout.addWidget(_mono("STALKER_GAMMA_CLI=/path/to/stalker-gamma ./run.sh"))
        layout.addStretch(1)
        return card

    def _environment_card(self) -> QWidget:
        card, layout = make_card(expand=True)
        layout.addWidget(section_label("Current environment", level=2))
        self.profile_value = self._path_row(layout, "Active profile", "")
        self.cli_value = self._path_row(layout, "CLI binary", str(cli_binary_path()))
        self.settings_value = self._path_row(layout, "CLI settings", str(settings_path()))
        self.gui_settings_value = self._path_row(
            layout, "GUI settings", str(gui_settings_path())
        )
        self.logs_value = self._path_row(layout, "Logs", str(logs_dir()))
        layout.addStretch(1)
        return card

    def _credits_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Credits", level=2))
        for line in (
            (
                "FaithBeam — stalker-gamma-cli, the installer this GUI drives and "
                "bundles. All installation, download, checksum and ModDB logic is "
                "theirs."
            ),
            "Grokitach and the G.A.M.M.A. team — the mod pack itself.",
            "GSC Game World and the Anomaly team — for the game.",
        ):
            layout.addWidget(info_label(f"• {line}"))
        return card

    def _license_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("License", level=2))
        layout.addWidget(
            info_label(
                "Licensed under the GNU General Public License v3.0. This project "
                "bundles and drives stalker-gamma-cli, which is GPL-3.0, so this "
                "front-end is GPL-3.0 as well."
            )
        )
        layout.addWidget(
            info_label(
                "Copyright for the underlying CLI installer logic: FaithBeam. "
                "Copyright for this Python/Qt graphical interface: SSH-Kitty. "
                "Not affiliated with GSC Game World or the G.A.M.M.A. development "
                "team."
            )
        )
        return card

    def _links_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label("Links", level=2))
        links = [
            ("Project on GitHub", _GITHUB, "primary"),
            ("Releases", f"{_GITHUB}/releases", "secondary"),
            ("stalker-gamma-cli (FaithBeam)", "https://github.com/FaithBeam/stalker-gamma-cli", "secondary"),
            ("G.A.M.M.A. mod pack (Grokitach)", "https://github.com/Grokitach/Stalker_GAMMA", "secondary"),
        ]
        row = QHBoxLayout()
        row.setSpacing(8)
        buttons = []
        for text, url, style in links:
            button = QPushButton(text)
            button.setObjectName(style)
            button.clicked.connect(
                lambda _checked=False, target=url: QDesktopServices.openUrl(QUrl(target))
            )
            row.addWidget(button)
            buttons.append(button)
        row.addStretch(1)
        layout.addLayout(row)
        QTimer.singleShot(0, lambda: _equalize_widths(buttons))
        return card

    def _path_row(self, layout: QVBoxLayout, label: str, value: str) -> QLabel:
        value_label = QLabel(value)
        value_label.setWordWrap(True)
        value_label.setTextInteractionFlags(
            value_label.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        inner = QVBoxLayout()
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)
        key = QLabel(label)
        key.setObjectName("dim")
        inner.addWidget(key)
        inner.addWidget(value_label)
        layout.addLayout(inner)
        return value_label

    def refresh(self) -> None:
        profile = self.window.settings.active_profile
        self.profile_value.setText(profile.profile_name if profile else "No active profile")
