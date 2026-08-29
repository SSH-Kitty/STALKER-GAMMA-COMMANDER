"""MO2-style mod organizer for GAMMA profiles.

The mod list is shown grouped by the separator categories GAMMA ships in
``modlist.txt``. Safe operations (install, toggle, delete, reorder within a
category) are done directly on the file with automatic backups.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..cli_runner import run_sync
from ..config import logs_dir
from ..fomod import FomodConfig, apply_options, parse_config
from ..launcher import LaunchError, build_command, launch_detached, resolve_runner
from ..mod_install import (
    ModInstallError,
    default_mod_name,
    extract_archive,
    move_payload,
    sanitize_name,
)
from ..modlist import (
    add_category,
    add_mod,
    delete_at,
    entries,
    flip_priority,
    grouped,
    install_conflict,
    move,
    move_mod,
    read_lines,
    rename_mod,
    reorder_to_original,
    reparent_into_category,
    save_lines,
    set_status_at,
)
from ..user_mods import (
    add_user_mod,
    read_user_mods,
    remove_user_mods,
    rename_user_mod,
)
from .common import (
    ACCENT,
    ITEM_GREEN,
    STATUS_GREY,
    BackgroundTask,
    ProgressArea,
    StreamTask,
    info_label,
    make_card,
    mo2_running,
    section_label,
)

BACKUP_SUFFIX = ".gammagui.bak"
#: Short timeout: these are metadata lookups, not installs.
_QUERY_TIMEOUT = 30


class DragTree(QTreeWidget):
    """QTreeWidget subclass with custom drag-and-drop reordering.

    Uses manual mouse-based drag (not ``QDrag::exec()``) so the event
    loop stays open and wheel events continue to work during a drag,
    allowing the user to scroll the list.
    """

    mod_dropped = Signal(str, object, str, bool)
    _SCROLL_MARGIN = 40
    _SCROLL_STEP = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_source_name: str | None = None
        self._drag_active = False
        self._drag_label: QLabel | None = None
        self._press_x = 0
        self._press_y = 0
        self._scroll_direction = 0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(16)
        self._scroll_timer.timeout.connect(self._auto_scroll)
        self._drop_indicator = QFrame(self.viewport())
        self._drop_indicator.setFrameShape(QFrame.Shape.HLine)
        self._drop_indicator.setLineWidth(2)
        self._drop_indicator.setStyleSheet(f"color: {ACCENT.name()};")
        self._drop_indicator.hide()
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropOverwriteMode(False)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setMouseTracking(True)

    def _find_scroll_area(self):
        """Walk up the widget tree to find the parent QScrollArea."""
        w = self.parent()
        while w is not None:
            if isinstance(w, QScrollArea):
                return w
            w = w.parent()
        return None

    def _auto_scroll(self) -> None:
        """Scroll the parent QScrollArea while a drag is in progress."""
        if self._scroll_direction == 0:
            self._scroll_timer.stop()
            return
        area = self._find_scroll_area()
        if area is None:
            self._scroll_timer.stop()
            return
        sb = area.verticalScrollBar()
        new_val = sb.value() + self._scroll_direction * self._SCROLL_STEP
        sb.setValue(new_val)
        if (self._scroll_direction < 0 and new_val <= sb.minimum()) or (
            self._scroll_direction > 0 and new_val >= sb.maximum()
        ):
            self._scroll_timer.stop()

    def startDrag(self, supported_actions) -> None:
        """No-op: drag is handled manually via mouse events."""

    def _reset_drag_state(self) -> None:
        """Clear any in-progress drag."""
        if self._drag_label is not None:
            self._drag_label.deleteLater()
            self._drag_label = None
        self._hide_drop_indicator()
        self._scroll_timer.stop()
        self._scroll_direction = 0
        self._drag_active = False
        self._drag_source_name = None

    def _hide_drop_indicator(self) -> None:
        if self._drop_indicator is not None and not self._drop_indicator.isHidden():
            self._drop_indicator.hide()

    def _update_drop_indicator(self, pos) -> None:
        """Show a bar at the slot the dragged mod will land in, if any."""
        drop_item = self.itemAt(pos.x(), pos.y())
        if drop_item is None:
            self._hide_drop_indicator()
            return
        item_rect = self.visualItemRect(drop_item)
        before = pos.y() < item_rect.center().y()
        self._drop_indicator.setGeometry(
            0,
            item_rect.top() if before else item_rect.bottom() - 1,
            self.viewport().width(),
            2,
        )
        self._drop_indicator.show()
        self._drop_indicator.raise_()

    def mousePressEvent(self, event) -> None:
        """Begin tracking when the user presses on a mod item."""
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None and item.parent() is not None:
                self._drag_source_name = item.text(0)
                self._drag_active = False
                self._press_x = event.position().toPoint().x()
                self._press_y = event.position().toPoint().y()
                return
        self._drag_source_name = None

    def mouseMoveEvent(self, event) -> None:
        """Track the cursor during a manual drag."""
        if self._drag_source_name is not None:
            pos = event.position().toPoint()
            if not self._drag_active:
                dx = abs(pos.x() - self._press_x)
                dy = abs(pos.y() - self._press_y)
                if dx + dy > QApplication.startDragDistance():
                    self._drag_active = True
                    self._drag_label = QLabel(self._drag_source_name, self.viewport())
                    self._drag_label.setStyleSheet(
                        f"background: {ACCENT.name()}; color: white; "
                        "padding: 2px 6px; border-radius: 3px; font-weight: bold;"
                    )
                    self._drag_label.adjustSize()
                    self._drag_label.show()
                    self._drag_label.move(pos.x() + 12, pos.y() + 12)
                else:
                    return
            if self._drag_label is not None:
                self._drag_label.move(pos.x() + 12, pos.y() + 12)
            self._update_drop_indicator(pos)
            y = pos.y()
            if y < self._SCROLL_MARGIN:
                self._scroll_direction = -1
            elif y > self.viewport().height() - self._SCROLL_MARGIN:
                self._scroll_direction = 1
            else:
                self._scroll_direction = 0
            if self._scroll_direction != 0 and not self._scroll_timer.isActive():
                self._scroll_timer.start()
            elif self._scroll_direction == 0 and self._scroll_timer.isActive():
                self._scroll_timer.stop()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """Complete the drag by emitting ``mod_dropped``."""
        if self._drag_active and self._drag_source_name is not None:
            pos = event.position().toPoint()
            if self._drag_label is not None:
                self._drag_label.deleteLater()
                self._drag_label = None
            self._hide_drop_indicator()
            self._scroll_timer.stop()
            self._scroll_direction = 0
            drop_item = self.itemAt(pos.x(), pos.y())
            source = self._drag_source_name
            if drop_item is None:
                if self.topLevelItemCount() == 0:
                    self._drag_active = False
                    self._drag_source_name = None
                    return
                target_header = self.topLevelItem(self.topLevelItemCount() - 1)
                target_name = None
                before = False
            else:
                target_header = (
                    drop_item if drop_item.parent() is None else drop_item.parent()
                )
                target_name = None if drop_item.parent() is None else drop_item.text(0)
                item_rect = self.visualItemRect(drop_item)
                before = pos.y() < item_rect.center().y()
            self._drag_active = False
            self._drag_source_name = None
            self.mod_dropped.emit(source, target_name, target_header.text(0), before)
            return
        self._drag_active = False
        self._drag_source_name = None
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        """Forward wheel events to the appropriate scrollbar."""
        delta = event.angleDelta().y()
        sb = self.verticalScrollBar()
        if sb.maximum() > sb.minimum():
            sb.setValue(sb.value() - delta)
        else:
            area = self._find_scroll_area()
            if area is not None:
                vsb = area.verticalScrollBar()
                vsb.setValue(vsb.value() - delta)
        event.accept()

    def dragMoveEvent(self, event) -> None:
        """Accept drops onto any child item (mod) so cross-category drags work."""
        pos = event.position().toPoint()
        item = self.itemAt(pos.x(), pos.y())
        if item is not None and item.parent() is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        """Unused: drops are handled by ``mouseReleaseEvent``."""
        event.ignore()

    def leaveEvent(self, event) -> None:
        self._reset_drag_state()
        super().leaveEvent(event)

    def focusOutEvent(self, event) -> None:
        self._reset_drag_state()
        super().focusOutEvent(event)


class _FomodDialog(QDialog):
    """Basic FOMOD wizard for XML installers using standard option groups."""

    def __init__(self, config: FomodConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Install {config.name}")
        self.resize(620, 480)
        self._config = config
        self._controls: dict[tuple[int, int], list[QCheckBox | QRadioButton]] = {}
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"{config.name}\n{config.author}\n\nChoose the optional components to install."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        for step_index, step in enumerate(config.steps):
            layout.addWidget(QLabel(f"<b>{step.name}</b>"))
            for group_index, group in enumerate(step.groups):
                box = QGroupBox(f"{group.name} ({group.group_type})")
                group_layout = QVBoxLayout(box)
                controls: list[QCheckBox | QRadioButton] = []
                exclusive = group.group_type in {"SelectExactlyOne", "SelectAtMostOne"}
                for option in group.options:
                    control = (
                        QRadioButton(option.name)
                        if exclusive
                        else QCheckBox(option.name)
                    )
                    control.setToolTip(option.description)
                    group_layout.addWidget(control)
                    controls.append(control)
                self._controls[(step_index, group_index)] = controls
                layout.addWidget(box)
        layout.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        """Enforce the standard FOMOD group cardinality rules."""
        errors: list[str] = []
        for step_index, step in enumerate(self._config.steps):
            for group_index, group in enumerate(step.groups):
                selected = sum(
                    control.isChecked()
                    for control in self._controls[(step_index, group_index)]
                )
                required = group.group_type
                valid = (
                    (required == "SelectExactlyOne" and selected == 1)
                    or (required == "SelectAtMostOne" and selected <= 1)
                    or (required == "SelectAtLeastOne" and selected >= 1)
                    or (required == "SelectAll" and selected == len(group.options))
                    or required in {"SelectAny", "SelectAll"}
                    and required != "SelectAll"
                )
                if not valid:
                    errors.append(
                        f"{group.name}: choose the required number of options "
                        f"({group.group_type})."
                    )
        if errors:
            QMessageBox.warning(
                self,
                "FOMOD selections incomplete",
                "Please review these groups:\n\n" + "\n".join(errors),
            )
            return
        self.accept()

    def selections(self) -> dict[tuple[int, int], list[int]]:
        return {
            key: [
                index for index, control in enumerate(controls) if control.isChecked()
            ]
            for key, controls in self._controls.items()
        }


def _query_mo2_profiles() -> tuple[list[str], str]:
    """Return (profile names, selected profile). Runs on a worker thread."""
    rc, out = run_sync(["mo2", "profiles", "list"], timeout=_QUERY_TIMEOUT)
    names = (
        [line.strip() for line in out.splitlines() if line.strip()] if rc == 0 else []
    )
    rc, out = run_sync(
        ["mo2", "config", "get", "selected-profile"], timeout=_QUERY_TIMEOUT
    )
    return names, out.strip() if rc == 0 else ""


class ModManagerPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._lines: list[str] = []
        self._populating = False
        self._reorder_warned = False
        self._profiles_loading = False
        self._profiles_generation = 0
        self._profiles_task = None
        self._install_task: StreamTask | None = None
        self._finalize_task: BackgroundTask | None = None
        self._install_staging: Path | None = None
        self._install_source: Path | None = None
        self._install_name = ""
        self._install_active = False
        self._install_generation = 0
        self._pending_refresh = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(content)

        title = section_label("MOD MANAGER", level=1)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(title)
        subtitle = info_label(
            "Choose an MO2 profile and safely manage its GAMMA modlist."
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(subtitle)

        card, layout = make_card()
        root.addWidget(card, 1)
        layout.addWidget(section_label("GAMMA modlist"))

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.install_button = QPushButton("Install Mod")
        self.install_button.setObjectName("primary")
        self.install_button.setToolTip(
            "Install a local ZIP, 7Z, RAR, or FOMOD archive into the GAMMA mods folder."
        )
        self.install_button.clicked.connect(self._install_mod)
        action_row.addWidget(self.install_button)

        self.create_backup_button = QPushButton("Create Backup")
        self.create_backup_button.setToolTip(
            "Save a backup of the current MO2 modlist."
        )
        self.create_backup_button.clicked.connect(self._create_backup)
        action_row.addWidget(self.create_backup_button)

        self.restore_button = QPushButton("Restore Backup")
        self.restore_button.clicked.connect(self._restore_backup)
        action_row.addWidget(self.restore_button)

        self.restore_original_button = QPushButton("Restore Original Order")
        self.restore_original_button.setToolTip(
            "Restore the modlist saved automatically before the first Commander edit."
        )
        self.restore_original_button.clicked.connect(self._restore_original_order)
        action_row.addWidget(self.restore_original_button)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        action_row.addWidget(self.refresh_button)
        self.new_category_button = QPushButton("New Category")
        self.new_category_button.setToolTip(
            "Add an MO2 separator category to this modlist."
        )
        self.new_category_button.clicked.connect(self._create_category)
        action_row.addWidget(self.new_category_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("MO2 profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._load_mods)
        top_row.addWidget(self.profile_combo, 1)
        layout.addLayout(top_row)

        sel_row = QHBoxLayout()
        self.selected_label = QLabel("MO2 selected profile: -")
        self.selected_label.setObjectName("dim")
        sel_row.addWidget(self.selected_label)
        self.set_selected_button = QPushButton("Use as MO2 selected profile")
        self.set_selected_button.clicked.connect(self._set_selected)
        sel_row.addWidget(self.set_selected_button)
        self.open_mo2_button = QPushButton("Open MO2")
        self.open_mo2_button.clicked.connect(self._open_mo2)
        sel_row.addWidget(self.open_mo2_button)
        sel_row.addStretch(1)
        layout.addLayout(sel_row)

        self.guard_label = QLabel(
            "MO2 is running. Close it before editing the modlist; edits are disabled while it is open."
        )
        self.guard_label.setObjectName("warn")
        self.guard_label.hide()
        layout.addWidget(self.guard_label)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search the modlist...")
        self.search.textChanged.connect(self._apply_filter)
        search_row = QHBoxLayout()
        search_row.addWidget(self.search, 1)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: #8fe45c")
        search_row.addWidget(self.count_label)
        layout.addLayout(search_row)

        self.tree = DragTree(self)
        self.tree.setColumnCount(1)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(18)
        self.tree.setMinimumHeight(320)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemSelectionChanged.connect(self._update_count)
        self.tree.mod_dropped.connect(self._on_tree_drop)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.tree, 1)

        btn_row = QHBoxLayout()
        selected_label = QLabel("Selected mods")
        selected_label.setObjectName("dim")
        btn_row.addWidget(selected_label)
        self.enable_button = QPushButton("Enable")
        self.enable_button.clicked.connect(lambda: self._set_selected_mods(True))
        self.disable_button = QPushButton("Disable")
        self.disable_button.clicked.connect(lambda: self._set_selected_mods(False))
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete_selected_mods)
        self.move_up_button = QPushButton("Move Up")
        self.move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self.move_down_button = QPushButton("Move Down")
        self.move_down_button.clicked.connect(lambda: self._move_selected(1))
        self.flip_priority_button = QPushButton("Flip Priority")
        self.flip_priority_button.setToolTip(
            "Reverse the mod order: mods at the top go to the bottom and vice versa.\n"
            "Keeps comments and separators in place."
        )
        self.flip_priority_button.clicked.connect(self._on_flip_priority)
        for b in (
            self.enable_button,
            self.disable_button,
            self.delete_button,
            self.move_up_button,
            self.move_down_button,
            self.flip_priority_button,
        ):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.install_progress = ProgressArea(show_table=False, show_log=True)
        self.install_progress.cancel_button.clicked.connect(self._cancel_install)
        self.install_progress.hide()
        layout.addWidget(self.install_progress)

        backup_row = QHBoxLayout()
        self.backup_status = QLabel("")
        self.backup_status.setObjectName("dim")
        backup_row.addWidget(self.backup_status, 1)
        layout.addLayout(backup_row)

    # ----- data access -----
    def _active_profile(self):
        profile = self.window.settings.active_profile
        if profile is None:
            raise RuntimeError("No active profile")
        return profile

    def _modlist_path(self, mo2_profile: str) -> Path:
        if (
            not mo2_profile
            or mo2_profile in {".", ".."}
            or "/" in mo2_profile
            or "\\" in mo2_profile
        ):
            raise RuntimeError("Invalid MO2 profile name")
        gamma = self._active_profile().gamma
        profiles_root = (Path(gamma) / "profiles").resolve()
        path = profiles_root / mo2_profile / "modlist.txt"
        try:
            path.parent.resolve().relative_to(profiles_root)
        except ValueError as exc:
            raise RuntimeError("MO2 profile path escapes the profiles directory") from exc
        return path

    def _backup_path(self, modlist: Path) -> Path:
        return modlist.with_name(modlist.name + BACKUP_SUFFIX)

    def _timestamped_backup_path(self, modlist: Path) -> Path:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        candidate = modlist.with_name(f"{modlist.stem}-{stamp}.bak")
        suffix = 2
        while candidate.exists():
            candidate = modlist.with_name(f"{modlist.stem}-{stamp}-{suffix}.bak")
            suffix += 1
        return candidate

    _MAX_BACKUPS = 20

    def _prune_backups(self, modlist: Path) -> None:
        """Keep only this modlist's newest timestamped backups."""
        baks = sorted(
            (
                p
                for p in modlist.parent.glob(f"{modlist.stem}-*.bak")
                if p.is_file()
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in baks[self._MAX_BACKUPS :]:
            try:
                old.unlink()
            except OSError:
                pass

    # ----- MO2 running guard -----
    def _mo2_running(self) -> bool:
        return mo2_running()

    def _update_guard(self) -> None:
        running = self._mo2_running()
        blocked = running or self._install_active or self.window.install_busy
        self.guard_label.setVisible(running)
        for widget in (
            self.tree,
            self.profile_combo,
            self.enable_button,
            self.disable_button,
            self.delete_button,
            self.move_up_button,
            self.move_down_button,
            self.restore_button,
            self.restore_original_button,
            self.create_backup_button,
            self.install_button,
            self.new_category_button,
            self.flip_priority_button,
            self.set_selected_button,
        ):
            widget.setEnabled(not blocked)
        if self._install_active:
            self.install_button.setEnabled(False)
            self.profile_combo.setEnabled(False)
        if running:
            self._update_count()
        original_backup = False
        profile = self.profile_combo.currentText()
        if profile:
            try:
                original_backup = self._backup_path(self._modlist_path(profile)).is_file()
            except (OSError, RuntimeError):
                pass
        self.restore_original_button.setEnabled(not blocked and original_backup)

    def on_busy_changed(self, _busy: bool) -> None:
        """Keep modlist mutations locked during global install operations."""
        self._update_guard()

    # ----- load -----
    def refresh(self) -> None:
        self._profiles_generation += 1
        self.window.refresh_settings()
        if self._profiles_loading:
            self._pending_refresh = True
            return
        self._load_profiles(self._profiles_generation)
        self._update_guard()

    def _load_profiles(self, generation: int) -> None:
        """Query MO2 profiles off the GUI thread.

        Both queries shell out to the CLI; running them inline froze the window
        on every visit to this page.
        """
        if self._profiles_loading:
            return
        self._profiles_loading = True
        self.count_label.setText("Loading MO2 profiles...")
        task = BackgroundTask(_query_mo2_profiles, parent=self)
        task.result.connect(
            lambda result, task=task, generation=generation: self._on_profiles_loaded(
                result, task, generation
            )
        )
        task.error.connect(
            lambda message, task=task, generation=generation: self._on_profiles_error(
                message, task, generation
            )
        )
        self._profiles_task = task
        task.start()

    def _on_profiles_loaded(
        self, result: tuple[list[str], str], task: BackgroundTask, generation: int
    ) -> None:
        if self._profiles_task is not task:
            return
        self._profiles_loading = False
        self._profiles_task = None
        if generation != self._profiles_generation:
            return
        names, selected = result
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        if names:
            self.profile_combo.addItems(names)
            active = self.window.settings.active_profile
            if active is not None and active.mo2_profile in names:
                self.profile_combo.setCurrentText(active.mo2_profile)
        self.profile_combo.blockSignals(False)
        self.selected_label.setText(f"MO2 selected profile: {selected or '-'}")
        if not names:
            self.count_label.setText(
                "No MO2 profiles found. Complete a GAMMA installation first."
            )
            self.tree.clear()
            return
        self._load_mods()
        if self._pending_refresh:
            self._pending_refresh = False
            self.refresh()

    def _on_profiles_error(
        self, message: str, task: BackgroundTask, generation: int
    ) -> None:
        if self._profiles_task is not task:
            return
        self._profiles_loading = False
        self._profiles_task = None
        if generation != self._profiles_generation:
            return
        self.selected_label.setText("MO2 selected profile: -")
        self.count_label.setText(f"Could not list MO2 profiles: {message}")
        self.tree.clear()
        if self._pending_refresh:
            self._pending_refresh = False
            self.refresh()

    def _load_mods(self) -> None:
        mo2_profile = self.profile_combo.currentText()
        if not mo2_profile:
            return
        try:
            path = self._modlist_path(mo2_profile)
            self._lines = read_lines(path)
        except Exception as exc:  # noqa: BLE001
            self._lines = []
            self.count_label.setText(f"Could not read modlist: {exc}")
            self.tree.clear()
            return
        if self._restore_extra_mods_placement(path):
            self.backup_status.setText(
                "Moved Commander-installed mod(s) back into Extra Mods"
            )
        else:
            self.backup_status.setText(self._backup_status_text(path))
        self._populate_tree()
        self._update_count()

    def _restore_extra_mods_placement(self, path: Path) -> bool:
        """Regroup Commander-installed mods under the Extra Mods category.

        GAMMA/MO2 can rewrite modlist.txt outside the app, dropping the Extra
        Mods separator and stranding previously installed mods in whichever
        category the rewrite left them in.  Re-parent those entries back into
        a single Extra Mods category and prune tracker names that are no
        longer in the list.  This only writes while MO2 is not running.

        Returns ``True`` when a repair write was performed.
        """
        tracked = read_user_mods(path)
        if not tracked:
            return False
        try:
            healed = reparent_into_category(self._lines, tracked)
        except ValueError:
            healed = None
        healed_write = (
            healed is not None
            and not self._mo2_running()
            and self._write_lines(healed, quiet=True)
        )
        present = {name for _, name in entries(self._lines)}
        stale = [name for name in tracked if name not in present]
        if stale:
            remove_user_mods(path, stale)
        return healed_write

    def _backup_status_text(self, modlist: Path) -> str:
        bak = self._backup_path(modlist)
        if bak.is_file():
            # Shown to the user, so render in their local timezone explicitly.
            stamp = (
                datetime.fromtimestamp(bak.stat().st_mtime, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
            )
            return f"Backup: {bak.name} ({stamp})"
        return "No backup yet"

    def _create_backup(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            QMessageBox.warning(
                self,
                "Mod Organizer is running",
                "Close Mod Organizer before creating a backup.",
            )
            return
        profile = self.profile_combo.currentText()
        if not profile:
            return
        try:
            modlist = self._modlist_path(profile)
            if not modlist.is_file():
                raise FileNotFoundError(f"Modlist not found: {modlist}")
            default_path = self._timestamped_backup_path(modlist)
            backup_name, _ = QFileDialog.getSaveFileName(
                self,
                "Save modlist backup",
                str(default_path),
                "Modlist backups (*.bak *.txt);;All files (*)",
            )
            if not backup_name:
                return
            backup = Path(backup_name).expanduser()
            if backup.exists():
                QMessageBox.warning(
                    self,
                    "Backup Already Exists",
                    "Choose a new timestamped filename so existing backups are not overwritten.",
                )
                return
            if self.window.install_busy:
                self._update_guard()
                return
            backup.parent.mkdir(parents=True, exist_ok=True)
            temporary = backup.with_name(f".{backup.name}.tmp")
            shutil.copy2(modlist, temporary)
            temporary.replace(backup)
            self.backup_status.setText(f"Backup saved: {backup}")
            self._update_guard()
        except OSError as exc:
            QMessageBox.warning(self, "Backup Failed", str(exc))

    def _populate_tree(self) -> None:
        self._populating = True
        self.tree.blockSignals(True)
        self.tree.clear()
        priority = 0
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Mod", "Priority"])
        self.tree.header().setVisible(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        for category, mods in grouped(self._lines):
            header = QTreeWidgetItem([category])
            header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            header.setForeground(0, QColor(ACCENT.name()))
            font = header.font(0)
            font.setBold(True)
            header.setFont(0, font)
            self.tree.addTopLevelItem(header)
            for status, name, line_index in mods:
                priority += 1
                item = QTreeWidgetItem([name])
                item.setText(1, str(priority))
                item.setToolTip(1, f"MO2 priority {priority}")
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsDragEnabled
                )
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if status == "Enabled"
                    else Qt.CheckState.Unchecked,
                )
                item.setData(0, Qt.ItemDataRole.UserRole, line_index)
                item.setForeground(
                    0,
                    QColor(STATUS_GREY.name())
                    if status != "Enabled"
                    else QColor(ITEM_GREEN.name()),
                )
                header.addChild(item)
        self.tree.expandAll()
        self.tree.blockSignals(False)
        self._populating = False
        self._apply_filter()

    # ----- search filter -----
    def _apply_filter(self) -> None:
        needle = self.search.text().strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            matches = 0
            for j in range(header.childCount()):
                item = header.child(j)
                hit = not needle or needle in item.text(0).lower()
                item.setHidden(not hit)
                matches += int(hit)
            header.setHidden(matches == 0 and bool(needle))
        self._update_count()

    def _update_count(self) -> None:
        total = enabled = visible = 0
        visible_enabled = 0
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                item = header.child(j)
                total += 1
                if item.checkState(0) == Qt.CheckState.Checked:
                    enabled += 1
                if not item.isHidden():
                    visible += 1
                    visible_enabled += int(item.checkState(0) == Qt.CheckState.Checked)
        if self.search.text().strip():
            self.count_label.setText(
                f"{visible} matching of {total} mods ({visible_enabled} enabled)"
            )
        else:
            self.count_label.setText(f"{total} mods ({enabled} enabled)")

    # ----- writes -----
    def _write_lines(
        self,
        new_lines: list[str],
        *,
        internal: bool = False,
        quiet: bool = False,
        snapshot: bool = True,
    ) -> bool:
        guard_failed = (
            (self.window.install_busy and not internal)
            or (self._install_active and not internal)
            or self._mo2_running()
        )
        if guard_failed:
            self._update_guard()
            if not quiet:
                QMessageBox.warning(
                    self,
                    "Mod Organizer is running",
                    "Close Mod Organizer first - it would overwrite your changes "
                    "when it exits.",
                )
            return False
        mo2_profile = self.profile_combo.currentText()
        try:
            path = self._modlist_path(mo2_profile)
            if path.exists():
                bak = self._backup_path(path)
                if not bak.exists():
                    shutil.copy2(path, bak)
                if snapshot:
                    shutil.copy2(path, self._timestamped_backup_path(path))
            save_lines(path, new_lines)
            self._lines = new_lines
            self._backup_status_text(path)
            self._prune_backups(path)
            self._update_guard()
            return True
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                QMessageBox.warning(self, "Failed", str(exc))
            return False

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._populating or item.parent() is None:
            return
        if self.window.install_busy:
            self._update_guard()
            return
        line_index = item.data(0, Qt.ItemDataRole.UserRole)
        if line_index is None or line_index >= len(self._lines):
            return
        enabled = item.checkState(0) == Qt.CheckState.Checked
        new_lines = set_status_at(self._lines, line_index, enabled)
        if self._write_lines(new_lines, snapshot=False):
            self._populating = True
            item.setForeground(
                0,
                QColor(ITEM_GREEN.name()) if enabled else QColor(STATUS_GREY.name()),
            )
            self._populating = False
            self._update_count()
            return
        # The write failed, so put the checkbox back rather than showing a
        # state the file on disk does not have.
        self._populating = True
        self.tree.blockSignals(True)
        item.setCheckState(
            0, Qt.CheckState.Unchecked if enabled else Qt.CheckState.Checked
        )
        self.tree.blockSignals(False)
        self._populating = False

    # ----- actions -----
    def _install_mod(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            QMessageBox.warning(
                self,
                "Mod Organizer is running",
                "Close Mod Organizer before installing a mod.",
            )
            return
        archive_name, _ = QFileDialog.getOpenFileName(
            self,
            "Install mod archive",
            str(Path.home()),
            "Mod archives (*.zip *.7z *.rar *.fomod);;All files (*)",
        )
        if not archive_name:
            return
        archive = Path(archive_name)
        try:
            default_name = default_mod_name(archive)
        except ModInstallError as exc:
            QMessageBox.warning(self, "Invalid mod archive", str(exc))
            return
        # Use a custom dialog for better text wrapping
        dialog = QDialog(self)
        dialog.setWindowTitle("Name installed mod")
        dialog.resize(520, 130)
        layout = QVBoxLayout(dialog)
        label = QLabel("MO2 mod name:")
        label.setWordWrap(True)
        layout.addWidget(label)
        text_field = QLineEdit(default_name)
        text_field.setMinimumWidth(300)
        text_field.selectAll()
        layout.addWidget(text_field)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = text_field.text()
        if not name.strip():
            return
        try:
            safe_name = sanitize_name(name)
        except ModInstallError as exc:
            QMessageBox.warning(self, "Invalid mod name", str(exc))
            return
        if not self._resolve_install_target(safe_name):
            return
        self._start_mod_install(archive, safe_name)

    def _resolve_install_target(self, name: str) -> bool:
        """Check the install destination, replacing a leftover folder.

        Returns ``True`` to proceed.  A folder already in use by a listed mod
        blocks the install (enable it instead); a leftover folder from a
        deleted list entry is removed after confirmation.
        """
        try:
            profile = self._active_profile()
        except RuntimeError as exc:
            QMessageBox.warning(self, "Error", str(exc))
            return False
        mods_dir = Path(profile.gamma) / "mods"
        if mods_dir.is_symlink():
            QMessageBox.warning(
                self,
                "Cannot install",
                "The GAMMA mods directory cannot be a symlink.",
            )
            return False
        conflict = install_conflict(self._lines, mods_dir, name)
        if conflict is None:
            return True
        if conflict == "listed":
            QMessageBox.warning(
                self,
                "Mod already installed",
                f"'{name}' is already in the modlist. Enable it in the list, "
                "or delete it from the list first if you want to reinstall it.",
            )
            return False
        answer = QMessageBox.question(
            self,
            "Replace existing folder",
            f"A leftover folder '{name}' exists in the mods folder but it is not "
            "in your modlist.\n\nReplace it with the new install? The old folder "
            "will be permanently deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            shutil.rmtree(mods_dir / name)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot replace folder", str(exc))
            return False
        return True

    def _start_mod_install(self, archive: Path, name: str) -> None:
        if self.window.install_busy or self._install_active or self._mo2_running():
            self._update_guard()
            return
        self._install_generation += 1
        generation = self._install_generation
        self._install_source = archive
        self._install_name = name
        self._install_staging = None
        self._install_active = True
        self.window.set_install_busy(True, "mod_install")
        self._update_guard()
        self.install_progress.reset()
        self.install_progress.show()
        self.install_progress.on_started()
        self.install_progress.pause_button.hide()

        cancel_event = None

        def worker(report):
            staging_parent = Path(tempfile.mkdtemp(prefix="gamma-mod-install-"))
            staging = staging_parent / "archive"
            try:
                extract_archive(
                    archive,
                    staging,
                    cancel_event,
                    lambda percent, text: report(
                        f"Extracting {archive.name}... {percent}%"
                        if percent is not None
                        else text
                    ),
                )
                return str(staging)
            except Exception:
                shutil.rmtree(staging_parent, ignore_errors=True)
                raise

        self._install_task = StreamTask(worker, parent=self)
        cancel_event = self._install_task.cancel_event
        self._install_task.line.connect(self.install_progress.status_message)
        self._install_task.result.connect(
            lambda staging_name, generation=generation: self._on_archive_extracted(
                staging_name, generation
            )
        )
        self._install_task.error.connect(
            lambda message, generation=generation: self._on_install_error(
                message, generation
            )
        )
        self._install_task.start()

    def _on_archive_extracted(
        self, staging_name: str, generation: int | None = None
    ) -> None:
        if generation is not None and (
            not self._install_active or generation != self._install_generation
        ):
            return
        staging = Path(staging_name)
        self._install_staging = staging
        config_path = staging / "fomod" / "ModuleConfig.xml"
        fomod_root = staging
        if not config_path.is_file():
            candidates = list(staging.glob("*/fomod/ModuleConfig.xml"))
            if len(candidates) == 1:
                config_path = candidates[0]
                fomod_root = candidates[0].parent.parent
        if config_path.is_file():
            try:
                config = parse_config(config_path)
            except ModInstallError as exc:
                self._on_install_error(str(exc), generation)
                return
            dialog = _FomodDialog(config, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self._on_install_error("FOMOD installation cancelled", generation)
                return
            selected = staging.parent / "selected"
            try:
                apply_options(config, fomod_root, selected, dialog.selections())
            except ModInstallError as exc:
                self._on_install_error(str(exc), generation)
                return
            source = selected
        else:
            source = staging
        self._finalize_mod(source, generation)

    def _finalize_mod(self, source: Path, generation: int) -> None:
        try:
            profile = self._active_profile()
        except RuntimeError as exc:
            self._on_install_error(str(exc), generation)
            return
        mods_dir = Path(profile.gamma) / "mods"
        if mods_dir.is_symlink():
            self._on_install_error("The GAMMA mods directory cannot be a symlink.", generation)
            return
        name = self._install_name
        self.install_progress.status_message(f"Installing {name}...")

        def worker():
            destination = mods_dir / name
            move_payload(source, destination, self._finalize_task.cancel_event)
            return destination

        task = BackgroundTask(worker, parent=self)
        task.result.connect(
            lambda destination, generation=generation: self._on_mod_moved(
                destination, generation
            )
        )
        task.error.connect(
            lambda message, generation=generation: self._on_install_error(
                message, generation
            )
        )
        self._finalize_task = task
        task.start()

    def _on_mod_moved(
        self, destination: Path, generation: int | None = None
    ) -> None:
        if generation is not None and (
            not self._install_active or generation != self._install_generation
        ):
            return
        try:
            new_lines = add_mod(self._lines, destination.name, enabled=False)
        except ValueError as exc:
            # Mod already exists in modlist.txt (e.g., disabled mod whose folder was deleted).
            try:
                shutil.rmtree(destination)
            except OSError:
                pass
            QMessageBox.warning(self, "Mod installation failed", str(exc))
            self._finish_install()
            return
        if self._write_lines(new_lines, internal=True):
            try:
                add_user_mod(self._modlist_path(self.profile_combo.currentText()), destination.name)
            except OSError:
                pass
            self._finish_install()
            return
        try:
            shutil.rmtree(destination)
        except OSError:
            pass
        QMessageBox.warning(
            self,
            "Mod installed but not listed",
            f"The files were installed to {destination}, but modlist.txt could not be updated.",
        )
        self._finish_install()

    def _on_install_error(self, message: str, generation: int) -> None:
        if not self._install_active or generation != self._install_generation:
            return
        if self._install_task is not None:
            self._install_task.cancel()
        if self._finalize_task is not None:
            self._finalize_task.cancel()
        QMessageBox.warning(self, "Mod installation failed", message)
        self._finish_install()

    def _cancel_install(self) -> None:
        """Cancel only the currently active archive-install tasks."""
        if self._install_task is not None:
            self._install_task.cancel()
        if self._finalize_task is not None:
            self._finalize_task.cancel()

    def _finish_install(self) -> None:
        if self._install_staging is not None:
            shutil.rmtree(self._install_staging.parent, ignore_errors=True)
        self._install_staging = None
        self._install_task = None
        self._finalize_task = None
        self._install_active = False
        self.install_progress.reset()
        self.install_progress.hide()
        if self.window.install_operation == "mod_install":
            self.window.set_install_busy(False)
        self._update_guard()
        self._load_mods()

    def _selected_mod_indexes(self) -> list[int]:
        indexes: list[int] = []
        for item in self.tree.selectedItems():
            if item.parent() is not None:
                indexes.append(item.data(0, Qt.ItemDataRole.UserRole))
        return indexes

    def _selected_mod_names(self) -> list[str]:
        return [
            item.text(0)
            for item in self.tree.selectedItems()
            if item.parent() is not None
        ]

    def _select_mod_names(self, names: list[str]) -> None:
        wanted = set(names)
        self.tree.clearSelection()
        for index in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(index)
            for child_index in range(header.childCount()):
                item = header.child(child_index)
                if item.text(0) in wanted:
                    item.setSelected(True)
                    self.tree.scrollToItem(item)
                    wanted.discard(item.text(0))
        self._update_count()

    def _set_selected_mods(self, enabled: bool) -> None:
        if self.window.install_busy:
            self._update_guard()
            return
        indexes = self._selected_mod_indexes()
        if not indexes:
            return
        new_lines = list(self._lines)
        for idx in indexes:
            new_lines = set_status_at(new_lines, idx, enabled)
        if self._write_lines(new_lines, snapshot=False):
            self._load_mods()

    def _delete_selected_mods(self) -> None:
        if self.window.install_busy:
            self._update_guard()
            return
        indexes = self._selected_mod_indexes()
        if not indexes:
            return
        names = self._selected_mod_names()

        box = QMessageBox(self)
        box.setWindowTitle("Delete Mods")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"Remove {len(indexes)} mod(s) from the modlist?\n\n"
            "This only edits the modlist; mod files are not deleted."
        )
        delete_files = QCheckBox(
            "Also delete the mod folder(s) on disk - cannot be undone"
        )
        box.setCheckBox(delete_files)
        confirm = box.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() != confirm:
            return

        new_lines = list(self._lines)
        for idx in sorted(indexes, reverse=True):
            new_lines = delete_at(new_lines, idx)
        if not self._write_lines(new_lines):
            return
        try:
            remove_user_mods(
                self._modlist_path(self.profile_combo.currentText()), names
            )
        except OSError:
            pass
        if delete_files.isChecked():
            self._delete_mod_folders(names)
        self._load_mods()

    def _delete_mod_folders(self, names: list[str]) -> None:
        """Permanently remove the mod folders for *names* from the mods dir."""
        try:
            profile = self._active_profile()
        except RuntimeError as exc:
            QMessageBox.warning(self, "Error", str(exc))
            return
        mods_dir = Path(profile.gamma) / "mods"
        if mods_dir.is_symlink():
            QMessageBox.warning(
                self,
                "Cannot delete folders",
                "The GAMMA mods directory cannot be a symlink.",
            )
            return
        failures: list[str] = []
        for name in names:
            folder = mods_dir / name
            try:
                if folder.is_symlink():
                    failures.append(f"{name} (symlink, skipped)")
                    continue
                if not folder.exists():
                    continue
                shutil.rmtree(folder)
            except OSError as exc:
                failures.append(f"{name} ({exc})")
        if failures:
            QMessageBox.warning(
                self,
                "Some folders were not deleted",
                "\n".join(failures),
            )

    def _move_selected(self, delta: int) -> None:
        if self.window.install_busy:
            self._update_guard()
            return
        indexes = self._selected_mod_indexes()
        if len(indexes) != 1:
            return
        selected_name = self._selected_mod_names()[0]
        if not self._reorder_warned:
            answer = QMessageBox.question(
                self,
                "Reorder Mods",
                "Moving a mod changes the load order. An incorrect load order "
                "can break your save or the game.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._reorder_warned = True
        new_lines = move(self._lines, indexes[0], delta)
        if new_lines == self._lines:
            return
        if self._write_lines(new_lines):
            self._load_mods()
            self._select_mod_names([selected_name])

    # ----- context menu -----
    def _show_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None or item.parent() is None:
            return
        # Operate on the mod that was right-clicked.
        self.tree.clearSelection()
        self.tree.setCurrentItem(item)
        item.setSelected(True)
        self._update_count()

        blocked = self._mo2_running() or self.window.install_busy or self._install_active
        indexes = self._selected_mod_indexes()
        single = len(indexes) == 1

        menu = QMenu(self)
        rename_action = menu.addAction("Rename...")
        rename_action.setEnabled(single and not blocked)
        menu.addSeparator()
        enable_action = menu.addAction("Enable")
        disable_action = menu.addAction("Disable")
        menu.addSeparator()
        move_up_action = menu.addAction("Move Up")
        move_down_action = menu.addAction("Move Down")
        for action in (enable_action, disable_action, move_up_action, move_down_action):
            action.setEnabled(not blocked)
        menu.addSeparator()

        category_actions: dict = {}
        category_menu = menu.addMenu("Move to Category")
        categories = [name for name, _ in grouped(self._lines)]
        current = item.parent().text(0)
        for cat in categories:
            if cat == current:
                continue
            action = category_menu.addAction(cat)
            category_actions[action] = cat
        if not category_actions:
            category_menu.setEnabled(False)

        folder_action = menu.addAction("Open Mod Folder")
        folder_action.setEnabled(not blocked)
        menu.addSeparator()
        delete_action = menu.addAction("Delete")
        delete_action.setEnabled(not blocked)

        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == rename_action:
            self._rename_selected_mod()
        elif chosen == enable_action:
            self._set_selected_mods(True)
        elif chosen == disable_action:
            self._set_selected_mods(False)
        elif chosen == move_up_action:
            self._move_selected(-1)
        elif chosen == move_down_action:
            self._move_selected(1)
        elif chosen in category_actions:
            self._move_selected_to_category(category_actions[chosen])
        elif chosen == folder_action:
            names = self._selected_mod_names()
            if names:
                self._open_mod_folder_by_name(names[0])
        elif chosen == delete_action:
            self._delete_selected_mods()

    def _rename_selected_mod(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        indexes = self._selected_mod_indexes()
        if len(indexes) != 1:
            return
        old_name = self._selected_mod_names()[0]
        new_name, accepted = QInputDialog.getText(
            self,
            "Rename Mod",
            "New display name in the modlist:",
            text=old_name,
        )
        if not accepted:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        try:
            new_lines = rename_mod(self._lines, old_name, new_name)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Name", str(exc))
            return
        if new_lines == self._lines:
            return
        if self._write_lines(new_lines):
            try:
                rename_user_mod(
                    self._modlist_path(self.profile_combo.currentText()),
                    old_name,
                    new_name,
                )
            except OSError:
                pass
            self._load_mods()
            self._select_mod_names([new_name])

    def _move_selected_to_category(self, category: str) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        names = self._selected_mod_names()
        if len(names) != 1:
            return
        if not self._reorder_warned:
            answer = QMessageBox.question(
                self,
                "Move Mod",
                "Moving a mod changes the load order. An incorrect load order "
                "can break your save or the game.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._reorder_warned = True
        new_lines = move_mod(self._lines, names[0], category=category)
        if new_lines == self._lines:
            return
        if self._write_lines(new_lines):
            self._load_mods()
            self._select_mod_names([names[0]])

    def _open_mod_folder_by_name(self, name: str) -> None:
        try:
            profile = self._active_profile()
        except RuntimeError as exc:
            QMessageBox.warning(self, "Error", str(exc))
            return
        mods_dir = Path(profile.gamma) / "mods"
        if mods_dir.is_symlink():
            QMessageBox.warning(
                self,
                "Cannot open folder",
                "The GAMMA mods directory cannot be a symlink.",
            )
            return
        folder = mods_dir / name
        if not folder.is_dir():
            QMessageBox.information(
                self,
                "No Mod Folder",
                f"No folder '{name}' exists in {mods_dir}.\n\n"
                "Renaming only changes the modlist display name; it does not "
                "rename the folder on disk.",
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(self, "Cannot open folder", f"Could not open:\n{folder}")

    def _set_selected(self) -> None:
        if self.window.install_busy:
            self._update_guard()
            return
        profile = self.profile_combo.currentText()
        if not profile:
            return
        self.set_selected_button.setEnabled(False)
        task = BackgroundTask(
            run_sync,
            ["mo2", "config", "set", "selected-profile", profile],
            timeout=_QUERY_TIMEOUT,
            parent=self,
        )
        task.result.connect(lambda res: self._on_set_selected_done(profile, *res))
        task.error.connect(lambda msg: self._on_set_selected_error(msg))
        task.start()

    def _on_set_selected_done(self, profile: str, rc: int, out: str) -> None:
        self.set_selected_button.setEnabled(True)
        if rc == 0:
            self.selected_label.setText(f"Selected profile: {profile}")
        else:
            QMessageBox.warning(
                self, "Failed", out.strip() or "Could not set selected profile"
            )

    def _on_set_selected_error(self, msg: str) -> None:
        self.set_selected_button.setEnabled(True)
        QMessageBox.warning(self, "Error", msg)

    def _create_category(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        name, accepted = QInputDialog.getText(self, "New Category", "Category name:")
        if not accepted:
            return
        try:
            new_lines = add_category(self._lines, sanitize_name(name))
        except (ModInstallError, ValueError) as exc:
            QMessageBox.warning(self, "Invalid Category", str(exc))
            return
        if self._write_lines(new_lines):
            self._load_mods()

    def _on_flip_priority(self) -> None:
        """Reverse the mod order in the current modlist."""
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        mo2_profile = self.profile_combo.currentText()
        if not mo2_profile:
            return
        try:
            path = self._modlist_path(mo2_profile)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Error", str(exc))
            return
        if not path.exists():
            QMessageBox.warning(self, "No modlist", "Modlist file not found.")
            return
        new_lines = flip_priority(self._lines)
        if self._write_lines(new_lines):
            self._load_mods()
            # Show temporary status
            self.flip_priority_button.setText("Priority Flipped!")
            QTimer.singleShot(
                2000, lambda: self.flip_priority_button.setText("Flip Priority")
            )
        else:
            self._load_mods()
            QMessageBox.warning(self, "Flip Failed", "Could not flip priority order.")

    def _on_tree_drop(
        self,
        source_name: str,
        target_name: str | None,
        category: str,
        before: bool,
    ) -> None:
        """Persist a drag/drop reorder reported by the mod tree."""
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        new_lines = move_mod(
            self._lines,
            source_name,
            target_name=target_name,
            category=category if target_name is None else None,
            before=before,
            at_start=before and target_name is None,
        )
        if new_lines == self._lines:
            return
        if self._write_lines(new_lines):
            # Defer tree rebuild so it runs after Qt's DragDrop
            # state machine finishes cleaning up the drop event.
            QTimer.singleShot(0, self._load_mods)
            QTimer.singleShot(
                0, lambda name=source_name: self._select_mod_names([name])
            )

    def _open_mo2(self) -> None:
        mo2_profile = self.profile_combo.currentText()
        state = gui_settings.load_gui_settings()
        kind = state.get("runner") or "auto"
        # resolve_runner wants the raw configured path (STEAM_COMPAT_DATA_PATH
        # for Proton), not the resolved WINEPREFIX.
        prefix = state.get("wine_prefix") or ""
        try:
            profile = self._active_profile()
            runner = resolve_runner(kind, prefix)
            command, env, cwd = build_command(
                profile.gamma, runner, profile=mo2_profile or None
            )
            launch_detached(command, env, cwd, log_path=logs_dir() / "launcher.log")
        except (LaunchError, RuntimeError) as exc:
            QMessageBox.warning(self, "Cannot launch MO2", str(exc))

    def _restore_backup(self) -> None:
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            QMessageBox.warning(
                self,
                "Mod Organizer is running",
                "Close Mod Organizer first - it would overwrite your changes "
                "when it exits.",
            )
            return
        mo2_profile = self.profile_combo.currentText()
        try:
            path = self._modlist_path(mo2_profile)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Failed", str(exc))
            return
        default_backup = self._backup_path(path)
        start_path = default_backup if default_backup.is_file() else path.parent
        backup_name, _ = QFileDialog.getOpenFileName(
            self,
            "Open modlist backup",
            str(start_path),
            "Modlist backups (*.bak *.txt);;All files (*)",
        )
        if not backup_name:
            return
        bak = Path(backup_name).expanduser()
        try:
            if not bak.is_file():
                raise FileNotFoundError(f"Backup not found: {bak}")
            if bak.is_symlink():
                raise ValueError("The selected backup cannot be a symlink.")
            if bak.resolve() == path.resolve():
                raise ValueError("The selected file is the current modlist.")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Invalid Backup", str(exc))
            return
        self._restore_backup_file(path, bak, "Restore Backup")

    def _restore_original_order(self) -> None:
        """Place the GAMMA mods back into their original default load order.

        Only the shared GAMMA mods are reordered to match the automatic
        pre-edit backup. User-installed mods, new categories, comments, and
        each mod's enabled/disabled state are left untouched.
        """
        if self.window.install_busy or self._mo2_running():
            self._update_guard()
            return
        mo2_profile = self.profile_combo.currentText()
        if not mo2_profile:
            return
        try:
            path = self._modlist_path(mo2_profile)
            bak = self._backup_path(path)
            if not bak.is_file():
                raise FileNotFoundError("No original modlist backup exists yet.")
            original = read_lines(bak)
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "Original Order Unavailable", str(exc))
            self._update_guard()
            return
        new_lines = reorder_to_original(self._lines, original)
        if new_lines == self._lines:
            QMessageBox.information(
                self,
                "Original Order",
                "The GAMMA mods are already in their original order.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Restore Original Order",
            "Place the GAMMA mods back into their original default load order?\n\n"
            "Your installed mods, new categories, and enabled/disabled state "
            "will be kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._write_lines(new_lines):
            self._load_mods()
            self.backup_status.setText(f"GAMMA mods restored to order from: {bak.name}")

    def _restore_backup_file(self, path: Path, bak: Path, title: str) -> None:
        """Restore *bak* atomically after preserving the current modlist."""
        answer = QMessageBox.question(
            self,
            title,
            f"Restore the modlist from:\n{bak}\n\nCurrent edits will be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.window.install_busy:
            self._update_guard()
            return
        current_backup = self._backup_path(path)
        tmp = path.with_name(path.name + ".restore.tmp")
        try:
            if path.is_file():
                shutil.copy2(path, self._timestamped_backup_path(path))
                if not current_backup.exists():
                    shutil.copy2(path, current_backup)
            shutil.copy2(bak, tmp)
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            QMessageBox.warning(self, "Failed", str(exc))
            return
        self._load_mods()
        self.backup_status.setText(f"Restored from: {bak}")
        self._update_guard()
