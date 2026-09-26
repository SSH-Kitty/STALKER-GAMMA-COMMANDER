"""Desktop UI tests that drive a real Qt event loop.

The Welcome overlay and smooth wheel scrolling only work by pumping events
(background fetches, animations). tests/conftest.py runs this file early,
with the Steam Deck tests, before the rest of the suite leaves the process
in a state where dispatching events can crash it.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from commander_gui import gui_settings


class DesktopWelcomeOverlayTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)

    def _overlay(self):
        from PySide6.QtWidgets import QWidget

        from commander_gui.ui import welcome_overlay

        welcome_overlay._shown_this_run = False
        self.addCleanup(setattr, welcome_overlay, "_shown_this_run", False)

        host = QWidget()
        host.resize(1200, 800)
        self.addCleanup(host.deleteLater)
        with patch.object(
            welcome_overlay,
            "fetch_latest_release_notes",
            return_value=("COMMANDER 9.9", "<h2>Notes</h2>"),
        ):
            overlay = welcome_overlay.maybe_show_welcome(host)
            for _ in range(50):
                self.app.processEvents()
                if overlay is None or overlay._task is None:
                    break
                time.sleep(0.02)
        return overlay

    def test_shows_notes_and_hides_for_good(self):
        overlay = self._overlay()
        self.assertIsNotNone(overlay)
        self.assertEqual(overlay.version_bar.latest.text(), "9.9")
        self.assertIn("Update available", overlay.version_bar.status.text())
        self.assertIn("Notes", overlay.notes.toPlainText())
        overlay.credits_button.click()
        self.assertIn("SSH-Kitty", overlay.notes.toPlainText())
        overlay.credits_button.click()
        self.assertIn("Notes", overlay.notes.toPlainText())
        overlay.hide_check.click()
        self.assertTrue(gui_settings.load_gui_settings()["welcome_hidden"])
        overlay.close_overlay()
        self.assertIsNone(self._overlay())

    def test_version_status_compares_with_the_latest_release(self):
        from commander_gui.ui import welcome_overlay

        with patch.object(welcome_overlay, "__version__", "1.3.0"):
            status = welcome_overlay.version_status
            self.assertEqual(status("COMMANDER 1.2.9H3"), ("Development build", "ok"))
            self.assertEqual(status("COMMANDER 1.3.0"), ("Up to date", "ok"))
            self.assertEqual(status("COMMANDER 1.3.1"), ("Update available", "warn"))
            self.assertEqual(status("")[1], "dim")
            self.assertEqual(status(None)[1], "dim")
        self.assertEqual(welcome_overlay.release_name("COMMANDER 1.2.9H3"), "1.2.9H3")

    def test_shows_again_after_an_update_despite_dont_show_again(self):
        from commander_gui.ui import welcome_overlay

        self.addCleanup(setattr, welcome_overlay, "_shown_this_run", False)

        def new_run():
            # Each check below stands for a separate launch of COMMANDER.
            welcome_overlay._shown_this_run = False
            os.environ.pop(welcome_overlay.WELCOME_SHOWN_ENV, None)

        with patch.object(welcome_overlay, "__version__", "1.3.0"):
            new_run()
            self.assertTrue(welcome_overlay.should_show_welcome())
            welcome_overlay.mark_welcome_seen()
            # Same run (e.g. switching to Deck Mode): not again.
            self.assertFalse(welcome_overlay.should_show_welcome())
            gui_settings.save_gui_settings(welcome_hidden=True)
            new_run()
            self.assertFalse(welcome_overlay.should_show_welcome())
        with patch.object(welcome_overlay, "__version__", "1.3.0H1"):
            new_run()
            self.assertTrue(welcome_overlay.should_show_welcome())
            welcome_overlay.mark_welcome_seen()
            new_run()
            self.assertFalse(welcome_overlay.should_show_welcome())

    def test_switching_modes_does_not_show_it_again(self):
        """Desktop <-> Deck Mode is an execve restart: module state is gone,
        but the environment carries over, and with it "already shown"."""
        from commander_gui.ui import welcome_overlay

        self.addCleanup(setattr, welcome_overlay, "_shown_this_run", False)
        with patch.object(welcome_overlay, "__version__", "1.3.0"):
            welcome_overlay._shown_this_run = False
            os.environ.pop(welcome_overlay.WELCOME_SHOWN_ENV, None)
            self.assertTrue(welcome_overlay.should_show_welcome())
            welcome_overlay.mark_welcome_seen()
            welcome_overlay._shown_this_run = False  # the exec'd process
            self.assertFalse(welcome_overlay.should_show_welcome())
        # A self-update restarts into a new version: its notes still show.
        with patch.object(welcome_overlay, "__version__", "1.3.1"):
            self.assertTrue(welcome_overlay.should_show_welcome())

    def test_link_buttons_carry_the_logos(self):
        overlay = self._overlay()
        discord, github = overlay.link_buttons[:2]
        self.assertFalse(github.icon().isNull())
        self.assertFalse(discord.icon().isNull())
        # Discord comes first, highlighted in its own colour.
        self.assertEqual(discord.objectName(), "discordButton")
        self.assertEqual(github.objectName(), "welcomeLink")
        overlay.close_overlay()


class DesktopSmoothScrollTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])

    def _wheel(self, target, delta, pixel=None):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent

        event = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            pixel or QPoint(0, 0),
            QPoint(0, delta),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        return self.app.sendEvent(target, event)

    def _view(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QListWidget, QMainWindow

        from commander_gui.ui.smooth_scroll import install

        window = QMainWindow()
        self.addCleanup(window.deleteLater)
        view = QListWidget()
        view.addItems([f"row {i}" for i in range(200)])
        window.setCentralWidget(view)
        window.resize(300, 300)
        window.show()
        self.app.processEvents()
        scroller = install(window)
        self.addCleanup(lambda: self.app.removeEventFilter(scroller))
        return view, QPoint

    def test_wheel_glides_instead_of_jumping(self):
        view, _ = self._view()
        bar = view.verticalScrollBar()
        self._wheel(view.viewport(), -120)
        self._wheel(view.viewport(), -120)
        # Not there yet: a glide is running.
        self.assertLess(bar.value(), 1)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertGreater(bar.value(), 0)

    def test_touchpads_are_left_alone(self):
        from PySide6.QtCore import QPoint

        view, _ = self._view()
        bar = view.verticalScrollBar()
        before = bar.value()
        self._wheel(view.viewport(), -120, pixel=QPoint(0, -40))
        from PySide6.QtCore import QVariantAnimation

        from commander_gui.ui.smooth_scroll import _ANIMATION_NAME

        animation = bar.findChild(QVariantAnimation, _ANIMATION_NAME)
        self.assertTrue(animation is None or animation.state() != QVariantAnimation.State.Running)
        self.assertGreaterEqual(bar.value(), before)
