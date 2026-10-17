<div align="center">

<a><img alt="S.T.A.L.K.E.R. COMMANDER: an abandoned Pripyat apartment block under a stormy sky." src="docs/readme/banner.svg" width="100%"></a>

<br>

<a href="https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases"><img alt="Total downloads" src="https://img.shields.io/github/downloads/SSH-Kitty/STALKER-GAMMA-COMMANDER/total?label=downloads&style=for-the-badge&labelColor=141b15&color=5fb548"></a>
<a href="https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/SSH-Kitty/STALKER-GAMMA-COMMANDER?label=release&style=for-the-badge&labelColor=141b15&color=5fb548"></a>
<a href="#install"><img alt="Platform: Linux and Steam Deck" src="https://img.shields.io/badge/platform-Linux%20%7C%20Steam%20Deck-5fb548?style=for-the-badge&labelColor=141b15&color=5fb548"></a>
<a href="LICENSE"><img alt="License: GPL v3" src="https://img.shields.io/badge/license-GPLv3-5fb548?style=for-the-badge&labelColor=141b15&color=5fb548"></a>

<br>

<a href="https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases/latest"><img alt="Download the AppImage" src="docs/readme/button-download.svg" width="300"></a>
&nbsp;
<a href="https://discord.gg/6A9psrtYhh"><img alt="Join the Discord" src="docs/readme/button-discord.svg" width="300"></a>

### A graphical installer and launcher for S.T.A.L.K.E.R. GAMMA on Linux

Install, update, manage and launch S.T.A.L.K.E.R. Anomaly with the GAMMA modpack on Linux and Steam Deck.<br>
Installation progress, mod management, Proton configuration and co-op support in a single application.

<a href="https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases"><b>Download</b></a> ·
<a href="#whats-new-in-140">What's new</a> ·
<a href="#features">Features</a> ·
<a href="#install">Install</a> ·
<a href="#first-run">First run</a> ·
<a href="#steam-deck-mode">Steam Deck</a> ·
<a href="https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/issues">Report a bug</a>

<br>

<a href="docs/readme/screens/dashboard.png"><img alt="The COMMANDER Dashboard" src="docs/readme/screens/dashboard.png" width="100%"></a>

<sub>The Dashboard: active profile, installation status, dependencies, storage, playtime and available updates.</sub>

</div>

> [!WARNING]
> **This is the `1.4.0-Unstable` branch.** Unstable builds receive new features first and may contain defects. For a
> tested build, use the [latest stable release](https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases/latest).
> The release channel can be changed at any time under **Settings → COMMANDER build**.

<br>

<a><img src="docs/readme/divider.svg" width="100%" alt=""></a>

## Overview

GAMMA is a large modpack for S.T.A.L.K.E.R. Anomaly. It is normally installed with a Windows launcher and run through
Mod Organizer 2 (MO2). On Linux, the established installer is
[FaithBeam/stalker-gamma-cli](https://github.com/FaithBeam/stalker-gamma-cli), which is operated entirely from the
terminal.

**COMMANDER is a desktop front-end for that CLI.** It does not reimplement any installer logic. It runs the upstream
`stalker-gamma` binary and parses its output in real time, so every download, checksum and extraction is performed by
the CLI itself and the result is identical to a manual installation.

COMMANDER also provides functionality the CLI does not cover: launching through MO2 in a Wine/Proton prefix, GE-Proton
installation, a `modlist.txt` editor, dependency setup, xrRazom co-op, save and settings backups, and full integrity
verification with repair.

<a id="whats-new-in-140"></a>
<a><img src="docs/readme/h-whats-new.svg" width="100%" alt="What's new in 1.4.0"></a>

<table>
<tr>
<td width="33%" valign="top">
<a><img src="docs/readme/icon-multiplayer.svg" width="44" alt=""></a><br>
<b>Multiplayer: xrRazom co-op</b><br>
A new Multiplayer page installs xrRazom Co-op (Slim) from a downloaded archive and switches between co-op and
singleplayer in one step. All original files are backed up, so singleplayer can always be restored.
</td>
<td width="33%" valign="top">
<a><img src="docs/readme/icon-updates.svg" width="44" alt=""></a><br>
<b>Stable or unstable builds</b><br>
Select the release channel in Settings. Unstable tracks pre-releases such as this one; switching back to stable
installs the latest stable release, even when its version number is lower.
</td>
<td width="33%" valign="top">
<a><img src="docs/readme/icon-system.svg" width="44" alt=""></a><br>
<b>Built-in documentation</b><br>
A complete in-app Help page covering every page and control, the changes each action makes on disk, file locations,
and all network activity.
</td>
</tr>
</table>

<details>
<summary><b>How co-op works</b></summary>
<br>

<img alt="The Multiplayer page" src="docs/readme/screens/multiplayer.png" width="100%">

- **Two components, managed separately.** The Anomaly component (engine executables, Steam networking DLLs and loose
  files) is stored in `<anomaly>/commander-coop/files`, and each original file it replaces is stored in
  `<anomaly>/commander-coop/backup`. The `gamedata/` component is installed as a standard MO2 mod, **xrRazom Co-op**,
  at the highest priority.
- **Switch to co-op** backs up the affected Anomaly files, copies in the co-op files and enables the mod. **Switch to
  Singleplayer** restores the originals and disables the mod. Co-op saves are preserved.
- **Launch validation.** While co-op is enabled, only the co-op target can be launched; while it is disabled, only
  singleplayer. Updates and repairs require switching back to singleplayer first, and **Re-apply co-op** restores the
  co-op files if an update has replaced the engine.
- **Steam or LAN.** Steam sessions use Steam invites through Call of Pripyat (app 41700) when it is owned, and Spacewar
  (app 480) otherwise. COMMANDER creates the co-op shortcut in Steam with the correct compatibility tool and artwork.
  LAN sessions use an IP address and port (27015 by default).
- **Settings** are stored as the mod's `xrr_*` variables in `user.ltx`, and COMMANDER modifies only those lines. The
  host's settings apply to the session: player slots, friendly fire, rescue, saving, teleport and fast travel.

xrRazom Co-op is an independent mod maintained by its own authors. Close the game before changing any co-op setting.

</details>

<a id="features"></a>
<a><img src="docs/readme/h-features.svg" width="100%" alt="Features"></a>

<table>
<tr>
<td width="33%" valign="top">
<a><img src="docs/readme/icon-dashboard.svg" width="44" alt=""></a><br>
<b>Dashboard</b><br>
Active profile, installation status, dependency status, storage usage, playtime and a background check for GAMMA updates.
</td>
<td width="33%" valign="top">
<a><img src="docs/readme/icon-install.svg" width="44" alt=""></a><br>
<b>Install</b><br>
Anomaly and GAMMA with live per-addon progress, resumable installs, and a Minimal mode that saves approximately 50 GB.
</td>
<td width="33%" valign="top">
<a><img src="docs/readme/icon-play.svg" width="44" alt=""></a><br>
<b>Play</b><br>
Launch through MO2 with automatic GE-Proton selection, per-runner prefixes and diagnosis of common launch failures.
</td>
</tr>
<tr>
<td valign="top">
<a><img src="docs/readme/icon-updates.svg" width="44" alt=""></a><br>
<b>Updates</b><br>
Review added, modified and removed addons, apply updates with live progress, and roll back the most recent update.
</td>
<td valign="top">
<a><img src="docs/readme/icon-mods.svg" width="44" alt=""></a><br>
<b>Mod Manager</b><br>
Search, enable, reorder and install custom mods, including FOMOD archives. All changes are backed up and written atomically.
</td>
<td valign="top">
<a><img src="docs/readme/icon-verify.svg" width="44" alt=""></a><br>
<b>Verify and repair</b><br>
A full MD5 scan of every mod, with non-destructive repair that downloads only the affected files again.
</td>
</tr>
<tr>
<td valign="top">
<a><img src="docs/readme/icon-profiles.svg" width="44" alt=""></a><br>
<b>Profiles</b><br>
Multiple independent installations, each with its own folders, MO2 profile and repositories, with export and import.
</td>
<td valign="top">
<a><img src="docs/readme/icon-system.svg" width="44" alt=""></a><br>
<b>System Check</b><br>
Every dependency required by GAMMA and MO2 in one view, with the install command for your distribution.
</td>
<td valign="top">
<a><img src="docs/readme/icon-utilities.svg" width="44" alt=""></a><br>
<b>Utilities</b><br>
Cache cleanup, prefix repair, relocation to another drive, diagnostic log dumps and safeguarded resets.
</td>
</tr>
<tr>
<td valign="top">
<a><img src="docs/readme/icon-backup.svg" width="44" alt=""></a><br>
<b>Saves and settings backups</b><br>
Saves, <code>user.ltx</code> and MCM settings are backed up outside the installation folders before every reset, update and repair.
</td>
<td valign="top">
<a><img src="docs/readme/icon-deck.svg" width="44" alt=""></a><br>
<b>Steam Deck Mode</b><br>
A dedicated interface designed for the Deck's display, controller and touchscreen, with full Game Mode support.
</td>
<td valign="top">
<a><img src="docs/readme/icon-language.svg" width="44" alt=""></a><br>
<b>10 languages, 6 themes</b><br>
Language and theme changes apply immediately without a restart. Font and text size are configurable.
</td>
</tr>
</table>

**Also included:** achievements and game statistics, Discord Rich Presence (disabled by default), desktop shortcuts for
each launch target, one-step **Add to Steam**, the bundled COMMANDER ASSISTANT log analyser, and automatic self-updates
for the AppImage.

<details>
<summary><b>Dashboard</b></summary>
<br>

The default start page. It shows the active profile, the installation status of Anomaly and GAMMA, dependency status,
storage usage across the Anomaly, GAMMA and cache folders, and the result of a background check for GAMMA addon
updates. Shortcut buttons open the Anomaly, GAMMA, cache and log folders, and **Play GAMMA** launches the game directly.
Playtime is tracked per profile, and the Game stats card provides access to achievements.

</details>

<details>
<summary><b>Install</b></summary>
<br>

<img alt="The Install page" src="docs/readme/screens/install.png" width="100%">

Installs S.T.A.L.K.E.R. Anomaly and GAMMA with a **live per-addon progress table** (name, operation, percentage) and
an overall progress bar. Select a base folder and choose **Create folders** to generate the Anomaly, GAMMA and cache
directory layout.

- If a download is interrupted, **Resume GAMMA Installation** is offered on the next launch. Cached archives are
  hash-verified and reused; only missing or changed archives are downloaded again.
- **Minimal mode** deletes addon archives after extraction, saving approximately 50 GB of disk space.
- **Preserve user.ltx** and **Preserve MCM settings** retain keybindings, game options and mod configuration across a
  reinstallation.
- **Install Dependencies** installs everything MO2 and the game require in a single step: `umu-run`, `protontricks` (via `pipx`
  on PEP 668 systems), and eight Visual C++/DirectX runtimes (`d3dcompiler_43`, `d3dcompiler_47`, `d3dx10`,
  `d3dx11_43`, `d3dx9`, `quartz`, `dx8vb`, `vcrun2022`). They are installed through the selected runner's own Wine
  (`umu-run winetricks` for GE-Proton, the runner's `wine` binary otherwise), never the system `winetricks` or `wine`.
  Running a different Wine build against a Proton prefix corrupts its system DLLs and causes the game to crash
  repeatedly on launch, so COMMANDER does not permit it.

</details>

<details>
<summary><b>Play</b></summary>
<br>

<img alt="The Play page" src="docs/readme/screens/play.png" width="100%">

Launch GAMMA through Mod Organizer 2, open MO2 directly, or run Anomaly's executable without MO2's virtual file system.
Targets are read straight from `ModOrganizer.ini`, with `AnomalyLauncher.exe` as a fallback.

- **Automatic runner detection.** The newest installed GE-Proton build is selected and launched through `umu-run`.
- **Built-in GE-Proton installer.** Recent releases can be browsed and downloaded with progress reporting; COMMANDER
  verifies the SHA-512 checksum and installs the build into `compatibilitytools.d`.
- **Per-runner prefixes.** Each runner uses its own Wine prefix, so switching runners cannot corrupt a prefix created by
  a different version.
- A live command preview with a copy button, a custom launch options field (supporting variables such as
  `PROTON_USE_WINED3D=1`), and status indicators for installed GE-Proton builds, GameMode and MangoHud.
- The game runs as a **detached process**, so closing COMMANDER does not end the session. Output is written to a
  rotating `launcher.log`. Failed launches are diagnosed automatically: DXVK/Vulkan problems, runner/prefix mismatches
  and the common `concrt140.dll` error are identified by name, with a one-step bug report.
- **Desktop shortcuts** for any launch target using the current runner.
- Optional **Discord Rich Presence** ("Playing S.T.A.L.K.E.R. GAMMA" with mod count and playtime). Disabled by
  default and enabled from Settings.

</details>

<details>
<summary><b>Updates</b></summary>
<br>

<img alt="The Updates page" src="docs/readme/screens/update.png" width="100%">

Compares the installed GAMMA version and addon list with the latest official data, without using the rate-limited
GitHub REST API, and lists every change: added, modified, removed and renamed archives.

Updates use the same live progress reporting as a new installation, honour the Minimal and preserve-settings options,
and hold the global install lock so they cannot run concurrently with another installation. A **background check runs
at most once a day**, independently of this page, and raises a desktop notification when an update is available. If an
update fails, **Undo Last Update** restores the `modlist.txt` snapshot taken immediately before it.

</details>

<details>
<summary><b>Mod Manager</b></summary>
<br>

<img alt="The Mod Manager" src="docs/readme/screens/modmanager.png" width="100%">

Safe editing of the active MO2 profile's `modlist.txt`: search, enable, disable, delete and reorder mods, grouped by
the `_separator` categories that GAMMA provides.

- **Drag-and-drop reordering** with multi-selection, plus Move Up and Move Down.
- **Reversed load order detection.** A fully reversed GAMMA load order, which causes a crash on startup, is detected
  here and before launch, with a one-step fix.
- Create categories, install a local ZIP/7Z/RAR/FOMOD archive directly into the mod list, and set the active profile as
  MO2's selected profile without opening MO2.
- **A backup is created before the first edit** and can be restored from the Backups menu, alongside **Restore Original
  Order**, which returns GAMMA's mods to their original order while keeping custom mods.
- **Writes are atomic**, so a crash or full disk cannot truncate the load order, and **editing is disabled while Mod
  Organizer is running**, because MO2 rewrites the file on exit.
- **Check for File Conflicts** lists files provided by more than one enabled mod, showing which mod takes precedence.
- **Ctrl+F** focuses this page's search field from anywhere in the application.

</details>

<details>
<summary><b>Verify Integrity and Repair</b></summary>
<br>

Performs three checks: an Anomaly file check, a GAMMA presence check (every enabled mod must exist on disk), and a
full MD5 scan of every file under `gamma/mods` against a stored baseline. Missing, corrupted and modified files are
reported.

When a damaged mod appears in the official GAMMA mod list, COMMANDER can **repair it automatically and
non-destructively**: the mod folder and its cached archive are moved aside rather than deleted, then the mod is
downloaded again and verified against the official MD5 checksum. If the repair fails or is cancelled, the original
copies are restored. Custom mods are never modified, and mods without a known download source are reported instead.

</details>

<details>
<summary><b>Profiles</b></summary>
<br>

<img alt="The Profiles page" src="docs/readme/screens/profiles.png" width="100%">

Create, edit, activate and delete CLI profiles, each with its own Anomaly, GAMMA, cache, MO2 profile, download-thread
and repository settings. Creation, activation and deletion are performed through `stalker-gamma config`, so their side
effects (MO2's `selected_profile`, mod list downloads) occur exactly as the CLI defines them. Advanced fields expose
every repository URL and branch the CLI supports, for use with forks and mirrors.

**Export and Import Profile** save a profile's portable settings (and its `modlist.txt`, if installed) to a file for
backup or sharing. Installation folders are selected again on import, because they are specific to each machine.

</details>

<details>
<summary><b>System Check</b></summary>
<br>

<img alt="The System Check page" src="docs/readme/screens/systemcheck.png" width="100%">

Checks every dependency GAMMA and MO2 need: the CLI, Wine, Winetricks, Protontricks, `umu-run`, Vulkan (including the
32-bit loader), each Winetricks runtime, GE-Proton builds, GameMode and MangoHud. Each check reports its status with
the install command for your distribution, and manual overrides allow tools in non-standard locations to be used.

</details>

<details>
<summary><b>Utilities</b></summary>
<br>

<img alt="The Utilities page" src="docs/readme/screens/utilities.png" width="100%">

- **Cache cleanup.** Lists outdated archives and the space they occupy before removing them.
- **Clear shader cache** and **Remove ReShade** restore a clean state after driver or mod changes.
- **Fix GOG installation** corrects `ModOrganizer.ini` paths for a GOG copy of Anomaly.
- **Repair Wine prefix** restores the runner's system DLLs after another Wine build has written to the game's prefix
  (symptom: every launch crashes repeatedly and may exhaust system memory). COMMANDER also refuses to launch into a
  prefix it detects as damaged. Reinstall dependencies after a repair.
- **Move installation** copies Anomaly, GAMMA and the cache to another drive and verifies the copy before deleting the
  originals. An interrupted move is detected and resumed on the next launch.
- **Create Log Dump** and **Export Diagnostics** package logs, settings and system information into a single archive for
  bug reports.
- **Fresh Reset**, **GAMMA Reset** and **Full Uninstall** list exactly what will be deleted and what will be kept
  before proceeding, and back up saves and settings first. The Wine prefix is always preserved.
- Opens the bundled **COMMANDER ASSISTANT** log analyser.

<img alt="COMMANDER ASSISTANT" src="docs/readme/screens/assistant.png" width="100%">

</details>

<details>
<summary><b>Settings</b></summary>
<br>

- **10 languages:** English, French, Spanish, German, Romanian, Polish, Russian, Ukrainian, Portuguese and Turkish.
  Changes apply immediately without a restart, unless a background task is running.
- **6 themes:** GAMMA, GAMMA Black, Dusk, Midnight, Terminal and Reactor.
- Interface font (6 options) and size (9–22 px), both applied immediately.
- **COMMANDER build:** stable or unstable.
- Startup page, default runner, "Always use GameMode", an Open Winecfg shortcut, and MO2 display scale presets
  (100–200%) for legible text in Mod Organizer.
- Desktop autostart on login.
- **Add to Steam** adds COMMANDER and Deck Mode to the Steam library as non-Steam games.

</details>

<a id="screens"></a>
<a><img src="docs/readme/h-screens.svg" width="100%" alt="Screenshots"></a>

<table>
<tr>
<td width="50%"><a href="docs/readme/screens/multiplayer.png"><img src="docs/readme/screens/multiplayer.png" alt="Multiplayer: xrRazom co-op"></a><br><sub>Multiplayer: xrRazom co-op</sub></td>
<td width="50%"><a href="docs/readme/screens/profiles.png"><img src="docs/readme/screens/profiles.png" alt="Profiles: several installs side by side"></a><br><sub>Profiles: several installs side by side</sub></td>
</tr>
<tr>
<td width="50%"><a href="docs/readme/screens/install.png"><img src="docs/readme/screens/install.png" alt="Install with live per-addon progress"></a><br><sub>Install: live per-addon progress</sub></td>
<td width="50%"><a href="docs/readme/screens/play.png"><img src="docs/readme/screens/play.png" alt="Play: runners, prefixes and launch targets"></a><br><sub>Play: runners, prefixes and launch targets</sub></td>
</tr>
<tr>
<td width="50%"><a href="docs/readme/screens/update.png"><img src="docs/readme/screens/update.png" alt="Updates: every changed addon"></a><br><sub>Updates: every changed addon</sub></td>
<td width="50%"><a href="docs/readme/screens/modmanager.png"><img src="docs/readme/screens/modmanager.png" alt="Mod Manager: GAMMA's categories, drag to reorder"></a><br><sub>Mod Manager: GAMMA's categories, drag to reorder</sub></td>
</tr>
<tr>
<td width="50%"><a href="docs/readme/screens/systemcheck.png"><img src="docs/readme/screens/systemcheck.png" alt="System Check: every dependency in one place"></a><br><sub>System Check: every dependency in one place</sub></td>
<td width="50%"><a href="docs/readme/screens/utilities.png"><img src="docs/readme/screens/utilities.png" alt="Utilities: maintenance and recovery"></a><br><sub>Utilities: maintenance and recovery</sub></td>
</tr>
</table>

<a id="install"></a>
<a><img src="docs/readme/h-install.svg" width="100%" alt="Install"></a>

### AppImage (recommended)

Download the latest `.AppImage` from [**Releases**](https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases).
Unstable builds are marked **Pre-release**.

```bash
chmod +x STALKER-GAMMA-COMMANDER-*-x86_64.AppImage
./STALKER-GAMMA-COMMANDER-*-x86_64.AppImage
```

The AppImage bundles Python, Qt and the CLI, and updates itself automatically. For a menu entry and icon, use
[Gear Lever](https://github.com/mijorus/gearlever) or [AppImageLauncher](https://github.com/TheAssassin/AppImageLauncher).

### Arch Linux

A `PKGBUILD` is provided under [`packaging/aur/`](packaging/aur/):

```bash
git clone https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER.git
cd STALKER-GAMMA-COMMANDER/packaging/aur
makepkg -si
```

### From source

```bash
git clone -b 1.4.0-Unstable https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER.git
cd STALKER-GAMMA-COMMANDER
./run.sh
```

`run.sh` creates a virtual environment in `.venv/`, installs PySide6 and starts the application. The `stalker-gamma`
CLI is bundled at `cli/usr/bin/stalker-gamma`. Pass `--deck` to start in [Steam Deck Mode](#steam-deck-mode):
`./run.sh --deck`.

<details>
<summary><b>Running several instances and the tests</b></summary>
<br>

**`run.sh` permits multiple concurrent instances**, so two builds can run side by side during development. Each
additional window shows `[extra instance]` in its title bar. All instances share `~/.config/stalker-gamma`, so the last
settings write takes effect, and nothing prevents two instances from installing into the same folders. Use
`COMMANDER_ALLOW_MULTIPLE=0 ./run.sh` to match release behaviour. The AppImage and the AUR package always reject a
second instance.

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ -q
```

The Qt platform must be set on the command line, because the suite creates real widgets and no `pytest.ini` defines
the variable.

</details>

<a id="first-run"></a>
<a><img src="docs/readme/h-first-run.svg" width="100%" alt="First run"></a>

1. **Profiles:** set the Anomaly, GAMMA and cache folders (absolute paths) and create the profile. The new profile is
   activated automatically.
2. **Install → Install GAMMA.** Anomaly is installed first if it is not present. The download is large: approximately
   150 GB, or 100 GB in Minimal mode.
3. **Install → Install Dependencies** installs the Wine/Proton runtimes required by MO2.
4. **Play:** select a runner and a launch target, then choose **Launch Game**.

Afterwards, use **Updates** to apply addon updates and **Verify Integrity** to diagnose a damaged installation.

> [!TIP]
> For support, consult the **Help** page in COMMANDER, or create a report with **Utilities → Create Log Dump** and
> share it on the [Discord](https://discord.gg/6A9psrtYhh).

<a id="steam-deck-mode"></a>
<a><img src="docs/readme/h-deck.svg" width="100%" alt="Steam Deck Mode"></a>

A dedicated interface designed for the Deck's 1280×800 display, controls and touchscreen. To open it, select the Steam
Deck icon on the **Dashboard**, next to *Quick actions*; COMMANDER restarts in Deck Mode. **Exit Deck Mode**, on Deck
Mode's Settings screen or in the ☰ Menu, returns to the desktop interface in the same way.

- **First-run setup.** When no profile exists, Deck Mode asks whether to use internal storage or an SD card, creates
  the profile and continues to Install.
- **Tabs, in handheld order:** Dashboard · Play · Mods · Update · Install · Utilities · System · Settings.
- **Dashboard** summarises readiness in a single banner with the relevant next action (Play, Install, Resume or
  Update), followed by the status of Anomaly, GAMMA and dependencies, updates, mods, storage and profiles, with a
  built-in folder browser for editing them.
- **Play** provides **Force Stop Game** for an unresponsive game, as the Deck has no Alt+F4. **Mods** presents the Mod
  Manager's category tree as a full-screen list. **Update**, **Install**, **Utilities**, **System** and **Settings**
  match their desktop counterparts.
- **Handheld-aware.** The Deck is kept awake during installs, updates and moves, a warning is shown before a long
  download on battery power, and the header displays the battery level.
- **Scales to any display.** On an external monitor or TV, the interface scales with the window.

<details>
<summary><b>Controls</b></summary>
<br>

The controller works in Game Mode without configuration (COMMANDER reads the gamepad directly, so Steam's default
layout is sufficient) and with Desktop Mode's keyboard layout.

| Button | Action |
|---|---|
| D-pad / left stick | Move (hold to repeat) |
| A | Select / toggle |
| B | Back |
| X | The screen's main action (below) |
| Y | The screen's second action / search |
| L1 / R1 | Previous / next tab |
| L2 / R2 | Page up / down |
| ☰ Menu | Quick menu: Play GAMMA, Exit Deck Mode or Quit |
| ⧉ View | Controls help |

| Screen | X | Y |
|---|---|---|
| Dashboard | What the banner says (Play / Install / Resume / Update) | Re-check |
| Play | Play GAMMA (Force Stop while running) | Open MO2 |
| Mods | Category menu | Search |
| Update | Check / Apply updates | Show changes |
| Install | Install or resume GAMMA | Install dependencies |
| System | Re-check | — |

Text fields include a built-in on-screen keyboard (Y), touch input is supported throughout (tap to select, drag to
scroll), and each screen lists its controls in the footer. All six themes and ten languages are supported, and text
size can be scaled independently from 80% to 150%.

</details>

<details>
<summary><b>Adding Deck Mode to Steam</b></summary>
<br>

On a Steam Deck, COMMANDER offers Deck Mode on first start and remembers the choice, which can be changed under
**Settings → When COMMANDER starts**. In Game Mode, Deck Mode opens automatically.

In Desktop Mode, use **Add COMMANDER to Steam** (desktop **Settings → Steam**, or Deck Mode's **Settings**). This adds
both **STALKER COMMANDER** and **STALKER COMMANDER DECK** to the Steam library. If Steam is running, it is closed while
the shortcuts are written and then restarted. To add them manually:

- *AUR / from source:* in Desktop Mode, **Add a Non-Steam Game** and pick **STALKER COMMANDER (Deck Mode)**.
- *AppImage:* add the `.AppImage` file, then set its launch options to `--deck`. An AppImage can publish only one
  desktop entry, so a separate Deck Mode entry is not included.

Under gamescope, Deck Mode switches to full screen automatically, because gamescope has no window manager to size the
window.

</details>

<a id="contributing-a-translation"></a>
<a><img src="docs/readme/h-contrib.svg" width="100%" alt="Contribute"></a>

Adding a language requires no code changes. Start from
[`commander_gui/locales/template.py`](commander_gui/locales/template.py), which lists every translatable string in the
application, and follow the instructions in its header.

To report a bug, [open an issue](https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/issues) that includes the
COMMANDER version, your distribution and a log dump from **Utilities → Create Log Dump**. Feedback on unstable builds is
particularly welcome on the [Discord](https://discord.gg/6A9psrtYhh).

<a id="credits"></a>
<a><img src="docs/readme/h-credits.svg" width="100%" alt="Credits"></a>

- **[FaithBeam](https://github.com/FaithBeam)**: [`stalker-gamma-cli`](https://github.com/FaithBeam/stalker-gamma-cli),
  the installer that COMMANDER bundles and runs. All installation, download and checksum logic is their work.
- **[dnttnd](https://github.com/dnttnd)**: testing, development builds, bug reports and UI refinement.
- **[Grokitach](https://github.com/Grokitach)** and the GAMMA team: [the modpack itself](https://github.com/Grokitach/Stalker_GAMMA).
- **The xrRazom team**, for xrRazom Co-op.
- **[GSC Game World](https://www.gsc-game.com/)** and the Anomaly team, for the game.

<a id="license"></a>
<a><img src="docs/readme/h-license.svg" width="100%" alt="License"></a>

Licensed under the **GNU General Public License v3.0**; see [LICENSE](LICENSE). COMMANDER bundles and runs
`stalker-gamma-cli`, which is licensed under GPL-3.0, and is therefore distributed under the same license.

Copyright for the Python/Qt graphical interface: **SSH-Kitty**.

<div align="center">

<a><img src="docs/readme/footer.svg" width="100%" alt=""></a>

<sub>S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER · made with Python, PySide6 and Qt 6</sub><br>
<sub>Not affiliated with GSC Game World or the GAMMA development team.</sub><br>
<sub><i>Good hunting, stalker.</i></sub>

</div>
