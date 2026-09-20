"""
Install page: Anomaly + GAMMA install (top), cache folder (middle),
winetricks and verify (bottom).
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings
from ..cli_runner import cli_command
from ..config import logs_dir
from ..dependencies import check_all_dependencies
from ..gui_settings import configured_runner, configured_wine_prefix
from ..integrity import (
    CacheArchiveVerifyResult,
    anomaly_status,
    fetch_official_mod_names,
    invalidate_baseline,
    is_expected_gamma_overlay_corrupt,
    scan_mods_md5,
    verify_cache_archives,
    verify_gamma,
)
from ..launcher import LaunchError
from ..parsers import ProgressEvent, parse_progress_line, strip_ansi
from ..repair import (
    classify_problems,
    fetch_modpack_records,
    purge_quarantine,
    quarantine_mod_and_archive,
    restore_from_quarantine,
)
from ..settings import cli_ok
from ..winetricks import (
    WINETRICKS_VERBS,
    check_winetricks_full_status,
    protontricks_binary,
    protontricks_install_command,
    umu_binary,
    umu_install_command,
    winetricks_install_command,
)
from .common import (
    ACCENT,
    STATUS_RED,
    BackgroundTask,
    CommandRunner,
    InstallStatusRow,
    ProgressArea,
    StreamTask,
    anomaly_installed,
    display_state,
    free_space_bytes,
    gamma_installed,
    human_size,
    info_label,
    make_card,
    make_header_row,
    mo2_running,
    normalize_path,
    notify_desktop,
    section_label,
    tr,
    update_cache_label,
    winetricks_tooltip,
)

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
            "minimal",
            "Minimal (~100 GB)",
            "Delete addon archives after extraction to save ~50 GB of disk space.",
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
            "auto_retry_large_files",
            "Auto-retry on large file pack failures",
            "Automatically retry if a large download fails, up to 10 times.",
        ),
    )
]

#: Checkboxes above (by key) that start checked on a fresh InstallPage -
#: none of these are persisted anywhere (see gui_settings.py), so this is
#: only the initial state each time the page is constructed, same as
#: every other checkbox here; the user can still uncheck for the current
#: session.
_DEFAULT_CHECKED_OPTIONS = {"auto_retry_large_files", "auto_continue_gamma"}

_ANOMALY_CHECKBOXES = [
    (
        "verify_after_install",
        "Verify files after install",
        "Run an integrity check on Anomaly after it finishes installing.",
    ),
    (
        "auto_continue_gamma",
        "Automatically install GAMMA after Anomaly",
        "Once Anomaly finishes, automatically start the GAMMA modpack install without needing to click Install GAMMA yourself.",
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
# determinate staged style used for Anomaly: umu first, then protontricks,
# then the winetricks verbs fill the remainder.
_WT_STAGE_RANGES = {
    "umu": (0, 15),
    "tools": (15, 35),
    "verbs": (35, 100),
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
) -> list[str]:
    """Build the full-install argv.

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
    return args


def _verify_phase_value(start: int, end: int, fraction: float) -> int:
    """Map a 0-1 fraction onto a phase range of the overall verify bar."""
    fraction = max(0.0, min(1.0, fraction))
    return round(start + (end - start) * fraction)


# Overall-bar ranges for the Verify Integrity phases.
_VERIFY_PHASE = {
    "presence": (10, 15),
    "md5": (15, 95),
}

_GAMMA_NOT_INSTALLED = "__gamma_not_installed__"

VERIFY_INTEGRITY_DISABLED = False


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
        "--preserve-user-settings",
        "--preserve-mcm-settings",
    ]


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
    if _looks_like_network_failure(output):
        summary = tr(
            "This looks like a temporary GitHub or network problem, not an issue with your setup - the download or connection was interrupted partway through."
        )
    else:
        summary = tr("The install stopped unexpectedly.")
    parts = [
        summary,
        tr(
            "Downloads from GitHub and moddb can fail partway through, especially for large files - this can take a few attempts before it succeeds."
        ),
    ]
    if _looks_like_special_repo_clone_failure(output):
        parts.append(
            tr(
                "This specific step (downloading the GAMMA setup/large-files repository) always restarts from scratch on the next attempt - it isn't cached and resumed like regular mod archives are, so a dropped connection here means redoing this one large download again."
            )
        )
        if _looks_like_gamma_large_files_failure(output):
            parts.append(
                tr(
                    "If your download is failing on gamma_large_files_v2, this is most likely a GitHub rate limit, and not a problem with COMMANDER or your setup. You can try to bypass this rate limit by switching networks or using a VPN. Or try downloading again later."
                )
            )
    if resume_hint:
        parts.append(resume_hint)
    parts.append(tr("Full logs: {arg}", arg=logs_dir()))
    return "\n\n".join(parts), detail


class InstallPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.setObjectName("installPage")
        # Ensure ~/.local/bin is on PATH so umu-run installed there is discoverable.
        local_bin = os.path.expanduser("~/.local/bin")
        if local_bin not in os.environ.get("PATH", ""):
            os.environ["PATH"] = local_bin + ":" + os.environ.get("PATH", "")
        self.window = window
        self._runner = None
        self._anomaly_runner = None
        self._post_anomaly_verify_runner = None
        self._post_anomaly_verify_cancelled = False
        self._verify_anomaly_after_install = False
        self._auto_chain = False
        self._resume_state: dict[str, str] | None = None
        self._auto_cancelled = False
        self._auto_retry_count = 0
        self._active_preserve_user = False
        self._active_preserve_mcm = False
        self._checked_archives: set[str] = set()
        self._verify_runner = None
        self._verify_task = None
        self._verify_counts = {"OK": 0, "CORRUPT": 0, "NOT FOUND": 0}
        self._verify_anomaly_ok = False
        self._scan_cancel = None
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
        self._gamma_remaining_issues: int | None = None
        self._official_missing = False
        self._cache_archive_result: CacheArchiveVerifyResult | None = None
        self._anomaly_recheck_done = False
        self._wt_runner = None
        self._wt_task = None
        self._wt_checking = False
        self._wt_installed: bool | None = None
        self._wt_stage = "verbs"
        self._wt_completed_verbs: set[str] = set()
        self._wt_last_pct = -1
        self._winetricks_status_enabled = False
        self._persisting = False
        # Refreshes the download-cache archive count live while a GAMMA install
        # is running (see _refresh_cache_count).
        self._cache_timer = QTimer(self)
        self._cache_timer.setInterval(1200)
        self._cache_timer.timeout.connect(self._refresh_cache_count)
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
                tr("<span style='color:{arg};'>Step 1.</span> Select a directory where you would like STALKER Anomaly and GAMMA.", arg=ACCENT.name())
            ),
            1, 0,
        )
        dd_grid.addWidget(
            info_label(
                tr("<span style='color:{arg};'>Step 2.</span> Install required dependencies - Downloads Visual C++, DirectX and winetricks runtimes.", arg=ACCENT.name())
            ),
            1, 2,
        )

        self.root_card_edit, self.root_card_browse, self.root_card_row = (
            self._make_folder_row(
                "Installation directory:",
                self._browse_install_root,
                placeholder="Select an installation directory",
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
        root.addWidget(self.anomaly_gamma_card, 1)
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
                tr("<span style='color:{arg};'>Step 3.</span> Install STALKER Anomaly - Downloads and installs Anomaly 1.5.3.", arg=ACCENT.name())
            ),
            1, 0,
        )
        grid.addWidget(
            info_label(
                tr("<span style='color:{arg};'>Step 4.</span> Install STALKER GAMMA - Downloads and installs all GAMMA mods.", arg=ACCENT.name())
            ),
            1, 2,
        )

        # -- "Anomaly folder" / "GAMMA folder" - aligned on the same row. --
        self.anomaly_edit, self.anomaly_browse, self.anomaly_folder_row = (
            self._make_folder_row("Anomaly folder:", self._browse_anomaly)
        )
        grid.addLayout(self.anomaly_folder_row, 2, 0)
        self.gamma_edit, self.gamma_browse, self.gamma_folder_row = (
            self._make_folder_row("GAMMA folder:", self._browse_gamma)
        )
        grid.addLayout(self.gamma_folder_row, 2, 2)

        # -- "Install options" (Anomaly) / "Install options" (GAMMA) -
        # same row, same size (QGridLayout stretches same-row
        # Preferred-policy widgets to the shared row height when nothing
        # else occupies that row). Anomaly's own `anomaly install` CLI
        # command has no user-facing flags at all, so these two
        # checkboxes are GUI-level orchestration, not CLI arguments -
        # see _start_anomaly_install/_on_anomaly_finished. --
        anomaly_opts_group = QGroupBox(tr("Install options"))
        anomaly_opts_layout = QVBoxLayout()
        anomaly_opts_layout.setSpacing(6)
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
        opts_layout = QVBoxLayout()
        opts_layout.setSpacing(6)
        self.checkboxes = {}
        for key, label, tooltip in _CHECKBOXES:
            cb = QCheckBox(tr(label))
            cb.setToolTip(tr(tooltip))
            cb.setChecked(key in _DEFAULT_CHECKED_OPTIONS)
            self.checkboxes[key] = cb
            opts_layout.addWidget(cb)
        opts_group.setLayout(opts_layout)
        grid.addWidget(opts_group, 3, 2)

        # Cache folder selector is GAMMA-only, so it gets its own row
        # (column 0 stays empty here) rather than being bundled into a
        # cell that has to stay comparable in height with the Anomaly
        # side. No section title above it - "Cache folder:" on the row
        # itself already says what it is.
        self.cache_edit, self.cache_browse, self.cache_folder_row = (
            self._make_folder_row("Cache folder:", self._browse_cache)
        )
        grid.addLayout(self.cache_folder_row, 5, 2)

        # cache_info_label ("N archives cached") is kept but not shown on
        # this page - _update_cache_info's call sites stay unchanged, they
        # just update an off-screen label now.
        self.cache_info_label = info_label("")
        self.cache_info_label.setObjectName("dim")

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
        root.addWidget(self.verify_card, 1)
        v_layout.addWidget(section_label(tr("Verify Integrity"), level=2))
        v_layout.addWidget(
            info_label(
                tr("<span style='color:{arg};'>Step 5.</span> (Optional) Verify your game files by running an MD5 check across Anomaly and GAMMA. This will repair any missing/corrupted mods by redownloading and repairing.", arg=ACCENT.name())
            )
        )
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
        v_layout.addWidget(self.verify_button)
        self.verify_progress = ProgressArea(show_table=False, show_log=True, log_max_height=180)
        self.verify_progress.cancel_button.clicked.connect(self._cancel_verify)
        v_layout.addWidget(self.verify_progress)
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
            self._update_cache_info(profile.cache)
            if not self.window.install_busy:
                # Anomaly's installed/not-installed state is already shown
                # by anomaly_status above - the shared progress console
                # below only needs to reflect the overall/GAMMA state here.
                op = getattr(self.window, "install_operation", None)
                if gamma_installed(profile.gamma, profile.mo2_profile):
                    self.full_progress.bar.setRange(0, 1)
                    self.full_progress.bar.setValue(1)
                    self.full_progress.bar.setFormat(
                        "Incomplete" if self._resume_state is not None else "Installed"
                    )
                elif op != "gamma":
                    self.full_progress.bar.setRange(0, 1)
                    self.full_progress.bar.setValue(0)
                    self.full_progress.bar.setFormat("Not installed")
        else:
            self._update_cache_info("")
        self._update_install_status()
        self.wt_prefix_label.setText(tr("Prefix: {arg}", arg=self._wt_prefix()))
        self._refresh_winetricks_status()
        self._update_button_states()

    def on_busy_changed(self, _busy):
        """Global install lock changed; re-evaluate this page's controls."""
        self._update_button_states()

    def on_install_activity_changed(self, operation):
        """Reflect the active Anomaly or GAMMA install in the status rows."""
        if operation == "anomaly":
            self.anomaly_status.set_installing("Installing Anomaly...")
        elif operation == "gamma":
            self.gamma_status.set_installing("Installing GAMMA...")
        elif operation is None:
            self._update_install_status()

    def _update_button_states(self):
        busy = self.window.install_busy
        self.root_card_edit.setEnabled(not busy)
        self.root_card_browse.setEnabled(not busy)
        self.create_folders_button.setEnabled(not busy)
        profile = self.window.settings.active_profile
        resumable = profile is not None and self._resume_state is not None
        self.install_button.setText(
            tr(_RESUME_BUTTON_LABEL) if resumable else tr("Install GAMMA")
        )
        self.install_button.setToolTip(
            tr("Resume the interrupted GAMMA installation using valid cached archives.")
            if resumable
            else tr("Install or update GAMMA. Anomaly is installed first if it is missing.")
        )
        if busy or profile is None:
            self.anomaly_button.setEnabled(False)
            self.install_button.setEnabled(False)
            self.verify_button.setEnabled(False)
            self.winetricks_button.setEnabled(False)
            self.anomaly_browse.setEnabled(False)
            self.gamma_browse.setEnabled(False)
            self.cache_browse.setEnabled(False)
            self.anomaly_edit.setReadOnly(True)
            self.gamma_edit.setReadOnly(True)
            self.cache_edit.setReadOnly(True)
            for cb in self.checkboxes.values():
                cb.setEnabled(False)
            for cb in self.anomaly_checkboxes.values():
                cb.setEnabled(False)
            return
        anomaly = anomaly_installed(profile.anomaly)
        self.anomaly_button.setEnabled(not anomaly)
        self.install_button.setEnabled(
            resumable or not gamma_installed(profile.gamma, profile.mo2_profile)
        )
        self.verify_button.setEnabled(not mo2_running() and not VERIFY_INTEGRITY_DISABLED)
        self.winetricks_button.setEnabled(
            self._wt_installed is False and not mo2_running()
        )
        self.anomaly_browse.setEnabled(True)
        self.gamma_browse.setEnabled(True)
        self.cache_browse.setEnabled(True)
        self.anomaly_edit.setReadOnly(False)
        self.gamma_edit.setReadOnly(False)
        self.cache_edit.setReadOnly(False)
        for cb in self.checkboxes.values():
            cb.setEnabled(True)
        for cb in self.anomaly_checkboxes.values():
            cb.setEnabled(True)

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
            self.anomaly_status.set_installing("Installing Anomaly...")
        else:
            self.anomaly_status.set_state(bool(anomaly_state))
        if gamma_state == "installing":
            self.gamma_status.set_installing("Installing GAMMA...")
        elif gamma_state and self._resume_state is not None:
            self.gamma_status.set_incomplete(
                tr("Last install attempt failed - resume it to finish.")
            )
        else:
            self.gamma_status.set_state(bool(gamma_state))

    def _update_cache_info(self, cache_path: str) -> None:
        update_cache_label(self.cache_info_label, cache_path)

    def _refresh_cache_count(self) -> None:
        """Live-update the cache archive count while a GAMMA install runs."""
        if self._runner is None or not self._runner.is_running():
            self._cache_timer.stop()
            return
        profile = self.window.settings.active_profile
        if profile is None:
            self._cache_timer.stop()
            return
        self._update_cache_info(profile.cache)

    def _browse_install_root(self):
        start = str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Select install root directory", start
        )
        if folder:
            self.root_card_edit.setText(folder)

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
                self, "Select install root directory", str(Path.home())
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
        for name, folder in folders.items():
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    tr("Create Failed"),
                    tr("Could not create {name} ({folder}):\n{exc}", name=name, folder=folder, exc=exc),
                )
                return
        self.anomaly_edit.setText(str(folders["Anomaly folder"]))
        self.gamma_edit.setText(str(folders["GAMMA folder"]))
        self.cache_edit.setText(str(folders["Cache folder"]))
        self._update_cache_info(str(folders["Cache folder"]))
        self._persist_dirs()
        self.window.statusBar().showMessage(
            "Created and set install folders: Anomaly, GAMMA, cache", 6000
        )

    def _make_folder_row(self, label_text, on_browse, placeholder="Enter or browse to a folder..."):
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.editingFinished.connect(self._persist_dirs)
        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(on_browse)
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        row.addWidget(edit, 1)
        row.addSpacing(10)
        row.addWidget(browse)
        return (edit, browse, row)

    def _browse_anomaly(self):
        start = str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Select Anomaly install folder", start
        )
        if folder:
            self.anomaly_edit.setText(folder)
            self._persist_dirs()

    def _browse_gamma(self):
        start = str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Select GAMMA install folder", start
        )
        if folder:
            self.gamma_edit.setText(folder)
            self._persist_dirs()

    def _browse_cache(self):
        start = str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Select cache folder", start)
        if folder:
            self.cache_edit.setText(folder)
            self._update_cache_info(folder)
            self._persist_dirs()
            return

    def _persist_dirs(self):
        # editingFinished fires on focus-out, and the message boxes below steal
        # focus - without this guard the handler re-enters itself.
        if self.window.install_busy:
            self._update_button_states()
            return
        if self._persisting:
            return
        self._persisting = True
        try:
            self._persist_dirs_locked()
        finally:
            self._persisting = False

    def _persist_dirs_locked(self):
        if self.window.install_busy:
            self._update_button_states()
            return
        self.window.refresh_settings()
        profile = self.window.settings.active_profile
        if profile is None:
            QMessageBox.warning(
                self,
                tr("No Profile"),
                tr("Create or activate a profile first (Profiles page)."),
            )
            self.refresh()
            return
        anomaly = normalize_path(self.anomaly_edit.text())
        gamma = normalize_path(self.gamma_edit.text())
        cache = normalize_path(self.cache_edit.text())
        self.anomaly_edit.setText(anomaly)
        self.gamma_edit.setText(gamma)
        self.cache_edit.setText(cache)
        if not anomaly or not gamma:
            QMessageBox.warning(
                self,
                tr("Invalid Folder"),
                tr("Both install folders must be set. Reverting to the saved paths."),
            )
            self.refresh()
            return
        if not cache:
            QMessageBox.warning(
                self,
                tr("Invalid Folder"),
                tr("Cache folder must be set. Reverting to the saved path."),
            )
            self.refresh()
            return
        if (
            anomaly == profile.anomaly
            and gamma == profile.gamma
            and cache == profile.cache
        ):
            return
        old_anomaly, old_gamma, old_cache = profile.anomaly, profile.gamma, profile.cache
        profile.anomaly = anomaly
        profile.gamma = gamma
        profile.cache = cache
        try:
            self.window.settings.save()
        except OSError as exc:
            # Roll back the in-memory profile so it cannot diverge from the
            # settings.json that is still on disk.
            profile.anomaly = old_anomaly
            profile.gamma = old_gamma
            profile.cache = old_cache
            QMessageBox.warning(
                self, tr("Save Failed"), tr("Could not write settings.json:\n{exc}", exc=exc)
            )
            self.refresh()
            return
        self.window.refresh_settings()
        self._update_install_status()
        self.window.statusBar().showMessage(
            f"Install folders updated: {anomaly} | {gamma} | {cache}", 6000
        )

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
        )

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
            QMessageBox.information(self, tr("Busy"), tr("An install is already running."))
            return
        if not _is_auto_retry:
            # Any fresh, user-initiated (or auto-chained-from-Anomaly) start
            # is not a continuation of a failure streak.
            self._auto_retry_count = 0
        profile = self.window.settings.active_profile
        if profile is None:
            QMessageBox.warning(
                self,
                tr("No Profile"),
                tr("Create or activate a profile first (Profiles page)."),
            )
            return
        minimal = self.checkboxes["minimal"].isChecked()
        size_hint = "~100 GB" if minimal else "~150 GB"
        if not skip_confirm:
            # Tell the user upfront whether this run will redownload Anomaly
            # or resume a prior interrupted install, rather than only
            # surfacing it as a status-bar toast after they've already
            # clicked Yes.
            note = ""
            if self._resume_state is not None:
                note = (
                    "<br><br>Resuming a previously interrupted GAMMA "
                    "installation - valid cached archives will be reused."
                )
            elif anomaly_installed(profile.anomaly):
                note = (
                    "<br><br>Anomaly is already installed - it will not be "
                    "re-downloaded."
                )
            required_gb = 100 if minimal else 150
            free = free_space_bytes(profile.gamma)
            if free is not None and free < required_gb * 1024**3:
                note += tr(
                    "<br><br><span style='color: {arg}; font-weight: bold;'>WARNING: Only {free} free on this drive - this install needs about {required} GB.</span>",
                    arg=STATUS_RED.name(),
                    free=human_size(free),
                    required=required_gb,
                )
            answer = QMessageBox.question(
                self,
                tr("Confirm Install GAMMA"),
                tr("<html><body>This will install/update Anomaly (if not already installed) and all GAMMA addons ({size_hint}). Existing installations are preserved.{note}<br><br><span style='color: {arg}; font-weight: bold;'>WARNING: Your user.ltx (keybindings, controls) and MCM settings will be overwritten unless you checked the preserve options above.</span><br><br>Continue?</body></html>", size_hint=size_hint, note=note, arg=STATUS_RED.name()),
                (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            # install_busy may have flipped True while the dialog was open.
            if self.window.install_busy:
                QMessageBox.information(self, tr("Busy"), tr("An install is already running."))
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
                    tr("Create Failed"),
                    tr("Could not create {name} ({path}):\n{exc}", name=name, path=path, exc=exc),
                )
                # An auto-retry re-entry finds install_busy already True
                # from the attempt that just failed - clearing it here is a
                # harmless no-op on a normal first call, but on a retry it's
                # the only thing that stops this from bricking the page
                # (no runner left to cancel, buttons stuck disabled).
                self.window.set_install_busy(False)
                self._update_button_states()
                return
        self.window.set_install_busy(True, "gamma")
        self.full_progress.reset()
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
        cmd = cli_command(
            self._build_full_command(
                skip_extract_on_hash_match=skip_extract,
                preserve_user=preserve_user,
                preserve_mcm=preserve_mcm,
            ),
            progress_interval_ms=200,
        )
        if skip_extract:
            # full_progress has no console pane; use the status bar instead.
            self.window.statusBar().showMessage(
                "Anomaly and GAMMA already installed - skipping re-extract of unchanged files.",
                6000,
            )
        if self._resume_state is not None:
            self.window.statusBar().showMessage(
                "Resuming GAMMA installation - valid cached archives will be reused.",
                6000,
            )
        self._runner = CommandRunner(cmd, parent=self)
        self._checked_archives = set()
        self._runner.line.connect(self._on_full_install_line)
        self._runner.finished.connect(self._on_full_finished)
        self._runner.cancelled.connect(
            lambda: self.full_progress.status_message("Cancelled")
        )
        self.full_progress.set_runner(self._runner)
        self.full_progress.on_started()
        self._cache_timer.start()
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
        self._cache_timer.stop()
        cancelled = self._runner is not None and self._runner.was_cancelled
        # Clear the reference so later cancel paths cannot act on a dead runner.
        self._runner = None
        self.full_progress.on_finished(rc, output)
        self.full_progress.set_runner(None)
        if cancelled:
            self.full_progress.bar.setFormat("Cancelled")
            self.full_progress.status_message("Cancelled")
            self._save_resume_state()
            self._auto_retry_count = 0
        elif not cli_ok(rc, output, ""):
            self.full_progress.status_message(f"Failed (exit code {rc})")
            self._save_resume_state()
            if _should_auto_retry(
                self.checkboxes["auto_retry_large_files"].isChecked(),
                self._auto_retry_count,
                output,
            ):
                self._auto_retry_count += 1
                self.window.statusBar().showMessage(
                    tr(
                        "A large file pack failed again - auto-retrying ({count}/{max})...",
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
                'Click "{button}" to continue - cached archives are reused automatically, so nothing that already finished has to download again.',
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
            self._show_error_popup(tr("Install Failed"), rc, output, resume_hint=hint)
        else:
            self._clear_resume_state()
            self._auto_retry_count = 0
            # A full install/reinstall (including one triggered by Fresh
            # Reset/GAMMA Reset) legitimately (re)writes every file under
            # gamma/mods - Verify Integrity's MD5 baseline must not
            # compare fresh files against a stale pre-install snapshot.
            profile = self.window.settings.active_profile
            if profile is not None:
                invalidate_baseline(profile.gamma)
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
        gui_settings.save_gui_settings(gamma_install_resume=self._resume_state)

    def _clear_resume_state(self) -> None:
        self._resume_state = None
        gui_settings.save_gui_settings(gamma_install_resume={})

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
            tr("Cancel Install"),
            tr("Installation is in progress.\n\nCancel the operation? Downloaded archives stay cached and the install can be resumed later - each cached archive is hash-verified before reuse, so a cancelled download is re-fetched automatically rather than trusted if it didn't finish cleanly."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # The install may have finished while the dialog was open, clearing
        # the runner - re-check before touching it, or this crashes.
        runner = self._active_install_runner()
        if runner is None:
            return
        if self.full_progress.is_paused:
            runner.resume()
        self.full_progress.cancel_button.setEnabled(False)
        self.full_progress.cancel_button.setText(tr("Cancelling..."))
        self.full_progress.status_message("Cancelling installation...")
        runner.cancel()

    def _start_anomaly_install(self, skip_confirm=False, respect_options=True):
        if self._anomaly_runner is not None and self._anomaly_runner.is_running():
            return
        if self.window.install_busy and not skip_confirm:
            QMessageBox.information(self, tr("Busy"), tr("An install is already running."))
            return
        profile = self.window.settings.active_profile
        if profile is None:
            QMessageBox.warning(
                self,
                tr("No Profile"),
                tr("Create or activate a profile first (Profiles page)."),
            )
            return
        if not skip_confirm:
            message = tr("Download and install STALKER Anomaly 1.5.3?")
            required_gb = 20
            free = free_space_bytes(profile.anomaly)
            if free is not None and free < required_gb * 1024**3:
                message += "\n\n" + tr(
                    "WARNING: Only {free} free on this drive - this install needs about {required} GB.",
                    free=human_size(free),
                    required=required_gb,
                )
            answer = QMessageBox.question(
                self,
                tr("Confirm Anomaly Install"),
                message,
                (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            # install_busy may have flipped True while the dialog was open.
            if self.window.install_busy:
                QMessageBox.information(self, tr("Busy"), tr("An install is already running."))
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
                    tr("Create Failed"),
                    tr("Could not create Anomaly folder ({anomaly}):\n{exc}", anomaly=profile.anomaly, exc=exc),
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
        self.window.set_install_busy(True, "anomaly")
        self.full_progress.reset()
        self.anomaly_button.setEnabled(False)
        self.install_button.setEnabled(False)
        cmd = cli_command(["anomaly", "install"], progress_interval_ms=200)
        self._anomaly_runner = CommandRunner(cmd, parent=self)
        self._anomaly_runner.line.connect(self.full_progress.on_line)
        self._anomaly_runner.finished.connect(self._on_anomaly_finished)
        self._anomaly_runner.cancelled.connect(
            lambda: self.full_progress.status_message("Cancelled")
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
        self._auto_chain = include_anomaly
        self._auto_cancelled = False
        if include_anomaly:
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
        if cancelled:
            self.full_progress.status_message("Cancelled")
        elif not install_ok:
            self.full_progress.status_message(f"Failed (exit code {rc})")
            self._show_error_popup(
                tr("Anomaly Install Failed"),
                rc,
                output,
                resume_hint=tr("Try the Anomaly install again."),
            )
        self._update_install_status()
        # Single-shot: consumed unconditionally on every outcome so a
        # failed/cancelled run cannot leave it armed for a later, unrelated
        # (e.g. Reset-driven) anomaly install that never asked for it.
        should_verify = self._verify_anomaly_after_install
        self._verify_anomaly_after_install = False
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
        self.full_progress.status_message(tr("Verifying Anomaly files..."))
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
        if cancelled:
            self.full_progress.status_message("Cancelled")
        elif cli_ok(rc, output, ""):
            self.full_progress.status_message(
                tr("Anomaly files verified - no issues found.")
            )
        else:
            self.full_progress.status_message(
                tr("Anomaly verification found issues - see Verify Integrity for details.")
            )
        self._finish_anomaly_sequence(cancelled, True)

    def _finish_anomaly_sequence(self, cancelled: bool, install_ok: bool) -> None:
        """Deliver the final outcome of an Anomaly install (and its

        optional post-install verify), continuing into GAMMA if the
        "Continue to GAMMA install automatically" checkbox armed it.
        """
        chain = self._auto_chain
        # The chain is single-shot: clear it on every outcome so a failed
        # anomaly install cannot leave it armed for a later manual run.
        self._auto_chain = False
        if chain and not cancelled and install_ok:
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
        self.window.set_install_busy(False)

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
                tr("Verify Integrity"),
                tr("Verify Integrity is currently under maintenance"),
            )
            return
        if self._verify_runner is not None and self._verify_runner.is_running():
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("An install is already running."))
            return
        if mo2_running(force=True):
            # The game holds the Wine prefix; verification must not read it.
            QMessageBox.information(
                self,
                tr("Game Running"),
                tr("Mod Organizer / the game is currently running.\n\nClose it before running Verify Integrity."),
            )
            return
        if self.window.settings.active_profile is None:
            QMessageBox.warning(
                self,
                tr("No Profile"),
                tr("Create or activate a profile first (Profiles page)."),
            )
            return
        self._verify_counts = {"OK": 0, "CORRUPT": 0, "NOT FOUND": 0}
        self._verify_anomaly_ok = False
        self._presence = None
        self._repair_plan = None
        self._repair_records = {}
        self._quarantine_records = []
        self._repair_quarantined_count = 0
        # Cleared so a cancelled *previous* repair cannot suppress this run.
        self._repair_runner = None
        self._gamma_overlay_restore_pending = False
        self._gamma_overlay_restored = False
        self._gamma_repair_done = False
        self._gamma_skipped = False
        self._gamma_remaining_issues = None
        self._official_missing = False
        self._cache_archive_result = None
        self._anomaly_recheck_done = False
        self.window.set_install_busy(True)
        self.verify_progress.reset()
        self.verify_button.setEnabled(False)
        self.verify_progress.on_started()
        # Verify cannot pause (no runner is bound), so hide the inert Pause button.
        self.verify_progress.pause_button.hide()
        self._verify_phase_busy("Checking Anomaly files")
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
            seen = sum(self._verify_counts.values())
            if seen and seen % 20 == 0:
                # Keep the busy bar label moving during the anomaly check.
                self.verify_progress.bar.setFormat(
                    f"Checking Anomaly files ({seen})..."
                )

    def _on_anomaly_verify_finished(self, rc, output):
        # 'finished' still arrives after a cancel (the CLI exits in response to
        # SIGINT), so the cancelled run must not fall through to the next stage.
        if self._verify_runner is not None and self._verify_runner.was_cancelled:
            return
        counts = self._verify_counts
        self.verify_progress.log.append_line("")
        parsed = counts["OK"] + counts["CORRUPT"] + counts["NOT FOUND"]
        # The CLI exits 0 on some failures, so trust it only when no failure
        # markers appear and at least one status line was actually parsed.
        if not cli_ok(rc, output, ""):
            self.verify_progress.log.append_line("Anomaly check failed.")
            self._verify_anomaly_ok = False
        elif parsed == 0:
            self.verify_progress.log.append_line(
                "Anomaly check produced no results - treated as failed."
            )
            self._verify_anomaly_ok = False
        else:
            self._verify_anomaly_ok = counts["NOT FOUND"] == 0
            self.verify_progress.log.append_line(
                f"Anomaly: {counts['OK']} OK, {counts['CORRUPT']} CORRUPT, {counts['NOT FOUND']} NOT FOUND"
            )
        self._start_gamma_verify()

    def _verify_phase_busy(self, label: str) -> None:
        """Animated busy bar with a phase label (no numeric source)."""
        self.verify_progress.bar.setRange(0, 0)
        self.verify_progress.bar.setFormat(f"{label}...")
        self.verify_progress.status_message(label)

    def _set_verify_phase(
        self, start: int, end: int, fraction: float, label: str
    ) -> None:
        """Determinate phase progress on the overall verify bar."""
        value = _verify_phase_value(start, end, fraction)
        bar = self.verify_progress.bar
        bar.setRange(0, 100)
        bar.setValue(value)
        bar.setFormat(f"{label} ({value}%)")
        self.verify_progress.status_message(label)

    def _start_gamma_verify(self):
        self._scan_cancel = threading.Event()
        self.verify_progress.cancel_button.show()
        self.verify_progress.status_message("Checking GAMMA mods...")
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== GAMMA integrity check ==")
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
        report("Downloading official GAMMA mod list...")
        official = fetch_official_mod_names(profile.mod_list_url)
        official_missing = official is None or len(official) == 0
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
        report("Starting full MD5 scan of mod files...")
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
        report("Checking GAMMA download cache...")
        records = fetch_modpack_records(profile.mod_pack_maker_url)
        expected: dict[str, str] = {}
        for record in records.values():
            digest = record.md5_mod_db.lower()
            if len(digest) != 32 or any(char not in "0123456789abcdef" for char in digest):
                continue
            for archive_name in record.archive_names():
                expected.setdefault(archive_name, digest)
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
            report("Looking up download sources for broken mods...")
            plan = classify_problems(scan, records, extra_broken_folders=extra_broken)
        return (presence, scan, plan, records, official_missing, cache_result)

    def _on_gamma_verify_progress(self, text):
        self.verify_progress.status_message(text)
        for pattern, start, end in (
            (re.compile(r"Checking GAMMA mod (\d+)/(\d+)"), *_VERIFY_PHASE["presence"]),
            (re.compile(r"MD5 hashing (\d+)/(\d+)"), *_VERIFY_PHASE["md5"]),
        ):
            match = pattern.search(text)
            if match:
                done, total = int(match.group(1)), int(match.group(2))
                fraction = done / total if total > 0 else 0.0
                self._set_verify_phase(start, end, fraction, text)
                break
        self.verify_progress.log.append_line(text)

    def _on_gamma_verify_done(self, result):
        (
            presence_or_sentinel,
            scan,
            plan,
            _records,
            official_missing,
            cache_result,
        ) = result
        if presence_or_sentinel == _GAMMA_NOT_INSTALLED:
            # GAMMA absent: never report its core launcher files as errors.
            self._gamma_skipped = True
            self.verify_progress.log.append_line(
                "GAMMA is not installed - skipping GAMMA checks."
            )
            self.verify_progress.log.append_line(
                "Only Anomaly was verified in this run."
            )
            # A corrupt/missing Anomaly install must still be offered a
            # repair here - only the GAMMA-specific checks below genuinely
            # don't apply on a GAMMA-less profile.
            counts = self._verify_counts
            anomaly_needs_repair = counts["CORRUPT"] > 0 or counts["NOT FOUND"] > 0
            if anomaly_needs_repair:
                self._prompt_repair(anomaly_needs_repair, False)
                return
            self._conclude_after_repairs()
            return
        presence = presence_or_sentinel
        self._presence = presence
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
        counts = self._verify_counts
        anomaly_ok = self._verify_anomaly_ok and counts["CORRUPT"] == 0
        presence_ok = presence.problems == 0
        if scan.cancelled:
            self._finish_verify(
                ok=False,
                message="Verify cancelled during the GAMMA MD5 scan.",
                summary="Verify cancelled",
            )
            return
        if cache_result is not None and cache_result.cancelled:
            self._finish_verify(
                ok=False,
                message="Verify cancelled during the GAMMA cache scan.",
                summary="Verify cancelled",
            )
            return
        # A cached archive not matching the *current live* modpack list is
        # normal, expected staleness (the same condition Utilities' own
        # cache-cleanup preflight calls "needs a redownload," not
        # corruption) - it must never by itself flip the pass/fail verdict
        # or trigger a repair prompt. It is still shown in the log above.
        all_clean = (
            anomaly_ok
            and counts["NOT FOUND"] == 0
            and presence_ok
            and scan.problems == 0
        )
        if all_clean:
            if scan.created:
                self._finish_verify(
                    ok=True,
                    message=(
                        "MD5 baseline created. Run Verify Integrity again "
                        "to detect changes."
                    ),
                    summary=scan.summary,
                    baseline_created=True,
                )
                return
            self._finish_verify(
                ok=True,
                message=self._gamma_ok_message(repaired=0),
                summary=presence.summary,
            )
            return
        anomaly_needs_repair = counts["CORRUPT"] > 0 or counts["NOT FOUND"] > 0
        gamma_repairable = plan is not None and plan.has_repairable
        if anomaly_needs_repair or gamma_repairable:
            self._prompt_repair(anomaly_needs_repair, gamma_repairable)
            return
        self._finish_with_issues()

    def _prompt_repair(
        self, anomaly_needs_repair: bool, gamma_repairable: bool
    ) -> None:
        sections: list[str] = []
        if anomaly_needs_repair:
            counts = self._verify_counts
            sections.append(
                f"Anomaly: {counts['CORRUPT']} corrupt / "
                f"{counts['NOT FOUND']} missing file(s).\n"
                "Repairing re-runs the Anomaly installer, then restores "
                "GAMMA's own file overlay (including its replacement "
                "engine files) on top - Anomaly's installer alone would "
                "otherwise silently revert them. Your appdata (saves, "
                "user.ltx) lives outside the game folder and is never "
                "touched."
            )
        if gamma_repairable:
            names = self._repair_plan.repairable
            shown = "\n".join(f"  - {name}" for name in names[:8])
            if len(names) > 8:
                shown += f"\n  ... and {(len(names) - 8)} more"
            sections.append(
                "GAMMA: broken mod(s):\n"
                + shown
                + "\nRepairing sets each broken mod folder and cached "
                "archive aside (not deleted), then re-downloads and "
                "re-installs it (MD5-verified). If the reinstall fails or "
                "is cancelled, the set-aside copies are restored "
                "automatically - nothing is lost. Extra mods and your own "
                "added files are never touched."
            )
        plan = self._repair_plan
        if plan is not None and plan.unrepairable:
            shown = ", ".join(plan.unrepairable[:5])
            if len(plan.unrepairable) > 5:
                shown += f"... (+{(len(plan.unrepairable) - 5)} more)"
            sections.append(
                f"GAMMA: {len(plan.unrepairable)} mod(s) cannot be repaired "
                f"(no download source found) and will be left broken: {shown}"
            )
        answer = QMessageBox.question(
            self,
            tr("Verify & Repair"),
            "Issues found:\n\n" + "\n\n".join(sections) + "\n\nRepair now?",
            (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._finish_with_issues()
            return
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
        self._advance_repair_pipeline()

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
        self._conclude_after_repairs()

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
        self.verify_progress.status_message("Re-installing Anomaly...")
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Repairing Anomaly ==")
        runner = CommandRunner(cli_command(["anomaly", "install"]), parent=self)
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
            return
        repair_ok = cli_ok(rc, output, "")
        self.verify_progress.log.append_line(
            "Anomaly repair finished successfully."
            if repair_ok
            else f"Anomaly repair failed (exit code {rc})."
        )
        # One bounded re-check so the verdict reflects reality.
        self._verify_counts = {"OK": 0, "CORRUPT": 0, "NOT FOUND": 0}
        self._anomaly_recheck_done = True
        self._verify_phase_busy("Re-checking Anomaly files")
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
            return
        counts = self._verify_counts
        parsed = sum(counts.values())
        ok = (
            cli_ok(rc, output, "")
            and parsed > 0
            and counts["CORRUPT"] == 0
            and counts["NOT FOUND"] == 0
        )
        self._verify_anomaly_ok = ok
        self.verify_progress.log.append_line(
            f"Anomaly re-check: {counts['OK']} OK, {counts['CORRUPT']} CORRUPT, "
            f"{counts['NOT FOUND']} NOT FOUND"
        )
        # Route through the pipeline, not straight to the verdict: a pending
        # GAMMA repair (both Anomaly and GAMMA needed fixing) must still run
        # here, or it's silently skipped and reported as "no issues found".
        self._advance_repair_pipeline()

    def _conclude_after_repairs(self) -> None:
        counts = self._verify_counts
        counts_ok = counts["CORRUPT"] == 0 and counts["NOT FOUND"] == 0
        anomaly_ok = bool(self._verify_anomaly_ok) and counts_ok
        repaired_gamma = self._repair_quarantined_count if self._gamma_repair_done else 0
        remaining = self._gamma_remaining_issues
        plan = self._repair_plan
        unrepairable_count = len(plan.unrepairable) if plan is not None else 0
        lines: list[str] = [f"Anomaly: {'OK' if anomaly_ok else 'ISSUES REMAIN'}"]
        if self._gamma_skipped:
            lines.append("GAMMA: not installed - skipped")
        elif self._gamma_repair_done:
            if self._gamma_overlay_restored:
                lines.append(
                    "GAMMA: file overlay restored over the repaired "
                    "Anomaly files; remaining problems: "
                    f"{remaining if remaining is not None else 'unknown'}"
                )
            else:
                lines.append(
                    f"GAMMA: repaired ({repaired_gamma} mod(s)); remaining "
                    f"problems: {remaining if remaining is not None else 'unknown'}"
                )
            lines.append("Your saves, user.ltx and MCM settings were preserved.")
        elif unrepairable_count:
            shown = ", ".join(plan.unrepairable[:5])
            if unrepairable_count > 5:
                shown += f"... (+{(unrepairable_count - 5)} more)"
            lines.append(
                f"GAMMA: {unrepairable_count} mod(s) left broken - "
                f"no download source found: {shown}"
            )
        else:
            lines.append("GAMMA: verified - no issues found")
        if self._official_missing:
            lines.append("Note: official mod list was unavailable.")
        if self._cache_archive_result is not None:
            lines.append(
                "Cache: "
                f"{len(self._cache_archive_result.verified)} reusable, "
                f"{self._cache_archive_result.problems} needing attention"
            )
        # Cache staleness vs. the current live list is never itself a
        # failure condition - see the matching comment in _on_gamma_verify_done.
        ok_final = anomaly_ok and (remaining in (None, 0)) and unrepairable_count == 0
        message = "\n".join(lines)
        summary = "Verify & Repair complete" if ok_final else "Issues remain"
        dialog_lines = "\n".join(f"• {line}" for line in lines)
        QMessageBox.information(self, tr("Verify & Repair"), tr("Results:\n\n{dialog_lines}", dialog_lines=dialog_lines))
        self._finish_verify(ok=ok_final, message=message, summary=summary)

    def _gamma_ok_message(self, repaired):
        if repaired:
            return f"Confirmed: GAMMA repaired ({repaired} mod(s)) and verified successfully."
        return "Confirmed: Anomaly and GAMMA verified successfully."

    def _finish_with_issues(self):
        plan = self._repair_plan
        if plan is not None and plan.unrepairable:
            shown = ", ".join(plan.unrepairable[:5])
            if len(plan.unrepairable) > 5:
                shown += f"... (+{(len(plan.unrepairable) - 5)} more)"
            self.verify_progress.log.append_line(
                f"Not repairable ({len(plan.unrepairable)}): no download source found - {shown}"
            )
        if plan is not None and plan.added_only:
            self.verify_progress.log.append_line(
                f"Left untouched ({len(plan.added_only)} mod(s) with added files - your own edits are kept)."
            )
        if self._cache_archive_result is not None and self._cache_archive_result.problems:
            self.verify_progress.log.append_line(
                "Cache is not fully reusable; affected archives will be downloaded "
                "again during reinstall."
            )
        message = "Verify finished with issues - Anomaly and/or GAMMA are not fully verified (see details above)."
        self._finish_verify(ok=False, message=message, summary="GAMMA has issues")

    def _start_gamma_repair_deletion(self) -> None:
        self.verify_progress.cancel_button.show()
        self.verify_progress.status_message("Setting broken mods aside...")
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
                message="Repair cancelled." + restore_from_quarantine_failures,
                summary="Repair cancelled",
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
        return (
            "\n\nWARNING: could not fully restore everything that was set "
            f"aside ({len(failures)} item(s)) - see the log above."
        )

    def _start_repair_install(self):
        # Shown unconditionally rather than relying on the preceding deletion
        # stage having left it visible - this is the pipeline's real network-
        # bound download/reinstall phase, so Cancel must not silently go
        # missing if a future reordering ever reaches this stage directly.
        self.verify_progress.cancel_button.show()
        self.verify_progress.status_message(
            "Re-downloading and re-installing broken mods..."
        )
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line("== Running installer (repair) ==")
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

    def _on_repair_install_finished(self, rc, output):
        # A cancelled repair must not fall through to the post-scan: that scan
        # re-baselines the MD5 manifest and would record the broken state as
        # the new reference.
        if self._repair_runner is not None and self._repair_runner.was_cancelled:
            return
        self.verify_progress.log.append_line("")
        if not cli_ok(rc, output, ""):
            self.verify_progress.log.append_line("Repair install failed.")
            self.verify_progress.log.append_line(
                "Restoring mods that were set aside..."
            )
            restore_note = self._restore_all_quarantined()
            self._finish_verify(
                ok=False,
                message=(
                    "Repair install failed - GAMMA is not fully repaired "
                    "(see details above). Mods that were set aside for "
                    "repair have been restored." + restore_note
                ),
                summary="Repair failed",
            )
            return
        # Reinstall itself succeeded: the quarantined originals are no
        # longer needed - keep the newly-installed files, not the old
        # broken ones.
        purge_quarantine(self.window.settings.active_profile.gamma)
        self._quarantine_records = []
        self._start_post_scan()

    def _on_repair_install_cancelled(self):
        self.verify_progress.log.append_line("Repair install cancelled")
        self.verify_progress.log.append_line("Restoring mods that were set aside...")
        restore_note = self._restore_all_quarantined()
        self._finish_verify(
            ok=False,
            message="Repair cancelled. Mods that were set aside for repair "
            "have been restored." + restore_note,
            summary="Repair cancelled",
        )

    def _start_post_scan(self):
        self.verify_progress.status_message("Re-checking GAMMA mods...")
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
        )
        if post.cancelled:
            return (post, None)
        report("Re-checking GAMMA mods are present...")
        presence = verify_gamma(
            profile.gamma,
            profile.mo2_profile,
            on_progress=(
                lambda done, total, name: report(
                    f"Checking GAMMA mod {done}/{total}: {name}"
                )
            ),
        )
        if post.problems == 0 and presence.problems == 0:
            # Only establish a new baseline after both the content and presence
            # checks have passed. A failed repair must never bless corruption.
            scan_mods_md5(profile.gamma, cancel=self._scan_cancel, rebaseline=True)
        return (post, presence)

    def _on_post_scan_done(self, result):
        post, presence = result
        for line in post.lines():
            self.verify_progress.log.append_line(line)
        if presence is not None:
            for line in presence.lines():
                self.verify_progress.log.append_line(line)
        if post.cancelled:
            self._finish_verify(
                ok=False,
                message="Verify cancelled during the post-repair scan.",
                summary="Verify cancelled",
            )
            return
        repaired = self._repair_quarantined_count
        remaining = post.problems + (presence.problems if presence is not None else 0)
        self._gamma_repair_done = True
        self._gamma_remaining_issues = remaining
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
            message="Verify failed - Anomaly and/or GAMMA are not fully verified.",
            summary="GAMMA check failed",
        )

    def _finish_verify(self, ok, message, summary, baseline_created=False):
        self.verify_button.setEnabled(True)
        self.verify_progress.cancel_button.hide()
        # Drop references to finished runners/tasks so a later cancel cannot
        # target an already-dead process or thread.
        self._verify_runner = None
        self._repair_runner = None
        self._verify_task = None
        self._scan_cancel = None
        self.verify_progress.log.append_line("")
        self.verify_progress.log.append_line(message)
        if baseline_created:
            self.verify_progress.set_success_state("Baseline created")
        elif ok:
            self.verify_progress.set_success_state("Verified successfully")
        else:
            self.verify_progress.on_finished(1, "")
        self.verify_progress.status_message(message)
        self.window.statusBar().showMessage(summary, 8000)
        self.window.set_install_busy(False)

    def _cancel_verify(self):
        if self._verify_runner is not None:
            self._verify_runner.cancel()
        if self._scan_cancel is not None:
            self._scan_cancel.set()
            return

    def _on_verify_cancelled(self, stage: str = "Anomaly check"):
        self.verify_progress.log.append_line(f"{stage} cancelled")
        self.verify_button.setEnabled(True)
        self.verify_progress.on_cancelled()
        self.verify_progress.status_message("Cancelled")
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
        paused = {verb: True for verb in WINETRICKS_VERBS}
        paused["wine"] = True
        paused["protontricks"] = True
        paused["umu"] = True
        total = len(paused)
        self._wt_installed = True
        self.wt_status.set_state(True)
        self.wt_progress.status_label.setText(
            f"{total}/{total} dependencies installed (paused - game running)"
        )
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
        if mo2_running():
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
        if mo2_running():
            # The game started while the check was in flight; the result is stale.
            self._paused_status()
            return
        installed = sum(1 for ok in status.values() if ok)
        total = len(status)
        # total == 0 means the check found no verbs at all (lookup failure),
        # not "everything installed".
        all_done = total > 0 and installed == total
        self._wt_installed = all_done
        self.wt_status.set_state(all_done)
        self.wt_progress.status_label.setText(f"{installed}/{total} dependencies installed")
        self.wt_status.set_status_tooltip(winetricks_tooltip(status))
        self._update_button_states()

    def _on_winetricks_status_error(self, message):
        # Must clear the in-flight flag, or the status never refreshes again.
        self._wt_checking = False
        if getattr(self.window, "install_operation", None) == "dependencies":
            return
        if mo2_running():
            self._paused_status()
            return
        self._wt_installed = None
        self.wt_status.set_state(None, "status unavailable", pending_text="Unknown")
        self.wt_status.set_status_tooltip(f"Could not query dependencies: {message}")
        self._update_button_states()

    def _start_winetricks(self):
        self._winetricks_status_enabled = True
        if self._wt_runner is not None and self._wt_runner.is_running():
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("An install is already running."))
            return
        if mo2_running(force=True):
            # The game holds the Wine prefix; winetricks must not touch it.
            return
        errors = check_all_dependencies()
        if errors:
            QMessageBox.warning(
                self,
                tr("Missing Dependencies"),
                "The following are required but not found:\n\n"
                + "\n\n".join(errors)
                + "\n\nInstall them and try again.",
            )
            return
        prefix = self._wt_prefix()
        need_umu = not umu_binary()
        need_protontricks = not protontricks_binary()
        message = f"This installs the runtime libraries needed by Mod Organizer and the game into:\n\n{prefix}\n\nVerbs: {', '.join(WINETRICKS_VERBS)}\n(~150 MB download on first run)."
        if need_umu:
            message += (
                "\n\numu-run (Proton launcher) is missing and will be installed first."
            )
        if need_protontricks:
            message += "\n\nprotontricks is missing and will be installed first."
        answer = QMessageBox.question(
            self,
            tr("Confirm Install Dependencies"),
            message,
            (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # install_busy/mo2 state may have changed while the dialog was open;
        # winetricks must never touch a prefix the game is holding.
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("An install is already running."))
            return
        if mo2_running(force=True):
            QMessageBox.information(
                self,
                tr("Game Running"),
                tr("Mod Organizer / the game started while this dialog was open.\n\nClose it before installing dependencies."),
            )
            return
        if need_umu:
            self._wt_stage = "umu"
        elif need_protontricks:
            self._wt_stage = "tools"
        else:
            self._wt_stage = "verbs"
        self._wt_completed_verbs.clear()
        self._wt_last_pct = -1
        self.window.set_install_busy(True, "dependencies")
        self.wt_status.set_installing("Installing dependencies...")
        self.wt_progress.reset()
        self._start_winetricks_stage()

    def _wt_status_failed(self, detail: str) -> None:
        """Reset the dependency status row after a stage aborted mid-install."""
        self._wt_installed = False
        first_line = detail.splitlines()[0] if detail else "Install failed"
        self.wt_status.set_state(False, first_line)
        self.wt_status.set_status_tooltip(detail)

    def _start_winetricks_stage(self):
        start, _end = _WT_STAGE_RANGES.get(self._wt_stage, (0, 100))
        self.wt_progress.on_started()
        self.wt_progress.bar.setRange(0, 100)
        self.wt_progress.bar.setValue(start)
        self._wt_last_pct = start
        self.wt_progress.bar.setFormat("%p%")
        if self._wt_stage == "umu":
            command = umu_install_command()
            if not command:
                msg = "umu-run could not be installed (curl is not available)."
                self.wt_progress.on_finished(1, msg)
                self._wt_runner = None
                self._wt_status_failed(msg)
                self.window.set_install_busy(False)
                self._refresh_winetricks_status()
                return
            self.wt_progress.status_message("Installing umu-run...")
            self.wt_progress.log.append_line(
                "== Installing umu-run (Proton launcher) =="
            )
        elif self._wt_stage == "tools":
            command = protontricks_install_command()
            if not command:
                from ..dependencies import _externally_managed, _install_command

                msg = "protontricks could not be installed."
                if _externally_managed():
                    cmd = _install_command("pipx")
                    msg += (
                        "\n\nThis system marks Python as externally managed "
                        "(PEP 668), so pip cannot install packages directly."
                        f"\n\nInstall pipx first:\n  {cmd}\n\n"
                        "Then try again — protontricks will be installed automatically."
                    )
                else:
                    msg += "\n\nInstall pipx or pip, then try again."
                self.wt_progress.on_finished(1, msg)
                self._wt_runner = None
                self._wt_status_failed(msg)
                self.window.set_install_busy(False)
                self._refresh_winetricks_status()
                return
            self.wt_progress.status_message("Installing protontricks...")
            self.wt_progress.log.append_line("== Installing protontricks ==")
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
                self.window.set_install_busy(False)
                self._refresh_winetricks_status()
                return
            self.wt_progress.status_message("Installing runtimes...")
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
        pct = _winetricks_progress(clean, self._wt_stage, self._wt_completed_verbs)
        overall = _dependencies_progress(self._wt_stage, pct)
        if overall is not None and overall >= self._wt_last_pct:
            self.wt_progress.bar.setRange(0, 100)
            self.wt_progress.bar.setValue(overall)
            self.wt_progress.bar.setFormat("%p%")
            self._wt_last_pct = overall

    def _on_winetricks_finished(self, rc, output):
        # 'finished' still arrives after a cancel; don't overwrite the
        # 'Cancelled' status with a spurious failure.
        if self._wt_runner is not None and self._wt_runner.was_cancelled:
            self._wt_runner = None
            self.window.set_install_busy(False)
            self._refresh_winetricks_status()
            return
        if rc == 0 and self._wt_stage == "umu":
            # UMU installed — chain to protontricks or verbs.
            self._wt_stage = "tools" if not protontricks_binary() else "verbs"
            self._wt_runner = None
            self._start_winetricks_stage()
            return
        if self._wt_stage == "tools" and rc == 0:
            self._wt_stage = "verbs"
            self._wt_runner = None
            # Do not call on_finished — the verbs stage continues directly.
            self._start_winetricks_stage()
            return
        self.wt_progress.on_finished(rc, output)
        if rc == 0:
            self.wt_progress.status_message("Complete - runtimes installed")
        else:
            self.wt_progress.status_message(f"Failed (exit code {rc})")
        self._wt_runner = None
        self.window.set_install_busy(False)
        self._refresh_winetricks_status()

    def _on_winetricks_cancelled(self):
        self.wt_progress.log.append_line("Winetricks cancelled")
        self.wt_progress.on_cancelled()
        self.wt_progress.status_message("Cancelled")

    def _cancel_winetricks(self):
        if self._wt_runner is None or not self._wt_runner.is_running():
            return
        answer = QMessageBox.question(
            self,
            tr("Cancel Install Dependencies"),
            tr("Dependencies are being downloaded or installed.\n\nCancel the operation? The prefix may be left partially configured."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # The install may have finished while the dialog was open, clearing
        # self._wt_runner - re-check before touching it, or this crashes.
        if self._wt_runner is None or not self._wt_runner.is_running():
            return
        self.wt_progress.cancel_button.setEnabled(False)
        self.wt_progress.cancel_button.setText(tr("Cancelling..."))
        self.wt_progress.status_message("Cancelling Winetricks...")
        self._wt_runner.cancel()
