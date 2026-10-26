"""Desktop features ported to Deck Mode: Open MO2 launch options, the Help
screen's rendering of the desktop docs."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from commander_gui import gui_settings
from commander_gui.ui.about_docs import TOPICS
from commander_gui.ui.about_page import _translated


class OpenMo2LaunchOptionsTest(unittest.TestCase):
    def _command(self, target):
        from Steamdeck import launch

        gui_settings.save_gui_settings(custom_launch_options="mangohud %command% -nointro")
        profile = SimpleNamespace(gamma="/g", anomaly="", mo2_profile="")
        with (
            patch.object(launch, "resolve_runner", return_value=SimpleNamespace(env={})),
            patch.object(launch, "build_command", return_value=(["mo2"], {}, "/")),
            patch.object(launch, "ensure_runner_prefix"),
            patch.object(launch, "mo2_pids", return_value=set()),
        ):
            return launch._prepare_launch(profile, "auto", "", target, None, None, None)["command"]

    def test_open_mo2_drops_words_after_command(self):
        command = self._command(None)
        self.assertIn("mo2", command)
        self.assertNotIn("-nointro", command)

    def test_game_launch_keeps_them(self):
        self.assertIn("-nointro", self._command("Anomaly"))


class HelpScreenTest(unittest.TestCase):
    def test_every_topic_renders(self):
        from Steamdeck.screens.help import topic_html

        for topic in TOPICS:
            text = topic_html(_translated(topic), {})
            self.assertIn("<h3>", text, topic["title"])


class BuildRowTest(unittest.TestCase):
    def test_unstable_build_offers_going_back(self):
        from Steamdeck.screens.settings import SettingsScreen

        stub = SimpleNamespace(_appimage=True, build_row=Mock(), build_note=Mock(), _unstable_offer=None, _unstable_task=None)
        with patch("Steamdeck.screens.settings.is_unstable_version", return_value=True):
            SettingsScreen._render_build(stub)
        stub.build_row.set_value.assert_called_once_with("Unstable")
        self.assertIn("stable release", stub.build_note.setText.call_args[0][0])


class BackGripTest(unittest.TestCase):
    def _report(self, mask: int) -> bytes:
        import struct

        return bytes([1, 0, 9, 64]) + bytes(4) + struct.pack("<II", mask & 0xFFFFFFFF, mask >> 32) + bytes(48)

    def test_report_parsing(self):
        from Steamdeck.back_grips import L4, R4, buttons

        self.assertEqual(buttons(self._report(L4 | R4)), L4 | R4)
        self.assertIsNone(buttons(bytes([1, 0, 4, 64]) + bytes(60)))  # not a state report

    def test_held_chord_fires_once_and_navigation_only_when_on(self):
        from PySide6.QtWidgets import QApplication

        from Steamdeck import gamepad as pad
        from Steamdeck.back_grips import L4, R4, BackGripMonitor

        QApplication.instance() or QApplication([])
        monitor = BackGripMonitor()
        fired, actions = [], []
        monitor.shortcut.connect(lambda: fired.append(1))
        monitor.action.connect(actions.append)
        monitor.feed(L4 | R4)
        monitor._check_hold()  # the hold timer running out
        monitor._check_hold()
        self.assertEqual(fired, [1])
        monitor.feed(1 << 8)  # D-pad up, overlay closed
        self.assertEqual(actions, [])
        monitor.navigating = True
        monitor.feed(0)
        monitor.feed(1 << 8)
        self.assertEqual(actions, [pad.UP])

    def test_finds_only_the_deck_controller(self):
        import tempfile
        from pathlib import Path

        from Steamdeck.back_grips import deck_hidraw_nodes

        root = Path(tempfile.mkdtemp())
        for name, hid in (("hidraw0", "0003:0000046D:0000C52B"), ("hidraw2", "0003:000028DE:00001205")):
            (root / name / "device").mkdir(parents=True)
            (root / name / "device" / "uevent").write_text(f"HID_ID={hid}\n")
        self.assertEqual(deck_hidraw_nodes(root), [Path("/dev/hidraw2")])


if __name__ == "__main__":
    unittest.main()
