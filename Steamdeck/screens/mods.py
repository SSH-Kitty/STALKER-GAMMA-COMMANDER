"""Turning mods on and off, and installing new ones.

Enable, disable, search, reorder, and Install mod (an archive from
Downloads or an SD card, FOMOD installers included - see
``Steamdeck/mod_installer.py``). Reordering is the desktop's drag and drop
done with the pad: hold A on a mod to pick it up, D-pad up/down to carry it
(across category headers too), A to put it down, B to put it back. Nothing
is written until it is put down, and then once. Deleting mod folders stays
a desktop job - getting it subtly wrong on a handheld costs the user their
install.

Two implementation choices are worth knowing about:

* Rows are painted by a delegate rather than built from widgets. GAMMA ships
  500-900 mods; one widget per row would be hundreds of QWidgets and makes
  scrolling stutter on the Deck's APU.
* The list is the desktop Mod Manager's tree, flattened: every MO2
  separator is a category header row ("Weapons (42)") with its mods under
  it, in MO2's own on-screen order (modlist.txt is written bottom-up, so it
  is walked in reverse) with the same priority numbers. A on a header
  collapses or expands it; Collapse all folds the whole list down to its
  categories, which is the fastest way to reach one with a thumbstick.
  Search still exists, with Deck Mode's own keyboard on Y.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
)

from commander_gui.i18n import tr
from commander_gui.modlist import (
    grouped,
    modlist_path_for,
    move_mod,
    read_lines,
    set_status_at,
)
from commander_gui.themes import active_theme_tokens
from commander_gui.ui.common import mo2_running

from .. import gamepad as pad
from ..focus import enable_kinetic_scroll
from ..modlist_io import ModlistWriteBlocked, write_lines
from ..scale import px
from ..widgets import (
    MIN_TOUCH,
    DeckOverlay,
    DeckPicker,
    deck_button,
    deck_label,
    picker_overlay,
)
from .base import DeckScreen

#: Row heights and the tappable check circle painted at the left of each mod.
_ROW = 68
_HEADER_ROW = 56
_CHECK = 38
_PAD = 18
#: Mod name, header and priority text sizes, in design pixels.
_NAME_PX = 21
_HEADER_PX = 19
_PRIORITY_PX = 15

#: Item data roles.
_LINE = Qt.ItemDataRole.UserRole  # mod: its line index in modlist.txt
_ENABLED = Qt.ItemDataRole.UserRole + 1  # mod: bool
_CATEGORY = Qt.ItemDataRole.UserRole + 2  # both: the category name
_KIND = Qt.ItemDataRole.UserRole + 3  # "header" or "mod"
_PRIORITY = Qt.ItemDataRole.UserRole + 4  # mod: MO2 priority number
_COUNT = Qt.ItemDataRole.UserRole + 5  # header: mods in the category
_COLLAPSED = Qt.ItemDataRole.UserRole + 6  # header: bool
_GRABBED = Qt.ItemDataRole.UserRole + 7  # mod: picked up to be moved


def search_icon(color: QColor, size: int) -> QIcon:
    """A painted magnifying glass - no icon font or emoji font needed."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, max(2.0, size / 11))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        radius = size * 0.3
        centre = QPointF(size * 0.42, size * 0.42)
        painter.drawEllipse(centre, radius, radius)
        offset = radius * 0.72
        painter.drawLine(
            QPointF(centre.x() + offset, centre.y() + offset),
            QPointF(size * 0.88, size * 0.88),
        )
    finally:
        painter.end()
    return QIcon(pixmap)


class _ModRowDelegate(QStyledItemDelegate):
    """Paints a category header or a mod row.

    A delegate rather than row widgets - see this module's docstring for
    why that matters at GAMMA's modlist sizes.
    """

    def sizeHint(self, option, index) -> QSize:
        header = index.data(_KIND) == "header"
        return QSize(0, px(_HEADER_ROW if header else _ROW))

    def paint(self, painter, option, index) -> None:
        tokens = active_theme_tokens()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = option.rect
        pad = px(_PAD)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if index.data(_KIND) == "header":
            self._paint_header(painter, rect, index, tokens, selected, pad)
        else:
            self._paint_mod(painter, rect, index, tokens, selected, pad)
        painter.restore()

    @staticmethod
    def _selection(painter, rect, tokens, selected: bool, fill: QColor | None) -> None:
        inner = QRectF(rect.adjusted(2, 2, -2, -2))
        if fill is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(inner, px(10), px(10))
        if selected:
            painter.setPen(QPen(QColor(tokens.get("focus", "#9fe96f")), 2))
            painter.setBrush(QColor(tokens.get("btn_hover", "#222222")))
            painter.drawRoundedRect(inner, px(10), px(10))

    def _paint_header(self, painter, rect, index, tokens, selected, pad) -> None:
        # The same "header band" treatment the desktop tree gives category
        # rows: the card colour behind bold accent text.
        self._selection(painter, rect, tokens, selected, QColor(tokens.get("btn", "#222222")))
        collapsed = bool(index.data(_COLLAPSED))
        font = QFont(painter.font())
        font.setPixelSize(px(_HEADER_PX))
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QColor(tokens.get("accent_strong", "#9fe96f")))
        arrow = "▸" if collapsed else "▾"
        text = f"{arrow}   {index.data(_CATEGORY)}"
        painter.drawText(
            rect.adjusted(pad, 0, -pad, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            text,
        )
        count = QFont(font)
        count.setPixelSize(px(_PRIORITY_PX))
        count.setWeight(QFont.Weight.Normal)
        painter.setFont(count)
        painter.setPen(QColor(tokens.get("text_dim", "#888888")))
        painter.drawText(
            rect.adjusted(pad, 0, -pad, 0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            tr("{count} mods", count=index.data(_COUNT)),
        )

    def _paint_mod(self, painter, rect, index, tokens, selected, pad) -> None:
        self._selection(painter, rect, tokens, selected, None)
        grabbed = bool(index.data(_GRABBED))
        if grabbed:
            # Picked up: a thick accent outline, so it reads as lifted off
            # the list rather than merely highlighted.
            painter.setPen(QPen(QColor(tokens.get("accent", "#9fe96f")), px(4)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(rect.adjusted(3, 3, -3, -3)), px(10), px(10))
        enabled = bool(index.data(_ENABLED))
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")

        # Hairline separator. Without it a fast scroll through GAMMA's 900
        # entries reads as one block of text rather than discrete rows.
        painter.setPen(QColor(tokens.get("gridline", "#222222")))
        painter.drawLine(
            rect.left() + pad, rect.bottom(), rect.right() - pad, rect.bottom()
        )
        indent = pad + px(12)
        box_size = px(_CHECK)
        box_y = rect.y() + (rect.height() - box_size) // 2
        box = rect.adjusted(indent, box_y - rect.y(), 0, 0)
        box.setWidth(box_size)
        box.setHeight(box_size)
        accent = QColor(tokens.get("accent", "#9fe96f"))
        border = QColor(tokens.get("border_strong", "#333333"))
        painter.setPen(border)
        painter.setBrush(accent if enabled else QColor(0, 0, 0, 0))
        painter.drawRoundedRect(box, box_size / 2, box_size / 2)
        if enabled:
            painter.setPen(QColor(tokens.get("accent_text", "#000000")))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "✓")

        # MO2 priority on the right, like the desktop tree's second column.
        small = QFont(painter.font())
        small.setPixelSize(px(_PRIORITY_PX))
        painter.setFont(small)
        painter.setPen(QColor(tokens.get("text_dim", "#888888")))
        priority_width = px(64)
        painter.drawText(
            rect.adjusted(rect.width() - pad - priority_width, 0, -pad, 0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "⇅" if grabbed else str(index.data(_PRIORITY) or ""),
        )

        text_x = box.right() + pad
        name_font = QFont(painter.font())
        name_font.setPixelSize(px(_NAME_PX))
        name_font.setWeight(QFont.Weight.DemiBold if enabled else QFont.Weight.Normal)
        painter.setFont(name_font)
        painter.setPen(
            QColor(tokens.get("text_bright" if enabled else "text_dim", "#dddddd"))
        )
        painter.drawText(
            rect.adjusted(text_x - rect.x(), 0, -(pad + priority_width + pad), 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            name,
        )


class ModsScreen(DeckScreen):
    def build(self) -> None:
        self._lines: list[str] = []
        self._path: Path | None = None
        self._filter = "all"
        self._shown = 0
        self._snapshot_taken = False
        self._collapsed: set[str] = set()
        #: The mod row picked up with a held A, while it is being carried.
        self._grabbed: QListWidgetItem | None = None
        self._grab_warned = False

        # The list is the page: no search box eating a row above it. Search
        # is a magnifier button at the right of the filter chips (or Y), and
        # the text lives in a hidden field the on-screen keyboard types into.
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search mods..."))
        self.search.textChanged.connect(self._on_search_changed)
        self.search.hide()

        # Nothing here needs the page to scroll - the list scrolls itself -
        # and a scrolling page around a scrolling list fights the finger.
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        chips = QHBoxLayout()
        chips.setSpacing(px(8))
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
        self.category_button = deck_button(
            "☰  " + tr("Categories"), role="chip", on_click=self._pick_category
        )
        chips.addWidget(self.category_button, 1)
        self.collapse_button = deck_button(
            tr("Collapse all"), role="chip", on_click=self._toggle_collapse_all
        )
        chips.addWidget(self.collapse_button, 1)
        self.search_button = deck_button("", role="chip", on_click=self.open_search)
        self.search_button.setObjectName("deckIconChip")
        self.search_button.setIcon(search_icon(QColor(active_theme_tokens().get("text", "#dddddd")), px(26)))
        self.search_button.setIconSize(QSize(px(26), px(26)))
        self.search_button.setFixedWidth(px(MIN_TOUCH + 32))
        self.search_button.setToolTip(tr("Search mods..."))
        chips.addWidget(self.search_button)
        self.install_button = deck_button(
            "＋  " + tr("Install mod"), role="chip", on_click=self._install_mod
        )
        chips.addWidget(self.install_button, 1)
        self.body.addLayout(chips)
        self._installer = None

        # Shown only while a search is active: what is being searched for,
        # and one tap (or B) to clear it.
        self.search_chip = deck_button("", role="chip", on_click=self._clear_search)
        self.search_chip.setObjectName("deckSearchChip")
        self.search_chip.hide()
        self.body.addWidget(self.search_chip)

        self.list = QListWidget()
        self.list.setItemDelegate(_ModRowDelegate(self.list))
        self.list.setUniformItemSizes(False)
        self.list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.list.setMinimumHeight(px(_ROW) * 4)
        # itemClicked only - A reaches it too, through focus.activate(). With
        # itemActivated also connected, styles that activate on a single
        # click toggled every mod twice (i.e. not at all) and wrote twice.
        self.list.itemClicked.connect(self._on_item)
        enable_kinetic_scroll(self.list)
        self.body.addWidget(self.list, 1)

        self.status = deck_label("", role="caption", wrap=True)
        self.body.addWidget(self.status)

    # -- loading ----------------------------------------------------------
    def refresh(self) -> None:
        # Whatever was being carried belongs to the list about to be rebuilt.
        self._grabbed = None
        profile = self.profile()
        if profile is None:
            self._lines = []
            self.list.clear()
            self.status.setText(
                tr("Create or activate a profile first (Dashboard → Profiles).")
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
        self.install_button.setEnabled(not blocked)
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
        self.update_hints()

    def _populate(self) -> None:
        needle = self.search.text().strip().lower()
        current = self.list.currentItem()
        keep = None
        if current is not None:
            keep = (current.data(_KIND), current.data(_CATEGORY), current.data(_LINE))
        self.list.clear()
        shown = 0
        # Parsed once: this runs on every keystroke in the search box, over a
        # modlist that is 500-900 entries in a real GAMMA install.
        groups = grouped(self._lines)
        priority = 0
        restore_row = 0
        # Reversed, exactly like the desktop tree: MO2 writes modlist.txt
        # highest priority first but shows it at the bottom, so walking the
        # file backwards gives MO2's on-screen order and its numbering.
        for category, mods in reversed(groups):
            rows = []
            for status, name, line_index in reversed(mods):
                priority += 1
                enabled = status == "Enabled"
                if self._filter == "enabled" and not enabled:
                    continue
                if self._filter == "disabled" and enabled:
                    continue
                if needle and needle not in name.lower():
                    continue
                rows.append((name, line_index, enabled, priority))
            filtering = bool(needle) or self._filter != "all"
            if not rows and filtering:
                continue
            # A search shows every match, collapsed category or not.
            collapsed = category in self._collapsed and not needle
            header = QListWidgetItem(category)
            header.setData(_KIND, "header")
            header.setData(_CATEGORY, category)
            header.setData(_COUNT, len(rows))
            header.setData(_COLLAPSED, collapsed)
            self.list.addItem(header)
            if keep == ("header", category, None):
                restore_row = self.list.count() - 1
            if collapsed:
                continue
            for name, line_index, enabled, number in rows:
                item = QListWidgetItem(name)
                item.setData(_KIND, "mod")
                item.setData(_LINE, line_index)
                item.setData(_ENABLED, enabled)
                item.setData(_CATEGORY, category)
                item.setData(_PRIORITY, number)
                self.list.addItem(item)
                if keep == ("mod", category, line_index):
                    restore_row = self.list.count() - 1
                shown += 1
        if self.list.count():
            self.list.setCurrentRow(restore_row)
        self._shown = shown
        self._render_counts(groups)
        self._update_guard()

    def mod_items(self) -> list[QListWidgetItem]:
        """The mod rows currently in the list (headers excluded)."""
        return [
            self.list.item(row)
            for row in range(self.list.count())
            if self.list.item(row).data(_KIND) == "mod"
        ]

    def _on_item(self, item: QListWidgetItem) -> None:
        if self._grabbed is not None:
            # A (or a tap anywhere) puts the carried mod down where it is.
            self._drop()
            return
        if item.data(_KIND) == "header":
            self._toggle_category(str(item.data(_CATEGORY)))
        else:
            self._toggle(item)

    def _toggle_category(self, category: str) -> None:
        if category in self._collapsed:
            self._collapsed.discard(category)
        else:
            self._collapsed.add(category)
        self._populate()
        self._sync_collapse_button()

    def _toggle_collapse_all(self) -> None:
        categories = [name for name, _mods in grouped(self._lines)]
        if self._collapsed >= set(categories):
            self._collapsed.clear()
        else:
            self._collapsed = set(categories)
        self._populate()
        self._sync_collapse_button()
        self.list.setCurrentRow(0)

    def _sync_collapse_button(self) -> None:
        categories = {name for name, _mods in grouped(self._lines)}
        all_collapsed = bool(categories) and self._collapsed >= categories
        self.collapse_button.setText(tr("Expand all") if all_collapsed else tr("Collapse all"))

    # -- categories -------------------------------------------------------
    def _categories(self) -> list[str]:
        """Category headers in the list as currently shown, in order."""
        return [
            str(self.list.item(row).data(_CATEGORY) or "")
            for row in range(self.list.count())
            if self.list.item(row).data(_KIND) == "header"
        ]

    def _pick_category(self) -> None:
        categories = self._categories()
        if not categories:
            self.window.notify(tr("No mods to show."))
            return
        current = None
        item = self.list.currentItem()
        if item is not None:
            current = item.data(_CATEGORY)
        picker = DeckPicker(
            "",
            [(name or tr("Uncategorized"), name) for name in categories],
            current,
            searchable=len(categories) > 12,
        )
        picker.chosen.connect(self._jump_to_category)
        self.window.show_overlay(picker_overlay(self.window, tr("Jump to category"), picker))

    def _jump_to_category(self, category: object) -> None:
        self.window.dismiss_overlay(refocus=False)
        if category in self._collapsed:
            self._collapsed.discard(category)
            self._populate()
            self._sync_collapse_button()
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(_KIND) == "header" and item.data(_CATEGORY) == category:
                self.list.setCurrentRow(row)
                self.list.scrollToItem(
                    self.list.item(row), QListWidget.ScrollHint.PositionAtTop
                )
                break
        self.list.setFocus(Qt.FocusReason.OtherFocusReason)

    def _category_actions(self) -> None:
        """X on a mod: act on its whole category at once."""
        item = self.list.currentItem()
        if item is None:
            return
        category = item.data(_CATEGORY)
        label = category or tr("Uncategorized")
        body = deck_label(
            tr("Enable or disable every mod in \"{category}\"?", category=label),
            role="body",
            wrap=True,
        )
        self.window.show_overlay(
            DeckOverlay(
                label,
                body,
                [
                    (tr("Enable all"), lambda: self._set_category(category, True), "primary"),
                    (tr("Disable all"), lambda: self._set_category(category, False), "normal"),
                    (tr("Cancel"), self.window.dismiss_overlay, "normal"),
                ],
                default_index=2,
            )
        )

    def _set_category(self, category: object, enabled: bool) -> None:
        self.window.dismiss_overlay()
        if self._path is None:
            return
        indexes = [
            line_index
            for name, mods in grouped(self._lines)
            if name == category
            for _status, _name, line_index in mods
        ]
        new_lines = list(self._lines)
        for line_index in indexes:
            new_lines = set_status_at(new_lines, line_index, enabled)
        if new_lines == self._lines:
            return
        if not self._write(new_lines):
            return
        row = self.list.currentRow()
        self._populate()
        self.list.setCurrentRow(min(row, self.list.count() - 1))

    # -- search -----------------------------------------------------------
    def open_search(self) -> None:
        self.window.open_keyboard(
            self.search, title=tr("Search mods"), on_done=lambda _t: self._focus_list()
        )

    def _focus_list(self) -> None:
        target = self.list if self.list.count() else self.search_button
        target.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_search_changed(self, text: str) -> None:
        text = text.strip()
        self.search_chip.setVisible(bool(text))
        self.search_chip.setText("\u2715   " + tr("Search: {query}", query=text))
        self._populate()
        self.update_hints()

    def _clear_search(self) -> None:
        self.search.clear()
        # Back to the list the search was narrowing - the chip just pressed
        # is gone.
        self._focus_list()

    # -- reordering -------------------------------------------------------
    def wants_hold(self) -> bool:
        """Tell a held A from a tap on the list (see GamepadMonitor.hold_gate).

        Not while carrying a mod: then A puts it down, at once.
        """
        return self._grabbed is None and self.list.hasFocus() and self.list.isEnabled()

    def is_grabbing(self) -> bool:
        return self._grabbed is not None

    def _grab(self) -> bool:
        """Held A: pick up the highlighted mod. False on a header."""
        item = self.list.currentItem()
        if item is None or item.data(_KIND) != "mod":
            return False
        if self._path is None or self.window.install_busy or mo2_running():
            self._update_guard()
            return True
        if self.search.text().strip() or self._filter != "all":
            # With rows hidden, "just above this one" is not a place in the
            # load order the user can see.
            self.window.notify(tr("Clear the search and filter to move mods."), 5000)
            return True
        self._grabbed = item
        item.setData(_GRABBED, True)
        self.list.viewport().update()
        if not self._grab_warned:
            self._grab_warned = True
            self.window.notify(
                tr(
                    "Moving a mod changes the load order. D-pad moves it, "
                    "A drops it, B cancels."
                ),
                6000,
            )
        self.update_hints()
        return True

    def on_direction(self, action: str) -> bool:
        """D-pad while carrying a mod: up/down moves it, left/right do nothing."""
        item = self._grabbed
        if item is None:
            return False
        if action in (pad.UP, pad.DOWN):
            row = self.list.row(item)
            target = row + (-1 if action == pad.UP else 1)
            # Row 0 is the first category's header; nothing goes above it.
            if 1 <= target < self.list.count():
                self.list.takeItem(row)
                self.list.insertItem(target, item)
            self.list.setCurrentItem(item)
            self.list.scrollToItem(item)
        return True

    def _drop(self) -> None:
        """Put the carried mod down: one move_mod(), one write."""
        item = self._grabbed
        if item is None:
            return
        self._grabbed = None
        item.setData(_GRABBED, False)
        name = item.text()
        row = self.list.row(item)
        category = None
        for above in range(row - 1, -1, -1):
            if self.list.item(above).data(_KIND) == "header":
                category = str(self.list.item(above).data(_CATEGORY))
                break
        before = self.list.item(row - 1) if row > 0 else None
        after = self.list.item(row + 1) if row + 1 < self.list.count() else None
        # Screen order runs opposite to file order (see _populate), so
        # "below that mod on screen" is move_mod's before=True, the same
        # inversion the desktop tree's drop makes.
        if before is not None and before.data(_KIND) == "mod":
            new_lines = move_mod(self._lines, name, target_name=before.text(), before=True)
        elif after is not None and after.data(_KIND) == "mod":
            new_lines = move_mod(self._lines, name, target_name=after.text(), before=False)
        else:
            new_lines = move_mod(self._lines, name, category=category)
        if new_lines != self._lines:
            self._write(new_lines)
        self._populate()
        self._select_mod(name)
        self.update_hints()

    def _cancel_grab(self) -> None:
        """B: put the carried mod back where it was picked up."""
        item = self._grabbed
        if item is None:
            return
        self._grabbed = None
        name = item.text()
        self._populate()
        self._select_mod(name)
        self.update_hints()

    def _select_mod(self, name: str) -> None:
        for mod in self.mod_items():
            if mod.text() == name:
                self.list.setCurrentItem(mod)
                self.list.scrollToItem(mod)
                return

    def hideEvent(self, event) -> None:
        # Leaving the screen (L1/R1, a tab tap) with a mod still in hand
        # puts it back rather than leaving it half moved.
        self._cancel_grab()
        super().hideEvent(event)

    # -- input ------------------------------------------------------------
    def on_action(self, action: str) -> bool:
        if action == pad.ACCEPT_HOLD:
            if self._grabbed is not None:
                self._drop()
                return True
            return self._grab()
        if self._grabbed is not None and action in (pad.SEARCH, pad.CONTEXT):
            # Search or a category menu would rebuild the list under it.
            return True
        if action == pad.SEARCH:
            self.open_search()
            return True
        if action == pad.CONTEXT:
            if self.list.hasFocus():
                self._category_actions()
            else:
                self._pick_category()
            return True
        return False

    def hints(self):
        # X does one of two things depending on where focus is, and B peels
        # off a search or filter before it leaves - so say which.
        if self._grabbed is not None:
            return [("↑↓", "Move"), ("A", "Drop"), ("B", "Cancel")]
        on_list = self.list.hasFocus()
        if self.search.text():
            back = "Clear search"
        elif self._filter != "all":
            back = "Clear filter"
        else:
            back = "Back"
        return [
            ("A", "Toggle / fold · hold: move" if on_list else "Select"),
            ("X", "Enable / disable category" if on_list else "Jump to category"),
            ("Y", "Search"),
            ("B", back),
        ]

    def default_focus(self):
        return self.list if self.list.count() and self.list.isEnabled() else self.search_button

    # -- installing -------------------------------------------------------
    def _install_mod(self) -> None:
        from ..mod_installer import DeckModInstaller

        if self._installer is None:
            self._installer = DeckModInstaller(self)
        self._installer.start()

    def reveal_mod(self, name: str) -> None:
        """Show and select a mod by name - a fresh install, say - clearing
        whatever search, filter or folded category would hide it."""
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.search_chip.hide()
        self._filter = "all"
        for chip_key, chip in self._chips.items():
            chip.setChecked(chip_key == "all")
        category = next(
            (c for c, mods in grouped(self._lines) for _s, n, _i in mods if n == name), None
        )
        if category is not None:
            self._collapsed.discard(category)
        self._populate()
        for item in self.mod_items():
            if item.text() == name:
                self.list.setCurrentItem(item)
                self.list.scrollToItem(item)
                self.list.setFocus(Qt.FocusReason.OtherFocusReason)
                return

    # -- editing ----------------------------------------------------------
    def _toggle(self, item: QListWidgetItem) -> None:
        if self._path is None or item.data(_KIND) != "mod":
            return
        line_index = item.data(_LINE)
        enabled = bool(item.data(_ENABLED))
        try:
            new_lines = set_status_at(self._lines, int(line_index), not enabled)
        except ValueError as exc:
            self.window.notify(tr("Failed") + ": " + str(exc), 6000)
            return
        if not self._write(new_lines):
            return
        item.setData(_ENABLED, not enabled)
        self.list.viewport().update()
        self._render_counts(grouped(self._lines))

    def _write(self, new_lines: list[str]) -> bool:
        """Write through the guarded choke point; False if refused."""
        try:
            write_lines(
                self.window,
                self._path,
                new_lines,
                snapshot=not self._snapshot_taken,
            )
        except ModlistWriteBlocked as exc:
            # Nothing was written, so rows must go on showing what the file
            # still says - leaving them flipped would misreport the load order.
            self.window.notify(str(exc), 6000)
            self._update_guard()
            return False
        self._snapshot_taken = True
        self._lines = new_lines
        self.window.update_mod_counter()
        return True

    def _render_counts(self, groups) -> None:
        enabled_count = sum(
            1
            for _c, mods in groups
            for status, _n, _i in mods
            if status == "Enabled"
        )
        total = sum(len(mods) for _c, mods in groups)
        filtered = bool(self.search.text().strip()) or self._filter != "all"
        self.status.setText(
            tr("{enabled} Mods", enabled=enabled_count)
            + f"  /  {total}"
            + (f"   ·   {self._shown} " + tr("shown") if filtered else "")
        )

    # -- back -------------------------------------------------------------
    def on_back(self) -> bool:
        """B puts a carried mod back, or clears a search, before it leaves."""
        if self._grabbed is not None:
            self._cancel_grab()
            return True
        if self.search.text():
            self.search.clear()
            return True
        if self._filter != "all":
            self._set_filter("all")
            return True
        return False
