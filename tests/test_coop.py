import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from commander_gui import coop


def _make_zip(path: Path, exe: bytes = b"MZcoop") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xrRazom-release.txt", "﻿xrRazom 1.4 - Slim\n\nprotocol      116\n")
        archive.writestr("Anomaly/bin/AnomalyDX11.exe", exe)
        archive.writestr("Anomaly/bin/steam_appid.txt", "41700")
        archive.writestr("Anomaly/gamedata/scripts/x.script", "-- coop")
        archive.writestr("gamedata/scripts/xrr_core.script", "-- core")
        archive.writestr("fomod/info.xml", "<fomod/>")
    return path


class CoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.anomaly = root / "anomaly"
        self.gamma = root / "gamma"
        # Lower-case on disk: the zip's "AnomalyDX11.exe" must land on it.
        (self.anomaly / "bin").mkdir(parents=True)
        (self.anomaly / "bin" / "anomalydx11.exe").write_bytes(b"MZgamma")
        (self.anomaly / "appdata").mkdir()
        (self.anomaly / "appdata" / "user.ltx").write_text("rs_screenmode fullscreen\nxrr_player_name Old\n")
        profile = self.gamma / "profiles" / "G.A.M.M.A"
        profile.mkdir(parents=True)
        (profile / "modlist.txt").write_text("# generated\n+GAMMA Base\n-Other\n")
        self.modlist = profile / "modlist.txt"
        self.zip = _make_zip(root / "xrRazom-1.4-Slim.zip")

    def tearDown(self):
        self._tmp.cleanup()

    def _install(self):
        """Install, then switch to co-op as the player would."""
        info = coop.install(self.zip, self.anomaly, self.gamma, "G.A.M.M.A")
        coop.switch_to_coop(self.anomaly, self.gamma, "G.A.M.M.A")
        return info

    def test_a_corrupt_state_file_falls_back_to_defaults(self):
        state = coop._state_file(self.anomaly)
        state.parent.mkdir(parents=True, exist_ok=True)
        for text in ('["not", "a", "dict"]', '{"appid": "abc"}', '{"appid": null}'):
            state.write_text(text)
            self.assertEqual(coop.steam_appid(self.anomaly), coop.COP_APPID)

    def test_first_install_stays_singleplayer(self):
        coop.install(self.zip, self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertEqual(coop.status(self.anomaly), coop.SINGLEPLAYER)
        self.assertEqual((self.anomaly / "bin" / "anomalydx11.exe").read_bytes(), b"MZgamma")
        self.assertNotIn(f"+{coop.MOD_NAME}", self.modlist.read_text())

    def test_update_keeps_singleplayer(self):
        self._install()
        coop.switch_to_singleplayer(self.anomaly, self.gamma, "G.A.M.M.A")
        coop.install(self.zip, self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertEqual(coop.status(self.anomaly), coop.SINGLEPLAYER)

    def test_uninstall_without_modlist(self):
        self._install()
        self.modlist.unlink()
        coop.uninstall(self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertEqual((self.anomaly / "bin" / "anomalydx11.exe").read_bytes(), b"MZgamma")
        self.assertFalse(coop.state_dir(self.anomaly).exists())

    def test_install_switches_to_coop_and_round_trips(self):
        info = self._install()
        self.assertEqual(info["version"], "1.4")
        exe = self.anomaly / "bin" / "anomalydx11.exe"
        self.assertEqual(exe.read_bytes(), b"MZcoop")
        self.assertEqual([p.name for p in (self.anomaly / "bin").glob("*.exe")], ["anomalydx11.exe"])
        self.assertTrue((self.anomaly / "gamedata" / "scripts" / "x.script").is_file())
        self.assertTrue((self.gamma / "mods" / coop.MOD_NAME / "gamedata" / "scripts" / "xrr_core.script").is_file())
        lines = self.modlist.read_text().splitlines()
        self.assertEqual(lines[1], f"+{coop.MOD_NAME}")  # highest priority
        self.assertEqual(coop.status(self.anomaly), coop.COOP)

        coop.switch_to_singleplayer(self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertEqual(exe.read_bytes(), b"MZgamma")
        self.assertFalse((self.anomaly / "bin" / "steam_appid.txt").exists())
        self.assertFalse((self.anomaly / "gamedata" / "scripts" / "x.script").exists())
        self.assertIn(f"-{coop.MOD_NAME}", self.modlist.read_text())
        self.assertEqual(coop.status(self.anomaly), coop.SINGLEPLAYER)

        coop.switch_to_coop(self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertEqual(exe.read_bytes(), b"MZcoop")

    def test_steam_appid_also_lands_where_the_game_looks_for_it(self):
        """steam_api reads steam_appid.txt from the working directory - the
        Anomaly folder - not bin/; without it Steam init failed."""
        self._install()
        root_appid = self.anomaly / "steam_appid.txt"
        self.assertEqual(root_appid.read_text(), "41700")
        self.assertEqual(coop.status(self.anomaly), coop.COOP)
        coop.switch_to_singleplayer(self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertFalse(root_appid.exists())

    def test_switching_between_spacewar_and_cop(self):
        self._install()
        bin_appid = self.anomaly / "bin" / "steam_appid.txt"
        root_appid = self.anomaly / "steam_appid.txt"
        coop.set_steam_appid(self.anomaly, coop.SPACEWAR_APPID)
        self.assertEqual((bin_appid.read_text(), root_appid.read_text()), ("480", "480"))
        self.assertEqual(coop.steam_appid(self.anomaly), 480)
        self.assertEqual(coop.status(self.anomaly), coop.COOP)

        # Off and on again: the original files come back, Spacewar stays.
        coop.switch_to_singleplayer(self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertFalse(bin_appid.exists())
        self.assertEqual((self.anomaly / "bin" / "anomalydx11.exe").read_bytes(), b"MZgamma")
        coop.switch_to_coop(self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertEqual(root_appid.read_text(), "480")

        # A switch while in singleplayer only takes effect on the next co-op.
        coop.switch_to_singleplayer(self.anomaly, self.gamma, "G.A.M.M.A")
        coop.set_steam_appid(self.anomaly, coop.COP_APPID)
        self.assertFalse(root_appid.exists())
        coop.switch_to_coop(self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertEqual(root_appid.read_text(), "41700")
        self.assertEqual(coop.status(self.anomaly), coop.COOP)

    def test_update_keeps_spacewar(self):
        self._install()
        coop.set_steam_appid(self.anomaly, coop.SPACEWAR_APPID)
        self._install()
        self.assertEqual((self.anomaly / "steam_appid.txt").read_text(), "480")
        self.assertEqual(coop.status(self.anomaly), coop.COOP)

    def test_an_older_install_gets_the_root_appid_before_launch(self):
        self._install()
        root_appid = self.anomaly / "steam_appid.txt"
        state = coop.load_state(self.anomaly)
        del state["files"]["steam_appid.txt"]
        coop._save_state(self.anomaly, state)
        root_appid.unlink()

        coop.ensure_root_appid(self.anomaly, self.gamma, "G.A.M.M.A")

        self.assertEqual(root_appid.read_text(), "41700")
        self.assertEqual(coop.status(self.anomaly), coop.COOP)

    def test_reverted_detected_and_newer_original_kept(self):
        self._install()
        exe = self.anomaly / "bin" / "anomalydx11.exe"
        exe.write_bytes(b"MZgamma-updated")  # a GAMMA update put its exe back
        self.assertEqual(coop.status(self.anomaly), coop.REVERTED)
        # Single-player must not roll the update back to the old backup.
        coop.switch_to_singleplayer(self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertEqual(exe.read_bytes(), b"MZgamma-updated")
        coop.switch_to_coop(self.anomaly, self.gamma, "G.A.M.M.A")
        coop.switch_to_singleplayer(self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertEqual(exe.read_bytes(), b"MZgamma-updated")

    def test_engine_changed_tells_a_repair_from_a_new_engine(self):
        self._install()
        exe = self.anomaly / "bin" / "anomalydx11.exe"
        exe.write_bytes(b"MZgamma")  # a repair put the same original back
        self.assertFalse(coop.engine_changed(self.anomaly))
        exe.write_bytes(b"MZgamma-new-engine")
        self.assertTrue(coop.engine_changed(self.anomaly))

    def test_launch_mode_lock(self):
        block = coop.launch_block_reason
        # Not installed / single-player: solo only.
        self.assertIsNone(block(self.anomaly, coop_launch=False))
        self.assertIsNone(block(self.anomaly, coop_launch=False, direct=True))
        self.assertIsNotNone(block(self.anomaly, coop_launch=True))
        # Co-op: co-op through MO2 only.
        self._install()
        self.assertIsNone(block(self.anomaly, coop_launch=True))
        self.assertIsNotNone(block(self.anomaly, coop_launch=False))
        self.assertIsNotNone(block(self.anomaly, coop_launch=True, direct=True))
        coop.switch_to_singleplayer(self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertIsNone(block(self.anomaly, coop_launch=False))
        self.assertIsNotNone(block(self.anomaly, coop_launch=True))
        # Reverted: nothing launches.
        coop.switch_to_coop(self.anomaly, self.gamma, "G.A.M.M.A")
        (self.anomaly / "bin" / "anomalydx11.exe").write_bytes(b"MZgamma-updated")
        self.assertIsNotNone(block(self.anomaly, coop_launch=True))
        self.assertIsNotNone(block(self.anomaly, coop_launch=False))

    def test_uninstall_restores_tree(self):
        self._install()
        coop.uninstall(self.anomaly, self.gamma, "G.A.M.M.A")
        self.assertEqual((self.anomaly / "bin" / "anomalydx11.exe").read_bytes(), b"MZgamma")
        self.assertNotIn(coop.MOD_NAME, self.modlist.read_text())
        self.assertFalse((self.gamma / "mods" / coop.MOD_NAME).exists())
        self.assertEqual(coop.status(self.anomaly), coop.NOT_INSTALLED)

    def test_rejects_unsafe_and_foreign_zips(self):
        bad = Path(self._tmp.name) / "bad.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("../evil.txt", "x")
        with self.assertRaises(coop.CoopError):
            coop.inspect_zip(bad)
        other = Path(self._tmp.name) / "other.zip"
        with zipfile.ZipFile(other, "w") as archive:
            archive.writestr("readme.txt", "x")
        with self.assertRaises(coop.CoopError):
            coop.inspect_zip(other)

    def test_settings_keep_unrelated_lines(self):
        values, present = coop.read_settings(self.anomaly)
        self.assertEqual(values["xrr_player_name"], "Old")
        self.assertEqual(present, {"xrr_player_name"})
        coop.write_settings(self.anomaly, {"xrr_player_name": "New\nName", "xrr_friendly_fire": True})
        text = (self.anomaly / "appdata" / "user.ltx").read_text().splitlines()
        self.assertEqual(text, ["rs_screenmode fullscreen", "xrr_player_name New Name", "xrr_friendly_fire on"])

    def test_settings_survive_a_solo_session(self):
        # The single-player engine rewrites user.ltx without the xrr_* lines.
        self._install()
        coop.write_settings(self.anomaly, {"xrr_friendly_fire": True})
        coop.switch_to_singleplayer(self.anomaly, self.gamma, "G.A.M.M.A")
        user_ltx = self.anomaly / "appdata" / "user.ltx"
        user_ltx.write_text("rs_screenmode windowed\n")
        values, present = coop.read_settings(self.anomaly)
        self.assertEqual((values["xrr_player_name"], values["xrr_friendly_fire"], present), ("Old", True, set()))
        coop.switch_to_coop(self.anomaly, self.gamma, "G.A.M.M.A")
        text = user_ltx.read_text().splitlines()
        self.assertEqual(text[0], "rs_screenmode windowed")
        self.assertEqual(sorted(text[1:]), ["xrr_friendly_fire on", "xrr_host_session on", "xrr_max_players 4", "xrr_player_name Old"])

    def test_slot_default_lifted_once(self):
        # The engine writes 3 friends; xrRazom takes 4. Lift it once, then
        # leave whatever the player picks alone.
        user_ltx = self.anomaly / "appdata" / "user.ltx"
        user_ltx.write_text("xrr_max_players 3\n")
        self._install()
        self.assertEqual(coop.read_settings(self.anomaly)[0]["xrr_max_players"], 4)
        self.assertEqual(user_ltx.read_text(), "xrr_max_players 4\nxrr_host_session on\n")
        coop.write_settings(self.anomaly, {"xrr_max_players": 3})
        self.assertEqual(coop.read_settings(self.anomaly)[0]["xrr_max_players"], 3)

    def test_role_starts_as_host_once(self):
        self._install()
        self.assertTrue(coop.read_settings(self.anomaly)[0]["xrr_host_session"])
        coop.write_settings(self.anomaly, {"xrr_host_session": False})  # the player picks Joining
        self.assertFalse(coop.read_settings(self.anomaly)[0]["xrr_host_session"])
        self._install()  # an update keeps the pick
        self.assertFalse(coop.read_settings(self.anomaly)[0]["xrr_host_session"])

    def test_last_ip_round_trip(self):
        coop.write_last_ip(self.anomaly, self.gamma, "G.A.M.M.A", "1.2.3.4:5445")
        self.assertEqual(coop.read_last_ip(self.anomaly, self.gamma, "G.A.M.M.A"), "1.2.3.4:5445")
        coop.write_last_ip(self.anomaly, self.gamma, "G.A.M.M.A", "5.6.7.8:1")
        self.assertEqual(coop.read_last_ip(self.anomaly, self.gamma, "G.A.M.M.A"), "5.6.7.8:1")


class PlainAnomalyCoopTests(unittest.TestCase):
    """Co-op on Anomaly alone: no MO2, so the gamedata half goes loose."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.anomaly = root / "anomaly"
        (self.anomaly / "bin").mkdir(parents=True)
        (self.anomaly / "bin" / "anomalydx11.exe").write_bytes(b"MZvanilla")
        self.zip = _make_zip(root / "xrRazom-1.4-Slim.zip")
        self.core = self.anomaly / "gamedata" / "scripts" / "xrr_core.script"
        # A plain Anomaly profile may leave the GAMMA folder empty, and
        # Path("") is the cwd: nothing may land there.
        self._cwd = os.getcwd()
        self.cwd = root / "cwd"
        self.cwd.mkdir()
        os.chdir(self.cwd)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _install(self):
        coop.install(self.zip, self.anomaly, "", "", kind=coop.KIND_ANOMALY)
        coop.switch_to_coop(self.anomaly, "", "")

    def test_round_trip_leaves_nothing_behind(self):
        self._install()
        self.assertEqual(coop.status(self.anomaly), coop.COOP)
        self.assertEqual(coop.install_kind(self.anomaly), coop.KIND_ANOMALY)
        self.assertEqual(self.core.read_text(), "-- core")
        self.assertFalse((self.anomaly / "fomod").exists())
        coop.switch_to_singleplayer(self.anomaly, "", "")
        self.assertFalse(self.core.exists())
        self.assertEqual((self.anomaly / "bin" / "anomalydx11.exe").read_bytes(), b"MZvanilla")
        self._install()
        coop.uninstall(self.anomaly, "", "")
        self.assertFalse(self.core.exists())
        self.assertFalse(coop.state_dir(self.anomaly).exists())
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_an_original_identical_to_the_coop_copy_survives_a_round_trip(self):
        # GAMMA's Anomaly folder already holds some of the Modded Exes files
        # xrRazom ships, byte for byte.
        same = self.anomaly / "gamedata" / "scripts" / "x.script"
        same.parent.mkdir(parents=True)
        same.write_text("-- coop")
        self._install()
        coop.switch_to_singleplayer(self.anomaly, "", "")
        self.assertEqual(same.read_text(), "-- coop")
        self._install()
        coop.uninstall(self.anomaly, "", "")
        self.assertEqual(same.read_text(), "-- coop")

    def test_launch_lock_allows_only_launches_without_mo2(self):
        block = coop.launch_block_reason
        self._install()
        self.assertIsNone(block(self.anomaly, coop_launch=True, direct=True))
        self.assertIn("plain Anomaly", block(self.anomaly, coop_launch=True))
        self.assertIsNotNone(block(self.anomaly, coop_launch=False, direct=True))
        coop.switch_to_singleplayer(self.anomaly, "", "")
        self.assertIsNone(block(self.anomaly, coop_launch=False))

    def test_last_ip_goes_where_plain_anomaly_reads_it(self):
        self._install()
        coop.write_last_ip(self.anomaly, "", "", "1.2.3.4:5445")
        options = self.anomaly / "gamedata" / "configs" / "axr_options.ltx"
        self.assertIn("last_ip = 1.2.3.4:5445", options.read_text())
        self.assertEqual(coop.read_last_ip(self.anomaly, "", ""), "1.2.3.4:5445")

    def test_reinstall_for_plain_anomaly_drops_the_mo2_mod(self):
        gamma = Path(self._tmp.name) / "gamma"
        modlist = gamma / "profiles" / "G.A.M.M.A" / "modlist.txt"
        modlist.parent.mkdir(parents=True)
        modlist.write_text("+GAMMA Base\n")
        coop.install(self.zip, self.anomaly, gamma, "G.A.M.M.A")
        coop.switch_to_coop(self.anomaly, gamma, "G.A.M.M.A")
        coop.install(self.zip, self.anomaly, gamma, "G.A.M.M.A", kind=coop.KIND_ANOMALY)
        self.assertEqual(coop.status(self.anomaly), coop.COOP)
        self.assertTrue(self.core.is_file())
        self.assertFalse((gamma / "mods" / coop.MOD_NAME).exists())
        self.assertNotIn(coop.MOD_NAME, modlist.read_text())

    def test_switching_kind_needs_no_zip_and_keeps_coop_on(self):
        gamma = Path(self._tmp.name) / "gamma"
        modlist = gamma / "profiles" / "G.A.M.M.A" / "modlist.txt"
        modlist.parent.mkdir(parents=True)
        modlist.write_text("+GAMMA Base\n")
        mod = gamma / "mods" / coop.MOD_NAME / "gamedata" / "scripts" / "xrr_core.script"
        coop.install(self.zip, self.anomaly, gamma, "G.A.M.M.A")
        coop.switch_to_coop(self.anomaly, gamma, "G.A.M.M.A")
        self.zip.unlink()  # nothing below may need it

        coop.set_install_kind(self.anomaly, gamma, "G.A.M.M.A", coop.KIND_ANOMALY)
        self.assertEqual(coop.install_kind(self.anomaly), coop.KIND_ANOMALY)
        self.assertEqual(coop.status(self.anomaly), coop.COOP)
        self.assertEqual(self.core.read_text(), "-- core")
        self.assertFalse(mod.exists())
        self.assertNotIn(coop.MOD_NAME, modlist.read_text())

        coop.set_install_kind(self.anomaly, gamma, "G.A.M.M.A", coop.KIND_GAMMA)
        self.assertEqual(coop.install_kind(self.anomaly), coop.KIND_GAMMA)
        self.assertEqual(coop.status(self.anomaly), coop.COOP)
        self.assertFalse(self.core.exists())
        self.assertEqual(mod.read_text(), "-- core")
        self.assertIn(f"+{coop.MOD_NAME}", modlist.read_text())
        # The engine half's own gamedata stays where it was.
        self.assertTrue((self.anomaly / "gamedata" / "scripts" / "x.script").is_file())
        coop.switch_to_singleplayer(self.anomaly, gamma, "G.A.M.M.A")
        self.assertFalse((self.anomaly / "gamedata" / "scripts" / "x.script").exists())

    def test_a_plain_install_switches_to_gamma(self):
        gamma = Path(self._tmp.name) / "gamma"
        (gamma / "profiles" / "G.A.M.M.A").mkdir(parents=True)
        (gamma / "profiles" / "G.A.M.M.A" / "modlist.txt").write_text("+GAMMA Base\n")
        self._install()
        coop.set_install_kind(self.anomaly, gamma, "G.A.M.M.A", coop.KIND_GAMMA)
        self.assertFalse(self.core.exists())
        self.assertTrue((gamma / "mods" / coop.MOD_NAME / "gamedata" / "scripts" / "xrr_core.script").is_file())
        self.assertTrue((gamma / "mods" / coop.MOD_NAME / "meta.ini").is_file())

    def test_launch_options_without_mo2(self):
        from commander_gui.steam_shortcuts import coop_launch_options

        options = coop_launch_options("/p", "G.A.M.M.A", "Anomaly", 480, "-x", mo2=False)
        self.assertEqual(options, "STEAM_COMPAT_DATA_PATH=/p SteamAppId=480 %command% -x")


if __name__ == "__main__":
    unittest.main()


class MultiplayerPageTests(unittest.TestCase):
    setUp, tearDown, _install = CoopTests.setUp, CoopTests.tearDown, CoopTests._install

    def test_refresh_keeps_unsaved_edits(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.multiplayer_page import MultiplayerPage

        QApplication.instance() or QApplication([])
        self._install()
        profile = SimpleNamespace(anomaly=str(self.anomaly), gamma=str(self.gamma), mo2_profile="G.A.M.M.A")
        window = SimpleNamespace(settings=SimpleNamespace(active_profile=profile), install_busy=False)
        with patch.object(coop, "find_downloaded_zip", return_value=None):
            page = MultiplayerPage(window)
            page.refresh()
            name = page._widgets["xrr_player_name"]
            self.assertEqual(name.text(), "Old")
            name.setText("New")
            name.textEdited.emit("New")
            page.refresh()  # switching back to the page
            self.assertEqual(name.text(), "New")
            # An emptied name is not saved: the host would see a new player.
            name.setText("")
            name.textEdited.emit("")
            self.assertTrue(page._save())
            self.assertEqual(name.text(), "Old")
            self.assertIn("xrr_player_name Old", (self.anomaly / "appdata" / "user.ltx").read_text())
            # Toggles save at once, no Save button.
            fire = page._widgets["xrr_friendly_fire"]
            fire.setChecked(not fire.isChecked())
            self.assertFalse(page._dirty)
            self.assertEqual(coop.read_settings(self.anomaly)[0]["xrr_friendly_fire"], fire.isChecked())
        page.deleteLater()

    def test_unversioned_build_says_custom_and_first_role_is_host(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.multiplayer_page import MultiplayerPage

        QApplication.instance() or QApplication([])
        self._install()
        state = coop.load_state(self.anomaly)
        state["release"] = {"title": "", "version": ""}
        coop._save_state(self.anomaly, state)
        profile = SimpleNamespace(anomaly=str(self.anomaly), gamma=str(self.gamma), mo2_profile="G.A.M.M.A")
        window = SimpleNamespace(settings=SimpleNamespace(active_profile=profile), install_busy=False)
        with patch.object(coop, "find_downloaded_zip", return_value=None):
            page = MultiplayerPage(window)
            page.refresh()
            self.assertEqual(page.version_stat.value.text(), "Custom")
            self.assertTrue(page.host_pill.isChecked())
            self.assertEqual(page.tab_group.checkedId(), 0)
        page.deleteLater()

    def test_plain_anomaly_shortcut_runs_the_game_without_its_splash(self):
        from commander_gui.steam_shortcuts import coop_launch_options
        from commander_gui.ui.play_page import plain_game_exe

        (self.anomaly / "bin").mkdir(exist_ok=True)
        (self.anomaly / "bin" / "AnomalyDX10.exe").write_bytes(b"MZ")
        (self.anomaly / "AnomalyLauncher.cfg").write_text("DX10\n\n0\n")
        self.assertEqual(plain_game_exe(self.anomaly).name, "AnomalyDX10.exe")
        (self.anomaly / "bin" / "AnomalyDX11AVX.exe").write_bytes(b"MZ")
        (self.anomaly / "AnomalyLauncher.cfg").write_text("DX11\nAVX\n0\n")
        self.assertEqual(plain_game_exe(self.anomaly).name, "AnomalyDX11AVX.exe")
        self.assertTrue(coop_launch_options("/p", "", "t", 41700, "-nosplashwindow", mo2=False).endswith("%command% -nosplashwindow"))

    def test_copied_files_keep_their_mode(self):
        src = self.anomaly / "src.bin"
        src.write_text("x")
        src.chmod(0o755)
        coop._copy_atomic(src, self.anomaly / "dst.bin")
        self.assertEqual((self.anomaly / "dst.bin").stat().st_mode & 0o777, 0o755)

    def test_mirrors_play_page_launch_status(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QApplication, QLabel

        from commander_gui.ui.multiplayer_page import MultiplayerPage

        class FakePlay(QObject):
            launch_state_changed = Signal(bool)
            launch_status_changed = Signal()
            is_launching = False

        QApplication.instance() or QApplication([])
        self._install()
        play = FakePlay()
        play.playtime_label, play.launch_live_status = QLabel("Total playtime: 1h"), QLabel("")
        profile = SimpleNamespace(anomaly=str(self.anomaly), gamma=str(self.gamma), mo2_profile="G.A.M.M.A")
        window = SimpleNamespace(settings=SimpleNamespace(active_profile=profile), install_busy=False, _pages={"play": play})
        with patch.object(coop, "find_downloaded_zip", return_value=None):
            page = MultiplayerPage(window)
            page.refresh()
            self.assertEqual(page.playtime_label.text(), "Total playtime: 1h")
            self.assertTrue(page.launch_live_status.isHidden())
            play.launch_live_status.setText("Mod Organizer 2 is running")
            play.launch_status_changed.emit()
            self.assertEqual(page.launch_live_status.text(), "Mod Organizer 2 is running")
            self.assertFalse(page.launch_live_status.isHidden())
        page.deleteLater()

    def test_lan_session_keeps_join_address_with_spacewar(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.multiplayer_page import MultiplayerPage

        QApplication.instance() or QApplication([])
        self._install()
        coop.set_steam_appid(self.anomaly, coop.SPACEWAR_APPID)
        coop.write_settings(self.anomaly, {"xrr_host_steam": False, "xrr_host_session": False})
        profile = SimpleNamespace(anomaly=str(self.anomaly), gamma=str(self.gamma), mo2_profile="G.A.M.M.A")
        window = SimpleNamespace(settings=SimpleNamespace(active_profile=profile), install_busy=False)
        with patch.object(coop, "find_downloaded_zip", return_value=None), patch.object(coop, "lan_ip", return_value="10.0.0.2"):
            page = MultiplayerPage(window)
            page.refresh()
            # Spacewar only matters for Steam sessions: LAN joiners still need the address box.
            self.assertFalse(page.join_tips.isHidden())
            self.assertFalse(page.join_address.isHidden())
            self.assertTrue(page.join_warn.isHidden())
        page.deleteLater()

    def test_offers_reapply_when_an_update_reverts_coop(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.multiplayer_page import MultiplayerPage

        QApplication.instance() or QApplication([])
        self._install()
        profile = SimpleNamespace(anomaly=str(self.anomaly), gamma=str(self.gamma), mo2_profile="G.A.M.M.A")
        window = SimpleNamespace(settings=SimpleNamespace(active_profile=profile), install_busy=False)
        with patch.object(coop, "find_downloaded_zip", return_value=None), \
                patch("commander_gui.ui.multiplayer_page.QTimer.singleShot") as later:
            page = MultiplayerPage(window)
            # An update that leaves co-op alone asks nothing.
            page.on_busy_changed(True)
            page.on_busy_changed(False)
            later.assert_not_called()
            # One that puts the original exe back offers to re-apply.
            page.on_busy_changed(True)
            (self.anomaly / "bin" / "anomalydx11.exe").write_bytes(b"MZgamma-updated")
            page.on_busy_changed(False)
            later.assert_called_once_with(0, page._offer_reapply)
            # Already reverted before the next operation: no nagging.
            page.on_busy_changed(True)
            page.on_busy_changed(False)
            later.assert_called_once()
        page.deleteLater()

    def test_edits_save_to_the_profile_they_came_from(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.multiplayer_page import MultiplayerPage

        QApplication.instance() or QApplication([])
        self._install()
        other = Path(self._tmp.name) / "other"
        (other / "appdata").mkdir(parents=True)
        (other / "appdata" / "user.ltx").write_text("xrr_player_name Other\n")
        profile = SimpleNamespace(anomaly=str(self.anomaly), gamma=str(self.gamma), mo2_profile="G.A.M.M.A")
        window = SimpleNamespace(settings=SimpleNamespace(active_profile=profile), install_busy=False)
        with patch.object(coop, "find_downloaded_zip", return_value=None):
            page = MultiplayerPage(window)
            page.refresh()
            name = page._widgets["xrr_player_name"]
            name.setText("Typed")
            name.textEdited.emit("Typed")
            # The active profile changes before the name field loses focus.
            window.settings.active_profile = SimpleNamespace(anomaly=str(other), gamma=str(self.gamma), mo2_profile="G.A.M.M.A")
            self.assertTrue(page._save())
        self.assertEqual(coop.read_settings(self.anomaly)[0]["xrr_player_name"], "Typed")
        self.assertEqual(coop.read_settings(other)[0]["xrr_player_name"], "Other")
        page.deleteLater()

    def test_default_player_name_must_be_changed_before_launch(self):
        from unittest.mock import patch

        from commander_gui.ui import multiplayer_page

        anomaly = Path(self._tmp.name) / "fresh"
        (anomaly / "appdata").mkdir(parents=True)
        ask = "commander_gui.ui.multiplayer_page.QInputDialog.getText"
        # Cancelled: no launch, nothing written.
        with patch(ask, return_value=("", False)):
            self.assertFalse(multiplayer_page.confirm_player_name(None, anomaly))
        # The default again is asked again; a real name is saved.
        with patch(ask, side_effect=[("player", True), ("  Strelok  ", True)]) as dialog:
            self.assertTrue(multiplayer_page.confirm_player_name(None, anomaly))
        self.assertEqual(dialog.call_count, 2)
        self.assertEqual(coop.read_settings(anomaly)[0]["xrr_player_name"], "Strelok")
        # Already named: no pop-up.
        with patch(ask) as dialog:
            self.assertTrue(multiplayer_page.confirm_player_name(None, anomaly))
        dialog.assert_not_called()


class Mo2BlacklistTests(unittest.TestCase):
    def test_unblock_mo2_python_keeps_other_entries(self):
        from commander_gui.launcher import unblock_mo2_python

        with tempfile.TemporaryDirectory() as tmp:
            ini = Path(tmp) / "ModOrganizer.ini"
            ini.write_text("[General]\ngameName=STALKER Anomaly\n\n[pluginBlacklist]\nsize=2\n1\\name=plugin_python\n2\\name=foo.dll\n\n[Settings]\nx=1\n")
            self.assertTrue(unblock_mo2_python(tmp))
            text = ini.read_text()
            self.assertIn("[pluginBlacklist]\nsize=1\n1\\name=foo.dll\n", text)
            self.assertNotIn("plugin_python", text)
            self.assertIn("[Settings]\nx=1", text)
            self.assertFalse(unblock_mo2_python(tmp))
