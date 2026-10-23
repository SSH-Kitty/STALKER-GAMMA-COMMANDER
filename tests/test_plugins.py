"""The ModDB browser plugin: install from an archive, update checks, download events."""

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from commander_gui import plugins


def _archive(path: Path, files: dict[str, str]) -> str:
    with tarfile.open(path, "w:xz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return hashlib.sha512(path.read_bytes()).hexdigest()


class PluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)
        plugins.data_root().mkdir(parents=True)

    def _plugin(self, version: str = "1") -> tuple[Path, str]:
        meta = json.dumps({"protocol": plugins.PROTOCOL, "qt": "6.11.1", "exec": "run"})
        archive = self.tmp / f"plugin-{version}.tar.xz"
        digest = _archive(archive, {"moddb-browser/plugin.json": meta, "moddb-browser/run": version})
        return archive, digest

    def test_install_then_update_replaces_it(self) -> None:
        for version in ("1", "2"):
            archive, digest = self._plugin(version)
            plugins.install_archive(archive, digest)
            info = plugins.installed()
            self.assertEqual(plugins.launcher(info).read_text(), version)
        self.assertEqual(sorted(p.name for p in plugins.data_root().iterdir()), ["moddb-browser"])

    def test_checksum_mismatch_leaves_installed_plugin(self) -> None:
        archive, digest = self._plugin("1")
        plugins.install_archive(archive, digest)
        bad, _digest = self._plugin("2")
        with self.assertRaises(plugins.PluginError):
            plugins.install_archive(bad, "0" * 128)
        self.assertEqual(plugins.launcher(plugins.installed()).read_text(), "1")

    def test_failed_swap_puts_the_installed_plugin_back(self) -> None:
        archive, digest = self._plugin("1")
        plugins.install_archive(archive, digest)
        new, digest = self._plugin("2")
        real_replace = os.replace

        def replace(src, dst):
            if Path(src).parent.name.startswith(".plugin-staging-"):
                raise OSError("disk full")
            real_replace(src, dst)

        with mock.patch.object(plugins.os, "replace", replace), self.assertRaises(OSError):
            plugins.install_archive(new, digest)
        self.assertEqual(plugins.launcher(plugins.installed()).read_text(), "1")

    def test_install_from_a_file_uses_a_checksum_saved_next_to_it(self) -> None:
        archive, digest = self._plugin("1")
        self.assertIsNone(plugins.local_checksum(archive))
        Path(f"{archive}.sha512sum").write_text(f"{digest}  {archive.name}\n")
        self.assertEqual(plugins.local_checksum(archive), digest)
        plugins.install_archive(archive, plugins.local_checksum(archive))
        self.assertEqual(plugins.launcher(plugins.installed()).read_text(), "1")

    def test_archive_without_plugin_json_is_refused(self) -> None:
        archive = self.tmp / "other.tar.xz"
        digest = _archive(archive, {"something/else": "x"})
        with self.assertRaises(plugins.PluginError):
            plugins.install_archive(archive, digest)
        self.assertIsNone(plugins.installed())

    def test_launcher_outside_plugin_is_refused(self) -> None:
        archive, digest = self._plugin()
        plugins.install_archive(archive, digest)
        with self.assertRaises(plugins.PluginError):
            plugins.launcher({"exec": "../../../bin/sh"})

    def test_needs_update(self) -> None:
        ok = {"protocol": plugins.PROTOCOL, "qt": "6.11.1"}
        self.assertFalse(plugins.needs_update(ok, "6.11.1"))
        self.assertTrue(plugins.needs_update(ok, "6.12.0"))
        self.assertFalse(plugins.needs_update(ok, "6.10.2"))
        self.assertTrue(plugins.needs_update({**ok, "protocol": plugins.PROTOCOL + 1}, "6.11.1"))
        self.assertTrue(plugins.needs_update({}, "6.11.1"))
        with mock.patch.dict(os.environ, {"APPIMAGE": ""}):
            self.assertFalse(plugins.needs_update({**ok, "qt": "6.0.0"}))

    def test_parse_event_only_trusts_the_downloads_folder(self) -> None:
        plugins.downloads_dir().mkdir(parents=True)
        inside = plugins.downloads_dir() / "mod.7z"
        inside.write_text("x")
        outside = self.tmp / "elsewhere.7z"
        outside.write_text("x")

        def line(path) -> str:
            return json.dumps({"event": "downloaded", "file": str(path), "url": "https://x"})

        self.assertEqual(plugins.parse_event(line(inside)), inside.resolve())
        self.assertIsNone(plugins.parse_event(line(outside)))
        self.assertIsNone(plugins.parse_event(line(plugins.downloads_dir() / "missing.7z")))
        self.assertIsNone(plugins.parse_event("Qt: some warning"))
        self.assertIsNone(plugins.parse_event("[1, 2]"))

    def test_release_candidates_own_release_first_and_stable_skips_prereleases(self) -> None:
        feed = ["v1.5.0-Unstable1", "v1.4.1", "v1.4.0", "v1.4.0-Unstable2"]
        self.assertEqual(plugins.release_candidates(feed, "1.4.0"), ["v1.4.0", "v1.4.1"])
        self.assertEqual(
            plugins.release_candidates(feed, "1.4.0-unstable2"),
            ["v1.4.0-Unstable2", "v1.5.0-Unstable1", "v1.4.1", "v1.4.0"],
        )

    def test_asset_name_names_system_and_arch(self) -> None:
        self.assertRegex(plugins.asset_name(), r"^COMMANDER-ModDB-Browser-Plugin-(linux|windows|macos)-\w+\.tar\.xz$")



class AdBlockTests(unittest.TestCase):
    def test_ad_hosts_and_their_subdomains_are_blocked_but_moddb_is_not(self) -> None:
        try:
            from commander_gui.moddb_browser import is_ad_host
        except ImportError:
            self.skipTest("QtWebEngine isn't installed")
        self.assertTrue(is_ad_host("securepubads.g.doubleclick.net"))
        self.assertTrue(is_ad_host("cdn.intergient.com"))
        self.assertTrue(is_ad_host("HTML-LOAD.CC"))
        self.assertFalse(is_ad_host("www.moddb.com"))
        self.assertFalse(is_ad_host("sjc3.dl.dbolical.com"))
        self.assertFalse(is_ad_host("challenges.cloudflare.com"))
        self.assertFalse(is_ad_host("notdoubleclick.net"))


if __name__ == "__main__":
    unittest.main()
