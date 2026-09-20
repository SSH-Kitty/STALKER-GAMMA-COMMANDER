"""The single-instance rule and its from-source opt-out.

COMMANDER allows one running instance. ``run.sh`` opts out of that so several
source builds can run side by side while developing; the AppImage and the AUR
package do not. These tests pin both halves of that split, and the one line in
``run.sh`` whose loss would silently restore the old behaviour.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from pathlib import Path

from commander_gui import config
from commander_gui.config import (
    ALLOW_MULTIPLE_ENV,
    is_secondary_instance,
    mark_secondary_instance,
    multiple_instances_allowed,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class MultipleInstancesAllowedTests(unittest.TestCase):
    """``env`` is injected, so none of this touches the real environment."""

    def test_off_by_default(self):
        self.assertFalse(multiple_instances_allowed({}))

    def test_enabled_by_the_variable(self):
        self.assertTrue(multiple_instances_allowed({ALLOW_MULTIPLE_ENV: "1"}))

    def test_falsey_values_keep_the_rule(self):
        for value in ("0", "", "false", "FALSE", "no", "off", "  0  "):
            self.assertFalse(
                multiple_instances_allowed({ALLOW_MULTIPLE_ENV: value}), value
            )

    def test_appimage_cannot_be_unlocked_by_an_inherited_variable(self):
        """The requirement: shipped AppImages stay single-instance.

        AppRun deliberately avoids ``-E`` so ``PYTHONPATH`` survives, which
        means it inherits the whole environment - so a variable exported in
        the launching shell must not reach inside and switch the rule off.
        """
        self.assertFalse(
            multiple_instances_allowed(
                {ALLOW_MULTIPLE_ENV: "1", "APPIMAGE": "/opt/COMMANDER.AppImage"}
            )
        )

    def test_appdir_alone_is_not_an_appimage(self):
        """An extracted AppDir sets APPDIR but not APPIMAGE."""
        self.assertTrue(
            multiple_instances_allowed(
                {ALLOW_MULTIPLE_ENV: "1", "APPDIR": "/tmp/squashfs-root"}
            )
        )


class SecondaryInstanceStateTests(unittest.TestCase):
    def setUp(self):
        # Module-level state: restore it so test order cannot leak a marked
        # instance into anything else in the suite.
        previous = config._SECONDARY_INSTANCE
        self.addCleanup(setattr, config, "_SECONDARY_INSTANCE", previous)
        config._SECONDARY_INSTANCE = False

    def test_defaults_to_primary(self):
        self.assertFalse(is_secondary_instance())

    def test_marking_is_visible(self):
        mark_secondary_instance()
        self.assertTrue(is_secondary_instance())

    def test_title_is_untouched_for_the_first_instance(self):
        from commander_gui.ui.common import instance_window_title

        self.assertEqual(
            instance_window_title("STALKER COMMANDER"), "STALKER COMMANDER"
        )

    def test_title_is_marked_for_an_extra_instance(self):
        from commander_gui.ui.common import instance_window_title

        mark_secondary_instance()
        marked = instance_window_title("STALKER COMMANDER")
        self.assertNotEqual(marked, "STALKER COMMANDER")
        self.assertTrue(marked.startswith("STALKER COMMANDER"))
        # Composed titles (the nav bar builds "Install - ...") keep the mark.
        self.assertTrue(
            instance_window_title("Install - STALKER COMMANDER").startswith(
                "Install - STALKER COMMANDER"
            )
        )

    def test_title_marking_is_idempotent(self):
        from commander_gui.ui.common import instance_window_title

        mark_secondary_instance()
        once = instance_window_title("STALKER COMMANDER")
        self.assertEqual(instance_window_title(once), once)


class RunShTests(unittest.TestCase):
    """Guards on ``run.sh``, asserted against its source.

    Unusual, and deliberate: the fd-9 close is one easily-deleted line, and
    losing it silently restores the old behaviour - a second ``./run.sh``
    blocking on the venv-setup flock - with no other test in the suite
    failing. Same rationale as the architecture guards in test_steamdeck.py.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO_ROOT / "run.sh").read_text(encoding="utf-8")

    def test_opts_out_of_the_single_instance_rule(self):
        self.assertIn(f'export {ALLOW_MULTIPLE_ENV}="${{{ALLOW_MULTIPLE_ENV}:-1}}"', self.source)

    def test_releases_the_venv_lock_before_handing_off(self):
        """fd 9 must be closed, and closed *before* the exec into Python.

        `exec N>file` does not set FD_CLOEXEC and a flock lives on the open
        file description, so an inherited fd 9 keeps the venv-setup lock held
        for the GUI's entire lifetime.
        """
        self.assertIn("exec 9>&-", self.source)
        close_at = self.source.index("exec 9>&-")
        handoff_at = self.source.index("-m commander_gui")
        self.assertLess(close_at, handoff_at)

    def test_the_lock_is_still_taken_for_the_venv_setup(self):
        """The opt-out must not remove the protection it was added for."""
        self.assertIn("flock -x 9", self.source)
        lock_at = self.source.index("flock -x 9")
        close_at = self.source.index("exec 9>&-")
        self.assertLess(lock_at, close_at)


if __name__ == "__main__":
    unittest.main()
