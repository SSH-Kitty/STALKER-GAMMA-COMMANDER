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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import tr
from .network import read_response_bytes
from .network import urlopen_with_retry as urlopen
from .parsers import UpdateDiff
from .repair import USER_AGENT, ModPackRecord, parse_modpack_records

VERSION_FILENAME = "G.A.M.M.A_definition_version.txt"
PATCHNOTES_FILENAME = "Patchnotes.md"
README_FILENAME = "README.md"
REMOTE_TIMEOUT = 15.0
_MAX_VERSION_BYTES = 1 * 1024 * 1024
_MAX_MARKDOWN_BYTES = 8 * 1024 * 1024
_MAX_MODPACK_BYTES = 32 * 1024 * 1024

_PATCHNOTES_VERSION_RE = re.compile(
    r"^#\s*\*\*GAMMA\s+(?P<version>[0-9]+(?:\.[0-9]+)+)\*\*"
)
_README_VERSION_RE = re.compile(r"gamma-v(?P<version>[0-9]+(?:\.[0-9]+)+)")
#: A release's own heading in Patchnotes.md - always a level-1 Markdown
#: heading ("# **GAMMA 0.9.5**", "# **...0.9.4 Patch Notes**", ...); the
#: exact wording has drifted across releases, so this only anchors on
#: the "# " marker itself, not any particular phrasing.
_RELEASE_HEADING_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)


@dataclass
class UpdateStatus:
    """Result of a GUI-side update check. Never raises for network issues."""

    installed: str | None = None
    latest: str | None = None
    #: Human-readable GAMMA version (e.g. "0.9.5") for each build number.
    installed_human: str | None = None
    latest_human: str | None = None
    #: Full Patchnotes.md body for the latest release, or None if it
    #: couldn't be fetched - see fetch_latest_patchnotes().
    patchnotes: str | None = None
    diffs: list[UpdateDiff] = field(default_factory=list)
    error: str | None = None

    @property
    def update_available(self) -> bool:
        if self.error:
            return False
        if self.diffs:
            return True
        return bool(self.latest and self.installed and self.latest != self.installed)


def format_version(
    build: str | None, human: str | None, missing: str = "Not installed"
) -> str:
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
        return tr("GAMMA is not installed yet - run a full install first."), "warn"
    if status.update_available:
        if status.latest and status.installed and status.latest != status.installed:
            text = tr(
                "Update available ({installed} → {latest})",
                installed=format_version(status.installed, status.installed_human),
                latest=format_version(status.latest, status.latest_human),
            )
        else:
            text = tr("Mod updates available")
        if status.diffs:
            text += " " + tr("- {count} change(s)", count=len(status.diffs))
        return text, "accent"
    return tr("GAMMA is up to date"), "accent"


def installed_version(gamma_dir: str | None) -> str | None:
    """Return the installed GAMMA version from ``gamma/version.txt``."""
    if not gamma_dir:
        return None
    try:
        text = Path(gamma_dir, "version.txt").read_text(
            encoding="utf-8", errors="replace"
        )
    except (OSError, ValueError, UnicodeError):
        return None
    version = text.strip()
    return version or None


def _repo_owner_and_name(profile) -> tuple[str, str]:
    repo_url = (getattr(profile, "stalker_gamma_repo_url", "") or "").strip()
    # str.split("/") always returns at least one (possibly empty) element,
    # even for "" - so `len(parts) >= 1` can never actually be False and the
    # "Stalker_GAMMA" fallback below it was dead code; an emptied repo URL
    # produced "https://.../Grokitach//refs/heads/..." (empty repo segment)
    # instead of ever reaching that fallback. Check emptiness directly.
    parts = repo_url.rstrip("/").split("/") if repo_url else []
    owner = parts[-2] if len(parts) >= 2 else "Grokitach"
    repo = parts[-1] if parts and parts[-1] else "Stalker_GAMMA"
    return owner, repo


def _repo_branch(profile) -> str:
    return (getattr(profile, "stalker_gamma_repo_branch", "") or "main").strip()


def _raw_repo_url(profile, filename: str) -> str:
    """Raw.githubusercontent URL for a file in the GAMMA repo."""
    owner, repo = _repo_owner_and_name(profile)
    branch = _repo_branch(profile)
    return (
        f"https://raw.githubusercontent.com/{owner}/{repo}/"
        f"refs/heads/{branch}/{filename}"
    )


def changelog_web_url(profile) -> str:
    """Browser-facing (non-raw) GitHub URL for the repo's Patchnotes.md.

    For the "View full changelog on GitHub" link - same owner/repo/branch
    resolution as _raw_repo_url(), just a normal blob view a person can
    actually open instead of a raw-text fetch URL.
    """
    owner, repo = _repo_owner_and_name(profile)
    branch = _repo_branch(profile)
    return f"https://github.com/{owner}/{repo}/blob/{branch}/{PATCHNOTES_FILENAME}"


def remote_version(profile) -> str | None:
    """Fetch the latest GAMMA version marker; None if unreachable."""
    try:
        req = urllib.request.Request(
            _raw_repo_url(profile, VERSION_FILENAME),
            headers={"User-Agent": USER_AGENT},
        )
        with urlopen(req, timeout=REMOTE_TIMEOUT) as resp:
            version = (
                read_response_bytes(resp, _MAX_VERSION_BYTES)
                .decode("utf-8", errors="replace")
                .strip()
            )
    except (OSError, ValueError, UnicodeError):
        return None
    return version or None


def fetch_latest_patchnotes(profile) -> str | None:
    """Fetch the repo's full ``Patchnotes.md`` body; None if unreachable.

    latest_version_human() only ever needed a version-number regex match
    out of this same fetch and discarded the rest - this keeps the full
    text so callers (the Updates page's "What's New" panel) can show it.
    """
    try:
        req = urllib.request.Request(
            _raw_repo_url(profile, PATCHNOTES_FILENAME),
            headers={"User-Agent": USER_AGENT},
        )
        with urlopen(req, timeout=REMOTE_TIMEOUT) as resp:
            text = read_response_bytes(resp, _MAX_MARKDOWN_BYTES).decode(
                "utf-8", errors="replace"
            )
    except (OSError, ValueError, UnicodeError):
        return None
    return text or None


def _version_from_text(text: str | None) -> str | None:
    if not text:
        return None
    match = _PATCHNOTES_VERSION_RE.search(text) or _README_VERSION_RE.search(text)
    return match.group("version") if match else None


def parse_patchnotes_sections(text: str) -> list[tuple[str, str]]:
    """Split a Patchnotes.md body into (title, body) per release.

    Patchnotes.md is not just the latest release's notes - it's the
    whole history, one level-1 Markdown heading per release (confirmed
    against the real file: 0.9.5, 0.9.4, 0.9.3.1, 0.9.3, three separate
    0.9.1 entries), stacked oldest-last. ``latest_version_human()``/
    ``fetch_latest_patchnotes()`` only ever needed the very first one;
    this is for showing the rest too, each as its own collapsible entry,
    in the same (already latest-first) order the file itself uses.

    ``title`` has its surrounding Markdown bold markers (``**``) and
    whitespace stripped, since the exact heading wording has drifted
    release to release ("# **GAMMA 0.9.5**" vs
    "# **S.T.A.L.K.E.R. G.A.M.M.A. 0.9.4 Patch Notes**"). An empty list
    means no level-1 heading was found at all (an unexpected format) -
    callers should fall back to showing the raw text as a single,
    untitled section rather than showing nothing.
    """
    matches = list(_RELEASE_HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        title = match.group(1).strip().strip("*").strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        sections.append((title, body))
    return sections


def latest_version_human(profile) -> str | None:
    """Fetch the human-readable GAMMA version (e.g. "0.9.5").

    Taken from the repo's ``Patchnotes.md`` heading (``# **GAMMA 0.9.5**``),
    falling back to the README badge ``gamma-v0.9.5``. None if unreachable.
    """
    version = _version_from_text(fetch_latest_patchnotes(profile))
    if version:
        return version
    try:
        req = urllib.request.Request(
            _raw_repo_url(profile, README_FILENAME),
            headers={"User-Agent": USER_AGENT},
        )
        with urlopen(req, timeout=REMOTE_TIMEOUT) as resp:
            text = read_response_bytes(resp, _MAX_MARKDOWN_BYTES).decode(
                "utf-8", errors="replace"
            )
    except (OSError, ValueError, UnicodeError):
        return None
    return _version_from_text(text)


_COMMANDER_REPO = "https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER"
_VERSION_NUMERIC_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*")


def _numeric_version_tuple(text: str) -> tuple[int, ...] | None:
    match = _VERSION_NUMERIC_RE.search(text)
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def check_commander_update(current_version: str) -> str | None:
    """Return the newer COMMANDER release tag, or None if up to date/unreachable.

    Deliberately avoids api.github.com/repos/.../releases/latest - like the
    GAMMA checks above, that endpoint is rate-limited to 60 requests/hour
    per IP and frequently 403s. GitHub's own "/releases/latest" HTML page
    redirects (302) to "/releases/tag/<name>" without touching the REST
    API at all - a HEAD request just needs the resolved URL, not the page
    body, to read the tag name off it.
    """
    current = _numeric_version_tuple(current_version)
    if current is None:
        return None
    try:
        req = urllib.request.Request(
            f"{_COMMANDER_REPO}/releases/latest",
            method="HEAD",
            headers={"User-Agent": USER_AGENT},
        )
        with urlopen(req, timeout=REMOTE_TIMEOUT) as resp:
            final_url = resp.geturl()
    except (OSError, ValueError):
        return None
    match = re.search(r"/releases/tag/([^/]+)/?$", final_url)
    if not match:
        return None
    tag = urllib.parse.unquote(match.group(1))
    remote = _numeric_version_tuple(tag)
    if remote is None or remote <= current:
        return None
    return tag


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


def local_modpack_records(
    gamma_dir: str, mo2_profile: str
) -> dict[str, ModPackRecord] | None:
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
            if not isinstance(entries, list):
                return None
            for counter, entry in enumerate(entries, start=1):
                if not isinstance(entry, dict):
                    continue
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
        local_record = local_by_link[key]
        remote_record = remote_by_link[key]
        local_hash = (local_record.md5_mod_db or "").lower()
        remote_hash = (remote_record.md5_mod_db or "").lower()
        archive_changed = local_record.zip_name != remote_record.zip_name
        hash_changed = local_hash != remote_hash and (local_hash or remote_hash)
        if not (archive_changed or hash_changed):
            continue
        local_patch = (local_record.patch or "").strip()
        remote_patch = (remote_record.patch or "").strip()
        if remote_patch and local_patch != remote_patch:
            detail = tr("{old} → {new}", old=local_patch or "?", new=remote_patch)
        elif archive_changed:
            detail = tr(
                "{old} → {new}",
                old=local_record.zip_name or "?",
                new=remote_record.zip_name or "?",
            )
        else:
            detail = tr("Archive updated")
        tooltip = tr(
            "MD5: {old} → {new}",
            old=local_hash or "(none)",
            new=remote_hash or "(none)",
        )
        diffs.append(
            UpdateDiff("Modified", local_record.folder_name, detail, tooltip)
        )
    return diffs


def check_updates(profile) -> UpdateStatus:
    """Check the active profile for GAMMA updates without the GitHub API."""
    status = UpdateStatus()
    gamma_dir = getattr(profile, "gamma", None) or ""
    mo2_profile = getattr(profile, "mo2_profile", "") or "G.A.M.M.A"

    if not gamma_dir or not Path(gamma_dir).is_dir():
        status.error = tr("GAMMA is not installed yet. Run a full install first.")
        return status

    status.installed = installed_version(gamma_dir)

    local = local_modpack_records(gamma_dir, mo2_profile)
    if local is None:
        status.error = tr(
            "No modpack list found in this profile. Run a full install so the "
            "installer can generate the installed-addon list."
        )
        return status

    remote: dict[str, ModPackRecord] = {}
    mod_pack_url = getattr(profile, "mod_pack_maker_url", "") or ""
    list_failed = False
    if mod_pack_url:
        try:
            req = urllib.request.Request(
                mod_pack_url, headers={"User-Agent": USER_AGENT}
            )
            with urlopen(req, timeout=REMOTE_TIMEOUT) as resp:
                remote = parse_modpack_records(
                    read_response_bytes(resp, _MAX_MODPACK_BYTES).decode(
                        "utf-8", errors="replace"
                    )
                )
        except (OSError, ValueError, UnicodeError) as exc:
            status.error = tr("Could not reach the official mod list: {exc}", exc=exc)
            list_failed = True
    else:
        status.error = tr("The profile has no modpack maker URL configured.")
        list_failed = True

    # If the remote list is empty but local addons exist, treat as a fetch
    # failure rather than reporting every local addon as "Removed".
    if not list_failed and not remote and local:
        status.error = tr(
            "The remote mod list is empty; this usually means the fetch "
            "returned an error page. Try again later."
        )
        list_failed = True

    # The version marker is served independently of the addon list -- still
    # fetch it so a list outage does not hide an available update.
    status.latest = remote_version(profile)
    # One fetch shared between the human version label and the "What's
    # New" panel - latest_version_human()'s own README fallback is only
    # used if this single Patchnotes.md fetch didn't yield a match.
    status.patchnotes = fetch_latest_patchnotes(profile)
    status.latest_human = _version_from_text(status.patchnotes) or latest_version_human(
        profile
    )
    # The human label is only reliable for the latest release; an installed
    # build that is not current keeps its bare build number instead.
    if status.installed and status.latest and status.installed == status.latest:
        status.installed_human = status.latest_human
    if not list_failed:
        status.diffs = diff_records(local, remote)
    if not status.error and not status.latest and not status.diffs:
        status.error = tr(
            "Could not fetch the latest GAMMA version marker; the addon list "
            "itself is up to date."
        )
    return status
