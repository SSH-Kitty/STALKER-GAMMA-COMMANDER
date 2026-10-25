import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commander_gui import gui_settings, launch_options, overlays


class OverlayTests(unittest.TestCase):
    """MangoHud and vkBasalt: config from Settings > Graphics, the bundled
    layers where none is installed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for name, value in (("XDG_CONFIG_HOME", self.tmp / "config"), ("XDG_DATA_HOME", self.tmp / "data"), ("XDG_DATA_DIRS", "")):
            stub = patch.dict(os.environ, {name: str(value)})
            stub.start()
            self.addCleanup(stub.stop)
        overlays._install_bundled.cache_clear()
        self.addCleanup(overlays._install_bundled.cache_clear)

    def test_bundled_layers_replace_missing_ones(self):
        gui_settings.save_gui_settings(mangohud_preset="fps", vkbasalt_preset="bleak", vkbasalt_sharpness="off", vkbasalt_aa="smaa")
        with patch("shutil.which", return_value=None):
            env = {}
            command = launch_options.apply(["game"], env, "mangohud ENABLE_VKBASALT=1 %command%")
        self.assertEqual(command, ["game"])
        self.assertEqual(env["COMMANDER_MANGOHUD"], "1")
        self.assertEqual(env["COMMANDER_VKBASALT"], "1")
        self.assertNotIn("ENABLE_VKBASALT", env)
        config = Path(env["MANGOHUD_CONFIGFILE"]).read_text()
        self.assertIn("legacy_layout=0\n", config)
        self.assertIn("\nfps\n", config)
        self.assertNotIn("cpu_stats", config)
        vkbasalt = Path(env["VKBASALT_CONFIG_FILE"]).read_text()
        self.assertIn("effects = smaa:lut\n", vkbasalt)
        self.assertTrue(Path(vkbasalt.split('lutFile = "')[1].split('"')[0]).is_file())
        manifest = json.loads((self.tmp / "data/vulkan/implicit_layer.d/commander-mangohud.json").read_text())
        self.assertEqual(manifest["layer"]["enable_environment"], {"COMMANDER_MANGOHUD": "1"})
        self.assertIn("disable_environment", manifest["layer"])  # the loader skips an implicit layer without one
        self.assertTrue(Path(manifest["layer"]["library_path"]).is_file())

    def test_settings_changes_rewrite_the_live_config(self):
        gui_settings.save_gui_settings(mangohud_preset="custom", mangohud_items=["fps", "time"],
                                       mangohud_layout="bar", mangohud_background_transparency=100)
        config = overlays.write_mangohud_config().read_text()
        # Ordered as shown, with background_alpha the opposite of transparency.
        self.assertLess(config.index("\ntime\n"), config.index("\nfps\n"))
        self.assertIn("background_alpha=0.0\n", config)
        self.assertIn("\nhorizontal\n", config)

    def test_a_bar_stays_on_the_left(self):
        # MangoHud draws a bar across the whole screen, so right can't work.
        gui_settings.save_gui_settings(mangohud_layout="bar", mangohud_position="bottom-right")
        self.assertIn("position=bottom-left\n", overlays.write_mangohud_config().read_text())
        gui_settings.save_gui_settings(mangohud_layout="box")
        self.assertIn("position=bottom-right\n", overlays.write_mangohud_config().read_text())

    def test_a_right_corner_switches_the_bar_to_a_box(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.settings import CliSettings
        from commander_gui.ui.settings_page import SettingsPage

        QApplication.instance() or QApplication([])

        class FakeWindow:
            settings = CliSettings()

            def switch_language(self):
                pass

            def apply_theme(self):
                pass

        gui_settings.save_gui_settings(mangohud_layout="bar", mangohud_position="top-left")
        with patch("commander_gui.ui.settings_page.probe_discord", return_value=False):
            page = SettingsPage(FakeWindow())
        page._render_graphics()
        page._graphics._mangohud_position.setCurrentIndex(page._graphics._mangohud_position.findData("top-right"))
        self.assertEqual(page._graphics._mangohud_layout.currentData(), "box")
        saved = overlays.settings()
        self.assertEqual((saved["mangohud_layout"], saved["mangohud_position"]), ("box", "top-right"))
        # And back: a bar moves the HUD to the left corner.
        page._graphics._mangohud_layout.setCurrentIndex(page._graphics._mangohud_layout.findData("bar"))
        self.assertEqual(page._graphics._mangohud_position.currentData(), "top-left")

    def test_graphics_overlay_window_and_key(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui import graphics_window

        QApplication.instance() or QApplication([])
        self.assertEqual(graphics_window.key_setting(), "shift+grave")
        gui_settings.save_gui_settings(graphics_overlay_key="off")
        graphics_window.start_key()
        self.assertIsNone(graphics_window._grab)  # Off grabs nothing
        window = graphics_window.GraphicsWindow()
        window.toggle()
        self.assertTrue(window.isVisible())
        window.toggle()
        self.assertFalse(window.isVisible())
        # MangoHud, vkBasalt and Frame generation tabs; the last passes the
        # launch options text it changes back to COMMANDER.
        self.assertEqual(len(window.tabs.buttons()), 3)
        # A new click outside (in the game) closes it; one inside, or a
        # button held from a drag that started inside, doesn't.
        from PySide6.QtCore import QPointF

        window.toggle()
        inside = QPointF(window.frameGeometry().center())
        outside = QPointF(window.frameGeometry().right() + 50, 10)
        window.on_pointer(inside, False)
        window.on_pointer(inside, True)
        window.on_pointer(outside, True)
        self.assertTrue(window.isVisible())
        window.on_pointer(outside, False)
        window.on_pointer(outside, True)
        self.assertFalse(window.isVisible())
        sent = []
        window.options_changed.connect(sent.append)
        window.set_options("LSFGVK_ENV=1 %command%")
        window.frame_gen._set("LSFGVK_MULTIPLIER", "3")
        self.assertIn("LSFGVK_MULTIPLIER=3", sent[-1])


    def test_overlay_takes_focus_unless_the_game_is_fullscreen(self):
        from commander_gui.ui import graphics_window

        ltx = self.tmp / "anomaly" / "appdata" / "user.ltx"
        ltx.parent.mkdir(parents=True)
        for mode, fullscreen in (("fullscreen", True), ("borderless", False), ("windowed", False)):
            ltx.write_text(f"rs_render_static off\nrs_screenmode {mode}\n")
            self.assertEqual(graphics_window.game_fullscreen(self.tmp / "anomaly"), fullscreen, mode)
        self.assertFalse(graphics_window.game_fullscreen(self.tmp / "missing"))
        # Before launch, fullscreen becomes borderless; the rest stays.
        ltx.write_text("rs_screenmode fullscreen\nvid_mode 2560x1440\n")
        graphics_window.use_borderless(self.tmp / "anomaly")
        self.assertEqual(ltx.read_text(), "rs_screenmode borderless\nvid_mode 2560x1440\n")

    def test_dll_label_shows_folder_and_version(self):
        dll = self.tmp / "Lossless.dll"
        dll.write_bytes(b"MZ" + "FileVersion".encode("utf-16-le") + b"\0\0" + "3.2.2.0".encode("utf-16-le") + b"\0\0")
        self.assertEqual(launch_options.dll_label(dll), f"{dll} 3.2.2")
        dll.write_bytes(b"MZ no version here")
        self.assertEqual(launch_options.dll_label(dll), str(dll))

    def test_typed_frame_gen_values_never_break_the_config(self):
        # Hand-typed launch options: junk falls back, numbers are clamped.
        with patch("commander_gui.config.settings_dir", return_value=self.tmp):
            path = launch_options._lsfg2_config(self.tmp / "x.dll", "LSFGVK_ENV=1 LSFGVK_MULTIPLIER=abc LSFGVK_FLOW_SCALE=nan")
            self.assertIn("multiplier = 2\nflow_scale = 1.0", path.read_text())
            path = launch_options._lsfg1_config(self.tmp / "x.dll", "LSFGVK_ENV=1 LSFGVK_MULTIPLIER=99 LSFGVK_FLOW_SCALE=0.01")
            self.assertIn("multiplier = 20\nflow_scale = 0.25", path.read_text())

    def test_both_dlls_pick_the_lsfg_vk(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui import frame_gen_card

        QApplication.instance() or QApplication([])
        folder = self.tmp / "ls"
        folder.mkdir()
        for name in ("Lossless.dll", "lsfg-vk.dll"):
            (folder / name).write_bytes(b"MZ")
        gui_settings.save_gui_settings(lossless_dll=str(folder / "Lossless.dll"))
        versions = {"Lossless.dll": "1", "lsfg-vk.dll": "2"}
        text = ["LSFGVK_ENV=1 %command%"]
        with patch.object(launch_options, "frame_gen_version", side_effect=lambda dll=None: versions.get(Path(dll).name)), \
                patch.object(launch_options, "lsfg_versions", return_value={"1", "2"}), \
                patch.object(frame_gen_card, "game_has_frame_gen", return_value=None):
            card = frame_gen_card.FrameGenCard(lambda: text[0], lambda value: text.__setitem__(0, value))
            card.sync()
            self.assertFalse(card._engine_label.isHidden())
            card._engines[1][0].click()  # 2.0
            self.assertEqual(gui_settings.load_gui_settings()["lossless_dll"], str(folder / "lsfg-vk.dll"))
            self.assertTrue(card._row_labels[launch_options.FG_PRESENT].isHidden())  # no Latency on 2.0
            (folder / "Lossless.dll").unlink()
            card.sync()
            # One DLL: the other version stays in view, greyed, saying what it needs.
            self.assertFalse(card._engine_label.isHidden())
            self.assertFalse(card._engines[0][0].isEnabled())
            self.assertIn("Lossless.dll", card._engines[0][0].toolTip())

    def test_mouse_grab_set_in_the_prefix(self):
        # Off, so the overlay gets clicks over a fullscreen game; only the
        # X11 Driver section's GrabPointer changes.
        from commander_gui import launcher

        pfx = self.tmp / "prefix" / "pfx"
        pfx.mkdir(parents=True)
        reg = pfx / "user.reg"
        reg.write_text('WINE REGISTRY Version 2\n\n[Software\\\\Wine\\\\X11 Driver] 1\n"KeyboardLayoutList"="x"\n\n[Other] 2\n"a"="b"\n')
        runner = launcher.Runner(kind="proton", label="t", env={"STEAM_COMPAT_DATA_PATH": str(self.tmp / "prefix")})
        launcher.set_mouse_grab(runner, False)
        self.assertIn('"KeyboardLayoutList"="x"\n"GrabPointer"="N"\n\n[Other] 2', reg.read_text())
        launcher.set_mouse_grab(runner, True)
        self.assertEqual(reg.read_text().count("GrabPointer"), 1)
        self.assertIn('"GrabPointer"="Y"', reg.read_text())
        reg.write_text("WINE REGISTRY Version 2\n")
        launcher.set_mouse_grab(runner, False)
        self.assertIn('[Software\\\\Wine\\\\X11 Driver]', reg.read_text())
        self.assertIn('"GrabPointer"="N"', reg.read_text())

    def test_installed_ones_are_used_and_own_config_kept(self):
        gui_settings.save_gui_settings(mangohud_preset="own")
        layer = self.tmp / "data/vulkan/implicit_layer.d/vkBasalt.json"
        layer.parent.mkdir(parents=True)
        layer.write_text("{}")
        # vkBasalt: the installed one only without a bundled one.
        with patch("shutil.which", return_value="/usr/bin/mangohud"), patch.object(overlays, "_install_bundled", return_value=False):
            env = {}
            command = launch_options.apply(["game"], env, "mangohud ENABLE_VKBASALT=1 %command%")
        self.assertEqual(command, ["mangohud", "game"])
        self.assertNotIn("MANGOHUD_CONFIGFILE", env)
        self.assertEqual(env["ENABLE_VKBASALT"], "1")
        self.assertNotIn("COMMANDER_VKBASALT", env)

    def test_vkbasalt_and_native_wayland_turn_each_other_off(self):
        text = launch_options.toggle("PROTON_ENABLE_WAYLAND=1 %command%", overlays.VKBASALT, True)
        self.assertEqual(text, "ENABLE_VKBASALT=1 %command%")
        text = launch_options.toggle(text, launch_options.NATIVE_WAYLAND, True)
        self.assertEqual(text, "PROTON_ENABLE_WAYLAND=1 %command%")

    def test_a_look_sets_the_sliders(self):
        gui_settings.save_gui_settings(vkbasalt_preset="warm", vkbasalt_warmth=-40)
        self.assertEqual(overlays.settings()["vkbasalt_warmth"], overlays.VKBASALT_LOOKS["warm"][3])
        self.assertEqual(overlays.settings()["vkbasalt_custom"][3], -40)  # kept for Custom look
        gui_settings.save_gui_settings(vkbasalt_preset="custom")
        self.assertEqual(overlays.settings()["vkbasalt_warmth"], -40)

    def test_luts_stay_in_range(self):
        extremes = [(50, 50, -50, 50, -50), (-50, -50, 50, -50, 50), *overlays.VKBASALT_LOOKS.values()]
        for values in extremes:
            data = dict(zip((f"vkbasalt_{name}" for name in overlays.VKBASALT_COLOURS), values))
            rows = overlays.lut(data, 5).splitlines()
            self.assertEqual(rows[0], "LUT_3D_SIZE 5")
            self.assertEqual(len(rows), 1 + 5 ** 3)
            self.assertTrue(all(0 <= float(v) <= 1 for row in rows[1:] for v in row.split()))


    def test_looks_stay_gentle(self):
        # GAMMA is dark already: no look crushes the shadows or tints grey hard.
        for name, values in overlays.VKBASALT_LOOKS.items():
            data = dict(zip((f"vkbasalt_{key}" for key in overlays.VKBASALT_COLOURS), values))
            for grey in (0.05, 0.2, 0.5, 0.8):
                r, g, b = overlays._adjust(data, grey, grey, grey)
                luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
                with self.subTest(look=name, grey=grey):
                    self.assertLess(abs(luma / grey - 1), 0.25)
                    self.assertLess((max(r, g, b) - min(r, g, b)) / grey, 0.16)

if __name__ == "__main__":
    unittest.main()
