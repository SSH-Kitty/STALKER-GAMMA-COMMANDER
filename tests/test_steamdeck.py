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
        with patch.dict(os.environ, {DEPTH_ENV: "0"}, clear=False):
            with patch(
                "commander_gui.deck_launch.os.execve",
                side_effect=lambda *a, **k: order.append("exec"),
            ):
                relaunch_exec(
                    deck=True,
                    release_lock=lambda: order.append("unlock"),
                )
        self.assertEqual(order, ["unlock", "exec"])

    def test_exec_failure_is_reported_not_raised(self):
        with patch.dict(os.environ, {DEPTH_ENV: "0"}, clear=False):
            with patch(
                "commander_gui.deck_launch.os.execve",
                side_effect=OSError(12, "Cannot allocate memory"),
            ):
                self.assertFalse(
                    relaunch_exec(deck=True, release_lock=lambda: None)
                )

    def test_relaunch_loop_guard(self):
        execve = Mock()
        with patch.dict(os.environ, {DEPTH_ENV: "2"}, clear=False):
            with patch("commander_gui.deck_launch.os.execve", execve):
                with self.assertRaises(DeckRelaunchError):
                    relaunch_exec(deck=True, release_lock=lambda: None)
        execve.assert_not_called()

    def test_depth_is_incremented_for_the_child(self):
        captured = {}

        def fake_execve(path, argv, env):
            captured.update(env)

        with patch.dict(os.environ, {DEPTH_ENV: "0"}, clear=False):
            with patch("commander_gui.deck_launch.os.execve", fake_execve):
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
        four have no Deck screen behind them, so accepting one would leave
        the window with a start screen it cannot build.
        """
        for desktop_only in ("modmanager", "utilities", "systemcheck", "about"):
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
            r'"deck_start_screen"\s*\]\s*not in \{(.*?)\}', source, re.S
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
        ):
            patcher = patch(target, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Deck tests run before the rest of the suite (see the ordering hook
        # in tests/conftest.py), so the process is still clean here and
        # DeckWindow's real app-wide restyle is safe to exercise.
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

    def test_nav_is_reachable_from_content_and_back(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        from Steamdeck.focus import audit_focusables

        self.goto("settings")
        content = audit_focusables(self.window._pages["settings"])
        self.assertTrue(content)
        content[-1].setFocus(Qt.FocusReason.OtherFocusReason)
        self.pump(2)
        for _ in range(4):
            QTest.keyClick(self.window, Qt.Key.Key_Down)
            self.pump(1)
            if self.app.focusWidget() in self.window._nav_buttons.values():
                break
        self.assertIn(self.app.focusWidget(), self.window._nav_buttons.values())

        for _ in range(4):
            QTest.keyClick(self.window, Qt.Key.Key_Up)
            self.pump(1)
            if self.app.focusWidget() not in self.window._nav_buttons.values():
                break
        self.assertNotIn(self.app.focusWidget(), self.window._nav_buttons.values())

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
        with patch.dict(os.environ, {"COMMANDER_DECK_WINDOWED": ""}):
            with patch("Steamdeck.window.is_steam_deck", return_value=True):
                self.window.show_for_environment()
                self.pump()
                self.assertTrue(self.window.isFullScreen())

    def test_windowed_elsewhere(self):
        with patch.dict(os.environ, {"COMMANDER_DECK_WINDOWED": ""}):
            with patch("Steamdeck.window.is_steam_deck", return_value=False):
                with patch("Steamdeck.window.in_game_mode", return_value=False):
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
        item = mods.list.item(0)
        self.assertIsNotNone(item)
        before = item.data(3 + 1)  # Qt.UserRole + 1
        with patch("Steamdeck.modlist_io.mo2_running", return_value=True):
            with patch("Steamdeck.modlist_io.save_lines") as save:
                mods._toggle(item)
        save.assert_not_called()
        self.assertEqual(item.data(3 + 1), before)

    def test_mod_toggle_is_blocked_during_an_install(self):
        mods = self.goto("mods")
        item = mods.list.item(0)
        self.window.install_busy = True
        with patch("Steamdeck.modlist_io.save_lines") as save:
            mods._toggle(item)
        self.window.install_busy = False
        save.assert_not_called()

    def test_mod_toggle_writes_and_backs_up(self):
        mods = self.goto("mods")
        item = mods.list.item(0)
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
        from commander_gui.ui import deck_switch

        self.window.install_busy = True
        try:
            with patch.object(deck_switch.QMessageBox, "warning") as warn:
                with patch("commander_gui.deck_launch.os.execve") as execve:
                    deck_switch.switch_mode(self.window, deck=False)
            execve.assert_not_called()
            warn.assert_called_once()
        finally:
            self.window.install_busy = False

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


if __name__ == "__main__":
    unittest.main()
