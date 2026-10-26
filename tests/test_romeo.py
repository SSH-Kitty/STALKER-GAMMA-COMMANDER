"""CLIKRΔD's ROMEO repack: detection and install layout."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from commander_gui.romeo import (
    KEPT_MCM,
    MCM_MOD,
    back_to_gamma,
    find_romeo,
    install,
    installed,
    is_romeo_profile,
    mcm_changed,
    uninstall,
)

ROOT = "CLIKRΔD - ROMEO"
PROFILE = "CLIKRΔD - ROMEO"


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class FindRomeoTest(unittest.TestCase):
    def test_release_layout(self):
        names = [ROOT, f"{ROOT}/GAMMA/mods/xx-A/gamedata/a.ltx", f"{ROOT}/GAMMA/profiles/{PROFILE}/modlist.txt"]
        self.assertEqual(find_romeo(names), (ROOT, PROFILE))

    def test_unwrapped_layout(self):
        names = ["GAMMA/mods/xx-A/a.ltx", "GAMMA/profiles/P/modlist.txt"]
        self.assertEqual(find_romeo(names), ("", "P"))

    def test_plain_mod_is_not_romeo(self):
        self.assertIsNone(find_romeo(["gamedata/configs/a.ltx", "meta.ini"]))
        self.assertIsNone(find_romeo([f"{ROOT}/GAMMA/profiles/{PROFILE}/modlist.txt"]))


class InstallTest(unittest.TestCase):
    def test_install_merges_into_gamma_and_anomaly(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "staging" / ROOT
            _write(src / "GAMMA/mods/xx-A/gamedata/new.ltx", "new")
            _write(src / f"GAMMA/profiles/{PROFILE}/modlist.txt", "+xx-A\n")
            _write(src / "GAMMA/overwrite/gamedata/configs/wd.ltx", "romeo")
            _write(src / "GAMMA/overwrite/gamedata/configs/laser.ltx", "romeo")
            _write(src / "GAMMA/splash.png", "romeo")
            _write(src / "Anomaly/appdata/clikrad_lean_presets.ltx", "romeo")
            gamma, anomaly = tmp / "gamma", tmp / "anomaly"
            _write(gamma / "mods/xx-A/gamedata/old.ltx", "old")  # previous ROMEO
            _write(gamma / "mods/10- GAMMA mod/a.ltx")
            _write(gamma / "profiles/G.A.M.M.A/modlist.txt", "+10- GAMMA mod\n")
            _write(gamma / "overwrite/gamedata/configs/wd.ltx", "player")
            _write(gamma / "splash.png", "gamma")
            _write(anomaly / "appdata/shaders_cache/x")
            _write(anomaly / "appdata/atmos_neutral.ltx")
            _write(anomaly / "appdata/Atmos_ato_summer.ltx")
            _write(anomaly / "appdata/user.ltx", "player")

            install(tmp / "staging", ROOT, PROFILE, gamma, anomaly)

            self.assertEqual(sorted(p.name for p in (gamma / "mods/xx-A/gamedata").iterdir()), ["new.ltx"])
            self.assertTrue((gamma / "mods/10- GAMMA mod/a.ltx").is_file())
            self.assertEqual((gamma / f"profiles/{PROFILE}/modlist.txt").read_text(), "+xx-A\n")
            self.assertEqual((gamma / "profiles/G.A.M.M.A/modlist.txt").read_text(), "+10- GAMMA mod\n")
            self.assertEqual((gamma / "overwrite/gamedata/configs/wd.ltx").read_text(), "player")
            self.assertEqual((gamma / "overwrite/gamedata/configs/laser.ltx").read_text(), "romeo")
            self.assertEqual((gamma / "splash.png").read_text(), "romeo")
            self.assertEqual((anomaly / "appdata/clikrad_lean_presets.ltx").read_text(), "romeo")
            self.assertEqual((anomaly / "appdata/user.ltx").read_text(), "player")
            self.assertFalse((anomaly / "appdata/shaders_cache").exists())
            self.assertFalse((anomaly / "appdata/atmos_neutral.ltx").exists())
            self.assertFalse((anomaly / "appdata/Atmos_ato_summer.ltx").exists())

    def test_update_drops_old_mods_and_keeps_changed_mcm(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            gamma, anomaly = tmp / "gamma", tmp / "anomaly"
            _write(gamma / "mods/10- GAMMA mod/a.ltx")

            def release(version: str, mods: list[str]) -> Path:
                src = tmp / version / ROOT
                for name in mods:
                    _write(src / f"GAMMA/mods/{name}/a.ltx", version)
                _write(src / f"GAMMA/mods/{MCM_MOD}/gamedata/configs/axr_options.ltx", version)
                _write(src / f"GAMMA/profiles/{PROFILE}/modlist.txt", f"+xx-A\n+{MCM_MOD}\n+10- GAMMA mod\n")
                return tmp / version

            install(release("v1", ["xx-A", "xx-Dropped"]), ROOT, PROFILE, gamma, anomaly)
            self.assertIs(mcm_changed(gamma, PROFILE), False)
            mcm = gamma / f"mods/{MCM_MOD}/gamedata/configs/axr_options.ltx"
            mcm.write_text("player")
            self.assertIs(mcm_changed(gamma, PROFILE), True)
            (gamma / f"profiles/{PROFILE}/modlist.txt").write_text("player order\n")

            install(release("v2", ["xx-A"]), ROOT, PROFILE, gamma, anomaly, keep_mcm=True)

            self.assertFalse((gamma / "mods/xx-Dropped").exists())
            self.assertEqual((gamma / "mods/xx-A/a.ltx").read_text(), "v2")
            self.assertTrue((gamma / "mods/10- GAMMA mod/a.ltx").is_file())
            self.assertEqual(mcm.read_text(), "v2")
            self.assertEqual((gamma / f"mods/{KEPT_MCM}/gamedata/configs/axr_options.ltx").read_text(), "player")
            profile = gamma / f"profiles/{PROFILE}"
            self.assertEqual(
                (profile / "modlist.txt").read_text().splitlines(),
                ["+xx-A", f"+{KEPT_MCM}", f"-{MCM_MOD}", "+10- GAMMA mod"],
            )
            self.assertEqual((profile / "modlist.txt.before-update").read_text(), "player order\n")
            self.assertIs(mcm_changed(gamma, PROFILE), False)

            # The next update leaves the kept copy alone and puts it back in place.
            install(release("v3", ["xx-A"]), ROOT, PROFILE, gamma, anomaly)
            self.assertEqual((gamma / f"mods/{KEPT_MCM}/gamedata/configs/axr_options.ltx").read_text(), "player")
            self.assertIn(f"+{KEPT_MCM}", (profile / "modlist.txt").read_text().splitlines())

            # Kept again: the newer copy wins (listed first) over the older one.
            mcm.write_text("player 2")
            install(release("v4", ["xx-A"]), ROOT, PROFILE, gamma, anomaly, keep_mcm=True)
            lines = (profile / "modlist.txt").read_text().splitlines()
            self.assertLess(lines.index(f"+{KEPT_MCM} 2"), lines.index(f"+{KEPT_MCM}"))


    def test_a_cancelled_update_keeps_the_old_mods(self):
        from threading import Event

        from commander_gui.mod_install import ModInstallError

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            gamma, anomaly = tmp / "gamma", tmp / "anomaly"
            for version, mods in (("v1", ["xx-A", "xx-B"]), ("v2", ["xx-A", "xx-C"])):
                for name in mods:
                    _write(tmp / version / ROOT / f"GAMMA/mods/{name}/a.ltx", version)
                _write(tmp / version / ROOT / f"GAMMA/profiles/{PROFILE}/modlist.txt", "")
            install(tmp / "v1", ROOT, PROFILE, gamma, anomaly)
            cancel = Event()
            cancel.set()
            with self.assertRaises(ModInstallError):
                install(tmp / "v2", ROOT, PROFILE, gamma, anomaly, cancel_event=cancel)
            self.assertEqual((gamma / "mods/xx-A/a.ltx").read_text(), "v1")
            self.assertTrue((gamma / "mods/xx-B").is_dir())

class UninstallTest(unittest.TestCase):
    def test_uninstall_puts_gamma_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            gamma, anomaly, cache = tmp / "gamma", tmp / "anomaly", tmp / "cache"
            src = tmp / "staging" / ROOT
            _write(src / "GAMMA/mods/xx-A/a.ltx")
            _write(src / f"GAMMA/mods/{MCM_MOD}/gamedata/configs/axr_options.ltx")
            _write(src / f"GAMMA/profiles/{PROFILE}/modlist.txt", f"+xx-A\n+{MCM_MOD}\n")
            _write(src / "GAMMA/splash.png", "romeo")
            _write(src / "Anomaly/appdata/clikrad_lean_presets.ltx")
            _write(gamma / "mods/10- GAMMA mod/a.ltx")
            _write(gamma / f"mods/{KEPT_MCM}/a.ltx")
            _write(gamma / "profiles/G.A.M.M.A/modlist.txt", "+10- GAMMA mod\n")
            _write(gamma / "splash.png", "gamma")
            _write(gamma / "ModOrganizer.ini", "[General]\nselected_profile=@ByteArray(G.A.M.M.A)\n")
            _write(anomaly / "appdata/Atmos_Neutral.ltx", "old")
            _write(anomaly / "appdata/user.ltx", "player")
            git = shutil.which("git")
            if git:
                work = tmp / "work"
                _write(work / "G.A.M.M.A/modpack_patches/appdata/Atmos_Neutral.ltx", "gamma atmos")
                _write(work / "G.A.M.M.A/modpack_patches/appdata/other.ltx")
                run = lambda *a: subprocess.run([git, "-C", str(work), *a], check=True, capture_output=True)
                run("init", "-q")
                run("add", ".")
                run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
                subprocess.run([git, "clone", "-q", "--bare", str(work), str(cache / "Stalker_GAMMA.git")], check=True)

            install(tmp / "staging", ROOT, PROFILE, gamma, anomaly)
            self.assertTrue(installed(gamma))
            self.assertTrue(is_romeo_profile(gamma, PROFILE))
            self.assertFalse(is_romeo_profile(gamma, "G.A.M.M.A"))
            removed, atmos = uninstall(gamma, anomaly, cache)

            self.assertEqual(removed, [PROFILE])
            self.assertFalse(installed(gamma))
            self.assertEqual(sorted(p.name for p in (gamma / "mods").iterdir()), ["10- GAMMA mod"])
            self.assertEqual(sorted(p.name for p in (gamma / "profiles").iterdir()), ["G.A.M.M.A"])
            self.assertEqual((gamma / "splash.png").read_text(), "gamma")
            self.assertFalse((gamma / "splash.png.before-romeo").exists())
            self.assertFalse((anomaly / "appdata/clikrad_lean_presets.ltx").exists())
            self.assertEqual((anomaly / "appdata/user.ltx").read_text(), "player")
            self.assertIn("G.A.M.M.A", (gamma / "ModOrganizer.ini").read_text())
            if git:
                self.assertTrue(atmos)
                self.assertEqual((anomaly / "appdata/Atmos_Neutral.ltx").read_text(), "gamma atmos")
                self.assertFalse((anomaly / "appdata/other.ltx").exists())

            other = tmp / "other-gamma"
            profiles = [
                SimpleNamespace(gamma=str(gamma), mo2_profile=PROFILE),
                SimpleNamespace(gamma=str(other), mo2_profile=PROFILE),
            ]
            settings = SimpleNamespace(profiles=profiles, save=lambda: None)
            back_to_gamma(settings, gamma, removed)
            self.assertEqual([p.mo2_profile for p in profiles], ["G.A.M.M.A", PROFILE])

    def test_refuses_without_gamma_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            _write(gamma / "mods/xx-A/a.ltx")
            with self.assertRaises(Exception):
                uninstall(gamma, Path(tmp) / "anomaly", Path(tmp) / "cache")
            self.assertTrue((gamma / "mods/xx-A/a.ltx").exists())


if __name__ == "__main__":
    unittest.main()
