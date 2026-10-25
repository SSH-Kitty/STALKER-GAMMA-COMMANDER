"""Tester-reported install/update regressions (user state lost, stale verify, FOMOD)."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from commander_gui.fomod import find_fomod
from commander_gui.integrity import scan_mods_md5
from commander_gui.keep_state import capture, restore
from commander_gui.modlist import (
    entries,
    merge_after_update,
    newest_backup_with_mods,
    snapshot_if_changed,
)


class MergeAfterUpdateTest(unittest.TestCase):
    def test_keeps_user_mods_and_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp)
            for name in ("My Mod", "5- Old GAMMA Mod"):
                (mods / name).mkdir()
            old = ["+My Mod", "+1- Pack Mod", "-2- Other", "+5- Old GAMMA Mod"]
            # Update renumbered the pack and dropped "Old GAMMA Mod".
            new = ["-3- Pack Mod", "+4- Other", "+6- New Mod"]
            merged = merge_after_update(
                old,
                new,
                official_before={"1- Pack Mod", "2- Other", "5- Old GAMMA Mod"},
                mods_dir=mods,
            )
            states = {name: status for status, name in entries(merged)}
            self.assertEqual(states["My Mod"], "Enabled")
            self.assertEqual(states["3- Pack Mod"], "Enabled")
            self.assertEqual(states["4- Other"], "Disabled")
            self.assertEqual(states["6- New Mod"], "Enabled")
            self.assertNotIn("5- Old GAMMA Mod", states)

    def test_user_mods_go_back_to_their_old_spot(self):
        # Reported: an update moved a mod the user had filed inside a GAMMA
        # category down to "Custom Mods" at the very bottom.
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp)
            for name in ("Atmospherics", "Mine A", "Mine B"):
                (mods / name).mkdir()
            old = [
                "# header",
                "+Mine A",
                "-Mine B",
                "-Custom Mods_separator",
                "+1- Fix",
                "+Atmospherics",
                "+2- Shaders",
                "-Fixes_separator",
                "+3- Audio",
            ]
            new = ["# header", "+4- Fix", "+5- Shaders", "-Fixes_separator", "+6- Audio"]
            merged = merge_after_update(
                old, new, official_before={"1- Fix", "2- Shaders", "3- Audio"}, mods_dir=mods
            )
            self.assertEqual(
                merged,
                [
                    "# header",
                    "+Mine A",
                    "-Mine B",
                    "-Custom Mods_separator",
                    "+4- Fix",
                    "+Atmospherics",
                    "+5- Shaders",
                    "-Fixes_separator",
                    "+6- Audio",
                ],
            )

    def test_wiped_modlist_finds_last_backup_with_mods(self):
        # Reported: after a PC crash mid-game MO2 rewrote the list with
        # every mod disabled; the pre-launch snapshot must bring it back.
        with tempfile.TemporaryDirectory() as tmp:
            modlist = Path(tmp) / "modlist.txt"
            modlist.write_text("+1- Pack\n-Sep_separator\n")
            snapshot_if_changed(modlist)
            snapshot_if_changed(modlist)  # unchanged: no second copy
            self.assertEqual(len(list(Path(tmp).glob("modlist-*.bak"))), 1)
            modlist.write_text("-1- Pack\n-Sep_separator\n")
            time.sleep(0.01)
            snapshot_if_changed(modlist)
            found = newest_backup_with_mods(modlist)
            self.assertEqual(found.read_text(), "+1- Pack\n-Sep_separator\n")

    def test_capture_restore_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            profile_dir = gamma / "profiles" / "G.A.M.M.A"
            profile_dir.mkdir(parents=True)
            (gamma / "mods" / "My Mod").mkdir(parents=True)
            modlist = profile_dir / "modlist.txt"
            modlist.write_text("+My Mod\n+1- Pack\n")
            ini = gamma / "ModOrganizer.ini"
            ini.write_text("[General]\nx=1\n[Settings]\nstyle=dark.qss\nbase_directory=/old\n")
            anomaly = Path(tmp) / "anomaly"
            (anomaly / "bin").mkdir(parents=True)
            exe = anomaly / "bin" / "AnomalyDX11.exe"
            exe.write_bytes(b"MZ new")
            profile = SimpleNamespace(gamma=str(gamma), anomaly=str(anomaly), mo2_profile="G.A.M.M.A")
            state = capture(profile, keep_exes=True)
            # What the CLI does.
            modlist.write_text("-1- Pack\n")
            ini.write_text("[General]\nx=1\n[Settings]\nstyle=default\nbase_directory=/new\n")
            exe.write_bytes(b"MZ old")
            restore(state)
            self.assertEqual(
                {n: s for s, n in entries(modlist.read_text().splitlines())},
                {"My Mod": "Enabled", "1- Pack": "Enabled"},
            )
            text = ini.read_text()
            self.assertIn("style=dark.qss", text)
            self.assertIn("base_directory=/new", text)
            self.assertEqual(exe.read_bytes(), b"MZ new")

    def test_keeps_mo2_executable_choice_and_arguments(self):
        from commander_gui.launcher import (
            default_launch_target,
            mo2_selected_title,
            set_mo2_selected_title,
        )

        def ini_text(titles, row, args=""):
            lines = ["[customExecutables]", f"size={len(titles)}"]
            for number, title in enumerate(titles, 1):
                lines += [f"{number}\\arguments=" + (args if "(DX11)" in title else ""),
                          f"{number}\\hide=" + ("true" if title == "Hidden" else "false"),
                          f"{number}\\title={title}"]
            lines += ["[Widgets]"] + ([f"MainWindow_executablesListBox_index={row}"] if row else [])
            return "\n".join(lines + ["[Settings]", "style=dark.qss", ""])

        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp)
            ini = gamma / "ModOrganizer.ini"
            # User picked DX11 (row 3: <Edit...>, Launcher, DX11; Hidden skipped) with -dbg.
            ini.write_text(ini_text(["Launcher", "Hidden", "Anomaly (DX11-AVX)", "Anomaly (DX11)"], 0, "-dbg"))
            ini.write_text(ini.read_text().replace("[Widgets]", "[Widgets]\nMainWindow_executablesListBox_index=3"))
            self.assertEqual(mo2_selected_title(str(gamma)), "Anomaly (DX11)")
            titles = ["Anomaly (DX11-AVX)", "Anomaly (DX11)"]
            self.assertEqual(default_launch_target(titles, str(gamma)), "Anomaly (DX11)")
            state = capture(SimpleNamespace(gamma=str(gamma), anomaly="", mo2_profile="G.A.M.M.A"))
            # The CLI renumbers, resets the args and re-picks DX11-AVX.
            ini.write_text(ini_text(["Launcher", "Anomaly (DX11)", "Anomaly (DX11-AVX)"], 3))
            restore(state)
            self.assertEqual(mo2_selected_title(str(gamma)), "Anomaly (DX11)")
            self.assertIn("2\\arguments=-dbg", ini.read_text())
            # A pick made in Commander lands in MO2's dropdown; a stale saved one loses.
            set_mo2_selected_title(str(gamma), "Anomaly (DX11-AVX)")
            self.assertEqual(mo2_selected_title(str(gamma)), "Anomaly (DX11-AVX)")
            self.assertEqual(
                default_launch_target(titles, str(gamma), saved="Anomaly (DX11)"),
                "Anomaly (DX11-AVX)",
            )


class StaleBaselineTest(unittest.TestCase):
    def test_update_after_baseline_rebuilds_instead_of_flagging(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp)
            (gamma / "mods" / "1- A").mkdir(parents=True)
            (gamma / "mods" / "1- A" / "f.txt").write_text("v1")
            profile = gamma / "profiles" / "G.A.M.M.A"
            profile.mkdir(parents=True)
            self.assertTrue(scan_mods_md5(str(gamma)).created)
            # A terminal update: files change and the modpack list is rewritten.
            (gamma / "mods" / "1- A" / "f.txt").write_text("v2")
            listing = profile / "modpack_maker_list.txt"
            listing.write_text("x")
            future = time.time() + 10
            os.utime(listing, (future, future))
            result = scan_mods_md5(str(gamma))
            self.assertTrue(result.stale)
            self.assertTrue(result.created)
            self.assertEqual(result.changed, [])


class FindFomodTest(unittest.TestCase):
    def test_mixed_case_and_wrapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "APM" / "inner"
            (root / "FOMOD").mkdir(parents=True)
            config = root / "FOMOD" / "moduleconfig.xml"
            config.write_text("<config/>")
            self.assertEqual(find_fomod(Path(tmp)), (config, root))

    def test_not_a_fomod(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "gamedata").mkdir()
            self.assertIsNone(find_fomod(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
