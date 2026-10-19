"""ptrace_scope 2 or 3 stops Wine hooking the game: MO2 fails with Error 87."""

import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.analyzers.mo2 import analyze_usvfs
from assistant.findings import Severity
from commander_gui.launcher import ptrace_scope


class PtraceScopeTests(unittest.TestCase):
    def test_reads_value_and_defaults_to_zero(self):
        with patch.object(Path, "read_text", return_value="3\n"):
            self.assertEqual(ptrace_scope(), 3)
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            self.assertEqual(ptrace_scope(), 0)

    def test_assistant_flags_blocked_injection(self):
        lines = [
            "injecting to process 688 with same bitness",
            r"dll path: S:\Games\Stalker GAMMA\gamma\usvfs_x64.dll",
            "failed to inject: failed to write control data to target process (Access denied. [5])",
        ]
        findings = analyze_usvfs("gamma/logs/usvfs.log", "usvfs", lines)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, Severity.ERROR)
        self.assertIn("ptrace_scope", findings[0].detail)


if __name__ == "__main__":
    unittest.main()
