"""Every part of COMMANDER must act on the same Wine prefix a launch uses.

A friend's report: after the runner was changed in Settings, Open Winecfg
edited one prefix while MO2 ran in another, so the DPI set in Winecfg never
showed in MO2. The Settings page fell back to the previous runner's prefix;
the Play page used the new runner's default.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from commander_gui import gui_settings


class SavedPrefixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_new_runner_does_not_inherit_the_old_runners_prefix(self):
        gui_settings.save_gui_settings(
            runner="umup:/p/GE-Proton10-34/proton",
            wine_prefix="/games/old-prefix",
            prefixes={"auto": "/games/old-prefix"},
        )
        self.assertEqual(gui_settings.saved_prefix_for("umup:/p/GE-Proton10-34/proton"), "")
        self.assertEqual(gui_settings.saved_prefix_for("auto"), "/games/old-prefix")

    def test_legacy_single_prefix_is_kept_for_its_own_runner(self):
        gui_settings.save_gui_settings(runner="auto", wine_prefix="/games/legacy")
        self.assertEqual(gui_settings.saved_prefix_for("auto"), "/games/legacy")
        self.assertEqual(gui_settings.saved_prefix_for("umu"), "")

    def test_umu_default_is_never_reused_for_steam_proton(self):
        gui_settings.save_gui_settings(
            prefixes={"proton:/s/proton": "/home/x/Games/umu/umu-default"}
        )
        self.assertEqual(gui_settings.saved_prefix_for("proton:/s/proton"), "")

    def test_winecfg_and_the_play_page_resolve_the_same_prefix(self):
        from commander_gui.launcher import Runner

        gui_settings.save_gui_settings(
            runner="umup:/p/GE-Proton10-34/proton",
            wine_prefix="/games/old-prefix",
            prefixes={"auto": "/games/old-prefix"},
        )
        seen = []

        def fake_resolve(kind, prefix=""):
            seen.append((kind, prefix))
            return Runner("umu", "x", [], {"WINEPREFIX": prefix or "/default"})

        with patch("commander_gui.launcher.resolve_runner", side_effect=fake_resolve):
            gui_settings.configured_runner()
        # The Play page's rule for the same runner: saved or its default.
        self.assertEqual(seen, [("umup:/p/GE-Proton10-34/proton", "")])


if __name__ == "__main__":
    unittest.main()
