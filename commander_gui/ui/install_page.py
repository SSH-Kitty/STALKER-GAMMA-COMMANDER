"""
Install page: Anomaly + GAMMA install (top), cache folder (middle),
winetricks and verify (bottom).
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

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..cli_runner import cli_command, cli_resume_args
from ..config import logs_dir
from ..dependencies import check_all_dependencies
from ..game_backup import apply_pending_settings_restore, backup_settings_before
from ..gui_settings import configured_runner, configured_wine_prefix
from ..integrity import (
    MANIFEST_FILENAME,
    CacheArchiveVerifyResult,
    Finding,
    anomaly_status,
    build_findings,
    fetch_official_mod_names,
    format_size,
    invalidate_baseline,
    is_expected_gamma_overlay_corrupt,
    overall_verdict,
    restore_gamma_overlay,
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
    classify_problems,
    expected_archive_md5s,
    fetch_modpack_records,
    purge_quarantine,
    quarantine_mod_and_archive,
    repair_preview,
    restore_from_quarantine,
    restore_modlist_after_repair,
    settle_quarantine,
    snapshot_modlist,
)
from ..settings import cli_ok
from ..themes import active_theme_tokens
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
from .common import (
    ACCENT,
    STATUS_RED,
    BackgroundTask,
    CommandRunner,
    InstallStatusRow,
    ProgressArea,
    StatusDot,
    StreamTask,
    anomaly_installed,
    clear_layout,
    count_cached_archives,
    display_state,
    free_space_bytes,
    gamma_installed,
    human_size,
    info_label,
    make_card,
    make_header_row,
    game_running,
    normalize_path,
    notify_desktop,
    paused_dependency_status,
    save_profile_dirs,
    tr,
    use_stock_mo2_profile_if_missing,
    winetricks_tooltip,
)
from .dialogs import MessageBox as QMessageBox
from .dialogs import ProgressDialog
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
            "Fetch the newest code for every GitHub addon. Unchecked reuses cached copies (faster).",
        ),
        (
            "offline",
            "Offline install (cache only)",
            "Install from a cache you already extracted into the cache folder. Nothing is downloaded.",
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
_DEFAULT_CHECKED_OPTIONS = {"auto_retry_large_files", "auto_continue_gamma", "anomaly_mirror"}

_ANOMALY_CHECKBOXES = [
    (
        "verify_after_install",
        "Verify files after install",
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
        "Download Anomaly from the GAMMA installer author's mirror instead of ModDB. It unpacks about 9 times faster but needs about 5 GB of free memory while unpacking. If the mirror fails, ModDB is used.",
    ),
]

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


def _remaining_problems(post, presence) -> int:
    """Problems left after a repair. New files are expected then: the
    reinstalled mods' own, absent from a snapshot taken while broken."""
    count = len(post.changed) + len(post.removed) + len(post.errors)
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
        # -- Installation Directory / Install Dependencies - merged into
        # one card, side by side, same grid+divider pattern as the
        # Anomaly/GAMMA card below. --
        self.directory_deps_card, dd_layout = make_card()
        root.addWidget(self.directory_deps_card)
        dd_grid = QGridLayout()
        dd_grid.setHorizontalSpacing(32)
        dd_grid.setVerticalSpacing(10)
        dd_grid.setColumnStretch(0, 1)
        dd_grid.setColumnStretch(1, 0)
        dd_grid.setColumnStretch(2, 1)
        dd_layout.addLayout(dd_grid)

        dd_divider = QFrame()
        dd_divider.setObjectName("installDivider")
        dd_divider.setFrameShape(QFrame.Shape.NoFrame)
        dd_divider.setFixedWidth(1)
        dd_grid.addWidget(dd_divider, 0, 1, 5, 1)

        self.wt_status = InstallStatusRow("", ok=None, pending_text="Checking")
        dd_grid.addLayout(make_header_row("Installation Directory"), 0, 0)
        dd_grid.addLayout(
            make_header_row("Install Dependencies", self.wt_status), 0, 2
        )

        dd_grid.addWidget(
            info_label(
                tr("<span style='color:{arg};'>Step 1.</span> Choose where to install Anomaly and GAMMA.", arg=ACCENT.name())
            ),
            1, 0,
        )
        dd_grid.addWidget(
            info_label(
                tr("<span style='color:{arg};'>Step 2.</span> Install the Windows runtimes the game needs.", arg=ACCENT.name())
            ),
            1, 2,
        )

        self.root_card_edit, self.root_card_row = (
            self._make_folder_row(
                tr("Installation directory:"),
                self._browse_install_root,
            )
        )
        dd_grid.addLayout(self.root_card_row, 2, 0)
        self.wt_prefix_label = info_label("", wrap=False)
        self.wt_prefix_label.setObjectName("dim")
        dd_grid.addWidget(self.wt_prefix_label, 2, 2)

        self.create_folders_button = QPushButton(tr("Create folders"))
        self.create_folders_button.setObjectName("primary")
        self.create_folders_button.clicked.connect(self._create_install_folders)
        dd_grid.addWidget(self.create_folders_button, 3, 0)

        self.winetricks_button = QPushButton(tr("Install Dependencies"))
        self.winetricks_button.setObjectName("primary")
        self.winetricks_button.setToolTip(
            tr("Installs required Windows runtime components (VC++, DirectX, etc.) into the Wine prefix.")
        )
        self.winetricks_button.clicked.connect(self._start_winetricks)
        dd_grid.addWidget(self.winetricks_button, 3, 2)

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
        dd_grid.addWidget(self.wt_progress, 4, 0, 1, 3)
        # Anomaly and GAMMA merged into one card, side by side in a grid
        # instead of two separately-bordered cards of very different
        # heights (which left a large empty gap under the shorter
        # Anomaly column). Grid rows are shared across both columns, so
        # the status row, header, info text, button and progress area
        # all line up at the same height in both columns regardless of
        # how much content sits in between (the "main content" cell,
        # row 3, is the only one that can differ in height between the
        # two columns - everything below it still lines back up).
        self.anomaly_gamma_card, ag_layout = make_card()
        root.addWidget(self.anomaly_gamma_card)
        grid = QGridLayout()
        grid.setHorizontalSpacing(32)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(2, 1)
        ag_layout.addLayout(grid)

        # Vertical divider between the two columns - ends after the
        # install buttons (row 7), stopping above the shared progress
        # console (row 8), which spans the full card width on its own.
        # Styled via #installDivider in themes.py (a plain background
        # color, not the native QFrame bevel) so it follows the active
        # theme's border color instead of a fixed OS palette look.
        divider = QFrame()
        divider.setObjectName("installDivider")
        divider.setFrameShape(QFrame.Shape.NoFrame)
        divider.setFixedWidth(1)
        grid.addWidget(divider, 0, 1, 8, 1)
        # Keeps the divider centred: _reflow_gamma_options drops the
        # GAMMA options to one column when two don't fit in half the card.
        self.anomaly_gamma_card.installEventFilter(self)

        self.anomaly_status = InstallStatusRow("")
        self.gamma_status = InstallStatusRow("")
        grid.addLayout(
            make_header_row("STALKER Anomaly", self.anomaly_status), 0, 0
        )
        grid.addLayout(
            make_header_row("GAMMA Modpack", self.gamma_status), 0, 2
        )

        grid.addWidget(
            info_label(
                tr("<span style='color:{arg};'>Step 3.</span> Download and install STALKER Anomaly 1.5.3.", arg=ACCENT.name())
            ),
            1, 0,
        )
        grid.addWidget(
            info_label(
                tr("<span style='color:{arg};'>Step 4.</span> Download and install GAMMA and all its mods.", arg=ACCENT.name())
            ),
            1, 2,
        )

        # -- "Anomaly folder" / "GAMMA folder" - aligned on the same row. --
        self.anomaly_edit, self.anomaly_folder_row = (
            self._make_folder_row(tr("Anomaly folder:"), self._browse_anomaly)
        )
        grid.addLayout(self.anomaly_folder_row, 2, 0)
        self.gamma_edit, self.gamma_folder_row = (
            self._make_folder_row(tr("GAMMA folder:"), self._browse_gamma)
        )
        grid.addLayout(self.gamma_folder_row, 2, 2)

        # -- "Install options" (Anomaly) / "Install options" (GAMMA) -
        # same row, same size (QGridLayout stretches same-row
        # Preferred-policy widgets to the shared row height when nothing
        # else occupies that row). The first two checkboxes are GUI-level
        # orchestration, not CLI arguments - see
        # _start_anomaly_install/_on_anomaly_finished. The mirror one is
        # the CLI's --use-anomaly-mirror. --
        anomaly_opts_group = QGroupBox(tr("Install options"))
        anomaly_opts_layout = QVBoxLayout()
        anomaly_opts_layout.setSpacing(6)
        anomaly_opts_layout.setContentsMargins(0, 8, 0, 0)  # borderless: line up with the folder row
        self.anomaly_checkboxes = {}
        for key, label, tooltip in _ANOMALY_CHECKBOXES:
            cb = QCheckBox(tr(label))
            cb.setToolTip(tr(tooltip))
            cb.setChecked(key in _DEFAULT_CHECKED_OPTIONS)
            self.anomaly_checkboxes[key] = cb
            anomaly_opts_layout.addWidget(cb)
        anomaly_opts_group.setLayout(anomaly_opts_layout)
        grid.addWidget(anomaly_opts_group, 3, 0)

        opts_group = QGroupBox(tr("Install options"))
        # Two columns of three: one long list read as a wall of boxes.
        opts_layout = QGridLayout()
        opts_layout.setVerticalSpacing(6)
        opts_layout.setHorizontalSpacing(16)
        opts_layout.setContentsMargins(0, 8, 0, 0)  # borderless: line up with the folder row
        self.checkboxes = {}
        for key, label, tooltip in _CHECKBOXES:
            cb = QCheckBox(tr(label))
            cb.setToolTip(tr(tooltip))
            cb.setChecked(key in _DEFAULT_CHECKED_OPTIONS)
            self.checkboxes[key] = cb
        opts_group.setLayout(opts_layout)
        self._opts_layout = opts_layout
        self._opts_columns = 0
        self._ag_grid = grid
        self._reflow_gamma_options()
        grid.addWidget(opts_group, 3, 2)

        # Cache folder selector is GAMMA-only, so it gets its own row
        # (column 0 stays empty here) rather than being bundled into a
        # cell that has to stay comparable in height with the Anomaly
        # side. No section title above it - "Cache folder:" on the row
        # itself already says what it is.
        self.cache_edit, self.cache_folder_row = (
            self._make_folder_row(tr("Cache folder:"), self._browse_cache)
        )
        grid.addLayout(self.cache_folder_row, 5, 2)

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
        grid.addWidget(self.offline_box, 6, 2)
        self.checkboxes["offline"].toggled.connect(self._on_offline_toggled)
        self.checkboxes["offline"].toggled.connect(lambda _on: self._update_button_states())

        # -- Install buttons, aligned on the same row --
        self.anomaly_button = QPushButton(tr("Install Anomaly"))
        self.anomaly_button.setObjectName("primary")
        # Wrapped: clicked() passes a bool that would land in skip_confirm.
        self.anomaly_button.clicked.connect(lambda: self._start_anomaly_install())
        grid.addWidget(self.anomaly_button, 7, 0)

        self.install_button = QPushButton(tr("Install GAMMA"))
        self.install_button.setObjectName("primary")
        self.install_button.setToolTip(
            tr("Install or update GAMMA. Anomaly is installed first if it is missing.")
        )
        # Wrapped: clicked() passes a bool that would land in skip_confirm.
        self.install_button.clicked.connect(lambda: self._start_full_install())
        grid.addWidget(self.install_button, 7, 2)

        # -- Shared progress console for both Anomaly and GAMMA installs,
        # spanning the full card width right under the two buttons -
        # there is only ever one install running at a time, so one wide
        # console (with the Addon/Operation/Percent table) is clearer
        # than two separate bars, and avoids the empty space a lone
        # Anomaly-only bar left underneath.
        self.full_progress = ProgressArea(show_log=False)
        self.full_progress.cancel_button.clicked.connect(self._cancel_full_install)
        grid.addWidget(self.full_progress, 8, 0, 1, 3)
        self.verify_card, v_layout = make_card()
        root.addWidget(self.verify_card)
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
        v_layout.addLayout(make_header_row("Verify Integrity", pill_box))
        v_layout.addWidget(
            info_label(
                tr("<span style='color:{arg};'>Step 5.</span> Find and repair missing or changed game files. Your saves and settings are left alone.", arg=ACCENT.name())
            )
        )
        tiles = QGridLayout()
        tiles.setSpacing(10)
        self.verify_tiles = {
            "anomaly": _CheckTile(tr("Anomaly files")),
            "mods": _CheckTile(tr("GAMMA mods")),
            "md5": _CheckTile(tr("Mod file changes")),
            "cache": _CheckTile(tr("Download cache")),
        }
        self.verify_tiles["anomaly"].setToolTip(
            tr("Every Anomaly file is compared with Anomaly's own list of original checksums.")
        )
        self.verify_tiles["mods"].setToolTip(
            tr("Every enabled mod in your Mod Organizer profile must have a folder with files in it.")
        )
        self.verify_tiles["md5"].setToolTip(
            tr("Finds mod files changed since the last check. The first check after an install or mod change only records them.")
        )
        self.verify_tiles["cache"].setToolTip(
            tr("Checks downloaded archives against GAMMA's list. Only affects future installs, never the game.")
        )
        for index, tile in enumerate(self.verify_tiles.values()):
            if index:
                divider = QFrame()
                divider.setObjectName("vDivider")
                divider.setFixedWidth(1)
                tiles.addWidget(divider, 0, index * 2 - 1)
            tiles.addWidget(tile, 0, index * 2)
            tiles.setColumnStretch(index * 2, 1)
        v_layout.addLayout(tiles)
        self.verify_maintenance_label = QLabel(
            tr("Verify Integrity is currently under maintenance")
        )
        self.verify_maintenance_label.setStyleSheet(
            f"color: {STATUS_RED.name()}; font-weight: bold;"
        )
        self.verify_maintenance_label.setVisible(VERIFY_INTEGRITY_DISABLED)
        v_layout.addWidget(self.verify_maintenance_label)
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
        buttons = QHBoxLayout()
        buttons.addWidget(self.verify_button, 1)
        buttons.addWidget(self.repair_button, 1)
        v_layout.addLayout(buttons)
        # No status line: the bar's text already names the step.
        self.verify_progress = ProgressArea(
            show_table=False, show_log=True, log_max_height=180, show_status=False
        )
        # Only offered while a check or repair runs (see _set_verify_console).
        self.verify_progress.log_toggle.hide()
        self.verify_progress.cancel_button.clicked.connect(self._cancel_verify)
        # The tiles and pill already say "not checked"; an empty Idle bar adds nothing.
        self.verify_progress.bar.hide()
        v_layout.addWidget(self.verify_progress)
        self.verify_findings = QVBoxLayout()
        self.verify_findings.setSpacing(6)
        v_layout.addLayout(self.verify_findings)
        # Spare window height goes below the last card, not inside the
        # cards - the console grows the card only when it is shown.
        root.addStretch(1)
        self._pending_repair: tuple | None = None
        self.refresh()

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
        self.wt_prefix_label.setText(tr("Prefix: {arg}", arg=self._wt_prefix()))
        self._refresh_winetricks_status()
        self._update_button_states()

    def on_busy_changed(self, _busy):
        """Global install lock changed; re-evaluate this page's controls."""
        self._update_button_states()

    def on_install_activity_changed(self, operation):
        """Reflect the active Anomaly or GAMMA install in the status rows."""
        # Recompute both rows: when Anomaly chains into GAMMA, Anomaly's row
        # must flip to Installed as GAMMA's flips to Installing.
        if operation in ("anomaly", "gamma", None):
            self._update_install_status()

    def _update_button_states(self):
        busy = self.window.install_busy
        self.root_card_edit.setEnabled(not busy)
        self.create_folders_button.setEnabled(not busy)
        profile = self.window.settings.active_profile
        resumable = profile is not None and self._resume_state is not None
        reinstall = (
            not resumable
            and profile is not None
            and gamma_installed(profile.gamma, profile.mo2_profile)
        )
        if resumable:
            label = tr(_RESUME_BUTTON_LABEL)
            tooltip = tr("Resume the GAMMA install where it stopped. Mods that are already installed are skipped.")
        elif reinstall:
            label = tr("Reinstall GAMMA offline") if self._offline_lists() else tr("Reinstall GAMMA")
            tooltip = tr(
                "Unpack every GAMMA mod again over your install. Fixes broken "
                "mods, and gets the latest GitHub addons if that option is ticked."
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
            tr("Retry Anomaly") if self._anomaly_failed else tr("Install Anomaly")
        )
        self.anomaly_button.setEnabled((not anomaly or self._anomaly_failed) and not offline)
        self.anomaly_button.setToolTip(
            tr("Offline install unpacks Anomaly from the cache together with GAMMA. Use the install button on the right.")
            if offline and not anomaly
            else ""
        )
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
        # Nothing left to watch once both are installed and idle.
        done = anomaly_state is True and gamma_state is True and self._resume_state is None
        toggle = self.full_progress.table_toggle
        toggle.setVisible(not done)
        if done and self.full_progress.table.isVisible():
            self.full_progress._toggle_table()

    def _browse_install_root(self):
        self._pick_folder(self.root_card_edit, tr("Select install root directory"))

    def _create_install_folders(self):
        """Create the anomaly/gamma/cache folders under the chosen install root.

        When no root is entered, prompt the user to pick one. Always fills and
        persists the three per-step folder paths so installs just work.
        """
        if self.window.install_busy:
            self._update_button_states()
            return
        root = self.root_card_edit.text().strip()
        if not root:
            root = QFileDialog.getExistingDirectory(
                self, tr("Select install root directory"), str(Path.home())
            )
            if not root:
                return
            self.root_card_edit.setText(root)
        base = Path(root).expanduser()
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
                return
        self.anomaly_edit.setText(str(folders["Anomaly folder"]))
        self.gamma_edit.setText(str(folders["GAMMA folder"]))
        self.cache_edit.setText(str(folders["Cache folder"]))
        if not self._persist_dirs():
            return
        self.window.statusBar().showMessage(
            tr("Created and set install folders: Anomaly, GAMMA, cache"), 6000
        )
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

    def eventFilter(self, obj, event):
        if obj is self.anomaly_gamma_card and event.type() == QEvent.Type.Resize:
            self._reflow_gamma_options()
        return super().eventFilter(obj, event)

    def _reflow_gamma_options(self):
        """GAMMA install options in two columns of three when they fit in
        half the Anomaly/GAMMA card, else one column - two columns made
        the GAMMA side wider and pushed the divider off centre."""
        boxes = list(self.checkboxes.values())
        rows = (len(boxes) + 1) // 2
        layout = self._opts_layout
        two_wide = (
            max(cb.sizeHint().width() for cb in boxes[:rows])
            + layout.horizontalSpacing()
            + max(cb.sizeHint().width() for cb in boxes[rows:])
        )
        # Card width, not the grid's: on the card's Resize event the
        # grid geometry is still the old one.
        card = self.anomaly_gamma_card
        margins = card.layout().contentsMargins()
        inner = card.width() - margins.left() - margins.right()
        half = (inner - 2 * self._ag_grid.horizontalSpacing() - 1) // 2
        columns = 2 if half >= two_wide else 1
        if columns == self._opts_columns:
            return
        self._opts_columns = columns
        rows = len(boxes) if columns == 1 else rows
        for cb in boxes:
            layout.removeWidget(cb)
        for index, cb in enumerate(boxes):
            layout.addWidget(cb, index % rows, index // rows)

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
            self._verify_anomaly_after_install = self.anomaly_checkboxes[
                "verify_after_install"
            ].isChecked()
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

        successful Anomaly install (see the "Verify files after install"
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
            self._start_full_install(skip_confirm=True)
            return
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
        self._anomaly_recheck_done = False
        self._pending_repair = None
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
            return (_GAMMA_NOT_INSTALLED, None, None, {}, False, None)
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
        report("Starting full MD5 scan of mod files")
        scan = scan_mods_md5(
            profile.gamma,
            on_progress=(
                lambda done, total, size: report(
                    f"MD5 hashing {done}/{total} files ({size})"
                )
            ),
            cancel=self._scan_cancel,
        )
        plan = None
        report("Checking GAMMA download cache")
        records = fetch_modpack_records(profile.mod_pack_maker_url)
        expected = expected_archive_md5s(records)
        cache_result = (
            verify_cache_archives(
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
            plan = classify_problems(scan, records, extra_broken_folders=extra_broken)
        return (presence, scan, plan, records, official_missing, cache_result)

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
        for line in presence.lines():
            self.verify_progress.log.append_line(line)
        for line in scan.lines():
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

    def _offer_repair(self, anomaly_needs_repair: bool, gamma_repairable: bool) -> None:
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
            (profile.anomaly, profile.gamma, profile.cache),
        )
        self.repair_button.show()

    def _on_repair_clicked(self) -> None:
        pending = self._pending_repair
        profile = self.window.settings.active_profile
        if (
            pending is None
            or profile is None
            or pending[2] != (profile.anomaly, profile.gamma, profile.cache)
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
        if not self._prompt_repair(pending[0], pending[1]):
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
        self, anomaly_needs_repair: bool, gamma_repairable: bool
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
            sections.append(tr("GAMMA mods to download again:") + "\n\n" + shown)
            sections.append(
                tr(
                    "The old copies are set aside first, not deleted. If the "
                    "repair fails or you stop it, they're put back. Your own "
                    "mods and files are never touched."
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
        self._conclude_verify()

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
                    tr("GAMMA mods downloaded again"),
                    tr("Your saves, user.ltx and MCM settings were kept."),
                    list(self._repaired_folders),
                    len(self._repaired_folders),
                )
            )
        profile = self.window.settings.active_profile
        findings = build_findings(
            anomaly_checked=self._anomaly_checked or self._verify_anomaly_ok,
            extra=extra,
            anomaly_problem_lines=self._anomaly_problem_lines,
            anomaly_root=profile.anomaly if profile is not None else "",
            presence=self._presence,
            scan=self._scan,
            cache=self._cache_archive_result,
            plan=self._repair_plan,
            official_missing=self._official_missing,
            gamma_skipped=self._gamma_skipped,
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
        if anomaly_needs_repair or gamma_repairable:
            self._offer_repair(anomaly_needs_repair, gamma_repairable)
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

    def _start_gamma_repair_deletion(self) -> None:
        self.verify_progress.cancel_button.show()
        self.verify_progress.status_message(tr("Setting broken mods aside"))
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Repairing GAMMA mods ==")
        task = StreamTask(self._run_repair_quarantine, parent=self)
        task.line.connect(self._on_gamma_verify_progress)
        task.result.connect(self._on_repair_quarantined)
        task.error.connect(self._on_gamma_verify_error)
        self._verify_task = task
        task.start()

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
        for folder in self._repair_plan.repairable:
            if self._scan_cancel is not None and self._scan_cancel.is_set():
                report("Repair cancelled - quarantine aborted.")
                break
            report(f"Setting aside {folder}")
            try:
                quarantined.append(
                    quarantine_mod_and_archive(
                        profile.gamma, folder, self._repair_records.get(folder)
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
        runner = CommandRunner(
            cli_command(_repair_install_args(), progress_interval_ms=200),
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
        if settled.restored:
            self.verify_progress.log.append_line(
                "WARNING: the installer did not reinstall "
                f"{len(settled.restored)} mod(s); their previous copies were "
                "put back instead:"
            )
            for folder in settled.restored:
                self.verify_progress.log.append_line(f"  {folder}")
        for line in settled.failures:
            self.verify_progress.log.append_line(f"WARNING: {line}")
        self._repair_quarantined_count = len(settled.reinstalled)
        self._repaired_folders = list(settled.reinstalled)
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
        if _remaining_problems(post, presence) == 0:
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
        remaining = _remaining_problems(post, presence)
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
            # Never the success colour for snapshot/attention: a snapshot run
            # proved nothing about mod file contents, and "attention" is not
            # a pass (QProgressBar[level=...] in the theme).
            self.verify_progress.set_success_state(
                bar_text, "" if verdict == "healthy" else verdict
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
        if not checked:
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
        if cache is None:
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
        self._wt_installed = None
        self._update_button_states()
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

    def _start_winetricks(self):
        self._winetricks_status_enabled = True
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
        if dialog is not None:
            dialog.hide()
            dialog.deleteLater()
        if rc is None:
            return
        if rc == 0:
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
