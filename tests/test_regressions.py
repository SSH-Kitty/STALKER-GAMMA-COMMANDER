import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commander_gui import gui_settings
from commander_gui.atomic import write_text
from commander_gui.diagnostics import _redact
from commander_gui.integrity import scan_mods_md5
from commander_gui.launcher import LaunchError, launch_detached
from commander_gui.modlist import set_status_at
from commander_gui.network import read_response_bytes
from commander_gui.proton_installer import _safe_extract, fetch_ge_proton_releases
from commander_gui.repair import ModPackRecord
from commander_gui.settings import CliProfile, cli_ok
from commander_gui.ui.install_page import _winetricks_progress
from commander_gui.ui.play_page import _is_hidden_launch_target
from commander_gui.ui.system_check_page import _check_tool, _winetricks_checks
from commander_gui.ui.utilities_page import _copy_dir_tree, _safe_wipe_path
from commander_gui.updates import diff_records, local_modpack_records
from commander_gui.winetricks import WINETRICKS_VERBS, winetricks_install_command


class RegressionTests(unittest.TestCase):
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

    def test_diagnostics_redacts_sensitive_values(self):
        text = '{"ApiToken": "secret", "ProfileName": "gamma"}'
        redacted = _redact(text)
        self.assertNotIn("secret", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_launch_detached_rejects_empty_command(self):
        with self.assertRaises(LaunchError):
            launch_detached([], {}, "")

    def test_modlist_status_ignores_stale_index(self):
        lines = ["+First"]
        self.assertEqual(set_status_at(lines, 5, False), lines)

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

    def test_wipe_guard_rejects_broad_paths(self):
        target = Path.home()
        self.assertFalse(_safe_wipe_path(str(target), target))

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

    def test_system_check_lists_every_winetricks_dependency(self):
        checks = _winetricks_checks(
            {verb: verb in {"quartz", "dx8vb"} for verb in WINETRICKS_VERBS},
            "/usr/bin/winetricks",
        )
        by_label = {check["label"]: check for check in checks}
        self.assertEqual(set(by_label), set(WINETRICKS_VERBS))
        self.assertEqual(by_label["quartz"]["state"], "ready")
        self.assertEqual(by_label["dx8vb"]["state"], "ready")

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


if __name__ == "__main__":
    unittest.main()
