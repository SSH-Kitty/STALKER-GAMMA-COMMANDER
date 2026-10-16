"""A GOG GAMMA install has no modpack_maker_list: COMMANDER rebuilds it.

Regression tests for the GOG report where Updates said "No modpack list
found" and the CLI's ``update apply`` could not run at all.
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from commander_gui import updates
from commander_gui.updates import (
    _read_modpack_list,
    check_updates,
    diff_records,
    rebuild_modpack_list,
    status_summary,
)

# DlLink, Instructions, Patch, AddonName, ModDbUrl, ZipName, Md5ModDb
REMOTE = "\n".join(
    [
        "Audio",
        "https://x/start/1\t0\t- alice\tProven Mod\thttps://m/1\tproven_2.zip\taaaa",
        "https://x/start/2\tgamedata:bin\t- bob\tOld Mod\thttps://m/2\told_2.zip\tbbbb",
        "https://x/start/3\t0\t- carol\tMissing Mod\thttps://m/3\tmissing.zip\tcccc",
        "https://x/start/4\t0\t- dave\tNo Md5 Mod\thttps://m/4\tnomd5.zip",
    ]
)


def _fake_gamma(root: Path) -> Path:
    gamma = root / "gamma"
    (gamma / "profiles" / "G.A.M.M.A").mkdir(parents=True)
    for marker in ("ModOrganizer.exe", "ModOrganizer.ini"):
        (gamma / marker).write_text("")
    mods = gamma / "mods"
    # Counters differ from the remote list on purpose (an older GAMMA).
    for folder, archive in (
        ("10- Proven Mod - alice", "C:\\downloads\\Proven_2.zip"),
        ("11- Old Mod - bob", "old_1.zip"),
        ("12- No Md5 Mod - dave", None),
    ):
        (mods / folder).mkdir(parents=True)
        if archive:
            (mods / folder / "meta.ini").write_text(
                f"[General]\ninstallationFile={archive}\n"
            )
    return gamma


class RebuildModpackListTest(unittest.TestCase):
    def test_only_proven_mods_count_as_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = _fake_gamma(Path(tmp))
            profile = gamma / "profiles" / "G.A.M.M.A"
            self.assertEqual(rebuild_modpack_list(str(gamma), "G.A.M.M.A", REMOTE), 3)

            remote = updates.parse_modpack_records(REMOTE)
            local = _read_modpack_list(profile)
            diffs = {d.text.split("- ", 1)[1]: d.status for d in diff_records(local, remote)}
            self.assertEqual(
                diffs,
                {"Old Mod - bob": "Modified", "Missing Mod - carol": "Added",
                 "No Md5 Mod - dave": "Added"},
            )

            # The JSON is what the CLI's update apply reads: same shape as
            # its own serializer (instructions list, patch without "- ").
            cli = json.loads((profile / "modpack_maker_list.json").read_text())
            self.assertEqual(cli[0]["dlLink"], "Audio")
            self.assertIsNone(cli[0]["addonName"])
            proven, old = cli[1], cli[2]
            self.assertEqual(proven["instructions"], [])
            self.assertEqual(proven["patch"], "alice")
            self.assertEqual(proven["md5ModDb"], "aaaa")
            self.assertEqual(old["instructions"], ["gamedata", "bin"])
            self.assertEqual(old["md5ModDb"], "")
            self.assertEqual(len(cli), 3)

            # Never overwrites a list that is already there.
            self.assertIsNone(rebuild_modpack_list(str(gamma), "G.A.M.M.A", REMOTE))

    def test_check_updates_rebuilds_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            gamma = _fake_gamma(Path(tmp))
            profile = SimpleNamespace(
                gamma=str(gamma), mo2_profile="G.A.M.M.A", mod_pack_maker_url="https://list"
            )
            response = io.BytesIO(REMOTE.encode())
            response.headers = {}
            with patch.object(updates, "urlopen", return_value=response), patch.object(updates, "remote_version", return_value="920"), patch.object(
                updates, "fetch_latest_patchnotes", return_value=None
            ), patch.object(updates, "_version_from_patchnotes_or_readme", return_value=None):
                status = check_updates(profile)
            self.assertIsNone(status.error)
            self.assertTrue(status.version_unknown)
            self.assertTrue(status.update_available)
            self.assertEqual(len(status.diffs), 3)
            text, _kind = status_summary(status)
            self.assertIn("version unknown", text)
            self.assertIn("3 mod(s)", status.note)

    def test_not_installed_still_reports_missing_list_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = SimpleNamespace(gamma=tmp, mo2_profile="G.A.M.M.A", mod_pack_maker_url="u")
            with patch.object(updates, "urlopen", side_effect=AssertionError("no fetch")):
                status = check_updates(profile)
            self.assertIn("No modpack list", status.error)


if __name__ == "__main__":
    unittest.main()
