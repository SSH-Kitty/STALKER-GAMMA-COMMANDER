"""COMMANDER's game splash: the engine's splash switch and the engine-start check."""

import os
import shlex
import tempfile
import time
import unittest
from pathlib import Path

from commander_gui.launcher import Runner, build_command
from commander_gui.steam_shortcuts import coop_launch_options
from commander_gui.ui.game_splash import (
    SPLASH_FLAG,
    engine_started,
    is_launcher,
    mo2_error,
    wants_splash,
)


class GameSplashTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_splash_process_ends_when_the_game_starts(self):
        # A Tool window closing doesn't quit Qt on its own: splashes used to
        # stay running for good once COMMANDER was gone.
        import subprocess
        import sys

        from commander_gui.ui.game_splash import MARKERS

        logs = self.tmp / "anomaly" / "appdata" / "logs"
        logs.mkdir(parents=True)
        process = subprocess.Popen(
            [sys.executable, "-m", "commander_gui.ui.game_splash", str(self.tmp / "anomaly"),
             str(self.tmp / "gamma"), "#00ff00", "LOADING", "title", "--still"],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.kill)
        time.sleep(1.5)
        (logs / "xray_test.log").write_bytes(MARKERS[0])
        self.assertEqual(process.wait(timeout=10), 0)

    def test_flag_rides_on_the_real_command_line_after_saved_arguments(self):
        gamma = self.tmp / "gamma"
        (gamma / "profiles" / "Default").mkdir(parents=True)
        (gamma / "ModOrganizer.exe").write_bytes(b"")
        runner = Runner("native", "test", [], {})
        command, _env, _cwd = build_command(
            str(gamma), runner, target="Anomaly (DX11)", profile="Default",
            arguments=f"-smap2048 {SPLASH_FLAG}",
        )
        self.assertEqual(
            command[-4:],
            ["run", f"--arguments=-smap2048 {SPLASH_FLAG}", "-e", "Anomaly (DX11)"],
        )
        options = coop_launch_options("/p", "Default", "Anomaly", 480, f"-dbg {SPLASH_FLAG}")
        words = shlex.split(options.replace("%command%", "CMD"))
        self.assertEqual(words[-4:], ["run", f"--arguments=-dbg {SPLASH_FLAG}", "-e", "Anomaly"])
        # No splash arguments: the old shortcut stays byte-for-byte the same.
        self.assertTrue(coop_launch_options("/p", "D", "Anomaly", 480).endswith("run -e Anomaly"))

    def test_only_a_fresh_log_with_the_marker_counts(self):
        log = self.tmp / "xray_steamuser.log"
        log.write_bytes(b"Initializing Engine...\nStarting engine...\n")
        old = time.time() - 600
        os.utime(log, (old, old))
        since = time.time() - 1
        self.assertFalse(engine_started([self.tmp], since))  # last session's log
        log.write_bytes(b"Initializing Engine...\n")
        self.assertFalse(engine_started([self.tmp], since))
        log.write_bytes(b"Initializing Engine...\nStarting engine...\n")
        self.assertEqual(engine_started([self.tmp, self.tmp / "missing"], since), log)

    def test_a_load_crash_closes_the_splash_too(self):
        log = self.tmp / "xray_steamuser.log"
        log.write_bytes(b"Initializing Engine...\nFATAL ERROR\n")
        self.assertEqual(engine_started([self.tmp], time.time() - 1), log)
        log.write_bytes(b"Initializing Engine...\nstack trace:\n")
        self.assertEqual(engine_started([self.tmp], time.time() - 1), log)

    def test_only_a_new_mo2_error_counts(self):
        log = self.tmp / "mo_interface.log"
        since = time.time() - 1
        stamp = lambda t, level: time.strftime(f"[%Y-%m-%d %H:%M:%S.421 {level}] x", time.localtime(t))
        log.write_text(stamp(since - 600, "E") + "\n" + stamp(since + 1, "W") + "\n")
        self.assertFalse(mo2_error(log, since))  # old error, new warning
        log.write_text(stamp(since + 1, "E") + " Failed to run 'Anomaly (DX11-AVX)'.\n")
        self.assertTrue(mo2_error(log, since))
        self.assertFalse(mo2_error(self.tmp / "missing.log", since))

    def test_only_the_game_exes_get_a_splash(self):
        self.assertTrue(wants_splash("Z:/games/anomaly/bin/AnomalyDX11AVX.exe"))
        self.assertTrue(wants_splash("C:\\Anomaly\\bin\\AnomalyDX9.exe"))
        self.assertFalse(wants_splash("Z:/games/anomaly/AnomalyLauncher.exe"))
        self.assertFalse(wants_splash(""))
        self.assertTrue(is_launcher("Z:/games/anomaly/AnomalyLauncher.exe"))
        self.assertFalse(is_launcher("Z:/games/anomaly/bin/AnomalyDX11AVX.exe"))


if __name__ == "__main__":
    unittest.main()
