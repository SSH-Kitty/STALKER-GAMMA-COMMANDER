import os
import tempfile
import unittest
from pathlib import Path

from commander_gui.launcher import (
    LaunchError,
    shortcut_slug,
    write_desktop_shortcut,
)


class ShortcutSlugTest(unittest.TestCase):
    def test_sanitizes_title(self):
        self.assertEqual(shortcut_slug("Anomaly (DX11-AVX)"), "anomaly-dx11-avx")

    def test_empty_title_falls_back(self):
        self.assertEqual(shortcut_slug("!!!"), "stalker-gamma")


class WriteDesktopShortcutTest(unittest.TestCase):
    def test_writes_desktop_file_with_env_and_exec_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_desktop_shortcut(
                "Anomaly (DX11-AVX)",
                ["gamemoderun", "umu-run", "/games/GAMMA/ModOrganizer.exe", "run", "-e", "Anomaly (DX11-AVX)"],
                {"PROTONPATH": "/opt/GE-Proton with space", "WINEPREFIX": "/pfx"},
                "/games/GAMMA",
                icon="/icons/stalker-gamma.png",
                directory=Path(tmp),
            )
            self.assertEqual(path.name, "anomaly-dx11-avx.desktop")
            content = path.read_text(encoding="utf-8")
            self.assertIn("Name=Anomaly (DX11-AVX)", content)
            self.assertIn("Type=Application", content)
            self.assertIn("Path=/games/GAMMA", content)
            self.assertIn("Terminal=false", content)
            self.assertIn("Icon=/icons/stalker-gamma.png", content)
            exec_line = next(l for l in content.splitlines() if l.startswith("Exec="))
            # env vars inlined, sorted, quoted
            self.assertTrue(exec_line.startswith('Exec=env "PROTONPATH=/opt/GE-Proton with space" "WINEPREFIX=/pfx"'))
            self.assertIn('"Anomaly (DX11-AVX)"', exec_line)
            self.assertTrue(os.stat(path).st_mode & 0o111, "shortcut must be executable")

    def test_no_env_omits_env_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_desktop_shortcut(
                "Anomaly", ["wine", "game.exe"], {}, "/games", directory=Path(tmp)
            )
            exec_line = next(
                l for l in path.read_text().splitlines() if l.startswith("Exec=")
            )
            self.assertEqual(exec_line, 'Exec="wine" "game.exe"')

    def test_empty_command_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LaunchError):
                write_desktop_shortcut("X", [], {}, ".", directory=Path(tmp))

    def test_quoting_escapes_special_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_desktop_shortcut(
                "X", ['C:\\dir with "quotes"\\app.exe'], {}, ".", directory=Path(tmp)
            )
            exec_line = next(
                l for l in path.read_text().splitlines() if l.startswith("Exec=")
            )
            self.assertIn('"C:\\\\dir with \\"quotes\\"\\\\app.exe"', exec_line)


if __name__ == "__main__":
    unittest.main()
