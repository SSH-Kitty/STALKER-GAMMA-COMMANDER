"""Export/import a shareable bundle of a COMMANDER profile's portable data.

Install folder paths (Anomaly/GAMMA/Cache) are inherently machine-specific,
so they are deliberately left out of a bundle - "reinstall after a fresh
OS" or "share my setup with a friend" both need the importer to pick their
own folders, the same as creating any new profile. What does travel well:

* the profile's non-path settings (MO2 profile name, download-thread
  count, repo URLs/branches for forks/mirrors);
* the GAMMA ``modlist.txt`` (load order, enabled/disabled state,
  categories), if the exporting profile has one - this only means
  anything once GAMMA is actually installed on the importing side too,
  since it references mod folders that must already exist.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import zipfile
from dataclasses import fields
from pathlib import Path

from .modlist import modlist_path_for
from .settings import CliProfile

_MANIFEST_NAME = "profile.json"
_MODLIST_NAME = "modlist.txt"
_BUNDLE_FORMAT = 1

#: Deliberately excludes active/profile_name/anomaly/gamma/cache/extra -
#: see module docstring.
_PORTABLE_FIELDS = (
    "mo2_profile",
    "download_threads",
    "mod_pack_maker_url",
    "mod_list_url",
    "gamma_setup_repo_url",
    "gamma_setup_repo_branch",
    "stalker_gamma_repo_url",
    "stalker_gamma_repo_branch",
    "gamma_large_files_repo_url",
    "gamma_large_files_repo_branch",
    "teivaz_anomaly_gunslinger_repo_url",
    "teivaz_anomaly_gunslinger_repo_branch",
)


#: The fields that decide *where GAMMA is downloaded from*. A bundle that
#: changes them points the whole install at another source, so the import
#: shows them and asks instead of applying them silently.
SOURCE_FIELDS = tuple(name for name in _PORTABLE_FIELDS if name.endswith(("_url", "_branch")))

#: Upper bounds for the two members read out of a bundle (zip-bomb guard).
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_MODLIST_BYTES = 16 * 1024 * 1024


#: A git branch name as GAMMA's repos use them. Never starting with "-",
#: which a tool receiving it as an argument could read as an option.
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def _valid_source_url(value: str) -> bool:
    """An http(s) URL with a host - no file://, no bare paths."""
    parts = urllib.parse.urlsplit(value.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _valid_branch(value: str) -> bool:
    return bool(_BRANCH_RE.fullmatch(value.strip())) and ".." not in value


def _valid_source_value(name: str, value: object) -> bool:
    """A string, and for a download source, one shaped like a source."""
    if not isinstance(value, str):
        return False
    if name.endswith("_url"):
        return _valid_source_url(value)
    if name.endswith("_branch"):
        return _valid_branch(value)
    if name == "mo2_profile":
        # Becomes a folder under <gamma>/profiles and a CLI argument.
        return modlist_path_for("/", value) is not None and not value.startswith("-")
    return True


class ProfileBundleError(ValueError):
    """Raised when a bundle cannot be built or read."""


def non_default_sources(profile: CliProfile) -> list[tuple[str, str]]:
    """``(field, value)`` for every download source differing from the default."""
    default = CliProfile()
    return [
        (name, getattr(profile, name))
        for name in SOURCE_FIELDS
        if getattr(profile, name) != getattr(default, name)
    ]


def reset_sources(profile: CliProfile) -> None:
    """Put every download source back to the official default."""
    default = CliProfile()
    for name in SOURCE_FIELDS:
        setattr(profile, name, getattr(default, name))


def _read_member(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > limit:
        raise ProfileBundleError(f"{name} in this bundle is too large.")
    with zf.open(info) as member:
        data = member.read(limit + 1)
    if len(data) > limit:
        raise ProfileBundleError(f"{name} in this bundle is too large.")
    return data


def export_profile_bundle(profile: CliProfile, dest_path: Path) -> None:
    """Write *profile*'s portable settings (+ modlist.txt, if present) to a zip."""
    manifest = {
        "format": _BUNDLE_FORMAT,
        "settings": {name: getattr(profile, name) for name in _PORTABLE_FIELDS},
    }
    modlist_path = modlist_path_for(profile.gamma, profile.mo2_profile)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(
            dest_path, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False
        ) as zf:
            zf.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2))
            if modlist_path is not None and modlist_path.is_file():
                zf.write(modlist_path, _MODLIST_NAME)
    except OSError as exc:
        raise ProfileBundleError(f"Could not write bundle: {exc}") from exc


class ImportedProfileBundle:
    """Parsed bundle contents, ready to apply to a new/existing profile."""

    def __init__(self, settings: dict[str, object], modlist_text: str | None) -> None:
        self.settings = settings
        self.modlist_text = modlist_text

    def apply_to(self, profile: CliProfile) -> None:
        """Copy the bundle's portable settings onto *profile* in place.

        A hand-edited or version-skewed bundle can carry a malformed
        value (e.g. a non-numeric ``download_threads``) - validated the
        same way ``CliProfile.from_dict`` validates the identical fields
        loaded from settings.json, so a bad bundle is silently skipped
        per-field instead of setting an attribute that later crashes
        ``CliProfile.to_dict()``'s unguarded ``int(value)`` cast.
        """
        valid_names = {f.name for f in fields(CliProfile)}
        for name, value in self.settings.items():
            if name not in _PORTABLE_FIELDS or name not in valid_names:
                continue
            if name == "download_threads":
                try:
                    value = int(value)
                except (TypeError, ValueError, OverflowError):
                    # OverflowError: JSON's Infinity parses to float("inf").
                    continue
            elif not _valid_source_value(name, value):
                continue
            setattr(profile, name, value)


def read_profile_bundle(bundle_path: Path) -> ImportedProfileBundle:
    """Parse a bundle previously written by :func:`export_profile_bundle`."""
    try:
        with zipfile.ZipFile(bundle_path) as zf:
            try:
                manifest_raw = _read_member(zf, _MANIFEST_NAME, _MAX_MANIFEST_BYTES)
            except KeyError as exc:
                raise ProfileBundleError(
                    f"{bundle_path.name} is not a COMMANDER profile bundle."
                ) from exc
            modlist_text = None
            if _MODLIST_NAME in zf.namelist():
                modlist_text = _read_member(zf, _MODLIST_NAME, _MAX_MODLIST_BYTES).decode(
                    "utf-8", errors="replace"
                )
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProfileBundleError(f"Could not read {bundle_path.name}: {exc}") from exc
    try:
        manifest = json.loads(manifest_raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProfileBundleError(f"{bundle_path.name} is corrupted: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ProfileBundleError(f"{bundle_path.name} is corrupted: no settings found.")
    settings = manifest.get("settings")
    if not isinstance(settings, dict):
        raise ProfileBundleError(f"{bundle_path.name} is corrupted: no settings found.")
    return ImportedProfileBundle(settings, modlist_text)
