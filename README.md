# S.T.A.L.K.E.R. G.A.M.M.A. Commander

A fully featured graphical desktop front-end for the [STALKER-GAMMA](https://github.com/Grokitach/Stalker_GAMMA) tool. It lets you install, update, and manage your S.T.A.L.K.E.R. Anomaly + GAMMA mod pack seamlessly without opening a terminal.

Built with Python 3 and PySide6 (Qt 6). It drives the proven CLI binary as a subprocess, ensuring all core installer logic, downloads, integrity checks, and ModDB handling match the original implementation perfectly.

---

## ⚖️ License & Open Source Compliance

This project is a graphical interface built on top of [stalker-gamma-cli](https://github.com/FaithBeam/stalker-gamma-cli) developed by **FaithBeam**. 

Because this application bundles, relies on, and drives the original CLI binary (licensed under GPL-3.0), this front-end is also fully open-source under the **GNU General Public License v3.0**. 
* The original copyright for the underlying CLI installer logic belongs to **FaithBeam**.
* The copyright for this Python/Qt graphical interface shell belongs to **SSH-Kitty**.

See the `LICENSE` file in this repository for full details.

---

## ✨ Features

- **Dashboard** — Active profile summary, storage usage visualization, and automatic background update checks.
- **Play** — Launch the game directly through Mod Organizer 2 with all GAMMA mods loaded. It features an auto-detected runner: Steam/UMU Proton (`umu-run` + `gamemoderun` using the `~/Games/umu/umu-default` prefix) is prioritized, with plain Wine as a fallback. Also includes options to launch MO2 itself or run the vanilla game executable.
- **Install** — Full GAMMA and Anomaly setup with a live per-addon progress table, overall completion bar, live console logging, and clean cancellation.
- **Update** — Check for updates with detailed diffs (Added / Modified / Removed) and apply them using the live progress UI.
- **Mod Manager** — Browse MO2 profiles, view active mods, change selected profiles, and enable, disable, or delete individual mods.
- **Profiles** — Create, edit, activate, and delete CLI profiles seamlessly. Profiles are shared with the CLI via `~/.config/stalker-gamma/settings.json`.
- **Utilities** — Run Anomaly integrity checks, purge shader-caches, clean up ReShade, prune old caches, apply GOG fix-installs, view logs, and run install hashing.

## 📋 Requirements

- **Python 3.10+**
- **A Desktop Environment** (The application is Qt-based)
- **The Bundled CLI** — Located under `cli/usr/bin/stalker-gamma` (already included in this package)

## 🚀 How to Run

### Option 1 — AppImage (recommended)

Download the `.AppImage` from [Releases](https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases), make it executable and run it. Python, Qt and the `stalker-gamma` CLI are all bundled — nothing else to install.

```bash
chmod +x STALKER-GAMMA-COMMANDER-*-x86_64.AppImage
./STALKER-GAMMA-COMMANDER-*-x86_64.AppImage
```

**Requirements:** x86_64, **glibc 2.34 or newer** (Ubuntu 22.04+, Debian 12+, Fedora 35+, RHEL 9+, any current Arch/openSUSE) and a desktop session.

One system package cannot be bundled without breaking that glibc floor, and some distros do not install it by default. If the app reports it is missing:

```bash
sudo apt install libxcb-cursor0        # Debian / Ubuntu
sudo dnf install xcb-util-cursor       # Fedora / RHEL
sudo pacman -S xcb-util-cursor         # Arch
sudo zypper install libxcb-cursor0     # openSUSE
```

### Option 2 — From source

Execute the main launcher script from your terminal:

```bash
./run.sh
```

*Note: On your very first run, the script will automatically create a local Python virtual environment (`venv`) and install `PySide6`.*

### Building the AppImage yourself

```bash
./build-appimage.sh [output-dir]
```

The script downloads a [python-appimage](https://github.com/niess/python-appimage) **manylinux_2_28** Python rather than using your system interpreter — building against a rolling-release host would pin the result to that host's glibc and it would refuse to start almost everywhere. It then installs `PySide6-Essentials`, strips every Qt module unreachable from a QtWidgets-only app (~150 MB), and packages the result with `appimagetool`. Any library copied from the build host is gated on the glibc floor and skipped (with a warning) if it would raise it.

### Using a Custom CLI Binary
If you wish to point the GUI toward a different or manually updated CLI binary, specify it using the `STALKER_GAMMA_CLI` environment variable:

```bash
STALKER_GAMMA_CLI=/path/to/stalker-gamma ./run.sh
```

## 📂 Project Layout

```text
Project/
├── run.sh                          # Launcher script (manages venv & PySide6)
├── requirements.txt
├── cli/usr/bin/                    # Bundled stalker-gamma CLI + essential resources
└── stalker_gamma_gui/
    ├── main.py                     # App entry point + global dark theme
    ├── config.py                   # Path resolution
    ├── settings.py                 # settings.json model data & I/O
    ├── gui_settings.py             # GUI preferences (launch runners, custom variables)
    ├── launcher.py                 # Game execution engine (MO2, umu-run, wine)
    ├── cli_runner.py               # Subprocess runner (handles threading & SIGINT cancel)
    ├── parsers.py                  # CLI string output parsers (progress, diffs, mods)
    ├── modlist.py                  # MO2 modlist.txt direct configuration editing
    └── ui/                         # Qt view pages
```

## ⚠️ Notes & Technical Caveats

- **Shared Configuration:** Core profile configurations and experimental ModDB server options are completely shared with the upstream CLI inside `~/.config/stalker-gamma/settings.json`. The GUI manages its own specific preferences (like wine prefixes or preferred runners) safely inside `gui-settings.json`.
- **Game Execution Flow:** The Play page reads MO2's configured executables out of `ModOrganizer.ini` and triggers the game using `ModOrganizer.exe run -e <title>` to ensure MO2's virtual file system (VFS) hooks in correctly. The game process is started detached; closing this GUI will not interrupt your game session.
- **Progress Monitoring:** App UI progress is actively read from the CLI's standard output streams (sampled every 200 ms). The per-addon percentage tracks individual network/extraction states, while the dashboard tracker reflects completed tasks out of the aggregate total.
- **Safe Aborts:** Cancelling an installation fires a safe `SIGINT` signal directly down to the CLI process, triggering its internal `CancellationToken`. Partially downloaded archives remain safe inside your cache folder to resume instantly next time.
- **Platform Limitations:** This GUI is currently tailored and tested specifically for **Linux/macOS** environments. While the underlying CLI natively supports Windows, the local launcher workflow (`run.sh`) focuses on Unix environments.
