<div align="center">

# S.T.A.L.K.E.R. G.A.M.M.A. COMMANDER

**A complete graphical front-end for installing, updating, managing and launching the S.T.A.L.K.E.R. Anomaly + G.A.M.M.A. mod pack on Linux.**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20x86__64-informational)](#requirements)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](#option-2--run-from-source)
[![Qt](https://img.shields.io/badge/GUI-PySide6%20%2F%20Qt%206-41cd52)](https://doc.qt.io/qtforpython-6/)
[![Release](https://img.shields.io/github/v/release/SSH-Kitty/STALKER-GAMMA-COMMANDER?include_prereleases&label=release)](https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases)

</div>

---

<img width="1076" height="927" alt="image" src="https://github.com/user-attachments/assets/5dcb3184-9262-4169-9e23-66c456c89f24" />

## What this is

G.A.M.M.A. is a large S.T.A.L.K.E.R. Anomaly mod pack that is normally installed through a Windows launcher and run through Mod Organizer 2. On Linux the community solution is [**FaithBeam/stalker-gamma-cli**](https://github.com/FaithBeam/stalker-gamma-cli) — an excellent but entirely terminal-driven installer.

**Commander is a desktop GUI around that CLI.** It does not reimplement any installer logic: it drives the real `stalker-gamma` binary as a subprocess and parses its output live. Every download, checksum, ModDB fetch and extraction is performed by the upstream CLI, so results are identical to using it by hand — you just get progress tables, a mod manager, prefix handling and a Play button instead of a terminal.

On top of the CLI it adds things the CLI does not do: launching the game through Mod Organizer in a Wine/Proton prefix, editing `modlist.txt`, installing the Visual C++/DirectX runtimes MO2 needs, and a full MD5 integrity-and-repair pass over your installed mods.

> **Scope:** Linux desktop, x86_64. The underlying CLI also supports Windows, but this GUI's launcher, prefix handling and runner detection are Linux-specific.

---

## Features

### Dashboard
Active profile summary, install status for Anomaly and G.A.M.M.A., Winetricks runtime status, storage usage across your Anomaly/G.A.M.M.A./cache folders, a background update check, and quick-open buttons for each folder and the log directory.

### Play
The front page. Reads the `[customExecutables]` section of your `ModOrganizer.ini` and launches through `ModOrganizer.exe run -e "<target>"` so MO2's virtual file system is active and every G.A.M.M.A. mod is loaded.

- **Auto runner detection** — Steam/UMU Proton (`umu-run`, wrapped in `gamemoderun` when present) is preferred, then any Steam Proton install, then plain Wine.
- **Explicit runner picking** — Steam Protons from `steamapps/common`, umu-managed builds from `compatibilitytools.d` (`GE-Proton`, `UMU-Proton`, …), and Wine builds from Lutris, Bottles or `/opt/wine*`.
- **Per-runner prefixes** — each runner remembers its own prefix, so switching runners does not corrupt a prefix built by another version.
- **Live command preview** with a copy button, an availability chip row, and a direct "launch the vanilla exe without MO2" option.
- The game is started **detached** — closing Commander does not kill your session — with output captured to a rotating `launcher.log`. A failed launch is diagnosed from that log, and a runner/prefix mismatch is called out explicitly instead of surfacing a raw Wine error.

### Install
Full Anomaly and G.A.M.M.A. installation with a **live per-addon progress table** (name, operation, percent), an overall completion bar driven by the CLI's `[done/total]` counter, and clean cancellation.

- Options for `--minimal` (delete archives after extract, ~50 GB saved), and preserving `user.ltx` and MCM settings across a reinstall.
- Optional download-thread override.
- **Winetricks panel** — installs `vcrun2022`, `d3dx9`, `d3dx10`, `d3dx11_43`, `d3dcompiler_43` and `d3dcompiler_47` into the configured prefix. Without the real Visual C++ redistributable MO2 aborts at startup on `concrt140.dll`. Per-verb status is shown on hover.
- **Verify Integrity** — see [Integrity and repair](#integrity-and-repair).

### Update
`update check` with a parsed diff table (Added / Modified / Removed, with archive-name changes), then `update apply` through the same live progress UI. Holds the global install lock so it can never run concurrently with an install.

### Mod Manager
Direct, careful editing of the MO2 profile's `modlist.txt`.

- Mods grouped by the `_separator` category entries G.A.M.M.A. ships, with search.
- Enable, disable, delete and reorder within a category.
- **A backup is taken before the first edit** (`modlist.txt.gammagui.bak`) and can be restored from the UI.
- **Writes are atomic** — a crash or full disk cannot truncate your load order.
- **Edits are blocked while Mod Organizer is running**, because MO2 rewrites the file on exit and would discard them.

### Profiles
Create, edit, activate and delete CLI profiles. Creation, activation and deletion are delegated to `stalker-gamma config` so its side effects (MO2 `selected_profile`, modlist download) happen exactly as the CLI intends. Advanced fields expose every repo URL and branch the CLI supports.

### Utilities
Anomaly integrity check, purge shader cache, delete ReShade, cache prune check/apply with reclaimable-size totals, GOG `fix-install`, open the log folder, and `debug hash-install`.

Plus two guarded destructive actions:
- **Fresh Reset** — wipe the Anomaly and G.A.M.M.A. folders and reinstall both from scratch into the same locations.
- **Full Uninstall** — remove the Anomaly, G.A.M.M.A. and cache folders, leaving your Wine/Proton prefix intact.

Both show an explicit warning listing the exact folders that will be deleted and what you will lose (saves, MO2 settings, MCM settings, any mods you added), refuse to operate on system paths, home directories or symlinks, and re-check that the profile still points where it did before deleting anything.

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

Python **3.10+**, `PySide6 >= 6.6`, and a desktop session. `run.sh` creates the virtual environment for you.

---

## Installation

### Option 1 — AppImage (recommended)

Grab the latest `.AppImage` from [**Releases**](https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases):

```bash
chmod +x STALKER-GAMMA-COMMANDER-*-x86_64.AppImage
./STALKER-GAMMA-COMMANDER-*-x86_64.AppImage
```

Nothing else to install — Python, Qt and the CLI are all inside. For a menu entry and icon, use [Gear Lever](https://github.com/mijorus/gearlever) or [AppImageLauncher](https://github.com/TheAssassin/AppImageLauncher); the image ships a valid `.desktop` entry and icon.

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

1. **Profiles** → fill in *Anomaly path*, *G.A.M.M.A. path* and *Cache path*, then **Create Profile**. It is activated automatically.
   > Use **absolute** paths. The CLI's defaults (`gamma/anomaly`, …) are relative and resolve against whatever directory the app was started from.
2. **Install** → **Install Anomaly**, then **Start Full Install** for G.A.M.M.A. Expect a very large download (~150 GB, or ~100 GB with *Minimal*).
3. **Install** → **Install / Update Runtimes** in the Winetricks panel. MO2 will not start without these.
4. **Play** → pick a runner and launch target, then **Launch Game**.

`Start Full Install` is idempotent — it doubles as the update and repair path, so it stays available after everything is installed.

---

## Configuration

Commander respects `XDG_CONFIG_HOME`; paths below assume the default.

| Path | Owner | Contents |
|---|---|---|
| `~/.config/stalker-gamma/settings.json` | **shared with the CLI** | Profiles: install paths, MO2 profile, download threads, repo URLs and branches |
| `~/.config/stalker-gamma/gui-settings.json` | GUI only | Selected runner, per-runner prefixes, last launch target |
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
| `auto` | umu → Steam Proton → Wine | depends on winner | — |
| `umu` | `umu-run`, `gamemoderun` if present | `WINEPREFIX` | `~/Games/umu/umu-default` |
| `wine` | System `wine` | `WINEPREFIX` | `~/Games/umu/umu-default` |
| Steam Proton *(listed by version)* | `proton run` | `STEAM_COMPAT_DATA_PATH` | `~/Games/proton` |
| `compatibilitytools.d` builds | `umu-run` with `PROTONPATH` | `WINEPREFIX` | `~/Games/umu/umu-default` |
| Lutris / Bottles / `/opt` Wine | that `wine` binary | `WINEPREFIX` | `~/Games/umu/umu-default` |

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

Output defaults to the project root: `STALKER-GAMMA-COMMANDER-<version>-x86_64.AppImage` (~98 MB). The version comes from `stalker_gamma_gui/__init__.py`.

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

   **294 MB → 217 MB AppDir → 98 MB compressed.**

4. **Bundles the xcb/xkb helper libraries** Qt needs but distros often omit, into `PySide6/Qt/lib` — already on the `RUNPATH` of `libqxcb.so` and `libQt6XcbQpa.so.6`, so **no `LD_LIBRARY_PATH` is set**. That matters: the `stalker-gamma` subprocess inherits a clean environment and keeps resolving its own `libcurl-impersonate` and `libgit2`.

   Every library copied from the build host is **gated against the glibc floor** (`MAX_GLIBC`, 2.34) and skipped with a warning if it would raise it. `libxcb-cursor.so.0` and `libxkbcommon.so.0` from a rolling distro need glibc 2.38 and are correctly rejected — which is why `libxcb-cursor0` is a documented runtime requirement instead of a bundled file.

5. **Copies the payload** to `opt/stalker-gamma-commander/`, laid out so `config.py`'s `project_root()` resolves `cli/usr/bin/stalker-gamma` with no code changes.

6. **Writes `AppRun`**, the `.desktop` entry and icons, then packages with `appimagetool` (zstd, falling back to gzip).

`AppRun` launches Python with `-s -P`, and deliberately **not** `-E`: `-E` would discard the `PYTHONPATH` pointing at the bundled payload, while `-P` stops a stray `stalker_gamma_gui` directory in the launch directory from shadowing it.

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
./STALKER-GAMMA-COMMANDER-1.1.0-x86_64.AppImage --appimage-extract >/dev/null
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
└── stalker_gamma_gui/
    ├── __main__.py             # python -m stalker_gamma_gui
    ├── main.py                 # Entry point, palette + global dark stylesheet
    ├── config.py               # CLI binary discovery, config/log path resolution
    ├── settings.py             # settings.json model + atomic I/O (shared with the CLI)
    ├── gui_settings.py         # gui-settings.json, prefix resolution
    ├── cli_runner.py           # Subprocess runner: streaming output, SIGINT cancellation
    ├── parsers.py              # Serilog output parsers (progress, diffs, mods, prune)
    ├── launcher.py             # Runner detection, MO2/Proton/Wine command building
    ├── modlist.py              # modlist.txt reading, grouping and atomic editing
    ├── integrity.py            # Presence check + MD5 baseline scanning
    ├── repair.py               # Modpack list lookup, repair planning, safe deletion
    ├── winetricks.py           # Runtime verbs and prefix status queries
    └── ui/
        ├── main_window.py      # Tab navigation, page registry, global install lock
        ├── common.py           # Shared widgets, CommandRunner, background tasks
        ├── dashboard.py  play_page.py     install_page.py  update_page.py
        ├── mod_manager_page.py profiles_page.py utilities_page.py
        └── help_page.py  about_page.py
```

### Architecture notes

- **The CLI is the source of truth.** Long-running commands run in a `QThread`; output lines are streamed to the UI and parsed for progress. Nothing reimplements installer logic.
- **Cancellation** sends `SIGINT` to the CLI so its internal `CancellationToken` unwinds cleanly. Partially downloaded archives stay in the cache and resume next run. Because a cancelled process still reports completion, chained operations explicitly check for cancellation before advancing.
- **One global install lock** (`MainWindow.set_install_busy`) gates every page that spawns a CLI process, so two runs can never write the same install tree. Pages opt in via an `on_busy_changed` hook.
- **Destructive operations** resolve and validate the exact path they will delete, refuse system directories, home directories and symlinks, and never act on a name that escapes its expected parent.
- **All config and modlist writes are atomic** (temp file + rename) and preserve keys the GUI does not model.

### Development

```bash
./run.sh                                    # run from source
.venv/bin/python -m ruff check stalker_gamma_gui    # lint (clean)
.venv/bin/python -m compileall -q stalker_gamma_gui # syntax check
```

The GUI can be exercised headlessly with `QT_QPA_PLATFORM=offscreen`. Point `XDG_CONFIG_HOME` at a scratch directory when testing so you never touch a real install.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `could not load the Qt platform plugin "xcb"` | Install `libxcb-cursor0` / `xcb-util-cursor` (see [Requirements](#appimage)) |
| AppImage will not start at all | Check `ldd --version` — needs glibc 2.34+. Also `chmod +x` the file |
| **CLI Not Found** on startup | The bundled binary is missing or not executable — `chmod +x cli/usr/bin/stalker-gamma`, or set `STALKER_GAMMA_CLI` |
| MO2 exits immediately, log mentions `concrt140.dll` | Winetricks runtimes are not installed in the prefix — Install page → **Install / Update Runtimes** |
| `wine client error: version mismatch` after switching runners | The prefix was built by a different Wine/Proton version. Select the original runner, or configure a separate prefix for the new one |
| Play page has no launch targets | G.A.M.M.A. is not installed yet, or `ModOrganizer.ini` has no `[customExecutables]` |
| Mod Manager edits are disabled | Mod Organizer is running; close it first — it rewrites `modlist.txt` on exit |
| Install folders look wrong / files appear in odd places | Profile paths are relative. Set absolute paths in **Profiles** |
| Everything shows "No active profile" | Create and activate one on the **Profiles** page |

Logs live in `~/.config/stalker-gamma/logs/`; the Dashboard and Utilities pages both have a button to open it.

---

## Credits

- **[FaithBeam](https://github.com/FaithBeam)** — [`stalker-gamma-cli`](https://github.com/FaithBeam/stalker-gamma-cli), the installer this GUI drives and bundles. All installation, download, checksum and ModDB logic is theirs.
- **[Grokitach](https://github.com/Grokitach)** and the G.A.M.M.A. team — [the mod pack itself](https://github.com/Grokitach/Stalker_GAMMA).
- **[python-appimage](https://github.com/niess/python-appimage)** and **[AppImageKit](https://github.com/AppImage/appimagetool)** — portable packaging.
- GSC Game World and the Anomaly team, for the game.

## License

Licensed under the **GNU General Public License v3.0** — see [LICENSE](LICENSE).

This project bundles and drives `stalker-gamma-cli`, which is GPL-3.0, so this front-end is GPL-3.0 as well.

- Copyright for the underlying CLI installer logic: **FaithBeam**
- Copyright for this Python/Qt graphical interface: **SSH-Kitty**

*Not affiliated with GSC Game World or the G.A.M.M.A. development team.*
