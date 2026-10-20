"""MO2 profiles named with non-ASCII characters (CLIKRΔD - ROMEO).

MO2 2.5 dies at startup when ``-p`` carries such a name, so Commander
selects the profile in ModOrganizer.ini instead.
"""

import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QSettings

from commander_gui.launcher import Runner, build_command, mo2_profile_args
from commander_gui.steam_shortcuts import coop_launch_options

NAMES = ["CLIKRΔD - ROMEO", " CLIKRΔD - ROMEO ", "a;b,cΔ", "Δ=1", "ÄBCé0"]


class Mo2ProfileArgsTest(unittest.TestCase):
    def setUp(self):
        self.gamma = Path(tempfile.mkdtemp())
        self.ini = self.gamma / "ModOrganizer.ini"
        self.ini.write_text(
            "[General]\r\nselected_profile=@ByteArray(G.A.M.M.A)\r\nversion=2.5.2\r\n\r\n[Widgets]\r\nx=1\r\n"
        )

    def test_ascii_names_still_go_through_dash_p(self):
        self.assertEqual(mo2_profile_args(str(self.gamma), "G.A.M.M.A"), ["-p", "G.A.M.M.A"])
        self.assertIn("selected_profile=@ByteArray(G.A.M.M.A)", self.ini.read_text())

    def test_non_ascii_names_are_selected_in_the_ini_qt_reads_back(self):
        for name in NAMES:
            with self.subTest(name=name):
                self.assertEqual(mo2_profile_args(str(self.gamma), name), [])
                settings = QSettings(str(self.ini), QSettings.IniFormat)
                self.assertEqual(bytes(settings.value("selected_profile")).decode(), name)
                self.assertEqual(settings.value("version"), "2.5.2")
        text = self.ini.read_text()
        self.assertEqual(text.count("selected_profile="), 1)
        self.assertIn("\n[Widgets]\nx=1\n", text)

    def test_launch_command_leaves_out_dash_p_for_a_non_ascii_name(self):
        (self.gamma / "ModOrganizer.exe").touch()
        (self.gamma / "profiles" / NAMES[0]).mkdir(parents=True)
        command, _env, _cwd = build_command(str(self.gamma), Runner(kind="wine", label="Wine"), profile=NAMES[0])
        self.assertNotIn("-p", command)

    def test_coop_launch_options_without_a_profile_leave_out_dash_p(self):
        self.assertEqual(
            coop_launch_options("/p", "", "Anomaly", 41700),
            "STEAM_COMPAT_DATA_PATH=/p SteamAppId=41700 %command% run -e Anomaly",
        )


if __name__ == "__main__":
    unittest.main()


class SelectedProfileTests(unittest.TestCase):
    def test_non_ascii_name_round_trips_through_the_ini(self):
        from commander_gui.launcher import qt_ini_text, select_mo2_profile

        with tempfile.TemporaryDirectory() as tmp:
            ini = Path(tmp) / "ModOrganizer.ini"
            ini.write_text("[General]\nselected_profile=@ByteArray(G.A.M.M.A)\n")
            self.assertEqual(select_mo2_profile(tmp, "CLIKRΔD - ROMEO"), (0, ""))
            self.assertIn(r"selected_profile=@ByteArray(CLIKR\xce\x94\x44 - ROMEO)", ini.read_text())
            # What `mo2 config get selected-profile` prints for it.
            self.assertEqual(qt_ini_text(r"CLIKR\xce\x94\x44 - ROMEO"), "CLIKRΔD - ROMEO")
            self.assertEqual(qt_ini_text("G.A.M.M.A"), "G.A.M.M.A")
            self.assertEqual(select_mo2_profile(str(Path(tmp) / "missing"), "x")[0], 1)
