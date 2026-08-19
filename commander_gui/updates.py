"""GUI-side GAMMA update checking.

The bundled CLI's ``update check`` computes repo diffs through the GitHub REST
API (``api.github.com``), which is rate-limited to 60 requests/hour per IP and
frequently fails with 403. The same information is available without touching
that API:

* the official modpack maker list at ``profile.mod_pack_maker_url``
  (stalker-gamma.com) vs the locally stored ``modpack_maker_list.txt`` - a
  per-addon diff including archive MD5 changes (Added / Modified / Removed);
* the version marker ``G.A.M.M.A_definition_version.txt`` from the
  Stalker_GAMMA repo (served from raw.githubusercontent.com, not rate-limited)
  vs the installed ``gamma/version.txt``.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .parsers import UpdateDiff
from .repair import USER_AGENT, ModPackRecord, parse_modpack_records

VERSION_FILENAME = "G.A.M.M.A_definition_version.txt"
PATCHNOTES_FILENAME = "Patchnotes.md"
README_FILENAME = "README.md"
REMOTE_TIMEOUT = 15.0

_PATCHNOTES_VERSION_RE = re.compile(
    r"^#\s*\*\*GAMMA\s+(?P<version>[0-9]+(?:\.[0-9]+)+)\*\*"
)
_README_VERSION_RE = re.compile(r"gamma-v(?P<version>[0-9]+(?:\.[0-9]+)+)")


@dataclass
class UpdateStatus:
    """Result of a GUI-side update check. Never raises for network issues."""

    installed: str | None = None
    latest: str | None = None
    #: Human-readable GAMMA version (e.g. "0.9.5") for each build number.
    installed_human: str | None = None
    latest_human: str | None = None
    diffs: list[UpdateDiff] = field(default_factory=list)
    error: str | None = None

    @property
    def update_available(self) -> bool:
        if self.error:
            return False
        if self.diffs:
            return True
        return bool(self.latest and self.installed and self.latest != self.installed)


def format_version(build: str | None, human: str | None, missing: str = "Not installed") -> str:
    """Render a version as ``"0.9.5 (build 920)"``.

    The build number is always kept because the human label is coarse and only
    known for the latest release; an outdated install falls back to the bare
    build number (``"build 910"``).
    """
    if not build:
        return missing
    if human:
        return f"{human} (build {build})"
    return f"build {build}"


def status_summary(status: UpdateStatus) -> tuple[str, str]:
    """Return (status text, QSS objectName) for a check result.

    ``objectName`` is one of ``accent`` (green), ``warn`` (amber) or ``dim``.
    """
    if status.error:
        return status.error, "warn"
    if status.installed is None:
        return "GAMMA is not installed yet - run a full install first.", "warn"
    if status.update_available:
        if status.latest and status.installed and status.latest != status.installed:
            text = (
                "Update available "
                f"({format_version(status.installed, status.installed_human)} → "
                f"{format_version(status.latest, status.latest_human)})"
            )
        else:
            text = "Addon updates available"
        if status.diffs:
            text += f" - {len(status.diffs)} change(s)"
        return text, "accent"
    return "GAMMA is up to date", "accent"


def installed_version(gamma_dir: str | None) -> str | None:
    """Return the installed GAMMA version from ``gamma/version.txt``."""
    if not gamma_dir:
        return None
    try:
        text = Path(gamma_dir, "version.txt").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    version = text.strip()
    return version or None


def _raw_repo_url(profile, filename: str) -> str:
    """Raw.githubusercontent URL for a file in the GAMMA repo."""
    repo_url = (getattr(profile, "stalker_gamma_repo_url", "") or "").strip()
    branch = (getattr(profile, "stalker_gamma_repo_branch", "") or "main").strip()
    parts = repo_url.rstrip("/").split("/")
    owner = parts[-2] if len(parts) >= 2 else "Grokitach"
    repo = parts[-1] if len(parts) >= 1 else "Stalker_GAMMA"
    return (
        f"https://raw.githubusercontent.com/{owner}/{repo}/"
        f"refs/heads/{branch}/{filename}"
    )


def remote_version(profile) -> str | None:
    """Fetch the latest GAMMA version marker; None if unreachable."""
    try:
        req = urllib.request.Request(
            _raw_repo_url(profile, VERSION_FILENAME),
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT) as resp:
            version = resp.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    return version or None


def latest_version_human(profile) -> str | None:
    """Fetch the human-readable GAMMA version (e.g. "0.9.5").

    Taken from the repo's ``Patchnotes.md`` heading (``# **GAMMA 0.9.5**``),
    falling back to the README badge ``gamma-v0.9.5``. None if unreachable.
    """
    text: str | None = None
    for filename in (PATCHNOTES_FILENAME, README_FILENAME):
        try:
            req = urllib.request.Request(
                _raw_repo_url(profile, filename),
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            if text:
                break
        except OSError:
            text = None
            continue
    if not text:
        return None
    match = _PATCHNOTES_VERSION_RE.search(text)
    if match is None:
        match = _README_VERSION_RE.search(text)
    return match.group("version") if match else None


def _records_by_dl_link(
    records: dict[str, ModPackRecord],
) -> dict[str, ModPackRecord]:
    """Re-key records by their stable download link (folder-name fallback).

    Folder names embed the list line number, which shifts whenever the official
    list is reordered, so the download link (moddb ``/addons/start/<id>``) is the
    stable identity across GAMMA versions.
    """
    by_link: dict[str, ModPackRecord] = {}
    for record in records.values():
        key = (record.dl_link or "").strip() or record.folder_name
        by_link.setdefault(key, record)
    return by_link


def local_modpack_records(gamma_dir: str, mo2_profile: str) -> dict[str, ModPackRecord] | None:
    """Records of what this install contains, or None if the profile has none.

    The CLI writes ``modpack_maker_list.txt`` (and a JSON twin) into the active
    profile after a full install. The TSV is preferred; the JSON is parsed when
    only it exists.
    """
    profile_dir = Path(gamma_dir, "profiles", mo2_profile)
    txt_path = profile_dir / "modpack_maker_list.txt"
    json_path = profile_dir / "modpack_maker_list.json"
    try:
        if txt_path.is_file():
            return parse_modpack_records(
                txt_path.read_text(encoding="utf-8", errors="replace")
            )
        if json_path.is_file():
            records: dict[str, ModPackRecord] = {}
            entries = json.loads(json_path.read_text(encoding="utf-8"))
            for counter, entry in enumerate(entries, start=1):
                addon = (entry.get("addonName") or "").strip()
                if not addon:
                    continue
                record = ModPackRecord(
                    counter=counter,
                    addon_name=addon,
                    patch=(entry.get("patch") or "").strip(),
                    dl_link=(entry.get("dlLink") or "").strip(),
                    mod_db_url=(entry.get("modDbUrl") or "").strip(),
                    zip_name=(entry.get("zipName") or "").strip(),
                    md5_mod_db=(entry.get("md5ModDb") or "").strip(),
                    instructions="",
                )
                records[record.folder_name] = record
            return records
    except OSError:
        return None
    except ValueError:
        return None
    return None


def diff_records(
    local: dict[str, ModPackRecord],
    remote: dict[str, ModPackRecord],
) -> list[UpdateDiff]:
    """Compute the Added / Modified / Removed diff between two modpack lists.

    Addons are matched by download link; a matching addon whose archive MD5
    differs is reported as Modified with the local and remote hashes.
    """
    diffs: list[UpdateDiff] = []
    local_by_link = _records_by_dl_link(local)
    remote_by_link = _records_by_dl_link(remote)
    local_keys = set(local_by_link)
    remote_keys = set(remote_by_link)
    for key in sorted(remote_keys - local_keys):
        diffs.append(UpdateDiff("Added", remote_by_link[key].folder_name))
    for key in sorted(local_keys - remote_keys):
        diffs.append(UpdateDiff("Removed", local_by_link[key].folder_name))
    for key in sorted(local_keys & remote_keys):
        local_hash = (local_by_link[key].md5_mod_db or "").lower()
        remote_hash = (remote_by_link[key].md5_mod_db or "").lower()
        if local_hash and remote_hash and local_hash != remote_hash:
            diffs.append(
                UpdateDiff(
                    "Modified",
                    f"{local_by_link[key].folder_name} -> {local_hash} -> {remote_hash}",
                )
            )
    return diffs


def check_updates(profile) -> UpdateStatus:
    """Check the active profile for GAMMA updates without the GitHub API."""
    status = UpdateStatus()
    gamma_dir = getattr(profile, "gamma", None) or ""
    mo2_profile = getattr(profile, "mo2_profile", "") or "G.A.M.M.A"

    if not gamma_dir or not Path(gamma_dir).is_dir():
        status.error = "GAMMA is not installed yet. Run a full install first."
        return status

    status.installed = installed_version(gamma_dir)

    local = local_modpack_records(gamma_dir, mo2_profile)
    if local is None:
        status.error = (
            "No modpack list found in this profile. Run a full install so the "
            "installer can generate the installed-addon list."
        )
        return status

    remote: dict[str, ModPackRecord] = {}
    mod_pack_url = getattr(profile, "mod_pack_maker_url", "") or ""
    if mod_pack_url:
        try:
            req = urllib.request.Request(
                mod_pack_url, headers={"User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT) as resp:
                remote = parse_modpack_records(
                    resp.read().decode("utf-8", errors="replace")
                )
        except OSError as exc:
            status.error = f"Could not reach the official mod list: {exc}"
            return status
    else:
        status.error = "The profile has no modpack maker URL configured."
        return status

    status.latest = remote_version(profile)
    status.latest_human = latest_version_human(profile)
    # The human label is only reliable for the latest release; an installed
    # build that is not current keeps its bare build number instead.
    if status.installed and status.latest and status.installed == status.latest:
        status.installed_human = status.latest_human
    status.diffs = diff_records(local, remote)
    if not status.error and not status.latest and not status.diffs:
        status.error = (
            "Could not fetch the latest GAMMA version marker; the addon list "
            "itself is up to date."
        )
    return status
