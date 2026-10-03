"""Tests for Steam Deck Mode.

Qt's platform plugin is chosen when PySide6 is first imported, and nothing
else in this repository sets one, so it has to be selected here - before any
PySide6 import, including the transitive ones through commander_gui.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from commander_gui import gui_settings
from commander_gui.deck_launch import (
    DECK_FLAG,
    DEPTH_ENV,
    DeckRelaunchError,
    deck_requested,
    in_game_mode,
    is_steam_deck,
    relaunch_argv,
    relaunch_exec,
    steam_deck_model,
    strip_deck_flag,
)
from commander_gui.winetricks import WINETRICKS_VERBS
from Steamdeck.screens import SCREEN_KEYS, SCREENS

REPO_ROOT = Path(__file__).resolve().parent.parent


# =====================================================================
# A. Relaunch mechanics (no Qt)
# =====================================================================
class DeckLaunchTests(unittest.TestCase):
    def test_flag_detection_and_stripping(self):
        self.assertFalse(deck_requested(["prog"]))
        self.assertTrue(deck_requested(["prog", DECK_FLAG]))
        self.assertEqual(strip_deck_flag(["prog", DECK_FLAG, "x"]), ["prog", "x"])
        # Idempotent: stripping twice is the same as stripping once.
        once = strip_deck_flag(["prog", DECK_FLAG, DECK_FLAG])
        self.assertEqual(strip_deck_flag(once), once)

    def test_relaunch_argv_preserves_interpreter_flags(self):
        """The AppImage regression: -s and -P must survive the re-exec.

        AppRun runs `python3.12 -s -P -m commander_gui`; losing -s would let
        the user's ~/.local PySide6 shadow the bundled one, and losing -P
        would let a stray commander_gui next to the cwd shadow the payload.
        """
        argv = relaunch_argv(
            deck=True,
            orig_argv=["python3.12", "-s", "-P", "-m", "commander_gui"],
            executable="/opt/py/bin/python3.12",
        )
        self.assertEqual(
            argv,
            ["/opt/py/bin/python3.12", "-s", "-P", "-m", "commander_gui", DECK_FLAG],
        )

    def test_relaunch_argv_round_trip(self):
        into = relaunch_argv(
            deck=True, orig_argv=["p", "-m", "commander_gui"], executable="/x"
        )
        self.assertEqual(into.count(DECK_FLAG), 1)
        out = relaunch_argv(deck=False, orig_argv=into, executable="/x")
        self.assertNotIn(DECK_FLAG, out)
        # Asking for deck again from an argv that already has the flag must
        # not accumulate copies of it.
        again = relaunch_argv(deck=True, orig_argv=into, executable="/x")
        self.assertEqual(again.count(DECK_FLAG), 1)

    def test_relaunch_argv_without_orig_argv(self):
        argv = relaunch_argv(deck=True, orig_argv=[], executable="/x")
        self.assertEqual(argv, ["/x", "-m", "commander_gui", DECK_FLAG])

    def test_lock_is_released_before_exec(self):
        """The single-instance bug, pinned.

        execve keeps the PID, so a lock still held at exec time makes the
        replacement process believe another COMMANDER is running and exit
        with no window at all.
        """
        order = []
        with patch.dict(os.environ, {DEPTH_ENV: "0"}, clear=False), patch(
            "commander_gui.deck_launch.os.execve",
            side_effect=lambda *a, **k: order.append("exec"),
        ):
            relaunch_exec(
                deck=True,
                release_lock=lambda: order.append("unlock"),
            )
        self.assertEqual(order, ["unlock", "exec"])

    def test_exec_failure_is_reported_not_raised(self):
        with patch.dict(os.environ, {DEPTH_ENV: "0"}, clear=False), patch(
            "commander_gui.deck_launch.os.execve",
            side_effect=OSError(12, "Cannot allocate memory"),
        ):
            self.assertFalse(
                relaunch_exec(deck=True, release_lock=lambda: None)
            )

    def test_relaunch_loop_guard(self):
        execve = Mock()
        with (
            patch.dict(os.environ, {DEPTH_ENV: "2"}, clear=False),
            patch("commander_gui.deck_launch.os.execve", execve),
            self.assertRaises(DeckRelaunchError),
        ):
            relaunch_exec(deck=True, release_lock=lambda: None)
        execve.assert_not_called()

    def test_depth_is_incremented_for_the_child(self):
        captured = {}

        def fake_execve(path, argv, env):
            captured.update(env)

        with (
            patch.dict(os.environ, {DEPTH_ENV: "0"}, clear=False),
            patch("commander_gui.deck_launch.os.execve", fake_execve),
        ):
            relaunch_exec(deck=True, release_lock=lambda: None)
        self.assertEqual(captured[DEPTH_ENV], "1")


class DeckDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _dmi(self, value: str) -> Path:
        path = self.tmp / "product_name"
        path.write_text(value, encoding="utf-8")
        return path

    def test_known_models(self):
        self.assertEqual(steam_deck_model(dmi_path=self._dmi("Jupiter"), env={}), "lcd")
        self.assertEqual(
            steam_deck_model(dmi_path=self._dmi("Galileo\n"), env={}), "oled"
        )

    def test_other_hardware(self):
        self.assertIsNone(
            steam_deck_model(dmi_path=self._dmi("20HQS0R200"), env={})
        )
        self.assertFalse(is_steam_deck(dmi_path=self._dmi("ThinkPad"), env={}))

    def test_env_fallback(self):
        missing = self.tmp / "nope"
        self.assertEqual(
            steam_deck_model(dmi_path=missing, env={"SteamDeck": "1"}), "unknown"
        )
        self.assertIsNone(steam_deck_model(dmi_path=missing, env={"SteamDeck": "0"}))
        self.assertIsNone(steam_deck_model(dmi_path=missing, env={}))

    def test_unreadable_dmi_is_not_an_error(self):
        path = self._dmi("Jupiter")
        with patch.object(
            Path, "read_text", side_effect=PermissionError("denied")
        ):
            self.assertIsNone(steam_deck_model(dmi_path=path, env={}))

    def test_game_mode_needs_both_signals(self):
        self.assertFalse(in_game_mode({"SteamDeck": "1"}))
        self.assertFalse(in_game_mode({"XDG_CURRENT_DESKTOP": "gamescope"}))
        self.assertTrue(
            in_game_mode(
                {"SteamDeck": "1", "XDG_CURRENT_DESKTOP": "gamescope"}
            )
        )


# =====================================================================
# B. Settings
# =====================================================================
class DeckSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._reset_cache()
        self.addCleanup(self._reset_cache)

    @staticmethod
    def _reset_cache():
        # load_gui_settings caches by path+mtime; a fresh temp dir per test
        # would otherwise keep reading the previous one.
        gui_settings._cache = None
        gui_settings._cache_path_str = ""
        gui_settings._cache_file_mtime = 0.0

    def _write_raw(self, **values):
        path = gui_settings.gui_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values), encoding="utf-8")
        self._reset_cache()

    def test_defaults(self):
        data = gui_settings.load_gui_settings()
        self.assertEqual(data["deck_mode_preference"], "ask")
        self.assertEqual(data["deck_font_scale"], 100)
        self.assertEqual(data["deck_start_screen"], "play")
        self.assertIs(data["deck_runner_confirmed"], False)

    def test_round_trip(self):
        gui_settings.save_gui_settings(
            deck_mode_preference="always",
            deck_font_scale=130,
            deck_start_screen="mods",
            deck_runner_confirmed=True,
        )
        self._reset_cache()
        data = gui_settings.load_gui_settings()
        self.assertEqual(data["deck_mode_preference"], "always")
        self.assertEqual(data["deck_font_scale"], 130)
        self.assertEqual(data["deck_start_screen"], "mods")
        self.assertIs(data["deck_runner_confirmed"], True)

    def test_preference_is_coerced(self):
        for bad in ("maybe", 42, None, ["always"]):
            self._write_raw(deck_mode_preference=bad)
            self.assertEqual(
                gui_settings.load_gui_settings()["deck_mode_preference"], "ask"
            )

    def test_start_screen_rejects_desktop_nav_keys(self):
        """Desktop-only pages must not leak into the Deck start screen.

        Deck Mode shares "dashboard" with the desktop window, but these
        three have no Deck screen behind them, so accepting one would leave
        the window with a start screen it cannot build. "utilities" used to
        be in this list too, before Deck Mode got its own Utilities screen.
        """
        for desktop_only in ("modmanager", "systemcheck", "about"):
            self._write_raw(deck_start_screen=desktop_only)
            self.assertEqual(
                gui_settings.load_gui_settings()["deck_start_screen"],
                "play",
                desktop_only,
            )

    def test_font_scale_is_clamped(self):
        for bad, expected in (
            ("abc", 100),
            (None, 100),
            (9999, 150),
            (-5, 80),
            (float("inf"), 100),  # json's bare Infinity token; int() raises
        ):
            self._write_raw(deck_font_scale=bad)
            self.assertEqual(
                gui_settings.load_gui_settings()["deck_font_scale"], expected
            )

    def test_runner_confirmed_accepts_string_bools(self):
        self._write_raw(deck_runner_confirmed="true")
        self.assertIs(
            gui_settings.load_gui_settings()["deck_runner_confirmed"], True
        )
        self._write_raw(deck_runner_confirmed="nonsense")
        self.assertIs(
            gui_settings.load_gui_settings()["deck_runner_confirmed"], False
        )

    def test_validator_matches_the_screen_list(self):
        """gui_settings hardcodes the screen keys; keep the two in step.

        Deriving them from Steamdeck.screens would make gui_settings import
        the Deck GUI and invert the dependency, so this test stands in for
        that import.
        """
        source = Path(gui_settings.__file__).read_text(encoding="utf-8")
        match = re.search(
            r'"deck_start_screen"\s*\]\s*not in \{(.*?)\}', source, re.DOTALL
        )
        self.assertIsNotNone(match, "could not find the deck_start_screen check")
        declared = set(re.findall(r'"([a-z_]+)"', match.group(1)))
        self.assertEqual(declared, set(SCREEN_KEYS))


# =====================================================================
# C. Theming
# =====================================================================
class DeckThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # _stylesheet_complaints() builds a QWidget, and constructing one
        # before a QApplication exists aborts the process rather than
        # raising. It cannot be left to whichever test gets there first:
        # unittest runs them in name order, which is not the order they are
        # written in.
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_every_theme_substitutes_completely(self):
        from commander_gui.themes import THEME_INFO
        from Steamdeck.deck_theme import build_deck_stylesheet

        for key, _label, _desc, _swatches in THEME_INFO:
            qss = build_deck_stylesheet(key)
            # safe_substitute leaves unknown tokens in place, and Qt then
            # silently drops the entire rule they appear in - so a missing
            # token is an invisible, whole-section styling failure.
            self.assertNotIn("$", qss, f"unsubstituted token in {key}")
            self.assertNotIn("%FONT_FAMILY%", qss)
            self.assertGreater(len(qss), 500)

    def test_unknown_theme_falls_back(self):
        from Steamdeck.deck_theme import build_deck_stylesheet

        self.assertNotIn("$", build_deck_stylesheet("no-such-theme"))

    def test_font_scale(self):
        import re

        from Steamdeck.deck_theme import build_deck_stylesheet

        base = build_deck_stylesheet("gamma")
        scaled = build_deck_stylesheet("gamma", font_scale=150)
        sizes = [int(n) for n in re.findall(r"font-size: (\d+)px", base)]
        scaled_sizes = [int(n) for n in re.findall(r"font-size: (\d+)px", scaled)]
        self.assertEqual(len(sizes), len(scaled_sizes))
        for before, after in zip(sizes, scaled_sizes):
            self.assertEqual(after, max(9, round(before * 1.5)))

    def test_focus_rules_exist_for_every_focusable_class(self):
        from Steamdeck.deck_theme import build_deck_stylesheet

        qss = build_deck_stylesheet("gamma")
        for selector in (
            "QPushButton:focus",
            "QLineEdit:focus",
            "QListWidget:focus",
            "QCheckBox:focus",
            "QPushButton#deckHeroDanger:focus",
            "QPushButton#deckChip:checked:focus",
            "QPushButton#deckKey:checked:focus",
            'QPushButton#deckNavCell[current="true"]:focus',
            "QListWidget::item:selected:focus",
        ):
            self.assertIn(selector, qss)

    def test_qt_accepts_every_theme(self):
        """Qt must parse each sheet without complaint.

        Applied to a throwaway widget tree, never to the QApplication.
        QApplication.setStyleSheet() re-polishes every widget alive in the
        process, and by the time this runs the rest of the suite has left
        hundreds of orphaned ones behind - re-polishing those segfaults the
        interpreter. A widget-level sheet goes through the same parser.

        Qt parses lazily, so the probe has to be shown and polished or a
        broken sheet would sail through unnoticed; the check that this
        actually detects one is test_a_broken_stylesheet_is_detected below.
        """
        from commander_gui.themes import THEME_INFO
        from Steamdeck.deck_theme import build_deck_stylesheet

        for key, _label, _desc, _swatches in THEME_INFO:
            self.assertEqual(
                self._stylesheet_complaints(build_deck_stylesheet(key)),
                [],
                f"Qt rejected the {key} Deck stylesheet",
            )

    @staticmethod
    def _stylesheet_complaints(sheet: str) -> list[str]:
        """Warnings Qt emits while polishing a probe widget with ``sheet``."""
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
        from PySide6.QtWidgets import (
            QPushButton,
            QVBoxLayout,
            QWidget,
        )

        complaints: list[str] = []

        def handler(mode, _context, message):
            if mode == QtMsgType.QtWarningMsg and "stylesheet" in message.lower():
                complaints.append(message)

        probe = QWidget()
        layout = QVBoxLayout(probe)
        layout.addWidget(QPushButton("probe"))
        previous = qInstallMessageHandler(handler)
        try:
            probe.setStyleSheet(sheet)
            probe.show()
            probe.ensurePolished()
        finally:
            qInstallMessageHandler(previous)
            probe.hide()
            probe.deleteLater()
        return complaints

    def test_a_broken_stylesheet_is_detected(self):
        """Proves the check above is not silently vacuous.

        Uses an unsubstituted ``$token`` specifically, since that is the
        failure build_deck_stylesheet can actually produce: string.Template's
        safe_substitute leaves an unknown token in place, and Qt then drops
        the whole rule it appears in.
        """
        self.assertTrue(
            self._stylesheet_complaints("QPushButton { color: $accent; }")
        )


# =====================================================================
# C2. DeckProgress cancel routing
# =====================================================================
class DeckProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_set_cancellable_routes_cancel_through_the_given_callable(self):
        from Steamdeck.widgets import DeckProgress

        progress = DeckProgress()
        calls = []
        progress.set_cancellable(lambda: calls.append(1))
        self.assertFalse(progress.pause_button.isVisible() and progress.pause_button.isEnabled())
        self.assertTrue(progress.cancel_button.isEnabled())
        progress._cancel()
        self.assertEqual(calls, [1])

    def test_set_cancellable_none_disables_cancel(self):
        from Steamdeck.widgets import DeckProgress

        progress = DeckProgress()
        progress.set_cancellable(None)
        self.assertFalse(progress.cancel_button.isEnabled())

    def test_set_runner_restores_pause_after_set_cancellable(self):
        from unittest.mock import Mock

        from Steamdeck.widgets import DeckProgress

        progress = DeckProgress()
        progress.set_cancellable(lambda: None)
        self.assertTrue(progress.pause_button.isHidden())
        progress.set_runner(Mock())
        self.assertFalse(progress.pause_button.isHidden())


# =====================================================================
# C3. The Deck-native folder picker (Move Installation's destination)
# =====================================================================
class DeckFolderPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_list_subfolders_returns_only_real_directories_sorted(self):
        from Steamdeck.folder_picker import list_subfolders

        (self.tmp / "b").mkdir()
        (self.tmp / "a").mkdir()
        (self.tmp / "file.txt").write_text("x", encoding="utf-8")
        (self.tmp / "link").symlink_to(self.tmp / "a")
        self.assertEqual(
            [p.name for p in list_subfolders(self.tmp)], ["a", "b"]
        )

    def test_list_subfolders_on_a_missing_path_returns_empty(self):
        from Steamdeck.folder_picker import list_subfolders

        self.assertEqual(list_subfolders(self.tmp / "missing"), [])

    def test_candidate_roots_includes_home_and_real_mounts_only(self):
        from Steamdeck import folder_picker

        media = self.tmp / "media"
        user_dir = media / "deck"
        user_dir.mkdir(parents=True)
        mounted = user_dir / "sdcard"
        mounted.mkdir()
        not_mounted = user_dir / "not-a-mount"
        not_mounted.mkdir()
        with (
            patch.object(folder_picker, "_MEDIA_ROOTS", (media,)),
            patch(
                "Steamdeck.folder_picker.os.path.ismount",
                side_effect=lambda p: Path(p) == mounted,
            ),
        ):
            roots = folder_picker.candidate_roots()
        labels = dict(roots)
        self.assertIn(Path.home(), labels.values())
        self.assertIn(mounted, labels.values())
        self.assertNotIn(not_mounted, labels.values())

    def test_folder_picker_navigates_up_and_down(self):
        from Steamdeck.folder_picker import _UP, DeckFolderPicker

        sub = self.tmp / "sub"
        sub.mkdir()
        picker = DeckFolderPicker(self.tmp)
        self.assertEqual(picker.current(), self.tmp)
        picker._activate(sub)
        self.assertEqual(picker.current(), sub)
        picker._activate(_UP)
        self.assertEqual(picker.current(), self.tmp)

    def test_show_folder_picker_calls_on_choose_and_dismisses(self):
        from PySide6.QtWidgets import QPushButton

        from Steamdeck.folder_picker import show_folder_picker
        from Steamdeck.window import DeckWindow

        config = self.tmp / "config"
        (config / "stalker-gamma").mkdir(parents=True)
        (config / "stalker-gamma" / "settings.json").write_text("{}", encoding="utf-8")
        with patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(config), "COMMANDER_DECK_WINDOWED": "1"},
        ):
            window = DeckWindow()
            try:
                window.setGeometry(0, 0, 1280, 800)
                window.show()
                self.app.processEvents()

                chosen = []
                sub = self.tmp / "sub"
                sub.mkdir()
                show_folder_picker(
                    window, title="Pick", start=self.tmp, on_choose=chosen.append
                )
                self.app.processEvents()
                self.assertIsNotNone(window.current_overlay())

                overlay = window.current_overlay()
                from Steamdeck.folder_picker import DeckFolderPicker

                body = overlay.findChild(DeckFolderPicker)
                self.assertIsNotNone(body)
                body._activate(sub)

                use_button = next(
                    b
                    for b in overlay.findChildren(QPushButton)
                    if b.text() == "Use this folder"
                )
                use_button.click()
                self.app.processEvents()

                self.assertEqual(chosen, [sub])
                self.assertIsNone(window.current_overlay())
            finally:
                window.close()
                window.deleteLater()
                self.app.processEvents()


# =====================================================================
# Shared fixture for the widget tests
# =====================================================================
class DeckWindowFixture(unittest.TestCase):
    """Builds a real DeckWindow against a throwaway config and install tree."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        cli = bin_dir / "stalker-gamma"
        cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        cli.chmod(0o755)

        self.anomaly = self.tmp / "anomaly"
        self.gamma = self.tmp / "gamma"
        for marker in ("AnomalyLauncher.exe", "fsgame.ltx"):
            (self.anomaly / marker).parent.mkdir(parents=True, exist_ok=True)
            (self.anomaly / marker).write_text("", encoding="utf-8")
        for marker in ("ModOrganizer.exe", "ModOrganizer.ini"):
            (self.gamma / marker).parent.mkdir(parents=True, exist_ok=True)
            (self.gamma / marker).write_text("", encoding="utf-8")
        self.modlist = self.gamma / "profiles" / "G.A.M.M.A" / "modlist.txt"
        self.modlist.parent.mkdir(parents=True, exist_ok=True)
        self.modlist.write_text(
            "+Weapons_separator\n+Alpha Mod\n-Beta Mod\n+Gamma Mod\n",
            encoding="utf-8",
        )

        config = self.tmp / "config"
        (config / "stalker-gamma").mkdir(parents=True)
        (config / "stalker-gamma" / "settings.json").write_text(
            json.dumps(
                {
                    "Profiles": [
                        {
                            "Active": True,
                            "ProfileName": "Deck",
                            "Anomaly": str(self.anomaly),
                            "Gamma": str(self.gamma),
                            "Cache": str(self.tmp / "cache"),
                            "Mo2Profile": "G.A.M.M.A",
                            "DownloadThreads": 6,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        self._env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(config),
                "STALKER_GAMMA_CLI": str(cli),
                "COMMANDER_DECK_WINDOWED": "1",
                # Never read the host's real controllers from the test suite.
                "COMMANDER_DECK_NO_GAMEPAD": "1",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        DeckSettingsTests._reset_cache()
        self.addCleanup(DeckSettingsTests._reset_cache)

        # Both of these shell out to package managers and Wine tooling from a
        # background thread. Left real they make the suite depend on the
        # host's toolchain, and - worse - a subprocess still running when the
        # window is torn down can take the interpreter down with it in a
        # later, unrelated test (the hazard tests/conftest.py documents for
        # network calls).
        for target, result in (
            (
                "Steamdeck.screens.system._collect_checks",
                ([{"label": "Wine", "state": "ready", "detail": ""}], True, {}),
            ),
            ("Steamdeck.screens.install.check_all_dependencies", []),
            (
                "Steamdeck.screens.install.check_winetricks_full_status",
                {verb: True for verb in WINETRICKS_VERBS},
            ),
        ):
            patcher = patch(target, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Deck tests run before the rest of the suite (see the ordering hook
        # in tests/conftest.py), so the process is still clean here and
        # DeckWindow's real app-wide restyle is safe to exercise.
        # Scroll glides are instant here: tests check where focus and the
        # view end up straight after a key press, not the animation.
        from Steamdeck.widgets import DeckSmoothScrollArea

        glide = patch.object(DeckSmoothScrollArea, "GLIDE_MS", 0)
        glide.start()
        self.addCleanup(glide.stop)

        # The Welcome overlay opens on a timer and would take the D-pad
        # scope away from the screen under test; the COMMANDER update check
        # is a network call.
        from commander_gui import __version__

        gui_settings.save_gui_settings(welcome_hidden=True, welcome_seen_version=__version__)
        update_check = patch(
            "Steamdeck.window.DeckWindow._check_commander_update", return_value=None
        )
        update_check.start()
        self.addCleanup(update_check.stop)

        from Steamdeck.window import DeckWindow

        self.window = DeckWindow()
        self.window.setGeometry(0, 0, 1280, 800)
        self.window.show()
        self.addCleanup(self._teardown_window)
        self.pump()

    def _teardown_window(self):
        from commander_gui.ui.common import (
            begin_shutdown,
            resume_after_shutdown,
            shutdown_active_runners,
        )

        # Background tasks outliving the window segfault the interpreter in a
        # later, unrelated test - the same hazard tests/conftest.py documents.
        begin_shutdown()
        shutdown_active_runners(timeout_ms=3000)
        resume_after_shutdown()
        self.window.close()
        self.window.deleteLater()
        self.pump()

    def pump(self, rounds: int = 8):
        for _ in range(rounds):
            self.app.processEvents()

    def goto(self, key: str):
        self.window.set_page(key)
        self.pump()
        return self.window._pages[key]


# =====================================================================
# D. Window and layout
# =====================================================================
class DeckLayoutTests(DeckWindowFixture):
    def test_every_screen_fits_the_deck_panel(self):
        """No screen may demand more than the Deck's content area.

        Asserted on size hints, not on screen geometry: the offscreen
        platform reports a fixed virtual screen unrelated to a real Deck.
        """
        from Steamdeck.widgets import CONTENT_H, DECK_W

        for key in SCREEN_KEYS:
            page = self.goto(key)
            hint = page.minimumSizeHint()
            self.assertLessEqual(hint.width(), DECK_W, key)
            self.assertLessEqual(hint.height(), CONTENT_H, key)

    def test_long_job_ends_with_a_result_panel_and_chime(self):
        import time as _time

        with (
            patch("Steamdeck.window.play_click_sound") as chime,
            patch("Steamdeck.window.notify_desktop"),
        ):
            self.window._busy_since = _time.monotonic()
            self.window.announce_finished("Short", "done")
            self.pump()
            self.assertIsNone(self.window.current_overlay())
            self.window._busy_since = _time.monotonic() - 3600
            self.window.announce_finished("GAMMA install finished", "done")
            self.pump()
            self.assertIsNotNone(self.window.current_overlay())
            self.assertEqual(chime.call_count, 2)
            self.window.dismiss_overlay()

    def test_play_screen_offers_crash_analysis(self):
        from PySide6.QtWidgets import QLabel

        page = self.goto("play")
        with patch.object(self.window.gamepad, "rumble") as rumble:
            page.controller.crashed.emit()
            self.pump()
        overlay = self.window.current_overlay()
        self.assertIsNotNone(overlay)
        texts = " ".join(label.text() for label in overlay.findChildren(QLabel))
        self.assertIn("Game Crashed", texts)
        rumble.assert_called_once()
        self.window.dismiss_overlay()

        dump = Path(tempfile.gettempdir()) / "dump.zip"
        # Game Mode: never opens ASSISTANT, shows where the dump is.
        with (
            patch("Steamdeck.screens.play.in_game_mode", return_value=True),
            patch("Steamdeck.screens.play.launch_assistant") as launch,
        ):
            page._on_crash_dump_done((dump, {}))
            self.pump()
        launch.assert_not_called()
        texts = " ".join(label.text() for label in self.window.current_overlay().findChildren(QLabel))
        self.assertIn(str(dump), texts)
        self.window.dismiss_overlay()
        # Desktop Mode: straight into ASSISTANT.
        with (
            patch("Steamdeck.screens.play.in_game_mode", return_value=False),
            patch("Steamdeck.screens.play.launch_assistant") as launch,
        ):
            page._on_crash_dump_done((dump, {}))
            self.pump()
        launch.assert_called_once_with(dump)
        self.assertIsNone(self.window.current_overlay())

    def test_update_screen_lists_each_release_collapsed(self):
        from types import SimpleNamespace

        page = self.goto("update")
        notes = (
            "# **GAMMA 0.9.5**\n## General\nBig **armor** rework.\n\n"
            "# **S.T.A.L.K.E.R. G.A.M.M.A. 0.9.4 Patch Notes**\nOlder notes.\n"
        )
        status = SimpleNamespace(update_available=False, diffs=[], patchnotes=notes)
        with patch("Steamdeck.screens.update.status_summary", return_value=("GAMMA is up to date", "accent")):
            page._on_checked(status)
        self.pump()
        titles = [r.header.title_label.text() for r in page._releases]
        self.assertEqual(titles, ["GAMMA 0.9.5", "GAMMA 0.9.4"])
        self.assertFalse(any(r.is_open() for r in page._releases))
        first = page._releases[0]
        first.header.activated.emit()
        self.pump()
        self.assertTrue(first.is_open())
        text = first.panel.label.text()
        self.assertIn("Big armor rework.", text)
        self.assertNotIn("#", text)
        first.header.activated.emit()
        self.pump()
        self.assertFalse(first.is_open())

    def test_mod_list_scrolls_smoothly_by_pixel(self):
        from PySide6.QtWidgets import QAbstractItemView

        page = self.goto("mods")
        self.assertEqual(
            page.list.verticalScrollMode(), QAbstractItemView.ScrollMode.ScrollPerPixel
        )

    def test_utilities_screen_offers_backup_and_restore(self):
        page = self.goto("utilities")
        self.assertTrue(callable(page._backup_now))
        self.assertIn(page._backup_task, (None,))
        page._backup_now()
        self.pump()
        overlay = self.window.current_overlay()
        self.assertIsNotNone(overlay)
        with patch.object(page, "_start_backup") as start:
            overlay.default_button.setText("My run")
            from PySide6.QtWidgets import QPushButton

            button = next(b for b in overlay.findChildren(QPushButton) if b.text() == "Back up")
            button.click()
            self.pump()
        start.assert_called_once_with("My run")
        install = self.goto("install")
        self.assertTrue(hasattr(install, "proton_builds_row"))

    def test_header_shows_the_wordmark_not_the_page_title(self):
        """The header's top-left is a persistent "COMMANDER" wordmark.

        Regression test for a UI overhaul that removed the old per-screen
        title label (the nav bar's own highlighted entry already shows which
        screen is current, so a second copy of that in the header was
        redundant clutter) in favour of the same branding the desktop UI
        shows in its top-left corner.
        """
        from PySide6.QtWidgets import QLabel

        self.goto("dashboard")
        wordmarks = [
            w for w in self.window.findChildren(QLabel) if w.objectName() == "deckWordmark"
        ]
        self.assertEqual(len(wordmarks), 1)
        self.assertEqual(wordmarks[0].text(), "COMMANDER")
        self.assertFalse(hasattr(self.window, "title_label"))

    def test_dashboard_update_row_still_opens_update_screen(self):
        """Regression test: restructuring the Updates section into a card

        must not disturb the row below it, whose only job is navigating to
        the full Update screen.
        """
        dashboard = self.goto("dashboard")
        dashboard.update_row.activated.emit()
        self.pump()
        self.assertEqual(self.window.current_key(), "update")

    def test_play_status_caption_reflects_launch_state(self):
        """Regression test: de-boxing the Status row into a plain caption

        must not silently drop the live launch-lifecycle feed.
        """
        play = self.goto("play")
        play._on_status("Running")
        self.assertEqual(play.status_caption.text(), "Running")

    def test_install_options_reach_full_install_args(self):
        """Regression test: the Preserve toggles are no longer hardcoded.

        Unchecking them on screen must genuinely change the CLI args the
        install invokes, not just look editable.
        """
        install = self.goto("install")
        install.preserve_user_row.set_checked(False, notify=True)
        install.preserve_mcm_row.set_checked(False, notify=True)
        with (
            patch("Steamdeck.screens.install.cli_command", return_value=["stub"]) as cli,
            patch("Steamdeck.screens.install.CommandRunner"),
        ):
            install._start_install()
        args = cli.call_args[0][0]
        self.assertNotIn("--preserve-user-settings", args)
        self.assertNotIn("--preserve-mcm-settings", args)
        # The mocked CommandRunner never fires .finished, so install_busy is
        # still True here - left alone, tearDown's window.close() would hit
        # the real "An install is still running, quit anyway?" QMessageBox
        # and hang forever with nothing to click it.
        install._finish_run()

    def test_interactive_widgets_are_touch_sized(self):
        from Steamdeck.focus import audit_focusables
        from Steamdeck.widgets import MIN_TOUCH

        # Every exception is listed by object name so loosening the rule has
        # to be a deliberate, reviewable act rather than a lowered threshold.
        allowed_small = {"deckJumpChip"}
        for key in SCREEN_KEYS:
            page = self.goto(key)
            for widget in audit_focusables(page):
                if widget.objectName() in allowed_small:
                    continue
                self.assertGreaterEqual(
                    widget.height(),
                    MIN_TOUCH,
                    f"{key}: {widget.objectName() or type(widget).__name__}"
                    f" is {widget.height()}px tall",
                )

    def test_every_control_is_reachable_by_dpad(self):
        """The automated proof of the D-pad contract.

        Explores each screen as a graph rather than sweeping in a fixed
        order: from every widget reached so far, press all four arrows and
        record where focus lands, until nothing new appears. A sweep would
        pass or fail depending on which direction ran first; this does not,
        and it is what catches a control placed where a thumbstick can never
        reach it.
        """
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        from Steamdeck.focus import audit_focusables

        arrows = (
            Qt.Key.Key_Down,
            Qt.Key.Key_Up,
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
        )
        for key in SCREEN_KEYS:
            page = self.goto(key)
            focusables = audit_focusables(page)
            if len(focusables) < 2:
                continue

            start_widget = focusables[0]
            reached = {start_widget}
            frontier = [start_widget]
            while frontier:
                widget = frontier.pop()
                for arrow in arrows:
                    widget.setFocus(Qt.FocusReason.OtherFocusReason)
                    self.pump(1)
                    QTest.keyClick(self.window, arrow)
                    self.pump(1)
                    landed = self.app.focusWidget()
                    if landed is not None and landed not in reached:
                        reached.add(landed)
                        frontier.append(landed)

            missing = [
                w.objectName() or type(w).__name__
                for w in focusables
                if w not in reached
            ]
            self.assertEqual(missing, [], f"{key}: unreachable {missing}")

    def test_dpad_never_leaves_the_screen_for_the_tab_bar(self):
        """Moving around stays inside the screen; tabs change with the
        shoulder buttons only (L1/L2, R1/R2)."""
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        from Steamdeck.focus import audit_focusables

        tabs = set(self.window._nav_buttons.values())
        for button in tabs:
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertFalse(tabs & set(audit_focusables(self.window.centralWidget())))
        self.goto("settings")
        content = audit_focusables(self.window._pages["settings"])
        self.assertTrue(content)
        content[-1].setFocus(Qt.FocusReason.OtherFocusReason)
        self.pump(2)
        for key in (Qt.Key.Key_Down, Qt.Key.Key_Down, Qt.Key.Key_Right, Qt.Key.Key_Left):
            QTest.keyClick(self.window, key)
            self.pump(1)
            self.assertNotIn(self.app.focusWidget(), tabs)

    def test_tapping_a_tab_still_switches_screens(self):
        self.goto("settings")
        self.window._nav_buttons["mods"].click()
        self.pump()
        self.assertEqual(self.window.current_key(), "mods")

    def test_status_bar_is_a_shim(self):
        from PySide6.QtWidgets import QStatusBar

        bar = self.window.statusBar()
        self.assertNotIsInstance(bar, QStatusBar)
        before = self.window.centralWidget().height()
        bar.showMessage("hello", 10)
        self.pump()
        self.assertEqual(self.window.centralWidget().height(), before)

    def test_close_does_not_overwrite_desktop_geometry(self):
        gui_settings.save_gui_settings(window_width=1080, window_height=950)
        DeckSettingsTests._reset_cache()
        self.window.close()
        self.pump()
        DeckSettingsTests._reset_cache()
        data = gui_settings.load_gui_settings()
        self.assertEqual(data["window_width"], 1080)
        self.assertEqual(data["window_height"], 950)


class DeckUtilitiesTests(DeckWindowFixture):
    """The Utilities screen: maintenance tools, guards, resets, moves."""

    def test_clean_cache_confirm_run_report_cycle(self):

        utilities = self.goto("utilities")
        with (
            patch("Steamdeck.screens.utilities.mo2_running", return_value=False),
            patch(
                "Steamdeck.screens.utilities.cli_command", return_value=["stub"]
            ) as cli,
            patch("Steamdeck.screens.utilities.CommandRunner"),
        ):
            utilities._prune_apply()
            self.pump()
            overlay = self.window.current_overlay()
            self.assertIsNotNone(overlay)
            # Action on the left, Cancel on the right - and Cancel focused.
            self.assertIs(overlay.default_button, overlay.buttons[-1])
            overlay.buttons[0].click()
            self.pump()
        cli.assert_called_once_with(["cache", "prune", "apply"])
        self.assertTrue(self.window.install_busy)
        # The mocked CommandRunner never fires .finished, so install_busy is
        # still True here - left alone, tearDown's window.close() would hang
        # on the real "quit anyway?" modal with nothing to click it (see the
        # Install screen's own test_install_options_reach_full_install_args
        # for the same lesson learned the hard way).
        utilities._on_cli_finished(0, "")
        self.assertFalse(self.window.install_busy)
        self.assertEqual(utilities.status.text(), "Done")

    def test_mo2_guard_blocks_after_confirm_too(self):
        """TOCTOU: mo2_running is re-checked inside the confirm callback,

        not just before the confirm dialog is shown.
        """

        utilities = self.goto("utilities")
        with (
            patch("Steamdeck.screens.utilities.mo2_running", return_value=True),
            patch("Steamdeck.screens.utilities.CommandRunner") as runner_cls,
        ):
            utilities._purge_shader_cache()
            self.pump()
            overlay = self.window.current_overlay()
            self.assertIsNotNone(overlay)
            overlay.buttons[0].click()
            self.pump()
            runner_cls.assert_not_called()
        self.assertFalse(self.window.install_busy)

    def test_busy_blocks_a_tool_before_any_confirm_is_shown(self):
        utilities = self.goto("utilities")
        self.window.install_busy = True
        utilities._purge_shader_cache()
        self.pump()
        self.assertIsNone(self.window.current_overlay())
        self.window.install_busy = False

    def test_fresh_reset_forces_preserve_off_gamma_reset_reads_toggles(self):
        from Steamdeck.screens.install import InstallScreen

        install = self.goto("install")
        utilities = self.goto("utilities")
        profile = utilities.profile()
        utilities._wipe_targets = (profile.anomaly, profile.gamma)

        for include_anomaly, toggle_state, expected in (
            (True, True, (False, False)),
            (False, True, (True, True)),
            (False, False, (False, False)),
        ):
            install.preserve_user_row.set_checked(toggle_state, notify=True)
            install.preserve_mcm_row.set_checked(toggle_state, notify=True)
            utilities._reset_includes_anomaly = include_anomaly
            with patch.object(
                InstallScreen, "start_auto_install", return_value=True
            ) as start_auto:
                utilities._on_reset_wiped("Title")
            kwargs = start_auto.call_args.kwargs
            self.assertEqual(
                (kwargs["preserve_user"], kwargs["preserve_mcm"]), expected
            )

    def test_move_installation_success_and_recovery_required(self):
        utilities = self.goto("utilities")
        profile = utilities.profile()
        sources = [
            ("Anomaly", profile.anomaly),
            ("GAMMA", profile.gamma),
            ("Cache", profile.cache),
        ]
        moved = [
            ("Anomaly", str(self.tmp / "moved_anomaly")),
            ("GAMMA", str(self.tmp / "moved_gamma")),
            ("Cache", str(self.tmp / "moved_cache")),
        ]
        with patch("Steamdeck.screens.utilities.StreamTask") as stream_cls:
            utilities._start_move(sources, self.tmp / "dest", profile.profile_name)
        self.assertTrue(self.window.install_busy)
        stream_cls.return_value.start.assert_called_once()

        with (
            patch("Steamdeck.screens.utilities._rewrite_mo2_ini_paths"),
            patch("Steamdeck.screens.utilities._save_moved_profile"),
        ):
            utilities._on_move_done(moved)
        self.assertFalse(self.window.install_busy)
        self.assertEqual(utilities.status.text(), "Move complete.")

        with (
            patch("Steamdeck.screens.utilities._rewrite_mo2_ini_paths"),
            patch(
                "Steamdeck.screens.utilities._save_moved_profile",
                side_effect=OSError("boom"),
            ),
        ):
            utilities._on_move_done(moved)
        self.assertIn("Move Recovery Required", utilities.status.text())
        self.assertFalse(self.window.install_busy)


class DeckBackStackTests(DeckWindowFixture):
    def test_escape_returns_to_the_start_screen(self):
        self.goto("settings")
        self.window.handle_back()
        self.pump()
        self.assertEqual(self.window.current_key(), "play")
        self.assertTrue(self.window.isVisible())

    def test_escape_at_home_asks_instead_of_quitting(self):
        """One stray B press must never close the app in Game Mode."""
        self.goto("play")
        self.window.handle_back()
        self.pump()
        self.assertIsNotNone(self.window.current_overlay())
        self.assertTrue(self.window.isVisible())

    def test_escape_dismisses_only_the_overlay(self):
        self.goto("play")
        self.window.confirm("T", "M", lambda: None)
        self.pump()
        self.assertIsNotNone(self.window.current_overlay())
        self.window.handle_back()
        self.pump()
        self.assertIsNone(self.window.current_overlay())
        self.assertEqual(self.window.current_key(), "play")

    def test_screen_back_hook_runs_first(self):
        mods = self.goto("mods")
        mods.search.setText("alpha")
        self.pump()
        self.window.handle_back()
        self.pump()
        self.assertEqual(mods.search.text(), "")
        self.assertEqual(self.window.current_key(), "mods")


class DeckEnvironmentTests(DeckWindowFixture):
    def test_fullscreen_on_deck_hardware(self):
        with (
            patch.dict(os.environ, {"COMMANDER_DECK_WINDOWED": ""}),
            patch("Steamdeck.window.is_steam_deck", return_value=True),
        ):
            self.window.show_for_environment()
            self.pump()
            self.assertTrue(self.window.isFullScreen())

    def test_windowed_elsewhere(self):
        with (
            patch.dict(os.environ, {"COMMANDER_DECK_WINDOWED": ""}),
            patch("Steamdeck.window.is_steam_deck", return_value=False),
            patch("Steamdeck.window.in_game_mode", return_value=False),
        ):
            self.window.show_for_environment()
            self.pump()
            self.assertFalse(self.window.isFullScreen())

    def test_windowed_override_wins(self):
        with patch("Steamdeck.window.is_steam_deck", return_value=True):
            self.window.show_for_environment()
            self.pump()
            self.assertFalse(self.window.isFullScreen())


# =====================================================================
# E. Behaviour guards
# =====================================================================
class DeckBehaviourTests(DeckWindowFixture):
    def test_mod_toggle_is_blocked_while_mo2_runs(self):
        mods = self.goto("mods")
        item = mods.mod_items()[0]
        self.assertIsNotNone(item)
        before = item.data(3 + 1)  # Qt.UserRole + 1
        with (
            patch("Steamdeck.modlist_io.mo2_running", return_value=True),
            patch("Steamdeck.modlist_io.save_lines") as save,
        ):
            mods._toggle(item)
        save.assert_not_called()
        self.assertEqual(item.data(3 + 1), before)

    def test_mod_toggle_is_blocked_during_an_install(self):
        mods = self.goto("mods")
        item = mods.mod_items()[0]
        self.window.install_busy = True
        with patch("Steamdeck.modlist_io.save_lines") as save:
            mods._toggle(item)
        self.window.install_busy = False
        save.assert_not_called()

    def test_mod_toggle_writes_and_backs_up(self):
        mods = self.goto("mods")
        item = mods.mod_items()[0]
        original = self.modlist.read_text(encoding="utf-8")
        with patch("Steamdeck.modlist_io.mo2_running", return_value=False):
            mods._toggle(item)
        backup = self.modlist.with_name(self.modlist.name + ".gammagui.bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        self.assertNotEqual(
            self.modlist.read_text(encoding="utf-8"), original
        )

    def test_switch_mode_refuses_while_busy(self):
        """Refused with an in-window notice - never a QMessageBox, which
        gamescope has no window manager to place."""
        from commander_gui.ui import deck_switch

        self.window.install_busy = True
        try:
            with (
                patch.object(deck_switch.QMessageBox, "warning") as warn,
                patch.object(self.window, "notify") as notify,
                patch("commander_gui.deck_launch.os.execve") as execve,
            ):
                deck_switch.switch_mode(self.window, deck=False)
            execve.assert_not_called()
            warn.assert_not_called()
            notify.assert_called_once()
        finally:
            self.window.install_busy = False

    def test_switch_mode_asks_in_an_overlay_while_mo2_runs(self):
        from commander_gui.ui import deck_switch

        with (
            patch.object(deck_switch, "mo2_running", return_value=True),
            patch.object(deck_switch.QMessageBox, "question") as question,
            patch.object(deck_switch, "_restart") as restart,
        ):
            deck_switch.switch_mode(self.window, deck=False)
            question.assert_not_called()
            restart.assert_not_called()
            self.assertIsNotNone(self.window.current_overlay())
        self.window.dismiss_overlay()

    def test_switch_mode_does_not_wait_on_background_work(self):
        """The switch must cancel background work, never wait it out.

        Waiting was the entire cost of changing modes: the Dashboard always
        has a winetricks probe, a size scan and an update check in flight
        when its Deck button is pressed, none of them interruptible, so the
        handoff sat out the full shutdown timeout - about ten seconds - with
        the window already closed. execve discards those threads wholesale,
        so there is nothing to wait for.
        """
        from commander_gui.ui import common, deck_switch

        with (
            patch.object(common, "shutdown_active_runners") as waited,
            patch.object(deck_switch, "cancel_active_runners") as cancelled,
            patch.object(deck_switch, "mo2_running", return_value=False),
            patch("commander_gui.deck_launch.os.execve"),
            patch.object(deck_switch.QMessageBox, "critical"),
            patch.object(deck_switch.QApplication, "instance", return_value=None),
        ):
            deck_switch.switch_mode(self.window, deck=True)

        cancelled.assert_called_once()
        waited.assert_not_called()

    def test_cancel_active_runners_returns_immediately(self):
        """Even with an uninterruptible task in flight."""
        import time

        from commander_gui.ui.common import BackgroundTask, cancel_active_runners

        task = BackgroundTask(time.sleep, 5, parent=self.window)
        task.start()
        self.pump(2)
        try:
            started = time.perf_counter()
            cancel_active_runners()
            elapsed = time.perf_counter() - started
        finally:
            from commander_gui.ui.common import resume_after_shutdown

            resume_after_shutdown()
        # Generously bounded: the regression this guards against was ten
        # seconds, so anything near a second would already be the bug back.
        self.assertLess(elapsed, 1.0, f"cancel took {elapsed:.2f}s")

    def test_install_busy_fans_out_to_every_screen(self):
        for key in SCREEN_KEYS:
            self.goto(key)
        seen = {}
        for key, page in self.window._pages.items():
            page.on_busy_changed = lambda busy, k=key: seen.__setitem__(k, busy)
        self.window.set_install_busy(True, "gamma")
        self.assertEqual(set(seen), set(SCREEN_KEYS))
        self.assertTrue(all(seen.values()))
        self.window.set_install_busy(False)


# =====================================================================
# F. Architecture guards
# =====================================================================
class DeckRebuildTests(DeckWindowFixture):
    def test_welcome_screen_survives_a_language_rebuild(self):
        """rebuild() used to read the overlay's reopen hook after clearing
        the overlay, so the Welcome screen closed on every language change."""
        from Steamdeck.welcome import show_welcome

        show_welcome(self.window)
        self.pump()
        self.window.rebuild()
        self.pump()
        overlay = self.window._overlay
        self.assertIsNotNone(overlay)
        self.assertTrue(callable(getattr(overlay, "reopen", None)))


class DeckFlippedLoadOrderTests(DeckWindowFixture):
    def test_play_offers_to_fix_a_reversed_load_order(self):
        """The old Flip Priority reversed modlist.txt, which crashes GAMMA:
        Play must catch it and put it back before launching."""
        from commander_gui import gui_settings
        from commander_gui.modlist import flip_priority
        from tests.test_flipped_load_order import GAMMA

        page = self.goto("play")
        modlist = Path(tempfile.mkdtemp()) / "modlist.txt"
        modlist.write_text("\n".join(flip_priority(GAMMA)) + "\n", encoding="utf-8")
        gui_settings.save_gui_settings(target="Anomaly (DX11)")
        with (
            patch("Steamdeck.screens.play.modlist_path_for", return_value=modlist),
            patch("Steamdeck.modlist_io.mo2_running", return_value=False),
            patch.object(page.controller, "launch") as launch,
        ):
            page._play()
            self.pump()
            overlay = self.window.current_overlay()
            self.assertIsNotNone(overlay)
            launch.assert_not_called()
            overlay.buttons[0].click()  # Fix and launch
            self.pump()
            launch.assert_called_once()
        self.assertEqual(modlist.read_text(encoding="utf-8").splitlines(), GAMMA)


class DeckArchitectureTests(unittest.TestCase):
    def test_commander_gui_touches_steamdeck_exactly_once(self):
        """The dependency must stay one-way and greppable."""
        hits = []
        for path in (REPO_ROOT / "commander_gui").rglob("*.py"):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                stripped = line.strip()
                # Only real import statements count; the name appears in
                # comments and docstrings that explain the boundary.
                if not stripped.startswith(("import ", "from ")):
                    continue
                if "Steamdeck" in stripped:
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{number}")
        self.assertEqual(len(hits), 1, f"expected one import site, found {hits}")
        self.assertTrue(hits[0].startswith("commander_gui/main.py"), hits)

    def test_package_init_is_qt_free(self):
        """build-appimage.sh imports this with no display attached."""
        source = (REPO_ROOT / "Steamdeck" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("PySide6", source)

    def test_deck_icon_avoids_qtsvg(self):
        """QtSvg bindings are pruned from the AppImage; the icon cannot use them."""
        source = (REPO_ROOT / "commander_gui" / "ui" / "deck_icon.py").read_text(
            encoding="utf-8"
        )
        imports = [
            line
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        self.assertFalse([line for line in imports if "QtSvg" in line])

    def test_screens_list_matches_the_modules(self):
        for key, title, glyph in SCREENS:
            self.assertTrue(title)
            self.assertTrue(glyph)
            self.assertTrue(
                (REPO_ROOT / "Steamdeck" / "screens" / f"{key}.py").is_file(), key
            )


class DeckIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_icon_renders_at_every_size(self):
        from PySide6.QtGui import QColor

        from commander_gui.ui.deck_icon import deck_icon

        for size in (16, 22, 32, 48):
            pixmap = deck_icon(QColor("#9fe96f"), size).pixmap(size, size)
            self.assertFalse(pixmap.isNull())
            image = pixmap.toImage()
            opaque = sum(
                1
                for y in range(image.height())
                for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 0
            )
            self.assertGreater(opaque, size, f"icon looks empty at {size}px")


class DashboardDeckButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_button_is_rebuilt_on_every_refresh(self):
        """It must survive clear_layout() by being remade, never retained."""
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QPushButton

        from commander_gui.settings import CliProfile, CliSettings
        from commander_gui.ui.dashboard import DashboardPage

        window = Mock()
        window.settings = CliSettings(
            profiles=[CliProfile(active=True, profile_name="Deck")], extra={}
        )
        window.install_busy = False
        window.install_operation = None

        with patch.object(DashboardPage, "refresh", lambda self: None):
            page = DashboardPage(window)
        page._build_actions()
        self.app.processEvents()
        first = page.findChild(QPushButton, "deckModeButton")
        self.assertIsNotNone(first)
        page._build_actions()
        # clear_layout() only schedules the old widgets for deletion, and
        # processEvents() alone does not run deferred deletes - without this
        # the stale button is still a child and still findable.
        self.app.processEvents()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        buttons = page.findChildren(QPushButton, "deckModeButton")
        self.assertEqual(len(buttons), 1)
        self.assertIsNot(first, buttons[0])
        page.deleteLater()


# =====================================================================
# G. Controller input (Steamdeck.gamepad), no window needed
# =====================================================================
class DeckGamepadTests(unittest.TestCase):
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

    def test_rumble_uploads_a_rumble_effect_and_plays_it(self):
        pad = self.pad
        device = pad._Device(Path("/dev/input/event99"), 5, "Steam Deck")
        self.monitor._devices[device.path] = device
        calls = {"ioctl": [], "write": []}

        def fake_ioctl(fd, request, buf):
            calls["ioctl"].append(request)
            if request == pad._eviocgbit(pad.EV_FF, len(buf)):
                buf[pad.FF_RUMBLE // 8] |= 1 << (pad.FF_RUMBLE % 8)
            elif request == pad._EVIOCSFF:
                buf[2:4] = (7).to_bytes(2, "little")  # kernel assigns id 7
            return 0

        with (
            patch.object(pad.os, "open", return_value=42),
            patch.object(pad.os, "write", side_effect=lambda fd, data: calls["write"].append(data)),
            patch.object(pad.os, "close"),
            patch.object(pad.fcntl, "ioctl", side_effect=fake_ioctl),
            patch.object(pad.GamepadMonitor, "available", return_value=True),
        ):
            self.assertEqual(self.monitor.rumble(strength=0.5, length_ms=300), 1)
        self.monitor._devices.clear()
        self.assertIn(pad._EVIOCSFF, calls["ioctl"])
        _sec, _usec, ev_type, code, value = pad._EVENT.unpack(calls["write"][0])
        self.assertEqual((ev_type, code, value), (pad.EV_FF, 7, 1))

    def test_rumble_skips_pads_without_force_feedback(self):
        pad = self.pad
        device = pad._Device(Path("/dev/input/event98"), 5, "Keyboard-ish pad")
        self.monitor._devices[device.path] = device
        with (
            patch.object(pad.os, "open", return_value=42),
            patch.object(pad.os, "write") as write,
            patch.object(pad.os, "close"),
            patch.object(pad.fcntl, "ioctl", return_value=0),
            patch.object(pad.GamepadMonitor, "available", return_value=True),
        ):
            self.assertEqual(self.monitor.rumble(), 0)
        self.monitor._devices.clear()
        write.assert_not_called()

    def test_face_buttons_fire_once_per_press(self):
        pad = self.pad
        for code, action in (
            (pad.BTN_SOUTH, pad.ACCEPT),
            (pad.BTN_EAST, pad.BACK),
            (pad.BTN_X, pad.CONTEXT),
            (pad.BTN_Y, pad.SEARCH),
            (pad.BTN_TL, pad.TAB_PREV),
            (pad.BTN_TR, pad.TAB_NEXT),
            (pad.BTN_START, pad.MENU),
        ):
            self.actions.clear()
            self.monitor.feed(pad.EV_KEY, code, 1)
            self.monitor.feed(pad.EV_KEY, code, 2)  # kernel autorepeat
            self.monitor.feed(pad.EV_KEY, code, 0)
            self.assertEqual(self.actions, [action], hex(code))

    def test_hat_directions_and_release(self):
        pad = self.pad
        self.monitor.feed(pad.EV_ABS, pad.ABS_HAT0Y, 1)
        self.monitor.feed(pad.EV_ABS, pad.ABS_HAT0Y, 0)
        self.monitor.feed(pad.EV_ABS, pad.ABS_HAT0X, -1)
        self.monitor.feed(pad.EV_ABS, pad.ABS_HAT0X, 0)
        self.assertEqual(self.actions, [pad.DOWN, pad.LEFT])
        self.assertFalse(self.monitor._repeat.isActive())

    def test_stick_has_hysteresis(self):
        pad = self.pad
        span = {pad.ABS_Y: (-32768, 32767)}
        for value in (0, 20000, 25000, 15000, 22000, 5000):
            self.monitor.feed(pad.EV_ABS, pad.ABS_Y, value, span)
        # Crossed "on" once at 20000; 15000 is still above "off", so the
        # wobble back up to 22000 is not a second press.
        self.assertEqual(self.actions, [pad.DOWN])

    def test_triggers_switch_tabs_like_the_bumpers(self):
        pad = self.pad
        span = {pad.ABS_Z: (0, 255), pad.ABS_RZ: (0, 255)}
        self.monitor.feed(pad.EV_ABS, pad.ABS_RZ, 255, span)
        self.monitor.feed(pad.EV_ABS, pad.ABS_RZ, 250, span)
        self.monitor.feed(pad.EV_ABS, pad.ABS_RZ, 0, span)
        self.monitor.feed(pad.EV_ABS, pad.ABS_Z, 200, span)
        self.assertEqual(self.actions, [pad.TAB_NEXT, pad.TAB_PREV])

    def test_a_trigger_reported_twice_is_one_pull(self):
        """Some pads send a trigger as a button and as an axis at once; one
        pull must not skip two tabs."""
        pad = self.pad
        span = {pad.ABS_RZ: (0, 255)}
        self.monitor.feed(pad.EV_KEY, pad.BTN_TR2, 1)
        self.monitor.feed(pad.EV_ABS, pad.ABS_RZ, 255, span)
        self.assertEqual(self.actions, [pad.TAB_NEXT])

    def test_held_direction_repeats(self):
        import time

        pad = self.pad
        self.monitor.feed(pad.EV_KEY, pad.BTN_DPAD_DOWN, 1)
        deadline = time.monotonic() + (pad.REPEAT_DELAY_MS + 3 * pad.REPEAT_RATE_MS) / 1000 + 0.3
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.monitor.feed(pad.EV_KEY, pad.BTN_DPAD_DOWN, 0)
        self.assertGreaterEqual(len(self.actions), 3)
        self.assertEqual(set(self.actions), {pad.DOWN})

    def test_decodes_raw_input_events_from_a_file_descriptor(self):
        import struct

        pad = self.pad
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        event = struct.Struct("llHHi")
        os.write(
            write_fd,
            event.pack(0, 0, pad.EV_KEY, pad.BTN_SOUTH, 1)
            + event.pack(0, 0, pad.EV_SYN, 0, 0)
            + event.pack(0, 0, pad.EV_KEY, pad.BTN_SOUTH, 0),
        )
        os.close(write_fd)
        device = pad._Device(Path("/dev/input/event99"), read_fd, "test pad")
        self.monitor._read(device)
        self.assertEqual(self.actions, [pad.ACCEPT])

    def test_no_input_directory_is_a_silent_no_op(self):
        monitor = self.pad.GamepadMonitor(input_dir=Path("/nonexistent/input"))
        monitor.start()
        self.assertEqual(monitor.device_names(), [])
        monitor.stop()


class DeckPowerTests(unittest.TestCase):
    def _supply(self, root, name, **files):
        folder = root / name
        folder.mkdir(parents=True)
        for key, value in files.items():
            (folder / key).write_text(value + "\n", encoding="ascii")

    def test_battery_discharging(self):
        from Steamdeck.power import battery_state, on_battery

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self._supply(root, "ACAD", type="Mains", online="0")
        self._supply(root, "BAT1", type="Battery", capacity="42", status="Discharging")
        state = battery_state(root)
        self.assertEqual((state.percent, state.charging, state.on_ac), (42, False, False))
        self.assertTrue(on_battery(root))

    def test_charging_and_peripheral_batteries(self):
        from Steamdeck.power import battery_state, on_battery

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        # A controller's battery (scope=Device) must never be mistaken for
        # the machine's own.
        self._supply(root, "hid-pad", type="Battery", scope="Device", capacity="5", status="Discharging")
        self._supply(root, "BAT1", type="Battery", capacity="80", status="Charging")
        state = battery_state(root)
        self.assertEqual((state.percent, state.charging), (80, True))
        self.assertFalse(on_battery(root))

    def test_no_battery(self):
        from Steamdeck.power import battery_state, on_battery

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.assertIsNone(battery_state(root))
        self.assertFalse(on_battery(root))

    def test_inhibitor_without_systemd_is_harmless(self):
        from Steamdeck.power import SleepInhibitor

        inhibitor = SleepInhibitor()
        with patch("Steamdeck.power.shutil.which", return_value=None):
            self.assertFalse(inhibitor.hold("test"))
        self.assertFalse(inhibitor.active())
        inhibitor.release()


class DeckSetupHelperTests(unittest.TestCase):
    def test_install_root_and_profile_paths(self):
        from Steamdeck.screens.setup import install_root_for, profile_for

        self.assertEqual(install_root_for(Path.home()), Path.home() / "Games" / "GAMMA")
        sd = Path("/run/media/deck/SD")
        self.assertEqual(install_root_for(sd), sd / "GAMMA")
        profile = profile_for(sd / "GAMMA", "GAMMA")
        self.assertEqual(profile.anomaly, str(sd / "GAMMA" / "Anomaly"))
        self.assertEqual(profile.gamma, str(sd / "GAMMA" / "GAMMA"))
        self.assertEqual(profile.cache, str(sd / "GAMMA" / "cache"))

    def test_unique_name(self):
        from Steamdeck.screens.setup import unique_name

        self.assertEqual(unique_name("GAMMA", []), "GAMMA")
        self.assertEqual(unique_name("GAMMA", ["GAMMA", "GAMMA 2"]), "GAMMA 3")

    def test_wizard_and_desktop_build_the_same_create_command(self):
        from commander_gui.ui.profiles_page import create_profile_args
        from Steamdeck.screens.setup import profile_for

        args = create_profile_args(profile_for(Path("/x/GAMMA"), "Deck"))
        self.assertEqual(args[0], "create")
        self.assertEqual(args[args.index("--name") + 1], "Deck")
        self.assertEqual(args[args.index("--gamma") + 1], "/x/GAMMA/GAMMA")


# =====================================================================
# H. The overhaul's window behaviour
# =====================================================================
class DeckOverhaulTests(DeckWindowFixture):
    def _press(self, key):
        from PySide6.QtTest import QTest

        QTest.keyClick(self.window, key)
        self.pump(2)

    def test_dpad_focus_never_leaves_the_visible_area(self):
        """The bug that made long screens unusable: focus moved below the
        fold and the page never scrolled after it."""
        from PySide6.QtCore import Qt

        from Steamdeck.focus import audit_focusables

        for key in SCREEN_KEYS:
            page = self.goto(key)
            focusables = audit_focusables(page)
            if not focusables:
                continue
            focusables[0].setFocus(Qt.FocusReason.OtherFocusReason)
            self.pump(2)
            for _ in range(len(focusables) + 2):
                self._press(Qt.Key.Key_Down)
                focused = self.app.focusWidget()
                if focused is None or not page.isAncestorOf(focused):
                    break  # reached the nav bar
                self.assertFalse(
                    focused.visibleRegion().isEmpty(),
                    f"{key}: {focused.objectName() or type(focused).__name__} is off screen",
                )

    def test_enter_clicks_a_focused_button(self):
        """Steam's desktop layout sends Enter for A; a plain QPushButton
        outside a dialog ignores Enter, so the controller must click it."""
        from PySide6.QtCore import Qt

        settings = self.goto("settings")
        clicked = []
        settings.plus_button.clicked.connect(lambda: clicked.append(True))
        settings.plus_button.setFocus(Qt.FocusReason.OtherFocusReason)
        with patch.object(settings, "_nudge_scale"):
            self._press(Qt.Key.Key_Return)
        self.assertEqual(clicked, [True])

    def test_gamepad_action_drives_focus_when_window_is_active(self):
        from Steamdeck import gamepad as pad

        play = self.goto("play")
        play.target_row.setFocus()
        self.pump()
        with patch.object(self.window, "isActiveWindow", return_value=True):
            self.window._focus.on_pad_action(pad.RIGHT)
        self.pump()
        self.assertIs(self.app.focusWidget(), play.runner_row)

    def test_gamepad_is_ignored_while_the_game_has_focus(self):
        from Steamdeck import gamepad as pad

        with (
            patch.object(self.window, "isActiveWindow", return_value=False),
            patch.object(self.window, "handle_back") as back,
        ):
            self.window._focus.on_pad_action(pad.BACK)
        back.assert_not_called()

    def test_key_duplicate_of_a_pad_press_is_dropped(self):
        from PySide6.QtCore import Qt

        from Steamdeck import gamepad as pad

        self.goto("settings")
        with (
            patch.object(self.window, "isActiveWindow", return_value=True),
            patch.object(self.window, "handle_back") as back,
        ):
            self.window._focus.on_pad_action(pad.BACK)
            self._press(Qt.Key.Key_Escape)
        self.assertEqual(back.call_count, 1)

    def test_shoulder_buttons_cycle_tabs_and_wrap(self):
        from Steamdeck import gamepad as pad

        self.goto(SCREEN_KEYS[-1])
        self.window.handle_action(pad.TAB_NEXT)
        self.pump()
        self.assertEqual(self.window.current_key(), SCREEN_KEYS[0])
        self.window.handle_action(pad.TAB_PREV)
        self.pump()
        self.assertEqual(self.window.current_key(), SCREEN_KEYS[-1])

    def test_picker_fires_once_per_tap(self):
        from Steamdeck.widgets import DeckPicker

        picker = DeckPicker("", [("One", 1), ("Two", 2)])
        chosen = []
        picker.chosen.connect(chosen.append)
        item = picker.list.item(1)
        picker.list.itemClicked.emit(item)
        picker.list.itemActivated.emit(item)
        self.assertEqual(chosen, [2])
        picker.deleteLater()

    def test_mods_toggle_once_and_keeps_shown_count(self):
        mods = self.goto("mods")
        mods._set_filter("enabled")
        self.pump()
        from PySide6.QtCore import Qt

        enabled_role = Qt.ItemDataRole.UserRole + 1
        item = mods.mod_items()[0]
        before = item.data(enabled_role)
        with patch("Steamdeck.modlist_io.mo2_running", return_value=False):
            mods.list.itemClicked.emit(item)
            mods.list.itemActivated.emit(item)
        self.assertNotEqual(item.data(enabled_role), before)
        self.assertIn("shown", mods.status.text())

    def test_mods_category_enable_all(self):
        mods = self.goto("mods")
        with patch("Steamdeck.modlist_io.mo2_running", return_value=False):
            # The fixture's mods sit after the only separator, which MO2's
            # bottom-up order makes their own "Uncategorized" group.
            mods._set_category("Uncategorized", True)
        self.assertIn("+Beta Mod", self.modlist.read_text(encoding="utf-8"))

    def test_on_screen_keyboard_types_into_the_search(self):
        from Steamdeck import gamepad as pad

        mods = self.goto("mods")
        keyboard = self.window.open_keyboard(mods.search)
        self.pump()
        for char in "alp":
            keyboard._type(char)
        keyboard.handle_action(pad.SEARCH)  # Y = backspace
        keyboard._type("p")
        self.assertEqual(mods.search.text(), "alp")
        keyboard.handle_action(pad.MENU)  # Start = done
        self.pump()
        self.assertIsNone(self.window.current_overlay())
        self.assertEqual(len(mods.mod_items()), 1)

    def test_keyboard_over_a_picker_returns_to_that_picker(self):
        from Steamdeck.widgets import DeckOverlay, DeckPicker

        options = [(f"GE-Proton9-{n}", n) for n in range(20)]
        picker = DeckPicker("", options, searchable=True)
        overlay = DeckOverlay("Pick", picker)
        self.window.show_overlay(overlay)
        self.pump()
        keyboard = self.window.open_keyboard(picker.search)
        self.pump()
        self.assertIsNot(self.window.current_overlay(), overlay)
        for char in "9-1":
            keyboard._type(char)
        keyboard.done()
        self.pump()
        # Back on the same, still-alive picker, filtered by what was typed.
        self.assertIs(self.window.current_overlay(), overlay)
        self.assertEqual(picker.list.count(), 11)  # 9-1 and 9-10..9-19
        self.window.handle_back()
        self.pump()
        self.assertIsNone(self.window.current_overlay())

    def test_status_row_detail_opens_in_an_overlay(self):
        from Steamdeck.widgets import DeckStatusRow

        row = DeckStatusRow("Wine", interactive=True, parent=self.window._deck_root)
        from PySide6.QtCore import Qt

        self.assertEqual(row.focusPolicy(), Qt.FocusPolicy.NoFocus)
        row.set_detail("Install wine from your package manager.")
        self.assertEqual(row.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        row.activated.emit()
        self.pump()
        self.assertIsNotNone(self.window.current_overlay())
        self.window.dismiss_overlay()

    def test_window_x_offers_desktop_or_exit_instead_of_closing(self):
        from unittest.mock import MagicMock

        event = MagicMock()
        event.spontaneous.return_value = True
        self.window.closeEvent(event)
        event.ignore.assert_called_once()
        overlay = self.window.current_overlay()
        self.assertIsNotNone(overlay)
        self.assertEqual(
            [b.text() for b in overlay.buttons],
            ["Switch to Desktop", "Exit COMMANDER", "Cancel"],
        )
        self.assertIs(overlay.default_button, overlay.buttons[0])

        with patch("Steamdeck.window.switch_mode") as switch:
            overlay.buttons[0].click()
        switch.assert_called_once_with(self.window, deck=False)
        self.assertTrue(self.window.isVisible())

    def test_own_close_calls_skip_the_x_question(self):
        """Quit COMMANDER and the mode switch call close() themselves."""
        self.window.close()
        self.pump()
        self.assertFalse(self.window.isVisible())

    def test_close_while_busy_asks_in_an_overlay_and_keeps_the_dpad(self):
        from PySide6.QtWidgets import QApplication

        self.window.install_busy = True
        try:
            self.window.close()
            self.pump()
            self.assertTrue(self.window.isVisible())
            self.assertIsNotNone(self.window.current_overlay())
            # Declining must leave the focus filter installed.
            self.window.dismiss_overlay()
            with patch.object(self.window, "handle_back") as back:
                from PySide6.QtCore import Qt

                self._press(Qt.Key.Key_Escape)
            back.assert_called_once()
            self.assertIsInstance(QApplication.instance(), QApplication)
        finally:
            self.window.install_busy = False

    def test_busy_holds_and_releases_the_sleep_inhibitor(self):
        with (
            patch.object(self.window._inhibitor, "hold", return_value=True) as hold,
            patch.object(self.window._inhibitor, "release") as release,
        ):
            self.window.set_install_busy(True, "gamma")
            self.assertFalse(self.window.awake_label.isHidden())
            self.window.set_install_busy(False)
        hold.assert_called_once()
        release.assert_called_once()
        self.assertTrue(self.window.awake_label.isHidden())

    def test_failed_install_is_resumable_and_success_clears_it(self):
        install = self.goto("install")
        with (
            patch("Steamdeck.screens.install.cli_command", return_value=["stub"]),
            patch("Steamdeck.screens.install.CommandRunner"),
        ):
            install._start_install()
        install._runner = None
        install._on_finished(1, "boom")
        DeckSettingsTests._reset_cache()
        state = gui_settings.load_gui_settings().get("gamma_install_resume")
        self.assertEqual(state.get("gamma"), str(self.gamma))
        install.refresh()
        self.assertEqual(install.install_button.text(), "Resume install")

        with (
            patch("Steamdeck.screens.install.cli_command", return_value=["stub"]),
            patch("Steamdeck.screens.install.CommandRunner"),
        ):
            install._start_install()
        install._runner = None
        install._on_finished(0, "")
        DeckSettingsTests._reset_cache()
        self.assertEqual(gui_settings.load_gui_settings().get("gamma_install_resume"), {})
        self.assertFalse(self.window.install_busy)

    def test_force_stop_kills_the_launch_and_records_playtime(self):
        import time

        play = self.goto("play")
        controller = play.controller
        controller._active = True
        controller._game_seen = True
        controller._game_started_at = time.time() - 60
        controller._profile_name = "Deck"
        with patch.object(controller._registry, "cleanup_all") as cleanup, patch.object(
            controller, "_kill_stray_debuggers"
        ):
            controller.force_stop()
        cleanup.assert_called_once()
        self.assertFalse(controller.is_active())
        DeckSettingsTests._reset_cache()
        playtime = gui_settings.load_gui_settings()["playtime_seconds"]["Deck"]
        self.assertGreaterEqual(playtime, 59)

    def test_setup_screen_creates_a_profile(self):
        from Steamdeck.window import SETUP_KEY

        self.window.set_page(SETUP_KEY)
        self.pump()
        setup = self.window._pages[SETUP_KEY]
        root = self.tmp / "sd"
        setup._set_root(root)
        self.pump()
        setup.name_edit.setText("Handheld")
        with patch("Steamdeck.screens.setup.BackgroundTask") as task_cls:
            setup._create()
        args = task_cls.call_args[0][1]
        self.assertEqual(args[args.index("--name") + 1], "Handheld")
        self.assertTrue((root / "GAMMA").is_dir())


# =====================================================================
# I. Polish pass 2
# =====================================================================
class DeckScaleTests(unittest.TestCase):
    def tearDown(self):
        from Steamdeck.scale import set_scale

        set_scale(1.0)

    def test_scale_for_window_sizes(self):
        from Steamdeck.scale import MAX_SCALE, scale_for

        self.assertEqual(scale_for(1280, 800), 1.0)
        self.assertEqual(scale_for(1024, 640), 1.0)  # never shrinks
        self.assertEqual(scale_for(1920, 1080), 1.35)  # height-limited
        self.assertEqual(scale_for(7680, 4320), MAX_SCALE)

    def test_px_and_stylesheet_follow_the_scale(self):
        from Steamdeck.deck_theme import build_deck_stylesheet
        from Steamdeck.scale import px, set_scale

        set_scale(1.5)
        self.assertEqual(px(64), 96)
        sheet = build_deck_stylesheet("gamma")
        self.assertIn("min-height: 96px", sheet)  # 64px button rule
        self.assertNotIn("min-height: 64px;", sheet)


class DeckPolishTests(DeckWindowFixture):
    def test_rescale_rebuilds_but_waits_for_a_running_install(self):
        from Steamdeck.scale import scale

        self.window.set_install_busy(True, "gamma")
        with patch.object(self.window, "width", return_value=1920), patch.object(
            self.window, "height", return_value=1080
        ):
            self.window._apply_window_scale()
            self.assertEqual(scale(), 1.0)
            self.assertTrue(self.window._rescale_pending)
            with patch.object(self.window, "rebuild") as rebuild:
                self.window.set_install_busy(False)
                self.window._apply_window_scale()
            rebuild.assert_called_once()
        from Steamdeck.scale import set_scale

        set_scale(1.0)

    def test_picker_shows_short_lists_without_scrolling(self):
        from Steamdeck.scale import px
        from Steamdeck.widgets import PICKER_ROWS, ROW_H, DeckPicker

        short = DeckPicker("", [("A", 1), ("B", 2), ("C", 3)])
        self.assertGreaterEqual(short.list.minimumHeight(), 3 * px(ROW_H))
        long = DeckPicker("", [(str(n), n) for n in range(40)])
        self.assertLess(long.list.minimumHeight(), (PICKER_ROWS + 1) * px(ROW_H))
        short.deleteLater()
        long.deleteLater()

    def test_confirm_puts_action_left_cancel_right_equal_size_cancel_focused(self):
        self.window.confirm("T", "M", lambda: None, confirm_text="Install")
        self.pump()
        overlay = self.window.current_overlay()
        first, second = overlay.buttons
        self.assertEqual((first.text(), second.text()), ("Install", "Cancel"))
        self.assertEqual(first.height(), second.height())
        self.assertIs(self.app.focusWidget(), second)
        self.window.dismiss_overlay()

    def test_switch_animation_ends_at_the_new_state(self):
        import time

        install = self.goto("install")
        row = install.minimal_row
        row.set_checked(True, notify=True)
        deadline = time.monotonic() + 1
        while row._position < 1.0 and time.monotonic() < deadline:
            self.pump(1)
            time.sleep(0.01)
        self.assertEqual(row._position, 1.0)
        self.assertTrue(row.is_checked())

    def test_status_cards_are_not_selectable(self):
        from PySide6.QtCore import Qt

        dashboard = self.goto("dashboard")
        install = self.goto("install")
        for row in (
            dashboard.anomaly_row,
            dashboard.gamma_row,
            dashboard.deps_row,
            dashboard.storage_row,
            install.anomaly_row,
            install.gamma_row,
            install.deps_row,
        ):
            self.assertEqual(row.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_install_step_buttons_match(self):
        install = self.goto("install")
        self.assertEqual(install.anomaly_button.height(), install.install_button.height())
        self.assertEqual(install.anomaly_row.y(), install.gamma_row.y())

    def test_update_lists_the_changed_mods(self):
        from commander_gui.parsers import UpdateDiff
        from commander_gui.updates import UpdateStatus

        update = self.goto("update")
        update._task = None
        update._on_checked(
            UpdateStatus(
                installed="1",
                latest="2",
                diffs=[UpdateDiff("Added", "New Mod"), UpdateDiff("Removed", "Old Mod")],
            )
        )
        self.assertFalse(update.changes.isHidden())
        self.assertIn("New Mod", update.changes.text())
        self.assertIn("Old Mod", update.changes.text())
        self.assertIn("1 added", update.changes_summary.text())

    def test_profile_switch_asks_while_the_game_runs(self):
        panel = self.goto("dashboard").profile_panel
        with (
            patch("Steamdeck.profiles.game_running", return_value=True),
            patch.object(panel, "_switch") as switch,
        ):
            panel._confirm_switch("Other")
            self.pump()
        switch.assert_not_called()
        overlay = self.window.current_overlay()
        self.assertIsNotNone(overlay)
        self.window.dismiss_overlay()

    def test_profile_editor_changes_folders_and_saves(self):
        profile_screen = self.goto("dashboard").profile_panel
        profile_screen.open_editor("Deck")
        self.pump()
        editor = profile_screen._editor
        new_gamma = self.tmp / "elsewhere" / "gamma"
        editor.set_path("gamma", str(new_gamma))
        editor.name_edit.setText("Handheld")
        with patch("Steamdeck.profiles.mo2_running", return_value=False):
            profile_screen._save()
        self.pump()
        saved = self.window.settings.active_profile
        self.assertEqual(saved.profile_name, "Handheld")
        self.assertEqual(saved.gamma, str(new_gamma))
        self.assertEqual(saved.anomaly, str(self.anomaly))
        self.assertIsNone(self.window.current_overlay())

    def test_folder_picker_new_folder_and_stacking(self):
        from Steamdeck.folder_picker import DeckFolderPicker, show_folder_picker
        from Steamdeck.widgets import DeckOverlay, deck_label

        base = DeckOverlay("Base", deck_label("editor"))
        self.window.show_overlay(base)
        chosen = []
        show_folder_picker(
            self.window, title="Pick", start=self.tmp, on_choose=chosen.append, stacked=True
        )
        self.pump()
        body = self.window.current_overlay().findChild(DeckFolderPicker)
        made = body.create_folder("New Install")
        self.assertTrue(made.is_dir())
        self.assertIsNone(body.create_folder("../escape"))
        self.window.current_overlay().buttons[0].click()  # Use this folder
        self.pump()
        self.assertEqual(chosen, [made])
        self.assertIs(self.window.current_overlay(), base)
        self.window.dismiss_overlay()

    def test_text_size_presses_restyle_once(self):
        settings = self.goto("settings")
        with patch.object(self.window, "apply_style") as apply_style:
            for _ in range(5):
                settings._nudge_scale(10)
            self.assertEqual(apply_style.call_count, 0)
            self.assertEqual(settings.scale_row.value_label.text(), "150%")
            self.window._restyle_timer.timeout.emit()
        self.assertEqual(apply_style.call_count, 1)

    def test_startup_choice_is_inline_and_saves(self):
        settings = self.goto("settings")
        # Two choices, no "Ask"; unsettled shows what startup does here.
        self.assertIsNone(settings.startup_choice.button("ask"))
        self.assertEqual(settings.startup_choice.button("always").text(), "Steam Deck")
        self.assertEqual(settings.startup_choice.button("never").text(), "Desktop")
        with patch("Steamdeck.screens.settings.steam_deck_model", return_value=None):
            settings.refresh()
        self.assertEqual(settings.startup_choice.value(), "never")
        settings.startup_choice.button("always").click()
        DeckSettingsTests._reset_cache()
        self.assertEqual(gui_settings.load_gui_settings()["deck_mode_preference"], "always")

    def test_screen_shortcuts(self):
        from Steamdeck import gamepad as pad

        play = self.goto("play")
        with patch.object(play, "play") as launch:
            self.window.handle_action(pad.CONTEXT)
        launch.assert_called_once()

        mods = self.goto("mods")
        with patch.object(mods, "open_search") as search:
            self.window.handle_action(pad.SEARCH)
        search.assert_called_once()

        dashboard = self.goto("dashboard")
        with patch.object(dashboard, "_banner_action") as banner:
            self.window.handle_action(pad.CONTEXT)
        banner.assert_called_once()

        # Profiles moved onto the Dashboard: no Profile tab any more.
        self.assertNotIn("profile", SCREEN_KEYS)

    def test_quick_menu_play(self):
        from Steamdeck import gamepad as pad

        self.goto("settings")
        self.window.handle_action(pad.MENU)
        self.pump()
        overlay = self.window.current_overlay()
        self.assertIs(overlay.default_button, overlay.buttons[-1])  # Back
        play = self.window._ensure_page("play")
        with patch.object(play, "play") as launch:
            overlay.buttons[0].click()
            self.pump()
        launch.assert_called_once()
        self.assertEqual(self.window.current_key(), "play")

    def test_footer_carries_profile_and_clock(self):
        self.assertEqual(self.window.profile_label.text(), "Deck")
        footer = self.window.findChild(type(self.window.hint_bar).__mro__[1], "deckFooter")
        self.assertTrue(footer.isAncestorOf(self.window.clock_label))
        self.assertTrue(footer.isAncestorOf(self.window.profile_label))


if __name__ == "__main__":
    unittest.main()


class DeckRound3Tests(DeckWindowFixture):
    def test_mods_list_has_category_headers_in_mo2_order(self):
        mods = self.goto("mods")
        first = mods.list.item(0)
        from Steamdeck.screens.mods import _KIND, _PRIORITY

        self.assertEqual(first.data(_KIND), "header")
        names = [item.text() for item in mods.mod_items()]
        # modlist.txt is written bottom-up; MO2 shows it reversed.
        self.assertEqual(names, ["Gamma Mod", "Beta Mod", "Alpha Mod"])
        self.assertEqual([i.data(_PRIORITY) for i in mods.mod_items()], [1, 2, 3])

    def test_header_collapses_and_expands_its_category(self):
        mods = self.goto("mods")
        header = mods.list.item(0)
        mods.list.itemClicked.emit(header)
        self.assertEqual(mods.mod_items(), [])
        mods.list.itemClicked.emit(mods.list.item(0))
        self.assertEqual(len(mods.mod_items()), 3)
        mods._toggle_collapse_all()
        self.assertEqual(mods.mod_items(), [])
        self.assertEqual(mods.collapse_button.text(), "Expand all")

    def test_dashboard_holds_the_profiles(self):
        dashboard = self.goto("dashboard")
        self.pump()
        panel = dashboard.profile_panel
        self.assertIn("Deck", panel.chooser.value_label.text())
        # A dropdown: the profiles open in a picker, not stacked on the page.
        panel.choose_profile()
        self.pump()
        from Steamdeck.widgets import DeckPicker

        picker = self.window.current_overlay().findChild(DeckPicker)
        self.assertEqual(picker.list.count(), 1)
        with patch.object(panel, "_confirm_switch") as switch:
            picker.list.itemClicked.emit(picker.list.item(0))  # the active one
        switch.assert_not_called()
        self.assertIsNone(self.window.current_overlay())

    def test_updates_is_one_card(self):
        dashboard = self.goto("dashboard")
        self.assertFalse(hasattr(dashboard, "updates_card"))
        self.assertTrue(dashboard.update_row.isAncestorOf(dashboard.updates_status_label))

    def test_system_rows_offer_a_copy_command_button(self):
        from PySide6.QtGui import QGuiApplication

        system = self.goto("system")
        system._render(
            (
                [{"label": "Wine", "state": "missing", "detail": "", "command": "sudo pacman -S wine"}],
                False,
                {},
            )
        )
        from Steamdeck.widgets import DeckStatusRow

        rows = [r for r in system.findChildren(DeckStatusRow) if hasattr(r, "copy_button")]
        self.assertEqual(len(rows), 1)
        rows[0].copy_button.click()
        self.assertEqual(QGuiApplication.clipboard().text(), "sudo pacman -S wine")
        self.assertEqual(rows[0].copy_button.text(), "Copied!")
        from PySide6.QtCore import Qt

        self.assertEqual(rows[0].focusPolicy(), Qt.FocusPolicy.NoFocus)


class DeckRound4Tests(DeckWindowFixture):
    def test_system_checks_are_grouped_like_desktop(self):
        from PySide6.QtWidgets import QLabel

        system = self.goto("system")
        system._render(
            (
                [
                    {"label": "Wine", "state": "ready", "detail": ""},
                    {"label": "GameMode", "state": "optional", "detail": ""},
                    {"label": "Proton Builds", "state": "ready", "builds": [("GE-Proton10-1", "ready", "")]},
                    {"label": "Brand new check", "state": "ready", "detail": ""},
                ],
                True,
                {},
            )
        )
        headings = [
            w.text() for w in system.findChildren(QLabel) if w.objectName() == "deckSection"
        ]
        self.assertEqual(
            headings, ["Required Tools", "Proton Builds", "Optional Enhancements", "Other"]
        )
        from Steamdeck.widgets import DeckStatusRow

        titles = [r.title_label.text() for r in system.findChildren(DeckStatusRow)]
        self.assertIn("GE-Proton10-1", titles)

    def test_profile_switcher_carries_edit_and_new(self):
        dashboard = self.goto("dashboard")
        panel = dashboard.profile_panel
        self.assertFalse(hasattr(panel, "edit_button"))
        panel.choose_profile()
        self.pump()
        overlay = self.window.current_overlay()
        self.assertEqual(
            [b.text() for b in overlay.header_buttons], ["Edit", "+  New"]
        )
        overlay.header_buttons[0].click()
        self.pump()
        from PySide6.QtWidgets import QLineEdit

        self.assertEqual(self.window.current_overlay().findChild(QLineEdit).text(), "Deck")

    def test_dashboard_play_stays_on_the_dashboard(self):
        dashboard = self.goto("dashboard")
        dashboard._banner_target = "play"
        play = self.window._ensure_page("play")
        with patch.object(play, "play") as start:
            dashboard._banner_action()
        start.assert_called_once()
        self.assertEqual(self.window.current_key(), "dashboard")

    def test_hero_turns_into_quit_game_while_running(self):
        play = self.goto("play")
        self.assertEqual(
            play.mo2_button.height(), play.anomaly_button.height()
        )
        with patch.object(play.controller, "is_active", return_value=True):
            play._on_state_changed(True)
            self.assertIn("Quit Game", play.hero.text())
            self.assertEqual(play.hero.objectName(), "deckHeroDanger")
            self.assertTrue(play.hero.isEnabled())
            with patch.object(play, "_confirm_stop") as stop:
                play.hero.click()
            stop.assert_called_once()
        play._on_state_changed(False)
        self.assertIn("Play GAMMA", play.hero.text())

    def test_open_mo2_is_labelled_close_mo2_not_quit_game(self):
        play = self.goto("play")
        with (
            patch.object(play.controller, "is_active", return_value=True),
            patch.object(play.controller, "mo2_only", return_value=True),
        ):
            play._on_state_changed(True)
            self.assertIn("Close MO2", play.hero.text())
            self.assertNotIn("Quit Game", play.hero.text())
            self.assertIn(("X", "Close MO2"), play.hints())
            play._on_status("Running")
            self.assertEqual(play.status_caption.text(), "Mod Organizer is running")
        play._on_state_changed(False)

    def test_mo2_only_follows_the_launch_kind(self):
        from types import SimpleNamespace

        play = self.goto("play")
        controller = play.controller
        profile = SimpleNamespace(gamma="", anomaly="", mo2_profile="", profile_name="Deck")
        with (
            patch("Steamdeck.launch.resolve_runner", side_effect=OSError("stop")),
        ):
            controller.launch(profile, target=None)
        # Failed launch leaves nothing active.
        self.assertFalse(controller.mo2_only())
        controller._mo2_only = True
        controller._active = True
        self.assertTrue(controller.mo2_only())
        controller._active = False

    def test_welcome_overlay_and_dont_show_again(self):
        gui_settings.save_gui_settings(welcome_hidden=False)
        with patch("Steamdeck.welcome.fetch_latest_release_notes", return_value=("COMMANDER 9.9", "<p>Notes</p>")):
            self.window.show_welcome()
            self.pump(20)
        overlay = self.window.current_overlay()
        self.assertEqual(overlay.panel.objectName(), "deckOverlayPanelGlass")
        from Steamdeck.welcome import WelcomePanel

        panel = overlay.findChild(WelcomePanel)
        self.assertEqual(panel.latest_label.text(), "9.9")
        self.assertIn("Update available", panel.version_status.text())
        self.assertFalse(panel.hide_check.isChecked())
        panel.hide_check.click()
        self.assertTrue(gui_settings.load_gui_settings()["welcome_hidden"])
        overlay.header_buttons[0].click()
        self.pump()
        self.assertIsNone(self.window.current_overlay())


class DeckGameStatsTests(unittest.TestCase):
    @staticmethod
    def _scoc(values):
        import struct

        def key(name):
            raw = name.encode()
            return b"\x04" + struct.pack("<I", len(raw)) + raw

        data = b"junk" + key("actor_statistics") + b"\x05\x02" + struct.pack("<I", 0)
        for name, value in values:
            data += key(name) + b"\x03" + struct.pack("<d", value)
        data += key("actor_articles") + b"\x05\x02\x00\x00\x00\x00"
        data += key("actor_achievements") + b"\x05\x02" + struct.pack("<I", 0)
        data += key("tourist") + b"\x01\x01" + key("geologist") + b"\x01\x00"
        return data

    def test_reads_counters_from_the_newest_save(self):
        from commander_gui.game_stats import latest_save_stats, read_actor_statistics

        with tempfile.TemporaryDirectory() as tmp:
            saves = Path(tmp) / "appdata" / "savedgames"
            saves.mkdir(parents=True)
            old = saves / "old.scoc"
            old.write_bytes(self._scoc([("deaths", 9.0)]))
            os.utime(old, (1, 1))
            (saves / "new.scoc").write_bytes(
                self._scoc([("deaths", 2.0), ("killed_monsters", 41.0), ("killed_stalkers", 7.0)])
            )
            stats = latest_save_stats(tmp)
            self.assertEqual(stats.save_name, "new")
            self.assertEqual(stats.get("killed_monsters") + stats.get("killed_stalkers"), 48)
            self.assertEqual(stats.get("deaths"), 2)
            self.assertEqual(stats.achievements, {"tourist": True, "geologist": False})
            (saves / "bad.scoc").write_bytes(b"\x04\x10\x00")
            self.assertIsNone(read_actor_statistics(saves / "bad.scoc"))
            self.assertIsNone(latest_save_stats(str(Path(tmp) / "missing")))

    def test_finds_mo2_local_and_overwrite_saves(self):
        from commander_gui.game_stats import latest_save_stats

        with tempfile.TemporaryDirectory() as tmp:
            anomaly = Path(tmp) / "anomaly" / "appdata" / "savedgames"
            local = Path(tmp) / "gamma" / "profiles" / "G.A.M.M.A" / "saves"
            overwrite = Path(tmp) / "gamma" / "overwrite" / "appdata" / "savedgames"
            for folder in (anomaly, local, overwrite):
                folder.mkdir(parents=True)
            (anomaly / "a.scoc").write_bytes(self._scoc([("deaths", 1.0)]))
            os.utime(anomaly / "a.scoc", (10, 10))
            (overwrite / "o.scoc").write_bytes(self._scoc([("deaths", 2.0)]))
            os.utime(overwrite / "o.scoc", (20, 20))
            (local / "l.scoc").write_bytes(self._scoc([("deaths", 3.0)]))
            stats = latest_save_stats(
                str(Path(tmp) / "anomaly"), str(Path(tmp) / "gamma"), "G.A.M.M.A"
            )
            self.assertEqual((stats.save_name, stats.get("deaths")), ("l", 3))
            os.utime(local / "l.scoc", (5, 5))
            stats = latest_save_stats(
                str(Path(tmp) / "anomaly"), str(Path(tmp) / "gamma"), "G.A.M.M.A"
            )
            self.assertEqual(stats.save_name, "o")


class DeckAchievementTests(DeckWindowFixture):
    def _save(self):
        from commander_gui.game_stats import SaveStats

        return SaveStats(
            "quick",
            0.0,
            {"boxes_smashed": 143.0, "stashes_found": 250.0},
            {"tourist": True, "geologist": False},
        )

    def test_catalogue_covers_every_icon(self):
        from commander_gui.achievements import ACHIEVEMENTS, progress
        from commander_gui.ui.brand_icons import achievement_icon

        self.assertEqual(len(ACHIEVEMENTS), 24)
        self.assertEqual(len({a.key for a in ACHIEVEMENTS}), 24)
        for achievement in ACHIEVEMENTS:
            self.assertFalse(achievement_icon(achievement.key, "#ffffff").isNull(), achievement.key)
        boxes = next(a for a in ACHIEVEMENTS if a.key == "infantile_pleasure")
        stashes = next(a for a in ACHIEVEMENTS if a.key == "rag_and_bone")
        self.assertEqual(progress(self._save(), boxes), (143, 200))
        self.assertEqual(progress(self._save(), stashes), (100, 100))

    def test_window_lists_every_achievement(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QLabel

        play = self.goto("play")
        play._show_stats(self._save())
        self.assertIn("1 / 24", play.achievements_button.text())
        play.achievements_button.click()
        self.pump()
        overlay = self.window.current_overlay()
        self.assertEqual(len(overlay.rows), 24)
        tourist = next(r for r in overlay.rows if r.achievement.key == "tourist")
        boxes = next(r for r in overlay.rows if r.achievement.key == "infantile_pleasure")
        self.assertTrue(tourist.is_unlocked)
        self.assertEqual(tourist.value_label.text(), "Unlocked")
        self.assertEqual(boxes.value_label.text(), "143 / 200")
        self.assertEqual(boxes.title_label.objectName(), "deckRowTitleDim")
        # The D-pad walks the list: Down from the first row lands on the next.
        overlay.rows[0].setFocus(Qt.FocusReason.OtherFocusReason)
        self.pump(1)
        QTest.keyClick(self.window, Qt.Key.Key_Down)
        self.pump(1)
        self.assertIs(self.app.focusWidget(), overlay.rows[1])
        boxes.activated.emit()
        self.pump()
        self.assertEqual(self.window.current_overlay().findChild(QLabel, "deckOverlayTitle").text(), "Infantile Pleasure")
        self.window.dismiss_overlay()
        self.pump()
        self.assertIs(self.window.current_overlay(), overlay)

    def test_list_glides_to_the_focused_row(self):
        import time

        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        from Steamdeck.widgets import DeckSmoothScrollArea

        glide = patch.object(DeckSmoothScrollArea, "GLIDE_MS", 220)
        glide.start()
        self.addCleanup(glide.stop)
        play = self.goto("play")
        play._show_stats(self._save())
        play.achievements_button.click()
        self.pump()
        overlay = self.window.current_overlay()
        scroll = overlay.findChild(DeckSmoothScrollArea)
        bar = scroll.verticalScrollBar()
        overlay.rows[0].setFocus(Qt.FocusReason.OtherFocusReason)
        self.pump(1)
        for _ in range(10):
            QTest.keyClick(self.window, Qt.Key.Key_Down)
            self.pump(1)
        # Mid-glide: the animation is still running towards the target.
        self.assertEqual(scroll._glide.state(), scroll._glide.State.Running)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            self.pump(1)
        row = self.app.focusWidget()
        self.assertIs(row, overlay.rows[10])
        top = row.mapTo(scroll.widget(), row.rect().topLeft()).y() - bar.value()
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(top + row.height(), scroll.viewport().height())

    def test_no_save_disables_the_button(self):
        play = self.goto("play")
        play._show_stats(None)
        self.assertFalse(play.achievements_button.isEnabled())


class DeckControllerMapTests(DeckWindowFixture):
    def test_view_button_opens_the_controller_layout(self):
        from Steamdeck import gamepad as pad
        from Steamdeck.controller_map import ControllerDiagram

        self.goto("play")
        self.window.handle_action(pad.HELP)
        self.pump()
        diagram = self.window.current_overlay().findChild(ControllerDiagram)
        self.assertIsNotNone(diagram)
        # The icon is drawn, not loaded: three blades, one per 60-degree slot.
        from PySide6.QtCore import QPointF

        from Steamdeck.controller_map import trefoil_path

        blades = trefoil_path(QPointF(0, 0), 20, 7)
        self.assertTrue(blades.contains(QPointF(0, 14)))  # down
        self.assertFalse(blades.contains(QPointF(0, -14)))  # gap at the top
        # Paints at a small and a large size without error.
        for size in ((600, 220), (1400, 520)):
            diagram.resize(*size)
            self.assertFalse(diagram.grab().isNull())
        self.window.dismiss_overlay()

    def test_footer_offers_controls_on_every_screen_and_tapping_opens_it(self):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QLabel

        from Steamdeck.controller_map import ControllerDiagram

        for key in ("dashboard", "play", "settings"):
            self.goto(key)
            self.assertIn(("\u29c9", "Controls"), self.window.hint_bar.hints())
        pill = next(
            w for w in self.window.hint_bar.findChildren(QLabel) if w.text() == "\u29c9"
        )
        release = QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPointF(2, 2),
            QPointF(2, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.app.sendEvent(pill, release)
        self.pump()
        self.assertIsNotNone(self.window.current_overlay().findChild(ControllerDiagram))
        self.window.dismiss_overlay()

    def test_settings_no_longer_has_a_controls_row(self):
        settings = self.goto("settings")
        self.assertFalse(hasattr(settings, "controls_row"))


class DeckVersionTests(unittest.TestCase):
    def test_deck_mode_shares_commanders_version(self):
        import commander_gui
        import Steamdeck

        self.assertEqual(Steamdeck.__version__, commander_gui.__version__)
        self.assertEqual(Steamdeck.__version_label__, commander_gui.__version_label__)
        self.assertEqual(commander_gui.__version_label__, "1.3.1")


class DeckAuditTests(DeckWindowFixture):
    def test_rebuild_with_a_task_still_running_does_not_abort(self):
        """Regression test: deleting a screen whose BackgroundTask was still
        running destroyed a live QThread - Qt aborts the process for that.
        rebuild() now cancels and detaches running tasks first."""
        import threading

        from commander_gui.ui.common import BackgroundTask

        release = threading.Event()
        dashboard = self.goto("dashboard")
        task = BackgroundTask(release.wait, 5, parent=dashboard)
        task.start()
        self.pump(2)
        self.window.rebuild()
        self.pump(5)
        release.set()
        self.pump(5)
        self.assertTrue(self.window.isVisible())

    def test_rescale_waits_for_a_game_session(self):
        from Steamdeck.scale import scale, set_scale

        play = self.goto("play")
        play.controller._active = True
        try:
            with patch.object(self.window, "width", return_value=1920), patch.object(
                self.window, "height", return_value=1080
            ):
                self.window._apply_window_scale()
            self.assertEqual(scale(), 1.0)
            self.assertTrue(self.window._rescale_pending)
        finally:
            play.controller._active = False
            set_scale(1.0)
            self.window._rescale_pending = False

    def test_failed_reinstall_start_is_reported_so_the_lock_is_released(self):
        install = self.goto("install")
        with patch("Steamdeck.screens.install.Path.mkdir", side_effect=OSError("read-only")):
            self.assertFalse(install.start_auto_install())

    def test_resume_state_write_failure_still_releases_the_lock(self):
        install = self.goto("install")
        with (
            patch("Steamdeck.screens.install.cli_command", return_value=["stub"]),
            patch("Steamdeck.screens.install.CommandRunner"),
        ):
            install._start_install()
        install._runner = None
        with patch(
            "Steamdeck.screens.install.gui_settings.save_gui_settings",
            side_effect=OSError("No space left on device"),
        ):
            install._on_finished(1, "boom")
        self.assertFalse(self.window.install_busy)

    def test_profile_switch_failure_uses_a_toast_not_a_dialog(self):
        from commander_gui.ui import common

        with (
            patch.object(common.QMessageBox, "warning") as dialog,
            patch.object(self.window, "notify") as notify,
            patch("commander_gui.settings.run_config_command", return_value=(1, "", "nope")),
        ):
            task = common.activate_profile(self.window, self.window, "Deck")
            import time

            deadline = time.monotonic() + 3
            while not notify.called and time.monotonic() < deadline:
                self.pump(1)
                time.sleep(0.01)
            task.shutdown(1000)
        dialog.assert_not_called()
        notify.assert_called()

    def test_only_one_profile_switch_at_a_time(self):
        panel = self.goto("dashboard").profile_panel
        panel._task = object()
        with patch.object(panel, "_switch") as switch:
            panel._confirm_switch("Other")
        switch.assert_not_called()
        panel._task = None


class DeckCrashDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _controller(self, tmp):
        from Steamdeck.launch import DeckLaunchController

        controller = DeckLaunchController()
        self.addCleanup(controller.shutdown)
        controller._anomaly = tmp
        controller._profile_name = "Mine"
        controller._crash_baseline = {"old.mdmp"}
        seen = []
        controller.crashed.connect(lambda: seen.append(True))
        return controller, seen

    def test_new_dump_after_a_session_reports_a_crash(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            controller, seen = self._controller(tmp)
            with (
                patch.object(controller, "_record_playtime"),
                patch("Steamdeck.launch.crash_dump_names", return_value={"old.mdmp", "new.mdmp"}),
            ):
                controller._finish_session(120.0)
            self.assertEqual(seen, [True])
            self.assertFalse(controller._crash_timer.isActive())

    def test_no_new_dump_keeps_polling_then_stays_quiet(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            controller, seen = self._controller(tmp)
            with (
                patch.object(controller, "_record_playtime"),
                patch("Steamdeck.launch.crash_dump_names", return_value={"old.mdmp"}),
            ):
                controller._finish_session(120.0)
                self.assertTrue(controller._crash_timer.isActive())
                controller._crash_attempts_left = 1
                controller._crash_timer.stop()
                controller._poll_crash()
            self.assertEqual(seen, [])
            self.assertFalse(controller._crash_timer.isActive())

    def test_force_stop_never_reports_a_crash(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            controller, seen = self._controller(tmp)
            controller._active = True
            with (
                patch.object(controller._registry, "cleanup_all"),
                patch.object(controller, "_kill_stray_debuggers"),
                patch("Steamdeck.launch.crash_dump_names", return_value={"old.mdmp", "new.mdmp"}),
            ):
                controller.force_stop()
            self.assertEqual(seen, [])
            self.assertFalse(controller._crash_timer.isActive())


# =====================================================================
# Controller-first navigation: focus memory, rescue, B levels, stick
# =====================================================================
_TWO_STEP_FOMOD = """<config>
  <moduleName>Two Steps</moduleName>
  <installSteps order="Explicit">
    <installStep name="First">
      <optionalFileGroups order="Explicit">
        <group name="Colour" type="SelectAny">
          <plugins order="Explicit">
            <plugin name="Red"><files><folder source="red" destination="" /></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor></plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Second">
      <optionalFileGroups order="Explicit">
        <group name="Size" type="SelectAny">
          <plugins order="Explicit">
            <plugin name="Big"><files><folder source="big" destination="" /></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor></plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
</config>
"""


class DeckControllerNavigationTests(DeckWindowFixture):
    def focused(self):
        return self.app.focusWidget()

    def press(self, key, times: int = 1):
        from PySide6.QtTest import QTest

        for _ in range(times):
            QTest.keyClick(self.window, key)
            self.pump(2)

    def walk(self, scope, start):
        """Every widget reachable from ``start`` with the four arrows."""
        from PySide6.QtCore import Qt

        arrows = (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Left, Qt.Key.Key_Right)
        reached, frontier = {start}, [start]
        while frontier:
            widget = frontier.pop()
            for arrow in arrows:
                widget.setFocus(Qt.FocusReason.OtherFocusReason)
                self.pump(1)
                self.press(arrow)
                landed = self.focused()
                if landed is not None and scope.isAncestorOf(landed) and landed not in reached:
                    reached.add(landed)
                    frontier.append(landed)
        return reached

    # -- where focus lands ------------------------------------------------
    def test_pickers_open_on_their_list(self):
        from PySide6.QtWidgets import QListWidget

        settings = self.goto("settings")
        for row in (settings.theme_row, settings.language_row, settings.runner_row):
            row.setFocus()
            row.activated.emit()
            self.pump()
            self.assertIsInstance(self.focused(), QListWidget, row.title_label.text())
            self.window.handle_back()
            self.pump()

    def test_closing_an_overlay_returns_focus_to_its_opener(self):
        play = self.goto("play")
        play.runner_row.setFocus()
        self.pump()
        play.runner_row.activated.emit()
        self.pump()
        self.assertIsNotNone(self.window.current_overlay())
        self.window.handle_back()
        self.pump()
        self.assertIs(self.focused(), play.runner_row)

    def test_each_tab_remembers_where_focus_was(self):
        settings = self.goto("settings")
        settings.language_row.setFocus()
        self.pump()
        self.window.cycle_page(1)
        self.pump()
        self.window.cycle_page(-1)
        self.pump()
        self.assertIs(self.window.current_page(), settings)
        self.assertIs(self.focused(), settings.language_row)

    def test_play_starts_on_the_play_button(self):
        play = self.goto("play")
        self.window._nav_buttons["dashboard"].setFocus()
        self.window.cycle_page(1)  # dashboard -> play, first visit
        self.pump()
        if play.hero.isEnabled():
            self.assertIs(self.focused(), play.hero)

    def test_focus_is_rescued_when_the_focused_control_hides(self):
        settings = self.goto("settings")
        settings.chime_row.setFocus()
        self.pump()
        settings.chime_row.hide()
        self.pump()
        landed = self.focused()
        self.assertIsNotNone(landed)
        self.assertTrue(landed.isVisible())
        self.assertTrue(settings.isAncestorOf(landed))
        settings.chime_row.show()

    def test_focus_is_rescued_when_the_focused_control_disables(self):
        settings = self.goto("settings")
        settings.language_row.setFocus()
        self.pump()
        settings.language_row.setEnabled(False)
        self.pump()
        landed = self.focused()
        self.assertIsNotNone(landed)
        self.assertTrue(landed.isVisible() and landed.isEnabled())
        self.assertTrue(settings.isAncestorOf(landed))
        self.assertNotIn(landed, self.window._nav_buttons.values())
        settings.language_row.setEnabled(True)

    # -- lists and the nav bar -----------------------------------------------
    def test_left_right_leave_a_list_with_a_neighbour_and_page_without(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QHBoxLayout, QWidget

        from Steamdeck.widgets import DeckOverlay, DeckPicker, deck_button

        body = QWidget()
        row = QHBoxLayout(body)
        picker = DeckPicker("", [(str(n), n) for n in range(30)])
        side = deck_button("Side")
        row.addWidget(picker, 1)
        row.addWidget(side)
        self.window.show_overlay(DeckOverlay("Beside", body))
        self.pump()
        picker.list.setFocus()
        picker.list.setCurrentRow(3)
        self.press(Qt.Key.Key_Right)
        self.assertIs(self.focused(), side)
        self.window.dismiss_overlay()
        self.pump()

        alone = DeckPicker("", [(str(n), n) for n in range(30)])
        self.window.show_overlay(DeckOverlay("Alone", alone))
        self.pump()
        alone.list.setFocus()
        alone.list.setCurrentRow(0)
        self.press(Qt.Key.Key_Right)
        self.assertIs(self.focused(), alone.list)
        self.assertGreater(alone.list.currentRow(), 0)
        self.window.dismiss_overlay()

    def test_b_in_the_keyboard_finishes_like_done(self):
        mods = self.goto("mods")
        done = []
        keyboard = self.window.open_keyboard(mods.search, on_done=done.append)
        self.pump()
        keyboard._type("x")
        self.window.handle_back()
        self.pump()
        self.assertIsNone(self.window.current_overlay())
        self.assertEqual(done, ["x"])
        mods.search.clear()

    def test_b_in_the_folder_picker_goes_up_then_closes(self):
        from Steamdeck.folder_picker import DeckFolderPicker, show_folder_picker

        (self.tmp / "outer" / "inner").mkdir(parents=True)
        show_folder_picker(
            self.window, title="Pick", start=self.tmp / "outer", on_choose=lambda _p: None
        )
        self.pump()
        overlay = self.window.current_overlay()
        body = overlay.findChild(DeckFolderPicker)
        body._activate(self.tmp / "outer" / "inner")
        self.pump()
        self.window.handle_back()
        self.pump()
        self.assertIs(self.window.current_overlay(), overlay)
        self.assertEqual(body.current(), self.tmp / "outer")
        # Lands on the folder it came out of, not on ".. (up)".
        self.assertEqual(body.picker.list.currentItem().text(), "inner")
        self.window.handle_back()
        self.pump()
        self.assertIsNone(self.window.current_overlay())

    def test_b_in_a_fomod_steps_back_and_asks_before_cancelling(self):
        from commander_gui.fomod import parse_config
        from Steamdeck import gamepad as pad
        from Steamdeck.fomod_panel import show_fomod

        config_path = self.tmp / "fomod" / "ModuleConfig.xml"
        config_path.parent.mkdir()
        config_path.write_text(_TWO_STEP_FOMOD, encoding="utf-8")
        cancelled = []
        show_fomod(
            self.window,
            parse_config(config_path),
            self.tmp,
            on_install=lambda _s: None,
            on_cancel=lambda: cancelled.append(True),
        )
        self.pump()
        overlay = self.window.current_overlay()
        self.window.handle_action(pad.CONTEXT)  # X = Next
        self.pump()
        self.window.handle_back()  # back to step 1
        self.pump()
        self.assertIs(self.window.current_overlay(), overlay)
        self.window.handle_back()  # asks
        self.pump()
        self.assertIsNot(self.window.current_overlay(), overlay)
        self.window.handle_back()  # "no" - back in the installer
        self.pump()
        self.assertIs(self.window.current_overlay(), overlay)
        self.assertEqual(cancelled, [])
        self.window.dismiss_overlay()
        self.pump()
        self.assertEqual(cancelled, [True])

    def test_b_on_setup_with_profiles_goes_back_instead_of_quitting(self):
        from Steamdeck.window import SETUP_KEY

        self.goto(SETUP_KEY)
        self.window.handle_back()
        self.pump()
        self.assertIsNone(self.window.current_overlay())
        self.assertEqual(self.window.current_key(), self.window.start_screen())

    # -- keyboard -----------------------------------------------------------
    def test_keyboard_wraps_rows_and_l1_shifts(self):
        from Steamdeck import gamepad as pad

        mods = self.goto("mods")
        keyboard = self.window.open_keyboard(mods.search)
        self.pump()
        first = keyboard.first_key  # "q"
        first.setFocus()
        self.assertTrue(keyboard.grid_move(first, "left"))
        self.assertEqual(self.focused().text(), "p")
        keyboard.handle_action(pad.TAB_PREV)  # L1 = Shift
        self.assertTrue(keyboard.shift_button.isChecked())
        keyboard._number_keys[0].click()  # "1" shifted
        self.assertEqual(mods.search.text(), "!")
        self.assertFalse(keyboard.shift_button.isChecked())  # one-shot
        keyboard.done()
        self.pump()
        mods.search.clear()

    def test_every_key_is_reachable_on_the_keyboard(self):
        mods = self.goto("mods")
        keyboard = self.window.open_keyboard(mods.search)
        self.pump()
        overlay = self.window.current_overlay()
        from Steamdeck.focus import audit_focusables

        reached = self.walk(overlay, keyboard.first_key)
        missing = [w.text() for w in audit_focusables(overlay) if w not in reached]
        self.assertEqual(missing, [])
        keyboard.done()
        self.pump()

    # -- misc --------------------------------------------------------------
    def test_system_rows_with_details_are_focus_stops(self):
        from PySide6.QtCore import Qt

        from Steamdeck.widgets import DeckStatusRow

        row = DeckStatusRow("Proton", interactive=True)
        row.set_detail("GE-Proton10-3 in compatibilitytools.d")
        self.assertEqual(row.focusPolicy(), Qt.FocusPolicy.StrongFocus)

    def test_hints_drop_actions_that_would_do_nothing(self):
        play = self.goto("play")
        play.mo2_button.setEnabled(False)
        play.update_hints()
        buttons = [button for button, _label in self.window.hint_bar.hints()]
        self.assertNotIn("Y", buttons)
        play.mo2_button.setEnabled(True)
        play.update_hints()
        buttons = [button for button, _label in self.window.hint_bar.hints()]
        self.assertIn("Y", buttons)

    def test_right_stick_glides_the_screen_and_focus_follows(self):
        import time

        settings = self.goto("settings")
        self.window.resize(1280, 500)
        self.pump()
        bar = settings.scroll.verticalScrollBar()
        if bar.maximum() == 0:
            self.skipTest("settings fits without scrolling here")
        settings.theme_row.setFocus()
        self.pump()
        focus = self.window._focus
        with patch.object(self.window, "isActiveWindow", return_value=True):
            focus.on_pad_scroll(0.6)
            seen = []
            for _ in range(6):
                deadline = time.monotonic() + 0.03
                while time.monotonic() < deadline:
                    self.app.processEvents()
                seen.append(bar.value())
            focus.on_pad_scroll(0.0)
        self.pump()
        # Many small steps, not one jump: the value kept climbing frame by
        # frame while the stick was held.
        self.assertGreater(seen[-1], seen[0])
        self.assertGreaterEqual(len(set(seen)), 3)
        self.assertFalse(focus._scroll_timer.isActive())
        landed = self.focused()
        viewport = settings.scroll.viewport()
        centre = landed.mapTo(viewport, landed.rect().center())
        self.assertTrue(viewport.rect().contains(centre))

    def test_right_stick_glides_a_list_and_the_row_follows(self):
        import time

        from Steamdeck.widgets import DeckOverlay, DeckPicker

        picker = DeckPicker("", [(str(n), n) for n in range(80)])
        self.window.show_overlay(DeckOverlay("Long", picker))
        self.pump()
        picker.list.setFocus()
        picker.list.setCurrentRow(0)
        self.pump()
        bar = picker.list.verticalScrollBar()
        focus = self.window._focus
        with patch.object(self.window, "isActiveWindow", return_value=True):
            focus.on_pad_scroll(1.0)
            deadline = time.monotonic() + 0.2
            while time.monotonic() < deadline:
                self.app.processEvents()
            focus.on_pad_scroll(0.0)
        self.pump()
        self.assertGreater(bar.value(), 0)
        current = picker.list.currentIndex()
        self.assertGreater(current.row(), 0)
        self.assertTrue(
            picker.list.viewport().rect().contains(picker.list.visualRect(current))
        )
        self.window.dismiss_overlay()

    def test_right_stick_is_ignored_while_the_game_has_focus(self):
        focus = self.window._focus
        with patch.object(self.window, "isActiveWindow", return_value=False):
            focus.on_pad_scroll(1.0)
        self.assertFalse(focus._scroll_timer.isActive())

    def test_right_stick_scrolls_the_screen(self):
        from Steamdeck import gamepad as pad

        settings = self.goto("settings")
        self.window.resize(1280, 500)
        self.pump()
        bar = settings.scroll.verticalScrollBar()
        if bar.maximum() == 0:
            self.skipTest("settings fits without scrolling here")
        before = bar.value()
        self.window._focus.dispatch(pad.SCROLL_DOWN)
        self.pump()
        self.assertGreater(bar.value(), before)


class DeckGamepadStickTests(unittest.TestCase):
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

    def test_right_stick_is_analog_scroll_not_movement(self):
        pad = self.pad
        speeds = []
        self.monitor.scroll_axis.connect(speeds.append)
        self.monitor.feed(pad.EV_ABS, pad.ABS_RY, 32767)
        self.monitor.feed(pad.EV_ABS, pad.ABS_RY, 16384)
        self.monitor.feed(pad.EV_ABS, pad.ABS_RY, 16500)  # too small a change
        self.monitor.feed(pad.EV_ABS, pad.ABS_RY, 1000)  # inside the rest zone
        self.monitor.feed(pad.EV_ABS, pad.ABS_RY, -32768)
        self.assertEqual(self.actions, [])  # never a D-pad move
        self.assertEqual(len(speeds), 4)
        self.assertAlmostEqual(speeds[0], 1.0, places=2)
        self.assertTrue(0.3 < speeds[1] < 0.5)  # half tilt, rest zone removed
        self.assertEqual(speeds[2], 0.0)
        self.assertAlmostEqual(speeds[3], -1.0, places=2)

    def test_pause_lets_go_of_the_pad_and_resume_picks_it_back_up(self):
        with patch.object(self.monitor, "available", return_value=True):
            self.monitor.start()
            self.monitor.pause()
            self.assertTrue(self.monitor.is_paused())
            self.assertFalse(self.monitor._rescan.isActive())
            self.assertEqual(self.monitor.device_names(), [])
            self.monitor.resume()
            self.assertFalse(self.monitor.is_paused())
            self.assertTrue(self.monitor._rescan.isActive())

    def test_held_direction_speeds_up(self):
        pad = self.pad
        self.monitor.feed(pad.EV_KEY, pad.BTN_DPAD_DOWN, 1)
        self.assertEqual(self.monitor.repeat_interval(), pad.REPEAT_RATE_MS)
        for _ in range(pad.REPEAT_FAST_AFTER):
            self.monitor._on_repeat()
        self.assertEqual(self.monitor.repeat_interval(), pad.REPEAT_FAST_RATE_MS)
        self.assertEqual(self.monitor._repeat.interval(), pad.REPEAT_FAST_RATE_MS)
        self.monitor.feed(pad.EV_KEY, pad.BTN_DPAD_DOWN, 0)
        self.assertEqual(self.actions.count(pad.DOWN), pad.REPEAT_FAST_AFTER + 1)

