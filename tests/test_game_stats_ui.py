import os
import tempfile
import unittest
from typing import ClassVar
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from commander_gui.achievements import ACHIEVEMENTS, status_text
from commander_gui.game_stats import STAT_FIELDS, SaveStats, stat_value
from commander_gui.settings import CliProfile, CliSettings

SAVE = SaveStats(
    "quicksave_3",
    1_790_000_000.0,
    {"killed_monsters": 812, "killed_stalkers": 392, "artefacts_detected": 31},
    {"tourist": True},
)


def _achievement(key):
    return next(a for a in ACHIEVEMENTS if a.key == key)


class StatHelpersTest(unittest.TestCase):
    def test_total_kills_is_mutants_plus_stalkers(self):
        self.assertEqual(stat_value(SAVE, "total_kills"), 1204)

    def test_missing_counter_is_zero(self):
        self.assertEqual(stat_value(SAVE, "items_crafted"), 0)

    def test_every_field_reads(self):
        for key, _caption in STAT_FIELDS:
            self.assertIsInstance(stat_value(SAVE, key), int)

    def test_status_text(self):
        self.assertEqual(status_text(SAVE, _achievement("tourist")), "Unlocked")
        self.assertEqual(status_text(SAVE, _achievement("geologist")), "31 / 50")
        self.assertEqual(status_text(SAVE, _achievement("patriarch")), "Locked")
        self.assertEqual(status_text(None, _achievement("tourist")), "Locked")


class _QtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])


class AchievementsDialogTest(_QtTest):
    def test_lists_every_achievement_and_shows_selection(self):
        from commander_gui.ui.achievements_dialog import AchievementsDialog

        dialog = AchievementsDialog(None, SAVE)
        self.assertEqual(dialog.list.count(), len(ACHIEVEMENTS))
        self.assertEqual(dialog.detail_name.text(), "Tourist")
        index = [a.key for a in ACHIEVEMENTS].index("geologist")
        dialog.list.setCurrentRow(index)
        self.assertEqual(dialog.detail_name.text(), "Geologist")
        self.assertEqual(dialog.detail_state.text(), "31 / 50")
        self.assertEqual(dialog.detail_how.text(), "Detect 50 artefacts.")

    def test_works_without_a_save(self):
        from commander_gui.ui.achievements_dialog import AchievementsDialog

        dialog = AchievementsDialog(None, None)
        self.assertEqual(dialog.detail_state.text(), "Locked")


class DashboardStatsCardTest(_QtTest):
    def _page(self, profiles):
        from commander_gui.ui.dashboard import DashboardPage

        class FakeWindow:
            settings = CliSettings(profiles=profiles)
            install_busy = False
            install_operation = None

            def refresh_settings(self):
                pass

        return DashboardPage(FakeWindow())

    def test_shows_counters_from_the_newest_save(self):
        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch("commander_gui.ui.dashboard.latest_save_stats", return_value=None),
            # No real worker: its result could otherwise land mid-test and
            # redraw the card behind the assertions' back.
            patch("commander_gui.ui.dashboard.BackgroundTask.start"),
        ):
            page = self._page([CliProfile(active=True, profile_name="Mine")])
            self.assertFalse(page.stats_card.isHidden())
            self.assertFalse(page.achievements_button.isEnabled())
            self.assertIn("No saves yet", page.stats_source.text())

            page._on_stats_loaded(page._stats_task, SAVE)
            self.assertEqual(page._stat_values["total_kills"].text(), "1,204")
            self.assertEqual(page._stat_values["items_crafted"].text(), "0")
            self.assertIn("quicksave_3", page.stats_title.toolTip())
            self.assertTrue(page.stats_source.isHidden())
            self.assertTrue(page.achievements_button.isEnabled())
            self.assertIn(f"1 / {len(ACHIEVEMENTS)}", page.achievements_button.text())

    def test_stale_result_is_dropped(self):
        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch("commander_gui.ui.dashboard.latest_save_stats", return_value=None),
        ):
            page = self._page([CliProfile(active=True, profile_name="Mine")])
            page._on_stats_loaded(object(), SAVE)
            self.assertEqual(page._stat_values["total_kills"].text(), "–")

    def test_hidden_without_a_profile(self):
        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            page = self._page([])
            self.assertTrue(page.stats_card.isHidden())


class StorageCardTest(DashboardStatsCardTest):
    def test_shows_free_space_and_warns_when_low(self):
        from PySide6.QtWidgets import QLabel, QProgressBar

        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch("commander_gui.ui.dashboard.latest_save_stats", return_value=None),
            patch(
                "commander_gui.ui.dashboard.disk_usage_bytes",
                return_value=(1000 * 1024**3, 10 * 1024**3),
            ),
        ):
            page = self._page(
                [CliProfile(active=True, profile_name="Mine", gamma=tmp)]
            )
            page._render_sizes(
                {"Anomaly": 1, "GAMMA": 2, "Cache": 3}, page._refresh_generation
            )
            texts = [w.text() for w in page.sizes_card.findChildren(QLabel)]
            self.assertTrue(any(t.startswith("Free on drive: 10") for t in texts))
            self.assertIn("99% used", texts)
            meter = page.sizes_card.findChild(QProgressBar)
            self.assertEqual(meter.objectName(), "storageMeterLow")

    def test_last_known_sizes_show_before_the_scan_finishes(self):
        from PySide6.QtWidgets import QLabel

        from commander_gui.gui_settings import load_gui_settings, save_gui_settings

        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch("commander_gui.ui.dashboard.latest_save_stats", return_value=None),
            # The scan never returns during the test: only saved numbers
            # or the placeholder can be on screen.
            patch("commander_gui.ui.dashboard.BackgroundTask.start"),
        ):
            profile = CliProfile(
                active=True, profile_name="Mine", anomaly="/a", gamma="/g", cache="/c"
            )
            page = self._page([profile])
            texts = [w.text() for w in page.sizes_card.findChildren(QLabel)]
            self.assertIn("Measuring folder sizes...", texts)

            save_gui_settings(
                storage_sizes={
                    "key": ["/a", "/g", "/c"],
                    "sizes": {"Anomaly": 2 * 1024**3, "GAMMA": 3 * 1024**3, "Cache": 0},
                }
            )
            page = self._page([profile])
            texts = [w.text() for w in page.sizes_card.findChildren(QLabel)]
            self.assertIn("Total: 5.0 GB", texts)

            # A finished scan replaces them and is saved for next launch.
            page._render_sizes(
                {"Anomaly": 1, "GAMMA": 1, "Cache": 1},
                page._refresh_generation,
                cache_key=("/a", "/g", "/c"),
            )
            self.assertEqual(
                load_gui_settings()["storage_sizes"]["sizes"],
                {"Anomaly": 1, "GAMMA": 1, "Cache": 1},
            )


class DashboardPlayButtonTest(_QtTest):
    def test_play_button_greys_on_the_first_click_after_startup(self):
        """The Play page is built by the click itself; the Dashboard must
        still hear its launch state straight away, not after a tab switch."""
        from PySide6.QtCore import QObject, Signal

        from commander_gui.ui.dashboard import DashboardPage

        class FakePlayPage(QObject):
            launch_state_changed = Signal(bool)

            def __init__(self):
                super().__init__()
                self.is_launching = False

            def launch_game(self):
                self.is_launching = True
                self.launch_state_changed.emit(True)

        class FakeWindow:
            settings = CliSettings(profiles=[CliProfile(active=True, profile_name="Mine")])
            install_busy = False
            install_operation = None

            def __init__(self):
                self._pages = {}

            def refresh_settings(self):
                pass

            def _ensure_page(self, key):
                return self._pages.setdefault(key, FakePlayPage())

        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch("commander_gui.ui.dashboard.latest_save_stats", return_value=None),
        ):
            window = FakeWindow()
            page = DashboardPage(window)
            self.assertNotIn("play", window._pages)
            page._play_gamma()
            self.assertFalse(page._play_button.isEnabled())
            window._pages["play"].is_launching = False
            window._pages["play"].launch_state_changed.emit(False)
            self.assertTrue(page._play_button.isEnabled())


class ModManagerMo2WatchTest(_QtTest):
    def test_warning_clears_when_mo2_closes_without_a_page_switch(self):
        from PySide6.QtWidgets import QStatusBar

        from commander_gui.ui import mod_manager_page
        from commander_gui.ui.mod_manager_page import ModManagerPage

        class FakeWindow:
            settings = CliSettings(profiles=[CliProfile(active=True, profile_name="Mine")])
            install_busy = False
            install_operation = None
            _pages: ClassVar[dict] = {}

            def refresh_settings(self):
                pass

            def update_mod_counter(self):
                pass

            def statusBar(self):
                return QStatusBar()

        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch.object(mod_manager_page, "mo2_running", return_value=True),
        ):
            page = ModManagerPage(FakeWindow())
            page.show()
            page._update_guard()
            self.assertTrue(page.guard_label.isVisible())
            self.assertTrue(page._mo2_watch.isActive())
        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch.object(mod_manager_page, "mo2_running", return_value=False),
            patch.object(ModManagerPage, "_load_mods"),
        ):
            page._check_mo2_closed()
            self.assertFalse(page.guard_label.isVisible())
            self.assertFalse(page._mo2_watch.isActive())
            page.hide()


if __name__ == "__main__":
    unittest.main()
