"""About: what COMMANDER is, its version, credits and the legal notices.

The long-form documentation lives on the Help page (``about_page.py``);
this is the short product card. License texts are read from the bundled
``licenses/`` folder, so they show offline and match what actually ships.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version_label__
from ..config import project_root
from ..themes import active_theme_tokens
from .about_page import _DISCORD, _GITHUB
from .brand_icons import brand_icon, icon_size
from .common import info_label, make_card, open_url, section_label, tr
from .dialogs import add_close_button, style_window

#: (name, role) - the same English strings as the Help page's credits table.
_CREDITS = (
    ("SSH-Kitty", "COMMANDER's graphical interface."),
    ("Dnttnd", "Testing implementations, dev builds, bug reports, and helping polish the UI."),
    ("FaithBeam", "stalker-gamma-cli, the installer COMMANDER bundles and drives. All installation, download and checksum logic is theirs."),
    ("Grokitach and the GAMMA team", "The GAMMA modpack."),
    ("GSC Game World and the Anomaly team", "The game."),
    ("Other projects", "GE-Proton (GloriousEggroll), umu-launcher (Open Wine Components), Winetricks, Wine, Mod Organizer 2, xrRazom Co-op, 7-Zip, Qt and PySide6."),
)

_COPYRIGHT = "Copyright \u00a9 2026 SSH-Kitty and contributors"

#: (name, license, files under licenses/) for every third-party component
#: COMMANDER ships; a folder means every file in it. Rows whose files are
#: missing (Python's, outside the AppImage) are left out. Names and SPDX
#: ids are not translated. licenses/THIRD-PARTY-NOTICES.txt has the
#: versions, copyrights and source links.
_THIRD_PARTY = (
    ("stalker-gamma-cli", "GPL-3.0", ("texts/GPL-3.0-only.txt",)),
    ("libgit2", "GPL-2.0 WITH GCC-exception-2.0", ("libgit2",)),
    ("curl-impersonate", "MIT, curl, Apache-2.0, ...", ("curl-impersonate",)),
    ("7-Zip", "LGPL-2.1, BSD-3-Clause, unRAR", ("7-Zip", "texts/LGPL-2.1-or-later.txt")),
    ("Mozilla CA certificates, certifi", "MPL-2.0", ("texts/MPL-2.0.txt",)),
    ("Python", "PSF-2.0", ("Python-LICENSE.txt",)),
    ("Qt 6, PySide6, Shiboken6", "LGPL-3.0", ("texts/LGPL-3.0-only.txt", "texts/GPL-3.0-only.txt")),
    ("FFmpeg", "LGPL-2.1", ("texts/LGPL-2.1-or-later.txt",)),
    ("ICU", "Unicode-3.0", ("texts/Unicode-3.0.txt",)),
    ("XCB utilities, xkbcommon", "MIT", ("xcb-util", "libxkbcommon")),
    ("AppImage runtime", "MIT, LGPL-2.1, BSD-2-Clause", ("AppImage-runtime", "texts/LGPL-2.1-or-later.txt", "texts/BSD-2-Clause.txt")),
)


def _license_files(entries) -> list[Path]:
    root = project_root() / "licenses"
    files = []
    for entry in entries:
        path = root / entry
        files.extend(sorted(path.iterdir()) if path.is_dir() else [path])
    return files


def show_text_files(parent: QWidget, heading: str, files: list[Path]) -> None:
    """A themed read-only window with ``files`` one after another."""
    parts = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts.append(f"==== {path.name} ====\n\n{text}" if len(files) > 1 else text)
    dialog = QDialog(parent)
    dialog.setWindowTitle(heading)
    layout = QVBoxLayout(dialog)
    view = QPlainTextEdit("\n\n".join(parts))
    view.setObjectName("mono")
    view.setReadOnly(True)
    layout.addWidget(view, 1)
    add_close_button(dialog, layout)
    style_window(dialog, heading)
    dialog.resize(760, 640)
    dialog.exec()

class CreditsPage(QWidget):
    """The "About" tab: hero card, credits and the legal notices."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 20)

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

        root.addWidget(self._hero_card())
        root.addWidget(self._credits_card())
        root.addWidget(self._legal_card())
        root.addStretch(1)

        # The status bar's check is the one check: reuse its result.
        found = getattr(window, "commander_update_found", None)
        if found is not None:
            found.connect(self._on_commander_update_checked)
        self._on_commander_update_checked(getattr(window, "_commander_update_tag", None))

    def _hero_card(self) -> QWidget:
        card, layout = make_card()
        row = QHBoxLayout()
        row.setSpacing(18)
        logo = QPixmap(str(project_root() / "cli" / "stalker-gamma.png"))
        if not logo.isNull():
            image = QLabel()
            image.setPixmap(
                logo.scaled(
                    96, 96,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            image.setAlignment(Qt.AlignmentFlag.AlignTop)
            row.addWidget(image)

        text = QVBoxLayout()
        text.setSpacing(6)
        text.addWidget(section_label("STALKER GAMMA COMMANDER", level=1))
        version = QLabel(tr("COMMANDER {version_label}", version_label=__version_label__))
        version.setObjectName("accent")
        text.addWidget(version)
        text.addWidget(
            info_label(
                tr("STALKER GAMMA COMMANDER is a Linux desktop application for installing, updating, launching, repairing and managing S.T.A.L.K.E.R. Anomaly with the GAMMA modpack. GAMMA is normally installed on Windows with its own launcher and played through Mod Organizer 2. On Linux, the community answer is FaithBeam's stalker-gamma-cli, an installer that runs in a terminal. COMMANDER is a graphical front-end for that CLI, plus everything around it that the CLI does not do on its own.")
            )
        )
        row.addLayout(text, 1)
        layout.addLayout(row)

        buttons = QHBoxLayout()
        icon_color = active_theme_tokens().get("text_btn_hover", "#e0e0e0")
        for label, url, name, logo_name in (
            (tr("Join the Discord"), _DISCORD, "discordButton", "discord"),
            (tr("Project on GitHub"), _GITHUB, "secondary", "github"),
            (tr("Releases"), f"{_GITHUB}/releases", "secondary", None),
        ):
            button = QPushButton(("  " + label) if logo_name else label)
            button.setObjectName(name)
            if logo_name:
                color = "#ffffff" if logo_name == "discord" else icon_color
                button.setIcon(brand_icon(logo_name, color, 18))
                button.setIconSize(icon_size(18))
            button.setFixedHeight(44)
            button.setStyleSheet(f"QPushButton#{name} {{ padding: 0px 16px; }}")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, target=url: open_url(target))
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.update_button = QPushButton("")
        self.update_button.setObjectName("primary")
        self.update_button.hide()
        self.update_button.clicked.connect(lambda: open_url(f"{_GITHUB}/releases"))
        layout.addWidget(self.update_button)
        return card

    def _on_commander_update_checked(self, tag: object) -> None:
        if not tag or not isinstance(tag, str):
            return
        self.update_button.setText(tr("Update available: {tag} — open Releases", tag=tag))
        self.update_button.show()

    def _credits_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Credits"), level=2))
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        for row, (name, role) in enumerate(_CREDITS):
            who = QLabel(tr(name))
            who.setObjectName("accent")
            who.setAlignment(Qt.AlignmentFlag.AlignTop)
            grid.addWidget(who, row, 0)
            grid.addWidget(info_label(tr(role)), row, 1)
        layout.addLayout(grid)
        return card

    def _legal_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Legal"), level=2))
        layout.addWidget(QLabel(_COPYRIGHT))
        layout.addWidget(
            info_label(
                tr("COMMANDER is free software under the GNU General Public License version 3 (GPL-3.0). You may use, study, share and change it; if you share a changed version, it must be under the same license with its source code available. The bundled stalker-gamma-cli is GPL-3.0 as well.")
            )
        )
        layout.addWidget(
            info_label(
                tr("COMMANDER comes with ABSOLUTELY NO WARRANTY, to the extent permitted by law. See sections 15 and 16 of the license.")
            )
        )
        buttons = QHBoxLayout()
        root = project_root()
        for label, action in (
            (tr("Read the license"), lambda: show_text_files(self, tr("License"), [root / "LICENSE"])),
            (
                tr("Third-party notices"),
                lambda: show_text_files(
                    self, tr("Third-party notices"), [root / "licenses" / "THIRD-PARTY-NOTICES.txt"]
                ),
            ),
            (tr("Source code"), lambda: open_url(_GITHUB)),
            (tr("Network and privacy"), self._open_privacy),
        ):
            button = QPushButton(label)
            button.setObjectName("secondary")
            button.setFixedHeight(44)
            button.setStyleSheet("QPushButton#secondary { padding: 0px 16px; }")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, run=action: run())
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        layout.addSpacing(6)
        layout.addWidget(section_label(tr("Included software"), level=3))
        layout.addWidget(
            info_label(tr("COMMANDER ships with these open-source components. Each keeps its own license."))
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        rows = [(name, spdx, _license_files(entries)) for name, spdx, entries in _THIRD_PARTY]
        rows = [row for row in rows if all(path.is_file() for path in row[2])]
        for row, (name, spdx, files) in enumerate(rows):
            who = QLabel(name)
            who.setObjectName("accent")
            grid.addWidget(who, row, 0)
            terms = QLabel(spdx)
            terms.setObjectName("dim")
            grid.addWidget(terms, row, 1)
            button = QPushButton(tr("License"))
            button.setObjectName("pillButton")
            button.setFixedHeight(30)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, title=name, paths=files: show_text_files(self, title, paths)
            )
            grid.addWidget(button, row, 2)
        layout.addLayout(grid)

        layout.addSpacing(6)
        for text in (
            tr("COMMANDER is not affiliated with GSC Game World or the GAMMA team."),
            tr("S.T.A.L.K.E.R. is a trademark of GSC Game World. Discord and GitHub are trademarks of their owners. They are named and shown here only to identify those projects and services."),
        ):
            note = info_label(text)
            note.setObjectName("dim")
            layout.addWidget(note)
        return card

    def _open_privacy(self) -> None:
        self.window.set_page("about")
        self.window._ensure_page("about").show_topic("Network and privacy")
