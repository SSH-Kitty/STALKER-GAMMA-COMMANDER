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
        # The version marker is fetched even when the addon list fails, so
        # a newer version is an update whatever went wrong with the list -
        # checking the error first hid it exactly when the list was down.
        if self.latest and self.installed and self.latest != self.installed:
            return True
        if self.error:
            return False
        return bool(self.diffs)


def format_version(
    build: str | None,
    human: str | None,
    missing: str = "Not installed",
    *,
    show_build: bool = True,
) -> str:
    """Render a version as ``"0.9.5 (build 920)"``.

    The build number is kept by default because the human label is coarse and
    only known for the latest release; an outdated install falls back to the
    bare build number (``"build 910"``). ``show_build=False`` drops it where a
    label is known (``"0.9.5"``) - the fallback still shows the build.
    """
    if not build:
        return missing
    if human:
        return f"{human} (build {build})" if show_build else human
    return f"build {build}"


def status_summary(status: UpdateStatus) -> tuple[str, str]:
    """Return (status text, QSS objectName) for a check result.

    ``objectName`` is one of ``accent`` (green), ``warn`` (amber) or ``dim``.
    """
    if status.error and not status.update_available:
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
        if status.error:
            # The version is newer, but the per-addon list couldn't be read.
            text += "\n" + status.error
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
    # A clone URL ("…/Stalker_GAMMA.git") names the same repo, but
    # raw.githubusercontent.com 404s on the ".git" suffix.
    repo = repo.removesuffix(".git") or "Stalker_GAMMA"
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
    return _version_from_patchnotes_or_readme(profile, fetch_latest_patchnotes(profile))


def _version_from_patchnotes_or_readme(profile, patchnotes: str | None) -> str | None:
    """The version from already-fetched patch notes, else from the README.

    Takes the patch notes as fetched by the caller, so a check that already
    has them (or already failed to get them) doesn't download them again -
    offline, the repeat fetch alone added a full timeout-and-retry cycle.
    """
    version = _version_from_text(patchnotes)
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
#: Release tags look like "v1.2.9" or "v1.2.9H2" - the trailing "H<n>" is a
#: hotfix counter on top of the same numeric version, not a separate
#: release. Without capturing it, "1.2.9H1" and "1.2.9H2" both reduced to
#: the same (1, 2, 9) tuple and compared as equal, so a hotfix release
#: never looked newer than the one before it.
#:
#: Unstable builds (GitHub pre-releases) add "-unstable" and an optional
#: counter: "1.3.1-unstable", "1.3.1-unstable2", ...
_VERSION_NUMERIC_RE = re.compile(
    r"(?P<base>[0-9]+(?:\.[0-9]+)*)(?:[Hh](?P<hotfix>[0-9]+))?"
    r"(?:-unstable(?P<unstable>[0-9]*))?",
    re.IGNORECASE,
)


def _numeric_version_tuple(text: str) -> tuple[int, ...] | None:
    match = _VERSION_NUMERIC_RE.search(text)
    if not match:
        return None
    base = tuple(int(part) for part in match.group("base").split("."))
    # Padded to major.minor.patch before the hotfix is appended: otherwise
    # "v1.3H5" -> (1, 3, 5) outranked "1.3.2" -> (1, 3, 2, 0), and "1.3"
    # vs "1.3.0" compared unequal.
    base = (base + (0, 0, 0))[:3] if len(base) < 3 else base
    # Then (stage, n): a stable build is (1, 0) and "-unstableN" is (0, N),
    # so 1.3.1-unstable < 1.3.1-unstable2 < 1.3.1 < 1.3.1H1.
    unstable = match.group("unstable")
    stage = (1, 0) if unstable is None else (0, int(unstable or 1))
    return base + (int(match.group("hotfix") or 0),) + stage


def is_unstable_version(text: str) -> bool:
    """True for an unstable build's version or tag ("1.3.1-unstable2")."""
    version = _numeric_version_tuple(text or "")
    return version is not None and version[-2] == 0


def effective_update_channel(saved: str | None, current_version: str) -> str:
    """The update channel to check: always "unstable" on an unstable build.

    Someone who installed an unstable AppImage by hand, with the setting
    still on "stable", would otherwise never hear about newer unstable
    builds - or about the stable release that supersedes theirs.
    """
    if is_unstable_version(current_version):
        return "unstable"
    return saved if saved in UPDATE_CHANNELS else "stable"


#: Update channels for COMMANDER itself (gui setting ``update_channel``).
UPDATE_CHANNELS = ("stable", "unstable")
#: The release feed can list many entries; it is only ever a few KB.
_MAX_FEED_BYTES = 1_000_000
_FEED_TAG_RE = re.compile(r"/releases/tag/([^\"'<>\s]+)")


def _latest_stable_tag() -> str | None:
    """The tag GitHub marks as "latest" (never a pre-release)."""
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
    return urllib.parse.unquote(match.group(1)) if match else None


def _feed_tags() -> list[str]:
    """Every release tag in the repository's Atom feed, pre-releases included.

    ``/releases.atom`` is a plain page like ``/releases/latest``, not the
    rate-limited REST API, and - unlike "latest" - it lists pre-releases.
    """
    try:
        req = urllib.request.Request(
            f"{_COMMANDER_REPO}/releases.atom", headers={"User-Agent": USER_AGENT}
        )
        with urlopen(req, timeout=REMOTE_TIMEOUT) as resp:
            text = read_response_bytes(resp, _MAX_FEED_BYTES).decode("utf-8", "replace")
    except (OSError, ValueError):
        return []
    return [urllib.parse.unquote(tag) for tag in _FEED_TAG_RE.findall(text)]


_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def fetch_latest_release_notes(channel: str = "stable") -> tuple[str, str] | None:
    """The newest COMMANDER release's title and patch notes (HTML).

    Read from the same ``/releases.atom`` feed as :func:`_feed_tags` - each
    entry carries the release body already rendered to HTML. "stable" picks
    the entry GitHub marks as latest (never a pre-release), "unstable" the
    newest entry of all; either falls back to the first entry. None when
    the feed can't be fetched or parsed.
    """
    from . import safe_xml

    try:
        req = urllib.request.Request(
            f"{_COMMANDER_REPO}/releases.atom", headers={"User-Agent": USER_AGENT}
        )
        with urlopen(req, timeout=REMOTE_TIMEOUT) as resp:
            data = read_response_bytes(resp, _MAX_FEED_BYTES)
        # Remote XML: parsed without entity expansion (see safe_xml).
        entries = safe_xml.parse_bytes(data, namespaces=True).findall(f"{_ATOM_NS}entry")
    except (ValueError, *safe_xml.XML_ERRORS):
        return None
    if not entries:
        return None
    chosen = entries[0]
    if channel != "unstable":
        stable = _latest_stable_tag()
        for entry in entries:
            link = entry.find(f"{_ATOM_NS}link")
            href = link.get("href", "") if link is not None else ""
            if stable and urllib.parse.unquote(href).rstrip("/").endswith(f"/{stable}"):
                chosen = entry
                break
    title = (chosen.findtext(f"{_ATOM_NS}title") or "").strip()
    body = (chosen.findtext(f"{_ATOM_NS}content") or "").strip()
    return title, body


def latest_stable_tag() -> str | None:
    """The tag of the release GitHub marks as latest (never a pre-release)."""
    return _latest_stable_tag()


def latest_unstable_tag() -> str | None:
    """The newest "-unstable" pre-release tag in the release feed, or None."""
    best: tuple[tuple[int, ...], str] | None = None
    for tag in _feed_tags():
        if not is_unstable_version(tag):
            continue
        version = _numeric_version_tuple(tag)
        if version is not None and (best is None or version > best[0]):
            best = (version, tag)
    return best[1] if best else None


def newer_unstable_tag(current_version: str) -> str | None:
    """The unstable build worth switching to, or None.

    Only one that is newer than both the running build and the latest
    stable release counts: an unstable 1.3.1-unstable2 is offered over
    1.3.0, but never once stable 1.3.1 is out.
    """
    tag = latest_unstable_tag()
    if tag is None:
        return None
    version = _numeric_version_tuple(tag)
    floor = [_numeric_version_tuple(current_version)]
    stable = _latest_stable_tag()
    if stable:
        floor.append(_numeric_version_tuple(stable))
    if version is None or any(v is not None and version <= v for v in floor):
        return None
    return tag


def check_commander_update(current_version: str, channel: str = "stable") -> str | None:
    """Return the newer COMMANDER release tag, or None if up to date/unreachable.

    ``channel`` "stable" (the default) only ever looks at the release GitHub
    marks as latest, which is never a pre-release - stable users are not
    moved onto an unstable build. "unstable" also considers pre-releases
    and offers the highest version of all of them, falling back to the
    stable check when the feed can't be read.

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
    candidates: list[str] = []
    if channel == "unstable":
        candidates = _feed_tags()
    if not candidates:
        tag = _latest_stable_tag()
        candidates = [tag] if tag else []
    best: tuple[tuple[int, ...], str] | None = None
    for tag in candidates:
        version = _numeric_version_tuple(tag)
        if version is not None and (best is None or version > best[0]):
            best = (version, tag)
    if best is None or best[0] <= current:
        return None
    return best[1]


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


def _json_text(entry: dict, key: str) -> str:
    """``entry[key]`` as stripped text; a number/list/null is not text.

    A non-string value (a hand-edited or newer-format list) used to raise
    AttributeError on ``.strip()``, which nothing here caught.
    """
    value = entry.get(key)
    return value.strip() if isinstance(value, str) else ""


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
                addon = _json_text(entry, "addonName")
                if not addon:
                    continue
                record = ModPackRecord(
                    counter=counter,
                    addon_name=addon,
                    patch=_json_text(entry, "patch"),
                    dl_link=_json_text(entry, "dlLink"),
                    mod_db_url=_json_text(entry, "modDbUrl"),
                    zip_name=_json_text(entry, "zipName"),
                    md5_mod_db=_json_text(entry, "md5ModDb"),
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
    status.latest_human = _version_from_patchnotes_or_readme(profile, status.patchnotes)
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
