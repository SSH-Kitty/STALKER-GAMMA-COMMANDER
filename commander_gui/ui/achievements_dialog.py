"""The desktop Achievements window, opened from the Dashboard's Game stats card.

Every achievement down the left - ``icon | name | state``, with a thin bar
under the ones that count towards a goal - and the selected one's details on
the right: what unlocks it and what it gives. Deck Mode shows the same list
as a two-column overlay instead (``Steamdeck/achievements.py``).

Unlocked flags and progress come from the newest save
(``game_stats.latest_save_stats``); the list itself is
``commander_gui.achievements``.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..achievements import (
    ACHIEVEMENTS,
    Achievement,
    progress,
    status_text,
    unlocked,
    unlocked_count,
)
from ..game_stats import SaveStats
from ..i18n import tr
from .brand_icons import achievement_color, achievement_icon
from .common import info_label, make_card, section_label

_ROW_ICON = 32
_DETAIL_ICON = 96


def _icon_label(achievement: Achievement, is_unlocked: bool, size: int) -> QLabel:
    label = QLabel()
    label.setFixedSize(size, size)
    label.setPixmap(
        achievement_icon(achievement.key, achievement_color(is_unlocked), size).pixmap(
            size, size
        )
    )
    return label


class AchievementRow(QWidget):
    """One list entry: icon, name and state, and a progress bar if it counts."""

    def __init__(self, achievement: Achievement, save: SaveStats | None) -> None:
        super().__init__()
        is_unlocked = unlocked(save, achievement)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 10, 6)
        layout.setSpacing(10)
        layout.addWidget(_icon_label(achievement, is_unlocked, _ROW_ICON))

        text = QVBoxLayout()
        text.setSpacing(4)
        top = QHBoxLayout()
        self.name_label = QLabel(tr(achievement.name))
        self.name_label.setObjectName(
            "achievementName" if is_unlocked else "achievementNameLocked"
        )
        top.addWidget(self.name_label, 1)
        self.state_label = QLabel(status_text(save, achievement))
        self.state_label.setObjectName("accent" if is_unlocked else "dim")
        top.addWidget(self.state_label)
        text.addLayout(top)
        counted = progress(save, achievement)
        if counted is not None and not is_unlocked:
            meter = QProgressBar()
            meter.setObjectName("achievementMeter")
            meter.setTextVisible(False)
            meter.setRange(0, counted[1])
            meter.setValue(counted[0])
            text.addWidget(meter)
        layout.addLayout(text, 1)


class AchievementsDialog(QDialog):
    def __init__(self, parent: QWidget | None, save: SaveStats | None) -> None:
        super().__init__(parent)
        self.save = save
        self.setWindowTitle(tr("Achievements"))
        self.resize(920, 600)
        self.setMinimumSize(720, 460)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(section_label(tr("Achievements"), level=2))
        summary = tr(
            "{count} of {total} unlocked",
            count=unlocked_count(save),
            total=len(ACHIEVEMENTS),
        )
        if save is not None:
            summary += "   ·   " + tr("From save: {name}", name=save.save_name)
        caption = info_label(summary)
        caption.setObjectName("dim")
        layout.addWidget(caption)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.list = QListWidget()
        self.list.setObjectName("achievementList")
        self.list.setMinimumWidth(380)
        for achievement in ACHIEVEMENTS:
            item = QListWidgetItem(self.list)
            row = AchievementRow(achievement, save)
            item.setSizeHint(QSize(0, row.sizeHint().height()))
            item.setData(Qt.ItemDataRole.UserRole, achievement.key)
            self.list.setItemWidget(item, row)
        body.addWidget(self.list, 5)

        detail, detail_layout = make_card()
        detail_layout.setSpacing(8)
        self.detail_icon = QLabel()
        self.detail_icon.setFixedSize(_DETAIL_ICON, _DETAIL_ICON)
        detail_layout.addWidget(self.detail_icon, 0, Qt.AlignmentFlag.AlignHCenter)
        self.detail_name = section_label("", level=1)
        self.detail_name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        detail_layout.addWidget(self.detail_name)
        self.detail_state = QLabel("")
        self.detail_state.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        detail_layout.addWidget(self.detail_state)
        detail_layout.addSpacing(8)
        detail_layout.addWidget(section_label(tr("How to unlock"), level=2))
        self.detail_how = info_label("")
        detail_layout.addWidget(self.detail_how)
        detail_layout.addSpacing(4)
        detail_layout.addWidget(section_label(tr("Reward"), level=2))
        self.detail_reward = info_label("")
        detail_layout.addWidget(self.detail_reward)
        detail_layout.addStretch(1)
        body.addWidget(detail, 4)
        layout.addLayout(body, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.list.currentRowChanged.connect(self._show_detail)
        self.list.setCurrentRow(0)

    def _show_detail(self, index: int) -> None:
        if not 0 <= index < len(ACHIEVEMENTS):
            return
        achievement = ACHIEVEMENTS[index]
        is_unlocked = unlocked(self.save, achievement)
        self.detail_icon.setPixmap(
            achievement_icon(
                achievement.key, achievement_color(is_unlocked), _DETAIL_ICON
            ).pixmap(_DETAIL_ICON, _DETAIL_ICON)
        )
        self.detail_name.setText(tr(achievement.name))
        self.detail_state.setText(status_text(self.save, achievement))
        self.detail_state.setObjectName("accent" if is_unlocked else "dim")
        self.detail_state.style().unpolish(self.detail_state)
        self.detail_state.style().polish(self.detail_state)
        self.detail_how.setText(tr(achievement.how))
        self.detail_reward.setText(tr(achievement.reward))
