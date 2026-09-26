import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from commander_gui import game_backup
from commander_gui.game_backup import (
    BackupError,
    backup_before_wipe,
    create_backup,
    delete_backup,
    list_backups,
    prune_automatic,
    restore_backup,
)

MCM_MOD = "G.A.M.M.A. MCM values - Rename to keep your personal changes"


class GameBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.data = root / "data"
        env = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.data)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.anomaly = root / "anomaly"
        self.gamma = root / "gamma"
        saves = self.anomaly / "appdata" / "savedgames"
        saves.mkdir(parents=True)
        for stem in ("one", "two"):
            (saves / f"{stem}.scop").write_bytes(b"save-" + stem.encode())
            (saves / f"{stem}.scoc").write_bytes(b"c")
            (saves / f"{stem}.dds").write_bytes(b"d")
        (self.anomaly / "appdata" / "user.ltx").write_text("bind jump kSPACE\n")
        self.mcm = self.gamma / "mods" / MCM_MOD / "gamedata" / "configs" / "axr_options.ltx"
        self.mcm.parent.mkdir(parents=True)
        self.mcm.write_text("[mcm]\nvalue = 1\n")

    def test_backup_holds_saves_user_ltx_and_mcm(self) -> None:
        info = create_backup("My Profile", self.anomaly, self.gamma)
        self.assertIsNotNone(info)
        self.assertEqual(info.saves, 2)
        self.assertTrue(info.user_ltx)
        self.assertEqual(info.mcm, [f"mods/{MCM_MOD}/gamedata/configs/axr_options.ltx"])
        self.assertIn(self.data, info.path.parents)
        with zipfile.ZipFile(info.path) as archive:
            names = set(archive.namelist())
        self.assertIn("saves/one.scop", names)
        self.assertIn("user.ltx", names)
        self.assertEqual([b.path for b in list_backups("My Profile")], [info.path])

    def test_restore_after_wipe_puts_everything_back(self) -> None:
        info = create_backup("p", self.anomaly, self.gamma)
        import shutil

        shutil.rmtree(self.anomaly / "appdata")
        shutil.rmtree(self.gamma / "mods")
        # The reinstall recreates the mod folder with default values.
        self.mcm.parent.mkdir(parents=True)
        self.mcm.write_text("[mcm]\nvalue = 0\n")
        (self.anomaly / "appdata").mkdir(parents=True)
        written = restore_backup(info, self.anomaly, self.gamma)
        self.assertEqual(written, 8)
        self.assertEqual((self.anomaly / "appdata/savedgames/one.scop").read_bytes(), b"save-one")
        self.assertIn("kSPACE", (self.anomaly / "appdata/user.ltx").read_text())
        self.assertIn("value = 1", self.mcm.read_text())
        # The default MCM file it replaced was kept in a "restore" backup.
        reasons = [b.reason for b in list_backups("p")]
        self.assertIn("restore", reasons)

    def test_mcm_goes_to_overwrite_when_its_mod_folder_is_gone(self) -> None:
        info = create_backup("p", self.anomaly, self.gamma, saves=False)
        import shutil

        shutil.rmtree(self.gamma / "mods" / MCM_MOD)
        restore_backup(info, self.anomaly, self.gamma, saves=False)
        target = self.gamma / "overwrite" / "gamedata" / "configs" / "axr_options.ltx"
        self.assertIn("value = 1", target.read_text())

    def test_saves_only_restore_leaves_settings_alone(self) -> None:
        info = create_backup("p", self.anomaly, self.gamma)
        (self.anomaly / "appdata" / "user.ltx").write_text("new\n")
        restore_backup(info, self.anomaly, self.gamma, saves=True, settings=False)
        self.assertEqual((self.anomaly / "appdata/user.ltx").read_text(), "new\n")

    def test_restore_never_writes_outside_the_install(self) -> None:
        bad = self.data / "stalker-gamma-commander" / "backups" / "p" / "bad.zip"
        bad.parent.mkdir(parents=True)
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("manifest.json", '{"profile": "p", "created": 1, "saves": 1}')
            archive.writestr("saves/../../../escape.scop", b"x")
            archive.writestr("saves/ok.scop", b"ok")
        info = game_backup.read_info(bad)
        restore_backup(info, self.anomaly, self.gamma)
        self.assertFalse((self.anomaly.parent / "escape.scop").exists())
        self.assertTrue((self.anomaly / "appdata/savedgames/ok.scop").exists())

    def test_nothing_to_back_up_returns_none(self) -> None:
        empty = Path(self._tmp.name) / "empty"
        self.assertIsNone(create_backup("p", empty, empty))

    def test_automatic_backups_are_pruned_manual_kept(self) -> None:
        manual = create_backup("p", self.anomaly, self.gamma, settings=False)
        for index in range(7):
            with patch("commander_gui.game_backup.time.time", return_value=2e9 + index):
                create_backup("p", self.anomaly, self.gamma, reason="gamma-reset", saves=False)
        prune_automatic("p")
        backups = list_backups("p")
        self.assertEqual(sum(b.reason == "gamma-reset" for b in backups), 5)
        self.assertIn(manual.path, [b.path for b in backups])

    def test_not_enough_space_raises_and_leaves_no_file(self) -> None:
        with patch(
            "commander_gui.game_backup.shutil.disk_usage",
            return_value=SimpleNamespace(free=10),
        ), self.assertRaises(BackupError):
            create_backup("p", self.anomaly, self.gamma)
        self.assertEqual(list_backups("p"), [])

    def test_backup_before_wipe_skips_saves_for_a_gamma_reset(self) -> None:
        profile = SimpleNamespace(profile_name="p", anomaly=str(self.anomaly), gamma=str(self.gamma))
        info = backup_before_wipe(profile, {"GAMMA"}, "gamma-reset", lambda _l: None)
        self.assertEqual(info.saves, 0)
        self.assertTrue(info.mcm)
        info = backup_before_wipe(profile, {"Anomaly", "GAMMA"}, "fresh-reset", lambda _l: None)
        self.assertEqual(info.saves, 2)
        self.assertIsNone(backup_before_wipe(profile, {"Cache"}, "uninstall", lambda _l: None))

    def test_delete_refuses_paths_outside_the_backup_folder(self) -> None:
        outside = Path(self._tmp.name) / "x.zip"
        outside.write_bytes(b"")
        info = game_backup.BackupInfo(path=outside, profile="p", created=0, reason="manual")
        with self.assertRaises(BackupError):
            delete_backup(info)
        self.assertTrue(outside.exists())


class WipeBackupTests(unittest.TestCase):
    def test_failed_backup_deletes_nothing(self) -> None:
        from commander_gui.ui import utilities_page

        with tempfile.TemporaryDirectory() as tmp:
            profile = SimpleNamespace(profile_name="p", anomaly=tmp, gamma=tmp)
            with (
                patch.object(utilities_page, "_validate_wipe_paths"),
                patch.object(utilities_page, "_resolved_wipe_target", return_value=Path(tmp)),
                patch.object(
                    utilities_page, "backup_before_wipe", side_effect=BackupError("disk full")
                ),
                patch.object(utilities_page, "_wipe_folders") as wipe,
                self.assertRaises(ValueError),
            ):
                utilities_page._backup_then_wipe(
                    profile, [("GAMMA", tmp)], "gamma-reset", lambda _l: None
                )
            wipe.assert_not_called()


class ProtonBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tools = Path(self._tmp.name) / "compatibilitytools.d"
        self.builds = {}
        for name in ("GE-Proton10-1", "GE-Proton11-5-x86_64"):
            build = self.tools / name
            build.mkdir(parents=True)
            (build / "proton").write_text("#!/bin/sh\n")
            (build / "proton").chmod(0o755)
            (build / "toolmanifest.vdf").write_text("")
            (build / "files.bin").write_bytes(b"x" * 1000)
            self.builds[name] = build
        found = [(n, str((b / "proton").resolve())) for n, b in self.builds.items()]
        for target in (
            patch("commander_gui.launcher.find_extra_protons", return_value=found),
            patch("commander_gui.proton_installer.steam_compat_tool_names", return_value=set()),
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_lists_newest_first_and_marks_the_auto_pick_in_use(self) -> None:
        from commander_gui.proton_installer import build_size, installed_builds

        builds = installed_builds("auto")
        self.assertEqual([b.name for b in builds], ["GE-Proton11-5-x86_64", "GE-Proton10-1"])
        self.assertTrue(builds[0].in_use and builds[0].newest)
        self.assertFalse(builds[1].in_use)
        self.assertGreaterEqual(build_size(builds[1].path), 1000)

    def test_removes_an_unused_build_but_never_the_one_in_use(self) -> None:
        from commander_gui.proton_installer import remove_build

        with self.assertRaises(ValueError):
            remove_build(self.builds["GE-Proton11-5-x86_64"], "auto")
        remove_build(self.builds["GE-Proton10-1"], "auto")
        self.assertFalse(self.builds["GE-Proton10-1"].exists())
        self.assertTrue(self.builds["GE-Proton11-5-x86_64"].exists())

    def test_explicit_runner_protects_its_own_build(self) -> None:
        from commander_gui.proton_installer import installed_builds, remove_build

        kind = "umup:" + str(self.builds["GE-Proton10-1"] / "proton")
        in_use = {b.name for b in installed_builds(kind) if b.in_use}
        self.assertEqual(in_use, {"GE-Proton10-1"})
        with self.assertRaises(ValueError):
            remove_build(self.builds["GE-Proton10-1"], kind)

    def test_refuses_folders_outside_compatibilitytools(self) -> None:
        from commander_gui.proton_installer import remove_build

        stray = Path(self._tmp.name) / "GE-Proton9-1"
        stray.mkdir()
        (stray / "proton").write_text("")
        with self.assertRaises(ValueError):
            remove_build(stray, "auto")
        self.assertTrue(stray.exists())



class SteamCompatMappingTests(unittest.TestCase):
    def test_reads_only_the_compat_tool_mapping_block(self) -> None:
        from commander_gui import proton_installer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "config.vdf").write_text(
                '"InstallConfigStore"\n{\n "Software"\n {\n  "CompatToolMapping"\n  {\n'
                '   "0"\n   {\n    "name"\t\t"GE-Proton10-1"\n   }\n  }\n  "Other"\n'
                '  {\n   "name"\t\t"NotATool"\n  }\n }\n}\n'
            )
            with patch("commander_gui.launcher.STEAM_ROOT_CANDIDATES", (root,)):
                names = proton_installer.steam_compat_tool_names()
        self.assertEqual(names, {"GE-Proton10-1"})


if __name__ == "__main__":
    unittest.main()


class BackupNameTests(unittest.TestCase):
    def test_named_backup_keeps_its_name_in_list_and_file_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "anomaly" / "appdata").mkdir(parents=True)
            (root / "anomaly" / "appdata" / "user.ltx").write_text("x")
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data")}):
                info = create_backup(
                    "p", root / "anomaly", root / "gamma", name="  Before\nZaton\x00 trip  "
                )
                self.assertEqual(info.name, "Before Zaton trip")
                self.assertEqual(info.label, "Before Zaton trip")
                self.assertIn("Before_Zaton_trip", info.path.name)
                self.assertEqual(list_backups("p")[0].name, "Before Zaton trip")
                unnamed = create_backup("p", root / "anomaly", root / "gamma")
                self.assertEqual(unnamed.label, "Manual backup")


class BackupReviewFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        env = patch.dict(
            os.environ,
            {"XDG_DATA_HOME": str(root / "data"), "XDG_CONFIG_HOME": str(root / "cfg")},
        )
        env.start()
        self.addCleanup(env.stop)
        self.anomaly = root / "anomaly"
        self.gamma = root / "gamma"
        (self.anomaly / "appdata" / "savedgames").mkdir(parents=True)
        (self.anomaly / "appdata" / "savedgames" / "a.scop").write_bytes(b"s")
        (self.anomaly / "appdata" / "user.ltx").write_text("keys")
        self.mcm = self.gamma / "mods" / MCM_MOD / "gamedata" / "configs" / "axr_options.ltx"
        self.mcm.parent.mkdir(parents=True)
        self.mcm.write_text("mine")

    def test_settings_backups_never_push_out_the_pre_reset_backup(self) -> None:
        reset = create_backup("p", self.anomaly, self.gamma, reason="fresh-reset")
        for index in range(8):
            with patch("commander_gui.game_backup.time.time", return_value=2e9 + index):
                create_backup("p", self.anomaly, self.gamma, reason="update", saves=False)
        paths = [b.path for b in list_backups("p")]
        self.assertIn(reset.path, paths)
        self.assertEqual(sum(b.reason == "update" for b in list_backups("p")), 5)

    def test_renaming_a_profile_keeps_its_backups(self) -> None:
        from commander_gui.game_backup import rename_profile_backups

        create_backup("Old Name", self.anomaly, self.gamma)
        rename_profile_backups("Old Name", "New Name")
        self.assertEqual(len(list_backups("New Name")), 1)
        self.assertEqual(list_backups("Old Name"), [])

    def test_gamma_reset_puts_mcm_back_after_the_reinstall(self) -> None:
        import shutil

        from commander_gui.game_backup import (
            apply_pending_settings_restore,
            mark_settings_restore,
        )

        profile = SimpleNamespace(
            profile_name="p", anomaly=str(self.anomaly), gamma=str(self.gamma)
        )
        info = backup_before_wipe(profile, {"GAMMA"}, "gamma-reset", lambda _l: None)
        mark_settings_restore("p", info, user_ltx=True, mcm=True)
        # The wipe and reinstall: the MCM values mod comes back with defaults.
        shutil.rmtree(self.gamma)
        self.mcm.parent.mkdir(parents=True)
        self.mcm.write_text("defaults")
        note = apply_pending_settings_restore(profile)
        self.assertIn("Restored", note)
        self.assertEqual(self.mcm.read_text(), "mine")
        # Consumed: a later install does not restore again.
        self.mcm.write_text("changed later")
        self.assertIsNone(apply_pending_settings_restore(profile))
        self.assertEqual(self.mcm.read_text(), "changed later")

    def test_mcm_only_restore_leaves_user_ltx(self) -> None:
        info = create_backup("p", self.anomaly, self.gamma, saves=False)
        (self.anomaly / "appdata" / "user.ltx").write_text("new keys")
        self.mcm.write_text("defaults")
        restore_backup(info, self.anomaly, self.gamma, saves=False, user_ltx=False)
        self.assertEqual((self.anomaly / "appdata" / "user.ltx").read_text(), "new keys")
        self.assertEqual(self.mcm.read_text(), "mine")
