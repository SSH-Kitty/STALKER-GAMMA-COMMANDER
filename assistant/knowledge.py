"""Knowledge base of plain-language explanations and suggested fixes.

The wording is aligned with GAMMA COMMANDER's own troubleshooting guidance
(Help page and Play-page diagnostics) so the Assistant gives the same
advice the main app would.
"""

# --- Launcher and Wine -------------------------------------------------------

CONCRT140 = (
    "MO2 needs the Microsoft C++ runtime (concrt140.dll), which some Wine/Proton "
    "builds do not provide. In COMMANDER, go to Install → Install Dependencies and "
    "run it against your active prefix, or switch to GE-Proton on COMMANDER's Play page."
)

PREFIX_MISMATCH = (
    "This Wine prefix was created by a different runner. Close MO2 and the game, "
    "then either select the original runner again on COMMANDER's Play page or configure a "
    "separate prefix for the new runner. Do not switch runners while a prefix is "
    "in use."
)

GRAPHICS_DXVK = (
    "The renderer fell back to WineD3D (slower than DXVK/Vulkan). Install the "
    "Vulkan driver for your GPU (e.g. mesa-vulkan-drivers or the NVIDIA driver), "
    "then remove PROTON_USE_WINED3D=1 from Custom Launch Options if present."
)

LAUNCH_EXITED = (
    "Check the selected target, runner, and prefix on COMMANDER's Play page. The "
    "Play page shows the exit code and recent launcher output; System Check verifies tools."
)

GAMEMODE_OPTIONAL = (
    "GameMode is an optional performance booster. Install your distro's gamemode "
    "package to silence this, or leave it installed as-is; it does not affect the game."
)

GAMEMODE_TITLE = "GameMode is not installed (optional performance booster)."

PRESSURE_VESSEL = (
    "The Steam Linux Runtime container prints these loader warnings on many "
    "systems. They are cosmetic and do not affect the game; no action is required."
)

PRESSURE_VESSEL_TITLE = (
    "Steam Linux Runtime printed non-critical loader warnings (pressure-vessel)."
)

PROTONFIXES_SUMMARY = (
    "ProtonFixes prepares the Proton environment before each "
    "launch. Its routine messages were collapsed here; only actionable problems "
    "are shown separately."
)

TOOLMANIFEST_MISSING = (
    "Part of the Proton/UMU runtime files are missing. Reinstalling umu-run "
    "(COMMANDER can do this from Install → Install Dependencies), or let Steam "
    "recreate the compatibility data."
)

QTPDF_HARMLESS = (
    "This is a cosmetic warning from Mod Organizer's image plugins. It is not "
    "the cause of any crash; no action is required for this warning."
)

PROTONFIXES_NOTE = (
    "ProtonFixes is adjusting the runtime. These messages are "
    "informational; only act on them if the game fails to start."
)


def launch_exit_code(code: str) -> tuple[str, str]:
    """Title/detail pair for a non-zero exit code line."""
    return (
        f"The game or Mod Organizer exited with an error (code {code}).",
        (
            "A non-zero exit code means the program closed abnormally. The lines "
            "around this entry in launcher.log may show the underlying cause."
        ),
    )


# --- XRay engine -------------------------------------------------------------

XRAY_FATAL_GENERIC = (
    "The game engine encountered an unrecoverable problem and shut down.",
    (
        "XRay can stop when something in the game data prevents startup or play. The "
        "[error] block names the subsystem that failed."
    ),
)


def xray_missing_section(section: str) -> tuple[str, str, str]:
    """(title, detail, suggestion) for a missing config section crash."""
    return (
        f"The game crashed reading a config section that does not exist ('{section}').",
        (
            "This is a common symptom of a mod conflict: two mods edit the same "
            "settings file and one removed or renamed a section another mod expects."
        ),
        (
            "Recently added or updated mods are a common cause. Disable suspect "
            "mods in COMMANDER's Mod Manager (or MO2), then run Verify Integrity on "
            "COMMANDER's Install page; it can repair broken modpack files."
        ),
    )


# --- CLI installer -----------------------------------------------------------


def cli_failed(reason_kind: str, reason: str) -> tuple[str, str, str]:
    """(title, detail, suggestion) per failure kind."""
    if reason_kind == "canceled":
        return (
            "An install was cancelled before it finished.",
            (
                "The operation received a cancel request (or lost its connection) "
                "and stopped cleanly."
            ),
            (
                "This does not indicate a problem. Run the install again when ready; "
                "already downloaded mods are kept and reused."
            ),
        )
    if reason_kind == "download":
        return (
            f"A download failed: {reason}",
            (
                "The installer could not fetch a file from ModDB or GitHub. This is "
                "often a temporary network issue or a busy mirror."
            ),
            (
                "Run the install again — completed downloads are cached and reused. "
                "If it keeps failing, try later or check your connection/VPN."
            ),
        )
    if reason_kind == "integrity":
        return (
            f"A file failed verification: {reason}",
            (
                "The MD5 check found a mod archive or installed file that does not "
                "match the official checksums."
            ),
            (
                "Use Verify Integrity on COMMANDER's Install page. It can repair "
                "corrupted or missing files by re-downloading them."
            ),
        )
    if reason_kind == "storage":
        return (
            f"Installation failed while writing files: {reason}",
            (
                "Extraction or file operations failed, often due to low disk space "
                "or permission problems."
            ),
            (
                "Free up space on the install drive (GAMMA needs ~150 GB) and make "
                "sure the folders are writable, then run the install again."
            ),
        )
    return (
        f"The installation failed: {reason}",
        "The stalker-gamma CLI reported a failure while installing.",
        (
            "Read the technical details below for the root cause, then retry the "
            "install. Verify Integrity can repair partial installs."
        ),
    )


DEPENDENCY_MISSING = (
    "Install the missing tool with your package manager. COMMANDER's System "
    "Check page shows the exact command for your distribution; then continue "
    "the installation."
)

NO_ACTIVE_PROFILE = (
    "Create and activate a profile on COMMANDER's Profiles page before "
    "installing or updating."
)


# --- MO2 ---------------------------------------------------------------------

USVFS_OK = (
    "Mod Organizer's virtual file system started normally. Mods are being "
    "presented to the game without touching the real folders."
)

BENIGN_MO2_WARNINGS = {
    "not saving lists during directory update",
}


# --- Winetricks / runtimes ---------------------------------------------------

WINETRICKS_MISSING = (
    "Some expected runtimes are not recorded as installed in this prefix. "
    "In COMMANDER, use Install → Install Dependencies to install them into "
    "the active Wine/Proton prefix."
)

WINETRICKS_ALL_GOOD = (
    "All runtimes COMMANDER installs were recorded in winetricks history for "
    "this prefix. No action is required here."
)

VCREDIST_REBOOT = (
    "The Visual C++ installer finished successfully but asks for a Windows "
    "reboot inside the prefix. Closing and relaunching the game (or restarting "
    "the prefix session) satisfies this; no action is required."
)

VCREDIST_DUPLICATE = (
    "A different version of this runtime is already installed in the prefix. "
    "This is expected when runtimes were installed previously; no action is required."
)

VCREDIST_FAILED = (
    "Re-run COMMANDER's Install Dependencies step with MO2 and the game "
    "closed. If it keeps failing, the technical details below show which "
    "component refused to install."
)
