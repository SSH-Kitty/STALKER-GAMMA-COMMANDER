"""Deck Mode's Mods screen: hold A to pick a mod up, D-pad to carry it, A to drop."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.test_steamdeck import DeckWindowFixture

#: Two categories. modlist.txt is written bottom-up, so on screen this is
#: Weapons (W2, W1) above Audio (A2, A1).
_MODLIST = "+A1\n+A2\n+Audio_separator\n+W1\n+W2\n+Weapons_separator\n"


class DeckGamepadHoldTests(unittest.TestCase):
    """A tells a tap from a hold only where hold_gate asks for it."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from Steamdeck import gamepad as pad

        self.pad = pad
        self.monitor = pad.GamepadMonitor(input_dir=Path(tempfile.mkdtemp()))
        self.addCleanup(self.monitor.stop)
        self.actions = []
        self.monitor.action.connect(self.actions.append)

    def test_without_a_gate_a_fires_on_the_press(self):
        pad = self.pad
        self.monitor.feed(pad.EV_KEY, pad.BTN_SOUTH, 1)
        self.assertEqual(self.actions, [pad.ACCEPT])
        self.monitor.feed(pad.EV_KEY, pad.BTN_SOUTH, 0)
        self.assertEqual(self.actions, [pad.ACCEPT])

    def test_gated_tap_fires_accept_on_release(self):
        pad = self.pad
        self.monitor.hold_gate = lambda: True
        self.monitor.feed(pad.EV_KEY, pad.BTN_SOUTH, 1)
        self.assertEqual(self.actions, [])
        self.assertTrue(self.monitor.accept_down())
        self.monitor.feed(pad.EV_KEY, pad.BTN_SOUTH, 0)
        self.assertEqual(self.actions, [pad.ACCEPT])
        self.assertFalse(self.monitor.accept_down())

    def test_gated_hold_fires_accept_hold_and_nothing_on_release(self):
        pad = self.pad
        self.monitor.hold_gate = lambda: True
        self.monitor.feed(pad.EV_KEY, pad.BTN_SOUTH, 1)
        self.monitor._accept_timer.timeout.emit()  # HOLD_MS elapsed
        self.assertEqual(self.actions, [pad.ACCEPT_HOLD])
        self.monitor.feed(pad.EV_KEY, pad.BTN_SOUTH, 0)
        self.assertEqual(self.actions, [pad.ACCEPT_HOLD])

    def test_pad_lost_mid_press_fires_nothing(self):
        pad = self.pad
        self.monitor.hold_gate = lambda: True
        self.monitor.feed(pad.EV_KEY, pad.BTN_SOUTH, 1)
        self.monitor._release_all()
        self.monitor._on_accept_held()
        self.monitor.feed(pad.EV_KEY, pad.BTN_SOUTH, 0)
        self.assertEqual(self.actions, [])


class StartupModeTests(unittest.TestCase):
    """"When COMMANDER starts" is honoured on any machine, not only a Deck."""

    def test_always_opens_deck_mode_on_a_desktop_pc(self):
        from commander_gui.main import startup_wants_deck

        asked = []
        ask = lambda: asked.append(1) or True
        self.assertTrue(startup_wants_deck("always", on_deck=False, ask=ask))
        self.assertTrue(startup_wants_deck("always", on_deck=True, ask=ask))
        self.assertFalse(startup_wants_deck("never", on_deck=True, ask=ask))
        self.assertFalse(startup_wants_deck("ask", on_deck=False, ask=ask))
        self.assertEqual(asked, [])
        self.assertTrue(startup_wants_deck("ask", on_deck=True, ask=ask))
        self.assertEqual(asked, [1])


class DeckModReorderTests(DeckWindowFixture):
    def setUp(self):
        super().setUp()
        self.modlist.write_text(_MODLIST, encoding="utf-8")
        for target in ("Steamdeck.modlist_io.mo2_running", "Steamdeck.screens.mods.mo2_running"):
            patcher = patch(target, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mods = self.goto("mods")
        self.mods.list.setFocus()
        self.pump()

    def _names(self):
        return [item.text() for item in self.mods.mod_items()]

    def _select(self, name):
        for item in self.mods.mod_items():
            if item.text() == name:
                self.mods.list.setCurrentItem(item)
                return item
        raise AssertionError(name)

    def _dispatch(self, action):
        self.window._focus.dispatch(action)
        self.pump()

    def test_screen_asks_for_holds_only_on_the_list(self):
        self.assertTrue(self.window._accept_hold_wanted())
        self.mods.search_button.setFocus()
        self.pump()
        self.assertFalse(self.window._accept_hold_wanted())

    def test_carry_across_a_category_and_drop_writes_once(self):
        from Steamdeck import gamepad as pad

        self.assertEqual(self._names(), ["W2", "W1", "A2", "A1"])
        self._select("W1")
        self._dispatch(pad.ACCEPT_HOLD)
        self.assertTrue(self.mods.is_grabbing())
        with patch("Steamdeck.screens.mods.write_lines") as write:
            self._dispatch(pad.DOWN)  # past the Audio header
            self._dispatch(pad.DOWN)  # below A2
            write.assert_not_called()
        self._dispatch(pad.ACCEPT)
        self.assertFalse(self.mods.is_grabbing())
        self.assertEqual(self._names(), ["W2", "A2", "W1", "A1"])
        self.assertEqual(
            self.modlist.read_text(encoding="utf-8"),
            "+A1\n+W1\n+A2\n+Audio_separator\n+W2\n+Weapons_separator\n",
        )
        self.assertEqual(self.mods.list.currentItem().text(), "W1")

    def test_moving_just_under_a_header_lands_at_the_category_top(self):
        from Steamdeck import gamepad as pad

        self._select("A1")
        self._dispatch(pad.ACCEPT_HOLD)
        self._dispatch(pad.UP)
        self._dispatch(pad.UP)  # directly under the Audio header
        self._dispatch(pad.ACCEPT)
        self.assertEqual(self._names(), ["W2", "W1", "A1", "A2"])

    def test_nothing_goes_above_the_first_header(self):
        from Steamdeck import gamepad as pad

        self._select("W2")
        self._dispatch(pad.ACCEPT_HOLD)
        self._dispatch(pad.UP)
        self._dispatch(pad.UP)
        self.assertEqual(self.mods.list.row(self.mods._grabbed), 1)
        with patch("Steamdeck.screens.mods.write_lines") as write:
            self._dispatch(pad.ACCEPT)
        write.assert_not_called()

    def test_b_puts_the_mod_back_without_writing(self):
        from Steamdeck import gamepad as pad

        original = self.modlist.read_text(encoding="utf-8")
        self._select("W1")
        self._dispatch(pad.ACCEPT_HOLD)
        self._dispatch(pad.DOWN)
        self._dispatch(pad.BACK)
        self.assertFalse(self.mods.is_grabbing())
        self.assertEqual(self._names(), ["W2", "W1", "A2", "A1"])
        self.assertEqual(self.modlist.read_text(encoding="utf-8"), original)
        self.assertEqual(self.window.current_key(), "mods")

    def test_leaving_the_screen_puts_the_mod_back(self):
        from Steamdeck import gamepad as pad

        self._select("W1")
        self._dispatch(pad.ACCEPT_HOLD)
        self._dispatch(pad.DOWN)
        self.goto("play")
        self.assertFalse(self.mods.is_grabbing())

    def test_tap_still_toggles(self):
        from Steamdeck import gamepad as pad

        self._select("W1")
        self._dispatch(pad.ACCEPT)
        self.assertFalse(self.mods.is_grabbing())
        self.assertIn("-W1", self.modlist.read_text(encoding="utf-8"))

    def test_hold_on_a_header_folds_it(self):
        from Steamdeck import gamepad as pad

        self.mods.list.setCurrentRow(0)
        self._dispatch(pad.ACCEPT_HOLD)
        self.assertFalse(self.mods.is_grabbing())
        self.assertEqual(self._names(), ["A2", "A1"])

    def test_grab_refused_while_searching(self):
        from Steamdeck import gamepad as pad

        self.mods.search.setText("W")
        self.pump()
        self._select("W1")
        with patch.object(self.window, "notify") as notify:
            self._dispatch(pad.ACCEPT_HOLD)
        self.assertFalse(self.mods.is_grabbing())
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
