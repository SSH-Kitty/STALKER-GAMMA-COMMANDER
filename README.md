<div align="center">

# S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER

**A complete graphical front-end for installing, updating, managing and launching S.T.A.L.K.E.R. Anomaly with the GAMMA modpack on Linux.**

**Current GUI version: 1.2.0**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20x86__64-informational)](#requirements)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](#option-2--run-from-source)
[![Qt](https://img.shields.io/badge/GUI-PySide6%20%2F%20Qt%206-41cd52)](https://doc.qt.io/qtforpython-6/)
[![Release](https://img.shields.io/github/v/release/SSH-Kitty/STALKER-GAMMA-COMMANDER?include_prereleases&label=release)](https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases)
[![Linux](https://img.shields.io/badge/Linux-%23FCC624.svg?style=Flat&logo=linux&logoColor=black)](https://github.com/torvalds)

</div>

---

<img width="1235" height="993" alt="image" src="https://github.com/user-attachments/assets/34a95fd4-7f86-461c-a8de-db0c7e75101a" />


## What this is

G.A.M.M.A. is a large S.T.A.L.K.E.R. Anomaly mod pack that is normally installed through a Windows launcher and run through Mod Organizer 2. On Linux the community solution is [**FaithBeam/stalker-gamma-cli**](https://github.com/FaithBeam/stalker-gamma-cli) — an excellent but entirely terminal-driven installer.

**Commander is a desktop GUI around that CLI.** It does not reimplement any installer logic: it drives the real `stalker-gamma` binary as a subprocess and parses its output live. Every download, checksum, ModDB fetch and extraction is performed by the upstream CLI, so results are identical to using it by hand — you just get progress tables, a mod manager, prefix handling and a Play button instead of a terminal.

On top of the CLI it adds things the CLI does not do: launching the game through Mod Organizer in a Wine/Proton prefix, editing `modlist.txt`, installing the Visual C++/DirectX runtimes MO2 needs, and a full MD5 integrity-and-repair pass over your installed mods.

> **Scope:** Linux desktop, x86_64. The underlying CLI also supports Windows, but this GUI's launcher, prefix handling and runner detection are Linux-specific.

---

## Features

### Dashboard
Active profile summary, installation status for Anomaly and GAMMA, Wine/Proton runtime status, storage usage across Anomaly/GAMMA/cache folders, GAMMA version and addon-change status, background update checks, and quick-open buttons for Anomaly, GAMMA, cache and log folders. Runtime checks pause while MO2 or the game is running.

### System Check
Checks the GAMMA CLI, Linux, Steam, `umu-run`, Wine, Winetricks, Protontricks, Vulkan, 32-bit Vulkan, each configured Winetricks runtime dependency, GE-Proton builds, GameMode and MangoHud. Each check includes its current status and copyable installation guidance where available. Manual overrides can browse for Steam libraries, executables, GE-Proton builds and Vulkan tooling; overrides are saved and rechecked on refresh.

<img width="1161" height="537" alt="System Check" src="https://github.com/user-attachments/assets/d55c673f-6139-48f7-9eff-190737d7dd64" />


### Play
Launches GAMMA through Mod Organizer 2 (MO2), opens MO2 for mod management, or launches a selected Anomaly executable directly. Targets are read from `ModOrganizer.ini`; if no MO2 target is available, `AnomalyLauncher.exe` is used as a direct-launch fallback.

<img width="1234" height="949" alt="Play" src="https://github.com/user-attachments/assets/a64d8611-e23a-4b76-ae1a-ec465223489c" />


- **Auto runner detection** — the latest discovered GE-Proton build is selected automatically and launched through `umu-run`, wrapped in `gamemoderun` when enabled.
- **Explicit runner picking** — discovered GE-Proton builds from `compatibilitytools.d` can be selected directly. The current GUI exposes Auto-detect and GE-Proton builds only.
- **GE-Proton installer** — look up recent releases in-app, download them with progress and cancellation, verify SHA-512 checksums, install into `compatibilitytools.d`, and refresh the runner list automatically.
- **Per-runner prefixes** — each runner remembers its own prefix, so switching runners does not corrupt a prefix built by another version.
- **Live command preview** with a copy button, detected-runner status, custom launch options, and direct Anomaly launch without MO2's virtual mod filesystem.
- The game is started **detached** — closing Commander does not kill your session — with output captured to a rotating `launcher.log`. A failed launch is diagnosed from that log, and a runner/prefix mismatch is called out explicitly instead of surfacing a raw Wine error.

### Install
Installs S.T.A.L.K.E.R. Anomaly and GAMMA with a **live per-addon progress table** (name, operation, percent), an overall completion bar driven by the CLI's `[done/total]` counter, and clean cancellation.

<img width="1184" height="1088" alt="Install" src="https://github.com/user-attachments/assets/653778d2-049d-4b84-b780-c3141c33ffab" />


- Options for `--minimal` (delete archives after extract, ~50 GB saved), and preserving `user.ltx` and MCM settings across a reinstall.
- Anomaly 1.5.3 installation can be chained automatically before GAMMA installation when Anomaly is missing. Installation and cache folder fields are editable and browseable directly on this page.
- **Winetricks panel** — checks Wine, Winetricks, and protontricks first. If protontricks is missing, it can be installed automatically before the eight runtime verbs run: `d3dcompiler_43`, `d3dcompiler_47`, `d3dx10`, `d3dx11_43`, `d3dx9`, `quartz`, `dx8vb`, and `vcrun2022`. PEP 668 systems are directed to install pipx instead of using system pip. Per-verb and tool status is shown in the panel.
- **Verify Integrity** — see [Integrity and repair](#integrity-and-repair).

### Update
GUI-side GAMMA update checks compare the official mod list and version files without using the rate-limited GitHub REST API. The page shows Added / Modified / Removed addon changes, archive-name changes and version information, then runs `update apply` through the same live progress UI. GAMMA must already be installed and the official files must be reachable. Updates hold the global install lock so they cannot run concurrently with an install.

<img width="1183" height="583" alt="Updates" src="https://github.com/user-attachments/assets/8547ab1f-2660-4df8-997c-9957447dca81" />


### Mod Manager
Direct, careful editing of the selected MO2 profile's `modlist.txt`. MO2 profiles are discovered from the active GAMMA installation and the selected profile is shown in the page.

<img width="1198" height="892" alt="image" src="https://github.com/user-attachments/assets/6f2367b1-fa7f-4e01-8b0f-6f851d038fd1" />


- Mods grouped by the `_separator` category entries G.A.M.M.A. ships, with search.
- Enable, disable, delete and reorder within a category, including multi-selection actions.
- Use the selected profile as MO2's selected profile or open MO2 directly. Delete removes entries from `modlist.txt`; it does not delete mod files.
- **A backup is taken before the first edit** (`modlist.txt.gammagui.bak`) and can be restored from the UI.
- **Writes are atomic** — a crash or full disk cannot truncate your load order.
- **Edits are blocked while Mod Organizer is running**, because MO2 rewrites the file on exit and would discard them.

### Profiles
Create, edit, activate and delete CLI profiles. Creation, activation and deletion are delegated to `stalker-gamma config` so its side effects (MO2 `selected_profile`, modlist download) happen exactly as the CLI intends. Advanced fields expose every repo URL and branch the CLI supports. Folder fields have browse dialogs; Commander configures existing MO2 profiles but does not create them.

COMMANDER profiles store Anomaly, GAMMA, cache, MO2 profile, download-thread, repository, and branch settings. Absolute paths are strongly recommended. A COMMANDER profile is separate from the selected MO2 profile.

### Utilities
Anomaly integrity checks, shader-cache cleanup, ReShade removal, download-cache cleanup with reclaimable-size totals and preview, GOG installation-path repair, installation-folder moves with cancellation and copy verification, log-folder access, a collapsible console, and `debug hash-install` diagnostic archives. Interrupted moves are recovered and reviewed at startup.

<img width="1178" height="616" alt="Utilities" src="https://github.com/user-attachments/assets/7f5e790e-f532-4f1c-97f9-b249eb2b5e63" />


Plus two guarded destructive actions:
- **Fresh Reset** — wipe the Anomaly and GAMMA folders and reinstall both from scratch into the same locations.
- **Full Uninstall** — remove the Anomaly, GAMMA, and cache folders, leaving your Wine/Proton prefix intact.

- **Move installation** — copy Anomaly, GAMMA, and cache folders to an existing destination, verify the copies, update the active profile, and then remove the originals. Interrupted moves are checked on the next startup.

Both show an explicit warning listing the exact folders that will be deleted and what you will lose (saves, MO2 settings, MCM settings, any mods you added), refuse to operate on system paths, home directories or symlinks, and re-check that the profile still points where it did before deleting anything.

### Settings and autostart
Settings controls the startup page, desktop autostart, default GE-Proton runner, GameMode behavior, interface font size from 9–22 px, custom launch options, saved tool overrides, diagnostic export, and five themes: GAMMA, Dusk, Midnight, Terminal and Black. Autostart creates `stalker-gamma-commander.desktop` under `$XDG_CONFIG_HOME/autostart/` or `~/.config/autostart/`.

---

## Requirements

### AppImage

| | |
|---|---|
| **Architecture** | x86_64 |
| **glibc** | **2.34 or newer** — Ubuntu 22.04+, Debian 12+, Fedora 35+, RHEL 9+, current Arch / openSUSE |
| **Session** | Any X11 or Wayland desktop |
| **Bundled** | Python 3.12, Qt 6 (PySide6-Essentials), the `stalker-gamma` CLI |

The glibc floor is set by the vendored `stalker-gamma` binary and the official Qt wheels — it is not a packaging choice and cannot be lowered without rebuilding both.

**One system package cannot be bundled** without raising that floor, and several distros do not install it by default. If it is missing the AppImage prints per-distro install instructions to the terminal (run it from a terminal if the window never appears). To install it ahead of time:

```bash
sudo apt install libxcb-cursor0        # Debian / Ubuntu
sudo dnf install xcb-util-cursor       # Fedora / RHEL
sudo pacman -S xcb-util-cursor         # Arch
sudo zypper install libxcb-cursor0     # openSUSE
```

To actually *play*, you additionally need a Wine or Proton runner — `umu-run` (recommended), Steam with any Proton, or system Wine — and optionally `gamemoderun`.

### From source

Python **3.10+**, `PySide6 >= 6.6`, a desktop session, and network access on the first `run.sh` execution. `run.sh` creates the virtual environment for you and runs `python -m commander_gui`.

---

## Installation

### Option 1 — AppImage (recommended)

Grab the latest `.AppImage` from [**Releases**](https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases):

```bash
chmod +x STALKER-GAMMA-COMMANDER-*-x86_64.AppImage
./STALKER-GAMMA-COMMANDER-*-x86_64.AppImage
```

Python, Qt and the CLI are inside. A desktop session still needs the system `libxcb-cursor` package listed under [Requirements](#appimage). For a menu entry and icon, use [Gear Lever](https://github.com/mijorus/gearlever) or [AppImageLauncher](https://github.com/TheAssassin/AppImageLauncher); the image ships a valid `.desktop` entry and icon.

### Option 2 — Run from source

```bash
git clone https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER.git
cd STALKER-GAMMA-COMMANDER
./run.sh
```

On first run `run.sh` creates `.venv/`, installs PySide6, and launches the app. The `stalker-gamma` CLI is already bundled at `cli/usr/bin/stalker-gamma`.

To point at a different or newer CLI build:

```bash
STALKER_GAMMA_CLI=/path/to/stalker-gamma ./run.sh
```

---

## First run

1. **Profiles** → fill in *Anomaly path*, *GAMMA path* and *Cache path*, then **Create Profile**. It is activated automatically.
   > Use **absolute** paths. The CLI's defaults (`gamma/anomaly`, …) are relative and resolve against whatever directory the app was started from.
2. **Install** → **Install GAMMA**. If Anomaly is missing, it is installed first. Expect a very large download (~150 GB, or ~100 GB with *Minimal*).
3. **Install** → **Install / Update Runtimes** in the Winetricks panel. The workflow may install protontricks first, then the eight Winetricks verbs.
4. **Play** → pick a runner and launch target, then **Launch Game**, **Open MO2**, or **Launch Anomaly**.

The GAMMA installation can resume an incomplete setup. Use **Updates** for normal addon updates and **Verify Integrity** when checking or repairing installed files.

---

## Configuration

Commander respects `XDG_CONFIG_HOME`; paths below assume the default.

| Path | Owner | Contents |
|---|---|---|
| `~/.config/stalker-gamma/settings.json` | **shared with the CLI** | Profiles: install paths, MO2 profile, download threads, repo URLs and branches |
| `~/.config/stalker-gamma/gui-settings.json` | GUI only | Runner, per-runner prefixes, last target, theme, startup page, font size, GameMode, autostart, launch options, and interrupted-move state |
| `~/.config/stalker-gamma/logs/` | both | CLI logs plus `launcher.log` (rotates at 1 MB) |
| `<gamma>/gamma-md5.txt` | GUI only | MD5 baseline for integrity checking |
| `<gamma>/profiles/<profile>/modlist.txt.gammagui.bak` | GUI only | Pre-edit modlist backup |

`settings.json` is written **atomically**, and any keys a newer CLI adds that this GUI does not understand are **preserved verbatim** on save — so editing profiles here will not clobber CLI-only settings.

### Environment variables

| Variable | Effect |
|---|---|
| `STALKER_GAMMA_CLI` | Absolute path to the `stalker-gamma` binary to drive instead of the bundled one |
| `XDG_CONFIG_HOME` | Relocates the config and log directory |

### Runner reference

| Setting | Meaning | Prefix variable | Default prefix |
|---|---|---|---|
| `auto` | latest discovered GE-Proton through `umu-run` | `WINEPREFIX` | `~/Games/umu/umu-default` |
| `umup:<path>` | selected GE-Proton through `umu-run` with `PROTONPATH` | `WINEPREFIX` | `~/Games/umu/umu-default` |

For a Steam Proton runner the prefix you enter is the **compat-data** directory; the real Wine prefix is its `pfx` subdirectory. Commander resolves this for you when running Winetricks, so the runtimes land where the game will actually look for them.

> Do not switch runners while a prefix is in use, and prefer a separate prefix per runner. Commander stores prefixes per runner for this reason and detects version-mismatch errors in the launch log.

---

## Integrity and repair

**Verify Integrity** (Install page) runs three passes:

1. **Anomaly** — the CLI's `anomaly check`, hashing against `anomaly/tools/checksums.md5`.
2. **G.A.M.M.A. presence** — every enabled mod in the profile's `modlist.txt` must have a non-empty folder under `gamma/mods`. When the official mod list is reachable, results are split into official mods and your own extras.
3. **G.A.M.M.A. content** — every file under `gamma/mods` is MD5-hashed and compared against a baseline stored at `<gamma>/gamma-md5.txt`. The first run records the baseline; later runs report changed, added, removed and unreadable files.

If damaged mods are found and they map to entries in the official modpack list, Commander offers to **repair** them: the mod folder and its cached archive are deleted, then `full-install --skip-extract-on-hash-match` re-downloads and re-extracts only those mods, MD5-verified against the official checksum. Your own added files and mods with no official download source are reported and left untouched.

Cancellation is honoured at every stage — a cancelled repair will not re-baseline the manifest over a known-broken install.

---

## Building the AppImage

```bash
./build-appimage.sh [output-dir]
```

Output defaults to the project root: `STALKER-GAMMA-COMMANDER-<version>-x86_64.AppImage` (approximately 98 MB for the current build). The version comes from `commander_gui/__init__.py`.

### Build requirements

| | |
|---|---|
| **OS** | Any x86_64 Linux with `bash`, `curl`, `find`, `objdump` (binutils) and `ldconfig` |
| **Network** | Yes, on first run — downloads `appimagetool` and a base Python, cached in `.appimage-build/cache/` |
| **Disk** | ~600 MB during the build |
| **Not needed** | Root, FUSE, Docker, or a matching system Python/Qt |

### What it does

1. **Downloads a [python-appimage](https://github.com/niess/python-appimage) `manylinux_2_28` Python 3.12** and extracts it as the AppDir base.

   This is the important part. Building against the *host* interpreter pins the AppImage to the host's glibc — on a rolling-release build machine that is far newer than anything users run, and the result refuses to start almost everywhere. `manylinux_2_28` also matches the ABI target of the official PySide6 wheels.

2. **Installs `PySide6-Essentials`** into that interpreter. The app imports only `QtCore`, `QtGui` and `QtWidgets`, so the full `PySide6` metapackage (WebEngine, Charts, 3D, Multimedia) is never needed.

3. **Prunes everything unreachable** from a QtWidgets-only app: Python bindings for unused Qt modules, QML/Quick, Designer, Sql, Multimedia, WebSockets, the bundled Qt developer tools, `.pyi` stubs, Qt translations, and the stdlib pieces a bundled GUI cannot use (`test`, `idlelib`, `tkinter`, `lib2to3`, `ensurepip`) plus `pip` itself.

   The current build is approximately 217 MB as an AppDir and 98 MB compressed; sizes vary with dependency updates.

4. **Bundles the xcb/xkb helper libraries** Qt needs but distros often omit, into `PySide6/Qt/lib` — already on the `RUNPATH` of `libqxcb.so` and `libQt6XcbQpa.so.6`, so **no `LD_LIBRARY_PATH` is set**. That matters: the `stalker-gamma` subprocess inherits a clean environment and keeps resolving its own `libcurl-impersonate` and `libgit2`.

   Every library copied from the build host is **gated against the glibc floor** (`MAX_GLIBC`, 2.34) and skipped with a warning if it would raise it. `libxcb-cursor.so.0` and `libxkbcommon.so.0` from a rolling distro need glibc 2.38 and are correctly rejected — which is why `libxcb-cursor0` is a documented runtime requirement instead of a bundled file.

5. **Copies the payload** to `opt/stalker-gamma-commander/`, laid out so `config.py`'s `project_root()` resolves `cli/usr/bin/stalker-gamma` with no code changes.

6. **Writes `AppRun`**, the `.desktop` entry and icons, then packages with `appimagetool` (zstd, falling back to gzip).

`AppRun` launches Python with `-s -P`, and deliberately **not** `-E`: `-E` would discard the `PYTHONPATH` pointing at the bundled payload, while `-P` stops a stray `commander_gui` directory in the launch directory from shadowing it.

### Tuning

Edit the settings block at the top of the script:

| Variable | Purpose |
|---|---|
| `PY_SERIES`, `PY_FULL`, `PY_ABI` | Bundled Python version |
| `PY_PLATFORM` | manylinux tag — lower it to widen distro support, if your Qt wheels allow |
| `QT_PACKAGE` | Qt distribution to install |
| `MAX_GLIBC` | ABI ceiling for libraries copied from the build host |
| `BUNDLED_SYS_LIBS` | Which host libraries to attempt to bundle |
| `BUILD_DIR` | Override the build/cache location |

### Verifying a build

```bash
# Highest glibc symbol any bundled binary needs — this is your real floor
./STALKER-GAMMA-COMMANDER-1.2.0-x86_64.AppImage --appimage-extract >/dev/null
find squashfs-root -type f \( -name '*.so*' -o -perm -u+x \) \
  -exec sh -c 'objdump -T "$1" 2>/dev/null | grep -oE "GLIBC_[0-9]+\.[0-9]+"' _ {} \; \
  | sort -uV | tail -1

# Launch offscreen from a neutral directory (should stay running until killed)
cd /tmp && QT_QPA_PLATFORM=offscreen timeout 10 /path/to/*.AppImage; echo "exit=$?"   # 124 = OK
```

---

## Project layout

```text
.
├── run.sh                      # Source launcher: creates .venv, installs PySide6, runs the app
├── build-appimage.sh           # Reproducible AppImage build
├── requirements.txt
├── LICENSE                     # GPL-3.0
├── cli/usr/bin/                # Bundled stalker-gamma CLI + resources (7zz, cloudscraper, certs)
└── commander_gui/
    ├── __main__.py             # python -m commander_gui
    ├── main.py                 # Entry point, palette + global dark stylesheet
    ├── config.py               # CLI binary discovery, config/log path resolution
    ├── settings.py             # settings.json model + atomic I/O (shared with the CLI)
    ├── gui_settings.py         # GUI-only settings, prefixes, autostart, launch state
    ├── autostart.py            # XDG autostart desktop-entry management
    ├── dependencies.py         # Distro, package-manager, and PEP 668 checks
    ├── diagnostics.py          # Diagnostic report generation
    ├── updates.py              # GUI-side GAMMA update checks
    ├── cli_runner.py           # Subprocess runner: streaming output, SIGINT cancellation
    ├── parsers.py              # Serilog output parsers (progress, diffs, mods, prune)
    ├── launcher.py             # GE-Proton/MO2/Wine command building
    ├── proton_installer.py     # GE-Proton release lookup, download and verification
    ├── themes.py               # Theme palettes and global Qt stylesheet
    ├── modlist.py              # modlist.txt reading, grouping and atomic editing
    ├── integrity.py            # Presence check + MD5 baseline scanning
    ├── repair.py               # Modpack list lookup, repair planning, safe deletion
    ├── winetricks.py           # Runtime verbs and prefix status queries
    └── ui/
        ├── main_window.py      # Navigation, settings, global install lock, backdrop
        ├── common.py           # Shared widgets, CommandRunner, background tasks
        ├── dashboard.py  play_page.py     install_page.py  update_page.py
        ├── mod_manager_page.py profiles_page.py utilities_page.py
        ├── settings_page.py    # Appearance, runner, startup, and diagnostics settings
        ├── system_check_page.py # Dependency checks and manual overrides
        └── help_page.py  about_page.py
```

### Architecture notes

- **The CLI is the source of truth.** Long-running commands run in a `QThread`; output lines are streamed to the UI and parsed for progress. Nothing reimplements installer logic.
- **Cancellation** sends `SIGINT` to the CLI so its internal `CancellationToken` unwinds cleanly. Partially downloaded archives stay in the cache and resume next run. Because a cancelled process still reports completion, chained operations explicitly check for cancellation before advancing.
- **One global install lock** (`MainWindow.set_install_busy`) gates every page that spawns a CLI process, so two runs can never write the same install tree. Pages opt in via an `on_busy_changed` hook.
- **Destructive operations** resolve and validate the exact path they will delete, refuse system directories, home directories and symlinks, and never act on a name that escapes its expected parent.
- **All config and modlist writes are atomic** (temp file + rename) and preserve keys the GUI does not model.
- **Runtime setup is staged** — missing protontricks is handled first, followed by the eight Winetricks verbs. Externally managed Python environments are directed to pipx.
- **Runner prefixes are stored per runner** to prevent accidentally reusing a prefix with an incompatible Wine or Proton build.
- **The GUI uses a painted GAMMA-style gradient backdrop** with translucent page content and cards, while controls retain their own theme colors.

### Development

```bash
./run.sh                                    # run from source
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m ruff check commander_gui    # lint (clean)
.venv/bin/python -m compileall -q commander_gui # syntax check
.venv/bin/python -m unittest discover -s tests       # regression tests
```

The GUI can be exercised headlessly with `QT_QPA_PLATFORM=offscreen`. Point `XDG_CONFIG_HOME` at a scratch directory when testing so you never touch a real install. The regression suite covers settings normalization, CLI failure markers, update diffs and archive extraction safety.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `could not load the Qt platform plugin "xcb"` | Install `libxcb-cursor0` / `xcb-util-cursor` (see [Requirements](#appimage)) |
| AppImage will not start at all | Check `ldd --version` — needs glibc 2.34+. Also `chmod +x` the file |
| **CLI not found** on startup | The bundled binary is missing or not executable — `chmod +x cli/usr/bin/stalker-gamma`, or set `STALKER_GAMMA_CLI` |
| MO2 exits immediately or mentions `concrt140.dll` | Install Wine/Proton runtimes into the active prefix from Install → **Install / Update Runtimes** |
| `wine client error: version mismatch` after switching runners | The prefix was built by a different Wine/Proton version. Select the original runner, or configure a separate prefix for the new one |
| Play page has no launch targets | The active profile may point to the wrong GAMMA folder, GAMMA may not be installed, or `ModOrganizer.ini` has no parseable executable. `AnomalyLauncher.exe` is used as a direct-launch fallback when available |
| Mod Manager edits are disabled | MO2 is running; close it first — it rewrites `modlist.txt` on exit |
| Install folders look wrong / files appear in odd places | Profile paths are relative. Set absolute paths in **Profiles** |
| Everything shows "No active profile" | Create and activate one on the **Profiles** page |
| No GE-Proton runner detected or `umu-run` is missing | Install `umu-run` and a GE-Proton build, or install one from Play and refresh the runner list |
| System Check reports missing 32-bit Vulkan | Install the distro's 32-bit Vulkan loader/driver package, then refresh System Check |
| A manual override is ignored | Disable **Detect automatically**, choose a real executable, library or GE-Proton directory, then refresh checks |
| GE-Proton download or checksum verification fails | Retry the download; confirm network access and that the selected GitHub release still provides its SHA-512 asset |
| No compatible GE-Proton build is available | Install a GE-Proton release from Play, or place one under `compatibilitytools.d`, then reload the runner list |
| Winetricks or protontricks installation fails | Install Wine and Winetricks. On PEP 668 systems, install pipx using the displayed package-manager command |
| Settings or autostart changes do not take effect | Check `XDG_CONFIG_HOME` and recreate autostart from Settings if the desktop file was removed |
| An installation move was interrupted | Restart Commander and review the orphan-folder prompt before removing anything |
| A bug report needs diagnostics | Use Settings → **Export diagnostics**, or Utilities → **Create diagnostic archive** |

Logs live in `~/.config/stalker-gamma/logs/`; the Dashboard and Utilities pages both have a button to open it.

---

## Credits

- **[FaithBeam](https://github.com/FaithBeam)** — [`stalker-gamma-cli`](https://github.com/FaithBeam/stalker-gamma-cli), the installer this GUI drives and bundles. All installation, download, checksum and ModDB logic is theirs.
- **[Grokitach](https://github.com/Grokitach)** and the G.A.M.M.A. team — [the mod pack itself](https://github.com/Grokitach/Stalker_GAMMA).
- **[python-appimage](https://github.com/niess/python-appimage)** and **[AppImageKit](https://github.com/AppImage/appimagetool)** — portable packaging.
- **[GSC Game World](https://www.gsc-game.com/)** and the Anomaly team, for the game.

- **[dnttnd](https://github.com/dnttnd)** **Application Tester** - Thank you for testing implementations, dev builds, bug reports, and helping polish the overall UI.


## License

Licensed under the **GNU General Public License v3.0** — see [LICENSE](LICENSE).

This project bundles and drives `stalker-gamma-cli`, which is GPL-3.0, so this front-end is GPL-3.0 as well.

- Copyright for the underlying CLI installer logic: **FaithBeam**
- Copyright for this Python/Qt graphical interface: **SSH-Kitty**

*Not affiliated with GSC Game World or the G.A.M.M.A. development team.*
