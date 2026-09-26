import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from commander_gui.fomod import (
    NOT_USABLE,
    RECOMMENDED,
    FomodWizard,
    apply_options,
    parse_config,
)

#: A cut-down copy of a real installer's shape (UI Rework G.A.M.M.A. Style):
#: a game choice that sets a flag, options whose type follows that flag, a
#: step only shown for one choice, conditional installs and priorities.
CONFIG = """<config>
  <moduleName>Test Mod</moduleName>
  <moduleImage path="fomod\\images\\logo.png" />
  <installSteps order="Explicit">
    <installStep name="Game">
      <optionalFileGroups order="Explicit">
        <group name="Vanilla or GAMMA?" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="GAMMA">
              <description>For GAMMA.</description>
              <image path="fomod\\images\\gamma.png" />
              <conditionFlags><flag name="anomaly">Inactive</flag></conditionFlags>
              <files><folder source="base_gamma" destination="" priority="0" /></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
            <plugin name="Vanilla">
              <description>For vanilla.</description>
              <conditionFlags><flag name="anomaly">Active</flag></conditionFlags>
              <files><folder source="base_vanilla" destination="" priority="0" /></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Styles">
      <optionalFileGroups order="Explicit">
        <group name="Bar" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="White">
              <files><folder source="white" destination="" priority="1" /></files>
              <typeDescriptor><dependencyType>
                <defaultType name="Optional"/>
                <patterns><pattern>
                  <dependencies operator="And"><flagDependency flag="anomaly" value="Inactive"/></dependencies>
                  <type name="Recommended"/>
                </pattern></patterns>
              </dependencyType></typeDescriptor>
            </plugin>
            <plugin name="Gold">
              <files><folder source="gold" destination="" priority="1" /></files>
              <typeDescriptor><dependencyType>
                <defaultType name="Optional"/>
                <patterns><pattern>
                  <dependencies operator="And"><flagDependency flag="anomaly" value="Active"/></dependencies>
                  <type name="Recommended"/>
                </pattern></patterns>
              </dependencyType></typeDescriptor>
            </plugin>
          </plugins>
        </group>
        <group name="Menu" type="SelectAtMostOne">
          <plugins order="Explicit">
            <plugin name="GAMMA menu">
              <files><folder source="menu_gamma" destination="" priority="0" /></files>
              <typeDescriptor><dependencyType>
                <defaultType name="Optional"/>
                <patterns><pattern>
                  <dependencies operator="And"><flagDependency flag="anomaly" value="Active"/></dependencies>
                  <type name="NotUsable"/>
                </pattern></patterns>
              </dependencyType></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Vanilla extras">
      <visible><flagDependency flag="anomaly" value="Active"/></visible>
      <optionalFileGroups order="Explicit">
        <group name="Extras" type="SelectAny">
          <plugins order="Explicit">
            <plugin name="Extra">
              <files><folder source="extra" destination="" /></files>
              <typeDescriptor><type name="Recommended"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls><patterns>
    <pattern>
      <dependencies operator="And"><flagDependency flag="anomaly" value="Inactive"/></dependencies>
      <files><folder source="patch_gamma" destination="" priority="3" /></files>
    </pattern>
  </patterns></conditionalFileInstalls>
</config>
"""


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class FomodWizardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "mod"
        (self.root / "fomod").mkdir(parents=True)
        # Real installers are often UTF-16 with a BOM.
        (self.root / "fomod" / "ModuleConfig.xml").write_text(CONFIG, encoding="utf-16")
        for folder in (
            "base_gamma", "base_vanilla", "white", "gold", "menu_gamma", "extra", "patch_gamma",
        ):
            _write(self.root, f"{folder}/gamedata/configs/{folder}.ltx", folder)
        # Two folders writing the same file: priority decides who wins.
        _write(self.root, "base_gamma/gamedata/configs/bar.ltx", "base")
        _write(self.root, "white/gamedata/configs/bar.ltx", "white")
        _write(self.root, "patch_gamma/gamedata/configs/bar.ltx", "patch")
        self.config = parse_config(self.root / "fomod" / "ModuleConfig.xml")

    def _run(self, choose_vanilla: bool = False) -> FomodWizard:
        wizard = FomodWizard(self.config)
        wizard.enter(0)
        if choose_vanilla:
            wizard.toggle(0, 0, 1)
        for step in wizard.visible_steps():
            wizard.enter(step)
        return wizard

    def test_parses_flags_types_images_and_conditions(self):
        option = self.config.steps[0].groups[0].options[0]
        self.assertEqual(option.flags, (("anomaly", "Inactive"),))
        self.assertEqual(option.image, "fomod\\images\\gamma.png")
        self.assertEqual(self.config.image, "fomod\\images\\logo.png")
        self.assertEqual(len(self.config.conditional_files), 1)
        self.assertIsNotNone(self.config.steps[2].visible)

    def test_defaults_follow_the_earlier_choice(self):
        wizard = self._run()
        # GAMMA is the first option of a pick-one group: chosen by default.
        self.assertTrue(wizard.is_selected(0, 0, 0))
        self.assertEqual(wizard.option_type(1, 0, 0), RECOMMENDED)
        self.assertTrue(wizard.is_selected(1, 0, 0))
        self.assertNotIn(2, wizard.visible_steps())

        vanilla = self._run(choose_vanilla=True)
        self.assertEqual(vanilla.option_type(1, 0, 1), RECOMMENDED)
        self.assertTrue(vanilla.is_selected(1, 0, 1))
        self.assertEqual(vanilla.option_type(1, 1, 0), NOT_USABLE)
        self.assertIn(2, vanilla.visible_steps())
        self.assertTrue(vanilla.is_selected(2, 0, 0))

    def test_not_usable_options_cannot_be_picked(self):
        wizard = self._run(choose_vanilla=True)
        wizard.toggle(1, 1, 0)
        self.assertFalse(wizard.is_selected(1, 1, 0))

    def test_at_most_one_can_be_unticked_and_exactly_one_cannot_be_emptied(self):
        wizard = self._run()
        wizard.toggle(1, 1, 0)
        self.assertTrue(wizard.is_selected(1, 1, 0))
        wizard.toggle(1, 1, 0)
        self.assertFalse(wizard.is_selected(1, 1, 0))
        wizard.toggle(1, 0, 0)
        self.assertTrue(wizard.is_selected(1, 0, 0))
        self.assertEqual(wizard.errors(1), [])

    def test_changing_an_earlier_choice_redoes_later_defaults(self):
        wizard = self._run()
        wizard.toggle(0, 0, 1)
        wizard.enter(1)
        self.assertTrue(wizard.is_selected(1, 0, 1))
        self.assertFalse(wizard.is_selected(1, 0, 0))

    def test_a_change_that_sets_no_flag_keeps_later_choices(self):
        wizard = self._run(choose_vanilla=True)
        wizard.toggle(2, 0, 0)  # untick the vanilla extra on the last step
        self.assertFalse(wizard.is_selected(2, 0, 0))
        wizard.toggle(1, 0, 0)  # Bar: White instead of Gold - sets no flag
        self.assertFalse(wizard.is_selected(2, 0, 0))
        wizard.enter(2)
        self.assertFalse(wizard.is_selected(2, 0, 0))

    def test_install_applies_priorities_and_conditional_files(self):
        wizard = self._run()
        out = self.tmp / "out"
        apply_options(self.config, self.root, out, wizard.selections())
        configs = out / "gamedata" / "configs"
        # base (0) < white (1) < conditional GAMMA patch (3).
        self.assertEqual((configs / "bar.ltx").read_text(encoding="utf-8"), "patch")
        self.assertTrue((configs / "patch_gamma.ltx").is_file())
        self.assertFalse((configs / "base_vanilla.ltx").exists())
        self.assertFalse((configs / "extra.ltx").exists())

    def test_hidden_step_selections_are_not_installed(self):
        wizard = self._run(choose_vanilla=True)
        self.assertIn((2, 0), wizard.selections())
        wizard.toggle(0, 0, 0)  # back to GAMMA: the vanilla-only step hides
        self.assertNotIn((2, 0), wizard.selections())
        out = self.tmp / "out"
        apply_options(self.config, self.root, out, wizard.selections())
        self.assertFalse((out / "gamedata" / "configs" / "extra.ltx").exists())


class FomodWizardDialogTest(unittest.TestCase):
    """The desktop dialog: one step at a time, Install on the last one."""

    setUp = FomodWizardTest.setUp

    def test_walks_the_visible_steps_and_returns_the_selection(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui.fomod_dialog import FomodWizardDialog

        QApplication.instance() or QApplication([])
        dialog = FomodWizardDialog(self.config, self.root)
        self.assertEqual(dialog.next_button.text(), "Next")
        self.assertIn("Step 1 of 2", dialog.step_label.text())
        self.assertEqual(dialog.detail_name.text(), "GAMMA")
        dialog._next()
        self.assertEqual(dialog.next_button.text(), "Install")
        # The NotUsable-for-vanilla menu is usable on the GAMMA path.
        menu = dialog._controls[(1, 1)][0]
        self.assertTrue(menu.isEnabled())
        dialog._back()
        dialog._toggle(0, 1)  # Vanilla: the extras step appears
        self.assertEqual(dialog.next_button.text(), "Next")
        dialog._next()
        self.assertFalse(dialog._controls[(1, 1)][0].isEnabled())
        dialog._next()
        self.assertIn("Step 3 of 3", dialog.step_label.text())
        dialog._next()
        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        self.assertIn((2, 0), dialog.selections())


class ReadOnlyArchiveTest(unittest.TestCase):
    """Archives can store read-only folders; installing must still work."""

    def test_extracted_folders_are_owner_writable(self):
        from commander_gui.mod_install import extract_archive

        tmp = Path(tempfile.mkdtemp())
        archive = tmp / "mod.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            folder = zipfile.ZipInfo("mod/gamedata/scripts/")
            folder.external_attr = (stat.S_IFDIR | 0o555) << 16
            zf.writestr(folder, "")
            script = zipfile.ZipInfo("mod/gamedata/scripts/a.script")
            script.external_attr = (stat.S_IFREG | 0o444) << 16
            zf.writestr(script, "x")
        staging = tmp / "staging"
        extract_archive(archive, staging)
        for directory, _dirs, files in os.walk(staging):
            self.assertTrue(os.access(directory, os.W_OK), directory)
            for name in files:
                self.assertTrue(os.access(Path(directory) / name, os.W_OK))


if __name__ == "__main__":
    unittest.main()
