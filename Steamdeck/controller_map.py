"""The controller layout overlay: a drawn pad with what every button does.

Modelled on Steam's own "Controller Settings" screen - line-art controller
in the middle, callouts for the shoulder buttons and View/Menu on either
side, and the rest grouped in columns underneath. COMMANDER's radiation
trefoil sits where the guide button would be, redrawn flat in the same
line-art style rather than pasted in as the photo-like app icon.

The pad is painted with QPainterPath from a fixed design box and scaled to
the widget, like ``commander_gui/ui/deck_icon.py`` - no image files, and
every colour is read from the active theme on each paint, so it follows a
theme change. Opened with the View button (⧉) from any screen, or the
"Controls" prompt in the footer.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from commander_gui.i18n import tr
from commander_gui.themes import active_theme_tokens

from .scale import px
from .widgets import DeckOverlay, deck_label

#: Design box. The controller occupies the middle 400 units; the callouts
#: use the space either side.
_W, _H = 1000.0, 360.0
#: Where the controller's own 400x340 drawing sits inside the design box.
_PAD_X, _PAD_Y = 300.0, 14.0

#: Xbox face-button colours - the one place the diagram does not follow
#: the theme, because these colours *are* the buttons' names on a pad.
FACE_COLOURS = {"A": "#4caf50", "B": "#e5533d", "X": "#2f8bdb", "Y": "#e8b92f"}


def _tok(name: str, fallback: str) -> QColor:
    color = QColor(active_theme_tokens().get(name, fallback))
    return color if color.isValid() else QColor(fallback)


def _pad(x: float, y: float) -> QPointF:
    """A point in the controller's own drawing, in design-box units."""
    return QPointF(_PAD_X + x, _PAD_Y + y)


def _body_path() -> QPainterPath:
    path = QPainterPath(_pad(70, 55))
    for c1, c2, end in (
        ((130, 40), (270, 40), (330, 55)),  # top edge
        ((380, 65), (395, 110), (398, 170)),  # right shoulder
        ((402, 240), (395, 300), (360, 330)),  # right grip
        ((335, 345), (310, 335), (300, 315)),  # right grip tip
        ((285, 285), (270, 258), (245, 255)),  # inner right
        ((220, 252), (180, 252), (155, 255)),  # bottom middle
        ((130, 258), (115, 285), (100, 315)),  # inner left
        ((90, 335), (65, 345), (40, 330)),  # left grip tip
        ((5, 300), (-2, 240), (2, 170)),  # left grip
        ((5, 110), (20, 65), (70, 55)),  # left shoulder
    ):
        path.cubicTo(_pad(*c1), _pad(*c2), _pad(*end))
    path.closeSubpath()
    return path


#: Callouts: (side, button pill, label, point on the pad it points to).
def _callouts() -> list[tuple[str, str, str, QPointF]]:
    return [
        ("left", "LT", tr("Previous tab"), _pad(105, 14)),
        ("left", "LB", tr("Previous tab"), _pad(95, 44)),
        ("left", "⧉", tr("This controller layout"), _pad(170, 120)),
        ("right", "RT", tr("Next tab"), _pad(295, 14)),
        ("right", "RB", tr("Next tab"), _pad(305, 44)),
        ("right", "☰", tr("Menu: play, exit, quit"), _pad(230, 120)),
    ]


class ControllerDiagram(QWidget):
    """The painted controller with its shoulder / View / Menu callouts."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(px(300))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            scale = min(self.width() / _W, self.height() / _H)
            painter.translate(
                (self.width() - _W * scale) / 2, (self.height() - _H * scale) / 2
            )
            painter.scale(scale, scale)
            self._paint(painter)
        finally:
            painter.end()

    # -- drawing, in design-box units ---------------------------------------
    def _paint(self, p: QPainter) -> None:
        line = _tok("text_dim", "#8a9a86")
        bright = _tok("text_bright", "#eef3ea")
        accent = _tok("accent", "#9fe96f")
        pen = QPen(line, 2.2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Triggers: tabs rising behind the shoulders, then the bumpers.
        for left in (True, False):
            def at(x: float, y: float, left: bool = left) -> QPointF:
                return _pad(x if left else 400 - x, y)

            trigger = QPainterPath(at(72, 44))
            trigger.cubicTo(at(70, 20), at(78, 6), at(100, 5))
            trigger.lineTo(at(122, 5))
            trigger.cubicTo(at(140, 6), at(146, 20), at(146, 38))
            p.drawPath(trigger)
        for left in (True, False):
            bumper = QPainterPath()
            if left:
                bumper.moveTo(_pad(52, 60))
                bumper.cubicTo(_pad(66, 34), _pad(118, 26), _pad(158, 36))
            else:
                bumper.moveTo(_pad(348, 60))
                bumper.cubicTo(_pad(334, 34), _pad(282, 26), _pad(242, 36))
            p.drawPath(bumper)

        p.drawPath(_body_path())

        # Sticks: an outer ring and the cap.
        for cx, cy in ((95, 120), (255, 205)):
            p.drawEllipse(_pad(cx, cy), 36, 36)
            p.drawEllipse(_pad(cx, cy), 25, 25)

        # D-pad: a plus inside a ring.
        p.drawEllipse(_pad(145, 205), 38, 38)
        plus = QPainterPath()
        arm, reach = 11, 27
        cx, cy = 145, 205
        for x, y in (
            (-arm, -reach), (arm, -reach), (arm, -arm), (reach, -arm), (reach, arm),
            (arm, arm), (arm, reach), (-arm, reach), (-arm, arm), (-reach, arm),
            (-reach, -arm), (-arm, -arm),
        ):
            point = _pad(cx + x, cy + y)
            if plus.elementCount() == 0:
                plus.moveTo(point)
            else:
                plus.lineTo(point)
        plus.closeSubpath()
        p.drawPath(plus)

        # View and Menu, with their glyphs.
        glyph_font = QFont(self.font())
        glyph_font.setPixelSize(14)
        for x, glyph in ((170, "⧉"), (230, "☰")):
            p.setPen(pen)
            p.drawEllipse(_pad(x, 120), 12, 12)
            p.setPen(bright)
            p.setFont(glyph_font)
            p.drawText(
                QRectF(_pad(x - 12, 108), _pad(x + 12, 132)),
                Qt.AlignmentFlag.AlignCenter,
                glyph,
            )

        # Face buttons, filled in their colours, letter on top.
        letter_font = QFont(self.font())
        letter_font.setPixelSize(15)
        letter_font.setBold(True)
        p.setFont(letter_font)
        for letter, (x, y) in {
            "Y": (305, 90), "X": (275, 120), "B": (335, 120), "A": (305, 150)
        }.items():
            p.setPen(QPen(line, 1.5))
            p.setBrush(QColor(FACE_COLOURS[letter]))
            p.drawEllipse(_pad(x, y), 14, 14)
            p.setPen(QColor("#10140f"))
            p.drawText(
                QRectF(_pad(x - 14, y - 14), _pad(x + 14, y + 14)),
                Qt.AlignmentFlag.AlignCenter,
                letter,
            )
        p.setBrush(Qt.BrushStyle.NoBrush)

        _paint_trefoil(p, _pad(200, 78), 24.0, accent)

        self._paint_callouts(p, line, bright)

    def _paint_callouts(self, p: QPainter, line: QColor, bright: QColor) -> None:
        text_font = QFont(self.font())
        text_font.setPixelSize(19)
        pill_font = QFont(self.font())
        pill_font.setPixelSize(13)
        pill_font.setBold(True)
        rows = {"left": 0, "right": 0}
        leader = QColor(line)
        leader.setAlpha(150)
        for side, button, label, target in _callouts():
            y = 70 + rows[side] * 62
            rows[side] += 1
            pill_w, pill_h = 38.0, 24.0
            if side == "left":
                pill = QRectF(_PAD_X - 34 - pill_w, y - pill_h / 2, pill_w, pill_h)
                text_rect = QRectF(0, y - 16, pill.left() - 12, 32)
                align = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                start = QPointF(pill.right() + 4, y)
            else:
                pill = QRectF(_PAD_X + 400 + 34, y - pill_h / 2, pill_w, pill_h)
                text_rect = QRectF(pill.right() + 12, y - 16, _W - pill.right() - 12, 32)
                align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                start = QPointF(pill.left() - 4, y)
            p.setPen(QPen(leader, 1.4))
            p.drawLine(start, target)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bright)
            p.drawRoundedRect(pill, pill_h / 2, pill_h / 2)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QColor("#10140f"))
            p.setFont(pill_font)
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, button)
            p.setPen(bright)
            p.setFont(text_font)
            p.drawText(text_rect, align, label)


def trefoil_path(centre: QPointF, outer: float, inner: float) -> QPainterPath:
    """The three blades of COMMANDER's radiation trefoil, as one fill path.

    Blades point up-left, up-right and down, like the app icon; each spans
    60 degrees from ``inner`` out to ``outer`` radius.
    """
    path = QPainterPath()
    outer_box = QRectF(centre.x() - outer, centre.y() - outer, outer * 2, outer * 2)
    inner_box = QRectF(centre.x() - inner, centre.y() - inner, inner * 2, inner * 2)
    # Qt's angles run counter-clockwise from 3 o'clock.
    for middle in (30.0, 150.0, 270.0):
        start = middle - 30.0
        blade = QPainterPath()
        blade.arcMoveTo(outer_box, start)
        blade.arcTo(outer_box, start, 60.0)
        blade.arcTo(inner_box, start + 60.0, -60.0)
        blade.closeSubpath()
        path.addPath(blade)
    return path


def _paint_trefoil(p: QPainter, centre: QPointF, radius: float, accent: QColor) -> None:
    """COMMANDER's icon, flat: outer ring, three blades, hub ring.

    Stroke weight matches the controller's own lines so it reads as part of
    the drawing; the blades are the one solid fill, in the theme accent.
    """
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(accent)
    # Blades run into the outer ring and the hub ring, as in the icon; both
    # rings are stroked over their ends so no seam shows.
    hub = radius * 0.3
    p.drawPath(trefoil_path(centre, radius, hub))
    p.setPen(QPen(accent, 2.2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(centre, radius, radius)
    p.drawEllipse(centre, hub, hub)
    p.restore()


def _face_dot(letter: str) -> QLabel:
    dot = QLabel(letter)
    dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
    size = px(26)
    dot.setFixedSize(size, size)
    dot.setStyleSheet(
        f"background-color: {FACE_COLOURS[letter]}; color: #10140f; "
        f"border-radius: {size // 2}px; font-weight: bold; font-size: {px(15)}px;"
    )
    return dot


def _pill(text: str) -> QLabel:
    pill = QLabel(text)
    pill.setObjectName("deckHintButton")
    pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return pill


def _column(title: str, rows: list[tuple[QLabel, str]]) -> QWidget:
    column = QWidget()
    grid = QGridLayout(column)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(px(10))
    grid.setVerticalSpacing(px(6))
    grid.addWidget(deck_label(title, role="section"), 0, 0, 1, 2)
    for index, (badge, text) in enumerate(rows, start=1):
        grid.addWidget(badge, index, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(deck_label(text, role="caption", wrap=True), index, 1)
    grid.setColumnStretch(1, 1)
    grid.setRowStretch(len(rows) + 1, 1)
    return column


def controller_map_body() -> QWidget:
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(px(12))
    body.diagram = ControllerDiagram()
    layout.addWidget(body.diagram, 1)

    columns = QHBoxLayout()
    columns.setSpacing(px(24))
    columns.addWidget(
        _column(
            tr("Moving around"),
            [
                (_pill("D-pad"), tr("Move between controls")),
                (_pill("L-stick"), tr("Move (hold to go faster)")),
                (_pill("R-stick"), tr("Scroll text and lists")),
            ],
        ),
        1,
    )
    columns.addWidget(
        _column(
            tr("Face buttons"),
            [
                (_face_dot("A"), tr("Select, toggle, open")),
                (_face_dot("B"), tr("Back one step: close, up a folder, previous page")),
                (_face_dot("X"), tr("Main action of the screen")),
                (_face_dot("Y"), tr("Second action / search")),
            ],
        ),
        1,
    )
    columns.addWidget(
        _column(
            tr("Shoulder buttons"),
            [
                (_pill("LB LT"), tr("Previous tab")),
                (_pill("RB RT"), tr("Next tab")),
            ],
        ),
        1,
    )
    columns.addWidget(
        _column(
            tr("Menu and View"),
            [
                (_pill("☰"), tr("Menu: play, exit, quit")),
                (_pill("⧉"), tr("This controller layout")),
            ],
        ),
        1,
    )
    layout.addLayout(columns)
    layout.addWidget(
        deck_label(
            tr(
                "Each screen shows its own X / Y actions in the bar at the bottom. "
                "In the keyboard: X space, Y delete, L1 shift (twice for caps). "
                "Touch works everywhere too."
            ),
            role="caption",
            wrap=True,
        )
    )
    return body


def show_controller_map(window) -> DeckOverlay:
    overlay = DeckOverlay(
        tr("Controls"),
        controller_map_body(),
        panel_width=1220,
        header_buttons=[("✕", window.dismiss_overlay, "normal")],
    )
    overlay.hints = [("B", "Close")]
    overlay.is_controller_map = True
    # Over whatever overlay is open, so closing the layout returns to it.
    window.show_overlay(overlay, stacked=window.current_overlay() is not None)
    return overlay
