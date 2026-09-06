<div align="center">

# S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER

**A complete graphical front-end for installing, updating, managing and launching S.T.A.L.K.E.R. Anomaly with the GAMMA modpack on Linux.**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20x86__64-informational)](#requirements)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](#from-source)
[![Qt](https://img.shields.io/badge/GUI-PySide6%20%2F%20Qt%206-41cd52)](https://doc.qt.io/qtforpython-6/)
[![Release](https://img.shields.io/github/v/release/SSH-Kitty/STALKER-GAMMA-COMMANDER?include_prereleases&label=release)](https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases)

</div>

<img width="1551" height="1044" alt="image" src="https://github.com/user-attachments/assets/e06df21c-ccb7-4541-8a01-76a07bc7ae1d" />

## What this is

GAMMA is a huge S.T.A.L.K.E.R. Anomaly mod pack, normally installed through a Windows launcher and run through Mod Organizer 2. On Linux, the community solution is [FaithBeam/stalker-gamma-cli](https://github.com/FaithBeam/stalker-gamma-cli) — an excellent but entirely terminal-driven installer.

**COMMANDER is a desktop GUI around that CLI.** It doesn't reimplement any installer logic — it drives the real `stalker-gamma` binary as a subprocess and parses its output live. Every download, checksum and extraction is performed by the upstream CLI, so results are identical to using it by hand; you just get progress tables, a mod manager, prefix handling and a Play button instead of a terminal.

On top of the CLI, COMMANDER adds things it doesn't do on its own: launching through Mod Organizer 2 in a Wine/Proton prefix, a GE-Proton installer, a `modlist.txt` editor, dependency setup, and a full integrity check & repair pass.

---

## Features

### Dashboard
The landing page. Shows the active profile, install status for Anomaly and GAMMA, a live dependency count, storage usage across your Anomaly/GAMMA/cache folders, and a background check for GAMMA addon updates. Quick-open buttons jump straight to the Anomaly, GAMMA, cache and log folders, and a **Play GAMMA** shortcut launches the game without leaving the page.

### Install
Installs S.T.A.L.K.E.R. Anomaly and GAMMA with a **live per-addon progress table** (name, operation, percent) and an overall completion bar. Pick a base folder and hit **Create folders** to auto-generate the Anomaly/GAMMA/cache layout.

<img width="1551" height="1044" alt="image" src="https://github.com/user-attachments/assets/b246956d-f400-41dc-8bd5-d66245e59b2e" />

- If a download is interrupted, the next launch offers **Resume GAMMA Installation** — cached archives are hash-verified and reused, only missing or changed ones are re-downloaded.
- **Minimal mode** deletes addon archives after extraction to save ~50 GB of disk space.
- **Preserve user.ltx / Preserve MCM settings** checkboxes protect your keybindings, game options and mod configs across a reinstall.
- **Install Dependencies** sets up everything MO2 and the game need in one click: `umu-run`, `protontricks` (via `pipx` on PEP 668 systems), and eight Visual C++/DirectX runtimes (`d3dcompiler_43`, `d3dcompiler_47`, `d3dx10`, `d3dx11_43`, `d3dx9`, `quartz`, `dx8vb`, `vcrun2022`).

### Play
Launch GAMMA through Mod Organizer 2, open MO2 directly, or run Anomaly's executable without the MO2 virtual file system. Targets are read straight from `ModOrganizer.ini`, with `AnomalyLauncher.exe` used as a fallback if none are found.

<img width="1551" height="1044" alt="image" src="https://github.com/user-attachments/assets/c8b9db12-f630-4bc6-90fd-2d994616db36" />

- **Auto runner detection** — the newest installed GE-Proton build is picked automatically and launched through `umu-run`.
- **Built-in GE-Proton installer** — browse recent GE-Proton releases, download with a progress bar and cancel support, and COMMANDER verifies the SHA-512 checksum and installs it into `compatibilitytools.d` for you. No manual downloading or extracting.
- **Per-runner prefixes** — each runner remembers its own Wine prefix, so switching runners never corrupts a prefix built by a different version.
- A live command preview with a copy button, a custom launch options field (supports env vars like `PROTON_USE_WINED3D=1`), and status chips showing installed GE-Proton builds, GameMode and MangoHud.
- The game launches **detached** — closing COMMANDER doesn't kill your session — with output captured to a rotating `launcher.log`. Failed launches are diagnosed automatically: a DXVK/Vulkan problem, a runner/prefix mismatch, or the classic `concrt140.dll` error are called out by name instead of surfacing a raw Wine crash.
- One-click **desktop shortcuts** that launch a specific target with the currently selected runner.

### Updates
Compares your installed GAMMA version and addon list against the latest official data — without hitting the rate-limited GitHub REST API — and shows exactly what changed: Added, Modified, Removed, and archive-name changes.

Applying updates reuses the same live progress UI as a fresh install, respects the Minimal/preserve-settings options, and holds the global install lock so it can never run alongside another install.

### Mod Manager
Direct, careful editing of the active MO2 profile's `modlist.txt` — search, enable/disable, delete and reorder mods, grouped by the `_separator` categories GAMMA ships.

<img width="1551" height="1044" alt="image" src="https://github.com/user-attachments/assets/a0ccfe1a-5a25-42a7-a7c3-55af4c105773" />

- **Drag-and-drop reordering** with multi-selection support, plus Move Up/Down and **Flip Priority** to reverse the entire load order in one click.
- Create new categories, install a local ZIP/7Z/RAR/FOMOD mod archive straight into the modlist, and use the active profile as MO2's selected profile without opening MO2.
- **A backup is taken automatically before your first edit** (`modlist.txt.gammagui.bak`) and can be restored from the UI, alongside a **Restore Original Order** option.
- **Writes are atomic** — a crash or full disk cannot truncate your load order — and **edits are blocked while Mod Organizer is running**, since MO2 rewrites the file on exit and would silently discard them.

### Verify Integrity & Repair
Runs three passes: an Anomaly file check, a GAMMA presence check (every enabled mod must exist on disk), and a full MD5 scan of every file under `gamma/mods` against a saved baseline. Anything missing, corrupted or changed is reported.

If a broken mod matches an entry in the official GAMMA mod list, COMMANDER can **repair it automatically**: the mod folder and its cached archive are deleted, then it's redownloaded and MD5-verified against the official checksum. Your own added mods and files are never touched, and anything with no known download source is reported instead of silently deleted.

### Profiles
Create, edit, activate and delete CLI profiles — each with its own Anomaly, GAMMA, cache, MO2 profile, download-thread and repository settings. Creation, activation and deletion are delegated to `stalker-gamma config` so its side effects (MO2's `selected_profile`, modlist downloads) happen exactly as the CLI intends. Advanced fields expose every repo URL and branch the CLI supports, for anyone using a fork or mirror.

### Utilities
A toolbox for maintenance and recovery:

<img width="1551" height="1044" alt="image" src="https://github.com/user-attachments/assets/244e4dec-eefa-45ea-9111-d10d947f8ffc" />

- **Cache cleanup** — preview which archives are out of date and how much space they'll free, then clean them.
- **Clear shader cache** and **Remove ReShade** for a clean slate after driver or mod changes.
- **Fix GOG installation** — repairs `ModOrganizer.ini` paths for a GOG-provided copy of Anomaly.
- **Move installation** — copy Anomaly, GAMMA and cache to another drive, verified before the originals are deleted. An interrupted move is safely detected and resumed on the next launch.
- **Create Log Dump** and **Export Diagnostics** — bundle logs, settings and system info into one archive for bug reports.
- **Fresh Reset**, **GAMMA Reset** and **Full Uninstall** — guarded destructive actions that show exactly what will be deleted and what's kept (your Wine prefix always survives) before doing anything.
- Opens the bundled **COMMANDER ASSISTANT** log analyzer directly from the page.

### Settings
- **10 languages** — English, French, Spanish, German, Romanian, Polish, Russian, Ukrainian, Portuguese and Turkish. Switch anytime; it applies instantly with no restart, unless a background task is running.
- **5 themes** — GAMMA, Dusk, Midnight, Terminal and Black, each with its own color palette.
- Interface font family (6 options) and size (9–22 px), both applied live.
- Startup page, default runner, an "Always use GameMode" toggle, an Open Winecfg shortcut, and MO2 Display Scale presets (100–200%) for readable text in Mod Organizer.
- Desktop autostart, launching COMMANDER automatically on login.

### System Check
Checks every dependency GAMMA and MO2 need in one place: the CLI, Wine, Winetricks, Protontricks, `umu-run`, Vulkan (including the 32-bit loader), each individual Winetricks runtime, GE-Proton builds, GameMode and MangoHud. Every check shows its status and a copyable install command for your distro, and manual overrides let you point COMMANDER at tools installed in non-standard locations.

<img width="1551" height="1044" alt="image" src="https://github.com/user-attachments/assets/55aa2e70-9940-476d-9526-d53611718e2d" />

---

## Installation

### AppImage (recommended)

Download the latest `.AppImage` from [**Releases**](https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases):

```bash
chmod +x STALKER-GAMMA-COMMANDER-*-x86_64.AppImage
./STALKER-GAMMA-COMMANDER-*-x86_64.AppImage
```

Python, Qt and the CLI are all bundled inside. For a menu entry and icon, use [Gear Lever](https://github.com/mijorus/gearlever) or [AppImageLauncher](https://github.com/TheAssassin/AppImageLauncher).

### From source

```bash
git clone https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER.git
cd STALKER-GAMMA-COMMANDER
./run.sh
```

`run.sh` creates a `.venv/`, installs PySide6, and launches the app. The `stalker-gamma` CLI is already bundled at `cli/usr/bin/stalker-gamma`.

---

## First run

1. **Profiles** → set your Anomaly, GAMMA and Cache folders (use absolute paths) and create the profile — it activates automatically.
2. **Install** → **Install GAMMA**. Anomaly is installed first if it's missing. Expect a large download: ~150 GB, or ~100 GB with Minimal mode.
3. **Install → Install Dependencies** — sets up the Wine/Proton runtimes MO2 needs.
4. **Play** → pick a runner and launch target, then **Launch Game**.

Use **Updates** for addon updates afterward, and **Verify Integrity** if something seems broken.

---

## Credits

- **[FaithBeam](https://github.com/FaithBeam)** — [`stalker-gamma-cli`](https://github.com/FaithBeam/stalker-gamma-cli), the installer this GUI drives and bundles. All installation, download and checksum logic is theirs.
- **[Grokitach](https://github.com/Grokitach)** and the GAMMA team — [the mod pack itself](https://github.com/Grokitach/Stalker_GAMMA).
- **[GSC Game World](https://www.gsc-game.com/)** and the Anomaly team, for the game.
- **[dnttnd](https://github.com/dnttnd)** — testing implementations, dev builds, bug reports, and helping polish the UI.

## License

Licensed under the **GNU General Public License v3.0** — see [LICENSE](LICENSE). This project bundles and drives `stalker-gamma-cli`, which is GPL-3.0, so this front-end is GPL-3.0 as well.

- Copyright for the underlying CLI installer logic: **FaithBeam**
- Copyright for this Python/Qt graphical interface: **SSH-Kitty**

*Not affiliated with GSC Game World or the GAMMA development team.*
