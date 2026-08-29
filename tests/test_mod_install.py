import tempfile
import unittest
import zipfile
from pathlib import Path

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
    default_mod_name,
    install_archive,
    sanitize_name,
)
from commander_gui.modlist import (
    add_category,
    add_mod,
    flip_priority,
    grouped,
    move_mod,
    reorder_mods,
)


class ModInstallTests(unittest.TestCase):
    def test_sanitize_name_rejects_empty_and_path_parts(self):
        self.assertEqual(sanitize_name(" My Mod "), "My Mod")
        self.assertEqual(sanitize_name("folder/name"), "folder name")
        with self.assertRaises(ModInstallError):
            sanitize_name("...")
        with self.assertRaises(ModInstallError):
            sanitize_name("unsafe\nname")

    def test_add_mod_appends_disabled_entry(self):
        lines = ["# GAMMA", "+Existing"]
        # Newly installed mods are grouped under an "Extra Mods" separator
        # appended at the end of modlist.txt (= bottom of MO2 left pane). The
        # mod starts disabled so it cannot alter a working GAMMA setup.
        self.assertEqual(
            add_mod(lines, "New Mod"),
            ["# GAMMA", "+Existing", "-Extra Mods_separator", "-New Mod"],
        )
        with self.assertRaises(ValueError):
            add_mod(lines, "Existing")

    def test_reorder_mods_moves_down_without_skipping_target(self):
        lines = ["# GAMMA", "+A", "-B", "+C"]
        self.assertEqual(
            reorder_mods(lines, 1, 3),
            ["# GAMMA", "-B", "+A", "+C"],
        )

    def test_move_mod_preserves_status_and_can_target_empty_category(self):
        lines = ["+A", "-Graphics_separator", "+B", "-Empty_separator"]
        moved = move_mod(lines, "A", category="Empty")
        self.assertEqual(
            moved,
            ["-Graphics_separator", "+B", "-Empty_separator", "+A"],
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

    def test_flip_priority_preserves_categories_and_statuses(self):
        lines = ["+A", "-Graphics_separator", "-B", "+C", "-Empty_separator"]
        self.assertEqual(
            flip_priority(lines),
            ["+A", "-Graphics_separator", "+C", "-B", "-Empty_separator"],
        )

    def test_grouped_retains_empty_separator_categories(self):
        lines = ["-Audio_separator", "-Empty_separator"]
        self.assertEqual(
            [(name, mods) for name, mods in grouped(lines)],
            [("Audio", []), ("Empty", [])],
        )

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
