"""Utilities page: integrity checks, cache pruning, maintenance tasks."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Mapping
from html import escape
from math import isfinite
from numbers import Real
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
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
from ..assistant_launcher import (
    AssistantLaunchError,
    active_assistant_process,
    launch_assistant,
    reap_assistant_processes,
)
from ..atomic import write_text
from ..cli_runner import cli_command
from ..integrity import format_size, verify_cache_archives
from ..log_dump import create_log_dump
from ..parsers import (
    parse_prune_archive,
    strip_ansi,
)
from ..repair import fetch_modpack_records
from .common import (
    STATUS_RED,
    CommandRunner,
    OutputPane,
    ProgressArea,
    StreamTask,
    anomaly_installed,
    assistant_token,
    dir_size,
    gamma_installed,
    info_label,
    make_card,
    mo2_running,
    section_label,
    tr,
)

#: System directories that must never be handed to rmtree, whatever a
#: profile says - a path is rejected if it equals one of these OR is nested
#: under one of them, not just on an exact string match, so e.g.
#: /etc/NetworkManager and /var/lib/anything are refused too, not just
#: /etc and /var themselves. "/" itself is handled separately (every path
#: is "under" the filesystem root, so it can't be part of this list without
#: rejecting everything). "/root" is included here (nesting always
#: rejected) because it is only ever meaningful as the root *user's* home,
#: not a normal install location.
#:
#: Deliberately NOT in this list, because a real install/cache folder can
#: legitimately live nested underneath them: "/home" (its own
#: exact-match-only check lives further down, alongside the logic that
#: rejects other users' home trees at any depth), "/tmp" (used by the app's
#: own temp/staging paths and test fixtures), and "/mnt"/"/media" (the
#: conventional place to install a large modpack to an external/secondary
#: drive).
_PROTECTED_ROOTS = tuple(
    Path(p)
    for p in (
        "/root",
        "/usr",
        "/etc",
        "/var",
        "/opt",
        "/boot",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/srv",
        "/run",
        "/proc",
        "/sys",
        "/dev",
        "/snap",
    )
)


def _resolved_wipe_target(path: str) -> Path | None:
    """Resolve ``path`` to the directory that would actually be deleted.

    Returns ``None`` when the path is blank or not a real directory.
    Resolution and deletion must agree on one path, so callers use this
    result for both.
    """
    if not path or not path.strip():
        return None
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_dir() else None


def _resolved_path(path: str) -> Path | None:
    """Resolve a configured path, including paths that do not exist yet."""
    if not path or not path.strip():
        return None
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _safe_wipe_path(raw: str, resolved: Path) -> bool:
    """Refuse paths too broad to be a GAMMA install folder.

    A real install folder is nested at least two levels below the filesystem
    root, is never a system directory, any user's home, the GUI's own
    location, the current working directory, or a symlink (deleting through
    one would take out an unrelated tree).
    """
    if resolved.parent == resolved:
        return False
    for root in _PROTECTED_ROOTS:
        if resolved == root or root in resolved.parents:
            return False
    home = Path.home()
    if resolved == home or resolved == home.parent:
        return False
    # Anything under our home's parent (/home) that is not inside our own
    # home tree is another user's home, at any depth (/home/<other>/...),
    # not just a direct child of /home.
    if home.parent in resolved.parents or resolved == home.parent:
        try:
            top_level = resolved.relative_to(home.parent).parts[0]
        except (ValueError, IndexError):
            top_level = None
        if top_level != home.name:
            return False
    try:
        if resolved == Path(__file__).resolve().parents[2]:
            return False
    except IndexError:
        pass
    if resolved == Path.cwd().resolve():
        return False
    # Deleting through a symlink would take out an unrelated tree; resolve()
    # already followed it, so inspect the pre-resolution path itself.
    if Path(raw).expanduser().is_symlink():
        return False
    return len(resolved.parts) >= 3


def _validate_wipe_paths(paths: list[tuple[str, str]]) -> list[tuple[str, Path]]:
    """Validate every configured target before any target is deleted."""
    resolved_paths: list[tuple[str, Path]] = []
    for label, path in paths:
        resolved = _resolved_path(path)
        if resolved is None:
            continue
        if not _safe_wipe_path(path, resolved):
            raise ValueError(f"Refusing to wipe unsafe path: {resolved}")
        resolved_paths.append((label, resolved))

    for index, (label, path) in enumerate(resolved_paths):
        for other_label, other in resolved_paths[index + 1 :]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError(f"Refusing to wipe overlapping paths: {label} and {other_label}")
    return resolved_paths


def _validate_move_destination(
    destination: str | Path,
    sources: list[tuple[str, str]],
) -> Path:
    """Validate a move destination and its relationship to every source."""
    raw_destination = Path(destination).expanduser()
    resolved_destination = _resolved_path(str(destination))
    if resolved_destination is None or not resolved_destination.is_dir():
        raise ValueError(f"Invalid move destination: {destination}")
    if not _safe_wipe_path(str(raw_destination), resolved_destination):
        raise ValueError(f"Refusing to move to unsafe destination: {resolved_destination}")

    resolved_sources = [
        (label, resolved)
        for label, path in sources
        if (resolved := _resolved_path(path)) is not None
    ]
    for label, source in resolved_sources:
        if (
            resolved_destination == source
            or resolved_destination in source.parents
            or source in resolved_destination.parents
        ):
            raise ValueError(
                f"Destination overlaps the {label} source folder: {resolved_destination}"
            )
    return resolved_destination


def _wipe_folders(paths: list[tuple[str, str]], report) -> list[str]:
    """Delete the given ``(label, path)`` install folders completely.

    Raises ``ValueError`` if any present path fails :func:`_safe_wipe_path`.
    Returns the list of paths that were actually deleted.
    """
    _validate_wipe_paths(paths)
    wiped: list[str] = []
    for label, path in paths:
        resolved = _resolved_wipe_target(path)
        if resolved is None:
            report(f"{label}: folder not present, skipping ({path})")
            continue
        if not _safe_wipe_path(path, resolved):
            raise ValueError(f"Refusing to wipe unsafe path: {resolved}")
        if not resolved.is_dir():
            report(f"{label}: folder not present, skipping ({path})")
            continue
        report(f"Deleting {label} folder: {resolved} ...")
        shutil.rmtree(resolved, ignore_errors=True)
        if resolved.exists():
            raise ValueError(f"{label} folder could not be fully deleted: {resolved}")
        wiped.append(str(resolved))
        report(f"{label} folder deleted.")
    return wiped


def _copy_dir_tree(src: Path, dst: Path, report, cancel_event=None) -> None:
    """Copy *src* into *dst*, streaming progress via *report*."""
    if not src.is_dir():
        raise ValueError(f"Source folder does not exist: {src}")
    if src.is_symlink() or dst.is_symlink():
        raise ValueError("Move source and destination cannot be symlinks")
    dst.mkdir(parents=True, exist_ok=True)
    # Collect entries once: rglob-then-walk would stat a 100k-file tree twice.
    entries: list[Path] = []
    for root, dirs, names in os.walk(src):
        dirs.sort()
        names.sort()
        for name in (*dirs, *names):
            entries.append(Path(root) / name)
    total = len(entries)
    for idx, item in enumerate(entries, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Move cancelled")
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_symlink():
            raise ValueError(f"Move source contains a symlink: {item}")
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
        if idx % 200 == 0 or idx == total:
            report(f"  copied {idx}/{total} entries ...")


def _file_count_and_verify(source: Path, destination: Path) -> tuple[int, bool]:
    """Count source files while checking each exists, with matching size."""
    source_count = 0
    valid = True
    for root, _dirs, names in os.walk(source):
        for name in names:
            source_count += 1
            relative = (Path(root) / name).relative_to(source)
            target = destination / relative
            try:
                # A truncated or zero-byte copy must fail verification, not
                # pass on a count match alone.
                if target.stat().st_size != (Path(root) / name).stat().st_size:
                    valid = False
            except OSError:
                valid = False
    destination_count = sum(len(names) for _root, _dirs, names in os.walk(destination))
    return source_count, valid and source_count == destination_count


def _move_folders(
    sources: list[tuple[str, str]],
    dest_parent: Path,
    report,
    cancel_event=None,
) -> list[tuple[str, str]]:
    """Copy then delete the given ``(label, path)`` folders into *dest_parent*.

    Returns a list of ``(label, new_path)`` for folders that were moved.
    Raises ``ValueError`` on failure; originals are left intact in that case.
    """
    resolved_sources: list[tuple[str, Path]] = []
    resolved_input_sources: list[tuple[str, Path]] = []
    for label, path in sources:
        if not path or not path.strip():
            report(f"{label}: path is empty, skipping")
            continue
        src = _resolved_path(path)
        if src is None:
            raise ValueError(f"Unable to resolve {label} source path: {path}")
        resolved_input_sources.append((label, src))
        if not src.is_dir():
            report(f"{label}: folder not found, skipping ({src})")
            continue
        if not _safe_wipe_path(path, src):
            raise ValueError(f"Refusing to move unsafe path: {src}")
        if dest_parent == src or dest_parent in src.parents:
            raise ValueError(
                f"Destination is inside the {label} source folder: {dest_parent}"
            )
        resolved_sources.append((label, src))

    for index, (label, src) in enumerate(resolved_input_sources):
        for other_label, other in resolved_input_sources[index + 1 :]:
            if src == other or src in other.parents or other in src.parents:
                raise ValueError(f"Source folders overlap: {label} and {other_label}")

    dest_parent = _validate_move_destination(dest_parent, sources)

    # Free-space preflight: fail before copying instead of mid-copy, where a
    # full disk forces an expensive and risky rollback. The destination needs
    # room for the copy; each source's own filesystem separately needs room
    # for the safety backup taken there (as a sibling folder) before the
    # original is removed - that backup does not land on the destination.
    try:
        dest_needed = sum(
            dir_size(src)
            for _label, src in resolved_sources
            if src.is_dir()
        )
        if dest_needed > 0:
            free = shutil.disk_usage(dest_parent).free
            if free < dest_needed:
                raise ValueError(
                    "Not enough free space at the destination.\n\n"
                    f"Required (copy): {format_size(dest_needed)}\n"
                    f"Available: {format_size(free)}\n"
                    f"Destination: {dest_parent}"
                )

        source_needed: dict[int, int] = {}
        source_sample: dict[int, Path] = {}
        for _label, src in resolved_sources:
            if not src.is_dir():
                continue
            device = src.stat().st_dev
            source_needed[device] = source_needed.get(device, 0) + dir_size(src)
            source_sample.setdefault(device, src)
        for device, needed in source_needed.items():
            free = shutil.disk_usage(source_sample[device]).free
            if free < needed:
                raise ValueError(
                    "Not enough free space on the source disk for the safety "
                    "backup taken there before the original is removed.\n\n"
                    f"Required: {format_size(needed)}\n"
                    f"Available: {format_size(free)}\n"
                    f"Source: {source_sample[device]}"
                )
    except OSError:
        # A stat failure must not block the move; the copy itself will error.
        pass

    destinations: list[tuple[str, Path, Path]] = []
    for label, src in resolved_sources:
        dst = dest_parent / src.name
        if dst == src or dst in src.parents or src in dst.parents:
            raise ValueError(
                f"Destination overlaps the {label} source folder: {dest_parent}"
            )
        if dst.exists():
            raise ValueError(
                f"Destination already exists: {dst}\n"
                "Choose a folder that does not already contain these subfolders."
            )
        destinations.append((label, src, dst))

    for index, (label, _src, dst) in enumerate(destinations):
        for other_label, _other_src, other_dst in destinations[index + 1 :]:
            if dst == other_dst or dst in other_dst.parents or other_dst in dst.parents:
                raise ValueError(f"Destination paths overlap: {label} and {other_label}")

    copied: list[Path] = []
    backups: list[tuple[Path, Path]] = []
    backup_paths: list[Path] = []
    try:
        for label, src, dst in destinations:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Move cancelled")
            report(f"Copying {label}: {src} -> {dst}")
            # Register before copying so an interrupted/failed copy is removed.
            copied.append(dst)
            _copy_dir_tree(src, dst, report, cancel_event)
            report("Copy complete. Verifying ...")
            _source_count, verified = _file_count_and_verify(src, dst)
            if not verified:
                raise ValueError(f"Copy verification failed for {label}: {dst}")

        for label, src, _dst in destinations:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Move cancelled")
            backup = src.with_name(f".{src.name}.move-backup-{uuid.uuid4().hex}")
            # Registered before copying so an interrupted/failed backup copy
            # is cleaned up below instead of left orphaned on disk.
            backup_paths.append(backup)
            shutil.copytree(src, backup, symlinks=True)
            backups.append((src, backup))
            report(f"Original {label} staged for removal.")

        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Move cancelled")
        for (label, src, _dst), (_original, backup) in zip(destinations, backups):
            report(f"Removing original {label}: {src}")
            shutil.rmtree(src)

        # Backups remain available until every original has been removed.
        for _original, backup in backups:
            shutil.rmtree(backup, ignore_errors=True)

        return [(label, str(dst)) for label, _src, dst in destinations]
    except Exception:
        for original, backup in reversed(backups):
            try:
                if backup.exists():
                    # copytree-with-dirs_exist_ok restores over a partially
                    # deleted original; rmtree-first could leave nothing to
                    # copy onto if the rmtree itself half-failed.
                    shutil.copytree(backup, original, symlinks=True, dirs_exist_ok=True)
                    shutil.rmtree(backup, ignore_errors=True)
            except OSError:
                report(
                    f"Warning: could not restore {original} from {backup}; "
                    "backup file preserved for manual recovery"
                )
        # A backup whose copytree itself failed or was interrupted never made
        # it into `backups` above (its original is untouched, so there is
        # nothing to restore) - just remove the partial copy so it does not
        # linger as an orphaned folder next to the original.
        completed_backup_paths = {backup for _original, backup in backups}
        for backup in backup_paths:
            if backup not in completed_backup_paths and backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
        for dst in reversed(copied):
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
        raise


def _save_moved_profile(settings, profile_name: str, moved: list[tuple[str, str]]):
    """Save moved paths on the profile that started the operation."""
    profile = next(
        (
            candidate
            for candidate in settings.profiles
            if candidate.profile_name == profile_name
        ),
        None,
    )
    if profile is None:
        raise ValueError(f"Profile '{profile_name}' no longer exists")
    # Explicit label -> attribute mapping: a silent .lower() coupling breaks
    # the moment a label is renamed; this fails loudly on unknown labels.
    _MOVED_ATTRS = {"Anomaly": "anomaly", "GAMMA": "gamma", "Cache": "cache"}
    for label, new_path in moved:
        attribute = _MOVED_ATTRS.get(label)
        if attribute is None:
            raise ValueError(f"Unknown moved path type: {label}")
        setattr(profile, attribute, new_path)
    settings.save()
    return profile


def _rewrite_mo2_ini_paths(
    gamma_dir: str | Path,
    replacements: list[tuple[str, str]],
) -> Path:
    """Rewrite old Anomaly/GAMMA paths in the moved MO2 configuration."""
    ini = Path(gamma_dir) / "ModOrganizer.ini"
    if not ini.is_file():
        raise FileNotFoundError(f"ModOrganizer.ini not found: {ini}")
    text = ini.read_text(encoding="utf-8", errors="replace")
    pairs: list[tuple[str, str]] = []
    for old, new in replacements:
        old_path = str(Path(old).expanduser())
        new_path = str(Path(new).expanduser())
        pairs.extend(
            (old_path.replace("/", sep), new_path.replace("/", sep))
            for sep in ("/", "\\", "\\\\")
        )
    # Longest old string first: if one source path is a literal substring
    # prefix of another (e.g. ".../anomaly" and ".../anomaly-gamma", both
    # plausible sibling folder names), replacing the shorter one first would
    # mangle the longer path's occurrences before its own replacement runs,
    # silently corrupting it into a hybrid old/new path.
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    updated = text
    for old_variant, new_variant in pairs:
        updated = updated.replace(old_variant, new_variant)
    if updated == text:
        raise ValueError(f"No configured paths were found in {ini}")
    if any(old_variant in updated for old_variant, _new_variant in pairs):
        raise ValueError(f"Old paths remain in {ini}")
    backup = ini.with_name(ini.name + ".gammagui.bak")
    if not backup.exists():
        shutil.copy2(ini, backup)
    write_text(ini, updated)
    return ini


class UtilitiesPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._runner: CommandRunner | None = None
        self._prune_mb = 0
        self._wipe_task: StreamTask | None = None
        self._cache_task: StreamTask | None = None
        self._cache_cancel_event = None
        self._wipe_targets: tuple[str, str] = ("", "")
        self._reset_wipe_paths: list[tuple[str, str]] = []
        self._reset_folders = ""
        self._reset_includes_anomaly = True
        self._wipe_title = "Fresh Reset"
        self._reset_preserve_user = False
        self._reset_preserve_mcm = False
        self._full_uninstall_targets: tuple[str, str, str] = ("", "", "")
        self._move_task: StreamTask | None = None
        self._move_profile_name: str | None = None
        self._move_sources: list[tuple[str, str]] = []
        self._log_dump_task: StreamTask | None = None
        self._assistant_process = None
        self._assistant_timer = QTimer(self)
        self._assistant_timer.setInterval(100)
        self._assistant_timer.timeout.connect(self._check_assistant)
        self.buttons: list[QPushButton] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 20)
        outer.setSpacing(12)

        title = section_label(tr("UTILITIES"), level=1)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(title)
        outer.addWidget(
            info_label(
                tr("Maintenance tools for Anomaly and GAMMA: manage the download and shader caches, fix GOG paths, create a log dump, and safely reset or remove folders.")
            )
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 8, 0)
        root.setSpacing(14)
        scroll.setWidget(content)

        root.addWidget(self._tools_card())
        root.addWidget(self._destructive_card())
        root.addWidget(self._move_card())
        root.addStretch(1)

        self.refresh()

    def _tools_card(self) -> QWidget:
        card, layout = make_card()
        header = QHBoxLayout()
        header.addWidget(section_label(tr("Tools"), level=2))
        header.addStretch(1)
        self.assistant_button = QPushButton(tr("Open ASSISTANT"))
        self.assistant_button.setObjectName("primary")
        self.assistant_button.setMinimumSize(150, 34)
        self.assistant_button.setToolTip(
            tr("Open the COMMANDER ASSISTANT log analyzer.")
        )
        self.assistant_button.clicked.connect(self._open_assistant)
        header.addWidget(self.assistant_button)
        layout.addLayout(header)
        layout.addWidget(
            info_label(
                tr("Tools use the active profile's folders. Technical output is available in the console below.")
            )
        )
        rows = QVBoxLayout()
        rows.setSpacing(8)
        tools = [
            (
                tr("Preview cache cleanup"),
                tr(
                    "List out-of-date addon archives in the cache with the total "
                    "size that can be reclaimed."
                ),
                self._prune_check,
            ),
            (
                tr("Clean the download cache"),
                tr("Permanently delete out-of-date addon archives from the cache."),
                self._prune_apply,
            ),
            (
                tr("Clear shader cache"),
                tr("Delete the shader cache for the active Anomaly profile."),
                self._purge_shader_cache,
            ),
            (
                tr("Remove ReShade"),
                tr("Remove all ReShade-related files from the Anomaly bin directory."),
                self._delete_reshade,
            ),
            (
                tr("Fix GOG installation"),
                tr("Fix the ModOrganizer.ini paths for a GOG-provided install."),
                self._gog_fix,
            ),
            (
                tr("Create Log Dump"),
                tr(
                    "Collect COMMANDER, Anomaly, GAMMA/MO2 and Wine-prefix logs "
                    "plus crash dumps into one zip archive. Open the archive in "
                    "{assistant} to check all errors and warnings.",
                    assistant=assistant_token(),
                ),
                self._start_log_dump,
            ),
        ]
        for title, description, slot in tools:
            rows.addWidget(self._tool_row(title, description, slot))
        layout.addLayout(rows)
        return card

    def _open_assistant(self) -> None:
        """Open the bundled or installed ASSISTANT application."""
        active_process = active_assistant_process()
        if active_process is not None:
            self._track_assistant(active_process)
            return
        try:
            process = launch_assistant()
        except AssistantLaunchError as exc:
            title = (
                tr("ASSISTANT Already Open")
                if "already running" in str(exc)
                else tr("ASSISTANT Unavailable")
            )
            QMessageBox.information(self, title, str(exc))
            return
        self._track_assistant(process)

    def _track_assistant(self, process) -> None:
        """Watch ASSISTANT without blocking the Qt event loop."""
        self._assistant_process = process
        self._set_assistant_button(False)
        self._assistant_timer.start()

    def _set_assistant_button(self, enabled: bool) -> None:
        self.assistant_button.setEnabled(enabled)

    def _check_assistant(self) -> None:
        process = self._assistant_process
        if process is None:
            self._assistant_timer.stop()
            return
        return_code = process.poll()
        if return_code is None:
            return
        reap_assistant_processes()
        self._assistant_process = None
        self._assistant_timer.stop()
        self._set_assistant_button(True)
        if return_code != 0:
            QMessageBox.warning(
                self,
                tr("ASSISTANT failed to start"),
                tr("ASSISTANT exited during startup with code {return_code}.", return_code=return_code),
            )

    def _tool_row(self, title: str, description: str, slot) -> QWidget:
        container = QWidget()
        container.setObjectName("panelTransparent")
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        body = QVBoxLayout()
        body.setSpacing(2)
        name = QLabel(title)
        name.setObjectName("section2")
        body.addWidget(name)
        body.addWidget(info_label(description))
        row.addLayout(body, 1)
        button = QPushButton(tr("Run"))
        button.clicked.connect(slot)
        self.buttons.append(button)
        row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        return container

    def _destructive_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Reset or uninstall"), level=2))
        layout.addWidget(
            info_label(
                tr("These destructive actions show the exact folders to be deleted and ask for confirmation first.")
            )
        )
        panels = QHBoxLayout()
        panels.setSpacing(14)

        fresh_panel, self.fresh_reset_button = self._destructive_panel(
            tr("Fresh reset"),
            tr(
                "Deletes the Anomaly and GAMMA folders, then reinstalls both from "
                "scratch into the same locations."
            ),
            [
                tr("Deletes ALL saves, MO2 settings, MCM settings and any mods you added"),
                tr("Requires Anomaly and GAMMA to be installed"),
            ],
            tr("Fresh Reset"),
            self._start_fresh_reset,
        )
        panels.addWidget(fresh_panel, 1)

        gamma_panel, self.gamma_reset_button = self._destructive_panel(
            tr("GAMMA reset"),
            tr(
                "Deletes the GAMMA folder and reinstalls GAMMA while preserving the "
                "existing Anomaly installation."
            ),
            [
                tr("Deletes GAMMA saves, MO2 settings, MCM settings and added mods"),
                tr("Preserves the Anomaly folder and installation"),
            ],
            tr("GAMMA Reset"),
            self._start_gamma_reset,
        )
        panels.addWidget(gamma_panel, 1)

        full_panel, self.full_uninstall_button = self._destructive_panel(
            tr("Full uninstall"),
            tr(
                "Removes the Anomaly, GAMMA, and cache folders, leaving your "
                "Wine/Proton prefix intact."
            ),
            [
                tr(
                    "Deletes ALL saves, MO2 settings, MCM settings, added mods "
                    "and the download cache"
                ),
                tr(
                    "The configured Wine/Proton prefix and its Winetricks "
                    "configuration are kept"
                ),
            ],
            tr("Full Uninstall"),
            self._start_full_uninstall,
        )
        panels.addWidget(full_panel, 1)
        layout.addLayout(panels)

        caution = info_label(
            tr("All reset and uninstall actions refuse to operate on system paths, home directories or symlinks, and re-check that the profile still points where it did before deleting anything.")
        )
        caution.setObjectName("warn")
        layout.addWidget(caution)

        self.fresh_reset_hint = info_label(
            tr("Fresh Reset requires Anomaly and GAMMA; GAMMA Reset requires GAMMA.")
        )
        layout.addWidget(self.fresh_reset_hint)
        self._add_console(layout)
        return card

    def _destructive_panel(
        self,
        title: str,
        description: str,
        bullets: list[str],
        button_text: str,
        slot,
    ) -> tuple[QWidget, QPushButton]:
        panel = QWidget()
        panel.setObjectName("panelTransparent")
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        v.addWidget(section_label(title, level=2))
        v.addWidget(info_label(description))
        for bullet in bullets:
            v.addWidget(info_label(tr("• {bullet}", bullet=bullet)))
        button = QPushButton(button_text)
        button.setObjectName("danger")
        button.clicked.connect(slot)
        v.addStretch(1)
        v.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        return panel, button

    # ----- move game -----
    def _move_card(self) -> QWidget:
        card, layout = make_card()
        layout.addWidget(section_label(tr("Move installation"), level=2))
        layout.addWidget(
            info_label(
                tr("Move the Anomaly, GAMMA, and cache folders to another drive. Files are copied and checked before the originals are removed.")
            )
        )

        self._move_anomaly_label = info_label(tr("Anomaly: (no profile)"))
        self._move_gamma_label = info_label(tr("GAMMA: (no profile)"))
        self._move_cache_label = info_label(tr("Cache: (no profile)"))
        current = info_label(tr("Current:"))
        current.setObjectName("section2")
        for label in (current, self._move_anomaly_label, self._move_gamma_label, self._move_cache_label):
            label.setContentsMargins(0, 0, 0, 0)

        paths_box = QWidget()
        paths_box.setObjectName("panelTransparent")
        paths_v = QVBoxLayout(paths_box)
        paths_v.setContentsMargins(12, 8, 12, 8)
        paths_v.setSpacing(2)
        paths_v.addWidget(current)
        paths_v.addWidget(self._move_anomaly_label)
        paths_v.addWidget(self._move_gamma_label)
        paths_v.addWidget(self._move_cache_label)

        dest_box = QWidget()
        dest_box.setObjectName("panelTransparent")
        dest_v = QVBoxLayout(dest_box)
        dest_v.setContentsMargins(12, 8, 12, 8)
        dest_v.setSpacing(6)
        dest_label = info_label(tr("Destination:"))
        dest_label.setObjectName("section2")
        dest_label.setContentsMargins(0, 0, 0, 0)
        dest_v.addWidget(dest_label)
        dest_row = QHBoxLayout()
        dest_row.setSpacing(10)
        self._move_dest_edit = QLineEdit()
        self._move_dest_edit.setPlaceholderText("Select destination folder...")
        dest_btn = QPushButton(tr("Browse..."))
        dest_btn.clicked.connect(self._browse_move_dest)
        dest_row.addWidget(self._move_dest_edit, 1)
        dest_row.addWidget(dest_btn)
        dest_v.addLayout(dest_row)

        side_by_side = QHBoxLayout()
        side_by_side.setSpacing(14)
        side_by_side.addWidget(paths_box, 2)
        side_by_side.addWidget(dest_box, 3)
        layout.addLayout(side_by_side)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self._move_btn = QPushButton(tr("Move installation"))
        self._move_btn.setObjectName("danger")
        self._move_btn.clicked.connect(self._start_move)
        btn_row.addWidget(self._move_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self._move_cancel_btn = QPushButton(tr("Cancel"))
        self._move_cancel_btn.setObjectName("secondary")
        self._move_cancel_btn.setFixedSize(100, 32)
        self._move_cancel_btn.clicked.connect(self._cancel_move)
        self._move_cancel_btn.hide()
        btn_row.addWidget(self._move_cancel_btn, 0, Qt.AlignmentFlag.AlignLeft)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._move_progress = ProgressArea(
            show_table=False, show_log=True, log_max_height=110
        )
        layout.addWidget(self._move_progress)
        return card

    def _browse_move_dest(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select destination folder", self._move_dest_edit.text()
        )
        if path:
            self._move_dest_edit.setText(path)

    def _refresh_move_paths(self) -> None:
        profile = self.window.settings.active_profile
        if profile is None:
            self._move_anomaly_label.setText(tr("Anomaly: (no profile)"))
            self._move_gamma_label.setText(tr("GAMMA: (no profile)"))
            self._move_cache_label.setText(tr("Cache: (no profile)"))
            self._move_btn.setEnabled(False)
            return
        self._move_anomaly_label.setText(tr("Anomaly:  {anomaly}", anomaly=profile.anomaly))
        self._move_gamma_label.setText(tr("GAMMA:    {gamma}", gamma=profile.gamma))
        self._move_cache_label.setText(tr("Cache:    {cache}", cache=profile.cache))
        has_paths = bool(profile.anomaly and profile.gamma and profile.cache)
        self._move_btn.setEnabled(has_paths and not self.window.install_busy)

    def _start_move(self) -> None:
        if self._move_task is not None:
            QMessageBox.information(self, tr("Busy"), tr("A move is already in progress."))
            return
        if self._runner is not None and self._runner.is_running():
            QMessageBox.information(self, tr("Busy"), tr("Another task is running."))
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("An install is already running."))
            return
        if not self._require_profile():
            return

        dest = self._move_dest_edit.text().strip()
        if not dest:
            QMessageBox.warning(
                self, tr("No Destination"), tr("Select a destination folder first.")
            )
            return
        sources = [
            ("Anomaly", self.window.settings.active_profile.anomaly),
            ("GAMMA", self.window.settings.active_profile.gamma),
            ("Cache", self.window.settings.active_profile.cache),
        ]
        try:
            dest_path = _validate_move_destination(dest, sources)
        except ValueError as exc:
            QMessageBox.warning(
                self, tr("Invalid Destination"), str(exc)
            )
            return

        profile = self.window.settings.active_profile
        profile_name = profile.profile_name

        answer = QMessageBox.question(
            self,
            tr("Move installation"),
            "<html><body>"
            "<div style='font-weight: bold; font-size: 13px;'>Move installation</div><br>"
            "This will copy the following folders to the destination and "
            "then remove the originals:<br><br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;Anomaly: {escape(profile.anomaly)}<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;GAMMA: {escape(profile.gamma)}<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;Cache: {escape(profile.cache)}<br><br>"
            f"<strong>Destination:</strong> {escape(dest)}<br><br>"
            "Make sure the destination has enough free space.<br><br>"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # install_busy may have flipped True while the dialog was open.
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("A task is already running."))
            return
        if not self._check_mo2_free():
            return

        self.window.set_install_busy(True)
        self._move_profile_name = profile_name
        self._move_sources = list(sources)
        self._show_console()
        self._move_progress.reset()
        self._move_progress.on_started()
        self._move_btn.setEnabled(False)
        self._move_cancel_btn.show()
        self._set_buttons_enabled(False)
        gui_settings.save_gui_settings(
            move_dest=str(dest_path),
            move_expected=[Path(path).name for _label, path in sources if path],
        )

        task = StreamTask(
            lambda report: _move_folders(sources, dest_path, report, task.cancel_event),
            parent=self,
        )
        self._move_task = task
        task.line.connect(self._on_move_progress)
        task.result.connect(self._on_move_done)
        task.error.connect(self._on_move_error)
        task.start()

    def _on_move_progress(self, line: str) -> None:
        self._move_progress.on_line(line)

    def _on_move_done(self, moved: object) -> None:
        self._move_task = None
        self._move_cancel_btn.hide()
        self._move_progress.on_finished(0, "")
        self._move_progress.status_message("Move copied; verifying configuration")
        # Keep the busy lock until the INI rewrite, profile save, and
        # verification below have all finished; releasing it early lets the
        # user start a reset/second move against not-yet-updated paths.

        consistency_error = None
        if isinstance(moved, list) and moved:
            ini_error = None
            try:
                old_paths = dict(self._move_sources)
                new_paths = dict(moved)
                _rewrite_mo2_ini_paths(
                    new_paths["GAMMA"],
                    [
                        (old_paths["Anomaly"], new_paths["Anomaly"]),
                        (old_paths["GAMMA"], new_paths["GAMMA"]),
                    ],
                )
            except (OSError, ValueError, KeyError) as exc:
                ini_error = exc
            try:
                _save_moved_profile(
                    self.window.settings,
                    self._move_profile_name or "",
                    moved,
                )
            except (OSError, ValueError) as exc:
                consistency_error = exc
            self.window.refresh_settings()
            saved_profile = next(
                (
                    candidate
                    for candidate in self.window.settings.profiles
                    if candidate.profile_name == self._move_profile_name
                ),
                None,
            )
            _ATTRS = {"Anomaly": "anomaly", "GAMMA": "gamma", "Cache": "cache"}
            if saved_profile is None or any(
                getattr(saved_profile, _ATTRS.get(label, label)) != new_path
                for label, new_path in moved
            ):
                consistency_error = consistency_error or OSError(
                    "The moved profile paths could not be verified after saving"
                )
            if ini_error is not None:
                consistency_error = consistency_error or OSError(
                    f"ModOrganizer.ini could not be updated: {ini_error}"
                )

            # Refresh pages that cache profile paths so the next launch uses the
            # moved installation without requiring a restart or tab switch.
            for key in ("dashboard", "play", "install", "modmanager"):
                page = getattr(self.window, "_pages", {}).get(key)
                if page is not None and hasattr(page, "refresh"):
                    page.refresh()

        self._refresh_move_paths()
        if isinstance(moved, list) and moved:
            if consistency_error is None:
                self._move_progress.status_message("Move complete")
                message = (
                    "Game folders moved successfully.\n"
                    "Profile paths and ModOrganizer.ini have been verified."
                )
            else:
                self._move_progress.status_message("Move requires manual recovery")
                message = (
                    "Game folders were copied, but the move could not be made fully "
                    "consistent:\n"
                    f"{consistency_error}\n\n"
                    "Manual recovery is required. Verify ModOrganizer.ini and the "
                    "profile paths before deleting or reusing either location."
                )
            QMessageBox.information(
                self,
                "Move Complete" if consistency_error is None else "Move Recovery Required",
                message,
            )
            # Keep the interrupted-move marker until both configuration stores
            # have been successfully written and verified.
            if consistency_error is None:
                gui_settings.save_gui_settings(move_dest="", move_expected=[])
        else:
            # Nothing was moved (all sources missing): clear the marker so a
            # bogus "interrupted move" recovery is not offered on next launch.
            gui_settings.save_gui_settings(move_dest="", move_expected=[])
        # Now that paths, INI, and verification have settled, release the lock.
        self.window.set_install_busy(False)
        self._set_buttons_enabled(True)
        self._move_profile_name = None
        self._move_sources = []

    def _on_move_error(self, message: str) -> None:
        self._move_task = None
        self._move_profile_name = None
        self._move_sources = []
        self._move_cancel_btn.hide()
        self._move_progress.on_finished(1, "")
        self._move_progress.status_message("Move failed")
        self.window.set_install_busy(False)
        self._set_buttons_enabled(True)
        self._refresh_move_paths()
        QMessageBox.warning(self, tr("Move Failed"), tr("Move failed:\n{message}", message=message))

    def _cancel_move(self) -> None:
        if self._move_task is not None:
            self._move_task.cancel()
            self._move_progress.status_message("Cancelling ...")

    # ----- console -----
    def _add_console(self, layout) -> None:
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(section_label(tr("Console"), level=2))
        header.addStretch(1)
        self._console_toggle = QPushButton(tr("Show"))
        self._console_toggle.setObjectName("consoleToggle")
        self._console_toggle.setFixedSize(60, 26)
        self._console_toggle.clicked.connect(self._toggle_console)
        header.addWidget(self._console_toggle)
        layout.addLayout(header)

        self.summary = QLabel("")
        self.summary.setObjectName("accent")
        layout.addWidget(self.summary)
        self.output = OutputPane()
        self.output.setMaximumHeight(140)
        layout.addWidget(self.output)

        self.summary.hide()
        self.output.hide()
        self._console_visible = False

    def _toggle_console(self) -> None:
        if self._console_visible:
            self.summary.hide()
            self.output.hide()
            self._console_toggle.setText(tr("Show"))
            self._console_visible = False
        else:
            self.summary.show()
            self.output.show()
            self._console_toggle.setText(tr("Hide"))
            self._console_visible = True

    def _show_console(self) -> None:
        if not self._console_visible:
            self.summary.show()
            self.output.show()
            self._console_toggle.setText(tr("Hide"))
            self._console_visible = True

    def refresh(self) -> None:
        self.window.refresh_settings()
        self._update_fresh_reset_enabled()
        self._refresh_move_paths()

    def _update_fresh_reset_enabled(self) -> None:
        profile = self.window.settings.active_profile
        installed = (
            profile is not None
            and anomaly_installed(profile.anomaly)
            and gamma_installed(profile.gamma, profile.mo2_profile)
        )
        fresh_reset_enabled = installed and not self.window.install_busy
        full_uninstall_enabled = (
            profile is not None
            and gamma_installed(profile.gamma, profile.mo2_profile)
            and not self.window.install_busy
        )
        self.fresh_reset_button.setEnabled(fresh_reset_enabled)
        self.gamma_reset_button.setEnabled(
            profile is not None
            and gamma_installed(profile.gamma, profile.mo2_profile)
            and not self.window.install_busy
        )
        self.full_uninstall_button.setEnabled(full_uninstall_enabled)
        self.fresh_reset_hint.setVisible(
            not (fresh_reset_enabled or full_uninstall_enabled)
        )

    def _require_profile(self) -> bool:
        if self.window.settings.active_profile is None:
            QMessageBox.warning(
                self,
                tr("No Profile"),
                tr("Create or activate a profile first (Profiles page)."),
            )
            return False
        return True

    def _confirm(self, text: str, title: str = "Confirm") -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _check_mo2_free(self) -> bool:
        """Warn and refuse when MO2/the game holds the files this action touches."""
        if not mo2_running(force=True):
            return True
        QMessageBox.information(
            self,
            tr("Game Running"),
            tr("Mod Organizer / the game is currently running.\n\nClose it before running this action."),
        )
        return False

    def _run(
        self,
        args: list[str],
        *,
        handler=None,
        confirm: str | None = None,
        confirm_title: str = "Confirm",
        on_finished=None,
        guard_mo2: bool = False,
    ) -> None:
        if self._runner is not None and self._runner.is_running():
            QMessageBox.information(self, tr("Busy"), tr("A task is already running."))
            return
        if confirm is not None and not self._confirm(confirm, confirm_title):
            return
        if guard_mo2 and not self._check_mo2_free():
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("Another install task is running."))
            return
        self._show_console()
        self.summary.setText("")
        self.output.clear()
        self._prune_mb = 0
        self._set_buttons_enabled(False)
        self.window.set_install_busy(True)
        runner = CommandRunner(cli_command(args), parent=self)
        self._runner = runner
        runner.line.connect(handler if handler is not None else self.output.append_line)
        runner.finished.connect(lambda rc, out: self._on_finished(rc, out, on_finished))
        runner.cancelled.connect(lambda: self.output.append_line("[cancelled]"))
        runner.start()

    def on_busy_changed(self, busy: bool) -> None:
        """Global install lock changed; re-evaluate this page's controls."""
        self._set_buttons_enabled(not busy and self._tasks_idle())

    def _tasks_idle(self) -> bool:
        return (
            not (self._runner is not None and self._runner.is_running())
            and self._wipe_task is None
            and self._move_task is None
        )

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in self.buttons:
            btn.setEnabled(
                enabled and not self.window.install_busy and self._tasks_idle()
            )
        self._update_fresh_reset_enabled()

    def _on_finished(self, rc: int, _output: str, on_finished) -> None:
        try:
            if on_finished:
                on_finished(rc, _output)
            elif rc != 0:
                self.output.append_line(f"[command exited with code {rc}]")
        finally:
            self._runner = None
            self.window.set_install_busy(False)
            self._set_buttons_enabled(True)

    # ----- fresh reset -----
    def _start_fresh_reset(self) -> None:
        self._start_reset(include_anomaly=True)

    def _start_gamma_reset(self) -> None:
        self._start_reset(include_anomaly=False)

    def _start_reset(self, *, include_anomaly: bool) -> None:
        if self._runner is not None and self._runner.is_running():
            QMessageBox.information(self, tr("Busy"), tr("A task is already running."))
            return
        if self._wipe_task is not None:
            QMessageBox.information(
                self, tr("Busy"), tr("A reset is already in progress.")
            )
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("An install is already running."))
            return
        if not self._require_profile():
            return
        profile = self.window.settings.active_profile
        self._reset_includes_anomaly = include_anomaly
        self._wipe_targets = (profile.anomaly, profile.gamma)
        wipe_paths = [("GAMMA", profile.gamma)]
        if include_anomaly:
            wipe_paths.insert(0, ("Anomaly", profile.anomaly))
        try:
            _validate_wipe_paths(wipe_paths)
        except ValueError as exc:
            QMessageBox.warning(self, tr("Unsafe Reset"), str(exc))
            return
        title = tr("Fresh Reset") if include_anomaly else tr("GAMMA Reset")
        self._wipe_title = title
        self._reset_preserve_user = False
        self._reset_preserve_mcm = False
        folders = "Anomaly and GAMMA" if include_anomaly else "GAMMA"
        warning = (
            "FRESH RESET WILL COMPLETELY WIPE & RE-INSTALL STALKER ANOMALY & GAMMA FOLDERS"
            if include_anomaly
            else "GAMMA RESET WILL WIPE & RE-INSTALL THE GAMMA FOLDER"
        )
        anomaly_folder = (
            f"&nbsp;&nbsp;&nbsp;&nbsp;{profile.anomaly}<br>" if include_anomaly else ""
        )
        message = (
            "<html><body>"
            "<div style='font-weight: bold; font-size: 13px;'>WARNING</div><br>"
            f"<div style='color: {STATUS_RED.name()}; text-align: center; font-weight: bold; font-size: 14px;'>"
            f"{warning}"
            "</div><br><br>"
            "This will permanently delete the following folders:<br>"
            f"{anomaly_folder}"
            f"&nbsp;&nbsp;&nbsp;&nbsp;{profile.gamma}<br><br>"
            "<strong>THIS DELETES:</strong><br>"
            "&nbsp;&nbsp;• ALL SAVES<br>"
            "&nbsp;&nbsp;• MO2 SETTINGS<br>"
            "&nbsp;&nbsp;• MCM SETTINGS<br>"
            "&nbsp;&nbsp;• ANY ADDITIONAL MODS YOU ADDED<br><br>"
            "Please back up anything you want to keep before continuing.<br><br>"
            f"Are you sure you want to run a {title}?</body></html>"
        )
        if include_anomaly:
            answer = QMessageBox.question(
                self,
                title,
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
        else:
            dialog = QMessageBox(self)
            dialog.setWindowTitle(title)
            dialog.setText(message)
            preserve = QCheckBox(tr("Keep user.ltx and MCM settings"))
            preserve.setChecked(True)
            preserve.setToolTip(
                tr("Preserve your game options, controls, keybindings, and MCM settings during the GAMMA reinstall.")
            )
            dialog.setCheckBox(preserve)
            dialog.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            answer = dialog.exec()
            if answer == QMessageBox.StandardButton.Yes:
                self._reset_preserve_user = preserve.isChecked()
                self._reset_preserve_mcm = preserve.isChecked()
        if answer != QMessageBox.StandardButton.Yes:
            return
        # install_busy may have flipped True while the dialog was open.
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("A task is already running."))
            return
        if not self._check_mo2_free():
            return

        self._reset_wipe_paths = wipe_paths
        self._reset_folders = folders
        self._start_cache_preflight()

    def _start_cache_preflight(self) -> None:
        """Check cached archives before a destructive reset begins."""
        self.window.set_install_busy(True, "cache_preflight")
        self._show_console()
        self.summary.setText(tr("Checking cached GAMMA archives before reset..."))
        self.output.clear()
        self._set_buttons_enabled(False)
        task = StreamTask(
            self._run_cache_preflight,
            parent=self,
        )
        self._cache_task = task
        # Capture the cancel event now: the worker thread must not read
        # self._cache_task, which the GUI thread may set to None mid-run.
        self._cache_cancel_event = task.cancel_event
        task.line.connect(self.output.append_line)
        task.result.connect(self._on_cache_preflight_done)
        task.error.connect(self._on_cache_preflight_error)
        task.start()

    def _run_cache_preflight(self, report):
        profile = self.window.settings.active_profile
        if profile is None:
            raise RuntimeError("No active profile")
        report("Downloading the current official GAMMA archive list...")
        records = fetch_modpack_records(profile.mod_pack_maker_url)
        expected: dict[str, str] = {}
        for record in records.values():
            digest = record.md5_mod_db.lower()
            if len(digest) != 32 or any(char not in "0123456789abcdef" for char in digest):
                continue
            for archive_name in record.archive_names():
                expected.setdefault(archive_name, digest)
        if not expected:
            return None
        return verify_cache_archives(
            profile.cache,
            expected,
            on_progress=lambda done, total, name: report(
                f"Checking cached archive {done}/{total}: {name}"
            ),
            cancel=self._cache_cancel_event,
        )

    def _on_cache_preflight_done(self, result) -> None:
        self._cache_task = None
        if result is None:
            answer = QMessageBox.warning(
                self,
                self._wipe_title,
                tr("The current official archive list could not be loaded, so the number of redownloads cannot be predicted. Continue with the reset?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
        else:
            lines = result.lines()
            for line in lines:
                self.output.append_line(line)
            # Cap the dialog body: a large cache could produce hundreds of
            # lines and blow the message box off the screen.
            shown = lines[:40]
            if len(lines) > len(shown):
                shown.append(f"... and {len(lines) - len(shown)} more line(s)")
            answer = QMessageBox.question(
                self,
                self._wipe_title,
                "Cache preflight complete.\n\n"
                + "\n".join(shown)
                + "\n\nContinue with the reset?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
        if answer != QMessageBox.StandardButton.Yes:
            self.window.set_install_busy(False)
            self._set_buttons_enabled(True)
            self.summary.clear()
            return
        self._begin_reset_wipe()

    def _on_cache_preflight_error(self, message: str) -> None:
        self._cache_task = None
        self.window.set_install_busy(False)
        self._set_buttons_enabled(True)
        self.summary.clear()
        QMessageBox.warning(self, self._wipe_title, tr("Cache preflight failed: {message}", message=message))

    def _begin_reset_wipe(self) -> None:
        self.window.set_install_busy(
            True, "anomaly" if self._reset_includes_anomaly else "gamma"
        )
        self.summary.setText(
            tr("Wiping {reset_folders} folder{arg}...", reset_folders=self._reset_folders, arg='s' if self._reset_includes_anomaly else '')
        )
        self.output.clear()
        self._set_buttons_enabled(False)
        task = StreamTask(
            lambda report: _wipe_folders(self._reset_wipe_paths, report),
            parent=self,
        )
        self._wipe_task = task
        task.line.connect(self.output.append_line)
        task.result.connect(self._on_wipe_done)
        task.error.connect(self._on_wipe_error)
        task.start()

    def _on_wipe_done(self, _wiped: object) -> None:
        self._wipe_task = None
        self._set_buttons_enabled(True)
        profile = self.window.settings.active_profile
        if profile is None:
            self.window.set_install_busy(False)
            QMessageBox.warning(
                self,
                self._wipe_title,
                tr("No active profile. Reinstall aborted."),
            )
            return
        if (
            profile.anomaly != self._wipe_targets[0]
            or profile.gamma != self._wipe_targets[1]
        ):
            self.window.set_install_busy(False)
            QMessageBox.warning(
                self,
                self._wipe_title,
                tr("The active profile's install folders changed during the wipe. Re-install aborted so nothing is installed to the wrong location."),
            )
            return
        if self._reset_includes_anomaly:
            self.summary.setText(tr("Folders wiped. Reinstalling Anomaly and GAMMA..."))
        else:
            self.summary.setText(tr("GAMMA folder wiped. Reinstalling GAMMA..."))
        install_page = self.window._pages["install"]
        self.window.set_page("install")
        if not install_page.start_auto_install(
            include_anomaly=self._reset_includes_anomaly,
            preserve_user=self._reset_preserve_user,
            preserve_mcm=self._reset_preserve_mcm,
        ):
            self.window.set_install_busy(False)
            QMessageBox.warning(
                self,
                self._wipe_title,
                tr("{wipe_title} could not be started (another task is running or no profile is active).", wipe_title=self._wipe_title),
            )

    def _on_wipe_error(self, message: str) -> None:
        self._wipe_task = None
        self.window.set_install_busy(False)
        self._set_buttons_enabled(True)
        self.summary.setText("")
        QMessageBox.warning(self, self._wipe_title, tr("Wipe failed: {message}", message=message))

    def _start_full_uninstall(self) -> None:
        if self._runner is not None and self._runner.is_running():
            QMessageBox.information(self, tr("Busy"), tr("A task is already running."))
            return
        if self._wipe_task is not None:
            QMessageBox.information(
                self, tr("Busy"), tr("A removal task is already in progress.")
            )
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("An install is already running."))
            return
        if not self._require_profile():
            return
        profile = self.window.settings.active_profile
        if not gamma_installed(profile.gamma, profile.mo2_profile):
            QMessageBox.information(self, tr("Not Installed"), tr("GAMMA is not installed."))
            self._update_fresh_reset_enabled()
            return

        self._full_uninstall_targets = (profile.anomaly, profile.gamma, profile.cache)
        uninstall_paths = [
            ("Anomaly", profile.anomaly),
            ("GAMMA", profile.gamma),
            ("Cache", profile.cache),
        ]
        try:
            _validate_wipe_paths(uninstall_paths)
        except ValueError as exc:
            QMessageBox.warning(self, tr("Unsafe Uninstall"), str(exc))
            return
        answer = QMessageBox.question(
            self,
            tr("Full Uninstall"),
            "<html><body>"
            "<div style='font-weight: bold; font-size: 13px;'>WARNING</div><br>"
            f"<div style='color: {STATUS_RED.name()}; text-align: center; font-weight: bold; font-size: 14px;'>"
            "FULL UNINSTALL WILL COMPLETELY REMOVE STALKER ANOMALY & GAMMA"
            "</div><br><br>"
            "This will permanently delete the following folders:<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;{profile.anomaly}<br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;{profile.gamma}<br><br>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;{profile.cache}<br><br>"
            "<strong>THIS DELETES:</strong><br>"
            "&nbsp;&nbsp;• ALL SAVES<br>"
            "&nbsp;&nbsp;• MO2 SETTINGS<br>"
            "&nbsp;&nbsp;• MCM SETTINGS<br>"
            "&nbsp;&nbsp;• ANY ADDITIONAL MODS YOU ADDED<br><br>"
            "&nbsp;&nbsp;• DOWNLOAD CACHE<br><br>"
            "The configured Wine/Proton prefix and its Winetricks configuration "
            "will not be deleted.<br><br>"
            "Please back up anything you want to keep before continuing.<br><br>"
            "Are you sure you want to completely uninstall Anomaly and GAMMA?</body></html>",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # install_busy may have flipped True while the dialog was open.
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("A task is already running."))
            return
        if not self._check_mo2_free():
            return

        self.window.set_install_busy(True)
        self._show_console()
        self.summary.setText(tr("Removing Anomaly, GAMMA, and cache folders..."))
        self.output.clear()
        self._set_buttons_enabled(False)
        task = StreamTask(
            lambda report: _wipe_folders(
                uninstall_paths,
                report,
            ),
            parent=self,
        )
        self._wipe_task = task
        task.line.connect(self.output.append_line)
        task.result.connect(self._on_full_uninstall_done)
        task.error.connect(self._on_full_uninstall_error)
        task.start()

    def _on_full_uninstall_done(self, _wiped: object) -> None:
        self._wipe_task = None
        self.window.set_install_busy(False)
        self._set_buttons_enabled(True)
        self.summary.setText(tr("Anomaly and GAMMA completely uninstalled"))
        self.refresh()
        # Immediately clear the Install page bars and Dashboard rows that
        # would otherwise keep showing a stale "Installed" state.
        for key in ("dashboard", "install"):
            page = getattr(self.window, "_pages", {}).get(key)
            if page is not None and hasattr(page, "refresh"):
                page.refresh()

    def _on_full_uninstall_error(self, message: str) -> None:
        self._wipe_task = None
        self.window.set_install_busy(False)
        self._set_buttons_enabled(True)
        self.summary.setText("")
        QMessageBox.warning(self, tr("Full Uninstall"), tr("Uninstall failed: {message}", message=message))

    # ----- tasks -----
    def _purge_shader_cache(self) -> None:
        if self._require_profile():
            self._run(
                ["anomaly", "purge-shader-cache"],
                confirm="Delete the shader cache for the active Anomaly profile?",
                confirm_title="Purge Shader Cache",
                guard_mo2=True,
            )

    def _delete_reshade(self) -> None:
        if self._require_profile():
            self._run(
                ["anomaly", "delete-reshade"],
                confirm="Delete all ReShade-related files from the Anomaly bin directory?",
                confirm_title="Delete ReShade",
                guard_mo2=True,
            )

    def _prune_check(self) -> None:
        if self._require_profile():
            self._run(["cache", "prune", "check"], handler=self._prune_handler)

    def _prune_apply(self) -> None:
        if self._require_profile():
            self._run(
                ["cache", "prune", "apply"],
                handler=self._prune_handler,
                confirm="Permanently delete out-of-date addon archives from the cache?",
                confirm_title="Prune Cache",
                guard_mo2=True,
            )

    def _prune_handler(self, line: str) -> None:
        clean = strip_ansi(line)
        self.output.append_line(clean)
        archive = parse_prune_archive(clean)
        if archive is not None:
            self._prune_mb = self._prune_mb + archive.mb
            self.summary.setText(tr("Total size to reclaim: {prune_mb} MB", prune_mb=self._prune_mb))
        elif clean.startswith("Total size to reclaim:"):
            self.summary.setText(clean.strip())

    def _gog_fix(self) -> None:
        if self._require_profile():
            self._run(
                ["gog", "fix-install"],
                confirm="Fix the ModOrganizer.ini paths for a GOG-provided install?",
                confirm_title="Fix GOG Install",
                guard_mo2=True,
            )

    # ----- log dump -----
    def _start_log_dump(self) -> None:
        if self._log_dump_task is not None:
            return
        if self.window.install_busy:
            QMessageBox.information(self, tr("Busy"), tr("Another install task is running."))
            return
        self.window.set_install_busy(True)
        self._show_console()
        self.summary.setText(tr("Creating Log Dump..."))
        self.output.clear()
        self._set_buttons_enabled(False)
        task = StreamTask(create_log_dump, parent=self)
        self._log_dump_task = task
        task.line.connect(self.output.append_line)
        task.result.connect(self._on_log_dump_done)
        task.error.connect(self._on_log_dump_error)
        task.start()

    def _on_log_dump_done(self, result: object) -> None:
        self._log_dump_task = None
        try:
            if not isinstance(result, (tuple, list)) or len(result) != 2:
                raise TypeError("worker returned an invalid log dump result")
            path, stats = result
            if not isinstance(path, (str, os.PathLike)):
                raise TypeError("worker returned an invalid log dump path")
            if not isinstance(stats, Mapping):
                raise TypeError("worker returned invalid log dump statistics")

            values = {}
            for field in ("files", "skipped", "bytes"):
                value = stats.get(field, 0)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, Real)
                    or not isfinite(float(value))
                ):
                    raise ValueError(f"worker returned invalid log dump {field}")
                values[field] = int(value)
            files = values["files"]
            skipped = values["skipped"]
            size_mb = values["bytes"] / (1024 * 1024)
            self.summary.setText(tr("Log Dump saved: {path}", path=path))
            dialog = QMessageBox(self)
            dialog.setWindowTitle(tr("Log Dump Created"))
            dialog.setText(
                "Your log dump has been created:<br><br>"
                f"{path}<br><br>"
                f"Files archived: {files}"
                + (f"<br>Skipped (too large): {skipped}" if skipped else "")
                + f"<br>Archive size: {size_mb:.1f} MB<br><br>"
                f"Open this archive in {assistant_token()} to check all the "
                "errors and warnings."
            )
            open_button = dialog.addButton(
                "Open in ASSISTANT", QMessageBox.ButtonRole.AcceptRole
            )
            dialog.addButton(QMessageBox.StandardButton.Close)
            dialog.exec()
            if dialog.clickedButton() is open_button:
                try:
                    process = launch_assistant(path)
                    self._track_assistant(process)
                except AssistantLaunchError as exc:
                    title = (
                        tr("ASSISTANT Already Open")
                        if "already running" in str(exc)
                        else tr("ASSISTANT Unavailable")
                    )
                    QMessageBox.information(self, title, str(exc))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            self.summary.setText("")
            QMessageBox.warning(
                self, tr("Log Dump Failed"), tr("Could not create log dump:\n{exc}", exc=exc)
            )
        finally:
            self.window.set_install_busy(False)
            self._set_buttons_enabled(True)

    def _on_log_dump_error(self, message: str) -> None:
        self._log_dump_task = None
        self._set_buttons_enabled(True)
        self.summary.setText("")
        self.window.set_install_busy(False)
        QMessageBox.warning(
            self, tr("Log Dump Failed"), tr("Could not create log dump:\n{message}", message=message)
        )
