import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commander_gui import desktop_integration as di


class BuildCmdlineTest(unittest.TestCase):
    def test_replaces_first_word_and_keeps_length(self):
        old = b"/home/me/commander/.venv/bin/python\0-m\0commander_gui\0"
        new = di.build_cmdline(old, "STALKER COMMANDER")
        self.assertEqual(len(new), len(old))
        self.assertEqual(new.rstrip(b"\0").split(b"\0"), [b"STALKER COMMANDER", b"-m", b"commander_gui"])

    def test_drops_arguments_that_no_longer_fit(self):
        old = b"/bin/py\0-m\0commander_gui\0"
        new = di.build_cmdline(old, "STALKER COMMANDER")
        self.assertEqual(new.rstrip(b"\0"), b"STALKER COMMANDER")

    def test_gives_up_when_title_does_not_fit(self):
        self.assertIsNone(di.build_cmdline(b"/bin/py\0", "STALKER COMMANDER"))


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux /proc only")
class SetProcessTitleTest(unittest.TestCase):
    def test_process_monitors_see_the_full_name(self):
        code = (
            "from commander_gui.desktop_integration import set_process_title\n"
            "set_process_title()\n"
            "import sys\n"
            "sys.stdout.buffer.write(open('/proc/self/comm','rb').read())\n"
            "sys.stdout.buffer.write(open('/proc/self/cmdline','rb').read())\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        ).stdout
        comm, _, cmdline = out.partition(b"\n")
        self.assertEqual(comm, b"STALKER COMMAND")
        self.assertEqual(cmdline.split(b"\0")[0], b"STALKER COMMANDER")


class ScopeNameTest(unittest.TestCase):
    def test_escapes_dashes_in_the_desktop_id(self):
        self.assertEqual(
            di.scope_unit_name("stalker-gamma-commander", 42),
            "app-stalker\\x2dgamma\\x2dcommander-42.scope",
        )

    def test_already_in_matching_scope_skips_systemd(self):
        cgroup = "0::/user.slice/app.slice/app-stalker\\x2dgamma\\x2dcommander-7.scope\n"
        with patch.object(di.sys, "platform", "linux"), patch.object(
            di.Path, "read_text", return_value=cgroup
        ), patch.object(di.subprocess, "run") as run:
            self.assertTrue(di.join_app_scope())
            run.assert_not_called()


class MenuEntryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.system = self.tmp / "system"
        env = {"XDG_DATA_HOME": str(self.home), "XDG_DATA_DIRS": str(self.system)}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        target = patch(
            "commander_gui.steam_shortcuts.commander_launch_target",
            return_value=("/opt/My Games/run.sh", "/opt/My Games", ""),
        )
        target.start()
        self.addCleanup(target.stop)
        icon = patch("commander_gui.steam_shortcuts.ICON_DEST", self.home / "icons" / "c.png")
        icon.start()
        self.addCleanup(icon.stop)

    def test_writes_entry_with_name_icon_and_marker(self):
        path = di.ensure_menu_entry()
        self.assertEqual(path, self.home / "applications" / "stalker-gamma-commander.desktop")
        content = path.read_text(encoding="utf-8")
        self.assertIn("Name=STALKER COMMANDER\n", content)
        self.assertIn('Exec="/opt/My Games/run.sh"\n', content)
        self.assertIn(f"Icon={self.home / 'icons' / 'c.png'}\n", content)
        self.assertIn("X-Commander-Generated=true", content)
        self.assertEqual(list(path.parent.glob(".*")), [])

    def test_leaves_packaged_entry_alone(self):
        packaged = self.system / "applications" / "stalker-gamma-commander.desktop"
        packaged.parent.mkdir(parents=True)
        packaged.write_text("[Desktop Entry]\nName=Packaged\n", encoding="utf-8")
        self.assertIsNone(di.ensure_menu_entry())
        self.assertFalse((self.home / "applications").exists())

    def test_leaves_user_made_entry_alone(self):
        own = self.home / "applications" / "stalker-gamma-commander.desktop"
        own.parent.mkdir(parents=True)
        own.write_text("[Desktop Entry]\nName=Mine\n", encoding="utf-8")
        self.assertIsNone(di.ensure_menu_entry())
        self.assertEqual(own.read_text(encoding="utf-8"), "[Desktop Entry]\nName=Mine\n")


if __name__ == "__main__":
    unittest.main()
