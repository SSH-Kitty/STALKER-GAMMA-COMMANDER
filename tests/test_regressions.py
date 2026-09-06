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
from commander_gui.integrity import scan_mods_md5, verify_cache_archives
from commander_gui.launcher import (
    LaunchError,
    ProcessGroupRegistry,
    Runner,
    build_command,
    launch_detached,
    resolve_runner,
    wine_prefix_for,
)
from commander_gui.log_dump import _safe_archive_part, build_log_dump
from commander_gui.modlist import (
    add_category,
    add_mod,
    entries,
    install_conflict,
    rename_mod,
    reorder_mods,
    reorder_to_original,
    reparent_into_category,
    set_status_at,
)
from commander_gui.network import read_response_bytes
from commander_gui.proton_installer import (
    _safe_extract,
    fetch_ge_proton_releases,
    install_proton,
)
from commander_gui.repair import ModPackRecord
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
from commander_gui.user_mods import (
    add_user_mod,
    read_user_mods,
    remove_user_mods,
    rename_user_mod,
    tracker_path,
    write_user_mods,
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

    def test_gamma_progress_terminal_states_pin_at_100(self):
        """Completed/Skipped pin at 1.0; Extract/Expand keep prior value."""

        from commander_gui.ui.common import ProgressArea

        area = ProgressArea(show_table=True)
        # Mod A completes download
        area.on_line("[00:00:01] Mod A | Download | 100% | [1/4]")
        self.assertEqual(area._per_archive["Mod A"], 1.0)
        # Extract event doesn't regress
        area.on_line("[00:00:02] Mod A | Extract | 0% | [1/4]")
        self.assertEqual(area._per_archive["Mod A"], 1.0)
        # Skipped pins at 1.0
        area.on_line("[00:00:03] Mod B | Skipped | 100% | [2/4]")
        self.assertEqual(area._per_archive["Mod B"], 1.0)
        # Bar reflects average
        self.assertEqual(area.bar.value(), 50)  # 2/4 * 100

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
        to scroll to - new mods land disabled at the end of the file
        (see modlist.add_mod), easy to miss on a real-sized modlist."""
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
                patch("commander_gui.ui.mod_manager_page.add_user_mod"),
                patch.object(page, "_finish_install") as mock_finish,
            ):
                page._on_mod_moved(new_mod)

            self.assertEqual(page._just_installed_name, "Terrain Textures Redone")
            mock_finish.assert_called_once()

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

    def test_delete_mod_and_archive_deletes_archive_through_symlinked_downloads(self):
        from commander_gui.repair import ModPackRecord, delete_mod_and_archive

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "gamma"
            cache = Path(tmp) / "cache"
            record = ModPackRecord(
                1, "Broken Mod", "", "https://example.com/dl", "", "broken.zip", "", ""
            )
            folder = record.folder_name  # "1- Broken Mod"
            (base / "mods" / folder).mkdir(parents=True)
            cache.mkdir()
            (cache / "broken.zip").write_bytes(b"zip")
            (base / "downloads").symlink_to(cache, target_is_directory=True)
            removed = delete_mod_and_archive(base, folder, record)
            self.assertFalse((base / "mods" / folder).exists())
            self.assertFalse((cache / "broken.zip").exists())
            self.assertEqual(len(removed), 2)


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
        common._MO2_RUNNING_CACHE = 0.0
        run.return_value = Mock(returncode=1)
        self.assertFalse(common.mo2_running())
        self.assertEqual(run.call_args.args[0], [
            "/usr/bin/pgrep", "-f", r"[Mm]odOrganizer\.exe"
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
        page._registry = ProcessGroupRegistry()
        page._set_result = Mock()
        page._set_launch_button_state = Mock(
            side_effect=lambda launching: setattr(page, "_launching", launching)
        )
        page._launch_status_clear_timer = Mock()
        page._launch_timer = Mock()
        page._refresh_preview = Mock()
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
    def test_mo2_handoff_errors_when_mo2_never_appears(self, _mo2_pids):
        page = self._make_play_page_stub()
        page._handoff_checks = 10

        page._on_launch_check("GAMMA", ["umu-run"], Path("/tmp/launcher.log"))

        self.assertFalse(page._monitoring_mo2)
        self.assertFalse(page._launching)
        page._set_result.assert_called_with(
            "GAMMA launcher exited before MO2 was detected.", error=True
        )
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

    def test_add_mod_creates_extra_mods_separator(self):
        self.assertEqual(
            add_mod([], "NewMod"),
            ["-Extra Mods_separator", "-NewMod"],
        )

    def test_add_mod_creates_named_category_and_places_mod(self):
        self.assertEqual(
            add_mod(["-Audio_separator", "+Music"], "NewMod", category="Audio"),
            ["-Audio_separator", "+Music", "-NewMod"],
        )

    def test_add_mod_appends_to_end_of_existing_category(self):
        lines = ["-Extra Mods_separator", "+Mix", "-Audio_separator", "+Music"]
        self.assertEqual(
            add_mod(lines, "NewMod"),
            ["-Extra Mods_separator", "+Mix", "-NewMod", "-Audio_separator", "+Music"],
        )

    def test_add_mod_reuses_existing_extra_mods_separator(self):
        lines = ["+Visual_separator", "+Shaders", "+Extra Mods_separator"]
        self.assertEqual(
            add_mod(lines, "NewMod"),
            ["+Visual_separator", "+Shaders", "+Extra Mods_separator", "-NewMod"],
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

    def test_reparent_places_stray_mod_into_extra_mods(self):
        lines = [
            "+1- Audio_separator",
            "+KVMA HD 2.10",
            "+Terrain Textures Redone",
        ]
        result = reparent_into_category(lines, ["KVMA HD 2.10"])
        self.assertEqual(
            result,
            [
                "+1- Audio_separator",
                "+Terrain Textures Redone",
                "-Extra Mods_separator",
                "+KVMA HD 2.10",
            ],
        )

    def test_reparent_moves_multiple_mods_keeping_order_and_status(self):
        lines = [
            "-Audio_separator",
            "+SFX",
            "-KVMA HD 2.10",
            "+Gameplay_separator",
            "+Gameplay Mod",
            "-Terrain Textures Redone",
        ]
        result = reparent_into_category(
            lines, ["KVMA HD 2.10", "Terrain Textures Redone"]
        )
        self.assertEqual(
            result,
            [
                "-Audio_separator",
                "+SFX",
                "+Gameplay_separator",
                "+Gameplay Mod",
                "-Extra Mods_separator",
                "-KVMA HD 2.10",
                "-Terrain Textures Redone",
            ],
        )

    def test_reparent_no_change_when_already_grouped(self):
        lines = [
            "-Audio_separator",
            "+SFX",
            "-Extra Mods_separator",
            "-KVMA HD 2.10",
            "-Terrain Textures Redone",
        ]
        self.assertIsNone(reparent_into_category(lines, ["KVMA HD 2.10"]))

    def test_reparent_leaves_in_place_entries_and_appends_stray(self):
        lines = [
            "-Audio_separator",
            "+SFX",
            "+KVMA HD 2.10",
            "-Extra Mods_separator",
            "-Terrain Textures Redone",
        ]
        result = reparent_into_category(
            lines, ["KVMA HD 2.10", "Terrain Textures Redone"]
        )
        self.assertEqual(
            result,
            [
                "-Audio_separator",
                "+SFX",
                "-Extra Mods_separator",
                "-Terrain Textures Redone",
                "+KVMA HD 2.10",
            ],
        )

    def test_reparent_collapses_duplicate_separators(self):
        lines = [
            "-Extra Mods_separator",
            "+Mod A",
            "-Extra Mods_separator",
            "+Mod B",
        ]
        result = reparent_into_category(lines, ["Mod A", "Mod B"])
        self.assertEqual(
            result,
            ["-Extra Mods_separator", "+Mod A", "+Mod B"],
        )

    def test_reparent_ignores_unknown_and_separator_labels(self):
        lines = ["-Audio_separator", "+SFX", "+Real Mod"]
        result = reparent_into_category(
            lines, ["Missing Mod", "Audio_separator", "Real Mod"]
        )
        self.assertEqual(
            result,
            ["-Audio_separator", "+SFX", "-Extra Mods_separator", "+Real Mod"],
        )

    def test_reparent_moves_mod_back_after_add_mod_bug_scenario(self):
        # State reported by the user: an external rewrite stranded KVMA just
        # below the Audio separator, then a fresh install created a NEW Extra
        # Mods category at the end for the second mod.
        lines = [
            "+1- Audio_separator",
            "+KVMA HD 2.10",
            "-Extra Mods_separator",
            "-Terrain Textures Redone",
        ]
        result = reparent_into_category(
            lines, ["KVMA HD 2.10", "Terrain Textures Redone"]
        )
        grouped = {}
        current = "Uncategorized"
        for line in result:
            if "_separator" in line:
                current = line.lstrip("+-").removesuffix("_separator")
                grouped.setdefault(current, [])
            elif line[:1] in "+-":
                grouped.setdefault(current, []).append(line[1:])
        self.assertNotIn("KVMA HD 2.10", grouped.get("1- Audio", []))
        self.assertIn("KVMA HD 2.10", grouped["Extra Mods"])
        self.assertIn("Terrain Textures Redone", grouped["Extra Mods"])

    def test_reparent_requires_valid_category(self):
        with self.assertRaises(ValueError):
            reparent_into_category(["-Extra Mods_separator"], ["A"], category="a/b")


class ModCounterTests(unittest.TestCase):
    def _make_gamma(
        self,
        tmpdir,
        profile_folder: str = "G.A.M.M.A",
        modlist_text: str = "+ModA\n-ModB\n",
    ) -> Path:
        gamma_dir = Path(tmpdir) / "gamma"
        gamma_dir.mkdir(parents=True, exist_ok=True)
        for marker in ("ModOrganizer.exe", "ModOrganizer.ini"):
            (gamma_dir / marker).touch()
        profile_dir = gamma_dir / "profiles" / profile_folder
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_text(modlist_text, encoding="utf-8")
        return gamma_dir

    def test_counts_enabled_and_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma_dir = self._make_gamma(tmp)
            self.assertEqual(count_active_mods(str(gamma_dir), "G.A.M.M.A"), (1, 2))

    def test_case_insensitive_profile_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma_dir = self._make_gamma(tmp, profile_folder="G.A.M.M.A")
            self.assertEqual(count_active_mods(str(gamma_dir), "g.a.m.m.a"), (1, 2))

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

    def test_main_window_update_mod_counter_shows_and_hides_label(self):
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

            # No active profile -> hidden, not crashed.
            window.settings = CliSettings(profiles=[])
            window.update_mod_counter()
            self.assertFalse(window.mod_counter_label.isVisible())


class UserModsTrackerTests(unittest.TestCase):
    def _modlist(self, tmp: Path) -> Path:
        path = tmp / "modlist.txt"
        path.write_text("-Mod A\n+Mod B\n", encoding="utf-8")
        return path

    def test_tracker_path_sits_next_to_modlist(self):
        self.assertEqual(
            str(tracker_path("/x/modlist.txt")),
            "/x/modlist.txt.gammagui.mods.json",
        )

    def test_read_returns_empty_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read_user_mods(Path(tmp) / "modlist.txt"), [])

    def test_add_then_read_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            modlist = self._modlist(Path(tmp))
            add_user_mod(modlist, "Mod A")
            add_user_mod(modlist, "Mod A")
            add_user_mod(modlist, "Mod B")
            self.assertEqual(read_user_mods(modlist), ["Mod A", "Mod B"])

    def test_remove_drops_only_asked_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            modlist = self._modlist(Path(tmp))
            write_user_mods(modlist, ["Mod A", "Mod B", "Mod C"])
            remove_user_mods(modlist, ["Mod B"])
            self.assertEqual(read_user_mods(modlist), ["Mod A", "Mod C"])

    def test_rename_updates_tracked_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            modlist = self._modlist(Path(tmp))
            write_user_mods(modlist, ["Mod A", "Mod B"])
            rename_user_mod(modlist, "Mod A", "Renamed A")
            self.assertEqual(read_user_mods(modlist), ["Renamed A", "Mod B"])

    def test_corrupt_tracker_recovers_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            modlist = self._modlist(Path(tmp))
            tracker_path(modlist).write_text("{not json", encoding="utf-8")
            self.assertEqual(read_user_mods(modlist), [])

    def test_non_list_payload_recovers_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            modlist = self._modlist(Path(tmp))
            tracker_path(modlist).write_text('{"user_mods": "nope"}', encoding="utf-8")
            self.assertEqual(read_user_mods(modlist), [])

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

    def test_reorder_mods_rejects_cross_separator_move(self):
        lines = [
            "-Audio_separator",
            "+Music",
            "+SFX",
            "-Graphics_separator",
            "+Shaders",
            "+Textures",
        ]
        result = reorder_mods(lines, 1, 4)
        self.assertEqual(result, lines)

    def test_reorder_mods_allows_same_category_move(self):
        lines = [
            "-Audio_separator",
            "+Music",
            "+SFX",
            "-Graphics_separator",
            "+Shaders",
            "+Textures",
        ]
        result = reorder_mods(lines, 2, 1)
        self.assertEqual(result[1], "+SFX")
        self.assertEqual(result[2], "+Music")

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


if __name__ == "__main__":
    unittest.main()
