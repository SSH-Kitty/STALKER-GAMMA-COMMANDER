"""CLI 1.38 ``--resume``: only passed when resume.txt was left by the same command.

The CLI exits with an error when the file came from the other command
(``full-install`` vs ``update apply``).
"""

import tempfile
import unittest
from pathlib import Path

from commander_gui.cli_runner import cli_resume_args

HEADER = "# stalker-gamma resume file. Run the same command with --resume to continue.\n"


class CliResumeArgsTest(unittest.TestCase):
    def test_matches_only_the_command_that_wrote_the_file(self):
        with tempfile.TemporaryDirectory() as gamma:
            self.assertEqual(cli_resume_args(gamma, "full-install"), [])
            Path(gamma, "resume.txt").write_text(
                HEADER + "# command: update apply\n# status\trecord\nComplete\tfoo | bar\n"
            )
            self.assertEqual(cli_resume_args(gamma, "update apply"), ["--resume"])
            self.assertEqual(cli_resume_args(gamma, "full-install"), [])
            self.assertEqual(cli_resume_args(None, "update apply"), [])


if __name__ == "__main__":
    unittest.main()
