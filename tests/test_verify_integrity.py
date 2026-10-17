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
            ):
                page._start_repair_install()
            self.assertFalse(capture.call_args.kwargs["keep_exes"])

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
