"""stalker-gamma-cli 1.40 output changes COMMANDER has to follow."""

import unittest

from commander_gui.parsers import parse_progress_line
from commander_gui.ui.install_page import _anomaly_check_ran, _full_install_args


class Cli140Test(unittest.TestCase):
    def test_size_unknown_download_line(self):
        # Shapes of the CLI's Informational and Verbose templates, ANSI stripped.
        for line in (
            "[12:00:00] MagsRedux                                 | Download   | 12.69 MB   | [40/120]",
            "MagsRedux                                 | Download   | 512 B      | [40/120] | https://github.com/x",
        ):
            event = parse_progress_line(line)
            self.assertIsNotNone(event, line)
            self.assertEqual((event.name, event.percent, event.complete), ("MagsRedux", 0.0, 40))
            self.assertTrue(event.amount.endswith("B"))
        event = parse_progress_line("[12:00:00] Addon | Download   | 42.50%     | [3/250]")
        self.assertEqual((event.size, event.amount), ("", "42.5%"))

    def test_anomaly_check_exit_1_with_results_ran(self):
        self.assertTrue(_anomaly_check_ran(1, "x | CORRUPT", 3))
        self.assertFalse(_anomaly_check_ran(1, "", 0))
        self.assertFalse(_anomaly_check_ran(1, "Unhandled exception", 3))

    def test_mirror_flag_only_online(self):
        self.assertIn("--use-anomaly-mirror", _full_install_args(False, True, True, anomaly_mirror=True))
        offline = _full_install_args(False, True, True, offline_lists=("a", "b"), anomaly_mirror=True)
        self.assertNotIn("--use-anomaly-mirror", offline)


if __name__ == "__main__":
    unittest.main()
