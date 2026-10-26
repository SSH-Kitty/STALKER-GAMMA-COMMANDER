import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from commander_gui import applog
from commander_gui.ui.play_page import engine_log_summary


class CommanderLogTests(unittest.TestCase):
    def test_unclean_previous_run_is_a_startup_without_a_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(applog, "logs_dir", return_value=Path(tmp)):
            log = Path(tmp) / "commander.log"
            self.assertFalse(applog._previous_run_unclean())  # no log yet
            log.write_text("t INFO startup: COMMANDER starting\nt INFO shutdown: clean exit\n")
            self.assertFalse(applog._previous_run_unclean())
            log.write_text(log.read_text() + "t INFO startup: COMMANDER starting\nt INFO play: launch\n")
            self.assertTrue(applog._previous_run_unclean())

    def test_engine_log_summary_quotes_the_fatal_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "appdata" / "logs"
            logs.mkdir(parents=True)
            since = time.time() - 60
            self.assertEqual(engine_log_summary(tmp, since), "none written this session")
            (logs / "xray_a.log").write_text("Starting engine...\nFATAL ERROR\n[error]Expression : x\n")
            self.assertIn("[error]Expression : x", engine_log_summary(tmp, since))
            (logs / "xray_a.log").write_text("Starting engine...\nInternalCloseLog\n")
            self.assertIn("shut down normally", engine_log_summary(tmp, since))

    def test_dump_keeps_the_previous_launch_log(self):
        from commander_gui.log_dump import _is_candidate

        self.assertTrue(_is_candidate(Path("launcher.log.1")))
        self.assertTrue(_is_candidate(Path("commander.log.1")))
        self.assertFalse(_is_candidate(Path("launcher.log.stale-1789855249")))
        self.assertFalse(_is_candidate(Path("libfoo.so.1")))

    def test_scrub_hides_the_login_name_in_any_path_form(self):
        from commander_gui.log_dump import _scrub

        with patch("commander_gui.log_dump.getpass.getuser", return_value="Quiet"), \
                patch("commander_gui.log_dump.Path.home", return_value=Path("/home/Quiet")):
            text = _scrub(
                "/home/Quiet/Games z:/home/quiet/games Z:\\home\\QUIET\\x "
                "/run/media/quiet/Disk WINEPREFIX=/mnt/quiet quietly /x/quietness"
            )
        self.assertNotRegex(text.lower(), r"[/\\]quiet([/\\ ]|$)")
        self.assertIn("~/Games", text)
        self.assertIn("/run/media/user/Disk", text)
        self.assertIn("quietly /x/quietness", text)  # not a whole path part
