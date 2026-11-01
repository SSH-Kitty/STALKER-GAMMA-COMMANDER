"""
Install page: one card that walks through the five install steps
(folder, dependencies, Anomaly, GAMMA, verify) with a clickable step
track on top and the picked step's page under it.
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..cli_runner import cli_command, cli_resume_args
from ..config import logs_dir
from ..dependencies import check_all_dependencies
from ..game_backup import (
    apply_pending_settings_restore,
    backup_settings_before,
    prepare_anomaly_reinstall,
)
from ..game_installs import find_install, fix_windows_paths, is_gamma, mo2_profile_for, windows_paths
from ..gui_settings import configured_runner, configured_wine_prefix
from ..integrity import (
    MANIFEST_FILENAME,
    CacheArchiveVerifyResult,
    Finding,
    RepoRepairResult,
    anomaly_status,
    build_findings,
    compare_repo_files,
    fetch_official_mod_names,
    format_size,
    gamma_repo_files,
    invalidate_baseline,
    is_expected_gamma_overlay_corrupt,
    overall_verdict,
    repo_branches,
    restore_gamma_overlay,
    restore_repo_files,
    reverted_gamma_overlay,
    scan_mods_md5,
    verify_cache_archives,
    verify_gamma,
    write_baseline,
)
from ..keep_state import capture, discard, restore
from ..launcher import LaunchError
from ..modlist import modlist_path_for
from ..parsers import ProgressEvent, parse_progress_line, strip_ansi
from ..repair import (
    RESUME_FILENAME,
    classify_problems,
    expected_archive_md5s,
    fetch_modpack_records,
    purge_quarantine,
    quarantine_mod_and_archive,
    remove_resume_file,
    repair_preview,
    restore_from_quarantine,
    restore_modlist_after_repair,
    resume_keys,
    settle_quarantine,
    snapshot_modlist,
    write_reextract_resume,
)
from ..settings import cli_ok
from ..themes import active_theme_tokens, token_color
from ..winetricks import (
    WINETRICKS_VERBS,
    check_winetricks_full_status,
    forget_verbs,
    umu_binary,
    umu_install_command,
    winetricks_binary,
    winetricks_install_command,
    winetricks_tool_install_command,
)
from . import reflow
from .common import (
    ACCENT,
    OK_GREEN,
    STATUS_RED,
    BackgroundTask,
    CommandRunner,
    InstallStatusRow,
    ProgressArea,
    StatusDot,
    StreamTask,
    anomaly_installed,
    clear_layout,
    count_active_mods,
    count_cached_archives,
    disk_usage_bytes,
    display_state,
    free_space_bytes,
    game_running,
    gamma_installed,
    human_size,
    info_label,
    make_card,
    make_header_row,
    mo2_running,
    normalize_path,
    notify_desktop,
    paused_dependency_status,
    save_profile_dirs,
    section_label,
    tr,
    use_stock_mo2_profile_if_missing,
    warn_wine_unsafe_folder,
    wine_unsafe_folder,
    winetricks_tooltip,
)
from .dashboard import _SEGMENT_TOKENS, _SegmentedBar, _Swatch
from .dialogs import MessageBox as QMessageBox
from .dialogs import ProgressDialog, add_close_button, style_window
from .multiplayer_page import confirm_singleplayer

#: tr() msgid shared between the Install button's own label and the failure
#: popup's resume hint, so the two can't drift apart if this ever gets renamed.
_RESUME_BUTTON_LABEL = "Resume GAMMA Installation"

#: How many times auto-retry (see the "auto_retry_large_files"
#: checkbox) will automatically restart a failed install before giving up
#: and showing the normal failure popup - a cap so a persistently broken
#: connection can't retry forever unattended.
_AUTO_RETRY_MAX = 10

_CHECKBOXES = [
    *(
        (
            "auto_retry_large_files",
            "Auto-retry on large file pack failures",
            "Automatically retry if a large download fails, up to 10 times.",
        ),
        (
            "preserve_user",
            "Preserve user.ltx settings",
            "Keep your existing game settings (user.ltx) instead of resetting them.",
        ),
        (
            "preserve_mcm",
            "Preserve MCM settings",
            "Keep your existing MCM (mod menu) settings instead of resetting them.",
        ),
        (
            "minimal",
            "Minimal (~100 GB)",
            "Delete addon archives after extraction to save ~50 GB of disk space.",
        ),
        (
            "keep_engine_exes",
            "Keep my current engine executables",
            "Keep your current Anomaly executables instead of GAMMA's bundled ones.",
        ),
        (
            "redownload_github",
            "Re-download GitHub addons (get latest)",
            "Get the newest code for every GitHub addon. Off reuses cached copies.",
        ),
        (
            "offline",
            "Offline install (cache only)",
            "Install from a cache already in the cache folder. Nothing is downloaded.",
        ),
    )
]

#: Where players download the offline cache archive and its two list files.
OFFLINE_GUIDE_URL = "https://github.com/FaithBeam/stalker-gamma-cli/wiki/Offline-Install"

#: Checkboxes an offline install greys out: Minimal would delete the cache
#: the offline install depends on, and the other two only affect downloads.
_OFFLINE_DISABLES = ("minimal", "redownload_github", "auto_retry_large_files")

#: Checkboxes above (by key) that start checked on a fresh InstallPage -
#: none of these are persisted anywhere (see gui_settings.py), so this is
#: only the initial state each time the page is constructed, same as
#: every other checkbox here; the user can still uncheck for the current
#: session.
_DEFAULT_CHECKED_OPTIONS = {
    "auto_retry_large_files", "auto_continue_gamma", "anomaly_mirror",
    "check_mods", "check_anomaly", "check_cache",
}

_ANOMALY_CHECKBOXES = [
    (
        "verify_after_install",
        "Check Anomaly files after install",
        "Run an integrity check on Anomaly after it finishes installing.",
    ),
    (
        "auto_continue_gamma",
        "Automatically install GAMMA after Anomaly",
        "Start the GAMMA install as soon as Anomaly finishes.",
    ),
    (
        "anomaly_mirror",
        "Faster Anomaly download (mirror)",
        "Faster download from the GAMMA installer author's mirror. Needs 5 GB free RAM.",
    ),
]

_VERIFY_CHECKBOXES = [
    (
        "check_mods",
        "GAMMA mods and mod files",
        "Always checked: the repair is worked out from this part.",
    ),
    (
        "check_anomaly",
        "Anomaly files",
        "Compare every Anomaly file with Anomaly's own list of original checksums.",
    ),
    (
        "check_cache",
        "Download cache",
        "Check cached archives so Repair can reuse them.",
    ),
    (
        "repair_prompt",
        "Show the repair list right away",
        "Show what Repair would fix when problems are found.",
    ),
    (
        "verify_after_gamma",
        "Check after a GAMMA install",
        "Run Verify Integrity as soon as a GAMMA install or reinstall finishes.",
    ),
]

#: The step track's names, in order: one page each on the Install card.
_STEP_NAMES = ("Folder", "Dependencies", "Anomaly", "GAMMA", "Verify")

#: How each step's options are grouped, by key, in two columns of groups
#: (two fit beside each other down to a small window; three stacked).
_ANOMALY_OPTION_GROUPS = (
    (("Install", ("verify_after_install", "anomaly_mirror")),),
    (("After install", ("auto_continue_gamma",)),),
)
_GAMMA_OPTION_GROUPS = (
    (
        ("Install", ("auto_retry_large_files", "minimal", "offline")),
        ("Addons", ("redownload_github",)),
    ),
    (("Keep my settings", ("preserve_user", "preserve_mcm", "keep_engine_exes")),),
)
_VERIFY_OPTION_GROUPS = (
    (("What to check", ("check_mods", "check_anomaly", "check_cache")),),
    (("Automatic", ("repair_prompt", "verify_after_gamma")),),
)

#: Shown on the Anomaly step while Offline install is ticked on the GAMMA step.
_ANOMALY_OFFLINE_NOTE = "Offline install is on. Anomaly unpacks from the cache in step 4."

#: What the dependency check covers, as (group title, keys): the
#: winetricks verbs, then the tools that run the installers. The first
#: group is one column, the rest share the second.
_DEPENDENCY_GROUPS = (
    ("DirectX", ("d3dcompiler_43", "d3dcompiler_47", "d3dx9", "d3dx10", "d3dx11_43", "dx8vb")),
    ("Other runtimes", ("vcrun2022", "quartz")),
    ("Tools", ("umu", "wine")),
)

#: Free space a fresh Anomaly + GAMMA + cache install needs (see step 1).
_INSTALL_SPACE_BYTES = 150 * 1024**3

#: Length of a step change on the Install card: fade out, then back in.
_STEP_FADE_MS = 260

#: Verify pill level -> step track state.
_VERIFY_STEP_STATES = {
    "healthy": "ok", "snapshot": "ok", "running": "busy", "attention": "warn", "failed": "bad",
}

_WT_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3})\s*%")


def _winetricks_progress(line: str, stage: str, completed: set[str]) -> int | None:
    """Extract a Winetricks percentage or approximate verb-stage progress."""
    clean = line.strip()
    match = _WT_PERCENT_RE.search(clean)
    if match:
        return min(100, int(match.group(1)))
    if stage != "verbs":
        return None
    for index, verb in enumerate(WINETRICKS_VERBS, start=1):
        if verb in clean and verb not in completed:
            completed.add(verb)
            return round(index / len(WINETRICKS_VERBS) * 100)
    return None


# Overall-bar ranges for each dependency-install stage, mirroring the
# determinate staged style used for Anomaly: the winetricks tool itself
# first (if missing), then umu, then the winetricks verbs fill the
# remainder.
_WT_STAGE_RANGES = {
    "winetricks": (0, 10),
    "umu": (10, 40),
    "verbs": (40, 100),
}


def _dependencies_progress(stage: str, pct: int | None) -> int | None:
    """Map a within-stage percentage onto the overall dependency bar."""
    if pct is None:
        return None
    start, end = _WT_STAGE_RANGES.get(stage, (0, 100))
    clamped = max(0, min(100, pct))
    return round(start + (end - start) * clamped / 100)


def _full_install_args(
    minimal: bool,
    preserve_user: bool,
    preserve_mcm: bool,
    skip_extract_on_hash_match: bool = False,
    skip_github_downloads: bool = True,
    offline_lists: tuple[str, str] | None = None,
    anomaly_mirror: bool = False,
) -> list[str]:
    """Build the full-install argv.

    ``offline_lists`` is ``(modpack_maker_list.txt, modlist.txt)``: install
    only from the pre-extracted cache. Both paths are needed - ``--offline``
    alone still downloads the two lists.

    ``--skip-github-downloads`` reuses a cached GitHub addon archive instead
    of downloading it again: those archives have no MD5 in the modpack list,
    so without it the CLI re-fetches every one on every run. A missing
    archive is still downloaded.

    ``--skip-extract-on-hash-match`` makes the CLI skip re-extracting any
    archive it didn't need to (re-)download because a cached copy already
    matched the expected MD5 - it has no idea whether the *extraction
    destination* (``gamma/mods/<name>/``) currently has anything in it.
    Passing this after ``gamma/mods`` was wiped (e.g. by GAMMA Reset) but
    the download cache was not is NOT lossless: every already-cached,
    unchanged mod would be silently skipped instead of re-extracted, never
    reappearing. Callers must only pass ``True`` when GAMMA's own install
    is already known to genuinely exist (see ``gamma_installed()``), not
    just because Anomaly is present.

    ``--use-anomaly-mirror`` (CLI 1.40+) only matters when the Anomaly
    archive is downloaded, so an offline install leaves it out.
    """
    args = ["full-install"]
    if minimal:
        args.append("--minimal")
    if preserve_user:
        args.append("--preserve-user-settings")
    if preserve_mcm:
        args.append("--preserve-mcm-settings")
    if skip_extract_on_hash_match:
        args.append("--skip-extract-on-hash-match")
    if skip_github_downloads:
        args.append("--skip-github-downloads")
    if offline_lists:
        modpack_list, mod_list = offline_lists
        args += ["--offline", "--mod-pack-maker-path", modpack_list, "--mod-list-path", mod_list]
    elif anomaly_mirror:
        args.append("--use-anomaly-mirror")
    return args


def offline_list_paths(cache: str) -> tuple[str, str]:
    """The (modpack_maker_list.txt, modlist.txt) for an offline install.

    The cache archive holds a ``cache/`` folder with both lists beside it, so
    extracting it into the cache folder's parent leaves them there. A saved
    choice that still exists wins; otherwise they are looked for beside (or
    in) the cache folder. No fallback to the profile's own copies: the lists
    must match the cache snapshot. Empty string where nothing is found.
    """
    saved = gui_settings.load_gui_settings()
    cache_dir = Path(cache).expanduser() if cache else None
    result = []
    for key, name in (
        ("offline_modpack_list", "modpack_maker_list.txt"),
        ("offline_mod_list", "modlist.txt"),
    ):
        found = saved[key] if Path(saved[key]).is_file() else ""
        if not found and cache_dir is not None:
            found = next(
                (str(c) for c in (cache_dir.parent / name, cache_dir / name) if c.is_file()),
                "",
            )
        result.append(found)
    return (result[0], result[1])


def offline_install_problem(lists: tuple[str, str], cache: str) -> str | None:
    """Why an offline install can't start yet, or None if it can."""
    if not all(Path(path).is_file() for path in lists):
        return tr("Pick both list files for the offline install: modpack_maker_list.txt and modlist.txt.")
    if count_cached_archives(cache) == 0:
        nested = str(Path(cache).expanduser() / "cache")
        if count_cached_archives(nested):
            # The archive has its own top-level cache/ folder: extracted
            # into the cache folder itself, it ends up one level too deep.
            return tr(
                "The archives ended up in {nested}. Move them up into {cache}, or extract the archive into {parent} instead.",
                nested=nested,
                cache=cache,
                parent=str(Path(cache).expanduser().parent),
            )
        return tr(
            "No archives found in {cache}. Extract the offline cache archive into {parent} first.",
            cache=cache,
            parent=str(Path(cache).expanduser().parent),
        )
    return None


def _verify_phase_value(start: int, end: int, fraction: float) -> int:
    """Map a 0-1 fraction onto a phase range of the overall verify bar."""
    fraction = max(0.0, min(1.0, fraction))
    return round(start + (end - start) * fraction)


# Overall-bar ranges for the Verify Integrity phases.
_VERIFY_PHASE = {
    "presence": (10, 15),
    "md5": (15, 92),
    "cache": (92, 100),
}
_PRESENCE_RE = re.compile(r"Checking GAMMA mod (\d+)/(\d+)")
_MD5_RE = re.compile(r"MD5 hashing (\d+)/(\d+)")
_CACHE_RE = re.compile(r"Checking cached archive (\d+)/(\d+)")


def _anomaly_tally(counts: dict[str, int]) -> str:
    """Log line for an Anomaly check. Plain words: the assistant's log
    analyzer reads the status keywords as real damage reports."""
    return (
        f"Anomaly: {counts['OK']} files match, {counts['CORRUPT']} differ "
        f"from the originals, {counts['NOT FOUND']} missing"
    )


def _remaining_problems(post, presence, repo_owned=()) -> int:
    """Problems left after a repair. New files are expected then: the
    reinstalled mods' own, absent from a snapshot taken while broken.
    GAMMA's own files (``repo_owned``) are judged by their repositories."""
    owned = set(repo_owned)
    count = len(set(post.changed) - owned) + len(set(post.removed) - owned) + len(post.errors)
    return count + (presence.problems if presence is not None else 0)


def _check_gamma_overlay(anomaly: str, cache: str):
    """(restored, reason, still reverted) for GAMMA's engine files after an
    Anomaly repair - see integrity.reverted_gamma_overlay."""
    reverted = reverted_gamma_overlay(anomaly)
    if not reverted:
        return [], "", []
    fixed = restore_gamma_overlay(anomaly, cache, reverted)
    return fixed.restored, fixed.reason, reverted_gamma_overlay(anomaly)

_GAMMA_NOT_INSTALLED = "__gamma_not_installed__"

VERIFY_INTEGRITY_DISABLED = False

#: gui_settings key holding the last finished Verify Integrity result, per
#: GAMMA folder, so the card still shows it after a restart.
_VERIFY_LAST_KEY = "verify_last"

#: Rows shown when a finding's list is expanded; the full list is in the log.
_FINDING_ITEMS_SHOWN = 200

_STATE_ICONS = {
    "idle": "•",
    "running": "…",
    "ok": "✓",
    "info": "i",
    "warn": "!",
    "problem": "✕",
}


def _repolish(*widgets: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    for widget in widgets:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def _age_text(seconds: float) -> str:
    """How long ago a check ran, in the pill's short form."""
    minutes = int(max(0, seconds) // 60)
    if minutes < 1:
        return tr("just now")
    if minutes < 60:
        return tr("{count} min ago", count=minutes)
    if minutes < 48 * 60:
        return tr("{count} h ago", count=minutes // 60)
    return tr("{count} d ago", count=minutes // (24 * 60))


class _PathEdit(QLineEdit):
    """A folder box that opens the folder picker when clicked."""

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("pathEdit")
        self.setReadOnly(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class _ChoiceIcon(QWidget):
    """The big line icon on an Automatic / Manual choice, drawn in the
    accent like the tiles' state badges: a download into a tray, three
    sliders, a window copied into a folder, or two folders swapping."""

    def __init__(self, kind: str, size: int = 110, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(size, size)

    def paintEvent(self, _event) -> None:
        ink = token_color("accent_strong", "#8fe45c")
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Drawn on a 110 px grid, scaled to the icon's size; the lines
            # keep the big icons' thickness.
            grid = 110 / self.width()
            painter.scale(1 / grid, 1 / grid)
            painter.setPen(QPen(ink, 4 * grid, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if self.kind == "auto":
                painter.drawLine(QPointF(55, 14), QPointF(55, 66))
                head = QPainterPath(QPointF(35, 47))
                head.lineTo(55, 67)
                head.lineTo(75, 47)
                painter.drawPath(head)
                tray = QPainterPath(QPointF(16, 66))
                tray.lineTo(16, 92)
                tray.lineTo(94, 92)
                tray.lineTo(94, 66)
                painter.drawPath(tray)
                painter.setPen(QPen(ink, 3 * grid, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawLine(QPointF(86, 10), QPointF(86, 26))
                painter.drawLine(QPointF(78, 18), QPointF(94, 18))
            elif self.kind == "windows":
                painter.drawRoundedRect(QRectF(10, 12, 60, 44), 4, 4)
                painter.drawLine(QPointF(10, 25), QPointF(70, 25))
                painter.drawPath(_folder_path(48, 66, 52, 34))
                arrow = QPainterPath(QPointF(28, 62))
                arrow.lineTo(28, 86)
                arrow.lineTo(40, 86)
                painter.drawPath(arrow)
                head = QPainterPath(QPointF(34, 79))
                head.lineTo(41, 86)
                head.lineTo(34, 93)
                painter.drawPath(head)
            elif self.kind == "switch":
                painter.drawPath(_folder_path(8, 42, 36, 28))
                painter.drawPath(_folder_path(66, 42, 36, 28))
                for start, ctrl, end, tip in (
                    ((26, 34), (55, 2), (84, 34), (84, 26)),
                    ((84, 78), (55, 108), (26, 78), (26, 86)),
                ):
                    arc = QPainterPath(QPointF(*start))
                    arc.quadTo(QPointF(*ctrl), QPointF(*end))
                    painter.drawPath(arc)
                    side = 7 if tip[1] < end[1] else -7
                    head = QPainterPath(QPointF(end[0] - 7, end[1] - side))
                    head.lineTo(*end)
                    head.lineTo(end[0] + 7, end[1] - side)
                    painter.drawPath(head)
            else:
                for x, knob in ((26, 66), (55, 34), (84, 56)):
                    painter.drawLine(QPointF(x, 14), QPointF(x, knob - 9))
                    painter.drawLine(QPointF(x, knob + 9), QPointF(x, 96))
                    painter.drawEllipse(QPointF(x, knob), 9, 9)
        finally:
            painter.end()


def _folder_path(x: float, y: float, w: float, h: float) -> QPainterPath:
    """A folder outline with its tab on the top left."""
    path = QPainterPath(QPointF(x, y))
    path.lineTo(x + w * 0.38, y)
    path.lineTo(x + w * 0.5, y + h * 0.22)
    path.lineTo(x + w, y + h * 0.22)
    path.lineTo(x + w, y + h)
    path.lineTo(x, y + h)
    path.closeSubpath()
    return path


class _InstallChoice(QFrame):
    """One half of the Automatic / Manual card: title, intro, icon and
    tiles, all one click target (or Enter/Space with keyboard focus)."""

    clicked = Signal()

    def __init__(self, kind: str, title: str, tag: str, intro: str, tiles, parent: QWidget | None = None, *, icon_size: int = 110) -> None:
        super().__init__(parent)
        self.setObjectName("installChoice")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(section_label(title, level=2))
        head.addStretch(1)
        self.tag = QLabel(tag)
        self.tag.setObjectName("installChoiceTag")
        head.addWidget(self.tag)
        layout.addLayout(head)
        layout.addWidget(_lead_label(intro))
        layout.addWidget(_ChoiceIcon(kind, icon_size), 0, Qt.AlignmentFlag.AlignHCenter)
        self.tiles = []
        for value, name, caption in tiles:
            tile = _CheckTile(name)
            tile.set_state("ok", value, caption)
            self.tiles.append(tile)
        layout.addLayout(_tile_row(self.tiles))
        layout.addStretch(1)
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_available(self, available: bool, tag: str, tooltip: str = "") -> None:
        """Offer this choice, or show it dimmed and unclickable."""
        self.setEnabled(available)
        self.tag.setText(tag)
        self.setToolTip(tooltip)
        if available:
            self.setGraphicsEffect(None)
        else:
            dim = QGraphicsOpacityEffect(self)
            dim.setOpacity(0.45)
            self.setGraphicsEffect(dim)
            self._set_hover(False)

    def _set_hover(self, hover: bool) -> None:
        self.setProperty("hover", hover and self.isEnabled())
        _repolish(self)

    def enterEvent(self, event) -> None:
        self._set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hover(False)
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        self._set_hover(True)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._set_hover(self.underMouse())
        super().focusOutEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class _WizardStep(QPushButton):
    """One step on the Install card's track: number and name, with the
    step's status under it. Its ``state`` property (ok, missing, busy,
    warn, bad, pending) colours the status in themes.py. The picked step
    gets the main nav bar's rounded accent underline, hugging the text."""

    def __init__(self, number: int, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("wizardStep")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self._name = QLabel(f"{number} · {name}")
        self._name.setObjectName("wizardStepName")
        self._status = QLabel()
        self._status.setObjectName("wizardStepStatus")
        for label in (self._name, self._status):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            # May be cut short in a narrow window, rather than widening the
            # page past it.
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout.addWidget(label)
        self.state = "pending"
        self.setAccessibleName(name)

    def sizeHint(self):
        return self.layout().sizeHint()

    def minimumSizeHint(self):
        return QSize(0, self.layout().minimumSize().height())

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.isChecked():
            return
        width = max(
            label.fontMetrics().horizontalAdvance(label.text())
            for label in (self._name, self._status)
        )
        left = self.layout().contentsMargins().left()
        width = min(width, self.width() - 2 * left)
        if width <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(token_color("accent_strong", "#9fe96f"))
        pen.setWidth(3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        y = self.height() - 3
        painter.drawLine(QPointF(left, y), QPointF(left + width, y))
        painter.end()

    def set_status(self, state: str, text: str) -> None:
        self.state = state
        self._status.setText(text)
        self.setToolTip(text)
        for widget in (self, self._status):
            widget.setProperty("state", state)
        _repolish(self, self._status)

    def set_selected(self, selected: bool) -> None:
        self.setChecked(selected)
        self._name.setProperty("selected", selected)
        _repolish(self, self._name)


class _StateIcon(QWidget):
    """A tile's state badge, painted so it stays crisp at any font size:
    a ring with a drawn tick, cross, ! or i, and a turning arc while running."""

    #: state -> theme token for the ring and mark.
    _COLORS: ClassVar[dict[str, str]] = {
        "idle": "text_dim",
        "running": "accent_strong",
        "ok": "accent_strong",
        "info": "text_info",
        "warn": "warn",
        "problem": "danger_border",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.state = "idle"
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._spin)

    def set_state(self, state: str) -> None:
        self.state = state
        if state == "running":
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _spin(self) -> None:
        self._angle = (self._angle - 12) % 360
        self.update()

    def paintEvent(self, _event) -> None:
        tokens = active_theme_tokens()
        color = QColor(tokens.get(self._COLORS.get(self.state, "border_strong"), "#7f8f78"))
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)
            pen = QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            if self.state == "running":
                faint = QColor(color)
                faint.setAlpha(60)
                painter.setPen(QPen(faint, 1.6))
                painter.drawEllipse(rect)
                painter.setPen(QPen(color, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawArc(rect, self._angle * 16, 100 * 16)
                return
            if self.state == "idle":
                pen.setStyle(Qt.PenStyle.DotLine)
                painter.setPen(pen)
                painter.drawEllipse(rect)
                return
            if self.state in ("ok", "problem"):
                # Solid badge with a dark mark, like text on a primary button.
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawEllipse(rect)
                pen.setColor(QColor(tokens.get("accent_text", "#0c130a")))
                pen.setWidthF(2.0)
            else:
                painter.setPen(pen)
                painter.drawEllipse(rect)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            c = rect.center()
            if self.state == "ok":
                path = QPainterPath(QPointF(c.x() - 3.6, c.y() + 0.2))
                path.lineTo(c.x() - 1.0, c.y() + 2.8)
                path.lineTo(c.x() + 3.8, c.y() - 2.4)
                painter.drawPath(path)
            elif self.state == "problem":
                painter.drawLine(QPointF(c.x() - 3, c.y() - 3), QPointF(c.x() + 3, c.y() + 3))
                painter.drawLine(QPointF(c.x() + 3, c.y() - 3), QPointF(c.x() - 3, c.y() + 3))
            else:
                # "!" for warn, "i" for info: a bar and a dot, flipped.
                top, bottom = (-4.0, 0.8) if self.state == "warn" else (-0.8, 4.0)
                dot = 3.4 if self.state == "warn" else -3.4
                painter.drawLine(QPointF(c.x(), c.y() + top), QPointF(c.x(), c.y() + bottom))
                painter.drawPoint(QPointF(c.x(), c.y() + dot))
        finally:
            painter.end()


class _CheckTile(QFrame):
    """One of the four check tiles on the Verify Integrity card.

    Laid out like the Dashboard's stats: centred value over a caption,
    straight on the card, with dividers between tiles.
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("verifyTile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.icon = _StateIcon()
        self.title = QLabel(title)
        self.title.setObjectName("verifyTileTitle")
        top.addStretch(1)
        top.addWidget(self.icon)
        top.addWidget(self.title)
        top.addStretch(1)
        layout.addLayout(top)
        self.value = QLabel()
        self.value.setObjectName("verifyTileValue")
        self.value.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.value)
        self.caption = QLabel()
        self.caption.setObjectName("verifyTileCaption")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        layout.addStretch(1)
        self.set_state("idle", "-", tr("Not checked yet"))

    def set_state(self, state: str, value: str, caption: str) -> None:
        self.state = state
        self.icon.set_state(state)
        # An em dash reads as "no number yet"; a bold hyphen looked like a glitch.
        self.value.setText("—" if value == "-" else value)
        self.caption.setText(caption)
        for widget in (self, self.value):
            widget.setProperty("state", state)
        _repolish(self, self.value)

    def snapshot(self) -> list[str]:
        return [self.state, self.value.text(), self.caption.text()]


def _lead_label(template: str) -> QLabel:
    """An intro line whose first words (the ``{arg}`` span) are green in
    every theme. Rich text keeps the colour it was given, so
    InstallPage.on_theme_changed fills it in again."""
    label = info_label(template.replace("{arg}", OK_GREEN.name()))
    label.setProperty("leadTemplate", template)
    return label


def _step_intro(text: str, status: QWidget | None = None) -> QHBoxLayout:
    """A step page's opening line ("Step 1. ..."), with the step's status
    badge, if any, at its right; the step track already names the step."""
    row = QHBoxLayout()
    row.addWidget(_lead_label(text), 1)
    if status is not None:
        row.addWidget(status, 0, Qt.AlignmentFlag.AlignTop)
    return row


def _folder_tile(tile: _CheckTile, path: str) -> None:
    """Ready or Missing, with the folder's path under it."""
    if not path:
        tile.set_state("idle", "-", tr("Not set"))
    elif Path(path).expanduser().is_dir():
        tile.set_state("ok", tr("Ready"), path)
    else:
        tile.set_state("warn", tr("Missing"), path)


def _space_tile(tile: _CheckTile, path: str) -> None:
    """Free space on the drive that holds (or will hold) ``path``."""
    free = free_space_bytes(path) if path else None
    if free is None:
        tile.set_state("idle", "-", tr("Choose a folder first"))
    else:
        tile.set_state("ok", human_size(free), tr("free on this drive"))


class _DependencyRow(QWidget):
    """One runtime or tool on the Dependencies step: the tiles' state
    badge (tick, cross, or empty ring before the check) and its name."""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panelTransparent")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)  # a checkbox row's height
        layout.setSpacing(10)
        self.icon = _StateIcon()
        layout.addWidget(self.icon)
        layout.addWidget(QLabel(name), 1)

    def set_state(self, state: str) -> None:
        self.icon.set_state(state)


def _tile_row(tiles) -> QGridLayout:
    """Check tiles side by side with dividers between, as on the Verify
    step; each side goes under the one before when the window is narrow."""
    grid = reflow.divided(QGridLayout())
    grid.setSpacing(10)
    for index, tile in enumerate(tiles):
        if index:
            divider = QFrame()
            divider.setObjectName("vDivider")
            divider.setFixedWidth(1)
            grid.addWidget(divider, 0, index * 2 - 1)
        grid.addWidget(tile, 0, index * 2)
        grid.setColumnStretch(index * 2, 1)
    return grid


class _FindingRow(QFrame):
    """One plain-language result (see integrity.Finding) with its list."""

    def __init__(self, finding, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("verifyFinding")
        self.setProperty("level", finding.level)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)
        icon = QLabel(_STATE_ICONS.get(finding.level, "i"))
        icon.setObjectName("verifyFindingIcon")
        icon.setFixedWidth(16)
        icon.setProperty("level", finding.level)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.addWidget(icon)
        body = QVBoxLayout()
        body.setSpacing(3)
        title = QLabel(html.escape(finding.title))
        title.setObjectName("verifyFindingTitle")
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setWordWrap(True)
        if finding.count:
            # Inline, so the count stays at the start of the title as it wraps.
            tokens = active_theme_tokens()
            color = {"warn": tokens["warn"], "problem": tokens["danger_border"]}.get(
                finding.level, tokens["text_bright"]
            )
            title.setText(
                f"<span style='color: {color};'>{finding.count:,}</span>&nbsp;&nbsp;{title.text()}"
            )
        head = QHBoxLayout()
        head.addWidget(title, 1)
        body.addLayout(head)
        if finding.detail:
            detail = QLabel(finding.detail)
            detail.setObjectName("verifyFindingDetail")
            detail.setWordWrap(True)
            body.addWidget(detail)
        self.items = None
        self.toggle = None
        if finding.items:
            shown = finding.items[:_FINDING_ITEMS_SHOWN]
            text = "\n".join(shown)
            if len(finding.items) > len(shown):
                text += "\n" + tr(
                    "and {count} more (see the technical log)",
                    count=len(finding.items) - len(shown),
                )
            self.items = QLabel(text)
            self.items.setObjectName("verifyFindingItems")
            self.items.setWordWrap(True)
            self.items.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.items.hide()
            self.toggle = QPushButton(tr("Show list"))
            self.toggle.setObjectName("consoleToggle")
            self.toggle.setFixedSize(110, 26)
            self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
            self.toggle.clicked.connect(self._toggle_items)
            body.addSpacing(4)
            body.addWidget(self.toggle)
            body.addWidget(self.items)
        outer.addLayout(body, 1)
        _repolish(self, icon)

    def _toggle_items(self) -> None:
        visible = not self.items.isVisible()
        self.items.setVisible(visible)
        self.toggle.setText(tr("Hide list") if visible else tr("Show list"))


def _resume_state_matches(state: object, profile) -> bool:
    """Return whether a saved failed install belongs to the active profile.

    Matches on the install's actual location (anomaly/gamma/cache paths)
    only, not ``profile_name`` - a profile name is just a renameable label,
    not a stable identity (renaming a profile mid-failure would otherwise
    make its "incomplete" warning silently vanish even though the exact
    same on-disk install is still just as broken).
    """
    if not isinstance(state, dict):
        return False
    return all(
        state.get(key) == getattr(profile, attr)
        for key, attr in (
            ("anomaly", "anomaly"),
            ("gamma", "gamma"),
            ("cache", "cache"),
        )
    )


def _gamma_verify_gate(gamma_installed_flag: bool) -> str | None:
    """Return the skip sentinel when GAMMA is absent, else ``None``."""
    return None if gamma_installed_flag else _GAMMA_NOT_INSTALLED


def _repair_install_args() -> list[str]:
    """Argv for the post-scan repair reinstall.

    Preservation flags are mandatory here: repairs must never touch
    user.ltx or MCM settings.
    """
    return [
        "full-install",
        "--skip-extract-on-hash-match",
        "--skip-github-downloads",
        "--preserve-user-settings",
        "--preserve-mcm-settings",
    ]


def _anomaly_check_ran(rc: int, output: str, parsed: int) -> bool:
    """Whether ``anomaly check`` ran to the end.

    CLI 1.40+ exits 1 when any file is CORRUPT or NOT FOUND, and GAMMA's
    own overlay files always are. With status lines printed, exit code 1
    is that verdict, not a failed check.
    """
    return cli_ok(0 if rc == 1 and parsed else rc, output, "")


def _note_md5_redownload(tracked: set[str], event: ProgressEvent) -> str | None:
    """Track pending MD5 checks and flag an unexpected re-download.

    The CLI verifies a cached archive (``Check MD5``) and, when the hash
    matches, proceeds to ``Extract`` without re-downloading. If a
    ``Download`` for the same archive follows the check instead, the cached
    file failed verification - surface that so the user understands why the
    installer is fetching it again. Returns a status message to show, or
    ``None``.
    """
    if event.operation == "Check MD5":
        tracked.add(event.name)
        return None
    if event.operation == "Download" and event.name in tracked:
        tracked.discard(event.name)
        return "Cached archive failed verification - re-downloading."
    if event.operation in ("Extract", "Expand") and event.name in tracked:
        tracked.discard(event.name)
    return None


_NETWORK_FAILURE_MARKERS = (
    "ssl error",
    "error cloning repo",
    "gitutilityexception",
    "libgit2sharp",
    "connection reset",
    "connection refused",
    "could not resolve host",
    "name or service not known",
    "temporary failure in name resolution",
    "network is unreachable",
    "the operation has timed out",
    "timed out",
    "unable to connect to the remote server",
)


#: stalker-gamma CLI 1.37+ aborts the whole addon batch with this when moddb
#: serves a Cloudflare "Just a moment..." page instead of the download.
_CLOUDFLARE_MARKER = "cloudflare challenge detected"


def _looks_like_cloudflare_challenge(output: str) -> bool:
    return _CLOUDFLARE_MARKER in (output or "").lower()


def _looks_like_network_failure(output: str) -> bool:
    """True if failed CLI output matches a known download/network failure signature.

    Covers git-clone/SSL failures (LibGit2Sharp) and general .NET HTTP
    timeout/DNS failures seen from GitHub/moddb downloads during install -
    the overwhelming majority of real install failures, which otherwise
    surface as an opaque exit code and a raw .NET stack trace with no
    indication it was just a network hiccup.
    """
    text = (output or "").lower()
    return any(marker in text for marker in _NETWORK_FAILURE_MARKERS)


_SPECIAL_REPO_CLONE_MARKERS = (
    "specialrepoexception",
    "gitutilityexception",
    "libgit2sharpexception",
)


def _looks_like_special_repo_clone_failure(output: str) -> bool:
    """True if the failure is specifically one of the large git-cloned
    "special repos" (Stalker_GAMMA, gamma_setup, gamma_large_files_v2,
    teivaz_anomaly_gunslinger) - confirmed via a real captured log and
    the CLI's own --help that these have no incremental resume: a dropped
    connection partway
    through one of these large, slow clones means redoing that whole
    clone on the next attempt, "fresh" or "Resume" alike - unlike regular
    mod archives, which skip re-downloading once verified by hash.
    """
    text = (output or "").lower()
    return any(marker in text for marker in _SPECIAL_REPO_CLONE_MARKERS)


_GAMMA_LARGE_FILES_MARKER = "gamma large files repo"


def _looks_like_gamma_large_files_failure(output: str) -> bool:
    """True only when the failure is specifically the gamma_large_files_v2

    clone - the CLI's own output names it explicitly ("Error downloading
    from Gamma Large Files Repo"). Narrower than
    _looks_like_large_repo_failure() below on purpose: this one only
    backs the GitHub-rate-limit hint text in _install_failure_message(),
    which is specific to gamma_large_files_v2's own known flakiness and
    would be misleading if shown for one of the other 3 repos.
    """
    return _GAMMA_LARGE_FILES_MARKER in (output or "").lower()


#: The CLI's own output names the failing repo explicitly ("Error
#: downloading/expanding from X Repo") for each of the 4 large, special
#: git-cloned repos the full GAMMA install pipeline depends on.
_LARGE_REPO_FAILURE_MARKERS = (
    _GAMMA_LARGE_FILES_MARKER,
    "gamma setup repo",
    "stalker gamma repo",
    "teivaz anomaly gunslinger repo",
)


def _looks_like_large_repo_failure(output: str) -> bool:
    """True when the failure is one of the 4 large/special repo clones

    (Stalker_GAMMA, gamma_setup, gamma_large_files_v2,
    teivaz_anomaly_gunslinger) - this stays an explicit list of their own
    named failure text instead of reusing the broader, generic
    _looks_like_special_repo_clone_failure() check, which would also
    retry on unrelated generic git errors that happen to hit one of
    these repos for a different reason.
    """
    text = (output or "").lower()
    return any(marker in text for marker in _LARGE_REPO_FAILURE_MARKERS)


#: unzip's and 7z's own words for "this file is not a whole archive" - a
#: download that was cut off. Other extract failures (full disk,
#: permissions) never match, so a good archive is never deleted for them.
_BROKEN_ARCHIVE_MARKERS = (
    "end-of-central-directory signature not found",
    "cannot find zipfile directory",
    "can not open the file as archive",
    # 7zz: "Cannot open the file as [zip] archive" / "Is not archive"
    "cannot open the file as",
    "is not archive",
    "unexpected end of archive",
)


def _broken_archive(output: str) -> tuple[str, str] | None:
    """(Download Path, Url) of the archive the CLI could not unpack because

    the file itself is broken. The CLI reuses any archive already in
    gamma/downloads without checking GitHub-hosted ones, so a cut-off
    download fails the same way on every resume until it is removed.
    """
    text = output or ""
    start = text.find("Install failed! Error extracting")
    if start < 0:
        return None
    block = text[start:]
    if not any(marker in block.lower() for marker in _BROKEN_ARCHIVE_MARKERS):
        return None
    path = re.search(r"^Download Path: (.+?)\s*$", block, re.MULTILINE)
    url = re.search(r"^Url: (.+?)\s*$", block, re.MULTILINE)
    if path is None:
        return None
    return path.group(1), url.group(1) if url else ""


def _replace_broken_archive(download_path: str, url: str, gamma_dir: str) -> str | None:
    """Get a broken archive out of the CLI's way so the next run fixes it.

    Returns "renamed" when the user already saved the file under the name
    in its download link (a manual GitHub download) - that copy takes the
    broken one's place - "deleted" when the broken file was just removed
    so the CLI downloads it again, or None when nothing was touched.
    """
    try:
        downloads = (Path(gamma_dir) / "downloads").resolve()
        archive = Path(download_path)
        # Only ever touch a plain file directly inside this profile's downloads.
        if archive.is_symlink() or archive.parent.resolve() != downloads:
            return None
        target = downloads / archive.name
        if not target.is_file():
            return None
        link_name = Path(urlparse(url).path).name
        manual = downloads / link_name
        if (
            link_name != archive.name
            and manual.suffix.lower() in (".zip", ".7z", ".rar")
            and manual.is_file()
            and not manual.is_symlink()
        ):
            os.replace(manual, target)
            return "renamed"
        target.unlink()
        return "deleted"
    except OSError:
        return None


def _should_auto_retry(enabled: bool, attempt_count: int, output: str) -> bool:
    """Whether a failed full-install should auto-restart instead of showing

    the failure popup - only when the user opted in, only under the
    attempt cap, and only for one of the known large-repo failure
    signatures (never a generic/unrelated failure).
    """
    return (
        enabled
        and attempt_count < _AUTO_RETRY_MAX
        and _looks_like_large_repo_failure(output)
    )


#: Loosely shaped: a leading timestamp, a percentage, and a trailing
#: [done/total] counter somewhere in the line - not the exact pipe-delimited
#: layout, so a minor CLI formatting drift (spacing, a different separator)
#: can't leak progress spam into a failure popup.
_PROGRESS_LINE_RE = re.compile(
    r"^\[\d{2}:\d{2}:\d{2}\].*\d+(?:[.,]\d+)?\s*%.*\[\d+/\d+\]\s*$"
)


def _install_failure_message(
    rc: int, output: str, resume_hint: str | None
) -> tuple[str, str]:
    """Return (summary, detail) text for a failed CLI run.

    ``summary`` is a plain-English explanation meant to be the main,
    visible dialog text; ``detail`` is the exit code plus the tail of raw
    CLI output, meant to go behind a collapsible "Show Details" control so
    it doesn't bury the summary the way it used to.
    """
    lines = [
        l
        for l in (output or "").splitlines()
        if l.strip() and not _PROGRESS_LINE_RE.match(l)
    ]
    detail = "Exit code: {}\n\n{}".format(
        rc, "\n".join(lines[-40:]) if lines else "(no output captured)"
    )
    if _looks_like_cloudflare_challenge(output):
        summary = tr(
            "moddb blocked the download with a Cloudflare check. Trying again from the same connection usually fails too. Switch to a VPN in another country, or wait a while, then run the install again."
        )
    elif _looks_like_network_failure(output):
        summary = tr(
            "This looks like a temporary GitHub or network problem, not a problem with your setup. The download was cut off partway."
        )
    else:
        summary = tr("The install stopped unexpectedly.")
    parts = [
        summary,
        tr(
            "Big downloads from GitHub and moddb sometimes fail partway. It can take a few tries to get through."
        ),
    ]
    if _looks_like_special_repo_clone_failure(output):
        parts.append(
            tr(
                "This step (GAMMA's setup and large files from GitHub) isn't cached like normal mod downloads, so it starts again from the beginning on the next try."
            )
        )
        if _looks_like_gamma_large_files_failure(output):
            parts.append(
                tr(
                    "If it keeps failing on gamma_large_files_v2, GitHub is most likely limiting your downloads. It's not a problem with COMMANDER or your setup. Switch networks or use a VPN, or try again later."
                )
            )
    if resume_hint:
        parts.append(resume_hint)
    parts.append(tr("Full logs: {arg}", arg=logs_dir()))
    return "\n\n".join(parts), detail


class InstallPage(QWidget):
    _wt_dialog = None  # progress pop-up while dependencies install
    def __init__(self, window):
        super().__init__()
        self.setObjectName("installPage")
        self.window = window
        self._runner = None
        self._kept = None
        self._anomaly_runner = None
        self._post_anomaly_verify_runner = None
        self._post_anomaly_verify_cancelled = False
        self._verify_anomaly_after_install = False
        # (rc, output) of an Anomaly run that exited badly, held while its
        # files are verified; _anomaly_failed: last attempt left it broken.
        self._anomaly_failure: tuple[int, str] | None = None
        self._anomaly_failed = False
        self._auto_chain = False
        #: Keep-settings choice from the Reinstall Anomaly pop-up, handed to
        #: the GAMMA reinstall it chains into. None = the GAMMA step's boxes.
        self._chain_preserve: bool | None = None
        self._resume_state: dict[str, str] | None = None
        self._auto_cancelled = False
        self._auto_retry_count = 0
        # Broken archives already replaced during this run of retries; a
        # second failure on the same one stops and asks the user instead.
        self._replaced_archives: set[str] = set()
        self._active_preserve_user = False
        self._active_preserve_mcm = False
        self._checked_archives: set[str] = set()
        self._verify_runner = None
        self._verify_task = None
        self._verify_counts = {"OK": 0, "CORRUPT": 0, "NOT FOUND": 0}
        self._anomaly_problem_lines: list[str] = []
        self._verify_anomaly_ok = False
        self._anomaly_checked = False
        self._scan_cancel = None
        self._scan = None
        self._verify_records = {}
        self._official_names = None
        self._repaired_folders: list[str] = []
        self._repair_kept = None
        self._presence = None
        self._repair_plan = None
        self._repair_records = {}
        self._repair_runner = None
        self._quarantine_records = []
        self._repair_quarantined_count = 0
        self._repair_anomaly_pending = False
        self._gamma_repair_pending = False
        self._gamma_overlay_restore_pending = False
        self._gamma_overlay_restored = False
        self._gamma_repair_done = False
        self._gamma_skipped = False
        self._official_missing = False
        self._cache_archive_result: CacheArchiveVerifyResult | None = None
        self._repo_check = None
        self._repo_files = {}
        self._repo_repair_pending = False
        self._repo_restored: list[str] = []
        # Unpacking broken mods again from their cached archives.
        self._repair_reuse_keys: dict[str, str] = {}
        self._repair_reused: set[str] = set()
        self._repair_resume_written = False
        self._repair_retrying = False
        self._anomaly_recheck_done = False
        self._wt_runner = None
        self._wt_task = None
        self._wt_checking = False
        self._deps_check_task = None
        self._deps_checking = False
        self._wt_installed: bool | None = None
        self._wt_stage = "verbs"
        self._wt_completed_verbs: set[str] = set()
        self._wt_force = False
        self._wt_partial = False
        self._wt_last_pct = -1
        self._winetricks_status_enabled = False
        self._persisting = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(content)
        # -- First view: Automatic or Manual. Automatic has its own card
        # (folder, progress tiles, Start); Manual is the step card below. --
        self._chooser_card, chooser_layout = make_card()
        root.addWidget(self._chooser_card)
        choices = QHBoxLayout()
        choices.setSpacing(16)
        self._auto_choice = _InstallChoice(
            "auto",
            tr("Automatic install"),
            tr("Recommended"),
            tr("<span style='color:{arg};'>Hands-off.</span> Pick a folder and COMMANDER installs Anomaly and GAMMA for you."),
            (
                (tr("Folder"), tr("You choose"), tr("everything else is automatic")),
                (tr("New installs"), tr("Best for"), tr("first time on this PC")),
                (tr("Default"), tr("Settings"), tr("GAMMA's recommended setup")),
            ),
        )
        self._auto_choice.clicked.connect(lambda: self._show_view("auto"))
        self._manual_choice = _InstallChoice(
            "manual",
            tr("Manual install"),
            "",
            tr("<span style='color:{arg};'>Step by step.</span> Go through each step yourself and choose every option."),
            (
                (tr("Everything"), tr("You choose"), tr("folders, options and each step")),
                (tr("Any install"), tr("Best for"), tr("reinstall, update or repair")),
                (tr("Your choice"), tr("Settings"), tr("Default, Minimal or keep yours")),
            ),
        )
        self._manual_choice.clicked.connect(lambda: self._show_view("manual"))
        choice_divider = QFrame()
        choice_divider.setObjectName("vDivider")
        choice_divider.setFixedWidth(1)
        choices.addWidget(self._auto_choice, 1)
        choices.addWidget(choice_divider)
        choices.addWidget(self._manual_choice, 1)
        chooser_layout.addLayout(choices)

        # Under it, for installs already on disk: fix one copied from
        # Windows, or switch which one the game uses.
        self._installs_card, installs_layout = make_card()
        root.addWidget(self._installs_card)
        installs_row = QHBoxLayout()
        installs_row.setSpacing(16)
        self._windows_choice = _InstallChoice(
            "windows",
            tr("Open a Windows install"),
            "",
            tr("<span style='color:{arg};'>Moved from Windows?</span> Pick the copied GAMMA folder and COMMANDER fixes it for Linux."),
            (
                (tr("Folder"), tr("You choose"), tr("the copied GAMMA folder")),
                (tr("Moved installs"), tr("Best for"), tr("set up on a Windows PC")),
                (tr("Kept"), tr("Settings"), tr("saves, mods and MCM stay")),
            ),
            icon_size=64,
        )
        self._windows_choice.clicked.connect(self._open_windows_install)
        self._switch_choice = _InstallChoice(
            "switch",
            tr("Switch install"),
            "",
            tr("<span style='color:{arg};'>One click.</span> Swap between GAMMA installs in different folders."),
            (
                (tr("Install"), tr("You choose"), ""),
                (tr("Several installs"), tr("Best for"), tr("GAMMA in more than one folder")),
                (tr("Kept"), tr("Settings"), tr("runner and launch options stay")),
            ),
            icon_size=64,
        )
        self._switch_choice.clicked.connect(self._open_install_switcher)
        installs_divider = QFrame()
        installs_divider.setObjectName("vDivider")
        installs_divider.setFixedWidth(1)
        installs_row.addWidget(self._windows_choice, 1)
        installs_row.addWidget(installs_divider)
        installs_row.addWidget(self._switch_choice, 1)
        installs_layout.addLayout(installs_row)

        self._auto_card, auto_layout = make_card()
        root.addWidget(self._auto_card)
        auto_layout.setSpacing(14)
        auto_layout.addLayout(make_header_row("Automatic install"))
        auto_layout.addWidget(info_label(tr(
            "COMMANDER installs the Windows runtimes, then Anomaly, then GAMMA, one after another, with the default settings."
        )))
        self.auto_root_edit, auto_root_row = self._make_folder_row(
            tr("Installation directory:"), self._browse_auto_root
        )
        # Folder and offline choice: hidden while Automatic runs, when the
        # card is only tiles and progress.
        self._auto_setup = QWidget()
        self._auto_setup.setObjectName("panelTransparent")
        setup_layout = QVBoxLayout(self._auto_setup)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        setup_layout.setSpacing(14)
        auto_layout.addWidget(self._auto_setup)
        setup_layout.addLayout(auto_root_row)
        auto_folder_hint = info_label(tr(
            "Pick an empty folder on a drive with about 150 GB free. COMMANDER makes anomaly, gamma and cache folders inside it."
        ))
        # Offline: the same install from an extracted cache archive. Above
        # the hints it swaps, so it stays put when clicked.
        self.auto_offline = QCheckBox(tr("Offline install (cache only)"))
        self.auto_offline.setToolTip(tr("Install from a cache already in the cache folder. Nothing is downloaded."))
        setup_layout.addWidget(self.auto_offline)
        setup_layout.addWidget(auto_folder_hint)
        self.auto_offline_box = QWidget()
        self.auto_offline_box.setObjectName("panelTransparent")
        offline_layout = QVBoxLayout(self.auto_offline_box)
        offline_layout.setContentsMargins(0, 0, 0, 0)
        offline_layout.addWidget(info_label(tr(
            "Extract the offline cache archive into the installation directory first, so the cache folder and both list files sit inside it. Only the Windows runtimes are downloaded, about 150 MB."
        )))
        auto_guide = QLabel(
            f'<a href="{OFFLINE_GUIDE_URL}" style="color: {ACCENT.name()};">'
            + html.escape(tr("Where to download the offline cache"))
            + " ↗</a>"
        )
        auto_guide.setOpenExternalLinks(True)
        offline_layout.addWidget(auto_guide)
        self.auto_offline_box.setVisible(False)
        self.auto_offline.toggled.connect(self.auto_offline_box.setVisible)
        # Offline wants the folder the archive went into, not an empty one.
        self.auto_offline.toggled.connect(lambda on: auto_folder_hint.setVisible(not on))
        setup_layout.addWidget(self.auto_offline_box)
        self.auto_tiles = {
            "deps": _CheckTile(tr("Runtimes")),
            "anomaly": _CheckTile(tr("Anomaly")),
            "gamma": _CheckTile(tr("GAMMA")),
            "space": _CheckTile(tr("Free space")),
        }
        auto_layout.addLayout(_tile_row(self.auto_tiles.values()))
        self.auto_start_button = QPushButton(tr("Start install"))
        self.auto_start_button.setObjectName("hero")
        self.auto_start_button.clicked.connect(self._start_automatic_install)
        auto_layout.addWidget(self.auto_start_button)
        auto_nav = QHBoxLayout()
        self.auto_back_button = QPushButton(tr("Back"))
        self.auto_back_button.setObjectName("secondary")
        self.auto_back_button.setProperty("compact", True)
        self.auto_back_button.clicked.connect(lambda: self._show_view("chooser"))
        auto_nav.addWidget(self.auto_back_button)
        auto_nav.addStretch(1)
        auto_layout.addLayout(auto_nav)
        # The install progress panel goes above Back while Automatic runs
        # (see _place_progress).
        self._auto_layout = auto_layout
        #: Set while Automatic runs the dependencies, so their end starts
        #: Anomaly and GAMMA instead of showing the result pop-up.
        self._wt_then_install = False
        #: An Automatic install is running; the tab reopens on its card.
        self._auto_active = False

        # -- One card: a track of the five steps, and under it the picked
        # step's page. Every step keeps its own widgets; only one shows. --
        self._wizard_card, w_layout = make_card()
        root.addWidget(self._wizard_card)
        w_layout.setSpacing(14)
        # The track's underlines sit on the divider, like the main nav bar.
        track_box = QVBoxLayout()
        track_box.setSpacing(0)
        track = reflow.row(0)
        track_box.addLayout(track)
        track_line = QFrame()
        track_line.setObjectName("rowDivider")
        track_line.setFixedHeight(1)
        track_box.addWidget(track_line)
        w_layout.addLayout(track_box)
        # Faded as one when the step changes: the page, its button and the
        # console under it.
        self._fade_box = QWidget()
        self._fade_box.setObjectName("panelTransparent")
        fade_layout = QVBoxLayout(self._fade_box)
        fade_layout.setContentsMargins(0, 0, 0, 0)
        fade_layout.setSpacing(14)
        w_layout.addWidget(self._fade_box)
        # As tall as the tallest step, so every step's button below sits
        # at the same height.
        self._page_stack = QStackedWidget()
        self._page_stack.setObjectName("panelTransparent")
        fade_layout.addWidget(self._page_stack)
        # Each step's main button, all the same size in the same place.
        self._action_stack = QStackedWidget()
        self._action_stack.setObjectName("panelTransparent")
        fade_layout.addWidget(self._action_stack)
        # Under the button: the console of the step that has one.
        self._step_extras: list[QWidget | None] = [None] * len(_STEP_NAMES)
        self._extras_layout = fade_layout
        self._steps: list[_WizardStep] = []
        self._step_pages: list[QWidget] = []
        self._step = 0
        for number, name in enumerate(_STEP_NAMES, start=1):
            step = _WizardStep(number, tr(name))
            step.clicked.connect(lambda _checked=False, i=number - 1: self._go_to_step(i))
            track.addWidget(step, 1)
            self._steps.append(step)

        def step_page(intro: str, status: QWidget | None = None) -> QVBoxLayout:
            """One step's page, laid out like every other: the "Step N."
            line, the step's own fields and options, then (see step_tiles)
            its row of tiles kept at the bottom, so tiles and button sit at
            the same height on every step."""
            page = QWidget()
            page.setObjectName("panelTransparent")
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(12)
            layout.addLayout(_step_intro(intro, status))
            body = QVBoxLayout()
            body.setSpacing(10)
            layout.addLayout(body)
            layout.addStretch(1)
            self._page_stack.addWidget(page)
            self._step_pages.append(page)
            return body

        def step_tiles(tiles: dict) -> dict:
            layout = self._step_pages[-1].layout()
            line = QFrame()
            line.setObjectName("rowDivider")
            line.setFixedHeight(1)
            layout.addWidget(line)
            layout.addLayout(_tile_row(tiles.values()))
            return tiles

        def step_action(*buttons: QPushButton) -> None:
            box = QWidget()
            box.setObjectName("panelTransparent")
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            for button in buttons:
                row.addWidget(button, 1)
            self._action_stack.addWidget(box)

        def step_extra(index: int, widget: QWidget) -> None:
            self._step_extras[index] = widget
            self._extras_layout.addWidget(widget)

        def option_groups(body: QVBoxLayout, columns, boxes: dict) -> None:
            """Option groups in two equal columns, on Anomaly's step and
            GAMMA's alike, so their columns line up between the two."""
            row = reflow.row(32)
            for groups in columns:
                column = QWidget()
                column.setObjectName("panelTransparent")
                column_layout = QVBoxLayout(column)
                column_layout.setContentsMargins(0, 0, 0, 0)
                column_layout.setSpacing(10)
                for title, keys in groups:
                    group = QGroupBox(tr(title))
                    layout = QVBoxLayout(group)
                    layout.setSpacing(6)
                    layout.setContentsMargins(0, 0, 0, 0)  # borderless: line up with the fields
                    for key in keys:
                        layout.addWidget(boxes[key])
                    column_layout.addWidget(group)
                column_layout.addStretch(1)
                row.addWidget(column, 1)
            body.addLayout(row)

        # -- Step 1: installation directory --
        body = step_page(
            tr("<span style='color:{arg};'>Step 1.</span> Choose where to install Anomaly and GAMMA."),
        )
        self.root_card_edit, self.root_card_row = (
            self._make_folder_row(
                tr("Installation directory:"),
                self._browse_install_root,
            )
        )
        body.addLayout(self.root_card_row)
        body.addWidget(info_label(tr(
            "Pick an empty folder on a drive with about 150 GB free. Create folders makes anomaly, gamma and cache folders inside it and saves them to your profile."
        )))
        self.storage_group = QGroupBox(tr("Storage usage"))
        storage_layout = QVBoxLayout(self.storage_group)
        storage_layout.setContentsMargins(0, 0, 0, 0)  # borderless, like the option groups
        storage_layout.setSpacing(8)
        body.addWidget(self.storage_group)
        # What Create folders sets up, and whether the drive has room.
        self.folder_tiles = step_tiles({
            "anomaly": _CheckTile(tr("Anomaly folder")),
            "gamma": _CheckTile(tr("GAMMA folder")),
            "cache": _CheckTile(tr("Cache folder")),
            "space": _CheckTile(tr("Free space")),
        })
        self.folder_tiles["space"].setToolTip(
            tr("About 150 GB in total, or about 100 GB with Minimal.")
        )
        self.create_folders_button = QPushButton(tr("Create folders"))
        self.create_folders_button.setObjectName("primary")
        self.create_folders_button.clicked.connect(self._create_install_folders)
        step_action(self.create_folders_button)

        # -- Step 2: dependencies --
        self.wt_status = InstallStatusRow("", ok=None, pending_text="Checking")
        body = step_page(
            tr("<span style='color:{arg};'>Step 2.</span> Install the Windows runtimes the game needs."),
            self.wt_status,
        )
        # One row per runtime and tool the check covers, ticked once it is
        # there - grouped in columns like the Anomaly and GAMMA options.
        self._wt_verbs: dict[str, bool] | None = None
        self.deps_rows = {key: _DependencyRow(key) for _title, keys in _DEPENDENCY_GROUPS for key in keys}
        columns = ((_DEPENDENCY_GROUPS[0],), _DEPENDENCY_GROUPS[1:])
        option_groups(body, columns, self.deps_rows)
        self.deps_tiles = step_tiles({
            "runtimes": _CheckTile(tr("Runtimes")),
            "prefix": _CheckTile(tr("Wine prefix")),
            "runner": _CheckTile(tr("Runner")),
            "space": _CheckTile(tr("Free space")),
        })
        self.winetricks_button = QPushButton(tr("Install Dependencies"))
        self.winetricks_button.setObjectName("primary")
        self.winetricks_button.setToolTip(
            tr("Installs Windows runtimes (VC++, DirectX) into the Wine prefix.")
        )
        self.winetricks_button.clicked.connect(self._start_winetricks)
        step_action(self.winetricks_button)
        self.wt_progress = ProgressArea(show_table=False, show_log=True, log_max_height=180)
        # No Idle/percent bar for Dependencies - the status label and log
        # below it already say what's happening; nothing in ProgressArea
        # ever re-shows the bar once hidden, so this sticks permanently.
        self.wt_progress.bar.hide()
        # "N/N dependencies installed" (and other status text) sits here,
        # between the Install Dependencies button above and the Show
        # Console toggle below - right-aligned per request.
        self.wt_progress.status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.wt_progress.cancel_button.clicked.connect(self._cancel_winetricks)
        step_extra(1, self.wt_progress)

        # -- Step 3: STALKER Anomaly. The first two options are GUI-level
        # orchestration, not CLI arguments - see _start_anomaly_install/
        # _on_anomaly_finished. The mirror one is the CLI's
        # --use-anomaly-mirror. --
        self.anomaly_status = InstallStatusRow("")
        body = step_page(
            tr("<span style='color:{arg};'>Step 3.</span> Download and install STALKER Anomaly 1.5.3."),
            self.anomaly_status,
        )
        self.anomaly_edit, self.anomaly_folder_row = (
            self._make_folder_row(tr("Anomaly folder:"), self._browse_anomaly)
        )
        body.addLayout(self.anomaly_folder_row)
        self.anomaly_checkboxes = {}
        for key, label, tooltip in _ANOMALY_CHECKBOXES:
            cb = QCheckBox(tr(label))
            cb.setToolTip(tr(tooltip))
            cb.setChecked(key in _DEFAULT_CHECKED_OPTIONS)
            self.anomaly_checkboxes[key] = cb
        option_groups(body, _ANOMALY_OPTION_GROUPS, self.anomaly_checkboxes)
        # Says why Install Anomaly is greyed out while Offline is ticked.
        self.anomaly_offline_note = info_label(tr(_ANOMALY_OFFLINE_NOTE))
        self.anomaly_offline_note.hide()
        body.addWidget(self.anomaly_offline_note)
        # GAMMA's options live on step 4; when Anomaly chains into GAMMA,
        # say here which of them that install will use.
        self.gamma_chain_note = info_label("")
        body.addWidget(self.gamma_chain_note)
        self.anomaly_tiles = step_tiles({
            "version": _CheckTile(tr("Version")),
            "folder": _CheckTile(tr("Anomaly folder")),
            "download": _CheckTile(tr("Download")),
            "space": _CheckTile(tr("Free space")),
        })
        self.anomaly_button = QPushButton(tr("Install Anomaly"))
        self.anomaly_button.setObjectName("primary")
        # Wrapped: clicked() passes a bool that would land in skip_confirm.
        self.anomaly_button.clicked.connect(self._on_anomaly_button_clicked)
        step_action(self.anomaly_button)

        # -- Step 4: GAMMA modpack --
        self.gamma_status = InstallStatusRow("")
        body = step_page(
            tr("<span style='color:{arg};'>Step 4.</span> Download and install GAMMA and all its mods."),
            self.gamma_status,
        )
        self.gamma_edit, self.gamma_folder_row = (
            self._make_folder_row(tr("GAMMA folder:"), self._browse_gamma)
        )
        self.cache_edit, self.cache_folder_row = (
            self._make_folder_row(tr("Cache folder:"), self._browse_cache)
        )
        # Side by side, one row like Anomaly's single folder.
        folders = reflow.row(24)
        folders.addLayout(self.gamma_folder_row, 1)
        folders.addLayout(self.cache_folder_row, 1)
        body.addLayout(folders)
        self.checkboxes = {}
        for key, label, tooltip in _CHECKBOXES:
            cb = QCheckBox(tr(label))
            cb.setToolTip(tr(tooltip))
            cb.setChecked(key in _DEFAULT_CHECKED_OPTIONS)
            self.checkboxes[key] = cb
        option_groups(body, _GAMMA_OPTION_GROUPS, self.checkboxes)

        # Offline install: the two list files, shown only while ticked.
        # Checkbox states to put back when Offline is unticked again.
        self._offline_saved: dict[str, bool] = {}
        self.offline_box = QWidget()
        self.offline_box.setObjectName("panelTransparent")
        offline_layout = QGridLayout(self.offline_box)
        offline_layout.setContentsMargins(0, 4, 0, 0)
        offline_layout.setVerticalSpacing(8)
        self.offline_hint = info_label("")
        offline_layout.addWidget(self.offline_hint, 0, 0, 1, 2)
        self.offline_edits = []
        for row, (label, key, placeholder) in enumerate(
            (
                ("Modpack maker list:", "offline_modpack_list", "Choose modpack_maker_list.txt"),
                ("Mod list:", "offline_mod_list", "Choose modlist.txt"),
            ),
            start=1,
        ):
            edit = _PathEdit()
            edit.setPlaceholderText(tr(placeholder))
            edit.clicked.connect(lambda key=key: self._browse_offline_list(key))
            self.offline_edits.append(edit)
            offline_layout.addWidget(QLabel(tr(label)), row, 0)
            offline_layout.addWidget(edit, row, 1)
        guide = QLabel(
            f'<a href="{OFFLINE_GUIDE_URL}" style="color: {ACCENT.name()};">'
            + html.escape(tr("Where to download the offline cache"))
            + " ↗</a>"
        )
        guide.setOpenExternalLinks(True)
        offline_layout.addWidget(guide, 3, 0, 1, 2)
        offline_layout.setColumnStretch(1, 1)
        self.offline_box.setVisible(False)
        body.addWidget(self.offline_box)
        self.checkboxes["offline"].toggled.connect(self._on_offline_toggled)
        self.checkboxes["offline"].toggled.connect(lambda _on: self._update_button_states())
        self.gamma_tiles = step_tiles({
            "mods": _CheckTile(tr("Mods")),
            "folder": _CheckTile(tr("GAMMA folder")),
            "cache": _CheckTile(tr("Download cache")),
            "space": _CheckTile(tr("Free space")),
        })
        # The Download tile follows these as they are ticked.
        for cb in (self.checkboxes["offline"], self.anomaly_checkboxes["anomaly_mirror"]):
            cb.toggled.connect(lambda _on: self._refresh_install_tiles())
        self.install_button = QPushButton(tr("Install GAMMA"))
        self.install_button.setObjectName("primary")
        self.install_button.setToolTip(
            tr("Install or update GAMMA. Anomaly is installed first if it is missing.")
        )
        # Wrapped: clicked() passes a bool that would land in skip_confirm.
        self.install_button.clicked.connect(lambda: self._start_full_install())
        step_action(self.install_button)

        # -- Step 5: verify integrity --
        # Text, then the dot; both named verifyPill so the level colours
        # in themes.py tint them together.
        self.verify_pill = QLabel()
        self.verify_pill.setObjectName("verifyPill")
        self.verify_dot = StatusDot()
        self.verify_dot.setObjectName("verifyPill")
        pill_box = QWidget()
        pill_box.setObjectName("panelTransparent")
        pill_row = QHBoxLayout(pill_box)
        pill_row.setContentsMargins(0, 0, 0, 0)
        pill_row.setSpacing(6)
        pill_row.addWidget(self.verify_pill)
        pill_row.addWidget(self.verify_dot)
        body = step_page(
            tr("<span style='color:{arg};'>Step 5.</span> Find and repair missing or changed game files. Your saves and settings are left alone."),
            pill_box,
        )
        self.verify_maintenance_label = QLabel(
            tr("Verify Integrity is currently under maintenance")
        )
        self.verify_maintenance_label.setStyleSheet(
            f"color: {STATUS_RED.name()}; font-weight: bold;"
        )
        self.verify_maintenance_label.setVisible(VERIFY_INTEGRITY_DISABLED)
        body.addWidget(self.verify_maintenance_label)
        self.verify_checkboxes = {}
        for key, label, tooltip in _VERIFY_CHECKBOXES:
            cb = QCheckBox(tr(label))
            cb.setToolTip(tr(tooltip))
            cb.setChecked(key in _DEFAULT_CHECKED_OPTIONS)
            self.verify_checkboxes[key] = cb
        self.verify_checkboxes["check_mods"].setEnabled(False)
        # The two Automatic ones are standing preferences, so they're saved.
        saved = gui_settings.load_gui_settings()
        for key in ("repair_prompt", "verify_after_gamma"):
            setting = "verify_" + key.removeprefix("verify_")
            cb = self.verify_checkboxes[key]
            cb.setChecked(bool(saved.get(setting, False)))
            cb.toggled.connect(lambda on, s=setting: gui_settings.save_gui_settings(**{s: on}))
        option_groups(body, _VERIFY_OPTION_GROUPS, self.verify_checkboxes)
        self.verify_tiles = step_tiles({
            "anomaly": _CheckTile(tr("Anomaly files")),
            "mods": _CheckTile(tr("GAMMA mods")),
            "md5": _CheckTile(tr("Mod file changes")),
            "cache": _CheckTile(tr("Download cache")),
        })
        self.verify_tiles["anomaly"].setToolTip(
            tr("Every Anomaly file is compared with Anomaly's own list of original checksums.")
        )
        self.verify_tiles["mods"].setToolTip(
            tr("Every enabled mod must have a folder with files in it.")
        )
        self.verify_tiles["md5"].setToolTip(
            tr("Finds mod files changed since the last check.")
        )
        self.verify_tiles["cache"].setToolTip(
            tr("Checks cached archives against GAMMA's list. Never touches the game.")
        )
        self.verify_button = QPushButton(tr("Verify Integrity"))
        self.verify_button.setObjectName("primary")
        self.verify_button.clicked.connect(self._start_verify)
        self.repair_button = QPushButton(tr("Repair now"))
        self.repair_button.setObjectName("primary")
        self.repair_button.setToolTip(
            tr("Shows exactly what will be re-downloaded before anything changes.")
        )
        self.repair_button.clicked.connect(self._on_repair_clicked)
        self.repair_button.hide()
        step_action(self.verify_button, self.repair_button)
        # No status line: the bar's text already names the step.
        self.verify_progress = ProgressArea(
            show_table=False, show_log=True, log_max_height=180, show_status=False
        )
        # Only offered while a check or repair runs (see _set_verify_console).
        self.verify_progress.log_toggle.hide()
        self.verify_progress.cancel_button.clicked.connect(self._cancel_verify)
        # The tiles and pill already say "not checked"; an empty Idle bar adds nothing.
        self.verify_progress.bar.hide()
        verify_extra = QWidget()
        verify_extra.setObjectName("panelTransparent")
        extra_layout = QVBoxLayout(verify_extra)
        extra_layout.setContentsMargins(0, 0, 0, 0)
        extra_layout.addWidget(self.verify_progress)
        self.verify_findings = QVBoxLayout()
        self.verify_findings.setSpacing(6)
        extra_layout.addLayout(self.verify_findings)
        step_extra(4, verify_extra)

        # -- One progress panel for the Anomaly and GAMMA installs (only
        # one runs at a time): on the running step of the Manual card, or
        # on the Automatic card while Automatic runs - see _place_progress. --
        self.progress_panel = QFrame()
        self.progress_panel.setObjectName("installProgress")
        panel_layout = QVBoxLayout(self.progress_panel)
        panel_layout.setContentsMargins(14, 12, 14, 14)
        panel_layout.setSpacing(8)
        self.progress_title = QLabel()
        self.progress_title.setObjectName("cardTitle")
        panel_layout.addWidget(self.progress_title)
        # No status line: the table already names each file and step.
        self.full_progress = ProgressArea(show_log=False, show_status=False)
        self.full_progress.cancel_button.clicked.connect(self._cancel_full_install)
        # The addon table is the console, always open: no Show Console.
        self.full_progress.table_toggle.hide()
        self.full_progress.table.show()
        self.full_progress.table.setMinimumHeight(170)
        self.full_progress.table.setMaximumHeight(240)
        self.full_progress.table.setShowGrid(False)
        self.full_progress.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft)
        panel_layout.addWidget(self.full_progress)
        self._panel_index = fade_layout.count()
        fade_layout.addWidget(self.progress_panel)
        # The other steps say in one line what is running instead.
        self.progress_note = info_label("")
        fade_layout.addWidget(self.progress_note)

        # -- Back / Next, under every step --
        step_nav = QHBoxLayout()
        self.step_back_button = QPushButton(tr("Back"))
        self.step_next_button = QPushButton(tr("Next"))
        for button, delta in ((self.step_back_button, -1), (self.step_next_button, 1)):
            button.setObjectName("secondary")
            button.setProperty("compact", True)
            button.clicked.connect(lambda _checked=False, d=delta: self._step_nav(d))
        # In Next's place on the last step: everything is done, go play.
        self.step_play_button = QPushButton(tr("Play"))
        self.step_play_button.setObjectName("secondary")
        self.step_play_button.setProperty("compact", True)
        self.step_play_button.clicked.connect(self._open_play_page)
        step_nav.addWidget(self.step_back_button)
        step_nav.addStretch(1)
        step_nav.addWidget(self.step_next_button)
        step_nav.addWidget(self.step_play_button)
        w_layout.addLayout(step_nav)
        # Step changes fade out and back in (see _go_to_step).
        self._fade_effect = QGraphicsOpacityEffect(self._fade_box)
        self._fade_effect.setOpacity(1.0)
        # Off between fades: a live effect renders the whole box offscreen
        # on every repaint inside it.
        self._fade_effect.setEnabled(False)
        self._fade_box.setGraphicsEffect(self._fade_effect)
        self._fade_target: int | None = None
        # 0 -> 1 fades out, the step switches at 1, 1 -> 2 fades back in.
        self._fade = QVariantAnimation(self)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(2.0)
        self._fade.setDuration(_STEP_FADE_MS)
        self._fade.valueChanged.connect(self._on_fade_value)
        self._fade.finished.connect(self._on_fade_finished)
        self.anomaly_status.changed.connect(lambda state, text: self._steps[2].set_status(state, text))
        self.gamma_status.changed.connect(lambda state, text: self._steps[3].set_status(state, text))
        self.wt_status.changed.connect(lambda state, text: self._steps[1].set_status(state, text))
        self.wt_status.changed.connect(lambda _state, _text: self._refresh_deps_tiles())
        for key, row in (("deps", self.wt_status), ("anomaly", self.anomaly_status), ("gamma", self.gamma_status)):
            row.changed.connect(lambda state, text, k=key: self._set_auto_tile(k, state, text))
        for cb in (*self.checkboxes.values(), self.anomaly_checkboxes["auto_continue_gamma"]):
            cb.toggled.connect(lambda _on: self._update_chain_note())
        self._update_chain_note()
        self._steps[1].set_status("pending", tr("Checking"))
        # Spare window height goes below the card, not inside it - the
        # console grows the card only when it is shown.
        root.addStretch(1)
        self._pending_repair: tuple | None = None
        self.refresh()
        self._show_view("manual")
        QApplication.instance().applicationStateChanged.connect(self._on_app_state_changed)
        # The snapshot age counts up while the page stays open.
        self._age_timer = QTimer(self)
        self._age_timer.timeout.connect(lambda: self.isVisible() and self._refresh_verify_age())
        self._age_timer.start(60_000)
        # Free space counts down live while Anomaly or GAMMA downloads.
        self._space_timer = QTimer(self)
        self._space_timer.timeout.connect(self._refresh_live_space)
        self._space_timer.start(2_000)

    def on_theme_changed(self) -> None:
        for label in self.findChildren(QLabel):
            template = label.property("leadTemplate")
            if template:
                label.setText(template.replace("{arg}", OK_GREEN.name()))

    def enable_winetricks_status(self):
        self._winetricks_status_enabled = True
        self._refresh_winetricks_status()

    def refresh(self):
        self.window.refresh_settings()
        profile = self.window.settings.active_profile
        if profile is not None:
            state = gui_settings.load_gui_settings().get("gamma_install_resume")
            self._resume_state = state if _resume_state_matches(state, profile) else None
            self.anomaly_edit.setText(profile.anomaly)
            self.gamma_edit.setText(profile.gamma)
            self.cache_edit.setText(profile.cache)
            self._fill_offline_lists(profile)
            if not self.window.install_busy:
                # Anomaly's installed/not-installed state is already shown
                # by anomaly_status above - the shared progress console
                # below only needs to reflect the overall/GAMMA state here.
                op = getattr(self.window, "install_operation", None)
                if gamma_installed(profile.gamma, profile.mo2_profile):
                    self.full_progress.bar.setRange(0, 1)
                    self.full_progress.bar.setValue(1)
                    self.full_progress.bar.setFormat(
                        tr("Incomplete") if self._resume_state is not None else tr("Installed")
                    )
                elif op != "gamma":
                    self.full_progress.bar.setRange(0, 1)
                    self.full_progress.bar.setValue(0)
                    self.full_progress.bar.setFormat(tr("Not installed"))
                self._show_saved_verify_result()
        self._update_install_status()
        self._refresh_deps_tiles()
        self._refresh_winetricks_status()
        self._update_button_states()

    def on_busy_changed(self, _busy):
        """Global install lock changed; re-evaluate this page's controls."""
        if not self.window.install_busy and not self._wt_then_install and not self._auto_chain:
            self._auto_active = False
        self._update_button_states()

    def on_install_activity_changed(self, operation):
        """Reflect the active Anomaly or GAMMA install in the status rows."""
        # Recompute both rows: when Anomaly chains into GAMMA, Anomaly's row
        # must flip to Installed as GAMMA's flips to Installing.
        if operation in ("anomaly", "gamma", None):
            self._update_install_status()
        # A running install opens its step; any change re-checks whether
        # the shared progress console is shown.
        if operation in ("anomaly", "gamma"):
            self._show_step(2 if operation == "anomaly" else 3)
        else:
            self._show_step(self._step)

    def showEvent(self, event) -> None:
        """Opening the Install tab starts on the Automatic / Manual choice -
        or on whatever is running (its progress and Cancel live there).
        Not when the window is merely restored (a spontaneous show)."""
        super().showEvent(event)
        if not event.spontaneous():
            step = self._running_step()
            if self._auto_running():
                self._show_view("auto")
            elif step:
                self._show_view("manual", step)
            elif self._resume_state is not None:
                # Back where the stopped install was started, on its Resume button.
                if self._resume_state.get("auto"):
                    self._show_view("auto")
                else:
                    self._show_view("manual", 3)
            else:
                self._show_view("chooser")

    def _running_step(self) -> int:
        """The step with something running, or 0 (Folder) when idle."""
        if self.verify_pill.property("level") == "running":  # a check or repair
            return 4
        op = getattr(self.window, "install_operation", None)
        return {"dependencies": 1, "anomaly": 2, "gamma": 3}.get(op, 0)

    def _auto_running(self) -> bool:
        """An Automatic install is somewhere in its chain."""
        return self._auto_active

    def _show_view(self, view: str, step: int = 0) -> None:
        """Show the Automatic / Manual choice, the Automatic card, or the
        Manual step card at ``step``."""
        if view == "chooser":
            self._refresh_auto_choice()
            self._refresh_switch_choice()
        self._chooser_card.setVisible(view == "chooser")
        self._installs_card.setVisible(view == "chooser")
        self._auto_card.setVisible(view == "auto")
        self._wizard_card.setVisible(view == "manual")
        if view == "manual":
            self._show_step(step)
        elif view == "auto":
            if not self.auto_root_edit.text():
                self.auto_root_edit.setText(self.root_card_edit.text())
            self._refresh_auto_space()
            # The Runtimes tile needs the dependencies check.
            self.enable_winetricks_status()
            self._update_button_states()
            self._place_progress()

    # ------------------------------------------------- installs on disk
    def _known_installs(self) -> list[tuple[str, str, str]]:
        """Installs added to the switcher, the one in use first."""
        installs = [tuple(i) for i in gui_settings.load_gui_settings().get("game_installs") or []]
        profile = self.window.settings.active_profile
        if profile is not None and is_gamma(Path(profile.gamma).expanduser()):
            current = (profile.anomaly, profile.gamma, profile.cache)
            installs = [current, *(i for i in installs if i != current)]
        return installs

    @staticmethod
    def _install_root(install: tuple[str, str, str]) -> str:
        return os.path.commonpath([install[0], install[1]])

    def _refresh_switch_choice(self) -> None:
        installs = self._known_installs()
        profile = self.window.settings.active_profile
        self._switch_choice.tiles[0].set_state(
            "ok", tr("Install"), tr("{count} saved in COMMANDER", count=len(installs))
        )
        # The tag names the install in use, as Automatic's names its mode.
        tag = self._switch_choice.tag
        if installs and profile is not None and installs[0][1] == profile.gamma:
            root = self._install_root(installs[0])
            tag.setText(tr("Playing: {name}", name=Path(root).name or root))
            tag.setToolTip(root)
        else:
            tag.setText("")
            tag.setToolTip("")

    def _use_install(self, install: tuple[str, str, str], info: str = "") -> bool:
        """Point the active profile at *install*, remember it and say so."""
        if self.window.install_busy or mo2_running():
            QMessageBox.warning(
                self,
                tr("Please wait"),
                tr("An install or the game is running, so folder changes can't be saved right now. Try again when it's done."),
            )
            return False
        self.window.refresh_settings()
        profile = self.window.settings.active_profile
        if profile is None:
            return False
        # Each install keeps the MO2 profile it was last played with.
        old_mo2 = profile.mo2_profile
        remembered = gui_settings.load_gui_settings().get("install_mo2_profiles")
        remembered = {**(remembered if isinstance(remembered, dict) else {}), profile.gamma: old_mo2}
        profile.mo2_profile = mo2_profile_for(install[1], remembered.get(install[1], old_mo2))
        try:
            save_profile_dirs(self.window.settings, profile, *install)
        except OSError as exc:
            profile.mo2_profile = old_mo2
            QMessageBox.warning(
                self, tr("Couldn't save the profile"), tr("COMMANDER couldn't write its settings file (settings.json).\n\n{exc}", exc=exc)
            )
            return False
        installs = [list(i) for i in self._known_installs()]
        gui_settings.save_gui_settings(game_installs=installs, install_mo2_profiles=remembered)
        self.window.refresh_settings()
        self.refresh()
        self._refresh_switch_choice()
        folder = self._install_root(install)
        done = tr("The game now runs from {name}.", name=Path(folder).name or folder)
        self.window.statusBar().showMessage(done, 6000)
        QMessageBox.information(
            self,
            tr("Install switched"),
            done,
            points=(
                tr("Folder: {folder}", folder=folder),
                tr("Mod Organizer profile: {name}", name=profile.mo2_profile),
            ),
            info=" ".join(filter(None, (info, tr("Your runner and launch options stay the same.")))),
        )
        return True

    def _pick_install(self, title: str) -> tuple[str, str, str] | None:
        """Ask for a folder and find an install in it."""
        folder = QFileDialog.getExistingDirectory(self, title, str(Path.home()))
        if not folder:
            return None
        install = find_install(folder)
        if install is None:
            QMessageBox.warning(
                self,
                tr("No GAMMA install found"),
                tr("Pick the folder that holds both the Anomaly folder and the GAMMA folder (the one with ModOrganizer.exe)."),
            )
        return install

    def _fix_for_linux(self, install: tuple[str, str, str]) -> bool:
        """Fix Windows paths in *install*'s ModOrganizer.ini, after asking."""
        if not windows_paths(install[1]):
            return True
        answer = QMessageBox.question(
            self,
            tr("Fix this install for Linux?"),
            tr("This GAMMA install was set up on Windows. COMMANDER changes its paths so it runs here, then plays from it."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            points=(
                tr("Only ModOrganizer.ini changes. Mods, saves and settings stay as they are."),
                tr("The old file is kept as ModOrganizer.ini.windows.bak."),
            ),
            labels={QMessageBox.StandardButton.Yes: tr("Fix and play"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            left = fix_windows_paths(install[0], install[1])
        except OSError as exc:
            QMessageBox.warning(self, tr("Couldn't fix the install"), tr("ModOrganizer.ini could not be updated: {exc}", exc=exc))
            return False
        if left:
            QMessageBox.information(
                self,
                tr("Some paths point outside the install"),
                tr("These are on another Windows drive, so they were left as they are. Change them in Mod Organizer if you need them."),
                points=tuple(left),
            )
        return True

    def _open_windows_install(self) -> None:
        install = self._pick_install(tr("Select the GAMMA install copied from Windows"))
        if install is None:
            return
        info = "" if windows_paths(install[1]) else tr("This install has no Windows paths left, so it already works on Linux.")
        if self._fix_for_linux(install):
            self._use_install(install, info)

    def _open_install_switcher(self) -> None:
        installs = self._known_installs()
        profile = self.window.settings.active_profile
        dialog = QDialog(self)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        layout.addWidget(info_label(tr("Pick the install to play. Only the folders change, your runner and launch options stay.")))
        chosen: list = []
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setColumnStretch(0, 1)
        for row, install in enumerate(installs):
            active = profile is not None and install[1] == profile.gamma
            path = QLabel(self._install_root(install))
            path.setToolTip("\n".join(install))
            grid.addWidget(path, row, 0)
            use = QPushButton(tr("Playing") if active else tr("Play this"))
            use.setObjectName("primary")
            use.setEnabled(not active and Path(install[1]).is_dir())
            use.clicked.connect(lambda _c=False, i=install: (chosen.append(("use", i)), dialog.accept()))
            grid.addWidget(use, row, 1)
            remove = QPushButton(tr("Remove"))
            remove.setToolTip(tr("Remove from this list. No files are deleted."))
            remove.setEnabled(not active)
            remove.clicked.connect(lambda _c=False, i=install: (chosen.append(("remove", i)), dialog.accept()))
            grid.addWidget(remove, row, 2)
        layout.addLayout(grid)
        add = QPushButton(tr("Add install"))
        add.clicked.connect(lambda: (chosen.append(("add", None)), dialog.accept()))
        layout.addWidget(add, 0, Qt.AlignmentFlag.AlignLeft)
        add_close_button(dialog, layout)
        style_window(dialog, tr("Switch install"), resizable=False)
        dialog.setMinimumWidth(640)
        if not dialog.exec() or not chosen:
            return
        action, install = chosen[0]
        if action == "remove":
            gui_settings.save_gui_settings(game_installs=[list(i) for i in installs if i != install])
            self._refresh_switch_choice()
        elif action == "add":
            install = self._pick_install(tr("Select a folder with a GAMMA install"))
            if install is not None and install not in installs:
                gui_settings.save_gui_settings(game_installs=[*map(list, installs), list(install)])
                self._refresh_switch_choice()
        else:
            if self._fix_for_linux(install):
                self._use_install(install)
            return
        self._open_install_switcher()

    def _place_progress(self) -> None:
        """Put the install progress panel where the install is watched:
        on the Automatic card while it shows, else on the Manual card's
        running step. Only while Anomaly or GAMMA installs - the step's
        status pill says Installed / Not installed the rest of the time."""
        op = getattr(self.window, "install_operation", None)
        running = {"anomaly": 2, "gamma": 3}.get(op)
        auto = not self._auto_card.isHidden()
        panel = self.progress_panel
        self._auto_layout.removeWidget(panel)
        self._extras_layout.removeWidget(panel)
        if auto:
            self._auto_layout.insertWidget(self._auto_layout.count() - 1, panel)
        else:
            self._extras_layout.insertWidget(self._panel_index, panel)
        panel.setVisible(running is not None and (auto or self._step == running))
        # The panel stands in for the running step's greyed-out button.
        self._action_stack.setVisible(running is None or self._step != running)
        self.progress_title.setText(tr("Installing STALKER Anomaly") if op == "anomaly" else tr("Installing GAMMA"))
        note = ""
        if running is not None and not auto and self._step != running:
            if op == "anomaly" and self._step == 3:
                note = (
                    tr("Anomaly is installing. GAMMA starts as soon as it is done.")
                    if self._auto_chain
                    else tr("Anomaly is installing. GAMMA can be installed once it is done.")
                )
            else:
                note = tr(
                    "{name} is installing. Its progress is on step {step}.",
                    name="Anomaly" if op == "anomaly" else "GAMMA",
                    step=running + 1,
                )
        self.progress_note.setText(note)
        self.progress_note.setVisible(bool(note))

    def _gamma_already_installed(self) -> bool:
        profile = self.window.settings.active_profile
        return profile is not None and gamma_installed(profile.gamma, profile.mo2_profile)

    def _refresh_auto_choice(self) -> None:
        """Automatic is for new installs only: over an existing GAMMA its
        default options would replace the player's keybindings and MCM
        settings. Reinstalls go through Manual, which asks. Developer mode
        lifts the lock."""
        if self._resume_state is not None:
            self._auto_choice.set_available(True, tr("Incomplete"))
        elif not self._gamma_already_installed():
            self._auto_choice.set_available(True, tr("Recommended"))
        elif gui_settings.developer_feature("auto_reinstall"):
            self._auto_choice.set_available(
                True,
                tr("Developer mode"),
                tr("GAMMA is already installed. Automatic installs with the default options, so your keybindings and MCM settings are replaced."),
            )
        else:
            self._auto_choice.set_available(
                False,
                tr("For new installs"),
                tr("GAMMA is already installed. Use Manual to reinstall, update or repair it."),
            )

    def _step_nav(self, delta: int) -> None:
        if self._step == 0 and delta < 0:
            self._show_view("chooser")
        else:
            self._go_to_step(self._step + delta)

    def _browse_auto_root(self) -> None:
        if self._pick_folder(self.auto_root_edit, tr("Select install root directory")):
            self._refresh_auto_space()

    def _refresh_auto_space(self) -> None:
        root = self.auto_root_edit.text().strip()
        tile = self.auto_tiles["space"]
        _space_tile(tile, root)
        free = free_space_bytes(root) if root else None
        # Not mid-install: the install itself is what uses the space up.
        if free is not None and free < 150 * 1024**3 and not self.window.install_busy:
            tile.set_state("warn", human_size(free), tr("free on this drive, about 150 GB needed"))

    def _refresh_live_space(self) -> None:
        """The Free space tiles, while an install fills the drive."""
        profile = self.window.settings.active_profile
        if not self.isVisible() or profile is None:
            return
        if getattr(self.window, "install_operation", None) not in ("anomaly", "gamma"):
            return
        self._refresh_auto_space()
        _space_tile(self.anomaly_tiles["space"], profile.anomaly)
        _space_tile(self.gamma_tiles["space"], profile.gamma)

    def _set_auto_tile(self, key: str, state: str, text: str) -> None:
        """Mirror a step's status (from its InstallStatusRow) on the
        Automatic card's tile for it."""
        tile_state, caption = {
            "ok": ("ok", tr("Done")),
            "busy": ("running", tr("Running now")),
            "warn": ("warn", tr("Picks up where it stopped")),
        }.get(state, ("idle", ""))
        self.auto_tiles[key].set_state(tile_state, text, caption)

    def _reset_install_options(self) -> None:
        """Put the Anomaly and GAMMA options back to their defaults:
        Automatic always installs with them. Offline first, since turning
        it off puts back the boxes it had cleared."""
        boxes = {"offline": self.checkboxes["offline"], **self.checkboxes, **self.anomaly_checkboxes}
        for key, cb in boxes.items():
            cb.setChecked(key in _DEFAULT_CHECKED_OPTIONS)

    def _start_automatic_install(self) -> None:
        """Automatic: make the folders, then the dependencies, Anomaly and
        GAMMA one after another with the default options. Each runs only
        if it isn't installed yet."""
        if self.window.install_busy:
            QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
            return
        self._refresh_auto_choice()
        if not self._auto_choice.isEnabled():
            # GAMMA got installed while this card was open.
            self._show_view("chooser")
            return
        if game_running(force=True):
            self._warn_game_open()
            return
        root = self.auto_root_edit.text().strip()
        if not root:
            self._browse_auto_root()
            root = self.auto_root_edit.text().strip()
            if not root:
                return
        offline = self.auto_offline.isChecked()
        if offline:
            cache = str(Path(root).expanduser() / "cache")
            problem = offline_install_problem(offline_list_paths(cache), cache)
            if problem:
                QMessageBox.warning(self, tr("Offline install"), problem)
                return
        # Offline: the cache is already on disk, only the install remains.
        required = 100 if offline else 150
        free = free_space_bytes(root)
        note = None
        if self._resume_state is not None:
            note = tr("This picks up the install where it stopped. Mods that are already installed are skipped.")
        elif free is not None and free < required * 1024**3:
            note = tr(
                "Only {free} is free on this drive. This install needs about {required} GB.",
                free=human_size(free),
                required=required,
            )
        answer = QMessageBox.question(
            self,
            tr("Install GAMMA automatically?"),
            tr("COMMANDER installs the Windows runtimes, then Anomaly, then GAMMA, into:\n{path}\n\nThis can take a few hours. You can keep using your computer meanwhile.", path=root),
            (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
            note=note,
            labels={QMessageBox.StandardButton.Yes: tr("Install now"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.root_card_edit.setText(root)
        if not self._create_install_folders(quiet=True):
            return
        self._reset_install_options()
        # Ticked after the reset: it switches off the download-only options
        # and fills in the two list files found beside the cache.
        self.checkboxes["offline"].setChecked(offline)
        self._auto_active = True
        if self._wt_installed is True:
            self._start_auto_gamma()
        else:
            self._start_winetricks(then_install=True)

    def _start_auto_gamma(self) -> None:
        """Automatic, after the dependencies: Anomaly (if missing), then GAMMA."""
        profile = self.window.settings.active_profile
        include_anomaly = profile is not None and not anomaly_installed(profile.anomaly)
        if not self.start_auto_install(include_anomaly=include_anomaly):
            self._auto_active = False
            QMessageBox.warning(
                self,
                tr("Couldn't start the install"),
                tr("Another install is still running, or no profile is selected. Check the Profiles page, then try again."),
            )

    def _show_step(self, index: int) -> None:
        """Show one step's page on the Install card and mark it on the track."""
        index = max(0, min(index, len(self._steps) - 1))
        self._step = index
        for i, step in enumerate(self._steps):
            step.set_selected(i == index)
        self._page_stack.setCurrentIndex(index)
        self._action_stack.setCurrentIndex(index)
        for i, extra in enumerate(self._step_extras):
            # The Dependencies console is _refresh_wt_extra's: showing it
            # here first, to hide it again there, left its gap on screen
            # for a frame or two.
            if extra is not None and extra is not self.wt_progress:
                extra.setVisible(i == index)
        self._refresh_wt_extra()
        self._place_progress()
        # Nowhere to go before the first step or after the last: no button.
        # On step 1, Back goes to the Automatic / Manual choice.
        self.step_back_button.setVisible(True)
        self.step_next_button.setVisible(index < len(self._steps) - 1)
        self.step_play_button.setVisible(index == len(self._steps) - 1)

    def _go_to_step(self, index: int) -> None:
        """A click on the track or Back/Next: fade the step out and the new
        one in, or switch straight away with Animations off."""
        index = max(0, min(index, len(self._steps) - 1))
        for i, step in enumerate(self._steps):
            step.set_selected(i == index)  # the track answers the click at once
        if index == self._step and self._fade_target is None:
            return
        if not gui_settings.animations_enabled() or not self.isVisible():
            self._fade.stop()
            self._on_fade_finished()
            self._show_step(index)
            return
        self._fade_target = index
        if self._fade.state() != QVariantAnimation.State.Running:
            self._fade_effect.setEnabled(True)
            self._fade.start()

    def _on_fade_value(self, value: float) -> None:
        if value >= 1.0 and self._fade_target is not None:
            self._show_step(self._fade_target)
            self._fade_target = None
        self._fade_effect.setOpacity(abs(1.0 - value))

    def _on_fade_finished(self) -> None:
        if self._fade_target is not None:  # a click landed after the switch
            self._show_step(self._fade_target)
            self._fade_target = None
        self._fade_effect.setOpacity(1.0)
        self._fade_effect.setEnabled(False)

    def _open_play_page(self) -> None:
        from .main_window import _TAB_INDEX  # deferred: avoids an import cycle

        self.window.tabs.setCurrentIndex(_TAB_INDEX["play"])

    def _update_chain_note(self) -> None:
        """Step 3's line naming the step 4 options the chained GAMMA install uses."""
        picked = [cb.text() for cb in self.checkboxes.values() if cb.isChecked()]
        self.gamma_chain_note.setText(
            tr("GAMMA installs right after, with these options from step 4: {options}.", options=", ".join(picked))
            if picked
            else tr("GAMMA installs right after, with no options ticked on step 4.")
        )
        self.gamma_chain_note.setVisible(self.anomaly_checkboxes["auto_continue_gamma"].isChecked())

    def _refresh_folder_tiles(self, profile) -> None:
        """Step 1's tiles: each install folder, and free space on its drive.

        A picked Installation directory that differs from the saved folders
        is previewed: the folders Create folders would make there."""
        paths = {}
        parent = None
        if profile is not None:
            paths = {"anomaly": profile.anomaly, "gamma": profile.gamma, "cache": profile.cache}
            # Show the base folder the three were made in, if they share one.
            parents = {str(Path(p).expanduser().parent) for p in paths.values() if p}
            if len(parents) == 1 and all(paths.values()):
                parent = parents.pop()
                if not self.root_card_edit.text():
                    self.root_card_edit.setText(parent)
        root = self.root_card_edit.text().strip()
        preview = bool(root) and str(Path(root).expanduser()) != parent
        if preview:
            paths = {key: str(Path(root).expanduser() / key) for key in ("anomaly", "gamma", "cache")}
        for key in ("anomaly", "gamma", "cache"):
            path = paths.get(key, "")
            if not path:
                self.folder_tiles[key].set_state("idle", "-", tr("Not set"))
            elif Path(path).expanduser().is_dir():
                self.folder_tiles[key].set_state("ok", tr("Ready"), path)
            elif preview:
                self.folder_tiles[key].set_state("info", tr("Not created"), path)
            else:
                self.folder_tiles[key].set_state("warn", tr("Missing"), path)
        self._refresh_storage(paths, preview)
        free = free_space_bytes(paths["gamma"]) if paths.get("gamma") else None
        if free is None:
            self.folder_tiles["space"].set_state("idle", "-", tr("Choose a folder first"))
            return
        # Room for a fresh install; an installed GAMMA already has its room.
        roomy = free >= _INSTALL_SPACE_BYTES or (
            profile is not None and gamma_installed(paths["gamma"], profile.mo2_profile)
        )
        self.folder_tiles["space"].set_state(
            "ok" if roomy else "warn", human_size(free), tr("free on this drive, about 150 GB needed")
        )

    def _refresh_storage(self, paths: dict, preview: bool) -> None:
        """Step 1's Storage usage: what the install folders take, as the
        Dashboard last measured them (no folder walk here), and the drive's
        free space - the Dashboard's own bar, legend and meter."""
        layout = self.storage_group.layout()
        clear_layout(layout)
        stored = gui_settings.load_gui_settings().get("storage_sizes") or {}
        key = [paths.get(k, "") for k in ("anomaly", "gamma", "cache")]
        sizes = stored.get("sizes") if stored.get("key") == key and not preview else None
        if isinstance(sizes, dict) and sizes:
            segments = [(value, _SEGMENT_TOKENS.get(name, "storage_c")) for name, value in sizes.items()]
            layout.addWidget(_SegmentedBar(segments))
            legend = reflow.mark_row(QHBoxLayout())
            legend.setSpacing(18)
            for (name, value), (_value, token) in zip(sizes.items(), segments, strict=True):
                item = QHBoxLayout()
                item.setSpacing(6)
                item.addWidget(_Swatch(token), 0, Qt.AlignmentFlag.AlignVCenter)
                item.addWidget(QLabel(tr("{key}: {arg}", key=name, arg=human_size(value))))
                legend.addLayout(item)
            legend.addStretch(1)
            layout.addLayout(legend)
        usage = disk_usage_bytes(paths["gamma"]) if paths.get("gamma") else None
        if usage is None or usage[0] <= 0:
            self.storage_group.setVisible(bool(sizes))
            return
        self.storage_group.show()
        total, free = usage
        used = round(100 * (total - free) / total)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Free on drive: {free} of {total}", free=human_size(free), total=human_size(total))))
        row.addStretch(1)
        percent = QLabel(tr("{percent}% used", percent=used))
        percent.setObjectName("dim")
        row.addWidget(percent)
        layout.addLayout(row)
        meter = QProgressBar()
        meter.setTextVisible(False)
        meter.setRange(0, 100)
        meter.setValue(used)
        meter.setObjectName("storageMeterLow" if free < _INSTALL_SPACE_BYTES else "storageMeter")
        layout.addWidget(meter)

    def _refresh_install_tiles(self) -> None:
        """The Anomaly and GAMMA steps' tiles: what is installed where, the
        download the next install uses, and room on each drive."""
        profile = self.window.settings.active_profile
        if profile is None:
            for tile in (*self.anomaly_tiles.values(), *self.gamma_tiles.values()):
                tile.set_state("idle", "-", tr("Not set"))
            return
        anomaly = anomaly_installed(profile.anomaly)
        self.anomaly_tiles["version"].set_state(
            "ok" if anomaly else "idle", "1.5.3", tr("STALKER Anomaly")
        )
        if self.checkboxes["offline"].isChecked():
            download = ("Offline", "Unpacked from the download cache")
        elif self.anomaly_checkboxes["anomaly_mirror"].isChecked():
            download = ("Mirror", "Faster, falls back to ModDB")
        else:
            download = ("ModDB", "Straight from ModDB")
        self.anomaly_tiles["download"].set_state("ok", tr(download[0]), tr(download[1]))
        counts = count_active_mods(profile.gamma, profile.mo2_profile)
        if counts is None:
            self.gamma_tiles["mods"].set_state("idle", "-", tr("Not installed"))
        else:
            self.gamma_tiles["mods"].set_state(
                "ok", str(counts[0]), tr("of {total} mods enabled", total=counts[1])
            )
        archives = count_cached_archives(profile.cache)
        self.gamma_tiles["cache"].set_state(
            "ok" if archives else "idle", str(archives), tr("archives ready to reuse")
        )
        for tiles, path in ((self.anomaly_tiles, profile.anomaly), (self.gamma_tiles, profile.gamma)):
            _folder_tile(tiles["folder"], path)
            _space_tile(tiles["space"], path)

    def _refresh_deps_tiles(self) -> None:
        """Step 2's tiles and verb chips, from the last dependency check."""
        verbs = self._wt_verbs
        if self._wt_installed is True and verbs is None:
            verbs = dict.fromkeys((*WINETRICKS_VERBS, "umu"), True)  # paused while the game runs
        if verbs:
            done = sum(1 for ok in verbs.values() if ok)
            self.deps_tiles["runtimes"].set_state(
                "ok" if done == len(verbs) else "warn",
                f"{done}/{len(verbs)}",
                tr("Windows runtimes in the prefix"),
            )
        else:
            self.deps_tiles["runtimes"].set_state("idle", "-", self.wt_status._status.text())
        for key, row in self.deps_rows.items():
            found = verbs.get(key) if verbs else None
            row.set_state("idle" if found is None else "ok" if found else "problem")
        # System Wine is only checked, and needed, when umu-run is missing.
        self.deps_rows["wine"].setVisible(bool(verbs) and "wine" in verbs)
        prefix = self._wt_prefix()
        _space_tile(self.deps_tiles["space"], prefix)
        if prefix and Path(prefix).is_dir():
            self.deps_tiles["prefix"].set_state("ok", tr("Ready"), prefix)
        else:
            self.deps_tiles["prefix"].set_state("warn", tr("Missing"), prefix or tr("Not set"))
        try:
            runner = configured_runner()
        except LaunchError as exc:
            self.deps_tiles["runner"].set_state("warn", tr("Missing"), str(exc))
        else:
            # A tick like its neighbours: an "i" badge read as clickable.
            self.deps_tiles["runner"].set_state(
                "ok", runner.label, tr("Runs the game and installs these runtimes")
            )

    def _update_button_states(self):
        busy = self.window.install_busy
        self.root_card_edit.setEnabled(not busy)
        self.create_folders_button.setEnabled(not busy)
        self.auto_root_edit.setEnabled(not busy)
        self.auto_offline.setEnabled(not busy)
        self.auto_start_button.setEnabled(not busy)
        # While Automatic runs its card is only the tiles and the progress.
        auto_running = busy and self._auto_active
        self._auto_setup.setVisible(not auto_running)
        self.auto_start_button.setVisible(not auto_running)
        profile = self.window.settings.active_profile
        resumable = profile is not None and self._resume_state is not None
        reinstall = (
            not resumable
            and profile is not None
            and gamma_installed(profile.gamma, profile.mo2_profile)
        )
        self.auto_start_button.setText(
            tr(_RESUME_BUTTON_LABEL) if resumable else tr("Start install")
        )
        if resumable:
            label = tr(_RESUME_BUTTON_LABEL)
            tooltip = tr("Resume the install where it stopped. Installed mods are skipped.")
        elif reinstall:
            label = tr("Reinstall GAMMA offline") if self._offline_lists() else tr("Reinstall GAMMA")
            tooltip = tr(
                "Unpack every GAMMA mod again over your install to fix broken mods."
            )
        else:
            label = tr("Install GAMMA offline") if self._offline_lists() else tr("Install GAMMA")
            tooltip = tr("Install or update GAMMA. Anomaly is installed first if it is missing.")
        self.install_button.setText(label)
        self.install_button.setToolTip(tooltip)
        if busy or profile is None:
            self.anomaly_button.setEnabled(False)
            self.install_button.setEnabled(False)
            self.verify_button.setEnabled(False)
            self.repair_button.setEnabled(False)
            self.winetricks_button.setEnabled(False)
            self.anomaly_edit.setEnabled(False)
            self.gamma_edit.setEnabled(False)
            self.cache_edit.setEnabled(False)
            for cb in self.checkboxes.values():
                cb.setEnabled(False)
            for cb in self.anomaly_checkboxes.values():
                cb.setEnabled(False)
            return
        anomaly = anomaly_installed(profile.anomaly)
        offline = self.checkboxes["offline"].isChecked()
        # `anomaly install` has no offline mode; the offline full install
        # unpacks Anomaly from the cache together with GAMMA.
        self.anomaly_button.setText(
            tr("Retry Anomaly") if self._anomaly_failed
            else tr("Reinstall Anomaly") if anomaly
            else tr("Install Anomaly")
        )
        # A reinstall reuses the archive already in the cache, so Offline
        # only blocks a first install.
        self.anomaly_button.setEnabled(anomaly or not offline)
        self.anomaly_button.setToolTip(tr(_ANOMALY_OFFLINE_NOTE) if offline and not anomaly else "")
        self.anomaly_offline_note.setVisible(offline and not anomaly)
        # Stays available once installed, as Reinstall: it is the only way
        # to re-unpack mods or apply "Re-download GitHub addons".
        self.install_button.setEnabled(True)
        self.verify_button.setEnabled(not game_running() and not VERIFY_INTEGRITY_DISABLED)
        self.repair_button.setEnabled(not game_running())
        # Offered whenever the status is known: "Install" when something is
        # missing, "Reinstall" (winetricks --force) when it is there or half there.
        self.winetricks_button.setText(
            tr("Install Dependencies") if self._wt_installed is not True and not self._wt_partial
            else tr("Reinstall Dependencies")
        )
        self.winetricks_button.setEnabled(
            self._wt_installed is not None and not game_running()
        )
        self.anomaly_edit.setEnabled(True)
        self.gamma_edit.setEnabled(True)
        self.cache_edit.setEnabled(True)
        for cb in self.checkboxes.values():
            cb.setEnabled(True)
        for cb in self.anomaly_checkboxes.values():
            cb.setEnabled(True)
        self._on_offline_toggled(self.checkboxes["offline"].isChecked())

    def _update_install_status(self):
        profile = self.window.settings.active_profile
        if profile is not None and all((profile.anomaly, profile.gamma, profile.cache)):
            self._steps[0].set_status("ok", tr("Selected"))
        else:
            self._steps[0].set_status("missing", tr("Not set"))
        self._refresh_folder_tiles(profile)
        self._refresh_install_tiles()
        if profile is None:
            self.anomaly_status.set_state(None)
            self.gamma_status.set_state(None)
            return
        op = getattr(self.window, "install_operation", None)
        anomaly_state = display_state(anomaly_installed(profile.anomaly), op, "anomaly")
        gamma_state = display_state(gamma_installed(profile.gamma, profile.mo2_profile), op, "gamma")
        if anomaly_state == "installing":
            self.anomaly_status.set_installing()
        elif anomaly_state and self._anomaly_failed:
            self.anomaly_status.set_incomplete(
                tr("Last install attempt failed - retry it to finish.")
            )
        else:
            self.anomaly_status.set_state(bool(anomaly_state))
        if gamma_state == "installing":
            self.gamma_status.set_installing()
        elif gamma_state and self._resume_state is not None:
            self.gamma_status.set_incomplete(
                tr("Last install attempt failed - resume it to finish.")
            )
        else:
            self.gamma_status.set_state(bool(gamma_state))

    def _browse_install_root(self):
        if self._pick_folder(self.root_card_edit, tr("Select install root directory")):
            self._refresh_folder_tiles(self.window.settings.active_profile)

    def _on_app_state_changed(self, state) -> None:
        """Back in COMMANDER (say from a file manager): re-check the folders,
        free space, install states and Wine prefix, which only take a few
        file lookups, and the runtimes while the Dependencies step is open."""
        if state == Qt.ApplicationState.ApplicationActive and self.isVisible():
            self._update_install_status()
            self._refresh_deps_tiles()
            self._refresh_verify_age()
            # The runtimes check runs winetricks in the background: only
            # while its step is open.
            if self._step == 1:
                self._refresh_winetricks_status()

    def _create_install_folders(self, quiet: bool = False) -> bool:
        """Create the anomaly/gamma/cache folders under the chosen install root.

        When no root is entered, prompt the user to pick one. Always fills and
        persists the three per-step folder paths so installs just work.
        ``quiet`` skips the "folders ready" pop-up; True when they're set.
        """
        if self.window.install_busy:
            self._update_button_states()
            return False
        root = self.root_card_edit.text().strip()
        if not root:
            root = QFileDialog.getExistingDirectory(
                self, tr("Select install root directory"), str(Path.home())
            )
            if not root:
                return False
            self.root_card_edit.setText(root)
        base = Path(root).expanduser()
        if bad := wine_unsafe_folder(str(base)):
            warn_wine_unsafe_folder(self, bad)
            return False
        folders = {
            "Anomaly folder": base / "anomaly",
            "GAMMA folder": base / "gamma",
            "Cache folder": base / "cache",
        }
        for folder in folders.values():
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    tr("Couldn't create a folder"),
                    tr("COMMANDER couldn't create this folder:\n{folder}\n\n{exc}", folder=folder, exc=exc),
                )
                return False
        self.anomaly_edit.setText(str(folders["Anomaly folder"]))
        self.gamma_edit.setText(str(folders["GAMMA folder"]))
        self.cache_edit.setText(str(folders["Cache folder"]))
        if not self._persist_dirs():
            return False
        self.window.statusBar().showMessage(
            tr("Created and set install folders: Anomaly, GAMMA, cache"), 6000
        )
        if quiet:
            return True
        # Creating the folders used to be silent apart from the status bar,
        # and people could not tell whether it had worked.
        QMessageBox.information(
            self,
            tr("Install folders ready"),
            tr("COMMANDER created these folders and will use them for steps 3 and 4:"),
            points=(
                tr("Anomaly: {path}", path=str(folders["Anomaly folder"])),
                tr("GAMMA: {path}", path=str(folders["GAMMA folder"])),
                tr("Cache: {path}", path=str(folders["Cache folder"])),
            ),
        )
        return True

    def _make_folder_row(self, label_text, on_browse, placeholder="Click to choose a folder"):
        edit = _PathEdit()
        edit.setPlaceholderText(tr(placeholder))
        edit.setToolTip(tr("Click to choose a folder"))
        edit.clicked.connect(on_browse)
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        row.addWidget(edit, 1)
        return (edit, row)

    def _pick_folder(self, edit, title) -> bool:
        """Folder picker opened at the box's current folder; True if picked."""
        start = edit.text() if Path(edit.text()).is_dir() else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, title, start)
        if folder:
            edit.setText(folder)
        return bool(folder)

    def _browse_offline_list(self, key):
        edit = self.offline_edits[0 if key == "offline_modpack_list" else 1]
        start = edit.text() or str(Path.home())
        path, _filter = QFileDialog.getOpenFileName(
            self, tr("Select list file"), start, tr("Text files (*.txt);;All files (*)")
        )
        if not path:
            return
        changes = {key: path}
        # Both lists are published side by side: fill in the other one too.
        for other_key, name, other_edit in (
            ("offline_modpack_list", "modpack_maker_list.txt", self.offline_edits[0]),
            ("offline_mod_list", "modlist.txt", self.offline_edits[1]),
        ):
            sibling = Path(path).with_name(name)
            if other_key != key and sibling.is_file():
                changes[other_key] = str(sibling)
                other_edit.setText(str(sibling))
        edit.setText(path)
        gui_settings.save_gui_settings(**changes)

    def _fill_offline_lists(self, profile) -> None:
        for edit, path in zip(self.offline_edits, offline_list_paths(profile.cache)):
            edit.setText(path)
        self.offline_hint.setText(
            tr(
                "Extract the downloaded cache archive into {parent}. COMMANDER finds the two list files it unpacks there. If they are somewhere else, choose them below.",
                parent=str(Path(profile.cache).expanduser().parent),
            )
        )

    def _on_offline_toggled(self, offline: bool):
        self.offline_box.setVisible(offline)
        profile = self.window.settings.active_profile
        if offline and profile is not None:
            # Re-detect: the archive may have been extracted since the page loaded.
            self._fill_offline_lists(profile)
        # Options that only matter for a download, or that chain into the
        # online `anomaly install`: off (and put back later) while offline.
        boxes = {key: self.checkboxes[key] for key in _OFFLINE_DISABLES}
        boxes["auto_continue_gamma"] = self.anomaly_checkboxes["auto_continue_gamma"]
        boxes["anomaly_mirror"] = self.anomaly_checkboxes["anomaly_mirror"]
        usable = self.checkboxes["offline"].isEnabled() and not offline
        for key, cb in boxes.items():
            if offline and key not in self._offline_saved:
                self._offline_saved[key] = cb.isChecked()
                cb.setChecked(False)
            elif not offline and key in self._offline_saved:
                cb.setChecked(self._offline_saved.pop(key))
            cb.setEnabled(usable)

    def _offline_lists(self) -> tuple[str, str] | None:
        if not self.checkboxes["offline"].isChecked():
            return None
        return (self.offline_edits[0].text(), self.offline_edits[1].text())

    def _browse_anomaly(self):
        if self._pick_folder(self.anomaly_edit, tr("Select Anomaly install folder")):
            self._persist_dirs()

    def _browse_gamma(self):
        if self._pick_folder(self.gamma_edit, tr("Select GAMMA install folder")):
            self._persist_dirs()

    def _browse_cache(self):
        if self._pick_folder(self.cache_edit, tr("Select cache folder")):
            self._persist_dirs()

    def _persist_dirs(self) -> bool:
        """Save the three folder fields to the active profile.

        True when the profile now holds exactly these folders (saved, or
        already the same); False when nothing was saved - the reason has
        already been shown.
        """
        # editingFinished fires on focus-out, and the message boxes below steal
        # focus - without this guard the handler re-enters itself.
        if self.window.install_busy:
            self._update_button_states()
            return False
        if self._persisting:
            return False
        self._persisting = True
        try:
            return self._persist_dirs_locked()
        finally:
            self._persisting = False

    def _persist_dirs_locked(self) -> bool:
        if self.window.install_busy:
            self._update_button_states()
            return False
        self.window.refresh_settings()
        profile = self.window.settings.active_profile
        if profile is None:
            QMessageBox.warning(
                self,
                tr("No profile selected"),
                tr("Create or pick a profile on the Profiles page first."),
            )
            self.refresh()
            return False
        anomaly = normalize_path(self.anomaly_edit.text())
        gamma = normalize_path(self.gamma_edit.text())
        cache = normalize_path(self.cache_edit.text())
        self.anomaly_edit.setText(anomaly)
        self.gamma_edit.setText(gamma)
        self.cache_edit.setText(cache)
        if not anomaly or not gamma:
            QMessageBox.warning(
                self,
                tr("Pick both install folders"),
                tr("The Anomaly and GAMMA folders can't be empty, so COMMANDER put the saved ones back."),
            )
            self.refresh()
            return False
        if not cache:
            QMessageBox.warning(
                self,
                tr("Pick a cache folder"),
                tr("The cache folder can't be empty, so COMMANDER put the saved one back."),
            )
            self.refresh()
            return False
        # Not the cache: only the CLI reads it, never Wine.
        if bad := wine_unsafe_folder(anomaly, gamma):
            warn_wine_unsafe_folder(self, bad)
            self.refresh()
            return False
        if (
            anomaly == profile.anomaly
            and gamma == profile.gamma
            and cache == profile.cache
        ):
            return True
        try:
            save_profile_dirs(self.window.settings, profile, anomaly, gamma, cache)
        except OSError as exc:
            QMessageBox.warning(
                self, tr("Couldn't save the profile"), tr("COMMANDER couldn't write its settings file (settings.json).\n\n{exc}", exc=exc)
            )
            self.refresh()
            return False
        # Same refresh a page switch does, so the install buttons and
        # status rows match the new folders straight away.
        self.refresh()
        self.window.statusBar().showMessage(
            tr("Install folders updated: {anomaly} | {gamma} | {cache}", anomaly=anomaly, gamma=gamma, cache=cache),
            6000,
        )
        return True

    def _build_full_command(
        self,
        skip_extract_on_hash_match: bool = False,
        preserve_user: bool | None = None,
        preserve_mcm: bool | None = None,
    ):
        if preserve_user is None:
            preserve_user = self.checkboxes["preserve_user"].isChecked()
        if preserve_mcm is None:
            preserve_mcm = self.checkboxes["preserve_mcm"].isChecked()
        return _full_install_args(
            self.checkboxes["minimal"].isChecked(),
            preserve_user,
            preserve_mcm,
            skip_extract_on_hash_match,
            not self.checkboxes["redownload_github"].isChecked(),
            self._offline_lists(),
            self.anomaly_checkboxes["anomaly_mirror"].isChecked(),
        )

    def _anomaly_install_args(self) -> list[str]:
        args = ["anomaly", "install"]
        if self.anomaly_checkboxes["anomaly_mirror"].isChecked():
            args.append("--use-anomaly-mirror")
        return args

    def _start_full_install(
        self,
        skip_confirm=False,
        preserve_user: bool | None = None,
        preserve_mcm: bool | None = None,
        _is_auto_retry: bool = False,
    ):
        if self._runner is not None and self._runner.is_running():
            return
        if self.window.install_busy and not skip_confirm:
            QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
            return
        if not _is_auto_retry:
            # Any fresh, user-initiated (or auto-chained-from-Anomaly) start
            # is not a continuation of a failure streak.
            self._auto_retry_count = 0
            self._replaced_archives = set()
        profile = self.window.settings.active_profile
        if profile is None:
            QMessageBox.warning(
                self,
                tr("No profile selected"),
                tr("Create or pick a profile on the Profiles page first."),
            )
            return
        if not skip_confirm and not confirm_singleplayer(self, profile, tr("installing GAMMA")):
            return
        offline_lists = self._offline_lists()
        if offline_lists is not None:
            problem = offline_install_problem(offline_lists, profile.cache)
            if problem:
                QMessageBox.warning(self, tr("Offline install"), problem)
                # A chained or retried start finds install_busy already set.
                self.window.set_install_busy(False)
                self._update_button_states()
                return
        minimal = self.checkboxes["minimal"].isChecked()
        size_hint = "~100 GB" if minimal else "~150 GB"
        if not skip_confirm:
            # Tell the user upfront whether this run will redownload Anomaly
            # or resume a prior interrupted install, rather than only
            # surfacing it as a status-bar toast after they've already
            # clicked Yes.
            points = []
            if self._resume_state is not None:
                points.append(tr("This picks up the install where it stopped. Mods that are already installed are skipped."))
            elif anomaly_installed(profile.anomaly):
                points.append(tr("Anomaly is already installed, so it isn't downloaded again."))
            points.append(tr("Your existing files are kept."))
            warnings = [
                tr("Your keybindings, controls (user.ltx) and MCM settings will be replaced, unless you ticked the options above to keep them.")
            ]
            # Offline: the cache is already on disk, only the install remains.
            required_gb = 100 if minimal or offline_lists else 150
            free = free_space_bytes(profile.gamma)
            if free is not None and free < required_gb * 1024**3:
                warnings.append(tr(
                    "Only {free} is free on this drive. This install needs about {required} GB.",
                    free=human_size(free),
                    required=required_gb,
                ))
            reinstall = self._resume_state is None and gamma_installed(
                profile.gamma, profile.mo2_profile
            )
            answer = QMessageBox.question(
                self,
                tr("Reinstall GAMMA?") if reinstall else tr("Install GAMMA?"),
                tr(
                    "COMMANDER unpacks every GAMMA mod again over your install. "
                    "Mods already in the download cache are reused, so only missing "
                    "ones are downloaded."
                )
                if reinstall and offline_lists is None
                else tr("COMMANDER installs GAMMA from your local cache. Nothing is downloaded.")
                if offline_lists is not None
                else tr("COMMANDER downloads and installs all GAMMA mods ({size_hint}), plus Anomaly if it isn't installed yet.", size_hint=size_hint),
                (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
                points=tuple(points),
                note="\n\n".join(warnings),
                labels={QMessageBox.StandardButton.Yes: tr("Install now"), QMessageBox.StandardButton.No: tr("Cancel")},
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            # install_busy may have flipped True while the dialog was open.
            if self.window.install_busy:
                QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
                return
        for name, path in (
            ("Anomaly folder", profile.anomaly),
            ("GAMMA folder", profile.gamma),
            ("Cache folder", profile.cache),
        ):
            try:
                Path(path).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    tr("Couldn't create a folder"),
                    tr("COMMANDER couldn't create this folder:\n{folder}\n\n{exc}", folder=path, exc=exc),
                )
                # An auto-retry re-entry finds install_busy already True
                # from the attempt that just failed - clearing it here is a
                # harmless no-op on a normal first call, but on a retry it's
                # the only thing that stops this from bricking the page
                # (no runner left to cancel, buttons stuck disabled).
                self.window.set_install_busy(False)
                self._update_button_states()
                return
        use_stock_mo2_profile_if_missing(self.window.settings, profile)
        backup_error = None
        if not _is_auto_retry and gamma_installed(profile.gamma, profile.mo2_profile):
            # Reinstalling over a working GAMMA can reset user.ltx and MCM
            # settings; keep a copy first (a few KB, so done in place).
            backup_error = backup_settings_before(profile, "reinstall")
        # Kept across failed and resumed attempts: a run that failed midway
        # may already have replaced the modlist, so capturing again on
        # resume would save the stock list instead of the user's.
        # An abandoned failed install leaves the exe copy in the Anomaly
        # folder until the next capture() clears it.
        if getattr(self, "_kept", None) is None and gamma_installed(
            profile.gamma, profile.mo2_profile
        ):
            self._kept = capture(
                profile, keep_exes=self.checkboxes["keep_engine_exes"].isChecked()
            )
        self.window.set_install_busy(True, "gamma")
        self.full_progress.reset()
        if backup_error:
            # full_progress has no log pane (show_log=False).
            self.window.statusBar().showMessage(
                tr("Settings backup failed: {error}", error=backup_error), 8000
            )
        self.full_progress.set_concurrency(profile.download_threads)
        self.install_button.setEnabled(False)
        self.anomaly_button.setEnabled(False)
        # Both Anomaly AND GAMMA must already be genuinely present before
        # trusting the cache to skip re-extraction - checking Anomaly alone
        # is wrong right after a GAMMA-only Reset (which wipes gamma/mods
        # but never the download cache): every already-cached, hash-valid
        # archive would then be silently skipped instead of re-extracted
        # into the now-empty mods folder, permanently losing those mods.
        skip_extract = anomaly_installed(profile.anomaly) and gamma_installed(
            profile.gamma, profile.mo2_profile
        )
        # Resolve and remember the actual preserve flags used for this run
        # (an explicit override, e.g. from a Reset dialog, or else the
        # Install page's own checkboxes) - an auto-retry re-enters this
        # method with _is_auto_retry=True and must reuse the exact same
        # resolved values, not silently fall back to the checkboxes.
        if preserve_user is None:
            preserve_user = self.checkboxes["preserve_user"].isChecked()
        if preserve_mcm is None:
            preserve_mcm = self.checkboxes["preserve_mcm"].isChecked()
        self._active_preserve_user = preserve_user
        self._active_preserve_mcm = preserve_mcm
        # Only a resume continues from resume.txt: a fresh install, Reset or
        # repair must unpack every mod again, whatever an old file says.
        resume = (
            cli_resume_args(profile.gamma, "full-install")
            if self._resume_state is not None
            else []
        )
        cmd = cli_command(
            self._build_full_command(
                skip_extract_on_hash_match=skip_extract,
                preserve_user=preserve_user,
                preserve_mcm=preserve_mcm,
            )
            + resume,
            progress_interval_ms=200,
        )
        if skip_extract:
            # full_progress has no console pane; use the status bar instead.
            self.window.statusBar().showMessage(
                tr("Anomaly and GAMMA already installed - skipping re-extract of unchanged files."),
                6000,
            )
        if self._resume_state is not None:
            self.window.statusBar().showMessage(
                tr("Resuming the GAMMA install. Mods that are already installed are skipped."),
                6000,
            )
        # Marked before it runs, not only on failure: closing COMMANDER
        # mid-install (paused or not) never reaches _on_full_finished.
        self._save_resume_state()
        self._runner = CommandRunner(cmd, parent=self)
        self._checked_archives = set()
        self._runner.line.connect(self._on_full_install_line)
        self._runner.finished.connect(self._on_full_finished)
        self._runner.cancelled.connect(
            lambda: self.full_progress.status_message(tr("Cancelled"))
        )
        self.full_progress.set_runner(self._runner)
        self.full_progress.on_started()
        self._runner.start()

    def _on_full_install_line(self, line):
        """Forward a full-install progress line and surface MD5 redownloads.

        ``full_progress`` has no console pane (``show_log=False``), so any
        transparency note goes to the status bar instead.
        """
        self.full_progress.on_line(line)
        event = parse_progress_line(strip_ansi(line))
        if event is None:
            return
        note = _note_md5_redownload(self._checked_archives, event)
        if note:
            self.window.statusBar().showMessage(note, 6000)

    def _on_full_finished(self, rc, output):
        cancelled = self._runner is not None and self._runner.was_cancelled
        # Clear the reference so later cancel paths cannot act on a dead runner.
        self._runner = None
        self.full_progress.on_finished(rc, output)
        self.full_progress.set_runner(None)
        if cancelled:
            self.full_progress.bar.setFormat(tr("Cancelled"))
            self.full_progress.status_message(tr("Cancelled"))
            self._save_resume_state()
            self._auto_retry_count = 0
        elif not cli_ok(rc, output, ""):
            self.full_progress.status_message(tr("Failed (exit code {rc})", rc=rc))
            self._save_resume_state()
            broken = _broken_archive(output)
            profile = self.window.settings.active_profile
            if (
                broken is not None
                and profile is not None
                and broken[0] not in self._replaced_archives
            ):
                action = _replace_broken_archive(*broken, profile.gamma)
                if action is not None:
                    self._replaced_archives.add(broken[0])
                    name = Path(broken[0]).name
                    if action == "renamed":
                        note = tr(
                            "Using the copy of {name} you downloaded - retrying",
                            name=name,
                        )
                    else:
                        note = tr(
                            "{name} was broken - downloading it again", name=name
                        )
                    self.window.statusBar().showMessage(note, 8000)
                    self._start_full_install(
                        skip_confirm=True,
                        preserve_user=self._active_preserve_user,
                        preserve_mcm=self._active_preserve_mcm,
                        _is_auto_retry=True,
                    )
                    return
            if _should_auto_retry(
                self.checkboxes["auto_retry_large_files"].isChecked(),
                self._auto_retry_count,
                output,
            ):
                self._auto_retry_count += 1
                self.window.statusBar().showMessage(
                    tr(
                        "A large file pack failed again - auto-retrying ({count}/{max})",
                        count=self._auto_retry_count,
                        max=_AUTO_RETRY_MAX,
                    ),
                    8000,
                )
                self._start_full_install(
                    skip_confirm=True,
                    preserve_user=self._active_preserve_user,
                    preserve_mcm=self._active_preserve_mcm,
                    _is_auto_retry=True,
                )
                return
            gave_up = self._auto_retry_count >= _AUTO_RETRY_MAX
            self._auto_retry_count = 0
            hint = tr(
                "Click \"{button}\" to continue - most of what already downloaded is reused. "
                "Some large files from GAMMA's GitHub repos aren't kept in the cache, so those download again.",
                button=tr(_RESUME_BUTTON_LABEL),
            )
            if gave_up:
                hint = (
                    tr(
                        "Auto-retry gave up after {max} attempts without success.",
                        max=_AUTO_RETRY_MAX,
                    )
                    + "\n\n"
                    + hint
                )
            if broken is not None and broken[0] in self._replaced_archives:
                hint = tr(
                    "{name} is still broken after downloading it again - GitHub may be "
                    "cutting the download off. Download it yourself from {url} and save "
                    "it as {path}, then click \"{button}\".",
                    name=Path(broken[0]).name,
                    url=broken[1],
                    path=broken[0],
                    button=tr(_RESUME_BUTTON_LABEL),
                )
            self._show_error_popup(tr("The GAMMA install didn't finish"), rc, output, resume_hint=hint)
        else:
            self._clear_resume_state()
            self._auto_retry_count = 0
            # A full install/reinstall (including one triggered by Fresh
            # Reset/GAMMA Reset) legitimately (re)writes every file under
            # gamma/mods - Verify Integrity's MD5 baseline must not
            # compare fresh files against a stale pre-install snapshot.
            kept, self._kept = getattr(self, "_kept", None), None
            if kept is not None:
                notes = restore(kept)
                if notes:
                    self.window.statusBar().showMessage(" ".join(notes), 8000)
            profile = self.window.settings.active_profile
            if profile is not None:
                invalidate_baseline(profile.gamma)
                note = apply_pending_settings_restore(profile)
                if note:
                    self.window.statusBar().showMessage(note, 8000)
        # Only for a run that's actually done (not cancelled - the user
        # already knows - and not a failure that's about to silently
        # auto-retry, handled by the early return above), and only while
        # the user isn't already looking at the window watching it finish.
        is_active = getattr(self.window, "isActiveWindow", lambda: True)()
        if not cancelled and not is_active:
            if cli_ok(rc, output, ""):
                notify_desktop(
                    tr("GAMMA install finished"),
                    tr("The GAMMA installation completed successfully."),
                )
            else:
                notify_desktop(
                    tr("GAMMA install failed"),
                    tr(
                        "Install failed with exit code {rc}. Open COMMANDER for details.",
                        rc=rc,
                    ),
                )
        # The topbar mod counter's "incomplete" warning depends on the
        # gamma_install_resume state just saved/cleared above - without
        # this, it would only pick that up whenever some other page
        # happened to refresh, not right when the install actually
        # finished/failed.
        self.window.refresh_settings()
        self._update_install_status()
        self.window.set_install_busy(False)
        self._update_button_states()
        if not cancelled and cli_ok(rc, output, ""):
            if self.verify_checkboxes["verify_after_gamma"].isChecked():
                self._show_step(4)
                QTimer.singleShot(0, self._start_verify)
            else:
                # Setup is done; the next thing to do is play.
                self.window.set_page("play")

    def _save_resume_state(self) -> None:
        profile = self.window.settings.active_profile
        if profile is None:
            return
        self._resume_state = {
            "profile": profile.profile_name,
            "anomaly": profile.anomaly,
            "gamma": profile.gamma,
            "cache": profile.cache,
            "auto": getattr(self, "_auto_active", False),
        }
        self._write_resume_state(self._resume_state)

    def _clear_resume_state(self) -> None:
        self._resume_state = None
        self._write_resume_state({})

    def _write_resume_state(self, state: dict) -> None:
        # Called from the install's finished handler before the install lock
        # is released: a failed write (a full disk is the usual reason an
        # install stops) must not raise out of it and leave the lock held.
        try:
            gui_settings.save_gui_settings(gamma_install_resume=state)
        except OSError as exc:
            self.window.statusBar().showMessage(
                tr("Could not save install progress: {exc}", exc=exc), 8000
            )

    def _active_install_runner(self) -> CommandRunner | None:
        """Whichever install is currently running on the shared console.

        Anomaly install, GAMMA install and the optional post-Anomaly
        verify step all share one progress console/cancel button, but
        never run at the same time (Anomaly first, then its optional
        verify, then GAMMA, when chained - see start_auto_install and
        _finish_anomaly_sequence) - at most one of these three runners
        can ever be running.
        """
        if self._runner is not None and self._runner.is_running():
            return self._runner
        if self._anomaly_runner is not None and self._anomaly_runner.is_running():
            return self._anomaly_runner
        if (
            self._post_anomaly_verify_runner is not None
            and self._post_anomaly_verify_runner.is_running()
        ):
            return self._post_anomaly_verify_runner
        return None

    def _cancel_full_install(self):
        runner = self._active_install_runner()
        if runner is None:
            return
        answer = QMessageBox.question(
            self,
            tr("Stop the install?"),
            tr("You can pick it up again later from where it stopped."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            points=(
                tr("Finished downloads stay in the cache and are reused."),
                tr("Each cached file is checked first, so a half-finished download is simply fetched again."),
                tr("Mods that are already installed are skipped when you resume."),
            ),
            labels={QMessageBox.StandardButton.Yes: tr("Stop install"), QMessageBox.StandardButton.No: tr("Keep installing")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            if self._wt_dialog is not None:
                self._wt_dialog.resume()
            return
        # The install may have finished while the dialog was open, clearing
        # the runner - re-check before touching it, or this crashes.
        runner = self._active_install_runner()
        if runner is None:
            return
        if self.full_progress.is_paused:
            runner.resume()
        self.full_progress.cancel_button.setEnabled(False)
        self.full_progress.cancel_button.setText(tr("Cancelling"))
        self.full_progress.status_message(tr("Cancelling installation"))
        runner.cancel()

    def _on_anomaly_button_clicked(self) -> None:
        profile = self.window.settings.active_profile
        if profile is not None and not self._anomaly_failed and anomaly_installed(profile.anomaly):
            self._start_anomaly_reinstall()
        else:
            self._start_anomaly_install()

    def _start_anomaly_reinstall(self) -> None:
        """Unpack Anomaly again over the install, then reinstall GAMMA.

        `anomaly install` puts stock Anomaly back over GAMMA's own files in
        the Anomaly folder (its engine exes among them), so GAMMA has to go
        back on top, as Verify's repair does. The archive has no saves or
        user.ltx, so those are only touched when the user unticks Keep.
        """
        if self.window.install_busy:
            QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
            return
        if game_running():
            QMessageBox.information(self, tr("Close the game first"), tr("Anomaly can't be reinstalled while the game is running."))
            return
        profile = self.window.settings.active_profile
        with_gamma = gamma_installed(profile.gamma, profile.mo2_profile)
        if not confirm_singleplayer(self, profile, tr("reinstalling Anomaly")):
            return
        options = QWidget()
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(0, 0, 0, 0)
        keep_saves = QCheckBox(tr("Keep saves"))
        keep_saves.setChecked(True)
        keep_settings = QCheckBox(tr("Keep user.ltx and MCM settings"))
        keep_settings.setChecked(True)
        keep_settings.setToolTip(
            tr("Keep your game options, keybindings and MCM settings during the reinstall.")
        )
        options_layout.addWidget(keep_saves)
        options_layout.addWidget(keep_settings)
        points = [tr("The Anomaly archive in the download cache is reused if it is still valid.")]
        if with_gamma:
            points.append(tr("GAMMA is reinstalled right after, so its own files go back on top of Anomaly."))
        points.append(tr("Files you added to the Anomaly folder yourself stay. For a clean install, use Fresh Reset on the Utilities page."))
        answer = QMessageBox.question(
            self,
            tr("Reinstall Anomaly?"),
            tr("COMMANDER unpacks STALKER Anomaly 1.5.3 again over your install."),
            (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
            points=tuple(points),
            extra=options,
            info=tr("A backup of your saves, user.ltx and MCM settings is made first."),
            labels={QMessageBox.StandardButton.Yes: tr("Reinstall now"), QMessageBox.StandardButton.No: tr("Cancel")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # install_busy may have flipped True while the dialog was open.
        if self.window.install_busy:
            QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
            return
        message, ok = prepare_anomaly_reinstall(
            profile,
            keep_saves=keep_saves.isChecked(),
            keep_settings=keep_settings.isChecked(),
            with_gamma=with_gamma,
        )
        if not ok:
            QMessageBox.warning(self, tr("Anomaly wasn't reinstalled"), message)
            return
        if message:
            self.window.statusBar().showMessage(message, 8000)
        self._auto_chain = with_gamma
        self._auto_cancelled = False
        self._chain_preserve = keep_settings.isChecked() if with_gamma else None
        self._verify_anomaly_after_install = self._wants_anomaly_only_check()
        self._start_anomaly_install(skip_confirm=True, respect_options=False)

    def _wants_anomaly_only_check(self) -> bool:
        """"Check Anomaly files after install", unless Anomaly goes straight
        on to GAMMA and the full check after GAMMA covers it anyway.
        Call after _auto_chain is set."""
        if not self.anomaly_checkboxes["verify_after_install"].isChecked():
            return False
        boxes = getattr(self, "verify_checkboxes", None)
        return not (self._auto_chain and boxes and boxes["verify_after_gamma"].isChecked())

    def _start_anomaly_install(self, skip_confirm=False, respect_options=True):
        if self._anomaly_runner is not None and self._anomaly_runner.is_running():
            return
        if self.window.install_busy and not skip_confirm:
            QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
            return
        profile = self.window.settings.active_profile
        if profile is None:
            QMessageBox.warning(
                self,
                tr("No profile selected"),
                tr("Create or pick a profile on the Profiles page first."),
            )
            return
        if not skip_confirm and not confirm_singleplayer(self, profile, tr("installing Anomaly")):
            return
        if not skip_confirm:
            warning = ""
            required_gb = 20
            free = free_space_bytes(profile.anomaly)
            if free is not None and free < required_gb * 1024**3:
                warning = tr(
                    "Only {free} is free on this drive. This install needs about {required} GB.",
                    free=human_size(free),
                    required=required_gb,
                )
            answer = QMessageBox.question(
                self,
                tr("Install Anomaly?"),
                tr("COMMANDER downloads and installs STALKER Anomaly 1.5.3, the game GAMMA is built on."),
                (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
                note=warning,
                labels={QMessageBox.StandardButton.Yes: tr("Install now"), QMessageBox.StandardButton.No: tr("Cancel")},
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            # install_busy may have flipped True while the dialog was open.
            if self.window.install_busy:
                QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
                return
        profile = self.window.settings.active_profile
        if profile is not None:
            try:
                Path(profile.anomaly).expanduser().mkdir(
                    parents=True, exist_ok=True
                )
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    tr("Couldn't create a folder"),
                    tr("COMMANDER couldn't create this folder:\n{folder}\n\n{exc}", folder=profile.anomaly, exc=exc),
                )
                return
        if respect_options:
            # A direct "Install Anomaly" click (not an internal auto-chain
            # call from start_auto_install/GAMMA Reset, which drives its
            # own chaining) - respect the Anomaly card's own checkboxes.
            self._auto_chain = self.anomaly_checkboxes["auto_continue_gamma"].isChecked()
            self._auto_cancelled = False
            self._verify_anomaly_after_install = self._wants_anomaly_only_check()
        self._anomaly_failed = False
        self._anomaly_failure = None
        self.window.set_install_busy(True, "anomaly")
        self.full_progress.reset()
        self.anomaly_button.setEnabled(False)
        self.install_button.setEnabled(False)
        cmd = cli_command(self._anomaly_install_args(), progress_interval_ms=200)
        self._anomaly_runner = CommandRunner(cmd, parent=self)
        self._anomaly_runner.line.connect(self.full_progress.on_line)
        self._anomaly_runner.finished.connect(self._on_anomaly_finished)
        self._anomaly_runner.cancelled.connect(
            lambda: self.full_progress.status_message(tr("Cancelled"))
        )
        self._anomaly_runner.cancelled.connect(self._on_anomaly_cancelled)
        self.full_progress.set_runner(self._anomaly_runner)
        self.full_progress.on_started()
        self._anomaly_runner.start()

    def start_auto_install(
        self,
        include_anomaly=True,
        preserve_user: bool = False,
        preserve_mcm: bool = False,
    ):
        if self._runner is not None and self._runner.is_running():
            return False
        if self._anomaly_runner is not None and self._anomaly_runner.is_running():
            return False
        if self.window.settings.active_profile is None:
            return False
        offline = self.checkboxes["offline"].isChecked()
        # Offline: `anomaly install` would download, so skip it - the offline
        # full install unpacks Anomaly from the cache along with GAMMA.
        self._auto_chain = include_anomaly and not offline
        self._auto_cancelled = False
        if include_anomaly and offline:
            self._start_full_install(skip_confirm=True)
        elif include_anomaly:
            self._start_anomaly_install(skip_confirm=True, respect_options=False)
        else:
            self._start_full_install(
                skip_confirm=True,
                preserve_user=preserve_user,
                preserve_mcm=preserve_mcm,
            )
        return True

    def _on_anomaly_cancelled(self):
        self._auto_cancelled = True

    def _on_anomaly_finished(self, rc, output):
        cancelled = self._auto_cancelled or (
            self._anomaly_runner is not None and self._anomaly_runner.was_cancelled
        )
        self._anomaly_runner = None
        self.full_progress.on_finished(rc, output)
        self.full_progress.set_runner(None)
        install_ok = not cancelled and cli_ok(rc, output, "")
        # Single-shot: consumed unconditionally on every outcome so a
        # failed/cancelled run cannot leave it armed for a later, unrelated
        # (e.g. Reset-driven) anomaly install that never asked for it.
        should_verify = self._verify_anomaly_after_install
        self._verify_anomaly_after_install = False
        if cancelled:
            self.full_progress.status_message(tr("Cancelled"))
        elif not install_ok:
            # The exit code alone can lie (the installer can crash on exit
            # after it finished), so check the files before calling it a
            # failure or holding GAMMA back.
            self._anomaly_failure = (rc, output)
            self._update_install_status()
            self._start_post_anomaly_verify()
            return
        self._update_install_status()
        if install_ok and should_verify:
            self._start_post_anomaly_verify()
            return
        self._finish_anomaly_sequence(cancelled, install_ok)

    def _start_post_anomaly_verify(self):
        """Optional extra step: re-run `anomaly check` right after a

        successful Anomaly install (see the "Check Anomaly files after install"
        checkbox) - a lightweight nicety, not a replacement for the full
        Verify Integrity pipeline.
        """
        # on_finished() (just called for the Anomaly install itself) hides
        # the cancel button - re-show it, or this step is uncancellable
        # even though _active_install_runner()/_cancel_full_install both
        # already know how to cancel it.
        self.full_progress.cancel_button.show()
        self.full_progress.cancel_button.setEnabled(True)
        self.full_progress.cancel_button.setText(tr("Cancel"))
        self.full_progress.status_message(tr("Verifying Anomaly files"))
        self._post_anomaly_verify_cancelled = False
        self._post_anomaly_verify_runner = CommandRunner(
            cli_command(["anomaly", "check"]), parent=self
        )
        self.full_progress.set_runner(self._post_anomaly_verify_runner)
        self._post_anomaly_verify_runner.finished.connect(
            self._on_post_anomaly_verify_finished
        )
        self._post_anomaly_verify_runner.cancelled.connect(
            self._on_post_anomaly_verify_cancelled
        )
        self._post_anomaly_verify_runner.start()

    def _on_post_anomaly_verify_cancelled(self) -> None:
        self._post_anomaly_verify_cancelled = True

    def _on_post_anomaly_verify_finished(self, rc, output):
        cancelled = self._post_anomaly_verify_cancelled or (
            self._post_anomaly_verify_runner is not None
            and self._post_anomaly_verify_runner.was_cancelled
        )
        self._post_anomaly_verify_cancelled = False
        self._post_anomaly_verify_runner = None
        self.full_progress.on_finished(rc, output)
        self.full_progress.set_runner(None)
        verified = not cancelled and cli_ok(rc, output, "")
        if cancelled:
            self.full_progress.status_message(tr("Cancelled"))
        elif verified:
            self.full_progress.status_message(
                tr("Anomaly files verified - no issues found.")
            )
        else:
            self.full_progress.status_message(
                tr("Anomaly verification found issues - see Verify Integrity for details.")
            )
        if getattr(self, "_anomaly_failure", None) is not None:
            # The install itself exited badly: only files that check out
            # and are really there overrule that. A cancelled check proves
            # nothing, so the failure still stands.
            profile = self.window.settings.active_profile
            ok = verified and profile is not None and anomaly_installed(profile.anomaly)
            self._finish_anomaly_sequence(False, ok)
            return
        self._finish_anomaly_sequence(cancelled, verified or cancelled)

    def _offer_anomaly_retry(self, chain: bool) -> None:
        """Anomaly is broken: GAMMA was not started - offer a retry."""
        failure, self._anomaly_failure = self._anomaly_failure, None
        if failure is not None:
            summary, detail = _install_failure_message(
                failure[0], failure[1], None
            )
        else:
            summary = tr("Anomaly verification found issues - see Verify Integrity for details.")
            detail = ""
        if chain:
            summary = tr("GAMMA install was not started.") + "\n\n" + summary
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            tr("Anomaly didn't install correctly"),
            summary,
            QMessageBox.StandardButton.NoButton,
            self,
        )
        if detail:
            box.setDetailedText(detail)
        retry = box.addButton(tr("Retry Anomaly"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(retry)
        box.exec()
        if box.clickedButton() is retry:
            self._start_anomaly_install(skip_confirm=True)

    def _finish_anomaly_sequence(self, cancelled: bool, install_ok: bool) -> None:
        """Deliver the final outcome of an Anomaly install (and its

        optional post-install verify), continuing into GAMMA if the
        "Continue to GAMMA install automatically" checkbox armed it.
        """
        chain = self._auto_chain
        # The chain is single-shot: clear it on every outcome so a failed
        # anomaly install cannot leave it armed for a later manual run.
        self._auto_chain = False
        self._anomaly_failed = not cancelled and not install_ok
        if cancelled:
            self._anomaly_failure = None
        if chain and not cancelled and install_ok:
            self._anomaly_failure = None
            self.full_progress.status_message(
                tr("Anomaly installed. Starting GAMMA install")
            )
            preserve = getattr(self, "_chain_preserve", None)
            self._chain_preserve = None
            if preserve is None:
                self._start_full_install(skip_confirm=True)
            else:
                self._start_full_install(
                    skip_confirm=True, preserve_user=preserve, preserve_mcm=preserve
                )
            return
        self._chain_preserve = None
        # Only for a run that's actually done (not chaining into GAMMA,
        # where _on_full_finished's own notification covers the eventual
        # outcome instead) and not cancelled - the user already knows -
        # and only while the user isn't already looking at the window
        # watching it finish.
        is_active = getattr(self.window, "isActiveWindow", lambda: True)()
        if not cancelled and not is_active:
            if install_ok:
                notify_desktop(
                    tr("Anomaly install finished"),
                    tr("The Anomaly installation completed successfully."),
                )
            else:
                notify_desktop(
                    tr("Anomaly install failed"),
                    tr("The Anomaly installation failed. Open COMMANDER for details."),
                )
        # Refreshes the badge and button labels through on_busy_changed().
        self.window.set_install_busy(False)
        if self._anomaly_failed:
            self._offer_anomaly_retry(chain)

    def _show_error_popup(
        self, title: str, rc: int, output: str, *, resume_hint: str | None = None
    ) -> None:
        """Show a plain-English summary of a failed CLI run, raw output collapsed."""
        summary, detail = _install_failure_message(rc, output, resume_hint)
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            title,
            summary,
            QMessageBox.StandardButton.Ok,
            self,
        )
        box.setDetailedText(detail)
        box.exec()

    def _start_verify(self):
        if VERIFY_INTEGRITY_DISABLED:
            QMessageBox.information(
                self,
                tr("Not available right now"),
                tr("Verify Integrity is being worked on and will be back in a later update."),
            )
            return
        if self._verify_runner is not None and self._verify_runner.is_running():
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
            return
        if game_running(force=True):
            # The game holds the Wine prefix; verification must not read it.
            QMessageBox.information(
                self,
                tr("Close the game first"),
                tr("Mod Organizer or the game is still open. Close it, then run Verify Integrity again."),
            )
            return
        if self.window.settings.active_profile is None:
            QMessageBox.warning(
                self,
                tr("No profile selected"),
                tr("Create or pick a profile on the Profiles page first."),
            )
            return
        self._verify_counts = {"OK": 0, "CORRUPT": 0, "NOT FOUND": 0}
        self._anomaly_problem_lines: list[str] = []
        self._verify_anomaly_ok = False
        self._anomaly_checked = False
        self._presence = None
        self._scan = None
        self._repair_plan = None
        self._repair_records = {}
        self._verify_records = {}
        self._official_names = None
        self._quarantine_records = []
        self._repair_quarantined_count = 0
        self._repaired_folders: list[str] = []
        # Cleared so a cancelled *previous* repair cannot suppress this run.
        self._repair_runner = None
        self._gamma_overlay_restore_pending = False
        self._gamma_overlay_restored = False
        self._gamma_repair_done = False
        self._gamma_skipped = False
        self._official_missing = False
        self._cache_archive_result = None
        self._repo_check = None
        self._repo_files = {}
        self._repo_repair_pending = False
        self._repo_restored = []
        self._anomaly_recheck_done = False
        self._pending_repair = None
        # Read here, on the GUI thread: the GAMMA check runs on a worker.
        self._anomaly_skipped = not self.verify_checkboxes["check_anomaly"].isChecked()
        self._cache_skipped = not self.verify_checkboxes["check_cache"].isChecked()
        self.repair_button.hide()
        clear_layout(self.verify_findings)
        self.verify_tiles["anomaly"].set_state("running", "0", tr("files checked"))
        for key in ("mods", "md5", "cache"):
            self.verify_tiles[key].set_state("idle", "-", tr("Waiting"))
        self._set_verify_pill("running", tr("Checking"))
        self.window.set_install_busy(True)
        self.verify_progress.reset()
        self.verify_button.setEnabled(False)
        self.verify_progress.on_started()
        self._set_verify_console(True)
        self.verify_progress.bar.show()
        # Verify cannot pause (no runner is bound), so hide the inert Pause button.
        self.verify_progress.pause_button.hide()
        if self._anomaly_skipped:
            self.verify_progress.log.append_line("Anomaly check skipped.")
            self._update_verify_tiles()
            self._start_gamma_verify()
            return
        self._verify_phase_busy(tr("Checking Anomaly files"))
        self.verify_progress.log.append_line("== Anomaly integrity check ==")
        runner = CommandRunner(cli_command(["anomaly", "check"]), parent=self)
        runner.line.connect(self._on_verify_line)
        runner.finished.connect(self._on_anomaly_verify_finished)
        runner.cancelled.connect(self._on_verify_cancelled)
        self._verify_runner = runner
        runner.start()

    def _on_verify_line(self, line):
        profile = self.window.settings.active_profile
        anomaly_path = profile.anomaly if profile is not None else ""
        if is_expected_gamma_overlay_corrupt(line, anomaly_path):
            # GAMMA deliberately overwrites this exact file - it will
            # always mismatch anomaly check's vanilla-only baseline, and
            # is not a real problem. Relabel rather than hide the line
            # outright, so the report stays transparent about why.
            line = re.sub(
                r"\|\s*CORRUPT\s*$", "| OK (GAMMA-modified, expected)", line
            )
            status = "OK"
        else:
            status = anomaly_status(line)
        self.verify_progress.on_line(line)
        if status is not None:
            if status in self._verify_counts:
                self._verify_counts[status] += 1
            if status in ("CORRUPT", "NOT FOUND"):
                self._anomaly_problem_lines.append(strip_ansi(line).strip())
            seen = sum(self._verify_counts.values())
            if seen and seen % 20 == 0:
                # Keep the busy bar label moving during the anomaly check.
                self.verify_progress.bar.setFormat(
                    tr("Checking Anomaly files ({count})", count=seen)
                )
                self.verify_tiles["anomaly"].set_state(
                    "running", str(seen), tr("files checked")
                )

    def _on_anomaly_verify_finished(self, rc, output):
        # 'finished' still arrives after a cancel (the CLI exits in response to
        # SIGINT), so the cancelled run must not fall through to the next stage.
        if self._verify_runner is not None and self._verify_runner.was_cancelled:
            self._verify_cancel_done()
            return
        counts = self._verify_counts
        self.verify_progress.log.append_line("")
        parsed = counts["OK"] + counts["CORRUPT"] + counts["NOT FOUND"]
        # The CLI exits 0 on some failures, so trust it only when no failure
        # markers appear and at least one status line was actually parsed.
        # Anything else is "could not be checked" - never a pass.
        self._anomaly_checked = _anomaly_check_ran(rc, output, parsed) and parsed > 0
        if not _anomaly_check_ran(rc, output, parsed):
            self.verify_progress.log.append_line("Anomaly check failed.")
            self._verify_anomaly_ok = False
        elif parsed == 0:
            self.verify_progress.log.append_line(
                "Anomaly check produced no results - treated as failed."
            )
            self._verify_anomaly_ok = False
        else:
            self._verify_anomaly_ok = counts["NOT FOUND"] == 0
            self.verify_progress.log.append_line(_anomaly_tally(counts))
        self._update_verify_tiles()
        self._start_gamma_verify()

    def _verify_phase_busy(self, label: str) -> None:
        """Animated busy bar with a phase label (no numeric source)."""
        self.verify_progress.bar.setRange(0, 0)
        self.verify_progress.bar.setFormat(label)
        self.verify_progress.status_message(label)

    def _set_verify_phase(
        self, start: int, end: int, fraction: float, label: str
    ) -> None:
        """Determinate phase progress on the overall verify bar.

        ``label`` is the short phase name (the tile title) - the counts
        and sizes already sit on the tile above, so the bar stays short."""
        value = _verify_phase_value(start, end, fraction)
        bar = self.verify_progress.bar
        bar.setRange(0, 100)
        bar.setValue(value)
        bar.setFormat(f"{label}  ·  {value}%")

    def _start_gamma_verify(self):
        self._scan_cancel = threading.Event()
        self.verify_progress.cancel_button.show()
        self.verify_progress.status_message(tr("Checking GAMMA mods"))
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== GAMMA integrity check ==")
        self.verify_tiles["mods"].set_state("running", "-", tr("Downloading GAMMA's mod list"))
        task = StreamTask(self._run_gamma_verify, parent=self)
        task.line.connect(self._on_gamma_verify_progress)
        task.result.connect(self._on_gamma_verify_done)
        task.error.connect(self._on_gamma_verify_error)
        self._verify_task = task
        task.start()

    def _run_gamma_verify(self, report):
        profile = self.window.settings.active_profile
        if profile is None:
            raise RuntimeError("No active profile")
        from .common import gamma_installed

        if _gamma_verify_gate(gamma_installed(profile.gamma, profile.mo2_profile)) is not None:
            return (_GAMMA_NOT_INSTALLED, None, None, {}, False, None, None, {})
        report("Downloading official GAMMA mod list")
        official = fetch_official_mod_names(profile.mod_list_url)
        official_missing = official is None or len(official) == 0
        # Read on the GUI thread only after this task's result arrives;
        # the post-repair re-check sorts GAMMA vs. own mods with it too.
        self._official_names = None if official_missing else official
        if official_missing:
            report(
                "Official mod list unavailable - verification will be "
                "presence-based only."
            )
        presence = verify_gamma(
            profile.gamma,
            profile.mo2_profile,
            on_progress=(
                lambda done, total, name: report(
                    f"Checking GAMMA mod {done}/{total}: {name}"
                )
            ),
            official_mods=official,
        )
        report("Reading GAMMA's own files from the download cache")
        repo_files, unreadable_repos = gamma_repo_files(profile.gamma, repo_branches(profile))
        report("Starting full MD5 scan of mod files")
        scan = scan_mods_md5(
            profile.gamma,
            on_progress=(
                lambda done, total, size: report(
                    f"MD5 hashing {done}/{total} files ({size})"
                )
            ),
            cancel=self._scan_cancel,
            git_blobs=repo_files,
        )
        repo_check = (
            None if scan.cancelled else compare_repo_files(repo_files, scan, unreadable_repos)
        )
        plan = None
        report("Checking GAMMA download cache")
        records = fetch_modpack_records(profile.mod_pack_maker_url)
        expected = expected_archive_md5s(records)
        cache_result = (
            None
            if getattr(self, "_cache_skipped", False)
            else verify_cache_archives(
                profile.cache,
                expected,
                on_progress=lambda done, total, name: report(
                    f"Checking cached archive {done}/{total}: {name}"
                ),
                cancel=self._scan_cancel,
            )
            if expected
            else None
        )
        # Presence-check misses (a mod missing/empty right now) are real,
        # classifiable problems even on the very first baseline run (no
        # content comparison has happened yet, so scan.problems is always
        # 0 then) or when the missing mod's files were simply never in
        # the baseline to begin with - without folding these in, such a
        # mod would be reported forever but never actually offered for
        # repair (see classify_problems' extra_broken_folders docstring).
        extra_broken = presence.missing + presence.empty
        if not scan.cancelled and (scan.problems or extra_broken):
            report("Looking up download sources for broken mods")
            plan = classify_problems(
                scan, records, extra_broken_folders=extra_broken, repo_owned=repo_files
            )
        return (
            presence, scan, plan, records, official_missing, cache_result, repo_check, repo_files
        )

    def _on_gamma_verify_progress(self, text):
        # The worker reports fixed English lines (matched by the patterns
        # below, and kept in the log as they are): translated only here,
        # for the status line.
        self.verify_progress.status_message(tr(text))
        for pattern, (start, end), tile, caption in (
            (_PRESENCE_RE, _VERIFY_PHASE["presence"], "mods", tr("mods checked")),
            (_MD5_RE, _VERIFY_PHASE["md5"], "md5", tr("files fingerprinted")),
            (_CACHE_RE, _VERIFY_PHASE["cache"], "cache", tr("archives checked")),
        ):
            match = pattern.search(text)
            if match:
                done, total = int(match.group(1)), int(match.group(2))
                fraction = done / total if total > 0 else 0.0
                self._set_verify_phase(start, end, fraction, self.verify_tiles[tile].title.text())
                self.verify_tiles[tile].set_state(
                    "running", f"{done:,} / {total:,}", caption
                )
                self.verify_progress.status_message(
                    f"{self.verify_tiles[tile].title.text()}: {done:,} / {total:,}"
                )
                break
        self.verify_progress.log.append_line(text)

    def _on_gamma_verify_done(self, result):
        (
            presence_or_sentinel,
            scan,
            plan,
            records,
            official_missing,
            cache_result,
            repo_check,
            repo_files,
        ) = result
        self._verify_records = records or {}
        if presence_or_sentinel == _GAMMA_NOT_INSTALLED:
            # GAMMA absent: never report its core launcher files as errors.
            # A corrupt/missing Anomaly install is still offered a repair
            # (see _deliver_verdict) - only the GAMMA checks don't apply.
            self._gamma_skipped = True
            self.verify_progress.log.append_line(
                "GAMMA is not installed - skipping GAMMA checks."
            )
            self.verify_progress.log.append_line(
                "Only Anomaly was verified in this run."
            )
            self._conclude_verify()
            return
        presence = presence_or_sentinel
        self._presence = presence
        self._scan = scan
        self._repair_plan = plan
        # matched_records (not the raw records dict) so a folder matched via
        # the counter-shift fallback still resolves to its record here.
        self._repair_records = plan.matched_records if plan is not None else {}
        self._official_missing = official_missing
        self._cache_archive_result = cache_result
        self._repo_check = repo_check
        self._repo_files = repo_files or {}
        for line in presence.lines():
            self.verify_progress.log.append_line(line)
        for line in scan.lines():
            self.verify_progress.log.append_line(line)
        if repo_check is not None:
            for line in repo_check.lines():
                self.verify_progress.log.append_line(line)
        if cache_result is not None:
            for line in cache_result.lines():
                self.verify_progress.log.append_line(line)
        if official_missing:
            self.verify_progress.log.append_line(
                "Note: official mod list unavailable - GAMMA results are "
                "presence-based only."
            )
        if scan.cancelled or (cache_result is not None and cache_result.cancelled):
            stage = tr("the mod file scan") if scan.cancelled else tr("the download cache check")
            self._finish_verify(
                ok=False,
                message=tr("Check cancelled during {stage}.", stage=stage),
                summary=tr("Verify cancelled"),
                verdict="cancelled",
            )
            return
        self._conclude_verify()

    def _offer_repair(
        self, anomaly_needs_repair: bool, gamma_repairable: bool, repo_repairable: bool = False
    ) -> None:
        """Show the Repair button for what this check found.

        Repairing is the user's call, made after reading the findings, so
        the check itself never pops a dialog. The offer is tied to the
        install it was found on; a profile switch voids it.
        """
        profile = self.window.settings.active_profile
        if profile is None:
            return
        self._pending_repair = (
            anomaly_needs_repair,
            gamma_repairable,
            repo_repairable,
            (profile.anomaly, profile.gamma, profile.cache),
        )
        self.repair_button.show()

    def _on_repair_clicked(self) -> None:
        pending = self._pending_repair
        profile = self.window.settings.active_profile
        if (
            pending is None
            or profile is None
            or pending[3] != (profile.anomaly, profile.gamma, profile.cache)
        ):
            self._pending_repair = None
            self.repair_button.hide()
            QMessageBox.information(
                self,
                tr("Check again first"),
                tr("The last check was for a different profile or folder. Run Verify Integrity again, then repair."),
            )
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
            return
        if game_running(force=True):
            QMessageBox.information(
                self,
                tr("Close the game first"),
                tr("Mod Organizer or the game is still open. Close it, then run Verify Integrity again."),
            )
            return
        if not self._prompt_repair(pending[0], pending[1], pending[2]):
            return
        self._pending_repair = None
        self.repair_button.hide()
        self._begin_repair_run()
        self._advance_repair_pipeline()

    def _begin_repair_run(self) -> None:
        self._scan_cancel = threading.Event()
        self._repair_runner = None
        self._quarantine_records = []
        self._repaired_folders = []
        self._repo_restored = []
        self._repair_reuse_keys = {}
        self._repair_reused = set()
        self._repair_resume_written = False
        self._repair_retrying = False
        self.window.set_install_busy(True)
        self.verify_button.setEnabled(False)
        self.verify_progress.on_started()
        self._set_verify_console(True)
        self.verify_progress.bar.show()
        self.verify_progress.pause_button.hide()
        self._set_verify_pill("running", tr("Repairing"))
        error = backup_settings_before(self.window.settings.active_profile, "repair")
        if error:
            self.verify_progress.log.append_line(f"Settings backup failed: {error}")

    def _prompt_repair(
        self, anomaly_needs_repair: bool, gamma_repairable: bool, repo_repairable: bool = False
    ) -> bool:
        """Confirm the repair. Returns True and schedules it on Yes."""
        sections: list[str] = []
        if anomaly_needs_repair:
            counts = self._verify_counts
            sections.append(
                tr(
                    "Anomaly: {corrupt} changed and {missing} missing file(s).\n"
                    "The Anomaly installer runs again, then GAMMA's own files go "
                    "back on top. Your saves and user.ltx aren't touched.",
                    corrupt=counts["CORRUPT"],
                    missing=counts["NOT FOUND"],
                )
            )
        if gamma_repairable:
            names = self._repair_plan.repairable
            shown = "\n".join(f"• {name}" for name in names[:8])
            if len(names) > 8:
                shown += "\n• " + tr("and {count} more", count=len(names) - 8)
            sections.append(tr("GAMMA mods to reinstall:") + "\n\n" + shown)
            sections.append(
                tr(
                    "The old copies are set aside first, not deleted. If the "
                    "repair fails or you stop it, they're put back. Your own "
                    "mods and files are never touched."
                )
                + " "
                + tr("Mods whose archive in the download cache is intact are unpacked again from it; only the others are downloaded.")
            )
        if repo_repairable:
            sections.append(
                tr(
                    "GAMMA's own files: {count} damaged or missing. The originals are copied back from GAMMA's repositories in the download cache - nothing is downloaded.",
                    count=len(self._repo_check.broken),
                )
            )
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle(tr("Repair now?"))
        dialog.setText("\n\n".join(sections))
        dialog.setInformativeText(tr("Show details lists every file and mod the repair will touch."))
        dialog.setDetailedText(
            repair_preview(
                self._repair_plan if gamma_repairable else None,
                getattr(self, "_anomaly_problem_lines", []) if anomaly_needs_repair else [],
                self._repo_check.broken if repo_repairable else (),
            )
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        dialog.set_labels({
            QMessageBox.StandardButton.Yes: tr("Repair"),
            QMessageBox.StandardButton.No: tr("Cancel"),
        })
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        if dialog.exec() != QMessageBox.StandardButton.Yes:
            return False
        self._repair_anomaly_pending = anomaly_needs_repair
        self._gamma_repair_pending = gamma_repairable
        self._repo_repair_pending = repo_repairable
        # An Anomaly repair (`anomaly install`) unconditionally re-extracts
        # vanilla Anomaly over everything, reverting GAMMA's own file
        # overlay (its replacement engine executables/DLLs, copied
        # directly into the Anomaly root - not a `gamma/mods/` entry, so
        # invisible to the MD5 scan above). The GAMMA-mods repair step
        # already restores that overlay as a side effect of its own
        # full-install call, so this is only needed when that step isn't
        # already going to run.
        self._gamma_overlay_restore_pending = anomaly_needs_repair and not gamma_repairable
        self._gamma_overlay_restored = False
        self._gamma_repair_done = False
        return True

    def _advance_repair_pipeline(self) -> None:
        """Run pending repairs in order, then deliver the final verdict."""
        if self._repair_anomaly_pending:
            self._repair_anomaly_pending = False
            self._run_anomaly_repair()
            return
        if self._gamma_repair_pending:
            self._gamma_repair_pending = False
            self._start_gamma_repair_deletion()
            return
        if self._gamma_overlay_restore_pending:
            self._gamma_overlay_restore_pending = False
            self._start_gamma_overlay_restore()
            return
        # Last: the installer runs above copy GAMMA's repositories out again,
        # which may already have fixed these.
        if self._repo_repair_pending:
            self._repo_repair_pending = False
            self._start_repo_repair()
            return
        self._conclude_verify()

    def _start_repo_repair(self) -> None:
        """Copy GAMMA's own damaged files back from its repositories."""
        profile = self.window.settings.active_profile
        broken = self._repo_check.broken if self._repo_check is not None else []
        if profile is None or not broken:
            self._advance_repair_pipeline()
            return
        self.verify_progress.cancel_button.show()
        self._verify_phase_busy(tr("Copying GAMMA's own files back"))
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Restoring GAMMA's own files ==")
        task = BackgroundTask(
            restore_repo_files, profile.gamma, self._repo_files, broken, self._scan_cancel,
            parent=self,
        )
        task.result.connect(self._on_repo_repaired)
        task.error.connect(
            lambda message: self._on_repo_repaired(
                RepoRepairResult(failed=list(broken), reason=message)
            )
        )
        self._verify_task = task
        task.start()

    def _on_repo_repaired(self, result) -> None:
        failed = set(result.failed)
        for rel in result.restored:
            self.verify_progress.log.append_line(f"Copied back: {rel}")
        for rel in result.failed:
            self.verify_progress.log.append_line(f"Could not copy back: {rel}")
        if result.reason:
            self.verify_progress.log.append_line(f"Reason: {result.reason}")
        check = self._repo_check
        check.changed = [rel for rel in check.changed if rel in failed]
        check.missing = [rel for rel in check.missing if rel in failed]
        check.checked += len(result.restored)
        self._repo_restored = list(result.restored)
        if self._scan_cancel is not None and self._scan_cancel.is_set():
            self._finish_verify(
                ok=False, message=tr("Repair cancelled."), summary=tr("Repair cancelled")
            )
            return
        self._advance_repair_pipeline()

    def _start_gamma_overlay_restore(self) -> None:
        """Re-apply GAMMA's file overlay (including its replacement engine

        executables/DLLs) on top of Anomaly after an Anomaly repair.
        `anomaly install` unconditionally re-extracts vanilla Anomaly over
        everything, silently reverting GAMMA's own root-level overlay
        files - without this, a "successful" Anomaly repair would leave
        the game unplayable (e.g. head_damage_017 crashes from a reverted
        engine). Reuses the same full-install call the GAMMA-mods repair
        step uses; nothing under gamma/mods is touched, so no quarantine
        step is needed here.
        """
        self._gamma_overlay_restored = True
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line(
            "== Restoring GAMMA's file overlay over Anomaly =="
        )
        self._start_repair_install()

    def _run_anomaly_repair(self) -> None:
        self.verify_progress.cancel_button.show()
        self.verify_progress.status_message(tr("Re-installing Anomaly"))
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Repairing Anomaly ==")
        runner = CommandRunner(cli_command(self._anomaly_install_args()), parent=self)
        runner.line.connect(self._on_verify_line)
        runner.finished.connect(self._on_anomaly_repair_finished)
        runner.cancelled.connect(
            lambda: self._on_verify_cancelled("Anomaly repair")
        )
        self._verify_runner = runner
        self.verify_progress.set_runner(runner)
        runner.start()

    def _on_anomaly_repair_finished(self, rc, output):
        if self._verify_runner is not None and self._verify_runner.was_cancelled:
            self._verify_cancel_done()
            return
        repair_ok = cli_ok(rc, output, "")
        self.verify_progress.log.append_line(
            "Anomaly repair finished successfully."
            if repair_ok
            else f"Anomaly repair failed (exit code {rc})."
        )
        # One bounded re-check so the verdict reflects reality.
        self._verify_counts = {"OK": 0, "CORRUPT": 0, "NOT FOUND": 0}
        self._anomaly_problem_lines: list[str] = []
        self._anomaly_recheck_done = True
        self._verify_phase_busy(tr("Re-checking Anomaly files"))
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Re-checking Anomaly ==")
        runner = CommandRunner(cli_command(["anomaly", "check"]), parent=self)
        runner.line.connect(self._on_verify_line)
        runner.finished.connect(self._on_anomaly_recheck_finished)
        runner.cancelled.connect(
            lambda: self._on_verify_cancelled("Anomaly re-check")
        )
        self._verify_runner = runner
        runner.start()

    def _on_anomaly_recheck_finished(self, rc, output):
        if self._verify_runner is not None and self._verify_runner.was_cancelled:
            # A cancelled re-check delivers a partial verdict; do not let it
            # conclude the repair pipeline as if the check had passed/failed.
            self._verify_cancel_done()
            return
        counts = self._verify_counts
        parsed = sum(counts.values())
        self._anomaly_checked = _anomaly_check_ran(rc, output, parsed) and parsed > 0
        ok = (
            self._anomaly_checked
            and counts["CORRUPT"] == 0
            and counts["NOT FOUND"] == 0
        )
        self._verify_anomaly_ok = ok
        self.verify_progress.log.append_line("Re-check " + _anomaly_tally(counts))
        # Route through the pipeline, not straight to the verdict: a pending
        # GAMMA repair (both Anomaly and GAMMA needed fixing) must still run
        # here, or it's silently skipped and reported as "no issues found".
        self._advance_repair_pipeline()

    def _conclude_verify(self) -> None:
        """Every finished check and repair ends here.

        After an Anomaly repair, GAMMA's own engine files must be back.
        "Anomaly: OK" can't show it - vanilla files are exactly what the
        Anomaly check expects - and the game crashes with them. That check
        (and putting GAMMA's copies back from the cached GAMMA repo) runs
        git, so it happens off the GUI thread.
        """
        profile = self.window.settings.active_profile
        if not self._anomaly_recheck_done or self._gamma_skipped or profile is None:
            self._deliver_verdict()
            return
        self.verify_progress.status_message(tr("Checking GAMMA's engine files"))
        task = BackgroundTask(
            _check_gamma_overlay, profile.anomaly, profile.cache, parent=self
        )
        task.result.connect(self._on_overlay_checked)
        task.error.connect(lambda message: self._on_overlay_checked(([], message, [])))
        self._verify_task = task
        task.start()

    def _on_overlay_checked(self, result) -> None:
        restored, reason, reverted = result
        extra: list[Finding] = []
        if reason:
            self.verify_progress.log.append_line(
                f"Could not restore GAMMA engine files: {reason}"
            )
        if restored:
            extra.append(
                Finding(
                    "ok",
                    tr("GAMMA's engine files put back"),
                    tr("The Anomaly repair had reset them to the vanilla versions."),
                    list(restored),
                    len(restored),
                )
            )
        if reverted:
            extra.append(
                Finding(
                    "problem",
                    tr("GAMMA's engine files are the vanilla Anomaly versions"),
                    tr("The game won't run correctly with them. Use Install GAMMA - it keeps your mods and settings."),
                    list(reverted),
                    len(reverted),
                )
            )
        self._deliver_verdict(extra)

    def _deliver_verdict(self, extra: list[Finding] | None = None) -> None:
        """Explain the results on the card, offer a repair, and finish."""
        extra = list(extra or [])
        if self._gamma_repair_done and self._repaired_folders:
            extra.append(
                Finding(
                    "ok",
                    tr("GAMMA mods reinstalled"),
                    tr("Your saves, user.ltx and MCM settings were kept."),
                    list(self._repaired_folders),
                    len(self._repaired_folders),
                )
            )
        if self._repo_restored:
            extra.append(
                Finding(
                    "ok",
                    tr("GAMMA's own files copied back"),
                    tr("They came from GAMMA's repositories in the download cache."),
                    [rel.removeprefix("mods/") for rel in self._repo_restored],
                    len(self._repo_restored),
                )
            )
        profile = self.window.settings.active_profile
        findings = build_findings(
            anomaly_checked=(
                self._anomaly_checked
                or self._verify_anomaly_ok
                or getattr(self, "_anomaly_skipped", False)
            ),
            extra=extra,
            anomaly_problem_lines=self._anomaly_problem_lines,
            anomaly_root=profile.anomaly if profile is not None else "",
            presence=self._presence,
            scan=self._scan,
            cache=self._cache_archive_result,
            plan=self._repair_plan,
            official_missing=self._official_missing,
            gamma_skipped=self._gamma_skipped,
            repo=self._repo_check,
        )
        verdict = overall_verdict(findings, self._scan)
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Summary ==")
        for finding in findings:
            count = f" ({finding.count})" if finding.count else ""
            self.verify_progress.log.append_line(
                f"[{finding.level}] {finding.title}{count}"
            )
        self._update_verify_tiles()
        self._render_findings(findings)
        counts = self._verify_counts
        anomaly_needs_repair = counts["CORRUPT"] > 0 or counts["NOT FOUND"] > 0
        gamma_repairable = self._repair_plan is not None and self._repair_plan.has_repairable
        repo_repairable = self._repo_check is not None and bool(self._repo_check.broken)
        if anomaly_needs_repair or gamma_repairable or repo_repairable:
            self._offer_repair(anomaly_needs_repair, gamma_repairable, repo_repairable)
        message, summary = {
            "healthy": (
                tr("No problems found"),
                tr("Verify Integrity: no problems found"),
            ),
            "snapshot": (
                tr("Snapshot saved. Mod files will be compared from the next check on."),
                tr("Verify Integrity: snapshot saved"),
            ),
            "attention": (
                tr("Problems found - see below."),
                tr("Verify Integrity: problems found"),
            ),
        }[verdict]
        self._save_verify_result(verdict, findings)
        self._finish_verify(
            ok=verdict != "attention", message=message, summary=summary, verdict=verdict
        )
        if self._pending_repair is not None and self.verify_checkboxes["repair_prompt"].isChecked():
            # After _finish_verify: the repair refuses to start while busy.
            QTimer.singleShot(0, self._on_repair_clicked)

    def _start_gamma_repair_deletion(self) -> None:
        self.verify_progress.cancel_button.show()
        self.verify_progress.status_message(tr("Setting broken mods aside"))
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Repairing GAMMA mods ==")
        self._repair_reuse_keys = {} if self._repair_retrying else self._archive_reuse_keys()
        task = StreamTask(self._run_repair_quarantine, parent=self)
        task.line.connect(self._on_gamma_verify_progress)
        task.result.connect(self._on_repair_quarantined)
        task.error.connect(self._on_gamma_verify_error)
        self._verify_task = task
        task.start()

    def _archive_reuse_keys(self) -> dict[str, str]:
        """Broken mod folder -> resume key, for each mod the installer can
        unpack again from its cached archive instead of downloading it.

        Only archives this check just MD5-verified, and never while an
        interrupted install's resume.txt is waiting (that file is the
        channel used to ask for it - see repair.write_reextract_resume).
        """
        profile = self.window.settings.active_profile
        cache = self._cache_archive_result
        if profile is None or cache is None or self._repair_plan is None:
            return {}
        if (Path(profile.gamma) / RESUME_FILENAME).exists():
            return {}
        verified = set(cache.verified)
        keys = resume_keys(self._verify_records)
        reuse: dict[str, str] = {}
        for folder in self._repair_plan.repairable:
            record = self._repair_records.get(folder)
            key = keys.get(record.folder_name) if record is not None else None
            names = record.archive_names() if record is not None else []
            if key and names and set(names) <= verified:
                reuse[folder] = key
        return reuse

    def _run_repair_quarantine(self, report):
        """Move each broken mod/archive aside instead of deleting it.

        Nothing here is permanently lost: if the reinstall below fails or
        is cancelled, everything moved aside gets moved right back (see
        _on_repair_install_finished/_on_repair_install_cancelled). Only a
        confirmed-successful repair calls purge_quarantine to actually
        free the space.
        """
        profile = self.window.settings.active_profile
        if profile is None:
            raise RuntimeError("No active profile")
        quarantined = []
        failed: list[str] = []
        # Read once: set on the GUI thread before this task started.
        reuse = dict(self._repair_reuse_keys)
        for folder in self._repair_plan.repairable:
            if self._scan_cancel is not None and self._scan_cancel.is_set():
                report("Repair cancelled - quarantine aborted.")
                break
            report(f"Setting aside {folder}")
            try:
                # A reusable archive stays where the installer looks for it.
                quarantined.append(
                    quarantine_mod_and_archive(
                        profile.gamma,
                        folder,
                        None if folder in reuse else self._repair_records.get(folder),
                    )
                )
            except (OSError, ValueError) as exc:
                # Keep going: one stubborn folder (e.g. a symlinked mods/
                # folder tripping the path-escape guard, which raises
                # ValueError rather than OSError) must not abort the whole
                # repair or lose track of what was already set aside.
                failed.append(f"{folder}: {exc}")
                report(f"Could not set aside {folder}: {exc}")
        if failed:
            report(
                "WARNING: some mods could not be set aside and were NOT "
                "reinstalled:"
            )
            for line in failed:
                report(f"  {line}")
        return quarantined

    def _on_repair_quarantined(self, quarantined):
        self._quarantine_records = quarantined
        # Recorded separately from _quarantine_records (which gets cleared
        # once the repair install finishes) so the final "repaired (N
        # mod(s))" summary reflects what was actually set aside, not the
        # full repair plan - a folder that failed to quarantine (see
        # _run_repair_quarantine's per-folder (OSError, ValueError) catch)
        # was never actually repaired.
        self._repair_quarantined_count = len(quarantined)
        if self._scan_cancel is not None and self._scan_cancel.is_set():
            self.verify_progress.log.append_line("Repair cancelled before reinstall.")
            restore_from_quarantine_failures = self._restore_all_quarantined()
            self._finish_verify(
                ok=False,
                message=tr("Repair cancelled.") + restore_from_quarantine_failures,
                summary=tr("Repair cancelled"),
            )
            return
        self.verify_progress.log.append_line(
            f"Set aside {len(quarantined)} mod(s) pending reinstall"
        )
        reused: dict[str, str] = {}
        if self._repair_reuse_keys:
            set_aside = {record.folder for record in quarantined}
            reused = {
                folder: key
                for folder, key in self._repair_reuse_keys.items()
                if folder in set_aside
            }
        profile = self.window.settings.active_profile
        self._repair_resume_written = bool(reused) and write_reextract_resume(
            profile.gamma, reused.values()
        )
        # Not written: their archives are still in place, so the installer
        # skips them and the fallback below downloads them instead.
        self._repair_reused = set(reused)
        if self._repair_resume_written:
            self.verify_progress.log.append_line(
                f"{len(reused)} mod(s) will be unpacked again from the download "
                f"cache, {len(quarantined) - len(reused)} downloaded"
            )
        self._start_repair_install()

    def _restore_all_quarantined(self) -> str:
        """Move every currently-quarantined item back. Returns a short

        note to append to the user-facing message if any restore failed
        (empty string on full success).
        """
        failures: list[str] = []
        for record in self._quarantine_records:
            failures.extend(restore_from_quarantine(record))
        self._quarantine_records = []
        if not failures:
            self.verify_progress.log.append_line(
                "Restored everything that was set aside."
            )
            return ""
        self.verify_progress.log.append_line(
            f"WARNING: {len(failures)} item(s) could not be restored:"
        )
        for line in failures:
            self.verify_progress.log.append_line(f"  {line}")
        return "\n\n" + tr(
            "WARNING: could not fully restore everything that was set aside "
            "({count} item(s)) - see the log above.",
            count=len(failures),
        )

    def _start_repair_install(self):
        # Shown unconditionally rather than relying on the preceding deletion
        # stage having left it visible - this is the pipeline's real network-
        # bound download/reinstall phase, so Cancel must not silently go
        # missing if a future reordering ever reaches this stage directly.
        self.verify_progress.cancel_button.show()
        self.verify_progress.status_message(
            tr("Re-downloading and re-installing broken mods")
        )
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Running installer (repair) ==")
        # The installer writes the official modlist.txt over the profile's;
        # keep the user's to put back afterwards.
        profile = self.window.settings.active_profile
        self._repair_modlist_path = (
            modlist_path_for(profile.gamma, profile.mo2_profile) if profile is not None else None
        )
        self._repair_modlist_snapshot = snapshot_modlist(self._repair_modlist_path)
        # The installer also resets MO2's own settings and, unless asked,
        # the engine executables - keep those the same way installs do.
        # Never the exes after an Anomaly repair: those are vanilla by now,
        # and putting them back would undo exactly what this run restores.
        self._repair_kept = None
        if profile is not None:
            self._repair_kept = capture(
                profile,
                keep_exes=self.checkboxes["keep_engine_exes"].isChecked()
                and not self._anomaly_recheck_done,
            )
            # modlist.txt is handled exactly (order included) above.
            self._repair_kept.modlist = None
        # Preservation flags are mandatory: a repair must never touch
        # user.ltx or MCM settings.
        args = _repair_install_args()
        if self._repair_resume_written:
            args.append("--resume")
        runner = CommandRunner(
            cli_command(args, progress_interval_ms=200),
            parent=self,
        )
        runner.line.connect(self._on_verify_line)
        runner.finished.connect(self._on_repair_install_finished)
        runner.cancelled.connect(self._on_repair_install_cancelled)
        self._verify_runner = runner
        self._repair_runner = runner
        runner.start()

    def _restore_user_modlist(self, *, succeeded: bool = False) -> None:
        path = getattr(self, "_repair_modlist_path", None)
        snapshot = getattr(self, "_repair_modlist_snapshot", None)
        self._repair_modlist_snapshot = None
        kept, self._repair_kept = getattr(self, "_repair_kept", None), None
        if path is not None:
            note = restore_modlist_after_repair(path, snapshot)
            if note:
                self.verify_progress.log.append_line(note)
        if kept is None:
            return
        if succeeded:
            for note in restore(kept):
                self.verify_progress.log.append_line(note)
        else:
            discard(kept)

    def _on_repair_install_finished(self, rc, output):
        # A cancelled repair must not fall through to the post-scan: that scan
        # re-baselines the MD5 manifest and would record the broken state as
        # the new reference.
        if self._repair_resume_written:
            # Done with it either way: the CLI deletes it after a success,
            # and after a failure everything set aside is put back below, so
            # a later install must not resume from it.
            self._repair_resume_written = False
            remove_resume_file(self.window.settings.active_profile.gamma)
        if self._repair_runner is not None and self._repair_runner.was_cancelled:
            # Only now has full-install actually exited: putting the
            # set-aside mods back (or unlocking the app) any earlier raced
            # an installer still extracting into the same folders.
            self._restore_after_repair_cancel()
            return
        succeeded = cli_ok(rc, output, "")
        self._restore_user_modlist(succeeded=succeeded)
        self.verify_progress.log.append_line("")
        if not succeeded:
            self.verify_progress.log.append_line("Repair install failed.")
            self.verify_progress.log.append_line(
                "Restoring mods that were set aside"
            )
            restore_note = self._restore_all_quarantined()
            if _looks_like_cloudflare_challenge(output):
                reason = tr("moddb blocked the download with a Cloudflare challenge. Retrying from the same connection usually fails again - switch to a VPN in another country, or wait a while, then run the install again.")
            elif _looks_like_network_failure(output):
                reason = tr("This looks like a temporary GitHub or network problem, not an issue with your setup - the download or connection was interrupted partway through.")
            else:
                reason = tr("The technical log has the installer's error.")
            self._finish_verify(
                ok=False,
                message=tr(
                    "The repair's download or install failed, so nothing was changed: mods set aside for repair were put back.",
                )
                + " "
                + reason
                + restore_note,
                summary=tr("Repair failed"),
            )
            return
        # The installer succeeded, but that alone doesn't prove each mod
        # was reinstalled: keep the new copy where one exists and put the
        # old one back where it doesn't (see settle_quarantine).
        settled = settle_quarantine(self._quarantine_records)
        self._quarantine_records = []
        self._repaired_folders = [*self._repaired_folders, *settled.reinstalled]
        self._repair_quarantined_count = len(self._repaired_folders)
        for line in settled.failures:
            self.verify_progress.log.append_line(f"WARNING: {line}")
        # Mods meant to be unpacked from the cache that the installer skipped
        # anyway (a newer CLI keying resume.txt differently): one more round,
        # with their archives set aside so they're downloaded instead.
        retry = [folder for folder in settled.restored if folder in self._repair_reused]
        self._repair_reused = set()
        if retry and not self._repair_retrying:
            self.verify_progress.log.append_line(
                f"The installer did not unpack {len(retry)} mod(s) from the "
                "download cache; downloading them instead"
            )
            self._repair_retrying = True
            self._repair_plan.repairable = retry
            self._start_gamma_repair_deletion()
            return
        if settled.restored:
            self.verify_progress.log.append_line(
                "WARNING: the installer did not reinstall "
                f"{len(settled.restored)} mod(s); their previous copies were "
                "put back instead:"
            )
            for folder in settled.restored:
                self.verify_progress.log.append_line(f"  {folder}")
        # Leftovers from an older, interrupted run can go now - but never
        # while something just failed to move back: that copy may be the
        # only one left.
        if not any("->" in line for line in settled.failures):
            purge_quarantine(self.window.settings.active_profile.gamma)
        self._start_post_scan()

    def _on_repair_install_cancelled(self):
        # The process is still exiting; _on_repair_install_finished restores.
        self.verify_progress.log.append_line("Repair install cancelled")
        self.verify_progress.status_message(tr("Cancelling"))

    def _restore_after_repair_cancel(self):
        self._restore_user_modlist()
        self.verify_progress.log.append_line("Restoring mods that were set aside")
        restore_note = self._restore_all_quarantined()
        self._finish_verify(
            ok=False,
            message=tr("Repair cancelled. Mods that were set aside for repair have been restored.")
            + restore_note,
            summary=tr("Repair cancelled"),
        )

    def _start_post_scan(self):
        self.verify_progress.status_message(tr("Re-checking GAMMA mods"))
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Re-checking after repair ==")
        task = StreamTask(self._run_post_scan, parent=self)
        task.line.connect(self._on_gamma_verify_progress)
        task.result.connect(self._on_post_scan_done)
        task.error.connect(self._on_gamma_verify_error)
        self._verify_task = task
        task.start()

    def _run_post_scan(self, report):
        profile = self.window.settings.active_profile
        if profile is None:
            raise RuntimeError("No active profile")
        post = scan_mods_md5(
            profile.gamma,
            on_progress=(
                lambda done, total, size: report(
                    f"MD5 hashing {done}/{total} files ({size})"
                )
            ),
            cancel=self._scan_cancel,
            rebaseline=False,
            # The repair's own full-install just rewrote the modpack list;
            # that must not make this re-check discard the snapshot.
            trust_baseline=True,
        )
        if post.cancelled:
            return (post, None, None)
        report("Re-checking GAMMA mods are present")
        presence = verify_gamma(
            profile.gamma,
            profile.mo2_profile,
            on_progress=(
                lambda done, total, name: report(
                    f"Checking GAMMA mod {done}/{total}: {name}"
                )
            ),
            official_mods=self._official_names,
        )
        if _remaining_problems(post, presence, self._repo_files) == 0:
            # Only establish a new baseline after both the content and presence
            # checks have passed. A failed repair must never bless corruption.
            # New files (the reinstalled mods' own) are expected here.
            # From the hashes just taken: nothing changed since.
            try:
                write_baseline(profile.gamma, post)
            except OSError:
                # As before: the repair itself succeeded; the next check
                # just compares against the older snapshot.
                pass
            return (post, presence, None)
        # Problems remain: keep the old snapshot the reference. The repair's
        # install rewrote the modpack list after it, and the next check
        # would otherwise take that for a GAMMA update and start over,
        # silently dropping these problems.
        manifest = Path(profile.gamma) / MANIFEST_FILENAME
        try:
            if manifest.is_file():
                os.utime(manifest)
        except OSError:
            pass
        plan = classify_problems(
            post,
            self._verify_records,
            extra_broken_folders=presence.missing + presence.empty,
            repo_owned=self._repo_files,
        )
        return (post, presence, plan)

    def _on_post_scan_done(self, result):
        post, presence, plan = result
        for line in post.lines():
            self.verify_progress.log.append_line(line)
        if presence is not None:
            for line in presence.lines():
                self.verify_progress.log.append_line(line)
        if post.cancelled:
            self._finish_verify(
                ok=False,
                message=tr("Check cancelled during {stage}.", stage=tr("the mod file scan")),
                summary=tr("Verify cancelled"),
                verdict="cancelled",
            )
            return
        repaired = self._repair_quarantined_count
        remaining = _remaining_problems(post, presence, self._repo_files)
        self._gamma_repair_done = True
        self._scan = post
        if presence is not None:
            self._presence = presence
        self._repair_plan = plan
        self._repair_records = plan.matched_records if plan is not None else {}
        if remaining == 0:
            self.verify_progress.log.append_line(
                f"GAMMA repair verified clean ({repaired} mod(s) reinstalled)."
            )
        else:
            self.verify_progress.log.append_line(
                f"{remaining} problem(s) remain after repair."
            )
        # Route through the pipeline so a pending Anomaly repair can still
        # run before the final verdict is delivered.
        self._advance_repair_pipeline()

    def _on_gamma_verify_error(self, message):
        self.verify_progress.log.append_line(f"GAMMA check failed: {message}")
        self._finish_verify(
            ok=False,
            message=tr("The GAMMA check stopped with an error: {message}", message=message),
            summary=tr("GAMMA check failed"),
        )

    def _set_verify_console(self, running: bool) -> None:
        """Show Console is only there while a check or repair runs."""
        progress = self.verify_progress
        if not running and progress.log.isVisible():
            progress._toggle_log()
        progress.log_toggle.setVisible(running)

    def _finish_verify(self, ok, message, summary, baseline_created=False, verdict=None):
        """End a check or repair run and release the install lock.

        ``verdict`` comes from _deliver_verdict ("healthy", "snapshot",
        "attention"); "cancelled" or None (a failure) shows ``message`` as
        the card's only finding instead.
        """
        if verdict is None:
            verdict = "snapshot" if baseline_created else ("healthy" if ok else "failed")
        self.verify_button.setEnabled(True)
        self.verify_progress.cancel_button.hide()
        self._set_verify_console(False)
        # Drop references to finished runners/tasks so a later cancel cannot
        # target an already-dead process or thread.
        self._verify_runner = None
        self._repair_runner = None
        self._verify_task = None
        self._scan_cancel = None
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line(message)
        bar_text = {
            "healthy": tr("No problems found"),
            "snapshot": tr("Snapshot saved"),
            "attention": tr("Needs attention"),
            "cancelled": tr("Cancelled"),
        }.get(verdict)
        if verdict in ("healthy", "snapshot", "attention"):
            # A snapshot run is a pass (only mod file edits can't be judged
            # yet); "attention" is not (QProgressBar[level=...] in the theme).
            self.verify_progress.set_success_state(
                bar_text, "attention" if verdict == "attention" else ""
            )
        elif verdict == "cancelled":
            self.verify_progress.on_cancelled()
        elif verdict == "failed":
            self.verify_progress.on_finished(1, "")
        if verdict in ("failed", "cancelled"):
            self._render_findings(
                [
                    Finding(
                        "info" if verdict == "cancelled" else "problem",
                        tr("The check was cancelled") if verdict == "cancelled"
                        else tr("The check didn't finish"),
                        message,
                    )
                ]
            )
            self._settle_running_tiles()
        self._set_verify_pill(
            verdict,
            {
                "healthy": tr("Healthy"),
                "snapshot": tr("Snapshot saved"),
                "attention": tr("Needs attention"),
                "cancelled": tr("Cancelled"),
            }.get(verdict, tr("Didn't finish")),
        )
        self.verify_progress.status_message(message)
        self.window.statusBar().showMessage(summary, 8000)
        self.window.set_install_busy(False)

    def _set_verify_pill(self, level: str, text: str) -> None:
        self.verify_pill.setText(text)
        for widget in (self.verify_pill, self.verify_dot):
            widget.setProperty("level", level)
        _repolish(self.verify_pill, self.verify_dot)
        self._steps[4].set_status(_VERIFY_STEP_STATES.get(level, "pending"), text)

    def _settle_running_tiles(self) -> None:
        """A run that stopped early leaves no tile spinning."""
        for tile in self.verify_tiles.values():
            if tile.state == "running":
                tile.set_state("idle", "-", tr("Not finished"))

    def _render_findings(self, findings) -> None:
        clear_layout(self.verify_findings)
        for finding in findings:
            self.verify_findings.addWidget(_FindingRow(finding))

    def _update_verify_tiles(self) -> None:
        """Fill the four tiles from the results gathered so far."""
        tiles = self.verify_tiles
        counts = self._verify_counts
        checked = self._anomaly_checked or self._verify_anomaly_ok
        bad = counts["CORRUPT"] + counts["NOT FOUND"]
        if getattr(self, "_anomaly_skipped", False):
            tiles["anomaly"].set_state("idle", "-", tr("Skipped"))
        elif not checked:
            tiles["anomaly"].set_state("problem", "-", tr("Couldn't be checked"))
        elif bad:
            tiles["anomaly"].set_state("problem", f"{bad:,}", tr("files need repair"))
        else:
            tiles["anomaly"].set_state(
                "ok", f"{counts['OK']:,}", tr("files match the originals")
            )
        if self._gamma_skipped:
            for key in ("mods", "md5", "cache"):
                tiles[key].set_state("idle", "-", tr("GAMMA isn't installed"))
            return
        presence = self._presence
        if presence is not None:
            broken = len(presence.missing) + len(presence.empty)
            if presence.marker_missing:
                tiles["mods"].set_state("problem", "-", tr("Core files not found"))
            elif broken:
                tiles["mods"].set_state(
                    "problem",
                    f"{broken:,}",
                    tr("of {total} mods missing or empty", total=f"{presence.ok_mods + broken:,}"),
                )
            else:
                tiles["mods"].set_state(
                    "ok",
                    f"{presence.ok_mods:,}",
                    tr("mods in place, {count} optional off", count=presence.disabled_mods),
                )
        scan = self._scan
        if scan is not None and not scan.cancelled:
            changed = len(scan.changed) + len(scan.removed)
            files = f"{scan.files_scanned:,}"
            if scan.created:
                tiles["md5"].set_state(
                    "info", files, tr("files recorded - changes show from the next check")
                )
            elif changed:
                # Amber, not red: changed files are often deliberate edits
                # (MCM values, tweaks), and the game still runs.
                tiles["md5"].set_state(
                    "warn", f"{changed:,}", tr("files changed since the last check")
                )
            else:
                tiles["md5"].set_state(
                    "ok",
                    files,
                    tr("files unchanged since the last check ({size})", size=format_size(scan.bytes_scanned)),
                )
        cache = self._cache_archive_result
        if getattr(self, "_cache_skipped", False):
            tiles["cache"].set_state("idle", "-", tr("Skipped"))
        elif cache is None:
            if presence is not None:
                tiles["cache"].set_state("info", "-", tr("GAMMA's list unavailable"))
        elif not cache.cancelled:
            later = len(cache.missing) + len(cache.mismatched)
            ready = f"{len(cache.verified):,}"
            if cache.unreadable:
                tiles["cache"].set_state(
                    "warn", ready, tr("ready, {count} unreadable", count=len(cache.unreadable))
                )
            elif later:
                tiles["cache"].set_state(
                    "info", ready, tr("ready, {count} download when needed", count=later)
                )
            else:
                tiles["cache"].set_state("ok", ready, tr("archives ready to reuse"))

    def _save_verify_result(self, verdict: str, findings) -> None:
        profile = self.window.settings.active_profile
        if profile is None or not profile.gamma:
            return
        saved = gui_settings.load_gui_settings().get(_VERIFY_LAST_KEY)
        saved = saved if isinstance(saved, dict) else {}
        saved.pop(profile.gamma, None)
        saved[profile.gamma] = {
            "time": time.time(),
            "verdict": verdict,
            "tiles": {key: tile.snapshot() for key, tile in self.verify_tiles.items()},
            "findings": [asdict(finding) for finding in findings],
        }
        # Most recent last; a handful of installs is plenty.
        for key in list(saved)[:-8]:
            del saved[key]
        gui_settings.save_gui_settings(**{_VERIFY_LAST_KEY: saved})

    def _show_saved_verify_result(self) -> None:
        """Show the last finished check for the active install, if any."""
        profile = self.window.settings.active_profile
        pending = self._pending_repair
        if pending is not None and (
            profile is None or pending[2] != (profile.anomaly, profile.gamma, profile.cache)
        ):
            self._pending_repair = None
        self.repair_button.setVisible(self._pending_repair is not None)
        saved = gui_settings.load_gui_settings().get(_VERIFY_LAST_KEY)
        entry = saved.get(profile.gamma) if isinstance(saved, dict) and profile else None
        if not isinstance(entry, dict):
            for tile in self.verify_tiles.values():
                tile.set_state("idle", "-", tr("Not checked yet"))
            clear_layout(self.verify_findings)
            self._set_verify_pill("", tr("Never checked"))
            return
        tiles = entry.get("tiles")
        for key, tile in self.verify_tiles.items():
            value = tiles.get(key) if isinstance(tiles, dict) else None
            if isinstance(value, list) and len(value) == 3 and all(
                isinstance(part, str) for part in value
            ):
                if key == "md5" and value[0] == "problem":
                    # Saved before changed files became amber.
                    value = ["warn", *value[1:]]
                tile.set_state(*value)
        findings = []
        for raw in entry.get("findings") or []:
            try:
                findings.append(Finding(**raw))
            except TypeError:
                continue
        self._render_findings(findings)
        self._set_saved_verify_pill(entry)

    def _set_saved_verify_pill(self, entry: dict) -> None:
        """The pill for a saved check: its verdict and how long ago it ran."""
        verdict = entry.get("verdict")
        label = {
            "healthy": tr("Healthy"),
            "snapshot": tr("Snapshot saved"),
            "attention": tr("Needs attention"),
        }.get(verdict)
        if label is None:
            self._set_verify_pill("", tr("Never checked"))
            return
        try:
            age = _age_text(time.time() - float(entry.get("time", 0)))
        except (TypeError, ValueError):
            age = ""
        self._set_verify_pill(verdict, f"{label} · {age}" if age else label)

    def _refresh_verify_age(self) -> None:
        """Re-count the pill's "3 h ago" while it shows a finished check,
        without re-drawing the tiles and findings under it."""
        level = self.verify_pill.property("level")
        profile = self.window.settings.active_profile
        if level not in ("healthy", "snapshot", "attention") or profile is None:
            return
        saved = gui_settings.load_gui_settings().get(_VERIFY_LAST_KEY)
        entry = saved.get(profile.gamma) if isinstance(saved, dict) else None
        if isinstance(entry, dict) and entry.get("verdict") == level:
            self._set_saved_verify_pill(entry)

    def _cancel_verify(self):
        running = self._verify_runner is not None and self._verify_runner.is_running()
        if not running and self._scan_cancel is None:
            return
        answer = QMessageBox.question(
            self,
            tr("Stop Verify Integrity?"),
            tr("You can run Verify Integrity again later."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            labels={QMessageBox.StandardButton.Yes: tr("Stop"), QMessageBox.StandardButton.No: tr("Keep going")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._verify_runner is not None:
            self._verify_runner.cancel()
        if self._scan_cancel is not None:
            self._scan_cancel.set()
            return

    def _on_verify_cancelled(self, stage: str = "Anomaly check"):
        # The CLI is still exiting (an Anomaly repair may still be writing
        # files); the runner's finished handler unlocks once it has.
        self.verify_progress.log.append_line(f"{stage} cancelled")
        self.verify_progress.status_message(tr("Cancelling"))

    def _verify_cancel_done(self):
        """The cancelled Anomaly check/repair/re-check has exited."""
        self.verify_button.setEnabled(True)
        self._set_verify_console(False)
        self.verify_progress.on_cancelled()
        self.verify_progress.status_message(tr("Cancelled"))
        self._settle_running_tiles()
        self._set_verify_pill("cancelled", tr("Cancelled"))
        self.window.set_install_busy(False)

    def _wt_prefix(self):
        # Resolves STEAM_COMPAT_DATA_PATH -> <path>/pfx for Proton runners, so
        # winetricks acts on the prefix the game actually uses.
        return configured_wine_prefix()

    def _paused_status(self):
        """Status shown while the game is running.

        The game cannot start without the runtimes, so it stays "Installed";
        the live winetricks query is unreliable against a running prefix (it
        can even report everything missing), so it is paused until the game
        closes and this page next refreshes.
        """
        paused, text = paused_dependency_status()
        self._wt_installed = True
        self.wt_status.set_state(True)
        self.wt_progress.status_label.setText(text)
        self._refresh_wt_extra()
        self.wt_status.set_status_tooltip(winetricks_tooltip(paused))
        self._update_button_states()

    def _refresh_winetricks_status(self):
        if not self._winetricks_status_enabled:
            return
        if self._wt_checking:
            return
        if getattr(self.window, "install_operation", None) == "dependencies":
            # Live "Installing..." status must survive refreshes.
            return
        if game_running():
            self._paused_status()
            return
        # A re-check keeps the last result on screen until the new one is
        # in; blanking it greyed the button out for a moment on every visit.
        self._wt_checking = True
        task = BackgroundTask(
            check_winetricks_full_status, self._wt_prefix(), parent=self
        )
        task.result.connect(self._on_winetricks_status)
        task.error.connect(self._on_winetricks_status_error)
        self._wt_task = task
        task.start()

    def _on_winetricks_status(self, status):
        self._wt_checking = False
        if getattr(self.window, "install_operation", None) == "dependencies":
            # Live "Installing..." status must survive refreshes.
            return
        if game_running():
            # The game started while the check was in flight; the result is stale.
            self._paused_status()
            return
        self._wt_verbs = dict(status)
        installed = sum(1 for ok in status.values() if ok)
        total = len(status)
        # total == 0 means the check found no verbs at all (lookup failure),
        # not "everything installed".
        all_done = total > 0 and installed == total
        self._wt_installed = all_done
        self._wt_partial = 0 < installed < total
        self.wt_status.set_state(all_done)
        # Nothing to show once everything is in place.
        self.wt_progress.status_label.setText(
            "" if all_done else tr("{done}/{total} dependencies installed", done=installed, total=total)
        )
        self._set_wt_console_visible(not all_done)
        self.wt_status.set_status_tooltip(winetricks_tooltip(status))
        self._update_button_states()

    def _on_winetricks_status_error(self, message):
        # Must clear the in-flight flag, or the status never refreshes again.
        self._wt_checking = False
        if getattr(self.window, "install_operation", None) == "dependencies":
            return
        if game_running():
            self._paused_status()
            return
        self._wt_installed = None
        self.wt_status.set_state(None, tr("status unavailable"), pending_text=tr("Unknown"))
        self.wt_status.set_status_tooltip(tr("Could not query dependencies: {error}", error=message))
        self._update_button_states()

    def _start_winetricks(self, then_install: bool = False):
        """``then_install``: Automatic - no confirm pop-up, and Anomaly and
        GAMMA start once the dependencies are in."""
        self._winetricks_status_enabled = True
        self._wt_then_install = then_install
        if self._wt_runner is not None and self._wt_runner.is_running():
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
            return
        if game_running(force=True):
            # The game holds the Wine prefix; winetricks must not touch it.
            self._warn_game_open()
            return
        if self._deps_checking:
            return
        self._deps_checking = True
        # check_all_dependencies() chains several subprocess lookups (umu,
        # winetricks) with their own timeouts - worst case a several
        # -second stall, so it runs off the GUI thread like every other
        # dependency probe on this page.
        task = BackgroundTask(check_all_dependencies, parent=self)
        task.result.connect(self._on_dependencies_checked)
        task.error.connect(self._on_dependencies_check_error)
        self._deps_check_task = task
        task.start()

    def _warn_game_open(self) -> None:
        QMessageBox.warning(
            self,
            tr("Close the game first"),
            tr("Mod Organizer 2 or the game is still open. Close both, then try again."),
        )

    def _on_dependencies_check_error(self, message: str) -> None:
        self._deps_checking = False
        QMessageBox.warning(
            self, tr("Couldn't check the dependencies"), message
        )

    def _on_dependencies_checked(self, errors) -> None:
        self._deps_checking = False
        # install_busy/mo2 state may have changed while the check ran in
        # the background.
        if self.window.install_busy:
            QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
            return
        if game_running(force=True):
            self._warn_game_open()
            return
        if errors:
            QMessageBox.warning(
                self,
                tr("Some tools are missing"),
                tr("COMMANDER needs these, but couldn't find them:"),
                points=tuple(errors),
                note=tr("Install them, then try again."),
            )
            return
        prefix = self._wt_prefix()
        need_winetricks = not winetricks_binary()
        need_umu = not umu_binary()
        message = tr(
            "This installs the Windows libraries Mod Organizer and the game need, into:\n{prefix}\n\n"
            "About 150 MB to download the first time.",
            prefix=prefix,
        )
        missing = [
            tr("{tool} isn't installed yet, so it's installed first.", tool=tool)
            for tool, needed in (
                ("winetricks", need_winetricks),
                (tr("umu-run (the Proton launcher)"), need_umu),
            )
            if needed
        ]
        details = tr("Libraries: {verbs}", verbs=", ".join(WINETRICKS_VERBS))
        self._wt_force = self._wt_installed is True or self._wt_partial
        if not self._wt_then_install:
            answer = QMessageBox.question(
                self,
                tr("Reinstall the dependencies?") if self._wt_force else tr("Install the dependencies?"),
                message,
                (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
                points=tuple(missing),
                info=details,
                labels={QMessageBox.StandardButton.Yes: tr("Reinstall now") if self._wt_force else tr("Install now"), QMessageBox.StandardButton.No: tr("Cancel")},
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        # install_busy/mo2 state may have changed while the dialog was open;
        # winetricks must never touch a prefix the game is holding.
        if self.window.install_busy:
            QMessageBox.information(self, tr("Please wait"), tr("Another install is still running. Wait for it to finish, then try again."))
            return
        if game_running(force=True):
            QMessageBox.information(
                self,
                tr("Close the game first"),
                tr("Mod Organizer or the game was opened while this window was open. Close it, then install the dependencies again."),
            )
            return
        if need_winetricks:
            self._wt_stage = "winetricks"
        elif need_umu:
            self._wt_stage = "umu"
        else:
            self._wt_stage = "verbs"
        self._wt_completed_verbs.clear()
        self._wt_last_pct = -1
        self.window.set_install_busy(True, "dependencies")
        self.wt_status.set_installing()
        self._set_wt_console_visible(True)
        self.wt_progress.reset()
        self._wt_dialog = ProgressDialog(
            tr("Reinstalling dependencies") if self._wt_force else tr("Installing dependencies"),
            tr("Starting"),
            self,
            log=True,
        )
        self._wt_dialog.canceled.connect(self._cancel_winetricks)
        self._wt_dialog.show()
        self._start_winetricks_stage()

    def _set_wt_console_visible(self, visible: bool) -> None:
        self.wt_progress.log_toggle.setVisible(visible)
        if not visible and self.wt_progress.log.isVisible():
            self.wt_progress._toggle_log()
        self._refresh_wt_extra()

    def _refresh_wt_extra(self) -> None:
        """Hide the Dependencies console row while it has nothing to show,
        so Back/Next sit where they do on the other steps."""
        self.wt_progress.setVisible(
            getattr(self, "_step", None) == 1
            and (
                bool(self.wt_progress.status_label.text())
                or not self.wt_progress.log_toggle.isHidden()
            )
        )

    def _wt_status_failed(self, detail: str) -> None:
        """Reset the dependency status row after a stage aborted mid-install."""
        self._wt_installed = False
        first_line = detail.splitlines()[0] if detail else tr("Install failed")
        self.wt_status.set_state(False, first_line)
        self.wt_status.set_status_tooltip(detail)

    def _start_winetricks_stage(self):
        start, _end = _WT_STAGE_RANGES.get(self._wt_stage, (0, 100))
        self.wt_progress.on_started()
        self.wt_progress.bar.setRange(0, 100)
        self.wt_progress.bar.setValue(start)
        self._wt_last_pct = start
        self.wt_progress.bar.setFormat("%p%")
        if self._wt_stage == "winetricks":
            command = winetricks_tool_install_command()
            if not command:
                msg = "winetricks could not be installed (curl is not available)."
                self.wt_progress.on_finished(1, msg)
                self._wt_runner = None
                self._wt_status_failed(msg)
                self._wt_dialog_done(1, msg)
                self.window.set_install_busy(False)
                self._refresh_winetricks_status()
                return
            self.wt_progress.status_message(tr("Installing winetricks"))
            self._wt_dialog_text(tr("Installing winetricks"))
            self.wt_progress.log.append_line("== Installing winetricks ==")
        elif self._wt_stage == "umu":
            command = umu_install_command()
            if not command:
                msg = "umu-run could not be installed (curl is not available)."
                self.wt_progress.on_finished(1, msg)
                self._wt_runner = None
                self._wt_status_failed(msg)
                self._wt_dialog_done(1, msg)
                self.window.set_install_busy(False)
                self._refresh_winetricks_status()
                return
            self.wt_progress.status_message(tr("Installing umu-run"))
            self._wt_dialog_text(tr("Installing umu-run"))
            self.wt_progress.log.append_line(
                "== Installing umu-run (Proton launcher) =="
            )
        else:
            # Through the game's own runner, never bare winetricks: bare
            # winetricks runs whatever wine is on PATH inside the Proton
            # prefix and overwrites Proton's DLLs with that wine's, after
            # which nothing launches (see winetricks.py's module docstring).
            try:
                runner = configured_runner()
                command, env = winetricks_install_command(runner)
            except LaunchError as exc:
                command, env = [], {}
                failure = str(exc)
            else:
                failure = (
                    "Winetricks could not be run with the selected runner - "
                    "install umu-run (or select a runner that provides Wine) "
                    "and try again."
                )
            if not command:
                self.wt_progress.on_finished(1, failure)
                self._wt_runner = None
                self._wt_status_failed(failure)
                self._wt_dialog_done(1, failure)
                self.window.set_install_busy(False)
                self._refresh_winetricks_status()
                return
            if self._wt_force and runner.kind == "umu":
                # umu-run rejects --force as "not a verb"; it only looks at
                # winetricks.log, so clear our verbs from it instead.
                forget_verbs(runner.env.get("WINEPREFIX") or self._wt_prefix())
            elif self._wt_force:
                # winetricks skips verbs already in winetricks.log without this.
                command.insert(len(command) - len(WINETRICKS_VERBS), "--force")
            self.wt_progress.status_message(tr("Installing runtimes"))
            self._wt_dialog_text(tr("Installing runtimes"))
            self.wt_progress.log.append_line(
                "== Winetricks via " + runner.label + ": " + " ".join(WINETRICKS_VERBS) + " =="
            )
        if self._wt_stage != "verbs":
            # curl / pipx: nothing here touches Wine, so it gets no Wine env.
            env = None
        self._wt_runner = CommandRunner(command, env=env, parent=self)
        self._wt_runner.line.connect(self._on_winetricks_line)
        self._wt_runner.finished.connect(self._on_winetricks_finished)
        self._wt_runner.cancelled.connect(self._on_winetricks_cancelled)
        self._wt_runner.start()

    def _on_winetricks_line(self, line):
        clean = line.strip()
        if not clean:
            return
        # Skip noisy winetricks/gamemode lines: on_line() must not run for
        # these either, or they'd still show up in the console despite the
        # comment's intent - only the percentage-parsing below was ever
        # actually skipped previously, not the line itself.
        if clean.startswith(("Using winetricks", "gamemodeauto")):
            return
        self.wt_progress.on_line(line)
        if self._wt_dialog is not None:
            self._wt_dialog.appendLine(clean)
        pct = _winetricks_progress(clean, self._wt_stage, self._wt_completed_verbs)
        overall = _dependencies_progress(self._wt_stage, pct)
        if overall is not None and overall >= self._wt_last_pct:
            self.wt_progress.bar.setRange(0, 100)
            self.wt_progress.bar.setValue(overall)
            self.wt_progress.bar.setFormat("%p%")
            self._wt_last_pct = overall
            if self._wt_dialog is not None:
                self._wt_dialog.setValue(overall)

    def _on_winetricks_finished(self, rc, output):
        # 'finished' still arrives after a cancel; don't overwrite the
        # 'Cancelled' status with a spurious failure.
        if self._wt_runner is not None and self._wt_runner.was_cancelled:
            self._wt_runner = None
            self._wt_dialog_done(None)
            self.window.set_install_busy(False)
            self._refresh_winetricks_status()
            return
        if rc == 0 and self._wt_stage == "winetricks":
            # winetricks tool installed — chain to umu or verbs.
            self._wt_stage = "umu" if not umu_binary() else "verbs"
            self._wt_runner = None
            self._start_winetricks_stage()
            return
        if rc == 0 and self._wt_stage == "umu":
            # UMU installed — chain to the verbs.
            self._wt_stage = "verbs"
            self._wt_runner = None
            self._start_winetricks_stage()
            return
        self.wt_progress.on_finished(rc, output)
        if rc == 0:
            self.wt_progress.status_message(tr("Complete - runtimes installed"))
        else:
            self.wt_progress.status_message(tr("Failed (exit code {rc})", rc=rc))
        self._wt_runner = None
        self.window.set_install_busy(False)
        self._refresh_winetricks_status()
        self._wt_dialog_done(rc, output)

    def _wt_dialog_text(self, text: str) -> None:
        if self._wt_dialog is not None:
            self._wt_dialog.setLabelText(text)

    def _wt_dialog_done(self, rc, detail: str = "") -> None:
        """Close the progress pop-up; report the outcome (rc None = cancelled)."""
        dialog, self._wt_dialog = self._wt_dialog, None
        then_install, self._wt_then_install = self._wt_then_install, False
        if dialog is not None:
            dialog.hide()
            dialog.deleteLater()
        if rc != 0:
            self._auto_active = False
        if rc is None:
            return
        if rc == 0 and then_install:
            self._start_auto_gamma()
        elif rc == 0:
            QMessageBox.information(self, tr("Dependencies installed"), tr("The Windows libraries are ready."))
        else:
            lines = [ln for ln in (detail or "").strip().splitlines() if ln.strip()]
            QMessageBox.warning(
                self,
                tr("Dependencies failed"),
                tr("The install stopped with exit code {rc}.", rc=rc),
                note=lines[-1] if lines else None,
            )

    def _on_winetricks_cancelled(self):
        self.wt_progress.log.append_line("Winetricks cancelled")
        self.wt_progress.on_cancelled()
        self.wt_progress.status_message(tr("Cancelled"))
        self._wt_dialog_done(None)

    def _cancel_winetricks(self):
        if self._wt_runner is None or not self._wt_runner.is_running():
            return
        answer = QMessageBox.question(
            self,
            tr("Stop installing the dependencies?"),
            tr("They're still downloading or installing. Stopping now can leave the game's Wine prefix half set up. Running the install again fixes it."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            labels={QMessageBox.StandardButton.Yes: tr("Stop"), QMessageBox.StandardButton.No: tr("Keep installing")},
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # The install may have finished while the dialog was open, clearing
        # self._wt_runner - re-check before touching it, or this crashes.
        if self._wt_runner is None or not self._wt_runner.is_running():
            return
        self.wt_progress.cancel_button.setEnabled(False)
        self.wt_progress.cancel_button.setText(tr("Cancelling"))
        self.wt_progress.status_message(tr("Cancelling Winetricks"))
        self._wt_runner.cancel()
