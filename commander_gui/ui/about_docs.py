"""The COMMANDER documentation shown on the About page.

Plain English data: ``about_page.py`` runs every visible string through
``tr()`` when it renders a topic, so each one needs a locale entry (code
blocks and live keys excepted).

A topic is a dict with ``title``, ``summary``, ``page`` (nav key for an
"Open" button, or None) and ``sections``: ``[(heading, blocks)]``. A block is
one of:

* ``"text"`` - a paragraph;
* ``("list", [text, ...])`` - bullet points;
* ``("steps", [text, ...])`` - numbered steps;
* ``("table", [(term, text), ...])`` - a term and its explanation;
* ``("code", text)`` - a command or file snippet, with a Copy button;
* ``("note", text)`` / ``("warn", text)`` - a highlighted tip or caution;
* ``("live", [(label, key), ...])`` - values read from this machine, keyed
  into ``about_page.live_values()``;
* ``("widget", name)`` - one of the page's own widgets ("update", "links").
"""

from __future__ import annotations

TOPICS: list[dict] = [
    {
        "title": "Overview",
        "summary": "What COMMANDER is, what it is made of, and how this documentation is organised.",
        "page": None,
        "sections": [
            (
                "What COMMANDER is",
                [
                    "STALKER GAMMA COMMANDER is a Linux desktop application for installing, updating, launching, repairing and managing S.T.A.L.K.E.R. Anomaly with the GAMMA modpack. GAMMA is normally installed on Windows with its own launcher and played through Mod Organizer 2. On Linux, the community answer is FaithBeam's stalker-gamma-cli, an installer that runs in a terminal. COMMANDER is a graphical front-end for that CLI, plus everything around it that the CLI does not do on its own.",
                    "COMMANDER does not reimplement the installer. Every download, checksum check and archive extraction is done by the real stalker-gamma binary, which COMMANDER starts as a separate process and whose output it reads line by line. That means an install made with COMMANDER is exactly the same as one made with the CLI by hand: you can switch between the two at any time, and they share the same settings file.",
                    "On top of the CLI, COMMANDER adds: launching through Mod Organizer 2 inside a Wine/Proton prefix, a GE-Proton downloader, installing the Windows runtime libraries the game needs, a mod list editor with backups, FOMOD installers, integrity checks with automatic repair, save and settings backups, co-op through xrRazom, a controller-friendly Steam Deck interface and a log analyser.",
                    ("widget", "update"),
                ],
            ),
            (
                "The parts of COMMANDER",
                [
                    ("table", [
                        ("Desktop interface", "The main window you are looking at: the tabs along the top (Dashboard, System Check, Install, Play, Multiplayer, Updates, Mod Manager, Profiles, Utilities, Help) and Settings behind the cog."),
                        ("Steam Deck Mode", "A second, separate interface built for 1280x800 screens, controllers and touch. It uses the same profiles, settings and game install as the desktop interface. See the Steam Deck Mode topic."),
                        ("stalker-gamma CLI", "FaithBeam's installer, bundled inside COMMANDER. It owns installing Anomaly and GAMMA, updates, cache pruning, profiles in settings.json and a few maintenance commands."),
                        ("ASSISTANT", "A bundled log analyser. It opens the log dumps COMMANDER makes and explains crashes and errors in plain words. See the ASSISTANT topic."),
                    ]),
                ],
            ),
            (
                "Using this documentation",
                [
                    "This Help page is the full COMMANDER documentation: what each page and button does, how each feature works underneath, what it changes on disk, where every file lives, what COMMANDER sends over the network, how it protects your data, and how to build and contribute to it.",
                    "Use the search box on the left to find any word in any topic. Click a section heading to fold it away or open it again, or use Expand all. The Files and folders topic, and several others, show live values read from your own machine, with buttons to copy them or open the folder.",
                    ("note", "This documentation, like the rest of COMMANDER, is available in all ten of its languages. Switch language in the status bar at the bottom of the window."),
                ],
            ),
            (
                "This installation",
                [
                    ("live", [
                        ("COMMANDER version", "version"),
                        ("How COMMANDER was installed", "packaging"),
                        ("Program folder", "project_root"),
                        ("Python", "python"),
                        ("Qt / PySide6", "qt"),
                        ("Active profile", "profile"),
                    ]),
                ],
            ),
            (
                "Links",
                [
                    ("widget", "links"),
                ],
            ),
        ],
    },
    {
        "title": "Installing COMMANDER",
        "summary": "Requirements, the three ways to install COMMANDER, and how it updates itself.",
        "page": None,
        "sections": [
            (
                "Requirements",
                [
                    ("table", [
                        ("Operating system", "Linux on x86_64, in an X11 or Wayland desktop session, or SteamOS Game Mode on a Steam Deck."),
                        ("AppImage", "glibc 2.34 or newer. Python, Qt and the CLI are bundled inside, so nothing else needs installing to open COMMANDER."),
                        ("From source", "Python 3.10 or newer and PySide6 6.6 or newer (below 7). run.sh installs PySide6 into a private virtual environment for you. Network access is needed the first time."),
                        ("To play", "A Vulkan-capable graphics driver, including the 32-bit Vulkan driver, and a way to run Windows programs: umu-run with GE-Proton is the recommended one. System Check lists exactly what is missing."),
                        ("Disk space", "About 150 GB for Anomaly, GAMMA and the download cache, or about 100 GB with the Minimal install option. A GE-Proton build is about 1.5 GB more."),
                        ("Optional", "GameMode (gamemoderun) for better performance and MangoHud for an on-screen performance overlay. Both are used only when installed."),
                    ]),
                    ("warn", "If COMMANDER says it could not load the Qt platform plugin xcb, install libxcb-cursor0 (Debian, Ubuntu) or xcb-util-cursor (Fedora, Arch) with your package manager."),
                ],
            ),
            (
                "AppImage (recommended)",
                [
                    "Download the newest .AppImage from the Releases page on GitHub, make it executable, and run it. Everything COMMANDER needs is inside the one file. For a menu entry and icon you can use Gear Lever or AppImageLauncher, though COMMANDER also writes its own menu entry the first time it starts.",
                    ("code", "chmod +x STALKER-GAMMA-COMMANDER-*-x86_64.AppImage\n./STALKER-GAMMA-COMMANDER-*-x86_64.AppImage"),
                    "Every release also has a .sha512sum file next to the AppImage. You can check your download with it, and COMMANDER's own updater checks it automatically.",
                    ("code", "sha512sum -c STALKER-GAMMA-COMMANDER-*-x86_64.AppImage.sha512sum"),
                ],
            ),
            (
                "Arch Linux (AUR)",
                [
                    "A PKGBUILD lives in packaging/aur. It installs COMMANDER system-wide with a menu entry for both the normal interface and Deck Mode. Updates come through your AUR helper or makepkg, never through COMMANDER's own updater, so pacman always knows which files it owns.",
                    ("code", "git clone https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER.git\ncd STALKER-GAMMA-COMMANDER/packaging/aur\nmakepkg -si"),
                ],
            ),
            (
                "From source",
                [
                    "run.sh creates a .venv folder inside the checkout, installs the packages in requirements.txt into it (only again when that file changes), and starts COMMANDER. Add --deck to start straight in Steam Deck Mode. The CLI is already in the repository at cli/usr/bin/stalker-gamma.",
                    ("code", "git clone https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER.git\ncd STALKER-GAMMA-COMMANDER\n./run.sh"),
                    "Set PYTHON to use a specific interpreter, for example PYTHON=python3.12 ./run.sh. If the Python version changes, run.sh rebuilds the virtual environment by itself.",
                    ("note", "run.sh is the only way to run more than one COMMANDER at once (see Command line and environment). Each extra window says [extra instance] in its title."),
                ],
            ),
            (
                "Updating COMMANDER itself",
                [
                    "COMMANDER checks GitHub for a newer release of itself when it starts and then every 12 hours. When one is out, the bar at the bottom of the window says so, the Help page shows an update button, and (if Update notifications is on in Settings) you get one desktop notification per new version.",
                    "Only the AppImage can update itself. Clicking the update downloads the new AppImage into the same folder as the one you are running, then replaces it in one step and restarts. The download is refused unless all of these are true:",
                    ("list", [
                        "It is complete: exactly as many bytes as the server announced. A dropped connection never replaces your working copy.",
                        "It really is an AppImage: an ELF program with the AppImage type-2 marker, not an error page.",
                        "It matches the release's published .sha512sum file, when the release has one.",
                    ]),
                    "The AUR package and a source checkout are updated the normal way (your package manager, or git pull) and only show that an update exists.",
                ],
            ),
            (
                "Stable and unstable builds",
                [
                    "Settings has a COMMANDER build choice. Stable follows normal releases. Unstable also follows pre-releases, which get new features first and may have bugs. Switching from unstable back to stable installs the newest stable release even if it is older than the build you are on. Like updates, switching only works in the AppImage.",
                ],
            ),
        ],
    },
    {
        "title": "Key concepts",
        "summary": "Anomaly, GAMMA, Mod Organizer, load order, profiles, runners and Wine prefixes, explained properly.",
        "page": None,
        "sections": [
            (
                "Anomaly and GAMMA",
                [
                    "S.T.A.L.K.E.R. Anomaly is a free, standalone game built on the X-Ray engine from the original S.T.A.L.K.E.R. games. It does not need any other S.T.A.L.K.E.R. game to be installed.",
                    "G.A.M.M.A. (GAMMA) is a modpack by Grokitach: several hundred Anomaly mods, picked, patched and ordered to work together. GAMMA does not change the Anomaly folder much. Instead, its mods live in a separate GAMMA folder and are loaded on top of Anomaly by Mod Organizer 2 when the game starts. GAMMA also replaces a few Anomaly files directly, such as the engine executables.",
                    "That is why a profile always has two game folders: Anomaly (the base game) and GAMMA (Mod Organizer and all the mods).",
                ],
            ),
            (
                "Mod Organizer 2 and its virtual file system",
                [
                    "Mod Organizer 2 (MO2) is a Windows mod manager. It keeps every mod in its own folder under gamma/mods, and when it starts the game it shows the game a merged view of Anomaly plus every enabled mod, without copying or changing any game files. This merged view is the virtual file system (USVFS). Turn a mod off and it is simply left out of the view next time.",
                    "COMMANDER starts the game through MO2 (ModOrganizer.exe run -e \"<target>\"), so the virtual file system is active and GAMMA's mods are loaded. Launch Anomaly on the Play page is the exception: it starts the game exe directly, without MO2 and without any mods, which is useful for testing.",
                    "Files that the game or a mod writes while running, and which belong to no mod, end up in gamma/overwrite.",
                ],
            ),
            (
                "Mod list and load order",
                [
                    "Each MO2 profile has a modlist.txt: one line per mod. A line starting with + is an enabled mod, - is a disabled one, and # lines are comments. Lines ending in _separator are not mods but category headers, like GAMMA's numbered sections.",
                    "When two enabled mods contain the same file, the one that loads later wins. MO2's interface shows the list so that the winner is lower down. modlist.txt itself is stored the other way round (highest priority first). COMMANDER shows the list exactly like MO2 does, so you never have to think about this.",
                    ("warn", "The order of GAMMA's own mods matters. Moving them around can stop the game starting or break saves. Your own mods go into the Custom Mods category at the bottom, so they win over GAMMA's."),
                ],
            ),
            (
                "COMMANDER profiles and MO2 profiles",
                [
                    "These are two different things with similar names.",
                    ("table", [
                        ("COMMANDER profile", "Stored in the CLI's settings.json. It records where Anomaly, GAMMA and the download cache are, which MO2 profile to use, how many download threads to use, and which repositories to download GAMMA from. The active profile is the one every page works on."),
                        ("MO2 profile", "A folder under gamma/profiles with its own modlist.txt (and its own MO2 ini files). Several MO2 profiles inside one GAMMA install share the same mod files but have different mod lists. GAMMA's default is called G.A.M.M.A."),
                    ]),
                    "So you can have two COMMANDER profiles that point at the same GAMMA folder but use different MO2 profiles, or two COMMANDER profiles for two completely separate installs.",
                ],
            ),
            (
                "Runners, Proton and Wine prefixes",
                [
                    "Anomaly and MO2 are Windows programs. A runner is what lets them run on Linux. Wine translates Windows calls into Linux ones; Proton is Valve's version of Wine with extra parts for games (such as DXVK, which turns Direct3D into Vulkan); GE-Proton is a community build of Proton with more fixes, and is what COMMANDER recommends. umu-run is a small launcher that runs a Proton build outside Steam the way Steam would.",
                    "A Wine prefix is a folder that acts as a small, private Windows installation: a C: drive, a registry, and Windows system files. The game's Windows runtime libraries (DirectX, Visual C++) are installed into it by Install Dependencies.",
                    "A prefix belongs to the Wine build that created it. If a different Wine build is pointed at it, it 'updates' the prefix and overwrites the system files with its own. The game then crashes the moment it starts. COMMANDER is built around never letting that happen (see Runners and Wine prefixes).",
                ],
            ),
            (
                "The download cache",
                [
                    "The cache folder keeps every archive the CLI downloaded. Updates, repairs and reinstalls check each cached archive's MD5 against GAMMA's official list and reuse it when it matches, so only new or changed mods are downloaded again. The Minimal option deletes archives after unpacking to save about 50 GB, at the cost of re-downloading them later.",
                ],
            ),
        ],
    },
    {
        "title": "First run walkthrough",
        "summary": "From a fresh download to playing, step by step, with what each step does underneath.",
        "page": "install",
        "sections": [
            (
                "Step by step",
                [
                    ("steps", [
                        "Open System Check. Install anything shown as Missing with the copy-command button next to it, then press Refresh Checks. The minimum is a runner (umu-run is best), Vulkan including 32-bit, and unzip.",
                        "Open Install. Pick an empty base folder on a drive with about 150 GB free and press Create folders. COMMANDER makes anomaly, gamma and cache folders inside it and saves them to your profile.",
                        "Press Install Dependencies. COMMANDER installs any missing tools first (umu-run, winetricks), then installs the Windows runtime libraries into your runner's Wine prefix. This is about 150 MB and only needs doing once per prefix.",
                        "Press Install GAMMA. If Anomaly is not installed yet, it is installed first (stalker-gamma anomaly install), then GAMMA (stalker-gamma full-install). This downloads many gigabytes and can take a few hours. You can pause, cancel and resume.",
                        "When it finishes, COMMANDER takes you to Play. Keep the runner on Auto-detect. If you have no GE-Proton yet, press Manage GE-Proton and download the newest one.",
                        "Press Launch Game. The first start is slow while shaders are built.",
                    ]),
                    ("note", "Each page unlocks as the step before it is done, so greyed-out buttons usually mean an earlier step is missing."),
                ],
            ),
            (
                "Using an existing install",
                [
                    "If you already installed GAMMA with the CLI, COMMANDER finds the profile in ~/.config/stalker-gamma/settings.json and uses it straight away. If you installed it somewhere else, create a profile on Profiles that points at your existing Anomaly and GAMMA folders. Nothing is downloaded until you ask for an install or update.",
                ],
            ),
            (
                "How long and how big",
                [
                    ("table", [
                        ("Disk space", "About 150 GB for Anomaly, GAMMA and the download cache together. With Minimal, about 100 GB."),
                        ("Time", "Mostly depends on your connection and on moddb's download servers. A few hours is normal."),
                        ("Download threads", "How many files download at once (Dashboard or Profiles): 4 is safe, 6 is balanced (the default), 8 is fast. Lower it if downloads keep failing."),
                    ]),
                ],
            ),
        ],
    },
    {
        "title": "Installing Anomaly and GAMMA",
        "summary": "Exactly what the Install page runs, what each option changes, and how failures are handled.",
        "page": "install",
        "sections": [
            (
                "The commands COMMANDER runs",
                [
                    "Every install goes through the bundled CLI. COMMANDER adds --progress-update-interval-ms 200 so it can draw the progress table smoothly, and reads each progress line (addon name, operation, percent and position) as it arrives.",
                    ("table", [
                        ("Install Anomaly", "stalker-gamma anomaly install"),
                        ("Verify Anomaly", "stalker-gamma anomaly check (compares every file against anomaly/tools/checksums.md5)"),
                        ("Install GAMMA", "stalker-gamma full-install plus the flags chosen below"),
                        ("Repair", "stalker-gamma full-install --skip-extract-on-hash-match --skip-github-downloads --preserve-user-settings --preserve-mcm-settings"),
                    ]),
                    "Only one install, update, repair or move can run at a time across the whole app. The other pages' install buttons are disabled while one runs, and Mod Organizer must be closed.",
                ],
            ),
            (
                "Install options",
                [
                    ("table", [
                        ("Preserve user.ltx (--preserve-user-settings)", "Keeps Anomaly's user.ltx: graphics, sound, controls and keybinds. Off means it is replaced with GAMMA's default."),
                        ("Preserve MCM settings (--preserve-mcm-settings)", "Keeps every mod's settings from the Mod Configuration Menu (axr_options.ltx). Off means they are reset."),
                        ("Minimal (--minimal)", "Deletes each archive from the cache after it is unpacked. Saves about 50 GB, but every later update or repair has to download those archives again."),
                        ("Re-download GitHub addons", "Without this, COMMANDER passes --skip-github-downloads so archives hosted on GitHub are reused from the cache (they have no MD5 in GAMMA's list, so the CLI would otherwise download every one again). A missing archive is always downloaded."),
                        ("Auto-retry on large file pack failures", "GAMMA's large-files repository on GitHub sometimes fails part way. With this on, COMMANDER retries it up to 10 times by itself."),
                        ("Keep my current engine executables", "GAMMA puts its own engine exes into the Anomaly folder. Tick this only if you installed different (for example newer, modded) exes on purpose and want to keep them."),
                        ('Offline install (--offline)', 'Installs from the offline cache archive, passing --mod-pack-maker-path and --mod-list-path so nothing is downloaded. Extract the archive into the folder that contains your cache folder: the modlist.txt and modpack_maker_list.txt it holds are found automatically, or you can choose them yourself. Minimal, Re-download GitHub addons and Auto-retry are turned off while it is on.'),
                    ]),
                    ("warn", "Install options are not remembered between runs. Check them every time."),
                ],
            ),
            (
                "What COMMANDER keeps for you",
                [
                    "full-install and update apply both rewrite things that belong to you. COMMANDER takes a snapshot before the CLI starts and puts it back after it succeeds (on failure or cancel the snapshot is just discarded):",
                    ("list", [
                        "modlist.txt: the CLI replaces it with GAMMA's stock list. COMMANDER re-applies your enabled/disabled choices and re-adds your own mods under Custom Mods, as long as their folders still exist.",
                        "ModOrganizer.ini: MO2's own [Settings] (its theme and preferences), the executable picked in MO2's dropdown, and each executable's arguments (for example DX11 instead of DX11-AVX, or -dbg).",
                        "Your engine executables, when Keep my current engine executables is ticked.",
                        "A settings backup of user.ltx and MCM settings is also written before the job starts (see Backups).",
                    ]),
                ],
            ),
            (
                "Pause, cancel and resume",
                [
                    "Pause freezes the CLI process; Resume continues it. Cancel stops it cleanly (it gets an interrupt signal first, and is only killed if it does not stop). Downloads that already finished stay in the cache.",
                    "If an install fails or is cancelled, COMMANDER remembers which folders it was installing into. Next time the Install button becomes Resume GAMMA Installation. The CLI then checks every cached archive's MD5 and only downloads what is missing or changed. The resume state follows the folders, not the profile name, so renaming a profile does not lose it.",
                    "If a cached archive fails its MD5 check, the CLI downloads it again and COMMANDER says so in the status line, so you know why something is being fetched twice.",
                ],
            ),
            (
                "When an install fails",
                [
                    "COMMANDER reads the end of the CLI's output and explains the failure instead of showing a raw error:",
                    ("table", [
                        ("Cloudflare challenge", "moddb served a 'Just a moment...' page instead of the file. This is a block on too many requests. Wait a while (or use a VPN), then Resume."),
                        ("Network failure", "SSL errors, git clone errors, timeouts, 'could not resolve host' and similar. Check your connection, then Resume."),
                        ("Rate limit", "GitHub limits how often one address can download. Waiting is the only fix."),
                        ("Low disk space", "COMMANDER warns before starting when the target drive is low. Free space or use Minimal."),
                    ]),
                    "Show Console shows the CLI's full output. The CLI also keeps its own daily logs, which a log dump includes.",
                ],
            ),
        ],
    },
    {
        "title": "Dependencies and System Check",
        "summary": "What the game needs from your system, what Install Dependencies installs, and how System Check decides what is missing.",
        "page": "systemcheck",
        "sections": [
            (
                "Install Dependencies",
                [
                    "A fresh Wine or Proton prefix does not contain Microsoft's DirectX and Visual C++ runtime libraries; Wine only provides stand-ins. MO2 refuses to start without the real Visual C++ runtime (the classic concrt140.dll error), and the game needs the DirectX ones. Install Dependencies installs these winetricks verbs, in order:",
                    ("code", "d3dcompiler_43 d3dcompiler_47 d3dx10 d3dx11_43 d3dx9 quartz dx8vb vcrun2022"),
                    "Before that, it installs any missing tools: umu-run (a pinned, known-good release, version 1.4.4) and winetricks (a pinned release, 20260125, checked against a known SHA-256 before use).",
                    ("warn", "The verbs are always installed through your selected runner's own Wine: umu-run winetricks for GE-Proton, or that runner's own wine binary otherwise. Never the system wine or winetricks on your PATH. A different Wine build touching a Proton prefix overwrites its system DLLs and makes every launch crash. COMMANDER refuses to do it."),
                    "Checking which runtimes are installed is a plain file read of the prefix, never a winetricks command, for the same reason.",
                ],
            ),
            (
                "What System Check looks at",
                [
                    ("table", [
                        ("stalker-gamma CLI", "That the bundled binary exists and runs."),
                        ("Wine, Winetricks, Protontricks, umu-run", "Each is found automatically. You only need one way to run the game, so once Steam, Wine or umu-run is found, the others show Optional."),
                        ("Vulkan", "Your graphics driver must support Vulkan, and the 32-bit Vulkan loader must also be present. The card name is shown."),
                        ("Runtime libraries", "How many of the eight verbs above are in your prefix. Hover for the list."),
                        ("Proton builds", "Every GE-Proton build found in Steam's compatibilitytools.d folders."),
                        ("GameMode and MangoHud", "Optional extras."),
                    ]),
                    "For anything missing, COMMANDER detects your distribution and package manager (and your GPU vendor for Vulkan drivers) and gives you the exact install command to copy into a terminal.",
                ],
            ),
            (
                "Manual overrides",
                [
                    "If a tool lives somewhere COMMANDER does not look (for example a Steam library on another drive), untick Detect automatically and browse to it. The path is saved in gui-settings.json under tool_overrides. A wrong path shows the tool as Missing; clear the box to go back to automatic detection.",
                ],
            ),
        ],
    },
    {
        "title": "Runners and Wine prefixes",
        "summary": "How COMMANDER picks a runner, where each runner's prefix lives, and how it protects prefixes from damage.",
        "page": "play",
        "sections": [
            (
                "Runner choices",
                [
                    ("table", [
                        ("Auto-detect (auto)", "The recommended choice. Uses umu-run with the newest GE-Proton (wrapped in gamemoderun when GameMode is installed). Without umu-run it falls back to Steam's Proton, then to plain Wine."),
                        ("A specific GE-Proton", "Runs that build through umu-run, using PROTONPATH to tell umu which build."),
                        ("Steam Proton", "Any Proton installed through Steam (steamapps/common/Proton*), run with 'proton run'. It works, but MO2 is less reliable on Valve's builds."),
                        ("Wine", "Plain system Wine with WINEPREFIX set."),
                    ]),
                    "The Default runner in Settings is used unless you pick another one on Play. The current choice is shown on the Dashboard too.",
                ],
            ),
            (
                "Where prefixes live",
                [
                    "Each runner remembers its own prefix, so switching runners never points one Wine at a prefix made by another. umu-run and Wine use ~/Games/umu/umu-default unless you choose another folder. Steam Proton uses a compat-data folder under ~/Games/proton. Your choice per runner is saved in gui-settings.json under prefixes.",
                    ("live", [
                        ("Saved runner", "runner"),
                        ("Wine prefix for that runner", "prefix"),
                    ]),
                    ("note", "A new runner means a new prefix, so Install Dependencies has to be run again after switching."),
                ],
            ),
            (
                "Prefix protection",
                [
                    "Before every launch, COMMANDER checks the prefix's ntdll.dll and other system files. If they were written by a different Wine build on this machine, it refuses to start the game. Such a prefix does not fail cleanly: every Windows process crashes, each crash starts the Wine debugger, which crashes too, and within minutes the computer runs out of memory.",
                    "Instead you get a message with Repair now. Repair puts the runner's own copies of those DLLs back over the foreign ones. Run Install Dependencies again afterwards, because the runtime libraries are among the files a foreign Wine overwrites.",
                    "While the game runs, COMMANDER also watches launcher.log for a crash loop (many 'starting debugger...' lines) and stray winedbg processes belonging to this prefix, and stops them before they pile up.",
                    "Utilities also has Repair Wine prefix and Use the default Wine prefix (which stops using a custom prefix folder without deleting it).",
                ],
            ),
            (
                "GE-Proton manager",
                [
                    "Play > Manage GE-Proton lists recent GE-Proton releases from GitHub. Download fetches the build into a temporary folder on the same drive (not /tmp, which is often too small), checks its SHA-512 checksum against the release's own checksum file, and only then unpacks it into ~/.local/share/Steam/compatibilitytools.d (or the Steam folder set in System Check's overrides), where Steam can use it too. Downloads can be cancelled.",
                    "Remove deletes a build, but refuses when it is the build COMMANDER launches the game with, or when the folder is not a Proton build inside compatibilitytools.d. Builds a Steam game is set to use are marked and left out of Remove unused.",
                ],
            ),
        ],
    },
    {
        "title": "Playing and launching",
        "summary": "How the launch command is built, launch options, logs, crash detection, playtime and shortcuts.",
        "page": "play",
        "sections": [
            (
                "What Launch Game does",
                [
                    ("steps", [
                        "Checks that GAMMA is installed and that co-op or singleplayer matches what you are launching.",
                        "Checks the load order. If it has been reversed end to end (which crashes the game), it offers Fix and launch, taking a backup first.",
                        "Checks the Wine prefix (see Runners and Wine prefixes).",
                        "Builds the command: runner wrapper, then ModOrganizer.exe -p \"<MO2 profile>\" run -e \"<launch target>\". The working folder is the GAMMA folder.",
                        "Starts it detached in its own process group, so closing COMMANDER does not close the game. All output goes to launcher.log.",
                        "Watches the game: playtime, crash detection and Discord Rich Presence.",
                    ]),
                    "Open MO2 runs the same command without run -e, so MO2 opens and you start things from it. Launch Anomaly runs the target's exe directly with no MO2 and no mods.",
                ],
            ),
            (
                "Launch targets",
                [
                    "Targets come from the [customExecutables] section of GAMMA's ModOrganizer.ini, the same list as MO2's dropdown. COMMANDER keeps both in step: picking a target in COMMANDER selects it in MO2 too. The default is MO2's own selection, then your last choice, then Anomaly (DX11-AVX), Anomaly (DX11) or Anomaly Launcher.",
                ],
            ),
            (
                "Launch options",
                [
                    "The launch options box works like Steam's:",
                    ("table", [
                        ("NAME=value", "Sets an environment variable for the game, for example PROTON_USE_WINED3D=1 or DXVK_HUD=fps."),
                        ("words before %command%", "Wrap the game, for example mangohud %command% or gamemoderun %command%."),
                        ("words after %command%", "Are passed to the game itself."),
                    ]),
                    ("code", "DXVK_HUD=fps mangohud %command%"),
                    "The quick toggles add common ones for you: MangoHud, an FPS counter, and No Esync / No Fsync (which can help if the game freezes or stutters). Always use GameMode wraps every launch in gamemoderun. The live preview shows the exact command, with a copy button.",
                ],
            ),
            (
                "A clean environment for the game",
                [
                    "The game is started with an environment cleaned of any WINE*, PROTON* and STEAM_COMPAT_* variables COMMANDER inherited, so a variable left over from another program cannot point the game at the wrong Wine. Variables whose names contain TOKEN, PASSWORD, SECRET or API_KEY are removed too; launcher.log records which were removed.",
                    "When COMMANDER itself runs as a Steam shortcut (always the case in Game Mode), the game is given COMMANDER's Steam app ID. That is what lets Steam apply the right controller layout and gamescope focus the game window.",
                    "When running as an AppImage, variables that point inside the AppImage's temporary mount are also removed, so the game and the tools it starts do not break when COMMANDER closes.",
                ],
            ),
            (
                "When a launch goes wrong",
                [
                    "COMMANDER reads launcher.log and names the problem:",
                    ("table", [
                        ("Runner and prefix mismatch", "Wine version mismatch, wrong wineserver, invalid prefix version, or a concrt140.dll error. Fix: use the runner that made the prefix, or repair it and run Install Dependencies."),
                        ("Graphics", "DXVK or Vulkan failures. Fix: install the Vulkan driver for your card, including 32-bit."),
                        ("Crash loop", "Wine crashing again and again. The launch is stopped before it exhausts memory."),
                        ("Game crash", "When Anomaly writes a crash log, COMMANDER offers Analyze crash, which makes a log dump and opens it in ASSISTANT."),
                    ]),
                    "Each of these also offers to create a bug report on the spot.",
                    ("live", [
                        ("launcher.log", "launcher_log"),
                    ]),
                    "launcher.log is emptied at the start of every launch, and the previous one is kept as launcher.log.1.",
                ],
            ),
            (
                "Playtime, Discord and shortcuts",
                [
                    ("table", [
                        ("Playtime", "Counted per profile, only while the game itself runs (not while only MO2 is open). Shown on the Dashboard; Reset under Settings > Maintenance > Playtime sets it back to zero."),
                        ("Discord Rich Presence", "Off by default. When on, shows 'Playing S.T.A.L.K.E.R. GAMMA' on your Discord profile, optionally with your mod count and playtime. It talks only to the Discord app on your own computer, through its local socket; any failure is ignored and never affects the game."),
                        ("Desktop shortcut", "Add shortcut to desktop writes a .desktop launcher for the current target and runner, so you can start the game without opening COMMANDER."),
                        ("Quit Game", "While the game runs, Launch Game becomes Quit Game, which closes the game and Mod Organizer."),
                    ]),
                ],
            ),
        ],
    },
    {
        "title": "Updates",
        "summary": "How COMMANDER checks for GAMMA updates without GitHub's rate limits, what an update changes, and how to undo it.",
        "page": "update",
        "sections": [
            (
                "How the check works",
                [
                    "The CLI's own update check uses GitHub's API, which allows only 60 requests an hour per address and often fails. COMMANDER gets the same answer without it:",
                    ("list", [
                        "Your installed version is read from gamma/version.txt. The latest is G.A.M.M.A_definition_version.txt from the Stalker_GAMMA repository, fetched from raw.githubusercontent.com, which is not rate-limited.",
                        "The list of changes comes from comparing GAMMA's official modpack list (from stalker-gamma.com) with the copy stored in your install: every addon that was added, removed, or whose archive changed.",
                        "Patch notes come from Patchnotes.md in the same repository.",
                    ]),
                    "The Updates page checks every time you open it, and the Dashboard shows the result too. A background check also runs at most once a day even if you never open the page, with a desktop notification once per new version.",
                ],
            ),
            (
                "Applying an update",
                [
                    "Apply updates runs stalker-gamma update apply, with --minimal, --preserve-user-settings and --preserve-mcm-settings depending on the boxes. It uses the same progress view and the same one-at-a-time lock as an install. Before it starts, COMMANDER:",
                    ("list", [
                        "warns if disk space is low;",
                        "asks you to switch back to singleplayer if co-op is on;",
                        "saves a snapshot of your modlist.txt for Undo last update;",
                        "backs up user.ltx and MCM settings;",
                        "captures your mod choices and MO2 settings, which are put back afterwards (see Installing Anomaly and GAMMA).",
                    ]),
                    ("warn", "Cancel on the Updates page stops the update straight away, without asking."),
                ],
            ),
            (
                "Undo last update",
                [
                    "Puts back the modlist.txt from just before the update. It does not put back mod files, so if an update itself broke something, run Verify Integrity on Install afterwards.",
                ],
            ),
        ],
    },
    {
        "title": "Mod Manager",
        "summary": "Editing the MO2 mod list safely: how changes are written, backups, conflicts, installing your own mods and FOMOD.",
        "page": "modmanager",
        "sections": [
            (
                "What it edits",
                [
                    "Mod Manager edits the active MO2 profile's modlist.txt, at gamma/profiles/<MO2 profile>/modlist.txt. It shows the list in MO2's order, grouped by GAMMA's categories (separators). Unknown lines and comments are kept exactly as they were.",
                    ("live", [
                        ("Active modlist.txt", "modlist"),
                    ]),
                ],
            ),
            (
                "How changes are kept safe",
                [
                    ("list", [
                        "Every change is saved straight away and written atomically: to a temporary file first, then swapped in. A crash or full disk cannot leave a half-written list.",
                        "Before COMMANDER's very first change, the untouched list is saved as modlist.txt.gammagui.bak. That copy is never overwritten.",
                        "Before most later changes a timestamped copy (modlist-YYYYmmdd-HHMMSS.bak) is saved beside it. The newest 20 are kept.",
                        "Editing is locked while Mod Organizer or the game is running, because MO2 rewrites modlist.txt when it closes and would silently undo your changes. The page unlocks by itself when they close.",
                    ]),
                    "The Backups menu has Create Backup, Restore Backup, and Restore Original Order, which puts GAMMA's own mods back in GAMMA's order while keeping your own mods.",
                ],
            ),
            (
                "Load order tools",
                [
                    ("table", [
                        ("Drag and drop", "Drag one or several mods to a new place, or drag a category to move it with all its mods. Custom Mods and G.A.M.M.A. End of List stay at the bottom."),
                        ("Move Up / Down / to Category", "In the right-click menu. You are warned the first time, because the wrong order can break the game."),
                        ("Reversed list detection", "A GAMMA list that has been flipped end to end crashes the game. COMMANDER detects it here and before launching, and offers a one-click fix with a backup first."),
                        ("New Category", "Adds your own separator. Only categories you created can be deleted; GAMMA's own ones cannot."),
                        ("Rename", "Gives a mod a nickname shown only in COMMANDER. modlist.txt and the mod's folder keep the real name, so MO2 and your other profiles are not affected. Reset Name brings the real one back."),
                    ]),
                ],
            ),
            (
                "Conflicts",
                [
                    "Check for File Conflicts scans every enabled mod's folder and lists files that more than one enabled mod contains, grouped by pairs of mods, with your own mods first. Show Conflicts on one mod lists what it overrides and what overrides it. The mod lower in the list wins.",
                ],
            ),
            (
                "Installing your own mods",
                [
                    "Install Mod accepts .zip, .7z, .rar and .fomod archives; you can also drag archives onto the page. Archives are unpacked with the 7-Zip (7zz) bundled with COMMANDER, or the system 7-Zip if needed, into a temporary folder first.",
                    ("list", [
                        "The mod name is cleaned to a safe single folder name, and nothing outside the new mod folder is ever written, even if the archive contains paths like ../.",
                        "A harmless wrapper folder at the top of the archive is unwrapped, so the mod's gamedata ends up where MO2 expects it.",
                        "A basic meta.ini is written so MO2 recognises the mod.",
                        "The mod is added turned off, in the Custom Mods category at the bottom. Tick it to turn it on.",
                        "If a mod of that name already exists, you are asked first; nothing is overwritten.",
                    ]),
                    "Update from Archive replaces one of your own mods with a newer archive, keeping its place and on/off state. Reinstall from Cache unpacks a GAMMA mod again from your download cache; it only works while the mod still matches GAMMA's official list.",
                ],
            ),
            (
                "FOMOD installers",
                [
                    "Many mods ship a FOMOD installer (fomod/ModuleConfig.xml) with choices. COMMANDER shows these as a step-by-step wizard, the same one in both interfaces. It supports install steps and option groups, options whose type depends on earlier choices (Required options are locked on, Recommended ones pre-ticked, Not usable ones disabled), steps that only appear for some choices, conditional files and file priorities. Option preview images are shown when the archive has them.",
                    "Archives that just bundle several mods side by side get a simple one-step chooser. FOMOD XML is parsed with entity expansion turned off, so a malicious installer cannot freeze COMMANDER.",
                ],
            ),
            (
                "MO2 profiles",
                [
                    "The menu next to the profile name creates, renames and deletes MO2 profiles. A new MO2 profile starts as a copy of an existing mod list. Use in MO2 makes MO2 open the profile shown (stalker-gamma mo2 config set selected-profile). Ctrl+F jumps to the search box from any page.",
                ],
            ),
        ],
    },
    {
        "title": "Verify Integrity and Repair",
        "summary": "The checks Verify Integrity runs, how the MD5 snapshot works, and how repair avoids losing anything.",
        "page": "install",
        "sections": [
            (
                "The checks",
                [
                    ("table", [
                        ("Anomaly files", "stalker-gamma anomaly check: every Anomaly file against Anomaly's official checksum list. Files GAMMA itself replaces are expected to differ and are not reported."),
                        ("GAMMA mods", "Every enabled mod in modlist.txt must have a non-empty folder in gamma/mods, and Mod Organizer's own files must be present."),
                        ("Mod file changes", "Every file under gamma/mods is hashed (MD5) and compared with a snapshot."),
                        ("Download cache", "Cached archives are compared against the MD5s in GAMMA's official list. Problems here only affect future installs."),
                    ]),
                    "Results are shown as plain-language rows, worst first, with an overall verdict: healthy, needs attention, or snapshot taken.",
                ],
            ),
            (
                "The MD5 snapshot",
                [
                    "The first run only saves a snapshot of every mod file's MD5 to gamma/gamma-md5.txt. Later runs list files that changed since. These are shown in amber rather than red, because a changed file is often your own edit, such as a tweaked config. The snapshot is replaced automatically after a GAMMA update or a repair, so legitimate changes are not reported forever.",
                    ("live", [
                        ("MD5 snapshot", "md5_baseline"),
                    ]),
                ],
            ),
            (
                "How repair works",
                [
                    ("steps", [
                        "Each problem is matched against GAMMA's official modpack list. Problems are split into repairable (an official GAMMA mod), not repairable (no known download source) and your own additions, which are never touched.",
                        "You are shown the full list of what will happen before anything starts.",
                        "Your modlist.txt and settings are backed up.",
                        "Each broken mod folder and its cached archive are moved aside into gamma/mods/.verify-quarantine. Nothing is deleted.",
                        "The repair install runs (full-install with --skip-extract-on-hash-match, --skip-github-downloads and both preserve flags). Only the set-aside mods are downloaded, MD5-checked and unpacked again.",
                        "Mods that were reinstalled successfully are kept; for anything that was not, the set-aside copy is moved back. Only then is the quarantine emptied. If you cancel, everything is moved back.",
                        "Your modlist.txt is put back if the installer changed it.",
                    ]),
                    "Verify also notices Anomaly files that GAMMA patches but which have gone back to the vanilla version (for example after a Steam or GOG file check) and can put GAMMA's copies back.",
                ],
            ),
        ],
    },
    {
        "title": "Profiles",
        "summary": "What a COMMANDER profile stores, how it is shared with the CLI, and what export and import include.",
        "page": "profiles",
        "sections": [
            (
                "Fields of a profile",
                [
                    "Profiles live in the CLI's settings.json. COMMANDER reads and writes the exact same format the CLI uses (PascalCase names) and keeps any field it does not know, so a newer CLI's settings survive.",
                    ("table", [
                        ("ProfileName", "The name shown everywhere."),
                        ("Anomaly / Gamma / Cache", "The three folders. Use absolute paths."),
                        ("Mo2Profile", "Which MO2 profile to use, normally G.A.M.M.A."),
                        ("DownloadThreads", "Parallel downloads, 6 by default."),
                        ("ModPackMakerUrl / ModListUrl", "Where GAMMA's mod list comes from (stalker-gamma.com and the Stalker_GAMMA repository)."),
                        ("Repository URLs and branches", "gamma_setup, Stalker_GAMMA, gamma_large_files_v2 and teivaz_anomaly_gunslinger. Change these only to use a fork or mirror. Profiles shows a notice when any differs from the official one, with a way to reset."),
                        ("Active", "Which profile is in use. Exactly one is active."),
                    ]),
                    ("live", [
                        ("Anomaly folder", "anomaly"),
                        ("GAMMA folder", "gamma"),
                        ("Cache folder", "cache"),
                        ("MO2 profile", "mo2_profile"),
                    ]),
                ],
            ),
            (
                "Create, activate, delete",
                [
                    "Creating, activating and deleting are done through the CLI (stalker-gamma config create / use / delete), so the CLI's own side effects, such as setting MO2's selected profile, happen exactly as the CLI intends. Folders are checked live while you type (Anomaly found, Will be created on install, and so on).",
                    "Deleting a profile only removes it from settings.json. The game folders stay on disk.",
                    "Each profile can have a colour tag and a note (right-click), and Compare profiles shows which mods differ between two profiles.",
                ],
            ),
            (
                "Export and import",
                [
                    "Export writes a .zip with a manifest.json of the profile's portable settings (MO2 profile name, download threads, repository URLs and branches) and its modlist.txt if GAMMA is installed. Folder paths are never included, because they only make sense on your machine. Importing asks you to pick your own folders, like creating any new profile. The imported mod list only means something once GAMMA is installed on the importing side too.",
                ],
            ),
        ],
    },
    {
        "title": "Multiplayer (xrRazom co-op)",
        "summary": "How co-op is installed, how switching between co-op and singleplayer works, and where its settings live.",
        "page": "multiplayer",
        "sections": [
            (
                "What gets installed",
                [
                    "xrRazom Co-op (Slim edition) is a separate mod by its own authors. You download its zip from moddb; COMMANDER finds the newest one in Downloads or on the Desktop. The zip has two halves, handled differently:",
                    ("table", [
                        ("Anomaly/ part", "Replacement engine exes, Steam networking DLLs and loose files that must sit in the Anomaly folder itself, which MO2 cannot switch off. COMMANDER keeps the co-op copies in <anomaly>/commander-coop/files and every original they replace in <anomaly>/commander-coop/backup, so singleplayer can always be restored."),
                        ("gamedata/ part", "An ordinary MO2 mod, installed as xrRazom Co-op at the highest priority and turned on and off together with the engine files."),
                    ]),
                    ("live", [
                        ("Co-op files and backups", "coop_dir"),
                    ]),
                ],
            ),
            (
                "Switching",
                [
                    "Switch to co-op backs up whatever is in the Anomaly folder now, then copies the co-op files in and enables the mod. Switch to Singleplayer puts the originals back, removes what co-op added and disables the mod. Your co-op saves are kept.",
                    "Launches are checked: with co-op on, only co-op can be launched; with it off, only singleplayer. Updates and repairs ask you to switch back to singleplayer first, because they rewrite the same engine files. If an update put the original engine back, Re-apply co-op fixes it.",
                ],
            ),
            (
                "Steam and LAN sessions",
                [
                    "Steam sessions use Steam invites and need the game to run as a Steam app: Call of Pripyat (app 41700) if you own it, otherwise Spacewar (app 480), Valve's free test app. COMMANDER writes a co-op shortcut into Steam with the right compatibility tool and artwork; on Linux Steam restarts once for this. LAN sessions use an IP address and port (27015 by default). The whole party must use the same type.",
                    "Co-op settings are the mod's xrr_* console variables in Anomaly's appdata/user.ltx. COMMANDER only changes those lines and keeps everything else. Only the host's rules (player slots, friendly fire, rescue, saving, teleport and fast travel) count.",
                    "On the invite tab your LAN address is blurred so it is safe to show on stream. Finding your public address asks api.ipify.org, and only when you ask for it.",
                    ("warn", "Close the game before changing any co-op setting. Skip mod check lets friends join with different mods, but often causes desyncs and crashes."),
                ],
            ),
        ],
    },
    {
        "title": "Backups",
        "summary": "Saves and settings backups, where they are stored, when they are taken automatically, and how restore works.",
        "page": "utilities",
        "sections": [
            (
                "What a backup contains",
                [
                    "Three things you cannot get back from a reinstall live inside the install folders, so a reset or uninstall would delete them:",
                    ("table", [
                        ("Saves", "<anomaly>/appdata/savedgames"),
                        ("Game options and keybinds", "<anomaly>/appdata/user.ltx"),
                        ("MCM settings", "axr_options.ltx, inside GAMMA's MCM values mod (or a renamed copy) and gamma/overwrite"),
                    ]),
                    "A backup is one .zip with a manifest.json plus saves/, user.ltx and mcm/. It is stored outside every install folder, so no reset can reach it.",
                    ("live", [
                        ("Backups folder", "backups"),
                    ]),
                ],
            ),
            (
                "Automatic backups",
                [
                    "COMMANDER makes a backup by itself before a Fresh Reset, GAMMA Reset, Full Uninstall, GAMMA update, Verify & Repair, GAMMA reinstall, and before every restore. The newest 5 of each kind are kept per profile, so frequent settings-only backups can never push out the one with your saves from before a reset. Backups you make yourself are never removed.",
                    "After a GAMMA Reset that kept your settings, they are put back automatically once the reinstall succeeds.",
                ],
            ),
            (
                "Restoring",
                [
                    "Restore lets you choose saves, settings or both. It never deletes anything: files are copied over the current ones, and every file that would be replaced is first packed into a 'Before restore' backup, so a restore can itself be undone. Renaming a profile keeps its backups with it.",
                ],
            ),
        ],
    },
    {
        "title": "Utilities",
        "summary": "Every maintenance tool, what it runs, and what it deletes or keeps.",
        "page": "utilities",
        "sections": [
            (
                "Cleanup",
                [
                    ("table", [
                        ("Preview cache cleanup", "stalker-gamma cache prune check: lists archives the current GAMMA no longer uses and how much space they take. Deletes nothing."),
                        ("Clean the download cache", "stalker-gamma cache prune apply: deletes those archives."),
                        ("Clear shader cache", "stalker-gamma anomaly purge-shader-cache. Can fix graphics glitches after driver or mod changes. The next start is slower."),
                        ("Remove ReShade", "stalker-gamma anomaly delete-reshade."),
                    ]),
                ],
            ),
            (
                "Fixes",
                [
                    ("table", [
                        ("Fix GOG installation", "stalker-gamma gog fix-install: corrects ModOrganizer.ini paths for an Anomaly copy from GOG."),
                        ("Repair Wine prefix", "Puts the runner's own system DLLs back over ones another Wine build wrote (see Runners and Wine prefixes). Run Install Dependencies afterwards."),
                        ("Use the default Wine prefix", "Stops using a custom prefix folder. The custom folder is not deleted."),
                    ]),
                ],
            ),
            (
                "Move Installation",
                [
                    "Moves Anomaly, GAMMA and the cache to another folder or drive. Files are copied and verified before the originals are deleted, and everything is undone if anything fails. The move's progress is saved in gui-settings.json, so if COMMANDER or the computer stops part way, the next start detects it and tells you what to do before anything is deleted.",
                ],
            ),
            (
                "Reset and uninstall",
                [
                    ("table", [
                        ("Fresh Reset", "Deletes Anomaly and GAMMA, then installs both again."),
                        ("GAMMA Reset", "Deletes and reinstalls GAMMA only. Anomaly is kept, and you can keep user.ltx and MCM settings."),
                        ("Full Uninstall", "Deletes Anomaly, GAMMA and the download cache."),
                    ]),
                    "Before any of them, COMMANDER shows exactly which folders will be deleted and what is kept, checks the cache to show what can be reused and what must be downloaded again, and makes an automatic backup of saves and settings. They only ever delete the active profile's own folders. Your Wine prefix is always kept.",
                    ("warn", "Resets and uninstall cannot be undone, apart from the backup of saves and settings."),
                ],
            ),
            (
                "Logs",
                [
                    "Create Log Dump and Open ASSISTANT are here too. See Logs and diagnostics.",
                ],
            ),
        ],
    },
    {
        "title": "Settings reference",
        "summary": "Every option in Settings, and every key COMMANDER stores in gui-settings.json.",
        "page": "settings",
        "sections": [
            (
                "Settings page",
                [
                    "Open Settings with the cog at the top right or Ctrl+,. Most changes apply straight away. Language, theme and font cannot change while an install runs or the game is open.",
                    ("table", [
                        ("Page on startup", "Which tab COMMANDER opens on."),
                        ("When COMMANDER starts", "Desktop interface, Steam Deck Mode, or ask (on a real Steam Deck)."),
                        ("Start COMMANDER when I log in", "Writes ~/.config/autostart/stalker-gamma-commander.desktop."),
                        ("Update notifications", "One desktop notification per new GAMMA or COMMANDER version."),
                        ("COMMANDER build", "Stable or unstable (see Installing COMMANDER)."),
                        ("Default runner", "The runner Play uses unless you pick another."),
                        ("Always use GameMode", "Wraps every launch in gamemoderun."),
                        ("Open Winecfg", "Wine's own settings window for your game prefix, run through your runner."),
                        ("MO2 Display Scale", "Sets the prefix's display DPI so Mod Organizer's text is readable: 100% to 200%, 125% recommended. Restart MO2 after applying."),
                        ("Language", "Ten languages: English, French, Spanish, German, Romanian, Polish, Russian, Ukrainian, Portuguese and Turkish."),
                        ("Theme", "Six themes: GAMMA (default), GAMMA Black, Dusk, Midnight, Terminal and Reactor."),
                        ("Font and size", "Six font families (Exo 2 by default) and 9 to 22 px."),
                        ("Use COMMANDER's own title bar", "Draws COMMANDER's own title bar; turn it off to use your desktop's."),
                        ("Discord Rich Presence", "See Playing and launching. A custom Discord Application ID can be set for your own app."),
                        ("Playtime", "Sets the active profile's playtime to zero."),
                        ("Add COMMANDER to Steam", "See Steam integration."),
                        ("Export diagnostics", "See Logs and diagnostics."),
                    ]),
                ],
            ),
            (
                "gui-settings.json",
                [
                    "COMMANDER's own preferences are kept apart from the CLI's settings.json, in gui-settings.json in the same folder. It is written atomically, and a last-known-good copy (gui-settings.json.last-good) is used if the main file is ever corrupt. Unknown or invalid values fall back to their defaults instead of stopping COMMANDER from starting.",
                    ("live", [
                        ("gui-settings.json", "gui_settings"),
                    ]),
                    ("table", [
                        ("runner, prefixes, wine_prefix", "Selected runner and the prefix chosen for each runner."),
                        ("target", "Last launch target."),
                        ("custom_launch_options, always_gamemoderun", "Play page launch options."),
                        ("theme, language, font_family, font_size", "Appearance."),
                        ("start_page, custom_title_bar, window_width, window_height, window_maximized", "Window and startup."),
                        ("autostart, update_notifications, update_channel", "Startup and updates."),
                        ("last_update_check_ts, notified_gamma_version, notified_commander_tag", "When the daily GAMMA check last ran, and which versions you were already told about."),
                        ("mo2_display_dpi", "MO2 Display Scale (120 means 125%)."),
                        ("tool_overrides", "Manual tool paths from System Check."),
                        ("playtime_seconds, last_played_ts", "Playtime per profile."),
                        ("profile_notes, profile_colors", "Profile notes and colour tags."),
                        ("user_created_categories, mod_nicknames", "Your own Mod Manager categories and mod nicknames."),
                        ("discord_rpc_enabled, discord_show_mods, discord_show_playtime, discord_client_id", "Discord Rich Presence."),
                        ("move_dest, move_expected, move_sources", "An unfinished Move Installation, cleared when it completes."),
                        ("pending_settings_restore", "Settings to put back after a GAMMA reinstall."),
                        ("welcome_hidden, welcome_seen_version", "The Welcome screen."),
                        ("deck_mode_preference, deck_font_scale, deck_start_screen, deck_runner_confirmed, deck_finish_sound, deck_finish_rumble", "Steam Deck Mode."),
                    ]),
                ],
            ),
        ],
    },
    {
        "title": "Files and folders",
        "summary": "Every file and folder COMMANDER reads or writes on this computer, with live paths.",
        "page": None,
        "sections": [
            (
                "COMMANDER and the CLI",
                [
                    "COMMANDER shares its settings folder with the CLI. It follows XDG_CONFIG_HOME, so it is ~/.config/stalker-gamma unless you changed that.",
                    ("live", [
                        ("Settings folder", "settings_dir"),
                        ("settings.json (profiles, owned by the CLI)", "settings_json"),
                        ("gui-settings.json (COMMANDER preferences)", "gui_settings"),
                        ("Logs", "logs"),
                        ("Log dumps", "dumps"),
                        ("Single-instance lock", "lock"),
                        ("Bundled CLI", "cli"),
                    ]),
                ],
            ),
            (
                "Your game install",
                [
                    ("live", [
                        ("Anomaly", "anomaly"),
                        ("GAMMA", "gamma"),
                        ("Download cache", "cache"),
                        ("modlist.txt", "modlist"),
                        ("MD5 snapshot", "md5_baseline"),
                        ("Repair quarantine", "quarantine"),
                        ("Co-op files", "coop_dir"),
                    ]),
                    "Inside the Anomaly folder, appdata holds user.ltx, saves (savedgames) and the engine's own logs and crash dumps. Inside the GAMMA folder are ModOrganizer.exe, ModOrganizer.ini, mods, profiles, overwrite and version.txt.",
                ],
            ),
            (
                "Elsewhere",
                [
                    ("live", [
                        ("Saves and settings backups", "backups"),
                        ("Wine prefix", "prefix"),
                        ("App menu entry", "menu_entry"),
                        ("Autostart entry (when enabled)", "autostart"),
                    ]),
                    ("table", [
                        ("GE-Proton builds", "~/.local/share/Steam/compatibilitytools.d, and the same folder in the other Steam installs COMMANDER finds (~/.steam/steam, Flatpak Steam)."),
                        ("Steam shortcuts", "<Steam>/userdata/<account>/config/shortcuts.vdf, only when you use Add to Steam or set up co-op."),
                        ("Steam icon", "~/.local/share/icons/stalker-gamma-commander.png"),
                        ("Desktop shortcuts", "Your desktop folder (from xdg-user-dir), only when you ask for one."),
                    ]),
                    "Every file COMMANDER writes is written atomically: to a temporary file beside it, then renamed into place.",
                ],
            ),
        ],
    },
    {
        "title": "Network and privacy",
        "summary": "Every server COMMANDER contacts, when, and why. There is no telemetry.",
        "page": None,
        "sections": [
            (
                "No telemetry",
                [
                    "COMMANDER does not collect usage data, crash reports or anything else about you, and has no account. Logs, log dumps and diagnostics stay on your computer until you choose to share them.",
                ],
            ),
            (
                "Servers COMMANDER itself contacts",
                [
                    ("table", [
                        ("raw.githubusercontent.com", "GAMMA's version file and patch notes (update checks), and the winetricks script during Install Dependencies."),
                        ("stalker-gamma.com", "GAMMA's official modpack list, for update checks, verify and repair."),
                        ("github.com", "COMMANDER's release feed (releases.atom) for its own update check and the Welcome screen's patch notes, downloading COMMANDER updates and GE-Proton builds, and umu-launcher during Install Dependencies."),
                        ("api.github.com", "Only the GE-Proton release list in Manage GE-Proton."),
                        ("api.ipify.org", "Only when you ask the Multiplayer page for your public address."),
                        ("Discord", "Nothing over the internet. Rich Presence only talks to the Discord app on your own computer."),
                    ]),
                    "Requests have time limits and size limits, only http and https addresses are allowed, and failures are shown, never crash the app.",
                ],
            ),
            (
                "Servers the CLI contacts",
                [
                    "During installs and updates the stalker-gamma CLI downloads Anomaly and GAMMA's mods from wherever GAMMA's list says they are hosted, mostly moddb and GitHub (the gamma_setup, Stalker_GAMMA, gamma_large_files_v2 and teivaz_anomaly_gunslinger repositories). These addresses come from your profile and can be changed to use a mirror.",
                ],
            ),
            (
                "What a diagnostics file contains",
                [
                    "Export diagnostics and log dumps include system information, the CLI version, settings.json, gui-settings.json, launcher.log and commander.log. Tokens, passwords, authorization headers and passwords inside URLs are masked before anything is written. Folder paths and your user name do appear, so look through a file before posting it publicly.",
                ],
            ),
        ],
    },
    {
        "title": "Safety design",
        "summary": "The rules COMMANDER follows so that a bug, crash or full disk does not cost you your install or your data.",
        "page": None,
        "sections": [
            (
                "Rules",
                [
                    ("table", [
                        ("One job at a time", "A single install lock covers install, update, repair, move and reset. Anything that would write to the install tree waits for it."),
                        ("One COMMANDER at a time", "A lock file (commander.lock) stops a second copy from starting; starting it again just tells you to use the open window."),
                        ("Atomic writes", "Settings, modlist.txt and other files are written to a temporary file and swapped in, so a crash cannot leave half a file."),
                        ("Nothing is deleted on a guess", "Repair sets broken mods aside, restore takes a backup first, a move verifies before deleting, and resets list exactly what goes."),
                        ("MO2 must be closed", "Edits to files MO2 owns are blocked while it runs."),
                        ("Wine prefix protection", "No Wine build is ever pointed at a prefix it did not create; damaged prefixes are refused before launch."),
                        ("Safe archives", "Mod archives cannot write outside their own folder; untrusted XML is parsed without entity expansion; downloads have size limits."),
                        ("Verified downloads", "GE-Proton builds are checked against SHA-512, COMMANDER updates against size, format and SHA-512."),
                        ("Crash records", "An unexpected error is written to commander.log and shown in a dialog instead of closing the app silently. A hard crash writes a stack trace to crash.log."),
                    ]),
                ],
            ),
        ],
    },
    {
        "title": "Steam integration",
        "summary": "Add to Steam, how the shortcuts are written, and running COMMANDER from Steam.",
        "page": "settings",
        "sections": [
            (
                "Add COMMANDER to Steam",
                [
                    "Writes two non-Steam game entries straight into your Steam library: STALKER COMMANDER and STALKER COMMANDER DECK (which opens in Deck Mode). If Steam is running, it is closed cleanly first, the shortcuts are written, and Steam is started again. If you have several Steam accounts, you are asked which one.",
                    "Steam's shortcuts.vdf is a binary file. COMMANDER reads and writes it with its own small parser that keeps every field it does not know byte for byte, so your other non-Steam games are never damaged. If the file cannot be read, the action stops before Steam is closed.",
                ],
            ),
            (
                "Adding it by hand",
                [
                    ("list", [
                        "AUR or from source: in Steam, Add a Non-Steam Game and pick STALKER COMMANDER (Deck Mode).",
                        "AppImage: add the .AppImage itself, then set its launch options to --deck. An AppImage can only publish one menu entry, so the Deck one cannot come with it.",
                    ]),
                ],
            ),
            (
                "Running from Steam",
                [
                    "When COMMANDER is started as a Steam shortcut it passes its Steam app ID on to the game, so in Game Mode the controller layout and window focus follow the game. Under gamescope, Deck Mode makes itself full screen.",
                ],
            ),
        ],
    },
    {
        "title": "Steam Deck Mode",
        "summary": "The controller interface: how to get in and out, what each screen does, and every control.",
        "page": "deck",
        "sections": [
            (
                "What it is",
                [
                    "A second interface built for the Deck's 1280x800 screen, its controls and its touchscreen. It is not a setting inside the desktop window: COMMANDER closes and starts again in Deck Mode (the same process, re-executed), and Exit Deck Mode brings you back the same way. Both use the same profiles, runner, prefix, launch target, theme, language and playtime, so switching never changes what you play with.",
                    "On a real Steam Deck, COMMANDER offers Deck Mode the first time it starts and remembers your answer (Settings > When COMMANDER starts). In Game Mode it opens Deck Mode without asking. On any other computer it opens as a normal 1280x800 window, so you can try it. On a bigger screen the whole interface scales up.",
                    ("table", [
                        ("Getting in", "The Deck Mode button on the Dashboard, the STALKER COMMANDER DECK Steam shortcut, ./run.sh --deck, or the --deck option on the AppImage."),
                        ("Getting out", "Exit Deck Mode in Deck Mode's Settings or the Menu button. Ctrl+Shift+D always works."),
                    ]),
                ],
            ),
            (
                "Screens",
                [
                    ("table", [
                        ("Dashboard", "One banner that answers 'can I play?' with the one button that moves things forward (Play, Install, Resume, Update), then Anomaly, GAMMA and dependency status, updates, mods, storage and your profiles (switch, create, edit name and folders with a built-in folder browser)."),
                        ("Play", "Launch target, runner, session timer and Force Stop Game for a game that hangs (the Deck has no Alt+F4)."),
                        ("Mods", "The mod list in MO2 order with priority numbers; fold categories, filter, search, enable or disable a whole category, reorder, install mods with FOMOD support."),
                        ("Update", "Every added, modified and removed mod, and scrollable patch notes."),
                        ("Install", "Anomaly, GAMMA, dependencies, GE-Proton, install options, and Resume after an interrupted install."),
                        ("Utilities", "Cache, shader cache, ReShade, GOG fix, prefix repair, move install, log dump, and the resets."),
                        ("System", "System Check, with Copy install command on each missing item."),
                        ("Settings", "Theme, language, text size (80 to 150%), start screen, sounds and rumble, Add to Steam, Exit Deck Mode."),
                    ]),
                    "With no profile yet, Deck Mode asks one question, internal storage or SD card, creates the profile and sends you to Install. While an install, update or move runs, the Deck is kept awake; you are warned before a long download on battery; and it chimes and vibrates when a long task finishes.",
                    ("note", "Deleting mods and some profile management are only in the desktop interface."),
                ],
            ),
            (
                "Controls",
                [
                    "COMMANDER reads the controller directly, so Steam's default layout is fine in Game Mode, and Desktop Mode's keyboard layout works too.",
                    ("table", [
                        ("D-pad / left stick", "Move (hold to repeat)"),
                        ("A", "Select or toggle. Hold A on a mod to pick it up, move it, press A to drop or B to cancel."),
                        ("B", "Back"),
                        ("X", "The screen's main action: Play, Install, Resume, Update, Check or Apply updates, Re-check"),
                        ("Y", "The screen's second action, or search. Opens the on-screen keyboard in text fields."),
                        ("L1 / R1", "Previous / next tab"),
                        ("L2 / R2", "Page up / down"),
                        ("Menu", "Quick menu: Play GAMMA, Exit Deck Mode or Quit"),
                        ("View", "Controls help"),
                    ]),
                    "On the on-screen keyboard, X is space and Y is backspace. Touch works everywhere: tap to select, drag to scroll.",
                ],
            ),
        ],
    },
    {
        "title": "Steam Machine",
        "summary": "Installing and playing GAMMA on a Valve Steam Machine, from the first download to Game Mode.",
        "page": None,
        "sections": [
            (
                "Before you start",
                [
                    ("table", [
                        ("Desktop Mode", "Do the setup there: Steam button > Power > Switch to Desktop. A keyboard and mouse help."),
                        ("Internal SSD", "Install GAMMA there. It is a huge number of small files and loads much more slowly from a microSD card."),
                        ("A few hours", "Mostly the GAMMA download. If it stops, Install offers to resume it next time."),
                    ]),
                ],
            ),
            (
                "Get COMMANDER running",
                [
                    ("steps", [
                        "Download the newest STALKER-GAMMA-COMMANDER AppImage from the Releases page on GitHub.",
                        "In Dolphin, right-click it, choose Properties > Permissions and tick Is executable. Then double-click it.",
                        "When COMMANDER asks, choose Deck Mode. It is the interface you use from the couch, and it keeps the machine awake during long downloads.",
                        "Deck Mode asks where to install. Choose Internal storage. It creates the folders and opens the Install tab.",
                    ]),
                ],
            ),
            (
                "Install GAMMA",
                [
                    ("steps", [
                        "Choose Install GE-Proton.",
                        "Choose Install Dependencies. The runtimes go into GE-Proton's Wine prefix, so do this after GE-Proton.",
                        "Press X to install GAMMA. It installs Anomaly first, then every GAMMA addon.",
                    ]),
                    ("note", "You don't need system Wine or protontricks. COMMANDER installs everything it needs."),
                ],
            ),
            (
                "Play from Game Mode",
                [
                    ("steps", [
                        "In Settings, choose Add COMMANDER to Steam. Steam restarts with two new shortcuts: STALKER COMMANDER and STALKER COMMANDER DECK.",
                        "On Play, press X to test the game once. The first launch builds shaders and downloads Steam's Linux runtime, so give it a few minutes.",
                        "Return to Gaming Mode. In your library, open Non-Steam and start STALKER COMMANDER DECK.",
                    ]),
                    ("warn", "Only use Add COMMANDER to Steam in Desktop Mode. It closes Steam, which would end a Game Mode session."),
                ],
            ),
            (
                "If something goes wrong",
                [
                    ("table", [
                        ("Mod Organizer closes straight away", "GE-Proton's VR support (wineopenxr) can crash when no VR software is installed, and takes Mod Organizer with it. GAMMA doesn't use VR, so turn it off: close the game and run this in Konsole."),
                    ]),
                    ("code", "WINEPREFIX=~/Games/umu/umu-default PROTONPATH=~/.local/share/Steam/compatibilitytools.d/GE-Proton11-9-x86_64 ~/.local/bin/umu-run reg add 'HKCU\\Software\\Wine\\DllOverrides' /v wineopenxr /d '' /f"),
                    ("note", "If your GE-Proton folder in ~/.local/share/Steam/compatibilitytools.d has another name, change GE-Proton11-9-x86_64 to match."),
                    ("table", [
                        ("A small window in Game Mode", "Start the STALKER COMMANDER DECK shortcut, not STALKER COMMANDER. If you added the AppImage by hand, set its launch options to --deck."),
                        ("Crashes after running Wine yourself", "Another Wine damaged the game's prefix. Use Utilities > Repair Wine prefix, then Install Dependencies again."),
                    ]),
                ],
            ),
            (
                "Removing everything",
                [
                    ("steps", [
                        "Choose Utilities > Full Uninstall. It deletes Anomaly, GAMMA and the cache, after backing up your saves, user.ltx and MCM settings to ~/.local/share/stalker-gamma-commander/backups.",
                        "Remove the two shortcuts from your Steam library and delete the AppImage.",
                        "Quit COMMANDER and run this in Konsole to remove the rest:",
                    ]),
                    ("code", "rm -rf ~/Games/umu/umu-default ~/.local/share/umu ~/.cache/winetricks ~/.config/stalker-gamma\nrm -f ~/.local/bin/umu-run ~/.local/bin/winetricks\nrm -f ~/.local/share/applications/stalker-gamma-commander.desktop ~/.local/share/icons/stalker-gamma-commander.png ~/.config/autostart/stalker-gamma-commander.desktop\nrm -rf ~/.local/share/stalker-gamma-commander"),
                    ("note", "The last line deletes your save backups, so skip it if you might come back. The Wine prefix is umu-run's default, so keep it if you run other games with umu-run."),
                ],
            ),
            (
                "Credits",
                [
                    "Based on the Steam Machine field guide by phiratio, the first tested GAMMA install on this hardware.",
                ],
            ),
        ],
    },
    {
        "title": "ASSISTANT",
        "summary": "The bundled log analyser: what it reads and how it explains problems.",
        "page": "utilities",
        "sections": [
            (
                "What it does",
                [
                    "ASSISTANT is a separate window bundled with COMMANDER (it runs as python -m assistant). It opens a COMMANDER log dump, reads every log inside it, and lists findings in plain words with the file, the line, and a suggested fix. It works entirely offline.",
                    ("table", [
                        ("X-Ray engine logs", "Anomaly's own logs and crash logs: the crash reason, the script or mod involved."),
                        ("Mod Organizer logs", "mo_interface.log and the USVFS logs."),
                        ("launcher.log", "Wine and MO2 launch output: prefix mismatches, missing runtimes, Vulkan problems."),
                        ("CLI logs", "The stalker-gamma CLI's daily logs: download and install failures."),
                        ("Wine prefix records", "Winetricks and Visual C++ install records."),
                        ("Every text file", "Python tracebacks and segfaults."),
                    ]),
                ],
            ),
            (
                "Using it",
                [
                    ("list", [
                        "After a crash, press Analyze crash when COMMANDER offers it; the dump opens in ASSISTANT by itself.",
                        "Any time: Utilities > Create Log Dump, then Open in ASSISTANT. Or open ASSISTANT and use Open ZIP, Ctrl+O, or drag a dump onto its window.",
                        "Findings are sorted by severity: Crash and Problem first, then Warning and Note; Normal means fine. Search filters by any word.",
                        "Save report (Ctrl+S) writes the findings to a file you can share.",
                    ]),
                    "ASSISTANT shows its findings in the language you picked in COMMANDER. Set COMMANDER_ASSISTANT to the path of another ASSISTANT build to use that one instead of the bundled copy.",
                ],
            ),
        ],
    },
    {
        "title": "Logs and diagnostics",
        "summary": "Every log COMMANDER writes, what a log dump collects, and what to attach to a bug report.",
        "page": "utilities",
        "sections": [
            (
                "COMMANDER's logs",
                [
                    ("table", [
                        ("commander.log", "Errors inside COMMANDER itself, with full Python tracebacks."),
                        ("crash.log", "A stack trace if COMMANDER crashes hard (for example inside Qt)."),
                        ("launcher.log", "Everything the runner, MO2 and the game print. Emptied at each launch; the previous one is kept as launcher.log.1."),
                    ]),
                    ("live", [
                        ("Logs folder", "logs"),
                    ]),
                ],
            ),
            (
                "Log dumps",
                [
                    "Create Log Dump collects COMMANDER's logs, Anomaly's and GAMMA's logs (MO2, USVFS, X-Ray, crash dumps) and the Wine prefix's logs into one zip named commander-log-dump-YYYYMMDD-HHMMSS.zip, with a diagnostics.txt added. Only log files and crash dumps are taken (.log, .dmp, .mdmp, .crash and files named like crash or backtrace); mods, textures, archives and other game data are skipped. Each file is capped at 64 MB and the whole dump at 256 MB.",
                    ("live", [
                        ("Log dumps folder", "dumps"),
                    ]),
                ],
            ),
            (
                "Export diagnostics",
                [
                    "Settings > Export diagnostics saves one text file: system information, the CLI version, settings.json, gui-settings.json, launcher.log and commander.log, with tokens and passwords masked. It is smaller than a log dump and good for questions about your setup rather than a crash.",
                ],
            ),
            (
                "Reporting a bug",
                [
                    ("steps", [
                        "Make a log dump right after the problem happens.",
                        "Open it in ASSISTANT and check whether the finding already explains it.",
                        "Ask on the COMMANDER Discord, or open an issue on GitHub, with the dump attached and what you did just before the problem. Include your COMMANDER version (bottom left of the window).",
                    ]),
                ],
            ),
        ],
    },
    {
        "title": "Command line and environment",
        "summary": "Start-up options and environment variables COMMANDER understands.",
        "page": None,
        "sections": [
            (
                "Options",
                [
                    ("table", [
                        ("--deck", "Start in Steam Deck Mode. Works with run.sh, the AUR wrapper and the AppImage."),
                        ("python -m commander_gui", "Start from a source checkout without run.sh (you provide PySide6)."),
                    ]),
                ],
            ),
            (
                "Environment variables",
                [
                    ("table", [
                        ("STALKER_GAMMA_CLI", "Path to a stalker-gamma binary to use instead of the bundled one."),
                        ("XDG_CONFIG_HOME", "Moves the settings folder (default ~/.config)."),
                        ("XDG_DATA_HOME", "Moves the backups folder and menu entry (default ~/.local/share)."),
                        ("COMMANDER_ALLOW_MULTIPLE", "Set by run.sh to allow several COMMANDERs at once. All instances share the same settings (last write wins) and nothing stops two of them installing into the same folders. COMMANDER_ALLOW_MULTIPLE=0 ./run.sh gives the normal one-window behaviour. The AppImage and AUR package always ignore it."),
                        ("COMMANDER_ASSISTANT", "Path to an ASSISTANT build to use instead of the bundled one."),
                        ("COMMANDER_DECK_WINDOWED=1", "Keep Deck Mode in a window even on a Deck or under gamescope."),
                        ("COMMANDER_DECK_NO_GAMEPAD=1", "Deck Mode leaves the controller alone, for controllers already mapped to keys."),
                        ("COMMANDER_SKIP_PREFIX_GUARD=1", "Skip the damaged-prefix check before launch. For debugging only."),
                        ("QT_QPA_PLATFORM", "Qt's platform plugin; offscreen is used by the test suite."),
                    ]),
                    ("warn", "COMMANDER_SKIP_PREFIX_GUARD=1 lets a launch into a damaged prefix happen, which can freeze the computer. Only use it if you know why."),
                ],
            ),
        ],
    },
    {
        "title": "Troubleshooting",
        "summary": "Common problems, why they happen and how to fix them.",
        "page": None,
        "sections": [
            (
                "Install stops with a Cloudflare or rate-limit error",
                [
                    "moddb or GitHub is refusing too many requests from your address. It is not a problem with your install. Wait a while (an hour is usually enough), optionally lower Download threads or use a VPN, then press Resume GAMMA Installation. Everything already downloaded is reused.",
                ],
            ),
            (
                "Mod Organizer closes straight away or mentions concrt140.dll",
                [
                    "The Visual C++ runtime is missing from the prefix, or the prefix belongs to a different runner. Run Install Dependencies on Install and use GE-Proton (Auto-detect) rather than Steam's Proton.",
                ],
            ),
            (
                "The game freezes the computer at start, or Wine keeps crashing",
                [
                    "A different Wine build has written into the prefix. COMMANDER normally refuses to launch into such a prefix and offers Repair now. If it happened anyway (for example a manual winetricks command), use Utilities > Repair Wine prefix, then Install Dependencies.",
                ],
            ),
            (
                "Vulkan errors or a black screen",
                [
                    "Install the Vulkan driver for your card, and also the 32-bit one; System Check shows the command. As a test, PROTON_USE_WINED3D=1 in launch options uses OpenGL instead of Vulkan (slow, but shows whether Vulkan is the problem).",
                ],
            ),
            (
                "The game crashes on start after editing mods",
                [
                    "Check the load order banner in Mod Manager: a reversed list crashes the game. Otherwise turn off the mod you changed last, or use Backups > Restore Backup. Analyze crash in ASSISTANT usually names the script or mod.",
                ],
            ),
            (
                "Mod Manager won't let me edit",
                [
                    "Mod Organizer or the game is open. Close it and the page unlocks by itself.",
                ],
            ),
            (
                "Play has nothing to launch",
                [
                    "GAMMA is not installed yet, or the profile points at the wrong GAMMA folder. Check the folders on Profiles; the folder checks there show what was found.",
                ],
            ),
            (
                "No runner found",
                [
                    "Install umu-run (System Check), then at least one GE-Proton from Play > Manage GE-Proton.",
                ],
            ),
            (
                "Co-op stopped working after an update",
                [
                    "The update put the original engine back. Press Re-apply co-op on Multiplayer, and check the xrRazom Discord for a version that matches the new GAMMA.",
                ],
            ),
            (
                "Every page says No active profile",
                [
                    "Create a profile, or set one as active, on Profiles.",
                ],
            ),
            (
                "Could not load the Qt platform plugin xcb",
                [
                    "Install libxcb-cursor0 (Debian, Ubuntu) or xcb-util-cursor (Fedora, Arch).",
                ],
            ),
            (
                "A move was interrupted",
                [
                    "Restart COMMANDER. It detects the unfinished move and explains what is where. Read that message before deleting anything by hand.",
                ],
            ),
            (
                "Autostart doesn't work",
                [
                    "Turn Start COMMANDER when I log in off and on again in Settings. If you moved the AppImage, this rewrites the entry with the new path.",
                ],
            ),
        ],
    },
    {
        "title": "For developers",
        "summary": "How the code is organised, running the tests, building the AppImage, and the rules for contributions.",
        "page": None,
        "sections": [
            (
                "Repository layout",
                [
                    ("table", [
                        ("commander_gui/", "The desktop application. Non-UI logic (launcher, modlist, integrity, repair, coop, updates, settings, fomod, steam_shortcuts and so on) sits at the top level, with no Qt where it can be avoided, so it is easy to test."),
                        ("commander_gui/ui/", "One module per page plus shared widgets (common.py), themed dialogs (dialogs.py) and the main window."),
                        ("commander_gui/locales/", "One TRANSLATIONS dictionary per language, plus template.py listing every string."),
                        ("Steamdeck/", "Steam Deck Mode: its own window, screens, gamepad input, on-screen keyboard and scaling. It reuses commander_gui's logic."),
                        ("assistant/", "ASSISTANT: analysers per log type and its own small UI."),
                        ("cli/", "The bundled stalker-gamma CLI binary and its libraries."),
                        ("packaging/", "The AUR PKGBUILD, desktop entries and the system wrapper script."),
                        ("tests/", "The test suite."),
                        ("build-appimage.sh", "Builds the AppImage."),
                    ]),
                ],
            ),
            (
                "How COMMANDER talks to the CLI",
                [
                    "cli_runner.py builds the command (the binary path from STALKER_GAMMA_CLI, the bundled copy, or PATH) and runs it. Long jobs run in a worker thread and stream each output line to the page; quick queries run synchronously with a timeout. parsers.py reads the CLI's Serilog-style progress lines with ANSI colours removed. settings.py mirrors the CLI's settings.json exactly. Cancel sends an interrupt to the whole process group and only kills it if it does not stop.",
                ],
            ),
            (
                "Running and testing",
                [
                    ("code", "./run.sh                      # run from source\n./run.sh --deck               # run Deck Mode\n.venv/bin/pip install -r requirements-dev.txt\nQT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ -q"),
                    "The tests build real widgets, so the Qt platform must be set to offscreen. They run in parallel through pytest-xdist by default; add -n0 for a single process. Lint with ruff.",
                ],
            ),
            (
                "Building the AppImage",
                [
                    ("code", "./build-appimage.sh [output-dir]"),
                    "The AppImage is built on a manylinux_2_28 Python, not the host's, so it runs on older distributions; the bundled Qt and CLI raise the actual floor to glibc 2.34. The appimagetool runtime and other downloads are pinned versions checked against known SHA256 hashes. Qt's xcb helper libraries are bundled because many distributions lack them. Builds are cached under .appimage-build/cache; set REBUILD_BASE=1 to rebuild the base. Each build writes a .sha512sum beside the AppImage.",
                ],
            ),
            (
                "Rules for contributions",
                [
                    ("list", [
                        "Wrap every visible string in tr() and add it to template.py and every locale file. tests/test_translations.py fails on missing, extra or stale keys and on broken {placeholders}.",
                        "Colours only through the theme tokens in themes.py; reuse existing widgets and object names (cards, section labels, primary/secondary buttons). Check new screens in several themes.",
                        "Pop-ups go through ui/dialogs.py MessageBox, never Qt's native box, with short sentence-case headings, plain wording and action labels on buttons.",
                        "Write files through atomic.py. Never run a Wine binary against a prefix it did not create. Never block the GUI thread on the network or the CLI.",
                        "Run the tests and ruff before opening a pull request.",
                    ]),
                ],
            ),
            (
                "Translations",
                [
                    "Adding a language needs no code: copy commander_gui/locales/template.py to <code>.py, fill in the values, keep brand names and {placeholders} as they are, and add the language to LANGUAGE_INFO in commander_gui/i18n.py. A missing translation falls back to English, so a partial one is safe to ship. Language changes apply instantly without a restart.",
                ],
            ),
        ],
    },
    {
        "title": "Credits and license",
        "summary": "Who made COMMANDER and the projects it is built on, and the terms it is shared under.",
        "page": None,
        "sections": [
            (
                "Credits",
                [
                    ("table", [
                        ("SSH-Kitty", "COMMANDER's graphical interface."),
                        ("Dnttnd", "Testing implementations, dev builds, bug reports, and helping polish the UI."),
                        ("FaithBeam", "stalker-gamma-cli, the installer COMMANDER bundles and drives. All installation, download and checksum logic is theirs."),
                        ("Grokitach and the GAMMA team", "The GAMMA modpack."),
                        ("GSC Game World and the Anomaly team", "The game."),
                        ("Other projects", "GE-Proton (GloriousEggroll), umu-launcher (Open Wine Components), Winetricks, Wine, Mod Organizer 2, xrRazom Co-op, 7-Zip, Qt and PySide6."),
                    ]),
                ],
            ),
            (
                "License",
                [
                    "COMMANDER is free software under the GNU General Public License version 3 (GPL-3.0). You may use, study, share and change it; if you share a changed version, it must be under the same license with its source code available. The bundled stalker-gamma-cli is GPL-3.0 as well.",
                    "COMMANDER is not affiliated with GSC Game World or the GAMMA team.",
                ],
            ),
        ],
    },
]
