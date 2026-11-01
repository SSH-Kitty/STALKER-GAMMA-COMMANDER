import tempfile
import unittest
from pathlib import Path

from commander_gui.game_installs import find_install, fix_windows_paths, mo2_profile_for

WINDOWS_INI = (
    "[General]\n"
    "gamePath=@ByteArray(C:\\\\Games\\\\Anomaly)\n"
    "selected_profile=@ByteArray(G.A.M.M.A)\n"
    "[customExecutables]\n"
    "1\\binary=C:/Games/Anomaly/bin/AnomalyDX11AVX.exe\n"
    "1\\workingDirectory=C:/Games/Anomaly\n"
    "2\\binary=c:/games/gamma/explorer++/Explorer++.exe\n"
    "[Settings]\n"
    "download_directory=C:\\\\Games\\\\GAMMA\\\\downloads\n"
    "base_directory=C:/Games/GAMMA\n"
    "screenshot_directory=D:/Pictures\n"
)


class GameInstallsTest(unittest.TestCase):
    def test_find_and_fix_windows_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "anomaly" / "bin").mkdir(parents=True)
            (root / "anomaly" / "bin" / "AnomalyDX11AVX.exe").touch()
            gamma = root / "gamma"
            (gamma / "downloads").mkdir(parents=True)
            (gamma / "explorer++").mkdir()
            (gamma / "ModOrganizer.exe").touch()
            (gamma / "ModOrganizer.ini").write_text(WINDOWS_INI)

            anomaly, gamma_dir, cache = find_install(root / "gamma")
            self.assertEqual((anomaly, gamma_dir, cache), (
                str(root / "anomaly"), str(gamma), str(gamma / "downloads")
            ))

            left = fix_windows_paths(anomaly, gamma_dir)
            text = (gamma / "ModOrganizer.ini").read_text()
            z_anomaly, z_gamma = "Z:" + anomaly, "Z:" + gamma_dir
            self.assertEqual(left, ["D:/Pictures"])
            self.assertIn("gamePath=@ByteArray(" + z_anomaly.replace("/", "\\\\") + ")", text)
            self.assertIn("1\\binary=" + z_anomaly + "/bin/AnomalyDX11AVX.exe", text)
            self.assertIn("2\\binary=" + z_gamma + "/explorer++/Explorer++.exe", text)
            self.assertIn("download_directory=" + z_gamma.replace("/", "\\\\") + "\\\\downloads", text)
            self.assertIn("base_directory=" + z_gamma + "\n", text)
            self.assertTrue((gamma / "ModOrganizer.ini.windows.bak").is_file())

    def test_mo2_profile_falls_back_to_one_the_install_has(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp)
            (gamma / "profiles" / "G.A.M.M.A").mkdir(parents=True)
            self.assertEqual(mo2_profile_for(gamma, "CLIKRAD - ROMEO"), "G.A.M.M.A")
            (gamma / "profiles" / "CLIKRAD - ROMEO").mkdir()
            self.assertEqual(mo2_profile_for(gamma, "CLIKRAD - ROMEO"), "CLIKRAD - ROMEO")


if __name__ == "__main__":
    unittest.main()
