import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from commander_gui import assistant_launcher, autostart, gui_settings, network
from commander_gui.assistant_launcher import assistant_command, launch_assistant
from commander_gui.atomic import write_text
from commander_gui.dependencies import (
    check_umu,
    detect_package_manager,
    install_command,
)
from commander_gui.diagnostics import _redact
from commander_gui.integrity import (
    CacheArchiveVerifyResult,
    scan_mods_md5,
    verify_cache_archives,
)
from commander_gui.launcher import (
    LaunchError,
    ProcessGroupRegistry,
    Runner,
    build_command,
    ensure_runner_prefix,
    launch_detached,
    resolve_runner,
    wine_prefix_for,
)
from commander_gui.log_dump import _safe_archive_part, build_log_dump
from commander_gui.modlist import (
    add_category,
    add_mod,
    entries,
    grouped,
    install_conflict,
    rename_category,
    rename_mod,
    reorder_to_original,
    set_status_at,
)
from commander_gui.network import read_response_bytes
from commander_gui.proton_installer import (
    _safe_extract,
    fetch_ge_proton_releases,
    install_proton,
)
from commander_gui.repair import ModPackRecord
from commander_gui.self_update import (
    CommanderSelfUpdateError,
    CommanderUpdateAssetNotFoundError,
    commander_appimage_path,
    commander_update_asset_url,
    download_and_install_commander_update,
    download_commander_update,
    install_commander_update,
)
from commander_gui.settings import CliProfile, CliSettings, cli_ok, load_settings
from commander_gui.ui import common
from commander_gui.ui.common import (
    BackgroundTask,
    StreamTask,
    aggregate_progress_value,
    count_active_mods,
    display_state,
    normalize_path,
    progress_value,
    single_file_progress,
)
from commander_gui.ui.install_page import (
    _dependencies_progress,
    _resume_state_matches,
    _winetricks_progress,
)
from commander_gui.ui.play_page import _is_hidden_launch_target
from commander_gui.ui.system_check_page import (
    _check_tool,
    _installation_checks,
    _winetricks_checks,
)
from commander_gui.ui.utilities_page import (
    UtilitiesPage,
    _cache_preflight_summary_html,
    _copy_dir_tree,
    _move_folders,
    _rewrite_mo2_ini_paths,
    _safe_wipe_path,
    _save_moved_profile,
)
from commander_gui.updates import (
    diff_records,
    latest_version_human,
    local_modpack_records,
    remote_version,
)
from commander_gui.winetricks import (
    WINETRICKS_VERBS,
    umu_binary,
    umu_install_command,
    winetricks_install_command,
)


class AssistantLauncherTests(unittest.TestCase):
    def test_bundled_assistant_command_uses_payload_python(self):
        command, cwd = assistant_command()
        self.assertEqual(command[:2], [sys.executable, "-m"])
        self.assertEqual(command[2], "assistant")
        self.assertTrue((cwd / "assistant" / "__main__.py").is_file())

    @patch("commander_gui.assistant_launcher.subprocess.Popen")
    def test_launch_assistant_passes_archive_as_argument(self, popen):
        archive = Path(tempfile.gettempdir()) / "dump with spaces.zip"
        launch_assistant(archive)
        command = popen.call_args.args[0]
        self.assertEqual(command[-1], str(archive.resolve()))
        self.assertEqual(command[1:3], ["-m", "assistant"])


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Initialize QApplication once for all tests that need Qt widgets."""
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_diagnostics_redacts_common_secret_forms(self):
        text = (
            '"api_key": "json-secret"\n'
            "token=plain-secret\n"
            "Authorization: Bearer header-secret\n"
            "https://user:password@example.com/download\n"
        )
        redacted = _redact(text)
        self.assertNotIn("json-secret", redacted)
        self.assertNotIn("plain-secret", redacted)
        self.assertNotIn("header-secret", redacted)
        self.assertNotIn("user:password@", redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 4)

    def test_diagnostics_redacts_underscore_prefixed_secret_names(self):
        """Regression test: Python's `\\b` treats `_` as a word char, so

        `\\btoken\\b` never matched "token" inside GITHUB_TOKEN - a very
        common env-var naming convention. The redaction regexes use a
        custom left-boundary instead; this locks that in and also checks
        the fix didn't start over-matching ordinary identifiers like
        "my_tokenizer".
        """
        text = "GITHUB_TOKEN=ghp_supersecret\nSTEAM_API_KEY=abc123\nmy_tokenizer=not-a-secret\n"
        redacted = _redact(text)
        self.assertNotIn("ghp_supersecret", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertIn("not-a-secret", redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 2)

    def test_save_gui_settings_never_backs_up_a_corrupted_file(self):
        """Regression test: the last-known-good backup must only ever be

        refreshed from a file that is actually valid JSON. Copying a
        corrupted current file over the backup would destroy the one thing
        `load_gui_settings()` falls back to when corruption strikes,
        leaving no way to recover on the next load.
        """
        from commander_gui.config import gui_settings_path

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            # First save creates the file; the backup only appears once a
            # *second* save finds an existing, valid file to back up.
            gui_settings.save_gui_settings(theme="midnight")
            gui_settings.save_gui_settings(theme="midnight")
            path = gui_settings_path()
            backup = path.with_suffix(".json.last-good")
            self.assertTrue(backup.exists())
            good_backup_contents = backup.read_text(encoding="utf-8")

            # Corrupt the live file, then save again - the backup must be
            # left untouched (not overwritten with the corrupted bytes).
            path.write_text("{not valid json", encoding="utf-8")
            gui_settings.save_gui_settings(theme="gamma")
            self.assertEqual(backup.read_text(encoding="utf-8"), good_backup_contents)

            # And loading while the live file is still corrupt must recover
            # the last-known-good state from that intact backup rather than
            # silently resetting to defaults.
            path.write_text("{still not valid", encoding="utf-8")
            gui_settings._cache = None
            recovered = gui_settings.load_gui_settings()
            self.assertEqual(recovered["theme"], "midnight")

    def test_assistant_report_redacts_json_and_url_and_underscored_secrets(self):
        """Regression test for the Assistant sub-tool's own separate redactor

        (`assistant/report.py`, distinct from `commander_gui/diagnostics.py`)
        - it had the same `\\b`-vs-underscore gap plus two more gaps that
        didn't exist in the main app's redactor: a JSON `"key": "value"`
        pair and a `user:pass@host` URL.
        """
        from assistant.report import _redact

        text = (
            '{"password": "json-secret"}\n'
            "GITHUB_TOKEN=env-secret\n"
            "https://user:url-secret@example.com/path\n"
        )
        redacted = _redact(text)
        self.assertNotIn("json-secret", redacted)
        self.assertNotIn("env-secret", redacted)
        self.assertNotIn("url-secret", redacted)

    def test_dump_archive_rejects_an_entry_whose_content_disagrees_with_its_declared_size(
        self,
    ):
        """Regression test: opening a dump reads each text entry in bounded

        chunks (rather than one ``zf.read()`` call) so a crafted entry
        whose declared ``file_size`` disagrees with what it actually
        decompresses to is rejected instead of either silently truncating,
        hanging, or blowing up memory. Whether the rejection comes from our
        own running-byte-count check or from zipfile's own CRC bookkeeping,
        it must surface as a clean ``DumpError`` - not an uncaught
        exception - and must not be the generic "Could not read..." wrapper
        swallowing a *deliberately raised* ``DumpError`` (that wrapper only
        applies to `_ZIP_READ_ERRORS`, and ``DumpError`` subclasses
        ``ValueError`` which is itself one of those errors, so the guard
        that keeps the two from colliding is what's under test here).
        """
        import zipfile

        from assistant.dump import DumpArchive, DumpError

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "dump.zip"
            payload = b"A" * 10_000
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("commander/big.log", payload)

            real_infolist = zipfile.ZipFile.infolist

            def lying_infolist(self):
                infos = real_infolist(self)
                for info in infos:
                    if info.filename == "commander/big.log":
                        info.file_size = 100  # lies: claims far smaller than actual
                return infos

            with (
                patch.object(zipfile.ZipFile, "infolist", lying_infolist),
                self.assertRaises(DumpError) as ctx,
            ):
                DumpArchive.open(zip_path)
            self.assertIn("big.log", str(ctx.exception))

    def test_dump_archive_opens_a_well_formed_dump_normally(self):
        import zipfile

        from assistant.dump import DumpArchive

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "dump.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("MANIFEST.txt", "[ok] commander/launcher.log (12 bytes)")
                zf.writestr("commander/launcher.log", "hello world\n")

            archive = DumpArchive.open(zip_path)
            self.assertEqual(archive.manifest_text, "[ok] commander/launcher.log (12 bytes)")
            launcher = next(f for f in archive.files if f.arcname == "commander/launcher.log")
            self.assertEqual(launcher.text, "hello world\n")

    def test_raw_repo_url_falls_back_to_stalker_gamma_for_an_empty_repo_url(self):
        """Regression test: ``str.split("/")`` always returns at least one

        element even for `""`, so the old `len(parts) >= 1` guard could
        never be false and the "Stalker_GAMMA" fallback was dead code - an
        emptied repo URL produced a URL with an empty repo segment
        (".../Grokitach//refs/heads/...") instead of ever falling back.
        """
        from commander_gui.updates import _raw_repo_url

        class FakeProfile:
            stalker_gamma_repo_url = ""
            stalker_gamma_repo_branch = ""

        url = _raw_repo_url(FakeProfile(), "x.txt")
        self.assertEqual(
            url,
            "https://raw.githubusercontent.com/Grokitach/Stalker_GAMMA/refs/heads/main/x.txt",
        )

    def test_profiles_page_rejects_two_profiles_sharing_the_same_install_folders(self):
        """Regression test: two differently-named profiles pointing at the

        identical Anomaly/GAMMA/cache folders would alias the same on-disk
        install - switching "active" between them, editing mods under one,
        or an incomplete-install warning tracked for one would silently
        affect the other too. Saving a new profile onto an existing
        profile's exact paths must be blocked, the same way a name
        collision already is.
        """
        from commander_gui.ui.profiles_page import ProfilesPage

        existing = CliProfile(
            active=True,
            profile_name="Existing",
            anomaly="/tmp/shared-anomaly",
            gamma="/tmp/shared-gamma",
            cache="/tmp/shared-cache",
            mo2_profile="G.A.M.M.A",
        )

        class FakeWindow:
            settings = CliSettings(profiles=[existing])
            install_busy = False

            def refresh_settings(self):
                pass

        page = ProfilesPage(FakeWindow())
        page._new_profile()
        page.name_edit.setText("Different Name")
        page.anomaly_edit.setText(existing.anomaly)
        page.gamma_edit.setText(existing.gamma)
        page.cache_edit.setText(existing.cache)

        with patch("commander_gui.ui.profiles_page.QMessageBox.warning") as mock_warn:
            page._save_or_create()

        mock_warn.assert_called_once()
        self.assertEqual(len(page.settings.profiles), 1)

    def test_tr_survives_a_translation_with_mismatched_placeholders(self):
        """Regression test: a translation whose placeholders don't exactly

        match the English source (a typo'd `{name}`, a missing one) must
        not crash the caller - `tr()` falls back to formatting the English
        source text itself, and if even that fails, returns the raw
        (unformatted) translated string rather than raising.
        """
        from commander_gui import i18n

        with patch.dict(
            i18n._TRANSLATIONS,
            {"fr": {"Hello {name}": "Bonjour {nom}"}},  # mismatched placeholder
        ):
            previous = i18n.active_language()
            i18n.set_active_language("fr")
            try:
                result = i18n.tr("Hello {name}", name="World")
            finally:
                i18n.set_active_language(previous)
            # Falls back to formatting the English source successfully.
            self.assertEqual(result, "Hello World")

    def test_collapse_findings_uses_dedup_key_not_the_truncated_title(self):
        """Regression test: an analyzer that truncates its displayed

        `title` for UI brevity must dedup on a separate, untruncated
        `dedup_key` - deduping on the truncated `title` itself would
        silently merge two genuinely different messages that happen to
        share the same truncated prefix, discarding the second one's
        detail.
        """
        from assistant.findings import (
            CATEGORY_LAUNCHER,
            Finding,
            Severity,
            collapse_findings,
        )

        distinct = collapse_findings(
            [
                Finding(
                    severity=Severity.WARNING,
                    category=CATEGORY_LAUNCHER,
                    title="Error: something went wrong with the fir...",
                    arcname="commander/launcher.log",
                    dedup_key="Error: something went wrong with the first thing",
                ),
                Finding(
                    severity=Severity.WARNING,
                    category=CATEGORY_LAUNCHER,
                    title="Error: something went wrong with the fir...",
                    arcname="commander/launcher.log",
                    dedup_key="Error: something went wrong with the fireplace",
                ),
            ]
        )
        self.assertEqual(len(distinct), 2)

        true_duplicates = collapse_findings(
            [
                Finding(
                    severity=Severity.WARNING,
                    category=CATEGORY_LAUNCHER,
                    title="Error: same thing",
                    arcname="commander/launcher.log",
                    dedup_key="Error: same thing",
                ),
                Finding(
                    severity=Severity.WARNING,
                    category=CATEGORY_LAUNCHER,
                    title="Error: same thing",
                    arcname="commander/launcher.log",
                    dedup_key="Error: same thing",
                ),
            ]
        )
        self.assertEqual(len(true_duplicates), 1)
        self.assertEqual(true_duplicates[0].count, 2)

    def test_desktop_exec_quotes_all_reserved_desktop_entry_characters(self):
        """Regression test: the Desktop Entry spec reserves more than just

        whitespace/quotes/backslash in an unquoted Exec= argument - a path
        containing "(", "&", "$", etc. must be quoted too, not just spaces.
        """
        from commander_gui.autostart import _desktop_exec

        self.assertEqual(
            _desktop_exec(["/bin/app", "/path/with (parens)/run.sh"]),
            '/bin/app "/path/with (parens)/run.sh"',
        )
        self.assertEqual(
            _desktop_exec(["/bin/app", "cost$5&more"]),
            '/bin/app "cost\\$5&more"',
        )
        # Plain arguments with nothing reserved stay unquoted.
        self.assertEqual(
            _desktop_exec(["/bin/app", "--flag"]),
            "/bin/app --flag",
        )

    def test_desktop_exec_quotes_a_single_quote_in_the_path(self):
        """Regression test: a literal "'" is a Desktop Entry reserved char.

        Left unquoted, glib's g_shell_parse_argv (used by many desktop
        environments to launch autostart entries) treats a bare "'" as
        opening shell-style quoting, so an Exec= value with a stray quote
        fails to parse and autostart silently does nothing for that path.
        A single quote does not need its own backslash escape inside the
        double quotes though - only the value must be wrapped.
        """
        from commander_gui.autostart import _desktop_exec

        self.assertEqual(
            _desktop_exec(["/bin/app", "/home/o'brien/App.AppImage"]),
            '/bin/app "/home/o\'brien/App.AppImage"',
        )

    def test_log_dump_rejects_unsafe_virtual_path_parts(self):
        self.assertEqual(_safe_archive_part("commander"), "commander")
        for value in ("../escape", "nested/name", "/absolute", "..secret"):
            with self.assertRaises(ValueError):
                _safe_archive_part(value)

    def test_package_manager_falls_back_to_available_executable(self):
        with (
            patch(
                "commander_gui.dependencies._read_os_release",
                return_value={"ID": "unknown", "ID_LIKE": ""},
            ),
            patch(
                "commander_gui.dependencies.shutil.which",
                side_effect=lambda command: "/sbin/apk" if command == "apk" else None,
            ),
        ):
            self.assertEqual(detect_package_manager(), "apk")

    def test_unknown_package_manager_does_not_get_apt_command(self):
        with (
            patch(
                "commander_gui.dependencies._read_os_release",
                return_value={"ID": "unknown", "ID_LIKE": ""},
            ),
            patch("commander_gui.dependencies.shutil.which", return_value=None),
        ):
            self.assertEqual(
                install_command("wine"), "Install 'wine' with your package manager"
            )

    def test_mod_progress_uses_cli_completed_count(self):
        self.assertEqual(progress_value(0, 500), 0)
        self.assertEqual(progress_value(125, 500), 25)
        self.assertEqual(progress_value(500, 500), 100)

    def test_mod_progress_handles_invalid_or_out_of_range_counts(self):
        self.assertEqual(progress_value(1, 0), 0)
        self.assertEqual(progress_value(-1, 10), 0)
        self.assertEqual(progress_value(11, 10), 100)

    def test_single_file_progress_advances_across_install_stages(self):
        # Download tracks the true reported percent (bar == text below).
        self.assertEqual(single_file_progress("Download", 0.5), 50)
        self.assertEqual(single_file_progress("Download", 1.0), 100)
        self.assertEqual(single_file_progress("Extract", 0), 50)
        self.assertEqual(single_file_progress("Expand", 0), 85)
        self.assertEqual(single_file_progress("Check MD5", 0), 95)
        self.assertEqual(single_file_progress("Skipped", 0), 100)

    def test_display_state_prefers_active_operation_over_filesystem(self):
        self.assertEqual(display_state(False, "anomaly", "anomaly"), "installing")
        self.assertEqual(display_state(True, "gamma", "gamma"), "installing")
        self.assertIs(display_state(True, None, "anomaly"), True)
        # A different active operation must not mask this target's state.
        self.assertIs(display_state(False, "gamma", "anomaly"), False)

    def test_background_task_shutdown_requests_cooperative_cancellation(self):
        task = BackgroundTask(lambda: None)
        thread = Mock()
        thread.wait.return_value = True
        task._thread = thread

        task.shutdown(timeout_ms=123)

        self.assertTrue(task.cancel_event.is_set())
        thread.wait.assert_called_once_with(123)

    def test_background_task_suppresses_callbacks_after_shutdown_begins(self):
        task = BackgroundTask(lambda: None)
        results = []
        errors = []
        task.result.connect(results.append)
        task.error.connect(errors.append)

        with patch.object(common, "_SHUTTING_DOWN", False):
            common.begin_shutdown()
            task._on_result("late result")
            task._on_error("late error")

        self.assertEqual(results, [])
        self.assertEqual(errors, [])

    def test_stream_task_suppresses_late_callbacks_after_shutdown_begins(self):
        task = StreamTask(lambda _report: None)
        lines = []
        results = []
        errors = []
        task.line.connect(lines.append)
        task.result.connect(results.append)
        task.error.connect(errors.append)

        with patch.object(common, "_SHUTTING_DOWN", False):
            common.begin_shutdown()
            task._on_line("late line")
            task._on_result("late result")
            task._on_error("late error")

        self.assertEqual(lines, [])
        self.assertEqual(results, [])
        self.assertEqual(errors, [])

    def _assert_detached_when_outliving_shutdown(self, make_task):
        """Shared body for the two task classes' abandonment behaviour.

        shutdown()'s wait is bounded, and a plain Python callable cannot be
        force-killed - so it can return with the worker thread still running
        (dir_size() over a 100k-file GAMMA tree ignores cancellation entirely
        and easily outlives switch_language()'s 2s budget). The task is
        parented to the page that started it and its QThread is parented to
        the task, so switch_language() deleting the whole central widget used
        to destroy a *running* QThread, which Qt answers with qFatal - the
        whole app died on a language switch. The task must be re-parented out
        of that chain instead, and must drop its late result rather than
        delivering it to the destroyed page's handlers.
        """
        import time as _time

        from PySide6.QtWidgets import QApplication, QWidget

        QApplication.instance() or QApplication([])
        page = QWidget()
        started = threading.Event()

        def slow(*_args):
            started.set()
            _time.sleep(0.5)
            return "late result"

        task = make_task(slow, page)
        results = []
        task.result.connect(results.append)
        task.start()
        self.assertTrue(started.wait(5))

        task.shutdown(timeout_ms=1)

        self.assertTrue(task._thread.isRunning())
        self.assertIsNone(task.parent())
        # QThread.wait() blocks without running this (main) thread's event
        # loop, but the worker's `finished` signal -> `thread.quit()` is a
        # queued cross-thread connection that needs that loop pumped to be
        # delivered - a real app's own app.exec() does this for free, so
        # pump it manually here instead of a single blocking wait().
        deadline = _time.monotonic() + 5
        finished = False
        while _time.monotonic() < deadline:
            QApplication.processEvents()
            if task._thread.wait(50):
                finished = True
                break
        self.assertTrue(finished)
        QApplication.processEvents()
        self.assertEqual(results, [])

    def test_background_task_outliving_shutdown_is_detached_from_its_page(self):
        self._assert_detached_when_outliving_shutdown(
            lambda fn, page: BackgroundTask(fn, parent=page)
        )

    def test_stream_task_outliving_shutdown_is_detached_from_its_page(self):
        self._assert_detached_when_outliving_shutdown(
            lambda fn, page: StreamTask(fn, parent=page)
        )

    def test_full_install_args_include_skip_extract_flag_conditionally(self):
        from commander_gui.ui.install_page import _full_install_args

        self.assertEqual(
            _full_install_args(False, False, False, False), ["full-install"]
        )
        self.assertEqual(
            _full_install_args(True, True, True, True),
            [
                "full-install",
                "--minimal",
                "--preserve-user-settings",
                "--preserve-mcm-settings",
                "--skip-extract-on-hash-match",
            ],
        )

    def test_repair_install_args_always_preserve_user_content(self):
        """Verify & Repair must never touch user.ltx or MCM settings."""
        from commander_gui.ui.install_page import _repair_install_args

        args = _repair_install_args()
        self.assertIn("--skip-extract-on-hash-match", args)
        self.assertIn("--preserve-user-settings", args)
        self.assertIn("--preserve-mcm-settings", args)

    def test_verify_phase_value_maps_and_clamps(self):
        from commander_gui.ui.install_page import _verify_phase_value

        self.assertEqual(_verify_phase_value(15, 95, 0.0), 15)
        self.assertEqual(_verify_phase_value(15, 95, 0.5), 55)
        self.assertEqual(_verify_phase_value(15, 95, 1.0), 95)
        self.assertEqual(_verify_phase_value(15, 95, -1.0), 15)
        self.assertEqual(_verify_phase_value(15, 95, 2.0), 95)

    def test_gamma_verify_gate_skips_when_not_installed(self):
        from commander_gui.ui.install_page import (
            _GAMMA_NOT_INSTALLED,
            _gamma_verify_gate,
        )

        self.assertIsNone(_gamma_verify_gate(True))
        self.assertEqual(_gamma_verify_gate(False), _GAMMA_NOT_INSTALLED)

    def test_md5_redownload_note_detects_failed_cached_archive(self):
        """A Download after Check MD5 for the same archive must be explained."""
        from commander_gui.parsers import ProgressEvent
        from commander_gui.ui.install_page import _note_md5_redownload

        tracked: set[str] = set()
        check = ProgressEvent("Stalker Anomaly", "Check MD5", 1.0, 1, 1)
        self.assertIsNone(_note_md5_redownload(tracked, check))
        self.assertIn("Stalker Anomaly", tracked)
        # A later Extract means the cached archive was valid: no note.
        extract = ProgressEvent("Stalker Anomaly", "Extract", 0.0, 1, 1)
        self.assertIsNone(_note_md5_redownload(tracked, extract))
        self.assertNotIn("Stalker Anomaly", tracked)

    def test_md5_redownload_note_flags_re_download(self):
        from commander_gui.parsers import ProgressEvent
        from commander_gui.ui.install_page import _note_md5_redownload

        tracked: set[str] = set()
        _note_md5_redownload(
            tracked, ProgressEvent("Stalker Anomaly", "Check MD5", 1.0, 1, 1)
        )
        note = _note_md5_redownload(
            tracked, ProgressEvent("Stalker Anomaly", "Download", 0.0, 1, 1)
        )
        self.assertEqual(note, "Cached archive failed verification - re-downloading.")
        self.assertNotIn("Stalker Anomaly", tracked)

    def test_md5_redownload_note_ignores_first_seen_downloads(self):
        """A Download with no prior Check MD5 is normal (fresh fetch)."""
        from commander_gui.parsers import ProgressEvent
        from commander_gui.ui.install_page import _note_md5_redownload

        tracked: set[str] = set()
        note = _note_md5_redownload(
            tracked, ProgressEvent("Some Mod", "Download", 0.0, 1, 1)
        )
        self.assertIsNone(note)

    def test_looks_like_network_failure_matches_real_git_clone_failure(self):
        from commander_gui.ui.install_page import _looks_like_network_failure

        # Verbatim shape of a real captured install failure (git-clone/SSL
        # failure downloading the Stalker_GAMMA repo).
        real_failure = (
            "Install failed! Error downloading from Stalker Gamma Repo\n"
            "Exception Message: Error cloning repo\n"
            "Exception Message: SSL error: unknown error\n"
            "Stalker.Gamma.GammaInstallerServices.SpecialRepos.SpecialRepoException: "
            "Error downloading from Stalker Gamma Repo\n"
            " ---> LibGit2Sharp.LibGit2SharpException: SSL error: unknown error\n"
            "   at LibGit2Sharp.Core.Ensure.HandleError(Int32) + 0xea\n"
        )
        self.assertTrue(_looks_like_network_failure(real_failure))
        self.assertFalse(_looks_like_network_failure(""))
        self.assertFalse(
            _looks_like_network_failure("Archive extraction failed: bad CRC")
        )

    def test_install_failure_message_explains_network_failures(self):
        from commander_gui.ui.install_page import _install_failure_message

        network_output = "Exception Message: SSL error: unknown error\n42% [3/577]"
        summary, detail = _install_failure_message(1, network_output, None)
        self.assertIn("GitHub or network", summary)
        self.assertIn("Exit code: 1", detail)
        self.assertIn("SSL error", detail)

    def test_install_failure_message_falls_back_for_unrecognized_failures(self):
        from commander_gui.ui.install_page import _install_failure_message

        summary, _detail = _install_failure_message(
            1, "Archive extraction failed: bad CRC", None
        )
        self.assertNotIn("GitHub or network", summary)
        self.assertIn("stopped unexpectedly", summary)

    def test_install_failure_message_includes_resume_hint_and_strips_progress_spam(
        self,
    ):
        from commander_gui.ui.install_page import _install_failure_message

        output = (
            "[04:02:51] 12.3% [1/577]\n"
            "Exception Message: SSL error: unknown error\n"
            "[04:02:52] 12.4% [1/577]\n"
        )
        summary, detail = _install_failure_message(
            1, output, "Click Resume to continue."
        )
        self.assertIn("Click Resume to continue.", summary)
        self.assertNotIn("12.3%", detail)
        self.assertNotIn("12.4%", detail)
        self.assertIn("SSL error", detail)

    def test_install_failure_message_flags_special_repo_clone_failures(self):
        from commander_gui.ui.install_page import _install_failure_message

        ssl_output = (
            "Install failed! Error downloading from Stalker Gamma Repo\n"
            "Exception Message: SSL error: unknown error\n"
            "LibGit2Sharp.LibGit2SharpException: SSL error: unknown error\n"
        )
        summary, _detail = _install_failure_message(1, ssl_output, "")
        self.assertIn("always restarts from scratch", summary)
        # Stalker_GAMMA failing is not gamma_large_files_v2 failing - the
        # rate-limit note must not be misattributed to the wrong repo.
        self.assertNotIn("GitHub rate limit", summary)

        clone_output = (
            "Install failed! Error downloading from Gamma Large Files Repo\n"
            "Exception Message: Error cloning repo\n"
            "Exception Message: could not read from remote repository\n"
            "Stalker.Gamma.GammaInstallerServices.SpecialRepos."
            "SpecialRepoException\n"
            "LibGit2Sharp.LibGit2SharpException: could not read from remote "
            "repository\n"
        )
        summary, _detail = _install_failure_message(1, clone_output, "")
        self.assertIn("always restarts from scratch", summary)
        self.assertIn("GitHub rate limit", summary)

    def test_looks_like_gamma_large_files_failure_is_repo_specific(self):
        from commander_gui.ui.install_page import (
            _looks_like_gamma_large_files_failure,
        )

        self.assertTrue(
            _looks_like_gamma_large_files_failure(
                "Install failed! Error downloading from Gamma Large Files Repo"
            )
        )
        self.assertFalse(
            _looks_like_gamma_large_files_failure(
                "Install failed! Error downloading from Stalker Gamma Repo"
            )
        )
        self.assertFalse(_looks_like_gamma_large_files_failure(""))

    def test_should_auto_retry_for_any_of_the_4_large_repos_under_the_cap(self):
        """Regression test: auto-retry must cover all 4 of the GAMMA

        install pipeline's large/special git-cloned repos (confirmed via
        the CLI's own output strings), not just gamma_large_files_v2 -
        while still staying quiet for a genuinely unrelated failure.
        """
        from commander_gui.ui.install_page import _AUTO_RETRY_MAX, _should_auto_retry

        glf_output = "Install failed! Error downloading from Gamma Large Files Repo"
        unrelated_output = "Install failed! Some unrelated network timeout"

        # Not opted in.
        self.assertFalse(_should_auto_retry(False, 0, glf_output))
        # Opted in, but a genuinely unrelated failure - never auto-retries.
        self.assertFalse(_should_auto_retry(True, 0, unrelated_output))
        # Opted in, matching failure, under the cap.
        self.assertTrue(_should_auto_retry(True, 0, glf_output))
        self.assertTrue(_should_auto_retry(True, _AUTO_RETRY_MAX - 1, glf_output))
        # At/over the cap - stop, even if opted in and matching.
        self.assertFalse(_should_auto_retry(True, _AUTO_RETRY_MAX, glf_output))
        self.assertFalse(_should_auto_retry(True, _AUTO_RETRY_MAX + 1, glf_output))

        # All 4 large repos' own failure text must retry, each on its own.
        for phrase in (
            "Error downloading from Gamma Large Files Repo",
            "Error expanding files from Gamma Large Files Repo",
            "Error downloading from Gamma Setup Repo",
            "Error expanding from Gamma Setup Repo",
            "Error downloading from Stalker Gamma Repo",
            "Error expanding Stalker Gamma Repo",
            "Error downloading from Teivaz Anomaly Gunslinger Repo",
            "Error expanding files from Teivaz Anomaly Gunslinger Repo",
        ):
            self.assertTrue(
                _should_auto_retry(True, 0, f"Install failed! {phrase}"),
                msg=phrase,
            )

    def test_auto_retry_and_auto_continue_gamma_are_checked_by_default(self):
        """Regression test: both checkboxes must start checked on a fresh

        Install page - neither is persisted (see gui_settings.py), so
        this is the only place "on by default" is enforced.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
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
                install_operation = None

                def refresh_settings(self):
                    pass

                def set_install_busy(self, *a, **kw):
                    pass

                def statusBar(self):
                    return Mock()

            page = InstallPage(FakeWindow())
            self.assertTrue(page.checkboxes["auto_retry_large_files"].isChecked())
            self.assertTrue(
                page.anomaly_checkboxes["auto_continue_gamma"].isChecked()
            )
            # Every other checkbox stays off by default, unchanged.
            self.assertFalse(page.checkboxes["minimal"].isChecked())
            self.assertFalse(
                page.anomaly_checkboxes["verify_after_install"].isChecked()
            )
            self.assertEqual(
                page.anomaly_checkboxes["auto_continue_gamma"].text(),
                "Automatically install GAMMA after Anomaly",
            )

    def test_on_verify_line_relabels_known_gamma_overlay_files_as_ok(self):
        """Regression test for a real reported false positive: Verify

        Integrity flagged GAMMA's own 8 patched engine executables and
        its fsgame.ltx as CORRUPT (they always mismatch anomaly check's
        vanilla-only baseline by design) and would have offered to
        "repair" them back to vanilla, breaking the working install. A
        genuinely unrelated CORRUPT file must still be reported as such.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            anomaly_path = str(Path(tmp) / "anomaly")
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=anomaly_path,
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

            page = InstallPage(FakeWindow())
            overlay_files = [
                "fsgame.ltx",
                "bin/AnomalyDX8.exe",
                "bin/AnomalyDX8AVX.exe",
                "bin/AnomalyDX9.exe",
                "bin/AnomalyDX9AVX.exe",
                "bin/AnomalyDX10.exe",
                "bin/AnomalyDX10AVX.exe",
                "bin/AnomalyDX11.exe",
                "bin/AnomalyDX11AVX.exe",
            ]
            for rel in overlay_files:
                page._on_verify_line(f"{anomaly_path}/{rel}   | CORRUPT")
            page._on_verify_line(f"{anomaly_path}/gamedata/real_problem.xml | CORRUPT")
            page._on_verify_line(f"{anomaly_path}/default-44100.mhr | OK")

            self.assertEqual(page._verify_counts["OK"], len(overlay_files) + 1)
            self.assertEqual(page._verify_counts["CORRUPT"], 1)
            log_text = page.verify_progress.log.edit.toPlainText()
            self.assertIn("bin/AnomalyDX11AVX.exe   | OK (GAMMA-modified, expected)", log_text)
            self.assertIn("real_problem.xml | CORRUPT", log_text)

            # With ONLY the known-overlay files reported CORRUPT (no real
            # problem), the CORRUPT count must be zero - the exact
            # condition install_page.py's anomaly_needs_repair checks
            # (`counts["CORRUPT"] > 0 or counts["NOT FOUND"] > 0`) before
            # offering to "repair" (re-extract vanilla Anomaly, reverting
            # GAMMA's engine overlay).
            page2 = InstallPage(FakeWindow())
            for rel in overlay_files:
                page2._on_verify_line(f"{anomaly_path}/{rel}   | CORRUPT")
            self.assertEqual(page2._verify_counts["CORRUPT"], 0)
            self.assertEqual(page2._verify_counts["NOT FOUND"], 0)

    def test_on_full_finished_actually_wires_up_the_auto_retry_it_promises(self):
        """Regression test: the auto-retry checkbox/status message is only

        half the feature - ``_on_full_finished`` itself has to increment
        ``_auto_retry_count``, chain into ``_start_full_install(skip_confirm=
        True, _is_auto_retry=True)`` on a matching gamma_large_files_v2
        failure, and give up (popping the normal failure dialog) once
        ``_AUTO_RETRY_MAX`` is hit. Nothing in the suite exercised
        ``_on_full_finished``/``_is_auto_retry`` directly before this.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.install_page import _AUTO_RETRY_MAX, InstallPage

        QApplication.instance() or QApplication([])
        glf_output = "Install failed! Error downloading from Gamma Large Files Repo"

        with tempfile.TemporaryDirectory() as tmp:
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
                install_busy = True
                install_operation = "gamma"

                def refresh_settings(self):
                    pass

                def set_install_busy(self, *a, **kw):
                    pass

                def statusBar(self):
                    return self._bar

                _bar = Mock()

            page = InstallPage(FakeWindow())
            page.checkboxes["auto_retry_large_files"].setChecked(True)
            # Simulates a run started with explicit overrides (e.g. from
            # the Utilities GAMMA Reset dialog) distinct from the Install
            # page's own (unrelated, unchecked) preserve checkboxes - the
            # retry below must reuse these, not silently fall back to the
            # checkboxes. See _active_preserve_user/_active_preserve_mcm.
            page._active_preserve_user = True
            page._active_preserve_mcm = True

            with patch.object(page, "_start_full_install") as mock_retry:
                page._on_full_finished(1, glf_output)

            self.assertEqual(page._auto_retry_count, 1)
            mock_retry.assert_called_once_with(
                skip_confirm=True,
                preserve_user=True,
                preserve_mcm=True,
                _is_auto_retry=True,
            )

            # At the cap: no further retry call, count resets, and the
            # normal failure popup fires with a "gave up" hint instead.
            page._auto_retry_count = _AUTO_RETRY_MAX
            with (
                patch.object(page, "_start_full_install") as mock_retry_capped,
                patch.object(page, "_show_error_popup") as mock_popup,
            ):
                page._on_full_finished(1, glf_output)

            mock_retry_capped.assert_not_called()
            self.assertEqual(page._auto_retry_count, 0)
            mock_popup.assert_called_once()
            self.assertIn("gave up", mock_popup.call_args.kwargs["resume_hint"])

    def test_install_failure_message_omits_special_repo_note_for_regular_failures(
        self,
    ):
        from commander_gui.ui.install_page import _install_failure_message

        output = (
            "[04:02:51] 12.3% [1/577]\n"
            "Exception Message: SSL error: unknown error\n"
        )
        summary, _detail = _install_failure_message(1, output, "")
        self.assertNotIn("always restarts from scratch", summary)

    def test_gamma_progress_uses_per_archive_average(self):
        """GAMMA overall bar uses the CLI counter and current item fraction."""

        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        # The current item contributes fractionally instead of making the bar
        # wait for the archive to finish.
        area.on_line("[00:00:01] Mod A | Download | 50% | [1/10]")
        self.assertEqual(area.bar.value(), 5)  # 0.5/10 * 100
        area.on_line("[00:00:02] Mod B | Download | 80% | [2/10]")
        self.assertEqual(area.bar.value(), 18)  # (1 + 0.8)/10 * 100 = 18

    def test_aggregate_progress_value_is_bounded(self):
        self.assertEqual(aggregate_progress_value(1, 10, 0.5), 5)
        self.assertEqual(aggregate_progress_value(10, 10, 1.0), 100)
        self.assertEqual(aggregate_progress_value(99, 10, 2.0), 100)
        self.assertEqual(aggregate_progress_value(-1, 10, -1.0), 0)

    def test_gamma_resume_state_is_bound_to_install_paths(self):
        profile = CliProfile(
            profile_name="gamma",
            anomaly="/games/anomaly",
            gamma="/games/gamma",
            cache="/games/cache",
        )
        state = {
            "profile": "gamma",
            "anomaly": "/games/anomaly",
            "gamma": "/games/gamma",
            "cache": "/games/cache",
        }
        self.assertTrue(_resume_state_matches(state, profile))
        state["gamma"] = "/other/gamma"
        self.assertFalse(_resume_state_matches(state, profile))

    def test_gamma_resume_state_survives_a_profile_rename(self):
        """Regression test: renaming a profile with a pending failed

        install must not make its "incomplete" warning silently vanish -
        profile_name is a renameable label, not a stable identity, so the
        match must be based on the install's actual location only.
        """
        state = {
            "profile": "OldName",
            "anomaly": "/games/anomaly",
            "gamma": "/games/gamma",
            "cache": "/games/cache",
        }
        renamed = CliProfile(
            profile_name="NewName",
            anomaly="/games/anomaly",
            gamma="/games/gamma",
            cache="/games/cache",
        )
        self.assertTrue(_resume_state_matches(state, renamed))

    def test_gamma_progress_terminal_states_pin_at_100(self):
        """Bar tracks the CLI's own [done/total] counter across mixed events."""

        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        # Mod A completes download
        area.on_line("[00:00:01] Mod A | Download | 100% | [1/4]")
        # Extract event for the same archive, still counter-driven
        area.on_line("[00:00:02] Mod A | Extract | 0% | [1/4]")
        # Skipped archive advances the counter
        area.on_line("[00:00:03] Mod B | Skipped | 100% | [2/4]")
        # Bar reflects the CLI's own counter (aggregate_progress_value()),
        # not any locally-tracked per-archive state.
        self.assertEqual(area.bar.value(), 50)  # 2/4 * 100

    def test_aggregate_progress_value_matches_old_formula_before_heavy_activity(
        self,
    ):
        """Unchanged flat item-count math until a heavy repo has reported."""
        self.assertEqual(aggregate_progress_value(574, 577, 1.0), 99)
        self.assertEqual(
            aggregate_progress_value(
                574, 577, 1.0, name="Mod A", operation="Skipped"
            ),
            99,
        )

    def test_gamma_progress_reserves_a_share_for_heavy_special_repos(self):
        """The 3 git-cloned special repos get a fixed share of the bar

        instead of racing to ~99% on item count alone - regression test for
        the real-world report that the bar shoots to ~99% quickly then
        stalls for most of the install, because Stalker_GAMMA/gamma_setup/
        gamma_large_files_v2 take the bulk of wall-clock time despite being
        only 3 of ~577 total items. Real captured logs show these 3 start
        cloning almost immediately, so this feeds them in near the start -
        matching real CLI behavior - interleaved with ordinary archives
        completing well before the heavy repos do.
        """
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        # All 3 heavy repos start cloning right away, at 0%.
        area.on_line("[00:00:01] stalker_gamma | Download | 0% | [1/577]")
        area.on_line("[00:00:02] gamma_setup | Download | 0% | [1/577]")
        area.on_line(
            "[00:00:03] gamma_large_files_v2 | Download | 0% | [1/577]"
        )
        self.assertEqual(area.bar.value(), 0)

        # Hundreds of ordinary archives finish while the heavy repos are
        # still cloning - the bar must not race ahead to ~99%.
        area.on_line("[00:00:04] Mod A | Skipped | 100% | [400/577]")
        area.on_line("[00:00:05] Mod B | Skipped | 100% | [574/577]")
        self.assertEqual(area.bar.value(), 50)

        # As each heavy repo finishes (Download -> Extract), the bar climbs
        # smoothly instead of jumping straight from ~99% to 100% at the end.
        area.on_line("[00:00:06] stalker_gamma | Extract | 100% | [575/577]")
        area.on_line("[00:00:07] gamma_setup | Extract | 100% | [576/577]")
        mid_value = area.bar.value()
        self.assertLess(mid_value, 100)
        area.on_line(
            "[00:00:08] gamma_large_files_v2 | Extract | 100% | [577/577]"
        )
        self.assertEqual(area.bar.value(), 100)

    def test_gamma_progress_table_hides_row_stuck_on_a_stale_line(self):
        """Regression test for the reported frozen-addon-row bug.

        An archive whose own last progress line never cleanly reached a
        terminal state (e.g. a "Check MD5" pass that doesn't show 100%)
        must still get hidden/force-completed once the CLI's own counter
        proves nothing but the heavy repos remain unfinished - instead of
        staying stuck showing that stale operation/percent forever.
        """
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        # 3 heavy repos + 2 ordinary archives = 5 total.
        area.on_line("[00:00:01] Mod A | Skipped | 100% | [1/5]")
        mod_a_row = area.table._rows["Mod A"]
        self.assertTrue(area.table.isRowHidden(mod_a_row))

        # Mod X's own last-ever line is a non-terminal "Check MD5 47%" -
        # but the CLI's own counter already says 2 of 5 items are done
        # (Mod A and, implicitly, Mod X itself) - only the 3 heavy repos
        # remain, so Mod X's stale row must be force-completed and hidden.
        area.on_line("[00:00:02] Mod X | Check MD5 | 47% | [2/5]")
        mod_x_row = area.table._rows["Mod X"]
        self.assertTrue(area.table.isRowHidden(mod_x_row))
        self.assertEqual(area.table.item(mod_x_row, 1).text(), "Complete")

    def test_gamma_progress_table_skipped_is_terminal_regardless_of_percent(
        self,
    ):
        """A Skipped event hides its row immediately even if percent < 100%."""
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        area.on_line("[00:00:01] Mod Y | Skipped | 42% | [1/10]")
        row = area.table._rows["Mod Y"]
        self.assertTrue(area.table.isRowHidden(row))

    def test_gamma_progress_table_keeps_in_progress_rows_visible(self):
        """An ordinary still-downloading row is not hidden prematurely."""
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        area.on_line("[00:00:01] Mod Z | Download | 50% | [0/10]")
        row = area.table._rows["Mod Z"]
        self.assertFalse(area.table.isRowHidden(row))

    def test_heavy_notice_hides_when_install_finishes_successfully(self):
        """Regression test: the "large repository downloading in the

        background" banner must not survive past the run's own terminal
        state - it used to only be hidden by a per-line check that could
        miss the run's actual last progress line, leaving it stuck
        visible even after the install had fully completed.
        """
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        area.on_line("[00:00:01] gamma_large_files_v2 | Download | 30% | [1/500]")
        self.assertFalse(area.heavy_notice_label.isHidden())
        area.on_finished(0, "Install complete!")
        self.assertTrue(area.heavy_notice_label.isHidden())

    def test_heavy_notice_hides_when_install_fails(self):
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        area.on_line("[00:00:01] gamma_large_files_v2 | Download | 30% | [1/500]")
        self.assertFalse(area.heavy_notice_label.isHidden())
        area.on_finished(1, "error: something went wrong")
        self.assertTrue(area.heavy_notice_label.isHidden())

    def test_heavy_notice_hides_when_install_is_cancelled(self):
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        area.on_line("[00:00:01] gamma_large_files_v2 | Download | 30% | [1/500]")
        self.assertFalse(area.heavy_notice_label.isHidden())
        area.on_cancelled()
        self.assertTrue(area.heavy_notice_label.isHidden())

    def test_notify_desktop_is_a_noop_without_notify_send(self):
        with patch("commander_gui.ui.common.shutil.which", return_value=None):
            common.notify_desktop("Title", "Message")  # must not raise

    def test_notify_desktop_spawns_notify_send_when_available(self):
        with (
            patch("commander_gui.ui.common.shutil.which", return_value="/usr/bin/notify-send"),
            patch("commander_gui.ui.common.subprocess.Popen") as mock_popen,
        ):
            common.notify_desktop("Title", "Message")
        args = mock_popen.call_args.args[0]
        self.assertIn("Title", args)
        self.assertIn("Message", args)

    def test_on_full_finished_notifies_only_when_the_window_is_not_active(self):
        """Regression test: a finished/failed GAMMA install must surface a

        desktop notification when the user has alt-tabbed away, but not
        spam one while they're already watching the progress bar finish.
        """
        from commander_gui.ui.install_page import InstallPage

        with tempfile.TemporaryDirectory() as tmp:
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
                install_busy = True
                install_operation = "gamma"
                active = False

                def refresh_settings(self):
                    pass

                def set_install_busy(self, *a, **kw):
                    pass

                def statusBar(self):
                    return Mock()

                def isActiveWindow(self):
                    return self.active

            window = FakeWindow()
            page = InstallPage(window)

            with patch("commander_gui.ui.install_page.notify_desktop") as mock_notify:
                window.active = True
                page._on_full_finished(0, "Install complete!")
            mock_notify.assert_not_called()

            with patch("commander_gui.ui.install_page.notify_desktop") as mock_notify:
                window.active = False
                page._on_full_finished(0, "Install complete!")
            mock_notify.assert_called_once()

    def test_gamma_progress_table_evicts_stale_rows_mid_install_by_concurrency(
        self,
    ):
        """Regression test for the reported frozen-Percent bug, mid-install.

        finish_all_except() only ever fires once the CLI's own
        [complete/total] counter proves nothing but the heavy repos remain
        - i.e. only at the very end of a run. A row whose own last line
        never cleanly hit 100%/Skipped much earlier (with hundreds of
        items still genuinely left) had no way to recover before. With
        only a handful of download threads, at most roughly that many
        mods can genuinely be in flight at once - so once more rows than
        that are simultaneously non-terminal, the oldest ones must
        actually be done already.
        """
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        # Default concurrency cap (no set_concurrency() call) is 10. Feed
        # 12 distinct mods, none ever reaching a clean terminal line, with
        # `total` large so the end-of-install condition never triggers.
        for i in range(12):
            area.on_line(f"[00:00:{i:02d}] Mod {i} | Check MD5 | 47% | [{i}/1000]")

        rows = {i: area.table._rows[f"Mod {i}"] for i in range(12)}
        # The 2 oldest (least-recently-touched) rows must be evicted...
        self.assertTrue(area.table.isRowHidden(rows[0]))
        self.assertTrue(area.table.isRowHidden(rows[1]))
        self.assertEqual(area.table.item(rows[0], 1).text(), "Complete")
        # ...while the 10 most recent stay visible, still showing their
        # real (stale-for-now, but not yet evicted) state.
        for i in range(2, 12):
            self.assertFalse(area.table.isRowHidden(rows[i]))

    def test_gamma_progress_table_concurrency_cap_follows_configured_threads(
        self,
    ):
        """set_concurrency() must actually change the eviction threshold."""
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        area.set_concurrency(2)  # cap becomes 2 + 4 = 6
        for i in range(8):
            area.on_line(f"[00:00:{i:02d}] Mod {i} | Check MD5 | 47% | [{i}/1000]")
        rows = {i: area.table._rows[f"Mod {i}"] for i in range(8)}
        for i in range(2):
            self.assertTrue(area.table.isRowHidden(rows[i]))
        for i in range(2, 8):
            self.assertFalse(area.table.isRowHidden(rows[i]))

    def test_start_full_install_configures_the_progress_table_concurrency(self):
        """The per-addon table's staleness cap must track the profile's

        actual configured download-thread count, not just the default -
        a profile using far more (or fewer) threads than the default
        changes how many rows can genuinely be in flight at once.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(Path(tmp) / "gamma"),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
                download_threads=15,
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

                def set_install_busy(self, *a, **kw):
                    pass

                def statusBar(self):
                    return Mock()

            page = InstallPage(FakeWindow())
            with (
                patch("commander_gui.ui.install_page.QMessageBox.question"),
                patch.object(page, "_build_full_command", return_value=["true"]),
            ):
                page._start_full_install(skip_confirm=True)
            self.assertEqual(page.full_progress.table._concurrency_cap, 15 + 4)
            if page._runner is not None:
                page._runner.shutdown()

    def test_progress_table_marks_interrupted_rows_on_failed_run(self):
        """A failed run relabels leftover rows instead of leaving them

        showing a stale operation/percent with no indication anything
        stopped.
        """
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        area.on_line("[00:00:01] Mod A | Skipped | 100% | [1/5]")
        area.on_line("[00:00:02] Mod B | Extract | 47% | [1/5]")
        row_a = area.table._rows["Mod A"]
        row_b = area.table._rows["Mod B"]
        self.assertTrue(area.table.isRowHidden(row_a))
        self.assertFalse(area.table.isRowHidden(row_b))

        area.on_finished(1, "Install failed! Error downloading from X")

        self.assertTrue(area.table.isRowHidden(row_a))
        self.assertFalse(area.table.isRowHidden(row_b))
        self.assertEqual(area.table.item(row_b, 1).text(), "Interrupted")

    def test_progress_table_marks_interrupted_rows_on_cancel(self):
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        area.on_line("[00:00:01] Mod C | Download | 20% | [0/5]")
        row = area.table._rows["Mod C"]

        area.on_cancelled()

        self.assertFalse(area.table.isRowHidden(row))
        self.assertEqual(area.table.item(row, 1).text(), "Interrupted")

    def test_heavy_notice_shows_while_a_heavy_repo_is_active(self):
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        self.assertTrue(area.heavy_notice_label.isHidden())

        area.on_line("[00:00:01] stalker_gamma | Download | 0% | [1/5]")
        self.assertFalse(area.heavy_notice_label.isHidden())

        area.on_line("[00:00:02] gamma_setup | Extract | 100% | [2/5]")
        area.on_line("[00:00:03] gamma_large_files_v2 | Extract | 100% | [3/5]")
        area.on_line("[00:00:04] stalker_gamma | Extract | 100% | [4/5]")
        self.assertTrue(area.heavy_notice_label.isHidden())

    def test_status_label_updates_are_throttled(self):
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        with patch(
            "commander_gui.ui.common.time.monotonic", return_value=100.0
        ):
            area.on_line("[00:00:01] Mod A | Download | 10% | [0/5]")
            first_text = area.status_label.text()
            area.on_line("[00:00:02] Mod B | Download | 20% | [0/5]")
            # Same instant - throttled, label unchanged.
            self.assertEqual(area.status_label.text(), first_text)

        with patch(
            "commander_gui.ui.common.time.monotonic", return_value=101.5
        ):
            area.on_line("[00:00:03] Mod C | Download | 30% | [0/5]")
            self.assertNotEqual(area.status_label.text(), first_text)

    def test_status_label_format(self):
        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        area.on_line("[00:00:01] Craft From Stashes | Extract | 100% | [574/577]")
        self.assertEqual(
            area.status_label.text(),
            "Craft From Stashes — Extract — 100%  ·  574/577 done",
        )

    def test_mod_install_duplicate_name_is_rejected(self):
        """Mod install with duplicate name shows error and cleans up."""
        import tempfile
        from unittest.mock import MagicMock, patch

        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.settings import CliProfile
        from commander_gui.ui.mod_manager_page import ModManagerPage

        # Setup minimal profile and temp directory
        with tempfile.TemporaryDirectory() as tmpdir:
            gamma_dir = Path(tmpdir) / "gamma"
            mods_dir = gamma_dir / "mods"
            mods_dir.mkdir(parents=True)

            # Create existing mod folder
            existing_mod = mods_dir / "DuplicateMod"
            existing_mod.mkdir()
            (existing_mod / "info.txt").write_text("existing")

            # Create modlist with existing mod
            profile_dir = gamma_dir / "profiles" / "G.A.M.M.A"
            profile_dir.mkdir(parents=True)
            modlist = profile_dir / "modlist.txt"
            modlist.write_text("+DuplicateMod\n")

            profile = CliProfile(
                active=True,
                profile_name="test",
                anomaly="/tmp/anomaly",
                gamma=str(gamma_dir),
                cache="/tmp/cache",
            )

            QApplication.instance() or QApplication([])
            page = ModManagerPage.__new__(ModManagerPage)
            page.window = MagicMock()
            page.window.settings = MagicMock()
            page.window.settings.active_profile = profile
            page._install_staging = mods_dir / "DuplicateMod_staging"
            page._install_staging.mkdir()
            (page._install_staging / "test.txt").write_text("new")
            # Load modlist lines
            page._lines = modlist.read_text().splitlines()

            # Mock QMessageBox to capture warning
            with patch.object(QMessageBox, "warning") as mock_warning:
                # Mock _finish_install to avoid needing full page initialization
                with patch.object(page, "_finish_install") as mock_finish:
                    # Call _on_mod_moved with duplicate destination
                    page._on_mod_moved(existing_mod)

                # Verify warning was shown
                mock_warning.assert_called_once()
                args = mock_warning.call_args[0]
                self.assertEqual(args[1], "Mod installation failed")
                self.assertIn("already in the modlist", args[2])

                # Verify _finish_install was called (which handles staging cleanup)
                mock_finish.assert_called_once()

    def test_on_profiles_loaded_matches_active_profile_case_insensitively(self):
        """A CLI/settings.json case mismatch must not select the wrong profile.

        Every read/write on this page targets whatever profile_combo shows;
        if it silently lands on the wrong one (e.g. MO2's own "Default"
        profile instead of the real active one), installs go into a
        completely different modlist.txt than every other page - and the
        topbar mod counter, which resolves the profile case-insensitively -
        reads from.
        """
        from unittest.mock import MagicMock, patch

        from PySide6.QtWidgets import QApplication, QComboBox, QLabel

        from commander_gui.settings import CliProfile
        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        page = ModManagerPage.__new__(ModManagerPage)
        page.profile_combo = QComboBox()
        page.selected_label = QLabel()
        page.count_label = QLabel()
        page.window = MagicMock()
        page._profiles_task = MagicMock()
        page._profiles_generation = 1
        page._pending_refresh = False
        page.window.settings.active_profile = CliProfile(mo2_profile="G.A.M.M.A")

        result = (["Default", "g.a.m.m.a"], "g.a.m.m.a")
        with patch.object(page, "_load_mods"):
            page._on_profiles_loaded(result, page._profiles_task, 1)

        self.assertEqual(page.profile_combo.currentText(), "g.a.m.m.a")

    def test_on_profiles_loaded_prefers_mo2_selected_profile_over_stale_config(self):
        """MO2's actual selected profile wins over a stale CliProfile field.

        A user who creates/switches to a custom profile directly in MO2
        (e.g. "Solo Profile") without also updating it on the Profiles page
        must still have Mod Manager follow what MO2 is really using - not
        silently keep reading/writing the old configured profile's
        modlist.txt while the game itself runs a different one.
        """
        from unittest.mock import MagicMock, patch

        from PySide6.QtWidgets import QApplication, QComboBox, QLabel

        from commander_gui.settings import CliProfile
        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        page = ModManagerPage.__new__(ModManagerPage)
        page.profile_combo = QComboBox()
        page.selected_label = QLabel()
        page.count_label = QLabel()
        page.window = MagicMock()
        page._profiles_task = MagicMock()
        page._profiles_generation = 1
        page._pending_refresh = False
        page.window.settings.active_profile = CliProfile(mo2_profile="G.A.M.M.A")

        result = (["G.A.M.M.A", "Solo Profile"], "Solo Profile")
        with patch.object(page, "_load_mods"):
            page._on_profiles_loaded(result, page._profiles_task, 1)

        self.assertEqual(page.profile_combo.currentText(), "Solo Profile")

    def test_on_profiles_loaded_falls_back_to_configured_profile_with_no_mo2_selection(
        self,
    ):
        """No MO2 selection yet (e.g. before first launch) still resolves."""
        from unittest.mock import MagicMock, patch

        from PySide6.QtWidgets import QApplication, QComboBox, QLabel

        from commander_gui.settings import CliProfile
        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        page = ModManagerPage.__new__(ModManagerPage)
        page.profile_combo = QComboBox()
        page.selected_label = QLabel()
        page.count_label = QLabel()
        page.window = MagicMock()
        page._profiles_task = MagicMock()
        page._profiles_generation = 1
        page._pending_refresh = False
        page.window.settings.active_profile = CliProfile(mo2_profile="G.A.M.M.A")

        result = (["G.A.M.M.A", "Solo Profile"], "")
        with patch.object(page, "_load_mods"):
            page._on_profiles_loaded(result, page._profiles_task, 1)

        self.assertEqual(page.profile_combo.currentText(), "G.A.M.M.A")

    def test_successful_install_records_name_for_tree_focus(self):
        """A successful install must remember the mod name for _finish_install
        to scroll to - new mods land disabled in "Custom Mods", at the
        bottom of the list (see modlist.add_custom_mod), easy to miss on a
        real-sized modlist."""
        import tempfile
        from unittest.mock import MagicMock, patch

        from PySide6.QtWidgets import QApplication

        from commander_gui.settings import CliProfile
        from commander_gui.ui.mod_manager_page import ModManagerPage

        with tempfile.TemporaryDirectory() as tmpdir:
            gamma_dir = Path(tmpdir) / "gamma"
            mods_dir = gamma_dir / "mods"
            mods_dir.mkdir(parents=True)
            new_mod = mods_dir / "Terrain Textures Redone"
            new_mod.mkdir()

            profile = CliProfile(
                active=True,
                profile_name="test",
                anomaly="/tmp/anomaly",
                gamma=str(gamma_dir),
                cache="/tmp/cache",
            )

            QApplication.instance() or QApplication([])
            page = ModManagerPage.__new__(ModManagerPage)
            page.window = MagicMock()
            page.window.settings = MagicMock()
            page.window.settings.active_profile = profile
            page.window.statusBar.return_value.showMessage = MagicMock()
            page._install_staging = None
            page._lines = ["+ExistingMod"]
            page._just_installed_name = None
            page.profile_combo = MagicMock()
            page.profile_combo.currentText.return_value = "G.A.M.M.A"

            with (
                patch.object(page, "_write_lines", return_value=True),
                patch.object(page, "_finish_install") as mock_finish,
            ):
                page._on_mod_moved(new_mod)

            self.assertEqual(page._just_installed_name, "Terrain Textures Redone")
            mock_finish.assert_called_once()

    def test_cached_archive_for_matches_and_verifies_a_real_archive(self):
        """Regression test for a real reported crash-adjacent mismatch:

        the topbar mod counter excludes an enabled mod whose folder is
        missing from disk, while Mod Manager's own counter (no such
        check) still shows it - "Reinstall from Cache" is how a user
        fixes that one mod without a full reinstall. This confirms the
        matching + MD5-verification step actually works end-to-end
        against a real ModPackRecord/cache archive pair.
        """
        import hashlib
        import tempfile
        from unittest.mock import MagicMock

        from PySide6.QtWidgets import QApplication

        from commander_gui.repair import ModPackRecord
        from commander_gui.settings import CliProfile
        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cache_dir.mkdir()
            archive = cache_dir / "SomeMod.zip"
            archive.write_bytes(b"archive bytes")
            digest = hashlib.md5(b"archive bytes").hexdigest()

            profile = CliProfile(
                active=True,
                profile_name="test",
                gamma=str(Path(tmp) / "gamma"),
                cache=str(cache_dir),
            )
            page = ModManagerPage.__new__(ModManagerPage)
            page.window = MagicMock()
            page.window.settings.active_profile = profile
            page.profile_combo = MagicMock()
            page.profile_combo.currentText.return_value = "G.A.M.M.A"

            record = ModPackRecord(1, "Some Mod", "", "", "", "SomeMod.zip", digest, "")
            with patch(
                "commander_gui.ui.mod_manager_page.local_modpack_records",
                return_value={"1- Some Mod": record},
            ):
                found = page._cached_archive_for("1- Some Mod")
                self.assertEqual(found, archive)

                # A tampered/outdated archive (wrong MD5) must not match.
                archive.write_bytes(b"different bytes")
                self.assertIsNone(page._cached_archive_for("1- Some Mod"))

    def test_cached_archive_for_returns_none_without_a_matching_record(self):
        """A mod not resolvable via the modpack list (confirmed against a

        real profile: roughly a third of entries aren't) must return
        None, not raise or guess - callers show an explanation instead.
        """
        from unittest.mock import MagicMock

        from PySide6.QtWidgets import QApplication

        from commander_gui.settings import CliProfile
        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="test", gamma="/tmp/gamma", cache="/tmp/cache")
        page = ModManagerPage.__new__(ModManagerPage)
        page.window = MagicMock()
        page.window.settings.active_profile = profile
        page.profile_combo = MagicMock()
        page.profile_combo.currentText.return_value = "G.A.M.M.A"

        with patch(
            "commander_gui.ui.mod_manager_page.local_modpack_records",
            return_value={},
        ):
            self.assertIsNone(page._cached_archive_for("Some Unlisted Mod"))

    def test_reinstall_mod_from_cache_explains_when_no_archive_found(self):
        from unittest.mock import MagicMock

        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        page = ModManagerPage.__new__(ModManagerPage)
        page.window = MagicMock()
        page.window.install_busy = False
        page._install_active = False

        with (
            patch.object(page, "_mo2_running", return_value=False),
            patch.object(page, "_cached_archive_for", return_value=None),
            patch.object(QMessageBox, "information") as mock_info,
            patch.object(page, "_start_mod_install") as mock_start,
        ):
            page._reinstall_mod_from_cache("Some Mod")

        mock_info.assert_called_once()
        mock_start.assert_not_called()

    def test_reinstall_mod_from_cache_backs_up_the_existing_folder_first(self):
        import tempfile
        from unittest.mock import MagicMock

        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.settings import CliProfile
        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            gamma_dir = Path(tmp) / "gamma"
            mods_dir = gamma_dir / "mods"
            mods_dir.mkdir(parents=True)
            existing = mods_dir / "Some Mod"
            existing.mkdir()
            (existing / "corrupt.txt").write_text("partial")
            archive = Path(tmp) / "SomeMod.zip"
            archive.write_bytes(b"data")

            profile = CliProfile(
                active=True, profile_name="test", gamma=str(gamma_dir), cache=str(Path(tmp) / "cache")
            )
            page = ModManagerPage.__new__(ModManagerPage)
            page.window = MagicMock()
            page.window.install_busy = False
            page._install_active = False
            page.window.settings.active_profile = profile

            with (
                patch.object(page, "_mo2_running", return_value=False),
                patch.object(page, "_cached_archive_for", return_value=archive),
                patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
                patch.object(page, "_start_mod_install") as mock_start,
            ):
                page._reinstall_mod_from_cache("Some Mod")

            self.assertFalse(existing.exists())
            self.assertTrue(page._install_is_reinstall)
            self.assertIsNotNone(page._reinstall_backup)
            backed_up_destination, backup = page._reinstall_backup
            self.assertEqual(backed_up_destination, existing)
            self.assertTrue((backup / "corrupt.txt").is_file())
            mock_start.assert_called_once_with(archive, "Some Mod")

    def test_on_mod_moved_reinstall_skips_add_custom_mod(self):
        """The modlist.txt entry already exists for a reinstall - it must

        not go through add_custom_mod() (which would just raise on the
        duplicate name and delete the freshly-restored files)."""
        from unittest.mock import MagicMock

        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.settings import CliProfile
        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="test", gamma="/tmp/gamma")
        page = ModManagerPage.__new__(ModManagerPage)
        page.window = MagicMock()
        page.window.settings.active_profile = profile
        page.window.statusBar.return_value.showMessage = MagicMock()
        page._install_is_reinstall = True
        page._lines = ["+SomeMod"]

        with (
            patch.object(page, "_write_lines") as mock_write,
            patch.object(page, "_finish_install") as mock_finish,
            patch.object(QMessageBox, "information") as mock_info,
        ):
            page._on_mod_moved(Path("/tmp/gamma/mods/SomeMod"))

        mock_write.assert_not_called()
        mock_info.assert_called_once()
        self.assertEqual(page._just_installed_name, "SomeMod")
        mock_finish.assert_called_once()

    def test_finish_install_restores_the_backup_when_the_reinstall_failed(self):
        import tempfile
        from unittest.mock import MagicMock

        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "Some Mod"
            backup = Path(tmp) / ".Some Mod.reinstall-backup-abc"
            backup.mkdir()
            (backup / "original.txt").write_text("original")
            # destination does NOT exist - move_payload() never succeeded.

            page = ModManagerPage.__new__(ModManagerPage)
            page.window = MagicMock()
            page.window.install_operation = "mod_install"
            page._install_staging = None
            page._just_installed_name = None
            page._reinstall_backup = (destination, backup)
            page.install_progress = MagicMock()
            page.search = MagicMock()
            page.search.text.return_value = ""

            with (
                patch.object(page, "_update_guard"),
                patch.object(page, "_load_mods"),
            ):
                page._finish_install()

            self.assertTrue((destination / "original.txt").is_file())
            self.assertFalse(backup.exists())
            self.assertIsNone(page._reinstall_backup)
            self.assertFalse(page._install_is_reinstall)

    def test_finish_install_discards_the_backup_when_the_reinstall_succeeded(self):
        import tempfile
        from unittest.mock import MagicMock

        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "Some Mod"
            destination.mkdir()
            (destination / "new.txt").write_text("new")
            backup = Path(tmp) / ".Some Mod.reinstall-backup-abc"
            backup.mkdir()
            (backup / "original.txt").write_text("original")

            page = ModManagerPage.__new__(ModManagerPage)
            page.window = MagicMock()
            page.window.install_operation = "mod_install"
            page._install_staging = None
            page._just_installed_name = None
            page._reinstall_backup = (destination, backup)
            page.install_progress = MagicMock()
            page.search = MagicMock()
            page.search.text.return_value = ""

            with (
                patch.object(page, "_update_guard"),
                patch.object(page, "_load_mods"),
            ):
                page._finish_install()

            self.assertTrue((destination / "new.txt").is_file())
            self.assertFalse(backup.exists())

    def test_finish_install_focuses_and_clears_pending_name(self):
        from unittest.mock import MagicMock, patch

        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        page = ModManagerPage.__new__(ModManagerPage)
        page.window = MagicMock()
        page.window.install_operation = "mod_install"
        page._install_staging = None
        page._just_installed_name = "Terrain Textures Redone"
        page.install_progress = MagicMock()
        page.search = MagicMock()
        page.search.text.return_value = ""

        with (
            patch.object(page, "_update_guard"),
            patch.object(page, "_load_mods"),
            patch.object(page, "_focus_mod_in_tree") as mock_focus,
        ):
            page._finish_install()

        mock_focus.assert_called_once_with("Terrain Textures Redone")
        self.assertIsNone(page._just_installed_name)
        page.search.clear.assert_not_called()

    def test_finish_install_clears_a_leftover_search_before_focusing(self):
        """A stale search term must not leave the just-installed mod hidden.

        _apply_filter() hides tree items that don't match the search box;
        scrollToItem()/setCurrentItem() on a hidden item is a silent no-op,
        so the search has to be cleared before focusing the new mod, or the
        "look, here's your mod" behavior does nothing visible.
        """
        from unittest.mock import MagicMock, patch

        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        page = ModManagerPage.__new__(ModManagerPage)
        page.window = MagicMock()
        page.window.install_operation = "mod_install"
        page._install_staging = None
        page._just_installed_name = "Terrain Textures Redone"
        page.install_progress = MagicMock()
        page.search = MagicMock()
        page.search.text.return_value = "some old search"

        with (
            patch.object(page, "_update_guard"),
            patch.object(page, "_load_mods"),
            patch.object(page, "_focus_mod_in_tree") as mock_focus,
        ):
            page._finish_install()

        page.search.clear.assert_called_once()
        mock_focus.assert_called_once_with("Terrain Textures Redone")

    def test_focus_mod_in_tree_selects_matching_item(self):
        from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        page = ModManagerPage.__new__(ModManagerPage)
        page.tree = QTreeWidget()
        header = QTreeWidgetItem(["Extra Mods"])
        page.tree.addTopLevelItem(header)
        child = QTreeWidgetItem(["Terrain Textures Redone"])
        header.addChild(child)
        other = QTreeWidgetItem(["Unrelated Mod"])
        header.addChild(other)

        page._focus_mod_in_tree("Terrain Textures Redone")

        self.assertIs(page.tree.currentItem(), child)

    def test_focus_mod_in_tree_does_not_unhide_a_search_filtered_item(self):
        """Demonstrates why _finish_install() must clear the search first.

        _apply_filter() hides non-matching items with setHidden(True).
        _focus_mod_in_tree() makes the item Qt's "current" item regardless,
        but does not un-hide it - so without clearing the search first, the
        newly-installed mod is selected yet still invisible to the user.
        """
        from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        page = ModManagerPage.__new__(ModManagerPage)
        page.tree = QTreeWidget()
        header = QTreeWidgetItem(["Extra Mods"])
        page.tree.addTopLevelItem(header)
        child = QTreeWidgetItem(["Terrain Textures Redone"])
        header.addChild(child)
        child.setHidden(True)  # as _apply_filter() would leave it

        page._focus_mod_in_tree("Terrain Textures Redone")

        self.assertTrue(child.isHidden())

    def test_populate_tree_matches_mo2s_actual_on_screen_order(self):
        """Regression for the file-order-vs-screen-order bug.

        MO2 writes modlist.txt with file-top as the HIGHEST-priority mod
        (on-screen BOTTOM) and file-bottom as the lowest-priority mod
        (on-screen TOP) - confirmed against MO2's own source
        (profile.cpp). _populate_tree() must walk grouped()'s file-order
        output in reverse to match. Shaped like the real GAMMA evidence:
        "Audio" is the file's LAST category (so it must render FIRST/top),
        with two real members whose own relative order must also flip.
        """
        from unittest.mock import MagicMock

        from PySide6.QtWidgets import QApplication, QTreeWidget

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        page = ModManagerPage.__new__(ModManagerPage)
        page.window = MagicMock()
        page.tree = QTreeWidget()
        page.search = MagicMock()
        page.search.text.return_value = ""
        page.count_label = MagicMock()
        page._lines = ["+X", "-Visual_separator", "+Y", "+Z", "-Audio_separator"]

        page._populate_tree()

        headers = [
            page.tree.topLevelItem(i).text(0)
            for i in range(page.tree.topLevelItemCount())
        ]
        self.assertEqual(headers, ["Audio (2)", "Visual (1)"])
        audio_header = page.tree.topLevelItem(0)
        audio_children = [
            (audio_header.child(j).text(0), audio_header.child(j).text(1))
            for j in range(audio_header.childCount())
        ]
        self.assertEqual(audio_children, [("Z", "1"), ("Y", "2")])
        visual_header = page.tree.topLevelItem(1)
        self.assertEqual(visual_header.child(0).text(1), "3")

    def test_log_dump_archives_logs_and_skips_noise(self):
        import zipfile
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            commander = root / "commander"
            (commander / "dumps").mkdir(parents=True)
            (commander / "launcher.log").write_text("launch ok", encoding="utf-8")
            (commander / "stalker-gamma-cli20260823.log").write_text(
                "[12:00:00] line", encoding="utf-8"
            )
            (commander / "dumps" / "old.zip").write_bytes(b"old")
            anomaly = root / "anomaly"
            (anomaly / "textures").mkdir(parents=True)
            (anomaly / "app.log").write_text("game log", encoding="utf-8")
            (anomaly / "crash.dmp").write_text("minidump", encoding="utf-8")
            (anomaly / "textures" / "grass.dds").write_bytes(b"\x00" * 16)
            big = anomaly / "huge.log"
            big.write_text("x" * 100, encoding="utf-8")

            target, stats = build_log_dump(
                root / "out",
                {"commander": commander, "anomaly": anomaly},
                extra_texts={"diagnostics.txt": "system info"},
                now=datetime(2026, 8, 23, 14, 37, tzinfo=timezone.utc),
                per_file_cap=50,
            )

            self.assertEqual(target.name, "commander-log-dump-20260823-143700.zip")
            with zipfile.ZipFile(target) as zf:
                names = set(zf.namelist())
                manifest = zf.read("MANIFEST.txt").decode()
        self.assertIn("commander/launcher.log", names)
        self.assertIn("commander/stalker-gamma-cli20260823.log", names)
        self.assertIn("anomaly/app.log", names)
        self.assertIn("anomaly/crash.dmp", names)
        self.assertIn("report/diagnostics.txt", names)
        self.assertIn("MANIFEST.txt", names)
        self.assertNotIn("commander/dumps/old.zip", names)
        self.assertNotIn("anomaly/textures/grass.dds", names)
        self.assertIn("huge.log", manifest)
        self.assertEqual(stats["files"], 5)

    def test_log_dump_name_suffixes_on_collision(self):
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            src = Path(tmp) / "logs"
            src.mkdir()
            (src / "a.log").write_text("a", encoding="utf-8")
            first, _ = build_log_dump(
                dest,
                {"commander": src},
                now=datetime(2026, 8, 23, 14, 37, tzinfo=timezone.utc),
            )
            second, _ = build_log_dump(
                dest,
                {"commander": src},
                now=datetime(2026, 8, 23, 14, 37, tzinfo=timezone.utc),
            )
        self.assertEqual(first.name, "commander-log-dump-20260823-143700.zip")
        self.assertEqual(second.name, "commander-log-dump-20260823-143700-2.zip")

    def test_gui_settings_normalizes_corrupt_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gui-settings.json"
            path.write_text(
                json.dumps({"font_size": "bad", "theme": [], "prefixes": [1]}),
                encoding="utf-8",
            )
            with patch.object(gui_settings, "gui_settings_path", return_value=path):
                state = gui_settings.load_gui_settings()
            self.assertEqual(state["font_size"], 13)
            self.assertEqual(state["theme"], "gamma")
            self.assertEqual(state["prefixes"], {})

    def test_gui_settings_rejects_non_finite_playtime_values(self):
        """Regression test: a corrupt/hand-edited gui-settings.json can hold

        JSON's non-standard "Infinity"/"NaN" literals (json.loads accepts
        them by default). An Infinity there used to sail through
        sanitization (it is a float, not a bool, and >= 0) and land in
        playtime_seconds/last_played_ts, and format_playtime() later does
        ``int(total_seconds // 60)`` - undefined for a non-finite float
        (raises ValueError/OverflowError) - crashing the Dashboard/Play
        page the next time it renders that profile's playtime.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gui-settings.json"
            path.write_text(
                '{"playtime_seconds": {"Gamma": Infinity, "Other": NaN, '
                '"Ok": 42}, "last_played_ts": {"Gamma": -Infinity, "Ok": 5}}',
                encoding="utf-8",
            )
            with patch.object(gui_settings, "gui_settings_path", return_value=path):
                state = gui_settings.load_gui_settings()
            self.assertEqual(state["playtime_seconds"], {"Ok": 42.0})
            self.assertEqual(state["last_played_ts"], {"Ok": 5.0})

    def test_gui_settings_survives_infinite_numeric_fields(self):
        """Regression test: json.loads() accepts the bare "Infinity" token as

        a real float, and int() on a non-finite float raises OverflowError,
        not ValueError - font_size/window_width/window_height/
        mo2_display_dpi each only caught (TypeError, ValueError), so a
        corrupt/hand-edited gui-settings.json with e.g. "font_size":
        Infinity used to crash load_gui_settings() itself with an unhandled
        OverflowError, taking the app down before a single window opened.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gui-settings.json"
            path.write_text(
                '{"font_size": Infinity, "window_width": -Infinity, '
                '"window_height": Infinity, "mo2_display_dpi": Infinity}',
                encoding="utf-8",
            )
            with patch.object(gui_settings, "gui_settings_path", return_value=path):
                state = gui_settings.load_gui_settings()
            self.assertEqual(state["font_size"], 13)
            self.assertEqual(state["window_width"], 1080)
            self.assertEqual(state["window_height"], 950)
            self.assertEqual(state["mo2_display_dpi"], 120)

    def test_safe_extract_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "bad.tar.gz"
            destination = Path(tmp) / "destination"
            destination.mkdir()
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("../../outside.txt")
                data = b"unsafe"
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            with tarfile.open(archive, "r:gz") as tf, self.assertRaises(ValueError):
                _safe_extract(tf, destination)

    def test_cli_failure_marker_overrides_zero_exit(self):
        self.assertFalse(cli_ok(0, "Install failed: error: disk full", ""))
        self.assertTrue(cli_ok(0, "Install finished", ""))

    def test_atomic_write_uses_complete_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            write_text(path, "complete")
            self.assertEqual(path.read_text(encoding="utf-8"), "complete")

    def test_atomic_write_preserves_existing_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("old", encoding="utf-8")
            with patch("commander_gui.atomic.os.replace", side_effect=OSError("full")), self.assertRaises(OSError):
                write_text(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(path.parent.glob(".state.json.*.tmp")), [])

    def test_atomic_write_preserves_existing_file_permissions(self):
        import stat as _stat

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared.json"
            path.write_text("old", encoding="utf-8")
            os.chmod(path, 0o644)
            write_text(path, "new")
            mode = _stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o644)
            self.assertEqual(path.read_text(encoding="utf-8"), "new")

    def test_atomic_write_closes_fd_when_fdopen_fails(self):
        """write_text() hands tempfile.mkstemp()'s raw fd to os.fdopen(). If
        fdopen() itself raises before a file object takes ownership of the
        descriptor, the fd must be closed explicitly by write_text() - the
        surrounding except block only unlinks the temp *file*, it never
        touches the fd, so without an explicit close the descriptor leaks
        for the rest of the process every time this happens."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            captured_fd = {}

            def _boom(fd, *args, **kwargs):
                captured_fd["fd"] = fd
                raise OSError("fdopen exploded")

            with (
                patch("commander_gui.atomic.os.fdopen", side_effect=_boom),
                self.assertRaises(OSError),
            ):
                write_text(path, "new")
            self.assertIn("fd", captured_fd)
            # If write_text leaked the descriptor, closing it again here
            # would succeed; a proper fix already closed it, so this must
            # fail with EBADF.
            with self.assertRaises(OSError):
                os.close(captured_fd["fd"])
            self.assertEqual(list(path.parent.glob(".state.json.*.tmp")), [])

    def test_load_settings_treats_non_list_profiles_as_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            # A hand-edited file where Profiles is an object must not silently
            # wipe the user's profiles.
            path.write_text('{"Profiles": {"oops": true}}', encoding="utf-8")
            settings = load_settings(path)
            self.assertEqual(len(settings.profiles), 1)  # fresh default
            backups = list(Path(tmp).glob("settings.json.corrupt*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("oops", backups[0].read_text(encoding="utf-8"))

    def test_load_settings_repeat_corruption_keeps_earlier_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{bad json", encoding="utf-8")
            load_settings(path)
            first = Path(tmp) / "settings.json.corrupt"
            self.assertTrue(first.exists())
            first.write_text("ORIGINAL CORRUPT", encoding="utf-8")
            # Corrupt again; the existing backup must not be overwritten.
            path.write_text("{worse json", encoding="utf-8")
            load_settings(path)
            self.assertEqual(first.read_text(encoding="utf-8"), "ORIGINAL CORRUPT")
            extra = list(Path(tmp).glob("settings.json.corrupt.2*"))
            self.assertEqual(len(extra), 1)

    def test_quarantine_mod_and_archive_moves_archive_through_symlinked_downloads(self):
        """Regression test: repair must not delete anything outright -

        a broken mod folder and its cached archive are moved aside so a
        failed reinstall can restore them, not lost outright.
        """
        from commander_gui.repair import ModPackRecord, quarantine_mod_and_archive

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "gamma"
            cache = Path(tmp) / "cache"
            record = ModPackRecord(
                1, "Broken Mod", "", "https://example.com/dl", "", "broken.zip", "", ""
            )
            folder = record.folder_name  # "1- Broken Mod"
            mod_dir = base / "mods" / folder
            mod_dir.mkdir(parents=True)
            (mod_dir / "file.txt").write_text("original content")
            cache.mkdir()
            (cache / "broken.zip").write_bytes(b"zip")
            (base / "downloads").symlink_to(cache, target_is_directory=True)

            result = quarantine_mod_and_archive(base, folder, record)

            self.assertFalse(mod_dir.exists())
            self.assertFalse((cache / "broken.zip").exists())
            self.assertEqual(len(result.items), 2)
            # Nothing was deleted - both moved somewhere still on disk.
            for item in result.items:
                self.assertTrue(item.quarantined.exists())

    def test_restore_from_quarantine_undoes_a_failed_repair(self):
        from commander_gui.repair import (
            ModPackRecord,
            quarantine_mod_and_archive,
            restore_from_quarantine,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "gamma"
            record = ModPackRecord(1, "Broken Mod", "", "", "", "", "", "")
            folder = record.folder_name
            mod_dir = base / "mods" / folder
            mod_dir.mkdir(parents=True)
            (mod_dir / "file.txt").write_text("original content")

            result = quarantine_mod_and_archive(base, folder)
            self.assertFalse(mod_dir.exists())

            failures = restore_from_quarantine(result)

            self.assertEqual(failures, [])
            self.assertTrue(mod_dir.is_dir())
            self.assertEqual(
                (mod_dir / "file.txt").read_text(), "original content"
            )

    def test_purge_quarantine_permanently_removes_quarantined_copies(self):
        from commander_gui.repair import (
            ModPackRecord,
            purge_quarantine,
            quarantine_mod_and_archive,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "gamma"
            record = ModPackRecord(1, "Broken Mod", "", "", "", "", "", "")
            folder = record.folder_name
            (base / "mods" / folder).mkdir(parents=True)

            result = quarantine_mod_and_archive(base, folder)
            quarantined_path = result.items[0].quarantined
            self.assertTrue(quarantined_path.exists())

            purge_quarantine(base)

            self.assertFalse(quarantined_path.exists())

    def test_quarantine_rolls_back_the_mod_folder_when_the_archive_move_fails(self):
        """Regression test: a half-quarantined mod must never be stranded.

        The QuarantineRecord is the caller's only handle on what was moved
        aside, and install_page never receives it when this raises - so a
        mod folder left sitting in .verify-quarantine would never be
        restored on cancel/failure and the next successful repair's
        purge_quarantine() would delete the user's only copy of it.
        """
        from commander_gui.repair import ModPackRecord, quarantine_mod_and_archive

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "gamma"
            record = ModPackRecord(
                1, "Broken Mod", "", "https://example.com/dl", "", "broken.zip", "", ""
            )
            folder = record.folder_name
            mod_dir = base / "mods" / folder
            mod_dir.mkdir(parents=True)
            (mod_dir / "file.txt").write_text("original content")
            downloads = base / "downloads"
            downloads.mkdir()
            (downloads / "broken.zip").write_bytes(b"zip")

            real_rename = Path.rename

            def rename(self, target):
                if self.name == "broken.zip":
                    raise OSError("read-only download cache")
                return real_rename(self, target)

            with (
                patch.object(Path, "rename", rename),
                self.assertRaises(OSError),
            ):
                quarantine_mod_and_archive(base, folder, record)

            self.assertTrue(mod_dir.is_dir())
            self.assertEqual((mod_dir / "file.txt").read_text(), "original content")
            self.assertTrue((downloads / "broken.zip").is_file())

    def test_cli_worker_cancel_before_spawn_is_applied_after_popen(self):
        """Regression test: cancel() arriving before Popen must still cancel.

        The pre-spawn branch of cancel() used to also set a _cancel_pending
        flag that nothing ever read; _cancel_event is the only thing run()
        actually checks, so that flag must not be what this relies on.
        """
        from commander_gui.cli_runner import CliWorker

        worker = CliWorker()
        worker.setup([sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertFalse(hasattr(worker, "_cancel_pending"))
        worker.cancel()  # no process yet - must arm the event
        self.assertTrue(worker._cancel_event.is_set())

        codes: list[int] = []
        worker.finished.connect(lambda rc, _out: codes.append(rc))
        worker.run()

        self.assertEqual(len(codes), 1)
        self.assertNotEqual(codes[0], 0)

    def test_install_archive_honours_cancel_during_the_move_phase(self):
        """Regression test: install_archive dropped its cancel_event before

        move_payload, so cancelling while a large mod was being moved into
        gamma/mods was silently ignored and the install completed anyway.
        """
        from commander_gui.mod_install import ModInstallError, install_archive

        cancel = threading.Event()

        def fake_extract(archive, staging, cancel_event=None, progress=None):
            staging.mkdir(parents=True)
            (staging / "a.txt").write_text("a")
            (staging / "b.txt").write_text("b")
            cancel.set()

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "mod.zip"
            archive.write_bytes(b"not really a zip")
            mods = Path(tmp) / "mods"
            with (
                patch(
                    "commander_gui.mod_install.extract_archive",
                    side_effect=fake_extract,
                ),
                self.assertRaisesRegex(ModInstallError, "cancelled"),
            ):
                install_archive(archive, mods, "My Mod", cancel)
            self.assertFalse((mods / "My Mod").exists())

    def test_payload_root_does_not_unwrap_a_lone_gamedata_folder(self):
        """Regression test for the reported red-X/non-functional-mod bug:

        a mod whose whole payload is a single top-level "gamedata" folder
        (very common for small, single-purpose mods) must keep that
        folder, not have it silently unwrapped away - MO2's own
        StalkerAnomalyModDataChecker requires one of appdata/bin/db/
        gamedata literally at the mod's top level, or it flags the mod
        INVALID (red X) and the game's VFS never mounts it.
        """
        from commander_gui.mod_install import payload_root

        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            (staging / "gamedata" / "scripts").mkdir(parents=True)
            (staging / "gamedata" / "scripts" / "foo.script").write_text("x")
            self.assertEqual(payload_root(staging), staging)

            for name in ("bin", "db", "appdata", "GameData", "BIN"):
                staging2 = Path(tmp) / f"staging_{name}"
                (staging2 / name).mkdir(parents=True)
                (staging2 / name / "f.txt").write_text("x")
                self.assertEqual(payload_root(staging2), staging2, msg=name)

    def test_payload_root_still_unwraps_an_unrelated_wrapper_folder(self):
        """A lone top-level directory with an ordinary (non-data-folder)

        name is still a meaningless archiver-added wrapper and must still
        be unwrapped - preserving today's existing, correct behavior.
        """
        from commander_gui.mod_install import payload_root

        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            wrapper = staging / "QAW_Overhaul_1.0.2"
            (wrapper / "gamedata").mkdir(parents=True)
            self.assertEqual(payload_root(staging), wrapper)

    def test_payload_root_collapses_a_duplicate_gamedata_wrapper(self):
        """Regression test: an archive genuinely shaped like

        "gamedata/gamedata/<real files>" (the outer gamedata is a
        redundant duplicate wrapper, not the real payload) must collapse
        to a single "gamedata" at the destination - not the double
        nesting the single-level check would otherwise produce, since it
        can't tell this apart from a real "gamedata/<real files>"
        archive (which must NOT be unwrapped - see the sibling test
        above this one).
        """
        from commander_gui.mod_install import payload_root

        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            inner = staging / "gamedata" / "gamedata"
            (inner / "scripts").mkdir(parents=True)
            (inner / "scripts" / "foo.script").write_text("x")
            payload = payload_root(staging)
            # payload's own children must be exactly the inner
            # "gamedata" folder - moving them lands a single "gamedata"
            # at the mod's top level, not "gamedata/gamedata".
            payload_children = list(payload.iterdir())
            self.assertEqual([c.name for c in payload_children], ["gamedata"])
            self.assertTrue((payload_children[0] / "scripts" / "foo.script").is_file())

    def test_payload_root_handles_triple_duplicate_gamedata_nesting(self):
        from commander_gui.mod_install import payload_root

        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            inner = staging / "gamedata" / "gamedata" / "gamedata"
            (inner / "scripts").mkdir(parents=True)
            payload = payload_root(staging)
            payload_children = list(payload.iterdir())
            self.assertEqual([c.name for c in payload_children], ["gamedata"])
            self.assertTrue((payload_children[0] / "scripts").is_dir())

    def test_move_payload_keeps_the_gamedata_folder_intact(self):
        """End-to-end: moving a lone-"gamedata"-folder payload must leave

        "gamedata" as a real top-level folder in the destination, not
        flatten its contents into the mod's root.
        """
        from commander_gui.mod_install import move_payload

        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            (staging / "gamedata" / "scripts").mkdir(parents=True)
            (staging / "gamedata" / "scripts" / "foo.script").write_text("x")
            destination = Path(tmp) / "mods" / "My Mod"
            move_payload(staging, destination)
            self.assertTrue((destination / "gamedata" / "scripts" / "foo.script").is_file())
            self.assertFalse((destination / "scripts").exists())

    def test_write_basic_meta_ini_writes_minimal_metadata_and_never_overwrites(self):
        from commander_gui.mod_install import write_basic_meta_ini

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "My Mod"
            destination.mkdir()
            write_basic_meta_ini(destination, "MyMod-1.0.zip")
            content = (destination / "meta.ini").read_text(encoding="utf-8")
            self.assertIn("gameName=stalkeranomaly", content)
            self.assertIn("installationFile=MyMod-1.0.zip", content)

            (destination / "meta.ini").write_text("[General]\nkeepme=1\n", encoding="utf-8")
            write_basic_meta_ini(destination, "MyMod-1.0.zip")
            self.assertEqual(
                (destination / "meta.ini").read_text(encoding="utf-8"),
                "[General]\nkeepme=1\n",
            )

    def test_fomod_required_install_files_only_parses_and_installs_without_steps(self):
        """Regression test for the reported "This FOMOD has no supported

        installation steps" error: a FOMOD that only has
        <requiredInstallFiles> and no <installStep> elements at all is a
        real, legitimate shape (common for small, no-choice mods) that
        must parse and install successfully, not raise.
        """
        from commander_gui.fomod import apply_options, parse_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "gamedata").mkdir(parents=True)
            (root / "gamedata" / "test.txt").write_text("x")
            fomod_dir = root / "fomod"
            fomod_dir.mkdir()
            (fomod_dir / "ModuleConfig.xml").write_text(
                "<config>"
                "<moduleName>Test Mod</moduleName>"
                "<requiredInstallFiles>"
                '<folder source="gamedata" destination=""/>'
                "</requiredInstallFiles>"
                "</config>",
                encoding="utf-8",
            )
            config = parse_config(fomod_dir / "ModuleConfig.xml")
            self.assertEqual(config.steps, ())
            self.assertEqual(len(config.required_files), 1)

            destination = Path(tmp) / "selected"
            apply_options(config, root, destination, {})
            # destination="" merges the source folder's own CONTENTS at
            # the mod root - "gamedata" is itself just this FOMOD's
            # source folder name here, not preserved as a wrapper (see
            # test_fomod_folder_with_wrapper_source_name_merges_its_contents,
            # confirmed against a real archive that relies on exactly
            # this to correctly land ITS "gamedata" at the mod root).
            self.assertTrue((destination / "test.txt").is_file())

    def test_fomod_no_steps_and_no_required_files_still_raises(self):
        from commander_gui.fomod import parse_config
        from commander_gui.mod_install import ModInstallError

        with tempfile.TemporaryDirectory() as tmp:
            fomod_dir = Path(tmp) / "fomod"
            fomod_dir.mkdir()
            (fomod_dir / "ModuleConfig.xml").write_text(
                "<config><moduleName>Empty</moduleName></config>", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ModInstallError, "no supported installation steps"
            ):
                parse_config(fomod_dir / "ModuleConfig.xml")

    def test_fomod_omitted_destination_preserves_source_subfolder(self):
        """Regression test for the other half of the reported red-X bug:

        a <file>/<folder> that omits its destination attribute entirely
        must default to the source's own path (preserving subfolders),
        not flatten to the mod's root - a very common FOMOD shorthand
        that used to strip the "gamedata/..." prefix away entirely.
        """
        from commander_gui.fomod import apply_options, parse_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "gamedata" / "scripts").mkdir(parents=True)
            (root / "gamedata" / "scripts" / "foo.script").write_text("x")
            fomod_dir = root / "fomod"
            fomod_dir.mkdir()
            (fomod_dir / "ModuleConfig.xml").write_text(
                "<config>"
                "<moduleName>Test Mod</moduleName>"
                "<requiredInstallFiles>"
                '<file source="gamedata/scripts/foo.script"/>'
                "</requiredInstallFiles>"
                "</config>",
                encoding="utf-8",
            )
            config = parse_config(fomod_dir / "ModuleConfig.xml")
            destination = Path(tmp) / "selected"
            apply_options(config, root, destination, {})
            self.assertTrue(
                (destination / "gamedata" / "scripts" / "foo.script").is_file()
            )
            self.assertFalse((destination / "foo.script").exists())

    def test_fomod_explicit_empty_destination_still_flattens_to_root(self):
        """An *explicit* destination="" (as opposed to an omitted

        attribute) must keep today's existing, already-correct behavior:
        placed at the mod root, not preserving the source's subfolder.
        """
        from commander_gui.fomod import apply_options, parse_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "gamedata" / "scripts").mkdir(parents=True)
            (root / "gamedata" / "scripts" / "foo.script").write_text("x")
            fomod_dir = root / "fomod"
            fomod_dir.mkdir()
            (fomod_dir / "ModuleConfig.xml").write_text(
                "<config>"
                "<moduleName>Test Mod</moduleName>"
                "<requiredInstallFiles>"
                '<file source="gamedata/scripts/foo.script" destination=""/>'
                "</requiredInstallFiles>"
                "</config>",
                encoding="utf-8",
            )
            config = parse_config(fomod_dir / "ModuleConfig.xml")
            destination = Path(tmp) / "selected"
            apply_options(config, root, destination, {})
            self.assertTrue((destination / "foo.script").is_file())

    def test_fomod_folder_with_wrapper_source_name_merges_its_contents(self):
        """Regression test for a real reported bug/archive: QAW Overhaul

        Gwnf5066 1.0.2 duplicated its gamedata folder ("gamedata/
        gamedata"). Confirmed via the actual archive's ModuleConfig.xml:
        every <folder> entry uses a meaningless step-label source name
        ("00 - Core") that itself contains the real "gamedata" folder,
        with destination="". A <folder> entry installs the SOURCE
        FOLDER'S CONTENTS at the destination, not the folder itself
        nested under its own name - "00 - Core" must be discarded
        entirely, landing a single "gamedata" at the mod root, not
        "00 - Core/gamedata".
        """
        from commander_gui.fomod import apply_options, parse_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "00 - Core" / "gamedata" / "scripts").mkdir(parents=True)
            (root / "00 - Core" / "gamedata" / "scripts" / "foo.script").write_text("x")
            fomod_dir = root / "fomod"
            fomod_dir.mkdir()
            (fomod_dir / "ModuleConfig.xml").write_text(
                "<config>"
                "<moduleName>Test Mod</moduleName>"
                "<requiredInstallFiles>"
                '<folder source="00 - Core" destination=""/>'
                "</requiredInstallFiles>"
                "</config>",
                encoding="utf-8",
            )
            config = parse_config(fomod_dir / "ModuleConfig.xml")
            destination = Path(tmp) / "selected"
            apply_options(config, root, destination, {})
            self.assertTrue(
                (destination / "gamedata" / "scripts" / "foo.script").is_file()
            )
            self.assertFalse((destination / "00 - Core").exists())
            self.assertFalse(
                (destination / "gamedata" / "gamedata").exists()
            )

    def test_fomod_folder_with_explicit_named_destination_still_renames(self):
        """A <folder> whose destination is an explicit non-empty path

        still places the source's contents under that path (not root,
        not the source's own name) - only an empty/omitted destination
        means "merge at the mod root".
        """
        from commander_gui.fomod import apply_options, parse_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "00 - Core" / "file.txt").parent.mkdir(parents=True)
            (root / "00 - Core" / "file.txt").write_text("x")
            fomod_dir = root / "fomod"
            fomod_dir.mkdir()
            (fomod_dir / "ModuleConfig.xml").write_text(
                "<config>"
                "<moduleName>Test Mod</moduleName>"
                "<requiredInstallFiles>"
                '<folder source="00 - Core" destination="gamedata"/>'
                "</requiredInstallFiles>"
                "</config>",
                encoding="utf-8",
            )
            config = parse_config(fomod_dir / "ModuleConfig.xml")
            destination = Path(tmp) / "selected"
            apply_options(config, root, destination, {})
            self.assertTrue((destination / "gamedata" / "file.txt").is_file())

    def test_cli_version_survives_non_utf8_output(self):
        """Regression test: a CLI emitting invalid UTF-8 must not abort the

        diagnostics export - UnicodeDecodeError is a ValueError, so it used
        to escape _cli_version()'s (OSError, TimeoutExpired) handler.
        """
        from commander_gui import diagnostics as diagnostics_module

        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "stalker-gamma"
            cli.write_text("#!/bin/sh\nprintf '\\xff\\xfeversion 1.0\\n'\n")
            cli.chmod(0o755)
            with patch.object(
                diagnostics_module, "cli_binary_path", return_value=cli
            ):
                output = diagnostics_module._cli_version()
        self.assertIn("version 1.0", output)

    def test_diagnostics_redacts_sensitive_values(self):
        text = '{"ApiToken": "secret", "ProfileName": "gamma"}'
        redacted = _redact(text)
        self.assertNotIn("secret", redacted)
        self.assertIn("[REDACTED]", redacted)

    @patch("commander_gui.ui.common.shutil.which", return_value="/usr/bin/pgrep")
    @patch("commander_gui.ui.common.subprocess.run")
    def test_mo2_running_uses_self_excluding_case_insensitive_pattern(
        self, run, _which
    ):
        """Regression test: a bracket expression on only the first letter

        (the previous pattern) does not catch Wine/umu-run reporting the
        running process's path in a different case entirely (e.g. fully
        lowercased) - the real-world failure this caused: MO2's handoff
        was never detected, so Total Playtime never got recorded even
        though the game and MO2 were genuinely running for the whole
        session (see commander.log evidence: "wrapper exited" followed by
        no "mo2 handoff detected" line at all, ever, for a real session).
        """
        common._MO2_RUNNING_CACHE = 0.0
        run.return_value = Mock(returncode=1)
        self.assertFalse(common.mo2_running())
        self.assertEqual(run.call_args.args[0], [
            "/usr/bin/pgrep", "-if", r"ModOrganizer\.exe"
        ])

    def test_diagnostics_redacts_quoted_values_with_spaces(self):
        redacted = _redact(
            'password = "secret phrase"\nsecret: \'another secret\'\n'
        )
        self.assertNotIn("secret phrase", redacted)
        self.assertNotIn("another secret", redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 2)

    def test_launch_detached_rejects_empty_command(self):
        with self.assertRaises(LaunchError):
            launch_detached([], {}, "")

    @patch("commander_gui.launcher.subprocess.Popen")
    def test_launch_detached_removes_secrets_from_child_environment(self, popen):
        popen.return_value = Mock()
        with patch.dict(
            "os.environ",
            {
                "COMMANDER_TOKEN": "inherited",
                "WINEPREFIX": "/stale/prefix",
                "PROTONPATH": "/stale/proton",
                "SteamGameId": "stale",
                "Visible": "base",
            },
            clear=True,
        ):
            launch_detached(
                ["not-a-real-launch"],
                {"API_KEY": "supplied", "Visible": "override"},
                ".",
            )

        child_env = popen.call_args.kwargs["env"]
        self.assertNotIn("COMMANDER_TOKEN", child_env)
        self.assertNotIn("WINEPREFIX", child_env)
        self.assertNotIn("PROTONPATH", child_env)
        self.assertNotIn("SteamGameId", child_env)
        self.assertNotIn("API_KEY", child_env)
        self.assertEqual(child_env["Visible"], "override")
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    @patch("commander_gui.assistant_launcher.subprocess.Popen")
    def test_assistant_launch_uses_devnull_stdin_and_rejects_duplicate(self, popen):
        process = Mock()
        process.poll.return_value = None
        popen.return_value = process
        assistant_launcher._assistant_processes.clear()
        try:
            with patch.object(
                assistant_launcher,
                "assistant_command",
                return_value=(["assistant-test"], Path(".")),
            ):
                self.assertIs(launch_assistant(), process)
                self.assertIs(assistant_launcher.active_assistant_process(), process)
                with self.assertRaises(assistant_launcher.AssistantLaunchError):
                    launch_assistant()

            self.assertEqual(popen.call_count, 1)
            self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        finally:
            assistant_launcher._assistant_processes.clear()

    def test_assistant_process_reaping_removes_completed_handles(self):
        active = Mock()
        active.poll.return_value = None
        completed = Mock()
        completed.poll.return_value = 0
        assistant_launcher._assistant_processes[:] = [active, completed]

        self.assertEqual(assistant_launcher.reap_assistant_processes(), [completed])
        self.assertEqual(assistant_launcher._assistant_processes, [active])
        assistant_launcher._assistant_processes.clear()

    @patch("commander_gui.launcher._terminate_process_group")
    def test_process_registry_cleanup_removes_handles(self, terminate_group):
        process = Mock()
        process.pid = 4321
        registry = ProcessGroupRegistry()
        registry.register(process)

        registry.cleanup_all()

        terminate_group.assert_called_once_with(4321, process)

    def test_play_page_launch_guards_do_not_start_duplicate_actions(self):
        from commander_gui.ui.play_page import PlayPage

        page = PlayPage.__new__(PlayPage)
        page._launching = True
        page._install_busy = False
        page._run = Mock()

        page.launch_game()
        page._open_mo2()
        page._launch_direct()

        page._run.assert_not_called()
    def _make_play_page_stub(self):
        from commander_gui.ui.play_page import PlayPage

        page = PlayPage.__new__(PlayPage)
        page._launching = True
        page._install_busy = False
        page._proc = None
        page._launch_timer = None
        page._monitoring_mo2 = True
        page._mo2_seen = False
        page._handoff_checks = 0
        page._pre_launch_mo2_pids = set()
        page._mo2_launch_pids = set()
        page._game_exe_name = None
        page._pre_launch_game_pids = set()
        page._game_seen = False
        page._crash_check_pending = False
        page._pre_launch_crash_dumps = set()
        page._launch_wrapper_pid = None
        page._stall_warned = True
        page._launch_started_at = None
        page._registry = ProcessGroupRegistry()
        page._set_result = Mock()
        page._set_launch_button_state = Mock(
            side_effect=lambda launching: setattr(page, "_launching", launching)
        )
        page._launch_status_clear_timer = Mock()
        page._launch_timer = Mock()
        page._refresh_preview = Mock()
        # Bare PlayPage.__new__() has no real QWidget backing to parent a
        # QMessageBox on - these stub-based tests exercise the launch-
        # monitoring state machine, not the crash-report prompt.
        page._offer_crash_report = Mock()
        page._record_playtime = Mock()
        page._discord_rpc = None
        return page

    @patch("commander_gui.ui.play_page.mo2_pids", return_value={111})
    def test_mo2_handoff_detects_running_mo2(self, _mo2_pids):
        page = self._make_play_page_stub()

        page._on_launch_check("GAMMA", ["umu-run", "mo2"], Path("/tmp/launcher.log"))

        self.assertTrue(page._mo2_seen)
        self.assertEqual(page._mo2_launch_pids, {111})
        self.assertEqual(page._handoff_checks, -1)
        page._set_result.assert_called_with("MO2 is running...")
        page._set_launch_button_state.assert_not_called()

    @patch("commander_gui.ui.play_page.mo2_pids", return_value=set())
    def test_mo2_handoff_waits_during_startup_window(self, _mo2_pids):
        page = self._make_play_page_stub()

        for _ in range(10):
            page._on_launch_check("GAMMA", ["umu-run"], Path("/tmp/launcher.log"))

        self.assertTrue(page._monitoring_mo2)
        self.assertEqual(page._handoff_checks, 10)
        page._set_launch_button_state.assert_not_called()

    @patch("commander_gui.ui.play_page.mo2_pids", return_value=set())
    def test_mo2_handoff_treats_wrapper_exit_with_no_mo2_seen_as_normal_close(
        self, _mo2_pids
    ):
        """Regression test: on runner setups where the wrapper (e.g.

        umu-run/GE-Proton) blocks until the whole Wine session - MO2
        included - has already closed, MO2 never appears as a "new"
        process within the handoff window (it already came and went with
        the wrapper) - this used to be reported as an error ("launcher
        exited before MO2 was detected") and, critically, never called
        _record_playtime(), so Total Playtime was never recorded for a
        session that actually ran successfully. Confirmed via a real
        user's commander.log: every genuine play session ended in this
        exact "MO2 never detected" branch, every time.
        """
        page = self._make_play_page_stub()
        page._handoff_checks = 10

        page._on_launch_check("GAMMA", ["umu-run"], Path("/tmp/launcher.log"))

        self.assertFalse(page._monitoring_mo2)
        self.assertFalse(page._launching)
        page._record_playtime.assert_called_once()
        page._set_result.assert_called_with("GAMMA closed normally.", error=False)
        page._launch_status_clear_timer.start.assert_called_once_with(3000)

    @patch("commander_gui.ui.play_page.mo2_pids", return_value=set())
    def test_mo2_handoff_reports_normal_close_after_mo2_exits(self, _mo2_pids):
        page = self._make_play_page_stub()
        page._mo2_seen = True
        page._mo2_launch_pids = {111}

        page._on_launch_check("GAMMA", ["umu-run"], Path("/tmp/launcher.log"))

        self.assertFalse(page._monitoring_mo2)
        self.assertFalse(page._launching)
        page._set_result.assert_called_with("GAMMA closed normally.", error=False)
        page._launch_status_clear_timer.start.assert_called_once_with(3000)

    @patch("commander_gui.ui.play_page.mo2_pids", return_value={111, 222})
    def test_mo2_handoff_ignores_a_pre_existing_unrelated_mo2_window(
        self, _mo2_pids
    ):
        """A stale MO2 window open before launch must not block detection."""
        page = self._make_play_page_stub()
        page._pre_launch_mo2_pids = {222}

        page._on_launch_check("GAMMA", ["umu-run"], Path("/tmp/launcher.log"))

        self.assertTrue(page._mo2_seen)
        self.assertEqual(page._mo2_launch_pids, {111})

    @patch("commander_gui.ui.play_page.mo2_pids", return_value={222})
    def test_mo2_handoff_survives_closing_only_the_launched_instance(
        self, _mo2_pids
    ):
        """Closing this launch's MO2 finishes even if a stale one lingers."""
        page = self._make_play_page_stub()
        page._mo2_seen = True
        page._mo2_launch_pids = {111}
        page._pre_launch_mo2_pids = {222}

        page._on_launch_check("GAMMA", ["umu-run"], Path("/tmp/launcher.log"))

        self.assertFalse(page._monitoring_mo2)
        page._set_result.assert_called_with("GAMMA closed normally.", error=False)

    @patch("commander_gui.ui.play_page.mo2_running", return_value=False)
    def test_launch_wrapper_success_keeps_monitoring_for_mo2(self, _mo2_running):
        page = self._make_play_page_stub()
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0
        process.pid = 4321
        page._proc = process
        page._registry.register(process)

        page._on_launch_check("GAMMA", ["umu-run"], Path("/tmp/launcher.log"))

        self.assertIsNone(page._proc)
        self.assertTrue(page._monitoring_mo2)
        self.assertNotIn(process.pid, page._registry._processes)
        page._set_result.assert_called_with("Launcher exited; waiting for MO2...")
        page._set_launch_button_state.assert_not_called()

    def test_launch_wrapper_failure_discards_registry_and_reports(self):
        page = self._make_play_page_stub()
        page._monitoring_mo2 = False
        process = Mock()
        process.poll.return_value = 1
        process.returncode = 1
        process.pid = 4322
        page._proc = process
        page._registry.register(process)

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "launcher.log"
            log_path.write_text("boom\n", encoding="utf-8")
            with patch("commander_gui.ui.play_page.QMessageBox.warning") as warning:
                page._on_launch_check("GAMMA", ["umu-run"], log_path)

        self.assertIsNone(page._proc)
        self.assertNotIn(process.pid, page._registry._processes)
        self.assertFalse(page._launching)
        warning.assert_called_once()
        result = page._set_result.call_args.args[0]
        self.assertIn("exited with an error (code 1)", result)
        self.assertIn("boom", result)
        page._launch_status_clear_timer.start.assert_called_once_with(3000)

    def test_abort_launch_cleans_spawned_process_group(self):
        page = self._make_play_page_stub()
        process = Mock()
        process.pid = 9876
        process.poll.return_value = None
        page._proc = process
        page._registry.register(process)

        with patch("commander_gui.launcher._terminate_process_group") as terminate:
            page._abort_launch("Unexpected launch error: boom")

        terminate.assert_called_once()
        self.assertEqual(terminate.call_args.args[0], process.pid)
        self.assertIsNone(page._proc)
        self.assertFalse(page._launching)
        self.assertFalse(page._monitoring_mo2)
        self.assertNotIn(process.pid, page._registry._processes)
        page._set_result.assert_called_with(
            "Unexpected launch error: boom", error=True
        )

    def test_abort_launch_falls_back_to_the_remembered_wrapper_pid(self):
        """Regression test: once MO2 takes over, the wrapper process can

        exit on its own (self._proc becomes None) while MO2/the game keep
        running under the same process group - Quit Game/abort must still
        be able to kill them via the pid remembered at launch time.
        """
        page = self._make_play_page_stub()
        page._proc = None
        page._launch_wrapper_pid = 4321

        with patch("commander_gui.ui.play_page._terminate_process_group") as terminate:
            page._abort_launch("Game closed by user.")

        terminate.assert_called_once_with(4321)
        self.assertIsNone(page._launch_wrapper_pid)

    def test_abort_launch_does_nothing_extra_with_no_process_and_no_pid(self):
        page = self._make_play_page_stub()
        page._proc = None
        page._launch_wrapper_pid = None

        with patch("commander_gui.ui.play_page._terminate_process_group") as terminate:
            page._abort_launch("boom")

        terminate.assert_not_called()

    def test_play_page_persist_dirs_expands_tilde(self):
        from commander_gui.ui.play_page import PlayPage

        page = PlayPage.__new__(PlayPage)
        page._persisting = False
        page.window = Mock()
        page.window.install_busy = False
        profile = CliProfile(anomaly="old-a", gamma="old-g", cache="old-c")
        page.window.settings = Mock(active_profile=profile, save=Mock())
        page.window.statusBar.return_value.showMessage = Mock()
        page.anomaly_edit = Mock(text=Mock(return_value="~/anomaly"))
        page.gamma_edit = Mock(text=Mock(return_value="~/gamma"))
        page.cache_edit = Mock(text=Mock(return_value="~/cache"))
        page._reload_targets = Mock()
        page._refresh_preview = Mock()
        page._update_cache_info = Mock()

        with patch("commander_gui.ui.play_page.mo2_running", return_value=False):
            page._persist_dirs()

        self.assertNotIn("~", profile.anomaly)
        self.assertTrue(Path(profile.anomaly).is_absolute())
        page.anomaly_edit.setText.assert_called_with(profile.anomaly)

    def test_play_page_persist_dirs_refuses_while_busy(self):
        from commander_gui.ui.play_page import PlayPage

        page = PlayPage.__new__(PlayPage)
        page._persisting = False
        page.window = Mock()
        page.window.install_busy = True
        page._load_folders = Mock()

        with patch("commander_gui.ui.play_page.QMessageBox.warning") as warning:
            page._persist_dirs()

        warning.assert_called_once()
        page._load_folders.assert_called_once()
        page.window.settings.save.assert_not_called()

    def test_cancel_full_install_rechecks_after_dialog_to_avoid_crash(self):
        from PySide6.QtWidgets import QMessageBox

        from commander_gui.ui.install_page import InstallPage

        page = InstallPage.__new__(InstallPage)
        runner = Mock()
        runner.is_running.return_value = True
        page._runner = runner
        page._anomaly_runner = None
        page._post_anomaly_verify_runner = None
        page.full_progress = Mock()
        page.full_progress.is_paused = False

        def _answer_yes(*_args, **_kwargs):
            # Simulate the install finishing (clearing the runner) while the
            # confirm dialog was open - must not crash on the stale runner.
            page._runner = None
            return QMessageBox.StandardButton.Yes

        with patch(
            "commander_gui.ui.install_page.QMessageBox.question",
            side_effect=_answer_yes,
        ):
            page._cancel_full_install()

        runner.resume.assert_not_called()
        runner.cancel.assert_not_called()

    def test_cancel_winetricks_rechecks_after_dialog_to_avoid_crash(self):
        from PySide6.QtWidgets import QMessageBox

        from commander_gui.ui.install_page import InstallPage

        page = InstallPage.__new__(InstallPage)
        runner = Mock()
        runner.is_running.return_value = True
        page._wt_runner = runner
        page.wt_progress = Mock()

        def _answer_yes(*_args, **_kwargs):
            page._wt_runner = None
            return QMessageBox.StandardButton.Yes

        with patch(
            "commander_gui.ui.install_page.QMessageBox.question",
            side_effect=_answer_yes,
        ):
            page._cancel_winetricks()

        runner.cancel.assert_not_called()

    def test_start_full_install_creates_anomaly_folder_too(self):
        """Install GAMMA directly on a brand-new profile must not fail for a
        missing Anomaly folder - _start_anomaly_install already creates it,
        full-install must do the same since it installs Anomaly first too."""
        import tempfile

        from PySide6.QtWidgets import QMessageBox

        from commander_gui.settings import CliProfile
        from commander_gui.ui.install_page import InstallPage

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profile = CliProfile(
                active=True,
                profile_name="test",
                anomaly=str(base / "anomaly"),
                gamma=str(base / "gamma"),
                cache=str(base / "cache"),
            )
            page = InstallPage.__new__(InstallPage)
            page._runner = None
            page.window = Mock()
            page.window.install_busy = False
            page.window.settings = Mock(active_profile=profile)
            page.checkboxes = {
                "minimal": Mock(isChecked=Mock(return_value=False)),
                "preserve_user": Mock(isChecked=Mock(return_value=False)),
                "preserve_mcm": Mock(isChecked=Mock(return_value=False)),
            }
            page._resume_state = None
            page.full_progress = Mock()
            page.install_button = Mock()
            page.anomaly_button = Mock()
            page._checked_archives = set()
            page._cache_timer = Mock()

            with (
                patch(
                    "commander_gui.ui.install_page.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
                patch(
                    "commander_gui.ui.install_page.cli_command",
                    return_value=["stalker-gamma", "full-install"],
                ),
                patch("commander_gui.ui.install_page.CommandRunner") as runner_cls,
            ):
                runner_cls.return_value = Mock()
                page._start_full_install()

            self.assertTrue((base / "anomaly").is_dir())
            self.assertTrue((base / "gamma").is_dir())
            self.assertTrue((base / "cache").is_dir())

    def test_start_anomaly_install_uses_the_shared_progress_console(self):
        """Regression test: Anomaly and GAMMA share one progress console

        (full_progress) on the merged Install page card - a standalone
        Anomaly install must drive that shared console, not a separate
        widget, so its progress actually shows up where the user is
        looking.
        """
        import tempfile

        from PySide6.QtWidgets import QMessageBox

        from commander_gui.settings import CliProfile
        from commander_gui.ui.install_page import InstallPage

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profile = CliProfile(
                active=True,
                profile_name="test",
                anomaly=str(base / "anomaly"),
                gamma=str(base / "gamma"),
                cache=str(base / "cache"),
            )
            page = InstallPage.__new__(InstallPage)
            page._anomaly_runner = None
            page.window = Mock()
            page.window.install_busy = False
            page.window.settings = Mock(active_profile=profile)
            page.full_progress = Mock()
            page.install_button = Mock()
            page.anomaly_button = Mock()
            page.anomaly_checkboxes = {
                "verify_after_install": Mock(isChecked=Mock(return_value=False)),
                "auto_continue_gamma": Mock(isChecked=Mock(return_value=False)),
            }

            with (
                patch(
                    "commander_gui.ui.install_page.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
                patch(
                    "commander_gui.ui.install_page.cli_command",
                    return_value=["stalker-gamma", "anomaly", "install"],
                ),
                patch("commander_gui.ui.install_page.CommandRunner") as runner_cls,
            ):
                runner_cls.return_value = Mock()
                page._start_anomaly_install()

            page.full_progress.reset.assert_called_once()
            page.full_progress.on_started.assert_called_once()
            page.full_progress.set_runner.assert_called_once_with(page._anomaly_runner)
            connected_targets = [
                call.args[0] for call in page._anomaly_runner.line.connect.call_args_list
            ]
            self.assertIn(page.full_progress.on_line, connected_targets)
            self.assertFalse(hasattr(page, "anomaly_progress"))

    def _make_bare_install_page(self, tmp):
        from commander_gui.settings import CliProfile
        from commander_gui.ui.install_page import InstallPage

        base = Path(tmp)
        profile = CliProfile(
            active=True,
            profile_name="test",
            anomaly=str(base / "anomaly"),
            gamma=str(base / "gamma"),
            cache=str(base / "cache"),
        )
        page = InstallPage.__new__(InstallPage)
        page._runner = None
        page._anomaly_runner = None
        page._post_anomaly_verify_runner = None
        page._post_anomaly_verify_cancelled = False
        page._verify_anomaly_after_install = False
        page._auto_chain = False
        page._auto_cancelled = False
        page.window = Mock()
        page.window.install_busy = False
        page.window.settings = Mock(active_profile=profile)
        page.full_progress = Mock()
        page.anomaly_status = Mock()
        page.gamma_status = Mock()
        page.anomaly_button = Mock()
        page.install_button = Mock()
        return page

    def test_auto_continue_checkbox_chains_into_gamma_on_success(self):
        """Regression test: the new "Continue to GAMMA install

        automatically when done" checkbox must actually chain into
        _start_full_install after a successful Anomaly install.
        """
        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_bare_install_page(tmp)
            page.anomaly_checkboxes = {
                "verify_after_install": Mock(isChecked=Mock(return_value=False)),
                "auto_continue_gamma": Mock(isChecked=Mock(return_value=True)),
            }
            with (
                patch("commander_gui.ui.install_page.CommandRunner"),
                patch.object(page, "_update_install_status"),
            ):
                page._start_anomaly_install(skip_confirm=True)
                page._anomaly_runner.was_cancelled = False
                with patch.object(page, "_start_full_install") as mock_start_full:
                    page._on_anomaly_finished(0, "Anomaly install complete!")
            mock_start_full.assert_called_once_with(skip_confirm=True)

    def test_auto_continue_checkbox_unchecked_does_not_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_bare_install_page(tmp)
            page.anomaly_checkboxes = {
                "verify_after_install": Mock(isChecked=Mock(return_value=False)),
                "auto_continue_gamma": Mock(isChecked=Mock(return_value=False)),
            }
            with (
                patch("commander_gui.ui.install_page.CommandRunner"),
                patch.object(page, "_update_install_status"),
            ):
                page._start_anomaly_install(skip_confirm=True)
                with patch.object(page, "_start_full_install") as mock_start_full:
                    page._on_anomaly_finished(0, "Anomaly install complete!")
            mock_start_full.assert_not_called()
            page.window.set_install_busy.assert_called_with(False)

    def test_verify_after_install_checkbox_runs_a_second_anomaly_check(self):
        """Regression test: the "Verify files after install" checkbox

        must run a second `anomaly check` CommandRunner after a
        successful install, before the sequence is considered finished.
        """
        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_bare_install_page(tmp)
            page.anomaly_checkboxes = {
                "verify_after_install": Mock(isChecked=Mock(return_value=True)),
                "auto_continue_gamma": Mock(isChecked=Mock(return_value=False)),
            }
            with (
                patch("commander_gui.ui.install_page.CommandRunner"),
                patch.object(page, "_update_install_status"),
            ):
                page._start_anomaly_install(skip_confirm=True)
                page._anomaly_runner.was_cancelled = False
                with patch.object(
                    page, "_start_post_anomaly_verify"
                ) as mock_start_verify:
                    page._on_anomaly_finished(0, "Anomaly install complete!")
            mock_start_verify.assert_called_once()
            # The sequence must not be considered finished yet - the only
            # set_install_busy call so far is _start_anomaly_install's own
            # (True, "anomaly"), not a finishing (False).
            page.window.set_install_busy.assert_called_once_with(True, "anomaly")

    def test_verify_after_install_unchecked_skips_the_extra_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_bare_install_page(tmp)
            page.anomaly_checkboxes = {
                "verify_after_install": Mock(isChecked=Mock(return_value=False)),
                "auto_continue_gamma": Mock(isChecked=Mock(return_value=False)),
            }
            with (
                patch("commander_gui.ui.install_page.CommandRunner"),
                patch.object(page, "_update_install_status"),
            ):
                page._start_anomaly_install(skip_confirm=True)
                with patch.object(
                    page, "_start_post_anomaly_verify"
                ) as mock_start_verify:
                    page._on_anomaly_finished(0, "Anomaly install complete!")
            mock_start_verify.assert_not_called()
            page.window.set_install_busy.assert_called_with(False)

    def test_anomaly_install_notifies_only_when_the_window_is_not_active(self):
        """Regression test: a standalone (non-chained) Anomaly install

        finishing/failing must surface a desktop notification when the
        user has alt-tabbed away, mirroring the existing GAMMA install
        notification - but not when the "Continue to GAMMA install
        automatically" checkbox is chaining into GAMMA, since
        _on_full_finished's own notification covers that eventual outcome
        instead.
        """
        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_bare_install_page(tmp)
            page.anomaly_checkboxes = {
                "verify_after_install": Mock(isChecked=Mock(return_value=False)),
                "auto_continue_gamma": Mock(isChecked=Mock(return_value=False)),
            }
            with (
                patch("commander_gui.ui.install_page.CommandRunner"),
                patch.object(page, "_update_install_status"),
            ):
                page._start_anomaly_install(skip_confirm=True)
                page._anomaly_runner.was_cancelled = False
                page.window.isActiveWindow = Mock(return_value=True)
                with patch(
                    "commander_gui.ui.install_page.notify_desktop"
                ) as mock_notify:
                    page._on_anomaly_finished(0, "Anomaly install complete!")
                mock_notify.assert_not_called()

            page = self._make_bare_install_page(tmp)
            page.anomaly_checkboxes = {
                "verify_after_install": Mock(isChecked=Mock(return_value=False)),
                "auto_continue_gamma": Mock(isChecked=Mock(return_value=False)),
            }
            with (
                patch("commander_gui.ui.install_page.CommandRunner"),
                patch.object(page, "_update_install_status"),
            ):
                page._start_anomaly_install(skip_confirm=True)
                page._anomaly_runner.was_cancelled = False
                page.window.isActiveWindow = Mock(return_value=False)
                with patch(
                    "commander_gui.ui.install_page.notify_desktop"
                ) as mock_notify:
                    page._on_anomaly_finished(0, "Anomaly install complete!")
                mock_notify.assert_called_once()

    def test_anomaly_install_chaining_into_gamma_skips_its_own_notification(self):
        """Regression test: when the "Continue to GAMMA install

        automatically" checkbox chains the Anomaly install straight into
        GAMMA, the Anomaly step itself must stay quiet - only the
        eventual GAMMA finish (_on_full_finished) should notify, or the
        user gets two notifications for what feels like one action.
        """
        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_bare_install_page(tmp)
            page.anomaly_checkboxes = {
                "verify_after_install": Mock(isChecked=Mock(return_value=False)),
                "auto_continue_gamma": Mock(isChecked=Mock(return_value=True)),
            }
            page.window.isActiveWindow = Mock(return_value=False)
            with (
                patch("commander_gui.ui.install_page.CommandRunner"),
                patch.object(page, "_update_install_status"),
                patch.object(page, "_start_full_install") as mock_start_full,
                patch(
                    "commander_gui.ui.install_page.notify_desktop"
                ) as mock_notify,
            ):
                page._start_anomaly_install(skip_confirm=True)
                page._anomaly_runner.was_cancelled = False
                page._on_anomaly_finished(0, "Anomaly install complete!")
            mock_start_full.assert_called_once_with(skip_confirm=True)
            mock_notify.assert_not_called()

    def test_reset_auto_chain_ignores_the_anomaly_checkboxes(self):
        """Regression test: GAMMA Reset's own auto-chain

        (start_auto_install(include_anomaly=True)) must keep chaining
        into GAMMA regardless of what the Anomaly card's own checkboxes
        currently show - it drives its own _auto_chain, independent of
        the "respect_options" checkbox path used by a direct click.
        """
        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_bare_install_page(tmp)
            # Checkboxes both off - if the auto-chain were wrongly gated
            # by them, this would fail to chain.
            page.anomaly_checkboxes = {
                "verify_after_install": Mock(isChecked=Mock(return_value=False)),
                "auto_continue_gamma": Mock(isChecked=Mock(return_value=False)),
            }
            with (
                patch("commander_gui.ui.install_page.CommandRunner"),
                patch.object(page, "_update_install_status"),
            ):
                self.assertTrue(page.start_auto_install(include_anomaly=True))
                page._anomaly_runner.was_cancelled = False
                with patch.object(page, "_start_full_install") as mock_start_full:
                    page._on_anomaly_finished(0, "Anomaly install complete!")
            mock_start_full.assert_called_once_with(skip_confirm=True)

    def test_post_anomaly_verify_finished_resumes_the_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_bare_install_page(tmp)
            with patch.object(page, "_finish_anomaly_sequence") as mock_finish:
                page._on_post_anomaly_verify_finished(0, "OK")
            mock_finish.assert_called_once_with(False, True)
            self.assertIsNone(page._post_anomaly_verify_runner)

    def test_active_install_runner_recognizes_the_post_verify_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_bare_install_page(tmp)
            page._post_anomaly_verify_runner = Mock(is_running=Mock(return_value=True))
            self.assertIs(
                page._active_install_runner(), page._post_anomaly_verify_runner
            )

    def test_update_button_states_locks_the_anomaly_checkboxes_while_busy(self):
        """Regression test: _update_button_states disables every other

        control on the page while an install is busy (anomaly/install/
        verify/winetricks buttons, folder browse/edit, GAMMA's own
        checkboxes) but used to skip the Anomaly card's own two
        checkboxes entirely.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.settings import CliProfile
        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profile = CliProfile(
                active=True,
                profile_name="test",
                anomaly=str(base / "anomaly"),
                gamma=str(base / "gamma"),
                cache=str(base / "cache"),
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = True

                def refresh_settings(self):
                    pass

            page = InstallPage(FakeWindow())
            page._update_button_states()
            for cb in page.anomaly_checkboxes.values():
                self.assertFalse(cb.isEnabled())

            page.window.install_busy = False
            page._update_button_states()
            for cb in page.anomaly_checkboxes.values():
                self.assertTrue(cb.isEnabled())

    def test_start_post_anomaly_verify_shows_the_cancel_button(self):
        """Regression test: on_finished() (just called for the Anomaly

        install itself) hides the cancel button - _start_post_anomaly_verify
        must re-show it, or the optional post-install verify step runs
        with no way to cancel it even though the plumbing to cancel it
        already works.
        """
        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_bare_install_page(tmp)
            with patch("commander_gui.ui.install_page.CommandRunner"):
                page._start_post_anomaly_verify()
            page.full_progress.cancel_button.show.assert_called_once()
            page.full_progress.cancel_button.setEnabled.assert_called_once_with(True)

    def test_cancel_full_install_cancels_whichever_runner_is_active(self):
        """The shared console's one Cancel button must cancel an active

        Anomaly-only install too, not just a GAMMA full-install.
        """
        from PySide6.QtWidgets import QMessageBox

        from commander_gui.ui.install_page import InstallPage

        page = InstallPage.__new__(InstallPage)
        page._runner = None
        page._anomaly_runner = Mock(is_running=Mock(return_value=True))
        page.full_progress = Mock(is_paused=False)

        with patch(
            "commander_gui.ui.install_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            page._cancel_full_install()

        page._anomaly_runner.cancel.assert_called_once()

    def test_confirm_install_gamma_dialog_notes_resume_state(self):
        """The confirm dialog must say upfront if this run resumes a prior
        interrupted install, not only as a status-bar toast afterward."""
        from PySide6.QtWidgets import QMessageBox

        from commander_gui.settings import CliProfile
        from commander_gui.ui.install_page import InstallPage

        page = InstallPage.__new__(InstallPage)
        page._runner = None
        page.window = Mock()
        page.window.install_busy = False
        page.window.settings = Mock(
            active_profile=CliProfile(
                anomaly="/nonexistent/anomaly", gamma="/g", cache="/c"
            )
        )
        page.checkboxes = {
            "minimal": Mock(isChecked=Mock(return_value=False))
        }
        page._resume_state = {"profile": "test"}

        with patch(
            "commander_gui.ui.install_page.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ) as mock_question:
            page._start_full_install()

        dialog_text = mock_question.call_args[0][2]
        self.assertIn("Resuming a previously interrupted", dialog_text)

    def test_on_winetricks_line_suppresses_noise_from_visible_log(self):
        """A noise line must not reach the console, not just skip %-parsing."""
        from commander_gui.ui.install_page import InstallPage

        page = InstallPage.__new__(InstallPage)
        page.wt_progress = Mock()
        page._wt_stage = "verbs"
        page._wt_completed_verbs = set()
        page._wt_last_pct = -1

        page._on_winetricks_line("Using winetricks 20240105 - sha256sum: abc123")
        page.wt_progress.on_line.assert_not_called()

        page._on_winetricks_line("Executing w_do_call vcrun2022")
        page.wt_progress.on_line.assert_called_once()

    @patch("commander_gui.cli_runner.subprocess.Popen")
    def test_cli_worker_clears_process_handle_and_uses_process_group(self, popen):
        from commander_gui.cli_runner import CliWorker

        process = Mock()
        process.stdout = iter(["line\n"])
        process.returncode = 0
        process.poll.return_value = 0
        popen.return_value = process
        worker = CliWorker()
        finished = []
        worker.finished.connect(lambda rc, output: finished.append((rc, output)))
        worker.setup(["not-a-real-cli"])
        worker.run()

        self.assertIsNone(worker._process)
        self.assertEqual(finished, [(0, "line")])
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["start_new_session"], os.name != "nt")
        self.assertEqual(
            kwargs["creationflags"],
            subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0,
        )

    @patch("commander_gui.cli_runner.cli_binary_path", return_value=Path("/cli/stalker-gamma"))
    @patch("commander_gui.cli_runner._terminate_process_group")
    @patch("commander_gui.cli_runner.subprocess.Popen")
    def test_run_sync_timeout_terminates_group_and_returns_result(
        self, popen, terminate_group, _binary
    ):
        from commander_gui.cli_runner import TIMEOUT_RC, run_sync

        process = Mock()
        process.pid = 1234
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(
                ["/cli/stalker-gamma", "status"],
                2,
                output=b"partial output",
                stderr=b"diagnostic",
            ),
            ("partial output", "diagnostic"),
        ]
        popen.return_value = process
        rc, output = run_sync(["status"], timeout=2)

        self.assertEqual(rc, TIMEOUT_RC)
        self.assertIn("partial outputdiagnostic", output)
        self.assertIn("timed out after 2s", output)
        terminate_group.assert_called_once_with(process)
        self.assertEqual(popen.call_args.args[0], ["/cli/stalker-gamma", "status"])

    @patch("commander_gui.cli_runner.cli_binary_path", return_value=Path("/cli/stalker-gamma"))
    @patch("commander_gui.cli_runner.subprocess.Popen")
    def test_run_sync_timeout_before_spawn_uses_exception_output(self, popen, _binary):
        from commander_gui.cli_runner import TIMEOUT_RC, run_sync

        popen.side_effect = subprocess.TimeoutExpired(
            ["/cli/stalker-gamma", "status"],
            2,
            output=b"partial output",
            stderr=b"diagnostic",
        )
        rc, output = run_sync(["status"], timeout=2)

        self.assertEqual(rc, TIMEOUT_RC)
        self.assertIn("partial outputdiagnostic", output)
        self.assertIn("timed out after 2s", output)

    @patch("commander_gui.cli_runner.cli_binary_path", return_value=Path("/cli/stalker-gamma"))
    def test_cli_command_constructs_base_and_progress_arguments(self, _binary):
        from commander_gui.cli_runner import cli_command

        self.assertEqual(
            cli_command(["install", "--profile", "gamma"], progress_interval_ms=200),
            [
                "/cli/stalker-gamma",
                "install",
                "--profile",
                "gamma",
                "--progress-update-interval-ms",
                "200",
            ],
        )

    def test_build_command_constructs_runner_profile_and_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp)
            (gamma / "ModOrganizer.exe").touch()
            (gamma / "profiles" / "GAMMA").mkdir(parents=True)
            command, env, cwd = build_command(
                str(gamma),
                Runner("wine", "Test Wine", ["wine"], {"WINEPREFIX": "/prefix"}),
                profile="GAMMA",
                target="Anomaly (DX11)",
            )

        self.assertEqual(
            command,
            [
                "wine",
                str(gamma / "ModOrganizer.exe"),
                "-p",
                "GAMMA",
                "run",
                "-e",
                "Anomaly (DX11)",
            ],
        )
        self.assertEqual(env, {"WINEPREFIX": "/prefix"})
        self.assertEqual(cwd, str(gamma))

    def test_svg_noise_detector_suppresses_renderer_warnings(self):
        from PySide6.QtCore import QtMsgType

        from commander_gui.main import _is_svg_noise

        self.assertTrue(
            _is_svg_noise(
                QtMsgType.QtWarningMsg,
                "qt.svg: /usr/share/icons/x/mimetypes/application-zip.svg:1170:6: "
                "Could not resolve property: pattern1238",
            )
        )
        self.assertTrue(_is_svg_noise(QtMsgType.QtWarningMsg, "qt.svg: foo"))
        self.assertTrue(
            _is_svg_noise(QtMsgType.QtWarningMsg, "Could not resolve property: s1")
        )
        self.assertFalse(_is_svg_noise(QtMsgType.QtWarningMsg, "normal warning"))
        self.assertFalse(_is_svg_noise(QtMsgType.QtInfoMsg, "qt.svg: info passes"))

    def test_portal_noise_detector_suppresses_appid_registration_warning(self):
        from PySide6.QtCore import QtMsgType

        from commander_gui.main import _is_portal_noise

        self.assertTrue(
            _is_portal_noise(
                QtMsgType.QtWarningMsg,
                'Failed to register with host portal QDBusError('
                '"org.freedesktop.portal.Error.Failed", "Could not register '
                'app ID: App info not found for \'stalker-gamma-commander\'")',
            )
        )
        self.assertFalse(_is_portal_noise(QtMsgType.QtWarningMsg, "normal warning"))
        self.assertFalse(
            _is_portal_noise(
                QtMsgType.QtInfoMsg, "Failed to register with host portal"
            )
        )

    def test_modlist_status_ignores_stale_index(self):
        lines = ["+First"]
        self.assertEqual(set_status_at(lines, 5, False), lines)

    def test_modlist_rejects_control_characters(self):
        with self.assertRaises(ValueError):
            entries(["+Unsafe\tName"])
        with self.assertRaises(ValueError):
            add_mod([], "Unsafe\nName")
        with self.assertRaises(ValueError):
            add_category([], "Unsafe\x7fName")

    def test_modlist_rejects_path_components(self):
        for name in ("../escape", "nested/mod", r"nested\mod", ".", ".."):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    add_mod([], name)
                with self.assertRaises(ValueError):
                    add_category([], name)

    def test_add_mod_lands_uncategorized_at_the_end_of_the_file(self):
        # No default category anymore: a fresh install is appended to the
        # very end of the file, in no category. MO2 writes modlist.txt
        # with file-END as the lowest-priority mod (rendered at the
        # on-screen TOP - confirmed against MO2's own source, see
        # add_mod()'s docstring), so this is what actually lands a new
        # mod at the top of the list on screen, matching real MO2.
        self.assertEqual(add_mod([], "NewMod"), ["-NewMod"])
        self.assertEqual(
            add_mod(["-Audio_separator", "+Music"], "NewMod"),
            ["-Audio_separator", "+Music", "-NewMod"],
        )

    def test_add_mod_creates_named_category_and_places_mod(self):
        # A category's members sit immediately above its own separator
        # (see grouped()), so "Music" is Audio's member here, not "NewMod"
        # unless it's explicitly requested - which inserts it as the new
        # last member, right before the separator.
        self.assertEqual(
            add_mod(["+Music", "-Audio_separator"], "NewMod", category="Audio"),
            ["+Music", "-NewMod", "-Audio_separator"],
        )

    def test_add_mod_appends_to_end_of_existing_category(self):
        lines = ["+Old1", "+Old2", "-Audio_separator"]
        self.assertEqual(
            add_mod(lines, "NewMod", category="Audio"),
            ["+Old1", "+Old2", "-NewMod", "-Audio_separator"],
        )

    def test_add_mod_reuses_existing_extra_mods_separator(self):
        # Finding a category's separator (see _category_separator_index())
        # doesn't care about its enabled/disabled prefix, so an existing
        # "+Extra Mods_separator" is reused as-is, not duplicated.
        lines = ["+Visual_separator", "+Shaders", "+Extra Mods_separator"]
        self.assertEqual(
            add_mod(lines, "NewMod", category="Extra Mods"),
            ["+Visual_separator", "+Shaders", "-NewMod", "+Extra Mods_separator"],
        )

    def test_rename_mod_changes_display_name_only(self):
        self.assertEqual(
            rename_mod(["-Old Name", "+Keep"], "Old Name", "New Name"),
            ["-New Name", "+Keep"],
        )

    def test_rename_mod_preserves_status(self):
        self.assertEqual(rename_mod(["+Old Name"], "Old Name", "Renamed"), ["+Renamed"])

    def test_rename_mod_rejects_duplicate(self):
        with self.assertRaises(ValueError):
            rename_mod(["-First", "+Second"], "First", "Second")

    def test_rename_mod_rejects_separator_name(self):
        with self.assertRaises(ValueError):
            rename_mod(["-First"], "First", "Uncategorized_separator")

    def test_rename_mod_rejects_invalid_name(self):
        with self.assertRaises(ValueError):
            rename_mod(["-First"], "First", "nested/mod")

    def test_rename_mod_returns_same_list_when_missing(self):
        lines = ["-First"]
        self.assertEqual(rename_mod(lines, "Missing", "New Name"), lines)

    def test_rename_category_changes_the_separator_name_only(self):
        self.assertEqual(
            rename_category(
                ["+ModA", "-Old Category_separator", "+ModB"],
                "Old Category",
                "New Category",
            ),
            ["+ModA", "-New Category_separator", "+ModB"],
        )

    def test_rename_category_preserves_enabled_status(self):
        self.assertEqual(
            rename_category(["+Old_separator"], "Old", "New"),
            ["+New_separator"],
        )

    def test_rename_category_rejects_duplicate(self):
        with self.assertRaises(ValueError):
            rename_category(
                ["-First_separator", "+Mod", "-Second_separator"],
                "First",
                "Second",
            )

    def test_rename_category_rejects_uncategorized(self):
        with self.assertRaises(ValueError):
            rename_category(["-First_separator"], "First", "Uncategorized")
        with self.assertRaises(ValueError):
            rename_category(["-First_separator"], "First", "uncategorized")

    def test_rename_category_rejects_invalid_name(self):
        with self.assertRaises(ValueError):
            rename_category(["-First_separator"], "First", "nested/category")

    def test_rename_category_no_op_on_unchanged_name(self):
        lines = ["-First_separator"]
        self.assertEqual(rename_category(lines, "First", "First"), lines)

    def test_rename_category_returns_same_list_when_missing(self):
        lines = ["-First_separator"]
        self.assertEqual(rename_category(lines, "Missing", "New"), lines)

    def test_rename_category_strips_redundant_separator_suffix(self):
        self.assertEqual(
            rename_category(["-Old_separator"], "Old", "New_separator"),
            ["-New_separator"],
        )

    def test_reorder_to_original_restores_gamma_order_keeps_user_mods(self):
        current = [
            "-Audio_separator",
            "+SFX",
            "+MyUserMod",
            "+Music",
            "-Visual_separator",
            "+Shaders",
        ]
        original = [
            "-Audio_separator",
            "+Music",
            "+SFX",
            "-Visual_separator",
            "+Shaders",
        ]
        self.assertEqual(
            reorder_to_original(current, original),
            [
                "-Audio_separator",
                "+Music",
                "+MyUserMod",
                "+SFX",
                "-Visual_separator",
                "+Shaders",
            ],
        )

    def test_reorder_to_original_preserves_status_prefixes(self):
        current = ["+OldG1", "-OldG2"]
        original = ["+OldG2", "+OldG1"]
        self.assertEqual(reorder_to_original(current, original), ["-OldG2", "+OldG1"])

    def test_reorder_to_original_omits_deleted_gamma_mods(self):
        current = ["-Audio_separator", "-OldG1"]
        original = ["-Audio_separator", "+OldG1", "+DeletedG"]
        result = reorder_to_original(current, original)
        self.assertNotIn("DeletedG", result)

    def test_reorder_to_original_keeps_new_categories_in_place(self):
        current = [
            "-Audio_separator",
            "+Music",
            "+SFX",
            "-My New Category_separator",
            "+MyUserMod",
        ]
        original = [
            "-Audio_separator",
            "+SFX",
            "+Music",
        ]
        result = reorder_to_original(current, original)
        self.assertEqual(
            result,
            [
                "-Audio_separator",
                "+SFX",
                "+Music",
                "-My New Category_separator",
                "+MyUserMod",
            ],
        )

    def test_reorder_to_original_without_shared_mods_is_unchanged(self):
        lines = ["-UserMod"]
        self.assertEqual(reorder_to_original(lines, ["+Other", "+Gamma"]), lines)

    def test_reorder_to_original_handles_a_name_duplicated_in_the_reference_list(self):
        """Regression test: GAMMA's own official modlist.txt has been

        confirmed to list at least one mod name twice (see
        test_mod_manager_count_label_dedups_a_duplicate_name). A prior
        implementation paired ascending line positions against a
        separately-built target-name list via zip() - when the current
        modlist only had that name once (no literal duplicate line),
        the length mismatch silently misaligned every pairing after it,
        dropping a real mod ("C" below) and duplicating "B" instead of
        just reordering them.
        """
        original = ["+A", "+B", "+B", "+C"]
        current = ["+C", "+B", "+A", "+D"]
        result = reorder_to_original(current, original)
        self.assertEqual(result, ["+A", "+B", "+C", "+D"])

    def test_reorder_to_original_handles_a_name_duplicated_in_both_lists(self):
        original = ["+A", "+B", "+B", "+C"]
        current = ["+C", "+B", "+B", "+A"]
        result = reorder_to_original(current, original)
        self.assertEqual(result, ["+A", "+B", "+B", "+C"])

    def test_install_conflict_free_when_no_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            mods.mkdir()
            self.assertIsNone(install_conflict(["-Old"], mods, "New Mod"))

    def test_install_conflict_reports_listed_mod(self):
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            (mods / "Listed Mod").mkdir(parents=True)
            self.assertEqual(install_conflict(["-Listed Mod"], mods, "Listed Mod"), "listed")

    def test_install_conflict_reports_leftover_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            (mods / "Deleted From List").mkdir(parents=True)
            self.assertEqual(
                install_conflict(["-Other"], mods, "Deleted From List"),
                "leftover",
            )

    def test_install_conflict_treats_symlink_folder_as_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            target = Path(tmp) / "target"
            target.mkdir()
            mods.mkdir()
            (target / "file.txt").write_text("x", encoding="utf-8")
            (mods / "Symlinked Mod").symlink_to(target, target_is_directory=True)
            self.assertEqual(
                install_conflict([], mods, "Symlinked Mod"),
                "leftover",
            )

    def test_add_mod_rejects_status_prefix(self):
        with self.assertRaises(ValueError):
            add_mod([], "+LeadingPlus")
        with self.assertRaises(ValueError):
            add_mod([], "-LeadingDash")

    def test_rename_mod_rejects_status_prefix(self):
        with self.assertRaises(ValueError):
            rename_mod(["-First"], "First", "+Prefixed")
        with self.assertRaises(ValueError):
            rename_mod(["-First"], "First", "-Prefixed")

    def test_add_category_rejects_status_prefix(self):
        with self.assertRaises(ValueError):
            add_category([], "+Category")

    def test_flip_priority_keeps_trailing_uncategorized_mods_uncategorized(self):
        """Regression test: a trailing run of mods with no separator of its

        own ("Uncategorized" - e.g. any freshly-installed mod, since
        add_mod() defaults to appending at file-end) must not get silently
        absorbed into an unrelated category just because flipping relocates
        it next to that category's separator.
        """
        from commander_gui.modlist import flip_priority, grouped

        lines = [
            "+ModA",
            "+ModB",
            "-Foo_separator",
            "+ModC",
            "-Bar_separator",
            "+NewMod",
        ]
        before = dict(grouped(lines))
        self.assertEqual(
            [name for _status, name, _idx in before["Uncategorized"]], ["NewMod"]
        )

        flipped = flip_priority(lines)
        after = dict(grouped(flipped))
        self.assertIn("Uncategorized", after)
        self.assertEqual(
            [name for _status, name, _idx in after["Uncategorized"]], ["NewMod"]
        )
        # The two real categories still flip relative to each other.
        self.assertEqual(
            [name for _status, name, _idx in after["Bar"]], ["ModC"]
        )
        self.assertEqual(
            [name for _status, name, _idx in after["Foo"]], ["ModB", "ModA"]
        )

        # Flipping twice must round-trip back to the original file exactly.
        self.assertEqual(flip_priority(flipped), lines)

    def test_flip_priority_pins_the_gamma_end_of_list_category_first(self):
        """Regression test for a real reported crash: the user's actual

        modlist.txt has a "G.A.M.M.A. End of List_separator" category
        wrapping manually/MO2-added mods at file-start (= highest real
        MO2 priority = rendered at the *bottom*, i.e. literal end, of
        MO2's on-screen list - the name is not a coincidence). Reversing
        it like an ordinary category flips it to a near-lowest-priority
        position instead - the opposite of what its name promises, which
        plausibly broke the user's load order. It must stay pinned first,
        the same way the trailing Uncategorized run stays pinned last.
        """
        from commander_gui.modlist import flip_priority, grouped

        lines = [
            "+AnomalyTogether",
            "-SomeDisabledMod",
            "-G.A.M.M.A. End of List_separator",
            "+ModA",
            "+ModB",
            "-Foo_separator",
            "+ModC",
            "-Bar_separator",
            "+NewMod",
        ]
        flipped = flip_priority(lines)
        after = grouped(flipped)
        # Still first in file order (highest real priority) - not
        # reversed into last place among the real categories.
        self.assertEqual(after[0][0], "G.A.M.M.A. End of List")
        # Its own mod order still flips, like any other pinned block.
        self.assertEqual(
            [name for _status, name, _idx in after[0][1]],
            ["SomeDisabledMod", "AnomalyTogether"],
        )
        # The other two real categories still flip relative to each other.
        category_order = [category for category, _mods in after]
        self.assertEqual(
            category_order,
            ["G.A.M.M.A. End of List", "Bar", "Foo", "Uncategorized"],
        )

        # Flipping twice must round-trip back to the original file exactly.
        self.assertEqual(flip_priority(flipped), lines)

    def test_flip_priority_pins_the_end_of_list_category_case_insensitively(self):
        from commander_gui.modlist import flip_priority, grouped

        lines = [
            "+AnomalyTogether",
            "-g.a.m.m.a. end of list_separator",
            "+ModA",
            "-Foo_separator",
        ]
        after = grouped(flip_priority(lines))
        self.assertEqual(after[0][0], "g.a.m.m.a. end of list")

    def test_move_category_reorders_a_whole_block_with_its_members(self):
        from commander_gui.modlist import grouped, move_category

        lines = [
            "+ModA",
            "-Foo_separator",
            "+ModB",
            "-Bar_separator",
            "+NewMod",
        ]
        moved = move_category(lines, "Bar", "Foo", before=True)
        names = [name for name, _mods in grouped(moved)]
        self.assertEqual(names, ["Bar", "Foo", "Uncategorized"])
        # Members travel with their category.
        after = dict(grouped(moved))
        self.assertEqual([n for _s, n, _i in after["Bar"]], ["ModB"])
        self.assertEqual([n for _s, n, _i in after["Foo"]], ["ModA"])
        self.assertEqual([n for _s, n, _i in after["Uncategorized"]], ["NewMod"])

    def test_move_category_after_target(self):
        from commander_gui.modlist import grouped, move_category

        lines = ["+ModA", "-Foo_separator", "+ModB", "-Bar_separator"]
        moved = move_category(lines, "Foo", "Bar", before=False)
        names = [name for name, _mods in grouped(moved)]
        self.assertEqual(names, ["Bar", "Foo"])

    def test_move_category_next_to_uncategorized(self):
        from commander_gui.modlist import grouped, move_category

        lines = ["+ModA", "-Foo_separator", "+ModB", "-Bar_separator", "+NewMod"]
        moved = move_category(lines, "Foo", "Uncategorized", before=True)
        names = [name for name, _mods in grouped(moved)]
        self.assertEqual(names, ["Bar", "Foo", "Uncategorized"])

    def test_move_category_refuses_to_move_the_pinned_category(self):
        from commander_gui.modlist import move_category

        lines = [
            "+AnomalyTogether",
            "-G.A.M.M.A. End of List_separator",
            "+ModA",
            "-Foo_separator",
        ]
        with self.assertRaises(ValueError):
            move_category(lines, "G.A.M.M.A. End of List", "Foo", before=True)

    def test_move_category_refuses_to_target_before_the_pinned_category(self):
        from commander_gui.modlist import move_category

        lines = [
            "+AnomalyTogether",
            "-G.A.M.M.A. End of List_separator",
            "+ModA",
            "-Foo_separator",
        ]
        with self.assertRaises(ValueError):
            move_category(lines, "Foo", "G.A.M.M.A. End of List", before=True)
        # Moving it to AFTER the pinned category is fine - it's only
        # "before" that's refused.
        moved = move_category(lines, "Foo", "G.A.M.M.A. End of List", before=False)
        from commander_gui.modlist import grouped

        names = [name for name, _mods in grouped(moved)]
        self.assertEqual(names, ["G.A.M.M.A. End of List", "Foo"])

    def test_move_category_rejects_unknown_names(self):
        from commander_gui.modlist import move_category

        lines = ["+ModA", "-Foo_separator"]
        with self.assertRaises(ValueError):
            move_category(lines, "Nope", "Foo", before=True)
        with self.assertRaises(ValueError):
            move_category(lines, "Foo", "Nope", before=True)

    def test_delete_category_relocates_members_to_uncategorized_by_default(self):
        from commander_gui.modlist import delete_category, grouped

        lines = ["+ModA", "-Foo_separator", "+ModB", "-Bar_separator"]
        deleted = delete_category(lines, "Foo")
        names = [name for name, _mods in grouped(deleted)]
        self.assertEqual(names, ["Bar", "Uncategorized"])
        after = dict(grouped(deleted))
        self.assertEqual([n for _s, n, _i in after["Uncategorized"]], ["ModA"])

    def test_delete_category_with_delete_members_removes_them_outright(self):
        from commander_gui.modlist import delete_category, entries

        lines = ["+ModA", "-Foo_separator", "+ModB", "-Bar_separator"]
        deleted = delete_category(lines, "Foo", delete_members=True)
        names = [name for _status, name in entries(deleted)]
        self.assertNotIn("ModA", names)
        self.assertIn("ModB", names)

    def test_delete_category_refuses_the_pinned_category(self):
        from commander_gui.modlist import delete_category

        lines = ["-G.A.M.M.A. End of List_separator", "+ModA", "-Foo_separator"]
        with self.assertRaises(ValueError):
            delete_category(lines, "G.A.M.M.A. End of List")

    def test_delete_category_rejects_unknown_category(self):
        from commander_gui.modlist import delete_category

        with self.assertRaises(ValueError):
            delete_category(["+ModA", "-Foo_separator"], "Nope")

    def test_add_custom_mod_creates_the_category_directly_before_end_of_list(self):
        """Regression test for a real reported crash: mods installed via

        the "Install Mod" button must always keep the highest priority
        relative to the rest of GAMMA (rendered at the on-screen bottom,
        directly under "G.A.M.M.A. End of List"), or Anomaly can crash on
        load. add_mod()'s plain "Uncategorized" landing only stays safe
        until the mod is filed into a real category by hand - this instead
        creates/uses a "Custom Mods" category that flip_priority() pins
        the same way it pins "G.A.M.M.A. End of List" itself.
        """
        from commander_gui.modlist import add_custom_mod, grouped

        lines = [
            "+AnomalyTogether",
            "-G.A.M.M.A. End of List_separator",
            "+ModA",
            "-Foo_separator",
        ]
        out = add_custom_mod(lines, "NewMod")
        names = [name for name, _mods in grouped(out)]
        self.assertEqual(names, ["Custom Mods", "G.A.M.M.A. End of List", "Foo"])
        after = dict(grouped(out))
        self.assertEqual(
            [n for _s, n, _i in after["Custom Mods"]], ["NewMod"]
        )
        self.assertEqual(after["Custom Mods"][0][0], "Disabled")

    def test_add_custom_mod_appends_to_an_existing_custom_mods_category(self):
        from commander_gui.modlist import add_custom_mod, grouped

        lines = [
            "+FirstCustom",
            "-Custom Mods_separator",
            "+ModA",
            "-Foo_separator",
        ]
        out = add_custom_mod(lines, "SecondCustom", enabled=True)
        after = dict(grouped(out))
        self.assertEqual(
            [n for _s, n, _i in after["Custom Mods"]],
            ["FirstCustom", "SecondCustom"],
        )

    def test_add_custom_mod_creates_at_file_start_without_an_end_of_list(self):
        from commander_gui.modlist import add_custom_mod, grouped

        out = add_custom_mod(["+ModA", "-Foo_separator"], "NewMod")
        names = [name for name, _mods in grouped(out)]
        self.assertEqual(names, ["Custom Mods", "Foo"])

    def test_add_custom_mod_rejects_a_duplicate_name(self):
        from commander_gui.modlist import add_custom_mod

        with self.assertRaises(ValueError):
            add_custom_mod(["+ModA", "-Foo_separator"], "ModA")

    def test_flip_priority_keeps_custom_mods_pinned_directly_under_end_of_list(self):
        from commander_gui.modlist import add_custom_mod, flip_priority, grouped

        lines = [
            "-G.A.M.M.A. End of List_separator",
            "+ModA",
            "-Foo_separator",
            "+ModB",
            "-Bar_separator",
        ]
        with_custom = add_custom_mod(lines, "NewMod")
        flipped = flip_priority(with_custom)
        category_order = [category for category, _mods in grouped(flipped)]
        self.assertEqual(
            category_order,
            ["Custom Mods", "G.A.M.M.A. End of List", "Bar", "Foo"],
        )
        # Round-trips back to the original exactly, same as the single-pin case.
        self.assertEqual(flip_priority(flipped), with_custom)

    def test_move_category_refuses_to_move_custom_mods(self):
        from commander_gui.modlist import add_custom_mod, move_category

        lines = add_custom_mod(
            ["-G.A.M.M.A. End of List_separator", "+ModA", "-Foo_separator"], "NewMod"
        )
        with self.assertRaises(ValueError):
            move_category(lines, "Custom Mods", "Foo", before=True)
        with self.assertRaises(ValueError):
            move_category(lines, "Foo", "Custom Mods", before=True)

    def test_delete_category_refuses_to_delete_custom_mods(self):
        from commander_gui.modlist import add_custom_mod, delete_category

        lines = add_custom_mod(["+ModA", "-Foo_separator"], "NewMod")
        with self.assertRaises(ValueError):
            delete_category(lines, "Custom Mods")

    def test_drag_tree_drop_on_header_reports_the_bare_category_name(self):
        """Regression test: dropping a mod directly on a category HEADER

        (not on another mod) must report the header's real category name,
        not its "(N)" mod-count-suffixed display text - the display text
        never matches a real separator name, which silently sent the mod
        to the very end of the file (Uncategorized) instead of into the
        intended category.
        """
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication, QTreeWidgetItem

        from commander_gui.ui.mod_manager_page import DragTree

        QApplication.instance() or QApplication([])
        tree = DragTree()
        header = QTreeWidgetItem(["Weapons (12)"])
        header.setData(0, Qt.ItemDataRole.UserRole, "Weapons")
        tree.addTopLevelItem(header)
        _mod_item = QTreeWidgetItem(header, ["SomeMod"])
        tree.resize(300, 200)

        tree._drag_source_name = "SomeMod"
        tree._drag_active = True
        header_rect = tree.visualItemRect(header)
        pos = QPointF(header_rect.center())

        received: list = []
        tree.mod_dropped.connect(lambda *args: received.append(args))
        event = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            pos,
            pos,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        tree.mouseReleaseEvent(event)

        self.assertEqual(len(received), 1)
        source, target_name, category, _before = received[0]
        self.assertEqual(source, "SomeMod")
        self.assertIsNone(target_name)
        self.assertEqual(category, "Weapons")

    def test_drag_tree_press_on_header_starts_a_category_drag(self):
        """Regression test: pressing on a category HEADER row (not one of

        its mods) must arm a whole-category drag, distinct from a
        per-mod drag - the two are mutually exclusive.
        """
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication, QTreeWidgetItem

        from commander_gui.ui.mod_manager_page import DragTree

        QApplication.instance() or QApplication([])
        tree = DragTree()
        header = QTreeWidgetItem(["Weapons (1)"])
        header.setData(0, Qt.ItemDataRole.UserRole, "Weapons")
        tree.addTopLevelItem(header)
        QTreeWidgetItem(header, ["SomeMod"])
        tree.resize(300, 200)

        pos = QPointF(tree.visualItemRect(header).center())
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            pos,
            pos,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        tree.mousePressEvent(event)

        self.assertEqual(tree._drag_source_category, "Weapons")
        self.assertIsNone(tree._drag_source_name)

    def test_drag_tree_press_on_uncategorized_header_does_not_start_a_drag(self):
        """"Uncategorized" has no separator line to move by name - it

        must never be draggable as a whole category.
        """
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication, QTreeWidgetItem

        from commander_gui.ui.mod_manager_page import DragTree

        QApplication.instance() or QApplication([])
        tree = DragTree()
        header = QTreeWidgetItem(["Uncategorized (1)"])
        header.setData(0, Qt.ItemDataRole.UserRole, "Uncategorized")
        tree.addTopLevelItem(header)
        QTreeWidgetItem(header, ["SomeMod"])
        tree.resize(300, 200)

        pos = QPointF(tree.visualItemRect(header).center())
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            pos,
            pos,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        tree.mousePressEvent(event)

        self.assertIsNone(tree._drag_source_category)
        self.assertIsNone(tree._drag_source_name)

    def test_drag_tree_category_drop_emits_category_dropped(self):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication, QTreeWidgetItem

        from commander_gui.ui.mod_manager_page import DragTree

        QApplication.instance() or QApplication([])
        tree = DragTree()
        header_a = QTreeWidgetItem(["Foo (1)"])
        header_a.setData(0, Qt.ItemDataRole.UserRole, "Foo")
        tree.addTopLevelItem(header_a)
        QTreeWidgetItem(header_a, ["ModA"])
        header_b = QTreeWidgetItem(["Bar (1)"])
        header_b.setData(0, Qt.ItemDataRole.UserRole, "Bar")
        tree.addTopLevelItem(header_b)
        QTreeWidgetItem(header_b, ["ModB"])
        tree.resize(300, 200)

        tree._drag_source_category = "Foo"
        tree._drag_active = True
        pos = QPointF(tree.visualItemRect(header_b).center())
        received: list = []
        tree.category_dropped.connect(lambda *args: received.append(args))
        event = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            pos,
            pos,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        tree.mouseReleaseEvent(event)

        self.assertEqual(len(received), 1)
        source, target, _before = received[0]
        self.assertEqual(source, "Foo")
        self.assertEqual(target, "Bar")
        self.assertIsNone(tree._drag_source_category)

    def test_drag_tree_category_dropped_on_itself_is_cancelled(self):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication, QTreeWidgetItem

        from commander_gui.ui.mod_manager_page import DragTree

        QApplication.instance() or QApplication([])
        tree = DragTree()
        header = QTreeWidgetItem(["Foo (1)"])
        header.setData(0, Qt.ItemDataRole.UserRole, "Foo")
        tree.addTopLevelItem(header)
        QTreeWidgetItem(header, ["ModA"])
        tree.resize(300, 200)

        tree._drag_source_category = "Foo"
        tree._drag_active = True
        pos = QPointF(tree.visualItemRect(header).center())
        category_received: list = []
        cancelled_received: list = []
        tree.category_dropped.connect(lambda *args: category_received.append(args))
        tree.drop_cancelled.connect(lambda: cancelled_received.append(True))
        event = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            pos,
            pos,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        tree.mouseReleaseEvent(event)

        self.assertEqual(category_received, [])
        self.assertEqual(cancelled_received, [True])

    def test_drag_tree_drop_on_empty_space_emits_drop_cancelled(self):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication, QTreeWidgetItem

        from commander_gui.ui.mod_manager_page import DragTree

        QApplication.instance() or QApplication([])
        tree = DragTree()
        header = QTreeWidgetItem(["Foo (1)"])
        header.setData(0, Qt.ItemDataRole.UserRole, "Foo")
        tree.addTopLevelItem(header)
        QTreeWidgetItem(header, ["ModA"])
        tree.resize(300, 200)

        tree._drag_source_name = "ModA"
        tree._drag_active = True
        cancelled_received: list = []
        tree.drop_cancelled.connect(lambda: cancelled_received.append(True))
        pos = QPointF(10, 190)  # below the last row - empty space
        event = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            pos,
            pos,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        tree.mouseReleaseEvent(event)

        self.assertEqual(cancelled_received, [True])

    def test_drag_tree_wheel_scroll_moves_a_normal_step_not_a_huge_jump(self):
        """Regression test: the tree never set a scroll mode, so it stayed

        on Qt's default ScrollPerItem, where the scrollbar's `value()` is a
        ROW INDEX, not a pixel offset. The old wheelEvent subtracted the
        wheel's raw angleDelta() (~120 per notch) straight from that row
        index, jumping the list by ~120 rows per notch instead of a normal
        few-line step. Switching to ScrollPerPixel and animating toward a
        properly-scaled per-notch pixel target fixes both the jump and the
        lack of any animation.
        """
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QAbstractItemView, QApplication

        from commander_gui.ui.mod_manager_page import DragTree

        QApplication.instance() or QApplication([])
        tree = DragTree()

        self.assertEqual(
            tree.verticalScrollMode(), QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        # Simulate a scrollable list without depending on real layout/show()
        # (which would pump the shared event loop and risk waking up
        # unrelated leftover widgets from other tests in the same process).
        sb = tree.verticalScrollBar()
        sb.setRange(0, 1000)
        sb.setValue(500)
        start = sb.value()

        def scroll_down_notch() -> None:
            event = QWheelEvent(
                QPointF(10, 10),
                QPointF(10, 10),
                QPoint(0, 0),
                QPoint(0, -120),  # scrolling down
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            tree.wheelEvent(event)

        scroll_down_notch()
        end = tree._scroll_anim.endValue()
        # Moved down by a normal, bounded per-notch step - not a ~120-row
        # (effectively near-max) jump.
        self.assertGreater(end, start)
        self.assertLessEqual(end - start, 500)
        self.assertLessEqual(end, sb.maximum())

        # A second notch in the same direction accumulates further rather
        # than resetting/restarting from the live (not-yet-reached) value.
        scroll_down_notch()
        end2 = tree._scroll_anim.endValue()
        self.assertGreater(end2, end)


class ModCounterTests(unittest.TestCase):
    def _make_gamma(
        self,
        tmpdir,
        profile_folder: str = "G.A.M.M.A",
        modlist_text: str = "+ModA\n-ModB\n",
        create_mod_folders: bool = True,
    ) -> Path:
        gamma_dir = Path(tmpdir) / "gamma"
        gamma_dir.mkdir(parents=True, exist_ok=True)
        for marker in ("ModOrganizer.exe", "ModOrganizer.ini"):
            (gamma_dir / marker).touch()
        profile_dir = gamma_dir / "profiles" / profile_folder
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_text(modlist_text, encoding="utf-8")
        if create_mod_folders:
            for line in modlist_text.splitlines():
                stripped = line.strip()
                if stripped[:1] in "+-":
                    (gamma_dir / "mods" / stripped[1:]).mkdir(parents=True, exist_ok=True)
        return gamma_dir

    def test_counts_enabled_and_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma_dir = self._make_gamma(tmp)
            self.assertEqual(count_active_mods(str(gamma_dir), "G.A.M.M.A"), (1, 2))

    def test_case_insensitive_profile_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma_dir = self._make_gamma(tmp, profile_folder="G.A.M.M.A")
            self.assertEqual(count_active_mods(str(gamma_dir), "g.a.m.m.a"), (1, 2))

    def test_count_mods_dedups_a_duplicate_name(self):
        """Regression test: GAMMA's own official modlist.txt has been

        confirmed to list at least one mod twice (e.g. "G.A.M.M.A.
        Vehicles in Darkscape") - there's only one real mod/folder for
        it, so counting it twice overcounts relative to MO2's own
        (name-keyed) count and the community's known total.
        """
        from commander_gui.modlist import count_mods

        lines = ["+DupMod", "+DupMod", "+OtherMod", "-DupMod"]
        enabled, total = count_mods(lines)
        # First occurrence (+DupMod, enabled) wins; the later "+DupMod"
        # and "-DupMod" duplicates are ignored entirely.
        self.assertEqual((enabled, total), (2, 2))

    def test_count_active_mods_dedups_a_duplicate_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma_dir = self._make_gamma(
                tmp, modlist_text="+DupMod\n+DupMod\n+OtherMod\n"
            )
            self.assertEqual(count_active_mods(str(gamma_dir), "G.A.M.M.A"), (2, 2))

    def test_missing_gamma_install_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                count_active_mods(str(Path(tmp) / "nope"), "G.A.M.M.A")
            )

    def test_missing_profile_folder_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma_dir = self._make_gamma(tmp, profile_folder="OtherProfile")
            self.assertIsNone(count_active_mods(str(gamma_dir), "G.A.M.M.A"))

    def test_missing_modlist_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma_dir = self._make_gamma(tmp)
            (gamma_dir / "profiles" / "G.A.M.M.A" / "modlist.txt").unlink()
            self.assertIsNone(count_active_mods(str(gamma_dir), "G.A.M.M.A"))

    def test_excludes_mods_listed_but_never_extracted(self):
        """Regression test: a mod listed in modlist.txt whose folder was

        never actually created (e.g. one archive failed mid-install while
        the rest succeeded) must not be counted - it isn't really
        installed, and counting it overstates the real total by however
        many silently failed (reported as "578 instead of 577").
        """
        with tempfile.TemporaryDirectory() as tmp:
            gamma_dir = self._make_gamma(
                tmp,
                modlist_text="+ModA\n-ModB\n+ModC\n",
                create_mod_folders=False,
            )
            (gamma_dir / "mods" / "ModA").mkdir(parents=True)
            (gamma_dir / "mods" / "ModB").mkdir(parents=True)
            # ModC is listed but its folder never got extracted.
            self.assertEqual(count_active_mods(str(gamma_dir), "G.A.M.M.A"), (1, 2))

    def test_main_window_mod_counter_timer_keeps_the_count_live(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            try:
                self.assertTrue(window._mod_counter_timer.isActive())
                self.assertLessEqual(window._mod_counter_timer.interval(), 10000)
                with patch.object(window, "update_mod_counter") as mock_update:
                    window._mod_counter_timer.timeout.emit()
                mock_update.assert_called_once()
            finally:
                window._mod_counter_timer.stop()
                window.close()

    def test_switching_pages_refreshes_the_mod_counter_immediately(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            try:
                with patch.object(window, "update_mod_counter") as mock_update:
                    window._on_nav(1)
                mock_update.assert_called_once()
            finally:
                window._mod_counter_timer.stop()
                window.close()

    def test_main_window_update_mod_counter_always_shows_a_count(self):
        from PySide6.QtWidgets import QApplication, QLabel

        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            gamma_dir = self._make_gamma(tmp)
            profile = CliProfile(
                active=True,
                profile_name="Test",
                gamma=str(gamma_dir),
                mo2_profile="G.A.M.M.A",
            )
            window = MainWindow.__new__(MainWindow)
            window.settings = CliSettings(profiles=[profile])
            window.mod_counter_label = QLabel()

            window.update_mod_counter()
            self.assertTrue(window.mod_counter_label.isVisible())
            self.assertEqual(window.mod_counter_label.text(), "1 Mods")

            # Simulate an in-page edit rewriting modlist.txt, then re-check.
            (gamma_dir / "profiles" / "G.A.M.M.A" / "modlist.txt").write_text(
                "+ModA\n+ModB\n", encoding="utf-8"
            )
            window.update_mod_counter()
            self.assertEqual(window.mod_counter_label.text(), "2 Mods")

            # No active profile -> still shown, reading "0 Mods" instead
            # of disappearing (a permanent topbar fixture, not crashed).
            window.settings = CliSettings(profiles=[])
            window.update_mod_counter()
            self.assertTrue(window.mod_counter_label.isVisible())
            self.assertEqual(window.mod_counter_label.text(), "0 Mods")

    def test_mod_counter_flags_an_incomplete_install(self):
        """Regression test: a failed install must not silently read as done.

        If the active profile's last GAMMA install attempt ended in
        failure (tracked the same way the Install page's own Resume
        button decides whether to offer resuming), the topbar counter
        must say so instead of just showing a plain, seemingly-final
        mod count.
        """
        from PySide6.QtWidgets import QApplication, QLabel

        from commander_gui import gui_settings
        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            gamma_dir = self._make_gamma(tmp)
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(gamma_dir),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
            )
            window = MainWindow.__new__(MainWindow)
            window.settings = CliSettings(profiles=[profile])
            window.mod_counter_label = QLabel()

            window.update_mod_counter()
            self.assertEqual(window.mod_counter_label.text(), "1 Mods")

            gui_settings.save_gui_settings(
                gamma_install_resume={
                    "profile": profile.profile_name,
                    "anomaly": profile.anomaly,
                    "gamma": profile.gamma,
                    "cache": profile.cache,
                }
            )
            window.update_mod_counter()
            self.assertEqual(window.mod_counter_label.text(), "1 Mods (incomplete)")
            self.assertIn("last install attempt failed", window.mod_counter_label.toolTip())

    def test_install_status_row_set_incomplete(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.common import WARN, InstallStatusRow

        QApplication.instance() or QApplication([])
        row = InstallStatusRow("GAMMA")
        row.set_incomplete("Last install attempt failed - resume it to finish.")
        self.assertEqual(row._status.text(), "Incomplete")
        self.assertIn(WARN.name(), row._status.styleSheet())

    def test_play_click_sound_is_a_noop_when_the_asset_is_missing(self):
        """A missing sound asset must never raise - it must never block a launch."""
        import commander_gui.ui.common as common_mod

        with tempfile.TemporaryDirectory() as tmp:
            common_mod._click_sound_player = None
            common_mod._click_sound_output = None
            with patch("commander_gui.config.project_root", return_value=Path(tmp)):
                common_mod.play_click_sound()  # must not raise
            self.assertIsNone(common_mod._click_sound_player)

    def test_play_click_sound_plays_the_bundled_asset_without_raising(self):
        from PySide6.QtWidgets import QApplication

        import commander_gui.ui.common as common_mod

        QApplication.instance() or QApplication([])
        common_mod._click_sound_player = None
        common_mod._click_sound_output = None
        common_mod.play_click_sound()  # must not raise, uses the real bundled asset
        common_mod.play_click_sound()  # a second call must reuse the cached player

    def test_launch_buttons_play_the_click_sound(self):
        """Launch Game and Launch Anomaly both trigger the click sound."""
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.play_page import PlayPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            gamma_dir = self._make_gamma(tmp)
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(gamma_dir),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

                def refresh_settings(self):
                    pass

            # .connect() captures the bound method/function object at
            # connect time - patching (or reassigning an instance
            # attribute) after PlayPage.__init__ has already run would not
            # affect what the signal is wired to. Everything that must not
            # fire for real - the launch handlers and the click sound -
            # has to be patched at the class level before construction.
            with (
                patch.object(PlayPage, "launch_game", lambda self: None),
                patch.object(PlayPage, "_launch_direct", lambda self: None),
                patch("commander_gui.ui.play_page.play_click_sound") as mock_sound,
            ):
                page = PlayPage(FakeWindow())
                # emit() rather than click(): these buttons start disabled
                # without a real MO2/runner setup on disk, and a disabled
                # button's click() is a no-op - the signal wiring itself,
                # not the (separately tested) enabled-state logic, is what
                # this test covers.
                page.launch_button.clicked.emit()
                page.direct_button.clicked.emit()
            self.assertEqual(mock_sound.call_count, 2)

    def test_dashboard_play_gamma_plays_the_click_sound(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            gamma_dir = self._make_gamma(tmp)
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(gamma_dir),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False

                def refresh_settings(self):
                    pass

            with (
                patch.object(DashboardPage, "_play_gamma", lambda self: None),
                patch("commander_gui.ui.dashboard.play_click_sound") as mock_sound,
            ):
                page = DashboardPage(FakeWindow())
                page._play_button.clicked.emit()
            mock_sound.assert_called_once()

    def test_dashboard_profile_card_drops_folder_paths_and_adds_new_rows(self):
        """Regression test: the Dashboard's "Active COMMANDER profile" card

        used to repeat the Anomaly/GAMMA/Cache folder paths already shown
        in the Installation status card above it - those rows are gone,
        Total playtime moved up under Profile, and Last played/Current
        runner are new. The Profile row is now a live switcher, not a
        static label.
        """
        from PySide6.QtWidgets import QApplication, QLabel

        from commander_gui.ui.common import NoWheelComboBox
        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            p1 = CliProfile(active=True, profile_name="Mine")
            p2 = CliProfile(active=False, profile_name="Other")

            class FakeWindow:
                settings = CliSettings(profiles=[p1, p2])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            page = DashboardPage(FakeWindow())
            combined = " | ".join(
                w.text() for w in page.profile_card.findChildren(QLabel)
            )
            self.assertNotIn("Anomaly folder", combined)
            self.assertNotIn("Cache folder", combined)
            self.assertIn("Total playtime", combined)
            self.assertIn("Last played", combined)
            self.assertIn("Current runner", combined)
            self.assertIn("Download threads", combined)

            # Four live switchers now, in the order they were added to the
            # layout: Profile, MO2 profile, Current runner, Download
            # threads.
            combos = page.profile_card.findChildren(NoWheelComboBox)
            self.assertEqual(len(combos), 4)
            self.assertEqual(
                [combos[0].itemText(i) for i in range(combos[0].count())],
                ["Mine", "Other"],
            )
            self.assertEqual(combos[0].currentText(), "Mine")
            # The MO2 profile combo's real list is filled in async by
            # _start_mo2_profiles_task() - synchronously it only has the
            # one placeholder item, the configured profile.
            self.assertIs(combos[1], page.mo2_profile_combo)
            self.assertEqual(combos[1].currentText(), p1.mo2_profile)
            self.assertIs(combos[2], page.runner_combo)
            self.assertIs(combos[3], page.download_threads_combo)
            self.assertEqual(combos[3].currentData(), p1.download_threads)

    def test_dashboard_profile_switch_declines_when_game_running(self):
        """Declining the "game running" confirmation must leave the active

        profile unchanged and revert the combo back to it.
        """
        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.ui.common import NoWheelComboBox
        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            p1 = CliProfile(active=True, profile_name="Mine")
            p2 = CliProfile(active=False, profile_name="Other")

            class FakeWindow:
                settings = CliSettings(profiles=[p1, p2])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            page = DashboardPage(FakeWindow())
            combo = page.profile_card.findChildren(NoWheelComboBox)[0]

            with (
                patch("commander_gui.ui.dashboard.mo2_running", return_value=True),
                patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.No,
                ),
                patch("commander_gui.ui.dashboard.activate_profile") as mock_activate,
            ):
                combo.setCurrentIndex(combo.findData("Other"))

            mock_activate.assert_not_called()
            self.assertEqual(combo.currentText(), "Mine")

    def test_dashboard_profile_switch_callback_survives_the_card_being_rebuilt(self):
        """Regression test: the Dashboard's profile switcher kept the combo

        alive in activate_profile()'s on_done callback, but _render_profile()
        rebuilds the whole card (clear_layout() deletes that combo) on every
        refresh - and a refresh easily lands while the CLI "config use" call
        is still running (navigating away and back re-refreshes the page).
        The failure path then raised "Internal C++ object already deleted"
        out of a slot.
        """
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.common import NoWheelComboBox
        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            p1 = CliProfile(active=True, profile_name="Mine")
            p2 = CliProfile(active=False, profile_name="Other")

            class FakeWindow:
                settings = CliSettings(profiles=[p1, p2])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            captured = {}
            with patch("commander_gui.ui.dashboard.BackgroundTask"):
                page = DashboardPage(FakeWindow())
                combo = page.profile_card.findChildren(NoWheelComboBox)[0]
                with (
                    patch(
                        "commander_gui.ui.dashboard.mo2_running", return_value=False
                    ),
                    patch(
                        "commander_gui.ui.dashboard.activate_profile",
                        side_effect=lambda window, parent, name, on_done=None: (
                            captured.setdefault("done", on_done)
                        ),
                    ),
                ):
                    combo.setCurrentIndex(combo.findData("Other"))
                # The user navigates away and back: the card - and with it
                # the combo the callback is holding - is rebuilt.
                page.refresh()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                # The CLI call only now comes back, reporting failure.
                captured["done"](False)

    def test_dashboard_profile_switch_is_refused_while_an_install_is_running(self):
        """Regression test: the Profiles page's "Set active" is guarded by

        _busy_guard(), but the Dashboard's inline switcher had no
        install_busy guard at all - so the active profile could be
        repointed mid-install, while the installer was still writing into
        the old profile's folders.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.common import NoWheelComboBox
        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            p1 = CliProfile(active=True, profile_name="Mine")
            p2 = CliProfile(active=False, profile_name="Other")

            class FakeWindow:
                settings = CliSettings(profiles=[p1, p2])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            window = FakeWindow()
            with patch("commander_gui.ui.dashboard.BackgroundTask"):
                page = DashboardPage(window)
                combo = page.profile_card.findChildren(NoWheelComboBox)[0]
                window.install_busy = True
                with (
                    patch(
                        "commander_gui.ui.dashboard.mo2_running", return_value=False
                    ),
                    patch(
                        "commander_gui.ui.dashboard.activate_profile"
                    ) as mock_activate,
                ):
                    combo.setCurrentIndex(combo.findData("Other"))

            mock_activate.assert_not_called()
            self.assertEqual(combo.currentText(), "Mine")

    def test_dashboard_mo2_profiles_loaded_prefers_actual_selected_over_stale_config(
        self,
    ):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            p1 = CliProfile(active=True, profile_name="Mine", mo2_profile="G.A.M.M.A")

            class FakeWindow:
                settings = CliSettings(profiles=[p1])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            with patch("commander_gui.ui.dashboard.BackgroundTask"):
                page = DashboardPage(FakeWindow())
            page._on_mo2_profiles_loaded(
                (["G.A.M.M.A", "Solo Profile"], "Solo Profile"),
                page._refresh_generation,
            )
            self.assertEqual(page.mo2_profile_combo.currentText(), "Solo Profile")

    def test_mo2_profile_switch_updates_the_cli_profile_and_refreshes(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            p1 = CliProfile(active=True, profile_name="Mine", mo2_profile="G.A.M.M.A")

            class FakeWindow:
                settings = CliSettings(profiles=[p1])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            with patch("commander_gui.ui.dashboard.BackgroundTask"):
                page = DashboardPage(FakeWindow())
                page.mo2_profile_combo.addItem("Solo Profile")
                with patch(
                    "commander_gui.ui.dashboard.mo2_running", return_value=False
                ):
                    # setCurrentIndex, not setCurrentText: real popup-driven
                    # selection goes through the index (this combo is
                    # editable now, purely for right-aligned display text -
                    # setCurrentText on an editable combo only sets the
                    # line edit's text and does not fire
                    # currentIndexChanged).
                    page.mo2_profile_combo.setCurrentIndex(
                        page.mo2_profile_combo.findText("Solo Profile")
                    )
                    set_task = page._set_mo2_selected_task
                    done_handler = set_task.result.connect.call_args[0][0]
            with patch.object(page, "refresh") as mock_refresh:
                done_handler((0, ""))
            self.assertEqual(p1.mo2_profile, "Solo Profile")
            mock_refresh.assert_called_once()

    def test_mo2_profile_switch_blocked_while_mo2_is_running(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            p1 = CliProfile(active=True, profile_name="Mine", mo2_profile="G.A.M.M.A")

            class FakeWindow:
                settings = CliSettings(profiles=[p1])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            with patch("commander_gui.ui.dashboard.BackgroundTask"):
                page = DashboardPage(FakeWindow())
                page.mo2_profile_combo.addItem("Solo Profile")
                with (
                    patch(
                        "commander_gui.ui.dashboard.mo2_running", return_value=True
                    ),
                    patch(
                        "commander_gui.ui.dashboard.QMessageBox.warning"
                    ) as mock_warn,
                    patch("commander_gui.ui.dashboard.BackgroundTask") as mock_task,
                ):
                    page.mo2_profile_combo.setCurrentIndex(
                        page.mo2_profile_combo.findText("Solo Profile")
                    )
                mock_warn.assert_called_once()
                mock_task.assert_not_called()
            self.assertEqual(p1.mo2_profile, "G.A.M.M.A")

    def test_dashboard_cards_stay_pinned_to_their_natural_size_on_resize(self):
        """Regression test: every make_card() on the Dashboard uses

        expand=True (Expanding size policy), and root (the page's outer
        QVBoxLayout) had no trailing stretch - so a window taller than the
        page's natural content stretched the cards themselves to fill the
        extra height instead of leaving it as blank page space, shifting
        each card's (and so each title's) position as the window was
        resized rather than keeping every card pinned in place.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        p1 = CliProfile(active=True, profile_name="Mine", mo2_profile="G.A.M.M.A")

        class FakeWindow:
            settings = CliSettings(profiles=[p1])
            install_busy = False
            install_operation = None

            def refresh_settings(self):
                pass

        with patch("commander_gui.ui.dashboard.BackgroundTask"):
            page = DashboardPage(FakeWindow())

        geometries = []
        for height in (500, 900, 1400):
            page.resize(700, height)
            page.show()
            QApplication.processEvents()
            geometries.append(
                (page.actions_card.y(), page.actions_card.height())
            )
        self.assertEqual(len(set(geometries)), 1)

    def test_flat_value_combo_click_opens_the_popup_without_immediately_closing_it(
        self,
    ):
        """Regression test: clicking the (right-aligned) text of a

        _FlatValueCombo opens the popup via an event filter on its
        internal line edit. Calling showPopup() from the *press* handler
        (the first attempt at this fix) started the popup's own mouse
        grab while the same click was still in progress, so Qt read the
        click's own release - landing back on the line edit, outside the
        popup's list - as the native combo box's press-drag-release
        gesture cancelling with nothing picked, closing the popup the
        instant it opened. It must open on *release* instead, once the
        click that triggered it has fully finished - press is still
        swallowed (so the line edit itself never reacts to it), but must
        not itself call showPopup().
        """
        from PySide6.QtCore import QEvent, QPoint, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.dashboard import _FlatValueCombo

        QApplication.instance() or QApplication([])
        combo = _FlatValueCombo()
        combo.addItem("A")
        combo.addItem("B")

        with (
            patch.object(combo, "showPopup") as mock_show,
            patch.object(combo, "hidePopup") as mock_hide,
        ):
            pos = QPoint(5, 5)
            press = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                pos,
                pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            release = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                pos,
                pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            self.assertTrue(combo.eventFilter(combo.lineEdit(), press))
            mock_show.assert_not_called()
            self.assertTrue(combo.eventFilter(combo.lineEdit(), release))
            mock_show.assert_called_once()
            mock_hide.assert_not_called()

    def test_dashboard_runner_combo_lists_auto_and_installed_protons(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            p1 = CliProfile(active=True, profile_name="Mine")

            class FakeWindow:
                settings = CliSettings(profiles=[p1])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            with (
                patch("commander_gui.ui.dashboard.BackgroundTask"),
                patch(
                    "commander_gui.ui.dashboard.find_extra_protons",
                    return_value=[("GE-Proton9-20", "/path/to/proton")],
                ),
            ):
                page = DashboardPage(FakeWindow())
            combo = page.runner_combo
            self.assertEqual(
                [combo.itemData(i) for i in range(combo.count()) if combo.itemData(i)],
                ["auto", "umup:/path/to/proton"],
            )
            self.assertEqual(combo.currentData(), "auto")

    def test_dashboard_runner_switch_saves_the_gui_setting(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            p1 = CliProfile(active=True, profile_name="Mine")

            class FakeWindow:
                settings = CliSettings(profiles=[p1])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            with (
                patch("commander_gui.ui.dashboard.BackgroundTask"),
                patch(
                    "commander_gui.ui.dashboard.find_extra_protons",
                    return_value=[("GE-Proton9-20", "/path/to/proton")],
                ),
            ):
                page = DashboardPage(FakeWindow())
                page.runner_combo.setCurrentIndex(
                    page.runner_combo.findData("umup:/path/to/proton")
                )
            self.assertEqual(
                gui_settings.load_gui_settings().get("runner"),
                "umup:/path/to/proton",
            )

    def test_dashboard_download_threads_combo_has_exactly_three_options(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            p1 = CliProfile(active=True, profile_name="Mine", download_threads=6)

            class FakeWindow:
                settings = CliSettings(profiles=[p1])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            with patch("commander_gui.ui.dashboard.BackgroundTask"):
                page = DashboardPage(FakeWindow())
            combo = page.download_threads_combo
            self.assertEqual(
                [
                    (combo.itemText(i), combo.itemData(i))
                    for i in range(combo.count())
                ],
                [("4 (Safe)", 4), ("6 (Balanced)", 6), ("8 (Fast)", 8)],
            )
            self.assertEqual(combo.currentData(), 6)

    def test_dashboard_download_threads_switch_updates_the_active_profile(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.settings import load_settings
        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            p1 = CliProfile(active=True, profile_name="Mine", download_threads=6)

            class FakeWindow:
                settings = CliSettings(profiles=[p1])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            with patch("commander_gui.ui.dashboard.BackgroundTask"):
                page = DashboardPage(FakeWindow())
                page.download_threads_combo.setCurrentIndex(
                    page.download_threads_combo.findData(8)
                )
            self.assertEqual(p1.download_threads, 8)
            # Persisted to disk, not just mutated in memory.
            reloaded = load_settings()
            self.assertEqual(reloaded.active_profile.download_threads, 8)

    def test_dashboard_forgets_the_play_button_when_the_profile_disappears(self):
        """Regression test: _build_actions() only builds a Play button when

        a profile is active, but clear_layout() deletes the previous one
        either way - so after the active profile was deleted, self.
        _play_button still pointed at a destroyed widget and the next
        on_busy_changed()/launch_state_changed slot (e.g. installing
        GE-Proton, which needs no profile) raised "Internal C++ object
        already deleted".
        """
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            profile = CliProfile(active=True, profile_name="Mine")

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            window = FakeWindow()
            with patch("commander_gui.ui.dashboard.BackgroundTask"):
                page = DashboardPage(window)
                self.assertIsNotNone(page._play_button)
                # The only profile is deleted on the Profiles page.
                window.settings = CliSettings(profiles=[])
                page.refresh()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

            self.assertIsNone(page._play_button)
            page._set_play_button_disabled(True)  # must not raise

    def test_dashboard_play_state_binding_tolerates_a_window_without_pages(self):
        """Regression test: _bind_play_state()/on_busy_changed() assumed

        self.window._pages always exists and has a "play" entry - any
        test (or future caller) using a minimal window stub without
        `_pages` hit an unhandled AttributeError/KeyError from the
        QTimer.singleShot(0, ...) callback _build_actions() schedules,
        printed as noise to the terminal on every such Dashboard
        construction instead of failing loudly or degrading gracefully.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.dashboard import DashboardPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            profile = CliProfile(active=True, profile_name="Mine")

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False
                install_operation = None
                # Deliberately no `_pages` attribute at all.

                def refresh_settings(self):
                    pass

            with patch("commander_gui.ui.dashboard.BackgroundTask"):
                page = DashboardPage(FakeWindow())
                page._bind_play_state()  # must not raise
                page.on_busy_changed(True)  # must not raise
            self.assertFalse(page._play_button.isEnabled())

    def test_install_page_shows_incomplete_not_installed_for_a_failed_install(self):
        """Regression test: the Install page's GAMMA status dot must not

        read plain "Installed" (green) when the last attempt for this
        profile actually failed and never finished - it must read
        "Incomplete" (amber) instead, using the same gamma_install_resume
        signal the Resume button and the topbar counter already rely on.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            gamma_dir = self._make_gamma(tmp)
            (gamma_dir / "Stalker Anomaly.exe").touch()
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(gamma_dir),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False
                install_operation = None

                def refresh_settings(self):
                    pass

            page = InstallPage(FakeWindow())
            page.refresh()
            self.assertEqual(page.gamma_status._status.text(), "Installed")

            gui_settings.save_gui_settings(
                gamma_install_resume={
                    "profile": profile.profile_name,
                    "anomaly": profile.anomaly,
                    "gamma": profile.gamma,
                    "cache": profile.cache,
                }
            )
            page.refresh()
            self.assertEqual(page.gamma_status._status.text(), "Incomplete")
            self.assertEqual(page.full_progress.bar.format(), "Incomplete")

    def test_play_page_blocks_launch_on_an_incomplete_install(self):
        """Regression test: Launch/Open MO2/Launch Anomaly must not be

        clickable when the active profile's GAMMA install failed partway -
        launching a half-installed modpack just crashes on startup.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.launcher import Runner
        from commander_gui.ui.play_page import PlayPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            gamma_dir = self._make_gamma(tmp)
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(gamma_dir),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

                def refresh_settings(self):
                    pass

            page = PlayPage(FakeWindow())
            # Stub out the parts that would otherwise need a real MO2/Proton
            # setup on disk - only the incomplete-install gating is under
            # test here.
            page._runner = lambda: Runner("umu", "Proton", [], {})
            page._resolve_command = lambda **_kwargs: (["fake", "launch", "cmd"], None, None)
            page.executables = []

            page._refresh_preview_inner()
            self.assertTrue(page.launch_button.isEnabled())

            gui_settings.save_gui_settings(
                gamma_install_resume={
                    "profile": profile.profile_name,
                    "anomaly": profile.anomaly,
                    "gamma": profile.gamma,
                    "cache": profile.cache,
                }
            )
            page._refresh_preview_inner()
            self.assertFalse(page.launch_button.isEnabled())
            self.assertFalse(page.open_mo2_button.isEnabled())
            self.assertFalse(page.direct_button.isEnabled())
            self.assertIn("resume it", page.launch_button.toolTip())

    def _make_stubbed_play_page(self, tmp):
        """A real PlayPage whose preview can be refreshed without a real

        MO2/Proton setup on disk - the same stubbing
        test_play_page_blocks_launch_on_an_incomplete_install uses.
        """
        from commander_gui.launcher import Runner
        from commander_gui.ui.play_page import PlayPage

        gamma_dir = self._make_gamma(tmp)
        profile = CliProfile(
            active=True,
            profile_name="Test",
            anomaly=str(Path(tmp) / "anomaly"),
            gamma=str(gamma_dir),
            cache=str(Path(tmp) / "cache"),
            mo2_profile="G.A.M.M.A",
        )

        class FakeWindow:
            settings = CliSettings(profiles=[profile])

            def refresh_settings(self):
                pass

        page = PlayPage(FakeWindow())
        page._runner = lambda: Runner("umu", "Proton", [], {})
        page._resolve_command = lambda **_kwargs: (["fake", "launch", "cmd"], None, None)
        page.executables = []
        return page

    def test_set_launch_button_state_shows_quit_game_while_launching(self):
        """Regression test: the hero button must relabel to "Quit Game"

        (and stay clickable) while a launch is active, and revert to
        "Launch Game" once it ends - see also
        test_launch_button_click_routes_to_quit_confirmation_while_launching.
        """
        from PySide6.QtWidgets import QApplication

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}
        ):
            QApplication.instance() or QApplication([])
            page = self._make_stubbed_play_page(tmp)

            # launch_button goes through install_hover_grow_text() (see
            # common.py) - its own .text() is permanently cleared and an
            # overlay QLabel is what's actually visible, so that's what
            # must be checked here, not .text() itself.
            overlay = page.launch_button._hover_grow_overlay

            page._set_launch_button_state(True)
            self.assertEqual(overlay.text(), "Quit Game")
            self.assertTrue(page.launch_button.isEnabled())

            page._set_launch_button_state(False)
            self.assertEqual(overlay.text(), "Launch Game")

    def test_set_launch_button_state_never_shows_overlapping_text(self):
        """Regression test for a real reported bug: switching between

        "Launch Game"/"Quit Game" left both texts visibly overlaid on
        top of each other, never actually switching. Root cause:
        install_hover_grow_text() (common.py) clears the button's own
        real .text() permanently and paints a separate overlay label
        instead - calling plain setText() on the button re-populated its
        real text *in addition to* the overlay's own (unchanged, stale)
        text, rendering both at once. The button's own .text() must stay
        empty always; only the overlay may ever show text.
        """
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}
        ):
            from PySide6.QtWidgets import QApplication

            QApplication.instance() or QApplication([])
            page = self._make_stubbed_play_page(tmp)

            page._set_launch_button_state(True)
            self.assertEqual(page.launch_button.text(), "")
            page._set_launch_button_state(False)
            self.assertEqual(page.launch_button.text(), "")

    def test_launch_button_click_routes_to_quit_confirmation_while_launching(self):
        from PySide6.QtWidgets import QApplication, QMessageBox

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}
        ):
            QApplication.instance() or QApplication([])
            page = self._make_stubbed_play_page(tmp)
            page._launching = True

            with (
                patch.object(page, "_abort_launch") as mock_abort,
                patch(
                    "commander_gui.ui.play_page.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.No,
                ),
            ):
                page._on_launch_button_clicked()
            mock_abort.assert_not_called()

            with (
                patch.object(page, "_abort_launch") as mock_abort,
                patch(
                    "commander_gui.ui.play_page.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
            ):
                page._on_launch_button_clicked()
            mock_abort.assert_called_once_with("Game closed by user.")

    def test_launch_button_click_while_idle_still_launches_normally(self):
        from PySide6.QtWidgets import QApplication

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}
        ):
            QApplication.instance() or QApplication([])
            page = self._make_stubbed_play_page(tmp)
            page._launching = False

            with (
                patch.object(page, "launch_game") as mock_launch,
                patch.object(page, "_confirm_quit_game") as mock_confirm,
            ):
                page._on_launch_button_clicked()
            mock_launch.assert_called_once()
            mock_confirm.assert_not_called()

    def test_refresh_preview_inner_leaves_the_quit_game_button_alone(self):
        """Regression test: a preview refresh mid-launch (e.g. from an

        unrelated profile/folder edit) must not silently disable or
        relabel the "Quit Game" button back to a "Launch Game" state -
        that state is exclusively owned by _set_launch_button_state()
        while self._launching is True.
        """
        from PySide6.QtWidgets import QApplication

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}
        ):
            QApplication.instance() or QApplication([])
            page = self._make_stubbed_play_page(tmp)
            page._set_launch_button_state(True)
            overlay = page.launch_button._hover_grow_overlay

            page._refresh_preview_inner()

            self.assertEqual(overlay.text(), "Quit Game")
            self.assertTrue(page.launch_button.isEnabled())

    def test_stall_warning_fires_once_after_the_threshold(self):
        """Regression test for a real incident: a hung wrapper (a Wine

        crash loop, in that case) ran indefinitely with no MO2/game
        window ever appearing, silently consuming system memory. After
        _STALL_WARNING_SECONDS with nothing detected, the app must warn
        and offer to quit - exactly once per launch attempt.
        """
        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.ui import play_page as play_page_module

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}
        ):
            QApplication.instance() or QApplication([])
            page = self._make_stubbed_play_page(tmp)
            page._stall_warned = False
            page._launch_started_at = (
                time.monotonic() - play_page_module._STALL_WARNING_SECONDS - 1
            )

            with (
                patch.object(page, "_abort_launch") as mock_abort,
                patch(
                    "commander_gui.ui.play_page.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.No,
                ) as mock_question,
            ):
                page._on_launch_check("GAMMA", ["umu-run"], Path("/tmp/launcher.log"))
            mock_question.assert_called_once()
            mock_abort.assert_not_called()
            self.assertTrue(page._stall_warned)

            # A second tick must not prompt again for this same launch.
            with patch(
                "commander_gui.ui.play_page.QMessageBox.question"
            ) as mock_question_again:
                page._on_launch_check("GAMMA", ["umu-run"], Path("/tmp/launcher.log"))
            mock_question_again.assert_not_called()

    def test_stall_warning_does_not_fire_before_the_threshold(self):
        from PySide6.QtWidgets import QApplication

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}
        ):
            QApplication.instance() or QApplication([])
            page = self._make_stubbed_play_page(tmp)
            page._stall_warned = False
            page._launch_started_at = time.monotonic()
            page._proc = None
            page._monitoring_mo2 = False

            with patch(
                "commander_gui.ui.play_page.QMessageBox.question"
            ) as mock_question:
                page._on_launch_check("GAMMA", ["umu-run"], Path("/tmp/launcher.log"))
            mock_question.assert_not_called()

    def _chip_texts(self, page) -> list[str]:
        texts = []
        for i in range(page.chips_row.count()):
            widget = page.chips_row.itemAt(i).widget()
            if widget is not None:
                texts.append(widget.text())
        return texts

    def test_proton_chip_shows_only_the_selected_version(self):
        """Regression test: the Proton-GE chip used to list every

        installed GE-Proton build comma-separated (very wide with several
        installed) - it must show only the build that will actually be
        used for the current runner selection instead.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.launcher import Runner
        from commander_gui.ui.play_page import PlayPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            profile = CliProfile(active=True, profile_name="Test")

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

                def refresh_settings(self):
                    pass

            page = PlayPage(FakeWindow())
            page._proton_labels = ["GE-Proton9-20", "GE-Proton9-15"]

            page._build_chips(
                ok=True,
                runner=Runner(
                    "umu", "Proton", [],
                    {"PROTONPATH": "/home/user/.../GE-Proton9-20"},
                ),
            )
            self.assertIn("Proton GE: 9-20", self._chip_texts(page))

            page._build_chips(
                ok=True,
                runner=Runner("wine", "Wine", ["wine"], {}),
            )
            self.assertIn("Proton GE: none", self._chip_texts(page))

    def test_switch_language_does_not_duplicate_keyboard_shortcuts(self):
        """Regression test mirroring the status-bar duplication bug above:

        keyboard shortcuts are wired once from __init__, not from
        _build_ui() - which reruns on every switch_language() - so a
        repeated language switch must not stack duplicate QShortcuts each
        firing their action again.
        """
        from PySide6.QtGui import QShortcut
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            window.switch_language()
            window.switch_language()
            shortcuts = window.findChildren(QShortcut)
            sequences = [s.key().toString() for s in shortcuts]
            self.assertEqual(sequences.count("Ctrl+F"), 1)
            self.assertEqual(sequences.count("Ctrl+,"), 1)

    def test_ctrl_f_shortcut_jumps_to_mod_manager_and_focuses_search(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            window._focus_mod_search()
            self.assertEqual(window.tabs.currentIndex(), window._page_index["modmanager"])

    def test_switch_language_does_not_duplicate_status_bar_widgets(self):
        """Regression test for a status bar widget-duplication bug.

        The status bar (GitHub link button, Language/Theme combos) used to be
        built inside _build_ui(), which also runs on every switch_language()
        rebuild. QStatusBar.addPermanentWidget()/addWidget() are not
        idempotent - nothing removed the previous widgets first - so
        repeated language switches stacked duplicate GitHub buttons (and
        would have duplicated the Language/Theme combos too). Status bar
        construction now happens exactly once, from __init__.
        """
        from PySide6.QtWidgets import QApplication, QPushButton

        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            window.switch_language()
            window.switch_language()
            github_buttons = [
                b
                for b in window.statusBar().findChildren(QPushButton)
                if b.text() == "GitHub"
            ]
            self.assertEqual(len(github_buttons), 1)

    def test_switch_language_does_not_stack_mod_counter_timers(self):
        """Regression test mirroring the status-bar duplication bug above.

        The topbar's mod-counter QTimer is created in _build_ui(), which
        reruns on every switch_language(), but is parented to the long-lived
        window rather than to the central widget that rebuild tears down -
        so nothing stopped the previous one and every language switch left
        another live timer firing update_mod_counter() on the same cadence.
        """
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()

            def live_counter_timers() -> int:
                return len(
                    [
                        t
                        for t in window.findChildren(QTimer)
                        if t.isActive() and t.interval() == 5000
                    ]
                )

            self.assertEqual(live_counter_timers(), 1)
            window.switch_language()
            self.assertEqual(live_counter_timers(), 1)
            window.switch_language()
            self.assertEqual(live_counter_timers(), 1)
            self.assertTrue(window._mod_counter_timer.isActive())

    def test_status_bar_pickers_follow_a_change_made_from_the_settings_page(self):
        """The status bar is built once and never rebuilt, so its Theme/Font
        combos only track the active values if something refreshes them.

        Left stale after the Settings page's own pickers applied a change,
        a combo still showing the previous value could not switch back to
        it - picking the item it already displays emits no
        currentIndexChanged, so nothing would be applied.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.themes import active_theme
        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            self.assertEqual(window._status_theme_combo.currentData(), active_theme())

            # Exactly what settings_page.py's pickers call.
            window.apply_theme("dusk")
            window.apply_font_size(18)
            window.apply_font_family("Inter")

            self.assertEqual(active_theme(), "dusk")
            self.assertEqual(window._status_theme_combo.currentData(), "dusk")
            self.assertEqual(window._status_fontsize_combo.currentData(), 18)
            self.assertEqual(window._status_font_combo.currentData(), "Inter")


class UserModsTrackerTests(unittest.TestCase):
    def test_build_command_rejects_missing_profile(self):
        from commander_gui.launcher import Runner, build_command

        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp)
            (gamma / "ModOrganizer.exe").touch()
            with self.assertRaises(LaunchError):
                build_command(
                    str(gamma),
                    Runner("wine", "Wine", ["wine"]),
                    profile="missing",
                )

    def test_move_updates_the_profile_that_started_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            settings = CliSettings(
                profiles=[
                    CliProfile(active=True, profile_name="active"),
                    CliProfile(active=False, profile_name="moved"),
                ]
            )
            with patch("commander_gui.settings.settings_path", return_value=path):
                _save_moved_profile(
                    settings,
                    "moved",
                    [
                        ("Anomaly", "/new/anomaly"),
                        ("GAMMA", "/new/gamma"),
                        ("Cache", "/new/cache"),
                    ],
                )
                loaded = load_settings(path)
            moved = next(p for p in loaded.profiles if p.profile_name == "moved")
            active = next(p for p in loaded.profiles if p.profile_name == "active")
            self.assertEqual(moved.gamma, "/new/gamma")
            self.assertEqual(moved.anomaly, "/new/anomaly")
            self.assertEqual(moved.cache, "/new/cache")
            self.assertEqual(active.gamma, "gamma/gamma")

    def test_move_rewrites_mo2_ini_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "new-gamma"
            gamma.mkdir()
            ini = gamma / "ModOrganizer.ini"
            ini.write_text(
                "gamePath=@ByteArray(Z:\\\\old\\\\anomaly)\n"
                "1\\binary=Z:/old/anomaly/bin/AnomalyDX11.exe\n"
                "1\\workingDirectory=Z:/old/anomaly/bin\n"
                '10\\arguments="Z:\\\\old\\\\anomaly"\n',
                encoding="utf-8",
            )
            _rewrite_mo2_ini_paths(
                gamma,
                [
                    ("/old/anomaly", "/new/anomaly"),
                    ("/old/gamma", "/new/gamma"),
                ],
            )
            text = ini.read_text(encoding="utf-8")
            self.assertNotIn("/old/anomaly", text)
            self.assertIn("/new/anomaly", text)
            self.assertTrue((gamma / "ModOrganizer.ini.gammagui.bak").is_file())

    def test_move_copy_streams_tree_without_losing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            target = Path(tmp) / "target"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "file.txt").write_text("content", encoding="utf-8")
            _copy_dir_tree(source, target, lambda _message: None)
            self.assertEqual(
                (target / "nested" / "file.txt").read_text(encoding="utf-8"),
                "content",
            )

    def test_move_removes_partial_destination_on_copy_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "first.txt").write_text("first", encoding="utf-8")
            (source / "second.txt").write_text("second", encoding="utf-8")

            original_copy = _copy_dir_tree

            def failing_copy(src, dst, report, cancel_event=None):
                original_copy(src, dst, report, cancel_event)
                raise OSError("injected copy failure")

            with (
                patch("commander_gui.ui.utilities_page._copy_dir_tree", failing_copy),
                self.assertRaises(OSError),
            ):
                _move_folders([("Source", str(source))], destination, lambda _: None)
            self.assertTrue(source.is_dir())
            self.assertFalse((destination / "source").exists())

    def test_move_restores_all_sources_when_later_deletion_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_one = root / "one"
            source_two = root / "two"
            destination = root / "destination"
            source_one.mkdir()
            source_two.mkdir()
            destination.mkdir()
            (source_one / "one.txt").write_text("one", encoding="utf-8")
            (source_two / "two.txt").write_text("two", encoding="utf-8")
            original_rmtree = shutil.rmtree
            deletions = 0

            def failing_rmtree(path, *args, **kwargs):
                nonlocal deletions
                if Path(path) in (source_one, source_two):
                    deletions += 1
                    if deletions == 2:
                        raise OSError("injected deletion failure")
                return original_rmtree(path, *args, **kwargs)

            with (
                patch("commander_gui.ui.utilities_page.shutil.rmtree", failing_rmtree),
                self.assertRaises(OSError),
            ):
                _move_folders(
                    [("One", str(source_one)), ("Two", str(source_two))],
                    destination,
                    lambda _: None,
                )
            self.assertEqual((source_one / "one.txt").read_text(), "one")
            self.assertEqual((source_two / "two.txt").read_text(), "two")
            self.assertFalse((destination / "one").exists())
            self.assertFalse((destination / "two").exists())

    def test_move_done_keeps_recovery_state_when_ini_rewrite_fails(self):
        from types import SimpleNamespace

        page = UtilitiesPage.__new__(UtilitiesPage)
        page._move_task = object()
        page._move_profile_name = "profile"
        page._move_sources = [("Anomaly", "/old/anomaly"), ("GAMMA", "/old/gamma")]
        page._move_cancel_btn = SimpleNamespace(hide=lambda: None)
        page._move_progress = SimpleNamespace(
            on_finished=lambda *_args: None, status_message=lambda *_args: None
        )
        page._set_buttons_enabled = lambda _enabled: None
        page._refresh_move_paths = lambda: None
        page.window = SimpleNamespace(
            settings=SimpleNamespace(profiles=[]),
            set_install_busy=lambda _busy: None,
            refresh_settings=lambda: None,
            _pages={},
        )
        moved = [("Anomaly", "/new/anomaly"), ("GAMMA", "/new/gamma")]
        with (
            patch(
                "commander_gui.ui.utilities_page._rewrite_mo2_ini_paths",
                side_effect=ValueError("injected INI failure"),
            ),
            patch("commander_gui.ui.utilities_page._save_moved_profile"),
            patch(
                "commander_gui.ui.utilities_page.gui_settings.save_gui_settings"
            ) as save,
            patch("commander_gui.ui.utilities_page.QMessageBox.information"),
        ):
            page._on_move_done(moved)
        save.assert_not_called()

    def test_move_done_re_enables_the_move_button_once_the_lock_is_released(self):
        """Regression test: _on_move_done() refreshed the Move controls while

        the global install lock was still held, so _refresh_move_paths()
        disabled the "Move installation" button - and _set_buttons_enabled()
        never touches that button, so nothing re-enabled it. After a
        successful move the button stayed dead until the user navigated away
        from Utilities and back. _on_move_error() already refreshes after
        releasing the lock, which is the order the success path must match.
        """
        from types import SimpleNamespace

        from PySide6.QtWidgets import QApplication, QLabel, QPushButton

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name in ("anomaly", "gamma", "cache"):
                (base / name).mkdir()
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(base / "anomaly"),
                gamma=str(base / "gamma"),
                cache=str(base / "cache"),
            )
            page = UtilitiesPage.__new__(UtilitiesPage)
            page._move_task = object()
            page._move_profile_name = "Test"
            page._move_sources = [
                ("Anomaly", profile.anomaly),
                ("GAMMA", profile.gamma),
                ("Cache", profile.cache),
            ]
            page._move_cancel_btn = SimpleNamespace(hide=lambda: None)
            page._move_progress = SimpleNamespace(
                on_finished=lambda *_args: None,
                status_message=lambda *_args: None,
            )
            page._move_btn = QPushButton()
            page._move_anomaly_label = QLabel()
            page._move_gamma_label = QLabel()
            page._move_cache_label = QLabel()
            page.buttons = []
            page.fresh_reset_button = QPushButton()
            page.gamma_reset_button = QPushButton()
            page.full_uninstall_button = QPushButton()
            page.fresh_reset_hint = QLabel()
            page._runner = None
            page._wipe_task = None

            settings = CliSettings(profiles=[profile])

            class FakeWindow:
                # The move itself is still holding the global lock here.
                install_busy = True

                def __init__(self):
                    self.settings = settings
                    self._pages: dict = {}

                def set_install_busy(self, busy, operation=None):
                    type(self).install_busy = busy

                def refresh_settings(self):
                    pass

            window = FakeWindow()
            page.window = window

            moved = [
                ("Anomaly", str(base / "moved-anomaly")),
                ("GAMMA", str(base / "moved-gamma")),
                ("Cache", str(base / "moved-cache")),
            ]
            # _save_moved_profile is patched out below, so mirror what it
            # would have written or the consistency re-check reports a failure.
            profile.anomaly, profile.gamma, profile.cache = (
                path for _label, path in moved
            )
            with (
                patch("commander_gui.ui.utilities_page._rewrite_mo2_ini_paths"),
                patch("commander_gui.ui.utilities_page._save_moved_profile"),
                patch(
                    "commander_gui.ui.utilities_page.gui_settings.save_gui_settings"
                ),
                patch("commander_gui.ui.utilities_page.QMessageBox.information"),
            ):
                page._on_move_done(moved)

            self.assertFalse(window.install_busy)
            self.assertTrue(page._move_btn.isEnabled())

    def _make_utilities_page(self, tmp):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.utilities_page import UtilitiesPage

        QApplication.instance() or QApplication([])
        profile = CliProfile(
            active=True,
            profile_name="Test",
            anomaly=str(Path(tmp) / "anomaly"),
            gamma=str(Path(tmp) / "gamma"),
            cache=str(Path(tmp) / "cache"),
        )

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

            def set_install_busy(self, *a, **kw):
                pass

        return UtilitiesPage(FakeWindow())

    def test_move_installation_is_a_tool_row_above_create_log_dump(self):
        """Regression test: Move Installation used to be its own

        always-visible card; it is now a Tools row (title/description/Run,
        same as every other tool) sitting right above Create Log Dump,
        and Run opens it in its own popup dialog instead.
        """
        from PySide6.QtWidgets import QLabel

        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_utilities_page(tmp)
            titles = [
                label.text()
                for label in page.findChildren(QLabel)
                if label.objectName() == "section2"
            ]
            self.assertIn("Move Installation", titles)
            self.assertIn("Create Log Dump", titles)
            self.assertLess(
                titles.index("Move Installation"), titles.index("Create Log Dump")
            )
            self.assertFalse(page._move_dialog.isVisible())

    def test_move_installation_run_opens_the_dialog(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_utilities_page(tmp)
            page._open_move_dialog()
            self.assertTrue(page._move_dialog.isVisible())
            self.assertEqual(page._move_dialog.windowTitle(), "Move Installation")

    def test_move_dialog_refuses_to_close_while_a_move_is_running(self):
        from PySide6.QtGui import QCloseEvent

        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_utilities_page(tmp)
            page._open_move_dialog()
            page._move_task = object()
            with patch(
                "commander_gui.ui.utilities_page.QMessageBox.information"
            ) as mock_info:
                event = QCloseEvent()
                page._move_dialog.closeEvent(event)
            mock_info.assert_called_once()
            self.assertFalse(event.isAccepted())
            self.assertTrue(page._move_dialog.isVisible())

    def test_move_dialog_closes_normally_once_idle(self):
        from PySide6.QtGui import QCloseEvent

        with tempfile.TemporaryDirectory() as tmp:
            page = self._make_utilities_page(tmp)
            page._open_move_dialog()
            page._move_task = None
            event = QCloseEvent()
            page._move_dialog.closeEvent(event)
            self.assertTrue(event.isAccepted())

    def test_wipe_guard_rejects_broad_paths(self):
        target = Path.home()
        self.assertFalse(_safe_wipe_path(str(target), target))

    def test_wipe_guard_rejects_subdirectories_of_system_roots(self):
        for raw in ("/etc/NetworkManager", "/var/lib/anything", "/run/user/1000"):
            target = Path(raw)
            self.assertFalse(
                _safe_wipe_path(raw, target), f"{raw} should be rejected"
            )

    def test_wipe_guard_rejects_home_parent_exactly(self):
        home_parent = str(Path.home().parent)
        self.assertFalse(_safe_wipe_path(home_parent, Path(home_parent)))

    def test_wipe_guard_allows_nested_path_under_own_home(self):
        target = Path.home() / "Games" / "anomaly" / "gamma"
        self.assertTrue(_safe_wipe_path(str(target), target))

    def test_cache_preflight_summary_html_explains_each_category(self):
        """Regression test for a real user report: "392 reusable, 3

        missing" read as inaccurate/confusing because the dialog never
        explained what "reusable" actually means (skips re-download
        only - every mod still gets freshly extracted) nor what happens
        to "missing"/"unreadable" archives. The rewritten HTML summary
        must cover all of that, plus real table alignment instead of
        space-padded plain text (which doesn't align in a proportional
        font - see the "hero" button/QMessageBox font investigation
        this session).
        """
        result = CacheArchiveVerifyResult(
            verified=["OkMod.7z"] * 392,
            missing=[
                "33D_Shader_Scopes_for_GAMMA_5.02.7z",
                "GAMMA_Immaculate_Munitions_Pack.36.7z",
                "winchester_1892_billwa_stalker_anomaly-1.0.zip",
            ],
            mismatched=["OldList.7z"],
            unreadable=["Corrupt.7z"],
        )
        html = _cache_preflight_summary_html(result, include_anomaly=False)

        self.assertIn("GAMMA Reset deletes and reinstalls the GAMMA modpack", html)
        self.assertIn("Anomaly itself is not touched", html)
        self.assertIn("skip re-downloading", html)
        self.assertIn("<table", html)
        self.assertIn("<td>Already downloaded, valid</td><td align='right'><b>392</b></td>", html)
        self.assertIn("<td>Need downloading</td><td align='right'><b>3</b></td>", html)
        self.assertIn("<td>Outdated - official list changed</td><td align='right'><b>1</b></td>", html)
        self.assertIn("<td>Unreadable</td><td align='right'><b>1</b></td>", html)
        self.assertIn("33D_Shader_Scopes_for_GAMMA_5.02.7z", html)
        self.assertIn("simply be downloaded, same as a normal install", html)
        self.assertIn("not evidence anything is broken", html)
        self.assertIn("Couldn't be verified", html)
        self.assertIn("Continue with the reset?", html)

    def test_cache_preflight_summary_html_worded_for_fresh_reset(self):
        result = CacheArchiveVerifyResult(verified=["OkMod.7z"])
        html = _cache_preflight_summary_html(result, include_anomaly=True)
        self.assertIn(
            "Fresh Reset deletes and reinstalls both Anomaly and the GAMMA modpack",
            html,
        )

    def test_cache_preflight_summary_html_omits_explanations_when_all_clean(self):
        result = CacheArchiveVerifyResult(verified=["OkMod.7z"] * 5)
        html = _cache_preflight_summary_html(result, include_anomaly=False)
        self.assertNotIn("<ul>", html)
        self.assertNotIn("Need downloading:</b>", html)
        self.assertNotIn("Outdated:</b>", html)
        self.assertNotIn("Unreadable:</b>", html)

    def test_cache_preflight_summary_html_truncates_long_name_lists(self):
        result = CacheArchiveVerifyResult(
            missing=[f"Mod{i}.7z" for i in range(20)]
        )
        html = _cache_preflight_summary_html(result, include_anomaly=False)
        self.assertIn("Mod14.7z", html)
        self.assertNotIn("Mod15.7z", html)
        self.assertIn("... and 5 more", html)

    def test_cache_preflight_summary_html_escapes_archive_names(self):
        result = CacheArchiveVerifyResult(missing=["<script>evil.7z"])
        html = _cache_preflight_summary_html(result, include_anomaly=False)
        self.assertNotIn("<script>evil.7z", html)
        self.assertIn("&lt;script&gt;evil.7z", html)

    def test_reset_uninstall_buttons_enable_on_partial_interrupted_install(self):
        """A crashed/interrupted download leaves folders without
        ModOrganizer.exe/AnomalyLauncher.exe - anomaly_installed()/
        gamma_installed() would say "not installed", but the buttons must
        still be clickable so the user can wipe and retry (_wipe_folders()
        already handles a partial folder fine - it just deletes it)."""
        import tempfile
        from unittest.mock import MagicMock

        from PySide6.QtWidgets import QApplication, QLabel, QPushButton

        from commander_gui.ui.utilities_page import UtilitiesPage

        QApplication.instance() or QApplication([])

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            anomaly, gamma, cache = base / "anomaly", base / "gamma", base / "cache"
            anomaly.mkdir()
            gamma.mkdir()
            cache.mkdir()
            # Partial content only - no ModOrganizer.exe/.ini, no
            # AnomalyLauncher.exe/fsgame.ltx, no MO2 profile folder.
            (anomaly / "some_partial_download.tmp").write_text("x")

            profile = CliProfile(
                active=True,
                profile_name="test",
                anomaly=str(anomaly),
                gamma=str(gamma),
                cache=str(cache),
            )
            page = UtilitiesPage.__new__(UtilitiesPage)
            page.window = MagicMock()
            page.window.settings = CliSettings(profiles=[profile])
            page.window.install_busy = False
            page.fresh_reset_button = QPushButton()
            page.gamma_reset_button = QPushButton()
            page.full_uninstall_button = QPushButton()
            page.fresh_reset_hint = QLabel()

            page._update_fresh_reset_enabled()

            self.assertTrue(page.fresh_reset_button.isEnabled())
            self.assertTrue(page.gamma_reset_button.isEnabled())
            self.assertTrue(page.full_uninstall_button.isEnabled())

    def test_reset_uninstall_buttons_disabled_when_nothing_exists(self):
        """No folders at all: still nothing to wipe, buttons stay disabled."""
        import tempfile
        from unittest.mock import MagicMock

        from PySide6.QtWidgets import QApplication, QLabel, QPushButton

        from commander_gui.ui.utilities_page import UtilitiesPage

        QApplication.instance() or QApplication([])

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profile = CliProfile(
                active=True,
                profile_name="test",
                anomaly=str(base / "anomaly"),
                gamma=str(base / "gamma"),
                cache=str(base / "cache"),
            )
            page = UtilitiesPage.__new__(UtilitiesPage)
            page.window = MagicMock()
            page.window.settings = CliSettings(profiles=[profile])
            page.window.install_busy = False
            page.fresh_reset_button = QPushButton()
            page.gamma_reset_button = QPushButton()
            page.full_uninstall_button = QPushButton()
            page.fresh_reset_hint = QLabel()

            page._update_fresh_reset_enabled()

            self.assertFalse(page.fresh_reset_button.isEnabled())
            self.assertFalse(page.gamma_reset_button.isEnabled())
            self.assertFalse(page.full_uninstall_button.isEnabled())

    def test_network_reads_are_bounded(self):
        class Response:
            def __init__(self, data):
                self.headers = {}
                self.data = data

            def read(self, size):
                chunk, self.data = self.data[:size], self.data[size:]
                return chunk

        self.assertEqual(read_response_bytes(Response(b"safe"), 4), b"safe")
        with self.assertRaises(ValueError):
            read_response_bytes(Response(b"too large"), 4)

    def test_network_urlopen_rejects_non_http_schemes(self):
        # A profile's editable mod_list_url/mod_pack_maker_url must never be
        # able to make the GUI read an arbitrary local file.
        with self.assertRaises(ValueError):
            network.urlopen("file:///etc/passwd", timeout=1)
        with self.assertRaises(ValueError):
            network.urlopen(
                urllib.request.Request("file:///etc/passwd"), timeout=1
            )

    def test_urlopen_with_retry_recovers_from_a_transient_failure(self):
        """A momentary connection drop must not read as a permanent failure -

        most clear up within a retry or two.
        """
        import urllib.error

        with (
            patch("commander_gui.network.time.sleep"),
            patch("commander_gui.network.urllib.request.urlopen") as mock_open,
        ):
            mock_open.side_effect = [
                urllib.error.URLError("temporary failure"),
                Mock(),
            ]
            result = network.urlopen_with_retry("https://example.com/x", timeout=1)
        self.assertEqual(mock_open.call_count, 2)
        self.assertIsNotNone(result)

    def test_urlopen_with_retry_gives_up_on_a_non_retryable_http_error(self):
        """A 404 will never succeed no matter how many times it's asked -

        must not waste retries/time on it.
        """
        import urllib.error

        with (
            patch("commander_gui.network.time.sleep") as mock_sleep,
            patch("commander_gui.network.urllib.request.urlopen") as mock_open,
        ):
            mock_open.side_effect = urllib.error.HTTPError(
                "https://example.com/x", 404, "Not Found", {}, None
            )
            with self.assertRaises(urllib.error.HTTPError):
                network.urlopen_with_retry("https://example.com/x", timeout=1)
        self.assertEqual(mock_open.call_count, 1)
        mock_sleep.assert_not_called()

    def test_about_page_shows_the_update_button_only_when_a_newer_tag_exists(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.about_page import AboutPage

        QApplication.instance() or QApplication([])

        class FakeWindow:
            settings = CliSettings(profiles=[])

        with patch("commander_gui.ui.about_page.BackgroundTask") as mock_task_cls:
            page = AboutPage(FakeWindow())
        self.assertTrue(page.update_button.isHidden())
        mock_task_cls.assert_called_once()

        page._on_commander_update_checked(None)
        self.assertTrue(page.update_button.isHidden())

        page._on_commander_update_checked("v9.9.9")
        self.assertFalse(page.update_button.isHidden())
        self.assertIn("v9.9.9", page.update_button.text())

    def test_main_window_update_status_shows_up_to_date_in_green(self):
        """Regression test: the status-bar COMMANDER update indicator

        must show a clear, non-clickable "Up to date" state (green) when
        no newer release exists.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.common import OK_GREEN
        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            try:
                window._on_commander_update_status_checked(None)
                button = window._update_status_button
                self.assertEqual(button.text(), "COMMANDER is up to date")
                self.assertFalse(button.isEnabled())
                self.assertIn(OK_GREEN.name(), button.styleSheet())
            finally:
                window.close()

    def test_main_window_update_status_shows_update_available_and_opens_releases(self):
        """Regression test: an available update must show a clickable,

        orange "update available" status that opens the GitHub Releases
        page when clicked.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.common import WARN
        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            try:
                window._on_commander_update_status_checked("v9.9.9")
                button = window._update_status_button
                self.assertEqual(button.text(), "COMMANDER update available")
                self.assertTrue(button.isEnabled())
                self.assertIn(WARN.name(), button.styleSheet())

                with patch(
                    "commander_gui.ui.main_window.QDesktopServices.openUrl"
                ) as mock_open:
                    button.click()
                mock_open.assert_called_once()
                opened_url = mock_open.call_args.args[0].toString()
                self.assertIn(
                    "SSH-Kitty/STALKER-GAMMA-COMMANDER/releases", opened_url
                )
            finally:
                window.close()

    def test_main_window_checks_for_a_commander_update_at_startup(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            try:
                self.assertEqual(
                    window._update_status_button.text(), "Checking for updates..."
                )
                with patch(
                    "commander_gui.ui.main_window.BackgroundTask"
                ) as mock_task_cls:
                    window._check_commander_update_status()
                mock_task_cls.assert_called_once()
            finally:
                window.close()

    def test_main_window_update_status_sits_left_of_github_with_a_separator(self):
        """Regression test: the desired reading order in the status

        bar's bottom-right corner is "COMMANDER is up to date" >
        separator > "GitHub". Getting this right by relying on the
        relative order of multiple separate addPermanentWidget() calls
        proved unreliable in practice (needed 3 attempts) - this locks
        in the actual layout order of the single container widget that
        replaced that approach, using widget geometry (x position) as
        the source of truth rather than call order.
        """
        from PySide6.QtWidgets import QApplication, QLabel, QPushButton

        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            try:
                update_button = window._update_status_button
                container = update_button.parent()
                github_button = next(
                    child
                    for child in container.findChildren(QPushButton)
                    if child.objectName() == "githubLink"
                )
                separator = next(
                    child
                    for child in container.findChildren(QLabel)
                    if "|" in child.text()
                )
                window.show()
                self.assertLess(update_button.x(), separator.x())
                self.assertLess(separator.x(), github_button.x())
            finally:
                window.close()

    def test_main_window_update_status_offers_self_update_when_running_as_appimage(
        self,
    ):
        """Regression test: an AppImage install must offer one-click

        self-update instead of just opening the releases page - the
        signal is the APPIMAGE env var the AppImage runtime itself sets
        (the same one autostart.py already keys off).
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": tmp, "APPIMAGE": "/fake/Commander.AppImage"},
            ),
        ):
            window = MainWindow()
            try:
                window._on_commander_update_status_checked("v9.9.9")
                button = window._update_status_button

                with patch.object(
                    window, "_offer_commander_self_update"
                ) as mock_offer:
                    button.click()
                mock_offer.assert_called_once_with("v9.9.9")
            finally:
                window.close()

    def test_main_window_update_status_opens_releases_page_without_appimage(self):
        """Regression test: a source/AUR install (no APPIMAGE env var) must

        keep today's plain "open the releases page" behavior - self-update
        would fight pacman for an AUR install, see packaging/aur/PKGBUILD.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False),
        ):
            os.environ.pop("APPIMAGE", None)
            window = MainWindow()
            try:
                window._on_commander_update_status_checked("v9.9.9")
                button = window._update_status_button
                with patch(
                    "commander_gui.ui.main_window.QDesktopServices.openUrl"
                ) as mock_open:
                    button.click()
                mock_open.assert_called_once()
                opened_url = mock_open.call_args.args[0].toString()
                self.assertIn(
                    "SSH-Kitty/STALKER-GAMMA-COMMANDER/releases", opened_url
                )
            finally:
                window.close()

    def test_commander_appimage_path_reads_the_appimage_env_var(self):
        with patch.dict(os.environ, {"APPIMAGE": "/fake/Commander.AppImage"}):
            self.assertEqual(
                commander_appimage_path(), Path("/fake/Commander.AppImage")
            )
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(commander_appimage_path())

    def test_commander_update_asset_url_matches_the_appimage_build_naming(self):
        self.assertEqual(
            commander_update_asset_url("v1.2.9"),
            "https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases/"
            "download/v1.2.9/STALKER-GAMMA-COMMANDER-1.2.9-x86_64.AppImage",
        )
        # A tag with no leading "v" must not have a character stripped.
        self.assertEqual(
            commander_update_asset_url("1.2.9"),
            "https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases/"
            "download/1.2.9/STALKER-GAMMA-COMMANDER-1.2.9-x86_64.AppImage",
        )

    def test_download_commander_update_writes_the_file_next_to_the_running_appimage(
        self,
    ):
        body = b"fake appimage bytes"

        class FakeResponse:
            def __init__(self, data: bytes, headers: dict | None = None):
                self._data = data
                self.headers = headers or {}

            def read(self, size: int = -1) -> bytes:
                if size < 0 or size >= len(self._data):
                    chunk, self._data = self._data, b""
                    return chunk
                chunk, self._data = self._data[:size], self._data[size:]
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        def fake_urlopen(request, timeout=None):
            return FakeResponse(body, {"Content-Length": str(len(body))})

        with tempfile.TemporaryDirectory() as tmp:
            running_path = Path(tmp) / "Commander.AppImage"
            running_path.write_bytes(b"old bytes")
            with patch(
                "commander_gui.self_update.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ):
                downloaded = download_commander_update("v9.9.9", running_path)
            try:
                self.assertEqual(downloaded.parent, running_path.parent)
                self.assertEqual(downloaded.read_bytes(), body)
            finally:
                downloaded.unlink(missing_ok=True)

    def test_download_commander_update_raises_a_distinct_error_on_404(self):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, None
            )

        with tempfile.TemporaryDirectory() as tmp:
            running_path = Path(tmp) / "Commander.AppImage"
            running_path.write_bytes(b"old bytes")
            with patch(
                "commander_gui.self_update.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ), self.assertRaises(CommanderUpdateAssetNotFoundError):
                download_commander_update("v9.9.9", running_path)

    def test_download_commander_update_rejects_an_oversized_body(self):
        class FakeResponse:
            def __init__(self, headers: dict):
                self.headers = headers
                self._served = False

            def read(self, size: int = -1) -> bytes:
                if self._served:
                    return b""
                self._served = True
                return b"x" * 1024

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        def fake_urlopen(request, timeout=None):
            return FakeResponse({"Content-Length": str(10 * 1024**3)})

        with tempfile.TemporaryDirectory() as tmp:
            running_path = Path(tmp) / "Commander.AppImage"
            running_path.write_bytes(b"old bytes")
            with patch(
                "commander_gui.self_update.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ), self.assertRaises(CommanderSelfUpdateError):
                download_commander_update("v9.9.9", running_path)
            # No leftover temp file next to the running AppImage.
            leftovers = [
                p for p in running_path.parent.iterdir() if p != running_path
            ]
            self.assertEqual(leftovers, [])

    def test_install_commander_update_atomically_swaps_and_marks_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            running_path = Path(tmp) / "Commander.AppImage"
            running_path.write_bytes(b"old bytes")
            running_path.chmod(0o644)

            downloaded = Path(tmp) / ".Commander.AppImage.new"
            downloaded.write_bytes(b"new bytes")

            install_commander_update(downloaded, running_path)

            self.assertEqual(running_path.read_bytes(), b"new bytes")
            self.assertFalse(downloaded.exists())
            self.assertTrue(os.access(running_path, os.X_OK))

    def test_download_and_install_commander_update_requires_an_appimage(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(CommanderSelfUpdateError),
        ):
            download_and_install_commander_update("v9.9.9")

    def test_help_page_snapshot_reflects_the_active_profile(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.help_page import HelpPage

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="Mine", gamma="/games/gamma")

        class FakeWindow:
            settings = CliSettings(profiles=[profile])

            def refresh_settings(self):
                pass

        page = HelpPage(FakeWindow())
        page.refresh()
        self.assertIn("Mine", page.snapshot_status.text())

        FakeWindow.settings = CliSettings(profiles=[])
        page.refresh()
        self.assertIn("No active profile", page.snapshot_status.text())

    def test_system_check_page_constructs_and_refreshes_without_error(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.system_check_page import SystemCheckPage

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="Mine")

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

        # _collect_checks shells out to several system tools - not something
        # a unit test should actually run - so the background check dispatch
        # itself is mocked, leaving construction/refresh's own wiring as
        # what's under test here.
        with patch("commander_gui.ui.system_check_page.BackgroundTask") as mock_task_cls:
            page = SystemCheckPage(FakeWindow())
            page.refresh()
        self.assertTrue(mock_task_cls.called)

    def test_system_check_page_shows_a_first_load_error_immediately(self):
        """Regression test: _show_error() used to always defer applying an

        error behind the 2-second minimum-display timer, even on the very
        first check (before _last_check_ts is ever set). _show_checks()
        already skips that delay on a first load because the "Scanning..."
        placeholders are on screen already - _show_error() must match that,
        or a fast failure on startup sits behind a pointless artificial
        delay instead of being reported right away.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.system_check_page import SystemCheckPage

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="Mine")

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

        with patch("commander_gui.ui.system_check_page.BackgroundTask"):
            page = SystemCheckPage(FakeWindow())
        self.assertEqual(page._last_check_ts, 0.0)

        page._show_error("boom")

        self.assertFalse(page._settling)
        self.assertIsNone(page._pending_timer)
        self.assertIn("boom", page.summary.text())

    def test_system_check_copy_button_revert_tolerates_a_deleted_button(self):
        """Regression test: the "Copy install command" button's 1.5s revert

        timer used to call btn.setText() unconditionally. A refresh() that
        lands while that timer is still pending rebuilds every row via
        clear_layout(), which schedules deleteLater() on the old button -
        if the C++ object is already gone by the time the timer fires,
        touching it must be a no-op instead of raising RuntimeError.
        """
        import shiboken6
        from PySide6.QtWidgets import QApplication, QPushButton

        from commander_gui.ui.system_check_page import SystemCheckPage

        QApplication.instance() or QApplication([])
        button = QPushButton("Copy install command")
        shiboken6.delete(button)

        SystemCheckPage._revert_copy_button(button)  # must not raise

    def test_settings_page_persists_discord_rpc_toggle_and_client_id(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.settings_page import SettingsPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            profile = CliProfile(active=True, profile_name="Test")

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

                def switch_language(self):
                    pass

                def apply_theme(self):
                    pass

            page = SettingsPage(FakeWindow())
            page._discord_enable_check.setChecked(True)
            self.assertTrue(gui_settings.load_gui_settings()["discord_rpc_enabled"])

            page._discord_client_id_edit.setText("123456789")
            page._discord_client_id_edit.editingFinished.emit()
            self.assertEqual(
                gui_settings.load_gui_settings()["discord_client_id"], "123456789"
            )

            # refresh() must load it back onto the widgets too.
            gui_settings.save_gui_settings(
                discord_rpc_enabled=False, discord_client_id="987654321"
            )
            page.refresh()
            self.assertFalse(page._discord_enable_check.isChecked())
            self.assertEqual(page._discord_client_id_edit.text(), "987654321")

    def test_settings_page_font_change_resyncs_the_status_bar_pickers(self):
        """Regression test: the status bar now exposes the same font family,

        font size and theme settings as this page, and both are on screen at
        once. The status bar is built exactly once and only re-selects its
        combos from refresh_settings(), which apply_font_size()/
        apply_font_family()/apply_theme() never call - so a change made here
        left the status bar showing the previous value until some unrelated
        page refresh happened to fix it.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.settings_page import SettingsPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            profile = CliProfile(active=True, profile_name="Test")

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

                def __init__(self):
                    self.status_bar_refreshes = 0

                def apply_font_size(self, size):
                    gui_settings.save_gui_settings(font_size=int(size))

                def apply_font_family(self, family):
                    gui_settings.save_gui_settings(font_family=family)

                def refresh_settings(self):
                    self.status_bar_refreshes += 1

            window = FakeWindow()
            page = SettingsPage(window)
            self.assertEqual(window.status_bar_refreshes, 0)

            page._font_size_combo.setCurrentIndex(
                page._font_size_combo.findData(18)
            )
            self.assertEqual(gui_settings.load_gui_settings()["font_size"], 18)
            self.assertEqual(window.status_bar_refreshes, 1)

            page._font_family_combo.setCurrentIndex(
                page._font_family_combo.findData("Inter")
            )
            self.assertEqual(gui_settings.load_gui_settings()["font_family"], "Inter")
            self.assertEqual(window.status_bar_refreshes, 2)

            # The other direction already worked and must keep working: a
            # change made from the status bar shows up on this page's own
            # combos the next time Settings is opened (refresh()).
            gui_settings.save_gui_settings(font_size=21, font_family="Liberation Sans")
            page.refresh()
            self.assertEqual(page._font_size_combo.currentData(), 21)
            self.assertEqual(page._font_family_combo.currentData(), "Liberation Sans")

    def test_discord_presence_is_a_noop_without_pypresence_installed(self):
        """Regression test: pypresence is NOT a hard dependency of this app -

        every discord_rpc function must degrade silently when it isn't
        installed, never raise ImportError up into a launch.
        """
        import builtins

        from commander_gui.discord_rpc import (
            start_presence,
            stop_presence,
            update_presence,
        )

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pypresence":
                raise ImportError("no module named pypresence")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            rpc = start_presence("123456789")
        self.assertIsNone(rpc)
        update_presence(rpc, "Playing S.T.A.L.K.E.R. GAMMA")  # must not raise
        stop_presence(rpc)  # must not raise

    def test_discord_presence_requires_a_client_id(self):
        from commander_gui.discord_rpc import start_presence

        self.assertIsNone(start_presence(""))

    def test_discord_presence_swallows_connection_failures(self):
        from commander_gui.discord_rpc import start_presence

        fake_module = Mock()
        fake_module.Presence.side_effect = OSError("no discord IPC socket")
        with patch.dict("sys.modules", {"pypresence": fake_module}):
            self.assertIsNone(start_presence("123456789"))

    def test_format_playtime_renders_hours_and_minutes(self):
        from commander_gui.ui.common import format_playtime

        self.assertEqual(format_playtime(0), "0m")
        self.assertEqual(format_playtime(59), "0m")
        self.assertEqual(format_playtime(60), "1m")
        self.assertEqual(format_playtime(3600), "1h 0m")
        self.assertEqual(format_playtime(3600 * 2 + 60 * 15), "2h 15m")

    def test_format_last_played_renders_never_and_a_readable_date(self):
        from commander_gui.ui.common import format_last_played

        self.assertEqual(format_last_played(None), "Never")
        self.assertEqual(format_last_played(0), "Never")
        rendered = format_last_played(1_700_000_000.0)
        # Exact string depends on the local timezone - just check the shape
        # (European DD/MM/YYYY, not ISO).
        self.assertRegex(rendered, r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$")

    def test_free_space_bytes_walks_up_to_the_nearest_existing_ancestor(self):
        """Regression test: an install's target folder (e.g. profile.gamma)

        need not exist yet when the confirm dialog runs the free-space
        check - free_space_bytes() must walk up to whatever ancestor
        directory does exist instead of raising/returning None just
        because the exact leaf path hasn't been created.
        """
        from commander_gui.ui.common import free_space_bytes

        with tempfile.TemporaryDirectory() as tmp:
            existing = free_space_bytes(tmp)
            self.assertIsNotNone(existing)
            self.assertGreater(existing, 0)
            nested = free_space_bytes(str(Path(tmp) / "not" / "created" / "yet"))
            self.assertEqual(nested, existing)

    def test_crash_dump_names_finds_mdmp_files_and_tolerates_missing_folder(self):
        from commander_gui.ui.common import crash_dump_names

        with tempfile.TemporaryDirectory() as tmp:
            anomaly = Path(tmp) / "anomaly"
            self.assertEqual(crash_dump_names(anomaly), set())

            logs_dir = anomaly / "appdata" / "logs"
            logs_dir.mkdir(parents=True)
            (logs_dir / "xray_steamuser_09-15-26_02-52-03.mdmp").touch()
            (logs_dir / "xray_steamuser.log").touch()
            self.assertEqual(
                crash_dump_names(anomaly),
                {"xray_steamuser_09-15-26_02-52-03.mdmp"},
            )

    def test_record_playtime_accumulates_onto_the_active_profiles_total(self):
        import time

        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.play_page import PlayPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            profile = CliProfile(active=True, profile_name="Mine")

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

            page = PlayPage(FakeWindow())
            page._launch_started_at = None
            page._record_playtime()  # no-op: nothing was actually launched
            self.assertEqual(
                gui_settings.load_gui_settings()["playtime_seconds"], {}
            )
            self.assertEqual(
                gui_settings.load_gui_settings()["last_played_ts"], {}
            )

            before = time.time()
            page._launch_started_at = time.monotonic() - 90  # 1.5 minutes ago
            page._record_playtime()
            state = gui_settings.load_gui_settings()
            recorded = state["playtime_seconds"]["Mine"]
            self.assertGreaterEqual(recorded, 90)
            self.assertIsNone(page._launch_started_at)
            last_played = state["last_played_ts"]["Mine"]
            self.assertGreaterEqual(last_played, before)

    def test_playtime_label_shows_under_launch_game_and_updates_live(self):
        """Regression test: the Play page shows a "Total playtime" label

        under the Launch Game button, and it must refresh immediately
        after a session ends (not only on the next full page refresh).
        """
        import time

        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.play_page import PlayPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            profile = CliProfile(active=True, profile_name="Mine")

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

            page = PlayPage(FakeWindow())
            self.assertIn("Total playtime", page.playtime_label.text())

            page._launch_started_at = time.monotonic() - 3600
            page._record_playtime()
            self.assertIn("1h", page.playtime_label.text())

    def test_check_flip_priority_crash_skips_the_filesystem_when_not_pending(self):
        """No Flip Priority since the last session - must not even look

        for crash dumps, so this stays free for every ordinary launch.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.play_page import PlayPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            profile = CliProfile(active=True, profile_name="Mine")

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

            page = PlayPage(FakeWindow())
            page._crash_check_pending = False

            with (
                patch(
                    "commander_gui.ui.play_page.crash_dump_names"
                ) as mock_crash_dumps,
                patch("commander_gui.ui.play_page.QMessageBox.warning") as mock_warn,
            ):
                page._check_flip_priority_crash()

            mock_crash_dumps.assert_not_called()
            mock_warn.assert_not_called()

    def test_check_flip_priority_crash_warns_when_a_new_dump_appears(self):
        """Regression test: a new crash dump appearing after a Flip

        Priority-flagged session must warn the user it may be the cause,
        and the pending flag must be cleared afterward regardless.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.play_page import PlayPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            profile = CliProfile(
                active=True, profile_name="Mine", anomaly=str(Path(tmp) / "anomaly")
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

            page = PlayPage(FakeWindow())
            gui_settings.save_gui_settings(
                flip_priority_pending={"Mine": True}
            )
            page._crash_check_pending = True
            page._pre_launch_crash_dumps = set()

            with (
                patch(
                    "commander_gui.ui.play_page.crash_dump_names",
                    return_value={"xray_steamuser_09-15-26_02-52-03.mdmp"},
                ),
                patch("commander_gui.ui.play_page.QMessageBox.warning") as mock_warn,
            ):
                page._check_flip_priority_crash()

            mock_warn.assert_called_once()
            self.assertIn("Flip Priority", mock_warn.call_args.args[2])
            self.assertFalse(page._crash_check_pending)
            self.assertEqual(
                gui_settings.load_gui_settings()["flip_priority_pending"], {}
            )

    def test_check_flip_priority_crash_stays_quiet_after_the_poll_window_ends(self):
        """Pending flag set, but no new crash dump ever appears - no

        warning once the bounded poll (see _poll_for_flip_priority_crash)
        gives up, and the flag is still consumed so a later, unrelated
        crash isn't mis-attributed to this same flip.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.play_page import PlayPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch("commander_gui.ui.play_page._CRASH_POLL_MAX_ATTEMPTS", 2),
        ):
            profile = CliProfile(
                active=True, profile_name="Mine", anomaly=str(Path(tmp) / "anomaly")
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

            page = PlayPage(FakeWindow())
            gui_settings.save_gui_settings(flip_priority_pending={"Mine": True})
            page._crash_check_pending = True
            page._pre_launch_crash_dumps = {"old.mdmp"}

            with (
                patch(
                    "commander_gui.ui.play_page.crash_dump_names",
                    return_value={"old.mdmp"},
                ),
                patch("commander_gui.ui.play_page.QMessageBox.warning") as mock_warn,
            ):
                page._check_flip_priority_crash()
                # First attempt found nothing - a real timer is now
                # armed for the next one; drive it directly instead of
                # waiting on the real clock.
                self.assertIsNotNone(page._crash_poll_timer)
                page._crash_poll_timer.stop()
                page._poll_for_flip_priority_crash()

            mock_warn.assert_not_called()
            self.assertIsNone(page._crash_poll_timer)
            self.assertFalse(page._crash_check_pending)
            self.assertEqual(
                gui_settings.load_gui_settings()["flip_priority_pending"], {}
            )

    def test_check_flip_priority_crash_catches_a_dump_that_appears_late(self):
        """Regression test for a real crash: X-Ray took 39s to finish

        writing the .mdmp after the session was already reported closed,
        so a single immediate check missed it and the user never saw the
        warning. The poll must keep trying, not give up after one look.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.play_page import PlayPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            profile = CliProfile(
                active=True, profile_name="Mine", anomaly=str(Path(tmp) / "anomaly")
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

            page = PlayPage(FakeWindow())
            gui_settings.save_gui_settings(flip_priority_pending={"Mine": True})
            page._crash_check_pending = True
            page._pre_launch_crash_dumps = set()

            with (
                patch(
                    "commander_gui.ui.play_page.crash_dump_names",
                    side_effect=[
                        set(),
                        set(),
                        {"xray_steamuser_09-15-26_05-00-57.mdmp"},
                    ],
                ),
                patch("commander_gui.ui.play_page.QMessageBox.warning") as mock_warn,
            ):
                page._check_flip_priority_crash()
                mock_warn.assert_not_called()
                page._crash_poll_timer.stop()
                page._poll_for_flip_priority_crash()
                mock_warn.assert_not_called()
                page._crash_poll_timer.stop()
                page._poll_for_flip_priority_crash()

            mock_warn.assert_called_once()
            self.assertIsNone(page._crash_poll_timer)

    def test_playtime_records_when_game_exe_exits_even_if_mo2_stays_open(self):
        """Regression test: MO2 stays running after the game it launched

        closes, so playtime must be recorded when the actual game
        executable exits, not only when MO2 itself exits (the bug: a user
        who closed just the game, leaving MO2 open, never got their
        playtime recorded).
        """
        import time

        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.play_page import PlayPage

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            profile = CliProfile(active=True, profile_name="Mine")

            class FakeWindow:
                settings = CliSettings(profiles=[profile])

            page = PlayPage(FakeWindow())
            page._monitoring_mo2 = True
            page._mo2_seen = True
            page._mo2_launch_pids = {123}
            page._game_exe_name = "AnomalyDX11AVX.exe"
            page._pre_launch_game_pids = set()
            page._game_seen = False
            page._launch_started_at = time.monotonic() - 300  # 5 minutes ago
            # _launch_started_at is 300s ago so _record_playtime() below has
            # something to record - but that's also past _STALL_WARNING_SECONDS
            # (180s), so without this the first _on_launch_check() call would
            # hit the (unmocked, real) stall-warning QMessageBox.question()
            # instead of the game/MO2 detection this test actually exercises,
            # and hang forever waiting for a click that never comes.
            page._stall_warned = True

            with (
                patch(
                    "commander_gui.ui.play_page.mo2_pids", return_value={123}
                ),
                patch(
                    "commander_gui.ui.play_page.exe_pids",
                    side_effect=[{456}, set()],
                ),
            ):
                page._on_launch_check("GAMMA", ["ModOrganizer.exe"], Path("/tmp/x.log"))
                self.assertTrue(page._game_seen)
                self.assertIsNotNone(page._launch_started_at)

                page._on_launch_check("GAMMA", ["ModOrganizer.exe"], Path("/tmp/x.log"))

            recorded = gui_settings.load_gui_settings()["playtime_seconds"]["Mine"]
            self.assertGreaterEqual(recorded, 300)
            self.assertIsNone(page._launch_started_at)
            self.assertIsNone(page._game_exe_name)
            # MO2 itself never exited in this scenario - launch lock/button
            # state must not have been released yet.
            self.assertTrue(page._monitoring_mo2)

    def test_scheduled_update_check_is_skipped_within_a_day_of_the_last_one(self):
        import time

        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            gui_settings.save_gui_settings(last_update_check_ts=time.time())
            with patch("commander_gui.ui.main_window.BackgroundTask") as mock_task_cls:
                window._maybe_check_for_updates_in_background()
            mock_task_cls.assert_not_called()

    def test_scheduled_update_check_runs_when_overdue_and_notifies_on_a_hit(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.main_window import MainWindow

        QApplication.instance() or QApplication([])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
        ):
            window = MainWindow()
            gui_settings.save_gui_settings(last_update_check_ts=0.0)
            profile = CliProfile(active=True, profile_name="Test")
            window.settings = CliSettings(profiles=[profile])
            with patch("commander_gui.ui.main_window.BackgroundTask") as mock_task_cls:
                window._maybe_check_for_updates_in_background()
            mock_task_cls.assert_called_once()

            with patch("commander_gui.ui.main_window.notify_desktop") as mock_notify:
                window._on_scheduled_update_checked(Mock(update_available=True))
            mock_notify.assert_called_once()
            self.assertGreater(gui_settings.load_gui_settings()["last_update_check_ts"], 0.0)

            with patch("commander_gui.ui.main_window.notify_desktop") as mock_notify:
                window._on_scheduled_update_checked(Mock(update_available=False))
            mock_notify.assert_not_called()

    def test_offer_crash_report_starts_a_log_dump_task_only_on_yes(self):
        """Regression test: a failed launch must offer to build a bug report

        in one extra click, reusing the same create_log_dump()/
        launch_assistant() pieces Utilities' own log-dump button uses -
        but only when the user actually says yes.
        """
        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.ui.play_page import PlayPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(Path(tmp) / "gamma"),
                cache=str(Path(tmp) / "cache"),
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False

                def refresh_settings(self):
                    pass

            page = PlayPage(FakeWindow())

            with (
                patch(
                    "commander_gui.ui.play_page.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.No,
                ),
                patch("commander_gui.ui.play_page.BackgroundTask") as mock_task_cls,
            ):
                page._offer_crash_report()
            mock_task_cls.assert_not_called()

            with (
                patch(
                    "commander_gui.ui.play_page.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
                patch("commander_gui.ui.play_page.BackgroundTask") as mock_task_cls,
            ):
                page._offer_crash_report()
            mock_task_cls.assert_called_once()

    def test_find_enabled_mod_file_conflicts_reports_shared_paths_in_priority_order(
        self,
    ):
        from commander_gui.modlist import find_enabled_mod_file_conflicts

        with tempfile.TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "mods"
            for name in ("ModA", "ModB", "ModC"):
                (mods_dir / name / "gamedata" / "textures").mkdir(parents=True)
                # A loose root-level file every mod happens to share (MO2's
                # own meta.ini, a README, ...) must never be reported - see
                # test_find_enabled_mod_file_conflicts_ignores_loose_root_files.
                (mods_dir / name / "meta.ini").write_text("modid=0")
            (mods_dir / "ModA" / "gamedata" / "textures" / "wall.dds").write_text("a")
            (mods_dir / "ModB" / "gamedata" / "textures" / "wall.dds").write_text("b")
            (mods_dir / "ModC" / "gamedata" / "textures" / "floor.dds").write_text("c")
            # ModA is file-top (highest priority, per the file-order vs
            # on-screen-order convention), ModB is file-bottom, ModC has
            # no overlapping file at all.
            lines = ["+ModA", "+ModB", "+ModC"]

            conflicts = find_enabled_mod_file_conflicts(lines, mods_dir)

            self.assertEqual(len(conflicts), 1)
            path, owners = conflicts[0]
            self.assertEqual(path, "gamedata/textures/wall.dds")
            self.assertEqual(owners, ["ModA", "ModB"])

    def test_find_enabled_mod_file_conflicts_ignores_loose_root_files(self):
        """Regression test: a user reported the output as unreadable -

        "meta.ini — ModA wins over ModB, ModC, ...[100+ names]". meta.ini
        (MO2's own per-mod bookkeeping file) sits at nearly every real
        mod's root (confirmed against a real profile: 539 of 777 mod
        folders) and is never read by the game - only gamedata/appdata/
        bin/db content, what MO2 actually virtualizes into the game, can
        be a real conflict.
        """
        from commander_gui.modlist import find_enabled_mod_file_conflicts

        with tempfile.TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "mods"
            for name in ("ModA", "ModB"):
                (mods_dir / name).mkdir(parents=True)
                (mods_dir / name / "meta.ini").write_text("modid=0")
                (mods_dir / name / "README.txt").write_text("read me")
            lines = ["+ModA", "+ModB"]

            conflicts = find_enabled_mod_file_conflicts(lines, mods_dir)
            self.assertEqual(conflicts, [])

    def test_find_enabled_mod_file_conflicts_ignores_disabled_and_missing_mods(self):
        from commander_gui.modlist import find_enabled_mod_file_conflicts

        with tempfile.TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "mods"
            (mods_dir / "ModA").mkdir(parents=True)
            (mods_dir / "ModA" / "x.txt").write_text("a")
            (mods_dir / "ModB").mkdir(parents=True)
            (mods_dir / "ModB" / "x.txt").write_text("b")
            # ModB is disabled, ModC is enabled but not actually installed.
            lines = ["+ModA", "-ModB", "+ModC", "-Category_separator"]

            conflicts = find_enabled_mod_file_conflicts(lines, mods_dir)
            self.assertEqual(conflicts, [])

    def test_check_file_conflicts_shows_a_message_when_none_are_found(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
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

            page = ModManagerPage(FakeWindow())
            page._lines = ["+SoloMod"]
            with patch("commander_gui.ui.mod_manager_page.QMessageBox.information") as mock_info:
                page._on_conflicts_found([])
            mock_info.assert_called_once()
            self.assertFalse(page.conflicts_button.text() == "Scanning...")

    def test_check_file_conflicts_opens_the_conflicts_dialog_when_found(self):
        """Regression test: a real report shared one conflicting file

        ("meta.ini") whose owner list alone ran to 100+ mod names on a
        single QMessageBox line - unreadable. Results must now open the
        dedicated, searchable _ConflictsDialog instead of a QMessageBox.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
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

            page = ModManagerPage(FakeWindow())
            page._lines = ["+ModA", "+ModB"]
            with patch(
                "commander_gui.ui.mod_manager_page._ConflictsDialog.exec"
            ) as mock_exec:
                page._on_conflicts_found(
                    [("gamedata/scripts/x.script", ["ModA", "ModB"])]
                )
            mock_exec.assert_called_once()

    def test_conflicts_dialog_defaults_to_custom_mods_only(self):
        """Regression test: a user reported that even the collapsed,

        one-row-per-mod-pair table (~1000 rows on a real profile) was
        still too much to be useful - almost all of it is GAMMA's own
        curated, intentional internal overrides. The dialog now defaults
        to only the pairs touching something filed under "Custom Mods"
        (a mod the user installed themselves) - the "Show all" checkbox
        reveals the full picture for anyone who wants it.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import _ConflictsDialog

        QApplication.instance() or QApplication([])
        dialog = _ConflictsDialog(
            rows=[
                ("ModA", "ModB", 10),
                ("ModC", "ModD", 1),
            ],
            custom_mods={"ModC"},
        )
        try:
            # Default view: only the pair touching "ModC".
            self.assertFalse(dialog.show_all_checkbox.isChecked())
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(dialog.table.item(0, 0).text(), "ModC")
            self.assertEqual(dialog.count_label.text(), "Showing 1 of 1")

            dialog.show_all_checkbox.setChecked(True)
            self.assertEqual(dialog.table.rowCount(), 2)
            self.assertEqual(dialog.count_label.text(), "Showing 2 of 2")

            dialog.show_all_checkbox.setChecked(False)
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(dialog.table.item(0, 0).text(), "ModC")
        finally:
            dialog.close()

    def test_conflicts_dialog_explains_when_nothing_is_custom_installed(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import _ConflictsDialog

        QApplication.instance() or QApplication([])
        dialog = _ConflictsDialog(
            rows=[("ModA", "ModB", 10)],
            custom_mods=set(),
        )
        try:
            self.assertEqual(dialog.table.rowCount(), 0)
            self.assertIn("haven't installed", dialog.info.text())
        finally:
            dialog.close()

    def test_conflicts_dialog_populates_and_filters(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import _ConflictsDialog

        QApplication.instance() or QApplication([])
        dialog = _ConflictsDialog(
            rows=[
                ("ModA", "ModB", 10),
                ("ModC", "ModD", 1),
            ],
            custom_mods={"ModA", "ModC"},
        )
        try:
            self.assertEqual(dialog.table.rowCount(), 2)
            self.assertEqual(dialog.count_label.text(), "Showing 2 of 2")

            dialog.search.setText("ModA")
            self.assertEqual(dialog.count_label.text(), "Showing 1 of 2")
            visible_rows = [
                row
                for row in range(dialog.table.rowCount())
                if not dialog.table.isRowHidden(row)
            ]
            self.assertEqual(len(visible_rows), 1)
            self.assertEqual(dialog.table.item(visible_rows[0], 0).text(), "ModA")

            dialog.search.setText("ModC")
            self.assertEqual(dialog.count_label.text(), "Showing 1 of 2")

            dialog.search.setText("")
            self.assertEqual(dialog.count_label.text(), "Showing 2 of 2")
        finally:
            dialog.close()

    def test_summarize_mod_conflicts_collapses_duplicate_pairs_with_a_count(self):
        from commander_gui.modlist import summarize_mod_conflicts

        conflicts = [
            ("gamedata/scripts/a.script", ["ModA", "ModB"]),
            ("gamedata/scripts/b.script", ["ModA", "ModB"]),
            ("gamedata/scripts/c.script", ["ModA", "ModB", "ModC"]),
            ("gamedata/textures/d.dds", ["ModX", "ModY"]),
        ]
        rows = summarize_mod_conflicts(conflicts)
        by_pair = {(winner, loser): count for winner, loser, count in rows}
        self.assertEqual(by_pair[("ModA", "ModB")], 3)
        self.assertEqual(by_pair[("ModA", "ModC")], 1)
        self.assertEqual(by_pair[("ModX", "ModY")], 1)
        # Sorted by file count descending first.
        self.assertEqual(rows[0][:2], ("ModA", "ModB"))

    def test_custom_mod_names_reads_the_custom_mods_category(self):
        from commander_gui.modlist import custom_mod_names

        lines = [
            "+ModA",
            "-Foo_separator",
            "-UserInstalled",
            "+AnotherOne",
            "-Custom Mods_separator",
        ]
        self.assertEqual(custom_mod_names(lines), {"UserInstalled", "AnotherOne"})

    def test_custom_mod_names_is_empty_without_the_category(self):
        from commander_gui.modlist import custom_mod_names

        self.assertEqual(custom_mod_names(["+ModA", "-Foo_separator"]), set())

    def test_check_file_conflicts_warns_instead_of_crashing_with_no_profile(self):
        """Regression test: every other action in this file

        (_resolve_install_target, _finalize_mod, _delete_mod_folders,
        _open_mod_folder_by_name, _open_mo2) catches _active_profile()'s
        RuntimeError and shows a warning dialog - _check_file_conflicts
        used to let it propagate uncaught instead.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])

        class FakeWindow:
            settings = CliSettings(profiles=[])
            install_busy = False

            def refresh_settings(self):
                pass

        page = ModManagerPage(FakeWindow())
        with patch("commander_gui.ui.mod_manager_page.QMessageBox.warning") as mock_warn:
            page._check_file_conflicts()  # must not raise
        mock_warn.assert_called_once()

    def test_create_backup_warns_instead_of_crashing_with_no_profile(self):
        """Regression test: _create_backup() caught only OSError, so the

        RuntimeError _modlist_path()/_active_profile() raise when the
        active profile is gone (deleted on the Profiles page while this
        page's combo still lists its MO2 profiles) escaped the slot
        instead of being reported like every other action here does.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])

        class FakeWindow:
            settings = CliSettings(profiles=[])
            install_busy = False

            def refresh_settings(self):
                pass

            def update_mod_counter(self):
                pass

        with patch(
            "commander_gui.ui.mod_manager_page.mo2_running", return_value=False
        ):
            page = ModManagerPage(FakeWindow())
            page.profile_combo.addItem("G.A.M.M.A")
            with patch(
                "commander_gui.ui.mod_manager_page.QMessageBox.warning"
            ) as mock_warn:
                page._create_backup()  # must not raise
        mock_warn.assert_called_once()

    def test_move_to_category_menu_omits_the_mods_own_category(self):
        """Regression test: the context menu compared grouped()'s raw

        category names against the tree header's DISPLAY text, which
        carries a "(N)" mod-count suffix - so the comparison never matched
        and a mod's own category was offered under "Move to Category",
        where picking it silently reordered the load order (and showed the
        reorder warning) for a move that should not have been possible.
        """
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication, QMenu

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])

        class FakeWindow:
            settings = CliSettings(profiles=[])
            install_busy = False

            def refresh_settings(self):
                pass

            def update_mod_counter(self):
                pass

        page = ModManagerPage(FakeWindow())
        page._lines = ["+ModA", "-Weapons_separator", "+ModB", "-Gameplay_separator"]
        page._populate_tree()

        weapons_header = next(
            page.tree.topLevelItem(i)
            for i in range(page.tree.topLevelItemCount())
            if page.tree.topLevelItem(i).text(0).startswith("Weapons")
        )
        mod_item = weapons_header.child(0)
        page.tree.itemAt = lambda *_args: mod_item

        built: list[QMenu] = []

        class CapturingMenu(QMenu):
            """Records the built menu instead of opening it modally."""

            def exec(self, *_args, **_kwargs):
                built.append(self)

        with (
            patch("commander_gui.ui.mod_manager_page.mo2_running", return_value=False),
            patch("commander_gui.ui.mod_manager_page.QMenu", CapturingMenu),
        ):
            page._show_context_menu(QPoint(0, 0))

        menu = built[0]
        submenu = next(a.menu() for a in menu.actions() if a.menu() is not None)
        offered = [a.text() for a in submenu.actions()]
        self.assertEqual(offered, ["Gameplay"])

    def test_flip_priority_warns_instead_of_silently_doing_nothing_while_mo2_runs(self):
        """Regression test: _on_flip_priority()'s own top-of-handler guard

        used to also check _mo2_running() and return with zero feedback -
        no dialog, no status change - whenever MO2/the game happened to
        be running (a completely normal state while tuning mod order),
        making the button appear to do nothing. _write_lines() already
        has the correct handling for this exact condition (a proper "Mod
        Organizer is running" warning) - the click must reach it instead
        of being silently swallowed one level up.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            profiles_dir = gamma / "profiles" / "G.A.M.M.A"
            profiles_dir.mkdir(parents=True)
            (profiles_dir / "modlist.txt").write_text("+ModA\n+ModB\n", encoding="utf-8")
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(gamma),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False

                def refresh_settings(self):
                    pass

                def update_mod_counter(self):
                    pass

            with patch(
                "commander_gui.ui.mod_manager_page.mo2_running", return_value=False
            ):
                page = ModManagerPage(FakeWindow())
            if page.profile_combo.count() == 0:
                page.profile_combo.addItem("G.A.M.M.A")
            page._lines = ["+ModA", "+ModB"]
            page._reorder_warned = True  # skip the "reverse load order?" confirm

            with (
                patch(
                    "commander_gui.ui.mod_manager_page.mo2_running", return_value=True
                ),
                patch(
                    "commander_gui.ui.mod_manager_page.QMessageBox.warning"
                ) as mock_warn,
            ):
                page._on_flip_priority()

            # _write_lines()'s own guard now correctly fires its "Mod
            # Organizer is running" warning (previously never reached at
            # all); _on_flip_priority()'s failure branch also shows its
            # own generic "Flip Failed" after write_lines() reports
            # failure - two dialogs is pre-existing behavior for any
            # write failure reason, not something this fix changes. The
            # point under test is that the MO2 warning is no longer
            # silently skipped.
            self.assertTrue(mock_warn.called)
            first_call_message = mock_warn.call_args_list[0].args[1]
            self.assertIn("Mod Organizer", first_call_message)

    def test_flip_priority_marks_the_profile_for_a_crash_check(self):
        """Regression test: a successful Flip Priority must flag the

        active profile so the Play page can warn if the very next
        session ends in a crash (see PlayPage._check_flip_priority_crash).
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui import gui_settings
        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            profiles_dir = gamma / "profiles" / "G.A.M.M.A"
            profiles_dir.mkdir(parents=True)
            (profiles_dir / "modlist.txt").write_text("+ModA\n+ModB\n", encoding="utf-8")
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(gamma),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False

                def refresh_settings(self):
                    pass

                def update_mod_counter(self):
                    pass

            with (
                tempfile.TemporaryDirectory() as xdg,
                patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
                patch(
                    "commander_gui.ui.mod_manager_page.mo2_running", return_value=False
                ),
            ):
                page = ModManagerPage(FakeWindow())
                if page.profile_combo.count() == 0:
                    page.profile_combo.addItem("G.A.M.M.A")
                page._lines = ["+ModA", "+ModB"]
                page._reorder_warned = True

                self.assertEqual(
                    gui_settings.load_gui_settings().get("flip_priority_pending", {}),
                    {},
                )
                page._on_flip_priority()
                self.assertTrue(
                    gui_settings.load_gui_settings()["flip_priority_pending"]["Test"]
                )

    def test_rename_category_ui_writes_the_renamed_separator_and_reloads(self):
        """Regression test: the Mod Manager page's new "Rename Category..."

        action (right-click on a category header) must actually persist
        the rename to modlist.txt and reload the tree, mirroring the
        existing per-mod rename flow.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            profiles_dir = gamma / "profiles" / "G.A.M.M.A"
            profiles_dir.mkdir(parents=True)
            modlist_path = profiles_dir / "modlist.txt"
            modlist_path.write_text("+ModA\n-Old Category_separator\n", encoding="utf-8")
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(gamma),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False

                def refresh_settings(self):
                    pass

                def update_mod_counter(self):
                    pass

            with (
                tempfile.TemporaryDirectory() as xdg,
                patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
                patch(
                    "commander_gui.ui.mod_manager_page.mo2_running", return_value=False
                ),
            ):
                page = ModManagerPage(FakeWindow())
                if page.profile_combo.count() == 0:
                    page.profile_combo.addItem("G.A.M.M.A")
                page._lines = ["+ModA", "-Old Category_separator"]

                with patch(
                    "commander_gui.ui.mod_manager_page.QInputDialog.getText",
                    return_value=("New Category", True),
                ):
                    page._rename_category("Old Category")

            self.assertEqual(
                modlist_path.read_text(encoding="utf-8").splitlines(),
                ["+ModA", "-New Category_separator"],
            )

    def test_mod_manager_count_label_dedups_a_duplicate_name(self):
        """Regression test: GAMMA's own official modlist.txt has been

        confirmed to list at least one mod twice (e.g. "G.A.M.M.A.
        Vehicles in Darkscape") - the Mod Manager count label must count
        it once, matching MO2's own (name-keyed) count, not once per
        line.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            profiles_dir = gamma / "profiles" / "G.A.M.M.A"
            profiles_dir.mkdir(parents=True)
            (profiles_dir / "modlist.txt").write_text(
                "+DupMod\n+DupMod\n+OtherMod\n", encoding="utf-8"
            )
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(gamma),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
            )

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False

                def refresh_settings(self):
                    pass

                def update_mod_counter(self):
                    pass

            with (
                tempfile.TemporaryDirectory() as xdg,
                patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
                patch(
                    "commander_gui.ui.mod_manager_page.mo2_running", return_value=False
                ),
            ):
                page = ModManagerPage(FakeWindow())
                if page.profile_combo.count() == 0:
                    page.profile_combo.addItem("G.A.M.M.A")
                    page._load_mods()

            self.assertEqual(page.count_label.text(), "2 mods (2 enabled)")

    def test_collapse_all_button_collapses_then_expands_every_category(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(
                tmp, "+ModA\n-CatOne_separator\n+ModB\n-CatTwo_separator\n"
            )
            headers = [
                page.tree.topLevelItem(i)
                for i in range(page.tree.topLevelItemCount())
            ]
            self.assertTrue(all(h.isExpanded() for h in headers))

            page._toggle_collapse_all()

            self.assertTrue(all(not h.isExpanded() for h in headers))
            self.assertEqual(page.collapse_all_button.text(), "Expand All")

            page._toggle_collapse_all()

            self.assertTrue(all(h.isExpanded() for h in headers))
            self.assertEqual(page.collapse_all_button.text(), "Collapse All")

    def _make_mod_manager_page(self, tmp, modlist_text: str):
        """A real ModManagerPage over a temp profile, for category-

        management tests (create/rename/move/delete-category, and the
        user_created_categories tracking that gates "Delete Category").
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        gamma = Path(tmp) / "gamma"
        profiles_dir = gamma / "profiles" / "G.A.M.M.A"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "modlist.txt").write_text(modlist_text, encoding="utf-8")
        profile = CliProfile(
            active=True,
            profile_name="Test",
            anomaly=str(Path(tmp) / "anomaly"),
            gamma=str(gamma),
            cache=str(Path(tmp) / "cache"),
            mo2_profile="G.A.M.M.A",
        )

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

            def update_mod_counter(self):
                pass

        with patch(
            "commander_gui.ui.mod_manager_page.mo2_running", return_value=False
        ):
            page = ModManagerPage(FakeWindow())
            if page.profile_combo.count() == 0:
                page.profile_combo.addItem("G.A.M.M.A")
                page._load_mods()
        return page

    def test_set_selected_profile_also_updates_the_active_cli_profile(self):
        """Regression test: the Dashboard's "Profile overview" card (and

        Play page launches) read CliProfile.mo2_profile directly, not
        MO2's own selected_profile - clicking "Use as MO2 selected
        profile" for a different profile updated ModOrganizer.ini but left
        every one of those readers silently showing/using the old profile
        until it happened to get edited some other way.
        """
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            page._on_set_selected_done("NewProfile", 0, "")
            self.assertEqual(page._active_profile().mo2_profile, "NewProfile")

    def test_set_selected_profile_is_a_noop_when_already_matching(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            calls = []
            page.window.refresh_settings = lambda: calls.append(True)
            page._on_set_selected_done("G.A.M.M.A", 0, "")
            self.assertEqual(calls, [])

    def test_create_mo2_profile_copies_the_chosen_source_modlist(self):
        from PySide6.QtWidgets import QComboBox, QDialog, QLineEdit

        def _fake_exec(dialog_self):
            dialog_self.findChild(QLineEdit).setText("NewProf")
            combo = dialog_self.findChild(QComboBox)
            combo.setCurrentIndex(combo.findText("G.A.M.M.A"))
            return QDialog.DialogCode.Accepted

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            with (
                patch("commander_gui.ui.mod_manager_page.QDialog.exec", _fake_exec),
                patch.object(page, "_load_profiles"),
            ):
                page._create_mo2_profile()

            new_modlist = Path(page._active_profile().gamma) / "profiles" / "NewProf" / "modlist.txt"
            self.assertEqual(
                new_modlist.read_text(encoding="utf-8"), "+ModA\n-Foo_separator\n"
            )
            self.assertEqual(page._pending_profile_select, "NewProf")

    def test_create_mo2_profile_empty_choice_writes_an_empty_modlist(self):
        from PySide6.QtWidgets import QComboBox, QDialog, QLineEdit

        def _fake_exec(dialog_self):
            dialog_self.findChild(QLineEdit).setText("BlankProf")
            # The source combo defaults to whatever profile is currently
            # being viewed - explicitly pick "(Empty profile)" (always
            # item 0) instead.
            dialog_self.findChild(QComboBox).setCurrentIndex(0)
            return QDialog.DialogCode.Accepted

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            with (
                patch("commander_gui.ui.mod_manager_page.QDialog.exec", _fake_exec),
                patch.object(page, "_load_profiles"),
            ):
                page._create_mo2_profile()

            new_modlist = Path(page._active_profile().gamma) / "profiles" / "BlankProf" / "modlist.txt"
            self.assertEqual(new_modlist.read_text(encoding="utf-8"), "\n")

    def test_create_mo2_profile_rejects_a_name_that_already_exists(self):
        from PySide6.QtWidgets import QDialog, QLineEdit

        def _fake_exec(dialog_self):
            dialog_self.findChild(QLineEdit).setText("G.A.M.M.A")
            return QDialog.DialogCode.Accepted

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            with (
                patch("commander_gui.ui.mod_manager_page.QDialog.exec", _fake_exec),
                patch("commander_gui.ui.mod_manager_page.QMessageBox.warning") as mock_warn,
                patch.object(page, "_load_profiles") as mock_reload,
            ):
                page._create_mo2_profile()
            mock_warn.assert_called_once()
            mock_reload.assert_not_called()

    def test_create_mo2_profile_blocked_while_mo2_is_running(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            with (
                patch.object(page, "_mo2_running", return_value=True),
                patch("commander_gui.ui.mod_manager_page.QDialog.exec") as mock_exec,
            ):
                page._create_mo2_profile()
            mock_exec.assert_not_called()

    def test_rename_mo2_profile_moves_the_folder_and_updates_cli_profile(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            gamma = Path(page._active_profile().gamma)
            with (
                patch(
                    "commander_gui.ui.mod_manager_page.QInputDialog.getText",
                    return_value=("Renamed", True),
                ),
                patch("commander_gui.ui.mod_manager_page.run_sync") as mock_run_sync,
                patch.object(page, "_load_profiles"),
            ):
                page._rename_mo2_profile()

            self.assertFalse((gamma / "profiles" / "G.A.M.M.A").exists())
            self.assertTrue((gamma / "profiles" / "Renamed" / "modlist.txt").is_file())
            self.assertEqual(page._active_profile().mo2_profile, "Renamed")
            self.assertEqual(page._pending_profile_select, "Renamed")
            # MO2's own selected profile was never "G.A.M.M.A" in this
            # setup (_mo2_selected_profile defaults to "") - nothing to fix
            # up on the MO2 side.
            mock_run_sync.assert_not_called()

    def test_rename_mo2_profile_updates_mo2_selected_profile_when_it_matched(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            page._mo2_selected_profile = "G.A.M.M.A"
            with (
                patch(
                    "commander_gui.ui.mod_manager_page.QInputDialog.getText",
                    return_value=("Renamed", True),
                ),
                patch("commander_gui.ui.mod_manager_page.run_sync") as mock_run_sync,
                patch.object(page, "_load_profiles"),
            ):
                page._rename_mo2_profile()
            mock_run_sync.assert_called_once_with(
                ["mo2", "config", "set", "selected-profile", "Renamed"], timeout=30
            )

    def test_delete_mo2_profile_calls_the_cli_after_confirmation(self):
        from PySide6.QtWidgets import QMessageBox

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            # A second profile so this isn't refused as "the last one".
            page.profile_combo.addItem("Other")
            page.profile_combo.setCurrentText("G.A.M.M.A")
            with (
                patch("commander_gui.ui.mod_manager_page.QMessageBox") as mock_mb,
                patch("commander_gui.ui.mod_manager_page.BackgroundTask") as mock_task_cls,
            ):
                instance = mock_mb.return_value
                instance.exec.return_value = None
                instance.clickedButton.return_value = instance.addButton.return_value
                mock_mb.Icon = QMessageBox.Icon
                mock_mb.ButtonRole = QMessageBox.ButtonRole
                mock_mb.StandardButton = QMessageBox.StandardButton
                page._delete_mo2_profile()
            mock_task_cls.assert_called_once()
            args, _ = mock_task_cls.call_args
            self.assertEqual(args[1], ["mo2", "profile", "delete", "G.A.M.M.A"])

    def test_delete_mo2_profile_refuses_the_last_remaining_profile(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            with (
                patch("commander_gui.ui.mod_manager_page.QMessageBox") as mock_mb,
                patch("commander_gui.ui.mod_manager_page.BackgroundTask") as mock_task_cls,
            ):
                page._delete_mo2_profile()
            mock_mb.return_value.exec.assert_not_called()
            mock_task_cls.assert_not_called()

    def test_delete_mo2_profile_declined_confirmation_does_nothing(self):
        from PySide6.QtWidgets import QMessageBox

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            page.profile_combo.addItem("Other")
            with (
                patch("commander_gui.ui.mod_manager_page.QMessageBox") as mock_mb,
                patch("commander_gui.ui.mod_manager_page.BackgroundTask") as mock_task_cls,
            ):
                instance = mock_mb.return_value
                instance.exec.return_value = None
                # clickedButton() returns neither addButton() call's result -
                # simulates Cancel/closing the dialog.
                instance.clickedButton.return_value = object()
                mock_mb.Icon = QMessageBox.Icon
                mock_mb.ButtonRole = QMessageBox.ButtonRole
                mock_mb.StandardButton = QMessageBox.StandardButton
                page._delete_mo2_profile()
            mock_task_cls.assert_not_called()

    def test_create_category_tracks_the_new_name_as_user_created(self):
        from commander_gui import gui_settings

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            with patch(
                "commander_gui.ui.mod_manager_page.QInputDialog.getText",
                return_value=("My Category", True),
            ):
                page._create_category()
            self.assertIn("My Category", page._tracked_user_categories())
            # A real GAMMA category (Foo) was never created via this UI
            # and must never be tracked as deletable.
            self.assertNotIn("Foo", page._tracked_user_categories())
            self.assertEqual(
                gui_settings.load_gui_settings()["user_created_categories"]["Test"],
                ["My Category"],
            )

    def test_load_mods_restores_a_user_category_mo2_dropped(self):
        """Regression test for a real reported bug: a "New Category" the

        user made disappeared after launching the game. MO2 itself does
        not persist a completely empty separator across a session where
        it rewrites modlist.txt (which happens at exit when launched
        through it) - this confirms the next _load_mods() (e.g. when the
        page is next opened/refreshed) re-adds it from this profile's own
        tracked user_created_categories list, and that the fix is
        actually written back to modlist.txt on disk, not just shown in
        the tree for one session.
        """
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            with patch(
                "commander_gui.ui.mod_manager_page.QInputDialog.getText",
                return_value=("My Category", True),
            ):
                page._create_category()
            self.assertIn("My Category", page._tracked_user_categories())

            modlist_path = Path(tmp) / "gamma" / "profiles" / "G.A.M.M.A" / "modlist.txt"
            # Simulate MO2 rewriting modlist.txt on its own (e.g. at game
            # exit) and dropping the still-empty category, exactly as
            # reported - "My Category" never had a mod filed into it.
            modlist_path.write_text("+ModA\n-Foo_separator\n", encoding="utf-8")

            with patch(
                "commander_gui.ui.mod_manager_page.mo2_running", return_value=False
            ):
                page._load_mods()

            categories = [name for name, _mods in grouped(page._lines)]
            self.assertIn("My Category", categories)
            # Actually persisted, not just shown for this one session.
            self.assertIn("My Category_separator", modlist_path.read_text())
            # Still tracked - nothing about the restore should untrack it.
            self.assertIn("My Category", page._tracked_user_categories())

    def test_load_mods_does_not_restore_an_untracked_category(self):
        """A real GAMMA category disappearing (e.g. removed upstream) must

        never be "restored" - only categories this profile's own
        user_created_categories list actually created are self-healed.
        """
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            modlist_path = Path(tmp) / "gamma" / "profiles" / "G.A.M.M.A" / "modlist.txt"
            modlist_path.write_text("+ModA\n", encoding="utf-8")

            with patch(
                "commander_gui.ui.mod_manager_page.mo2_running", return_value=False
            ):
                page._load_mods()

            categories = [name for name, _mods in grouped(page._lines)]
            self.assertNotIn("Foo", categories)

    def test_restore_missing_user_categories_is_a_noop_while_mo2_is_running(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            with patch(
                "commander_gui.ui.mod_manager_page.QInputDialog.getText",
                return_value=("My Category", True),
            ):
                page._create_category()

            modlist_path = Path(tmp) / "gamma" / "profiles" / "G.A.M.M.A" / "modlist.txt"
            modlist_path.write_text("+ModA\n-Foo_separator\n", encoding="utf-8")

            with patch(
                "commander_gui.ui.mod_manager_page.mo2_running", return_value=True
            ):
                page._load_mods()

            # Not restored while blocked - and the file on disk is untouched.
            categories = [name for name, _mods in grouped(page._lines)]
            self.assertNotIn("My Category", categories)
            self.assertNotIn("My Category", modlist_path.read_text())
            # Still tracked, so a later load (once MO2 closes) can retry.
            self.assertIn("My Category", page._tracked_user_categories())

    def test_rename_category_migrates_the_tracked_name(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            with patch(
                "commander_gui.ui.mod_manager_page.QInputDialog.getText",
                return_value=("My Category", True),
            ):
                page._create_category()
            with patch(
                "commander_gui.ui.mod_manager_page.QInputDialog.getText",
                return_value=("Renamed Category", True),
            ):
                page._rename_category("My Category")
            self.assertIn("Renamed Category", page._tracked_user_categories())
            self.assertNotIn("My Category", page._tracked_user_categories())

    def test_delete_category_ui_untracks_after_deletion(self):
        from PySide6.QtWidgets import QMessageBox

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            with patch(
                "commander_gui.ui.mod_manager_page.QInputDialog.getText",
                return_value=("My Category", True),
            ):
                page._create_category()

            with patch("commander_gui.ui.mod_manager_page.QMessageBox") as mock_mb:
                instance = mock_mb.return_value
                instance.exec.return_value = None
                instance.clickedButton.return_value = instance.addButton.return_value
                mock_mb.Icon = QMessageBox.Icon
                mock_mb.ButtonRole = QMessageBox.ButtonRole
                mock_mb.StandardButton = QMessageBox.StandardButton
                page._delete_category("My Category")

            self.assertNotIn("My Category", page._tracked_user_categories())
            from commander_gui.modlist import grouped

            names = [name for name, _mods in grouped(page._lines)]
            self.assertNotIn("My Category", names)

    def test_category_context_menu_only_offers_delete_for_tracked_categories(self):
        """Regression test: "Delete Category..." must never be offered

        for a real GAMMA category, only for one the user created
        themselves via "New Category" (see user_created_categories in
        gui_settings.py).
        """
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(
                tmp, "+ModA\n-Foo_separator\n+ModB\n-My Category_separator\n"
            )
            page._add_tracked_user_category("My Category")

            from PySide6.QtCore import QPoint

            def header_item(category_name):
                item = Mock()
                item.data = Mock(return_value=category_name)
                return item

            with patch("commander_gui.ui.mod_manager_page.QMenu") as mock_menu_cls:
                menu = mock_menu_cls.return_value
                menu.exec.return_value = None
                page._show_category_context_menu(header_item("Foo"), QPoint(0, 0))
                foo_actions = [c.args[0] for c in menu.addAction.call_args_list]

            with patch("commander_gui.ui.mod_manager_page.QMenu") as mock_menu_cls:
                menu = mock_menu_cls.return_value
                menu.exec.return_value = None
                page._show_category_context_menu(header_item("My Category"), QPoint(0, 0))
                tracked_actions = [c.args[0] for c in menu.addAction.call_args_list]

            self.assertNotIn("Delete Category...", foo_actions)
            self.assertIn("Delete Category...", tracked_actions)

    def test_move_category_ui_reorders_via_the_context_menu_handler(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(
                tmp, "+ModA\n-Foo_separator\n+ModB\n-Bar_separator\n"
            )
            page._move_category("Bar", -1)
            from commander_gui.modlist import grouped

            names = [name for name, _mods in grouped(page._lines)]
            self.assertEqual(names, ["Bar", "Foo"])

    def test_on_category_drop_persists_a_whole_category_drag(self):
        """Regression test: DragTree's category_dropped signal (a whole

        category header dragged onto another) must actually persist the
        reorder to modlist.txt, with the same screen-vs-file "before"
        inversion _on_tree_drop() already applies for per-mod drags.
        """
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(
                tmp, "+ModA\n-Foo_separator\n+ModB\n-Bar_separator\n"
            )
            # Dropped "above" Bar on screen (before=True, screen terms)
            # wants LOWER priority than Bar - a LATER file position, i.e.
            # move_category's before=False - Bar ends up first in file
            # order (screen-bottom, highest priority), Foo second.
            page._on_category_drop("Foo", "Bar", True)
            from commander_gui.modlist import grouped

            names = [name for name, _mods in grouped(page._lines)]
            self.assertEqual(names, ["Bar", "Foo"])

    def test_on_category_drop_reports_pinned_category_refusal(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(
                tmp,
                "+AnomalyTogether\n-G.A.M.M.A. End of List_separator\n"
                "+ModA\n-Foo_separator\n",
            )
            with patch(
                "commander_gui.ui.mod_manager_page.QMessageBox.warning"
            ) as mock_warn:
                page._on_category_drop("G.A.M.M.A. End of List", "Foo", True)
            mock_warn.assert_called_once()

    def test_on_drop_cancelled_shows_a_status_bar_message(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as xdg,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}),
        ):
            page = self._make_mod_manager_page(tmp, "+ModA\n-Foo_separator\n")
            page.window.statusBar = Mock()
            page._on_drop_cancelled()
            page.window.statusBar.return_value.showMessage.assert_called_once()

    def test_undo_last_update_restores_the_pre_apply_modlist_snapshot(self):
        """Regression test: applying a GAMMA update happens entirely inside

        the wrapped CLI subprocess, which never goes through this app's own
        Mod Manager backup machinery - so without its own snapshot, "Undo
        Last Update" would have nothing to restore. The snapshot must be
        taken right before the CLI update-apply command runs.
        """
        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.ui.update_page import UpdatePage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            profile_dir = gamma / "profiles" / "G.A.M.M.A"
            profile_dir.mkdir(parents=True)
            modlist = profile_dir / "modlist.txt"
            modlist.write_text("+OldMod\n")
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(gamma),
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

            page = UpdatePage(FakeWindow())
            self.assertIsNone(page._pre_update_snapshot_path())

            # Simulate what _apply() does right before shelling out.
            page._snapshot_modlist_before_update()
            self.assertIsNotNone(page._pre_update_snapshot_path())

            # The update "changed" the modlist; undo should bring back the
            # pre-apply content.
            modlist.write_text("+NewMod\n")
            with (
                patch("commander_gui.ui.update_page.mo2_running", return_value=False),
                patch("commander_gui.ui.update_page.QMessageBox.question")
                as mock_question,
                patch("commander_gui.ui.update_page.QMessageBox.information"),
            ):
                mock_question.return_value = QMessageBox.StandardButton.Yes
                page._undo_last_update()

            self.assertEqual(modlist.read_text(), "+OldMod\n")

    def test_profiles_page_import_button_prefills_the_new_profile_form(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.profile_bundle import export_profile_bundle
        from commander_gui.ui.profiles_page import ProfilesPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            source = CliProfile(
                profile_name="Source",
                anomaly=str(Path(tmp) / "a"),
                gamma=str(Path(tmp) / "g"),
                cache=str(Path(tmp) / "c"),
                download_threads=17,
            )
            bundle_path = Path(tmp) / "bundle.zip"
            export_profile_bundle(source, bundle_path)

            class FakeWindow:
                settings = CliSettings(profiles=[])
                install_busy = False

                def refresh_settings(self):
                    pass

            page = ProfilesPage(FakeWindow())
            with (
                patch(
                    "commander_gui.ui.profiles_page.QFileDialog.getOpenFileName",
                    return_value=(str(bundle_path), ""),
                ),
                patch("commander_gui.ui.profiles_page.QMessageBox.information"),
            ):
                page._import_profile()

            self.assertEqual(page.threads_spin.value(), 17)
            self.assertEqual(page._form_state, "")
            # Install paths must never come from the bundle.
            self.assertNotEqual(page.gamma_edit.text(), str(Path(tmp) / "g"))

    def test_export_import_profile_bundle_round_trips_portable_settings(self):
        from commander_gui.profile_bundle import (
            export_profile_bundle,
            read_profile_bundle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            profiles_dir = gamma / "profiles" / "G.A.M.M.A"
            profiles_dir.mkdir(parents=True)
            (profiles_dir / "modlist.txt").write_text("+ModA\n-Sep_separator\n+ModB\n")
            profile = CliProfile(
                profile_name="Mine",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(gamma),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
                download_threads=12,
                stalker_gamma_repo_url="https://example.com/fork",
            )
            bundle_path = Path(tmp) / "out.zip"
            export_profile_bundle(profile, bundle_path)
            self.assertTrue(bundle_path.is_file())

            bundle = read_profile_bundle(bundle_path)
            self.assertEqual(bundle.modlist_text, "+ModA\n-Sep_separator\n+ModB\n")

            imported = CliProfile()
            bundle.apply_to(imported)
            self.assertEqual(imported.download_threads, 12)
            self.assertEqual(imported.stalker_gamma_repo_url, "https://example.com/fork")
            self.assertEqual(imported.mo2_profile, "G.A.M.M.A")
            # Paths are never part of the bundle - untouched from defaults.
            self.assertEqual(imported.anomaly, CliProfile().anomaly)
            self.assertEqual(imported.gamma, CliProfile().gamma)

    def test_export_profile_bundle_without_a_modlist_still_succeeds(self):
        from commander_gui.profile_bundle import (
            export_profile_bundle,
            read_profile_bundle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            profile = CliProfile(
                profile_name="Fresh",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(Path(tmp) / "gamma"),
                cache=str(Path(tmp) / "cache"),
            )
            bundle_path = Path(tmp) / "out.zip"
            export_profile_bundle(profile, bundle_path)
            bundle = read_profile_bundle(bundle_path)
            self.assertIsNone(bundle.modlist_text)

    def test_read_profile_bundle_rejects_a_non_bundle_zip(self):
        import zipfile

        from commander_gui.profile_bundle import (
            ProfileBundleError,
            read_profile_bundle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-a-bundle.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("hello.txt", "hi")
            with self.assertRaises(ProfileBundleError):
                read_profile_bundle(path)

    def test_check_commander_update_finds_a_newer_release(self):
        from commander_gui.updates import check_commander_update

        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.geturl.return_value = (
            "https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases/tag/v1.3.0"
        )
        with patch(
            "commander_gui.updates.urllib.request.urlopen", return_value=response
        ):
            self.assertEqual(check_commander_update("1.2.9-unstable"), "v1.3.0")

    def test_check_commander_update_returns_none_when_already_current(self):
        from commander_gui.updates import check_commander_update

        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.geturl.return_value = (
            "https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER/releases/tag/v1.2.9"
        )
        with patch(
            "commander_gui.updates.urllib.request.urlopen", return_value=response
        ):
            self.assertIsNone(check_commander_update("1.2.9-unstable"))

    def test_check_commander_update_returns_none_when_unreachable(self):
        from commander_gui.updates import check_commander_update

        with (
            patch("commander_gui.network.time.sleep"),
            patch(
                "commander_gui.updates.urllib.request.urlopen",
                side_effect=OSError("no network"),
            ),
        ):
            self.assertIsNone(check_commander_update("1.2.9-unstable"))

    def test_network_urlopen_allows_http_and_https(self):
        with patch("commander_gui.network.urllib.request.urlopen") as urlopen:
            network.urlopen("https://example.com/x", timeout=1)
            network.urlopen(
                urllib.request.Request("http://example.com/x"), timeout=1
            )
        self.assertEqual(urlopen.call_count, 2)

    def test_remote_version_returns_none_for_oversized_response(self):
        profile = CliProfile()
        response = type("Response", (), {})()
        response.headers = {"Content-Length": str(1024 * 1024 + 1)}
        with patch("commander_gui.updates.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = response
            self.assertIsNone(remote_version(profile))

    def test_remote_version_returns_none_for_malformed_response(self):
        with patch(
            "commander_gui.updates.urllib.request.urlopen",
            side_effect=ValueError("malformed response"),
        ):
            self.assertIsNone(remote_version(CliProfile()))

    def test_desktop_exec_keeps_a_spaced_path_as_one_argument(self):
        """Regression test: a path with a space must stay one Exec= token.

        _desktop_exec() used to shlex.split() an already-assembled path
        string as if it were a shell command line, which mis-tokenized any
        path containing a space into multiple bogus arguments.
        """
        exec_line = autostart._desktop_exec(["/home/John Doe/App.AppImage"])
        self.assertEqual(exec_line, '"/home/John Doe/App.AppImage"')

    def test_latest_version_human_falls_back_to_readme_on_format_mismatch(self):
        """Regression test: the README fallback must actually run.

        Previously the loop broke on the first *non-empty* fetch regardless
        of whether it matched either version regex, so a Patchnotes.md that
        fetched successfully but didn't match the expected heading format
        silently returned None instead of falling back to README.md.
        """
        responses = {
            "Patchnotes.md": "# Some unrelated heading with no version",
            "README.md": "badge gamma-v0.9.5 badge",
        }

        class FakeResponse:
            def __init__(self, text: str):
                self._data = text.encode()
                self.headers = {"Content-Length": str(len(self._data))}

            def read(self, size: int = -1) -> bytes:
                if size < 0 or size >= len(self._data):
                    chunk, self._data = self._data, b""
                    return chunk
                chunk, self._data = self._data[:size], self._data[size:]
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        def fake_urlopen(request, timeout=None):
            url = request.full_url
            for filename, body in responses.items():
                if url.endswith(filename):
                    return FakeResponse(body)
            raise AssertionError(f"unexpected url requested: {url}")

        with patch(
            "commander_gui.updates.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            self.assertEqual(latest_version_human(CliProfile()), "0.9.5")

    def test_proton_archive_rejects_special_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "special.tar.gz"
            destination = Path(tmp) / "destination"
            destination.mkdir()
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("GE-Proton11-5/device")
                info.type = tarfile.CHRTYPE
                info.devmajor = 1
                info.devminor = 3
                tf.addfile(info)
            with tarfile.open(archive, "r:gz") as tf, self.assertRaisesRegex(
                ValueError, "special file"
            ):
                _safe_extract(tf, destination)

    def test_install_proton_cancel_after_replace_cleans_up(self):
        """A cancel signaled right after the rename must not orphan the build.

        Regression test for a bug where a premature check_cancelled() call
        right after os.replace() raised out of the function before the
        single intended cleanup path (the post-`with` check) could run,
        leaving the build fully installed while reporting cancellation -
        which then permanently broke reinstall with "already exists".
        """
        dir_name = "GE-Proton99-1"
        tar_name = f"{dir_name}.tar.gz"

        with tempfile.TemporaryDirectory() as tmp:
            install_dir = Path(tmp) / "compatibilitytools.d"
            install_dir.mkdir()

            tar_path = Path(tmp) / tar_name
            with tarfile.open(tar_path, "w:gz") as tf:
                dir_info = tarfile.TarInfo(f"{dir_name}/")
                dir_info.type = tarfile.DIRTYPE
                tf.addfile(dir_info)
                data = b"hello"
                file_info = tarfile.TarInfo(f"{dir_name}/file.txt")
                file_info.size = len(data)
                tf.addfile(file_info, io.BytesIO(data))

            tar_bytes = tar_path.read_bytes()
            checksum_hex = hashlib.sha512(tar_bytes).hexdigest()
            tar_url = f"https://example.invalid/{tar_name}"
            sum_url = f"https://example.invalid/{tar_name}.sha512sum"

            class FakeResponse:
                def __init__(self, data: bytes, headers: dict | None = None):
                    self._data = data
                    self.headers = headers or {}

                def read(self, size: int = -1) -> bytes:
                    if size < 0 or size >= len(self._data):
                        chunk, self._data = self._data, b""
                        return chunk
                    chunk, self._data = self._data[:size], self._data[size:]
                    return chunk

                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

            def fake_urlopen(request, timeout=None):
                url = request.full_url
                if url == tar_url:
                    return FakeResponse(
                        tar_bytes, {"Content-Length": str(len(tar_bytes))}
                    )
                if url == sum_url:
                    return FakeResponse(checksum_hex.encode())
                raise AssertionError(f"unexpected url requested: {url}")

            cancel_event = threading.Event()
            real_replace = os.replace

            def fake_replace(src, dst):
                # Simulate cancellation landing exactly after the rename
                # that commits the install succeeds.
                real_replace(src, dst)
                cancel_event.set()

            with (
                patch(
                    "commander_gui.proton_installer._find_assets",
                    return_value=(tar_url, sum_url),
                ),
                patch(
                    "commander_gui.proton_installer.urllib.request.urlopen",
                    side_effect=fake_urlopen,
                ),
                patch(
                    "commander_gui.proton_installer.os.replace",
                    side_effect=fake_replace,
                ),
                self.assertRaisesRegex(ValueError, "cancelled"),
            ):
                install_proton(dir_name, install_dir, cancel_event=cancel_event)

            # The single cleanup path must have removed the fully-installed
            # build rather than leaving it orphaned - otherwise a retry
            # would permanently fail with "Proton build already exists".
            self.assertFalse((install_dir / dir_name).exists())

    def test_proton_release_list_includes_legacy_version_nine(self):
        releases = [
            {"tag_name": "GE-Proton11-5", "published_at": "2026-01-01"},
            {"tag_name": "GE-Proton9-10", "published_at": "2024-01-01"},
            {"tag_name": "not-a-proton-release", "published_at": ""},
        ]
        with patch(
            "commander_gui.proton_installer._api_get", return_value=releases
        ) as api_get:
            result = fetch_ge_proton_releases(count=100)
        api_get.assert_called_once_with(
            "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases?per_page=100"
        )
        self.assertEqual(
            [item["tag"] for item in result], ["GE-Proton11-5", "GE-Proton9-10"]
        )

    def test_update_diff_detects_archive_change(self):
        local = {"Addon": ModPackRecord(1, "Addon", "", "link", "", "old.zip", "", "")}
        remote = {"Addon": ModPackRecord(1, "Addon", "", "link", "", "new.zip", "", "")}
        diffs = diff_records(local, remote)
        self.assertEqual([diff.status for diff in diffs], ["Modified"])
        # Archive-only change (no version label on either side): the cell
        # shows the filename change, not a hash.
        self.assertEqual(diffs[0].detail, "old.zip → new.zip")

    def test_update_diff_shows_version_bump_over_hash(self):
        # A version/patch label on the remote record is the most readable
        # signal a general user can get, so it wins even though the zip
        # name and hash also changed here.
        local = {
            "Addon": ModPackRecord(1, "Addon", "1.0", "link", "", "old.zip", "aaa", "")
        }
        remote = {
            "Addon": ModPackRecord(1, "Addon", "1.1", "link", "", "new.zip", "bbb", "")
        }
        diffs = diff_records(local, remote)
        self.assertEqual(diffs[0].detail, "1.0 → 1.1")
        self.assertIn("aaa", diffs[0].detail_tooltip)
        self.assertIn("bbb", diffs[0].detail_tooltip)

    def test_update_diff_falls_back_to_archive_updated_for_hash_only_change(self):
        # Same filename, same version label, but a different archive hash
        # (a repack) - still a real change, just with nothing readable to
        # show beyond a generic label; the hash moves to the tooltip.
        local = {
            "Addon": ModPackRecord(1, "Addon", "1.0", "link", "", "same.zip", "aaa", "")
        }
        remote = {
            "Addon": ModPackRecord(1, "Addon", "1.0", "link", "", "same.zip", "bbb", "")
        }
        diffs = diff_records(local, remote)
        self.assertEqual(diffs[0].detail, "Archive updated")
        self.assertIn("aaa", diffs[0].detail_tooltip)
        self.assertIn("bbb", diffs[0].detail_tooltip)

    def test_winetricks_includes_extra_media_verbs(self):
        self.assertIn("quartz", WINETRICKS_VERBS)
        self.assertIn("dx8vb", WINETRICKS_VERBS)
        self.assertEqual(WINETRICKS_VERBS.count("d3dx9"), 1)
        with patch(
            "commander_gui.winetricks.winetricks_binary", return_value="winetricks"
        ):
            command = winetricks_install_command()
        self.assertEqual(command[2:], list(WINETRICKS_VERBS))

    def test_winetricks_progress_parses_percent_and_verb_stages(self):
        completed = set()
        self.assertEqual(
            _winetricks_progress("Downloading 42%", "verbs", completed), 42
        )
        self.assertEqual(
            _winetricks_progress("Executing quartz", "verbs", completed),
            round((WINETRICKS_VERBS.index("quartz") + 1) / len(WINETRICKS_VERBS) * 100),
        )

    def test_dependencies_progress_maps_stages_onto_overall_bar(self):
        """Audit follow-up: determinate staged bar for Install Dependencies."""
        self.assertIsNone(_dependencies_progress("umu", None))
        # Stage boundaries: umu 0-15, tools 15-35, verbs 35-100.
        self.assertEqual(_dependencies_progress("umu", 0), 0)
        self.assertEqual(_dependencies_progress("umu", 100), 15)
        self.assertEqual(_dependencies_progress("tools", 0), 15)
        self.assertEqual(_dependencies_progress("tools", 100), 35)
        self.assertEqual(_dependencies_progress("verbs", 0), 35)
        self.assertEqual(_dependencies_progress("verbs", 50), 68)
        self.assertEqual(_dependencies_progress("verbs", 100), 100)
        # Out-of-range input clamps instead of leaving the stage range.
        self.assertEqual(_dependencies_progress("verbs", -5), 35)
        self.assertEqual(_dependencies_progress("verbs", 150), 100)
        # Unknown stages fall back to the full range.
        self.assertEqual(_dependencies_progress("future", 25), 25)

    def test_system_check_lists_every_winetricks_dependency(self):
        # The page shows one collapsed "Runtime libraries" row (not one row
        # per verb codename) with the per-verb breakdown in its tooltip.
        # Wine/Protontricks/umu-run are folded into the same row's count
        # (matching the Dashboard's "X/Y dependencies installed" scope),
        # even though each also has its own row further up the page.
        status = {verb: verb in {"quartz", "dx8vb"} for verb in WINETRICKS_VERBS}
        extra_tools = [
            ("wine", "Wine", True, "sudo pacman -S wine"),
            (
                "protontricks",
                "Protontricks",
                False,
                "sudo pacman -S pipx && pipx install protontricks",
            ),
            ("umu", "umu-run", False, "curl -fL ... -o umu-run"),
        ]
        checks = _winetricks_checks(status, "/usr/bin/winetricks", extra_tools)
        self.assertEqual(len(checks), 1)
        row = checks[0]
        self.assertEqual(row["label"], "Runtime libraries")
        self.assertEqual(row["state"], "missing")
        # 2 verbs + Wine installed, out of 8 verbs + 3 tools = 11 total.
        self.assertIn("3/11 runtime libraries installed", row["detail"])
        # Missing verbs are named by their human label, not the raw codename.
        self.assertIn("Visual C++ Runtime 2022", row["detail"])
        self.assertIn("Protontricks", row["detail"])
        self.assertNotIn("quartz", row["detail"])
        # A copy-command button is always offered, not just when something
        # is missing (re-running winetricks verbs is a harmless no-op) - and
        # now includes the missing tools' install commands too, but not an
        # already-installed tool's (Wine's), since that could need sudo for
        # no reason.
        self.assertIn("winetricks -q", row["command"])
        self.assertIn("pipx install protontricks", row["command"])
        self.assertIn("curl -fL", row["command"])
        self.assertNotIn("sudo pacman -S wine", row["command"])
        # The raw verb codenames (and full per-verb state) are still
        # available via the tooltip.
        self.assertIn("quartz", row["tooltip"])
        self.assertIn("dx8vb", row["tooltip"])

    def test_system_check_reports_installation_state(self):
        profile = CliProfile(
            active=True,
            profile_name="test",
            anomaly="/missing/anomaly",
            gamma="/missing/gamma",
        )
        with patch(
            "commander_gui.ui.system_check_page.load_settings"
        ) as load_settings_mock:
            load_settings_mock.return_value.active_profile = profile
            checks = _installation_checks()
        states = {check["label"]: check["state"] for check in checks}
        self.assertEqual(states["Active profile"], "ready")
        self.assertEqual(states["Anomaly installation"], "missing")
        self.assertEqual(states["GAMMA modpack"], "missing")

    def test_system_check_reports_missing_active_profile(self):
        with patch(
            "commander_gui.ui.system_check_page.load_settings"
        ) as load_settings_mock:
            load_settings_mock.return_value.active_profile = None
            checks = _installation_checks()
        self.assertTrue(all(check["state"] == "missing" for check in checks))

    def test_gamma_installed_uses_the_profiles_own_mo2_profile_name(self):
        # A profile whose MO2 profile folder isn't literally "G.A.M.M.A"
        # must not be reported as not installed just because of that name.
        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp)
            (gamma / "ModOrganizer.exe").write_text("")
            (gamma / "ModOrganizer.ini").write_text("")
            (gamma / "profiles" / "MyCustomProfile").mkdir(parents=True)
            self.assertTrue(common.gamma_installed(str(gamma), "MyCustomProfile"))
            # The default fallback must still work when no name is given.
            self.assertFalse(common.gamma_installed(str(gamma)))
            (gamma / "profiles" / "G.A.M.M.A").mkdir()
            self.assertTrue(common.gamma_installed(str(gamma)))

    def test_normalize_path_expands_tilde_and_resolves_relative(self):
        home = str(Path.home())
        self.assertEqual(normalize_path("~"), home)
        self.assertTrue(normalize_path("~/Games/Anomaly").startswith(home))
        self.assertNotIn("~", normalize_path("~/Games/Anomaly"))
        # A relative path must resolve to an absolute one, not stay relative.
        self.assertTrue(Path(normalize_path("relative/anomaly")).is_absolute())
        # Blank input stays blank rather than resolving to the cwd.
        self.assertEqual(normalize_path("   "), "")

    def test_required_tools_keep_copyable_install_commands_when_ready(self):
        with (
            patch(
                "commander_gui.ui.system_check_page.configured_tool",
                return_value="",
            ),
            patch(
                "commander_gui.ui.system_check_page.shutil.which",
                return_value="/usr/bin/steam",
            ),
            patch(
                "commander_gui.ui.system_check_page.install_command",
                return_value="sudo apt install steam",
            ),
        ):
            check = _check_tool("Steam", "steam", "apt")
        self.assertEqual(check["state"], "ready")
        self.assertEqual(check["command"], "sudo apt install steam")

    def test_unwanted_dx8_launch_targets_are_hidden(self):
        self.assertTrue(_is_hidden_launch_target("DX8"))
        self.assertTrue(_is_hidden_launch_target("dx8-avx"))
        self.assertFalse(_is_hidden_launch_target("Anomaly"))

    def test_start_page_validation_matches_nav_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gui-settings.json"
            path.write_text(json.dumps({"start_page": "systemcheck"}), encoding="utf-8")
            with patch.object(gui_settings, "gui_settings_path", return_value=path):
                state = gui_settings.load_gui_settings()
            self.assertEqual(state["start_page"], "systemcheck")
            path.write_text(json.dumps({"start_page": "modmanager"}), encoding="utf-8")
            with patch.object(gui_settings, "gui_settings_path", return_value=path):
                state = gui_settings.load_gui_settings()
            self.assertEqual(state["start_page"], "modmanager")
            path.write_text(json.dumps({"start_page": "update"}), encoding="utf-8")
            with patch.object(gui_settings, "gui_settings_path", return_value=path):
                state = gui_settings.load_gui_settings()
            self.assertEqual(state["start_page"], "update")

    def test_malformed_modpack_json_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "modpack_maker_list.json"
            path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
            result = local_modpack_records(tmp, "profile")
            self.assertIsNone(result)

    def test_corrupt_md5_baseline_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "game"
            game_dir.mkdir()
            (game_dir / "mods").mkdir()
            manifest = game_dir / "gamma-md5.txt"
            manifest.write_text("corrupt garbage", encoding="utf-8")
            result = scan_mods_md5(str(game_dir))
            self.assertTrue(any("corrupt" in e.lower() for e in result.errors))

    def test_empty_md5_baseline_is_valid_for_empty_mods(self):
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "game"
            (game_dir / "mods").mkdir(parents=True)
            (game_dir / "gamma-md5.txt").write_text("", encoding="utf-8")
            result = scan_mods_md5(str(game_dir))
            self.assertEqual(result.errors, [])
            self.assertEqual(result.problems, 0)

    def test_cache_archive_verification_classifies_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "good.zip").write_bytes(b"good")
            (cache / "bad.zip").write_bytes(b"bad")
            good_digest = hashlib.md5(b"good").hexdigest()
            result = verify_cache_archives(
                str(cache),
                {
                    "good.zip": good_digest,
                    "bad.zip": "0" * 32,
                    "missing.zip": good_digest,
                },
            )
            self.assertEqual(result.verified, ["good.zip"])
            self.assertEqual(result.mismatched, ["bad.zip"])
            self.assertEqual(result.missing, ["missing.zip"])
            self.assertEqual(result.problems, 2)

    def test_cache_archive_verification_honors_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancel = threading.Event()
            cancel.set()
            result = verify_cache_archives(str(Path(tmp)), {"one.zip": "0" * 32}, cancel=cancel)
            self.assertTrue(result.cancelled)
            self.assertEqual(result.problems, 0)

    def _make_gamma_install(self, base: Path, mo2_profile: str = "G.A.M.M.A"):
        """Build a minimal on-disk GAMMA install for verify_gamma()/scan tests."""
        gamma = base / "gamma"
        (gamma / "ModOrganizer.exe").parent.mkdir(parents=True, exist_ok=True)
        (gamma / "ModOrganizer.exe").touch()
        (gamma / "ModOrganizer.ini").touch()
        profile_dir = gamma / "profiles" / mo2_profile
        profile_dir.mkdir(parents=True)
        (gamma / "mods").mkdir()
        return gamma, profile_dir / "modlist.txt"

    def test_is_expected_gamma_overlay_corrupt_matches_the_9_known_files(self):
        """Regression test for a real reported false positive: GAMMA

        deliberately overwrites these exact 9 files with its own patched
        versions, so `anomaly check`'s vanilla-only baseline always
        flags them CORRUPT - that's expected, not a real problem.
        """
        from commander_gui.integrity import is_expected_gamma_overlay_corrupt

        anomaly_path = "/home/quiet/Games/GAMMA/anomaly"
        known_paths = [
            "fsgame.ltx",
            "bin/AnomalyDX8.exe",
            "bin/AnomalyDX8AVX.exe",
            "bin/AnomalyDX9.exe",
            "bin/AnomalyDX9AVX.exe",
            "bin/AnomalyDX10.exe",
            "bin/AnomalyDX10AVX.exe",
            "bin/AnomalyDX11.exe",
            "bin/AnomalyDX11AVX.exe",
        ]
        for rel in known_paths:
            line = f"{anomaly_path}/{rel}                    | CORRUPT"
            self.assertTrue(is_expected_gamma_overlay_corrupt(line, anomaly_path), msg=rel)
            # Backslash-style path (as the CLI's own baseline manifest
            # uses) and mixed case must match too.
            backslash_line = f"{anomaly_path}\\{rel.replace('/', chr(92))}   | CORRUPT"
            self.assertTrue(
                is_expected_gamma_overlay_corrupt(backslash_line, anomaly_path), msg=rel
            )

    def test_is_expected_gamma_overlay_corrupt_false_for_unrelated_or_wrong_status(self):
        from commander_gui.integrity import is_expected_gamma_overlay_corrupt

        anomaly_path = "/home/quiet/Games/GAMMA/anomaly"
        # A genuinely unrelated CORRUPT file must not be excused.
        self.assertFalse(
            is_expected_gamma_overlay_corrupt(
                f"{anomaly_path}/gamedata/some_real_file.xml | CORRUPT", anomaly_path
            )
        )
        # OK/NOT FOUND on a known-overlay path is never "expected" -
        # NOT FOUND means GAMMA's own overwrite never happened at all,
        # a real problem worth flagging.
        self.assertFalse(
            is_expected_gamma_overlay_corrupt(
                f"{anomaly_path}/fsgame.ltx | OK", anomaly_path
            )
        )
        self.assertFalse(
            is_expected_gamma_overlay_corrupt(
                f"{anomaly_path}/bin/AnomalyDX11AVX.exe | NOT FOUND", anomaly_path
            )
        )
        # No anomaly_path (e.g. no active profile) must never crash or
        # accidentally match.
        self.assertFalse(
            is_expected_gamma_overlay_corrupt(f"{anomaly_path}/fsgame.ltx | CORRUPT", "")
        )

    def test_verify_gamma_classifies_missing_empty_and_ok_mods(self):
        from commander_gui.integrity import verify_gamma

        with tempfile.TemporaryDirectory() as tmp:
            gamma, modlist = self._make_gamma_install(Path(tmp))
            (gamma / "mods" / "OkMod").mkdir()
            (gamma / "mods" / "OkMod" / "file.txt").write_text("x")
            (gamma / "mods" / "EmptyMod").mkdir()
            modlist.write_text("+OkMod\n+EmptyMod\n+MissingMod\n-Disabled Mod\n")

            result = verify_gamma(str(gamma), "G.A.M.M.A")

            self.assertEqual(result.ok_mods, 1)
            self.assertEqual(result.missing, ["MissingMod"])
            self.assertEqual(result.empty, ["EmptyMod"])
            self.assertEqual(result.disabled_mods, 1)
            self.assertEqual(result.problems, 2)

    def test_verify_gamma_rejects_a_symlinked_mod_folder(self):
        """Regression test: a symlinked mod folder must be reported missing,

        never followed - it's the same path-escape guard used before any
        deletion in repair.py, applied here too.
        """
        from commander_gui.integrity import verify_gamma

        with tempfile.TemporaryDirectory() as tmp:
            gamma, modlist = self._make_gamma_install(Path(tmp))
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (gamma / "mods" / "SneakyMod").symlink_to(outside, target_is_directory=True)
            modlist.write_text("+SneakyMod\n")

            result = verify_gamma(str(gamma), "G.A.M.M.A")

            self.assertEqual(result.missing, ["SneakyMod"])
            self.assertEqual(result.ok_mods, 0)

    def test_verify_gamma_buckets_official_vs_extra_mods(self):
        from commander_gui.integrity import verify_gamma

        with tempfile.TemporaryDirectory() as tmp:
            gamma, modlist = self._make_gamma_install(Path(tmp))
            for name in ("Official", "Extra"):
                (gamma / "mods" / name).mkdir()
                (gamma / "mods" / name / "f").write_text("x")
            modlist.write_text("+Official\n+Extra\n")

            result = verify_gamma(str(gamma), "G.A.M.M.A", official_mods={"Official"})

            self.assertEqual(result.official, ["Official"])
            self.assertEqual(result.extra, ["Extra"])

    def test_scan_mods_md5_detects_changed_added_and_removed_files(self):
        from commander_gui.integrity import scan_mods_md5

        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            mod_dir = gamma / "mods" / "SomeMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "stable.txt").write_text("stable")
            (mod_dir / "will_change.txt").write_text("original")
            (mod_dir / "will_be_removed.txt").write_text("gone soon")

            first = scan_mods_md5(str(gamma))
            self.assertTrue(first.created)
            self.assertEqual(first.problems, 0)

            (mod_dir / "will_change.txt").write_text("mutated")
            (mod_dir / "will_be_removed.txt").unlink()
            (mod_dir / "new_file.txt").write_text("brand new")

            second = scan_mods_md5(str(gamma))

            self.assertFalse(second.created)
            self.assertEqual(second.changed, ["mods/SomeMod/will_change.txt"])
            self.assertEqual(second.added, ["mods/SomeMod/new_file.txt"])
            self.assertEqual(second.removed, ["mods/SomeMod/will_be_removed.txt"])

    def test_scan_mods_md5_ignores_leftover_quarantine_folder(self):
        """Regression test: a `.verify-quarantine` folder left behind by a

        failed purge/restore (see repair.py's quarantine mechanism) sits
        directly under gamma/mods - it must never be hashed into the MD5
        baseline, or a later cleanup of that stray folder would look like
        a wave of "removed" files on the next Verify Integrity run.
        """
        from commander_gui.integrity import scan_mods_md5

        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            mod_dir = gamma / "mods" / "SomeMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "stable.txt").write_text("stable")
            quarantine_dir = gamma / "mods" / ".verify-quarantine" / "Leftover.123.abcd1234"
            quarantine_dir.mkdir(parents=True)
            (quarantine_dir / "orphaned.txt").write_text("should never be hashed")

            result = scan_mods_md5(str(gamma))

            self.assertTrue(result.created)
            self.assertEqual(result.files_scanned, 1)
            manifest_text = Path(result.manifest_path).read_text(encoding="utf-8")
            self.assertNotIn("verify-quarantine", manifest_text)
            self.assertNotIn("orphaned.txt", manifest_text)

    def test_scan_mods_md5_rejects_filenames_with_embedded_newlines(self):
        """Regression test: the baseline manifest is a plain-text

        "<md5>  <relpath>\\n" file, one entry per line. A relative path
        carrying an embedded newline (legal on Linux, and reachable via a
        third-party mod archive - mod_install.py only rejects symlinks and
        path traversal in archive entries, not control characters) would
        split into extra lines on write, corrupting the baseline and
        risking a desynced digest/path pair that repair.py's
        classify_problems() could then use to quarantine the wrong mod
        folder. Such files must be skipped and reported as errors instead
        of ever being written into the manifest.
        """
        from commander_gui.integrity import scan_mods_md5

        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            mod_dir = gamma / "mods" / "SomeMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "stable.txt").write_text("stable")
            (mod_dir / "weird\nname.txt").write_text("dangerous")

            result = scan_mods_md5(str(gamma))

            self.assertTrue(result.created)
            self.assertEqual(result.files_scanned, 1)
            self.assertEqual(len(result.errors), 1)
            self.assertIn("weird\nname.txt", result.errors[0])
            manifest_text = Path(result.manifest_path).read_text(encoding="utf-8")
            self.assertNotIn("weird", manifest_text)
            # A second scan must still be able to read back its own
            # baseline as valid (not "empty or corrupt") and keep
            # reporting the same file as unreadable rather than as a
            # spurious change.
            second = scan_mods_md5(str(gamma))
            self.assertFalse(second.created)
            self.assertEqual(second.changed, [])
            self.assertEqual(second.added, [])
            self.assertEqual(second.removed, [])
            self.assertEqual(len(second.errors), 1)

    def test_invalidate_baseline_removes_the_manifest(self):
        from commander_gui.integrity import MANIFEST_FILENAME, invalidate_baseline

        with tempfile.TemporaryDirectory() as tmp:
            gamma = Path(tmp) / "gamma"
            gamma.mkdir()
            manifest = gamma / MANIFEST_FILENAME
            manifest.write_text("stale baseline")

            invalidate_baseline(str(gamma))
            self.assertFalse(manifest.exists())

            invalidate_baseline(str(gamma))  # no-op, must not raise

    def test_classify_problems_matches_the_counter_shift_fallback(self):
        from commander_gui.integrity import Md5ScanResult
        from commander_gui.repair import ModPackRecord, classify_problems

        records = {
            "2- Some Mod": ModPackRecord(2, "Some Mod", "", "", "", "", "abc", "")
        }
        scan = Md5ScanResult(changed=["mods/1- Some Mod/file.txt"])

        plan = classify_problems(scan, records)

        self.assertEqual(plan.repairable, ["1- Some Mod"])
        self.assertEqual(plan.matched_records["1- Some Mod"].counter, 2)

    def test_classify_problems_treats_ambiguous_name_match_as_unrepairable(self):
        """Regression test: two records that stripped-match the same on-disk

        folder name must never be silently repaired against whichever one
        happens to be found first - that could delete/redownload a mod
        against the wrong archive and checksum.
        """
        from commander_gui.integrity import Md5ScanResult
        from commander_gui.repair import ModPackRecord, classify_problems

        records = {
            "2- Some Mod": ModPackRecord(2, "Some Mod", "", "", "", "", "aaa", ""),
            "5- Some Mod": ModPackRecord(5, "Some Mod", "", "", "", "", "bbb", ""),
        }
        scan = Md5ScanResult(changed=["mods/1- Some Mod/file.txt"])

        plan = classify_problems(scan, records)

        self.assertEqual(plan.repairable, [])
        self.assertEqual(plan.unrepairable, ["1- Some Mod"])

    def test_classify_problems_folds_in_presence_missing_mods(self):
        """Regression test: a mod verify_gamma reports missing/empty must be

        offered for repair too, not just content-level changed/removed
        files - otherwise a mod that was never in the MD5 baseline to
        begin with is reported forever but never actually repaired.
        """
        from commander_gui.integrity import Md5ScanResult
        from commander_gui.repair import ModPackRecord, classify_problems

        records = {
            "1- Missing Mod": ModPackRecord(1, "Missing Mod", "", "", "", "", "abc", "")
        }
        scan = Md5ScanResult()  # no changed/removed - the baseline never saw it

        plan = classify_problems(
            scan, records, extra_broken_folders=["1- Missing Mod"]
        )

        self.assertEqual(plan.repairable, ["1- Missing Mod"])

    def test_update_apply_success_invalidates_the_verify_integrity_baseline(self):
        """Regression test: an applied GAMMA update legitimately changes

        files under gamma/mods - Verify Integrity's MD5 baseline must be
        invalidated afterward, or the next run reports every updated file
        as "corrupted".
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.update_page import UpdatePage

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="Test", gamma="/games/gamma")

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

            def set_install_busy(self, *a, **kw):
                pass

        page = UpdatePage(FakeWindow())
        page._apply_runner = None
        with patch("commander_gui.ui.update_page.invalidate_baseline") as mock_invalidate:
            page._on_apply_finished(0, "Applied cleanly.")
        mock_invalidate.assert_called_once_with("/games/gamma")

    def test_update_apply_failure_does_not_invalidate_the_baseline(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.update_page import UpdatePage

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="Test", gamma="/games/gamma")

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

            def set_install_busy(self, *a, **kw):
                pass

        page = UpdatePage(FakeWindow())
        page._apply_runner = None
        with patch("commander_gui.ui.update_page.invalidate_baseline") as mock_invalidate:
            page._on_apply_finished(1, "Error: something went wrong")
        mock_invalidate.assert_not_called()

    def test_applied_update_clears_the_stale_change_counts(self):
        """Regression test: a clean "Apply updates" emptied the diff list and

        hid the table, but left the filter box and the pre-update change
        counts ("1 modified") on screen, directly contradicting the "No addon
        changes. GAMMA is up to date." label shown beside them. _render() is
        the one place that keeps all four widgets consistent.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.parsers import UpdateDiff
        from commander_gui.ui.update_page import UpdatePage
        from commander_gui.updates import UpdateStatus

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="Test", gamma="/games/gamma")

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

            def set_install_busy(self, *a, **kw):
                pass

        page = UpdatePage(FakeWindow())
        page._apply_runner = None
        page._render(
            UpdateStatus(diffs=[UpdateDiff(status="Modified", text="SomeAddon")])
        )
        self.assertFalse(page.filter_combo.isHidden())
        self.assertFalse(page.count_summary.isHidden())

        with patch("commander_gui.ui.update_page.invalidate_baseline"):
            page._on_apply_finished(0, "Applied cleanly.")

        self.assertEqual(page._diffs, [])
        self.assertTrue(page.table.isHidden())
        self.assertTrue(page.filter_combo.isHidden())
        self.assertTrue(page.count_summary.isHidden())

    def test_stale_update_check_result_starts_a_replacement_check(self):
        """Regression test: revisiting the Updates page while a check was

        still in flight bumped _check_generation, and refresh()'s own
        _check() refused to start a replacement (one was already running).
        The in-flight result was then discarded on arrival as stale and
        nothing took its place, leaving the page stuck on "Checking the
        active GAMMA installation for updates..." with no version data until
        the user pressed "Check for updates" themselves.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.update_page import UpdatePage
        from commander_gui.updates import UpdateStatus

        QApplication.instance() or QApplication([])
        profile = CliProfile(
            active=True,
            profile_name="Test",
            anomaly="/games/anomaly",
            gamma="/games/gamma",
            cache="/games/cache",
        )

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

            def set_install_busy(self, *a, **kw):
                pass

        # The check itself is a network call - only the dispatch is under
        # test here, so the task never actually runs or completes on its own.
        with patch("commander_gui.ui.update_page.BackgroundTask") as mock_task_cls:
            page = UpdatePage(FakeWindow())
            page.refresh()  # first visit starts a check
            in_flight = page._check_task
            page.refresh()  # revisited before that check finished
            self.assertEqual(mock_task_cls.call_count, 1)
            self.assertTrue(page._checking)

            page._on_check_done(
                UpdateStatus(),
                in_flight,
                1,  # the generation the discarded check was started with
                ("Test", "/games/anomaly", "/games/gamma", "/games/cache"),
            )
            self.assertEqual(mock_task_cls.call_count, 2)
            self.assertTrue(page._checking)

    def test_fetch_latest_patchnotes_returns_the_full_body(self):
        from commander_gui.updates import fetch_latest_patchnotes

        body = "# **GAMMA 0.9.5**\n\n- Fixed a bug\n- Added a mod"

        class FakeResponse:
            def __init__(self, text: str):
                self._data = text.encode()
                self.headers = {"Content-Length": str(len(self._data))}

            def read(self, size: int = -1) -> bytes:
                if size < 0 or size >= len(self._data):
                    chunk, self._data = self._data, b""
                    return chunk
                chunk, self._data = self._data[:size], self._data[size:]
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        def fake_urlopen(request, timeout=None):
            assert request.full_url.endswith("Patchnotes.md")
            return FakeResponse(body)

        with patch(
            "commander_gui.updates.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            self.assertEqual(fetch_latest_patchnotes(CliProfile()), body)

    def test_fetch_latest_patchnotes_returns_none_when_unreachable(self):
        from commander_gui.updates import fetch_latest_patchnotes

        def fake_urlopen(request, timeout=None):
            raise OSError("network unreachable")

        with patch(
            "commander_gui.updates.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            self.assertIsNone(fetch_latest_patchnotes(CliProfile()))

    def test_changelog_web_url_builds_a_github_blob_link(self):
        from commander_gui.updates import changelog_web_url

        class FakeProfile:
            stalker_gamma_repo_url = "https://github.com/Grokitach/Stalker_GAMMA"
            stalker_gamma_repo_branch = "main"

        self.assertEqual(
            changelog_web_url(FakeProfile()),
            "https://github.com/Grokitach/Stalker_GAMMA/blob/main/Patchnotes.md",
        )

    def test_check_updates_populates_patchnotes_on_the_status(self):
        """Regression test: fetch_latest_patchnotes()'s fetch must be shared

        with the human-version lookup, not repeated - and the full text
        must actually land on UpdateStatus.patchnotes for the "What's
        New" panel to have something to show.
        """
        from commander_gui.updates import check_updates

        with tempfile.TemporaryDirectory() as tmp:
            gamma_dir = Path(tmp) / "gamma"
            profile_dir = gamma_dir / "profiles" / "G.A.M.M.A"
            profile_dir.mkdir(parents=True)
            (gamma_dir / "version.txt").write_text("910")
            (profile_dir / "modpack_maker_list.txt").write_text(
                "link\t\t\tSome Addon\t\tzip1\thash1\n"
            )
            profile = CliProfile(
                active=True,
                profile_name="Test",
                gamma=str(gamma_dir),
                mo2_profile="G.A.M.M.A",
                mod_pack_maker_url="https://example.invalid/list.txt",
            )

            patchnotes_body = "# **GAMMA 0.9.5**\n\nSome notes here."

            class FakeResponse:
                def __init__(self, text: str):
                    self._data = text.encode()
                    self.headers = {"Content-Length": str(len(self._data))}

                def read(self, size: int = -1) -> bytes:
                    if size < 0 or size >= len(self._data):
                        chunk, self._data = self._data, b""
                        return chunk
                    chunk, self._data = self._data[:size], self._data[size:]
                    return chunk

                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

            fetch_count = {"patchnotes": 0}

            def fake_urlopen(request, timeout=None):
                url = request.full_url
                if url.endswith("Patchnotes.md"):
                    fetch_count["patchnotes"] += 1
                    return FakeResponse(patchnotes_body)
                if url.endswith("list.txt"):
                    return FakeResponse("link\t\t\tSome Addon\t\tzip1\thash1\n")
                if url.endswith("G.A.M.M.A_definition_version.txt"):
                    return FakeResponse("920")
                raise AssertionError(f"unexpected url requested: {url}")

            with patch(
                "commander_gui.updates.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ):
                status = check_updates(profile)

            self.assertEqual(status.patchnotes, patchnotes_body)
            self.assertEqual(status.latest_human, "0.9.5")
            # Only one Patchnotes.md fetch for both the human version and
            # the full text - not fetched twice.
            self.assertEqual(fetch_count["patchnotes"], 1)

    def test_parse_patchnotes_sections_splits_the_full_release_history(self):
        """Regression test: Patchnotes.md is not just the latest release's

        notes - it's the whole history, one level-1 heading per release
        (confirmed against the real file: 0.9.5, 0.9.4, 0.9.3.1, 0.9.3,
        three separate 0.9.1 entries). The heading wording itself has
        drifted release to release, so this only anchors on "# ".
        """
        from commander_gui.updates import parse_patchnotes_sections

        text = (
            "# **GAMMA 0.9.5**\n\nLatest notes here.\n\n"
            "## Sub-heading\nMore latest notes.\n\n"
            "# **S.T.A.L.K.E.R. G.A.M.M.A. 0.9.4 Patch Notes**  \n\n"
            "Older notes here.\n"
        )
        sections = parse_patchnotes_sections(text)
        self.assertEqual(len(sections), 2)
        title0, body0 = sections[0]
        title1, body1 = sections[1]
        self.assertEqual(title0, "GAMMA 0.9.5")
        self.assertIn("Latest notes here.", body0)
        self.assertIn("More latest notes.", body0)
        self.assertNotIn("Older notes here.", body0)
        self.assertEqual(title1, "S.T.A.L.K.E.R. G.A.M.M.A. 0.9.4 Patch Notes")
        self.assertIn("Older notes here.", body1)

    def test_parse_patchnotes_sections_handles_no_heading_at_all(self):
        from commander_gui.updates import parse_patchnotes_sections

        self.assertEqual(parse_patchnotes_sections("just some text, no heading"), [])

    def test_whats_new_shows_one_collapsible_section_per_release(self):
        """Regression test: the "What's New" box used to show only the

        latest release's own text (from latest_version_human()'s regex
        match) even though the fetched Patchnotes.md held the entire
        release history - this confirms each release now gets its own
        collapsible entry, latest expanded, the rest collapsed.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.update_page import UpdatePage
        from commander_gui.updates import UpdateStatus

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="Test", gamma="/games/gamma")

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

        page = UpdatePage(FakeWindow())
        patchnotes = (
            "# **GAMMA 0.9.5**\n\nNewest changes.\n\n"
            "# **GAMMA 0.9.4**\n\nOlder changes.\n"
        )
        page._render(UpdateStatus(patchnotes=patchnotes))

        self.assertEqual(page.whats_new_sections_layout.count(), 2)
        first = page.whats_new_sections_layout.itemAt(0).widget()
        second = page.whats_new_sections_layout.itemAt(1).widget()
        self.assertEqual(first.toggle_button.text(), "▾  GAMMA 0.9.5")
        self.assertTrue(first.toggle_button.isChecked())
        self.assertFalse(first.body.isHidden())
        self.assertEqual(second.toggle_button.text(), "▸  GAMMA 0.9.4")
        self.assertFalse(second.toggle_button.isChecked())
        self.assertTrue(second.body.isHidden())

        # Clicking the collapsed one expands it in place.
        second.toggle_button.click()
        self.assertEqual(second.toggle_button.text(), "▾  GAMMA 0.9.4")
        self.assertFalse(second.body.isHidden())
        self.assertIn("Older changes.", second.body.toPlainText())

    def test_render_shows_the_whats_new_panel_when_patchnotes_are_present(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.update_page import UpdatePage
        from commander_gui.updates import UpdateStatus

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="Test", gamma="/games/gamma")

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

        page = UpdatePage(FakeWindow())
        # No check has happened yet - nothing to show.
        self.assertFalse(page.no_patchnotes_label.isHidden())
        self.assertTrue(page.whats_new_sections.isHidden())

        page._render(UpdateStatus(patchnotes="# **GAMMA 0.9.5**\n\nSome notes."))
        self.assertTrue(page.no_patchnotes_label.isHidden())
        self.assertFalse(page.whats_new_sections.isHidden())
        self.assertEqual(page.whats_new_sections_layout.count(), 1)
        section = page.whats_new_sections_layout.itemAt(0).widget()
        self.assertEqual(section.toggle_button.text(), "▾  GAMMA 0.9.5")
        self.assertIn("Some notes", section.body.toPlainText())

        page._render(UpdateStatus())
        self.assertFalse(page.no_patchnotes_label.isHidden())
        self.assertTrue(page.whats_new_sections.isHidden())
        self.assertEqual(page.whats_new_sections_layout.count(), 0)

    def test_last_checked_label_updates_after_a_successful_check(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.update_page import UpdatePage
        from commander_gui.updates import UpdateStatus

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="Test", gamma="/games/gamma")

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

        page = UpdatePage(FakeWindow())
        self.assertEqual(page.last_checked_label.text(), "")

        fake_task = object()
        page._check_task = fake_task
        page._on_check_done(
            UpdateStatus(),
            fake_task,
            page._check_generation,
            (profile.profile_name, profile.anomaly, profile.gamma, profile.cache),
        )
        self.assertIn("just now", page.last_checked_label.text())

    def test_apply_warns_on_low_disk_space_but_can_proceed(self):
        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.parsers import UpdateDiff
        from commander_gui.ui.update_page import UpdatePage
        from commander_gui.updates import UpdateStatus

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="Test", gamma="/games/gamma")

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

            def set_install_busy(self, *a, **kw):
                pass

        page = UpdatePage(FakeWindow())
        page._render(UpdateStatus(diffs=[UpdateDiff(status="Modified", text="X")]))

        with (
            patch.object(QMessageBox, "question")
            as mock_question,
            patch(
                "commander_gui.ui.update_page.free_space_bytes", return_value=1024
            ),
            patch("commander_gui.ui.update_page.CommandRunner") as mock_runner_cls,
        ):
            mock_question.side_effect = [
                QMessageBox.StandardButton.Yes,  # Confirm Update
                QMessageBox.StandardButton.Yes,  # Low Disk Space -> continue
            ]
            page._apply()

        self.assertEqual(mock_question.call_count, 2)
        mock_runner_cls.assert_called_once()

    def test_apply_cancelled_at_low_disk_space_prompt_does_not_start(self):
        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.parsers import UpdateDiff
        from commander_gui.ui.update_page import UpdatePage
        from commander_gui.updates import UpdateStatus

        QApplication.instance() or QApplication([])
        profile = CliProfile(active=True, profile_name="Test", gamma="/games/gamma")

        class FakeWindow:
            settings = CliSettings(profiles=[profile])
            install_busy = False

            def refresh_settings(self):
                pass

            def set_install_busy(self, *a, **kw):
                pass

        page = UpdatePage(FakeWindow())
        page._render(UpdateStatus(diffs=[UpdateDiff(status="Modified", text="X")]))

        with (
            patch.object(QMessageBox, "question") as mock_question,
            patch(
                "commander_gui.ui.update_page.free_space_bytes", return_value=1024
            ),
            patch("commander_gui.ui.update_page.CommandRunner") as mock_runner_cls,
        ):
            mock_question.side_effect = [
                QMessageBox.StandardButton.Yes,  # Confirm Update
                QMessageBox.StandardButton.No,  # Low Disk Space -> cancel
            ]
            page._apply()

        mock_runner_cls.assert_not_called()
        self.assertFalse(page._applying)

    def test_mod_manager_deleting_on_disk_files_invalidates_the_baseline(self):
        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(Path(tmp) / "gamma"),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
            )
            (Path(profile.gamma) / "mods" / "SomeMod").mkdir(parents=True)

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False

                def refresh_settings(self):
                    pass

            page = ModManagerPage(FakeWindow())
            page._lines = ["+SomeMod"]

            captured: dict = {}

            def capture_add_button(*a, **kw):
                btn = object()
                if a and a[0] == "Delete":
                    captured["confirm"] = btn
                return btn

            def capture_set_checkbox(checkbox):
                checkbox.setChecked(True)

            with (
                patch.object(page, "_selected_mod_indexes", return_value=[0]),
                patch.object(page, "_selected_mod_names", return_value=["SomeMod"]),
                patch.object(page, "_delete_mod_folders", return_value=True),
                patch.object(page, "_write_lines", return_value=True),
                patch.object(page, "_load_mods"),
                patch("commander_gui.ui.mod_manager_page.invalidate_baseline") as mock_invalidate,
                patch.object(QMessageBox, "exec", autospec=True, return_value=None),
                patch.object(
                    QMessageBox,
                    "clickedButton",
                    autospec=True,
                    side_effect=lambda: captured.get("confirm"),
                ),
                patch.object(
                    QMessageBox, "addButton", autospec=True, side_effect=capture_add_button
                ),
                patch.object(
                    QMessageBox, "setCheckBox", autospec=True, side_effect=capture_set_checkbox
                ),
            ):
                page._delete_selected_mods()

            mock_invalidate.assert_called_once_with(profile.gamma)

    def test_mod_installed_via_on_mod_moved_invalidates_the_baseline(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.mod_manager_page import ModManagerPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(Path(tmp) / "anomaly"),
                gamma=str(Path(tmp) / "gamma"),
                cache=str(Path(tmp) / "cache"),
                mo2_profile="G.A.M.M.A",
            )
            dest = Path(profile.gamma) / "mods" / "NewMod"
            dest.mkdir(parents=True)

            class FakeWindow:
                settings = CliSettings(profiles=[profile])
                install_busy = False

                def refresh_settings(self):
                    pass

                def statusBar(self):
                    return Mock()

            page = ModManagerPage(FakeWindow())
            page._lines = []
            page._install_active = True
            page._install_generation = 1

            with (
                patch.object(page, "_write_lines", return_value=True),
                patch.object(page, "_finish_install"),
                patch("commander_gui.ui.mod_manager_page.invalidate_baseline") as mock_invalidate,
            ):
                page._on_mod_moved(dest, generation=1)

            mock_invalidate.assert_called_once_with(profile.gamma)

    def test_run_repair_quarantine_skips_one_bad_folder_not_the_whole_batch(self):
        """Regression test: a ValueError from quarantine_mod_and_archive

        (e.g. a symlinked mods/ folder tripping the path-escape guard)
        used to propagate uncaught and abort the entire repair batch,
        defeating its own "one stubborn folder must not abort the whole
        repair" design intent - only OSError was caught before.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
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

            page = InstallPage(FakeWindow())
            page._scan_cancel = None
            page._repair_plan = type(
                "Plan", (), {"repairable": ["Bad Folder", "Good Folder"]}
            )()
            page._repair_records = {}

            def fake_quarantine(gamma_dir, folder, record=None):
                if folder == "Bad Folder":
                    raise ValueError("Refusing to touch mod folder outside mods/")
                return f"quarantined-{folder}"

            with patch(
                "commander_gui.ui.install_page.quarantine_mod_and_archive",
                side_effect=fake_quarantine,
            ):
                result = page._run_repair_quarantine(lambda *_a: None)

            self.assertEqual(result, ["quarantined-Good Folder"])

    def test_repaired_count_reflects_what_was_actually_quarantined(self):
        """Regression test: the final "repaired (N mod(s))" summaries

        (_on_post_scan_done, _conclude_after_repairs) used to count
        len(self._repair_plan.repairable) - the full repair *plan* - not
        what actually got set aside. A folder that failed to quarantine
        (see the batch-resilience test above) was never really repaired,
        so it must not be counted.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.integrity import GammaVerifyResult, Md5ScanResult
        from commander_gui.repair import RepairPlan
        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
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

            page = InstallPage(FakeWindow())
            page._scan_cancel = None
            # Plan says 3 folders needed repair, but only 2 were actually
            # quarantined (one failed - the batch-resilience case).
            page._repair_plan = RepairPlan(repairable=["A", "B", "C"])
            with patch.object(page, "_start_repair_install"):
                page._on_repair_quarantined(["quarantined-A", "quarantined-B"])
            self.assertEqual(page._repair_quarantined_count, 2)

            page._gamma_repair_done = True
            result = (
                Md5ScanResult(manifest_path=str(Path(tmp) / "gamma-md5.txt")),
                GammaVerifyResult(used_official_list=True),
            )
            with (
                patch.object(page, "_finish_verify"),
                patch("commander_gui.ui.install_page.QMessageBox.information"),
            ):
                page._on_post_scan_done(result)
            log_text = page.verify_progress.log.edit.toPlainText()
            self.assertIn("2 mod(s) reinstalled", log_text)
            self.assertNotIn("3 mod(s) reinstalled", log_text)

    def _make_install_page_for_repair(self, tmp):
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

        page = InstallPage(FakeWindow())
        page._scan_cancel = None
        page._repair_runner = Mock(was_cancelled=False)
        page._quarantine_records = ["record-a", "record-b"]
        return page, profile

    def test_full_install_does_not_skip_extraction_after_a_gamma_reset(self):
        """Regression test: GAMMA Reset wipes gamma/mods but never the

        download cache, so anomaly_installed(profile.anomaly) alone was a
        false "safe to skip re-extraction" signal (Anomaly is untouched by
        a GAMMA-only Reset) - every already-cached, hash-valid mod archive
        was then silently skipped instead of re-extracted into the
        freshly-emptied gamma/mods, permanently losing those mods (reported
        as 321 of ~577 mods surviving a GAMMA Reset).
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            anomaly = Path(tmp) / "anomaly"
            gamma = Path(tmp) / "gamma"
            anomaly.mkdir(parents=True)
            gamma.mkdir(parents=True)
            # Anomaly markers present (untouched by a GAMMA-only Reset)...
            (anomaly / "AnomalyLauncher.exe").touch()
            (anomaly / "fsgame.ltx").touch()
            # ...but GAMMA's own markers are absent - gamma/ was just wiped.
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(anomaly),
                gamma=str(gamma),
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

            page = InstallPage(FakeWindow())
            page._resume_state = None
            with (
                patch("commander_gui.ui.install_page.cli_command") as mock_cli_command,
                patch("commander_gui.ui.install_page.CommandRunner"),
            ):
                page._start_full_install(skip_confirm=True)

            argv = mock_cli_command.call_args[0][0]
            self.assertNotIn("--skip-extract-on-hash-match", argv)

    def test_full_install_skips_extraction_when_gamma_already_set_up(self):
        """The fast path must still work for a normal re-run when GAMMA is

        already genuinely present (not just Anomaly).
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            anomaly = Path(tmp) / "anomaly"
            gamma = Path(tmp) / "gamma"
            anomaly.mkdir(parents=True)
            gamma.mkdir(parents=True)
            (anomaly / "AnomalyLauncher.exe").touch()
            (anomaly / "fsgame.ltx").touch()
            (gamma / "ModOrganizer.exe").touch()
            (gamma / "ModOrganizer.ini").touch()
            (gamma / "profiles" / "G.A.M.M.A").mkdir(parents=True)
            profile = CliProfile(
                active=True,
                profile_name="Test",
                anomaly=str(anomaly),
                gamma=str(gamma),
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

            page = InstallPage(FakeWindow())
            page._resume_state = None
            with (
                patch("commander_gui.ui.install_page.cli_command") as mock_cli_command,
                patch("commander_gui.ui.install_page.CommandRunner"),
            ):
                page._start_full_install(skip_confirm=True)

            argv = mock_cli_command.call_args[0][0]
            self.assertIn("--skip-extract-on-hash-match", argv)

    def test_full_install_confirm_warns_when_disk_space_is_low(self):
        """Regression test: the Install GAMMA confirm dialog now warns

        upfront when the target drive doesn't have enough free space,
        instead of letting a multi-hour install fail partway through.
        """
        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
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

            page = InstallPage(FakeWindow())
            page._resume_state = None

            with (
                patch(
                    "commander_gui.ui.install_page.free_space_bytes",
                    return_value=5 * 1024**3,  # 5 GB free - well under ~150 GB
                ),
                patch(
                    "commander_gui.ui.install_page.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.No,
                ) as mock_question,
            ):
                page._start_full_install(skip_confirm=False)

            dialog_body = mock_question.call_args.args[2]
            self.assertIn("WARNING", dialog_body)
            self.assertIn("150 GB", dialog_body)

            with (
                patch(
                    "commander_gui.ui.install_page.free_space_bytes",
                    return_value=500 * 1024**3,  # plenty of free space
                ),
                patch(
                    "commander_gui.ui.install_page.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.No,
                ) as mock_question_ok,
            ):
                page._start_full_install(skip_confirm=False)

            self.assertNotIn(
                "this install needs about",
                mock_question_ok.call_args.args[2],
            )

    def test_anomaly_install_confirm_warns_when_disk_space_is_low(self):
        from PySide6.QtWidgets import QApplication, QMessageBox

        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
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

            page = InstallPage(FakeWindow())

            with (
                patch(
                    "commander_gui.ui.install_page.free_space_bytes",
                    return_value=2 * 1024**3,  # 2 GB free - under ~20 GB
                ),
                patch(
                    "commander_gui.ui.install_page.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.No,
                ) as mock_question,
            ):
                page._start_anomaly_install(skip_confirm=False)

            dialog_body = mock_question.call_args.args[2]
            self.assertIn("WARNING", dialog_body)
            self.assertIn("20 GB", dialog_body)

    def test_start_full_install_remembers_the_resolved_preserve_flags(self):
        """Regression test: an auto-retry (gamma_large_files_v2) re-enters

        _start_full_install and must reuse the exact preserve_user/
        preserve_mcm values the failed run actually used - previously
        nothing remembered them, so a retry silently fell back to the
        Install page's own (possibly unrelated/unchecked) checkboxes,
        dropping an explicit override e.g. from the Utilities GAMMA Reset
        dialog.
        """
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.install_page import InstallPage

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
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

            page = InstallPage(FakeWindow())
            page._resume_state = None
            page.checkboxes["preserve_user"].setChecked(False)
            page.checkboxes["preserve_mcm"].setChecked(False)
            with (
                patch("commander_gui.ui.install_page.cli_command"),
                patch("commander_gui.ui.install_page.CommandRunner"),
            ):
                # Explicit override, distinct from the (unchecked) checkboxes.
                page._start_full_install(
                    skip_confirm=True, preserve_user=True, preserve_mcm=True
                )
            self.assertTrue(page._active_preserve_user)
            self.assertTrue(page._active_preserve_mcm)

            # The mocked runner from the first call reports as still
            # "running" (a MagicMock's is_running() is truthy by default),
            # which would otherwise make this second, independent call a
            # no-op at _start_full_install's own busy guard.
            page._runner = None
            with (
                patch("commander_gui.ui.install_page.cli_command"),
                patch("commander_gui.ui.install_page.CommandRunner"),
            ):
                # No override this time - falls back to the checkboxes.
                page._start_full_install(skip_confirm=True)
            self.assertFalse(page._active_preserve_user)
            self.assertFalse(page._active_preserve_mcm)

    def test_prompt_repair_schedules_overlay_restore_when_only_anomaly_broken(self):
        """Regression test: an Anomaly-only repair (anomaly install) must

        not leave GAMMA's own file overlay (its replacement engine
        executables/DLLs, copied straight into the Anomaly root - not a
        gamma/mods/ entry, so invisible to the MD5 scan) reverted. Before
        this fix, when no GAMMA mods needed repairing, nothing ever
        re-ran full-install afterward to restore that overlay.
        """
        from PySide6.QtWidgets import QMessageBox

        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            page._repair_plan = None
            page._verify_counts = {"OK": 10, "CORRUPT": 9, "NOT FOUND": 0}
            with (
                patch.object(
                    QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
                ),
                patch.object(page, "_advance_repair_pipeline"),
            ):
                page._prompt_repair(anomaly_needs_repair=True, gamma_repairable=False)
            self.assertTrue(page._repair_anomaly_pending)
            self.assertFalse(page._gamma_repair_pending)
            self.assertTrue(page._gamma_overlay_restore_pending)

    def test_prompt_repair_skips_overlay_restore_when_gamma_mods_also_repaired(self):
        """The GAMMA-mods repair step's own full-install call already

        restores the overlay as a side effect - a second, redundant
        restore pass must not be scheduled on top of it.
        """
        from PySide6.QtWidgets import QMessageBox

        from commander_gui.repair import RepairPlan

        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            page._repair_plan = RepairPlan(repairable=["Some Mod"])
            page._verify_counts = {"OK": 10, "CORRUPT": 9, "NOT FOUND": 0}
            with (
                patch.object(
                    QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
                ),
                patch.object(page, "_advance_repair_pipeline"),
            ):
                page._prompt_repair(anomaly_needs_repair=True, gamma_repairable=True)
            self.assertTrue(page._repair_anomaly_pending)
            self.assertTrue(page._gamma_repair_pending)
            self.assertFalse(page._gamma_overlay_restore_pending)

    def test_advance_repair_pipeline_runs_overlay_restore_when_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            page._repair_anomaly_pending = False
            page._gamma_repair_pending = False
            page._gamma_overlay_restore_pending = True
            with patch.object(page, "_start_gamma_overlay_restore") as mock_restore:
                page._advance_repair_pipeline()
            mock_restore.assert_called_once()
            self.assertFalse(page._gamma_overlay_restore_pending)

    def test_start_gamma_overlay_restore_reuses_the_full_install_repair_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            with patch.object(page, "_start_repair_install") as mock_start_install:
                page._start_gamma_overlay_restore()
            mock_start_install.assert_called_once()
            self.assertTrue(page._gamma_overlay_restored)

    def test_on_repair_install_failure_restores_quarantined_mods(self):
        """Regression test: a failed reinstall must restore the mods that

        were moved aside, not leave the user with a permanently missing
        mod folder (the old code path called shutil.rmtree unconditionally
        before the reinstall was even attempted).
        """
        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            with (
                patch("commander_gui.ui.install_page.restore_from_quarantine", return_value=[]),
                patch("commander_gui.ui.install_page.purge_quarantine") as mock_purge,
                patch.object(page, "_finish_verify"),
            ):
                page._on_repair_install_finished(1, "Error: install failed")
            mock_purge.assert_not_called()
            self.assertEqual(page._quarantine_records, [])

    def test_on_repair_install_success_purges_the_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, profile = self._make_install_page_for_repair(tmp)
            with (
                patch("commander_gui.ui.install_page.restore_from_quarantine") as mock_restore,
                patch("commander_gui.ui.install_page.purge_quarantine") as mock_purge,
                patch.object(page, "_start_post_scan"),
            ):
                page._on_repair_install_finished(0, "Repair finished cleanly.")
            mock_restore.assert_not_called()
            mock_purge.assert_called_once_with(profile.gamma)
            self.assertEqual(page._quarantine_records, [])

    def test_on_repair_install_cancelled_restores_quarantined_mods(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            with (
                patch("commander_gui.ui.install_page.restore_from_quarantine", return_value=[]),
                patch("commander_gui.ui.install_page.purge_quarantine") as mock_purge,
                patch.object(page, "_finish_verify"),
            ):
                page._on_repair_install_cancelled()
            mock_purge.assert_not_called()
            self.assertEqual(page._quarantine_records, [])

    def test_on_gamma_verify_done_handles_a_plan_less_clean_scan(self):
        """Regression test: a fully clean Verify Integrity run (nothing

        changed, nothing missing) leaves ``plan`` as ``None`` (classify_problems
        is only called when there's something to classify) - _on_gamma_verify_done
        used to crash with ``AttributeError: 'NoneType' object has no
        attribute 'matched_records'`` unconditionally reading
        ``plan.matched_records``.
        """
        from commander_gui.integrity import GammaVerifyResult, Md5ScanResult

        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            page._verify_anomaly_ok = True
            presence = GammaVerifyResult(used_official_list=True)
            scan = Md5ScanResult(manifest_path=str(Path(tmp) / "gamma-md5.txt"))
            result = (presence, scan, None, {}, False, None)
            with (
                patch.object(page, "_finish_verify") as mock_finish,
                patch.object(page, "_gamma_ok_message", return_value="ok"),
            ):
                page._on_gamma_verify_done(result)
            self.assertIsNone(page._repair_plan)
            self.assertEqual(page._repair_records, {})
            mock_finish.assert_called_once()
            self.assertTrue(mock_finish.call_args.kwargs["ok"])

    def test_gamma_not_installed_still_offers_an_anomaly_repair(self):
        """Regression test: on a GAMMA-less profile, _on_gamma_verify_done

        used to jump straight to _conclude_after_repairs() before
        anomaly_needs_repair was ever computed, so a corrupt/missing
        Anomaly install was detected but never offered a repair prompt -
        the full anomaly-repair machinery was unreachable whenever GAMMA
        wasn't installed.
        """
        from commander_gui.ui.install_page import _GAMMA_NOT_INSTALLED

        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            page._verify_counts = {"OK": 1, "CORRUPT": 2, "NOT FOUND": 0}
            result = (_GAMMA_NOT_INSTALLED, None, None, {}, False, None)
            with patch.object(page, "_prompt_repair") as mock_prompt:
                page._on_gamma_verify_done(result)
            mock_prompt.assert_called_once_with(True, False)
            self.assertTrue(page._gamma_skipped)

    def test_gamma_not_installed_with_clean_anomaly_still_concludes(self):
        """The no-repair-needed path on a GAMMA-less profile must keep

        working exactly as before - only the repair-needed branch was
        missing.
        """
        from commander_gui.ui.install_page import _GAMMA_NOT_INSTALLED

        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            page._verify_counts = {"OK": 5, "CORRUPT": 0, "NOT FOUND": 0}
            result = (_GAMMA_NOT_INSTALLED, None, None, {}, False, None)
            with (
                patch.object(page, "_prompt_repair") as mock_prompt,
                patch.object(page, "_conclude_after_repairs") as mock_conclude,
            ):
                page._on_gamma_verify_done(result)
            mock_prompt.assert_not_called()
            mock_conclude.assert_called_once()

    def test_anomaly_recheck_cancel_releases_the_busy_lock(self):
        """Regression test: the Anomaly re-check runner (the stage right

        after an Anomaly repair) never connected its `cancelled` signal,
        unlike every sibling CommandRunner in this pipeline - cancelling
        during "Re-checking Anomaly" left window.install_busy stuck True
        forever (blocking every other install-affecting control app-wide
        until restart), since _on_verify_cancelled (which releases it)
        was never called.
        """
        busy_calls = []

        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            page.window.set_install_busy = lambda *a, **kw: busy_calls.append(a)
            with patch("commander_gui.ui.install_page.CommandRunner") as MockRunner:
                runner = Mock()
                MockRunner.return_value = runner
                page._on_anomaly_repair_finished(0, "Anomaly repair finished cleanly.")
                # The recheck runner's `cancelled` signal must be connected
                # to something that eventually releases the busy lock.
                cancelled_handler = runner.cancelled.connect.call_args[0][0]
            cancelled_handler()
            self.assertEqual(busy_calls, [(False,)])
            self.assertTrue(page.verify_button.isEnabled())

    def test_anomaly_repair_cancel_message_names_the_repair_stage(self):
        """Regression test: the shared cancel handler always logged

        "Anomaly check cancelled" even when the Anomaly *repair* (not the
        initial check) was what got cancelled - a misleading message.
        """
        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            page.window.set_install_busy = lambda *a, **kw: None
            page.verify_progress.log.append_line = Mock()
            page._on_verify_cancelled("Anomaly repair")
            page.verify_progress.log.append_line.assert_called_once_with(
                "Anomaly repair cancelled"
            )

    def test_winetricks_cancelled_resets_the_progress_buttons(self):
        """Regression test: cancelling Install Dependencies used to leave

        the Cancel/Pause buttons stuck visible/disabled forever, since
        _on_winetricks_cancelled never called wt_progress.on_cancelled()
        (unlike _on_verify_cancelled, which always has for verify_progress).
        """
        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            page.wt_progress.cancel_button.show()
            page.wt_progress.pause_button.show()
            page._on_winetricks_cancelled()
            self.assertTrue(page.wt_progress.cancel_button.isHidden())
            self.assertTrue(page.wt_progress.pause_button.isHidden())

    def test_post_anomaly_verify_cancelled_does_not_report_false_issues(self):
        """Regression test: _on_post_anomaly_verify_finished never checked

        was_cancelled, so cancelling during the post-install Anomaly
        verify still ran the normal-completion path afterward (`finished`
        always fires after `cancelled`), overwriting "Cancelled" with a
        false "issues found" message and calling _finish_anomaly_sequence
        a second, wrong time.
        """
        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            page.full_progress.cancel_button.show()
            page._post_anomaly_verify_cancelled = True
            page._post_anomaly_verify_runner = Mock(was_cancelled=True)
            with patch.object(page, "_finish_anomaly_sequence") as mock_finish:
                page._on_post_anomaly_verify_finished(1, "killed")
            mock_finish.assert_called_once_with(True, True)
            self.assertTrue(page.full_progress.cancel_button.isHidden())
            self.assertEqual(page.full_progress.status_label.text(), "Cancelled")
            self.assertIsNone(page._post_anomaly_verify_runner)
            self.assertFalse(page._post_anomaly_verify_cancelled)

    def test_post_anomaly_verify_finished_hides_the_cancel_button(self):
        """Regression test: full_progress.on_finished() was never called

        for a normal (non-cancelled) post-install Anomaly verify,
        leaving its Cancel button stuck visible/enabled forever.
        """
        with tempfile.TemporaryDirectory() as tmp:
            page, _profile = self._make_install_page_for_repair(tmp)
            page.full_progress.cancel_button.show()
            page._post_anomaly_verify_cancelled = False
            page._post_anomaly_verify_runner = Mock(was_cancelled=False)
            with patch.object(page, "_finish_anomaly_sequence") as mock_finish:
                page._on_post_anomaly_verify_finished(0, "OK")
            self.assertTrue(page.full_progress.cancel_button.isHidden())
            mock_finish.assert_called_once_with(False, True)

    def test_gui_settings_string_boolean_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gui-settings.json"
            path.write_text(
                json.dumps({"autostart": "false", "always_gamemoderun": "false"}),
                encoding="utf-8",
            )
            with patch.object(gui_settings, "gui_settings_path", return_value=path):
                state = gui_settings.load_gui_settings()
            self.assertFalse(state["autostart"])
            self.assertFalse(state["always_gamemoderun"])

    def test_gui_settings_normalizes_window_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gui-settings.json"
            path.write_text(
                json.dumps({"window_width": "bad", "window_height": 5000}),
                encoding="utf-8",
            )
            with patch.object(gui_settings, "gui_settings_path", return_value=path):
                state = gui_settings.load_gui_settings()
            self.assertEqual(state["window_width"], 1080)
            self.assertEqual(state["window_height"], 3840)

    def test_cli_profile_rejects_non_string_path_fields(self):
        default = CliProfile()
        data = {
            "Anomaly": 123,
            "ProfileName": "test",
        }
        profile = CliProfile.from_dict(data)
        self.assertEqual(profile.anomaly, default.anomaly)
        self.assertEqual(profile.profile_name, "test")

    def test_cli_profile_from_dict_survives_infinite_download_threads(self):
        """Regression test: json.loads() accepts the bare "Infinity" token as

        a real float, and int() on a non-finite float raises OverflowError,
        not ValueError - CliProfile.from_dict() only caught (TypeError,
        ValueError) around int(value) for DownloadThreads, so a hand-edited
        or corrupt settings.json with "DownloadThreads": Infinity used to
        crash load_settings() itself with an unhandled OverflowError instead
        of just keeping the default thread count.
        """
        default = CliProfile()
        data = json.loads('{"DownloadThreads": Infinity, "ProfileName": "test"}')
        profile = CliProfile.from_dict(data)
        self.assertEqual(profile.download_threads, default.download_threads)
        self.assertEqual(profile.profile_name, "test")

    def test_run_sync_separates_stdout_and_stderr(self):
        from commander_gui.cli_runner import run_sync

        rc, output = run_sync(["--version"])
        self.assertEqual(rc, 0)
        self.assertIsInstance(output, str)
        self.assertTrue(len(output) > 0)

    def test_wine_prefix_for_expands_tilde(self):
        result = wine_prefix_for("umu", "~/Games/umu/test")
        self.assertTrue(result.startswith("/"))
        self.assertNotIn("~", result)

    def test_wine_prefix_for_proton_adds_pfx(self):
        result = wine_prefix_for("proton:8", "")
        self.assertTrue(result.endswith("pfx"))

    def test_resolve_runner_expands_tilde_in_prefix(self):
        try:
            runner = resolve_runner("auto", "~/Games/test-prefix")
        except LaunchError:
            return
        prefix = runner.env.get("WINEPREFIX", "")
        if prefix:
            self.assertNotIn("~", prefix)

    def test_ensure_runner_prefix_closes_fd_when_marker_write_fails(self):
        """A newly-created prefix writes a ``.commander-runner`` marker via a
        raw ``os.open()`` fd handed to ``os.fdopen()``. If ``fdopen()`` itself
        raises before a file object takes ownership of the descriptor, the
        fd must still be closed explicitly - otherwise it leaks for the rest
        of the process lifetime every time this (rare but real, e.g. an
        encoding/locale misconfiguration) failure occurs."""
        with tempfile.TemporaryDirectory() as tmp:
            prefix_dir = Path(tmp) / "prefix"
            runner = Runner("wine", "Test Wine", [], {"WINEPREFIX": str(prefix_dir)})
            captured_fd = {}

            def _boom(fd, *args, **kwargs):
                captured_fd["fd"] = fd
                raise OSError("fdopen exploded")

            with (
                patch("commander_gui.launcher.os.fdopen", side_effect=_boom),
                self.assertRaises(LaunchError),
            ):
                ensure_runner_prefix(runner)
            self.assertIn("fd", captured_fd)
            # If ensure_runner_prefix leaked the descriptor, closing it here
            # would succeed; a proper fix already closed it, so this must
            # fail with EBADF.
            with self.assertRaises(OSError):
                os.close(captured_fd["fd"])

    def test_ensure_runner_prefix_closes_fd_when_racing_marker_read_fails(self):
        """Same leak, other branch: two processes race to create the marker,
        this one loses the ``O_CREAT|O_EXCL`` race and falls back to reading
        the marker the other process just created - that read-only fd is
        also handed to ``os.fdopen()`` and must be closed if that fails."""
        with tempfile.TemporaryDirectory() as tmp:
            prefix_dir = Path(tmp) / "prefix"
            runner = Runner("wine", "Test Wine", [], {"WINEPREFIX": str(prefix_dir)})
            captured_fd = {}
            real_open = os.open

            def _open_side_effect(path, flags, *args, **kwargs):
                if flags & os.O_CREAT and flags & os.O_EXCL:
                    # Simulate a racing process that created the marker
                    # first, between our lstat() and our own O_EXCL attempt.
                    Path(path).write_bytes(b"wine:Other\n")
                    raise FileExistsError()
                return real_open(path, flags, *args, **kwargs)

            def _boom(fd, *args, **kwargs):
                captured_fd["fd"] = fd
                raise OSError("fdopen exploded")

            with (
                patch("commander_gui.launcher.os.open", side_effect=_open_side_effect),
                patch("commander_gui.launcher.os.fdopen", side_effect=_boom),
                self.assertRaises(LaunchError),
            ):
                ensure_runner_prefix(runner)
            self.assertIn("fd", captured_fd)
            with self.assertRaises(OSError):
                os.close(captured_fd["fd"])

    def test_check_umu_returns_false_when_valid_binary_found(self):
        with patch("commander_gui.dependencies._umu_binary_valid", return_value=True):
            need_install, msg = check_umu()
        self.assertFalse(need_install)
        self.assertIsNone(msg)

    def test_check_umu_returns_true_when_missing(self):
        with (
            patch("commander_gui.dependencies._umu_binary_valid", return_value=False),
            patch(
                "commander_gui.dependencies.shutil.which", return_value="/usr/bin/curl"
            ),
        ):
            need_install, msg = check_umu()
        self.assertTrue(need_install)
        self.assertIsNone(msg)

    def test_check_umu_returns_error_when_no_curl(self):
        with (
            patch("commander_gui.dependencies._umu_binary_valid", return_value=False),
            patch("commander_gui.dependencies.shutil.which", return_value=None),
        ):
            need_install, msg = check_umu()
        self.assertTrue(need_install)
        self.assertIn("curl", msg)

    def test_umu_install_command_contains_zipapp_url(self):
        with patch(
            "commander_gui.winetricks.shutil.which", return_value="/usr/bin/curl"
        ):
            cmd = umu_install_command()
        self.assertEqual(cmd[0], "bash")
        self.assertIn("umu-launcher-1.4.4-zipapp.tar", cmd[2])
        self.assertIn("~/.local/bin/umu-run", cmd[2])

    def test_umu_install_command_is_atomic_and_time_bounded(self):
        with patch(
            "commander_gui.winetricks.shutil.which", return_value="/usr/bin/curl"
        ):
            script = umu_install_command()[2]
        # Stalled downloads must not hang forever.
        self.assertIn("--max-time 600", script)
        # Extract to a temp file and atomically move into place so a
        # truncated transfer never leaves a broken umu-run behind.
        self.assertIn("mktemp", script)
        self.assertIn("mv -f", script)
        self.assertNotIn("| tar -xOf - umu-run > ~/.local/bin/umu-run", script)

    def test_umu_install_command_returns_empty_without_curl(self):
        with patch("commander_gui.winetricks.shutil.which", return_value=None):
            cmd = umu_install_command()
        self.assertEqual(cmd, [])

    def test_umu_binary_returns_empty_when_not_found(self):
        with patch("commander_gui.winetricks.shutil.which", return_value=None):
            self.assertEqual(umu_binary(), "")


class XrayAnalyzerTests(unittest.TestCase):
    def test_engine_started_finding_is_reported_only_once(self):
        """Regression test: the dedup guard checked

        `f.title.startswith("Engine")`, but the actual finding title is
        "Game engine started (...)" - starting with "Game", not
        "Engine" - so the guard could never fire and every matching log
        line produced its own duplicate finding.
        """
        from assistant.analyzers.xray import analyze_xray

        lines = [
            "'XRAY' build 4436",
            "some other line",
            "'XRAY' build 4436",
        ]
        findings = analyze_xray("log.txt", "log.txt", lines)
        engine_started = [f for f in findings if f.title.startswith("Game engine started")]
        self.assertEqual(len(engine_started), 1)


class ProfileBundleTests(unittest.TestCase):
    def test_apply_to_skips_a_malformed_download_threads_value(self):
        """Regression test: a hand-edited or version-skewed bundle can

        carry a malformed value - apply_to() must validate the same way
        CliProfile.from_dict() validates the identical fields loaded
        from settings.json, or a bad bundle sets an attribute that later
        crashes CliProfile.to_dict()'s unguarded int(value) cast.
        """
        from commander_gui.profile_bundle import ImportedProfileBundle
        from commander_gui.settings import CliProfile

        profile = CliProfile(download_threads=4)
        bundle = ImportedProfileBundle(
            settings={"download_threads": "bogus", "mo2_profile": "G.A.M.M.A"},
            modlist_text=None,
        )
        bundle.apply_to(profile)
        self.assertEqual(profile.download_threads, 4)
        self.assertEqual(profile.mo2_profile, "G.A.M.M.A")

    def test_apply_to_skips_a_non_string_url_field(self):
        from commander_gui.profile_bundle import ImportedProfileBundle
        from commander_gui.settings import CliProfile

        profile = CliProfile(mod_pack_maker_url="https://original.example/list")
        bundle = ImportedProfileBundle(
            settings={"mod_pack_maker_url": 12345},
            modlist_text=None,
        )
        bundle.apply_to(profile)
        self.assertEqual(profile.mod_pack_maker_url, "https://original.example/list")


if __name__ == "__main__":
    unittest.main()
