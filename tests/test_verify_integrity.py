"""Verify Integrity: plain-language findings, verdicts and repair safety."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from commander_gui.integrity import (
    CacheArchiveVerifyResult,
    GammaVerifyResult,
    Md5ScanResult,
    build_findings,
    overall_verdict,
    scan_mods_md5,
    verify_gamma,
)
from commander_gui.settings import CliProfile, CliSettings


class InlineTask:
    """BackgroundTask stand-in that runs its callable at start()."""

    def __init__(self, fn, *args, parent=None, **kwargs):
        self._call = lambda: fn(*args, **kwargs)
        self._slots = []
        self.result = Mock(connect=self._slots.append)
        self.error = Mock()

    def start(self):
        value = self._call()
        for slot in self._slots:
            slot(value)


def _levels(findings):
    return {finding.title: finding.level for finding in findings}


class FindingsTests(unittest.TestCase):
    def test_cache_notes_are_info_and_never_a_problem(self):
        cache = CacheArchiveVerifyResult(
            verified=["a.7z"], missing=["CREPIS_ICONS_3_0_6.zip"], mismatched=["b.7z"]
        )
        scan = Md5ScanResult()
        findings = build_findings(anomaly_checked=True, scan=scan, cache=cache)
        self.assertEqual(overall_verdict(findings, scan), "healthy")
        levels = _levels(findings)
        self.assertEqual(levels["Archives not in the download cache"], "info")
        self.assertEqual(levels["Older archive versions in the download cache"], "info")

    def test_missing_gamma_mod_is_a_problem_and_own_mod_only_a_warning(self):
        presence = GammaVerifyResult(
            used_official_list=True,
            missing=["12- Gamma Mod", "My Mod"],
            official_missing=["12- Gamma Mod"],
            extra_missing=["My Mod"],
        )
        findings = build_findings(anomaly_checked=True, presence=presence, scan=Md5ScanResult())
        levels = _levels(findings)
        self.assertEqual(levels["GAMMA mods are missing or empty"], "problem")
        self.assertEqual(levels["Some of your own mods have no files"], "warn")
        self.assertEqual(findings[0].level, "problem")  # worst first

    def test_an_entry_gamma_never_installs_is_not_reported_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp)
            for marker in ("ModOrganizer.exe", "ModOrganizer.ini"):
                (gamma / marker).write_text("")
            (gamma / "mods").mkdir()
            profile = gamma / "profiles" / "G.A.M.M.A"
            profile.mkdir(parents=True)
            typo = "DarkasleQif's and iTheon's Parts Condition Color Thresholds Changes"
            (profile / "modlist.txt").write_text(f"+{typo}\n+12- Gone Mod\n")
            result = verify_gamma(str(gamma), "G.A.M.M.A", official_mods={typo, "12- Gone Mod"})
            self.assertEqual(result.missing, ["12- Gone Mod"])
            self.assertEqual(result.official_missing, ["12- Gone Mod"])

    def test_a_snapshot_only_run_is_never_called_healthy(self):
        scan = Md5ScanResult(created=True, files_scanned=10)
        findings = build_findings(anomaly_checked=True, presence=GammaVerifyResult(), scan=scan)
        self.assertEqual(overall_verdict(findings, scan), "snapshot")
        self.assertNotIn("Everything checks out", _levels(findings))

    def test_ok_row_is_first_and_never_denies_changed_files(self):
        cache = CacheArchiveVerifyResult(verified=["a.7z"], missing=["b.7z"])
        scan = Md5ScanResult(changed=["1- A/f.txt"])
        findings = build_findings(anomaly_checked=True, scan=scan, cache=cache)
        self.assertEqual(findings[0].level, "ok")
        self.assertNotIn("no mod file changed", findings[0].detail)

    def test_an_anomaly_check_that_returned_nothing_is_a_problem(self):
        findings = build_findings(anomaly_checked=False)
        self.assertEqual(overall_verdict(findings, None), "attention")


class StaleSnapshotAfterRepairTests(unittest.TestCase):
    def _gamma(self, tmp):
        gamma = Path(tmp)
        (gamma / "mods" / "1- A").mkdir(parents=True)
        (gamma / "mods" / "1- A" / "f.txt").write_text("v1")
        (gamma / "profiles" / "G.A.M.M.A").mkdir(parents=True)
        return gamma

    def test_trusted_rescan_still_compares_after_the_modpack_list_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = self._gamma(tmp)
            scan_mods_md5(str(gamma))
            (gamma / "mods" / "1- A" / "f.txt").write_text("broken")
            listing = gamma / "profiles" / "G.A.M.M.A" / "modpack_maker_list.txt"
            listing.write_text("x")
            os.utime(listing, (time.time() + 10, time.time() + 10))
            result = scan_mods_md5(str(gamma), trust_baseline=True)
            self.assertFalse(result.created)
            self.assertEqual(result.changed, ["mods/1- A/f.txt"])


    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFOs")
    def test_a_fifo_in_mods_is_reported_not_waited_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = self._gamma(tmp)
            os.mkfifo(gamma / "mods" / "1- A" / "pipe")
            result = scan_mods_md5(str(gamma))  # hung forever before
            self.assertIn("mods/1- A/pipe", result.errors)

def _git(*args, cwd=None):
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@unittest.skipUnless(__import__("shutil").which("git"), "needs git")
class GammaRepoFilesTests(unittest.TestCase):
    """GAMMA's own files are judged against its repositories in the cache."""

    def _install(self, tmp):
        from commander_gui.integrity import MANIFEST_FILENAME

        work = Path(tmp) / "work"
        addon = work / "G.A.M.M.A" / "modpack_addons" / "G.A.M.M.A. Fixes" / "gamedata"
        addon.mkdir(parents=True)
        (addon / "fix.ltx").write_text("original")
        (addon / "axr_options.ltx").write_text("mcm")
        (work / "README.md").write_text("not copied")
        _git("init", "-q", cwd=work)
        _git("add", ".", cwd=work)
        _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x", cwd=work)
        gamma = Path(tmp) / "gamma"
        repo = gamma / "downloads" / "Stalker_GAMMA.git"
        _git("clone", "-q", "--bare", str(work), str(repo))
        _git("--git-dir", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD")
        mod = gamma / "mods" / "G.A.M.M.A. Fixes" / "gamedata"
        mod.mkdir(parents=True)
        (mod / "fix.ltx").write_text("original")
        (mod / "axr_options.ltx").write_text("my own MCM values")
        return gamma, gamma / MANIFEST_FILENAME

    def test_a_damaged_file_is_found_and_copied_back_from_the_cache(self):
        from commander_gui.integrity import (
            compare_repo_files,
            gamma_repo_files,
            repo_branches,
            restore_repo_files,
        )

        with tempfile.TemporaryDirectory() as tmp:
            gamma, manifest = self._install(tmp)
            rel = "mods/G.A.M.M.A. Fixes/gamedata/fix.ltx"
            files, unreadable = gamma_repo_files(str(gamma), repo_branches(CliProfile()))
            # MCM's saved settings and files that land outside mods/ are left out.
            self.assertEqual(list(files), [rel])
            self.assertEqual(sorted(unreadable), ["gamma_large_files_v2", "gamma_setup", "teivaz_anomaly_gunslinger"])
            scan_mods_md5(str(gamma), git_blobs=files)  # first run: snapshot only
            self.assertEqual(compare_repo_files(files, scan_mods_md5(str(gamma), git_blobs=files)).broken, [])

            (gamma / rel).write_text("broken")
            check = compare_repo_files(files, scan_mods_md5(str(gamma), git_blobs=files))
            self.assertEqual(check.changed, [rel])

            stamp = manifest.stat().st_mtime_ns
            result = restore_repo_files(str(gamma), files, check.broken)
            self.assertEqual((result.restored, result.failed), ([rel], []))
            self.assertEqual((gamma / rel).read_text(), "original")
            # The snapshot learns the repaired file and keeps its date.
            self.assertEqual(scan_mods_md5(str(gamma)).changed, [])
            self.assertEqual(manifest.stat().st_mtime_ns, stamp)

    def test_a_symlinked_destination_is_never_written_through(self):
        from commander_gui.integrity import (
            gamma_repo_files,
            repo_branches,
            restore_repo_files,
        )

        with tempfile.TemporaryDirectory() as tmp:
            gamma, _ = self._install(tmp)
            outside = Path(tmp) / "outside.ltx"
            outside.write_text("keep")
            target = gamma / "mods" / "G.A.M.M.A. Fixes" / "gamedata" / "fix.ltx"
            target.unlink()
            target.symlink_to(outside)
            files, _ = gamma_repo_files(str(gamma), repo_branches(CliProfile()))
            result = restore_repo_files(str(gamma), files, list(files))
            self.assertEqual(result.failed, list(files))
            self.assertEqual(outside.read_text(), "keep")


class ReextractFromCacheTests(unittest.TestCase):
    def test_resume_keys_follow_the_cli_grouping(self):
        from commander_gui.repair import parse_modpack_records, resume_keys

        rows = [
            "https://www.moddb.com/a\t0\tp1\tShared A\thttps://m/a\tshared.7z\t" + "a" * 32,
            "https://www.moddb.com/b\t0\tp2\tShared B\thttps://m/b\tshared.7z\t" + "a" * 32,
            "https://github.com/o/repo/archive/main.zip\t0\tp\tGit Mod\t\t\t",
            "https://www.moddb.com/c\t0\t\tNo Zip\thttps://m/c\t\t",
        ]
        records = parse_modpack_records("\n".join(rows))
        keys = resume_keys(records)
        # Both share one archive: the CLI keys the group by its first addon.
        self.assertEqual(keys["1- Shared A p1"], "Shared A | shared.7z")
        self.assertEqual(keys["2- Shared B p2"], "Shared A | shared.7z")
        self.assertEqual(len(keys), 2)  # no GitHub, no ZipName-less addon

    def test_an_interrupted_installs_resume_file_is_never_overwritten(self):
        from commander_gui.repair import write_reextract_resume

        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(write_reextract_resume(tmp, ["A | a.7z"]))
            text = (Path(tmp) / "resume.txt").read_text()
            self.assertIn("# command: full-install\n", text)
            self.assertIn("Extract\tA | a.7z\n", text)
            self.assertFalse(write_reextract_resume(tmp, ["B | b.7z"]))
            self.assertEqual((Path(tmp) / "resume.txt").read_text(), text)

    def test_files_from_gammas_repositories_are_left_to_the_git_check(self):
        from commander_gui.repair import ModPackRecord, classify_problems

        scan = Md5ScanResult(changed=["mods/1- A x/f.txt", "mods/G Fix/g.ltx"])
        record = ModPackRecord(1, "A", "x", "https://moddb", "", "a.7z", "", "")
        plan = classify_problems(
            scan,
            {record.folder_name: record},
            extra_broken_folders=["G Empty"],
            repo_owned={"mods/G Fix/g.ltx", "mods/G Empty/e.ltx"},
        )
        self.assertEqual(plan.repairable, ["1- A x"])
        self.assertEqual(plan.unrepairable, [])


def _page(tmp):
    from PySide6.QtWidgets import QApplication

    from commander_gui.ui.install_page import InstallPage

    QApplication.instance() or QApplication([])
    profile = CliProfile(
        active=True,
        profile_name="Test",
        anomaly=str(Path(tmp) / "anomaly"),
        gamma=str(Path(tmp) / "gamma"),
        cache=str(Path(tmp) / "cache"),
        mo2_profile="G.A.M.M.A",
    )

    class FakeWindow:
        settings = CliSettings(profiles=[profile])
        install_busy = False

        def refresh_settings(self):
            pass

        def set_install_busy(self, *a, **kw):
            pass

        def statusBar(self):
            return Mock()

    return InstallPage(FakeWindow()), profile


class VerifyPageTests(unittest.TestCase):
    def test_failed_anomaly_check_never_finishes_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _ = _page(tmp)
            page._verify_runner = Mock(was_cancelled=False)
            with patch.object(page, "_start_gamma_verify"):
                page._on_anomaly_verify_finished(1, "Error: anomaly not found")
            page._gamma_skipped = True
            with patch.object(page, "_finish_verify") as finish:
                page._conclude_verify()
            self.assertFalse(finish.call_args.kwargs["ok"])
            self.assertEqual(finish.call_args.kwargs["verdict"], "attention")

    def test_post_repair_problems_keep_the_snapshot_as_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, profile = _page(tmp)
            gamma = Path(profile.gamma)
            (gamma / "mods" / "1- A").mkdir(parents=True)
            (gamma / "mods" / "1- A" / "f.txt").write_text("v1")
            (gamma / "profiles" / "G.A.M.M.A").mkdir(parents=True)
            (gamma / "profiles" / "G.A.M.M.A" / "modlist.txt").write_text("+1- A\n")
            scan_mods_md5(str(gamma))
            manifest = gamma / "gamma-md5.txt"
            os.utime(manifest, (time.time() - 100, time.time() - 100))
            (gamma / "mods" / "1- A" / "f.txt").write_text("still broken")
            listing = gamma / "profiles" / "G.A.M.M.A" / "modpack_maker_list.txt"
            listing.write_text("x")
            page._scan_cancel = None
            post, _presence, _plan = page._run_post_scan(lambda *_a: None)
            self.assertEqual(post.changed, ["mods/1- A/f.txt"])
            self.assertGreater(manifest.stat().st_mtime, listing.stat().st_mtime - 1)
            self.assertFalse(scan_mods_md5(str(gamma)).stale)

    def test_a_clean_post_repair_scan_hashes_the_mods_only_once(self):
        from commander_gui import integrity

        with tempfile.TemporaryDirectory() as tmp:
            page, profile = _page(tmp)
            gamma = Path(profile.gamma)
            (gamma / "mods" / "1- A").mkdir(parents=True)
            (gamma / "mods" / "1- A" / "f.txt").write_text("v1")
            (gamma / "profiles" / "G.A.M.M.A").mkdir(parents=True)
            (gamma / "profiles" / "G.A.M.M.A" / "modlist.txt").write_text("+1- A\n")
            scan_mods_md5(str(gamma))
            (gamma / "mods" / "1- A" / "f.txt").write_text("repaired")
            page._scan_cancel = None
            hashed = []
            real_md5 = integrity._md5_file
            with (
                patch.object(integrity, "_md5_file", side_effect=lambda p: hashed.append(p) or real_md5(p)),
                patch("commander_gui.ui.install_page._remaining_problems", return_value=0),
                patch("commander_gui.ui.install_page.verify_gamma"),
            ):
                page._run_post_scan(lambda *_a: None)
            self.assertEqual(len(hashed), 1)
            self.assertEqual(scan_mods_md5(str(gamma)).changed, [])

    def test_repair_install_keeps_mo2_settings_and_drops_them_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _ = _page(tmp)
            page._anomaly_recheck_done = False
            with (
                patch("commander_gui.ui.install_page.CommandRunner"),
                patch("commander_gui.ui.install_page.capture") as capture,
                patch("commander_gui.ui.install_page.BackgroundTask", InlineTask),
            ):
                page._start_repair_install()
            capture.assert_called_once()
            self.assertIsNone(capture.return_value.modlist)
            with (
                patch("commander_gui.ui.install_page.restore") as restore,
                patch("commander_gui.ui.install_page.discard") as discard,
            ):
                page._restore_user_modlist(succeeded=False)
            discard.assert_called_once()
            restore.assert_not_called()

    def test_exes_are_never_kept_after_an_anomaly_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _ = _page(tmp)
            page.checkboxes["keep_engine_exes"].setChecked(True)
            page._anomaly_recheck_done = True
            with (
                patch("commander_gui.ui.install_page.CommandRunner"),
                patch("commander_gui.ui.install_page.capture") as capture,
                patch("commander_gui.ui.install_page.BackgroundTask", InlineTask),
            ):
                page._start_repair_install()
            self.assertFalse(capture.call_args.kwargs["keep_exes"])

    def test_a_verified_archive_is_unpacked_again_then_downloaded_if_skipped(self):
        from commander_gui.repair import (
            ModPackRecord,
            QuarantineItem,
            QuarantineRecord,
            RepairPlan,
        )

        with tempfile.TemporaryDirectory() as tmp:
            page, profile = _page(tmp)
            gamma = Path(profile.gamma)
            (gamma / "mods").mkdir(parents=True)
            record = ModPackRecord(1, "A", "x", "https://www.moddb.com/a", "", "a.7z", "f" * 32, "")
            page._verify_records = {record.folder_name: record}
            page._repair_records = {record.folder_name: record}
            page._repair_plan = RepairPlan(repairable=[record.folder_name])
            page._cache_archive_result = CacheArchiveVerifyResult(verified=["a.7z"])
            page._begin_repair_run()
            self.assertEqual(page._archive_reuse_keys(), {record.folder_name: "A | a.7z"})
            page._repair_reuse_keys = page._archive_reuse_keys()
            mod = gamma / "mods" / record.folder_name
            set_aside = QuarantineRecord(
                record.folder_name, [QuarantineItem(mod, gamma / "q" / "A", is_mod=True)]
            )
            (gamma / "q" / "A").mkdir(parents=True)
            (gamma / "q" / "A" / "f").write_text("old")
            with (
                patch("commander_gui.ui.install_page.CommandRunner") as runner,
                patch("commander_gui.ui.install_page.BackgroundTask", InlineTask),
            ):
                page._on_repair_quarantined([set_aside])
            self.assertIn("--resume", runner.call_args.args[0])
            self.assertTrue((gamma / "resume.txt").is_file())
            # The installer skipped it anyway: one more round, downloading.
            page._repair_runner = Mock(was_cancelled=False)
            with (
                patch("commander_gui.ui.install_page.cli_ok", return_value=True),
                patch.object(page, "_restore_user_modlist"),
                patch.object(page, "_start_gamma_repair_deletion") as again,
                patch("commander_gui.ui.install_page.BackgroundTask", InlineTask),
            ):
                page._on_repair_install_finished(0, "")
            again.assert_called_once()
            self.assertTrue(page._repair_retrying)
            self.assertFalse((gamma / "resume.txt").exists())
            self.assertEqual((mod / "f").read_text(), "old")  # old copy back meanwhile

    def test_result_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _ = _page(tmp)
            page._verify_anomaly_ok = True
            page._gamma_skipped = True
            with patch.object(page, "_finish_verify"):
                page._deliver_verdict()
            again, _ = _page(tmp)
            self.assertTrue("Healthy" in again.verify_pill.text())
            self.assertEqual(again.verify_tiles["anomaly"].state, "ok")


if __name__ == "__main__":
    unittest.main()
