import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from commander_gui.fomod import (
    FomodConfig,
    FomodFile,
    FomodGroup,
    FomodOption,
    FomodStep,
    apply_options,
    parse_config,
)
from commander_gui.mod_install import (
    ModInstallError,
    _validate_archive_entries,
    default_mod_name,
    install_archive,
    sanitize_name,
)
from commander_gui.modlist import (
    add_category,
    add_mod,
    count_mods,
    flip_priority,
    grouped,
    move_mod,
    reorder_mods,
)


class ModInstallTests(unittest.TestCase):
    def test_count_mods_excludes_separators(self):
        lines = ["+ModA", "-ModB", "-Weapons_separator", "+ModC"]
        self.assertEqual(count_mods(lines), (2, 3))

    def test_count_mods_empty_modlist(self):
        self.assertEqual(count_mods([]), (0, 0))

    def test_count_mods_agrees_with_grouped(self):
        lines = ["+ModA", "-Weapons_separator", "+ModB", "-ModC"]
        total_from_grouped = sum(len(mods) for _, mods in grouped(lines))
        enabled_from_grouped = sum(
            1
            for _, mods in grouped(lines)
            for status, _, _ in mods
            if status == "Enabled"
        )
        self.assertEqual(count_mods(lines), (enabled_from_grouped, total_from_grouped))

    def test_sanitize_name_rejects_empty_and_path_parts(self):
        self.assertEqual(sanitize_name(" My Mod "), "My Mod")
        self.assertEqual(sanitize_name("folder/name"), "folder name")
        with self.assertRaises(ModInstallError):
            sanitize_name("...")
        with self.assertRaises(ModInstallError):
            sanitize_name("unsafe\nname")

    def test_add_mod_appends_disabled_entry(self):
        lines = ["# GAMMA", "+Existing"]
        # Newly installed mods land at the very top of the list (after any
        # leading comment line), with no category - matching real MO2's
        # landing spot for a fresh install. The mod starts disabled so it
        # cannot alter a working GAMMA setup.
        self.assertEqual(
            add_mod(lines, "New Mod"),
            ["# GAMMA", "-New Mod", "+Existing"],
        )
        with self.assertRaises(ValueError):
            add_mod(lines, "Existing")

    def test_add_mod_with_explicit_category_inserts_before_its_separator(self):
        # A category's members sit immediately above its separator, not
        # below (see grouped()), so an explicit-category add lands there.
        lines = ["+B", "-Graphics_separator"]
        self.assertEqual(
            add_mod(lines, "New Mod", category="Graphics"),
            ["+B", "-New Mod", "-Graphics_separator"],
        )

    def test_add_mod_with_new_category_appends_mod_then_separator(self):
        lines = ["+A"]
        self.assertEqual(
            add_mod(lines, "New Mod", category="Extras"),
            ["+A", "-New Mod", "-Extras_separator"],
        )

    def test_reorder_mods_moves_down_without_skipping_target(self):
        lines = ["# GAMMA", "+A", "-B", "+C"]
        self.assertEqual(
            reorder_mods(lines, 1, 3),
            ["# GAMMA", "-B", "+A", "+C"],
        )

    def test_move_mod_preserves_status_and_can_target_empty_category(self):
        # A category's members are the mod lines immediately above its own
        # separator (see grouped()), so two adjacent separators with
        # nothing between them is what makes "Empty" genuinely empty here.
        lines = ["+A", "-Graphics_separator", "-Empty_separator", "+B"]
        moved = move_mod(lines, "A", category="Empty")
        self.assertEqual(
            moved,
            ["-Graphics_separator", "+A", "-Empty_separator", "+B"],
        )

    def test_move_mod_crosses_categories_freely(self):
        lines = ["+A", "-Graphics_separator", "+B"]
        self.assertEqual(
            move_mod(lines, "A", target_name="B"),
            ["-Graphics_separator", "+A", "+B"],
        )

    def test_move_mod_places_before_or_after_target(self):
        lines = ["+A", "+B", "+C"]
        self.assertEqual(move_mod(lines, "A", target_name="C"), ["+B", "+A", "+C"])
        self.assertEqual(
            move_mod(lines, "A", target_name="B", before=False),
            ["+B", "+A", "+C"],
        )

    def test_flip_priority_reverses_categories_and_mods_together(self):
        # A real flip must move whole blocks end-to-end (not just shuffle
        # the mods inside each one while every block stays put, which
        # leaves the file in a mixed order that is neither the original
        # nor a true reversal). A block is a separator plus the mod lines
        # immediately above it (see grouped()); the "Empty" block (B, C,
        # then its separator) moves to the front with its own mods
        # reversed, and the "Graphics" block (A, then its separator) moves
        # after it.
        lines = ["+A", "-Graphics_separator", "-B", "+C", "-Empty_separator"]
        self.assertEqual(
            flip_priority(lines),
            ["+C", "-B", "-Empty_separator", "+A", "-Graphics_separator"],
        )

    def test_flip_priority_preserves_statuses_within_a_category(self):
        # B and C sit above Graphics_separator, so they're Graphics's real
        # members (see grouped()); Empty_separator's own block is just
        # itself, with no members, so it's unaffected by the mod reversal.
        lines = ["-Empty_separator", "-B", "+C", "-Graphics_separator"]
        self.assertEqual(
            flip_priority(lines),
            ["+C", "-B", "-Graphics_separator", "-Empty_separator"],
        )

    def test_flip_priority_without_categories_reverses_flat_list(self):
        lines = ["+A", "+B", "+C"]
        self.assertEqual(flip_priority(lines), ["+C", "+B", "+A"])

    def test_grouped_retains_empty_separator_categories(self):
        lines = ["-Audio_separator", "-Empty_separator"]
        self.assertEqual(
            [(name, mods) for name, mods in grouped(lines)],
            [("Audio", []), ("Empty", [])],
        )

    def test_grouped_separator_claims_mods_above_it_not_below(self):
        # Shaped like GAMMA's real modlist.txt: mods immediately above a
        # separator are its members (audio/voice mods before "Audio"),
        # mods below belong to whatever separator comes next (weapon mods
        # falling after "Audio" are not audio). This is the exact off-by-one
        # the bug report described ("1- Audio shows no mods").
        lines = [
            "-Weapons_separator",
            "+Voiced Actor",
            "+Ambient Music Pack",
            "-Audio_separator",
            "+Weapon Pack",
        ]
        self.assertEqual(
            [(name, [entry[1] for entry in mods]) for name, mods in grouped(lines)],
            [
                ("Weapons", []),
                ("Audio", ["Voiced Actor", "Ambient Music Pack"]),
                ("Uncategorized", ["Weapon Pack"]),
            ],
        )

    def test_mods_before_first_separator_are_always_uncategorized(self):
        # Deliberate carve-out: even though the general rule is "a
        # separator claims the mods above it", the very first separator in
        # the file never claims what precedes it - that run is always
        # Uncategorized. This is what makes a freshly-installed mod
        # (inserted at file-top by add_mod()) land uncategorized instead of
        # silently acquiring whatever category happens to be first.
        lines = ["+New Mod", "+Old Audio Mod", "-Audio_separator"]
        self.assertEqual(
            [(name, [entry[1] for entry in mods]) for name, mods in grouped(lines)],
            [
                ("Uncategorized", ["New Mod", "Old Audio Mod"]),
                ("Audio", []),
            ],
        )

    def test_move_to_category_survives_a_reload(self):
        # The bug report's core complaint: a mod moved into a category must
        # stay there. modlist.py no longer has any mechanism that
        # re-parents mods back into a fixed bucket on the next read, so a
        # move committed to `lines` is permanent - re-reading (simulated
        # here by just calling grouped() again on the saved result, the
        # same way the UI re-derives its tree on every reload) must keep
        # showing the mod under its new category.
        # "Audio" must not be the file's first separator here, or the
        # top-of-file carve-out (see test above) would force the mod back
        # to Uncategorized regardless of where move_mod() puts it.
        lines = add_mod(["-Weapons_separator", "-Audio_separator"], "New Mod")
        moved = move_mod(lines, "New Mod", category="Audio")
        categories = {name: [entry[1] for entry in mods] for name, mods in grouped(moved)}
        self.assertEqual(categories["Audio"], ["New Mod"])
        # Re-deriving the tree again (as a reload would) is idempotent.
        reloaded_categories = {
            name: [entry[1] for entry in mods] for name, mods in grouped(moved)
        }
        self.assertEqual(reloaded_categories, categories)

    def test_add_category_rejects_duplicates(self):
        lines = ["-Audio_separator"]
        self.assertEqual(
            add_category(lines, "Graphics"), lines + ["-Graphics_separator"]
        )
        with self.assertRaises(ValueError):
            add_category(lines, "Audio")

    def test_install_archive_unwraps_single_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "Example Mod.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("Example Mod/gamedata/config.ltx", "data")
            mods = root / "mods"
            installed = install_archive(archive, mods)
            self.assertEqual(installed, "Example Mod")
            self.assertEqual(
                (mods / installed / "gamedata/config.ltx").read_text(), "data"
            )

    def _mock_listing_result(self, listing_body: str):
        from subprocess import CompletedProcess

        header = (
            "----------\n"
            "Path = /tmp/archive.7z\n"
            "Type = 7z\n"
            "Physical Size = 1\n"
            "\n"
        )
        return CompletedProcess(
            args=[], returncode=0, stdout=header + listing_body, stderr=""
        )

    def test_validate_archive_entries_rejects_path_traversal(self):
        listing = "Path = ../../etc/cron.d/evil\nSize = 1\n"
        with (
            patch(
                "commander_gui.mod_install.subprocess.run",
                return_value=self._mock_listing_result(listing),
            ),
            self.assertRaises(ModInstallError),
        ):
            _validate_archive_entries(Path("/fake/7zz"), Path("/tmp/archive.7z"))

    def test_validate_archive_entries_rejects_absolute_path(self):
        listing = "Path = /etc/passwd\nSize = 1\n"
        with (
            patch(
                "commander_gui.mod_install.subprocess.run",
                return_value=self._mock_listing_result(listing),
            ),
            self.assertRaises(ModInstallError),
        ):
            _validate_archive_entries(Path("/fake/7zz"), Path("/tmp/archive.7z"))

    def test_validate_archive_entries_allows_normal_entries(self):
        listing = "Path = gamedata/config.ltx\nSize = 1\n\nPath = readme.txt\nSize = 1\n"
        with patch(
            "commander_gui.mod_install.subprocess.run",
            return_value=self._mock_listing_result(listing),
        ):
            _validate_archive_entries(Path("/fake/7zz"), Path("/tmp/archive.7z"))

    def test_fomod_config_and_selected_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "ModuleConfig.xml"
            config_path.write_text(
                """<config>
                  <moduleName>Example FOMOD</moduleName>
                  <author>Test Author</author>
                  <installSteps><installStep name=\"Choose\">
                    <optionalFileGroups><group name=\"Content\" type=\"SelectAny\">
                      <plugins><plugin><name>Patch</name><description>Patch files</description>
                        <files><file source=\"patch.txt\" destination=\"gamedata\" /></files>
                      </plugin></plugins>
                    </group></optionalFileGroups>
                  </installStep></installSteps>
                </config>""",
                encoding="utf-8",
            )
            (root / "patch.txt").write_text("patched", encoding="utf-8")
            config = parse_config(config_path)
            destination = root / "selected"
            apply_options(config, root, destination, {(0, 0): [0]})
            self.assertEqual(
                (destination / "gamedata/patch.txt").read_text(encoding="utf-8"),
                "patched",
            )

    def test_fomod_config_rejects_doctype_entity_expansion(self):
        """A malicious/corrupted ModuleConfig.xml must not run entity expansion."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "ModuleConfig.xml"
            config_path.write_text(
                '<?xml version="1.0"?>\n'
                "<!DOCTYPE config [ <!ENTITY x \"y\"> ]>\n"
                "<config><moduleName>&x;</moduleName></config>",
                encoding="utf-8",
            )
            with self.assertRaises(ModInstallError):
                parse_config(config_path)

    def test_fomod_standard_name_attributes_are_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "ModuleConfig.xml"
            config_path.write_text(
                """<config>
                  <moduleName>Example FOMOD</moduleName>
                  <author>Test Author</author>
                  <installSteps><installStep name="Version">
                    <optionalFileGroups>
                      <group name="Main files" type="SelectExactlyOne">
                        <plugins>
                          <plugin name="Recommended">
                            <description>Recommended files</description>
                          </plugin>
                          <plugin name="Minimal" />
                        </plugins>
                      </group>
                    </optionalFileGroups>
                  </installStep></installSteps>
                </config>""",
                encoding="utf-8",
            )

            config = parse_config(config_path)

            self.assertEqual(config.steps[0].name, "Version")
            self.assertEqual(config.steps[0].groups[0].name, "Main files")
            self.assertEqual(
                [option.name for option in config.steps[0].groups[0].options],
                ["Recommended", "Minimal"],
            )
            self.assertEqual(
                config.steps[0].groups[0].options[0].description,
                "Recommended files",
            )

    def test_fomod_normalizes_windows_source_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "ModuleConfig.xml"
            config_path.write_text(
                """<config><installSteps><installStep name="Install">
                  <optionalFileGroups><group name="Files" type="SelectAny">
                    <plugins><plugin name="Main"><files>
                      <folder source="payload\\gamedata" destination="gamedata" />
                    </files></plugin></plugins>
                  </group></optionalFileGroups>
                </installStep></installSteps></config>""",
                encoding="utf-8",
            )
            source = root / "payload" / "gamedata"
            source.mkdir(parents=True)
            (source / "config.ltx").write_text("data", encoding="utf-8")

            config = parse_config(config_path)
            destination = root / "selected"
            apply_options(config, root, destination, {(0, 0): [0]})

            self.assertEqual(
                (destination / "gamedata" / "gamedata" / "config.ltx").read_text(
                    encoding="utf-8"
                ),
                "data",
            )

    def test_fomod_rejects_symlink_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "patch.txt").write_text("patched", encoding="utf-8")
            destination = root / "selected"
            destination.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (destination / "gamedata").symlink_to(outside, target_is_directory=True)
            config = FomodConfig(
                "FOMOD",
                "",
                (
                    FomodStep(
                        "Install",
                        (
                            FomodGroup(
                                "Files",
                                "SelectAny",
                                (
                                    FomodOption(
                                        "Patch",
                                        files=(FomodFile("patch.txt", "gamedata"),),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )
            with self.assertRaisesRegex(ModInstallError, "symlink"):
                apply_options(config, root, destination, {(0, 0): [0]})
            self.assertFalse((outside / "patch.txt").exists())

    def test_default_mod_name_handles_compound_extension(self):
        self.assertEqual(default_mod_name(Path("My Mod.7z")), "My Mod")
        self.assertEqual(default_mod_name(Path("My Mod.fomod")), "My Mod")
