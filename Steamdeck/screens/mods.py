"""Turning mods on and off.

Enable, disable and search - nothing else. Reordering a 900-entry load
order by dragging, deleting mod folders, or running a FOMOD installer are
all things that want a mouse and a big screen, and getting any of them
subtly wrong on a handheld costs the user their install.

Two implementation choices are worth knowing about:

* Rows are painted by a delegate rather than built from widgets. GAMMA ships
  500-900 mods; one widget per row would be hundreds of QWidgets and makes
  scrolling stutter on the Deck's APU.
* There is an A-Z jump strip next to the search box. Steam's on-screen
  keyboard only reliably appears for applications launched through Steam
  with Steam Input active, so the list has to stay fully navigable without
  typing anything.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QStyledItemDelegate,
    QWidget,
)

from commander_gui.i18n import tr
from commander_gui.modlist import (
    grouped,
    modlist_path_for,
    read_lines,
    set_status_at,
)
from commander_gui.themes import active_theme_tokens
from commander_gui.ui.common import mo2_running

from ..modlist_io import ModlistWriteBlocked, write_lines
from ..widgets import MIN_TOUCH, ROW_H, deck_button, deck_label
from .base import DeckScreen

#: Size of the tappable check box painted at the left of each row.
_CHECK = 36
_PAD = 16


class _ModRowDelegate(QStyledItemDelegate):
    """Paints one mod row: check box, name, category.

    A delegate rather than a row widget - see this module's docstring for
    why that matters at GAMMA's modlist sizes.
    """

    def sizeHint(self, option, index) -> QSize:
        return QSize(0, ROW_H)

    def paint(self, painter, option, index) -> None:
        tokens = active_theme_tokens()
        enabled = bool(index.data(Qt.ItemDataRole.UserRole + 1))
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        category = str(index.data(Qt.ItemDataRole.UserRole + 2) or "")

        painter.save()
        rect = option.rect

        # Hairline separator. Without it a fast scroll through GAMMA's 900
        # entries reads as one block of text rather than discrete rows.
        painter.setPen(QColor(tokens.get("gridline", "#222222")))
        painter.drawLine(
            rect.left() + _PAD, rect.bottom(), rect.right() - _PAD, rect.bottom()
        )
        box_size = _CHECK
        box_y = rect.y() + (rect.height() - box_size) // 2
        box = rect.adjusted(_PAD, box_y - rect.y(), 0, 0)
        box.setWidth(box_size)
        box.setHeight(box_size)

        accent = QColor(tokens.get("accent", "#9fe96f"))
        border = QColor(tokens.get("border_strong", "#333333"))
        painter.setPen(border)
        painter.setBrush(accent if enabled else QColor(0, 0, 0, 0))
        painter.drawRoundedRect(box, 5, 5)
        if enabled:
            painter.setPen(QColor(tokens.get("accent_text", "#000000")))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "✓")

        text_x = box.right() + _PAD
        name_font = QFont(painter.font())
        name_font.setPointSizeF(max(9.0, name_font.pointSizeF() * 1.05))
        painter.setFont(name_font)
        painter.setPen(
            QColor(tokens.get("text_bright" if enabled else "text_dim", "#dddddd"))
        )
        name_rect = rect.adjusted(text_x - rect.x(), 8, -_PAD, -rect.height() // 2)
        painter.drawText(
            name_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            name,
        )

        small = QFont(painter.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() * 0.8))
        painter.setFont(small)
        painter.setPen(QColor(tokens.get("text_dim", "#888888")))
        cat_rect = rect.adjusted(
            text_x - rect.x(), rect.height() // 2, -_PAD, -8
        )
        painter.drawText(
            cat_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            category,
        )
        painter.restore()


class ModsScreen(DeckScreen):
    def build(self) -> None:
        self._lines: list[str] = []
        self._path: Path | None = None
        self._filter = "all"
        self._snapshot_taken = False

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search mods..."))
        self.search.textChanged.connect(lambda _t: self._populate())
        search_row.addWidget(self.search, 1)
        self.body.addLayout(search_row)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        self._chips = {}
        for key, label in (
            ("all", tr("All")),
            ("enabled", tr("Enabled")),
            ("disabled", tr("Disabled")),
        ):
            chip = deck_button(label, role="chip")
            chip.setCheckable(True)
            chip.clicked.connect(lambda _c=False, k=key: self._set_filter(k))
            chips.addWidget(chip, 1)
            self._chips[key] = chip
        self._chips["all"].setChecked(True)
        self.body.addLayout(chips)

        self.jump_row = QWidget()
        jump = QHBoxLayout(self.jump_row)
        jump.setContentsMargins(0, 0, 0, 0)
        jump.setSpacing(2)
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            button = deck_button(letter, role="chip")
            button.setObjectName("deckJumpChip")
            button.setMinimumHeight(MIN_TOUCH - 4)
            # A QPushButton's own size hint would keep this row wider than
            # the screen no matter what the stylesheet says.
            button.setMinimumWidth(0)
            button.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
            )
            button.clicked.connect(lambda _c=False, ch=letter: self._jump_to(ch))
            jump.addWidget(button, 1)
        self.body.addWidget(self.jump_row)

        self.list = QListWidget()
        self.list.setItemDelegate(_ModRowDelegate(self.list))
        self.list.setUniformItemSizes(True)
        self.list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.list.setMinimumHeight(ROW_H * 4)
        self.list.itemActivated.connect(self._toggle)
        self.list.itemClicked.connect(self._toggle)
        self.body.addWidget(self.list, 1)

        self.status = deck_label("", role="caption", wrap=True)
        self.body.addWidget(self.status)

    # -- loading ----------------------------------------------------------
    def refresh(self) -> None:
        profile = self.profile()
        if profile is None:
            self._lines = []
            self.list.clear()
            self.status.setText(
                tr("Create or activate a profile first (Profiles page).")
            )
            return
        self._path = modlist_path_for(profile.gamma, profile.mo2_profile)
        if self._path is None:
            self._lines = []
            self.list.clear()
            self.status.setText(tr("Not installed"))
            return
        try:
            self._lines = read_lines(self._path)
        except (OSError, ValueError) as exc:
            # A modlist we cannot parse is never written back - the desktop
            # page takes the same stance, because a partial understanding of
            # the file is how a load order gets destroyed.
            self._lines = []
            self.list.clear()
            self.status.setText(tr("Failed") + ": " + str(exc))
            return
        self._populate()

    def on_busy_changed(self, busy: bool) -> None:
        self._update_guard()

    def _update_guard(self) -> None:
        blocked = self.window.install_busy or mo2_running()
        self.list.setEnabled(not blocked)
        if blocked:
            self.status.setText(
                tr(
                    "Close Mod Organizer first - it would overwrite your "
                    "changes when it exits."
                )
                if mo2_running()
                else tr("An install is already running.")
            )

    # -- rendering --------------------------------------------------------
    def _set_filter(self, key: str) -> None:
        self._filter = key
        for chip_key, chip in self._chips.items():
            chip.setChecked(chip_key == key)
        self._populate()

    def _populate(self) -> None:
        needle = self.search.text().strip().lower()
        self.list.clear()
        shown = enabled_count = 0
        # Parsed once: this runs on every keystroke in the search box, over a
        # modlist that is 500-900 entries in a real GAMMA install.
        groups = grouped(self._lines)
        for category, mods in groups:
            for status, name, line_index in mods:
                enabled = status == "Enabled"
                enabled_count += int(enabled)
                if self._filter == "enabled" and not enabled:
                    continue
                if self._filter == "disabled" and enabled:
                    continue
                if needle and needle not in name.lower():
                    continue
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, line_index)
                item.setData(Qt.ItemDataRole.UserRole + 1, enabled)
                item.setData(Qt.ItemDataRole.UserRole + 2, category)
                self.list.addItem(item)
                shown += 1
        if self.list.count():
            self.list.setCurrentRow(0)
        total = sum(len(mods) for _c, mods in groups)
        self.status.setText(
            tr("{enabled} Mods", enabled=enabled_count)
            + f"  /  {total}"
            + (f"   ·   {shown} " + tr("shown") if needle or self._filter != "all" else "")
        )
        self._update_guard()

    def _jump_to(self, letter: str) -> None:
        for row in range(self.list.count()):
            text = self.list.item(row).text().lstrip()
            if text[:1].upper() >= letter:
                self.list.setCurrentRow(row)
                self.list.scrollToItem(
                    self.list.item(row), QListWidget.ScrollHint.PositionAtTop
                )
                self.list.setFocus(Qt.FocusReason.OtherFocusReason)
                return

    # -- editing ----------------------------------------------------------
    def _toggle(self, item: QListWidgetItem) -> None:
        if self._path is None:
            return
        line_index = item.data(Qt.ItemDataRole.UserRole)
        enabled = bool(item.data(Qt.ItemDataRole.UserRole + 1))
        try:
            new_lines = set_status_at(self._lines, int(line_index), not enabled)
            write_lines(
                self.window,
                self._path,
                new_lines,
                snapshot=not self._snapshot_taken,
            )
        except ModlistWriteBlocked as exc:
            # Nothing was written, so the row must go back to what the file
            # still says - leaving it flipped would misreport the load order.
            self.window.notify(str(exc), 6000)
            self._update_guard()
            return
        except ValueError as exc:
            self.window.notify(tr("Failed") + ": " + str(exc), 6000)
            return
        self._snapshot_taken = True
        self._lines = new_lines
        item.setData(Qt.ItemDataRole.UserRole + 1, not enabled)
        self.list.viewport().update()
        self.window.update_mod_counter()
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        groups = grouped(self._lines)
        enabled_count = sum(
            1
            for _c, mods in groups
            for status, _n, _i in mods
            if status == "Enabled"
        )
        total = sum(len(mods) for _c, mods in groups)
        self.status.setText(tr("{enabled} Mods", enabled=enabled_count) + f"  /  {total}")

    # -- back -------------------------------------------------------------
    def on_back(self) -> bool:
        """B clears an active search before it leaves the screen."""
        if self.search.text():
            self.search.clear()
            return True
        if self._filter != "all":
            self._set_filter("all")
            return True
        return False
