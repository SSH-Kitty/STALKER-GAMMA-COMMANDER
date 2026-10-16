"""Analyzer for COMMANDER's launcher.log (Wine/MO2 launch output)."""

from __future__ import annotations

import re

from commander_gui.i18n import tr

from .. import knowledge
from ..findings import CATEGORY_LAUNCHER, Finding, Severity
from .common import FindingFactory, excerpt

_PREFIX_MARKERS = (
    "wine client error",
    "version mismatch",
    "wrong wineserver",
    "prefix has an invalid version",
    "pfx.lock",
)
_GRAPHICS_MARKERS = (
    "setcolorspace1",
    "dxgi_color_space",
    "wined3d_swapchain",
    "d3d11_swapchain_setcolorspace",
)
_UNIMPLEMENTED_RE = re.compile(
    r"wine: Call from \S+ to unimplemented function (\S+), aborting"
)
_EXIT_RE = re.compile(r"exited with an error \(code (-?\d+)\)")
_GAMEMODE_DLOPEN_RE = re.compile(r"gamemodeauto: dlopen failed - (lib[\w.]+)")
_LD_PRELOAD_GAMEMODE_RE = re.compile(
    r"ld\.so: object '(libgamemode[\w.]*)' from LD_PRELOAD cannot be pre?loaded"
)
_PRESSURE_VESSEL_RE = re.compile(r"ld\.so: object '/tmp/pressure-vessel-libs-[^'/]+/")
#: pressure-vessel's own error lines ("pressure-vessel-wrap[12]: E: Failed
#: to run bwrap: ...") mean the container, and so the game, never started.
_PRESSURE_VESSEL_ERROR_RE = re.compile(r": E: |\berror\b|\bfailed\b", re.IGNORECASE)
_TOOLMANIFEST_RE = re.compile(r"toolmanifest\.vdf not found", re.IGNORECASE)
_QTPDF_RE = re.compile(r"qtpdf\.dll", re.IGNORECASE)
_GENERIC_ERROR_RE = re.compile(r"^ERROR:\s*(.+)$")
_PROTONFIXES_RE = re.compile(r"ProtonFixes\[\d+\]\s+(WARN|ERROR|INFO):\s*(.*)")


def analyze_launcher(arcname: str, where: str, lines: list[str]) -> list[Finding]:
    """Detect runner/prefix, graphics, dependency, and exit-code problems."""
    factory = FindingFactory(arcname, where)
    findings: list[Finding] = []
    protonfixes_count = 0
    protonfixes_first_line: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        lower = stripped.lower()
        # ProtonFixes first: its routine warnings can embed words like
        # "version mismatch" that must not be misfiled as prefix errors.
        protonfixes = _PROTONFIXES_RE.search(stripped)
        if protonfixes:
            level, message = protonfixes.group(1), protonfixes.group(2).strip()
            if level == "ERROR":
                findings.append(
                    _protonfixes_error_finding(factory, lines, index, message)
                )
            else:
                protonfixes_count += 1
                if protonfixes_first_line is None:
                    protonfixes_first_line = index + 1
            continue
        if any(marker in lower for marker in _PREFIX_MARKERS):
            findings.append(_prefix_finding(factory, lines, index, stripped))
            continue
        if "concrt140" in lower:
            findings.append(_concrt_finding(factory, lines, index))
            continue
        if any(marker in lower for marker in _GRAPHICS_MARKERS):
            findings.append(_graphics_finding(factory, lines, index))
            continue
        unimplemented = _UNIMPLEMENTED_RE.search(stripped)
        if unimplemented:
            findings.append(
                _unimplemented_finding(factory, lines, index, unimplemented.group(1))
            )
            continue
        exit_match = _EXIT_RE.search(stripped)
        if exit_match:
            title, detail = knowledge.launch_exit_code(exit_match.group(1))
            findings.append(
                factory.make(
                    Severity.ERROR,
                    CATEGORY_LAUNCHER,
                    title,
                    index + 1,
                    detail=detail,
                    suggestion=tr(knowledge.LAUNCH_EXITED),
                    excerpt_text=excerpt(lines, index),
                )
            )
            continue
        if "pressure-vessel" in lower and not _PRESSURE_VESSEL_RE.search(stripped) and (
            _PRESSURE_VESSEL_ERROR_RE.search(stripped)
        ):
            message = stripped[:200]
            findings.append(
                factory.make(
                    Severity.ERROR,
                    CATEGORY_LAUNCHER,
                    tr("The Steam Linux Runtime container reported an error: {message}", message=message),
                    index + 1,
                    detail=tr(
                        "Steam's container (pressure-vessel) failed while starting, "
                        "so the game may not have started at all."
                    ),
                    suggestion=tr(
                        "If the game starts and plays normally you can ignore "
                        "this. If it does not, include this log when asking for help."
                    ),
                    excerpt_text=excerpt(lines, index),
                    dedup_key=message,
                )
            )
            continue
        if _PRESSURE_VESSEL_RE.search(stripped) or "pressure-vessel" in lower:
            # Steam Linux Runtime loader noise; identical titles let the
            # central collapser merge every launch's copies into one entry.
            findings.append(_pressure_vessel_finding(factory, index + 1))
            continue
        if _GAMEMODE_DLOPEN_RE.search(stripped) or _LD_PRELOAD_GAMEMODE_RE.search(
            stripped
        ):
            findings.append(_gamemode_finding(factory, lines, index))
            continue
        if _TOOLMANIFEST_RE.search(stripped):
            findings.append(_toolmanifest_finding(factory, lines, index))
            continue
        if _QTPDF_RE.search(lower):
            findings.append(_qtpdf_finding(factory, lines, index))
            continue
        generic_error = _GENERIC_ERROR_RE.match(stripped)
        if generic_error:
            message = generic_error.group(1)[:130]
            if "ld.so" in message.lower():
                # Unrecognised dynamic-loader noise from the Steam container.
                findings.append(_pressure_vessel_finding(factory, index + 1))
            else:
                findings.append(
                    factory.make(
                        Severity.WARNING,
                        CATEGORY_LAUNCHER,
                        tr("The launcher reported an error: {message}", message=message),
                        index + 1,
                        detail=tr(
                            "An ERROR-prefixed line appeared in the launch output; "
                            "its impact is not known from this line alone."
                        ),
                        suggestion=tr(
                            "If the game starts and plays normally you can ignore "
                            "this. If it does not, include this log when asking for help."
                        ),
                        excerpt_text=excerpt(lines, index),
                        # The title is cut at 130 characters: two different
                        # errors must not merge because their starts match.
                        dedup_key=generic_error.group(1),
                    )
                )
    if protonfixes_count:
        findings.append(
            factory.make(
                Severity.OK,
                CATEGORY_LAUNCHER,
                tr("ProtonFixes printed 1 routine message (normal).")
                if protonfixes_count == 1
                else tr(
                    "ProtonFixes printed {count} routine messages (normal).",
                    count=protonfixes_count,
                ),
                protonfixes_first_line,
                detail=tr(knowledge.PROTONFIXES_SUMMARY),
                suggestion=tr("Nothing to do. This is normal."),
            )
        )
    return findings


def _prefix_finding(
    factory: FindingFactory, lines: list[str], index: int, stripped: str
) -> Finding:
    return factory.make(
        Severity.ERROR,
        CATEGORY_LAUNCHER,
        tr("The Wine prefix was made by a different Proton/Wine version."),
        index + 1,
        detail=tr("Wine refused to start with this prefix: {line}", line=stripped[:160]),
        suggestion=tr(knowledge.PREFIX_MISMATCH),
        excerpt_text=excerpt(lines, index),
    )


def _unimplemented_finding(
    factory: FindingFactory, lines: list[str], index: int, function: str
) -> Finding:
    # Fixed title so every aborting call merges into one entry.
    return factory.make(
        Severity.ERROR,
        CATEGORY_LAUNCHER,
        tr("The selected Proton build is too old for this prefix."),
        index + 1,
        detail=tr(
            "Wine aborted on a call to {function}, which this Proton build "
            "does not provide.",
            function=function,
        ),
        suggestion=tr(knowledge.PROTON_TOO_OLD),
        excerpt_text=excerpt(lines, index),
    )


def _concrt_finding(factory: FindingFactory, lines: list[str], index: int) -> Finding:
    return factory.make(
        Severity.ERROR,
        CATEGORY_LAUNCHER,
        tr("Mod Organizer is missing a Microsoft C++ runtime file (concrt140.dll)."),
        index + 1,
        detail=tr(
            "Without this runtime Mod Organizer exits immediately "
            "on many Wine/Proton versions."
        ),
        suggestion=tr(knowledge.CONCRT140),
        excerpt_text=excerpt(lines, index),
    )


def _graphics_finding(factory: FindingFactory, lines: list[str], index: int) -> Finding:
    return factory.make(
        Severity.WARNING,
        CATEGORY_LAUNCHER,
        tr("The game is rendering through WineD3D instead of DXVK."),
        index + 1,
        detail=tr(
            "This fallback works but performs noticeably worse "
            "than the Vulkan-based renderer."
        ),
        suggestion=tr(knowledge.GRAPHICS_DXVK),
        excerpt_text=excerpt(lines, index),
    )


def _pressure_vessel_finding(factory: FindingFactory, line_no: int) -> Finding:
    return factory.make(
        Severity.OK,
        CATEGORY_LAUNCHER,
        tr(knowledge.PRESSURE_VESSEL_TITLE),
        line_no,
        detail=tr(knowledge.PRESSURE_VESSEL),
        suggestion=tr("Nothing to do. This is normal."),
    )


def _gamemode_finding(factory: FindingFactory, lines: list[str], index: int) -> Finding:
    """One shared shape so dlopen and LD_PRELOAD variants merge together."""
    return factory.make(
        Severity.INFO,
        CATEGORY_LAUNCHER,
        tr(knowledge.GAMEMODE_TITLE),
        index + 1,
        detail=tr(
            "A launch attempted to enable GameMode, an optional performance "
            "booster, but its library is not installed on this system."
        ),
        suggestion=tr(knowledge.GAMEMODE_OPTIONAL),
        excerpt_text=excerpt(lines, index),
    )


def _toolmanifest_finding(
    factory: FindingFactory, lines: list[str], index: int
) -> Finding:
    return factory.make(
        Severity.WARNING,
        CATEGORY_LAUNCHER,
        tr("A Proton runtime file is missing (toolmanifest.vdf)."),
        index + 1,
        detail=tr(
            "Some Proton runtime metadata could not be read; the "
            "launch may still work."
        ),
        suggestion=tr(knowledge.TOOLMANIFEST_MISSING),
        excerpt_text=excerpt(lines, index),
    )


def _qtpdf_finding(factory: FindingFactory, lines: list[str], index: int) -> Finding:
    return factory.make(
        Severity.OK,
        CATEGORY_LAUNCHER,
        tr("MO2 printed a non-critical QtPdf plugin warning."),
        index + 1,
        detail=tr(knowledge.QTPDF_HARMLESS),
        suggestion=tr("Nothing to do. This is normal."),
        excerpt_text=excerpt(lines, index),
    )


def _protonfixes_error_finding(
    factory: FindingFactory, lines: list[str], index: int, message: str
) -> Finding:
    return factory.make(
        Severity.WARNING,
        CATEGORY_LAUNCHER,
        tr("ProtonFixes error: {message}", message=message[:120]),
        index + 1,
        detail=tr(
            "ProtonFixes failed while preparing the runtime environment "
            "for this launch."
        ),
        suggestion=tr(
            "If the game fails to start, try a different GE-Proton/UMU-Proton "
            "build on COMMANDER's Play page."
        ),
        excerpt_text=excerpt(lines, index),
        dedup_key=message,
    )
