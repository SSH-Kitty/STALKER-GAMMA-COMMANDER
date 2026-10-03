"""Profiles page: create, edit, activate and delete CLI profiles."""

from __future__ import annotations

import dataclasses
import html
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import atomic
from ..gui_settings import load_gui_settings, save_gui_settings
from ..modlist import (
    entries,
    modlist_path_for,
    read_lines,
    seed_new_mo2_profile,
    separator_name,
)
from ..profile_bundle import (
    ProfileBundleError,
    export_profile_bundle,
    read_profile_bundle,
    reset_sources,
)
from ..settings import CliProfile, cli_ok, run_config_command
from ..themes import active_theme_tokens
from .common import (
    GAMMA_MARKERS,
    GAMMA_PROFILE,
    BackgroundTask,
    NoWheelComboBox,
    activate_profile,
    anomaly_installed,
    count_active_mods,
    free_space_bytes,
    gamma_installed,
    human_size,
    info_label,
    make_card,
    mo2_running,
    normalize_path,
    open_in_file_manager,
    section_label,
    tr,
)

#: How long a success message stays in the editor's footer.
_STATUS_TIMEOUT_MS = 6000
#: Delay between typing in a folder field and checking that folder on disk.
_FOLDER_CHECK_DELAY_MS = 300


def check_cli_values(profile: CliProfile) -> None:
    """Refuse text fields that start with "-".

    Each value is passed as its own argument after its flag, and the CLI's
    parser reads an argument starting with "-" as another option - so a
    name, URL or branch like "--gamma" (typed, or from an imported bundle)
    would silently change what the command does.
    """
    for name in (
        "profile_name", "anomaly", "gamma", "cache", "mo2_profile",
        "mod_pack_maker_url", "mod_list_url",
        "gamma_setup_repo_url", "gamma_setup_repo_branch",
        "stalker_gamma_repo_url", "stalker_gamma_repo_branch",
        "gamma_large_files_repo_url", "gamma_large_files_repo_branch",
        "teivaz_anomaly_gunslinger_repo_url", "teivaz_anomaly_gunslinger_repo_branch",
    ):
        value = str(getattr(profile, name, "") or "")
        if value.lstrip().startswith("-"):
            raise ValueError(
                tr("{field} can't start with \"-\": {value}", field=name, value=value)
            )


def create_profile_args(profile: CliProfile) -> list[str]:
    """The CLI ``config create`` arguments for ``profile``.

    Module-level so Deck Mode's setup wizard builds exactly the same
    command as this page's "Create" button. Raises ``ValueError`` for a
    value the CLI would read as an option of its own.
    """
    check_cli_values(profile)
    return [
        "create",
        "--anomaly",
        profile.anomaly,
        "--gamma",
        profile.gamma,
        "--cache",
        profile.cache,
        "--name",
        profile.profile_name,
        "--mo2-profile",
        profile.mo2_profile,
        "--mod-pack-maker-url",
        profile.mod_pack_maker_url,
        "--mod-list-url",
        profile.mod_list_url,
        "--download-threads",
        str(profile.download_threads),
        "--gamma-setup-repo-url",
        profile.gamma_setup_repo_url,
        "--gamma-setup-repo-branch",
        profile.gamma_setup_repo_branch,
        "--stalker-gamma-repo-url",
        profile.stalker_gamma_repo_url,
        "--stalker-gamma-repo-branch",
        profile.stalker_gamma_repo_branch,
        "--gamma-large-files-repo-url",
        profile.gamma_large_files_repo_url,
        "--gamma-large-files-repo-branch",
        profile.gamma_large_files_repo_branch,
        "--teivaz-anomaly-gunslinger-repo-url",
        profile.teivaz_anomaly_gunslinger_repo_url,
        "--teivaz-anomaly-gunslinger-repo-branch",
        profile.teivaz_anomaly_gunslinger_repo_branch,
    ]


def save_profile(settings, original_name: str | None, profile: CliProfile) -> None:
    """Replace the profile named ``original_name`` with ``profile`` and save.

    Matches on the name the edit started from, not the (possibly just
    renamed) new name, so a rename replaces the original entry instead of
    leaving it behind as an orphaned duplicate. The edited profile keeps the
    original's unknown JSON keys and its active flag (or becomes active if
    no profile is). On a write failure ``settings.profiles`` is restored and
    the ``OSError`` re-raised. Shared by this page and Deck Mode's editor.
    """
    active = settings.active_profile
    existing = next(
        (p for p in settings.profiles if p.profile_name == original_name),
        None,
    )
    # Build the new list without mutating settings.profiles yet.
    profiles = [p for p in settings.profiles if p is not existing]
    if existing is not None:
        profile.extra = dict(existing.extra)
    profile.active = bool(
        active is None
        or (existing is not None and existing.profile_name == active.profile_name)
    )
    profiles.append(profile)
    original_profiles = settings.profiles[:]
    settings.profiles = profiles
    try:
        settings.save()
    except OSError:
        settings.profiles = original_profiles
        raise
    if original_name and original_name != profile.profile_name:
        # Backups live in a folder named after the profile.
        from ..game_backup import rename_profile_backups

        rename_profile_backups(original_name, profile.profile_name)
        move_profile_extras(original_name, profile.profile_name)


def unique_name(base: str, existing) -> str:
    """``base``, or ``base 2``, ``base 3``... - whichever isn't in ``existing``."""
    taken = {name.upper() for name in existing}
    if base.upper() not in taken:
        return base
    number = 2
    while f"{base} {number}".upper() in taken:
        number += 1
    return f"{base} {number}"


def mo2_profile_names(gamma: str) -> list[str]:
    """Folder names under ``<gamma>/profiles`` (the MO2 profiles on disk)."""
    profiles = Path(gamma) / "profiles" if gamma else None
    if profiles is None or not profiles.is_dir():
        return []
    try:
        return sorted(
            (p.name for p in profiles.iterdir() if p.is_dir()), key=str.lower
        )
    except OSError:
        return []


def profile_summary(profile: CliProfile) -> str:
    """The second line of a profile's list row: MO2 profile and mod count.

    Kept short to fit the narrow card - a mod count already means installed.
    """
    parts = [profile.mo2_profile or GAMMA_PROFILE]
    counts = (
        count_active_mods(profile.gamma, profile.mo2_profile)
        if gamma_installed(profile.gamma, profile.mo2_profile)
        else None
    )
    if counts is not None:
        parts.append(tr("{enabled}/{total} mods", enabled=counts[0], total=counts[1]))
    elif gamma_installed(profile.gamma, profile.mo2_profile):
        parts.append(tr("Installed"))
    else:
        parts.append(tr("Not installed"))
    return " · ".join(parts)


#: Longest note a profile can have.
NOTE_MAX_LENGTH = 500


def profile_note(name: str) -> str:
    return load_gui_settings().get("profile_notes", {}).get(name, "")


def set_profile_note(name: str, text: str) -> None:
    notes = dict(load_gui_settings().get("profile_notes", {}))
    text = text[:NOTE_MAX_LENGTH]
    if notes.get(name, "") == (text if text.strip() else ""):
        return
    if text.strip():
        notes[name] = text
    else:
        notes.pop(name, None)
    save_gui_settings(profile_notes=notes)


#: Color tags a profile can have: key -> (color, label), the same in every theme.
PROFILE_COLORS: dict[str, tuple[str, str]] = {
    "green": ("#5cb85c", "Green"),
    "blue": ("#4a90d9", "Blue"),
    "purple": ("#9b6cd6", "Purple"),
    "orange": ("#e8913a", "Orange"),
    "red": ("#d9534f", "Red"),
    "yellow": ("#e3c341", "Yellow"),
    "gray": ("#8a8f98", "Gray"),
}


def profile_color(name: str) -> str | None:
    key = load_gui_settings().get("profile_colors", {}).get(name)
    return key if key in PROFILE_COLORS else None


def set_profile_color(name: str, key: str | None) -> None:
    colors = dict(load_gui_settings().get("profile_colors", {}))
    if key in PROFILE_COLORS:
        colors[name] = key
    else:
        colors.pop(name, None)
    save_gui_settings(profile_colors=colors)


def move_profile_extras(old_name: str, new_name: str) -> None:
    """Carry a renamed profile's note and color tag over to its new name."""
    state = load_gui_settings()
    changes = {}
    for key in ("profile_notes", "profile_colors"):
        table = dict(state.get(key, {}))
        if old_name in table:
            table[new_name] = table.pop(old_name)
            changes[key] = table
    if changes:
        save_gui_settings(**changes)


def forget_profile_extras(name: str) -> None:
    """Drop a deleted profile's note and color tag."""
    state = load_gui_settings()
    changes = {}
    for key in ("profile_notes", "profile_colors"):
        table = dict(state.get(key, {}))
        if table.pop(name, None) is not None:
            changes[key] = table
    if changes:
        save_gui_settings(**changes)


@dataclasses.dataclass
class CompareRow:
    """One mod in a profile comparison: True enabled, False disabled, None missing."""

    name: str
    left: bool | None
    right: bool | None

    @property
    def differs(self) -> bool:
        return self.left != self.right


def _mod_states(lines: list[str]) -> list[tuple[str, bool]]:
    return [
        (name, status == "Enabled")
        for status, name in entries(lines)
        if separator_name(name) is None
    ]


def compare_modlists(left_lines: list[str], right_lines: list[str]) -> list[CompareRow]:
    """Line two mod lists up by mod name, in file order.

    Keeps the left list's order; a mod only the right list has goes right
    after the nearest mod before it (in the right list) that both share.
    """
    left = _mod_states(left_lines)
    right = _mod_states(right_lines)
    right_states = dict(right)
    rows = [CompareRow(name, enabled, right_states.get(name)) for name, enabled in left]
    position = {row.name: i for i, row in enumerate(rows)}
    pending: dict[int, list[CompareRow]] = {}
    anchor = -1
    for name, enabled in right:
        if name in position:
            anchor = position[name]
        else:
            pending.setdefault(anchor, []).append(CompareRow(name, None, enabled))
    merged = list(pending.get(-1, []))
    for i, row in enumerate(rows):
        merged.append(row)
        merged.extend(pending.get(i, []))
    return merged


def profile_mod_lines(profile: CliProfile) -> list[str] | None:
    """The profile's modlist.txt lines, or None when it has none (yet)."""
    path = modlist_path_for(profile.gamma, profile.mo2_profile or GAMMA_PROFILE)
    if path is None or not path.is_file():
        return None
    try:
        lines = read_lines(path)
        entries(lines)
    except (OSError, ValueError):
        return None
    return lines


def _short_name(name: str, limit: int = 24) -> str:
    """A profile name cut to fit a column header or button."""
    return name if len(name) <= limit else name[: limit - 1] + "…"


class NewProfileDialog(QDialog):
    """Asks whether a new profile is a fresh install or shares an existing one."""

    def __init__(self, profiles: list[CliProfile], active_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("New profile"))
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(section_label(tr("What is this profile for?"), level=2))

        self.fresh_radio = QRadioButton(tr("A fresh install"))
        self.fresh_radio.setChecked(True)
        layout.addWidget(self.fresh_radio)
        layout.addWidget(
            self._indented(
                info_label(
                    tr("Choose new, empty folders, then install Anomaly and GAMMA into them from the Install page.")
                )
            )
        )

        self.shared_radio = QRadioButton(tr("Another MO2 profile on an existing install"))
        layout.addWidget(self.shared_radio)
        layout.addWidget(
            self._indented(
                info_label(
                    tr("Uses the same Anomaly, GAMMA and cache folders as the profile below, with its own mod list. The mod list starts as a copy of that profile's.")
                )
            )
        )
        self.base_combo = NoWheelComboBox()
        for profile in profiles:
            self.base_combo.addItem(profile.profile_name)
        index = self.base_combo.findText(active_name)
        self.base_combo.setCurrentIndex(max(index, 0))
        layout.addWidget(self._indented(self.base_combo))

        group = QButtonGroup(self)
        group.addButton(self.fresh_radio)
        group.addButton(self.shared_radio)
        self.fresh_radio.toggled.connect(
            lambda fresh: self.base_combo.setEnabled(not fresh)
        )
        self.base_combo.setEnabled(False)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Continue"))
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _indented(widget: QWidget) -> QWidget:
        holder = QWidget()
        holder.setObjectName("panelTransparent")
        row = QHBoxLayout(holder)
        row.setContentsMargins(26, 0, 0, 4)
        row.addWidget(widget)
        return holder

    def base_name(self) -> str | None:
        """The profile to share folders with, or None for a fresh install."""
        if self.fresh_radio.isChecked():
            return None
        return self.base_combo.currentText() or None


class _Pages(QWidget):
    """Shows one of its pages at a time, sized to that page alone.

    Unlike a QStackedWidget, which is always as tall as its tallest page,
    so the short overview doesn't get stretched to the edit form's height.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("panelTransparent")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._pages: list[QWidget] = []
        self._index = 0

    def addWidget(self, page: QWidget) -> None:
        self._pages.append(page)
        self._layout.addWidget(page)
        page.setVisible(len(self._pages) - 1 == self._index)

    def setCurrentIndex(self, index: int) -> None:
        self._index = index
        for i, page in enumerate(self._pages):
            page.setVisible(i == index)

    def currentIndex(self) -> int:
        return self._index


class CompareView(QWidget):
    """Read-only side-by-side view of two profiles' mod lists.

    Mod lists are only changed on the Mod Manager page; double-clicking a
    row jumps there.
    """

    def __init__(self, page: ProfilesPage) -> None:
        super().__init__()
        self.page = page
        self.setObjectName("panelTransparent")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(section_label(tr("Compare profiles"), level=2))
        top.addSpacing(12)
        # Any two profiles: the left one starts as the profile on screen,
        # but either side can be changed (or swapped) freely.
        self.compare_left_combo = NoWheelComboBox()
        self.compare_combo = NoWheelComboBox()
        self.compare_swap_button = QPushButton("⇄")
        self.compare_swap_button.setObjectName("iconButton")
        self.compare_swap_button.setFixedSize(36, 30)
        self.compare_swap_button.setToolTip(tr("Swap sides"))
        self.compare_swap_button.clicked.connect(self._swap_compare)
        for combo in (self.compare_left_combo, self.compare_combo):
            combo.setMinimumWidth(220)
            combo.setSizeAdjustPolicy(NoWheelComboBox.SizeAdjustPolicy.AdjustToContents)
        self.compare_left_combo.currentIndexChanged.connect(
            lambda _i: self._on_compare_picked(self.compare_left_combo, self.compare_combo)
        )
        self.compare_combo.currentIndexChanged.connect(
            lambda _i: self._on_compare_picked(self.compare_combo, self.compare_left_combo)
        )
        top.addWidget(self.compare_left_combo)
        top.addWidget(self.compare_swap_button)
        top.addWidget(self.compare_combo)
        top.addStretch(1)
        self.compare_count = QLabel()
        self.compare_count.setObjectName("dim")
        top.addWidget(self.compare_count)
        self.compare_filter = NoWheelComboBox()
        self.compare_filter.addItems([tr("Differences only"), tr("All mods")])
        self.compare_filter.setCurrentIndex(1)
        self.compare_filter.currentIndexChanged.connect(lambda _i: self._refresh_compare())
        top.addWidget(self.compare_filter)
        layout.addLayout(top)

        self.compare_empty = info_label("")
        layout.addWidget(self.compare_empty)

        # One row per mod: its name once, then its state in each profile -
        # so a row always reads straight across.
        self.compare_table = QTableWidget(0, 3)
        self.compare_table.setObjectName("compareTable")
        self.compare_table.verticalHeader().setVisible(False)
        self.compare_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Click selects the whole row, highlighted like Mod Manager's list.
        self.compare_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.compare_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.compare_table.cellDoubleClicked.connect(self._open_in_mod_manager)
        self.compare_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.compare_table.setShowGrid(False)
        self.compare_table.setWordWrap(False)
        self.compare_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.compare_table.verticalHeader().setDefaultSectionSize(26)
        header = self.compare_table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setMinimumSectionSize(130)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # Fixed, sized once per fill: ResizeToContents re-measures every row
        # on every setItem(), which made refilling a ~700-row "All mods"
        # table freeze the window for seconds.
        for column in (1, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        mod_header = QTableWidgetItem(tr("Mod"))
        mod_header.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.compare_table.setHorizontalHeaderItem(0, mod_header)
        for column in (1, 2):
            self.compare_table.setHorizontalHeaderItem(column, QTableWidgetItem())
        # Tall by default so a long list of differences reads at a glance.
        self.compare_table.setMinimumHeight(480)
        layout.addWidget(self.compare_table, 1)
        # Profile the left side last followed (see show_for).
        self._synced_to = ""

    def show_for(self, name: str) -> None:
        """Refill the pickers; the left side follows the profile on screen."""
        names = [p.profile_name for p in self.page.settings.profiles]
        left_name = self.compare_left_combo.currentData()
        right_name = self.compare_combo.currentData()
        if self._synced_to != name:
            self._synced_to = name
            if right_name == name:
                right_name = left_name
            left_name = name
        if left_name not in names:
            left_name = name
        if right_name not in names or right_name == left_name:
            active = self.page.settings.active_profile
            candidates = ([active.profile_name] if active is not None else []) + names
            right_name = next((n for n in candidates if n != left_name), None)
        for combo, picked in ((self.compare_left_combo, left_name), (self.compare_combo, right_name)):
            combo.blockSignals(True)
            combo.clear()
            for profile_name in names:
                combo.addItem(profile_name, profile_name)
            combo.setCurrentIndex(max(combo.findData(picked), 0))
            combo.setProperty("previousData", combo.currentData())
            combo.blockSignals(False)
        self._refresh_compare()

    def _compare_left(self) -> CliProfile | None:
        return self.page._profile(self.compare_left_combo.currentData())

    def _compare_target(self) -> CliProfile | None:
        return self.page._profile(self.compare_combo.currentData())

    def _on_compare_picked(self, changed, other) -> None:
        """Picking the profile the other side shows swaps the two sides."""
        if changed.currentData() == other.currentData():
            previous = changed.property("previousData")
            index = other.findData(previous)
            if index >= 0 and previous != changed.currentData():
                other.blockSignals(True)
                other.setCurrentIndex(index)
                other.blockSignals(False)
        for combo in (changed, other):
            combo.setProperty("previousData", combo.currentData())
        self._refresh_compare()

    def _swap_compare(self) -> None:
        left, right = self.compare_left_combo.currentData(), self.compare_combo.currentData()
        for combo, name in ((self.compare_left_combo, right), (self.compare_combo, left)):
            combo.blockSignals(True)
            combo.setCurrentIndex(max(combo.findData(name), 0))
            combo.setProperty("previousData", combo.currentData())
            combo.blockSignals(False)
        self._refresh_compare()

    def _refresh_compare(self) -> None:
        left, right = self._compare_left(), self._compare_target()
        if left is None or right is None or left is right:
            self._compare_message(tr("Create another profile to compare mod lists."))
            return
        self.compare_swap_button.setEnabled(True)
        left_lines = profile_mod_lines(left)
        right_lines = profile_mod_lines(right)
        if left_lines is None or right_lines is None:
            missing = left if left_lines is None else right
            self.compare_empty.setText(
                tr("'{name}' has no mod list yet - install GAMMA for it first.", name=missing.profile_name)
            )
            self.compare_empty.show()
        else:
            self.compare_empty.hide()
        rows = compare_modlists(left_lines or [], right_lines or [])
        # Same order as Mod Manager and MO2 show it: file order reversed.
        rows.reverse()
        differences = [row for row in rows if row.differs]
        self.compare_count.setText(
            tr("Same mod list")
            if not differences
            else tr("{count} differences", count=len(differences))
        )
        self.compare_filter.show()
        shown = differences if self.compare_filter.currentIndex() == 0 else rows
        self._fill_compare_table(shown, tint=self.compare_filter.currentIndex() == 1)
        # Nothing to list (identical lists, "Differences only"): say so
        # instead of showing an empty table.
        self.compare_table.setVisible(bool(shown))
        if not shown and left_lines is not None and right_lines is not None:
            self.compare_empty.setText(
                tr(
                    "Both profiles have the same mods, enabled the same way. Choose \"{option}\" to see the whole list.",
                    option=tr("All mods"),
                )
            )
            self.compare_empty.show()

        for column, profile, lines in ((1, left, left_lines), (2, right, right_lines)):
            counts = count_active_mods(profile.gamma, profile.mo2_profile or GAMMA_PROFILE)
            text = _short_name(profile.profile_name)
            if counts is not None and lines is not None:
                text += "\n" + tr(
                    "{enabled} / {total} enabled", enabled=counts[0], total=counts[1]
                )
            item = self.compare_table.horizontalHeaderItem(column)
            item.setText(text)
            item.setToolTip(profile.profile_name)
        for column in (1, 2):
            self.compare_table.resizeColumnToContents(column)
            # A little breathing room between the two status columns.
            self.compare_table.setColumnWidth(
                column, self.compare_table.columnWidth(column) + 32
            )

    def _compare_message(self, text: str) -> None:
        self.compare_empty.setText(text)
        self.compare_empty.show()
        self.compare_table.hide()
        self.compare_swap_button.setEnabled(False)
        self.compare_filter.hide()
        self.compare_count.setText("")

    def _fill_compare_table(self, rows: list[CompareRow], *, tint: bool) -> None:
        tokens = active_theme_tokens()
        ok_color = QColor(tokens.get("accent_strong", "#7dc963"))
        warn_color = QColor(tokens.get("warn", "#e0a040"))
        dim_color = QColor(tokens.get("text_dim", "#888888"))
        tint_color = QColor(tokens.get("warn", "#e0a040"))
        tint_color.setAlpha(28)
        states = {
            True: ("✓ " + tr("Enabled"), ok_color),
            False: ("✗ " + tr("Disabled"), warn_color),
            None: ("— " + tr("Not in list"), dim_color),
        }
        table = self.compare_table
        selected = table.item(table.currentRow(), 0) if table.selectedItems() else None
        selected_name = selected.text() if selected is not None else None
        table.setUpdatesEnabled(False)
        table.setRowCount(0)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            name_item = QTableWidgetItem(row.name)
            name_item.setToolTip(
                row.name + "\n" + tr("Double-click to open in Mod Manager")
            )
            name_item.setData(Qt.ItemDataRole.UserRole, row)
            cells = [name_item]
            for state in (row.left, row.right):
                text, color = states[state]
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setForeground(color)
                cells.append(item)
            for column, item in enumerate(cells):
                if tint and row.differs:
                    item.setBackground(tint_color)
                table.setItem(row_index, column, item)
            if row.name == selected_name:
                table.selectRow(row_index)
        table.setUpdatesEnabled(True)

    def _open_in_mod_manager(self, row_index: int, _column: int) -> None:
        name_item = self.compare_table.item(row_index, 0)
        row: CompareRow | None = name_item.data(Qt.ItemDataRole.UserRole) if name_item else None
        active = self.page.settings.active_profile
        if row is None or active is None:
            return
        # Mod Manager only shows the active profile's list.
        sides = ((self._compare_left(), row.left), (self._compare_target(), row.right))
        if not any(
            profile is not None and profile.profile_name == active.profile_name and state is not None
            for profile, state in sides
        ):
            self.compare_empty.setText(
                tr(
                    "Mod Manager shows the active profile '{name}' - this mod is not in its list.",
                    name=active.profile_name,
                )
            )
            self.compare_empty.show()
            return
        window = self.page.window
        window.set_page("modmanager")
        window._ensure_page("modmanager").reveal_mod(row.name)


class ProfilesPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.settings = window.settings
        self._task: BackgroundTask | None = None
        # Name of the saved profile on screen, "" while creating one.
        self._form_state = ""
        # "empty" (no profiles), "view" (overview of a saved profile),
        # "edit" (editing it) or "create" (setting up a new one).
        self._mode = "empty"
        # The form's values as last loaded or saved - edits differ from it.
        self._baseline: dict = {}
        # Blocks change tracking while _load_form fills the fields.
        self._loading = False
        # MO2 profile a new profile's mod list is copied from on create -
        # None for a fresh install (the CLI's own download stays).
        self._source_mo2: str | None = None
        # Profile to go back to when a "create" is cancelled.
        self._return_to = ""
        self._browse_buttons: list[QPushButton] = []
        # The profile the form was loaded from: fields the form doesn't show
        # (MO2 profile, download threads) are kept from it on save.
        self._loaded = CliProfile()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages)
        self.pages.addWidget(self._build_empty_state())
        self.pages.addWidget(self._build_main())

        self._folder_timer = QTimer(self)
        self._folder_timer.setSingleShot(True)
        self._folder_timer.setInterval(_FOLDER_CHECK_DELAY_MS)
        self._folder_timer.timeout.connect(self._update_folder_checks)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self._show_status(""))

        for edit in self._text_fields():
            edit.textChanged.connect(self._on_form_changed)
        for edit in (self.anomaly_edit, self.gamma_edit, self.cache_edit):
            edit.textChanged.connect(lambda _text: self._folder_timer.start())

        self.refresh()

    # ----- building -----
    def _build_empty_state(self) -> QWidget:
        page = QWidget()
        page.setObjectName("pageContent")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        card, body = make_card()
        card.setFixedWidth(540)
        body.setContentsMargins(28, 28, 28, 28)
        body.setSpacing(12)
        body.addWidget(section_label(tr("No profiles yet")))
        body.addWidget(
            info_label(
                tr("A profile tells COMMANDER where Anomaly and GAMMA are installed, where downloads are cached and which MO2 profile to use. Create one to get started, or import one you exported before.")
            )
        )
        row = QHBoxLayout()
        self.empty_create_button = QPushButton(tr("Create first profile"))
        self.empty_create_button.setObjectName("primary")
        self.empty_create_button.clicked.connect(self._on_new_clicked)
        self.empty_import_button = QPushButton(tr("Import profile"))
        self.empty_import_button.clicked.connect(self._import_profile)
        row.addWidget(self.empty_create_button)
        row.addWidget(self.empty_import_button)
        row.addStretch(1)
        body.addLayout(row)
        # Centered with stretches, not an alignment flag: an aligned widget
        # gets its plain size hint, which clips the word-wrapped text.
        center = QHBoxLayout()
        center.addStretch(1)
        center.addWidget(card)
        center.addStretch(1)
        layout.addLayout(center)
        layout.addStretch(2)
        return page

    def _build_main(self) -> QWidget:
        page = QWidget()
        page.setObjectName("pageContent")
        root = QHBoxLayout(page)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(20)
        root.addWidget(self._build_list_panel())

        column = QVBoxLayout()
        column.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        holder = QWidget()
        holder.setObjectName("pageContent")
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(16)
        top_row = QHBoxLayout()
        top_row.setSpacing(16)
        top_row.addWidget(self._build_detail_card(), 2)
        top_row.addWidget(self._build_note_card(), 1)
        holder_layout.addLayout(top_row)
        # The compare card takes all spare height so its table shows more
        # rows; the stretch-0 spacer only soaks up space while it's hidden.
        self.compare_card, compare_layout = make_card()
        compare_layout.setContentsMargins(20, 14, 20, 16)
        self.compare_view = CompareView(self)
        compare_layout.addWidget(self.compare_view, 1)
        holder_layout.addWidget(self.compare_card, 1)
        holder_layout.addStretch(0)
        scroll.setWidget(holder)
        column.addWidget(scroll, 1)
        column.addWidget(self._build_footer())
        root.addLayout(column, 1)
        return page

    def _build_list_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("panelTransparent")
        panel.setFixedWidth(290)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.addWidget(section_label(tr("Profiles")))
        header.addStretch(1)
        self.new_button = QPushButton(tr("+ New"))
        self.new_button.setObjectName("primary")
        self.new_button.setToolTip(tr("Create a new profile or import one from a file."))
        new_menu = QMenu(self.new_button)
        new_menu.addAction(tr("New profile...")).triggered.connect(self._on_new_clicked)
        new_menu.addAction(tr("Import from file...")).triggered.connect(self._import_profile)
        self.new_button.setMenu(new_menu)
        header.addWidget(self.new_button)
        layout.addLayout(header)

        self.profile_list = QListWidget()
        self.profile_list.setObjectName("profileCards")
        self.profile_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.profile_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.profile_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.profile_list.customContextMenuRequested.connect(self._show_context_menu)
        self.profile_list.currentItemChanged.connect(self._paint_selection)
        self.profile_list.currentItemChanged.connect(self._on_select)
        self.profile_list.itemDoubleClicked.connect(self._on_double_click)
        self.profile_list.setToolTip(
            tr("Double-click a profile to make it active. Right-click for more.")
        )
        delete_key = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.profile_list)
        delete_key.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_key.activated.connect(self._delete_profile)
        layout.addWidget(self.profile_list, 1)
        return panel

    def _build_detail_card(self) -> QWidget:
        card, layout = make_card()
        layout.setContentsMargins(20, 14, 20, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        self.title_label = section_label("")
        self.active_chip = QLabel(tr("ACTIVE"))
        self.active_chip.setObjectName("statusReady")
        self.active_chip.setToolTip(tr("The other pages use this profile."))
        self.editing_chip = QLabel(tr("EDITING"))
        self.editing_chip.setObjectName("statusChecking")
        header.addWidget(self.title_label)
        header.addWidget(self.active_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.editing_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)
        self.active_button = QPushButton(tr("Set active"))
        self.active_button.setObjectName("primary")
        self.active_button.setToolTip(tr("Make this profile active for the other pages."))
        self.active_button.clicked.connect(self._set_active)
        self.edit_button = QPushButton(tr("Edit"))
        self.edit_button.setToolTip(tr("Change this profile's name and folders."))
        self.edit_button.clicked.connect(self._begin_edit)
        self.more_button = QPushButton("⋯")
        self.more_button.setObjectName("iconButton")
        self.more_button.setFixedSize(36, 34)
        self.more_button.setToolTip(tr("More actions"))
        self.more_menu = QMenu(self.more_button)
        self.export_action = self.more_menu.addAction(tr("Export..."))
        self.export_action.triggered.connect(self._export_profile)
        self.more_menu.addSeparator()
        self.delete_action = self.more_menu.addAction(tr("Delete"))
        self.delete_action.triggered.connect(self._delete_profile)
        self.more_button.setMenu(self.more_menu)
        for button in (self.active_button, self.edit_button, self.more_button):
            header.addWidget(button)
        layout.addLayout(header)

        self.status_label = QLabel()
        self.status_label.setObjectName("accent")
        self.status_label.hide()
        layout.addWidget(self.status_label)

        self.detail_stack = _Pages()
        self.detail_stack.addWidget(self._build_overview())
        self.detail_stack.addWidget(self._build_form())
        layout.addWidget(self.detail_stack)
        return card

    def _build_note_card(self) -> QWidget:
        self.note_card, layout = make_card()
        layout.setContentsMargins(20, 14, 20, 16)
        layout.setSpacing(8)
        layout.addWidget(section_label(tr("Note"), level=2))
        self.note_edit = QPlainTextEdit()
        # Styled like a text field, not like the monospace log panes.
        self.note_edit.setObjectName("noteEdit")
        self.note_edit.setPlaceholderText(tr("Add a note for this profile..."))
        self.note_edit.setMinimumHeight(60)
        # Ignored: the text box's tall default size hint would stretch the
        # whole top row; it just fills whatever height the profile card has.
        self.note_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.note_edit.setTabChangesFocus(True)
        self.note_edit.textChanged.connect(self._on_note_changed)
        layout.addWidget(self.note_edit, 1)
        self._note_owner = ""
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.setInterval(600)
        self._note_timer.timeout.connect(self._save_note)
        return self.note_card

    def _build_overview(self) -> QWidget:
        page = QWidget()
        page.setObjectName("panelTransparent")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # One line instead of stat tiles: install state, mods, free space.
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.details = QGridLayout()
        self.details.setHorizontalSpacing(10)
        self.details.setVerticalSpacing(4)
        self.details.setColumnMinimumWidth(0, 110)
        self.details.setColumnMinimumWidth(1, 14)
        self.details.setColumnStretch(2, 1)
        self._detail_values: dict[str, QLabel] = {}
        self._detail_marks: dict[str, QLabel] = {}
        rows = (
            ("anomaly", tr("Anomaly")),
            ("gamma", tr("GAMMA")),
            ("cache", tr("Cache")),
        )
        for row, (key, text) in enumerate(rows):
            label = QLabel(text)
            label.setObjectName("dim")
            value = QLabel()
            value.setTextFormat(Qt.TextFormat.RichText)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            value.linkActivated.connect(open_in_file_manager)
            value.setWordWrap(True)
            mark = QLabel()
            self.details.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
            self.details.addWidget(mark, row, 1, Qt.AlignmentFlag.AlignTop)
            self.details.addWidget(value, row, 2)
            self._detail_values[key] = value
            self._detail_marks[key] = mark
        layout.addLayout(self.details)
        return page

    def _build_form(self) -> QWidget:
        page = QWidget()
        page.setObjectName("panelTransparent")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(tr("e.g. GAMMA"))
        self.name_edit.setMaxLength(60)
        self.name_edit.setToolTip(tr("A short label used to identify this profile."))
        layout.addLayout(self._field_block(tr("Profile name"), self.name_edit))

        self.anomaly_edit = QLineEdit()
        self.gamma_edit = QLineEdit()
        self.cache_edit = QLineEdit()
        self.anomaly_check = QLabel()
        self.gamma_check = QLabel()
        self.cache_check = QLabel()
        folders = (
            (tr("Anomaly folder"), self.anomaly_edit, self.anomaly_check,
             tr("The folder containing the STALKER Anomaly base game.")),
            (tr("GAMMA folder"), self.gamma_edit, self.gamma_check,
             tr("The GAMMA folder containing ModOrganizer.exe.")),
            (tr("Cache folder"), self.cache_edit, self.cache_check,
             tr("A location with enough free space for downloaded addon archives.")),
        )
        folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        for text, edit, check, tooltip in folders:
            edit.setToolTip(tooltip)
            edit.setPlaceholderText(tr("Choose a folder..."))
            browse = QPushButton()
            browse.setIcon(folder_icon)
            browse.setFixedWidth(40)
            browse.setToolTip(tr("Pick the folder with a file dialog."))
            browse.clicked.connect(lambda _=False, e=edit: self._browse(e))
            self._browse_buttons.append(browse)
            row = QHBoxLayout()
            row.setSpacing(6)
            row.addWidget(edit, 1)
            row.addWidget(browse)
            layout.addLayout(self._field_block(text, row, note=check))

        return page

    def _field_block(self, text: str, field, *, note: QLabel | None = None) -> QVBoxLayout:
        """A small label (with an optional note on its right) above a field."""
        block = QVBoxLayout()
        block.setSpacing(6)
        top = QHBoxLayout()
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        top.addWidget(label)
        top.addStretch(1)
        if note is not None:
            note.setObjectName("dim")
            top.addWidget(note)
        block.addLayout(top)
        if isinstance(field, QWidget):
            block.addWidget(field)
        else:
            block.addLayout(field)
        return block

    def _build_footer(self) -> QWidget:
        self.footer, layout = make_card()
        layout.setContentsMargins(20, 10, 20, 10)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.unsaved_label = QLabel(tr("● Unsaved changes"))
        self.unsaved_label.setObjectName("warn")
        self.discard_button = QPushButton(tr("Cancel"))
        self.discard_button.clicked.connect(self._discard)
        self.save_button = QPushButton(tr("Save changes"))
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._save_or_create)
        row.addWidget(self.unsaved_label)
        row.addStretch(1)
        row.addWidget(self.discard_button)
        row.addWidget(self.save_button)
        layout.addLayout(row)
        return self.footer

    def _text_fields(self) -> tuple[QLineEdit, ...]:
        return (self.name_edit, self.anomaly_edit, self.gamma_edit, self.cache_edit)

    # ----- list -----
    def refresh(self, *, reload_form: bool = False) -> None:
        """Rebuild the list from settings.

        A form being edited stays on screen (the main window calls this on
        every visit to the page); ``reload_form`` drops it after a
        save/create/delete.
        """
        self.window.refresh_settings()
        self.settings = self.window.settings
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for profile in self.settings.profiles:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, profile.profile_name)
            item.setToolTip(
                tr(
                    "Anomaly: {anomaly}\nGAMMA: {gamma}\nCache: {cache}",
                    anomaly=profile.anomaly or "-",
                    gamma=profile.gamma or "-",
                    cache=profile.cache or "-",
                )
            )
            widget = self._profile_item_widget(profile)
            hint = widget.sizeHint()
            hint.setHeight(max(hint.height(), 68))
            item.setSizeHint(hint)
            self.profile_list.addItem(item)
            self.profile_list.setItemWidget(item, widget)
        self.profile_list.blockSignals(False)

        names = self._profile_names()
        keep_form = not reload_form and (
            self._mode == "create"
            or (self._mode == "edit" and self._form_state in names)
        )
        if keep_form:
            self._select_row_silently(self._form_state)
        elif names:
            name = self._form_state if self._form_state in names else None
            if name is None:
                active = self.settings.active_profile
                name = active.profile_name if active is not None else names[0]
            self._show_profile(name)
        else:
            self._mode = "empty"
            self._form_state = ""
            self._load_form(CliProfile())
        self.pages.setCurrentIndex(0 if self._mode == "empty" else 1)
        self._update_header()

    def _profile_item_widget(self, profile: CliProfile) -> QWidget:
        widget = QWidget()
        widget.setObjectName("panelTransparent")
        outer = QVBoxLayout(widget)
        # The bottom 8px is the gap between cards.
        outer.setContentsMargins(0, 0, 0, 8)
        # The frame draws the card (see profileCard in themes.py): the
        # active profile gets an accent border instead of an ACTIVE chip.
        frame = QFrame()
        frame.setObjectName("profileCard")
        frame.setProperty("active", profile.active)
        outer.addWidget(frame)
        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        color = profile_color(profile.profile_name)
        stripe = QFrame()
        stripe.setFixedWidth(4)
        if color is not None:
            stripe.setStyleSheet(
                f"background-color: {PROFILE_COLORS[color][0]}; border-radius: 2px;"
            )
        stripe.setVisible(color is not None)
        stripe_holder = QVBoxLayout()
        stripe_holder.setContentsMargins(6, 10, 0, 10)
        stripe_holder.addWidget(stripe)
        row.addLayout(stripe_holder)
        layout = QVBoxLayout()
        layout.setContentsMargins(14 if color is None else 8, 12, 14, 12)
        layout.setSpacing(4)
        row.addLayout(layout, 1)
        name = QLabel(profile.profile_name)
        name.setObjectName("cardTitle")
        name.setMinimumHeight(22)
        summary = QLabel(profile_summary(profile))
        summary.setObjectName("dim")
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        for label in (name, summary):
            # Never wider than the list: long text is clipped, not the card.
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout.addWidget(label)
        return widget

    def _paint_selection(self, *_args) -> None:
        """Mark the selected card's frame so the theme can outline it."""
        current = self.profile_list.currentItem()
        for i in range(self.profile_list.count()):
            item = self.profile_list.item(i)
            widget = self.profile_list.itemWidget(item)
            frame = widget.findChild(QFrame, "profileCard") if widget else None
            if frame is None:
                continue
            frame.setProperty("selected", item is current)
            frame.style().unpolish(frame)
            frame.style().polish(frame)

    def _profile_names(self) -> list[str]:
        return [p.profile_name for p in self.settings.profiles]

    def _profile(self, name: str | None) -> CliProfile | None:
        return next(
            (p for p in self.settings.profiles if p.profile_name == name), None
        )

    def _has_saved(self) -> bool:
        """True when the panel shows a saved profile (overview or editing)."""
        return self._mode in ("view", "edit") and bool(self._form_state)

    def _select_row_silently(self, name: str) -> None:
        """Highlight ``name`` in the list (none for "") without reloading."""
        self.profile_list.blockSignals(True)
        row = next(
            (
                i for i in range(self.profile_list.count())
                if self.profile_list.item(i).data(Qt.ItemDataRole.UserRole) == name
            ),
            -1,
        )
        self.profile_list.setCurrentRow(row)
        self.profile_list.blockSignals(False)
        self._paint_selection()

    def _show_profile(self, name: str) -> None:
        """Show a saved profile's overview, discarding any open edit."""
        profile = self._profile(name)
        if profile is None:
            return
        self._flush_note()
        self._mode = "view"
        self._form_state = name
        self._select_row_silently(name)
        self._load_form(profile)
        self._render_overview(profile)
        self._load_note(name)
        self.compare_view.show_for(name)
        self.detail_stack.setCurrentIndex(0)
        self.pages.setCurrentIndex(1)
        self._update_header()

    def _begin_edit(self) -> None:
        if self._busy_guard() or self._mode != "view":
            return
        profile = self._profile(self._form_state)
        if profile is None:
            return
        self._mode = "edit"
        self._load_form(profile)
        self.detail_stack.setCurrentIndex(1)
        self._update_header()
        self.name_edit.setFocus()

    def _on_select(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None:
            return
        name = current.data(Qt.ItemDataRole.UserRole)
        if self._mode == "view":
            self._show_profile(name)
            return
        if self._mode == "edit" and name == self._form_state:
            return
        # Put the highlight back until the user decides about unsaved edits.
        self._select_row_silently(self._form_state if self._mode == "edit" else "")
        if self._confirm_leave():
            self._show_profile(name)

    def _confirm_leave(self) -> bool:
        """Ask what to do with unsaved edits. True when it's fine to move on."""
        if not self._is_dirty():
            return True
        if self._mode == "create":
            answer = QMessageBox.question(
                self,
                tr("Unsaved Changes"),
                tr("Discard the new profile you're setting up?"),
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            return answer == QMessageBox.StandardButton.Discard
        answer = QMessageBox.question(
            self,
            tr("Unsaved Changes"),
            tr("Save your changes to '{name}' first?", name=self._form_state),
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self._save_or_create()
        return answer == QMessageBox.StandardButton.Discard

    def _on_double_click(self, item: QListWidgetItem) -> None:
        # Only when the click actually switched to that profile - not when
        # the user just chose to stay on one with unsaved edits.
        if self._mode == "view" and item.data(Qt.ItemDataRole.UserRole) == self._form_state:
            self._set_active()

    def _show_context_menu(self, pos) -> None:
        item = self.profile_list.itemAt(pos)
        if item is None or self.window.install_busy:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if name != self._form_state or self._mode != "view":
            self.profile_list.setCurrentItem(item)
            if name != self._form_state or self._mode != "view":
                return  # The user chose to stay on the unsaved form.
        profile = self._profile(name)
        menu = QMenu(self)
        set_active = menu.addAction(tr("Set active"))
        set_active.setEnabled(profile is not None and not profile.active)
        set_active.triggered.connect(self._set_active)
        menu.addAction(tr("Edit")).triggered.connect(self._begin_edit)
        colors = menu.addMenu(tr("Color"))
        current = profile_color(name)
        for key, (fill, label) in ((None, ("", "No color")), *PROFILE_COLORS.items()):
            action = colors.addAction(tr(label))
            if fill:
                pixmap = QPixmap(14, 14)
                pixmap.fill(QColor(fill))
                action.setIcon(QIcon(pixmap))
            action.setCheckable(True)
            action.setChecked(key == current)
            action.triggered.connect(lambda _c=False, k=key: self._set_color(name, k))
        menu.addSeparator()
        menu.addActions(self.more_menu.actions())
        menu.exec(self.profile_list.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def _set_color(self, name: str, key: str | None) -> None:
        try:
            set_profile_color(name, key)
        except OSError as exc:
            QMessageBox.warning(
                self, tr("Save Failed"), tr("Could not save the color:\n{exc}", exc=exc)
            )
            return
        self.refresh()

    # ----- note -----
    def _load_note(self, name: str) -> None:
        self._note_owner = name
        self.note_edit.blockSignals(True)
        self.note_edit.setPlainText(profile_note(name))
        self.note_edit.blockSignals(False)

    def _on_note_changed(self) -> None:
        text = self.note_edit.toPlainText()
        if len(text) > NOTE_MAX_LENGTH:
            cursor = self.note_edit.textCursor()
            self.note_edit.blockSignals(True)
            self.note_edit.setPlainText(text[:NOTE_MAX_LENGTH])
            self.note_edit.blockSignals(False)
            cursor.setPosition(min(cursor.position(), NOTE_MAX_LENGTH))
            self.note_edit.setTextCursor(cursor)
        self._note_timer.start()

    def _save_note(self) -> None:
        self._note_timer.stop()
        if not self._note_owner:
            return
        try:
            set_profile_note(self._note_owner, self.note_edit.toPlainText())
        except OSError as exc:
            QMessageBox.warning(
                self, tr("Save Failed"), tr("Could not save the note:\n{exc}", exc=exc)
            )

    def _flush_note(self) -> None:
        if self._note_timer.isActive():
            self._save_note()

    # ----- overview -----
    def _render_overview(self, profile: CliProfile) -> None:
        mo2 = profile.mo2_profile or GAMMA_PROFILE
        anomaly_ok = anomaly_installed(profile.anomaly)
        gamma_ok = gamma_installed(profile.gamma, mo2)
        if anomaly_ok and gamma_ok:
            parts = ["✓ " + tr("Ready")]
        elif gamma_ok:
            parts = ["✗ " + tr("Anomaly not found")]
        elif anomaly_ok:
            parts = ["✗ " + tr("GAMMA not installed")]
        else:
            parts = ["✗ " + tr("Not installed")]
        counts = count_active_mods(profile.gamma, mo2)
        if counts is not None:
            parts.append(tr("{enabled}/{total} mods", enabled=counts[0], total=counts[1]))
        free = free_space_bytes(profile.cache) if profile.cache else None
        if free is not None:
            parts.append(tr("{size} free", size=human_size(free)))
        self.summary_label.setText("  ·  ".join(parts))
        self.summary_label.setObjectName("accent" if anomaly_ok and gamma_ok else "warn")
        self.summary_label.style().unpolish(self.summary_label)
        self.summary_label.style().polish(self.summary_label)

        def folder(key: str, path: str, found: bool | None) -> None:
            value = self._detail_values[key]
            mark = self._detail_marks[key]
            if path and Path(path).is_dir():
                color = active_theme_tokens().get("text", "#e0e0e0")
                value.setText(
                    f'<a href="{html.escape(path, quote=True)}" '
                    f'style="color: {color}; text-decoration: none;">{html.escape(path)}</a>'
                )
                value.setToolTip(tr("Click to open this folder."))
            else:
                value.setText(html.escape(path or "-"))
                value.setToolTip(tr("This folder doesn't exist yet."))
            self._set_mark(mark, found)

        folder("anomaly", profile.anomaly, anomaly_ok)
        folder("gamma", profile.gamma, gamma_ok)
        folder("cache", profile.cache, True if profile.cache and Path(profile.cache).is_dir() else None)

    @staticmethod
    def _set_mark(mark: QLabel, ok: bool | None) -> None:
        mark.setText("" if ok is None else ("✓" if ok else "✗"))
        mark.setObjectName("accent" if ok else "warn")
        mark.style().unpolish(mark)
        mark.style().polish(mark)

    # ----- form -----
    def _load_form(self, profile: CliProfile) -> None:
        self._loaded = profile
        self._loading = True
        try:
            self.name_edit.setText(profile.profile_name)
            self.anomaly_edit.setText(profile.anomaly)
            self.gamma_edit.setText(profile.gamma)
            self.cache_edit.setText(profile.cache)
        finally:
            self._loading = False
        self._baseline = self._form_values().to_dict()
        self._update_folder_checks()
        self._on_form_changed()

    def _form_values(self) -> CliProfile:
        # MO2 profile and download threads aren't on the form: they come
        # from the loaded profile. Download sources are always the official
        # ones so the official GAMMA mod list can't be swapped out here.
        profile = dataclasses.replace(self._loaded, extra={})
        profile.profile_name = self.name_edit.text().strip()
        profile.anomaly = normalize_path(self.anomaly_edit.text())
        profile.gamma = normalize_path(self.gamma_edit.text())
        profile.cache = normalize_path(self.cache_edit.text())
        profile.mo2_profile = profile.mo2_profile or GAMMA_PROFILE
        reset_sources(profile)
        return profile

    def _is_dirty(self) -> bool:
        if self._mode not in ("edit", "create"):
            return False
        return self._form_values().to_dict() != self._baseline

    def _on_form_changed(self, *_args) -> None:
        if self._loading:
            return
        self.unsaved_label.setVisible(self._is_dirty())
        self._update_busy_state()

    def _update_header(self) -> None:
        viewing = self._mode == "view"
        editing = self._mode in ("edit", "create")
        profile = self._profile(self._form_state)
        is_active = bool(profile is not None and profile.active)
        self.title_label.setText(
            tr("New profile") if self._mode == "create" else self._form_state
        )
        self.active_chip.setVisible(self._mode != "create" and is_active)
        self.editing_chip.setVisible(self._mode == "edit")
        self.active_button.setVisible(viewing and not is_active)
        self.edit_button.setVisible(viewing)
        self.more_button.setVisible(viewing)
        self.note_card.setVisible(viewing)
        self.compare_card.setVisible(viewing)
        self.footer.setVisible(editing)
        if self._mode == "create":
            self.save_button.setText(tr("Create profile"))
            self.save_button.setToolTip(tr("Create a new profile and activate it."))
            self.discard_button.setToolTip(tr("Stop creating this profile."))
        else:
            self.save_button.setText(tr("Save changes"))
            self.save_button.setToolTip(tr("Save changes to this profile."))
            self.discard_button.setToolTip(tr("Leave without saving."))
        self._update_busy_state()

    # ----- folder checks -----
    def _set_check(self, label: QLabel, text: str, state: str) -> None:
        """Show a folder's check result; ``state`` is ok, info or bad."""
        label.setText(text)
        label.setObjectName({"ok": "accent", "bad": "warn"}.get(state, "dim"))
        label.style().unpolish(label)
        label.style().polish(label)

    def _update_folder_checks(self) -> None:
        anomaly = normalize_path(self.anomaly_edit.text())
        gamma = normalize_path(self.gamma_edit.text())
        cache = normalize_path(self.cache_edit.text())
        mo2 = self._loaded.mo2_profile or GAMMA_PROFILE

        if not anomaly:
            self._set_check(self.anomaly_check, tr("Required"), "bad")
        elif anomaly_installed(anomaly):
            self._set_check(self.anomaly_check, tr("✓ Anomaly found"), "ok")
        elif Path(anomaly).is_dir():
            self._set_check(self.anomaly_check, tr("Not installed yet"), "info")
        else:
            self._set_check(self.anomaly_check, tr("Will be created on install"), "info")

        gamma_dir = Path(gamma) if gamma else None
        if not gamma:
            self._set_check(self.gamma_check, tr("Required"), "bad")
        elif gamma_installed(gamma, mo2):
            self._set_check(self.gamma_check, tr("✓ GAMMA installed"), "ok")
        elif all((gamma_dir / m).is_file() for m in GAMMA_MARKERS):
            self._set_check(
                self.gamma_check, tr("✓ GAMMA found - new MO2 profile"), "ok"
            )
        elif gamma_dir.is_dir():
            self._set_check(self.gamma_check, tr("Not installed yet"), "info")
        else:
            self._set_check(self.gamma_check, tr("Will be created on install"), "info")

        if not cache:
            self._set_check(self.cache_check, tr("Required"), "bad")
        else:
            free = free_space_bytes(cache)
            self._set_check(
                self.cache_check,
                tr("{size} free", size=human_size(free)) if free is not None else "",
                "info",
            )

    # ----- button state -----
    def _update_busy_state(self) -> None:
        busy = self.window.install_busy or self._task is not None
        editing = self._mode in ("edit", "create")
        saved = self._has_saved()
        self.new_button.setEnabled(not busy)
        self.empty_create_button.setEnabled(not busy)
        self.empty_import_button.setEnabled(not busy)
        for button in (self.active_button, self.edit_button, self.more_button):
            button.setEnabled(not busy and saved)
        self.save_button.setEnabled(
            not busy and (self._mode == "create" or self._is_dirty())
        )
        self.discard_button.setEnabled(not busy and editing)
        for edit in self._text_fields():
            edit.setEnabled(not busy)
        for widget in (self.profile_list, *self._browse_buttons):
            widget.setEnabled(not busy)

    def on_busy_changed(self, _busy: bool) -> None:
        """Disable profile edits while an install-affecting task is running."""
        self._update_busy_state()

    def _busy_guard(self) -> bool:
        if not self.window.install_busy:
            return False
        self._update_busy_state()
        return True

    def _set_buttons_enabled(self, _enabled: bool) -> None:
        # _update_busy_state() already reads self._task, so callers set or
        # clear the task first and this just re-applies the state.
        self._update_busy_state()

    def _show_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))
        if text:
            self._status_timer.start(_STATUS_TIMEOUT_MS)

    # ----- save / create -----
    def _save_or_create(self) -> bool:
        """Save the edited profile, or start creating the new one.

        Returns True when the save went through (or the create started).
        """
        if self._busy_guard() or self._task is not None:
            return False
        name = self.name_edit.text().strip()
        # _form_state is the name of the profile the form was loaded from
        # (empty for "New"). Editing that profile - including renaming it -
        # must update that same entry in place instead of creating a
        # separate profile and leaving the original orphaned.
        editing_existing = bool(self._form_state) and any(
            p.profile_name == self._form_state for p in self.settings.profiles
        )
        # Renaming onto a *different* existing profile (or New keeping a name
        # that already exists) would silently overwrite that profile's data.
        collision = any(
            p.profile_name == name and p.profile_name != self._form_state
            for p in self.settings.profiles
        )
        if collision:
            QMessageBox.warning(
                self,
                tr("Name In Use"),
                tr("A profile named '{name}' already exists. Choose a different name.", name=name),
            )
            return False
        # Two differently-named profiles pointing at the identical
        # Anomaly/GAMMA/cache folders *and* the same MO2 profile would alias
        # the same on-disk MO2 profile - switching "active" between them,
        # editing mods under one, or an incomplete-install warning tracked
        # for one would silently affect the other too. A different
        # mo2_profile is the supported way to run multiple MO2 profiles
        # against one shared Anomaly/GAMMA/cache install (see
        # NewProfileDialog's "existing install" choice), so that
        # combination must not be blocked here.
        candidate = self._form_values()
        path_collision = any(
            p.profile_name != self._form_state
            and p.anomaly == candidate.anomaly
            and p.gamma == candidate.gamma
            and p.cache == candidate.cache
            and p.mo2_profile == candidate.mo2_profile
            for p in self.settings.profiles
        )
        if path_collision:
            QMessageBox.warning(
                self,
                tr("Folders In Use"),
                tr(
                    "Another profile already uses these exact Anomaly/GAMMA/cache folders with the same MO2 profile. Give this one a different MO2 profile or its own install location."
                ),
            )
            return False
        if editing_existing:
            return self._save_profile()
        return self._create_profile()

    def _validate_required(self, profile: CliProfile) -> bool:
        if not profile.profile_name:
            QMessageBox.warning(self, tr("Missing Name"), tr("A profile name is required."))
            self.name_edit.setFocus()
            return False
        if not (profile.anomaly and profile.gamma and profile.cache):
            QMessageBox.warning(
                self,
                tr("Missing folders"),
                tr("Anomaly, GAMMA, and cache folders are required."),
            )
            return False
        try:
            check_cli_values(profile)
        except ValueError as exc:
            QMessageBox.warning(self, tr("Invalid profile"), str(exc))
            return False
        return True

    def _create_profile(self) -> bool:
        profile = self._form_values()
        if not self._validate_required(profile):
            return False
        args = create_profile_args(profile)
        source_mo2 = self._source_mo2
        # "config create" downloads the official modlist.txt into a new MO2
        # profile. A profile based on another one must get that one's list
        # instead - but an MO2 profile that already existed keeps its own.
        replace = bool(
            source_mo2
            and source_mo2 != profile.mo2_profile
            and not (
                Path(profile.gamma) / "profiles" / profile.mo2_profile / "modlist.txt"
            ).exists()
        )
        self._task = BackgroundTask(run_config_command, args, timeout=300, parent=self)
        self._task.result.connect(
            lambda res: self._on_create_done(profile, source_mo2, replace, *res)
        )
        self._task.error.connect(self._on_task_error)
        self._task.start()
        self._set_buttons_enabled(False)
        self._show_status(tr("Creating profile..."))
        return True

    def _on_create_done(
        self,
        profile: CliProfile,
        source_mo2: str | None,
        replace: bool,
        rc: int,
        out: str,
        err: str,
    ) -> None:
        self._task = None
        self._set_buttons_enabled(True)
        self._show_status("")
        if not cli_ok(rc, out, err):
            QMessageBox.warning(
                self,
                tr("Create Failed"),
                (out + "\n" + err).strip() or "config create failed",
            )
            return
        self.window.refresh_settings()
        if not any(
            p.profile_name == profile.profile_name
            for p in self.window.settings.profiles
        ):
            QMessageBox.warning(
                self, tr("Create Failed"), tr("The CLI did not create the profile.")
            )
            return
        try:
            seed_new_mo2_profile(
                profile.gamma,
                profile.mo2_profile,
                source_mo2 or GAMMA_PROFILE,
                replace=replace,
            )
        except OSError:
            # Best-effort: the profile itself was created successfully above,
            # so a failure here (e.g. a read-only gamma folder) must not be
            # reported as the create having failed.
            pass
        self._mode = "view"
        self._form_state = profile.profile_name
        self.refresh(reload_form=True)
        self._show_status(
            tr("Profile '{profile_name}' created and activated.", profile_name=profile.profile_name)
        )

    def _save_profile(self) -> bool:
        profile = self._form_values()
        if not self._validate_required(profile):
            return False
        try:
            save_profile(self.settings, self._form_state, profile)
        except OSError as exc:
            QMessageBox.warning(
                self, tr("Save Failed"), tr("Could not write settings.json:\n{exc}", exc=exc)
            )
            return False
        self._form_state = profile.profile_name
        self.refresh(reload_form=True)
        self._show_status(
            tr("Profile '{profile_name}' saved.", profile_name=profile.profile_name)
        )
        return True

    def _discard(self) -> None:
        if self._mode == "create":
            if not self._confirm_leave():
                return
            self._mode = "view" if self._profile(self._return_to) else "empty"
            self._form_state = self._return_to
            self.refresh(reload_form=True)
            return
        if self._confirm_leave():
            self._show_profile(self._form_state)

    def _on_task_error(self, msg: str) -> None:
        self._task = None
        self._set_buttons_enabled(True)
        self._show_status("")
        QMessageBox.warning(self, tr("Error"), msg)

    # ----- new / import -----
    def _browse(self, edit: QLineEdit) -> None:
        if self._busy_guard():
            return
        path = QFileDialog.getExistingDirectory(self, tr("Select a folder"), edit.text())
        if path:
            edit.setText(path)

    def _open_folder(self, edit: QLineEdit) -> None:
        path = normalize_path(edit.text())
        if path and Path(path).is_dir():
            open_in_file_manager(path)

    def _on_new_clicked(self) -> None:
        if self._busy_guard() or not self._confirm_leave():
            return
        if not self.settings.profiles:
            self._new_profile()
            return
        active = self.settings.active_profile
        dialog = NewProfileDialog(
            self.settings.profiles,
            active.profile_name if active is not None else "",
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        base = self._profile(dialog.base_name())
        if base is None:
            self._new_profile()
        else:
            self._start_new(self._derived_profile(base, name=""), source_mo2=base.mo2_profile)

    def _new_profile(self) -> None:
        """Open a blank "create" form for a fresh install."""
        if self._busy_guard():
            return
        profile = CliProfile()
        profile.profile_name = ""
        profile.anomaly = profile.gamma = profile.cache = ""
        self._start_new(profile)

    def _derived_profile(self, base: CliProfile, *, name: str) -> CliProfile:
        """A copy of ``base`` with its own name and a free MO2 profile name.

        Sharing the install folders is fine; sharing the MO2 profile too
        would make both profiles edit the same mod list.
        """
        taken = set(mo2_profile_names(base.gamma))
        taken.update(p.mo2_profile for p in self.settings.profiles if p.gamma == base.gamma)
        return dataclasses.replace(
            base,
            active=False,
            profile_name=name,
            mo2_profile=unique_name(base.mo2_profile or GAMMA_PROFILE, taken),
            extra={},
        )

    def _start_new(self, profile: CliProfile, *, source_mo2: str | None = None) -> None:
        if self._has_saved():
            self._return_to = self._form_state
        self._mode = "create"
        self._form_state = ""
        self._source_mo2 = source_mo2
        self._select_row_silently("")
        self._load_form(profile)
        self.detail_stack.setCurrentIndex(1)
        self.pages.setCurrentIndex(1)
        self._update_header()
        self.name_edit.setFocus()

    def _selected_profile_name(self) -> str | None:
        """Name of the profile shown in the editor, or None when creating one."""
        if not self._has_saved():
            QMessageBox.information(
                self, tr("No Selection"), tr("Select a profile in the list first.")
            )
            return None
        return self._form_state

    def _export_profile(self) -> None:
        """Save the selected profile's portable settings (+ modlist.txt) to a file.

        Install folder paths are never included - they're machine-specific
        and meaningless to reuse on another machine/after a fresh OS
        install. See ``profile_bundle.py`` for exactly what travels.
        """
        if self._busy_guard():
            return
        name = self._selected_profile_name()
        if name is None:
            return
        profile = self._profile(name)
        if profile is None:
            return
        suggested = f"{name}.commander-profile.zip"
        path_str, _ = QFileDialog.getSaveFileName(
            self, tr("Export Profile"), suggested, tr("COMMANDER Profile Bundle (*.zip)")
        )
        if not path_str:
            return
        try:
            export_profile_bundle(profile, Path(path_str))
        except ProfileBundleError as exc:
            QMessageBox.warning(self, tr("Export Failed"), str(exc))
            return
        self._show_status(tr("Exported to {path}", path=path_str))

    def _import_profile(self) -> None:
        """Pre-fill the New Profile form from a previously exported bundle.

        Install folders are always chosen fresh - never taken from the
        bundle - the same as creating any new profile from scratch. A
        bundled modlist.txt (if present) is extracted next to the bundle
        for the user to copy into place manually once GAMMA is installed
        at the profile's chosen path, rather than being written directly
        into an install location that may not exist yet.
        """
        if self._busy_guard() or not self._confirm_leave():
            return
        path_str, _ = QFileDialog.getOpenFileName(
            self, tr("Import Profile"), "", tr("COMMANDER Profile Bundle (*.zip)")
        )
        if not path_str:
            return
        try:
            bundle = read_profile_bundle(Path(path_str))
        except ProfileBundleError as exc:
            QMessageBox.warning(self, tr("Import Failed"), str(exc))
            return
        imported = CliProfile()
        bundle.apply_to(imported)
        # A bundle is a file someone sends you: never take its download
        # sources, only the official ones.
        reset_sources(imported)
        imported.profile_name = ""
        imported.anomaly = imported.gamma = imported.cache = ""
        self._start_new(imported)
        message = tr(
            "Settings loaded. Choose a name and install folders, then create the profile."
        )
        if bundle.modlist_text is None:
            self._show_status(message)
            return
        modlist_out = Path(path_str).with_suffix("").with_suffix(".modlist.txt")
        # Never overwrite (or write through a symlink at) an existing file.
        counter = 2
        while modlist_out.exists() or modlist_out.is_symlink():
            modlist_out = modlist_out.with_name(
                f"{Path(path_str).with_suffix('').stem}.modlist-{counter}.txt"
            )
            counter += 1
        try:
            # lock=False: this folder (wherever the user saved the
            # bundle) isn't one COMMANDER owns, so no stray lock
            # dotfile is left behind in it.
            atomic.write_bytes(
                modlist_out, bundle.modlist_text.encode("utf-8"), lock=False
            )
            message += "\n\n" + tr(
                "This bundle also included a modlist.txt, extracted to:\n{path}\n"
                "Copy it into <GAMMA>/profiles/<MO2 profile>/modlist.txt after "
                "installing GAMMA to restore its load order.",
                path=str(modlist_out),
            )
        except OSError:
            pass
        QMessageBox.information(self, tr("Profile Imported"), message)

    # ----- activate / delete -----
    def _warn_if_active_profile_running(self, name: str, message: str) -> bool:
        """Ask to continue when MO2/the game is up and ``name`` is active.

        COMMANDER cannot tell which profile a running MO2 actually belongs
        to, so this only fires for the one case it *can* reason about: the
        selected profile is the current active one and MO2 is running.
        Returns True if the caller should proceed.
        """
        active = self.settings.active_profile
        if active is None or active.profile_name != name or not mo2_running():
            return True
        answer = QMessageBox.question(
            self,
            tr("Game Running"),
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _set_active(self) -> None:
        if self._busy_guard() or self._task is not None:
            return
        name = self._selected_profile_name()
        if name is None:
            return
        active = self.settings.active_profile
        if active is not None and active.profile_name == name:
            return
        if active is not None and mo2_running():
            answer = QMessageBox.question(
                self,
                tr("Game Running"),
                tr("Mod Organizer / the game appears to be running under the current active profile ('{active_name}').\n\nSwitching the active profile now will not stop it, but COMMANDER's other pages will stop reflecting its state.\n\nSwitch anyway?", active_name=active.profile_name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self._task is not None or self._busy_guard():
            return

        def _done(success: bool) -> None:
            self._task = None
            self._set_buttons_enabled(True)
            if success:
                self.refresh()
                self._show_status(tr("Profile '{name}' is now active.", name=name))

        self._task = activate_profile(self.window, self, name, on_done=_done)
        self._set_buttons_enabled(False)

    def _delete_profile(self) -> None:
        if self._busy_guard() or self._task is not None:
            return
        name = self._selected_profile_name()
        if name is None:
            return
        profile = self._profile(name)
        folders = ""
        if profile is not None:
            folders = tr(
                "Anomaly: {anomaly}\nGAMMA: {gamma}\nCache: {cache}",
                anomaly=profile.anomaly or "-",
                gamma=profile.gamma or "-",
                cache=profile.cache or "-",
            )
        answer = QMessageBox.question(
            self,
            tr("Delete Profile"),
            tr(
                "Delete profile '{name}'?\n\nOnly the COMMANDER profile is removed. Game files on disk are NOT deleted:\n\n{folders}",
                name=name,
                folders=folders,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self._warn_if_active_profile_running(
            name,
            tr("Mod Organizer / the game appears to be running under this profile.\n\nDeleting it now will not stop it, but COMMANDER will no longer have a profile to show its state under.\n\nDelete anyway?"),
        ):
            return
        if self._task is not None or self._busy_guard():
            return
        self._task = BackgroundTask(
            run_config_command, ["delete", name], timeout=300, parent=self
        )
        self._task.result.connect(lambda res: self._on_delete_done(name, *res))
        self._task.error.connect(self._on_task_error)
        self._task.start()
        self._set_buttons_enabled(False)

    def _on_delete_done(self, name: str, rc: int, out: str, err: str) -> None:
        self._task = None
        self._set_buttons_enabled(True)
        if not cli_ok(rc, out, err):
            QMessageBox.warning(
                self, tr("Failed"), (out + "\n" + err).strip() or "config delete failed"
            )
            return
        self.window.refresh_settings()
        if any(p.profile_name == name for p in self.window.settings.profiles):
            QMessageBox.warning(
                self, tr("Failed"), tr("Profile '{name}' could not be deleted.", name=name)
            )
            return
        forget_profile_extras(name)
        self._note_owner = ""
        self._form_state = ""
        self.refresh(reload_form=True)
        self._show_status(tr("Profile '{name}' deleted.", name=name))
