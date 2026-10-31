"""The shortcuts that keep page switches fast still notice every change."""

import os
import tempfile
import unittest
from pathlib import Path

from commander_gui import overlays
from commander_gui.launcher import _set_ini_value
from commander_gui.ui.common import count_active_mods


class ModCountCacheTest(unittest.TestCase):
    def test_count_follows_modlist_and_mod_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp)
            for marker in ("ModOrganizer.exe", "ModOrganizer.ini"):
                (gamma / marker).write_text("")
            profile = gamma / "profiles" / "G.A.M.M.A"
            profile.mkdir(parents=True)
            for name in ("A", "B"):
                (gamma / "mods" / name).mkdir(parents=True)
            modlist = profile / "modlist.txt"
            modlist.write_text("+A\n-B\n+Missing\n+Cat_separator\n")
            self.assertEqual(count_active_mods(str(gamma)), (1, 2))
            self.assertEqual(count_active_mods(str(gamma)), (1, 2))  # cached

            modlist.write_text("+A\n+B\n")
            self.assertEqual(count_active_mods(str(gamma)), (2, 2))

            (gamma / "mods" / "B").rmdir()
            # A folder removed in the same instant still changes the stamp.
            os.utime(gamma / "mods", ns=(1, 1))
            self.assertEqual(count_active_mods(str(gamma)), (1, 1))


class IniWriteTest(unittest.TestCase):
    def test_unchanged_value_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            ini = Path(tmp) / "ModOrganizer.ini"
            ini.write_text("[General]\nselected_profile=A\n")
            os.utime(ini, ns=(1, 1))
            _set_ini_value(ini, "General", "selected_profile", "A")
            self.assertEqual(ini.stat().st_mtime_ns, 1)
            _set_ini_value(ini, "General", "selected_profile", "B")
            self.assertEqual(ini.read_text(), "[General]\nselected_profile=B\n")


class LutCacheTest(unittest.TestCase):
    def test_lut_follows_the_sliders(self):
        data = {f"vkbasalt_{name}": 0 for name in overlays.VKBASALT_COLOURS}
        plain = overlays.lut(data, size=3)
        warm = overlays.lut({**data, "vkbasalt_warmth": 50}, size=3)
        self.assertNotEqual(plain, warm)
        self.assertEqual(overlays.lut(data, size=3), plain)


if __name__ == "__main__":
    unittest.main()
