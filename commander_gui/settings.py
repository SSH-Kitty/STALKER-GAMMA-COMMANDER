"""Read/write the stalker-gamma settings.json file.

The CLI serializes settings with the System.Text.Json source generator using
PascalCase property names. We mirror that layout exactly so the GUI and CLI can
safely share the same file.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .atomic import write_text
from .config import cli_binary_path, settings_path

DEFAULT_MOD_PACK_MAKER_URL = "https://stalker-gamma.com/api/client/v1/mods/list"
DEFAULT_MOD_LIST_URL = (
    "https://raw.githubusercontent.com/Grokitach/Stalker_GAMMA/refs/heads/main/"
    "G.A.M.M.A/modpack_data/modlist.txt"
)


@dataclass
class CliProfile:
    active: bool = False
    profile_name: str = "Gamma"
    anomaly: str = "gamma/anomaly"
    gamma: str = "gamma/gamma"
    cache: str = "gamma/cache"
    mo2_profile: str = "G.A.M.M.A"
    download_threads: int = 2
    mod_pack_maker_url: str = DEFAULT_MOD_PACK_MAKER_URL
    mod_list_url: str = DEFAULT_MOD_LIST_URL
    gamma_setup_repo_url: str = "https://github.com/Grokitach/gamma_setup"
    gamma_setup_repo_branch: str = "main"
    stalker_gamma_repo_url: str = "https://github.com/Grokitach/Stalker_GAMMA"
    stalker_gamma_repo_branch: str = "main"
    gamma_large_files_repo_url: str = (
        "https://github.com/Grokitach/gamma_large_files_v2"
    )
    gamma_large_files_repo_branch: str = "main"
    teivaz_anomaly_gunslinger_repo_url: str = (
        "https://github.com/Grokitach/teivaz_anomaly_gunslinger"
    )
    teivaz_anomaly_gunslinger_repo_branch: str = "main"
    #: Any JSON keys this GUI does not know about, kept verbatim so a newer
    #: CLI's settings survive a round-trip through the GUI.
    extra: dict = field(default_factory=dict)

    _JSON_KEYS: ClassVar[dict[str, str]] = {
        "active": "Active",
        "profile_name": "ProfileName",
        "anomaly": "Anomaly",
        "gamma": "Gamma",
        "cache": "Cache",
        "mo2_profile": "Mo2Profile",
        "download_threads": "DownloadThreads",
        "mod_pack_maker_url": "ModPackMakerUrl",
        "mod_list_url": "ModListUrl",
        "gamma_setup_repo_url": "GammaSetupRepoUrl",
        "gamma_setup_repo_branch": "GammaSetupRepoBranch",
        "stalker_gamma_repo_url": "StalkerGammaRepoUrl",
        "stalker_gamma_repo_branch": "StalkerGammaRepoBranch",
        "gamma_large_files_repo_url": "GammaLargeFilesRepoUrl",
        "gamma_large_files_repo_branch": "GammaLargeFilesRepoBranch",
        "teivaz_anomaly_gunslinger_repo_url": "TeivazAnomalyGunslingerRepoUrl",
        "teivaz_anomaly_gunslinger_repo_branch": "TeivazAnomalyGunslingerRepoBranch",
    }

    def to_dict(self) -> dict:
        out = dict(self.extra)
        for python_key, json_key in self._JSON_KEYS.items():
            value = getattr(self, python_key)
            if python_key == "active":
                # Must serialize as a real JSON bool: the CLI deserializes this
                # into a C# bool and throws on anything else.
                value = bool(value)
            elif python_key == "download_threads":
                value = int(value)
            out[json_key] = value
        return out

    @classmethod
    def from_dict(cls, data: dict) -> CliProfile:
        profile = cls()
        for python_key, json_key in cls._JSON_KEYS.items():
            if json_key not in data:
                continue
            value = data[json_key]
            if python_key == "download_threads":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            elif python_key == "active":
                if isinstance(value, bool):
                    pass
                elif isinstance(value, str) and value.strip().lower() in {
                    "true",
                    "false",
                }:
                    value = value.strip().lower() == "true"
                else:
                    continue
            elif not isinstance(value, str):
                continue
            setattr(profile, python_key, value)
        known = set(cls._JSON_KEYS.values())
        profile.extra = {k: v for k, v in data.items() if k not in known}
        return profile


@dataclass
class CliSettings:
    profiles: list[CliProfile] = field(default_factory=list)
    #: Top-level keys owned by the CLI that the GUI does not model. Preserved
    #: verbatim so saving from the GUI never drops CLI-only settings.
    extra: dict = field(default_factory=dict)

    @property
    def active_profile(self) -> CliProfile | None:
        for profile in self.profiles:
            if profile.active:
                return profile
        return None

    def save(self, path: Path | None = None) -> None:
        """Write settings.json atomically, preserving unknown keys."""
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(self.extra)
        data["Profiles"] = [p.to_dict() for p in self.profiles]
        write_text(path, json.dumps(data, indent=2) + "\n")


def _reset_settings(path: Path, backup: Path) -> CliSettings:
    try:
        path.replace(backup)
    except OSError:
        pass
    settings = CliSettings()
    settings.profiles = [CliProfile(active=True)]
    settings.save(path)
    return settings


def load_settings(path: Path | None = None) -> CliSettings:
    path = path or settings_path()
    if not path.exists():
        settings = CliSettings()
        default = CliProfile(active=True)
        settings.profiles = [default]
        settings.save(path)
        return settings
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Back up corrupt file and start fresh.
        return _reset_settings(path, path.with_name(path.name + ".corrupt"))
    if not isinstance(data, dict):
        return _reset_settings(path, path.with_name(path.name + ".corrupt"))
    settings = CliSettings()
    settings.profiles = [
        CliProfile.from_dict(p) for p in data.get("Profiles", []) if isinstance(p, dict)
    ]
    settings.extra = {k: v for k, v in data.items() if k != "Profiles"}
    if not settings.profiles:
        default = CliProfile(active=True)
        settings.profiles = [default]
        settings.save(path)
    return settings


def run_config_command(args: list[str], timeout: int = 120) -> tuple[int, str, str]:
    """Run a CLI config command (create/use/delete) capturing output.

    These commands perform extra side effects (MO2 selected_profile + modlist
    download), so we shell out to the CLI instead of editing JSON by hand.
    """
    binary = str(cli_binary_path())
    cmd = [binary, "config", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        label = args[0] if args else "<no args>"
        return 124, "", f"'config {label}' timed out after {timeout}s"
    except OSError as exc:
        return 126, "", f"Failed to start {binary!r}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def cli_ok(
    rc: int,
    out: str,
    err: str,
    markers: tuple[str, ...] = (
        "error:",
        "not found",
        "already exists",
        "exception",
        "unhandled",
    ),
) -> bool:
    """Whether a CLI invocation succeeded.

    The bundled CLI exits 0 even on failures ("profile not found", unhandled
    exceptions), so the exit code alone is not reliable. A non-zero rc or any
    failure marker in the combined output counts as a failure.
    """
    if rc != 0:
        return False
    text = f"{out}\n{err}".lower()
    return not any(marker in text for marker in markers)
