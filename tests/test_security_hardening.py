"""The hardening from the full-app security review."""

import os
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SafeXmlTest(unittest.TestCase):
    def test_entity_bombs_and_doctypes_are_refused(self):
        from commander_gui import safe_xml

        bomb = (
            b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]><feed>&lol2;</feed>'
        )
        with self.assertRaises(safe_xml.XML_ERRORS):
            safe_xml.parse_bytes(bomb)

    def test_plain_feed_parses(self):
        from commander_gui import safe_xml

        root = safe_xml.parse_bytes(b"<feed><entry><title>v1</title></entry></feed>")
        self.assertEqual(root.find("entry/title").text, "v1")

    def test_namespaced_atom_feed_keeps_its_namespace(self):
        """GitHub's release feed is namespaced Atom; the patch notes on the
        Welcome page look entries up as ``{uri}entry``."""
        from commander_gui import safe_xml, updates

        feed = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            b'<link rel="alternate" href="https://x/releases/tag/v1.2.9H3"/>'
            b"<title>COMMANDER 1.2.9H3</title><content>&lt;p&gt;notes&lt;/p&gt;</content>"
            b"</entry></feed>"
        )
        root = safe_xml.parse_bytes(feed, namespaces=True)
        entry = root.find(f"{updates._ATOM_NS}entry")
        self.assertIsNotNone(entry)
        self.assertEqual(
            entry.find(f"{updates._ATOM_NS}link").get("href"),
            "https://x/releases/tag/v1.2.9H3",
        )

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with (
            patch.object(updates, "urlopen", return_value=_Resp()),
            patch.object(updates, "read_response_bytes", return_value=feed),
            patch.object(updates, "_latest_stable_tag", return_value="v1.2.9H3"),
        ):
            self.assertEqual(
                updates.fetch_latest_release_notes(),
                ("COMMANDER 1.2.9H3", "<p>notes</p>"),
            )

    def test_update_feed_uses_the_safe_parser(self):
        from commander_gui import updates

        source = Path(updates.__file__).read_text(encoding="utf-8")
        self.assertNotIn("ET.fromstring", source)


class ProfileBundleSourceTest(unittest.TestCase):
    def _apply(self, settings):
        from commander_gui.profile_bundle import ImportedProfileBundle
        from commander_gui.settings import CliProfile

        profile = CliProfile()
        ImportedProfileBundle(settings, None).apply_to(profile)
        return profile

    def test_non_http_urls_and_option_like_branches_are_skipped(self):
        from commander_gui.settings import CliProfile

        default = CliProfile()
        profile = self._apply(
            {
                "mod_list_url": "file:///etc/passwd",
                "gamma_setup_repo_url": "https://example.com/repo.git",
                "gamma_setup_repo_branch": "--upload-pack=touch /tmp/x",
                "stalker_gamma_repo_branch": "../../main",
                "gamma_large_files_repo_branch": "dev/v1.2",
            }
        )
        self.assertEqual(profile.mod_list_url, default.mod_list_url)
        self.assertEqual(profile.gamma_setup_repo_url, "https://example.com/repo.git")
        self.assertEqual(profile.gamma_setup_repo_branch, default.gamma_setup_repo_branch)
        self.assertEqual(profile.stalker_gamma_repo_branch, default.stalker_gamma_repo_branch)
        self.assertEqual(profile.gamma_large_files_repo_branch, "dev/v1.2")


class SelfUpdateTagTest(unittest.TestCase):
    def test_only_plain_version_tags_become_urls(self):
        from commander_gui.self_update import (
            CommanderSelfUpdateError,
            commander_update_asset_url,
        )

        self.assertTrue(commander_update_asset_url("v1.3.0").endswith("1.3.0-x86_64.AppImage"))
        for bad in ("../../evil", "v1/../../x", "v1?x=1", ""):
            with self.assertRaises(CommanderSelfUpdateError):
                commander_update_asset_url(bad)


class DiscordIpcTest(unittest.TestCase):
    def test_oversized_frames_are_refused(self):
        from commander_gui.discord_rpc import _IpcClient

        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.sendall(struct.pack("<II", 1, 0x7FFFFFFF))
        with self.assertRaises(ConnectionError):
            _IpcClient(ours)._recv()

    def test_sockets_of_other_users_are_skipped(self):
        from commander_gui import discord_rpc

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "discord-ipc-0"
            path.touch()
            self.assertTrue(discord_rpc._owned_by_us(path))
            with patch.object(os, "getuid", return_value=os.getuid() + 1):
                self.assertFalse(discord_rpc._owned_by_us(path))


if __name__ == "__main__":
    unittest.main()
