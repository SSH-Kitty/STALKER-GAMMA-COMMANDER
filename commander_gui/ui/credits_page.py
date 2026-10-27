"""About: what COMMANDER is, its version, credits and the legal notices.

The long-form documentation lives on the Help page (``about_page.py``);
this is the short product card. License texts are read from the bundled
``licenses/`` folder, so they show offline and match what actually ships.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
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
from ..themes import active_theme, active_theme_tokens
from .about_page import _DISCORD, _GITHUB
from .brand_icons import brand_icon, icon_size
from .common import info_label, make_card, open_url, section_label, tr
from .dialogs import add_close_button, style_window

#: (name, role) - the same English strings as the Help page's credits table.
_CREDITS = (
    ("SSH-Kitty", "COMMANDER's graphical interface."),
    ("Dnttnd", "Testing implementations, dev builds, bug reports, and helping polish the UI."),
    ("FaithBeam", "stalker-gamma-cli, the installer COMMANDER bundles and drives. All installation, download and checksum logic is theirs."),
    ("Harbingerr", "xrRazom Co-op, the multiplayer mod the Multiplayer page installs."),
    ("Grokitach and the GAMMA team", "The GAMMA modpack."),
    ("The Anomaly Team", "Developers of the standalone S.T.A.L.K.E.R. Anomaly modification and engine base."),
    ("GSC Game World", "Creators of the original S.T.A.L.K.E.R. games and universe."),
    ("Other projects", "Proton-GE (GloriousEggroll), umu-launcher, Winetricks and Mod Organizer 2."),
)

_COPYRIGHT = "Copyright \u00a9 2026 SSH-Kitty and contributors"

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


def _fluff(points) -> QPainterPath:
    """Overlapping circles, unioned: the fuzzy outline of fur trim."""
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    for x, y, r in points:
        path.addEllipse(QPointF(x, y), r, r)
    return path.simplified()


#: Where the hat's canvas sits over the 96 px logo, and how far it reaches.
_HAT_OFFSET = 14
_HAT_SIZE = (116, 84)


def _santa_hat(dpr: float) -> QPixmap:
    """A Santa hat for the 96 px logo, its tip flopped down the right side."""
    canvas = QPixmap(round(_HAT_SIZE[0] * dpr), round(_HAT_SIZE[1] * dpr))
    canvas.setDevicePixelRatio(dpr)
    canvas.fill(Qt.GlobalColor.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(48, _HAT_OFFSET + 22)  # the middle of the brim

    cone = QPainterPath(QPointF(-34, 0))
    cone.cubicTo(-30, -30, -4, -38, 20, -32)
    cone.cubicTo(42, -26, 54, -6, 56, 20)
    cone.cubicTo(50, 22, 47, 22, 44, 20)
    cone.cubicTo(42, 4, 36, -8, 26, -12)
    cone.quadTo(32, -6, 34, 0)
    cone.closeSubpath()
    red = QLinearGradient(-30, -34, 30, 6)
    red.setColorAt(0, QColor("#e8323f"))
    red.setColorAt(0.6, QColor("#c3142a"))
    red.setColorAt(1, QColor("#8a0c1d"))
    p.setPen(QPen(QColor("#5a0712"), 1))
    p.setBrush(red)
    p.drawPath(cone)
    # The flopped tip falls into its own shade.
    p.setClipPath(cone)
    flop = QPainterPath(QPointF(18, -32))
    flop.cubicTo(30, -24, 30, -14, 26, -12)
    flop.cubicTo(38, -8, 44, 6, 44, 24)
    flop.lineTo(70, 24)
    flop.lineTo(70, -40)
    flop.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(60, 0, 10, 70))
    p.drawPath(flop)
    p.setClipping(False)

    trim = _fluff(
        [(x, -1, 5) for x in range(-39, 40, 6)]
        + [(x, 6, 5) for x in range(-36, 37, 6)]
        + [(-41, 2.5, 5), (41, 2.5, 5)]
    )
    ball = _fluff(
        [(50, 24, 6.5)]
        + [(50 + 4.5 * math.cos(a), 24 + 4.5 * math.sin(a), 3.4) for a in (k * math.pi / 5 for k in range(10))]
    )
    p.setBrush(QColor(0, 0, 0, 60))
    p.drawPath(trim.translated(0, 2.5))
    p.drawPath(ball.translated(0.5, 2))
    fur = QLinearGradient(0, -6, 0, 11)
    fur.setColorAt(0, QColor("#ffffff"))
    fur.setColorAt(0.6, QColor("#eef1f3"))
    fur.setColorAt(1, QColor("#bcc5cc"))
    p.setPen(QPen(QColor(0, 0, 0, 45), 0.6))
    p.setBrush(fur)
    p.drawPath(trim)
    ball_fur = QRadialGradient(QPointF(48, 21), 10)
    ball_fur.setColorAt(0, QColor("#ffffff"))
    ball_fur.setColorAt(0.6, QColor("#eef1f3"))
    ball_fur.setColorAt(1, QColor("#bcc5cc"))
    p.setBrush(ball_fur)
    p.drawPath(ball)
    p.end()
    return canvas


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
        # Scoped: a bare rule would cascade to every button inside.
        scroll.viewport().setObjectName("scrollViewport")
        scroll.viewport().setStyleSheet("#scrollViewport { background: transparent; }")
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
        self._logo = QPixmap(str(project_root() / "cli" / "stalker-gamma.png"))
        if not self._logo.isNull():
            self._logo_label = QLabel()
            self._logo_label.setAlignment(Qt.AlignmentFlag.AlignTop)
            row.addWidget(self._logo_label)
            # The hat floats over the logo instead of widening it, so the
            # card lays out the same in every theme.
            self._hat = QLabel(card)
            self._hat.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._logo_label.installEventFilter(self)
            self._paint_logo()

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

    def _paint_logo(self) -> None:
        # Drawn at the screen's pixel density so the hat stays crisp.
        dpr = self.devicePixelRatioF()
        logo = self._logo.scaled(
            round(96 * dpr), round(96 * dpr),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        logo.setDevicePixelRatio(dpr)
        self._logo_label.setPixmap(logo)
        winter = active_theme() == "winter"
        if winter:
            self._hat.setPixmap(_santa_hat(dpr))
            self._hat.resize(*_HAT_SIZE)
        self._hat.setVisible(winter)

    def eventFilter(self, watched, event) -> bool:
        if watched is getattr(self, "_logo_label", None) and event.type() in (
            QEvent.Type.Move,
            QEvent.Type.Resize,
        ):
            self._hat.move(watched.x(), watched.y() - _HAT_OFFSET)
            self._hat.raise_()
        return super().eventFilter(watched, event)

    def on_theme_changed(self) -> None:
        if hasattr(self, "_logo_label"):
            self._paint_logo()

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
        return card

    def _open_privacy(self) -> None:
        self.window.set_page("about")
        self.window._ensure_page("about").show_topic("Network and privacy")
