"""Regressions from the pre-release bug sweep (1.4.0-Unstable3)."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from commander_gui import gui_settings
from commander_gui.fomod import parse_config
from commander_gui.keep_state import capture, discard
from commander_gui.mod_install import ModInstallError
from commander_gui.ui.game_splash import engine_started


class KeptStateResumeTest(unittest.TestCase):
    def _install(self, tmp: str) -> SimpleNamespace:
        gamma = Path(tmp) / "gamma"
        anomaly = Path(tmp) / "anomaly"
        (gamma / "profiles" / "G.A.M.M.A").mkdir(parents=True)
        (gamma / "mods" / "My Mod").mkdir(parents=True)
        (gamma / "profiles" / "G.A.M.M.A" / "modlist.txt").write_text("+My Mod\n")
        (anomaly / "bin").mkdir(parents=True)
        (anomaly / "bin" / "AnomalyDX11.exe").write_bytes(b"MZ mine")
        return SimpleNamespace(gamma=str(gamma), anomaly=str(anomaly), mo2_profile="G.A.M.M.A")

    def test_resume_after_a_restart_keeps_the_users_modlist_and_exes(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._install(tmp)
            first = capture(profile, keep_exes=True)
            # The failed run wrote GAMMA's stock list; COMMANDER was closed.
            modlist = Path(profile.gamma) / "profiles" / "G.A.M.M.A" / "modlist.txt"
            modlist.write_text("+Stock\n")
            again = capture(profile, keep_exes=True, resume=True)
            self.assertEqual(again.modlist, ["+My Mod"])
            self.assertEqual(again.exe_copy, first.exe_copy)
            self.assertTrue((again.exe_copy / "AnomalyDX11.exe").is_file())
            discard(again)
            self.assertFalse((Path(profile.anomaly) / ".commander-keep-state.json").exists())
            # A fresh (not resumed) run captures what is there now.
            self.assertEqual(capture(profile).modlist, ["+Stock"])


class GuiSettingsTest(unittest.TestCase):
    def test_unhashable_values_in_a_hand_edited_file_load(self):
        path = gui_settings.gui_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"deck_overlay_shortcut": [], "hidden_nav_tabs": [["x"], "mods"]}))
        os.utime(path, (time.time() + 5, time.time() + 5))  # past the cache
        data = gui_settings.load_gui_settings()
        self.assertEqual(data["deck_overlay_shortcut"], "l4r4")
        self.assertNotIn(["x"], data["hidden_nav_tabs"])


class FomodDepthTest(unittest.TestCase):
    def test_deeply_nested_conditions_are_refused(self):
        nested = "<dependencies>" * 200 + "</dependencies>" * 200
        xml = (
            "<config><installSteps><installStep name='s'><visible>"
            f"{nested}</visible><optionalFileGroups/></installStep></installSteps></config>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ModuleConfig.xml"
            path.write_text(xml)
            with self.assertRaises(ModInstallError):
                parse_config(path)


class EngineStartedTest(unittest.TestCase):
    def test_finds_the_marker_in_a_growing_log(self):
        from commander_gui.ui.game_splash import MARKERS

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "xray.log"
            since = time.time() - 1
            log.write_bytes(b"loading\n" * 1000)
            self.assertIsNone(engine_started([Path(tmp)], since))
            marker = MARKERS[0]
            with open(log, "ab") as handle:  # split across two reads
                handle.write(marker[:3])
            self.assertIsNone(engine_started([Path(tmp)], since))
            with open(log, "ab") as handle:
                handle.write(marker[3:] + b"\n")
            self.assertEqual(engine_started([Path(tmp)], since), log)


class InstallRootTest(unittest.TestCase):
    def test_mixed_relative_and_absolute_folders_do_not_raise(self):
        from commander_gui.ui.install_page import InstallPage

        root = InstallPage._install_root(("gamma/anomaly", "/games/GAMMA/gamma", "c"))
        self.assertTrue(root)
        self.assertEqual(InstallPage._install_root(("", "/games/G/gamma", "c")), "/games/G")


if __name__ == "__main__":
    unittest.main()
