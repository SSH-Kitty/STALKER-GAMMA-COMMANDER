"""Steam shortcuts.vdf read/write - the highest-risk part of "Add to Steam".

A parsing or serialization bug here can corrupt a user's entire non-Steam
games list, so round-trip fidelity on a realistic sample is tested first,
before any of the entry-mutation logic that builds on top of it.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from commander_gui.steam_shortcuts import (
    COOP_SHORTCUT_NAME,
    ShortcutsFileError,
    _shortcut_appid,
    add_commander_shortcuts,
    add_or_update_shortcut,
    add_to_steam,
    autologin_shortcuts_vdf,
    commander_launch_target,
    compat_tool_name,
    coop_launch_options,
    coop_launch_url,
    coop_shortcut_current,
    find_shortcuts_vdf,
    find_userdata_config_dirs,
    get_shortcuts_map,
    install_icon,
    list_steam_accounts,
    mapped_compat_tool,
    read_shortcuts,
    restart_steam,
    rungameid,
    set_compat_tool,
    set_shortcuts_map,
    setup_coop_shortcut,
    steam_launch_command,
    unsigned_appid,
    write_coop_shortcut,
    write_shortcuts,
)


#: A realistic two-entry shortcuts.vdf, hand-built to match the real binary
#: format (not a real user's file - no personal data belongs in this repo).
#: Mirrors an actual sample: root -> "shortcuts" map -> "0"/"1" entries, each
#: a flat map of string/int fields, with an empty "tags" map, and the extra
#: trailing 0x08 for the file's own unlabeled root.
def _sample_bytes() -> bytes:
    def entry(index: str, appname: str, exe: str) -> bytes:
        body = (
            b"\x02appid\x00" + (1).to_bytes(4, "little", signed=True)
            + b"\x01appname\x00" + appname.encode() + b"\x00"
            + b"\x01Exe\x00" + exe.encode() + b"\x00"
            + b"\x01StartDir\x00/home/user/\x00"
            + b"\x01LaunchOptions\x00\x00"
            + b"\x02IsHidden\x00" + (0).to_bytes(4, "little", signed=True)
            + b"\x00tags\x00\x08"
        )
        return b"\x00" + index.encode() + b"\x00" + body + b"\x08"

    shortcuts_body = entry("0", "Existing Game", '"/usr/bin/existing"') + entry(
        "1", "Another Game", '"/usr/bin/another"'
    )
    return b"\x00shortcuts\x00" + shortcuts_body + b"\x08" + b"\x08"


class SteamShortcutsRoundTripTests(unittest.TestCase):
    def test_round_trip_is_byte_identical(self):
        original = _sample_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shortcuts.vdf"
            path.write_bytes(original)
            entries = read_shortcuts(path)
            write_shortcuts(path, entries)
            self.assertEqual(path.read_bytes(), original)

    def test_missing_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does-not-exist.vdf"
            self.assertEqual(read_shortcuts(path), [])

    def test_get_shortcuts_map_reads_the_two_sample_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shortcuts.vdf"
            path.write_bytes(_sample_bytes())
            shortcuts = get_shortcuts_map(read_shortcuts(path))
            names = [{k: v for k, _t, v in fields}["appname"] for _k, _t, fields in shortcuts]
            self.assertEqual(names, ["Existing Game", "Another Game"])

    def test_set_shortcuts_map_on_an_empty_root_creates_it(self):
        root = set_shortcuts_map([], [("0", 0, [("appname", 1, "X")])])
        self.assertEqual(get_shortcuts_map(root)[0][0], "0")


class AddOrUpdateShortcutTests(unittest.TestCase):
    def test_adding_to_an_empty_list_starts_at_index_zero(self):
        shortcuts = add_or_update_shortcut([], "COMMANDER", "/opt/cmd", "/opt")
        self.assertEqual(shortcuts[0][0], "0")

    def test_a_second_distinct_entry_gets_the_next_index(self):
        shortcuts = add_or_update_shortcut([], "COMMANDER", "/opt/cmd", "/opt")
        shortcuts = add_or_update_shortcut(
            shortcuts, "COMMANDER (Deck Mode)", "/opt/cmd", "/opt", launch_options="--deck"
        )
        self.assertEqual([key for key, _t, _v in shortcuts], ["0", "1"])

    def test_rerunning_unchanged_updates_in_place_not_a_duplicate(self):
        shortcuts = add_or_update_shortcut([], "COMMANDER", "/opt/cmd", "/opt")
        shortcuts = add_or_update_shortcut(shortcuts, "COMMANDER", "/opt/cmd", "/opt")
        self.assertEqual(len(shortcuts), 1)

    def test_a_moved_install_adds_a_fresh_entry_rather_than_losing_the_old_one(self):
        shortcuts = add_or_update_shortcut([], "COMMANDER", "/opt/cmd", "/opt")
        shortcuts = add_or_update_shortcut([*shortcuts], "COMMANDER", "/new/cmd", "/new")
        self.assertEqual(len(shortcuts), 2)

    def test_unrelated_existing_entries_are_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shortcuts.vdf"
            path.write_bytes(_sample_bytes())
            shortcuts = get_shortcuts_map(read_shortcuts(path))
            before = shortcuts[0]
            shortcuts = add_or_update_shortcut(shortcuts, "COMMANDER", "/opt/cmd", "/opt")
            self.assertEqual(shortcuts[0], before)
            self.assertEqual(len(shortcuts), 3)


class AddCommanderShortcutsTests(unittest.TestCase):
    def test_writes_both_entries_and_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shortcuts.vdf"
            path.write_bytes(_sample_bytes())
            add_commander_shortcuts(path, exe="/opt/cmd", start_dir="/opt")

            backup = path.with_name(path.name + ".gammagui.bak")
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_bytes(), _sample_bytes())

            shortcuts = get_shortcuts_map(read_shortcuts(path))
            names = {{k: v for k, _t, v in f}["appname"] for _k, _t, f in shortcuts}
            self.assertEqual(
                names, {"Existing Game", "Another Game", "STALKER COMMANDER", "STALKER COMMANDER DECK"}
            )

    def test_is_idempotent_on_a_second_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shortcuts.vdf"
            add_commander_shortcuts(path, exe="/opt/cmd", start_dir="/opt")
            add_commander_shortcuts(path, exe="/opt/cmd", start_dir="/opt")
            shortcuts = get_shortcuts_map(read_shortcuts(path))
            self.assertEqual(len(shortcuts), 2)

    def test_creates_a_new_file_when_none_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config" / "shortcuts.vdf"
            add_commander_shortcuts(path, exe="/opt/cmd", start_dir="/opt")
            self.assertTrue(path.is_file())
            self.assertEqual(len(get_shortcuts_map(read_shortcuts(path))), 2)


class FindShortcutsVdfTests(unittest.TestCase):
    def test_a_single_userdata_dir_is_used_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Steam"
            config = root / "userdata" / "12345" / "config"
            config.mkdir(parents=True)
            with patch("commander_gui.launcher.STEAM_ROOT_CANDIDATES", (root,)):
                self.assertEqual(find_userdata_config_dirs(), [config])
                self.assertEqual(find_shortcuts_vdf(), config / "shortcuts.vdf")

    def test_no_userdata_anywhere_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Steam"
            root.mkdir()
            with patch("commander_gui.launcher.STEAM_ROOT_CANDIDATES", (root,)):
                self.assertIsNone(find_shortcuts_vdf())

    def test_symlinked_root_candidates_are_not_listed_twice(self):
        """Regression test: ~/.local/share/Steam and ~/.steam/steam are the

        same directory on most real installs (one is a symlink to the
        other) - both are in STEAM_ROOT_CANDIDATES, and scanning each
        without resolving them first duplicated every real account in
        the "Add to Steam" account picker.
        """
        with tempfile.TemporaryDirectory() as tmp:
            real_root = Path(tmp) / "Steam"
            (real_root / "userdata" / "12345" / "config").mkdir(parents=True)
            symlink_root = Path(tmp) / "steam-symlink"
            symlink_root.symlink_to(real_root)
            with patch(
                "commander_gui.launcher.STEAM_ROOT_CANDIDATES",
                (real_root, symlink_root),
            ):
                dirs = find_userdata_config_dirs()
        self.assertEqual(len(dirs), 1)

    def test_the_no_account_logged_in_placeholder_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Steam"
            (root / "userdata" / "0" / "config").mkdir(parents=True)
            (root / "userdata" / "12345" / "config").mkdir(parents=True)
            with patch("commander_gui.launcher.STEAM_ROOT_CANDIDATES", (root,)):
                dirs = find_userdata_config_dirs()
        self.assertEqual(len(dirs), 1)
        self.assertNotIn("0", [d.parent.name for d in dirs])

    def test_multiple_accounts_are_never_auto_picked(self):
        """Regression test: a real Steam install's loginusers.vdf fields

        (Timestamp, AutoLogin) were seen to belong to the wrong account -
        and to change underneath a running COMMANDER mid-session - so
        nothing here may guess between multiple candidates, ever. The
        caller must offer list_steam_accounts() for the user to pick from.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Steam"
            (root / "userdata" / "111" / "config").mkdir(parents=True)
            time.sleep(0.01)
            (root / "userdata" / "222" / "config").mkdir(parents=True)
            with patch("commander_gui.launcher.STEAM_ROOT_CANDIDATES", (root,)):
                self.assertIsNone(find_shortcuts_vdf())


class ListSteamAccountsTests(unittest.TestCase):
    def test_labels_fall_back_to_the_account_id_without_a_matching_loginusers_entry(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Steam"
            config = root / "userdata" / "111" / "config"
            config.mkdir(parents=True)
            with patch("commander_gui.launcher.STEAM_ROOT_CANDIDATES", (root,)):
                accounts = list_steam_accounts()
            self.assertEqual(len(accounts), 1)
            label, vdf_path = accounts[0]
            self.assertEqual(label, "Account 111")
            self.assertEqual(vdf_path, config / "shortcuts.vdf")

    def test_labels_use_the_persona_name_when_loginusers_has_a_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Steam"
            account_id = "111"
            config = root / "userdata" / account_id / "config"
            config.mkdir(parents=True)
            steamid64 = int(account_id) + 76561197960265728
            (root / "config").mkdir(parents=True)
            (root / "config" / "loginusers.vdf").write_text(
                '"users"\n{\n'
                f'\t"{steamid64}"\n\t{{\n\t\t"PersonaName"\t\t"Tester"\n\t}}\n'
                "}\n",
                encoding="utf-8",
            )
            with patch("commander_gui.launcher.STEAM_ROOT_CANDIDATES", (root,)):
                accounts = list_steam_accounts()
            self.assertEqual(len(accounts), 1)
            label, _vdf_path = accounts[0]
            self.assertEqual(label, "Tester")

    def test_the_auto_login_account_is_picked_among_several(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Steam"
            users = ""
            for account_id, login in (("111", "first"), ("222", "second")):
                (root / "userdata" / account_id / "config").mkdir(parents=True)
                steamid64 = int(account_id) + 76561197960265728
                users += f'\t"{steamid64}"\n\t{{\n\t\t"AccountName"\t\t"{login}"\n\t}}\n'
            (root / "config").mkdir()
            (root / "config" / "loginusers.vdf").write_text('"users"\n{\n' + users + "}\n")
            registry = Path(tmp) / "registry.vdf"
            registry.write_text('"Registry"\n{\n\t"AutoLoginUser"\t\t"second"\n}\n')
            with patch("commander_gui.launcher.STEAM_ROOT_CANDIDATES", (root,)):
                self.assertEqual(
                    autologin_shortcuts_vdf((registry,)),
                    (root / "userdata" / "222" / "config" / "shortcuts.vdf").resolve(),
                )
                self.assertIsNone(autologin_shortcuts_vdf((Path(tmp) / "missing.vdf",)))

    def test_two_accounts_get_two_distinct_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Steam"
            (root / "userdata" / "111" / "config").mkdir(parents=True)
            (root / "userdata" / "222" / "config").mkdir(parents=True)
            with patch("commander_gui.launcher.STEAM_ROOT_CANDIDATES", (root,)):
                accounts = list_steam_accounts()
            labels = [label for label, _path in accounts]
            self.assertEqual(len(labels), len(set(labels)))
            self.assertEqual(len(labels), 2)

    def test_a_genuine_name_collision_is_disambiguated_by_account_id(self):
        """Regression test: two real accounts can share a PersonaName (a

        family member's second account, a rename, etc.) - the account ID
        is only appended when that actually happens, keeping the common
        case a clean plain name.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Steam"
            (root / "userdata" / "111" / "config").mkdir(parents=True)
            (root / "userdata" / "222" / "config").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            entries = "\n".join(
                f'\t"{int(account_id) + 76561197960265728}"\n\t{{\n'
                f'\t\t"PersonaName"\t\t"Same Name"\n\t}}'
                for account_id in ("111", "222")
            )
            (root / "config" / "loginusers.vdf").write_text(
                f'"users"\n{{\n{entries}\n}}\n', encoding="utf-8"
            )
            with patch("commander_gui.launcher.STEAM_ROOT_CANDIDATES", (root,)):
                accounts = list_steam_accounts()
            labels = sorted(label for label, _path in accounts)
            self.assertEqual(labels, ["Same Name (111)", "Same Name (222)"])


class RestartSteamTests(unittest.TestCase):
    def test_native_launch_command(self):
        self.assertEqual(steam_launch_command(Path("/home/user/.steam/steam")), ["steam"])

    def test_flatpak_launch_command(self):
        flatpak_root = Path(
            "/home/user/.var/app/com.valvesoftware.Steam/.local/share/Steam"
        )
        self.assertEqual(
            steam_launch_command(flatpak_root),
            ["flatpak", "run", "com.valvesoftware.Steam"],
        )

    def test_shuts_down_waits_then_relaunches_in_order(self):
        order = []
        with (
            patch(
                "commander_gui.steam_shortcuts.subprocess.run",
                side_effect=lambda *a, **k: order.append(("run", a[0])),
            ),
            patch(
                "commander_gui.steam_shortcuts.subprocess.Popen",
                side_effect=lambda *a, **k: order.append(("popen", a[0])),
            ),
            patch(
                "commander_gui.steam_shortcuts._wait_for_steam_to_exit",
                side_effect=lambda *_a, **_k: order.append(("wait",)) or True,
            ),
        ):
            restart_steam(
                Path("/home/user/.steam/steam"),
                while_stopped=lambda: order.append(("write",)),
            )
        # The write happens while Steam is closed - a running client can
        # save its in-memory shortcut list back over a file written earlier.
        self.assertEqual(
            order,
            [
                ("run", ["steam", "-shutdown"]),
                ("wait",),
                ("write",),
                ("popen", ["steam"]),
            ],
        )

    def test_steam_comes_back_even_if_the_write_fails(self):
        """Steam was shut down for the write; a failing write must not leave
        the user with Steam closed."""

        def failing_write():
            raise OSError("disk full")

        with (
            patch("commander_gui.steam_shortcuts.subprocess.run"),
            patch("commander_gui.steam_shortcuts.subprocess.Popen") as popen,
            patch(
                "commander_gui.steam_shortcuts._wait_for_steam_to_exit",
                return_value=True,
            ),
            self.assertRaises(OSError),
        ):
            restart_steam(Path("/home/user/.steam/steam"), while_stopped=failing_write)
        popen.assert_called_once()

    def test_relaunch_args_only_follow_a_successful_write(self):
        """A co-op launch relaunches Steam with its steam:// link - one
        start, since a second racing ``steam`` leaves both stuck. A failed
        write must not launch the stale shortcut."""

        def failing_write():
            raise OSError("disk full")

        for write, expected in ((lambda: None, ["steam", "steam://x"]), (failing_write, ["steam"])):
            with (
                patch("commander_gui.steam_shortcuts.subprocess.run"),
                patch("commander_gui.steam_shortcuts.subprocess.Popen") as popen,
                patch("commander_gui.steam_shortcuts._wait_for_steam_to_exit", return_value=True),
            ):
                try:
                    restart_steam(
                        Path("/home/user/.steam/steam"),
                        while_stopped=write,
                        relaunch_args=("steam://x",),
                    )
                except OSError:
                    pass
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0], expected)

    def test_a_hung_steam_is_stopped_then_the_write_goes_ahead(self):
        """A Steam that ignores -shutdown (hung, often windowless) is
        terminated - it swallows every steam:// link otherwise."""
        written = []
        with (
            patch("commander_gui.steam_shortcuts.subprocess.run") as run,
            patch("commander_gui.steam_shortcuts.subprocess.Popen") as popen,
            patch("commander_gui.steam_shortcuts.shutil.which", return_value="/usr/bin/pkill"),
            patch(
                "commander_gui.steam_shortcuts._wait_for_steam_to_exit",
                side_effect=[False, True],
            ),
        ):
            restart_steam(
                Path("/home/user/.steam/steam"),
                while_stopped=lambda: written.append(True),
            )
        self.assertEqual(written, [True])
        self.assertIn(["/usr/bin/pkill", "-TERM", "-x", "steam"], [c.args[0] for c in run.call_args_list])
        popen.assert_called_once()

    def test_nothing_is_written_if_steam_will_not_close(self):
        written = []
        with (
            patch("commander_gui.steam_shortcuts.subprocess.run"),
            patch("commander_gui.steam_shortcuts.subprocess.Popen") as popen,
            patch(
                "commander_gui.steam_shortcuts._wait_for_steam_to_exit",
                return_value=False,
            ),
            self.assertRaises(OSError),
        ):
            restart_steam(
                Path("/home/user/.steam/steam"),
                while_stopped=lambda: written.append(True),
            )
        self.assertEqual(written, [])
        popen.assert_not_called()


class AddToSteamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_icon_is_copied_somewhere_permanent(self):
        source = self.tmp / "icon.png"
        source.write_bytes(b"png")
        dest = self.tmp / "icons" / "commander.png"
        self.assertEqual(install_icon(source, dest), str(dest))
        self.assertEqual(dest.read_bytes(), b"png")
        self.assertEqual(install_icon(self.tmp / "missing.png", dest), "")

    def test_launch_target_prefers_appimage_then_wrapper_then_run_sh(self):
        appimage = self.tmp / "COMMANDER.AppImage"
        self.assertEqual(
            commander_launch_target(appimage=appimage),
            (str(appimage), str(self.tmp), ""),
        )
        (self.tmp / "run.sh").write_text("#!/bin/sh\n")
        with patch("commander_gui.self_update.commander_appimage_path", return_value=None):
            exe, start, options = commander_launch_target(
                wrapper="", repo_root=self.tmp
            )
        self.assertEqual((exe, start, options), (str(self.tmp / "run.sh"), str(self.tmp), ""))

    def test_a_corrupt_file_is_refused_and_left_untouched(self):
        vdf = self.tmp / "shortcuts.vdf"
        broken = _sample_bytes()[:40]
        vdf.write_bytes(broken)
        with self.assertRaises(ShortcutsFileError):
            read_shortcuts(vdf)
        with self.assertRaises(ShortcutsFileError):
            add_commander_shortcuts(vdf, exe="/x", start_dir="/")
        self.assertEqual(vdf.read_bytes(), broken)
        self.assertFalse(vdf.with_name("shortcuts.vdf.gammagui.bak").exists())

    def test_add_to_steam_without_restart_writes_directly(self):
        vdf = self.tmp / "userdata" / "1" / "config" / "shortcuts.vdf"
        with (
            patch(
                "commander_gui.steam_shortcuts.commander_launch_target",
                return_value=("/opt/commander", "/opt", ""),
            ),
            patch("commander_gui.steam_shortcuts.install_icon", return_value=""),
            patch("commander_gui.steam_shortcuts.restart_steam") as restart,
        ):
            add_to_steam(vdf, restart=False)
        restart.assert_not_called()
        names = sorted(
            {k: v for k, _t, v in entry[2]}["appname"]
            for entry in get_shortcuts_map(read_shortcuts(vdf))
        )
        self.assertEqual(names, ["STALKER COMMANDER", "STALKER COMMANDER DECK"])
        # Atomic, lock-free write: nothing left behind in Steam's folder.
        self.assertEqual(sorted(p.name for p in vdf.parent.iterdir()), ["grid", "shortcuts.vdf"])
        # Each shortcut gets its own four pieces of library artwork.
        for name in names:
            appid = _shortcut_appid('"/opt/commander"', name) & 0xFFFFFFFF
            self.assertEqual(
                sorted(p.name for p in (vdf.parent / "grid").glob(f"{appid}*")),
                [f"{appid}.png", f"{appid}_hero.jpg", f"{appid}_logo.png", f"{appid}p.jpg"],
            )

    def test_add_to_steam_with_restart_writes_while_stopped(self):
        vdf = self.tmp / "userdata" / "1" / "config" / "shortcuts.vdf"
        seen = {}

        def fake_restart(root, *, while_stopped):
            seen["root"] = root
            self.assertFalse(vdf.exists())
            while_stopped()

        with (
            patch(
                "commander_gui.steam_shortcuts.commander_launch_target",
                return_value=("/opt/commander", "/opt", ""),
            ),
            patch("commander_gui.steam_shortcuts.install_icon", return_value=""),
            patch("commander_gui.steam_shortcuts.restart_steam", side_effect=fake_restart),
        ):
            add_to_steam(vdf, restart=True)
        self.assertEqual(seen["root"], self.tmp)
        self.assertTrue(vdf.is_file())


if __name__ == "__main__":
    unittest.main()


def _coop_entry_icon(vdf: Path) -> str:
    for _k, _t, value in get_shortcuts_map(read_shortcuts(vdf)):
        fields = {k: v for k, _t, v in value}
        if fields["appname"] == COOP_SHORTCUT_NAME:
            return fields["icon"]
    return ""


class CoopShortcutTests(unittest.TestCase):
    """The co-op shortcut Steam co-op sessions launch through."""

    CONFIG = (
        '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
        '\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n'
        '\t\t\t\t\t"0"\n\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t\t"proton_9"\n\t\t\t\t\t\t"config"\t\t""\n\t\t\t\t\t\t"priority"\t\t"75"\n\t\t\t\t\t}\n'
        '\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'
    )

    def setUp(self):
        # Keep the shortcut icon out of the real ~/.local/share/icons.
        self.icon = "/icons/commander.png"
        icon = patch("commander_gui.steam_shortcuts.install_icon", return_value=self.icon)
        icon.start()
        self.addCleanup(icon.stop)

    def _setup(self, tmp: str) -> Path:
        config_dir = Path(tmp) / "Steam" / "userdata" / "111" / "config"
        config_dir.mkdir(parents=True)
        vdf = config_dir / "shortcuts.vdf"
        vdf.write_bytes(_sample_bytes())
        (Path(tmp) / "Steam" / "config").mkdir()
        (Path(tmp) / "Steam" / "config" / "config.vdf").write_text(self.CONFIG)
        return vdf

    def _fields(self, profile="Default", appid=41700):
        return {
            "exe": "/games/GAMMA/ModOrganizer.exe",
            "start_dir": "/games/GAMMA",
            "launch_options": coop_launch_options("/pfx dir", profile, "Anomaly (DX11-AVX)", appid),
            "proton_name": "GE-Proton10-4",
        }

    def test_launch_options_point_proton_at_the_prefix_and_cop(self):
        self.assertEqual(
            coop_launch_options("/p", "Default", "Anomaly", 41700),
            "STEAM_COMPAT_DATA_PATH=/p SteamAppId=41700 %command% -p Default run -e Anomaly",
        )

    def test_launch_options_keep_shell_characters_literal(self):
        import shlex

        options = coop_launch_options('/my "pfx"', 'a"$(touch x)`id`', "Anomaly (DX11)", 41700)
        words = shlex.split(options.replace("%command%", "CMD"))
        self.assertEqual(words[0], 'STEAM_COMPAT_DATA_PATH=/my "pfx"')
        self.assertEqual(words[4], 'a"$(touch x)`id`')
        self.assertEqual(words[-1], "Anomaly (DX11)")
        self.assertIn("SteamAppId=480 ", coop_launch_options("/p", "Default", "Anomaly", 480))

    def test_rungameid_is_the_64_bit_shortcut_id(self):
        self.assertEqual(rungameid(0x80000001), str((0x80000001 << 32) | 0x02000000))

    def test_a_shortcut_without_an_icon_is_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            vdf = self._setup(tmp)
            fields = self._fields()
            with patch("commander_gui.steam_shortcuts.install_icon", return_value=""):
                write_coop_shortcut(vdf, **fields)
            self.assertFalse(coop_shortcut_current(vdf, **fields))
            write_coop_shortcut(vdf, **fields)
            self.assertTrue(coop_shortcut_current(vdf, **fields))

    def test_writes_shortcut_and_compat_tool_keeping_everything_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            vdf = self._setup(tmp)
            fields = self._fields()
            self.assertFalse(coop_shortcut_current(vdf, **fields))
            appid = write_coop_shortcut(vdf, **fields)
            self.assertTrue(coop_shortcut_current(vdf, **fields))
            names = [
                {k: v for k, _t, v in value}["appname"]
                for _k, _t, value in get_shortcuts_map(read_shortcuts(vdf))
            ]
            self.assertEqual(names, ["Existing Game", "Another Game", COOP_SHORTCUT_NAME])
            config = (vdf.parents[3] / "config" / "config.vdf").read_text()
            self.assertEqual(mapped_compat_tool(config, appid), "GE-Proton10-4")
            self.assertEqual(mapped_compat_tool(config, 0), "proton_9")
            self.assertEqual(appid, unsigned_appid(fields["exe"]))
            self.assertEqual(_coop_entry_icon(vdf), self.icon)
            # Nothing of ours left behind in Steam's folders but the backups.
            for folder in (vdf.parent, vdf.parents[3] / "config"):
                self.assertEqual(list(folder.glob(".*")), [], folder)

    def test_a_changed_profile_updates_the_one_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            vdf = self._setup(tmp)
            write_coop_shortcut(vdf, **self._fields("Default"))
            write_coop_shortcut(vdf, **self._fields("Other"))
            entries = get_shortcuts_map(read_shortcuts(vdf))
            coop = [
                {k: v for k, _t, v in value}
                for _k, _t, value in entries
                if {k: v for k, _t, v in value}["appname"] == COOP_SHORTCUT_NAME
            ]
            self.assertEqual(len(coop), 1)
            self.assertIn("-p Other ", coop[0]["LaunchOptions"])
            self.assertTrue(coop_shortcut_current(vdf, **self._fields("Other")))

    def test_switching_to_spacewar_restarts_steam_and_launches_it(self):
        """A CoP/Spacewar switch changes the launch options, so the next launch
        rewrites the shortcut with Steam closed, then starts Steam with the game."""
        with tempfile.TemporaryDirectory() as tmp:
            vdf = self._setup(tmp)
            cop = write_coop_shortcut(vdf, **self._fields())
            fields = self._fields(appid=480)
            self.assertFalse(coop_shortcut_current(vdf, **fields))
            with (
                patch("commander_gui.steam_shortcuts.subprocess.run") as run,
                patch("commander_gui.steam_shortcuts.subprocess.Popen") as popen,
                patch("commander_gui.steam_shortcuts._wait_for_steam_to_exit", return_value=True),
            ):
                setup_coop_shortcut(vdf, restart=True, **fields)
            self.assertIn("-shutdown", run.call_args.args[0])
            self.assertEqual(popen.call_args.args[0][-1], coop_launch_url(fields["exe"]))
            self.assertTrue(coop_shortcut_current(vdf, **fields))
            # Same shortcut id: the Proton mapping and launch link still apply.
            self.assertEqual(write_coop_shortcut(vdf, **fields), cop)
            self.assertTrue(coop_shortcut_current(vdf, **self._fields(appid=480)))
            self.assertFalse(coop_shortcut_current(vdf, **self._fields()))

    def test_writes_library_artwork_into_the_grid_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            vdf = self._setup(tmp)
            grid = vdf.parent / "grid"
            grid.mkdir()
            appid = unsigned_appid(self._fields()["exe"])
            (grid / f"{appid}p.png").write_bytes(b"old")  # would shadow ours
            write_coop_shortcut(vdf, **self._fields())
            names = sorted(p.name for p in grid.iterdir())
            self.assertEqual(
                names, sorted(f"{appid}{s}" for s in ("p.jpg", "_hero.jpg", "_logo.png", ".png"))
            )

    def test_an_old_gamma_coop_entry_is_renamed_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            vdf = self._setup(tmp)
            root = read_shortcuts(vdf)
            old = add_or_update_shortcut(
                get_shortcuts_map(root), "GAMMA Co-op", "/old/ModOrganizer.exe", "/old"
            )
            write_shortcuts(vdf, set_shortcuts_map(root, old))
            write_coop_shortcut(vdf, **self._fields())
            names = [
                {k: v for k, _t, v in value}["appname"]
                for _k, _t, value in get_shortcuts_map(read_shortcuts(vdf))
            ]
            self.assertEqual(names, ["Existing Game", "Another Game", COOP_SHORTCUT_NAME])

    def test_a_config_without_a_mapping_gets_one(self):
        text = '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"x"\t"1"\n\t\t\t}\n\t\t}\n\t}\n}\n'
        out = set_compat_tool(text, 5, "GE-Proton10-4")
        self.assertEqual(mapped_compat_tool(out, 5), "GE-Proton10-4")
        self.assertIn('"x"\t"1"', out)
        self.assertEqual(mapped_compat_tool(set_compat_tool(out, 5, "GE-Proton10-5"), 5), "GE-Proton10-5")

    def test_an_unrecognised_config_is_refused(self):
        with self.assertRaises(ValueError):
            set_compat_tool('"Other"\n{\n}\n', 5, "GE-Proton10-4")

    def test_compat_tool_name_reads_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp) / "GE-Proton10-4"
            build.mkdir()
            self.assertEqual(compat_tool_name(build), "GE-Proton10-4")
            (build / "compatibilitytool.vdf").write_text(
                '"compatibilitytools"\n{\n  "compat_tools"\n  {\n    "GE-Proton10-4-custom"\n    {\n    }\n  }\n}\n'
            )
            self.assertEqual(compat_tool_name(build), "GE-Proton10-4-custom")

    def test_valve_proton_builds_get_steams_own_names(self):
        for folder, name in (
            ("Proton 9.0", "proton_9"),
            ("Proton 10.0", "proton_10"),
            ("Proton 6.3", "proton_63"),
            ("Proton - Experimental", "proton_experimental"),
            ("Proton Hotfix", "proton_hotfix"),
        ):
            self.assertEqual(compat_tool_name(Path("/nowhere") / folder), name)


class SteamHardeningTests(unittest.TestCase):
    def test_compat_tool_name_with_quote_refused(self):
        from commander_gui import steam_shortcuts

        text = '"Valve"\n{\n\t"Steam"\n\t{\n\t}\n}\n'
        with self.assertRaises(ValueError):
            steam_shortcuts.set_compat_tool(text, 1, 'x"\n"evil" "1')

    def test_plain_anomaly_arguments_are_quoted(self):
        from commander_gui import steam_shortcuts

        out = steam_shortcuts.coop_launch_options("/p", "", "t", 1, "-a $(id) 'b c'", mo2=False)
        self.assertIn("'$(id)'", out)
        self.assertIn("'b c'", out)


class CoopShortcutRollbackTests(unittest.TestCase):
    def test_shortcuts_restored_when_config_write_fails(self):
        from commander_gui import steam_shortcuts

        with tempfile.TemporaryDirectory() as tmp:
            vdf = Path(tmp) / "userdata" / "1" / "config" / "shortcuts.vdf"
            vdf.parent.mkdir(parents=True)
            fields = dict(exe="/x/run", start_dir="/x", launch_options="a", proton_name=None)
            steam_shortcuts.write_coop_shortcut(vdf, **fields)
            before = vdf.read_bytes()
            fields.update(launch_options="b", proton_name="GE-Proton9-1")
            with patch.object(steam_shortcuts, "_write_compat_config", side_effect=OSError("full")):
                with self.assertRaises(OSError):
                    steam_shortcuts.write_coop_shortcut(vdf, **fields)
            self.assertEqual(vdf.read_bytes(), before)
