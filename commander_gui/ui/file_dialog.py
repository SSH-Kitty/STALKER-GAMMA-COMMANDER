"""COMMANDER's own file and folder picker, drawn in the app's style.

``FileDialog`` is a drop-in for ``QFileDialog``'s static helpers: modules
import it under that name, so ``getExistingDirectory()``,
``getOpenFileName()`` and ``getSaveFileName()`` keep their signatures and
return values, and tests keep patching ``<module>.QFileDialog``.

The window: quick-access folders and drives on the left, a breadcrumb,
filter box and folder listing on the right, and "System picker" to fall
back to the desktop's own dialog.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from PySide6.QtCore import QSize, QStandardPaths, QStorageInfo, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr
from .brand_icons import line_icon as _icon
from .dialogs import MessageBox, add_buttons, style_window

#: Mount points worth listing as drives; the rest are system mounts.
_DRIVE_ROOTS = ("/run/media/", "/media/", "/mnt/", "/home")
_SKIP_FS = {"tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "ramfs", "efivarfs"}


def parse_filters(text: str) -> list[tuple[str, list[str]]]:
    """``"Zips (*.zip);;All files (*)"`` -> ``[("Zips (*.zip)", ["*.zip"]), ...]``."""
    filters = []
    for part in (text or "").split(";;"):
        part = part.strip()
        if not part:
            continue
        match = re.search(r"\(([^)]*)\)", part)
        patterns = (match.group(1) if match else part).split()
        filters.append((part, patterns or ["*"]))
    return filters


def drives() -> list[tuple[str, Path, int]]:
    """``(name, mount point, percent used)`` for / and real data drives."""
    found = []
    for volume in QStorageInfo.mountedVolumes():
        root = volume.rootPath()
        if not (volume.isValid() and volume.isReady()):
            continue
        if root != "/" and not root.startswith(_DRIVE_ROOTS):
            continue
        if bytes(volume.fileSystemType()).decode(errors="ignore") in _SKIP_FS:
            continue
        total = volume.bytesTotal()
        used = round(100 * (total - volume.bytesAvailable()) / total) if total > 0 else 0
        name = "/" if root == "/" else (volume.name() or Path(root).name)
        found.append((name, Path(root), used))
    return sorted(found, key=lambda d: (str(d[1]) != "/", d[0].lower()))


class _Picker(QDialog):
    """The picker window. ``mode`` is "dir", "open" or "save"."""

    def __init__(self, parent, caption: str, start: str, mode: str, filters: str = "", selected: str = "") -> None:
        super().__init__(parent)
        self.mode = mode
        self.caption = caption
        self.filter_text = filters
        self.result_path = ""
        self.show_hidden = False
        self.filters = parse_filters(filters) if mode != "dir" else []
        self.setWindowTitle(caption)
        self.resize(940, 580)

        start_path = Path(start).expanduser() if start else Path.home()
        name = ""
        if not start_path.is_absolute():
            # A bare save default ("commander-diagnostics.txt").
            start_path = Path.home() / start_path
        if not start_path.is_dir():
            # A file to save as or to pick again ("/x/y.zip"): folder + name.
            name = start_path.name
            # A folder that's gone (a moved drive): its nearest parent left.
            start_path = start_path.parent
            while not start_path.is_dir() and start_path.parent != start_path:
                start_path = start_path.parent
        self.current = start_path

        layout = QVBoxLayout(self)
        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self._build_sidebar())
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addLayout(self._build_toolbar())
        self.listing = QListWidget()
        self.listing.setObjectName("fileList")
        self.listing.setIconSize(QSize(18, 18))
        # Double-click and Enter both arrive here; also hooking up
        # itemDoubleClicked opened a folder, then its deleted row, again.
        self.listing.itemActivated.connect(self._activate)
        self.listing.currentItemChanged.connect(self._on_select)
        self.listing.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.listing.customContextMenuRequested.connect(self._context_menu)
        # H like Dolphin (typing H in the filter box still types it), and Ctrl+H.
        self.hidden_action = QAction(tr("Show hidden folders"), self)
        self.hidden_action.setCheckable(True)
        self.hidden_action.setShortcuts([QKeySequence("H"), QKeySequence("Ctrl+H")])
        self.hidden_action.toggled.connect(self._set_hidden)
        self.addAction(self.hidden_action)
        # Up a folder like Dolphin; the filter box keeps Backspace for its text.
        up = QAction(self)
        up.setShortcuts([QKeySequence("Backspace"), QKeySequence("Alt+Up")])
        up.triggered.connect(self._up)
        self.addAction(up)
        right.addWidget(self.listing, 1)
        body.addLayout(right, 1)
        layout.addLayout(body, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        if mode == "save":
            self.name_edit = QLineEdit(name)
            self.name_edit.setPlaceholderText(tr("File name"))
            self.name_edit.setMinimumHeight(34)
            self.name_edit.textChanged.connect(self._sync_ok)
            footer.addWidget(self.name_edit, 1)
        else:
            self.path_label = QLabel()
            self.path_label.setObjectName("dim")
            self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            footer.addWidget(self.path_label, 1)
        self.filter_combo = None
        if len(self.filters) > 1:
            self.filter_combo = QComboBox()
            self.filter_combo.addItems([label for label, _ in self.filters])
            if selected in [label for label, _ in self.filters]:
                self.filter_combo.setCurrentText(selected)
            self.filter_combo.currentIndexChanged.connect(lambda _i: self._render())
            footer.addWidget(self.filter_combo)
        layout.addLayout(footer)

        confirm = {"dir": tr("Use this folder"), "open": tr("Open"), "save": tr("Save")}[mode]
        system = QPushButton(tr("System picker"))
        system.setMinimumSize(110, 34)
        system.setToolTip(tr("Use your desktop's own file picker instead"))
        system.clicked.connect(self._system_picker)
        self.ok = add_buttons(self, layout, confirm)
        self.ok.clicked.disconnect()
        self.ok.clicked.connect(self._confirm)
        layout.itemAt(layout.count() - 1).layout().insertWidget(1, system)
        style_window(self, caption)
        self._render()
        if mode == "open" and name:
            # Picking again (the DLL, a backup): the old file starts selected.
            for row in range(self.listing.count()):
                if self.listing.item(row).text() == name:
                    self.listing.setCurrentRow(row)
        if mode == "save":
            self.name_edit.setFocus()
            self.name_edit.selectAll()
        else:
            # Not the listing: focusing it would pick its first folder.
            self.filter_edit.setFocus()

    # ------------------------------------------------------------- building
    def _build_sidebar(self) -> QListWidget:
        side = QListWidget()
        side.setObjectName("helpTopics")
        side.setFixedWidth(240)
        side.setIconSize(QSize(18, 18))

        def header(text: str) -> None:
            item = QListWidgetItem()
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            label = QLabel(text.upper())
            label.setObjectName("statCaption")
            item.setSizeHint(QSize(0, 30))
            side.addItem(item)
            side.setItemWidget(item, label)

        header(tr("Quick access"))
        places = (
            ("desktop", tr("Desktop"), QStandardPaths.StandardLocation.DesktopLocation),
            ("documents", tr("Documents"), QStandardPaths.StandardLocation.DocumentsLocation),
            ("downloads", tr("Downloads"), QStandardPaths.StandardLocation.DownloadLocation),
            ("home", tr("Home"), QStandardPaths.StandardLocation.HomeLocation),
        )
        for icon, label, location in places:
            path = QStandardPaths.writableLocation(location)
            if path and Path(path).is_dir():
                item = QListWidgetItem(_icon(icon), label)
                item.setData(Qt.ItemDataRole.UserRole, path)
                side.addItem(item)

        header(tr("Drives"))
        for name, root, used in drives():
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, str(root))
            item.setToolTip(str(root))
            row = QWidget()
            row.setObjectName("sideRow")
            row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            box = QHBoxLayout(row)
            box.setContentsMargins(3, 0, 10, 0)
            box.setSpacing(8)
            glyph = QLabel()
            glyph.setPixmap(_icon("drive").pixmap(18, 18))
            box.addWidget(glyph)
            box.addWidget(QLabel(name), 1)
            meter = QProgressBar()
            meter.setObjectName("storageMeterLow" if used >= 90 else "storageMeter")
            meter.setTextVisible(False)
            meter.setRange(0, 100)
            meter.setValue(used)
            meter.setFixedWidth(44)
            box.addWidget(meter)
            item.setSizeHint(QSize(0, 36))
            side.addItem(item)
            side.setItemWidget(item, row)

        side.itemClicked.connect(lambda item: self._go(item.data(Qt.ItemDataRole.UserRole)))
        self.sidebar = side
        return side

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(8)
        for icon, tip, slot in (("up", tr("Up one folder"), self._up), ("refresh", tr("Refresh"), self._render)):
            button = QPushButton()
            button.setObjectName("iconButton")
            button.setIcon(_icon(icon, "text"))
            button.setToolTip(tip)
            button.setFixedSize(34, 34)
            button.clicked.connect(lambda _c=False, s=slot: s())
            bar.addWidget(button)
        self.crumbs = QWidget()
        self.crumb_row = QHBoxLayout(self.crumbs)
        self.crumb_row.setContentsMargins(0, 0, 0, 0)
        self.crumb_row.setSpacing(2)
        self.crumb_scroll = QScrollArea()
        self.crumb_scroll.setObjectName("crumbs")
        self.crumb_scroll.setWidget(self.crumbs)
        self.crumb_scroll.setWidgetResizable(True)
        self.crumb_scroll.setFixedHeight(34)
        self.crumb_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.crumb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.crumb_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bar.addWidget(self.crumb_scroll, 1)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(tr("Filter, or paste a path"))
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setFixedWidth(240)
        self.filter_edit.setMinimumHeight(34)
        self.filter_edit.textChanged.connect(lambda _t: self._render())
        self.filter_edit.returnPressed.connect(self._typed_path)
        bar.addWidget(self.filter_edit)
        return bar

    # ------------------------------------------------------------- listing
    def _render(self) -> None:
        self._render_crumbs()
        typed = self.filter_edit.text().strip()
        needle = "" if typed.startswith(("/", "~")) else typed.lower()
        patterns = self._patterns()
        self.listing.clear()
        try:
            entries = list(os.scandir(self.current))
        except OSError:
            entries = None
        folders, files = [], []
        for entry in entries or ():
            if entry.name.startswith(".") and not (self.show_hidden or needle.startswith(".")):
                continue
            if needle and needle not in entry.name.lower():
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                folders.append(entry)
            elif self.mode != "dir" and any(fnmatch.fnmatch(entry.name.lower(), p.lower()) for p in patterns):
                files.append(entry)
        for entry in sorted(folders, key=lambda e: e.name.lower()):
            item = QListWidgetItem(_icon("folder", "accent"), entry.name)
            item.setData(Qt.ItemDataRole.UserRole, entry.path)
            self.listing.addItem(item)
        for entry in sorted(files, key=lambda e: e.name.lower()):
            item = QListWidgetItem(_icon("file"), entry.name)
            item.setData(Qt.ItemDataRole.UserRole, entry.path)
            self.listing.addItem(item)
        if not self.listing.count():
            if entries is None:
                text = tr("This folder can't be opened")
            elif needle:
                text = tr("Nothing matches the filter")
            else:
                text = tr("This folder is empty")
            empty = QListWidgetItem(text)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.listing.addItem(empty)
        self.listing.setCurrentRow(-1)
        self._mark_sidebar()
        self._on_select(None, None)

    def _render_crumbs(self) -> None:
        while self.crumb_row.count():
            widget = self.crumb_row.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        parts = [self.current, *self.current.parents][::-1]
        for index, part in enumerate(parts):
            if index > 1:
                sep = QLabel("›")
                sep.setObjectName("dim")
                self.crumb_row.addWidget(sep)
            button = QPushButton(part.name or "/")
            button.setObjectName("crumb")
            button.setProperty("current", index == len(parts) - 1)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c=False, p=part: self._go(p))
            self.crumb_row.addWidget(button)
        self.crumb_row.addStretch(1)
        # Keep the deepest folder in view when the path is long.
        # Tied to the scroll area so it's dropped if the window closes first.
        bar = self.crumb_scroll.horizontalScrollBar()
        QTimer.singleShot(0, bar, lambda: bar.setValue(bar.maximum()))

    def _mark_sidebar(self) -> None:
        """Highlight the deepest sidebar entry the current folder is in."""
        best, best_len = None, -1
        for row in range(self.sidebar.count()):
            item = self.sidebar.item(row)
            path = item.data(Qt.ItemDataRole.UserRole)
            if not path:
                continue
            if (self.current == Path(path) or Path(path) in self.current.parents) and len(path) > best_len:
                best, best_len = item, len(path)
        self.sidebar.setCurrentItem(best) if best else self.sidebar.clearSelection()

    def _patterns(self) -> list[str]:
        if not self.filters:
            return ["*"]
        index = self.filter_combo.currentIndex() if self.filter_combo else 0
        return self.filters[index][1]

    # ------------------------------------------------------------- actions
    def _go(self, path) -> None:
        # The sidebar's QUICK ACCESS / DRIVES headings carry no path.
        if not path:
            return
        path = Path(path).expanduser()
        if path.is_dir():
            self.current = path
            self.filter_edit.blockSignals(True)
            self.filter_edit.clear()
            self.filter_edit.blockSignals(False)
            self._render()

    def _set_hidden(self, on: bool) -> None:
        self.show_hidden = on
        self._render()

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction(self.hidden_action)
        menu.exec(self.listing.viewport().mapToGlobal(pos))

    def _up(self) -> None:
        if self.current.parent != self.current:
            came_from = self.current
            self._go(self.current.parent)
            for row in range(self.listing.count()):
                if self.listing.item(row).data(Qt.ItemDataRole.UserRole) == str(came_from):
                    self.listing.setCurrentRow(row)

    def _typed_path(self) -> None:
        typed = Path(self.filter_edit.text().strip()).expanduser()
        if not typed.is_absolute():
            # A filter, not a path: Enter opens the only match.
            if self.listing.count() == 1 and self.listing.item(0).flags() & Qt.ItemFlag.ItemIsEnabled:
                self._activate(self.listing.item(0))
            return
        if typed.is_dir():
            self._go(typed)
        elif self.mode == "open" and typed.is_file():
            self._finish(str(typed))
        elif self.mode == "save" and typed.parent.is_dir():
            self._go(typed.parent)
            self.name_edit.setText(typed.name)

    def _activate(self, item) -> None:
        path = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not path:
            return
        if Path(path).is_dir():
            self._go(path)
        elif self.mode == "open":
            self._finish(path)
        elif self.mode == "save":
            self.name_edit.setText(Path(path).name)
            self._confirm()

    def _on_select(self, item, _previous) -> None:
        path = item.data(Qt.ItemDataRole.UserRole) if item else None
        if self.mode == "dir":
            self.path_label.setText(path or str(self.current))
        elif self.mode == "open":
            self.path_label.setText(path if path and Path(path).is_file() else str(self.current))
        elif path and Path(path).is_file():
            self.name_edit.setText(Path(path).name)
        self._sync_ok()

    def _chosen(self) -> str:
        if self.mode == "save":
            name = self.name_edit.text().strip()
            return str(self.current / name) if name and "/" not in name else ""
        item = self.listing.currentItem()
        path = item.data(Qt.ItemDataRole.UserRole) if item else None
        if self.mode == "dir":
            return path if path and Path(path).is_dir() else str(self.current)
        return path if path and Path(path).is_file() else ""

    def _sync_ok(self, *_args) -> None:
        self.ok.setEnabled(bool(self._chosen()))

    def _confirm(self) -> None:
        path = self._chosen()
        if not path:
            return
        if self.mode == "save" and Path(path).exists():
            answer = MessageBox.question(
                self,
                tr("Replace the file?"),
                tr("{name} already exists in this folder. Replace it?", name=Path(path).name),
                labels={MessageBox.StandardButton.Yes: tr("Replace"), MessageBox.StandardButton.No: tr("Cancel")},
            )
            if answer != MessageBox.StandardButton.Yes:
                return
        self._finish(path)

    def _finish(self, path: str) -> None:
        self.result_path = path
        self.accept()

    def _system_picker(self) -> None:
        start = str(self.current / self.name_edit.text()) if self.mode == "save" else str(self.current)
        if self.mode == "dir":
            path = QFileDialog.getExistingDirectory(self, self.caption, start)
        elif self.mode == "open":
            path, _f = QFileDialog.getOpenFileName(self, self.caption, start, self.filter_text)
        else:
            path, _f = QFileDialog.getSaveFileName(self, self.caption, start, self.filter_text)
        if path:
            self._finish(path)

    def selected_filter(self) -> str:
        if not self.filters:
            return ""
        return self.filters[self.filter_combo.currentIndex() if self.filter_combo else 0][0]


class FileDialog:
    """``QFileDialog``'s static helpers with COMMANDER's look.
    Modules import it as ``QFileDialog``."""

    @staticmethod
    def getExistingDirectory(parent=None, caption="", dir="", *_args, **_kwargs) -> str:
        picker = _Picker(parent, caption, dir, "dir")
        return picker.result_path if picker.exec() == QDialog.DialogCode.Accepted else ""

    @staticmethod
    def getOpenFileName(parent=None, caption="", dir="", filter="", selectedFilter="", *_args, **_kwargs):
        picker = _Picker(parent, caption, dir, "open", filter, selectedFilter)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return "", ""
        return picker.result_path, picker.selected_filter()

    @staticmethod
    def getSaveFileName(parent=None, caption="", dir="", filter="", selectedFilter="", *_args, **_kwargs):
        picker = _Picker(parent, caption, dir, "save", filter, selectedFilter)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return "", ""
        return picker.result_path, picker.selected_filter()
