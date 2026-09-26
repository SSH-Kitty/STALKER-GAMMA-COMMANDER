"""The Achievements window, opened from the Play screen.

Every achievement as ``icon | name``, two columns, with its state on the
right: "Unlocked", progress towards a count ("143 / 200") or "Locked". The
icon is drawn in the theme's accent colour when unlocked and grey when not.
A on a row opens its details: what unlocks it and what it gives.

Unlocked flags and progress come from the newest save
(``game_stats.latest_save_stats``); the list itself - names, requirements,
rewards - is ``commander_gui.achievements``. The icons are COMMANDER's own
(``commander_gui/assets/achievements``), not Anomaly's.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget

from commander_gui.achievements import (
    ACHIEVEMENTS,
    Achievement,
    status_text,
    unlocked,
    unlocked_count,
)
from commander_gui.game_stats import SaveStats
from commander_gui.i18n import tr
from commander_gui.ui.brand_icons import achievement_color, achievement_icon

from .scale import px
from .widgets import DeckOverlay, DeckRow, DeckSmoothScrollArea, deck_label, repolish

#: Icon size inside a row, and in the detail panel.
_ROW_ICON = 44
_DETAIL_ICON = 96


def _icon_label(key: str, is_unlocked: bool, size: int) -> QLabel:
    label = QLabel()
    label.setFixedSize(px(size), px(size))
    label.setPixmap(
        achievement_icon(key, achievement_color(is_unlocked), px(size)).pixmap(px(size), px(size))
    )
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    return label


class AchievementRow(DeckRow):
    """``icon | name`` on the left, state on the right."""

    def __init__(self, achievement: Achievement, save: SaveStats | None) -> None:
        super().__init__(tr(achievement.name), status_text(save, achievement), chevron=False)
        self.achievement = achievement
        self.is_unlocked = unlocked(save, achievement)
        self.icon = _icon_label(achievement.key, self.is_unlocked, _ROW_ICON)
        layout = self.layout()
        layout.setContentsMargins(px(14 + 6), 0, px(18), 0)
        layout.setSpacing(px(14))
        layout.insertWidget(0, self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        # Locked rows read dimmer than unlocked ones, name and state both.
        self.title_label.setObjectName(
            "deckRowTitle" if self.is_unlocked else "deckRowTitleDim"
        )
        self.value_label.setObjectName(
            "deckRowValueOk" if self.is_unlocked else "deckRowValue"
        )
        repolish(self.title_label)
        repolish(self.value_label)


def _detail(achievement: Achievement, save: SaveStats | None) -> QWidget:
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(px(10))
    is_unlocked = unlocked(save, achievement)
    icon = _icon_label(achievement.key, is_unlocked, _DETAIL_ICON)
    layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)
    state = deck_label(status_text(save, achievement), role="caption")
    state.setAlignment(Qt.AlignmentFlag.AlignCenter)
    if is_unlocked:
        state.setObjectName("deckCaptionAccent")
    layout.addWidget(state)
    for heading, text in (
        (tr("How to unlock"), tr(achievement.how)),
        (tr("Reward"), tr(achievement.reward)),
    ):
        layout.addWidget(deck_label(heading, role="section"))
        layout.addWidget(deck_label(text, role="body", wrap=True))
    return body


def show_achievements(window, save: SaveStats | None) -> None:
    body = QWidget()
    outer = QVBoxLayout(body)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(px(10))
    summary = tr(
        "{count} of {total} unlocked",
        count=unlocked_count(save),
        total=len(ACHIEVEMENTS),
    )
    if save is not None:
        summary += "   ·   " + tr("From save: {name}", name=save.save_name)
    caption = deck_label(summary, role="caption")
    outer.addWidget(caption)

    scroll = DeckSmoothScrollArea()
    scroll.setObjectName("deckAchievementScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    # Not a focus stop itself: the rows are, and focusing one scrolls it in.
    scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    scroll.setMinimumHeight(px(420))
    grid_host = QWidget()
    grid = QGridLayout(grid_host)
    grid.setContentsMargins(0, 0, px(8), 0)
    grid.setHorizontalSpacing(px(12))
    grid.setVerticalSpacing(px(10))
    rows: list[AchievementRow] = []
    # Down the left column, then the right: the D-pad reads them in order.
    half = (len(ACHIEVEMENTS) + 1) // 2
    for index, achievement in enumerate(ACHIEVEMENTS):
        row = AchievementRow(achievement, save)
        row.activated.connect(
            lambda a=achievement: window.show_overlay(
                DeckOverlay(
                    tr(a.name),
                    _detail(a, save),
                    [(tr("Close"), window.dismiss_overlay, "primary")],
                    panel_width=760,
                ),
                stacked=True,
            )
        )
        grid.addWidget(row, index % half, index // half)
        rows.append(row)
    scroll.setWidget(grid_host)
    from .focus import enable_kinetic_scroll

    enable_kinetic_scroll(scroll)
    outer.addWidget(scroll, 1)

    overlay = DeckOverlay(
        tr("Achievements"),
        body,
        [(tr("Close"), window.dismiss_overlay, "normal")],
        panel_width=1180,
        translucent=True,
    )
    overlay.default_button = rows[0]
    overlay.hints = (("A", "Details"), ("B", "Close"))
    overlay.rows = rows
    window.show_overlay(overlay)
