import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commander_gui.game_installs import (
    CopyCancelled,
    copy_install,
    find_install,
    fix_windows_paths,
    mo2_profile_for,
    needs_fix,
    windows_drive_folder,
    windows_paths,
)

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

    def test_folder_names_ending_in_a_paren_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Anomaly (1)" / "bin").mkdir(parents=True)
            (root / "Anomaly (1)" / "bin" / "AnomalyDX11.exe").touch()
            gamma = root / "GAMMA (copy)"
            gamma.mkdir()
            (gamma / "ModOrganizer.exe").touch()
            (gamma / "ModOrganizer.ini").write_text(
                "gamePath=@ByteArray(C:\\\\Games\\\\Anomaly (1))\n"
                "base_directory=C:/Games/GAMMA (copy)\n"
                "screenshot_directory=D:/Pictures\n"
            )
            anomaly, gamma_dir, _cache = find_install(root)
            self.assertTrue(needs_fix(anomaly, gamma_dir))
            self.assertEqual(windows_paths(gamma_dir)[0], "C:/Games/Anomaly (1)")
            left = fix_windows_paths(anomaly, gamma_dir)
            text = (gamma / "ModOrganizer.ini").read_text()
            self.assertEqual(left, ["D:/Pictures"])
            self.assertIn("gamePath=@ByteArray(" + ("Z:" + anomaly).replace("/", "\\\\") + ")\n", text)
            self.assertIn("base_directory=Z:" + gamma_dir + "\n", text)
            # Only the other drive is left, which is never fixed: no new ask.
            self.assertFalse(needs_fix(anomaly, gamma_dir))

    def test_unreadable_folder_finds_nothing(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(Path, "iterdir", side_effect=PermissionError),
        ):
            self.assertIsNone(find_install(tmp))

    def test_mo2_profile_falls_back_to_one_the_install_has(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp)
            (gamma / "profiles" / "G.A.M.M.A").mkdir(parents=True)
            self.assertEqual(mo2_profile_for(gamma, "CLIKRAD - ROMEO"), "G.A.M.M.A")
            (gamma / "profiles" / "CLIKRAD - ROMEO").mkdir()
            self.assertEqual(mo2_profile_for(gamma, "CLIKRAD - ROMEO"), "CLIKRAD - ROMEO")


if __name__ == "__main__":
    unittest.main()


MOUNTINFO = (
    "22 1 259:2 / / rw,relatime shared:1 - btrfs /dev/nvme0n1p2 rw\n"
    "40 22 259:3 / /run/media/me/Windows\\040C rw shared:2 - fuseblk /dev/nvme0n1p3 rw\n"
    "41 40 259:4 / /run/media/me/Windows\\040C/linux rw shared:3 - ext4 /dev/sda1 rw\n"
)


class WindowsDriveTest(unittest.TestCase):
    def test_finds_folders_on_a_windows_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = Path(tmp) / "mountinfo"
            info.write_text(MOUNTINFO)
            on_ntfs = "/run/media/me/Windows C/Games/GAMMA"
            self.assertEqual(windows_drive_folder("/home/me/GAMMA", on_ntfs, mountinfo=str(info)), on_ntfs)
            # A Linux drive mounted inside the Windows one is not Windows.
            self.assertIsNone(windows_drive_folder("/run/media/me/Windows C/linux/GAMMA", mountinfo=str(info)))
            self.assertIsNone(windows_drive_folder("/run/media/me/Windows CC", mountinfo=str(info)))
            self.assertIsNone(windows_drive_folder(on_ntfs, mountinfo=str(Path(tmp) / "missing")))


class CopyInstallTest(unittest.TestCase):
    def _install(self, root: Path) -> tuple[str, str, str]:
        (root / "Anomaly" / "bin").mkdir(parents=True)
        (root / "Anomaly" / "bin" / "AnomalyDX11.exe").write_bytes(b"x" * 10)
        gamma = root / "GAMMA"
        (gamma / "downloads").mkdir(parents=True)
        (gamma / "downloads" / "mod.7z").write_bytes(b"y" * 20)
        (gamma / "ModOrganizer.ini").write_text(
            f"gamePath=@ByteArray(Z:\\\\{str(root / 'Anomaly')[1:].replace('/', chr(92) * 2)})\n"
            f"base_directory=Z:{gamma}\n"
            "download_directory=C:/Games/GAMMA/downloads\n"
        )
        return str(root / "Anomaly"), str(gamma), str(gamma / "downloads")

    def test_copies_and_points_linux_paths_at_the_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = self._install(Path(tmp) / "win")
            seen = []
            anomaly, gamma, cache = copy_install(install, Path(tmp) / "linux", 30, lambda d, t: seen.append(d), lambda: False)
            self.assertEqual(Path(cache), Path(gamma) / "downloads")
            self.assertEqual((Path(cache) / "mod.7z").read_bytes(), b"y" * 20)
            self.assertTrue((Path(anomaly) / "bin" / "AnomalyDX11.exe").is_file())
            ini = (Path(gamma) / "ModOrganizer.ini").read_text()
            self.assertIn(f"base_directory=Z:{gamma}\n", ini)
            self.assertIn("Z:\\\\" + anomaly[1:].replace("/", "\\\\") + ")", ini)
            self.assertNotIn("/win/", ini.replace("\\\\", "/"))
            # Windows paths stay for fix_windows_paths.
            self.assertIn("C:/Games/GAMMA/downloads", ini)
            self.assertTrue(seen)

    def test_cancel_leaves_nothing_and_existing_folders_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = self._install(Path(tmp) / "win")
            target = Path(tmp) / "linux"
            with self.assertRaises(CopyCancelled):
                copy_install(install, target, 30, lambda d, t: None, lambda: True)
            self.assertEqual(list(target.iterdir()), [])
            (target / "GAMMA").mkdir()
            with self.assertRaises(FileExistsError):
                copy_install(install, target, 30, lambda d, t: None, lambda: False)
            self.assertEqual(list((target / "GAMMA").iterdir()), [])
